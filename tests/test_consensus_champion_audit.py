"""Pins the arithmetic the consensus Champion Audit (#20) turns on.

The audit report (`docs/consensus-champion-audit.md`) makes two claims that are
pure arithmetic rather than statistical, so they are cheap to pin and expensive
to get wrong:

1. **2-of-3 is below 0.67.** A "majority" threshold written as the decimal 0.67
   marks a two-of-three field *contested*, because 2/3 = 0.666... The correct
   spelling is the fraction. If someone ever "rounds" the constant, this fails.
2. **The sweep is deterministic.** The published numbers are only reproducible
   because the corpus is seeded and nothing consults a clock or a machine
   constant.
"""
import unittest

from driver_core.consensus import (
    AGREED, DISAGREED, Vote, tally,
)
from driver_core.states import SCREEN_SCHEMA


def state(title):
    return {"window_title": title, "foreground_app": "notepad",
            "error_dialog_present": False}


class TwoThirdBoundaryTests(unittest.TestCase):
    """The decimal 0.67 is not a majority rule for a 3-slot pool."""

    def votes(self):
        return [Vote.ok("a", state("Inbox")),
                Vote.ok("b", state("Inbox")),
                Vote.ok("c", state("Terminal"))]

    def test_fraction_admits_two_of_three(self):
        result = tally(self.votes(), SCREEN_SCHEMA, quorum=2,
                       min_agreement=2 / 3)
        self.assertEqual(result.outcome, AGREED)
        # Unanimity is over the answering slots, so the agreed state is one
        # extractor's actual answer, never an averaged one.
        self.assertEqual(result.state["window_title"], "inbox")

    def test_decimal_067_rejects_two_of_three(self):
        result = tally(self.votes(), SCREEN_SCHEMA, quorum=2,
                       min_agreement=0.67)
        self.assertEqual(result.outcome, DISAGREED)
        self.assertIsNone(result.state)

    def test_the_two_thresholds_really_do_differ(self):
        # Guards the guard: if these ever behaved the same, the audit's
        # "majority 0.67 is behaviourally unanimity" finding would be wrong.
        fraction = tally(self.votes(), SCREEN_SCHEMA, quorum=2,
                         min_agreement=2 / 3)
        decimal = tally(self.votes(), SCREEN_SCHEMA, quorum=2,
                        min_agreement=0.67)
        self.assertNotEqual(fraction.outcome, decimal.outcome)


class DeterminismTests(unittest.TestCase):
    """A short sweep over the same seed must reproduce exactly."""

    def test_sweep_is_deterministic(self):
        from tools.consensus_champion_audit import sweep

        first = sweep(rounds=25)
        second = sweep(rounds=25)
        self.assertEqual(first, second)

    def test_champion_never_agrees_on_a_state_no_slot_produced(self):
        """The agreed state is always byte-identical to one slot's answer.

        This is the property ``tally`` documents, and the audit's false
        agreement metric depends on it: an "agreement" is only wrong if a real
        extractor said the wrong thing.
        """
        import random

        from driver_core.schema import validate_state
        from tools.consensus_champion_audit import _round

        rng = random.Random("pinned")
        for _ in range(200):
            _truth, votes = _round(rng, 2, clean=0.8, fail=0.1, corr=0.5)
            result = tally(votes, SCREEN_SCHEMA, quorum=2, min_agreement=1.0)
            if result.outcome != AGREED:
                continue
            # ``tally`` reports the schema-normalised state, so the slots'
            # answers have to be normalised the same way before comparing.
            originals = [validate_state(v.state, SCREEN_SCHEMA)
                         for v in votes if v.status == "ok"]
            self.assertIn(result.state, originals)


if __name__ == "__main__":
    unittest.main()
