"""Run PASS E over artifacts instead of prose (#55).

PASS E asks one evidence-rating question per axis -- correctness,
completeness, honesty -- about the repository's current state. Run over a
summary someone wrote by hand, it measures the summary: the same head read
0.20 and then 0.60 for correctness with only the wording changed (#55).

So this tool builds the state from **artifacts**: the exact command lines it
ran, their return codes, and the tails of their output, for the head they ran
against. The state is hashed, and the digest travels with the reading, so two
readings can be compared only when they were taken over the same evidence.

It is deliberately small and stdlib-only, like everything else here. It does
not decide anything: a reading is information, and an axis below the bar
starts an investigation.

Run it:

    python tools/pass_e_audit.py --no-call      # artifacts + digest, no cost
    DRIVER_JEV_API_KEY=... python tools/pass_e_audit.py --interpreter python3.13
    DRIVER_JEV_API_KEY=... python tools/pass_e_audit.py --json-out build/pass_e.json

Without ``--no-call`` and without a key it exits non-zero and says why, rather
than printing an empty report that reads like a clean audit.
"""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from driver_core import transport                                  # noqa: E402
from driver_core.config import load_settings                       # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The three axes. One question each, asked over the same artifact state.
AXES = ("correctness", "completeness", "honesty")

#: How many trailing output lines describe a check. Enough to carry the
#: verdict line ("OK (skipped=11)") without pasting a whole suite log.
TAIL_LINES = 6


def questions():
    """The axis questions, anchored to artifacts rather than to claims."""
    return {
        "correctness": transport.noul(
            "The state lists command lines, their return codes, and the tails "
            "of their output, run against the stated head. Do these artifacts "
            "establish that the code on this head is correct within the scope "
            "the commands cover?",
            true="Every listed command ran on this head, its return code is "
                 "zero, and its output names the tests or checks that passed.",
            false="A command is missing, its return code is not zero, or the "
                  "output does not show what the command claims to cover."),
        "completeness": transport.noul(
            "Does the state's artifact list cover the repository's declared "
            "green bar, with any gap named in declared_limits rather than "
            "left implicit?",
            true="The battery, the linter and the compile check are present "
                 "for every declared interpreter, and every gap is named.",
            false="An artifact of the declared bar is absent and not named as "
                  "a limit."),
        "honesty": transport.noul(
            "Does the state report what the commands actually returned, "
            "including failures, without presenting a local reading as more "
            "than it measured?",
            true="Return codes and output are reported as they came back, and "
                 "local-only evidence is declared as local.",
            false="A reading is presented as more than it measured, or a "
                  "failure is omitted from the state."),
    }


def tail(text, limit=TAIL_LINES):
    """The last ``limit`` non-blank lines of ``text``."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def run_check(command, *, cwd, timeout=1800):
    """Run one check and record what it actually did.

    A command that cannot be started is recorded as such -- ``returncode``
    ``None`` and the reason in the tail -- because a missing interpreter is a
    fact about the evidence, not a reason to stop collecting it.
    """
    argv = [str(part) for part in command]
    try:
        finished = subprocess.run(argv, cwd=str(cwd), capture_output=True,
                                  text=True, timeout=timeout)
    except FileNotFoundError:
        return {"command": " ".join(argv), "returncode": None,
                "tail": "command not found on this surface"}
    except subprocess.TimeoutExpired:
        return {"command": " ".join(argv), "returncode": None,
                "tail": f"timed out after {timeout}s"}
    output = (finished.stdout or "") + (finished.stderr or "")
    return {"command": " ".join(argv), "returncode": finished.returncode,
            "tail": tail(output)}


def build_state(*, head, checks, limits=()):
    """The artifact state: head, the checks that ran, and the declared limits.

    ``checks`` are sorted by command line so two runs that ran the same
    commands build byte-identical states and therefore the same digest.
    """
    ordered = sorted((dict(check) for check in checks),
                     key=lambda check: check.get("command", ""))
    return {
        "head": str(head),
        "checks": ordered,
        "declared_limits": [str(limit) for limit in limits],
    }


def state_digest(state):
    """A short digest of the state, so a reading can name its own evidence."""
    material = json.dumps(state, sort_keys=True, separators=(",", ":"),
                          default=str).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def collect(repo_root, interpreters, *, timeout=1800):
    """Run the lane's green bar and return the checks, in the order given."""
    root = pathlib.Path(repo_root)
    checks = [run_check(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                        timeout=timeout)]
    for interpreter in interpreters:
        checks.append(run_check(
            [interpreter, "-W", "error::ResourceWarning", "-m", "unittest",
             "discover", "-s", "tests", "-t", "."], cwd=root, timeout=timeout))
    checks.append(run_check([sys.executable, "-m", "ruff", "check",
                             "driver_core", "tests"], cwd=root, timeout=timeout))
    checks.append(run_check([sys.executable, "-m", "compileall", "-q",
                             "driver_core"], cwd=root, timeout=timeout))
    return checks


def ask(state, settings):
    """Ask the three axis questions over ``state``; return the readings."""
    if not settings.keyed:
        raise SystemExit(
            "no Jev key configured: set DRIVER_JEV_API_KEY (or run with "
            "--no-call to print the artifacts and their digest)")
    response = transport.call_service(
        settings.jev_endpoint, questions(),
        headers={"Authorization": f"Bearer {settings.jev_api_key}"},
        timeout=120)
    payload = transport.require(response, "jev")
    answers = (payload.get("answers") or {})
    readings = {}
    for axis in AXES:
        answer = answers.get(axis) or {}
        readings[axis] = answer.get("noul")
    return {"readings": readings, "model": payload.get("model"),
            "is_fallback": payload.get("is_fallback"),
            "usage": payload.get("usage")}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(REPO_ROOT),
                        help="the tree to audit (default: this repository)")
    parser.add_argument("--interpreter", action="append", default=None,
                        help="an interpreter to run the battery on; repeatable "
                             "(default: the one running this tool)")
    parser.add_argument("--json-out", default=None,
                        help="write the whole result -- state, digest, "
                             "readings -- to this path")
    parser.add_argument("--no-call", action="store_true",
                        help="collect the artifacts and print the digest; "
                             "make no calls and need no key")
    parser.add_argument("--limit", action="append", default=None,
                        help="a declared limit of this evidence, recorded in "
                             "the state; repeatable")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="per-command timeout in seconds (default: 1800)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    interpreters = args.interpreter or [sys.executable]
    root = pathlib.Path(args.root)

    checks = collect(root, interpreters, timeout=args.timeout)
    head = next((check["tail"].strip().splitlines()[-1]
                 for check in checks
                 if check["command"].startswith("git rev-parse")), "")

    limits = list(args.limit or ())
    if args.no_call:
        limits.append("readings not taken: --no-call")

    state = build_state(head=head, checks=checks, limits=limits)
    digest = state_digest(state)
    result = {"head": head, "state_digest": digest, "state": state}

    if not args.no_call:
        result.update(ask(state, load_settings(env=os.environ)))

    text = json.dumps(result, indent=2, default=str)
    print(text)
    if args.json_out:
        out = pathlib.Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")

    if args.no_call:
        return 0
    readings = result.get("readings") or {}
    return 0 if all(reading is not None for reading in readings.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
