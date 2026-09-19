"""Acceptance tests for Bookkeeping Work Engine v0."""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY", "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from shimline import auth, beancount_export, crm, work_engine  # noqa: E402
from shimline.postings import POSTING_TYPES, derive_ledger  # noqa: E402
from shimline.qbo_adapter import declare_pull  # noqa: E402
from shimline.synthetic_books import (SEEDED_DEFECTS, SyntheticQBOAdapter,
                                      generate_companies, generate_company,
                                      trial_balance)  # noqa: E402


class FiftyCompanyAcceptanceTests(unittest.TestCase):
    def test_fifty_corrupted_companies_close_to_the_golden_ledger(self):
        for company in generate_companies(50):
            adapter = SyntheticQBOAdapter(company, stale_once=True, fail_after_commit_once=True)
            result = work_engine.run_synthetic(adapter)
            detected = {item.defect_type for item in result.analysis.findings}
            self.assertEqual(detected, company.seeded_defects, company.company_id)
            self.assertEqual(result.reconciliation["status"], "reconciled", company.company_id)
            self.assertEqual(trial_balance(adapter.pull_all()), company.golden_trial_balance(),
                             company.company_id)
            self.assertEqual(result.unique_writes, len(result.analysis.proposals), company.company_id)
            self.assertEqual(len(adapter.applied_request_ids), 4, company.company_id)
            self.assertTrue(all(item.status == "reconciled" for item in result.analysis.proposals))
            self.assertEqual(len(result.working_papers["package_hash"]), 64)
            self.assertIn("stale_receivable:I-STALE", result.working_papers["residual_exceptions"])
            self.assertIn("unapplied_payment:PAY1", result.working_papers["residual_exceptions"])

    def test_missing_evidence_requests_a_document_and_never_proposes_a_guess(self):
        company = generate_company(1)
        analysis = work_engine.analyze(company.objects, company.evidence)
        stale = next(item for item in analysis.findings if item.defect_type == "stale_receivable")
        self.assertEqual(stale.evidence_status, "missing")
        self.assertIsNotNone(stale.evidence_request)
        self.assertIsNone(stale.proposal)

    def test_the_corpus_contains_every_posting_type_the_engine_handles(self):
        """Counting documents, not dictionary keys.

        This corpus held four of twelve types for most of its life. `Deposit` and
        `JournalEntry` read as covered because the generator declared the keys
        and set them to `[]`, so `"Deposit" in objects` was true and the acceptance
        run, the golden ledger and the Beancount oracle had all never seen one.
        An enumerator that finds nothing proves nothing -- so count the rows.
        """
        rows: dict[str, int] = {kind: 0 for kind in POSTING_TYPES}
        for company in generate_companies(50):
            for kind in POSTING_TYPES:
                rows[kind] += len(company.objects.get(kind) or [])
        empty = sorted(kind for kind, count in rows.items() if count == 0)
        self.assertEqual(
            empty, [],
            f"the engine derives these types and the corpus contains none of "
            f"them, so nothing grades them at scale: {empty}")

    def test_the_oracle_is_not_an_engine_input(self):
        company = generate_company(2)
        self.assertNotIn("golden", work_engine.analyze.__code__.co_varnames)
        detected = {item.defect_type for item in work_engine.analyze(company.objects, company.evidence).findings}
        self.assertEqual(detected, SEEDED_DEFECTS)


