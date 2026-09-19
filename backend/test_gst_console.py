"""The GST/HST return through the real router.

Two things are being defended. That the page an accountant signs from actually
renders what was computed, and that the tenancy boundary and the role gate both
still apply to a page carrying a CRA figure.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

from fastapi.testclient import TestClient  # noqa: E402

import app as service  # noqa: E402
from shimline import (admin, filing_periods, tax_rates, tenancy)

PASSWORD = "local-test-password-only"
ORG = "org_a"


def _closed_quarter():
    """The last calendar quarter that has finished, as of now."""
    arrangement = filing_periods.Arrangement(
        frequency=filing_periods.QUARTERLY, year_end_month=12, year_end_day=31)
    today = date.today()
    closed = [period for period in filing_periods.periods_between(
        arrangement, start=filing_periods._add_months(today, -18), end=today)
        if period.end < today]
    return closed[-1]


class GstConsoleCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "gst.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        admin.configure(
            db_factory=service._db, uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90,
            cookie_secure=False, pdf_renderer=lambda html: b"%PDF")

        conn = service._db()
        for ident, name in ((ORG, "Northlake Roofing"), ("org_b", "Other Firm's")):
            conn.execute("INSERT INTO organizations(id,name,normalized_name) "
                         "VALUES(?,?,?)", (ident, name, ident))
        tenancy.create_firm(conn, "firm_a", "Alder & Co")
        tenancy.create_firm(conn, "firm_b", "Birchwood LLP")
        tenancy.add_client(conn, "firm_a", ORG)
        tenancy.add_client(conn, "firm_b", "org_b")
        tenancy.create_firm_user(
            conn, "firm_a", email="dana@example.invalid", display_name="Dana",
            password=PASSWORD, firm_role="principal", workspace_role="reviewer")
        tenancy.create_firm_user(
            conn, "firm_a", email="sam@example.invalid", display_name="Sam",
            password=PASSWORD, firm_role="principal", workspace_role="viewer")
        tenancy.create_firm_user(
            conn, "firm_b", email="birch@example.invalid", display_name="Birch",
            password=PASSWORD, firm_role="principal", workspace_role="reviewer")

        # Whichever quarter has most recently closed, so this suite does not
        # start failing when the calendar moves. That is also the period an
        # accountant would actually be filing.
        self.period = _closed_quarter()
        conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
            "period_end,status) VALUES('run_1',?,?,?,'review')",
            (ORG, self.period.start.isoformat(), self.period.end.isoformat()))
        for ident, name, kind in (("acc_rev", "Revenue", "Income"),
                                  ("acc_tax", "GST/HST Payable",
                                   "Other Current Liability")):
            conn.execute(
                "INSERT INTO bookkeeping_accounts(id,organization_id,provider,"
                "provider_id,name,account_type) VALUES(?,?,'quickbooks',?,?,?)",
                (ident, ORG, ident, name, kind))
        tax_rates.persist(conn, ORG, {
            "TaxAgency": [{"Id": "1", "DisplayName": "Canada Revenue Agency"}],
            "TaxRate": [
                {"Id": "HST13", "Name": "HST ON", "RateValue": "13",
                 "AgencyRef": {"value": "1"}},
                {"Id": "MYST", "Name": "Standard", "RateValue": "7"},
            ]})
        filing_periods.record_arrangement(
            conn, ORG, frequency=filing_periods.QUARTERLY, year_end_month=12,
            year_end_day=31, gst_number="123456789RT0001",
            calculation_method=filing_periods.REGULAR_METHOD)
        conn.execute(
            "INSERT INTO bookkeeping_transactions(id,run_id,organization_id,"
            "provider,provider_type,provider_id,transaction_date,document_number,"
            "currency,total_amount,status,source_hash,tax_total) "
            "VALUES('inv_1','run_1',?,'quickbooks','Invoice','inv_1',?,"
            "'INV-1','CAD','1130','posted','h','130.00')",
            (ORG, (self.period.start + timedelta(days=14)).isoformat()))
        conn.execute(
            "INSERT INTO bookkeeping_transaction_taxes(id,transaction_id,"
            "tax_rate_ref,rate_percent,net_amount_taxable,tax_amount) "
            "VALUES('txt_1','inv_1','HST13','13','1000.00','130.00')")
        for index, (account, debit, credit) in enumerate(
                (("acc_rev", "0", "1000.00"), ("acc_tax", "0", "130.00"))):
            conn.execute(
                "INSERT INTO bookkeeping_transaction_lines(id,transaction_id,"
                "provider_line_id,account_id,amount,debit,credit) "
                "VALUES(?,'inv_1',?,?,?,?,?)",
                (f"lin_{index}", str(index), account,
                 credit if debit == "0" else debit, debit, credit))

        # A second sale charged at the rate nobody has classified. An
        # unclassified rate that is never charged is not a problem -- a client
        # may hold rates they do not use -- so the rate has to actually appear
        # on a document for the return to be blocked by it.
        conn.execute(
            "INSERT INTO bookkeeping_transactions(id,run_id,organization_id,"
            "provider,provider_type,provider_id,transaction_date,document_number,"
            "currency,total_amount,status,source_hash,tax_total) "
            "VALUES('inv_2','run_1',?,'quickbooks','Invoice','inv_2',?,"
            "'INV-2','CAD','1070','posted','h','70.00')",
            (ORG, (self.period.start + timedelta(days=20)).isoformat()))
        conn.execute(
            "INSERT INTO bookkeeping_transaction_taxes(id,transaction_id,"
            "tax_rate_ref,rate_percent,net_amount_taxable,tax_amount) "
            "VALUES('txt_2','inv_2','MYST','7','1000.00','70.00')")
        for index, (account, debit, credit) in enumerate(
                (("acc_rev", "0", "1000.00"), ("acc_tax", "0", "70.00"))):
            conn.execute(
                "INSERT INTO bookkeeping_transaction_lines(id,transaction_id,"
                "provider_line_id,account_id,amount,debit,credit) "
                "VALUES(?,'inv_2',?,?,?,?,?)",
                (f"lin2_{index}", str(index), account,
                 credit if debit == "0" else debit, debit, credit))
        conn.commit()
        conn.close()

        self.client = TestClient(service.app, base_url="http://testserver")
        self.csrf = self._login(self.client, "dana@example.invalid")

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def _login(self, client, email):
        client.post("/admin/login",
                    data={"email": email, "password": PASSWORD, "next": "/admin"},
                    follow_redirects=False)
        conn = service._db()
        row = conn.execute(
            "SELECT csrf_token FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE u.email=? ORDER BY s.created_at DESC LIMIT 1", (email,)).fetchone()
        conn.close()
        return row[0]


class ThePage(GstConsoleCase):

    def test_a_period_with_an_unclassified_rate_says_why_there_is_no_return(self):
        body = self.client.get(f"/admin/clients/{ORG}/gst").text
        self.assertIn("Why there is no return", body)
        self.assertIn("Standard 7%", body)

    def test_classifying_the_rate_produces_the_return(self):
        response = self.client.post(
            f"/admin/clients/{ORG}/gst/rates/MYST",
            data={"csrf_token": self.csrf, "classification": "provincial",
                  "reason": "Confirmed PST with the client"},
            follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        body = self.client.get(f"/admin/clients/{ORG}/gst").text
        self.assertIn("Ready for review", body)
        self.assertIn("Line 109", body)
        self.assertIn("130.00", body)

    def test_the_page_says_shimline_does_not_file(self):
        """The one claim that must never be ambiguous on this page."""
        self.client.post(
            f"/admin/clients/{ORG}/gst/rates/MYST",
            data={"csrf_token": self.csrf, "classification": "provincial",
                  "reason": "PST"}, follow_redirects=False)
        body = self.client.get(f"/admin/clients/{ORG}/gst").text
        self.assertIn("Shimline does not file", body)

    def test_the_prepared_return_is_kept(self):
        self.client.post(
            f"/admin/clients/{ORG}/gst/rates/MYST",
            data={"csrf_token": self.csrf, "classification": "provincial",
                  "reason": "PST"}, follow_redirects=False)
        self.client.get(f"/admin/clients/{ORG}/gst")
        conn = service._db()
        row = conn.execute(
            "SELECT period_start,period_end,line_109,filable,status,due_at "
            "FROM bookkeeping_gst_returns WHERE organization_id=?",
            (ORG,)).fetchone()
        conn.close()
        self.assertEqual(row[0], self.period.start.isoformat())
        self.assertEqual(row[2], "130.00")
        self.assertEqual(row[3], 1)
        self.assertEqual(row[4], "prepared")
        self.assertEqual(row[5], self.period.return_due.isoformat(),
                         "one month after the quarter")

    def test_a_client_with_no_filing_arrangement_says_so_plainly(self):
        self._forget_arrangement()
        body = self.client.get(f"/admin/clients/{ORG}/gst").text
        self.assertIn("have not been recorded", body)

    def test_a_client_with_no_arrangement_is_asked_rather_than_left_stuck(self):
        """Without somewhere to record it, the whole lane is unreachable --
        the same gap firm tenancy had before firm_admin.py existed."""
        self._forget_arrangement()
        body = self.client.get(f"/admin/clients/{ORG}/gst").text
        self.assertIn("How does this client file?", body)
        self.assertIn("gst/arrangement", body)
        self.assertIn('name="calculation_method"', body)

    def test_recording_the_arrangement_produces_a_period(self):
        self._forget_arrangement()
        response = self.client.post(
            f"/admin/clients/{ORG}/gst/arrangement",
            data={"csrf_token": self.csrf, "frequency": "quarterly",
                  "year_end_month": "12", "year_end_day": "31",
                  "registrant": "corporation", "gst_number": "123456789RT0001",
                  "calculation_method": "regular"},
            follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        conn = service._db()
        found = filing_periods.arrangement_for(conn, ORG)
        conn.close()
        self.assertEqual(found.frequency, "quarterly")
        self.assertIs(found.is_individual, False)
        self.assertEqual(found.gst_number, "123456789RT0001")
        self.assertEqual(found.calculation_method, "regular")

    def test_an_unrecorded_registrant_stays_unrecorded_not_corporation(self):
        """An annual filer's due date turns on it, and "not answered" must not
        read as "corporation" -- that is three months of difference."""
        self._forget_arrangement()
        self.client.post(
            f"/admin/clients/{ORG}/gst/arrangement",
            data={"csrf_token": self.csrf, "frequency": "annual",
                  "year_end_month": "12", "year_end_day": "31",
                  "registrant": "", "gst_number": "",
                  "calculation_method": "regular"},
            follow_redirects=False)
        conn = service._db()
        found = filing_periods.arrangement_for(conn, ORG)
        conn.close()
        self.assertIsNone(found.is_individual)

    def test_an_impossible_frequency_is_refused(self):
        self._forget_arrangement()
        response = self.client.post(
            f"/admin/clients/{ORG}/gst/arrangement",
            data={"csrf_token": self.csrf, "frequency": "fortnightly",
                  "year_end_month": "12", "year_end_day": "31",
                  "calculation_method": "regular"},
            follow_redirects=False)
        self.assertEqual(response.status_code, 400)

    def test_a_legacy_arrangement_with_no_method_is_asked_for_the_missing_fact(self):
        conn = service._db()
        conn.execute(
            "UPDATE bookkeeping_gst_filing SET calculation_method=NULL "
            "WHERE organization_id=?", (ORG,))
        conn.commit()
        conn.close()
        body = self.client.get(f"/admin/clients/{ORG}/gst").text
        self.assertIn("calculation method has not been recorded", body)
        self.assertIn('name="calculation_method"', body)

    def test_recording_quick_method_blocks_regular_method_figures(self):
        self._forget_arrangement()
        response = self.client.post(
            f"/admin/clients/{ORG}/gst/arrangement",
            data={"csrf_token": self.csrf, "frequency": "quarterly",
                  "year_end_month": "12", "year_end_day": "31",
                  "registrant": "corporation", "gst_number": "123456789RT0001",
                  "calculation_method": "quick"},
            follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        body = self.client.get(f"/admin/clients/{ORG}/gst").text
        self.assertIn("uses the GST/HST Quick Method", body)
        self.assertIn("Not filable", body)
        self.assertNotIn("Ready for review", body)

    def _forget_arrangement(self):
        conn = service._db()
        conn.execute("DELETE FROM bookkeeping_gst_filing WHERE organization_id=?",
                     (ORG,))
        conn.commit()
        conn.close()


class TheBoundariesStillHold(GstConsoleCase):

    def test_another_firm_cannot_read_this_clients_return(self):
        birch = TestClient(service.app, base_url="http://testserver")
        csrf = self._login(birch, "birch@example.invalid")
        try:
            self.assertEqual(
                birch.get(f"/admin/clients/{ORG}/gst",
                          follow_redirects=False).status_code, 404)
            self.assertEqual(
                birch.post(f"/admin/clients/{ORG}/gst/rates/MYST",
                           data={"csrf_token": csrf, "classification": "gst_hst",
                                 "reason": "x"},
                           follow_redirects=False).status_code, 404)
        finally:
            birch.close()

    def test_a_viewer_may_not_record_the_filing_arrangement(self):
        viewer = TestClient(service.app, base_url="http://testserver")
        csrf = self._login(viewer, "sam@example.invalid")
        try:
            self.assertEqual(
                viewer.post(f"/admin/clients/{ORG}/gst/arrangement",
                            data={"csrf_token": csrf, "frequency": "monthly",
                                  "year_end_month": "6", "year_end_day": "30",
                                  "calculation_method": "regular"},
                            follow_redirects=False).status_code, 403)
        finally:
            viewer.close()

    def test_a_viewer_may_read_but_not_classify_a_rate(self):
        """Classifying a rate changes a figure filed with the CRA. It is a
        judgement, not a setting."""
        viewer = TestClient(service.app, base_url="http://testserver")
        csrf = self._login(viewer, "sam@example.invalid")
        try:
            self.assertEqual(
                viewer.get(f"/admin/clients/{ORG}/gst").status_code, 200)
            self.assertEqual(
                viewer.post(f"/admin/clients/{ORG}/gst/rates/MYST",
                            data={"csrf_token": csrf, "classification": "gst_hst",
                                  "reason": "x"},
                            follow_redirects=False).status_code, 403)
        finally:
            viewer.close()
        conn = service._db()
        self.assertEqual(tax_rates.registry(conn, ORG)["MYST"].classification,
                         tax_rates.UNKNOWN)
        conn.close()

    def test_the_decision_records_who_made_it_and_why(self):
        self.client.post(
            f"/admin/clients/{ORG}/gst/rates/MYST",
            data={"csrf_token": self.csrf, "classification": "provincial",
                  "reason": "Confirmed PST with the client"},
            follow_redirects=False)
        conn = service._db()
        row = conn.execute(
            "SELECT classification, reason, decided_by_user_id "
            "FROM bookkeeping_tax_rate_overrides WHERE organization_id=?",
            (ORG,)).fetchone()
        events = [item[0] for item in conn.execute(
            "SELECT action FROM audit_events WHERE action LIKE 'gst.%'")]
        conn.close()
        self.assertEqual(row[0], "provincial")
        self.assertIn("Confirmed PST", row[1])
        self.assertIsNotNone(row[2])
        self.assertEqual(events, ["gst.rate.classify"])


class TheQueueShowsIt(GstConsoleCase):

    def test_a_filable_return_puts_the_client_in_needs_a_decision(self):
        self.client.post(
            f"/admin/clients/{ORG}/gst/rates/MYST",
            data={"csrf_token": self.csrf, "classification": "provincial",
                  "reason": "PST"}, follow_redirects=False)
        self.client.get(f"/admin/clients/{ORG}/gst")

        from shimline import portfolio
        conn = service._db()
        scope = tenancy.scope_for(conn, {"user_id": conn.execute(
            "SELECT id FROM users WHERE email='dana@example.invalid'").fetchone()[0],
            "roles": {"reviewer"}})
        queue = portfolio.build(conn, scope)
        conn.close()
        row = next(item for item in queue.rows if item.organization_id == ORG)
        self.assertEqual(row.state, portfolio.NEEDS_DECISION)
        self.assertEqual(
            row.gst_period,
            f"{self.period.start.isoformat()} to {self.period.end.isoformat()}")
        self.assertEqual(row.gst_due, self.period.return_due.isoformat())

    def test_a_blocked_return_puts_the_client_in_waiting_on_us(self):
        """An unclassified rate is a decision Shimline has not had made, not a
        document the client owes."""
        self.client.get(f"/admin/clients/{ORG}/gst")

        from shimline import portfolio
        conn = service._db()
        scope = tenancy.scope_for(conn, {"user_id": conn.execute(
            "SELECT id FROM users WHERE email='dana@example.invalid'").fetchone()[0],
            "roles": {"reviewer"}})
        queue = portfolio.build(conn, scope)
        conn.close()
        row = next(item for item in queue.rows if item.organization_id == ORG)
        self.assertEqual(row.state, portfolio.WAITING_ON_US)
        self.assertTrue(row.gst_blocked)


if __name__ == "__main__":
    unittest.main()
