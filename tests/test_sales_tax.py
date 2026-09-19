"""Sales tax collected and paid, and everything it refuses to total.

Numbers filed with the CRA under a client's name are the last place in this
system where a plausible answer is acceptable. Almost every test here is about
a period the module declines to total.
"""
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import sales_tax, work_engine  # noqa: E402
from shimline.sales_tax import TaxPeriodBlocked  # noqa: E402

ORG = "org_1"
_TEMP_DIRS = []
_CONNECTIONS = []


def _conn():
    """A real schema, built the way the application builds it.

    Hand-rolling the two tables under test would prove they work against a
    schema nobody runs. The migration itself is part of what is being checked.
    """
    temp = tempfile.TemporaryDirectory()
    _TEMP_DIRS.append(temp)
    service.DB_PATH = Path(temp.name) / "tax.db"
    conn = service._db()
    conn.execute("INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                 (ORG, "Client", ORG))
    conn.execute("INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
                 "period_end) VALUES('run_1',?,'2026-08-01','2026-08-31')", (ORG,))
    conn.commit()
    _CONNECTIONS.append(conn)
    return conn


def tearDownModule():
    # Windows will not delete an open SQLite file, so the handles go first.
    for conn in _CONNECTIONS:
        conn.close()
    for temp in _TEMP_DIRS:
        temp.cleanup()


def add(conn, txn_id, kind, date, *, tax_total=None, rates=(), doc="D1"):
    conn.execute(
        "INSERT INTO bookkeeping_transactions(id,run_id,organization_id,provider,"
        "provider_type,provider_id,transaction_date,document_number,currency,"
        "total_amount,status,source_hash,tax_total) "
        "VALUES(?,'run_1',?,'qbo',?,?,?,?,'CAD','0','posted','h',?)",
        (txn_id, ORG, kind, txn_id, date, doc,
         None if tax_total is None else str(tax_total)))
    for index, (ref, percent, base, amount) in enumerate(rates):
        conn.execute(
            "INSERT INTO bookkeeping_transaction_taxes(id,transaction_id,"
            "tax_rate_ref,rate_percent,net_amount_taxable,tax_amount) "
            "VALUES(?,?,?,?,?,?)",
            (f"txt_{txn_id}_{index}", txn_id, ref, percent, base, amount))


def totals(conn):
    return sales_tax.period(conn, ORG, period_start="2026-08-01",
                            period_end="2026-08-31")


class Totalling(unittest.TestCase):

    def test_tax_on_sales_is_collected_and_tax_on_purchases_is_paid(self):
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="130.00",
            rates=[("HST13", "13", "1000.00", "130.00")])
        add(conn, "t2", "Bill", "2026-08-06", tax_total="26.00",
            rates=[("HST13", "13", "200.00", "26.00")])
        result = totals(conn)
        self.assertTrue(result.usable, result.blocked)
        self.assertEqual(result.total_collected, Decimal("130.00"))
        self.assertEqual(result.total_paid, Decimal("26.00"))
        self.assertEqual(result.net(), Decimal("104.00"))

    def test_the_taxable_base_is_carried_not_just_the_tax(self):
        """The CRA asks for both, and one without the other cannot be checked."""
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="130.00",
            rates=[("HST13", "13", "1000.00", "130.00")])
        rate = totals(conn).collected[0]
        self.assertEqual(rate.taxable_base, Decimal("1000.00"))
        self.assertEqual(rate.rate_percent, "13")

    def test_two_rates_on_one_document_stay_separate(self):
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="12.00",
            rates=[("GST5", "5", "100.00", "5.00"),
                   ("PST7", "7", "100.00", "7.00")])
        collected = {item.tax_rate_ref: item for item in totals(conn).collected}
        self.assertEqual(collected["GST5"].tax_amount, Decimal("5.00"))
        self.assertEqual(collected["PST7"].tax_amount, Decimal("7.00"))

    def test_a_credit_memo_reduces_tax_collected(self):
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="130.00",
            rates=[("HST13", "13", "1000.00", "130.00")])
        add(conn, "t2", "CreditMemo", "2026-08-20", tax_total="13.00",
            rates=[("HST13", "13", "100.00", "13.00")])
        result = totals(conn)
        self.assertEqual(result.total_collected, Decimal("117.00"))
        self.assertEqual(result.collected[0].taxable_base, Decimal("900.00"))

    def test_a_vendor_credit_reduces_tax_paid(self):
        conn = _conn()
        add(conn, "t1", "Bill", "2026-08-06", tax_total="26.00",
            rates=[("HST13", "13", "200.00", "26.00")])
        add(conn, "t2", "VendorCredit", "2026-08-20", tax_total="13.00",
            rates=[("HST13", "13", "100.00", "13.00")])
        self.assertEqual(totals(conn).total_paid, Decimal("13.00"))

    def test_a_document_with_no_tax_is_not_a_problem(self):
        """A zero-rated sale or an exempt supply is a fact, not a gap."""
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04")
        result = totals(conn)
        self.assertTrue(result.usable, result.blocked)
        self.assertEqual(result.net(), Decimal("0.00"))

    def test_a_payment_settling_a_taxed_invoice_is_not_counted_twice(self):
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="130.00",
            rates=[("HST13", "13", "1000.00", "130.00")])
        add(conn, "t2", "Payment", "2026-08-20")
        self.assertEqual(totals(conn).total_collected, Decimal("130.00"))

    def test_documents_outside_the_period_are_not_counted(self):
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-07-31", tax_total="130.00",
            rates=[("HST13", "13", "1000.00", "130.00")])
        add(conn, "t2", "Invoice", "2026-09-01", tax_total="130.00",
            rates=[("HST13", "13", "1000.00", "130.00")])
        self.assertEqual(totals(conn).total_collected, Decimal("0.00"))


