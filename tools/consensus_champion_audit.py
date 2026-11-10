"""Champion Audit for the extractor consensus tally (issue #20, tracker #19).

The question this answers is narrow and numeric: **is unanimity among a
quorum-sized pool the right agreement rule, and what does it buy and cost
against a majority rule over a wider pool?**

That question is answerable offline because ``consensus.tally`` is pure
deterministic arithmetic. Given the same votes it returns the same verdict --
it touches no model, no clock, no network. So the mechanism can be measured
against a labelled corpus without spending anything, and the measurement is a
property of *the rule*, which is exactly what the Champion Audit asks about.

What this cannot tell you, and does not claim to: the real accuracy of a real
extractor. The corpus is synthetic with a declared generative model (below).
The point of the sweep is to find the regions of that model where the champion
and a challenger differ, so that any future challenger proposal has to argue
about a specific regime rather than about a hunch.

Generative model -- every token below is the same for every run and every
machine, so the numbers in ``docs/consensus-champion-audit.md`` reproduce by
re-running this file:

* A round has a ground-truth :data:`SCREEN_SCHEMA` state.
* Each slot independently fails to answer with probability ``fail`` (a
  :data:`consensus.SLOT_ERROR` vote -- no state at all, which the tally
  correctly refuses to treat as a vote).
* An answering slot reports the truth with probability ``clean``. Otherwise it
  reports a state in which exactly one field is wrong.
* ``corr`` is the correlation knob and the reason this audit matters. With
  probability ``corr`` the wrong value is drawn from a *narrow* per-round pool
  shared by all slots, modelling the case unanimity cannot defend against: two
  extractors reading the same corrupted source and agreeing on the same lie.
  Otherwise the wrong value is drawn from a wide pool, so two slots almost
  never collide by accident.

Metrics, per arm:

* **false-agreement rate** -- of all rounds, the fraction the arm called
  ``agreed`` while the agreed state was *not* the ground truth. This is the
  dangerous error: a confident wrong state reaches the decision tier.
* **blocked-but-correct rate** -- of all rounds, the fraction the arm refused
  (``insufficient`` or ``disagreed``) *even though* the majority of the slots
  that actually answered agreed on the truth. This is the expensive error: the
  right answer was available and the rule declined to use it.

Usage::

    python tools/consensus_champion_audit.py
    python tools/consensus_champion_audit.py --rounds 8000 --json out.json

Exit code 0 means the sweep ran and every arm was measured; it does not mean
any arm "passed". The verdict belongs to the issue, not the tool.
"""
import argparse
import json
import random
import sys

from driver_core.consensus import Vote, tally
from driver_core.schema import validate_state
from driver_core.states import SCREEN_SCHEMA

#: The tally's outcome token for "a state was agreed"; imported as a name so a
#: rename in consensus.py is a failure here rather than a silently dead branch.
AGREED = "agreed"

#: Ground-truth pools. Fixed and small so a "wrong" value is unambiguously
#: wrong after the schema's own provenance normalisation.
TITLES = ("Quarterly Report", "Untitled - Notepad", "Settings", "Inbox",
          "Terminal", "Save As")
APPS = ("notepad", "chrome", "explorer", "terminal", "settings")
TEXTS = ("No changes pending", "3 items selected", "Ready", "Path not found")
LABELS = ("File name", "Search", "Address", "Password")
KINDS = ("error", "warning", "confirmation", "information", "file_picker")
CONTROLS = ("Save", "Delete", "Overwrite", "Replace")

#: The regimes swept. Each is a point in (clean, fail, corr) space, chosen to
#: span "both extractors are good and independent" through "both are reading a
#: shared corrupted source", because those are the two ends that matter.
REGIMES = (
    ("clean-independent",        dict(clean=0.95, fail=0.00, corr=0.00)),
    ("good-independent",         dict(clean=0.90, fail=0.05, corr=0.00)),
    ("mixed-independent",        dict(clean=0.80, fail=0.10, corr=0.00)),
    ("poor-independent",         dict(clean=0.70, fail=0.15, corr=0.00)),
    ("good-lowslotless",         dict(clean=0.90, fail=0.20, corr=0.00)),
    ("good-correlated",          dict(clean=0.90, fail=0.05, corr=0.50)),
    ("mixed-correlated",         dict(clean=0.80, fail=0.10, corr=0.50)),
    ("good-highly-correlated",   dict(clean=0.90, fail=0.05, corr=0.90)),
)

