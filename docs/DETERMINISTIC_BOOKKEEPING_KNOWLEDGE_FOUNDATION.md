# Deterministic bookkeeping knowledge foundation

**Status:** research baseline, 2026-09-19
**Purpose:** make Shimline's future automation reproducible, inspectable, and
safe to combine with a typed-decision provider such as Jev.

This is deliberately not a scraped training corpus, a substitute for a CPA or
tax professional, or authorization to enable production writes. It is the
source-governed layer beneath the QBO connector work.

## The architecture to build

```text
source documents + QBO + bank data
    → immutable raw snapshots and provenance
    → normalized accounting facts
    → deterministic evidence/integrity controls
    → atomic typed decision(s), with confidence
    → client policy and approval routing
    → idempotent QBO write, when permitted
    → read-back, reconciliation, and audit trail
```

Jev is best placed at the fourth step: it scores or selects among a bounded set
of policy-eligible choices. It must not calculate money, invent a ledger
account, decide a tax position without facts, or directly invoke a write.
`data/bookkeeping_knowledge/deterministic_control_catalog.json` is the
non-negotiable layer; every control must pass before `allow_auto_post` is
possible.

## Research findings that change the engine

### 1. Auditability is a functional requirement

CRA guidance requires an audit trail from source documents to summarized
accounts, along with system documentation, readable records, backups, and a
chronological change record. The change record must be able to show who changed
what, when, and why. This maps directly to immutable snapshots, provenance,
policy versions, approval events, exact provider payloads, and read-back
results—not merely application logs. [CRA electronic record keeping](https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/ic05-1/electronic-record-keeping.html)

Source records normally need to be retained for at least six years, with
exceptions possible. Retention must be customer/jurisdiction/purpose-aware and
exportable; the current 90-day marketing-event retention policy is unrelated to
financial-record retention. [CRA business records](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/sole-proprietorships-partnerships/business-records.html)

### 2. Tax handling must fail closed on missing facts

An expense's account code is not proof that its GST/HST input tax credit is
supportable. CRA's documentary requirements depend on transaction value and
required supplier, tax, recipient, payment-term, and description information.
The engine must therefore model `expense_coding_status` separately from
`itc_evidence_status`. [CRA ITC documentation rules](https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/8-4/documentary-requirements-claiming-input-tax-credits.html)

GST/HST decisions depend on the nature and place of supply, effective date, and
facts such as whether work relates to real property. The engine may validate a
known configured rate, but must route unclear supply nature/location to review.
[CRA place-of-supply rules](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/gst-hst-businesses/charge-collect-place-supply.html)

### 3. Payroll is a separate, versioned calculation domain

