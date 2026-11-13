"""The untimed sidecar (#70): latency beside the chain, never inside it.

Two properties are checked here, and they pull in opposite directions:

* a duration **is** measured, once per capture attempt, by what a caller
  actually waited for -- otherwise #21's latency half is a number nobody can
  compute;
* the chain still says exactly what it said before, byte for byte -- the claim
  ``tests/test_audit_compat.py`` makes, and the reason the duration could not
  simply be added to the ``capture`` record.

The second is checked against the golden scenario itself, because the failure
being defended against is not a crash: it is a run that still passes its own
tests while writing a file it should not have touched.
"""
import json
import os
import tempfile
import time
import unittest

from driver_core.audit import AuditLog, MemoryAuditLog
from driver_core.budget import Budget
from driver_core.chain import select_capture
from driver_core.config import load_settings
from driver_core.driver import Driver, driver_from_settings
from driver_core.ev import FakeJev, action_answer
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.perception import CLI, StructuredSource, Target
from driver_core.states import CLI_SCHEMA
from driver_core.trace import FIELDS, CaptureTrace, trace_path_for

PAYLOAD = {"exit_code": 0, "stdout": "observed", "stderr": ""}


def _answers(target):
    """A structured source that carries state, so its capture is ``ok``."""
    return dict(PAYLOAD)


def _declines(target):
    """A structured source with nothing to say."""
    return None


def _reader(capture, schema):
    return dict(capture.payload)


def _driver(workdir, *, trace=True, sources=None, jev=None, slots=2):
    """A real driver over a real chain file, with the tier order in it.

    The chain is on disk rather than in memory because the sidecar's path is
    derived from it -- an in-memory log has no path and therefore no sidecar,
    which is a case of its own below.
    """
    settings = load_settings(
        env={}, quorum=slots, min_agreement=1.0, confidence_threshold=0.7,
        run_ceiling_usd=1.0, step_ceiling_usd=1.0, dry_run=True,
    )
    audit_path = os.path.join(workdir, "audit.jsonl")
    return Driver(
        settings=settings,
        budget=Budget(1.0, step_ceiling_usd=1.0),
        audit=AuditLog(audit_path),
        pool=ExtractorPool([StructuredExtractor(f"cli-{i}", _reader)
                            for i in range(slots)]),
        jev=jev or FakeJev(action_answer("observe", confidence=0.95)),
        sources=[StructuredSource("cli", _answers)] if sources is None
        else sources,
        trace=CaptureTrace(trace_path_for(audit_path)) if trace else None,
    )


class TraceFormatTests(unittest.TestCase):
    """What one record is, and where the file lives."""

    def test_the_sidecar_is_named_beside_the_log_it_belongs_to(self):
        expected = os.path.join("state", "audit.trace.jsonl")
        actual = trace_path_for(os.path.join("state", "audit.jsonl"))
        self.assertEqual(actual, expected)

    def test_a_chain_with_no_path_has_no_sidecar(self):
        """An in-memory log must not scatter a measurement file beside the
        process that happened to start it."""
        self.assertIsNone(trace_path_for(""))
        self.assertIsNone(trace_path_for(MemoryAuditLog().path))

    def test_every_attempt_is_recorded_with_the_declared_fields(self):
        with tempfile.TemporaryDirectory() as workdir:
            trace = CaptureTrace(os.path.join(workdir, "audit.trace.jsonl"))
            # An undeclared target, because this test is about the hook rather
            # than about the class rule: it lets two differently-named
            # structured sources be candidates for one target, so the record
            # sequence shows the tier order being walked.
            select_capture(
                Target("observed"),
                [StructuredSource("cli", _declines),
                 StructuredSource("mcp", _answers)],
                trace=trace.recorder("step000000001"))

            records = trace.read_all()
            self.assertEqual([r["source"] for r in records], ["cli", "mcp"],
                             "a declined attempt is data: it is how the "
                             "fall-through rate is measured")
            self.assertEqual([r["ok"] for r in records], [False, True])
            for record in records:
                self.assertEqual(tuple(sorted(record)), tuple(sorted(FIELDS)))
                self.assertEqual(record["step_id"], "step000000001")
                self.assertIsInstance(record["elapsed_ms"], float)
                self.assertGreaterEqual(record["elapsed_ms"], 0.0)

    def test_the_duration_is_the_wait_the_caller_actually_had(self):
        def slow(target):
            time.sleep(0.03)
            return dict(PAYLOAD)

        with tempfile.TemporaryDirectory() as workdir:
            trace = CaptureTrace(os.path.join(workdir, "audit.trace.jsonl"))
            select_capture(
                Target("observed"),
                [StructuredSource("cli", _declines),
                 StructuredSource("mcp", slow)],
                trace=trace.recorder("step000000002"))
            declined, answered = trace.read_all()
            self.assertGreaterEqual(answered["elapsed_ms"], 20.0,
                                    "a 30ms capture reported as faster than "
                                    "20ms is not measuring the call")
            self.assertGreater(answered["elapsed_ms"], declined["elapsed_ms"])

    def test_a_torn_final_line_costs_one_sample_not_the_measurement(self):
        """The sidecar carries no chain, so the interrupted-append case is
        dropped rather than raised: the audit log's rule exists because a hole
        in evidence is not evidence, and this file is not evidence."""
        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, "audit.trace.jsonl")
            trace = CaptureTrace(path)
            trace.record(step_id="s", source="cli", ok=True, elapsed_ms=1.0)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write('{"step_id":"s","sou')
            records = trace.read_all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source"], "cli")

    def test_an_unusable_path_is_refused_when_the_trace_is_built(self):
        """Failing at construction is the point: a sink that died mid-run
        would let a mean be reported over a directory that stopped being
        written, which is the one failure a measurement file exists to make
        impossible."""
        with tempfile.TemporaryDirectory() as workdir:
            blocker = os.path.join(workdir, "not-a-directory")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("x")
            with self.assertRaises(OSError):
                CaptureTrace(os.path.join(blocker, "audit.trace.jsonl"))
        with self.assertRaises(ValueError):
            CaptureTrace("")


