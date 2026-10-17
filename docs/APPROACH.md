# AI Support Agent — Solution Approach

## Problem and product goal

Support operators need customer information, license details, and technical documentation to investigate a ticket. The planned application will collect this information, prepare an answer supported by sources, and let an operator approve it before simulated delivery.

The project uses fictional TechFlow products and artificial support data.

## Current implementation: Stages 01–02

The application now has a runnable HTTP foundation and a persistent relational schema. Its current database operations are a readiness query and explicit demonstration commands. Product REST APIs will be introduced in Stage 03.

```text
HTTP client → Uvicorn → FastAPI
                         ├─ GET /health → application status
                         ├─ GET /ready  → PostgreSQL + Alembic revision check
                         └─ /docs and /openapi.json → API documentation

Explicit demo CLI → SQLAlchemy session → PostgreSQL
Alembic migrations → versioned PostgreSQL schema
```

`GET /health` returns the service name and a fixed `ok` status. It remains available without database configuration. `GET /ready` opens a connection, executes a query, and compares the database's applied Alembic heads with the heads shipped in the application. It returns HTTP 200 only when both connection and migration checks succeed; missing configuration, an unavailable database, or an outdated schema produces HTTP 503 without revealing the database URL.

`app/config.py` reads `DATABASE_URL` from the process environment or `.env`. `app/db/session.py` creates the engine lazily, checks pooled connections before reuse, and provides sessions that roll back on errors and close after use. Writes commit explicitly. No engine connection is required merely to import the application or call `/health`.

`pyproject.toml` declares Python requirements, dependencies, and tool configuration. `uv.lock` records resolved dependencies. `uv sync --locked` prepares `.venv` and checks that the lockfile matches the configuration.

## Stack and responsibility

| Component | Choice | Responsibility |
| --- | --- | --- |
| Language | Python 3.12+ | API, database code and later agent implementation |
| API | FastAPI | HTTP routing, JSON responses and generated OpenAPI |
| ASGI server | Uvicorn | Local and container HTTP serving |
| Database | PostgreSQL 17 | Persistent records, relational links and constraints |
| Database access | SQLAlchemy 2 with psycopg 3 | Typed models, connections, sessions and transactions |
| Schema changes | Alembic | Ordered schema creation, comparison and explicit rollback |
| Configuration | pydantic-settings | Environment and `.env` loading with validation |
| Dependency management | uv | Isolated environment and locked dependencies |
| Testing | Pytest, TestClient/HTTPX and real PostgreSQL | HTTP behavior, configuration, transactions and migrations |
| Code checks | Ruff | Formatting and common Python issues |
| Local orchestration | Docker Compose | Database health, migrations, API startup and volumes |

## Relational schema

`app/db/base.py` defines one declarative base and consistent names for primary keys, unique constraints, checks, foreign keys, and indexes. `app/models` registers and exports nine models:

| Model / table | Stored data |
| --- | --- |
| `User` / `users` | Employee profile and `operator` or `admin` role; no passwords or authentication |
| `Customer` / `customers` | Customer name, unique email and optional company |
| `Product` / `products` | Product name, unique code and optional description |
| `License` / `licenses` | Customer/product links, unique key, status and validity dates |
| `Ticket` / `tickets` | Customer/product links, subject, description, priority and status |
| `TicketMessage` / `ticket_messages` | Message content, author type and optional employee reference |
| `KnowledgeDocument` / `knowledge_documents` | Metadata of future Markdown articles, unique document key and source path |
| `AgentRun` / `agent_runs` | Future execution record linked to an existing ticket |
| `AgentSuggestion` / `agent_suggestions` | Future suggested content and the employee's review decision |

Foreign keys maintain record ownership. Database checks restrict statuses, priorities, date ranges, message authors, and suggestion review fields. Indexed foreign keys support joins. Identity keys are `BIGINT`; recorded timestamps include time-zone information.

Knowledge documents store metadata rather than a second copy of future Markdown content. Suggestions reach their ticket through `agent_runs.ticket_id`, avoiding a duplicate ticket reference. Agent tables describe future storage and do not execute an agent or implement human review behavior. Employee roles are data constraints and do not enforce access control.

`updated_at` is initialized by PostgreSQL and updated by SQLAlchemy when it issues an update for the mapped record. Direct SQL updates must set it explicitly; this stage creates no timestamp trigger.

## Schema lifecycle and local startup

The first migration explicitly creates the nine tables, constraints, and indexes. It is a frozen description of that schema. Editing a model does not rewrite an already released migration; later schema changes require another revision. Runtime startup never calls `Base.metadata.create_all()`.

Compose uses one application image for migration and API services:

```text
PostgreSQL healthy → alembic upgrade head succeeds → API passes /ready
        db                      migrate                     api
```

