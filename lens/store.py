"""Lens's own durable copy of what Hindsight believed, and when.

Hindsight forgets three things this store keeps: a deleted observation (gone
with its history), the reason for a change (kept one day), and history past
the 50-entry cap. Works on SQLite and Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.types import TypeDecorator

from .timeutil import utcnow


class UTCDateTime(TypeDecorator):
    """Aware UTC datetimes on every backend (SQLite drops the offset on its own)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


metadata = MetaData()

# One row per memory bank: where each incremental pull left off.
banks = Table(
    "lens_banks",
    metadata,
    Column("bank_id", String(255), primary_key=True),
    Column("first_seen_at", UTCDateTime, nullable=False),
    Column("last_ping_at", UTCDateTime),
    Column("last_synced_at", UTCDateTime),
    Column("obs_cursor", UTCDateTime),
    Column("trace_cursor", UTCDateTime),
    Column("audit_cursor", UTCDateTime),
    # When the reasons step last succeeded: the clock that races Hindsight's trace retention.
    Column("reasons_checked_at", UTCDateTime),
    Column("last_error", Text),
    Column("last_result", JSON),
)

# The current state of every belief ever seen, including ones Hindsight deleted.
beliefs = Table(
    "lens_beliefs",
    metadata,
    Column("bank_id", String(255), primary_key=True),
    Column("observation_id", String(64), primary_key=True),
    Column("text", Text, nullable=False),
    Column("status", String(16), nullable=False),  # live | deleted | invalidated
    Column("proof_count", Integer),
    Column("source_memory_ids", JSON, nullable=False),
    Column("tags", JSON, nullable=False),
    Column("first_seen_at", UTCDateTime, nullable=False),
    Column("last_changed_at", UTCDateTime),
    Column("deleted_at", UTCDateTime),
    Column("hindsight_updated_at", UTCDateTime),
    Index("ix_lens_beliefs_bank_status", "bank_id", "status"),
)

# Every wording a belief has had. Append-only apart from filling in `reason`.
belief_versions = Table(
    "lens_belief_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("bank_id", String(255), nullable=False),
    Column("observation_id", String(64), nullable=False),
    # "created", "deleted", or the change's own timestamp: what makes re-syncing idempotent.
    Column("change_key", String(64), nullable=False),
    # created | earliest_known | updated | deleted | invalidated
    Column("change_type", String(16), nullable=False),
    Column("text", Text, nullable=False),
    Column("changed_at", UTCDateTime),
    Column("changed_at_is_estimate", Boolean, nullable=False, default=False),
    Column("evidence_count", Integer),
    Column("new_source_memory_ids", JSON, nullable=False),
    # The facts in the LLM call that caused this change. For a delete this is the
    # only link back to what the user said.
    Column("trigger_memory_ids", JSON, nullable=False),
    Column("reason", Text),
    # The Hindsight LLM call (llm-requests row) the reason was read from.
    Column("reason_request_id", String(64)),
    Column("collected_at", UTCDateTime, nullable=False),
    UniqueConstraint("bank_id", "observation_id", "change_key", name="uq_lens_version"),
    Index("ix_lens_versions_obs", "bank_id", "observation_id"),
)

# The facts beliefs rest on, with the original text each fact came from.
evidence = Table(
    "lens_evidence",
    metadata,
    Column("bank_id", String(255), primary_key=True),
    Column("memory_id", String(64), primary_key=True),
    Column("text", Text),
    Column("fact_type", String(32)),
    Column("document_id", String(512)),
    Column("chunk_id", String(1024)),
    Column("mentioned_at", UTCDateTime),
    Column("source_text", Text),
    Column("tags", JSON, nullable=False),
    Column("missing", Boolean, nullable=False, default=False),
    Column("collected_at", UTCDateTime, nullable=False),
    # What Hindsight extracted the fact to be about. NULL: collected before Lens kept these.
    Column("entities", JSON),
)

# One row per recall Hindsight served (from its audit log).
recall_events = Table(
    "lens_recall_events",
    metadata,
    Column("audit_id", String(64), primary_key=True),
    Column("bank_id", String(255), nullable=False),
    Column("started_at", UTCDateTime),
    Column("duration_ms", Integer),
    Column("transport", String(16)),
    Column("query", Text),
    # The moment the caller said the question was asked "as of", when it said so.
    Column("query_timestamp", UTCDateTime),
    Column("result_count", Integer, nullable=False),
    Index("ix_lens_recall_bank_time", "bank_id", "started_at"),
)

