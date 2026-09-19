import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY", "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import crm, qbo_exports, reporting, work_engine  # noqa: E402


LEDGER = b'''Transaction Detail by Account,,,,,,\nJanuary 1 - March 31 2026,,,,,,\nDate,Transaction Type,Num,Name,Memo/Description,Account,Customer/Project,Amount\n01/05/2026,Invoice,1001,Smith,Progress billing,Construction Income,Smith:Kitchen,20000.00\n01/06/2026,Bill,B-91,Lumber Co,Framing lumber,Job Materials,Smith:Kitchen,12000.00\n01/07/2026,Expense,E-17,Tool Hire,Excavator,Job Materials,,1000.00\n02/01/2026,Expense,DUP-1,Tool Hire,Compressor,Job Materials,Smith:Kitchen,500.00\n02/03/2026,Expense,DUP-2,Tool Hire,Compressor,Job Materials,Smith:Kitchen,500.00\n'''

AR = b'''Accounts Receivable Aging Detail,,,,\nAs of March 31 2026,,,,\nCustomer,Transaction Type,Date,Num,Due Date,Open Balance\nSmith,Invoice,01/05/2026,1001,02/04/2026,7500.00\n'''


class ExportParserTests(unittest.TestCase):
    def test_parses_realistic_prefaced_exports_without_fabricating_rows(self):
        package = qbo_exports.parse_exports(LEDGER, AR)
        self.assertEqual(package.rows_imported, {"transaction_detail": 5, "ar_aging": 1})
        self.assertEqual(len(package.objects["Invoice"]), 1)
        self.assertEqual(package.objects["Invoice"][0]["Balance"], "7500.00")
        self.assertEqual(len(package.objects["Purchase"]), 3)
        self.assertIn("qbo_ar_aging_detail_csv", package.evidence["source_types"])
        self.assertEqual(package.evidence["period_end"], "2026-03-31")

    def test_the_ar_due_date_is_kept_on_the_invoice_but_does_not_age_it(self):
        package = qbo_exports.parse_exports(LEDGER, AR)
        invoice = package.objects["Invoice"][0]
        # The column is published by the export, so the canonical object keeps
        # it instead of parsing and discarding it.
        self.assertEqual(invoice["DueDate"], "2026-02-04")
        # The two definitions genuinely disagree for this invoice, which is why
        # keeping the field cannot be allowed to change aging by accident:
        # measured from the invoice date it is 85 days old and outside the 90+
        # bucket the stale-receivable finding is built on, while measured from
        # the due date it is 55 days overdue. `reporting.py` ages from the
        # invoice date by documented policy; aging itself is covered by
        # test_reporting.py, so this only pins the parsed inputs.
        as_of = date(2026, 3, 31)
        self.assertEqual((as_of - date.fromisoformat(invoice["TxnDate"])).days, 85)
        self.assertEqual((as_of - date.fromisoformat(invoice["DueDate"])).days, 55)

    def test_an_ar_export_without_a_due_date_column_still_parses(self):
        without_due = (
            b"Accounts Receivable Aging Detail,,,\n"
            b"As of March 31 2026,,,\n"
            b"Customer,Transaction Type,Date,Num,Open Balance\n"
            b"Smith,Invoice,01/05/2026,1001,7500.00\n"
        )
        package = qbo_exports.parse_exports(LEDGER, without_due)
        self.assertNotIn("DueDate", package.objects["Invoice"][0])

    def test_rejects_a_summary_report_without_transaction_columns(self):
        with self.assertRaisesRegex(qbo_exports.ExportError, "recognizable header"):
            qbo_exports.parse_exports(b"Profit and Loss\nIncome,100\n", AR)

    def test_rejects_ar_that_would_turn_unknown_into_zero(self):
        empty_ar = b"Date,Transaction Type,Open Balance\n03/31/2026,Payment,0\n"
        with self.assertRaisesRegex(qbo_exports.ExportError, "neither open invoice"):
            qbo_exports.parse_exports(LEDGER, empty_ar)

    def test_accepts_an_explicit_zero_ar_control_total(self):
        zero_ar = (
            b"Accounts Receivable Aging Detail,,,\n"
            b"Customer,Transaction Type,Date,Num,Open Balance\n"
            b"TOTAL,,,,0.00\n"
        )
        package = qbo_exports.parse_exports(LEDGER, zero_ar)
        self.assertEqual(package.rows_imported["ar_aging"], 0)
        self.assertEqual(package.evidence["ar_open_balance_control"], "0.00")

    def test_rejects_ar_rows_that_do_not_reconcile_to_the_control_total(self):
        mismatched = AR + b"TOTAL,,,,,8000.00\n"
        with self.assertRaisesRegex(qbo_exports.ExportError, "control total"):
            qbo_exports.parse_exports(LEDGER, mismatched)

    def _ledger(self, *rows: str) -> bytes:
        head = (
            "Transaction Detail by Account,,,,,,,\n"
            "January 1 - March 31 2026,,,,,,,\n"
            "Date,Transaction Type,Num,Name,Memo/Description,Account,Customer/Project,Amount\n"
        )
        return (head + "".join(rows)).encode("utf-8")

    def test_the_same_charge_entered_twice_stays_two_transactions(self):
        """Merging them would report one $1,000 charge and hide the duplicate."""
        row = "02/01/2026,Expense,E-22,Tool Hire,Compressor,Job Materials,Smith:Kitchen,500.00\n"
        package = qbo_exports.parse_exports(self._ledger(row, row), AR)
        purchases = package.objects["Purchase"]
        self.assertEqual(len(purchases), 2)
        self.assertEqual([item["TotalAmt"] for item in purchases], ["500.00", "500.00"])
        self.assertEqual(package.rows_imported["transaction_detail"], 2)

    def test_a_genuine_split_across_accounts_remains_one_transaction(self):
        package = qbo_exports.parse_exports(self._ledger(
            "02/01/2026,Expense,S-1,Tool Hire,Compressor,Job Materials,Smith:Kitchen,300.00\n",
            "02/01/2026,Expense,S-1,Tool Hire,Delivery,Freight,Smith:Kitchen,200.00\n",
        ), AR)
        purchases = package.objects["Purchase"]
        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0]["TotalAmt"], "500.00")
        self.assertEqual(len(purchases[0]["Line"]), 2)

    def test_a_duplicated_charge_reaches_the_duplicate_detector(self):
        row = "02/01/2026,Expense,E-22,Tool Hire,Compressor,Job Materials,Smith:Kitchen,500.00\n"
        package = qbo_exports.parse_exports(self._ledger(row, row), AR)
        analysis = work_engine.analyze(
            package.objects, package.evidence, today=date(2026, 3, 31))
        duplicates = [f for f in analysis.findings if f.defect_type == "duplicate_transaction"]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0].financial_effect, Decimal("-500.00"))


class ExportPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "exports.db"
        self.conn = service._db()
        self.org = crm.new_id("org")
        self.conn.execute(
            "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
            (self.org, "Export Contractor", self.org),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_export_bundle_reaches_the_existing_review_and_pdf_projection(self):
        package = qbo_exports.parse_exports(LEDGER, AR)
        analysis = work_engine.analyze(
            package.objects, package.evidence, today=date(2026, 3, 31))
        run_id = work_engine.persist_analysis(
            self.conn, organization_id=self.org, engagement_id=None,
            connection_id=None, analysis=analysis, evidence=package.evidence,
            provider="qbo_export",
        )
        result = reporting.portfolio(self.conn, run_id, as_of=date(2026, 3, 31))
        self.assertEqual(result["ar_total_outstanding"], 7500.0)
        self.assertEqual(result["unassigned_job_costs_total"], 1000.0)
        self.assertEqual(result["duplicate_vendor_charges_total"], 500.0)
        project = next(row for row in result["projects"] if row["project_name"] == "Smith:Kitchen")
        self.assertEqual(project["revenue"], 20000.0)
        self.assertEqual(project["cost"], 13000.0)


if __name__ == "__main__":
    unittest.main()
