"""Command line: serve the collector, sync by hand, check setup, read the store."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from .app import build_collector, create_app
from .config import Settings
from .hindsight import HindsightError
from .timeutil import iso


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lens", description="Hindsight Lens collector")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the webhook receiver, worker and sweep")
    sync = sub.add_parser("sync", help="sync one bank, or every bank, right now")
    sync.add_argument("--bank", help="bank id (default: all banks)")
    sync.add_argument("--reconcile", action="store_true", help="force the full check for deleted beliefs")
    sub.add_parser("doctor", help="check Hindsight is reachable and configured for Lens")
    forget = sub.add_parser("forget", help="erase everything Lens holds for a bank Hindsight no longer has")
    forget.add_argument("bank")
    forget.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    inspect = sub.add_parser("inspect", help="print what the store holds for one bank")
    inspect.add_argument("--bank", required=True)
    demo = sub.add_parser("demo", help="serve the dashboard with the demo users; needs no Hindsight")
    demo.add_argument("--file", default="demo/dashboard-demo.html", help="built by demo/build.py")
    demo.add_argument("--port", type=int, default=8901)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    commands = {
        "serve": _serve,
        "sync": _sync,
        "doctor": _doctor,
        "forget": _forget,
        "inspect": _inspect,
        "demo": _demo,
    }
    return commands[args.command](settings, args)


def _serve(settings: Settings, _: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
    return 0


def _sync(settings: Settings, args: argparse.Namespace) -> int:
    collector = build_collector(settings)
    try:
        bank_ids = [args.bank] if args.bank else [b["bank_id"] for b in collector.client.list_banks()]
    except HindsightError as exc:
        print(f"cannot list banks: {exc}", file=sys.stderr)
        return 1
    failed = 0
    for bank_id in bank_ids:
        result = collector.sync_now(bank_id, reconcile=args.reconcile)
        failed += result is None
        print(bank_id, result.as_dict() if result else "FAILED")
    return 1 if failed else 0


def _doctor(settings: Settings, _: argparse.Namespace) -> int:
    collector = build_collector(settings)
    print(f"Hindsight: {settings.hindsight_url}")
    try:
        info = collector.client.version()
        banks = collector.client.list_banks()
    except HindsightError as exc:
        print(f"  FAIL  cannot reach Hindsight: {exc}")
        return 1
    features = info.get("features") or {}
    checks = [
        (True, f"reachable, API {info.get('api_version')}, {len(banks)} bank(s)"),
        (bool(features.get("observations")), "observations (beliefs) enabled"),
        (
            bool(features.get("audit_log")),
            "audit log on by default: needed for recall analytics "
            "(set HINDSIGHT_API_AUDIT_LOG_ENABLED=true, or enable it per bank)",
        ),
        (
            bool(features.get("llm_trace")),
            "LLM request tracing on: needed for change reasons (HINDSIGHT_API_LLM_TRACE_ENABLED=true)",
        ),
        (bool(settings.webhook_secret), "LENS_WEBHOOK_SECRET set: webhook deliveries are verified"),
        (bool(settings.password), "LENS_PASSWORD set: the dashboard opens with a login"),
    ]
    for ok, text in checks:
        print(f"  {'ok  ' if ok else 'WARN'}  {text}")
    print(
        "\nSet these on the Hindsight server (API and worker), then restart it:\n"
        "  HINDSIGHT_API_WEBHOOK_URL=http://<lens-host>:%d/hooks/hindsight\n"
        "  HINDSIGHT_API_WEBHOOK_SECRET=<same value as LENS_WEBHOOK_SECRET>\n"
        "  HINDSIGHT_API_WEBHOOK_EVENT_TYPES=consolidation.completed,retain.completed\n"
        "  HINDSIGHT_API_WEBHOOK_ALLOWED_HOSTS=<lens-host>      # private addresses are blocked without this\n"
        "  HINDSIGHT_API_AUDIT_LOG_ENABLED=true\n"
        "  HINDSIGHT_API_LLM_TRACE_RETENTION_DAYS=7             # default is 1" % settings.port
    )
    return 0 if all(ok for ok, _ in checks) else 2


def _forget(settings: Settings, args: argparse.Namespace) -> int:
    collector = build_collector(settings)
    try:
        held = collector.client.bank_exists(args.bank)
    except HindsightError as exc:
        print(
            f"cannot confirm Hindsight no longer has bank {args.bank!r}, so nothing was erased: {exc}", file=sys.stderr
        )
        return 1
    if held:
        print(
            f"Hindsight still holds bank {args.bank!r}. Delete it there first: "
            "Lens only erases what Hindsight has forgotten.",
            file=sys.stderr,
        )
        return 1
    if not args.yes:
        try:
            answer = input(f"Erase everything Lens holds for {args.bank!r}? This cannot be undone. [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing erased.")
            return 1
    counts = collector.store.erase_bank(args.bank, actor=f"cli:{getpass.getuser()}", trigger="lens forget")
    print(f"Erased bank {args.bank!r}:")
    for table, n in counts.items():
        print(f"  {table}: {n}")
    return 0


def _inspect(settings: Settings, args: argparse.Namespace) -> int:
    store = build_collector(settings).store
    bank = store.get_bank(args.bank)
    if not bank:
        print(f"no data for bank {args.bank!r}; run `lens sync --bank {args.bank}` first", file=sys.stderr)
        return 1
    portrait = store.latest_portrait(args.bank)
    print(f"bank {args.bank}  last synced {iso(bank['last_synced_at'])}")
    if portrait:
        print(f"portrait: {portrait['content']}")
    for belief in store.list_beliefs(args.bank):
        recalls = store.recalls_for_memory(args.bank, belief["observation_id"])
        print(
            f"\n[{belief['status']}] {belief['text']}\n"
            f"    id {belief['observation_id']}  evidence {belief['proof_count']}  recalled {len(recalls)}x"
        )
        for version in store.versions(args.bank, belief["observation_id"]):
            when = iso(version["changed_at"]) or "unknown"
            approx = " (approx)" if version["changed_at_is_estimate"] else ""
            print(f"    {version['change_type']:<14} {when}{approx}")
            print(f"        text: {version['text']}")
            print(f"        why:  {version['reason'] or 'not captured'}")
        for fact in store.get_evidence(args.bank, belief["source_memory_ids"]):
            print(f"    evidence [{fact['document_id']}] {fact['text']}")
            if fact["source_text"]:
                print(f"        from: {fact['source_text'][:200]!r}")
        for recall in recalls:
            print(
                f"    recalled {iso(recall['started_at'])} rank {recall['rank']}/{recall['result_count']}: {recall['query']!r}"
            )
    return 0


def _demo(_: Settings, args: argparse.Namespace) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    path = Path(args.file)
    if not path.is_file():
        print(
            f"{path} not found. Run this from the repository root, or build it: python demo/build.py",
            file=sys.stderr,
        )
        return 1
    page = path.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_: object) -> None:
            pass

    print(f"Simulated users dashboard: http://127.0.0.1:{args.port}/  (Ctrl+C to stop)")
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
