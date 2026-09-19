"""The probe that makes the owner's ten minutes produce a complete answer.

These run against `qbo_conformance`, so the probe is exercised over real HTTP
through the production adapter rather than against a dict. That is the point: the
probe's whole job is to survive a real pull and report what it found, including
when the pull dies.
"""
import copy
import unittest

import app as service
import qbo_conformance
from shimline import first_contact
from shimline.first_contact import FAILED, HELD, UNKNOWN
from test_qbo_conformance import ConformanceTestCase


def verdict(result, name):
    return next(item.verdict for item in result.assumptions if item.name == name)


def detail(result, name):
    return next(item.detail for item in result.assumptions if item.name == name)


class ProbeTests(ConformanceTestCase):

    def probe(self, objects, **server):
        with qbo_conformance.ConformanceServer(objects=objects, **server) as running:
            return first_contact.probe(self.adapter(running))

    def test_a_healthy_file_probes_clean_and_reaches_a_ledger(self):
        result = self.probe(qbo_conformance.documented_company())
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.ledger_complete)
        self.assertEqual(result.refusals, [])
        self.assertEqual(result.documents_refused, 0)
        self.assertEqual(result.counted(FAILED), 0,
                         [item for item in result.assumptions
                          if item.verdict == FAILED])
        self.assertEqual(result.api_calls, 21)

    def test_it_records_the_shape_a_real_company_returns(self):
        """The fields the provider actually sends, which is wider than the
        reader and is the thing nobody has ever been able to look at."""
        result = self.probe(qbo_conformance.documented_company())
        self.assertIn("AccountSubType", result.shapes["Account"])
        self.assertIn("Classification", result.shapes["Account"])
        self.assertIn("TxnTaxDetail", result.shapes["Invoice"])
        self.assertEqual(result.rows["TaxAgency"], 1)

    def test_a_pull_that_dies_is_recorded_and_names_the_entity(self):
        """The single most valuable thing this could discover, so it must not be
        lost to an exception."""
        result = self.probe(
            qbo_conformance.documented_company(),
            faults=qbo_conformance.Faults(entity_errors={"TaxAgency": 400}))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_at, "TaxAgency")
        self.assertIn("TaxAgency", result.failure)
        self.assertFalse(result.ledger_complete)

    def test_a_purchase_with_no_account_is_reported_against_the_assumption(self):
        objects = copy.deepcopy(qbo_conformance.documented_company())
        objects["Purchase"][0].pop("AccountRef")
        result = self.probe(objects)
        self.assertEqual(
            verdict(result, "Every Purchase names the account that paid it"),
            FAILED)
        self.assertFalse(result.ledger_complete)
        self.assertEqual(result.documents_refused, 1)

    def test_a_reference_to_an_account_absent_from_the_chart_is_counted(self):
        result = self.probe(qbo_conformance.company_with_missing_account_reference())
        name = "Every referenced account came back in the pull"
        self.assertEqual(verdict(result, name), FAILED)
        self.assertIn("1 reference points at 1 id absent", detail(result, name))
        self.assertIn("ARCHIVED-64", detail(result, name))
        self.assertFalse(result.ledger_complete)
        self.assertEqual(result.documents_refused, 1)

    def test_named_references_are_reported_per_entity(self):
        objects = copy.deepcopy(qbo_conformance.documented_company())
        objects["Invoice"][0]["CustomerRef"] = {"value": "ARCHIVED-CUSTOMER"}
        objects["Purchase"][0]["EntityRef"] = {
            "value": "ARCHIVED-VENDOR", "type": "Vendor"}
        objects["Invoice"][0]["Line"][0]["SalesItemLineDetail"]["ClassRef"] = {
            "value": "ARCHIVED-CLASS"}
        result = self.probe(objects)
        for label, missing in (("customer", "ARCHIVED-CUSTOMER"),
                               ("vendor", "ARCHIVED-VENDOR"),
                               ("class", "ARCHIVED-CLASS")):
            name = f"Every referenced {label} came back in the pull"
            self.assertEqual(verdict(result, name), FAILED)
            self.assertIn(missing, detail(result, name))

    def test_a_second_receivable_account_is_reported_before_it_blocks_anything(self):
        objects = copy.deepcopy(qbo_conformance.documented_company())
        objects["Account"].append(
            {"Id": "85", "Name": "A/R - retainage",
             "AccountType": "Accounts Receivable", "Active": True})
        result = self.probe(objects)
        self.assertEqual(verdict(result, "The chart has exactly one A/R account"),
                         FAILED)
        self.assertIn("2 Accounts Receivable", detail(result, "The chart has exactly one A/R account"))

    def test_a_foreign_currency_document_is_named_as_the_reason_for_blocking(self):
        objects = copy.deepcopy(qbo_conformance.documented_company())
        objects["Bill"][0].update({"CurrencyRef": {"value": "USD"},
                                   "ExchangeRate": 1.37})
        result = self.probe(objects)
        self.assertEqual(verdict(result, "The file is single-currency"), FAILED)
        self.assertFalse(result.ledger_complete)
        self.assertIn("USD", " ".join(result.refusals))

    def test_a_refused_purchase_measures_checks_quarantine_could_preserve(self):
        objects = copy.deepcopy(qbo_conformance.documented_company())
        objects["Purchase"][0].pop("AccountRef")
        result = self.probe(objects)
        self.assertTrue(result.quarantine_evaluable)
        self.assertEqual(result.quarantine_documents, 1)
        preserved = {item["id"] for item in result.quarantine_checks}
        self.assertEqual(preserved, {"01", "03", "13"})

    def test_a_manifest_or_unknown_type_gap_is_not_called_quarantinable(self):
        objects = copy.deepcopy(qbo_conformance.documented_company())
        objects["MysteryLedgerThing"] = [{"Id": "M1"}]
        result = self.probe(objects)
        self.assertFalse(result.quarantine_evaluable)
        self.assertEqual(result.quarantine_checks, [])


