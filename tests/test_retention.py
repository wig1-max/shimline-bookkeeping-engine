"""Retention regression tests, including the old-but-recently-closed boundary."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import app as service


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "intake.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def add_submission(self, sid: str, created: datetime, closed: datetime | None):
        conn = service._db()
        conn.execute(
            "INSERT INTO submissions(id,created_at,company,files,closed_at) VALUES(?,?,?,?,?)",
            (sid, created.strftime("%Y-%m-%d %H:%M:%S"), "Retention test", "test.csv",
             closed.strftime("%Y-%m-%d %H:%M:%S") if closed else None),
        )
        conn.commit()
        conn.close()
        folder = service.UPLOADS_DIR / sid
        folder.mkdir()
        (folder / "test.csv").write_text("safe synthetic data", encoding="utf-8")

    def test_old_submission_closed_yesterday_is_kept(self):
        now = datetime.now(timezone.utc)
        self.add_submission("a" * 12, now - timedelta(days=200), now - timedelta(days=1))
        self.assertEqual(service.purge_expired(now), [])
        self.assertTrue((service.UPLOADS_DIR / ("a" * 12) / "test.csv").exists())

    def test_expired_closed_and_abandoned_submissions_are_removed(self):
        now = datetime.now(timezone.utc)
        self.add_submission("b" * 12, now - timedelta(days=200), now - timedelta(days=31))
        self.add_submission("c" * 12, now - timedelta(days=91), None)
        removed = service.purge_expired(now)
        self.assertEqual({item["id"] for item in removed}, {"b" * 12, "c" * 12})
        self.assertFalse((service.UPLOADS_DIR / ("b" * 12)).exists())
        conn = service._db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM purge_log").fetchone()[0], 2)
        conn.close()

    def test_retention_preview_is_complete_and_non_destructive(self):
        now = datetime.now(timezone.utc)
        self.add_submission("d" * 12, now - timedelta(days=200), now - timedelta(days=31))
        due = service.retention_candidates(now)
        self.assertEqual(due, [{"id": "d" * 12, "files": 1, "reason": "closed"}])
        self.assertTrue((service.UPLOADS_DIR / ("d" * 12) / "test.csv").exists())
        conn = service._db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM purge_log").fetchone()[0], 0)
        conn.close()

    def test_a_file_deletion_failure_keeps_the_database_record_for_retry(self):
        now = datetime.now(timezone.utc)
        sid = "e" * 12
        self.add_submission(sid, now - timedelta(days=200), now - timedelta(days=31))
        with mock.patch.object(service.shutil, "rmtree", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                service.purge_expired(now)
        conn = service._db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions WHERE id=?", (sid,)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM purge_log").fetchone()[0], 0)
        conn.close()
        self.assertTrue((service.UPLOADS_DIR / sid / "test.csv").exists())


if __name__ == "__main__":
    unittest.main()
