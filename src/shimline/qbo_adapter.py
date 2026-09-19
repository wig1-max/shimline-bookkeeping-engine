"""Complete QuickBooks entity adapter for the Bookkeeping Work Engine.

Reports answer questions; entities prove and repair them.  This adapter pulls
the source objects needed to reconstruct a ledger and exposes a deliberately
small, sandbox-only mutation catalogue.  Every mutation carries Intuit's
``requestid`` so a retry cannot create a second object.
"""
from __future__ import annotations

import json
import base64
import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable

from . import quickbooks
from .qbo_reports import API_BASE, MINOR_VERSION, REQUEST_TIMEOUT

PAGE_SIZE = 500
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
READ_OBJECTS = (
    "Account", "Customer", "Vendor", "Class", "TaxCode",
    # The rates themselves, and who administers each. Without these a return
    # line cannot name its own rate, and -- the part that matters -- nothing can
    # tell GST/HST from PST, which is the difference between a correct input tax
    # credit and one overstated on a CRA filing.
    "TaxRate", "TaxAgency",
    "Estimate", "Invoice", "Payment", "Bill", "Purchase", "Deposit", "JournalEntry",
    # Settlements and reversals. Every one of these posts to the ledger, and
    # until they were read the reconstruction refused any client who had paid a
    # bill or moved money between their own accounts -- which is every client.
    "BillPayment", "CreditMemo", "VendorCredit", "Transfer",
    "SalesReceipt", "RefundReceipt",
    "Attachable",
)
# Estimate is read but never written: it is the client's own quoted price, and
# check 10 compares it against actual cost. Nothing in the mutation catalogue
# touches one.
TRANSACTION_OBJECTS = {
    "Estimate", "Invoice", "Payment", "Bill", "Purchase", "Deposit", "JournalEntry",
    "BillPayment", "CreditMemo", "VendorCredit", "Transfer",
    "SalesReceipt", "RefundReceipt",
}
SAFE_ACTIONS = {
    "attach_evidence", "assign_project", "assign_class",
    "create_vendor", "correct_vendor", "create_customer", "correct_customer",
    "create_bill", "create_expense", "correcting_entry",
}
TYPE_PATH = {name: name.lower() for name in READ_OBJECTS}

# The key a pull records itself under. Leading underscore so `derive_ledger`
# skips it the way it skips `_Postings`, and so it cannot be mistaken for an
# entity type.
MANIFEST_KEY = "_pull_manifest"


def manifest(read: dict[str, int], *, source: str) -> dict:
    """A record of which entity types a pull actually read, and how many rows.

    Kept as a plain dict rather than a dataclass because it travels with the
    objects through JSON and into the working papers, where a person may have
    to read it a year later to answer "was this balance computed from a
    complete pull?".
    """
    return {"source": source, "read": dict(read),
            "types": sorted(read), "rows": sum(read.values())}


def declare_pull(objects: dict[str, list[dict]], *,
                 source: str = "fixture") -> dict[str, list[dict]]:
    """Stamp a dict of objects as a complete pull of every readable type.

    For the synthetic oracle and for fixtures, which genuinely do know what
    they contain. It is not a way around the gate: it asserts that every entity
    type *was* read, and a caller who stamps an incomplete dict is stating
    something false about it rather than bypassing a check. That is the right
    shape -- the gate exists to stop a silent absence, not a declared one.
    """
    objects[MANIFEST_KEY] = manifest(
        {kind: len(objects.get(kind) or []) for kind in READ_OBJECTS},
        source=source)
    return objects


class QBOError(RuntimeError):
    pass


class StaleObjectError(QBOError):
    pass


class UncertainWriteError(QBOError):
    """A transport failed after a mutation may have reached Intuit."""


@dataclass(frozen=True)
class MutationResult:
    object_type: str
    object_id: str
    sync_token: str
    payload: dict
    replayed: bool = False


