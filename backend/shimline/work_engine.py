"""Bookkeeping Work Engine v0: reconstruct, detect, propose, control, prove.

The detection layer is provider-neutral even though v0's first adapter is QBO.
Amounts remain Decimal/string throughout; floats are accepted only at the
provider boundary.  Findings without sufficient evidence never become writes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Iterable

from . import crm
from . import matching
from .qbo_adapter import (MANIFEST_KEY as PULL_MANIFEST_KEY, MutationResult,
                          StaleObjectError, UncertainWriteError)
from .postings import (POSTING_TYPES, DerivedLedger, ProviderReportError,
                       compare_to_provider, derive_ledger, provider_balances)


def money(value=0) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass
class Proposal:
    proposal_id: str
    finding_key: str
    action: str
    object_type: str
    object_id: str | None
    current: dict
    proposed: dict
    reason: str
    financial_effect: Decimal = Decimal("0")
    tax_effect: Decimal = Decimal("0")
    expected_sync_token: str | None = None
    status: str = "proposed"
    version: int = 1
    readback: dict | None = None


@dataclass
class Finding:
    key: str
    defect_type: str
    severity: str
    title: str
    reason: str
    affected_type: str
    affected_id: str
    evidence_status: str
    evidence: list[str]
    financial_effect: Decimal = Decimal("0")
    tax_effect: Decimal = Decimal("0")
    proposal: Proposal | None = None
    evidence_request: str | None = None
    status: str = "open"


@dataclass
class Analysis:
    period_start: str
    period_end: str
    objects: dict[str, list[dict]]
    findings: list[Finding]
    coverage: dict
    proposals: list[Proposal] = field(default_factory=list)


@dataclass
class WorkResult:
    analysis: Analysis
    reconciliation: dict
    working_papers: dict
    unique_writes: int


def _key(defect_type: str, affected: str) -> str:
    return f"{defect_type}:{affected}"


# --------------------------------------------------------------- check registry
#
# Every check Shimline advertises, and the honest status of each one in v0.
# The Week 2 exit gate is that an advertised check either runs against real data
# or is *explicitly* marked with what stops it — never silently absent. So this
# registry is the single place a check is declared, and `analyze` reports a
# status for all of them rather than only for the ones that fired.
#
#   automated  the engine computes it; `defect_type` says which detector
#   blocked    it could be computed, but a required source is not present
#   manual     v0 does not automate it; a person covers it during review
#
# Adding a detector means moving one entry from `manual` to `automated`. Adding
# an advertised check means adding an entry here first — a check that is on the
# website and not in this registry is the failure mode this exists to prevent.
CHECKS: list[dict] = [
    {"id": "01", "title": "Overdue receivables",
     "mode": "automated", "defect_type": "stale_receivable",
     "inputs": ("Invoice",)},
    {"id": "02", "title": "Invoices issued late",
     "mode": "manual",
     "note": "Needs the date work was completed, which QBO does not hold."},
    {"id": "03", "title": "Unapplied customer payments",
     "mode": "automated", "defect_type": "unapplied_payment",
     "inputs": ("Payment",)},
    {"id": "04", "title": "Unreconciled accounts",
     "mode": "automated", "defect_type": "unreconciled_account",
     "requires": "bank_statement", "inputs": tuple(POSTING_TYPES)},
    {"id": "05", "title": "Uncategorized expenses",
     "mode": "automated", "defect_type": "uncategorized_expense",
     "inputs": tuple(POSTING_TYPES),
     "coverage_note": "Exact QuickBooks holding accounts only: Ask My "
                      "Accountant and Uncategorized Expense. Broader coding "
                      "quality remains a reviewer judgement."},
    {"id": "06", "title": "Duplicate or strange vendor charges",
     "mode": "automated", "defect_type": "duplicate_transaction",
     "inputs": ("Purchase",),
     "coverage_note": "Exact-duplicate detection only; unusual-but-distinct charges are reviewed by a person."},
    {"id": "07", "title": "Materials not allocated to jobs",
     "mode": "automated", "defect_type": "incorrect_job_allocation",
     "inputs": ("Purchase",)},
    {"id": "08", "title": "Labour and subcontractor costs misallocated",
     "mode": "automated", "defect_type": "incorrect_job_allocation",
     "inputs": ("Purchase", "Bill"),
     "coverage_note": "Shares the allocation detector; no labour-specific rules yet."},
    {"id": "09", "title": "Jobs with weak or negative gross margin",
     "mode": "automated", "defect_type": "negative_job_margin",
     "inputs": ("Invoice", "Purchase", "Bill"),
     "coverage_note": "Negative margin only. Where 'weak' begins is an "
                      "accounting judgement about the client's circumstances; "
                      "a job that cost more than it billed is arithmetic. "
                      "Needs revenue and cost both tagged to the job."},
    {"id": "10", "title": "Estimate versus actual differences",
     "mode": "manual",
     "note": "Estimates are now read and persisted, so the comparison is no "
             "longer blocked on a missing source. What it needs is a "
             "materiality rule -- how far a job may run over its quote before "
             "that is a finding -- which is an accounting judgement, not a "
             "detector. A reviewer makes the call until that rule is set."},
    {"id": "11", "title": "Change-order and invoicing mismatches",
     "mode": "blocked", "requires": "change_orders",
     "note": "Change orders live outside QBO and must be supplied as evidence."},
    {"id": "12", "title": "Abnormal vendor or material price movement",
     "mode": "automated", "defect_type": "abnormal_price",
     "inputs": ("Purchase", "Bill"),
     "coverage_note": "Compares a unit price against the median of that "
                      "vendor and item's own history, and only once there are "
                      "more than PRICE_HISTORY_MINIMUM prior purchases. Item-"
                      "based lines only: an account-based line carries no unit "
                      "price, so a client who bills that way is invisible here "
                      "and is covered by review."},
    {"id": "13", "title": "Stale outstanding bills",
     "mode": "automated", "defect_type": "stale_payable",
     "inputs": ("Bill",),
     "coverage_note": "Flags a bill past its due date that still carries a "
                      "balance. Reported, never proposed: paying a vendor is "
                      "the client's decision, not a ledger correction."},
    {"id": "14", "title": "Owner or personal transactions in the books",
     "mode": "manual", "note": "Needs an owner-account policy from the client."},
    {"id": "15", "title": "Suspicious balance-sheet accounts",
     "mode": "automated", "defect_type": "suspicious_balance_sheet",
     "requires": "trusted_ledger", "inputs": tuple(POSTING_TYPES),
     "coverage_note": "Flags a balance in Opening Balance Equity or "
                      "Undeposited Funds, and credit balances on asset "
                      "accounts. Reported for review; never auto-corrected. "
                      "The credit-balance rule assumes the pull spans the "
                      "company's whole history: on any partial ledger a bank "
                      "account that simply has not received its opening "
                      "balance reads as overdrawn, and that has not yet been "
                      "verified against a real file."},
    {"id": "16", "title": "Project profitability tagging consistency",
     "mode": "automated", "defect_type": "project_profitability_configuration",
     "inputs": ("Customer", "Invoice", "CreditMemo", "SalesReceipt",
                "RefundReceipt", "Purchase", "Bill", "VendorCredit"),
     "coverage_note": "When QuickBooks jobs exist and both revenue and cost "
                      "activity exist, checks that the two sides use jobs "
                      "consistently. Choosing whether to use jobs is a one-time "
                      "readiness step, not a recurring finding. It does not "
                      "judge whether an individual allocation is correct."},
    # Engine checks that are not on the published 16. Listed so the registry
    # describes the engine completely in both directions.
    {"id": "E1", "title": "Sales tax coding errors",
     "mode": "automated", "defect_type": "sales_tax_error"},
    {"id": "E2", "title": "Transactions on the statement and missing from the ledger",
     "mode": "automated", "defect_type": "missing_transaction",
     "requires": "bank_statement",
     "coverage_note": "Every statement line is matched against the "
                      "reconstructed cash movements on exact amount, a five-day "
                      "window and vendor-name evidence. A line nothing accounts "
                      "for is reported but never proposed -- the statement does "
                      "not say which account the charge belongs to. Two equally "
                      "good candidates are refused and left to a person rather "
                      "than guessed. A ledger entry with no statement line is "
                      "not a finding: an outstanding cheque is the ordinary "
                      "state of a month-end."},
]

AUTOMATED_DEFECT_TYPES = {item["defect_type"] for item in CHECKS if item["mode"] == "automated"}


def checks_unaffected_by(refused_types: set[str]) -> list[dict]:
    """Published automated checks whose inputs exclude every refused type.

    This is evidence for a future quarantine decision, not quarantine itself.
    A check is counted only when the registry names its inputs and none overlap
    a refused document type. Manual checks and missing-source checks are never
    counted. The deliberately conservative rule means the number is a lower
    bound: it cannot claim a check that might have depended on a skipped row.
    """
    return [
        {"id": check["id"], "title": check["title"]}
        for check in CHECKS
        if check["id"].isdigit()
        and check["mode"] == "automated"
        and check.get("inputs")
        and not (set(check["inputs"]) & refused_types)
        and not check.get("requires")
    ]

# Evidence keys that a check's `requires` may be satisfied by. A statement may
# arrive as one dict or as a list of them, and both must count as "the source is
# present" for coverage reporting.
_EVIDENCE_ALIASES = {"bank_statement": ("bank_statements", "bank_statement")}


def statements(evidence: dict) -> list[dict]:
    """Every bank statement in an evidence dict, in one shape.

    The engine was written when a client had one account, so `bank_statement`
    held a single dict. The supported envelope allows up to four bank and card
    accounts, and reconcile() already read a `bank_statements` list. Both spellings
    are accepted here so that the singular form -- which the synthetic oracle and
    the acceptance suite use -- keeps working, while a real ingest can supply
    several accounts and have every one of them checked rather than just the first.
    """
    supplied = evidence.get("bank_statements")
    if supplied is None:
        single = evidence.get("bank_statement")
        supplied = [single] if single else []
    return [item for item in supplied if item]


def _has_evidence(evidence: dict, required: str) -> bool:
    for key in _EVIDENCE_ALIASES.get(required, (required,)):
        if evidence.get(key):
            return True
    return False


# Transaction kinds that post to the ledger. `POSTING_TYPES` itself, never a
# copy of it.
#
# This was a hand-written copy of the original six, and it went stale when six
# settlement and reversal types were added to the engine. The consequence was
# not cosmetic. `postings_derivable` below treats "no posted transactions" as
# "zero really is this company's balance" and returns True without consulting
# the derivation -- so a cash-basis trades business ringing everything through
# the till, whose documents are *all* SalesReceipts, was reported as having a
# usable ledger even when none could be derived. Every balance-comparing check
# then read `clean` -- ran and found nothing -- instead of `blocked`.
#
# That is the precise failure this architecture exists to prevent, and it is the
# third time a duplicated copy of this list has caused one.
POSTED_KINDS = POSTING_TYPES

# Fallback term for a bill the provider sends without a DueDate. QBO allows
# a bill to carry none, and treating "no due date" as "due on the day it was
# raised" would flag every unpaid bill the moment it aged a day.
DEFAULT_PAYMENT_TERM_DAYS = 30

# Check 12 thresholds. Both are deliberately blunt. With a handful of
# observations per vendor and item there is no distribution to reason about, so
# a multiple of the median is honest where a z-score would be theatre. Raise
# PRICE_HISTORY_MINIMUM before lowering PRICE_SPIKE_MULTIPLE: more history is
# what makes a tighter threshold meaningful.
PRICE_HISTORY_MINIMUM = 3
PRICE_SPIKE_MULTIPLE = Decimal("2")

UNCATEGORIZED_ACCOUNT_NAMES = {
    "ask my accountant", "uncategorized expense", "uncategorized expenses",
}
ASSET_ACCOUNT_TYPES = {
    "Bank", "Accounts Receivable", "Other Current Asset", "Fixed Asset", "Other Asset",
}


def _normalized_account_name(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _transaction_date(transaction: dict) -> date | None:
    """The document's date, or None when it does not state a readable one.

    `date.fromisoformat(txn["TxnDate"])` raised `KeyError` on a document with no
    date and `ValueError` on an unparseable one, and either took a client's
    whole scan down with it. Falling over is not refusing. Every caller has to
    decide what an undated document means for its own check, which is why this
    returns None rather than a default: a date invented here would silently age
    a receivable from the wrong day.
    """
    stated = transaction.get("TxnDate")
    if not stated:
        return None
    try:
        return date.fromisoformat(str(stated)[:10])
    except ValueError:
        return None


def _provider_agrees_it_is_negative(account: dict) -> bool:
    """Whether QuickBooks' own balance for this account is negative too.

    A credit balance on an asset is only a finding if the balance is right, and
    ours is only right if the reconstruction covers that account's whole
    history. It does not always: a file pulled as a period, or an account whose
    opening balance predates the documents we hold, produces a bank account that
    reads as overdrawn purely because the money going in was never in the pull.
    Unguarded, this rule fired on every company in the synthetic corpus and on
    the documented conformance company -- and a detector that fires on
    everything is worse than no detector.

    `Account.CurrentBalance` is QuickBooks' own answer, and it arrives with
    every chart. Requiring it to agree on the *sign* is a deliberately weak
    corroboration: it does not assume the two numbers match, only that both
    engines think the account is negative. An absent `CurrentBalance` means the
    claim cannot be corroborated, so nothing is reported -- silence is the
    correct answer to a question the data cannot settle.
    """
    stated = account.get("CurrentBalance")
    if stated is None:
        return False
    try:
        return money(stated) < 0
    except (TypeError, ValueError, InvalidOperation):
        return False


def _line_account_ref(line: dict) -> str:
    for key in ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail",
                "SalesItemLineDetail", "DepositLineDetail", "JournalEntryLineDetail"):
        detail = line.get(key) or {}
        for ref in ("AccountRef", "ItemAccountRef"):
            account = str((detail.get(ref) or {}).get("value") or "").strip()
            if account:
                return account
    return ""


def ledger_balances(objects: dict[str, list[dict]]) -> tuple[dict, DerivedLedger]:
    """Account balances for these objects, and the evidence they can be trusted.

    Two sources, one answer. The synthetic oracle attaches `_Postings` to every
    transaction; the live QBO adapter attaches none, because QuickBooks publishes
    documents and leaves the postings implied by the document type.
    `postings.derive` reconstructs them and refuses, per transaction, anything it
    cannot rebuild without inventing a fact.

    The returned `DerivedLedger` carries `complete`. A caller must not use the
    balances unless it is true: a partial reconstruction silently shifts an
    account, and a silently shifted balance is a wrong set of books that looks
    right.
    """
    derived = derive_ledger(objects)
    return derived.balances, derived


def postings_derivable(objects: dict[str, list[dict]]) -> bool:
    """Can a ledger balance be computed from these objects at all?

    The distinction this draws is the difference between "this account is out by
    the whole statement balance" and "we never computed a ledger balance to
    compare it against". The first is a finding. The second is a missing source,
    and reporting it as a finding would put a high-severity defect on every
    account of every real client.

    A company with no posted transactions is a legitimate separate case: zero
    really is its balance, and a statement that disagrees is a real exception.
    """
    posted = [txn for kind in POSTED_KINDS for txn in objects.get(kind, [])]
    if not posted:
        return True
    return derive_ledger(objects).complete


def check_coverage(findings: list["Finding"], evidence: dict,
                   *, ledger_available: bool = True) -> list[dict]:
    """Report a status for every advertised check, whether or not it fired.

    `clean` means the check ran and found nothing — which is a result, not an
    absence. `blocked` names the source that would unblock it.

    `ledger_available` is false when no ledger balance could be derived from the
    provider objects. Balance-comparing checks are blocked rather than clean in
    that case, because a check that could not run has not found nothing.
    """
    found = {item.defect_type for item in findings}
    report = []
    for check in CHECKS:
        entry = {"id": check["id"], "title": check["title"], "mode": check["mode"]}
        required = check.get("requires")
        if check["mode"] == "automated":
            if required in {"bank_statement", "trusted_ledger"} and not ledger_available:
                entry["status"] = "blocked"
                entry["missing_source"] = "derived_ledger_postings"
                entry["note"] = (
                    "No trusted ledger balance could be derived from the "
                    "provider objects, so this balance-based check did not run.")
                for key in ("note", "coverage_note"):
                    if check.get(key) and key != "note":
                        entry[key] = check[key]
                report.append(entry)
                continue
            if required and required != "trusted_ledger" and not _has_evidence(evidence, required):
                entry["status"] = "blocked"
                entry["missing_source"] = required
            elif check["defect_type"] in found:
                entry["status"] = "defect"
                entry["defect_type"] = check["defect_type"]
            else:
                entry["status"] = "clean"
                entry["defect_type"] = check["defect_type"]
        elif check["mode"] == "blocked":
            entry["status"] = "blocked"
            entry["missing_source"] = required
        else:
            entry["status"] = "manual"
        for key in ("note", "coverage_note"):
            if check.get(key):
                entry[key] = check[key]
        report.append(entry)
    return report


def _proposal(finding_key: str, action: str, object_type: str, object_id: str | None,
              current: dict, proposed: dict, reason: str, *, financial=0, tax=0,
              sync_token: str | None = None) -> Proposal:
    identifier = digest([finding_key, action, object_type, object_id])[:20]
    return Proposal(f"prp_{identifier}", finding_key, action, object_type, object_id,
                    current, proposed, reason, money(financial), money(tax), sync_token)


def _journal(doc_number: str, memo: str, postings: list[dict]) -> dict:
    lines = []
    for index, post in enumerate(postings, 1):
        debit, credit = money(post.get("debit")), money(post.get("credit"))
        lines.append({
            "Id": str(index), "Amount": float(debit or credit), "Description": memo,
            "JournalEntryLineDetail": {
                "PostingType": "Debit" if debit else "Credit",
                "AccountRef": {"value": post["account"]},
            },
        })
    return {"DocNumber": doc_number, "PrivateNote": memo, "Line": lines, "_Postings": postings}


def analyze(objects: dict[str, list[dict]], evidence: dict, *, today: date | None = None) -> Analysis:
    """Return evidence-backed defects. No golden data is accepted or consulted."""
    today = today or date.today()
    findings: list[Finding] = []
    accounts = {str(account.get("Id")): account
                for account in objects.get("Account", [])}

    # Duplicate purchases: one finding for every repeated source signature.
    seen: dict[tuple, dict] = {}
    for txn in objects.get("Purchase", []):
        # A duplicate finding proposes a reversing entry against a specific
        # object, so a document with no id cannot be the subject of one. This
        # crashed on `txn["Id"]` rather than saying so. The derivation reports
        # the same document as unidentifiable, so it is not lost here.
        if not txn.get("Id"):
            continue
        signature = (txn.get("TxnDate"), txn.get("DocNumber"),
                     (txn.get("EntityRef") or {}).get("value"), money(txn.get("TotalAmt")))
        if signature in seen:
            original = seen[signature]
            postings = [{"account": p["account"], "debit": p.get("credit", "0"),
                         "credit": p.get("debit", "0")} for p in txn.get("_Postings", [])]
            key = _key("duplicate_transaction", str(txn["Id"]))
            proposal = _proposal(
                key, "correcting_entry", "JournalEntry", None, txn,
                _journal(f"SL-DUP-{txn['Id']}", f"Reverse duplicate {txn['Id']}", postings),
                "The provider entry exactly duplicates the dated vendor source; v0 reverses it without deletion.",
                financial=-money(txn.get("TotalAmt")), sync_token=None)
            findings.append(Finding(
                key, "duplicate_transaction", "high", "Duplicate expense",
                f"{txn['Id']} duplicates {original['Id']} by date, vendor, document number, and amount.",
                "Purchase", str(txn["Id"]), "sufficient",
                [f"qbo:{original['Id']}", f"qbo:{txn['Id']}"], -money(txn.get("TotalAmt")),
                proposal=proposal))
        else:
            seen[signature] = txn

    # Check 05. QuickBooks' own holding accounts are not a judgement call. A
    # line posted to Ask My Accountant or Uncategorized Expense is explicitly
    # waiting to be coded. Broader ideas of "wrong account" stay with a person.
    for kind in POSTING_TYPES:
        for txn in objects.get(kind, []) or []:
            uncategorized: list[tuple[str, Decimal]] = []
            for line in txn.get("Line", []) or []:
                account_id = _line_account_ref(line)
                account = accounts.get(account_id) or {}
                if _normalized_account_name(account.get("Name")) in UNCATEGORIZED_ACCOUNT_NAMES:
                    uncategorized.append((str(account.get("Name")), money(line.get("Amount"))))
            if not uncategorized:
                continue
            txn_id = str(txn.get("Id") or "?")
            names = ", ".join(sorted({name for name, _ in uncategorized}))
            amount = sum((abs(value) for _, value in uncategorized), Decimal("0.00"))
            findings.append(Finding(
                _key("uncategorized_expense", f"{kind}:{txn_id}"),
                "uncategorized_expense", "medium", "Expense is still uncategorized",
                f"{kind} {txn.get('DocNumber') or txn_id} posts {amount} to {names}. "
                "Those are QuickBooks holding accounts, so the coding is not finished.",
                kind, txn_id, "sufficient", [f"qbo:{txn_id}"], amount))

    # Statement lines absent from QBO.
    bank_statements = statements(evidence)
    present = {str(txn.get("Id")) for kind in ("Purchase", "Deposit", "Payment")
               for txn in objects.get(kind, [])}
    for missing in (item for stmt in bank_statements
                    for item in stmt.get("missing_transactions", [])):
        if str(missing.get("Id")) in present:
            continue
        key = _key("missing_transaction", str(missing["Id"]))
        proposal = _proposal(
            key, "create_expense", "Purchase", None, {}, missing,
            "The bank statement and source document agree on a transaction absent from QBO.",
            financial=money(missing.get("TotalAmt")))
        findings.append(Finding(
            key, "missing_transaction", "high", "Missing bank transaction",
            f"Statement transaction {missing['DocNumber']} is absent from the provider ledger.",
            "Purchase", str(missing["Id"]), "sufficient", ["bank_statement", f"statement:{missing['Id']}"],
            money(missing.get("TotalAmt")), proposal=proposal))

    for payment in objects.get("Payment", []):
        if money(payment.get("UnappliedAmt")) <= 0:
            continue
        key = _key("unapplied_payment", str(payment["Id"]))
        findings.append(Finding(
            key, "unapplied_payment", "medium", "Unapplied customer payment",
            "Cash is recorded but not linked to an invoice. Applying cash is outside the v0 mutation catalogue.",
            "Payment", str(payment["Id"]), "sufficient", [f"qbo:{payment['Id']}"],
            status="escalated"))

    documents = evidence.get("source_documents") or {}
    purchases = {str(item.get("Id")): item for item in objects.get("Purchase", [])}
    for txn_id, document in documents.items():
        txn = purchases.get(str(txn_id))
        if not txn:
            continue
        line = (txn.get("Line") or [{}])[0]
        detail = line.get("AccountBasedExpenseLineDetail") or {}
        expected_project = document.get("expected_project_id")
        actual_project = (detail.get("CustomerRef") or {}).get("value")
        if expected_project and actual_project != expected_project:
            proposed = {"Line": json.loads(json.dumps(txn.get("Line", [])))}
            proposed["Line"][0].setdefault("AccountBasedExpenseLineDetail", {})["CustomerRef"] = {
                "value": expected_project}
            key = _key("incorrect_job_allocation", txn_id)
            proposal = _proposal(
                key, "assign_project", "Purchase", txn_id, {"Line": txn.get("Line", [])}, proposed,
                "The receipt/job evidence names a different project than QBO.",
                sync_token=str(txn.get("SyncToken", "")))
            findings.append(Finding(
                key, "incorrect_job_allocation", "medium", "Incorrect job allocation",
                f"Source evidence supports project {expected_project}; QBO uses {actual_project or 'none'}.",
                "Purchase", txn_id, "sufficient", [f"document:{document.get('hash')}"], proposal=proposal))
        expected_tax = document.get("expected_tax_code")
        actual_tax = (detail.get("TaxCodeRef") or {}).get("value")
        if expected_tax and actual_tax != expected_tax:
            tax_amount = money(document.get("tax"))
            postings = [{"account": "120", "debit": str(tax_amount), "credit": "0"},
                        {"account": "500", "debit": "0", "credit": str(tax_amount)}]
            key = _key("sales_tax_error", txn_id)
            proposal = _proposal(
                key, "correcting_entry", "JournalEntry", None,
                {"TaxCodeRef": actual_tax},
                _journal(f"SL-TAX-{txn_id}", f"Reclassify recoverable HST for {txn_id}", postings),
                "The source document separately states recoverable tax; v0 posts a scoped reclassification.",
                tax=tax_amount)
            findings.append(Finding(
                key, "sales_tax_error", "high", "GST/HST coding error",
                f"Document supports {expected_tax}; QBO uses {actual_tax or 'none'}.",
                "Purchase", txn_id, "sufficient", [f"document:{document.get('hash')}"],
                tax_effect=tax_amount, proposal=proposal))

    confirmations = evidence.get("receivable_confirmations") or {}
    for invoice in objects.get("Invoice", []):
        invoice_id = str(invoice.get("Id") or "?")
        txn_date = _transaction_date(invoice)
        # An invoice whose date cannot be read cannot be aged, and crashing on
        # it took the client's entire scan with it. It is still money owed, so
        # it becomes a receivable needing evidence rather than disappearing out
        # of the check: silently dropping it would report a client's books as
        # clean because one date was unreadable.
        if txn_date is None:
            if money(invoice.get("Balance")) > 0 and not confirmations.get(invoice_id):
                findings.append(Finding(
                    _key("stale_receivable", invoice_id), "stale_receivable",
                    "medium", "Receivable cannot be aged",
                    "This invoice carries a balance and states no readable "
                    "transaction date, so whether it is overdue cannot be "
                    "determined from the provider data.",
                    "Invoice", invoice_id, "missing", [f"qbo:{invoice_id}"],
                    money(invoice.get("Balance")),
                    evidence_request=(
                        "Confirm the invoice date so the balance can be aged.")))
            continue
        if money(invoice.get("Balance")) > 0 and (today - txn_date).days > 90:
            if confirmations.get(invoice_id):
                continue
            key = _key("stale_receivable", invoice_id)
            findings.append(Finding(
                key, "stale_receivable", "medium", "Stale receivable needs evidence",
                "The balance is over 90 days old, but age alone does not prove payment or bad debt.",
                "Invoice", invoice_id, "missing", [f"qbo:{invoice_id}"],
                money(invoice.get("Balance")), evidence_request=(
                    f"Provide remittance, customer confirmation, or bad-debt approval for {invoice.get('DocNumber')}")))

    # Check 12. A unit price that has moved far outside its own history.
    #
    # Deliberately conservative. A price is only "abnormal" relative to a
    # baseline, so nothing fires until an item has been bought from a vendor
    # PRICE_HISTORY_MINIMUM times. Comparison is against the median rather than
    # the mean, and the threshold is a multiple rather than a standard
    # deviation, because three or four observations do not support a
    # distributional claim -- and pretending otherwise would produce confident
    # findings from noise.
    #
    # Reported, never proposed. A price rise may be a real price rise; only the
    # client knows whether it was agreed.
    prices: dict[tuple[str, str], list[tuple[str, Decimal, str]]] = {}
    for txn in objects.get("Purchase", []) + objects.get("Bill", []):
        vendor = str((txn.get("EntityRef") or txn.get("VendorRef") or {}).get("value") or "")
        for line in txn.get("Line", []) or []:
            detail = line.get("ItemBasedExpenseLineDetail") or {}
            unit = detail.get("UnitPrice")
            item = str((detail.get("ItemRef") or {}).get("value") or "")
            if unit is None or not item or not vendor:
                continue
            prices.setdefault((vendor, item), []).append(
                (str(txn.get("TxnDate") or ""), money(unit), str(txn["Id"])))

    for (vendor, item), observations in sorted(prices.items()):
        if len(observations) <= PRICE_HISTORY_MINIMUM:
            continue
        observations.sort(key=lambda row: row[0])
        *history, latest = observations
        baseline = sorted(value for _, value, _ in history)
        middle = len(baseline) // 2
        median = (baseline[middle] if len(baseline) % 2
                  else (baseline[middle - 1] + baseline[middle]) / 2)
        if median <= 0:
            continue
        latest_date, latest_price, txn_id = latest
        if latest_price <= median * PRICE_SPIKE_MULTIPLE:
            continue
        key = _key("abnormal_price", txn_id)
        findings.append(Finding(
            key, "abnormal_price", "medium", "Unit price moved sharply",
            f"{item} from this vendor has a median unit price of {money(median)} "
            f"across {len(history)} prior purchases; {latest_date} was billed at "
            f"{latest_price}.",
            "Purchase", txn_id, "sufficient", [f"qbo:{txn_id}"],
            (latest_price - money(median))))

    # Check 09. A job whose costs exceed what it billed.
    #
    # Only negative margin, not "weak" margin. Where the line between healthy
    # and thin sits is an accounting judgement that belongs to the client's
    # circumstances; a job that cost more than it earned is arithmetic.
    revenue_by_job: dict[str, Decimal] = {}
    cost_by_job: dict[str, Decimal] = {}
    for invoice in objects.get("Invoice", []):
        for line in invoice.get("Line", []) or []:
            detail = line.get("SalesItemLineDetail") or {}
            job = str((detail.get("CustomerRef") or {}).get("value") or "")
            if job:
                revenue_by_job[job] = revenue_by_job.get(job, Decimal("0")) + money(line.get("Amount"))
    for txn in objects.get("Purchase", []) + objects.get("Bill", []):
        for line in txn.get("Line", []) or []:
            detail = (line.get("AccountBasedExpenseLineDetail")
                      or line.get("ItemBasedExpenseLineDetail") or {})
            job = str((detail.get("CustomerRef") or {}).get("value") or "")
            if job:
                cost_by_job[job] = cost_by_job.get(job, Decimal("0")) + money(line.get("Amount"))

    known_customers = {str(customer.get("Id"))
                       for customer in objects.get("Customer", [])}
    for job in sorted(set(revenue_by_job) & set(cost_by_job)):
        revenue, cost = revenue_by_job[job], cost_by_job[job]
        margin = revenue - cost
        if margin >= 0:
            continue
        key = _key("negative_job_margin", job)
        # A line can name a job that is not in the pull. `SELECT * FROM Customer`
        # returns only *active* records, so a job archived in QuickBooks keeps
        # its costs and disappears from the customer list -- and the finding then
        # points at something an operator cannot open. The arithmetic is still
        # real, so the loss is still reported; what changes is that the evidence
        # is not sufficient, because we cannot even confirm this id is a job.
        if job in known_customers:
            findings.append(Finding(
                key, "negative_job_margin", "high", "Job cost more than it billed",
                f"Billed {revenue} against {cost} of recorded cost, a shortfall of "
                f"{abs(margin)}. Costs allocated to the wrong job produce this too, "
                f"so confirm the allocation before treating it as a loss.",
                "Customer", job, "sufficient", [f"qbo:job:{job}"], abs(margin)))
        else:
            findings.append(Finding(
                key, "negative_job_margin", "high",
                "Job cost more than it billed, and the job is not in the pull",
                f"Transaction lines allocate {cost} of cost and {revenue} of "
                f"revenue to job {job}, a shortfall of {abs(margin)}, but no "
                "customer with that id came back from QuickBooks. An archived "
                "job behaves exactly like this, so the allocation cannot be "
                "confirmed from the provider data alone.",
                "Customer", job, "missing", [f"qbo:job:{job}"], abs(margin),
                evidence_request=(
                    f"Confirm in QuickBooks whether job {job} exists and is "
                    "archived, and whether these costs belong to it.")))

    # Check 16. Whether a client chooses to use QuickBooks jobs is a one-time
    # readiness fact, not a defect that should recur on every scan. The finding
    # is narrower: once jobs exist, using them on only one side of the margin is
    # an inconsistent transaction-level practice worth surfacing.
    jobs = {str(customer.get("Id")) for customer in objects.get("Customer", [])
            if customer.get("Job") is True}
    has_revenue_activity = any(objects.get(kind) for kind in
                               ("Invoice", "CreditMemo", "SalesReceipt", "RefundReceipt"))
    has_cost_activity = any(objects.get(kind) for kind in
                            ("Purchase", "Bill", "VendorCredit"))
    tagged_revenue = set(revenue_by_job) & jobs
    tagged_cost = set(cost_by_job) & jobs
    configuration_problem = ""
    if jobs and has_revenue_activity and has_cost_activity and not tagged_revenue and tagged_cost:
        configuration_problem = (
            "Cost lines name QuickBooks jobs, but no revenue line does, so project "
            "profitability has only the cost side.")
    elif jobs and has_revenue_activity and has_cost_activity and tagged_revenue and not tagged_cost:
        configuration_problem = (
            "Revenue lines name QuickBooks jobs, but no cost line does, so project "
            "profitability has only the revenue side.")
    if configuration_problem:
        findings.append(Finding(
            _key("project_profitability_configuration", "company"),
            "project_profitability_configuration", "medium",
            "Project profitability is not fully configured", configuration_problem,
            "Company", "company", "sufficient", ["qbo:Customer", "qbo:transaction-lines"]))

    # Check 13. A bill past its due date that still carries a balance.
    #
    # Deliberately narrower than the receivable check above. Age alone does not
    # make a payable a problem -- a bill on 30-day terms issued last week is
    # simply owed. What matters is the due date having passed, so a bill with no
    # DueDate falls back to its transaction date plus the default term rather
    # than being flagged for merely existing.
    #
    # No proposal, ever. The ledger fact is "this is unpaid and late"; deciding
    # to pay it moves money, which sits outside the mutation catalogue and
    # belongs to the client.
    for bill in objects.get("Bill", []):
        balance = money(bill.get("Balance"))
        if balance <= 0:
            continue
        raw_due = bill.get("DueDate") or bill.get("TxnDate")
        if not raw_due:
            continue
        try:
            due = date.fromisoformat(str(raw_due)[:10])
        except ValueError:
            continue
        if not bill.get("DueDate"):
            due = due + timedelta(days=DEFAULT_PAYMENT_TERM_DAYS)
        overdue_days = (today - due).days
        if overdue_days <= 0:
            continue
        bill_id = str(bill["Id"])
        key = _key("stale_payable", bill_id)
        findings.append(Finding(
            key, "stale_payable", "medium", "Supplier bill is past due and unpaid",
            f"{bill.get('DocNumber') or bill_id} was due {due.isoformat()}, "
            f"{overdue_days} days ago, and still carries a balance of {balance}.",
            "Bill", bill_id, "sufficient", [f"qbo:{bill_id}"], balance))

    # Every statement-backed account, not just the first. A client inside the
    # supported envelope may hold four; checking one and reporting `clean` would
    # understate the other three.
    #
    # Withheld entirely when the ledger side is not derivable: comparing a real
    # statement against balances we never computed would flag every account.
    derived_ledger, ledger_available, provider_agreement = trusted_ledger(objects, evidence)

    # Check 15. These are exception flags, not proposed corrections. The names
    # are QuickBooks' own holding accounts; a credit balance on an asset is a
    # mechanically observable sign that the account deserves review. A trusted
    # ledger is mandatory because a partial balance would manufacture all three.
    if ledger_available:
        for account_id, account in sorted(accounts.items()):
            balance = derived_ledger.balances.get(account_id, Decimal("0.00"))
            name = str(account.get("Name") or account_id)
            normalized = _normalized_account_name(name)
            reason = ""
            if normalized == "opening balance equity" and balance != 0:
                reason = (f"Opening Balance Equity carries {balance}; this holding account "
                          "normally needs to be cleared to its supported opening balances.")
            elif normalized == "undeposited funds" and balance != 0:
                reason = (f"Undeposited Funds carries {balance}; confirm the receipts clear "
                          "to an actual deposit rather than remaining in transit.")
            elif (account.get("AccountType") in ASSET_ACCOUNT_TYPES
                  and balance < 0 and _provider_agrees_it_is_negative(account)):
                reason = (f"Asset account {name} carries a credit balance of {abs(balance)}, "
                          "and QuickBooks' own balance for it is negative too. That may be "
                          "intentional, but it is unusual enough to review.")
            if not reason:
                continue
            findings.append(Finding(
                _key("suspicious_balance_sheet", account_id),
                "suspicious_balance_sheet", "medium",
                "Balance-sheet account needs review", reason,
                "Account", account_id, "sufficient", [f"qbo:Account:{account_id}"],
                abs(balance)))
    balances = derived_ledger.balances if bank_statements and ledger_available else {}
    for statement in (bank_statements if ledger_available else []):
        if statement.get("ending_ledger_balance") is None:
            continue
        account_id = str(statement.get("account_id"))
        actual = balances.get(account_id, Decimal("0"))
        expected = money(statement["ending_ledger_balance"])
        if actual == expected:
            continue
        key = _key("unreconciled_account", account_id)
        findings.append(Finding(
            key, "unreconciled_account", "high", "Control account does not reconcile",
            f"Statement-backed balance is {expected}; reconstructed ledger is {actual}.",
            "Account", account_id, "sufficient", ["bank_statement"],
            abs(expected - actual)))

    # Check E2, done properly: every statement line put against the
    # reconstructed cash movements, and the ones nothing accounts for reported.
    matching_summaries: dict[str, dict] = {}
    if ledger_available:
        extra, matching_summaries = _statement_line_findings(
            objects, bank_statements, derived_ledger)
        findings.extend(extra)

    proposals = [item.proposal for item in findings if item.proposal]
    source_gaps = []
    if not bank_statements:
        source_gaps.append("Bank statement covering the review period")
    if bank_statements and not ledger_available:
        if provider_agreement.get("status") == "disagrees":
            source_gaps.append(
                "A ledger reconstruction that QuickBooks agrees with. Our "
                "postings and QuickBooks' own trial balance read the same books "
                "differently, so neither is trusted until that is resolved")
        elif provider_agreement.get("status") == "unreadable":
            source_gaps.append(
                "A readable QuickBooks trial balance: " + provider_agreement["reason"])
        elif provider_agreement.get("status") == "no_manifest":
            source_gaps.append(
                "A pull that records which entity types it read. " +
                str(provider_agreement.get("reason", "")))
        else:
            source_gaps.append(
                "Ledger postings derived from the provider objects, without which a "
                "statement balance has nothing to reconcile against")
    if not evidence.get("source_documents") and objects.get("Purchase"):
        source_gaps.append("Receipts or supplier invoices for expense verification")
    checks = check_coverage(findings, evidence, ledger_available=ledger_available)
    coverage = {
        "period_start": evidence.get("period_start"), "period_end": evidence.get("period_end"),
        "account_ids": sorted(str(item.get("Id")) for item in objects.get("Account", [])),
        "objects_loaded": {kind: len(rows) for kind, rows in sorted(objects.items())},
        "missing_evidence": source_gaps + [item.evidence_request for item in findings if item.evidence_request],
        "complete_for_action": all(item.evidence_status == "sufficient" for item in findings if item.proposal),
        # Per-check status for every advertised check. `checks_automated` is the
        # number the engine actually computed this run — the honest headline
        # number, and deliberately not the same as len(CHECKS).
        "checks": checks,
        "checks_total": len(checks),
        "checks_automated": sum(1 for item in checks if item["status"] in {"clean", "defect"}),
        "checks_blocked": sum(1 for item in checks if item["status"] == "blocked"),
        "checks_manual": sum(1 for item in checks if item["status"] == "manual"),
        # What the matcher did, per account. A reviewer needs the ambiguous
        # count as much as the missing one: an ambiguity is not a clean line,
        # it is a line nobody has decided yet.
        "statement_matching": matching_summaries,
        # Three engines, one ingest: our reconstruction, Beancount's
        # recomputation of it, and QuickBooks' own answer for the same ledger.
        # This is the third, and the one an accountant can check unaided.
        "provider_agreement": provider_agreement,
    }
    return Analysis(str(evidence.get("period_start") or ""), str(evidence.get("period_end") or ""),
                    objects, findings, coverage, proposals)


TRANSITIONS = {
    "detected": {"proposed"}, "proposed": {"reviewed", "rejected", "escalated"},
    "reviewed": {"approved", "rejected", "escalated", "proposed"},
    "approved": {"executed", "escalated"}, "executed": {"verified"},
    "verified": {"reconciled"}, "reconciled": set(), "rejected": set(), "escalated": set(),
}


def transition(proposal: Proposal, target: str) -> None:
    if target not in TRANSITIONS.get(proposal.status, set()):
        raise ValueError(f"Invalid proposal transition {proposal.status} -> {target}")
    proposal.status = target


def edit(proposal: Proposal, proposed: dict) -> None:
    if proposal.status not in {"proposed", "reviewed"}:
        raise ValueError("Only a pending review can be edited")
    proposal.proposed = proposed
    proposal.version += 1
    proposal.status = "proposed"


def _matches(readback: dict, expected: dict) -> bool:
    for key, value in expected.items():
        if key.startswith("_"):
            continue
        if readback.get(key) != value:
            return False
    return True


def execute(proposal: Proposal, adapter, *, max_attempts: int = 4) -> MutationResult:
    if proposal.status != "approved":
        raise ValueError("Only an approved proposal can execute")
    key = f"shimline-{proposal.proposal_id}-v{proposal.version}"
    sync_token = proposal.expected_sync_token
    last_error = None
    for _ in range(max_attempts):
        try:
            result = adapter.mutate(
                proposal.action, object_type=proposal.object_type, payload=proposal.proposed,
                idempotency_key=key, object_id=proposal.object_id,
                expected_sync_token=sync_token)
            transition(proposal, "executed")
            readback = adapter.read(result.object_type, result.object_id)
            if not _matches(readback, proposal.proposed):
                raise RuntimeError("QBO read-back differs from the approved proposal")
            proposal.readback = readback
            transition(proposal, "verified")
            return result
        except StaleObjectError:
            if not proposal.object_id:
                raise
            latest = adapter.read(proposal.object_type, proposal.object_id)
            if not _matches(latest, proposal.current):
                raise RuntimeError("QBO changed a reviewed field; proposal requires fresh review")
            sync_token = str(latest.get("SyncToken", ""))
            last_error = "stale token"
        except UncertainWriteError as exc:
            # Safe to replay: production QBO and the synthetic double both use
            # the same stable request id.
            last_error = str(exc)
    raise RuntimeError(f"Proposal did not settle after {max_attempts} attempts: {last_error}")


def trusted_ledger(objects: dict[str, list[dict]], evidence: dict
                   ) -> tuple[DerivedLedger, bool, dict]:
    """Reconstruct the ledger and say whether it may be relied on.

    Two conditions, and both must hold. The reconstruction must be complete --
    every document turned into postings that balance. And where QuickBooks' own
    TrialBalance report has been supplied, its numbers must equal ours exactly,
    account by account.

    A disagreement with the provider is deliberately *not* a finding about the
    client. It is a statement about us: our reconstruction and QuickBooks read
    the same ledger two ways and got two answers, so at least one is wrong, and
    telling a client their books do not reconcile on that basis would be an
    accusation we cannot support. The ledger is withdrawn instead, and every
    check that depends on it reports `blocked` with the reason.

    Supplying the report is optional. Without it the reconstruction still has to
    be complete and still has Beancount behind it; the provider check is the
    third engine, not the only one.
    """
    derived = derive_ledger(objects)
    available = derived.complete
    agreement: dict = {"status": "not_supplied"}

    # A pull has to say what it read before anything may be computed from it.
    # Without that, an entity that failed to load is indistinguishable from an
    # entity with no rows: a pull that lost every invoice would produce a ledger
    # reporting itself complete, missing revenue and receivables, and the trial
    # balance would still sum to zero because both halves of every invoice went
    # missing together. This is the one place the requirement lives, because
    # this is the one place a ledger is handed to a check.
    if not isinstance(objects.get(PULL_MANIFEST_KEY), dict):
        return derived, False, {
            "status": "no_manifest",
            "reason": ("These objects did not come from a pull that recorded "
                       "which entity types it read, so an entity that failed to "
                       "load cannot be told apart from one with no rows.")}

    report = evidence.get("provider_trial_balance")
    if report and not available:
        agreement = {"status": "not_compared",
                     "reason": "the reconstruction is incomplete, so there is "
                               "nothing to compare"}
    elif report:
        try:
            comparison = compare_to_provider(derived.balances, provider_balances(report))
        except ProviderReportError as exc:
            agreement = {"status": "unreadable", "reason": str(exc)}
            available = False
        else:
            agreement = {"status": "agrees" if comparison.agrees else "disagrees",
                         "accounts_compared": comparison.accounts_compared,
                         "differences": comparison.differences[:10]}
            if not comparison.agrees:
                available = False
    return derived, available, agreement


def _statement_line_findings(objects: dict[str, list[dict]],
                             bank_statements: list[dict],
                             derived_ledger: DerivedLedger
                             ) -> tuple[list[Finding], dict[str, dict]]:
    """Check E2: statement lines the reconstructed ledger cannot account for.

    Reported, never proposed. A bank line says money moved and names a
    descriptor; it does not say which expense account the charge belongs to,
    nor whether the vendor is one the client already has. Turning that into a
    create_expense proposal would be inventing the coding, and the approval gate
    exists precisely so that nothing invented reaches a client's books. The
    pre-supplied `missing_transactions` path above still proposes, because there
    a source document corroborates the line.

    A ledger movement with no statement line is *not* a finding. An outstanding
    cheque is the ordinary state of a month-end and flagging it would fire on
    almost every client, which is worse than not checking at all. The count is
    carried in the summary so a reviewer can see it.
    """
    findings: list[Finding] = []
    summaries: dict[str, dict] = {}
    for statement in bank_statements:
        rows = statement.get("lines")
        account_id = str(statement.get("account_id") or "")
        if not rows or not account_id:
            continue
        try:
            movements = matching.cash_movements(derived_ledger, objects, account_id)
        except matching.UndatedMovement as exc:
            # Undated movements make the ledger side of the comparison
            # incomplete, so every unmatched line would be a false accusation.
            summaries[account_id] = {"status": "blocked", "reason": str(exc)}
            continue

        report = matching.match(matching.bank_lines(rows), movements)
        summaries[account_id] = dict(report.summary(), status="matched")

        statement_id = str(statement.get("statement_id") or account_id)
        for line in report.unmatched_bank:
            key = _key("missing_transaction", f"{statement_id}:{line.ordinal}")
            descriptor = line.party or "(no descriptor)"
            findings.append(Finding(
                key, "missing_transaction", "high", "Bank transaction missing from the ledger",
                f"The statement shows {money(line.amount)} on "
                f"{line.posted_date.isoformat()} ({descriptor}), and no entry in "
                "the books accounts for it. No correction is proposed: the "
                "statement does not say which account the amount belongs to.",
                "BankStatementLine", f"{statement_id}:{line.ordinal}", "insufficient",
                ["bank_statement", f"statement_line:{statement_id}:{line.ordinal}"],
                money(line.amount),
                evidence_request=("The receipt or invoice for this charge, so it "
                                  "can be coded to the right account.")))
    return findings, summaries


def reconcile(objects: dict[str, list[dict]], evidence: dict) -> list[dict]:
    """Reconcile every control account we hold a statement for.

    Returns one record per account. An account with no statement produces a
    `no_source` record rather than nothing, so that a missing statement is
    visible in the working papers instead of looking like a clean result.
    """
    derived_ledger, ledger_available, provider_agreement = trusted_ledger(objects, evidence)
    balances = derived_ledger.balances
    period_end = str(evidence.get("period_end") or "")

    records = []
    for statement in (statements(evidence) if ledger_available else []):
        if not statement or statement.get("ending_ledger_balance") is None:
            continue
        account_id = str(statement.get("account_id"))
        expected = money(statement["ending_ledger_balance"])
        actual = balances.get(account_id, Decimal("0"))
        record = {
            "account_id": account_id,
            "period_end": str(statement.get("period_end") or period_end),
            "statement_balance": str(expected),
            "ledger_balance": str(actual),
            "difference": str(actual - expected),
            "status": "reconciled" if actual == expected else "exception",
            "evidence": ["bank_statement"],
        }
        # A matching balance is not the same as a matching set of transactions:
        # two errors that cancel tie the closing balance exactly. The line-level
        # counts are what tell a reviewer whether the tie is real.
        _, summaries = _statement_line_findings(objects, [statement], derived_ledger)
        if summaries.get(account_id):
            record["line_matching"] = summaries[account_id]
        records.append(record)
    if not records:
        # Name the source that is actually absent. A statement was supplied but
        # unusable for a different reason than no statement at all, and a
        # reviewer chasing the client for a document they already sent is a
        # worse outcome than saying which half is missing.
        missing = "Bank statement covering the review period"
        if statements(evidence) and not ledger_available:
            if provider_agreement.get("status") == "disagrees":
                missing = ("A ledger reconstruction QuickBooks agrees with; the "
                           "statement is held, but our postings and QuickBooks' "
                           "own trial balance disagree, so neither is trusted")
            else:
                missing = ("Ledger postings derived from the provider objects; the "
                           "statement is held but has nothing to reconcile against")
        records.append({
            "account_id": "*", "period_end": period_end,
            "statement_balance": "", "ledger_balance": "",
            "difference": "", "status": "no_source",
            "evidence": [],
            "missing_source": missing,
        })
    return records


def run_synthetic(adapter, *, approve: bool = True) -> WorkResult:
    objects = adapter.pull_all()
    analysis = analyze(objects, adapter.evidence, today=date.fromisoformat(adapter.evidence["period_end"]))
    for proposal in analysis.proposals:
        transition(proposal, "reviewed")
        if approve:
            transition(proposal, "approved")
            execute(proposal, adapter)
    reconciliation = reconcile(adapter.pull_all(), adapter.evidence)[0]
    if reconciliation["status"] == "reconciled":
        for proposal in analysis.proposals:
            if proposal.status == "verified":
                transition(proposal, "reconciled")
    working_papers = make_working_papers(analysis, reconciliation)
    return WorkResult(analysis, reconciliation, working_papers, len(adapter.applied_request_ids))


def make_working_papers(analysis: Analysis, reconciliation: dict) -> dict:
    payload = {
        "engine": "Bookkeeping Work Engine v0",
        "period": {"start": analysis.period_start, "end": analysis.period_end},
        "coverage": analysis.coverage,
        "defect_register": [{k: v for k, v in asdict(item).items() if k != "proposal"}
                            for item in analysis.findings],
        "proposals": [asdict(item) for item in analysis.proposals],
        "reconciliations": [reconciliation],
        "residual_exceptions": [item.key for item in analysis.findings
                                if item.evidence_status != "sufficient" or item.status == "escalated"],
    }
    payload["package_hash"] = digest(payload)
    return payload


# ---------------------------------------------------------- persistence layer

def persist_analysis(conn, *, organization_id: str, engagement_id: str | None,
                     connection_id: str | None, analysis: Analysis,
                     evidence: dict | None = None,
                     provider: str = "quickbooks") -> str:
    """Persist the reviewable control record; raw provider JSON is not stored here."""
    run_id = crm.new_id("bkr")
    evidence = evidence if evidence is not None else {}
    conn.execute(
        "INSERT INTO bookkeeping_runs(id,organization_id,engagement_id,connection_id,period_start,period_end,"
        "status,evidence_json,coverage_json) VALUES(?,?,?,?,?,?,'review',?,?)",
        (run_id, organization_id, engagement_id, connection_id,
         analysis.period_start, analysis.period_end,
         canonical_json(evidence), canonical_json(analysis.coverage)))
    persist_canonical(conn, run_id, organization_id, analysis.objects, provider=provider)
    # Reconcile at analysis time so the run always carries a reconciliation
    # record — including the `no_source` case, which is the common one until a
    # bank statement has been supplied.
    persist_reconciliations(conn, run_id, reconcile(analysis.objects, evidence))
    for finding in analysis.findings:
        finding_id = crm.new_id("fnd")
        conn.execute(
            "INSERT INTO bookkeeping_findings(id,run_id,organization_id,defect_type,severity,title,reason,"
            "affected_type,affected_provider_id,evidence_status,evidence_json,financial_effect,tax_effect,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (finding_id, run_id, organization_id, finding.defect_type, finding.severity,
             finding.title, finding.reason, finding.affected_type, finding.affected_id,
             finding.evidence_status, canonical_json(finding.evidence), str(finding.financial_effect),
             str(finding.tax_effect), finding.status))
        if finding.evidence_request:
            conn.execute(
                "INSERT INTO bookkeeping_evidence_requests(id,finding_id,requested_item,reason) VALUES(?,?,?,?)",
                (crm.new_id("evr"), finding_id, finding.evidence_request,
                 "Correction is uncertain without corroborating evidence"))
        proposal = finding.proposal
        if proposal:
            conn.execute(
                "INSERT INTO bookkeeping_proposals(id,finding_id,run_id,action_type,target_type,target_provider_id,"
                "current_json,proposed_json,reason,financial_effect,tax_effect,expected_sync_token,status,version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal.proposal_id, finding_id, run_id, proposal.action, proposal.object_type,
                 proposal.object_id, canonical_json(proposal.current), canonical_json(proposal.proposed),
                 proposal.reason, str(proposal.financial_effect), str(proposal.tax_effect),
                 proposal.expected_sync_token, proposal.status, proposal.version))
            conn.execute("UPDATE bookkeeping_findings SET status='proposed' WHERE id=?", (finding_id,))
    conn.execute(
        "INSERT INTO audit_events(id,action,entity_type,entity_id,summary) "
        "VALUES(?,'bookkeeping.analysis','bookkeeping_run',?,?)",
        (crm.new_id("aud"), run_id, f"{len(analysis.findings)} findings; {len(analysis.proposals)} proposals"))
    conn.commit()
    return run_id


def persist_canonical(conn, run_id: str, organization_id: str,
                      objects: dict[str, list[dict]], provider: str = "quickbooks") -> None:
    """Shred provider objects into the neutral bookkeeping model."""
    account_ids: dict[str, str] = {}
    for item in objects.get("Account", []):
        provider_id = str(item["Id"])
        existing = conn.execute(
            "SELECT id FROM bookkeeping_accounts WHERE organization_id=? AND provider=? AND provider_id=?",
            (organization_id, provider, provider_id)).fetchone()
        row_id = existing[0] if existing else crm.new_id("acc")
        conn.execute(
            "INSERT INTO bookkeeping_accounts(id,organization_id,provider,provider_id,name,account_type,"
            "account_subtype,currency,active,sync_token) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(organization_id,provider,provider_id) DO UPDATE SET name=excluded.name,"
            "account_type=excluded.account_type,account_subtype=excluded.account_subtype,active=excluded.active,"
            "sync_token=excluded.sync_token",
            (row_id, organization_id, provider, provider_id, item.get("Name") or provider_id,
             item.get("AccountType") or "Unknown", item.get("AccountSubType"),
             (item.get("CurrencyRef") or {}).get("value", "CAD"), int(item.get("Active", True)),
             str(item.get("SyncToken", ""))))
        account_ids[provider_id] = row_id

    entity_ids: dict[tuple[str, str], str] = {}
    for object_type, entity_type in (("Customer", "customer"), ("Vendor", "vendor")):
        for item in objects.get(object_type, []):
            provider_id = str(item["Id"])
            existing = conn.execute(
                "SELECT id FROM bookkeeping_entities WHERE organization_id=? AND provider=? "
                "AND entity_type=? AND provider_id=?",
                (organization_id, provider, entity_type, provider_id)).fetchone()
            row_id = existing[0] if existing else crm.new_id("ent")
            email = (item.get("PrimaryEmailAddr") or {}).get("Address")
            conn.execute(
                "INSERT INTO bookkeeping_entities(id,organization_id,provider,provider_id,entity_type,"
                "display_name,email,active,sync_token) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(organization_id,provider,entity_type,provider_id) DO UPDATE SET "
                "display_name=excluded.display_name,email=excluded.email,active=excluded.active,sync_token=excluded.sync_token",
                (row_id, organization_id, provider, provider_id, entity_type,
                 item.get("DisplayName") or item.get("CompanyName") or provider_id, email,
                 int(item.get("Active", True)), str(item.get("SyncToken", ""))))
            entity_ids[(entity_type, provider_id)] = row_id

    project_ids: dict[str, str] = {}
    for item in objects.get("Customer", []):
        if not item.get("Job"):
            continue
        provider_id = str(item["Id"])
        existing = conn.execute(
            "SELECT id FROM bookkeeping_projects WHERE organization_id=? AND provider=? AND provider_id=?",
            (organization_id, provider, provider_id)).fetchone()
        row_id = existing[0] if existing else crm.new_id("prj")
        parent = str((item.get("ParentRef") or {}).get("value", ""))
        conn.execute(
            "INSERT INTO bookkeeping_projects(id,organization_id,provider,provider_id,customer_id,name,active,sync_token) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(organization_id,provider,provider_id) DO UPDATE SET "
            "customer_id=excluded.customer_id,name=excluded.name,active=excluded.active,sync_token=excluded.sync_token",
            (row_id, organization_id, provider, provider_id, entity_ids.get(("customer", parent)),
             item.get("DisplayName") or provider_id, int(item.get("Active", True)),
             str(item.get("SyncToken", ""))))
        project_ids[provider_id] = row_id

    class_ids: dict[tuple[str, str], str] = {}
    for object_type, kind in (("Class", "class"), ("TaxCode", "tax_code")):
        for item in objects.get(object_type, []):
            provider_id = str(item["Id"])
            existing = conn.execute(
                "SELECT id FROM bookkeeping_classifications WHERE organization_id=? AND provider=? "
                "AND kind=? AND provider_id=?", (organization_id, provider, kind, provider_id)).fetchone()
            row_id = existing[0] if existing else crm.new_id("cls")
            conn.execute(
                "INSERT INTO bookkeeping_classifications(id,organization_id,provider,provider_id,kind,name,code,active,sync_token) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(organization_id,provider,kind,provider_id) DO UPDATE SET "
                "name=excluded.name,code=excluded.code,active=excluded.active,sync_token=excluded.sync_token",
                (row_id, organization_id, provider, provider_id, kind,
                 item.get("Name") or provider_id, item.get("Name"), int(item.get("Active", True)),
                 str(item.get("SyncToken", ""))))
            class_ids[(kind, provider_id)] = row_id

    # Provider ids are unique per object type, not across them, so an
    # attachment's reference is only resolvable with the type it names.
    transaction_ids: dict[tuple[str, str], str] = {}
    for object_type in ("Estimate", "Invoice", "Payment", "Bill", "Purchase", "Deposit", "JournalEntry"):
        for item in objects.get(object_type, []):
            provider_id = str(item["Id"])
            customer_ref = str((item.get("CustomerRef") or {}).get("value", ""))
            vendor_ref = str((item.get("VendorRef") or item.get("EntityRef") or {}).get("value", ""))
            entity_id = entity_ids.get(("customer", customer_ref)) or entity_ids.get(("vendor", vendor_ref))
            # A payment's unapplied cash is its open balance. QBO states it as
            # UnappliedAmt rather than Balance, and dropping it here is what
            # made unapplied cash invisible to anything reading these tables.
            if object_type == "Payment":
                open_balance = str(money(item.get("UnappliedAmt"))) if "UnappliedAmt" in item else None
            elif "Balance" in item:
                open_balance = str(money(item.get("Balance")))
            else:
                open_balance = None
            # An estimate is a quote, not a posting. It is kept out of the
            # posted set so no balance or reconciliation can pick it up.
            status = "estimate" if object_type == "Estimate" else "posted"
            txn_id = crm.new_id("txn")
            conn.execute(
                "INSERT INTO bookkeeping_transactions(id,run_id,organization_id,provider,provider_type,provider_id,"
                "transaction_date,document_number,currency,total_amount,open_balance,entity_id,status,sync_token,source_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (txn_id, run_id, organization_id, provider, object_type, provider_id,
                 item.get("TxnDate") or analysis_date(item), item.get("DocNumber"),
                 (item.get("CurrencyRef") or {}).get("value", "CAD"), str(money(item.get("TotalAmt"))),
                 open_balance, entity_id, status,
                 str(item.get("SyncToken", "")), digest(item)))
            transaction_ids[(object_type, provider_id)] = txn_id
            _persist_transaction_tax(conn, txn_id, item)
            for index, line in enumerate(item.get("Line") or []):
                detail = (line.get("AccountBasedExpenseLineDetail") or
                          line.get("ItemBasedExpenseLineDetail") or
                          line.get("JournalEntryLineDetail") or line.get("SalesItemLineDetail") or {})
                account_ref = str((detail.get("AccountRef") or {}).get("value", ""))
                project_ref = str((detail.get("CustomerRef") or {}).get("value", ""))
                class_ref = str((detail.get("ClassRef") or {}).get("value", ""))
                tax_ref = str((detail.get("TaxCodeRef") or {}).get("value", ""))
                amount = money(line.get("Amount"))
                posting = detail.get("PostingType")
                debit = amount if posting == "Debit" else Decimal("0")
                credit = amount if posting == "Credit" else Decimal("0")
                # Absent unit price stays absent. A defaulted 0.00 would read
                # as "billed at nothing" to any price-movement check.
                unit_price = str(money(detail["UnitPrice"])) if "UnitPrice" in detail else None
                quantity = str(money(detail["Qty"])) if "Qty" in detail else None
                # Tax per line is only recorded when QuickBooks states it. It
                # usually does not: tax is published per *rate* on the document,
                # not per line, and apportioning it would invent the number a
                # GST return is filed on. `tax_amount_source` is what lets a
                # reader tell a genuine zero-rated line from one nobody filled.
                if "TaxAmount" in detail:
                    line_tax, tax_source = str(money(detail["TaxAmount"])), "provider"
                else:
                    line_tax, tax_source = "0.00", "unknown"
                line_id = crm.new_id("lin")
                conn.execute(
                    "INSERT INTO bookkeeping_transaction_lines(id,transaction_id,provider_line_id,description,account_id,"
                    "entity_id,project_id,class_id,tax_code_id,amount,debit,credit,tax_amount,tax_amount_source,"
                    "unit_price,quantity) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (line_id, txn_id, str(line.get("Id", index)), line.get("Description"),
                     account_ids.get(account_ref), entity_id, project_ids.get(project_ref),
                     class_ids.get(("class", class_ref)), class_ids.get(("tax_code", tax_ref)),
                     str(amount), str(debit), str(credit), line_tax, tax_source,
                     unit_price, quantity))
                if project_ref or class_ref:
                    conn.execute(
                        "INSERT INTO bookkeeping_allocations(id,line_id,project_id,class_id,amount,source,confidence) "
                        "VALUES(?,?,?,?,?,'provider','1.0')",
                        (crm.new_id("all"), line_id, project_ids.get(project_ref),
                         class_ids.get(("class", class_ref)), str(amount)))

    for attachment in objects.get("Attachable", []):
        # An attachment is evidence *for* something. Storing it without the
        # transaction it supports leaves bookkeeping_documents.transaction_id
        # permanently NULL, so nothing can ask "what proves this charge?" --
        # which is the question the whole evidence model exists to answer. An
        # attachment QBO does not link, or links to an object outside this
        # pull, is still stored; it just carries no transaction.
        linked = (attachment.get("AttachableRef") or [{}])[0].get("EntityRef") or {}
        transaction_id = transaction_ids.get(
            (str(linked.get("type", "")), str(linked.get("value", ""))))
        provider_id = str(attachment.get("Id", ""))
        conn.execute(
            "INSERT OR IGNORE INTO bookkeeping_documents(id,organization_id,run_id,provider,provider_id,"
            "transaction_id,document_type,filename,content_hash,period_start,period_end,evidence_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (crm.new_id("doc"), organization_id, run_id, provider, provider_id, transaction_id,
             attachment.get("ContentType") or "attachment", attachment.get("FileName"),
             digest(attachment), None, None, "present"))


def _persist_transaction_tax(conn, txn_id: str, item: dict) -> None:
    """Store sales tax at the grain QuickBooks publishes it: per rate.

    `TotalTax` goes on the transaction. Each `TaxLine` -- one per rate applied,
    with the net amount that rate was charged on -- becomes its own row. That is
    also the grain a GST/HST return is prepared at: tax collected and tax paid
    per rate over a period, each with its taxable base.

    Nothing is apportioned down to lines. QuickBooks does not state tax per
    line, and a number invented here would end up on a return filed with the CRA
    under the client's name.
    """
    detail = item.get("TxnTaxDetail") or {}
    if not detail:
        return
    if "TotalTax" in detail:
        conn.execute("UPDATE bookkeeping_transactions SET tax_total=? WHERE id=?",
                     (str(money(detail.get("TotalTax"))), txn_id))
    for tax_line in detail.get("TaxLine", []) or []:
        line_detail = tax_line.get("TaxLineDetail") or {}
        rate_ref = str((line_detail.get("TaxRateRef") or {}).get("value") or "")
        percent = line_detail.get("TaxPercent")
        taxable = line_detail.get("NetAmountTaxable")
        conn.execute(
            "INSERT INTO bookkeeping_transaction_taxes("
            "id,transaction_id,tax_rate_ref,rate_percent,net_amount_taxable,tax_amount) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(transaction_id,tax_rate_ref) DO UPDATE SET "
            "rate_percent=excluded.rate_percent,"
            "net_amount_taxable=excluded.net_amount_taxable,"
            "tax_amount=excluded.tax_amount",
            (crm.new_id("txt"), txn_id, rate_ref,
             None if percent is None else str(Decimal(str(percent))),
             None if taxable is None else str(money(taxable)),
             str(money(tax_line.get("Amount")))))


def persist_reconciliations(conn, run_id: str, records: Iterable[dict]) -> None:
    """Write one reconciliation row per control account, replacing prior ones.

    Called after analysis and again after any execution, because executing a
    proposal changes the ledger and therefore changes the answer.
    """
    for record in records:
        conn.execute(
            "INSERT INTO bookkeeping_reconciliations(id,run_id,account_ref,period_end,source_balance,"
            "ledger_balance,difference,status,missing_source,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,account_ref,period_end) DO UPDATE SET "
            "source_balance=excluded.source_balance,ledger_balance=excluded.ledger_balance,"
            "difference=excluded.difference,status=excluded.status,"
            "missing_source=excluded.missing_source,evidence_json=excluded.evidence_json,"
            "verified_at=CURRENT_TIMESTAMP",
            (crm.new_id("rec"), run_id, record["account_id"], record["period_end"],
             record["statement_balance"], record["ledger_balance"], record["difference"],
             record["status"], record.get("missing_source"),
             canonical_json(record.get("evidence", []))))


def refresh_reconciliation(conn, run_id: str, adapter, evidence: dict | None = None) -> list[dict]:
    """Re-pull the provider and re-reconcile. Use after executing a proposal."""
    run = conn.execute(
        "SELECT period_start,period_end,evidence_json FROM bookkeeping_runs WHERE id=?",
        (run_id,)).fetchone()
    if not run:
        raise ValueError("Bookkeeping run not found")
    if evidence is None:
        stored = run["evidence_json"] if "evidence_json" in run.keys() else None
        evidence = json.loads(stored) if stored else {}
        evidence.setdefault("period_start", run["period_start"])
        evidence.setdefault("period_end", run["period_end"])
    records = reconcile(adapter.pull_all(), evidence)
    persist_reconciliations(conn, run_id, records)
    conn.commit()
    return records


def analysis_date(item: dict) -> str:
    stamp = ((item.get("MetaData") or {}).get("CreateTime") or datetime.utcnow().isoformat())
    return str(stamp)[:10]


def record_decision(conn, proposal_id: str, decision: str, actor_user_id: str,
                    note: str = "", proposed: dict | None = None) -> None:
    if decision not in {"approve", "edit", "reject", "escalate"}:
        raise ValueError("Unknown review decision")
    row = conn.execute("SELECT status,version FROM bookkeeping_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError("Proposal not found")
    status, version = row
    if status == "proposed":
        status = "reviewed"
    if decision == "approve":
        # Preparer is not reviewer. A machine-generated proposal has no human
        # preparer, so this only bites when a person edited the payload: the
        # person who wrote the change cannot be its only approver.
        editor = conn.execute(
            "SELECT actor_user_id FROM bookkeeping_approvals WHERE proposal_id=? AND decision='edit' "
            "AND proposal_version=? ORDER BY decided_at DESC LIMIT 1",
            (proposal_id, version)).fetchone()
        if editor and editor[0] == actor_user_id:
            raise PermissionError(
                "Separation of duties: you edited this proposal, so another reviewer must approve it.")
    target = {"approve": "approved", "edit": "proposed", "reject": "rejected",
              "escalate": "escalated"}[decision]
    if decision == "edit":
        if proposed is None:
            raise ValueError("An edit requires a replacement proposal")
        version += 1
        conn.execute("UPDATE bookkeeping_proposals SET proposed_json=?,version=?,status='proposed',"
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (canonical_json(proposed), version, proposal_id))
    else:
        if target not in TRANSITIONS.get(status, set()):
            raise ValueError(f"Invalid proposal transition {status} -> {target}")
        conn.execute("UPDATE bookkeeping_proposals SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (target, proposal_id))
    conn.execute(
        "INSERT INTO bookkeeping_approvals(id,proposal_id,proposal_version,decision,actor_user_id,note) "
        "VALUES(?,?,?,?,?,?)",
        (crm.new_id("apr"), proposal_id, version, decision, actor_user_id, note[:2000] or None))
    conn.execute(
        "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) VALUES(?,?,?,?,?,?)",
        (crm.new_id("aud"), actor_user_id, f"bookkeeping.proposal.{decision}", "proposal",
         proposal_id, note[:500] or decision))
    conn.commit()


def proposal_rows(conn, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*,f.title,f.severity,f.evidence_status,f.evidence_json FROM bookkeeping_proposals p "
        "JOIN bookkeeping_findings f ON f.id=p.finding_id WHERE p.run_id=? ORDER BY f.severity DESC,p.created_at",
        (run_id,)).fetchall()
    from . import diffing, provenance

    output = []
    for row in rows:
        item = dict(row)
        for key in ("current_json", "proposed_json", "evidence_json"):
            item[key[:-5] if key.endswith("_json") else key] = json.loads(item.pop(key))
        # What this proposal actually changes, in fields rather than in JSON. An
        # approval is only meaningful if the approver saw the change, and a
        # reviewer reading twenty JSON blobs reads none of them.
        item["changes"] = diffing.changes(item["current"], item["proposed"])
        item["change_summary"] = diffing.summary(item["current"], item["proposed"])
        # Carried into the batch form and checked on the way back, so a proposal
        # that moved between the page rendering and the button being pressed
        # cannot be approved by someone who never saw its current state.
        item["fingerprint"] = diffing.fingerprint(item["current"], item["proposed"])
        item["evidence_lines"] = provenance.evidence_lines(item.get("evidence"))
        output.append(item)
    return output


class BatchChanged(RuntimeError):
    """A proposal moved between the page being read and the button being pressed."""


def record_batch_decision(conn, decisions: list[tuple[str, str]], actor_user_id: str,
                          *, note: str = "", run_id: str | None = None) -> list[str]:
    """Decide several proposals in one pass, all or nothing.

    `decisions` is (proposal_id, fingerprint) pairs, where the fingerprint is
    `diffing.fingerprint` of exactly what the reviewer was shown. Every one is
    re-checked against the proposal as it stands now, and a single mismatch
    refuses the whole batch.

    That is the point of the batch existing at all. Approving twenty proposals
    with one button is only safe if the twenty are the twenty that were on the
    screen; otherwise the volume that makes an accountant fast is the same
    volume that lets a change nobody read reach a client's books. Partial
    application would be worse still -- the reviewer would have to work out
    which half landed.

    Rejections are checked the same way. A reviewer rejecting a proposal they
    have not seen is a smaller harm than approving one, but it is still a
    decision recorded against their name.
    """
    from . import diffing

    if not decisions:
        return []

    staged = []
    for proposal_id, expected in decisions:
        row = conn.execute(
            "SELECT run_id, current_json, proposed_json, status "
            "FROM bookkeeping_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            raise BatchChanged(
                f"Proposal {proposal_id} no longer exists. Nothing was decided; "
                "reload the review and look again.")
        if run_id and str(row[0]) != str(run_id):
            raise BatchChanged(
                f"Proposal {proposal_id} does not belong to this review.")
        actual = diffing.fingerprint(row[1], row[2])
        if expected and actual != expected:
            raise BatchChanged(
                f"Proposal {proposal_id} changed since this page was loaded, so "
                "nothing was decided. Reload the review and read it again.")
        staged.append(proposal_id)
    return staged


def approver_of(conn, proposal_id: str, version: int) -> str | None:
    """Who approved the version of this proposal that is about to be released."""
    row = conn.execute(
        "SELECT actor_user_id FROM bookkeeping_approvals WHERE proposal_id=? AND decision='approve' "
        "AND proposal_version=? ORDER BY decided_at DESC LIMIT 1",
        (proposal_id, version)).fetchone()
    return row[0] if row else None


def execute_saved(conn, proposal_id: str, adapter, actor_user_id: str,
                  *, allow_single_operator: bool = False,
                  single_operator_note: str = "") -> MutationResult:
    """Execute and persist one approved proposal, idempotently.

    Approval and release are separate acts. By default the operator who approved
    a proposal may not also release it. `allow_single_operator` exists because a
    one-person company genuinely cannot separate them yet — but it is never
    silent: the execution row records that duties were not separated and why,
    and that record travels in the working papers.
    """
    row = conn.execute("SELECT * FROM bookkeeping_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError("Proposal not found")
    prior = conn.execute(
        "SELECT provider_type,provider_id,provider_sync_token,status FROM bookkeeping_executions "
        "WHERE proposal_id=?", (proposal_id,)).fetchone()
    if prior and prior[3] == "verified":
        readback = adapter.read(prior[0], prior[1])
        return MutationResult(prior[0], prior[1], prior[2] or "", readback, replayed=True)
    proposal = Proposal(
        row["id"], row["finding_id"], row["action_type"], row["target_type"],
        row["target_provider_id"], json.loads(row["current_json"]), json.loads(row["proposed_json"]),
        row["reason"], money(row["financial_effect"]), money(row["tax_effect"]),
        row["expected_sync_token"], row["status"], row["version"])
    if proposal.status != "approved":
        raise ValueError("Proposal has not been approved")

    approved_by = approver_of(conn, proposal_id, proposal.version)
    if not approved_by:
        raise PermissionError(
            "This proposal has no approval record for its current version and cannot be released.")
    duties_separated = approved_by != actor_user_id
    duties_note = None
    if not duties_separated:
        if not allow_single_operator:
            raise PermissionError(
                "Separation of duties: the operator who approved this proposal cannot also "
                "release it to QuickBooks. A second reviewer must approve, or release it from "
                "their account.")
        duties_note = single_operator_note or (
            "Released by the approving operator under the single-operator exception.")

    idempotency_key = f"shimline-{proposal.proposal_id}-v{proposal.version}"
    execution_id = crm.new_id("exe")
    if not prior:
        conn.execute(
            "INSERT INTO bookkeeping_executions(id,proposal_id,idempotency_key,provider,request_hash,status,"
            "attempt_count,executed_by,approved_by,duties_separated,duties_exception_note) "
            "VALUES(?,?,?,'quickbooks',?,'started',1,?,?,?,?)",
            (execution_id, proposal_id, idempotency_key, digest(proposal.proposed),
             actor_user_id, approved_by, 1 if duties_separated else 0, duties_note))
    else:
        conn.execute(
            "UPDATE bookkeeping_executions SET status='started',attempt_count=attempt_count+1,last_error=NULL,"
            "executed_by=?,approved_by=?,duties_separated=?,duties_exception_note=? WHERE proposal_id=?",
            (actor_user_id, approved_by, 1 if duties_separated else 0, duties_note, proposal_id))
    conn.commit()
    try:
        result = execute(proposal, adapter)
    except Exception as exc:
        conn.execute(
            "UPDATE bookkeeping_executions SET status='failed',last_error=?,finished_at=CURRENT_TIMESTAMP "
            "WHERE proposal_id=?", (str(exc)[:1000], proposal_id))
        conn.commit()
        raise
    conn.execute(
        "UPDATE bookkeeping_executions SET status='verified',provider_type=?,provider_id=?,provider_sync_token=?,"
        "finished_at=CURRENT_TIMESTAMP WHERE proposal_id=?",
        (result.object_type, result.object_id, result.sync_token, proposal_id))
    conn.execute(
        "UPDATE bookkeeping_proposals SET status='verified',updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (proposal_id,))
    conn.execute(
        "UPDATE bookkeeping_findings SET status='resolved' WHERE id=?", (row["finding_id"],))
    conn.execute(
        "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) VALUES(?,?,?,?,?,?)",
        (crm.new_id("aud"), actor_user_id, "bookkeeping.proposal.verified", "proposal", proposal_id,
         f"QBO read-back verified {result.object_type} {result.object_id}"))
    conn.commit()
    return result


def persistent_working_papers(conn, run_id: str) -> dict:
    """Build and hash the durable package from the control tables."""
    run = conn.execute("SELECT * FROM bookkeeping_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError("Bookkeeping run not found")

    def rows(sql: str) -> list[dict]:
        return [dict(row) for row in conn.execute(sql, (run_id,)).fetchall()]

    payload = {
        "engine": "Bookkeeping Work Engine v0",
        "run": dict(run),
        "coverage": {
            "accounts": rows(
                "SELECT a.* FROM bookkeeping_accounts a WHERE a.organization_id="
                "(SELECT organization_id FROM bookkeeping_runs WHERE id=?) ORDER BY a.name"),
            "transaction_count": conn.execute(
                "SELECT COUNT(*) FROM bookkeeping_transactions WHERE run_id=?", (run_id,)).fetchone()[0],
            # Per-check status for every advertised check, so a reader can see
            # what was examined and what was not, rather than inferring coverage
            # from the findings that happened to fire.
            "checks": json.loads(run["coverage_json"] or "{}").get("checks", []),
        },
        "findings": rows("SELECT * FROM bookkeeping_findings WHERE run_id=? ORDER BY defect_type,title"),
        "evidence_requests": rows(
            "SELECT q.* FROM bookkeeping_evidence_requests q JOIN bookkeeping_findings f ON f.id=q.finding_id "
            "WHERE f.run_id=? ORDER BY q.created_at"),
        "proposals": rows("SELECT * FROM bookkeeping_proposals WHERE run_id=? ORDER BY created_at"),
        "approvals": rows(
            "SELECT a.* FROM bookkeeping_approvals a JOIN bookkeeping_proposals p ON p.id=a.proposal_id "
            "WHERE p.run_id=? ORDER BY a.decided_at"),
        "executions": rows(
            "SELECT x.* FROM bookkeeping_executions x JOIN bookkeeping_proposals p ON p.id=x.proposal_id "
            "WHERE p.run_id=? ORDER BY x.started_at"),
        "reconciliations": rows(
            "SELECT * FROM bookkeeping_reconciliations WHERE run_id=? ORDER BY account_ref"),
    }
    payload["package_hash"] = digest(payload)
    conn.execute(
        "INSERT INTO bookkeeping_working_papers(id,run_id,package_json,package_hash) VALUES(?,?,?,?) "
        "ON CONFLICT(run_id) DO UPDATE SET package_json=excluded.package_json,package_hash=excluded.package_hash,"
        "created_at=CURRENT_TIMESTAMP",
        (crm.new_id("wpk"), run_id, canonical_json(payload), payload["package_hash"]))
    conn.commit()
    return payload
