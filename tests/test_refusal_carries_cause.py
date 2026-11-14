"""A refusal that has the cause in hand says it.

The screen tier used to refuse like this:

    [refused] insufficient_agreement: 0 of 2 slots answered; quorum is 2.

and the real reason - `no key for vision extraction` - was computed, carried
into the tally's detail, and then discarded twice on the way to the operator:
once by the tally's shortfall branch and once by the gate that renders the
refusal. Recovering it cost three probes and a direct OS-abstraction call, and
the first hypothesis it invited (a screen-capture problem) was simply wrong.

These tests pin the sentence, not the layout: the count stays, the cause is
appended, and a slot that answered ok contributes no noise.
"""
import unittest

from driver_core.consensus import (
    INSUFFICIENT, Vote, tally, tally_boxes,
)
from driver_core.policy import check_agreement
from driver_core.states import SCREEN_SCHEMA


def _failed(slot, reason):
    return Vote(slot, "error", reason=reason)


class ShortfallNamesItsCauseTest(unittest.TestCase):
    def test_the_tally_detail_names_each_silent_slot(self):
        votes = [_failed("vision-0", "no key for vision extraction"),
                 _failed("vision-1", "no key for vision extraction")]
        agreement = tally(votes, SCREEN_SCHEMA, quorum=2)
        self.assertEqual(agreement.outcome, INSUFFICIENT)
        self.assertIn("0 of 2 slots answered", agreement.detail)
        self.assertIn("slot 'vision-0' error: no key for vision extraction",
                      agreement.detail)
        self.assertIn("slot 'vision-1' error: no key for vision extraction",
                      agreement.detail)

    def test_two_slots_failing_for_different_reasons_both_show(self):
        votes = [_failed("vision-0", "no key for vision extraction"),
                 _failed("vision-1", "capture produced no readable bytes")]
        detail = tally(votes, SCREEN_SCHEMA, quorum=2).detail
        self.assertIn("no key for vision extraction", detail)
        self.assertIn("capture produced no readable bytes", detail)

    def test_the_gate_refusal_carries_the_same_cause(self):
        votes = [_failed("vision-0", "no key for vision extraction"),
                 _failed("vision-1", "no key for vision extraction")]
        agreement = tally(votes, SCREEN_SCHEMA, quorum=2)
        result = check_agreement(agreement, quorum=2, min_agreement=1.0)
        self.assertIn("no key for vision extraction", result.detail)
        # The count that was already there must not be lost either.
        self.assertIn("0 of 2 slots answered", result.detail)

    def test_the_cause_is_named_once_not_repeated(self):
        votes = [_failed("vision-0", "no key for vision extraction"),
                 _failed("vision-1", "no key for vision extraction")]
        result = check_agreement(tally(votes, SCREEN_SCHEMA, quorum=2),
                                 quorum=2, min_agreement=1.0)
        self.assertEqual(result.detail.count("no key for vision extraction"), 2)

    def test_the_box_tally_also_names_its_cause(self):
        votes = [_failed("vision-0", "no key for vision extraction"),
                 _failed("vision-1", "no key for vision extraction")]
        agreement = tally_boxes(votes, quorum=2)
        self.assertEqual(agreement.outcome, INSUFFICIENT)
        self.assertIn("no key for vision extraction", agreement.detail)


if __name__ == "__main__":
    unittest.main()
