-- What a real QuickBooks file turned out to contain, recorded the first time
-- one is ever read.
--
-- A dozen facts about Intuit's API have been taken from documentation and never
-- checked against a live company: whether TaxAgency is queryable at all, whether
-- TaxLine carries an Amount, whether every Purchase names the account that paid
-- it. Each wrong assumption is a client whose ledger blocks, and right now the
-- only way to find out is for somebody to sit and watch a session pull a file.
--
-- This table exists so nobody has to watch. The probe runs unattended the first
-- time a connection appears and writes down what it found, so the ten minutes of
-- a person's time that OAuth genuinely requires produces a complete answer
-- rather than a starting point.
--
-- `payload` is encrypted: it holds row counts, the field names a real company
-- returns, and the reasons the derivation refused. None of that is a client's
-- money, but it is a client's file, and the shape of someone's books is theirs.

CREATE TABLE IF NOT EXISTS connection_probes (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL
        REFERENCES connections(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL
        REFERENCES organizations(id) ON DELETE CASCADE,
    -- 'ok' when the probe ran to completion, 'failed' when it could not. A
    -- failed probe is kept: "the pull died on TaxAgency" is the finding, and
    -- deleting it would leave the next run rediscovering it.
    status TEXT NOT NULL,
    -- Set when status='failed'. The entity or step that stopped it, named --
    -- every entity read posts to the same /query URL, so without this an
    -- operator sees "query failed (HTTP 400)" and cannot tell the chart of
    -- accounts from the tax rates.
    failed_at TEXT,
    -- How many documents the derivation could not post, over how many it saw.
    -- This is the per-document refusal rate measured on real data, which is the
    -- number the all-or-nothing blocking estimate has only ever guessed at.
    documents_seen INTEGER NOT NULL DEFAULT 0,
    documents_refused INTEGER NOT NULL DEFAULT 0,
    -- Whether a complete double-entry ledger came out the other end.
    ledger_complete INTEGER NOT NULL DEFAULT 0,
    -- How many of the named assumptions held, failed, or could not be judged
    -- because the file contained no document that would settle it. "Unknown" is
    -- deliberately not folded into either of the others: a file with no taxed
    -- sale tells us nothing about TaxLine, and recording that as a pass would
    -- manufacture evidence.
    assumptions_held INTEGER NOT NULL DEFAULT 0,
    assumptions_failed INTEGER NOT NULL DEFAULT 0,
    assumptions_unknown INTEGER NOT NULL DEFAULT 0,
    api_calls INTEGER NOT NULL DEFAULT 0,
    payload BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The probe is looked up per connection, newest first.
CREATE INDEX IF NOT EXISTS idx_connection_probes_connection
    ON connection_probes(connection_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_connection_probes_organization
    ON connection_probes(organization_id, created_at DESC);
