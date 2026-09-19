"""One queue across every client a firm holds.

An accountant with thirty clients does not want thirty dashboards. They want to
know where their next hour goes. So this is ordered by what costs them time,
not by what is easiest to compute:

1. **Decisions waiting.** Work they can finish right now, and the only state
   that turns into revenue. Ordered within itself by the money at stake.
2. **Waiting on the client.** Someone has to chase, and the longer it sits the
   worse it gets.
3. **Waiting on us.** Shimline could not verify something. It is not the
   accountant's problem to fix, but they must be able to see it rather than
   wonder why a client looks quiet.
4. **Clean.** Reviewed, nothing outstanding. Worth showing, because "nothing to
   do here" is the answer that makes the other rows credible.
5. **Never scanned.** A client the firm holds and Shimline has not looked at.

The ordering is a product decision and is stated here rather than buried in an
ORDER BY, because it is the thing most likely to need changing once a real firm
uses it.

Everything is scoped. `rows` takes a `tenancy.Scope` and composes it into the
query, so a queue cannot be built without deciding whose queue it is.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from . import provenance, tenancy

NEEDS_DECISION = "needs_decision"
WAITING_ON_CLIENT = "waiting_on_client"
WAITING_ON_US = "waiting_on_us"
CLEAN = "clean"
NEVER_SCANNED = "never_scanned"

# Lower sorts first. The gaps are deliberate: a state inserted later should not
# require renumbering the others.
STATE_ORDER = {
    NEEDS_DECISION: 10,
    WAITING_ON_CLIENT: 20,
    WAITING_ON_US: 30,
    CLEAN: 40,
    NEVER_SCANNED: 50,
}

STATE_LABELS = {
    NEEDS_DECISION: "Needs a decision",
    WAITING_ON_CLIENT: "Waiting on the client",
    WAITING_ON_US: "Waiting on Shimline",
    CLEAN: "Clean",
    NEVER_SCANNED: "Not yet scanned",
}

# Proposal states that are sitting in front of a person.
AWAITING_DECISION = ("proposed", "reviewed")


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


@dataclass
class ClientRow:
    organization_id: str
    name: str
    run_id: str | None = None
    period_start: str = ""
    period_end: str = ""
    scanned_at: str = ""
    decisions_waiting: int = 0
    findings_open: int = 0
    evidence_requests: int = 0
    exposure: Decimal = Decimal("0")
    checks_blocked: int = 0
    blocked_on: list[str] = field(default_factory=list)
    verification: provenance.Provenance | None = None
    # A GST/HST return waiting to be checked and signed, or the reason there is
    # not one. A filing deadline is the one thing on this queue that costs money
    # if it slips, so it outranks everything else a client might be waiting on.
    gst_period: str = ""
    gst_due: str = ""
    gst_filable: bool = False
    gst_blocked: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        if not self.run_id:
            return NEVER_SCANNED
        if self.decisions_waiting or (self.gst_period and self.gst_filable):
            return NEEDS_DECISION
        if self.verification and self.verification.failures():
            return WAITING_ON_US
        if self.gst_blocked:
            return WAITING_ON_US
        if self.evidence_requests or self.blocked_on:
            return WAITING_ON_CLIENT
        return CLEAN

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def trustworthy(self) -> bool:
        return bool(self.verification and self.verification.trustworthy)

    def sort_key(self) -> tuple:
        # Within a state, the larger exposure first, then the older period: an
        # accountant chasing a quarter-end has a harder conversation than one
        # chasing last week.
        return (STATE_ORDER[self.state], -self.exposure, self.period_end or "",
                self.name.lower())


@dataclass
class Portfolio:
    rows: list[ClientRow] = field(default_factory=list)
    firm_id: str | None = None
    firm_name: str = ""

    def by_state(self, state: str) -> list[ClientRow]:
        return [row for row in self.rows if row.state == state]

    def counts(self) -> dict[str, int]:
        return {state: len(self.by_state(state)) for state in STATE_ORDER}

    @property
    def decisions_waiting(self) -> int:
        return sum(row.decisions_waiting for row in self.rows)

    @property
    def exposure(self) -> Decimal:
        return sum((row.exposure for row in self.rows), Decimal("0"))


def build(conn: sqlite3.Connection, scope: tenancy.Scope) -> Portfolio:
    """The queue for one scope. Empty for a scope that sees nothing."""
    portfolio = Portfolio(firm_id=scope.firm_id)
    if scope.firm_id:
        row = conn.execute("SELECT name FROM firms WHERE id=?",
                           (scope.firm_id,)).fetchone()
        portfolio.firm_name = row[0] if row else ""

    visible, params = tenancy.and_where(scope, "o.id")
    clients = conn.execute(
        "SELECT o.id, o.name FROM organizations o "
        "WHERE o.lifecycle_stage NOT IN ('lost','offboarded')" + visible
        + " ORDER BY o.name", params).fetchall()

    for client in clients:
        portfolio.rows.append(_row_for(conn, str(client[0]), str(client[1])))
    portfolio.rows.sort(key=ClientRow.sort_key)
    return portfolio


def _row_for(conn: sqlite3.Connection, organization_id: str, name: str) -> ClientRow:
    row = ClientRow(organization_id=organization_id, name=name)

    # The newest run, because a queue describes now. Older runs stay readable
    # from the client page; showing them here would double-count the work.
    latest = conn.execute(
        "SELECT id, period_start, period_end, coverage_json, started_at "
        "FROM bookkeeping_runs WHERE organization_id=? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (organization_id,)).fetchone()
    if not latest:
        return row

    row.run_id = str(latest[0])
    row.period_start = str(latest[1] or "")
    row.period_end = str(latest[2] or "")
    row.scanned_at = str(latest[4] or "")

    try:
        coverage = json.loads(latest[3] or "{}")
    except (TypeError, ValueError):
        coverage = {}
    row.verification = provenance.ledger_checks(coverage)
    blocked = [item for item in (coverage.get("checks") or [])
               if item.get("status") == "blocked"]
    row.checks_blocked = len(blocked)
    # What the client has to send, de-duplicated: four checks blocked on one
    # missing statement is one errand, not four.
    row.blocked_on = sorted({str(item.get("missing_source") or "").strip()
                             for item in blocked
                             if item.get("missing_source")})

    awaiting = ",".join("?" for _ in AWAITING_DECISION)
    row.decisions_waiting = conn.execute(
        f"SELECT COUNT(*) FROM bookkeeping_proposals WHERE run_id=? "
        f"AND status IN ({awaiting})",
        (row.run_id, *AWAITING_DECISION)).fetchone()[0]

    # Summed in Python rather than in SQL. SQLite has no decimal type, so
    # SUM(CAST(... AS REAL)) would add a client's exposure in binary floating
    # point -- the one thing this codebase does not do with money.
    effects = conn.execute(
        "SELECT financial_effect FROM bookkeeping_findings WHERE run_id=? "
        "AND status NOT IN ('resolved','dismissed')", (row.run_id,)).fetchall()
    row.findings_open = len(effects)
    row.exposure = sum((abs(_money(item[0])) for item in effects), Decimal("0"))

    _attach_gst(conn, row)

    row.evidence_requests = conn.execute(
        "SELECT COUNT(*) FROM bookkeeping_evidence_requests q "
        "JOIN bookkeeping_findings f ON f.id = q.finding_id "
        "WHERE f.run_id=? AND q.status='open'", (row.run_id,)).fetchone()[0]
    return row


def _attach_gst(conn: sqlite3.Connection, row: ClientRow) -> None:
    """The most recently prepared GST/HST return, if one has been.

    Read from what was stored rather than recomputed here. The queue is loaded
    for every client on every page view, and preparing a return walks a quarter
    of transactions; doing that thirty times to draw a list would make the one
    page an accountant lives on the slowest thing in the product.

    A client with no filing arrangement recorded contributes nothing to the
    queue. That is an ordinary state for a new engagement, not a problem, and
    showing it as one would train an accountant to ignore the column.
    """
    stored = conn.execute(
        "SELECT period_start, period_end, due_at, filable, blocked_json "
        "FROM bookkeeping_gst_returns WHERE organization_id=? "
        "ORDER BY period_end DESC LIMIT 1",
        (row.organization_id,)).fetchone()
    if not stored:
        return
    row.gst_period = f"{stored[0]} to {stored[1]}"
    row.gst_due = str(stored[2] or "")
    row.gst_filable = bool(stored[3])
    try:
        row.gst_blocked = list(json.loads(stored[4] or "[]"))
    except (TypeError, ValueError):
        row.gst_blocked = []
