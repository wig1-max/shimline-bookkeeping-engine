"""Every way the derivation can refuse, fired at least once.

Measured with coverage rather than assumed: **16 of the 33 refusal sites in
`postings.py` had never executed.** An untested refusal path is worse than an
untested happy path. It fires precisely when a client is blocked, its message is
the only thing an operator has to go on, and the all-or-nothing design means one
of them takes out the whole ledger. A message nobody has ever read is a message
nobody has ever checked is true.

The guard at the bottom is the point of the file. It enumerates the raise sites
from the source and asserts that this module fired every one, so adding a new
refusal without exercising it fails the build rather than shipping a sentence no
one has seen.
"""
import ast
import inspect
import pathlib
import unittest

from shimline import postings
from shimline.postings import derive_ledger
from shimline.qbo_adapter import declare_pull

SOURCE = pathlib.Path(postings.__file__)

CHART = [
    {"Id": "35", "Name": "Chequing", "AccountType": "Bank"},
    {"Id": "64", "Name": "Materials", "AccountType": "Cost of Goods Sold"},
    {"Id": "84", "Name": "A/R", "AccountType": "Accounts Receivable"},
    {"Id": "33", "Name": "A/P", "AccountType": "Accounts Payable"},
    {"Id": "79", "Name": "Revenue", "AccountType": "Income"},
    {"Id": "89", "Name": "GST/HST Payable", "AccountType": "Other Current Liability"},
]
# A second of each, so a document that does not name its own lands on "there is
# more than one and guessing would be a plausible wrong answer".
SECOND_AR = {"Id": "85", "Name": "A/R retainage", "AccountType": "Accounts Receivable"}
SECOND_AP = {"Id": "34", "Name": "A/P other", "AccountType": "Accounts Payable"}
SECOND_TAX = {"Id": "90", "Name": "PST Payable", "AccountType": "Other Current Liability"}

TAX = {"TotalTax": 13.00}


def income(amount=100.00, account="79"):
    return {"Amount": amount, "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": {"ItemAccountRef": {"value": account}}}


