"""A local server that speaks the QuickBooks Online API as Intuit documents it.

Every existing adapter test builds its subject with `object.__new__(QBOAdapter)`
and replaces `_request`. That is convenient and it leaves a seam untested: URL
construction, the bearer header, the `QueryResponse` envelope, `STARTPOSITION`
and `MAXRESULTS` arithmetic, refresh-on-401, the 429 and 5xx retries, and the
mapping from an HTTP status to a `QBOError` have never executed as part of a
whole pull. A real connection's first failure lands in exactly that seam.

So this serves the documented shapes over real HTTP and lets production code run
unchanged against it. What it cannot do is tell us whether Intuit matches its own
documentation -- it can only be wrong in the way the documentation is wrong. That
question needs a consented connection and nothing here substitutes for it.

Development tool. It is not under `shimline/`, so it is not part of the deployed
path set in `scripts/deploy_workspace.sh`.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Parsed out of the statement the adapter sends. It builds exactly one shape --
# `SELECT * FROM <Entity> [WHERE ...] STARTPOSITION n MAXRESULTS m` -- and this
# deliberately refuses anything else rather than guessing, so a change to how the
# adapter writes its query surfaces here instead of being quietly tolerated.
_QUERY = re.compile(
    r"^\s*SELECT\s+\*\s+FROM\s+(?P<entity>\w+)"
    r"(?:\s+WHERE\s+(?P<where>.+?))?"
    r"\s+STARTPOSITION\s+(?P<start>\d+)\s+MAXRESULTS\s+(?P<max>\d+)\s*$",
    re.IGNORECASE | re.DOTALL)


class ConformanceError(Exception):
    """The server was asked for something Intuit's API would not answer."""


@dataclass
class Faults:
    """Transport conditions a real connection produces and a fixture cannot.

    Each is consumed once, so a test asserts that the adapter recovered rather
    than that it kept hitting the same wall.
    """

    unauthorized_once: bool = False
    rate_limited_once: bool = False
    server_error_once: bool = False
    # Entity name -> HTTP status. Permanent: a pull has to name which entity
    # would not load, because every entity read posts to the same /query URL.
    entity_errors: dict[str, int] = field(default_factory=dict)


