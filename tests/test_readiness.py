"""What a client still needs, said before the scan rather than after it.

The thing being defended is honesty about urgency. Every step nobody can see is
a reason a client does not get onboarded -- but a list that calls everything
blocking is a list nobody reads twice, and that is worse than no list.
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import readiness, tax_rates  # noqa: E402

ORG = "org_1"


class ReadinessCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "ready.db"
        self.conn = service._db()
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) "
                          "VALUES(?,?,?)", (ORG, "Northlake Roofing", ORG))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def check(self):
        return readiness.for_client(self.conn, ORG)

    def step(self, key):
        return next(item for item in self.check().steps if item.key == key)

    def connect(self, status="active"):
        self.conn.execute(
            "INSERT INTO connections(id,organization_id,realm_id_enc,"
            "realm_id_hash,environment,status) "
            "VALUES('con_1',?,'x','h','sandbox',?)", (ORG, status))
        self.conn.commit()

    def probe(self, *, status="ok", ledger_complete=1, assumptions_failed=0):
        self.conn.execute(
            "INSERT INTO connection_probes(id,connection_id,organization_id,"
            "status,ledger_complete,assumptions_failed,payload) "
            "VALUES('prb_1','con_1',?,?,?,?,'x')",
            (ORG, status, ledger_complete, assumptions_failed))
        self.conn.commit()

    def statement(self, *, mapped=True):
        self.conn.execute(
            "INSERT INTO bank_statements(id,organization_id,bank_account_id,"
            "currency,qbo_account_id,period_start,period_end,closing_balance,"
            "source_filename,source_format,content_sha256,line_count) "
            "VALUES('bst_1',?,'8842','CAD',?,'2026-08-01','2026-08-31','-1.00',"
            "'a.ofx','ofx','h',1)", (ORG, "100" if mapped else None))
        self.conn.commit()

    def project(self, provider_id="P1", name="Kitchen", active=1):
        self.conn.execute(
            "INSERT INTO bookkeeping_projects(id,organization_id,provider,"
            "provider_id,name,active) VALUES(?,?,'quickbooks',?,?,?)",
            (f"prj_{provider_id}", ORG, provider_id, name, active))
        self.conn.commit()

    def recorded_line(self, *, project="P1", line_id="bln_1"):
        """One persisted transaction line, optionally naming a project.

        Usage is the whole point of the project step, and usage cannot be read
        from the project table -- it is a fact about transaction lines.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO bookkeeping_runs(id,organization_id,"
            "period_start,period_end,status) VALUES('run_ln',?,'2026-08-01',"
            "'2026-08-31','review')", (ORG,))
        self.conn.execute(
            "INSERT OR IGNORE INTO bookkeeping_transactions(id,run_id,"
            "organization_id,provider,provider_type,provider_id,"
            "transaction_date,total_amount,source_hash) VALUES('btx_1',"
            "'run_ln',?,'quickbooks','Invoice','I1','2026-08-15','100','h')",
            (ORG,))
        self.conn.execute(
            "INSERT INTO bookkeeping_transaction_lines(id,transaction_id,"
            "project_id,amount) VALUES(?,'btx_1',?,'100')",
            (line_id, f"prj_{project}" if project else None))
        self.conn.commit()


class TheFirstRun(ReadinessCase):

    def test_a_brand_new_client_has_everything_outstanding(self):
        result = self.check()
        self.assertEqual(result.done, 0,
                         [step.key for step in result.steps if step.done])
        self.assertFalse(result.ready)
        self.assertEqual(len(result.outstanding()), result.total)

    def test_the_next_step_is_the_connection(self):
        """The order is real: nothing else can be done before the client
        approves access."""
        self.assertEqual(self.check().next_step().key, readiness.CONNECT)

    def test_only_the_connection_stops_a_scan_producing_anything(self):
        """Calling everything blocking would be false, and the second time an
        operator noticed it was false they would stop reading the list."""
        result = self.check()
        blocking = [step.key for step in result.steps if step.blocking]
        self.assertEqual(blocking, [readiness.CONNECT])
        self.assertFalse(result.can_scan)

    def test_connecting_lets_a_scan_run_even_with_everything_else_missing(self):
        self.connect()
        result = self.check()
        self.assertTrue(result.can_scan)
        self.assertFalse(result.ready, "a scan that runs is not a scan that is "
                                       "complete")

    def test_an_inactive_connection_does_not_count(self):
        self.connect(status="revoked")
        self.assertFalse(self.step(readiness.CONNECT).done)


