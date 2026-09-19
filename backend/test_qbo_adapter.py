"""Entity-level QBO adapter controls."""
import json
import unittest
from contextlib import contextmanager
from unittest import mock

from shimline.qbo_adapter import PAGE_SIZE, QBOAdapter, QBOError, READ_OBJECTS


class QBOAdapterTests(unittest.TestCase):
    def test_complete_reconstruction_object_catalogue_is_present(self):
        self.assertTrue({
            "Invoice", "Payment", "Bill", "Purchase", "Deposit", "JournalEntry",
            "Account", "Vendor", "Customer", "Class", "TaxCode", "Attachable",
        }.issubset(READ_OBJECTS))

    def test_estimates_are_read_so_a_quote_can_be_compared_against_actuals(self):
        self.assertIn("Estimate", READ_OBJECTS)

    def test_query_paginates_until_a_short_page(self):
        adapter = object.__new__(QBOAdapter)
        calls = []

        def request(method, path, query=None, **kwargs):
            calls.append(query["query"])
            if len(calls) == 1:
                return {"QueryResponse": {"Invoice": [{"Id": str(i)} for i in range(PAGE_SIZE)]}}
            return {"QueryResponse": {"Invoice": [{"Id": "last"}]}}

        adapter._request = request
        rows = list(adapter.query("Invoice"))
        self.assertEqual(len(rows), PAGE_SIZE + 1)
        self.assertIn("STARTPOSITION 1", calls[0])
        self.assertIn(f"STARTPOSITION {PAGE_SIZE + 1}", calls[1])

    def test_v0_refuses_every_production_write(self):
        adapter = object.__new__(QBOAdapter)
        adapter.environment = "production"
        with self.assertRaises(QBOError):
            adapter.mutate("correcting_entry", object_type="JournalEntry", payload={},
                           idempotency_key="one")

    def test_delete_is_not_a_representable_action(self):
        adapter = object.__new__(QBOAdapter)
        adapter.environment = "sandbox"
        with self.assertRaises(ValueError):
            adapter.mutate("delete", object_type="Purchase", payload={},
                           object_id="1", expected_sync_token="0", idempotency_key="one")


class MeteredCallTests(unittest.TestCase):
    """Intuit meters data-out. A pull has to be able to say what it cost."""

    def _adapter(self):
        adapter = object.__new__(QBOAdapter)
        adapter.conn = None
        adapter.connection_id = "con_1"
        adapter.environment = "sandbox"
        adapter.realm_id = "4620816365"
        adapter.api_calls = {}
        return adapter

    @contextmanager
    def _transport(self, payload):
        class _Response:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner, _size=None):
                return json.dumps(payload).encode()

        with mock.patch("shimline.qbo_adapter.quickbooks.ensure_access_token",
                        return_value="token"), \
             mock.patch("shimline.qbo_adapter.urllib.request.urlopen",
                        return_value=_Response()):
            yield

    def test_a_paginated_pull_reports_one_call_per_page_per_object(self):
        adapter = self._adapter()
        with self._transport({"QueryResponse": {"Invoice": [{"Id": "1"}]}}):
            list(adapter.query("Invoice"))
            list(adapter.query("Bill"))
        self.assertEqual(adapter.call_report(),
                         {"total": 2, "by_object": {"Bill": 1, "Invoice": 1}})

    def test_the_count_is_attributed_to_the_object_not_the_query_endpoint(self):
        adapter = self._adapter()
        with self._transport({"QueryResponse": {"Customer": []}}):
            list(adapter.query("Customer"))
        self.assertEqual(adapter.api_calls, {"Customer": 1})
        self.assertEqual(adapter.api_call_count, 1)


if __name__ == "__main__":
    unittest.main()
