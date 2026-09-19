"""Create the first admin user without putting a password in argv or logs."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
from shimline import auth


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    password = getpass.getpass("Admin password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise SystemExit("No password received on stdin")
    conn = app._db()
    try:
        if conn.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (args.email,)).fetchone():
            raise SystemExit("That admin user already exists")
        auth.create_user(conn, args.email, args.name, password, "owner")
    finally:
        conn.close()
        password = ""
    print("Admin user created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

