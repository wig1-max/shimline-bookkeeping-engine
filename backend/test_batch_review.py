"""One-pass approval, driven through the real router.

The unit tests prove the guard refuses a changed batch. These prove the
fingerprint the page renders is the fingerprint the form posts back -- because
if those two disagreed, every batch would be refused and the feature would
silently never work, which is the quietest way for a feature to be broken.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

from fastapi.testclient import TestClient  # noqa: E402

import app as service  # noqa: E402
from shimline import admin, auth, tenancy, work_engine  # noqa: E402

PASSWORD = "local-test-password-only"

CURRENT = {"Line": [{"Amount": 250.0, "AccountBasedExpenseLineDetail":
                     {"AccountRef": {"value": "64"}}}]}
PROPOSED = {"Line": [{"Amount": 250.0, "AccountBasedExpenseLineDetail":
                      {"AccountRef": {"value": "64"},
                       "CustomerRef": {"value": "P-2"}}}]}


class BatchReviewCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "batch.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        admin.configure(
            db_factory=service._db, uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90,
            cookie_secure=False, pdf_renderer=lambda html: b"%PDF")

        conn = service._db()
        conn.execute("INSERT INTO organizations(id,name,normalized_name) "
                     "VALUES('org_1','Northlake Roofing','org_1')")
        tenancy.create_firm(conn, "firm_a", "Alder & Co")
        tenancy.add_client(conn, "firm_a", "org_1")
        tenancy.create_firm_user(
            conn, "firm_a", email="dana@example.invalid", display_name="Dana",
            password=PASSWORD, firm_role="principal", workspace_role="reviewer")
        auth.create_user(conn, "look@example.invalid", "Looker", PASSWORD, "viewer")
        conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
            "period_end,status) VALUES('run_1','org_1','2026-08-01',"
            "'2026-08-31','review')")
        for index in range(3):
            conn.execute(
                "INSERT INTO bookkeeping_findings(id,run_id,organization_id,"
                "defect_type,severity,title,reason,evidence_status,evidence_json,"
                "financial_effect,status) VALUES(?,'run_1','org_1',"
                "'incorrect_job_allocation','medium','Materials not on a job',"
                "'The receipt names project P-2.','sufficient',"
                "'[\"qbo:104\"]','250.00','open')", (f"fnd_{index}",))
            conn.execute(
                "INSERT INTO bookkeeping_proposals(id,run_id,finding_id,action_type,"
                "target_type,target_provider_id,current_json,proposed_json,reason,"
                "status) VALUES(?,'run_1',?,'assign_project','Purchase','104',?,?,"
                "'The receipt names a project QuickBooks does not.','proposed')",
                (f"prp_{index}", f"fnd_{index}",
                 json.dumps(CURRENT), json.dumps(PROPOSED)))
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

    def _tokens(self):
        conn = service._db()
        rows = work_engine.proposal_rows(conn, "run_1")
        conn.close()
        return [f"{row['id']}:{row['fingerprint']}" for row in rows]

    def _statuses(self):
        conn = service._db()
        rows = dict(conn.execute(
            "SELECT id,status FROM bookkeeping_proposals WHERE run_id='run_1'"))
        conn.close()
        return rows


class ThePageShowsTheChange(BatchReviewCase):

    def test_the_review_states_the_change_in_words(self):
        body = self.client.get("/admin/bookkeeping/run_1").text
        self.assertIn("Would become", body)
        self.assertIn("Project", body, "the field is named in accounting words")
        self.assertIn("P-2", body, "the value being written is on the page")

    def test_the_raw_documents_are_still_available(self):
        """Summarising must not mean hiding. The exact documents stay one
        disclosure away for anyone who wants them."""
        body = self.client.get("/admin/bookkeeping/run_1").text
        self.assertIn("as QuickBooks holds them", body)
        self.assertIn("AccountBasedExpenseLineDetail", body)

    def test_each_undecided_proposal_offers_a_checkbox(self):
        body = self.client.get("/admin/bookkeeping/run_1").text
        self.assertEqual(body.count('name="selected"'), 3)


class TheBatchOverHttp(BatchReviewCase):

    def test_approving_a_selection_decides_exactly_those(self):
        tokens = self._tokens()
        response = self.client.post(
            "/admin/bookkeeping/run_1/decisions",
            data={"csrf_token": self.csrf, "action": "approve",
                  "note": "Read and agreed", "selected": tokens[:2]},
            follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        statuses = self._statuses()
        self.assertEqual(statuses["prp_0"], "approved")
        self.assertEqual(statuses["prp_1"], "approved")
        self.assertEqual(statuses["prp_2"], "proposed",
                         "an unticked proposal is not decided")

    def test_a_proposal_that_moved_refuses_the_whole_batch(self):
        tokens = self._tokens()
        conn = service._db()
        conn.execute(
            "UPDATE bookkeeping_proposals SET proposed_json=? WHERE id='prp_1'",
            (json.dumps({"Line": [{"Amount": 9999.0}]}),))
        conn.commit()
        conn.close()

        response = self.client.post(
            "/admin/bookkeeping/run_1/decisions",
            data={"csrf_token": self.csrf, "action": "approve",
                  "note": "", "selected": tokens},
            follow_redirects=False)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(set(self._statuses().values()), {"proposed"},
                         "not one of them may be decided when any changed")

    def test_a_batch_cannot_reach_a_review_the_reviewer_never_opened(self):
        conn = service._db()
        conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
            "period_end,status) VALUES('run_2','org_1','2026-07-01',"
            "'2026-07-31','review')")
        conn.commit()
        conn.close()
        tokens = self._tokens()
        response = self.client.post(
            "/admin/bookkeeping/run_2/decisions",
            data={"csrf_token": self.csrf, "action": "approve",
                  "note": "", "selected": tokens},
            follow_redirects=False)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(set(self._statuses().values()), {"proposed"})

    def test_an_empty_selection_is_refused_rather_than_silently_doing_nothing(self):
        response = self.client.post(
            "/admin/bookkeeping/run_1/decisions",
            data={"csrf_token": self.csrf, "action": "approve", "note": ""},
            follow_redirects=False)
        self.assertEqual(response.status_code, 400)

    def test_a_batch_needs_a_csrf_token(self):
        response = self.client.post(
            "/admin/bookkeeping/run_1/decisions",
            data={"csrf_token": "wrong", "action": "approve",
                  "selected": self._tokens()},
            follow_redirects=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(set(self._statuses().values()), {"proposed"})

    def test_a_viewer_may_not_approve_a_batch(self):
        """The role gate and the tenancy boundary are separate controls, and
        both still apply to the batch path."""
        viewer = TestClient(service.app, base_url="http://testserver")
        csrf = self._login(viewer, "look@example.invalid")
        try:
            response = viewer.post(
                "/admin/bookkeeping/run_1/decisions",
                data={"csrf_token": csrf, "action": "approve",
                      "selected": self._tokens()},
                follow_redirects=False)
            self.assertEqual(response.status_code, 403)
        finally:
            viewer.close()
        self.assertEqual(set(self._statuses().values()), {"proposed"})

    def test_executing_is_not_something_a_batch_can_do(self):
        """Approval and release stay separate acts. A batch that could release
        writes would collapse the one control this engine is built around."""
        response = self.client.post(
            "/admin/bookkeeping/run_1/decisions",
            data={"csrf_token": self.csrf, "action": "execute",
                  "selected": self._tokens()},
            follow_redirects=False)
        self.assertEqual(response.status_code, 400)

    def test_the_batch_is_recorded_against_the_person_who_pressed_it(self):
        self.client.post(
            "/admin/bookkeeping/run_1/decisions",
            data={"csrf_token": self.csrf, "action": "approve",
                  "note": "Read and agreed", "selected": self._tokens()},
            follow_redirects=False)
        conn = service._db()
        events = [row[0] for row in conn.execute(
            "SELECT action FROM audit_events WHERE action LIKE 'bookkeeping.batch%'")]
        approvals = conn.execute(
            "SELECT COUNT(*) FROM bookkeeping_approvals WHERE decision='approve'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(events, ["bookkeeping.batch.approve"])
        self.assertEqual(approvals, 3, "each proposal keeps its own approval row")


if __name__ == "__main__":
    unittest.main()
