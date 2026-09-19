"""Tests for the bank-statement rail.

The point of this rail is narrow and worth stating: checks 04 and E2 were
written, tested against synthetic evidence, and unable to run in production
because nothing could put a real statement into the system. These tests cover
the parsing, the storage, and -- the part that actually matters -- the rule that
an unmapped statement must not be allowed to reconcile anything.
"""
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY", "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import crm, statement_store, statements, work_engine  # noqa: E402
from shimline.qbo_adapter import declare_pull  # noqa: E402

# A realistic Canadian chequing export: OFX 1.0.2 SGML, unclosed tags, a
# timezone-suffixed DTSERVER, NAME on every line and MEMO on only some.
QFX = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>20260831120000[-5:EST]<LANGUAGE>ENG
<FI><ORG>RBC<FID>1001</FI></SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>CAD
<BANKACCTFROM><BANKID>003<ACCTID>1234567<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260801<DTEND>20260831
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260805<TRNAMT>-1250.00<FITID>202608050001<NAME>HOME DEPOT #7021<MEMO>LUMBER</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260812<TRNAMT>8400.00<FITID>202608120002<NAME>DEPOSIT CLIENT ABC</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260820<TRNAMT>-431.75<FITID>202608200003<NAME>PETRO-CANADA<MEMO>FUEL</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>6718.25<DTASOF>20260831</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

CSV_WITH_BALANCE = (
    b"Date,Description,Withdrawals,Deposits,Balance\r\n"
    b"2026-08-05,HOME DEPOT #7021,1250.00,,5568.25\r\n"
    b"2026-08-12,DEPOSIT CLIENT ABC,,8400.00,13968.25\r\n"
    b"2026-08-20,PETRO-CANADA,431.75,,13536.50\r\n"
)

CSV_NO_BALANCE = (
    b"Date,Description,Amount\r\n"
    b"2026-08-05,HOME DEPOT,-1250.00\r\n"
    b"2026-08-12,DEPOSIT,8400.00\r\n"
)


