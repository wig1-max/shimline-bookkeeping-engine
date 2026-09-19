"""Meaningful admin workspace tests using only disposable data."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app as service
from shimline import auth, crm
from test_qbo_exports import AR, LEDGER
from shimline import admin as admin_workspace


class AdminCrmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "intake.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        admin_workspace.configure(
            db_factory=service._db, uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90, cookie_secure=False,
        )
        conn = service._db()
        self.user_id = auth.create_user(
            conn, "admin@example.invalid", "Local Admin", "local-test-password-only", "owner"
        )
        conn.close()
        self.client = TestClient(service.app)
        response = self.client.post("/admin/login", data={
            "email": "admin@example.invalid", "password": "local-test-password-only", "next": "/admin"
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        conn = service._db()
        self.csrf = conn.execute("SELECT csrf_token FROM sessions").fetchone()[0]
        conn.close()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def _create_engagement(self):
        conn = service._db()
        organization_id = crm.new_id("org")
        engagement_id = crm.new_id("eng")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage) VALUES(?,?,?,'active')",
            (organization_id, "Northstar Renovations", "northstarrenovations"),
        )
        conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status,assigned_user_id) "
            "VALUES(?,?,'Cash-Leak Review','awaiting_client',?)",
            (engagement_id, organization_id, self.user_id),
        )
        conn.commit()
        conn.close()
        return organization_id, engagement_id

    def test_session_cookie_and_csrf_protect_changes(self):
        _, engagement_id = self._create_engagement()
        cookie = self.client.cookies.get(auth.COOKIE_NAME)
        self.assertTrue(cookie)
        rejected = self.client.post(
            f"/admin/engagements/{engagement_id}/status", data={"status": "ready", "csrf_token": "bad"}
        )
        self.assertEqual(rejected.status_code, 403)
        accepted = self.client.post(
            f"/admin/engagements/{engagement_id}/status",
            data={"status": "ready", "csrf_token": self.csrf}, follow_redirects=False,
        )
        self.assertEqual(accepted.status_code, 303)
        conn = service._db()
        row = conn.execute("SELECT status,ready_at,due_at FROM engagements WHERE id=?", (engagement_id,)).fetchone()
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='engagement.status'").fetchone()[0]
        conn.close()
        self.assertEqual(row[0], "ready")
        self.assertIsNotNone(row[1])
        self.assertIsNotNone(row[2])
        due = crm.parse_timestamp(row[2])
        ready = crm.parse_timestamp(row[1])
        self.assertGreater(due, ready)
        self.assertEqual(audit_count, 1)

    def test_lead_import_merges_both_sources_and_is_idempotent(self):
        root = Path(__file__).resolve().parents[1]
        tracker_path = root / "crm" / "leads_tracker.csv"
        research_path = root / "leads" / "canada_leads_batch1.csv"
        if not tracker_path.is_file() or not research_path.is_file():
            self.skipTest("private lead fixtures are intentionally absent from this checkout")
        tracker = tracker_path.read_bytes()
        research = research_path.read_bytes()
        conn = service._db()
        preview = crm.preview_lead_import(conn, tracker, research, self.user_id)
        self.assertEqual(preview["source_rows"], 32)
        self.assertEqual(preview["organizations"], 16)
        self.assertEqual(preview["counts"]["create"], 16)
        crm.apply_lead_import(conn, preview["batch_id"], self.user_id)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0], 16)
        vibe = conn.execute(
            "SELECT o.fit_tier,o.website_url,o.personalization_fact,c.name,c.phone,p.outreach_route "
            "FROM organizations o JOIN contacts c ON c.organization_id=o.id "
            "JOIN opportunities p ON p.organization_id=o.id WHERE o.normalized_name=?",
            (crm.normalize_company("Vibe Design Build"),),
        ).fetchone()
        self.assertEqual(vibe["fit_tier"], "A")
        self.assertEqual(vibe["name"], "Blair Goodman")
        self.assertEqual(vibe["phone"], "604-833-4500")
        self.assertEqual(vibe["outreach_route"], "phone")
        self.assertIn("Buildertrend", vibe["personalization_fact"])
        again = crm.preview_lead_import(conn, tracker, research, self.user_id)
        self.assertTrue(again["already_seen"])
        crm.apply_lead_import(conn, again["batch_id"], self.user_id)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0], 16)
        conn.close()

    @staticmethod
    def lead_csv(company, *, city="Toronto", specialty="renovation", route="Email",
                 tier="A", contact="Dana", email="dana@grans.invalid"):
        """One well-formed lead row, so a test names only what it varies."""
        header = "company,city,specialty,contact_route,fit_tier,contact_name,email\n"
        row = ",".join((company, city, specialty, route, tier, contact, email)) + "\n"
        return (header + row).encode()

    def test_a_corrected_company_name_is_a_conflict_not_a_second_organization(self):
        """The real failure: research corrects a name already in the CRM.

        "Granstone" became "Granstone Renovations" after better research.
        `normalize_company` sees two companies, so importing as-is created a
        duplicate -- one holding the contacts, one holding none, both looking
        like legitimate leads. Nothing about a duplicate announces itself, so
        the import has to stop and ask.
        """
        conn = service._db()
        first = crm.preview_lead_import(
            conn, self.lead_csv("Granstone"), b"company\n", self.user_id)
        crm.apply_lead_import(conn, first["batch_id"], self.user_id)
        original = conn.execute("SELECT id FROM organizations").fetchone()[0]

        corrected = crm.preview_lead_import(
            conn, self.lead_csv("Granstone Renovations"), b"company\n", self.user_id)
        self.assertEqual(corrected["counts"]["conflict"], 1)
        self.assertEqual(corrected["counts"]["create"], 0)
        # Nothing applicable means the Apply button cannot write the duplicate.
        self.assertEqual(corrected["applicable"], 0)
        row = crm.get_import_preview(conn, corrected["batch_id"])["rows"][0]
        self.assertEqual(row["candidate"]["id"], original)
        self.assertEqual(row["candidate"]["name"], "Granstone")

        # Applying before the question is answered must not create anything.
        crm.apply_lead_import(conn, corrected["batch_id"], self.user_id)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0], 1)
        conn.close()

    def test_answering_the_conflict_once_merges_and_is_remembered(self):
        conn = service._db()
        first = crm.preview_lead_import(
            conn, self.lead_csv("Granstone"), b"company\n", self.user_id)
        crm.apply_lead_import(conn, first["batch_id"], self.user_id)
        original = conn.execute("SELECT id FROM organizations").fetchone()[0]

        corrected_csv = self.lead_csv("Granstone Renovations", route="Phone")
        corrected = crm.preview_lead_import(conn, corrected_csv, b"company\n", self.user_id)
        key = crm.normalize_company("Granstone Renovations")
        outcome = crm.resolve_import_conflict(
            conn, corrected["batch_id"], key,
            same_as_organization_id=original, user_id=self.user_id)
        self.assertEqual(outcome["action"], "merge")
        self.assertEqual(
            crm.get_import_preview(conn, corrected["batch_id"])["applicable"], 1)
        crm.apply_lead_import(conn, corrected["batch_id"], self.user_id)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0], 1)

        # Asked once: a later import of the corrected spelling merges outright.
        later = crm.preview_lead_import(
            conn, corrected_csv, b"company,notes\nGranstone Renovations,second pass\n",
            self.user_id)
        self.assertEqual(later["counts"]["merge"], 1)
        self.assertEqual(later["counts"]["conflict"], 0)
        conn.close()

    def test_renaming_keeps_the_former_name_resolvable(self):
        conn = service._db()
        preview = crm.preview_lead_import(
            conn, self.lead_csv("Granstone"), b"company\n", self.user_id)
        crm.apply_lead_import(conn, preview["batch_id"], self.user_id)
        organization_id = conn.execute("SELECT id FROM organizations").fetchone()[0]

        crm.rename_organization(conn, organization_id, "Granstone Renovations", self.user_id)
        conn.commit()
        self.assertEqual(
            crm.find_organization(conn, crm.normalize_company("Granstone")), organization_id)
        self.assertEqual(
            crm.find_organization(conn, crm.normalize_company("Granstone Renovations")),
            organization_id)
        # A paid intake under the pre-correction name is the same client.
        linked, _ = crm.ensure_intake_engagement(
            conn, submission_id="sub_rename", company="Granstone", contact_name="Dana",
            email="dana@grans.invalid", phone="416-555-0000")
        self.assertEqual(linked, organization_id)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0], 1)
        conn.close()

    def test_a_genuinely_different_company_can_still_be_created(self):
        conn = service._db()
        first = crm.preview_lead_import(
            conn, self.lead_csv("Granstone"), b"company\n", self.user_id)
        crm.apply_lead_import(conn, first["batch_id"], self.user_id)

        second = crm.preview_lead_import(
            conn, self.lead_csv("Granstone Plumbing", city="Calgary", specialty="plumbing",
                                tier="B", contact="Ali", email="ali@gp.invalid"),
            b"company\n", self.user_id)
        self.assertEqual(second["counts"]["conflict"], 1)
        crm.resolve_import_conflict(
            conn, second["batch_id"], crm.normalize_company("Granstone Plumbing"),
            same_as_organization_id=None, user_id=self.user_id)
        crm.apply_lead_import(conn, second["batch_id"], self.user_id)
        names = sorted(row[0] for row in conn.execute("SELECT name FROM organizations"))
        self.assertEqual(names, ["Granstone", "Granstone Plumbing"])
        # Saying "different company" must not have recorded an alias.
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM organization_aliases").fetchone()[0], 0)
        conn.close()

    def test_lead_import_skips_rows_the_schema_would_refuse(self):
        """A mistyped cell must not roll back everything behind a 500.

        fit_tier and outreach_route carry CHECK constraints, so an unknown
        value raises IntegrityError inside the single apply transaction. The
        preview is the operator's review screen, so that is where a row has to
        be refused, by name and with a reason.
        """
        header = (
            "company,city,specialty,contact_route,fit_tier,contact_name,email\n")
        rows = (
            "Alpha Roofing,Ottawa,roofing,Email,a,Jo,jo@alpha.invalid\n"
            "Beta Framing,Ottawa,framing,Contact Form,B,Sam,sam@beta.invalid\n"
            "Gamma Paving,Ottawa,paving,carrier pigeon,Z,Kim,kim@gamma.invalid\n"
        )
        conn = service._db()
        preview = crm.preview_lead_import(
            conn, (header + rows).encode(), b"company\n", self.user_id)
        self.assertEqual(preview["counts"]["create"], 2)
        self.assertEqual(preview["counts"]["skip"], 1)
        self.assertEqual(preview["applicable"], 2)

        refused = [row for row in crm.get_import_preview(conn, preview["batch_id"])["rows"]
                   if row["action"] == "skip"]
        self.assertEqual([row["company"] for row in refused], ["Gamma Paving"])
        self.assertEqual(len(refused[0]["problems"]), 2)
        self.assertTrue(any("carrier pigeon" in problem for problem in refused[0]["problems"]))

        crm.apply_lead_import(conn, preview["batch_id"], self.user_id)
        stored = dict(conn.execute(
            "SELECT o.name,o.fit_tier FROM organizations o ORDER BY o.name").fetchall())
        self.assertEqual(stored, {"Alpha Roofing": "A", "Beta Framing": "B"})
        # Case and spacing are the operator's spelling, not a different value.
        routes = dict(conn.execute(
            "SELECT o.name,p.outreach_route FROM organizations o "
            "JOIN opportunities p ON p.organization_id=o.id ORDER BY o.name").fetchall())
        self.assertEqual(routes, {"Alpha Roofing": "email", "Beta Framing": "contact_form"})
        conn.close()

    def test_operator_can_open_review_from_customer_qbo_exports(self):
        organization_id, engagement_id = self._create_engagement()
        response = self.client.post(
            f"/admin/engagements/{engagement_id}/import-qbo-exports",
            data={"csrf_token": self.csrf},
            files={
                "transaction_detail_file": ("Transaction Detail.csv", LEDGER, "text/csv"),
                "ar_aging_file": ("AR Aging Detail.csv", AR, "text/csv"),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertRegex(response.headers["location"], r"^/admin/bookkeeping/bkr_")
        conn = service._db()
        run = conn.execute(
            "SELECT id,connection_id,evidence_json FROM bookkeeping_runs WHERE engagement_id=?",
            (engagement_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT summary FROM audit_events WHERE action='bookkeeping.exports.import'"
        ).fetchone()
        transaction_count = conn.execute(
            "SELECT COUNT(*) FROM bookkeeping_transactions WHERE run_id=?", (run["id"],)
        ).fetchone()[0]
        conn.close()
        self.assertIsNone(run["connection_id"])
        self.assertIn("qbo_transaction_detail_csv", run["evidence_json"])
        self.assertEqual(transaction_count, 5)
        self.assertIn("5 transaction rows", event["summary"])

    def test_export_import_rejects_summary_files_without_creating_a_run(self):
        _, engagement_id = self._create_engagement()
        response = self.client.post(
            f"/admin/engagements/{engagement_id}/import-qbo-exports",
            data={"csrf_token": self.csrf},
            files={
                "transaction_detail_file": ("Profit and Loss.csv", b"Income,100\n", "text/csv"),
                "ar_aging_file": ("AR Aging Detail.csv", AR, "text/csv"),
            },
        )
        self.assertEqual(response.status_code, 400)
        conn = service._db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM bookkeeping_runs").fetchone()[0], 0)
        conn.close()

    def test_internal_do_not_contact_flag_is_persisted(self):
        organization_id, _ = self._create_engagement()
        conn = service._db()
        conn.execute(
            "INSERT INTO opportunities(id,organization_id,stage) VALUES(?,?,'new')",
            (crm.new_id("opp"), organization_id),
        )
        conn.commit()
        conn.close()
        response = self.client.post(
            f"/admin/clients/{organization_id}/opportunity",
            data={
                "csrf_token": self.csrf, "stage": "lost", "sequence_step": "declined",
                "do_not_contact": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        conn = service._db()
        opportunity = conn.execute(
            "SELECT stage,do_not_contact FROM opportunities WHERE organization_id=?",
            (organization_id,),
        ).fetchone()
        audit = conn.execute(
            "SELECT summary FROM audit_events WHERE action='opportunity.update'"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(opportunity), ("lost", 1))
        self.assertIn("do-not-contact", audit["summary"])

    def test_login_throttling_and_no_cache_headers(self):
        anonymous = TestClient(service.app)
        for _ in range(auth.MAX_FAILED_ATTEMPTS):
            response = anonymous.post("/admin/login", data={"email": "nobody@example.invalid", "password": "wrong"})
            self.assertEqual(response.status_code, 401)
        limited = anonymous.post("/admin/login", data={"email": "nobody@example.invalid", "password": "wrong"})
        self.assertEqual(limited.status_code, 429)
        page = self.client.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        anonymous.close()


if __name__ == "__main__":
    unittest.main()