The PostgreSQL named volume retains development records after normal `docker compose down` and later startup. On the host, the default database port is `5433`, and the Compose API is available on `8001`. Services inside Docker use `db:5432`; host Python uses `127.0.0.1:5433`. These separate URLs are shown in `.env.example`.

For development with automatic reload, only PostgreSQL needs to run in Docker. Apply migrations using host Python, then run Uvicorn on `127.0.0.1:8000`. The [README](../README.md) gives both startup paths.

`python -m app.db.demo seed` explicitly adds fictional customer, product, and license records in one transaction. Repeated calls reuse their unique keys and preserve existing values. `show` reads a stable JSON snapshot from a new session without writing. Comparing snapshots across `docker compose down` without `--volumes`, followed by `up`, demonstrates that records survive container removal and recreation in the named volume. The second snapshot comes from `show`; no second seed recreates missing records.

## Verification and database isolation

Unit tests cover the HTTP contract, safe readiness failures, configuration, engine/session behavior, and the smoke CLI. Real PostgreSQL integration tests apply migrations, compare models with the resulting schema, exercise records and constraints, and check transaction behavior and migration rollback.

The integration fixtures reset their schema between scenarios. They require an explicit `TEST_DATABASE_URL` pointing to a database whose name ends in `_test`; they never reuse `DATABASE_URL` as a fallback. Local runs without a test URL report skipped integration tests. A missing URL in CI is an error.

Compose provides a separate `db-test` service behind its `test` profile, on host port `5434` by default. It stores disposable test data in memory rather than the development volume. The [Stage 02 guide](STAGE_02.md) explains how to start it and run all tests.

Run lint, formatting, and the full test suite using the commands in the [README](../README.md). Successful verification must also include actual startup, a successful `/ready`, and reading saved records after removing and recreating the Compose containers while retaining the database volume. The [development log](DEVELOPMENT_LOG.md) records implementation steps and verification evidence. This design document makes no claims about agent quality, retrieval accuracy, latency, or cost; those require future functionality and evaluation scenarios.

## CI/CD and Docker packaging

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests targeting `main`, and manual dispatch. The Python job installs locked dependencies, starts its PostgreSQL 17 service, applies migrations, checks schema drift with `alembic check`, runs Ruff lint and formatting checks, and executes all Pytest tests against the explicit test database. The independent lint, formatting, and test steps still report their outcomes if another check fails.

After that job succeeds, the container job builds one runtime image and starts the Compose database, migration, and API services with `--no-build`. `scripts/smoke_test.py --check-database` validates `/health`, `/ready`, `/docs`, and `/openapi.json`, including the documented readiness operation. An unexpected HTTP 503 fails immediately. The job then seeds demonstration records, runs `docker compose down` without `--volumes`, starts the stack again, reads the saved snapshot, compares it with the original, and checks HTTP readiness again. This removes and recreates containers while preserving the named database volume. Failure logs include all Compose services; cleanup removes the isolated CI stack and its volume.

The runtime image installs runtime dependencies only, runs as UID 10001, and includes Alembic configuration and migrations. Its health check uses `/ready`, so a running HTTP process alone does not make the container healthy.

On a successful push to `main`, that same tested image is exported as an artifact, loaded and identified again in the publication job, and pushed to GHCR without rebuilding. Each commit receives a full SHA tag; only the current `main` tip updates `latest`. Publications queue sequentially. The workflow uses `GITHUB_TOKEN`, with `packages: write` limited to the publication job.

This implements continuous delivery of a container image. External server deployment is outside the chosen scope. Detailed operation is documented in [CI_CD.md](CI_CD.md). GitHub execution and publication must be confirmed from an actual workflow run, independently of local checks.

## Planned architecture

Later stages will grow into a modular backend with this flow:

```text
Streamlit operator interface
             ↓ HTTP / JSON
FastAPI routes → business services → repositories → PostgreSQL
                        ↑
                  agent tools
                        ↑
              agent runtime → LLM API
                        ↓
              document search → Qdrant
```

The interface will communicate through the API. Server-side services will enforce access rules and expose a restricted set of agent tools. The agent will prepare a suggestion for human review. LangGraph will be introduced after a basic tool-calling loop is understood and implemented.

PostgreSQL and its schema are now implemented. REST business operations, Qdrant, LLM integration, agent tools, authentication, human review, and the operator interface remain future work.

## Next step

Stage 03 will add customer, license, and ticket REST APIs using the existing models and explicit transaction handling. Its first useful scenario is creating a customer, assigning a license, opening a ticket, and reading the records through `/docs`. Later stages add LLM integration, tool calling, RAG, human review, and the operator interface in the existing roadmap order.
