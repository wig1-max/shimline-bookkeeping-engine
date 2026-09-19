import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app as service


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "intake.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        self.previous = service.SETTINGS.measurement_enabled
        service.SETTINGS.measurement_enabled = True
        self.client = TestClient(service.app)
        self.payload = {
            "id": "a" * 32, "visitor_id": "b" * 32, "session_id": "c" * 32,
            "event": "cta_clicked", "page": "/", "referrer": "search",
            "attribution": {"source": "google", "medium": "cpc", "campaign": "fall"},
            "data": {"cta": "start_review"},
        }

    def tearDown(self):
        service.SETTINGS.measurement_enabled = self.previous
        self.client.close()
        self.temp.cleanup()

    def post(self, payload):
        return self.client.post("/measure", json=payload, headers={
            "Origin": "https://shimline.ca", "User-Agent": "Mozilla/5.0",
        })

    def test_collector_accepts_only_minimized_contract_and_deduplicates(self):
        self.assertEqual(self.post(self.payload).status_code, 204)
        self.assertEqual(self.post(self.payload).status_code, 204)
        conn = service._db()
        row = conn.execute("SELECT * FROM measurement_events").fetchone()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM measurement_events").fetchone()[0], 1)
        self.assertEqual(row["utm_campaign"], "fall")
        self.assertNotIn("email", row.keys())
        self.assertNotIn("source_ip", row.keys())
        conn.close()

    def test_collector_rejects_unknown_fields_and_wrong_origin(self):
        bad = dict(self.payload)
        bad["data"] = {"email": "not-allowed@example.invalid"}
        self.assertEqual(self.post(bad).status_code, 422)
        self.assertEqual(self.client.post("/measure", json=self.payload, headers={
            "Origin": "https://elsewhere.invalid", "User-Agent": "Mozilla/5.0",
        }).status_code, 403)

    def test_withdrawal_erases_the_browser_measurement_record(self):
        self.assertEqual(self.post(self.payload).status_code, 204)
        response = self.client.request("DELETE", "/measure", json={"visitor_id": self.payload["visitor_id"]}, headers={
            "Origin": "https://shimline.ca", "User-Agent": "Mozilla/5.0",
        })
        self.assertEqual(response.status_code, 204)
        conn = service._db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM measurement_events").fetchone()[0], 0)
        conn.close()
