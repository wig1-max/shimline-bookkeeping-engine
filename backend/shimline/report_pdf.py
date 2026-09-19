"""Render a client-facing Cash-Leak Review from one persisted projection.

This module deliberately owns presentation only. ``shimline.reporting`` remains
the sole projection from canonical bookkeeping rows to report figures.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "report.html.j2"


class ReportUnavailable(RuntimeError):
    """The run cannot support a client report without inventing source data."""


class PdfRuntimeUnavailable(RuntimeError):
    """The host does not have the pinned PDF runtime and its native libraries."""


class PdfRenderError(RuntimeError):
    """The PDF runtime was present but failed to render this document."""


def dollars(value) -> str:
    """Format money, or state plainly that no number is available."""
    if value is None:
        return "not available"
    return "${:,.0f}".format(value)


def dollars_sum(values) -> str:
    """Add available values and label the result partial when any are absent."""
    present = [value for value in values if value is not None]
    if not present:
        return "not available"
    total = dollars(sum(present))
    return total if len(present) == len(values) else f"{total} (partial)"


def _positive(value) -> bool:
    if value is None:
        return False
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, ValueError):
        return False


def finding_count(findings: dict) -> int:
    """Count only report findings that the projection actually established."""
    count = sum(_positive(findings.get(field)) for field in (
        "ar_over_90", "unassigned_job_costs_total",
        "duplicate_vendor_charges_total", "unapplied_payments_total",
    ))
    count += int(bool(findings.get("worst_project")))
    count += int(bool(findings.get("vendor_price_variance")))
    return count


def render_html(findings: dict, company: dict, *, report_date: str,
                is_sample: bool = False) -> str:
    """Render the existing report template with escaped, explicit inputs."""
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(("html", "xml")),
        undefined=StrictUndefined,
    )
    environment.filters["dollars"] = dollars
    environment.filters["dollars_sum"] = dollars_sum
    template = environment.get_template(TEMPLATE_NAME)
    normalized_company = {
        "name": company.get("name") or "Client",
        "label": company.get("label") or "",
        "province": company.get("province") or "",
        "software": company.get("software") or "QuickBooks Online",
        "employees": company.get("employees"),
        "subcontractors": company.get("subcontractors"),
    }
    normalized_findings = dict(findings)
    # The historic CSV report contract predates explicit unavailable fields.
    # Treat their absence as an empty list only for that compatibility path;
    # the live projection always supplies the list itself.
    normalized_findings.setdefault("unavailable", [])
    projects = normalized_findings.get("projects") or []
    return template.render(
        company=normalized_company,
        f=normalized_findings,
        report_date=report_date,
        finding_count=finding_count(normalized_findings),
        urgent_project_count=len(normalized_findings.get("low_margin_projects") or []),
        ar_invoice_count=normalized_findings.get("ar_invoice_count", 0),
        project_count=len(projects),
        top_projects=projects[:6],
        is_sample=is_sample,
    )


def render_pdf(html: str, *, renderer: Callable[[str], bytes] | None = None) -> bytes:
    """Render PDF bytes in memory; never create a retained report file."""
    if renderer is not None:
        try:
            output = renderer(html)
        except Exception as exc:  # noqa: BLE001 - normalized at this boundary
            raise PdfRenderError("The PDF renderer failed") from exc
        if not output:
            raise PdfRenderError("The PDF renderer returned an empty document")
        return output
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:
        raise PdfRuntimeUnavailable(
            "PDF rendering is unavailable on this host; install the pinned "
            "WeasyPrint runtime and its native Pango/Harfbuzz libraries."
        ) from exc
    try:
        output = HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf()
    except Exception as exc:  # noqa: BLE001 - renderer errors are not client data
        raise PdfRenderError("The PDF renderer failed") from exc
    if not output:
        raise PdfRenderError("The PDF renderer returned an empty document")
    return output


def report_date(value: str | None, fallback: date) -> str:
    """Format a stored ISO date without locale-dependent leading zeroes."""
    try:
        parsed = date.fromisoformat(value or "")
    except ValueError:
        parsed = fallback
    return parsed.strftime("%B %d, %Y").replace(" 0", " ")
