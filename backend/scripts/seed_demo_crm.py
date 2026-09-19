"""Seed a disposable local database for visual QA. Never run in production.

Refuses to touch the default database path, so a mistyped command cannot
write invented clients into the live intake database.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
from shimline import auth, clock, crm


def stamp(value: datetime) -> str:
    return clock.format_timestamp(value)


def main() -> int:
    target = os.environ.get("SHIMLINE_DEMO_DB", "").strip()
    if not target:
        raise SystemExit("SHIMLINE_DEMO_DB is required")
    target_path = Path(target).resolve()
    forbidden = {(Path(app.BASE_DIR) / "intake.db").resolve()}
    if target_path in forbidden or "/opt/shimline-intake" in target_path.as_posix():
        raise SystemExit("Refusing to seed the working or deployed database")
    app.DB_PATH = target_path
    conn = app._db()
    user = conn.execute("SELECT id,display_name FROM users LIMIT 1").fetchone()
    if user:
        user_id = user[0]
    else:
        user_id = auth.create_user(conn, "demo@shimline.local", "Demo Operator", "local-demo-password-only", "owner")
    now = datetime.now(timezone.utc)

    samples = [
        ("Northstar Renovations", "August 2026", "in_progress", now + timedelta(days=2),
         "A cash timing issue from July could affect a new crew starting September 15.", 3),
        ("Maple Ridge Carpentry", "July 2026", "ready", now + timedelta(days=3),
         "Two uncategorized transactions need confirmation before review.", 1),
        ("Harbour Electrical", "August 2026", "internal_review", now + timedelta(days=6),
         "Payroll review is ready for internal sign-off.", 6),
        ("Peninsula Home Builds", "Q3 2026", "awaiting_client", now + timedelta(days=7),
         "Waiting for the August Visa statement.", 0),
        ("Coastal Concrete", "Q3 2026", "awaiting_client", now + timedelta(days=10),
         "Waiting for the inventory count and supplier statement.", 0),
    ]
    first_engagement = None
    first_organization = None
    for index, (name, period, status, due, reason, completed) in enumerate(samples):
        normalized = crm.normalize_company(name)
        row = conn.execute("SELECT id FROM organizations WHERE normalized_name=?", (normalized,)).fetchone()
        organization_id = row[0] if row else crm.new_id("org")
        if not row:
            conn.execute(
                "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage,city,specialty,"
                "owner_user_id,next_action,next_action_due_at) "
                "VALUES(?,?,?,'active','Toronto, ON','General contractor',?,'Advance current close',?)",
                (organization_id, name, normalized, user_id, stamp(due)),
            )
            conn.execute(
                "INSERT INTO opportunities(id,organization_id,stage,service_type,source,review_sold,last_touch_at) "
                "VALUES(?,?,'won','cash_leak_review','demo',1,?)",
                (crm.new_id("opp"), organization_id, stamp(now - timedelta(days=20))),
            )
            conn.execute(
                "INSERT INTO contacts(id,organization_id,name,email,phone,is_primary) VALUES(?,?,?,?,?,1)",
                (crm.new_id("con"), organization_id, "Sam Rivera",
                 f"owner{index}@example.invalid", "+1 416 555 0100"),
            )
        engagement_id = crm.new_id("eng")
        conn.execute(
            "INSERT INTO engagements(id,organization_id,title,accounting_period,status,priority_reason,"
            "assigned_user_id,ready_at,due_at) VALUES(?,?,'Monthly bookkeeping',?,?,?,?,?,?)",
            (engagement_id, organization_id, period, status, reason, user_id,
             stamp(now - timedelta(days=2)) if status != "awaiting_client" else None, stamp(due)),
        )
        for position, title in enumerate(crm.REVIEW_STEPS, 1):
            done = position <= completed
            conn.execute(
                "INSERT INTO work_items(id,engagement_id,title,status,position,completed_at) VALUES(?,?,?,?,?,?)",
                (crm.new_id("wrk"), engagement_id, title, "done" if done else "todo", position,
                 stamp(now - timedelta(days=1)) if done else None),
            )
        conn.execute(
            "INSERT INTO activities(id,organization_id,engagement_id,actor_user_id,kind,body,created_at) "
            "VALUES(?,?,?,?,'note',?,?)",
            (crm.new_id("act"), organization_id, engagement_id, user_id,
             "Called the owner about the missing statement.", stamp(now - timedelta(days=1))),
        )
        if first_engagement is None:
            first_engagement, first_organization = engagement_id, organization_id

    # One paid intake with documents, so the money and retention panels render.
    submission_id = "aa11bb22cc33"
    if not conn.execute("SELECT 1 FROM submissions WHERE id=?", (submission_id,)).fetchone():
        conn.execute(
            "INSERT INTO submissions(id,created_at,company,contact_name,email,phone,checklist,notes,files,"
            "organization_id,engagement_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (submission_id, stamp(now - timedelta(days=4)), "Northstar Renovations", "Sam Rivera",
             "owner0@example.invalid", "+1 416 555 0100", "P&L, balance sheet, bank exports",
             "Two crews, one truck loan.", "profit_and_loss.csv,bank_export.csv",
             first_organization, first_engagement),
        )
        conn.execute(
            "INSERT INTO payments(order_id,payment_id,amount,currency,status,email,created_at,paid_at,submission_id) "
            "VALUES('order_demo','pay_demo',19900,'CAD','paid',?,?,?,?)",
            ("owner0@example.invalid", stamp(now - timedelta(days=4)),
             stamp(now - timedelta(days=4)), submission_id),
        )
        conn.execute("UPDATE engagements SET submission_id=? WHERE id=?", (submission_id, first_engagement))
        folder = app.UPLOADS_DIR / submission_id
        folder.mkdir(parents=True, exist_ok=True)
        for filename in ("profit_and_loss.csv", "bank_export.csv"):
            (folder / filename).write_text("account,amount\nsynthetic,0\n", encoding="utf-8", newline="")

    prospects = [
        ("Granite Ridge Roofing", "Hamilton, ON", "Roofing", "replied"),
        ("Lakeshore Plumbing", "Oakville, ON", "Plumbing", "contacted"),
        ("Fairview Landscaping", "Burlington, ON", "Landscaping", "new"),
    ]
    for name, city, specialty, stage in prospects:
        normalized = crm.normalize_company(name)
        if conn.execute("SELECT 1 FROM organizations WHERE normalized_name=?", (normalized,)).fetchone():
            continue
        organization_id = crm.new_id("org")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage,city,specialty,next_action) "
            "VALUES(?,?,?,'prospect',?,?,'Research decision maker')",
            (organization_id, name, normalized, city, specialty),
        )
        conn.execute(
            "INSERT INTO opportunities(id,organization_id,stage,service_type,source,sequence_step,last_touch_at) "
            "VALUES(?,?,?,'cash_leak_review','demo','email 2',?)",
            (crm.new_id("opp"), organization_id, stage, stamp(now - timedelta(days=6))),
        )

    conn.execute(
        "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary,created_at) "
        "VALUES(?,?,'engagement.status','engagement',?,'awaiting_client -> in_progress',?)",
        (crm.new_id("aud"), user_id, first_engagement, stamp(now - timedelta(days=2))),
    )
    conn.commit()
    conn.close()
    print(f"Disposable demo data created in {target_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
