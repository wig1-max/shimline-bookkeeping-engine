"""Vendored browser dependencies stay local, pinned, and licensed."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class VendoredAssetTests(unittest.TestCase):
    def test_browser_dependencies_are_exactly_pinned(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["devDependencies"], {
            "axe-core": "4.13.0",
            "chart.js": "4.5.1",
            "htmx.org": "4.0.0",
        })

    def test_runtime_assets_and_license_notices_are_committed_locally(self):
        expected = [
            "backend/shimline/static/vendor/htmx-4.0.0.min.js",
            "backend/shimline/static/vendor/chart-4.5.1.umd.js",
            "backend/shimline/static/vendor/LICENSE.htmx.txt",
            "backend/shimline/static/vendor/LICENSE.chartjs.md",
            "backend/test_assets/axe-4.13.0.min.js",
            "backend/test_assets/LICENSE.axe-core.txt",
        ]
        for relative in expected:
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertGreater(path.stat().st_size, 100, relative)


if __name__ == "__main__":
    unittest.main()
