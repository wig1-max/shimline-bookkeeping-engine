"""A generator of QuickBooks-shaped documents, including ones nobody would write.

The blocking model says the whole of the risk is *unknown shapes*: the thirty-three
refusal paths the derivation knows about are now exercised, and `p` -- the chance
a real document meets something the engine has never seen -- is made of the cases
nobody thought of. A fixture suite cannot find those, by construction. Every
fixture in this repository is a shape somebody already imagined.

So this builds documents from the documented grammar and then damages them in the
ways real company files are damaged: a field omitted, an amount as a string, a
line with no account, a total that disagrees with its lines, a currency nobody
expected. The tests that use it assert *invariants* rather than outputs -- a
ledger balances or refuses, nothing is silently dropped, a refusal names its
document -- because for a randomly damaged document there is no expected answer,
only properties that must hold whatever the answer is.

**Why not Hypothesis.** Its real gift is shrinking, and the strategies would all
still have to be written by hand because this is a domain grammar, not integers.
A document here is a small dict, so the shrink moves are obvious and specific:
drop a document, drop a key, simplify a value. `shrink` below is thirty lines and
gives reproducible minimal counterexamples without adding a dependency to a
product whose whole argument is that it refuses rather than guesses. If that
stops being true, revisit it -- this is not a principled objection to the library.

Development tool. Not under `shimline/`, so not part of the deployed path set.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from shimline.postings import POSTING_TYPES

CHART = [
    {"Id": "35", "Name": "Chequing", "AccountType": "Bank"},
    {"Id": "36", "Name": "Savings", "AccountType": "Bank"},
    {"Id": "84", "Name": "A/R", "AccountType": "Accounts Receivable"},
    {"Id": "33", "Name": "A/P", "AccountType": "Accounts Payable"},
    {"Id": "79", "Name": "Contract Income", "AccountType": "Income"},
    {"Id": "64", "Name": "Job Materials", "AccountType": "Cost of Goods Sold"},
    {"Id": "89", "Name": "GST/HST Payable", "AccountType": "Other Current Liability"},
]
ACCOUNT_IDS = [account["Id"] for account in CHART]
BANKS = ["35", "36"]

# Amounts a real file contains. Strings and nulls are not hypothetical: QBO's
# JSON has carried numbers as both, and a line can arrive with no amount at all.
AMOUNTS = [0, 1, 100, 100.005, 0.01, -10, 1234.56, "100", "100.00",
           99999999.99, None]


def _line(rng: random.Random, style: str) -> dict:
    amount = rng.choice(AMOUNTS)
    account = rng.choice(ACCOUNT_IDS)
    if style == "income":
        return {"Id": "1", "Amount": amount, "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {"ItemAccountRef": {"value": account}}}
    if style == "expense":
        return {"Id": "1", "Amount": amount,
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account}}}
    if style == "deposit":
        return {"Id": "1", "Amount": amount, "DetailType": "DepositLineDetail",
                "DepositLineDetail": {"AccountRef": {"value": account}}}
    if style == "journal":
        return {"Id": "1", "Amount": amount,
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {
                    "PostingType": rng.choice(["Debit", "Credit"]),
                    "AccountRef": {"value": account}}}
    if style == "subtotal":
        return {"DetailType": "SubTotalLineDetail", "Amount": amount,
                "SubTotalLineDetail": {}}
    return {"DetailType": "DescriptionOnly", "Description": "thanks"}


_STYLES = {
    "Invoice": "income", "CreditMemo": "income", "SalesReceipt": "income",
    "RefundReceipt": "income", "Payment": "income",
    "Bill": "expense", "Purchase": "expense", "VendorCredit": "expense",
    "BillPayment": "expense",
    "Deposit": "deposit", "JournalEntry": "journal", "Transfer": "expense",
}

# Every way a document gets damaged, applied at random. Each is something a real
# file does, not an invented hostility.
DAMAGE = (
    "drop_id", "drop_date", "drop_total", "drop_lines", "empty_lines",
    "drop_account_ref", "drop_deposit_ref", "drop_control_account",
    "foreign_currency", "odd_exchange_rate", "add_tax", "add_tax_lines",
    "unknown_account", "total_disagrees", "extra_unknown_field",
    "duplicate_line", "add_subtotal_row", "negative_total", "string_total",
    "same_transfer_accounts", "null_line_amount",
)


def document(rng: random.Random, kind: str, *, damage: int = 1) -> dict:
    """One document of `kind`, damaged `damage` ways."""
    style = _STYLES.get(kind, "expense")
    txn: dict = {
        "Id": f"{kind[:2].upper()}{rng.randrange(1000)}",
        "TxnDate": "2026-07-15",
        "TotalAmt": rng.choice([0, 100, 113, 226.5, 1000]),
        "Line": [_line(rng, style) for _ in range(rng.randrange(1, 3))],
    }
    if kind == "Transfer":
        txn.pop("Line", None)
        txn["Amount"] = txn.pop("TotalAmt")
        txn["FromAccountRef"] = {"value": rng.choice(BANKS)}
        txn["ToAccountRef"] = {"value": rng.choice(BANKS)}
    if kind in ("Purchase",):
        txn["AccountRef"] = {"value": rng.choice(BANKS)}
    if kind in ("Payment", "Deposit", "SalesReceipt", "RefundReceipt"):
        txn["DepositToAccountRef"] = {"value": rng.choice(BANKS)}
    if kind == "BillPayment":
        txn["CheckPayment"] = {"BankAccountRef": {"value": rng.choice(BANKS)}}
        txn.pop("Line", None)

    for _ in range(damage):
        _damage(rng, txn, rng.choice(DAMAGE))
    return txn


def _damage(rng: random.Random, txn: dict, how: str) -> None:
    lines = txn.get("Line") or []
    if how == "drop_id":
        txn.pop("Id", None)
    elif how == "drop_date":
        txn.pop("TxnDate", None)
    elif how == "drop_total":
        txn.pop("TotalAmt", None)
    elif how == "drop_lines":
        txn.pop("Line", None)
    elif how == "empty_lines":
        txn["Line"] = []
    elif how == "drop_account_ref":
        txn.pop("AccountRef", None)
        txn.pop("CheckPayment", None)
    elif how == "drop_deposit_ref":
        txn.pop("DepositToAccountRef", None)
    elif how == "drop_control_account":
        txn.pop("ARAccountRef", None)
        txn.pop("APAccountRef", None)
        txn.pop("FromAccountRef", None)
    elif how == "foreign_currency":
        txn["CurrencyRef"] = {"value": rng.choice(["USD", "EUR", "CAD"])}
        txn.setdefault("ExchangeRate", rng.choice([1, 1.0, 1.37, "1.37"]))
    elif how == "odd_exchange_rate":
        txn["ExchangeRate"] = rng.choice(["about 1.4", None, 0, -1, ""])
    elif how == "add_tax":
        txn["TxnTaxDetail"] = {"TotalTax": rng.choice([0, 13, 13.005, "13"])}
    elif how == "add_tax_lines":
        txn.setdefault("TxnTaxDetail", {})["TotalTax"] = 13
        txn["TxnTaxDetail"]["TaxLine"] = [{
            "Amount": rng.choice([13, None]),
            "DetailType": "TaxLineDetail",
            "TaxLineDetail": {"TaxRateRef": {"value": rng.choice(
                ACCOUNT_IDS + ["4"])}}}]
    elif how == "unknown_account" and lines:
        for key in ("AccountBasedExpenseLineDetail", "SalesItemLineDetail",
                    "DepositLineDetail", "JournalEntryLineDetail"):
            if key in lines[0]:
                ref = "AccountRef" if "AccountRef" in lines[0][key] else "ItemAccountRef"
                lines[0][key][ref] = {"value": "does-not-exist"}
    elif how == "total_disagrees":
        txn["TotalAmt"] = rng.choice([1, 7.77, 10_000])
    elif how == "extra_unknown_field":
        txn[f"Custom{rng.randrange(100)}"] = {"nested": ["anything", 1, None]}
    elif how == "duplicate_line" and lines:
        txn["Line"] = lines + [copy.deepcopy(lines[0])]
    elif how == "add_subtotal_row":
        txn["Line"] = lines + [_line(rng, "subtotal"), _line(rng, "description")]
    elif how == "negative_total":
        for key in ("TotalAmt", "Amount"):
            if key in txn:
                txn[key] = -abs(float(txn[key] or 0))
    elif how == "string_total":
        for key in ("TotalAmt", "Amount"):
            if key in txn:
                txn[key] = str(txn[key])
    elif how == "same_transfer_accounts" and "FromAccountRef" in txn:
        txn["ToAccountRef"] = dict(txn["FromAccountRef"])
    elif how == "null_line_amount" and lines:
        lines[0]["Amount"] = None


# Customers, and which of them QuickBooks calls a job. Generated companies used
# to carry none at all, which made check 16 -- "no customers are marked as jobs"
# -- fire on 591 of 600 companies and drown out every other detector. A
# generator that only ever triggers one check tests one check.
CUSTOMERS = [
    {"Id": "CU1", "DisplayName": "Maple Build Ltd", "Job": False, "Active": True},
    {"Id": "P1", "DisplayName": "Maple Build Ltd:Kitchen", "Job": True,
     "ParentRef": {"value": "CU1"}, "Active": True},
    {"Id": "P2", "DisplayName": "Maple Build Ltd:Basement", "Job": True,
     "ParentRef": {"value": "CU1"}, "Active": True},
]
JOB_IDS = [customer["Id"] for customer in CUSTOMERS if customer["Job"]]


def company(seed: int, *, documents: int = 6, damage: int = 1,
            chart: list[dict] | None = None) -> dict[str, list[dict]]:
    """A whole pull: a chart, some customers, and damaged documents."""
    rng = random.Random(seed)
    objects: dict[str, list[dict]] = {"Account": [dict(a) for a in (chart or CHART)]}
    # A file with no customers at all is a legitimate shape, so it is generated
    # sometimes rather than never -- but not usually, or it is the only shape.
    if rng.random() < 0.85:
        objects["Customer"] = [dict(customer) for customer in CUSTOMERS]
    for _ in range(documents):
        kind = rng.choice(POSTING_TYPES)
        txn = document(rng, kind, damage=damage)
        # Tag some lines with a job, so revenue and cost reach the margin and
        # allocation detectors instead of stopping at "jobs are not set up".
        for line in txn.get("Line") or []:
            if rng.random() < 0.6:
                for key in ("AccountBasedExpenseLineDetail", "SalesItemLineDetail",
                            "ItemBasedExpenseLineDetail"):
                    if key in line:
                        line[key]["CustomerRef"] = {"value": rng.choice(JOB_IDS)}
        objects.setdefault(kind, []).append(txn)
    return objects


# ---------------------------------------------------------------- GST/HST --

GST_AGENCIES = [
    {"Id": "CRA", "DisplayName": "Canada Revenue Agency"},
    {"Id": "BC", "DisplayName": "British Columbia Ministry of Finance"},
    {"Id": "RQ", "DisplayName": "Revenu Québec"},
]
GST_RATES = [
    {"Id": "GST5", "Name": "GST", "RateValue": "5",
     "AgencyRef": {"value": "CRA"}},
    {"Id": "HST13", "Name": "HST ON", "RateValue": "13",
     "AgencyRef": {"value": "CRA"}},
    {"Id": "PST7", "Name": "PST BC", "RateValue": "7",
     "AgencyRef": {"value": "BC"}},
    {"Id": "QST", "Name": "QST", "RateValue": "9.975",
     "AgencyRef": {"value": "RQ"}},
    # A real list can contain a client-renamed rate whose name and agency settle
    # nothing. It must block when used rather than falling onto either return.
    {"Id": "MYST", "Name": "Standard", "RateValue": "8.5"},
]
GST_FEDERAL_RATE_IDS = {"GST5", "HST13"}
GST_PROVINCIAL_RATE_IDS = {"PST7", "QST"}
GST_UNCLASSIFIED_RATE_IDS = {"MYST", "GHOST"}
GST_COLLECTED_TYPES = {"Invoice", "SalesReceipt"}
GST_PAID_TYPES = {"Bill", "Purchase"}
GST_COLLECTED_REVERSAL_TYPES = {"CreditMemo", "RefundReceipt"}
GST_PAID_REVERSAL_TYPES = {"VendorCredit"}


@dataclass
class GSTCase:
    """One generated canonical tax period, ready for the persistence seam."""

    calculation_method: str
    rates: list[dict]
    documents: list[dict]


def gst_case(seed: int, *, documents: int = 8) -> GSTCase:
    """GST/HST inputs with federal, provincial and unresolved rate shapes.

    Amounts are strings because that is how the canonical SQLite model stores
    exact money. The damage is accounting-shaped: missing or mismatched rate
    detail and a rate absent from the synced list, not arbitrary invalid SQL.
    """
    rng = random.Random(seed)
    available = [copy.deepcopy(rate) for rate in GST_RATES
                 if rng.random() < 0.85 or rate["Id"] in {"GST5", "PST7"}]
    kinds = sorted(GST_COLLECTED_TYPES | GST_PAID_TYPES |
                   GST_COLLECTED_REVERSAL_TYPES | GST_PAID_REVERSAL_TYPES)
    amounts = ["0.00", "0.01", "5.00", "13.00", "99.75", "130.00",
               "99999999.99"]
    produced = []
    for index in range(documents):
        rate_ref = rng.choice([rate["Id"] for rate in GST_RATES] + ["GHOST"])
        amount = rng.choice(amounts)
        shape = rng.choices(["complete", "no_breakdown", "mismatch"],
                            weights=[8, 1, 1], k=1)[0]
        rate_rows = [] if shape == "no_breakdown" else [{
            "ref": rate_ref,
            "base": rng.choice(["0.00", "1.00", "100.00", "1000.00"]),
            "tax": amount if shape == "complete" else "0.01",
        }]
        day = date(2026, 6, 15) + timedelta(days=rng.randrange(0, 125))
        kind = rng.choice(kinds)
        sign = (-1 if kind in GST_COLLECTED_REVERSAL_TYPES |
                GST_PAID_TYPES else 1)
        produced.append({
            "id": f"gst_{seed}_{index}", "kind": kind,
            "date": day.isoformat(), "tax_total": amount,
            "rates": rate_rows,
            "tax_posting": str(sign * Decimal(amount)),
        })
    return GSTCase(
        calculation_method=rng.choice(["regular", "quick", ""]),
        rates=available, documents=produced)


def shrink_gst(case: GSTCase, still_fails) -> GSTCase:
    """Drop documents and rates until a GST/HST counterexample is minimal."""
    current = copy.deepcopy(case)
    changed = True
    while changed:
        changed = False
        candidates = []
        for index in range(len(current.documents)):
            candidate = copy.deepcopy(current)
            del candidate.documents[index]
            candidates.append(candidate)
        for index in range(len(current.rates)):
            candidate = copy.deepcopy(current)
            del candidate.rates[index]
            candidates.append(candidate)
        for candidate in candidates:
            if still_fails(candidate):
                current, changed = candidate, True
                break
    return current


def filing_arrangement(seed: int) -> dict:
    """One fiscal arrangement, emphasizing month ends that expose date bugs."""
    rng = random.Random(seed)
    return {
        "frequency": rng.choice(["monthly", "quarterly", "annual"]),
        "year_end_month": rng.randrange(1, 13),
        "year_end_day": rng.choice([1, 15, 28, 29, 30, 31]),
        "is_individual": rng.choice([True, False, None]),
    }


def shrink_filing(arrangement: dict, still_fails) -> dict:
    """Simplify a fiscal-calendar counterexample without changing its shape."""
    current = dict(arrangement)
    candidates = []
    for key, values in (("year_end_month", [1, 2, 12]),
                        ("year_end_day", [1, 28, 29, 30, 31]),
                        ("frequency", ["annual", "quarterly", "monthly"]),
                        ("is_individual", [False, True, None])):
        for value in values:
            if value == current[key]:
                continue
            candidate = dict(current)
            candidate[key] = value
            candidates.append(candidate)
    for candidate in candidates:
        if still_fails(candidate):
            return candidate
    return current


# ------------------------------------------------------------------ shrinking

def shrink(objects: dict, still_fails) -> dict:
    """The smallest version of `objects` that still fails `still_fails`.

    A minimal counterexample is the difference between a finding somebody can act
    on and a wall of generated JSON. Three moves, each obvious for this shape:
    drop a document, drop a key from a document, simplify a value.
    """
    current = copy.deepcopy(objects)
    changed = True
    while changed:
        changed = False
        for candidate in _smaller(current):
            if still_fails(candidate):
                current, changed = candidate, True
                break
    return current


def _smaller(objects: dict):
    # `_pull_manifest` is a dict, not a list of documents, and `declare_pull`
    # adds it to whatever it is handed. Shrinking it produces nonsense.
    kinds = [k for k in objects
             if k != "Account" and not k.startswith("_") and objects.get(k)]

    # Drop a whole document.
    for kind in kinds:
        for index in range(len(objects[kind])):
            candidate = copy.deepcopy(objects)
            del candidate[kind][index]
            if not candidate[kind]:
                del candidate[kind]
            yield candidate

    # Drop a key from a document, and simplify what is left.
    for kind in kinds:
        for index, txn in enumerate(objects[kind]):
            for key in list(txn):
                if key == "Id":
                    continue
                candidate = copy.deepcopy(objects)
                del candidate[kind][index][key]
                yield candidate
            for key in ("TotalAmt", "Amount"):
                if txn.get(key) not in (None, 0, "0"):
                    candidate = copy.deepcopy(objects)
                    candidate[kind][index][key] = 0
                    yield candidate
            for index_line in range(len(txn.get("Line") or [])):
                candidate = copy.deepcopy(objects)
                del candidate[kind][index]["Line"][index_line]
                yield candidate
