"""
Generates a realistic, internally-consistent synthetic QBO-style dataset for
NorthStar Renovations Ltd. -- a clearly-labelled DEMONSTRATION COMPANY used
only for the JobMargin sample Cash-Leak Review. No real client data anywhere.

The dataset deliberately contains the exact problems a real Cash-Leak Review
looks for (see analyze.py), planted at realistic magnitudes, so the sample
report's findings are *computed from data*, not hand-typed. That means this
generator + analyze.py is also the first working draft of the actual
diagnostic engine Aryan would run against a real client's exports later --
swap this synthetic data for a real QBO export in the same shape and the
same script produces a real report.

Output: CSV files in ../data/
"""
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)  # reproducible demo data

OUT = Path(__file__).parent.parent / "data"
OUT.mkdir(parents=True, exist_ok=True)

TODAY = date(2026, 8, 31)  # report "as of" date

COMPANY = {
    "name": "NorthStar Renovations Ltd.",
    "label": "DEMONSTRATION COMPANY - synthetic data, not a real client",
    "province": "Ontario",
    "software": "QuickBooks Online",
    "employees": 8,
    "subcontractors": 11,
}

# ---------------------------------------------------------------- customers
CUSTOMER_NAMES = [
    "Fenwick Residence", "Okafor Family Trust", "Bramwell Holdings",
    "Chen-Patel Household", "Dorset Lane Properties", "Ashworth Residence",
    "Levy Family Home", "Marchetti Properties", "Kowalski Residence",
    "Greenhill Rentals Inc.", "Osei Household", "Turnbull Residence",
    "Whitfield Family Trust", "Nakamura Residence", "Bellview Holdings",
]
customers = [{"customer_id": f"C{i+1:03d}", "name": n} for i, n in enumerate(CUSTOMER_NAMES)]

# ------------------------------------------------------------------ vendors
VENDOR_NAMES = [
    "Ontario Lumber Supply", "ProBuild Materials", "Elite Electrical Wholesale",
    "GTA Plumbing Supply Co.", "Superior Drywall & Insulation",
    "Precision Cabinetry Ltd.", "Northside Concrete", "Apex Tool Rental",
    "Reliable Roofing Supply", "Metro Paint & Finishes",
]
vendors = [{"vendor_id": f"V{i+1:02d}", "name": n} for i, n in enumerate(VENDOR_NAMES)]

# ----------------------------------------------------------------- projects
# 19 active/recent projects as described in the plan. Three are deliberately
# planted problem cases; the rest are healthy comparables.
PROJECT_TEMPLATES = [
    ("Kitchen #233", "Fenwick Residence", 64000, 42000, "completed"),
    ("Addition #237", "Bramwell Holdings", 118000, 82600, "completed"),
    ("Basement #241", "Okafor Family Trust", 71000, 65300, "active"),  # planted: weak margin
    ("Kitchen #205", "Ashworth Residence", 58000, 39400, "completed"),
    ("Bathroom #212", "Levy Family Home", 31000, 20200, "completed"),
    ("Full Reno #219", "Marchetti Properties", 210000, 148000, "active"),
    ("Addition #224", "Kowalski Residence", 96000, 66500, "completed"),
    ("Bathroom #229", "Greenhill Rentals Inc.", 27000, 17800, "completed"),
    ("Kitchen #244", "Osei Household", 61000, 41200, "active"),
    ("Basement #248", "Turnbull Residence", 45000, 30800, "completed"),
    # Includes both halves of the planted C$2,140 duplicate-looking charge.
    ("Full Reno #251", "Whitfield Family Trust", 187000, 135280, "active"),
    ("Addition #256", "Nakamura Residence", 103000, 71500, "active"),
    ("Kitchen #260", "Bellview Holdings", 59000, 40100, "completed"),
    ("Bathroom #263", "Chen-Patel Household", 29000, 19000, "completed"),
    ("Basement #267", "Dorset Lane Properties", 52000, 35700, "active"),
    ("Deck & Patio #271", "Fenwick Residence", 34000, 22900, "completed"),
    ("Kitchen #275", "Levy Family Home", 66000, 44800, "active"),
    ("Addition #279", "Marchetti Properties", 121000, 84200, "active"),
    ("Full Reno #283", "Whitfield Family Trust", 195000, 137100, "active"),
]

projects = []
for i, (name, customer, revenue, cost, status) in enumerate(PROJECT_TEMPLATES):
    projects.append({
        "project_id": f"P{233+i}" if "#" not in name else name.split("#")[1],
        "project_name": name,
        "customer": customer,
        "revenue": revenue,
        "actual_cost": cost,
        "status": status,
    })

# ----------------------------------------------------------- invoices (A/R)
# Planted: total >90 days overdue should land at ~C$18,000-19,000
invoices = []
inv_no = 1000
aging_plan = [
    # (days_old, amount) - a spread across buckets, with a concentrated
    # >90 day cluster to plant the finding
    (12, 8400), (18, 5200), (25, 11300), (33, 6100), (40, 9800),
    (52, 7200), (61, 4300), (70, 6600), (81, 5100),
    (94, 6420), (101, 7800), (118, 4200),  # >90 day bucket ~= 18,420
]
for days, amt in aging_plan:
    cust = random.choice(CUSTOMER_NAMES)
    invoices.append({
        "invoice_id": f"INV-{inv_no}",
        "customer": cust,
        "issue_date": (TODAY - timedelta(days=days)).isoformat(),
        "due_date": (TODAY - timedelta(days=days - 30)).isoformat(),
        "amount": amt,
        "days_outstanding": days,
        "status": "unpaid",
    })
    inv_no += 1
