"""Bring one memory bank's history in the store up to date.

A sync is idempotent: running it twice records nothing twice. Each step keeps
its own cursor, and a step that fails leaves its cursor where it was so the
next sync retries it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, TypeVar

from .config import Settings
from .hindsight import HindsightClient, HindsightError
from .store import Store
from .timeutil import parse_ts, utcnow

logger = logging.getLogger("lens.sync")

T = TypeVar("T")

# How far a version's own timestamp may sit from the LLM call that caused it.
# Hindsight writes a change within moments of the call; two minutes is generous.
REASON_MATCH_WINDOW = timedelta(minutes=2)
# The call's end and the write are stamped by different clocks (app vs database).
REASON_CLOCK_SLACK = timedelta(seconds=5)


@dataclass
class SyncResult:
    bank_id: str
    observations_seen: int = 0
    versions_added: int = 0
    deletes_detected: int = 0
    reasons_attached: int = 0
    evidence_added: int = 0
    recalls_added: int = 0
    portraits_added: int = 0
    reconciled: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Trace:
    """One consolidation LLM call, reduced to what explains a change."""

    request_id: str  # unique per LLM call; a consolidation run can make several
    started_at: datetime | None
    ended_at: datetime | None
    memory_ids: set[str]
    source_memory_ids: list[str]
    creates: list[dict]
    updates: list[dict]
    deletes: list[dict]


class BankSyncer:
    def __init__(self, client: HindsightClient, store: Store, settings: Settings) -> None:
        self.client = client
        self.store = store
        self.settings = settings

    # -- entry point --------------------------------------------------------

    def sync_bank(self, bank_id: str, *, reconcile: bool = False) -> SyncResult:
        """Sync one bank. ``reconcile`` forces the full check for deleted beliefs."""
        result = SyncResult(bank_id)
        self.store.touch_bank(bank_id)
        state = self.store.get_bank(bank_id) or {}
        overlap = timedelta(seconds=self.settings.cursor_overlap_seconds)
        cursors: dict[str, Any] = {}
        evidence_ids: dict[str, dict] = {}  # fact id -> whatever we already know about it

        # 1. Beliefs written since the last sync, with their history.
        since = state.get("obs_cursor")
        changed = self._step(
            result, "beliefs", lambda: self.client.list_observations(bank_id, updated_since=_back(since, overlap))
        )
        if changed is not None:
            newest = since
            for obs in changed:
                updated_at = parse_ts(obs.get("updated_at"))
                self._apply_observation(bank_id, obs, result, evidence_ids)
                if updated_at and (newest is None or updated_at > newest):
                    newest = updated_at
            cursors["obs_cursor"] = newest

        # 2. The LLM calls behind those changes. Fetched now, applied after step 3,
        #    because they also name beliefs that were deleted.
        trace_since = state.get("trace_cursor")
        raw_traces = self._step(
            result, "reasons", lambda: self.client.consolidation_traces(bank_id, since=_back(trace_since, overlap))
        )
        traces = sorted(
            (t for t in (_parse_trace(r) for r in raw_traces or []) if t is not None),
            key=lambda t: t.started_at or utcnow(),
        )

        # 3. Deleted beliefs. Hindsight keeps no tombstone, so a delete is only
        #    visible as an id we hold that Hindsight no longer lists.
        if not reconcile:
            reconcile = self._needs_reconcile(bank_id, traces, result)
        if reconcile:
            self._step(result, "deletes", lambda: self._reconcile(bank_id, result, evidence_ids))
            result.reconciled = True

        # 4. Attach each change's reason while Hindsight still has it.
        if raw_traces is not None:
            for trace in traces:
                self._apply_trace(bank_id, trace, result, evidence_ids)
            newest_trace = max((t.started_at for t in traces if t.started_at), default=trace_since)
            cursors["trace_cursor"] = newest_trace
            cursors["reasons_checked_at"] = utcnow()

        # 5. The facts behind the beliefs, and the text each fact came from. Facts held
        #    from before Lens kept entities get them from the listing, once.
        self._step(result, "entities", lambda: self._backfill_entities(bank_id))
        self._step(result, "evidence", lambda: self._collect_evidence(bank_id, evidence_ids, result))

        # 6. What was recalled.
        audit_since = state.get("audit_cursor")
        audits = self._step(
            result, "recalls", lambda: self.client.recall_audits(bank_id, since=_back(audit_since, overlap))
        )
        if audits is not None:
            newest_audit = audit_since
            for row in audits:
                started = self._apply_recall(bank_id, row, result)
                if started and (newest_audit is None or started > newest_audit):
                    newest_audit = started
            cursors["audit_cursor"] = newest_audit

        # 7. The plain-language portrait, when the bank has a mental model.
        self._step(result, "portraits", lambda: self._collect_portraits(bank_id, result))

        self.store.touch_bank(
            bank_id,
            last_synced_at=utcnow(),
            last_error="; ".join(result.warnings) or None,
            last_result=result.as_dict(),
            **cursors,
        )
        return result

    def _step(self, result: SyncResult, name: str, fn: Callable[[], T]) -> T | None:
        """Run one step; a Hindsight failure becomes a warning, not a lost sync."""
        try:
            return fn()
        except HindsightError as exc:
            logger.warning("bank %s: %s step failed: %s", result.bank_id, name, exc)
            result.warnings.append(f"{name}: {exc}")
            return None

    # -- step 1: beliefs and their versions ---------------------------------

    def _apply_observation(self, bank_id: str, obs: dict, result: SyncResult, evidence_ids: dict[str, dict]) -> None:
        obs_id = str(obs["id"])
        text = obs.get("text") or ""
        updated_at = parse_ts(obs.get("updated_at"))
        source_ids = [str(s) for s in obs.get("source_memory_ids") or []]
        existing = self.store.get_belief(bank_id, obs_id)
        result.observations_seen += 1

        unchanged = (
            existing is not None
            and existing["status"] == "live"
            and existing["hindsight_updated_at"] == updated_at
            and existing["text"] == text
        )
        if unchanged:
            return

        history = self.client.observation_history(bank_id, obs_id) or []
        entries = sorted(history, key=lambda e: parse_ts(e.get("changed_at")) or utcnow())
        known = {v["change_key"]: v for v in self.store.versions(bank_id, obs_id)}

        # The first wording. If history already exists when Lens first meets the
        # belief, the true creation moment is unknown (and may be past the cap).
        if "created" not in known:
            first_text = (entries[0].get("previous_text") if entries else None) or text
            added = self.store.add_version(
                bank_id=bank_id,
                observation_id=obs_id,
                change_key="created",
                change_type="earliest_known" if entries else "created",
                text=first_text,
                changed_at=None if entries else updated_at,
                changed_at_is_estimate=bool(entries),
                evidence_count=None if entries else len(source_ids),
                new_source_memory_ids=[] if entries else source_ids,
            )
            result.versions_added += int(added)

        # One version per recorded change. The wording *after* change i is the
        # "previous" wording of change i+1, or today's wording for the last one.
        for i, entry in enumerate(entries):
            changed_at = parse_ts(entry.get("changed_at"))
            if changed_at is None:
                continue
            after = (entries[i + 1].get("previous_text") if i + 1 < len(entries) else text) or text
            facts = entry.get("source_facts") or []
            for fact in facts:
                if fact.get("id"):
                    evidence_ids.setdefault(str(fact["id"]), fact)
            added = self.store.add_version(
                bank_id=bank_id,
                observation_id=obs_id,
                change_key=changed_at.isoformat(),
                change_type="updated",
                text=after,
                changed_at=changed_at,
                evidence_count=len(facts) or None,
                new_source_memory_ids=[str(s) for s in entry.get("new_source_memory_ids") or []],
            )
            result.versions_added += int(added)

        # History can be switched off in Hindsight. If the wording moved and no
        # history entry explains it, record the change from the row itself.
        versions = self.store.versions(bank_id, obs_id)
        latest_text = next((v["text"] for v in reversed(versions) if v["change_type"] != "deleted"), None)
        if latest_text != text and updated_at is not None:
            added = self.store.add_version(
                bank_id=bank_id,
                observation_id=obs_id,
                change_key=updated_at.isoformat(),
                change_type="updated",
                text=text,
                changed_at=updated_at,
                evidence_count=len(source_ids),
            )
            result.versions_added += int(added)

        status = "invalidated" if obs.get("state") == "invalidated" else "live"
        self.store.save_belief(
            bank_id,
            obs_id,
            text=text,
            status=status,
            proof_count=obs.get("proof_count"),
            source_memory_ids=source_ids,
            tags=list(obs.get("tags") or []),
            last_changed_at=updated_at,
            hindsight_updated_at=updated_at,
            deleted_at=None,
        )
        for sid in source_ids:
            evidence_ids.setdefault(sid, {})

    # -- step 3: deletes ----------------------------------------------------

    def _needs_reconcile(self, bank_id: str, traces: list[_Trace], result: SyncResult) -> bool:
        live = self.store.belief_ids(bank_id, status="live")
        named = {str(d.get("observation_id")) for t in traces for d in t.deletes}
        if named & live:
            return True
        stats = self._step(result, "stats", lambda: self.client.bank_stats(bank_id))
        total = (stats or {}).get("total_observations")
        return total is not None and total != len(live)

    def _reconcile(self, bank_id: str, result: SyncResult, evidence_ids: dict[str, dict]) -> None:
        listed = {str(o["id"]): o for o in self.client.list_observations(bank_id)}
        known = self.store.belief_ids(bank_id)
        for obs_id, obs in listed.items():
            if obs_id not in known:
                self._apply_observation(bank_id, obs, result, evidence_ids)

        now = utcnow()
        for obs_id in self.store.belief_ids(bank_id, status="live") - set(listed):
            # Confirm each one directly: a listing that is merely incomplete must
            # never be recorded as a deletion.
            current = self.client.get_memory(bank_id, obs_id)
            if current is not None and current.get("state") != "invalidated":
                result.warnings.append(f"deletes: {obs_id} is unlisted but still exists; left as live")
                continue
            change = "invalidated" if current is not None else "deleted"
            belief = self.store.get_belief(bank_id, obs_id) or {}
            self.store.add_version(
                bank_id=bank_id,
                observation_id=obs_id,
                change_key=change,
                change_type=change,
                text=belief.get("text") or "",
                changed_at=now,
                changed_at_is_estimate=True,
                reason=(current or {}).get("invalidation_reason"),
            )
            self.store.save_belief(bank_id, obs_id, status=change, deleted_at=now, last_changed_at=now)
            result.deletes_detected += 1
            result.versions_added += 1

    # -- step 4: reasons ----------------------------------------------------

    def _apply_trace(self, bank_id: str, trace: _Trace, result: SyncResult, evidence_ids: dict[str, dict]) -> None:
        """Attach this LLM call's reasons to the versions it produced.

        The cursor overlap means a call is usually seen more than once, and the
        same belief can be rewritten twice within seconds. So a call is applied
        to a version only once, and only to a version written just after it.
        """

        def attach(version: dict, reason: str | None, **extra: Any) -> None:
            if not reason or version.get("reason"):
                return
            self.store.update_version(
                version["id"],
                reason=reason,
                reason_request_id=trace.request_id,
                trigger_memory_ids=trace.source_memory_ids,
                **extra,
            )
            version["reason"] = reason
            version["reason_request_id"] = trace.request_id
            result.reasons_attached += 1
            for memory_id in trace.source_memory_ids:  # keep the facts that caused it
                evidence_ids.setdefault(memory_id, {})

        def already_used(versions: list[dict]) -> bool:
            return bool(trace.request_id) and any(v.get("reason_request_id") == trace.request_id for v in versions)

        for update in trace.updates:
            versions = self.store.versions(bank_id, str(update.get("observation_id") or ""))
            if already_used(versions):
                continue
            unexplained = [v for v in versions if v["change_type"] == "updated" and not v.get("reason")]
            match = _pick(unexplained, text=update.get("text"), after=trace.ended_at)
            if match:
                attach(match, update.get("reason"))

        for delete in trace.deletes:
            obs_id = str(delete.get("observation_id") or "")
            for version in self.store.versions(bank_id, obs_id):
                if version["change_type"] == "deleted":
                    # The trace knows when the delete really happened; ours was a guess.
                    extra = {"changed_at": trace.ended_at, "changed_at_is_estimate": False} if trace.ended_at else {}
                    was_unexplained = not version.get("reason")
                    attach(version, delete.get("reason"), **extra)
                    if was_unexplained and version.get("reason") and trace.ended_at:
                        self.store.save_belief(
                            bank_id, obs_id, deleted_at=trace.ended_at, last_changed_at=trace.ended_at
                        )

        if trace.creates:
            unexplained = self.store.versions_without_reason(bank_id, ("created", "earliest_known"))
            explained_here = self.store.versions_by_request(bank_id, trace.request_id) if trace.request_id else []
            for create in trace.creates:
                if any(v["text"] == create.get("text") for v in explained_here):
                    continue
                same_text = [v for v in unexplained if v["text"] == create.get("text") and not v.get("reason")]
                preferred = [v for v in same_text if v["observation_id"] in trace.memory_ids] or same_text
                match = _pick(preferred, text=create.get("text"), after=trace.ended_at)
                if match:
                    attach(match, create.get("reason"))
                    explained_here.append(match)

    # -- step 5: evidence ---------------------------------------------------

    def _collect_evidence(self, bank_id: str, wanted: dict[str, dict], result: SyncResult) -> None:
        ids = list(wanted)
        for memory_id in sorted(set(ids) - self.store.known_evidence_ids(bank_id, ids)):
            fact = self.client.get_memory(bank_id, memory_id)
            if fact is None:
                # The fact was deleted before we got here; keep what history told us.
                hint = wanted.get(memory_id) or {}
                self.store.add_evidence(
                    bank_id=bank_id,
                    memory_id=memory_id,
                    text=hint.get("text"),
                    fact_type=hint.get("type"),
                    missing=True,
                )
                result.evidence_added += 1
                continue
            source_text = None
            chunk_id = fact.get("chunk_id")
            if chunk_id and self.settings.fetch_source_text:
                chunk = self.client.get_chunk(chunk_id) or {}
                source_text = (chunk.get("chunk_text") or "")[: self.settings.source_text_max_chars] or None
            self.store.add_evidence(
                bank_id=bank_id,
                memory_id=memory_id,
                text=fact.get("text"),
                fact_type=fact.get("type") or fact.get("fact_type"),
                document_id=fact.get("document_id"),
                chunk_id=chunk_id,
                mentioned_at=parse_ts(fact.get("mentioned_at")),
                source_text=source_text,
                tags=list(fact.get("tags") or []),
                entities=_entity_names(fact.get("entities")),
            )
            result.evidence_added += 1

    def _backfill_entities(self, bank_id: str) -> None:
        stale = self.store.evidence_without_entities(bank_id)
        if not stale:
            return
        listed = self.client.fact_entities(bank_id)
        # A fact Hindsight no longer lists gets none; everything else Lens holds about it stays.
        self.store.set_evidence_entities(bank_id, {i: _entity_names(listed.get(i)) for i in stale})

    # -- step 6: recalls ----------------------------------------------------

    def _apply_recall(self, bank_id: str, row: dict, result: SyncResult) -> datetime | None:
        started = parse_ts(row.get("started_at"))
        results = ((row.get("response") or {}).get("results")) or []
        hits, seen = [], set()
        for rank, item in enumerate(results, start=1):
            memory_id = str(item.get("id") or "")
            if not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            hits.append(
                {
                    "audit_id": str(row["id"]),
                    "memory_id": memory_id,
                    "bank_id": bank_id,
                    "rank": rank,
                    "score": (item.get("scores") or {}).get("final"),
                    "fact_type": item.get("type"),
                }
            )
        event = {
            "audit_id": str(row["id"]),
            "bank_id": bank_id,
            "started_at": started,
            "duration_ms": row.get("duration_ms"),
            "transport": row.get("transport"),
            "query": (row.get("request") or {}).get("query"),
            "query_timestamp": parse_ts((row.get("request") or {}).get("query_timestamp")),
            "result_count": len(results),
        }
        result.recalls_added += int(self.store.add_recall(event, hits))
        return started

    # -- step 7: portraits --------------------------------------------------

    def _collect_portraits(self, bank_id: str, result: SyncResult) -> None:
        for model in self.client.mental_models(bank_id):
            content = (model.get("content") or "").strip()
            if not content:
                continue
            added = self.store.add_portrait(
                bank_id=bank_id,
                mental_model_id=str(model.get("id")),
                name=model.get("name"),
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                refreshed_at=parse_ts(model.get("last_refreshed_at")),
            )
            result.portraits_added += int(added)


# -- helpers ------------------------------------------------------------------


def _entity_names(raw: Any) -> list[str]:
    """Hindsight sends a list for one memory and a comma-separated string in listings."""
    names = raw.split(",") if isinstance(raw, str) else raw or []
    return [n.strip() for n in names if isinstance(n, str) and n.strip()]


def _back(cursor: datetime | None, overlap: timedelta) -> datetime | None:
    return cursor - overlap if cursor else None


def _parse_trace(row: dict) -> _Trace | None:
    """Pull the creates/updates/deletes out of one consolidation LLM call.

    Returns None for a call that failed, was truncated past valid JSON, or is
    some other consolidation step (e.g. the near-duplicate check).
    """
    if row.get("status") not in (None, "success"):
        return None
    output = row.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return None
    if not isinstance(output, dict):
        return None
    creates, updates, deletes = (_dicts(output.get(k)) for k in ("creates", "updates", "deletes"))
    if not (creates or updates or deletes):
        return None
    meta = row.get("metadata") or {}
    return _Trace(
        request_id=str(row.get("id") or row.get("span_id") or row.get("trace_id") or ""),
        started_at=parse_ts(row.get("started_at")),
        ended_at=parse_ts(row.get("ended_at")) or parse_ts(row.get("started_at")),
        memory_ids={str(m) for m in meta.get("memory_ids") or []},
        source_memory_ids=sorted(str(m) for m in meta.get("source_memory_ids") or []),
        creates=creates,
        updates=updates,
        deletes=deletes,
    )


def _dicts(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _pick(versions: list[dict], *, text: str | None, after: datetime | None) -> dict | None:
    """Choose the version one LLM call produced.

    Hindsight writes a change moments after the call that decided it, so the
    version must sit just after ``after``: not before it (that was an earlier
    rewrite) and not long after (that is a later one, or a rebuilt bank reusing
    the same wording). When a version's own time is unknown, identical wording
    is the only acceptable tie.
    """
    candidates = []
    for version in versions:
        same = bool(text) and version["text"] == text
        timed = (
            after is not None and version.get("changed_at") is not None and not version.get("changed_at_is_estimate")
        )
        if timed:
            lag = version["changed_at"] - after
            if lag < -REASON_CLOCK_SLACK or lag > REASON_MATCH_WINDOW:
                continue
        elif not same:
            continue
        else:
            lag = timedelta(0)
        candidates.append((not same, abs(lag), version["id"], version))
    return min(candidates)[3] if candidates else None
