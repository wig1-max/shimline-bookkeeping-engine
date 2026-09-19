-- Intuit's security requirements say, of the OAuth workflow:
--   "Encrypt and store the refresh token and realmID in persistent memory."
--
-- The realm ID identifies a customer's QuickBooks company, so it is treated as
-- customer-identifying information and encrypted like the tokens. Lookups use
-- a separate SHA-256 hash column, because Fernet ciphertext is randomised and
-- cannot be matched with an equality test.
--
-- The table is recreated rather than altered because the uniqueness constraint
-- has to move to the hash. This is safe: no connection has ever been stored.

DROP TABLE IF EXISTS connections;

CREATE TABLE connections (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'quickbooks',
    environment TEXT NOT NULL DEFAULT 'sandbox' CHECK (environment IN ('sandbox', 'production')),
    realm_id_enc TEXT NOT NULL,
    realm_id_hash TEXT NOT NULL,
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
    UNIQUE (provider, realm_id_hash, environment)
);

CREATE INDEX IF NOT EXISTS idx_connections_org ON connections(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_connections_realm ON connections(realm_id_hash);
