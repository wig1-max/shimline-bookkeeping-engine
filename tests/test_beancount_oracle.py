"""Differential tests: Beancount must agree with Shimline's own arithmetic.

work_engine imports trial_balance from synthetic_books -- the module that also
generates the golden ledger the acceptance suite grades against. A wrong
double-entry assumption would be wrong in both places and the suite would still
pass. These tests add an independently written engine that has to agree.

Several tests here deliberately break the ledger and assert that Beancount
*catches* it. An oracle nobody has watched fail is not evidence of anything, so
the negative cases matter more than the positive one.

Beancount is GPL-2.0 and is a development dependency only: never imported,
never shipped, run as a subprocess. See shimline/beancount_export.py.
"""
import copy
import re
import unittest
from decimal import Decimal

from shimline import beancount_export, postings
from shimline.synthetic_books import (generate_companies, generate_company,
                                      trial_balance)

_ORACLE_TAG = "_shimline_requires_bean_check"


def requires_bean_check(item):
    """Skip unless bean-check is installed, and record what was guarded.

    The tag is the point. `skipUnless` with a true condition returns the item
    unchanged, so where the oracle *can* run there is nothing left to count --
    and an oracle that shrinks to zero tests reports exactly the same green as
    one doing its job. `OracleCoverageTests` counts the tag instead, on every
    platform, because an enumerator that finds nothing proves nothing.
    """
    setattr(item, _ORACLE_TAG, True)
    return unittest.skipUnless(
        beancount_export.bean_check_available(),
        "bean-check is not installed (pip install -r requirements-dev.txt)")(item)


def _oracle_test_count() -> int:
    """How many tests the differential oracle actually contributes."""
    total = 0
    for obj in list(globals().values()):
        if not (isinstance(obj, type) and issubclass(obj, unittest.TestCase)):
            continue
        names = [name for name in dir(obj) if name.startswith("test")]
        if obj.__dict__.get(_ORACLE_TAG):
            total += len(names)
            continue
        total += sum(1 for name in names
                     if getattr(getattr(obj, name), _ORACLE_TAG, False))
    return total


class ExportShapeTests(unittest.TestCase):
    """These need no Beancount installed -- they check what we emit."""

    def test_account_names_map_to_the_five_beancount_roots(self):
        cases = [
            ({"Id": "100", "Name": "Operating Bank", "AccountType": "Bank"}, "Assets:"),
            ({"Id": "200", "Name": "A/P", "AccountType": "Accounts Payable"}, "Liabilities:"),
            ({"Id": "400", "Name": "Revenue", "AccountType": "Income"}, "Income:"),
            ({"Id": "500", "Name": "Materials", "AccountType": "Cost of Goods Sold"}, "Expenses:"),
            ({"Id": "300", "Name": "Owner", "AccountType": "Equity"}, "Equity:"),
        ]
        for account, root in cases:
            self.assertTrue(beancount_export.account_name(account).startswith(root))

    def test_qbo_names_with_punctuation_become_legal_components(self):
        name = beancount_export.account_name(
            {"Id": "120", "Name": "GST/HST Recoverable & Other",
             "AccountType": "Other Current Asset"})
        self.assertNotIn("/", name)
        self.assertNotIn("&", name)
        self.assertNotIn(" ", name)
        self.assertIn("120-GST-HST-Recoverable-Other", name)

    def test_an_unmapped_account_type_fails_rather_than_defaulting(self):
        # Defaulting to Assets would silently invert the sign of an Income
        # balance, which is exactly the class of error this oracle exists to find.
        with self.assertRaises(beancount_export.ExportError):
            beancount_export.account_name(
                {"Id": "999", "Name": "Mystery", "AccountType": "Nonexistent Type"})

    def test_live_adapter_objects_are_refused_rather_than_exported_empty(self):
        live = {"Account": [{"Id": "101", "Name": "Bank", "AccountType": "Bank"}],
                "Purchase": [{"Id": "P1", "TotalAmt": "10.00"}]}
        with self.assertRaises(beancount_export.ExportError) as caught:
            beancount_export.export(live)
        self.assertIn("no double entry", str(caught.exception).lower())

    def test_a_posting_to_an_unknown_account_is_refused(self):
        company = generate_company(1)
        objects = copy.deepcopy(company.objects)
        objects["Invoice"][0]["_Postings"][0]["account"] = "8888"
        with self.assertRaises(beancount_export.ExportError) as caught:
            beancount_export.export(objects)
        self.assertIn("8888", str(caught.exception))

    def test_balance_assertions_are_written_with_an_exact_tolerance(self):
        company = generate_company(1)
        text = beancount_export.export(
            company.objects, balances=trial_balance(company.objects))
        directives = [line for line in text.splitlines()
                      if re.match(r"^\d{4}-\d{2}-\d{2} balance ", line)]
        self.assertTrue(directives)
        for line in directives:
            self.assertIn("~ 0 CAD", line)


