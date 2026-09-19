"""Sales tax collected and paid, per rate, over a period.

This is the first floor of the GST/HST lane, and it is deliberately only a
floor: it reports what QuickBooks stated, and refuses to report anything it did
not. No return is prepared here, no filing figure is computed, and no rate is
inferred from an amount.

What it exists to prevent
-------------------------
Every persisted line used to carry a hard-coded tax of `0.00` -- not unknown,
zero. A return built on those tables would have come out at nil and looked
entirely correct. Numbers filed with the CRA under a client's name are the last
place in this system where a plausible answer is acceptable, so the rule here is
stricter than anywhere else: a period that contains even one document whose tax
cannot be accounted for produces no total at all.

Why the grain is the rate, not the line
---------------------------------------
QuickBooks publishes tax per rate on a document (`TxnTaxDetail.TaxLine`), with
the net amount each rate was charged on. It does not publish tax per line.
Apportioning a document's tax across its lines would invent the number a return
is filed on. Per rate, per period, with the taxable base, is both what the
provider states and what a GST/HST return asks for.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

# Which side of a return a document falls on. Tax on a sale is collected and
# owed to the CRA; tax on a purchase is paid and recoverable as an input tax
# credit. Getting this backwards would net to the wrong number in the one
# direction nobody would notice, so the mapping is explicit rather than
# inferred from the sign of an amount.
COLLECTED_TYPES = ("Invoice", "SalesReceipt")
PAID_TYPES = ("Bill", "Purchase")
# Reversals: a credit memo reduces tax collected, a vendor credit reduces tax
# paid, a refund receipt reduces tax collected.
COLLECTED_REVERSAL_TYPES = ("CreditMemo", "RefundReceipt")
PAID_REVERSAL_TYPES = ("VendorCredit",)

# Types that never carry sales tax of their own. A payment settles a document
# whose tax was already counted when the document was raised; counting it again
# would double the period.
SETTLEMENT_TYPES = ("Payment", "BillPayment", "Transfer", "Deposit", "JournalEntry")


def money(value=0) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class RateTotal:
    tax_rate_ref: str
    rate_percent: str | None
    taxable_base: Decimal
    tax_amount: Decimal
    documents: int


@dataclass
class TaxPeriod:
    period_start: str
    period_end: str
    collected: list[RateTotal] = field(default_factory=list)
    paid: list[RateTotal] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True only when nothing in the period was left unaccounted for."""
        return not self.blocked

    @property
    def total_collected(self) -> Decimal:
        return sum((item.tax_amount for item in self.collected), Decimal("0.00"))

    @property
    def total_paid(self) -> Decimal:
        return sum((item.tax_amount for item in self.paid), Decimal("0.00"))

    def net(self) -> Decimal:
        """Collected less paid. Meaningless unless `usable`, so it raises."""
        if not self.usable:
            raise TaxPeriodBlocked(
                "This period cannot be totalled: " + "; ".join(self.blocked[:5]))
        return self.total_collected - self.total_paid


class TaxPeriodBlocked(ValueError):
    """The period holds tax that could not be accounted for at all."""


def _rows(conn: sqlite3.Connection, organization_id: str,
          period_start: str, period_end: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.id, t.provider_type, t.provider_id, t.document_number, t.tax_total, "
        "       x.tax_rate_ref, x.rate_percent, x.net_amount_taxable, x.tax_amount "
        "FROM bookkeeping_transactions t "
        "LEFT JOIN bookkeeping_transaction_taxes x ON x.transaction_id = t.id "
        "WHERE t.organization_id = ? AND t.status = 'posted' "
        "  AND t.transaction_date >= ? AND t.transaction_date <= ? "
        "ORDER BY t.transaction_date, t.id",
        (organization_id, period_start, period_end)).fetchall()


