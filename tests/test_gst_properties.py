"""Generated invariants for the GST/HST figures an accountant signs.

Fixtures prove examples somebody anticipated. These cases mix federal,
provincial, unknown and absent rates across ordinary sales, purchases and their
reversals, then shrink any counterexample before reporting it.
"""
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
import qbo_grammar  # noqa: E402
from shimline import filing_periods, gst_return, sales_tax, tax_rates  # noqa: E402

PERIOD_START = "2026-07-01"
PERIOD_END = "2026-09-30"
CASES = 300


def money(value=0) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def shown(case) -> str:
    return json.dumps(asdict(case), indent=2, sort_keys=True)


class GeneratedReturns(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "gst-properties.db"
        self.conn = service._db()
        self.serial = 0

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def evaluate(self, case):
        self.serial += 1
        org = f"org_gst_{self.serial}"
        run = f"run_gst_{self.serial}"
        account = f"acc_tax_{self.serial}"
        self.conn.execute(
            "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
            (org, org, org))
        self.conn.execute(
            "INSERT INTO bookkeeping_runs(id,organization_id,period_start,"
            "period_end,status) VALUES(?,?,?,?,?)",
            (run, org, PERIOD_START, PERIOD_END, "review"))
        self.conn.execute(
            "INSERT INTO bookkeeping_accounts(id,organization_id,provider,"
            "provider_id,name,account_type) VALUES(?,?,'quickbooks','tax',"
            "'GST/HST Payable','Other Current Liability')", (account, org))
        tax_rates.persist(self.conn, org, {
            "TaxAgency": qbo_grammar.GST_AGENCIES,
            "TaxRate": case.rates,
        })
        filing_periods.record_arrangement(
            self.conn, org, frequency=filing_periods.QUARTERLY,
            year_end_month=12, year_end_day=31,
            calculation_method=(case.calculation_method or
                                filing_periods.REGULAR_METHOD))
        if not case.calculation_method:
            self.conn.execute(
                "UPDATE bookkeeping_gst_filing SET calculation_method=NULL "
                "WHERE organization_id=?", (org,))

        for index, document in enumerate(case.documents):
            transaction = f"txn_{self.serial}_{index}"
            self.conn.execute(
                "INSERT INTO bookkeeping_transactions("
                "id,run_id,organization_id,provider,provider_type,provider_id,"
                "transaction_date,document_number,currency,total_amount,status,"
                "source_hash,tax_total) VALUES(?,?,?,'quickbooks',?,?,?,?,"
                "'CAD','0','posted',?,?)",
                (transaction, run, org, document["kind"], document["id"],
                 document["date"], document["id"], transaction,
                 document["tax_total"]))
            for rate_index, rate in enumerate(document["rates"]):
                self.conn.execute(
                    "INSERT INTO bookkeeping_transaction_taxes("
                    "id,transaction_id,tax_rate_ref,rate_percent,"
                    "net_amount_taxable,tax_amount) VALUES(?,?,?,NULL,?,?)",
                    (f"tax_{self.serial}_{index}_{rate_index}", transaction,
                     rate["ref"], rate["base"], rate["tax"]))
            posting = money(document["tax_posting"])
            if posting:
                debit = -posting if posting < 0 else Decimal("0.00")
                credit = posting if posting > 0 else Decimal("0.00")
                self.conn.execute(
                    "INSERT INTO bookkeeping_transaction_lines("
                    "id,transaction_id,provider_line_id,account_id,amount,"
                    "debit,credit) VALUES(?,?,?,?,?,?,?)",
                    (f"line_{self.serial}_{index}", transaction, "tax", account,
                     str(abs(posting)), str(debit), str(credit)))
        self.conn.commit()
        stated = sales_tax.period(
            self.conn, org, period_start=PERIOD_START, period_end=PERIOD_END)
        prepared = gst_return.prepare(
            self.conn, org, period_start=PERIOD_START, period_end=PERIOD_END)
        return stated, prepared

    def assert_generated(self, property_name, predicate):
        for seed in range(CASES):
            case = qbo_grammar.gst_case(seed)
            try:
                stated, prepared = self.evaluate(case)
                failure = predicate(case, stated, prepared)
            except Exception as exc:  # noqa: BLE001 - never raises is a property
                failure = f"raised {type(exc).__name__}: {exc}"
            if not failure:
                continue

            def still_fails(candidate):
                try:
                    candidate_stated, candidate_prepared = self.evaluate(candidate)
                    return bool(predicate(candidate, candidate_stated,
                                          candidate_prepared))
                except Exception:  # noqa: BLE001 - retain a crashing shape
                    return True

            minimal = qbo_grammar.shrink_gst(case, still_fails)
            self.fail(f"{property_name}; seed {seed}: {failure}\n\n"
                      f"Minimal counterexample:\n{shown(minimal)}")

    def test_prepare_and_period_never_raise_for_generated_tax_shapes(self):
        self.assert_generated("GST/HST preparation raised", lambda *_: "")

    def test_the_generator_reaches_every_rate_method_and_document_lane(self):
        cases = [qbo_grammar.gst_case(seed) for seed in range(CASES)]
        rate_refs = {rate["ref"] for case in cases for document in case.documents
                     for rate in document["rates"]}
        methods = {case.calculation_method for case in cases}
        kinds = {document["kind"] for case in cases for document in case.documents}
        self.assertTrue(qbo_grammar.GST_FEDERAL_RATE_IDS <= rate_refs)
        self.assertTrue(qbo_grammar.GST_PROVINCIAL_RATE_IDS <= rate_refs)
        self.assertTrue(qbo_grammar.GST_UNCLASSIFIED_RATE_IDS <= rate_refs)
        self.assertEqual(methods, {"regular", "quick", ""})
        self.assertEqual(kinds, qbo_grammar.GST_COLLECTED_TYPES |
                         qbo_grammar.GST_PAID_TYPES |
                         qbo_grammar.GST_COLLECTED_REVERSAL_TYPES |
                         qbo_grammar.GST_PAID_REVERSAL_TYPES)

    def test_line_109_is_exactly_line_105_less_line_108(self):
        def check(_case, _stated, prepared):
            lines = prepared.lines
            expected = lines[gst_return.LINE_105] - lines[gst_return.LINE_108]
            return "line 109 differs in Decimal" if lines[gst_return.LINE_109] != expected else ""
        self.assert_generated("line 109 arithmetic", check)

    def test_a_used_unclassified_or_absent_rate_never_produces_a_return(self):
        def check(case, _stated, prepared):
            known = {rate["Id"] for rate in case.rates}
            used = {rate["ref"] for document in case.documents
                    if PERIOD_START <= document["date"] <= PERIOD_END
                    for rate in document["rates"]}
            unresolved = {ref for ref in used
                          if ref not in known or ref == "MYST"}
            if unresolved and prepared.filable:
                return f"filable with unresolved rate(s) {sorted(unresolved)}"
            return ""
        self.assert_generated("unclassified rates must block", check)

    def test_pst_and_qst_never_contribute_to_lines_105_or_108(self):
        def check(case, _stated, prepared):
            collected = Decimal("0.00")
            paid = Decimal("0.00")
            known = {rate["Id"] for rate in case.rates}
            for document in case.documents:
                if not PERIOD_START <= document["date"] <= PERIOD_END:
                    continue
                rates = document["rates"]
                if not rates or sum((money(rate["tax"]) for rate in rates),
                                    Decimal("0.00")) != money(document["tax_total"]):
                    continue
                for rate in rates:
                    if rate["ref"] not in known:
                        continue
                    if rate["ref"] not in qbo_grammar.GST_FEDERAL_RATE_IDS:
                        continue
                    amount = money(rate["tax"])
                    if document["kind"] in qbo_grammar.GST_COLLECTED_TYPES:
                        collected += amount
                    elif document["kind"] in qbo_grammar.GST_COLLECTED_REVERSAL_TYPES:
                        collected -= amount
                    elif document["kind"] in qbo_grammar.GST_PAID_TYPES:
                        paid += amount
                    elif document["kind"] in qbo_grammar.GST_PAID_REVERSAL_TYPES:
                        paid -= amount
            actual = (prepared.lines[gst_return.LINE_105],
                      prepared.lines[gst_return.LINE_108])
            expected = (collected, paid)
            if actual != expected:
                return f"federal lines {actual} differ from independent total {expected}"
            included = {line.rate_ref for line in
                        prepared.collected_rates + prepared.paid_rates}
            leaked = included & qbo_grammar.GST_PROVINCIAL_RATE_IDS
            return f"provincial rate(s) reached federal lines: {sorted(leaked)}" if leaked else ""
        self.assert_generated("provincial tax exclusion", check)

    def test_quick_method_and_unrecorded_method_always_block(self):
        def check(case, _stated, prepared):
            if case.calculation_method != filing_periods.REGULAR_METHOD and prepared.filable:
                return f"{case.calculation_method or 'unrecorded'} method was filable"
            return ""
        self.assert_generated("unsupported calculation methods", check)


class GeneratedFilingPeriods(unittest.TestCase):

    @staticmethod
    def check(arrangement_data):
        arrangement = filing_periods.Arrangement(**arrangement_data)
        periods = filing_periods.periods_between(
            arrangement, start=date(2024, 1, 1), end=date(2029, 12, 31))
        if not periods:
            raise AssertionError("no periods were generated")
        for previous, current in zip(periods, periods[1:]):
            if current.start != previous.end + timedelta(days=1):
                raise AssertionError(f"gap or overlap: {previous} then {current}")
            if (previous.return_due is not None and current.return_due is not None
                    and current.return_due <= previous.return_due):
                raise AssertionError("return due dates are not increasing")
        for period in periods:
            if not period.covers(period.end):
                raise AssertionError(f"period excludes its end date: {period}")
            special = (period.frequency == filing_periods.ANNUAL
                       and arrangement.is_individual is True
                       and period.end.month == 12 and period.end.day == 31)
            if special:
                if not period.payment_due < period.return_due:
                    raise AssertionError("December 31 individual dates are not split")
            elif period.payment_due is not None and period.return_due is not None:
                if period.payment_due < period.return_due:
                    raise AssertionError("a non-exception payment is due before its return")

    def test_generated_fiscal_calendars_are_contiguous_inclusive_and_monotonic(self):
        requested_days = set()
        for seed in range(600):
            arrangement = qbo_grammar.filing_arrangement(seed)
            requested_days.add(arrangement["year_end_day"])
            try:
                self.check(arrangement)
            except Exception as exc:  # noqa: BLE001 - shrink any calendar failure
                def still_fails(candidate):
                    try:
                        self.check(candidate)
                    except Exception:  # noqa: BLE001
                        return True
                    return False
                minimal = qbo_grammar.shrink_filing(arrangement, still_fails)
                self.fail(f"seed {seed}: {type(exc).__name__}: {exc}\n"
                          f"Minimal arrangement: {minimal}")
        self.assertTrue({29, 30, 31} <= requested_days,
                        "the generator never exercised every risky month-end")


if __name__ == "__main__":
    unittest.main()
