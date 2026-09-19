"""Password authentication and revocable server-side sessions."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from pwdlib import PasswordHash

from . import crypto, totp
from .crm import new_id

COOKIE_NAME = "shimline_admin_session"
IDLE_HOURS = 12
ABSOLUTE_DAYS = 7
MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW_MINUTES = 15
MFA_COOKIE_NAME = "shimline_admin_mfa"
MFA_CHALLENGE_MINUTES = 10
MFA_MAX_ATTEMPTS = 6
RECOVERY_CODE_COUNT = 10
_password_hash = PasswordHash.recommended()
_dummy_hash = _password_hash.hash("shimline-dummy-password-for-timing-only")


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ip_hash(ip: str) -> str:
    return hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()


def create_user(conn, email: str, display_name: str, password: str, role: str = "owner") -> str:
    email = email.strip().casefold()
    if not email or "@" not in email:
        raise ValueError("A valid email is required")
    if len(password) < 14:
        raise ValueError("Password must be at least 14 characters")
    role_row = conn.execute("SELECT id FROM roles WHERE id=?", (role,)).fetchone()
    if not role_row:
        raise ValueError("Unknown role")
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id,email,display_name,password_hash) VALUES(?,?,?,?)",
        (user_id, email, display_name.strip() or email, _password_hash.hash(password)),
    )
    conn.execute("INSERT INTO user_roles(user_id,role_id) VALUES(?,?)", (user_id, role))
    conn.commit()
    return user_id


def authenticate(conn, email: str, password: str, ip: str) -> dict | None:
    now = datetime.now(timezone.utc)
    cutoff = _utc_text(now - timedelta(minutes=ATTEMPT_WINDOW_MINUTES))
    ip_digest = _ip_hash(ip)
    failures = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip_hash=? AND succeeded=0 AND attempted_at>=?",
        (ip_digest, cutoff),
    ).fetchone()[0]
    if failures >= MAX_FAILED_ATTEMPTS:
        raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.")

    email_key = (email or "").strip().casefold()
    row = conn.execute(
        "SELECT id,email,display_name,password_hash,active FROM users WHERE email=? COLLATE NOCASE",
        (email_key,),
    ).fetchone()
    stored_hash = row[3] if row else _dummy_hash
    valid = _password_hash.verify(password or "", stored_hash)
    succeeded = bool(row and row[4] and valid)
    conn.execute(
        "INSERT INTO login_attempts(email,ip_hash,succeeded) VALUES(?,?,?)",
        (email_key[:254], ip_digest, int(succeeded)),
    )
    conn.execute("DELETE FROM login_attempts WHERE attempted_at < datetime('now','-30 days')")
    if succeeded:
        conn.execute("UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (row[0],))
    conn.commit()
    if not succeeded:
        return None
    return {"id": row[0], "email": row[1], "display_name": row[2]}


def create_session(conn, user_id: str, ip: str, user_agent: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,absolute_expires_at,ip_hash,user_agent) "
        "VALUES(?,?,?,?,?,?,?)",
        (_token_hash(token), user_id, csrf, _utc_text(now + timedelta(hours=IDLE_HOURS)),
         _utc_text(now + timedelta(days=ABSOLUTE_DAYS)), _ip_hash(ip), (user_agent or "")[:300]),
    )
    conn.commit()
    return token, csrf


def get_session(conn, request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        return None
    now = datetime.now(timezone.utc)
    now_text = _utc_text(now)
    row = conn.execute(
        "SELECT s.token_hash,s.user_id,s.csrf_token,s.last_seen_at,s.expires_at,s.absolute_expires_at,"
        "u.email,u.display_name,u.active,GROUP_CONCAT(ur.role_id) "
        "FROM sessions s JOIN users u ON u.id=s.user_id "
        "LEFT JOIN user_roles ur ON ur.user_id=u.id WHERE s.token_hash=? GROUP BY s.token_hash",
        (_token_hash(token),),
    ).fetchone()
    if not row or not row[8] or row[4] <= now_text or row[5] <= now_text:
        if row:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (row[0],))
            conn.commit()
        return None
    last_seen = datetime.strptime(row[3][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if now - last_seen > timedelta(minutes=5):
        conn.execute(
            "UPDATE sessions SET last_seen_at=CURRENT_TIMESTAMP,expires_at=? WHERE token_hash=?",
            (_utc_text(min(now + timedelta(hours=IDLE_HOURS),
                           datetime.strptime(row[5][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))), row[0]),
        )
        conn.commit()
    return {
        "token_hash": row[0], "user_id": row[1], "csrf_token": row[2],
        "email": row[6], "display_name": row[7],
        "roles": set((row[9] or "").split(",")),
    }


def require_session(conn, request: Request) -> dict:
    session = get_session(conn, request)
    if not session:
        raise HTTPException(401, "Sign in required")
    return session


def require_csrf(session: dict, supplied: str) -> None:
    if not supplied or not hmac.compare_digest(session["csrf_token"], supplied):
        raise HTTPException(403, "This form expired. Refresh the page and try again.")


def has_role(session: dict, *allowed: str) -> bool:
    return bool(session.get("roles", set()) & set(allowed))


def require_roles(session: dict, *allowed: str) -> None:
    """Gate an action on role.

    Roles have existed as rows since the first migration but nothing enforced
    them, which meant one signed-in session could both approve a bookkeeping
    proposal and release it to the provider. Approval and execution are the two
    actions that must never collapse together, so they are gated here.
    """
    if not has_role(session, *allowed):
        raise HTTPException(
            403, "Your account does not hold a role permitted to do this "
                 f"(needs one of: {', '.join(sorted(allowed))}).")


def destroy_session(conn, request: Request) -> None:
    token = request.cookies.get(COOKIE_NAME, "")
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
        conn.commit()



# ----------------------------------------------------------------- two-factor --
#
# A password alone must not open the workspace once MFA is on. `authenticate`
# still proves the password; everything below decides whether a session is
# issued now or only after a second factor.


def mfa_status(conn, user_id: str) -> dict:
    row = conn.execute(
        "SELECT totp_secret_enc,totp_confirmed_at FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not row:
        return {"enabled": False, "pending": False, "recovery_remaining": 0}
    remaining = conn.execute(
        "SELECT COUNT(*) FROM recovery_codes WHERE user_id=? AND used_at IS NULL", (user_id,)
    ).fetchone()[0]
    return {
        "enabled": bool(row[0] and row[1]),
        "pending": bool(row[0] and not row[1]),
        "recovery_remaining": remaining,
    }


def begin_totp_enrolment(conn, user_id: str) -> str:
    """Store an unconfirmed secret and return it for the authenticator app.

    Unconfirmed on purpose: a secret nobody has proved they can generate codes
    from must never be able to lock an operator out.
    """
    secret = totp.generate_secret()
    conn.execute(
        "UPDATE users SET totp_secret_enc=?,totp_confirmed_at=NULL,totp_last_counter=NULL WHERE id=?",
        (crypto.encrypt(secret, crypto.TOTP_SECRETS), user_id),
    )
    conn.commit()
    return secret


def pending_secret(conn, user_id: str) -> str | None:
    row = conn.execute(
        "SELECT totp_secret_enc,totp_confirmed_at FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not row or not row[0] or row[1]:
        return None
    return crypto.decrypt(row[0], crypto.TOTP_SECRETS)


def confirm_totp_enrolment(conn, user_id: str, code: str) -> list[str] | None:
    """Activate MFA once the operator proves the app is working.

    Returns the recovery codes, shown exactly once.
    """
    secret = pending_secret(conn, user_id)
    if not secret:
        return None
    counter = totp.verify(secret, code)
    if counter is None:
        return None
    conn.execute(
        "UPDATE users SET totp_confirmed_at=CURRENT_TIMESTAMP,totp_last_counter=? WHERE id=?",
        (counter, user_id),
    )
    codes = _issue_recovery_codes(conn, user_id)
    conn.commit()
    return codes


def disable_totp(conn, user_id: str) -> None:
    conn.execute(
        "UPDATE users SET totp_secret_enc=NULL,totp_confirmed_at=NULL,totp_last_counter=NULL WHERE id=?",
        (user_id,),
    )
    conn.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
    conn.commit()


def _recovery_hash(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").replace(" ", "").upper().encode()).hexdigest()


def _issue_recovery_codes(conn, user_id: str) -> list[str]:
    conn.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
    codes = []
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters
    while len(codes) < RECOVERY_CODE_COUNT:
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        code = f"{raw[:5]}-{raw[5:]}"
        if code in codes:
            continue
        codes.append(code)
        conn.execute(
            "INSERT INTO recovery_codes(user_id,code_hash) VALUES(?,?)",
            (user_id, _recovery_hash(code)),
        )
    return codes


def regenerate_recovery_codes(conn, user_id: str) -> list[str]:
    codes = _issue_recovery_codes(conn, user_id)
    conn.commit()
    return codes


def create_mfa_challenge(conn, user_id: str, ip: str, next_path: str) -> str:
    """Half-authenticated state. Grants nothing but the right to be asked."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    conn.execute("DELETE FROM mfa_challenges WHERE expires_at < ?", (_utc_text(now),))
    conn.execute(
        "INSERT INTO mfa_challenges(token_hash,user_id,ip_hash,next_path,expires_at) VALUES(?,?,?,?,?)",
        (_token_hash(token), user_id, _ip_hash(ip), next_path,
         _utc_text(now + timedelta(minutes=MFA_CHALLENGE_MINUTES))),
    )
    conn.commit()
    return token


