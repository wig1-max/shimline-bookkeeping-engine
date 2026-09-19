"""The third verification: QuickBooks has to agree with our reconstruction.

Shimline derives postings from documents. Beancount recomputes the same
postings from scratch. This is the third engine, and it is the one an accountant
can check without taking anything on trust -- it is the report they were going
to open anyway.

The interesting failure is not disagreement. It is *false agreement*: a reading
of the report that quietly skips a row would let an incomplete reconstruction
tie against a shortened report, which is the one outcome that must be
impossible.
"""
import json
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import crm, crypto, postings, qbo_reports, work_engine  # noqa: E402
from shimline.qbo_adapter import declare_pull  # noqa: E402
from shimline.postings import ProviderReportError, provider_balances
from shimline.qbo_reports import REPORTS_BY_NAME

CHART = [
    {"Id": "35", "Name": "Chequing", "AccountType": "Bank"},
    {"Id": "33", "Name": "Accounts Payable", "AccountType": "Accounts Payable"},
    {"Id": "79", "Name": "Contract Revenue", "AccountType": "Income"},
    {"Id": "64", "Name": "Materials", "AccountType": "Cost of Goods Sold"},
    {"Id": "84", "Name": "A/R", "AccountType": "Accounts Receivable"},
]


def data_row(account_id, name, debit="", credit=""):
    return {"type": "Data", "ColData": [
        {"value": name, "id": account_id}, {"value": debit}, {"value": credit}]}


def report(rows):
    return {"Header": {"ReportName": "TrialBalance"}, "Rows": {"Row": rows}}


class ReadingTheReport(unittest.TestCase):

    def test_debits_and_credits_become_signed_balances(self):
        balances = provider_balances(report([
            data_row("35", "Chequing", debit="1,234.56"),
            data_row("79", "Contract Revenue", credit="1234.56")]))
        self.assertEqual(balances, {"35": Decimal("1234.56"),
                                    "79": Decimal("-1234.56")})

    def test_currency_symbols_and_separators_are_read(self):
        balances = provider_balances(report([
            data_row("35", "Chequing", debit="$12,345.67")]))
        self.assertEqual(balances["35"], Decimal("12345.67"))

    def test_section_summaries_are_not_counted_twice(self):
        """A section summary subtotals rows already read. Adding it would
        double every account that sits under a parent."""
        balances = provider_balances(report([
            {"type": "Section",
             "Rows": {"Row": [data_row("64", "Materials", debit="100.00"),
                              data_row("35", "Chequing", debit="50.00")]},
             "Summary": {"ColData": [{"value": "Total Expenses"},
                                     {"value": "150.00"}, {"value": ""}]}}]))
        self.assertEqual(balances, {"64": Decimal("100.00"),
                                    "35": Decimal("50.00")})

    def test_a_total_line_is_ignored(self):
        balances = provider_balances(report([
            data_row("35", "Chequing", debit="100.00"),
            {"type": "Data", "ColData": [{"value": "TOTAL"},
                                         {"value": "100.00"}, {"value": ""}]}]))
        self.assertEqual(balances, {"35": Decimal("100.00")})

    def test_an_unidentifiable_account_row_is_refused_not_skipped(self):
        """Skipping it would let an incomplete reconstruction agree with a
        shortened report -- false agreement, the worst outcome available."""
        with self.assertRaises(ProviderReportError) as caught:
            provider_balances(report([
                {"type": "Data", "ColData": [{"value": "Petty Cash"},
                                             {"value": "40.00"}, {"value": ""}]}]))
        self.assertIn("names no account id", str(caught.exception))

    def test_an_unreadable_amount_is_refused(self):
        with self.assertRaises(ProviderReportError):
            provider_balances(report([data_row("35", "Chequing", debit="n/a")]))

    def test_the_same_account_appearing_twice_is_summed(self):
        balances = provider_balances(report([
            data_row("35", "Chequing", debit="100.00"),
            data_row("35", "Chequing", credit="30.00")]))
        self.assertEqual(balances["35"], Decimal("70.00"))


