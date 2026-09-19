"""
Shimline intake service.

Replaces the mailto-handoff on the landing page with a real endpoint: the
post-payment wizard POSTs here, the submission (and any attached files) is
saved, and you get an email the moment it lands. No third-party form tool,
no monthly fee, runs on a box you already own.

Endpoints:
  POST /intake   - public. Accepts the wizard's submission (JSON fields +
                   optional file uploads). Returns {"ok": true, "id": "..."}.
  GET  /admin    - session-authenticated operations workspace (see shimline/admin.py).
  GET  /health   - plain liveness check for your own monitoring.

Run locally to try it out:
  pip install -r requirements.txt
  uvicorn app:app --host 0.0.0.0 --port 8000

See docs/CONFIGURATION.md for putting this on your actual server behind Nginx + HTTPS.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.responses import Response

from shimline import admin as admin_workspace
from shimline import clients, crm, emailing, measurement, portal, quickbooks, rate_limit, release
from shimline.db import apply_migrations, configure_connection
from shimline.settings import settings as SETTINGS

BASE_DIR = Path(__file__).parent
DB_PATH = SETTINGS.db_path
UPLOADS_DIR = SETTINGS.uploads_dir
UPLOADS_DIR.mkdir(exist_ok=True)

# ---- configuration (all via environment variables, see docs/CONFIGURATION.md) ----
SMTP_HOST = SETTINGS.smtp_host
SMTP_PORT = SETTINGS.smtp_port
SMTP_USER = SETTINGS.smtp_user
SMTP_PASS = SETTINGS.smtp_pass.get_secret_value()
NOTIFY_TO = SETTINGS.notify_to  # where the "new submission" email goes
NOTIFY_FROM = SETTINGS.effective_notify_from
MAX_UPLOAD_MB = SETTINGS.max_upload_mb

# ---- data retention ----
# The published privacy policy promises documents are deleted 30 days after an
# engagement closes. That promise is enforced here and by shimline-purge.timer,
# not left to anyone remembering to do it.
#   closed engagement  -> purged RETENTION_DAYS_AFTER_CLOSE days after closing
#   never closed       -> purged RETENTION_DAYS_UNCLOSED days after arriving,
#                         so abandoned enquiries don't sit on disk forever
RETENTION_DAYS_AFTER_CLOSE = SETTINGS.retention_days_after_close
RETENTION_DAYS_UNCLOSED = SETTINGS.retention_days_unclosed

# ---- payments (Razorpay) ----
# The price is fixed server-side and never read from the browser, so a
# tampered client cannot buy a review for a dollar.
RAZORPAY_KEY_ID = SETTINGS.razorpay_key_id
RAZORPAY_KEY_SECRET = SETTINGS.razorpay_key_secret.get_secret_value()
RAZORPAY_WEBHOOK_SECRET = SETTINGS.razorpay_webhook_secret.get_secret_value()
PRICE_AMOUNT = SETTINGS.price_amount   # minor units: 19900 = 199.00
PRICE_CURRENCY = SETTINGS.price_currency
UPLOAD_TOKEN_HOURS = SETTINGS.upload_token_hours

# ---- QuickBooks Online (Phase E) ----
# Sandbox unless production is set deliberately. The one real enrolled client
# consented to Aryan as their accountant, not to a third-party app reading
# their books, so production must never be switched on by accident.
QBO_CLIENT_ID = SETTINGS.qbo_client_id
QBO_CLIENT_SECRET = SETTINGS.qbo_client_secret.get_secret_value()
QBO_REDIRECT_URI = SETTINGS.qbo_redirect_uri
QBO_ENVIRONMENT = SETTINGS.qbo_environment
QBO_TOKEN_KEY = SETTINGS.qbo_token_key.get_secret_value()
# Where the browser is sent to authorize. Named in the admin CSP so the
# "Connect QuickBooks" form submission is allowed to redirect there.
QBO_AUTHORIZE_ORIGIN = SETTINGS.qbo_authorize_origin

# Static assets are content-addressed by ?v= (see STATIC_VERSION), so they can
# be cached hard. Never applied to a page, a download, or anything authenticated.
# Where a client's emailed link points. Its own setting so a staging host can
# send links that do not lead to production.
PORTAL_BASE_URL = SETTINGS.normalized_portal_base_url

STATIC_MAX_AGE = 31536000
STATIC_VERSION = SETTINGS.static_version or str(int(
    max((BASE_DIR / "shimline" / "static").rglob("*"), key=lambda p: p.stat().st_mtime).stat().st_mtime
))

# Intuit's security requirements: "You must not log any user's credentials or
# QuickBooks data", and OAuth tokens must not be exposed. uvicorn's access log
# records the full request line, so a real /qbo/callback would write the
# authorization code and the customer's realm id into the system journal.
# Strip the query string from those paths before anything is emitted.
class _RedactQueryStrings(logging.Filter):
    SENSITIVE_PREFIXES = ("/qbo/",)

    def filter(self, record: logging.LogRecord) -> bool:
        args = getattr(record, "args", None)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if "?" in path and path.startswith(self.SENSITIVE_PREFIXES):
                scrubbed = list(args)
                scrubbed[2] = path.split("?", 1)[0] + "?<redacted>"
                record.args = tuple(scrubbed)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactQueryStrings())

app = FastAPI(title="Shimline intake")

# Metrics are opt-in and have bounded route labels only. The endpoint is hidden
# behind loopback or a dedicated bearer token and deliberately omitted from the
# public OpenAPI document.
if SETTINGS.metrics_enabled:
    Instrumentator(
        excluded_handlers=["/health", "/internal/metrics"],
        should_ignore_untemplated=True,
    ).instrument(app)


def _metrics_authorized(request: Request) -> bool:
    """Always require the bearer token. There is no loopback exemption.

    There used to be one, and behind this deployment it authorised everybody.
    Uvicorn binds 127.0.0.1 and Nginx proxies to it without --proxy-headers, so
    `request.client.host` is "127.0.0.1" for every request that arrives -- a
    genuine local scrape and a stranger hitting https://api.shimline.ca alike.
    A check that cannot tell those apart is not a check.

    A local scraper can send a header as easily as a remote one, so requiring
    the token costs nothing and removes the ambiguity entirely. If loopback
    ever needs to be distinguished again, that has to come from the socket the
    request arrived on, not from an address the proxy rewrites.
    """
    expected = SETTINGS.metrics_token.get_secret_value()
    supplied = request.headers.get("authorization", "")
    prefix = "Bearer "
    return bool(
        expected
        and supplied.startswith(prefix)
        and hmac.compare_digest(supplied[len(prefix):], expected)
    )


@app.get("/internal/metrics", include_in_schema=False)
def internal_metrics(request: Request):
    if not SETTINGS.metrics_enabled or not _metrics_authorized(request):
        raise HTTPException(status_code=404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

app.mount(
    "/admin/static",
    StaticFiles(directory=BASE_DIR / "shimline" / "static"),
    name="admin-static",
)

# Self-hosted webfonts, shared by every surface. Mounted at a bare /fonts so a
# single copy serves the workspace, the portal and the public site, and no page
# has to reach a third party for a typeface.
app.mount(
    "/fonts",
    StaticFiles(directory=BASE_DIR / "shimline" / "static" / "fonts"),
    name="fonts",
)

# Public stylesheet for the QuickBooks pages. Kept in its own directory so the
# operations workspace assets are not served from a client-facing path.
app.mount(
    "/qbo/static",
    StaticFiles(directory=BASE_DIR / "shimline" / "static" / "public"),
    name="qbo-static",
)

# Browsers request /favicon.ico from the origin root for any document that has
# no HTML head to declare an icon in, and some request it regardless. Without
# these routes the portal and the workspace fall back to the browser's generic
# icon, which is what a client sees in their tab after signing in.
_ICON_DIR = BASE_DIR / "shimline" / "static"
_ICON_CACHE = "public, max-age=604800"


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return FileResponse(
        _ICON_DIR / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": _ICON_CACHE},
    )


@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    return FileResponse(
        _ICON_DIR / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": _ICON_CACHE},
    )


# Only the production landing page needs browser cross-origin access.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://shimline.ca", "https://www.shimline.ca"],
    allow_methods=["POST", "GET", "DELETE"],
    allow_headers=["*"],
)

# 8 attempts per address per hour, counted in the database rather than in this
# process. The in-memory version that used to live here reset on every restart,
# and the deploy script restarts the service on every release -- so the limit on
# the paid front door was cleared several times a week, and a crash-looping
# service cleared it more often than that. See `shimline/rate_limit.py`.
RATE_LIMIT = rate_limit.DEFAULT_LIMIT
RATE_WINDOW_SECONDS = rate_limit.DEFAULT_WINDOW_SECONDS


def _rate_limited(ip: str, bucket: str = rate_limit.SUBMIT) -> bool:
    conn = _db()
    try:
        return not rate_limit.allow(conn, bucket, ip)
    finally:
        conn.close()


def _db():
    conn = configure_connection(sqlite3.connect(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            company TEXT,
            contact_name TEXT,
            email TEXT,
            checklist TEXT,
            notes TEXT,
            files TEXT,
            source_ip TEXT
        )
    """)
    # Migration: closed_at was added when automated retention was introduced.
    # Existing rows get NULL, i.e. "engagement still open".
    existing = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
    if "closed_at" not in existing:
        conn.execute("ALTER TABLE submissions ADD COLUMN closed_at TEXT")
    if "phone" not in existing:
        conn.execute("ALTER TABLE submissions ADD COLUMN phone TEXT")
    # Audit trail of what retention removed. Deliberately holds no personal
    # data — just the opaque submission id, so the log itself never becomes a
    # copy of the thing we promised to delete.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS purge_log (
            submission_id TEXT,
            purged_at TEXT DEFAULT CURRENT_TIMESTAMP,
            files_removed INTEGER,
            reason TEXT
        )
    """)
    # Payments. upload_token_hash is stored instead of the token itself so a
    # database read never yields a usable token.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            order_id TEXT PRIMARY KEY,
            payment_id TEXT,
            amount INTEGER,
            currency TEXT,
            status TEXT DEFAULT 'created',
            email TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            paid_at TEXT,
            upload_token_hash TEXT,
            token_used_at TEXT,
            submission_id TEXT
        )
    """)
    apply_migrations(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------- payments --

@app.post("/measure", status_code=204, include_in_schema=False)
async def collect_measurement(request: Request):
    """Accept one consent-gated, minimized website event.

    The setting is intentionally off by default.  Returning 404 while off
    prevents a static build accidentally sending events to a half-configured
    deployment.  Origin and bot checks are data-quality filters, not an
    authentication boundary; the payload contains no account or customer data.
    """
    if not SETTINGS.measurement_enabled:
        raise HTTPException(404, "Measurement is not enabled")
    if request.headers.get("origin") not in measurement.ORIGINS:
        raise HTTPException(403, "Measurement origin is not allowed")
    if measurement.is_bot(request.headers.get("user-agent", "")):
        return Response(status_code=204)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "Measurement payload must be JSON") from exc
    event = measurement.parse_event(body)
    conn = _db()
    try:
        measurement.save_event(conn, event)
    finally:
        conn.close()
    return Response(status_code=204)


