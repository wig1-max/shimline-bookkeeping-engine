"""Sorting a client's tax rates onto the right return.

PST and QST are not on a GST/HST return. Claiming provincial tax as an input tax
credit overstates the credit on a filing made under the client's name, and the
return looks entirely reasonable while it does it. Most of this file is about
refusing to decide rather than deciding wrongly.
"""
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import tax_rates  # noqa: E402
from shimline.tax_rates import GST_HST, PROVINCIAL, UNKNOWN  # noqa: E402


class Classification(unittest.TestCase):
    """The agency is asked first; the name is a fallback, not a guess."""

    def test_the_federal_agency_settles_it(self):
        self.assertEqual(tax_rates.classify("Standard", "Canada Revenue Agency"),
                         (GST_HST, "agency"))
        self.assertEqual(tax_rates.classify("Tax", "Agence du revenu du Canada"),
                         (GST_HST, "agency"))

    def test_a_provincial_agency_settles_it(self):
        self.assertEqual(tax_rates.classify("Standard", "Revenu Québec")[0],
                         PROVINCIAL)
        self.assertEqual(tax_rates.classify("Tax", "Ministry of Finance")[0],
                         PROVINCIAL)

    def test_the_agency_outranks_the_name(self):
        """A client may name a rate anything. The agency is what QuickBooks
        itself uses to decide which return a rate belongs to."""
        self.assertEqual(
            tax_rates.classify("PST BC", "Canada Revenue Agency"),
            (GST_HST, "agency"))

    def test_an_unambiguous_name_settles_it_when_there_is_no_agency(self):
        self.assertEqual(tax_rates.classify("GST"), (GST_HST, "name"))
        self.assertEqual(tax_rates.classify("HST ON"), (GST_HST, "name"))
        self.assertEqual(tax_rates.classify("PST BC"), (PROVINCIAL, "name"))
        self.assertEqual(tax_rates.classify("QST"), (PROVINCIAL, "name"))

    def test_a_vague_name_settles_nothing(self):
        """"Tax", "Sales Tax" and "Standard" are not evidence of anything, and
        treating them as federal would silently claim provincial tax."""
        for name in ("Tax", "Sales Tax", "Standard", "Taxable", ""):
            with self.subTest(name=name):
                self.assertEqual(tax_rates.classify(name), (UNKNOWN, "none"))

    def test_a_combined_rate_is_refused_rather_than_split(self):
        """Part of it belongs on the return and part of it does not, and
        nothing here can separate them."""
        for name in ("GST/PST BC", "HST + PST", "PST and GST"):
            with self.subTest(name=name):
                self.assertEqual(tax_rates.classify(name)[0], UNKNOWN)

    def test_an_unrecognised_agency_does_not_default_to_federal(self):
        """An agency absent from the list becomes a question, not a federal
        rate. Defaulting the other way is the expensive direction."""
        self.assertEqual(
            tax_rates.classify("Standard", "Some Municipal Levy Board")[0], UNKNOWN)


class RegistryCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "rates.db"
        self.conn = service._db()
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) "
                          "VALUES('org_1','Client','org_1')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def persist(self, rates, agencies=()):
        return tax_rates.persist(self.conn, "org_1", {
            "TaxAgency": list(agencies), "TaxRate": list(rates)})

    def registry(self):
        return tax_rates.registry(self.conn, "org_1")


