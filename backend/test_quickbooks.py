"""QuickBooks OAuth: the four public endpoints, state handling, token secrecy.

Intuit is never contacted. `_post_token_request` is the single seam where this
module talks to them, so it is replaced with a stub and everything either side
of it is exercised for real.
"""
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as service
from shimline import admin as admin_workspace
from shimline import auth, clock, crm
from shimline import quickbooks as qbo

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

FAKE_TOKENS = {
    "access_token": "fake-access-token-value",
    "refresh_token": "fake-refresh-token-value",
    "expires_in": 3600,
    "x_refresh_token_expires_in": 8726400,
    "scope": qbo.SCOPE,
}


class QuickBooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "intake.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        admin_workspace.configure(
            db_factory=service._db, uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90, cookie_secure=False,
        )
        qbo.configure(
            db_factory=service._db, client_id="test-client", client_secret="test-secret",
            redirect_uri="https://api.shimline.ca/qbo/callback", environment="sandbox",
        )
        conn = service._db()
        self.user_id = auth.create_user(
            conn, "admin@example.invalid", "Local Admin", "local-test-password-only", "owner"
        )
        self.organization_id = crm.new_id("org")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage) VALUES(?,?,?,'active')",
            (self.organization_id, "Northstar Renovations", "northstarrenovations"),
        )
        conn.commit()
        conn.close()

        self.client = TestClient(service.app, follow_redirects=False)
        self.client.post("/admin/login", data={
            "email": "admin@example.invalid", "password": "local-test-password-only", "next": "/admin",
        })
        conn = service._db()
        self.csrf = conn.execute("SELECT csrf_token FROM sessions").fetchone()[0]
        conn.close()

        self._real_post = qbo._post_token_request
        self.token_calls = []

        def fake_post(payload):
            self.token_calls.append(payload)
            return dict(FAKE_TOKENS)

        qbo._post_token_request = fake_post

    def tearDown(self):
        qbo._post_token_request = self._real_post
        self.client.close()
        self.temp.cleanup()

    def _start_connection(self) -> str:
        """Start an operator-initiated connect; return the state Intuit gets."""
        response = self.client.post(
            f"/admin/clients/{self.organization_id}/qbo-connect",
            data={"csrf_token": self.csrf},
        )
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertTrue(location.startswith(qbo.AUTHORIZE_URL), location)
        self.assertIn("scope=com.intuit.quickbooks.accounting", location)
        self.assertIn("response_type=code", location)
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(location).query)["state"][0]

    # ------------------------------------------------------ public surface --

    def test_the_four_intuit_urls_are_reachable(self):
        anonymous = TestClient(service.app, follow_redirects=False)
        launch = anonymous.get("/qbo/launch")
        self.assertEqual(launch.status_code, 200)
        self.assertIn("Shimline", launch.text)

        disconnected = anonymous.get("/qbo/disconnect")
        self.assertEqual(disconnected.status_code, 200)
        self.assertIn("no longer has access", disconnected.text)

        # The connect URL Intuit lists must answer without a session.
        connect = anonymous.get("/qbo/connect")
        self.assertEqual(connect.status_code, 200)
        self.assertIn("set up by your bookkeeper", connect.text)

        # Intuit fetches the stylesheet these pages reference.
        self.assertEqual(anonymous.get("/qbo/static/qbo.css").status_code, 200)
        # A bare callback redirects rather than returning a body.
        self.assertEqual(anonymous.get("/qbo/callback").status_code, 302)
        anonymous.close()

    def test_starting_a_connection_requires_a_session_and_a_csrf_token(self):
        anonymous = TestClient(service.app, follow_redirects=False)
        denied = anonymous.post(
            f"/admin/clients/{self.organization_id}/qbo-connect", data={"csrf_token": self.csrf})
        self.assertEqual(denied.status_code, 401)
        anonymous.close()

        forged = self.client.post(
            f"/admin/clients/{self.organization_id}/qbo-connect", data={"csrf_token": "wrong"})
        self.assertEqual(forged.status_code, 403)

        conn = service._db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0], 0)
        conn.close()

    def test_connect_rejects_an_unknown_client(self):
        response = self.client.post(
            "/admin/clients/org_nope/qbo-connect", data={"csrf_token": self.csrf})
        self.assertEqual(response.status_code, 404)

    def test_the_operator_session_cookie_is_never_sent_to_public_routes(self):
        """The cookie is scoped to /admin. This is what makes /qbo/connect
        an information page rather than the place authorization starts."""
        cookie = self.client.cookies.jar._cookies
        paths = {path for domain in cookie.values() for path in domain}
        self.assertIn("/admin", paths)
        self.assertNotIn("/", paths)

    # ------------------------------------------------------------- callback --

    def test_callback_stores_an_encrypted_connection(self):
        state = self._start_connection()
        response = self.client.get(
            f"/qbo/callback?code=auth-code&state={state}&realmId=4620816365")
        # Intuit forbids returning a body from an endpoint that receives tokens
        # in the URL, so success is a redirect carrying nothing sensitive.
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        self.assertEqual(location, f"/admin/clients/{self.organization_id}?qbo=connected")
        self.assertNotIn("4620816365", location)
        self.assertNotIn("auth-code", location)
        self.assertEqual(response.text.strip(), "")

        conn = service._db()
        row = conn.execute(
            "SELECT organization_id,realm_id_enc,realm_id_hash,environment,status,"
            "access_token_enc,refresh_token_enc,scope FROM connections"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], self.organization_id)
        self.assertEqual(row[3], "sandbox")
        self.assertEqual(row[4], "active")
        # The realm id identifies a customer's company, so it is encrypted too.
        self.assertNotIn("4620816365", row[1])
        self.assertEqual(qbo.decrypt_token(row[1]), "4620816365")
        self.assertNotIn("4620816365", row[2])
        self.assertNotIn(FAKE_TOKENS["access_token"], row[5])
        self.assertNotIn(FAKE_TOKENS["refresh_token"], row[6])
        self.assertEqual(qbo.decrypt_token(row[5]), FAKE_TOKENS["access_token"])
        self.assertEqual(qbo.decrypt_token(row[6]), FAKE_TOKENS["refresh_token"])
        self.assertEqual(row[7], qbo.SCOPE)

    def test_the_callback_never_returns_a_body_whatever_happens(self):
        """Intuit security requirements, "Sensitive information": an endpoint
        handling authentication tokens in URL parameters must redirect rather
        than return HTML, so the code cannot leak through a Referer header."""
        state = self._start_connection()
        cases = [
            "/qbo/callback",
            "/qbo/callback?error=access_denied&error_description=User+declined",
            "/qbo/callback?code=x&state=forged&realmId=1",
            f"/qbo/callback?code=auth-code&state={state}&realmId=4620816365",
        ]
        for path in cases:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertEqual(response.text.strip(), "", path)
            self.assertNotIn("auth-code", response.headers["location"], path)

    def test_the_status_page_explains_failures_without_carrying_secrets(self):
        anonymous = TestClient(service.app)
        page = anonymous.get("/qbo/status?problem=expired")
        self.assertEqual(page.status_code, 200)
        self.assertIn("expired or was already used", page.text)
        # An unknown slug must not blow up or echo anything back.
        junk = anonymous.get("/qbo/status?problem=<script>alert(1)</script>")
        self.assertEqual(junk.status_code, 200)
        self.assertNotIn("<script>alert(1)</script>", junk.text)
        anonymous.close()

    def test_the_access_log_never_records_an_authorization_code(self):
        """uvicorn logs the full request line, and the callback carries a code."""
        import logging
        log_filter = next(f for f in logging.getLogger("uvicorn.access").filters
                          if type(f).__name__ == "_RedactQueryStrings")

        def logged(path):
            record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s",
                                       ("1.2.3.4:0", "GET", path, "1.1", 302), None)
            log_filter.filter(record)
            return record.args[2]

        scrubbed = logged("/qbo/callback?code=SECRET&state=S&realmId=4620816365")
        self.assertNotIn("SECRET", scrubbed)
        self.assertNotIn("4620816365", scrubbed)
        self.assertEqual(scrubbed, "/qbo/callback?<redacted>")
        # Ordinary workspace filters stay readable for operations.
        self.assertEqual(logged("/admin/audit?action=auth."), "/admin/audit?action=auth.")

    def test_tokens_are_not_readable_from_the_raw_database_file(self):
        state = self._start_connection()
        self.client.get(f"/qbo/callback?code=auth-code&state={state}&realmId=4620816365")
        raw = Path(service.DB_PATH).read_bytes()
        self.assertNotIn(FAKE_TOKENS["refresh_token"].encode(), raw)
        self.assertNotIn(FAKE_TOKENS["access_token"].encode(), raw)

    def test_a_state_cannot_be_replayed(self):
        state = self._start_connection()
        first = self.client.get(f"/qbo/callback?code=auth-code&state={state}&realmId=4620816365")
        self.assertEqual(first.status_code, 302)
        replay = self.client.get(f"/qbo/callback?code=auth-code&state={state}&realmId=9999999999")
        self.assertEqual(replay.status_code, 302)
        self.assertIn("problem=expired", replay.headers["location"])
        conn = service._db()
        count = conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "a replayed state must not mint a second connection")

    def test_a_forged_state_is_refused(self):
        forged = self.client.get("/qbo/callback?code=auth-code&state=not-a-real-state&realmId=1")
        self.assertEqual(forged.status_code, 302)
        conn = service._db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0], 0)
        conn.close()

    def test_an_expired_state_is_refused(self):
        state = self._start_connection()
        conn = service._db()
        conn.execute("UPDATE oauth_states SET expires_at=?",
                     (clock.format_timestamp(clock.now() - __import__("datetime").timedelta(minutes=1)),))
        conn.commit()
        conn.close()
        response = self.client.get(f"/qbo/callback?code=auth-code&state={state}&realmId=1")
        self.assertEqual(response.status_code, 302)
        self.assertIn("problem=expired", response.headers["location"])

    def test_user_declining_at_intuit_is_reported_not_crashed(self):
        response = self.client.get("/qbo/callback?error=access_denied&error_description=User+declined")
        self.assertEqual(response.status_code, 302)
        self.assertIn("problem=declined", response.headers["location"])

    # -------------------------------------------------------- refresh/revoke --

    def test_refresh_writes_back_the_rotated_refresh_token(self):
        state = self._start_connection()
        self.client.get(f"/qbo/callback?code=auth-code&state={state}&realmId=4620816365")
        conn = service._db()
        connection_id = conn.execute("SELECT id FROM connections").fetchone()[0]

        rotated = dict(FAKE_TOKENS, access_token="second-access", refresh_token="rotated-refresh")
        qbo._post_token_request = lambda payload: dict(rotated)
        qbo.refresh_connection(conn, connection_id)

        row = conn.execute(
            "SELECT access_token_enc,refresh_token_enc FROM connections WHERE id=?", (connection_id,)
        ).fetchone()
        conn.close()
        # Intuit rotates the refresh token on use; failing to store it loses the
        # connection on the *next* refresh, which is the classic silent bug.
        self.assertEqual(qbo.decrypt_token(row[0]), "second-access")
        self.assertEqual(qbo.decrypt_token(row[1]), "rotated-refresh")

    def test_disconnect_inside_quickbooks_clears_the_stored_credentials(self):
        state = self._start_connection()
        self.client.get(f"/qbo/callback?code=auth-code&state={state}&realmId=4620816365")
        anonymous = TestClient(service.app)
        page = anonymous.get("/qbo/disconnect?realmId=4620816365")
        anonymous.close()
        self.assertEqual(page.status_code, 200)
        conn = service._db()
        row = conn.execute(
            "SELECT status,access_token_enc,refresh_token_enc FROM connections"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "revoked")
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])

    def test_operator_disconnect_is_scoped_to_the_right_client(self):
        state = self._start_connection()
        self.client.get(f"/qbo/callback?code=auth-code&state={state}&realmId=4620816365")
        conn = service._db()
        connection_id = conn.execute("SELECT id FROM connections").fetchone()[0]
        other_id = crm.new_id("org")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage) VALUES(?,?,?,'active')",
            (other_id, "Someone Else Ltd", "someoneelse"),
        )
        conn.commit()
        conn.close()

        # Revoking through a client that does not own the connection must fail.
        wrong = self.client.post(
            f"/admin/clients/{other_id}/qbo-disconnect",
            data={"connection_id": connection_id, "csrf_token": self.csrf},
        )
        self.assertEqual(wrong.status_code, 404)

        qbo._post_token_request = lambda payload: {}
        right = self.client.post(
            f"/admin/clients/{self.organization_id}/qbo-disconnect",
            data={"connection_id": connection_id, "csrf_token": self.csrf},
        )
        self.assertEqual(right.status_code, 303)
        conn = service._db()
        status = conn.execute("SELECT status FROM connections").fetchone()[0]
        conn.close()
        self.assertEqual(status, "revoked")

    # ------------------------------------------------- endpoints and retries --

    def test_endpoints_come_from_discovery_and_survive_an_outage(self):
        """Intuit asks apps to read endpoints from the discovery document.
        A failed fetch must degrade to known-good values, not to nothing."""
        import urllib.error
        qbo._discovery.update({"fetched_at": 0.0, "source": "fallback",
                               "endpoints": dict(qbo.FALLBACK_ENDPOINTS)})
        document = {
            "authorization_endpoint": "https://appcenter.example.invalid/connect/oauth2",
            "token_endpoint": "https://oauth.example.invalid/tokens/bearer",
            "revocation_endpoint": "https://api.example.invalid/tokens/revoke",
        }

        class _Response:
            def read(self_inner):
                import json as _json
                return _json.dumps(document).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

        real_urlopen = qbo.urllib.request.urlopen
        qbo.urllib.request.urlopen = lambda *a, **k: _Response()
        try:
            resolved = qbo.endpoints(force=True)
            self.assertEqual(resolved["token_endpoint"], document["token_endpoint"])
            self.assertEqual(qbo._discovery["source"], "discovery")
        finally:
            qbo.urllib.request.urlopen = real_urlopen

        # Now discovery breaks. The last good values must still be returned.
        def _boom(*args, **kwargs):
            raise urllib.error.URLError("discovery is down")

        qbo.urllib.request.urlopen = _boom
        try:
            self.assertEqual(qbo.endpoints(force=True)["token_endpoint"],
                             document["token_endpoint"])
        finally:
            qbo.urllib.request.urlopen = real_urlopen
            qbo._discovery.update({"fetched_at": 0.0, "source": "fallback",
                                   "endpoints": dict(qbo.FALLBACK_ENDPOINTS)})

    def test_transient_failures_are_retried_and_authentication_failures_are_not(self):
        import urllib.error
        qbo._post_token_request = self._real_post
        calls = []

        def responder(status, body=b"{}"):
            """Stand in for Intuit answering with a given status and error body."""
            def _urlopen(request, timeout=None):
                import io
                calls.append(request.full_url)
                raise urllib.error.HTTPError(
                    request.full_url, status, "error", {}, io.BytesIO(body))
            return _urlopen

        real_urlopen = qbo.urllib.request.urlopen
        real_sleep = qbo.time.sleep
        qbo.time.sleep = lambda seconds: None
        try:
            # 503 is transient: attempted TOKEN_ATTEMPTS times, then gives up.
            calls.clear()
            qbo.urllib.request.urlopen = responder(503)
            with self.assertRaises(HTTPException) as caught:
                qbo._post_token_request({"grant_type": "refresh_token"})
            self.assertEqual(caught.exception.status_code, 502)
            self.assertEqual(len(calls), qbo.TOKEN_ATTEMPTS)

            # invalid_grant is permanent: tried once, and raises the signal that
            # tells the rest of the app a human has to reconnect.
            calls.clear()
            qbo.urllib.request.urlopen = responder(400, b'{"error":"invalid_grant"}')
            with self.assertRaises(qbo.ReconnectRequired):
                qbo._post_token_request({"grant_type": "refresh_token"})
            self.assertEqual(len(calls), 1, "an authentication decision must not be retried")

            # A plain 400 that is not invalid_grant is also not retried.
            calls.clear()
            qbo.urllib.request.urlopen = responder(400, b'{"error":"invalid_client"}')
            with self.assertRaises(HTTPException):
                qbo._post_token_request({"grant_type": "refresh_token"})
            self.assertEqual(len(calls), 1)
        finally:
            qbo.urllib.request.urlopen = real_urlopen
            qbo.time.sleep = real_sleep
            qbo._post_token_request = lambda payload: dict(FAKE_TOKENS)

    # --------------------------------------------------------- token lifetime --

    def _connect(self) -> str:
        state = self._start_connection()
        self.client.get(f"/qbo/callback?code=auth-code&state={state}&realmId=4620816365")
        conn = service._db()
        connection_id = conn.execute("SELECT id FROM connections").fetchone()[0]
        conn.close()
        return connection_id

    def test_a_near_expired_access_token_is_refreshed_before_use(self):
        connection_id = self._connect()
        conn = service._db()
        # Push expiry inside the safety margin.
        conn.execute(
            "UPDATE connections SET access_expires_at=? WHERE id=?",
            (clock.format_timestamp(clock.now() + timedelta(seconds=60)), connection_id),
        )
        conn.commit()
        rotated = dict(FAKE_TOKENS, access_token="refreshed-access", refresh_token="rotated")
        qbo._post_token_request = lambda payload: dict(rotated)
        token = qbo.ensure_access_token(conn, connection_id)
        conn.close()
        self.assertEqual(token, "refreshed-access")

    def test_a_healthy_access_token_is_used_without_a_round_trip(self):
        connection_id = self._connect()
        conn = service._db()
        calls = []
        qbo._post_token_request = lambda payload: calls.append(payload) or dict(FAKE_TOKENS)
        token = qbo.ensure_access_token(conn, connection_id)
        conn.close()
        self.assertEqual(token, FAKE_TOKENS["access_token"])
        self.assertEqual(calls, [], "a valid token must not be refreshed needlessly")

    def test_idle_connections_are_refreshed_before_the_token_can_lapse(self):
        connection_id = self._connect()
        conn = service._db()
        self.assertEqual(qbo.connections_due_for_refresh(conn), [],
                         "a fresh connection is not due")
        stale = clock.now() - timedelta(days=qbo.REFRESH_KEEPALIVE_DAYS + 1)
        conn.execute("UPDATE connections SET last_refreshed_at=? WHERE id=?",
                     (clock.format_timestamp(stale), connection_id))
        conn.commit()
        self.assertEqual(qbo.connections_due_for_refresh(conn), [connection_id])

        rotated = dict(FAKE_TOKENS, access_token="keepalive-access", refresh_token="keepalive-refresh")
        qbo._post_token_request = lambda payload: dict(rotated)
        summary = qbo.run_keepalive(conn)
        self.assertEqual(summary, {"checked": 1, "refreshed": 1, "needs_reconnect": 0, "failed": 0})
        # Refreshed, so no longer due.
        self.assertEqual(qbo.connections_due_for_refresh(conn), [])
        conn.close()

    def test_a_dead_refresh_token_marks_the_connection_for_reconnection(self):
        connection_id = self._connect()
        conn = service._db()

        def dead(payload):
            raise qbo.ReconnectRequired("QuickBooks rejected the stored authorization")

        qbo._post_token_request = dead
        with self.assertRaises(qbo.ReconnectRequired):
            qbo.refresh_connection(conn, connection_id)
        row = conn.execute(
            "SELECT status,status_detail,access_token_enc,refresh_token_enc FROM connections WHERE id=?",
            (connection_id,),
        ).fetchone()
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_events")]
        conn.close()
        self.assertEqual(row[0], "revoked")
        self.assertIn("must reconnect", row[1])
        # Dead credentials are cleared rather than left lying around.
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        self.assertIn("qbo.reconnect_required", actions)

    def test_a_transient_refresh_failure_keeps_the_credentials_for_a_retry(self):
        connection_id = self._connect()
        conn = service._db()

        def unwell(payload):
            raise HTTPException(502, "Could not reach QuickBooks")

        qbo._post_token_request = unwell
        with self.assertRaises(HTTPException):
            qbo.refresh_connection(conn, connection_id)
        row = conn.execute(
            "SELECT status,refresh_token_enc FROM connections WHERE id=?", (connection_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "error")
        self.assertIsNotNone(row[1], "a transient failure must not discard a good token")

    def test_keepalive_counts_outcomes_without_raising(self):
        connection_id = self._connect()
        conn = service._db()
        conn.execute("UPDATE connections SET last_refreshed_at=? WHERE id=?",
                     (clock.format_timestamp(clock.now() - timedelta(days=99)), connection_id))
        conn.commit()

        def dead(payload):
            raise qbo.ReconnectRequired("gone")

        qbo._post_token_request = dead
        summary = qbo.run_keepalive(conn)
        conn.close()
        self.assertEqual(summary["needs_reconnect"], 1)
        self.assertEqual(summary["refreshed"], 0)

    # ---------------------------------------------------------- configuration --

    def test_production_must_be_chosen_deliberately(self):
        for value in ("", "sandbox", "prod", "PRODUCTION", "anything"):
            qbo.configure(db_factory=service._db, client_id="x", client_secret="y",
                          redirect_uri="https://api.shimline.ca/qbo/callback",
                          environment=value)
            self.assertEqual(qbo._settings["environment"], "sandbox", value)
        qbo.configure(db_factory=service._db, client_id="x", client_secret="y",
                      redirect_uri="https://api.shimline.ca/qbo/callback",
                      environment="production")
        self.assertEqual(qbo._settings["environment"], "production")

    def test_a_missing_master_key_refuses_rather_than_storing_plaintext(self):
        """Encryption keys come from the server environment, not from configure().

        With no master key the module must refuse outright: a refresh token
        written in the clear is worse than a failed connection.
        """
        saved = {name: os.environ.pop(name, None)
                 for name in ("SHIMLINE_SECRET_KEY", "QBO_TOKEN_KEY")}
        try:
            with self.assertRaises(HTTPException) as caught:
                qbo.encrypt_token("a-refresh-token")
            self.assertEqual(caught.exception.status_code, 503)
            self.assertIn("SHIMLINE_SECRET_KEY", caught.exception.detail)
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
