import { copyFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

const assets = [
  ["node_modules/htmx.org/dist/htmx.min.js", "backend/shimline/static/vendor/htmx-4.0.0.min.js"],
  ["node_modules/chart.js/dist/chart.umd.js", "backend/shimline/static/vendor/chart-4.5.1.umd.js"],
  ["node_modules/axe-core/axe.min.js", "backend/test_assets/axe-4.13.0.min.js"],
  ["node_modules/htmx.org/LICENSE", "backend/shimline/static/vendor/LICENSE.htmx.txt"],
  ["node_modules/chart.js/LICENSE.md", "backend/shimline/static/vendor/LICENSE.chartjs.md"],
  ["node_modules/axe-core/LICENSE", "backend/test_assets/LICENSE.axe-core.txt"]
];

for (const [source, destination] of assets) {
  mkdirSync(dirname(destination), { recursive: true });
  copyFileSync(source, destination);
}
