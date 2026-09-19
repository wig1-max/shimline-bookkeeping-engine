"""The rebuild of `firm_clients`, run against a table that is not empty.

Every other test in this suite gets a database built by applying all migrations
to an empty file, so `firm_clients` is empty when 020 runs and the INSERT ...
SELECT that carries existing rows across copies nothing. That is the branch
that does not matter.

The branch that matters is the one that runs if a firm has already been
onboarded in production, and until this file existed nothing executed it. A
migration whose data-preserving half is never run is a claim, not a migration.

The pre-020 database is built by the real code path -- `app._db()`, which
creates the base tables and then applies migrations -- with the migrations
directory pointed at a copy holding only the files that existed before 020.
Rebuilding the schema by hand here would test a hand-written schema.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import db as db_module  # noqa: E402

BEFORE = "020"


def _all_migrations() -> list[Path]:
    return sorted(db_module.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def _migration(prefix: str) -> Path:
    return next(path for path in _all_migrations() if path.stem[:3] == prefix)


class TheRebuildCarriesExistingFirms(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        earlier = root / "migrations"
        earlier.mkdir()
        for path in _all_migrations():
            if path.stem[:3] < BEFORE:
                shutil.copy(path, earlier / path.name)

        service.DB_PATH = root / "before.db"
        with mock.patch.object(db_module, "MIGRATIONS_DIR", earlier):
            self.conn = service._db()

        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) "
                          "VALUES('org_1','Northlake Roofing','northlakeroofing')")
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) "
                          "VALUES('org_2','Cedar Mechanical','cedarmechanical')")
        self.conn.execute("INSERT INTO firms(id,name,normalized_name) "
                          "VALUES('firm_a','Alder & Co','alderco')")
        self.conn.execute("INSERT INTO firms(id,name,normalized_name) "
                          "VALUES('firm_b','Birchwood LLP','birchwoodllp')")
        self.conn.execute("INSERT INTO firm_clients(firm_id,organization_id,"
                          "created_at) VALUES('firm_a','org_1','2026-03-04')")
        self.conn.execute("INSERT INTO firm_clients(firm_id,organization_id,"
                          "created_at) VALUES('firm_a','org_2','2026-03-05')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        try:
            self.temp.cleanup()
        except PermissionError:      # Windows holds the file briefly
            pass

    def migrate(self):
        self.conn.executescript(
            _migration(BEFORE).read_text(encoding="utf-8"))
        self.conn.commit()

    def test_the_old_constraint_refused_a_second_firm_before_the_rebuild(self):
        """The premise. If this stops failing, 020 is solving nothing."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO firm_clients(firm_id,organization_id)"
                              " VALUES('firm_b','org_1')")

    def test_every_row_survives_the_rebuild_with_its_created_at(self):
        self.migrate()
        rows = self.conn.execute(
            "SELECT firm_id, organization_id, created_at FROM firm_clients "
            "ORDER BY organization_id").fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("firm_a", "org_1", "2026-03-04"),
             ("firm_a", "org_2", "2026-03-05")],
            "who held which books, and since when, has to stay answerable")

    def test_a_second_firm_is_representable_after_the_rebuild(self):
        self.migrate()
        self.conn.execute("INSERT INTO firm_clients(firm_id,organization_id) "
                          "VALUES('firm_b','org_1')")
        self.conn.commit()
        held = self.conn.execute(
            "SELECT COUNT(*) FROM firm_clients WHERE organization_id='org_1'"
        ).fetchone()[0]
        self.assertEqual(held, 2)

    def test_one_firm_still_cannot_hold_one_client_twice(self):
        self.migrate()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO firm_clients(firm_id,organization_id)"
                              " VALUES('firm_a','org_1')")

    def test_the_scratch_table_does_not_outlive_the_migration(self):
        self.migrate()
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("firm_clients", names)
        self.assertNotIn("firm_clients_rebuilt", names)

    def test_the_index_survives_the_drop(self):
        """`DROP TABLE` takes the table's indexes with it, so the rebuild has to
        put this one back or every scope resolution goes to a full scan."""
        self.migrate()
        indexes = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='firm_clients'")}
        self.assertIn("idx_firm_clients_org", indexes)

    def test_foreign_keys_still_point_somewhere_real_after_the_rename(self):
        """A rebuilt table keeps its own references, and nothing referenced it,
        so the check should come back empty."""
        self.migrate()
        self.assertEqual(
            self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_an_existing_engagement_gains_a_null_firm_rather_than_a_guess(self):
        self.conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status) "
            "VALUES('eng_old','org_1','July books','delivered')")
        self.conn.commit()
        self.migrate()
        self.assertIsNone(self.conn.execute(
            "SELECT firm_id FROM engagements WHERE id='eng_old'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
