"""The matcher must be right, and where it cannot be right it must refuse.

Half of these tests are about what it declines to do. A reconciliation engine
that pairs a statement line with the wrong ledger entry produces books that look
balanced and are not, which is strictly worse than leaving the line for a human.
"""
from datetime import date
from decimal import Decimal

import pytest

from shimline import matching
from shimline.matching import BankLine, LedgerEntry, UndatedMovement, match
from shimline.postings import derive_ledger


def entry(key, day, amount, party="", doc=""):
    kind, _, ident = key.partition(":")
    return LedgerEntry(key=key, object_type=kind, object_id=ident,
                       posted_date=date.fromisoformat(day),
                       amount=Decimal(amount), party=party, doc_number=doc)


def line(ordinal, day, amount, description="", memo=""):
    return BankLine(ordinal=ordinal, posted_date=date.fromisoformat(day),
                    amount=Decimal(amount), description=description, memo=memo)


# ------------------------------------------------------------ the easy case --

def test_exact_same_day_match():
    report = match([line(0, "2026-08-04", "-412.55", "RONA INC #556")],
                   [entry("Purchase:1", "2026-08-04", "-412.55", "RONA")])
    assert report.matched_count == 1
    assert report.matches[0].ledger_key == "Purchase:1"
    assert report.matches[0].date_distance_days == 0
    assert not report.unmatched_bank and not report.unmatched_ledger


def test_cheque_clearing_late_still_matches():
    report = match([line(0, "2026-08-09", "-900.00", "NORTHERN SUPPLY LTD")],
                   [entry("Purchase:1", "2026-08-04", "-900.00", "NORTHERN SUPPLY")])
    assert report.matched_count == 1
    assert report.matches[0].date_distance_days == 5


def test_beyond_the_window_is_not_a_match():
    report = match([line(0, "2026-08-10", "-900.00", "NORTHERN SUPPLY LTD")],
                   [entry("Purchase:1", "2026-08-04", "-900.00", "NORTHERN SUPPLY")])
    assert report.matched_count == 0
    assert len(report.unmatched_bank) == 1
    assert len(report.unmatched_ledger) == 1


def test_a_cent_of_difference_is_a_different_transaction():
    report = match([line(0, "2026-08-04", "-412.56", "RONA INC #556")],
                   [entry("Purchase:1", "2026-08-04", "-412.55", "RONA")])
    assert report.matched_count == 0


def test_opposite_sign_does_not_match():
    """Money in and money out of the same size are not the same event."""
    report = match([line(0, "2026-08-04", "412.55", "RONA INC #556")],
                   [entry("Purchase:1", "2026-08-04", "-412.55", "RONA")])
    assert report.matched_count == 0


# ------------------------------------------------------------ the refusals --

def test_identical_twins_are_refused_not_guessed():
    """Two indistinguishable candidates go to a person. Splink guessed here."""
    report = match(
        [line(0, "2026-08-13", "-412.55", "RONA INC #556")],
        [entry("Purchase:1", "2026-08-13", "-412.55", "RONA"),
         entry("Purchase:2", "2026-08-13", "-412.55", "RONA")])
    assert report.matched_count == 0
    assert len(report.ambiguous) == 1
    assert set(report.ambiguous[0].candidates) == {"Purchase:1", "Purchase:2"}


def test_two_lines_competing_for_one_entry_are_both_refused():
    report = match(
        [line(0, "2026-08-13", "-412.55", "RONA INC #556"),
         line(1, "2026-08-13", "-412.55", "RONA INC #556")],
        [entry("Purchase:1", "2026-08-13", "-412.55", "RONA")])
    assert report.matched_count == 0
    assert {item.bank_ordinal for item in report.ambiguous} == {0, 1}


