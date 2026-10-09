from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_hindsight import FakeHindsight  # noqa: E402

from lens.config import Settings  # noqa: E402
from lens.hindsight import HindsightClient  # noqa: E402
from lens.store import Store  # noqa: E402
from lens.sync import BankSyncer  # noqa: E402


@pytest.fixture
def fake() -> FakeHindsight:
    return FakeHindsight()


@pytest.fixture
def settings(tmp_path) -> Settings:
    # LENS_TEST_DATABASE_URL points the whole suite at another database, e.g. Postgres.
    url = os.environ.get("LENS_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'lens.db'}"
    return Settings(database_url=url, sweep_interval_seconds=0, cursor_overlap_seconds=2)


@pytest.fixture
def store(settings) -> Store:
    from lens.store import metadata

    store = Store(settings.database_url)
    metadata.drop_all(store.engine)
    store.init_schema()
    yield store
    store.engine.dispose()


@pytest.fixture
def client(fake) -> HindsightClient:
    return HindsightClient("http://hindsight.test", transport=fake.transport(), page_size=2)


@pytest.fixture
def syncer(client, store, settings) -> BankSyncer:
    return BankSyncer(client, store, settings)
