"""A GST/HST return, and the many periods that do not produce one.

This return is signed by an accountant and filed with the CRA under a client's
name. A wrong figure here is the most expensive output this product can make,
and nothing downstream would ever reveal it. Almost every test in this file is
about refusing.
"""
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import filing_periods, gst_return, tax_rates  # noqa: E402
from shimline.gst_return import (LINE_101, LINE_105, LINE_108,  # noqa: E402
                                 LINE_109, NotFilable)

ORG = "org_1"


class ReturnCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "gst.db"
        self.conn = service._db()
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) "
                          "VALUES(?,?,?)", (ORG, "Northlake Roofing", ORG))
        self.conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
            "period_end,status) VALUES('run_1',?,'2026-07-01','2026-09-30',"
            "'review')", (ORG,))
        for ident, name, kind in (("acc_rev", "Contract Revenue", "Income"),
                                  ("acc_tax", "GST/HST Payable",
                                   "Other Current Liability"),
                                  ("acc_exp", "Materials", "Cost of Goods Sold")):
            self.conn.execute(
                "INSERT INTO bookkeeping_accounts(id,organization_id,provider,"
                "provider_id,name,account_type) VALUES(?,?,'quickbooks',?,?,?)",
                (ident, ORG, ident, name, kind))
        tax_rates.persist(self.conn, ORG, {
            "TaxAgency": [{"Id": "1", "DisplayName": "Canada Revenue Agency"},
                          {"Id": "2", "DisplayName": "Revenu Québec"}],
            "TaxRate": [
                {"Id": "GST5", "Name": "GST", "RateValue": "5",
                 "AgencyRef": {"value": "1"}},
                {"Id": "HST13", "Name": "HST ON", "RateValue": "13",
                 "AgencyRef": {"value": "1"}},
                {"Id": "QST", "Name": "QST", "RateValue": "9.975",
                 "AgencyRef": {"value": "2"}},
            ]})
        filing_periods.record_arrangement(
            self.conn, ORG, frequency=filing_periods.QUARTERLY,
            year_end_month=12, year_end_day=31,
            calculation_method=filing_periods.REGULAR_METHOD)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def document(self, ident, kind, date, *, tax_total=None, rates=(),
                 revenue=None, expense=None, tax_posting=None, doc=None):
        """One transaction, its stated tax, and the lines it posted."""
        self.conn.execute(
            "INSERT INTO bookkeeping_transactions(id,run_id,organization_id,"
            "provider,provider_type,provider_id,transaction_date,document_number,"
            "currency,total_amount,status,source_hash,tax_total) "
            "VALUES(?,'run_1',?,'quickbooks',?,?,?,?,'CAD','0','posted','h',?)",
            (ident, ORG, kind, ident, date, doc or ident,
             None if tax_total is None else str(tax_total)))
        for index, (ref, base, amount) in enumerate(rates):
            self.conn.execute(
                "INSERT INTO bookkeeping_transaction_taxes(id,transaction_id,"
                "tax_rate_ref,rate_percent,net_amount_taxable,tax_amount) "
                "VALUES(?,?,?,NULL,?,?)",
                (f"txt_{ident}_{index}", ident, ref, base, amount))
        lines = []
        if revenue is not None:
            lines.append(("acc_rev", "0", str(revenue)))
        if expense is not None:
            lines.append(("acc_exp", str(expense), "0"))
        if tax_posting is not None:
            debit, credit = (("0", str(tax_posting)) if Decimal(str(tax_posting)) >= 0
                             else (str(-Decimal(str(tax_posting))), "0"))
            lines.append(("acc_tax", debit, credit))
        for index, (account, debit, credit) in enumerate(lines):
            self.conn.execute(
                "INSERT INTO bookkeeping_transaction_lines(id,transaction_id,"
                "provider_line_id,account_id,amount,debit,credit) "
                "VALUES(?,?,?,?,?,?,?)",
                (f"lin_{ident}_{index}", ident, str(index), account,
                 debit if debit != "0" else credit, debit, credit))
        self.conn.commit()

    def prepare(self, start="2026-07-01", end="2026-09-30"):
        return gst_return.prepare(self.conn, ORG, period_start=start,
                                  period_end=end)

    def simple_quarter(self):
        """One taxed sale and one taxed purchase, posted consistently."""
        self.document("inv_1", "Invoice", "2026-07-15", tax_total="130.00",
                      rates=[("HST13", "1000.00", "130.00")],
                      revenue="1000.00", tax_posting="130.00")
        self.document("bill_1", "Bill", "2026-08-02", tax_total="26.00",
                      rates=[("HST13", "200.00", "26.00")],
                      expense="200.00", tax_posting="-26.00")


