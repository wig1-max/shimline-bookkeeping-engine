"""The GST/HST lane checked against the CRA's own published rules.

Every rule here was read from a primary source on 2026-09-12 and the source is
named in the test. That matters more than usual: this lane produces a figure an
accountant signs, and a rule I merely remember correctly is indistinguishable
from one I remember incorrectly. Where a source could not be read, the test says
so rather than asserting from memory.

Sources:
  [1] Reporting requirements and deadlines -- File your GST/HST return
      canada.ca/en/revenue-agency/services/tax/businesses/topics/
      gst-hst-businesses/file-gst-hst-return/reporting-requirements-deadlines.html
  [2] RC4022 General Information for GST/HST Registrants
      canada.ca/en/revenue-agency/services/forms-publications/publications/rc4022/
  [3] GST/HST rates and place-of-supply rules
      canada.ca/en/revenue-agency/services/tax/businesses/topics/
      gst-hst-businesses/charge-collect-place-supply.html

None of this makes Shimline authorised to act for anyone. Representative
authorisation (RC59) is a separate thing and is not code.
"""
import unittest
from datetime import date

from shimline import filing_periods
from shimline.filing_periods import ANNUAL, MONTHLY, QUARTERLY, Arrangement


def arrangement(frequency, *, month=12, day=31, individual=False):
    return Arrangement(frequency=frequency, year_end_month=month,
                       year_end_day=day, is_individual=individual)


class FilingDeadlines(unittest.TestCase):
    """Source [1], quoted: monthly and quarterly are due "1 month after the end
    of the reporting period"; annual is due "3 months after your fiscal
    year-end"."""

    def test_a_monthly_period_is_due_one_month_after_it_ends(self):
        period = filing_periods.periods_for(arrangement(MONTHLY),
                                            containing=date(2026, 3, 15))
        self.assertEqual(period.start, date(2026, 3, 1))
        self.assertEqual(period.end, date(2026, 3, 31))
        self.assertEqual(period.return_due, date(2026, 4, 30))

    def test_a_quarterly_period_is_due_one_month_after_it_ends(self):
        period = filing_periods.periods_for(arrangement(QUARTERLY),
                                            containing=date(2026, 5, 2))
        self.assertEqual(period.start, date(2026, 4, 1))
        self.assertEqual(period.end, date(2026, 6, 30))
        self.assertEqual(period.return_due, date(2026, 7, 31))

    def test_an_annual_corporate_return_is_due_three_months_after_year_end(self):
        period = filing_periods.periods_for(arrangement(ANNUAL, month=6, day=30),
                                            containing=date(2026, 1, 15))
        self.assertEqual(period.end, date(2026, 6, 30))
        self.assertEqual(period.return_due, date(2026, 9, 30))

    def test_an_individual_with_a_december_year_end_files_june_15_and_pays_april_30(self):
        """Source [1]: an individual with a business, a December 31 fiscal year
        end and business income that year files by June 15 and pays by April 30.

        Two different dates for one return. It is the only place in this lane
        where filing and paying diverge, which is exactly why it is recorded
        rather than derived."""
        period = filing_periods.periods_for(arrangement(ANNUAL, individual=True),
                                            containing=date(2026, 7, 1))
        self.assertEqual(period.end, date(2026, 12, 31))
        self.assertEqual(period.return_due, date(2027, 6, 15))
        self.assertEqual(period.payment_due, date(2027, 4, 30))

    def test_an_annual_corporate_filer_pays_when_it_files(self):
        """The exception above is for individuals. A corporation with the same
        December year end has one date, not two."""
        period = filing_periods.periods_for(arrangement(ANNUAL),
                                            containing=date(2026, 7, 1))
        self.assertEqual(period.return_due, date(2027, 3, 31))
        self.assertEqual(period.payment_due, period.return_due)