def test_better_name_evidence_breaks_a_tie_rather_than_refusing():
    """A refusal is for a genuine toss-up, not for any competition at all."""
    report = match(
        [line(0, "2026-08-13", "-412.55", "RONA INC #556")],
        [entry("Purchase:1", "2026-08-13", "-412.55", "RONA"),
         entry("Purchase:2", "2026-08-13", "-412.55", "PETRO-CANADA")])
    assert report.matched_count == 1
    assert report.matches[0].ledger_key == "Purchase:1"
    assert not report.ambiguous


def test_closer_date_breaks_a_tie_when_names_are_equally_silent():
    report = match(
        [line(0, "2026-08-13", "-412.55", "UNREADABLE DESCRIPTOR")],
        [entry("Purchase:1", "2026-08-12", "-412.55", "RONA"),
         entry("Purchase:2", "2026-08-09", "-412.55", "RONA")])
    assert report.matched_count == 1
    assert report.matches[0].ledger_key == "Purchase:1"


def test_an_orphan_bank_line_is_reported_not_forced():
    report = match([line(0, "2026-08-20", "-1875.00", "UNKNOWN VENDOR 9931")],
                   [entry("Purchase:1", "2026-08-20", "-900.00", "RONA")])
    assert report.matched_count == 0
    assert [item.ordinal for item in report.unmatched_bank] == [0]
    assert [item.key for item in report.unmatched_ledger] == ["Purchase:1"]


def test_result_does_not_depend_on_the_order_lines_arrive_in():
    """Assignment is global; a statement sorted differently must not answer
    differently, or the same books would reconcile twice with two answers."""
    entries = [entry("Purchase:1", "2026-08-04", "-412.55", "RONA"),
               entry("Purchase:2", "2026-08-06", "-412.55", "RONA")]
    lines = [line(0, "2026-08-04", "-412.55", "RONA INC #556"),
             line(1, "2026-08-06", "-412.55", "RONA INC #556")]
    forward = match(lines, entries)
    backward = match(list(reversed(lines)), list(reversed(entries)))
    assert {m.bank_ordinal: m.ledger_key for m in forward.matches} == \
           {m.bank_ordinal: m.ledger_key for m in backward.matches}
    assert forward.matched_count == 2


# ----------------------------------------------------------- the explanation --

def test_every_match_carries_its_components():
    report = match([line(0, "2026-08-06", "-412.55", "RONA INC #556")],
                   [entry("Purchase:1", "2026-08-04", "-412.55", "RONA")])
    found = report.matches[0]
    assert "amount exact -412.55" in found.reason
    assert "2 days apart" in found.reason
    assert "RONA" in found.reason
    assert found.name_tokens == ("RONA",)


def test_a_match_with_no_name_evidence_says_so():
    report = match([line(0, "2026-08-04", "-412.55", "CHQ 00412")],
                   [entry("Purchase:1", "2026-08-04", "-412.55", "RONA")])
    assert "no vendor name evidence" in report.matches[0].reason


def test_one_day_apart_reads_as_singular():
    report = match([line(0, "2026-08-05", "-412.55", "RONA")],
                   [entry("Purchase:1", "2026-08-04", "-412.55", "RONA")])
    assert "1 day apart" in report.matches[0].reason


# -------------------------------------------------------------- name tokens --

def test_bank_descriptors_that_close_up_spaces_still_match():
    """HOME DEPOT arrives as HOMEDEPOT. Whole-word equality finds nothing."""
    assert matching.name_evidence("HOME DEPOT", "HOMEDEPOT #7021 OTTAWA ON") == \
           ("DEPOT", "HOME")


def test_short_words_are_not_evidence():
    """INC and LTD would tie every vendor to every other."""
    assert matching.name_evidence("RONA INC", "NORTHERN SUPPLY INC") == ()


def test_punctuation_does_not_hide_a_name():
    assert "PETRO" in matching.name_evidence("PETRO-CANADA", "PETRO CAN 4412")


# ------------------------------------------ the ledger side of the pairing --