class TheFigures(ReturnCase):

    def test_a_clean_quarter_produces_four_lines(self):
        self.simple_quarter()
        result = self.prepare()
        self.assertTrue(result.filable, result.blocked)
        self.assertEqual(result.figure(LINE_101), Decimal("1000.00"))
        self.assertEqual(result.figure(LINE_105), Decimal("130.00"))
        self.assertEqual(result.figure(LINE_108), Decimal("26.00"))
        self.assertEqual(result.figure(LINE_109), Decimal("104.00"))

    def test_line_101_excludes_the_tax(self):
        """A document total includes the tax and line 101 does not. Reading
        totals instead of income postings would overstate revenue by the HST."""
        self.simple_quarter()
        self.assertEqual(self.prepare().figure(LINE_101), Decimal("1000.00"))

    def test_provincial_tax_is_excluded_and_shown_as_excluded(self):
        """Claiming QST as an input tax credit overstates the credit on a CRA
        filing, and the return would look entirely reasonable doing it."""
        self.document("bill_q", "Bill", "2026-07-20", tax_total="99.75",
                      rates=[("QST", "1000.00", "99.75")],
                      expense="1000.00", tax_posting="-99.75")
        result = self.prepare()
        self.assertTrue(result.filable, result.blocked)
        self.assertEqual(result.figure(LINE_108), Decimal("0.00"))
        self.assertEqual([item.label for item in result.excluded_provincial],
                         ["QST 9.975%"])

    def test_a_credit_memo_reduces_the_tax_collected(self):
        self.simple_quarter()
        self.document("cm_1", "CreditMemo", "2026-08-10", tax_total="13.00",
                      rates=[("HST13", "100.00", "13.00")],
                      revenue="-100.00", tax_posting="-13.00")
        result = self.prepare()
        self.assertTrue(result.filable, result.blocked)
        self.assertEqual(result.figure(LINE_105), Decimal("117.00"))
        self.assertEqual(result.figure(LINE_101), Decimal("900.00"))

    def test_two_federal_rates_are_added_together_and_listed_apart(self):
        self.document("inv_a", "Invoice", "2026-07-05", tax_total="130.00",
                      rates=[("HST13", "1000.00", "130.00")],
                      revenue="1000.00", tax_posting="130.00")
        self.document("inv_b", "Invoice", "2026-07-06", tax_total="25.00",
                      rates=[("GST5", "500.00", "25.00")],
                      revenue="500.00", tax_posting="25.00")
        result = self.prepare()
        self.assertEqual(result.figure(LINE_105), Decimal("155.00"))
        self.assertEqual(sorted(item.label for item in result.collected_rates),
                         ["GST 5%", "HST ON 13%"])

    def test_a_quarter_with_no_activity_is_a_nil_return_not_a_refusal(self):
        """A contractor between jobs files a nil return. Blocking would send an
        accountant looking for data that correctly does not exist."""
        result = self.prepare()
        self.assertTrue(result.filable, result.blocked)
        self.assertEqual(result.figure(LINE_109), Decimal("0.00"))

    def test_documents_outside_the_period_are_not_on_the_return(self):
        self.simple_quarter()
        self.document("inv_late", "Invoice", "2026-10-01", tax_total="130.00",
                      rates=[("HST13", "1000.00", "130.00")],
                      revenue="1000.00", tax_posting="130.00")
        self.assertEqual(self.prepare().figure(LINE_105), Decimal("130.00"))

    def test_the_taxable_base_rides_along_with_each_rate(self):
        """The CRA asks for both, and one without the other cannot be checked."""
        self.simple_quarter()
        line = self.prepare().collected_rates[0]
        self.assertEqual(line.taxable_base, Decimal("1000.00"))


