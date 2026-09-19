# Bookkeeping Work Engine v0

The first customer-facing expression is the **Free Cleanup Check**. The engine
is the controlled system underneath it:

`QBO connect → reconstruct → detect → evidence gate → propose → review → approve → execute → read back → reconcile → working papers`

## What is implemented

- Provider-neutral SQLite records for runs, accounts, customer/vendor entities,
  projects, classifications and tax codes, transactions and lines, documents,
  allocations, findings, evidence requests, proposals, approvals, executions,
  reconciliations, and working-paper packages.
- A paginated QBO entity adapter for accounts, customers/jobs, vendors, classes,
  tax codes, invoices, payments, bills, purchases/expenses, deposits, journal
  entries, and attachments.
- A bounded sandbox mutation catalogue. There is no delete operation. Updates
  are sparse and require the reviewed `SyncToken`; creates use a stable
  `requestid`; attachments use QBO's metadata/content multipart upload.
- A proposal state machine with exact versioned payloads and approve, edit,
  reject, and escalate decisions. An edit creates a new version and requires a
  fresh approval.
- Read-after-write verification, provider IDs and returned `SyncToken`s in the
  execution record, reconciliation output, and a hashed JSON working-paper
  package.
- Reconciliation persisted on every run and recomputed after every execution.
  An account with no statement records `no_source` and names the missing
  source; a missing statement must never read as a clean result.
- Separation of duties enforced, not assumed. Roles gate approval and release;
  the person who edited a proposal cannot be its only approver; the approver
  cannot release their own approval. The single-operator exception exists
  because a one-person company cannot separate them, and it is recorded on the
  execution — who approved, who released, and why it was permitted — rather
  than waved through.
- A `CHECKS` registry reporting a status for every advertised check on every
  run, so coverage is stated rather than inferred from whichever findings
  happened to fire.
- A reviewer console linked from each connected engagement. This is an internal
  operator console, not an external accountant portal — there is no firm
  tenancy and no referral attribution yet.
- A reporting projection over the same canonical tables, emitting the
  client-facing Cash-Leak Review from a connection rather than from CSVs. One
  ingest, two projections; see `REPORTING_PROJECTION.md`.

## Synthetic acceptance oracle

`shimline.synthetic_books` creates independent messy QBO-shaped objects,
external evidence, and a golden ledger. The engine receives only the first two.
The 50-company acceptance test seeds all of these in every company:

- duplicate transaction;
- missing bank transaction;
- unapplied payment;
- incorrect job allocation;
- GST/HST coding error;
- stale receivable with deliberately missing support;
- unreconciled bank account.

The test asserts exact defect-set equality (both recall and no invented
findings), an evidence request rather than a guessed stale-A/R correction,
stale-token recovery, recovery from a lost response after commit, one provider
write per approved proposal, golden trial-balance equality, control-account
reconciliation, and a complete hashed working-paper package.

Run the gate from `jobmargin_build_v1`:

```powershell
python -m pytest -q backend/test_qbo_adapter.py backend/test_work_engine.py backend/test_reporting.py
```

Beyond the 50-company gate, the same file covers check-coverage reporting,
reconciliation persistence and refresh, and separation of duties.

## A second engine has to agree

`work_engine` imports `trial_balance` from `synthetic_books` -- the module
that also generates the golden ledger the acceptance suite grades against.
The implementation being validated and the one validating it are the same
code, so a wrong double-entry assumption would be wrong in both places and
the suite would still pass.

`shimline/beancount_export.py` closes that gap, and since
`with_derived_postings` exists it reaches real client books rather than
only synthetic ones. It writes the same transactions out as a Beancount ledger with Shimline's own trial balance
embedded as `balance` assertions, and `test_beancount_oracle.py` runs
`bean-check` over the result. Beancount recomputes every balance by its own
rules and refuses the file if it disagrees.

It already covers a bug class `trial_balance` structurally cannot see: that
function accumulates debits minus credits per account and never checks that
one transaction's own postings sum to zero. The `~ 0` on every assertion is
load-bearing -- without an explicit tolerance Beancount infers one from the
file's precision and a one-cent disagreement passes silently.

Beancount is GPL-2.0 and lives in `requirements-dev.txt`: never imported,
never shipped, executed as a subprocess. If anyone writes `import beancount`
in `shimline/`, that licence analysis changes and needs review first.

