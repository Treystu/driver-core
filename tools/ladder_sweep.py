"""Sweep the decision gate over recorded envelopes: the floor and the ladder.

#22 asks whether the global confidence floor and the per-action ladder change
what gets acted on. That question has two halves, and only one of them can be
answered from what this repo holds:

* **How the verdicts move.** Recorded decision envelopes carry everything the
  gate reads -- status, confidence, guards, recommended action -- so the same
  envelope can be re-judged at any threshold, with and without the ladder.
  That is this tool.
* **Whether the moved verdicts are right.** That needs judged outcomes
  attached to the envelopes, which do not exist in the repo and may not be
  manufactured. No precision or false-act rate is computed here, and none
  should be inferred from these counts: an act count is not an accuracy.

The sweep is deliberately offline and deterministic. It makes no calls, reads
a log the driver already wrote, and prints a table a reader can recompute.
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from driver_core.actions import DEFAULT_VOCABULARY          # noqa: E402
from driver_core.config import (                              # noqa: E402
    CONFIDENCE_LADDER, DEFAULT_CONFIDENCE_THRESHOLD,
)
from driver_core.jev_client import Decision                   # noqa: E402
from driver_core.policy import check_decision                 # noqa: E402
from driver_core.verdicts import PASS                         # noqa: E402

#: The thresholds swept by default. The range brackets the jitter band
#: (0.63, 0.67) and every ladder rung (0.55 / 0.75 / 0.85), so a reader can
#: see where each mechanism would start to bite.
DEFAULT_THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.71, 0.75, 0.80, 0.85, 0.90)


def envelope_from_record(record):
    """The :class:`Decision` a recorded envelope describes.

    ``expected_guards`` is taken from the record's own guard keys: the record
    is what the gate saw, so a guard the record does not carry is a guard the
    decision did not have, and :attr:`Decision.usable` must treat it that way
    rather than silently judging a smaller envelope.
    """
    guards = dict(record.get("guards") or {})
    return Decision(
        record.get("status"),
        recommended_action=record.get("recommended_action"),
        confidence=record.get("confidence"),
        probabilities=dict(record.get("probabilities") or {}),
        guards=guards,
        expected_guards=tuple(guards),
        model=record.get("model"),
        native=bool(record.get("native")),
        stop_reason=record.get("stop_reason") or "",
        probability_deviation=record.get("probability_deviation") or 0.0,
    )


def load_decisions(path):
    """Every ``decision`` record in an audit log, in file order."""
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("kind") == "decision":
                records.append(record)
    return records


def action_class_for(name):
    """The declared consequence class of ``name``, or ``None`` if unknown.

    ``None`` is honest rather than a guess: the ladder is keyed by class, and
    an action this vocabulary does not declare has no rung to apply.
    """
    if not name:
        return None
    try:
        return DEFAULT_VOCABULARY.resolve(name).action_class
    except Exception:
        return None


def sweep(records, thresholds):
    """Re-judge every record at every threshold, ladder off and on."""
    rows = []
    for threshold in thresholds:
        row = {
            "threshold": threshold,
            "ladder_off": {"pass": 0, "review": 0, "blocked": 0},
            "ladder_on": {"pass": 0, "review": 0, "blocked": 0},
            "reasons": {},
        }
        for record in records:
            decision = envelope_from_record(record)
            action_class = action_class_for(decision.recommended_action)
            arms = (("ladder_off", None),
                    ("ladder_on", CONFIDENCE_LADDER if action_class else None))
            for label, ladder in arms:
                verdict = check_decision(decision, threshold=threshold,
                                         action_class=action_class,
                                         ladder=ladder)
                row[label][verdict.verdict] = \
                    row[label].get(verdict.verdict, 0) + 1
                if verdict.verdict != PASS:
                    key = f"{label}:{verdict.reason or 'unknown'}"
                    row["reasons"][key] = row["reasons"].get(key, 0) + 1
        rows.append(row)
    return rows


def self_check(rows):
    """Two invariants the table must satisfy, checked rather than asserted.

    Raising a threshold cannot admit more decisions, and the ladder can only
    raise the bar -- the policy's own documented ordering. A table that breaks
    either is a bug in this tool or in the gate, and reporting it is the point.
    """
    problems = []
    for left, right in zip(rows, rows[1:]):
        for arm in ("ladder_off", "ladder_on"):
            if right[arm]["pass"] > left[arm]["pass"]:
                problems.append(
                    f"{arm}: threshold {right['threshold']} admits more than "
                    f"{left['threshold']}")
    for row in rows:
        if row["ladder_on"]["pass"] > row["ladder_off"]["pass"]:
            problems.append(
                f"threshold {row['threshold']}: the ladder admits more than "
                f"no ladder")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sweep the decision gate over recorded envelopes (#22)")
    parser.add_argument("--audit", required=True,
                        help="audit log holding decision records")
    parser.add_argument("--json-out", default="",
                        help="write the table and the reason counts here")
    parser.add_argument("--threshold", type=float, action="append",
                        default=None,
                        help="a threshold to sweep (repeatable)")
    args = parser.parse_args(argv)

    records = load_decisions(args.audit)
    if not records:
        print(f"no decision records in {args.audit}", file=sys.stderr)
        return 2
    thresholds = tuple(args.threshold) if args.threshold else DEFAULT_THRESHOLDS
    rows = sweep(records, thresholds)

    print(f"envelopes   : {len(records)} from {args.audit}")
    print("action mix  : " + ", ".join(
        f"{name}={count}" for name, count in sorted(_action_mix(records).items())))
    print(f"documented floor: {DEFAULT_CONFIDENCE_THRESHOLD}")
    print("")
    header = f"{'threshold':>9} | {'off pass':>8} {'off review':>10} " \
             f"{'off block':>9} | {'on pass':>7} {'on review':>9} {'on block':>8}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['threshold']:>9.2f} | "
              f"{row['ladder_off']['pass']:>8} "
              f"{row['ladder_off']['review']:>10} "
              f"{row['ladder_off']['blocked']:>9} | "
              f"{row['ladder_on']['pass']:>7} "
              f"{row['ladder_on']['review']:>9} "
              f"{row['ladder_on']['blocked']:>8}")

    at_floor = next((r for r in rows
                     if abs(r["threshold"] - DEFAULT_CONFIDENCE_THRESHOLD) < 1e-9),
                    None)
    if at_floor is not None:
        print("")
        print(f"at the documented floor {DEFAULT_CONFIDENCE_THRESHOLD}: "
              f"{at_floor['ladder_off']['pass']} pass / "
              f"{at_floor['ladder_off']['review']} review / "
              f"{at_floor['ladder_off']['blocked']} blocked (ladder off)")

    problems = self_check(rows)
    print("")
    if problems:
        for problem in problems:
            print(f"SELF-CHECK FAILED: {problem}", file=sys.stderr)
    else:
        print("self-check  : monotone in the threshold, and the ladder never "
              "admits more than no ladder")

    print("")
    print("scope: verdict movement only. No outcome labels exist for these "
          "envelopes, so no precision, false-act rate or champion comparison "
          "is computed here (#22's acceptance needs judged outcomes).")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"envelopes": len(records), "rows": rows,
                        "self_check": problems}, indent=2),
            encoding="utf-8")
        print(f"table       : {args.json_out}")
    return 1 if problems else 0


def _action_mix(records):
    mix = {}
    for record in records:
        name = record.get("recommended_action") or "none"
        mix[name] = mix.get(name, 0) + 1
    return mix


if __name__ == "__main__":
    sys.exit(main())