CRA publishes formula and table updates, and PDOC is a verification tool for
common pay periods. A future payroll module needs effective-dated formula
versions, province-of-employment facts, employee elections, and differential
tests against CRA references/provider sandbox outputs. It must not use a model
decision for CPP/EI/tax arithmetic. Quebec remains out of the initial lane.
[CRA payroll formula guidance](https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4127-payroll-deductions-formulas/t4127-jan/t4127-jan-payroll-deductions-formulas-computer-programs.html)
[CRA PDOC](https://www.canada.ca/en/revenue-agency/services/e-services/digital-services-businesses/payroll-deductions-online-calculator.html)

### 4. Contractor reporting is a distinct evidence workflow

T5018 is relevant to construction subcontractor payments and has different
eligibility/reporting facts from ordinary AP categorization. Treat it as a
year-end preparation and exception-detection lane, not an automatic filing
feature. [CRA T5018 guidance](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/completing-filing-information-returns/t5018-slip-statement-contract-payments.html)

### 5. Provider use is a governed data flow

Financial documents contain personal information. Before sending any to Jev or
another processor, record a provider assessment, purpose limitation, retention
and deletion terms, subprocessor/security review, cross-border assessment,
customer disclosure, and permission/contract basis. PIPEDA accountability
continues to apply when a third party processes information. [OPC guidance on
third-party service providers](https://www.priv.gc.ca/en/privacy-topics/privacy-for-businesses/appropriate-handling-of-personal-information/gd_third-party_202609/)

## Machine-readable project assets

| Asset | Use |
|---|---|
| `data/bookkeeping_knowledge/source_registry.json` | Source provenance, allowed use, refresh cadence, and rule-citation starting points |
| `data/bookkeeping_knowledge/deterministic_control_catalog.json` | Provider-independent controls and named failure outcomes |
| `docs/QBO_SANDBOX_ACCEPTANCE_PLAN.md` | QBO integration evidence; owner/Claude implementation should keep it current |
| `docs/BOOKKEEPING_WORK_ENGINE_V0.md` | Existing engine behaviour, refusals, ledger reconstruction, and current release boundaries |

Do not copy public material into a model dataset simply because it is accessible.
Store citations and narrowly encoded rules. Before archiving a source snapshot,
confirm the site terms, copyright/licence, freshness, and whether the proposed
use is reference, test evidence, retrieval, or training.

## Typed-decision contract for a Jev adapter

The adapter should have one provider-neutral operation:

```json
{
  "decision_id": "uuid",
  "tenant_scope": {"organization_id": "...", "engagement_id": "..."},
  "policy_version": "client-policy:2026-09-19.1",
  "workflow": "expense_account_selection",
  "facts": {"currency": "CAD", "amount": "125.00", "vendor_alias": "..."},
  "allowed_options": ["account:tools", "account:vehicle_expense", "route:review"],
  "questions": [
    {"id": "eligible_account", "kind": "choice", "question": "Which allowed account best fits the supplied evidence?"},
    {"id": "evidence_sufficient", "kind": "noul", "question": "Does the supplied evidence identify the purchase sufficiently for automatic treatment?"}
  ],
  "minimize_data": true
}
```

Persist the response options, probabilities/confidence, model/provider version,
input-source hashes, and policy version. The policy engine—not the adapter—then
chooses auto-post, review, or owner confirmation. Questions should be atomic;
compose several independent answers in code rather than asking a model to make a
compound accounting judgment.

## Data acquisition plan

### Approved research inputs now

1. Official CRA and OPC source metadata and individually cited rules.
2. Owner-provided Intuit documentation, used for fixture design and later
   sandbox conformance checks.
3. Shimline's synthetic companies, canonical ledger, and golden tests.
4. Publicly licensed datasets only after licence and provenance review.

### Inputs that need an explicit gate

1. Customer QBO/bank/document data: connection agreement, privacy notice,
   retention policy, least-privilege access, and tenant isolation.
2. Customer corrections: per-client policy learning by default; any
   cross-customer use needs a specific contract and privacy basis.
3. JEV model calls: provider DPA/security/data-use approval and customer notice.
4. Partner-firm historical books: written data licence, de-identification
   standard, re-identification risk assessment, and accountant approval.

### Never acquire by default

- Credentials, data behind paywalls/logins, or data obtained by bypassing
  terms/technical controls.
- Public web pages copied wholesale into a training dataset without a licence.
- Financial records, invoices, or bank information scraped from third parties.
- CPA Canada Handbook material or other proprietary professional guidance
  without a licence.

## Build order after the QBO connector work lands

1. Make the two JSON catalogs loadable and validate their schemas in CI.
2. Add a policy store with effective dates, scoped overrides, review state, and
   immutable release snapshots.
3. Add deterministic controls around the existing normalized transaction and
   proposal models; emit named outcomes from the catalog.
4. Build the provider-neutral typed-decision adapter in shadow mode using
   synthetic/golden fixtures only.
5. Record reviewed decisions, calculate precision/coverage per workflow and
   client, and set thresholds from measured results.
6. Enable review-only decisions for a small approved pilot.
7. Enable auto-post one workflow at a time, with idempotency, read-back, and a
   compensating-entry path—not silent mutation or deletion.

## Open research gaps

- Per-province PST/QST rules and Quebec, which are outside the initial scope.
- Tax positions requiring professional judgment: holdbacks, mixed use, quick
  method, bad-debt relief, self-assessment, and complex real-property supplies.
- Exact commercial and data-processing terms for Jev once access is granted.
- Licensed/de-identified partner data and the required data-governance process.
- Measured calibration from real reviewed work; no confidence threshold should
  be claimed before this exists.

For the competitor landscape, database target architecture, measurable moat,
and multi-session execution sequence, see
`docs/ENGINE_100X_IMPROVEMENT_PROGRAM.md`.