@requires_bean_check
class DifferentialTests(unittest.TestCase):
    def test_a_synthetic_company_satisfies_both_engines(self):
        company = generate_company(1)
        balances = trial_balance(company.objects)
        result = beancount_export.verify(company.objects, balances=balances)
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.accounts_asserted, len(balances))
        self.assertGreater(result.transactions, 0)

    def test_ten_generated_companies_agree_account_for_account(self):
        for company in generate_companies(10):
            balances = trial_balance(company.objects)
            result = beancount_export.verify(company.objects, balances=balances)
            self.assertTrue(result.ok, f"{company.company_id}: {result.output}")

    def test_beancount_catches_an_unbalanced_transaction(self):
        """The bug class trial_balance structurally cannot see.

        trial_balance accumulates debits minus credits per account. It never
        checks that one transaction's own postings sum to zero, so a transaction
        that does not balance still yields a plausible trial balance and nothing
        notices. Beancount rejects the file.
        """
        company = generate_company(1)
        broken = copy.deepcopy(company.objects)
        broken["Invoice"][0]["_Postings"][1]["credit"] = "900"   # was 1000

        # Shimline's own arithmetic still returns an answer, without complaint.
        self.assertTrue(trial_balance(broken))

        result = beancount_export.verify(broken, balances=trial_balance(broken))
        self.assertFalse(result.ok)
        self.assertIn("does not balance", result.output)

    def test_beancount_catches_a_one_cent_disagreement(self):
        # Guards the `~ 0` tolerance in the exporter. Without it Beancount infers
        # a tolerance from the file's precision and this passes silently.
        company = generate_company(3)
        claimed = trial_balance(company.objects)
        account = sorted(claimed)[0]
        claimed[account] += Decimal("0.01")
        result = beancount_export.verify(company.objects, balances=claimed)
        self.assertFalse(result.ok, "a one-cent error must not be tolerated")
        self.assertIn("Balance failed", result.output)

    def test_beancount_catches_a_sign_inversion(self):
        company = generate_company(4)
        claimed = trial_balance(company.objects)
        revenue = next(k for k, v in claimed.items() if v < 0)
        claimed[revenue] = -claimed[revenue]
        result = beancount_export.verify(company.objects, balances=claimed)
        self.assertFalse(result.ok)
        self.assertIn("Balance failed", result.output)

    def test_a_failed_check_keeps_the_ledger_for_inspection(self):
        company = generate_company(5)
        claimed = trial_balance(company.objects)
        claimed[sorted(claimed)[0]] += Decimal("100")
        result = beancount_export.verify(company.objects, balances=claimed)
        self.assertFalse(result.ok)
        self.assertTrue(result.ledger_path, "a failure must leave the file behind")


LIVE_CHART = [
    {"Id": "35", "Name": "Chequing", "AccountType": "Bank"},
    {"Id": "84", "Name": "A/R", "AccountType": "Accounts Receivable"},
    {"Id": "89", "Name": "GST/HST Payable", "AccountType": "Other Current Liability"},
    {"Id": "79", "Name": "Revenue", "AccountType": "Income"},
    {"Id": "64", "Name": "Materials", "AccountType": "Cost of Goods Sold"},
]


