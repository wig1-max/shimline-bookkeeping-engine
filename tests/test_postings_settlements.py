"""The settlement and reversal documents, which used to block every ledger.

BillPayment, Transfer, SalesReceipt, RefundReceipt, CreditMemo and VendorCredit
were read by nothing and derived by nothing, so any client who had paid a bill
or moved money between their own accounts -- which is every client with a
chequing account -- got the whole reconstruction refused. Reconciliation worked
only on unusually tidy books.

Each of these is checked three ways: that it posts correctly, that it balances,
and that it refuses the moment the document stops saying where the money went.
"""
import unittest
from decimal import Decimal

from shimline import postings

CHART = [
    {"Id": "35", "Name": "Chequing", "AccountType": "Bank"},
    {"Id": "36", "Name": "Savings", "AccountType": "Bank"},
    {"Id": "40", "Name": "Visa", "AccountType": "Credit Card"},
    {"Id": "84", "Name": "Accounts Receivable", "AccountType": "Accounts Receivable"},
    {"Id": "33", "Name": "Accounts Payable", "AccountType": "Accounts Payable"},
    {"Id": "89", "Name": "GST/HST Payable", "AccountType": "Other Current Liability"},
    {"Id": "79", "Name": "Contract Revenue", "AccountType": "Income"},
    {"Id": "64", "Name": "Materials", "AccountType": "Cost of Goods Sold"},
]


def _objects(**kinds):
    return dict({"Account": list(CHART)}, **kinds)


