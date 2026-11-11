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


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.original = pass_e.run_check

        def fake_run_check(command, *, cwd, timeout=1800):
            self.seen.append(list(command))
            return {"command": " ".join(str(part) for part in command),
                    "returncode": 0, "tail": "ok"}

        pass_e.run_check = fake_run_check

    def tearDown(self):
        pass_e.run_check = self.original

    def batteries(self):
        return [command for command in self.seen
                if "unittest" in command]

    def test_the_running_interpreter_battery_is_always_collected(self):
        pass_e.collect(ROOT, ["python3.11", "python3.13"])
        interpreters = [command[0] for command in self.batteries()]
        self.assertIn(sys.executable, interpreters)
        self.assertEqual(interpreters.count(sys.executable), 1)

    def test_passing_the_running_interpreter_does_not_duplicate_it(self):
        pass_e.collect(ROOT, [sys.executable, "python3.11"])
        interpreters = [command[0] for command in self.batteries()]
        self.assertEqual(interpreters.count(sys.executable), 1)
        self.assertEqual(len(interpreters), 2)

    def test_the_state_still_carries_one_lint_and_one_compile_check(self):
        pass_e.collect(ROOT, ["python3.11"])
        joined = [" ".join(command) for command in self.seen]
        self.assertEqual(sum("ruff" in one for one in joined), 1)
        self.assertEqual(sum("compileall" in one for one in joined), 1)
        self.assertEqual(sum("git rev-parse" in one for one in joined), 1)


class AskOnlyTests(unittest.TestCase):
    def test_a_collected_state_can_be_asked_over_without_collecting_again(self):
        import json
        import tempfile

        state = pass_e.build_state(
            head="abc1234",
            checks=[{"command": "git rev-parse --short HEAD",
                     "returncode": 0, "tail": "abc1234"}],
            limits=["no CI has ever run"])
        digest = pass_e.state_digest(state)

        class Keyed:
            keyed = True
            jev_api_key = "test-key"
            jev_model = "jev-test"

        class FakeResponse:
            ok = True
            payload = {"answers": {"correctness": {"type": "noul", "noul": 0.8},
                                    "completeness": {"type": "noul", "noul": 0.9},
                                    "honesty": {"type": "noul", "noul": 0.95}},
                       "model": "jev-test"}

        original_call = pass_e.transport.call_service
        original_settings = pass_e.load_settings
        pass_e.transport.call_service = lambda *a, **k: FakeResponse()
        pass_e.load_settings = lambda env=None: Keyed()
        try:
            with tempfile.TemporaryDirectory() as folder:
                recorded = pathlib.Path(folder) / "state.json"
                recorded.write_text(json.dumps({"head": "abc1234",
                                                "state": state}),
                                    encoding="utf-8")
                out = pathlib.Path(folder) / "reading.json"
                code = pass_e.main(["--ask-only", str(recorded),
                                    "--json-out", str(out)])
                result = json.loads(out.read_text(encoding="utf-8"))
        finally:
            pass_e.transport.call_service = original_call
            pass_e.load_settings = original_settings

        self.assertEqual(code, 0)
        self.assertEqual(result["state_digest"], digest)
        self.assertAlmostEqual(result["readings"]["correctness"], 0.8)

    def test_a_file_that_is_not_a_collected_state_is_refused(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "not-a-state.json"
            path.write_text(json.dumps({"something": "else"}),
                            encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                pass_e.existing_state(path)
        self.assertIn("does not contain a collected state", str(ctx.exception))


class KeyTests(unittest.TestCase):
    def test_asking_without_a_key_refuses_instead_of_inventing_a_reading(self):
        class Unkeyed:
            keyed = False
            jev_api_key = ""

        state = pass_e.build_state(head="abc1234", checks=[])
        with self.assertRaises(SystemExit) as ctx:
            pass_e.ask(state, Unkeyed())
        self.assertIn("no Jev key configured", str(ctx.exception))

    def test_asking_uses_the_service_url_rather_than_reading_it_off_settings(self):
        # The first keyed run died on ``settings.jev_endpoint``: Settings owns
        # no such attribute, and only a keyed run reaches the line. A fake
        # settings object with just the two attributes the tool needs pins it.
        class Keyed:
            keyed = True
            jev_api_key = "test-key"
            jev_model = "jev-test"

        class FakeResponse:
            ok = True
            payload = {
                "answers": {"correctness": {"type": "noul", "noul": 0.9},
                            "completeness": {"type": "noul", "noul": 0.8},
                            "honesty": {"type": "noul", "noul": 0.95}},
                "model": "jev-test",
                "is_fallback": False,
            }

        seen = {}

        def fake_call(url, questions, **kwargs):
            seen["url"] = url
            seen["questions"] = questions
            seen["body_extra"] = kwargs.get("body_extra")
            return FakeResponse()

        original = pass_e.transport.call_service
        pass_e.transport.call_service = fake_call
        try:
            result = pass_e.ask(pass_e.build_state(head="abc1234", checks=[]),
                                Keyed())
        finally:
            pass_e.transport.call_service = original

        self.assertEqual(seen["url"], pass_e.SYSTEM_ONE_URL)
        self.assertEqual(set(seen["questions"]), set(pass_e.AXES))
        # The service answers 422 without a model, and the state is what the
        # questions are about: both must travel.
        self.assertEqual(seen["body_extra"]["model"], "jev-test")
        self.assertIn("state", seen["body_extra"])
        self.assertAlmostEqual(result["readings"]["correctness"], 0.9)
        self.assertEqual(result["model"], "jev-test")


if __name__ == "__main__":
    unittest.main()
