"""When a client's GST/HST periods begin, end, and fall due.

A filing period is not a calendar quarter. The reporting frequency is assigned
by the CRA or elected by the registrant, and the fiscal year end is the
client's own -- so an annual filer with a June 30 year end files July to June,
and a quarterly filer with that year end files quarters starting in July.
Assuming January would put a return on a period the client does not file, which
is a filing error wearing the clothes of an ordinary return.

Two kinds of arithmetic, and only one of them is safe
-----------------------------------------------------
**Period boundaries are calendar arithmetic.** Given a frequency and a year end
they are computable with no further facts, and they are computed here.

**Due dates are a rule with exceptions**, and one exception matters:

* monthly and quarterly returns are due one month after the period ends;
* an annual return is due three months after the fiscal year end -- *unless* the
  registrant is an individual whose year end is December 31, in which case the
  return is due June 15 and the payment April 30.

That last case depends on a fact about the client, not about the calendar. When
nobody has recorded whether the registrant is an individual, this returns no due
date rather than a plausible one. A date that is three months wrong produces a
late-filing penalty, and it would look entirely ordinary until the letter
arrived.
"""
from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

MONTHLY = "monthly"
QUARTERLY = "quarterly"
ANNUAL = "annual"

FREQUENCIES = (MONTHLY, QUARTERLY, ANNUAL)
MONTHS_IN_PERIOD = {MONTHLY: 1, QUARTERLY: 3, ANNUAL: 12}

# How the registrant calculates net tax. The Quick Method changes the return's
# arithmetic; this product deliberately does not implement it yet. Keeping the
# method on the filing arrangement lets the return lane refuse both an unknown
# method and a known-unsupported one instead of silently running the regular
# method for everybody.
REGULAR_METHOD = "regular"
QUICK_METHOD = "quick"
CALCULATION_METHODS = (REGULAR_METHOD, QUICK_METHOD)

# The reporting period the CRA assigns, by annual taxable supplies, and the more
# frequent periods a registrant may elect instead.
#
#   $1,500,000 or less   annual      (may elect monthly or quarterly)
#   $1,500,000-$6,000,000 quarterly  (may elect monthly)
#   over $6,000,000      monthly     (no election)
#
# Source: Reporting requirements and deadlines, and RC4022 General Information
# for GST/HST Registrants. Both read 2026-09-12.
# https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/
#   gst-hst-businesses/file-gst-hst-return/reporting-requirements-deadlines.html
QUARTERLY_THRESHOLD = 1_500_000
MONTHLY_THRESHOLD = 6_000_000


def assigned_frequency(annual_taxable_supplies) -> str:
    """The reporting period the CRA assigns at this level of taxable supplies.

    This is *not* used to fill in a frequency nobody recorded. A registrant may
    elect a more frequent period than the one assigned, so a client filing
    monthly on $400,000 of supplies is entirely ordinary and guessing "annual"
    for them would prepare a return for a period they do not file. It exists to
    answer the other question -- whether a recorded frequency is one the CRA
    would permit at all -- which `election_is_available` decides.
    """
    supplies = float(annual_taxable_supplies)
    if supplies < 0:
        raise ValueError("taxable supplies cannot be negative")
    if supplies > MONTHLY_THRESHOLD:
        return MONTHLY
    if supplies > QUARTERLY_THRESHOLD:
        return QUARTERLY
    return ANNUAL


def election_is_available(frequency: str, annual_taxable_supplies) -> bool:
    """Whether a registrant at this size may file on this frequency.

    Elections only ever go *more* frequent. Over $6,000,000 there is no election
    at all, so a client that size recorded as a quarterly filer is either
    mis-recorded or is filing late every quarter -- and the difference matters
    enough to surface rather than assume.
    """
    if frequency not in FREQUENCIES:
        raise FilingUnknown(f"Unknown filing frequency {frequency!r}")
    assigned = assigned_frequency(annual_taxable_supplies)
    allowed = {ANNUAL: {ANNUAL, QUARTERLY, MONTHLY},
               QUARTERLY: {QUARTERLY, MONTHLY},
               MONTHLY: {MONTHLY}}[assigned]
    return frequency in allowed


