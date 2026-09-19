"""The adapter against a server, rather than against a replaced `_request`.

Every other adapter test builds its subject with `object.__new__(QBOAdapter)` and
substitutes `_request`, which leaves the transport untested: URL construction,
the bearer header, the `QueryResponse` envelope, `STARTPOSITION` arithmetic,
refresh-on-401, the 429 and 5xx retries, and the HTTP-status-to-`QBOError`
mapping had never run as part of a whole pull. A real connection's first failure
lands in exactly that seam, and none of it needs a consented connection to test.

What these tests cannot establish is whether Intuit matches its own
documentation. `qbo_conformance` can only be wrong in the way the documentation
is wrong. That question needs a real file and nothing here substitutes for it.
"""
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
import qbo_conformance  # noqa: E402
from shimline import admin as admin_workspace  # noqa: E402
from shimline import clock, crm, qbo_adapter  # noqa: E402
from shimline import quickbooks as qbo  # noqa: E402
from shimline.postings import derive_ledger  # noqa: E402
from shimline.qbo_adapter import (MANIFEST_KEY, PAGE_SIZE, READ_OBJECTS,  # noqa: E402
                                  QBOAdapter, QBOError)


class ConformanceTestCase(unittest.TestCase):
    """A real connection row, a real adapter, and a server on localhost."""

    REALM = "4620816365"
    TOKEN = "conformance-access-token"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        service.DB_PATH = Path(self.temp.name) / "intake.db"
        service.UPLOADS_DIR = Path(self.temp.name) / "uploads"
        service.UPLOADS_DIR.mkdir()
        # cookie_secure=False so a session cookie survives TestClient's http://.
        admin_workspace.configure(
            db_factory=service._db, uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90,
            cookie_secure=False)
        qbo.configure(db_factory=service._db, client_id="id",
                      client_secret="secret",
                      redirect_uri="https://api.shimline.ca/qbo/callback",
                      environment="sandbox")

        conn = service._db()
        self.organization_id = crm.new_id("org")
        conn.execute(
            "INSERT INTO organizations(id,name,normalized_name,lifecycle_stage) "
            "VALUES(?,?,?,'active')",
            (self.organization_id, "Maple Build", "maple build"))
        self.connection_id = crm.new_id("con_qbo")
        conn.execute(
            "INSERT INTO connections(id,organization_id,provider,environment,"
            "realm_id_enc,realm_id_hash,access_token_enc,refresh_token_enc,"
            "access_expires_at,status) "
            "VALUES(?,?,'quickbooks','sandbox',?,?,?,?,?,'active')",
            (self.connection_id, self.organization_id,
             qbo.encrypt_token(self.REALM), "hash",
             qbo.encrypt_token(self.TOKEN), qbo.encrypt_token("refresh-token"),
             clock.format_timestamp(clock.now() + timedelta(hours=1))))
        conn.commit()
        conn.close()

        # Restored by addCleanup: production's own base URLs must not stay
        # pointed at a test server for the rest of the run.
        self._real_api_base = qbo_adapter.API_BASE
        self.addCleanup(setattr, qbo_adapter, "API_BASE", self._real_api_base)

    def open_db(self):
        """A connection to the test database, closed on cleanup.

        Windows will not delete the temporary directory while one is open, which
        is a cleanup failure that reads like sixteen test failures.
        """
        conn = service._db()
        self.addCleanup(conn.close)
        return conn

    def adapter(self, server):
        """A production adapter whose sandbox base URL is the test server."""
        qbo_adapter.API_BASE = dict(self._real_api_base, sandbox=server.origin)
        conn = service._db()
        # Windows will not delete the temporary directory while this is open.
        self.addCleanup(conn.close)
        built = QBOAdapter(conn, self.connection_id, sleep=lambda _seconds: None)
        self.assertEqual(built.base_url,
                         f"{server.origin}/v3/company/{self.REALM}")
        return built

    def server(self, **kwargs):
        running = qbo_conformance.ConformanceServer(**kwargs)
        running.__enter__()
        self.addCleanup(running.__exit__)
        return running


