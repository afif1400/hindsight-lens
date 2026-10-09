"""Which users the fleet asks someone to look at."""

from __future__ import annotations

from datetime import timedelta

from fake_hindsight import BANK

from lens.export import build_user


def _flags_after_four_rewrites(fake, syncer, store, *, days_known: int) -> set[str]:
    f1 = fake.retain("s1", "Maya said she walks at dawn.", "Maya walks at dawn")
    a = fake.create_observation("Maya is an early walker", [f1])
    syncer.sync_bank(BANK)
    fake.tick(timedelta(days=days_known).total_seconds())
    for i in range(4):
        fake.update_observation(a, f"Maya walks at dawn, take {i}", [])
    syncer.sync_bank(BANK)
    return set(build_user(store, BANK, now=fake.clock)["user"]["flags"]) - {"thin"}


def test_a_new_users_first_rewrites_are_not_many_changes(fake, syncer, store):
    assert _flags_after_four_rewrites(fake, syncer, store, days_known=0) == {"new", "changed"}


def test_an_established_user_with_many_rewrites_is_flagged(fake, syncer, store):
    assert _flags_after_four_rewrites(fake, syncer, store, days_known=8) == {"high_churn"}
