"""Every browser surface resolves an icon, including ones with no HTML head.

A browser only reads ``<link rel="icon">`` from a document that has a head. For
anything else on an origin -- a PDF, a plain-text response, a bare 404 -- it
falls back to requesting ``/favicon.ico`` at the root. The public site shipped
without one and the sample report showed the browser's generic document icon;
these tests exist so the API origin cannot regress the same way.
"""
import struct
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app as service

TEMPLATES = Path(__file__).parent / "shimline" / "templates"

# The templates that own a <head>. Everything else extends one of them, so if
# these five declare the icon, every rendered page inherits it.
HEAD_TEMPLATES = (
    "base.html",
    "portal_base.html",
    "qbo_base.html",
    "login.html",
    "mfa_challenge.html",
)


class FaviconRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(service.app)

    def test_root_ico_is_served(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/x-icon")
        self.assertTrue(response.content)

    def test_root_svg_is_served(self):
        response = self.client.get("/favicon.svg")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/svg+xml"))
        self.assertIn(b"<svg", response.content)

    def test_ico_is_a_real_multi_resolution_icon(self):
        """A PNG renamed to .ico satisfies some browsers and not others."""
        raw = self.client.get("/favicon.ico").content
        reserved, kind, count = struct.unpack("<HHH", raw[:6])
        self.assertEqual((reserved, kind), (0, 1), "not an ICO container")
        self.assertGreaterEqual(count, 3, "expected tab, bookmark and shortcut sizes")
        sizes = []
        for index in range(count):
            entry = raw[6 + 16 * index:22 + 16 * index]
            width, _, _, _, _, _, size, offset = struct.unpack("<BBBBHHII", entry)
            sizes.append(width or 256)
            self.assertEqual(raw[offset:offset + 8], b"\x89PNG\r\n\x1a\n",
                             "ICO entry is not a PNG payload")
            self.assertGreater(size, 0)
        self.assertIn(16, sizes)

    def test_icons_are_cacheable_but_not_immutable(self):
        # The mark changes rarely; a week is long enough to matter and short
        # enough that a rebrand is not stuck in caches for a year.
        for path in ("/favicon.ico", "/favicon.svg"):
            with self.subTest(path=path):
                self.assertIn("max-age", self.client.get(path).headers["cache-control"])


class FaviconTemplateTests(unittest.TestCase):
    def test_every_head_owning_template_declares_both_icons(self):
        for name in HEAD_TEMPLATES:
            with self.subTest(template=name):
                markup = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertIn('href="/favicon.svg', markup)
                self.assertIn('href="/favicon.ico', markup)

    def test_no_other_template_grows_its_own_head(self):
        """A new template with its own <head> would silently miss the icons."""
        for path in sorted(TEMPLATES.glob("*.html")):
            if path.name in HEAD_TEMPLATES:
                continue
            with self.subTest(template=path.name):
                self.assertNotIn("<head>", path.read_text(encoding="utf-8"),
                                 f"{path.name} owns a <head>; add it to HEAD_TEMPLATES "
                                 "and declare the icons in it")


if __name__ == "__main__":
    unittest.main()