## Two projections over one ingest

`persist_canonical` is the only thing that writes the canonical ledger, and two
readers sit on top of it:

- `work_engine.analyze` is defect-shaped — one record per problem, with an
  evidence gate and a write proposal. It feeds the reviewer console.
- `reporting.portfolio` is portfolio-shaped — aging buckets, per-project
  margin, health scores, one headline exposure figure. It feeds the client PDF.

Neither answer substitutes for the other, and neither may be computed from its
own copy of the ledger. A figure the projection cannot compute is omitted and
named in an `unavailable` list using the same `blocked` / `manual` vocabulary
the check registry uses, so the console and the PDF tell the same story.

## Check coverage: thirteen detectors against sixteen advertised checks

The engine automates thirteen defect types, covering twelve of the sixteen
advertised checks plus two engine checks that are not on the published list.
That gap is real, and the rule is that it must be *stated on every run* rather
than left to be inferred:

- `clean` — the check ran against this file and found nothing. A result.
- `defect` — found; see the findings.
- `blocked` — computable, but a required source is absent. Names the source.
- `manual` — v0 does not automate it; a person covers it during review.

`work_engine.CHECKS` is the single place a check is declared, and
`coverage["checks_automated"]` is deliberately reported separately from
`checks_total`. Adding a detector means moving one entry from `manual` to
`automated`. **A check that appears on the website and not in that registry is
the failure mode the registry exists to prevent.**

## The accountant console

The commercial thesis is that an accountant holds the decisions while Shimline
does the bookkeeping. That needs three things the internal console did not have.

**A firm is a tenant.** `shimline/tenancy.py` resolves one session to a `Scope`,
and a Scope is the only thing that answers "which clients". Three kinds:
`internal` (Shimline's own staff, unrestricted), `firm` (a principal sees every
client the firm holds; staff see only their assignments), `none` (sees nothing).
Firm membership is resolved **before** the workspace roles, because a principal
needs the `reviewer` role to approve anything at all and reading roles first
would promote every reviewing accountant to seeing every client in the system.

Two decisions inside it are load-bearing:

- **Out of scope answers 404, never 403.** A 403 confirms the record exists, so
  an outsider could enumerate client ids by watching which ones answer
  differently. A test asserts that a guessed id and a real id belonging to
  another firm return the same status *and the same body*.
- **An empty scope filters to nothing, not to everything.** `IN ()` is a SQL
  error, and the tempting fix -- drop the clause when the list is empty -- turns
  a firm with no clients into a firm with all of them. `sql_filter` emits an
  always-false predicate, with a test for exactly that case.

Three tables rather than one: `firm_clients` is which clients the firm is
engaged for, a commercial fact; `firm_client_assignments` is which of those a
particular staff accountant may open, an access-control fact. Collapsing them
would mean adding a client to a firm silently granted it to every junior.
`scripts/firm_admin.py` is the operator path; `tenancy.create_firm_user` refuses
to leave a new account unscoped rather than trusting whoever provisions it to
remember both steps.

`test_tenancy_routes.py` drives the real router as one firm against the other's
ids, and its last test enumerates the router and fails when a new client-bearing
route appears that nobody has decided about. That test was itself broken once:
FastAPI keeps an included router nested rather than flattening it into
`app.routes`, so the walk found one admin route out of forty-two and every
assertion after it was trivially true. It now asserts it found a plausible
number of routes before concluding anything from them.

**The portfolio queue** (`shimline/portfolio.py`, `/admin/portfolio`) is one view
across every client in scope, grouped by where the next hour goes: decisions
waiting, waiting on the client, waiting on us, clean, not yet scanned. Within a
state the larger exposure comes first. *Waiting on us* is a state of its own on
purpose -- when our reconstruction and QuickBooks disagree, a reviewer must not
spend the morning chasing a client for a document they already sent.

**Provenance** (`shimline/provenance.py`) is where the care went, and the care is
mostly restraint. It would be easy, and wrong, to tell an accountant that three
independent engines verified their client's books. Two run per client: the
reconstruction, and QuickBooks' own trial balance *where the reports have been
synced*. Beancount runs in CI over the engine, not over this ledger, because
production hosts do not install it. `engine_assurance` states that plainly and
is deliberately **not** one of the per-client checks. A check has three states,
not two: one that did not run is not a failure -- a client who never synced has
done nothing wrong -- and it is certainly not a pass.