def expense(amount=100.00, account="64"):
    return {"Amount": amount, "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account}}}


def refuse(chart=None, **kinds):
    """Derive a one-document company and return the reasons it refused."""
    objects = declare_pull({"Account": list(chart or CHART), **kinds},
                           source="fixture")
    ledger = derive_ledger(objects)
    return ledger, " ".join(ledger.reasons())


def _refusal_sites() -> set[int]:
    """Line numbers of every `raise _Undecidable(...)` / `_PostsNothing`."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if getattr(target, "id", "") in {"_Undecidable", "_PostsNothing"}:
            found.add(node.lineno)
    return found


class _Recorder:
    """Records which raise site produced each refusal.

    `derive_ledger` catches `_Undecidable` and turns it into a reason string, so
    no traceback escapes to inspect. Wrapping the constructor catches it at the
    moment of the raise, where the caller's frame is still the raise site.
    """

    def __init__(self):
        self.lines: set[int] = set()

    def __enter__(self):
        self._original = postings._Undecidable.__init__
        self._original_posts = postings._PostsNothing.__init__
        recorder = self

        def record(cls_init):
            def wrapped(self, *args, **kwargs):
                recorder.lines.add(inspect.currentframe().f_back.f_lineno)
                cls_init(self, *args, **kwargs)
            return wrapped

        postings._Undecidable.__init__ = record(self._original)
        postings._PostsNothing.__init__ = record(self._original_posts)
        return self

    def __exit__(self, *_exc):
        postings._Undecidable.__init__ = self._original
        postings._PostsNothing.__init__ = self._original_posts
        return False


class EveryRefusalPathFires(unittest.TestCase):
    """One test, because the guard has to run after every case has had its turn."""

    def test_every_refusal_the_engine_can_produce_is_exercised(self):
        with _Recorder() as recorder:
            for name, run in sorted(vars(type(self)).items()):
                if name.startswith("case_"):
                    with self.subTest(case=name[5:]):
                        run(self)

            sites = _refusal_sites()
            # An enumerator that finds nothing proves nothing.
            self.assertGreater(len(sites), 25,
                               "the AST walk found almost no refusal sites, so "
                               "this guard is not looking at the real module")
            missed = sorted(sites - recorder.lines)
            self.assertEqual(
                missed, [],
                "These refusal paths never fired, so nobody has read the "
                "sentence an operator sees when a client's ledger blocks on "
                "them. Add a case above for each line: "
                + ", ".join(f"{SOURCE.name}:{line}" for line in missed))

    # ----------------------------------------------------- shared to all kinds

    def case_a_line_with_no_amount(self):
        _, why = refuse(Purchase=[{"Id": "P1", "TxnDate": "2026-08-01",
                                   "TotalAmt": 100, "AccountRef": {"value": "35"},
                                   "Line": [dict(expense(), Amount=None)]}])
        self.assertIn("carrying no amount", why)

    def case_an_abandoned_document_posts_nothing(self):
        ledger, _ = refuse(Purchase=[{"Id": "P0", "TxnDate": "2026-08-01",
                                      "TotalAmt": 0,
                                      "AccountRef": {"value": "35"}, "Line": []}])
        self.assertTrue(ledger.complete)

    def case_a_document_with_neither_total_nor_lines(self):
        ledger, why = refuse(BillPayment=[{"Id": "BP0"}])
        self.assertFalse(ledger.complete)
        self.assertIn("states no total and carries no lines", why)
        self.assertIn("arrived incomplete", why)

    def case_a_foreign_currency_document(self):
        _, why = refuse(Bill=[{"Id": "B1", "TxnDate": "2026-08-01", "TotalAmt": 100,
                               "CurrencyRef": {"value": "USD"}, "ExchangeRate": 1.37,
                               "Line": [expense()]}])
        self.assertIn("USD", why)

    def case_no_derivation_rule_at_all(self):
        """The backstop. Unreachable through `derive_ledger` because every
        POSTING_TYPE has a rule -- which is exactly why it needs firing here, or
        it is a sentence that only appears the day somebody adds a type and
        forgets the rule."""
        with self.assertRaises(postings._Undecidable) as raised:
            postings._derive_one("Cheque", {"TotalAmt": 1, "Line": [expense()]},
                                 {}, None, None, set())
        self.assertIn("no derivation rule for Cheque", str(raised.exception))

    # ------------------------------------------------------------ by document

    def case_journal_line_without_an_account_or_posting_type(self):
        _, why = refuse(JournalEntry=[{"Id": "J1", "TxnDate": "2026-08-01", "Line": [
            {"Amount": 50, "DetailType": "JournalEntryLineDetail",
             "JournalEntryLineDetail": {"AccountRef": {"value": "64"}}}]}])
        self.assertIn("no account or no posting type", why)

    def case_journal_entry_with_no_posting_lines(self):
        _, why = refuse(JournalEntry=[{
            "Id": "J2", "TxnDate": "2026-08-01", "TotalAmt": 10,
            "Line": [{"DetailType": "DescriptionOnly", "Description": "note"}]}])
        self.assertIn("no posting lines", why)

    def case_purchase_without_a_funding_account(self):
        _, why = refuse(Purchase=[{"Id": "P2", "TxnDate": "2026-08-01",
                                   "TotalAmt": 100, "Line": [expense()]}])
        self.assertIn("funding account is unknown", why)

    def case_purchase_with_tax_and_two_tax_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_TAX],
                        Purchase=[{"Id": "P3", "TxnDate": "2026-08-01",
                                   "TotalAmt": 113, "AccountRef": {"value": "35"},
                                   "Line": [expense()], "TxnTaxDetail": dict(TAX)}])
        self.assertIn("no identifiable tax account", why)

    def case_bill_with_two_payable_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_AP],
                        Bill=[{"Id": "B2", "TxnDate": "2026-08-01",
                               "TotalAmt": 100, "Line": [expense()]}])
        self.assertIn("no single A/P in the chart", why)

    def case_bill_with_tax_and_two_tax_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_TAX],
                        Bill=[{"Id": "B3", "TxnDate": "2026-08-01", "TotalAmt": 113,
                               "APAccountRef": {"value": "33"},
                               "Line": [expense()], "TxnTaxDetail": dict(TAX)}])
        self.assertIn("no identifiable tax account", why)

    def case_invoice_with_two_receivable_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_AR],
                        Invoice=[{"Id": "I1", "TxnDate": "2026-08-01",
                                  "TotalAmt": 100, "Line": [income()]}])
        self.assertIn("no single A/R in the chart", why)

    def case_invoice_with_tax_and_two_tax_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_TAX],
                        Invoice=[{"Id": "I2", "TxnDate": "2026-08-01",
                                  "TotalAmt": 113, "ARAccountRef": {"value": "84"},
                                  "Line": [income()], "TxnTaxDetail": dict(TAX)}])
        self.assertIn("no identifiable tax account", why)

    def case_payment_with_nowhere_to_deposit(self):
        _, why = refuse(Payment=[{"Id": "PAY1", "TxnDate": "2026-08-01",
                                  "TotalAmt": 100, "Line": [income()]}])
        self.assertIn("receiving account is unknown", why)

    def case_payment_with_two_receivable_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_AR],
                        Payment=[{"Id": "PAY2", "TxnDate": "2026-08-01",
                                  "TotalAmt": 100,
                                  "DepositToAccountRef": {"value": "35"},
                                  "Line": [income()]}])
        self.assertIn("no A/R account on the payment", why)

    def case_deposit_with_nowhere_to_deposit(self):
        _, why = refuse(Deposit=[{"Id": "D1", "TxnDate": "2026-08-01",
                                  "TotalAmt": 100, "Line": [income()]}])
        self.assertIn("receiving account is unknown", why)

    def case_deposit_line_naming_no_account(self):
        _, why = refuse(Deposit=[{"Id": "D2", "TxnDate": "2026-08-01",
                                  "TotalAmt": 100,
                                  "DepositToAccountRef": {"value": "35"},
                                  "Line": [{"Amount": 100,
                                            "DetailType": "DepositLineDetail",
                                            "DepositLineDetail": {}}]}])
        self.assertIn("deposit line names no account", why)

    def case_bill_payment_with_two_payable_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_AP],
                        BillPayment=[{"Id": "BP1", "TxnDate": "2026-08-01",
                                      "TotalAmt": 100,
                                      "CheckPayment": {
                                          "BankAccountRef": {"value": "35"}}}])
        self.assertIn("no A/P account on the bill payment", why)

    def case_bill_payment_that_does_not_say_what_paid_it(self):
        _, why = refuse(BillPayment=[{"Id": "BP2", "TxnDate": "2026-08-01",
                                      "TotalAmt": 100}])
        self.assertIn("does not name the account it was paid from", why)

    def case_transfer_missing_an_account(self):
        _, why = refuse(Transfer=[{"Id": "T1", "TxnDate": "2026-08-01",
                                   "Amount": 100,
                                   "FromAccountRef": {"value": "35"}}])
        self.assertIn("must name both accounts", why)

    def case_transfer_to_and_from_the_same_account(self):
        _, why = refuse(Transfer=[{"Id": "T2", "TxnDate": "2026-08-01",
                                   "Amount": 100,
                                   "FromAccountRef": {"value": "35"},
                                   "ToAccountRef": {"value": "35"}}])
        self.assertIn("same account on both sides", why)

    def case_transfer_of_nothing(self):
        _, why = refuse(Transfer=[{"Id": "T3", "TxnDate": "2026-08-01", "Amount": 0,
                                   "FromAccountRef": {"value": "35"},
                                   "ToAccountRef": {"value": "33"}}])
        self.assertIn("carries no amount", why)

    def case_sales_receipt_with_nowhere_to_put_the_cash(self):
        _, why = refuse(SalesReceipt=[{"Id": "SR1", "TxnDate": "2026-08-01",
                                       "TotalAmt": 100, "Line": [income()]}])
        self.assertIn("Undeposited", why)

    def case_sales_receipt_with_tax_and_two_tax_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_TAX],
                        SalesReceipt=[{"Id": "SR2", "TxnDate": "2026-08-01",
                                       "TotalAmt": 113,
                                       "DepositToAccountRef": {"value": "35"},
                                       "Line": [income()],
                                       "TxnTaxDetail": dict(TAX)}])
        self.assertIn("no identifiable tax account", why)

    def case_refund_with_no_account_to_come_out_of(self):
        _, why = refuse(RefundReceipt=[{"Id": "RR1", "TxnDate": "2026-08-01",
                                        "TotalAmt": 100, "Line": [income()]}])
        self.assertIn("no account named for the refund", why)

    def case_refund_with_tax_and_two_tax_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_TAX],
                        RefundReceipt=[{"Id": "RR2", "TxnDate": "2026-08-01",
                                        "TotalAmt": 113,
                                        "DepositToAccountRef": {"value": "35"},
                                        "Line": [income()],
                                        "TxnTaxDetail": dict(TAX)}])
        self.assertIn("no identifiable tax account", why)

    def case_credit_memo_with_two_receivable_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_AR],
                        CreditMemo=[{"Id": "CM1", "TxnDate": "2026-08-01",
                                     "TotalAmt": 100, "Line": [income()]}])
        self.assertIn("no A/R account on the credit memo", why)

    def case_credit_memo_with_tax_and_two_tax_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_TAX],
                        CreditMemo=[{"Id": "CM2", "TxnDate": "2026-08-01",
                                     "TotalAmt": 113,
                                     "ARAccountRef": {"value": "84"},
                                     "Line": [income()],
                                     "TxnTaxDetail": dict(TAX)}])
        self.assertIn("no identifiable tax account", why)

    def case_vendor_credit_with_two_payable_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_AP],
                        VendorCredit=[{"Id": "VC1", "TxnDate": "2026-08-01",
                                       "TotalAmt": 100, "Line": [expense()]}])
        self.assertIn("no A/P account on the vendor credit", why)

    def case_vendor_credit_with_tax_and_two_tax_accounts(self):
        _, why = refuse(chart=CHART + [SECOND_TAX],
                        VendorCredit=[{"Id": "VC2", "TxnDate": "2026-08-01",
                                       "TotalAmt": 113,
                                       "APAccountRef": {"value": "33"},
                                       "Line": [expense()],
                                       "TxnTaxDetail": dict(TAX)}])
        self.assertIn("no identifiable tax account", why)

    def case_income_line_naming_no_account(self):
        _, why = refuse(Invoice=[{"Id": "I3", "TxnDate": "2026-08-01",
                                  "TotalAmt": 100, "ARAccountRef": {"value": "84"},
                                  "Line": [{"Amount": 100,
                                            "DetailType": "SalesItemLineDetail",
                                            "SalesItemLineDetail": {}}]}])
        self.assertIn("income line names no account", why)

    def case_invoice_with_no_income_lines(self):
        """A hundred dollars owed with nothing saying what it was for. Not an
        abandoned document -- that one states no total and posts nothing."""
        _, why = refuse(Invoice=[{"Id": "I4", "TxnDate": "2026-08-01",
                                  "TotalAmt": 100, "ARAccountRef": {"value": "84"},
                                  "Line": []}])
        self.assertIn("no income lines to post", why)

    def case_expense_line_naming_no_account(self):
        _, why = refuse(Bill=[{"Id": "B4", "TxnDate": "2026-08-01",
                               "TotalAmt": 100, "APAccountRef": {"value": "33"},
                               "Line": [{"Amount": 100,
                                         "DetailType": "AccountBasedExpenseLineDetail",
                                         "AccountBasedExpenseLineDetail": {}}]}])
        self.assertIn("expense line names no account", why)

    def case_bill_with_no_expense_lines(self):
        _, why = refuse(Bill=[{"Id": "B5", "TxnDate": "2026-08-01",
                               "TotalAmt": 100, "APAccountRef": {"value": "33"},
                               "Line": []}])
        self.assertIn("no expense lines to post", why)


if __name__ == "__main__":
    unittest.main()