class ProposalControlTests(unittest.TestCase):
    def test_invalid_state_transitions_are_rejected(self):
        proposal = work_engine.Proposal("p", "f", "correcting_entry", "JournalEntry", None,
                                        {}, {}, "reason")
        with self.assertRaises(ValueError):
            work_engine.transition(proposal, "executed")
        work_engine.transition(proposal, "reviewed")
        work_engine.transition(proposal, "approved")
        with self.assertRaises(ValueError):
            work_engine.edit(proposal, {"changed": True})

    def test_edit_bumps_version_and_requires_fresh_review(self):
        proposal = work_engine.Proposal("p", "f", "correcting_entry", "JournalEntry", None,
                                        {}, {"a": 1}, "reason", status="reviewed")
        work_engine.edit(proposal, {"a": 2})
        self.assertEqual((proposal.version, proposal.status, proposal.proposed),
                         (2, "proposed", {"a": 2}))

    def test_stale_token_with_a_changed_reviewed_field_forces_re_review(self):
        company = generate_company(9)
        adapter = SyntheticQBOAdapter(company, stale_once=False, fail_after_commit_once=False)
        analysis = work_engine.analyze(company.objects, company.evidence)
        proposal = next(item for item in analysis.proposals if item.action == "assign_project")
        current = next(item for item in adapter.objects["Purchase"] if item["Id"] == proposal.object_id)
        current["SyncToken"] = "1"
        current["Line"][0]["Description"] = "Changed by another user"
        work_engine.transition(proposal, "reviewed")
        work_engine.transition(proposal, "approved")
        with self.assertRaisesRegex(RuntimeError, "fresh review"):
            work_engine.execute(proposal, adapter)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "engine.db"
        self.conn = service._db()
        self.user = auth.create_user(self.conn, "engine@example.invalid", "Engine Reviewer",
                                     "local-test-password-only", "owner")
        self.org = crm.new_id("org")
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                          (self.org, "Synthetic Co", self.org))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_canonical_records_and_control_history_are_persisted(self):
        company = generate_company(3)
        analysis = work_engine.analyze(company.objects, company.evidence)
        run_id = work_engine.persist_analysis(
            self.conn, organization_id=self.org, engagement_id=None,
            connection_id=None, analysis=analysis)
        counts = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("bookkeeping_accounts", "bookkeeping_entities", "bookkeeping_projects",
                          "bookkeeping_classifications", "bookkeeping_transactions",
                          "bookkeeping_transaction_lines", "bookkeeping_allocations",
                          "bookkeeping_findings", "bookkeeping_evidence_requests",
                          "bookkeeping_proposals")
        }
        self.assertTrue(all(value > 0 for value in counts.values()), counts)
        self.assertEqual(len(work_engine.proposal_rows(self.conn, run_id)), 4)

    def test_accountant_console_renders_and_exports_working_papers(self):
        engagement = crm.new_id("eng")
        self.conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status) VALUES(?,?,?,'in_progress')",
            (engagement, self.org, "Free Cleanup Check"))
        company = generate_company(4)
        run_id = work_engine.persist_analysis(
            self.conn, organization_id=self.org, engagement_id=engagement,
            connection_id=None, analysis=work_engine.analyze(company.objects, company.evidence))
        http = TestClient(service.app, base_url="https://testserver")
        login = http.post("/admin/login", data={
            "email": "engine@example.invalid", "password": "local-test-password-only",
            "next": "/admin"})
        self.assertEqual(login.status_code, 200)
        page = http.get(f"/admin/bookkeeping/{run_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Cleanup review", page.text)
        self.assertIn("Missing evidence", page.text)
        # The console must show what was not examined, not only what was.
        self.assertIn("Check coverage", page.text)
        self.assertIn("Control account reconciliation", page.text)
        for check in work_engine.CHECKS:
            self.assertIn(check["title"], page.text, check["id"])
        self.assertIn("coverage-manual", page.text)
        self.assertIn("coverage-blocked", page.text)
        package = http.get(f"/admin/bookkeeping/{run_id}/working-papers")
        self.assertEqual(package.status_code, 200)
        self.assertEqual(package.json()["engine"], "Bookkeeping Work Engine v0")
        self.assertEqual(len(package.json()["package_hash"]), 64)


