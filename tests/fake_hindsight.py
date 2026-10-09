"""An in-memory Hindsight that answers with the exact shapes the real one does.

The shapes were copied from responses captured from a live Hindsight 0.10.2, so
these tests exercise the real client and the real sync logic end to end; only the
network is replaced (httpx.MockTransport).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import httpx

from lens.timeutil import parse_ts

BANK = "maya"


class FakeHindsight:
    def __init__(self) -> None:
        self.clock = datetime(2026, 10, 6, 17, 18, 19, tzinfo=timezone.utc)
        self.observations: dict[str, dict] = {}
        self.history: dict[str, list[dict]] = {}
        self.facts: dict[str, dict] = {}
        self.chunks: dict[str, dict] = {}
        self.traces: list[dict] = []
        self.audits: list[dict] = []
        self.mental_models: list[dict] = []
        self.history_enabled = True
        self.hide_from_listing: set[str] = set()
        self.stats_override: int | None = None
        self.fail_paths: set[str] = set()
        self.timeout_paths: set[str] = set()
        self.bank_deleted = False
        self.short_listing = False  # the bank listing claims one more bank than it returns
        self.other_banks: list[str] = []  # listed, but with nothing behind them
        self.requests: list[str] = []

    def tick(self, seconds: float = 1.0) -> datetime:
        self.clock += timedelta(seconds=seconds)
        return self.clock

    def _now(self) -> str:
        return self.clock.isoformat()

    # -- what "Hindsight" does ---------------------------------------------

    def retain(self, document_id: str, raw_text: str, fact_text: str, entities: list[str] | None = None) -> str:
        self.tick()
        fact_id = str(uuid.uuid4())
        chunk_id = f"{BANK}_{document_id}_0"
        self.chunks[chunk_id] = {
            "chunk_id": chunk_id,
            "document_id": document_id,
            "bank_id": BANK,
            "chunk_index": 0,
            "chunk_text": raw_text,
        }
        self.facts[fact_id] = {
            "id": fact_id,
            "text": fact_text,
            "type": "world",
            "context": "",
            "document_id": document_id,
            "chunk_id": chunk_id,
            "mentioned_at": self._now(),
            "tags": ["user:maya"],
            "state": "valid",
            "entities": list(entities or []),
        }
        return fact_id

    def _trace(self, *, creates=(), updates=(), deletes=(), memory_ids=(), source_ids=(), output=None) -> None:
        payload = {"creates": list(creates), "updates": list(updates), "deletes": list(deletes)}
        self.traces.append(
            {
                "id": str(uuid.uuid4()),
                "bank_id": BANK,
                "operation": "consolidation",
                "scope": "consolidation",
                "trace_id": str(uuid.uuid4()),
                "status": "success",
                "started_at": self._now(),
                "ended_at": self._now(),
                "output": output if output is not None else json.dumps(payload),
                "metadata": {"memory_ids": list(memory_ids), "source_memory_ids": list(source_ids)},
            }
        )

    def create_observation(self, text: str, fact_ids: list[str], reason: str | None = "new claim") -> str:
        self.tick()
        obs_id = str(uuid.uuid4())
        self.observations[obs_id] = {
            "id": obs_id,
            "text": text,
            "fact_type": "observation",
            "proof_count": len(fact_ids),
            "tags": ["user:maya"],
            "state": "valid",
            "updated_at": self._now(),
            "source_memory_ids": list(fact_ids),
        }
        self.history[obs_id] = []
        if reason is not None:
            self._trace(creates=[{"text": text, "source_fact_ids": fact_ids, "reason": reason}], memory_ids=[obs_id])
        return obs_id

    def update_observation(
        self, obs_id: str, text: str, new_fact_ids: list[str], reason: str | None = "preference changed", output=None
    ) -> None:
        self.tick()
        obs = self.observations[obs_id]
        if self.history_enabled:
            self.history[obs_id].append(
                {
                    "previous_text": obs["text"],
                    "previous_tags": obs["tags"],
                    "changed_at": self._now(),
                    "new_source_memory_ids": list(new_fact_ids),
                }
            )
        obs.update(text=text, updated_at=self._now(), source_memory_ids=obs["source_memory_ids"] + new_fact_ids)
        obs["proof_count"] = len(obs["source_memory_ids"])
        if reason is not None:
            self._trace(
                updates=[{"text": text, "observation_id": obs_id, "source_fact_ids": new_fact_ids, "reason": reason}],
                memory_ids=[obs_id],
                output=output,
            )

    def delete_observation(
        self, obs_id: str, reason: str | None = "no longer true", because: tuple[str, ...] = ()
    ) -> None:
        self.tick()
        del self.observations[obs_id]
        del self.history[obs_id]
        if reason is not None:
            self._trace(deletes=[{"observation_id": obs_id, "reason": reason}], source_ids=because)

    def recall(self, query: str, obs_ids: list[str]) -> str:
        self.tick()
        audit_id = str(uuid.uuid4())
        self.audits.append(
            {
                "id": audit_id,
                "action": "recall",
                "transport": "http",
                "bank_id": BANK,
                "started_at": self._now(),
                "ended_at": self._now(),
                "duration_ms": 39,
                "request": {"query": query},
                "response": {
                    "results": [
                        {
                            "id": i,
                            "type": "world" if i in self.facts else "observation",
                            "scores": {"final": round(0.3 - n * 0.05, 2)},
                        }
                        for n, i in enumerate(obs_ids)
                    ]
                },
                "metadata": {},
            }
        )
        return audit_id

    def delete_bank(self) -> None:
        """What Hindsight does on DELETE /banks/{id}: the bank and its memory go, its LLM traces stay."""
        self.bank_deleted = True
        for store in (self.observations, self.history, self.facts, self.chunks):
            store.clear()
        self.audits.clear()
        self.mental_models.clear()

    def recreate_bank(self) -> None:
        """A bank with the same id is created again, empty (the old traces are still there)."""
        self.bank_deleted = False

    # -- the HTTP surface ----------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _page(self, items: list[dict], request: httpx.Request, key: str = "items") -> httpx.Response:
        limit = int(request.url.params.get("limit", 100))
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(
            200, json={key: items[offset : offset + limit], "total": len(items), "limit": limit, "offset": offset}
        )

    def _since(self, rows: list[dict], request: httpx.Request, field: str) -> list[dict]:
        start = parse_ts(request.url.params.get("start_date"))
        rows = [r for r in rows if start is None or parse_ts(r[field]) >= start]
        return sorted(rows, key=lambda r: r[field], reverse=True)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        self.requests.append(path)
        if any(path.endswith(p) for p in self.fail_paths):
            return httpx.Response(500, text="boom")
        if any(path.endswith(p) for p in self.timeout_paths):
            raise httpx.ReadTimeout("timed out", request=request)
        prefix = f"/v1/default/banks/{BANK}"
        if self.bank_deleted and (path == prefix or path.startswith(prefix + "/")):
            return httpx.Response(404, json={"detail": f"Bank '{BANK}' not found"})
        if path == "/version":
            return httpx.Response(
                200,
                json={
                    "api_version": "0.10.2",
                    "features": {"observations": True, "audit_log": True, "llm_trace": True},
                },
            )
        if path == "/v1/default/banks":
            listed = ([] if self.bank_deleted else [BANK]) + self.other_banks
            response = self._page([{"bank_id": b} for b in listed], request, key="banks")
            if self.short_listing:
                body = response.json()
                return httpx.Response(200, json={**body, "total": body["total"] + 1})
            return response
        if path.startswith("/v1/default/chunks/"):
            chunk = self.chunks.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=chunk) if chunk else httpx.Response(404, json={"detail": "nope"})
        if path == f"{prefix}/stats":
            total = self.stats_override if self.stats_override is not None else len(self.observations)
            return httpx.Response(200, json={"bank_id": BANK, "total_observations": total})
        if path == f"{prefix}/memories/list" and request.url.params.get("type") in ("world", "experience"):
            kind = request.url.params["type"]
            # The listing sends entities as one comma-separated string.
            rows = [{**f, "entities": ", ".join(f["entities"])} for f in self.facts.values() if f["type"] == kind]
            return self._page(rows, request)
        if path == f"{prefix}/memories/list":
            rows = [o for o in self.observations.values() if o["id"] not in self.hide_from_listing]
            if request.url.params.get("time_field") == "updated_at":
                rows = self._since(rows, request, "updated_at")
            return self._page(rows, request)
        if path == f"{prefix}/llm-requests":
            assert request.url.params.get("operation") == "consolidation"
            return self._page(self._since(self.traces, request, "started_at"), request)
        if path == f"{prefix}/audit-logs":
            assert request.url.params.get("action") == "recall"
            return self._page(self._since(self.audits, request, "started_at"), request)
        if path == f"{prefix}/mental-models":
            with_content = request.url.params.get("detail") in ("content", "full")
            models = self.mental_models if with_content else [{**m, "content": None} for m in self.mental_models]
            return self._page(models, request)
        if path.startswith(f"{prefix}/memories/"):
            rest = path[len(prefix) + len("/memories/") :]
            if rest.endswith("/history"):
                obs_id = rest[: -len("/history")]
                if obs_id not in self.observations:
                    return httpx.Response(404, json={"detail": "not found"})
                current = self.observations[obs_id]["source_memory_ids"]
                out = []
                for entry in self.history[obs_id]:
                    facts = [
                        {
                            "id": f,
                            "text": self.facts.get(f, {}).get("text"),
                            "type": "world",
                            "context": None,
                            "is_new": f in entry["new_source_memory_ids"],
                        }
                        for f in current
                    ]
                    out.append({**entry, "source_facts": facts})
                return httpx.Response(200, json=out)
            item = self.observations.get(rest) or self.facts.get(rest)
            if item is None:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json={**item, "type": item.get("type") or item.get("fact_type")})
        return httpx.Response(404, json={"detail": f"unrouted {path}"})
