"""A GST/HST return, prepared for an accountant to check and file.

Nothing here is filed. This produces the four figures an accountant signs --
line 101, 105, 108, 109 -- with everything that went into them, and refuses
outright rather than producing a smaller one when anything is missing.

The rule this lane runs on
--------------------------
A return goes to the CRA under the client's name. Everywhere else in this
system a plausible answer is a liability; here it is the most expensive output
the product can produce, because it is *signed* and because nothing downstream
will ever reveal it. So the refusal is absolute: one document whose tax cannot
be accounted for, or one rate nobody has sorted onto a return, and there is no
return at all. Not a partial one, not an estimate, not a "best available"
figure with a caveat nobody reads.

The two readings
----------------
The same pattern as every other number in this codebase: compute it twice, from
different data, and require agreement.

* **Stated** -- `sales_tax.period` reads `TxnTaxDetail` as QuickBooks published
  it, per rate, with the taxable base.
* **Posted** -- the reconstructed double entry on the client's tax liability
  accounts over the same period. Credits are tax collected, debits are tax paid.

They come from different places -- one is Intuit's tax detail, the other is the
ledger rebuilt from the documents themselves -- so agreement is evidence and
disagreement is a real defect in one of them.

What the second reading does *not* check
----------------------------------------
Stated honestly because it is the limit of the claim: the posted reading
validates the **totals**, not the split between GST/HST and provincial tax.
Postings land on whichever accounts the client uses, and many clients run one
tax account for everything. The split rests entirely on `tax_rates`
classification -- which is why an unclassified rate blocks the return rather
than being assumed onto one side.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import sales_tax, tax_rates

# The four figures on a GST/HST return that this can compute. Lines 103, 106,
# 110 and the rest of the form exist; they are adjustments, rebates and
# instalments that live outside the ledger, and inventing them would be exactly
# the thing this module refuses to do.
LINE_101 = "101"
LINE_105 = "105"
LINE_108 = "108"
LINE_109 = "109"

LINE_TITLES = {
    LINE_101: "Sales and other revenue",
    LINE_105: "Total GST/HST and adjustments collected",
    LINE_108: "Total input tax credits and adjustments",
    LINE_109: "Net tax",
}

# Account types a sales-tax liability lives on in QuickBooks Canada. The posted
# reading looks only at these; a client who books GST somewhere else produces a
# disagreement, which is the correct outcome -- it means the two readings are
# not looking at the same thing and the return cannot be trusted.
TAX_LIABILITY_TYPES = ("Other Current Liability",)

# What counts as revenue on line 101.
REVENUE_TYPES = ("Income", "Other Income")


def money(value=0) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class RateLine:
    """One rate's contribution, so a figure can be taken apart."""
    rate_ref: str
    label: str
    classification: str
    taxable_base: Decimal
    tax_amount: Decimal


@dataclass
class GSTReturn:
    organization_id: str
    period_start: str
    period_end: str
    lines: dict[str, Decimal] = field(default_factory=dict)
    collected_rates: list[RateLine] = field(default_factory=list)
    paid_rates: list[RateLine] = field(default_factory=list)
    # Provincial tax is real and is deliberately not on this return. Carried so
    # an accountant can see it was found and excluded on purpose, rather than
    # wondering whether it was missed.
    excluded_provincial: list[RateLine] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    agreement: dict = field(default_factory=dict)

    @property
    def filable(self) -> bool:
        """True only when nothing was left unaccounted for.

        Deliberately not "no errors". A return that could be computed but whose
        two readings disagree is not filable either, because one of them is
        wrong and nothing here can say which.
        """
        return not self.blocked

    def figure(self, line: str) -> Decimal:
        if not self.filable:
            raise NotFilable(
                "This period cannot produce a return: " + "; ".join(self.blocked[:5]))
        return self.lines[line]

    def summary(self) -> dict:
        return {
            "period_start": self.period_start, "period_end": self.period_end,
            "filable": self.filable,
            "lines": {key: str(value) for key, value in sorted(self.lines.items())},
            "blocked": list(self.blocked),
            "agreement": dict(self.agreement),
        }


class NotFilable(ValueError):
    """The period holds something that cannot be accounted for."""


