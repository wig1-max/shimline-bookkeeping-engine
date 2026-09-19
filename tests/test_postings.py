"""Reconstructing double-entry postings from QuickBooks documents.

QuickBooks publishes documents, not postings. Rebuilding the postings is what
stands between this system and an automated close -- and a *partially* correct
reconstruction is worse than none, because one unhandled transaction type
silently shifts an account and a silently shifted balance is a wrong set of
books that looks right.

Most of these tests are therefore refusals. The value is in what the module
declines to reconstruct, not in what it can.
"""
import unittest
from decimal import Decimal

from shimline import postings, work_engine
from shimline.qbo_adapter import declare_pull

CHART = [
    {"Id": "35", "Name": "Chequing", "AccountType": "Bank"},
    {"Id": "84", "Name": "Accounts Receivable", "AccountType": "Accounts Receivable"},
    {"Id": "33", "Name": "Accounts Payable", "AccountType": "Accounts Payable"},
    {"Id": "89", "Name": "GST/HST Payable", "AccountType": "Other Current Liability"},
    {"Id": "79", "Name": "Contract Revenue", "AccountType": "Income"},
    {"Id": "64", "Name": "Materials", "AccountType": "Cost of Goods Sold"},
]


def _line(amount, account, detail="AccountBasedExpenseLineDetail"):
    return {"Amount": amount, "DetailType": detail,
            detail: {"AccountRef": {"value": account}}}


def _objects(**kinds):
    base = {"Account": list(CHART)}
    base.update(kinds)
    # Stamped as a complete pull, because the engine will not compute a ledger
    # from objects that cannot say which entity types were read -- an entity
    # that failed to load looks exactly like one with no rows.
    return declare_pull(base, source="fixture")