class Refusals(unittest.TestCase):

    def test_tax_with_no_rate_breakdown_blocks_the_period(self):
        """It cannot be attributed to a line of the return, and a return with an
        unattributed amount is not a return."""
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="130.00", doc="INV-9")
        result = totals(conn)
        self.assertFalse(result.usable)
        self.assertIn("INV-9", result.blocked[0])
        self.assertIn("no rate breakdown", result.blocked[0])

    def test_rates_that_do_not_add_up_block_the_period(self):
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="130.00", doc="INV-9",
            rates=[("HST13", "13", "1000.00", "129.99")])
        result = totals(conn)
        self.assertFalse(result.usable)
        self.assertIn("add up to", result.blocked[0])

    def test_a_blocked_period_refuses_to_produce_a_net_figure(self):
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="130.00")
        with self.assertRaises(TaxPeriodBlocked):
            totals(conn).net()

    def test_one_bad_document_blocks_the_whole_period(self):
        """Reporting the good half would be a smaller number that looks right."""
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="130.00",
            rates=[("HST13", "13", "1000.00", "130.00")])
        add(conn, "t2", "Invoice", "2026-08-09", tax_total="26.00", doc="INV-BAD")
        self.assertFalse(totals(conn).usable)

    def test_a_settlement_document_carrying_tax_blocks_rather_than_being_dropped(self):
        conn = _conn()
        add(conn, "t1", "BillPayment", "2026-08-20", tax_total="13.00", doc="BP-1",
            rates=[("HST13", "13", "100.00", "13.00")])
        result = totals(conn)
        self.assertFalse(result.usable)
        self.assertIn("should not", result.blocked[0])

    def test_an_unclassified_type_carrying_tax_blocks_the_period(self):
        conn = _conn()
        add(conn, "t1", "Estimate", "2026-08-04", tax_total="13.00", doc="EST-1",
            rates=[("HST13", "13", "100.00", "13.00")])
        result = totals(conn)
        self.assertFalse(result.usable)
        self.assertIn("which side of the return", result.blocked[0])

    def test_a_rate_that_changed_mid_period_reports_no_percentage(self):
        """Reporting either percentage would be wrong for half the documents."""
        conn = _conn()
        add(conn, "t1", "Invoice", "2026-08-04", tax_total="5.00",
            rates=[("GST", "5", "100.00", "5.00")])
        add(conn, "t2", "Invoice", "2026-08-24", tax_total="6.00",
            rates=[("GST", "6", "100.00", "6.00")])
        rate = totals(conn).collected[0]
        self.assertIsNone(rate.rate_percent)
        self.assertEqual(rate.tax_amount, Decimal("11.00"))


