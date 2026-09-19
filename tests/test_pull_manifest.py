"""A pull that lost an entity must not look like a client with no rows.

This is the hole these tests exist to keep closed, and it is the worst kind the
architecture can have: silent, plausible, and invisible to every other check.

If a pull fails to load invoices, `derive_ledger` receives a dict with no
`Invoice` key -- which is exactly what a client with no invoices looks like. The
ledger reports itself **complete**, revenue and receivables are simply absent,
and the trial balance still sums to zero, because both halves of every invoice
went missing together. The double-entry invariant cannot see it. Beancount
cannot see it: it recomputes the same postings and reaches the same wrong
answer. Only knowing what was supposed to be there can see it.

That is why the pull records what it read, and why the gate refuses objects
that cannot say.
"""
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import postings, work_engine  # noqa: E402
from shimline.qbo_adapter import (MANIFEST_KEY, READ_OBJECTS,  # noqa: E402
                                  declare_pull, manifest)

CHART = [{"Id": "35", "Name": "Chequing", "AccountType": "Bank"},
         {"Id": "64", "Name": "Materials", "AccountType": "Cost of Goods Sold"},
         {"Id": "84", "Name": "A/R", "AccountType": "Accounts Receivable"},
         {"Id": "79", "Name": "Revenue", "AccountType": "Income"}]

INVOICE = {"Id": "1", "TxnDate": "2026-08-01", "TotalAmt": 1000.0,
           "Line": [{"Amount": 1000.0, "DetailType": "SalesItemLineDetail",
                     "SalesItemLineDetail": {"ItemAccountRef": {"value": "79"}}}]}
PURCHASE = {"Id": "2", "TxnDate": "2026-08-02", "TotalAmt": 200.0,
            "AccountRef": {"value": "35"},
            "Line": [{"Amount": 200.0,
                      "DetailType": "AccountBasedExpenseLineDetail",
                      "AccountBasedExpenseLineDetail": {
                          "AccountRef": {"value": "64"}}}]}


def full_pull():
    return declare_pull({"Account": list(CHART), "Invoice": [INVOICE],
                         "Purchase": [PURCHASE]}, source="fixture")


def pull_that_lost_invoices():
    """What a pull looks like when the Invoice query came back empty.

    The manifest is the honest record: every other type was read, Invoice was
    not. Without it this dict is indistinguishable from a client who has never
    raised an invoice.
    """
    objects = {"Account": list(CHART), "Purchase": [PURCHASE]}
    read = {kind: len(objects.get(kind) or []) for kind in READ_OBJECTS
            if kind != "Invoice"}
    objects[MANIFEST_KEY] = manifest(read, source="quickbooks")
    return objects


class TheShapeOfTheProblem(unittest.TestCase):

    def test_a_lost_entity_and_an_empty_one_are_the_same_dict(self):
        """Stated as a test because it is the whole reason the manifest exists.
        Everything below is defending against this one fact."""
        lost = {"Account": list(CHART), "Purchase": [PURCHASE]}
        genuinely_empty = {"Account": list(CHART), "Purchase": [PURCHASE]}
        self.assertEqual(lost, genuinely_empty)

    def test_without_a_manifest_a_lost_entity_still_reports_complete(self):
        """The unguarded behaviour, kept visible. `derive_ledger` on its own
        cannot know, which is why the gate is one layer up."""
        derived = postings.derive_ledger(
            {"Account": list(CHART), "Purchase": [PURCHASE]})
        self.assertTrue(derived.complete)
        self.assertNotIn("79", derived.balances)

    def test_the_missing_books_still_balance_to_zero(self):
        """Neither the double-entry invariant nor Beancount can catch this:
        both halves of every invoice went missing together."""
        derived = postings.derive_ledger(
            {"Account": list(CHART), "Purchase": [PURCHASE]})
        self.assertEqual(sum(derived.balances.values()), Decimal("0.00"))


class TheManifestCatchesIt(unittest.TestCase):

    def test_a_complete_pull_derives_normally(self):
        derived = postings.derive_ledger(full_pull())
        self.assertTrue(derived.complete, derived.reasons())
        self.assertEqual(derived.balances["79"], Decimal("-1000.00"))

    def test_a_pull_that_lost_invoices_blocks_the_ledger(self):
        derived = postings.derive_ledger(pull_that_lost_invoices())
        self.assertFalse(derived.complete)
        reasons = " ".join(derived.reasons())
        self.assertIn("Invoice", reasons)
        self.assertIn("did not read it", reasons)

    def test_every_posting_type_is_checked_not_only_invoices(self):
        for kind in postings.POSTING_TYPES:
            with self.subTest(kind=kind):
                objects = {"Account": list(CHART), "Purchase": [PURCHASE]}
                read = {other: 0 for other in READ_OBJECTS if other != kind}
                objects[MANIFEST_KEY] = manifest(read, source="quickbooks")
                derived = postings.derive_ledger(objects)
                self.assertFalse(derived.complete, kind)
                self.assertIn(kind, " ".join(derived.reasons()))

    def test_a_non_posting_type_going_missing_does_not_block(self):
        """A missing Attachable loses evidence, not money. Blocking a ledger on
        it would train a reviewer to ignore blocks."""
        objects = {"Account": list(CHART), "Purchase": [PURCHASE]}
        read = {kind: 0 for kind in READ_OBJECTS if kind != "Attachable"}
        objects[MANIFEST_KEY] = manifest(read, source="quickbooks")
        self.assertTrue(postings.derive_ledger(objects).complete)

    def test_a_manifest_in_a_shape_nobody_can_read_blocks(self):
        objects = dict(full_pull())
        objects[MANIFEST_KEY] = "sometime later someone made this a string"
        self.assertFalse(postings.derive_ledger(objects).complete)