class Agreement(unittest.TestCase):

    def _objects(self):
        return {
            "Account": list(CHART),
            "Bill": [{"Id": "B1", "TxnDate": "2026-08-06", "TotalAmt": 500.00,
                      "Line": [{"Amount": 500.00,
                                "DetailType": "AccountBasedExpenseLineDetail",
                                "AccountBasedExpenseLineDetail": {
                                    "AccountRef": {"value": "64"}}}]}],
            "BillPayment": [{"Id": "BP1", "TxnDate": "2026-08-25", "TotalAmt": 500.00,
                             "CheckPayment": {"BankAccountRef": {"value": "35"}}}],
        }

    def test_a_matching_provider_report_agrees(self):
        result = postings.verify_against_provider(self._objects(), report([
            data_row("64", "Materials", debit="500.00"),
            data_row("35", "Chequing", credit="500.00")]))
        self.assertTrue(result.agrees, result.differences)
        self.assertEqual(result.accounts_compared, 3)

    def test_a_cent_of_drift_is_a_defect_not_rounding(self):
        result = postings.verify_against_provider(self._objects(), report([
            data_row("64", "Materials", debit="500.01"),
            data_row("35", "Chequing", credit="500.00")]))
        self.assertFalse(result.agrees)
        self.assertEqual(result.differences[0]["account"], "64")

    def test_a_balance_the_provider_holds_and_we_missed_is_a_failure(self):
        """This is the whole point: a transaction type we never read shows up
        here as an account we have no balance for."""
        result = postings.verify_against_provider(self._objects(), report([
            data_row("64", "Materials", debit="500.00"),
            data_row("35", "Chequing", credit="500.00"),
            data_row("84", "A/R", debit="900.00")]))
        self.assertFalse(result.agrees)
        self.assertIn("did not reconstruct", result.differences[0]["reason"])

    def test_an_incomplete_reconstruction_refuses_before_comparing(self):
        """An incomplete ledger disagreeing with the provider says nothing
        about whether the derivation is right."""
        objects = self._objects()
        objects["Transfer"] = [{"Id": "T1", "Amount": 100.00,
                                "FromAccountRef": {"value": "35"}}]
        result = postings.verify_against_provider(objects, report([]))
        self.assertFalse(result.agrees)
        self.assertIn("could not be fully reconstructed", result.note)

    def test_an_account_at_zero_on_both_sides_is_not_a_difference(self):
        """QuickBooks omits zero rows. An A/R raised and settled in the same
        month is zero on our side and absent on theirs, and that is agreement."""
        objects = {
            "Account": list(CHART),
            "Invoice": [{"Id": "I1", "TxnDate": "2026-08-04", "TotalAmt": 900.00,
                         "Line": [{"Amount": 900.00, "DetailType": "SalesItemLineDetail",
                                   "SalesItemLineDetail": {
                                       "ItemAccountRef": {"value": "79"}}}]}],
            "Payment": [{"Id": "PM1", "TxnDate": "2026-08-20", "TotalAmt": 900.00,
                         "DepositToAccountRef": {"value": "35"}}],
        }
        result = postings.verify_against_provider(objects, report([
            data_row("35", "Chequing", debit="900.00"),
            data_row("79", "Contract Revenue", credit="900.00")]))
        self.assertTrue(result.agrees, result.differences)