#: The arms. The champion is the shipped default *including its wiring*: the
#: pool is sized to the quorum, so the default pool is exactly 2 and unanimity
#: among 2 answering slots is the rule.
ARMS = (
    ("champion (pool 2, quorum 2, unanimity)",
     dict(pool=2, quorum=2, min_agreement=1.0)),
    ("pool 3, quorum 3, unanimity",
     dict(pool=3, quorum=3, min_agreement=1.0)),
    # The challenger the issue's own premise points at but does not propose.
    # It keeps the unanimity rule exactly (so it cannot manufacture a false
    # agreement the champion would have refused) and only decouples pool size
    # from quorum, so one slot FAILING no longer makes agreement arithmetically
    # impossible. Today `configured_pools` sets slots = quorum, which is why the
    # champion's blocked-but-correct rate rises with the failure rate.
    ("pool 3, quorum 2, unanimity (fault-tolerant)",
     dict(pool=3, quorum=2, min_agreement=1.0)),
    ("pool 3, quorum 2, majority 0.67",
     dict(pool=3, quorum=2, min_agreement=0.67)),
    ("pool 3, quorum 2, majority 0.5",
     dict(pool=3, quorum=2, min_agreement=0.5)),
    # 2/3 is written as a fraction on purpose. The issue proposes "a majority
    # rule (min_agreement 0.5-0.67)", but 2-of-3 is 0.666..., which is strictly
    # BELOW 0.67 -- so the stated range cannot express the rule it names. These
    # two arms are the unambiguous majority rule, and they are the ones that
    # should be compared against the champion.
    ("pool 3, quorum 2, majority exactly 2/3",
     dict(pool=3, quorum=2, min_agreement=2 / 3)),
    ("pool 3, quorum 3, majority exactly 2/3",
     dict(pool=3, quorum=3, min_agreement=2 / 3)),
)

CHAMPION_INDEX = 0


def _truth(rng):
    """One ground-truth state. Optional fields are present about half the time."""
    state = {
        "window_title": rng.choice(TITLES),
        "foreground_app": rng.choice(APPS),
        "error_dialog_present": rng.random() < 0.3,
    }
    if rng.random() < 0.5:
        state["visible_text"] = rng.choice(TEXTS)
    if state["error_dialog_present"] or rng.random() < 0.2:
        state["dialog_kind"] = rng.choice(KINDS)
        state["focused_field_label"] = rng.choice(LABELS)
        state["blocking_controls"] = rng.choice(CONTROLS)
    return state


def _corrupt(state, rng, narrow):
    """A copy of ``state`` with exactly one field replaced by a wrong value.

    ``narrow`` picks from the shared per-round pool, which is how a correlated
    error is modelled: the same lie is conveniently available to every slot.
    """
    broken = dict(state)
    field = rng.choice(("window_title", "foreground_app", "error_dialog_present"))
    if field == "error_dialog_present":
        broken[field] = not state[field]
        return broken
    pool = (TITLES if field == "window_title" else APPS)
    # The truth is never a candidate: a "wrong" value that happens to equal
    # the truth would silently label a correct answer as a false agreement.
    choices = sorted(v for v in pool if v != state[field])
    if narrow:
        # Only two candidate lies, so independent draws collide often.
        choices = choices[:2]
    broken[field] = rng.choice(choices)
    return broken


def _round(rng, pool, clean, fail, corr):
    """One labelled extraction round: the truth and ``pool`` slots' votes."""
    truth = _truth(rng)
    narrow = rng.random() < corr
    shared = {}
    votes = []
    for slot in range(pool):
        if rng.random() < fail:
            votes.append(Vote.error(f"slot{slot}", "extractor timed out"))
            continue
        if rng.random() < clean:
            votes.append(Vote.ok(f"slot{slot}", dict(truth)))
            continue
        if narrow:
            # Every slot in a correlated round sees the same corrupted state.
            if "state" not in shared:
                shared["state"] = _corrupt(truth, rng, narrow=True)
            votes.append(Vote.ok(f"slot{slot}", dict(shared["state"])))
        else:
            votes.append(Vote.ok(f"slot{slot}", _corrupt(truth, rng, narrow=False)))
    return truth, votes


def _modal_answer_is_truth(truth, votes):
    """Did a majority of the *answering* slots land on the truth?

    This is the fairness test for "blocked but correct": a refusal only counts
    against an arm if the right answer was actually discoverable from the votes
    it was given.
    """
    answering = [v for v in votes if v.status == "ok"]
    if not answering:
        return False
    correct = sum(1 for v in answering
                  if all(v.state.get(k) == truth.get(k) for k in truth))
    return correct > len(answering) / 2


def measure(arm, corpus):
    """Score one arm over one corpus of ``(truth, votes)`` rounds."""
    pool = arm["pool"]
    rounds = agreed = wrong_agreement = blocked_correct = 0
    for truth, votes in corpus:
        if len(votes) != pool:                      # corpus is per-pool
            raise SystemExit("corpus/arm pool size mismatch")
        rounds += 1
        # The tally reports the schema's *normalised* state, so the truth has
        # to be normalised the same way before it is compared. Skipping this
        # scores every casefolded title as a false agreement and makes the
        # clean regime look catastrophic -- a labelling bug, not a finding.
        expected = validate_state(truth, SCREEN_SCHEMA)
        result = tally(votes, SCREEN_SCHEMA,
                       quorum=arm["quorum"],
                       min_agreement=arm["min_agreement"])
        if result.outcome == AGREED:
            agreed += 1
            if result.state != expected:
                wrong_agreement += 1
        elif _modal_answer_is_truth(truth, votes):
            blocked_correct += 1
    return {
        "rounds": rounds,
        "agreed": agreed,
        "false_agreement_rate": wrong_agreement / rounds if rounds else 0.0,
        "blocked_but_correct_rate": blocked_correct / rounds if rounds else 0.0,
        "wrong_agreements": wrong_agreement,
        "blocked_but_correct": blocked_correct,
    }