class Persistence(unittest.TestCase):
    """The engine must write what QuickBooks stated, and nothing else."""

    def _persist(self, objects):
        conn = _conn()
        work_engine.persist_canonical(conn, "run_1", ORG, objects)
        return conn

    def _objects(self, invoice):
        return {"Account": [{"Id": "79", "Name": "Revenue", "AccountType": "Income"}],
                "Invoice": [invoice]}

    def test_the_document_total_and_the_rate_rows_are_stored(self):
        conn = self._persist(self._objects({
            "Id": "1001", "TxnDate": "2026-08-04", "TotalAmt": 1130.00,
            "DocNumber": "INV-1", "SyncToken": "0",
            "Line": [{"Amount": 1000.00, "DetailType": "SalesItemLineDetail",
                      "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}],
            "TxnTaxDetail": {"TotalTax": 130.00, "TaxLine": [
                {"Amount": 130.00, "DetailType": "TaxLineDetail",
                 "TaxLineDetail": {"TaxRateRef": {"value": "HST13"},
                                   "TaxPercent": 13, "NetAmountTaxable": 1000.00}}]}}))
        row = conn.execute(
            "SELECT tax_total FROM bookkeeping_transactions").fetchone()
        self.assertEqual(row[0], "130.00")
        tax = conn.execute(
            "SELECT tax_rate_ref,rate_percent,net_amount_taxable,tax_amount "
            "FROM bookkeeping_transaction_taxes").fetchone()
        self.assertEqual(tuple(tax), ("HST13", "13", "1000.00", "130.00"))
        self.assertEqual(totals(conn).total_collected, Decimal("130.00"))

    def test_a_line_tax_the_provider_did_not_state_is_marked_unknown(self):
        """It used to be written as 0.00 with nothing to say it was a guess.
        A return built on that would have come out at nil and looked right."""
        conn = self._persist(self._objects({
            "Id": "1001", "TxnDate": "2026-08-04", "TotalAmt": 1130.00,
            "SyncToken": "0",
            "Line": [{"Amount": 1000.00, "DetailType": "SalesItemLineDetail",
                      "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}],
            "TxnTaxDetail": {"TotalTax": 130.00}}))
        row = conn.execute("SELECT tax_amount,tax_amount_source "
                           "FROM bookkeeping_transaction_lines").fetchone()
        self.assertEqual(row[1], "unknown")

    def test_a_line_tax_the_provider_did_state_is_marked_provider(self):
        conn = self._persist(self._objects({
            "Id": "1001", "TxnDate": "2026-08-04", "TotalAmt": 1130.00,
            "SyncToken": "0",
            "Line": [{"Amount": 1000.00, "DetailType": "SalesItemLineDetail",
                      "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"},
                                              "TaxAmount": 130.00}}]}))
        row = conn.execute("SELECT tax_amount,tax_amount_source "
                           "FROM bookkeeping_transaction_lines").fetchone()
        self.assertEqual(tuple(row), ("130.00", "provider"))

    def test_a_document_with_no_tax_detail_writes_no_tax_rows(self):
        conn = self._persist(self._objects({
            "Id": "1001", "TxnDate": "2026-08-04", "TotalAmt": 1000.00,
            "SyncToken": "0",
            "Line": [{"Amount": 1000.00, "DetailType": "SalesItemLineDetail",
                      "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}]}))
        self.assertIsNone(conn.execute(
            "SELECT tax_total FROM bookkeeping_transactions").fetchone()[0])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM bookkeeping_transaction_taxes").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
