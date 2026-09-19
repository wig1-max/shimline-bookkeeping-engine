-- QuickBooks Online connections.
--
-- Tokens are stored encrypted (Fernet, key from QBO_TOKEN_KEY in the server
-- environment) because a refresh token is a long-lived credential to a
-- client's financial records. The columns are named _enc so nobody is ever
-- misled into thinking the value is readable.

CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'quickbooks',
    environment TEXT NOT NULL DEFAULT 'sandbox' CHECK (environment IN ('sandbox', 'production')),
    realm_id TEXT NOT NULL,
    access_token_enc TEXT,
    refresh_token_enc TEXT,
    access_expires_at TEXT,
    refresh_expires_at TEXT,
    scope TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'error')),
    status_detail TEXT,
    connected_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    last_refreshed_at TEXT,
    last_sync_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (provider, realm_id, environment)
);

CREATE INDEX IF NOT EXISTS idx_connections_org ON connections(organization_id, status);

-- Short-lived CSRF state for the OAuth round trip. Only the hash is stored,
-- matching how sessions and upload tokens are handled elsewhere. A row here is
-- also the proof that the redirect back from Intuit began with a signed-in
-- operator, since /qbo/callback itself cannot be authenticated.
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'quickbooks',
    created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_oauth_states_expiry ON oauth_states(expires_at);