# plus a batch of already-paid invoices for realism (not part of A/R aging)
for _ in range(40):
    days = random.randint(1, 400)
    invoices.append({
        "invoice_id": f"INV-{inv_no}",
        "customer": random.choice(CUSTOMER_NAMES),
        "issue_date": (TODAY - timedelta(days=days)).isoformat(),
        "due_date": (TODAY - timedelta(days=days - 30)).isoformat(),
        "amount": random.randint(2000, 18000),
        "days_outstanding": 0,
        "status": "paid",
    })
    inv_no += 1

# ------------------------------------------------------- customer payments
# Planted: ~3,170 in received-but-unapplied payments
payments = []
pay_no = 5000
unapplied_plan = [1240, 980, 950]  # sums to 3170
for amt in unapplied_plan:
    payments.append({
        "payment_id": f"PMT-{pay_no}",
        "customer": random.choice(CUSTOMER_NAMES),
        "date": (TODAY - timedelta(days=random.randint(5, 60))).isoformat(),
        "amount": amt,
        "applied_to_invoice": "",  # unapplied
    })
    pay_no += 1
for _ in range(60):
    payments.append({
        "payment_id": f"PMT-{pay_no}",
        "customer": random.choice(CUSTOMER_NAMES),
        "date": (TODAY - timedelta(days=random.randint(1, 300))).isoformat(),
        "amount": random.randint(1500, 15000),
        "applied_to_invoice": f"INV-{random.randint(1000, inv_no-1)}",
    })
    pay_no += 1

# ---------------------------------------------------- vendor / job cost txns
# Planted: ~11,760 in material spend with no project assigned
# Planted: ~4,280 in likely-duplicate vendor charges
# Planted: vendor price variance ~+14.8% on a repeated material line item
vendor_txns = []
txn_no = 9000

# unassigned material costs
unassigned_plan = [2450, 1980, 3100, 1730, 2500]  # sums to 11,760
for amt in unassigned_plan:
    vendor_txns.append({
        "txn_id": f"TXN-{txn_no}",
        "vendor": random.choice(VENDOR_NAMES),
        "date": (TODAY - timedelta(days=random.randint(5, 150))).isoformat(),
        "amount": amt,
        "category": "Materials",
        "project_id": "",  # planted gap
    })
    txn_no += 1

# duplicate-looking charges (same vendor, same amount, 1-3 days apart)
dup_vendor = "ProBuild Materials"
dup_amount = 2140
base_date = TODAY - timedelta(days=45)
vendor_txns.append({
    "txn_id": f"TXN-{txn_no}", "vendor": dup_vendor, "date": base_date.isoformat(),
    "amount": dup_amount, "category": "Materials", "project_id": "251",
})
txn_no += 1
vendor_txns.append({
    "txn_id": f"TXN-{txn_no}", "vendor": dup_vendor,
    "date": (base_date + timedelta(days=2)).isoformat(),
    "amount": dup_amount, "category": "Materials", "project_id": "251",
})
txn_no += 1

# normal, correctly-assigned costs feeding project actual_cost totals
for proj in projects:
    # The two explicit rows above are part of #251's authored actual cost.
    # Subtract them here so the ordinary rows plus the planted pair reconcile
    # to projects.csv instead of quietly overstating the project ledger.
    remaining = proj["actual_cost"] - (dup_amount * 2 if proj["project_id"] == "251" else 0)
    n_lines = random.randint(4, 8)
    for _ in range(n_lines - 1):
        line = int(remaining * random.uniform(0.08, 0.2))
        vendor_txns.append({
            "txn_id": f"TXN-{txn_no}", "vendor": random.choice(VENDOR_NAMES),
            "date": (TODAY - timedelta(days=random.randint(5, 200))).isoformat(),
            "amount": line, "category": random.choice(["Materials", "Subcontractor", "Labour"]),
            "project_id": proj["project_id"],
        })
        txn_no += 1
        remaining -= line
    vendor_txns.append({
        "txn_id": f"TXN-{txn_no}", "vendor": random.choice(VENDOR_NAMES),
        "date": (TODAY - timedelta(days=random.randint(5, 200))).isoformat(),
        "amount": max(remaining, 500), "category": "Subcontractor",
        "project_id": proj["project_id"],
    })
    txn_no += 1

# price variance series: same SKU-like line item, rising unit price over time
# ("2x6 spruce framing lumber, per bundle") to plant a ~+14.8% variance
price_points = [(210, 61.00), (150, 62.50), (90, 65.00), (45, 68.00), (10, 70.00)]
for days_ago, unit_price in price_points:
    vendor_txns.append({
        "txn_id": f"TXN-{txn_no}", "vendor": "Ontario Lumber Supply",
        "date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "amount": round(unit_price * 40, 2), "category": "Materials",
        "project_id": "", "line_item": "2x6 spruce framing lumber (per bundle of 40)",
        "unit_price": unit_price,
    })
    txn_no += 1

# --------------------------------------------------------------- write CSVs
def write_csv(name, rows):
    if not rows:
        return
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

write_csv("customers.csv", customers)
write_csv("vendors.csv", vendors)
write_csv("projects.csv", projects)
write_csv("invoices.csv", invoices)
write_csv("payments.csv", payments)
write_csv("vendor_transactions.csv", vendor_txns)

with open(OUT / "company.json", "w") as f:
    json.dump(COMPANY, f, indent=2)

print(f"Generated synthetic dataset for {COMPANY['name']} in {OUT}")
print(f"  {len(projects)} projects, {len(invoices)} invoices, "
      f"{len(payments)} payments, {len(vendor_txns)} vendor transactions")
