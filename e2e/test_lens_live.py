"""End to end against a real Hindsight server.

Runs inside Hindsight's own system-test harness (hindsight-system-tests), which
boots a real `hindsight-api` process with an embedded Postgres and a scripted
LLM. Nothing in Hindsight is mocked: the collector is driven only by the global
webhook Hindsight itself delivers, and reads only through the public HTTP API.

See e2e/README.md for how to run it.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
from hindsight_client import Hindsight
from hindsight_system_tests import start_hindsight_server, wait_until_settled
from hindsight_system_tests.payloads import (
    Consolidation,
    Observation,
    ObservationDelete,
    ObservationUpdate,
    extracted,
    fact,
    fact_ids_in,
)
from hindsight_system_tests.server import free_port

from lens.app import create_app
from lens.config import Settings

pytestmark = pytest.mark.asyncio

SECRET = "s3cret"
LOGIN = ("lens", "e2e")
OUT = os.environ.get("LENS_E2E_OUT")


@pytest.fixture(scope="session")
def lens_port() -> int:
    return free_port()


@pytest.fixture(scope="session")
def hindsight(stub_server, lens_port, tmp_path_factory):
    """Hindsight configured exactly as the README tells an operator to configure it."""
    server = start_hindsight_server(
        stub_url=stub_server.url,
        log_path=tmp_path_factory.mktemp("hindsight-lens") / "server.log",
        extra_env={
            "HINDSIGHT_API_WEBHOOK_URL": f"http://127.0.0.1:{lens_port}/hooks/hindsight",
            "HINDSIGHT_API_WEBHOOK_SECRET": SECRET,
            "HINDSIGHT_API_WEBHOOK_EVENT_TYPES": "consolidation.completed,retain.completed",
            "HINDSIGHT_API_AUDIT_LOG_ENABLED": "true",
        },
    )
    yield server
    server.stop()


@pytest.fixture(scope="session")
def lens(hindsight, lens_port, tmp_path_factory):
    """The collector, serving on a real port, with its own SQLite file."""
    db = tmp_path_factory.mktemp("lens") / "lens.db"
    settings = Settings(
        hindsight_url=hindsight.url,
        database_url=os.environ.get("LENS_E2E_DATABASE_URL") or f"sqlite:///{db}",
        webhook_secret=SECRET,
        password=LOGIN[1],
        sweep_interval_seconds=0,
        host="127.0.0.1",
        port=lens_port,
    )
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=lens_port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="lens")
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "collector did not start"
    yield app.state.collector
    server.should_exit = True
    thread.join(timeout=10)


async def _until(condition, *, timeout: float = 45.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


async def test_the_collector_keeps_what_hindsight_forgets(llm, hindsight, lens, lens_port):
    bank = f"systest-lens-{uuid.uuid4().hex[:8]}"
    store = lens.store
    client = Hindsight(base_url=hindsight.url)
    pings = {"n": 0}
    reasons_at_ping: list[dict] = []  # was each reason already there when the ping's own sync ran?

    async def round_(content: str, document_id: str, contains: str, extracted_fact: str, consolidate) -> None:
        """One user session: retain, let Hindsight consolidate, let the ping land."""
        llm.reset()
        llm.on_step("extract_facts", contains=contains).returns(
            extracted(fact(extracted_fact, who="Maya", entities=["Maya"]))
        )
        llm.on_step("consolidate").answers_with(consolidate)
        await client.aretain(bank_id=bank, content=content, document_id=document_id, tags=["user:maya"])
        await wait_until_settled(client, bank)
        pings["n"] += 1
        await _until(
            lambda: _consolidation_pings(store, bank) >= pings["n"] and lens.queue_depth() == 0,
            what=f"ping {pings['n']} to be received and synced",
        )
        reasons_at_ping.append(
            {
                "session": document_id,
                "versions": [
                    {"type": v["change_type"], "reason": v["reason"]}
                    for o in sorted(store.belief_ids(bank))
                    for v in store.versions(bank, o)
                ],
            }
        )

    try:
        # -- session 1: a belief is formed
        await round_(
            "Maya said she prefers reminders in the morning.",
            "session-1",
            "morning",
            "Maya prefers reminders in the morning",
            lambda req: Consolidation(
                creates=[
                    Observation(
                        text="Maya prefers morning reminders",
                        source_fact_ids=fact_ids_in(req.all_text),
                        reason="first mention of a reminder preference",
                    )
                ]
            ),
        )
        (a,) = store.belief_ids(bank)

        # -- session 2: contradicting evidence rewrites it
        await round_(
            "Maya asked to stop morning reminders and move them to 9pm.",
            "session-2",
            "9pm",
            "Maya asked to move reminders to 9pm",
            lambda req: Consolidation(
                updates=[
                    ObservationUpdate(
                        text="Maya used to prefer morning reminders but now wants them at 9pm",
                        observation_id=a,
                        source_fact_ids=fact_ids_in(req.all_text),
                        reason="preference changed",
                    )
                ]
            ),
        )

        # -- seconds later it is rewritten again: each rewrite must keep its own reason
        await round_(
            "Maya says the 9pm slot is working well.",
            "session-2b",
            "working well",
            "Maya says the 9pm reminders are working well",
            lambda req: Consolidation(
                updates=[
                    ObservationUpdate(
                        text="Maya moved her reminders from the morning to 9pm and says the 9pm slot works well",
                        observation_id=a,
                        source_fact_ids=fact_ids_in(req.all_text),
                        reason="confirmed the new time works",
                    )
                ]
            ),
        )

        # -- session 3: a second, unrelated belief
        await round_(
            "Maya is training for a 10k race.",
            "session-3",
            "10k",
            "Maya is training for a 10k race",
            lambda req: Consolidation(
                creates=[
                    Observation(
                        text="Maya is training for a 10k", source_fact_ids=fact_ids_in(req.all_text), reason="new facet"
                    )
                ]
            ),
        )
        (b,) = store.belief_ids(bank) - {a}

        # -- session 4: that belief is superseded, so Hindsight deletes it
        await round_(
            "Maya cancelled the 10k race.",
            "session-4",
            "cancelled",
            "Maya cancelled the 10k race",
            lambda req: Consolidation(
                creates=[
                    Observation(
                        text="Maya cancelled her 10k plans",
                        source_fact_ids=fact_ids_in(req.all_text),
                        reason="new claim",
                    )
                ],
                deletes=[ObservationDelete(observation_id=b, reason="no longer true")],
            ),
        )
        (c,) = store.belief_ids(bank) - {a, b}

        # -- the agent recalls; nothing pings for a recall, so the next sync picks it up
        await client.arecall(bank_id=bank, query="When does Maya want reminders?", types=["observation"])
        async with httpx.AsyncClient(base_url=hindsight.url) as http:
            await _until_async(http, bank)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{lens_port}", auth=LOGIN) as lens_http:
            assert (await lens_http.post(f"/sync/{bank}")).status_code == 200
            await _until(
                lambda: lens.queue_depth() == 0 and store.recalls_for_memory(bank, a), what="recall to be collected"
            )
            # Reasons are written by Hindsight just after the change they explain, so a
            # sync racing the ping can miss one. A later sync must pick it up.
            if any(v["reason"] is None for o in (a, b, c) for v in store.versions(bank, o)):
                await lens_http.post(f"/sync/{bank}")
                await _until(lambda: lens.queue_depth() == 0, what="follow-up sync")
            status = (await lens_http.get("/status")).json()

        # -- Hindsight itself has forgotten belief B entirely
        async with httpx.AsyncClient(base_url=hindsight.url) as http:
            gone = await http.get(f"/v1/default/banks/{bank}/memories/{b}/history")
        assert gone.status_code == 404

        # ===================== what Lens kept =====================
        va = store.versions(bank, a)
        assert [(v["change_type"], v["text"], v["reason"]) for v in va] == [
            ("created", "Maya prefers morning reminders", "first mention of a reminder preference"),
            ("updated", "Maya used to prefer morning reminders but now wants them at 9pm", "preference changed"),
            (
                "updated",
                "Maya moved her reminders from the morning to 9pm and says the 9pm slot works well",
                "confirmed the new time works",
            ),
        ]
        assert va[0]["changed_at"] is not None and va[0]["changed_at_is_estimate"] is False
        assert va[1]["evidence_count"] == 2 and len(va[1]["new_source_memory_ids"]) == 1
        belief_a = store.get_belief(bank, a)
        assert va[2]["evidence_count"] == 3
        assert belief_a["status"] == "live" and belief_a["proof_count"] == 3 and belief_a["tags"] == ["user:maya"]

        belief_b = store.get_belief(bank, b)
        assert belief_b["status"] == "deleted" and belief_b["text"] == "Maya is training for a 10k"
        vb = store.versions(bank, b)
        assert [(v["change_type"], v["reason"]) for v in vb] == [
            ("created", "new facet"),
            ("deleted", "no longer true"),
        ]
        assert vb[1]["changed_at_is_estimate"] is False, "the delete time came from Hindsight's own trace"

        vc = store.versions(bank, c)
        assert [(v["change_type"], v["text"], v["reason"]) for v in vc] == [
            ("created", "Maya cancelled her 10k plans", "new claim")
        ]

        facts = {f["document_id"]: f for f in store.get_evidence(bank, belief_a["source_memory_ids"])}
        assert set(facts) == {"session-1", "session-2", "session-2b"}
        assert facts["session-1"]["source_text"] == "Maya said she prefers reminders in the morning."
        assert facts["session-2"]["source_text"] == "Maya asked to stop morning reminders and move them to 9pm."
        assert facts["session-2"]["text"].startswith("Maya asked to move reminders to 9pm")

        (hit,) = store.recalls_for_memory(bank, a)
        assert hit["query"] == "When does Maya want reminders?" and hit["transport"] == "http"
        assert hit["result_count"] == 2 and hit["rank"] in (1, 2) and hit["score"] is not None
        assert vb[1]["trigger_memory_ids"], "the delete is linked to the facts that caused it"
        assert store.recalls_for_memory(bank, b) == [], "a deleted belief is not recalled"

        assert status["counts"]["beliefs_deleted"] >= 1 and status["queue_depth"] == 0
        assert status["seconds_since_last_ping"] is not None

        if OUT:
            dump = _dump(store, bank, status)
            dump["reasons_at_ping"] = reasons_at_ping
            Path(OUT).write_text(json.dumps(dump, indent=1, default=str))
    finally:
        try:
            await client.banks.delete_bank(bank)
        except Exception:
            pass
        await client.aclose()


async def _until_async(http: httpx.AsyncClient, bank: str) -> None:
    """Hindsight writes audit rows in the background; wait for the recall's row."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        rows = (await http.get(f"/v1/default/banks/{bank}/audit-logs", params={"action": "recall"})).json()
        if rows.get("total"):
            return
        await asyncio.sleep(0.25)
    raise AssertionError("Hindsight never wrote the recall's audit row")


def _consolidation_pings(store, bank: str) -> int:
    from sqlalchemy import and_, func, select

    from lens.store import webhook_events as w

    with store.engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(w).where(and_(w.c.bank_id == bank, w.c.event == "consolidation.completed"))
        ).scalar_one()


def _dump(store, bank: str, status: dict) -> dict:
    from sqlalchemy import select

    from lens.store import webhook_events as w

    with store.engine.connect() as conn:
        events = [
            {"event": r.event, "data": (r.payload or {}).get("data")}
            for r in conn.execute(select(w).where(w.c.bank_id == bank).order_by(w.c.id))
        ]
    beliefs = []
    for belief in store.list_beliefs(bank):
        beliefs.append(
            {
                "belief": belief,
                "versions": store.versions(bank, belief["observation_id"]),
                "evidence": store.get_evidence(bank, belief["source_memory_ids"]),
                "recalls": store.recalls_for_memory(bank, belief["observation_id"]),
            }
        )
    return {"status": status, "bank": store.get_bank(bank), "webhook_events": events, "beliefs": beliefs}