# Which memories each recall returned, in rank order.
recall_hits = Table(
    "lens_recall_hits",
    metadata,
    Column("audit_id", String(64), primary_key=True),
    Column("memory_id", String(64), primary_key=True),
    Column("bank_id", String(255), nullable=False),
    Column("rank", Integer, nullable=False),
    Column("score", Float),
    Column("fact_type", String(32)),
    Index("ix_lens_hits_memory", "bank_id", "memory_id"),
)

# Each wording a mental model (the "portrait") has had.
portraits = Table(
    "lens_portraits",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("bank_id", String(255), nullable=False),
    Column("mental_model_id", String(128), nullable=False),
    Column("name", Text),
    Column("content", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("refreshed_at", UTCDateTime),
    Column("collected_at", UTCDateTime, nullable=False),
    UniqueConstraint("bank_id", "mental_model_id", "content_hash", name="uq_lens_portrait"),
)

# Every webhook delivery, deduplicated (Hindsight delivers at least once).
webhook_events = Table(
    "lens_webhook_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event", String(64), nullable=False),
    Column("operation_id", String(128), nullable=False),
    Column("bank_id", String(255)),
    Column("status", String(32)),
    Column("occurred_at", UTCDateTime),
    Column("payload", JSON),
    Column("received_at", UTCDateTime, nullable=False),
    UniqueConstraint("event", "operation_id", name="uq_lens_webhook_event"),
)

# Who saw what: one row per fleet view, user view, manual sync and rejected sign-in,
# written before any data leaves. Append-only and kept indefinitely. Never holds
# memory text or passwords.
AccessAction = Literal["view_fleet", "view_user", "sync", "denied", "erase"]
access_log = Table(
    "lens_access_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", UTCDateTime, nullable=False),
    Column("action", String(16), nullable=False),  # see AccessAction
    Column("username", String(255)),  # the signed-in name; for denied, the name tried (if any)
    Column("bank_id", String(255)),  # the user exposed (view_user, sync) or erased (erase)
    Column("detail", JSON, nullable=False),  # source, forwarded-for, method, path; for erase, trigger and counts
    Index("ix_lens_access_log_bank_at", "bank_id", "at"),
)


# Everything Lens holds per bank, children before parents. The access record is not
# here: it outlives the data it describes.
_BANK_TABLES = (recall_hits, recall_events, belief_versions, evidence, beliefs, portraits, webhook_events, banks)