class PostedKindsTests(unittest.TestCase):
    """Nothing keeps its own copy of the list of posting types.

    Three modules have held a hand-written copy of it and two of them went stale
    when the engine gained settlement and reversal types. `beancount_export`
    silently stopped exporting the new documents while still counting them;
    `work_engine` reported a ledger as derivable when it was not. A second copy
    of a list is a defect waiting for the right day.
    """

    def test_no_module_keeps_its_own_copy_of_the_posting_types(self):
        self.assertIs(work_engine.POSTED_KINDS, POSTING_TYPES)
        self.assertIs(beancount_export.POSTED_KINDS, POSTING_TYPES)

    def test_a_client_whose_every_document_is_a_till_sale_is_not_called_derivable(self):
        """The failure the stale copy caused, named so it cannot come back.

        A cash-basis trades business rings everything through the till, so every
        document is a SalesReceipt. With the stale list none of them counted as
        posted, `postings_derivable` took the "this company has no transactions,
        so zero really is its balance" path, and every balance-comparing check
        reported `clean` -- ran and found nothing -- over books that could not be
        rebuilt at all.
        """
        objects = declare_pull({
            "Account": [{"Id": "35", "Name": "Chequing", "AccountType": "Bank"},
                        {"Id": "79", "Name": "Revenue", "AccountType": "Income"}],
            # No DepositToAccountRef: QuickBooks defaults it to Undeposited Funds
            # without naming the account, so the cash genuinely has nowhere to go.
            "SalesReceipt": [{"Id": "SR1", "TxnDate": "2026-08-30", "TotalAmt": 226.00,
                              "Line": [{"Amount": 200.00,
                                        "DetailType": "SalesItemLineDetail",
                                        "SalesItemLineDetail": {
                                            "ItemAccountRef": {"value": "79"}}}]}],
        }, source="fixture")
        self.assertFalse(derive_ledger(objects).complete)
        self.assertFalse(work_engine.postings_derivable(objects))

    def test_a_company_with_no_transactions_at_all_is_still_derivable(self):
        """The legitimate case the shortcut exists for must survive the fix:
        zero really is the balance of a company that has posted nothing."""
        objects = declare_pull(
            {"Account": [{"Id": "35", "Name": "Chequing", "AccountType": "Bank"}]},
            source="fixture")
        self.assertTrue(work_engine.postings_derivable(objects))


class CheckCoverageTests(unittest.TestCase):
    """Every advertised check reports a status, whether or not it fired."""

    def test_every_registered_check_is_reported_with_a_status(self):
        company = generate_company(5)
        coverage = work_engine.analyze(company.objects, company.evidence).coverage
        reported = {item["id"] for item in coverage["checks"]}
        self.assertEqual(reported, {item["id"] for item in work_engine.CHECKS})
        self.assertEqual(coverage["checks_total"], len(work_engine.CHECKS))
        for item in coverage["checks"]:
            self.assertIn(item["status"], {"clean", "defect", "blocked", "manual"}, item)

    def test_coverage_does_not_claim_more_than_the_engine_computes(self):
        company = generate_company(6)
        coverage = work_engine.analyze(company.objects, company.evidence).coverage
        automated = coverage["checks_automated"]
        self.assertLess(automated, coverage["checks_total"])
        self.assertEqual(
            automated + coverage["checks_blocked"] + coverage["checks_manual"],
            coverage["checks_total"])

    def test_a_blocked_check_names_the_source_that_would_unblock_it(self):
        company = generate_company(7)
        coverage = work_engine.analyze(company.objects, company.evidence).coverage
        blocked = [item for item in coverage["checks"] if item["status"] == "blocked"]
        self.assertTrue(blocked)
        for item in blocked:
            self.assertTrue(item.get("missing_source"), item)

    def test_removing_the_bank_statement_blocks_the_checks_that_need_it(self):
        company = generate_company(8)
        evidence = dict(company.evidence)
        evidence.pop("bank_statement", None)
        coverage = work_engine.analyze(company.objects, evidence).coverage
        by_id = {item["id"]: item for item in coverage["checks"]}
        self.assertEqual(by_id["04"]["status"], "blocked")
        self.assertEqual(by_id["04"]["missing_source"], "bank_statement")
        self.assertEqual(by_id["E2"]["status"], "blocked")

    def test_a_check_that_ran_and_found_nothing_reports_clean_not_absent(self):
        company = generate_company(9)
        findings = [item for item in work_engine.analyze(company.objects, company.evidence).findings
                    if item.defect_type != "duplicate_transaction"]
        report = {item["id"]: item for item in
                  work_engine.check_coverage(findings, company.evidence)}
        self.assertEqual(report["06"]["status"], "clean")


