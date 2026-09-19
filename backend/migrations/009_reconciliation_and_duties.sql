-- Two corrections to the Bookkeeping Work Engine's control tables.
--
-- 1. bookkeeping_reconciliations was declared in 008 but never written to, and
--    its status vocabulary ('matched') did not match the engine's ('reconciled').
--    The engine now persists reconciliation on every run, so the constraint has
--    to accept what the engine actually produces, including 'no_source' — an
--    account we could not reconcile because no statement was supplied. That is
--    a result worth recording; silence is not.
--
--    Rebuilt rather than altered because SQLite cannot alter a CHECK. Safe: the
--    table has never held a row.
--
-- 2. Executions now record who released the write and whether it was approved
--    by a different person, so separation of duties is evidence in the working
--    papers rather than a claim in a document.

DROP TABLE IF EXISTS bookkeeping_reconciliations;

CREATE TABLE bookkeeping_reconciliations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES bookkeeping_runs(id) ON DELETE CASCADE,
    account_ref TEXT NOT NULL,
    period_end TEXT NOT NULL,
    source_balance TEXT NOT NULL,
    ledger_balance TEXT NOT NULL,
    difference TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reconciled', 'exception', 'no_source')),
    missing_source TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, account_ref, period_end)
);

CREATE INDEX IF NOT EXISTS idx_reconciliations_run ON bookkeeping_reconciliations(run_id);

-- Separation of duties on the execution record.
--   executed_by            the operator who released the write
--   approved_by            the operator whose approval authorised it
--   duties_separated       0 when both are the same person
--   duties_exception_note  why that was permitted, when it was
-- The run keeps the evidence it was given and the per-check coverage report, so
-- a later reconciliation refresh uses the same evidence the analysis used, and
-- the console can show which checks ran without re-running the analysis.
ALTER TABLE bookkeeping_runs ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE bookkeeping_runs ADD COLUMN coverage_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE bookkeeping_executions ADD COLUMN executed_by TEXT;
ALTER TABLE bookkeeping_executions ADD COLUMN approved_by TEXT;
ALTER TABLE bookkeeping_executions ADD COLUMN duties_separated INTEGER NOT NULL DEFAULT 1;
ALTER TABLE bookkeeping_executions ADD COLUMN duties_exception_note TEXT;
