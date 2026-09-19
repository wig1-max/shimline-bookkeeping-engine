"""Invariants the derivation must hold for documents nobody wrote by hand.

The blocking model says the whole of the risk is unknown shapes: the thirty-three
refusal paths the engine knows about are exercised, and `p` is made of the cases
nobody imagined. Every fixture in this repository is a shape somebody already
thought of, so fixtures cannot reach that risk by construction.

These generate damaged documents from the documented grammar and assert
*properties* rather than outputs. For a randomly damaged document there is no
expected answer -- only things that must be true whatever the answer is:

  1. A complete ledger balances, and so does every document in it.
  2. Every document reaches an outcome. None vanishes.
  3. A refusal names the document it came from.
  4. The same input gives the same answer.
  5. A document that refuses alone still refuses in company.
  6. Documents that derive individually derive the same way together.

Seeds are fixed, so a failure here is reproducible rather than a rumour, and a
failure shrinks itself to a minimal document before it is reported.

This found one defect on the first run. A `BillPayment` carrying nothing but an
`Id` -- no total, no lines, no accounts -- was silently treated as a document
somebody had abandoned, and the ledger reported itself **complete**. `money(None)`
is 0, so a document that *states* zero and a document that *states nothing* both
arrived at the same branch. The same distinction as the pull manifest, one level
down.
"""
import copy
import json
import unittest
from collections import Counter
from decimal import Decimal

import qbo_grammar
from shimline.postings import POSTING_TYPES, derive_ledger
from shimline.qbo_adapter import declare_pull

# Enough to be worth running, small enough that nobody is tempted to skip it.
COMPANIES = 800
SPLIT_COMPANIES = 250


def ledger_of(objects: dict):
    """Derive a copy. `declare_pull` stamps the dict it is handed."""
    return derive_ledger(declare_pull(copy.deepcopy(objects), source="fixture"))


def documents(objects: dict):
    for kind in POSTING_TYPES:
        for txn in objects.get(kind) or []:
            yield kind, txn


def describe(objects: dict) -> str:
    shown = {kind: rows for kind, rows in objects.items()
             if kind != "Account" and not kind.startswith("_")}
    return json.dumps(shown, indent=2, default=str, sort_keys=True)


class PropertyCase(unittest.TestCase):
    """Shrinks a counterexample before reporting it."""

    def fail_with(self, objects, predicate, message):
        minimal = qbo_grammar.shrink(objects, predicate)
        self.fail(f"{message}\n\nMinimal counterexample:\n{describe(minimal)}")


class EveryDocumentReachesAnOutcome(PropertyCase):
    """Posted, refused, or recorded as moving nothing. Never nothing at all.

    The failure this guards is the one that produces books that look right: a
    document goes through the reconstruction, leaves no trace, and the trial
    balance still ties because nothing of it was posted on either side.
    """

    @staticmethod
    def vanishes(objects) -> bool:
        if not any(objects.get(kind) for kind in POSTING_TYPES):
            return False
        return not ledger_of(objects).every_document_accounted_for

    def test_no_document_vanishes(self):
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            if self.vanishes(objects):
                self.fail_with(objects, self.vanishes,
                               f"seed {seed}: a document was neither posted, "
                               "refused, nor recorded as moving nothing")

    def test_a_document_stating_zero_is_recorded_rather_than_dropped(self):
        objects = {"Account": list(qbo_grammar.CHART),
                   "Purchase": [{"Id": "P1", "TxnDate": "2026-07-01",
                                 "TotalAmt": 0, "AccountRef": {"value": "35"},
                                 "Line": []}]}
        ledger = ledger_of(objects)
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertTrue(ledger.every_document_accounted_for)
        self.assertEqual(len(ledger.posted_nothing), 1)
        self.assertIn("moves no account", ledger.posted_nothing[0].reason)

    def test_a_document_stating_nothing_is_refused_not_treated_as_abandoned(self):
        """The defect these properties found. A document carrying only an Id is
        not a document somebody abandoned; it is a document we cannot read."""
        objects = {"Account": list(qbo_grammar.CHART),
                   "BillPayment": [{"Id": "BP1"}]}
        ledger = ledger_of(objects)
        self.assertFalse(ledger.complete)
        self.assertTrue(ledger.every_document_accounted_for)
        self.assertIn("arrived incomplete", " ".join(ledger.reasons()))


class ABalancedLedgerOrNone(PropertyCase):

    @staticmethod
    def unbalanced(objects) -> bool:
        ledger = ledger_of(objects)
        if ledger.complete and sum(ledger.balances.values()) != Decimal("0"):
            return True
        return any(
            sum(Decimal(str(post.get("debit") or 0))
                - Decimal(str(post.get("credit") or 0)) for post in posts) != 0
            for posts in ledger.postings.values())

    def test_a_complete_ledger_balances_and_so_does_every_document_in_it(self):
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            if self.unbalanced(objects):
                self.fail_with(objects, self.unbalanced,
                               f"seed {seed}: postings do not balance")


