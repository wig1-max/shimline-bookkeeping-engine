"""Operator-facing workspace behaviour: access, ownership, money, retention, paging."""
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app as service
from shimline import admin as admin_workspace
from shimline import auth, clock, crm, icons


class WorkspaceTests(unittest.TestCase):
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
        signed_in = self.client.post("/admin/login", data={
            "email": "admin@example.invalid", "password": "local-test-password-only", "next": "/admin",
        }, follow_redirects=False)
        self.assertEqual(signed_in.status_code, 303)
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
            "INSERT INTO opportunities(id,organization_id,stage,service_type) "
            "VALUES(?,?,'qualified','cash_leak_review')",
            (crm.new_id("opp"), organization_id),
        )
        conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status) "
            "VALUES(?,?,'Cash-Leak Review','awaiting_client')",
            (engagement_id, organization_id),
        )
        conn.commit()
        conn.close()
        return organization_id, engagement_id

    # ------------------------------------------------------------- access --

    def test_every_workspace_route_refuses_an_anonymous_visitor(self):
        organization_id, engagement_id = self._create_engagement()
        anonymous = TestClient(service.app, follow_redirects=False)
        pages = [
            "/admin", "/admin/pipeline", "/admin/clients", "/admin/work",
            "/admin/calendar", "/admin/audit", "/admin/imports/leads",
            f"/admin/clients/{organization_id}", f"/admin/engagements/{engagement_id}",
        ]
        for path in pages:
            response = anonymous.get(path)
            self.assertEqual(response.status_code, 303, path)
            self.assertTrue(response.headers["location"].startswith("/admin/login"), path)
        # A signed-out visitor must not be able to pull a client's documents.
        self.assertEqual(anonymous.get("/admin/files/abcdef123456/anything.csv").status_code, 401)
        anonymous.close()

    def test_csp_allows_the_quickbooks_authorization_redirect(self):
        """`form-action` is applied to redirects that follow a form submission,
        so omitting Intuit's host makes Connect QuickBooks a dead button with
        no error anywhere — the failure this test exists to prevent."""
        csp = self.client.get("/admin").headers["content-security-policy"]
        directive = next(d.strip() for d in csp.split(";") if d.strip().startswith("form-action"))
        self.assertIn("'self'", directive)
        self.assertIn("https://appcenter.intuit.com", directive)
        # Everything else stays locked down.
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("default-src 'self'", csp)

    def test_every_page_renders_for_a_signed_in_operator(self):
        organization_id, engagement_id = self._create_engagement()
        for path in ["/admin", "/admin/pipeline", "/admin/clients", "/admin/work", "/admin/calendar",
                     "/admin/audit", "/admin/imports/leads", f"/admin/clients/{organization_id}",
                     f"/admin/engagements/{engagement_id}"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)

    def test_pipeline_filters_return_an_htmx_partial_with_full_page_fallback(self):
        self._create_engagement()
        full = self.client.get("/admin/pipeline?q=Northstar")
        partial = self.client.get(
            "/admin/pipeline?q=Northstar", headers={"HX-Request": "true"}
        )
        self.assertIn("<html", full.text)
        self.assertIn('id="pipeline-results"', full.text)
        self.assertNotIn("<html", partial.text)
        self.assertIn("Northstar Renovations", partial.text)
        self.assertEqual(partial.headers["vary"], "HX-Request")

    def test_today_chart_has_a_visible_aggregate_table_equivalent(self):
        self._create_engagement()
        page = self.client.get("/admin")
        self.assertIn('id="serviceRiskChart"', page.text)
        self.assertIn("Waiting on client", page.text)
        self.assertIn("Internal review", page.text)
        self.assertIn('aria-hidden="true"', page.text)

    # -------------------------------------------------------- ownership --

    def test_assignment_stage_and_next_action_are_saved_and_audited(self):
        organization_id, engagement_id = self._create_engagement()
        assigned = self.client.post(
            f"/admin/engagements/{engagement_id}/assign",
            data={"assigned_user_id": self.user_id, "priority_reason": "Client crew starts Monday",
                  "csrf_token": self.csrf}, follow_redirects=False,
        )
        self.assertEqual(assigned.status_code, 303)
        relationship = self.client.post(
            f"/admin/clients/{organization_id}/details",
            data={"lifecycle_stage": "active", "next_action": "Send August statement request",
                  "next_action_due": "2026-09-18", "owner_user_id": self.user_id,
                  "csrf_token": self.csrf}, follow_redirects=False,
        )
        self.assertEqual(relationship.status_code, 303)
        opportunity = self.client.post(
            f"/admin/clients/{organization_id}/opportunity",
            data={"stage": "won", "sequence_step": "email 3", "next_follow_up": "",
                  "csrf_token": self.csrf}, follow_redirects=False,
        )
        self.assertEqual(opportunity.status_code, 303)

        conn = service._db()
        engagement = conn.execute(
            "SELECT assigned_user_id,priority_reason FROM engagements WHERE id=?", (engagement_id,)
        ).fetchone()
        organization = conn.execute(
            "SELECT lifecycle_stage,next_action,next_action_due_at,owner_user_id FROM organizations WHERE id=?",
            (organization_id,),
        ).fetchone()
        sold = conn.execute(
            "SELECT stage,review_sold FROM opportunities WHERE organization_id=?", (organization_id,)
        ).fetchone()
        actions = {row[0] for row in conn.execute("SELECT action FROM audit_events")}
        conn.close()

        self.assertEqual(engagement[0], self.user_id)
        self.assertEqual(engagement[1], "Client crew starts Monday")
        self.assertEqual(organization[0], "active")
        self.assertEqual(organization[1], "Send August statement request")
        self.assertEqual(organization[3], self.user_id)
        # A date entered as a Canadian business day must not drift to the day before.
        stored = clock.to_business(crm.parse_timestamp(organization[2])).date().isoformat()
        self.assertEqual(stored, "2026-09-18")
        self.assertEqual(sold[0], "won")
        self.assertEqual(sold[1], 1)
        self.assertLessEqual({"engagement.assign", "organization.update", "opportunity.update"}, actions)

    def test_unknown_owner_stage_and_malformed_date_are_rejected(self):
        organization_id, engagement_id = self._create_engagement()
        unknown_owner = self.client.post(
            f"/admin/engagements/{engagement_id}/assign",
            data={"assigned_user_id": "usr_does_not_exist", "priority_reason": "", "csrf_token": self.csrf},
        )
        self.assertEqual(unknown_owner.status_code, 400)
        unknown_stage = self.client.post(
            f"/admin/clients/{organization_id}/details",
            data={"lifecycle_stage": "invented", "next_action": "", "next_action_due": "",
                  "owner_user_id": "", "csrf_token": self.csrf},
        )
        self.assertEqual(unknown_stage.status_code, 400)
        bad_date = self.client.post(
            f"/admin/clients/{organization_id}/details",
            data={"lifecycle_stage": "active", "next_action": "", "next_action_due": "18-09-2026",
                  "owner_user_id": "", "csrf_token": self.csrf},
        )
        self.assertEqual(bad_date.status_code, 400)

    # ------------------------------------------------- money and evidence --

    def test_client_workspace_shows_payment_documents_and_deletion_date(self):
        organization_id, engagement_id = self._create_engagement()
        submission_id = "ab12cd34ef56"
        conn = service._db()
        conn.execute(
            "INSERT INTO submissions(id,company,contact_name,email,files,organization_id,engagement_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (submission_id, "Northstar Renovations", "Sam Rivera", "sam@example.invalid",
             "bank_export.csv", organization_id, engagement_id),
        )
        conn.execute(
            "INSERT INTO payments(order_id,payment_id,amount,currency,status,email,submission_id) "
            "VALUES('order_test','pay_test',19900,'CAD','paid','sam@example.invalid',?)",
            (submission_id,),
        )
        conn.execute("UPDATE engagements SET submission_id=? WHERE id=?", (submission_id, engagement_id))
        conn.commit()
        conn.close()

        page = self.client.get(f"/admin/clients/{organization_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("199.00 CAD", page.text)
        self.assertIn("bank_export.csv", page.text)
        # Never closed, so the 90-day abandoned-intake rule applies, not the 30-day one.
        self.assertIn("90 days after intake", page.text)
        self.assertIn("Files delete in", page.text)

    def test_closing_an_engagement_starts_the_deletion_clock_and_reopening_clears_it(self):
        _, engagement_id = self._create_engagement()
        submission_id = "ff11ee22dd33"
        conn = service._db()
        conn.execute("INSERT INTO submissions(id,company,files) VALUES(?,?,'export.csv')",
                     (submission_id, "Northstar Renovations"))
        conn.execute("UPDATE engagements SET submission_id=? WHERE id=?", (submission_id, engagement_id))
        conn.commit()
        conn.close()

        self.client.post(f"/admin/engagements/{engagement_id}/status",
                         data={"status": "closed", "csrf_token": self.csrf}, follow_redirects=False)
        conn = service._db()
        closed = conn.execute("SELECT closed_at FROM submissions WHERE id=?", (submission_id,)).fetchone()[0]
        conn.close()
        self.assertIsNotNone(closed)

        self.client.post(f"/admin/engagements/{engagement_id}/status",
                         data={"status": "in_progress", "csrf_token": self.csrf}, follow_redirects=False)
        conn = service._db()
        reopened = conn.execute("SELECT closed_at FROM submissions WHERE id=?", (submission_id,)).fetchone()[0]
        retention_actions = [row[0] for row in conn.execute(
            "SELECT action FROM audit_events WHERE action LIKE 'retention.%' ORDER BY created_at,rowid")]
        conn.close()
        self.assertIsNone(reopened)
        self.assertEqual(retention_actions, ["retention.start", "retention.stop"])

    def test_sla_due_date_is_five_canadian_business_days_after_ready(self):
        _, engagement_id = self._create_engagement()
        self.client.post(f"/admin/engagements/{engagement_id}/status",
                         data={"status": "ready", "csrf_token": self.csrf}, follow_redirects=False)
        conn = service._db()
        ready_at, due_at = conn.execute(
            "SELECT ready_at,due_at FROM engagements WHERE id=?", (engagement_id,)
        ).fetchone()
        conn.close()
        ready = crm.parse_timestamp(ready_at)
        due = crm.parse_timestamp(due_at)
        self.assertEqual(clock.business_days_between(ready, due), 5)
        self.assertEqual(clock.to_business(due).hour, 17)

    # ------------------------------------------------- history and paging --

    def test_audit_history_page_lists_and_filters_recorded_actions(self):
        _, engagement_id = self._create_engagement()
        self.client.post(f"/admin/engagements/{engagement_id}/status",
                         data={"status": "ready", "csrf_token": self.csrf}, follow_redirects=False)
        page = self.client.get("/admin/audit")
        self.assertEqual(page.status_code, 200)
        self.assertIn("engagement.status", page.text)
        self.assertIn("awaiting_client -&gt; ready", page.text)
        filtered = self.client.get("/admin/audit?action=auth.")
        self.assertIn("auth.login", filtered.text)
        rows = filtered.text.split('class="data-row audit-row"')[1:]
        self.assertTrue(rows)
        self.assertTrue(all("auth." in row for row in rows))

    def test_long_lists_are_paginated(self):
        conn = service._db()
        for index in range(admin_workspace.PER_PAGE + 4):
            organization_id = crm.new_id("org")
            conn.execute(
                "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage) VALUES(?,?,?,'prospect')",
                (organization_id, f"Paginated Company {index:03d}", f"paginatedcompany{index:03d}"),
            )
            conn.execute(
                "INSERT INTO opportunities(id,organization_id,stage,service_type) "
                "VALUES(?,?,'new','cash_leak_review')",
                (crm.new_id("opp"), organization_id),
            )
        conn.commit()
        conn.close()
        first = self.client.get("/admin/clients")
        self.assertEqual(first.text.count('class="data-row clients-row"'), admin_workspace.PER_PAGE)
        self.assertIn("Page 1 of 2", first.text)
        second = self.client.get("/admin/clients?page=2")
        self.assertEqual(second.text.count('class="data-row clients-row"'), 4)
        # An out-of-range page must clamp rather than render an empty screen.
        self.assertIn("Page 2 of 2", self.client.get("/admin/clients?page=99").text)
        self.assertIn("Page 1 of 2", self.client.get("/admin/pipeline").text)


class IconRegistryTests(unittest.TestCase):
    """Every icon a template asks for must exist.

    A missing icon renders as blank space with no error anywhere, so the names
    are checked against the registry rather than trusted.
    """

    TEMPLATES = Path(__file__).resolve().parent / "shimline" / "templates"

    def _names_used(self):
        names = set()
        for template in self.TEMPLATES.glob("*.html"):
            text = template.read_text(encoding="utf-8")
            # icon('name') and icon("name")
            names.update(re.findall(r"""icon\(\s*['"]([a-z0-9-]+)['"]""", text))
            # icon(expr) where expr picks between literals
            for expression in re.findall(r"icon\(([^)]*if[^)]*)\)", text):
                expression = re.sub(r"(?:==|!=)\s*'[^']*'", "", expression)
                names.update(re.findall(r"'([a-z][a-z0-9-]{2,})'", expression))
        return names

    def test_every_icon_used_by_a_template_resolves(self):
        used = self._names_used()
        self.assertGreater(len(used), 15, "icon usage was not detected at all")
        missing = sorted(n for n in used if icons.resolve(n) not in icons.PATHS)
        self.assertEqual(missing, [], f"templates use icons absent from the registry: {missing}")

    def test_the_icon_font_is_gone(self):
        """It was 462 KB to draw about twenty-five glyphs."""
        for template in self.TEMPLATES.glob("*.html"):
            text = template.read_text(encoding="utf-8")
            self.assertNotIn("ti ti-", text, template.name)
            self.assertNotIn("tabler", text, template.name)
        vendor = Path(__file__).resolve().parent / "shimline" / "static" / "vendor"
        forbidden = [] if not vendor.exists() else [
            path.name for path in vendor.rglob("*")
            if path.is_file() and (
                path.suffix.lower() in {".woff", ".woff2", ".ttf", ".otf"}
                or "tabler" in path.name.lower()
            )
        ]
        self.assertEqual(forbidden, [], "the vendored icon font is still on disk")

    def test_icon_stroke_styling_lives_in_css_not_sprite_attributes(self):
        """Presentation attributes on the sprite root do not cross the <use>
        shadow boundary; inheritable CSS does. Getting this wrong renders
        every icon as a filled black blob, which no test would otherwise catch.
        """
        for name in ("admin.css", "public/qbo.css"):
            css = (Path(__file__).resolve().parent / "shimline" / "static" / name).read_text(encoding="utf-8")
            block = css[css.index(".icon {"):]
            block = block[:block.index("}")]
            self.assertIn("fill: none", block, name)
            self.assertIn("stroke: currentColor", block, name)

    def test_client_pages_use_the_same_palette_as_the_marketing_site(self):
        """The portal sat in light mode beside a dark site because it hardcoded
        the light values. Both stylesheets must be dark-first with the same
        tokens, or a client meets what looks like a different product."""
        root = Path(__file__).resolve().parents[1]
        # The tokens live in the shared stylesheet, not the landing page's
        # inline <style>, which used to hold a second copy of them.
        site = (root / "site" / "shimline.css").read_text(encoding="utf-8")
        portal_css = (Path(__file__).resolve().parent / "shimline" / "static" /
                      "public" / "qbo.css").read_text(encoding="utf-8")

        def dark_bg(text):
            block = text[text.index(":root"):]
            block = block[:block.index("}")]
            line = next(l for l in block.splitlines() if "--bg:" in l)
            return line.split("--bg:")[1].split(";")[0].strip().lower()

        self.assertEqual(dark_bg(portal_css), dark_bg(site),
                         "the portal's default background must match the site's")
        # And it must still answer a viewer who asks for light.
        self.assertIn("prefers-color-scheme: light", portal_css)
        self.assertIn('[data-theme="light"]', portal_css)

    def test_an_unknown_icon_is_visible_rather_than_silent(self):
        rendered = str(icons.icon("does-not-exist"))
        self.assertIn("i-warning", rendered)

    def test_aliases_resolve_to_real_icons(self):
        for alias, target in icons.ALIASES.items():
            self.assertIn(target, icons.PATHS, alias)

    def test_the_sprite_carries_every_icon_once(self):
        sprite = str(icons.sprite())
        for name in icons.PATHS:
            self.assertEqual(sprite.count(f'id="i-{name}"'), 1, name)


if __name__ == "__main__":
    unittest.main()
