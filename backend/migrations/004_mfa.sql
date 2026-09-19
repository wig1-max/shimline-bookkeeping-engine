-- Two-factor authentication for operator accounts.
--
-- The TOTP seed is a credential equivalent to a second password, so it is
-- stored encrypted (see shimline/crypto.py) rather than in the clear.
-- totp_last_counter blocks replay: a code stays valid for the remainder of its
-- 30-second step after somebody has watched it being typed.

ALTER TABLE users ADD COLUMN totp_secret_enc TEXT;
ALTER TABLE users ADD COLUMN totp_confirmed_at TEXT;
ALTER TABLE users ADD COLUMN totp_last_counter INTEGER;

-- Single-use printable codes for the day the phone is lost. Only hashes are
-- stored; the plaintext is shown once at enrolment and never again.
CREATE TABLE IF NOT EXISTS recovery_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_recovery_codes_user ON recovery_codes(user_id, used_at);

-- The half-authenticated state between a correct password and a correct code.
-- A row here is NOT a session: it grants nothing except the right to be asked
-- for a second factor, and it expires quickly.
CREATE TABLE IF NOT EXISTS mfa_challenges (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL DEFAULT 0,
    ip_hash TEXT,
    next_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mfa_challenges_expiry ON mfa_challenges(expires_at);
