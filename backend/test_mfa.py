"""Two-factor authentication: RFC conformance, enrolment, and the login gate."""
import base64
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from shimline import crypto, totp

# A master key must exist before app/auth import anything that needs it.
os.environ.setdefault("SHIMLINE_SECRET_KEY", "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import admin as admin_workspace  # noqa: E402
from shimline import auth  # noqa: E402


def b32(ascii_secret: str) -> str:
    return base64.b32encode(ascii_secret.encode()).decode().rstrip("=")


class TotpConformanceTests(unittest.TestCase):
    """RFC 6238 Appendix B. These are the spec's own answers, not ours."""

    SHA1 = b32("12345678901234567890")
    SHA256 = b32("12345678901234567890123456789012")
    SHA512 = b32("1234567890123456789012345678901234567890123456789012345678901234")

    VECTORS = [
        (59, "94287082", "46119246", "90693936"),
        (1111111109, "07081804", "68084774", "25091201"),
        (1111111111, "14050471", "67062674", "99943326"),
        (1234567890, "89005924", "91819424", "93441116"),
        (2000000000, "69279037", "90698825", "38618901"),
        (20000000000, "65353130", "77737706", "47863826"),
    ]

    def test_matches_rfc6238_test_vectors(self):
        for moment, sha1, sha256, sha512 in self.VECTORS:
            self.assertEqual(totp.generate(self.SHA1, moment=moment, digits=8,
                                           digest=hashlib.sha1), sha1, moment)
            self.assertEqual(totp.generate(self.SHA256, moment=moment, digits=8,
                                           digest=hashlib.sha256), sha256, moment)
            self.assertEqual(totp.generate(self.SHA512, moment=moment, digits=8,
                                           digest=hashlib.sha512), sha512, moment)

    def test_secret_parsing_tolerates_how_people_type(self):
        secret = totp.generate_secret()
        expected = totp.normalize(secret)
        for variant in (secret.lower(), totp.format_secret(secret), f" {secret} "):
            self.assertEqual(totp.normalize(variant), expected)
        with self.assertRaises(ValueError):
            totp.normalize("not valid base32 !!")

    def test_accepts_one_step_of_clock_drift_but_not_two(self):
        secret = totp.generate_secret()
        now = 1_700_000_000
        for offset in (-30, 0, 30):
            code = totp.generate(secret, moment=now + offset)
            self.assertIsNotNone(totp.verify(secret, code, moment=now), offset)
        for offset in (-90, 90):
            code = totp.generate(secret, moment=now + offset)
            self.assertIsNone(totp.verify(secret, code, moment=now), offset)

    def test_a_used_code_cannot_be_replayed(self):
        secret = totp.generate_secret()
        now = 1_700_000_000
        code = totp.generate(secret, moment=now)
        counter = totp.verify(secret, code, moment=now)
        self.assertIsNotNone(counter)
        # Same code, same window, but the counter has already been spent.
        self.assertIsNone(totp.verify(secret, code, moment=now, last_counter=counter))

    def test_malformed_codes_are_rejected_without_raising(self):
        secret = totp.generate_secret()
        for bad in ("", "abc", "12345", "1234567", None, "12 34 56"):
            self.assertIsNone(totp.verify(secret, bad))


class CryptoTests(unittest.TestCase):
    def test_purposes_do_not_share_key_material(self):
        blob = crypto.encrypt("sensitive", crypto.TOTP_SECRETS)
        self.assertEqual(crypto.decrypt(blob, crypto.TOTP_SECRETS), "sensitive")
        from cryptography.fernet import InvalidToken
        with self.assertRaises(InvalidToken):
            crypto.decrypt(blob, crypto.QUICKBOOKS_TOKENS)

    def test_a_missing_master_key_refuses_rather_than_storing_plaintext(self):
        saved = os.environ.pop("SHIMLINE_SECRET_KEY", None)
        legacy = os.environ.pop("QBO_TOKEN_KEY", None)
        try:
            self.assertFalse(crypto.is_configured())
            with self.assertRaises(crypto.KeyUnavailable):
                crypto.encrypt("secret", crypto.TOTP_SECRETS)
        finally:
            if saved:
                os.environ["SHIMLINE_SECRET_KEY"] = saved
            if legacy:
                os.environ["QBO_TOKEN_KEY"] = legacy

    def test_a_short_master_key_is_refused(self):
        saved = os.environ["SHIMLINE_SECRET_KEY"]
        os.environ["SHIMLINE_SECRET_KEY"] = "too-short"
        try:
            with self.assertRaises(crypto.KeyUnavailable):
                crypto.master_key()
        finally:
            os.environ["SHIMLINE_SECRET_KEY"] = saved


class MfaFlowTests(unittest.TestCase):
    PASSWORD = "local-test-password-only"

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
        self.user_id = auth.create_user(conn, "admin@example.invalid", "Local Admin",
                                        self.PASSWORD, "owner")
        conn.close()
        self.client = TestClient(service.app, follow_redirects=False)
        self._sign_in()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def _sign_in(self):
        response = self.client.post("/admin/login", data={
            "email": "admin@example.invalid", "password": self.PASSWORD, "next": "/admin"})
        self.csrf = self._csrf()
        return response

    def _next_code(self, secret: str) -> str:
        """A code from the following 30-second step.

        Confirming enrolment spends its counter, and the replay guard then
        refuses that same counter for the rest of the window. That is the
        intended behaviour, so the test moves to the next step rather than
        weakening it. Real operators hit this only if they sign out and back
        in within the same half-minute as enrolling.
        """
        return totp.generate(secret, moment=time.time() + totp.PERIOD)

    def _csrf(self):
        conn = service._db()
        row = conn.execute("SELECT csrf_token FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else None

    def _enrol(self) -> tuple[str, list[str]]:
        self.client.post("/admin/security/mfa/start", data={"csrf_token": self.csrf})
        conn = service._db()
        secret = auth.pending_secret(conn, self.user_id)
        conn.close()
        page = self.client.post("/admin/security/mfa/confirm", data={
            "code": totp.generate(secret), "csrf_token": self.csrf})
        self.assertEqual(page.status_code, 200)
        codes = [line for line in page.text.split("<code>")[1:]]
        codes = [c.split("</code>")[0].strip() for c in codes]
        codes = [c for c in codes if len(c) == 11 and "-" in c]
        self.assertEqual(len(codes), auth.RECOVERY_CODE_COUNT)
        return secret, codes

    # ------------------------------------------------------------ enrolment --

    def test_enrolment_requires_a_working_code_before_it_activates(self):
        self.client.post("/admin/security/mfa/start", data={"csrf_token": self.csrf})
        conn = service._db()
        status = auth.mfa_status(conn, self.user_id)
        conn.close()
        self.assertTrue(status["pending"])
        self.assertFalse(status["enabled"], "a secret alone must not enable MFA")

        rejected = self.client.post("/admin/security/mfa/confirm", data={
            "code": "000000", "csrf_token": self.csrf})
        self.assertEqual(rejected.status_code, 400)
        conn = service._db()
        self.assertFalse(auth.mfa_status(conn, self.user_id)["enabled"])
        conn.close()

        secret, codes = self._enrol()
        conn = service._db()
        self.assertTrue(auth.mfa_status(conn, self.user_id)["enabled"])
        conn.close()

    def test_the_secret_is_stored_encrypted(self):
        secret, _ = self._enrol()
        conn = service._db()
        stored = conn.execute("SELECT totp_secret_enc FROM users WHERE id=?", (self.user_id,)).fetchone()[0]
        conn.close()
        self.assertNotIn(secret, stored)
        self.assertNotIn(secret.encode(), Path(service.DB_PATH).read_bytes())
        self.assertEqual(crypto.decrypt(stored, crypto.TOTP_SECRETS), secret)

    def test_recovery_codes_are_stored_only_as_hashes(self):
        _, codes = self._enrol()
        raw = Path(service.DB_PATH).read_bytes()
        for code in codes:
            self.assertNotIn(code.encode(), raw)

    # ---------------------------------------------------------- login gate --

    def test_password_alone_no_longer_opens_the_workspace(self):
        secret, _ = self._enrol()
        fresh = TestClient(service.app, follow_redirects=False)
        response = fresh.post("/admin/login", data={
            "email": "admin@example.invalid", "password": self.PASSWORD, "next": "/admin"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/mfa")
        # No session cookie yet — only the challenge cookie.
        self.assertIsNone(fresh.cookies.get(auth.COOKIE_NAME))
        self.assertIsNotNone(fresh.cookies.get(auth.MFA_COOKIE_NAME))
        # And the workspace is still shut.
        self.assertEqual(fresh.get("/admin").status_code, 303)

        verified = fresh.post("/admin/mfa", data={"code": self._next_code(secret)})
        self.assertEqual(verified.status_code, 303)
        self.assertEqual(verified.headers["location"], "/admin")
        self.assertEqual(fresh.get("/admin").status_code, 200)
        fresh.close()

    def test_a_wrong_code_does_not_issue_a_session(self):
        self._enrol()
        fresh = TestClient(service.app, follow_redirects=False)
        fresh.post("/admin/login", data={
            "email": "admin@example.invalid", "password": self.PASSWORD, "next": "/admin"})
        denied = fresh.post("/admin/mfa", data={"code": "000000"})
        self.assertEqual(denied.status_code, 401)
        self.assertIsNone(fresh.cookies.get(auth.COOKIE_NAME))
        self.assertEqual(fresh.get("/admin").status_code, 303)
        fresh.close()

    def test_the_challenge_locks_out_after_repeated_wrong_codes(self):
        self._enrol()
        fresh = TestClient(service.app, follow_redirects=False)
        fresh.post("/admin/login", data={
            "email": "admin@example.invalid", "password": self.PASSWORD, "next": "/admin"})
        for _ in range(auth.MFA_MAX_ATTEMPTS):
            fresh.post("/admin/mfa", data={"code": "000000"})
        # The challenge is spent: back to the sign-in page, not another guess.
        exhausted = fresh.post("/admin/mfa", data={"code": "000000"})
        self.assertEqual(exhausted.status_code, 303)
        self.assertEqual(exhausted.headers["location"], "/admin/login")
        fresh.close()

    def test_a_recovery_code_works_once_and_only_once(self):
        _, codes = self._enrol()
        code = codes[0]
        fresh = TestClient(service.app, follow_redirects=False)
        fresh.post("/admin/login", data={
            "email": "admin@example.invalid", "password": self.PASSWORD, "next": "/admin"})
        accepted = fresh.post("/admin/mfa", data={"code": code})
        self.assertEqual(accepted.status_code, 303)
        self.assertEqual(fresh.get("/admin").status_code, 200)
        fresh.close()

        again = TestClient(service.app, follow_redirects=False)
        again.post("/admin/login", data={
            "email": "admin@example.invalid", "password": self.PASSWORD, "next": "/admin"})
        reused = again.post("/admin/mfa", data={"code": code})
        self.assertEqual(reused.status_code, 401)
        again.close()

        conn = service._db()
        self.assertEqual(auth.mfa_status(conn, self.user_id)["recovery_remaining"],
                         auth.RECOVERY_CODE_COUNT - 1)
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_events")]
        conn.close()
        self.assertIn("auth.recovery_code.used", actions)

    def test_the_next_path_survives_the_second_factor(self):
        secret, _ = self._enrol()
        fresh = TestClient(service.app, follow_redirects=False)
        fresh.post("/admin/login", data={
            "email": "admin@example.invalid", "password": self.PASSWORD, "next": "/admin/clients"})
        verified = fresh.post("/admin/mfa", data={"code": self._next_code(secret)})
        self.assertEqual(verified.headers["location"], "/admin/clients")
        fresh.close()

    # ------------------------------------------------------------- disable --

    def test_disabling_costs_both_factors(self):
        secret, _ = self._enrol()
        csrf = self._csrf()
        wrong_password = self.client.post("/admin/security/mfa/disable", data={
            "password": "not-the-password", "code": self._next_code(secret), "csrf_token": csrf})
        self.assertEqual(wrong_password.status_code, 403)
        wrong_code = self.client.post("/admin/security/mfa/disable", data={
            "password": self.PASSWORD, "code": "000000", "csrf_token": csrf})
        self.assertEqual(wrong_code.status_code, 403)
        conn = service._db()
        self.assertTrue(auth.mfa_status(conn, self.user_id)["enabled"])
        conn.close()

        ok = self.client.post("/admin/security/mfa/disable", data={
            "password": self.PASSWORD, "code": self._next_code(secret), "csrf_token": csrf})
        self.assertEqual(ok.status_code, 303)
        conn = service._db()
        status = auth.mfa_status(conn, self.user_id)
        self.assertFalse(status["enabled"])
        self.assertEqual(status["recovery_remaining"], 0, "recovery codes must not outlive MFA")
        conn.close()

    def test_regenerating_recovery_codes_invalidates_the_old_set(self):
        _, first = self._enrol()
        page = self.client.post("/admin/security/recovery-codes", data={"csrf_token": self._csrf()})
        self.assertEqual(page.status_code, 200)
        conn = service._db()
        self.assertEqual(auth.mfa_status(conn, self.user_id)["recovery_remaining"],
                         auth.RECOVERY_CODE_COUNT)
        stale = conn.execute(
            "SELECT COUNT(*) FROM recovery_codes WHERE code_hash=?",
            (auth._recovery_hash(first[0]),)).fetchone()[0]
        conn.close()
        self.assertEqual(stale, 0, "an old code must not survive regeneration")

    def test_security_page_and_mfa_page_need_the_right_state(self):
        anonymous = TestClient(service.app, follow_redirects=False)
        self.assertEqual(anonymous.get("/admin/security").status_code, 303)
        # No challenge in flight, so the code page sends you back to sign in.
        mfa_page = anonymous.get("/admin/mfa")
        self.assertEqual(mfa_page.status_code, 303)
        self.assertEqual(mfa_page.headers["location"], "/admin/login")
        anonymous.close()
        self.assertEqual(self.client.get("/admin/security").status_code, 200)


if __name__ == "__main__":
    unittest.main()
