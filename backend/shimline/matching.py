"""Match bank statement lines to ledger cash movements, or refuse to.

Why this is deterministic
-------------------------
The capability report recommended splink (Fellegi-Sunter probabilistic record
linkage) for this, for the sake of its explainable match weights. That was
tested against a deterministic baseline on realistic contractor noise at 182,
2,002 and 12,002 rows, and the baseline won on precision and recall at every
scale (research/probes/splink_probe.py reproduces it).

The reason is structural. Record linkage is built for the case where *no field
is reliable* -- matching people across censuses by name and birth year. Bank
reconciliation has a near-perfect key: the amount. Given a reliable key, an
explicit rule with an ambiguity refusal beats probabilistic inference, and more
training data does not close the gap.

What survived from that recommendation is the *interface*, not the library.
Splink's real appeal was returning a decomposed weight rather than a boolean.
Every match here carries its components -- amount, date distance, name evidence
-- so a reviewer can see why, and disagree.

The refusal
-----------
Two equally good candidates go to a person, never to a guess. In the probe,
splink committed to both of two deliberately identical transactions and got one
wrong at every scale; this refused both. For a system whose architecture is an
evidence gate, a confident wrong answer is the most expensive output available.

Sign convention
---------------
Both sides are signed from the account holder's point of view: positive is money
arriving, negative is money leaving. A statement line and the ledger movement it
represents must agree exactly, to the cent. No tolerance -- a near-miss on an
amount is a different transaction, not a rounding error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# How late a cheque may clear and still be the same transaction. Five days
# covers a cheque written Friday and cleared the following Wednesday, which is
# the ordinary case in the trades. Beyond that the amount coincidence is more
# likely than the timing.
DEFAULT_DATE_WINDOW_DAYS = 5

# A token shorter than this matches too much to be evidence of anything: "INC",
# "LTD" and "THE" would tie every vendor to every other.
MIN_TOKEN_LENGTH = 4


class UndatedMovement(ValueError):
    """A reconstructed movement carries no date, so matching cannot be trusted."""


def money(value=0) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class LedgerEntry:
    """One cash movement against one account, reconstructed from a document."""
    key: str                 # "Purchase:42"
    object_type: str
    object_id: str
    posted_date: date
    amount: Decimal          # signed, account holder's view
    party: str = ""
    doc_number: str = ""


@dataclass(frozen=True)
class BankLine:
    ordinal: int
    posted_date: date
    amount: Decimal
    description: str = ""
    memo: str = ""
    fitid: str | None = None

    @property
    def party(self) -> str:
        return f"{self.description} {self.memo}".strip()


@dataclass(frozen=True)
class Match:
    bank_ordinal: int
    ledger_key: str
    amount: Decimal
    date_distance_days: int
    name_tokens: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Ambiguity:
    bank_ordinal: int
    candidates: tuple[str, ...]
    reason: str


@dataclass
class MatchReport:
    matches: list[Match] = field(default_factory=list)
    ambiguous: list[Ambiguity] = field(default_factory=list)
    unmatched_bank: list[BankLine] = field(default_factory=list)
    unmatched_ledger: list[LedgerEntry] = field(default_factory=list)
    lines_considered: int = 0
    entries_considered: int = 0

    @property
    def matched_count(self) -> int:
        return len(self.matches)

    def summary(self) -> dict:
        return {
            "lines": self.lines_considered,
            "entries": self.entries_considered,
            "matched": len(self.matches),
            "ambiguous": len(self.ambiguous),
            "unmatched_bank": len(self.unmatched_bank),
            "unmatched_ledger": len(self.unmatched_ledger),
        }


# ------------------------------------------------------------------ naming --

def tokens(text: str) -> set[str]:
    """Meaningful words in a party name, upper-cased and punctuation-free."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").upper())
    return {word for word in cleaned.split() if len(word) >= MIN_TOKEN_LENGTH}


def _squashed(text: str) -> str:
    return "".join(ch for ch in (text or "").upper() if ch.isalnum())