**Proposals are shown as field changes** (`shimline/diffing.py`), in accounting
words: "Line 1 · Project: — → P-2 Maple St", with the exact documents one
disclosure away. The console previously showed two JSON blobs side by side,
which in practice means a reviewer approving twenty proposals reads none of them
and the approval gate becomes a button. Every difference is shown -- a field
with no friendly label is rendered with its raw path rather than dropped -- and
no value is ever reformatted, because `1200` becoming `1200.00` is either a real
change or it is not.

**A batch is all or nothing.** Each checkbox carries a fingerprint of the diff
that was on the screen; every one is re-checked before anything is written, and
a single mismatch refuses the whole batch with nothing decided. Volume is what
makes an accountant fast and it is the same volume that would let an unread
change reach a client's books; partial application would be worse still, because
the reviewer would have to work out which half landed. A batch can decide but
never execute: approval and release stay separate acts.

## GST/HST returns

The first regulatory obligation. It prepares the four figures an accountant
signs -- lines 101, 105, 108 and 109 -- and **files nothing**. Submission to the
CRA is not built and must not be built speculatively.

The refusal is stricter here than anywhere else in the system, for a reason
worth writing down: a return is *signed*, it goes to the CRA under the client's
name, and nothing downstream would ever reveal a wrong figure. One document
whose tax cannot be accounted for, or one rate nobody has sorted onto a return,
and `gst_return.prepare` produces no return at all. Not a partial one, not an
estimate, not a figure with a caveat nobody reads. `GSTReturn.figure()` raises
rather than handing over a number from a blocked period.

**The rate classification is the part that matters.** PST and QST are not on a
GST/HST return -- provincial sales tax is not an input tax credit. Claiming it
would overstate the credit, and the return would look entirely reasonable doing
it. `shimline/tax_rates.py` classifies every rate from the tax agency
QuickBooks names it under, falling back to the rate's own name only when that is
unambiguous. "GST" and "PST BC" settle it; "Tax", "Sales Tax" and "Standard"
settle nothing and go to a person. A combined "GST/PST BC" rate is refused
rather than split, because part of it belongs on the return and part does not
and nothing here can separate them. An operator's decision is stored in
`bookkeeping_tax_rate_overrides`, apart from the synced row, so a routine
re-pull cannot quietly reverse a judgement about a CRA figure.

**Two readings, with the limit of the claim stated.** `sales_tax.period` reads
`TxnTaxDetail` as Intuit published it; the second reading is the reconstructed
double entry on the client's tax liability accounts over the same period. They
come from different places, so agreement is evidence and disagreement blocks.
The posted reading validates the **totals**, not the GST-versus-provincial
split -- postings land on whichever account the client uses, and many clients run
one tax account for everything. The split rests entirely on the classification,
which is why an unclassified rate blocks rather than being assumed onto a side.
The console says this rather than implying more.

**Filing periods are the client's, not the calendar's**
(`shimline/filing_periods.py`). A quarterly filer with a June 30 year end files
July to September, and assuming January would put a return on a period the
client does not file. Two bugs were found by running it: chaining month
subtraction walked a December 31 year end back to November 30 and then October
30, losing a day at every short month; and a June 30 year end produced quarters
ending March 30, which is not a quarter end anywhere. Both are pinned by tests
that name the failure.

Due dates are a rule with one exception, and the exception is *recorded* rather
than assumed. Monthly and quarterly returns are due one month after the period
ends. An annual return is due three months after the fiscal year end -- unless
the registrant is an individual with a December 31 year end, in which case the
return is due June 15 and the **payment** April 30. Those two dates differ,
which is unusual enough that a client whose status nobody has recorded gets no
due date at all rather than one that may be three months wrong.

An unclassified rate that is never *charged* does not block anything. A client
may hold rates they do not use, and blocking on one would make every return
conditional on tidying a list that has no bearing on it -- an accountant who
learns the blocks are noise stops reading them.

The GST/HST lane is also generated now, rather than defended only by examples
somebody anticipated. Three hundred generated periods mix federal, PST, QST,
unknown and absent rates across sales, purchases and reversals. The invariants
pin exact Decimal line 109 arithmetic, provincial-tax exclusion, the two method
refusals, and `prepare`/`period` never raising. Six hundred generated fiscal
arrangements pin contiguous inclusive periods, month ends through the 29th,
30th and 31st, increasing due dates, and the December 31 individual exception.
Both generators shrink a failure before printing it.