class Store:
    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_engine(database_url, future=True)

    def init_schema(self) -> None:
        metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Bring a store written by an older Lens up to date by adding the columns it lacks."""
        found = inspect(self.engine)
        # Postgres: two Lens processes starting at once must not both fail on the same column.
        guard = "IF NOT EXISTS " if self.engine.dialect.name == "postgresql" else ""
        with self.engine.begin() as conn:
            for table in metadata.sorted_tables:
                have = {c["name"] for c in found.get_columns(table.name)}
                for column in table.columns:
                    if column.name in have:
                        continue
                    if not column.nullable:
                        raise RuntimeError(
                            f"{table.name}.{column.name} is NOT NULL: it needs a migration, not ADD COLUMN"
                        )
                    kind = column.type.compile(dialect=self.engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {guard}"{column.name}" {kind}'))

    # -- generic helpers --------------------------------------------------

    def _insert_unique(self, table: Table, values: dict[str, Any]) -> bool:
        """Insert a row; return False (and change nothing) if it already exists."""
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(table).values(**values))
            return True
        except IntegrityError:
            return False

    def _rows(self, stmt) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt)]

    def _one(self, stmt) -> dict[str, Any] | None:
        rows = self._rows(stmt.limit(1))
        return rows[0] if rows else None

    # -- access record ----------------------------------------------------

    def add_access(
        self, *, action: AccessAction, username: str | None, bank_id: str | None, detail: dict[str, Any]
    ) -> None:
        """Append one access row. Raises if it cannot be written: the caller must then refuse."""
        values = {"username": username, "bank_id": bank_id}
        values = {k: v[:255] if v else v for k, v in values.items()}  # the column width; a request can send more
        with self.engine.begin() as conn:
            conn.execute(insert(access_log).values(at=utcnow(), action=action, detail=detail, **values))

    def erase_bank(self, bank_id: str, *, actor: str, trigger: str) -> dict[str, int]:
        """Delete every row Lens holds for a bank and record the erasure, in one transaction.

        Returns rows removed per table. The access record keeps the bank id and the
        counts, never memory text; if it cannot be written, nothing is erased.
        """
        with self.engine.begin() as conn:
            counts = {
                table.name.removeprefix("lens_"): conn.execute(delete(table).where(table.c.bank_id == bank_id)).rowcount
                for table in _BANK_TABLES
            }
            conn.execute(
                insert(access_log).values(
                    at=utcnow(),
                    action="erase",
                    username=actor[:255],
                    bank_id=bank_id[:255],
                    detail={"trigger": trigger, "counts": counts},
                )
            )
        return counts

    # -- banks ------------------------------------------------------------

    def get_bank(self, bank_id: str) -> dict[str, Any] | None:
        return self._one(select(banks).where(banks.c.bank_id == bank_id))

    def touch_bank(self, bank_id: str, **fields: Any) -> None:
        """Create the bank row if needed, then set ``fields`` on it."""
        self._insert_unique(banks, {"bank_id": bank_id, "first_seen_at": utcnow()})
        if fields:
            with self.engine.begin() as conn:
                conn.execute(update(banks).where(banks.c.bank_id == bank_id).values(**fields))

    def list_banks(self) -> list[dict[str, Any]]:
        return self._rows(select(banks).order_by(banks.c.bank_id))

    # -- beliefs ----------------------------------------------------------

    def get_belief(self, bank_id: str, observation_id: str) -> dict[str, Any] | None:
        return self._one(
            select(beliefs).where(and_(beliefs.c.bank_id == bank_id, beliefs.c.observation_id == observation_id))
        )

    def save_belief(self, bank_id: str, observation_id: str, **fields: Any) -> None:
        created = self._insert_unique(
            beliefs,
            {
                "bank_id": bank_id,
                "observation_id": observation_id,
                "first_seen_at": utcnow(),
                "source_memory_ids": [],
                "tags": [],
                "text": "",
                "status": "live",
                **fields,
            },
        )
        if not created and fields:
            with self.engine.begin() as conn:
                conn.execute(
                    update(beliefs)
                    .where(and_(beliefs.c.bank_id == bank_id, beliefs.c.observation_id == observation_id))
                    .values(**fields)
                )

    def belief_ids(self, bank_id: str, *, status: str | None = None) -> set[str]:
        stmt = select(beliefs.c.observation_id).where(beliefs.c.bank_id == bank_id)
        if status:
            stmt = stmt.where(beliefs.c.status == status)
        return {r["observation_id"] for r in self._rows(stmt)}

    def list_beliefs(self, bank_id: str) -> list[dict[str, Any]]:
        return self._rows(
            select(beliefs).where(beliefs.c.bank_id == bank_id).order_by(beliefs.c.last_changed_at.desc())
        )

    # -- versions ---------------------------------------------------------

    def add_version(self, **values: Any) -> bool:
        values.setdefault("collected_at", utcnow())
        values.setdefault("new_source_memory_ids", [])
        values.setdefault("trigger_memory_ids", [])
        values.setdefault("changed_at_is_estimate", False)
        return self._insert_unique(belief_versions, values)

    def versions(self, bank_id: str, observation_id: str) -> list[dict[str, Any]]:
        """All versions of one belief, oldest first."""
        rows = self._rows(
            select(belief_versions).where(
                and_(belief_versions.c.bank_id == bank_id, belief_versions.c.observation_id == observation_id)
            )
        )
        return sorted(rows, key=_version_order)

    def versions_without_reason(self, bank_id: str, change_types: tuple[str, ...]) -> list[dict[str, Any]]:
        return self._rows(
            select(belief_versions).where(
                and_(
                    belief_versions.c.bank_id == bank_id,
                    belief_versions.c.reason.is_(None),
                    belief_versions.c.change_type.in_(change_types),
                )
            )
        )

    def versions_by_request(self, bank_id: str, request_id: str) -> list[dict[str, Any]]:
        return self._rows(
            select(belief_versions).where(
                and_(belief_versions.c.bank_id == bank_id, belief_versions.c.reason_request_id == request_id)
            )
        )

    def update_version(self, version_id: int, **fields: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(belief_versions).where(belief_versions.c.id == version_id).values(**fields))

    # -- evidence ---------------------------------------------------------

    def known_evidence_ids(self, bank_id: str, memory_ids: list[str]) -> set[str]:
        if not memory_ids:
            return set()
        stmt = select(evidence.c.memory_id).where(
            and_(evidence.c.bank_id == bank_id, evidence.c.memory_id.in_(memory_ids))
        )
        return {r["memory_id"] for r in self._rows(stmt)}

    def evidence_without_entities(self, bank_id: str) -> list[str]:
        """Facts collected before Lens kept entities."""
        stmt = select(evidence.c.memory_id).where(and_(evidence.c.bank_id == bank_id, evidence.c.entities.is_(None)))
        return [r["memory_id"] for r in self._rows(stmt)]

    def set_evidence_entities(self, bank_id: str, entities: dict[str, list[str]]) -> None:
        """Fill in entities for facts already held, touching nothing else about them."""
        with self.engine.begin() as conn:
            for memory_id, names in entities.items():
                key = and_(evidence.c.bank_id == bank_id, evidence.c.memory_id == memory_id)
                conn.execute(update(evidence).where(key).values(entities=names))

    def add_evidence(self, **values: Any) -> bool:
        values.setdefault("collected_at", utcnow())
        values.setdefault("tags", [])
        values.setdefault("missing", False)
        return self._insert_unique(evidence, values)

    def get_evidence(self, bank_id: str, memory_ids: list[str]) -> list[dict[str, Any]]:
        if not memory_ids:
            return []
        return self._rows(
            select(evidence).where(and_(evidence.c.bank_id == bank_id, evidence.c.memory_id.in_(memory_ids)))
        )

    # -- recalls ----------------------------------------------------------

    def add_recall(self, event: dict[str, Any], hits: list[dict[str, Any]]) -> bool:
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(recall_events).values(**event))
                if hits:
                    conn.execute(insert(recall_hits), hits)
            return True
        except IntegrityError:
            return False

    def recalls_for_memory(self, bank_id: str, memory_id: str) -> list[dict[str, Any]]:
        stmt = (
            select(
                recall_events.c.audit_id,
                recall_events.c.started_at,
                recall_events.c.query_timestamp,
                recall_events.c.query,
                recall_events.c.transport,
                recall_events.c.result_count,
                recall_hits.c.rank,
                recall_hits.c.score,
            )
            .join(recall_hits, recall_hits.c.audit_id == recall_events.c.audit_id)
            .where(and_(recall_hits.c.bank_id == bank_id, recall_hits.c.memory_id == memory_id))
            .order_by(recall_events.c.started_at.desc())
        )
        return self._rows(stmt)

    # -- portraits and webhook events -------------------------------------

    def add_portrait(self, **values: Any) -> bool:
        values.setdefault("collected_at", utcnow())
        return self._insert_unique(portraits, values)

    def latest_portrait(self, bank_id: str) -> dict[str, Any] | None:
        return self._one(select(portraits).where(portraits.c.bank_id == bank_id).order_by(portraits.c.id.desc()))

    def add_webhook_event(self, **values: Any) -> bool:
        values.setdefault("received_at", utcnow())
        return self._insert_unique(webhook_events, values)

    # -- status -----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        with self.engine.connect() as conn:

            def count(table: Table, *where) -> int:
                stmt = select(func.count()).select_from(table)
                for clause in where:
                    stmt = stmt.where(clause)
                return int(conn.execute(stmt).scalar_one())

            return {
                "banks": count(banks),
                "beliefs_live": count(beliefs, beliefs.c.status == "live"),
                "beliefs_deleted": count(beliefs, beliefs.c.status == "deleted"),
                "versions": count(belief_versions),
                "versions_without_reason": count(belief_versions, belief_versions.c.reason.is_(None)),
                "evidence": count(evidence),
                "recalls": count(recall_events),
                "last_ping_at": conn.execute(select(func.max(webhook_events.c.received_at))).scalar_one(),
                "last_synced_at": conn.execute(select(func.max(banks.c.last_synced_at))).scalar_one(),
                "oldest_reasons_check_at": conn.execute(select(func.min(banks.c.reasons_checked_at))).scalar_one(),
            }


_TYPE_RANK = {"earliest_known": 0, "created": 0, "updated": 1, "invalidated": 2, "deleted": 3}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _version_order(row: dict[str, Any]) -> tuple:
    """Creation first, deletion last, updates between them in time order."""
    rank = _TYPE_RANK.get(row["change_type"], 1)
    return (rank, row["changed_at"] or _EPOCH, row["id"])
