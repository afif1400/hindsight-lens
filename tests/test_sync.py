"""The collector's promises, each checked against the fake Hindsight."""

from __future__ import annotations

from fake_hindsight import BANK
from sqlalchemy import text


def _story(fake):
    """Create, rewrite, create a second, then supersede it: one of everything."""
    f1 = fake.retain(
        "session-1", "Maya said she prefers reminders in the morning.", "Maya prefers reminders in the morning"
    )
    a = fake.create_observation("Maya prefers morning reminders", [f1], reason="first mention")
    f2 = fake.retain("session-2", "Maya asked to move reminders to 9pm.", "Maya asked to move reminders to 9pm")
    fake.update_observation(a, "Maya used to prefer morning reminders but now wants them at 9pm", [f2])
    f3 = fake.retain("session-3", "Maya is training for a 10k race.", "Maya is training for a 10k race")
    b = fake.create_observation("Maya is training for a 10k", [f3], reason="new facet")
    return a, b, (f1, f2, f3)


def test_a_new_belief_is_recorded_with_its_reason_and_evidence(fake, syncer, store):
    f1 = fake.retain(
        "session-1", "Maya said she prefers reminders in the morning.", "Maya prefers reminders in the morning"
    )
    a = fake.create_observation("Maya prefers morning reminders", [f1], reason="first mention")

    result = syncer.sync_bank(BANK)

    belief = store.get_belief(BANK, a)
    assert belief["status"] == "live" and belief["proof_count"] == 1
    [version] = store.versions(BANK, a)
    assert (version["change_type"], version["text"], version["reason"]) == (
        "created",
        "Maya prefers morning reminders",
        "first mention",
    )
    [fact] = store.get_evidence(BANK, [f1])
    assert fact["document_id"] == "session-1"
    assert fact["source_text"] == "Maya said she prefers reminders in the morning."
    assert result.warnings == []


def test_a_rewrite_keeps_both_wordings_and_says_why(fake, syncer, store):
    a, _, (_, f2, _) = _story(fake)

    syncer.sync_bank(BANK)

    created, updated = store.versions(BANK, a)
    assert created["text"] == "Maya prefers morning reminders"
    assert updated["text"] == "Maya used to prefer morning reminders but now wants them at 9pm"
    assert updated["change_type"] == "updated" and updated["reason"] == "preference changed"
    assert updated["new_source_memory_ids"] == [f2] and updated["evidence_count"] == 2
    assert store.get_belief(BANK, a)["proof_count"] == 2


def test_a_belief_first_met_after_it_changed_is_marked_earliest_known(fake, syncer, store):
    """Lens was not running at creation, so it must not claim to know when that was."""
    a, _, _ = _story(fake)
    syncer.sync_bank(BANK)

    first = store.versions(BANK, a)[0]
    assert first["change_type"] == "earliest_known"
    assert first["changed_at"] is None and first["changed_at_is_estimate"] is True


def test_syncing_twice_records_nothing_twice(fake, syncer, store):
    _story(fake)
    fake.recall("When does Maya want reminders?", list(fake.observations))
    syncer.sync_bank(BANK)
    before = store.summary()

    second = syncer.sync_bank(BANK)

    assert store.summary()["versions"] == before["versions"]
    assert (second.versions_added, second.reasons_attached, second.evidence_added, second.recalls_added) == (0, 0, 0, 0)


def test_only_changed_beliefs_are_refetched(fake, syncer, store):
    a, b, _ = _story(fake)
    syncer.sync_bank(BANK)
    fake.tick(60)
    fake.requests.clear()
    f4 = fake.retain("session-5", "Maya now runs at dawn.", "Maya runs at dawn")
    fake.update_observation(b, "Maya is training for a 10k and runs at dawn", [f4], reason="more detail")

    syncer.sync_bank(BANK)

    history_calls = [r for r in fake.requests if r.endswith("/history")]
    assert history_calls == [f"/v1/default/banks/{BANK}/memories/{b}/history"]
    assert len(store.versions(BANK, b)) == 2 and len(store.versions(BANK, a)) == 2


def test_a_deleted_belief_survives_with_its_reason(fake, syncer, store):
    """Hindsight returns 404 for it and its history; Lens still has all of it."""
    a, b, _ = _story(fake)
    syncer.sync_bank(BANK)
    fake.tick(30)
    fake.delete_observation(b, reason="no longer true")
    deleted_at = fake.clock

    result = syncer.sync_bank(BANK, reconcile=True)  # what a ping with observations_deleted=1 triggers

    belief = store.get_belief(BANK, b)
    assert belief["status"] == "deleted" and belief["text"] == "Maya is training for a 10k"
    tombstone = store.versions(BANK, b)[-1]
    assert tombstone["change_type"] == "deleted" and tombstone["reason"] == "no longer true"
    # The trace knows the real moment; the collector's own guess is replaced by it.
    assert tombstone["changed_at"] == deleted_at and tombstone["changed_at_is_estimate"] is False
    assert result.deletes_detected == 1
    assert store.get_belief(BANK, a)["status"] == "live"