## A pull has to say what it read

The subtlest hole this architecture has had, and the only one that was invisible
to every other check.

`derive_ledger` receives a dict of provider objects. An entity that **failed to
load** and an entity **with no rows** arrive as the same thing: an absent key. So
a pull that lost every invoice produced a ledger reporting itself *complete*,
with revenue and receivables simply missing — and the trial balance still summed
to zero, because both halves of every invoice went missing together. The
double-entry invariant could not see it. Beancount could not see it: it
recomputes the same postings and reaches the same wrong answer. Only knowing
what was *supposed* to be there can see it.

`QBOAdapter.pull_all` now records which entity types it read and how many rows
each returned, under `_pull_manifest`. `derive_ledger` blocks on any posting type
the manifest does not cover, and `work_engine.trusted_ledger` — the one place a
ledger is handed to a check — refuses objects that carry no manifest at all. A
*malformed* manifest blocks rather than reading as absent: treating a mangled
record as "none supplied" would turn a damaged pull into a fixture and wave it
through the check it was meant to fail.

`declare_pull` stamps a fixture or the synthetic oracle as a complete pull. That
is not a way around the gate — it asserts every type *was* read, so a caller who
stamps an incomplete dict is stating something false rather than skipping a
check. The gate exists to stop a silent absence, not a declared one. The
synthetic oracle uses it, so the acceptance suite exercises the path production
takes rather than one that skips the gate.

`pull_all` also names the entity that failed. Every entity read posts to the same
`/query` URL, so without that an operator saw "query failed (HTTP 400)" with no
way to tell the chart of accounts from the tax rates.

The pull now measures another kind of silent absence without changing query
semantics: first contact counts every account, customer/job, vendor and class id
named by a posting document but absent from the corresponding name-list rows.
It reports one assumption per entity. This observes the active-only failure
shape; it does not claim every absent id was archived. The decision is to read
inactive rows for historical resolution only after the entity-specific query is
proved on a real sandbox file, while continuing to refuse inactive targets for
new postings and assignments.

**Measured cost of a pull.** 21 entity calls plus 7 reports for a client inside
the supported envelope; 35 entity calls at a thousand transactions, 91 at five
thousand. Intuit meters data-out at 500 requests per minute per realm, so this
is not close to a constraint.

## What a client still needs, said before the scan

Nine things have to be true before a run produces a full answer — connection,
the first file read, a project-tracking choice, reports, statement, statement
mapping, filing arrangement, rate classification, and the scan itself — and
every one of them used to be discovered *afterwards*,
as a block with no instruction attached. `shimline/readiness.py` says the same
things earlier, in order, each with somewhere to go.

It is deliberately not a second source of truth: every step reports the same
condition the engine already refuses on, read from the same tables. If a step
says the statement is mapped and reconciliation still says `no_source`, the
readiness module is wrong and the engine is right.

Only the connection is marked **blocking**. Calling a missing GST filing
frequency as urgent as a missing QuickBooks connection would be false, and the
second time an operator noticed it was false they would stop reading the list —
the same reasoning as "a detector that fires on everything is worse than no
detector", applied to a checklist.

Choosing not to use QuickBooks jobs is also readiness, not a monthly defect.
Check 16 reports only the inconsistent state where jobs exist and revenue uses
them while cost does not, or the reverse. A file with no jobs is asked once what
it intends to do; it is not told on every scan that its books are broken.

The readiness step that received that question asks about **use, not
existence**. Jobs that exist and that no transaction line ever names produce an
empty project-profitability report, and a step that ticked on existence alone
would call that client configured while the report came back blank — a green
tick on a decision nobody made. Across 200 generated companies, 31 sit in
exactly that state, so it is the common case rather than the corner. The step is
done when at least one recorded line names a project, and it distinguishes the
three ways it can be outstanding: no jobs at all, jobs with nothing read yet,
and jobs nothing uses.

## Shapes a real file contains and a fixture does not

The derivation was probed with five ordinary QuickBooks states that had never
been put in front of it. One was a silent error of **thirty-seven percent**.

