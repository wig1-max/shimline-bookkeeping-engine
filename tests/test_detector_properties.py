"""Invariants the detector pipeline must hold for companies nobody wrote.

`test_derivation_properties.py` does this for the reconstruction and found a
document that vanished. This does it for `analyze`, which is where the newest
and least-exercised code lives: checks 05, 15 and 16 were automated in a single
change, and every fixture for them is a shape somebody had already imagined.

It found two crashes on the first run. `date.fromisoformat(invoice["TxnDate"])`
raised `KeyError` on an invoice with no date, and the duplicate detector raised
`KeyError` on a purchase with no `Id`. Both took a client's entire scan down
with them, which is worse than any wrong finding: falling over is not refusing.

The properties, none of which depend on a particular company:

  1. `analyze` never raises, whatever the documents look like.
  2. A finding never names a document or account absent from the pull.
  3. Every finding's `defect_type` is one the registry advertises.
  4. Two findings never share a key.
  5. The same input gives the same findings in the same order.
  6. A company with no documents produces no findings about documents.
  7. Every advertised check reports one of the four legal statuses.
  8. A check never reports `clean` when no ledger could be derived.
"""
import copy
import json
import unittest

import qbo_grammar
from shimline import work_engine
from shimline.postings import POSTING_TYPES
from shimline.qbo_adapter import declare_pull

COMPANIES = 600
LEGAL_STATUSES = {"clean", "defect", "blocked", "manual"}
LEGAL_SEVERITIES = {"high", "medium", "low"}


def analyzed(objects: dict, evidence: dict | None = None):
    return work_engine.analyze(
        declare_pull(copy.deepcopy(objects), source="fixture"), evidence or {})


def describe(objects: dict) -> str:
    shown = {kind: rows for kind, rows in objects.items()
             if kind != "Account" and not kind.startswith("_")}
    return json.dumps(shown, indent=2, default=str, sort_keys=True)


