# AI Support Agent

**Начни с [инструкции этапа 04 на русском](docs/STAGE_04.md):**
черновик ответа по обращению, mock-режим, настройка OpenAI и проверка ошибок.
[Этап 03](docs/STAGE_03.md) объясняет создание и чтение записей через API.
[Этап 02](docs/STAGE_02.md) объясняет PostgreSQL, модели, миграции и запуск.
[Журнал разработки](docs/DEVELOPMENT_LOG.md) объясняет каждый выполненный шаг;
[инструкция этапа 01](docs/STAGE_01.md) разбирает основу FastAPI.

A Python learning project for a technical support assistant. The future product will help an operator investigate tickets, check customer licenses, search documentation, and review suggested replies before simulated delivery.

The current implementation covers **Stages 01–04**: FastAPI, PostgreSQL 17, nine SQLAlchemy models, Alembic migrations, twelve business API operations, database readiness, Docker Compose, and automated checks. The GitHub Actions workflow checks the application and database before delivering the tested Docker image to GHCR on successful pushes to `main`. The API creates, lists, and reads customers, products, licenses, and tickets. Draft previews support a local mock and an explicitly configured OpenAI provider. Tool calling is the next stage.

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
  --base-url http://127.0.0.1:8001 --timeout 30 --check-database --check-api
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

## Work with the business API

Open [Swagger UI](http://127.0.0.1:8001/docs). Each resource supports these operations:

| Resource | Create | List | Read one |
| --- | --- | --- | --- |
| Customers | `POST /api/customers` | `GET /api/customers` | `GET /api/customers/{resource_id}` |
| Products | `POST /api/products` | `GET /api/products` | `GET /api/products/{resource_id}` |
| Licenses | `POST /api/licenses` | `GET /api/licenses` | `GET /api/licenses/{resource_id}` |
| Tickets | `POST /api/tickets` | `GET /api/tickets` | `GET /api/tickets/{resource_id}` |

Start by creating a customer and product. Use the returned IDs to create their license and ticket; existing demo records mean IDs may differ between machines. The [Russian Stage 03 guide](docs/STAGE_03.md) walks through complete requests and responses.

Creation returns HTTP 201; reads return 200. Invalid input returns 422, an unknown record or missing creation reference returns 404, a duplicate unique value returns 409, and an unavailable database returns a safe 503. Lists return JSON arrays ordered by ID, with `offset=0` and `limit=20` by default (maximum 100). License lists support `customer_id`, `product_id`, and `license_key`; ticket lists support `customer_id`, `product_id`, and `status`. Filters without matches return an empty array.

New tickets always start as `open`. License dates must include a time zone, and expiration cannot precede issuance. The API uses explicit transactions; a failed request must not leave a partial write. This stage adds create/read operations; editing, deletion, authentication, and AI behavior belong to later work.

## Preview a reply with the LLM client

The default `LLM_PROVIDER=mock` requires no API key and makes no external requests. Check its configuration:

```bash
curl --silent --show-error --fail-with-body http://127.0.0.1:8001/api/llm/status
```

The response is `{"provider":"mock","model":"mock-draft-v1","configured":true}`. In Swagger, open `POST /api/tickets/{resource_id}/draft`, enter an existing ticket ID, and execute it without a request body. The response includes `summary`, `category`, `draft_reply`, provider/model metadata, and `requires_human_review: true`. Mock replies explicitly state that no real model was called. This preview does not save a draft or change the ticket.

To enable OpenAI, set `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and an explicit `OPENAI_MODEL` in your local `.env`, then recreate the API service:

```bash
docker compose up --detach --no-build --force-recreate --wait api
```

`configured: true` only confirms that the required settings exist. The [Stage 04 guide](docs/STAGE_04.md) explains real-provider setup, structured output, timeouts, token usage, safe errors, and the first manual request. No real OpenAI request has been verified in this iteration because no API key is configured. Automated tests use mock responses and the real SDK with a substituted HTTP transport.

For a complete container smoke test in mock mode only:

```bash
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 30 --check-llm
```

This explicit flag first rejects any provider other than configured mock, then creates **three fictional records** (customer, product, ticket) and checks the draft and unchanged ticket. It also includes database and business API checks. Use the ordinary smoke command above when you only want read checks.

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

The Python job applies migrations to a separate PostgreSQL test database and runs `alembic check` to detect differences between the schema and models. The container job builds once, starts `db → migrate → api`, checks health/readiness and the business API, validates a mock draft, and compares saved records before and after removing and recreating the Compose containers while retaining the PostgreSQL volume. CI explicitly selects mock with no OpenAI credentials. The draft smoke creates disposable fixture records only after confirming mock configuration. A failed check blocks publication. The publication job receives that same image as an artifact and does not rebuild it.

Images use `ghcr.io/merkertg/ai-support-agent:sha-<full-commit-SHA>`. The current tip of `main` can also update `latest`. The workflow delivers an image to the registry; running it on an external server is a later step.

**[Подробная инструкция CI/CD на русском](docs/CI_CD.md)** explains GitHub permissions, logs, artifacts, image tags, local reproduction, and troubleshooting. A local `git commit` starts no GitHub jobs until the commit is pushed. Confirm execution and publication in an actual GitHub Actions run.

## Project files

```text
.github/workflows/
  ci.yml                 # Python/database checks, Compose and GHCR delivery
app/
  main.py                # /health, /ready, API lifecycle and router registration
  api/
    routes.py            # Thin HTTP handlers for four resources
    dependencies.py      # Request sessions and safe database errors
  schemas/               # Validated resources and structured draft responses
  llm/                   # Independent settings, mock/OpenAI clients and safe errors
  services/              # Business checks, queries and explicit transactions
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
  integration/           # Real PostgreSQL, migrations and complete HTTP workflows
  test_health.py         # Standalone API behavior
  test_readiness.py      # Database readiness behavior
  test_config.py         # Environment configuration
  test_smoke_test.py     # Real HTTP smoke CLI regression tests
  test_llm.py            # Real SDK parser with a substituted HTTP transport
  test_drafts_api.py     # Draft/status HTTP behavior without paid requests
scripts/
  smoke_test.py          # Health, readiness and business API checks
docs/
  APPROACH.md            # Current design and future architecture
  CI_CD.md               # CI/CD guide in Russian
  STAGE_01.md            # FastAPI learning guide in Russian
  STAGE_02.md            # Database learning guide in Russian
  STAGE_03.md            # Business API learning guide in Russian
  STAGE_04.md            # LLM integration learning guide in Russian
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
| 03 | Customer, product, license, and ticket REST APIs | Implemented: create, list and read with PostgreSQL integration tests |
| 04 | LLM draft previews and error handling | Implemented; mock and SDK transport tested, live OpenAI validation requires credentials |
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
- [Stage 03 learning guide](docs/STAGE_03.md)
- [Stage 04 learning guide](docs/STAGE_04.md)
- [Development log](docs/DEVELOPMENT_LOG.md)
- [CI/CD and Docker guide](docs/CI_CD.md)

TechFlow and the support scenarios are fictional. The default mock mode requires no LLM API key; OpenAI mode requires an explicit key and model. The business API persists customer, product, license, and ticket records. The current LLM feature is a read-only draft preview. Tool calling, RAG, authentication, and an operator interface remain future work.
