"""The lead vocabularies are written in three places; they must agree.

Migration 011 puts fit_tier and outreach_route under CHECK constraints, the
import validates against them before writing, and the offline research tool
validates against them before an operator ever loads a file. Three copies is
one copy per boundary, which is right -- the offline tool deliberately does not
import the backend -- but nothing except this test stops them drifting apart,
and drift here is silent until an import fails against production data.
"""
import ast
import re
import tempfile
import unittest
from pathlib import Path

import app as service
from shimline import crm

ROOT = Path(__file__).resolve().parents[1]


def _check_values(schema: str, column: str) -> tuple[str, ...]:
    """Pull the allowed values out of a column's CHECK (... IN (...)) clause."""
    match = re.search(rf"{column} TEXT[^,]*?CHECK \({column} IN \(([^)]*)\)\)", schema, re.S)
    if not match:
        raise AssertionError(f"no CHECK constraint found for {column}")
    return tuple(ast.literal_eval(value.strip()) for value in match.group(1).split(","))


class LeadVocabularyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "vocab.db"
        self.conn = service._db()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _schema(self, table: str) -> str:
        return self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]

    def test_crm_accepts_exactly_what_the_database_allows(self):
        self.assertEqual(
            set(crm.FIT_TIERS), set(_check_values(self._schema("organizations"), "fit_tier")))
        self.assertEqual(
            set(crm.OUTREACH_ROUTES),
            set(_check_values(self._schema("opportunities"), "outreach_route")))

    def test_the_offline_research_tool_agrees_with_the_database(self):
        source = (ROOT / "scripts" / "lead_ops.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        constants = {
            target.id: ast.literal_eval(node.value)
            for node in module.body if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id in {"ROUTES", "FIT_POINTS"}
        }
        self.assertEqual(constants["ROUTES"], set(crm.OUTREACH_ROUTES))
        self.assertEqual(set(constants["FIT_POINTS"]), set(crm.FIT_TIERS))


if __name__ == "__main__":
    unittest.main()
