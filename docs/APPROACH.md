# AI Support Agent — Solution Approach

## Problem and product goal

Support operators need customer information, license details, and technical documentation to investigate a ticket. The planned application will collect this information, prepare an answer supported by sources, and let an operator approve it before simulated delivery.

The project uses fictional TechFlow products and artificial support data.

## Current implementation: Stages 01–05

The application now has a runnable HTTP foundation and a persistent relational schema. Its database operations include readiness, explicit demonstration commands, and create/read REST operations for customers, products, licenses, and tickets. An LLM client produces a structured draft preview from an existing ticket without modifying stored records. A bounded agent gathers facts through four read-only tools and saves its run together with a pending reply suggestion for human review.

```text
HTTP client → Uvicorn → FastAPI
                         ├─ GET /health → application status
                         ├─ GET /ready  → PostgreSQL + Alembic revision check
                         ├─ /api/* → routers → services → PostgreSQL
                         ├─ POST /api/tickets/{id}/analyze → agent runtime
                         │      ├─ provider (mock / OpenAI Responses)
                         │      └─ read-only tools → PostgreSQL
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
| `AgentRun` / `agent_runs` | Agent run for a ticket: status, provider/model, steps, tool trace, token usage, structured result or safe error |
| `AgentSuggestion` / `agent_suggestions` | Suggested reply and the employee's review decision; the agent creates only `pending` |

Foreign keys maintain record ownership. Database checks restrict statuses, priorities, date ranges, message authors, and suggestion review fields. Indexed foreign keys support joins. Identity keys are `BIGINT`; recorded timestamps include time-zone information.

Knowledge documents store metadata rather than a second copy of future Markdown content. Suggestions reach their ticket through `agent_runs.ticket_id`, avoiding a duplicate ticket reference. Since Stage 05 the agent writes these tables; approving or rejecting a suggestion is not implemented yet. Employee roles are data constraints and do not enforce access control.

`updated_at` is initialized by PostgreSQL and updated by SQLAlchemy when it issues an update for the mapped record. Direct SQL updates must set it explicitly; this stage creates no timestamp trigger.

## Business API and transaction boundaries

Stage 03 adds `app/schemas`, `app/services`, and `app/api` while keeping the existing schema. No new migration is needed: the nine Stage 02 models already describe the stored fields.

Pydantic create schemas trim strings, reject empty required fields and unknown fields, validate email addresses, restrict enum values, and require positive identifiers and time-zone-aware license dates. Read schemas explicitly describe public response fields and read attributes from SQLAlchemy objects. Request clients cannot assign database IDs, creation timestamps, or the initial ticket status.

Twelve operations create, list, and read one record for each of `/api/customers`, `/api/products`, `/api/licenses`, and `/api/tickets`. List queries order by ID and bound pagination to at most 100 results. License and ticket queries support documented filters. A filter with no matching records returns `[]`.

HTTP handlers call services rather than embedding business rules. Services check referenced records, add data, commit explicitly, and refresh the saved object before serialization. The session dependency rolls back failed transactions and closes sessions. Duplicate unique values return HTTP 409; missing records or creation references return 404; invalid request data returns 422. Database and configuration failures return a generic 503 without revealing URLs, SQL statements, or parameters.

Ticket creation always uses `open`; later status transitions are outside this stage. License issuance defaults to current UTC time when omitted, and expiry cannot precede issuance. A ticket references its customer and product; no ticket-to-license column or automatic license entitlement policy is invented. Later agent tools can inspect a license independently.

The [Stage 03 guide](STAGE_03.md) explains each schema, service, handler, HTTP request, and error. Every new function has a Russian docstring; inline comments explain transaction and validation decisions. This remains a local learning application without authentication, editing, deletion, or AI execution.

## LLM draft preview

Stage 04 adds `GET /api/llm/status` and `POST /api/tickets/{resource_id}/draft`. The status endpoint reads configuration without SQL or a provider request. `configured` reflects settings presence, not key validity, model access, quota, or provider availability.

The draft service checks the ticket and product, copies only ticket ID, subject, description, priority, product name, and product code into a standalone context object, then ends the read transaction before contacting a provider. The connection returns to the PostgreSQL pool during the potentially slow call. It does not load the customer or licenses. Arbitrary text inside the ticket may still contain sensitive data; no automatic redaction is claimed.

A separate LLM settings class prevents invalid model configuration from breaking database settings or `/health`. Mock mode is the default and explicitly labels its deterministic demonstration reply. OpenAI mode requires both a secret key and a model chosen in `.env`. The SDK client is created for one request and always closed. It uses Responses with a strict Pydantic output schema, a configured network timeout and output token ceiling, no automatic retries, `store=False`, and disabled context truncation. No temperature parameter is assumed to work across all models.

System instructions are separate from JSON ticket data. They require a Russian draft for human review, forbid treating ticket text as instructions, and prohibit invented claims about checked licenses, logs, or documentation. These instructions are not evidence that a real model is immune to prompt injection or produces correct technical advice.

The response contains summary, category, draft reply, provider, actual response model, optional token usage, and a server-owned `requires_human_review: true`. A serialized context above 16000 characters is rejected rather than truncated; this character bound is distinct from model token limits. Refusal, incomplete/invalid output, timeout, configuration, and provider failures map to safe HTTP errors. No draft, agent run, message, status change, or automatic reply is saved or sent in this stage.

The actual OpenAI SDK parser is exercised with a substituted HTTP transport. CI never requires a key or sends paid requests. Live OpenAI validation remains pending until credentials and an accessible model are configured. The [Stage 04 guide](STAGE_04.md) documents each file, mock verification, explicit live setup, and these verification limits.

## Agent with read-only tools

Stage 05 adds `POST /api/tickets/{resource_id}/analyze` and `GET /api/agent-runs/{resource_id}` (`app/agent`, `app/services/agents.py`, `app/api/agents.py`). The optional request body accepts only a `license_key` hint. The server builds the access scope — ticket, customer, and product IDs — from the stored ticket; the model cannot replace it.

`AgentRuntime` alternates provider turns and tool execution. Each turn must contain either function calls or a final `DraftContent`, never both. The whole batch is validated before the first tool runs: unique call IDs, the global tool-call budget, and no reused IDs. Limits are `AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`, and `AGENT_TIMEOUT_SECONDS` (a monotonic deadline for the whole loop), plus fixed character bounds for the transcript (64000), tool arguments (4096), and tool results (16000). Exceeding a bound stops the run instead of truncating silently.

The four tools are `get_ticket`, `get_customer` (without email), `get_license` (only within the ticket's customer and product; `valid` also checks issue and expiry dates), and `get_ticket_history` (up to five other tickets for the same customer and product). Arguments are parsed by strict Pydantic schemas that reject unknown fields, type coercion, and duplicate JSON keys. Invalid calls return a structured tool error to the model and still consume the budget. Each tool opens its own short session with `SET TRANSACTION READ ONLY` and statement/lock timeouts, so PostgreSQL itself rejects writes. The trace stores validated arguments with license keys masked.

The service never holds a database connection while waiting for the provider. It reads the ticket, ends that transaction, commits a `running` run, executes the loop, and then saves either `completed` with the result, trace, usage, and exactly one `pending` suggestion, or `failed` with a constant safe message and partial progress — each in one transaction. Errors after the run starts return `run_id`. A process crash during the final commit may leave a run `running`; there is no recovery job yet.

The mock provider performs three deterministic steps with the real tools and labels its reply as a demonstration. The OpenAI adapter uses the Responses API with strict function schemas, `store=False`, no retries, disabled truncation, and replays all output items, including `reasoning.encrypted_content`. It is tested with the real SDK and a substituted HTTP transport; no live model request has been made. Migration `0002_agent_runtime` adds the run fields with backward-compatible defaults. The [Stage 05 guide](STAGE_05.md) documents the full flow.

## Schema lifecycle and local startup

The first migration explicitly creates the nine tables, constraints, and indexes. It is a frozen description of that schema. Editing a model does not rewrite an already released migration; later schema changes require another revision. The second revision, `0002_agent_runtime`, adds agent run fields and CHECK constraints while preserving existing rows. Runtime startup never calls `Base.metadata.create_all()`.

Compose uses one application image for migration and API services:

```text
PostgreSQL healthy → alembic upgrade head succeeds → API passes /ready
        db                      migrate                     api
