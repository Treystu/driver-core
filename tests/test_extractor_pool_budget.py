"""A ceiling refusal is a spend stop, not an extractor fault (#63).

`ExtractorPool.run` isolates a slot failure so one bad extractor cannot take
the step down. That isolation used to swallow `BudgetRefused` as well, so a
step the ceiling had stopped was reported as an extraction shortfall - the
bound held, the signal was lost, and the capture got the blame.
"""
import unittest

from driver_core.budget import Budget
from driver_core.extractors.pool import ExtractorPool


class _Slot:
    """A slot that answers, or raises whatever it was built to raise."""

    def __init__(self, slot, *, raises=None, state=None, uses_capture=True):
        self.slot = slot
        self.uses_capture = uses_capture
        self._raises = raises
        self._state = state if state is not None else {"title": slot}

    def extract(self, *args):
        if self._raises is not None:
            raise self._raises

        class _Result:
            ok = True
            state = self._state
            cost = 0.0
            usage_source = "actual"
            reason = ""

        return _Result()


class _RefusingSlot(_Slot):
    """Takes a real reservation, so the budget's own refusal is what raises."""

    def __init__(self, slot, budget, amount):
        super().__init__(slot)
        self.budget = budget
        self.amount = amount

    def extract(self, *args):
        self.budget.reserve(self.amount, label=self.slot).settle(self.amount)
        return super().extract(*args)


class RefusalTests(unittest.TestCase):
    def test_a_budget_refusal_is_marked_and_does_not_read_as_a_fault(self):
        budget = Budget(1.0, step_ceiling_usd=0.05)
        pool = ExtractorPool([
            _RefusingSlot("slot-a", budget, 0.04),
            _RefusingSlot("slot-b", budget, 0.04),
        ])
        with budget.step_scope("step-1"):
            votes = pool.run(None, schema=None)

        self.assertEqual(len(votes), 2)
        self.assertEqual(votes[0].status, "ok")
        self.assertIn("budget refused", votes[1].reason)
        self.assertNotIn("extractor raised", votes[1].reason)
        self.assertIsNotNone(pool.refused())
        self.assertIn("0.05", pool.refused())

    def test_a_genuine_slot_fault_is_still_a_fault_and_not_a_refusal(self):
        pool = ExtractorPool([_Slot("slot-a", raises=RuntimeError("wire died"))])
        votes = pool.run(None, schema=None)
        self.assertIn("extractor raised", votes[0].reason)
        self.assertIsNone(pool.refused())

    def test_a_run_with_no_refusal_reports_none(self):
        pool = ExtractorPool([_Slot("slot-a")])
        votes = pool.run(None, schema=None)
        self.assertEqual(votes[0].status, "ok")
        self.assertIsNone(pool.refused())

    def test_the_mark_does_not_survive_into_the_next_run(self):
        budget = Budget(1.0, step_ceiling_usd=0.05)
        pool = ExtractorPool([
            _RefusingSlot("slot-a", budget, 0.04),
            _RefusingSlot("slot-b", budget, 0.04),
        ])
        with budget.step_scope("step-1"):
            pool.run(None, schema=None)
        self.assertIsNotNone(pool.refused())

        pool_again = ExtractorPool([_Slot("slot-a")])
        pool_again.run(None, schema=None)
        self.assertIsNone(pool_again.refused())


if __name__ == "__main__":
    unittest.main()
