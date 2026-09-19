-- Bookkeeping Work Engine v0.
-- Provider-neutral records are deliberately separate from QBO snapshots: the
-- snapshots preserve what a provider said, while these tables preserve what
-- Shimline understood, decided, changed, and proved.

CREATE TABLE IF NOT EXISTS bookkeeping_runs (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    engagement_id TEXT REFERENCES engagements(id) ON DELETE SET NULL,
    connection_id TEXT REFERENCES connections(id) ON DELETE SET NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ingesting'
        CHECK (status IN ('ingesting','review','approved','executing','verifying','reconciled','blocked','failed')),
    source_cursor TEXT,
    detail TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bookkeeping_runs_org ON bookkeeping_runs(organization_id, started_at DESC);

CREATE TABLE IF NOT EXISTS bookkeeping_accounts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL,
    account_subtype TEXT,
    currency TEXT NOT NULL DEFAULT 'CAD',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    sync_token TEXT,
    UNIQUE (organization_id, provider, provider_id)
);

CREATE TABLE IF NOT EXISTS bookkeeping_entities (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('customer','vendor')),
    display_name TEXT NOT NULL,
    email TEXT,
    tax_identifier_last4 TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    sync_token TEXT,
    UNIQUE (organization_id, provider, entity_type, provider_id)
);

CREATE TABLE IF NOT EXISTS bookkeeping_projects (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    customer_id TEXT REFERENCES bookkeeping_entities(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    sync_token TEXT,
    UNIQUE (organization_id, provider, provider_id)
);

CREATE TABLE IF NOT EXISTS bookkeeping_classifications (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('class','tax_code','location')),
    name TEXT NOT NULL,
    code TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    sync_token TEXT,
    UNIQUE (organization_id, provider, kind, provider_id)
);

CREATE TABLE IF NOT EXISTS bookkeeping_transactions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES bookkeeping_runs(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    document_number TEXT,
    currency TEXT NOT NULL DEFAULT 'CAD',
    total_amount TEXT NOT NULL,
    open_balance TEXT,
    entity_id TEXT REFERENCES bookkeeping_entities(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'posted',
    sync_token TEXT,
    source_hash TEXT NOT NULL,
    UNIQUE (run_id, provider, provider_type, provider_id)
);
CREATE INDEX IF NOT EXISTS idx_bookkeeping_txn_period ON bookkeeping_transactions(organization_id, transaction_date);

CREATE TABLE IF NOT EXISTS bookkeeping_transaction_lines (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES bookkeeping_transactions(id) ON DELETE CASCADE,
    provider_line_id TEXT,
    description TEXT,
    account_id TEXT REFERENCES bookkeeping_accounts(id) ON DELETE SET NULL,
    entity_id TEXT REFERENCES bookkeeping_entities(id) ON DELETE SET NULL,
    project_id TEXT REFERENCES bookkeeping_projects(id) ON DELETE SET NULL,
    class_id TEXT REFERENCES bookkeeping_classifications(id) ON DELETE SET NULL,
    tax_code_id TEXT REFERENCES bookkeeping_classifications(id) ON DELETE SET NULL,
    amount TEXT NOT NULL,
    debit TEXT NOT NULL DEFAULT '0',
    credit TEXT NOT NULL DEFAULT '0',
    tax_amount TEXT NOT NULL DEFAULT '0',
    UNIQUE (transaction_id, provider_line_id)
);

CREATE TABLE IF NOT EXISTS bookkeeping_documents (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES bookkeeping_runs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT,
    transaction_id TEXT REFERENCES bookkeeping_transactions(id) ON DELETE SET NULL,
    document_type TEXT NOT NULL,
    filename TEXT,
    content_hash TEXT,
    period_start TEXT,
    period_end TEXT,
    evidence_status TEXT NOT NULL DEFAULT 'present'
        CHECK (evidence_status IN ('present','missing','requested','insufficient')),
    UNIQUE (organization_id, provider, provider_id)
);

CREATE TABLE IF NOT EXISTS bookkeeping_allocations (
    id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL REFERENCES bookkeeping_transaction_lines(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES bookkeeping_projects(id) ON DELETE SET NULL,
    class_id TEXT REFERENCES bookkeeping_classifications(id) ON DELETE SET NULL,
    amount TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('provider','document','proposal','operator')),
    confidence TEXT NOT NULL DEFAULT '1.0'
);

CREATE TABLE IF NOT EXISTS bookkeeping_findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES bookkeeping_runs(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    defect_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info','low','medium','high','critical')),
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    affected_type TEXT,
    affected_provider_id TEXT,
    evidence_status TEXT NOT NULL CHECK (evidence_status IN ('sufficient','missing','uncertain')),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    financial_effect TEXT NOT NULL DEFAULT '0',
    tax_effect TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','proposed','resolved','dismissed','escalated')),
    UNIQUE (run_id, defect_type, affected_provider_id)
);

CREATE TABLE IF NOT EXISTS bookkeeping_evidence_requests (
    id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL REFERENCES bookkeeping_findings(id) ON DELETE CASCADE,
    requested_item TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','received','waived')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS bookkeeping_proposals (
    id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL UNIQUE REFERENCES bookkeeping_findings(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES bookkeeping_runs(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_provider_id TEXT,
    current_json TEXT NOT NULL,
    proposed_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    financial_effect TEXT NOT NULL DEFAULT '0',
    tax_effect TEXT NOT NULL DEFAULT '0',
    expected_sync_token TEXT,
    status TEXT NOT NULL DEFAULT 'detected'
        CHECK (status IN ('detected','proposed','reviewed','approved','executed','verified','reconciled','rejected','escalated')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bookkeeping_proposals_run ON bookkeeping_proposals(run_id, status);

CREATE TABLE IF NOT EXISTS bookkeeping_approvals (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES bookkeeping_proposals(id) ON DELETE CASCADE,
    proposal_version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve','edit','reject','escalate')),
    actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    note TEXT,
    decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (proposal_id, proposal_version, decision)
);

CREATE TABLE IF NOT EXISTS bookkeeping_executions (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES bookkeeping_proposals(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    provider_type TEXT,
    provider_id TEXT,
    provider_sync_token TEXT,
    status TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started','applied','verified','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS bookkeeping_reconciliations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES bookkeeping_runs(id) ON DELETE CASCADE,
    account_ref TEXT NOT NULL,
    period_end TEXT NOT NULL,
    source_balance TEXT NOT NULL,
    ledger_balance TEXT NOT NULL,
    difference TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('matched','exception')),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, account_ref, period_end)
);

CREATE TABLE IF NOT EXISTS bookkeeping_working_papers (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES bookkeeping_runs(id) ON DELETE CASCADE,
    package_json TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Counted independently from report snapshots in the retention audit.
ALTER TABLE purge_log ADD COLUMN bookkeeping_runs_removed INTEGER NOT NULL DEFAULT 0;
