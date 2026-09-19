# Reporting projection

The loop from "client connected QuickBooks" to "client receives their report"
is now closed with no CSV anywhere in it.

```
QBO pull -> persist_canonical -> canonical tables -+-> work_engine.analyze  -> reviewer console
                                                   |
                                                   +-> reporting.portfolio  -> findings.json -> PDF
```

One ingest, two projections. The engine answers "what is wrong with this
file"; the report answers "what does this portfolio look like". They read the
same tables, so a detector and a client-facing number can no longer drift onto
different copies of the ledger.

## What was built

| | |
|---|---|
| `backend/shimline/reporting.py` | The projection. Reads `bookkeeping_transactions`, `bookkeeping_transaction_lines` and their dimensions; emits the `findings.json` contract plus `unavailable`. |
| `backend/shimline/report_fixture.py` | Turns `data/*.csv` into QuickBooks-shaped objects for the acceptance oracle. It does not write to the tables -- it feeds the production shredder. |
| `backend/shimline/report_pdf.py` | The shared strict Jinja renderer and in-memory WeasyPrint PDF boundary used by both sample and client reports. |
| `backend/test_reporting.py` | The CSV oracle, honest degradation, ingest, rendering, retention, and authored-total reconciliation tests. |
| `backend/test_report_download.py` | Authorization, cross-client/run association, unavailable-source, rendering-failure, audit, and retention coverage for the operator download. |
| `scripts/build_report.py` | Renders from either source: `--run-id`/`--db` for a connection, no flags for the synthetic CSV path. It writes a freshness/hash manifest for the public sample. |
| `POST /admin/engagements/{engagement}/bookkeeping/{run}/cash-leak-review.pdf` | Owner/reviewer-only, CSRF-protected on-demand client PDF delivery from a persisted run. |
| `backend/migrations/010_reporting_line_detail.sql` | `unit_price` and `quantity` on transaction lines. |

Run a report from a persisted run:

```bash
python scripts/build_report.py --run-id bkr_... --db /path/to/intake.db --as-of 2026-08-31
```

## What the ingest was missing, and now is not

Three things the canonical model discarded, each of which made a client-facing
number uncomputable:

1. **Unapplied cash.** `Payment.UnappliedAmt` was dropped. `open_balance` was
   written only when an object carried `Balance`, which a Payment does not, so
   unapplied payments were invisible to anything reading these tables. A
   payment's unapplied amount is now its open balance.
2. **Unit prices.** Lines kept the extended amount only, so there was no price
   history to compare against, and `ItemBasedExpenseLineDetail` was not in the
   detail lookup at all -- item-based bill lines lost their account, project
   and tax code as well. Both are fixed; `unit_price` and `quantity` are
   nullable and stay null when the provider states none.
3. **Estimates.** `Estimate` is now in `READ_OBJECTS` and is persisted with
   status `estimate`, deliberately outside the posted set so no balance or
   reconciliation can pick up a quote.

## The oracle

`data/*.csv` and `data/findings.json` are an existing known-good pair. The test
loads the CSVs through `persist_canonical` -- the same shredder a live pull
goes through -- projects the result, and asserts equality against
`scripts/analyze.py` run over the same inputs. All seventeen contract keys
match exactly.

Two places where the CSVs hold less than a QuickBooks file, and what the
fixture does:

* `invoices.csv` has no project column, so the fixture materialises one
  project-revenue invoice per row of `projects.csv`, settled in full so it
  cannot disturb receivables.
* `projects.csv` states `actual_cost` as an authored total. The fixture does
  not use that column; project cost is derived from the vendor transactions.

## Synthetic cost reconciliation

The reporting migration exposed a C$4,280 inconsistency in the old sample.
`generate_dataset.py` planted a duplicate pair of ProBuild charges (2 x
C$2,140) on project 251 after generating the project's normal costs, while the
authored `actual_cost` remained C$131,000. The real ledger therefore held
C$135,280 against an authored total of C$131,000.

The generator now treats C$135,280 as project 251's intended total and reduces
the normal generated costs by the planted pair before appending the pair. The
vendor ledger still totals exactly C$135,280 and the authored project total now
reconciles to it. The corrected sample changes only the four downstream values
that depend on that cost:

| | Old sample | Corrected sample |
|---|---|---|
| Full Reno #251 cost | C$131,000 (29.9%) | C$135,280 (27.7%) |
| Portfolio average margin | 30.6% | 30.5% |
| Worst-project margin gap | C$16,026 | C$15,955 |
| Total exposure | C$51,516 | C$51,445 |

Health scores and every unrelated finding remain unchanged. The reporting
oracle now requires the canonical projection and the corrected sample contract
to match exactly, and a dedicated test reconciles every authored project cost
to its vendor-transaction ledger.