class EachStepSaysWhatIsLost(ReadinessCase):

    def test_project_tracking_is_a_readiness_choice_not_a_recurring_defect(self):
        step = self.step(readiness.PROJECTS)
        self.assertFalse(step.done)
        self.assertFalse(step.blocking)
        self.assertIn("deliberate bookkeeping choice", step.detail)

    def test_a_project_nothing_uses_is_not_a_configured_client(self):
        """The hole check 16 left behind.

        Check 16 stopped reporting unused jobs so it could report genuine
        one-sided tagging. If this step then ticked on mere existence, a client
        with jobs no transaction ever names would read as fully configured while
        their project profitability came back empty -- and nothing anywhere
        would say so.
        """
        self.project()
        self.recorded_line(project=None)
        step = self.step(readiness.PROJECTS)
        self.assertFalse(step.done)
        self.assertIn("not one of the 1 recorded transaction line(s)",
                      step.detail)
        self.assertIn("profitability will be empty", step.detail)

    def test_projects_are_not_judged_unused_before_anything_is_read(self):
        """Same rule the rate step keeps: do not judge before reading, and do
        not tick the step for not having read either."""
        self.project()
        step = self.step(readiness.PROJECTS)
        self.assertFalse(step.done)
        self.assertIn("no scan has recorded transaction lines yet",
                      step.detail.casefold())

    def test_a_project_a_transaction_actually_names_clears_the_step(self):
        self.project()
        self.recorded_line()
        step = self.step(readiness.PROJECTS)
        self.assertTrue(step.done)
        self.assertIn("1 named by recorded transaction lines", step.detail)

    def test_missing_reports_name_the_verification_that_will_not_run(self):
        detail = self.step(readiness.REPORTS).detail
        self.assertIn("third verification", detail)
        self.assertIn("checked twice", detail,
                      "it must say what still happens, not only what does not")

    def test_a_synced_trial_balance_completes_the_step(self):
        self.conn.execute(
            "INSERT INTO sync_runs(id,connection_id,organization_id,period_start,"
            "period_end,reports_requested) VALUES('syn_1',NULL,?,'2026-01-01',"
            "'2026-08-31',1)", (ORG,))
        self.conn.execute(
            "INSERT INTO source_snapshots(id,organization_id,sync_run_id,"
            "report_name,period_start,period_end,payload_enc,byte_size) "
            "VALUES('snp_1',?,'syn_1','TrialBalance','2026-01-01','2026-08-31',"
            "'x',1)", (ORG,))
        self.conn.commit()
        self.assertTrue(self.step(readiness.REPORTS).done)

    def test_an_unmapped_statement_is_held_but_not_done(self):
        """The trap this catches: an operator uploads a statement, sees it
        listed, and cannot understand why reconciliation still says no source."""
        self.statement(mapped=False)
        self.assertTrue(self.step(readiness.STATEMENT).done)
        mapping = self.step(readiness.STATEMENT_MAPPED)
        self.assertFalse(mapping.done)
        self.assertIn("cannot prove anything", mapping.detail)

    def test_a_mapped_statement_completes_both_steps(self):
        self.statement(mapped=True)
        self.assertTrue(self.step(readiness.STATEMENT).done)
        self.assertTrue(self.step(readiness.STATEMENT_MAPPED).done)

    def test_no_statement_means_nothing_to_map_rather_than_a_failure(self):
        self.assertEqual(self.step(readiness.STATEMENT_MAPPED).detail,
                         "Nothing to map yet.")

    def test_a_missing_filing_frequency_says_why_nothing_is_defaulted(self):
        detail = self.step(readiness.FILING).detail
        self.assertIn("a period the client does not file", detail)

    def test_a_recorded_frequency_says_which_one(self):
        from shimline import filing_periods
        filing_periods.record_arrangement(
            self.conn, ORG, frequency="quarterly", year_end_month=12,
            year_end_day=31,
            calculation_method=filing_periods.REGULAR_METHOD)
        self.conn.commit()
        self.assertEqual(
            self.step(readiness.FILING).detail,
            "Files quarterly using the regular method.")

    def test_a_quick_method_arrangement_is_recorded_but_not_called_ready(self):
        from shimline import filing_periods
        filing_periods.record_arrangement(
            self.conn, ORG, frequency="quarterly", year_end_month=12,
            year_end_day=31,
            calculation_method=filing_periods.QUICK_METHOD)
        self.conn.commit()
        step = self.step(readiness.FILING)
        self.assertFalse(step.done)
        self.assertIn("does not implement", step.detail)

    def test_unclassified_rates_are_counted_not_merely_flagged(self):
        tax_rates.persist(self.conn, ORG, {"TaxRate": [
            {"Id": "A", "Name": "Standard"}, {"Id": "B", "Name": "Tax"}]})
        self.conn.commit()
        step = self.step(readiness.RATES)
        self.assertFalse(step.done)
        self.assertIn("2 rate(s)", step.detail)

    def test_rates_are_not_green_before_anything_has_read_them(self):
        """A checklist with a step already ticked on day one is a checklist an
        operator stops trusting."""
        step = self.step(readiness.RATES)
        self.assertFalse(step.done)
        self.assertIn("has read this client's rates yet", step.detail)

    def test_a_client_genuinely_without_rates_reaches_done_once_connected(self):
        """Not every contractor is registered for GST. They must be able to
        finish the list rather than carrying a step they can never satisfy."""
        self.connect()
        step = self.step(readiness.RATES)
        self.assertTrue(step.done)
        self.assertIn("no sales tax rates", step.detail)

    def test_every_outstanding_step_offers_somewhere_to_go(self):
        """A step that says what is wrong and not where to fix it is a
        complaint, not an instruction."""
        for step in self.check().outstanding():
            with self.subTest(step=step.key):
                self.assertTrue(step.action_label, step.key)
                self.assertTrue(step.action_path.startswith("/admin"), step.key)