class QBOAdapter:
    """Authenticated QBO V3 reader/writer for one connection."""

    def __init__(self, conn, connection_id: str, *, sleep: Callable = time.sleep):
        row = conn.execute(
            "SELECT realm_id_enc,environment,status FROM connections WHERE id=?",
            (connection_id,),
        ).fetchone()
        if not row:
            raise QBOError("QuickBooks connection not found")
        if row[2] != "active":
            raise quickbooks.ReconnectRequired("QuickBooks connection is not active")
        self.conn = conn
        self.connection_id = connection_id
        self.realm_id = quickbooks.decrypt_token(row[0])
        self.environment = row[1]
        self.sleep = sleep
        # Intuit meters data-out -- reads and reports -- not writes, and this
        # product is almost entirely data-out. Counting calls while client
        # files are small turns the capacity estimate in
        # docs/INTUIT_ECOSYSTEM_RESEARCH.md into a measurement. Every HTTP
        # attempt is counted, retries included, because Intuit meters attempts.
        self.api_calls: dict[str, int] = {}

    @property
    def api_call_count(self) -> int:
        return sum(self.api_calls.values())

    def _count_call(self, label: str) -> None:
        self.api_calls[label] = self.api_calls.get(label, 0) + 1

    @property
    def base_url(self) -> str:
        return f"{API_BASE[self.environment]}/v3/company/{self.realm_id}"

    def _request(self, method: str, path: str, *, query: dict | None = None,
                 payload: dict | None = None, attempts: int = 3,
                 mutation: bool = False, label: str | None = None) -> dict:
        params = {"minorversion": MINOR_VERSION, **(query or {})}
        url = f"{self.base_url}/{path}?{urllib.parse.urlencode(params)}"
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        for attempt in range(attempts):
            self._count_call(label or path.split("/")[0])
            token = quickbooks.ensure_access_token(self.conn, self.connection_id)
            request = urllib.request.Request(url, data=body, method=method, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                    return json.loads(response.read(16 * 1024 * 1024))
            except urllib.error.HTTPError as exc:
                raw = exc.read(16 * 1024)
                text = raw.decode("utf-8", "replace")
                if exc.code == 401 and attempt == 0:
                    quickbooks.refresh_connection(self.conn, self.connection_id)
                    continue
                if exc.code == 400 and ("5010" in text or "stale" in text.lower()):
                    raise StaleObjectError("QuickBooks object changed since review")
                if exc.code == 429 or exc.code >= 500:
                    if attempt + 1 < attempts:
                        self.sleep(min(2 ** attempt, 4))
                        continue
                raise QBOError(f"QuickBooks {method} {path} failed (HTTP {exc.code})") from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                if mutation:
                    # requestid makes a caller retry safe, but the caller must
                    # first record that the outcome is unknown.
                    raise UncertainWriteError("QuickBooks write outcome is uncertain") from exc
                if attempt + 1 < attempts:
                    self.sleep(min(2 ** attempt, 4))
                    continue
                raise QBOError("Could not reach QuickBooks") from exc
        raise QBOError("QuickBooks request failed")

    def query(self, object_type: str, *, where: str = "") -> Iterable[dict]:
        """Yield every object with explicit QBO pagination."""
        if object_type not in READ_OBJECTS:
            raise ValueError(f"Unsupported QuickBooks object: {object_type}")
        start = 1
        while True:
            clause = f" WHERE {where}" if where else ""
            statement = (f"SELECT * FROM {object_type}{clause} "
                         f"STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}")
            response = self._request("GET", "query", query={"query": statement},
                                     label=object_type)
            page = response.get("QueryResponse", {}).get(object_type, [])
            yield from page
            if len(page) < PAGE_SIZE:
                return
            start += len(page)

    def pull_all(self) -> dict[str, list[dict]]:
        """Every readable object, and a record of what was actually read.

        The manifest is not bookkeeping about the pull -- it closes a hole that
        would otherwise produce silently wrong books. An entity that failed to
        load and an entity with no rows arrive at `derive_ledger` as exactly the
        same thing: an absent key. A pull that lost every invoice would then
        produce a ledger reporting itself *complete*, with revenue and
        receivables missing, and the trial balance would still sum to zero
        because both halves of every invoice went missing together. Neither the
        double-entry check nor Beancount can see that; only knowing what was
        supposed to be there can.

        A failure names the entity. `query` posts every read to the same URL, so
        without this an operator sees "query failed (HTTP 400)" and has no way
        to tell whether it was the chart of accounts or the tax rates.
        """
        objects: dict[str, list[dict]] = {}
        read: dict[str, int] = {}
        for kind in READ_OBJECTS:
            try:
                rows = list(self.query(kind))
            except QBOError as exc:
                raise QBOError(
                    f"QuickBooks would not return {kind}, so this pull is "
                    f"incomplete and nothing was computed from it: {exc}") from exc
            objects[kind] = rows
            read[kind] = len(rows)
        objects["_pull_manifest"] = manifest(read, source="quickbooks")
        return objects

    def call_report(self) -> dict:
        """What this adapter has cost against the metered API so far."""
        return {"total": self.api_call_count, "by_object": dict(sorted(self.api_calls.items()))}

    def read(self, object_type: str, object_id: str) -> dict:
        path = TYPE_PATH.get(object_type)
        if not path:
            raise ValueError(f"Unsupported QuickBooks object: {object_type}")
        response = self._request("GET", f"{path}/{urllib.parse.quote(str(object_id))}")
        return response.get(object_type, response)

    def download_attachment(self, attachment: dict) -> bytes:
        """Fetch attachment bytes on demand; never follow an arbitrary URL."""
        uri = attachment.get("TempDownloadUri")
        if not uri and attachment.get("Id"):
            uri = self.read("Attachable", str(attachment["Id"])).get("TempDownloadUri")
        parsed = urllib.parse.urlparse(str(uri or ""))
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".intuit.com"):
            raise QBOError("QuickBooks returned an invalid attachment URL")
        token = quickbooks.ensure_access_token(self.conn, self.connection_id)
        request = urllib.request.Request(uri, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                content = response.read(MAX_ATTACHMENT_BYTES + 1)
        except Exception as exc:
            raise QBOError("Could not read QuickBooks attachment") from exc
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise QBOError("QuickBooks attachment exceeds the 20 MiB v0 limit")
        return content

    def _upload_attachment(self, payload: dict, idempotency_key: str) -> MutationResult:
        """Upload evidence using QBO's paired metadata/content multipart form."""
        try:
            content = base64.b64decode(payload["content_base64"], validate=True)
        except Exception as exc:
            raise ValueError("Attachment content must be valid base64") from exc
        if not content or len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachment is empty or exceeds the 20 MiB v0 limit")
        filename = str(payload.get("FileName") or "evidence.bin").replace('"', "")
        content_type = str(payload.get("ContentType") or "application/octet-stream")
        metadata = {key: value for key, value in payload.items()
                    if key not in {"content_base64"} and not key.startswith("_")}
        boundary = "shimline-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file_metadata_01\"\r\n"
            "Content-Type: application/json\r\n\r\n".encode() +
            json.dumps(metadata, separators=(",", ":")).encode() + b"\r\n",
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file_content_01\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode() +
            content + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        query = urllib.parse.urlencode({"minorversion": MINOR_VERSION, "requestid": idempotency_key})
        url = f"{self.base_url}/upload?{query}"
        token = quickbooks.ensure_access_token(self.conn, self.connection_id)
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                result = json.loads(response.read(16 * 1024 * 1024))
        except (TimeoutError, urllib.error.URLError) as exc:
            raise UncertainWriteError("QuickBooks attachment outcome is uncertain") from exc
        except urllib.error.HTTPError as exc:
            raise QBOError(f"QuickBooks attachment upload failed (HTTP {exc.code})") from exc
        attached = ((result.get("AttachableResponse") or [{}])[0].get("Attachable") or
                    result.get("Attachable"))
        if not attached or not attached.get("Id"):
            raise QBOError("QuickBooks did not return the uploaded attachment")
        return MutationResult("Attachable", str(attached["Id"]), str(attached.get("SyncToken", "")), attached)

    def mutate(self, action: str, *, object_type: str, payload: dict,
               idempotency_key: str, object_id: str | None = None,
               expected_sync_token: str | None = None) -> MutationResult:
        """Apply one approved, bounded mutation in QBO sandbox.

        Delete operations are not representable here. Updates are sparse and
        always use a reviewed concurrency token. A single stale-token retry is
        allowed after re-reading the target; no other fields are copied from
        the newly-read object into the approved change.
        """
        if self.environment != "sandbox":
            raise QBOError("Bookkeeping Work Engine v0 writes are sandbox-only")
        if action not in SAFE_ACTIONS:
            raise ValueError(f"Unsafe or unsupported action: {action}")
        if action == "attach_evidence":
            if object_type != "Attachable" or object_id:
                raise ValueError("Evidence attachment must create an Attachable")
            return self._upload_attachment(payload, idempotency_key)
        path = TYPE_PATH.get(object_type)
        if not path:
            raise ValueError(f"Unsupported QuickBooks object: {object_type}")
        # Keys beginning with '_' are canonical/test bookkeeping metadata, not
        # provider fields. They are useful to the synthetic ledger but must
        # never cross the QBO boundary.
        body = {key: value for key, value in payload.items() if not key.startswith("_")}
        if object_id:
            if not expected_sync_token:
                raise ValueError("An update requires the reviewed SyncToken")
            body.update({"Id": str(object_id), "SyncToken": str(expected_sync_token), "sparse": True})
        query = {"requestid": idempotency_key}
        response = self._request("POST", path, query=query, payload=body, mutation=True)
        result = response.get(object_type, response)
        return MutationResult(object_type, str(result["Id"]), str(result.get("SyncToken", "")), result)
