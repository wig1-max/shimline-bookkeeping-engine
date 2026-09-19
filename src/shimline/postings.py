"""Reconstruct double-entry postings from QuickBooks objects.

Why this exists
---------------
`trial_balance` sums the `_Postings` that `synthetic_books` attaches to each
transaction. The live adapter attaches none -- it returns the provider's objects
as they arrive -- so for a real pull it returned `{}`, every account read as
zero, and check 04 had to be blocked because a statement had nothing to
reconcile against. That block is the single thing standing between this system
and an automated month-end close.

The honest difficulty
---------------------
QuickBooks does not publish postings. It publishes documents, and the postings
are implied by the document type. Rebuilding them is real accounting work, and a
*partially* correct reconstruction is far worse than none: one unhandled
transaction type silently shifts an account balance, and a silently shifted
balance is a wrong set of books that looks right.

So this module never guesses. Every transaction either produces postings that
balance to the cent, or it is recorded in `unsupported` with the reason, and the
whole derivation is marked incomplete. A caller may only use the balances when
`complete` is true.

The check that makes it safe
----------------------------
Even a careful reconstruction can be complete-looking and wrong. Every posting
type QuickBooks publishes is derived here now, but a type Intuit adds tomorrow,
or a field it starts populating differently, would move a balance we never saw.
So the reconstruction is not trusted on its own. `compare_to_provider` puts it
against QuickBooks' own TrialBalance report and requires exact agreement,
account by account.

That is the same idea as the Beancount oracle one layer down: do not assert that
the arithmetic is right, make a second independent implementation agree with it.
Here the second implementation is QuickBooks itself, which is the authority the
client's accountant will be looking at anyway.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# Transaction types the adapter reads that post to the ledger. Estimate is read
# but deliberately excluded: a quote is not a posting, and neither is a purchase
# order -- both are promises, and QuickBooks keeps them off the ledger too.
POSTING_TYPES = (
    "Invoice", "Payment", "Bill", "Purchase", "Deposit", "JournalEntry",
    "BillPayment", "CreditMemo", "VendorCredit", "Transfer",
    "SalesReceipt", "RefundReceipt",
)

# Types that post but which the adapter does not read. Listed so the gap is
# stated in code rather than discovered from a balance that will not tie.
#
# This list is now empty, and that is the point of the second block of
# derivations below: a client with a bill payment or a transfer -- which is
# every client with a chequing account -- used to get the whole ledger blocked,
# so reconciliation worked only on unusually tidy books. Keep the list here. The
# next type Intuit adds, or the next one someone decides to read, belongs in it
# rather than in a silently wrong balance.
UNREAD_POSTING_TYPES: tuple[str, ...] = ()

# Everything else a pull may legitimately contain, none of which posts: the
# chart and the lists, the attachments, and the two promises. This list is not
# documentation -- it is enforced. Any other key in a pull blocks the whole
# derivation, so a type Intuit adds, or one somebody adds to READ_OBJECTS and
# forgets to give a rule, cannot be quietly dropped out of a balance. The cost
# of being wrong here is a client's books that tie and are missing a
# transaction, and nothing downstream would ever reveal it.
NON_POSTING_TYPES = (
    "Account", "Customer", "Vendor", "Employee", "Class", "TaxCode", "TaxRate",
    "TaxAgency", "TaxService", "Item", "Term", "PaymentMethod", "Attachable",
    "CompanyInfo",
    "Preferences", "Department", "Budget", "CustomerType", "Estimate",
    "PurchaseOrder", "TimeActivity", "RecurringTransaction",
)

_AR_TYPES = {"Accounts Receivable"}
_AP_TYPES = {"Accounts Payable"}
_TAX_LIABILITY_TYPES = {"Other Current Liability"}


def money(value=0) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))


@dataclass
class Unsupported:
    object_type: str
    object_id: str
    reason: str


@dataclass
class DerivedLedger:
    balances: dict[str, Decimal] = field(default_factory=dict)
    postings: dict[str, list[dict]] = field(default_factory=dict)
    unsupported: list[Unsupported] = field(default_factory=list)
    transactions_seen: int = 0
    # Documents that legitimately move no account: a zero total, no lines and no
    # tax. They were silently `continue`d, which meant a document that posted
    # nothing and a document that was lost looked identical from outside -- the
    # defect this codebase keeps finding. Recorded so the arithmetic below is an
    # identity anybody can check.
    posted_nothing: list[Unsupported] = field(default_factory=list)
    documents_decided: int = 0

    @property
    def complete(self) -> bool:
        """True only when every posting transaction was fully reconstructed.

        A document that moved no account does not make the ledger incomplete. It
        does have to have been *seen* to move no account, which is what
        `every_document_accounted_for` asserts.
        """
        return not self.unsupported

    @property
    def every_document_accounted_for(self) -> bool:
        """Every document reached one of the three outcomes.

        Posted, refused, or recorded as moving nothing. If this is ever false a
        document went through the reconstruction and left no trace, which is the
        one failure mode that produces books that look right.
        """
        return self.documents_decided == self.transactions_seen

    def reasons(self) -> list[str]:
        return [f"{item.object_type} {item.object_id}: {item.reason}"
                for item in self.unsupported]


def _accounts_by_type(objects: dict) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for account in objects.get("Account", []):
        grouped.setdefault((account.get("AccountType") or "").strip(), []).append(
            str(account.get("Id")))
    return grouped


def _single_control_account(grouped: dict[str, list[str]], types: set[str]) -> str | None:
    """The one control account of a family, or None if it is not unambiguous.

    Two A/R accounts is legitimate in QuickBooks and means the document has to
    say which one it used. When it does not, guessing would put the balance on
    the wrong account, so this returns None and the caller records it.
    """
    found = [account for name, ids in grouped.items() if name in types for account in ids]
    return found[0] if len(found) == 1 else None


def _ref(value: dict | None) -> str:
    return str((value or {}).get("value") or "").strip()


def _line_account(line: dict) -> str:
    """The account a line posts to, whichever detail shape QBO used."""
    for key in ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail",
                "SalesItemLineDetail", "DepositLineDetail", "JournalEntryLineDetail"):
        detail = line.get(key)
        if not detail:
            continue
        for ref in ("AccountRef", "ItemAccountRef"):
            account = _ref(detail.get(ref))
            if account:
                return account
    return ""


def _posting_lines(transaction: dict) -> list[dict]:
    """Lines that carry an amount, skipping QBO's subtotal and description rows.

    A subtotal or a description-only row carries no money and is skipped. A row
    that should carry money and does not is a different thing entirely, and it
    is raised rather than dropped: silently ignoring it would leave a journal
    entry that still balances while missing a posting, which is the one failure
    the document-level balance check cannot catch.
    """
    lines = []
    for line in transaction.get("Line", []) or []:
        detail_type = (line.get("DetailType") or "").strip()
        if detail_type in {"SubTotalLineDetail", "DescriptionOnly"}:
            continue
        if line.get("Amount") is None:
            raise _Undecidable(
                f"has a {detail_type or 'detail'} line carrying no amount, so "
                "what it posts is unknown")
        lines.append(line)
    return lines


def _tax_total(transaction: dict) -> Decimal:
    return money((transaction.get("TxnTaxDetail") or {}).get("TotalTax"))


def _tax_account(transaction: dict, grouped: dict[str, list[str]],
                 known_accounts: set[str]) -> str | None:
    """Where sales tax lands, or None when the document does not say.

    QuickBooks does not put the tax *liability account* on a transaction. What
    `TaxLine` carries is a `TaxRateRef` -- the id of a rate, not of an account --
    so anything found there is only usable if it happens to name a real account
    in this client's chart. An earlier version returned it unconditionally,
    which made every taxed document fail the chart check and blocked the whole
    ledger even when the client had exactly one tax account. Validating against
    the chart is what turns that from a wall into a fallback.

    The fallback is a single tax-bearing liability account. Two of them and this
    returns None: guessing which one GST went to is the kind of plausible answer
    that produces books that look right.
    """
    detail = transaction.get("TxnTaxDetail") or {}
    for line in detail.get("TaxLine", []) or []:
        for ref in ("AccountRef", "TaxRateRef"):
            candidate = _ref((line.get("TaxLineDetail") or {}).get(ref))
            if candidate and candidate in known_accounts:
                return candidate
    candidates = [account for name, ids in grouped.items()
                  if name in _TAX_LIABILITY_TYPES for account in ids]
    return candidates[0] if len(candidates) == 1 else None


def _balanced(postings: list[dict]) -> bool:
    total = sum((money(p.get("debit")) - money(p.get("credit")) for p in postings),
                Decimal("0"))
    return total == Decimal("0")


def _post(account: str, *, debit=0, credit=0) -> dict:
    return {"account": account, "debit": str(money(debit)), "credit": str(money(credit))}


def _flip(postings: list[dict]) -> list[dict]:
    """The same postings the other way round, for a credit note or a refund.

    A credit memo is an invoice run backwards and a vendor credit is a bill run
    backwards. Deriving them by reversing the forward rule means the two can
    never drift apart, which is the failure that would show up as a customer
    balance that is right in one direction only.
    """
    return [_post(item["account"], debit=item["credit"], credit=item["debit"])
            for item in postings]


def derive_ledger(objects: dict[str, list[dict]]) -> DerivedLedger:
    """Rebuild postings for every posting transaction, or say why not."""
    ledger = DerivedLedger()
    grouped = _accounts_by_type(objects)
    known_accounts = {str(a.get("Id")) for a in objects.get("Account", [])}
    ar_default = _single_control_account(grouped, _AR_TYPES)
    ap_default = _single_control_account(grouped, _AP_TYPES)

    # Anything that posts but that we never read makes the reconstruction
    # incomplete by construction. Say so once, loudly, rather than producing a
    # balance that cannot tie and leaving someone to work out why.
    # A posting type the pull never read is not the same as one with no rows,
    # and as plain dicts they are identical -- an absent key either way. A pull
    # that lost every invoice would otherwise produce a ledger reporting itself
    # complete, missing revenue and receivables, with the trial balance still
    # summing to zero because both halves of every invoice went missing
    # together. Neither the double-entry check nor Beancount can see that.
    # Only knowing what was supposed to be there can.
    ledger.unsupported.extend(_manifest_gaps(objects))

    for kind in UNREAD_POSTING_TYPES:
        rows = objects.get(kind) or []
        if rows:
            ledger.unsupported.append(Unsupported(
                kind, f"{len(rows)} object(s)",
                "this type posts to the ledger but the adapter does not read it"))

    # Anything in the pull that is neither derived above nor a known non-posting
    # list. Silence here is the dangerous answer: an unrecognised type would
    # simply not appear in any balance, and the trial balance would still tie.
    for kind, rows in objects.items():
        if not rows or kind.startswith("_"):
            continue
        if kind in POSTING_TYPES or kind in UNREAD_POSTING_TYPES:
            continue
        if kind in NON_POSTING_TYPES:
            continue
        ledger.unsupported.append(Unsupported(
            kind, f"{len(rows) if isinstance(rows, list) else 1} object(s)",
            "this type is not recognised, so it is not known whether it posts "
            "to the ledger; it must be given a derivation rule or named as "
            "non-posting before these books can be trusted"))

    for kind in POSTING_TYPES:
        for transaction in objects.get(kind, []) or []:
            ledger.transactions_seen += 1
            object_id = str(transaction.get("Id") or "?")

            # Already-derived postings win. The synthetic oracle attaches them,
            # and re-deriving would test this module against itself.
            existing = transaction.get("_Postings")
            if existing:
                ledger.postings[f"{kind}:{object_id}"] = existing
                ledger.documents_decided += 1
                continue

            try:
                postings = _derive_one(kind, transaction, grouped,
                                       ar_default, ap_default, known_accounts)
            except _PostsNothing:
                ledger.posted_nothing.append(Unsupported(
                    kind, object_id,
                    "states a zero total and carries no lines and no tax, so it "
                    "moves no account"))
                ledger.documents_decided += 1
                continue
            except _Undecidable as exc:
                ledger.unsupported.append(Unsupported(kind, object_id, str(exc)))
                ledger.documents_decided += 1
                continue

            unknown = [p["account"] for p in postings
                       if p["account"] not in known_accounts]
            if unknown:
                ledger.unsupported.append(Unsupported(
                    kind, object_id,
                    f"posts to account(s) absent from the chart: {', '.join(sorted(set(unknown)))}"))
                ledger.documents_decided += 1
                continue
            if not _balanced(postings):
                ledger.unsupported.append(Unsupported(
                    kind, object_id, "reconstructed postings do not balance"))
                ledger.documents_decided += 1
                continue
            ledger.postings[f"{kind}:{object_id}"] = postings
            ledger.documents_decided += 1

    for postings in ledger.postings.values():
        for posting in postings:
            account = str(posting["account"])
            ledger.balances[account] = ledger.balances.get(account, Decimal("0")) + (
                money(posting.get("debit")) - money(posting.get("credit")))
    ledger.balances = {key: value.quantize(Decimal("0.01"))
                       for key, value in sorted(ledger.balances.items())}
    return ledger


MANIFEST_KEY = "_pull_manifest"


def _manifest_gaps(objects: dict[str, list[dict]]) -> list[Unsupported]:
    """Posting types the pull did not read, according to the pull itself.

    Silent when there is no manifest. That is deliberate and is not a hole: a
    fixture, a golden company or a hand-built dict in a test legitimately has
    nothing to declare, and demanding one here would make every one of them
    unusable. The requirement lives at the gate instead --
    `work_engine.trusted_ledger` will not hand a ledger to any check unless the
    objects came from a pull that said what it read. One place, one rule, and a
    test that proves the gate refuses.
    """
    if MANIFEST_KEY not in objects:
        return []
    declared = objects.get(MANIFEST_KEY)
    read = declared.get("read") if isinstance(declared, dict) else None
    # Present but unreadable is a corruption signal, not an absence. Treating a
    # mangled manifest as "no manifest supplied" would turn a damaged pull into
    # a fixture and wave it through the very check it was meant to fail.
    if not isinstance(read, dict):
        return [Unsupported(
            MANIFEST_KEY, "-",
            "the pull recorded itself in a shape this cannot read, so which "
            "entity types it covered is unknown")]
    return [Unsupported(
        kind, "-",
        "this type posts to the ledger and the pull did not read it, so the "
        "books are missing whatever it held")
        for kind in POSTING_TYPES if kind not in read]


class _Undecidable(ValueError):
    """This document cannot be turned into postings without inventing a fact."""


def _foreign_currency(transaction: dict) -> str:
    """The transaction's currency when it is not the company's own, else "".

    `ExchangeRate` is the signal. QuickBooks populates it only on a transaction
    denominated in something other than the home currency, so its presence at
    anything but 1 is the company telling us this document is foreign.
    """
    rate = transaction.get("ExchangeRate")
    if rate in (None, "", 1, 1.0, "1"):
        return ""
    try:
        if Decimal(str(rate)) == Decimal("1"):
            return ""
    except (ArithmeticError, ValueError):
        pass
    return str((transaction.get("CurrencyRef") or {}).get("value") or "foreign")


def _derive_one(kind: str, transaction: dict, grouped: dict[str, list[str]],
                ar_default: str | None, ap_default: str | None,
                known_accounts: set[str]) -> list[dict]:
    lines = _posting_lines(transaction)
    total = money(transaction.get("TotalAmt"))

    # A document with no amount and no lines is a QuickBooks artefact: an entry
    # started and abandoned, or one whose lines were all removed. It moves no
    # account, so there is nothing to get wrong, and blocking a client's entire
    # ledger over it would be a refusal with no accounting meaning behind it.
    # The total is what distinguishes it -- a document stating an amount with no
    # lines to explain it is genuinely underivable and still blocks below.
    if kind != "Transfer" and not lines and not _tax_total(transaction):
        # A document that *states* zero is abandoned and moves no account. A
        # document that states nothing is not the same thing, and `money(None)`
        # makes them identical: both arrive here with a total of 0. Found by
        # generating damaged documents -- a BillPayment carrying nothing but an
        # Id vanished into this branch and the ledger reported itself complete.
        #
        # The same distinction as the pull manifest, one level down: an absent
        # field and a zero value have to be told apart, or a document that
        # arrived truncated is indistinguishable from one somebody abandoned.
        if "TotalAmt" not in transaction:
            raise _Undecidable(
                "states no total and carries no lines, so there is no way to "
                "tell a document somebody abandoned from one that arrived "
                "incomplete")
        if total == 0:
            raise _PostsNothing

    # A foreign-currency document posts to the ledger in the *home* currency, at
    # the rate on the document. Its `TotalAmt` and every line amount are in the
    # transaction currency. Posting those numbers unconverted understates the
    # entry by the whole exchange rate -- a USD 100 bill at 1.37 lands as 100.00
    # CAD instead of 137.00 -- and it balances perfectly while doing it, because
    # both sides carry the same wrong number. Neither the double-entry check nor
    # Beancount can see that: Beancount recomputes the same postings and agrees.
    #
    # Converting is not the fix either, not yet. Rounding the rate per line and
    # rounding it on the total give different answers to the cent, and which one
    # QuickBooks uses has never been checked against a real multi-currency file.
    # So this refuses, which blocks a multi-currency client rather than
    # mis-stating one. Revisit with a real file in front of you.
    foreign = _foreign_currency(transaction)
    if foreign:
        raise _Undecidable(
            f"is denominated in {foreign} at a rate of "
            f"{transaction.get('ExchangeRate')}, and converting it to the "
            "company's own currency has not been verified against a real "
            "multi-currency file. Posting the stated amounts would understate "
            "this entry by the exchange rate, and it would still balance")

    if kind == "JournalEntry":
        # The only type where QuickBooks states the postings outright.
        postings = []
        for line in lines:
            detail = line.get("JournalEntryLineDetail") or {}
            account = _ref(detail.get("AccountRef"))
            posting_type = (detail.get("PostingType") or "").strip()
            if not account or posting_type not in {"Debit", "Credit"}:
                raise _Undecidable("a journal line has no account or no posting type")
            amount = money(line.get("Amount"))
            postings.append(_post(account, **({"debit": amount} if posting_type == "Debit"
                                              else {"credit": amount})))
        if not postings:
            raise _Undecidable("journal entry has no posting lines")
        return postings

    if kind == "Purchase":
        # Expense, cheque or credit-card charge. AccountRef is what paid it.
        funding = _ref(transaction.get("AccountRef"))
        if not funding:
            raise _Undecidable("no AccountRef, so the funding account is unknown")
        postings = [_post(funding, credit=total)]
        postings += _expense_debits(lines)
        tax = _tax_total(transaction)
        if tax:
            account = _tax_account(transaction, grouped, known_accounts)
            if not account:
                raise _Undecidable("carries tax but no identifiable tax account")
            postings.append(_post(account, debit=tax))
        return postings

    if kind == "Bill":
        payable = _ref(transaction.get("APAccountRef")) or ap_default
        if not payable:
            raise _Undecidable("no A/P account on the bill and no single A/P in the chart")
        postings = [_post(payable, credit=total)]
        postings += _expense_debits(lines)
        tax = _tax_total(transaction)
        if tax:
            account = _tax_account(transaction, grouped, known_accounts)
            if not account:
                raise _Undecidable("carries tax but no identifiable tax account")
            postings.append(_post(account, debit=tax))
        return postings

    if kind == "Invoice":
        receivable = _ref(transaction.get("ARAccountRef")) or ar_default
        if not receivable:
            raise _Undecidable("no A/R account on the invoice and no single A/R in the chart")
        postings = [_post(receivable, debit=total)]
        postings += _revenue_credits(lines)
        tax = _tax_total(transaction)
        if tax:
            account = _tax_account(transaction, grouped, known_accounts)
            if not account:
                raise _Undecidable("carries tax but no identifiable tax account")
            postings.append(_post(account, credit=tax))
        return postings

    if kind == "Payment":
        deposit_to = _ref(transaction.get("DepositToAccountRef"))
        receivable = _ref(transaction.get("ARAccountRef")) or ar_default
        if not deposit_to:
            raise _Undecidable("no DepositToAccountRef, so the receiving account is unknown")
        if not receivable:
            raise _Undecidable("no A/R account on the payment and no single A/R in the chart")
        return [_post(deposit_to, debit=total), _post(receivable, credit=total)]

    if kind == "Deposit":
        deposit_to = _ref(transaction.get("DepositToAccountRef"))
        if not deposit_to:
            raise _Undecidable("no DepositToAccountRef, so the receiving account is unknown")
        postings = [_post(deposit_to, debit=total)]
        for line in lines:
            account = _line_account(line)
            if not account:
                raise _Undecidable("a deposit line names no account")
            postings.append(_post(account, credit=money(line.get("Amount"))))
        return postings

    # ------------------------------------------------------------------
    # The types that used to block the whole ledger. Each is the settlement
    # or the reversal of one of the documents above, and each is refused the
    # moment the document stops saying which accounts it moved between.
    # ------------------------------------------------------------------

    if kind == "BillPayment":
        # Settles a bill: payable goes down, cash or the card goes down with it.
        payable = _ref(transaction.get("APAccountRef")) or ap_default
        if not payable:
            raise _Undecidable(
                "no A/P account on the bill payment and no single A/P in the chart")
        funding = _payment_funding_account(transaction)
        if not funding:
            raise _Undecidable(
                "the payment does not name the account it was paid from")
        return [_post(payable, debit=total), _post(funding, credit=total)]

    if kind == "Transfer":
        # Between two of the client's own accounts. Carries `Amount`, not
        # `TotalAmt`, and a zero total here would silently post nothing.
        amount = money(transaction.get("Amount"))
        source = _ref(transaction.get("FromAccountRef"))
        destination = _ref(transaction.get("ToAccountRef"))
        if not source or not destination:
            raise _Undecidable("a transfer must name both accounts")
        if source == destination:
            raise _Undecidable("a transfer names the same account on both sides")
        if amount == 0:
            raise _Undecidable("the transfer carries no amount")
        return [_post(destination, debit=amount), _post(source, credit=amount)]

    if kind == "SalesReceipt":
        # A sale settled at the till: revenue and cash in the same document.
        deposit_to = _ref(transaction.get("DepositToAccountRef"))
        if not deposit_to:
            raise _Undecidable(
                "no DepositToAccountRef; QuickBooks defaults this to Undeposited "
                "Funds without naming the account, and guessing which account "
                "that is would put the cash in the wrong place")
        postings = [_post(deposit_to, debit=total)]
        postings += _revenue_credits(lines)
        tax = _tax_total(transaction)
        if tax:
            account = _tax_account(transaction, grouped, known_accounts)
            if not account:
                raise _Undecidable("carries tax but no identifiable tax account")
            postings.append(_post(account, credit=tax))
        return postings

    if kind == "RefundReceipt":
        # The mirror of a sales receipt: cash out, revenue reversed.
        refund_from = _ref(transaction.get("DepositToAccountRef"))
        if not refund_from:
            raise _Undecidable("no account named for the refund to come out of")
        postings = [_post(refund_from, credit=total)]
        postings += _flip(_revenue_credits(lines))
        tax = _tax_total(transaction)
        if tax:
            account = _tax_account(transaction, grouped, known_accounts)
            if not account:
                raise _Undecidable("carries tax but no identifiable tax account")
            postings.append(_post(account, debit=tax))
        return postings

    if kind == "CreditMemo":
        # Reduces what a customer owes: the mirror of an invoice.
        receivable = _ref(transaction.get("ARAccountRef")) or ar_default
        if not receivable:
            raise _Undecidable(
                "no A/R account on the credit memo and no single A/R in the chart")
        postings = [_post(receivable, credit=total)]
        postings += _flip(_revenue_credits(lines))
        tax = _tax_total(transaction)
        if tax:
            account = _tax_account(transaction, grouped, known_accounts)
            if not account:
                raise _Undecidable("carries tax but no identifiable tax account")
            postings.append(_post(account, debit=tax))
        return postings

    if kind == "VendorCredit":
        # Reduces what the client owes a supplier: the mirror of a bill.
        payable = _ref(transaction.get("APAccountRef")) or ap_default
        if not payable:
            raise _Undecidable(
                "no A/P account on the vendor credit and no single A/P in the chart")
        postings = [_post(payable, debit=total)]
        postings += _flip(_expense_debits(lines))
        tax = _tax_total(transaction)
        if tax:
            account = _tax_account(transaction, grouped, known_accounts)
            if not account:
                raise _Undecidable("carries tax but no identifiable tax account")
            postings.append(_post(account, credit=tax))
        return postings

    raise _Undecidable(f"no derivation rule for {kind}")


def _payment_funding_account(transaction: dict) -> str:
    """The account a bill payment came out of, whichever way it was paid.

    QuickBooks puts the bank account under `CheckPayment` and the card under
    `CreditCardPayment`, and `PayType` says which to read. Reading whichever is
    present, without trusting PayType, is deliberate: a document that carries
    both is the ambiguous case, and it is refused by the caller when this
    returns nothing rather than by picking a side here.
    """
    cheque = _ref((transaction.get("CheckPayment") or {}).get("BankAccountRef"))
    card = _ref((transaction.get("CreditCardPayment") or {}).get("CCAccountRef"))
    if cheque and card and cheque != card:
        return ""
    return cheque or card


def _revenue_credits(lines: list[dict]) -> list[dict]:
    postings = []
    for line in lines:
        account = _line_account(line)
        if not account:
            raise _Undecidable("an income line names no account")
        postings.append(_post(account, credit=money(line.get("Amount"))))
    if not postings:
        raise _Undecidable("no income lines to post")
    return postings


def _expense_debits(lines: list[dict]) -> list[dict]:
    postings = []
    for line in lines:
        account = _line_account(line)
        if not account:
            raise _Undecidable("an expense line names no account")
        postings.append(_post(account, debit=money(line.get("Amount"))))
    if not postings:
        raise _Undecidable("no expense lines to post")
    return postings


class _PostsNothing(Exception):
    """A document that is real, carries no money, and moves no account.

    A zero-total document with no lines is a QuickBooks artefact -- an entry
    started and abandoned, or one whose lines were all removed. It posts
    nothing, so there is nothing to get wrong, and blocking a client's entire
    ledger over it would be a refusal with no accounting meaning behind it. The
    distinction that matters is the *total*: a document stating an amount with
    no lines to explain it is genuinely underivable and still blocks.
    """


# ------------------------------------------------------- provider agreement --

@dataclass
class ProviderComparison:
    agrees: bool
    differences: list[dict] = field(default_factory=list)
    accounts_compared: int = 0
    note: str = ""


def compare_to_provider(derived: dict[str, Decimal],
                        provider: dict[str, Decimal]) -> ProviderComparison:
    """Require our reconstruction to equal QuickBooks' own trial balance.

    Exact equality, account by account, in both directions. An account we have
    and the provider does not is as much a failure as a differing amount: it
    means we invented a balance. An account the provider has and we do not means
    we missed transactions -- most likely one of UNREAD_POSTING_TYPES.

    No tolerance. These are the same ledger read two ways; a cent of drift is a
    defect, not rounding.
    """
    differences = []
    for account in sorted(set(derived) | set(provider)):
        ours = derived.get(account)
        theirs = provider.get(account)
        if ours is None:
            differences.append({"account": account, "derived": None,
                                "provider": str(theirs),
                                "reason": "provider holds a balance we did not reconstruct"})
        elif theirs is None:
            if ours != Decimal("0"):
                differences.append({"account": account, "derived": str(ours),
                                    "provider": None,
                                    "reason": "we reconstructed a balance the provider does not report"})
        elif ours != theirs:
            differences.append({"account": account, "derived": str(ours),
                                "provider": str(theirs),
                                "reason": "balances differ"})
    return ProviderComparison(
        agrees=not differences,
        differences=differences,
        accounts_compared=len(set(derived) | set(provider)),
        note="" if not differences else
             "The reconstruction does not tie to QuickBooks; balances are not trustworthy.",
    )


class ProviderReportError(ValueError):
    """QuickBooks' trial balance could not be read without inventing a number."""


