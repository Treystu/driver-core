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
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from driver_core import transport                                  # noqa: E402
from driver_core.config import load_settings                       # noqa: E402
from driver_core.jev_client import SYSTEM_ONE_URL                   # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The three axes. One question each, asked over the same artifact state.
AXES = ("correctness", "completeness", "honesty")

#: How many trailing output lines describe a check. Enough to carry the
#: verdict line ("OK (skipped=11)") without pasting a whole suite log.
TAIL_LINES = 6

#: Limits that are true of every collection this lane can take, so a state
#: always carries them unless a caller says otherwise (#62). Without them a
#: state can list six green checks and still leave out the two facts that most
#: change how a reading should be read: nothing has ever run in CI, and every
#: number came from one machine.
DEFAULT_LIMITS = (
    "no CI has ever run on this repository: every result here is a local run",
    "one surface only: no second machine, container, or non-author review has "
    "reproduced these results",
)

#: What each kind of check is for. A state that lists only command lines makes
#: the reader infer coverage from the argv; naming the purpose makes the state
#: describe itself.
PURPOSES = ("head", "battery", "lint", "compile")


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


class CollectionError(RuntimeError):
    """A check could not be resolved, so a state recording it would lie."""


#: Run-to-run noise in a check's output. The suite prints how long it took, so
#: without this two collections of the same evidence would hash differently and
#: no reading could be compared with the next one.
_ELAPSED = re.compile(r"\bin \d+(?:\.\d+)?s\b")


def normalise(text):
    """Strip the run-to-run noise from a check's output."""
    return _ELAPSED.sub("in <elapsed>", (text or "").replace("\r\n", "\n"))


def resolve_interpreter(name, *, root=None):
    """An absolute path to a real interpreter, or a loud refusal.

    Recording "command not found" as a check's result is how a state came to
    describe a thin audit as though it were a real one: the reading then rates
    the hole instead of the repository. Resolution happens before anything
    runs, so a missing interpreter stops the collection rather than becoming an
    entry inside it.
    """
    candidate = str(name)
    # A relative candidate means "relative to the repository being audited",
    # because that is the working directory ``run_check`` executes it in. It
    # used to be resolved against the caller's cwd, so launching the audit
    # from outside the repo (``--root /path/to/repo --interpreter
    # venv/bin/python``) rejected an interpreter that had always worked
    # (Codex, PR #61). Repo-relative first, PATH second.
    if root is not None and not pathlib.Path(candidate).is_absolute():
        repo_relative = pathlib.Path(root) / candidate
        if repo_relative.exists():
            # Absolute, but NOT resolved: ``venv/bin/python`` is a symlink to
            # the base installation on most platforms, and following it would
            # run the base interpreter - discarding the virtualenv's
            # site-packages and sys.prefix, which is the entire reason the
            # audit accepts a repo-relative interpreter (Codex, PR #78).
            return os.path.abspath(repo_relative)
    found = shutil.which(candidate)
    if found is None:
        raise CollectionError(
            f"the interpreter {candidate!r} does not resolve on this surface "
            f"(PATH lookup found nothing); pass an absolute path to a real "
            f"install - a state that records an unresolvable check is not "
            f"evidence")
    return found


