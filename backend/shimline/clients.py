"""Client identity and passwordless access.

A client account is created automatically the moment a payment is verified, so
nobody is asked to sign up before they have bought anything. Access is by an
expiring, single-use emailed link that exchanges itself for a session — the
same pattern most modern finance tools use, and the reason there is no password
column anywhere in this file.

Kept separate from `auth.py` throughout. Operator sessions and client sessions
have different lifetimes, different cookie paths, and different powers; sharing
one table would make it possible for a route that only asks "is there a
session?" to accept the wrong kind.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException, Request

from . import clock
from .crm import new_id

COOKIE_NAME = "shimline_client_session"
COOKIE_PATH = "/portal"

# A client interacts rarely — once at intake, then when a review is delivered.
# Short sessions would mean a link request every visit for no security gain,
# since the session only ever reaches their own record.
IDLE_DAYS = 30
ABSOLUTE_DAYS = 90

# Long enough to survive a weekend and a spam folder, short enough that a
# forwarded email stops working.
LINK_HOURS = 336  # 14 days
MAX_ACTIVE_LINKS = 5


def _hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _ip_hash(ip: str) -> str:
    return hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()


def _utc(value: datetime) -> str:
    return clock.format_timestamp(value)


# ---------------------------------------------------------------- identity --

def ensure_client(conn, *, email: str, display_name: str = "",
                  organization_id: str | None = None) -> str:
    """Find or create the account for this email address.

    Idempotent by email, because a client who buys a second review is the same
    client. Never overwrites a name or an organization that is already set —
    an operator's correction outranks whatever was typed at a checkout.
    """
    address = (email or "").strip()
    if not address or "@" not in address:
        raise ValueError("A valid email is required to create a client account")

    row = conn.execute(
        "SELECT id,organization_id,display_name FROM client_users WHERE email=? COLLATE NOCASE",
        (address,),
    ).fetchone()
    if row:
        client_id = row[0]
        if organization_id and not row[1]:
            conn.execute(
                "UPDATE client_users SET organization_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (organization_id, client_id),
            )
        if display_name.strip() and not row[2]:
            conn.execute(
                "UPDATE client_users SET display_name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (display_name.strip()[:200], client_id),
            )
        return client_id

    client_id = new_id("cli")
    conn.execute(
        "INSERT INTO client_users(id,email,organization_id,display_name) VALUES(?,?,?,?)",
        (client_id, address[:254], organization_id, display_name.strip()[:200] or None),
    )
    return client_id


def attach_organization(conn, client_user_id: str, organization_id: str) -> None:
    conn.execute(
        "UPDATE client_users SET organization_id=COALESCE(organization_id,?),"
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (organization_id, client_user_id),
    )


def get_client(conn, client_user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT c.id,c.email,c.display_name,c.organization_id,c.status,c.created_at,c.last_seen_at,"
        "o.name organization_name FROM client_users c "
        "LEFT JOIN organizations o ON o.id=c.organization_id WHERE c.id=?",
        (client_user_id,),
    ).fetchone()
    return dict(row) if row else None


def client_for_organization(conn, organization_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id,email,display_name,status,created_at,last_seen_at FROM client_users "
        "WHERE organization_id=? ORDER BY created_at LIMIT 1",
        (organization_id,),
    ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------ access links --

def issue_access_link(conn, client_user_id: str, *, purpose: str = "sign_in",
                      issued_by: str | None = None) -> str:
    """Mint a single-use link token. Returns the plaintext, stored only as a hash.

    Older unused links for the same person are revoked, so a forwarded or
    leaked link stops working as soon as a fresh one is sent.
    """
    if purpose not in ("sign_in", "connect"):
        raise ValueError("Unknown access link purpose")
    token = secrets.token_urlsafe(32)
    now = clock.now()
    conn.execute(
        "DELETE FROM client_access_tokens WHERE expires_at < ? OR "
        "(client_user_id=? AND used_at IS NULL AND purpose=?)",
        (_utc(now), client_user_id, purpose),
    )
    conn.execute(
        "INSERT INTO client_access_tokens(token_hash,client_user_id,purpose,issued_by_user_id,expires_at) "
        "VALUES(?,?,?,?,?)",
        (_hash(token), client_user_id, purpose, issued_by, _utc(now + timedelta(hours=LINK_HOURS))),
    )
    return token


def redeem_access_link(conn, token: str) -> dict | None:
    """Spend a link token. Single use: a second attempt gets nothing."""
    row = conn.execute(
        "SELECT t.token_hash,t.client_user_id,t.purpose,t.expires_at,t.used_at,c.status "
        "FROM client_access_tokens t JOIN client_users c ON c.id=t.client_user_id "
        "WHERE t.token_hash=?",
        (_hash(token),),
    ).fetchone()
    if not row or row[4] is not None or row[5] != "active":
        return None
    if row[3] <= _utc(clock.now()):
        conn.execute("DELETE FROM client_access_tokens WHERE token_hash=?", (row[0],))
        conn.commit()
        return None
    conn.execute(
        "UPDATE client_access_tokens SET used_at=CURRENT_TIMESTAMP WHERE token_hash=?", (row[0],)
    )
    return {"client_user_id": row[1], "purpose": row[2]}


# --------------------------------------------------------------- sessions --

def create_session(conn, client_user_id: str, ip: str, user_agent: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = clock.now()
    conn.execute(
        "INSERT INTO client_sessions(token_hash,client_user_id,csrf_token,expires_at,"
        "absolute_expires_at,ip_hash,user_agent) VALUES(?,?,?,?,?,?,?)",
        (_hash(token), client_user_id, csrf, _utc(now + timedelta(days=IDLE_DAYS)),
         _utc(now + timedelta(days=ABSOLUTE_DAYS)), _ip_hash(ip), (user_agent or "")[:300]),
    )
    conn.execute(
        "UPDATE client_users SET last_seen_at=CURRENT_TIMESTAMP WHERE id=?", (client_user_id,)
    )
    return token, csrf


def get_session(conn, request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        return None
    now_text = _utc(clock.now())
    row = conn.execute(
        "SELECT s.token_hash,s.client_user_id,s.csrf_token,s.expires_at,s.absolute_expires_at,"
        "c.email,c.display_name,c.organization_id,c.status "
        "FROM client_sessions s JOIN client_users c ON c.id=s.client_user_id "
        "WHERE s.token_hash=?",
        (_hash(token),),
    ).fetchone()
    if not row or row[8] != "active" or row[3] <= now_text or row[4] <= now_text:
        if row:
            conn.execute("DELETE FROM client_sessions WHERE token_hash=?", (row[0],))
            conn.commit()
        return None
    return {
        "token_hash": row[0], "client_user_id": row[1], "csrf_token": row[2],
        "email": row[5], "display_name": row[6], "organization_id": row[7],
    }


def require_csrf(session: dict, supplied: str) -> None:
    if not supplied or not hmac.compare_digest(session["csrf_token"], supplied):
        raise HTTPException(403, "This form expired. Reload the page and try again.")


def destroy_session(conn, request: Request) -> None:
    token = request.cookies.get(COOKIE_NAME, "")
    if token:
        conn.execute("DELETE FROM client_sessions WHERE token_hash=?", (_hash(token),))
        conn.commit()


def revoke_all_sessions(conn, client_user_id: str) -> None:
    conn.execute("DELETE FROM client_sessions WHERE client_user_id=?", (client_user_id,))
    conn.execute("DELETE FROM client_access_tokens WHERE client_user_id=?", (client_user_id,))
