"""Business-time rules for a Canadian service promise operated from India.

Every timestamp in the database is UTC. Everything an operator or a client
reasons about — "due Friday", "3 days overdue", "five business days" — is a
*Canadian business day*, because that is what shimline.ca promises. Computing
those in UTC quietly shifts the answer by a day for anyone working outside
UTC, so the conversion lives here and nowhere else.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import holidays

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# End of the working day a deliverable is promised by, in business-local time.
BUSINESS_DAY_END = time(17, 0)

_TZ_NAME = os.environ.get("SHIMLINE_BUSINESS_TZ", "America/Toronto")
_PROVINCE = os.environ.get("SHIMLINE_BUSINESS_PROVINCE", "ON").strip().upper()

try:
    BUSINESS_TZ = ZoneInfo(_TZ_NAME)
except (ZoneInfoNotFoundError, ValueError):
    # A host without the tz database must not take the service down or, worse,
    # silently produce Toronto-looking dates that are really UTC.
    print(f"[warn] timezone {_TZ_NAME!r} unavailable; business dates fall back to UTC")
    BUSINESS_TZ = timezone.utc


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    """Read any timestamp this codebase has ever written, as aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            try:
                parsed = datetime.strptime(raw[:19], TIMESTAMP_FORMAT)
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    """Render for storage: UTC, in the format the rest of the schema uses."""
    return value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def to_business(value: datetime) -> datetime:
    return value.astimezone(BUSINESS_TZ)


def business_date(value: datetime) -> date:
    """The calendar date this instant falls on for a Canadian client."""
    return to_business(value).date()


@lru_cache(maxsize=16)
def _statutory_holidays(year: int):
    """Province-aware Canadian holidays for the client-facing SLA calendar.

    Falls back to the federal calendar if the configured province is not one
    `holidays` recognises. settings.py rejects a bad value at boot, but this
    module reads the environment directly and is imported by scripts and
    timers that never construct Settings -- and a typo in an env var must not
    take down date arithmetic that the intake path, the admin queue, and a
    published five-business-day promise all depend on.

    Falling back widens the calendar rather than narrowing it: a federal-only
    calendar has fewer holidays, so an SLA date computed from it is earlier,
    never later than promised. Erring toward the earlier date is the safe
    direction for a service commitment.
    """
    try:
        return holidays.country_holidays("CA", subdiv=_PROVINCE, years=[year])
    except (NotImplementedError, KeyError, ValueError):
        print(f"[warn] SHIMLINE_BUSINESS_PROVINCE={_PROVINCE!r} is not a Canadian "
              "subdivision; SLA dates fall back to the federal holiday calendar")
        return holidays.country_holidays("CA", years=[year])


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _statutory_holidays(day.year)


def add_business_days(start: datetime, days: int = 5) -> datetime:
    """The end of the Nth business day after `start`, in business-local time.

    Counting starts the day after `start`, so work marked ready on Monday is
    due at the close of the following Monday, not the preceding Friday
    evening. Province-aware statutory holidays never consume an SLA day.
    """
    current = business_date(start)
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if is_business_day(current):
            remaining -= 1
    due_local = datetime.combine(current, BUSINESS_DAY_END, tzinfo=BUSINESS_TZ)
    return due_local.astimezone(timezone.utc)


def days_until(target: datetime, reference: datetime | None = None) -> int:
    """Whole business-local calendar days from `reference` to `target`.

    Comparing dates rather than 24-hour spans is what makes "due today" mean
    today and "1 day overdue" mean yesterday, regardless of clock time.
    """
    reference = reference or now()
    return (business_date(target) - business_date(reference)).days


def business_days_between(start: datetime, end: datetime) -> int:
    """Business days elapsed between two instants, used for SLA reporting."""
    first, last = sorted((business_date(start), business_date(end)))
    count = 0
    cursor = first
    while cursor < last:
        cursor += timedelta(days=1)
        if is_business_day(cursor):
            count += 1
    return count