**Foreign currency blocks.** A foreign-currency document posts to the ledger in
the *home* currency at the rate on the document, but its `TotalAmt` and every
line amount are stated in the transaction currency. A USD 100 bill at 1.37
landed as 100.00 CAD instead of 137.00 — and balanced perfectly, because both
sides carried the same wrong number. Double entry holds. Beancount recomputes
the same postings and agrees. Only `compare_to_provider` could have caught it,
and only for a client whose reports have been synced.

Converting is deliberately *not* the fix yet: rounding the rate per line and
rounding it on the total give different answers to the cent, and which one
QuickBooks uses has never been checked against a real multi-currency file. So it
refuses — blocking a multi-currency client rather than mis-stating one.
`ExchangeRate` is the signal; QuickBooks populates it only on foreign documents,
and an unreadable rate blocks rather than being read as 1.

**A line with no `Amount` blocks.** It used to be dropped. On a journal entry
that leaves an entry which still balances while missing a posting — the one
failure the document-level balance check structurally cannot catch. Subtotal and
description-only rows are still skipped: they carry no money by design, which is
a different thing from a row that should and does not.

**An abandoned document posts nothing.** A zero-total document with no lines is
a QuickBooks artefact — an entry started and abandoned, or one whose lines were
removed. It moves no account, so blocking a client's entire ledger over it is a
refusal with no accounting meaning behind it. The *total* is what separates an
artefact from a real document nobody can explain: a stated amount with no lines
still blocks, and so does a zero-amount `Transfer`, which carries `Amount`
rather than `TotalAmt`.

Checked and already correct, recorded so nobody re-checks them: a voided
document posts zeroes, a discount line reduces the expense, and a document with
no date still derives — placing it in a period is a separate problem that
`matching.cash_movements` refuses on its own.

## Three copies of one list, and the one that mattered

`POSTING_TYPES` names the twelve document types the engine derives. Three other
modules kept a hand-written copy of it, and two had gone stale.

`beancount_export.POSTED_KINDS` was caught earlier: it still named the original
six types, so six kinds of document counted toward Shimline's balances while
being silently absent from the file the oracle checked.

`work_engine.POSTED_KINDS` was the same defect with a worse consequence. It feeds
`postings_derivable`, which treats "no posted transactions" as *zero really is
this company's balance* and returns `True` without consulting the derivation. A
cash-basis trades business ringing everything through the till — every document a
`SalesReceipt` — therefore read as having a usable ledger even when none could be
derived, and every balance-comparing check reported `clean`, meaning *ran and
found nothing*, over books that could not be rebuilt at all. Both are now
`POSTING_TYPES` itself, and a test asserts identity rather than equality.

**The acceptance corpus held four of the twelve types.** Counting documents rather
than dictionary keys: `Deposit` and `JournalEntry` read as covered because the
generator declared the keys and set them to `[]`. So the golden ledger, every
seeded defect and the Beancount oracle had never seen a settlement or a reversal,
which is where a real contractor's file is dense. All eight are now generated,
each drifted a cent and confirmed caught by the second engine. The refund
deliberately is not the same amount as the sale: with tax collected netting to
zero across the corpus, a sign inversion on the tax leg cancels itself out and no
balance check anywhere can see it.

## The gate that could not see the oracle

`.github/workflows/quality.yml` runs the suite on Ubuntu, where beancount
installs, plus ruff, pyright, bandit and pip-audit. **It has never executed**:
this repository has no Git remote. `scripts/deploy_workspace.sh` is the only gate
that exists, and it ran `python -m pytest` on the release machine — Windows on
ARM64, which has no beancount wheel, so the ten oracle tests skipped and the gate
went green at `675 passed, 11 skipped`.

The gate now selects an interpreter that can actually run `bean-check`, falling
back to the WSL environment, and **refuses to deploy if none can**. Neither
interpreter covers everything on its own — WSL has no WeasyPrint and skips the
PDF tests — so it runs both. It also runs ruff, since nothing else ever has.

That also makes an assurance shown to accountants true. `provenance.engine_assurance`
claimed the build fails if Beancount disagrees by a cent; until this change no
build ran Beancount at all. The wording is now "every release", because that is
the gate that enforces it.

## The adapter against a server

Every adapter test built its subject with `object.__new__(QBOAdapter)` and
replaced `_request`. Convenient, and it left the transport untested: URL
construction, the bearer header, the `QueryResponse` envelope, `STARTPOSITION`
arithmetic, refresh-on-401, the 429 and 5xx retries, and the mapping from an HTTP
status to a `QBOError` had never run as part of a whole pull. A real connection's
first failure lands in exactly that seam, and none of it needs a consented
connection to exercise.