def period(conn: sqlite3.Connection, organization_id: str, *,
           period_start: str, period_end: str) -> TaxPeriod:
    """Tax collected and paid over a period, per rate, or the reason it cannot be.

    A document blocks the whole period when it states a tax total but no rate
    breakdown, or when its rate rows do not add up to the total it states. Both
    are cases where a number could be produced and would be wrong, and a wrong
    GST return is the most expensive output in this system.

    A document with no tax at all is not a problem: a zero-rated sale to a
    status-Indian customer, an exempt supply, an out-of-province job. Absence of
    a TxnTaxDetail is a fact QuickBooks states, not a gap.
    """
    result = TaxPeriod(period_start=period_start, period_end=period_end)
    collected: dict[str, list] = {}
    paid: dict[str, list] = {}
    seen: dict[str, dict] = {}

    for row in _rows(conn, organization_id, period_start, period_end):
        entry = seen.setdefault(row["id"], {
            "type": row["provider_type"],
            "label": row["document_number"] or row["provider_id"] or row["id"],
            "total": None if row["tax_total"] is None else money(row["tax_total"]),
            "rates": []})
        if row["tax_rate_ref"] is not None:
            entry["rates"].append(row)

    for record in seen.values():
        kind, label = record["type"], record["label"]
        stated = record["total"]
        if stated is None and not record["rates"]:
            continue                      # no tax on this document, which is a fact
        if kind in SETTLEMENT_TYPES:
            # A payment carries no tax of its own; the document it settles was
            # already counted. If one arrives carrying tax, that is a shape we
            # do not understand and must not silently drop.
            if stated or record["rates"]:
                result.blocked.append(
                    f"{kind} {label} carries sales tax, which a settlement "
                    "document should not; counting or ignoring it would both be "
                    "guesses")
            continue

        if stated is not None and not record["rates"]:
            result.blocked.append(
                f"{kind} {label} states {stated} of tax but no rate breakdown, so "
                "it cannot be attributed to a line of the return")
            continue

        summed = sum((money(item["tax_amount"]) for item in record["rates"]),
                     Decimal("0.00"))
        if stated is not None and summed != stated:
            result.blocked.append(
                f"{kind} {label} states {stated} of tax but its rates add up to "
                f"{summed}")
            continue

        if kind in COLLECTED_TYPES:
            bucket, sign = collected, Decimal("1")
        elif kind in PAID_TYPES:
            bucket, sign = paid, Decimal("1")
        elif kind in COLLECTED_REVERSAL_TYPES:
            bucket, sign = collected, Decimal("-1")
        elif kind in PAID_REVERSAL_TYPES:
            bucket, sign = paid, Decimal("-1")
        else:
            result.blocked.append(
                f"{kind} {label} carries sales tax and is not classified as a "
                "sale, a purchase or a reversal of either, so which side of the "
                "return it belongs on is unknown")
            continue

        for item in record["rates"]:
            bucket.setdefault(item["tax_rate_ref"] or "", []).append((sign, item))

    result.collected = _totals(collected)
    result.paid = _totals(paid)
    return result


def _totals(bucket: dict[str, list]) -> list[RateTotal]:
    totals = []
    for rate_ref, entries in sorted(bucket.items()):
        tax = sum((sign * money(item["tax_amount"]) for sign, item in entries),
                  Decimal("0.00"))
        base = sum((sign * money(item["net_amount_taxable"])
                    for sign, item in entries
                    if item["net_amount_taxable"] is not None), Decimal("0.00"))
        percents = {item["rate_percent"] for _, item in entries
                    if item["rate_percent"] is not None}
        totals.append(RateTotal(
            tax_rate_ref=rate_ref,
            # More than one percentage against one rate reference means the rate
            # changed mid-period. Reporting either would be wrong, so neither is.
            rate_percent=percents.pop() if len(percents) == 1 else None,
            taxable_base=base.quantize(Decimal("0.01")),
            tax_amount=tax.quantize(Decimal("0.01")),
            documents=len(entries)))
    return totals
