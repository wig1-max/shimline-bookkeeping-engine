"""Export the canonical ledger to Beancount, so a second engine can check it.

`work_engine` computes account balances with `trial_balance`, which it imports
from `synthetic_books` -- the same module that generates the golden ledger the
acceptance suite grades it against. The thing being validated and the thing
validating it share an implementation. If a double-entry assumption is wrong it
is wrong in both places, and the suite still passes.

This module closes that gap by writing the same transactions out in Beancount's
plain-text format, together with Shimline's *claimed* balances as `balance`
assertions. Running `bean-check` over the result makes Beancount recompute every
balance by its own rules and refuse the file if it disagrees. Two independently
written double-entry engines then have to agree before the build goes green.

Beancount also enforces something `trial_balance` never checks: that each
individual transaction sums to zero. `trial_balance` accumulates debits minus
credits per account, so a transaction whose own postings do not balance still
produces a plausible-looking trial balance and nothing notices. Beancount
rejects it outright.

Licence note, deliberate: Beancount is GPL-2.0. Nothing here imports it. This
module writes text; verification runs the `bean-check` *program* in a
subprocess. Beancount is a development and CI tool, is absent from
requirements.txt, and is never distributed with Shimline. Keep it that way --
if this ever becomes an import, the licence question changes completely.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .postings import POSTING_TYPES, derive_ledger

# QBO AccountType -> Beancount's five roots. Beancount will not accept an
# account outside these, and the root determines the natural sign of a balance,
# so an unmapped type must fail loudly rather than default to Assets and quietly
# invert an Income balance.
ACCOUNT_ROOTS: dict[str, str] = {
    "Bank": "Assets",
    "Accounts Receivable": "Assets",
    "Other Current Asset": "Assets",
    "Fixed Asset": "Assets",
    "Other Asset": "Assets",
    "Accounts Payable": "Liabilities",
    "Credit Card": "Liabilities",
    "Other Current Liability": "Liabilities",
    "Long Term Liability": "Liabilities",
    "Equity": "Equity",
    "Income": "Income",
    "Other Income": "Income",
    "Cost of Goods Sold": "Expenses",
    "Expense": "Expenses",
    "Other Expense": "Expenses",
}

# Not a list of its own. This module used to keep a hand-written copy of the
# six original posting types, and when six more were added to `postings` the
# copy stayed behind -- so the new documents were counted in Shimline's
# balances and silently left out of the Beancount file. The oracle caught it as
# a balance mismatch, which is the oracle working, but the divergence should not
# have been possible. Deriving the list from the one registry means it cannot
# happen again.
POSTED_KINDS = POSTING_TYPES

_INVALID = re.compile(r"[^A-Za-z0-9]+")


class ExportError(ValueError):
    """The ledger could not be expressed as Beancount without inventing facts."""


@dataclass
class VerificationResult:
    ok: bool
    output: str
    ledger_path: str = ""
    accounts_asserted: int = 0
    transactions: int = 0
    skipped: str = ""          # set when bean-check is unavailable

    @property
    def available(self) -> bool:
        return not self.skipped


def _component(value: str) -> str:
    """Beancount components must start with a letter or digit, and hold only
    letters, digits and dashes. QBO names contain slashes, ampersands, spaces."""
    cleaned = _INVALID.sub("-", (value or "").strip()).strip("-")
    if not cleaned:
        return "Unnamed"
    if not cleaned[0].isalnum():
        cleaned = f"X{cleaned}"
    return cleaned


def account_name(account: dict) -> str:
    account_type = (account.get("AccountType") or "").strip()
    root = ACCOUNT_ROOTS.get(account_type)
    if root is None:
        raise ExportError(
            f"Account {account.get('Id')!r} has AccountType {account_type!r}, which "
            "is not mapped to a Beancount root. Add it to ACCOUNT_ROOTS rather "
            "than letting it default, because the root decides the sign.")
    return f"{root}:{_component(account_type)}:{_component(str(account.get('Id')))}-{_component(account.get('Name') or '')}"


def _txn_date(txn: dict, fallback: date) -> date:
    raw = txn.get("TxnDate")
    if not raw:
        return fallback
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return fallback


def with_derived_postings(objects: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Attach reconstructed postings so live provider objects can be exported.

    This module needs `_Postings`, which only the synthetic oracle attaches, so
    until now the differential check could only ever grade synthetic companies.
    `postings.derive_ledger` reconstructs them from real QuickBooks documents,
    which means a live client's books can be put in front of an independently
    written double-entry engine and required to survive it.

    Raises when the reconstruction is incomplete. Exporting a partial ledger
    would hand Beancount a file that is missing transactions, and the balance
    assertions would fail for a reason that has nothing to do with the books.
    """
    derived = derive_ledger(objects)
    if not derived.complete:
        raise ExportError(
            "The ledger could not be fully reconstructed, so it cannot be "
            "checked: " + "; ".join(derived.reasons()[:5]))

    enriched = dict(objects)
    for kind in POSTED_KINDS:
        rows = []
        for txn in objects.get(kind, []) or []:
            key = f"{kind}:{txn.get('Id')}"
            if txn.get("_Postings") or key not in derived.postings:
                rows.append(txn)
            else:
                rows.append({**txn, "_Postings": derived.postings[key]})
        if rows:
            enriched[kind] = rows
    return enriched