def _live_objects():
    """Objects exactly as QuickBooks sends them: no _Postings anywhere."""
    return {
        "Account": list(LIVE_CHART),
        "Invoice": [{"Id": "1001", "TxnDate": "2026-08-04", "TotalAmt": 1130.00,
                     "Line": [{"Amount": 1000.00, "DetailType": "SalesItemLineDetail",
                               "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}],
                     "TxnTaxDetail": {"TotalTax": 130.00}}],
        "Purchase": [{"Id": "2001", "TxnDate": "2026-08-06", "TotalAmt": 200.00,
                      "AccountRef": {"value": "35"},
                      "Line": [{"Amount": 200.00, "DetailType": "AccountBasedExpenseLineDetail",
                                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "64"}}}]}],
    }


class LiveDataOracleTests(unittest.TestCase):
    """The differential check reaches real client books, not only synthetic ones.

    This module needs `_Postings`, which only synthetic_books attaches, so until
    the reconstruction existed the oracle could only ever grade generated
    companies. It can now be pointed at a live pull.
    """

    def test_live_shaped_objects_gain_postings(self):
        enriched = beancount_export.with_derived_postings(_live_objects())
        self.assertTrue(enriched["Invoice"][0]["_Postings"])
        self.assertTrue(enriched["Purchase"][0]["_Postings"])

    def test_an_incomplete_reconstruction_is_refused_rather_than_exported(self):
        # A partial ledger would fail Beancount's balance assertions for a
        # reason that has nothing to do with the books being wrong.
        objects = _live_objects()
        objects["BillPayment"] = [{"Id": "BP1", "TotalAmt": 500.00}]
        with self.assertRaises(beancount_export.ExportError) as caught:
            beancount_export.with_derived_postings(objects)
        self.assertIn("could not be fully reconstructed", str(caught.exception))

    def test_already_present_postings_are_left_alone(self):
        company = generate_company(1)
        enriched = beancount_export.with_derived_postings(company.objects)
        self.assertEqual(enriched["Purchase"][0]["_Postings"],
                         company.objects["Purchase"][0]["_Postings"])

    @requires_bean_check
    def test_beancount_agrees_with_a_reconstructed_live_ledger(self):
        objects = _live_objects()
        enriched = beancount_export.with_derived_postings(objects)
        balances = postings.derive(objects).balances
        result = beancount_export.verify(enriched, balances=balances)
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.transactions, 2)

    @requires_bean_check
    def test_beancount_agrees_on_settlements_and_reversals(self):
        """The six types that used to block every ledger, graded by a second
        double-entry engine rather than by the module that derived them.

        A bill paid, money moved to savings, a till sale and a supplier credit
        is an ordinary contractor month. Until these were derived it was refused
        outright, so nothing here had ever been independently checked.
        """
        objects = dict(_live_objects())
        objects["Account"] = list(LIVE_CHART) + [
            {"Id": "36", "Name": "Savings", "AccountType": "Bank"},
            {"Id": "33", "Name": "A/P", "AccountType": "Accounts Payable"}]
        objects["Bill"] = [{"Id": "4001", "TxnDate": "2026-08-06", "TotalAmt": 500.00,
                            "Line": [{"Amount": 500.00,
                                      "DetailType": "AccountBasedExpenseLineDetail",
                                      "AccountBasedExpenseLineDetail": {
                                          "AccountRef": {"value": "64"}}}]}]
        objects["BillPayment"] = [{"Id": "BP1", "TxnDate": "2026-08-25",
                                   "TotalAmt": 400.00,
                                   "CheckPayment": {"BankAccountRef": {"value": "35"}}}]
        objects["VendorCredit"] = [{"Id": "VC1", "TxnDate": "2026-08-28",
                                    "TotalAmt": 100.00,
                                    "Line": [{"Amount": 100.00,
                                              "DetailType": "AccountBasedExpenseLineDetail",
                                              "AccountBasedExpenseLineDetail": {
                                                  "AccountRef": {"value": "64"}}}]}]
        objects["Transfer"] = [{"Id": "T1", "TxnDate": "2026-08-29", "Amount": 300.00,
                                "FromAccountRef": {"value": "35"},
                                "ToAccountRef": {"value": "36"}}]
        objects["SalesReceipt"] = [{"Id": "SR1", "TxnDate": "2026-08-30",
                                    "TotalAmt": 226.00,
                                    "DepositToAccountRef": {"value": "35"},
                                    "Line": [{"Amount": 200.00,
                                              "DetailType": "SalesItemLineDetail",
                                              "SalesItemLineDetail": {
                                                  "ItemAccountRef": {"value": "79"}}}],
                                    "TxnTaxDetail": {"TotalTax": 26.00}}]
        objects["CreditMemo"] = [{"Id": "CM1", "TxnDate": "2026-08-31",
                                  "TotalAmt": 113.00,
                                  "Line": [{"Amount": 100.00,
                                            "DetailType": "SalesItemLineDetail",
                                            "SalesItemLineDetail": {
                                                "ItemAccountRef": {"value": "79"}}}],
                                  "TxnTaxDetail": {"TotalTax": 13.00}}]

        enriched = beancount_export.with_derived_postings(objects)
        balances = postings.derive(objects).balances
        result = beancount_export.verify(enriched, balances=balances)
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.transactions, 8)

    @requires_bean_check
    def test_beancount_catches_a_settlement_that_drifts(self):
        """The new derivations are guarded the same way as the old ones: drift a
        cent and an independently written engine has to notice."""
        objects = dict(_live_objects())
        objects["Account"] = list(LIVE_CHART) + [
            {"Id": "33", "Name": "A/P", "AccountType": "Accounts Payable"}]
        objects["BillPayment"] = [{"Id": "BP1", "TxnDate": "2026-08-25",
                                   "TotalAmt": 400.00,
                                   "CheckPayment": {"BankAccountRef": {"value": "35"}}}]
        enriched = beancount_export.with_derived_postings(objects)
        claimed = postings.derive(objects).balances
        claimed["33"] += Decimal("0.01")
        result = beancount_export.verify(enriched, balances=claimed)
        self.assertFalse(result.ok)
        self.assertIn("Balance failed", result.output)

    @requires_bean_check
    def test_beancount_catches_a_reconstruction_that_drifts(self):
        # Guards the reconstruction itself: if derive() ever mis-posts, an
        # independently written engine has to notice.
        objects = _live_objects()
        enriched = beancount_export.with_derived_postings(objects)
        claimed = postings.derive(objects).balances
        claimed["79"] += Decimal("0.01")
        result = beancount_export.verify(enriched, balances=claimed)
        self.assertFalse(result.ok)
        self.assertIn("Balance failed", result.output)