## Honest degradation

A number that cannot be computed is `None`, and every `None` has a matching
entry in `unavailable`:

```json
{"field": "project_profitability", "status": "blocked",
 "reason": "Project profitability unavailable: no projects configured in QuickBooks.",
 "missing_source": "customer:job records"}
```

`status` reuses the work-engine vocabulary -- `blocked` (a source is missing,
and it is named) and `manual` (v0 does not compute it) -- so the PDF and the
reviewer console tell the same story.

What is covered:

* No jobs configured -- the common case in a real contractor file, not an edge
  case.
* Jobs configured but no invoice tagged to one, so no job has revenue. Every
  `synthetic_books` company is in this state and the projection reports it
  rather than printing 0% margins.
* No unapplied amount stated on any payment.
* No repeated unit-priced line, so no price history.
* Job cost posted by journal entry, which this version does not count toward a
  project. Declared as `manual` rather than silently dropped.

Two knock-on rules:

* **Total exposure becomes a floor, not a total,** when a component is
  missing, and it names which. The template prints it as `(partial)`.
* **Health scores are withheld entirely** when their inputs are unavailable,
  rather than computed from a partial base. A score is a single number a
  client will quote back; a partial one is worse than none.

The template was changed to match. `report.html.j2` previously dereferenced
`f.worst_project.project_name` and `f.vendor_price_variance.line_item`
unguarded -- with Jinja's default undefined those render as an empty string,
which is precisely the plausible-looking blank this work exists to prevent.
Every money figure now goes through a `dollars` filter that prints
"not available" rather than formatting `None`, optional sections are guarded,
and a "What this review could not determine" table renders the `unavailable`
list.

## Rules the projection applies, which a reviewer should confirm

These are judgement calls encoded in code. They are collected in
`reporting.ReportConfig` and here so they can be argued with.

1. **Aging runs from the invoice date, not the due date.** That is what the
   published report has always measured and what the health scores are
   calibrated against. QuickBooks' own A/R Aging report runs from the due
   date, so the two will not agree, and a client comparing them will notice.
2. **A unit-priced catalogue line is a stock purchase, not a job cost.** It is
   excluded from "materials not allocated to a job" -- otherwise every stock
   purchase is reported as a costing failure. It still counts in total vendor
   spend and it is the only source for price movement.
3. **A closed job is an inactive customer:job.** QuickBooks carries no
   "completed" state for one. A firm that never deactivates finished jobs will
   show everything as active.
4. **Project cost comes from bills and expenses only.** Journal entries can
   carry job cost in a real file; when they do, the run says so.
5. **`tax_amount` is still hard-coded `0.00`** on every persisted line. This
   was left alone deliberately: the report contract does not use net-of-tax
   revenue, and populating it correctly means apportioning `TxnTaxDetail`
   across lines. A wrong tax figure on a client report is worse than a visibly
   absent one. It must be populated before anything sales-tax-facing is built
   on these tables.

## Metered-call instrumentation

`QBOAdapter` now counts every HTTP attempt, attributed to the object being
queried, and `call_report()` returns the total and the per-object breakdown.
Intuit meters data-out -- reads and reports, not writes -- which is the entire
shape of this product, so the estimate of 1,500-3,000 CorePlus calls per client
per month in `INTUIT_ECOSYSTEM_RESEARCH.md` §2 is the number that decides how
many clients a tier holds. The counter is in place; it has not yet been run
against a live file, because that needs a sandbox connection with real volume.

## Delivery and remaining boundaries

* **Client PDFs are on demand, not stored.** The reviewer console streams the
  rendered bytes and audits a successful download. This avoids a second
  retained copy that could outlive its engagement. The tradeoff is that the
  report must be regenerated for another download and PDF rendering must be
  available at request time.
* **The renderer is a release dependency.** `weasyprint==69.0` is pinned and the
  Ubuntu Pango/HarfBuzz libraries are documented in `backend/DEPLOY.md`. The
  deploy preflight refuses to proceed if the live server lacks them.
* **The synthetic sample is reproducible.** It has been rendered and visually
  inspected in the release environment. Its PDF and maintained inputs are tied
  by the adjacent SHA-256 manifest; static publishing rejects a stale sample.
* **Check 10 (estimate vs. actual) is not automated.** Estimates are read and
  persisted, so it is no longer *blocked* -- its registry entry moved from
  `blocked` to `manual`. What it needs now is a materiality rule: how far a job
  may run over its quote before that is a finding. That is an accounting
  judgement.
* **Monthly-close reporting** for the Ledger/Jobs/Desk plans is a second
  projection on this same substrate, and is the next session.
