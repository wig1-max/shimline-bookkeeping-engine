"""Deterministic document extraction that always produces a review proposal.

Native PDF text is cheaper and more accurate than OCR, so it is attempted
first. invoice2data templates augment known suppliers. OCR is an injected,
optional fallback for scans; no extracted value is ever posted automatically.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

import pdfplumber
from invoice2data.api import extract_data
from pypdf import PdfReader

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
INVOICE_RE = re.compile(
    r"(?:invoice|inv)\s*(?:number|no\.?|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9-]{2,})",
    re.IGNORECASE,
)
JOB_RE = re.compile(r"(?:job|project)\s*(?:number|no\.?|#)?\s*[:#-]\s*(.+)", re.IGNORECASE)
MONEY_RE = re.compile(r"(?:CAD\s*)?\$?\s*([0-9][0-9,]*\.\d{2})", re.IGNORECASE)
TOTAL_RE = re.compile(
    r"^\s*(?:total\s+due|invoice\s+total|amount\s+due|total)\b\s*:?\s*"
    r"(?:CAD\s*)?\$?\s*([0-9][0-9,]*\.\d{2})",
    re.IGNORECASE | re.MULTILINE,
)


def _clean_amount(value) -> str | None:
    try:
        amount = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    return f"{amount:.2f}"


def parse_invoice_text(text: str) -> dict[str, str | list[str] | None]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    invoice = INVOICE_RE.search(text)
    job = JOB_RE.search(text)
    total = TOTAL_RE.search(text)
    amounts = [_clean_amount(value) for value in MONEY_RE.findall(text)]
    amounts = [value for value in amounts if value is not None]
    if total:
        total_value = _clean_amount(total.group(1))
    elif amounts:
        total_value = max(amounts, key=Decimal)
    else:
        total_value = None
    return {
        "vendor": lines[0][:200] if lines else None,
        "invoice_number": invoice.group(1)[:100] if invoice else None,
        "dates": DATE_RE.findall(text)[:10],
        "job_reference": job.group(1).strip()[:200] if job else None,
        "total": total_value,
    }


def _pypdf_text(path: Path) -> tuple[str, int]:
    reader = PdfReader(path, strict=True)
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages), len(pages)


def _pdfplumber_text(path: Path) -> tuple[str, int]:
    with pdfplumber.open(path) as document:
        pages = [page.extract_text() or "" for page in document.pages]
    return "\n".join(pages), len(pages)


def _template_fields(path: Path) -> dict:
    # Explicitly no AI fallback. Built-in regex templates are deterministic.
    return extract_data(str(path), input_module="pdfplumber", ai_fallback=False) or {}


def extract_document(
    source: str | Path,
    *,
    ocr_reader: Callable[[Path], str] | None = None,
    template_reader: Callable[[Path], dict] = _template_fields,
) -> dict:
    path = Path(source)
    if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("Document must be a PDF, PNG, or JPEG file")
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds the 20 MiB extraction limit")

    method, page_count, text = "none", 1, ""
    template = {}
    if path.suffix.lower() == ".pdf":
        try:
            text, page_count = _pypdf_text(path)
            method = "pypdf"
        except Exception:
            text = ""
        if len(text.strip()) < 40:
            try:
                text, page_count = _pdfplumber_text(path)
                method = "pdfplumber"
            except Exception:
                text = ""
        try:
            template = template_reader(path)
        except Exception:
            template = {}
    if len(text.strip()) < 40 and ocr_reader is not None:
        text = ocr_reader(path)
        method = "ocr"

    fields = parse_invoice_text(text)
    template_map = {
        "vendor": template.get("issuer"),
        "invoice_number": template.get("invoice_number"),
        "job_reference": template.get("job_reference"),
        "total": _clean_amount(template.get("amount")) if template.get("amount") is not None else None,
    }
    for key, value in template_map.items():
        if value not in (None, "", []):
            fields[key] = str(value)[:200]
    template_date = template.get("date")
    if isinstance(template_date, (date, datetime)):
        fields["dates"] = [template_date.date().isoformat() if isinstance(template_date, datetime)
                           else template_date.isoformat()]

    present = sum(value not in (None, "", []) for value in fields.values())
    return {
        "source_file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "extraction_method": method,
        "template_name": template.get("template_name"),
        "page_count": page_count,
        "fields": fields,
        "field_count": present,
        "review_required": True,
        "authority": "proposal_only",
    }
