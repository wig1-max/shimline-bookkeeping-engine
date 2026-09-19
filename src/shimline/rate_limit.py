"""How often one address may do one thing, counted where a restart cannot reset it.

The limiter this replaces lived in a dict in the web process. That was honest
for an internal form and wrong for the paid front door: the deploy script
restarts the service on every release, so every deploy handed each address a
fresh allowance, and a crash-looping service handed one out per crash.

Three decisions worth stating, because each was the alternative to something
simpler and worse.

**The address is stored as a keyed hash.** Persisting the counter means
persisting who was counted, which the in-memory version never had to answer
for. An unkeyed SHA-256 of an IPv4 address is not anonymisation: the entire
space is 2^32 and precomputing it is trivial. This is HMAC under the service's
own master key, so a copy of the database is not a copy of the visitor log.

**It fails open, and says so.** A limiter is a control on abuse, not a
correctness control -- nothing downstream is unsafe because a request was not
counted. If the counter cannot be read or written, refusing the customer's
payment would convert a storage problem into a lost sale, so the request is
allowed and the failure is printed. The payment and upload paths have their own
authorisation, which is where correctness actually lives.

**Expired rows are deleted rather than kept.** A row outside its window proves
nothing and would otherwise accumulate into exactly the log of who-visited-when
that the hashing above exists to avoid.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import datetime, timedelta

from . import clock, crypto

#: The paid front door. Both were 8 per hour under the in-memory limiter and
#: stay there: the point of this change is where the count lives, not how
#: generous it is.
SUBMIT = "submit"
PAYMENT = "payment"

DEFAULT_LIMIT = 8
DEFAULT_WINDOW_SECONDS = 3600


def subject_hash(subject: str) -> str:
    """A stable, keyed digest of a client address.

    Falls back to an unkeyed digest only when no master key is configured,
    which in practice means a test or a local run -- the service refuses to
    start in production without one. The fallback keeps the limiter working
    rather than making key configuration a prerequisite for counting.
    """
    value = (subject or "unknown").encode("utf-8")
    try:
        return hmac.new(crypto.master_key(), value, hashlib.sha256).hexdigest()
    except Exception:
        return hashlib.sha256(value).hexdigest()


def allow(conn: sqlite3.Connection, bucket: str, subject: str, *,
          limit: int = DEFAULT_LIMIT,
          window_seconds: int = DEFAULT_WINDOW_SECONDS,
          now: datetime | None = None) -> bool:
    """True when this subject may act again in this bucket, recording that it did.

    Returns True and writes a hit, or returns False and writes nothing -- a
    refused attempt must not extend its own lockout, or an address that keeps
    retrying can never recover.
    """
    moment = now or clock.now()
    cutoff = clock.format_timestamp(moment - timedelta(seconds=window_seconds))
    digest = subject_hash(subject)
    try:
        conn.execute(
            "DELETE FROM rate_limit_hits WHERE bucket=? AND subject_hash=? "
            "AND occurred_at <= ?", (bucket, digest, cutoff))
        used = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_hits WHERE bucket=? "
            "AND subject_hash=? AND occurred_at > ?",
            (bucket, digest, cutoff)).fetchone()[0]
        if used >= limit:
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO rate_limit_hits(bucket, subject_hash, occurred_at) "
            "VALUES(?,?,?)", (bucket, digest, clock.format_timestamp(moment)))
        conn.commit()
        return True
    except sqlite3.Error as failure:
        # See the module docstring: a limiter that cannot count must not become
        # an outage on the paid path.
        print(f"[warn] rate limit unavailable for {bucket}: {failure}")
        return True


def prune(conn: sqlite3.Connection, *, window_seconds: int = DEFAULT_WINDOW_SECONDS,
          now: datetime | None = None) -> int:
    """Drop every hit outside the window, for every subject.

    `allow` already clears the rows it walks past, but only for subjects that
    come back. One-shot addresses would otherwise leave a row behind for good,
    so retention sweeps this too rather than trusting the traffic pattern.
    """
    cutoff = clock.format_timestamp(
        (now or clock.now()) - timedelta(seconds=window_seconds))
    removed = conn.execute(
        "DELETE FROM rate_limit_hits WHERE occurred_at <= ?", (cutoff,)).rowcount
    conn.commit()
    return int(removed or 0)
