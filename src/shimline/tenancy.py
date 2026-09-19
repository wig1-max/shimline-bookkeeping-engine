"""Who may see which client's books.

This is a security boundary, not a UI convenience, so it is built as one thing
rather than as a check sprinkled through the routes. A route that forgets to
call a guard is invisible until someone reads another firm's ledger; a query
that cannot be written without a scope is not forgettable in the same way.

The shape
---------
`scope_for(session)` returns a `Scope`, and a Scope is the only thing that
answers "which organizations". Three kinds:

- `internal` -- Shimline's own staff, who see every client. They are not
  modelled as a firm holding everything: an internal account and a very large
  customer must never look the same in an audit trail.
- `firm` -- a firm principal sees every client the firm holds; a staff
  accountant sees only the clients they are assigned. The set is resolved once,
  from the database, and carried as data.
- `none` -- belonging to no firm and holding no workspace role, or belonging to
  a firm that has been suspended. Sees nothing, which is the safe direction for
  an account whose standing is unclear.

The order is load-bearing: firm membership is resolved *before* the workspace
roles. A firm principal needs the `reviewer` role to approve a proposal at all,
and reading the roles first would quietly promote every reviewing accountant to
seeing every client in the system. Provision firm people with
`create_firm_user`, which refuses to leave someone unscoped.

Two rules that matter more than the code
----------------------------------------
**Out of scope is 404, never 403.** A 403 confirms the id exists, so an outsider
can enumerate client ids by watching which ones answer differently. Absence and
denial have to be indistinguishable from outside.

**An empty scope filters to nothing, not to everything.** `IN ()` is a syntax
error in SQL, and the tempting fix -- drop the clause when the list is empty --
turns a firm with no clients into a firm with all of them. `sql_filter` emits
an always-false predicate instead, and there is a test for exactly that.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fastapi import HTTPException

# Two axes, and they must not be confused. Tenancy answers *which clients* a
# session may see. The workspace roles answer *which actions* it may take --
# approving a proposal, releasing a write, downloading a client report. Every
# one of the four seeded roles, `viewer` included, belongs to Shimline rather
# than to a customer firm, so all four are internal here.
#
# An earlier version of this list left `viewer` out, and the effect was that an
# internal read-only account stopped seeing any client at all: a role gate that
# used to answer 403 began answering 404. The role gate is a separate control
# and still applies on top of this one.
INTERNAL_ROLES = frozenset({"owner", "operator", "reviewer", "viewer"})

INTERNAL = "internal"
FIRM = "firm"
NONE = "none"


@dataclass(frozen=True)
class Scope:
    """What one session may see. Resolved once, then carried as data."""
    kind: str
    firm_id: str | None = None
    firm_role: str | None = None
    organization_ids: frozenset[str] | None = None   # None means unrestricted

    @property
    def unrestricted(self) -> bool:
        return self.kind == INTERNAL

    @property
    def sees_nothing(self) -> bool:
        return self.kind == NONE or (
            self.organization_ids is not None and not self.organization_ids)

    def allows(self, organization_id: str | None) -> bool:
        if not organization_id:
            return False
        if self.unrestricted:
            return True
        if self.organization_ids is None:
            return False
        return str(organization_id) in self.organization_ids


def scope_for(conn: sqlite3.Connection, session: dict | None) -> Scope:
    """Resolve a session to the set of clients it may see.

    Firm membership is checked before the internal roles on purpose. A firm
    principal who has also been given the workspace `reviewer` role -- which is
    how they get to approve a proposal at all -- must still be confined to their
    own firm. Reading the roles first would quietly promote every reviewing
    accountant to seeing every client in the system.
    """
    if not session:
        return Scope(kind=NONE)

    user_id = session.get("user_id")
    membership = conn.execute(
        "SELECT m.firm_id, m.firm_role, f.status FROM firm_members m "
        "JOIN firms f ON f.id = m.firm_id WHERE m.user_id = ?",
        (user_id,)).fetchone()

    if membership:
        firm_id, firm_role, status = membership[0], membership[1], membership[2]
        if status != "active":
            # A suspended firm keeps its rows and loses its access.
            return Scope(kind=NONE, firm_id=firm_id, firm_role=firm_role,
                         organization_ids=frozenset())
        return Scope(kind=FIRM, firm_id=firm_id, firm_role=firm_role,
                     organization_ids=_firm_organizations(conn, firm_id, user_id,
                                                          firm_role))

    if set(session.get("roles") or ()) & INTERNAL_ROLES:
        return Scope(kind=INTERNAL)

    return Scope(kind=NONE, organization_ids=frozenset())


def _firm_organizations(conn: sqlite3.Connection, firm_id: str, user_id: str,
                        firm_role: str) -> frozenset[str]:
    if firm_role == "principal":
        rows = conn.execute(
            "SELECT organization_id FROM firm_clients WHERE firm_id = ?",
            (firm_id,)).fetchall()
    else:
        # Staff see assignments, and only where the firm actually holds the
        # client. An assignment that outlives the engagement must not keep
        # granting access, so the join is against firm_clients rather than
        # against the assignment alone.
        rows = conn.execute(
            "SELECT a.organization_id FROM firm_client_assignments a "
            "JOIN firm_clients c ON c.firm_id = a.firm_id "
            "                   AND c.organization_id = a.organization_id "
            "WHERE a.firm_id = ? AND a.user_id = ?",
            (firm_id, user_id)).fetchall()
    return frozenset(str(row[0]) for row in rows)


def sql_filter(scope: Scope, column: str = "organization_id") -> tuple[str, list]:
    """A predicate and its parameters, to AND into a query.

    Returns `("", [])` only for an unrestricted scope. Everything else gets a
    real predicate, including the empty case: a firm holding no clients must
    match no rows, and dropping the clause would match all of them.
    """
    if scope.unrestricted:
        return "", []
    identifiers = sorted(scope.organization_ids or ())
    if not identifiers:
        return "1 = 0", []
    placeholders = ",".join("?" for _ in identifiers)
    return f"{column} IN ({placeholders})", list(identifiers)


def where(scope: Scope, column: str = "organization_id") -> tuple[str, list]:
    """`sql_filter`, rendered as a leading `WHERE` clause."""
    predicate, params = sql_filter(scope, column)
    return (f" WHERE {predicate}" if predicate else ""), params


def and_where(scope: Scope, column: str = "organization_id") -> tuple[str, list]:
    """`sql_filter`, rendered to follow an existing `WHERE`."""
    predicate, params = sql_filter(scope, column)
    return (f" AND {predicate}" if predicate else ""), params


class OutOfScope(HTTPException):
    """Deliberately a 404.

    Answering 403 would confirm the record exists, which lets an outsider
    enumerate client ids by watching which ones answer differently. From outside
    this boundary, "you may not see it" and "there is no such thing" have to be
    the same answer.
    """

    def __init__(self, what: str = "Not found"):
        super().__init__(404, what)


def require_organization(scope: Scope, organization_id: str | None,
                         what: str = "Not found") -> str:
    if not scope.allows(organization_id):
        raise OutOfScope(what)
    return str(organization_id)


def organization_of_run(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT organization_id FROM bookkeeping_runs WHERE id = ?",
        (run_id,)).fetchone()
    return str(row[0]) if row else None


def organization_of_proposal(conn: sqlite3.Connection, proposal_id: str) -> str | None:
    row = conn.execute(
        "SELECT r.organization_id FROM bookkeeping_proposals p "
        "JOIN bookkeeping_runs r ON r.id = p.run_id WHERE p.id = ?",
        (proposal_id,)).fetchone()
    return str(row[0]) if row else None


def organization_of_engagement(conn: sqlite3.Connection, engagement_id: str) -> str | None:
    row = conn.execute(
        "SELECT organization_id FROM engagements WHERE id = ?",
        (engagement_id,)).fetchone()
    return str(row[0]) if row else None


# ----------------------------------------------------------- administration --

def create_firm(conn: sqlite3.Connection, firm_id: str, name: str) -> str:
    conn.execute(
        "INSERT INTO firms(id, name, normalized_name) VALUES(?,?,?)",
        (firm_id, name, _normalized(name)))
    return firm_id


def add_member(conn: sqlite3.Connection, firm_id: str, user_id: str,
               firm_role: str = "staff") -> None:
    if firm_role not in {"principal", "staff"}:
        raise ValueError("A firm member is a principal or staff")
    conn.execute(
        "INSERT INTO firm_members(firm_id, user_id, firm_role) VALUES(?,?,?)",
        (firm_id, user_id, firm_role))


def create_firm_user(conn: sqlite3.Connection, firm_id: str, *, email: str,
                     display_name: str, password: str,
                     firm_role: str = "staff",
                     workspace_role: str = "viewer") -> str:
    """Create a person at a firm, and refuse to leave them unscoped.

    The one provisioning mistake that matters: creating an accountant, giving
    them a workspace role so they can do their job, and forgetting the firm
    membership. They would then resolve as internal and see every client in the
    system. Membership is written first and the resulting scope is checked
    before the transaction is allowed to stand, so that mistake cannot be made
    by forgetting a step.
    """
    from . import auth

    user_id = auth.create_user(conn, email, display_name, password, workspace_role)
    add_member(conn, firm_id, user_id, firm_role)
    scope = scope_for(conn, {"user_id": user_id, "roles": {workspace_role}})
    if scope.kind != FIRM or scope.firm_id != firm_id:
        raise RuntimeError(
            f"{email} would not be confined to firm {firm_id}; refusing to "
            "create an account that can see every client.")
    return user_id


def add_client(conn: sqlite3.Connection, firm_id: str, organization_id: str,
               *, alongside: bool = False) -> None:
    """Engage a firm for a client.

    More than one firm may hold one client. A bookkeeper monthly and a CPA at
    year end is the ordinary Canadian arrangement rather than an anomaly, and
    migration 020 removed the `UNIQUE (organization_id)` that refused it.

    That constraint was also doing a second job: catching the typo that hands a
    client's books to a firm that was never engaged for them. Permitting the
    arrangement should not silently permit the accident, so the refusal moves
    here, where it can tell the two apart -- a second firm has to be asked for
    in as many words.
    """
    others = [str(row[0]) for row in conn.execute(
        "SELECT firm_id FROM firm_clients "
        "WHERE organization_id=? AND firm_id<>?",
        (organization_id, firm_id)).fetchall()]
    if others and not alongside:
        raise ValueError(
            "That client is already held by " + ", ".join(sorted(others))
            + ". Two firms holding one client is a real arrangement -- a "
            "bookkeeper and an accountant -- but it is also what a mistyped "
            "client id looks like. Pass alongside=True to mean it.")
    conn.execute(
        "INSERT INTO firm_clients(firm_id, organization_id) VALUES(?,?)",
        (firm_id, organization_id))


def assign(conn: sqlite3.Connection, firm_id: str, user_id: str,
           organization_id: str) -> None:
    """Give one staff member access to one client the firm holds.

    Refuses when the firm does not hold the client. An assignment that grants
    access to books the firm was never engaged for is the same disclosure as no
    boundary at all, and it would be created by a typo.
    """
    held = conn.execute(
        "SELECT 1 FROM firm_clients WHERE firm_id=? AND organization_id=?",
        (firm_id, organization_id)).fetchone()
    if not held:
        raise ValueError(
            "That client is not held by this firm, so it cannot be assigned to "
            "one of its people.")
    conn.execute(
        "INSERT OR IGNORE INTO firm_client_assignments("
        "firm_id, user_id, organization_id) VALUES(?,?,?)",
        (firm_id, user_id, organization_id))


def unassign(conn: sqlite3.Connection, firm_id: str, user_id: str,
             organization_id: str) -> None:
    conn.execute(
        "DELETE FROM firm_client_assignments "
        "WHERE firm_id=? AND user_id=? AND organization_id=?",
        (firm_id, user_id, organization_id))


def _normalized(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())
