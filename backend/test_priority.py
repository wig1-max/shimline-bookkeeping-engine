"""The priority model and the domain vocabulary.

Both exist so that adding a state or a new consideration is one edit rather
than five. These tests hold that property, not just today's behaviour.
"""
import unittest
from datetime import timedelta

from shimline import clock, priority, vocabulary


def engagement(**overrides) -> dict:
    now = clock.now()
    base = {
        "id": "eng_test",
        "status": "in_progress",
        "created_at": clock.format_timestamp(now - timedelta(days=1)),
        "updated_at": clock.format_timestamp(now),
        "ready_at": clock.format_timestamp(now - timedelta(days=1)),
        "due_at": clock.format_timestamp(now + timedelta(days=4)),
        "work_total": 8,
        "work_done": 2,
        "client_value_cents": 19900,
    }
    base.update(overrides)
    return base


class VocabularyTests(unittest.TestCase):
    def test_one_definition_serves_codes_choices_and_positions(self):
        engagements = vocabulary.ENGAGEMENT
        self.assertIn("delivered", engagements)
        self.assertNotIn("invented", engagements)
        self.assertEqual(engagements.get("delivered").label, "Delivery")
        self.assertIn(("ready", "Ready"), engagements.choices())
        self.assertLess(engagements.position("ready"), engagements.position("delivered"))

    def test_an_unknown_code_degrades_instead_of_raising(self):
        stage = vocabulary.ENGAGEMENT.get("something_new")
        self.assertEqual(stage.label, "Something new")
        self.assertEqual(vocabulary.ENGAGEMENT.position("something_new"), 0)

    def test_the_progress_path_is_ordered_and_excludes_internal_states(self):
        path = vocabulary.ENGAGEMENT.path()
        codes = [stage.code for stage in path]
        self.assertEqual(codes, ["awaiting_client", "ready", "in_progress",
                                 "internal_review", "delivered"])
        self.assertNotIn("draft", codes)
        self.assertEqual([s.position for s in path], sorted(s.position for s in path))

    def test_terminal_states_leave_the_active_set(self):
        active = {stage.code for stage in vocabulary.ENGAGEMENT.active()}
        self.assertNotIn("closed", active)
        self.assertIn("in_progress", active)

    def test_every_tone_is_one_the_stylesheet_knows(self):
        allowed = {"neutral", "soon", "warn", "good"}
        for name, vocab in vocabulary.ALL.items():
            for stage in vocab:
                self.assertIn(stage.tone, allowed, f"{name}.{stage.code}")


class PriorityTests(unittest.TestCase):
    def test_overdue_work_outranks_everything_else(self):
        late = engagement(due_at=clock.format_timestamp(clock.now() - timedelta(days=2)))
        soon = engagement(due_at=clock.format_timestamp(clock.now() + timedelta(days=1)))
        self.assertGreater(priority.evaluate(late).score, priority.evaluate(soon).score)
        self.assertIn("Past the five-business-day promise", priority.evaluate(late).headline)

    def test_lateness_saturates_rather_than_running_away(self):
        two = priority.evaluate(engagement(
            due_at=clock.format_timestamp(clock.now() - timedelta(days=2)))).score
        thirty = priority.evaluate(engagement(
            due_at=clock.format_timestamp(clock.now() - timedelta(days=30)))).score
        self.assertGreater(thirty, two)
        # A month late must not be fifteen times a two-day slip, or one stale
        # item would permanently own the top of the queue.
        self.assertLess(thirty, two * 3)

    def test_work_awaiting_our_own_signoff_is_promoted(self):
        review = engagement(status="internal_review", work_done=8)
        early = engagement(status="in_progress", work_done=1)
        self.assertGreater(priority.evaluate(review).score, priority.evaluate(early).score)
        self.assertIn("our own sign-off", priority.evaluate(review).headline)

    def test_a_paid_engagement_nobody_started_surfaces(self):
        stalled = engagement(status="awaiting_client", ready_at=None, due_at=None,
                             created_at=clock.format_timestamp(clock.now() - timedelta(days=6)))
        reasons = " ".join(priority.evaluate(stalled).reasons)
        self.assertIn("not started", reasons)
        self.assertIn("SLA clock has not begun", reasons)

    def test_waiting_on_a_client_escalates_only_once_it_goes_stale(self):
        fresh = engagement(status="awaiting_client", due_at=None,
                           updated_at=clock.format_timestamp(clock.now()))
        stale = engagement(status="awaiting_client", due_at=None,
                           updated_at=clock.format_timestamp(clock.now() - timedelta(days=20)))
        self.assertGreater(priority.evaluate(stale).score, priority.evaluate(fresh).score)
        self.assertIn("chase it", priority.evaluate(stale).headline)

    def test_every_ranked_item_can_explain_itself(self):
        items = [
            engagement(id="a", due_at=clock.format_timestamp(clock.now() - timedelta(days=3))),
            engagement(id="b", status="internal_review", work_done=8),
            engagement(id="c", status="awaiting_client", due_at=None),
        ]
        ranked = priority.rank(items)
        self.assertEqual(ranked[0]["id"], "a")
        for item in ranked:
            self.assertTrue(item["priority_reason"], item["id"])
            self.assertIsInstance(item["priority_score"], float)

    def test_ranking_is_stable_for_identical_work(self):
        items = [engagement(id=f"e{i}") for i in range(5)]
        first = [item["id"] for item in priority.rank(list(items))]
        second = [item["id"] for item in priority.rank(list(reversed(items)))]
        self.assertEqual(first, second, "ties must not depend on row order")

    def test_a_new_signal_needs_no_change_to_existing_code(self):
        """The extension point the model exists for."""
        def flagged_by_hand(item, now):
            return priority.Contribution(
                "manual_flag", 500, 1.0 if item.get("flagged") else 0.0,
                "Flagged by an operator." if item.get("flagged") else "")

        original = priority.SIGNALS
        priority.SIGNALS = original + (flagged_by_hand,)
        try:
            ranked = priority.rank([
                engagement(id="ordinary",
                           due_at=clock.format_timestamp(clock.now() - timedelta(days=5))),
                engagement(id="flagged", flagged=True),
            ])
            self.assertEqual(ranked[0]["id"], "flagged")
            self.assertEqual(ranked[0]["priority_reason"], "Flagged by an operator.")
        finally:
            priority.SIGNALS = original

    def test_signals_never_raise_on_missing_data(self):
        sparse = {"id": "eng_sparse", "status": "draft"}
        result = priority.evaluate(sparse)
        self.assertIsInstance(result.score, float)
        self.assertTrue(result.headline)


if __name__ == "__main__":
    unittest.main()