class ChainIsUntouchedTests(unittest.TestCase):

    def test_a_step_writes_the_same_records_with_the_sidecar_on(self):
        """Same records, same kinds, same fields -- and a verifiable chain.

        The strong claim stays where it was made, in
        :mod:`tests.test_audit_compat`; this checks that turning the sidecar
        on adds a file rather than changing one.
        """
        with tempfile.TemporaryDirectory() as workdir:
            driver = _driver(workdir)
            result = driver.step(Target("observed", CLI), schema=CLI_SCHEMA,
                                 step_id="step000000003")
            self.assertTrue(result.ok, result.detail)

            records = driver.audit.read_all()
            self.assertTrue(records)
            self.assertNotIn("elapsed_ms", json.dumps(records))
            self.assertTrue(driver.audit.verify().ok)

            sidecar = CaptureTrace(
                trace_path_for(os.path.join(workdir, "audit.jsonl")))
            self.assertEqual([r["step_id"] for r in sidecar.read_all()],
                             ["step000000003"])

    def test_the_byte_identical_golden_run_writes_no_sidecar(self):
        """The golden scenario builds its drivers with its own chain and no
        trace, so the run that the compatibility test compares byte for byte
        leaves exactly one file in its directory."""
        from tests.test_audit_compat import run_scenario

        with tempfile.TemporaryDirectory() as workdir:
            log = run_scenario(workdir)
            self.assertEqual(sorted(os.listdir(workdir)), ["audit.jsonl"])
            self.assertTrue(log.verify().ok)

    def test_a_driver_with_no_trace_writes_no_file(self):
        with tempfile.TemporaryDirectory() as workdir:
            driver = _driver(workdir, trace=False)
            self.assertIsNone(driver.trace)
            driver.step(Target("observed", CLI), schema=CLI_SCHEMA,
                        step_id="step000000004")
            self.assertEqual(sorted(os.listdir(workdir)), ["audit.jsonl"])

    def test_a_declined_capture_is_traced_even_though_the_step_stops(self):
        """The failure path is where a tier audit needs the data most: a
        source that declined is exactly the one whose latency and rate #21
        reports."""
        with tempfile.TemporaryDirectory() as workdir:
            driver = _driver(workdir,
                             sources=[StructuredSource("cli", _declines)])
            result = driver.step(Target("observed", CLI), schema=CLI_SCHEMA,
                                 step_id="step000000005")
            self.assertFalse(result.ok)
            self.assertEqual(result.stopped_at, "capture")

            records = CaptureTrace(
                trace_path_for(os.path.join(workdir, "audit.jsonl"))
            ).read_all()
            self.assertEqual([(r["source"], r["ok"]) for r in records],
                             [("cli", False)])


class EntryPointWiringTests(unittest.TestCase):

    def test_the_pairing_puts_one_sidecar_beside_the_declared_chain(self):
        """One setting decides where a run's records live; the measurements
        live next to them, derived rather than configured."""
        with tempfile.TemporaryDirectory() as workdir:
            settings = load_settings(
                env={}, audit_path=os.path.join(workdir, "audit.jsonl"))
            driver = driver_from_settings(settings)
            self.assertIsNotNone(driver.trace)
            self.assertEqual(driver.trace.path,
                             trace_path_for(settings.audit_path))
            self.assertEqual(driver.trace.path,
                             os.path.join(workdir, "audit.trace.jsonl"))


if __name__ == "__main__":
    unittest.main()
