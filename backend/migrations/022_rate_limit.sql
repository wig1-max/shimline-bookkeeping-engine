-- Rate limiting that survives a restart.
--
-- The limiter it replaces was a dict of deques in the web process, with the
-- comment "restarting the service clears it -- fine for this volume". The
-- volume part is still true. The restart part stopped being fine once these
-- endpoints became the paid front door: every deploy clears the counters, the
-- deploy script restarts the service on every release, and a service that
-- crash-loops hands an attacker a fresh allowance each time it comes back.
--
-- The address is stored as a keyed hash, never in the clear. An unkeyed digest
-- of an IPv4 address is not anonymisation -- the whole space is four billion
-- values and a rainbow table over it is minutes of work -- so this is HMAC
-- under the service's own master key, which never leaves the server. The row
-- is then useful for counting and useless to anyone who reads the database.
--
-- Rows are evidence of nothing once their window has passed, so the limiter
-- deletes them as it goes rather than accumulating a log of who visited.

CREATE TABLE IF NOT EXISTS rate_limit_hits (
    -- Which limit this counts against, so one bucket filling does not exhaust
    -- another. Payment attempts and document submissions are separate.
    bucket TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

-- The limiter's only read is "how many hits in this bucket, for this subject,
-- since this moment", and its only delete is the same shape.
CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup
    ON rate_limit_hits(bucket, subject_hash, occurred_at);