def _expense(amount, account="64"):
    return {"Amount": amount, "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account}}}


def _income(amount, account="79"):
    return {"Amount": amount, "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": {"ItemAccountRef": {"value": account}}}


def derive(**kinds):
    return postings.derive(_objects(**kinds))


def refuse(testcase, **kinds):
    ledger = derive(**kinds)
    testcase.assertFalse(ledger.complete, ledger.balances)
    return " ".join(ledger.reasons())


class BillPaymentTests(unittest.TestCase):

    def test_a_cheque_reduces_payables_and_the_bank(self):
        ledger = derive(BillPayment=[{
            "Id": "BP1", "TxnDate": "2026-08-20", "TotalAmt": 500.00,
            "PayType": "Check",
            "CheckPayment": {"BankAccountRef": {"value": "35"}}}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances, {"33": Decimal("500.00"),
                                           "35": Decimal("-500.00")})

    def test_a_card_payment_moves_the_debt_rather_than_the_cash(self):
        ledger = derive(BillPayment=[{
            "Id": "BP1", "TxnDate": "2026-08-20", "TotalAmt": 250.00,
            "PayType": "CreditCard",
            "CreditCardPayment": {"CCAccountRef": {"value": "40"}}}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["40"], Decimal("-250.00"))
        self.assertEqual(ledger.balances["33"], Decimal("250.00"))
        # Paying a bill on the card takes nothing out of the bank.
        self.assertNotIn("35", ledger.balances)

    def test_a_payment_naming_no_account_is_refused(self):
        self.assertIn("does not name the account it was paid from", refuse(
            self, BillPayment=[{"Id": "BP1", "TotalAmt": 500.00, "PayType": "Check"}]))

    def test_a_payment_naming_two_different_accounts_is_refused(self):
        """Both a cheque and a card on one document: picking one would put the
        money in an account it never left."""
        self.assertIn("does not name the account it was paid from", refuse(
            self, BillPayment=[{
                "Id": "BP1", "TotalAmt": 500.00,
                "CheckPayment": {"BankAccountRef": {"value": "35"}},
                "CreditCardPayment": {"CCAccountRef": {"value": "40"}}}]))

    def test_an_explicit_payables_account_overrides_the_default(self):
        chart = list(CHART) + [{"Id": "34", "Name": "A/P (US)",
                                "AccountType": "Accounts Payable"}]
        ledger = postings.derive({"Account": chart, "BillPayment": [{
            "Id": "BP1", "TotalAmt": 500.00, "APAccountRef": {"value": "34"},
            "CheckPayment": {"BankAccountRef": {"value": "35"}}}]})
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["34"], Decimal("500.00"))

    def test_two_payables_accounts_and_no_choice_is_refused(self):
        chart = list(CHART) + [{"Id": "34", "Name": "A/P (US)",
                                "AccountType": "Accounts Payable"}]
        ledger = postings.derive({"Account": chart, "BillPayment": [{
            "Id": "BP1", "TotalAmt": 500.00,
            "CheckPayment": {"BankAccountRef": {"value": "35"}}}]})
        self.assertFalse(ledger.complete)
        self.assertIn("no single A/P in the chart", " ".join(ledger.reasons()))


class TransferTests(unittest.TestCase):

    def test_money_moved_between_own_accounts_nets_to_nothing(self):
        ledger = derive(Transfer=[{
            "Id": "T1", "TxnDate": "2026-08-15", "Amount": 2000.00,
            "FromAccountRef": {"value": "35"}, "ToAccountRef": {"value": "36"}}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["35"], Decimal("-2000.00"))
        self.assertEqual(ledger.balances["36"], Decimal("2000.00"))
        self.assertEqual(sum(ledger.balances.values()), Decimal("0.00"))

    def test_a_transfer_reads_amount_not_total_amt(self):
        """QuickBooks carries `Amount` on a transfer. Reading TotalAmt would
        post a zero and silently lose the movement."""
        self.assertIn("carries no amount", refuse(self, Transfer=[{
            "Id": "T1", "TotalAmt": 2000.00,
            "FromAccountRef": {"value": "35"}, "ToAccountRef": {"value": "36"}}]))

    def test_a_transfer_missing_a_side_is_refused(self):
        self.assertIn("must name both accounts", refuse(self, Transfer=[{
            "Id": "T1", "Amount": 100.00, "FromAccountRef": {"value": "35"}}]))

    def test_a_transfer_to_itself_is_refused(self):
        self.assertIn("same account on both sides", refuse(self, Transfer=[{
            "Id": "T1", "Amount": 100.00,
            "FromAccountRef": {"value": "35"}, "ToAccountRef": {"value": "35"}}]))


class SalesReceiptTests(unittest.TestCase):

    def test_a_till_sale_books_cash_revenue_and_tax(self):
        ledger = derive(SalesReceipt=[{
            "Id": "SR1", "TxnDate": "2026-08-09", "TotalAmt": 113.00,
            "DepositToAccountRef": {"value": "35"},
            "Line": [_income(100.00)],
            "TxnTaxDetail": {"TotalTax": 13.00}}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["35"], Decimal("113.00"))
        self.assertEqual(ledger.balances["79"], Decimal("-100.00"))
        self.assertEqual(ledger.balances["89"], Decimal("-13.00"))

    def test_undeposited_funds_is_refused_rather_than_guessed(self):
        """QuickBooks omits the ref and defaults to Undeposited Funds without
        naming it. Guessing which account that is puts the cash somewhere it
        is not."""
        self.assertIn("Undeposited Funds", refuse(self, SalesReceipt=[{
            "Id": "SR1", "TotalAmt": 113.00, "Line": [_income(113.00)]}]))

    def test_a_receipt_with_no_income_lines_is_refused(self):
        self.assertIn("no income lines", refuse(self, SalesReceipt=[{
            "Id": "SR1", "TotalAmt": 113.00,
            "DepositToAccountRef": {"value": "35"}, "Line": []}]))


class ReversalTests(unittest.TestCase):
    """A credit note is its forward document run backwards, to the cent."""

    def test_a_credit_memo_exactly_reverses_an_invoice(self):
        invoice = derive(Invoice=[{
            "Id": "I1", "TxnDate": "2026-08-04", "TotalAmt": 113.00,
            "Line": [_income(100.00)], "TxnTaxDetail": {"TotalTax": 13.00}}])
        memo = derive(CreditMemo=[{
            "Id": "CM1", "TxnDate": "2026-08-11", "TotalAmt": 113.00,
            "Line": [_income(100.00)], "TxnTaxDetail": {"TotalTax": 13.00}}])
        self.assertTrue(memo.complete, memo.reasons())
        self.assertEqual(memo.balances,
                         {key: -value for key, value in invoice.balances.items()})

    def test_a_vendor_credit_exactly_reverses_a_bill(self):
        bill = derive(Bill=[{
            "Id": "B1", "TxnDate": "2026-08-04", "TotalAmt": 226.00,
            "Line": [_expense(200.00)], "TxnTaxDetail": {"TotalTax": 26.00}}])
        credit = derive(VendorCredit=[{
            "Id": "VC1", "TxnDate": "2026-08-11", "TotalAmt": 226.00,
            "Line": [_expense(200.00)], "TxnTaxDetail": {"TotalTax": 26.00}}])
        self.assertTrue(credit.complete, credit.reasons())
        self.assertEqual(credit.balances,
                         {key: -value for key, value in bill.balances.items()})

    def test_a_refund_receipt_exactly_reverses_a_sales_receipt(self):
        sale = derive(SalesReceipt=[{
            "Id": "SR1", "TotalAmt": 113.00,
            "DepositToAccountRef": {"value": "35"},
            "Line": [_income(100.00)], "TxnTaxDetail": {"TotalTax": 13.00}}])
        refund = derive(RefundReceipt=[{
            "Id": "RR1", "TotalAmt": 113.00,
            "DepositToAccountRef": {"value": "35"},
            "Line": [_income(100.00)], "TxnTaxDetail": {"TotalTax": 13.00}}])
        self.assertTrue(refund.complete, refund.reasons())
        self.assertEqual(refund.balances,
                         {key: -value for key, value in sale.balances.items()})

    def test_a_credit_memo_with_two_receivables_and_no_choice_is_refused(self):
        chart = list(CHART) + [{"Id": "85", "Name": "A/R (retainage)",
                                "AccountType": "Accounts Receivable"}]
        ledger = postings.derive({"Account": chart, "CreditMemo": [{
            "Id": "CM1", "TotalAmt": 100.00, "Line": [_income(100.00)]}]})
        self.assertFalse(ledger.complete)
        self.assertIn("no single A/R in the chart", " ".join(ledger.reasons()))


class SalesTaxAccountTests(unittest.TestCase):
    """The fix that turned a wall back into a fallback."""

    def test_a_tax_line_naming_a_rate_does_not_block_a_single_tax_account(self):
        """TaxLine carries a TaxRateRef -- the id of a rate, not an account.
        Trusting it made every taxed document fail the chart check."""
        ledger = derive(Invoice=[{
            "Id": "I1", "TotalAmt": 113.00, "Line": [_income(100.00)],
            "TxnTaxDetail": {"TotalTax": 13.00, "TaxLine": [
                {"Amount": 13.00, "DetailType": "TaxLineDetail",
                 "TaxLineDetail": {"TaxRateRef": {"value": "TAXRATE-7"}}}]}}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["89"], Decimal("-13.00"))

    def test_a_tax_line_naming_a_real_account_is_used(self):
        chart = list(CHART) + [{"Id": "90", "Name": "PST Payable",
                                "AccountType": "Other Current Liability"}]
        ledger = postings.derive({"Account": chart, "Invoice": [{
            "Id": "I1", "TotalAmt": 113.00, "Line": [_income(100.00)],
            "TxnTaxDetail": {"TotalTax": 13.00, "TaxLine": [
                {"Amount": 13.00, "DetailType": "TaxLineDetail",
                 "TaxLineDetail": {"AccountRef": {"value": "90"}}}]}}]})
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["90"], Decimal("-13.00"))

    def test_two_tax_accounts_and_no_statement_is_still_refused(self):
        chart = list(CHART) + [{"Id": "90", "Name": "PST Payable",
                                "AccountType": "Other Current Liability"}]
        ledger = postings.derive({"Account": chart, "Invoice": [{
            "Id": "I1", "TotalAmt": 113.00, "Line": [_income(100.00)],
            "TxnTaxDetail": {"TotalTax": 13.00}}]})
        self.assertFalse(ledger.complete)
        self.assertIn("no identifiable tax account", " ".join(ledger.reasons()))


class WholeMonthTests(unittest.TestCase):

    def test_an_ordinary_contractor_month_reconstructs_and_balances(self):
        """The case that used to be refused outright: a month with a bill paid,
        a transfer to savings, a till sale and a supplier credit."""
        ledger = derive(
            Invoice=[{"Id": "I1", "TxnDate": "2026-08-04", "TotalAmt": 1130.00,
                      "Line": [_income(1000.00)],
                      "TxnTaxDetail": {"TotalTax": 130.00}}],
            Payment=[{"Id": "PM1", "TxnDate": "2026-08-20", "TotalAmt": 1130.00,
                      "DepositToAccountRef": {"value": "35"}}],
            Bill=[{"Id": "B1", "TxnDate": "2026-08-06", "TotalAmt": 500.00,
                   "Line": [_expense(500.00)]}],
            BillPayment=[{"Id": "BP1", "TxnDate": "2026-08-25", "TotalAmt": 400.00,
                          "CheckPayment": {"BankAccountRef": {"value": "35"}}}],
            VendorCredit=[{"Id": "VC1", "TxnDate": "2026-08-28", "TotalAmt": 100.00,
                           "Line": [_expense(100.00)]}],
            Transfer=[{"Id": "T1", "TxnDate": "2026-08-29", "Amount": 300.00,
                       "FromAccountRef": {"value": "35"},
                       "ToAccountRef": {"value": "36"}}],
            SalesReceipt=[{"Id": "SR1", "TxnDate": "2026-08-30", "TotalAmt": 226.00,
                           "DepositToAccountRef": {"value": "35"},
                           "Line": [_income(200.00)],
                           "TxnTaxDetail": {"TotalTax": 26.00}}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.transactions_seen, 7)
        # Double entry holds across the whole month.
        self.assertEqual(sum(ledger.balances.values()), Decimal("0.00"))
        # The bill was 500, 400 was paid and 100 credited: nothing left owing.
        self.assertEqual(ledger.balances["33"], Decimal("0.00"))
        # Bank: +1130 payment -400 bill payment -300 transfer +226 till sale.
        self.assertEqual(ledger.balances["35"], Decimal("656.00"))
        self.assertEqual(ledger.balances["36"], Decimal("300.00"))


if __name__ == "__main__":
    unittest.main()
