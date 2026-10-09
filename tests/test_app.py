"""The webhook receiver: who it believes, and what a delivery sets in motion."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fake_hindsight import BANK
from fastapi.testclient import TestClient
from sqlalchemy import select

from lens import signatures
from lens.app import create_app
from lens.config import Settings
from lens.export import build_snapshot
from lens.store import access_log
from lens.sync import BankSyncer
from lens.worker import Collector

SECRET = "s3cret"
USER, PASSWORD = "lens", "open-sesame"
LOGIN = (USER, PASSWORD)
GUARDED = [("GET", "/"), ("GET", "/api/fleet"), ("GET", f"/api/users/{BANK}"), ("POST", f"/sync/{BANK}")]


def _sign_v2(body: bytes, *, secret: str = SECRET, at: float | None = None) -> dict[str, str]:
    t = int(at if at is not None else time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Hindsight-Signature-V2": f"t={t},v1={mac}", "X-Hindsight-Event": "consolidation.completed"}


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _event(deleted: int = 0, operation_id: str = "op-1") -> bytes:
    return json.dumps(
        {
            "event": "consolidation.completed",
            "bank_id": BANK,
            "operation_id": operation_id,
            "status": "completed",
            "timestamp": "2026-10-06T17:18:19.818869Z",
            "data": {"observations_created": 1, "observations_updated": 0, "observations_deleted": deleted},
        }
    ).encode()


@contextmanager
def _serve(client, store, settings):
    collector = Collector(client, store, BankSyncer(client, store, settings), sweep_interval_seconds=0)
    with TestClient(create_app(settings, collector)) as http:
        yield http, collector


@pytest.fixture
def app_client(client, store, settings):
    with _serve(client, store, replace(settings, webhook_secret=SECRET, password=PASSWORD)) as served:
        yield served


@pytest.mark.parametrize("method,path", GUARDED)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic !!!"}, {"Authorization": "Bearer abc"}, {"Authorization": _basic("lens", "guess")}],
    ids=["none", "malformed", "not-basic", "wrong"],
)
def test_guarded_routes_ask_for_a_login(app_client, method, path, headers):
    http, _ = app_client
    response = http.request(method, path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="Hindsight Lens"'


def test_a_password_outside_ascii_works(client, store, settings):
    with _serve(client, store, replace(settings, password="pässwörd")) as (http, _):
        assert http.get("/api/fleet", headers={"Authorization": _basic(USER, "pässwörd")}).status_code == 200


def test_a_wrong_name_and_a_wrong_password_look_the_same(app_client):
    http, _ = app_client
    wrong_name = http.get("/api/fleet", auth=("admin", PASSWORD))
    wrong_password = http.get("/api/fleet", auth=(USER, "guess"))
    assert wrong_name.status_code == wrong_password.status_code == 401
    assert wrong_name.json() == wrong_password.json()
    assert wrong_name.headers["www-authenticate"] == wrong_password.headers["www-authenticate"]


def test_the_username_defaults_to_lens(client, store, settings, monkeypatch):
    monkeypatch.delenv("LENS_USERNAME", raising=False)
    monkeypatch.setenv("LENS_PASSWORD", "from-env")
    settings = replace(Settings.from_env(), database_url=settings.database_url, sweep_interval_seconds=0)
    with _serve(client, store, settings) as (http, _):
        assert http.get("/api/fleet", auth=("lens", "from-env")).status_code == 200


def test_without_a_password_memory_stays_shut_but_collection_runs(client, store, settings, fake):
    f1 = fake.retain("s1", "raw", "fact")
    a = fake.create_observation("a belief", [f1])
    with _serve(client, store, replace(settings, webhook_secret=SECRET, password=None)) as (http, collector):
        for method, path in GUARDED:
            for headers in ({"Authorization": _basic(*LOGIN)}, {"Authorization": "Basic !!!"}):
                response = http.request(method, path, headers=headers)
                assert response.status_code == 503 and "LENS_PASSWORD" in response.json()["detail"]
        assert collector.sweep() == 1
        assert collector.wait_until_idle(10)
        assert store.get_belief(BANK, a)["text"] == "a belief"

        fake.tick(30)
        b = fake.create_observation("another belief", [f1])
        body = _event()
        assert http.post("/hooks/hindsight", content=body, headers=_sign_v2(body)).json()["received"] is True
        assert collector.wait_until_idle(10)
        assert store.get_belief(BANK, b)["text"] == "another belief"


def test_health_and_status_need_no_login_and_carry_no_memory_text(app_client, fake):
    http, collector = app_client
    f1 = fake.retain("s1", "Maya said she walks at dawn.", "Maya walks at dawn")
    fake.create_observation("Maya walks at dawn", [f1])
    body = _event()
    http.post("/hooks/hindsight", content=body, headers=_sign_v2(body))
    assert collector.wait_until_idle(10)

    assert http.get("/healthz").json() == {"ok": True}
    status = http.get("/status")
    assert status.status_code == 200 and "dawn" not in status.text.lower()
    assert status.json()["last_sweep_failed"] is False


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_no_api_docs_are_served(app_client, path):
    http, _ = app_client
    assert http.get(path, auth=LOGIN).status_code == 404


def test_manual_sync_runs_with_the_login(app_client):
    http, collector = app_client
    assert http.post(f"/sync/{BANK}", auth=LOGIN).json() == {"queued": BANK}
    assert collector.wait_until_idle(10)


def test_another_site_cannot_make_a_signed_in_browser_sync(app_client):
    http, _ = app_client
    response = http.post(f"/sync/{BANK}", auth=LOGIN, headers={"Origin": "https://elsewhere.example"})
    assert response.status_code == 403
    assert http.post(f"/sync/{BANK}", auth=LOGIN, headers={"Origin": "http://testserver"}).status_code == 200


def test_signatures():
    body = b'{"a":1}'
    assert signatures.verify(SECRET, body, _sign_v2(body))
    assert not signatures.verify("other", body, _sign_v2(body))
    assert not signatures.verify(SECRET, b'{"a":2}', _sign_v2(body)), "a changed body must not verify"
    assert not signatures.verify(SECRET, body, _sign_v2(body, at=time.time() - 3600)), "a replayed delivery is refused"
    legacy = {"X-Hindsight-Signature": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}
    assert not signatures.verify(SECRET, body, legacy), "an undated signature can be replayed forever"
    assert not signatures.verify(SECRET, body, {})


def test_an_unsigned_delivery_is_refused(app_client):
    http, _ = app_client
    assert http.post("/hooks/hindsight", content=_event()).status_code == 401


def test_a_ping_triggers_a_sync(app_client, fake, store):
    http, collector = app_client
    f1 = fake.retain(
        "session-1", "Maya said she prefers reminders in the morning.", "Maya prefers reminders in the morning"
    )
    a = fake.create_observation("Maya prefers morning reminders", [f1])
    body = _event()

    response = http.post("/hooks/hindsight", content=body, headers=_sign_v2(body))

    assert response.json() == {"received": True, "duplicate": False}
    assert collector.wait_until_idle(10)
    assert store.get_belief(BANK, a)["text"] == "Maya prefers morning reminders"
    assert store.get_bank(BANK)["last_ping_at"] is not None


def test_a_redelivery_is_recognised(app_client):
    http, collector = app_client
    body = _event()
    http.post("/hooks/hindsight", content=body, headers=_sign_v2(body))
    again = http.post("/hooks/hindsight", content=body, headers=_sign_v2(body))
    assert again.json()["duplicate"] is True
    assert collector.wait_until_idle(10)


def test_a_ping_reporting_deletes_forces_the_delete_check(app_client, fake, store):
    http, collector = app_client
    f1 = fake.retain("s1", "raw", "fact")
    b = fake.create_observation("Maya is training for a 10k", [f1])
    first = _event(operation_id="op-1")
    http.post("/hooks/hindsight", content=first, headers=_sign_v2(first))
    assert collector.wait_until_idle(10)
    fake.tick(30)
    fake.delete_observation(b, reason=None)
    fake.stats_override = 1  # a stale cached count must not hide the delete

    second = _event(deleted=1, operation_id="op-2")
    http.post("/hooks/hindsight", content=second, headers=_sign_v2(second))

    assert collector.wait_until_idle(10)
    assert store.get_belief(BANK, b)["status"] == "deleted"


def test_status_reports_what_the_fleet_strip_needs(app_client, fake):
    http, collector = app_client
    f1 = fake.retain("s1", "raw", "fact")
    fake.create_observation("a belief", [f1])
    body = _event()
    http.post("/hooks/hindsight", content=body, headers=_sign_v2(body))
    assert collector.wait_until_idle(10)

    status = http.get("/status").json()

    assert status["counts"]["beliefs_live"] == 1 and status["queue_depth"] == 0
    assert status["seconds_since_last_ping"] is not None
    assert status["reason_harvest_lag_seconds"] is not None


def _remember_maya(http, collector, fake) -> str:
    """One belief with a reason, its evidence and source text, a recall and a portrait, all synced."""
    f1 = fake.retain("s1", "Maya said she walks at dawn by the lake.", "Maya walks at dawn")
    a = fake.create_observation("Maya is an early walker", [f1], reason="first mention of walks")
    fake.recall("when does Maya exercise?", [a])
    fake.mental_models.append(
        {
            "id": "mm-1",
            "name": "User portrait",
            "content": "Maya loves the lake.",
            "last_refreshed_at": fake.clock.isoformat(),
        }
    )
    body = _event()
    http.post("/hooks/hindsight", content=body, headers=_sign_v2(body))
    assert collector.wait_until_idle(10)
    return a


MEMORY_TEXT = ["early walker", "dawn", "lake", "exercise", "first mention", "User portrait"]
SUMMARY_FIELDS = ["id", "flags", "counts", "first_activity_at", "last_activity_at", "last_recall_at", "last_synced_at"]


def test_the_fleet_summary_carries_no_memory_text(app_client, fake):
    http, collector = app_client
    _remember_maya(http, collector, fake)

    response = http.get("/api/fleet", auth=LOGIN)

    fleet = response.json()
    [user] = fleet["users"]
    assert sorted(user) == sorted(SUMMARY_FIELDS)
    assert user["id"] == BANK and user["counts"]["live"] == 1 and user["counts"]["recalls"] == 1
    assert fleet["fleet"]["now"]["users"] == 1 and len(fleet["fleet"]["series"]) == 8
    assert fleet["collector"]["beliefs_live"] == 1
    assert [t for t in MEMORY_TEXT if t.lower() in response.text.lower()] == []


def test_a_user_detail_carries_the_whole_user_and_agrees_with_the_fleet(app_client, fake, store):
    http, collector = app_client
    a = _remember_maya(http, collector, fake)

    detail = http.get(f"/api/users/{BANK}", auth=LOGIN).json()

    user = detail["user"]
    [belief] = user["beliefs"]
    assert belief["id"] == a and belief["text"] == "Maya is an early walker"
    assert belief["versions"][0]["reason"] == "first mention of walks"
    assert belief["recalls"][0]["query"] == "when does Maya exercise?" and belief["flags"] == ["new", "thin"]
    assert [f["text"] for f in user["facts"].values()] == ["Maya walks at dawn"]
    assert list(user["chunks"].values()) == ["Maya said she walks at dawn by the lake."]
    assert user["portrait"]["text"] == "Maya loves the lake." and user["timeline"]
    [summary] = http.get("/api/fleet", auth=LOGIN).json()["users"]
    assert {k: user[k] for k in SUMMARY_FIELDS} == summary
    assert detail["as_of"] and detail["time_basis"] == "write"
    [in_snapshot] = build_snapshot(store)["users"]  # what the offline demo embeds
    assert in_snapshot == user


def test_a_user_detail_holds_only_that_user(app_client, fake, store):
    http, collector = app_client
    _remember_maya(http, collector, fake)
    store.touch_bank("ravi")
    store.save_belief("ravi", "obs-ravi", text="Ravi skips breakfast")

    assert {u["id"] for u in http.get("/api/fleet", auth=LOGIN).json()["users"]} == {BANK, "ravi"}
    ravi = http.get("/api/users/ravi", auth=LOGIN)
    assert [b["text"] for b in ravi.json()["user"]["beliefs"]] == ["Ravi skips breakfast"]
    assert [t for t in MEMORY_TEXT if t.lower() in ravi.text.lower()] == []
    maya = http.get(f"/api/users/{BANK}", auth=LOGIN)
    assert "breakfast" not in maya.text


def test_a_user_lens_does_not_hold_is_not_found(app_client):
    http, _ = app_client
    response = http.get("/api/users/nobody", auth=LOGIN)
    assert response.status_code == 404 and "nobody" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/api/fleet", f"/api/users/{BANK}"])
def test_both_endpoints_take_a_time_basis(app_client, fake, path):
    http, collector = app_client
    _remember_maya(http, collector, fake)
    assert http.get(path, params={"time_basis": "event"}, auth=LOGIN).json()["time_basis"] == "event"
    assert http.get(path, params={"time_basis": "nonsense"}, auth=LOGIN).status_code == 400


def test_the_all_users_snapshot_route_is_gone(app_client):
    http, _ = app_client
    assert http.get("/api/snapshot", auth=LOGIN).status_code == 404


def test_the_dashboard_page_is_served(app_client):
    http, _ = app_client
    page = http.get("/", auth=LOGIN, headers={"Accept-Encoding": "gzip"})
    assert page.status_code == 200 and "Hindsight Lens" in page.text and "__LENS_DATA__" not in page.text
    assert page.text.startswith("<!doctype html>") and page.text.count("<!doctype html>") == 1
    assert page.headers["content-encoding"] == "gzip", "the one-file dashboard is large; it travels compressed"


# -- the access record ---------------------------------------------------------


def _access(store) -> list[dict]:
    with store.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(access_log).order_by(access_log.c.id))]


def test_every_view_and_sync_is_recorded(app_client, fake, store):
    http, collector = app_client
    _remember_maya(http, collector, fake)

    http.get("/", auth=LOGIN)  # the page itself holds no data
    http.get("/api/fleet", auth=LOGIN)
    http.get(f"/api/users/{BANK}", params={"time_basis": "event"}, auth=LOGIN)
    http.post(f"/sync/{BANK}", auth=LOGIN, headers={"X-Forwarded-For": "203.0.113.7"})
    assert collector.wait_until_idle(10)

    rows = _access(store)
    assert [(r["action"], r["username"], r["bank_id"]) for r in rows] == [
        ("view_fleet", USER, None),
        ("view_user", USER, BANK),
        ("sync", USER, BANK),
    ]
    assert rows[1]["detail"] == {
        "source": "testclient",
        "forwarded_for": None,
        "method": "GET",
        "path": f"/api/users/{BANK}",
    }
    assert rows[2]["detail"]["forwarded_for"] == "203.0.113.7"
    assert all(r["at"] is not None for r in rows)
    dump = repr(rows).lower()
    assert [t for t in MEMORY_TEXT if t.lower() in dump] == [] and PASSWORD not in dump


def test_a_rejected_sign_in_is_recorded_without_its_password(app_client, store):
    http, _ = app_client
    assert http.get("/").status_code == 401  # the browser's first, credential-less load: not a sign-in yet
    assert http.get("/api/fleet", auth=("mallory", "hunter2-guess")).status_code == 401
    assert http.get(f"/api/users/{BANK}", auth=(PASSWORD, "oops")).status_code == 401  # password in the name field

    rows = _access(store)
    assert [(r["action"], r["username"], r["bank_id"]) for r in rows] == [
        ("denied", "mallory", None),
        ("denied", None, None),
    ]
    assert PASSWORD not in repr(rows)
    assert rows[0]["detail"]["path"] == "/api/fleet" and rows[0]["detail"]["source"] == "testclient"
    assert "hunter2" not in repr(rows)


@pytest.mark.parametrize(
    "method,path,login",
    [
        ("GET", "/api/fleet", LOGIN),
        ("GET", f"/api/users/{BANK}", LOGIN),
        ("POST", f"/sync/{BANK}", LOGIN),
        ("GET", "/api/fleet", ("x", "y")),
    ],
    ids=["fleet", "user", "sync", "denied"],
)
def test_no_access_record_means_no_data(app_client, fake, store, method, path, login):
    http, collector = app_client
    _remember_maya(http, collector, fake)
    access_log.drop(store.engine)  # the record cannot be written

    response = http.request(method, path, auth=login)

    assert response.status_code == 503 and "access record" in response.json()["detail"]
    assert [t for t in MEMORY_TEXT if t.lower() in response.text.lower()] == []
    assert collector.queue_depth() == 0


def test_a_failed_access_record_does_not_log_the_name_tried(app_client, store, caplog):
    http, _ = app_client
    access_log.drop(store.engine)
    with caplog.at_level("DEBUG"):
        assert http.get("/api/fleet", auth=("N3arMissPassw0rd", "x")).status_code == 503
    assert "could not write the access record" in caplog.text
    assert "N3arMissPassw0rd" not in caplog.text


def test_an_access_row_stays_small_whatever_the_request_sends(app_client, store):
    http, _ = app_client
    http.get("/api/users/" + "a" * 5000, auth=("b" * 5000, "x"), headers={"X-Forwarded-For": "c" * 5000})
    [row] = _access(store)
    assert len(repr(row)) < 1500


# -- what the dashboard reads about a user ------------------------------------------


def test_a_user_detail_names_what_each_fact_is_about(app_client, fake):
    http, collector = app_client
    f1 = fake.retain("s1", "Maya said she walks at dawn.", "Maya walks at dawn", entities=["Maya", "walk"])
    fake.create_observation("Maya is an early walker", [f1])
    body = _event()
    http.post("/hooks/hindsight", content=body, headers=_sign_v2(body))
    assert collector.wait_until_idle(10)

    user = http.get(f"/api/users/{BANK}", auth=LOGIN).json()["user"]

    assert user["facts"][f1]["entities"] == ["Maya", "walk"]


def test_a_user_detail_lists_every_result_a_recall_returned(app_client, fake):
    http, collector = app_client
    f1 = fake.retain("s1", "Maya said she walks at dawn.", "Maya walks at dawn")
    a = fake.create_observation("Maya is an early walker", [f1])
    fake.recall("when does Maya exercise?", [a, f1])
    body = _event()
    http.post("/hooks/hindsight", content=body, headers=_sign_v2(body))
    assert collector.wait_until_idle(10)

    [recall] = http.get(f"/api/users/{BANK}", auth=LOGIN).json()["user"]["recalls"]

    assert recall["query"] == "when does Maya exercise?" and recall["id"]
    assert [(h["id"], h["rank"], h["type"]) for h in recall["hits"]] == [(a, 1, "observation"), (f1, 2, "world")]
