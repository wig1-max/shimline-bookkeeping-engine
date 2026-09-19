"""The portfolio queue, and the provenance that makes it worth reading.

Two things are being defended here. The ordering, because a queue that puts the
wrong client first costs an accountant the hour the product is selling them.
And the honesty of the verification claims, because telling an accountant that
three engines checked their client's books when only two ran is the single most
tempting lie available in this product.
"""
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import auth, portfolio, provenance, tenancy  # noqa: E402
from shimline.provenance import FAILED, NOT_RUN, PASSED  # noqa: E402


def coverage(*, provider="not_supplied", reconstruction_ok=True,
             statement=None, blocked=(), differences=()):
    checks = [{"id": "01", "status": "clean"},
              {"id": "04",
               "status": "clean" if reconstruction_ok else "blocked",
               "missing_source": None if reconstruction_ok else "derived_ledger_postings"}]
    for identifier, source in blocked:
        checks.append({"id": identifier, "status": "blocked",
                       "missing_source": source})
    agreement = {"status": provider}
    if provider == "agrees":
        agreement["accounts_compared"] = 6
    if provider == "disagrees":
        agreement["differences"] = list(differences) or [
            {"account": "35", "reason": "balances differ"}]
    if provider == "unreadable":
        agreement["reason"] = "a row names no account id"
    return {
        "objects_loaded": {"Invoice": 4, "Purchase": 9},
        "account_ids": ["35", "64", "79"],
        "provider_agreement": agreement,
        "statement_matching": statement if statement is not None else {},
        "checks": checks,
    }


class ProvenanceHonesty(unittest.TestCase):
    """What ran on this client, versus what is true of the software."""

    def test_a_client_whose_reports_were_never_synced_is_not_run_not_passed(self):
        checks = {c.name: c for c in provenance.ledger_checks(coverage()).checks}
        provider = checks["QuickBooks' own trial balance agrees"]
        self.assertEqual(provider.state, NOT_RUN)
        self.assertNotEqual(provider.state, PASSED)
        self.assertIn("have not been synced", provider.detail)

    def test_a_check_that_did_not_run_does_not_make_the_run_untrustworthy(self):
        """A client who never synced has not done anything wrong."""
        result = provenance.ledger_checks(coverage())
        self.assertTrue(result.trustworthy)
        self.assertEqual(result.ran, 1)

    def test_a_disagreement_is_a_failure_and_says_the_gap_is_ours(self):
        result = provenance.ledger_checks(coverage(provider="disagrees"))
        self.assertFalse(result.trustworthy)
        failure = result.failures()[0]
        self.assertIn("gap on Shimline's side", failure.action)

    def test_a_blocked_reconstruction_is_a_failure(self):
        result = provenance.ledger_checks(coverage(reconstruction_ok=False))
        self.assertFalse(result.trustworthy)
        self.assertEqual(
            [c.state for c in result.checks
             if c.name.startswith("Ledger rebuilt")], [FAILED])

    def test_agreement_counts_the_accounts_it_compared(self):
        result = provenance.ledger_checks(coverage(provider="agrees"))
        passed = [c for c in result.checks if c.state == PASSED]
        self.assertTrue(any("All 6 account balances" in c.detail for c in passed))

    def test_matched_statement_lines_pass_and_unmatched_ones_fail(self):
        clean = provenance.ledger_checks(coverage(statement={
            "35": {"status": "matched", "matched": 12, "ambiguous": 0,
                   "unmatched_bank": 0, "unmatched_ledger": 2}}))
        self.assertTrue(clean.trustworthy)

        dirty = provenance.ledger_checks(coverage(statement={
            "35": {"status": "matched", "matched": 10, "ambiguous": 1,
                   "unmatched_bank": 2, "unmatched_ledger": 0}}))
        self.assertFalse(dirty.trustworthy)
        failure = dirty.failures()[0]
        self.assertIn("2 statement line(s)", failure.detail)
        self.assertIn("fit more than one entry", failure.detail)

    def test_an_outstanding_cheque_alone_does_not_fail_the_statement_check(self):
        """A ledger entry that has not cleared is the ordinary state of a
        month-end, and flagging it would fail nearly every client."""
        result = provenance.ledger_checks(coverage(statement={
            "35": {"status": "matched", "matched": 12, "ambiguous": 0,
                   "unmatched_bank": 0, "unmatched_ledger": 5}}))
        self.assertTrue(result.trustworthy)

    def test_the_beancount_claim_is_about_the_software_not_the_client(self):
        """Presenting a CI check as a verification of this client's books would
        be the one thing an accountant would be entitled to be angry about."""
        assurance = provenance.engine_assurance()
        self.assertIn("not over this", assurance.does_not_cover)
        self.assertIn("not that these particular books", assurance.does_not_cover)
        # And it must not be one of the per-client checks.
        names = [c.name for c in provenance.ledger_checks(coverage()).checks]
        self.assertNotIn(assurance.name, names)

    def test_evidence_references_are_rendered_readably(self):
        lines = provenance.evidence_lines(
            '["qbo:1042", "bank_statement", "statement_line:bst_1:4", "odd:thing"]')
        self.assertEqual(lines, [
            "QuickBooks 1042",
            "The bank statement for the period",
            "Bank statement line bst_1:4",
            "odd:thing"])

    def test_an_unknown_evidence_tag_is_shown_rather_than_dropped(self):
        """An unexplained reference is a smaller problem than a missing one."""
        self.assertEqual(provenance.evidence_lines(["brand_new:9"]), ["brand_new:9"])


class QueueCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "portfolio.db"
        self.conn = service._db()
        tenancy.create_firm(self.conn, "firm_a", "Alder & Co")
        self.principal = tenancy.create_firm_user(
            self.conn, "firm_a", email="p@example.invalid", display_name="P",
            password="local-test-password-only", firm_role="principal")
        self.internal = auth.create_user(
            self.conn, "ops@shimline.invalid", "Ops",
            "local-test-password-only", "owner")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def client(self, ident, name, *, firm="firm_a"):
        self.conn.execute(
            "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
            (ident, name, ident))
        if firm:
            tenancy.add_client(self.conn, firm, ident)
        self.conn.commit()
        return ident

    def run_for(self, organization_id, *, run_id=None, cover=None,
                started_at="2026-09-01 10:00:00", period_end="2026-08-31"):
        run_id = run_id or f"run_{organization_id}"
        self.conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
            "period_end,status,coverage_json,started_at) "
            "VALUES(?,?,?,?,'review',?,?)",
            (run_id, organization_id, "2026-08-01", period_end,
             json.dumps(cover if cover is not None else coverage()), started_at))
        self.conn.commit()
        return run_id

    def finding(self, run_id, organization_id, *, ident="fnd", effect="0",
                status="open", with_request=False):
        self.conn.execute(
            "INSERT INTO bookkeeping_findings(id,run_id,organization_id,defect_type,"
            "severity,title,reason,evidence_status,evidence_json,financial_effect,status) "
            "VALUES(?,?,?,'stale_receivable','high','T','r','sufficient','[]',?,?)",
            (ident, run_id, organization_id, effect, status))
        if with_request:
            self.conn.execute(
                "INSERT INTO bookkeeping_evidence_requests(id,finding_id,"
                "requested_item,reason,status) VALUES(?,?,'A receipt','because','open')",
                (f"req_{ident}", ident))
        self.conn.commit()
        return ident

    def proposal(self, run_id, finding_id, *, ident="prp", status="proposed"):
        self.conn.execute(
            "INSERT INTO bookkeeping_proposals(id,run_id,finding_id,action_type,"
            "target_type,target_provider_id,current_json,proposed_json,reason,status) "
            "VALUES(?,?,?,'assign_project','Purchase','1','{}','{}','because',?)",
            (ident, run_id, finding_id, status))
        self.conn.commit()

    def queue(self, user_id, *roles):
        scope = tenancy.scope_for(
            self.conn, {"user_id": user_id, "roles": set(roles) or {"viewer"}})
        return portfolio.build(self.conn, scope)