`qbo_conformance` serves the documented shapes over real HTTP for all 21 entities
in `READ_OBJECTS` — `TaxAgency` included, which was added from documentation and
has never been read from a live company. Production code runs against it
unchanged and reaches a complete derived ledger that sums to zero.

Its company is deliberately **not** `synthetic_books`. Those rows carry
`_Postings`, which QuickBooks never sends, so reusing them would check the engine
against its own assumptions a second time. These rows carry fields the adapter
does not read, because a real payload is wider than its reader and a reader that
breaks on an unread field breaks on every real file.

The pagination tests would otherwise prove only that nothing crashed, so the
server is made to demonstrate it can distinguish a correct pager from three
plausibly-wrong ones. An off-by-one in `STARTPOSITION` does not fail loudly — it
repeats or drops exactly one row per page boundary, which on a real file is one
double-counted purchase.

**What this cannot establish:** whether Intuit matches its own documentation. The
harness can only be wrong in the way the documentation is wrong. It proves the
rail runs end to end; the remaining question needs a real file.

## The file reads itself

A dozen facts about Intuit's API were taken from documentation and had never been
checked against a live company. Is `TaxAgency` queryable at all? Does `TaxLine`
carry an `Amount`? Does every `Purchase` name the account that paid it? Each
wrong assumption is a client whose ledger blocks, and finding out was waiting on
somebody being at a keyboard at the moment a connection was made.

`shimline/first_contact.py` runs on the keepalive timer instead. It probes any
newly connected file and records row counts, the field names a real company
returns, every documented assumption judged, and every refusal in the order the
derivation produced them — so a file connected at two in the morning has an
answer waiting by morning.

Three rules it holds to:

1. **An assumption nothing in the file could settle is `unknown`, never `held`.**
   A company with no taxed sale says nothing about `TaxLine`, and recording that
   as a pass manufactures evidence — the same defect as a checklist step ticked
   on day one.
2. **A failed pull is a result and is kept.** "The pull died on `TaxAgency`" is
   the most valuable thing this could discover.
3. **It reports; it does not fix.**

It also records `documents_refused / documents_seen`, which is the per-document
refusal rate on real data — the `p` the blocking model below has only guessed at.

## Every way the engine can refuse, fired at least once

Measured rather than assumed: **16 of the 33 refusal sites in `postings.py` had
never executed.** An untested refusal path is worse than an untested happy path.
It fires exactly when a client is blocked, its message is the only thing the
operator has to go on, and the all-or-nothing design means one of them takes out
the whole ledger. A sentence nobody has read is a sentence nobody has checked.

`test_refusal_paths.py` fires all 33 and asserts what each says. It enumerates
the raise sites from the source and fails if any did not fire, so a new refusal
cannot be added without exercising it.

`test_derivation_properties.py` now generates damaged documents across every
posting type and asserts the invariants fixtures cannot discover: every document
is posted, refused, or explicitly recorded as moving nothing; each posted
document and every complete ledger balance; derivation is deterministic and
does not mutate its input; and documents derive independently. The first run
found an ID-only BillPayment being silently treated as a zero-value abandoned
document. `DerivedLedger.every_document_accounted_for` now makes that class of
loss observable, and a missing total with no lines refuses rather than reading
as zero.

## What all-or-nothing costs, as arithmetic

`derive_ledger` refuses all-or-nothing. With `n` documents and a per-document
refusal chance `p`, P(the whole ledger blocks) = 1 − (1 − p)ⁿ. Inverted: at one
refusal in ten thousand, a five-percent blocking budget buys **512 documents**,
which is a small contractor.

`scripts/blocking_risk.py` renders the curve and counts the known refusal paths
per document type. It states plainly that `p` is not known, and that no number
from the synthetic corpus belongs in the table — a generated corpus measures the
generator. The decision this points at, moving fail-closed from per-ledger to
per-check with a stated quarantine, is recorded in
`research/OWNER_UNBLOCKED_PLAN.md` as a decision rather than taken.

## The GST/HST lane against the CRA's own words

Every rule in the lane that could be read from a CRA primary source now is, and
each test in `test_cra_rules.py` names its source. A rule remembered correctly is
indistinguishable from one remembered incorrectly, and this lane produces a figure
an accountant signs.

