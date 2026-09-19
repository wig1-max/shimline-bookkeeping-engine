"""Portfolio reporting projection over the canonical bookkeeping tables.

The work engine is defect-shaped: one record per problem, with an evidence
gate and a write proposal. The client-facing Cash-Leak Review is
portfolio-shaped: aging buckets, per-project margin, health scores, one
headline exposure number. Both questions are worth asking and neither answer
substitutes for the other.

What they share is the substrate. ``work_engine.persist_canonical`` already
shreds a provider pull into ``bookkeeping_transactions``,
``bookkeeping_transaction_lines`` and their dimensions. This module reads
those tables and nothing else. One ingest, two projections -- so a detector
added to the engine and a number shown to a client can never drift onto
different copies of the ledger.

The output is the ``findings.json`` contract that ``scripts/build_report.py``
already renders, plus an ``unavailable`` list. That list is the point of the
module as much as the numbers are: a figure that cannot be computed from the
connected file is omitted and named, never defaulted to zero. A zero and an
unknown look identical in a PDF and mean opposite things.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .work_engine import money

# Transactions that carry cost. Journal entries can also carry job cost in a
# real file; v0 does not read them here, and reports that limit when a run
# contains project-tagged journal lines.
COST_TYPES = ("Bill", "Purchase")
REVENUE_TYPES = ("Invoice",)
COST_ACCOUNT_TYPES = {"Cost of Goods Sold", "Expense", "Other Expense"}

AGING_BUCKETS = ("0-30", "31-60", "61-90", "90+")


@dataclass
class ReportConfig:
    """Rules a reviewer should be able to see and change without editing code."""

    #: Days of tolerance for calling two same-vendor, same-amount charges a
    #: likely duplicate.
    duplicate_window_days: int = 5
    #: A project is flagged when its margin is this many points below the
    #: portfolio average.
    low_margin_gap_points: Decimal = Decimal("15")
    #: Aging runs from the invoice date, not the due date. That is what the
    #: published report has always measured and what the health scores are
    #: calibrated against. QuickBooks' own A/R Aging runs from the due date,
    #: so the two will not agree.
    aging_from: str = "transaction_date"


@dataclass
class Unavailable:
    """One figure that could not be computed, and what would unblock it."""

    field: str
    status: str  # 'blocked' (a source is missing) or 'manual' (v0 does not compute it)
    reason: str
    missing_source: str | None = None

    def as_dict(self) -> dict:
        out = {"field": self.field, "status": self.status, "reason": self.reason}
        if self.missing_source:
            out["missing_source"] = self.missing_source
        return out


@dataclass
class _Ledger:
    transactions: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    projects: list = field(default_factory=list)


def _load(conn, run_id: str) -> _Ledger:
    transactions = conn.execute(
        "SELECT id, provider_type, provider_id, transaction_date, document_number, "
        "total_amount, open_balance, entity_id, status "
        "FROM bookkeeping_transactions WHERE run_id=?", (run_id,)).fetchall()
    lines = conn.execute(
        "SELECT l.id, l.transaction_id, l.description, l.amount, l.debit, l.credit, "
        "l.unit_price, l.quantity, l.project_id, l.account_id, "
        "t.provider_type, t.transaction_date, t.entity_id, "
        "a.account_type "
        "FROM bookkeeping_transaction_lines l "
        "JOIN bookkeeping_transactions t ON t.id = l.transaction_id "
        "LEFT JOIN bookkeeping_accounts a ON a.id = l.account_id "
        "WHERE t.run_id=?", (run_id,)).fetchall()
    organization = conn.execute(
        "SELECT organization_id FROM bookkeeping_runs WHERE id=?", (run_id,)).fetchone()
    projects = []
    if organization:
        projects = conn.execute(
            "SELECT p.id, p.provider_id, p.name, p.active, e.display_name customer "
            "FROM bookkeeping_projects p "
            "LEFT JOIN bookkeeping_entities e ON e.id = p.customer_id "
            "WHERE p.organization_id=?", (organization[0],)).fetchall()
    return _Ledger(transactions, lines, projects)


def _is_cost_line(line) -> bool:
    if line["provider_type"] not in COST_TYPES:
        return False
    account_type = line["account_type"]
    # An unmapped account is still a cost line on a bill or an expense; the
    # transaction type already established that. Only an explicitly non-cost
    # account excludes it.
    return account_type is None or account_type in COST_ACCOUNT_TYPES


def _is_stock_line(line) -> bool:
    """A unit-priced catalogue line is a stock purchase, not a job cost.

    QuickBooks records a bundle of lumber bought against the item list
    differently from a delivery booked straight to a job. Until such a line is
    issued to a job it is inventory, so it is excluded from "materials not
    allocated to a job" -- otherwise every stock purchase would be reported as
    a costing failure. It is still counted in total vendor spend, and it is
    the only source for price-movement analysis.
    """
    return line["unit_price"] is not None


def _round(value: Decimal) -> float:
    return float(round(value, 2))


def portfolio(conn, run_id: str, *, as_of: date,
              config: ReportConfig | None = None) -> dict:
    """Project the canonical tables for one run into the report contract."""
    config = config or ReportConfig()
    ledger = _load(conn, run_id)
    findings: dict = {}
    unavailable: list[Unavailable] = []

    by_id = {row["id"]: row for row in ledger.transactions}

    # 1. A/R aging -----------------------------------------------------
    buckets = {name: Decimal("0") for name in AGING_BUCKETS}
    total_ar = Decimal("0")
    for txn in ledger.transactions:
        if txn["provider_type"] not in REVENUE_TYPES or txn["open_balance"] is None:
            continue
        balance = money(txn["open_balance"])
        if balance <= 0:
            continue
        days = (as_of - date.fromisoformat(txn["transaction_date"])).days
        total_ar += balance
        if days <= 30:
            buckets["0-30"] += balance
        elif days <= 60:
            buckets["31-60"] += balance
        elif days <= 90:
            buckets["61-90"] += balance
        else:
            buckets["90+"] += balance
    findings["ar_aging"] = {name: _round(value) for name, value in buckets.items()}
    findings["ar_total_outstanding"] = _round(total_ar)
    findings["ar_over_90"] = _round(buckets["90+"])
    findings["ar_invoice_count"] = sum(
        1 for txn in ledger.transactions
        if txn["provider_type"] in REVENUE_TYPES and txn["open_balance"] is not None
        and money(txn["open_balance"]) > 0)

    # 2. Unapplied customer payments ------------------------------------
    payments = [t for t in ledger.transactions if t["provider_type"] == "Payment"]
    if payments and all(t["open_balance"] is None for t in payments):
        # The provider did not state how much of each payment is unapplied.
        # Reporting zero here would say "all cash is applied", which is a
        # different claim from "we could not tell".
        findings["unapplied_payments_total"] = None
        findings["unapplied_payments_count"] = None
        unavailable.append(Unavailable(
            "unapplied_payments", "blocked",
            "No payment in this pull carries an unapplied amount, so applied "
            "and unapplied cash cannot be told apart.",
            "Payment.UnappliedAmt"))
    else:
        unapplied = [t for t in payments
                     if t["open_balance"] is not None and money(t["open_balance"]) > 0]
        findings["unapplied_payments_total"] = _round(
            sum((money(t["open_balance"]) for t in unapplied), Decimal("0")))
        findings["unapplied_payments_count"] = len(unapplied)

    # 3. Costs not allocated to any job ---------------------------------
    unassigned = [line for line in ledger.lines
                  if _is_cost_line(line) and not _is_stock_line(line)
                  and line["project_id"] is None]
    findings["unassigned_job_costs_total"] = _round(
        sum((money(line["amount"]) for line in unassigned), Decimal("0")))
    findings["unassigned_job_costs_count"] = len(unassigned)

    # 4. Duplicate or strange vendor charges -----------------------------
    # Same vendor, same amount, within the configured window. Stock purchases
    # are excluded: a standing order for the same bundle at the same price is
    # normal, not a duplicate.
    stock_transactions = {line["transaction_id"] for line in ledger.lines if _is_stock_line(line)}
    by_vendor: dict = defaultdict(list)
    for txn in ledger.transactions:
        if txn["provider_type"] not in COST_TYPES or txn["id"] in stock_transactions:
            continue
        by_vendor[txn["entity_id"]].append(txn)
    duplicate_total = Decimal("0")
    duplicate_count = 0
    for vendor_transactions in by_vendor.values():
        ordered = sorted(vendor_transactions,
                         key=lambda t: (t["transaction_date"], t["provider_id"]))
        for first, second in zip(ordered, ordered[1:]):
            if money(first["total_amount"]) != money(second["total_amount"]):
                continue
            gap = (date.fromisoformat(second["transaction_date"])
                   - date.fromisoformat(first["transaction_date"])).days
            if abs(gap) <= config.duplicate_window_days:
                duplicate_total += money(second["total_amount"])
                duplicate_count += 1
    findings["duplicate_vendor_charges_total"] = _round(duplicate_total)
    findings["duplicate_vendor_charges_count"] = duplicate_count

    # 5. Project profitability -------------------------------------------
    revenue: dict = defaultdict(Decimal)
    cost: dict = defaultdict(Decimal)
    for line in ledger.lines:
        if line["project_id"] is None:
            continue
        if line["provider_type"] in REVENUE_TYPES:
            revenue[line["project_id"]] += money(line["amount"])
        elif line["provider_type"] in COST_TYPES:
            cost[line["project_id"]] += money(line["amount"])

    rows = []
    for project in ledger.projects:
        project_revenue = revenue.get(project["id"], Decimal("0"))
        project_cost = cost.get(project["id"], Decimal("0"))
        margin = (round((project_revenue - project_cost) / project_revenue * 100, 1)
                  if project_revenue else Decimal("0"))
        rows.append({
            "project_id": project["provider_id"],
            "project_name": project["name"],
            "customer": project["customer"],
            "revenue": _round(project_revenue),
            "cost": _round(project_cost),
            "margin_pct": float(margin),
            # QuickBooks marks a customer:job active or inactive; it carries no
            # "completed" state, so a closed job is one that has been made
            # inactive. A firm that never deactivates finished jobs shows
            # everything as active.
            "status": "active" if project["active"] else "completed",
        })
    rows.sort(key=lambda r: r["margin_pct"])
    findings["projects"] = rows

    if not rows:
        _no_margins(findings, unavailable, Unavailable(
            "project_profitability", "blocked",
            "Project profitability unavailable: no projects configured in QuickBooks.",
            "customer:job records"))
    elif not any(r["revenue"] for r in rows):
        _no_margins(findings, unavailable, Unavailable(
            "project_profitability", "blocked",
            "Projects exist but no invoice line is tagged to one, so no job "
            "has revenue and margin cannot be computed.",
            "project-tagged invoice lines"))
    else:
        margins = [Decimal(str(r["margin_pct"])) for r in rows]
        average = round(sum(margins) / len(margins), 1)
        findings["portfolio_avg_margin_pct"] = float(average)
        threshold = average - config.low_margin_gap_points
        findings["low_margin_projects"] = [r for r in rows
                                           if Decimal(str(r["margin_pct"])) < threshold]
        if findings["low_margin_projects"]:
            worst = rows[0]
            benchmark_cost = money(worst["revenue"]) * (1 - average / 100)
            findings["worst_project"] = worst
            findings["worst_project_margin_gap"] = _round(money(worst["cost"]) - benchmark_cost)
        else:
            findings["worst_project"] = None
            findings["worst_project_margin_gap"] = 0

    # 6. Vendor / material price movement ---------------------------------
    series: dict = defaultdict(list)
    for line in ledger.lines:
        if _is_stock_line(line) and line["description"]:
            series[line["description"]].append(line)
    priced = {name: observed for name, observed in series.items() if len(observed) >= 2}
    if priced:
        name, observations = max(priced.items(), key=lambda item: (len(item[1]), item[0]))
        ordered = sorted(observations, key=lambda line: line["transaction_date"])
        first_price = money(ordered[0]["unit_price"])
        last_price = money(ordered[-1]["unit_price"])
        vendor_row = by_id.get(ordered[0]["transaction_id"])
        vendor_name = None
        if vendor_row is not None and vendor_row["entity_id"]:
            found = conn.execute("SELECT display_name FROM bookkeeping_entities WHERE id=?",
                                 (vendor_row["entity_id"],)).fetchone()
            vendor_name = found[0] if found else None
        findings["vendor_price_variance"] = {
            "line_item": name,
            "vendor": vendor_name,
            "first_price": float(first_price),
            "last_price": float(last_price),
            "pct_change": (float(round((last_price - first_price) / first_price * 100, 1))
                           if first_price else None),
        }
    else:
        findings["vendor_price_variance"] = None
        unavailable.append(Unavailable(
            "vendor_price_variance", "blocked",
            "No repeated unit-priced item line was found, so there is no price "
            "history to compare against.",
            "item-based bill lines with a unit price"))

    # 7. Total exposure headline ------------------------------------------
    components = {
        "ar_over_90": findings["ar_over_90"],
        "unassigned_job_costs_total": findings["unassigned_job_costs_total"],
        "duplicate_vendor_charges_total": findings["duplicate_vendor_charges_total"],
        "unapplied_payments_total": findings["unapplied_payments_total"],
        "worst_project_margin_gap": findings["worst_project_margin_gap"],
    }
    omitted = sorted(name for name, value in components.items() if value is None)
    findings["total_exposure"] = _round(
        sum((money(value) for value in components.values() if value is not None), Decimal("0")))
    if omitted:
        # The headline is still worth showing, but it is a floor rather than a
        # total, and it must say which components are missing from it.
        unavailable.append(Unavailable(
            "total_exposure", "blocked",
            "Exposure is a partial total: " + ", ".join(omitted)
            + " could not be computed and are excluded."))

    # A limit that is worth stating even though nothing here is wrong.
    journal_job_lines = sum(1 for line in ledger.lines
                            if line["provider_type"] == "JournalEntry" and line["project_id"])
    if journal_job_lines:
        unavailable.append(Unavailable(
            "journal_entry_job_cost", "manual",
            f"{journal_job_lines} journal-entry line(s) are tagged to a job. "
            "Project cost here counts bills and expenses only, so those "
            "amounts are not in the margins above."))

    findings["health_scores"] = _health_scores(findings, ledger, total_ar, unavailable)
    findings["unavailable"] = [item.as_dict() for item in unavailable]
    findings["as_of"] = as_of.isoformat()
    return findings


def _no_margins(findings: dict, unavailable: list, entry) -> None:
    findings["portfolio_avg_margin_pct"] = None
    findings["low_margin_projects"] = []
    findings["worst_project"] = None
    findings["worst_project_margin_gap"] = None
    unavailable.append(entry)


def _health_scores(findings: dict, ledger: _Ledger, total_ar: Decimal,
                   unavailable: list) -> dict | None:
    """The published scoring formulas, unchanged, over the canonical ledger.

    Every score is a documented arithmetic function of numbers already in the
    report. A score whose inputs are unavailable is not emitted at all.
    """
    blocked = {item.field for item in unavailable}
    if "project_profitability" in blocked or "unapplied_payments" in blocked:
        unavailable.append(Unavailable(
            "health_scores", "blocked",
            "Health scores are a function of the receivables, job-costing and "
            "reconciliation figures; at least one of those is unavailable."))
        return None

    def score(penalty: Decimal, weight: Decimal, floor: int = 0) -> int:
        return max(floor, int(round(100 - penalty * weight)))

    ar_over_90 = money(findings["ar_over_90"])
    pct_ar_over90 = (ar_over_90 / total_ar * 100) if total_ar else Decimal("0")
    receivables = score(pct_ar_over90, Decimal("1.75"))

    # Job costing reliability is a process-integrity question as much as a
    # dollar-ratio one: a handful of untracked cost lines undermines trust in
    # every project's margin even when the dollars involved are modest, so
    # each instance carries a flat penalty on top of the dollar-weighted one.
    total_project_cost = sum((money(row["cost"]) for row in findings["projects"]), Decimal("0"))
    unassigned_total = money(findings["unassigned_job_costs_total"])
    denominator = unassigned_total + total_project_cost
    pct_unassigned = (unassigned_total / denominator * 100) if denominator else Decimal("0")
    job_costing = max(30, int(round(
        100 - findings["unassigned_job_costs_count"] * 5 - pct_unassigned * 2)))

    vendor_spend = sum((money(t["total_amount"]) for t in ledger.transactions
                        if t["provider_type"] in COST_TYPES), Decimal("0"))
    duplicate_total = money(findings["duplicate_vendor_charges_total"])
    pct_duplicate = (duplicate_total / vendor_spend * 100) if vendor_spend else Decimal("0")
    expense_classification = max(40, int(round(
        100 - findings["duplicate_vendor_charges_count"] * 8 - pct_duplicate * 2)))

    # Management reporting is only as trustworthy as the job-costing and A/R
    # data it is built on, so it is modelled as a knock-on of those two scores
    # rather than an independent measure.
    management_reporting = max(30, int(round(Decimal(job_costing + receivables) / 2 - 5)))

    payment_count = sum(1 for t in ledger.transactions if t["provider_type"] == "Payment")
    if payment_count:
        unapplied_rate = Decimal(findings["unapplied_payments_count"]) / payment_count * 100
        reconciliation = max(70, int(round(100 - unapplied_rate * 3)))
    else:
        reconciliation = 100

    scores = {
        "Reconciliation": reconciliation,
        "Receivables": receivables,
        "Job costing": job_costing,
        "Expense classification": expense_classification,
        "Management reporting": management_reporting,
    }
    scores["Total"] = int(round(Decimal(sum(scores.values())) / len(scores)))
    return scores
