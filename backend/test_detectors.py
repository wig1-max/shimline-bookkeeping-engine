"""Detector tests for checks moved from `manual` to `automated`.

The 50-company acceptance suite proves a detector fires. These tests prove it
does not fire on the cases next to it, which is the harder half: a check that
flags every unpaid bill is worse than no check, because a reviewer learns to
ignore it.
"""
import unittest
from datetime import date, timedelta
from decimal import Decimal

from shimline import work_engine
from shimline.synthetic_books import generate_company

TODAY = date(2026, 9, 8)


def _bill(bill_id, *, balance, due=None, txn=None, doc=None):
    bill = {"Id": bill_id, "TotalAmt": float(balance), "Balance": float(balance),
            "VendorRef": {"value": "V1"}, "SyncToken": "0",
            "DocNumber": doc or f"DOC-{bill_id}"}
    if txn:
        bill["TxnDate"] = txn.isoformat()
    if due:
        bill["DueDate"] = due.isoformat()
    return bill


def _ledger(bills):
    return {"Account": [{"Id": "200", "Name": "A/P", "AccountType": "Accounts Payable"}],
            "Invoice": [], "Payment": [], "Purchase": [], "Deposit": [],
            "JournalEntry": [], "Bill": bills}


def _findings(bills):
    analysis = work_engine.analyze(
        _ledger(bills), {"period_start": "2026-01-01", "period_end": "2026-09-08"},
        today=TODAY)
    return [f for f in analysis.findings if f.defect_type == "stale_payable"]


class StalePayableTests(unittest.TestCase):
    def test_a_bill_past_its_due_date_with_a_balance_is_flagged(self):
        found = _findings([_bill("B1", balance=480,
                                 due=TODAY - timedelta(days=65),
                                 txn=TODAY - timedelta(days=95))])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].affected_id, "B1")
        self.assertEqual(found[0].affected_type, "Bill")
        self.assertEqual(found[0].financial_effect, Decimal("480.00"))
        self.assertIn("65 days ago", found[0].reason)

    def test_a_bill_not_yet_due_is_not_flagged(self):
        # Owed is not overdue. This is the case that makes the check usable.
        self.assertEqual(_findings([_bill("B2", balance=220,
                                          due=TODAY + timedelta(days=20),
                                          txn=TODAY - timedelta(days=10))]), [])

    def test_a_bill_due_today_is_not_yet_late(self):
        self.assertEqual(_findings([_bill("B3", balance=100, due=TODAY)]), [])

    def test_a_paid_bill_is_not_flagged_however_old(self):
        self.assertEqual(_findings([_bill("B4", balance=0,
                                          due=TODAY - timedelta(days=400))]), [])

    def test_a_bill_without_a_due_date_falls_back_to_the_default_term(self):
        term = work_engine.DEFAULT_PAYMENT_TERM_DAYS
        # Raised inside the term: not late.
        self.assertEqual(
            _findings([_bill("B5", balance=50, txn=TODAY - timedelta(days=term - 5))]), [])
        # Raised well outside it: late.
        self.assertEqual(
            len(_findings([_bill("B6", balance=50, txn=TODAY - timedelta(days=term + 10))])), 1)

    def test_an_unreadable_date_is_skipped_rather_than_crashing_the_run(self):
        bill = _bill("B7", balance=90)
        bill["DueDate"] = "not-a-date"
        self.assertEqual(_findings([bill]), [])

    def test_the_finding_never_carries_a_proposal(self):
        # Paying a vendor moves money. It is outside the mutation catalogue and
        # is the client's decision, so this check reports and stops.
        found = _findings([_bill("B8", balance=480, due=TODAY - timedelta(days=65))])
        self.assertIsNone(found[0].proposal)
        self.assertEqual(found[0].evidence_status, "sufficient")

    def test_check_13_reports_automated_coverage_now(self):
        analysis = work_engine.analyze(
            _ledger([]), {"period_start": "2026-01-01", "period_end": "2026-09-08"},
            today=TODAY)
        entry = next(c for c in analysis.coverage["checks"] if c["id"] == "13")
        self.assertEqual(entry["mode"], "automated")
        self.assertEqual(entry["status"], "clean")

    def test_the_synthetic_oracle_seeds_both_an_overdue_and_a_current_bill(self):
        # Guards the negative cases inside the 50-company suite: if the generator
        # only ever seeded an overdue bill, a detector that flagged every bill
        # would pass the acceptance run.
        #
        # There are two negatives, and they fail differently. B-CURRENT is unpaid
        # but not yet due, so a detector that ignores the due date flags it.
        # B-PAID is past its due date and settled, so a detector that reads the
        # due date and not the balance flags that one. Owing money late and
        # having owed money are not the same finding.
        company = generate_company(1)
        ids = {bill["Id"] for bill in company.objects["Bill"]}
        self.assertEqual(ids, {"B-STALE", "B-CURRENT", "B-PAID"})
        flagged = {f.affected_id for f in work_engine.analyze(
            company.objects, company.evidence).findings
            if f.defect_type == "stale_payable"}
        self.assertEqual(flagged, {"B-STALE"})