class RegistryTests(unittest.TestCase):
    """The export and the reconstruction must agree on what posts.

    This module used to hold its own hand-written copy of the posting types.
    When six more were added to `postings`, the copy stayed behind: the new
    documents counted towards Shimline's balances and were silently left out of
    the Beancount file, so the oracle failed for a reason that had nothing to do
    with the books. The registry is now shared; this makes a future divergence
    a test failure rather than a puzzling balance mismatch.
    """

    def test_the_export_posts_every_type_the_reconstruction_derives(self):
        self.assertEqual(set(beancount_export.POSTED_KINDS),
                         set(postings.POSTING_TYPES))

    def test_no_derived_transaction_is_dropped_from_the_export(self):
        objects = dict(_live_objects())
        objects["Account"] = list(LIVE_CHART) + [
            {"Id": "36", "Name": "Savings", "AccountType": "Bank"},
            {"Id": "33", "Name": "A/P", "AccountType": "Accounts Payable"}]
        objects["BillPayment"] = [{"Id": "BP1", "TxnDate": "2026-08-25",
                                   "TotalAmt": 400.00,
                                   "CheckPayment": {"BankAccountRef": {"value": "35"}}}]
        objects["Transfer"] = [{"Id": "T1", "TxnDate": "2026-08-29", "Amount": 300.00,
                                "FromAccountRef": {"value": "35"},
                                "ToAccountRef": {"value": "36"}}]
        derived = postings.derive(objects)
        enriched = beancount_export.with_derived_postings(objects)
        exported = beancount_export.export(enriched,
                                           balances=derived.balances)
        for key in derived.postings:
            object_id = key.split(":", 1)[1]
            self.assertIn(f'shimline-id: "{object_id}"', exported, key)


