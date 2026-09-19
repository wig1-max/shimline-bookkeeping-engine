"""The client's tax rates, named, and sorted onto the right return.

`sales_tax.period` reports per tax-rate reference. A return line that cannot
name its own rate cannot be filed, so this reads the rates and the agencies that
administer them.

The classification is the part that matters
-------------------------------------------
**PST and QST are not on a GST/HST return.** Provincial sales tax is not an
input tax credit, and quietly including it would overstate the credit claimed on
a return filed with the CRA under the client's name. The return would look
entirely reasonable, which is what makes it the most expensive mistake available
in this lane.

So the question is never skipped. A rate is classified from the tax agency
QuickBooks says administers it; failing that, from its own name, but only when
the name is unambiguous; and a rate that neither settles is `unknown` and
**blocks the return**, naming itself. An operator can decide it, and their
decision is stored apart from the synced row so that re-reading the provider
cannot quietly overwrite a judgement a person made.

Why the agency and not the name
-------------------------------
"GST" in a rate name is good evidence and "PST" is good evidence, but "Tax" and
"Sales Tax" and "HST/PST BC" are not, and a client may rename anything. The
agency is what QuickBooks itself uses to decide which return a rate belongs to,
which makes it the honest primary signal and the name a fallback rather than a
guess dressed as one.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

GST_HST = "gst_hst"
PROVINCIAL = "provincial"
UNKNOWN = "unknown"

# The federal administrator, however a client has spelled it. Matched on the
# agency name rather than on an id, because QuickBooks assigns agency ids per
# company file.
_FEDERAL_AGENCY = re.compile(
    r"canada\s*revenue|revenue\s*canada|\bcra\b|agence\s*du\s*revenu\s*du\s*canada",
    re.IGNORECASE)

# Provincial administrators, which run PST, QST and their equivalents. This list
# is evidence, not an authority: an agency absent from it does not become
# federal by default, it becomes a question.
_PROVINCIAL_AGENCY = re.compile(
    r"revenu\s*qu|minist(ry|ère|ere)\s*(of|des?)\s*finance|"
    r"manitoba\s*finance|saskatchewan\s*finance|"
    r"british\s*columbia|bc\s*ministry|"
    r"provincial\s*(sales\s*)?tax|revenue\s*services",
    re.IGNORECASE)

# Names that settle the question on their own. Deliberately narrow: "Tax",
# "Sales Tax" and "Standard" settle nothing and must fall through to a person.
_FEDERAL_NAME = re.compile(r"^\s*(gst|hst|tps|tvh)\b", re.IGNORECASE)
_PROVINCIAL_NAME = re.compile(r"^\s*(pst|qst|rst|tvq)\b", re.IGNORECASE)

# A name carrying both is a combined rate, and combined rates are exactly the
# case that must not be waved through: part of it belongs on the return and part
# of it does not, and nothing here can split them.
_BOTH_NAME = re.compile(
    r"(gst|hst|tps|tvh).*(pst|qst|rst|tvq)|(pst|qst|rst|tvq).*(gst|hst|tps|tvh)",
    re.IGNORECASE)


@dataclass(frozen=True)
class TaxRate:
    provider_id: str
    name: str
    rate_percent: Decimal | None
    classification: str
    classification_source: str
    agency_name: str = ""
    active: bool = True

    @property
    def on_gst_return(self) -> bool:
        return self.classification == GST_HST

    @property
    def undecided(self) -> bool:
        return self.classification == UNKNOWN

    def label(self) -> str:
        """What an accountant reads on a return line."""
        if self.rate_percent is None:
            return self.name
        percent = self.rate_percent.normalize()
        return f"{self.name} {percent}%"


def percent(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def classify(name: str, agency_name: str = "") -> tuple[str, str]:
    """Which return a rate belongs on, and what decided it.

    Returns `(classification, source)`. The agency is asked first because it is
    what QuickBooks itself uses; the name is a fallback and only when it is
    unambiguous. Everything else is a question for a person, which is the whole
    point -- an unasked question here becomes a wrong number on a CRA filing.
    """
    agency_name = (agency_name or "").strip()
    if agency_name:
        if _FEDERAL_AGENCY.search(agency_name):
            return GST_HST, "agency"
        if _PROVINCIAL_AGENCY.search(agency_name):
            return PROVINCIAL, "agency"

    label = (name or "").strip()
    if label:
        # Checked before either single-sided pattern: a combined rate matches
        # both, and letting the federal test win would claim provincial tax as
        # an input tax credit.
        if _BOTH_NAME.search(label):
            return UNKNOWN, "none"
        if _FEDERAL_NAME.match(label):
            return GST_HST, "name"
        if _PROVINCIAL_NAME.match(label):
            return PROVINCIAL, "name"
    return UNKNOWN, "none"


# ------------------------------------------------------------- persistence --

def persist(conn: sqlite3.Connection, organization_id: str,
            objects: dict[str, list[dict]], provider: str = "quickbooks") -> int:
    """Store the agencies and rates from a provider pull, classifying each.

    An operator's decision is applied last and always wins. Re-reading the
    provider must never quietly undo a judgement a person made about which
    return a rate belongs on.
    """
    agencies: dict[str, str] = {}
    for item in objects.get("TaxAgency", []) or []:
        provider_id = str(item.get("Id") or "")
        name = str(item.get("DisplayName") or item.get("TaxAgencyName")
                   or item.get("Name") or "")
        if not provider_id:
            continue
        agencies[provider_id] = name
        conn.execute(
            "INSERT INTO bookkeeping_tax_agencies("
            "id,organization_id,provider,provider_id,name,sync_token) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(organization_id,provider,provider_id) DO UPDATE SET "
            "name=excluded.name, sync_token=excluded.sync_token",
            (f"tag_{organization_id}_{provider_id}", organization_id, provider,
             provider_id, name, str(item.get("SyncToken", ""))))

    overrides = {
        str(row[0]): str(row[1]) for row in conn.execute(
            "SELECT provider_id, classification FROM bookkeeping_tax_rate_overrides "
            "WHERE organization_id=? AND provider=?", (organization_id, provider))}

    stored = 0
    for item in objects.get("TaxRate", []) or []:
        provider_id = str(item.get("Id") or "")
        if not provider_id:
            continue
        name = str(item.get("Name") or provider_id)
        agency_id = str((item.get("AgencyRef") or {}).get("value") or "")
        agency_name = agencies.get(agency_id) or str(
            (item.get("AgencyRef") or {}).get("name") or "")
        classification, source = classify(name, agency_name)
        if provider_id in overrides:
            classification, source = overrides[provider_id], "operator"
        conn.execute(
            "INSERT INTO bookkeeping_tax_rates("
            "id,organization_id,provider,provider_id,name,description,rate_percent,"
            "agency_provider_id,special_tax_type,active,sync_token,"
            "classification,classification_source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(organization_id,provider,provider_id) DO UPDATE SET "
            "name=excluded.name, description=excluded.description,"
            "rate_percent=excluded.rate_percent,"
            "agency_provider_id=excluded.agency_provider_id,"
            "special_tax_type=excluded.special_tax_type, active=excluded.active,"
            "sync_token=excluded.sync_token, classification=excluded.classification,"
            "classification_source=excluded.classification_source",
            (f"txr_{organization_id}_{provider_id}", organization_id, provider,
             provider_id, name, item.get("Description"),
             None if percent(item.get("RateValue")) is None
             else str(percent(item.get("RateValue"))),
             agency_id or None, item.get("SpecialTaxType"),
             int(bool(item.get("Active", True))), str(item.get("SyncToken", "")),
             classification, source))
        stored += 1
    return stored


def decide(conn: sqlite3.Connection, organization_id: str, provider_id: str,
           classification: str, *, user_id: str | None = None,
           reason: str = "", provider: str = "quickbooks") -> None:
    """Record a person's decision about which return a rate belongs on."""
    if classification not in {GST_HST, PROVINCIAL}:
        raise ValueError("A rate is on the GST/HST return or it is provincial")
    conn.execute(
        "INSERT INTO bookkeeping_tax_rate_overrides("
        "organization_id,provider,provider_id,classification,decided_by_user_id,reason) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(organization_id,provider,provider_id) DO UPDATE SET "
        "classification=excluded.classification,"
        "decided_by_user_id=excluded.decided_by_user_id, reason=excluded.reason,"
        "decided_at=CURRENT_TIMESTAMP",
        (organization_id, provider, provider_id, classification, user_id,
         (reason or "")[:2000] or None))
    conn.execute(
        "UPDATE bookkeeping_tax_rates SET classification=?, "
        "classification_source='operator' "
        "WHERE organization_id=? AND provider=? AND provider_id=?",
        (classification, organization_id, provider, provider_id))


