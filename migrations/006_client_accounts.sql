-- Client accounts.
--
-- Everything a client eventually connects — QuickBooks today, a bank feed or a
-- mailbox later — has to hang off one durable record, or reconciling across
-- those systems is impossible. That record is created automatically when a
-- payment is verified, so there is no signup step before revenue.
--
-- Deliberately no password column. The QuickBooks consent screen authenticates
-- the connection, and a signed, expiring emailed link authenticates the person.
-- Storing client passwords would add reset flows, a credential-breach surface
-- and support load, to solve a problem nobody has.
--
-- client_users is not contacts. A contact is somebody we know about; a client
-- user is somebody who can act. A prospect has contacts and no account.

CREATE TABLE IF NOT EXISTS client_users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    organization_id TEXT REFERENCES organizations(id) ON DELETE SET NULL,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_client_users_org ON client_users(organization_id);

-- Expiring, single-use links. Only the hash is stored, as with every other
-- token in this codebase, so a database copy does not yield working links.
CREATE TABLE IF NOT EXISTS client_access_tokens (
    token_hash TEXT PRIMARY KEY,
    client_user_id TEXT NOT NULL REFERENCES client_users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL DEFAULT 'sign_in' CHECK (purpose IN ('sign_in', 'connect')),
    issued_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_client_access_expiry ON client_access_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_client_access_user ON client_access_tokens(client_user_id);

-- Client sessions are separate from operator sessions on purpose: different
-- lifetimes, different cookie path, and no possibility of one being mistaken
-- for the other by a route that only checks "is there a session".
CREATE TABLE IF NOT EXISTS client_sessions (
    token_hash TEXT PRIMARY KEY,
    client_user_id TEXT NOT NULL REFERENCES client_users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    ip_hash TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_client_sessions_user ON client_sessions(client_user_id);
CREATE INDEX IF NOT EXISTS idx_client_sessions_expiry ON client_sessions(expires_at);

-- Provenance: which client account a payment, a submission, and a connection
-- came from. Nullable because everything created before today has none.
ALTER TABLE payments ADD COLUMN client_user_id TEXT;
ALTER TABLE submissions ADD COLUMN client_user_id TEXT;
ALTER TABLE connections ADD COLUMN connected_by_client_id TEXT;

CREATE INDEX IF NOT EXISTS idx_payments_client ON payments(client_user_id);
CREATE INDEX IF NOT EXISTS idx_submissions_client ON submissions(client_user_id);

-- Which side began an authorization: an operator in the workspace, or the
-- client from their own portal. "Who authorised access to these books" is
-- exactly the question an audit needs to answer.
ALTER TABLE oauth_states ADD COLUMN created_by_client_id TEXT;