class DerivationTests(unittest.TestCase):
    def test_a_full_month_of_documents_reconstructs_and_balances(self):
        objects = _objects(
            Invoice=[{"Id": "1001", "TxnDate": "2026-08-04", "TotalAmt": 1130.00,
                      "Line": [{"Amount": 1000.00, "DetailType": "SalesItemLineDetail",
                                "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}},
                               {"Amount": 1130.00, "DetailType": "SubTotalLineDetail"}],
                      "TxnTaxDetail": {"TotalTax": 130.00}}],
            Purchase=[{"Id": "2001", "TxnDate": "2026-08-06", "TotalAmt": 226.00,
                       "AccountRef": {"value": "35"},
                       "Line": [_line(200.00, "64")],
                       "TxnTaxDetail": {"TotalTax": 26.00}}],
            Payment=[{"Id": "3001", "TxnDate": "2026-08-20", "TotalAmt": 1130.00,
                      "DepositToAccountRef": {"value": "35"}}],
            Bill=[{"Id": "4001", "TxnDate": "2026-08-10", "TotalAmt": 500.00,
                   "Line": [_line(500.00, "64")]}],
            JournalEntry=[{"Id": "5001", "TxnDate": "2026-08-31", "Line": [
                {"Amount": 50.00, "DetailType": "JournalEntryLineDetail",
                 "JournalEntryLineDetail": {"PostingType": "Debit",
                                            "AccountRef": {"value": "64"}}},
                {"Amount": 50.00, "DetailType": "JournalEntryLineDetail",
                 "JournalEntryLineDetail": {"PostingType": "Credit",
                                            "AccountRef": {"value": "35"}}}]}],
        )
        ledger = postings.derive(objects)
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.transactions_seen, 5)
        self.assertEqual(ledger.balances, {
            "33": Decimal("-500.00"),    # A/P: the unpaid bill
            "35": Decimal("854.00"),     # bank: -226 +1130 -50
            "64": Decimal("750.00"),     # materials: 200 +500 +50
            "79": Decimal("-1000.00"),   # revenue, net of tax
            "84": Decimal("0.00"),       # A/R raised then settled
            "89": Decimal("-104.00"),    # tax collected 130 less tax paid 26
        })

    def test_every_reconstruction_obeys_double_entry(self):
        objects = _objects(
            Invoice=[{"Id": "I1", "TotalAmt": 113.00,
                      "Line": [{"Amount": 100.00, "DetailType": "SalesItemLineDetail",
                                "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}],
                      "TxnTaxDetail": {"TotalTax": 13.00}}],
            Deposit=[{"Id": "D1", "TotalAmt": 40.00,
                      "DepositToAccountRef": {"value": "35"},
                      "Line": [{"Amount": 40.00, "DetailType": "DepositLineDetail",
                                "DepositLineDetail": {"AccountRef": {"value": "79"}}}]}],
        )
        ledger = postings.derive(objects)
        self.assertTrue(ledger.complete, ledger.reasons())
        # The whole trial balance must sum to zero, or it is not double entry.
        self.assertEqual(sum(ledger.balances.values()), Decimal("0.00"))

    def test_sales_tax_is_posted_not_folded_into_revenue(self):
        objects = _objects(
            Invoice=[{"Id": "I1", "TotalAmt": 113.00,
                      "Line": [{"Amount": 100.00, "DetailType": "SalesItemLineDetail",
                                "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}],
                      "TxnTaxDetail": {"TotalTax": 13.00}}])
        balances = postings.derive(objects).balances
        self.assertEqual(balances["79"], Decimal("-100.00"))
        self.assertEqual(balances["89"], Decimal("-13.00"))

    def test_subtotal_and_description_lines_are_not_posted_twice(self):
        objects = _objects(
            Purchase=[{"Id": "P1", "TotalAmt": 100.00, "AccountRef": {"value": "35"},
                       "Line": [_line(100.00, "64"),
                                {"Amount": 100.00, "DetailType": "SubTotalLineDetail"},
                                {"DetailType": "DescriptionOnly",
                                 "Description": "thanks for your business"}]}])
        ledger = postings.derive(objects)
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["64"], Decimal("100.00"))

    def test_existing_postings_are_used_rather_than_re_derived(self):
        # The synthetic oracle attaches its own. Re-deriving would test this
        # module against itself instead of against the oracle.
        objects = _objects(Purchase=[{
            "Id": "P1", "TotalAmt": 75.00, "AccountRef": {"value": "35"},
            "Line": [_line(75.00, "64")],
            "_Postings": [{"account": "64", "debit": "75", "credit": "0"},
                          {"account": "35", "debit": "0", "credit": "75"}]}])
        ledger = postings.derive(objects)
        self.assertTrue(ledger.complete)
        self.assertEqual(ledger.postings["Purchase:P1"][0]["debit"], "75")


class RefusalTests(unittest.TestCase):
    """What the module declines to reconstruct, and why."""

    def _reasons(self, **kinds):
        ledger = postings.derive(_objects(**kinds))
        self.assertFalse(ledger.complete)
        return " ".join(ledger.reasons())

    def test_an_unrecognised_type_blocks_the_whole_ledger(self):
        # The dangerous answer here is silence. A type nothing derives would
        # simply not appear in any balance, and the trial balance would still
        # tie -- a wrong set of books that passes every check we have.
        reasons = self._reasons(
            ReimburseCharge=[{"Id": "RC1", "TotalAmt": 500.00}],
            Purchase=[{"Id": "P1", "TotalAmt": 10.00, "AccountRef": {"value": "35"},
                       "Line": [_line(10.00, "64")]}])
        self.assertIn("ReimburseCharge", reasons)
        self.assertIn("not recognised", reasons)

    def test_every_unread_posting_type_is_named(self):
        for kind in postings.UNREAD_POSTING_TYPES:
            ledger = postings.derive(_objects(**{kind: [{"Id": "X"}]}))
            self.assertFalse(ledger.complete, kind)

    def test_every_type_the_adapter_reads_is_classified(self):
        """The two registries must together cover the pull exactly. A type in
        READ_OBJECTS and in neither list would block every client's ledger."""
        from shimline.qbo_adapter import READ_OBJECTS
        classified = (set(postings.POSTING_TYPES) | set(postings.UNREAD_POSTING_TYPES)
                      | set(postings.NON_POSTING_TYPES))
        self.assertEqual(set(READ_OBJECTS) - classified, set())

    def test_a_non_posting_list_does_not_block_anything(self):
        ledger = postings.derive(_objects(
            Item=[{"Id": "I1", "Name": "2x4"}],
            Estimate=[{"Id": "E1", "TotalAmt": 999.00}],
            Purchase=[{"Id": "P1", "TotalAmt": 10.00, "AccountRef": {"value": "35"},
                       "Line": [_line(10.00, "64")]}]))
        self.assertTrue(ledger.complete, ledger.reasons())

    def test_a_purchase_without_a_funding_account_is_refused(self):
        self.assertIn("funding account is unknown", self._reasons(
            Purchase=[{"Id": "P1", "TotalAmt": 10.00, "Line": [_line(10.00, "64")]}]))

    def test_tax_with_no_identifiable_tax_account_is_refused(self):
        # Two candidate liability accounts: picking one would be a guess.
        objects = {"Account": CHART + [{"Id": "90", "Name": "PST Payable",
                                        "AccountType": "Other Current Liability"}],
                   "Invoice": [{"Id": "I1", "TotalAmt": 113.00,
                                "Line": [{"Amount": 100.00, "DetailType": "SalesItemLineDetail",
                                          "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}],
                                "TxnTaxDetail": {"TotalTax": 13.00}}]}
        ledger = postings.derive(objects)
        self.assertFalse(ledger.complete)
        self.assertIn("no identifiable tax account", " ".join(ledger.reasons()))

    def test_a_second_receivable_account_makes_an_invoice_undecidable(self):
        objects = {"Account": CHART + [{"Id": "85", "Name": "A/R Retainage",
                                        "AccountType": "Accounts Receivable"}],
                   "Invoice": [{"Id": "I1", "TotalAmt": 100.00,
                                "Line": [{"Amount": 100.00, "DetailType": "SalesItemLineDetail",
                                          "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}]}]}
        ledger = postings.derive(objects)
        self.assertFalse(ledger.complete)
        self.assertIn("no single A/R", " ".join(ledger.reasons()))

    def test_naming_the_receivable_account_resolves_the_ambiguity(self):
        objects = {"Account": CHART + [{"Id": "85", "Name": "A/R Retainage",
                                        "AccountType": "Accounts Receivable"}],
                   "Invoice": [{"Id": "I1", "TotalAmt": 100.00,
                                "ARAccountRef": {"value": "85"},
                                "Line": [{"Amount": 100.00, "DetailType": "SalesItemLineDetail",
                                          "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}]}]}
        ledger = postings.derive(objects)
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(ledger.balances["85"], Decimal("100.00"))

    def test_a_line_posting_outside_the_chart_is_refused(self):
        self.assertIn("absent from the chart", self._reasons(
            Purchase=[{"Id": "P1", "TotalAmt": 10.00, "AccountRef": {"value": "35"},
                       "Line": [_line(10.00, "999")]}]))

    def test_lines_that_do_not_sum_to_the_total_are_refused(self):
        # The commonest silent corruption: a document whose parts do not add up.
        self.assertIn("do not balance", self._reasons(
            Purchase=[{"Id": "P1", "TotalAmt": 100.00, "AccountRef": {"value": "35"},
                       "Line": [_line(40.00, "64")]}]))

    def test_a_journal_line_without_a_posting_type_is_refused(self):
        self.assertIn("no posting type", self._reasons(
            JournalEntry=[{"Id": "J1", "Line": [
                {"Amount": 50.00, "DetailType": "JournalEntryLineDetail",
                 "JournalEntryLineDetail": {"AccountRef": {"value": "64"}}}]}]))

    def test_one_bad_document_blocks_the_ledger_rather_than_being_skipped(self):
        # Skipping it would leave a plausible balance missing one transaction.
        ledger = postings.derive(_objects(
            Purchase=[{"Id": "GOOD", "TotalAmt": 10.00, "AccountRef": {"value": "35"},
                       "Line": [_line(10.00, "64")]},
                      {"Id": "BAD", "TotalAmt": 10.00, "Line": [_line(10.00, "64")]}]))
        self.assertFalse(ledger.complete)
        self.assertEqual(len(ledger.unsupported), 1)


class ProviderAgreementTests(unittest.TestCase):
    """Our reconstruction has to equal QuickBooks' own trial balance."""

    def test_identical_balances_agree(self):
        both = {"35": Decimal("854.00"), "79": Decimal("-1000.00")}
        self.assertTrue(postings.compare_to_provider(both, dict(both)).agrees)

    def test_a_single_cent_of_drift_is_a_disagreement(self):
        ours = {"35": Decimal("854.00")}
        theirs = {"35": Decimal("854.01")}
        result = postings.compare_to_provider(ours, theirs)
        self.assertFalse(result.agrees)
        self.assertEqual(result.differences[0]["reason"], "balances differ")

    def test_a_balance_the_provider_reports_and_we_missed_is_caught(self):
        # The signature of an unread posting type.
        result = postings.compare_to_provider({}, {"33": Decimal("-500.00")})
        self.assertFalse(result.agrees)
        self.assertIn("did not reconstruct", result.differences[0]["reason"])

    def test_a_balance_we_invented_is_caught(self):
        result = postings.compare_to_provider({"64": Decimal("750.00")}, {})
        self.assertFalse(result.agrees)
        self.assertIn("does not report", result.differences[0]["reason"])

    def test_a_zero_balance_we_hold_and_the_provider_omits_is_not_a_difference(self):
        # QuickBooks omits zero rows from a trial balance; an account that
        # nets to nothing is agreement, not drift.
        self.assertTrue(postings.compare_to_provider({"84": Decimal("0.00")}, {}).agrees)


class EngineIntegrationTests(unittest.TestCase):
    def test_live_shaped_objects_now_produce_a_usable_ledger(self):
        # The exact gap that blocked check 04: no _Postings anywhere.
        objects = _objects(
            Purchase=[{"Id": "P1", "TotalAmt": 226.00, "AccountRef": {"value": "35"},
                       "Line": [_line(200.00, "64")],
                       "TxnTaxDetail": {"TotalTax": 26.00}}])
        self.assertTrue(work_engine.postings_derivable(objects))
        balances, derived = work_engine.ledger_balances(objects)
        self.assertTrue(derived.complete)
        self.assertEqual(balances["35"], Decimal("-226.00"))

    def test_check_04_runs_against_a_reconstructed_ledger(self):
        objects = _objects(
            Purchase=[{"Id": "P1", "TotalAmt": 226.00, "AccountRef": {"value": "35"},
                       "Line": [_line(200.00, "64")],
                       "TxnTaxDetail": {"TotalTax": 26.00}}])
        evidence = {"period_start": "2026-08-01", "period_end": "2026-08-31",
                    "bank_statements": [{"account_id": "35",
                                         "ending_ledger_balance": "-226.00"}]}
        analysis = work_engine.analyze(objects, evidence)
        entry = next(c for c in analysis.coverage["checks"] if c["id"] == "04")
        self.assertEqual(entry["status"], "clean")   # ran, and the account ties
        records = work_engine.reconcile(objects, evidence)
        self.assertEqual(records[0]["status"], "reconciled")

    def test_an_undecidable_ledger_still_blocks_check_04(self):
        objects = _objects(
            BillPayment=[{"Id": "BP1", "TotalAmt": 500.00}],
            Purchase=[{"Id": "P1", "TotalAmt": 10.00, "AccountRef": {"value": "35"},
                       "Line": [_line(10.00, "64")]}])
        evidence = {"period_start": "2026-08-01", "period_end": "2026-08-31",
                    "bank_statements": [{"account_id": "35",
                                         "ending_ledger_balance": "-10.00"}]}
        analysis = work_engine.analyze(objects, evidence)
        entry = next(c for c in analysis.coverage["checks"] if c["id"] == "04")
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(entry["missing_source"], "derived_ledger_postings")


if __name__ == "__main__":
    unittest.main()