class ARefusalNamesItsDocument(PropertyCase):
    """An operator reading "Purchase ?: ..." cannot find the document."""

    @staticmethod
    def anonymous(objects) -> bool:
        if any(not txn.get("Id") for _kind, txn in documents(objects)):
            return False  # nothing to name; not this property's business
        return any(item.object_id in ("", "?")
                   for item in ledger_of(objects).unsupported)

    def test_every_refusal_names_the_document_that_caused_it(self):
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            if self.anonymous(objects):
                self.fail_with(objects, self.anonymous,
                               f"seed {seed}: a refusal names no document")


class TheSameInputGivesTheSameAnswer(PropertyCase):

    def test_deriving_twice_agrees(self):
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            first, second = ledger_of(objects), ledger_of(objects)
            self.assertEqual(first.reasons(), second.reasons(), f"seed {seed}")
            self.assertEqual(first.balances, second.balances, f"seed {seed}")

    def test_deriving_does_not_modify_what_it_was_given(self):
        """A reader that edits its input makes the second run a different run."""
        for seed in range(100):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            before = json.dumps(objects, sort_keys=True, default=str)
            derive_ledger(declare_pull(objects, source="fixture"))
            after = {k: v for k, v in objects.items() if not k.startswith("_")}
            self.assertEqual(
                json.dumps(after, sort_keys=True, default=str), before,
                f"seed {seed}: the derivation modified the objects it was given")


class DocumentsDoNotAffectEachOther(PropertyCase):
    """Each document derives from itself and the chart, nothing else.

    This is what makes quarantine sound. If one unreadable document could change
    what a readable one posts, then deriving the rest of a file after setting it
    aside would produce a different ledger from deriving the file whole -- and
    the all-or-nothing refusal would be the only safe design. It is also worth
    asserting on its own: a reconstruction where documents influence each other
    is one where a finding depends on what else happened to be in the pull.
    """

    def test_a_document_that_refuses_alone_refuses_in_company(self):
        for seed in range(SPLIT_COMPANIES):
            objects = qbo_grammar.company(seed, documents=4, damage=2)
            whole = ledger_of(objects)
            if not whole.complete:
                continue
            for kind, txn in documents(objects):
                alone = ledger_of({"Account": copy.deepcopy(objects["Account"]),
                                   kind: [copy.deepcopy(txn)]})
                self.assertTrue(
                    alone.complete,
                    f"seed {seed}: {kind} {txn.get('Id')} refuses on its own but "
                    f"the whole file derived: {alone.reasons()}")

    def test_documents_that_derive_apart_derive_the_same_together(self):
        for seed in range(SPLIT_COMPANIES):
            objects = qbo_grammar.company(seed, documents=4, damage=2)
            expected: Counter = Counter()
            for kind, txn in documents(objects):
                alone = ledger_of({"Account": copy.deepcopy(objects["Account"]),
                                   kind: [copy.deepcopy(txn)]})
                if not alone.complete:
                    break
                for account, amount in alone.balances.items():
                    expected[account] += amount
            else:
                whole = ledger_of(objects)
                self.assertTrue(whole.complete,
                                f"seed {seed}: every document derived alone but "
                                f"together they did not: {whole.reasons()}")
                self.assertEqual(
                    {a: v for a, v in whole.balances.items() if v != 0},
                    {a: v for a, v in expected.items() if v != 0},
                    f"seed {seed}: the whole is not the sum of its documents")


class TheGeneratorIsWorthRunning(unittest.TestCase):
    """A generator that only produces documents the engine already handles
    proves nothing, and so does one that produces nothing it can handle."""

    def test_it_produces_both_derivable_and_refused_documents(self):
        derived = refused = 0
        for seed in range(200):
            ledger = ledger_of(qbo_grammar.company(seed, documents=5, damage=2))
            derived += len(ledger.postings)
            refused += len(ledger.unsupported)
        self.assertGreater(derived, 50, "nothing generated could be derived")
        self.assertGreater(refused, 50, "nothing generated was refused")

    def test_it_reaches_every_posting_type(self):
        seen = set()
        for seed in range(200):
            seen.update(kind for kind, _txn
                        in documents(qbo_grammar.company(seed, documents=5)))
        self.assertEqual(seen, set(POSTING_TYPES))

    def test_the_shrinker_actually_shrinks(self):
        objects = qbo_grammar.company(5, documents=5, damage=2)

        def has_documents(candidate):
            return any(candidate.get(kind) for kind in POSTING_TYPES)

        minimal = qbo_grammar.shrink(objects, has_documents)
        self.assertEqual(sum(1 for _ in documents(minimal)), 1)
        self.assertLess(len(describe(minimal)), len(describe(objects)))


if __name__ == "__main__":
    unittest.main()