def name_evidence(ledger_party: str, bank_party: str) -> tuple[str, ...]:
    """Ledger tokens that appear in the bank descriptor, exactly or squashed.

    Banks close up spaces -- "HOME DEPOT" arrives as "HOMEDEPOT #7021 OTTAWA
    ON" -- so whole-token equality alone finds nothing on the very names it
    most needs to. Containment against the squashed descriptor recovers those
    without inventing a fuzzy-distance threshold nobody can audit.
    """
    bank_tokens = tokens(bank_party)
    squashed = _squashed(bank_party)
    found = []
    for token in sorted(tokens(ledger_party)):
        if token in bank_tokens or token in squashed:
            found.append(token)
    return tuple(found)


def _score(entry: LedgerEntry, line: BankLine) -> tuple[int, int]:
    """Higher is better. Name evidence first, then closeness in time."""
    matched = name_evidence(entry.party, line.party)
    distance = abs((line.posted_date - entry.posted_date).days)
    return (len(matched), -distance)


def _reason(entry: LedgerEntry, line: BankLine, matched: tuple[str, ...],
            distance: int) -> str:
    parts = [f"amount exact {money(entry.amount)}"]
    if distance == 0:
        parts.append("posted the same day")
    else:
        parts.append(f"posted {distance} day{'s' if distance != 1 else ''} apart")
    if matched:
        parts.append("vendor name matched on " + ", ".join(matched))
    else:
        parts.append("no vendor name evidence; matched on amount and date alone")
    return "; ".join(parts)


# ----------------------------------------------------------------- matching --

def match(lines: list[BankLine], entries: list[LedgerEntry], *,
          window_days: int = DEFAULT_DATE_WINDOW_DAYS) -> MatchReport:
    """Pair statement lines with ledger movements, refusing where it is a toss-up.

    Candidates require the amount to agree exactly and the dates to be within
    `window_days`. Among candidates, the best name evidence wins, then the
    smallest date gap. A tie -- two candidates equally good for the same line,
    or two lines equally good for the same entry -- is not resolved. Both are
    reported as ambiguous and left to a person.

    Assignment is global rather than first-come: every admissible pair is scored,
    the strongest are taken first, and ties are detected against the whole field
    rather than against whatever happened to be examined earlier. Otherwise the
    answer would depend on the order the statement happened to arrive in.
    """
    report = MatchReport(lines_considered=len(lines), entries_considered=len(entries))

    by_key = {entry.key: entry for entry in entries}
    by_ordinal = {line.ordinal: line for line in lines}

    pairs = []
    for line in lines:
        for entry in entries:
            if entry.amount != line.amount:
                continue
            if abs((line.posted_date - entry.posted_date).days) > window_days:
                continue
            pairs.append((_score(entry, line), line.ordinal, entry.key))

    # Strongest first. The trailing keys only make the walk deterministic; they
    # never decide a contest, because an equal score is refused either way.
    pairs.sort(key=lambda item: (item[0], -item[1]), reverse=True)

    taken_lines: set[int] = set()
    taken_entries: set[str] = set()
    refused_lines: set[int] = set()

    for score, ordinal, key in pairs:
        if ordinal in taken_lines or key in taken_entries or ordinal in refused_lines:
            continue
        rivals_for_line = [other for other in pairs
                           if other[1] == ordinal and other[2] != key
                           and other[0] == score and other[2] not in taken_entries]
        rivals_for_entry = [other for other in pairs
                            if other[2] == key and other[1] != ordinal
                            and other[0] == score and other[1] not in taken_lines
                            and other[1] not in refused_lines]
        if rivals_for_line or rivals_for_entry:
            contenders = sorted({key} | {other[2] for other in rivals_for_line})
            report.ambiguous.append(Ambiguity(
                bank_ordinal=ordinal, candidates=tuple(contenders),
                reason=("more than one ledger entry fits this line equally well "
                        f"({len(contenders)} candidate(s), same amount and same "
                        "quality of evidence); a person decides this one")))
            refused_lines.add(ordinal)
            for other in rivals_for_entry:
                if other[1] not in refused_lines:
                    report.ambiguous.append(Ambiguity(
                        bank_ordinal=other[1], candidates=(key,),
                        reason=("this line and another fit the same ledger entry "
                                "equally well; a person decides which")))
                    refused_lines.add(other[1])
            continue

        entry, line = by_key[key], by_ordinal[ordinal]
        matched = name_evidence(entry.party, line.party)
        distance = abs((line.posted_date - entry.posted_date).days)
        report.matches.append(Match(
            bank_ordinal=ordinal, ledger_key=key, amount=entry.amount,
            date_distance_days=distance, name_tokens=matched,
            reason=_reason(entry, line, matched, distance)))
        taken_lines.add(ordinal)
        taken_entries.add(key)

    report.matches.sort(key=lambda item: item.bank_ordinal)
    report.ambiguous.sort(key=lambda item: item.bank_ordinal)
    report.unmatched_bank = [line for line in lines
                             if line.ordinal not in taken_lines
                             and line.ordinal not in refused_lines]
    report.unmatched_ledger = [entry for entry in entries if entry.key not in taken_entries]
    return report