class FilingUnknown(ValueError):
    """The client's filing arrangement has not been recorded."""


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    frequency: str
    # None when the rule depends on a fact about the client that nobody has
    # recorded. Never a guess: three months out is a penalty.
    return_due: date | None = None
    payment_due: date | None = None
    due_note: str = ""

    @property
    def label(self) -> str:
        if self.frequency == ANNUAL:
            return f"{self.start.isoformat()} to {self.end.isoformat()}"
        return f"{self.start.isoformat()} to {self.end.isoformat()}"

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class Arrangement:
    frequency: str
    year_end_month: int
    year_end_day: int
    is_individual: bool | None = None
    gst_number: str = ""
    calculation_method: str = ""

    def year_end_in(self, year: int) -> date:
        """The client's year end in a given calendar year, clamped to the month.

        A February 30 year end does not exist, and a client who entered one
        should get the last day of February rather than an exception in the
        middle of preparing a return.
        """
        last = calendar.monthrange(year, self.year_end_month)[1]
        return date(year, self.year_end_month, min(self.year_end_day, last))


def _add_months(day: date, months: int) -> date:
    """Shift by whole months, staying inside the target month.

    A year end that falls on the last day of its month means the last day of
    every period's month, not the same numbered day. A June 30 year end has
    quarters ending March 31 and December 31 -- keeping the 30th would produce
    March 30, which is not a quarter end and is a day short of the client's
    actual period. This is how a fiscal calendar works, and getting it wrong
    puts a return on a period the client does not file.
    """
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    last_of_target = calendar.monthrange(year, month)[1]
    if day.day == calendar.monthrange(day.year, day.month)[1]:
        return date(year, month, last_of_target)
    return date(year, month, min(day.day, last_of_target))


def periods_for(arrangement: Arrangement, *, containing: date) -> Period:
    """The filing period a given day falls in."""
    for period in periods_between(
            arrangement,
            start=_add_months(containing, -13),
            end=_add_months(containing, 13)):
        if period.covers(containing):
            return period
    raise FilingUnknown(
        f"No {arrangement.frequency} period contains {containing.isoformat()}")


def periods_between(arrangement: Arrangement, *, start: date,
                    end: date) -> list[Period]:
    """Every filing period overlapping a span, in order.

    Periods are laid out from the fiscal year end backwards, which is what makes
    a June 30 year end produce July-September quarters rather than
    January-March.
    """
    if arrangement.frequency not in FREQUENCIES:
        raise FilingUnknown(f"Unknown filing frequency {arrangement.frequency!r}")
    step = MONTHS_IN_PERIOD[arrangement.frequency]

    # Walk back from a year end comfortably after the span, then forward.
    anchor = arrangement.year_end_in(end.year + 1)
    while anchor > _add_months(end, 12):
        anchor = arrangement.year_end_in(anchor.year - 1)

    # Every boundary is measured from the anchor, never from the previous
    # boundary. Chaining `_add_months` by one step at a time degrades the day:
    # a December 31 year end walks back to November 30 and then to October 30,
    # losing a day each time it passes a short month, and by the third quarter
    # the periods no longer line up with the client's year at all.
    boundaries: list[date] = []
    offset = 0
    floor = _add_months(start, -24)
    while True:
        cursor = _add_months(anchor, -offset * step)
        boundaries.append(cursor)
        if cursor < floor:
            break
        offset += 1
    boundaries.reverse()

    found = []
    for index in range(1, len(boundaries)):
        period_end = boundaries[index]
        period_start = boundaries[index - 1] + timedelta(days=1)
        if period_end < start or period_start > end:
            continue
        found.append(_with_due_dates(arrangement, period_start, period_end))
    return found