class TheRefusals(ReturnCase):

    def test_an_unclassified_rate_blocks_the_whole_return(self):
        """Assuming it federal overstates the credit; assuming it provincial
        understates the tax collected. Both are wrong and both are hidden."""
        tax_rates.persist(self.conn, ORG, {
            "TaxRate": [{"Id": "MYST", "Name": "Standard", "RateValue": "7"}]})
        self.conn.commit()
        self.document("inv_m", "Invoice", "2026-07-15", tax_total="70.00",
                      rates=[("MYST", "1000.00", "70.00")],
                      revenue="1000.00", tax_posting="70.00")
        result = self.prepare()
        self.assertFalse(result.filable)
        self.assertIn("Standard 7%", result.blocked[0])
        self.assertIn("not classified", result.blocked[0])

    def test_a_decision_unblocks_the_return(self):
        tax_rates.persist(self.conn, ORG, {
            "TaxRate": [{"Id": "MYST", "Name": "Standard", "RateValue": "7"}]})
        self.document("inv_m", "Invoice", "2026-07-15", tax_total="70.00",
                      rates=[("MYST", "1000.00", "70.00")],
                      revenue="1000.00", tax_posting="70.00")
        self.assertFalse(self.prepare().filable)
        tax_rates.decide(self.conn, ORG, "MYST", tax_rates.GST_HST,
                         reason="Confirmed federal with the client")
        self.conn.commit()
        result = self.prepare()
        self.assertTrue(result.filable, result.blocked)
        self.assertEqual(result.figure(LINE_105), Decimal("70.00"))

    def test_an_unclassified_rate_nobody_charged_does_not_block(self):
        """A client may hold rates they do not use. Blocking on one would make
        every return conditional on tidying a list that has no bearing on it,
        and an accountant who learns the blocks are noise stops reading them."""
        tax_rates.persist(self.conn, ORG, {
            "TaxRate": [{"Id": "UNUSED", "Name": "Standard", "RateValue": "7"}]})
        self.conn.commit()
        self.simple_quarter()
        result = self.prepare()
        self.assertTrue(result.filable, result.blocked)

    def test_the_period_that_most_recently_closed_is_the_one_prepared(self):
        """An accountant does not want the period they are standing in; it is
        not finished. They want the one that just ended and is now due."""
        from datetime import date as _date

        filing_periods.record_arrangement(
            self.conn, ORG, frequency=filing_periods.QUARTERLY,
            year_end_month=12, year_end_day=31,
            calculation_method=filing_periods.REGULAR_METHOD)
        self.simple_quarter()
        self.conn.commit()
        prepared, problem = gst_return.prepare_current(
            self.conn, ORG, today=_date(2026, 11, 15))
        self.assertEqual(problem, "")
        self.assertEqual((prepared.period_start, prepared.period_end),
                         ("2026-07-01", "2026-09-30"))

    def test_a_client_with_no_filing_arrangement_gets_a_reason_not_a_crash(self):
        """An ordinary state for a new engagement, not an error."""
        self.conn.execute(
            "DELETE FROM bookkeeping_gst_filing WHERE organization_id=?", (ORG,))
        prepared, problem = gst_return.prepare_current(self.conn, ORG)
        self.assertIsNone(prepared)
        self.assertIn("have not been recorded", problem)

    def test_an_unrecorded_calculation_method_blocks_the_whole_return(self):
        self.conn.execute(
            "UPDATE bookkeeping_gst_filing SET calculation_method=NULL "
            "WHERE organization_id=?", (ORG,))
        self.simple_quarter()
        result = self.prepare()
        self.assertFalse(result.filable)
        self.assertTrue(any("calculation method has not been recorded" in reason
                            for reason in result.blocked))

    def test_a_quick_method_client_is_refused_not_given_regular_figures(self):
        self.conn.execute(
            "UPDATE bookkeeping_gst_filing SET calculation_method='quick' "
            "WHERE organization_id=?", (ORG,))
        self.simple_quarter()
        result = self.prepare()
        self.assertFalse(result.filable)
        self.assertTrue(any("uses the GST/HST Quick Method" in reason
                            for reason in result.blocked))
        with self.assertRaises(NotFilable):
            result.figure(LINE_109)

    def test_a_rate_charged_but_absent_from_the_rate_list_blocks(self):
        self.document("inv_g", "Invoice", "2026-07-15", tax_total="70.00",
                      rates=[("GHOST", "1000.00", "70.00")],
                      revenue="1000.00", tax_posting="70.00")
        result = self.prepare()
        self.assertFalse(result.filable)
        self.assertIn("GHOST", result.blocked[0])
        self.assertIn("Re-sync", result.blocked[0])

    def test_one_document_with_no_rate_breakdown_blocks_the_period(self):
        self.simple_quarter()
        self.document("inv_bad", "Invoice", "2026-08-20", tax_total="65.00",
                      revenue="500.00", tax_posting="65.00", doc="INV-BAD")
        result = self.prepare()
        self.assertFalse(result.filable)
        self.assertTrue(any("INV-BAD" in item for item in result.blocked))

    def test_a_blocked_return_refuses_to_hand_over_a_figure(self):
        """Not a partial return, not an estimate, not a figure with a caveat
        nobody reads."""
        self.document("inv_bad", "Invoice", "2026-08-20", tax_total="65.00",
                      revenue="500.00", tax_posting="65.00")
        result = self.prepare()
        with self.assertRaises(NotFilable):
            result.figure(LINE_109)


