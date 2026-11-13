"""The untimed sidecar: how long a capture took, kept out of the chain.

The audit log is a hash chain, and :mod:`tests.test_audit_compat` holds it to
the stronger of the two claims a chain can make -- the same run writes
**byte-identical** records, not merely a chain that verifies. A duration is
not reproducible: the same run on a busier machine writes different bytes, so
putting ``elapsed_ms`` into the ``capture`` record made the run stop being a
pure function of the code and broke the determinism test on exactly the
assertion it exists to make.

The metric is still needed. The capture tier's acceptance (issue #21) is a
25% reduction in *mean capture latency* or a 5-point drop in the billed-screen
rate, and with latency nowhere in the log only the second branch could ever be
satisfied from traces. So the duration lives somewhere that is allowed to
vary: a separate, unchained file written beside the audit log, one JSON object
per capture attempt::

    {"elapsed_ms": 12.418, "ok": true, "source": "cli", "step_id": "..."}

Four fields, because four is what a latency reader and a success-rate reader
need and a fifth would be a claim nobody asked for. There is no ``hash`` and
no ``previous``: this file is a measurement, not evidence. Nothing in it can
be used to argue that a step happened -- the chain does that, and it does it
without a number that changes between two runs of the same input.

The two files are told apart by purpose rather than by accident:

* the chain answers *what did it see, decide and do*, and must be identical on
  identical input;
* the sidecar answers *how long did looking take*, and is expected to differ
  on every run.

Because the sidecar is derived from the audit log's path rather than
configured separately, there is no second declaration to keep in agreement:
:func:`beside` is the one rule, and a driver with an in-memory chain has a
pathless sidecar that writes nothing at all.

A sidecar that could not be written raises, exactly as the chain does. It
would be easy to swallow that error to keep a step alive, and it would be
wrong: a run whose traces silently stopped being recorded is a run whose
latency is about to be reported from a truncated file. The failure is shared
with the audit log on purpose, at the same place in the same step.
"""
import json
import os
import threading
import time

#: The sidecar's name. Fixed rather than parameterised, because it is derived
#: from the audit path and two runs sharing a state directory are sharing one
#: trace, which is what a latency reader over a sequence of steps wants.
TRACE_NAME = "trace.jsonl"

#: How many decimal places of a millisecond survive to the file. Microsecond
#: resolution is far below any latency a capture tier is compared on, and
#: rounding here keeps a record readable instead of stringifying a float's
#: full binary expansion into a metric nobody diffs.
_PLACES = 3


def beside(audit_path):
    """The sidecar path belonging to the audit log at ``audit_path``.

    Empty in, empty out: a driver whose chain lives only in memory has no
    directory to write a trace into, and inventing one would put a stray file
    on disk for a run that deliberately kept everything off it.
    """
    if not audit_path:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(audit_path)),
                        TRACE_NAME)


class Stopwatch:
    """A duration, measured on the monotonic clock.

    ``perf_counter`` rather than ``time.time`` because this number is a
    difference between two points on the same machine: a wall clock can be
    stepped by NTP mid-capture and report a negative or wildly inflated
    latency, and a metric that occasionally reports -4000ms is one nobody can
    average.
    """

    __slots__ = ("_start",)

    def __init__(self):
        self._start = time.perf_counter()

    @property
    def elapsed_ms(self):
        """Milliseconds since construction, rounded for the file."""
        return round((time.perf_counter() - self._start) * 1000.0, _PLACES)


class TraceLog:
    """An append-only, unchained JSONL file of capture timings.

    Deliberately not a subclass of, or a variant of, :class:`AuditLog`. The
    two look similar -- append, flush, fsync, a lock -- and they are the same
    *shape* for the same reason (a crash must not leave a half-record that
    reads as a whole one), but they make opposite promises. Sharing a class
    would invite exactly the change this module exists to prevent: a field
    that cannot be reproduced being added to the chain because both files were
    written by one object.
    """

    def __init__(self, path=""):
        self.path = path
        self._lock = threading.RLock()

    def append(self, *, step_id, source, ok, elapsed_ms):
        """Record one capture attempt and return the record.

        ``source`` is ``None`` when no tier answered, which is a real outcome
        and must be recorded rather than skipped: a capture that never named a
        source still spent the time, and dropping it would bias the mean
        towards the tiers that succeeded.
        """
        record = {
            "step_id": step_id,
            "source": source,
            "ok": bool(ok),
            "elapsed_ms": round(float(elapsed_ms), _PLACES),
        }
        if not self.path:
            return record
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def read_all(self):
        """Every trace record, in the order they were written.

        A line that does not parse raises rather than being skipped. A metric
        file with a hole in it produces a mean that is quietly wrong, and a
        reader that cannot tell "no captures" from "unreadable captures" is
        worse than one that stops.
        """
        if not self.path or not os.path.exists(self.path):
            return []
        records = []
        with open(self.path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise TraceError(
                        f"{self.path}:{lineno} is not valid JSON ({exc}); the "
                        f"timings past this point cannot be trusted") from None
        return records


class TraceError(Exception):
    """The trace file is unreadable, so the metric it carries is unknown."""


__all__ = ["TRACE_NAME", "Stopwatch", "TraceError", "TraceLog", "beside"]
