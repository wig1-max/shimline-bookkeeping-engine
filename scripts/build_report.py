"""Renders the Cash-Leak Review PDF.

Two sources, one renderer:

* ``--from-csv`` (the default) reads ``data/findings.json``, which
  ``scripts/analyze.py`` computes from the synthetic CSVs. This is how the
  published sample report is produced and it is unchanged.
* ``--run-id`` reads the canonical bookkeeping tables for one run and projects
  them through ``shimline.reporting``. No CSV is involved anywhere in that
  path: the numbers come from a QuickBooks pull that was shredded into the
  same tables the reviewer console reads.

``findings.json`` is the contract between the two. A figure the projection
could not compute arrives as ``None`` alongside an entry in ``unavailable``,
and the template prints it as unavailable rather than as a formatted zero.
"""
import argparse
import csv
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"
DEFAULT_OUTPUT = ROOT / "output" / "pdf"
sys.path.insert(0, str(ROOT / "backend"))
from shimline import report_pdf  # noqa: E402


dollars = report_pdf.dollars
dollars_sum = report_pdf.dollars_sum


def sample_source_digest() -> str:
    """Hash every maintained input that determines the published sample."""
    paths = (
        DATA / "findings.json",
        DATA / "company.json",
        DATA / "invoices.csv",
        ROOT / "backend" / "shimline" / "templates" / "report.html.j2",
        ROOT / "backend" / "shimline" / "report_pdf.py",
        Path(__file__).resolve(),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def render(findings: dict, company: dict, *, report_date: str,
           finding_count: int = 6) -> str:
    # Kept as a small compatibility surface for the reporting oracle. The
    # production operator route calls the same module with ``is_sample=False``.
    return report_pdf.render_html(
        findings, company, report_date=report_date, is_sample=True)


def findings_from_csv() -> dict:
    findings = json.loads((DATA / "findings.json").read_text(encoding="utf-8"))
    # The historic findings contract did not retain the invoice count. Derive
    # it from the same source CSV so the sample never prints a fabricated zero.
    with open(DATA / "invoices.csv", newline="", encoding="utf-8") as handle:
        findings["ar_invoice_count"] = sum(
            1 for row in csv.DictReader(handle) if row["status"] == "unpaid"
        )
    return findings


def findings_from_run(db_path: Path, run_id: str, as_of: date) -> dict:
    """Project one persisted QuickBooks pull into the report contract."""
    from shimline import db as shimline_db  # noqa: E402
    from shimline import reporting  # noqa: E402
    import sqlite3

    connection = shimline_db.configure_connection(sqlite3.connect(db_path))
    try:
        return reporting.portfolio(connection, run_id, as_of=as_of)
    finally:
        connection.close()


def company_from_run(db_path: Path, run_id: str) -> dict:
    """Read the client identity associated with a run; never use sample copy."""
    from shimline import db as shimline_db  # noqa: E402
    import sqlite3

    connection = shimline_db.configure_connection(sqlite3.connect(db_path))
    try:
        row = connection.execute(
            "SELECT o.name FROM bookkeeping_runs r "
            "JOIN organizations o ON o.id=r.organization_id WHERE r.id=?", (run_id,)
        ).fetchone()
        if not row:
            raise report_pdf.ReportUnavailable("Bookkeeping run not found")
        return {"name": row[0], "software": "QuickBooks Online"}
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="Render from a persisted bookkeeping run.")
    parser.add_argument("--db", type=Path, help="SQLite database holding that run.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today(),
                        help="Aging date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--report-date", default="August 31, 2026",
                        help="Date printed on the report.")
    parser.add_argument("--out", default="NorthStar_Cash-Leak_Review",
                        help="Output basename.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT,
                        help="Output directory. Defaults to output/pdf/.")
    arguments = parser.parse_args()

    if arguments.run_id:
        if not arguments.db:
            parser.error("--run-id requires --db")
        findings = findings_from_run(arguments.db, arguments.run_id, arguments.as_of)
        company = company_from_run(arguments.db, arguments.run_id)
        is_sample = False
    else:
        findings = findings_from_csv()
        company = json.loads((DATA / "company.json").read_text(encoding="utf-8"))
        is_sample = True

    html_str = report_pdf.render_html(
        findings, company, report_date=arguments.report_date, is_sample=is_sample)

    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    out_html = arguments.output_dir / f"{arguments.out}.html"
    out_html.write_text(html_str, encoding="utf-8")
    print(f"Wrote {out_html}")

    for gap in findings.get("unavailable", []):
        print(f"  unavailable: {gap['field']} ({gap['status']}) -- {gap['reason']}")

    try:
        pdf = report_pdf.render_pdf(html_str)
    except (report_pdf.PdfRuntimeUnavailable, report_pdf.PdfRenderError) as exc:
        raise SystemExit(f"PDF not written: {exc}") from exc
    out_pdf = arguments.output_dir / f"{arguments.out}.pdf"
    out_pdf.write_bytes(pdf)
    print(f"Wrote {out_pdf}")
    if is_sample:
        artifact = {
            "source_sha256": sample_source_digest(),
            "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        }
        manifest = arguments.output_dir / f"{arguments.out}.manifest.json"
        manifest.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {manifest}")


if __name__ == "__main__":
    main()
