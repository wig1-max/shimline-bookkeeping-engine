"""Acceptance tests for the reporting projection.

The oracle is not invented here. ``data/*.csv`` and ``data/findings.json`` are
an existing known-good pair -- the published sample Cash-Leak Review was
computed from those CSVs by ``scripts/analyze.py`` -- so the test loads the
CSVs into the canonical tables through the production shredder, projects them,
and asserts the answer matches what ``analyze.py`` computes from the same
inputs. That is a regression test against a known answer, not a smoke test.
"""
import copy
import csv
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY", "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import crm, report_fixture, reporting, work_engine  # noqa: E402
from shimline.synthetic_books import generate_companies  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
AS_OF = date(2026, 8, 31)

DUPLICATE_UNIT = Decimal("2140")
PLANTED_DUPLICATE_PAIR = DUPLICATE_UNIT * 2
LEDGER_251_COST = Decimal("135280")


def reference_findings() -> dict:
    """Run scripts/analyze.py -- the reference implementation -- over the CSVs."""
    spec = importlib.util.spec_from_file_location("analyze_reference",
                                                  ROOT / "scripts" / "analyze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.analyze()


class DatasetConsistencyTests(unittest.TestCase):
    def test_every_project_actual_cost_reconciles_to_its_vendor_rows(self):
        with open(DATA / "projects.csv", newline="", encoding="utf-8") as handle:
            projects = list(csv.DictReader(handle))
        with open(DATA / "vendor_transactions.csv", newline="", encoding="utf-8") as handle:
            transactions = list(csv.DictReader(handle))
        actual = {row["project_id"]: Decimal(row["actual_cost"]) for row in projects}
        ledger = {
            project_id: sum(
                (Decimal(row["amount"]) for row in transactions
                 if row["project_id"] == project_id), Decimal("0")
            )
            for project_id in actual
        }
        self.assertEqual(ledger, actual)
        duplicate_rows = [
            row for row in transactions
            if row["project_id"] == "251" and Decimal(row["amount"]) == DUPLICATE_UNIT
        ]
        self.assertEqual(len(duplicate_rows), 2)
        self.assertEqual(sum((Decimal(row["amount"]) for row in duplicate_rows), Decimal("0")),
                         PLANTED_DUPLICATE_PAIR)


class _CanonicalCase(unittest.TestCase):
    """Loads provider objects through the production shredder, then projects."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "reporting.db"
        self.conn = service._db()
        self.org = crm.new_id("org")
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                          (self.org, "Reporting Co", self.org))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def ingest(self, objects: dict) -> str:
        run_id = crm.new_id("bkr")
        self.conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,period_end,status) "
            "VALUES(?,?,?,?,'review')", (run_id, self.org, "2026-01-01", "2026-08-31"))
        work_engine.persist_canonical(self.conn, run_id, self.org, objects)
        self.conn.commit()
        return run_id

    def project(self, objects: dict, **kwargs) -> dict:
        return reporting.portfolio(self.conn, self.ingest(objects), as_of=AS_OF, **kwargs)


class CsvOracleTests(_CanonicalCase):
    def test_the_projection_reproduces_the_reference_findings_exactly(self):
        expected = reference_findings()
        produced = self.project(report_fixture.qbo_objects())
        for key, value in expected.items():
            with self.subTest(field=key):
                self.assertEqual(produced.get(key), value)

    def test_nothing_in_the_contract_is_missing_from_the_projection(self):
        expected = reference_findings()
        produced = self.project(report_fixture.qbo_objects())
        self.assertEqual(set(expected) - set(produced), set())

    def test_a_clean_run_reports_nothing_as_unavailable(self):
        produced = self.project(report_fixture.qbo_objects())
        self.assertEqual(produced["unavailable"], [])

    def test_published_sample_and_canonical_projection_are_identical(self):
        published = json.loads((DATA / "findings.json").read_text(encoding="utf-8"))
        produced = self.project(report_fixture.qbo_objects())
        differing = {key for key in published if published[key] != produced.get(key)}
        self.assertEqual(differing, set())
        project = next(row for row in produced["projects"] if row["project_id"] == "251")
        self.assertEqual(Decimal(str(project["cost"])), LEDGER_251_COST)
        self.assertEqual(Decimal(str(produced["duplicate_vendor_charges_total"])), DUPLICATE_UNIT)


class HonestDegradationTests(_CanonicalCase):
    def test_a_file_with_no_jobs_says_so_instead_of_reporting_zero_margin(self):
        objects = report_fixture.qbo_objects()
        job_ids = {item["Id"] for item in objects["Customer"] if item.get("Job")}
        objects["Customer"] = [item for item in objects["Customer"] if not item.get("Job")]
        objects["Invoice"] = [item for item in objects["Invoice"]
                              if item["CustomerRef"]["value"] not in job_ids]
        produced = self.project(objects)
        self.assertEqual(produced["projects"], [])
        self.assertIsNone(produced["portfolio_avg_margin_pct"])
        self.assertIsNone(produced["worst_project"])
        self.assertIsNone(produced["worst_project_margin_gap"])
        blocked = {item["field"]: item for item in produced["unavailable"]}
        self.assertEqual(blocked["project_profitability"]["status"], "blocked")
        self.assertIn("no projects configured", blocked["project_profitability"]["reason"])
        self.assertTrue(blocked["project_profitability"]["missing_source"])

    def test_jobs_with_no_tagged_revenue_are_reported_as_unmeasurable(self):
        objects = report_fixture.qbo_objects()
        objects["Invoice"] = [item for item in objects["Invoice"]
                              if not str(item["Id"]).startswith("PRJ-REV-")]
        produced = self.project(objects)
        self.assertTrue(produced["projects"])
        self.assertIsNone(produced["portfolio_avg_margin_pct"])
        blocked = {item["field"] for item in produced["unavailable"]}
        self.assertIn("project_profitability", blocked)

    def test_a_pull_without_unapplied_amounts_does_not_claim_cash_is_applied(self):
        objects = report_fixture.qbo_objects()
        for payment in objects["Payment"]:
            payment.pop("UnappliedAmt")
        produced = self.project(objects)
        self.assertIsNone(produced["unapplied_payments_total"])
        self.assertIsNone(produced["unapplied_payments_count"])
        blocked = {item["field"]: item for item in produced["unavailable"]}
        self.assertEqual(blocked["unapplied_payments"]["missing_source"], "Payment.UnappliedAmt")

    def test_a_partial_exposure_total_names_what_it_left_out(self):
        objects = report_fixture.qbo_objects()
        for payment in objects["Payment"]:
            payment.pop("UnappliedAmt")
        produced = self.project(objects)
        partial = next(item for item in produced["unavailable"]
                       if item["field"] == "total_exposure")
        self.assertIn("unapplied_payments_total", partial["reason"])
        # The remaining components are still added up; the figure is a floor.
        self.assertGreater(produced["total_exposure"], 0)

    def test_health_scores_are_withheld_rather_than_computed_from_gaps(self):
        objects = report_fixture.qbo_objects()
        for payment in objects["Payment"]:
            payment.pop("UnappliedAmt")
        produced = self.project(objects)
        self.assertIsNone(produced["health_scores"])
        blocked = {item["field"] for item in produced["unavailable"]}
        self.assertIn("health_scores", blocked)

    def test_price_movement_is_blocked_without_a_repeated_unit_priced_line(self):
        objects = report_fixture.qbo_objects()
        for purchase in objects["Purchase"]:
            detail = purchase["Line"][0].pop("ItemBasedExpenseLineDetail", None)
            if detail:
                detail.pop("UnitPrice", None)
                detail.pop("Qty", None)
                purchase["Line"][0]["AccountBasedExpenseLineDetail"] = detail
        produced = self.project(objects)
        self.assertIsNone(produced["vendor_price_variance"])
        blocked = {item["field"]: item for item in produced["unavailable"]}
        self.assertEqual(blocked["vendor_price_variance"]["status"], "blocked")
        self.assertTrue(blocked["vendor_price_variance"]["missing_source"])

    def test_job_cost_posted_by_journal_entry_is_declared_not_silently_dropped(self):
        objects = report_fixture.qbo_objects()
        project_id = next(item["Id"] for item in objects["Customer"] if item.get("Job"))
        objects["JournalEntry"] = [{
            "Id": "JE-1", "TxnDate": "2026-05-01", "TotalAmt": 900.0, "SyncToken": "0",
            "Line": [{"Id": "1", "Amount": 900.0, "Description": "Reclass",
                      "JournalEntryLineDetail": {"PostingType": "Debit",
                                                 "AccountRef": {"value": "5000"},
                                                 "CustomerRef": {"value": project_id}}}],
        }]
        produced = self.project(objects)
        declared = {item["field"]: item for item in produced["unavailable"]}
        self.assertEqual(declared["journal_entry_job_cost"]["status"], "manual")
        self.assertIn("1 journal-entry line", declared["journal_entry_job_cost"]["reason"])


class CanonicalIngestTests(_CanonicalCase):
    def test_unapplied_cash_survives_the_shredder_as_an_open_balance(self):
        run_id = self.ingest(report_fixture.qbo_objects())
        total, count = self.conn.execute(
            "SELECT SUM(CAST(open_balance AS REAL)), COUNT(*) FROM bookkeeping_transactions "
            "WHERE run_id=? AND provider_type='Payment' AND CAST(open_balance AS REAL) > 0",
            (run_id,)).fetchone()
        self.assertEqual(count, 3)
        self.assertAlmostEqual(total, 3170.0, places=2)

    def test_a_unit_price_is_persisted_and_an_absent_one_stays_null(self):
        run_id = self.ingest(report_fixture.qbo_objects())
        priced, unpriced = self.conn.execute(
            "SELECT SUM(l.unit_price IS NOT NULL), SUM(l.unit_price IS NULL) "
            "FROM bookkeeping_transaction_lines l "
            "JOIN bookkeeping_transactions t ON t.id=l.transaction_id "
            "WHERE t.run_id=? AND t.provider_type='Purchase'", (run_id,)).fetchone()
        self.assertEqual(priced, 5)
        self.assertGreater(unpriced, 0)

    def test_an_estimate_is_read_and_kept_out_of_the_posted_set(self):
        objects = report_fixture.qbo_objects()
        project_id = next(item["Id"] for item in objects["Customer"] if item.get("Job"))
        objects["Estimate"] = [{
            "Id": "EST-1", "DocNumber": "EST-1", "TxnDate": "2026-01-05", "TotalAmt": 64000.0,
            "CustomerRef": {"value": project_id}, "SyncToken": "0",
            "Line": [{"Id": "1", "Amount": 64000.0, "Description": "Quoted scope",
                      "SalesItemLineDetail": {"AccountRef": {"value": "4000"},
                                              "CustomerRef": {"value": project_id}}}],
        }]
        run_id = self.ingest(objects)
        row = self.conn.execute(
            "SELECT status, total_amount FROM bookkeeping_transactions "
            "WHERE run_id=? AND provider_type='Estimate'", (run_id,)).fetchone()
        self.assertEqual(row["status"], "estimate")
        self.assertEqual(row["total_amount"], "64000.00")
        # A quote must not become revenue.
        produced = reporting.portfolio(self.conn, run_id, as_of=AS_OF)
        expected = reference_findings()
        self.assertEqual(produced["projects"], expected["projects"])

    def test_estimates_are_read_but_check_ten_still_reports_honestly(self):
        # Reading the objects is not the same as automating the comparison.
        # The registry must not claim a detector that does not exist.
        from shimline.qbo_adapter import READ_OBJECTS
        self.assertIn("Estimate", READ_OBJECTS)
        check = next(item for item in work_engine.CHECKS if item["id"] == "10")
        automated = {item["defect_type"] for item in work_engine.CHECKS
                     if item["mode"] == "automated"}
        self.assertEqual(check["mode"] == "automated", "estimate_variance" in automated)


class RetentionOfProjectedDataTests(_CanonicalCase):
    """The reporting substrate is client financial data and expires like the rest.

    The projection reads a full shredded ledger, and this change added unit
    prices and quantities to it. Nothing may hold client data that the daily
    purge does not reach, so that is asserted here rather than assumed from
    the cascade.
    """

    def test_a_closed_engagements_ledger_including_prices_is_purged(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        engagement = crm.new_id("eng")
        self.conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status,created_at,closed_at) "
            "VALUES(?,?,?,'closed',?,?)",
            (engagement, self.org, "Cash-Leak Review",
             (now - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S"),
             (now - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")))
        run_id = crm.new_id("bkr")
        self.conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,engagement_id,period_start,"
            "period_end,status) VALUES(?,?,?,?,?,'review')",
            (run_id, self.org, engagement, "2026-01-01", "2026-08-31"))
        work_engine.persist_canonical(self.conn, run_id, self.org, report_fixture.qbo_objects())
        self.conn.commit()

        def count(table, where, args):
            return self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]

        self.assertGreater(count("bookkeeping_transactions", "run_id=?", (run_id,)), 0)
        self.assertGreater(self.conn.execute(
            "SELECT COUNT(*) FROM bookkeeping_transaction_lines l "
            "JOIN bookkeeping_transactions t ON t.id=l.transaction_id "
            "WHERE t.run_id=? AND l.unit_price IS NOT NULL", (run_id,)).fetchone()[0], 0)

        removed = service.purge_expired(now)
        self.assertIn(engagement, {item["id"] for item in removed})

        self.conn.close()
        self.conn = service._db()
        self.assertEqual(count("bookkeeping_runs", "id=?", (run_id,)), 0)
        self.assertEqual(count("bookkeeping_transactions", "run_id=?", (run_id,)), 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM bookkeeping_transaction_lines").fetchone()[0], 0)


def _build_report_module():
    spec = importlib.util.spec_from_file_location("build_report",
                                                  ROOT / "scripts" / "build_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RenderTests(_CanonicalCase):
    """The PDF is rendered from a connection, and never prints a fake number."""

    def setUp(self):
        super().setUp()
        self.build_report = _build_report_module()
        self.company = json.loads((DATA / "company.json").read_text(encoding="utf-8"))

    def render(self, findings: dict) -> str:
        return self.build_report.render(findings, self.company, report_date="August 31, 2026")

    def test_a_full_file_renders_the_same_headline_numbers_as_the_csv_path(self):
        produced = self.project(report_fixture.qbo_objects())
        from_connection = self.render(produced)
        from_csv = self.render(reference_findings())
        for figure in ("$18,420", "$11,760", "$2,140", "$3,170"):
            self.assertIn(figure, from_connection)
            self.assertIn(figure, from_csv)
        self.assertNotIn("not available", from_connection)
        self.assertNotIn("could not determine", from_connection)

    def test_an_unavailable_figure_is_named_in_the_pdf_not_printed_as_zero(self):
        objects = report_fixture.qbo_objects()
        for payment in objects["Payment"]:
            payment.pop("UnappliedAmt")
        produced = self.project(objects)
        html = self.render(produced)
        self.assertIn("What this review could not determine", html)
        self.assertIn("unapplied payments", html)
        self.assertIn("Payment.UnappliedAmt", html)
        self.assertIn("not available", html)
        # The unapplied figure must not have become a dollar-zero anywhere.
        self.assertNotIn("$0 in customer payments", html)
        self.assertIn("Health scores not available", html)

    def test_a_file_with_no_jobs_renders_without_an_empty_margin_table(self):
        objects = report_fixture.qbo_objects()
        job_ids = {item["Id"] for item in objects["Customer"] if item.get("Job")}
        objects["Customer"] = [item for item in objects["Customer"] if not item.get("Job")]
        objects["Invoice"] = [item for item in objects["Invoice"]
                              if item["CustomerRef"]["value"] not in job_ids]
        html = self.render(self.project(objects))
        self.assertIn("Project profitability not available", html)
        self.assertIn("no projects configured", html)
        self.assertNotIn("requires investigation", html)

    def test_the_dollar_filter_refuses_to_format_a_missing_number(self):
        self.assertEqual(self.build_report.dollars(None), "not available")
        self.assertEqual(self.build_report.dollars(18420.0), "$18,420")

    def test_no_synthetic_company_renders_a_none_into_the_page(self):
        for company in generate_companies(4):
            with self.subTest(company=company.company_id):
                produced = self.project(company.objects)
                html = self.render(produced)
                self.assertNotIn("None", html)
                # The section is rendered when there is something to put in it.
                # It used to be unconditional only because these files always
                # had an unavailable figure -- job margin, for want of any
                # revenue tagged to a job. Now that margin computes, a clean
                # file legitimately has nothing it could not determine, and
                # printing an empty "could not determine" heading would invent
                # a caveat.
                if produced["unavailable"]:
                    self.assertIn("What this review could not determine", html)
                else:
                    self.assertNotIn("What this review could not determine", html)

    def test_the_unavailable_section_appears_when_a_figure_is_missing(self):
        company = generate_companies(1)[0]
        objects = copy.deepcopy(company.objects)
        for payment in objects["Payment"]:
            payment.pop("UnappliedAmt", None)
        produced = self.project(objects)
        self.assertTrue(produced["unavailable"])
        html = self.render(produced)
        self.assertIn("What this review could not determine", html)
        self.assertNotIn("None", html)


class SyntheticRobustnessTests(_CanonicalCase):
    def test_the_projection_neither_crashes_nor_fabricates_on_messy_files(self):
        for company in generate_companies(12):
            with self.subTest(company=company.company_id):
                produced = self.project(company.objects)
                self.assertIn("unavailable", produced)
                # Invoice lines now carry a CustomerRef, so revenue is
                # attributable to a job and margin is computable. It used to be
                # declared unavailable here because the generator emitted
                # `"Line": []` on every invoice -- cost was tagged to a job and
                # revenue was not, so the client report could never show margin.
                self.assertTrue([row for row in produced["projects"] if row["revenue"]],
                                "no job has revenue; job margin is uncomputable again")
                self.assertIsNotNone(produced["worst_project"])
                self.assertIsNotNone(produced["portfolio_avg_margin_pct"])
                for item in produced["unavailable"]:
                    self.assertIn(item["status"], {"blocked", "manual"})
                    self.assertTrue(item["reason"])
                self.assertGreaterEqual(produced["total_exposure"], 0)

    def test_margin_is_declared_unavailable_when_no_revenue_is_job_tagged(self):
        """The guarantee the test above used to carry, kept on its own file.

        A contractor mid-job has cost booked against a job and nothing invoiced
        back yet. Margin is not computable from that, and reporting it as zero
        would say the job lost every dollar spent on it.
        """
        company = generate_companies(1)[0]
        objects = copy.deepcopy(company.objects)
        for invoice in objects["Invoice"]:
            invoice["Line"] = []          # cost stays tagged; revenue does not
        produced = self.project(objects)
        declared = {item["field"] for item in produced["unavailable"]}
        self.assertIn("project_profitability", declared)
        self.assertIsNone(produced["worst_project"])
        self.assertIsNone(produced["portfolio_avg_margin_pct"])
        self.assertEqual([row for row in produced["projects"] if row["revenue"]], [])

    def test_every_figure_is_either_a_number_or_declared_unavailable(self):
        contract = set(reference_findings())
        for company in generate_companies(4):
            produced = self.project(company.objects)
            declared = {item["field"] for item in produced["unavailable"]}
            for key in contract:
                value = produced[key]
                if value is None:
                    self.assertTrue(
                        declared, f"{key} is absent from {company.company_id} "
                                  "without anything declared unavailable")


if __name__ == "__main__":
    unittest.main()
