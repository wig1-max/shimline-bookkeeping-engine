"""Application-level encryption with per-purpose key separation.

One master secret lives in the server environment. Every distinct kind of
secret gets its own key derived from it via HKDF, so a TOTP seed and a
QuickBooks refresh token are never protected by the same key material — a
mistake in one place cannot decrypt the other.

Rotating the master invalidates everything derived from it. That is a real
operational event, not a routine one: every stored QuickBooks token becomes
undecryptable and every operator has to re-enrol MFA. See docs/DEPLOY.md.
"""
from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MIN_KEY_LENGTH = 32

# Purposes. Add a new constant rather than reusing an existing string, so two
# unrelated secrets never share a derived key.
QUICKBOOKS_TOKENS = "shimline/quickbooks-tokens/v1"
TOTP_SECRETS = "shimline/totp-secrets/v1"
QUICKBOOKS_REPORTS = "shimline/quickbooks-reports/v1"
# What a real company file turned out to contain: row counts, the field names
# the provider actually returned, and why the derivation refused a document. Not
# a client's money, but the shape of a client's books is still theirs.
CONNECTION_PROBES = "shimline/connection-probes/v1"


class KeyUnavailable(RuntimeError):
    """Raised instead of silently storing a secret in the clear."""


def master_key() -> bytes:
    """The server's master secret.

    QBO_TOKEN_KEY is accepted as a legacy name so an older deployment keeps
    working; SHIMLINE_SECRET_KEY is the one to set.
    """
    raw = os.environ.get("SHIMLINE_SECRET_KEY") or os.environ.get("QBO_TOKEN_KEY") or ""
    raw = raw.strip()
    if not raw:
        raise KeyUnavailable(
            "SHIMLINE_SECRET_KEY is not set on the server — see docs/DEPLOY.md. "
            "Refusing to store a secret unencrypted."
        )
    if len(raw) < MIN_KEY_LENGTH:
        raise KeyUnavailable(
            f"SHIMLINE_SECRET_KEY must be at least {MIN_KEY_LENGTH} characters"
        )
    return raw.encode("utf-8")


def fernet_for(purpose: str) -> Fernet:
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=purpose.encode("utf-8")
    ).derive(master_key())
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(value: str, purpose: str) -> str:
    return fernet_for(purpose).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str, purpose: str) -> str:
    return fernet_for(purpose).decrypt(value.encode("ascii")).decode("utf-8")


def is_configured() -> bool:
    try:
        master_key()
        return True
    except KeyUnavailable:
        return False