def test_a_delete_is_caught_without_a_ping(fake, syncer, store):
    """The ping was dropped. The sweep still notices: the trace names the belief."""
    _, b, _ = _story(fake)
    syncer.sync_bank(BANK)
    fake.tick(30)
    fake.delete_observation(b)

    result = syncer.sync_bank(BANK)

    assert result.reconciled and store.get_belief(BANK, b)["status"] == "deleted"


def test_a_delete_is_caught_even_after_the_trace_expired(fake, syncer, store):
    """No ping and no trace: only the observation count gives it away."""
    _, b, _ = _story(fake)
    syncer.sync_bank(BANK)
    fake.tick(30)
    fake.delete_observation(b, reason=None)

    syncer.sync_bank(BANK)

    tombstone = store.versions(BANK, b)[-1]
    assert tombstone["change_type"] == "deleted" and tombstone["reason"] is None
    assert tombstone["changed_at_is_estimate"] is True


def test_an_unlisted_belief_that_still_exists_is_not_called_deleted(fake, syncer, store):
    """An incomplete listing must never be written down as a deletion."""
    a, _, _ = _story(fake)
    syncer.sync_bank(BANK)
    fake.hide_from_listing.add(a)

    result = syncer.sync_bank(BANK, reconcile=True)

    assert store.get_belief(BANK, a)["status"] == "live"
    assert any("still exists" in w for w in result.warnings)


def test_recalls_are_joined_to_the_beliefs_they_returned(fake, syncer, store):
    a, b, _ = _story(fake)
    fake.recall("When does Maya want reminders?", [a, b])

    syncer.sync_bank(BANK)

    [hit] = store.recalls_for_memory(BANK, a)
    assert (hit["query"], hit["rank"], hit["result_count"], hit["score"]) == (
        "When does Maya want reminders?",
        1,
        2,
        0.3,
    )
    assert store.recalls_for_memory(BANK, b)[0]["rank"] == 2


def test_a_change_is_still_recorded_when_hindsight_keeps_no_history(fake, syncer, store):
    f1 = fake.retain("s1", "raw one", "fact one")
    a = fake.create_observation("first wording", [f1])
    syncer.sync_bank(BANK)
    fake.history_enabled = False
    fake.tick(30)
    f2 = fake.retain("s2", "raw two", "fact two")
    fake.update_observation(a, "second wording", [f2], reason="changed")

    syncer.sync_bank(BANK)

    assert [v["text"] for v in store.versions(BANK, a)] == ["first wording", "second wording"]
    assert store.versions(BANK, a)[1]["reason"] == "changed"


def test_a_truncated_trace_costs_the_reason_but_not_the_change(fake, syncer, store):
    """Hindsight truncates long LLM output, which can leave invalid JSON behind."""
    f1 = fake.retain("s1", "raw", "fact")
    a = fake.create_observation("first", [f1])
    f2 = fake.retain("s2", "raw2", "fact2")
    fake.update_observation(a, "second", [f2], output='{"creates":[],"updates":[{"text":"sec')

    result = syncer.sync_bank(BANK)

    assert store.versions(BANK, a)[1]["text"] == "second"
    assert store.versions(BANK, a)[1]["reason"] is None
    assert result.warnings == []


def test_one_failing_endpoint_does_not_lose_the_rest_and_is_retried(fake, syncer, store):
    a, _, _ = _story(fake)
    fake.fail_paths.add("/llm-requests")

    first = syncer.sync_bank(BANK)

    assert len(store.versions(BANK, a)) == 2, "beliefs were still collected"
    assert any(w.startswith("reasons:") for w in first.warnings)
    assert store.get_bank(BANK)["trace_cursor"] is None, "the failed step's cursor did not move"

    fake.fail_paths.clear()
    syncer.sync_bank(BANK)
    assert store.versions(BANK, a)[1]["reason"] == "preference changed"


def test_a_fact_deleted_before_collection_is_kept_as_far_as_known(fake, syncer, store):
    f1 = fake.retain("s1", "raw", "fact one")
    a = fake.create_observation("first", [f1])
    f2 = fake.retain("s2", "raw2", "fact two")
    fake.update_observation(a, "second", [f2])
    del fake.facts[f2]

    syncer.sync_bank(BANK)

    [gone] = store.get_evidence(BANK, [f2])
    assert gone["missing"] is True


def test_the_portrait_is_versioned_by_content(fake, syncer, store):
    fake.mental_models.append(
        {
            "id": "mm-1",
            "name": "User portrait",
            "content": "Maya wants morning reminders.",
            "last_refreshed_at": fake.clock.isoformat(),
        }
    )
    syncer.sync_bank(BANK)
    syncer.sync_bank(BANK)
    fake.mental_models[0]["content"] = "Maya wants 9pm reminders."

    result = syncer.sync_bank(BANK)

    assert result.portraits_added == 1
    assert store.latest_portrait(BANK)["content"] == "Maya wants 9pm reminders."