class TheGate(unittest.TestCase):
    """A disagreement withdraws the ledger. It never accuses the client.

    Our reconstruction and QuickBooks read the same books two ways. If they
    disagree, at least one of us is wrong, and telling a client their books do
    not reconcile on that basis would be an accusation we cannot support.
    """

    def _objects(self):
        return {
            "Account": list(CHART),
            "Purchase": [{"Id": "P1", "TxnDate": "2026-08-06", "TotalAmt": 500.00,
                          "AccountRef": {"value": "35"},
                          "Line": [{"Amount": 500.00,
                                    "DetailType": "AccountBasedExpenseLineDetail",
                                    "AccountBasedExpenseLineDetail": {
                                        "AccountRef": {"value": "64"}}}]}],
        }

    def _evidence(self, provider=None, statement_balance="-500.00"):
        evidence = {
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "bank_statements": [{"account_id": "35", "period_end": "2026-08-31",
                                 "ending_ledger_balance": statement_balance}]}
        if provider is not None:
            evidence["provider_trial_balance"] = provider
        return evidence

    def _check(self, analysis, check_id):
        return next(item for item in analysis.coverage["checks"]
                    if item["id"] == check_id)

    def test_no_report_supplied_leaves_the_ledger_usable(self):
        """The provider check is the third engine, not the only one."""
        analysis = work_engine.analyze(declare_pull(self._objects()), self._evidence(),
                                       today=date(2026, 9, 1))
        self.assertEqual(analysis.coverage["provider_agreement"]["status"],
                         "not_supplied")
        self.assertEqual(self._check(analysis, "04")["status"], "clean")

    def test_agreement_leaves_the_ledger_usable(self):
        analysis = work_engine.analyze(declare_pull(self._objects()), self._evidence(report([
            data_row("64", "Materials", debit="500.00"),
            data_row("35", "Chequing", credit="500.00")])),
            today=date(2026, 9, 1))
        self.assertEqual(analysis.coverage["provider_agreement"]["status"], "agrees")
        self.assertEqual(self._check(analysis, "04")["status"], "clean")

    def test_disagreement_blocks_the_check_rather_than_failing_it(self):
        analysis = work_engine.analyze(declare_pull(self._objects()), self._evidence(report([
            data_row("64", "Materials", debit="600.00"),
            data_row("35", "Chequing", credit="600.00")])),
            today=date(2026, 9, 1))
        self.assertEqual(analysis.coverage["provider_agreement"]["status"],
                         "disagrees")
        self.assertEqual(self._check(analysis, "04")["status"], "blocked")
        self.assertEqual(
            [item for item in analysis.findings
             if item.defect_type == "unreconciled_account"], [])

    def test_disagreement_names_itself_in_the_missing_evidence(self):
        """A reviewer must not be left chasing the client for a document they
        already sent, when the gap is on our side."""
        analysis = work_engine.analyze(declare_pull(self._objects()), self._evidence(report([
            data_row("64", "Materials", debit="600.00"),
            data_row("35", "Chequing", credit="600.00")])),
            today=date(2026, 9, 1))
        gaps = " ".join(analysis.coverage["missing_evidence"])
        self.assertIn("QuickBooks agrees with", gaps)

    def test_an_unreadable_report_blocks_rather_than_being_ignored(self):
        """Ignoring it would silently downgrade three verifications to two."""
        analysis = work_engine.analyze(declare_pull(self._objects()), self._evidence(report([
            {"type": "Data", "ColData": [{"value": "Petty Cash"},
                                         {"value": "40.00"}, {"value": ""}]}])),
            today=date(2026, 9, 1))
        self.assertEqual(analysis.coverage["provider_agreement"]["status"],
                         "unreadable")
        self.assertEqual(self._check(analysis, "04")["status"], "blocked")

    def test_reconciliation_says_the_gap_is_ours(self):
        records = work_engine.reconcile(declare_pull(self._objects()), self._evidence(report([
            data_row("64", "Materials", debit="600.00"),
            data_row("35", "Chequing", credit="600.00")])))
        self.assertEqual(records[0]["status"], "no_source")
        self.assertIn("QuickBooks agrees with", records[0]["missing_source"])

    def test_an_incomplete_reconstruction_is_not_reported_as_disagreement(self):
        objects = self._objects()
        objects["Transfer"] = [{"Id": "T1", "Amount": 100.00,
                                "FromAccountRef": {"value": "35"}}]
        analysis = work_engine.analyze(declare_pull(objects), self._evidence(report([])),
                                       today=date(2026, 9, 1))
        self.assertEqual(analysis.coverage["provider_agreement"]["status"],
                         "not_compared")


