"""The untimed sidecar: how long capture took, kept out of the chain.

The audit log cannot carry a duration. ``tests/test_audit_compat.py`` requires
the same run to write **byte-identical** records against a frozen golden --
"stronger than verification: identical records, not a valid chain" -- and a
wall-clock number is not reproducible: the same run on a busier machine writes
different bytes. An ``elapsed_ms`` field on the ``capture`` record therefore
fails that test by construction, which is exactly what it did when it was
tried (#70).

Rather than weaken the test -- the determinism it protects is what makes the
chain evidence rather than a diary -- the duration goes to a second file
written *beside* the log and tied to it by ``step_id`` alone. This is option 1
of #70, and #21's latency half is unmeasurable without it.

What is written, and what is deliberately not:

* One record per **capture attempt**, not per step. ``select_capture`` walks
  the tier chain and a declined source is as interesting as an answering one
  -- "how often do we fall through to pixels, and what did the fall-through
  cost" is two of the three numbers #21 reports. Fields:
  ``{step_id, source, ok, elapsed_ms}``.
* **No hash and no chain.** This file is measurement, not evidence, and
  nothing depends on it being intact -- which is precisely why it may carry a
  number the chain may not.
* **No timestamp.** A record is tied to its step by ``step_id``, and the
  chain already answers "when". A second clock reading would be a second
  answer to a question that has one owner.

The path is derived from the audit log's, so there is no second setting to
keep in step: ``audit.jsonl`` gets ``audit.trace.jsonl`` in the same
directory. A chain held only in memory (:class:`~driver_core.audit.
MemoryAuditLog`) has no path and therefore no sidecar -- nothing on disk,
nothing to derive.

A write failure is not swallowed. Capture latency exists in this file and
nowhere else, so a sink that had quietly stopped measuring would let a mean be
reported over a directory that stopped being written -- the one failure a
measurement file exists to make impossible. The file is opened when the trace
is built, so an unusable path fails at construction rather than part-way
through a step that has already spent money.
"""
import json
import os
import threading


#: Appended to the audit log's stem to name the sidecar.
#:
#: ``audit.jsonl`` -> ``audit.trace.jsonl``. A suffix rather than a sibling
#: directory because "beside the log" is the whole point: one path to find,
#: one path to ship, and no second setting that can drift away from the first.
SUFFIX = ".trace.jsonl"

#: The fields of one record, in the order they are described above.
#:
#: Kept as a name rather than being inlined so a reader (and the tests) can
#: assert the shape instead of re-spelling it. A consumer that finds a field
#: missing here has found a producer bug, not a format choice.
FIELDS = ("step_id", "source", "ok", "elapsed_ms")


def trace_path_for(audit_path):
    """The sidecar path belonging to an audit log, or ``None`` for no path.

    ``None`` rather than a default because an in-memory chain has no
    directory to be beside: deriving ``.trace.jsonl`` in the working
    directory would put measurement for a -- possibly test -- run wherever the
    process happened to be started, which is the kind of stray file the audit
    module's own docstring refuses.
    """
    if not audit_path:
        return None
    stem, _ = os.path.splitext(audit_path)
    return stem + SUFFIX


class CaptureTrace:
    """An append-only, unchained trace of capture attempts."""

    def __init__(self, path):
        if not path:
            raise ValueError(
                "a capture trace needs a path; derive one from the audit "
                "log with trace_path_for(), or pass no trace at all")
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Opened once and closed: an unusable path (a directory that cannot be
        # created, a parent that is a file, a permission the process does not
        # have) is a configuration fault and belongs at construction, not at
        # the first capture of a run that is already spending.
        with open(path, "a", encoding="utf-8", newline="\n"):
            pass

    def record(self, *, step_id, source, ok, elapsed_ms):
        """Append one attempt and return the record that was written."""
        record = {"step_id": step_id,
                  "source": source,
                  "ok": bool(ok),
                  # Milliseconds, rounded to the microsecond a perf counter
                  # can actually resolve on a wall clock. Eight decimal
                  # places would be noise dressed as precision.
                  "elapsed_ms": round(float(elapsed_ms), 3)}
        line = json.dumps(record, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
        with self._lock:
            # Opened per record rather than held open: this sink is written
            # once per capture attempt, not in a hot loop, and an append that
            # does not hold a descriptor through a long step cannot be left
            # pointing at a rotated file.
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
        return record

    def recorder(self, step_id):
        """A callback for :func:`driver_core.chain.select_capture`'s ``trace``.

        Returns a function of ``(source, ok, elapsed_ms)`` with the step
        already bound, so the tier policy never learns what a step id is and
        this module stays the only owner of the file format.
        """

        def note(source, ok, elapsed_ms):
            self.record(step_id=step_id, source=source, ok=ok,
                        elapsed_ms=elapsed_ms)

        return note

    def read_all(self):
        """Every record, in order.

        A line that does not parse is dropped rather than raised on. The
        audit log refuses one because a chain with an unexplained hole is not
        evidence; this file carries no chain, so the ordinary shape of an
        interrupted append -- a torn final line -- costs one sample rather
        than the whole measurement.
        """
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def __repr__(self):
        return f"CaptureTrace({self.path!r})"