def provider_balances(report: dict) -> dict[str, Decimal]:
    """Read QuickBooks' own TrialBalance report into account-keyed balances.

    The report is a nested grid: `Rows.Row` holds data rows and sections, and a
    section holds more rows and a summary. Only the data rows are read -- a
    section summary is a subtotal of rows already counted, and adding it would
    double every grouped account.

    Rows are matched by the account id QuickBooks puts on the first cell, never
    by account name. Two accounts may share a name across different parents, and
    a name is something a client can rename between two pulls.

    A row whose account cannot be identified raises rather than being skipped.
    The whole purpose of this function is to prove the reconstruction is
    complete, and quietly dropping a row would make an incomplete reconstruction
    agree with a shortened report.
    """
    balances: dict[str, Decimal] = {}

    def walk(rows: list) -> None:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            nested = (row.get("Rows") or {}).get("Row")
            if nested:
                walk(nested)
            columns = row.get("ColData")
            if not columns:
                continue
            if (row.get("type") or "Data") not in {"Data", ""}:
                continue
            account_id = str((columns[0] or {}).get("id") or "").strip()
            if not account_id:
                # A total line carries no id. So does a row for an account the
                # report names but does not identify, and the two are not
                # distinguishable here, so neither is guessed at.
                label = str((columns[0] or {}).get("value") or "").strip()
                if label and not _looks_like_a_total(label):
                    raise ProviderReportError(
                        f"The trial balance row {label!r} names no account id, so "
                        "it cannot be matched to the chart. Reading the report "
                        "without it would let an incomplete reconstruction agree "
                        "with a shortened report.")
                continue
            debit = _report_money(columns, 1)
            credit = _report_money(columns, 2)
            balances[account_id] = balances.get(account_id, Decimal("0")) + debit - credit

    walk((report.get("Rows") or {}).get("Row") or [])
    return {key: value.quantize(Decimal("0.01")) for key, value in balances.items()}


