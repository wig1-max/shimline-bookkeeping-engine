# Shimline engine 100× improvement program

**Status:** strategic research and multi-session build plan, 2026-09-19
**Decision:** build a verifiable bookkeeping engine, not a generic transaction
categorizer or another AI chat layer.

“100× better” is a direction, not a claim we can make today. It becomes credible
only if Shimline can prove a qualitative step-change in: evidence coverage,
correctness on a declared scenario set, time-to-close, exception precision,
recovery from failure, and auditability. Every external competitor statement in
this document is vendor-provided product information, not an independent
accuracy benchmark.

## 1. What the market already does

| Company / approach | Current method | What to learn | Why it is not the Shimline moat |
|---|---|---|---|
| Intuit / QuickBooks | AI categorization, transaction information requests, reconciliation assistance, anomaly detection, firm policy templates, and an accountant-suite workflow | Work must be continuous, policy-aware, reviewable, and native to the accountant's daily queue | Intuit owns the ledger and distribution. Shimline should complement QBO with evidence, cross-source assurance, Canadian contractor depth, and portability—not duplicate its transaction feed. |
| Digits | AI-native/agentic general ledger; continuous categorization, matching, reconciliation, document routing, confidence-based inbox | Keep books current continuously; route uncertainty rather than guess | Its differentiated asset is the ledger it owns. Shimline can retain QBO as the ledger of record and build a portable proof layer across systems. |
| Ramp | Spend captured at source, policy checks, historical coding, approvals, real-time ERP sync, accruals and close workflows | Source-time capture and policy checks prevent month-end cleanup | It is strongest where it originates spend. Shimline must work when the source is bank/QBO/document evidence and no single spend rail controls the workflow. |
| Dext | Document collection/OCR, supplier rules, categorization suggestions, missing-paperwork requests | Evidence collection must be a first-class workflow, not an attachment afterthought | Extraction is not accounting proof. Shimline needs field/page provenance plus a source-to-ledger proof chain. |
| Vic.ai | AP invoice processing and bill-pay automation | Deep vertical workflows can achieve high automation in a bounded domain | AP is only one lane; Shimline must coordinate the full close and independently reconcile the ledger. |