def registry(conn: sqlite3.Connection, organization_id: str,
             provider: str = "quickbooks") -> dict[str, TaxRate]:
    """Every known rate for a client, keyed by the reference a document uses."""
    rows = conn.execute(
        "SELECT r.provider_id, r.name, r.rate_percent, r.classification, "
        "       r.classification_source, COALESCE(a.name,''), r.active "
        "FROM bookkeeping_tax_rates r "
        "LEFT JOIN bookkeeping_tax_agencies a "
        "       ON a.organization_id = r.organization_id "
        "      AND a.provider = r.provider "
        "      AND a.provider_id = r.agency_provider_id "
        "WHERE r.organization_id=? AND r.provider=? ORDER BY r.name",
        (organization_id, provider)).fetchall()
    return {str(row[0]): TaxRate(
        provider_id=str(row[0]), name=str(row[1]), rate_percent=percent(row[2]),
        classification=str(row[3]), classification_source=str(row[4]),
        agency_name=str(row[5]), active=bool(row[6])) for row in rows}


def undecided(conn: sqlite3.Connection, organization_id: str,
              provider: str = "quickbooks") -> list[TaxRate]:
    """Rates nobody has sorted onto a return yet. Each one blocks a filing."""
    return [rate for rate in registry(conn, organization_id, provider).values()
            if rate.undecided]
