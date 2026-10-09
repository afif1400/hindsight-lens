"""A thin, read-only client for the Hindsight HTTP API.

Lens never writes to Hindsight. Every call here is a GET against an endpoint that
was confirmed against Hindsight 0.10.2 (see README, "What was verified").
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from .timeutil import iso


class HindsightError(RuntimeError):
    """Hindsight answered with something other than success or a clean 404."""


class HindsightClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        page_size: int = 100,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport)
        self._page_size = page_size

    def close(self) -> None:
        self._http.close()

    # -- plumbing ---------------------------------------------------------

    def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            return self._http.get(path, params=clean)
        except httpx.HTTPError as exc:
            raise HindsightError(f"GET {path} failed: {exc}") from exc

    @staticmethod
    def _raise_for(path: str, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise HindsightError(f"GET {path} returned {response.status_code}: {response.text[:300]}")

    def _get(self, path: str, params: dict[str, Any] | None = None, *, missing_ok: bool = False) -> Any:
        response = self._request(path, params)
        if response.status_code == 404 and missing_ok:
            return None
        self._raise_for(path, response)
        return response.json()

    def _paged(
        self, path: str, params: dict[str, Any] | None = None, *, key: str = "items", complete: bool = False
    ) -> Iterator[dict]:
        """Walk an offset-paginated listing to its end. ``complete``: raise if it ends short of its own total."""
        offset = 0
        while True:
            data = self._get(path, {**(params or {}), "limit": self._page_size, "offset": offset}) or {}
            items = data.get(key) or []
            yield from items
            offset += len(items)
            total = data.get("total")
            if len(items) < self._page_size or (total is not None and offset >= total):
                if complete and total is not None and offset < total:
                    raise HindsightError(f"GET {path} ended at {offset} of {total}")
                return

    @staticmethod
    def _bank(bank_id: str) -> str:
        return f"/v1/default/banks/{quote(bank_id, safe='')}"

    # -- server -----------------------------------------------------------

    def version(self) -> dict:
        return self._get("/version")

    def list_banks(self) -> list[dict]:
        """Every bank. Raises rather than return a partial listing: a missing bank may get erased."""
        return list(self._paged("/v1/default/banks", key="banks", complete=True))

    def bank_exists(self, bank_id: str) -> bool:
        """False only on Hindsight's own "Bank '<id>' not found". Any other answer raises or means it exists.

        A bare 404 from a proxy or a wrong base URL must never read as "deleted".
        """
        path = f"{self._bank(bank_id)}/stats"
        response = self._request(path)
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("detail") == f"Bank '{bank_id}' not found":
                return False
        self._raise_for(path, response)
        return True

    def bank_stats(self, bank_id: str) -> dict:
        return self._get(f"{self._bank(bank_id)}/stats")

    # -- beliefs (Hindsight calls them observations) ------------------------

    def list_observations(self, bank_id: str, *, updated_since: datetime | None = None) -> list[dict]:
        """Every observation, or only those written at/after ``updated_since``."""
        params: dict[str, Any] = {"type": "observation"}
        if updated_since is not None:
            params.update(time_field="updated_at", start_date=iso(updated_since))
        return list(self._paged(f"{self._bank(bank_id)}/memories/list", params))

    def fact_entities(self, bank_id: str) -> dict[str, str | list]:
        """Each fact's entities, as the listing sends them, a hundred facts per call."""
        out: dict[str, str | list] = {}
        for kind in ("world", "experience"):
            for fact in self._paged(f"{self._bank(bank_id)}/memories/list", {"type": kind}):
                out[fact["id"]] = fact.get("entities") or []
        return out

    def observation_history(self, bank_id: str, observation_id: str) -> list[dict] | None:
        """Earlier wordings of one observation. None if it no longer exists."""
        return self._get(f"{self._bank(bank_id)}/memories/{quote(observation_id, safe='')}/history", missing_ok=True)

    def get_memory(self, bank_id: str, memory_id: str) -> dict | None:
        return self._get(f"{self._bank(bank_id)}/memories/{quote(memory_id, safe='')}", missing_ok=True)

    def get_chunk(self, chunk_id: str) -> dict | None:
        """The original text a fact was extracted from."""
        return self._get(f"/v1/default/chunks/{quote(chunk_id, safe='')}", missing_ok=True)

    # -- why things changed, and what was recalled ---------------------------

    def consolidation_traces(self, bank_id: str, *, since: datetime | None = None) -> list[dict]:
        """LLM calls that created, rewrote or deleted observations.

        Their output carries a ``reason`` per change and the id of anything
        deleted. Hindsight keeps these rows for one day by default.
        """
        params = {"operation": "consolidation", "start_date": iso(since)}
        return list(self._paged(f"{self._bank(bank_id)}/llm-requests", params))

    def recall_audits(self, bank_id: str, *, since: datetime | None = None) -> list[dict]:
        """Audit rows for recalls. Empty unless audit logging is switched on."""
        params = {"action": "recall", "start_date": iso(since)}
        return list(self._paged(f"{self._bank(bank_id)}/audit-logs", params))

    def mental_models(self, bank_id: str) -> list[dict]:
        # The listing returns metadata only unless content is asked for.
        return list(self._paged(f"{self._bank(bank_id)}/mental-models", {"detail": "content"}))
