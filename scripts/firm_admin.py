"""Create accounting firms and place their people, from the operator's shell.

Firm tenancy is a security boundary, and until something can create a firm the
boundary is code nobody can reach. This is that something. It is deliberately a
script and not a console page: the actions here hand one firm access to a
client's books, they happen a handful of times per firm, and an operator running
them at a prompt with the audit trail in front of them is the right shape for
that until there is a reason it is not.

Every command is idempotent where it sensibly can be and refuses where it
cannot. Two rules are enforced here rather than left to the operator to
remember:

* A person is created *into* a firm. `tenancy.create_firm_user` writes the
  membership and then checks the resulting scope before letting the transaction
  stand, so the one mistake that matters -- an accountant with a workspace role
  and no firm, who would see every client in the system -- cannot be made by
  forgetting a step.
* A client can only be assigned to someone at the firm that holds them.

Usage
-----
    python scripts/firm_admin.py list
    python scripts/firm_admin.py create-firm "Alder & Co"
    python scripts/firm_admin.py add-client firm_xxx org_yyy
    python scripts/firm_admin.py add-client firm_xxx org_yyy --alongside
    python scripts/firm_admin.py add-person firm_xxx dana@alder.example "Dana Alder" \
        --role principal
    python scripts/firm_admin.py assign firm_xxx usr_zzz org_yyy
    python scripts/firm_admin.py suspend firm_xxx
    python scripts/firm_admin.py show firm_xxx

A password is never taken as an argument -- it would land in the shell history
and in any process listing. `add-person` prompts for one, and prints nothing
back.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("SHIMLINE_SECRET_KEY", "")


def _connect(db_path: str | None):
    """Open the live database, or one named explicitly."""
    import sqlite3

    from shimline import db as shimline_db

    path = db_path or os.environ.get("SHIMLINE_DB_PATH") or "intake.db"
    if not Path(path).exists():
        raise SystemExit(
            f"No database at {path}. Pass --db, or set SHIMLINE_DB_PATH.")
    conn = sqlite3.connect(path)
    shimline_db.configure_connection(conn)
    return conn


def _firms(conn):
    return conn.execute(
        "SELECT f.id, f.name, f.status, "
        "(SELECT COUNT(*) FROM firm_clients c WHERE c.firm_id = f.id), "
        "(SELECT COUNT(*) FROM firm_members m WHERE m.firm_id = f.id) "
        "FROM firms f ORDER BY f.name").fetchall()


def cmd_list(conn, args) -> int:
    rows = _firms(conn)
    if not rows:
        print("No firms yet.")
        return 0
    print(f"{'id':<24} {'name':<30} {'status':<10} clients  people")
    for firm_id, name, status, clients, people in rows:
        print(f"{firm_id:<24} {name:<30} {status:<10} {clients:>7}  {people:>6}")
    return 0


def cmd_show(conn, args) -> int:
    firm = conn.execute("SELECT id, name, status FROM firms WHERE id=?",
                        (args.firm_id,)).fetchone()
    if not firm:
        raise SystemExit(f"No firm {args.firm_id}")
    print(f"{firm[1]}  ({firm[0]}, {firm[2]})\n")

    print("Clients held:")
    clients = conn.execute(
        "SELECT o.id, o.name FROM firm_clients c "
        "JOIN organizations o ON o.id = c.organization_id "
        "WHERE c.firm_id=? ORDER BY o.name", (args.firm_id,)).fetchall()
    for ident, name in clients:
        print(f"  {ident:<24} {name}")
    if not clients:
        print("  (none)")

    print("\nPeople, and what each can open:")
    people = conn.execute(
        "SELECT u.id, u.email, m.firm_role FROM firm_members m "
        "JOIN users u ON u.id = m.user_id WHERE m.firm_id=? ORDER BY u.email",
        (args.firm_id,)).fetchall()
    for user_id, email, firm_role in people:
        if firm_role == "principal":
            visible = f"every client the firm holds ({len(clients)})"
        else:
            assigned = conn.execute(
                "SELECT COUNT(*) FROM firm_client_assignments a "
                "JOIN firm_clients c ON c.firm_id = a.firm_id "
                "                   AND c.organization_id = a.organization_id "
                "WHERE a.firm_id=? AND a.user_id=?",
                (args.firm_id, user_id)).fetchone()[0]
            visible = f"{assigned} assigned client(s)"
        print(f"  {user_id:<24} {email:<34} {firm_role:<10} {visible}")
    if not people:
        print("  (none)")
    return 0


def cmd_create_firm(conn, args) -> int:
    from shimline import crm, tenancy

    firm_id = crm.new_id("frm")
    tenancy.create_firm(conn, firm_id, args.name)
    conn.commit()
    print(f"Created {args.name} as {firm_id}")
    return 0


def cmd_add_client(conn, args) -> int:
    from shimline import tenancy

    row = conn.execute("SELECT name FROM organizations WHERE id=?",
                       (args.organization_id,)).fetchone()
    if not row:
        raise SystemExit(f"No client {args.organization_id}")
    try:
        tenancy.add_client(conn, args.firm_id, args.organization_id,
                           alongside=args.alongside)
    except ValueError as refusal:
        raise SystemExit(str(refusal)) from None
    conn.commit()
    print(f"{row[0]} is now held by {args.firm_id}.")
    print("Principals can open it immediately. Staff still need an assignment.")
    return 0


def cmd_add_person(conn, args) -> int:
    from shimline import tenancy

    # Never an argument: it would land in the shell history and in any process
    # listing on a shared host.
    password = getpass.getpass(f"Password for {args.email}: ")
    if len(password) < 12:
        raise SystemExit("Refusing a password under 12 characters.")
    if password != getpass.getpass("Again: "):
        raise SystemExit("They did not match. Nothing was created.")

    user_id = tenancy.create_firm_user(
        conn, args.firm_id, email=args.email, display_name=args.display_name,
        password=password, firm_role=args.role, workspace_role=args.workspace_role)
    conn.commit()
    print(f"Created {args.email} as {user_id} ({args.role}).")
    if args.role == "staff":
        print("They can open nothing yet. Assign clients with: "
              f"firm_admin.py assign {args.firm_id} {user_id} <organization_id>")
    return 0


def cmd_assign(conn, args) -> int:
    from shimline import tenancy

    tenancy.assign(conn, args.firm_id, args.user_id, args.organization_id)
    conn.commit()
    print(f"{args.user_id} can now open {args.organization_id}.")
    return 0


def cmd_unassign(conn, args) -> int:
    from shimline import tenancy

    tenancy.unassign(conn, args.firm_id, args.user_id, args.organization_id)
    conn.commit()
    print(f"{args.user_id} can no longer open {args.organization_id}.")
    return 0


def cmd_suspend(conn, args) -> int:
    status = "suspended" if args.command == "suspend" else "active"
    changed = conn.execute("UPDATE firms SET status=?, "
                           "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (status, args.firm_id)).rowcount
    if not changed:
        raise SystemExit(f"No firm {args.firm_id}")
    conn.commit()
    print(f"{args.firm_id} is {status}.")
    if status == "suspended":
        print("Its people can open nothing. Its rows are kept, so who could "
              "see which books, and when, stays answerable.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", help="database path (default: the live one)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="every firm, with its counts")

    show = sub.add_parser("show", help="one firm, and who can open what")
    show.add_argument("firm_id")

    create = sub.add_parser("create-firm")
    create.add_argument("name")

    client = sub.add_parser("add-client", help="the firm is engaged for a client")
    client.add_argument("firm_id")
    client.add_argument("organization_id")
    client.add_argument(
        "--alongside", action="store_true",
        help="the client is already held by another firm and that is intended "
             "-- a bookkeeper and an accountant on the same books")

    person = sub.add_parser("add-person", help="create an account at a firm")
    person.add_argument("firm_id")
    person.add_argument("email")
    person.add_argument("display_name")
    person.add_argument("--role", choices=("principal", "staff"), default="staff")
    person.add_argument("--workspace-role", default="viewer",
                        choices=("viewer", "reviewer"),
                        help="reviewer is needed to approve a proposal")

    for name, help_text in (("assign", "let one person open one client"),
                            ("unassign", "take that access away")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("firm_id")
        command.add_argument("user_id")
        command.add_argument("organization_id")

    for name in ("suspend", "reinstate"):
        command = sub.add_parser(name)
        command.add_argument("firm_id")

    return parser


HANDLERS = {
    "list": cmd_list, "show": cmd_show, "create-firm": cmd_create_firm,
    "add-client": cmd_add_client, "add-person": cmd_add_person,
    "assign": cmd_assign, "unassign": cmd_unassign,
    "suspend": cmd_suspend, "reinstate": cmd_suspend,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = _connect(args.db)
    try:
        return HANDLERS[args.command](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
