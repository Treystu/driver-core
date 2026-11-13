"""The untimed sidecar: a metric that is allowed to differ between two runs.

Issue #70 is the reason this file exists at all. The chain is held to
byte-identical determinism, so a duration cannot live in it, and the capture
tier's acceptance (issue #21) needs mean capture latency. The split is checked
here from both sides:

* the sidecar carries the duration, one record per capture attempt, and
* the chain is unchanged by the sidecar's presence -- the recorded run is
  compared against the frozen golden it already had, imported rather than
  copied, so a weakened golden would fail here too.

The last test is the one that matters most for #70's wording ("the byte
identical golden is never weakened"): the sidecar is exercised and the golden
is compared in the same run, so the two cannot drift into a state where the
metric works because determinism was traded away.
"""
import os
import tempfile
import unittest
from unittest import mock

from driver_core.audit import AuditLog, MemoryAuditLog
from driver_core.driver import Driver
from driver_core.perception import CLI, Target
from driver_core.states import CLI_SCHEMA
from driver_core.trace import TRACE_NAME, Stopwatch, TraceError, TraceLog, beside
from tests.test_audit_compat import (
    FIXED_NOW, golden_records, run_scenario,
)

#: Exactly the four fields #70 specifies: what a latency reader and a
#: success-rate reader need, and nothing that would turn the sidecar into a
#: second, weaker log.
FIELDS = {"step_id", "source", "ok", "elapsed_ms"}


class TimingTests(unittest.TestCase):

    def test_a_stopwatch_reports_a_non_negative_millisecond_duration(self):
        watch = Stopwatch()
        self.assertGreaterEqual(watch.elapsed_ms, 0.0)

    def test_the_clock_cannot_run_backwards_across_a_capture(self):
        """Monotonic, not wall clock: an NTP step must not produce -4000ms."""
        watch = Stopwatch()
        first = watch.elapsed_ms
        second = watch.elapsed_ms
        self.assertGreaterEqual(second, first)


class PathTests(unittest.TestCase):

    def test_the_sidecar_is_derived_from_the_audit_path(self):
        self.assertEqual(beside(os.path.join("state", "audit.jsonl")),
                         os.path.join(os.path.abspath("state"), TRACE_NAME))

    def test_an_in_memory_chain_has_no_sidecar_path(self):
        self.assertEqual(beside(""), "")

    def test_a_pathless_sink_writes_nothing_anywhere(self):
        """A run that kept its chain off the disk must not leave a trace on it.

        ``MemoryAuditLog`` exists so a test or a dry run writes nothing; a
        sidecar that invented a path would undo that in one line.
        """
        sink = TraceLog()
        record = sink.append(step_id="s", source="cli", ok=True, elapsed_ms=1.0)
        self.assertEqual(set(record), FIELDS)
        self.assertEqual(sink.read_all(), [])
        self.assertFalse(os.path.exists(TRACE_NAME))


class SidecarTests(unittest.TestCase):

    def _step(self, **kwargs):
        return Driver(audit=MemoryAuditLog(), sources=[], **kwargs).step(
            Target("unobservable", CLI), schema=CLI_SCHEMA, step_id="step-a")

    def test_a_failed_capture_is_timed_and_named_by_its_absence_of_source(self):
        with tempfile.TemporaryDirectory() as workdir:
            sink = TraceLog(os.path.join(workdir, TRACE_NAME))
            result = self._step(trace=sink)
            self.assertFalse(result.ok)
            records = sink.read_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), FIELDS)
        self.assertEqual(records[0]["step_id"], "step-a")
        self.assertIsNone(records[0]["source"])
        self.assertFalse(records[0]["ok"])
        self.assertGreaterEqual(records[0]["elapsed_ms"], 0.0)

    def test_the_sidecar_carries_no_linkage_so_it_cannot_read_as_evidence(self):
        """Unchained on purpose: no ``hash``, no ``previous``, no ``seq``.

        A file with a chain-shaped field would invite a reader to verify it,
        and it cannot be verified -- it is a measurement, and the chain beside
        it is the record of what happened.
        """
        with tempfile.TemporaryDirectory() as workdir:
            sink = TraceLog(os.path.join(workdir, TRACE_NAME))
            self._step(trace=sink)
            with open(sink.path, encoding="utf-8") as handle:
                line = handle.readline().strip()
        for forbidden in ("hash", "previous", "seq", "at"):
            self.assertNotIn(f'"{forbidden}"', line)

    def test_a_driver_puts_the_sidecar_beside_its_own_chain(self):
        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, "audit.jsonl")
            driver = Driver(audit=AuditLog(path), sources=[])
            self.assertEqual(driver.trace.path, os.path.join(workdir,
                                                             TRACE_NAME))
            driver.step(Target("unobservable", CLI), schema=CLI_SCHEMA,
                        step_id="step-a")
            self.assertTrue(os.path.exists(os.path.join(workdir, TRACE_NAME)))

    def test_an_unreadable_line_is_named_rather_than_skipped(self):
        """A hole in a metric file is a wrong mean, not a missing record."""
        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, TRACE_NAME)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write('{"elapsed_ms":1.0,"ok":true,"source":"cli",'
                             '"step_id":"s"}\n')
                handle.write("not json\n")
            with self.assertRaises(TraceError) as ctx:
                TraceLog(path).read_all()
        self.assertIn(":2 is not valid JSON", str(ctx.exception))


class TheGoldenIsNeverWeakenedTests(unittest.TestCase):
    """The chain is compared to its frozen golden *with* the sidecar live.

    The recorded scenario already runs through the real driver, so the only
    thing this adds is the claim that turning the metric on changes the chain
    not at all -- asserted against the literal, not against a fresh run of the
    same code, which would prove only that the code agrees with itself.
    """

    def test_the_recorded_run_is_still_byte_identical_while_the_trace_is_written(self):
        with tempfile.TemporaryDirectory() as workdir:
            with mock.patch("driver_core.audit._now", return_value=FIXED_NOW):
                log = run_scenario(workdir)
            self.assertEqual(log.read_all(), golden_records())
            self.assertTrue(os.path.exists(os.path.join(workdir, TRACE_NAME)))
            records = TraceLog(os.path.join(workdir, TRACE_NAME)).read_all()
        # Three steps in the scenario, each with exactly one capture attempt,
        # and the two that observed name the tier that answered.
        self.assertEqual([record["step_id"] for record in records],
                         ["step000000001", "step000000002", "step000000003"])
        self.assertEqual([record["ok"] for record in records],
                         [False, True, True])
        self.assertEqual([record["source"] for record in records],
                         [None, "cli", "cli"])

    def test_no_chained_record_carries_a_duration(self):
        """The invariant the split rests on, checked on every record kind.

        A duration anywhere in the record set -- not only on ``capture`` --
        would make the golden machine-dependent, so the whole set is scanned
        rather than the one kind the field was proposed for.
        """
        with tempfile.TemporaryDirectory() as workdir:
            with mock.patch("driver_core.audit._now", return_value=FIXED_NOW):
                log = run_scenario(workdir)
            for record in log.read_all():
                self.assertNotIn("elapsed_ms", record)
                self.assertNotIn("elapsed", record)


if __name__ == "__main__":
    unittest.main()
