# Shimline — evidence-native bookkeeping engine

> **Archived research prototype.** An AI-native full-stack exploration of how
> bookkeeping automation can remain deterministic, evidence-backed, and
> reviewable instead of becoming a black-box ledger writer.

Shimline began as a Canadian bookkeeping workflow prototype. This public
repository preserves the reusable engineering work: normalized QuickBooks
Online ingestion, double-entry reconstruction, deterministic defect detection,
approval-gated proposals, reconciliation, working-paper generation, and a
source-governed knowledge foundation for future automation.

It is deliberately **not** a live product, tax engine, payroll product,
financial adviser, or a substitute for a qualified bookkeeper/accountant.

## Why this project exists

The interesting problem is not merely classifying a receipt. It is producing a
system that can answer:

- What source evidence supports this conclusion?
- Which deterministic control permitted it?
- What is missing, ambiguous, or outside policy?
- Who approved a change, against which exact payload?
- Did the accounting provider accept it, and does the ledger still reconcile?

That leads to the central loop:

```text
QBO + statements + documents
        ↓
immutable snapshots → normalized accounting facts → deterministic controls
        ↓
bounded typed decision (optional / shadow mode)
        ↓
proposal → human review → idempotent provider write → read-back
        ↓
reconciliation + hashed working papers
```

## What is included

- A Python/FastAPI prototype and SQLite schema for a bookkeeping work engine.
- QBO-shaped adapters and fixtures with pagination, pull manifests, sparse
  mutation controls, idempotency keys, and read-after-write checks.
- Double-entry reconstruction, matching, tax/GST preparation controls,
  statement matching, reconciliation, and explicit refusal paths.
- Synthetic companies with seeded accounting defects and golden-ledger checks.
- A machine-readable deterministic control catalog and source registry.
- Research notes covering the system design, safety boundaries, QBO sandbox
  acceptance, and a long-term improvement programme.

## Safety model

The engine is designed to fail closed. Missing evidence is a named outcome,
not a confidence score that quietly becomes a posting. Every automated action
needs a policy-eligible proposal, a review state, idempotency, provider
read-back, and reconciliation. Production writes and filing are intentionally
out of scope for this archived prototype.

## Quick start

Requires Python 3.12+.

```powershell
git clone https://github.com/wig1-max/shimline-bookkeeping-engine.git
cd shimline-bookkeeping-engine
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-dev.txt
$env:PYTHONPATH = "$PWD\src"
python -m pytest -q tests/test_qbo_adapter.py tests/test_postings.py tests/test_matching.py
```

The repository retains the original prototype layout so its test suite and
research artifacts remain inspectable. It has no default production
configuration and must not be pointed at customer data.

## Repository map

| Path | Purpose |
| --- | --- |
| `src/shimline/` | Engine, adapters, controls, web prototype, and generated artifacts |
| `migrations/` | SQLite schema evolution for the prototype |
| `tests/` | Unit, contract, properties, and synthetic acceptance tests |
| `data/bookkeeping_knowledge/` | Source provenance and deterministic-control catalogs |
| `docs/` | Architecture, research, safety boundaries, and integration acceptance plan |

## Research highlights

The most reusable design decisions are documented in:

- [Bookkeeping work engine v0](docs/BOOKKEEPING_WORK_ENGINE_V0.md)
- [Deterministic bookkeeping knowledge foundation](docs/DETERMINISTIC_BOOKKEEPING_KNOWLEDGE_FOUNDATION.md)
- [100× engine improvement program](docs/ENGINE_100X_IMPROVEMENT_PROGRAM.md)
- [QBO sandbox acceptance plan](docs/QBO_SANDBOX_ACCEPTANCE_PLAN.md)

## What was intentionally excluded

No production infrastructure, deployment procedures, secrets, recovery keys,
customer data, lead/CRM exports, analytics events, payment credentials, or
current live-service configuration is published here. Historical prototype
branding remains in a few UI fixtures; it does not identify an active service.
The public repository is a curated technical artifact, not a copy of a former
production environment.

## License

Released under the [Apache License 2.0](LICENSE). You may use, modify, and
distribute this work, including commercially, subject to the license terms.