def sweep(rounds=4000, seed=20261107):
    """Run every arm over every regime. Fully deterministic."""
    out = {}
    for name, params in REGIMES:
        # One corpus per pool size, drawn from the same stream in a fixed
        # order, so every arm in a regime sees the identical rounds.
        corpora = {}
        for pool in sorted({a[1]["pool"] for a in ARMS}):
            rng = random.Random(f"{seed}:{name}:{pool}")
            corpora[pool] = [_round(rng, pool, **params) for _ in range(rounds)]
        out[name] = {
            "params": params,
            "arms": {label: measure(cfg, corpora[cfg["pool"]])
                     for label, cfg in ARMS},
        }
    return out


def verdicts(results):
    """Apply the issue's acceptance rule to every arm, per regime.

    The criterion, quoted from #20: a challenger ships only if it cuts
    blocked-but-correct rounds by >=20% relative with **zero** increase in
    false agreements, otherwise the champion stands.
    """
    champion = ARMS[CHAMPION_INDEX][0]
    out = {}
    for regime, block in results.items():
        champ = block["arms"][champion]
        rows = {}
        for label, m in block["arms"].items():
            if label == champion:
                continue
            base = champ["blocked_but_correct"]
            if base:
                cut = (base - m["blocked_but_correct"]) / base
            else:
                cut = None
            false_delta = (m["false_agreement_rate"]
                           - champ["false_agreement_rate"])
            passes = cut is not None and cut >= 0.20 and false_delta <= 0
            rows[label] = {"blocked_cut_relative": cut,
                           "false_agreement_delta": false_delta,
                           "passes": passes}
        out[regime] = rows
    return out


def _pct(x):
    return f"{100 * x:.2f}%"


def report(results, rounds):
    lines = []
    lines.append("# Champion Audit -- extractor consensus tally (#20)\n")
    lines.append(f"Corpus: synthetic, {rounds} rounds per regime, "
                 f"seed `20261107`, generative model declared in "
                 f"`tools/consensus_champion_audit.py`.\n")
    lines.append("Arms are named by rule; the first is the shipped champion "
                 "(pool sized to quorum, so 2 slots and unanimity).\n")
    for name, block in results.items():
        p = block["params"]
        lines.append(f"\n## {name}\n")
        lines.append(f"clean={p['clean']} fail={p['fail']} corr={p['corr']}\n")
        lines.append("| arm | agreed | false-agreement | blocked-but-correct |")
        lines.append("|---|---|---|---|")
        for label, m in block["arms"].items():
            lines.append(
                f"| {label} | {m['agreed']} | {_pct(m['false_agreement_rate'])} "
                f"| {_pct(m['blocked_but_correct_rate'])} |")

    # Acceptance rule from #20, applied verbatim: >=20% relative cut in
    # blocked-but-correct with zero increase in false agreements.
    lines.append("\n## Acceptance test against the champion\n")
    lines.append("A challenger passes only with a >=20% relative cut in "
                 "blocked-but-correct and **no** increase in "
                 "false-agreement.\n")
    lines.append("| regime | arm | blocked cut | false-agreement delta | verdict |")
    lines.append("|---|---|---|---|---|")
    for regime, rows in verdicts(results).items():
        for label, v in rows.items():
            cut = v["blocked_cut_relative"]
            cut_text = "n/a" if cut is None else _pct(cut)
            deltas = v["false_agreement_delta"]
            delta_text = ("0.00%" if deltas == 0
                          else f"{'+' if deltas > 0 else ''}{100 * deltas:.2f} pp")
            lines.append(f"| {regime} | {label} | {cut_text} | {delta_text} "
                         f"| {'PASS' if v['passes'] else 'fail'} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rounds", type=int, default=4000,
                        help="rounds per regime (default 4000)")
    parser.add_argument("--seed", type=int, default=20261107,
                        help="corpus seed (default 20261107)")
    parser.add_argument("--json", metavar="PATH",
                        help="also write the raw measurement here")
    parser.add_argument("--markdown", metavar="PATH",
                        help="also write the rendered report here")
    args = parser.parse_args(argv)

    results = sweep(rounds=args.rounds, seed=args.seed)
    text = report(results, args.rounds)
    sys.stdout.write(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"seed": args.seed, "rounds": args.rounds,
                       "regimes": results}, handle, indent=2, sort_keys=True)
            handle.write("\n")
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
