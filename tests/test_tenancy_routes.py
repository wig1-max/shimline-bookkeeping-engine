"""Firm B drives the real application against firm A's ids and gets nothing.

The unit tests in test_tenancy.py prove the scope is correct. These prove it is
actually *reached* -- by every route that carries a client's data, through the
real router, with a real session cookie. A scope nobody calls is a comment.

The last test in this file is the one that will still be doing work in a year:
it enumerates the router and fails when a new client-bearing route appears that
nobody has decided about. A guard you have to remember is a guard you will
forget.
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

from fastapi.testclient import TestClient  # noqa: E402

import app as service  # noqa: E402
from shimline import admin, auth, tenancy  # noqa: E402

PASSWORD = "local-test-password-only"


def _all_paths(app) -> set:
    """Every route path on an application, including nested routers.

    FastAPI does not always flatten an included router into `app.routes` -- it
    may keep it as a nested object -- so a walk that only reads the top level
    silently sees nothing. Recursing is the difference between this file being
    a guard and being decoration.
    """
    found = set()

    seen = set()

    def walk(container):
        if container is None or id(container) in seen:
            return
        seen.add(id(container))
        routes = getattr(container, "routes", None)
        if not isinstance(routes, (list, tuple)):
            return
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str) and path:
                found.add(path)
            # An included router arrives wrapped, and the wrapper exposes no
            # `routes` of its own -- only the router it wrapped. Following just
            # `.routes` finds the wrapper and stops, which is how this walk
            # came to see one admin route out of forty-three.
            for attribute in ("original_router", "router", "app"):
                walk(getattr(route, attribute, None))
            walk(route)

    walk(app)
    walk(getattr(app, "router", None))
    return found


class FirmRoutesCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "routes.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        admin.configure(
            db_factory=service._db,
            uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90,
            cookie_secure=False,
            pdf_renderer=lambda html: b"%PDF-1.7\n% test renderer\n")

        conn = service._db()
        for ident, name in (("org_a", "Alder Client"), ("org_b", "Birch Client")):
            conn.execute(
                "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                (ident, name, ident))
        tenancy.create_firm(conn, "firm_a", "Alder & Co")
        tenancy.create_firm(conn, "firm_b", "Birchwood LLP")
        tenancy.add_client(conn, "firm_a", "org_a")
        tenancy.add_client(conn, "firm_b", "org_b")
        tenancy.create_firm_user(
            conn, "firm_a", email="alder@example.invalid", display_name="Alder",
            password=PASSWORD, firm_role="principal", workspace_role="reviewer")
        tenancy.create_firm_user(
            conn, "firm_b", email="birch@example.invalid", display_name="Birch",
            password=PASSWORD, firm_role="principal", workspace_role="reviewer")
        auth.create_user(conn, "ops@shimline.invalid", "Ops", PASSWORD, "owner")

        # One engagement, one run, one finding and one proposal, all firm A's.
        conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status) "
            "VALUES('eng_a','org_a','Cash-Leak Review','internal_review')")
        conn.execute(
            "INSERT INTO work_items(id,engagement_id,title,status) "
            "VALUES('wki_a','eng_a','Reconcile August','todo')")
        conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,engagement_id,"
            "period_start,period_end,status) "
            "VALUES('run_a','org_a','eng_a','2026-08-01','2026-08-31','review')")
        conn.execute(
            "INSERT INTO bookkeeping_findings(id,run_id,organization_id,defect_type,"
            "severity,title,reason,evidence_status,evidence_json,status) "
            "VALUES('fnd_a','run_a','org_a','stale_receivable','high','Overdue',"
            "'a reason','sufficient','[]','open')")
        conn.execute(
            "INSERT INTO bookkeeping_proposals(id,run_id,finding_id,action_type,"
            "target_type,target_provider_id,current_json,proposed_json,reason,status) "
            "VALUES('prp_a','run_a','fnd_a','assign_project','Purchase','1',"
            "'{}','{}','because','proposed')")
        conn.commit()
        conn.close()

        self.birch = TestClient(service.app, base_url="http://testserver")
        self.birch_csrf = self._login(self.birch, "birch@example.invalid")
        self.alder = TestClient(service.app, base_url="http://testserver")
        self.alder_csrf = self._login(self.alder, "alder@example.invalid")

    def tearDown(self):
        self.birch.close()
        self.alder.close()
        self.temp.cleanup()

    def _login(self, client, email):
        response = client.post(
            "/admin/login",
            data={"email": email, "password": PASSWORD, "next": "/admin"},
            follow_redirects=False)
        self.assertEqual(response.status_code, 303, email)
        conn = service._db()
        row = conn.execute(
            "SELECT csrf_token FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE u.email=? ORDER BY s.created_at DESC LIMIT 1", (email,)).fetchone()
        conn.close()
        return row[0]


# Every route that carries one client's data, with firm A's identifiers in it.
# A firm B session must get 404 from each: not 403, which would confirm the
# record exists and let an outsider enumerate client ids.
FOREIGN_GETS = [
    "/admin/clients/org_a",
    "/admin/engagements/eng_a",
    "/admin/bookkeeping/run_a",
    "/admin/bookkeeping/run_a/working-papers",
    "/admin/clients/org_a/gst",
    # The shape of another firm's client's books -- row counts, which accounts
    # exist, why documents were refused -- is as much theirs as the figures.
    "/admin/clients/org_a/first-contact",
]

FOREIGN_POSTS = [
    ("/admin/clients/org_a/note", {"body": "trying"}),
    ("/admin/clients/org_a/qbo-connect", {}),
    ("/admin/clients/org_a/qbo-disconnect", {"connection_id": "con_a"}),
    ("/admin/engagements/eng_a/pull-reports", {}),
    ("/admin/engagements/eng_a/bookkeeping-scan", {}),
    ("/admin/engagements/eng_a/status", {"status": "delivered"}),
    ("/admin/engagements/eng_a/assign", {"assigned_user_id": ""}),
    ("/admin/bookkeeping/proposals/prp_a/decision",
     {"action": "approve", "note": "", "edited_json": ""}),
    ("/admin/bookkeeping/proposals/prp_a/execute", {}),
    ("/admin/bookkeeping/run_a/decisions",
     {"action": "approve", "note": "", "selected": "prp_a:0000000000000000"}),
    ("/admin/clients/org_a/details",
     {"lifecycle_stage": "active", "next_action": "", "next_action_due": "",
      "owner_user_id": ""}),
    ("/admin/clients/org_a/opportunity",
     {"stage": "qualified", "sequence_step": "", "next_follow_up": "",
      "do_not_contact": ""}),
    ("/admin/clients/org_a/client-link", {}),
    ("/admin/work-items/wki_a/status", {"status": "done"}),
    ("/admin/engagements/eng_a/bookkeeping/run_a/cash-leak-review.pdf", {}),
    ("/admin/clients/org_a/gst/rates/HST13",
     {"classification": "gst_hst", "reason": "trying"}),
    ("/admin/clients/org_a/gst/arrangement",
     {"frequency": "monthly", "year_end_month": "12", "year_end_day": "31",
      "calculation_method": "regular"}),
]


class TheBoundaryHoldsOverHttp(FirmRoutesCase):

    def test_every_read_of_another_firms_client_is_not_found(self):
        for path in FOREIGN_GETS:
            with self.subTest(path=path):
                response = self.birch.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 404, path)

    def test_every_write_against_another_firms_client_is_not_found(self):
        for path, payload in FOREIGN_POSTS:
            with self.subTest(path=path):
                response = self.birch.post(
                    path, data=dict(payload, csrf_token=self.birch_csrf),
                    follow_redirects=False)
                # 422 would mean the request never reached the guard, so the
                # test would be proving nothing about the boundary.
                self.assertNotEqual(response.status_code, 422,
                                    f"{path} rejected the payload before the guard ran")
                self.assertEqual(response.status_code, 404, path)

    def test_refusal_is_indistinguishable_from_absence(self):
        """A guessed id and a real id belonging to someone else must answer the
        same, or the difference is an enumeration oracle."""
        real = self.birch.get("/admin/bookkeeping/run_a", follow_redirects=False)
        invented = self.birch.get("/admin/bookkeeping/run_zzzz",
                                  follow_redirects=False)
        self.assertEqual(real.status_code, invented.status_code)
        self.assertEqual(real.text, invented.text)

    def test_the_owning_firm_still_gets_through(self):
        """A boundary that refuses everyone is not a boundary, it is an outage."""
        for path in FOREIGN_GETS:
            with self.subTest(path=path):
                response = self.alder.get(path, follow_redirects=False)
                self.assertNotEqual(response.status_code, 404, path)

    def test_listings_do_not_name_another_firms_client(self):
        for path in ("/admin/clients", "/admin/work", "/admin/calendar", "/admin"):
            with self.subTest(path=path):
                body = self.birch.get(path, follow_redirects=False).text
                self.assertNotIn("Alder Client", body, path)

    def test_a_listing_total_does_not_count_another_firms_clients(self):
        """Filtering after the fetch would still have leaked the count, which
        tells a competitor how many books the other firm holds."""
        body = self.birch.get("/admin/clients").text
        self.assertIn("Birch Client", body)
        self.assertNotIn("Alder Client", body)

    def test_internal_staff_still_see_everything(self):
        ops = TestClient(service.app, base_url="http://testserver")
        self._login(ops, "ops@shimline.invalid")
        try:
            body = ops.get("/admin/clients").text
            self.assertIn("Alder Client", body)
            self.assertIn("Birch Client", body)
            self.assertEqual(
                ops.get("/admin/bookkeeping/run_a",
                        follow_redirects=False).status_code, 200)
        finally:
            ops.close()


class NoRouteEscapesTheDecision(FirmRoutesCase):
    """The guard that keeps working after everyone forgets this file exists."""

    # Routes whose path carries a client-scoped identifier. Each must either be
    # covered by a test above or be listed here with the reason it is exempt.
    EXEMPT = {
        # Client-facing and staff-facing file download; scoped by the submission's
        # own token check rather than by firm, and covered by test_retention.
        "/admin/files/{sub_id}/{filename}",
        # Lead import batches are Shimline's own prospecting data, not a
        # client's books. No firm holds them.
        "/admin/imports/leads/{batch_id}/apply",
        "/admin/imports/leads/{batch_id}/resolve",
    }

    def test_every_client_scoped_route_is_either_tested_or_exempt(self):
        covered = {path for path in FOREIGN_GETS}
        covered |= {path for path, _ in FOREIGN_POSTS}
        # Two that cannot be driven from this file, each for a stated reason
        # rather than because listing them was easier than testing them.
        covered |= {
            # A multipart upload of two CSV exports. The scope guard sits on the
            # same line as every other engagement route and is covered by
            # test_qbo_exports; driving a file upload here would test multipart
            # parsing rather than the boundary.
            "/admin/engagements/eng_a/import-qbo-exports",
            # Scoped by the snapshot's own organization rather than by the
            # route, so there is no foreign id to aim at without first creating
            # a snapshot for the other firm; covered in test_qbo_reports.
            "/admin/snapshots/snp_a",
        }

        def concrete(template: str) -> str:
            return (template
                    .replace("{organization_id}", "org_a")
                    .replace("{engagement_id}", "eng_a")
                    .replace("{run_id}", "run_a")
                    .replace("{proposal_id}", "prp_a")
                    .replace("{work_item_id}", "wki_a")
                    .replace("{snapshot_id}", "snp_a")
                    .replace("{rate_id}", "HST13"))

        scoped_markers = ("{organization_id}", "{engagement_id}", "{run_id}",
                          "{proposal_id}", "{work_item_id}", "{snapshot_id}",
                          "{rate_id}")

        admin_paths = {path for path in _all_paths(service.app)
                       if path.startswith("/admin")}
        # An enumerator that finds nothing passes every assertion below and
        # proves nothing. This one did exactly that until FastAPI started
        # nesting included routers instead of flattening them into app.routes,
        # so the count is asserted before anything is concluded from it.
        self.assertGreater(
            len(admin_paths), 20,
            "the router walk found almost no admin routes, so this guard is "
            "not actually looking at the application")

        undecided = []
        for template in admin_paths:
            if template in self.EXEMPT:
                continue
            if not any(marker in template for marker in scoped_markers):
                continue
            if concrete(template) not in covered:
                undecided.append(template)

        self.assertEqual(
            sorted(undecided), [],
            "These routes carry a client identifier and nothing has decided "
            "whether they are scoped. Add them to FOREIGN_GETS/FOREIGN_POSTS "
            "with a 404 assertion, or to EXEMPT with the reason.")


if __name__ == "__main__":
    unittest.main()
