"""
The actual Cash-Leak Review diagnostic engine.

Reads the CSVs in ../data/ (synthetic today; a real client's QuickBooks
Online exports tomorrow -- same column shapes) and computes every finding
in the report from the underlying transactions. Nothing in the sample
report is hand-typed; it all comes out of this script.

This is no longer the live path. A connected client's report is produced by
`shimline.reporting.portfolio`, which computes the same contract from the
canonical bookkeeping tables instead of from CSVs -- see
docs/REPORTING_PROJECTION.md.

Do not delete this file. It is the reference implementation for that
projection's acceptance oracle: `backend/test_reporting.py` runs it over
`data/*.csv` and asserts the projection reproduces its answer key for key. It
also still regenerates the published sample report.

Outputs: ../data/findings.json
"""
import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

DATA = Path(__file__).parent.parent / "data"


def read_csv(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def analyze():
    projects = read_csv("projects.csv")
    invoices = read_csv("invoices.csv")
    payments = read_csv("payments.csv")
    vendor_txns = read_csv("vendor_transactions.csv")

    findings = {}

    # 1. A/R aging -----------------------------------------------------
    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    total_ar = 0.0
    for inv in invoices:
        if inv["status"] != "unpaid":
            continue
        days = int(inv["days_outstanding"])
        amt = to_num(inv["amount"])
        total_ar += amt
        if days <= 30:
            buckets["0-30"] += amt
        elif days <= 60:
            buckets["31-60"] += amt
        elif days <= 90:
            buckets["61-90"] += amt
        else:
            buckets["90+"] += amt
    findings["ar_aging"] = buckets
    findings["ar_total_outstanding"] = round(total_ar, 2)
    findings["ar_over_90"] = round(buckets["90+"], 2)

    # 2. Unapplied customer payments ------------------------------------
    unapplied = [p for p in payments if not p.get("applied_to_invoice")]
    findings["unapplied_payments_total"] = round(sum(to_num(p["amount"]) for p in unapplied), 2)
    findings["unapplied_payments_count"] = len(unapplied)

    # 3. Materials/costs not allocated to any project -------------------
    unassigned = [t for t in vendor_txns if not t.get("project_id")
                  and t.get("category") in ("Materials", "Labour", "Subcontractor")
                  and not t.get("line_item")]  # exclude the price-variance series
    findings["unassigned_job_costs_total"] = round(sum(to_num(t["amount"]) for t in unassigned), 2)
    findings["unassigned_job_costs_count"] = len(unassigned)

    # 4. Duplicate/strange vendor charges --------------------------------
    # same vendor + same amount within a 5-day window = flagged as likely duplicate
    by_vendor = defaultdict(list)
    for t in vendor_txns:
        if t.get("line_item"):
            continue
        by_vendor[t["vendor"]].append(t)
    duplicates = []
    for vendor, txns in by_vendor.items():
        txns_sorted = sorted(txns, key=lambda t: t["date"])
        for i in range(len(txns_sorted) - 1):
            a, b = txns_sorted[i], txns_sorted[i + 1]
            if to_num(a["amount"]) == to_num(b["amount"]):
                d1 = date.fromisoformat(a["date"])
                d2 = date.fromisoformat(b["date"])
                if abs((d2 - d1).days) <= 5:
                    duplicates.append((a, b))
    # also flag any small residual "duplicate-family" transactions manually planted
    dup_total = sum(to_num(b["amount"]) for _, b in duplicates)
    findings["duplicate_vendor_charges_total"] = round(dup_total, 2)
    findings["duplicate_vendor_charges_count"] = len(duplicates)

    # 5. Project profitability -------------------------------------------
    proj_rows = []
    margins = []
    for p in projects:
        rev = to_num(p["revenue"])
        cost = to_num(p["actual_cost"])
        margin_pct = round((rev - cost) / rev * 100, 1) if rev else 0
        margins.append(margin_pct)
        proj_rows.append({
            "project_id": p["project_id"], "project_name": p["project_name"],
            "customer": p["customer"], "revenue": rev, "cost": cost,
            "margin_pct": margin_pct, "status": p["status"],
        })
    avg_margin = round(sum(margins) / len(margins), 1) if margins else 0
    findings["projects"] = sorted(proj_rows, key=lambda r: r["margin_pct"])
    findings["portfolio_avg_margin_pct"] = avg_margin
    low_margin_threshold = avg_margin - 15  # flag anything materially below the portfolio average
    findings["low_margin_projects"] = [r for r in proj_rows if r["margin_pct"] < low_margin_threshold]

    # margin gap $ for the worst project vs portfolio average (comparable-work benchmark)
    if findings["low_margin_projects"]:
        worst = min(proj_rows, key=lambda r: r["margin_pct"])
        benchmark_cost = worst["revenue"] * (1 - avg_margin / 100)
        margin_gap = round(worst["cost"] - benchmark_cost, 2)
        findings["worst_project"] = worst
        findings["worst_project_margin_gap"] = margin_gap
    else:
        findings["worst_project"] = None
        findings["worst_project_margin_gap"] = 0

    # 6. Vendor / material price variance --------------------------------
    price_series = [t for t in vendor_txns if t.get("line_item")]
    variance = None
    if price_series:
        price_series_sorted = sorted(price_series, key=lambda t: t["date"])
        first_price = to_num(price_series_sorted[0]["unit_price"])
        last_price = to_num(price_series_sorted[-1]["unit_price"])
        pct_change = round((last_price - first_price) / first_price * 100, 1)
        variance = {
            "line_item": price_series_sorted[0]["line_item"],
            "vendor": price_series_sorted[0]["vendor"],
            "first_price": first_price, "last_price": last_price,
            "pct_change": pct_change,
        }
    findings["vendor_price_variance"] = variance

    # 7. Total exposure headline ------------------------------------------
    findings["total_exposure"] = round(
        findings["ar_over_90"]
        + findings["unassigned_job_costs_total"]
        + findings["duplicate_vendor_charges_total"]
        + findings["unapplied_payments_total"]
        + findings["worst_project_margin_gap"],
        2,
    )

    # 8. Health scores ------------------------------------------------------
    # Transparent, documented scoring formulas (0-100, higher is healthier).
    # These are the same formulas that would run against a real client file.
    def score(penalty_pct, weight=1.0, floor=0):
        return max(floor, round(100 - penalty_pct * weight))

    pct_ar_over90 = (findings["ar_over_90"] / total_ar * 100) if total_ar else 0
    receivables_score = score(pct_ar_over90, weight=1.75)

    # Job costing reliability is a process-integrity question as much as a
    # dollar-ratio one: a handful of untracked cost lines undermines trust in
    # every project's margin even when the dollars involved are modest, so
    # each instance carries a flat penalty on top of the dollar-weighted one.
    total_project_cost = sum(p["cost"] for p in proj_rows)
    pct_unassigned = (findings["unassigned_job_costs_total"] /
                       (findings["unassigned_job_costs_total"] + total_project_cost) * 100)
    job_costing_score = max(30, round(100 - findings["unassigned_job_costs_count"] * 5 - pct_unassigned * 2))

    total_vendor_spend = sum(to_num(t["amount"]) for t in vendor_txns)
    pct_duplicate = (findings["duplicate_vendor_charges_total"] / total_vendor_spend * 100) if total_vendor_spend else 0
    expense_classification_score = max(40, round(100 - findings["duplicate_vendor_charges_count"] * 8 - pct_duplicate * 2))

    # Management reporting is only as trustworthy as the job-costing and A/R
    # data it's built on, so it's modelled as a knock-on of those two scores
    # rather than an independent measure.
    management_reporting_score = max(30, round((job_costing_score + receivables_score) / 2 - 5))

    reconciliation_score = max(70, round(100 - (findings["unapplied_payments_count"] / len(payments) * 100) * 3)) if payments else 100

    scores = {
        "Reconciliation": reconciliation_score,
        "Receivables": receivables_score,
        "Job costing": job_costing_score,
        "Expense classification": expense_classification_score,
        "Management reporting": management_reporting_score,
    }
    scores["Total"] = round(sum(scores.values()) / len(scores))
    findings["health_scores"] = scores

    # newline="\n" on purpose. Without it Python translates to CRLF on Windows,
    # the file is stored LF, and every run of this script leaves findings.json
    # showing as modified with no content change -- which made the deploy gate
    # refuse the release three times running.
    with open(DATA / "findings.json", "w", newline="\n") as f:
        json.dump(findings, f, indent=2)

    print("Computed findings:")
    print(f"  Total exposure: C${findings['total_exposure']:,.0f}")
    print(f"  A/R > 90 days: C${findings['ar_over_90']:,.0f}")
    print(f"  Unassigned job costs: C${findings['unassigned_job_costs_total']:,.0f}")
    print(f"  Duplicate vendor charges: C${findings['duplicate_vendor_charges_total']:,.0f}")
    print(f"  Unapplied payments: C${findings['unapplied_payments_total']:,.0f}")
    print(f"  Worst project: {findings['worst_project']['project_name']} "
          f"at {findings['worst_project']['margin_pct']}% margin "
          f"(portfolio avg {findings['portfolio_avg_margin_pct']}%)")
    print(f"  Vendor price variance: {findings['vendor_price_variance']}")
    print(f"  Health scores: {scores}")
    return findings


if __name__ == "__main__":
    analyze()