@dataclass
class ConformanceServer:
    """A QBO-shaped HTTP server for one realm.

    `objects` maps entity name to the rows that entity returns, in the field
    shapes Intuit's entity reference documents.
    """

    objects: dict[str, list[dict]]
    realm_id: str = "4620816365"
    access_token: str = "conformance-access-token"
    faults: Faults = field(default_factory=Faults)
    reports: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Every request, so a test can assert what the adapter actually asked
        # for -- including the page boundaries, which is the whole point.
        self.requests: list[tuple[str, str]] = []

    # ------------------------------------------------------------- lifecycle --

    def __enter__(self) -> "ConformanceServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # noqa: N802 - silence the test run
                pass

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's contract
                outer._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def origin(self) -> str:
        assert self._server, "server is not running"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # --------------------------------------------------------------- serving --

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlparse(handler.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.requests.append((parsed.path, parsed.query))

        # Intuit rejects a request with no usable bearer token before it looks
        # at anything else, and the adapter's refresh path depends on that 401.
        presented = (handler.headers.get("Authorization") or "")
        if self.faults.unauthorized_once:
            self.faults.unauthorized_once = False
            return self._send(handler, 401, {"Fault": {"Error": [
                {"code": "3200", "Message": "message=AuthenticationFailed"}]}})
        if presented != f"Bearer {self.access_token}":
            return self._send(handler, 401, {"Fault": {"Error": [
                {"code": "3200", "Message": "message=AuthenticationFailed"}]}})

        if self.faults.rate_limited_once:
            self.faults.rate_limited_once = False
            return self._send(handler, 429, {"Fault": {"Error": [
                {"code": "3001", "Message": "Throttle exceeded"}]}})
        if self.faults.server_error_once:
            self.faults.server_error_once = False
            return self._send(handler, 500, {"Fault": {"Error": [
                {"code": "5000", "Message": "Internal error"}]}})

        # Every real URL carries it, and the adapter pins a version on purpose:
        # a response shape is only meaningful relative to one.
        if params.get("minorversion") != ["75"]:
            return self._send(handler, 400, {"Fault": {"Error": [
                {"code": "4000", "Message": "minorversion is required"}]}})

        prefix = f"/v3/company/{self.realm_id}/"
        if not parsed.path.startswith(prefix):
            return self._send(handler, 401, {"Fault": {"Error": [
                {"code": "3100", "Message": "realm does not match the token"}]}})
        route = parsed.path[len(prefix):]

        if route == "query":
            return self._query(handler, params)
        if route.startswith("reports/"):
            return self._report(handler, route[len("reports/"):])
        return self._read_one(handler, route)

    def _query(self, handler, params) -> None:
        statements = params.get("query") or []
        if len(statements) != 1:
            return self._send(handler, 400, {"Fault": {"Error": [
                {"code": "4000", "Message": "exactly one query is required"}]}})
        match = _QUERY.match(statements[0])
        if not match:
            raise ConformanceError(
                f"the adapter sent a query this server cannot parse, which means "
                f"it no longer matches the documented form: {statements[0]!r}")

        entity = match.group("entity")
        status = self.faults.entity_errors.get(entity)
        if status:
            return self._send(handler, status, {"Fault": {"Error": [
                {"code": "4000", "Message": f"{entity} is unavailable"}]}})

        rows = list(self.objects.get(entity) or [])
        # QBO's STARTPOSITION is 1-based, and getting that wrong by one silently
        # drops or repeats a row on every page boundary.
        start = int(match.group("start"))
        page = rows[start - 1:start - 1 + int(match.group("max"))]
        body: dict = {"QueryResponse": {}, "time": "2026-09-12T00:00:00.000-05:00"}
        if page:
            body["QueryResponse"] = {
                entity: page, "startPosition": start, "maxResults": len(page)}
        return self._send(handler, 200, body)

    def _report(self, handler, name) -> None:
        if name not in self.reports:
            return self._send(handler, 400, {"Fault": {"Error": [
                {"code": "4000", "Message": f"unknown report {name}"}]}})
        return self._send(handler, 200, self.reports[name])

    def _read_one(self, handler, route) -> None:
        parts = route.split("/")
        if len(parts) != 2:
            return self._send(handler, 404, {"Fault": {"Error": [
                {"code": "4000", "Message": "no such resource"}]}})
        path, object_id = parts
        # The adapter lowercases the type for the path; map it back.
        for entity, rows in self.objects.items():
            if entity.lower() != path:
                continue
            for row in rows:
                if str(row.get("Id")) == object_id:
                    return self._send(handler, 200, {entity: row})
        return self._send(handler, 404, {"Fault": {"Error": [
            {"code": "610", "Message": "Object Not Found"}]}})

    def _send(self, handler, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json;charset=UTF-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)


# ---------------------------------------------------------------------------
# A company in the field shapes Intuit's entity reference documents.
#
# Deliberately not `synthetic_books`. That corpus exists to be graded against a
# golden ledger and carries `_Postings`, which QuickBooks never sends; reusing it
# here would check the engine against its own assumptions a second time. These
# rows carry only fields the documented API returns -- including several the
# adapter does not read, because a real payload is wider than its reader and a
# reader that breaks on an unread field breaks on every real file.
# ---------------------------------------------------------------------------

_TAX_AGENCY = {
    # Added to READ_OBJECTS from documentation and never once read from a live
    # company. If this entity is not queryable in practice, every pull fails.
    "Id": "1", "DisplayName": "Canada Revenue Agency",
    "TaxRegistrationNumber": "800000000RT0001",
    "TaxTrackedOnPurchases": True, "TaxTrackedOnSales": True,
}

_ACCOUNTS = [
    {"Id": "35", "Name": "Chequing", "AccountType": "Bank",
     "AccountSubType": "Checking", "Classification": "Asset",
     "CurrentBalance": 18422.15, "Active": True, "SyncToken": "3",
     "MetaData": {"CreateTime": "2025-01-04T09:12:00-05:00"}},
    {"Id": "36", "Name": "Savings", "AccountType": "Bank",
     "AccountSubType": "Savings", "Classification": "Asset",
     "CurrentBalance": 9000.00, "Active": True, "SyncToken": "1"},
    {"Id": "84", "Name": "Accounts Receivable (A/R)",
     "AccountType": "Accounts Receivable",
     "AccountSubType": "AccountsReceivable", "Classification": "Asset",
     "Active": True, "SyncToken": "0"},
    {"Id": "33", "Name": "Accounts Payable (A/P)",
     "AccountType": "Accounts Payable", "AccountSubType": "AccountsPayable",
     "Classification": "Liability", "Active": True, "SyncToken": "0"},
    {"Id": "89", "Name": "GST/HST Payable",
     "AccountType": "Other Current Liability",
     "AccountSubType": "GlobalTaxPayable", "Classification": "Liability",
     "Active": True, "SyncToken": "0"},
    {"Id": "79", "Name": "Contract Income", "AccountType": "Income",
     "AccountSubType": "SalesOfProductIncome", "Classification": "Revenue",
     "Active": True, "SyncToken": "0"},
    {"Id": "64", "Name": "Job Materials", "AccountType": "Cost of Goods Sold",
     "AccountSubType": "SuppliesMaterialsCogs", "Classification": "Expense",
     "Active": True, "SyncToken": "0"},
]

_TAX_LINE_13 = {
    "DetailType": "TaxLineDetail",
    "TaxLineDetail": {"TaxRateRef": {"value": "4"}, "PercentBased": True,
                      "TaxPercent": 13},
}


def _tax(total, taxable):
    line = dict(_TAX_LINE_13, Amount=total)
    line["TaxLineDetail"] = dict(_TAX_LINE_13["TaxLineDetail"],
                                 NetAmountTaxable=taxable)
    return {"TotalTax": total, "TxnTaxCodeRef": {"value": "TAX"},
            "TaxLine": [line]}


def _sales_line(amount, *, line_id="1", project=None):
    detail = {"ItemAccountRef": {"value": "79"}, "ClassRef": {"value": "C1"},
              "TaxCodeRef": {"value": "TAX"}, "Qty": 1, "UnitPrice": amount}
    if project:
        detail["CustomerRef"] = {"value": project}
    return {"Id": line_id, "LineNum": int(line_id), "Amount": amount,
            "Description": "Contract work", "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": detail}


def _expense_line(amount, *, line_id="1", account="64"):
    return {"Id": line_id, "Amount": amount, "Description": "Framing lumber",
            "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": account},
                "BillableStatus": "NotBillable",
                "TaxCodeRef": {"value": "TAX"}}}


