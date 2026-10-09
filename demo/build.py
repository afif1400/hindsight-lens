"""Build the self-contained demo dashboard from a replay's store and the persona files.

    python demo/build.py --db demo/lens_demo.db

Writes demo/dashboard-demo.html: open it in a browser, or run `lens demo`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from lens.export import build_snapshot
from lens.store import Store

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(HERE / "lens_demo.db"))
    ap.add_argument("--personas", default=str(HERE / "personas"))
    ap.add_argument("--as-of", default="2026-10-06T18:00:00+00:00", help="the 'now' the flags are computed against")
    ap.add_argument("--out", default=str(HERE / "dashboard-demo.html"))
    args = ap.parse_args()

    # Event time: the replay retained six weeks of conversation in under an hour.
    # A copy, brought up to the current schema, so the committed store is never rewritten.
    with tempfile.TemporaryDirectory() as tmp:
        db = shutil.copy(args.db, Path(tmp) / "lens.db")
        store = Store(f"sqlite:///{db}")
        store.init_schema()
        snapshot = build_snapshot(store, time_basis="event", now=datetime.fromisoformat(args.as_of))
        store.engine.dispose()
    sim = {}
    for path in sorted(Path(args.personas).glob("*.json")):
        p = json.loads(path.read_text(encoding="utf-8"))
        sim[p["user_id"]] = {
            "display_name": p["display_name"],
            "profile": p["profile"],
            "sessions": [
                {"id": s["id"], "started_at": s["started_at"], "messages": s["messages"]} for s in p["sessions"]
            ],
        }
    payload = json.dumps({"snapshot": snapshot, "sim": sim}, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("<!--", "\\u003c!--")  # safe inside a <script> element
    page = (HERE.parent / "lens" / "dashboard.html").read_text(encoding="utf-8")
    # A function, so backslashes in the JSON are not read as replacement escapes.
    Path(args.out).write_text(re.sub("__LENS_DATA__", lambda _: payload, page, count=1), encoding="utf-8")
    print(
        f"wrote {args.out}: {len(snapshot['users'])} users, {sum(len(u['beliefs']) for u in snapshot['users'])} beliefs"
    )


if __name__ == "__main__":
    main()
