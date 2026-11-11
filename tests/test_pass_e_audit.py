"""The PASS E audit tool reads artifacts, and says so when it cannot (#55).

The tool's job is to make a PASS E reading name its own evidence, so these
tests pin the two properties that makes true: the state is built from what
the commands returned, and the same evidence always hashes to the same
digest while a changed artifact changes it. Nothing here calls a model.
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "pass_e_audit", ROOT / "tools" / "pass_e_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pass_e = _load_tool()


class TailTests(unittest.TestCase):
    def test_tail_keeps_the_last_lines_and_drops_blanks(self):
        text = "one\n\ntwo\n\nthree\nfour\n"
        self.assertEqual(pass_e.tail(text, limit=2), "three\nfour")

    def test_tail_of_nothing_is_empty(self):
        self.assertEqual(pass_e.tail("", limit=3), "")


class RunCheckTests(unittest.TestCase):
    def test_a_real_command_reports_its_return_code_and_output(self):
        check = pass_e.run_check([sys.executable, "-c",
                                  "print('ok'); raise SystemExit(3)"],
                                 cwd=ROOT, timeout=60)
        self.assertEqual(check["returncode"], 3)
        self.assertIn("ok", check["tail"])

    def test_a_missing_command_is_recorded_rather_than_raised(self):
        check = pass_e.run_check(["definitely-not-a-real-command-xyz"],
                                 cwd=ROOT, timeout=60)
        self.assertIsNone(check["returncode"])
        self.assertIn("not found", check["tail"])


class StateTests(unittest.TestCase):
    def checks(self, passed=0):
        return [
            {"command": "git rev-parse --short HEAD", "returncode": 0,
             "tail": "abc1234"},
            {"command": "python -m unittest discover -s tests -t .",
             "returncode": passed, "tail": "Ran 713 tests\n\nOK (skipped=11)"},
        ]

    def test_the_state_carries_the_head_checks_and_limits(self):
        state = pass_e.build_state(head="abc1234", checks=self.checks(),
                                   limits=["no CI has ever run"])
        self.assertEqual(state["head"], "abc1234")
        self.assertEqual(state["declared_limits"], ["no CI has ever run"])
        self.assertEqual(len(state["checks"]), 2)

    def test_the_same_evidence_always_hashes_the_same(self):
        first = pass_e.build_state(head="abc1234", checks=self.checks())
        second = pass_e.build_state(head="abc1234",
                                    checks=list(reversed(self.checks())))
        self.assertEqual(pass_e.state_digest(first),
                         pass_e.state_digest(second))

    def test_a_changed_return_code_changes_the_digest(self):
        green = pass_e.state_digest(
            pass_e.build_state(head="abc1234", checks=self.checks()))
        red = pass_e.state_digest(
            pass_e.build_state(head="abc1234", checks=self.checks(passed=1)))
        self.assertNotEqual(green, red)

    def test_a_changed_limit_changes_the_digest(self):
        base = pass_e.build_state(head="abc1234", checks=self.checks())
        other = pass_e.build_state(head="abc1234", checks=self.checks(),
                                   limits=["no CI has ever run"])
        self.assertNotEqual(pass_e.state_digest(base),
                            pass_e.state_digest(other))

    def test_the_digest_is_short_and_hex(self):
        digest = pass_e.state_digest(
            pass_e.build_state(head="abc1234", checks=self.checks()))
        self.assertEqual(len(digest), 16)
        int(digest, 16)


class KeyTests(unittest.TestCase):
    def test_asking_without_a_key_refuses_instead_of_inventing_a_reading(self):
        class Unkeyed:
            keyed = False
            jev_api_key = ""
            jev_endpoint = "https://example.invalid"

        state = pass_e.build_state(head="abc1234", checks=[])
        with self.assertRaises(SystemExit) as ctx:
            pass_e.ask(state, Unkeyed())
        self.assertIn("no Jev key configured", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
