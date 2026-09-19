# QBO sandbox end-to-end acceptance plan

Status on 2026-09-10: not executed. This machine has no configured
`QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REDIRECT_URI`,
`QBO_ENVIRONMENT`, or `SHIMLINE_SECRET_KEY`, and no local `.env` or
`.local-secrets` file was found. No production credential was read and no
QuickBooks connection was attempted.

This plan is the release gate once an Intuit development app and a disposable
sandbox company are available.

## Hard safety boundary

- Use Intuit development credentials and a sandbox company created for this
  test. Never use a real customer company or production realm ID.
- Set `QBO_ENVIRONMENT=sandbox` explicitly and verify the operator UI displays
  Sandbox before authorizing.
- Use a fresh local SQLite database and a temporary upload directory.
- Do not point local callbacks at the production API. Register a dedicated
  HTTPS callback for the local/test host in the Intuit development app.
- Do not execute a correction until the read-only path has passed. If the
  sandbox-write exercise is run, use one disposable transaction, record its
  original state, and restore or delete it in the sandbox afterward.
- Stop immediately if any hostname is not an Intuit sandbox/developer host, the
  realm is not the named sandbox company, or the UI reports Production.

## Fixture to prepare in the sandbox company

Create or verify synthetic records with unique `SHIMLINE-E2E-<date>` markers:

1. one active customer and project;
2. one estimate for the project;
3. one paid project invoice and one overdue open invoice;
4. two identical vendor charges that form a duplicate candidate;
5. one material expense assigned to the project and one deliberately
   unassigned material expense;
6. one vendor item purchased twice at different unit prices;
7. one unapplied customer payment;
8. bank opening/closing balances and supporting evidence needed for the
   reconciliation check.

Record expected IDs, amounts, dates, balances, and expected findings in a test
worksheet before connecting. Do not use personally identifying data.

## Execution

1. Start the candidate API against the fresh database with payment disabled,
   sandbox QBO settings, and a generated test-only `SHIMLINE_SECRET_KEY`.
2. Create a local owner and reviewer. Confirm a viewer cannot perform operator
   actions.
3. Create one synthetic organization and engagement, then generate its signed
   client-portal link.
4. Enter through the link and choose **Connect QuickBooks**. Confirm the browser
   leaves Shimline for Intuit, shows only the development app and named sandbox
   company, then returns through the registered callback.
5. Confirm the stored connection is scoped to the synthetic organization,
   contains only encrypted token material, records the sandbox environment, and
   never logs a token or authorization code.
6. From the operator engagement, pull the supported QBO reports/objects. Record
   the API call count and per-object breakdown. Verify a successful sync run and
   immutable source-snapshot hashes.
7. Start the bookkeeping scan. Confirm canonical transactions, lines, accounts,
   entities, projects, estimates, unapplied amount, unit price, and quantity
   reconcile to the pre-recorded sandbox worksheet.
8. Compare the work-engine findings, evidence requests, reconciliation, and
   proposed corrections with the expected defect list. Every unsupported input
   must be marked `blocked` or `manual` with its missing source; it must not be
   printed as zero.
9. Run `reporting.portfolio` for the persisted bookkeeping run. Confirm every
   report figure traces to canonical rows and agrees with the reviewed work
   engine where the contracts overlap.
10. As owner/reviewer, download the Cash-Leak Review PDF from the engagement.
    Confirm correct company identity, as-of date, figures, unavailable-source
    table, synthetic-data marking where applicable, PDF header, and successful
    audit event. Confirm viewer, anonymous, cross-engagement, and cross-run
    attempts fail.
11. Optional sandbox-only correction exercise: review and approve one bounded
    proposal, execute it once, verify read-back and reconciliation, then retry
    the same operation to prove idempotency. Change the sandbox source between
    review and execution once to prove the stale-token guard requires re-review.
12. Disconnect from the client portal and confirm the connection is revoked and
    cannot pull again. Reconnect once to prove recovery from a revoked grant.
13. Close the engagement, advance the isolated test clock past 30 days, run the
    retention purge, and prove source snapshots, canonical bookkeeping runs, and
    uploaded documents are removed while the non-sensitive purge/audit evidence
    remains. Confirm no generated PDF file exists to purge because delivery is
    in memory.

## Required evidence

- sanitized test worksheet with sandbox IDs redacted to stable aliases;
- browser screenshots of sandbox label, authorization return, report review,
  and PDF at desktop and mobile widths;
- API call-count report and source-snapshot hashes;
- canonical reconciliation totals and expected-versus-actual finding list;
- PDF SHA-256 and a six-page visual review record;
- authorization, cross-tenant, CSRF, idempotency, stale-token, disconnect,
  reconnect, and retention results;
- proof that no production host, credential, company, payment, or customer data
  was used.

## Pass criteria

The gate passes only when the complete chain succeeds:

```text
sandbox authorization
-> QBO pull
-> immutable source snapshots
-> canonical ingest
-> work-engine findings and reconciliation
-> reporting projection
-> authenticated in-memory PDF
-> disconnect/reconnect
-> retention purge
```

Any unexplained amount difference, missing tenant association, plaintext token,
production hostname, write outside the explicitly approved sandbox exercise,
unlabeled unavailable input, or retained financial data after purge is a release
blocker.
