"""Filing periods, and the due dates that are not calendar arithmetic.

A period on the wrong months is a filing error wearing the clothes of an
ordinary return, and a due date three months out is a late-filing penalty that
looks fine until the letter arrives.
"""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import filing_periods  # noqa: E402
from shimline.filing_periods import (ANNUAL, MONTHLY, QUARTERLY,  # noqa: E402
                                     Arrangement, FilingUnknown)


def arrangement(frequency, month=12, day=31, individual=None):
    return Arrangement(frequency=frequency, year_end_month=month,
                       year_end_day=day, is_individual=individual)


class TheBoundaries(unittest.TestCase):

    def test_a_december_year_end_gives_calendar_quarters(self):
        period = filing_periods.periods_for(arrangement(QUARTERLY),
                                            containing=date(2026, 8, 15))
        self.assertEqual((period.start, period.end),
                         (date(2026, 7, 1), date(2026, 9, 30)))

    def test_a_june_year_end_moves_the_quarters(self):
        """A quarterly filer with a June 30 year end files July-September, not
        January-March. Assuming the calendar would put the return on a period
        the client does not file."""
        period = filing_periods.periods_for(arrangement(QUARTERLY, month=6, day=30),
                                            containing=date(2026, 8, 15))
        self.assertEqual((period.start, period.end),
                         (date(2026, 7, 1), date(2026, 9, 30)))
        earlier = filing_periods.periods_for(arrangement(QUARTERLY, month=6, day=30),
                                             containing=date(2026, 5, 15))
        self.assertEqual((earlier.start, earlier.end),
                         (date(2026, 4, 1), date(2026, 6, 30)))

    def test_a_monthly_filer_gets_one_month(self):
        period = filing_periods.periods_for(arrangement(MONTHLY),
                                            containing=date(2026, 2, 10))
        self.assertEqual((period.start, period.end),
                         (date(2026, 2, 1), date(2026, 2, 28)))

    def test_an_annual_filer_with_a_june_year_end_files_july_to_june(self):
        period = filing_periods.periods_for(arrangement(ANNUAL, month=6, day=30),
                                            containing=date(2026, 9, 1))
        self.assertEqual((period.start, period.end),
                         (date(2026, 7, 1), date(2027, 6, 30)))

    def test_periods_are_contiguous_and_do_not_overlap(self):
        found = filing_periods.periods_between(
            arrangement(QUARTERLY, month=6, day=30),
            start=date(2026, 1, 1), end=date(2026, 12, 31))
        self.assertGreaterEqual(len(found), 4)
        for earlier, later in zip(found, found[1:]):
            self.assertEqual((later.start - earlier.end).days, 1,
                             "a day must not fall between two periods or in both")

    def test_a_february_year_end_day_is_clamped_rather_than_crashing(self):
        """A client who entered the 30th should get the end of February, not an
        exception in the middle of preparing a return."""
        period = filing_periods.periods_for(arrangement(ANNUAL, month=2, day=30),
                                            containing=date(2026, 6, 1))
        self.assertEqual(period.end, date(2027, 2, 28))

    def test_a_month_end_year_end_gives_month_end_boundaries(self):
        """The bug this pins: computing each boundary from the previous one
        walked a December 31 year end back to November 30 and then October 30,
        losing a day at every short month. Three quarters later the periods no
        longer lined up with the client's year at all."""
        found = filing_periods.periods_between(
            arrangement(QUARTERLY), start=date(2025, 1, 1), end=date(2026, 12, 31))
        ends = {(period.end.month, period.end.day) for period in found}
        self.assertEqual(ends, {(3, 31), (6, 30), (9, 30), (12, 31)})

    def test_a_june_year_end_quarter_ends_on_march_31_not_march_30(self):
        """A quarter end is the last day of its month. March 30 is a day short
        of the client's actual period and is not a quarter end anywhere."""
        found = filing_periods.periods_between(
            arrangement(QUARTERLY, month=6, day=30),
            start=date(2026, 1, 1), end=date(2026, 12, 31))
        ends = {(period.end.month, period.end.day) for period in found}
        self.assertEqual(ends, {(3, 31), (6, 30), (9, 30), (12, 31)})

    def test_a_mid_month_year_end_keeps_its_day(self):
        """A 52-week fiscal year can end mid-month, and that day must survive
        rather than being rounded to the end of the month."""
        found = filing_periods.periods_between(
            arrangement(QUARTERLY, month=6, day=15),
            start=date(2026, 1, 1), end=date(2026, 12, 31))
        self.assertTrue(all(period.end.day == 15 for period in found),
                        [period.end.isoformat() for period in found])

    def test_an_unknown_frequency_is_refused(self):
        with self.assertRaises(FilingUnknown):
            filing_periods.periods_for(arrangement("weekly"),
                                       containing=date(2026, 8, 1))