class AnalyzeNeverFallsOver(unittest.TestCase):
    """A crash is not a refusal. It takes the whole engagement, not one document."""

    @staticmethod
    def raises(objects) -> bool:
        try:
            analyzed(objects)
        except Exception:  # noqa: BLE001 - that is the property
            return True
        return False

    def test_no_generated_company_makes_the_detectors_raise(self):
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            if self.raises(objects):
                minimal = qbo_grammar.shrink(objects, self.raises)
                with self.assertRaises(Exception) as raised:
                    analyzed(minimal)
                self.fail(f"seed {seed}: analyze raised "
                          f"{type(raised.exception).__name__}: {raised.exception}"
                          f"\n\nMinimal counterexample:\n{describe(minimal)}")

    def test_an_invoice_with_no_date_becomes_a_receivable_needing_evidence(self):
        """The first crash these properties found. An invoice that cannot be
        aged must not silently leave the check either -- it is still money owed,
        and reporting the books clean because one date was unreadable is the
        failure this architecture exists to avoid."""
        objects = {"Account": list(qbo_grammar.CHART),
                   "Invoice": [{"Id": "I1", "TotalAmt": 500, "Balance": 500,
                                "ARAccountRef": {"value": "84"},
                                "Line": [{"Amount": 500,
                                          "DetailType": "SalesItemLineDetail",
                                          "SalesItemLineDetail": {
                                              "ItemAccountRef": {"value": "79"}}}]}]}
        finding = next(item for item in analyzed(objects).findings
                       if item.defect_type == "stale_receivable")
        self.assertEqual(finding.affected_id, "I1")
        self.assertEqual(finding.evidence_status, "missing")
        self.assertIn("states no readable transaction date", finding.reason)
        self.assertIsNotNone(finding.evidence_request)

    def test_an_invoice_with_an_unparseable_date_is_treated_the_same(self):
        objects = {"Account": list(qbo_grammar.CHART),
                   "Invoice": [{"Id": "I2", "TxnDate": "last Tuesday",
                                "TotalAmt": 500, "Balance": 500,
                                "ARAccountRef": {"value": "84"},
                                "Line": [{"Amount": 500,
                                          "DetailType": "SalesItemLineDetail",
                                          "SalesItemLineDetail": {
                                              "ItemAccountRef": {"value": "79"}}}]}]}
        finding = next(item for item in analyzed(objects).findings
                       if item.defect_type == "stale_receivable")
        self.assertEqual(finding.evidence_status, "missing")

    def test_a_paid_invoice_with_no_date_is_not_a_finding(self):
        """Nothing is owed, so there is nothing that needs aging. A detector
        that fires on every undated document is worse than no detector."""
        objects = {"Account": list(qbo_grammar.CHART),
                   "Invoice": [{"Id": "I3", "TotalAmt": 500, "Balance": 0,
                                "ARAccountRef": {"value": "84"},
                                "Line": [{"Amount": 500,
                                          "DetailType": "SalesItemLineDetail",
                                          "SalesItemLineDetail": {
                                              "ItemAccountRef": {"value": "79"}}}]}]}
        self.assertEqual([item for item in analyzed(objects).findings
                          if item.defect_type == "stale_receivable"], [])

    def test_a_purchase_with_no_id_cannot_be_a_duplicate_finding(self):
        """The second crash. A duplicate finding proposes a reversing entry
        against a specific object, so an unidentifiable document cannot be its
        subject -- and the derivation reports it as unidentifiable anyway."""
        line = {"Amount": 50, "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "64"}}}
        twin = {"TxnDate": "2026-07-01", "DocNumber": "R-1", "TotalAmt": 50,
                "AccountRef": {"value": "35"}, "Line": [line]}
        objects = {"Account": list(qbo_grammar.CHART),
                   "Purchase": [dict(twin), dict(twin)]}
        analysis = analyzed(objects)
        self.assertEqual([item for item in analysis.findings
                          if item.defect_type == "duplicate_transaction"], [])


class AFindingNamesSomethingReal(unittest.TestCase):
    """A finding pointing at a document that is not in the pull cannot be acted
    on, and an operator who meets one stops trusting the list."""

    def test_a_job_absent_from_the_pull_is_not_reported_as_confirmed(self):
        """`SELECT * FROM Customer` returns only active records, so a job
        archived in QuickBooks keeps its costs and vanishes from the customer
        list. The loss is still real; claiming sufficient evidence for it points
        an operator at something they cannot open."""
        line = lambda amount, detail: {  # noqa: E731
            "Amount": amount, "DetailType": detail, detail: {
                ("ItemAccountRef" if detail == "SalesItemLineDetail"
                 else "AccountRef"): {"value": "79" if detail
                                      == "SalesItemLineDetail" else "64"},
                "CustomerRef": {"value": "GONE"}}}
        objects = {
            "Account": list(qbo_grammar.CHART),
            "Customer": [{"Id": "CU1", "DisplayName": "Maple", "Job": False}],
            "Invoice": [{"Id": "I1", "TxnDate": "2026-07-01", "TotalAmt": 100,
                         "ARAccountRef": {"value": "84"},
                         "Line": [line(100, "SalesItemLineDetail")]}],
            "Bill": [{"Id": "B1", "TxnDate": "2026-07-02", "TotalAmt": 400,
                      "APAccountRef": {"value": "33"},
                      "Line": [line(400, "AccountBasedExpenseLineDetail")]}],
        }
        finding = next(item for item in analyzed(objects).findings
                       if item.defect_type == "negative_job_margin")
        self.assertEqual(finding.affected_id, "GONE")
        self.assertEqual(finding.evidence_status, "missing")
        self.assertIn("not in the pull", finding.title)
        self.assertIsNotNone(finding.evidence_request)

    def test_every_finding_names_a_document_or_account_from_the_pull(self):
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            documents = {(kind, str(txn.get("Id")))
                         for kind in POSTING_TYPES
                         for txn in (objects.get(kind) or [])}
            accounts = {str(account.get("Id"))
                        for account in objects.get("Account") or []}
            customers = {str(customer.get("Id"))
                         for customer in objects.get("Customer") or []}
            for finding in analyzed(objects).findings:
                if finding.affected_type in POSTING_TYPES:
                    self.assertIn((finding.affected_type, finding.affected_id),
                                  documents, f"seed {seed}: {finding.defect_type}")
                elif finding.affected_type == "Account":
                    self.assertIn(finding.affected_id, accounts,
                                  f"seed {seed}: {finding.defect_type}")
                elif finding.affected_type == "Customer":
                    # A customer we do not hold is allowed, but only when the
                    # finding says so rather than claiming sufficient evidence.
                    if finding.affected_id not in customers:
                        self.assertEqual(
                            finding.evidence_status, "missing",
                            f"seed {seed}: {finding.defect_type} claims "
                            "sufficient evidence for a customer not in the pull")

    def test_every_defect_type_is_one_the_registry_advertises(self):
        """A finding of a type no check claims reaches a client with nothing
        explaining it, and the coverage report cannot count it."""
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            for finding in analyzed(objects).findings:
                self.assertIn(finding.defect_type,
                              work_engine.AUTOMATED_DEFECT_TYPES,
                              f"seed {seed}")
                self.assertIn(finding.severity, LEGAL_SEVERITIES, f"seed {seed}")

    def test_no_two_findings_share_a_key(self):
        """Keys address a proposal and a decision. A collision would apply one
        person's approval to a different finding."""
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            keys = [finding.key for finding in analyzed(objects).findings]
            self.assertEqual(len(keys), len(set(keys)), f"seed {seed}")


class TheSameBooksGiveTheSameAnswer(unittest.TestCase):

    def test_analyzing_twice_produces_the_same_findings_in_order(self):
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            first = [(item.key, item.reason) for item in analyzed(objects).findings]
            second = [(item.key, item.reason) for item in analyzed(objects).findings]
            self.assertEqual(first, second, f"seed {seed}")


class NothingIsFoundInAnEmptyCompany(unittest.TestCase):
    """A detector that fires on a company with no transactions is broken, and
    check 16 is the one most likely to: 'no jobs configured' is trivially true
    of a client who has not traded yet."""

    def test_a_chart_with_no_documents_produces_no_document_findings(self):
        analysis = analyzed({"Account": list(qbo_grammar.CHART)})
        self.assertEqual([item for item in analysis.findings
                          if item.affected_type in POSTING_TYPES], [])

    def test_a_company_with_no_activity_is_not_told_to_configure_jobs(self):
        analysis = analyzed({"Account": list(qbo_grammar.CHART), "Customer": []})
        self.assertEqual([item for item in analysis.findings
                          if item.defect_type
                          == "project_profitability_configuration"], [])

    def test_an_entirely_empty_pull_produces_nothing_at_all(self):
        self.assertEqual(analyzed({}).findings, [])


class CoverageAlwaysReportsAStatus(unittest.TestCase):

    def test_every_advertised_check_reports_a_legal_status(self):
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            coverage = analyzed(objects).coverage
            self.assertEqual(len(coverage["checks"]), len(work_engine.CHECKS),
                             f"seed {seed}")
            for check in coverage["checks"]:
                self.assertIn(check["status"], LEGAL_STATUSES, f"seed {seed}")

    def test_a_balance_check_never_reads_clean_without_a_ledger(self):
        """`clean` means the check ran and found nothing. A check that could not
        run has not found nothing, and this is the assertion that keeps those
        two apart for every generated company rather than one fixture.
        """
        needs_ledger = {check["id"] for check in work_engine.CHECKS
                        if check.get("requires") in {"bank_statement",
                                                     "trusted_ledger"}}
        self.assertTrue(needs_ledger, "no check declares it needs a ledger")
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            analysis = analyzed(objects)
            if work_engine.trusted_ledger(
                    declare_pull(copy.deepcopy(objects), source="fixture"),
                    {})[1]:
                continue
            for check in analysis.coverage["checks"]:
                if check["id"] in needs_ledger:
                    self.assertNotEqual(check["status"], "clean", f"seed {seed}")


class TheGeneratorReachesTheDetectors(unittest.TestCase):
    """These properties are only worth running if the companies actually make
    checks fire. A generator that never triggers a detector proves nothing."""

    def test_the_generated_companies_produce_findings_of_several_types(self):
        found = set()
        for seed in range(COMPANIES):
            objects = qbo_grammar.company(seed, documents=5, damage=2)
            found.update(item.defect_type for item in analyzed(objects).findings)
        self.assertGreaterEqual(
            len(found), 2,
            f"the generator only ever triggered {sorted(found)}, so these "
            "properties are barely exercising the detectors")


if __name__ == "__main__":
    unittest.main()