class TheTwoReadings(ReturnCase):

    def test_agreement_is_reported_with_what_it_covers(self):
        self.simple_quarter()
        agreement = self.prepare().agreement
        self.assertEqual(agreement["status"], "agrees")
        self.assertIn("totals only", agreement["checked"])

    def test_a_ledger_that_posts_different_tax_blocks_the_return(self):
        """QuickBooks' tax detail and the posted ledger are two readings of the
        same period. If they disagree one is wrong, and nothing here can say
        which, so no figure is produced."""
        self.simple_quarter()
        self.conn.execute(
            "UPDATE bookkeeping_transaction_lines SET credit='999.00' "
            "WHERE transaction_id='inv_1' AND account_id='acc_tax'")
        self.conn.commit()
        result = self.prepare()
        self.assertFalse(result.filable)
        self.assertEqual(result.agreement["status"], "disagrees")
        self.assertTrue(any("do not agree" in item for item in result.blocked))

    def test_stated_tax_with_nothing_posted_is_a_disagreement(self):
        """Not an absence. QuickBooks says tax was charged and the ledger has
        it nowhere, which is a defect in one of the two."""
        self.document("inv_1", "Invoice", "2026-07-15", tax_total="130.00",
                      rates=[("HST13", "1000.00", "130.00")],
                      revenue="1000.00")
        result = self.prepare()
        self.assertFalse(result.filable)
        self.assertIn("posts none", result.agreement["detail"])

    def test_a_period_with_no_tax_on_either_side_agrees(self):
        self.document("inv_zero", "Invoice", "2026-07-15", revenue="1000.00")
        result = self.prepare()
        self.assertTrue(result.filable, result.blocked)
        self.assertEqual(result.agreement["status"], "agrees")

    def test_an_untotalable_period_is_not_reported_as_a_disagreement(self):
        """"We could not read it" and "the two readings differ" are different
        answers and must not be confused."""
        self.document("inv_bad", "Invoice", "2026-08-20", tax_total="65.00",
                      revenue="500.00", tax_posting="65.00")
        self.assertEqual(self.prepare().agreement["status"], "not_compared")

    def test_the_summary_says_whether_it_is_filable(self):
        self.simple_quarter()
        summary = self.prepare().summary()
        self.assertTrue(summary["filable"])
        self.assertEqual(summary["lines"][LINE_109], "104.00")
        self.assertEqual(summary["blocked"], [])


if __name__ == "__main__":
    unittest.main()