Evidence: [Intuit Accounting AI](https://quickbooks.intuit.com/learn-support/en-us/help-article/bank-transactions/accounting-agent-features/L6pl9rv94_US_en_US), [Intuit Accountant Suite](https://quickbooks.intuit.com/ca/accountants/intuit-accountant-suite/), [Digits AI](https://help.digits.com/business-agentic-general-ledger/digits-ai), [Ramp accounting automation](https://ramp.com/accounting-automation-software), [Dext AI Assist](https://help.dext.com/en/articles/500051-what-is-dext-ai-assist), and [Vic.ai](https://www.vic.ai/).

## 2. The breakthrough thesis

The winning engine is an **evidence graph + policy compiler + continuous
assurance system**.

It will answer, for every material ledger state: *what happened, what evidence
proves it, what rules were applied, what alternatives were considered, who or
what authorized it, what QBO did, and whether the final books still reconcile.*

That yields five durable capabilities that most point solutions do not combine:

1. **Proof objects, not just postings.** Every decision creates a replayable
   package: raw-source hashes, normalized facts, rule/policy versions, allowed
   options, confidence, approvals, provider request/response, read-back, and
   reconciliation result.
2. **A client policy compiler.** Human corrections are turned into scoped,
   effective-dated policy candidates and tests. They never silently retrain a
   global system or cross a tenant boundary.
3. **Redundant assurance.** The engine independently compares document-derived
   postings, QBO documents/reports, bank statements, and tax/account-control
   totals. Agreement increases evidence; disagreement produces a named case.
4. **Uncertainty as a workflow primitive.** Every result is `proved`,
   `allowed`, `review`, `missing_evidence`, `unsupported`, or `integrity_fail`.
   “Probably correct” never becomes a silent posting.
5. **A scenario factory and benchmark.** The product improves against a
   declared, versioned corpus of edge cases—not anecdotes or model demos.

This aligns with CRA's expectation of a traceable source-to-summary audit trail
and retained system documentation. [CRA electronic record keeping](https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/ic05-1/electronic-record-keeping.html)

## 3. Database and knowledge structure to build now

Do not begin with a giant “training database.” Begin with an event-sourced,
tenant-isolated proof graph that supports deterministic replay.

### 3.1 Core facts: immutable and provider-neutral

| Aggregate | Required identity / grain | Must retain |
|---|---|---|
| `source_artifact` | one received document, statement, API payload, email, or import version | content hash, original format/location, received time, source system, retention class |
| `source_record` | one provider object or extracted document fact | artifact reference, provider/entity IDs, source timestamps, raw/normalized field mapping |
| `financial_document` + `financial_line` | one economic document and its lines | parties, amount/currency, tax facts, dates, dimensions, evidence links |
| `ledger_candidate` | one normalized proposed double-entry representation | Decimal postings, derivation version, data-quality status, source records |
| `reconciliation_link` | one asserted relationship between two facts | relation type, deterministic features, competing candidates, status |
| `period_state` | one organization/account/period | opening/closing source coverage, lock state, reconciliation state, completeness result |

### 3.2 Policy, decision, and control plane

| Aggregate | Purpose |
|---|---|
| `policy_rule` | Effective-dated client or firm policy with scope, citations, owner, review state, and allowed action class |
| `policy_release` | Immutable set of policy rules used on a decision; enables replay after a rule changes |
| `control_run` | One result per deterministic control ID from `deterministic_control_catalog.json` |
| `decision_request` / `decision_response` | Minimized typed Jev/provider question, allowed choices, probabilities/confidence, and provider/model version |
| `proposal` | Exact action presented to a reviewer, including alternatives and proof links |
| `approval` | Role, actor, timestamp, version of payload approved, expiry, and separation-of-duties checks |
| `execution` | Idempotency key, QBO request/response identifiers, retry class, read-back comparison, compensating action if needed |
| `case` | Exception/work item with a precise question for bookkeeper or owner, evidence, SLA, resolution, and resulting policy candidate |

### 3.3 Evaluation and learning plane

| Aggregate | Purpose |
|---|---|
| `scenario` | Versioned synthetic or licensed/de-identified case with expected facts, controls, outcome, and source authority |
| `golden_assertion` | One expected invariant, calculation, or decision against a named scenario |
| `evaluation_run` | Code/policy/model versions, corpus slice, pass/fail, latency, cost, and replay hash |
| `reviewed_decision` | Human judgement linked to evidence and rationale—not merely a final label |
| `calibration_slice` | Workflow/client/policy cohort used to measure precision and coverage before changing an automation threshold |
| `rule_candidate` | A correction-derived proposed rule, tests, impact preview, reviewer sign-off, and release decision |

The initial schemas should be normalized enough to preserve provenance, but
store raw provider payloads immutably alongside normalized facts. Never make a
model output the source of truth.

## 4. Non-negotiable engine principles

1. **Facts, policies, decisions, and executions are separate records.** A later
   correction must not rewrite the original fact or hide the original decision.
2. **Every mutation is replayable.** Re-run the same facts and policy release to
   reproduce the proposal; re-read QBO to verify the execution.
3. **Use deterministic code for arithmetic and eligibility.** Jev scores bounded
   choices; it does not calculate payroll, invent amounts, or decide legal tax
   positions.
4. **Data minimization is built into model calls.** Use aliases and extracted
   facts where possible; do not send raw documents by default.
5. **The system fails locally where possible.** Quarantine an incomplete
   transaction or failed check rather than blocking an entire close, while
   reporting exactly what remains unproved.
6. **Policies are code-reviewed product assets.** They have tests, citations,
   an owner, expiry/effective dates, change impact, and rollback.
7. **Customer learning is local first.** Cross-customer templates require a
   deliberate anonymized pattern, legal basis, and release review.

## 5. Multi-session program

Each session produces a checked-in artifact and has an exit gate. Work may run
in parallel where interfaces are stable; QBO connector changes remain with the
assigned QuickBooks implementation owner.

### Foundation: sessions 1–3

| Session | Deliverable | Exit gate |
|---|---|---|
| 1. Engine inventory | Map existing schemas, provenance, postings, matching, work engine, and QBO boundaries to the target aggregates above | No duplicate source of truth; every current table/module has an owner and migration path |
| 2. Canonical ontology | Versioned accounting vocabulary: entities, document types, relation types, dimensions, statuses, evidence types, and action classes | JSON schema and example fixtures validate in CI |
| 3. Proof graph migration plan | Database migrations for artifact/source record/policy/control/proposal/execution/case; retention classes and tenant foreign keys | Migration rehearses against synthetic fixture and reverses safely |

### Assurance: sessions 4–6

| Session | Deliverable | Exit gate |
|---|---|---|
| 4. Scenario factory v1 | 100 deterministic Canadian contractor cases: normal flows, duplicates, splits, refunds, partial payments, tax uncertainty, missing evidence, closed periods, and stale provider versions | Every case has expected outcome and at least one invariant; no expected result is model-generated |
| 5. Control compiler | Execute the named controls in `deterministic_control_catalog.json`, returning standardized outcomes and proof objects | Mutation tests demonstrate each critical control can fail and block the intended action |
| 6. Continuous reconciliation | Independent document-to-ledger, bank-to-ledger, and QBO report/control-account comparisons with local quarantine | Deliberate omissions/duplicates/rounding defects are surfaced with an actionable case |

### Intelligence: sessions 7–9

| Session | Deliverable | Exit gate |
|---|---|---|
| 7. Policy compiler | Rule authoring, effective dates, scope precedence, conflict detection, impact preview, review, release, rollback | A rule change replays historical scenarios and cannot affect another tenant |
| 8. Jev adapter in shadow mode | Provider-neutral typed-decision adapter, minimized states, allowed-option builder, response recorder, and offline evaluator | Synthetic corpus shows measured precision/coverage by workflow; no write path calls the adapter |
| 9. Learning loop | Review resolution → labelled case → policy candidate → regression tests → controlled release | A correction demonstrably improves the intended cohort without regressing the protected corpus |

### Operational automation: sessions 10–12

| Session | Deliverable | Exit gate |
|---|---|---|
| 10. Exception operating system | Evidence requests, owner/bookkeeper question templates, assignment, SLA, resolution taxonomy, and root-cause reporting | Every `review` or `block` reaches a human-readable next action; no dead-end queue |
| 11. Writeback simulator | QBO-like test rail for idempotency, concurrency, partial failure, read-back mismatch, and compensating-entry workflows | 100% pass on fault-injection matrix before a sandbox write is enabled |
| 12. Automation ladder | Per-workflow Observe → Review → Limited autopilot release flags with measured threshold and rollback | No workflow advances without an approved calibration slice, error budget, and owner sign-off |

### Private alpha: sessions 13–15

| Session | Deliverable | Exit gate |
|---|---|---|
| 13. QBO sandbox certification | Integrate the assigned connector with the proof graph and run existing sandbox acceptance plan plus new fault tests | Sandbox evidence package is complete and reviewed |
| 14. Accountant design review | Five to ten accountants/bookkeepers evaluate cases, proof objects, escalations, and policy authoring | Top failure modes become scenarios and product changes; no unsupported claims remain |
| 15. Narrow pilot readiness | One construction/trades archetype, CAD, configured tax posture, defined transaction bounds, review-only first | Privacy, retention, support, incident, and approval controls pass a launch review |

## 6. Measurement: how the program earns “better”

Do not use raw auto-categorization rate as the north star—it can be gamed by
guessing. Track the following per workflow, client archetype, and policy/model
version:

| Metric | Definition | Release implication |
|---|---|---|
| Proof coverage | Share of material transactions with complete source-to-ledger evidence and passed controls | Must be 100% for closed/represented work; missing proof is a case, not a success |
| Decision precision | Correct auto-eligible decisions ÷ all auto-eligible decisions, based on held-out human-reviewed truth | Controls automation threshold |
| Review capture | Share of incorrect/ambiguous cases routed to review before execution | Protects against silent errors |
| Exception precision | Confirmed exceptions ÷ raised exceptions | Prevents operators drowning in noise |
| Reconciliation completeness | Accounts/periods independently reconciled ÷ required accounts/periods | Gates close completion |
| Replay determinism | Identical facts + policy release producing identical proposal/control outcomes | Gates policy/compiler releases |
| Time to evidence-complete close | Time from period end to all required evidence/reconciliation state | Measures customer value without rewarding unsafe speed |
| Correction regression rate | Previously correct scenario results broken by a rule/model change | Blocks release when above error budget |
| Autonomous value rate | Correctly auto-completed low-risk work ÷ eligible low-risk work | Indicates automation only after quality gates pass |

Initial targets must be set from synthetic/golden performance and then replaced
with pilot evidence. Do not set a public “99%” or “100×” claim before a reviewed
cohort supports it.

## 7. Immediate work while access is pending

1. Complete sessions 1–4: inventory, ontology, proof-graph migration design,
   and the first scenario corpus.
2. Convert the current 28 deterministic controls into executable tests, starting
   with source provenance, ledger balance, duplicate prevention, closed period,
   idempotency, and read-back comparison.
3. Build a public-source watcher that records metadata and human-reviewed diffs
   for CRA sources—never an uncontrolled scraper or training pipeline.
4. Create accountant-review templates for the five hardest recurring questions:
   tax treatment, mixed-use allocation, missing receipt, duplicate/partial
   payment, and owner/personal transaction.
5. Establish a benchmark harness now. JEV and any later model compete against
   the same frozen cases, controls, cost, latency, and calibration measures.

## 8. What not to do

- Do not attempt to replace QuickBooks' ledger, bank, payroll, tax filing, or
  payments rails before the proof layer has repeatable evidence.
- Do not merge raw customer data into a global “training set.”
- Do not automate a broad class because a few examples look good.
- Do not treat a model's confidence as a legal, accounting, or tax conclusion.
- Do not duplicate competitors' generic OCR, transaction feed, chat, or
  dashboard features before the proof graph and scenario factory exist.