class WhatTheQueueSays(QueueCase):

    def test_a_client_with_a_proposal_waiting_needs_a_decision(self):
        org = self.client("org_1", "Northlake Roofing")
        run = self.run_for(org)
        self.finding(run, org, effect="1200.00")
        self.proposal(run, "fnd")
        row = self.queue(self.principal).rows[0]
        self.assertEqual(row.state, portfolio.NEEDS_DECISION)
        self.assertEqual(row.decisions_waiting, 1)
        self.assertEqual(row.exposure, Decimal("1200.00"))

    def test_an_approved_proposal_is_no_longer_waiting_on_a_person(self):
        org = self.client("org_1", "Northlake Roofing")
        run = self.run_for(org)
        self.finding(run, org)
        self.proposal(run, "fnd", status="approved")
        self.assertEqual(self.queue(self.principal).rows[0].decisions_waiting, 0)

    def test_an_open_evidence_request_is_waiting_on_the_client(self):
        org = self.client("org_1", "Northlake Roofing")
        run = self.run_for(org)
        self.finding(run, org, with_request=True)
        row = self.queue(self.principal).rows[0]
        self.assertEqual(row.state, portfolio.WAITING_ON_CLIENT)
        self.assertEqual(row.evidence_requests, 1)

    def test_a_failed_verification_is_waiting_on_us_not_on_the_client(self):
        """A reviewer must not chase a client for a document they already sent
        when the gap is on Shimline's side."""
        org = self.client("org_1", "Northlake Roofing")
        self.run_for(org, cover=coverage(provider="disagrees"))
        row = self.queue(self.principal).rows[0]
        self.assertEqual(row.state, portfolio.WAITING_ON_US)

    def test_a_reviewed_client_with_nothing_outstanding_is_clean(self):
        org = self.client("org_1", "Northlake Roofing")
        self.run_for(org, cover=coverage(provider="agrees"))
        row = self.queue(self.principal).rows[0]
        self.assertEqual(row.state, portfolio.CLEAN)
        self.assertTrue(row.trustworthy)

    def test_a_client_never_scanned_says_so_rather_than_looking_clean(self):
        self.client("org_1", "Northlake Roofing")
        row = self.queue(self.principal).rows[0]
        self.assertEqual(row.state, portfolio.NEVER_SCANNED)
        self.assertIsNone(row.run_id)

    def test_only_the_newest_run_counts(self):
        """Two runs would double the work the queue says is waiting."""
        org = self.client("org_1", "Northlake Roofing")
        old = self.run_for(org, run_id="run_old", started_at="2026-07-01 10:00:00")
        self.finding(old, org, ident="fnd_old")
        self.proposal(old, "fnd_old", ident="prp_old")
        self.run_for(org, run_id="run_new", started_at="2026-09-01 10:00:00")
        row = self.queue(self.principal).rows[0]
        self.assertEqual(row.run_id, "run_new")
        self.assertEqual(row.decisions_waiting, 0)

    def test_one_missing_statement_blocking_four_checks_is_one_errand(self):
        org = self.client("org_1", "Northlake Roofing")
        self.run_for(org, cover=coverage(blocked=[
            ("04", "Bank statement covering the review period"),
            ("11", "Bank statement covering the review period"),
            ("E2", "Bank statement covering the review period")]))
        row = self.queue(self.principal).rows[0]
        self.assertEqual(row.checks_blocked, 3)
        self.assertEqual(row.blocked_on, ["Bank statement covering the review period"])

    def test_exposure_is_summed_as_decimal_not_as_float(self):
        """Money never goes through binary floating point in this codebase."""
        org = self.client("org_1", "Northlake Roofing")
        run = self.run_for(org)
        for index in range(3):
            self.finding(run, org, ident=f"fnd_{index}", effect="0.10")
        row = self.queue(self.principal).rows[0]
        self.assertEqual(row.exposure, Decimal("0.30"))
        self.assertIsInstance(row.exposure, Decimal)

    def test_a_negative_effect_still_counts_towards_exposure(self):
        org = self.client("org_1", "Northlake Roofing")
        run = self.run_for(org)
        self.finding(run, org, ident="fnd_a", effect="-500.00")
        self.assertEqual(self.queue(self.principal).rows[0].exposure,
                         Decimal("500.00"))

    def test_a_resolved_finding_stops_counting(self):
        org = self.client("org_1", "Northlake Roofing")
        run = self.run_for(org)
        self.finding(run, org, ident="fnd_a", effect="900.00", status="resolved")
        self.assertEqual(self.queue(self.principal).rows[0].exposure, Decimal("0"))


