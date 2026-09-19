"""Where a number came from, and what has been checked about it.

The accountant holds the decisions. That only works if they can see why a
figure is trustworthy without taking Shimline's word for it, and fast enough
that reviewing a client is cheaper than doing the bookkeeping themselves. This
module turns what a run already recorded into that account.

The honesty that makes it worth anything
----------------------------------------
It would be easy, and wrong, to tell an accountant that three independent
engines verified their client's books. Three engines verify the *arithmetic*,
but they do not all run in the same place:

- The reconstruction runs per client, per run. Every document either becomes
  postings that balance to the cent or blocks the whole ledger.
- QuickBooks' own trial balance is compared per client, per run -- when the
  client's reports have been synced. When they have not, that check did not
  run, and saying so is the whole point.
- Beancount recomputes the same postings with an independently written
  double-entry engine. It runs in CI over the engine, not over this client's
  books, because production hosts do not install it. It is evidence that the
  code is right, not that this ledger is.

A claim about the software must never be presented as a claim about the client.
`ledger_checks` returns the first two; `engine_assurance` returns the third and
says plainly what it does and does not cover.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# What each state means to a person reading it, written once so the console and
# the working papers cannot describe the same run differently.
PASSED = "passed"
FAILED = "failed"
NOT_RUN = "not_run"


@dataclass(frozen=True)
class Check:
    name: str
    state: str            # PASSED / FAILED / NOT_RUN
    detail: str
    # What an accountant should do about it. Empty when there is nothing to do.
    action: str = ""

    @property
    def blocking(self) -> bool:
        return self.state == FAILED


@dataclass
class Provenance:
    checks: list[Check] = field(default_factory=list)
    documents: dict[str, int] = field(default_factory=dict)
    accounts: int = 0
    statement_accounts: dict[str, dict] = field(default_factory=dict)

    @property
    def trustworthy(self) -> bool:
        """No check failed. Deliberately not 'every check passed'.

        A check that did not run is not a failure -- a client who has never
        synced their QuickBooks reports has not done anything wrong. It is also
        not a pass, which is why `passed` and `not_run` are separate states and
        the console shows both.
        """
        return not any(check.blocking for check in self.checks)

    @property
    def ran(self) -> int:
        return sum(1 for check in self.checks if check.state != NOT_RUN)

    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.blocking]


def ledger_checks(coverage: dict) -> Provenance:
    """What was verified about *this client's* ledger on *this run*."""
    coverage = coverage or {}
    result = Provenance(
        documents={key: value for key, value in
                   (coverage.get("objects_loaded") or {}).items() if value},
        accounts=len(coverage.get("account_ids") or []),
        statement_accounts=dict(coverage.get("statement_matching") or {}))

    result.checks.append(_reconstruction_check(coverage))
    result.checks.append(_provider_check(coverage.get("provider_agreement") or {}))
    result.checks.append(_statement_check(coverage))
    return result


def _reconstruction_check(coverage: dict) -> Check:
    """Did every document become postings that balance?

    Read from the check registry rather than recomputed. `04` is the
    reconciliation check and it is the one that reports `derived_ledger_postings`
    as its missing source when the reconstruction refused, so its status is the
    honest signal without deriving the ledger a second time here.
    """
    checks = {item.get("id"): item for item in (coverage.get("checks") or [])}
    reconciliation = checks.get("04") or {}
    missing = str(reconciliation.get("missing_source") or "")
    if missing == "derived_ledger_postings":
        return Check(
            "Ledger rebuilt from the client's documents", FAILED,
            "At least one document could not be turned into postings that "
            "balance, so no balance was computed from it at all.",
            "Open the run and read the reason; a partial rebuild is refused on "
            "purpose rather than producing a plausible balance.")
    if not coverage.get("objects_loaded"):
        return Check("Ledger rebuilt from the client's documents", NOT_RUN,
                     "No provider objects were loaded on this run.", "")
    return Check(
        "Ledger rebuilt from the client's documents", PASSED,
        "Every document became postings that balance to the cent. A document "
        "that could not be rebuilt would have blocked the whole ledger.")