class Persistence(RegistryCase):

    def test_a_rate_is_named_and_classified_from_its_agency(self):
        self.persist(
            [{"Id": "5", "Name": "Standard", "RateValue": "5",
              "AgencyRef": {"value": "1"}, "SyncToken": "0"}],
            [{"Id": "1", "DisplayName": "Canada Revenue Agency"}])
        rate = self.registry()["5"]
        self.assertEqual(rate.classification, GST_HST)
        self.assertEqual(rate.classification_source, "agency")
        self.assertEqual(rate.rate_percent, Decimal("5"))
        self.assertEqual(rate.label(), "Standard 5%")

    def test_a_rate_with_no_percentage_still_labels_itself(self):
        self.persist([{"Id": "9", "Name": "GST"}])
        self.assertEqual(self.registry()["9"].label(), "GST")

    def test_a_whole_percentage_reads_cleanly(self):
        """13.00% would be correct and would look like a mistake on a return."""
        self.persist([{"Id": "7", "Name": "HST ON", "RateValue": "13.00"}])
        self.assertEqual(self.registry()["7"].label(), "HST ON 13%")

    def test_re_reading_the_provider_updates_the_rate(self):
        self.persist([{"Id": "5", "Name": "GST", "RateValue": "5"}])
        self.persist([{"Id": "5", "Name": "GST", "RateValue": "6"}])
        self.assertEqual(self.registry()["5"].rate_percent, Decimal("6"))
        self.assertEqual(len(self.registry()), 1)

    def test_an_unreadable_percentage_is_dropped_rather_than_guessed(self):
        self.persist([{"Id": "5", "Name": "GST", "RateValue": "five"}])
        self.assertIsNone(self.registry()["5"].rate_percent)


class TheOperatorDecision(RegistryCase):

    def test_an_undecided_rate_is_listed_for_somebody_to_settle(self):
        self.persist([{"Id": "5", "Name": "Standard", "RateValue": "5"}])
        pending = tax_rates.undecided(self.conn, "org_1")
        self.assertEqual([item.provider_id for item in pending], ["5"])

    def test_a_decision_classifies_the_rate(self):
        self.persist([{"Id": "5", "Name": "Standard", "RateValue": "5"}])
        tax_rates.decide(self.conn, "org_1", "5", GST_HST,
                         user_id=None, reason="Confirmed with the client")
        rate = self.registry()["5"]
        self.assertEqual(rate.classification, GST_HST)
        self.assertEqual(rate.classification_source, "operator")
        self.assertEqual(tax_rates.undecided(self.conn, "org_1"), [])

    def test_re_reading_the_provider_does_not_undo_a_persons_decision(self):
        """A judgement about which return a rate belongs on must survive a
        re-sync, or it would be quietly reversed by a routine pull."""
        self.persist([{"Id": "5", "Name": "Standard", "RateValue": "5"}])
        tax_rates.decide(self.conn, "org_1", "5", PROVINCIAL)
        self.persist([{"Id": "5", "Name": "Standard", "RateValue": "5"}])
        self.assertEqual(self.registry()["5"].classification, PROVINCIAL)
        self.assertEqual(self.registry()["5"].classification_source, "operator")

    def test_a_decision_can_be_changed(self):
        self.persist([{"Id": "5", "Name": "Standard"}])
        tax_rates.decide(self.conn, "org_1", "5", PROVINCIAL)
        tax_rates.decide(self.conn, "org_1", "5", GST_HST, reason="Corrected")
        self.assertEqual(self.registry()["5"].classification, GST_HST)

    def test_unknown_is_not_a_decision_anyone_can_record(self):
        """Leaving it undecided is the default; choosing to leave it undecided
        is not a thing, because it would look like somebody had looked."""
        self.persist([{"Id": "5", "Name": "Standard"}])
        with self.assertRaises(ValueError):
            tax_rates.decide(self.conn, "org_1", "5", UNKNOWN)


class TheAdapterReadsThem(unittest.TestCase):

    def test_tax_rates_and_agencies_are_pulled(self):
        from shimline.qbo_adapter import READ_OBJECTS
        self.assertIn("TaxRate", READ_OBJECTS)
        self.assertIn("TaxAgency", READ_OBJECTS)

    def test_neither_is_treated_as_posting_to_the_ledger(self):
        """A list appearing in a pull must be classified, or derive_ledger
        blocks every client's books."""
        from shimline import postings
        classified = (set(postings.POSTING_TYPES)
                      | set(postings.UNREAD_POSTING_TYPES)
                      | set(postings.NON_POSTING_TYPES))
        self.assertIn("TaxRate", classified)
        self.assertIn("TaxAgency", classified)


if __name__ == "__main__":
    unittest.main()