def _with_due_dates(arrangement: Arrangement, start: date, end: date) -> Period:
    if arrangement.frequency in (MONTHLY, QUARTERLY):
        due = _add_months(end, 1)
        return Period(start=start, end=end, frequency=arrangement.frequency,
                      return_due=due, payment_due=due,
                      due_note="One month after the period ends.")

    # Annual. The individual-with-December-year-end case has two different
    # dates, so the registrant's status has to be known before either is stated.
    december = end.month == 12 and end.day == 31
    if december and arrangement.is_individual is None:
        return Period(
            start=start, end=end, frequency=ANNUAL,
            due_note=("An annual return is due three months after the year end, "
                      "unless the registrant is an individual with a December 31 "
                      "year end -- then the return is due June 15 and the payment "
                      "April 30. Nobody has recorded which this client is, so no "
                      "date is stated rather than one that may be three months "
                      "wrong."))
    if december and arrangement.is_individual:
        return Period(
            start=start, end=end, frequency=ANNUAL,
            return_due=date(end.year + 1, 6, 15),
            payment_due=date(end.year + 1, 4, 30),
            due_note=("An individual with a December 31 year end: the return is "
                      "due June 15 and the payment April 30. The two dates are "
                      "different on purpose."))
    due = _add_months(end, 3)
    return Period(start=start, end=end, frequency=ANNUAL, return_due=due,
                  payment_due=due, due_note="Three months after the year end.")


# ------------------------------------------------------------- persistence --

def arrangement_for(conn: sqlite3.Connection, organization_id: str) -> Arrangement:
    """The client's recorded filing arrangement, or a refusal.

    There is no default. A guessed frequency prepares a return for a period the
    client does not file, and a guessed year end puts it on the wrong months.
    """
    row = conn.execute(
        "SELECT frequency, fiscal_year_end_month, fiscal_year_end_day, "
        "       is_individual, COALESCE(gst_number,''), "
        "       COALESCE(calculation_method,'') "
        "FROM bookkeeping_gst_filing WHERE organization_id=?",
        (organization_id,)).fetchone()
    if not row:
        raise FilingUnknown(
            "This client's GST/HST filing frequency and fiscal year end have "
            "not been recorded, so there is no period to prepare a return for.")
    return Arrangement(
        frequency=str(row[0]), year_end_month=int(row[1]),
        year_end_day=int(row[2]),
        is_individual=None if row[3] is None else bool(row[3]),
        gst_number=str(row[4]), calculation_method=str(row[5]))


def record_arrangement(conn: sqlite3.Connection, organization_id: str, *,
                       frequency: str, year_end_month: int, year_end_day: int,
                       is_individual: bool | None = None,
                       gst_number: str = "",
                       calculation_method: str = "",
                       registered_from: str | None = None) -> None:
    if frequency not in FREQUENCIES:
        raise ValueError(f"A GST/HST filer is {', '.join(FREQUENCIES)}")
    # Validated here rather than left to the CHECK constraint, so an operator
    # gets a sentence instead of an integrity error.
    calendar.monthrange(2026, year_end_month)      # raises on a bad month
    if not 1 <= year_end_day <= 31:
        raise ValueError("A fiscal year end day is between 1 and 31")
    if calculation_method not in CALCULATION_METHODS:
        raise ValueError(
            "Record whether this client uses the regular method or the Quick Method")
    conn.execute(
        "INSERT INTO bookkeeping_gst_filing("
        "organization_id,frequency,fiscal_year_end_month,fiscal_year_end_day,"
        "is_individual,gst_number,calculation_method,registered_from) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(organization_id) DO UPDATE SET frequency=excluded.frequency,"
        "fiscal_year_end_month=excluded.fiscal_year_end_month,"
        "fiscal_year_end_day=excluded.fiscal_year_end_day,"
        "is_individual=excluded.is_individual, gst_number=excluded.gst_number,"
        "calculation_method=excluded.calculation_method,"
        "registered_from=excluded.registered_from, updated_at=CURRENT_TIMESTAMP",
        (organization_id, frequency, year_end_month, year_end_day,
         None if is_individual is None else int(is_individual),
         gst_number or None, calculation_method, registered_from))