class ParsingTests(unittest.TestCase):
    def test_qfx_yields_decimal_money_and_the_banks_own_identifiers(self):
        parsed = statements.parse(QFX, "rbc-august.qfx")
        self.assertEqual(parsed.source_format, "qfx")
        self.assertEqual(parsed.bank_account_id, "1234567")
        self.assertEqual(parsed.routing_number, "003")
        self.assertEqual(parsed.currency, "CAD")
        self.assertEqual(parsed.period_start, date(2026, 8, 1))
        self.assertEqual(parsed.period_end, date(2026, 8, 31))
        self.assertEqual(parsed.closing_balance, Decimal("6718.25"))
        self.assertEqual(parsed.line_count, 3)

        # Money must never arrive as float. A binary-rounded cent here is a
        # wrong set of books downstream.
        for line in parsed.lines:
            self.assertIsInstance(line.amount, Decimal)
        self.assertIsInstance(parsed.closing_balance, Decimal)

        first = parsed.lines[0]
        self.assertEqual(first.posted_date, date(2026, 8, 5))
        self.assertEqual(first.amount, Decimal("-1250.00"))
        self.assertEqual(first.description, "HOME DEPOT #7021")
        self.assertEqual(first.memo, "LUMBER")
        self.assertEqual(first.fitid, "202608050001")
        # The bank sends no MEMO on the deposit; absent must stay absent.
        self.assertEqual(parsed.lines[1].memo, "")

    def test_signs_are_preserved_so_direction_is_never_inferred(self):
        parsed = statements.parse(QFX, "rbc.qfx")
        self.assertEqual([line.amount for line in parsed.lines],
                         [Decimal("-1250.00"), Decimal("8400.00"), Decimal("-431.75")])
        self.assertEqual(parsed.net_movement(), Decimal("6718.25"))

    def test_format_is_detected_from_content_not_from_the_extension(self):
        # Banks serve QFX content under every extension imaginable.
        self.assertEqual(statements.detect_format("statement.txt", QFX), "ofx")
        self.assertEqual(statements.detect_format("statement.qfx", QFX), "qfx")
        self.assertEqual(statements.detect_format("export.csv", CSV_WITH_BALANCE), "csv")

    def test_csv_with_split_debit_credit_columns_is_signed_correctly(self):
        parsed = statements.parse(CSV_WITH_BALANCE, "td-august.csv")
        self.assertEqual(parsed.source_format, "csv")
        self.assertEqual([line.amount for line in parsed.lines],
                         [Decimal("-1250.00"), Decimal("8400.00"), Decimal("-431.75")])
        self.assertEqual(parsed.closing_balance, Decimal("13536.50"))
        self.assertEqual(parsed.period_start, date(2026, 8, 5))
        self.assertEqual(parsed.period_end, date(2026, 8, 20))

    def test_csv_without_a_balance_column_is_refused_rather_than_guessed(self):
        # A statement whose closing balance we invented would reconcile against
        # nothing while looking like a clean result. Refusing is the point.
        with self.assertRaises(statements.StatementError) as caught:
            statements.parse(CSV_NO_BALANCE, "export.csv")
        self.assertIn("closing balance", str(caught.exception))

    def test_a_multi_account_file_is_refused_rather_than_silently_split(self):
        two = QFX.replace(
            b"</STMTRS></STMTTRNRS></BANKMSGSRSV1>",
            b"</STMTRS></STMTTRNRS>"
            b"<STMTTRNRS><TRNUID>2<STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            b"<STMTRS><CURDEF>CAD"
            b"<BANKACCTFROM><BANKID>003<ACCTID>7654321<ACCTTYPE>SAVINGS</BANKACCTFROM>"
            b"<BANKTRANLIST><DTSTART>20260801<DTEND>20260831"
            b"<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260806<TRNAMT>10.00<FITID>X1<NAME>INTEREST</STMTTRN>"
            b"</BANKTRANLIST>"
            b"<LEDGERBAL><BALAMT>10.00<DTASOF>20260831</LEDGERBAL>"
            b"</STMTRS></STMTTRNRS></BANKMSGSRSV1>")
        with self.assertRaises(statements.StatementError) as caught:
            statements.parse(two, "both.qfx")
        self.assertIn("one account per file", str(caught.exception).lower())

    def test_unreadable_input_raises_instead_of_returning_an_empty_statement(self):
        for payload, name in ((b"", "empty.qfx"), (b"not a statement at all", "junk.dat")):
            with self.assertRaises(statements.StatementError):
                statements.parse(payload, name)

    def test_the_content_hash_is_over_the_exact_uploaded_bytes(self):
        parsed = statements.parse(QFX, "a.qfx")
        self.assertEqual(parsed.content_sha256, statements.content_hash(QFX))
        self.assertNotEqual(statements.parse(QFX + b"\n", "b.qfx").content_sha256,
                            parsed.content_sha256)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "statements.db"
        self.conn = service._db()
        self.org = crm.new_id("org")
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                          (self.org, "Statement Co", self.org))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_statement_and_lines_persist_with_order_preserved(self):
        stored, parsed = statement_store.ingest(
            self.conn, organization_id=self.org, data=QFX, filename="rbc.qfx")
        self.assertFalse(stored.already_present)
        rows = statement_store.lines(self.conn, stored.id)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["ordinal"] for row in rows], [0, 1, 2])
        self.assertEqual(rows[0]["amount"], Decimal("-1250.00"))
        self.assertEqual(rows[0]["fitid"], "202608050001")
        self.assertEqual(parsed.closing_balance, Decimal("6718.25"))

    def test_reimporting_the_same_file_is_a_no_op_not_a_doubled_ledger(self):
        first, _ = statement_store.ingest(
            self.conn, organization_id=self.org, data=QFX, filename="rbc.qfx")
        second, _ = statement_store.ingest(
            self.conn, organization_id=self.org, data=QFX, filename="rbc-again.qfx")
        self.assertTrue(second.already_present)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(statement_store.lines(self.conn, first.id)), 3)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM bank_statements WHERE organization_id=?",
            (self.org,)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_a_modified_file_is_not_mistaken_for_the_one_already_held(self):
        statement_store.ingest(self.conn, organization_id=self.org, data=QFX, filename="a.qfx")
        tampered = QFX.replace(b"<BALAMT>6718.25", b"<BALAMT>9999.99")
        stored, _ = statement_store.ingest(
            self.conn, organization_id=self.org, data=tampered, filename="a.qfx")
        self.assertFalse(stored.already_present)