def get_mfa_challenge(conn, token: str) -> dict | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT c.token_hash,c.user_id,c.attempts,c.next_path,c.expires_at,u.email,u.display_name "
        "FROM mfa_challenges c JOIN users u ON u.id=c.user_id WHERE c.token_hash=?",
        (_token_hash(token),),
    ).fetchone()
    if not row:
        return None
    if row[4] <= _utc_text(datetime.now(timezone.utc)) or row[2] >= MFA_MAX_ATTEMPTS:
        conn.execute("DELETE FROM mfa_challenges WHERE token_hash=?", (row[0],))
        conn.commit()
        return None
    return {
        "token_hash": row[0], "user_id": row[1], "attempts": row[2],
        "next_path": row[3], "email": row[5], "display_name": row[6],
    }


def complete_mfa_challenge(conn, token: str, code: str) -> dict | None:
    """Verify a TOTP code or a recovery code. Consumes the challenge on success."""
    challenge = get_mfa_challenge(conn, token)
    if not challenge:
        return None
    user_id = challenge["user_id"]
    row = conn.execute(
        "SELECT totp_secret_enc,totp_last_counter FROM users WHERE id=?", (user_id,)
    ).fetchone()

    accepted = False
    method = ""
    if row and row[0]:
        counter = totp.verify(crypto.decrypt(row[0], crypto.TOTP_SECRETS), code,
                              last_counter=row[1])
        if counter is not None:
            conn.execute("UPDATE users SET totp_last_counter=? WHERE id=?", (counter, user_id))
            accepted, method = True, "totp"

    if not accepted:
        used = conn.execute(
            "SELECT id FROM recovery_codes WHERE user_id=? AND code_hash=? AND used_at IS NULL",
            (user_id, _recovery_hash(code or "")),
        ).fetchone()
        if used:
            conn.execute("UPDATE recovery_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?", (used[0],))
            accepted, method = True, "recovery_code"

    if not accepted:
        conn.execute(
            "UPDATE mfa_challenges SET attempts=attempts+1 WHERE token_hash=?",
            (challenge["token_hash"],),
        )
        conn.commit()
        return None

    conn.execute("DELETE FROM mfa_challenges WHERE token_hash=?", (challenge["token_hash"],))
    conn.commit()
    return {"user_id": user_id, "method": method, "next_path": challenge["next_path"]}


def destroy_mfa_challenge(conn, token: str) -> None:
    if token:
        conn.execute("DELETE FROM mfa_challenges WHERE token_hash=?", (_token_hash(token),))
        conn.commit()
