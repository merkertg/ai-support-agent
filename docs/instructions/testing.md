# Testing and verification

[← README](../../README.md) · [All documentation](../../README.md#documentation)

Lint, formatting, unit and integration tests, migration checks, and offline evaluations.

## Verify

```bash
uv run --locked python scripts/check_dependency_age.py
uv run --locked python scripts/check_project.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -v
uv run --locked python -m evaluations.run
```

The default `uv sync --locked` installs the `dev` group, which includes the `ui` group, so the Streamlit AppTest checks in `tests/test_operator_ui.py` run with the rest of the suite.

`scripts/check_dependency_age.py` fails when any locked package was published on PyPI less than 14 days ago; the `[tool.uv] exclude-newer` date in `pyproject.toml` keeps `uv lock` from picking such releases.

`python -m evaluations.run` measures retrieval quality and the agent's server-side safety contract without network, keys, or PostgreSQL. It writes `reports/evaluations.json` and exits `1` when a threshold from `evaluations/thresholds.json` is violated. Metrics, scenarios, and limits are described in the [Stage 09 guide](../STAGE_09.md).

Without `TEST_DATABASE_URL`, local Pytest explicitly skips database integration tests. To run the full suite, start the separate disposable test database on its default port `5434`:

```bash
docker compose --profile test up --detach --wait db-test qdrant-test
TEST_DATABASE_URL='postgresql+psycopg://support:support_ci@127.0.0.1:5434/ai_support_test' \
TEST_QDRANT_URL=http://127.0.0.1:6336 \
  uv run --locked pytest -v --cov --cov-report=term-missing:skip-covered \
    --cov-report=xml --cov-report=json --cov-report=html
```

Integration fixtures reset the schema through Alembic. They accept only an explicitly provided `TEST_DATABASE_URL` whose database name ends in `_test`; they never fall back to the development `DATABASE_URL`. Knowledge tests use only an explicit `TEST_QDRANT_URL` and create and delete their own `test_*` collections. In CI, a missing test URL fails the checks. Unit tests use an in-process Qdrant (`:memory:`) and need neither service. `tests/conftest.py` gives every test an in-memory LangGraph checkpointer, so no test writes workflow state to the development database; review integration tests replace it with a `PostgresSaver` on the `_test` database.

To inspect the development migration state separately:

```bash
uv run --locked alembic current
uv run --locked alembic check
```

`python scripts/check_project.py` checks every Python function/class docstring, local Markdown links, fenced code blocks, Bash syntax (`bash -n`, without executing examples), PowerShell syntax (the `pwsh` parser, also without execution), and JSON examples. Without PowerShell 7 installed, PowerShell blocks are reported as `powershell_unchecked`; CI on Linux runs `check_project.py --require-powershell`, which turns unchecked blocks into an error. [The verification report](../VERIFICATION.md) records the latest complete run and its limits.

## Coverage gate

`pytest-cov` is part of the development dependencies. Run the full suite against the separate test services with `--cov`; running unit tests alone cannot establish the CI result. `pyproject.toml` enables branch measurement, collects Python subprocesses (including smoke CLI), and requires a minimum **85% combined line/branch coverage**. A result below the threshold fails the command even when all tests pass. The measured source directories are `app`, `operator_ui`, `evaluations`, and `scripts`; tests and migrations are outside the metric, and only `scripts/build_guide.py` is omitted because its layout requires visual inspection.

Reports: `reports/coverage.xml`, `reports/coverage.json`, and `reports/coverage-html/index.html`. They are ignored by Git and uploaded by CI as `coverage-report` for seven days, including a failed gate when reports exist. Keep the denominator intact when adding code; cover meaningful behavior and error paths rather than lowering the threshold.

The global recovery monitor is disabled in ordinary test clients so tests cannot touch the development database. Recovery tests opt in explicitly with isolated dependencies. Real concurrent PostgreSQL scenarios live in `tests/integration/test_abandoned_runs.py`; see [recovery details](../RUN_RECOVERY.md).

Subprocess collection uses the [official pytest-cov 7 configuration](https://pytest-cov.readthedocs.io/en/latest/subprocess-support.html): `patch = ["subprocess"]`. Without it, CLI behavior already tested through a child interpreter would incorrectly appear unmeasured.

The omit list uses `*/scripts/build_guide.py` in addition to the relative path. A child interpreter started by a test in a temporary directory resolves relative omit patterns against its own working directory; with only `scripts/build_guide.py`, that child recorded the PDF generator as an unexecuted file. This is why commit `571538c` failed in GitHub Actions at 77.13% (825 generator statements at 0%) while all 951 tests passed. The pattern with `*/` matches regardless of the working directory.

## Portability checks

The CI job `portability` runs on Linux, macOS, and Windows. Reproduce it on any of them:

```bash
uv run --locked python scripts/check_dependency_age.py
uv run --locked python scripts/check_project.py
uv run --locked pytest -q --ignore=tests/integration
uv run --locked python scripts/check_local_start.py
```

Tests read and write files with an explicit `encoding="utf-8"` and decode child processes as UTF-8, because Windows defaults to cp1252. To catch a missing encoding on macOS or Linux, run the unit tests under a non-UTF-8 locale:

```bash
LC_ALL=en_US.ISO8859-1 PYTHONUTF8=0 uv run --locked pytest -q --ignore=tests/integration
```

If macOS shows zero coverage for the subprocess CLI despite its tests passing, inspect the coverage startup hook with `ls -lO .venv/lib/python3.12/site-packages/a1_coverage.pth`. Python skips a `.pth` file marked `hidden`. In a normal Terminal session, remove that flag before running coverage:

```bash
chflags nohidden .venv/lib/python3.12/site-packages/a1_coverage.pth
```

This is a local environment issue. The GitHub Linux runner uses the standard subprocess hook. During the recorded Codex check, its runtime repeatedly restored the macOS flag; an equivalent temporary `sitecustomize` hook outside the repository was used only to collect the same child-process coverage. It does not change source exclusions or the 85% threshold.
