"""Pull the reports a Cash-Leak Review runs on, straight from QuickBooks.

This is what makes "connect" better than "upload" rather than merely different:
a connected client exports nothing, and the evidence arrives in a known shape
instead of whatever a spreadsheet happened to be saved as.

Reports are declared in `REPORTS`, one entry each. Adding one — a general
ledger, a sales-tax summary, payroll — is a single `Report` appended to that
tuple; the puller, the storage, the operator screen and the retention rule all
pick it up without changes.

Read-only throughout. Nothing here can write to a customer's books, and the
scope Shimline requests would not permit it if it tried.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

import httpx
from fastapi import HTTPException
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import clock, crm, crypto, quickbooks
from .telemetry import get_logger

API_BASE = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
MINOR_VERSION = "75"
REQUEST_TIMEOUT = 45

# A pull is a handful of requests; Intuit throttles per realm, so they go one
# at a time with a modest ceiling rather than in parallel.
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_READ_ATTEMPTS = 3

_HTTP = httpx.Client(
    timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10),
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    follow_redirects=False,
    headers={"User-Agent": "Shimline/1.0"},
)
_LOG = get_logger("qbo_reports")


class _TransientRead(RuntimeError):
    def __init__(self, status_code: int | None = None):
        self.status_code = status_code
        super().__init__("transient QuickBooks read failure")


def _period(months: int = 12) -> tuple[str, str]:
    """Trailing whole months up to today, on the business calendar."""
    end = clock.business_date(clock.now())
    start = (end.replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    return start.isoformat(), end.isoformat()


@dataclass(frozen=True)
class Report:
    name: str            # Intuit's report identifier, used in the URL
    label: str           # what an operator sees
    why: str             # which part of the review this feeds
    params: Callable[[str, str], dict] = lambda start, end: {
        "start_date": start, "end_date": end}


def _summary_params(start: str, end: str) -> dict:
    return {"start_date": start, "end_date": end, "accounting_method": "Accrual"}


def _asof_params(start: str, end: str) -> dict:
    # Balance-sheet style reports take a point in time, not a range.
    return {"as_of": end, "accounting_method": "Accrual"}


REPORTS: tuple[Report, ...] = (
    Report("ProfitAndLoss", "Profit & loss", "Revenue, cost of sales and where margin actually lands.",
           _summary_params),
    Report("BalanceSheet", "Balance sheet", "Cash, receivables, payables and debt at a point in time.",
           _asof_params),
    Report("AgedReceivables", "A/R ageing", "Which invoices are overdue, and by how long.",
           _asof_params),
    Report("AgedPayables", "A/P ageing", "What is owed out, and when it falls due.",
           _asof_params),
    Report("CustomerIncome", "Income by customer", "Which jobs and customers earn, and which do not.",
           _summary_params),
    Report("ProfitAndLossDetail", "Profit & loss detail",
           "Transaction-level costs, for spotting unbilled materials.", _summary_params),
    # Not for a human to read. This is the third independent implementation of
    # the client's arithmetic: Shimline reconstructs postings from documents,
    # Beancount recomputes them from scratch, and this is QuickBooks' own answer
    # for the same ledger. `postings.compare_to_provider` requires all three to
    # agree, account by account, before a set of books is trusted. It is also
    # the authority the client's accountant will be looking at anyway.
    Report("TrialBalance", "Trial balance",
           "QuickBooks' own account balances, used to prove our reconstruction "
           "of them is complete.", _summary_params),
)

REPORTS_BY_NAME = {report.name: report for report in REPORTS}


# ------------------------------------------------------------------ intuit --

def _fetch(realm_id: str, report: Report, access_token: str, environment: str,
           start: str, end: str, *, client: httpx.Client | None = None,
           sleep: Callable[[float], None] = time.sleep) -> dict:
    query = urllib.parse.urlencode({**report.params(start, end), "minorversion": MINOR_VERSION})
    url = f"{API_BASE[environment]}/v3/company/{realm_id}/reports/{report.name}?{query}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    transport = client or _HTTP

    def request_once() -> bytes:
        try:
            with transport.stream("GET", url, headers=headers) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise _TransientRead(response.status_code)
                if response.status_code >= 400:
                    # Never inspect or log the body: it may carry company data.
                    raise HTTPException(
                        502,
                        f"QuickBooks refused the {report.label} report "
                        f"(HTTP {response.status_code})",
                    )
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_PAYLOAD_BYTES:
                        raise HTTPException(
                            502, f"The {report.label} report was larger than we accept"
                        )
                return bytes(chunks)
        except (HTTPException, _TransientRead):
            raise
        except httpx.TransportError as exc:
            raise _TransientRead() from exc

    try:
        for attempt in Retrying(
            stop=stop_after_attempt(MAX_READ_ATTEMPTS),
            wait=wait_exponential(multiplier=0.5, max=4),
            retry=retry_if_exception_type(_TransientRead),
            sleep=sleep,
            reraise=True,
        ):
            with attempt:
                raw = request_once()
    except _TransientRead as exc:
        _LOG.warning(
            "provider.read_failed",
            provider="quickbooks",
            operation="report",
            object_type=report.name,
            status_code=exc.status_code,
            outcome="exhausted",
        )
        raise HTTPException(
            502, f"Could not reach QuickBooks for the {report.label} report"
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, f"QuickBooks returned an invalid {report.label} report") from exc


# ------------------------------------------------------------------- pull --

def pull(conn, *, connection_id: str, engagement_id: str | None = None,
         reports: tuple[Report, ...] = REPORTS, months: int = 12,
         requested_by_user_id: str | None = None,
         requested_by_client_id: str | None = None) -> dict:
    """Fetch every report and store it as an encrypted snapshot.

    One failing report does not abandon the rest: the run is marked `partial`
    and says which failed, because five reports out of six is still most of a
    review, and an operator can see exactly what is missing.
    """
    row = conn.execute(
        "SELECT organization_id,realm_id_enc,environment,status FROM connections WHERE id=?",
        (connection_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "No such QuickBooks connection")
    if row[3] != "active":
        raise quickbooks.ReconnectRequired("This QuickBooks connection is not active")

    organization_id, environment = row[0], row[2]
    realm_id = quickbooks.decrypt_token(row[1])
    start, end = _period(months)

    run_id = crm.new_id("syn")
    conn.execute(
        "INSERT INTO sync_runs(id,connection_id,organization_id,engagement_id,period_start,"
        "period_end,reports_requested,requested_by_user_id,requested_by_client_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (run_id, connection_id, organization_id, engagement_id, start, end,
         len(reports), requested_by_user_id, requested_by_client_id),
    )
    conn.commit()

    access_token = quickbooks.ensure_access_token(conn, connection_id)
    stored, failures = 0, []
    for report in reports:
        try:
            payload = _fetch(realm_id, report, access_token, environment, start, end)
        except HTTPException as exc:
            failures.append(f"{report.label}: {exc.detail}")
            continue
        body = json.dumps(payload, separators=(",", ":"))
        conn.execute(
            "INSERT OR REPLACE INTO source_snapshots(id,sync_run_id,organization_id,engagement_id,"
            "report_name,period_start,period_end,byte_size,payload_enc) VALUES(?,?,?,?,?,?,?,?,?)",
            (crm.new_id("snp"), run_id, organization_id, engagement_id, report.name,
             start, end, len(body), crypto.encrypt(body, crypto.QUICKBOOKS_REPORTS)),
        )
        stored += 1

    status = "complete" if stored == len(reports) else ("partial" if stored else "failed")
    detail = "; ".join(failures)[:1000] or None
    conn.execute(
        "UPDATE sync_runs SET status=?,reports_stored=?,detail=?,finished_at=CURRENT_TIMESTAMP "
        "WHERE id=?", (status, stored, detail, run_id),
    )
    conn.execute(
        "INSERT INTO activities(id,organization_id,engagement_id,actor_user_id,kind,body) "
        "VALUES(?,?,?,?,'system',?)",
        (crm.new_id("act"), organization_id, engagement_id, requested_by_user_id,
         f"Pulled {stored} of {len(reports)} QuickBooks reports for {start} to {end}"),
    )
    conn.execute(
        "INSERT INTO audit_events(id,actor_user_id,action,entity_type,entity_id,summary) "
        "VALUES(?,?,'qbo.reports.pull','organization',?,?)",
        (crm.new_id("aud"), requested_by_user_id, organization_id,
         f"{status}: {stored}/{len(reports)} reports" + (f" — {detail}" if detail else "")),
    )
    conn.commit()
    return {"sync_run_id": run_id, "status": status, "stored": stored,
            "requested": len(reports), "failures": failures,
            "period_start": start, "period_end": end}


# ---------------------------------------------------------------- reading --

def latest_run(conn, organization_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id,status,period_start,period_end,reports_requested,reports_stored,detail,"
        "started_at,finished_at FROM sync_runs WHERE organization_id=? "
        "ORDER BY started_at DESC LIMIT 1", (organization_id,),
    ).fetchone()
    return dict(row) if row else None


def snapshots_for(conn, organization_id: str, sync_run_id: str | None = None) -> list[dict]:
    """Report metadata only. Payloads are fetched one at a time, on purpose."""
    if sync_run_id:
        rows = conn.execute(
            "SELECT id,report_name,period_start,period_end,byte_size,fetched_at "
            "FROM source_snapshots WHERE sync_run_id=? ORDER BY report_name", (sync_run_id,))
    else:
        rows = conn.execute(
            "SELECT id,report_name,period_start,period_end,byte_size,fetched_at "
            "FROM source_snapshots WHERE organization_id=? ORDER BY fetched_at DESC,report_name",
            (organization_id,))
    output = []
    for row in rows:
        item = dict(row)
        report = REPORTS_BY_NAME.get(item["report_name"])
        item["label"] = report.label if report else item["report_name"]
        item["why"] = report.why if report else ""
        output.append(item)
    return output


def latest_report(conn, organization_id: str, report_name: str) -> dict | None:
    """The most recently fetched snapshot of one report, decrypted.

    Written for the trial balance, which is not a report anyone reads: it is the
    third independent implementation of the client's arithmetic, and
    `work_engine.trusted_ledger` needs it to prove the reconstruction is
    complete. Returns None when it has never been fetched, which the engine
    treats as "the third check was not run" rather than as agreement.
    """
    row = conn.execute(
        "SELECT id FROM source_snapshots WHERE organization_id=? AND report_name=? "
        "ORDER BY fetched_at DESC LIMIT 1", (organization_id, report_name)).fetchone()
    return read_snapshot(conn, row[0]) if row else None


def report_for_run(conn, sync_run_id: str, report_name: str) -> dict | None:
    """One report from one explicit sync, never a stale fallback.

    `latest_report` is right for browsing: if today's pull missed one report,
    the most recent successful copy is still useful to a person. It is wrong for
    a verification gate. Comparing today's document pull with yesterday's trial
    balance produces a puzzling disagreement that says nothing about either.
    Callers that trigger a refresh use this function so a failed refresh reads
    as "not supplied" rather than silently reaching back to an older snapshot.
    """
    row = conn.execute(
        "SELECT id FROM source_snapshots WHERE sync_run_id=? AND report_name=? "
        "ORDER BY fetched_at DESC LIMIT 1", (sync_run_id, report_name)).fetchone()
    return read_snapshot(conn, row[0]) if row else None


def read_snapshot(conn, snapshot_id: str) -> dict | None:
    row = conn.execute(
        "SELECT organization_id,report_name,period_start,period_end,payload_enc "
        "FROM source_snapshots WHERE id=?", (snapshot_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "organization_id": row[0], "report_name": row[1],
        "period_start": row[2], "period_end": row[3],
        "payload": json.loads(crypto.decrypt(row[4], crypto.QUICKBOOKS_REPORTS)),
    }