CHART = [{"Id": "100", "AccountType": "Bank"},
         {"Id": "500", "AccountType": "Expense"},
         {"Id": "120", "AccountType": "Accounts Receivable"}]


def purchase(ident, day, amount, vendor_id="9"):
    return {"Id": ident, "TxnDate": day, "TotalAmt": amount,
            "AccountRef": {"value": "100"}, "EntityRef": {"value": vendor_id},
            "DocNumber": f"D{ident}",
            "Line": [{"Amount": amount, "DetailType": "AccountBasedExpenseLineDetail",
                      "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "500"}}}]}


def test_cash_movements_reads_the_reconstruction_not_the_documents():
    objects = {"Account": CHART,
               "Vendor": [{"Id": "9", "DisplayName": "RONA"}],
               "Purchase": [purchase("1", "2026-08-04", 412.55)]}
    derived = derive_ledger(objects)
    assert derived.complete
    movements = matching.cash_movements(derived, objects, "100")
    assert len(movements) == 1
    assert movements[0].amount == Decimal("-412.55")   # money left the bank
    assert movements[0].party == "RONA"
    assert movements[0].doc_number == "D1"


def test_cash_movements_ignores_documents_that_miss_the_account():
    objects = {"Account": CHART,
               "Purchase": [purchase("1", "2026-08-04", 412.55)],
               "Invoice": [{"Id": "7", "TxnDate": "2026-08-04", "TotalAmt": 900,
                            "ARAccountRef": {"value": "120"},
                            "Line": [{"Amount": 900, "DetailType": "SalesItemLineDetail",
                                      "SalesItemLineDetail": {
                                          "ItemAccountRef": {"value": "500"}}}]}]}
    derived = derive_ledger(objects)
    movements = matching.cash_movements(derived, objects, "100")
    assert [item.key for item in movements] == ["Purchase:1"]


def test_an_undated_movement_stops_matching_rather_than_looking_missing():
    """Silently dropping it would make its statement line read as a missing
    transaction, which is a manufactured finding against the client."""
    objects = {"Account": CHART, "Purchase": [purchase("1", "", 412.55)]}
    derived = derive_ledger(objects)
    with pytest.raises(UndatedMovement):
        matching.cash_movements(derived, objects, "100")


def test_bank_lines_accepts_stored_rows():
    rows = [{"posted_date": "2026-08-04", "amount": Decimal("-412.55"),
             "description": "RONA", "memo": "", "fitid": "X1", "ordinal": 0}]
    built = matching.bank_lines(rows)
    assert built[0].posted_date == date(2026, 8, 4)
    assert built[0].amount == Decimal("-412.55")
    assert built[0].fitid == "X1"


def test_the_whole_pairing_works_end_to_end_from_qbo_objects():
    objects = {"Account": CHART,
               "Vendor": [{"Id": "9", "DisplayName": "HOME DEPOT"}],
               "Purchase": [purchase("1", "2026-08-04", 412.55),
                            purchase("2", "2026-08-05", 88.10)]}
    derived = derive_ledger(objects)
    movements = matching.cash_movements(derived, objects, "100")
    lines = matching.bank_lines([
        {"posted_date": "2026-08-06", "amount": "-412.55",
         "description": "HOMEDEPOT #7021 OTTAWA ON", "ordinal": 0},
        {"posted_date": "2026-08-05", "amount": "-1200.00",
         "description": "UNKNOWN 9931", "ordinal": 1},
    ])
    report = match(lines, movements)
    assert report.matched_count == 1
    assert report.matches[0].ledger_key == "Purchase:1"
    assert [item.ordinal for item in report.unmatched_bank] == [1]
    assert [item.key for item in report.unmatched_ledger] == ["Purchase:2"]
    assert report.summary() == {"lines": 2, "entries": 2, "matched": 1,
                                "ambiguous": 0, "unmatched_bank": 1,
                                "unmatched_ledger": 1}