class NewlyAutomatedBookkeeperChecks(unittest.TestCase):
    """Checks 05, 15 and 16 use facts already present in every QBO pull."""

    @staticmethod
    def analyze(objects):
        return work_engine.analyze(declare_pull(objects, source="fixture"), {},
                                   today=date(2026, 9, 30))

    def test_check_05_finds_quickbooks_uncategorized_holding_accounts(self):
        objects = {
            "Account": [
                {"Id": "100", "Name": "Chequing", "AccountType": "Bank"},
                {"Id": "999", "Name": "Ask My Accountant", "AccountType": "Expense"},
            ],
            "Purchase": [{
                "Id": "P1", "TxnDate": "2026-09-01", "DocNumber": "R-1",
                "TotalAmt": 125,
                "Line": [{"Amount": 125, "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"value": "999"}}}],
                "_Postings": [
                    {"account": "999", "debit": "125", "credit": "0"},
                    {"account": "100", "debit": "0", "credit": "125"}],
            }],
        }
        analysis = self.analyze(objects)
        finding = next(item for item in analysis.findings
                       if item.defect_type == "uncategorized_expense")
        self.assertEqual(finding.affected_id, "P1")
        self.assertIn("Ask My Accountant", finding.reason)
        self.assertIsNone(finding.proposal)
        check = next(item for item in analysis.coverage["checks"] if item["id"] == "05")
        self.assertEqual(check["status"], "defect")

    def test_check_15_reports_each_mechanical_balance_sheet_exception(self):
        objects = {
            "Account": [
                # CurrentBalance is QuickBooks' own answer, and the
                # credit-balance rule requires it to agree on the sign before
                # it will call an overdrawn asset a finding.
                {"Id": "100", "Name": "Chequing", "AccountType": "Bank",
                 "CurrentBalance": -5},
                {"Id": "110", "Name": "Undeposited Funds",
                 "AccountType": "Other Current Asset"},
                {"Id": "300", "Name": "Opening Balance Equity", "AccountType": "Equity"},
                {"Id": "500", "Name": "Expense", "AccountType": "Expense"},
            ],
            "JournalEntry": [{
                "Id": "J1", "TxnDate": "2026-09-30",
                "_Postings": [
                    {"account": "110", "debit": "10", "credit": "0"},
                    {"account": "500", "debit": "20", "credit": "0"},
                    {"account": "300", "debit": "0", "credit": "25"},
                    {"account": "100", "debit": "0", "credit": "5"},
                ],
            }],
        }
        findings = [item for item in self.analyze(objects).findings
                    if item.defect_type == "suspicious_balance_sheet"]
        self.assertEqual({item.affected_id for item in findings}, {"100", "110", "300"})
        self.assertTrue(all(item.proposal is None for item in findings))

    def _overdrawn_only_in_our_arithmetic(self, **account_extra):
        """A bank account our reconstruction shows negative, because the money
        that went into it predates the documents we hold."""
        return {
            "Account": [
                dict({"Id": "100", "Name": "Chequing", "AccountType": "Bank"},
                     **account_extra),
                {"Id": "500", "Name": "Materials",
                 "AccountType": "Cost of Goods Sold"},
            ],
            "JournalEntry": [{
                "Id": "J1", "TxnDate": "2026-09-30",
                "_Postings": [{"account": "500", "debit": "80", "credit": "0"},
                              {"account": "100", "debit": "0", "credit": "80"}],
            }],
        }

    def test_an_asset_negative_only_in_our_ledger_is_not_reported(self):
        """The false positive this guard exists for.

        Unguarded, this rule fired on all fifty synthetic companies and on the
        documented conformance company, because neither includes the deposit
        that opened the bank account. A detector that fires on everything is
        worse than no detector, so with nothing to corroborate the sign, the
        rule says nothing.
        """
        findings = [item for item in
                    self.analyze(self._overdrawn_only_in_our_arithmetic()).findings
                    if item.defect_type == "suspicious_balance_sheet"]
        self.assertEqual(findings, [])

    def test_quickbooks_saying_the_account_is_in_funds_silences_it(self):
        """Explicit disagreement, not just absence: QuickBooks says the account
        holds money, so our negative balance is a gap in what we pulled rather
        than a fact about the client's books."""
        objects = self._overdrawn_only_in_our_arithmetic(CurrentBalance=18422.15)
        findings = [item for item in self.analyze(objects).findings
                    if item.defect_type == "suspicious_balance_sheet"]
        self.assertEqual(findings, [])

    def test_both_engines_agreeing_it_is_overdrawn_is_reported(self):
        objects = self._overdrawn_only_in_our_arithmetic(CurrentBalance=-80)
        finding = next(item for item in self.analyze(objects).findings
                       if item.defect_type == "suspicious_balance_sheet")
        self.assertEqual(finding.affected_id, "100")
        self.assertIn("QuickBooks' own balance for it is negative too",
                      finding.reason)

    def test_an_unreadable_provider_balance_is_not_taken_as_agreement(self):
        objects = self._overdrawn_only_in_our_arithmetic(CurrentBalance="overdrawn")
        findings = [item for item in self.analyze(objects).findings
                    if item.defect_type == "suspicious_balance_sheet"]
        self.assertEqual(findings, [])

    def test_check_15_blocks_instead_of_reading_a_partial_ledger(self):
        objects = {
            "Account": [
                {"Id": "100", "Name": "Chequing", "AccountType": "Bank"},
                {"Id": "400", "Name": "Revenue", "AccountType": "Income"},
            ],
            "SalesReceipt": [{
                "Id": "S1", "TxnDate": "2026-09-01", "TotalAmt": 100,
                "Line": [{"Amount": 100, "SalesItemLineDetail": {
                    "ItemAccountRef": {"value": "400"}}}],
            }],
        }
        analysis = self.analyze(objects)
        check = next(item for item in analysis.coverage["checks"] if item["id"] == "15")
        self.assertEqual(check["status"], "blocked")
        self.assertEqual(check["missing_source"], "derived_ledger_postings")
        self.assertFalse(any(item.defect_type == "suspicious_balance_sheet"
                             for item in analysis.findings))

    def test_check_16_leaves_the_no_jobs_choice_to_readiness(self):
        objects = {
            "Account": [], "Customer": [],
            "Invoice": [{"Id": "I1", "TxnDate": "2026-09-01", "Balance": 0,
                         "Line": [], "_Postings": []}],
            "Purchase": [{"Id": "P1", "TxnDate": "2026-09-02", "Line": [],
                          "_Postings": []}],
        }
        analysis = self.analyze(objects)
        self.assertFalse(any(
            item.defect_type == "project_profitability_configuration"
            for item in analysis.findings))
        check = next(item for item in analysis.coverage["checks"]
                     if item["id"] == "16")
        self.assertEqual(check["status"], "clean")

    def test_check_16_does_not_call_unused_jobs_an_inconsistency(self):
        objects = {
            "Account": [],
            "Customer": [{"Id": "JOB1", "DisplayName": "Kitchen", "Job": True}],
            "Invoice": [{"Id": "I1", "TxnDate": "2026-09-01", "Balance": 0,
                         "Line": [{"Amount": 100, "SalesItemLineDetail": {}}]}],
            "Purchase": [{"Id": "P1", "TxnDate": "2026-09-02", "Line": [],
                          "_Postings": []}],
        }
        self.assertFalse(any(
            item.defect_type == "project_profitability_configuration"
            for item in self.analyze(objects).findings))

    def test_check_16_finds_one_sided_job_tracking(self):
        objects = {
            "Account": [],
            "Customer": [{"Id": "JOB1", "DisplayName": "Kitchen", "Job": True}],
            "Invoice": [{"Id": "I1", "TxnDate": "2026-09-01", "Balance": 0,
                         "Line": [{"Amount": 100, "SalesItemLineDetail": {}}]}],
            "Purchase": [{"Id": "P1", "TxnDate": "2026-09-02",
                          "Line": [{"Amount": 50, "AccountBasedExpenseLineDetail": {
                              "CustomerRef": {"value": "JOB1"}}}]}],
        }
        finding = next(item for item in self.analyze(objects).findings
                       if item.defect_type == "project_profitability_configuration")
        self.assertIn("only the cost side", finding.reason)

    def test_check_16_is_clean_when_both_sides_name_jobs(self):
        objects = {
            "Account": [],
            "Customer": [{"Id": "JOB1", "DisplayName": "Kitchen", "Job": True}],
            "Invoice": [{"Id": "I1", "TxnDate": "2026-09-01", "Balance": 0,
                         "Line": [{"Amount": 100, "SalesItemLineDetail": {
                             "CustomerRef": {"value": "JOB1"}}}]}],
            "Purchase": [{"Id": "P1", "TxnDate": "2026-09-02",
                          "Line": [{"Amount": 50, "AccountBasedExpenseLineDetail": {
                              "CustomerRef": {"value": "JOB1"}}}]}],
        }
        analysis = self.analyze(objects)
        self.assertFalse(any(item.defect_type == "project_profitability_configuration"
                             for item in analysis.findings))
        check = next(item for item in analysis.coverage["checks"] if item["id"] == "16")
        self.assertEqual(check["status"], "clean")


class ReconciliationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "recon.db"
        self.conn = service._db()
        self.org = crm.new_id("org")
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                          (self.org, "Recon Co", self.org))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _run(self, company, evidence=None):
        evidence = company.evidence if evidence is None else evidence
        analysis = work_engine.analyze(company.objects, evidence)
        return work_engine.persist_analysis(
            self.conn, organization_id=self.org, engagement_id=None,
            connection_id=None, analysis=analysis, evidence=evidence)

    def test_analysis_always_persists_a_reconciliation_row(self):
        run_id = self._run(generate_company(10))
        rows = self.conn.execute(
            "SELECT * FROM bookkeeping_reconciliations WHERE run_id=?", (run_id,)).fetchall()
        self.assertTrue(rows)
        self.assertIn(rows[0]["status"], {"reconciled", "exception"})

    def test_a_missing_statement_is_recorded_not_omitted(self):
        company = generate_company(11)
        evidence = dict(company.evidence)
        evidence.pop("bank_statement", None)
        run_id = self._run(company, evidence)
        row = self.conn.execute(
            "SELECT * FROM bookkeeping_reconciliations WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "no_source")
        self.assertTrue(row["missing_source"])

    def test_working_papers_carry_the_reconciliation_and_the_check_report(self):
        run_id = self._run(generate_company(12))
        package = work_engine.persistent_working_papers(self.conn, run_id)
        self.assertTrue(package["reconciliations"])
        self.assertEqual(len(package["coverage"]["checks"]), len(work_engine.CHECKS))

    def test_refresh_recomputes_against_the_current_ledger(self):
        company = generate_company(13)
        run_id = self._run(company)
        adapter = SyntheticQBOAdapter(company)
        before = self.conn.execute(
            "SELECT ledger_balance FROM bookkeeping_reconciliations WHERE run_id=?",
            (run_id,)).fetchone()[0]
        records = work_engine.refresh_reconciliation(self.conn, run_id, adapter)
        self.assertTrue(records)
        after = self.conn.execute(
            "SELECT ledger_balance FROM bookkeeping_reconciliations WHERE run_id=?",
            (run_id,)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM bookkeeping_reconciliations WHERE run_id=?",
                              (run_id,)).fetchone()[0],
            len(records))


class SeparationOfDutiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "duties.db"
        self.conn = service._db()
        self.reviewer = auth.create_user(self.conn, "reviewer@example.invalid", "Reviewer",
                                         "local-test-password-only", "reviewer")
        self.releaser = auth.create_user(self.conn, "operator@example.invalid", "Operator",
                                         "local-test-password-only", "operator")
        self.org = crm.new_id("org")
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                          (self.org, "Duties Co", self.org))
        self.conn.commit()
        self.company = generate_company(14)
        self.adapter = SyntheticQBOAdapter(self.company)
        self.run_id = work_engine.persist_analysis(
            self.conn, organization_id=self.org, engagement_id=None, connection_id=None,
            analysis=work_engine.analyze(self.company.objects, self.company.evidence),
            evidence=self.company.evidence)
        self.proposal_id = work_engine.proposal_rows(self.conn, self.run_id)[0]["id"]

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_the_editor_of_a_proposal_cannot_be_its_only_approver(self):
        row = self.conn.execute("SELECT proposed_json FROM bookkeeping_proposals WHERE id=?",
                                (self.proposal_id,)).fetchone()
        edited = work_engine.json.loads(row[0])
        work_engine.record_decision(self.conn, self.proposal_id, "edit", self.reviewer,
                                    "tightened the memo", edited)
        with self.assertRaisesRegex(PermissionError, "another reviewer"):
            work_engine.record_decision(self.conn, self.proposal_id, "approve", self.reviewer)
        # A different reviewer may approve the same version.
        work_engine.record_decision(self.conn, self.proposal_id, "approve", self.releaser)
        self.assertEqual(
            self.conn.execute("SELECT status FROM bookkeeping_proposals WHERE id=?",
                              (self.proposal_id,)).fetchone()[0], "approved")

    def test_release_without_an_approval_record_is_refused(self):
        self.conn.execute("UPDATE bookkeeping_proposals SET status='approved' WHERE id=?",
                          (self.proposal_id,))
        self.conn.commit()
        with self.assertRaisesRegex(PermissionError, "no approval record"):
            work_engine.execute_saved(self.conn, self.proposal_id, self.adapter, self.releaser)

    def test_the_approver_cannot_also_release_by_default(self):
        work_engine.record_decision(self.conn, self.proposal_id, "approve", self.reviewer)
        with self.assertRaisesRegex(PermissionError, "cannot also"):
            work_engine.execute_saved(self.conn, self.proposal_id, self.adapter, self.reviewer)

    def test_a_second_operator_may_release_what_another_approved(self):
        work_engine.record_decision(self.conn, self.proposal_id, "approve", self.reviewer)
        work_engine.execute_saved(self.conn, self.proposal_id, self.adapter, self.releaser)
        row = self.conn.execute(
            "SELECT * FROM bookkeeping_executions WHERE proposal_id=?", (self.proposal_id,)).fetchone()
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["duties_separated"], 1)
        self.assertEqual(row["approved_by"], self.reviewer)
        self.assertEqual(row["executed_by"], self.releaser)
        self.assertIsNone(row["duties_exception_note"])

    def test_the_single_operator_exception_is_recorded_never_silent(self):
        work_engine.record_decision(self.conn, self.proposal_id, "approve", self.reviewer)
        work_engine.execute_saved(self.conn, self.proposal_id, self.adapter, self.reviewer,
                                  allow_single_operator=True,
                                  single_operator_note="Sole operator; sandbox only.")
        row = self.conn.execute(
            "SELECT * FROM bookkeeping_executions WHERE proposal_id=?", (self.proposal_id,)).fetchone()
        self.assertEqual(row["duties_separated"], 0)
        self.assertEqual(row["duties_exception_note"], "Sole operator; sandbox only.")
        package = work_engine.persistent_working_papers(self.conn, self.run_id)
        exception = next(item for item in package["executions"]
                         if item["proposal_id"] == self.proposal_id)
        self.assertEqual(exception["duties_separated"], 0)
        self.assertTrue(exception["duties_exception_note"])


