"""Server-rendered Shimline operations workspace."""
from __future__ import annotations

import re
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import (
    auth,
    clock,
    crm,
    crypto,
    filing_periods,
    first_contact,
    gst_return,
    icons,
    measurement,
    portfolio,
    priority,
    provenance,
    qbo_exports,
    qbo_reports,
    quickbooks,
    readiness,
    report_pdf,
    reporting,
    statement_store,
    tax_rates,
    tenancy,
    totp,
    work_engine,
)

# Aliased: this module defines route functions called `clients` and `calendar`,
# which would otherwise shadow these at module scope. The failure is quiet --
# the name resolves to the route function and only breaks when it is called.
import calendar as calendar_module

from . import clients as client_accounts
from .qbo_adapter import QBOAdapter
from .vocabulary import ALL as VOCABULARIES
from .vocabulary import ENGAGEMENT, LIFECYCLE, OPPORTUNITY, WORK_ITEM

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

PER_PAGE = 25

# The domain vocabulary lives in shimline/vocabulary.py. These names are kept
# so call sites read naturally; the definitions are not duplicated here.
LIFECYCLE_STAGES = LIFECYCLE.codes()
OPPORTUNITY_STAGES = OPPORTUNITY.codes()
WORK_ITEM_STATUSES = WORK_ITEM.codes()
# Draft and awaiting_payment are real states but an operator cannot select
# them: they are set by the payment flow, never by hand.
ENGAGEMENT_STATUSES = tuple(
    stage.code for stage in ENGAGEMENT if stage.code not in ("draft", "awaiting_payment")
)

_db_factory = None
_uploads_dir = None
_retention_after_close = 30
_retention_unclosed = 90
_cookie_secure = True
_pdf_renderer = None


_portal_base = ""


def configure(*, db_factory, uploads_dir, retention_after_close: int,
              retention_unclosed: int, cookie_secure: bool = True,
              portal_base: str = "", pdf_renderer=None) -> None:
    global _db_factory, _uploads_dir, _retention_after_close, _retention_unclosed
    global _cookie_secure, _portal_base, _pdf_renderer
    _db_factory = db_factory
    _uploads_dir = uploads_dir
    _retention_after_close = retention_after_close
    _retention_unclosed = retention_unclosed
    _cookie_secure = cookie_secure
    _portal_base = portal_base.rstrip("/")
    _pdf_renderer = pdf_renderer


def _db():
    if _db_factory is None:
        raise RuntimeError("Admin workspace is not configured")
    return _db_factory()


def _session(request: Request, conn):
    """The session, told which house it is standing in.

    Every authenticated page goes through here, so this is the one place that
    can label a firm accountant correctly without touching twenty routes. An
    accountant at Alder & Co seeing "Shimline operations" in the corner would
    reasonably conclude they were inside somebody else's workspace.
    """
    session = auth.get_session(conn, request)
    if not session:
        return session
    membership = conn.execute(
        "SELECT f.id, f.name FROM firm_members m JOIN firms f ON f.id = m.firm_id "
        "WHERE m.user_id = ?", (session["user_id"],)).fetchone()
    session["firm_id"] = membership[0] if membership else None
    session["workspace_label"] = membership[1] if membership else "Shimline operations"
    return session


def _redirect_login(request: Request):
    next_path = request.url.path if request.url.path.startswith("/admin") else "/admin"
    return RedirectResponse(f"/admin/login?next={next_path}", status_code=303)


def _authorized_post(conn, request: Request, csrf_token: str) -> dict:
    try:
        session = auth.require_session(conn, request)
        auth.require_csrf(session, csrf_token)
        return session
    except Exception:
        conn.close()
        raise


def _scope(conn, session: dict | None):
    """What this session may see. Resolved once per request, from the database."""
    return tenancy.scope_for(conn, session)


def _scoped(conn, session, organization_id, what: str = "Not found") -> str:
    """Confirm this session may see this client, or 404.

    Never 403: that would confirm the record exists, and an outsider could
    enumerate client ids by watching which ones answer differently. Absence and
    denial have to be indistinguishable from outside the boundary.

    Closes the connection on refusal, the same contract `_authorized_post`
    keeps, so a route may call it as a single bare statement without needing a
    try block of its own. Closing twice is a no-op, so routes that already have
    one are unaffected.
    """
    try:
        return tenancy.require_organization(_scope(conn, session), organization_id, what)
    except Exception:
        conn.close()
        raise


def _scoped_engagement(conn, session, engagement_id: str) -> str:
    return _scoped(conn, session,
                   tenancy.organization_of_engagement(conn, engagement_id),
                   "Engagement not found")


def _scoped_run(conn, session, run_id: str) -> str:
    return _scoped(conn, session, tenancy.organization_of_run(conn, run_id),
                   "Bookkeeping run not found")


def _scoped_proposal(conn, session, proposal_id: str) -> str:
    return _scoped(conn, session,
                   tenancy.organization_of_proposal(conn, proposal_id),
                   "Proposal not found")


def _audit(conn, session: dict, action: str, entity_type: str, entity_id: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) VALUES(?,?,?,?,?,?)",
        (crm.new_id("aud"), session["user_id"], action, entity_type, entity_id, summary),
    )


_static_version = ""


def set_static_version(value: str) -> None:
    """Cache-busting suffix so a hard-cached stylesheet still updates on deploy."""
    global _static_version
    _static_version = value
    templates.env.globals["static_version"] = value


templates.env.globals["static_version"] = ""
templates.env.globals["vocab"] = VOCABULARIES
icons.register(templates)


def _base_context(request: Request, session: dict, active: str, **extra):
    context = {
        "request": request,
        "session": session,
        "active": active,
        "today": clock.to_business(clock.now()),
    }
    context.update(extra)
    return context


def _display_date(value, include_year: bool = False) -> str:
    """Dates are shown on the Canadian business calendar, never in UTC."""
    parsed = clock.parse_timestamp(value)
    if not parsed:
        return "—"
    local = clock.to_business(parsed)
    rendered = local.strftime("%b %d, %Y" if include_year else "%b %d")
    return rendered.replace(" 0", " ")


def _display_datetime(value) -> str:
    parsed = clock.parse_timestamp(value)
    if not parsed:
        return "—"
    local = clock.to_business(parsed)
    return local.strftime("%b %d, %Y at %H:%M").replace(" 0", " ")


def _relative_due(value) -> tuple[str, str]:
    """A human due label counted in business-local calendar days."""
    due = clock.parse_timestamp(value)
    if not due:
        return "No due date", "neutral"
    days = clock.days_until(due)
    if days < 0:
        magnitude = abs(days)
        return f"{magnitude}d overdue" if magnitude > 1 else "1d overdue", "overdue"
    if days == 0:
        return "due today", "overdue" if due < clock.now() else "soon"
    if days == 1:
        return "due tomorrow", "soon"
    return f"in {days}d", "soon" if days <= 7 else "on-track"


def _form_date(value) -> str:
    """Value for an <input type="date">, on the business calendar."""
    parsed = clock.parse_timestamp(value)
    return clock.to_business(parsed).strftime("%Y-%m-%d") if parsed else ""


def _parse_form_date(value: str) -> str | None:
    """Read an <input type="date"> as end of that Canadian business day."""
    value = (value or "").strip()
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(400, "Dates must look like YYYY-MM-DD")
    from datetime import datetime as _dt
    try:
        day = _dt.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "That is not a real date")
    local = _dt.combine(day, clock.BUSINESS_DAY_END, tzinfo=clock.BUSINESS_TZ)
    return clock.format_timestamp(local)


templates.env.filters["display_date"] = _display_date
templates.env.filters["display_datetime"] = _display_datetime
templates.env.filters["form_date"] = _form_date


def _paging(page: int, total: int) -> dict:
    """Every list in the workspace is bounded; unbounded queries are a bug."""
    page = max(page, 1)
    pages = max(-(-total // PER_PAGE), 1)
    page = min(page, pages)
    return {
        "page": page, "pages": pages, "total": total,
        "offset": (page - 1) * PER_PAGE, "per_page": PER_PAGE,
        "has_prev": page > 1, "has_next": page < pages,
        "first": 0 if total == 0 else (page - 1) * PER_PAGE + 1,
        "last": min(page * PER_PAGE, total),
    }


def _users(conn) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT id,display_name,email FROM users WHERE active=1 ORDER BY display_name"
    )]


def _retention(submission: dict | None) -> dict | None:
    """When the published deletion promise removes this submission's files."""
    if not submission:
        return None
    closed = clock.parse_timestamp(submission.get("closed_at"))
    created = clock.parse_timestamp(submission.get("created_at"))
    if closed:
        deletes_at = closed + timedelta(days=_retention_after_close)
        basis = f"{_retention_after_close} days after the engagement closed"
    elif created:
        deletes_at = created + timedelta(days=_retention_unclosed)
        basis = f"{_retention_unclosed} days after intake, because no engagement has closed"
    else:
        return None
    days = clock.days_until(deletes_at)
    return {
        "deletes_at": clock.format_timestamp(deletes_at),
        "basis": basis,
        "days_remaining": days,
        "running": bool(closed),
        "tone": "overdue" if days <= 7 else ("soon" if days <= 30 else "on-track"),
    }


