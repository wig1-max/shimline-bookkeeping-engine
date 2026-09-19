"""Durable background jobs.

The queue database contains opaque IDs only. Customer names, email addresses,
documents and accounting payloads remain in their existing protected stores.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from huey import SqliteHuey

from . import clients
from .db import configure_connection
from .emailing import send_portal_link, send_submission_notification
from .invoice_extract import extract_document
from .paddle_ocr import read_text as paddle_ocr_text
from .settings import settings

huey = SqliteHuey(
    "shimline",
    filename=str(settings.task_db_path),
    results=False,
    store_none=False,
)


def _connect():
    return configure_connection(sqlite3.connect(settings.db_path))


@huey.task(retries=3, retry_delay=60)
def notify_submission(submission_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT company,contact_name,email FROM submissions WHERE id=?",
            (submission_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    if not send_submission_notification(row[0] or "", row[1] or "", row[2] or "", submission_id):
        raise RuntimeError("submission notification was not delivered")
    return True


@huey.task(retries=3, retry_delay=60)
def send_client_portal_link(client_user_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT email FROM client_users WHERE id=? AND status='active'", (client_user_id,)
        ).fetchone()
        if not row:
            return False
        token = clients.issue_access_link(conn, client_user_id)
        conn.commit()
    finally:
        conn.close()
    if not send_portal_link(row[0], token):
        raise RuntimeError("portal link was not delivered")
    return True


@huey.task(retries=1, retry_delay=120)
def extract_submission_documents(submission_id: str) -> int:
    """Write review-only proposals beside uploads so retention removes both."""
    conn = _connect()
    try:
        row = conn.execute("SELECT files FROM submissions WHERE id=?", (submission_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return 0
    folder = (settings.uploads_dir / submission_id).resolve()
    root = settings.uploads_dir.resolve()
    if root not in folder.parents:
        raise ValueError("Invalid submission storage path")
    extracted = []
    ocr_reader = paddle_ocr_text if settings.invoice_ocr_backend == "paddle" else None
    for filename in filter(None, str(row[0] or "").split(",")):
        path = (folder / Path(filename).name).resolve()
        if path.parent != folder or path.suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg"}:
            continue
        extracted.append(extract_document(path, ocr_reader=ocr_reader))
    if not extracted:
        return 0
    destination = folder / "extraction-proposals.json"
    temporary = folder / ".extraction-proposals.tmp"
    temporary.write_text(json.dumps(extracted, indent=2, default=str), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)
    return len(extracted)
