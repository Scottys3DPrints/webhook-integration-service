"""Webhook signature verification.

Scheme (the one Stripe uses, and the one most providers copy):

    X-Signature: t=<unix-ts>,v1=<hex hmac-sha256>

where the signed string is f"{t}.{raw_body}". Two properties matter and both
are easy to get wrong:

  * the HMAC is compared in constant time, so the comparison can't be used as
    an oracle to guess the signature byte by byte
  * the timestamp is checked against a tolerance, so a request captured off
    the wire can't be replayed later
"""

import hashlib
import hmac
import time

from app import config


class SignatureError(ValueError):
    pass


def sign(payload: bytes, timestamp: int, secret: str = None) -> str:
    """Build a header value. Used by the test suite and replay.py."""
    secret = secret if secret is not None else config.WEBHOOK_SECRET
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _parse(header: str):
    parts = {}
    for chunk in header.split(","):
        key, _, value = chunk.strip().partition("=")
        if key and value:
            parts[key] = value
    if "t" not in parts or "v1" not in parts:
        raise SignatureError("malformed signature header")
    try:
        timestamp = int(parts["t"])
    except ValueError:
        raise SignatureError("signature timestamp is not an integer")
    return timestamp, parts["v1"]


def verify(payload: bytes, header: str, secret: str = None, now: int = None):
    """Raise SignatureError unless the header is valid and fresh."""
    if not header:
        raise SignatureError("missing signature header")

    secret = secret if secret is not None else config.WEBHOOK_SECRET
    timestamp, provided = _parse(header)

    now = now if now is not None else int(time.time())
    age = abs(now - timestamp)
    if age > config.SIGNATURE_TOLERANCE_SECONDS:
        raise SignatureError(f"signature timestamp is {age}s off")

    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, provided):
        raise SignatureError("signature mismatch")