if __name__ == "__main__":
    unittest.main()

class AttachmentEvidenceTests(unittest.TestCase):
    """An attachment is evidence for a transaction, and must say which one."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "evidence.db"
        self.conn = service._db()
        self.org = crm.new_id("org")
        self.conn.execute(
            "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
            (self.org, "Evidence Co", self.org))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _objects(self, attachable_ref):
        purchase = {
            "Id": "P-1", "TxnDate": "2026-02-01", "DocNumber": "E-1",
            "TotalAmt": "500.00", "CurrencyRef": {"value": "CAD"},
            "Line": [{"Id": "1", "Amount": "500.00",
                      "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "A-1"}}}],
        }
        attachment = {"Id": "ATT-1", "FileName": "receipt.pdf", "ContentType": "application/pdf"}
        if attachable_ref is not None:
            attachment["AttachableRef"] = attachable_ref
        return {
            "Account": [{"Id": "A-1", "Name": "Job Materials", "AccountType": "Cost of Goods Sold"}],
            "Purchase": [purchase], "Attachable": [attachment],
        }

    def _persist(self, objects):
        analysis = work_engine.analyze(objects, {}, today=date(2026, 3, 31))
        run_id = work_engine.persist_analysis(
            self.conn, organization_id=self.org, engagement_id=None,
            connection_id=None, analysis=analysis)
        return self.conn.execute(
            "SELECT d.filename,t.provider_type,t.provider_id FROM bookkeeping_documents d "
            "LEFT JOIN bookkeeping_transactions t ON t.id=d.transaction_id WHERE d.run_id=?",
            (run_id,)).fetchone()

    def test_an_attachment_records_the_transaction_it_evidences(self):
        row = self._persist(self._objects([{"EntityRef": {"value": "P-1", "type": "Purchase"}}]))
        self.assertEqual(tuple(row), ("receipt.pdf", "Purchase", "P-1"))

    def test_an_unlinked_attachment_is_still_stored(self):
        """QBO does not guarantee a reference, and losing the file is worse."""
        for label, ref in (("no ref", None), ("empty ref", []),
                           ("ref to an object outside this pull",
                            [{"EntityRef": {"value": "P-99", "type": "Purchase"}}])):
            with self.subTest(case=label):
                self.tearDown()
                self.setUp()
                row = self._persist(self._objects(ref))
                self.assertEqual(row[0], "receipt.pdf")
                self.assertIsNone(row[1])

    def test_a_reference_to_another_type_with_the_same_id_is_not_matched(self):
        """Provider ids repeat across types; only the pair identifies a row."""
        row = self._persist(self._objects([{"EntityRef": {"value": "P-1", "type": "Invoice"}}]))
        self.assertIsNone(row[1])