class TheOrdering(QueueCase):

    def test_decisions_come_before_everything_else(self):
        clean = self.client("org_clean", "Aaa Clean")
        self.run_for(clean, cover=coverage(provider="agrees"))
        waiting = self.client("org_wait", "Bbb Waiting")
        run = self.run_for(waiting)
        self.finding(run, waiting, ident="fnd_w", with_request=True)
        deciding = self.client("org_decide", "Zzz Deciding")
        run2 = self.run_for(deciding, run_id="run_d")
        self.finding(run2, deciding, ident="fnd_d")
        self.proposal(run2, "fnd_d", ident="prp_d")

        order = [row.organization_id for row in self.queue(self.principal).rows]
        self.assertEqual(order[0], "org_decide",
                         "the client with work to do comes first, not the one "
                         "whose name sorts first")
        self.assertEqual(order[-1], "org_clean")

    def test_within_a_state_the_larger_exposure_comes_first(self):
        small = self.client("org_small", "Aaa Small")
        run_s = self.run_for(small, run_id="run_s")
        self.finding(run_s, small, ident="fnd_s", effect="10.00")
        self.proposal(run_s, "fnd_s", ident="prp_s")

        large = self.client("org_large", "Zzz Large")
        run_l = self.run_for(large, run_id="run_l")
        self.finding(run_l, large, ident="fnd_l", effect="90000.00")
        self.proposal(run_l, "fnd_l", ident="prp_l")

        order = [row.organization_id for row in self.queue(self.principal).rows]
        self.assertEqual(order, ["org_large", "org_small"])

    def test_the_counts_add_up_to_the_rows(self):
        for index in range(3):
            self.client(f"org_{index}", f"Client {index}")
        queue = self.queue(self.principal)
        self.assertEqual(sum(queue.counts().values()), len(queue.rows))


class TheQueueIsScoped(QueueCase):

    def test_a_queue_holds_only_the_firms_own_clients(self):
        self.client("org_mine", "Mine")
        tenancy.create_firm(self.conn, "firm_b", "Other")
        self.client("org_theirs", "Theirs", firm="firm_b")
        rows = {row.organization_id for row in self.queue(self.principal).rows}
        self.assertEqual(rows, {"org_mine"})

    def test_a_staff_member_with_no_assignments_gets_an_empty_queue(self):
        self.client("org_mine", "Mine")
        staff = tenancy.create_firm_user(
            self.conn, "firm_a", email="s@example.invalid", display_name="S",
            password="local-test-password-only", firm_role="staff")
        self.conn.commit()
        self.assertEqual(self.queue(staff).rows, [])

    def test_internal_staff_see_every_firms_clients(self):
        self.client("org_mine", "Mine")
        tenancy.create_firm(self.conn, "firm_b", "Other")
        self.client("org_theirs", "Theirs", firm="firm_b")
        rows = {row.organization_id for row in self.queue(self.internal, "owner").rows}
        self.assertEqual(rows, {"org_mine", "org_theirs"})

    def test_a_lost_prospect_is_not_in_the_queue(self):
        self.client("org_mine", "Mine")
        self.conn.execute(
            "UPDATE organizations SET lifecycle_stage='lost' WHERE id='org_mine'")
        self.conn.commit()
        self.assertEqual(self.queue(self.principal).rows, [])


if __name__ == "__main__":
    unittest.main()