class EvidenceGateTests(unittest.TestCase):
    """The rule this rail exists to enforce."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "evidence.db"
        self.conn = service._db()
        self.org = crm.new_id("org")
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                          (self.org, "Evidence Co", self.org))
        self.conn.commit()
        self.stored, _ = statement_store.ingest(
            self.conn, organization_id=self.org, data=QFX, filename="rbc.qfx")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _evidence(self):
        return statement_store.evidence(
            self.conn, self.org,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31))

    def test_an_unmapped_statement_is_not_evidence(self):
        # Held, readable, and deliberately withheld: it proves nothing about the
        # ledger until someone says which ledger account it belongs to.
        self.assertEqual(self._evidence(), {})
        self.assertEqual(statement_store.unmapped_count(self.conn, self.org), 1)

    def test_mapping_the_account_turns_it_into_evidence(self):
        statement_store.map_account(self.conn, self.stored.id, "101")
        evidence = self._evidence()
        self.assertEqual(len(evidence["bank_statements"]), 1)
        record = evidence["bank_statements"][0]
        self.assertEqual(record["account_id"], "101")
        self.assertEqual(record["ending_ledger_balance"], "6718.25")
        self.assertEqual(record["source"]["content_sha256"], statements.content_hash(QFX))
        self.assertEqual(statement_store.unmapped_count(self.conn, self.org), 0)

    def test_withdrawing_a_mapping_reverts_to_no_evidence(self):
        statement_store.map_account(self.conn, self.stored.id, "101")
        statement_store.map_account(self.conn, self.stored.id, None)
        self.assertEqual(self._evidence(), {})

    def test_a_statement_outside_the_period_is_not_offered_as_evidence(self):
        statement_store.map_account(self.conn, self.stored.id, "101")
        outside = statement_store.evidence(
            self.conn, self.org,
            period_start=date(2027, 1, 1), period_end=date(2027, 12, 31))
        self.assertEqual(outside, {})

    def test_evidence_never_claims_a_transaction_is_missing(self):
        # E2 generates a create-expense proposal from `missing_transactions`.
        # Populating that without a real matcher would manufacture writes from
        # unmatched noise, so this rail must leave it alone.
        statement_store.map_account(self.conn, self.stored.id, "101")
        for record in self._evidence()["bank_statements"]:
            self.assertNotIn("missing_transactions", record)


class EngineIntegrationTests(unittest.TestCase):
    """Checks 04 and E2 stop being blocked, and reconciliation stops saying no_source."""

    LEDGER = {
        "Account": [{"Id": "101", "Name": "Chequing", "AccountType": "Bank"}],
        "Purchase": [],
        "Deposit": [],
        "Payment": [],
        "Invoice": [],
    }

    def _coverage(self, evidence):
        analysis = work_engine.analyze(declare_pull(dict(self.LEDGER)), evidence, today=date(2026, 8, 31))
        return {item["id"]: item for item in analysis.coverage["checks"]}, analysis

    def test_without_a_statement_the_checks_report_blocked(self):
        coverage, analysis = self._coverage(
            {"period_start": "2026-08-01", "period_end": "2026-08-31"})
        self.assertEqual(coverage["04"]["status"], "blocked")
        self.assertEqual(coverage["04"]["missing_source"], "bank_statement")
        self.assertEqual(coverage["E2"]["status"], "blocked")
        self.assertIn("Bank statement covering the review period",
                      analysis.coverage["missing_evidence"])

    def test_with_a_statement_the_checks_run_and_a_difference_is_a_finding(self):
        evidence = {
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "bank_statements": [{
                "account_id": "101", "ending_ledger_balance": "6718.25",
                "period_end": "2026-08-31",
            }],
        }
        coverage, analysis = self._coverage(evidence)
        # The ledger holds nothing, so the statement balance cannot be matched.
        self.assertEqual(coverage["04"]["status"], "defect")
        self.assertNotIn("Bank statement covering the review period",
                         analysis.coverage["missing_evidence"])
        finding = next(item for item in analysis.findings
                       if item.defect_type == "unreconciled_account")
        self.assertEqual(finding.affected_id, "101")
        self.assertEqual(finding.financial_effect, Decimal("6718.25"))

    def test_reconcile_reports_the_account_instead_of_no_source(self):
        evidence = {
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "bank_statements": [{
                "account_id": "101", "ending_ledger_balance": "0",
                "period_end": "2026-08-31",
            }],
        }
        records = work_engine.reconcile(declare_pull(dict(self.LEDGER)), evidence)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "reconciled")
        self.assertEqual(records[0]["account_id"], "101")

    def test_every_statement_account_is_checked_not_only_the_first(self):
        # The supported envelope allows four accounts. Checking one and calling
        # the run clean would understate the other three.
        ledger = {
            "Account": [{"Id": "101"}, {"Id": "102"}, {"Id": "103"}],
            "Purchase": [], "Deposit": [], "Payment": [], "Invoice": [],
        }
        evidence = {
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "bank_statements": [
                {"account_id": "101", "ending_ledger_balance": "0"},
                {"account_id": "102", "ending_ledger_balance": "500.00"},
                {"account_id": "103", "ending_ledger_balance": "250.00"},
            ],
        }
        analysis = work_engine.analyze(declare_pull(ledger), evidence, today=date(2026, 8, 31))
        flagged = {item.affected_id for item in analysis.findings
                   if item.defect_type == "unreconciled_account"}
        self.assertEqual(flagged, {"102", "103"})

    def test_a_statement_never_reconciles_against_an_underivable_ledger(self):
        """The failure this rail could most easily have introduced.

        The live QBO adapter returns provider objects without `_Postings`, so
        trial_balance() computes {} and every account reads as zero. Comparing a
        real statement balance against that would put a high-severity
        unreconciled-account finding on every account of every real client. It
        has to block instead.
        """
        live_shaped = {
            "Account": [{"Id": "101", "Name": "Chequing", "AccountType": "Bank"}],
            # A purchase as the provider actually sends it: no _Postings.
            "Purchase": [{"Id": "P1", "TotalAmt": "1250.00", "Line": [
                {"Amount": 1250.0,
                 "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "500"}}}]}],
            "Deposit": [], "Payment": [], "Invoice": [],
        }
        self.assertFalse(work_engine.postings_derivable(live_shaped))

        evidence = {
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "bank_statements": [{"account_id": "101",
                                 "ending_ledger_balance": "6718.25"}],
        }
        analysis = work_engine.analyze(declare_pull(live_shaped), evidence, today=date(2026, 8, 31))
        self.assertEqual(
            [f for f in analysis.findings if f.defect_type == "unreconciled_account"], [])
        check = next(c for c in analysis.coverage["checks"] if c["id"] == "04")
        self.assertEqual(check["status"], "blocked")
        self.assertEqual(check["missing_source"], "derived_ledger_postings")

        records = work_engine.reconcile(declare_pull(live_shaped), evidence)
        self.assertEqual(records[0]["status"], "no_source")
        self.assertIn("Ledger postings", records[0]["missing_source"])

    def test_a_company_with_no_transactions_still_reconciles_honestly(self):
        # Distinct from the case above: zero really is the balance here, so a
        # statement that disagrees is a genuine exception, not a blocked check.
        empty = {"Account": [{"Id": "101"}], "Purchase": [], "Deposit": [],
                 "Payment": [], "Invoice": []}
        self.assertTrue(work_engine.postings_derivable(empty))
        evidence = {
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "bank_statements": [{"account_id": "101",
                                 "ending_ledger_balance": "6718.25"}],
        }
        analysis = work_engine.analyze(declare_pull(empty), evidence, today=date(2026, 8, 31))
        self.assertEqual(
            next(c for c in analysis.coverage["checks"] if c["id"] == "04")["status"],
            "defect")

    def test_the_singular_evidence_key_still_works(self):
        # The synthetic oracle and the 50-company acceptance suite use it.
        evidence = {
            "period_start": "2026-08-01", "period_end": "2026-08-31",
            "bank_statement": {"account_id": "101", "ending_ledger_balance": "42.00"},
        }
        coverage, analysis = self._coverage(evidence)
        self.assertEqual(coverage["04"]["status"], "defect")
        self.assertEqual(work_engine.statements(evidence)[0]["account_id"], "101")


class EndToEndTests(unittest.TestCase):
    """Upload through to a firing check, with no synthetic evidence anywhere."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "e2e.db"
        self.conn = service._db()
        self.org = crm.new_id("org")
        self.conn.execute("INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                          (self.org, "End To End Co", self.org))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_a_real_qfx_upload_unblocks_check_04(self):
        ledger = {"Account": [{"Id": "101"}], "Purchase": [], "Deposit": [],
                  "Payment": [], "Invoice": []}
        period = dict(period_start=date(2026, 8, 1), period_end=date(2026, 8, 31))

        base = {"period_start": "2026-08-01", "period_end": "2026-08-31"}
        before = work_engine.analyze(declare_pull(dict(ledger)), dict(base), today=date(2026, 8, 31))
        self.assertEqual(
            next(c for c in before.coverage["checks"] if c["id"] == "04")["status"],
            "blocked")

        stored, _ = statement_store.ingest(
            self.conn, organization_id=self.org, data=QFX, filename="rbc-august.qfx")
        statement_store.map_account(self.conn, stored.id, "101")

        evidence = dict(base)
        evidence.update(statement_store.evidence(self.conn, self.org, **period))
        after = work_engine.analyze(declare_pull(dict(ledger)), evidence, today=date(2026, 8, 31))

        self.assertEqual(
            next(c for c in after.coverage["checks"] if c["id"] == "04")["status"],
            "defect")
        records = work_engine.reconcile(declare_pull(dict(ledger)), evidence)
        self.assertEqual([r["status"] for r in records], ["exception"])
        self.assertEqual(records[0]["statement_balance"], "6718.25")


if __name__ == "__main__":
    unittest.main()
