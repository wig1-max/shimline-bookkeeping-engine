"""Check E2 end to end: statement lines the books cannot account for.

The danger this guards against is not missing a finding -- it is manufacturing
one. Every unmatched line is an accusation that the client's bookkeeping is
incomplete, so the tests for what must *not* be reported carry more weight than
the ones for what must.
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
from shimline import statement_store, work_engine  # noqa: E402
from shimline.qbo_adapter import declare_pull  # noqa: E402
from shimline.statements import ParsedStatement, StatementLine  # noqa: E402

CHART = [
    {"Id": "100", "Name": "Chequing", "AccountType": "Bank"},
    {"Id": "500", "Name": "Materials", "AccountType": "Expense"},
    {"Id": "120", "Name": "A/R", "AccountType": "Accounts Receivable"},
]
VENDORS = [{"Id": "9", "DisplayName": "HOME DEPOT"},
           {"Id": "10", "DisplayName": "RONA"}]


def purchase(ident, day, amount, vendor="9"):
    return {"Id": ident, "TxnDate": day, "TotalAmt": amount, "SyncToken": "0",
            "AccountRef": {"value": "100"}, "EntityRef": {"value": vendor},
            "DocNumber": f"D{ident}",
            "Line": [{"Amount": amount, "DetailType": "AccountBasedExpenseLineDetail",
                      "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "500"}}}]}


def statement(lines, *, closing="0.00", account="100"):
    return {"statement_id": "bst_1", "account_id": account,
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "ending_ledger_balance": closing, "currency": "CAD", "lines": lines}


def bank_line(ordinal, day, amount, description):
    return {"ordinal": ordinal, "posted_date": day, "amount": amount,
            "description": description, "memo": "", "fitid": None,
            "txn_type": "DEBIT"}


def findings_of(analysis, defect_type):
    return [item for item in analysis.findings if item.defect_type == defect_type]


def analyze(objects, statements, **evidence):
    payload = {"period_start": "2026-08-01", "period_end": "2026-08-31",
               "bank_statements": statements}
    payload.update(evidence)
    return work_engine.analyze(declare_pull(objects, source="fixture"), payload,
                               today=date(2026, 9, 1))


class StatementLineFindings(unittest.TestCase):

    def test_a_line_the_ledger_accounts_for_is_not_reported(self):
        objects = {"Account": CHART, "Vendor": VENDORS,
                   "Purchase": [purchase("1", "2026-08-04", 412.55)]}
        analysis = analyze(objects, [statement(
            [bank_line(0, "2026-08-05", "-412.55", "HOMEDEPOT #7021 OTTAWA ON")],
            closing="-412.55")])
        self.assertEqual(findings_of(analysis, "missing_transaction"), [])

    def test_a_line_nothing_accounts_for_is_reported(self):
        objects = {"Account": CHART, "Vendor": VENDORS,
                   "Purchase": [purchase("1", "2026-08-04", 412.55)]}
        analysis = analyze(objects, [statement([
            bank_line(0, "2026-08-05", "-412.55", "HOMEDEPOT #7021 OTTAWA ON"),
            bank_line(1, "2026-08-11", "-1875.00", "UNKNOWN VENDOR 9931")],
            closing="-2287.55")])
        found = findings_of(analysis, "missing_transaction")
        self.assertEqual(len(found), 1)
        self.assertIn("1875.00", found[0].reason)
        self.assertIn("UNKNOWN VENDOR 9931", found[0].reason)

    def test_a_reported_line_never_proposes_a_correction(self):
        """A bank descriptor does not say which account the charge belongs to.
        Proposing one would put an invented coding in front of an approver."""
        objects = {"Account": CHART, "Vendor": VENDORS, "Purchase": []}
        analysis = analyze(objects, [statement(
            [bank_line(0, "2026-08-11", "-1875.00", "UNKNOWN VENDOR 9931")],
            closing="-1875.00")])
        found = findings_of(analysis, "missing_transaction")
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].proposal)
        self.assertEqual(found[0].evidence_status, "insufficient")
        self.assertIn("receipt", found[0].evidence_request.lower())
        self.assertEqual(analysis.proposals, [])

    def test_an_outstanding_cheque_is_not_a_finding(self):
        """A ledger entry that has not cleared is the ordinary state of a
        month-end. Flagging it would fire on nearly every client."""
        objects = {"Account": CHART, "Vendor": VENDORS,
                   "Purchase": [purchase("1", "2026-08-04", 412.55),
                                purchase("2", "2026-08-29", 900.00, vendor="10")]}
        analysis = analyze(objects, [statement(
            [bank_line(0, "2026-08-05", "-412.55", "HOMEDEPOT #7021 OTTAWA ON")],
            closing="-412.55")])
        self.assertEqual(findings_of(analysis, "missing_transaction"), [])
        summary = analysis.coverage["statement_matching"]["100"]
        self.assertEqual(summary["unmatched_ledger"], 1)

    def test_an_ambiguous_line_is_neither_matched_nor_accused(self):
        """Two identical candidates: the line is not proven present and not
        proven absent. Saying either would be a guess dressed as a result."""
        objects = {"Account": CHART, "Vendor": VENDORS,
                   "Purchase": [purchase("1", "2026-08-04", 412.55, vendor="10"),
                                purchase("2", "2026-08-04", 412.55, vendor="10")]}
        analysis = analyze(objects, [statement([
            bank_line(0, "2026-08-04", "-412.55", "RONA INC #556")],
            closing="-825.10")])
        self.assertEqual(findings_of(analysis, "missing_transaction"), [])
        summary = analysis.coverage["statement_matching"]["100"]
        self.assertEqual(summary["ambiguous"], 1)
        self.assertEqual(summary["matched"], 0)

    def test_nothing_is_reported_when_the_ledger_cannot_be_reconstructed(self):
        """An unreadable transaction type blocks the ledger, and with it every
        conclusion about what the ledger does or does not contain."""
        objects = {"Account": CHART, "Vendor": VENDORS,
                   "Purchase": [purchase("1", "2026-08-04", 412.55)],
                   "Transfer": [{"Id": "77", "TxnDate": "2026-08-06", "Amount": 500}]}
        analysis = analyze(objects, [statement(
            [bank_line(0, "2026-08-11", "-1875.00", "UNKNOWN VENDOR 9931")],
            closing="-1875.00")])
        self.assertEqual(findings_of(analysis, "missing_transaction"), [])
        self.assertEqual(analysis.coverage["statement_matching"], {})

    def test_an_unmapped_statement_produces_no_line_findings(self):
        objects = {"Account": CHART, "Vendor": VENDORS, "Purchase": []}
        unmapped = statement(
            [bank_line(0, "2026-08-11", "-1875.00", "UNKNOWN 9931")], account="")
        analysis = analyze(objects, [unmapped])
        self.assertEqual(findings_of(analysis, "missing_transaction"), [])

    def test_a_statement_without_lines_still_reconciles_on_the_balance(self):
        """Line-level matching is an addition, not a precondition. A statement
        that carries only a closing balance must keep working as before."""
        objects = {"Account": CHART, "Vendor": VENDORS,
                   "Purchase": [purchase("1", "2026-08-04", 412.55)]}
        records = work_engine.reconcile(declare_pull(objects, source="fixture"), {
            "period_end": "2026-08-31",
            "bank_statements": [{"account_id": "100", "period_end": "2026-08-31",
                                 "ending_ledger_balance": "-412.55"}]})
        self.assertEqual(records[0]["status"], "reconciled")
        self.assertNotIn("line_matching", records[0])

    def test_reconciliation_carries_the_line_counts(self):
        """A tied balance is not a tied ledger: two errors that cancel agree
        exactly on the closing balance."""
        objects = {"Account": CHART, "Vendor": VENDORS,
                   "Purchase": [purchase("1", "2026-08-04", 412.55)]}
        records = work_engine.reconcile(declare_pull(objects, source="fixture"), {
            "period_end": "2026-08-31",
            "bank_statements": [statement(
                [bank_line(0, "2026-08-05", "-412.55", "HOMEDEPOT #7021"),
                 bank_line(1, "2026-08-06", "-99.00", "MYSTERY")],
                closing="-412.55")]})
        self.assertEqual(records[0]["status"], "reconciled")
        self.assertEqual(records[0]["line_matching"]["unmatched_bank"], 1)

    def test_the_corroborated_path_still_proposes(self):
        """The pre-supplied missing_transactions key is a different, stronger
        claim -- statement and source document agree -- and still writes."""
        objects = {"Account": CHART, "Vendor": VENDORS, "Purchase": []}
        analysis = analyze(objects, [{
            "account_id": "100", "period_end": "2026-08-31",
            "ending_ledger_balance": "0.00",
            "missing_transactions": [{"Id": "M1", "DocNumber": "R-88",
                                      "TotalAmt": 250.00}]}])
        found = findings_of(analysis, "missing_transaction")
        self.assertEqual(len(found), 1)
        self.assertIsNotNone(found[0].proposal)


class EvidenceFromStorage(unittest.TestCase):
    """The stored statement must arrive in the shape the engine matches on."""

    def setUp(self):
        # The real schema, built the way the application builds it. A
        # hand-rolled copy of the two tables would prove the store works against
        # a schema nobody runs, and the migration is part of what is under test.
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "statements.db"
        self.conn = service._db()
        self.conn.execute(
            "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
            ("org_1", "Client", "org_1"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _store(self, qbo_account_id="100"):
        parsed = ParsedStatement(
            bank_account_id="8842", period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31), closing_balance=Decimal("-412.55"),
            content_sha256="a" * 64, source_filename="aug.ofx",
            lines=[StatementLine(posted_date=date(2026, 8, 5),
                                 amount=Decimal("-412.55"),
                                 description="HOMEDEPOT #7021 OTTAWA ON")])
        return statement_store.store(self.conn, organization_id="org_1",
                                     statement=parsed,
                                     qbo_account_id=qbo_account_id)

    def test_evidence_carries_the_lines(self):
        self._store()
        evidence = statement_store.evidence(
            self.conn, "org_1", period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31))
        record = evidence["bank_statements"][0]
        self.assertEqual(len(record["lines"]), 1)
        self.assertEqual(record["lines"][0]["amount"], "-412.55")
        self.assertEqual(record["lines"][0]["ordinal"], 0)

    def test_stored_lines_drive_the_engine(self):
        self._store()
        evidence = dict(statement_store.evidence(
            self.conn, "org_1", period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31)),
            period_start="2026-08-01", period_end="2026-08-31")
        objects = {"Account": CHART, "Vendor": VENDORS,
                   "Purchase": [purchase("1", "2026-08-04", 412.55)]}
        analysis = work_engine.analyze(declare_pull(objects), evidence,
                                       today=date(2026, 9, 1))
        self.assertEqual(findings_of(analysis, "missing_transaction"), [])
        self.assertEqual(analysis.coverage["statement_matching"]["100"]["matched"], 1)

    def test_an_unmapped_statement_supplies_nothing_at_all(self):
        self._store(qbo_account_id=None)
        evidence = statement_store.evidence(
            self.conn, "org_1", period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31))
        self.assertEqual(evidence, {})


if __name__ == "__main__":
    unittest.main()
