"""Deterministic synthetic truth factory for Bookkeeping Work Engine v0.

Each company has three independent artefacts: the messy provider objects, the
external evidence available to Shimline, and a known-correct golden ledger.
The engine never receives the golden ledger; tests use it only as an oracle.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .postings import POSTING_TYPES
from .qbo_adapter import (MutationResult, SAFE_ACTIONS, StaleObjectError,
                          declare_pull)

SEEDED_DEFECTS = {
    "duplicate_transaction", "missing_transaction", "unapplied_payment",
    "incorrect_job_allocation", "sales_tax_error", "stale_receivable",
    "unreconciled_account", "stale_payable", "abnormal_price", "negative_job_margin",
    "suspicious_balance_sheet",
}


def _posting(account: str, debit=0, credit=0):
    return {"account": account, "debit": str(debit), "credit": str(credit)}


def _line(line_id: str, amount, account="500", project="P1", tax="HST13"):
    return {
        "Id": line_id, "Amount": float(amount), "Description": "Synthetic source document",
        "AccountBasedExpenseLineDetail": {
            "AccountRef": {"value": account},
            "CustomerRef": {"value": project},
            "ClassRef": {"value": "C1"},
            "TaxCodeRef": {"value": tax},
        },
    }


def _item_line(line_id: str, item: str, unit_price, quantity,
               account="500", project="P1", tax="HST13"):
    """A priced purchase line.

    `_line` above is account-based and carries no unit price, which is what left
    check 12 with a storage column and nothing to put in it. An item-based line
    states UnitPrice and Qty, which persist_canonical already reads, so a priced
    history accumulates per vendor and item.
    """
    amount = Decimal(str(unit_price)) * Decimal(str(quantity))
    return {
        "Id": line_id, "Amount": float(amount), "Description": item,
        "ItemBasedExpenseLineDetail": {
            "ItemRef": {"value": item},
            "UnitPrice": float(unit_price),
            "Qty": float(quantity),
            "AccountRef": {"value": account},
            "CustomerRef": {"value": project},
            "ClassRef": {"value": "C1"},
            "TaxCodeRef": {"value": tax},
        },
    }


def _revenue_line(line_id: str, amount, project="P1", account="400", tax="HST13"):
    """An invoice line that says which job earned the money.

    Invoices carried `"Line": []`, so revenue existed in the postings but had no
    project dimension. Cost was attributable to a job and revenue was not, which
    made job margin -- check 09 -- uncomputable from either side.
    """
    return {
        "Id": line_id, "Amount": float(amount), "Description": "Contract work",
        "SalesItemLineDetail": {
            "ItemAccountRef": {"value": account},
            "CustomerRef": {"value": project},
            "ClassRef": {"value": "C1"},
            "TaxCodeRef": {"value": tax},
        },
    }


@dataclass
class SyntheticCompany:
    company_id: str
    objects: dict[str, list[dict]]
    evidence: dict
    golden_objects: dict[str, list[dict]]
    seeded_defects: set[str] = field(default_factory=lambda: set(SEEDED_DEFECTS))

    def golden_trial_balance(self) -> dict[str, Decimal]:
        return trial_balance(self.golden_objects)


def trial_balance(objects: dict[str, list[dict]]) -> dict[str, Decimal]:
    """Sum every `_Postings` block in the corpus.

    The kind list is `POSTING_TYPES` rather than a copy of it. A hand-written
    copy of exactly this list went stale once already -- six types were added to
    the engine, `beancount_export.POSTED_KINDS` kept naming the original six, and
    the new documents counted toward Shimline's balances while being silently
    absent from the file the oracle checked. A second copy of a list is a defect
    waiting for the right day.
    """
    balances: dict[str, Decimal] = {}
    for kind in POSTING_TYPES:
        for txn in objects.get(kind, []):
            for item in txn.get("_Postings", []):
                account = item["account"]
                balances[account] = balances.get(account, Decimal("0")) + (
                    Decimal(item.get("debit", "0")) - Decimal(item.get("credit", "0")))
    return {key: value.quantize(Decimal("0.01")) for key, value in sorted(balances.items())}


def generate_company(seed: int) -> SyntheticCompany:
    rng = random.Random(seed)
    company_id = f"synthetic-{seed:03d}"
    today = date(2026, 9, 8)
    amount = Decimal(str(rng.choice((75, 80, 95))))
    missing_amount = Decimal(str(rng.choice((125, 150, 175))))
    accounts = [
        {"Id": "100", "Name": "Operating Bank", "AccountType": "Bank", "SyncToken": "0"},
        # A second bank account, so a Transfer has somewhere to go. A transfer
        # between a chequing account and an Other Current Asset is not a
        # transfer, and without two real bank accounts the type could not be
        # represented at all.
        {"Id": "105", "Name": "Savings", "AccountType": "Bank", "SyncToken": "0"},
        {"Id": "110", "Name": "Accounts Receivable", "AccountType": "Accounts Receivable", "SyncToken": "0"},
        {"Id": "120", "Name": "GST/HST Recoverable", "AccountType": "Other Current Asset", "SyncToken": "0"},
        {"Id": "200", "Name": "Accounts Payable", "AccountType": "Accounts Payable", "SyncToken": "0"},
        # Tax *collected* is a liability, and it is also the only thing
        # `_tax_account` can fall back to: it resolves a single Other Current
        # Liability and returns None when there are two, rather than guessing.
        # Until this existed no generated document could carry TxnTaxDetail at
        # all, so the tax leg of a sale had never been through the corpus.
        {"Id": "210", "Name": "GST/HST Payable", "AccountType": "Other Current Liability", "SyncToken": "0"},
        # Where till takings sit between the sale and the bank run. A balance
        # left here is one of the most common real bookkeeping faults, and it is
        # seeded deliberately below so check 15 is graded against an intended
        # defect rather than an accident of the generator.
        {"Id": "115", "Name": "Undeposited Funds", "AccountType": "Other Current Asset", "SyncToken": "0"},
        # Without an opening balance this company's bank account nets negative
        # for its whole life, which is not a company -- it is a generator that
        # forgot the owner put money in. Every asset-credit-balance check would
        # fire on all fifty companies, and a detector that fires on everything
        # is worse than no detector.
        {"Id": "300", "Name": "Owner's Equity", "AccountType": "Equity", "SyncToken": "0"},
        {"Id": "400", "Name": "Contract Revenue", "AccountType": "Income", "SyncToken": "0"},
        {"Id": "500", "Name": "Materials", "AccountType": "Cost of Goods Sold", "SyncToken": "0"},
    ]
    customers = [
        {"Id": "CU1", "DisplayName": "Maple Build Ltd", "Job": False, "SyncToken": "0"},
        {"Id": "P1", "DisplayName": "Maple Build:Kitchen", "Job": True,
         "ParentRef": {"value": "CU1"}, "SyncToken": "0"},
        {"Id": "P2", "DisplayName": "Maple Build:Basement", "Job": True,
         "ParentRef": {"value": "CU1"}, "SyncToken": "0"},
    ]
    vendor = {"Id": "V1", "DisplayName": "Northern Supply", "SyncToken": "0"}
    invoice = {
        "Id": "I1", "TxnDate": (today - timedelta(days=35)).isoformat(), "DocNumber": f"INV-{seed}",
        "TotalAmt": 1000.0, "Balance": 0.0, "CustomerRef": {"value": "CU1"}, "SyncToken": "0",
        "Line": [_revenue_line("1", 1000, project="P1")],
        "_Postings": [_posting("110", debit=1000), _posting("400", credit=1000)],
    }
    payment = {
        "Id": "PAY1", "TxnDate": (today - timedelta(days=20)).isoformat(), "TotalAmt": 1000.0,
        "UnappliedAmt": 1000.0, "CustomerRef": {"value": "CU1"}, "SyncToken": "0", "Line": [],
        "_Postings": [_posting("100", debit=1000), _posting("110", credit=1000)],
    }
    payment_golden = copy.deepcopy(payment)
    payment_golden.update({"UnappliedAmt": 0.0, "Line": [{"LinkedTxn": [{"TxnId": "I1", "TxnType": "Invoice"}]}]})
    expense = {
        "Id": "E1", "TxnDate": (today - timedelta(days=12)).isoformat(), "DocNumber": f"R-{seed}",
        "TotalAmt": float(amount), "PaymentType": "Cash", "EntityRef": {"value": "V1"},
        "AccountRef": {"value": "100"}, "SyncToken": "0", "Line": [_line("1", amount)],
        "_Postings": [_posting("500", debit=amount), _posting("100", credit=amount)],
    }
    duplicate = copy.deepcopy(expense)
    duplicate.update({"Id": "E-DUP", "SyncToken": "0"})
    wrong_project = copy.deepcopy(expense)
    wrong_project.update({"Id": "E-PROJECT", "DocNumber": f"PJ-{seed}"})
    wrong_project["Line"] = [_line("1", 60, project="P2")]
    wrong_project["TotalAmt"] = 60.0
    wrong_project["_Postings"] = [_posting("500", debit=60), _posting("100", credit=60)]
    correct_project = copy.deepcopy(wrong_project)
    correct_project["Line"][0]["AccountBasedExpenseLineDetail"]["CustomerRef"] = {"value": "P1"}
    tax_error = copy.deepcopy(expense)
    tax_error.update({"Id": "E-TAX", "DocNumber": f"TAX-{seed}", "TotalAmt": 113.0})
    tax_error["Line"] = [_line("1", 113, tax="EXEMPT")]
    tax_error["_Postings"] = [_posting("500", debit=113), _posting("100", credit=113)]
    tax_golden = copy.deepcopy(tax_error)
    tax_golden["Line"] = [_line("1", 100, tax="HST13")]
    tax_golden["_Postings"] = [
        _posting("500", debit=100), _posting("120", debit=13), _posting("100", credit=113)]
    missing = copy.deepcopy(expense)
    missing.update({"Id": "E-MISSING", "DocNumber": f"BANK-{seed}", "TotalAmt": float(missing_amount)})
    missing["Line"] = [_line("1", missing_amount)]
    missing["_Postings"] = [_posting("500", debit=missing_amount), _posting("100", credit=missing_amount)]
    stale_invoice = {
        "Id": "I-STALE", "TxnDate": (today - timedelta(days=125)).isoformat(),
        "DocNumber": f"OLD-{seed}", "TotalAmt": 200.0, "Balance": 200.0,
        "CustomerRef": {"value": "CU1"}, "SyncToken": "0", "Line": [],
        "_Postings": [_posting("110", debit=200), _posting("400", credit=200)],
    }
    # An unpaid supplier bill, well past its due date. Unlike the other seeds
    # this is not a defect to be corrected -- the books are right, the money is
    # simply owed and late. It belongs in the golden ledger unchanged, and its
    # detection is a residual exception rather than a proposal, because deciding
    # to pay a vendor is the client's call and never the engine's.
    stale_bill = {
        "Id": "B-STALE", "TxnDate": (today - timedelta(days=95)).isoformat(),
        "DueDate": (today - timedelta(days=65)).isoformat(),
        "DocNumber": f"BILL-{seed}", "TotalAmt": 480.0, "Balance": 480.0,
        "VendorRef": {"value": "V1"}, "SyncToken": "0",
        "Line": [_line("1", 480)],
        "_Postings": [_posting("500", debit=480), _posting("200", credit=480)],
    }
    # A second bill, also unpaid, but not yet due. It must NOT be flagged; a
    # detector that cannot tell "owed" from "overdue" is worse than none.
    current_bill = {
        "Id": "B-CURRENT", "TxnDate": (today - timedelta(days=10)).isoformat(),
        "DueDate": (today + timedelta(days=20)).isoformat(),
        "DocNumber": f"BILLC-{seed}", "TotalAmt": 220.0, "Balance": 220.0,
        "VendorRef": {"value": "V1"}, "SyncToken": "0",
        "Line": [_line("1", 220)],
        "_Postings": [_posting("500", debit=220), _posting("200", credit=220)],
    }

    # --- Priced purchase history, for check 12 -------------------------------
    #
    # Four buys of the same item from the same vendor. Three establish a
    # baseline; the fourth is a 3x spike. A detector needs both -- a spike with
    # no baseline is not a finding, it is the first observation.
    price_history = []
    for index, unit in enumerate(("10.00", "10.00", "10.50"), start=1):
        price_history.append({
            "Id": f"E-PRICE{index}",
            "TxnDate": (today - timedelta(days=90 - index * 10)).isoformat(),
            "DocNumber": f"PR{index}-{seed}", "TotalAmt": float(Decimal(unit) * 20),
            "PaymentType": "Cash", "EntityRef": {"value": "V1"},
            "AccountRef": {"value": "100"}, "SyncToken": "0",
            "Line": [_item_line("1", "2x4-lumber", unit, 20)],
            "_Postings": [_posting("500", debit=Decimal(unit) * 20),
                          _posting("100", credit=Decimal(unit) * 20)],
        })
    price_spike = {
        "Id": "E-PRICE-SPIKE", "TxnDate": (today - timedelta(days=8)).isoformat(),
        "DocNumber": f"PRS-{seed}", "TotalAmt": 620.0,
        "PaymentType": "Cash", "EntityRef": {"value": "V1"},
        "AccountRef": {"value": "100"}, "SyncToken": "0",
        "Line": [_item_line("1", "2x4-lumber", "31.00", 20)],
        "_Postings": [_posting("500", debit=620), _posting("100", credit=620)],
    }

    # --- A job that loses money, for check 09 --------------------------------
    #
    # P2 bills 500 and costs 800. Negative gross margin is not a judgement call,
    # which is why the detector starts there rather than at "weak".
    losing_job_invoice = {
        "Id": "I-P2", "TxnDate": (today - timedelta(days=30)).isoformat(),
        "DocNumber": f"INVP2-{seed}", "TotalAmt": 500.0, "Balance": 0.0,
        "CustomerRef": {"value": "CU1"}, "SyncToken": "0",
        "Line": [_revenue_line("1", 500, project="P2")],
        "_Postings": [_posting("110", debit=500), _posting("400", credit=500)],
    }
    losing_job_cost = {
        "Id": "E-P2COST", "TxnDate": (today - timedelta(days=25)).isoformat(),
        "DocNumber": f"P2C-{seed}", "TotalAmt": 800.0, "PaymentType": "Cash",
        "EntityRef": {"value": "V1"}, "AccountRef": {"value": "100"}, "SyncToken": "0",
        "Line": [_line("1", 800, project="P2")],
        "_Postings": [_posting("500", debit=800), _posting("100", credit=800)],
    }

    # --- The eight types the corpus never contained ---------------------------
    #
    # `POSTING_TYPES` handles twelve document types. Counting documents rather
    # than dictionary keys, this corpus held four: Deposit and JournalEntry read
    # as covered because the generator declared the keys and set them to `[]`.
    # So every acceptance run, every seeded defect and every golden-ledger
    # comparison had only ever seen an invoice, a payment, a bill and a purchase.
    #
    # None of these carries a seeded defect. They are here to be *derived* and
    # graded: the golden ledger and Beancount both have to agree with what
    # `derive_ledger` makes of them, at corpus scale rather than in one
    # hand-written example. A settlement month is also where a real contractor's
    # file is dense, so an engine that has never summed one is not ready for a
    # real file.

    # Paid in full, so the stale-payable detector must leave it alone. The two
    # bills above are load-bearing: one has to be flagged as overdue and one has
    # to not be, and settling either would destroy that pair.
    paid_bill = {
        "Id": "B-PAID", "TxnDate": (today - timedelta(days=40)).isoformat(),
        "DueDate": (today - timedelta(days=10)).isoformat(),
        "DocNumber": f"BILLP-{seed}", "TotalAmt": 350.0, "Balance": 0.0,
        "VendorRef": {"value": "V1"}, "SyncToken": "0",
        "Line": [_line("1", 350)],
        "_Postings": [_posting("500", debit=350), _posting("200", credit=350)],
    }
    bill_payment = {
        "Id": "BP1", "TxnDate": (today - timedelta(days=38)).isoformat(),
        "DocNumber": f"BP-{seed}", "TotalAmt": 350.0,
        "VendorRef": {"value": "V1"}, "SyncToken": "0",
        "CheckPayment": {"BankAccountRef": {"value": "100"}},
        "_Postings": [_posting("200", debit=350), _posting("100", credit=350)],
    }
    # A supplier credit for returned materials: the mirror of a bill.
    vendor_credit = {
        "Id": "VC1", "TxnDate": (today - timedelta(days=18)).isoformat(),
        "DocNumber": f"VC-{seed}", "TotalAmt": 60.0,
        "VendorRef": {"value": "V1"}, "SyncToken": "0",
        "Line": [_line("1", 60)],
        "_Postings": [_posting("200", debit=60), _posting("500", credit=60)],
    }
    # Retained earnings moved to savings. Transfer carries `Amount`, not
    # `TotalAmt`, which is why a zero-amount transfer is refused rather than
    # treated as an abandoned document.
    transfer = {
        "Id": "T1", "TxnDate": (today - timedelta(days=15)).isoformat(),
        "Amount": 300.0, "SyncToken": "0",
        "FromAccountRef": {"value": "100"}, "ToAccountRef": {"value": "105"},
        "_Postings": [_posting("105", debit=300), _posting("100", credit=300)],
    }
    # Cash takings banked directly, with no invoice behind them.
    deposit = {
        "Id": "D1", "TxnDate": (today - timedelta(days=14)).isoformat(),
        "TotalAmt": 150.0, "SyncToken": "0",
        "DepositToAccountRef": {"value": "100"},
        "Line": [{"Id": "1", "Amount": 150.0, "DetailType": "DepositLineDetail",
                  "DepositLineDetail": {"AccountRef": {"value": "400"}}}],
        "_Postings": [_posting("100", debit=150), _posting("400", credit=150)],
    }
    # A month-end accrual for a supplier cost not yet billed. The only type where
    # QuickBooks states the postings outright, and the one where an accountant's
    # own corrections live -- so the one it is least acceptable never to have
    # checked against a second engine.
    journal_entry = {
        "Id": "JE1", "TxnDate": (today - timedelta(days=9)).isoformat(),
        "DocNumber": f"JE-{seed}", "SyncToken": "0",
        "Line": [
            {"Id": "1", "Amount": 40.0, "DetailType": "JournalEntryLineDetail",
             "JournalEntryLineDetail": {"PostingType": "Debit",
                                        "AccountRef": {"value": "500"}}},
            {"Id": "2", "Amount": 40.0, "DetailType": "JournalEntryLineDetail",
             "JournalEntryLineDetail": {"PostingType": "Credit",
                                        "AccountRef": {"value": "200"}}}],
        "_Postings": [_posting("500", debit=40), _posting("200", credit=40)],
    }
    # The owner's opening contribution. Stated as a journal entry because that
    # is how an accountant records one, and because it gives the corpus a real
    # equity account to export.
    opening_balance = {
        "Id": "JE-OPEN", "TxnDate": (today - timedelta(days=180)).isoformat(),
        "DocNumber": f"OPEN-{seed}", "SyncToken": "0",
        "Line": [
            {"Id": "1", "Amount": 5000.0, "DetailType": "JournalEntryLineDetail",
             "JournalEntryLineDetail": {"PostingType": "Debit",
                                        "AccountRef": {"value": "100"}}},
            {"Id": "2", "Amount": 5000.0, "DetailType": "JournalEntryLineDetail",
             "JournalEntryLineDetail": {"PostingType": "Credit",
                                        "AccountRef": {"value": "300"}}}],
        "_Postings": [_posting("100", debit=5000), _posting("300", credit=5000)],
    }
    # A till sale: revenue, tax collected and cash in one document. This is the
    # first generated document to carry TxnTaxDetail, so it is also the first
    # time the corpus exercises `_tax_account` at all.
    #
    # It deposits to Undeposited Funds rather than the bank, and nothing ever
    # clears it. That is the seeded `suspicious_balance_sheet` defect: takings
    # stuck in transit is an ordinary fault an accountant wants flagged, and
    # seeding it on purpose is what makes `detected == seeded_defects` mean
    # something for check 15.
    sales_receipt = {
        "Id": "SR1", "TxnDate": (today - timedelta(days=7)).isoformat(),
        "DocNumber": f"SR-{seed}", "TotalAmt": 226.0, "SyncToken": "0",
        "CustomerRef": {"value": "CU1"},
        "DepositToAccountRef": {"value": "115"},
        "Line": [_revenue_line("1", 200, project="P1")],
        "TxnTaxDetail": {"TotalTax": 26.0},
        "_Postings": [_posting("115", debit=226), _posting("400", credit=200),
                      _posting("210", credit=26)],
    }
    # The mirror of that sale, refunded. Tax reverses with it: a refund that
    # leaves the tax collected behind overstates a GST/HST return.
    #
    # The amount is deliberately not the sale's. Tax collected must not net to
    # zero across the corpus -- at 26 collected and 26 reversed, a sign
    # inversion on the tax leg cancels itself out and no balance check anywhere
    # can see it. 210 is left holding 6.50.
    refund_receipt = {
        "Id": "RR1", "TxnDate": (today - timedelta(days=6)).isoformat(),
        "DocNumber": f"RR-{seed}", "TotalAmt": 56.50, "SyncToken": "0",
        "CustomerRef": {"value": "CU1"},
        "DepositToAccountRef": {"value": "100"},
        "Line": [_revenue_line("1", 50, project="P1")],
        "TxnTaxDetail": {"TotalTax": 6.50},
        "_Postings": [_posting("100", credit="56.50"), _posting("400", debit=50),
                      _posting("210", debit="6.50")],
    }
    # Reduces what a customer owes: the mirror of an invoice.
    credit_memo = {
        "Id": "CM1", "TxnDate": (today - timedelta(days=5)).isoformat(),
        "DocNumber": f"CM-{seed}", "TotalAmt": 113.0, "SyncToken": "0",
        "CustomerRef": {"value": "CU1"},
        "Line": [_revenue_line("1", 100, project="P1")],
        "TxnTaxDetail": {"TotalTax": 13.0},
        "_Postings": [_posting("110", credit=113), _posting("400", debit=100),
                      _posting("210", debit=13)],
    }

    current = {
        "Account": accounts, "Customer": customers, "Vendor": [vendor],
        "Class": [{"Id": "C1", "Name": "Construction", "SyncToken": "0"}],
        "TaxCode": [{"Id": "HST13", "Name": "HST 13%", "Active": True},
                    {"Id": "EXEMPT", "Name": "Exempt", "Active": True}],
        "Invoice": [invoice, stale_invoice, losing_job_invoice], "Payment": [payment],
        "Bill": [stale_bill, current_bill, paid_bill],
        "Purchase": [expense, duplicate, wrong_project, tax_error,
                     *price_history, price_spike, losing_job_cost],
        "Deposit": [deposit],
        "JournalEntry": [opening_balance, journal_entry], "Attachable": [],
        "BillPayment": [bill_payment], "VendorCredit": [vendor_credit],
        "Transfer": [transfer], "SalesReceipt": [sales_receipt],
        "RefundReceipt": [refund_receipt], "CreditMemo": [credit_memo],
    }
    golden = copy.deepcopy(current)
    golden["Payment"] = [payment_golden]
    golden["Purchase"] = [expense, correct_project, tax_golden, missing,
                          *price_history, price_spike, losing_job_cost]
    expected_bank = trial_balance(golden)["100"]
    evidence = {
        "period_start": (today - timedelta(days=150)).isoformat(), "period_end": today.isoformat(),
        "bank_statement": {"account_id": "100", "ending_ledger_balance": str(expected_bank),
                           "missing_transactions": [copy.deepcopy(missing)]},
        "source_documents": {
            "E-PROJECT": {"expected_project_id": "P1", "hash": f"doc-project-{seed}"},
            "E-TAX": {"expected_tax_code": "HST13", "net": "100.00", "tax": "13.00",
                      "hash": f"doc-tax-{seed}"},
        },
        # Deliberately absent: proof that OLD-{seed} was paid or is uncollectible.
        "receivable_confirmations": {},
    }
    # The generated company is a pull, so it records itself like one. Without
    # this the acceptance suite would exercise a path production never takes --
    # and the manifest gate exists precisely because an entity that failed to
    # load looks exactly like an entity with no rows.
    for shape in (current, golden):
        declare_pull(shape, source="synthetic")
    return SyntheticCompany(company_id, current, evidence, golden)


def generate_companies(count: int = 50, seed: int = 7300) -> list[SyntheticCompany]:
    return [generate_company(seed + index) for index in range(count)]


class SyntheticQBOAdapter:
    """In-memory QBO double with stale tokens, retries, and request replay."""

    environment = "sandbox"

    def __init__(self, company: SyntheticCompany, *, stale_once: bool = True,
                 fail_after_commit_once: bool = True):
        self.company = company
        self.objects = copy.deepcopy(company.objects)
        self.evidence = copy.deepcopy(company.evidence)
        self.stale_once = stale_once
        self.fail_after_commit_once = fail_after_commit_once
        self._stale_seen: set[str] = set()
        self._uncertain_seen: set[str] = set()
        self._results: dict[str, MutationResult] = {}
        self.applied_request_ids: set[str] = set()

    def pull_all(self) -> dict[str, list[dict]]:
        return copy.deepcopy(self.objects)

    def read(self, object_type: str, object_id: str) -> dict:
        for item in self.objects.get(object_type, []):
            if str(item.get("Id")) == str(object_id):
                return copy.deepcopy(item)
        raise KeyError(f"No {object_type} {object_id}")

    def mutate(self, action: str, *, object_type: str, payload: dict,
               idempotency_key: str, object_id: str | None = None,
               expected_sync_token: str | None = None) -> MutationResult:
        from .qbo_adapter import UncertainWriteError

        if action not in SAFE_ACTIONS:
            raise ValueError(f"Unsupported synthetic mutation: {action}")
        if idempotency_key in self._results:
            result = self._results[idempotency_key]
            return MutationResult(result.object_type, result.object_id, result.sync_token,
                                  copy.deepcopy(result.payload), replayed=True)

        if object_id:
            current = self.read(object_type, object_id)
            if self.stale_once and idempotency_key not in self._stale_seen:
                self._stale_seen.add(idempotency_key)
                # A concurrent harmless edit advances QBO's token.
                for item in self.objects[object_type]:
                    if str(item.get("Id")) == str(object_id):
                        item["SyncToken"] = str(int(item.get("SyncToken", "0")) + 1)
                raise StaleObjectError("Synthetic stale token")
            if str(expected_sync_token) != str(current.get("SyncToken", "")):
                raise StaleObjectError("Synthetic stale token")
            for item in self.objects[object_type]:
                if str(item.get("Id")) == str(object_id):
                    item.update(copy.deepcopy(payload))
                    item["SyncToken"] = str(int(item.get("SyncToken", "0")) + 1)
                    stored = item
                    break
        else:
            stored = copy.deepcopy(payload)
            requested_id = stored.get("Id")
            if requested_id and any(str(row.get("Id")) == str(requested_id)
                                    for row in self.objects.setdefault(object_type, [])):
                # QBO assigns ids on create. The evidence id is retained only
                # in the deterministic request id, never sent as a provider id.
                stored.pop("Id", None)
            stored["Id"] = requested_id or f"SL-{len(self.objects.setdefault(object_type, [])) + 1}"
            stored["SyncToken"] = "0"
            self.objects[object_type].append(stored)

        result = MutationResult(object_type, str(stored["Id"]), str(stored["SyncToken"]),
                                copy.deepcopy(stored))
        self._results[idempotency_key] = result
        self.applied_request_ids.add(idempotency_key)
        if self.fail_after_commit_once and idempotency_key not in self._uncertain_seen:
            self._uncertain_seen.add(idempotency_key)
            raise UncertainWriteError("Synthetic connection dropped after commit")
        return result
