"""The metrics endpoint must never be readable without its token.

There used to be a loopback exemption here, and behind this deployment it
authorised everyone. Uvicorn binds 127.0.0.1 and Nginx proxies to it without
--proxy-headers, so `request.client.host` is "127.0.0.1" for a genuine local
scrape and for a stranger hitting the public hostname alike.

These tests pin the fix by asserting on the exact condition that was broken: a
request that *claims* to be loopback and carries no token.

Settings are patched per-test rather than set through the environment, because
`Settings()` is constructed once at first import and an env var set in this
module would arrive too late whenever another test module imports `app` first.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SHIMLINE_SECRET_KEY", "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402

TOKEN = "metrics-token-for-tests-only"


def _request(host: str, authorization: str = "") -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request({"type": "http", "method": "GET", "path": "/internal/metrics",
                    "headers": headers, "client": (host, 1), "server": ("test", 80),
                    "scheme": "http", "query_string": b""})


def _enabled():
    """SETTINGS with metrics on and a known token."""
    return service.SETTINGS.model_copy(
        update={"metrics_enabled": True, "metrics_token": SecretStr(TOKEN)})


class MetricsAuthorizationTests(unittest.TestCase):
    def test_a_loopback_address_alone_does_not_authorize(self):
        # The regression. Nginx makes every public request look exactly like this.
        with mock.patch.object(service, "SETTINGS", _enabled()):
            for host in ("127.0.0.1", "::1", "localhost"):
                self.assertFalse(service._metrics_authorized(_request(host)), host)

    def test_a_correct_bearer_token_authorizes_from_anywhere(self):
        with mock.patch.object(service, "SETTINGS", _enabled()):
            self.assertTrue(
                service._metrics_authorized(_request("203.0.113.9", f"Bearer {TOKEN}")))

    def test_loopback_with_the_token_still_works(self):
        with mock.patch.object(service, "SETTINGS", _enabled()):
            self.assertTrue(
                service._metrics_authorized(_request("127.0.0.1", f"Bearer {TOKEN}")))

    def test_a_wrong_or_malformed_token_is_refused(self):
        with mock.patch.object(service, "SETTINGS", _enabled()):
            for header in ("Bearer wrong-token", TOKEN, f"Basic {TOKEN}", "Bearer ", ""):
                self.assertFalse(
                    service._metrics_authorized(_request("127.0.0.1", header)), repr(header))

    def test_an_unset_token_refuses_everyone(self):
        # The closed default: metrics on but no token configured must not become
        # "no authentication required".
        blank = service.SETTINGS.model_copy(
            update={"metrics_enabled": True, "metrics_token": SecretStr("")})
        with mock.patch.object(service, "SETTINGS", blank):
            self.assertFalse(service._metrics_authorized(_request("127.0.0.1")))
            self.assertFalse(service._metrics_authorized(_request("127.0.0.1", "Bearer ")))


class MetricsRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "metrics.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_the_route_is_404_without_a_token(self):
        with mock.patch.object(service, "SETTINGS", _enabled()):
            with TestClient(service.app) as client:
                self.assertEqual(client.get("/internal/metrics").status_code, 404)

    def test_the_route_serves_prometheus_text_with_the_token(self):
        with mock.patch.object(service, "SETTINGS", _enabled()):
            with TestClient(service.app) as client:
                response = client.get("/internal/metrics",
                                      headers={"authorization": f"Bearer {TOKEN}"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/plain", response.headers["content-type"])

    def test_the_route_is_absent_from_the_public_schema(self):
        with TestClient(service.app) as client:
            schema = client.get("/openapi.json")
            if schema.status_code == 200:
                self.assertNotIn("/internal/metrics", schema.json().get("paths", {}))


if __name__ == "__main__":
    unittest.main()