class AssignedReportingPeriods(unittest.TestCase):
    """Source [1] and [2]: $1,500,000 or less is assigned annual; over that to
    $6,000,000 is quarterly; over $6,000,000 is monthly."""

    def test_the_thresholds_are_the_published_ones(self):
        self.assertEqual(filing_periods.QUARTERLY_THRESHOLD, 1_500_000)
        self.assertEqual(filing_periods.MONTHLY_THRESHOLD, 6_000_000)

    def test_each_band_gets_the_period_the_cra_assigns(self):
        for supplies, expected in ((0, ANNUAL),
                                   (1_499_999, ANNUAL),
                                   (1_500_000, ANNUAL),
                                   (1_500_001, QUARTERLY),
                                   (6_000_000, QUARTERLY),
                                   (6_000_001, MONTHLY),
                                   (40_000_000, MONTHLY)):
            with self.subTest(supplies=supplies):
                self.assertEqual(
                    filing_periods.assigned_frequency(supplies), expected)

    def test_the_boundaries_are_at_or_below_not_below(self):
        """"$1,500,000 or less" puts exactly 1,500,000 in the annual band. An
        off-by-one here moves a client onto a filing frequency the CRA did not
        assign them."""
        self.assertEqual(filing_periods.assigned_frequency(1_500_000), ANNUAL)
        self.assertEqual(filing_periods.assigned_frequency(6_000_000), QUARTERLY)

    def test_negative_supplies_are_refused_rather_than_banded(self):
        with self.assertRaises(ValueError):
            filing_periods.assigned_frequency(-1)


class Elections(unittest.TestCase):
    """A registrant may elect a *more* frequent period than the assigned one."""

    def test_a_small_registrant_may_file_as_often_as_they_like(self):
        for frequency in (ANNUAL, QUARTERLY, MONTHLY):
            with self.subTest(frequency=frequency):
                self.assertTrue(
                    filing_periods.election_is_available(frequency, 400_000))

    def test_a_mid_sized_registrant_cannot_elect_annual(self):
        self.assertFalse(filing_periods.election_is_available(ANNUAL, 2_000_000))
        self.assertTrue(filing_periods.election_is_available(QUARTERLY, 2_000_000))
        self.assertTrue(filing_periods.election_is_available(MONTHLY, 2_000_000))

    def test_over_six_million_there_is_no_election_at_all(self):
        """A client this size recorded as quarterly is either mis-recorded or is
        filing late every quarter, and those need different responses."""
        self.assertTrue(filing_periods.election_is_available(MONTHLY, 8_000_000))
        for frequency in (ANNUAL, QUARTERLY):
            with self.subTest(frequency=frequency):
                self.assertFalse(
                    filing_periods.election_is_available(frequency, 8_000_000))

    def test_an_unrecognised_frequency_is_refused_not_allowed(self):
        with self.assertRaises(filing_periods.FilingUnknown):
            filing_periods.election_is_available("fortnightly", 100)

    def test_nothing_here_fills_in_a_frequency_nobody_recorded(self):
        """The assigned period is not a default. A client filing monthly on
        $400,000 is ordinary, so guessing "annual" for them would prepare a
        return for a period they do not file."""
        self.assertFalse(hasattr(filing_periods, "default_frequency"))
        source = filing_periods.assigned_frequency.__doc__ or ""
        self.assertIn("not", source.lower())


class PublishedRates(unittest.TestCase):
    """Source [3], read 2026-09-12.

    Recorded as a reference table, deliberately not wired into a check yet. The
    useful check -- flagging a rate whose percentage is not one the CRA
    publishes for anywhere in Canada -- needs effective dates applied per
    document date, and Nova Scotia is precisely why: it moved from 15% to 14% on
    2025-04-01, so the same "HST NS" rate is right before that date and wrong
    after it. Doing that carelessly would flag every historical document.
    """

    PUBLISHED = {
        "AB": 5, "BC": 5, "MB": 5, "SK": 5, "QC": 5,
        "YT": 5, "NT": 5, "NU": 5,
        "ON": 13,
        "NS": 14,   # 15% before 2025-04-01
        "NB": 15, "PE": 15, "NL": 15,
    }
    NOVA_SCOTIA_CHANGED = date(2025, 4, 1)

    def test_every_province_and_territory_is_accounted_for(self):
        self.assertEqual(len(self.PUBLISHED), 13)

    def test_the_only_rates_in_canada_are_five_thirteen_fourteen_and_fifteen(self):
        self.assertEqual(sorted(set(self.PUBLISHED.values())), [5, 13, 14, 15])

    def test_nova_scotia_is_fourteen_not_fifteen(self):
        """The most recent change, and the one a hardcoded table gets wrong."""
        self.assertEqual(self.PUBLISHED["NS"], 14)
        self.assertEqual(self.NOVA_SCOTIA_CHANGED, date(2025, 4, 1))

    def test_quebec_is_five_because_qst_is_not_on_this_return(self):
        """QST is administered by Revenu Quebec and is not a GST/HST return
        line. A rate table that folded it in would overstate line 105."""
        self.assertEqual(self.PUBLISHED["QC"], 5)


if __name__ == "__main__":
    unittest.main()
