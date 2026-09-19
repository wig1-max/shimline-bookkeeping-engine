"""Exact static-release and sample-freshness controls."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import publish_site  # noqa: E402


class StaticReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.site = self.root / "site"
        self.shared = self.root / "shared"
        self.fonts = self.root / "fonts"
        self.output = self.root / "output"
        self.site.mkdir()
        self.shared.mkdir()
        self.fonts.mkdir()
        self.output.mkdir()
        self.pdf = self.output / "sample.pdf"
        self.manifest = self.output / "sample.manifest.json"
        self.originals = (
            publish_site.SITE,
            publish_site.SHARED_STATIC,
            publish_site.FONT_DIR,
            publish_site.SAMPLE_PDF,
            publish_site.SAMPLE_MANIFEST,
        )
        publish_site.SITE = self.site
        publish_site.SHARED_STATIC = self.shared
        publish_site.FONT_DIR = self.fonts
        publish_site.SAMPLE_PDF = self.pdf
        publish_site.SAMPLE_MANIFEST = self.manifest

    def tearDown(self):
        (
            publish_site.SITE,
            publish_site.SHARED_STATIC,
            publish_site.FONT_DIR,
            publish_site.SAMPLE_PDF,
            publish_site.SAMPLE_MANIFEST,
        ) = self.originals
        self.temp.cleanup()

    def _valid_sample(self, source_digest="source-digest"):
        content = b"%PDF-1.7\nsynthetic test\n"
        self.pdf.write_bytes(content)
        self.manifest.write_text(json.dumps({
            "source_sha256": source_digest,
            "pdf_sha256": hashlib.sha256(content).hexdigest(),
        }), encoding="utf-8")

    def test_collect_is_an_exact_allowlisted_tree(self):
        (self.site / "landing.html").write_text("landing", encoding="utf-8")
        (self.site / "privacy.html").write_text("privacy", encoding="utf-8")
        (self.site / "landing.template.html").write_text("source", encoding="utf-8")
        (self.site / "notes.md").write_text("private", encoding="utf-8")
        # The icons are published from the shared static directory, beside the
        # fonts, because the API serves the same two files.
        (self.shared / "favicon.svg").write_text("svg", encoding="utf-8")
        (self.shared / "favicon.ico").write_bytes(b"icon")
        (self.fonts / "fonts.css").write_text("css", encoding="utf-8")
        (self.fonts / "font.woff2").write_bytes(b"font")
        self._valid_sample()

        collected = publish_site.collect()
        destinations = {destination for _, destination in collected}
        self.assertEqual(destinations, {
            "index.html", "privacy.html", "favicon.svg", "favicon.ico",
            "sample-cash-leak-review.pdf", "fonts/fonts.css", "fonts/font.woff2",
        })
        sources = {destination: source for source, destination in collected}
        for icon in ("favicon.svg", "favicon.ico"):
            self.assertEqual(sources[icon].parent, self.shared,
                             "the icon must come from the shared static directory, "
                             "not a second copy under site/")

    def test_a_stray_icon_left_in_site_is_refused(self):
        """Two sources for one destination is the drift this guards against."""
        (self.site / "landing.html").write_text("landing", encoding="utf-8")
        (self.shared / "favicon.svg").write_text("shared", encoding="utf-8")
        (self.shared / "favicon.ico").write_bytes(b"icon")
        (self.site / "favicon.svg").write_text("stale duplicate", encoding="utf-8")
        self._valid_sample()
        with self.assertRaisesRegex(RuntimeError, "same destination"):
            publish_site.collect()

    def test_sample_requires_current_source_and_pdf_hashes(self):
        self._valid_sample()
        with mock.patch("build_report.sample_source_digest", return_value="source-digest"):
            publish_site.validate_sample()
            self.pdf.write_bytes(self.pdf.read_bytes() + b"changed")
            with self.assertRaisesRegex(RuntimeError, "PDF hash"):
                publish_site.validate_sample()

    def test_sample_rejects_stale_sources_and_non_pdf(self):
        self._valid_sample("old-source")
        with mock.patch("build_report.sample_source_digest", return_value="new-source"):
            with self.assertRaisesRegex(RuntimeError, "stale"):
                publish_site.validate_sample()
        self._valid_sample()
        self.pdf.write_bytes(b"not a PDF")
        self.manifest.write_text(json.dumps({
            "source_sha256": "source-digest",
            "pdf_sha256": hashlib.sha256(b"not a PDF").hexdigest(),
        }), encoding="utf-8")
        with mock.patch("build_report.sample_source_digest", return_value="source-digest"):
            with self.assertRaisesRegex(RuntimeError, "PDF header"):
                publish_site.validate_sample()


if __name__ == "__main__":
    unittest.main()
