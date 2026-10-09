"""The HTTP face of the collector: the webhook receiver and a status endpoint."""

from __future__ import annotations

import base64
import json
import logging
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib import resources
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse

from . import __version__, signatures
from .config import Settings
from .export import build_fleet, build_user
from .hindsight import HindsightClient
from .store import AccessAction, Store
from .sync import BankSyncer
from .timeutil import iso, parse_ts, utcnow
from .worker import Collector

logger = logging.getLogger("lens.app")


def build_collector(settings: Settings) -> Collector:
    store = Store(settings.database_url)
    store.init_schema()
    client = HindsightClient(
        settings.hindsight_url,
        settings.hindsight_api_key,
        timeout=settings.request_timeout_seconds,
        page_size=settings.page_size,
    )
    syncer = BankSyncer(client, store, settings)
    return Collector(client, store, syncer, sweep_interval_seconds=settings.sweep_interval_seconds)


_CHALLENGE = {"WWW-Authenticate": 'Basic realm="Hindsight Lens"'}


def _basic_credentials(header: str | None) -> tuple[str, str] | None:
    """The name and password from a Basic header, or None if it holds none.

    Parsed here rather than by FastAPI's HTTPBasic, which answers a malformed
    header itself (no realm, before the missing-password check) and reads only ASCII.
    """
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        name, sep, password = base64.b64decode(token.strip(), validate=True).decode("utf-8").partition(":")
    except ValueError:  # bad base64 or bad UTF-8
        return None
    return (name, password) if sep else None


Recorder = Callable[..., None]


def _recorder(store: Store) -> Recorder:
    """Write one access row for a request, or refuse it: no record, no data."""

    def record(request: Request, action: AccessAction, username: str | None, bank_id: str | None = None) -> None:
        forwarded = request.headers.get("x-forwarded-for")
        detail = {
            "source": request.client.host if request.client else None,
            # As the proxy in front of Lens reported it; only as trustworthy as that proxy.
            "forwarded_for": forwarded[:200] if forwarded else None,
            "method": request.method,
            "path": request.url.path[:500],
        }
        try:
            store.add_access(action=action, username=username, bank_id=bank_id, detail=detail)
        except Exception as exc:  # any failure to record must close the door, whatever its cause
            # Not logger.exception: SQLAlchemy's message quotes the bound values, the name tried among them.
            cause = getattr(exc, "orig", None) or type(exc).__name__
            logger.error("could not write the access record for %s %s: %s", request.method, detail["path"], cause)
            raise HTTPException(
                status_code=503, detail="the access record could not be written, so no data was returned"
            ) from exc

    return record


def _require_login(settings: Settings, record: Recorder):
    """A route dependency that lets a request through only with the configured login, and returns the name."""

    def require_login(request: Request) -> str:
        if not settings.password:
            raise HTTPException(status_code=503, detail="LENS_PASSWORD is not set: memory stays hidden until it is")
        given = _basic_credentials(request.headers.get("authorization"))
        # Compare both, always, so neither a wrong name nor a wrong password answers faster.
        ok = given is not None and (
            secrets.compare_digest(given[0].encode(), settings.username.encode())
            & secrets.compare_digest(given[1].encode(), settings.password.encode())
        )
        if not ok:
            # No credentials at all is the browser's first request before its prompt, not a sign-in.
            if given is not None:
                # The name tried, never the password, nor a password typed into the name field.
                typed_password = secrets.compare_digest(given[0].encode(), settings.password.encode())
                record(request, "denied", None if typed_password else given[0])
            raise HTTPException(status_code=401, detail="login required", headers=_CHALLENGE)
        return settings.username

    return Depends(require_login)