def test_a_delete_remembers_which_fact_caused_it(fake, syncer, store):
    """Hindsight's own record of a delete has no link to what the user said. Lens keeps one."""
    _, b, _ = _story(fake)
    syncer.sync_bank(BANK)
    fake.tick(30)
    f4 = fake.retain("session-4", "Maya cancelled the 10k race.", "Maya cancelled the 10k race")
    fake.delete_observation(b, because=(f4,))

    syncer.sync_bank(BANK, reconcile=True)

    tombstone = store.versions(BANK, b)[-1]
    assert tombstone["trigger_memory_ids"] == [f4]
    [cause] = store.get_evidence(BANK, [f4])
    assert cause["document_id"] == "session-4" and cause["source_text"] == "Maya cancelled the 10k race."


def test_a_leftover_trace_is_not_pinned_on_a_new_belief_with_the_same_wording(fake, syncer, store):
    """A bank was wiped and rebuilt. Yesterday's reasons belong to yesterday's beliefs."""
    f1 = fake.retain("s1", "raw", "fact")
    fake.create_observation("Maya prefers morning reminders", [f1], reason="reason from the old bank")
    fake.observations.clear()
    fake.history.clear()
    fake.tick(3600)
    f2 = fake.retain("s2", "raw again", "fact again")
    a = fake.create_observation("Maya prefers morning reminders", [f2], reason=None)

    syncer.sync_bank(BANK)

    [version] = store.versions(BANK, a)
    assert version["reason"] is None and version["trigger_memory_ids"] == []


def test_quick_successive_rewrites_each_keep_their_own_reason(fake, syncer, store):
    """Found by replaying six weeks in two minutes: the cursor overlap re-reads the
    previous rewrite's LLM call, which must not be pinned on the next rewrite."""
    f1 = fake.retain("s1", "raw", "Maya joined a gym")
    a = fake.create_observation("Maya joined a gym", [f1], reason="plan announced")
    syncer.sync_bank(BANK)
    f2 = fake.retain("s2", "raw", "Maya cancelled the gym")
    fake.update_observation(a, "Maya cancelled the gym and walks instead", [f2], reason="gym cancelled")
    syncer.sync_bank(BANK)
    f3 = fake.retain("s3", "raw", "Maya walks 25 minutes")
    fake.update_observation(a, "Maya walks 25 minutes every workday", [f3], reason="walk is holding")
    syncer.sync_bank(BANK)
    f4 = fake.retain("s4", "raw", "Maya walks 25 minutes again")
    fake.update_observation(a, "Maya walks 25 minutes every workday", [f4], reason="same wording, more evidence")
    syncer.sync_bank(BANK)

    assert [v["reason"] for v in store.versions(BANK, a)] == [
        "plan announced",
        "gym cancelled",
        "walk is holding",
        "same wording, more evidence",
    ]


def _store_from_before_entities(store) -> None:
    """What a store written before Lens kept entities looks like."""
    with store.engine.begin() as conn:
        conn.execute(text("ALTER TABLE lens_evidence DROP COLUMN entities"))
    store.init_schema()


def test_a_store_from_before_entities_gains_them_on_the_next_sync(fake, syncer, store):
    f1 = fake.retain("s1", "Maya said she walks at dawn.", "Maya walks at dawn", entities=["Maya", "walk"])
    fake.create_observation("Maya is an early walker", [f1])
    syncer.sync_bank(BANK)
    _store_from_before_entities(store)
    assert store.get_evidence(BANK, [f1])[0]["entities"] is None

    fake.tick(30)
    fake.requests.clear()
    syncer.sync_bank(BANK)

    [fact] = store.get_evidence(BANK, [f1])
    assert fact["entities"] == ["Maya", "walk"] and fact["text"] == "Maya walks at dawn"
    assert fact["source_text"] == "Maya said she walks at dawn.", "a backfill must not touch what Lens already holds"
    assert not [p for p in fake.requests if f"/memories/{f1}" in p or "/chunks/" in p], (
        "read from the listing, not fact by fact"
    )


def test_a_fact_hindsight_deleted_keeps_its_text_when_entities_are_backfilled(fake, syncer, store):
    f1 = fake.retain("s1", "Maya said she walks at dawn.", "Maya walks at dawn", entities=["walk"])
    fake.create_observation("Maya is an early walker", [f1])
    syncer.sync_bank(BANK)
    _store_from_before_entities(store)
    del fake.facts[f1]

    fake.tick(30)
    syncer.sync_bank(BANK)

    [fact] = store.get_evidence(BANK, [f1])
    assert fact["text"] == "Maya walks at dawn" and fact["entities"] == [] and not fact["missing"]