class TheFileReadOnceStep(ReadinessCase):
    """The probe runs itself, so this step says whether it has, not who must act."""

    def test_a_brand_new_client_is_told_the_probe_needs_nobody(self):
        step = self.step(readiness.FIRST_CONTACT)
        self.assertFalse(step.done)
        self.assertFalse(step.blocking)
        self.assertIn("needs no one", step.detail)

    def test_a_connected_client_is_told_it_runs_on_the_timer(self):
        self.connect()
        self.assertIn("daily timer", self.step(readiness.FIRST_CONTACT).detail)

    def test_a_failed_pull_is_not_a_done_step(self):
        self.connect()
        self.probe(status="failed", ledger_complete=0)
        step = self.step(readiness.FIRST_CONTACT)
        self.assertFalse(step.done)
        self.assertIn("did not finish", step.detail)

    def test_an_assumption_that_did_not_hold_is_counted_in_the_detail(self):
        """Done, because the file *was* read -- and the detail has to say that
        something in it contradicted the documentation, or the step reads as
        clean when it is the opposite."""
        self.connect()
        self.probe(assumptions_failed=3)
        step = self.step(readiness.FIRST_CONTACT)
        self.assertTrue(step.done)
        self.assertIn("3 documented assumption(s) did not hold", step.detail)

    def test_a_clean_read_says_so_without_overclaiming(self):
        self.connect()
        self.probe()
        step = self.step(readiness.FIRST_CONTACT)
        self.assertTrue(step.done)
        self.assertIn("every documented assumption held", step.detail)

    def test_a_read_that_produced_no_ledger_does_not_read_as_clean(self):
        self.connect()
        self.probe(ledger_complete=0)
        self.assertIn("no complete ledger",
                      self.step(readiness.FIRST_CONTACT).detail)


class AFullySetUpClient(ReadinessCase):

    def test_every_step_can_be_satisfied(self):
        from shimline import filing_periods
        self.connect()
        self.statement(mapped=True)
        self.probe()
        self.conn.execute(
            "INSERT INTO sync_runs(id,connection_id,organization_id,period_start,"
            "period_end,reports_requested) VALUES('syn_1',NULL,?,'2026-01-01',"
            "'2026-08-31',1)", (ORG,))
        self.conn.execute(
            "INSERT INTO source_snapshots(id,organization_id,sync_run_id,"
            "report_name,period_start,period_end,payload_enc,byte_size) "
            "VALUES('snp_1',?,'syn_1','TrialBalance','2026-01-01','2026-08-31',"
            "'x',1)", (ORG,))
        filing_periods.record_arrangement(
            self.conn, ORG, frequency="quarterly", year_end_month=12,
            year_end_day=31,
            calculation_method=filing_periods.REGULAR_METHOD)
        self.conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
            "period_end,status) VALUES('run_1',?,'2026-08-01','2026-08-31',"
            "'review')", (ORG,))
        self.project()
        self.recorded_line()

        result = self.check()
        self.assertTrue(result.ready, [s.key for s in result.outstanding()])
        self.assertIsNone(result.next_step())
        self.assertEqual(result.done, result.total)


if __name__ == "__main__":
    unittest.main()
