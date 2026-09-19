"""Normalize customer-produced QuickBooks CSV exports into canonical objects.

The live OAuth path is the preferred source.  This module is the deliberate
fallback for a first customer while Intuit production approval is pending.  It
accepts two reports exported by the customer:

* Transaction Detail by Account (or General Ledger), and
* Accounts Receivable Aging Detail.

The parser is intentionally strict.  A missing or ambiguous source is an
error, never an inferred zero.  The returned objects use the same small QBO
shape consumed by :mod:`shimline.work_engine`, so the existing evidence,
review, reporting, and retention controls remain in force.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation


MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_ROWS = 100_000


class ExportError(ValueError):
    """The supplied files cannot support an honest review."""


@dataclass(frozen=True)
class ExportPackage:
    objects: dict[str, list[dict]]
    evidence: dict
    warnings: list[str]
    rows_imported: dict[str, int]


def _decode(data: bytes, label: str) -> str:
    if not data:
        raise ExportError(f"{label} is empty")
    if len(data) > MAX_EXPORT_BYTES:
        raise ExportError(f"{label} exceeds the 8 MB limit")
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExportError(f"{label} is not a supported CSV encoding")


def _table(data: bytes, label: str) -> list[list[str]]:
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(_decode(data, label)))]
    rows = [row for row in rows if any(row)]
    if len(rows) > MAX_ROWS:
        raise ExportError(f"{label} has more than {MAX_ROWS:,} rows")
    if not rows:
        raise ExportError(f"{label} has no readable rows")
    return rows


def _heading(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


ALIASES = {
    "date": {"date", "transactiondate", "txndate"},
    "type": {"transactiontype", "type", "txntype"},
    "number": {"num", "no", "number", "transactionno", "transactionnumber", "docnumber"},
    "name": {"name", "payee", "vendor", "customer"},
    "memo": {"memodescription", "memo", "description"},
    "account": {"account", "accountname"},
    "project": {"customerproject", "customerjob", "project", "job"},
    "amount": {"amount", "transactionamount", "total"},
    "debit": {"debit", "debits"},
    "credit": {"credit", "credits"},
    "due_date": {"duedate"},
    "open_balance": {"openbalance", "balanceopen", "openamount"},
}


def _header(rows: list[list[str]], label: str, required: tuple[str, ...]) -> tuple[int, dict[str, int]]:
    for index, row in enumerate(rows[:50]):
        normalized = [_heading(cell) for cell in row]
        mapping = {}
        for field, aliases in ALIASES.items():
            for column, value in enumerate(normalized):
                if value in aliases:
                    mapping[field] = column
                    break
        if all(field in mapping for field in required):
            return index, mapping
    names = ", ".join(required)
    raise ExportError(f"{label} is missing a recognizable header with: {names}")


def _cell(row: list[str], mapping: dict[str, int], field: str) -> str:
    column = mapping.get(field)
    return row[column].strip() if column is not None and column < len(row) else ""


def _money(value: str, *, label: str, blank: Decimal | None = None) -> Decimal | None:
    text = value.strip()
    if not text:
        return blank
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if cleaned in {"", "-", "."}:
        raise ExportError(f"{label} contains a non-numeric amount: {value!r}")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ExportError(f"{label} contains a non-numeric amount: {value!r}") from exc
    return -abs(amount) if negative else amount


DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y",
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
)


def _date(value: str, *, label: str) -> str:
    text = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ExportError(f"{label} contains an unsupported date: {value!r}")


def _as_of(rows: list[list[str]], header_index: int, label: str) -> str | None:
    for row in rows[:header_index]:
        text = " ".join(cell for cell in row if cell).strip()
        match = re.search(r"\bas\s+of\s+(.+)$", text, re.I)
        if match:
            return _date(match.group(1), label=f"{label} as-of heading")
    return None


TYPE_MAP = {
    "invoice": "Invoice",
    "salesreceipt": "Invoice",
    "payment": "Payment",
    "receivepayment": "Payment",
    "bill": "Bill",
    "billpayment": "Payment",
    "expense": "Purchase",
    "check": "Purchase",
    "cheque": "Purchase",
    "creditcardexpense": "Purchase",
    "cashpurchase": "Purchase",
    "deposit": "Deposit",
    "journalentry": "JournalEntry",
    "estimate": "Estimate",
}


def _object_type(value: str) -> str | None:
    return TYPE_MAP.get(_heading(value))


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "\0".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:20]}"


def _account_type(name: str) -> str:
    value = name.casefold()
    if any(word in value for word in ("income", "revenue", "sales")):
        return "Income"
    if any(word in value for word in ("cost of goods", "cogs", "job cost", "materials")):
        return "Cost of Goods Sold"
    if any(word in value for word in ("expense", "subcontract", "labour", "labor")):
        return "Expense"
    if any(word in value for word in ("receivable", "a/r")):
        return "Accounts Receivable"
    if any(word in value for word in ("payable", "a/p")):
        return "Accounts Payable"
    if any(word in value for word in ("bank", "checking", "chequing", "cash")):
        return "Bank"
    return "Unknown"


def _project_parts(value: str) -> tuple[str, str] | None:
    text = value.strip()
    if not text:
        return None
    parts = [part.strip() for part in re.split(r"\s*[:>]\s*", text, maxsplit=1)]
    if len(parts) == 2 and all(parts):
        return parts[0], text
    return text, text


def parse_exports(general_ledger: bytes, ar_aging: bytes, *,
                  ledger_name: str = "general-ledger.csv",
                  ar_name: str = "ar-aging-detail.csv") -> ExportPackage:
    """Parse the two required exports and return provider-shaped objects."""
    ledger_rows = _table(general_ledger, ledger_name)
    ledger_header, ledger_map = _header(
        ledger_rows, ledger_name, ("date", "type", "amount"))
    if "account" not in ledger_map:
        raise ExportError(
            f"{ledger_name} needs an Account column. Export Transaction Detail by Account "
            "with columns Date, Transaction Type, Num, Name, Account, Amount, and Customer/Project.")

    ar_rows = _table(ar_aging, ar_name)
    ar_header, ar_map = _header(
        ar_rows, ar_name, ("date", "type", "open_balance"))

    objects = {name: [] for name in (
        "Account", "Customer", "Vendor", "Class", "TaxCode", "Estimate", "Invoice",
        "Payment", "Bill", "Purchase", "Deposit", "JournalEntry", "Attachable")}
    warnings: list[str] = []
    accounts: dict[str, str] = {}
    customers: dict[str, str] = {}
    vendors: dict[str, str] = {}
    transactions: dict[tuple[str, ...], dict] = {}
    line_signatures: dict[tuple[str, ...], set[tuple[str, str, str]]] = {}

    def account_id(name: str) -> str:
        clean = name.strip() or "Unspecified account"
        key = clean.casefold()
        if key not in accounts:
            identifier = _stable_id("acc", key)
            accounts[key] = identifier
            objects["Account"].append({
                "Id": identifier, "Name": clean, "AccountType": _account_type(clean),
                "Active": True, "CurrencyRef": {"value": "CAD"},
            })
        return accounts[key]

    def customer_id(name: str, *, job: bool = False, parent: str | None = None) -> str:
        clean = name.strip()
        key = clean.casefold()
        if key not in customers:
            identifier = _stable_id("cus", key)
            customers[key] = identifier
            item = {"Id": identifier, "DisplayName": clean, "Active": True, "Job": job}
            if parent:
                item["ParentRef"] = {"value": customer_id(parent)}
            objects["Customer"].append(item)
        return customers[key]

    def vendor_id(name: str) -> str:
        clean = name.strip() or "Unknown vendor"
        key = clean.casefold()
        if key not in vendors:
            identifier = _stable_id("ven", key)
            vendors[key] = identifier
            objects["Vendor"].append({"Id": identifier, "DisplayName": clean, "Active": True})
        return vendors[key]

    imported_ledger = 0
    ignored_types: set[str] = set()
    for row_number, row in enumerate(ledger_rows[ledger_header + 1:], ledger_header + 2):
        raw_date, raw_type = _cell(row, ledger_map, "date"), _cell(row, ledger_map, "type")
        if not raw_date and not raw_type:
            continue
        object_type = _object_type(raw_type)
        if not object_type:
            if raw_type:
                ignored_types.add(raw_type)
            continue
        txn_date = _date(raw_date, label=f"{ledger_name} row {row_number}")
        amount = _money(_cell(row, ledger_map, "amount"), label=f"{ledger_name} row {row_number}")
        if amount is None:
            continue
        number = _cell(row, ledger_map, "number")
        name = _cell(row, ledger_map, "name")
        account_name = _cell(row, ledger_map, "account")
        memo = _cell(row, ledger_map, "memo")
        # A document number groups split lines. Without one, each row remains
        # separate; merging anonymous same-day entries would hide duplicates.
        group = number or f"row-{row_number}"
        key = base_key = (object_type, txn_date, group, name.casefold())
        # Two rows that agree on account, memo and amount are not a split: a
        # split divides one payment across different accounts or amounts. They
        # are either the same charge entered twice or an export that cannot say
        # otherwise, and merging them would turn two $500 charges into one
        # $1,000 charge -- inflating job cost and hiding the duplicate from the
        # detector that looks for it. Each stays its own transaction so the
        # money is preserved and a reviewer decides. A bill that genuinely
        # repeats an identical line is the rare case, and it surfaces as a
        # finding with both rows attached rather than as a silent total.
        signature = (account_name.casefold(), memo.casefold(), str(amount))
        occurrence = 0
        while signature in line_signatures.setdefault(key, set()):
            occurrence += 1
            key = base_key + (str(occurrence),)
        line_signatures.setdefault(key, set()).add(signature)
        transaction = transactions.get(key)
        if transaction is None:
            identifier = _stable_id("txn", *key)
            transaction = {
                "Id": identifier, "TxnDate": txn_date, "DocNumber": number or None,
                "TotalAmt": Decimal("0"), "Line": [], "CurrencyRef": {"value": "CAD"},
            }
            if object_type in {"Invoice", "Payment", "Deposit", "Estimate"} and name:
                transaction["CustomerRef"] = {"value": customer_id(name)}
            elif object_type in {"Bill", "Purchase"}:
                transaction["VendorRef"] = {"value": vendor_id(name)}
                transaction["EntityRef"] = {"value": vendor_id(name)}
            transactions[key] = transaction
            objects[object_type].append(transaction)

        project = _project_parts(_cell(row, ledger_map, "project"))
        project_ref = None
        if project:
            parent, display = project
            project_ref = customer_id(display, job=True, parent=parent)
        account_ref = account_id(account_name)
        magnitude = abs(amount)
        if object_type not in {"Payment", "Deposit", "JournalEntry"}:
            transaction["TotalAmt"] += magnitude
        else:
            transaction["TotalAmt"] += amount
        if object_type in {"Bill", "Purchase"}:
            detail_name = "AccountBasedExpenseLineDetail"
        elif object_type in {"Invoice", "Estimate"}:
            detail_name = "SalesItemLineDetail"
        else:
            detail_name = "JournalEntryLineDetail"
        detail = {"AccountRef": {"value": account_ref}}
        if project_ref:
            detail["CustomerRef"] = {"value": project_ref}
        if detail_name == "JournalEntryLineDetail":
            detail["PostingType"] = "Debit" if amount >= 0 else "Credit"
        transaction["Line"].append({
            "Id": str(len(transaction["Line"]) + 1), "Amount": str(magnitude),
            "Description": memo or None, detail_name: detail,
        })
        imported_ledger += 1

    if not imported_ledger:
        raise ExportError(f"{ledger_name} contains no supported transaction rows")
    if ignored_types:
        warnings.append("Ignored unsupported transaction types: " + ", ".join(sorted(ignored_types)))

    # Overlay authoritative open balances from A/R Aging Detail.  It may
    # contain invoices that the ledger export omitted; those are added with no
    # fabricated line detail, so job-margin reporting remains conservative.
    invoice_index: dict[tuple[str, str, str], dict] = {}
    for item in objects["Invoice"]:
        customer = str((item.get("CustomerRef") or {}).get("value", ""))
        invoice_index[((item.get("DocNumber") or "").casefold(), item["TxnDate"], customer)] = item
    imported_ar = 0
    ar_total = Decimal("0")
    explicit_zero_total = False
    reported_total: Decimal | None = None
    for row_number, row in enumerate(ar_rows[ar_header + 1:], ar_header + 2):
        raw_type = _cell(row, ar_map, "type")
        if any(_heading(cell).startswith("total") for cell in row):
            total = _money(
                _cell(row, ar_map, "open_balance"), label=f"{ar_name} row {row_number}",
                blank=None)
            if total is not None:
                reported_total = total
                explicit_zero_total = total == 0
            continue
        if _heading(raw_type) not in {"invoice", "salesreceipt"}:
            continue
        raw_date = _cell(row, ar_map, "date")
        txn_date = _date(raw_date, label=f"{ar_name} row {row_number}")
        balance = _money(_cell(row, ar_map, "open_balance"), label=f"{ar_name} row {row_number}")
        if balance is None or balance <= 0:
            continue
        number = _cell(row, ar_map, "number")
        name = _cell(row, ar_map, "name")
        # The due date is recorded on the invoice because the export publishes
        # it and a canonical object should not quietly lose a field it was
        # handed. It deliberately does not drive aging: `reporting.py` ages
        # from the invoice date, which is the definition the thresholds are
        # calibrated against. Keeping the value makes that a stated choice
        # rather than a parser that reads a column and drops it.
        raw_due = _cell(row, ar_map, "due_date")
        due_date = _date(raw_due, label=f"{ar_name} row {row_number}") if raw_due else ""
        customer_ref = customer_id(name) if name else ""
        key = (number.casefold(), txn_date, customer_ref)
        invoice = invoice_index.get(key)
        if invoice is None:
            identifier = _stable_id("txn", "Invoice", txn_date, number or f"ar-{row_number}", name.casefold())
            invoice = {
                "Id": identifier, "TxnDate": txn_date, "DocNumber": number or None,
                "TotalAmt": str(balance), "Balance": str(balance), "Line": [],
                "CurrencyRef": {"value": "CAD"},
            }
            if name:
                invoice["CustomerRef"] = {"value": customer_ref}
            objects["Invoice"].append(invoice)
            invoice_index[key] = invoice
        invoice["Balance"] = str(balance)
        if due_date:
            invoice["DueDate"] = due_date
        ar_total += balance
        imported_ar += 1

    if not imported_ar and not explicit_zero_total:
        raise ExportError(
            f"{ar_name} contains neither open invoice rows nor an explicit zero total")
    if reported_total is not None and reported_total != ar_total:
        raise ExportError(
            f"{ar_name} open invoice rows total {ar_total:.2f}, but the report control total "
            f"is {reported_total:.2f}")

    all_dates = [item["TxnDate"] for kind in TYPE_MAP.values() for item in objects.get(kind, [])]
    period_start = min(all_dates)
    period_end = _as_of(ar_rows, ar_header, ar_name) or max(all_dates)
    if period_end < max(all_dates):
        raise ExportError(f"{ar_name} as-of date is earlier than a transaction in the supplied ledger")
    for rows in objects.values():
        for item in rows:
            if isinstance(item.get("TotalAmt"), Decimal):
                item["TotalAmt"] = str(item["TotalAmt"].quantize(Decimal("0.01")))
    evidence = {
        "period_start": period_start,
        "period_end": period_end,
        "source_documents": {},
        "receivable_confirmations": {},
        "source_types": ["qbo_transaction_detail_csv", "qbo_ar_aging_detail_csv"],
        "source_files": [ledger_name, ar_name],
        "ar_open_balance_control": str(ar_total.quantize(Decimal("0.01"))),
    }
    return ExportPackage(
        objects=objects, evidence=evidence, warnings=warnings,
        rows_imported={"transaction_detail": imported_ledger, "ar_aging": imported_ar})
