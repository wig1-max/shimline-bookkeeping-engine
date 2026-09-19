"""The client's side of Shimline.

Deliberately small. A client should arrive from an emailed link, see one screen
that says what we need, choose how to send it, and leave. There is no password,
no dashboard and no navigation, because a client interacts with a bookkeeping
service a handful of times a year and every extra surface is one more thing to
support and secure.

Everything here is scoped to the signed-in client's own record. A session
carries no ability to name another client — the identifiers come from the
session, never from the request.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import clients, crm, icons, quickbooks

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["static_version"] = ""
icons.register(templates)


def set_static_version(value: str) -> None:
    templates.env.globals["static_version"] = value


_db_factory = None
_cookie_secure = True


def configure(*, db_factory, cookie_secure: bool = True) -> None:
    global _db_factory, _cookie_secure
    _db_factory = db_factory
    _cookie_secure = cookie_secure


def _db():
    if _db_factory is None:
        raise RuntimeError("The client portal is not configured")
    return _db_factory()


def _page(request: Request, template: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _session_or_none(request: Request, conn):
    return clients.get_session(conn, request)


# ------------------------------------------------------------- getting in --

@router.get("/portal/enter")
def enter(request: Request, t: str = ""):
    """Exchange an emailed link for a session, then redirect.

    Redirects rather than rendering, so the link token never sits in the
    address bar of a page that loads subresources — the same reasoning as the
    QuickBooks callback.
    """
    if not t:
        return RedirectResponse("/portal/expired", status_code=302)
    conn = _db()
    try:
        claim = clients.redeem_access_link(conn, t)
        if not claim:
            conn.commit()
            return RedirectResponse("/portal/expired", status_code=302)
        ip = request.client.host if request.client else "unknown"
        token, _ = clients.create_session(
            conn, claim["client_user_id"], ip, request.headers.get("user-agent", "")
        )
        conn.commit()
    finally:
        conn.close()
    destination = "/portal/quickbooks" if claim["purpose"] == "connect" else "/portal"
    response = RedirectResponse(destination, status_code=302)
    response.set_cookie(
        clients.COOKIE_NAME, token, max_age=clients.ABSOLUTE_DAYS * 86400,
        secure=_cookie_secure, httponly=True, samesite="lax", path=clients.COOKIE_PATH,
    )
    return response


@router.get("/portal/expired", response_class=HTMLResponse)
def expired(request: Request):
    return _page(request, "portal_expired.html")


@router.post("/portal/leave")
def leave(request: Request):
    conn = _db()
    clients.destroy_session(conn, request)
    conn.close()
    response = RedirectResponse("/portal/expired", status_code=303)
    response.delete_cookie(clients.COOKIE_NAME, path=clients.COOKIE_PATH)
    return response


# ------------------------------------------------------------- the screen --

@router.get("/portal", response_class=HTMLResponse)
def home(request: Request):
    conn = _db()
    session = _session_or_none(request, conn)
    if not session:
        conn.close()
        return _page(request, "portal_access.html")

    client = clients.get_client(conn, session["client_user_id"])
    organization_id = client.get("organization_id")
    connection = quickbooks.active_connection(conn, organization_id) if organization_id else None
    submissions = []
    if organization_id:
        for row in conn.execute(
            "SELECT id,created_at,files FROM submissions WHERE organization_id=? "
            "ORDER BY created_at DESC LIMIT 10", (organization_id,)
        ):
            item = dict(row)
            item["files_list"] = [n for n in (item.get("files") or "").split(",") if n]
            submissions.append(item)
    engagement = None
    if organization_id:
        # The professional of record is read from the engagement's own columns
        # rather than joined through firms and users. It is a disclosure: what
        # the client was told has to keep reading as what the client was told,
        # after the person leaves or the firm renames itself.
        row = conn.execute(
            "SELECT id,title,status,due_at,professional_name,"
            "professional_firm_name,professional_disclosed_at "
            "FROM engagements WHERE organization_id=? "
            "ORDER BY created_at DESC LIMIT 1", (organization_id,)
        ).fetchone()
        engagement = dict(row) if row else None
    conn.close()

    connected = bool(connection and connection.get("status") == "active")
    return _page(request, "portal_home.html", client=client, connection=connection,
                 connected=connected, submissions=submissions, engagement=engagement,
                 qbo_ready=quickbooks.is_configured(), session_csrf=session["csrf_token"])


@router.get("/portal/quickbooks", response_class=HTMLResponse)
def quickbooks_page(request: Request):
    conn = _db()
    session = _session_or_none(request, conn)
    if not session:
        conn.close()
        return RedirectResponse("/portal/expired", status_code=302)
    client = clients.get_client(conn, session["client_user_id"])
    organization_id = client.get("organization_id")
    connection = quickbooks.active_connection(conn, organization_id) if organization_id else None
    conn.close()
    return _page(request, "portal_quickbooks.html", client=client, connection=connection,
                 connected=bool(connection and connection.get("status") == "active"),
                 qbo_ready=quickbooks.is_configured(),
                 environment=quickbooks.environment(),
                 session_csrf=session["csrf_token"])


@router.post("/portal/quickbooks/connect")
def start_connect(request: Request, csrf_token: str = Form(...)):
    """Client-initiated QuickBooks authorization.

    The organization comes from the session, never from the request, so a
    client cannot start an authorization against somebody else's record.
    """
    conn = _db()
    session = _session_or_none(request, conn)
    if not session:
        conn.close()
        return RedirectResponse("/portal/expired", status_code=302)
    try:
        clients.require_csrf(session, csrf_token)
        client = clients.get_client(conn, session["client_user_id"])
        organization_id = client.get("organization_id")
        if not organization_id:
            raise HTTPException(409, "Your account is not linked to a business yet. "
                                     "Reply to our email and we will sort it out.")
        target = quickbooks.start_authorization(
            conn, organization_id, user_id=None, client_user_id=session["client_user_id"]
        )
        conn.execute(
            "INSERT INTO activities(id,organization_id,kind,body) VALUES(?,?,'system',?)",
            (crm.new_id("act"), organization_id, "Client started connecting QuickBooks"),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(target, status_code=303)


@router.post("/portal/quickbooks/disconnect")
def stop_connect(request: Request, csrf_token: str = Form(...)):
    conn = _db()
    session = _session_or_none(request, conn)
    if not session:
        conn.close()
        return RedirectResponse("/portal/expired", status_code=302)
    try:
        clients.require_csrf(session, csrf_token)
        client = clients.get_client(conn, session["client_user_id"])
        connection = quickbooks.active_connection(conn, client.get("organization_id"))
        if connection:
            quickbooks.revoke_connection(conn, connection["id"], user_id=None)
    finally:
        conn.close()
    return RedirectResponse("/portal/quickbooks", status_code=303)