def _decorate_engagement(conn, row) -> dict:
    item = dict(row)
    due = clock.parse_timestamp(item.get("due_at"))
    label, tone = _relative_due(item.get("due_at"))
    item["due_label"] = label
    item["due_tone"] = tone
    item["overdue"] = bool(due and due < clock.now())
    item["due_sort"] = due or clock.now().replace(year=9999)
    item["progress_pct"] = round((item.get("work_done") or 0) * 100 / max(item.get("work_total") or 1, 1))
    item["stage_position"] = ENGAGEMENT.position(item.get("status"))
    next_item = conn.execute(
        "SELECT id,title,status FROM work_items WHERE engagement_id=? AND status NOT IN ('done','skipped') "
        "ORDER BY position LIMIT 1", (item["id"],)
    ).fetchone()
    item["next_item"] = dict(next_item) if next_item else None
    return item


def _engagement_rows(conn, scope):
    """Active engagements ranked by service risk, not creation date.

    Takes a scope rather than reading one, because three different pages share
    this query and a listing that forgot to filter would put another firm's
    client names on a queue.
    """
    visible, params = tenancy.and_where(scope, "e.organization_id")
    rows = conn.execute(
        "SELECT e.*,o.name organization_name,u.display_name owner_name,"
        "COUNT(w.id) work_total,SUM(CASE WHEN w.status='done' THEN 1 ELSE 0 END) work_done,"
        # What this relationship has paid, for the client-value signal.
        "(SELECT COALESCE(SUM(p.amount),0) FROM payments p WHERE p.status='paid' AND p.submission_id IN "
        "(SELECT s.id FROM submissions s WHERE s.organization_id=e.organization_id)) client_value_cents "
        "FROM engagements e JOIN organizations o ON o.id=e.organization_id "
        "LEFT JOIN users u ON u.id=e.assigned_user_id "
        "LEFT JOIN work_items w ON w.engagement_id=e.id "
        "WHERE e.status NOT IN ('delivered','closed')" + visible + " GROUP BY e.id",
        params
    ).fetchall()
    return priority.rank([_decorate_engagement(conn, row) for row in rows])


# ------------------------------------------------------------------ sign in --

@router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/admin"):
    conn = _db()
    existing = _session(request, conn)
    conn.close()
    if existing:
        return RedirectResponse("/admin", status_code=303)
    safe_next = next if next.startswith("/admin") and not next.startswith("//") else "/admin"
    return templates.TemplateResponse(request, "login.html", {"next": safe_next, "error": None})


@router.post("/admin/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/admin")):
    conn = _db()
    ip = request.client.host if request.client else "unknown"
    try:
        user = auth.authenticate(conn, email, password, ip)
    except HTTPException as exc:
        conn.close()
        return templates.TemplateResponse(request, "login.html", {
            "next": "/admin", "error": exc.detail,
        }, status_code=exc.status_code)
    if not user:
        conn.close()
        return templates.TemplateResponse(request, "login.html", {
            "next": "/admin", "error": "Email or password is incorrect.",
        }, status_code=401)
    safe_next = next if next.startswith("/admin") and not next.startswith("//") else "/admin"

    # A correct password is only the first factor once MFA is on.
    if auth.mfa_status(conn, user["id"])["enabled"]:
        challenge = auth.create_mfa_challenge(conn, user["id"], ip, safe_next)
        conn.execute(
            "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) "
            "VALUES(?,?,'auth.mfa.challenge','user',?,'Password accepted; second factor required')",
            (crm.new_id("aud"), user["id"], user["id"]),
        )
        conn.commit()
        conn.close()
        response = RedirectResponse("/admin/mfa", status_code=303)
        response.set_cookie(auth.MFA_COOKIE_NAME, challenge,
                            max_age=auth.MFA_CHALLENGE_MINUTES * 60, secure=_cookie_secure,
                            httponly=True, samesite="lax", path="/admin")
        return response

    token, _ = auth.create_session(conn, user["id"], ip, request.headers.get("user-agent", ""))
    conn.execute(
        "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary,request_id) "
        "VALUES(?,?,'auth.login','user',?,'Signed in',?)",
        (crm.new_id("aud"), user["id"], user["id"], request.headers.get("x-request-id")),
    )
    conn.commit()
    conn.close()
    response = RedirectResponse(safe_next, status_code=303)
    response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.ABSOLUTE_DAYS * 86400,
                        secure=_cookie_secure, httponly=True, samesite="lax", path="/admin")
    return response


@router.get("/admin/mfa", response_class=HTMLResponse)
def mfa_page(request: Request, error: str = ""):
    conn = _db()
    challenge = auth.get_mfa_challenge(conn, request.cookies.get(auth.MFA_COOKIE_NAME, ""))
    conn.close()
    if not challenge:
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(request, "mfa_challenge.html", {
        "error": error or None, "email": challenge["email"],
    })


@router.post("/admin/mfa", response_class=HTMLResponse)
def mfa_verify(request: Request, code: str = Form(...)):
    conn = _db()
    token = request.cookies.get(auth.MFA_COOKIE_NAME, "")
    result = auth.complete_mfa_challenge(conn, token, code)
    if not result:
        still_open = auth.get_mfa_challenge(conn, token)
        conn.close()
        if not still_open:
            response = RedirectResponse("/admin/login", status_code=303)
            response.delete_cookie(auth.MFA_COOKIE_NAME, path="/admin")
            return response
        return templates.TemplateResponse(request, "mfa_challenge.html", {
            "error": "That code is not valid. Check your authenticator, or use a recovery code.",
            "email": still_open["email"],
        }, status_code=401)

    ip = request.client.host if request.client else "unknown"
    session_token, _ = auth.create_session(conn, result["user_id"], ip,
                                           request.headers.get("user-agent", ""))
    conn.execute(
        "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) VALUES(?,?,?,'user',?,?)",
        (crm.new_id("aud"), result["user_id"], "auth.login", result["user_id"],
         f"Signed in with two-factor ({result['method']})"),
    )
    if result["method"] == "recovery_code":
        remaining = auth.mfa_status(conn, result["user_id"])["recovery_remaining"]
        conn.execute(
            "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) "
            "VALUES(?,?,'auth.recovery_code.used','user',?,?)",
            (crm.new_id("aud"), result["user_id"], result["user_id"],
             f"Recovery code used; {remaining} remaining"),
        )
    conn.commit()
    conn.close()
    response = RedirectResponse(result["next_path"] or "/admin", status_code=303)
    response.set_cookie(auth.COOKIE_NAME, session_token, max_age=auth.ABSOLUTE_DAYS * 86400,
                        secure=_cookie_secure, httponly=True, samesite="lax", path="/admin")
    response.delete_cookie(auth.MFA_COOKIE_NAME, path="/admin")
    return response