class UnknownIsNotAPassTests(ConformanceTestCase):
    """An assumption nothing in the file could settle is `unknown`.

    Recording it as held would manufacture evidence, which is the same defect as
    a readiness checklist step that is ticked on day one.
    """

    def test_a_file_with_no_taxed_document_says_nothing_about_tax_lines(self):
        objects = {"Account": copy.deepcopy(
            qbo_conformance.documented_company()["Account"])}
        with qbo_conformance.ConformanceServer(objects=objects) as running:
            result = first_contact.probe(self.adapter(running))
        self.assertEqual(verdict(result, "TxnTaxDetail.TaxLine carries an Amount"),
                         UNKNOWN)
        self.assertEqual(verdict(result, "Exactly one tax-bearing liability account"),
                         UNKNOWN)
        self.assertEqual(
            verdict(result, "Every Purchase names the account that paid it"),
            UNKNOWN)

    def test_an_empty_file_judges_only_what_an_empty_file_can_settle(self):
        with qbo_conformance.ConformanceServer(objects={}) as running:
            result = first_contact.probe(self.adapter(running))
        # An entity that returns no rows is still an entity that answered, so the
        # two queryability checks hold -- that is the fact worth recording about
        # TaxAgency, which was added from documentation. Everything else needs a
        # document to settle it and stays unknown.
        self.assertEqual(result.counted(HELD), 2)
        self.assertEqual(result.counted(FAILED), 0)
        self.assertGreater(result.counted(UNKNOWN), 9)

    def test_the_queryability_checks_are_always_judged(self):
        """These two need no document to settle: either the entity came back or
        the pull did not happen."""
        result = self.probe_company()
        for name in ("TaxRate is queryable", "TaxAgency is queryable"):
            self.assertEqual(verdict(result, name), HELD, name)

    def probe_company(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company()) as running:
            return first_contact.probe(self.adapter(running))


class RefusalRateTests(ConformanceTestCase):
    """The probe measures p, which the blocking estimate has only guessed at."""

    def test_the_refusal_rate_is_documents_refused_over_documents_seen(self):
        objects = copy.deepcopy(qbo_conformance.documented_company(purchases=9))
        objects["Purchase"][0].pop("AccountRef")
        objects["Purchase"][1].pop("AccountRef")
        with qbo_conformance.ConformanceServer(objects=objects) as running:
            result = first_contact.probe(self.adapter(running))
        # 9 purchases plus one each of the other eleven posting types.
        self.assertEqual(result.documents_seen, 20)
        self.assertEqual(result.documents_refused, 2)
        self.assertAlmostEqual(result.refusal_rate, 2 / 20)

    def test_a_clean_file_has_a_rate_of_zero_and_does_not_divide_by_zero(self):
        with qbo_conformance.ConformanceServer(objects={}) as running:
            result = first_contact.probe(self.adapter(running))
        self.assertEqual(result.documents_seen, 0)
        self.assertEqual(result.refusal_rate, 0.0)


