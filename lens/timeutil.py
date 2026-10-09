"""Timestamp helpers. Everything inside Lens is an aware UTC datetime."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_SHORT_OFFSET = re.compile(r"([+-]\d{2})$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: object) -> datetime | None:
    """Parse the timestamp shapes Hindsight emits; return None for anything else.

    Seen in the wild: ``2026-10-06T17:18:21.851551+00:00`` (read endpoints),
    ``2026-10-06T17:18:19.443848Z`` (webhooks) and ``2026-10-06 17:18:19.40+00``
    (webhook registry rows).
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        text = _SHORT_OFFSET.sub(r"\1:00", text)
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None
