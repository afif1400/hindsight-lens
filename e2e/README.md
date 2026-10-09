# Live end-to-end test

`test_lens_live.py` runs inside Hindsight's own system-test harness, which boots
a real `hindsight-api` with an embedded Postgres and a scripted LLM.

```bash
git clone https://github.com/vectorize-io/hindsight && cd hindsight
(cd hindsight-api-slim && uv sync --frozen --extra embedded-db)
cp /path/to/hindsight-lens/e2e/test_lens_live.py hindsight-system-tests/tests/test_98_lens_live.py
cd hindsight-system-tests
PYTHONPATH=/path/to/hindsight-lens uv run --frozen --with "sqlalchemy>=2.0" pytest tests/test_98_lens_live.py
```

Run it as a non-root user: the embedded Postgres refuses to start as root.