def tail(text, limit=TAIL_LINES):
    """The last ``limit`` non-blank lines of ``text``, with noise removed."""
    lines = [line for line in normalise(text).splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def run_check(command, *, cwd, timeout=1800, purpose=None, interpreter=None):
    """Run one check and record what it actually did.

    A command that cannot be started stops the collection: a state that
    records a missing command beside real results is a state that reads as a
    completed audit, which is the failure this module exists to prevent. A
    timeout is different - the command did start and did not finish - so it is
    recorded as a ``None`` return code with the reason, which is a fact about
    the evidence rather than a missing input.
    """
    argv = [str(part) for part in command]
    record = {}
    if purpose is not None:
        record["purpose"] = str(purpose)
        record["interpreter"] = None if interpreter is None else str(interpreter)
    try:
        finished = subprocess.run(argv, cwd=str(cwd), capture_output=True,
                                  text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise CollectionError(
            f"the check {' '.join(argv)!r} could not be started: {exc}. That "
            f"is a missing input, not a result, so the collection stops "
            f"rather than recording it as evidence") from exc
    except subprocess.TimeoutExpired:
        return {**record, "command": " ".join(argv), "returncode": None,
                "tail": f"timed out after {timeout}s"}
    output = (finished.stdout or "") + (finished.stderr or "")
    return {**record, "command": " ".join(argv),
            "returncode": finished.returncode, "tail": tail(output)}


def build_state(*, head, checks, limits=None):
    """The artifact state: head, the checks that ran, and the declared limits.

    ``checks`` are sorted by command line so two runs that ran the same
    commands build byte-identical states and therefore the same digest.

    ``limits`` defaults to :data:`DEFAULT_LIMITS`: a caller who passes nothing
    still gets the standing limits, because an empty list let a state read as
    unqualified evidence when it never was (#62).
    """
    ordered = sorted((dict(check) for check in checks),
                     key=lambda check: check.get("command", ""))
    declared = DEFAULT_LIMITS if limits is None else limits
    return {
        "head": str(head),
        "checks": ordered,
        "declared_limits": [str(limit) for limit in declared],
    }


def state_digest(state):
    """A short digest of the state, so a reading can name its own evidence."""
    material = json.dumps(state, sort_keys=True, separators=(",", ":"),
                          default=str).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def collect(repo_root, interpreters, *, timeout=1800):
    """Run the lane's green bar and return the checks.

    The interpreter running this tool is always included, deduplicated: a
    state that omits the interpreter the audit itself runs on reports an
    incomplete bar while presenting a summary that looks whole, which is the
    silent hole this tool exists to close. ``ruff`` and ``compileall`` are
    already invoked through ``sys.executable``, so without this the state
    mixed one interpreter's toolchain with other interpreters' batteries.
    """
    root = pathlib.Path(repo_root)
    order = []
    for interpreter in list(interpreters) + [sys.executable]:
        resolved = resolve_interpreter(interpreter, root=repo_root)
        if resolved not in order:
            order.append(resolved)
    checks = [run_check(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                        timeout=timeout, purpose="head")]
    for interpreter in order:
        checks.append(run_check(
            [interpreter, "-W", "error::ResourceWarning", "-m", "unittest",
             "discover", "-s", "tests", "-t", "."], cwd=root, timeout=timeout,
            purpose="battery", interpreter=interpreter))
    checks.append(run_check([sys.executable, "-m", "ruff", "check",
                             "driver_core", "tests"], cwd=root, timeout=timeout,
                            purpose="lint", interpreter=sys.executable))
    checks.append(run_check([sys.executable, "-m", "compileall", "-q",
                             "driver_core"], cwd=root, timeout=timeout,
                            purpose="compile", interpreter=sys.executable))
    return checks


def ask(state, settings):
    """Ask the three axis questions over ``state``; return the readings."""
    if not settings.keyed:
        raise SystemExit(
            "no Jev key configured: set DRIVER_JEV_API_KEY (or run with "
            "--no-call to print the artifacts and their digest)")
    # The endpoint is a property of the service, not of the settings: there
    # is one System One URL and ``jev_client`` owns it. Reading it off
    # settings was a bug that only a keyed run could reach.
    response = transport.call_service(
        SYSTEM_ONE_URL, questions(),
        headers={"Authorization": f"Bearer {settings.jev_api_key}"},
        # The service rejects a body without ``model`` (HTTP 422), and the
        # state is what the questions are asked about -- both travel exactly
        # as the package's own client sends them.
        body_extra={"state": state, "model": settings.jev_model},
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
    parser.add_argument("--ask-only", default=None, metavar="PATH",
                        help="ask over a state already collected into PATH "
                             "(a --json-out file); collects nothing, so the "
                             "call does not wait on a fresh battery")
    parser.add_argument("--limit", action="append", default=None,
                        help="a declared limit of this evidence, recorded in "
                             "the state; repeatable")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="per-command timeout in seconds (default: 1800)")
    return parser.parse_args(argv)


def existing_state(path):
    """The state recorded in a ``--json-out`` file, with its digest.

    Collection is the slow half of an audit - a full battery is minutes on a
    development machine - and asking is seconds. Separating them means a
    reading can be taken from a state that was collected earlier, without
    running the green bar again just to ask about it.
    """
    recorded = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    state = recorded.get("state")
    if not isinstance(state, dict) or "checks" not in state:
        raise SystemExit(f"{path} does not contain a collected state")
    return state


def main(argv=None):
    args = parse_args(argv)
    if args.ask_only:
        state = existing_state(args.ask_only)
        digest = state_digest(state)
        result = {"head": state.get("head"), "state_digest": digest,
                  "state": state}
        result.update(ask(state, load_settings(env=os.environ)))
        text = json.dumps(result, indent=2, default=str)
        print(text)
        if args.json_out:
            out = pathlib.Path(args.json_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8")
        readings = result.get("readings") or {}
        return 0 if all(value is not None
                        for value in readings.values()) else 1

    interpreters = args.interpreter or [sys.executable]
    root = pathlib.Path(args.root)

    try:
        checks = collect(root, interpreters, timeout=args.timeout)
    except CollectionError as exc:
        raise SystemExit(str(exc)) from exc
    head = next((check["tail"].strip().splitlines()[-1]
                 for check in checks
                 if check["command"].startswith("git rev-parse")), "")

    # Stand on the defaults rather than replace them: passing any --limit used
    # to swap the standing limits out, which would have re-opened the hole
    # this cure closes the moment a caller declared one limit of its own.
    limits = list(DEFAULT_LIMITS) + list(args.limit or ())
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
