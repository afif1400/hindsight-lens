"""Erasure: when Hindsight forgets a user, Lens forgets them too, and only then."""

from __future__ import annotations

import pytest
from fake_hindsight import BANK
from sqlalchemy import func, select
from test_app import LOGIN, MEMORY_TEXT, _access, _event, _remember_maya, _sign_v2, app_client  # noqa: F401

from lens import cli
from lens.store import _BANK_TABLES, access_log

ERASED_TABLES = {
    "banks",
    "beliefs",
    "belief_versions",
    "evidence",
    "recall_events",
    "recall_hits",
    "portraits",
    "webhook_events",
}


@pytest.fixture
def maya(app_client, fake, store):  # noqa: F811 (the fixture imported from test_app)
    """Maya with a live belief, a deleted one (its tombstone), evidence, source text, a recall and a portrait."""
    http, collector = app_client
    fake.other_banks = ["ravi"]  # a second user, so Maya is one bank of two
    _remember_maya(http, collector, fake)
    f2 = fake.retain("s2", "Maya said she quit sugar.", "Maya quit sugar")
    gone = fake.create_observation("Maya avoids sugar", [f2])
    first = _event(operation_id="op-1b")
    http.post("/hooks/hindsight", content=first, headers=_sign_v2(first))
    assert collector.wait_until_idle(10)
    fake.tick(30)
    fake.delete_observation(gone, reason="she started again")
    body = _event(deleted=1, operation_id="op-2")
    http.post("/hooks/hindsight", content=body, headers=_sign_v2(body))
    assert collector.wait_until_idle(10)
    assert store.get_belief(BANK, gone)["status"] == "deleted", "the tombstone is kept while the bank lives"
    return http, collector


def _erasures(store) -> list[dict]:
    return [r for r in _access(store) if r["action"] == "erase"]


def _held(http) -> int:
    return http.get(f"/api/users/{BANK}", auth=LOGIN).status_code


def _rows_left(store) -> dict[str, int]:
    with store.engine.connect() as conn:
        return {
            t.name: conn.execute(select(func.count()).select_from(t).where(t.c.bank_id == BANK)).scalar_one()
            for t in _BANK_TABLES
        }


def test_the_sweep_erases_a_bank_hindsight_deleted(maya, fake, store):
    http, collector = maya
    collector.sweep()
    assert collector.wait_until_idle(10)
    assert _held(http) == 200 and _erasures(store) == [], "a bank Hindsight still lists is never erased"
    before = _rows_left(store)
    assert all(before.values()), before

    fake.delete_bank()
    collector.sweep()

    assert _held(http) == 404
    assert BANK not in {u["id"] for u in http.get("/api/fleet", auth=LOGIN).json()["users"]}
    assert set(_rows_left(store).values()) == {0}
    [erase] = _erasures(store)
    assert erase["username"] == "system" and erase["bank_id"] == BANK
    counts = erase["detail"]["counts"]
    assert counts == {t.removeprefix("lens_"): n for t, n in before.items()}
    assert set(counts) == ERASED_TABLES and counts["beliefs"] == 2  # the live belief and the tombstone
    assert "sweep" in erase["detail"]["trigger"]
    assert [t for t in MEMORY_TEXT + ["sugar"] if t.lower() in repr(erase).lower()] == []


@pytest.mark.parametrize(
    "trouble",
    ["listing fails", "listing times out", "listing is partial", "check fails", "check times out"],
)
def test_the_sweep_erases_nothing_it_cannot_confirm(maya, fake, store, trouble):
    http, collector = maya
    fake.delete_bank()
    if trouble == "listing fails":
        fake.fail_paths.add("/v1/default/banks")
    elif trouble == "listing times out":
        fake.timeout_paths.add("/v1/default/banks")
    elif trouble == "listing is partial":
        fake.short_listing = True
    elif trouble == "check fails":
        fake.fail_paths.add(f"{BANK}/stats")
    else:
        fake.timeout_paths.add(f"{BANK}/stats")

    collector.sweep()

    assert _held(http) == 200 and _erasures(store) == []
    status = http.get("/status").json()
    if trouble.startswith("listing"):
        assert status["last_sweep_failed"] is True
    else:
        assert status["last_sweep_failed"] is False and status["last_sweep_warnings"] == 1