```

The PostgreSQL named volume retains development records after normal `docker compose down` and later startup. On the host, the default database port is `5433`, and the Compose API is available on `8001`. Services inside Docker use `db:5432`; host Python uses `127.0.0.1:5433`. These separate URLs are shown in `.env.example`.

For development with automatic reload, only PostgreSQL needs to run in Docker. Apply migrations using host Python, then run Uvicorn on `127.0.0.1:8000`. The [README](../README.md) gives both startup paths.

`python -m app.db.demo seed` explicitly adds fictional customer, product, and license records in one transaction. Repeated calls reuse their unique keys and preserve existing values. `show` reads a stable JSON snapshot from a new session without writing. Comparing snapshots across `docker compose down` without `--volumes`, followed by `up`, demonstrates that records survive container removal and recreation in the named volume. The second snapshot comes from `show`; no second seed recreates missing records.

## Verification and database isolation

Unit tests cover the HTTP contract, safe readiness failures, configuration, engine/session behavior, and the smoke CLI. Real PostgreSQL integration tests apply migrations, compare models with the resulting schema, exercise records and constraints, and check transaction behavior and migration rollback. Agent integration tests cover saved runs and suggestions, released connections during provider turns, failed runs with partial progress, tool scope, read-only tool transactions, and upgrading Stage 02 data to the agent schema.

The integration fixtures reset their schema between scenarios. They require an explicit `TEST_DATABASE_URL` pointing to a database whose name ends in `_test`; they never reuse `DATABASE_URL` as a fallback. Local runs without a test URL report skipped integration tests. A missing URL in CI is an error.

Compose provides a separate `db-test` service behind its `test` profile, on host port `5434` by default. It stores disposable test data in memory rather than the development volume. The [Stage 02 guide](STAGE_02.md) explains how to start it and run all tests.

Run lint, formatting, and the full test suite using the commands in the [README](../README.md). Successful verification must also include actual startup, a successful `/ready`, and reading saved records after removing and recreating the Compose containers while retaining the database volume. The [development log](DEVELOPMENT_LOG.md) records implementation steps and verification evidence. This design document makes no claims about agent quality, retrieval accuracy, latency, or cost; those require future functionality and evaluation scenarios.

## CI/CD and Docker packaging

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests targeting `main`, and manual dispatch. The Python job installs locked dependencies, starts its PostgreSQL 17 service, applies migrations, checks schema drift with `alembic check`, runs Ruff lint and formatting checks, and executes all Pytest tests against the explicit test database. The independent lint, formatting, and test steps still report their outcomes if another check fails.

After that job succeeds, the container job builds one runtime image and starts the Compose database, migration, and API services with `--no-build`. `scripts/smoke_test.py --check-agent` validates `/health`, `/ready`, `/docs`, and `/openapi.json`, including the documented readiness operation. The additional `--check-api` flag validates all twelve business operations in OpenAPI and reads the four resource lists with `limit=1`; it also enables database readiness checks and performs no writes. With `--check-llm`, a status guard requires configured mock before any POST. The smoke explicitly creates three fictional fixture records, requests a preview, and verifies the ticket remains unchanged. `--check-agent` includes these checks, adds a fictional license, runs the mock agent, checks the four-tool trace and pending suggestion, and reads the same run back through GET. CI fixes mock mode and supplies no OpenAI credentials. An unexpected HTTP 503 fails immediately. The job then seeds demonstration records, runs `docker compose down` without `--volumes`, starts the stack again, reads the saved snapshot and the saved agent run, compares both with the originals, and runs the HTTP smoke again. This removes and recreates containers while preserving the named database volume. Failure logs include all Compose services; cleanup removes the isolated CI stack and its volume.

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

The interface will communicate through the API. Server-side services enforce the data scope and expose a restricted set of agent tools. The agent prepares a suggestion for human review. LangGraph will be introduced on top of the basic tool-calling loop implemented in Stage 05.

PostgreSQL, its schema, create/read REST business operations, LLM draft previews, and the bounded agent with read-only tools are implemented. Qdrant, document retrieval, authentication, approving or rejecting suggestions, and the operator interface remain future work.

## Next step

Stage 06 will add a knowledge base with embeddings and Qdrant search, so the agent can cite documentation sources in its draft. Human review decisions, LangGraph orchestration, and the operator interface follow the roadmap.