class AvailabilityTests(unittest.TestCase):
    def test_absent_bean_check_skips_rather_than_fails_the_build(self):
        # Production hosts do not install Beancount. The check reporting itself
        # as skipped is correct; reporting a pass it never ran is not.
        company = generate_company(2)
        result = beancount_export.verify(
            company.objects, balances=trial_balance(company.objects))
        if beancount_export.bean_check_available():
            self.assertTrue(result.available)
            self.assertEqual(result.skipped, "")
        else:
            self.assertFalse(result.available)
            self.assertIn("not installed", result.skipped)


class GeneratedCorpusOracleTests(unittest.TestCase):
    """Every type the corpus now contains, drifted a cent, caught by Beancount.

    The corpus held four of twelve posting types until these eight were added,
    so the acceptance run and the golden ledger had never summed a settlement or
    a reversal. Adding documents only proves the oracle *ran* over them. This
    proves it would have objected -- which is the only version of the claim
    worth making, since an oracle nobody has watched fail is decoration.
    """

    # kind -> the account whose leg gets drifted, and what the document is.
    BREAKS = {
        "Deposit": ("400", "cash takings banked directly"),
        "JournalEntry": ("200", "a month-end accrual"),
        "BillPayment": ("100", "a bill settled by cheque"),
        "CreditMemo": ("110", "a credit memo against a receivable"),
        "VendorCredit": ("500", "a supplier credit for returned goods"),
        "Transfer": ("105", "money moved between two bank accounts"),
        "SalesReceipt": ("210", "the tax leg of a till sale"),
        "RefundReceipt": ("210", "tax reversed on a refund"),
    }

    def test_the_breaks_cover_every_type_the_corpus_gained(self):
        """So this class cannot fall behind the generator in silence."""
        company = generate_company(3)
        gained = {kind for kind in postings.POSTING_TYPES
                  if kind not in {"Invoice", "Payment", "Bill", "Purchase"}}
        self.assertEqual(set(self.BREAKS), gained)
        for kind in gained:
            self.assertTrue(company.objects.get(kind), f"{kind} is not generated")

    @requires_bean_check
    def test_a_cent_of_drift_in_any_generated_type_is_caught(self):
        for kind, (account, description) in self.BREAKS.items():
            with self.subTest(kind=kind, document=description):
                objects = copy.deepcopy(generate_company(3).objects)
                # Selected by the account it moves, not by position. Indexing
                # `[0]` broke the moment the corpus gained a second journal
                # entry: the opening balance took first place and the accrual
                # this case is about moved down.
                drifted = False
                for document in objects[kind]:
                    for posting in document["_Postings"]:
                        if posting["account"] != account:
                            continue
                        for side in ("debit", "credit"):
                            if Decimal(posting[side]) != 0:
                                posting[side] = str(
                                    Decimal(posting[side]) + Decimal("0.01"))
                                drifted = True
                                break
                        break
                    if drifted:
                        break
                self.assertTrue(drifted, f"no {account} leg to drift on a {kind}")
                result = beancount_export.verify(
                    objects, balances=trial_balance(objects))
                self.assertFalse(
                    result.ok,
                    f"Beancount accepted a {kind} whose {description} is a cent "
                    "out, so this type is not actually guarded")


class OracleCoverageTests(unittest.TestCase):
    """The second engine is the only check that has ever caught a wrong ledger
    every other check passed. It has to be impossible to lose quietly.

    Runs on every platform, including ones where the oracle itself skips: what
    it guards is the *number of tests*, which is a property of this file rather
    than of whether beancount installed here.
    """

    EXPECTED_MINIMUM = 11

    def test_the_oracle_still_has_tests_to_run(self):
        count = _oracle_test_count()
        self.assertGreaterEqual(
            count, self.EXPECTED_MINIMUM,
            f"the differential oracle is down to {count} tests from "
            f"{self.EXPECTED_MINIMUM}. Either restore them, or lower "
            "EXPECTED_MINIMUM deliberately and say in the commit message why "
            "the second engine needs to check less than it used to.")

    def test_every_posted_type_is_graded_by_the_second_engine(self):
        """A posting type Beancount never sees is arithmetic with one
        implementation, which is what this whole module exists to prevent."""
        self.assertEqual(beancount_export.POSTED_KINDS, postings.POSTING_TYPES)


if __name__ == "__main__":
    unittest.main()
