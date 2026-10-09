"""Replay the demo personas through a real Hindsight and a real Lens, then build nothing else.

Hindsight must already be running with a real model and with these settings:
webhooks to this script's Lens (default http://127.0.0.1:8910/hooks/hindsight,
secret "demo-secret"), the audit log on, LLM traces on. Lens runs here,
in-process, driven only by Hindsight's webhook, exactly as in production.

    python demo/replay.py --hindsight http://127.0.0.1:18889 --out demo/lens_demo.db
    python demo/build.py

Each session is retained as one timestamped transcript, document id
"<user>:conversation:<session>"; the persona's recall questions are asked before it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import uvicorn

from lens.app import create_app
from lens.config import Settings

HERE = Path(__file__).resolve().parent
PORTRAIT_QUERY = (
    "Reflect on everything known about this person: their name, age, family and relationships, work, "
    "daily routine, where they live, plans and preferences. Write a short profile an assistant would use."
)
TERMINAL = {"completed", "failed", "cancelled", "canceled", "succeeded", "success", "error", "skipped"}


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


async def settled(http: httpx.AsyncClient, bank: str, timeout: float = 900) -> bool:
    """No operation in flight for three polls in a row (consolidation is queued after retain finishes)."""
    deadline, quiet = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        r = await http.get(f"/v1/default/banks/{bank}/operations", params={"limit": 100})
        ops = r.json().get("operations", []) if r.status_code == 200 else []
        quiet = 0 if any(o.get("status") not in TERMINAL for o in ops) else quiet + 1
        if quiet >= 3:
            return True
        await asyncio.sleep(1.0)
    return False


async def replay(http: httpx.AsyncClient, persona: dict, problems: Counter) -> None:
    bank, name = persona["user_id"], persona["display_name"]
    await http.delete(f"/v1/default/banks/{bank}")  # leftovers from an earlier run; 404 is fine
    for n, s in enumerate(persona["sessions"]):
        started = datetime.fromisoformat(s["started_at"])
        for q in s.get("recalls") or []:  # the agent reads memory before it replies
            r = await http.post(
                f"/v1/default/banks/{bank}/memories/recall", json={"query": q, "query_timestamp": started.isoformat()}
            )
            if r.status_code != 200 and n:  # before the first retain the bank does not exist yet
                problems[f"recall {r.status_code}"] += 1
        lines = [
            f"[{(started + timedelta(seconds=45 * i)).isoformat()}] {name if m['role'] == 'user' else 'Assistant'}: "
            f"{m['text']}"
            for i, m in enumerate(s["messages"])
        ]
        item = {
            "content": "\n".join(lines),
            "timestamp": started.isoformat(),
            "context": "chat with a personal assistant",
            "document_id": f"{bank}:conversation:{s['id']}",
            "tags": ["conversation"],
        }
        r = await http.post(f"/v1/default/banks/{bank}/memories", json={"items": [item], "async": True})
        if r.status_code >= 300:
            problems[f"retain {r.status_code}"] += 1
            continue
        if not await settled(http, bank):
            problems["settle timeout"] += 1
        log(f"  {bank} {s['id']} ({n + 1}/{len(persona['sessions'])})")
    r = await http.post(
        f"/v1/default/banks/{bank}/mental-models", json={"name": "portrait", "source_query": PORTRAIT_QUERY}
    )
    if r.status_code >= 300:
        problems[f"portrait {r.status_code}"] += 1
    await settled(http, bank)
    log(f"== {bank} done")


async def run(args: argparse.Namespace) -> None:
    personas = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(args.personas).glob("*.json"))]
    db = Path(args.out)
    db.unlink(missing_ok=True)
    settings = Settings(
        hindsight_url=args.hindsight,
        database_url=f"sqlite:///{db.resolve()}",
        webhook_secret=args.secret,
        sweep_interval_seconds=0,
        host="127.0.0.1",
        port=args.lens_port,
    )
    app = create_app(settings, sweep_on_start=False)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="lens").start()
    while not server.started:
        time.sleep(0.05)
    collector = app.state.collector

    t0, problems = time.monotonic(), Counter()
    async with httpx.AsyncClient(base_url=args.hindsight, timeout=180) as http:
        log(f"replaying {len(personas)} personas in parallel")
        await asyncio.gather(*(replay(http, p, problems) for p in personas))
    await asyncio.sleep(3)  # audit rows are written in the background
    for p in personas:  # one closing sync per bank picks up recalls and portraits
        collector.enqueue(p["user_id"], reconcile=True)
    deadline = time.monotonic() + 600
    while collector.queue_depth() and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    server.should_exit = True
    log(f"done in {round(time.monotonic() - t0)}s, problems: {dict(problems) or 'none'}; wrote {db}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hindsight", default="http://127.0.0.1:18889")
    ap.add_argument("--personas", default=str(HERE / "personas"))
    ap.add_argument("--out", default=str(HERE / "lens_demo.db"))
    ap.add_argument("--lens-port", type=int, default=8910)
    ap.add_argument("--secret", default="demo-secret")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
