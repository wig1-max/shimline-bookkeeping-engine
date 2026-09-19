"""The first pooled HTTPX/Tenacity provider boundary: QBO report reads."""
import unittest

import httpx
from fastapi import HTTPException

from shimline.qbo_reports import Report, _fetch

REPORT = Report("ProfitAndLoss", "Profit & loss", "test")


class QuickBooksHttpxTests(unittest.TestCase):
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_transient_reads_retry_then_reuse_the_same_client(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) < 3:
                return httpx.Response(503, json={"do_not_log": "company details"})
            return httpx.Response(200, json={"Header": {"ReportName": "ProfitAndLoss"}})

        client = self._client(handler)
        result = _fetch(
            "realm", REPORT, "token", "sandbox", "2026-01-01", "2026-09-01",
            client=client, sleep=lambda _seconds: None,
        )
        self.assertEqual(result["Header"]["ReportName"], "ProfitAndLoss")
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(r.headers["authorization"] == "Bearer token" for r in requests))

    def test_permanent_provider_errors_are_not_retried(self):
        calls = 0

        def handler(_request):
            nonlocal calls
            calls += 1
            return httpx.Response(403, json={"private": "never inspect this"})

        with self.assertRaises(HTTPException) as raised:
            _fetch(
                "realm", REPORT, "token", "sandbox", "2026-01-01", "2026-09-01",
                client=self._client(handler), sleep=lambda _seconds: None,
            )
        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn("private", raised.exception.detail)

    def test_invalid_json_is_named_without_returning_the_body(self):
        client = self._client(lambda _request: httpx.Response(200, content=b"not-json"))
        with self.assertRaises(HTTPException) as raised:
            _fetch(
                "realm", REPORT, "token", "sandbox", "2026-01-01", "2026-09-01",
                client=client, sleep=lambda _seconds: None,
            )
        self.assertIn("invalid Profit & loss report", raised.exception.detail)
        self.assertNotIn("not-json", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
