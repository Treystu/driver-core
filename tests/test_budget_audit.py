"""Budget and audit-log tests.

Both modules exist to make two promises the rest of the system relies on:
spend is bounded *before* it happens, and history is verifiable *after* it
happens. The concurrency tests are real threads rather than simulated
interleavings, because the failure being defended against -- several workers
each preflighting against the full remainder and collectively overspending
it -- is precisely a check-then-act race and cannot be observed sequentially.
"""
import os
import tempfile
import threading
import unittest

from driver_core.audit import AuditError, AuditLog, GENESIS, MemoryAuditLog
from driver_core.budget import Budget
from driver_core.errors import BudgetRefused


class ReservationTests(unittest.TestCase):

    def test_a_reservation_holds_its_amount_against_the_ceiling(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.4, label="a")
        self.assertEqual(budget.reserved, 0.4)
        self.assertEqual(budget.remaining, 0.6)
        reservation.settle(0.3)
        self.assertEqual(budget.spent, 0.3)
        self.assertEqual(budget.reserved, 0.0)

    def test_settling_releases_the_hold_and_charges_the_actual(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        reservation.settle(0.1)
        self.assertAlmostEqual(budget.spent, 0.1)
        self.assertAlmostEqual(budget.remaining, 0.9)

    def test_an_unmeasurable_call_is_charged_the_full_reservation(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        charged = reservation.settle(None)
        self.assertAlmostEqual(charged, 0.5)
        self.assertEqual(reservation.usage_source, "unavailable")

    def test_cancelling_charges_nothing(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        reservation.cancel()
        self.assertEqual(budget.spent, 0.0)
        self.assertEqual(budget.reserved, 0.0)
        self.assertEqual(budget.remaining, 1.0)

    def test_a_reservation_settles_exactly_once(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        reservation.settle(0.1)
        with self.assertRaises(RuntimeError):
            reservation.settle(0.1)
        self.assertAlmostEqual(budget.spent, 0.1)

    def test_a_cancelled_reservation_cannot_then_settle(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        reservation.cancel()
        with self.assertRaises(RuntimeError):
            reservation.settle(0.1)
        self.assertEqual(budget.spent, 0.0)


class CeilingTests(unittest.TestCase):

    def test_an_over_ceiling_reservation_is_refused_before_dispatch(self):
        budget = Budget(0.10, step_ceiling_usd=0.10)
        with self.assertRaises(BudgetRefused) as ctx:
            budget.reserve(0.50, label="expensive")
        self.assertEqual(ctx.exception.estimated, 0.50)
        self.assertEqual(ctx.exception.ceiling, 0.10)
        self.assertEqual(budget.spent, 0.0)

    def test_a_refusal_carries_the_math(self):
        budget = Budget(0.10, step_ceiling_usd=0.10)
        with self.assertRaises(BudgetRefused) as ctx:
            budget.reserve(0.50, label="clip")
        self.assertIn("0.500000", str(ctx.exception))
        self.assertIn("0.100000", str(ctx.exception))
        self.assertIn("clip", str(ctx.exception))

    def test_the_step_ceiling_bites_before_the_run_ceiling(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        with self.assertRaises(BudgetRefused) as ctx:
            budget.reserve(0.5)
        self.assertEqual(ctx.exception.ceiling, 0.05)

    def test_a_leaked_reservation_is_visible_rather_than_silent(self):
        budget = Budget(1.0)
        budget.reserve(0.5, label="leaked")
        self.assertEqual(len(budget.open_reservations()), 1)
        budget.reserve(0.5, label="ok").cancel()
        self.assertEqual(len(budget.open_reservations()), 1)


class AccountingPrecisionTests(unittest.TestCase):
    """The comparison must agree with the balance the budget reports.

    ``snapshot`` rounds to 9 places and the ledger rounds with it, so the
    pre-flight check does too. Comparing raw floats made a ceiling of
    N x cost admit only N-1 charges, and then reported one cent remaining
    while refusing a one-cent charge.
    """

    def test_an_exact_fit_charge_is_admitted_to_the_last_cent(self):
        budget = Budget(0.30)
        for index in range(30):
            budget.reserve(0.01, label=f"c{index}").settle(0.01)
        self.assertEqual(round(budget.spent, 9), 0.30)
        self.assertEqual(budget.remaining, 0.0)
        self.assertEqual(len(budget.open_reservations()), 0)

    def test_an_exact_fit_charge_is_admitted_at_five_cent_steps(self):
        budget = Budget(1.00)
        for index in range(20):
            budget.reserve(0.05, label=f"c{index}").settle(0.05)
        self.assertEqual(budget.snapshot()["spent_usd"], 1.00)
        self.assertEqual(budget.snapshot()["remaining_usd"], 0.0)

    def test_the_report_and_the_enforcement_agree_at_the_boundary(self):
        budget = Budget(0.30)
        for index in range(29):
            budget.reserve(0.01, label=f"c{index}").settle(0.01)
        self.assertEqual(budget.snapshot()["remaining_usd"], 0.01)
        budget.reserve(0.01, label="last").settle(0.01)
        self.assertEqual(budget.snapshot()["remaining_usd"], 0.0)

    def test_a_remaining_balance_refusal_names_remaining_not_ceiling(self):
        budget = Budget(1.0)
        budget.reserve(0.95, label="most").settle(0.95)
        with self.assertRaises(BudgetRefused) as ctx:
            budget.reserve(0.50, label="over")
        message = str(ctx.exception)
        self.assertIn("exceeds remaining", message)
        self.assertNotIn("exceeds ceiling", message)
        self.assertIn("0.050000", message)
        self.assertEqual(ctx.exception.limit, "remaining")

    def test_a_step_ceiling_refusal_still_says_ceiling(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        with self.assertRaises(BudgetRefused) as ctx:
            budget.reserve(0.5)
        self.assertIn("exceeds ceiling", str(ctx.exception))
        self.assertEqual(ctx.exception.limit, "ceiling")


class StepCeilingTests(unittest.TestCase):
    """A step ceiling of zero is a request, not an absence."""

    def test_a_zero_step_ceiling_is_refused_rather_than_widened(self):
        with self.assertRaises(ValueError) as ctx:
            Budget(5.0, step_ceiling_usd=0.0)
        self.assertIn("step_ceiling_usd", str(ctx.exception))

    def test_a_negative_step_ceiling_is_refused(self):
        with self.assertRaises(ValueError):
            Budget(5.0, step_ceiling_usd=-1.0)

    def test_no_step_ceiling_inherits_the_run_ceiling(self):
        budget = Budget(5.0)
        self.assertEqual(budget.reserve(4.9, label="").state, "reserved")

    def test_a_tiny_step_ceiling_is_still_respected(self):
        budget = Budget(5.0, step_ceiling_usd=1e-9)
        with self.assertRaises(BudgetRefused) as ctx:
            budget.reserve(0.01)
        self.assertEqual(ctx.exception.ceiling, 1e-9)


class ConcurrencyTests(unittest.TestCase):

    def test_racing_workers_cannot_collectively_overspend(self):
        """40 threads race to reserve 0.1 against a 1.0 ceiling. Exactly 10
        may win. A check-then-act budget would admit more."""
        budget = Budget(1.0, step_ceiling_usd=1.0)
        admitted = []
        lock = threading.Lock()
        start = threading.Barrier(40)

        def attempt():
            start.wait()
            try:
                reservation = budget.reserve(0.1, label="race")
            except BudgetRefused:
                return
            with lock:
                admitted.append(reservation)

        threads = [threading.Thread(target=attempt) for _ in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(admitted), 10)
        self.assertLessEqual(budget.reserved, 1.0)

    def test_racing_settlements_never_double_charge(self):
        budget = Budget(10.0, step_ceiling_usd=10.0)
        reservations = [budget.reserve(0.1) for _ in range(20)]
        errors = []

        def settle(reservation):
            try:
                reservation.settle(0.05)
            except Exception as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        threads = [threading.Thread(target=settle, args=(r,))
                   for r in reservations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertAlmostEqual(budget.spent, 20 * 0.05)


class AuditChainTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "audit.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_a_chain_verifies_after_appending(self):
        log = AuditLog(self.path)
        log.append("capture", step_id="s1", source="cli")
        log.append("decision", step_id="s1", action="observe")
        verdict = log.verify()
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.records, 2)

    def test_the_first_record_links_to_genesis(self):
        log = AuditLog(self.path)
        record = log.append("capture", step_id="s1")
        self.assertEqual(record["previous"], GENESIS)
        self.assertEqual(record["seq"], 0)

    def test_tampering_with_a_record_breaks_verification(self):
        log = AuditLog(self.path)
        log.append("capture", step_id="s1", source="cli")
        log.append("decision", step_id="s1", action="observe")
        with open(self.path, "r+", encoding="utf-8") as handle:
            body = handle.read().replace('"action":"observe"',
                                         '"action":"delete_file"')
            handle.seek(0)
            handle.write(body)
        verdict = AuditLog(self.path).verify()
        self.assertFalse(verdict.ok)
        self.assertIn("altered", verdict.detail)

    def test_a_deleted_record_stops_the_chain_being_extended(self):
        """Deleting a record is caught the moment the log is reopened, and
        the log refuses to continue rather than writing past the hole."""
        log = AuditLog(self.path)
        log.append("capture", step_id="s1")
        log.append("decision", step_id="s1", action="observe")
        log.append("action", step_id="s1", action="click")
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.readlines()
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.writelines([lines[0], lines[2]])
        with self.assertRaises(AuditError):
            AuditLog(self.path)

    def test_a_restarted_run_extends_the_existing_chain(self):
        first = AuditLog(self.path)
        first.append("capture", step_id="s1")
        second = AuditLog(self.path)
        record = second.append("decision", step_id="s2")
        self.assertEqual(record["seq"], 1)
        self.assertEqual(record["previous"], first.head())
        self.assertTrue(AuditLog(self.path).verify().ok)

    def test_a_corrupt_line_stops_the_chain_from_being_extended(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        with self.assertRaises(AuditError):
            AuditLog(self.path)

    def test_an_unaltered_record_is_byte_stable(self):
        """Given the same facts, the serialised bytes and therefore the hash
        are identical. Without this a chain verifies once and then fails for
        no reason anyone can find."""
        from driver_core.audit import _canonical, digest
        record = {"seq": 0, "kind": "capture", "at": "2026-01-01T00:00:00",
                  "previous": GENESIS, "step_id": "s1", "source": "cli"}
        first = digest(record, GENESIS)
        second = digest(dict(reversed(list(record.items()))), GENESIS)
        self.assertEqual(first, second)
        self.assertEqual(_canonical(record), _canonical(
            dict(reversed(list(record.items())))))

    def test_a_truncated_record_returns_a_failed_verdict_rather_than_raising(self):
        path = os.path.join(self.dir.name, "truncated.jsonl")
        log = AuditLog(path)
        log.append("capture", step_id="s1", source="cli")
        log.append("capture", step_id="s2", source="cli")
        with open(path, "rb+") as handle:
            handle.seek(0, os.SEEK_END)
            handle.truncate(os.path.getsize(path) - 12)
        verdict = log.verify()
        self.assertFalse(verdict.ok)
        self.assertFalse(verdict)
        self.assertIn("not valid JSON", verdict.detail)
        self.assertEqual(verdict.records, 1)

    def test_an_altered_record_is_still_reported_as_drift(self):
        path = os.path.join(self.dir.name, "altered.jsonl")
        log = AuditLog(path)
        log.append("capture", step_id="s1", source="cli")
        log.append("capture", step_id="s2", source="cli")
        with open(path, encoding="utf-8") as handle:
            text = handle.read().replace('"cli"', '"forged"', 1)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        verdict = log.verify()
        self.assertFalse(verdict.ok)
        self.assertIn("altered", verdict.detail)

    def test_a_clean_chain_verifies_without_rereading_the_file(self):
        path = os.path.join(self.dir.name, "clean.jsonl")
        log = AuditLog(path)
        for index in range(3):
            log.append("capture", step_id=f"s{index}", source="cli")
        verdict = log.verify()
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.records, 3)
        self.assertEqual(verdict.detail, "chain verified")

    def test_two_independent_runs_record_the_same_facts(self):
        one = MemoryAuditLog()
        one.append("capture", step_id="s1", source="cli")
        two = MemoryAuditLog()
        two.append("capture", step_id="s1", source="cli")
        a, b = one.read_all()[0], two.read_all()[0]
        self.assertEqual(a["kind"], b["kind"])
        self.assertEqual(a["source"], b["source"])
        self.assertEqual(a["seq"], b["seq"])


class StepScopeTests(unittest.TestCase):
    """``step_ceiling_usd`` bounds a step, not each of its dispatches.

    The defect these cover: checked one call at a time, a step that made ten
    dispatches was bounded by ten times the declared cap, so the number in the
    configuration was not the number enforced.
    """

    def test_a_step_cannot_spend_its_ceiling_more_than_once(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        with budget.step_scope("s1"):
            budget.reserve(0.03, label="rung-1").settle(0.03)
            with self.assertRaises(BudgetRefused) as ctx:
                budget.reserve(0.03, label="rung-2")
        self.assertEqual(ctx.exception.ceiling, 0.05)
        self.assertEqual(budget.spent, 0.03)

    def test_the_refusal_names_the_step_and_the_call(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        with budget.step_scope("step-7"):
            budget.reserve(0.04, label="rung-1")
            with self.assertRaises(BudgetRefused) as ctx:
                budget.reserve(0.04, label="rung-2")
        message = str(ctx.exception)
        self.assertIn("step-7", message)
        self.assertIn("rung-2", message)

    def test_an_open_reservation_counts_against_the_step(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        with budget.step_scope("s1"):
            leaked = budget.reserve(0.04, label="held")
            with self.assertRaises(BudgetRefused):
                budget.reserve(0.04, label="second")
            leaked.cancel()
            budget.reserve(0.04, label="after-cancel").settle(0.04)

    def test_each_step_gets_its_own_ceiling(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        for step in ("s1", "s2"):
            with budget.step_scope(step):
                budget.reserve(0.05).settle(0.05)
        self.assertAlmostEqual(budget.spent, 0.10)

    def test_a_nested_scope_cannot_buy_a_second_allowance(self):
        # The defect: the ceiling was judged against the innermost open
        # scope, so every nested scope started at zero and N levels of
        # nesting admitted N times the declared cap.
        budget = Budget(10.0, step_ceiling_usd=0.05)
        with budget.step_scope("outer"):
            budget.reserve(0.03, label="outer-rung").settle(0.03)
            with budget.step_scope("inner"):
                with self.assertRaises(BudgetRefused) as ctx:
                    budget.reserve(0.03, label="inner-rung")
        self.assertEqual(ctx.exception.ceiling, 0.05)
        self.assertIn("outer", str(ctx.exception))
        self.assertAlmostEqual(budget.spent, 0.03)

    def test_a_nested_scope_still_admits_what_the_step_can_afford(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        with budget.step_scope("outer"):
            budget.reserve(0.02, label="outer-rung").settle(0.02)
            with budget.step_scope("inner"):
                budget.reserve(0.03, label="inner-rung").settle(0.03)
        self.assertAlmostEqual(budget.spent, 0.05)

    def test_outside_a_scope_the_call_is_the_step(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        budget.reserve(0.05).settle(0.05)
        budget.reserve(0.05).settle(0.05)
        with self.assertRaises(BudgetRefused):
            budget.reserve(0.051)

    def test_the_run_ceiling_still_bites_inside_a_scope(self):
        # A step ceiling wide enough not to be the binding one, so what the
        # refusal reports is the run ceiling the scope must not weaken.
        budget = Budget(0.06, step_ceiling_usd=0.20)
        with budget.step_scope("s1"):
            budget.reserve(0.04).settle(0.04)
            with self.assertRaises(BudgetRefused) as ctx:
                budget.reserve(0.04)
        self.assertAlmostEqual(ctx.exception.ceiling, 0.02)
        self.assertEqual(budget.spent, 0.04)

    def test_a_scope_leaves_nothing_behind_after_an_exception(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        with self.assertRaises(RuntimeError):
            with budget.step_scope("s1"):
                budget.reserve(0.04, label="held")
                raise RuntimeError("step blew up")
        # The next step is judged on its own exposure, not the failed one's.
        with budget.step_scope("s2"):
            budget.reserve(0.04).settle(0.04)
        self.assertAlmostEqual(budget.spent, 0.04)

    def test_a_scope_is_thread_local(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        entered, release = threading.Event(), threading.Event()
        seen = {}

        def worker():
            with budget.step_scope("slow"):
                budget.reserve(0.05).settle(0.05)
                entered.set()
                release.wait(5)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(5))
        # This thread is in no scope, so it is not charged for the other one.
        seen["ok"] = budget.reserve(0.05).settle(0.05)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertAlmostEqual(budget.spent, 0.10)
        self.assertAlmostEqual(seen["ok"], 0.05)


if __name__ == "__main__":
    unittest.main()
