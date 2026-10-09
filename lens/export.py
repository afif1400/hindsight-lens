"""Turn the store into the JSON documents the dashboard renders.

Everything the Fleet, User and Belief screens show is computed here, from the
store alone, by one per-user computation. The fleet summary keeps each user's
flags and counts and drops their memory text; a user detail is one user in
full; the snapshot (offline demo only) is every user in full. v1 loads a whole
deployment into memory, which is fine for thousands of beliefs; past that,
these become SQL queries.

Two clocks are supported. ``write`` time is when Hindsight recorded a change.
``event`` time is when the user said the thing that caused it (the newest
supporting fact's ``mentioned_at``). They are the same in live use; they differ
after a backfill or replay, where weeks of conversation are retained in minutes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import select

from .store import Store, belief_versions, beliefs, evidence, portraits, recall_events, recall_hits, webhook_events
from .timeutil import iso, utcnow

STALE_DAYS = 30
RECENT_DAYS = 7
HIGH_CHURN = 4  # changes + deletes in the recent window


# What the fleet summary keeps of each user: flags, counts and dates, never memory text.
SUMMARY_FIELDS = ("id", "flags", "counts", "first_activity_at", "last_activity_at", "last_recall_at", "last_synced_at")


def build_fleet(store: Store, **options: Any) -> dict[str, Any]:
    """The Fleet screen: headline numbers, eight-week trends, the collector strip, a summary per user."""
    users, head = _users(store, **options)
    return _with_fleet(store, users, head, shown=[{k: u[k] for k in SUMMARY_FIELDS} for u in users])


def build_user(store: Store, bank_id: str, **options: Any) -> dict[str, Any] | None:
    """One user in full (beliefs, versions, evidence, recalls, timeline, portrait), or None if Lens lacks the bank."""
    users, head = _users(store, only_bank=bank_id, **options)
    return _jsonable({**head, "user": users[0]}) if users else None


def build_snapshot(store: Store, **options: Any) -> dict[str, Any]:
    """Every user in full plus the fleet numbers: the data an offline demo page embeds."""
    users, head = _users(store, **options)
    return _with_fleet(store, users, head, shown=users)


def _with_fleet(store: Store, users: list[dict], head: dict[str, Any], *, shown: list[dict]) -> dict[str, Any]:
    fleet = _fleet(users, head["as_of"], head["thresholds"])
    return _jsonable({**head, "collector": store.summary(), "fleet": fleet, "users": shown})


def _users(
    store: Store,
    *,
    only_bank: str | None = None,
    time_basis: str = "write",
    now: datetime | None = None,
    stale_days: int = STALE_DAYS,
    recent_days: int = RECENT_DAYS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Each user's full object (only ``only_bank``'s, if given), most worth a look first, and the shared header."""
    if time_basis not in ("write", "event"):
        raise ValueError("time_basis must be 'write' or 'event'")
    now = now or utcnow()
    recent = now - timedelta(days=recent_days)
    stale_before = now - timedelta(days=stale_days)

    rows = _load(store, only_bank)
    users = []
    for bank in rows["banks"]:
        bank_id = bank["bank_id"]
        facts = {e["memory_id"]: e for e in rows["evidence"].get(bank_id, [])}
        recalls = rows["recalls"].get(bank_id, [])
        hits = rows["hits"].get(bank_id, [])
        hits_by_memory: dict[str, list[dict]] = defaultdict(list)
        recall_by_id = {r["audit_id"]: r for r in recalls}
        for hit in hits:
            hits_by_memory[hit["memory_id"]].append(hit)

        def recall_time(r: dict) -> datetime | None:
            return (r["query_timestamp"] if time_basis == "event" else None) or r["started_at"]

        def fact_times(ids: list[str]) -> list[datetime]:
            return [facts[i]["mentioned_at"] for i in ids if i in facts and facts[i]["mentioned_at"]]

        belief_out = []
        for belief in rows["beliefs"].get(bank_id, []):
            obs_id = belief["observation_id"]
            sources = list(belief["source_memory_ids"] or [])
            versions = []
            for v in rows["versions"].get((bank_id, obs_id), []):
                at, estimate = v["changed_at"], v["changed_at_is_estimate"]
                if time_basis == "event":
                    cause = fact_times(v["new_source_memory_ids"] or []) or fact_times(v["trigger_memory_ids"] or [])
                    if v["change_type"] == "earliest_known" and not cause:
                        cause = fact_times(sources)[:1] and [min(fact_times(sources))]
                    if cause:
                        at, estimate = max(cause), False
                versions.append(
                    {
                        "type": v["change_type"],
                        "text": v["text"],
                        "at": at,
                        "at_is_estimate": bool(estimate),
                        "reason": v["reason"],
                        "evidence_count": v["evidence_count"],
                        "new_evidence": list(v["new_source_memory_ids"] or []),
                        "triggered_by": list(v["trigger_memory_ids"] or []),
                    }
                )
            versions.sort(key=lambda v: (_RANK.get(v["type"], 1), v["at"] or _EPOCH))
            for n, v in enumerate(versions, start=1):
                v["n"] = n

            created_at = next((v["at"] for v in versions if v["at"]), None)
            deleted_at = next((v["at"] for v in versions if v["type"] in ("deleted", "invalidated")), None)
            update_times = [v["at"] for v in versions if v["type"] == "updated" and v["at"]]
            evidence_times = sorted(fact_times(sources))
            live = belief["status"] == "live"
            belief_recalls = sorted(
                (
                    {
                        "at": recall_time(recall_by_id[h["audit_id"]]),
                        "query": recall_by_id[h["audit_id"]]["query"],
                        "rank": h["rank"],
                        "of": recall_by_id[h["audit_id"]]["result_count"],
                        "score": h["score"],
                        "transport": recall_by_id[h["audit_id"]]["transport"],
                    }
                    for h in hits_by_memory.get(obs_id, [])
                    if h["audit_id"] in recall_by_id
                ),
                key=lambda r: r["at"] or _EPOCH,
                reverse=True,
            )
            evidence_count = belief["proof_count"] if belief["proof_count"] is not None else len(sources)
            flags = []
            if not live:
                flags.append("deleted")
            else:
                if created_at and created_at >= recent:
                    flags.append("new")
                if any(t >= recent for t in update_times):
                    flags.append("changed")
                if evidence_count <= 1:
                    flags.append("thin")
                if evidence_times and evidence_times[-1] < stale_before:
                    flags.append("stale")
                if not belief_recalls:
                    flags.append("never_recalled")
            belief_out.append(
                {
                    "id": obs_id,
                    "text": belief["text"],
                    "status": belief["status"],
                    "flags": flags,
                    "evidence_count": evidence_count,
                    "tags": belief["tags"],
                    "created_at": created_at,
                    "last_changed_at": max([t for t in [created_at, deleted_at, *update_times] if t], default=None),
                    "deleted_at": deleted_at,
                    "newest_evidence_at": evidence_times[-1] if evidence_times else None,
                    "evidence_times": evidence_times,
                    "update_times": update_times,
                    "versions": versions,
                    "evidence": sources,
                    "recalls": belief_recalls,
                }
            )
        belief_out.sort(key=lambda b: b["last_changed_at"] or _EPOCH, reverse=True)

        live_beliefs = [b for b in belief_out if b["status"] == "live"]
        hits_by_recall: dict[str, list[dict]] = defaultdict(list)
        for hit in hits:
            hits_by_recall[hit["audit_id"]].append(
                {"id": hit["memory_id"], "rank": hit["rank"], "score": hit["score"], "type": hit["fact_type"]}
            )
        recall_rows = sorted(
            (
                {
                    "id": r["audit_id"],
                    "at": recall_time(r),
                    "query": r["query"],
                    "results": r["result_count"],
                    "transport": r["transport"],
                    # Every result in rank order: beliefs, facts, and ids Lens never collected.
                    "hits": sorted(hits_by_recall.get(r["audit_id"], []), key=lambda h: h["rank"]),
                }
                for r in recalls
            ),
            key=lambda r: r["at"] or _EPOCH,
            reverse=True,
        )
        retains = rows["retains"].get(bank_id, [])
        changed_recent = sum(1 for b in belief_out for t in b["update_times"] if t >= recent)
        deleted_recent = sum(1 for b in belief_out if b["deleted_at"] and b["deleted_at"] >= recent)
        activity = [b["last_changed_at"] for b in belief_out if b["last_changed_at"]]
        first_activity = min((b["created_at"] for b in belief_out if b["created_at"]), default=None)
        counts = {
            "live": len(live_beliefs),
            "deleted": len(belief_out) - len(live_beliefs),
            "thin": sum("thin" in b["flags"] for b in live_beliefs),
            "stale": sum("stale" in b["flags"] for b in live_beliefs),
            "never_recalled": sum("never_recalled" in b["flags"] for b in live_beliefs),
            "changed_recent": changed_recent,
            "deleted_recent": deleted_recent,
            "facts": len(facts),
            "sessions": len(
                {r["document_id"] for r in retains if r["document_id"]}
                | {f["document_id"] for f in facts.values() if f["document_id"]}
            ),
            "sessions_without_facts": sum(1 for r in retains if r["memory_unit_count"] == 0),
            "recalls": len(recall_rows),
            "empty_recalls": sum(1 for r in recall_rows if not r["results"]),
        }
        user_flags = []
        is_new = bool(first_activity and first_activity >= recent)
        if is_new:
            user_flags.append("new")
        # A new user's first week is all change; many changes only means something after it.
        if changed_recent + deleted_recent >= HIGH_CHURN and not is_new:
            user_flags.append("high_churn")
        elif changed_recent + deleted_recent:
            user_flags.append("changed")
        if live_beliefs and counts["thin"] * 2 >= len(live_beliefs):
            user_flags.append("thin")
        if live_beliefs and counts["stale"] * 2 >= len(live_beliefs):
            user_flags.append("stale")
        if counts["recalls"] >= 3 and counts["empty_recalls"] * 2 >= counts["recalls"]:
            user_flags.append("empty_recalls")

        timeline = [
            {"at": v["at"], "kind": v["type"], "belief": b["id"], "text": v["text"], "reason": v["reason"]}
            for b in belief_out
            for v in b["versions"]
        ] + [{"at": r["at"], "kind": "recall", "text": r["query"], "results": r["results"]} for r in recall_rows]
        timeline.sort(key=lambda e: e["at"] or _EPOCH, reverse=True)

        chunks: dict[str, str] = {}
        fact_out = {}
        for memory_id, fact in facts.items():
            if fact["chunk_id"] and fact["source_text"]:
                chunks.setdefault(fact["chunk_id"], fact["source_text"])
            fact_out[memory_id] = {
                "text": fact["text"],
                "type": fact["fact_type"],
                "document": fact["document_id"],
                "chunk": fact["chunk_id"] if fact["chunk_id"] in chunks else None,
                "at": fact["mentioned_at"],
                "missing": bool(fact["missing"]),
                "entities": fact["entities"] or [],
            }
        portrait = rows["portraits"].get(bank_id)
        users.append(
            {
                "id": bank_id,
                "flags": user_flags,
                "counts": counts,
                "first_activity_at": first_activity,
                "last_activity_at": max(activity, default=None),
                "last_recall_at": recall_rows[0]["at"] if recall_rows else None,
                "last_synced_at": bank["last_synced_at"],
                "portrait": {
                    "text": portrait["content"],
                    "name": portrait["name"],
                    "refreshed_at": portrait["refreshed_at"],
                }
                if portrait
                else None,
                "beliefs": belief_out,
                "facts": fact_out,
                "chunks": chunks,
                "recalls": recall_rows,
                "timeline": timeline,
            }
        )

    users.sort(key=_attention, reverse=True)
    head = {
        "generated_at": utcnow(),
        "as_of": now,
        "time_basis": time_basis,
        "thresholds": {"stale_days": stale_days, "recent_days": recent_days, "high_churn": HIGH_CHURN},
    }
    return users, head