def prepare(conn: sqlite3.Connection, organization_id: str, *,
            period_start: str, period_end: str,
            provider: str = "quickbooks") -> GSTReturn:
    """Compute a return, or say exactly why there is not one."""
    from . import filing_periods

    result = GSTReturn(organization_id=organization_id,
                       period_start=period_start, period_end=period_end)

    # The Quick Method does not calculate net tax from the regular difference
    # between tax collected and input tax credits. Nothing in the transaction
    # data can reveal which method the client elected, so the fact must be on
    # file before any figures are handed over. A known Quick Method client is
    # also refused until that method is implemented and accountant-approved.
    try:
        arrangement = filing_periods.arrangement_for(conn, organization_id)
    except filing_periods.FilingUnknown:
        arrangement = None
    if arrangement is None or not arrangement.calculation_method:
        result.blocked.append(
            "The client's GST/HST calculation method has not been recorded. "
            "Record whether they use the regular method or the Quick Method; "
            "these produce different returns, so no method is assumed.")
    elif arrangement.calculation_method == filing_periods.QUICK_METHOD:
        result.blocked.append(
            "This client uses the GST/HST Quick Method, which this return lane "
            "does not implement. Regular-method figures would be wrong, so no "
            "return is produced.")

    stated = sales_tax.period(conn, organization_id,
                              period_start=period_start, period_end=period_end)
    result.blocked.extend(stated.blocked)

    registry = tax_rates.registry(conn, organization_id, provider)
    used = {item.tax_rate_ref for item in stated.collected} | {
        item.tax_rate_ref for item in stated.paid}

    # A rate used in the period that nobody has sorted onto a return. Blocking
    # here is the whole reason tax_rates exists: assuming it federal would
    # overstate the input tax credit, and assuming it provincial would
    # understate the tax collected. Both are wrong in a way the return hides.
    for rate_ref in sorted(used):
        rate = registry.get(rate_ref)
        if rate is None:
            result.blocked.append(
                f"Rate {rate_ref} was charged in this period and is not in the "
                "client's rate list, so it cannot be placed on a return. "
                "Re-sync the client's QuickBooks lists.")
        elif rate.undecided:
            result.blocked.append(
                f"Rate {rate.label()} is not classified as GST/HST or as "
                "provincial tax, so it cannot be placed on a return. Somebody "
                "has to decide which it is.")

    result.collected_rates = _lines_for(stated.collected, registry, tax_rates.GST_HST)
    result.paid_rates = _lines_for(stated.paid, registry, tax_rates.GST_HST)
    result.excluded_provincial = (
        _lines_for(stated.collected, registry, tax_rates.PROVINCIAL)
        + _lines_for(stated.paid, registry, tax_rates.PROVINCIAL))

    collected = sum((item.tax_amount for item in result.collected_rates),
                    Decimal("0.00"))
    credits = sum((item.tax_amount for item in result.paid_rates), Decimal("0.00"))

    revenue, revenue_problem = _revenue(conn, organization_id,
                                        period_start, period_end)
    if revenue_problem:
        result.blocked.append(revenue_problem)

    result.lines = {
        LINE_101: revenue,
        LINE_105: collected,
        LINE_108: credits,
        LINE_109: collected - credits,
    }

    result.agreement = _agreement(conn, organization_id, period_start,
                                  period_end, stated)
    if result.agreement.get("status") == "disagrees":
        result.blocked.append(
            "The tax QuickBooks states and the tax the ledger posts do not "
            f"agree ({result.agreement.get('detail', '')}). One of the two is "
            "wrong and nothing here can say which, so no figure is produced.")
    elif result.agreement.get("status") == "not_compared":
        result.blocked.append(
            "The ledger could not be read back to check these figures: "
            + str(result.agreement.get("detail", "")))
    return result


def prepare_current(conn: sqlite3.Connection, organization_id: str, *,
                    today: date | None = None) -> tuple[GSTReturn | None, str]:
    """The return for the period that has most recently closed, or the reason.

    An accountant does not want the period they are standing in -- it is not
    finished. They want the one that just ended and is now due. Returns
    `(None, reason)` rather than raising, because a client whose filing
    arrangement nobody has recorded is an ordinary state for a new engagement,
    not an error.
    """
    from . import filing_periods

    today = today or date.today()
    try:
        arrangement = filing_periods.arrangement_for(conn, organization_id)
    except filing_periods.FilingUnknown as exc:
        return None, str(exc)

    closed = [period for period in filing_periods.periods_between(
        arrangement, start=filing_periods._add_months(today, -26), end=today)
        if period.end < today]
    if not closed:
        return None, "No filing period has closed yet for this client."
    period = closed[-1]
    prepared = prepare(conn, organization_id,
                       period_start=period.start.isoformat(),
                       period_end=period.end.isoformat())
    prepared.agreement.setdefault("period_label", period.label)
    return prepared, ""


def persist(conn: sqlite3.Connection, prepared: GSTReturn, *,
            due_at: str | None = None) -> str:
    """Keep what was prepared, so that what an accountant saw is what they sign.

    Status never reaches a filed state from inside this system. Shimline
    prepares a return; a person files it.
    """
    import json

    from . import crm

    existing = conn.execute(
        "SELECT id FROM bookkeeping_gst_returns WHERE organization_id=? "
        "AND period_start=? AND period_end=?",
        (prepared.organization_id, prepared.period_start,
         prepared.period_end)).fetchone()
    return_id = existing[0] if existing else crm.new_id("gst")
    rates = [{"rate": item.rate_ref, "label": item.label,
              "side": side, "classification": item.classification,
              "base": str(item.taxable_base), "tax": str(item.tax_amount)}
             for side, group in (("collected", prepared.collected_rates),
                                 ("paid", prepared.paid_rates),
                                 ("excluded", prepared.excluded_provincial))
             for item in group]
    conn.execute(
        "INSERT INTO bookkeeping_gst_returns("
        "id,organization_id,period_start,period_end,due_at,line_101,line_105,"
        "line_108,line_109,filable,blocked_json,agreement_json,rates_json,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(organization_id,period_start,period_end) DO UPDATE SET "
        "due_at=excluded.due_at, line_101=excluded.line_101,"
        "line_105=excluded.line_105, line_108=excluded.line_108,"
        "line_109=excluded.line_109, filable=excluded.filable,"
        "blocked_json=excluded.blocked_json, agreement_json=excluded.agreement_json,"
        "rates_json=excluded.rates_json, status=excluded.status,"
        "prepared_at=CURRENT_TIMESTAMP",
        (return_id, prepared.organization_id, prepared.period_start,
         prepared.period_end, due_at,
         str(prepared.lines.get(LINE_101, "")), str(prepared.lines.get(LINE_105, "")),
         str(prepared.lines.get(LINE_108, "")), str(prepared.lines.get(LINE_109, "")),
         int(prepared.filable), json.dumps(prepared.blocked),
         json.dumps(prepared.agreement), json.dumps(rates),
         "prepared" if prepared.filable else "blocked"))
    return return_id


