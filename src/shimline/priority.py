"""Why a piece of work is at the top of the queue.

The product plan asks for a queue ranked by service risk rather than creation
date, combining overdue state, SLA time remaining, deadline proximity, client
value and whether prerequisites are present — and requires that the calculated
reason be visible, so an operator never has to guess.

This replaces a three-key sort with a set of independent **signals**. Each one
looks at an engagement, returns a strength between 0 and 1, and explains itself
in a sentence. The score is their weighted sum.

Adding a new consideration — a statutory filing date, a client who has churn
risk, a job that blocks another — is one function appended to `SIGNALS`. No
existing signal changes, no sort key is rewritten, and the explanation appears
in the interface for free.

Weights are deliberately coarse. They express an ordering of concerns, not a
precision we have the data to justify:

    breaching a promise      > about to breach one
    work we can finish       > work blocked on someone else
    something silently stuck > business as usual
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import clock
from .vocabulary import ENGAGEMENT


@dataclass(frozen=True)
class Contribution:
    """One signal's verdict on one engagement."""

    name: str
    weight: float
    strength: float          # 0.0 – 1.0
    reason: str = ""

    @property
    def value(self) -> float:
        return self.weight * max(0.0, min(1.0, self.strength))


@dataclass(frozen=True)
class Priority:
    score: float
    contributions: tuple[Contribution, ...]

    @property
    def reasons(self) -> tuple[str, ...]:
        """Explanations, strongest first, for anything actually firing."""
        firing = [c for c in self.contributions if c.strength > 0 and c.reason]
        firing.sort(key=lambda c: c.value, reverse=True)
        return tuple(c.reason for c in firing)

    @property
    def headline(self) -> str:
        return self.reasons[0] if self.reasons else "Next in line by due date."


Signal = Callable[[dict, datetime], Contribution]


def _saturate(value: float, half_life: float) -> float:
    """Map 0..inf onto 0..1, reaching 0.5 at `half_life`.

    A curve rather than a cliff, so a task two days overdue outranks one that
    is one day overdue without a task eleven days overdue swamping everything.
    """
    if value <= 0:
        return 0.0
    return value / (value + half_life)


# --------------------------------------------------------------- the signals --

# Deliberately larger than every other weight combined (240). A broken promise
# is a different kind of problem from a busy day, and no accumulation of
# ordinary pressure should push it down the queue. Within the overdue set the
# remaining signals still order things sensibly.
OVERDUE_WEIGHT = 300


def overdue(item: dict, now: datetime) -> Contribution:
    """A promise already broken. Nothing outranks this."""
    due = clock.parse_timestamp(item.get("due_at"))
    if not due or due >= now:
        return Contribution("overdue", OVERDUE_WEIGHT, 0.0)
    days = max(1, -clock.days_until(due, now))
    plural = "" if days == 1 else "s"
    return Contribution(
        "overdue", OVERDUE_WEIGHT, _saturate(days, 2),
        f"Past the five-business-day promise by {days} day{plural}.",
    )


def sla_pressure(item: dict, now: datetime) -> Contribution:
    """Not late yet, but the clock is visible."""
    due = clock.parse_timestamp(item.get("due_at"))
    if not due or due < now:
        return Contribution("sla_pressure", 60, 0.0)
    days = clock.days_until(due, now)
    if days > 5:
        return Contribution("sla_pressure", 60, 0.0)
    when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
    return Contribution(
        "sla_pressure", 60, _saturate(6 - days, 3),
        f"Due {when}.",
    )


def ready_to_finish(item: dict, now: datetime) -> Contribution:
    """Ours to finish, and nearly done.

    Work sitting in internal review is the cheapest revenue in the queue: it is
    finished, it is not waiting on anybody, and it is one action from delivered.
    """
    status = item.get("status")
    if status not in ("internal_review", "in_progress"):
        return Contribution("ready_to_finish", 45, 0.0)
    total = item.get("work_total") or 0
    done = item.get("work_done") or 0
    progress = done / total if total else 0.0
    if status == "internal_review":
        return Contribution("ready_to_finish", 45, 1.0,
                            "Finished and waiting on our own sign-off.")
    if progress >= 0.5:
        return Contribution("ready_to_finish", 45, progress,
                            f"{done} of {total} steps done — close to delivery.")
    return Contribution("ready_to_finish", 45, progress * 0.5)


