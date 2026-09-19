"""Client accounts, passwordless access, and the client portal."""
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import admin as admin_workspace  # noqa: E402
from shimline import auth, clients, clock, crm, portal  # noqa: E402
from shimline import quickbooks as qbo  # noqa: E402

FAKE_TOKENS = {
    "access_token": "portal-access-token",
    "refresh_token": "portal-refresh-token",
    "expires_in": 3600,
    "x_refresh_token_expires_in": 8726400,
    "scope": qbo.SCOPE,
}


class ClientAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "intake.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        admin_workspace.configure(
            db_factory=service._db, uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90, cookie_secure=False,
        )
        portal.configure(db_factory=service._db, cookie_secure=False)
        qbo.configure(db_factory=service._db, client_id="test-client",
                      client_secret="test-secret",
                      redirect_uri="https://api.shimline.ca/qbo/callback",
                      environment="sandbox")
        self._real_post = qbo._post_token_request
        qbo._post_token_request = lambda payload: dict(FAKE_TOKENS)

    def tearDown(self):
        qbo._post_token_request = self._real_post
        self.temp.cleanup()

    def _org(self, name="Northstar Renovations"):
        conn = service._db()
        organization_id = crm.new_id("org")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage) VALUES(?,?,?,'active')",
            (organization_id, name, crm.normalize_company(name)),
        )
        conn.commit()
        conn.close()
        return organization_id

    def _client_with_session(self, email="owner@example.invalid", organization_id=None):
        conn = service._db()
        client_id = clients.ensure_client(conn, email=email, organization_id=organization_id)
        token = clients.issue_access_link(conn, client_id)
        conn.commit()
        conn.close()
        http = TestClient(service.app, follow_redirects=False)
        entered = http.get(f"/portal/enter?t={token}")
        self.assertEqual(entered.status_code, 302)
        return client_id, http

    def _csrf(self, client_id):
        conn = service._db()
        row = conn.execute(
            "SELECT csrf_token FROM client_sessions WHERE client_user_id=? "
            "ORDER BY created_at DESC LIMIT 1", (client_id,)).fetchone()
        conn.close()
        return row[0]

    # ------------------------------------------------------------ identity --

    def test_paying_creates_the_account_with_no_signup_step(self):
        conn = service._db()
        conn.execute(
            "INSERT INTO payments(order_id,amount,currency,status,email) "
            "VALUES('order_1',19900,'CAD','created','buyer@example.invalid')")
        conn.commit()
        conn.close()

        service._mark_paid("order_1", "pay_1")

        conn = service._db()
        row = conn.execute(
            "SELECT c.id,c.email,p.client_user_id FROM client_users c "
            "JOIN payments p ON p.client_user_id=c.id WHERE p.order_id='order_1'").fetchone()
        conn.close()
        self.assertIsNotNone(row, "a verified payment must create a client account")
        self.assertEqual(row[1], "buyer@example.invalid")

    def test_a_payment_without_an_email_still_succeeds(self):
        """Losing a payment over a missing address would be the wrong trade."""
        conn = service._db()
        conn.execute("INSERT INTO payments(order_id,amount,currency,status,email) "
                     "VALUES('order_2',19900,'CAD','created','')")
        conn.commit()
        conn.close()
        token = service._mark_paid("order_2", "pay_2")
        self.assertTrue(token)
        conn = service._db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM client_users").fetchone()[0], 0)
        self.assertEqual(
            conn.execute("SELECT status FROM payments WHERE order_id='order_2'").fetchone()[0],
            "paid")
        conn.close()

    def test_the_same_buyer_is_one_account_however_often_they_buy(self):
        conn = service._db()
        first = clients.ensure_client(conn, email="repeat@example.invalid")
        second = clients.ensure_client(conn, email="REPEAT@example.invalid",
                                       display_name="Sam")
        conn.commit()
        self.assertEqual(first, second)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM client_users").fetchone()[0], 1)
        conn.close()

    def test_operator_entered_details_are_not_overwritten_by_a_later_checkout(self):
        organization_id = self._org()
        other = self._org("Someone Else Ltd")
        conn = service._db()
        client_id = clients.ensure_client(conn, email="a@example.invalid",
                                          display_name="Correct Name",
                                          organization_id=organization_id)
        # A second checkout with a mistyped name and the wrong business must
        # not clobber what is already recorded.
        clients.ensure_client(conn, email="a@example.invalid", display_name="Typo",
                              organization_id=other)
        conn.commit()
        row = conn.execute(
            "SELECT display_name,organization_id FROM client_users WHERE id=?", (client_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "Correct Name")
        self.assertEqual(row[1], organization_id)

    # -------------------------------------------------------- access links --

    def test_an_access_link_works_once(self):
        conn = service._db()
        client_id = clients.ensure_client(conn, email="once@example.invalid")
        token = clients.issue_access_link(conn, client_id)
        conn.commit()
        conn.close()

        http = TestClient(service.app, follow_redirects=False)
        self.assertEqual(http.get(f"/portal/enter?t={token}").status_code, 302)
        self.assertIsNotNone(http.cookies.get(clients.COOKIE_NAME))

        replay = TestClient(service.app, follow_redirects=False)
        used_again = replay.get(f"/portal/enter?t={token}")
        self.assertEqual(used_again.headers["location"], "/portal/expired")
        self.assertIsNone(replay.cookies.get(clients.COOKIE_NAME))

    def test_issuing_a_new_link_kills_the_previous_one(self):
        conn = service._db()
        client_id = clients.ensure_client(conn, email="fresh@example.invalid")
        old = clients.issue_access_link(conn, client_id)
        new = clients.issue_access_link(conn, client_id)
        conn.commit()
        conn.close()
        http = TestClient(service.app, follow_redirects=False)
        self.assertEqual(http.get(f"/portal/enter?t={old}").headers["location"], "/portal/expired")
        self.assertEqual(http.get(f"/portal/enter?t={new}").status_code, 302)

    def test_an_expired_link_is_refused(self):
        conn = service._db()
        client_id = clients.ensure_client(conn, email="stale@example.invalid")
        token = clients.issue_access_link(conn, client_id)
        conn.execute("UPDATE client_access_tokens SET expires_at=?",
                     (clock.format_timestamp(clock.now() - timedelta(hours=1)),))
        conn.commit()
        conn.close()
        http = TestClient(service.app, follow_redirects=False)
        self.assertEqual(http.get(f"/portal/enter?t={token}").headers["location"],
                         "/portal/expired")

    def test_the_link_token_never_lands_in_a_rendered_page(self):
        """Same reasoning as the QuickBooks callback: a page that loads
        subresources would leak the token through a Referer header."""
        conn = service._db()
        client_id = clients.ensure_client(conn, email="ref@example.invalid")
        token = clients.issue_access_link(conn, client_id)
        conn.commit()
        conn.close()
        http = TestClient(service.app, follow_redirects=False)
        entered = http.get(f"/portal/enter?t={token}")
        self.assertEqual(entered.status_code, 302)
        self.assertEqual(entered.text.strip(), "")
        self.assertNotIn(token, entered.headers["location"])

    def test_only_hashes_of_links_and_sessions_are_stored(self):
        conn = service._db()
        client_id = clients.ensure_client(conn, email="hash@example.invalid")
        token = clients.issue_access_link(conn, client_id)
        conn.commit()
        conn.close()
        TestClient(service.app, follow_redirects=False).get(f"/portal/enter?t={token}")
        raw = Path(service.DB_PATH).read_bytes()
        self.assertNotIn(token.encode(), raw)

    # ------------------------------------------------------------- portal --

    def test_the_portal_needs_a_session(self):
        anonymous = TestClient(service.app, follow_redirects=False)
        response = anonymous.get("/portal")
        self.assertEqual(response.status_code, 200)
        self.assertIn("personal, time-limited access link", response.text)
        response = anonymous.get("/portal/quickbooks")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/portal/expired")
        self.assertEqual(anonymous.get("/portal/expired").status_code, 200)

    def test_the_client_cookie_is_scoped_away_from_the_workspace(self):
        _, http = self._client_with_session()
        paths = {p for domain in http.cookies.jar._cookies.values() for p in domain}
        self.assertIn("/portal", paths)
        self.assertNotIn("/admin", paths)
        self.assertNotIn("/", paths)

    def test_a_client_session_cannot_open_the_operator_workspace(self):
        _, http = self._client_with_session()
        response = http.get("/admin")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/admin/login"))

    def test_the_portal_shows_the_two_ways_to_send_documents(self):
        organization_id = self._org()
        _, http = self._client_with_session(organization_id=organization_id)
        page = http.get("/portal")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Connect QuickBooks", page.text)
        self.assertIn("Send the exports yourself", page.text)
        self.assertIn("Northstar Renovations", page.text)

    def _engagement_with_a_professional(self, organization_id, *, disclose=True):
        conn = service._db()
        from shimline import tenancy
        tenancy.create_firm(conn, "firm_a", "Alder & Co")
        user_id = tenancy.create_firm_user(
            conn, "firm_a", email="dana@alder.invalid", display_name="Dana Alder",
            password="local-test-password-only", firm_role="principal")
        tenancy.add_client(conn, "firm_a", organization_id)
        conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status) "
            "VALUES('eng_1',?,'August books','in_progress')", (organization_id,))
        if disclose:
            crm.disclose_professional_of_record(
                conn, "eng_1", firm_id="firm_a", user_id=user_id)
        conn.commit()
        conn.close()
        return user_id

    def test_the_portal_names_the_professional_doing_the_work(self):
        """One brand, one interface, and the work still has a named author."""
        organization_id = self._org()
        self._engagement_with_a_professional(organization_id)
        _, http = self._client_with_session(organization_id=organization_id)
        page = http.get("/portal")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Dana Alder", page.text)
        self.assertIn("Alder &amp; Co", page.text)
        self.assertIn("Your work is with Shimline", page.text)

    def test_an_undisclosed_engagement_shows_no_panel_rather_than_a_blank_one(self):
        """A half-filled disclosure reads as an evasion of the question it
        exists to answer, so there is nothing to render until there is a name."""
        organization_id = self._org()
        self._engagement_with_a_professional(organization_id, disclose=False)
        _, http = self._client_with_session(organization_id=organization_id)
        page = http.get("/portal")
        self.assertNotIn("Who is doing your books", page.text)
        self.assertNotIn("Dana Alder", page.text)

    def test_the_disclosed_name_outlives_the_person_and_the_firms_name(self):
        """The point of snapshotting. What the client was told in March has to
        keep reading as what the client was told in March."""
        organization_id = self._org()
        user_id = self._engagement_with_a_professional(organization_id)
        conn = service._db()
        conn.execute("UPDATE firms SET name='Birchwood LLP' WHERE id='firm_a'")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        conn.close()

        _, http = self._client_with_session(organization_id=organization_id)
        page = http.get("/portal")
        self.assertIn("Dana Alder", page.text)
        self.assertIn("Alder &amp; Co", page.text)
        self.assertNotIn("Birchwood", page.text)

    # ------------------------------------------ client-initiated connect --

    def test_a_client_connects_their_own_books_and_lands_back_in_the_portal(self):
        organization_id = self._org()
        client_id, http = self._client_with_session(organization_id=organization_id)

        started = http.post("/portal/quickbooks/connect",
                            data={"csrf_token": self._csrf(client_id)})
        self.assertEqual(started.status_code, 303)
        self.assertTrue(started.headers["location"].startswith(qbo.AUTHORIZE_URL))
        from urllib.parse import parse_qs, urlparse
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

        finished = http.get(f"/qbo/callback?code=c&state={state}&realmId=4620816365")
        self.assertEqual(finished.status_code, 302)
        self.assertEqual(finished.headers["location"], "/portal/quickbooks?connected=1")

        conn = service._db()
        row = conn.execute(
            "SELECT organization_id,connected_by_client_id,status FROM connections").fetchone()
        summaries = [r[0] for r in conn.execute(
            "SELECT summary FROM audit_events WHERE action LIKE 'qbo.connect%'")]
        conn.close()
        self.assertEqual(row[0], organization_id)
        self.assertEqual(row[1], client_id, "the connection must record who authorised it")
        self.assertEqual(row[2], "active")
        self.assertTrue(any("by the client" in s for s in summaries))

    def test_a_client_cannot_connect_against_another_clients_business(self):
        """The organization comes from the session, never from the request."""
        mine = self._org("My Company")
        theirs = self._org("Their Company")
        client_id, http = self._client_with_session(organization_id=mine)
        csrf = self._csrf(client_id)

        # Every shape of injection attempt lands on the caller's own record.
        for payload in ({"csrf_token": csrf, "organization_id": theirs},
                        {"csrf_token": csrf, "organization": theirs}):
            response = http.post("/portal/quickbooks/connect", data=payload)
            self.assertEqual(response.status_code, 303)
        conn = service._db()
        owners = {r[0] for r in conn.execute("SELECT organization_id FROM oauth_states")}
        conn.close()
        self.assertEqual(owners, {mine})

    def test_connecting_requires_a_csrf_token(self):
        organization_id = self._org()
        client_id, http = self._client_with_session(organization_id=organization_id)
        response = http.post("/portal/quickbooks/connect", data={"csrf_token": "wrong"})
        self.assertEqual(response.status_code, 403)
        conn = service._db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0], 0)
        conn.close()

    def test_an_account_with_no_business_is_told_rather_than_failing(self):
        client_id, http = self._client_with_session()
        response = http.post("/portal/quickbooks/connect",
                             data={"csrf_token": self._csrf(client_id)})
        self.assertEqual(response.status_code, 409)

    def test_signing_out_ends_the_session(self):
        client_id, http = self._client_with_session()
        self.assertEqual(http.get("/portal").status_code, 200)
        http.post("/portal/leave")
        signed_out = http.get("/portal")
        self.assertEqual(signed_out.status_code, 200)
        self.assertIn("personal, time-limited access link", signed_out.text)


class OperatorClientLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "intake.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        admin_workspace.configure(
            db_factory=service._db, uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90, cookie_secure=False,
            portal_base="https://api.shimline.ca",
        )
        conn = service._db()
        auth.create_user(conn, "op@example.invalid", "Operator",
                         "local-test-password-only", "owner")
        self.organization_id = crm.new_id("org")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage) VALUES(?,?,?,'active')",
            (self.organization_id, "Northstar", "northstar"))
        self.client_id = clients.ensure_client(
            conn, email="owner@example.invalid", organization_id=self.organization_id)
        conn.commit()
        conn.close()
        self.http = TestClient(service.app, follow_redirects=False)
        self.http.post("/admin/login", data={
            "email": "op@example.invalid", "password": "local-test-password-only",
            "next": "/admin"})
        conn = service._db()
        self.csrf = conn.execute("SELECT csrf_token FROM sessions").fetchone()[0]
        conn.close()

    def tearDown(self):
        self.http.close()
        self.temp.cleanup()

    def test_the_module_is_not_shadowed_by_the_route_of_the_same_name(self):
        """`def clients(...)` is a route in admin.py; the module import has to
        be aliased or every client page 500s. This caught exactly that."""
        page = self.http.get(f"/admin/clients/{self.organization_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("owner@example.invalid", page.text)

    def test_an_operator_can_issue_a_link_and_it_signs_the_client_in(self):
        issued = self.http.post(
            f"/admin/clients/{self.organization_id}/client-link",
            data={"csrf_token": self.csrf})
        self.assertEqual(issued.status_code, 303)
        from urllib.parse import parse_qs, urlparse, unquote
        token = unquote(parse_qs(urlparse(issued.headers["location"]).query)["link"][0])

        conn = service._db()
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_events")]
        conn.close()
        self.assertIn("client.link.issued", actions)

        as_client = TestClient(service.app, follow_redirects=False)
        self.assertEqual(as_client.get(f"/portal/enter?t={token}").status_code, 302)
        self.assertEqual(as_client.get("/portal").status_code, 200)

    def test_issuing_a_link_needs_a_csrf_token(self):
        denied = self.http.post(f"/admin/clients/{self.organization_id}/client-link",
                                data={"csrf_token": "wrong"})
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
