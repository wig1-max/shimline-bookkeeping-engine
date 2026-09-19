-- Bank statements: the source checks 04 and E2 have always required and never had.
--
-- work_engine.CHECKS declares check 04 (unreconciled accounts) and E2
-- (transactions on the statement, missing from the ledger) as `automated`, and
-- both carry `"requires": "bank_statement"`. The detectors, the reconciliation,
-- and the `no_source` path that refuses to call an unreconciled account clean
-- were all written. Nothing could put a statement into the system, so
-- reconcile() returned `no_source` for every run and two automated checks never
-- fired. These tables are the missing input.
--
-- Two deliberate constraints:
--
-- 1. A statement is worthless for reconciliation until its bank account is
--    mapped to the QBO account whose balance it is supposed to prove.
--    `qbo_account_id` is therefore nullable, and an unmapped statement stays
--    invisible to reconcile() rather than reconciling against the wrong
--    account. An unmapped statement is a `no_source` result, which is the
--    honest answer -- the same rule the engine already applies to a missing one.
--
-- 2. Money is TEXT, matching every other amount in the schema, because these
--    values are Decimal and SQLite REAL would silently round them.

CREATE TABLE IF NOT EXISTS bank_statements (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    engagement_id TEXT REFERENCES engagements(id) ON DELETE SET NULL,

    -- As the bank states them. Kept verbatim so a reviewer can tie the record
    -- back to the institution's own identifiers.
    bank_account_id TEXT NOT NULL,
    routing_number TEXT,
    account_type TEXT,
    currency TEXT NOT NULL DEFAULT 'CAD',

    -- The QBO account this statement proves. NULL until an operator maps it;
    -- see note 1 above.
    qbo_account_id TEXT,

    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    closing_balance TEXT NOT NULL,

    -- Evidence lineage. content_sha256 is over the exact uploaded bytes, so a
    -- re-upload of the same file is detectable and a modified one is not
    -- mistaken for it.
    source_filename TEXT,
    source_format TEXT NOT NULL CHECK (source_format IN ('ofx', 'qfx', 'csv')),
    content_sha256 TEXT NOT NULL,
    line_count INTEGER NOT NULL DEFAULT 0,

    imported_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- The same file must not import twice for one client. Re-importing is a
    -- no-op, not a second statement and not a doubled set of lines.
    UNIQUE (organization_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_bank_statements_org_period
    ON bank_statements (organization_id, period_end);

CREATE TABLE IF NOT EXISTS bank_statement_lines (
    id TEXT PRIMARY KEY,
    statement_id TEXT NOT NULL REFERENCES bank_statements(id) ON DELETE CASCADE,

    posted_date TEXT NOT NULL,
    amount TEXT NOT NULL,          -- signed: negative is money leaving the account
    description TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    txn_type TEXT NOT NULL DEFAULT '',

    -- The bank's own transaction identifier (OFX FITID). Banks guarantee it is
    -- stable and unique within an account, which makes it the natural
    -- idempotency key for a re-imported overlapping statement period.
    fitid TEXT,

    -- Order within the file, so a statement can be redisplayed exactly as the
    -- bank presented it rather than in whatever order SQLite returns.
    ordinal INTEGER NOT NULL DEFAULT 0,

    UNIQUE (statement_id, fitid)
);

CREATE INDEX IF NOT EXISTS idx_bank_statement_lines_statement
    ON bank_statement_lines (statement_id, ordinal);
