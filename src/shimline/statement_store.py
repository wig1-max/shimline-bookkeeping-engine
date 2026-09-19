"""Persist bank statements and hand them to the work engine as evidence.

The engine already knows what to do with a statement. `work_engine.reconcile`
reads `evidence["bank_statements"]`, compares each statement's closing balance
against the reconstructed ledger balance for that account, and records
`reconciled`, `exception`, or `no_source`. Check 04 fires off the same data.
None of that has ever run against a real statement, because nothing populated
the evidence key.

This module is that missing link, and it holds one rule that matters more than
the rest: **an unmapped statement is not evidence.** The engine keys ledger
balances by QBO account id. A statement whose bank account has not been mapped
to a QBO account cannot prove anything about the ledger, so it is withheld from
the evidence dict entirely and reconciliation continues to report `no_source`.
Reconciling it against a guessed account would turn a missing control into a
passing one, which is the single worst outcome available here.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from . import crm
from .statements import ParsedStatement, StatementError


@dataclass(frozen=True)
class StoredStatement:
    id: str
    already_present: bool


def store(conn: sqlite3.Connection, *, organization_id: str,
          statement: ParsedStatement, engagement_id: str | None = None,
          qbo_account_id: str | None = None,
          imported_by: str | None = None) -> StoredStatement:
    """Persist a parsed statement. Re-importing the same bytes is a no-op.

    Idempotency is by SHA-256 of the uploaded file, per organisation. A client
    who uploads January twice gets one statement, not two, and not a doubled set
    of lines feeding the reconciliation.
    """
    existing = conn.execute(
        "SELECT id FROM bank_statements WHERE organization_id=? AND content_sha256=?",
        (organization_id, statement.content_sha256)).fetchone()
    if existing:
        return StoredStatement(id=existing[0], already_present=True)

    statement_id = crm.new_id("bst")
    conn.execute(
        "INSERT INTO bank_statements("
        "id,organization_id,engagement_id,bank_account_id,routing_number,account_type,"
        "currency,qbo_account_id,period_start,period_end,closing_balance,"
        "source_filename,source_format,content_sha256,line_count,imported_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (statement_id, organization_id, engagement_id, statement.bank_account_id,
         statement.routing_number, statement.account_type, statement.currency,
         qbo_account_id, statement.period_start.isoformat(),
         statement.period_end.isoformat(), str(statement.closing_balance),
         statement.source_filename, statement.source_format,
         statement.content_sha256, statement.line_count, imported_by))

    for ordinal, line in enumerate(statement.lines):
        conn.execute(
            "INSERT INTO bank_statement_lines("
            "id,statement_id,posted_date,amount,description,memo,txn_type,fitid,ordinal) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (crm.new_id("bsl"), statement_id, line.posted_date.isoformat(),
             str(line.amount), line.description, line.memo, line.txn_type,
             line.fitid, ordinal))
    return StoredStatement(id=statement_id, already_present=False)


def map_account(conn: sqlite3.Connection, statement_id: str,
                qbo_account_id: str | None) -> None:
    """Point a statement at the QBO account it proves, or unpoint it.

    Setting this to None is legitimate: an operator who realises the mapping was
    wrong should be able to withdraw it, and the effect is that reconciliation
    reverts to `no_source` rather than continuing to assert a bad match.
    """
    conn.execute("UPDATE bank_statements SET qbo_account_id=? WHERE id=?",
                 (qbo_account_id, statement_id))


def statements_for_period(conn: sqlite3.Connection, organization_id: str, *,
                          period_start: date, period_end: date,
                          mapped_only: bool = True) -> list[dict]:
    """Statements overlapping the review period, newest period first."""
    sql = ("SELECT id,bank_account_id,qbo_account_id,currency,period_start,period_end,"
           "closing_balance,source_format,source_filename,content_sha256,line_count "
           "FROM bank_statements WHERE organization_id=? "
           "AND period_end>=? AND period_start<=?")
    params: list = [organization_id, period_start.isoformat(), period_end.isoformat()]
    if mapped_only:
        sql += " AND qbo_account_id IS NOT NULL AND qbo_account_id<>''"
    sql += " ORDER BY period_end DESC, id"
    rows = conn.execute(sql, params).fetchall()
    return [{
        "id": row[0], "bank_account_id": row[1], "qbo_account_id": row[2],
        "currency": row[3], "period_start": row[4], "period_end": row[5],
        "closing_balance": row[6], "source_format": row[7],
        "source_filename": row[8], "content_sha256": row[9], "line_count": row[10],
    } for row in rows]


def lines(conn: sqlite3.Connection, statement_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT posted_date,amount,description,memo,txn_type,fitid,ordinal "
        "FROM bank_statement_lines WHERE statement_id=? ORDER BY ordinal",
        (statement_id,)).fetchall()
    return [{
        "posted_date": row[0], "amount": Decimal(row[1]), "description": row[2],
        "memo": row[3], "txn_type": row[4], "fitid": row[5], "ordinal": row[6],
    } for row in rows]


def evidence(conn: sqlite3.Connection, organization_id: str, *,
             period_start: date, period_end: date) -> dict:
    """Build the `bank_statements` evidence the work engine already consumes.

    Returns `{}` when there is nothing mapped, so that a caller can merge this
    unconditionally and the engine's existing `no_source` path stays in charge
    of saying so. Absence of a key and an empty list mean the same thing to
    `reconcile`, and absence keeps the evidence dict honest about what was
    actually supplied.

    `missing_transactions` is still deliberately NOT populated here. That key is
    the engine's *corroborated* path: a line the statement and a source document
    both attest to, which is strong enough to propose a create-expense against.
    Nothing here can meet that bar.

    What is supplied instead is `lines` -- the statement as parsed, nothing
    inferred. The engine matches those against the reconstructed ledger itself
    and reports the ones nothing accounts for, without proposing a correction,
    because a bank descriptor does not say which expense account a charge
    belongs to. Handing over the raw lines and letting the engine decide keeps
    the judgement in one place rather than splitting it across two modules.
    """
    mapped = statements_for_period(
        conn, organization_id, period_start=period_start, period_end=period_end)
    if not mapped:
        return {}

    records = []
    for row in mapped:
        records.append({
            "statement_id": row["id"],
            "account_id": row["qbo_account_id"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "ending_ledger_balance": row["closing_balance"],
            "currency": row["currency"],
            "lines": [dict(line, amount=str(line["amount"]))
                      for line in lines(conn, row["id"])],
            "source": {
                "format": row["source_format"],
                "filename": row["source_filename"],
                "content_sha256": row["content_sha256"],
                "line_count": row["line_count"],
            },
        })
    return {"bank_statements": records}


def unmapped_count(conn: sqlite3.Connection, organization_id: str) -> int:
    """Statements held but not yet pointed at a ledger account.

    Surfaced so an operator can see that evidence was supplied and is not being
    used, rather than wondering why reconciliation still says `no_source`.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM bank_statements WHERE organization_id=? "
        "AND (qbo_account_id IS NULL OR qbo_account_id='')",
        (organization_id,)).fetchone()
    return int(row[0]) if row else 0


def ingest(conn: sqlite3.Connection, *, organization_id: str, data: bytes,
           filename: str = "", engagement_id: str | None = None,
           qbo_account_id: str | None = None,
           imported_by: str | None = None) -> tuple[StoredStatement, ParsedStatement]:
    """Parse and persist in one step. Raises StatementError on unreadable input."""
    from .statements import parse
    parsed = parse(data, filename)
    if not parsed.lines:
        raise StatementError("The statement contains no transactions.")
    stored = store(conn, organization_id=organization_id, statement=parsed,
                   engagement_id=engagement_id, qbo_account_id=qbo_account_id,
                   imported_by=imported_by)
    return stored, parsed