def _provider_check(agreement: dict) -> Check:
    status = str(agreement.get("status") or "not_supplied")
    name = "QuickBooks' own trial balance agrees"
    if status == "agrees":
        compared = agreement.get("accounts_compared") or 0
        return Check(name, PASSED,
                     f"All {compared} account balances match exactly, with no "
                     "tolerance. Two readings of the same ledger, one of them "
                     "QuickBooks' own.")
    if status == "disagrees":
        differences = agreement.get("differences") or []
        first = differences[0] if differences else {}
        detail = (f"{len(differences)} account(s) differ. "
                  f"First: {first.get('account', '?')} — {first.get('reason', '')}")
        return Check(name, FAILED, detail,
                     "This is a gap on Shimline's side, not a defect in the "
                     "books. Nothing that depends on the ledger is reported "
                     "until it is resolved.")
    if status == "unreadable":
        return Check(name, FAILED,
                     f"The trial balance could not be read: {agreement.get('reason', '')}",
                     "Re-sync the client's QuickBooks reports.")
    if status == "not_compared":
        return Check(name, NOT_RUN,
                     "The ledger could not be rebuilt, so there was nothing to "
                     "compare against.", "")
    return Check(name, NOT_RUN,
                 "This client's QuickBooks reports have not been synced, so the "
                 "second reading was not available.",
                 "Pull the client's reports to turn this check on.")


def _statement_check(coverage: dict) -> Check:
    name = "The bank statement accounts for the ledger"
    accounts = coverage.get("statement_matching") or {}
    if not accounts:
        return Check(name, NOT_RUN,
                     "No bank statement was supplied, or none has been mapped "
                     "to a ledger account.",
                     "Upload the period's statement and map it to its account.")
    blocked = [key for key, item in accounts.items()
               if item.get("status") == "blocked"]
    if blocked:
        return Check(name, FAILED,
                     f"Matching could not run for account(s) {', '.join(sorted(blocked))}.",
                     "Open the run for the reason.")
    unmatched = sum(int(item.get("unmatched_bank") or 0) for item in accounts.values())
    ambiguous = sum(int(item.get("ambiguous") or 0) for item in accounts.values())
    matched = sum(int(item.get("matched") or 0) for item in accounts.values())
    if unmatched or ambiguous:
        parts = []
        if unmatched:
            parts.append(f"{unmatched} statement line(s) nothing in the books accounts for")
        if ambiguous:
            parts.append(f"{ambiguous} line(s) that fit more than one entry equally well")
        return Check(name, FAILED, f"{matched} matched; " + "; ".join(parts) + ".",
                     "The ambiguous ones need a person: two candidates fitted "
                     "equally well and neither was guessed at.")
    return Check(name, PASSED,
                 f"All {matched} statement line(s) matched a ledger entry.")


@dataclass(frozen=True)
class Assurance:
    name: str
    covers: str
    does_not_cover: str


def engine_assurance() -> Assurance:
    """What is true of the software, stated as being about the software.

    Presenting this as a verification of the client's books would be the single
    most tempting lie available in this product, and the one an accountant would
    be most entitled to be angry about.
    """
    return Assurance(
        name="Independently recomputed double entry",
        # "Every release" and not "every build", because that is the gate that
        # actually exists. This sentence was not true when it was written: the
        # release gate ran the oracle on an interpreter with no beancount wheel,
        # so those tests skipped and the gate passed without them -- and the
        # GitHub workflow that would have caught it has never run, because the
        # repository has no remote. The gate now refuses to deploy unless the
        # oracle ran. Do not widen this wording past what a gate enforces.
        covers=("The engine that rebuilds these postings is checked against "
                "Beancount, a separately written double-entry implementation, "
                "on every release. Beancount recomputes every balance by its own "
                "rules and the release is refused if it disagrees by a cent."),
        does_not_cover=("This runs over Shimline's engine, not over this "
                        "client's ledger: it is evidence that the arithmetic is "
                        "implemented correctly, not that these particular books "
                        "are right. The checks listed against each client above "
                        "are the ones that ran on that client."))


# --------------------------------------------------------- finding evidence --

# The evidence tags the engine writes, and what each means to a reader. A tag
# with no entry here is shown verbatim rather than dropped: an unexplained
# reference is a smaller problem than a silently missing one.
_EVIDENCE_LABELS = {
    "qbo": "QuickBooks {rest}",
    "document": "Source document {rest}",
    "statement": "Statement transaction {rest}",
    "statement_line": "Bank statement line {rest}",
    "bank_statement": "The bank statement for the period",
    "confirmation": "Client confirmation {rest}",
}


def evidence_lines(evidence) -> list[str]:
    """Turn a finding's evidence references into something readable."""
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except (TypeError, ValueError):
            return [evidence] if evidence else []
    lines = []
    for item in evidence or []:
        text = str(item)
        prefix, _, rest = text.partition(":")
        template = _EVIDENCE_LABELS.get(prefix)
        lines.append(template.format(rest=rest) if template else text)
    return lines
