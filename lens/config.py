"""Configuration, read from LENS_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Where Hindsight's HTTP API lives, and the key to call it with (if it needs one).
    hindsight_url: str = "http://localhost:8888"
    hindsight_api_key: str | None = None
    # Where Lens keeps its own copy. SQLite by default; any SQLAlchemy URL works.
    database_url: str = "sqlite:///lens.db"
    # Must match HINDSIGHT_API_WEBHOOK_SECRET. Unset = deliveries are not verified.
    webhook_secret: str | None = None
    webhook_tolerance_seconds: int = 300
    # The login for the dashboard, the snapshot API and manual sync. Unset password = those stay shut.
    username: str = "lens"
    password: str | None = None
    # The safety net for dropped pings, and what harvests reasons before they expire.
    sweep_interval_seconds: int = 3600
    request_timeout_seconds: float = 30.0
    page_size: int = 100
    # Re-read this far behind each cursor so a row written mid-page is not skipped.
    cursor_overlap_seconds: int = 10
    # Copy the original chunk text behind each fact (the closest thing to a quote).
    fetch_source_text: bool = True
    source_text_max_chars: int = 4000
    host: str = "0.0.0.0"
    port: int = 8900

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        return cls(
            hindsight_url=os.getenv("LENS_HINDSIGHT_URL", d.hindsight_url).rstrip("/"),
            hindsight_api_key=os.getenv("LENS_HINDSIGHT_API_KEY") or None,
            database_url=os.getenv("LENS_DATABASE_URL", d.database_url),
            webhook_secret=os.getenv("LENS_WEBHOOK_SECRET") or None,
            webhook_tolerance_seconds=int(os.getenv("LENS_WEBHOOK_TOLERANCE_SECONDS", d.webhook_tolerance_seconds)),
            username=os.getenv("LENS_USERNAME") or d.username,
            password=os.getenv("LENS_PASSWORD") or None,
            sweep_interval_seconds=int(os.getenv("LENS_SWEEP_INTERVAL_SECONDS", d.sweep_interval_seconds)),
            request_timeout_seconds=float(os.getenv("LENS_REQUEST_TIMEOUT_SECONDS", d.request_timeout_seconds)),
            page_size=int(os.getenv("LENS_PAGE_SIZE", d.page_size)),
            cursor_overlap_seconds=int(os.getenv("LENS_CURSOR_OVERLAP_SECONDS", d.cursor_overlap_seconds)),
            fetch_source_text=_bool("LENS_FETCH_SOURCE_TEXT", d.fetch_source_text),
            source_text_max_chars=int(os.getenv("LENS_SOURCE_TEXT_MAX_CHARS", d.source_text_max_chars)),
            host=os.getenv("LENS_HOST", d.host),
            port=int(os.getenv("LENS_PORT", d.port)),
        )
