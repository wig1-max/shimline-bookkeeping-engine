"""Release identity and schema readiness are observable without secrets."""
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app as service
from shimline import release


class ReleaseHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "health.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_health_identifies_code_and_applied_schema(self):
        response = TestClient(service.app).get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["release_id"], release.MANIFEST["release_id"])
        self.assertEqual(body["schema_migration"], release.source_schema_target())
        self.assertEqual(body["schema_target"], release.source_schema_target())
        self.assertNotIn("secret", " ".join(body).lower())


if __name__ == "__main__":
    unittest.main()


class DocumentationReferenceTests(unittest.TestCase):
    """A document the code names must exist, especially in an error message.

    `DEPLOY.md` was referenced four times -- including in the exception raised
    when SHIMLINE_SECRET_KEY is unset -- while no such file was in the repo.
    An operator hitting that error at the worst possible moment was told to
    read something that was not there.
    """

    ROOT = Path(__file__).resolve().parents[1]

    def test_every_markdown_file_named_in_code_exists(self):
        pattern = re.compile(r"(?<![\w./-])[A-Za-z_][\w-]*(?:/[\w-]+)*\.md(?![\w-])")
        sources = sorted(
            list((self.ROOT / "backend").glob("*.py"))
            + list((self.ROOT / "backend" / "shimline").glob("*.py"))
            + list((self.ROOT / "scripts").glob("*.py"))
            + list((self.ROOT / "scripts").glob("*.sh")))
        self.assertGreater(len(sources), 30, "source sweep found almost nothing")
        for path in sources:
            if path.name.startswith("test_"):
                continue  # test fixtures invent filenames on purpose
            for name in sorted(set(pattern.findall(path.read_text(encoding="utf-8")))):
                with self.subTest(source=path.name, document=name):
                    candidates = [self.ROOT / name, self.ROOT / "docs" / Path(name).name]
                    self.assertTrue(
                        any(candidate.is_file() for candidate in candidates),
                        f"{path.name} points at {name}, which does not exist")