@router.post("/admin/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    conn = _db()
    session = _session(request, conn)
    if session:
        auth.require_csrf(session, csrf_token)
        _audit(conn, session, "auth.logout", "user", session["user_id"], "Signed out")
        auth.destroy_session(conn, request)
    conn.close()
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/admin")
    return response


# ----------------------------------------------------------------- security --

@router.get("/admin/security", response_class=HTMLResponse)
def security(request: Request, enrolling: int = 0):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    status = auth.mfa_status(conn, session["user_id"])
    secret = auth.pending_secret(conn, session["user_id"]) if (enrolling and status["pending"]) else None
    recent = [dict(row) for row in conn.execute(
        "SELECT action,summary,created_at FROM audit_events WHERE actor_user_id=? AND action LIKE 'auth.%' "
        "ORDER BY created_at DESC,rowid DESC LIMIT 8", (session["user_id"],)
    )]
    conn.close()
    return templates.TemplateResponse(request, "security.html", _base_context(
        request, session, "security", status=status, secret=secret,
        secret_display=totp.format_secret(secret) if secret else None,
        provisioning_uri=totp.provisioning_uri(secret, session["email"]) if secret else None,
        codes=None, recent=recent, crypto_ready=crypto.is_configured(),
    ))


@router.post("/admin/security/mfa/start")
def start_mfa(request: Request, csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    if auth.mfa_status(conn, session["user_id"])["enabled"]:
        conn.close()
        raise HTTPException(400, "Two-factor authentication is already on")
    try:
        auth.begin_totp_enrolment(conn, session["user_id"])
    except crypto.KeyUnavailable as exc:
        conn.close()
        raise HTTPException(503, str(exc))
    conn.close()
    return RedirectResponse("/admin/security?enrolling=1", status_code=303)


@router.post("/admin/security/mfa/confirm", response_class=HTMLResponse)
def confirm_mfa(request: Request, code: str = Form(...), csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    codes = auth.confirm_totp_enrolment(conn, session["user_id"], code)
    if codes is None:
        secret = auth.pending_secret(conn, session["user_id"])
        status = auth.mfa_status(conn, session["user_id"])
        conn.close()
        if not secret:
            return RedirectResponse("/admin/security", status_code=303)
        return templates.TemplateResponse(request, "security.html", _base_context(
            request, session, "security", status=status, secret=secret,
            secret_display=totp.format_secret(secret),
            provisioning_uri=totp.provisioning_uri(secret, session["email"]),
            codes=None, recent=[], crypto_ready=True,
            error="That code didn't match. Check your authenticator's clock and try the current code.",
        ), status_code=400)
    _audit(conn, session, "auth.mfa.enabled", "user", session["user_id"],
           "Two-factor authentication enabled")
    conn.commit()
    status = auth.mfa_status(conn, session["user_id"])
    conn.close()
    return templates.TemplateResponse(request, "security.html", _base_context(
        request, session, "security", status=status, secret=None, secret_display=None,
        provisioning_uri=None, codes=codes, recent=[], crypto_ready=True,
    ))


@router.post("/admin/security/mfa/disable")
def disable_mfa(request: Request, password: str = Form(...), code: str = Form(...),
                csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    ip = request.client.host if request.client else "unknown"
    # Turning MFA off is exactly what a stolen session would try, so it costs
    # both remaining factors.
    user = auth.authenticate(conn, session["email"], password, ip)
    if not user:
        conn.close()
        raise HTTPException(403, "That password is not correct")
    token = auth.create_mfa_challenge(conn, session["user_id"], ip, "/admin/security")
    if not auth.complete_mfa_challenge(conn, token, code):
        auth.destroy_mfa_challenge(conn, token)
        conn.close()
        raise HTTPException(403, "That two-factor code is not valid")
    auth.disable_totp(conn, session["user_id"])
    _audit(conn, session, "auth.mfa.disabled", "user", session["user_id"],
           "Two-factor authentication disabled")
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/security", status_code=303)


@router.post("/admin/security/recovery-codes", response_class=HTMLResponse)
def new_recovery_codes(request: Request, csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    status = auth.mfa_status(conn, session["user_id"])
    if not status["enabled"]:
        conn.close()
        raise HTTPException(400, "Two-factor authentication is not enabled")
    codes = auth.regenerate_recovery_codes(conn, session["user_id"])
    _audit(conn, session, "auth.recovery_codes.regenerated", "user", session["user_id"],
           "Recovery codes regenerated; previous codes invalidated")
    conn.commit()
    status = auth.mfa_status(conn, session["user_id"])
    conn.close()
    return templates.TemplateResponse(request, "security.html", _base_context(
        request, session, "security", status=status, secret=None, secret_display=None,
        provisioning_uri=None, codes=codes, recent=[], crypto_ready=True,
    ))


# -------------------------------------------------------------------- today --

@router.get("/admin", response_class=HTMLResponse)
def today(request: Request):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    engagements = _engagement_rows(conn, _scope(conn, session))
    selected = engagements[0] if engagements else None
    queue = engagements[1:5]
    blockers = [item for item in engagements if item["status"] == "awaiting_client"][:3]
    summary = {
        "overdue": sum(item["overdue"] for item in engagements),
        "due_soon": sum(item["due_tone"] == "soon" for item in engagements),
        "waiting": sum(item["status"] == "awaiting_client" for item in engagements),
        "review": sum(item["status"] == "internal_review" for item in engagements),
    }
    conn.close()
    return templates.TemplateResponse(request, "today.html", _base_context(
        request, session, "today", selected=selected, queue=queue, blockers=blockers, summary=summary
    ))


@router.get("/admin/measurement", response_class=HTMLResponse)
def measurement_dashboard(request: Request):
    """Owner-only aggregate view of the consented marketing funnel."""
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    try:
        auth.require_roles(session, "owner")
        report = measurement.summary(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "measurement.html", _base_context(
        request, session, "measurement", report=report
    ))


# ----------------------------------------------------------------- pipeline --

@router.get("/admin/pipeline", response_class=HTMLResponse)
def pipeline(request: Request, q: str = "", stage: str = "", page: int = 1):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    clauses, params = [], []
    if q.strip():
        clauses.append("(o.name LIKE ? OR o.city LIKE ? OR o.specialty LIKE ?)")
        params.extend([f"%{q.strip()}%"] * 3)
    if stage:
        clauses.append("p.stage=?")
        params.append(stage)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    total = conn.execute(
        "SELECT COUNT(*) FROM organizations o JOIN opportunities p ON p.organization_id=o.id " + where, params
    ).fetchone()[0]
    paging = _paging(page, total)
    rows = conn.execute(
        "SELECT o.id,o.name,o.city,o.specialty,o.fit_tier,o.buying_signal,o.next_action,o.next_action_due_at,"
        "p.stage,p.sequence_step,p.last_touch_at,p.next_follow_up_at,p.outreach_route,p.do_not_contact,"
        "c.name contact_name,c.email,c.phone "
        "FROM organizations o JOIN opportunities p ON p.organization_id=o.id "
        "LEFT JOIN contacts c ON c.organization_id=o.id AND c.is_primary=1 " + where +
        " ORDER BY CASE p.stage WHEN 'replied' THEN 0 WHEN 'qualified' THEN 1 WHEN 'proposal_sent' THEN 2 "
        "WHEN 'contacted' THEN 3 WHEN 'new' THEN 4 ELSE 5 END,"
        "CASE o.fit_tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 ELSE 3 END,"
        "o.name LIMIT ? OFFSET ?",
        params + [paging["per_page"], paging["offset"]],
    ).fetchall()
    conn.close()
    template = "_pipeline_results.html" if request.headers.get("HX-Request") == "true" else "pipeline.html"
    response = templates.TemplateResponse(request, template, _base_context(
        request, session, "pipeline", rows=[dict(row) for row in rows], q=q, stage=stage,
        paging=paging, stages=OPPORTUNITY_STAGES
    ))
    response.headers["Vary"] = "HX-Request"
    return response


# ------------------------------------------------------------------ clients --

@router.get("/admin/clients", response_class=HTMLResponse)
def clients(request: Request, q: str = "", page: int = 1):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    pattern = f"%{q.strip()}%"
    filters = (q.strip(), pattern, pattern)
    # The listing is filtered in SQL rather than after the fetch. A post-filter
    # would still have counted another firm's clients in the pagination total,
    # which leaks how many books a competitor holds.
    visible, visible_params = tenancy.and_where(_scope(conn, session), "o.id")
    filters = filters + tuple(visible_params)
    total = conn.execute(
        "SELECT COUNT(*) FROM organizations o WHERE (?='' OR o.name LIKE ? OR o.city LIKE ?)"
        + visible, filters
    ).fetchone()[0]
    paging = _paging(page, total)
    rows = conn.execute(
        "SELECT o.id,o.name,o.city,o.lifecycle_stage,o.next_action,o.next_action_due_at,"
        "COUNT(DISTINCT e.id) engagement_count,"
        "SUM(CASE WHEN e.status='awaiting_client' THEN 1 ELSE 0 END) blocked_count,"
        "MIN(CASE WHEN e.status NOT IN ('delivered','closed') THEN e.due_at END) next_due "
        "FROM organizations o LEFT JOIN engagements e ON e.organization_id=o.id "
        "WHERE (?='' OR o.name LIKE ? OR o.city LIKE ?)" + visible
        + " GROUP BY o.id ORDER BY o.name LIMIT ? OFFSET ?",
        filters + (paging["per_page"], paging["offset"]),
    ).fetchall()
    output = []
    now = clock.now()
    for row in rows:
        item = dict(row)
        due = clock.parse_timestamp(item["next_due"])
        if due and due < now:
            item["health"] = "overdue"
        elif item["blocked_count"]:
            item["health"] = "blocked"
        elif item["engagement_count"]:
            item["health"] = "active"
        else:
            item["health"] = "prospect"
        output.append(item)
    conn.close()
    return templates.TemplateResponse(request, "clients.html", _base_context(
        request, session, "clients", rows=output, q=q, paging=paging
    ))


@router.get("/admin/clients/{organization_id}", response_class=HTMLResponse)
def client_detail(request: Request, organization_id: str):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    try:
        _scoped(conn, session, organization_id, "Client not found")
    except Exception:
        conn.close()
        raise
    organization = conn.execute("SELECT * FROM organizations WHERE id=?", (organization_id,)).fetchone()
    if not organization:
        conn.close()
        raise HTTPException(404, "Client not found")
    contacts = conn.execute(
        "SELECT * FROM contacts WHERE organization_id=? ORDER BY is_primary DESC,name", (organization_id,)
    ).fetchall()
    engagements = conn.execute(
        "SELECT * FROM engagements WHERE organization_id=? ORDER BY created_at DESC LIMIT 50", (organization_id,)
    ).fetchall()
    opportunity = conn.execute(
        "SELECT * FROM opportunities WHERE organization_id=? ORDER BY created_at DESC LIMIT 1", (organization_id,)
    ).fetchone()
    activities = conn.execute(
        "SELECT a.*,u.display_name actor_name FROM activities a LEFT JOIN users u ON u.id=a.actor_user_id "
        "WHERE a.organization_id=? ORDER BY a.created_at DESC LIMIT 30", (organization_id,)
    ).fetchall()
    audit = conn.execute(
        "SELECT v.*,u.display_name actor_name FROM audit_events v LEFT JOIN users u ON u.id=v.actor_user_id "
        "WHERE v.entity_id IN (SELECT id FROM engagements WHERE organization_id=?) OR v.entity_id=? "
        "ORDER BY v.created_at DESC LIMIT 15", (organization_id, organization_id)
    ).fetchall()

    # Money and source documents live on the intake side; join them here so the
    # client record answers "have they paid and what did they send" in one place.
    submissions = []
    for row in conn.execute(
        "SELECT s.id,s.created_at,s.closed_at,s.files,s.checklist,s.notes,s.engagement_id "
        "FROM submissions s WHERE s.organization_id=? ORDER BY s.created_at DESC", (organization_id,)
    ):
        item = dict(row)
        item["files_list"] = [name for name in (item.get("files") or "").split(",") if name]
        item["retention"] = _retention(item)
        submissions.append(item)
    payments = [dict(row) for row in conn.execute(
        "SELECT p.order_id,p.payment_id,p.amount,p.currency,p.status,p.created_at,p.paid_at,p.email "
        "FROM payments p WHERE p.submission_id IN (SELECT id FROM submissions WHERE organization_id=?) "
        "OR p.email IN (SELECT email FROM contacts WHERE organization_id=? AND email IS NOT NULL) "
        "ORDER BY p.created_at DESC LIMIT 20", (organization_id, organization_id)
    )]
    users = _users(conn)
    client_account = client_accounts.client_for_organization(conn, organization_id)
    connection = quickbooks.active_connection(conn, organization_id)
    client_readiness = readiness.for_client(conn, organization_id)
    conn.close()
    return templates.TemplateResponse(request, "client_detail.html", _base_context(
        request, session, "clients", organization=dict(organization),
        contacts=[dict(row) for row in contacts], engagements=[dict(row) for row in engagements],
        activities=[dict(row) for row in activities], audit=[dict(row) for row in audit],
        opportunity=dict(opportunity) if opportunity else None,
        submissions=submissions, payments=payments, users=users,
        lifecycle_stages=LIFECYCLE_STAGES, opportunity_stages=OPPORTUNITY_STAGES,
        connection=connection, qbo_ready=quickbooks.is_configured(),
        client_account=client_account, portal_base=_portal_base,
        # What this client still needs, in order, said before a scan rather
        # than discovered afterwards as a page of blocks with no instruction.
        readiness=client_readiness,
    ))


@router.post("/admin/clients/{organization_id}/note")
def add_note(request: Request, organization_id: str, body: str = Form(...), csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped(conn, session, organization_id)
    body = body.strip()
    if body:
        conn.execute(
            "INSERT INTO activities(id,organization_id,actor_user_id,kind,body) VALUES(?,?,?,'note',?)",
            (crm.new_id("act"), organization_id, session["user_id"], body[:4000]),
        )
        conn.execute("UPDATE organizations SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (organization_id,))
        conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/clients/{organization_id}", status_code=303)


@router.post("/admin/clients/{organization_id}/details")
def update_client(request: Request, organization_id: str,
                  lifecycle_stage: str = Form(...), next_action: str = Form(""),
                  next_action_due: str = Form(""), owner_user_id: str = Form(""),
                  csrf_token: str = Form(...)):
    if lifecycle_stage not in LIFECYCLE:
        raise HTTPException(400, "Unknown relationship stage")
    # Parse before opening a connection: a rejected form must not leak one.
    due_at = _parse_form_date(next_action_due)
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped(conn, session, organization_id)
    current = conn.execute(
        "SELECT lifecycle_stage,next_action,owner_user_id FROM organizations WHERE id=?", (organization_id,)
    ).fetchone()
    if not current:
        conn.close()
        raise HTTPException(404, "Client not found")
    owner = owner_user_id.strip() or None
    if owner and not conn.execute("SELECT 1 FROM users WHERE id=? AND active=1", (owner,)).fetchone():
        conn.close()
        raise HTTPException(400, "Unknown owner")
    conn.execute(
        "UPDATE organizations SET lifecycle_stage=?,next_action=?,next_action_due_at=?,owner_user_id=?,"
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (lifecycle_stage, next_action.strip()[:500] or None, due_at, owner, organization_id),
    )
    changes = []
    if current[0] != lifecycle_stage:
        changes.append(f"stage {current[0]} -> {lifecycle_stage}")
    if (current[1] or "") != next_action.strip():
        changes.append("next action updated")
    if (current[2] or "") != (owner or ""):
        changes.append("owner reassigned")
    if changes:
        summary = "; ".join(changes)
        conn.execute(
            "INSERT INTO activities(id,organization_id,actor_user_id,kind,body) VALUES(?,?,?,'status',?)",
            (crm.new_id("act"), organization_id, session["user_id"], summary),
        )
        _audit(conn, session, "organization.update", "organization", organization_id, summary)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/clients/{organization_id}", status_code=303)


@router.post("/admin/clients/{organization_id}/opportunity")
def update_opportunity(request: Request, organization_id: str, stage: str = Form(...),
                       sequence_step: str = Form(""), next_follow_up: str = Form(""),
                       do_not_contact: str = Form(""),
                       csrf_token: str = Form(...)):
    if stage not in OPPORTUNITY:
        raise HTTPException(400, "Unknown pipeline stage")
    # Parse before opening a connection: a rejected form must not leak one.
    follow_up = _parse_form_date(next_follow_up)
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped(conn, session, organization_id)
    row = conn.execute(
        "SELECT id,stage FROM opportunities WHERE organization_id=? ORDER BY created_at DESC LIMIT 1",
        (organization_id,),
    ).fetchone()
    suppressed = int(do_not_contact.strip().casefold() in {"1", "yes", "true", "on"})
    if row:
        conn.execute(
            "UPDATE opportunities SET stage=?,sequence_step=?,next_follow_up_at=?,"
            "last_touch_at=CURRENT_TIMESTAMP,replied=MAX(replied,?),review_sold=MAX(review_sold,?),"
            "do_not_contact=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (stage, sequence_step.strip()[:120] or None, follow_up,
             int(stage in {"replied", "qualified", "proposal_sent", "won"}), int(stage == "won"),
             suppressed, row[0]),
        )
        summary = f"pipeline {row[1]} -> {stage}"
    else:
        conn.execute(
            "INSERT INTO opportunities(id,organization_id,stage,service_type,sequence_step,next_follow_up_at,"
            "last_touch_at,do_not_contact) VALUES(?,?,?,'cash_leak_review',?,?,CURRENT_TIMESTAMP,?)",
            (crm.new_id("opp"), organization_id, stage, sequence_step.strip()[:120] or None,
             follow_up, suppressed),
        )
        summary = f"pipeline opened at {stage}"
    conn.execute(
        "INSERT INTO activities(id,organization_id,actor_user_id,kind,body) VALUES(?,?,?,'status',?)",
        (crm.new_id("act"), organization_id, session["user_id"], summary),
    )
    if suppressed:
        summary += "; added to internal do-not-contact list"
    _audit(conn, session, "opportunity.update", "organization", organization_id, summary)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/clients/{organization_id}", status_code=303)


# --------------------------------------------------------------------- work --

@router.post("/admin/clients/{organization_id}/client-link")
def send_client_link(request: Request, organization_id: str, csrf_token: str = Form(...)):
    """Issue a fresh portal link for this client.

    Shown to the operator rather than emailed silently, so they can put it in
    a reply the client is already expecting. Issuing a new link revokes any
    previous unused one.
    """
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped(conn, session, organization_id)
    try:
        account = client_accounts.client_for_organization(conn, organization_id)
        if not account:
            raise HTTPException(404, "This client has no account yet")
        token = client_accounts.issue_access_link(conn, account["id"], issued_by=session["user_id"])
        _audit(conn, session, "client.link.issued", "organization", organization_id,
               f"Portal access link issued to {account['email']}")
        conn.execute(
            "INSERT INTO activities(id,organization_id,actor_user_id,kind,body) VALUES(?,?,?,'system',?)",
            (crm.new_id("act"), organization_id, session["user_id"],
             f"Portal access link issued to {account['email']}"),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        f"/admin/clients/{organization_id}?link={urllib.parse.quote(token)}", status_code=303)


@router.post("/admin/clients/{organization_id}/qbo-connect")
def connect_quickbooks(request: Request, organization_id: str, csrf_token: str = Form(...)):
    """Operator-initiated QuickBooks authorization.

    Lives under /admin so the session cookie — scoped to /admin so it is never
    sent to a public endpoint — actually reaches it. A POST because it creates
    server state, which every other state change here proves with a CSRF token.
    """
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped(conn, session, organization_id)
    try:
        target = quickbooks.start_authorization(conn, organization_id, session["user_id"])
    finally:
        conn.close()
    return RedirectResponse(target, status_code=303)


@router.post("/admin/engagements/{engagement_id}/pull-reports")
def pull_reports(request: Request, engagement_id: str, csrf_token: str = Form(...)):
    """Fetch the review's source reports straight from QuickBooks.

    This is what a connected client is promised instead of exporting files, so
    it belongs on the engagement rather than buried in a settings screen.
    """
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped_engagement(conn, session, engagement_id)
    try:
        row = conn.execute(
            "SELECT organization_id FROM engagements WHERE id=?", (engagement_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Engagement not found")
        connection = quickbooks.active_connection(conn, row[0])
        if not connection or connection.get("status") != "active":
            raise HTTPException(409, "This client has no active QuickBooks connection")
        try:
            qbo_reports.pull(conn, connection_id=connection["id"],
                             engagement_id=engagement_id,
                             requested_by_user_id=session["user_id"])
        except quickbooks.ReconnectRequired:
            raise HTTPException(409, "QuickBooks access has lapsed — the client must reconnect")
    finally:
        conn.close()
    return RedirectResponse(f"/admin/engagements/{engagement_id}", status_code=303)


@router.post("/admin/engagements/{engagement_id}/bookkeeping-scan")
def start_bookkeeping_scan(request: Request, engagement_id: str, csrf_token: str = Form(...)):
    """Reconstruct provider objects and open an evidence-controlled review."""
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped_engagement(conn, session, engagement_id)
    try:
        row = conn.execute(
            "SELECT organization_id,accounting_period FROM engagements WHERE id=?", (engagement_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Engagement not found")
        connection = quickbooks.active_connection(conn, row[0])
        if not connection or connection.get("status") != "active":
            raise HTTPException(409, "This client has no active QuickBooks connection")
        # A verification report must describe the same books as the document
        # pull below. `latest_report` may legitimately return yesterday's last
        # successful snapshot after today's report request failed, which is
        # useful for browsing but produces a meaningless disagreement here.
        # Refresh the Trial Balance as part of the scan and read only the
        # snapshot created by this exact run. Failure therefore becomes
        # `not_supplied`, never a comparison against stale arithmetic.
        trial_balance_run = qbo_reports.pull(
            conn, connection_id=connection["id"], engagement_id=engagement_id,
            reports=(qbo_reports.REPORTS_BY_NAME["TrialBalance"],),
            requested_by_user_id=session["user_id"])
        adapter = QBOAdapter(conn, connection["id"])
        objects = adapter.pull_all()
        end = clock.business_date(clock.now())
        start = end - timedelta(days=365)
        evidence = {
            "period_start": start.isoformat(), "period_end": end.isoformat(),
            "source_documents": {}, "receivable_confirmations": {},
        }
        # Statements mapped to a ledger account become evidence; unmapped ones
        # stay out, so reconcile() keeps reporting no_source rather than
        # reconciling against an account nobody confirmed.
        evidence.update(statement_store.evidence(
            conn, row[0], period_start=start, period_end=end))
        # QuickBooks' own trial balance, which is the third independent reading
        # of this ledger. `trusted_ledger` requires it to agree with our
        # reconstruction exactly before any check may use the reconstruction.
        # Absent -- never synced -- means the third check did not run, which is
        # reported as such rather than counted as agreement.
        snapshot = qbo_reports.report_for_run(
            conn, trial_balance_run["sync_run_id"], "TrialBalance")
        if snapshot:
            evidence["provider_trial_balance"] = snapshot["payload"]
        analysis = work_engine.analyze(objects, evidence, today=end)
        run_id = work_engine.persist_analysis(
            conn, organization_id=row[0], engagement_id=engagement_id,
            connection_id=connection["id"], analysis=analysis, evidence=evidence)
        conn.execute(
            "INSERT INTO activities(id,organization_id,engagement_id,actor_user_id,kind,body) "
            "VALUES(?,?,?,?,'system',?)",
            (crm.new_id("act"), row[0], engagement_id, session["user_id"],
             f"Bookkeeping Work Engine opened {len(analysis.findings)} findings for review"))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/admin/bookkeeping/{run_id}", status_code=303)


@router.post("/admin/engagements/{engagement_id}/import-qbo-exports")
async def import_qbo_exports(
    request: Request,
    engagement_id: str,
    transaction_detail_file: UploadFile = File(...),
    ar_aging_file: UploadFile = File(...),
    csrf_token: str = Form(...),
):
    """Open a review from customer-produced exports while OAuth is unavailable.

    Files are parsed in memory and are not retained by this route. The
    customer can still use the normal paid-intake upload when source retention
    is required; this action exists for the operator's fulfilment workflow.
    """
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped_engagement(conn, session, engagement_id)
    try:
        auth.require_roles(session, "owner", "operator", "reviewer")
        row = conn.execute(
            "SELECT organization_id FROM engagements WHERE id=?", (engagement_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Engagement not found")
        ledger = await transaction_detail_file.read(qbo_exports.MAX_EXPORT_BYTES + 1)
        aging = await ar_aging_file.read(qbo_exports.MAX_EXPORT_BYTES + 1)
        ledger_name = Path(transaction_detail_file.filename or "transaction-detail.csv").name
        aging_name = Path(ar_aging_file.filename or "ar-aging-detail.csv").name
        try:
            package = qbo_exports.parse_exports(
                ledger, aging, ledger_name=ledger_name, ar_name=aging_name)
        except qbo_exports.ExportError as exc:
            raise HTTPException(400, str(exc)) from exc
        end = date.fromisoformat(package.evidence["period_end"])
        analysis = work_engine.analyze(package.objects, package.evidence, today=end)
        run_id = work_engine.persist_analysis(
            conn, organization_id=row[0], engagement_id=engagement_id,
            connection_id=None, analysis=analysis, evidence=package.evidence,
            provider="qbo_export",
        )
        summary = (
            f"Imported customer QuickBooks exports: "
            f"{package.rows_imported['transaction_detail']} transaction rows and "
            f"{package.rows_imported['ar_aging']} open invoices; "
            f"opened {len(analysis.findings)} findings"
        )
        if package.warnings:
            summary += ". " + " ".join(package.warnings)
        conn.execute(
            "INSERT INTO activities(id,organization_id,engagement_id,actor_user_id,kind,body) "
            "VALUES(?,?,?,?,'system',?)",
            (crm.new_id("act"), row[0], engagement_id, session["user_id"], summary[:2000]),
        )
        _audit(conn, session, "bookkeeping.exports.import", "bookkeeping_run", run_id, summary[:1000])
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/admin/bookkeeping/{run_id}", status_code=303)


@router.get("/admin/bookkeeping/{run_id}", response_class=HTMLResponse)
def bookkeeping_review(request: Request, run_id: str):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    _scoped_run(conn, session, run_id)
    run = conn.execute(
        "SELECT r.*,o.name organization_name,e.title engagement_title FROM bookkeeping_runs r "
        "JOIN organizations o ON o.id=r.organization_id "
        "LEFT JOIN engagements e ON e.id=r.engagement_id WHERE r.id=?", (run_id,)).fetchone()
    if not run:
        conn.close()
        raise HTTPException(404, "Bookkeeping run not found")
    proposals = work_engine.proposal_rows(conn, run_id)
    execution_rows = conn.execute(
        "SELECT * FROM bookkeeping_executions WHERE proposal_id IN "
        "(SELECT id FROM bookkeeping_proposals WHERE run_id=?)", (run_id,)).fetchall()
    executions = {row["proposal_id"]: dict(row) for row in execution_rows}
    findings = [dict(row) for row in conn.execute(
        "SELECT * FROM bookkeeping_findings WHERE run_id=? ORDER BY "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,title",
        (run_id,))]
    requests = [dict(row) for row in conn.execute(
        "SELECT q.*,f.title FROM bookkeeping_evidence_requests q JOIN bookkeeping_findings f ON f.id=q.finding_id "
        "WHERE f.run_id=? ORDER BY q.created_at", (run_id,))]
    reconciliations = [dict(row) for row in conn.execute(
        "SELECT * FROM bookkeeping_reconciliations WHERE run_id=? ORDER BY account_ref", (run_id,))]
    run_row = dict(run)
    import json as _json
    coverage = _json.loads(run_row.get("coverage_json") or "{}")
    conn.close()
    return templates.TemplateResponse(request, "bookkeeping_review.html", _base_context(
        request, session, "work", run=run_row, proposals=proposals,
        findings=findings, evidence_requests=requests, executions=executions,
        coverage=coverage, reconciliations=reconciliations,
        # The same provenance the portfolio queue summarises, in full. A
        # reviewer who cannot see why a number is trustworthy has to take it on
        # faith, and fast review is the whole economics of the console.
        verification=provenance.ledger_checks(coverage),
        assurance=provenance.engine_assurance()))


@router.get("/admin/bookkeeping/{run_id}/working-papers")
def download_working_papers(request: Request, run_id: str):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        raise HTTPException(401, "Sign in required")
    try:
        _scoped_run(conn, session, run_id)
        package = work_engine.persistent_working_papers(conn, run_id)
        _audit(conn, session, "bookkeeping.working_papers.download", "bookkeeping_run", run_id,
               f"Downloaded working-paper package {package['package_hash'][:12]}")
        conn.commit()
    finally:
        conn.close()
    import json
    return Response(
        content=json.dumps(package, indent=2), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="bookkeeping-{run_id}.json"',
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post(
    "/admin/engagements/{engagement_id}/bookkeeping/{run_id}/cash-leak-review.pdf"
)
def download_cash_leak_review(
    request: Request, engagement_id: str, run_id: str,
    csrf_token: str = Form(...),
):
    """Render one authenticated review in memory and retain no report file."""
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped_run(conn, session, run_id)
    try:
        auth.require_roles(session, "owner", "reviewer")
        run = conn.execute(
            "SELECT r.id,r.period_end,o.name organization_name "
            "FROM bookkeeping_runs r "
            "JOIN engagements e ON e.id=r.engagement_id "
            "JOIN organizations o ON o.id=r.organization_id "
            "WHERE r.id=? AND e.id=? AND e.organization_id=r.organization_id",
            (run_id, engagement_id),
        ).fetchone()
        if not run:
            raise HTTPException(404, "Bookkeeping review not found")
        transaction_count = conn.execute(
            "SELECT COUNT(*) FROM bookkeeping_transactions WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        if not transaction_count:
            raise HTTPException(
                409, "This review has no persisted bookkeeping transactions"
            )
        try:
            as_of = date.fromisoformat(run["period_end"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(409, "This review has no valid reporting date") from exc

        findings = reporting.portfolio(conn, run_id, as_of=as_of)
        html = report_pdf.render_html(
            findings,
            {"name": run["organization_name"], "software": "QuickBooks Online"},
            report_date=report_pdf.report_date(run["period_end"], as_of),
        )
        try:
            document = report_pdf.render_pdf(html, renderer=_pdf_renderer)
        except report_pdf.PdfRuntimeUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except report_pdf.PdfRenderError as exc:
            raise HTTPException(502, "The client report could not be rendered") from exc

        _audit(
            conn, session, "bookkeeping.report.download", "bookkeeping_run", run_id,
            "Generated and downloaded Cash-Leak Review PDF",
        )
        conn.commit()
    finally:
        conn.close()
    return Response(
        content=document,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="cash-leak-review.pdf"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/admin/bookkeeping/proposals/{proposal_id}/decision")
def bookkeeping_decision(request: Request, proposal_id: str, action: str = Form(...),
                         note: str = Form(""), edited_json: str = Form(""),
                         csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped_proposal(conn, session, proposal_id)
    try:
        auth.require_roles(session, "owner", "reviewer")
        row = conn.execute("SELECT run_id FROM bookkeeping_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Proposal not found")
        edited = None
        if action == "edit":
            try:
                import json
                edited = json.loads(edited_json)
            except Exception as exc:
                raise HTTPException(400, "Edited proposal must be valid JSON") from exc
        try:
            work_engine.record_decision(conn, proposal_id, action, session["user_id"], note, edited)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        run_id = row[0]
    finally:
        conn.close()
    return RedirectResponse(f"/admin/bookkeeping/{run_id}", status_code=303)


@router.post("/admin/bookkeeping/{run_id}/decisions")
def bookkeeping_batch_decision(
        request: Request, run_id: str, action: str = Form(...),
        selected: list[str] = Form(default=[]),
        note: str = Form(""), csrf_token: str = Form(...)):
    """Decide every selected proposal in one pass, or decide none of them.

    Each checkbox carries the fingerprint of the diff the reviewer was shown.
    They are all re-checked before anything is written, and one mismatch refuses
    the batch. Approving twenty with one button is only safe if the twenty are
    the twenty that were on the screen.
    """
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped_run(conn, session, run_id)
    try:
        auth.require_roles(session, "owner", "reviewer")
        if action not in {"approve", "reject", "escalate"}:
            raise HTTPException(400, "A batch may only approve, reject or escalate")

        # Each selected box posts "<proposal_id>:<fingerprint>". Parsing here
        # rather than trusting two parallel lists keeps a proposal from being
        # paired with somebody else's fingerprint by a reordered form.
        chosen = []
        for value in selected:
            proposal_id, _, fingerprint = str(value).partition(":")
            if proposal_id:
                chosen.append((proposal_id, fingerprint))
        if not chosen:
            raise HTTPException(400, "Nothing was selected")

        try:
            staged = work_engine.record_batch_decision(
                conn, chosen, session["user_id"], note=note, run_id=run_id)
            for proposal_id in staged:
                work_engine.record_decision(
                    conn, proposal_id, action, session["user_id"], note)
        except work_engine.BatchChanged as exc:
            conn.rollback()
            raise HTTPException(409, str(exc)) from exc
        except PermissionError as exc:
            conn.rollback()
            raise HTTPException(403, str(exc)) from exc
        _audit(conn, session, f"bookkeeping.batch.{action}", "bookkeeping_run",
               run_id, f"{action.title()}d {len(staged)} proposal(s) in one pass")
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/admin/bookkeeping/{run_id}", status_code=303)


@router.post("/admin/bookkeeping/proposals/{proposal_id}/execute")
def bookkeeping_execute(request: Request, proposal_id: str, csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped_proposal(conn, session, proposal_id)
    try:
        row = conn.execute(
            "SELECT p.run_id,r.connection_id FROM bookkeeping_proposals p "
            "JOIN bookkeeping_runs r ON r.id=p.run_id WHERE p.id=?", (proposal_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Proposal not found")
        if not row[1]:
            raise HTTPException(409, "This review has no provider connection")
        auth.require_roles(session, "owner", "operator")
        adapter = QBOAdapter(conn, row[1])
        # A one-person company cannot separate approval from release. That is
        # permitted only while writes are sandbox-only, and it is recorded on
        # the execution rather than waved through. Once a second operator
        # exists, remove the exception rather than widening it.
        operators = conn.execute(
            "SELECT COUNT(DISTINCT u.id) FROM users u JOIN user_roles ur ON ur.user_id=u.id "
            "WHERE u.active=1 AND ur.role_id IN ('owner','reviewer','operator')").fetchone()[0]
        single_operator = operators <= 1 and adapter.environment == "sandbox"
        try:
            work_engine.execute_saved(
                conn, proposal_id, adapter, session["user_id"],
                allow_single_operator=single_operator,
                single_operator_note=(
                    "Sole active operator; QuickBooks environment is sandbox. Approval and "
                    "release were performed by the same person."))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        # Executing changed the ledger, so the reconciliation on record is now
        # stale. Recompute it here rather than leaving the working papers to
        # describe a state that no longer exists.
        try:
            work_engine.refresh_reconciliation(conn, row[0], adapter)
        except Exception:                                          # noqa: BLE001
            # A failed refresh must not undo a verified write. The stored
            # reconciliation keeps its previous value and its timestamp shows it.
            pass
        run_id = row[0]
    finally:
        conn.close()
    return RedirectResponse(f"/admin/bookkeeping/{run_id}", status_code=303)


@router.get("/admin/snapshots/{snapshot_id}")
def download_snapshot(request: Request, snapshot_id: str):
    """Hand an operator the raw report exactly as QuickBooks returned it."""
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        raise HTTPException(401, "Sign in required")
    snapshot = qbo_reports.read_snapshot(conn, snapshot_id)
    if snapshot:
        # A report snapshot is the client's own financial data. Scoping the
        # download by the snapshot's organization rather than by the route it
        # was linked from means a guessed id does not become a disclosure.
        _scoped(conn, session, snapshot.get("organization_id"), "Snapshot not found")
    if not snapshot:
        conn.close()
        raise HTTPException(404, "Not found")
    _audit(conn, session, "qbo.report.download", "organization",
           snapshot["organization_id"], f"Downloaded the {snapshot['report_name']} report")
    conn.commit()
    conn.close()
    import json as _json
    filename = f"{snapshot['report_name']}_{snapshot['period_end']}.json"
    return Response(
        content=_json.dumps(snapshot["payload"], indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


@router.post("/admin/clients/{organization_id}/qbo-disconnect")
def disconnect_quickbooks(request: Request, organization_id: str, connection_id: str = Form(...),
                          csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped(conn, session, organization_id)
    owner = conn.execute(
        "SELECT organization_id FROM connections WHERE id=?", (connection_id,)
    ).fetchone()
    # Path the connection through its client so a guessed id cannot revoke
    # somebody else's books from this form.
    if not owner or owner[0] != organization_id:
        conn.close()
        raise HTTPException(404, "No such connection for this client")
    try:
        quickbooks.revoke_connection(conn, connection_id, session["user_id"])
    finally:
        conn.close()
    return RedirectResponse(f"/admin/clients/{organization_id}", status_code=303)


@router.get("/admin/portfolio", response_class=HTMLResponse)
def portfolio_queue(request: Request):
    """One queue across every client in scope, ordered by where the hour goes.

    The accountant console's front door. It is deliberately the same surface
    for an internal operator and for a firm principal -- the scope decides what
    is in it, not the template -- so there is only one queue to keep correct.
    """
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    scope = _scope(conn, session)
    queue = portfolio.build(conn, scope)
    conn.close()
    return templates.TemplateResponse(request, "portfolio.html", _base_context(
        request, session, "portfolio", portfolio=queue, scope_kind=scope.kind,
        state_labels=[(state, portfolio.STATE_LABELS[state])
                      for state in portfolio.STATE_ORDER],
        assurance=provenance.engine_assurance()))


@router.get("/admin/clients/{organization_id}/gst", response_class=HTMLResponse)
def gst_return_page(request: Request, organization_id: str):
    """The GST/HST return for the period that has most recently closed.

    Prepared on the way in rather than read from a cache, because an accountant
    opening this page is about to sign what it says and a figure computed before
    the last correction landed would be stale in the one place staleness costs
    money. The queue reads the stored copy; this one recomputes.
    """
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    _scoped(conn, session, organization_id, "Client not found")
    try:
        organization = conn.execute(
            "SELECT id,name FROM organizations WHERE id=?",
            (organization_id,)).fetchone()
        arrangement, arrangement_problem = None, ""
        try:
            arrangement = filing_periods.arrangement_for(conn, organization_id)
        except filing_periods.FilingUnknown as exc:
            arrangement_problem = str(exc)
        prepared, problem = gst_return.prepare_current(conn, organization_id)
        due = ""
        if prepared and arrangement:
            period = filing_periods.periods_for(
                arrangement,
                containing=date.fromisoformat(prepared.period_end))
            due = period.return_due.isoformat() if period.return_due else ""
            gst_return.persist(conn, prepared, due_at=due or None)
            conn.commit()
        pending_rates = tax_rates.undecided(conn, organization_id)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "gst_return.html", _base_context(
        request, session, "portfolio", organization=dict(organization),
        prepared=prepared, problem=problem or arrangement_problem,
        arrangement=arrangement, due=due, pending_rates=pending_rates,
        line_titles=gst_return.LINE_TITLES,
        months=[(number, calendar_module.month_name[number])
                for number in range(1, 13)]))


@router.get("/admin/clients/{organization_id}/first-contact",
            response_class=HTMLResponse)
def first_contact_report(request: Request, organization_id: str):
    """What this client's QuickBooks file turned out to contain.

    Read from the stored probe rather than recomputed. Unlike the GST page -- which
    recomputes because an accountant is about to sign what it says -- this is a
    record of an observation at a point in time, and recomputing it would throw
    away the thing worth keeping. The probe runs on the keepalive timer, so a
    connection made overnight has an answer waiting by morning.
    """
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    _scoped(conn, session, organization_id, "Client not found")
    try:
        organization = conn.execute(
            "SELECT id,name FROM organizations WHERE id=?",
            (organization_id,)).fetchone()
        if not organization:
            raise HTTPException(404, "Client not found")
        report = first_contact.latest(conn, organization_id)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "first_contact.html", _base_context(
        request, session, "portfolio", organization=dict(organization),
        report=report, held=first_contact.HELD, failed=first_contact.FAILED,
        unknown=first_contact.UNKNOWN))


@router.post("/admin/clients/{organization_id}/gst/arrangement")
def record_gst_arrangement(
        request: Request, organization_id: str, frequency: str = Form(...),
        year_end_month: int = Form(...), year_end_day: int = Form(...),
        registrant: str = Form(""), gst_number: str = Form(""),
        calculation_method: str = Form(...),
        csrf_token: str = Form(...)):
    """Record how often a client files and when their year ends.

    Nothing is defaulted anywhere in this lane, so this is where the facts have
    to arrive. A guessed frequency prepares a return for a period the client
    does not file, and a guessed year end puts it on the wrong months -- both
    are filing errors wearing the clothes of an ordinary return.
    """
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped(conn, session, organization_id, "Client not found")
    try:
        auth.require_roles(session, "owner", "reviewer")
        # Left as None rather than False when nobody has said. An annual
        # filer's due date turns on it, and "not answered" must not read as
        # "corporation".
        is_individual = {"individual": True, "corporation": False}.get(registrant)
        try:
            filing_periods.record_arrangement(
                conn, organization_id, frequency=frequency,
                year_end_month=int(year_end_month), year_end_day=int(year_end_day),
                is_individual=is_individual, gst_number=gst_number.strip(),
                calculation_method=calculation_method)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        _audit(conn, session, "gst.arrangement.record", "organization",
               organization_id,
               f"{frequency} filer using {calculation_method} method, "
               f"year end {year_end_month}/{year_end_day}")
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/admin/clients/{organization_id}/gst", status_code=303)


@router.post("/admin/clients/{organization_id}/gst/rates/{rate_id}")
def decide_tax_rate(request: Request, organization_id: str, rate_id: str,
                    classification: str = Form(...), reason: str = Form(""),
                    csrf_token: str = Form(...)):
    """Record which return a tax rate belongs on.

    A judgement, not a setting: it changes a figure filed with the CRA, so it
    carries who decided and why, and only a reviewer may make it.
    """
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped(conn, session, organization_id, "Client not found")
    try:
        auth.require_roles(session, "owner", "reviewer")
        try:
            tax_rates.decide(conn, organization_id, rate_id, classification,
                             user_id=session["user_id"], reason=reason)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        _audit(conn, session, "gst.rate.classify", "organization", organization_id,
               f"Rate {rate_id} classified as {classification}")
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/admin/clients/{organization_id}/gst", status_code=303)


@router.get("/admin/work", response_class=HTMLResponse)
def work(request: Request, status: str = "", page: int = 1):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    rows = _engagement_rows(conn, _scope(conn, session))
    if status:
        rows = [row for row in rows if row["status"] == status]
    paging = _paging(page, len(rows))
    rows = rows[paging["offset"]:paging["offset"] + paging["per_page"]]
    conn.close()
    return templates.TemplateResponse(request, "work.html", _base_context(
        request, session, "work", rows=rows, status=status, paging=paging
    ))


@router.get("/admin/calendar", response_class=HTMLResponse)
def calendar(request: Request):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    rows = [item for item in _engagement_rows(conn, _scope(conn, session)) if item["due_at"]]
    rows.sort(key=lambda item: item["due_sort"])
    groups = []
    for item in rows:
        due = clock.to_business(clock.parse_timestamp(item["due_at"]))
        label = due.strftime("%A, %B %d").replace(" 0", " ")
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(item)
    conn.close()
    return templates.TemplateResponse(request, "calendar.html", _base_context(
        request, session, "calendar", groups=groups
    ))


@router.get("/admin/engagements/{engagement_id}", response_class=HTMLResponse)
def engagement_detail(request: Request, engagement_id: str):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    _scoped_engagement(conn, session, engagement_id)
    row = conn.execute(
        "SELECT e.*,o.name organization_name,o.id organization_id,u.display_name owner_name,"
        "s.files,s.checklist,s.notes submission_notes,s.closed_at submission_closed_at,"
        "s.created_at submission_created_at "
        "FROM engagements e JOIN organizations o ON o.id=e.organization_id "
        "LEFT JOIN users u ON u.id=e.assigned_user_id LEFT JOIN submissions s ON s.id=e.submission_id "
        "WHERE e.id=?", (engagement_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Engagement not found")
    items = conn.execute(
        "SELECT * FROM work_items WHERE engagement_id=? ORDER BY position", (engagement_id,)
    ).fetchall()
    activities = conn.execute(
        "SELECT a.*,u.display_name actor_name FROM activities a LEFT JOIN users u ON u.id=a.actor_user_id "
        "WHERE a.engagement_id=? ORDER BY a.created_at DESC LIMIT 30", (engagement_id,)
    ).fetchall()
    audit = conn.execute(
        "SELECT v.*,u.display_name actor_name FROM audit_events v LEFT JOIN users u ON u.id=v.actor_user_id "
        "WHERE v.entity_id=? OR v.entity_id IN (SELECT id FROM work_items WHERE engagement_id=?) "
        "ORDER BY v.created_at DESC LIMIT 20", (engagement_id, engagement_id)
    ).fetchall()
    engagement = dict(row)
    engagement["files_list"] = [name for name in (engagement.get("files") or "").split(",") if name]
    engagement["due_label"], engagement["due_tone"] = _relative_due(engagement["due_at"])
    engagement["stage_position"] = ENGAGEMENT.position(engagement["status"])
    retention = _retention({
        "closed_at": engagement.get("submission_closed_at"),
        "created_at": engagement.get("submission_created_at"),
    }) if engagement.get("submission_id") else None
    if engagement.get("ready_at") and engagement.get("delivered_at"):
        engagement["turnaround_days"] = clock.business_days_between(
            clock.parse_timestamp(engagement["ready_at"]), clock.parse_timestamp(engagement["delivered_at"])
        )
    users = _users(conn)
    qbo_connection = quickbooks.active_connection(conn, engagement["organization_id"])
    latest_pull = qbo_reports.latest_run(conn, engagement["organization_id"])
    report_snapshots = qbo_reports.snapshots_for(
        conn, engagement["organization_id"],
        latest_pull["id"] if latest_pull else None) if latest_pull else []
    bookkeeping_run = conn.execute(
        "SELECT id,status,period_start,period_end,started_at FROM bookkeeping_runs "
        "WHERE engagement_id=? ORDER BY started_at DESC LIMIT 1", (engagement_id,)).fetchone()
    conn.close()
    return templates.TemplateResponse(request, "engagement_detail.html", _base_context(
        request, session, "work", engagement=engagement,
        items=[dict(item) for item in items], activities=[dict(item) for item in activities],
        audit=[dict(item) for item in audit], users=users,
        statuses=ENGAGEMENT_STATUSES, work_item_statuses=WORK_ITEM_STATUSES,
        retention=retention, retention_after_close=_retention_after_close,
        retention_unclosed=_retention_unclosed,
        qbo_connection=qbo_connection, latest_pull=latest_pull,
        report_snapshots=report_snapshots,
        bookkeeping_run=dict(bookkeeping_run) if bookkeeping_run else None,
    ))


@router.post("/admin/engagements/{engagement_id}/status")
def update_engagement_status(request: Request, engagement_id: str, status: str = Form(...),
                             csrf_token: str = Form(...)):
    if status not in ENGAGEMENT_STATUSES:
        raise HTTPException(400, "Invalid status")
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped_engagement(conn, session, engagement_id)
    current = conn.execute(
        "SELECT status,ready_at,submission_id,organization_id FROM engagements WHERE id=?", (engagement_id,)
    ).fetchone()
    if not current:
        conn.close()
        raise HTTPException(404, "Engagement not found")
    now = clock.now()
    ready_at = current[1]
    due_at = None
    # Payment does not start the clock. Marking work ready does, and the due
    # date is the close of the fifth Canadian business day after that.
    if status in {"ready", "in_progress", "internal_review", "delivered", "closed"} and not ready_at:
        ready_at = clock.format_timestamp(now)
        due_at = clock.format_timestamp(clock.add_business_days(now, 5))
    conn.execute(
        "UPDATE engagements SET status=?,ready_at=COALESCE(ready_at,?),due_at=COALESCE(due_at,?),"
        "delivered_at=CASE WHEN ?='delivered' THEN COALESCE(delivered_at,CURRENT_TIMESTAMP) ELSE delivered_at END,"
        "closed_at=CASE WHEN ?='closed' THEN COALESCE(closed_at,CURRENT_TIMESTAMP) "
        "WHEN status='closed' THEN NULL ELSE closed_at END,"
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, ready_at, due_at, status, status, engagement_id),
    )
    if current[2]:
        # Closing the engagement is what starts the published deletion clock.
        conn.execute(
            "UPDATE submissions SET closed_at=CASE WHEN ?='closed' THEN COALESCE(closed_at,CURRENT_TIMESTAMP) "
            "ELSE NULL END WHERE id=?",
            (status, current[2]),
        )
    conn.execute(
        "INSERT INTO activities(id,organization_id,engagement_id,actor_user_id,kind,body) VALUES(?,?,?,?,'status',?)",
        (crm.new_id("act"), current[3], engagement_id, session["user_id"],
         f"Status changed from {current[0]} to {status}"),
    )
    _audit(conn, session, "engagement.status", "engagement", engagement_id, f"{current[0]} -> {status}")
    if status == "closed" and current[2]:
        _audit(conn, session, "retention.start", "submission", current[2],
               f"Deletion clock started; files removed after {_retention_after_close} days")
    elif current[0] == "closed" and status != "closed" and current[2]:
        _audit(conn, session, "retention.stop", "submission", current[2], "Deletion clock cleared on reopen")
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/engagements/{engagement_id}", status_code=303)


@router.post("/admin/engagements/{engagement_id}/assign")
def assign_engagement(request: Request, engagement_id: str, assigned_user_id: str = Form(""),
                      priority_reason: str = Form(""), csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    _scoped_engagement(conn, session, engagement_id)
    current = conn.execute(
        "SELECT assigned_user_id,organization_id FROM engagements WHERE id=?", (engagement_id,)
    ).fetchone()
    if not current:
        conn.close()
        raise HTTPException(404, "Engagement not found")
    owner = assigned_user_id.strip() or None
    if owner and not conn.execute("SELECT 1 FROM users WHERE id=? AND active=1", (owner,)).fetchone():
        conn.close()
        raise HTTPException(400, "Unknown owner")
    conn.execute(
        "UPDATE engagements SET assigned_user_id=?,priority_reason=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (owner, priority_reason.strip()[:500] or None, engagement_id),
    )
    if (current[0] or "") != (owner or ""):
        name = conn.execute("SELECT display_name FROM users WHERE id=?", (owner,)).fetchone() if owner else None
        summary = f"Assigned to {name[0]}" if name else "Assignment cleared"
        conn.execute(
            "INSERT INTO activities(id,organization_id,engagement_id,actor_user_id,kind,body) "
            "VALUES(?,?,?,?,'assignment',?)",
            (crm.new_id("act"), current[1], engagement_id, session["user_id"], summary),
        )
        _audit(conn, session, "engagement.assign", "engagement", engagement_id, summary)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/engagements/{engagement_id}", status_code=303)


@router.post("/admin/work-items/{work_item_id}/status")
def update_work_item(request: Request, work_item_id: str, status: str = Form(...), csrf_token: str = Form(...)):
    if status not in WORK_ITEM:
        raise HTTPException(400, "Invalid status")
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    row = conn.execute("SELECT engagement_id,status FROM work_items WHERE id=?", (work_item_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Work item not found")
    _scoped_engagement(conn, session, row[0])
    conn.execute(
        "UPDATE work_items SET status=?,completed_at=CASE WHEN ?='done' THEN CURRENT_TIMESTAMP ELSE NULL END,"
        "updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, status, work_item_id),
    )
    _audit(conn, session, "work.status", "work_item", work_item_id, f"{row[1]} -> {status}")
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/engagements/{row[0]}", status_code=303)


# -------------------------------------------------------------------- audit --

@router.get("/admin/audit", response_class=HTMLResponse)
def audit_history(request: Request, action: str = "", page: int = 1):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    clauses, params = [], []
    if action.strip():
        clauses.append("v.action LIKE ?")
        params.append(f"{action.strip()}%")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    total = conn.execute("SELECT COUNT(*) FROM audit_events v " + where, params).fetchone()[0]
    paging = _paging(page, total)
    rows = conn.execute(
        "SELECT v.*,u.display_name actor_name FROM audit_events v LEFT JOIN users u ON u.id=v.actor_user_id "
        + where + " ORDER BY v.created_at DESC,v.rowid DESC LIMIT ? OFFSET ?",
        params + [paging["per_page"], paging["offset"]],
    ).fetchall()
    actions = [row[0] for row in conn.execute("SELECT DISTINCT action FROM audit_events ORDER BY action")]
    conn.close()
    return templates.TemplateResponse(request, "audit.html", _base_context(
        request, session, "audit", rows=[dict(row) for row in rows], action=action,
        actions=actions, paging=paging
    ))


# ------------------------------------------------------------------ imports --

@router.get("/admin/imports/leads", response_class=HTMLResponse)
def lead_import_page(request: Request, batch: str = ""):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        return _redirect_login(request)
    preview = crm.get_import_preview(conn, batch) if batch else None
    conn.close()
    return templates.TemplateResponse(request, "lead_import.html", _base_context(
        request, session, "pipeline", preview=preview
    ))


@router.post("/admin/imports/leads/preview", response_class=HTMLResponse)
async def preview_leads(request: Request, tracker_file: UploadFile = File(...),
                        research_file: UploadFile = File(...), csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    tracker = await tracker_file.read(2 * 1024 * 1024 + 1)
    research = await research_file.read(2 * 1024 * 1024 + 1)
    if len(tracker) > 2 * 1024 * 1024 or len(research) > 2 * 1024 * 1024:
        conn.close()
        raise HTTPException(413, "Lead files must be under 2 MB each")
    try:
        preview = crm.preview_lead_import(conn, tracker, research, session["user_id"])
    except (UnicodeDecodeError, ValueError) as exc:
        conn.close()
        raise HTTPException(400, f"Could not read lead CSV: {exc}")
    conn.close()
    return RedirectResponse(f"/admin/imports/leads?batch={preview['batch_id']}", status_code=303)


@router.post("/admin/imports/leads/{batch_id}/apply")
def apply_leads(request: Request, batch_id: str, csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    try:
        crm.apply_lead_import(conn, batch_id, session["user_id"])
    except ValueError as exc:
        conn.close()
        raise HTTPException(404, str(exc))
    conn.close()
    return RedirectResponse(f"/admin/imports/leads?batch={batch_id}", status_code=303)


@router.post("/admin/imports/leads/{batch_id}/resolve")
def resolve_lead_conflict(request: Request, batch_id: str, row_key: str = Form(...),
                          same_as: str = Form(""), csrf_token: str = Form(...)):
    conn = _db()
    session = _authorized_post(conn, request, csrf_token)
    try:
        outcome = crm.resolve_import_conflict(
            conn, batch_id, row_key,
            same_as_organization_id=same_as.strip() or None, user_id=session["user_id"])
    except ValueError as exc:
        conn.close()
        raise HTTPException(400, str(exc))
    if outcome["organization_id"]:
        _audit(conn, session, "organization.alias", "organization",
               outcome["organization_id"], f"import {batch_id}: {row_key} is the same company")
        conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/imports/leads?batch={batch_id}", status_code=303)


# -------------------------------------------------------------------- files --

@router.get("/admin/files/{sub_id}/{filename}")
def get_file(request: Request, sub_id: str, filename: str):
    conn = _db()
    session = _session(request, conn)
    if not session:
        conn.close()
        raise HTTPException(401, "Sign in required")
    if not re.fullmatch(r"[a-f0-9]{12}", sub_id):
        conn.close()
        raise HTTPException(404, "Not found")
    folder = (_uploads_dir() / sub_id).resolve()
    path = (folder / filename).resolve()
    if path.parent != folder or not path.is_file():
        conn.close()
        raise HTTPException(404, "Not found")
    _audit(conn, session, "document.download", "submission", sub_id, f"Downloaded {filename}")
    conn.commit()
    conn.close()
    return FileResponse(path, filename=filename, media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