Confirmed correct as already built: monthly and quarterly returns due one month
after the period ends; annual due three months after the fiscal year end; and the
individual with a December 31 year end filing June 15 while paying April 30 — the
only place in the lane where those two dates diverge.

Added: the reporting periods the CRA assigns by annual taxable supplies
($1.5M or less annual, to $6M quarterly, above that monthly) and which elections
each band permits. **Deliberately not a default.** A client filing monthly on
$400,000 of supplies is entirely ordinary, so guessing "annual" for them would
prepare a return for a period they do not file. It answers the other question:
whether a recorded frequency is one the CRA permits at all — above $6M there is
no election, so a quarterly filer that size is either mis-recorded or filing late
every quarter, and those need different responses.

The published rate table is recorded with Nova Scotia at 14% from 2025-04-01, and
deliberately not yet wired into a check: the useful check needs effective dates
applied per document date, and doing that carelessly flags every historical
document.

What remains genuinely ambiguous for a construction contractor — holdback,
progress billings, vehicle apportionment, the 50% meals restriction, Quick
Method, self-assessment, bad debt relief, place of supply, and what may be said
to a client — is nine questions in `research/GST_HST_LEGAL_QUESTIONS.md`. The
Quick Method no longer produces a confidently wrong regular-method return: the
filing arrangement asks which method the client uses, and the lane refuses a
known Quick Method filer until that arithmetic is implemented. The remaining
legal questions still need answers before those narrower rules can ship.

## Deliberate v0 boundaries

- Writes are hard-blocked outside `QBO_ENVIRONMENT=sandbox`.
- Unapplied payments are detected and escalated; applying cash is not in the v0
  write catalogue.
- A stale receivable is not itself proof of payment or bad debt. The engine
  requests remittance, customer confirmation, or bad-debt approval.
- Bank statements are ingested (`shimline/statements.py`, OFX/QFX via
  ofxparse, and CSV), stored with the SHA-256 of the uploaded bytes, and
  offered to the engine as evidence only once an operator has mapped the
  bank account to the QBO account it proves. An unmapped statement proves
  nothing and is withheld, so reconciliation keeps reporting `no_source`.
- **The ledger is reconstructed from the provider's documents.**
  QuickBooks publishes documents, not postings. `shimline/postings.py` rebuilds
  the postings each document implies -- every posting type QuickBooks publishes:
  Invoice, Payment, Bill, Purchase, Deposit, JournalEntry, BillPayment,
  CreditMemo, VendorCredit, Transfer, SalesReceipt and RefundReceipt, including
  sales tax -- so a live pull produces a real trial balance and check 04 runs
  against it. The six settlement and reversal types were added because their
  absence was a ceiling, not a detail: a client who had paid a bill or moved
  money to savings got the whole ledger refused, which is every client with a
  chequing account. Reconciliation worked on tidy files, not on books.
  It refuses rather than guesses. A document whose lines do not sum to its
  total, tax with no identifiable account, a payment naming no funding account
  or naming two, a transfer missing a side, a sales receipt QuickBooks defaulted
  to Undeposited Funds without saying which account that is, a second A/R
  account the document does not disambiguate, or an object type the module does
  not recognise at all -- each blocks the whole ledger. A partial reconstruction
  is worse than none: one unhandled type silently shifts an account, and a
  silently shifted balance is a wrong set of books that looks right. When it
  blocks, check 04 reports `derived_ledger_postings` as the missing source.
  The three reversals are derived by running their forward rule backwards, so a
  credit memo cannot drift away from the invoice it reverses.
- **Three engines, one ingest.** `work_engine.trusted_ledger` will not hand the
  reconstruction to any check unless it is complete *and*, where QuickBooks'
  own `TrialBalance` report has been supplied, agrees with it exactly, account
  by account, with no tolerance. Beancount recomputing the same postings from
  scratch is the second engine; QuickBooks is the third, and the one an
  accountant can check unaided because it is the report they were going to open
  anyway.
  A disagreement is deliberately **not** a finding about the client. Two
  readings of the same books gave two answers, so at least one of them is ours;
  telling a client their books do not reconcile on that basis would be an
  accusation we cannot support. The ledger is withdrawn instead, dependent
  checks report `blocked`, and the missing-evidence line says the gap is ours so
  a reviewer does not chase the client for a document they already sent.
