# Testing and verification

[← README](../../README.md) · [All documentation](../../README.md#documentation)

Lint, formatting, unit and integration tests, and migration checks.

## Verify

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -v
```

Without `TEST_DATABASE_URL`, local Pytest explicitly skips database integration tests. To run the full suite, start the separate disposable test database on its default port `5434`:

```bash
docker compose --profile test up --detach --wait db-test qdrant-test
TEST_DATABASE_URL='postgresql+psycopg://support:support_ci@127.0.0.1:5434/ai_support_test' \
TEST_QDRANT_URL=http://127.0.0.1:6336 \
  uv run --locked pytest -v
```

Integration fixtures reset the schema through Alembic. They accept only an explicitly provided `TEST_DATABASE_URL` whose database name ends in `_test`; they never fall back to the development `DATABASE_URL`. Knowledge tests use only an explicit `TEST_QDRANT_URL` and create and delete their own `test_*` collections. In CI, a missing test URL fails the checks. Unit tests use an in-process Qdrant (`:memory:`) and need neither service. `tests/conftest.py` gives every test an in-memory LangGraph checkpointer, so no test writes workflow state to the development database; review integration tests replace it with a `PostgresSaver` on the `_test` database.

To inspect the development migration state separately:

```bash
uv run --locked alembic current
uv run --locked alembic check
```
