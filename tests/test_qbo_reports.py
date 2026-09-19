"""Pulling QuickBooks reports, storing them, and deleting them on schedule."""
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import admin as admin_workspace  # noqa: E402
from shimline import auth, clock, crm, qbo_reports  # noqa: E402
from shimline import quickbooks as qbo  # noqa: E402


def fake_report(name):
    return {"Header": {"ReportName": name, "Currency": "CAD"},
            "Rows": {"Row": [{"ColData": [{"value": "Sales"}, {"value": "128400.00"}]}]}}


class ReportPullTests(unittest.TestCase):
    PASSWORD = "local-test-password-only"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "intake.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        admin_workspace.configure(
            db_factory=service._db, uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90, cookie_secure=False)
        qbo.configure(db_factory=service._db, client_id="id", client_secret="secret",
                      redirect_uri="https://api.shimline.ca/qbo/callback",
                      environment="sandbox")

        conn = service._db()
        self.user_id = auth.create_user(conn, "op@example.invalid", "Op", self.PASSWORD, "owner")
        self.organization_id = crm.new_id("org")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage) VALUES(?,?,?,'active')",
            (self.organization_id, "Northstar", "northstar"))
        self.engagement_id = crm.new_id("eng")
        conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status) "
            "VALUES(?,?,'Cash-Leak Review','in_progress')",
            (self.engagement_id, self.organization_id))
        self.connection_id = crm.new_id("con_qbo")
        conn.execute(
            "INSERT INTO connections(id,organization_id,provider,environment,realm_id_enc,"
            "realm_id_hash,access_token_enc,refresh_token_enc,access_expires_at,status) "
            "VALUES(?,?,'quickbooks','sandbox',?,?,?,?,?,'active')",
            (self.connection_id, self.organization_id,
             qbo.encrypt_token("4620816365"), "hash",
             qbo.encrypt_token("access-token"), qbo.encrypt_token("refresh-token"),
             clock.format_timestamp(clock.now() + timedelta(hours=1))))
        conn.commit()
        conn.close()

        self.fetched = []
        self._real_fetch = qbo_reports._fetch

        def fake_fetch(realm_id, report, access_token, environment, start, end):
            self.fetched.append((report.name, realm_id, environment, start, end))
            return fake_report(report.name)

        qbo_reports._fetch = fake_fetch

    def tearDown(self):
        qbo_reports._fetch = self._real_fetch
        self.temp.cleanup()

    # ------------------------------------------------------------- pulling --

    def test_a_pull_stores_every_report_encrypted(self):
        conn = service._db()
        result = qbo_reports.pull(conn, connection_id=self.connection_id,
                                  engagement_id=self.engagement_id,
                                  requested_by_user_id=self.user_id)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["stored"], len(qbo_reports.REPORTS))

        rows = conn.execute(
            "SELECT report_name,payload_enc,byte_size FROM source_snapshots").fetchall()
        conn.close()
        self.assertEqual(len(rows), len(qbo_reports.REPORTS))
        for name, payload_enc, size in rows:
            self.assertNotIn("128400.00", payload_enc, "payloads must be encrypted at rest")
            self.assertGreater(size, 0)
        # And nothing readable landed in the database file itself.
        self.assertNotIn(b"128400.00", Path(service.DB_PATH).read_bytes())

    def test_the_stored_report_round_trips_to_what_quickbooks_returned(self):
        conn = service._db()
        qbo_reports.pull(conn, connection_id=self.connection_id,
                         engagement_id=self.engagement_id)
        snapshot_id = conn.execute(
            "SELECT id FROM source_snapshots WHERE report_name='ProfitAndLoss'").fetchone()[0]
        snapshot = qbo_reports.read_snapshot(conn, snapshot_id)
        conn.close()
        self.assertEqual(snapshot["payload"], fake_report("ProfitAndLoss"))

    def test_a_scan_can_refresh_only_the_trial_balance_and_bind_to_that_run(self):
        conn = service._db()
        result = qbo_reports.pull(
            conn, connection_id=self.connection_id,
            reports=(qbo_reports.REPORTS_BY_NAME["TrialBalance"],))
        snapshot = qbo_reports.report_for_run(
            conn, result["sync_run_id"], "TrialBalance")
        conn.close()
        self.assertEqual([item[0] for item in self.fetched], ["TrialBalance"])
        self.assertEqual(snapshot["payload"], fake_report("TrialBalance"))

    def test_the_sandbox_host_is_used_for_a_sandbox_connection(self):
        conn = service._db()
        qbo_reports.pull(conn, connection_id=self.connection_id)
        conn.close()
        self.assertTrue(all(env == "sandbox" for _, _, env, _, _ in self.fetched))
        self.assertIn("sandbox", qbo_reports.API_BASE["sandbox"])
        self.assertNotIn("sandbox", qbo_reports.API_BASE["production"])

    def test_one_failing_report_does_not_lose_the_others(self):
        def flaky(realm_id, report, access_token, environment, start, end):
            if report.name == "GeneralLedger" or report.name == "CustomerIncome":
                raise HTTPException(502, f"QuickBooks refused the {report.label} report")
            return fake_report(report.name)

        qbo_reports._fetch = flaky
        conn = service._db()
        result = qbo_reports.pull(conn, connection_id=self.connection_id,
                                  engagement_id=self.engagement_id)
        stored = conn.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0]
        run = conn.execute("SELECT status,detail FROM sync_runs").fetchone()
        conn.close()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(stored, len(qbo_reports.REPORTS) - 1)
        self.assertEqual(run[0], "partial")
        self.assertIn("Income by customer", run[1])

    def test_a_revoked_connection_will_not_pull(self):
        conn = service._db()
        conn.execute("UPDATE connections SET status='revoked' WHERE id=?", (self.connection_id,))
        conn.commit()
        with self.assertRaises(qbo.ReconnectRequired):
            qbo_reports.pull(conn, connection_id=self.connection_id)
        conn.close()

    def test_the_pull_is_recorded_for_the_operator_and_the_audit(self):
        conn = service._db()
        qbo_reports.pull(conn, connection_id=self.connection_id,
                         engagement_id=self.engagement_id,
                         requested_by_user_id=self.user_id)
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_events")]
        activity = conn.execute("SELECT body FROM activities").fetchone()[0]
        conn.close()
        self.assertIn("qbo.reports.pull", actions)
        self.assertIn("QuickBooks reports", activity)

    # ----------------------------------------------------------- retention --

    def test_reports_are_deleted_on_the_same_promise_as_uploaded_documents(self):
        conn = service._db()
        qbo_reports.pull(conn, connection_id=self.connection_id,
                         engagement_id=self.engagement_id)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0],
            len(qbo_reports.REPORTS))
        # Closed 31 days ago: past the 30-day window.
        conn.execute("UPDATE engagements SET closed_at=? WHERE id=?",
                     (clock.format_timestamp(clock.now() - timedelta(days=31)),
                      self.engagement_id))
        conn.commit()
        conn.close()

        removed = service.purge_expired()

        conn = service._db()
        remaining = conn.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0]
        logged = conn.execute(
            "SELECT snapshots_removed FROM purge_log WHERE submission_id=?",
            (self.engagement_id,)).fetchone()
        conn.close()
        self.assertEqual(remaining, 0, "pulled reports must not outlive the promise")
        self.assertEqual(logged[0], len(qbo_reports.REPORTS))
        self.assertTrue(any(item["id"] == self.engagement_id for item in removed))

    def test_reports_for_a_recently_closed_engagement_are_kept(self):
        """The boundary that matters: closed yesterday is not expired."""
        conn = service._db()
        qbo_reports.pull(conn, connection_id=self.connection_id,
                         engagement_id=self.engagement_id)
        conn.execute("UPDATE engagements SET created_at=?,closed_at=? WHERE id=?",
                     (clock.format_timestamp(clock.now() - timedelta(days=200)),
                      clock.format_timestamp(clock.now() - timedelta(days=1)),
                      self.engagement_id))
        conn.commit()
        conn.close()
        service.purge_expired()
        conn = service._db()
        remaining = conn.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0]
        conn.close()
        self.assertEqual(remaining, len(qbo_reports.REPORTS))

    def test_an_unattached_pull_still_ages_out(self):
        conn = service._db()
        qbo_reports.pull(conn, connection_id=self.connection_id, engagement_id=None)
        conn.execute("UPDATE source_snapshots SET fetched_at=?",
                     (clock.format_timestamp(clock.now() - timedelta(days=91)),))
        conn.commit()
        conn.close()
        service.purge_expired()
        conn = service._db()
        remaining = conn.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0]
        conn.close()
        self.assertEqual(remaining, 0, "an orphan pull must not sit on disk for ever")

    # -------------------------------------------------------- operator use --

    def test_an_operator_can_pull_and_download_but_a_stranger_cannot(self):
        http = TestClient(service.app, follow_redirects=False)
        http.post("/admin/login", data={"email": "op@example.invalid",
                                        "password": self.PASSWORD, "next": "/admin"})
        conn = service._db()
        csrf = conn.execute("SELECT csrf_token FROM sessions").fetchone()[0]
        conn.close()

        pulled = http.post(f"/admin/engagements/{self.engagement_id}/pull-reports",
                           data={"csrf_token": csrf})
        self.assertEqual(pulled.status_code, 303)

        conn = service._db()
        snapshot_id = conn.execute(
            "SELECT id FROM source_snapshots WHERE report_name='BalanceSheet'").fetchone()[0]
        conn.close()

        downloaded = http.get(f"/admin/snapshots/{snapshot_id}")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(json.loads(downloaded.text), fake_report("BalanceSheet"))
        self.assertEqual(downloaded.headers["cache-control"], "no-store")

        anonymous = TestClient(service.app, follow_redirects=False)
        self.assertEqual(anonymous.get(f"/admin/snapshots/{snapshot_id}").status_code, 401)
        anonymous.close()
        http.close()

    def test_pulling_needs_a_csrf_token_and_a_live_connection(self):
        http = TestClient(service.app, follow_redirects=False)
        http.post("/admin/login", data={"email": "op@example.invalid",
                                        "password": self.PASSWORD, "next": "/admin"})
        forged = http.post(f"/admin/engagements/{self.engagement_id}/pull-reports",
                           data={"csrf_token": "wrong"})
        self.assertEqual(forged.status_code, 403)

        conn = service._db()
        csrf = conn.execute("SELECT csrf_token FROM sessions").fetchone()[0]
        conn.execute("UPDATE connections SET status='revoked' WHERE id=?", (self.connection_id,))
        conn.commit()
        conn.close()
        no_connection = http.post(f"/admin/engagements/{self.engagement_id}/pull-reports",
                                  data={"csrf_token": csrf})
        self.assertEqual(no_connection.status_code, 409)
        http.close()

    def test_every_declared_report_carries_an_operator_facing_explanation(self):
        for report in qbo_reports.REPORTS:
            self.assertTrue(report.label, report.name)
            self.assertTrue(report.why.endswith("."), report.name)


if __name__ == "__main__":
    unittest.main()
