"""The limit on the paid front door, and the restart that used to clear it."""
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import clock, rate_limit  # noqa: E402

ADDRESS = "203.0.113.7"


class RateLimitCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "limits.db"
        self.conn = service._db()
        self.now = clock.now()

    def tearDown(self):
        self.conn.close()
        try:
            self.temp.cleanup()
        except PermissionError:      # Windows holds the file briefly
            pass

    def spend(self, count, *, bucket=rate_limit.SUBMIT, subject=ADDRESS, at=None):
        return [rate_limit.allow(self.conn, bucket, subject, now=at or self.now)
                for _ in range(count)]


class TheLimitItself(RateLimitCase):

    def test_the_allowance_is_spent_and_then_refused(self):
        self.assertTrue(all(self.spend(rate_limit.DEFAULT_LIMIT)))
        self.assertFalse(rate_limit.allow(
            self.conn, rate_limit.SUBMIT, ADDRESS, now=self.now))

    def test_a_refused_attempt_does_not_extend_its_own_lockout(self):
        """An address that keeps retrying has to be able to recover. If a
        refusal recorded a hit, the window would restart on every retry and the
        lockout would never end."""
        self.spend(rate_limit.DEFAULT_LIMIT)
        for _ in range(5):
            rate_limit.allow(self.conn, rate_limit.SUBMIT, ADDRESS, now=self.now)
        held = self.conn.execute(
            "SELECT COUNT(*) FROM rate_limit_hits").fetchone()[0]
        self.assertEqual(held, rate_limit.DEFAULT_LIMIT)

    def test_the_allowance_returns_when_the_window_passes(self):
        self.spend(rate_limit.DEFAULT_LIMIT)
        later = self.now + timedelta(seconds=rate_limit.DEFAULT_WINDOW_SECONDS + 60)
        self.assertTrue(rate_limit.allow(
            self.conn, rate_limit.SUBMIT, ADDRESS, now=later))

    def test_one_address_filling_a_bucket_does_not_lock_out_another(self):
        self.spend(rate_limit.DEFAULT_LIMIT)
        self.assertTrue(rate_limit.allow(
            self.conn, rate_limit.SUBMIT, "198.51.100.2", now=self.now))

    def test_filling_the_payment_bucket_leaves_submissions_alone(self):
        """Separate buckets, or a burst of payment retries would silently cost
        the customer their document upload."""
        self.spend(rate_limit.DEFAULT_LIMIT, bucket=rate_limit.PAYMENT)
        self.assertFalse(rate_limit.allow(
            self.conn, rate_limit.PAYMENT, ADDRESS, now=self.now))
        self.assertTrue(rate_limit.allow(
            self.conn, rate_limit.SUBMIT, ADDRESS, now=self.now))


class TheReasonItMoved(RateLimitCase):

    def test_the_count_survives_a_restart(self):
        """The whole point. The in-memory limiter was cleared by every deploy,
        and the deploy script restarts the service on every release."""
        self.spend(rate_limit.DEFAULT_LIMIT)
        self.conn.close()

        restarted = service._db()        # a new process would do exactly this
        try:
            self.assertFalse(rate_limit.allow(
                restarted, rate_limit.SUBMIT, ADDRESS, now=self.now))
        finally:
            restarted.close()
        self.conn = service._db()

    def test_the_address_is_never_stored_in_the_clear(self):
        self.spend(1)
        stored = self.conn.execute(
            "SELECT subject_hash FROM rate_limit_hits").fetchone()[0]
        self.assertNotIn(ADDRESS, stored)
        self.assertNotEqual(stored, rate_limit.subject_hash(ADDRESS + "x"))
        self.assertEqual(stored, rate_limit.subject_hash(ADDRESS))

    def test_the_digest_is_keyed_rather_than_a_plain_sha256(self):
        """An unkeyed digest of an IPv4 address is not anonymisation: the space
        is 2^32 and precomputing it is trivial."""
        import hashlib
        plain = hashlib.sha256(ADDRESS.encode()).hexdigest()
        self.assertNotEqual(rate_limit.subject_hash(ADDRESS), plain)


class WhenTheCounterCannotBeRead(RateLimitCase):

    def test_a_broken_limiter_allows_rather_than_refusing_a_paying_customer(self):
        """A limiter is a control on abuse, not a correctness control. Turning a
        storage failure into a refused payment converts a fixable problem into a
        lost sale; the paid paths carry their own authorisation."""
        self.conn.execute("DROP TABLE rate_limit_hits")
        self.conn.commit()
        self.assertTrue(rate_limit.allow(
            self.conn, rate_limit.PAYMENT, ADDRESS, now=self.now))


class TheRowsDoNotAccumulate(RateLimitCase):

    def test_retention_removes_hits_once_their_window_has_passed(self):
        self.spend(3)
        later = self.now + timedelta(seconds=rate_limit.DEFAULT_WINDOW_SECONDS + 60)
        self.assertEqual(rate_limit.prune(self.conn, now=later), 3)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM rate_limit_hits").fetchone()[0], 0)

    def test_retention_leaves_hits_that_are_still_counting(self):
        self.spend(3)
        self.assertEqual(rate_limit.prune(self.conn, now=self.now), 0)

    def test_a_one_shot_address_does_not_leave_a_row_behind_for_good(self):
        """`allow` only clears rows for addresses that come back, so without the
        sweep this table becomes the visitor log the hashing exists to avoid."""
        for index in range(20):
            rate_limit.allow(self.conn, rate_limit.SUBMIT, f"198.51.100.{index}",
                             now=self.now)
        later = self.now + timedelta(seconds=rate_limit.DEFAULT_WINDOW_SECONDS + 60)
        rate_limit.prune(self.conn, now=later)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM rate_limit_hits").fetchone()[0], 0)


class TheRoutesUseIt(RateLimitCase):

    def test_the_payment_route_counts_against_the_payment_bucket(self):
        for _ in range(rate_limit.DEFAULT_LIMIT):
            self.assertFalse(service._rate_limited(ADDRESS, rate_limit.PAYMENT))
        self.assertTrue(service._rate_limited(ADDRESS, rate_limit.PAYMENT))
        self.assertFalse(service._rate_limited(ADDRESS))

    def test_the_submission_route_is_the_default_bucket(self):
        for _ in range(rate_limit.DEFAULT_LIMIT):
            self.assertFalse(service._rate_limited(ADDRESS))
        self.assertTrue(service._rate_limited(ADDRESS))
        bucket = self.conn.execute(
            "SELECT DISTINCT bucket FROM rate_limit_hits").fetchall()
        self.assertEqual([row[0] for row in bucket], [rate_limit.SUBMIT])


if __name__ == "__main__":
    unittest.main()