class TheDueDates(unittest.TestCase):

    def test_monthly_and_quarterly_are_due_one_month_later(self):
        for frequency in (MONTHLY, QUARTERLY):
            with self.subTest(frequency=frequency):
                period = filing_periods.periods_for(
                    arrangement(frequency), containing=date(2026, 8, 15))
                self.assertEqual(period.return_due,
                                 filing_periods._add_months(period.end, 1))
                self.assertEqual(period.payment_due, period.return_due)

    def test_an_annual_filer_is_due_three_months_after_the_year_end(self):
        period = filing_periods.periods_for(arrangement(ANNUAL, month=6, day=30),
                                            containing=date(2026, 9, 1))
        self.assertEqual(period.return_due, date(2027, 9, 30))

    def test_an_individual_with_a_december_year_end_has_two_different_dates(self):
        """The return is due June 15 and the payment April 30. Treating them as
        one date makes the payment two and a half months late."""
        period = filing_periods.periods_for(
            arrangement(ANNUAL, individual=True), containing=date(2026, 6, 1))
        self.assertEqual(period.end, date(2026, 12, 31))
        self.assertEqual(period.return_due, date(2027, 6, 15))
        self.assertEqual(period.payment_due, date(2027, 4, 30))
        self.assertNotEqual(period.return_due, period.payment_due)

    def test_a_corporation_with_a_december_year_end_gets_the_ordinary_rule(self):
        period = filing_periods.periods_for(
            arrangement(ANNUAL, individual=False), containing=date(2026, 6, 1))
        self.assertEqual(period.return_due, date(2027, 3, 31))

    def test_an_unrecorded_registrant_status_gets_no_date_rather_than_a_guess(self):
        """Three months wrong is a penalty, and it would look entirely ordinary
        until the letter arrived."""
        period = filing_periods.periods_for(
            arrangement(ANNUAL, individual=None), containing=date(2026, 6, 1))
        self.assertIsNone(period.return_due)
        self.assertIsNone(period.payment_due)
        self.assertIn("Nobody has recorded", period.due_note)

    def test_a_non_december_annual_filer_does_not_need_the_status(self):
        """The exception only applies to a December 31 year end, so a June filer
        must not be blocked by a fact that cannot affect them."""
        period = filing_periods.periods_for(
            arrangement(ANNUAL, month=6, day=30, individual=None),
            containing=date(2026, 9, 1))
        self.assertIsNotNone(period.return_due)


class ThePersistedArrangement(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "filing.db"
        self.conn = service._db()
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) "
                          "VALUES('org_1','Client','org_1')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_a_client_nobody_has_recorded_is_refused_not_defaulted(self):
        """A guessed frequency prepares a return for a period the client does
        not file."""
        with self.assertRaises(FilingUnknown) as caught:
            filing_periods.arrangement_for(self.conn, "org_1")
        self.assertIn("have not been recorded", str(caught.exception))

    def test_a_recorded_arrangement_comes_back(self):
        filing_periods.record_arrangement(
            self.conn, "org_1", frequency=QUARTERLY, year_end_month=6,
            year_end_day=30, gst_number="123456789RT0001",
            calculation_method=filing_periods.REGULAR_METHOD)
        self.conn.commit()
        found = filing_periods.arrangement_for(self.conn, "org_1")
        self.assertEqual(found.frequency, QUARTERLY)
        self.assertEqual(found.year_end_month, 6)
        self.assertEqual(found.gst_number, "123456789RT0001")
        self.assertIsNone(found.is_individual)
        self.assertEqual(found.calculation_method, filing_periods.REGULAR_METHOD)

    def test_recording_it_twice_updates_rather_than_duplicating(self):
        filing_periods.record_arrangement(
            self.conn, "org_1", frequency=QUARTERLY, year_end_month=12,
            year_end_day=31, calculation_method=filing_periods.REGULAR_METHOD)
        filing_periods.record_arrangement(
            self.conn, "org_1", frequency=MONTHLY, year_end_month=12,
            year_end_day=31, calculation_method=filing_periods.REGULAR_METHOD)
        self.conn.commit()
        self.assertEqual(
            filing_periods.arrangement_for(self.conn, "org_1").frequency, MONTHLY)

    def test_an_impossible_frequency_is_refused_with_a_sentence(self):
        with self.assertRaises(ValueError) as caught:
            filing_periods.record_arrangement(
                self.conn, "org_1", frequency="fortnightly",
                year_end_month=12, year_end_day=31,
                calculation_method=filing_periods.REGULAR_METHOD)
        self.assertIn("monthly", str(caught.exception))

    def test_an_impossible_month_is_refused(self):
        with self.assertRaises((ValueError, Exception)):
            filing_periods.record_arrangement(
                self.conn, "org_1", frequency=QUARTERLY, year_end_month=13,
                year_end_day=1,
                calculation_method=filing_periods.REGULAR_METHOD)

    def test_a_missing_calculation_method_is_refused_with_a_sentence(self):
        with self.assertRaises(ValueError) as caught:
            filing_periods.record_arrangement(
                self.conn, "org_1", frequency=QUARTERLY,
                year_end_month=12, year_end_day=31)
        self.assertIn("regular method or the Quick Method", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
