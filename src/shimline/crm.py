"""CRM domain helpers shared by intake, imports, and the admin workspace."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid

from .clock import add_business_days, parse_timestamp  # noqa: F401  (re-exported)


REVIEW_STEPS = (
    "Check required documents",
    "Request missing items",
    "Normalize and validate inputs",
    "Run cash-leak analysis",
    "Review findings and evidence",
    "Prepare client deliverable",
    "Internal quality review",
    "Deliver review",
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def normalize_company(value: str) -> str:
    value = (value or "").casefold().strip()
    value = re.sub(r"\b(incorporated|inc|limited|ltd|corporation|corp)\b", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)


#: Below this length a shared prefix means nothing -- "abc" prefixes far too
#: many real company names to be evidence that two rows are the same business.
MINIMUM_ALIAS_STEM = 5


def find_organization(conn, normalized: str) -> str | None:
    """Resolve a normalized company name to an organization, aliases included.

    A company can already be in the CRM under a name that later research
    corrects.  Looking through aliases means the correction is recorded once
    and every later import merges into the organization that already holds the
    contacts, instead of quietly creating a second one.
    """
    if not normalized:
        return None
    row = conn.execute(
        "SELECT id FROM organizations WHERE normalized_name=?", (normalized,)
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT organization_id FROM organization_aliases WHERE normalized_name=?", (normalized,)
    ).fetchone()
    return row[0] if row else None


def similar_organization(conn, normalized: str) -> dict | None:
    """Find an organization whose name looks like the same company.

    Deliberately narrow: one normalized name must be a prefix of the other, so
    "granstone" matches "granstonerenovations" but two merely similar names do
    not.  A hit is never acted on -- it becomes a conflict a person resolves.
    A false positive therefore costs one question, while a miss costs a
    duplicate that nobody notices, so the rule errs toward asking.
    """
    if len(normalized) < MINIMUM_ALIAS_STEM:
        return None
    for row in conn.execute("SELECT id,name,normalized_name FROM organizations"):
        other = row[2] or ""
        if other == normalized or len(other) < MINIMUM_ALIAS_STEM:
            continue
        if other.startswith(normalized) or normalized.startswith(other):
            return {"id": row[0], "name": row[1], "normalized_name": other}
    return None


def record_alias(conn, organization_id: str, alias_name: str, reason: str,
                 user_id: str | None = None) -> bool:
    """Remember that `alias_name` names an organization already in the CRM."""
    normalized = normalize_company(alias_name)
    if not normalized:
        return False
    current = conn.execute(
        "SELECT normalized_name FROM organizations WHERE id=?", (organization_id,)
    ).fetchone()
    if current and current[0] == normalized:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO organization_aliases"
        "(normalized_name,organization_id,alias_name,reason,created_by) VALUES(?,?,?,?,?)",
        (normalized, organization_id, alias_name.strip(), reason, user_id),
    )
    return True


def rename_organization(conn, organization_id: str, new_name: str,
                        user_id: str | None = None) -> str:
    """Rename a company and keep its former name resolvable.

    Renaming without recording the old name is what turns the next import of
    the pre-correction spelling into a duplicate.
    """
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("An organization needs a name")
    current = conn.execute(
        "SELECT name,normalized_name FROM organizations WHERE id=?", (organization_id,)
    ).fetchone()
    if not current:
        raise ValueError("Organization not found")
    normalized = normalize_company(new_name)
    if not normalized:
        raise ValueError(f"{new_name!r} does not normalize to a company name")
    clash = conn.execute(
        "SELECT id FROM organizations WHERE normalized_name=? AND id<>?",
        (normalized, organization_id),
    ).fetchone()
    if clash:
        raise ValueError(f"{new_name!r} is already another organization")
    conn.execute(
        "UPDATE organizations SET name=?,normalized_name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (new_name, normalized, organization_id),
    )
    # The alias points at this organization, so it must be written after the
    # rename lands; before it, the old name is still the current one.
    conn.execute(
        "DELETE FROM organization_aliases WHERE normalized_name=?", (normalized,))
    record_alias(conn, organization_id, current[0], "rename", user_id)
    return normalized


def ensure_intake_engagement(
    conn,
    *,
    submission_id: str,
    company: str,
    contact_name: str,
    email: str,
    phone: str,
) -> tuple[str, str]:
    """Create or link the CRM records for a paid intake in the same transaction."""
    company_name = (company or "").strip() or f"Unspecified client {submission_id}"
    normalized = normalize_company(company_name) or submission_id
    # Aliases count here too: a client who pays under the name the CRM knew
    # before a correction is the same client, and must land on the record that
    # already holds their history rather than on a fresh empty one.
    existing_id = find_organization(conn, normalized)
    if existing_id:
        organization_id = existing_id
        conn.execute(
            "UPDATE organizations SET lifecycle_stage = CASE WHEN lifecycle_stage IN ('prospect','qualified','proposal') "
            "THEN 'onboarding' ELSE lifecycle_stage END, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (organization_id,),
        )
    else:
        organization_id = new_id("org")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage,next_action) "
            "VALUES(?,?,?,'onboarding','Check submitted documents')",
            (organization_id, company_name, normalized),
        )

    if (email or contact_name or phone) and not conn.execute(
        "SELECT 1 FROM contacts WHERE organization_id=? AND "
        "((email IS NOT NULL AND email=? COLLATE NOCASE) OR (email IS NULL AND COALESCE(name,'')=?))",
        (organization_id, (email or "").strip(), (contact_name or "").strip()),
    ).fetchone():
        conn.execute(
            "INSERT INTO contacts(id,organization_id,name,email,phone,is_primary) VALUES(?,?,?,?,?,1)",
            (new_id("con"), organization_id, (contact_name or "").strip() or None,
             (email or "").strip() or None, (phone or "").strip() or None),
        )

    opportunity = conn.execute(
        "SELECT id FROM opportunities WHERE organization_id=? AND service_type='cash_leak_review' "
        "ORDER BY created_at DESC LIMIT 1", (organization_id,)
    ).fetchone()
    if opportunity:
        conn.execute(
            "UPDATE opportunities SET stage='won',review_sold=1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (opportunity[0],),
        )
    else:
        conn.execute(
            "INSERT INTO opportunities(id,organization_id,stage,service_type,review_sold) "
            "VALUES(?,?,'won','cash_leak_review',1)",
            (new_id("opp"), organization_id),
        )

    engagement_id = new_id("eng")
    conn.execute(
        "INSERT INTO engagements(id,organization_id,submission_id,title,status,priority_reason) "
        "VALUES(?,?,?,'Cash-Leak Review','awaiting_client','Confirm that all required documents arrived')",
        (engagement_id, organization_id, submission_id),
    )
    for position, title in enumerate(REVIEW_STEPS, start=1):
        conn.execute(
            "INSERT INTO work_items(id,engagement_id,title,position) VALUES(?,?,?,?)",
            (new_id("wrk"), engagement_id, title, position),
        )
    conn.execute(
        "UPDATE submissions SET organization_id=?, engagement_id=? WHERE id=?",
        (organization_id, engagement_id, submission_id),
    )
    conn.execute(
        "INSERT INTO activities(id,organization_id,engagement_id,kind,body) "
        "VALUES(?,?,?,'intake','Paid intake received')",
        (new_id("act"), organization_id, engagement_id),
    )
    return organization_id, engagement_id


def _truthy(value: str | None) -> int:
    return int(str(value or "").strip().casefold() in {"1", "y", "yes", "true", "sold"})


def _read_csv(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig")
    return [dict(row) for row in csv.DictReader(io.StringIO(text, newline=""))]


# These mirror the CHECK constraints in migration 011. A value the database
# will refuse has to be caught here, on the preview the operator reads, and not
# at apply time: the apply runs in one transaction, so a single mistyped cell
# would otherwise roll the whole batch back behind an opaque 500.
FIT_TIERS = ("A", "B", "C", "DISQUALIFIED")
OUTREACH_ROUTES = ("email", "phone", "contact_form", "linkedin", "directory_message", "none")


def _constrained(value: str | None, allowed: tuple[str, ...], *, upper: bool) -> str | None:
    """Return the canonical spelling of a constrained value, or None if unknown."""
    text = (value or "").strip()
    if not text:
        return ""
    candidate = text.upper() if upper else re.sub(r"[\s-]+", "_", text.casefold())
    return candidate if candidate in allowed else None


def _validate_payload(payload: dict) -> list[str]:
    """Normalize the constrained fields in place; report what cannot be stored."""
    problems = []
    for field, allowed, upper, label in (
        ("fit_tier", FIT_TIERS, True, "fit_tier"),
        ("contact_route", OUTREACH_ROUTES, False, "contact_route"),
    ):
        canonical = _constrained(payload.get(field), allowed, upper=upper)
        if canonical is None:
            problems.append(
                f"{label} {payload.get(field)!r} is not one of: " + ", ".join(allowed))
        else:
            payload[field] = canonical
    return problems


def preview_lead_import(conn, tracker_data: bytes, research_data: bytes, user_id: str) -> dict:
    tracker_rows = _read_csv(tracker_data)
    research_rows = _read_csv(research_data)
    merged: dict[str, dict] = {}
    for source, rows in (("research", research_rows), ("tracker", tracker_rows)):
        for row in rows:
            name = (row.get("company") or "").strip()
            key = normalize_company(name)
            if not key:
                continue
            item = merged.setdefault(key, {"company": name, "normalized_name": key})
            for field, value in row.items():
                value = (value or "").strip()
                if value and (not item.get(field) or source == "tracker"):
                    item[field] = value

    source_hash = hashlib.sha256(tracker_data + b"\0shimline-leads\0" + research_data).hexdigest()
    prior = conn.execute(
        "SELECT id,status,summary_json FROM import_batches WHERE source_hash=?", (source_hash,)
    ).fetchone()
    if prior:
        summary = json.loads(prior[2])
        summary.update({"batch_id": prior[0], "status": prior[1], "already_seen": True})
        return summary

    actions = []
    counts = {"create": 0, "merge": 0, "skip": 0, "conflict": 0}
    for key, payload in sorted(merged.items(), key=lambda pair: pair[1]["company"].casefold()):
        organization_id = find_organization(conn, key)
        problems = _validate_payload(payload)
        candidate = None
        if problems:
            # `skip` is the schema's existing word for a row the import does not
            # write. Nothing produced it before; a row the CRM cannot store is
            # exactly that case, so it needs no new state and no new migration.
            action = "skip"
        elif organization_id:
            action = "merge"
        else:
            # No exact or aliased match, but a name this close is more often a
            # corrected spelling than a second company. Creating would be the
            # silent duplicate; `conflict` asks instead, and the answer is
            # remembered as an alias so it is asked only once.
            candidate = similar_organization(conn, key)
            action = "conflict" if candidate else "create"
        counts[action] += 1
        actions.append({
            "row_key": key,
            "action": action,
            "organization_id": organization_id,
            "candidate": candidate,
            "company": payload["company"],
            "city": payload.get("city", ""),
            "specialty": payload.get("specialty", ""),
            "problems": problems,
            "payload": payload,
        })

    batch_id = new_id("imp")
    summary = {
        "batch_id": batch_id,
        "status": "preview",
        "already_seen": False,
        "source_rows": len(tracker_rows) + len(research_rows),
        "organizations": len(actions),
        "applicable": counts["create"] + counts["merge"],
        "counts": counts,
    }
    conn.execute(
        "INSERT INTO import_batches(id,source_hash,status,summary_json,created_by) VALUES(?,?,'preview',?,?)",
        (batch_id, source_hash, json.dumps(summary, sort_keys=True), user_id),
    )
    for item in actions:
        conn.execute(
            "INSERT INTO import_rows(batch_id,row_key,action,payload_json,candidate_organization_id) "
            "VALUES(?,?,?,?,?)",
            (batch_id, item["row_key"], item["action"], json.dumps(item["payload"], sort_keys=True),
             (item["candidate"] or {}).get("id")),
        )
    conn.commit()
    summary["rows"] = actions
    return summary


def get_import_preview(conn, batch_id: str) -> dict | None:
    batch = conn.execute(
        "SELECT id,status,summary_json FROM import_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not batch:
        return None
    summary = json.loads(batch[2])
    summary.update({"batch_id": batch[0], "status": batch[1]})
    summary["rows"] = []
    for row in conn.execute(
        "SELECT r.row_key,r.action,r.payload_json,r.candidate_organization_id,o.name "
        "FROM import_rows r LEFT JOIN organizations o ON o.id=r.candidate_organization_id "
        "WHERE r.batch_id=? ORDER BY r.row_key", (batch_id,)
    ):
        payload = json.loads(row[2])
        summary["rows"].append({
            "row_key": row[0], "action": row[1], "company": payload.get("company", ""),
            "city": payload.get("city", ""), "specialty": payload.get("specialty", ""),
            "candidate": {"id": row[3], "name": row[4]} if row[3] else None,
            # Re-derived rather than stored: validation is a pure function of the
            # payload, so the reason a row is unusable cannot drift from the row.
            "problems": _validate_payload(payload) if row[1] == "skip" else [],
            "payload": payload,
        })
    # Batches previewed before skip-on-invalid existed have no `applicable` in
    # their stored summary; derive it so an older preview still renders.
    summary.setdefault(
        "applicable", sum(1 for row in summary["rows"] if row["action"] != "skip"))
    return summary


def resolve_import_conflict(conn, batch_id: str, row_key: str, *,
                            same_as_organization_id: str | None, user_id: str) -> dict:
    """Answer one conflict row: an existing company under a new name, or new.

    Answering "same company" writes the alias, so the question is asked once
    rather than on every future import of that spelling.
    """
    row = conn.execute(
        "SELECT action,payload_json FROM import_rows WHERE batch_id=? AND row_key=?",
        (batch_id, row_key),
    ).fetchone()
    if not row:
        raise ValueError("Import row not found")
    if row[0] != "conflict":
        raise ValueError("That row is not awaiting a decision")
    batch = conn.execute(
        "SELECT status,summary_json FROM import_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not batch or batch[0] == "applied":
        raise ValueError("That import has already been applied")
    payload = json.loads(row[1])

    if same_as_organization_id:
        if not conn.execute(
            "SELECT 1 FROM organizations WHERE id=?", (same_as_organization_id,)
        ).fetchone():
            raise ValueError("Unknown organization")
        record_alias(conn, same_as_organization_id, payload.get("company", row_key),
                     "import_link", user_id)
        action = "merge"
    else:
        action = "create"
    conn.execute(
        "UPDATE import_rows SET action=?,candidate_organization_id=? WHERE batch_id=? AND row_key=?",
        (action, same_as_organization_id or None, batch_id, row_key),
    )

    summary = json.loads(batch[1])
    counts = summary.setdefault("counts", {})
    counts["conflict"] = max(0, counts.get("conflict", 0) - 1)
    counts[action] = counts.get(action, 0) + 1
    summary["applicable"] = counts.get("create", 0) + counts.get("merge", 0)
    conn.execute(
        "UPDATE import_batches SET summary_json=? WHERE id=?",
        (json.dumps(summary, sort_keys=True), batch_id),
    )
    conn.commit()
    return {"row_key": row_key, "action": action,
            "organization_id": same_as_organization_id or None}


def apply_lead_import(conn, batch_id: str, user_id: str) -> dict:
    preview = get_import_preview(conn, batch_id)
    if not preview:
        raise ValueError("Import preview not found")
    if preview["status"] == "applied":
        return preview
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in preview["rows"]:
            # A row the preview marked `skip` carries a value the schema will
            # refuse. Skipping it imports the rest of the file instead of
            # rolling the whole batch back; the preview says which rows and why.
            # A `conflict` row is held back for the same reason: the import
            # does not know whether it is a new company or a corrected name,
            # and guessing either way writes something wrong.
            if item["action"] in {"skip", "conflict"}:
                continue
            payload = item["payload"]
            key = item["row_key"]
            existing_id = find_organization(conn, key)
            if existing_id:
                organization_id = existing_id
                conn.execute(
                    "UPDATE organizations SET city=COALESCE(NULLIF(city,''),?),"
                    "category=COALESCE(NULLIF(category,''),?),specialty=COALESCE(NULLIF(specialty,''),?),"
                    "fit_notes=COALESCE(NULLIF(fit_notes,''),?),source_url=COALESCE(NULLIF(source_url,''),?),"
                    "website_url=COALESCE(NULLIF(website_url,''),?),"
                    "fit_tier=COALESCE(NULLIF(fit_tier,''),?),"
                    "buying_signal=COALESCE(NULLIF(buying_signal,''),?),"
                    "personalization_fact=COALESCE(NULLIF(personalization_fact,''),?),"
                    "next_action=COALESCE(NULLIF(?,''),next_action),"
                    "notes=COALESCE(NULLIF(notes,''),?),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (payload.get("city"), payload.get("category"), payload.get("specialty"),
                     payload.get("fit_notes"), payload.get("source_url"), payload.get("official_url"),
                     payload.get("fit_tier"), payload.get("buying_signal"),
                     payload.get("personalization_fact"), payload.get("next_action"),
                     payload.get("notes"), organization_id),
                )
            else:
                organization_id = new_id("org")
                conn.execute(
                    "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage,city,category,specialty,"
                    "fit_notes,source_url,website_url,fit_tier,buying_signal,personalization_fact,notes,next_action) "
                    "VALUES(?,?,?,'prospect',?,?,?,?,?,?,?,?,?,?,?)",
                    (organization_id, payload.get("company") or key, key, payload.get("city"),
                     payload.get("category"), payload.get("specialty"), payload.get("fit_notes"),
                     payload.get("source_url"), payload.get("official_url"), payload.get("fit_tier") or None,
                     payload.get("buying_signal"), payload.get("personalization_fact"), payload.get("notes"),
                     payload.get("next_action") or "Research decision maker"),
                )
            if payload.get("contact_name") or payload.get("email") or payload.get("phone"):
                email = payload.get("email") or None
                phone = payload.get("phone") or None
                contact = conn.execute(
                    "SELECT id FROM contacts WHERE organization_id=? AND "
                    "((? IS NOT NULL AND email=? COLLATE NOCASE) OR "
                    "(? IS NOT NULL AND phone=?) OR "
                    "(? IS NOT NULL AND name=? COLLATE NOCASE)) ORDER BY is_primary DESC LIMIT 1",
                    (organization_id, email, email, phone, phone,
                     payload.get("contact_name") or None, payload.get("contact_name") or None),
                ).fetchone()
                if contact:
                    conn.execute(
                        "UPDATE contacts SET name=COALESCE(NULLIF(name,''),?),"
                        "email=COALESCE(NULLIF(email,''),?),phone=COALESCE(NULLIF(phone,''),?),"
                        "title=COALESCE(NULLIF(title,''),?),source_url=COALESCE(NULLIF(source_url,''),?),"
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (payload.get("contact_name") or None, email, phone,
                         payload.get("contact_title") or None, payload.get("contact_source_url") or None,
                         contact[0]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO contacts(id,organization_id,name,email,phone,title,source_url,is_primary) "
                        "VALUES(?,?,?,?,?,?,?,1)",
                        (new_id("con"), organization_id, payload.get("contact_name") or None,
                         email, phone, payload.get("contact_title") or None,
                         payload.get("contact_source_url") or None),
                    )
            opportunity = conn.execute(
                "SELECT id FROM opportunities WHERE organization_id=? AND service_type='cash_leak_review'",
                (organization_id,),
            ).fetchone()
            stage = "won" if _truthy(payload.get("review_sold")) else (
                "replied" if _truthy(payload.get("replied")) else (
                    "contacted" if payload.get("sequence_step") or payload.get("date_last_touch") else "new"
                )
            )
            if opportunity:
                conn.execute(
                    "UPDATE opportunities SET stage=?,sequence_step=COALESCE(NULLIF(sequence_step,''),?),"
                    "last_touch_at=COALESCE(last_touch_at,?),replied=MAX(replied,?),sample_sent=MAX(sample_sent,?),"
                    "review_sold=MAX(review_sold,?),outreach_route=COALESCE(NULLIF(outreach_route,''),?),"
                    "contact_basis=COALESCE(NULLIF(contact_basis,''),?),"
                    "no_solicit_checked_at=COALESCE(NULLIF(no_solicit_checked_at,''),?),"
                    "relevance_reason=COALESCE(NULLIF(relevance_reason,''),?),"
                    "do_not_contact=MAX(do_not_contact,?),"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (stage, payload.get("sequence_step"), payload.get("date_last_touch"),
                     _truthy(payload.get("replied")), _truthy(payload.get("sample_sent")),
                     _truthy(payload.get("review_sold")), payload.get("contact_route") or None,
                     payload.get("public_contact_basis") or None,
                     payload.get("no_solicit_checked_at") or None,
                     payload.get("relevance_reason") or None,
                     _truthy(payload.get("do_not_contact")), opportunity[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO opportunities(id,organization_id,stage,service_type,source,sequence_step,last_touch_at,"
                    "replied,sample_sent,review_sold,outreach_route,contact_basis,no_solicit_checked_at,relevance_reason,"
                    "do_not_contact) VALUES(?,?,?,'cash_leak_review','lead_csv',?,?,?,?,?,?,?,?,?,?)",
                    (new_id("opp"), organization_id, stage, payload.get("sequence_step"),
                     payload.get("date_last_touch"), _truthy(payload.get("replied")),
                     _truthy(payload.get("sample_sent")), _truthy(payload.get("review_sold")),
                     payload.get("contact_route") or None, payload.get("public_contact_basis") or None,
                     payload.get("no_solicit_checked_at") or None,
                     payload.get("relevance_reason") or None,
                     _truthy(payload.get("do_not_contact"))),
                )
        conn.execute(
            "UPDATE import_batches SET status='applied',applied_at=CURRENT_TIMESTAMP WHERE id=?", (batch_id,)
        )
        conn.execute(
            "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) "
            "VALUES(?,?,'import.apply','import_batch',?,'Lead import applied')",
            (new_id("aud"), user_id, batch_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    preview["status"] = "applied"
    return preview



def disclose_professional_of_record(conn, engagement_id: str, *, firm_id: str,
                                    user_id: str) -> dict:
    """Name the firm and person responsible for an engagement, to the client.

    Shimline is one brand and one interface; the work has a named author and the
    client is told who. Both names are *copied* onto the engagement rather than
    joined at read time, because this is a disclosure and a disclosure has to
    keep reading as what was said at the time. A firm renames itself, a person
    leaves and their user row is deleted -- `assigned_user_id` is
    `ON DELETE SET NULL`, so a join would quietly answer "nobody" for work that
    somebody did.

    Refuses rather than disclosing something it cannot name. A blank name on a
    client-facing page is worse than no panel at all: it reads as an evasion of
    exactly the question the panel exists to answer.
    """
    firm = conn.execute("SELECT name FROM firms WHERE id=?", (firm_id,)).fetchone()
    if not firm:
        raise ValueError(f"No firm {firm_id}, so there is nobody to disclose.")
    person = conn.execute("SELECT display_name FROM users WHERE id=?",
                          (user_id,)).fetchone()
    if not person:
        raise ValueError(f"No user {user_id}, so there is nobody to disclose.")

    held = conn.execute(
        "SELECT 1 FROM firm_clients c JOIN engagements e "
        "               ON e.organization_id = c.organization_id "
        "WHERE e.id=? AND c.firm_id=?", (engagement_id, firm_id)).fetchone()
    if not held:
        # Naming a firm that was never engaged for this client would put a
        # disclosure in front of the client that is not true, and would be
        # created by a mistyped id.
        raise ValueError(
            "That firm does not hold this engagement's client, so it cannot be "
            "named as the professional of record for it.")

    firm_name, person_name = str(firm[0]), str(person[0])
    conn.execute(
        "UPDATE engagements SET firm_id=?, professional_name=?, "
        "professional_firm_name=?, professional_disclosed_at=CURRENT_TIMESTAMP "
        "WHERE id=?", (firm_id, person_name, firm_name, engagement_id))
    conn.execute(
        "INSERT INTO activities(id,organization_id,engagement_id,kind,body) "
        "SELECT ?, organization_id, id, 'note', ? FROM engagements WHERE id=?",
        (new_id("act"),
         f"{person_name} of {firm_name} named as professional of record.",
         engagement_id))
    return {"professional_name": person_name, "professional_firm_name": firm_name}
