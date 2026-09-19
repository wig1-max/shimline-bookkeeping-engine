"""Typed settings, safe telemetry and metrics access controls."""
import io
import json
import logging
import unittest
from pathlib import Path
from unittest import mock

from pydantic import SecretStr, ValidationError
from starlette.requests import Request

from shimline.settings import Settings
from shimline.telemetry import get_logger


class SettingsTests(unittest.TestCase):
    def test_invalid_numeric_configuration_fails_at_startup(self):
        with mock.patch.dict("os.environ", {"MAX_UPLOAD_MB": "zero"}, clear=True), \
             self.assertRaises(ValidationError):
            Settings()

    def test_secrets_are_not_exposed_by_repr(self):
        settings = Settings(SMTP_PASS="do-not-print")
        self.assertNotIn("do-not-print", repr(settings))

    def test_paths_are_typed(self):
        settings = Settings(SHIMLINE_DB_PATH="relative.db")
        self.assertEqual(settings.db_path, Path("relative.db"))


class TelemetryTests(unittest.TestCase):
    def test_unknown_fields_and_free_form_event_text_never_reach_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("shimline")
        logger.addHandler(handler)
        try:
            get_logger("qbo").info(
                "customer@example.com paid",
                operation="read",
                email="customer@example.com",
            )
        finally:
            logger.removeHandler(handler)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["event"], "invalid_event_name")
        self.assertEqual(payload["operation"], "read")
        self.assertNotIn("email", payload)
        self.assertNotIn("customer@example.com", stream.getvalue())


class MetricsAccessTests(unittest.TestCase):
    @staticmethod
    def _request(host: str, authorization: str = "") -> Request:
        headers = []
        if authorization:
            headers.append((b"authorization", authorization.encode()))
        return Request({"type": "http", "method": "GET", "path": "/internal/metrics",
                        "headers": headers, "client": (host, 1), "server": ("test", 80),
                        "scheme": "http", "query_string": b""})

    def test_loopback_alone_is_not_enough(self):
        """Corrected: there is no loopback exemption, and there must not be one.

        Uvicorn binds 127.0.0.1 and Nginx proxies to it without --proxy-headers,
        so every public request reaches the application claiming to be loopback.
        This test previously asserted that such a request was authorised, which
        would have made /internal/metrics world-readable the moment metrics were
        switched on in production.
        """
        import app
        configured = app.SETTINGS.model_copy(update={"metrics_token": SecretStr("secret")})
        with mock.patch.object(app, "SETTINGS", configured):
            self.assertFalse(app._metrics_authorized(self._request("127.0.0.1")))
            self.assertFalse(app._metrics_authorized(self._request("::1")))
            # A local scrape sends the token like everyone else.
            self.assertTrue(app._metrics_authorized(
                self._request("127.0.0.1", "Bearer secret")))

    def test_remote_access_needs_the_dedicated_token(self):
        import app
        configured = app.SETTINGS.model_copy(update={"metrics_token": SecretStr("secret")})
        with mock.patch.object(app, "SETTINGS", configured):
            self.assertFalse(app._metrics_authorized(self._request("203.0.113.4")))
            self.assertTrue(app._metrics_authorized(
                self._request("203.0.113.4", "Bearer secret")
            ))


if __name__ == "__main__":
    unittest.main()