class WholePullTests(ConformanceTestCase):

    def test_a_pull_over_http_reaches_a_complete_ledger(self):
        """The seam end to end: HTTP in, a derived double-entry ledger out."""
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company()) as running:
            objects = self.adapter(running).pull_all()

        for entity in READ_OBJECTS:
            self.assertIn(entity, objects, entity)
        ledger = derive_ledger(objects)
        self.assertTrue(ledger.complete, ledger.reasons())
        self.assertEqual(sum(ledger.balances.values()), 0,
                         "a derived ledger that does not sum to zero is not a ledger")

    def test_the_manifest_records_what_the_server_actually_returned(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(purchases=4)) as running:
            objects = self.adapter(running).pull_all()

        manifest = objects[MANIFEST_KEY]
        self.assertEqual(manifest["source"], "quickbooks")
        self.assertEqual(set(manifest["read"]), set(READ_OBJECTS))
        self.assertEqual(manifest["read"]["Purchase"], 4)
        self.assertEqual(manifest["read"]["TaxAgency"], 1)

    def test_a_pull_costs_one_call_per_entity(self):
        """The capacity estimate, measured against a server that counts."""
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company()) as running:
            adapter = self.adapter(running)
            adapter.pull_all()
            self.assertEqual(adapter.api_call_count, len(READ_OBJECTS))
            self.assertEqual(len(running.requests), len(READ_OBJECTS))

    def test_fields_the_adapter_does_not_read_are_carried_not_rejected(self):
        """A real payload is wider than its reader.

        The invoice here carries a SubTotalLineDetail row, MetaData, Classification
        and AccountSubType, none of which the derivation consults. A reader that
        cannot ignore an unread field cannot read any real file.
        """
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company()) as running:
            objects = self.adapter(running).pull_all()

        invoice = objects["Invoice"][0]
        self.assertTrue(any(line.get("DetailType") == "SubTotalLineDetail"
                            for line in invoice["Line"]))
        self.assertTrue(derive_ledger(objects).complete)


class PaginationTests(ConformanceTestCase):
    """`STARTPOSITION` is 1-based, and off by one drops or repeats a row."""

    def test_a_pull_crossing_the_page_boundary_loses_and_repeats_nothing(self):
        rows = PAGE_SIZE + 3
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(purchases=rows)) as running:
            adapter = self.adapter(running)
            purchases = list(adapter.query("Purchase"))
            statements = [query for path, query in running.requests
                          if "Purchase" in query]

        self.assertEqual(len(purchases), rows)
        ids = [row["Id"] for row in purchases]
        self.assertEqual(len(set(ids)), rows, "a row was repeated across pages")
        self.assertEqual(ids[0], "900")
        self.assertEqual(ids[-1], str(900 + rows - 1))
        self.assertEqual(len(statements), 2)
        self.assertIn("STARTPOSITION+1", statements[0])
        self.assertIn(f"STARTPOSITION+{PAGE_SIZE + 1}", statements[1])

    def test_a_page_exactly_the_size_of_the_limit_asks_once_more(self):
        """Otherwise the last full page is mistaken for the end of the data."""
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(
                    purchases=PAGE_SIZE)) as running:
            adapter = self.adapter(running)
            self.assertEqual(len(list(adapter.query("Purchase"))), PAGE_SIZE)
            statements = [query for path, query in running.requests
                          if "Purchase" in query]
        self.assertEqual(len(statements), 2)


