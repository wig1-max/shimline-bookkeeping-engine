"""Shapes a real QuickBooks file contains and a fixture never produces.

Written by probing the derivation with states nobody had put in front of it: an
abandoned document, a line with no amount, a foreign-currency bill, a voided
entry, a discount line. Each of these is ordinary in a real company file and
none of them was in the test data.

One of the five was a silent error of 37%.
"""
import unittest
from decimal import Decimal

from shimline.postings import derive_ledger
from shimline.qbo_adapter import declare_pull

CHART = [
    {"Id": "35", "Name": "Chequing", "AccountType": "Bank"},
    {"Id": "64", "Name": "Materials", "AccountType": "Cost of Goods Sold"},
    {"Id": "84", "Name": "A/R", "AccountType": "Accounts Receivable"},
    {"Id": "33", "Name": "A/P", "AccountType": "Accounts Payable"},
    {"Id": "79", "Name": "Revenue", "AccountType": "Income"},
]


def expense(amount, account="64"):
    return {"Amount": amount, "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account}}}


def income(amount, account="79"):
    return {"Amount": amount, "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": {"ItemAccountRef": {"value": account}}}


def derive(**kinds):
    return derive_ledger(declare_pull({"Account": list(CHART), **kinds},
                                      source="fixture"))


class ForeignCurrency(unittest.TestCase):
    """The one that was a silent 37% error."""

    def _usd_bill(self, rate=1.37):
        return {"Id": "B1", "TxnDate": "2026-08-01", "TotalAmt": 100,
                "CurrencyRef": {"value": "USD"}, "ExchangeRate": rate,
                "Line": [expense(100)]}

    def test_a_foreign_currency_document_blocks_the_ledger(self):
        """Its TotalAmt and every line are in the transaction currency. Posting
        them unconverted understates the entry by the whole exchange rate."""
        ledger = derive(Bill=[self._usd_bill()])
        self.assertFalse(ledger.complete)
        reason = " ".join(ledger.reasons())
        self.assertIn("USD", reason)
        self.assertIn("1.37", reason)

    def test_the_error_it_prevents_would_have_balanced_perfectly(self):
        """Why nothing else could have caught it: both sides carry the same
        wrong number, so double entry holds and Beancount recomputes the same
        wrong postings and agrees."""
        wrong = {"33": Decimal("-100.00"), "64": Decimal("100.00")}
        self.assertEqual(sum(wrong.values()), Decimal("0.00"))
        # And the truth, which is 37% larger.
        truth = {"33": Decimal("-137.00"), "64": Decimal("137.00")}
        self.assertEqual(sum(truth.values()), Decimal("0.00"))
        self.assertNotEqual(wrong, truth)

    def test_a_home_currency_document_is_unaffected(self):
        """Every single-currency client must be untouched by this refusal."""
        ledger = derive(Bill=[{"Id": "B2", "TxnDate": "2026-08-01",
                               "TotalAmt": 100, "CurrencyRef": {"value": "CAD"},
                               "ExchangeRate": 1, "Line": [expense(100)]}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["64"], Decimal("100.00"))

    def test_an_absent_exchange_rate_is_home_currency(self):
        ledger = derive(Bill=[{"Id": "B3", "TxnDate": "2026-08-01",
                               "TotalAmt": 100, "Line": [expense(100)]}])
        self.assertTrue(ledger.complete, ledger.reasons())

    def test_a_rate_of_one_expressed_any_way_is_home_currency(self):
        for rate in (1, 1.0, "1", "1.0", Decimal("1.000")):
            with self.subTest(rate=rate):
                ledger = derive(Bill=[{"Id": "B4", "TxnDate": "2026-08-01",
                                       "TotalAmt": 100, "ExchangeRate": rate,
                                       "Line": [expense(100)]}])
                self.assertTrue(ledger.complete, ledger.reasons())

    def test_an_unreadable_rate_blocks_rather_than_being_treated_as_one(self):
        ledger = derive(Bill=[{"Id": "B5", "TxnDate": "2026-08-01",
                               "TotalAmt": 100, "ExchangeRate": "about 1.4",
                               "CurrencyRef": {"value": "USD"},
                               "Line": [expense(100)]}])
        self.assertFalse(ledger.complete)


class DegenerateDocuments(unittest.TestCase):
    """An artefact that moves no account must not block a client's books."""

    def test_an_abandoned_document_posts_nothing_and_blocks_nothing(self):
        ledger = derive(Purchase=[{"Id": "P1", "TxnDate": "2026-08-01",
                                   "TotalAmt": 0, "AccountRef": {"value": "35"},
                                   "Line": []}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances, {})

    def test_it_does_not_swallow_a_real_document_alongside_it(self):
        ledger = derive(Purchase=[
            {"Id": "P1", "TxnDate": "2026-08-01", "TotalAmt": 0,
             "AccountRef": {"value": "35"}, "Line": []},
            {"Id": "P2", "TxnDate": "2026-08-02", "TotalAmt": 200,
             "AccountRef": {"value": "35"}, "Line": [expense(200)]}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["64"], Decimal("200.00"))

    def test_a_stated_total_with_no_lines_still_blocks(self):
        """The total is what separates an artefact from a real document nobody
        can explain. A hundred dollars with nothing saying where it went is
        underivable, not empty."""
        ledger = derive(Purchase=[{"Id": "P3", "TxnDate": "2026-08-01",
                                   "TotalAmt": 100,
                                   "AccountRef": {"value": "35"}, "Line": []}])
        self.assertFalse(ledger.complete)

    def test_a_zero_total_carrying_tax_still_blocks(self):
        """Tax with no lines is not an abandoned document."""
        ledger = derive(Bill=[{"Id": "B6", "TxnDate": "2026-08-01",
                               "TotalAmt": 0, "Line": [],
                               "TxnTaxDetail": {"TotalTax": 13.00}}])
        self.assertFalse(ledger.complete)

    def test_a_transfer_of_nothing_is_still_refused(self):
        """Transfers carry Amount rather than TotalAmt, so the artefact rule
        must not let a zero-amount transfer through as 'posts nothing'."""
        ledger = derive(Transfer=[{"Id": "T1", "TxnDate": "2026-08-01",
                                   "Amount": 0,
                                   "FromAccountRef": {"value": "35"},
                                   "ToAccountRef": {"value": "33"}}])
        self.assertFalse(ledger.complete)


class LinesWithNoAmount(unittest.TestCase):

    def test_a_line_carrying_no_amount_blocks_rather_than_being_dropped(self):
        """Dropping it would leave a journal entry that still balances while
        missing a posting -- the one failure the document balance check cannot
        catch."""
        ledger = derive(Purchase=[{
            "Id": "P4", "TxnDate": "2026-08-01", "TotalAmt": 100,
            "AccountRef": {"value": "35"},
            "Line": [{"Amount": None,
                      "DetailType": "AccountBasedExpenseLineDetail",
                      "AccountBasedExpenseLineDetail": {
                          "AccountRef": {"value": "64"}}}]}])
        self.assertFalse(ledger.complete)
        self.assertIn("carrying no amount", " ".join(ledger.reasons()))

    def test_a_journal_entry_that_would_still_balance_is_caught(self):
        """The specific case: two good lines that balance, plus a third with no
        amount. Dropping the third leaves a balanced entry missing a posting."""
        ledger = derive(JournalEntry=[{
            "Id": "J1", "TxnDate": "2026-08-01", "Line": [
                {"Amount": 50, "DetailType": "JournalEntryLineDetail",
                 "JournalEntryLineDetail": {"PostingType": "Debit",
                                            "AccountRef": {"value": "64"}}},
                {"Amount": 50, "DetailType": "JournalEntryLineDetail",
                 "JournalEntryLineDetail": {"PostingType": "Credit",
                                            "AccountRef": {"value": "35"}}},
                {"Amount": None, "DetailType": "JournalEntryLineDetail",
                 "JournalEntryLineDetail": {"PostingType": "Debit",
                                            "AccountRef": {"value": "79"}}}]}])
        self.assertFalse(ledger.complete)

    def test_subtotal_and_description_rows_are_still_skipped(self):
        """They carry no money by design and must not be confused with a line
        that should carry money and does not."""
        ledger = derive(Purchase=[{
            "Id": "P5", "TxnDate": "2026-08-01", "TotalAmt": 100,
            "AccountRef": {"value": "35"},
            "Line": [expense(100),
                     {"DetailType": "SubTotalLineDetail", "Amount": 100},
                     {"DetailType": "DescriptionOnly",
                      "Description": "thanks"}]}])
        self.assertTrue(ledger.complete, ledger.reasons())


class ShapesThatAlreadyWorked(unittest.TestCase):
    """Recorded because they were checked, not because they were fixed."""

    def test_a_voided_document_posts_zeroes(self):
        ledger = derive(Invoice=[{"Id": "I1", "TxnDate": "2026-08-01",
                                  "TotalAmt": 0, "PrivateNote": "Voided.",
                                  "Line": [income(0)]}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["84"], Decimal("0.00"))

    def test_a_discount_line_reduces_the_expense(self):
        ledger = derive(Purchase=[{"Id": "P6", "TxnDate": "2026-08-01",
                                   "TotalAmt": 90, "AccountRef": {"value": "35"},
                                   "Line": [expense(100), expense(-10)]}])
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["64"], Decimal("90.00"))
        self.assertEqual(ledger.balances["35"], Decimal("-90.00"))

    def test_a_document_with_no_date_still_derives(self):
        """The postings are right. Placing it in a period is a separate
        problem, and `matching.cash_movements` refuses it there."""
        ledger = derive(Purchase=[{"Id": "P7", "TotalAmt": 100,
                                   "AccountRef": {"value": "35"},
                                   "Line": [expense(100)]}])
        self.assertTrue(ledger.complete, ledger.reasons())


if __name__ == "__main__":
    unittest.main()