class SnapshotPlumbing(unittest.TestCase):
    """The gate is worthless if nothing ever feeds it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "reports.db"
        self.conn = service._db()
        self.conn.execute(
            "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
            ("org_1", "Client", "org_1"))
        for run in ("syn_1", "syn_2"):
            self.conn.execute(
                "INSERT INTO sync_runs(id,connection_id,organization_id,period_start,"
                "period_end,reports_requested) VALUES(?,NULL,'org_1',"
                "'2026-01-01','2026-08-31',1)", (run,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _store(self, name, payload, fetched_at, run="syn_1"):
        self.conn.execute(
            "INSERT INTO source_snapshots(id,organization_id,sync_run_id,report_name,"
            "period_start,period_end,payload_enc,byte_size,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (crm.new_id("snp"), "org_1", run, name, "2026-01-01", "2026-08-31",
             crypto.encrypt(json.dumps(payload), crypto.QUICKBOOKS_REPORTS),
             len(json.dumps(payload)), fetched_at))
        self.conn.commit()

    def test_the_newest_trial_balance_is_the_one_returned(self):
        self._store("TrialBalance", report([data_row("35", "Chequing", debit="1.00")]),
                    "2026-08-01T00:00:00")
        self._store("TrialBalance", report([data_row("35", "Chequing", debit="2.00")]),
                    "2026-09-01T00:00:00", run="syn_2")
        found = qbo_reports.latest_report(self.conn, "org_1", "TrialBalance")
        self.assertEqual(provider_balances(found["payload"])["35"], Decimal("2.00"))

    def test_another_report_is_not_mistaken_for_the_trial_balance(self):
        self._store("BalanceSheet", report([data_row("35", "Chequing", debit="9.00")]),
                    "2026-09-01T00:00:00")
        self.assertIsNone(
            qbo_reports.latest_report(self.conn, "org_1", "TrialBalance"))

    def test_a_never_synced_client_yields_nothing_rather_than_agreement(self):
        """Absent must read as 'the third check did not run', never as a pass."""
        self.assertIsNone(
            qbo_reports.latest_report(self.conn, "org_1", "TrialBalance"))

    def test_a_verification_run_never_falls_back_to_an_older_trial_balance(self):
        self._store("TrialBalance", report([
            data_row("35", "Chequing", debit="1.00")]),
            "2026-08-01T00:00:00", run="syn_1")
        self.assertIsNotNone(
            qbo_reports.latest_report(self.conn, "org_1", "TrialBalance"))
        self.assertIsNone(
            qbo_reports.report_for_run(self.conn, "syn_2", "TrialBalance"))

    def test_a_verification_run_reads_its_own_fresh_snapshot(self):
        payload = report([data_row("35", "Chequing", debit="2.00")])
        self._store("TrialBalance", payload, "2026-09-01T00:00:00", run="syn_2")
        found = qbo_reports.report_for_run(self.conn, "syn_2", "TrialBalance")
        self.assertEqual(provider_balances(found["payload"])["35"], Decimal("2.00"))


class ReportRegistration(unittest.TestCase):

    def test_the_trial_balance_is_one_of_the_reports_we_pull(self):
        """Nothing can compare against a report that is never fetched."""
        self.assertIn("TrialBalance", REPORTS_BY_NAME)

    def test_it_is_pulled_over_a_period_not_as_of_a_date(self):
        params = REPORTS_BY_NAME["TrialBalance"].params("2026-01-01", "2026-08-31")
        self.assertEqual(params["start_date"], "2026-01-01")
        self.assertEqual(params["end_date"], "2026-08-31")
        self.assertEqual(params["accounting_method"], "Accrual")


if __name__ == "__main__":
    unittest.main()