class TransportFailureTests(ConformanceTestCase):

    def test_a_failed_entity_is_named_rather_than_reported_as_a_url(self):
        """Every entity posts to the same /query URL, so without the name an
        operator cannot tell the chart of accounts from the tax rates."""
        faults = qbo_conformance.Faults(entity_errors={"TaxAgency": 400})
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(),
                faults=faults) as running:
            with self.assertRaises(QBOError) as raised:
                self.adapter(running).pull_all()
        self.assertIn("TaxAgency", str(raised.exception))

    def test_an_entity_that_fails_does_not_yield_a_partial_pull(self):
        """A pull missing an entity is the hole the manifest exists to close: it
        must raise, not return something that reads as a company with no rows."""
        faults = qbo_conformance.Faults(entity_errors={"Invoice": 400})
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(),
                faults=faults) as running:
            with self.assertRaises(QBOError):
                self.adapter(running).pull_all()

    def test_a_throttled_request_is_retried_and_succeeds(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(),
                faults=qbo_conformance.Faults(rate_limited_once=True)) as running:
            adapter = self.adapter(running)
            objects = adapter.pull_all()
            # Intuit meters attempts, so the retry must be counted, not hidden.
            self.assertEqual(adapter.api_call_count, len(READ_OBJECTS) + 1)
        self.assertTrue(derive_ledger(objects).complete)

    def test_a_server_error_is_retried_and_succeeds(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(),
                faults=qbo_conformance.Faults(server_error_once=True)) as running:
            objects = self.adapter(running).pull_all()
        self.assertEqual(set(objects[MANIFEST_KEY]["read"]), set(READ_OBJECTS))

    def test_a_401_refreshes_the_connection_and_retries(self):
        refreshed = []
        real_refresh = qbo.refresh_connection
        qbo.refresh_connection = lambda conn, connection_id: refreshed.append(
            connection_id)
        self.addCleanup(setattr, qbo, "refresh_connection", real_refresh)

        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(),
                faults=qbo_conformance.Faults(unauthorized_once=True)) as running:
            objects = self.adapter(running).pull_all()

        self.assertEqual(refreshed, [self.connection_id])
        self.assertTrue(derive_ledger(objects).complete)

    def test_a_token_the_server_rejects_does_not_loop_forever(self):
        """One refresh, then the error surfaces. An adapter that retries an
        authentication failure indefinitely takes the realm's rate limit with it.
        """
        real_refresh = qbo.refresh_connection
        qbo.refresh_connection = lambda conn, connection_id: None
        self.addCleanup(setattr, qbo, "refresh_connection", real_refresh)

        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(),
                access_token="a-different-token") as running:
            with self.assertRaises(QBOError) as raised:
                self.adapter(running).pull_all()
        self.assertIn("401", str(raised.exception))


class SingleObjectReadTests(ConformanceTestCase):

    def test_one_object_is_read_back_by_id(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company()) as running:
            row = self.adapter(running).read("Invoice", "1001")
        self.assertEqual(row["Id"], "1001")
        self.assertEqual(row["SyncToken"], "1")

    def test_an_unknown_id_is_an_error_and_not_an_empty_object(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company()) as running:
            with self.assertRaises(QBOError):
                self.adapter(running).read("Invoice", "does-not-exist")


class ServerContractTests(ConformanceTestCase):
    """The server is only useful while it still reflects the documented API."""

    def test_a_query_the_server_cannot_parse_is_raised_not_tolerated(self):
        """If the adapter stops sending the documented query form, that has to
        surface here rather than being quietly answered anyway."""
        with qbo_conformance.ConformanceServer(objects={}) as running:
            adapter = self.adapter(running)
            with self.assertRaises(Exception):
                adapter._request("GET", "query", query={"query": "SELECT nonsense"})

    def test_the_server_can_tell_a_broken_pager_from_a_correct_one(self):
        """Otherwise the pagination tests above prove only that nothing crashed.

        Three plausible ways to get `STARTPOSITION` wrong, each driven against the
        server directly. An off-by-one does not fail loudly -- it silently repeats
        or drops exactly one row per page boundary, which on a real file is one
        double-counted purchase.
        """
        rows = PAGE_SIZE + 3
        objects = qbo_conformance.documented_company(purchases=rows)
        truth = [row["Id"] for row in objects["Purchase"]]

        def page_through(advance, first_start=1):
            with qbo_conformance.ConformanceServer(objects=objects) as running:
                adapter = self.adapter(running)
                collected, start = [], first_start
                while True:
                    statement = (f"SELECT * FROM Purchase STARTPOSITION {start} "
                                 f"MAXRESULTS {PAGE_SIZE}")
                    page = (adapter._request("GET", "query",
                                             query={"query": statement})
                            .get("QueryResponse", {}).get("Purchase", []))
                    collected += [row["Id"] for row in page]
                    if len(page) < PAGE_SIZE:
                        return collected
                    start = advance(start, len(page))

        self.assertEqual(page_through(lambda start, size: start + size), truth)
        for label, advance, first in (
            ("start += size - 1 repeats a row", lambda s, n: s + n - 1, 1),
            ("start += size + 1 drops a row", lambda s, n: s + n + 1, 1),
            ("a 0-based first page reads nothing", lambda s, n: s + n, 0),
        ):
            with self.subTest(mistake=label):
                self.assertNotEqual(page_through(advance, first), truth, label)

    def test_the_minor_version_is_required_the_way_intuit_requires_it(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company()) as running:
            adapter = self.adapter(running)
            self.assertTrue(list(adapter.query("Account")))
            self.assertTrue(all("minorversion=75" in query
                                for _path, query in running.requests))


if __name__ == "__main__":
    unittest.main()
