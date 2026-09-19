"""QuickBooks Online OAuth — the four endpoints Intuit's app review fetches.

This is the front of Phase E. It establishes and stores a connection; it does
not read any accounting data yet. Deliberately kept to `urllib.request` for the
two HTTP calls, matching how the Razorpay integration avoids pulling in an SDK
for a handful of endpoints.

Intuit requires four reachable URLs on the app record:

    /qbo/launch      where a user lands from the app tile inside QuickBooks
    /qbo/connect     starts the authorization redirect
    /qbo/callback    receives the code and exchanges it for tokens
    /qbo/disconnect  where a user lands after disconnecting inside QuickBooks

Safety note carried from the project handoff: develop against Intuit **sandbox**
companies only. There is one real enrolled client, and that client consented to
Aryan as their accountant — not to a third-party application reading their
books. `QBO_ENVIRONMENT` defaults to sandbox and production must be set
deliberately.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import clock, crm, crypto, icons

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["static_version"] = ""
icons.register(templates)


def set_static_version(value: str) -> None:
    templates.env.globals["static_version"] = value

# Intuit publishes its OAuth endpoints in a discovery document and asks apps to
# read them from there rather than hardcode. These values are the fallback for
# when discovery is unreachable — they are what discovery returned on
# 2026-09-08, so a failed fetch degrades to correct behaviour rather than none.
DISCOVERY_URLS = {
    "sandbox": "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration",
    "production": "https://developer.api.intuit.com/.well-known/openid_configuration",
}
FALLBACK_ENDPOINTS = {
    "authorization_endpoint": "https://appcenter.intuit.com/connect/oauth2",
    "token_endpoint": "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
    "revocation_endpoint": "https://developer.api.intuit.com/v2/oauth2/tokens/revoke",
}
DISCOVERY_TTL_SECONDS = 24 * 3600
_discovery: dict = {"fetched_at": 0.0, "endpoints": dict(FALLBACK_ENDPOINTS), "source": "fallback"}

# Kept for readability at call sites and in tests.
AUTHORIZE_URL = FALLBACK_ENDPOINTS["authorization_endpoint"]
TOKEN_URL = FALLBACK_ENDPOINTS["token_endpoint"]
REVOKE_URL = FALLBACK_ENDPOINTS["revocation_endpoint"]

# Transient conditions worth another go. Never retry an authentication
# *decision* — only the transport and the provider being briefly unwell.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
TOKEN_ATTEMPTS = 3

# Access tokens last an hour; refresh well before the edge so a slow call does
# not expire mid-flight. Refresh tokens last ~100 days and rotate on every use,
# so an idle connection is refreshed long before it can lapse.
ACCESS_MARGIN_SECONDS = 10 * 60
REFRESH_KEEPALIVE_DAYS = 30


class ReconnectRequired(Exception):
    """Intuit rejected the grant itself. Retrying cannot help; the customer
    has to authorize again."""

# Read-only accounting is all the diagnostic needs. We do not request OpenID:
# Intuit is not used to sign anyone in to Shimline.
SCOPE = "com.intuit.quickbooks.accounting"

STATE_MINUTES = 15

_settings: dict = {}
_db_factory = None


def configure(*, db_factory, client_id: str, client_secret: str, redirect_uri: str,
              environment: str, token_key: str = "") -> None:
    """`token_key` is accepted and ignored; keys now come from shimline.crypto."""
    global _db_factory, _settings
    _db_factory = db_factory
    _settings = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "environment": "production" if environment == "production" else "sandbox",
    }


def is_configured() -> bool:
    return bool(_settings.get("client_id") and _settings.get("client_secret"))


def _db():
    if _db_factory is None:
        raise RuntimeError("QuickBooks integration is not configured")
    return _db_factory()


# ------------------------------------------------------------------ crypto --
# Key material comes from shimline.crypto, which derives a distinct key per
# purpose from one master secret. A TOTP seed and a refresh token therefore
# never share key material.


def encrypt_token(value: str) -> str:
    try:
        return crypto.encrypt(value, crypto.QUICKBOOKS_TOKENS)
    except crypto.KeyUnavailable as exc:
        raise HTTPException(503, str(exc))


def decrypt_token(value: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return crypto.decrypt(value, crypto.QUICKBOOKS_TOKENS)
    except crypto.KeyUnavailable as exc:
        raise HTTPException(503, str(exc))
    except InvalidToken:
        raise HTTPException(
            500, "Stored QuickBooks token could not be decrypted — has the master key changed?"
        )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- state --

def create_state(conn, organization_id: str, user_id: str | None,
                 client_user_id: str | None = None) -> str:
    state = secrets.token_urlsafe(32)
    expires = clock.now() + timedelta(minutes=STATE_MINUTES)
    conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (clock.format_timestamp(clock.now()),))
    conn.execute(
        "INSERT INTO oauth_states(state_hash,organization_id,created_by_user_id,"
        "created_by_client_id,expires_at) VALUES(?,?,?,?,?)",
        (_hash(state), organization_id, user_id, client_user_id, clock.format_timestamp(expires)),
    )
    conn.commit()
    return state


def consume_state(conn, state: str) -> dict | None:
    """Single use. A replayed state must not mint a second connection."""
    row = conn.execute(
        "SELECT state_hash,organization_id,created_by_user_id,expires_at,used_at,"
        "created_by_client_id FROM oauth_states WHERE state_hash=?", (_hash(state or ""),)
    ).fetchone()
    if not row or row[4] is not None:
        return None
    if clock.parse_timestamp(row[3]) < clock.now():
        conn.execute("DELETE FROM oauth_states WHERE state_hash=?", (row[0],))
        conn.commit()
        return None
    conn.execute("UPDATE oauth_states SET used_at=CURRENT_TIMESTAMP WHERE state_hash=?", (row[0],))
    conn.commit()
    return {"organization_id": row[1], "user_id": row[2], "client_user_id": row[5]}


# ---------------------------------------------------------------- intuit io --

def endpoints(force: bool = False) -> dict:
    """OAuth endpoints, read from Intuit's discovery document and cached.

    Never raises: if discovery is unreachable the last known-good values are
    used, so an Intuit outage cannot take our sign-in flow down with it.
    """
    now = time.time()
    if not force and now - _discovery["fetched_at"] < DISCOVERY_TTL_SECONDS \
            and _discovery["source"] == "discovery":
        return _discovery["endpoints"]
    url = DISCOVERY_URLS.get(_settings.get("environment", "sandbox"), DISCOVERY_URLS["sandbox"])
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            document = json.load(response)
        resolved = {
            key: document[key]
            for key in ("authorization_endpoint", "token_endpoint", "revocation_endpoint")
            if document.get(key)
        }
        if len(resolved) == 3:
            _discovery.update({"fetched_at": now, "endpoints": resolved, "source": "discovery"})
            return resolved
        print(f"[warn] intuit discovery document was missing endpoints: {sorted(document)[:8]}")
    except Exception as exc:
        print(f"[warn] intuit discovery unavailable ({exc}); using known-good endpoints")
    _discovery["fetched_at"] = now
    return _discovery["endpoints"]


def _error_code(body: bytes) -> str:
    try:
        return str(json.loads(body or b"{}").get("error", ""))
    except Exception:
        return ""


def _post_token_request(payload: dict) -> dict:
    """Exchange or refresh a token, retrying only what is worth retrying.

    A failed authentication is not retried: `invalid_grant` means the grant is
    dead and hammering it would only look like an attack. Transport failures
    and 5xx/429 are retried with backoff.
    """
    basic = base64.b64encode(
        f"{_settings['client_id']}:{_settings['client_secret']}".encode()
    ).decode()
    last_detail = "Could not reach QuickBooks"
    for attempt in range(1, TOKEN_ATTEMPTS + 1):
        request = urllib.request.Request(
            endpoints()["token_endpoint"],
            data=urllib.parse.urlencode(payload).encode(),
            method="POST",
            headers={
                "Authorization": "Basic " + basic,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read()[:500]
            code = _error_code(body)
            # Never surface Intuit's raw error body to a browser.
            print(f"[error] intuit token endpoint -> {exc.code} {code or body[:120]!r}")
            if code == "invalid_grant":
                raise ReconnectRequired("QuickBooks rejected the stored authorization")
            if exc.code in RETRYABLE_STATUS and attempt < TOKEN_ATTEMPTS:
                time.sleep(_backoff(attempt))
                continue
            raise HTTPException(502, "QuickBooks rejected the authorization request")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_detail = f"Could not reach QuickBooks ({exc})"
            print(f"[warn] intuit token endpoint attempt {attempt}/{TOKEN_ATTEMPTS}: {exc}")
            if attempt < TOKEN_ATTEMPTS:
                time.sleep(_backoff(attempt))
                continue
        except Exception as exc:
            print(f"[error] intuit token endpoint -> {exc}")
            break
    print(f"[error] intuit token endpoint gave up: {last_detail}")
    raise HTTPException(502, "Could not reach QuickBooks")


def _backoff(attempt: int) -> float:
    """Exponential with jitter, so simultaneous failures do not resynchronise."""
    return min(0.4 * (2 ** (attempt - 1)), 4.0) + random.uniform(0, 0.25)


def _store_tokens(conn, organization_id: str, realm_id: str, tokens: dict, user_id: str | None) -> str:
    now = clock.now()
    access_expires = now + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    refresh_expires = now + timedelta(seconds=int(tokens.get("x_refresh_token_expires_in", 8726400)))
    realm_hash = _hash(realm_id)
    existing = conn.execute(
        "SELECT id FROM connections WHERE provider='quickbooks' AND realm_id_hash=? AND environment=?",
        (realm_hash, _settings["environment"]),
    ).fetchone()
    values = (
        encrypt_token(tokens["access_token"]),
        encrypt_token(tokens["refresh_token"]),
        clock.format_timestamp(access_expires),
        clock.format_timestamp(refresh_expires),
        tokens.get("scope") or SCOPE,
    )
    if existing:
        connection_id = existing[0]
        conn.execute(
            "UPDATE connections SET organization_id=?,access_token_enc=?,refresh_token_enc=?,"
            "access_expires_at=?,refresh_expires_at=?,scope=?,status='active',status_detail=NULL,"
            "connected_by_user_id=COALESCE(?,connected_by_user_id),last_refreshed_at=CURRENT_TIMESTAMP,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (organization_id, *values, user_id, connection_id),
        )
    else:
        connection_id = crm.new_id("con_qbo")
        conn.execute(
            "INSERT INTO connections(id,organization_id,provider,environment,realm_id_enc,realm_id_hash,"
            "access_token_enc,refresh_token_enc,access_expires_at,refresh_expires_at,scope,"
            "connected_by_user_id,last_refreshed_at) "
            "VALUES(?,?,'quickbooks',?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (connection_id, organization_id, _settings["environment"],
             encrypt_token(realm_id), realm_hash, *values, user_id),
        )
    return connection_id


def refresh_connection(conn, connection_id: str) -> dict:
    """Exchange the stored refresh token for a new access token.

    Intuit rotates the refresh token on every use, so the response must be
    written back or the connection is lost on the following refresh.
    """
    row = conn.execute(
        "SELECT organization_id,realm_id_enc,refresh_token_enc,connected_by_user_id FROM connections WHERE id=?",
        (connection_id,),
    ).fetchone()
    if not row or not row[2]:
        raise HTTPException(404, "No stored QuickBooks connection to refresh")
    realm_id = decrypt_token(row[1])
    try:
        tokens = _post_token_request({
            "grant_type": "refresh_token",
            "refresh_token": decrypt_token(row[2]),
        })
    except ReconnectRequired as exc:
        # Permanent: the customer revoked access, or the refresh token lapsed.
        # Clear the dead credentials and say plainly what has to happen next.
        conn.execute(
            "UPDATE connections SET status='revoked',status_detail=?,access_token_enc=NULL,"
            "refresh_token_enc=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            ("QuickBooks rejected the stored authorization; the client must reconnect", connection_id),
        )
        conn.execute(
            "INSERT INTO activities(id,organization_id,kind,body) VALUES(?,?,'system',?)",
            (crm.new_id("act"), row[0],
             "QuickBooks access ended and must be reconnected by the client"),
        )
        conn.execute(
            "INSERT INTO audit_events(id,action,entity_type,entity_id,summary) "
            "VALUES(?,'qbo.reconnect_required','connection',?,?)",
            (crm.new_id("aud"), connection_id, str(exc)),
        )
        conn.commit()
        raise
    except HTTPException:
        # Transient: keep the credentials, flag it, try again on the next run.
        conn.execute(
            "UPDATE connections SET status='error',status_detail='Token refresh failed; will retry',"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?", (connection_id,),
        )
        conn.commit()
        raise
    _store_tokens(conn, row[0], realm_id, tokens, row[3])
    conn.commit()
    return {"connection_id": connection_id, "realm_id": realm_id}


def ensure_access_token(conn, connection_id: str) -> str:
    """A usable access token, refreshed first if it is close to expiring.

    This is what future report-reading code calls instead of reaching for the
    stored token directly, so an expired access token is never the caller's
    problem.
    """
    row = conn.execute(
        "SELECT access_token_enc,access_expires_at,status FROM connections WHERE id=?",
        (connection_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "No such QuickBooks connection")
    if row[2] == "revoked":
        raise ReconnectRequired("This QuickBooks connection was disconnected")
    expires = clock.parse_timestamp(row[1])
    fresh = expires and (expires - clock.now()).total_seconds() > ACCESS_MARGIN_SECONDS
    if fresh and row[0]:
        return decrypt_token(row[0])
    refresh_connection(conn, connection_id)
    row = conn.execute(
        "SELECT access_token_enc FROM connections WHERE id=?", (connection_id,)
    ).fetchone()
    return decrypt_token(row[0])


def connections_due_for_refresh(conn, now=None) -> list[str]:
    """Connections whose refresh token should be exercised before it lapses.

    Intuit rotates the refresh token on every use and expires it after roughly
    100 days. A client who connects once and is read monthly would otherwise
    drop out silently, so anything untouched for REFRESH_KEEPALIVE_DAYS is
    refreshed whether or not anyone needs it.
    """
    now = now or clock.now()
    cutoff = clock.format_timestamp(now - timedelta(days=REFRESH_KEEPALIVE_DAYS))
    rows = conn.execute(
        "SELECT id FROM connections WHERE status IN ('active','error') "
        "AND refresh_token_enc IS NOT NULL "
        "AND COALESCE(last_refreshed_at, created_at) <= ? ORDER BY created_at",
        (cutoff,),
    ).fetchall()
    return [row[0] for row in rows]


def run_keepalive(conn, now=None) -> dict:
    """Refresh every ageing connection. Safe to run daily; safe to rerun."""
    summary = {"checked": 0, "refreshed": 0, "needs_reconnect": 0, "failed": 0}
    for connection_id in connections_due_for_refresh(conn, now):
        summary["checked"] += 1
        try:
            refresh_connection(conn, connection_id)
            summary["refreshed"] += 1
        except ReconnectRequired:
            summary["needs_reconnect"] += 1
        except HTTPException:
            summary["failed"] += 1
    return summary


def revoke_connection(conn, connection_id: str, user_id: str | None) -> None:
    """Disconnect from Shimline's side and tell Intuit to invalidate the token.

    Local state is cleared even if Intuit is unreachable: holding a credential
    we have been told to stop using is worse than a stale record at their end.
    """
    row = conn.execute(
        "SELECT organization_id,refresh_token_enc FROM connections WHERE id=?", (connection_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "No such connection")
    detail = "Disconnected from Shimline"
    if row[1]:
        basic = base64.b64encode(
            f"{_settings['client_id']}:{_settings['client_secret']}".encode()
        ).decode()
        request = urllib.request.Request(
            endpoints()["revocation_endpoint"],
            data=json.dumps({"token": decrypt_token(row[1])}).encode(),
            method="POST",
            headers={"Authorization": "Basic " + basic, "Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=25).read()
        except Exception as exc:
            print(f"[warn] intuit revoke failed: {exc}")
            detail = "Disconnected from Shimline; Intuit revoke call did not complete"
    conn.execute(
        "UPDATE connections SET status='revoked',status_detail=?,access_token_enc=NULL,"
        "refresh_token_enc=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (detail, connection_id),
    )
    conn.execute(
        "INSERT INTO activities(id,organization_id,actor_user_id,kind,body) VALUES(?,?,?,'system',?)",
        (crm.new_id("act"), row[0], user_id, detail),
    )
    conn.execute(
        "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) "
        "VALUES(?,?,'qbo.revoke','connection',?,?)",
        (crm.new_id("aud"), user_id, connection_id, detail),
    )
    conn.commit()


def active_connection(conn, organization_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id,realm_id_enc,environment,status,status_detail,scope,access_expires_at,"
        "refresh_expires_at,last_refreshed_at,last_sync_at,created_at FROM connections "
        "WHERE organization_id=? AND provider='quickbooks' ORDER BY updated_at DESC LIMIT 1",
        (organization_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["realm_id"] = decrypt_token(item.pop("realm_id_enc"))
    return item


# ------------------------------------------------------------------ routes --

def _page(request: Request, template: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request, template, context, status_code=status_code)


@router.get("/qbo/launch", response_class=HTMLResponse)
def launch(request: Request):
    """Where a user lands from the Shimline tile inside QuickBooks.

    Public and unauthenticated by necessity: Intuit sends the browser straight
    here. It explains what the app is and points at a human, because Shimline
    has no client-facing login yet — that is Phase D.
    """
    return _page(request, "qbo_launch.html", environment=_settings.get("environment", "sandbox"))


@router.get("/qbo/connect", response_class=HTMLResponse)
def connect(request: Request):
    """Intuit's "connect / reconnect" URL. Public by necessity.

    It cannot start the authorization itself: the operator's session cookie is
    scoped to /admin, deliberately, so that an operator credential is never
    sent to a public endpoint. Widening that scope to make one link work would
    be a poor trade. Operators start a connection from the client workspace,
    which posts to /admin/clients/{id}/qbo-connect.

    When the Phase D client portal lands this becomes the entry point for an
    expiring, emailed invite link.
    """
    return _page(request, "qbo_connect.html", environment=_settings.get("environment", "sandbox"))


def environment() -> str:
    return _settings.get("environment", "sandbox")


def start_authorization(conn, organization_id: str, user_id: str | None,
                        client_user_id: str | None = None) -> str:
    """Build the Intuit authorization redirect.

    Started either by an operator from the workspace or by the client from
    their own portal. The audit trail records which, because "who authorised
    access to these books" is exactly the question worth being able to answer.
    """
    if not is_configured():
        raise HTTPException(503, "QuickBooks is not configured on this server")
    if not conn.execute("SELECT 1 FROM organizations WHERE id=?", (organization_id,)).fetchone():
        raise HTTPException(404, "Unknown client")
    state = create_state(conn, organization_id, user_id, client_user_id)
    started_by = "the client" if client_user_id else "an operator"
    conn.execute(
        "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) "
        "VALUES(?,?,'qbo.connect.start','organization',?,?)",
        (crm.new_id("aud"), user_id, organization_id,
         f"QuickBooks authorization started by {started_by} ({_settings['environment']})"),
    )
    conn.commit()
    query = urllib.parse.urlencode({
        "client_id": _settings["client_id"],
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": _settings["redirect_uri"],
        "state": state,
    })
    return f"{endpoints()['authorization_endpoint']}?{query}"


@router.get("/qbo/callback")
def callback(request: Request, code: str = "", state: str = "", realmId: str = "",
             error: str = "", error_description: str = ""):
    """Intuit redirects the browser here after the user approves or declines.

    **Always answers with a redirect, never HTML.** Intuit's security
    requirements are explicit: an endpoint that receives authentication tokens
    in URL parameters must not return an HTTP response body, because any
    subresource the page loads would carry the full URL — authorization code
    included — in its Referer header. So this handler does the work and then
    sends the browser somewhere with nothing sensitive in the address.

    Cannot be session-authenticated: the request arrives from Intuit. The
    single-use state row is what proves it began with a signed-in operator.
    """
    if error:
        return _problem("declined")
    if not (code and state and realmId):
        return _problem("incomplete")

    conn = _db()
    try:
        claim = consume_state(conn, state)
        if not claim:
            return _problem("expired")
        tokens = _post_token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _settings["redirect_uri"],
        })
        if not (tokens.get("access_token") and tokens.get("refresh_token")):
            return _problem("no-token")
        claimed_by_client = bool(claim.get("client_user_id"))
        connection_id = _store_tokens(conn, claim["organization_id"], realmId, tokens, claim["user_id"])
        if claimed_by_client:
            conn.execute("UPDATE connections SET connected_by_client_id=? WHERE id=?",
                         (claim["client_user_id"], connection_id))
        conn.execute(
            "INSERT INTO activities(id,organization_id,actor_user_id,kind,body) VALUES(?,?,?,'system',?)",
            (crm.new_id("act"), claim["organization_id"], claim["user_id"],
             f"QuickBooks connected by {'the client' if claimed_by_client else 'an operator'} "
             f"({_settings['environment']})"),
        )
        conn.execute(
            "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) "
            "VALUES(?,?,'qbo.connect.complete','connection',?,?)",
            (crm.new_id("aud"), claim["user_id"], connection_id,
             f"Connected a QuickBooks company ({_settings['environment']})"),
        )
        conn.commit()
        organization_id = claim["organization_id"]
    finally:
        conn.close()
    # Back where it started. The realm id is not put in this URL either — it is
    # customer-identifying and now stored encrypted.
    if claimed_by_client:
        return RedirectResponse("/portal/quickbooks?connected=1", status_code=302)
    return RedirectResponse(f"/admin/clients/{organization_id}?qbo=connected", status_code=302)


def _problem(slug: str) -> RedirectResponse:
    return RedirectResponse(f"/qbo/status?problem={slug}", status_code=302)


PROBLEMS = {
    "declined": "The connection was not approved in QuickBooks, so no access was granted.",
    "incomplete": "That connection link was incomplete. Start again from the client's page in Shimline.",
    "expired": "That connection link has expired or was already used. Start again from the client's page.",
    "no-token": "QuickBooks did not return a usable token. Nothing was stored; please try again.",
}


@router.get("/qbo/status", response_class=HTMLResponse)
def status(request: Request, problem: str = ""):
    """Where /qbo/callback sends a browser when something went wrong.

    Takes only a short slug, never a token, so returning HTML here is safe.
    """
    return _page(request, "qbo_error.html", status_code=200,
                 reason=PROBLEMS.get(problem, PROBLEMS["incomplete"]))


@router.get("/qbo/disconnect", response_class=HTMLResponse)
def disconnect(request: Request, realmId: str = ""):
    """Where a user lands after disconnecting Shimline inside QuickBooks.

    Intuit does not authenticate this redirect, so it is treated as a hint and
    not as authority: it records the event and marks the connection revoked,
    but the trustworthy signal is a failing token refresh. A caller cannot use
    this to reach any other client's data, because nothing is returned about
    the realm beyond confirming the page.
    """
    recorded = False
    if realmId:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT id,organization_id FROM connections WHERE provider='quickbooks' "
                "AND realm_id_hash=? AND environment=?",
                (_hash(realmId), _settings.get("environment", "sandbox")),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE connections SET status='revoked',status_detail='Disconnected inside QuickBooks',"
                    "access_token_enc=NULL,refresh_token_enc=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row[0],),
                )
                conn.execute(
                    "INSERT INTO activities(id,organization_id,kind,body) VALUES(?,?,'system',?)",
                    (crm.new_id("act"), row[1], "QuickBooks was disconnected by the client"),
                )
                conn.execute(
                    "INSERT INTO audit_events(id,action,entity_type,entity_id,summary) "
                    "VALUES(?,'qbo.disconnect','connection',?,'Disconnected inside QuickBooks')",
                    (crm.new_id("aud"), row[0]),
                )
                conn.commit()
                recorded = True
        finally:
            conn.close()
    return _page(request, "qbo_disconnected.html", recorded=recorded)
