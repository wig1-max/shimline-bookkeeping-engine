"""The domain's vocabulary, defined once.

Every stage and status the product knows about lives here with its label, its
visual tone, its position in a lifecycle, and what it means operationally.
Routes validate against it, templates render from it, and the priority model
reasons over it.

Before this existed, adding one engagement status meant editing a tuple in
`admin.py`, a hardcoded list in three templates, a position map, a ranking
dict, and a CSS rule — five places, and forgetting one produced a screen that
silently omitted the new state. Now it is one entry here.

Adding a state: append a `Stage` to the relevant `Vocabulary`. Nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Stage:
    """One state something can be in."""

    code: str
    label: str
    # Drives the pill colour. Kept to a small shared set so a new state cannot
    # invent a colour nobody has designed: see .tone-* in admin.css.
    tone: str = "neutral"
    # Position along the lifecycle, for progress paths and ordering. States
    # that share a position are alternatives at the same point.
    position: int = 0
    # Terminal states are done; they leave the active work queue.
    terminal: bool = False
    # Shown on a progress path (some states are real but not worth a step).
    on_path: bool = False
    # Short operator-facing explanation of what this state means.
    meaning: str = ""


@dataclass(frozen=True)
class Vocabulary:
    name: str
    stages: tuple[Stage, ...]
    _by_code: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "_by_code", {stage.code: stage for stage in self.stages})

    def __iter__(self):
        return iter(self.stages)

    def __contains__(self, code: object) -> bool:
        return code in self._by_code

    def get(self, code: str | None) -> Stage:
        """Never raises: an unknown code renders as itself rather than a 500."""
        if code in self._by_code:
            return self._by_code[code]
        text = str(code or "unknown")
        return Stage(code=text, label=text.replace("_", " ").capitalize())

    def codes(self) -> tuple[str, ...]:
        return tuple(stage.code for stage in self.stages)

    def choices(self) -> tuple[tuple[str, str], ...]:
        """(code, label) pairs for a <select>."""
        return tuple((stage.code, stage.label) for stage in self.stages)

    def path(self) -> tuple[Stage, ...]:
        """The stages worth drawing as a progress path."""
        return tuple(stage for stage in self.stages if stage.on_path)

    def active(self) -> tuple[Stage, ...]:
        return tuple(stage for stage in self.stages if not stage.terminal)

    def position(self, code: str | None) -> int:
        return self.get(code).position


# How a business relationship progresses, independent of any single job.
LIFECYCLE = Vocabulary("lifecycle", (
    Stage("prospect", "Prospect", "neutral", 0, meaning="Identified, not yet in conversation"),
    Stage("qualified", "Qualified", "soon", 1, meaning="Worth pursuing"),
    Stage("proposal", "Proposal", "soon", 2, meaning="They have our terms"),
    Stage("onboarding", "Onboarding", "soon", 3, meaning="Paid; being set up"),
    Stage("active", "Active", "good", 4, meaning="Live client"),
    Stage("paused", "Paused", "neutral", 5, meaning="On hold, expected back"),
    Stage("offboarded", "Offboarded", "neutral", 6, terminal=True, meaning="Relationship ended"),
    Stage("lost", "Lost", "neutral", 6, terminal=True, meaning="Did not convert"),
))

# The sales pipeline for a single opportunity.
OPPORTUNITY = Vocabulary("opportunity", (
    Stage("new", "New", "neutral", 0, meaning="Not yet contacted"),
    Stage("contacted", "Contacted", "neutral", 1, meaning="Outreach sent, no reply"),
    Stage("replied", "Replied", "soon", 2, meaning="They responded — act on this"),
    Stage("qualified", "Qualified", "soon", 3, meaning="Fits the service"),
    Stage("proposal_sent", "Proposal sent", "soon", 4, meaning="Awaiting a decision"),
    Stage("won", "Won", "good", 5, terminal=True, meaning="Paid"),
    Stage("lost", "Lost", "neutral", 5, terminal=True, meaning="Declined"),
))

# A unit of delivered work: one review, one month's bookkeeping.
ENGAGEMENT = Vocabulary("engagement", (
    Stage("draft", "Draft", "neutral", 0, meaning="Not yet real"),
    Stage("awaiting_payment", "Awaiting payment", "neutral", 0,
          meaning="Created but unpaid"),
    Stage("awaiting_client", "Documents", "warn", 0, on_path=True,
          meaning="Waiting on the client for something"),
    Stage("ready", "Ready", "soon", 1, on_path=True,
          meaning="Everything needed is here; the clock is running"),
    Stage("in_progress", "Analysis", "soon", 2, on_path=True,
          meaning="Being worked on"),
    Stage("internal_review", "Review", "soon", 3, on_path=True,
          meaning="Done, awaiting our own sign-off"),
    Stage("delivered", "Delivery", "good", 4, on_path=True,
          meaning="Sent to the client"),
    Stage("closed", "Closed", "good", 5, terminal=True,
          meaning="Finished; the deletion clock has started"),
))

# One step inside an engagement's checklist.
WORK_ITEM = Vocabulary("work_item", (
    Stage("todo", "To do", "neutral", 0),
    Stage("in_progress", "In progress", "soon", 1),
    Stage("waiting_client", "Waiting on client", "warn", 1),
    Stage("review", "Review", "soon", 2),
    Stage("done", "Done", "good", 3, terminal=True),
    Stage("skipped", "Skipped", "neutral", 3, terminal=True),
))

ALL = {
    "lifecycle": LIFECYCLE,
    "opportunity": OPPORTUNITY,
    "engagement": ENGAGEMENT,
    "work_item": WORK_ITEM,
}