def export(objects: dict[str, list[dict]], *,
           balances: dict[str, Decimal] | None = None,
           currency: str = "CAD",
           opening: date | None = None) -> str:
    """Render the ledger as a Beancount file.

    `balances` are Shimline's claimed closing balances, keyed by QBO account id.
    They become `balance` assertions, which is what turns this from a format
    conversion into a differential check: Beancount recomputes them and refuses
    the file if its own arithmetic disagrees.
    """
    accounts = {str(item.get("Id")): item for item in objects.get("Account", [])}
    if not accounts:
        raise ExportError("The ledger holds no accounts.")

    postings_seen: list[tuple[date, str, dict]] = []
    for kind in POSTED_KINDS:
        for txn in objects.get(kind, []):
            if not txn.get("_Postings"):
                continue
            postings_seen.append((_txn_date(txn, opening or date(2026, 1, 1)), kind, txn))
    if not postings_seen:
        raise ExportError(
            "No transaction carries postings, so there is no double entry to "
            "check. This is the live-adapter shape; see work_engine.postings_derivable.")

    postings_seen.sort(key=lambda item: (item[0], str(item[2].get("Id"))))
    first_date = opening or (postings_seen[0][0] - timedelta(days=1))
    last_date = postings_seen[-1][0]

    lines = [
        ";; Generated by shimline.beancount_export -- do not edit.",
        ";;",
        ";; Shimline's own trial balance is asserted below. bean-check recomputes",
        ";; every balance independently and fails if it disagrees.",
        "",
        'option "title" "Shimline differential ledger"',
        f'option "operating_currency" "{currency}"',
        "",
    ]

    # Every account referenced by a posting must be opened, and referencing an
    # account that is not in the chart is itself worth failing on.
    referenced = {str(p["account"]) for _, _, txn in postings_seen
                  for p in txn["_Postings"]}
    unknown = sorted(referenced - accounts.keys())
    if unknown:
        raise ExportError(
            f"Postings reference accounts absent from the chart: {', '.join(unknown)}")

    for account_id in sorted(referenced | set(balances or {})):
        account = accounts.get(account_id)
        if account is None:
            raise ExportError(
                f"A balance was claimed for account {account_id!r}, which is not "
                "in the chart of accounts.")
        lines.append(f"{first_date.isoformat()} open {account_name(account)} {currency}")
    lines.append("")

    for txn_date, kind, txn in postings_seen:
        narration = str(txn.get("DocNumber") or txn.get("Id") or kind)
        lines.append(f'{txn_date.isoformat()} * "{kind}" "{narration}"')
        lines.append(f'  shimline-id: "{txn.get("Id")}"')
        for posting in txn["_Postings"]:
            amount = Decimal(str(posting.get("debit", "0") or "0")) - \
                     Decimal(str(posting.get("credit", "0") or "0"))
            name = account_name(accounts[str(posting["account"])])
            lines.append(f"  {name:<60} {amount:>14} {currency}")
        lines.append("")

    if balances:
        # A balance directive asserts the balance at the *start* of its date, so
        # it must fall after the last transaction to include it.
        assert_on = last_date + timedelta(days=1)
        lines.append(";; Shimline's claimed closing balances, asserted exactly.")
        lines.append(";;")
        lines.append(";; The `~ 0` is load-bearing. Without an explicit tolerance")
        lines.append(";; Beancount infers one from the precision of the numbers in")
        lines.append(";; the file, and a one-cent disagreement passes silently --")
        lines.append(";; verified: a 0.01 error goes undetected without it and is")
        lines.append(";; caught with it. An oracle that tolerates cent errors is")
        lines.append(";; worse than none, because it manufactures confidence.")
        for account_id, value in sorted(balances.items()):
            name = account_name(accounts[str(account_id)])
            lines.append(
                f"{assert_on.isoformat()} balance {name:<52} {value:>14} ~ 0 {currency}")
        lines.append("")

    return "\n".join(lines)


def bean_check_available() -> bool:
    return shutil.which("bean-check") is not None


def verify(objects: dict[str, list[dict]], *,
           balances: dict[str, Decimal] | None = None,
           currency: str = "CAD",
           keep: bool = False) -> VerificationResult:
    """Write the ledger and run `bean-check` over it, out of process.

    Returns a result rather than raising, so a caller can report *what*
    Beancount objected to. `skipped` is set when bean-check is not installed,
    which is expected on a production host -- it is a development tool.
    """
    text = export(objects, balances=balances, currency=currency)
    transactions = text.count(" * ")
    if not bean_check_available():
        return VerificationResult(
            ok=True, output="", transactions=transactions,
            accounts_asserted=len(balances or {}),
            skipped="bean-check is not installed; the differential check did not run")

    directory = Path(tempfile.mkdtemp(prefix="shimline-beancount-"))
    ledger = directory / "ledger.beancount"
    ledger.write_text(text, encoding="utf-8", newline="\n")
    completed = subprocess.run(
        ["bean-check", str(ledger)], capture_output=True, text=True, timeout=120)
    output = (completed.stdout + completed.stderr).strip()
    if not keep and completed.returncode == 0:
        shutil.rmtree(directory, ignore_errors=True)
        ledger_path = ""
    else:
        ledger_path = str(ledger)
    return VerificationResult(
        ok=completed.returncode == 0,
        output=output,
        ledger_path=ledger_path,
        accounts_asserted=len(balances or {}),
        transactions=transactions,
    )
