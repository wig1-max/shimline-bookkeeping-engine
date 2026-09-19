"""What a real QuickBooks file turned out to contain, the first time one is read.

A dozen facts about Intuit's API have been taken from documentation and never
checked against a live company. Is `TaxAgency` queryable at all? Does
`TaxLine` carry an `Amount`? Does every `Purchase` name the account that paid it?
Each wrong assumption is a client whose ledger blocks, and until now the only way
to find out was for somebody to sit and watch a session pull a file.

So this runs unattended. The ten minutes of a person's time that OAuth genuinely
requires should produce a complete answer rather than a starting point.

Three rules this module holds to:

1. **An assumption nothing in the file could settle is `unknown`, not `held`.** A
   company with no taxed sale tells us nothing about `TaxLine`, and recording that
   as a pass manufactures evidence. This is the same reasoning as a readiness
   checklist step that is ticked on day one.
2. **A failed probe is a result.** "The pull died on TaxAgency" is the single most
   valuable thing this could discover, so it is recorded rather than discarded.
3. **It reports; it does not fix.** Nothing here writes to QuickBooks, and nothing
   relaxes a refusal to make a file look better than it is.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import crm, crypto
from .postings import POSTING_TYPES, derive_ledger
from .qbo_adapter import MANIFEST_KEY, READ_OBJECTS, QBOError
from .work_engine import checks_unaffected_by

HELD = "held"
FAILED = "failed"
UNKNOWN = "unknown"

# How many field names to record per entity. A real payload is much wider than
# the reader, and the point is to learn the shape, not to copy the file.
_SHAPE_LIMIT = 40


@dataclass
class Assumption:
    """One documented claim, and what a real file said about it."""

    name: str
    # What breaks if this is wrong, in the terms an operator cares about.
    consequence: str
    verdict: str = UNKNOWN
    detail: str = ""


@dataclass
class Probe:
    status: str = "ok"
    failed_at: str = ""
    failure: str = ""
    rows: dict[str, int] = field(default_factory=dict)
    shapes: dict[str, list[str]] = field(default_factory=dict)
    assumptions: list[Assumption] = field(default_factory=list)
    ledger_complete: bool = False
    # Every refusal, in the order the derivation produced them. The order is
    # itself the finding: the first one is what an operator would hit.
    refusals: list[str] = field(default_factory=list)
    documents_seen: int = 0
    documents_refused: int = 0
    # A conservative, per-check counterfactual for the quarantine decision.
    # It does not relax any block: it says which of the published sixteen use
    # none of the document types that were refused on this real file.
    quarantine_evaluable: bool = False
    quarantine_documents: int = 0
    quarantine_checks: list[dict] = field(default_factory=list)
    api_calls: int = 0

    def counted(self, verdict: str) -> int:
        return sum(1 for item in self.assumptions if item.verdict == verdict)

    @property
    def refusal_rate(self) -> float:
        """Documents the derivation could not post, as a fraction.

        This is the per-document refusal rate measured on real data. The
        all-or-nothing blocking estimate -- P(a whole ledger blocks) = 1-(1-p)^n
        -- has only ever had a guess for p.
        """
        if not self.documents_seen:
            return 0.0
        return self.documents_refused / self.documents_seen


def _count(number: int, singular: str, plural: str = "") -> str:
    """"1 row", "2 rows".

    Written out because this page is read by accountants, and "1 rows returned"
    on a page whose whole argument is that it is precise about what it observed
    costs more credibility than it saves effort.
    """
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


def _ref(value) -> str:
    return str((value or {}).get("value") or "") if isinstance(value, dict) else ""


def _documents(objects: dict) -> list[tuple[str, dict]]:
    return [(kind, row) for kind in POSTING_TYPES
            for row in (objects.get(kind) or [])]


def _accounts_by_type(objects: dict) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for account in objects.get("Account") or []:
        grouped.setdefault(str(account.get("AccountType") or ""), []).append(
            str(account.get("Id")))
    return grouped


# A document can survive longer than the name-list row it points at. QuickBooks'
# unqualified list queries return active rows, so an archived account, customer,
# vendor or class disappears while old transactions keep its id. Keep this map
# about named entities only: TaxRateRef is assessed by the GST/HST lane, and
# LinkedTxn points at another document rather than a name-list entity.
_NAMED_REFERENCE_KEYS = {
    "AccountRef": "Account",
    "ItemAccountRef": "Account",
    "ARAccountRef": "Account",
    "APAccountRef": "Account",
    "DepositToAccountRef": "Account",
    "FromAccountRef": "Account",
    "ToAccountRef": "Account",
    "BankAccountRef": "Account",
    "CCAccountRef": "Account",
    "CustomerRef": "Customer",
    "VendorRef": "Vendor",
    "ClassRef": "Class",
}


def named_references(objects: dict) -> dict[str, list[str]]:
    """Every named-entity id referenced by a posting document, by entity.

    Values are kept per occurrence rather than deduplicated: three documents
    pointing at one archived account are three documents exposed to the active-
    only pull, which is the blast radius the probe needs to measure.
    """
    found = {entity: [] for entity in ("Account", "Customer", "Vendor", "Class")}

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            entity = _NAMED_REFERENCE_KEYS.get(key)
            if key == "EntityRef" and isinstance(item, dict):
                entity = {"customer": "Customer", "vendor": "Vendor"}.get(
                    str(item.get("type") or "").casefold())
            if entity:
                reference = _ref(item)
                if reference:
                    found[entity].append(reference)
            visit(item)

    for _kind, document in _documents(objects):
        visit(document)
    return found


def _assess(objects: dict) -> list[Assumption]:
    """Every documentation-only assumption, judged against what was returned."""
    documents = _documents(objects)
    grouped = _accounts_by_type(objects)
    checks: list[Assumption] = []

    def add(name, consequence, *, applicable, held, detail=""):
        verdict = UNKNOWN if not applicable else (HELD if held else FAILED)
        checks.append(Assumption(name=name, consequence=consequence,
                                 verdict=verdict, detail=detail))

    # --- the two entities added from documentation alone ---------------------
    for entity in ("TaxRate", "TaxAgency"):
        present = entity in (objects.get(MANIFEST_KEY, {}).get("read") or {})
        add(f"{entity} is queryable",
            f"If {entity} cannot be queried, every pull for every client fails.",
            applicable=True, held=present,
            detail=_count(len(objects.get(entity) or []), "row") + " returned"
            if present else f"{entity} is absent from the pull manifest")

    # An unqualified QBO query returns active name-list rows. Transactions keep
    # their references after one of those rows is archived, so compare every id
    # a document names with the ids the same pull actually returned. This does
    # not infer that a missing id is archived; it records the observable shape.
    references = named_references(objects)
    consequences = {
        "Account": ("A posting to an account absent from the chart makes the "
                    "all-or-nothing ledger refuse the document."),
        "Customer": ("A customer or job absent from the pull makes project and "
                     "receivable evidence incomplete."),
        "Vendor": ("A vendor absent from the pull leaves payable and expense "
                   "evidence without the named counterparty."),
        "Class": ("A class absent from the pull makes class-based review "
                  "incomplete."),
    }
    labels = {"Account": "account", "Customer": "customer",
              "Vendor": "vendor", "Class": "class"}
    for entity in ("Account", "Customer", "Vendor", "Class"):
        returned = {str(row.get("Id")) for row in objects.get(entity) or []
                    if row.get("Id") is not None}
        named = references[entity]
        missing = [reference for reference in named if reference not in returned]
        missing_ids = sorted(set(missing))
        missing_detail = (
            _count(len(missing), "reference")
            + (" points" if len(missing) == 1 else " point")
            + f" at {_count(len(missing_ids), 'id')} absent from {entity}")
        add(f"Every referenced {labels[entity]} came back in the pull",
            consequences[entity], applicable=bool(named), held=not missing,
            detail=(_count(len(named), "reference") + "; " + missing_detail
                    + (": " + ", ".join(missing_ids[:5]) if missing_ids else "")))

    # Check 15's credit-balance rule only speaks when QuickBooks' own balance
    # agrees the account is negative. If a real chart does not carry
    # CurrentBalance, that rule is permanently silent and we should find out
    # here rather than from its never firing.
    chart = objects.get("Account") or []
    with_balance = [account for account in chart
                    if account.get("CurrentBalance") is not None]
    add("The chart states each account's current balance",
        "Without CurrentBalance nothing corroborates our own arithmetic per "
        "account, so the overdrawn-asset rule in check 15 never fires.",
        applicable=bool(chart), held=bool(with_balance),
        detail=_count(len(with_balance), "account") + " of "
               + _count(len(chart), "account") + " state a CurrentBalance")

    # --- the shapes the derivation depends on -------------------------------
    purchases = objects.get("Purchase") or []
    without_account = [row.get("Id") for row in purchases
                       if not _ref(row.get("AccountRef"))]
    add("Every Purchase names the account that paid it",
        "A Purchase with no AccountRef is underivable and blocks the ledger.",
        applicable=bool(purchases), held=not without_account,
        detail=_count(len(purchases), "purchase") + ", "
               + f"{len(without_account)} with no AccountRef"
               + (": " + ", ".join(map(str, without_account[:5]))
                  if without_account else ""))

    taxed = [(kind, row) for kind, row in documents
             if (row.get("TxnTaxDetail") or {}).get("TotalTax")]
    tax_lines = [line for _kind, row in taxed
                 for line in ((row.get("TxnTaxDetail") or {}).get("TaxLine") or [])]
    add("TxnTaxDetail.TaxLine carries an Amount",
        "Without a per-line amount, only the document's total tax is usable.",
        applicable=bool(tax_lines),
        held=all(line.get("Amount") is not None for line in tax_lines),
        detail=_count(len(tax_lines), "tax line") + " across "
               + _count(len(taxed), "taxed document"))

    # TaxLineDetail names a *rate*, not an account. Trusting it as an account id
    # once blocked every taxed document for every client, so this records which
    # it actually is rather than assuming either way.
    known = {str(account.get("Id")) for account in objects.get("Account") or []}
    named_accounts = [line for line in tax_lines
                      if _ref((line.get("TaxLineDetail") or {}).get("AccountRef"))]
    rate_refs_that_are_accounts = [
        line for line in tax_lines
        if _ref((line.get("TaxLineDetail") or {}).get("TaxRateRef")) in known]
    add("A tax line does not name the tax account",
        "If TaxRateRef happened to match an account id, the tax account would "
        "be resolved from a rate, which is how every taxed document once "
        "blocked. The fallback is a single tax-bearing liability account.",
        applicable=bool(tax_lines),
        held=not named_accounts and not rate_refs_that_are_accounts,
        detail=f"{len(named_accounts)} lines name an AccountRef; "
               f"{len(rate_refs_that_are_accounts)} TaxRateRefs collide with an "
               "account id")

    for label, account_type in (("A/R", "Accounts Receivable"),
                                ("A/P", "Accounts Payable")):
        ids = grouped.get(account_type) or []
        add(f"The chart has exactly one {label} account",
            f"With two, a document that does not name its own {label} account "
            "is refused rather than guessed at.",
            applicable=bool(ids), held=len(ids) == 1,
            detail=_count(len(ids), f"{account_type} account"))

    tax_liability = grouped.get("Other Current Liability") or []
    add("Exactly one tax-bearing liability account",
        "Two and no taxed document can say where its GST went, so every one is "
        "refused.",
        applicable=bool(taxed), held=len(tax_liability) == 1,
        detail=_count(len(tax_liability), "Other Current Liability account"))

    payments = objects.get("Payment") or []
    undeposited = [row.get("Id") for row in payments
                   if not _ref(row.get("DepositToAccountRef"))]
    add("Every Payment says where the money was deposited",
        "QuickBooks defaults this to Undeposited Funds without naming the "
        "account, and guessing puts the cash in the wrong place.",
        applicable=bool(payments), held=not undeposited,
        detail=_count(len(payments), "payment")
               + f", {len(undeposited)} with no DepositToAccountRef")

    # Multi-currency is refused by design. Whether this client has any is the
    # difference between "blocked" and "irrelevant" for them.
    foreign = [f"{kind} {row.get('Id')}" for kind, row in documents
               if _ref(row.get("CurrencyRef"))
               and str(_ref(row.get("CurrencyRef"))).upper() not in {"CAD", ""}]
    add("The file is single-currency",
        "Multi-currency documents are refused deliberately: converting has "
        "never been checked against a real file, and mis-stating a ledger by "
        "the exchange rate is worse than refusing it.",
        applicable=bool(documents), held=not foreign,
        detail=_count(len(foreign), "foreign-currency document")
               + (f" (e.g. {foreign[0]})" if foreign else ""))

    return checks


def probe(adapter) -> Probe:
    """Pull a connection and write down everything the pull revealed.

    Never raises for a provider failure: a pull that died is the finding, and an
    exception here would lose it.
    """
    result = Probe()
    try:
        objects = adapter.pull_all()
    except QBOError as exc:
        result.status = "failed"
        result.failure = str(exc)
        # pull_all names the entity in its message; record it as its own field so
        # an operator does not have to read prose to find out what broke.
        result.failed_at = next(
            (entity for entity in READ_OBJECTS if entity in str(exc)), "pull")
        result.api_calls = getattr(adapter, "api_call_count", 0)
        return result

    result.api_calls = getattr(adapter, "api_call_count", 0)
    manifest = objects.get(MANIFEST_KEY) or {}
    result.rows = dict(manifest.get("read") or {})
    for entity in READ_OBJECTS:
        rows = objects.get(entity) or []
        if rows and isinstance(rows[0], dict):
            result.shapes[entity] = sorted(rows[0])[:_SHAPE_LIMIT]

    result.assumptions = _assess(objects)

    ledger = derive_ledger(objects)
    result.ledger_complete = ledger.complete
    result.refusals = list(ledger.reasons())
    result.documents_seen = len(_documents(objects))
    document_refusals = [item for item in ledger.unsupported
                         if item.object_type in POSTING_TYPES]
    result.documents_refused = len(document_refusals)
    result.quarantine_documents = len(document_refusals)
    # Manifest gaps and unknown object types are not quarantinable documents:
    # there is no bounded row to set aside. Only report the counterfactual when
    # every block came from a known document type.
    result.quarantine_evaluable = bool(document_refusals) and (
        len(document_refusals) == len(ledger.unsupported))
    if result.quarantine_evaluable:
        result.quarantine_checks = checks_unaffected_by(
            {item.object_type for item in document_refusals})
    return result


# ------------------------------------------------------------- persistence --

def record(conn, *, connection_id: str, organization_id: str,
           result: Probe) -> str:
    probe_id = crm.new_id("probe")
    payload = crypto.encrypt(
        json.dumps({
            "failure": result.failure,
            "rows": result.rows,
            "shapes": result.shapes,
            "assumptions": [asdict(item) for item in result.assumptions],
            "refusals": result.refusals,
            "quarantine_evaluable": result.quarantine_evaluable,
            "quarantine_documents": result.quarantine_documents,
            "quarantine_checks": result.quarantine_checks,
        }, sort_keys=True), crypto.CONNECTION_PROBES)
    conn.execute(
        "INSERT INTO connection_probes(id,connection_id,organization_id,status,"
        "failed_at,documents_seen,documents_refused,ledger_complete,"
        "assumptions_held,assumptions_failed,assumptions_unknown,api_calls,"
        "payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (probe_id, connection_id, organization_id, result.status,
         result.failed_at or None, result.documents_seen,
         result.documents_refused, 1 if result.ledger_complete else 0,
         result.counted(HELD), result.counted(FAILED), result.counted(UNKNOWN),
         result.api_calls, payload))
    return probe_id


def latest(conn, organization_id: str) -> dict | None:
    """The most recent probe for a client, or None if none has ever run."""
    row = conn.execute(
        "SELECT id,connection_id,status,failed_at,documents_seen,"
        "documents_refused,ledger_complete,assumptions_held,assumptions_failed,"
        "assumptions_unknown,api_calls,payload,created_at "
        "FROM connection_probes WHERE organization_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (organization_id,)).fetchone()
    if not row:
        return None
    payload = json.loads(crypto.decrypt(row[11], crypto.CONNECTION_PROBES))
    return {
        "id": row[0], "connection_id": row[1], "status": row[2],
        "failed_at": row[3] or "", "documents_seen": row[4],
        "documents_refused": row[5], "ledger_complete": bool(row[6]),
        "assumptions_held": row[7], "assumptions_failed": row[8],
        "assumptions_unknown": row[9], "api_calls": row[10],
        "created_at": row[12], **payload,
    }


def needs_probe(conn) -> list[tuple[str, str]]:
    """Active connections with no probe yet: (connection_id, organization_id).

    This is what makes the owner's click pay off without anybody watching. A
    connection made at two in the morning is probed by the next timer run.
    """
    rows = conn.execute(
        "SELECT c.id,c.organization_id FROM connections c "
        "WHERE c.provider='quickbooks' AND c.status='active' "
        "AND NOT EXISTS (SELECT 1 FROM connection_probes p "
        "                WHERE p.connection_id=c.id AND p.status='ok') "
        "ORDER BY c.created_at").fetchall()
    return [(row[0], row[1]) for row in rows]