def _looks_like_a_total(label: str) -> bool:
    return label.strip().upper().startswith("TOTAL")


def _report_money(columns: list, index: int) -> Decimal:
    if index >= len(columns):
        return Decimal("0")
    raw = str((columns[index] or {}).get("value") or "").strip()
    if not raw:
        return Decimal("0")
    try:
        return money(raw.replace(",", "").replace("$", ""))
    except (ArithmeticError, ValueError) as exc:
        raise ProviderReportError(
            f"The trial balance carries {raw!r} where an amount was expected") from exc


def verify_against_provider(objects: dict[str, list[dict]],
                            report: dict) -> ProviderComparison:
    """Reconstruct, then require QuickBooks to agree with the reconstruction.

    The one entry point a caller should use. Refuses before comparing when the
    reconstruction is incomplete: an incomplete ledger disagreeing with the
    provider says nothing about whether the derivation is right.
    """
    derived = derive_ledger(objects)
    if not derived.complete:
        return ProviderComparison(
            agrees=False,
            differences=[{"account": "*", "derived": None, "provider": None,
                          "reason": reason} for reason in derived.reasons()[:5]],
            note=("The ledger could not be fully reconstructed, so there is "
                  "nothing to compare against QuickBooks."))
    return compare_to_provider(derived.balances, provider_balances(report))


# Kept as the plain name for direct callers and tests.
derive = derive_ledger
