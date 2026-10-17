# AI Support Agent

**Начни с [инструкции этапа 02 на русском](docs/STAGE_02.md):**
PostgreSQL, модели, миграции, запуск, демонстрационные данные и проверки.
[Журнал разработки](docs/DEVELOPMENT_LOG.md) объясняет каждый выполненный шаг;
[инструкция этапа 01](docs/STAGE_01.md) разбирает основу FastAPI.

A Python learning project for a technical support assistant. The future product will help an operator investigate tickets, check customer licenses, search documentation, and review suggested replies before simulated delivery.

The current implementation covers **Stages 01–02**: FastAPI, PostgreSQL 17, nine SQLAlchemy models, Alembic migrations, database readiness, Docker Compose, and automated checks. The GitHub Actions workflow checks the application and database before delivering the tested Docker image to GHCR on successful pushes to `main`. Customer, license, and ticket REST operations are the next stage.

## Requirements

- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for dependency and environment management.
- Docker with the `docker compose` command; start Docker Desktop on this Mac.

Run all commands from the project root, the directory containing `pyproject.toml`.

## Quickstart: API and PostgreSQL

```bash
cp -n .env.example .env
uv sync --locked
docker compose up --build --detach --wait api
```

`cp -n` preserves an existing `.env`. When reusing one, compare its settings with `.env.example`. The example contains credentials for local development. Compose starts PostgreSQL, waits for it to become healthy, applies Alembic migrations, and starts the API. Its final health check requires both a database connection and the current migration revision.

Default addresses:

- [API readiness](http://127.0.0.1:8001/ready): `{"status":"ok","database":"ok"}`.
- [Application health](http://127.0.0.1:8001/health): `{"status":"ok","service":"ai-support-agent"}`.
- [Interactive API documentation](http://127.0.0.1:8001/docs).
- PostgreSQL for host tools: `127.0.0.1:5433`.

Check the running stack through real HTTP:

```bash
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 30 --check-database
```

Create fictional demonstration records, then read them from a new session:

```bash
docker compose exec -T api python -m app.db.demo seed
docker compose exec -T api python -m app.db.demo show
```

`seed` is explicit and idempotent; application startup does not create sample records. `show` only reads the saved customer, product, and license. The [Stage 02 guide](docs/STAGE_02.md) shows how to compare these records before and after removing and recreating the Compose containers while retaining the PostgreSQL volume.

Inspect or stop the stack:

```bash
docker compose logs --no-color db migrate api
docker compose down
```

Ordinary `down` retains the named PostgreSQL volume and its records.

## Optional: run the API on the host

For Python development with automatic reload, start PostgreSQL in Docker and run the API from `.venv`. `.env` contains separate URLs for host Python (`127.0.0.1:5433`) and Compose services (`db:5432`).

```bash
docker compose up --detach --wait db
uv run --locked alembic upgrade head
uv run --locked uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open [host API documentation](http://127.0.0.1:8000/docs) and stop Uvicorn with `Ctrl+C`.

The Stage 01 standalone API still starts without a configured database: `/health`, `/docs`, and `/openapi.json` remain available. `/ready` returns HTTP 503 until PostgreSQL is reachable and its Alembic revision matches the application. `/health` checks the running application; `/ready` checks database readiness.

## Verify

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -v
```

Without `TEST_DATABASE_URL`, local Pytest explicitly skips database integration tests. To run the full suite, start the separate disposable test database on its default port `5434`:

```bash
docker compose --profile test up --detach --wait db-test
TEST_DATABASE_URL='postgresql+psycopg://support:support_ci@127.0.0.1:5434/ai_support_test' \
  uv run --locked pytest -v
```

Integration fixtures reset the schema through Alembic. They accept only an explicitly provided `TEST_DATABASE_URL` whose database name ends in `_test`; they never fall back to the development `DATABASE_URL`. In CI, a missing test URL fails the checks.

To inspect the development migration state separately:

```bash
uv run --locked alembic current
uv run --locked alembic check
```

## CI and delivery to GHCR

The workflow runs for pushes to `main`, pull requests targeting `main`, and manual checks:

```text
Locked dependencies + PostgreSQL + migrations + Ruff + all Pytest tests
                                  ↓
Docker build + Compose readiness + HTTP smoke + database persistence check
                                  ↓
Publish the tested image to GHCR (push to main only)
```

The Python job applies migrations to a separate PostgreSQL test database and runs `alembic check` to detect differences between the schema and models. The container job builds once, starts `db → migrate → api`, checks all four HTTP routes, and compares saved records before and after removing and recreating the Compose containers while retaining the PostgreSQL volume. A failed check blocks publication. The publication job receives that same image as an artifact and does not rebuild it.

Images use `ghcr.io/merkertg/ai-support-agent:sha-<full-commit-SHA>`. The current tip of `main` can also update `latest`. The workflow delivers an image to the registry; running it on an external server is a later step.

**[Подробная инструкция CI/CD на русском](docs/CI_CD.md)** explains GitHub permissions, logs, artifacts, image tags, local reproduction, and troubleshooting. A local `git commit` starts no GitHub jobs until the commit is pushed. Confirm execution and publication in an actual GitHub Actions run.

## Project files

```text
.github/workflows/
  ci.yml                 # Python/database checks, Compose and GHCR delivery
app/
  main.py                # /health and /ready, API lifecycle
  config.py              # DATABASE_URL from environment or .env
  db/
    base.py              # DeclarativeBase and constraint naming
    session.py           # Lazy engine and request-scoped sessions
    demo.py              # Explicit idempotent seed and read-only show
  models/
    entities.py          # Nine relational models
migrations/
  env.py                 # Alembic execution and database URL handling
  versions/              # Versioned schema creation and rollback
tests/
  integration/           # Real PostgreSQL and migration checks
  test_health.py         # Standalone API behavior
  test_readiness.py      # Database readiness behavior
  test_config.py         # Environment configuration
  test_smoke_test.py     # Real HTTP smoke CLI regression tests
scripts/
  smoke_test.py          # Three HTTP checks; /ready with --check-database
docs/
  APPROACH.md            # Current design and future architecture
  CI_CD.md               # CI/CD guide in Russian
  STAGE_01.md            # FastAPI learning guide in Russian
  STAGE_02.md            # Database learning guide in Russian
  DEVELOPMENT_LOG.md     # Step-by-step implementation log
compose.yaml             # PostgreSQL, migration, API and isolated test database
alembic.ini              # Migration configuration without a stored database URL
.env.example             # Local development settings
Dockerfile               # Runtime image with application and migrations
.dockerignore            # Excludes local environments, secrets and caches
pyproject.toml           # Metadata, dependencies and tool settings
uv.lock                  # Resolved dependency versions
README.md
```

## Roadmap

| Stage | Work | Status |
| --- | --- | --- |
| 01 | Initialize Python project, FastAPI, health endpoint, first tests | Implemented; verify with the commands above |
| 02 | PostgreSQL, SQLAlchemy models, Alembic migrations | Implemented; includes readiness, demo records and database checks |
| 03 | Customer, license, and ticket REST APIs | Planned |
| 04 | LLM API integration and error handling | Planned |
| 05 | Tool calling and the agent execution loop | Planned |
| 06 | RAG, embeddings, and Qdrant document search | Planned |
| 07 | LangGraph workflow and human review | Planned |
| 08 | Streamlit operator interface | Planned |
| 09 | Agent integration tests, evaluations and security checks | Planned; relational database integration tests are part of Stage 02 |
| 10 | Packaging, CI/CD, and GitHub demonstration materials | Docker Compose and CI/GHCR delivery configured; demonstration materials planned |

## Documentation

- [Solution approach](docs/APPROACH.md)
- [Stage 01 learning guide](docs/STAGE_01.md)
- [Stage 02 learning guide](docs/STAGE_02.md)
- [Development log](docs/DEVELOPMENT_LOG.md)
- [CI/CD and Docker guide](docs/CI_CD.md)

TechFlow and the support scenarios are fictional. The current implementation requires no LLM API key. Database-backed functionality is limited to readiness and explicit demonstration commands; the product REST APIs and AI behavior remain future work.
