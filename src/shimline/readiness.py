"""What a client still needs before their review can be complete.

Eight things have to be true before a bookkeeping run produces a full answer,
and until now every one of them was discovered *after* the scan, as a block with
no instruction attached. An operator onboarding their fourth client learns the
order by repetition; an operator onboarding their first gets a page of blocks
and no idea which to fix, or in what order, or whether any of them are theirs to
fix at all.

That is an onboarding problem before it is a UI problem. Every step nobody can
see is a reason a client does not get onboarded, and the clients who do not get
onboarded are invisible in a way the ones who do are not.

What this is not
----------------
It is not a second source of truth. Every step here reports the same condition
the engine already refuses on, read from the same tables. If a step says the
statement is mapped and reconciliation still says `no_source`, this module is
wrong and the engine is right. It exists to say the same thing *earlier*, in
order, with the action attached.

Which is why each step carries `blocking`: some of these stop a review dead, and
some only narrow it. Telling an operator that a missing GST filing frequency is
as urgent as a missing QuickBooks connection would be false, and the second time
they noticed it was false they would stop reading the list.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

# Steps in the order they have to happen. The order is real: a statement cannot
# be mapped to a ledger account before the accounts have been pulled, and a tax
# rate cannot be classified before the rates have been read.
CONNECT = "connect"
FIRST_CONTACT = "first_contact"
PROJECTS = "projects"
REPORTS = "reports"
STATEMENT = "statement"
STATEMENT_MAPPED = "statement_mapped"
FILING = "filing"
RATES = "rates"
SCAN = "scan"


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    done: bool
    # True when nothing useful can be produced without it. False when its
    # absence narrows the review rather than stopping it -- and the difference
    # has to be honest, or the list stops being read.
    blocking: bool
    detail: str
    action_label: str = ""
    action_path: str = ""

    @property
    def state(self) -> str:
        if self.done:
            return "done"
        return "blocking" if self.blocking else "narrowing"


@dataclass
class Readiness:
    organization_id: str
    steps: list[Step]

    @property
    def done(self) -> int:
        return sum(1 for step in self.steps if step.done)

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def ready(self) -> bool:
        """Every step done. A review can be complete rather than merely run."""
        return self.done == self.total

    @property
    def can_scan(self) -> bool:
        """Nothing blocking is outstanding. A scan will produce something."""
        return not any(step.blocking and not step.done for step in self.steps)

    def outstanding(self) -> list[Step]:
        return [step for step in self.steps if not step.done]

    def next_step(self) -> Step | None:
        """The one thing to do now. The list is in order for this reason."""
        pending = self.outstanding()
        return pending[0] if pending else None



def _projects(count: int) -> str:
    return (f"{count} active QuickBooks project"
            + ("" if count == 1 else "s")
            + (" is" if count == 1 else " are"))

def for_client(conn: sqlite3.Connection, organization_id: str, *,
               today: date | None = None) -> Readiness:
    """Read the same tables the engine refuses on, and say what is missing."""
    today = today or date.today()
    client = f"/admin/clients/{organization_id}"
    steps: list[Step] = []

    connection = conn.execute(
        "SELECT id, status FROM connections WHERE organization_id=? "
        "ORDER BY created_at DESC LIMIT 1", (organization_id,)).fetchone()
    connected = bool(connection and connection[1] == "active")
    steps.append(Step(
        CONNECT, "QuickBooks connected", connected, True,
        "Nothing can be read about this client's books until they approve the "
        "connection. This is the one step that needs the client rather than us."
        if not connected else "The client has approved access.",
        "Send the connection request", client))

    # Not blocking, and placed immediately after the connection because that is
    # when it happens: the probe runs itself on the keepalive timer. Its value is
    # that the dozen assumptions taken from Intuit's documentation stop being
    # assumptions for this client, and this is the only link to where they are
    # written down.
    probe = conn.execute(
        "SELECT status, ledger_complete, assumptions_failed FROM connection_probes "
        "WHERE organization_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (organization_id,)).fetchone()
    probed = bool(probe and probe[0] == "ok")
    if not probe:
        probe_detail = ("Nothing has read this file yet. The probe runs on its "
                        "own once a connection exists, so this needs no one."
                        if not connected else
                        "A connection exists and the probe has not run yet; it "
                        "runs on the daily timer.")
    elif probe[0] != "ok":
        probe_detail = ("The pull did not finish. What it died on is recorded, "
                        "and it is the most useful thing we could know.")
    elif probe[2]:
        probe_detail = (f"{probe[2]} documented assumption(s) did not hold on "
                        "this file. Each one is a reason a ledger blocks.")
    else:
        probe_detail = ("Read once, and every documented assumption held"
                        + ("." if probe[1] else
                           " -- though no complete ledger came out of it."))
    steps.append(Step(
        FIRST_CONTACT, "File read once", probed, False, probe_detail,
        "See what the file contained", f"{client}/first-contact"))

    # Existence is not the question. Check 16 deliberately stopped reporting the
    # configuration choice so it could report genuine one-sided tagging, and this
    # step is where the choice went. If it asked only whether jobs *exist*, a
    # client with a dozen jobs that no transaction ever names would read as fully
    # configured while their project profitability report came back empty -- a
    # green tick on a decision nobody made. So the step is done when the jobs are
    # used, and it says which of the three reasons it is not.
    projects = conn.execute(
        "SELECT COUNT(*) FROM bookkeeping_projects "
        "WHERE organization_id=? AND active=1", (organization_id,)).fetchone()[0]
    lines, used = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT l.project_id) "
        "FROM bookkeeping_transaction_lines l "
        "JOIN bookkeeping_transactions t ON t.id=l.transaction_id "
        "WHERE t.organization_id=?", (organization_id,)).fetchone()
    lines, used = int(lines or 0), int(used or 0)
    if not projects:
        projects_detail = (
            "No active QuickBooks customers are marked as projects. That can be "
            "a deliberate bookkeeping choice; record it here once during "
            "readiness rather than reporting the same configuration fact as a "
            "defect on every scan.")
    elif not lines:
        # Same reasoning as the rate step below: do not judge usage before
        # anything has been read, and do not tick the step for it either.
        projects_detail = (
            f"{_projects(projects)} available for job costing. No scan has "
            "recorded transaction lines yet, so whether they are used is not "
            "known.")
    elif not used:
        projects_detail = (
            f"{_projects(projects)} available for job costing, and not one of "
            f"the {lines} recorded transaction line(s) names a project. Project "
            "profitability will be empty for this client. Either jobs are not "
            "being chosen when transactions are entered, or these are left over "
            "from a setup nobody kept using -- and which of the two it is is a "
            "question for the client.")
    else:
        projects_detail = (f"{_projects(projects)} available for job costing; "
                           f"{used} named by recorded transaction lines.")
    steps.append(Step(
        PROJECTS, "Project tracking chosen", bool(projects and used), False,
        projects_detail, "Review project tracking", client))

    synced = conn.execute(
        "SELECT COUNT(*) FROM source_snapshots WHERE organization_id=? "
        "AND report_name='TrialBalance'", (organization_id,)).fetchone()[0]
    steps.append(Step(
        REPORTS, "Reports synced", bool(synced), False,
        "Without QuickBooks' own trial balance the third verification does not "
        "run. The books are still checked twice, and the console says so rather "
        "than counting a check that did not happen."
        if not synced else "QuickBooks' own trial balance is available to "
                           "check our reconstruction against.",
        "Pull the reports", client))

    statements = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN qbo_account_id IS NOT NULL "
        "AND qbo_account_id<>'' THEN 1 ELSE 0 END) "
        "FROM bank_statements WHERE organization_id=?",
        (organization_id,)).fetchone()
    held, mapped = int(statements[0] or 0), int(statements[1] or 0)
    steps.append(Step(
        STATEMENT, "Bank statement uploaded", bool(held), False,
        "Reconciliation and the missing-transaction check have nothing to work "
        "from. Every other check still runs."
        if not held else f"{held} statement(s) held.",
        "Upload a statement", client))
    steps.append(Step(
        STATEMENT_MAPPED, "Statement mapped to a ledger account",
        bool(held and mapped), False,
        "A statement that is not pointed at the account it proves cannot prove "
        "anything, so it is withheld from the engine entirely."
        if held and not mapped else
        ("Nothing to map yet." if not held else
         f"{mapped} of {held} statement(s) mapped."),
        "Map the statement", client))

    arrangement = conn.execute(
        "SELECT frequency, COALESCE(calculation_method,'') "
        "FROM bookkeeping_gst_filing WHERE organization_id=?",
        (organization_id,)).fetchone()
    filing_ready = bool(arrangement and arrangement[1] == "regular")
    steps.append(Step(
        FILING, "GST/HST filing arrangement supported", filing_ready, False,
        "No GST/HST return can be prepared. Frequency and year end must be "
        "recorded because guessing them would prepare a return for a period the "
        "client does not file; the calculation method must also be recorded "
        "because the regular and Quick Methods produce different figures."
        if not arrangement or not arrangement[1] else
        ("This client uses the Quick Method, which this return lane does not "
         "implement." if arrangement[1] == "quick" else
         f"Files {arrangement[0]} using the regular method."),
        "Record how they file", f"{client}/gst"))

    undecided = conn.execute(
        "SELECT COUNT(*) FROM bookkeeping_tax_rates WHERE organization_id=? "
        "AND classification='unknown'", (organization_id,)).fetchone()[0]
    # Not done until the rates have actually been read. A client with no rate
    # rows has nothing unclassified, so this would otherwise show green before
    # anybody had done anything -- and a checklist with a step that is already
    # ticked on day one is a checklist an operator stops trusting. A client who
    # genuinely has no rates, because they are not registered, reaches done as
    # soon as the connection is made and the pull comes back empty.
    rates_read = conn.execute(
        "SELECT COUNT(*) FROM bookkeeping_tax_rates WHERE organization_id=?",
        (organization_id,)).fetchone()[0]

    steps.append(Step(
        RATES, "Tax rates classified",
        bool((rates_read or connected) and not undecided), False,
        f"{undecided} rate(s) are not sorted onto a return. Any period one of "
        "them was charged in cannot produce a GST/HST return."
        if undecided else
        ("Nothing has read this client's rates yet."
         if not (rates_read or connected) else
         (f"All {rates_read} rate(s) are sorted onto a return." if rates_read
          else "This client has no sales tax rates.")),
        "Classify the rates", f"{client}/gst"))

    scanned = conn.execute(
        "SELECT COUNT(*) FROM bookkeeping_runs WHERE organization_id=?",
        (organization_id,)).fetchone()[0]
    steps.append(Step(
        SCAN, "Bookkeeping scan run", bool(scanned), False,
        "Nothing has looked at these books yet."
        if not scanned else f"{scanned} run(s) on record.",
        "Run the scan", client))

    return Readiness(organization_id=organization_id, steps=steps)
