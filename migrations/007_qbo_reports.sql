-- QuickBooks report pulls.
--
-- Each pull is a sync run; each report it fetched is a source snapshot. Keeping
-- the provider's raw payload versioned in its own table — rather than shredding
-- it into CRM columns — means a later change to the diagnostic can be re-run
-- against exactly what QuickBooks said at the time.
--
-- Payloads are encrypted at rest. They are a client's financial records, this
-- is a new store, and the key machinery already exists, so there is no reason
-- to write them in the clear.
--
-- RETENTION: snapshots are covered by the published deletion promise on the
-- same terms as uploaded documents — 30 days after the engagement closes, 90
-- days if none ever does. `purge_expired` enforces it. A pull that outlived
-- the promise would be a quiet breach of the privacy policy.

CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    connection_id TEXT REFERENCES connections(id) ON DELETE SET NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    engagement_id TEXT REFERENCES engagements(id) ON DELETE SET NULL,
    provider TEXT NOT NULL DEFAULT 'quickbooks',
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    period_start TEXT,
    period_end TEXT,
    reports_requested INTEGER NOT NULL DEFAULT 0,
    reports_stored INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    requested_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    requested_by_client_id TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_org ON sync_runs(organization_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_runs_engagement ON sync_runs(engagement_id);

CREATE TABLE IF NOT EXISTS source_snapshots (
    id TEXT PRIMARY KEY,
    sync_run_id TEXT NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    engagement_id TEXT REFERENCES engagements(id) ON DELETE SET NULL,
    provider TEXT NOT NULL DEFAULT 'quickbooks',
    report_name TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    byte_size INTEGER NOT NULL DEFAULT 0,
    payload_enc TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (sync_run_id, report_name)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_org ON source_snapshots(organization_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_engagement ON source_snapshots(engagement_id);

-- Counted alongside deleted files so the purge log stays a complete record of
-- what the retention promise actually removed.
ALTER TABLE purge_log ADD COLUMN snapshots_removed INTEGER NOT NULL DEFAULT 0;