def create_app(settings: Settings, collector: Collector | None = None, *, sweep_on_start: bool = True) -> FastAPI:
    collector = collector or build_collector(settings)
    store = collector.store
    record = _recorder(store)
    login = _require_login(settings, record)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if not settings.webhook_secret:
            logger.warning("LENS_WEBHOOK_SECRET is not set: webhook deliveries are accepted unverified")
        if not settings.password:
            logger.warning("LENS_PASSWORD is not set: the dashboard, snapshot API and manual sync answer 503")
        collector.start(sweep_on_start=sweep_on_start)
        yield
        collector.stop()

    # No /docs, /redoc or /openapi.json: they would sit outside the login.
    app = FastAPI(
        title="Hindsight Lens collector",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.collector = collector
    # The dashboard is one ~440 KB file and a user's detail runs to hundreds of KB.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/hooks/hindsight")
    async def hindsight_webhook(request: Request) -> dict:
        """Receive a Hindsight event, record it, queue a sync, and return at once."""
        body = await request.body()  # verify the raw bytes, never re-serialised JSON
        if settings.webhook_secret and not signatures.verify(
            settings.webhook_secret, body, dict(request.headers), tolerance_seconds=settings.webhook_tolerance_seconds
        ):
            raise HTTPException(status_code=401, detail="bad or missing signature")
        try:
            payload = json.loads(body)
            event, bank_id = payload["event"], payload["bank_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="not a Hindsight event") from exc

        data = payload.get("data") or {}
        fresh = store.add_webhook_event(
            event=event,
            operation_id=str(payload.get("operation_id") or ""),
            bank_id=bank_id,
            status=payload.get("status"),
            occurred_at=parse_ts(payload.get("timestamp")),
            payload=payload,
        )
        store.touch_bank(bank_id, last_ping_at=utcnow())
        # A redelivery is still queued: the first attempt's sync may have failed.
        if event == "consolidation.completed":
            collector.enqueue(bank_id, reconcile=bool(data.get("observations_deleted")))
        return {"received": True, "duplicate": not fresh}

    @app.post("/sync/{bank_id}")
    def sync_bank(request: Request, bank_id: str, reconcile: bool = False, who: str = login) -> dict:
        """Queue a sync by hand."""
        _refuse_other_sites(request)
        record(request, "sync", who, bank_id)
        collector.enqueue(bank_id, reconcile=reconcile)
        return {"queued": bank_id}

    @app.get("/status")
    def status() -> dict:
        """What the Fleet screen's collector strip shows."""
        summary = store.summary()
        now = utcnow()

        def age(ts) -> float | None:
            return round((now - ts).total_seconds(), 1) if ts else None

        return {
            "version": __version__,
            "queue_depth": collector.queue_depth(),
            "last_ping_at": iso(summary["last_ping_at"]),
            "seconds_since_last_ping": age(summary["last_ping_at"]),
            "last_synced_at": iso(summary["last_synced_at"]),
            # The number to watch: if this lag passes Hindsight's trace retention
            # (one day by default), reasons for that gap are gone for good.
            "reasons_harvested_up_to": iso(summary["oldest_reasons_check_at"]),
            "reason_harvest_lag_seconds": age(summary["oldest_reasons_check_at"]),
            "last_sweep_at": iso(collector.last_sweep_at),
            # Whether, not why: the error text can quote Hindsight's reply, so it stays in the log.
            "last_sweep_failed": collector.last_sweep_error is not None,
            "last_sweep_warnings": len(collector.last_sweep_warnings),
            "counts": {k: v for k, v in summary.items() if not k.endswith("_at")},
        }

    def valid_basis(time_basis: str) -> str:
        """``event`` dates each change by the fact that caused it, for deployments that were backfilled or replayed."""
        if time_basis not in ("write", "event"):
            raise HTTPException(status_code=400, detail="time_basis must be 'write' or 'event'")
        return time_basis

    @app.get("/api/fleet")
    def fleet(request: Request, time_basis: str = "write", who: str = login) -> dict:
        """The Fleet screen: numbers, trends and each user's flags and counts. No memory text."""
        basis = valid_basis(time_basis)  # a 400 exposes nothing, so it is answered before recording
        record(request, "view_fleet", who)
        return build_fleet(store, time_basis=basis)

    @app.get("/api/users/{bank_id}")
    def user(request: Request, bank_id: str, time_basis: str = "write", who: str = login) -> dict:
        """One user in full: what the User and Belief screens show."""
        basis = valid_basis(time_basis)
        record(request, "view_user", who, bank_id)
        detail = build_user(store, bank_id, time_basis=basis)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Lens holds no memory for {bank_id!r}")
        return detail

    @app.get("/", response_class=HTMLResponse, dependencies=[login])
    def dashboard() -> str:
        """The Fleet, User and Belief screens. Holds no data: it fetches the fleet and users, each recorded."""
        # Built from web/ (`pnpm build`); one self-contained file. Served, it loads its data from the API.
        page = resources.files("lens").joinpath("dashboard.html").read_text(encoding="utf-8")
        return page.replace("__LENS_DATA__", "")

    return app


def _refuse_other_sites(request: Request) -> None:
    """A browser re-sends a remembered Basic login to any site's form; refuse posts another page started."""
    headers = request.headers
    origin = headers.get("origin")
    own = {headers.get("host"), headers.get("x-forwarded-host")}
    if headers.get("sec-fetch-site") in ("cross-site", "same-site") or (origin and urlsplit(origin).netloc not in own):
        raise HTTPException(status_code=403, detail="requests started by another site are refused")