def test_a_not_found_that_is_not_hindsights_erases_nothing(maya, fake, store):
    http, collector = maya
    store.touch_bank("asha")  # held by Lens, missing from the listing, and the fake answers a bare 404 for it
    store.save_belief("asha", "obs-asha", text="Asha skips breakfast")

    collector.sweep()
    assert collector.wait_until_idle(10)

    assert http.get("/api/users/asha", auth=LOGIN).status_code == 200 and _erasures(store) == []
    assert http.get("/status").json()["last_sweep_warnings"] == 1


def test_a_sweep_that_would_erase_most_users_erases_nobody(maya, fake, store):
    """Lens pointed at a fresh or different Hindsight sees every bank "not found"."""
    http, collector = maya
    fake.delete_bank()
    fake.other_banks = []  # an empty listing

    collector.sweep()

    assert _held(http) == 200 and _erasures(store) == []
    assert http.get("/status").json()["last_sweep_warnings"] == 1


def test_a_sweep_survives_a_store_error(maya, fake, store, monkeypatch):
    http, collector = maya
    monkeypatch.setattr(store, "list_banks", lambda: 1 / 0)
    collector.sweep()
    assert http.get("/status").json()["last_sweep_warnings"] == 1


def test_an_erasure_that_cannot_be_recorded_erases_nothing(maya, fake, store):
    http, collector = maya
    fake.delete_bank()
    access_log.drop(store.engine)

    collector.sweep()

    assert store.get_bank(BANK) is not None and len(store.list_beliefs(BANK)) == 2  # still held
    assert http.get("/status").json()["last_sweep_warnings"] == 1


def test_a_bank_that_comes_back_starts_fresh(maya, fake, store):
    http, collector = maya
    fake.delete_bank()
    collector.sweep()
    assert _held(http) == 404

    fake.recreate_bank()
    fake.tick(60)
    f1 = fake.retain("s9", "Maya said she runs on Sundays.", "Maya runs on Sundays")
    fake.create_observation("Maya runs on Sundays", [f1])
    collector.sweep()
    assert collector.wait_until_idle(10)

    user = http.get(f"/api/users/{BANK}", auth=LOGIN).json()["user"]
    assert [b["text"] for b in user["beliefs"]] == ["Maya runs on Sundays"]
    assert user["portrait"] is None and user["recalls"] == []


# -- lens forget ---------------------------------------------------------------


@pytest.fixture
def forget(maya, monkeypatch, capsys):
    """Run `lens forget` against the test store and the fake Hindsight."""
    _, collector = maya
    monkeypatch.setattr(cli, "build_collector", lambda _settings: collector)

    def run(*args: str, answer: str | None = None) -> tuple[int, str, str]:
        if answer is not None:
            monkeypatch.setattr("builtins.input", lambda _prompt: answer)
        code = cli.main(["forget", BANK, *args])
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


def test_forget_refuses_while_hindsight_holds_the_bank(maya, forget, store):
    http, _ = maya
    code, _, err = forget("--yes")
    assert code == 1 and "still holds" in err
    assert _held(http) == 200 and _erasures(store) == []


def test_forget_refuses_when_hindsight_cannot_answer(maya, forget, fake, store):
    http, _ = maya
    fake.delete_bank()
    fake.fail_paths.add(f"{BANK}/stats")
    code, _, err = forget("--yes")
    assert code == 1 and "cannot confirm" in err
    assert _held(http) == 200


def test_forget_erases_once_hindsight_has_and_prints_what_it_removed(maya, forget, fake, store):
    http, _ = maya
    fake.delete_bank()

    code, out, _ = forget("--yes")

    assert code == 0 and "beliefs: 2" in out
    assert _held(http) == 404
    [erase] = _erasures(store)
    assert erase["username"].startswith("cli") and erase["detail"]["trigger"] == "lens forget"


def test_forget_asks_first_without_yes(maya, forget, fake, store, monkeypatch):
    http, _ = maya
    fake.delete_bank()

    code, out, _ = forget(answer="n")
    assert code == 1 and "Nothing erased" in out and _held(http) == 200

    def closed_input(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed_input)
    assert cli.main(["forget", BANK]) == 1 and _held(http) == 200

    code, _, _ = forget(answer="y")
    assert code == 0 and _held(http) == 404
