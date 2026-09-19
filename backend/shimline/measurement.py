"""Privacy-bounded, first-party website measurement.

This module intentionally accepts a small, documented event contract rather
than arbitrary browser payloads.  In particular it never records form values,
file names, IP addresses, full referrers, or URL query strings.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

from fastapi import HTTPException

EVENTS = frozenset({
    "page_view", "page_engaged", "cta_clicked", "sample_report_opened",
    "calculator_used", "faq_opened", "intake_opened", "intake_step_viewed",
    "intake_step_completed", "checklist_changed", "file_attached",
    "checkout_requested", "checkout_opened", "checkout_dismissed",
    "checkout_failed", "payment_verified", "intake_submitted", "intake_failed",
})
ORIGINS = frozenset({"https://shimline.ca", "https://www.shimline.ca"})
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
PATH = re.compile(r"^/[a-zA-Z0-9._/-]{0,160}$")
CAMPAIGN = re.compile(r"^[A-Za-z0-9._~-]{0,80}$")
BOT_UA = re.compile(r"bot|crawler|spider|headless|phantom|facebookexternalhit|slurp", re.I)


def is_bot(user_agent: str) -> bool:
    return bool(BOT_UA.search(user_agent or ""))


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise HTTPException(422, f"Invalid measurement {name}")
    return value


def _campaign(value: object) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not CAMPAIGN.fullmatch(value):
        raise HTTPException(422, "Invalid campaign value")
    return value.lower()


def parse_event(body: object) -> dict:
    """Validate and minimize one browser event before it reaches SQLite."""
    if not isinstance(body, Mapping):
        raise HTTPException(422, "Measurement payload must be an object")
    event = body.get("event")
    if event not in EVENTS:
        raise HTTPException(422, "Unknown measurement event")
    page = body.get("page")
    if not isinstance(page, str) or not PATH.fullmatch(page):
        raise HTTPException(422, "Invalid measurement page")
    referrer = body.get("referrer", "direct")
    if referrer not in {"direct", "search", "social", "referral", "email", "paid", "unknown"}:
        raise HTTPException(422, "Invalid measurement referrer")
    attribution = body.get("attribution", {})
    if not isinstance(attribution, Mapping):
        raise HTTPException(422, "Invalid attribution")
    # Event-specific data is deliberately scalar and bounded.  Unknown keys
    # are rejected so a future UI change cannot silently begin collecting PII.
    data = body.get("data", {})
    if not isinstance(data, Mapping) or set(data) - {"cta", "step", "count", "bucket", "seconds"}:
        raise HTTPException(422, "Invalid measurement event data")
    cleaned: dict[str, str | int] = {}
    for key, value in data.items():
        if key in {"step", "count", "seconds"}:
            if not isinstance(value, int) or value < 0 or value > 3600:
                raise HTTPException(422, "Invalid measurement number")
            cleaned[key] = value
        elif not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", value):
            raise HTTPException(422, "Invalid measurement label")
        else:
            cleaned[key] = value
    return {
        "id": _identifier(body.get("id"), "event id"),
        "visitor_id": _identifier(body.get("visitor_id"), "visitor id"),
        "session_id": _identifier(body.get("session_id"), "session id"),
        "event_name": event,
        "page_path": page,
        "referrer_class": referrer,
        "utm_source": _campaign(attribution.get("source")),
        "utm_medium": _campaign(attribution.get("medium")),
        "utm_campaign": _campaign(attribution.get("campaign")),
        "utm_term": _campaign(attribution.get("term")),
        "utm_content": _campaign(attribution.get("content")),
        "payload": json.dumps(cleaned, separators=(",", ":"), sort_keys=True),
    }


def parse_context(value: object) -> dict | None:
    """Validate the pseudonymous attribution attached to a created order."""
    if not isinstance(value, Mapping):
        return None
    try:
        visitor_id = _identifier(value.get("visitor_id"), "visitor id")
        session_id = _identifier(value.get("session_id"), "session id")
        attribution = value.get("attribution", {})
        if not isinstance(attribution, Mapping):
            return None
        return {
            "visitor_id": visitor_id, "session_id": session_id,
            "utm_source": _campaign(attribution.get("source")),
            "utm_medium": _campaign(attribution.get("medium")),
            "utm_campaign": _campaign(attribution.get("campaign")),
            "utm_term": _campaign(attribution.get("term")),
            "utm_content": _campaign(attribution.get("content")),
        }
    except HTTPException:
        return None


def save_event(conn, event: dict) -> bool:
    """Store an event once. Repeated sends are harmless client retries."""
    result = conn.execute(
        "INSERT OR IGNORE INTO measurement_events "
        "(id,visitor_id,session_id,event_name,page_path,referrer_class,utm_source,utm_medium,"
        "utm_campaign,utm_term,utm_content,payload) VALUES "
        "(:id,:visitor_id,:session_id,:event_name,:page_path,:referrer_class,:utm_source,:utm_medium,"
        ":utm_campaign,:utm_term,:utm_content,:payload)", event,
    )
    conn.commit()
    return result.rowcount == 1


def forget_visitor(conn, visitor_id: object) -> None:
    """Erase a browser's measurement data when its owner withdraws consent."""
    visitor_id = _identifier(visitor_id, "visitor id")
    conn.execute("DELETE FROM measurement_events WHERE visitor_id=?", (visitor_id,))
    conn.execute("DELETE FROM measurement_orders WHERE visitor_id=?", (visitor_id,))
    conn.commit()


def summary(conn, days: int = 30) -> dict:
    """Aggregate-only operator view; never exposes browser identifiers."""
    window = f"-{days} days"
    rows = conn.execute(
        "SELECT event_name, COUNT(*) events, COUNT(DISTINCT session_id) sessions, "
        "COUNT(DISTINCT visitor_id) visitors FROM measurement_events "
        "WHERE received_at >= datetime('now', ?) GROUP BY event_name", (window,)
    ).fetchall()
    by_event = {row["event_name"]: dict(row) for row in rows}
    funnel = []
    for name, label in (("page_view", "Page views"), ("page_engaged", "Engaged visits"),
                        ("cta_clicked", "Start-review clicks"), ("intake_opened", "Intake opened"),
                        ("checkout_requested", "Checkout requested"),
                        ("payment_verified", "Payment verified"),
                        ("intake_submitted", "Intake submitted")):
        funnel.append({"label": label, "sessions": by_event.get(name, {}).get("sessions", 0)})
    channels = [dict(row) for row in conn.execute(
        "SELECT CASE WHEN utm_source='' THEN 'direct / untagged' ELSE utm_source END channel, "
        "COUNT(DISTINCT visitor_id) visitors, COUNT(DISTINCT session_id) sessions "
        "FROM measurement_events WHERE received_at >= datetime('now', ?) "
        "GROUP BY channel ORDER BY visitors DESC, channel LIMIT 20", (window,)
    ).fetchall()]
    paid = conn.execute(
        "SELECT COUNT(*) FROM measurement_orders WHERE paid_at IS NOT NULL "
        "AND paid_at >= datetime('now', ?)", (window,)
    ).fetchone()[0]
    return {"days": days, "events": by_event, "funnel": funnel, "channels": channels, "paid_orders": paid}
