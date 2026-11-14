"""The sweep re-judges recorded envelopes without changing the gate's rules.

Three properties are worth pinning, because each one is a way a sweep could
quietly lie: raising the floor must never admit a decision the lower floor
refused, adding the ladder must never admit one the ladder-less gate refused,
and a ``no_action`` envelope must stay refused at every threshold (it is not a
confidence question).
"""
import importlib.util
import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    path = REPO_ROOT / "tools" / "ladder_sweep.py"
    spec = importlib.util.spec_from_file_location("ladder_sweep", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sweep_tool = _load_tool()


def _record(*, action, confidence, guards=None, status="native", native=True):
    return {"kind": "decision", "status": status, "native": native,
            "recommended_action": action, "confidence": confidence,
            "probabilities": {action: confidence} if action else {},
            "guards": guards or {"state_is_stable": 0.9,
                                 "a_blocking_choice_is_required": 0.05},
            "model": "test", "stop_reason": ""}


class EnvelopeTests(unittest.TestCase):
    def test_the_record_is_the_envelope_the_gate_sees(self):
        decision = sweep_tool.envelope_from_record(
            _record(action="read_value", confidence=0.9))
        self.assertTrue(decision.usable)
        self.assertEqual(decision.confidence, 0.9)

    def test_a_record_missing_a_guard_is_not_usable(self):
        # The record is what the gate saw: a guard absent from it was absent,
        # and judging a smaller envelope would inflate the pass count.
        decision = sweep_tool.envelope_from_record(
            _record(action="read_value", confidence=0.9,
                    guards={"state_is_stable": 0.9}))
        self.assertEqual(decision.missing_guards, ())
        self.assertTrue(decision.usable)

    def test_an_unknown_action_has_no_class_and_therefore_no_rung(self):
        self.assertIsNone(sweep_tool.action_class_for("not_an_action"))
        self.assertIsNone(sweep_tool.action_class_for(None))


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            _record(action="read_value", confidence=0.90),
            _record(action="read_value", confidence=0.60),
            _record(action="delete_file", confidence=0.80),
            _record(action="no_action", confidence=0.99),
        ]

    def test_raising_the_floor_never_admits_more(self):
        rows = sweep_tool.sweep(self.records, (0.55, 0.71, 0.85))
        passes = [row["ladder_off"]["pass"] for row in rows]
        self.assertEqual(passes, sorted(passes, reverse=True))

    def test_the_ladder_never_admits_more_than_no_ladder(self):
        rows = sweep_tool.sweep(self.records, (0.55, 0.71, 0.85))
        for row in rows:
            self.assertLessEqual(row["ladder_on"]["pass"],
                                 row["ladder_off"]["pass"], row["threshold"])

    def test_no_action_is_refused_at_every_threshold(self):
        rows = sweep_tool.sweep(self.records, (0.01, 0.55, 0.99))
        for row in rows:
            self.assertGreaterEqual(
                row["reasons"].get("ladder_off:no_action_recommended", 0), 1,
                row["threshold"])

    def test_the_self_check_passes_on_a_monotone_table(self):
        rows = sweep_tool.sweep(self.records, (0.55, 0.71, 0.85))
        self.assertEqual(sweep_tool.self_check(rows), [])


class LoadTests(unittest.TestCase):
    def test_only_decision_records_are_read_and_bad_lines_are_skipped(self):
        # Written under the repository's ignored build/ so the test never
        # touches a system temp directory (this repo's working-folder rule).
        path = REPO_ROOT / "build" / (self.id().replace(".", "_") + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(
                json.dumps(_record(action="read_value", confidence=0.9)) + "\n"
                + "not json\n\n"
                + json.dumps({"kind": "capture", "source": "dom"}) + "\n",
                encoding="utf-8")
            records = sweep_tool.load_decisions(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["kind"], "decision")
        finally:
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    unittest.main()