class TheGate(unittest.TestCase):
    """`trusted_ledger` is the one place a ledger reaches a check."""

    def _evidence(self):
        return {"period_start": "2026-08-01", "period_end": "2026-08-31",
                "bank_statements": [{"account_id": "35",
                                     "period_end": "2026-08-31",
                                     "ending_ledger_balance": "-200.00"}]}

    def test_objects_with_no_manifest_are_refused(self):
        _, available, agreement = work_engine.trusted_ledger(
            {"Account": list(CHART), "Purchase": [PURCHASE]}, self._evidence())
        self.assertFalse(available)
        self.assertEqual(agreement["status"], "no_manifest")
        self.assertIn("failed to load", agreement["reason"])

    def test_a_declared_pull_is_accepted(self):
        _, available, agreement = work_engine.trusted_ledger(
            full_pull(), self._evidence())
        self.assertTrue(available)
        self.assertNotEqual(agreement["status"], "no_manifest")

    def test_a_check_that_needs_the_ledger_blocks_without_a_manifest(self):
        analysis = work_engine.analyze(
            {"Account": list(CHART), "Purchase": [PURCHASE]}, self._evidence(),
            today=date(2026, 9, 1))
        reconciliation = next(item for item in analysis.coverage["checks"]
                              if item["id"] == "04")
        self.assertEqual(reconciliation["status"], "blocked")

    def test_the_missing_evidence_names_the_pull_not_the_client(self):
        """A reviewer must not go asking a client for a document when the gap
        is that our own pull could not account for itself."""
        analysis = work_engine.analyze(
            {"Account": list(CHART), "Purchase": [PURCHASE]}, self._evidence(),
            today=date(2026, 9, 1))
        gaps = " ".join(analysis.coverage["missing_evidence"])
        self.assertIn("records which entity types it read", gaps)

    def test_reconciliation_reports_no_source_rather_than_a_wrong_balance(self):
        records = work_engine.reconcile(
            {"Account": list(CHART), "Purchase": [PURCHASE]}, self._evidence())
        self.assertEqual(records[0]["status"], "no_source")


class TheAdapterDeclaresItself(unittest.TestCase):

    def test_a_manifest_covers_every_readable_type(self):
        declared = declare_pull({"Account": list(CHART)}, source="fixture")
        self.assertEqual(set(declared[MANIFEST_KEY]["read"]), set(READ_OBJECTS))

    def test_the_manifest_counts_rows_so_a_person_can_sanity_check_it(self):
        declared = full_pull()[MANIFEST_KEY]
        self.assertEqual(declared["read"]["Invoice"], 1)
        self.assertEqual(declared["read"]["Purchase"], 1)
        self.assertEqual(declared["source"], "fixture")

    def test_the_synthetic_oracle_declares_its_pull_like_production(self):
        """Otherwise the acceptance suite would exercise a path production
        never takes, which is how a gate ends up untested."""
        from shimline.synthetic_books import generate_company
        company = generate_company(3)
        self.assertIn(MANIFEST_KEY, company.objects)
        _, available, _ = work_engine.trusted_ledger(
            company.objects, company.evidence)
        self.assertTrue(available)

    def test_the_manifest_key_is_not_mistaken_for_an_entity(self):
        """It starts with an underscore so `derive_ledger`'s unrecognised-type
        check skips it rather than blocking every real pull."""
        self.assertTrue(MANIFEST_KEY.startswith("_"))
        self.assertTrue(postings.derive_ledger(full_pull()).complete)


class ThePullNamesWhatFailed(unittest.TestCase):
    """`query` posts every read to the same URL, so the error has to say which."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "pull.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_a_failed_entity_is_named_and_nothing_is_computed(self):
        from shimline.qbo_adapter import QBOAdapter, QBOError

        adapter = QBOAdapter.__new__(QBOAdapter)
        adapter.api_calls = {}

        def query(kind, **_):
            if kind == "TaxRate":
                raise QBOError("QuickBooks GET query failed (HTTP 400)")
            return iter(())

        adapter.query = query
        with self.assertRaises(QBOError) as caught:
            QBOAdapter.pull_all(adapter)
        self.assertIn("TaxRate", str(caught.exception))
        self.assertIn("nothing was computed", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
