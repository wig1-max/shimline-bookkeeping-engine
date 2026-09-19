"""Time-based one-time passwords (RFC 6238) over HMAC-OTP (RFC 4226).

Written out rather than pulled in as a dependency because the algorithm is
short, fully specified, and — unlike password hashing or encryption — has
official test vectors that prove an implementation correct. `test_mfa.py`
checks this against the vectors in RFC 6238 Appendix B for SHA-1, SHA-256 and
SHA-512, so "we rolled our own" is backed by the spec's own answers.

SHA-1 with 6 digits and a 30-second step is the default because that is what
Google Authenticator, 1Password, Aegis and Authy actually implement.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
# One step either side, so a clock a few seconds out still works.
DEFAULT_WINDOW = 1
SECRET_BYTES = 20  # 160 bits, the RFC 4226 recommendation


def generate_secret() -> str:
    """A fresh base32 secret, in the form authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def normalize(secret: str) -> bytes:
    """Accept what a human types: spaces, lower case, missing padding."""
    cleaned = (secret or "").replace(" ", "").replace("-", "").upper()
    padding = "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(cleaned + padding, casefold=True)
    except Exception as exc:
        raise ValueError("That is not a valid base32 secret") from exc


def hotp(secret: str, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    """RFC 4226 section 5.3."""
    mac = hmac.new(normalize(secret), struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    truncated = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** digits)).zfill(digits)


def counter_at(moment: float | None = None, period: int = PERIOD) -> int:
    return int((moment if moment is not None else time.time()) // period)


def generate(secret: str, moment: float | None = None, digits: int = DIGITS,
             period: int = PERIOD, digest=hashlib.sha1) -> str:
    return hotp(secret, counter_at(moment, period), digits, digest)


def verify(secret: str, code: str, *, moment: float | None = None,
           window: int = DEFAULT_WINDOW, last_counter: int | None = None) -> int | None:
    """Return the counter a valid code came from, or None.

    `last_counter` blocks replay: a code already accepted cannot be used again,
    which matters because a TOTP stays valid for the rest of its 30-second step
    after someone has watched it being typed.

    Comparison is constant-time so a wrong code leaks nothing by timing.
    """
    cleaned = (code or "").strip().replace(" ", "")
    if not cleaned.isdigit() or len(cleaned) != DIGITS:
        return None
    current = counter_at(moment)
    for offset in range(-window, window + 1):
        candidate = current + offset
        if candidate < 0:
            continue
        if last_counter is not None and candidate <= last_counter:
            continue
        if hmac.compare_digest(hotp(secret, candidate), cleaned):
            return candidate
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Shimline") -> str:
    """otpauth:// URI — what a QR code would encode, for manual entry."""
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}"
        f"&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"
    )


def format_secret(secret: str) -> str:
    """Group into fours so it can be typed off a screen without losing place."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
