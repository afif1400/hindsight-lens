"""Check a demo persona file against demo/BRIEF.md: python3 demo/check.py demo/personas/*.json"""

import json
import re
import sys
from datetime import datetime

START, END = datetime.fromisoformat("2026-08-24T00:00:00+00:00"), datetime.fromisoformat("2026-10-06T00:00:00+00:00")


def problems(p: dict) -> list[str]:
    out = []
    for key in ("user_id", "display_name", "profile", "sessions", "portrait"):
        if key not in p:
            out.append(f"missing {key}")
    if out:
        return out
    if not re.fullmatch(r"[a-z0-9-]+", p["user_id"]):
        out.append("user_id: lower-case letters, digits and hyphens only")
    if set(p["profile"]) != {"full_name", "about"}:
        out.append("profile: exactly full_name and about")
    if len(p["portrait"]) > 600:
        out.append("portrait over 600 characters")
    last = None
    for i, s in enumerate(p["sessions"]):
        sid = s.get("id")
        if sid != f"s{i + 1:02d}":
            out.append(f"session {i}: id should be s{i + 1:02d}")
        at = datetime.fromisoformat(s["started_at"])
        if at.tzinfo is None or not START <= at < END:
            out.append(f"{sid}: started_at needs an offset and a date from 2026-08-24 to 2026-10-05")
        if last and at <= last:
            out.append(f"{sid}: not after the previous session")
        last = at
        recalls = s.get("recalls")
        if not isinstance(recalls, list) or len(recalls) > 2 or (i == 0 and recalls):
            out.append(f"{sid}: recalls must be a list of 0-2 questions, empty for s01")
        msgs = s.get("messages") or []
        if not msgs or any(m.get("role") not in ("user", "assistant") for m in msgs):
            out.append(f"{sid}: messages need role user or assistant")
        if any(len(m.get("text", "")) > 300 for m in msgs):
            out.append(f"{sid}: a message is over 300 characters")
        total = sum(len(m.get("text", "")) for m in msgs)
        if total > 2200:
            out.append(f"{sid}: {total} characters of messages, over 2200")
    return out


def main() -> int:
    bad = 0
    for path in sys.argv[1:]:
        found = problems(json.load(open(path, encoding="utf-8")))
        for f in found:
            print(f"{path}: {f}")
        bad += bool(found)
    print("OK" if not bad else f"{bad} file(s) with problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