def waiting_on_client(item: dict, now: datetime) -> Contribution:
    """Blocked on someone else — worth a nudge, not a panic.

    Low weight while it is fresh, rising as it goes stale, because a request
    nobody chased is the most common way an engagement quietly dies.
    """
    if item.get("status") != "awaiting_client":
        return Contribution("waiting_on_client", 35, 0.0)
    since = clock.parse_timestamp(item.get("updated_at")) or clock.parse_timestamp(item.get("created_at"))
    days = max(0, -clock.days_until(since, now)) if since else 0
    if days < 3:
        return Contribution("waiting_on_client", 35, 0.15,
                            "Waiting on the client.")
    return Contribution("waiting_on_client", 35, _saturate(days, 7),
                        f"Waiting on the client for {days} days — chase it.")


def unstarted_after_payment(item: dict, now: datetime) -> Contribution:
    """Paid, but the clock was never started.

    This is the prerequisites signal from the plan. The client has paid and is
    waiting; if nobody marks the work ready, the SLA never begins and the
    engagement is invisible to every deadline-based measure.
    """
    if item.get("ready_at") or item.get("status") in ("delivered", "closed"):
        return Contribution("unstarted", 55, 0.0)
    created = clock.parse_timestamp(item.get("created_at"))
    days = max(0, -clock.days_until(created, now)) if created else 0
    if days < 1:
        return Contribution("unstarted", 55, 0.0)
    return Contribution("unstarted", 55, _saturate(days, 4),
                        f"Paid {days} days ago and not started — the SLA clock has not begun.")


def stalled(item: dict, now: datetime) -> Contribution:
    """Nothing has happened here for a while, whatever the state says."""
    touched = clock.parse_timestamp(item.get("updated_at"))
    if not touched or item.get("status") in ("delivered", "closed"):
        return Contribution("stalled", 25, 0.0)
    days = max(0, -clock.days_until(touched, now))
    if days < 7:
        return Contribution("stalled", 25, 0.0)
    return Contribution("stalled", 25, _saturate(days - 6, 10),
                        f"No movement for {days} days.")


def client_value(item: dict, now: datetime) -> Contribution:
    """Weight by what the relationship is worth.

    Every engagement is a C$199 review today, so this is currently flat and
    changes no ordering. It exists because the plan calls for it and because a
    recurring bookkeeping client will not be worth the same as a one-off
    diagnostic — at which point this becomes meaningful without anything else
    being rewritten.
    """
    cents = item.get("client_value_cents") or 0
    if cents <= 0:
        return Contribution("client_value", 20, 0.0)
    return Contribution("client_value", 20, _saturate(cents / 100, 500))


SIGNALS: tuple[Signal, ...] = (
    overdue,
    sla_pressure,
    ready_to_finish,
    waiting_on_client,
    unstarted_after_payment,
    stalled,
    client_value,
)


def evaluate(item: dict, now: datetime | None = None) -> Priority:
    now = now or clock.now()
    contributions = tuple(signal(item, now) for signal in SIGNALS)
    return Priority(score=sum(c.value for c in contributions), contributions=contributions)


def rank(items: list[dict], now: datetime | None = None) -> list[dict]:
    """Attach a priority to each engagement and sort by it, highest first.

    Ties break on the earlier due date, then on lifecycle position, so the
    ordering is stable and reproducible rather than dependent on row order.
    """
    now = now or clock.now()
    far_future = now.replace(year=9999)
    for item in items:
        priority = evaluate(item, now)
        item["priority"] = priority
        item["priority_score"] = round(priority.score, 2)
        item["priority_reason"] = priority.headline
        item["priority_reasons"] = priority.reasons
    # The final key on id gives a total order. Without it, two genuinely
    # identical engagements sort by whatever order the database returned, and
    # the queue reshuffles between refreshes for no visible reason.
    items.sort(key=lambda item: (
        -item["priority_score"],
        clock.parse_timestamp(item.get("due_at")) or far_future,
        ENGAGEMENT.position(item.get("status")),
        str(item.get("id") or ""),
    ))
    return items