def _priced(txn_id, item, unit, qty, day, vendor="V1"):
    return {"Id": txn_id, "TxnDate": day, "EntityRef": {"value": vendor},
            "TotalAmt": float(Decimal(str(unit)) * qty), "AccountRef": {"value": "100"},
            "Line": [{"Amount": float(Decimal(str(unit)) * qty),
                      "ItemBasedExpenseLineDetail": {
                          "ItemRef": {"value": item}, "UnitPrice": float(unit),
                          "Qty": qty, "AccountRef": {"value": "500"}}}]}


def _price_findings(purchases):
    analysis = work_engine.analyze(
        {"Account": [{"Id": "500"}, {"Id": "100"}], "Purchase": purchases,
         "Invoice": [], "Payment": [], "Bill": [], "Deposit": [], "JournalEntry": []},
        {"period_start": "2026-01-01", "period_end": "2026-09-08"}, today=TODAY)
    return [f for f in analysis.findings if f.defect_type == "abnormal_price"]


class AbnormalPriceTests(unittest.TestCase):
    def test_a_price_spike_above_an_established_baseline_is_flagged(self):
        found = _price_findings([
            _priced("A", "lumber", "10.00", 20, "2026-06-01"),
            _priced("B", "lumber", "10.00", 20, "2026-06-15"),
            _priced("C", "lumber", "10.50", 20, "2026-07-01"),
            _priced("D", "lumber", "31.00", 20, "2026-08-01"),
        ])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].affected_id, "D")
        self.assertIn("median unit price", found[0].reason)

    def test_a_first_observation_is_not_a_spike(self):
        # Nothing to be abnormal relative to. This is the failure mode that
        # would make the check fire on every new item a client ever buys.
        self.assertEqual(_price_findings([_priced("A", "lumber", "99.00", 1, "2026-08-01")]), [])

    def test_a_short_history_does_not_support_the_claim(self):
        # Fewer prior observations than PRICE_HISTORY_MINIMUM is not a baseline,
        # however far the latest price sits from them.
        for prior in range(1, work_engine.PRICE_HISTORY_MINIMUM):
            purchases = [_priced(f"H{i}", "lumber", "10.00", 1, f"2026-06-{i+1:02d}")
                         for i in range(prior)]
            purchases.append(_priced("SPIKE", "lumber", "99.00", 1, "2026-08-01"))
            self.assertEqual(_price_findings(purchases), [], f"{prior} prior purchase(s)")

    def test_exactly_the_minimum_history_is_enough_to_fire(self):
        # Pins the boundary from the other side, so the threshold cannot drift
        # without a test noticing.
        prior = work_engine.PRICE_HISTORY_MINIMUM
        purchases = [_priced(f"H{i}", "lumber", "10.00", 1, f"2026-06-{i+1:02d}")
                     for i in range(prior)]
        purchases.append(_priced("SPIKE", "lumber", "99.00", 1, "2026-08-01"))
        found = _price_findings(purchases)
        self.assertEqual(len(found), 1)
        self.assertIn(f"across {prior} prior purchases", found[0].reason)

    def test_an_ordinary_price_rise_is_not_flagged(self):
        # Prices move. Only a move far outside the item's own history counts.
        self.assertEqual(_price_findings([
            _priced("A", "lumber", "10.00", 20, "2026-06-01"),
            _priced("B", "lumber", "10.50", 20, "2026-06-15"),
            _priced("C", "lumber", "11.00", 20, "2026-07-01"),
            _priced("D", "lumber", "12.00", 20, "2026-08-01"),
        ]), [])

    def test_history_is_kept_per_vendor_and_per_item(self):
        # A different vendor's price is not a baseline for this one, and two
        # items are not one series.
        self.assertEqual(_price_findings([
            _priced("A", "lumber", "10.00", 1, "2026-06-01"),
            _priced("B", "lumber", "10.00", 1, "2026-06-15"),
            _priced("C", "lumber", "10.00", 1, "2026-07-01"),
            _priced("D", "lumber", "31.00", 1, "2026-08-01", vendor="V2"),
            _priced("E", "screws", "31.00", 1, "2026-08-02"),
        ]), [])

    def test_account_based_lines_carry_no_price_and_are_simply_absent(self):
        self.assertEqual(_price_findings([
            {"Id": "X", "TxnDate": "2026-08-01", "EntityRef": {"value": "V1"},
             "TotalAmt": 500.0, "AccountRef": {"value": "100"},
             "Line": [{"Amount": 500.0,
                       "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "500"}}}]}]), [])

    def test_the_finding_never_carries_a_proposal(self):
        found = _price_findings([
            _priced("A", "lumber", "10.00", 1, "2026-06-01"),
            _priced("B", "lumber", "10.00", 1, "2026-06-15"),
            _priced("C", "lumber", "10.00", 1, "2026-07-01"),
            _priced("D", "lumber", "40.00", 1, "2026-08-01"),
        ])
        self.assertIsNone(found[0].proposal)


def _job_findings(revenue, cost):
    """revenue/cost: {job_id: amount}"""
    invoices = [{"Id": f"INV-{job}", "TxnDate": "2026-07-01", "TotalAmt": float(amount),
                 "Line": [{"Amount": float(amount),
                           "SalesItemLineDetail": {"ItemAccountRef": {"value": "400"},
                                                   "CustomerRef": {"value": job}}}]}
                for job, amount in revenue.items()]
    purchases = [{"Id": f"EXP-{job}", "TxnDate": "2026-07-05", "TotalAmt": float(amount),
                  "AccountRef": {"value": "100"},
                  "Line": [{"Amount": float(amount),
                            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "500"},
                                                              "CustomerRef": {"value": job}}}]}
                 for job, amount in cost.items()]
    # The jobs are in the pull. A job absent from the customer list is a
    # separate finding -- an archived job keeps its costs and stops coming back
    # from `SELECT * FROM Customer` -- and these cases are about the arithmetic.
    jobs = sorted(set(revenue) | set(cost))
    analysis = work_engine.analyze(
        {"Account": [{"Id": "400"}, {"Id": "500"}, {"Id": "100"}],
         "Customer": [{"Id": job, "DisplayName": job, "Job": True} for job in jobs],
         "Invoice": invoices, "Purchase": purchases,
         "Payment": [], "Bill": [], "Deposit": [], "JournalEntry": []},
        {"period_start": "2026-01-01", "period_end": "2026-09-08"}, today=TODAY)
    return {f.affected_id for f in analysis.findings
            if f.defect_type == "negative_job_margin"}


class NegativeJobMarginTests(unittest.TestCase):
    def test_a_job_that_cost_more_than_it_billed_is_flagged(self):
        self.assertEqual(_job_findings({"P2": 500}, {"P2": 800}), {"P2"})

    def test_a_profitable_job_is_not_flagged(self):
        # Selectivity. A detector that flags every job teaches reviewers to
        # ignore it, which is worse than not having it.
        self.assertEqual(_job_findings({"P1": 1000}, {"P1": 600}), set())

    def test_a_job_that_exactly_breaks_even_is_not_flagged(self):
        self.assertEqual(_job_findings({"P1": 500}, {"P1": 500}), set())

    def test_only_the_losing_job_is_flagged_among_several(self):
        self.assertEqual(
            _job_findings({"P1": 1000, "P2": 500, "P3": 900}, {"P1": 600, "P2": 800, "P3": 400}),
            {"P2"})

    def test_a_job_with_cost_and_no_tagged_revenue_is_not_called_a_loss(self):
        # Mid-job is the ordinary state of a contractor file. Treating absent
        # revenue as zero would report the job as having lost every dollar.
        self.assertEqual(_job_findings({}, {"P1": 800}), set())

    def test_the_finding_names_misallocation_as_the_other_explanation(self):
        analysis = work_engine.analyze(
            {"Account": [{"Id": "400"}, {"Id": "500"}, {"Id": "100"}],
             "Customer": [{"Id": "P2", "DisplayName": "Basement", "Job": True}],
             "Invoice": [{"Id": "I", "TxnDate": "2026-07-01", "TotalAmt": 500.0,
                          "Line": [{"Amount": 500.0,
                                    "SalesItemLineDetail": {"ItemAccountRef": {"value": "400"},
                                                            "CustomerRef": {"value": "P2"}}}]}],
             "Purchase": [{"Id": "E", "TxnDate": "2026-07-05", "TotalAmt": 800.0,
                           "AccountRef": {"value": "100"},
                           "Line": [{"Amount": 800.0,
                                     "AccountBasedExpenseLineDetail": {
                                         "AccountRef": {"value": "500"},
                                         "CustomerRef": {"value": "P2"}}}]}],
             "Payment": [], "Bill": [], "Deposit": [], "JournalEntry": []},
            {"period_start": "2026-01-01", "period_end": "2026-09-08"}, today=TODAY)
        finding = next(f for f in analysis.findings if f.defect_type == "negative_job_margin")
        self.assertIn("wrong job", finding.reason)
        self.assertIsNone(finding.proposal)


class CoverageTests(unittest.TestCase):
    def test_twelve_of_sixteen_published_checks_are_automated(self):
        published = [c for c in work_engine.CHECKS if c["id"].isdigit()]
        automated = [c["id"] for c in published if c["mode"] == "automated"]
        self.assertEqual(
            automated,
            ["01", "03", "04", "05", "06", "07", "08", "09", "12", "13", "15", "16"])


if __name__ == "__main__":
    unittest.main()
