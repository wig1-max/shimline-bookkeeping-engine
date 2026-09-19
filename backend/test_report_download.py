"""Security and lifecycle tests for the client-facing PDF download."""
import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY", "test-master-key-not-used-in-production-0123456789")

from fastapi.testclient import TestClient  # noqa: E402

import app as service  # noqa: E402
from shimline import admin, auth, crm, report_fixture, work_engine  # noqa: E402


class ReportDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "reports.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        self.rendered_html = []

        def renderer(html):
            self.rendered_html.append(html)
            return b"%PDF-1.7\n% test renderer\n"

        self.renderer = renderer
        self._configure(renderer)
        conn = service._db()
        self.owner_id = auth.create_user(
            conn, "owner@example.invalid", "Owner", "local-test-password-only", "owner"
        )
        self.viewer_id = auth.create_user(
            conn, "viewer@example.invalid", "Viewer", "local-test-password-only", "viewer"
        )
        self.organization_id = crm.new_id("org")
        self.engagement_id = crm.new_id("eng")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
            (self.organization_id, "PDF Test Contractor", self.organization_id),
        )
        conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status) "
            "VALUES(?,?,?,'internal_review')",
            (self.engagement_id, self.organization_id, "Cash-Leak Review"),
        )
        self.run_id = self._ingest(conn, report_fixture.qbo_objects())
        conn.commit()
        conn.close()

        self.client = TestClient(service.app, base_url="http://testserver")
        self.csrf = self._login(self.client, "owner@example.invalid")

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def _configure(self, renderer):
        admin.configure(
            db_factory=service._db,
            uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30,
            retention_unclosed=90,
            cookie_secure=False,
            pdf_renderer=renderer,
        )

    def _login(self, client, email):
        response = client.post(
            "/admin/login",
            data={"email": email, "password": "local-test-password-only", "next": "/admin"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        conn = service._db()
        row = conn.execute(
            "SELECT csrf_token FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE u.email=? ORDER BY s.created_at DESC LIMIT 1", (email,),
        ).fetchone()
        conn.close()
        return row[0]

    def _ingest(self, conn, objects, *, engagement_id=None):
        run_id = crm.new_id("bkr")
        conn.execute(
            "INSERT INTO bookkeeping_runs"
            "(id,organization_id,engagement_id,period_start,period_end,status) "
            "VALUES(?,?,?,?,?,'review')",
            (run_id, self.organization_id, engagement_id or self.engagement_id,
             "2026-01-01", "2026-08-31"),
        )
        work_engine.persist_canonical(conn, run_id, self.organization_id, objects)
        return run_id

    def _download(self, *, engagement_id=None, run_id=None, client=None, csrf=None):
        return (client or self.client).post(
            f"/admin/engagements/{engagement_id or self.engagement_id}/bookkeeping/"
            f"{run_id or self.run_id}/cash-leak-review.pdf",
            data={"csrf_token": csrf or self.csrf},
        )

    def test_owner_download_is_pdf_no_store_and_audited(self):
        response = self._download()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-1.7\n% test renderer\n")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn('filename="cash-leak-review.pdf"',
                      response.headers["content-disposition"])
        self.assertIn("PDF Test Contractor", self.rendered_html[-1])
        self.assertNotIn("Demonstration using synthetic company data", self.rendered_html[-1])
        conn = service._db()
        events = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='bookkeeping.report.download' "
            "AND entity_id=?", (self.run_id,),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(events, 1)
        self.assertEqual(list(Path(self.temp.name).rglob("*.pdf")), [])

    def test_anonymous_and_viewer_access_are_rejected(self):
        anonymous = TestClient(service.app, base_url="http://testserver")
        response = self._download(client=anonymous, csrf="not-a-session-token")
        self.assertEqual(response.status_code, 401)
        anonymous.close()

        viewer = TestClient(service.app, base_url="http://testserver")
        viewer_csrf = self._login(viewer, "viewer@example.invalid")
        response = self._download(client=viewer, csrf=viewer_csrf)
        self.assertEqual(response.status_code, 403)
        viewer.close()

    def test_run_must_belong_to_the_engagement_in_the_url(self):
        conn = service._db()
        other_engagement = crm.new_id("eng")
        conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status) "
            "VALUES(?,?,?,'internal_review')",
            (other_engagement, self.organization_id, "Different review"),
        )
        conn.commit()
        conn.close()
        response = self._download(engagement_id=other_engagement)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.rendered_html, [])

    def test_missing_provider_field_is_named_instead_of_fabricated(self):
        objects = copy.deepcopy(report_fixture.qbo_objects())
        for payment in objects["Payment"]:
            payment.pop("UnappliedAmt", None)
        conn = service._db()
        run_id = self._ingest(conn, objects)
        conn.commit()
        conn.close()

        response = self._download(run_id=run_id)
        self.assertEqual(response.status_code, 200)
        html = self.rendered_html[-1]
        self.assertIn("not available", html)
        self.assertIn("Payment.UnappliedAmt", html)
        self.assertNotIn("None", html)

    def test_empty_run_is_rejected_before_rendering(self):
        conn = service._db()
        run_id = crm.new_id("bkr")
        conn.execute(
            "INSERT INTO bookkeeping_runs"
            "(id,organization_id,engagement_id,period_start,period_end,status) "
            "VALUES(?,?,?,?,?,'review')",
            (run_id, self.organization_id, self.engagement_id,
             "2026-01-01", "2026-08-31"),
        )
        conn.commit()
        conn.close()
        response = self._download(run_id=run_id)
        self.assertEqual(response.status_code, 409)
        self.assertIn("no persisted bookkeeping transactions", response.text)

    def test_renderer_failure_is_honest_and_not_audited(self):
        def broken_renderer(_html):
            raise RuntimeError("synthetic renderer failure")

        self._configure(broken_renderer)
        response = self._download()
        self.assertEqual(response.status_code, 502)
        self.assertIn("could not be rendered", response.text)
        conn = service._db()
        events = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='bookkeeping.report.download' "
            "AND entity_id=?", (self.run_id,),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(events, 0)

    def test_retention_purge_removes_source_run_and_leaves_no_pdf(self):
        self.assertEqual(self._download().status_code, 200)
        conn = service._db()
        conn.execute(
            "UPDATE engagements SET status='closed',closed_at='2026-01-01 00:00:00' "
            "WHERE id=?", (self.engagement_id,),
        )
        conn.commit()
        conn.close()
        removed = service.purge_expired(datetime(2026, 3, 1, tzinfo=timezone.utc))
        self.assertTrue(any(item.get("bookkeeping_runs") == 1 for item in removed), removed)
        self.assertEqual(self._download().status_code, 404)
        self.assertEqual(list(Path(self.temp.name).rglob("*.pdf")), [])


if __name__ == "__main__":
    unittest.main()