# -- fleet --------------------------------------------------------------------


def _fleet(users: list[dict], as_of: datetime, thresholds: dict[str, int]) -> dict[str, Any]:
    """Headline numbers, plus the same numbers at the end of each of the last eight weeks."""
    weeks = [as_of - timedelta(days=7 * k) for k in range(7, -1, -1)]
    series = [_fleet_at(users, t, thresholds["stale_days"], thresholds["recent_days"]) for t in weeks]
    return {"now": series[-1], "series": series}


def _fleet_at(users: list[dict], t: datetime, stale_days: int, window_days: int) -> dict[str, Any]:
    start = t - timedelta(days=window_days)
    stale_before = t - timedelta(days=stale_days)
    per_user_live, live_total, thin, stale, churn, recalls, empty = [], 0, 0, 0, 0, 0, 0
    for user in users:
        live = 0
        for b in user["beliefs"]:
            churn += sum(1 for u in b["update_times"] if start < u <= t)
            if b["deleted_at"] and start < b["deleted_at"] <= t:
                churn += 1
            if not b["created_at"] or b["created_at"] > t or (b["deleted_at"] and b["deleted_at"] <= t):
                continue
            live += 1
            seen = [e for e in b["evidence_times"] if e <= t]
            thin += len(seen) <= 1
            stale += bool(seen) and seen[-1] < stale_before
        for r in user["recalls"]:
            if r["at"] and start < r["at"] <= t:
                recalls += 1
                empty += not r["results"]
        if live:
            per_user_live.append(live)
            live_total += live
    n = len(per_user_live)
    return {
        "at": t,
        "users": n,
        "beliefs_per_user": median(per_user_live) if per_user_live else 0,
        "thin_pct": _pct(thin, live_total),
        "stale_pct": _pct(stale, live_total),
        "churn_per_user": round(churn / n, 2) if n else 0,
        "empty_recall_pct": _pct(empty, recalls),
        "recalls": recalls,
        "live_beliefs": live_total,
    }


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _attention(user: dict) -> tuple:
    """Fleet order: the users most worth a look first."""
    c = user["counts"]
    return (len(user["flags"]), c["changed_recent"] + c["deleted_recent"], c["stale"] + c["thin"], c["live"])