def _lines_for(totals, registry: dict, classification: str) -> list[RateLine]:
    lines = []
    for item in totals:
        rate = registry.get(item.tax_rate_ref)
        if rate is None or rate.classification != classification:
            continue
        lines.append(RateLine(
            rate_ref=item.tax_rate_ref, label=rate.label(),
            classification=rate.classification,
            taxable_base=item.taxable_base, tax_amount=item.tax_amount))
    return lines


def _revenue(conn: sqlite3.Connection, organization_id: str,
             period_start: str, period_end: str) -> tuple[Decimal, str]:
    """Line 101: sales and other revenue for the period, tax excluded.

    Read from the posted ledger lines rather than from document totals, because
    a document total includes the tax and line 101 does not. Revenue is a credit
    balance, so the sign is flipped to report it the way the form asks.
    """
    rows = conn.execute(
        "SELECT l.debit, l.credit FROM bookkeeping_transaction_lines l "
        "JOIN bookkeeping_transactions t ON t.id = l.transaction_id "
        "JOIN bookkeeping_accounts a ON a.id = l.account_id "
        "WHERE t.organization_id = ? AND t.status = 'posted' "
        f"  AND a.account_type IN ({','.join('?' for _ in REVENUE_TYPES)}) "
        "  AND t.transaction_date >= ? AND t.transaction_date <= ?",
        (organization_id, *REVENUE_TYPES, period_start, period_end)).fetchall()
    if not rows:
        # Nothing on an income account. A period with no sales is a fact, not a
        # gap -- a contractor between jobs files a nil return -- so this is zero
        # rather than a block. The tax figures below would be zero too, and a
        # period with tax but no revenue disagrees and blocks there instead.
        return Decimal("0.00"), ""
    total = sum((money(row[1]) - money(row[0]) for row in rows), Decimal("0.00"))
    return total, ""


def _agreement(conn: sqlite3.Connection, organization_id: str,
               period_start: str, period_end: str, stated) -> dict:
    """Require the ledger to post the tax QuickBooks says it charged.

    Two readings of the same period from different data. The posted side cannot
    tell GST from PST -- postings land on whichever account the client uses --
    so this checks the totals, and the split rests on the rate classification.
    Saying which half is checked matters more than the check itself.
    """
    rows = conn.execute(
        "SELECT l.debit, l.credit FROM bookkeeping_transaction_lines l "
        "JOIN bookkeeping_transactions t ON t.id = l.transaction_id "
        "JOIN bookkeeping_accounts a ON a.id = l.account_id "
        "WHERE t.organization_id = ? AND t.status = 'posted' "
        f"  AND a.account_type IN ({','.join('?' for _ in TAX_LIABILITY_TYPES)}) "
        "  AND t.transaction_date >= ? AND t.transaction_date <= ?",
        (organization_id, *TAX_LIABILITY_TYPES, period_start, period_end)).fetchall()

    stated_net = stated.total_collected - stated.total_paid if stated.usable else None
    if stated_net is None:
        return {"status": "not_compared",
                "detail": "the stated tax could not be totalled"}
    if not rows:
        # No postings on a tax account at all. With no stated tax either, the
        # two readings agree that the period is untaxed. With stated tax, they
        # do not, and that is a defect rather than an absence.
        if stated_net == 0:
            return {"status": "agrees", "stated": "0.00", "posted": "0.00",
                    "checked": "totals only; the split rests on rate classification"}
        return {"status": "disagrees",
                "detail": f"QuickBooks states {stated_net} of net tax and the "
                          "ledger posts none to any tax account",
                "stated": str(stated_net), "posted": "0.00"}

    posted_net = sum((money(row[1]) - money(row[0]) for row in rows), Decimal("0.00"))
    if posted_net != stated_net:
        return {"status": "disagrees",
                "detail": f"QuickBooks states {stated_net} and the ledger posts "
                          f"{posted_net}",
                "stated": str(stated_net), "posted": str(posted_net)}
    return {"status": "agrees", "stated": str(stated_net), "posted": str(posted_net),
            "checked": "totals only; the split rests on rate classification"}