- **Check E2 matches statement lines against the reconstruction.**
  `shimline/matching.py` pairs every statement line with the cash movements
  derived from the client's own documents: exact amount, a five-day window for
  cheques clearing late, and vendor-name evidence that survives a bank closing
  up the spaces in "HOME DEPOT". Assignment is global rather than first-come, so
  the same books cannot reconcile twice with two answers depending on the order
  the statement arrived in. Three deliberate limits:
  a line nothing accounts for is **reported, never proposed** -- a bank
  descriptor does not say which account a charge belongs to, and the
  corroborated `missing_transactions` path still proposes because there a source
  document agrees; two equally good candidates are **refused** and left to a
  person; and a ledger entry with no statement line is **not a finding**,
  because an outstanding cheque is the ordinary state of a month-end. The counts
  ride along in `coverage["statement_matching"]` and on each reconciliation
  record, because a tied closing balance is not a tied ledger -- two errors that
  cancel agree to the cent.
  This was measured, not assumed. The capability report recommended splink for
  it; against this deterministic baseline splink lost on recall at 182, 2,002
  and 12,002 rows and guessed both of two identical transactions every time.
  Reproduce with `research/probes/splink_probe.py`.
- **Checks 09 and 12 are automated.** Both were blocked on the generator rather
  than on a detector: `synthetic_books` now emits item-based lines carrying
  UnitPrice and Qty, and invoice lines carrying a CustomerRef, so a priced
  history and a per-job margin exist to develop rules against. Check 09 reports
  negative margin only; check 12 compares a unit price against the median of
  that vendor and item's own history, and only once a baseline exists.
- Check 13 (stale outstanding bills) is automated: a bill past its due date
  that still carries a balance. Reported, never proposed -- paying a vendor
  moves money and is the client's decision.
- **Checks 05, 15 and 16 are automated.** Check 05 flags only QuickBooks' own
  `Ask My Accountant` and `Uncategorized Expense` holding accounts. Check 15
  reports balances in Opening Balance Equity or Undeposited Funds and credit
  balances on asset accounts, but never proposes a correction. Check 16 verifies
  that QuickBooks jobs exist and both revenue and cost lines use them; it does
  not pretend to judge whether an individual allocation is right. Check 14 still
  needs an owner-account policy from the client.
- **Quarantine is now measurable, not enabled.** The first-contact probe records
  which refused document types it saw and the conservative subset of the
  published sixteen checks whose complete inputs exclude those types. Ledger-
  wide, missing-source and human checks are never counted. The page makes clear
  that no document was skipped and production remains fail-closed; this is the
  evidence for a later architecture decision.
- **A bookkeeping scan refreshes its Trial Balance.** The comparison is bound to
  the exact sync run the scan triggered. If that refresh fails, the third check
  reads `not_supplied`; it never falls back to an older successful snapshot and
  manufactures a disagreement between books observed at different times.
- The client-facing Cash-Leak Review can now report job margin. It could
  not before: `reporting.py` declared `project_profitability` blocked with
  "no invoice line is tagged to one", which was true of every file the
  generator produced.
- Estimates are read and persisted but the estimate-versus-actual comparison
  is not automated: it needs a materiality rule, which is an accounting
  judgement rather than a detector.
- **Sales tax is recorded at the grain QuickBooks states it.** The persisted
  line tax used to be hard-coded `0.00` -- not unknown, zero -- so anything
  sales-tax-facing built on those tables would have produced a nil return and
  looked correct doing it. Migration 014 stores the document total and one row
  per tax *rate*, carrying the net amount that rate was charged on, because that
  is what the provider publishes and what a GST/HST return asks for. Tax is
  never apportioned down to lines: QuickBooks does not state it per line, and a
  number invented here would be filed with the CRA under the client's name.
  `tax_amount_source` on a line distinguishes a genuinely zero-rated line from
  one nobody filled in.
  `shimline/sales_tax.py` totals a period per rate and mostly refuses to: a
  document stating a tax total with no rate breakdown, or whose rates do not add
  up to the total it states, blocks the **whole** period. Reporting the good
  half would be a smaller number that looks right. This is a foundation, not a
  return -- no filing figure is computed here.
- Production enablement requires sandbox fixtures using Intuit's actual
  Canadian tax objects, accountant sign-off on proposal templates, and an
  explicit configuration/code change. It is not unlocked by OAuth alone.