@app.delete("/measure", status_code=204, include_in_schema=False)
async def delete_measurement(request: Request):
    """Let a browser withdraw measurement consent and erase its own record."""
    if not SETTINGS.measurement_enabled:
        raise HTTPException(404, "Measurement is not enabled")
    if request.headers.get("origin") not in measurement.ORIGINS:
        raise HTTPException(403, "Measurement origin is not allowed")
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "Measurement payload must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(422, "Measurement payload must be an object")
    conn = _db()
    try:
        measurement.forget_visitor(conn, body.get("visitor_id"))
    finally:
        conn.close()
    return Response(status_code=204)

def _rzp_call(path: str, payload: dict) -> dict:
    """Minimal Razorpay REST call. Avoids pulling in their SDK for two endpoints."""
    import json as _json
    import urllib.error
    import urllib.request
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        raise HTTPException(503, "Payments are not configured")
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://api.razorpay.com/v1" + path,
        data=_json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return _json.load(resp)
    except urllib.error.HTTPError as exc:
        # Never surface the gateway's raw error to the browser.
        print(f"[error] razorpay {path} -> {exc.code} {exc.read()[:300]!r}")
        raise HTTPException(502, "Payment provider rejected the request")
    except Exception as exc:
        print(f"[error] razorpay {path} -> {exc}")
        raise HTTPException(502, "Could not reach the payment provider")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _valid_upload_token(token: str):
    """Return the paid order_id for an unused, unexpired token, else None."""
    if not token:
        return None
    conn = _db()
    row = conn.execute(
        "SELECT order_id, paid_at, token_used_at FROM payments "
        "WHERE upload_token_hash = ? AND status = 'paid'",
        (_token_hash(token),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    order_id, paid_at, used_at = row
    if used_at:
        return None
    paid = _parse_ts(paid_at)
    if paid is None or datetime.now(timezone.utc) - paid > timedelta(hours=UPLOAD_TOKEN_HOURS):
        return None
    return order_id


def _claim_upload_token(conn, token: str, submission_id: str) -> bool:
    """Atomically bind one valid paid token to one submission.

    The caller must hold a write transaction. The conditional UPDATE prevents
    two simultaneous requests from both spending the same token.
    """
    if not token:
        return False
    row = conn.execute(
        "SELECT order_id, paid_at FROM payments "
        "WHERE upload_token_hash = ? AND status = 'paid' AND token_used_at IS NULL",
        (_token_hash(token),),
    ).fetchone()
    if not row:
        return False
    order_id, paid_at = row
    paid = _parse_ts(paid_at)
    if paid is None or datetime.now(timezone.utc) - paid > timedelta(hours=UPLOAD_TOKEN_HOURS):
        return False
    updated = conn.execute(
        "UPDATE payments SET token_used_at = CURRENT_TIMESTAMP, submission_id = ? "
        "WHERE order_id = ? AND token_used_at IS NULL",
        (submission_id, order_id),
    )
    return updated.rowcount == 1


def _parse_ts(value: str):
    """SQLite CURRENT_TIMESTAMP is 'YYYY-MM-DD HH:MM:SS' in UTC."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _due_at(created_at: str, closed_at: str):
    """When this submission is scheduled for deletion, and why."""
    if closed_at:
        base, days, reason = _parse_ts(closed_at), RETENTION_DAYS_AFTER_CLOSE, "closed"
    else:
        base, days, reason = _parse_ts(created_at), RETENTION_DAYS_UNCLOSED, "unclosed"
    if base is None:
        return None, reason
    return base + timedelta(days=days), reason


def purge_expired(now=None) -> list:
    """Delete documents and submissions that are past their retention window.

    Removes the uploaded files from disk and the row from the database, then
    records the opaque id in purge_log. Returns what it removed so the caller
    (the systemd timer) can log it.
    """
    now = now or datetime.now(timezone.utc)
    conn = _db()
    removed = []
    for sub_id, created_at, closed_at in conn.execute(
        "SELECT id, created_at, closed_at FROM submissions"
    ).fetchall():
        due, reason = _due_at(created_at, closed_at)
        if due is None or now < due:
            continue
        folder = UPLOADS_DIR / sub_id
        file_count = 0
        if folder.is_dir():
            file_count = sum(1 for _ in folder.iterdir())
            # Never delete the database evidence that a file exists while a
            # locked or permission-denied file silently remains on disk. A
            # failed removal aborts this purge transaction so the timer fails
            # visibly and can be retried after the filesystem problem is fixed.
            try:
                shutil.rmtree(folder)
            except Exception:
                conn.rollback()
                conn.close()
                raise
        conn.execute("DELETE FROM submissions WHERE id = ?", (sub_id,))
        conn.execute(
            "INSERT INTO purge_log (submission_id, files_removed, reason) VALUES (?, ?, ?)",
            (sub_id, file_count, reason),
        )
        removed.append({"id": sub_id, "files": file_count, "reason": reason})

    removed.extend(_purge_expired_snapshots(conn, now))
    removed.extend(_purge_expired_bookkeeping(conn, now))
    # Website measurement is purpose-limited to improving active marketing.
    # Keep the raw pseudonymous event stream only for its documented window.
    conn.execute(
        "DELETE FROM measurement_events WHERE received_at < datetime(?, ?)",
        (now.strftime("%Y-%m-%d %H:%M:%S"), f"-{SETTINGS.measurement_retention_days} days"),
    )
    conn.execute(
        "DELETE FROM measurement_orders WHERE created_at < datetime(?, ?)",
        (now.strftime("%Y-%m-%d %H:%M:%S"), f"-{SETTINGS.measurement_retention_days} days"),
    )
    conn.commit()
    conn.close()
    return removed


def retention_candidates(now=None) -> list:
    """Preview every record set the next purge would remove, without writes."""
    now = now or datetime.now(timezone.utc)
    conn = _db()
    due_items = []
    try:
        for sub_id, created_at, closed_at in conn.execute(
            "SELECT id, created_at, closed_at FROM submissions"
        ).fetchall():
            due, reason = _due_at(created_at, closed_at)
            if due is None or now < due:
                continue
            folder = UPLOADS_DIR / sub_id
            file_count = sum(1 for _ in folder.iterdir()) if folder.is_dir() else 0
            due_items.append({"id": sub_id, "files": file_count, "reason": reason})

        for engagement_id, created_at, closed_at in conn.execute(
            "SELECT DISTINCT e.id,e.created_at,e.closed_at FROM engagements e "
            "JOIN source_snapshots s ON s.engagement_id=e.id"
        ).fetchall():
            due, reason = _due_at(created_at, closed_at)
            if due is not None and now >= due:
                count = conn.execute(
                    "SELECT COUNT(*) FROM source_snapshots WHERE engagement_id=?", (engagement_id,)
                ).fetchone()[0]
                if count:
                    due_items.append({"id": engagement_id, "snapshots": count, "reason": reason})
        snapshot_cutoff = (now - timedelta(days=RETENTION_DAYS_UNCLOSED)).strftime("%Y-%m-%d %H:%M:%S")
        orphan_snapshots = conn.execute(
            "SELECT COUNT(*) FROM source_snapshots WHERE engagement_id IS NULL AND fetched_at < ?",
            (snapshot_cutoff,),
        ).fetchone()[0]
        if orphan_snapshots:
            due_items.append({"id": "unattached-reports", "snapshots": orphan_snapshots,
                              "reason": f"unattached for {RETENTION_DAYS_UNCLOSED} days"})

        for engagement_id, created_at, closed_at in conn.execute(
            "SELECT DISTINCT e.id,e.created_at,e.closed_at FROM engagements e "
            "JOIN bookkeeping_runs r ON r.engagement_id=e.id"
        ).fetchall():
            due, reason = _due_at(created_at, closed_at)
            if due is not None and now >= due:
                count = conn.execute(
                    "SELECT COUNT(*) FROM bookkeeping_runs WHERE engagement_id=?", (engagement_id,)
                ).fetchone()[0]
                if count:
                    due_items.append({"id": engagement_id, "bookkeeping_runs": count, "reason": reason})
        bookkeeping_cutoff = (now - timedelta(days=RETENTION_DAYS_UNCLOSED)).strftime(
            "%Y-%m-%d %H:%M:%S")
        orphan_runs = conn.execute(
            "SELECT COUNT(*) FROM bookkeeping_runs WHERE engagement_id IS NULL AND started_at < ?",
            (bookkeeping_cutoff,),
        ).fetchone()[0]
        if orphan_runs:
            due_items.append({"id": "unattached-bookkeeping", "bookkeeping_runs": orphan_runs,
                              "reason": f"unattached for {RETENTION_DAYS_UNCLOSED} days"})
        return due_items
    finally:
        conn.close()


def _purge_expired_snapshots(conn, now) -> list:
    """Delete QuickBooks reports past the same window as uploaded documents.

    A report pulled from a client's books is their financial data exactly as an
    uploaded export is, so it lives under the same published promise: gone 30
    days after the engagement closes, or 90 days if none ever does. Reports
    that outlived that would be a quiet breach of the privacy policy, so this
    runs on the same daily timer.
    """
    removed = []
    engagements = conn.execute(
        "SELECT DISTINCT e.id, e.created_at, e.closed_at FROM engagements e "
        "JOIN source_snapshots s ON s.engagement_id = e.id"
    ).fetchall()
    for engagement_id, created_at, closed_at in engagements:
        due, reason = _due_at(created_at, closed_at)
        if due is None or now < due:
            continue
        count = conn.execute(
            "SELECT COUNT(*) FROM source_snapshots WHERE engagement_id = ?", (engagement_id,)
        ).fetchone()[0]
        if not count:
            continue
        conn.execute("DELETE FROM source_snapshots WHERE engagement_id = ?", (engagement_id,))
        conn.execute(
            "INSERT INTO purge_log (submission_id, files_removed, snapshots_removed, reason) "
            "VALUES (?, 0, ?, ?)",
            (engagement_id, count, reason),
        )
        removed.append({"id": engagement_id, "snapshots": count, "reason": reason})

    # A pull that was never tied to an engagement still ages out, measured from
    # when it was taken, so an orphan cannot sit on disk indefinitely.
    orphan_cutoff = (now - timedelta(days=RETENTION_DAYS_UNCLOSED)).strftime("%Y-%m-%d %H:%M:%S")
    orphans = conn.execute(
        "SELECT COUNT(*) FROM source_snapshots WHERE engagement_id IS NULL AND fetched_at < ?",
        (orphan_cutoff,),
    ).fetchone()[0]
    if orphans:
        conn.execute(
            "DELETE FROM source_snapshots WHERE engagement_id IS NULL AND fetched_at < ?",
            (orphan_cutoff,),
        )
        conn.execute(
            "INSERT INTO purge_log (submission_id, files_removed, snapshots_removed, reason) "
            "VALUES ('unattached-reports', 0, ?, ?)",
            (orphans, f"unattached for {RETENTION_DAYS_UNCLOSED} days"),
        )
        removed.append({"id": "unattached-reports", "snapshots": orphans,
                        "reason": f"unattached for {RETENTION_DAYS_UNCLOSED} days"})
    return removed


def _purge_expired_bookkeeping(conn, now) -> list:
    """Apply the document-retention promise to reconstructed financial data."""
    removed = []
    engagements = conn.execute(
        "SELECT DISTINCT e.id,e.created_at,e.closed_at FROM engagements e "
        "JOIN bookkeeping_runs r ON r.engagement_id=e.id").fetchall()
    for engagement_id, created_at, closed_at in engagements:
        due, reason = _due_at(created_at, closed_at)
        if due is None or now < due:
            continue
        count = conn.execute(
            "SELECT COUNT(*) FROM bookkeeping_runs WHERE engagement_id=?", (engagement_id,)
        ).fetchone()[0]
        if count:
            conn.execute("DELETE FROM bookkeeping_runs WHERE engagement_id=?", (engagement_id,))
            conn.execute(
                "INSERT INTO purge_log(submission_id,files_removed,bookkeeping_runs_removed,reason) "
                "VALUES(?,0,?,?)", (engagement_id, count, reason))
            removed.append({"id": engagement_id, "bookkeeping_runs": count, "reason": reason})
    cutoff = (now - timedelta(days=RETENTION_DAYS_UNCLOSED)).strftime("%Y-%m-%d %H:%M:%S")
    orphan_count = conn.execute(
        "SELECT COUNT(*) FROM bookkeeping_runs WHERE engagement_id IS NULL AND started_at < ?", (cutoff,)
    ).fetchone()[0]
    if orphan_count:
        conn.execute(
            "DELETE FROM bookkeeping_runs WHERE engagement_id IS NULL AND started_at < ?", (cutoff,))
        conn.execute(
            "INSERT INTO purge_log(submission_id,files_removed,bookkeeping_runs_removed,reason) "
            "VALUES('unattached-bookkeeping',0,?,?)",
            (orphan_count, f"unattached for {RETENTION_DAYS_UNCLOSED} days"))
        removed.append({"id": "unattached-bookkeeping", "bookkeeping_runs": orphan_count,
                        "reason": f"unattached for {RETENTION_DAYS_UNCLOSED} days"})
    # Master data is shared by an organization's runs. Remove it only after
    # that organization has no retained run left, in dependency order.
    for table in ("bookkeeping_projects", "bookkeeping_classifications",
                  "bookkeeping_accounts", "bookkeeping_entities"):
        conn.execute(
            f"DELETE FROM {table} WHERE organization_id NOT IN "
            "(SELECT DISTINCT organization_id FROM bookkeeping_runs)")
    return removed


def _notify(company: str, contact_name: str, email: str, sub_id: str):
    emailing.send_submission_notification(company, contact_name, email, sub_id)


def _dispatch_submission_notification(
    company: str, contact_name: str, email: str, sub_id: str
) -> None:
    if SETTINGS.async_jobs_enabled:
        try:
            from shimline.jobs import notify_submission

            notify_submission(sub_id)
            from shimline.jobs import extract_submission_documents

            extract_submission_documents(sub_id)
            return
        except (OSError, sqlite3.Error):
            # A queue outage must not lose the notification; SMTP is the
            # bounded fallback and the already-committed intake remains safe.
            pass
    _notify(company, contact_name, email, sub_id)


@app.get("/health")
def health():
    conn = _db()
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        schema_migration = row[0] if row and row[0] else "none"
    finally:
        conn.close()
    target = release.MANIFEST["expected_schema"]
    return {
        "ok": schema_migration == target,
        "release_id": release.MANIFEST["release_id"],
        "git_commit": release.MANIFEST["git_commit"],
        "schema_migration": schema_migration,
        "schema_target": target,
    }


@app.post(
    "/intake",
    responses={
        400: {"description": "Malformed multipart request"},
        402: {"description": "Valid paid upload token required"},
        413: {"description": "Uploaded document exceeds configured limit"},
        429: {"description": "Submission rate limit exceeded"},
    },
)
async def intake(
    request: Request,
    company: str = Form(""),
    contact_name: str = Form(""),
    email: str = Form(""),
    checklist: str = Form(""),   # comma-separated labels, filled by the wizard
    notes: str = Form(""),
    phone: str = Form(""),
    website: str = Form(""),     # honeypot — real users never see or fill this field
    upload_token: str = Form(""),  # issued after a verified payment
    pl_file: UploadFile | None = File(None),
    ar_file: UploadFile | None = File(None),
):
    ip = request.client.host if request.client else "unknown"

    if website.strip():
        # Bot filled the honeypot. Pretend success so it doesn't retry smarter.
        return JSONResponse({"ok": True, "id": "received"})

    if not _valid_upload_token(upload_token):
        raise HTTPException(402, "A valid payment is required before documents can be submitted.")

    if _rate_limited(ip):
        raise HTTPException(429, "Too many submissions from this address — try again later.")

    sub_id = uuid.uuid4().hex[:12]
    sub_dir = UPLOADS_DIR / sub_id
    sub_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []
    try:
        for field, f in (("pl", pl_file), ("ar", ar_file)):
            if f is None or not f.filename:
                continue
            # Never use a client-supplied path as a filesystem destination.
            filename = re.sub(r"[^A-Za-z0-9._-]", "_", f.filename.replace("\\", "/").rsplit("/", 1)[-1])[:150]
            filename = f"{field}_{filename or 'upload'}"
            dest = sub_dir / filename
            size = 0
            with dest.open("xb") as out:
                while chunk := await f.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_MB * 1024 * 1024:
                        raise HTTPException(413, f"File is over the {MAX_UPLOAD_MB}MB limit")
                    out.write(chunk)
            saved_files.append(filename)
    except Exception:
        shutil.rmtree(sub_dir)
        raise

    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not _claim_upload_token(conn, upload_token, sub_id):
            raise HTTPException(402, "This payment token is invalid, expired, or has already been used.")
        conn.execute(
            "INSERT INTO submissions (id, company, contact_name, email, phone, checklist, notes, files, source_ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sub_id, company, contact_name, email, phone[:40], checklist, notes,
             ",".join(saved_files), ip),
        )
        # The account already exists from the payment; bind it to the business
        # this submission is for, so connections and documents share one owner.
        client_user_id = conn.execute(
            "SELECT client_user_id FROM payments WHERE upload_token_hash=?",
            (_token_hash(upload_token),),
        ).fetchone()
        client_user_id = client_user_id[0] if client_user_id else None
        if not client_user_id and email:
            try:
                client_user_id = clients.ensure_client(conn, email=email, display_name=contact_name)
            except ValueError:
                client_user_id = None
        if client_user_id:
            conn.execute("UPDATE submissions SET client_user_id=? WHERE id=?",
                         (client_user_id, sub_id))

        organization_id, _engagement_id = crm.ensure_intake_engagement(
            conn,
            submission_id=sub_id,
            company=company,
            contact_name=contact_name,
            email=email,
            phone=phone[:40],
        )
        if client_user_id:
            clients.attach_organization(conn, client_user_id, organization_id)
        conn.commit()
    except Exception:
        conn.rollback()
        shutil.rmtree(sub_dir, ignore_errors=True)
        raise
    finally:
        conn.close()

    _dispatch_submission_notification(company, contact_name, email, sub_id)

    return {"ok": True, "id": sub_id}


@app.get("/pay/config")
def pay_config():
    """What the browser needs to open Checkout. Contains no secret."""
    return {
        "enabled": bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET),
        "key_id": RAZORPAY_KEY_ID,
        "amount": PRICE_AMOUNT,
        "currency": PRICE_CURRENCY,
    }


@app.post(
    "/pay/order",
    responses={
        429: {"description": "Payment-attempt rate limit exceeded"},
        502: {"description": "Payment provider rejected or could not receive the request"},
        503: {"description": "Payment provider is not configured"},
    },
)
def create_order(request: Request, email: str = Form(""), company: str = Form(""),
                 measurement_context: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip, rate_limit.PAYMENT):
        raise HTTPException(429, "Too many attempts from this address — try again later.")

    # Amount and currency come from server config, never from the request body.
    order = _rzp_call("/orders", {
        "amount": PRICE_AMOUNT,
        "currency": PRICE_CURRENCY,
        "receipt": "clr-" + uuid.uuid4().hex[:12],
        "notes": {"company": company[:120], "email": email[:120]},
    })
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO payments (order_id, amount, currency, status, email) "
        "VALUES (?, ?, ?, 'created', ?)",
        (order["id"], order["amount"], order["currency"], email[:200]),
    )
    # Attribution is optional, consent-gated in the browser and contains only
    # random identifiers plus UTM labels. It is never derived from email.
    if SETTINGS.measurement_enabled and measurement_context:
        try:
            context = measurement.parse_context(json.loads(measurement_context))
        except (TypeError, ValueError):
            context = None
        if context:
            conn.execute(
                "INSERT INTO measurement_orders(order_id,visitor_id,session_id,utm_source,utm_medium,"
                "utm_campaign,utm_term,utm_content) VALUES (:order_id,:visitor_id,:session_id,:utm_source,"
                ":utm_medium,:utm_campaign,:utm_term,:utm_content)",
                {"order_id": order["id"], **context},
            )
    conn.commit()
    conn.close()
    return {
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": RAZORPAY_KEY_ID,
    }


def _mark_paid(order_id: str, payment_id: str) -> str:
    """Mark an order paid, create the client account, mint an upload token.

    Payment is the right moment to create the account: the email has just been
    used for a real transaction, and the client is asked for nothing extra.
    Doing it later — at first upload, or when somebody remembers — is how you
    end up with connections and documents that belong to no one.
    """
    conn = _db()
    row = conn.execute(
        "SELECT status, upload_token_hash, email FROM payments WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, "Unknown order")
    token = secrets.token_urlsafe(32)
    conn.execute(
        "UPDATE payments SET status='paid', payment_id=?, paid_at=CURRENT_TIMESTAMP, "
        "upload_token_hash=? WHERE order_id=?",
        (payment_id, _token_hash(token), order_id),
    )
    conn.execute("UPDATE measurement_orders SET paid_at=CURRENT_TIMESTAMP WHERE order_id=?", (order_id,))

    # A payment without a usable email still succeeds; it simply has no
    # account until an operator supplies one. Losing the payment over a
    # missing address would be the wrong trade.
    client_user_id = None
    try:
        if row[2] and "@" in row[2]:
            client_user_id = clients.ensure_client(conn, email=row[2])
            conn.execute(
                "UPDATE payments SET client_user_id=? WHERE order_id=?",
                (client_user_id, order_id),
            )
    except (ValueError, sqlite3.Error) as exc:
        print(f"[warn] could not create a client account for order {order_id}: {exc}")

    conn.commit()
    conn.close()
    if client_user_id:
        if SETTINGS.async_jobs_enabled:
            try:
                from shimline.jobs import send_client_portal_link

                send_client_portal_link(client_user_id)
            except (OSError, sqlite3.Error):
                _send_portal_link(client_user_id, row[2])
        else:
            _send_portal_link(client_user_id, row[2])
    return token


def _send_portal_link(client_user_id: str, email: str) -> None:
    """Email the client their access link. Never blocks the payment."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        return
    conn = _db()
    try:
        token = clients.issue_access_link(conn, client_user_id)
        conn.commit()
    except Exception as exc:
        conn.close()
        print(f"[warn] could not issue a portal link: {exc}")
        return
    conn.close()

    emailing.send_portal_link(email, token, PORTAL_BASE_URL)


@app.post(
    "/pay/verify",
    responses={
        400: {"description": "Invalid provider signature"},
        404: {"description": "Payment order not found"},
        503: {"description": "Payment provider is not configured"},
    },
)
def verify_payment(
    razorpay_order_id: str = Form(..., min_length=1, max_length=200),
    razorpay_payment_id: str = Form(..., min_length=1, max_length=200),
    razorpay_signature: str = Form(..., min_length=1, max_length=256),
):
    """Confirm a Checkout success callback really came from Razorpay.

    The browser is never believed on its own: the signature is an HMAC over
    "order_id|payment_id" keyed with our secret, which only Razorpay and we
    can produce.
    """
    if not RAZORPAY_KEY_SECRET:
        raise HTTPException(503, "Payments are not configured")
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        f"{razorpay_order_id}|{razorpay_payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, razorpay_signature):
        print(f"[warn] bad payment signature for order {razorpay_order_id}")
        raise HTTPException(400, "Payment could not be verified")
    token = _mark_paid(razorpay_order_id, razorpay_payment_id)
    return {"ok": True, "upload_token": token}


@app.post("/pay/webhook")
async def razorpay_webhook(request: Request):
    """Authoritative payment confirmation.

    The browser callback can be lost if the customer closes the tab, so this
    is what actually guarantees we know about a payment.
    """
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(503, "Webhook secret not configured")
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        print("[warn] webhook with bad signature rejected")
        raise HTTPException(400, "Bad signature")

    import json as _json
    event = _json.loads(raw or b"{}")
    kind = event.get("event", "")
    entity = (event.get("payload", {}).get("payment", {}) or {}).get("entity", {})
    order_id = entity.get("order_id")
    payment_id = entity.get("id")
    if kind in ("payment.captured", "order.paid") and order_id:
        conn = _db()
        row = conn.execute("SELECT status FROM payments WHERE order_id=?", (order_id,)).fetchone()
        conn.close()
        if row and row[0] != "paid":
            _mark_paid(order_id, payment_id or "")
            print(f"[info] webhook marked {order_id} paid")
    return {"ok": True}

# ---------------------------------------------------------------- admin workspace --

admin_workspace.configure(
    db_factory=_db,
    uploads_dir=lambda: UPLOADS_DIR,
    retention_after_close=RETENTION_DAYS_AFTER_CLOSE,
    retention_unclosed=RETENTION_DAYS_UNCLOSED,
    cookie_secure=os.environ.get("ADMIN_COOKIE_SECURE", "1") != "0",
    portal_base=PORTAL_BASE_URL,
)
admin_workspace.set_static_version(STATIC_VERSION)
app.include_router(admin_workspace.router)

quickbooks.configure(
    db_factory=_db,
    client_id=QBO_CLIENT_ID,
    client_secret=QBO_CLIENT_SECRET,
    redirect_uri=QBO_REDIRECT_URI,
    environment=QBO_ENVIRONMENT,
    token_key=QBO_TOKEN_KEY,
)
quickbooks.set_static_version(STATIC_VERSION)
app.include_router(quickbooks.router)

portal.configure(
    db_factory=_db,
    cookie_secure=os.environ.get("ADMIN_COOKIE_SECURE", "1") != "0",
)
portal.set_static_version(STATIC_VERSION)
app.include_router(portal.router)


@app.middleware("http")
async def admin_security_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/admin", "/qbo", "/portal", "/fonts")):
        # Intuit requires no-store on pages carrying sensitive data. Vendored
        # CSS and fonts carry none, and re-fetching ~1 MB of them on every page
        # load is what made the workspace feel broken over a long-haul link.
        if path.startswith(("/admin/static/", "/qbo/static/", "/fonts/")):
            response.headers["Cache-Control"] = f"public, max-age={STATIC_MAX_AGE}, immutable"
        else:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # form-action must name Intuit's authorization host. Chrome and Firefox
        # apply this directive to redirects that *follow* a form submission, so
        # "Connect QuickBooks" — a POST that 303s to appcenter.intuit.com — is
        # silently blocked without it, with a dead button and no visible error.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; font-src 'self'; "
            f"form-action 'self' {QBO_AUTHORIZE_ORIGIN}; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
    return response
