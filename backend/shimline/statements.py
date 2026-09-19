"""Bank statement ingestion: the evidence rail checks 04 and E2 were waiting for.

Canada has no open-banking API a service like this can rely on today. What every
Canadian chartered bank does offer, because Quicken and QuickBooks have required
it for two decades, is a downloadable OFX/QFX file, and failing that a CSV. That
is the rail this module reads.

Parsing is delegated to `ofxparse` (MIT) rather than hand-rolled. OFX is
superficially simple and actually full of per-institution quirks -- unclosed
SGML tags, `[-5:EST]` timezone suffixes, banks that populate NAME and not MEMO
or the reverse. `ofxparse` has absorbed those for years and, importantly,
returns `Decimal` for money. A hand-written parser would have to earn that
trust from scratch on the one input path where a rounding error is a wrong set
of books.

CSV has no standard at all, so it is handled separately and conservatively: the
header is mapped explicitly, and anything ambiguous is refused rather than
guessed. A statement that cannot be read correctly must fail loudly here, not
produce a plausible-looking balance that silently reconciles against nothing.
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# ofxparse writes deprecation-adjacent warnings through BeautifulSoup on some
# inputs; the import is kept local to the parse call so a CSV-only deployment
# never pays for it.

SUPPORTED_FORMATS = ("ofx", "qfx", "csv")

# Accepted CSV header spellings, lowercased and stripped. Canadian banks are not
# consistent with each other and several change these between exports, so the
# mapping is explicit and additive rather than positional.
_CSV_DATE_KEYS = ("date", "transaction date", "posted date", "posting date", "date posted")
_CSV_DESC_KEYS = ("description", "description 1", "details", "narrative", "transaction description", "payee")
_CSV_MEMO_KEYS = ("description 2", "memo", "notes", "reference")
_CSV_AMOUNT_KEYS = ("amount", "transaction amount", "value")
_CSV_DEBIT_KEYS = ("debit", "withdrawal", "withdrawals", "money out", "funds out")
_CSV_CREDIT_KEYS = ("credit", "deposit", "deposits", "money in", "funds in")

_CSV_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y", "%m/%d/%y")


class StatementError(ValueError):
    """A statement could not be read well enough to be trusted as evidence."""


@dataclass(frozen=True)
class StatementLine:
    posted_date: date
    amount: Decimal          # signed; negative is money leaving the account
    description: str = ""
    memo: str = ""
    txn_type: str = ""
    fitid: str | None = None


@dataclass
class ParsedStatement:
    bank_account_id: str
    period_start: date
    period_end: date
    closing_balance: Decimal
    lines: list[StatementLine] = field(default_factory=list)
    routing_number: str = ""
    account_type: str = ""
    currency: str = "CAD"
    source_format: str = "ofx"
    source_filename: str = ""
    content_sha256: str = ""

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def net_movement(self) -> Decimal:
        """Sum of the lines. Not the closing balance -- see reconciliation."""
        return sum((line.amount for line in self.lines), Decimal("0"))


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_format(filename: str, data: bytes) -> str:
    """Decide by content first, extension second.

    A bank that serves a QFX with a .txt name, or a CSV renamed .qfx, is common
    enough that trusting the extension alone produces a confusing parse error
    instead of a correct read.
    """
    head = data[:2048].lstrip()
    if head[:1] == b"\xef\xbb\xbf":          # UTF-8 BOM
        head = head[3:].lstrip()
    upper = head.upper()
    if b"OFXHEADER" in upper or b"<OFX>" in upper or upper.startswith(b"<?OFX"):
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return "qfx" if suffix == "qfx" else "ofx"
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix in SUPPORTED_FORMATS:
        return suffix
    if b"," in head:
        return "csv"
    raise StatementError(
        "Unrecognised statement format. Supply an OFX/QFX download or a CSV "
        "with a header row.")


def parse(data: bytes, filename: str = "", *, currency: str = "CAD") -> ParsedStatement:
    """Read a statement file into a normalised, hashed record."""
    if not data:
        raise StatementError("The statement file is empty.")
    fmt = detect_format(filename, data)
    if fmt == "csv":
        statement = _parse_csv(data, currency=currency)
    else:
        statement = _parse_ofx(data)
    statement.source_format = fmt
    statement.source_filename = filename
    statement.content_sha256 = content_hash(data)
    if statement.period_end < statement.period_start:
        raise StatementError(
            f"Statement period ends ({statement.period_end}) before it starts "
            f"({statement.period_start}).")
    return statement


def _parse_ofx(data: bytes) -> ParsedStatement:
    try:
        from ofxparse import OfxParser
    except ImportError as exc:                       # pragma: no cover - packaging
        raise StatementError(
            "OFX support requires the ofxparse package.") from exc

    try:
        parsed = OfxParser.parse(io.BytesIO(data))
    except Exception as exc:
        raise StatementError(f"Could not read this OFX/QFX file: {exc}") from exc

    accounts = getattr(parsed, "accounts", None) or []
    if not accounts:
        single = getattr(parsed, "account", None)
        accounts = [single] if single else []
    if not accounts:
        raise StatementError("The file contains no account.")
    if len(accounts) > 1:
        # Refusing is deliberate. Picking the first account would reconcile one
        # account's balance against another's ledger without saying so.
        raise StatementError(
            f"The file contains {len(accounts)} accounts. Export one account "
            "per file so each statement maps to exactly one ledger account.")

    account = accounts[0]
    statement = getattr(account, "statement", None)
    if statement is None:
        raise StatementError("The account carries no statement.")
    if getattr(statement, "balance", None) is None:
        raise StatementError(
            "The statement carries no closing balance, so it cannot prove an "
            "account balance.")

    lines = []
    for txn in getattr(statement, "transactions", []) or []:
        amount = _decimal(getattr(txn, "amount", None))
        if amount is None:
            raise StatementError(
                f"Transaction {getattr(txn, 'id', '?')} has no readable amount.")
        lines.append(StatementLine(
            posted_date=_as_date(txn.date),
            amount=amount,
            description=(getattr(txn, "payee", "") or "").strip(),
            memo=(getattr(txn, "memo", "") or "").strip(),
            txn_type=(getattr(txn, "type", "") or "").strip(),
            fitid=(getattr(txn, "id", "") or "").strip() or None,
        ))

    return ParsedStatement(
        bank_account_id=str(getattr(account, "account_id", "") or "").strip(),
        routing_number=str(getattr(account, "routing_number", "") or "").strip(),
        account_type=str(getattr(account, "type", "") or "").strip(),
        currency=(getattr(statement, "currency", "") or "CAD").strip().upper() or "CAD",
        period_start=_as_date(statement.start_date),
        period_end=_as_date(statement.end_date),
        closing_balance=_decimal(statement.balance),
        lines=lines,
    )


def _parse_csv(data: bytes, *, currency: str = "CAD") -> ParsedStatement:
    """Read a CSV export.

    CSV carries no account identifier, no period, and no closing balance, so
    those are derived from the rows and the caller must map the account
    explicitly. A CSV statement is therefore weaker evidence than an OFX one,
    and `closing_balance` is only trustworthy when the file supplies a running
    balance column. When it does not, this raises rather than inventing one.
    """
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise StatementError("The CSV has no header row.")

    headers = {(name or "").strip().lower(): (name or "") for name in reader.fieldnames}
    date_col = _first_match(headers, _CSV_DATE_KEYS)
    if not date_col:
        raise StatementError(
            f"No date column found. Looked for one of: {', '.join(_CSV_DATE_KEYS)}.")
    amount_col = _first_match(headers, _CSV_AMOUNT_KEYS)
    debit_col = _first_match(headers, _CSV_DEBIT_KEYS)
    credit_col = _first_match(headers, _CSV_CREDIT_KEYS)
    if not amount_col and not (debit_col or credit_col):
        raise StatementError(
            "No amount column found. Supply either an 'Amount' column or "
            "separate debit/credit columns.")
    desc_col = _first_match(headers, _CSV_DESC_KEYS)
    memo_col = _first_match(headers, _CSV_MEMO_KEYS)
    balance_col = _first_match(headers, ("balance", "running balance", "account balance"))

    lines: list[StatementLine] = []
    last_balance: Decimal | None = None
    for number, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            continue
        posted = _parse_csv_date((row.get(date_col) or "").strip(), number)
        amount = _csv_amount(row, amount_col, debit_col, credit_col, number)
        lines.append(StatementLine(
            posted_date=posted,
            amount=amount,
            description=(row.get(desc_col) or "").strip() if desc_col else "",
            memo=(row.get(memo_col) or "").strip() if memo_col else "",
        ))
        if balance_col:
            running = _decimal((row.get(balance_col) or "").strip())
            if running is not None:
                last_balance = running

    if not lines:
        raise StatementError("The CSV contains no transaction rows.")
    if last_balance is None:
        raise StatementError(
            "This CSV has no running-balance column, so it cannot establish a "
            "closing balance. Supply an OFX/QFX export, or a CSV that includes "
            "the account balance.")

    dates = [line.posted_date for line in lines]
    return ParsedStatement(
        bank_account_id="",           # CSV states none; the operator maps it
        period_start=min(dates),
        period_end=max(dates),
        closing_balance=last_balance,
        lines=lines,
        currency=currency.upper(),
    )


def _first_match(headers: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for key in candidates:
        if key in headers:
            return headers[key]
    return None


def _csv_amount(row: dict, amount_col, debit_col, credit_col, line_number: int) -> Decimal:
    if amount_col:
        value = _decimal((row.get(amount_col) or "").strip())
        if value is None:
            raise StatementError(f"Row {line_number}: amount is not a number.")
        return value
    debit = _decimal((row.get(debit_col) or "").strip()) if debit_col else None
    credit = _decimal((row.get(credit_col) or "").strip()) if credit_col else None
    if debit is None and credit is None:
        raise StatementError(f"Row {line_number}: neither debit nor credit is a number.")
    if debit and credit:
        raise StatementError(
            f"Row {line_number}: both debit and credit are populated, so the "
            "direction of the movement is ambiguous.")
    if debit:
        return -abs(debit)
    return abs(credit or Decimal("0"))


def _parse_csv_date(value: str, line_number: int) -> date:
    if not value:
        raise StatementError(f"Row {line_number}: the date is blank.")
    for fmt in _CSV_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise StatementError(
        f"Row {line_number}: could not read the date {value!r}. Use ISO "
        "(YYYY-MM-DD) if the bank offers it.")


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return -parsed if negative else parsed


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise StatementError(f"Unreadable date value {value!r}.")
