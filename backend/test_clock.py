"""Business-time rules. The five-day promise is measured in Canadian days."""
import unittest
from datetime import date, datetime, timedelta, timezone

from shimline import clock


class BusinessDayTests(unittest.TestCase):
    def test_five_business_days_from_a_wednesday_lands_the_next_wednesday(self):
        # 2026-09-09 is a Wednesday. Thu, Fri, Mon, Tue, Wed = five business days.
        start = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
        due = clock.add_business_days(start, 5)
        self.assertEqual(clock.business_date(due), date(2026, 9, 16))

    def test_a_weekend_never_counts_as_a_business_day(self):
        friday = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
        due = clock.add_business_days(friday, 1)
        self.assertEqual(clock.business_date(due).weekday(), 0)  # Monday

    def test_due_date_is_the_close_of_the_business_day(self):
        start = datetime(2026, 9, 9, 3, 0, tzinfo=timezone.utc)
        due = clock.to_business(clock.add_business_days(start, 5))
        self.assertEqual((due.hour, due.minute), (17, 0))

    def test_late_utc_evening_is_still_the_same_canadian_business_day(self):
        """23:30 UTC is 19:30 in Toronto — the same day, not tomorrow.

        Counting in UTC would move every evening deadline forward a day for
        anyone working outside UTC, which is the entire reason this exists.
        """
        instant = datetime(2026, 9, 9, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(clock.business_date(instant), date(2026, 9, 9))

    def test_days_until_counts_calendar_days_not_24_hour_spans(self):
        now = datetime(2026, 9, 9, 21, 0, tzinfo=timezone.utc)      # 17:00 Toronto
        tomorrow = datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)  # 09:00 Toronto
        self.assertEqual(clock.days_until(tomorrow, now), 1)
        self.assertEqual(clock.days_until(now, now), 0)
        self.assertEqual(clock.days_until(now - timedelta(days=1), now), -1)

    def test_business_days_between_ignores_the_weekend(self):
        friday = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)
        monday = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(clock.business_days_between(friday, monday), 1)

    def test_ontario_statutory_holiday_does_not_consume_an_sla_day(self):
        monday = datetime(2026, 6, 29, 15, 0, tzinfo=timezone.utc)
        due = clock.add_business_days(monday, 3)
        # Tuesday counts, Canada Day on Wednesday does not, then Thu + Fri.
        self.assertEqual(clock.business_date(due), date(2026, 7, 3))

    def test_canada_day_is_not_a_business_day(self):
        self.assertFalse(clock.is_business_day(date(2026, 7, 1)))

    def test_timestamps_round_trip_through_storage_format(self):
        value = datetime(2026, 9, 9, 12, 34, 56, tzinfo=timezone.utc)
        self.assertEqual(clock.parse_timestamp(clock.format_timestamp(value)), value)
        self.assertIsNone(clock.parse_timestamp(""))
        self.assertIsNone(clock.parse_timestamp("not a date"))


if __name__ == "__main__":
    unittest.main()