# ------------------------------------------------- building the ledger side --

def _party_names(objects: dict[str, list[dict]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for kind in ("Vendor", "Customer", "Employee"):
        for record in objects.get(kind, []) or []:
            identifier = str(record.get("Id") or "")
            label = (record.get("DisplayName") or record.get("CompanyName")
                     or record.get("FullyQualifiedName") or "")
            if identifier and label:
                names[f"{kind}:{identifier}"] = str(label)
    return names


def _party_of(transaction: dict, names: dict[str, str]) -> str:
    """Whoever the money moved to or from, named as well as the pull allows."""
    for key, kinds in (("EntityRef", ("Vendor", "Customer", "Employee")),
                       ("VendorRef", ("Vendor",)),
                       ("CustomerRef", ("Customer",))):
        ref = transaction.get(key) or {}
        value = str(ref.get("value") or "")
        if not value:
            continue
        named = str(ref.get("name") or "")
        if named:
            return named
        for kind in kinds:
            if f"{kind}:{value}" in names:
                return names[f"{kind}:{value}"]
    return str(transaction.get("PrivateNote") or "")


def cash_movements(derived, objects: dict[str, list[dict]],
                   account_id: str) -> list[LedgerEntry]:
    """Every reconstructed movement against one account, as matchable entries.

    This reads the *reconstruction*, not the raw documents, which is the point:
    the same derivation that lets the ledger balance be checked against the
    statement also says which documents touched the bank account and by how
    much. One ingest, reused. A document whose postings could not be
    reconstructed is not here at all, because `derive_ledger` refuses the whole
    ledger in that case and the caller should never have got this far.
    """
    account_id = str(account_id)
    indexed: dict[str, dict] = {}
    for kind, rows in objects.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                indexed[f"{kind}:{row.get('Id')}"] = row

    names = _party_names(objects)
    entries: list[LedgerEntry] = []
    for key, postings in (derived.postings or {}).items():
        movement = sum(
            (money(posting.get("debit")) - money(posting.get("credit"))
             for posting in postings if str(posting.get("account")) == account_id),
            Decimal("0"))
        if movement == 0:
            continue
        transaction = indexed.get(key) or {}
        object_type, _, object_id = key.partition(":")
        raw_date = transaction.get("TxnDate") or ""
        try:
            posted = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            # A movement we cannot date cannot be windowed against a statement.
            # Skipping it here would quietly make its bank line look missing, so
            # the caller is told the ledger side is incomplete instead.
            raise UndatedMovement(
                f"{key} has no usable TxnDate, so it cannot be matched to a "
                "statement line by date")
        entries.append(LedgerEntry(
            key=key, object_type=object_type, object_id=str(object_id),
            posted_date=posted, amount=movement.quantize(Decimal("0.01")),
            party=_party_of(transaction, names),
            doc_number=str(transaction.get("DocNumber") or "")))
    entries.sort(key=lambda entry: (entry.posted_date, entry.key))
    return entries


def bank_lines(rows: list[dict]) -> list[BankLine]:
    """Statement rows as the matcher wants them, from storage or from evidence."""
    lines = []
    for index, row in enumerate(rows or []):
        raw_date = row.get("posted_date")
        posted = raw_date if isinstance(raw_date, date) else None
        if posted is None:
            try:
                posted = date.fromisoformat(str(raw_date)[:10])
            except (TypeError, ValueError):
                continue
        lines.append(BankLine(
            ordinal=int(row.get("ordinal", index)), posted_date=posted,
            amount=money(row.get("amount")),
            description=str(row.get("description") or ""),
            memo=str(row.get("memo") or ""),
            fitid=row.get("fitid")))
    return lines
