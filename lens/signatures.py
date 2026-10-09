"""Verify that a webhook delivery really came from Hindsight.

Hindsight signs the exact request body with HMAC-SHA256 and sends it two ways:
``X-Hindsight-Signature-V2: t=<unix>,v1=<hex>`` over ``<t>.<body>`` and
``X-Hindsight-Signature: sha256=<hex>`` over the body alone. Only the dated V2
is accepted: an undated signature verifies a captured delivery forever.
"""

from __future__ import annotations

import hashlib
import hmac
import time


def verify(secret: str, body: bytes, headers: dict[str, str], *, tolerance_seconds: int = 300) -> bool:
    lowered = {k.lower(): v for k, v in headers.items()}
    v2 = lowered.get("x-hindsight-signature-v2")
    return bool(v2) and _verify_v2(secret, body, v2, tolerance_seconds)


def _verify_v2(secret: str, body: bytes, header: str, tolerance_seconds: int) -> bool:
    try:
        parts = dict(p.strip().split("=", 1) for p in header.split(","))
        timestamp, received = parts["t"], parts["v1"]
        age = abs(time.time() - int(timestamp))
    except (KeyError, ValueError):
        return False
    if tolerance_seconds > 0 and age > tolerance_seconds:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)
