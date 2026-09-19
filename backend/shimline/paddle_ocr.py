"""Opt-in PaddleOCR adapter for scanned invoice proposals.

The large inference stack is imported lazily inside the worker. Web processes
never download models, and extracted text is returned only to the deterministic
proposal parser rather than logs or external services.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

MAX_OCR_LINES = 5_000
MAX_OCR_TEXT_CHARS = 200_000


@lru_cache(maxsize=1)
def _pipeline():
    from paddleocr import PaddleOCR

    return PaddleOCR(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        lang="en",
        enable_mkldnn=False,
    )


def _result_texts(result: Any) -> list[str]:
    payload = getattr(result, "json", result)
    if callable(payload):
        payload = payload()
    if isinstance(payload, dict) and isinstance(payload.get("res"), dict):
        payload = payload["res"]
    if isinstance(payload, dict):
        values = payload.get("rec_texts", [])
    else:
        try:
            values = result["rec_texts"]
        except (KeyError, TypeError):
            values = []
    return [str(value).strip() for value in values if str(value).strip()]


def read_text(path: Path, *, pipeline_factory: Callable[[], Any] = _pipeline) -> str:
    """Return bounded OCR text for the review-only invoice parser."""
    lines: list[str] = []
    for result in pipeline_factory().predict(str(path)):
        lines.extend(_result_texts(result))
        if len(lines) >= MAX_OCR_LINES:
            break
    return "\n".join(lines[:MAX_OCR_LINES])[:MAX_OCR_TEXT_CHARS]