class PersistenceTests(ConformanceTestCase):

    def test_a_probe_is_stored_encrypted_and_read_back(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company()) as running:
            adapter = self.adapter(running)
            result = first_contact.probe(adapter)
        conn = self.open_db()
        first_contact.record(conn, connection_id=self.connection_id,
                             organization_id=self.organization_id, result=result)
        conn.commit()

        stored = conn.execute(
            "SELECT payload FROM connection_probes").fetchone()[0]
        self.assertNotIn("TaxAgency", str(stored))

        read = first_contact.latest(conn, self.organization_id)
        self.assertEqual(read["status"], "ok")
        self.assertTrue(read["ledger_complete"])
        self.assertEqual(read["rows"]["TaxAgency"], 1)
        self.assertEqual(read["api_calls"], 21)
        self.assertIn("quarantine_checks", read)

    def test_a_failed_probe_is_kept_because_the_failure_is_the_finding(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(),
                faults=qbo_conformance.Faults(
                    entity_errors={"TaxRate": 400})) as running:
            result = first_contact.probe(self.adapter(running))
        conn = self.open_db()
        first_contact.record(conn, connection_id=self.connection_id,
                             organization_id=self.organization_id, result=result)
        conn.commit()
        read = first_contact.latest(conn, self.organization_id)
        self.assertEqual(read["status"], "failed")
        self.assertEqual(read["failed_at"], "TaxRate")

    def test_no_probe_for_a_client_reads_as_none_not_as_an_empty_one(self):
        conn = self.open_db()
        self.assertIsNone(first_contact.latest(conn, self.organization_id))

    def test_an_active_connection_with_no_probe_is_queued(self):
        conn = self.open_db()
        self.assertEqual(first_contact.needs_probe(conn),
                         [(self.connection_id, self.organization_id)])

    def test_a_probed_connection_is_not_queued_again(self):
        conn = self.open_db()
        first_contact.record(conn, connection_id=self.connection_id,
                             organization_id=self.organization_id,
                             result=first_contact.Probe(status="ok"))
        conn.commit()
        self.assertEqual(first_contact.needs_probe(conn), [])

    def test_a_connection_whose_probe_failed_is_tried_again(self):
        """A pull that died on a transient 500 must not leave a client with no
        probe forever; a pull that dies every time will say so every time."""
        conn = self.open_db()
        first_contact.record(conn, connection_id=self.connection_id,
                             organization_id=self.organization_id,
                             result=first_contact.Probe(status="failed",
                                                        failed_at="TaxAgency"))
        conn.commit()
        self.assertEqual(first_contact.needs_probe(conn),
                         [(self.connection_id, self.organization_id)])


class ReportPageTests(ConformanceTestCase):
    """A feature nothing can reach is not shipped, so the page is exercised."""

    def signed_in(self):
        from fastapi.testclient import TestClient

        from shimline import auth
        conn = self.open_db()
        auth.create_user(conn, "op@example.invalid", "Op",
                         "local-test-password-only", "owner")
        conn.commit()
        client = TestClient(service.app)
        response = client.post("/admin/login",
                               data={"email": "op@example.invalid",
                                     "password": "local-test-password-only"},
                               follow_redirects=False)
        self.assertIn(response.status_code, (303, 302))
        return client

    def test_a_client_with_no_probe_is_told_it_runs_on_its_own(self):
        page = self.signed_in().get(
            f"/admin/clients/{self.organization_id}/first-contact")
        self.assertEqual(page.status_code, 200)
        self.assertIn("No probe has run", page.text)

    def test_the_page_shows_every_assumption_and_the_refusals_in_order(self):
        objects = copy.deepcopy(qbo_conformance.documented_company())
        objects["Purchase"][0].pop("AccountRef")
        with qbo_conformance.ConformanceServer(objects=objects) as running:
            result = first_contact.probe(self.adapter(running))
        conn = self.open_db()
        first_contact.record(conn, connection_id=self.connection_id,
                             organization_id=self.organization_id, result=result)
        conn.commit()

        page = self.signed_in().get(
            f"/admin/clients/{self.organization_id}/first-contact")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Every Purchase names the account that paid it", page.text)
        self.assertIn("failed", page.text)
        # The refusal itself, not just a count of them.
        self.assertIn("Purchase 900", page.text)
        # And the shape a real company returned.
        self.assertIn("AccountSubType", page.text)

    def test_the_page_shows_the_quarantine_counterfactual_without_applying_it(self):
        objects = copy.deepcopy(qbo_conformance.documented_company())
        objects["Purchase"][0].pop("AccountRef")
        with qbo_conformance.ConformanceServer(objects=objects) as running:
            result = first_contact.probe(self.adapter(running))
        conn = self.open_db()
        first_contact.record(conn, connection_id=self.connection_id,
                             organization_id=self.organization_id, result=result)
        conn.commit()
        page = self.signed_in().get(
            f"/admin/clients/{self.organization_id}/first-contact")
        self.assertIn("What quarantine could preserve", page.text)
        self.assertIn("3 of the published 16 checks", page.text)
        self.assertIn("production engine remains fail-closed", page.text)

    def test_a_failed_pull_names_the_entity_on_the_page(self):
        with qbo_conformance.ConformanceServer(
                objects=qbo_conformance.documented_company(),
                faults=qbo_conformance.Faults(
                    entity_errors={"TaxAgency": 400})) as running:
            result = first_contact.probe(self.adapter(running))
        conn = self.open_db()
        first_contact.record(conn, connection_id=self.connection_id,
                             organization_id=self.organization_id, result=result)
        conn.commit()
        page = self.signed_in().get(
            f"/admin/clients/{self.organization_id}/first-contact")
        self.assertIn("The pull did not finish", page.text)
        self.assertIn("TaxAgency", page.text)


if __name__ == "__main__":
    unittest.main()
