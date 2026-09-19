"""The CSV acceptance fixture for the reporting projection.

``data/*.csv`` and ``data/findings.json`` are an existing known-good pair: the
numbers in the published sample Cash-Leak Review were computed from those CSVs
by ``scripts/analyze.py``. That makes them an oracle for the projection that
replaces it -- but only if the CSVs reach the projection the way a real
QuickBooks file would.

So this module does not write to the canonical tables. It turns the CSVs into
QuickBooks-shaped objects and hands them to ``work_engine.persist_canonical``,
which is the same shredder a live pull goes through. Everything after the
first step is production code.

Two places where the CSVs hold less than a QuickBooks file does, and what is
done about each:

* ``invoices.csv`` has no project column, so no invoice can be tagged to a
  job and project revenue would be zero for every project. The fixture
  materialises one project-revenue invoice per row of ``projects.csv``,
  carrying that row's authored revenue on a project-tagged line. It is settled
  in full so it cannot disturb receivables.
* ``projects.csv`` states ``actual_cost`` as an authored total rather than
  deriving it. The fixture does not use that column at all: project cost comes
  from the vendor transactions, which is the whole point of computing from a
  ledger. Where the two disagree, the ledger is right -- see
  ``test_reporting.py``.
"""
from __future__ import annotations

import csv
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# A minimal chart of accounts. The category column in vendor_transactions.csv
# is a contractor's cost breakdown, which is what these map onto.
ACCOUNTS = [
    {"Id": "1100", "Name": "Accounts Receivable", "AccountType": "Accounts Receivable"},
    {"Id": "4000", "Name": "Construction Income", "AccountType": "Income"},
    {"Id": "5000", "Name": "Materials", "AccountType": "Cost of Goods Sold"},
    {"Id": "5100", "Name": "Labour", "AccountType": "Cost of Goods Sold"},
    {"Id": "5200", "Name": "Subcontractor", "AccountType": "Cost of Goods Sold"},
]
COST_ACCOUNT_BY_CATEGORY = {"Materials": "5000", "Labour": "5100", "Subcontractor": "5200"}
INCOME_ACCOUNT = "4000"


def _read(name: str, data_dir: Path) -> list[dict]:
    with open(data_dir / name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def qbo_objects(data_dir: Path | None = None, *, revenue_date: str = "2026-01-01") -> dict:
    """Build QuickBooks-shaped objects from the CSV dataset.

    ``revenue_date`` dates the synthetic project-revenue invoices. It only has
    to fall inside the period; those invoices carry no balance, so they never
    reach an aging bucket.
    """
    data_dir = Path(data_dir) if data_dir else DATA_DIR
    customers = _read("customers.csv", data_dir)
    vendors = _read("vendors.csv", data_dir)
    projects = _read("projects.csv", data_dir)
    invoices = _read("invoices.csv", data_dir)
    payments = _read("payments.csv", data_dir)
    vendor_transactions = _read("vendor_transactions.csv", data_dir)

    customer_id = {row["name"]: row["customer_id"] for row in customers}
    vendor_id = {row["name"]: row["vendor_id"] for row in vendors}

    customer_objects = [
        {"Id": row["customer_id"], "DisplayName": row["name"], "Active": True,
         "SyncToken": "0"}
        for row in customers
    ]
    # A QuickBooks job is a customer with a parent. Nothing marks one
    # "completed"; a finished job is made inactive, so that is the mapping.
    for row in projects:
        customer_objects.append({
            "Id": row["project_id"],
            "DisplayName": row["project_name"],
            "Job": True,
            "Active": row["status"] != "completed",
            "ParentRef": {"value": customer_id[row["customer"]]},
            "SyncToken": "0",
        })

    vendor_objects = [
        {"Id": row["vendor_id"], "DisplayName": row["name"], "Active": True, "SyncToken": "0"}
        for row in vendors
    ]

    invoice_objects = []
    for row in invoices:
        amount = _number(row["amount"])
        invoice_objects.append({
            "Id": row["invoice_id"], "DocNumber": row["invoice_id"],
            "TxnDate": row["issue_date"], "DueDate": row["due_date"],
            "TotalAmt": amount,
            "Balance": amount if row["status"] == "unpaid" else 0.0,
            "CustomerRef": {"value": customer_id[row["customer"]]},
            "SyncToken": "0",
            "Line": [{"Id": "1", "Amount": amount, "Description": "Contract work",
                      "SalesItemLineDetail": {"AccountRef": {"value": INCOME_ACCOUNT}}}],
        })
    for row in projects:
        revenue = _number(row["revenue"])
        invoice_objects.append({
            "Id": f"PRJ-REV-{row['project_id']}", "DocNumber": f"PRJ-REV-{row['project_id']}",
            "TxnDate": revenue_date, "TotalAmt": revenue, "Balance": 0.0,
            "CustomerRef": {"value": row["project_id"]},
            "SyncToken": "0",
            "Line": [{"Id": "1", "Amount": revenue, "Description": row["project_name"],
                      "SalesItemLineDetail": {
                          "AccountRef": {"value": INCOME_ACCOUNT},
                          "CustomerRef": {"value": row["project_id"]}}}],
        })

    payment_objects = []
    for row in payments:
        amount = _number(row["amount"])
        payment_objects.append({
            "Id": row["payment_id"], "TxnDate": row["date"], "TotalAmt": amount,
            "UnappliedAmt": 0.0 if row.get("applied_to_invoice") else amount,
            "CustomerRef": {"value": customer_id[row["customer"]]},
            "SyncToken": "0", "Line": [],
        })

    purchase_objects = []
    for row in vendor_transactions:
        amount = _number(row["amount"])
        account = COST_ACCOUNT_BY_CATEGORY.get(row["category"], "5000")
        detail: dict = {"AccountRef": {"value": account}}
        if row["project_id"]:
            detail["CustomerRef"] = {"value": row["project_id"]}
        if row["line_item"]:
            # A catalogue item bought at a stated unit price. This is the only
            # shape that carries price history.
            unit_price = _number(row["unit_price"])
            detail["UnitPrice"] = unit_price
            detail["Qty"] = round(amount / unit_price, 4) if unit_price else 0
            line = {"Id": "1", "Amount": amount, "Description": row["line_item"],
                    "ItemBasedExpenseLineDetail": detail}
        else:
            line = {"Id": "1", "Amount": amount, "Description": row["category"],
                    "AccountBasedExpenseLineDetail": detail}
        purchase_objects.append({
            "Id": row["txn_id"], "DocNumber": row["txn_id"], "TxnDate": row["date"],
            "TotalAmt": amount, "EntityRef": {"value": vendor_id[row["vendor"]]},
            "SyncToken": "0", "Line": [line],
        })

    return {
        "Account": [dict(item, SyncToken="0", Active=True) for item in ACCOUNTS],
        "Customer": customer_objects,
        "Vendor": vendor_objects,
        "Class": [],
        "TaxCode": [],
        "Estimate": [],
        "Invoice": invoice_objects,
        "Payment": payment_objects,
        "Bill": [],
        "Purchase": purchase_objects,
        "Deposit": [],
        "JournalEntry": [],
        "Attachable": [],
    }