# -- loading ------------------------------------------------------------------


def _load(store: Store, bank_id: str | None = None) -> dict[str, Any]:
    """Every row the computation needs, for all banks or just ``bank_id``."""

    def rows_of(table, *where):
        stmt = select(table).where(*where)
        return stmt.where(table.c.bank_id == bank_id) if bank_id is not None else stmt

    def grouped(table, key) -> dict:
        out: dict = defaultdict(list)
        with store.engine.connect() as conn:
            for row in conn.execute(rows_of(table)):
                d = dict(row._mapping)
                out[key(d)].append(d)
        return out

    if bank_id is None:
        banks = store.list_banks()
    else:
        bank = store.get_bank(bank_id)
        banks = [bank] if bank else []

    latest_portrait: dict[str, dict] = {}
    with store.engine.connect() as conn:
        for row in conn.execute(rows_of(portraits).order_by(portraits.c.id)):
            latest_portrait[row.bank_id] = dict(row._mapping)
        retains: dict[str, list[dict]] = defaultdict(list)
        for row in conn.execute(rows_of(webhook_events, webhook_events.c.event == "retain.completed")):
            data = (row.payload or {}).get("data") or {}
            retains[row.bank_id].append(
                {"document_id": data.get("document_id"), "memory_unit_count": data.get("memory_unit_count")}
            )
    return {
        "banks": banks,
        "beliefs": grouped(beliefs, lambda d: d["bank_id"]),
        "versions": grouped(belief_versions, lambda d: (d["bank_id"], d["observation_id"])),
        "evidence": grouped(evidence, lambda d: d["bank_id"]),
        "recalls": grouped(recall_events, lambda d: d["bank_id"]),
        "hits": grouped(recall_hits, lambda d: d["bank_id"]),
        "portraits": latest_portrait,
        "retains": retains,
    }


_RANK = {"earliest_known": 0, "created": 0, "updated": 1, "invalidated": 2, "deleted": 3}
_EPOCH = datetime.fromisoformat("1970-01-01T00:00:00+00:00")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
