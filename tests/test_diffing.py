"""What a proposal changes, and the guard on approving twenty at once.

An approval is only meaningful if the approver saw the change. Two failure
modes are being defended against, and the second is the expensive one:

- A diff that hides a field. An approval that silently omitted a change would
  be worse than showing no diff at all.
- A batch that approves something other than what was on the screen. Volume is
  what makes an accountant fast, and it is the same volume that would let an
  unread change reach a client's books.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import diffing, work_engine  # noqa: E402
from shimline.diffing import ADDED, CHANGED, REMOVED  # noqa: E402


def labels(current, proposed):
    return {item.label: (item.before, item.after, item.kind)
            for item in diffing.changes(current, proposed)}


class WhatTheDiffSays(unittest.TestCase):

    def test_a_changed_field_shows_both_values(self):
        found = labels({"TxnDate": "2026-08-04"}, {"TxnDate": "2026-08-06"})
        self.assertEqual(found["Date"], ("2026-08-04", "2026-08-06", CHANGED))

    def test_unchanged_fields_are_not_listed(self):
        """A diff that lists everything is a diff nobody reads, which is the
        problem being solved rather than a smaller version of it."""
        found = labels({"TxnDate": "2026-08-04", "TotalAmt": 100,
                        "DocNumber": "INV-1"},
                       {"TxnDate": "2026-08-04", "TotalAmt": 100,
                        "DocNumber": "INV-2"})
        self.assertEqual(list(found), ["Document number"])

    def test_a_field_being_set_reads_as_an_addition(self):
        found = labels({}, {"PrivateNote": "reclassified"})
        self.assertEqual(found["Memo"], ("", "reclassified", ADDED))

    def test_a_field_being_cleared_reads_as_a_removal(self):
        found = labels({"PrivateNote": "old"}, {})
        self.assertEqual(found["Memo"], ("old", "", REMOVED))

    def test_plumbing_is_dropped_from_the_label_but_not_the_path(self):
        """A reviewer cares that the project changed, not that it changed
        inside AccountBasedExpenseLineDetail -- but the path must survive so a
        developer can still find the field."""
        current = {"Line": [{"AccountBasedExpenseLineDetail": {
            "CustomerRef": {"value": "7"}}}]}
        proposed = {"Line": [{"AccountBasedExpenseLineDetail": {
            "CustomerRef": {"value": "9"}}}]}
        change = diffing.changes(current, proposed)[0]
        self.assertEqual(change.label, "Line 1 · Project")
        self.assertIn("AccountBasedExpenseLineDetail", change.path)

    def test_line_numbers_are_shown_the_way_a_person_counts(self):
        current = {"Line": [{"Amount": 1}, {"Amount": 2}]}
        proposed = {"Line": [{"Amount": 1}, {"Amount": 3}]}
        self.assertEqual(diffing.changes(current, proposed)[0].label,
                         "Line 2 · Amount")

    def test_an_unknown_field_is_shown_rather_than_dropped(self):
        """An unexplained change is a much smaller problem than an invisible
        one, and this is the test that keeps it that way."""
        found = labels({"SomeNewIntuitField": "a"}, {"SomeNewIntuitField": "b"})
        self.assertEqual(found["SomeNewIntuitField"], ("a", "b", CHANGED))

    def test_a_value_is_never_reformatted(self):
        """1200 becoming 1200.00 is either a real change or it is not, and
        prettying it here would decide that question invisibly."""
        found = labels({"TotalAmt": 1200}, {"TotalAmt": "1200.00"})
        self.assertEqual(found["Total"], ("1200", "1200.00", CHANGED))

    def test_an_added_line_is_reported_field_by_field(self):
        """A big change should look big rather than becoming one opaque blob."""
        current = {"Line": [{"Amount": 1}]}
        proposed = {"Line": [{"Amount": 1}, {"Amount": 5, "DocNumber": "R-9"}]}
        found = labels(current, proposed)
        self.assertEqual(found["Line 2 · Amount"], ("", "5", ADDED))
        self.assertEqual(found["Line 2 · Document number"], ("", "R-9", ADDED))

    def test_an_added_reference_shows_its_value_not_its_wrapper(self):
        """QuickBooks wraps almost everything as {"value": "..."}. Reporting an
        added CustomerRef whole put the JSON in front of a reviewer where the
        project name was the thing they needed to read."""
        current = {"Line": [{"AccountBasedExpenseLineDetail": {}}]}
        proposed = {"Line": [{"AccountBasedExpenseLineDetail": {
            "CustomerRef": {"value": "P-2 Maple St"}}}]}
        found = diffing.changes(current, proposed)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].label, "Line 1 · Project")
        self.assertEqual(found[0].after, "P-2 Maple St")
        self.assertNotIn("{", found[0].after)

    def test_a_removed_line_is_reported_not_silently_dropped(self):
        current = {"Line": [{"Amount": 1}, {"Amount": 5}]}
        proposed = {"Line": [{"Amount": 1}]}
        found = diffing.changes(current, proposed)
        self.assertEqual(found[0].kind, REMOVED)
        self.assertEqual(found[0].label, "Line 2 · Amount")
        self.assertEqual(found[0].before, "5")

    def test_json_text_is_accepted_as_well_as_dicts(self):
        found = labels('{"TxnDate": "2026-08-04"}', {"TxnDate": "2026-08-06"})
        self.assertEqual(found["Date"][2], CHANGED)

    def test_unparseable_text_is_compared_rather_than_raising(self):
        """A reviewer seeing one opaque change is better served than one
        seeing an error page."""
        found = diffing.changes("not json at all", "also not json")
        self.assertEqual(len(found), 1)

    def test_identical_documents_produce_nothing(self):
        self.assertEqual(diffing.changes({"A": 1}, {"A": 1}), [])


class TheOneLineSummary(unittest.TestCase):

    def test_one_change_is_stated_in_full(self):
        self.assertEqual(
            diffing.summary({"TxnDate": "2026-08-04"}, {"TxnDate": "2026-08-06"}),
            "Date: 2026-08-04 → 2026-08-06")

    def test_several_changes_are_counted(self):
        self.assertEqual(
            diffing.summary({"TxnDate": "a", "DocNumber": "x"},
                            {"TxnDate": "b", "DocNumber": "y"}),
            "2 field changes")

    def test_nothing_changed_says_so(self):
        self.assertEqual(diffing.summary({"A": 1}, {"A": 1}), "No field changes")


class TheFingerprint(unittest.TestCase):

    def test_the_same_change_hashes_the_same(self):
        self.assertEqual(diffing.fingerprint({"A": 1}, {"A": 2}),
                         diffing.fingerprint({"A": 1}, {"A": 2}))

    def test_a_different_change_hashes_differently(self):
        self.assertNotEqual(diffing.fingerprint({"A": 1}, {"A": 2}),
                            diffing.fingerprint({"A": 1}, {"A": 3}))

    def test_moving_a_value_between_fields_changes_the_hash(self):
        """Concatenating the parts without a separator would let two different
        sets of changes collide, which is exactly the case the batch guard
        must not miss."""
        self.assertNotEqual(
            diffing.fingerprint({"AB": "", "C": "x"}, {"AB": "y", "C": "x"}),
            diffing.fingerprint({"A": "", "BC": "x"}, {"A": "y", "BC": "x"}))


class BatchCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "batch.db"
        self.conn = service._db()
        self.conn.execute(
            "INSERT INTO organizations(id,name,normalized_name) "
            "VALUES('org_1','Client','org_1')")
        self.conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
            "period_end,status) VALUES('run_1','org_1','2026-08-01',"
            "'2026-08-31','review')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def proposal(self, ident, current, proposed, *, run_id="run_1"):
        self.conn.execute(
            "INSERT INTO bookkeeping_findings(id,run_id,organization_id,defect_type,"
            "severity,title,reason,evidence_status,evidence_json,status) "
            "VALUES(?,?,'org_1','duplicate_transaction','high','T','r',"
            "'sufficient','[]','open')", (f"fnd_{ident}", run_id))
        self.conn.execute(
            "INSERT INTO bookkeeping_proposals(id,run_id,finding_id,action_type,"
            "target_type,target_provider_id,current_json,proposed_json,reason,status) "
            "VALUES(?,?,?,'assign_project','Purchase','1',?,?,'because','proposed')",
            (ident, run_id, f"fnd_{ident}", json.dumps(current), json.dumps(proposed)))
        self.conn.commit()
        return ident, diffing.fingerprint(current, proposed)


class TheBatchGuard(BatchCase):

    def test_a_batch_whose_proposals_are_unchanged_is_staged(self):
        one = self.proposal("prp_1", {"A": 1}, {"A": 2})
        two = self.proposal("prp_2", {"B": 1}, {"B": 2})
        staged = work_engine.record_batch_decision(
            self.conn, [one, two], "user_1", run_id="run_1")
        self.assertEqual(staged, ["prp_1", "prp_2"])

    def test_one_changed_proposal_refuses_the_whole_batch(self):
        """Partial application would leave the reviewer working out which half
        landed, which is worse than refusing."""
        one = self.proposal("prp_1", {"A": 1}, {"A": 2})
        two = self.proposal("prp_2", {"B": 1}, {"B": 2})
        self.conn.execute(
            "UPDATE bookkeeping_proposals SET proposed_json=? WHERE id='prp_2'",
            (json.dumps({"B": 99}),))
        self.conn.commit()
        with self.assertRaises(work_engine.BatchChanged) as caught:
            work_engine.record_batch_decision(
                self.conn, [one, two], "user_1", run_id="run_1")
        self.assertIn("prp_2", str(caught.exception))
        self.assertIn("nothing was decided", str(caught.exception))

    def test_a_proposal_that_vanished_refuses_the_batch(self):
        one = self.proposal("prp_1", {"A": 1}, {"A": 2})
        with self.assertRaises(work_engine.BatchChanged):
            work_engine.record_batch_decision(
                self.conn, [one, ("prp_gone", "deadbeefdeadbeef")],
                "user_1", run_id="run_1")

    def test_a_proposal_from_another_run_refuses_the_batch(self):
        """The run id comes from the URL and the proposal ids come from the
        form. Checking they agree stops a crafted form reaching a review the
        reviewer never opened."""
        self.conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
            "period_end,status) VALUES('run_2','org_1','2026-07-01',"
            "'2026-07-31','review')")
        self.conn.commit()
        mine = self.proposal("prp_1", {"A": 1}, {"A": 2})
        other = self.proposal("prp_x", {"A": 1}, {"A": 2}, run_id="run_2")
        with self.assertRaises(work_engine.BatchChanged) as caught:
            work_engine.record_batch_decision(
                self.conn, [mine, other], "user_1", run_id="run_1")
        self.assertIn("does not belong to this review", str(caught.exception))

    def test_an_empty_batch_decides_nothing_without_erroring(self):
        self.assertEqual(
            work_engine.record_batch_decision(self.conn, [], "user_1"), [])

    def test_the_fingerprint_survives_a_round_trip_through_the_row_builder(self):
        """The page renders the fingerprint from proposal_rows and the form
        posts it back. If those two computed it differently, every batch would
        be refused and the feature would silently never work."""
        self.proposal("prp_1", {"Line": [{"AccountBasedExpenseLineDetail":
                                          {"CustomerRef": {"value": "7"}}}]},
                      {"Line": [{"AccountBasedExpenseLineDetail":
                                 {"CustomerRef": {"value": "9"}}}]})
        row = work_engine.proposal_rows(self.conn, "run_1")[0]
        staged = work_engine.record_batch_decision(
            self.conn, [(row["id"], row["fingerprint"])], "user_1", run_id="run_1")
        self.assertEqual(staged, ["prp_1"])
        self.assertEqual(row["change_summary"], "Line 1 · Project: 7 → 9")


if __name__ == "__main__":
    unittest.main()