def documented_company(*, purchases: int = 3) -> dict[str, list[dict]]:
    """A Canadian contractor's file, one entity per `READ_OBJECTS` entry.

    `purchases` exists to push a single entity past `PAGE_SIZE`: the page
    arithmetic is the part of the adapter no test has ever run against a server
    that actually paginates.
    """
    purchase_rows = [
        {"Id": f"{900 + index}", "TxnDate": "2026-07-14", "TotalAmt": 124.30,
         "PaymentType": "CreditCard",
         "AccountRef": {"value": "35", "name": "Chequing"},
         "EntityRef": {"value": "V1", "name": "Northern Supply",
                       "type": "Vendor"},
         "DocNumber": f"EXP-{index}", "SyncToken": "0",
         "Line": [_expense_line(124.30)]}
        for index in range(purchases)
    ]
    return {
        "Account": [dict(row) for row in _ACCOUNTS],
        "Customer": [
            {"Id": "CU1", "DisplayName": "Maple Build Ltd", "Job": False,
             "Active": True, "Balance": 1130.00, "SyncToken": "2",
             "PrimaryEmailAddr": {"Address": "ap@maplebuild.invalid"}},
            {"Id": "P1", "DisplayName": "Maple Build Ltd:Kitchen", "Job": True,
             "ParentRef": {"value": "CU1"}, "Active": True, "SyncToken": "0"},
        ],
        "Vendor": [{"Id": "V1", "DisplayName": "Northern Supply",
                    "Active": True, "Balance": 0.0, "SyncToken": "1"}],
        "Class": [{"Id": "C1", "Name": "Residential", "Active": True,
                   "FullyQualifiedName": "Residential", "SyncToken": "0"}],
        "TaxCode": [
            {"Id": "TAX", "Name": "HST ON", "Active": True, "Taxable": True,
             "SalesTaxRateList": {"TaxRateDetail": [
                 {"TaxRateRef": {"value": "4", "name": "HST ON 13%"},
                  "TaxTypeApplicable": "TaxOnAmount", "TaxOrder": 0}]},
             "PurchaseTaxRateList": {"TaxRateDetail": [
                 {"TaxRateRef": {"value": "4", "name": "HST ON 13%"},
                  "TaxTypeApplicable": "TaxOnAmount", "TaxOrder": 0}]}},
            {"Id": "NON", "Name": "Exempt", "Active": True, "Taxable": False},
        ],
        "TaxRate": [{"Id": "4", "Name": "HST ON 13%", "RateValue": 13,
                     "AgencyRef": {"value": "1"}, "SpecialTaxType": "NONE",
                     "DisplayType": "ReadOnly", "Active": True,
                     "SyncToken": "0"}],
        "TaxAgency": [dict(_TAX_AGENCY)],
        "Estimate": [{"Id": "E1", "TxnDate": "2026-06-02", "TotalAmt": 4200.00,
                      "TxnStatus": "Accepted", "CustomerRef": {"value": "CU1"},
                      "SyncToken": "0", "Line": [_sales_line(4200.00)]}],
        "Invoice": [{"Id": "1001", "TxnDate": "2026-07-02",
                     "DueDate": "2026-08-01", "DocNumber": "1001",
                     "TotalAmt": 1130.00, "Balance": 630.00,
                     "CustomerRef": {"value": "CU1", "name": "Maple Build Ltd"},
                     "ARAccountRef": {"value": "84"}, "SyncToken": "1",
                     "ApplyTaxAfterDiscount": False,
                     "Line": [_sales_line(1000.00, project="P1"),
                              # QBO really does send these, and a reader that
                              # treats one as a money line double-counts a sale.
                              {"DetailType": "SubTotalLineDetail",
                               "Amount": 1000.00, "SubTotalLineDetail": {}}],
                     "TxnTaxDetail": _tax(130.00, 1000.00)}],
        "Payment": [{"Id": "PAY1", "TxnDate": "2026-07-20", "TotalAmt": 500.00,
                     "UnappliedAmt": 0.0, "SyncToken": "0",
                     "CustomerRef": {"value": "CU1"},
                     "DepositToAccountRef": {"value": "35"},
                     "ARAccountRef": {"value": "84"},
                     "Line": [{"Amount": 500.00, "LinkedTxn": [
                         {"TxnId": "1001", "TxnType": "Invoice"}]}]}],
        "Bill": [{"Id": "4001", "TxnDate": "2026-07-06",
                  "DueDate": "2026-08-05", "TotalAmt": 500.00,
                  "Balance": 100.00, "VendorRef": {"value": "V1"},
                  "APAccountRef": {"value": "33"}, "SyncToken": "1",
                  "Line": [_expense_line(500.00)]}],
        "Purchase": purchase_rows,
        "Deposit": [{"Id": "D1", "TxnDate": "2026-07-22", "TotalAmt": 220.00,
                     "DepositToAccountRef": {"value": "35"}, "SyncToken": "0",
                     "Line": [{"Id": "1", "Amount": 220.00,
                               "DetailType": "DepositLineDetail",
                               "DepositLineDetail": {
                                   "AccountRef": {"value": "79"},
                                   "CheckNum": "881"}}]}],
        "JournalEntry": [{"Id": "JE1", "TxnDate": "2026-07-31",
                          "DocNumber": "JE-07", "Adjustment": False,
                          "SyncToken": "0", "Line": [
                              {"Id": "0", "Amount": 75.00,
                               "Description": "Accrue unbilled materials",
                               "DetailType": "JournalEntryLineDetail",
                               "JournalEntryLineDetail": {
                                   "PostingType": "Debit",
                                   "AccountRef": {"value": "64"}}},
                              {"Id": "1", "Amount": 75.00,
                               "DetailType": "JournalEntryLineDetail",
                               "JournalEntryLineDetail": {
                                   "PostingType": "Credit",
                                   "AccountRef": {"value": "33"}}}]}],
        "BillPayment": [{"Id": "BP1", "TxnDate": "2026-07-28",
                         "TotalAmt": 400.00, "PayType": "Check",
                         "VendorRef": {"value": "V1"},
                         "APAccountRef": {"value": "33"}, "SyncToken": "0",
                         "CheckPayment": {"BankAccountRef": {"value": "35"}},
                         "Line": [{"Amount": 400.00, "LinkedTxn": [
                             {"TxnId": "4001", "TxnType": "Bill"}]}]}],
        "CreditMemo": [{"Id": "CM1", "TxnDate": "2026-07-29",
                        "TotalAmt": 113.00, "Balance": 113.00,
                        "RemainingCredit": 113.00,
                        "CustomerRef": {"value": "CU1"},
                        "ARAccountRef": {"value": "84"}, "SyncToken": "0",
                        "Line": [_sales_line(100.00)],
                        "TxnTaxDetail": _tax(13.00, 100.00)}],
        "VendorCredit": [{"Id": "VC1", "TxnDate": "2026-07-30",
                          "TotalAmt": 60.00, "VendorRef": {"value": "V1"},
                          "APAccountRef": {"value": "33"}, "SyncToken": "0",
                          "Line": [_expense_line(60.00)]}],
        "Transfer": [{"Id": "T1", "TxnDate": "2026-07-31", "Amount": 1500.00,
                      "FromAccountRef": {"value": "35"},
                      "ToAccountRef": {"value": "36"}, "SyncToken": "0"}],
        "SalesReceipt": [{"Id": "SR1", "TxnDate": "2026-07-25",
                          "TotalAmt": 226.00, "Balance": 0.0,
                          "CustomerRef": {"value": "CU1"},
                          "DepositToAccountRef": {"value": "35"},
                          "PaymentMethodRef": {"value": "1"}, "SyncToken": "0",
                          "Line": [_sales_line(200.00)],
                          "TxnTaxDetail": _tax(26.00, 200.00)}],
        "RefundReceipt": [{"Id": "RR1", "TxnDate": "2026-07-26",
                           "TotalAmt": 56.50, "CustomerRef": {"value": "CU1"},
                           "DepositToAccountRef": {"value": "35"},
                           "SyncToken": "0", "Line": [_sales_line(50.00)],
                           "TxnTaxDetail": _tax(6.50, 50.00)}],
        "Attachable": [{"Id": "AT1", "FileName": "northern-supply-0714.pdf",
                        "ContentType": "application/pdf", "Size": 48122,
                        "SyncToken": "0",
                        "AttachableRef": [{"EntityRef": {"value": "900",
                                                         "type": "Purchase"}}]}],
    }


def company_with_missing_account_reference() -> dict[str, list[dict]]:
    """A documented file whose transaction outlived one chart row.

    The server cannot prove why the id is absent -- archive, permission or API
    semantics are all possible -- and deliberately does not pretend to. It
    provides the exact observable shape first-contact must measure.
    """
    objects = documented_company()
    detail = objects["Purchase"][0]["Line"][0][
        "AccountBasedExpenseLineDetail"]
    detail["AccountRef"] = {"value": "ARCHIVED-64", "name": "Old materials"}
    return objects
