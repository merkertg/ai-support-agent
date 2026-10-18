# AI Support Agent — Solution Approach

## Problem and product goal

Support operators need customer information, license details, and technical documentation to investigate a ticket. The application collects this information, prepares an answer supported by sources, and lets an operator approve it before simulated delivery.

The project uses fictional TechFlow products and artificial support data.

## Current implementation: Stages 01–10

The application now has a runnable HTTP foundation and a persistent relational schema. Its database operations include readiness, explicit demonstration commands, and create/read REST operations for customers, products, licenses, and tickets. An LLM client produces a structured draft preview from an existing ticket without modifying stored records. Fictional Markdown support articles are indexed into Qdrant and searchable by product. A bounded agent gathers facts through five read-only tools, including knowledge search, and saves its run together with a pending reply suggestion that cites only the articles it found. A LangGraph workflow pauses until an employee approves or rejects the suggestion and survives API restarts through PostgreSQL checkpoints.

```text
HTTP client → Uvicorn → FastAPI
                         ├─ GET /health → application status
                         ├─ GET /ready  → PostgreSQL + Alembic revision check
                         ├─ /api/* → routers → services → PostgreSQL
                         ├─ POST /api/tickets/{id}/analyze → LangGraph: analyze node
                         │      └─ agent runtime
                         │             ├─ provider (mock / OpenAI Responses)
                         │             ├─ read-only tools → PostgreSQL
                         │             └─ search_knowledge → embeddings → Qdrant
                         │      → human_review: interrupt, checkpoint in PostgreSQL
                         ├─ POST /api/suggestions/{id}/review → resume → deliver | reject
                         ├─ /api/knowledge/* → embeddings → Qdrant
                         └─ /docs and /openapi.json → API documentation

Explicit demo CLI → SQLAlchemy session → PostgreSQL
Explicit ingest CLI → knowledge/*.md → Qdrant points + PostgreSQL registry
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
| Vector database | Qdrant 1.19 with qdrant-client | Article chunk vectors, payload filters and similarity search |
| Workflow | LangGraph 1.2 with langgraph-checkpoint-postgres | Review process graph, human-in-the-loop interrupt and durable checkpoints |
| Database access | SQLAlchemy 2 with psycopg 3 | Typed models, connections, sessions and transactions |
| Schema changes | Alembic | Ordered schema creation, comparison and explicit rollback |
| Configuration | pydantic-settings | Environment and `.env` loading with validation |
| Dependency management | uv | Isolated environment and locked dependencies |
| Testing | Pytest, TestClient/HTTPX, real PostgreSQL and Qdrant | HTTP behavior, configuration, transactions, migrations and search |
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
| `KnowledgeDocument` / `knowledge_documents` | Registry of indexed Markdown articles: key, source path, product code, checksum, chunk count and embedding signature |
| `AgentRun` / `agent_runs` | Agent run for a ticket: status, provider/model, steps, tool trace, token usage, structured result or safe error, LangGraph thread id |
| `AgentSuggestion` / `agent_suggestions` | Suggested reply and the employee's decision: final text, comment, reviewer, time, and the delivered message |

Foreign keys maintain record ownership. Database checks restrict statuses, priorities, date ranges, message authors, and suggestion review fields. Indexed foreign keys support joins. Identity keys are `BIGINT`; recorded timestamps include time-zone information.

Knowledge documents store metadata rather than a second copy of the Markdown content; article text and vectors live in Qdrant. Suggestions reach their ticket through `agent_runs.ticket_id`, avoiding a duplicate ticket reference. Since Stage 05 the agent writes these tables; since Stage 07 employees approve or reject suggestions. Employee roles are data constraints and do not enforce access control.

`updated_at` is initialized by PostgreSQL and updated by SQLAlchemy when it issues an update for the mapped record. Direct SQL updates must set it explicitly; this stage creates no timestamp trigger.

## Business API and transaction boundaries

Stage 03 adds `app/schemas`, `app/services`, and `app/api` while keeping the existing schema. No new migration is needed: the nine Stage 02 models already describe the stored fields.

Pydantic create schemas trim strings, reject empty required fields and unknown fields, validate email addresses, restrict enum values, and require positive identifiers and time-zone-aware license dates. Read schemas explicitly describe public response fields and read attributes from SQLAlchemy objects. Request clients cannot assign database IDs, creation timestamps, or the initial ticket status.

Twelve operations create, list, and read one record for each of `/api/customers`, `/api/products`, `/api/licenses`, and `/api/tickets`. List queries order by ID and bound pagination to at most 100 results. License and ticket queries support documented filters. A filter with no matching records returns `[]`.

HTTP handlers call services rather than embedding business rules. Services check referenced records, add data, commit explicitly, and refresh the saved object before serialization. The session dependency rolls back failed transactions and closes sessions. Duplicate unique values return HTTP 409; missing records or creation references return 404; invalid request data returns 422. Database and configuration failures return a generic 503 without revealing URLs, SQL statements, or parameters.

Ticket creation always uses `open`; later status transitions are outside this stage. License issuance defaults to current UTC time when omitted, and expiry cannot precede issuance. A ticket references its customer and product; no ticket-to-license column or automatic license entitlement policy is invented. Later agent tools can inspect a license independently.

The [Stage 03 guide](STAGE_03.md) explains each schema, service, handler, HTTP request, and error. Every new function has a Russian docstring; inline comments explain transaction and validation decisions. The Stage 03 resource operations do not implement editing or deletion. The current application also includes AI execution and review, as described below; authentication remains future work.

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

## Knowledge base and retrieval

Stage 06 adds `knowledge/` (seven fictional articles), `app/knowledge`, `GET /api/knowledge/status`, and `POST /api/knowledge/search`. Each article starts with a small `key: value` header (`document_key`, `title`, optional `product_code` and `product_version`); unknown or duplicated fields are rejected. The body is split by `## ` sections into chunks of at most 1200 characters; article and section titles are included in the embedded text.

`python -m app.knowledge.ingest` reconciles the folder with the index. It skips articles whose checksum, embedding signature, chunk count, and actual Qdrant point count all match the registry; reindexes the rest (deterministic UUID5 point IDs overwrite existing points, and stale tail chunks are deleted); and removes articles that disappeared from the folder, including orphan points without registry rows. Vectors are written before the registry commit, so a failed commit leads to reindexing on the next run. No SQL connection is held during embedding or Qdrant requests.

The default mock embedder hashes five-letter word stems into a signed, normalized 256-dimensional vector. It is deterministic and lexical, not semantic; `KNOWLEDGE_MIN_SCORE=0.2` drops weak matches. The OpenAI embedder calls `embeddings.create` with an explicit model and dimensions, batches of 64, no retries, and validates order, count, size, and finite values. Every point stores an embedding signature (`provider:model:dimensions`); search filters by it, and a collection with a different vector size is reported as incompatible.

Search filters by `scope`: the given product code plus `general`, or all articles without a code. The shared `ProductCode` validator reserves `general`: it cannot identify a product or a product-specific article; only omitted/NULL `product_code` creates a general article. This prevents a product article from crossing the scope boundary through the reserved marker. The agent tool `search_knowledge` takes only `query` and `limit`; the server supplies the ticket's product code after a read-only scope check and closes the SQL session before searching. It returns the best chunk per article. The final draft schema `AgentDraftContent` adds a required `sources` list, and the runtime rejects any key not returned by `search_knowledge` in the same run. Knowledge outages become a tool error, so analysis continues without sources. Migration `0003_knowledge_index` adds the registry fields. The [Stage 06 guide](STAGE_06.md) documents the flow.

## Review workflow and human in the loop

Stage 07 adds `app/workflow` (LangGraph graph, checkpointer, service), `app/services/reviews.py`, `app/api/reviews.py`, and a staff API (`/api/users`). `POST /api/tickets/{id}/analyze` starts a graph `analyze → human_review → deliver | reject` with a new `thread_id` stored in `agent_runs.workflow_thread_id`. The `analyze` node calls the unchanged Stage 05 service; a failed run ends the graph. `human_review` calls `interrupt()`: LangGraph saves the state and returns. `POST /api/suggestions/{id}/review` resumes it with `Command(resume=decision)`.

The checkpointer is `PostgresSaver` over a small psycopg pool on the same database; a connection is borrowed only for checkpoint reads and writes. Its tables come from migration `0004_review_workflow` as a frozen copy of the library DDL (indexes without `CONCURRENTLY`, versions recorded so `setup()` is a no-op); `migrations/env.py` excludes them from `alembic check`, and a test compares the copy with the installed library. Graph state holds only ids and outcomes; the license key hint is passed through LangGraph's non-persisted runtime context.

Approval stores `approved`, the reviewer, time, and `final_content`, creates a ticket message authored by the employee (simulated delivery), links it as `delivered_message_id`, and sets the ticket to `waiting_customer` — in one transaction with `SELECT … FOR UPDATE`. Rejection requires a comment and leaves the ticket unchanged. Before resuming, the service checks that the suggestion is pending, the reviewer exists and is active, the graph waits at `human_review`, and, for approval, that the ticket is not `resolved` or `closed`, so invalid requests do not consume the interrupt. The closed-ticket rule is checked again under the ticket row lock; if the status changed in between, the deliver node fails with 409 and the next request restores the wait from the database. Analysis is refused with 409 for a closed ticket, a ticket with a pending suggestion, or one with a recent `running` run, so a ticket never holds two drafts that could each be delivered. A PostgreSQL advisory lock per suggestion serializes concurrent decisions; the second gets 409. A unique review id in the resumed state detects a decision applied by another request. Suggestions created before Stage 07 have no thread and are decided directly. `reviewer_id` is not authentication. The [Stage 07 guide](STAGE_07.md) documents the flow.

## Schema lifecycle and local startup

The first migration explicitly creates the nine tables, constraints, and indexes. It is a frozen description of that schema. Editing a model does not rewrite an already released migration; later schema changes require another revision. The second revision, `0002_agent_runtime`, adds agent run fields and CHECK constraints while preserving existing rows; the third, `0003_knowledge_index`, adds the knowledge index registry fields; the fourth, `0004_review_workflow`, adds review fields and the LangGraph checkpoint tables. Runtime startup never calls `Base.metadata.create_all()`.

Compose uses one application image for migration and API services:

```text
PostgreSQL healthy → alembic upgrade head succeeds ─┐
        db                      migrate             ├→ API passes /ready
Qdrant healthy (/readyz) ───────────────────────────┘          api
       qdrant
```

The Qdrant named volume retains the index across `docker compose down`, like the PostgreSQL volume. Qdrant telemetry is disabled in Compose.

The PostgreSQL named volume retains development records after normal `docker compose down` and later startup. On the host, the default database port is `5433`, and the Compose API is available on `8001`. Services inside Docker use `db:5432`; host Python uses `127.0.0.1:5433`. These separate URLs are shown in `.env.example`.

For development with automatic reload, run PostgreSQL and Qdrant in Docker. Apply migrations and index the knowledge base using host Python, then run Uvicorn on `127.0.0.1:8000`. The [quickstart instructions](instructions/quickstart.md) give both startup paths.

`python -m app.db.demo seed` explicitly adds fictional customer, product, and license records in one transaction. Repeated calls reuse their unique keys and preserve existing values. `show` reads a stable JSON snapshot from a new session without writing. Comparing snapshots across `docker compose down` without `--volumes`, followed by `up`, demonstrates that records survive container removal and recreation in the named volume. The second snapshot comes from `show`; no second seed recreates missing records.

## Verification and database isolation

Unit tests cover the HTTP contract, safe readiness failures, configuration, engine/session behavior, and the smoke CLI. Real PostgreSQL integration tests apply migrations, compare models with the resulting schema, exercise records and constraints, and check transaction behavior and migration rollback. Agent integration tests cover saved runs and suggestions, released connections during provider turns, failed runs with partial progress, tool scope, read-only tool transactions, and upgrading Stage 02 data to the agent schema. Knowledge integration tests run against a real Qdrant via an explicit `TEST_QDRANT_URL`, each in its own temporary `test_*` collection: ingest, idempotency, changed and removed articles, recovery of lost points, HTTP search and status, agent citations, and the `0002 → 0003` upgrade. Unit tests use an in-process Qdrant. Review integration tests use a real `PostgresSaver` on the test database: approval with edited text, rejection, invalid and inactive reviewers, repeated and parallel decisions, resume with a new pool (simulated restart), legacy suggestions, CHECK constraints, and the `0003 → 0004` upgrade. Every other test uses an in-memory checkpointer from `tests/conftest.py`. Fault-injection scenarios cover checkpoint storage failure after a committed analysis or review. PostgreSQL business records remain the source of truth; reading workflow state and accepting a pending review repair stale or missing checkpoints under the review advisory lock, without repeating analysis or message delivery.

The integration fixtures reset their schema between scenarios. They require an explicit `TEST_DATABASE_URL` pointing to a database whose name ends in `_test`; they never reuse `DATABASE_URL` as a fallback. Local runs without a test URL report skipped integration tests. A missing URL in CI is an error.

Compose provides a separate `db-test` service behind its `test` profile, on host port `5434` by default. It stores disposable test data in memory rather than the development volume. The [Stage 02 guide](STAGE_02.md) explains how to start it and run all tests.

Run lint, formatting, and the full test suite using the commands in the [testing instructions](instructions/testing.md). Successful verification must also include actual startup, a successful `/ready`, and reading saved records after removing and recreating the Compose containers while retaining the database volume. The [development log](DEVELOPMENT_LOG.md) records implementation steps and verification evidence. Offline evaluations (Stage 09) measure retrieval with mock embeddings and the server-side agent contract; this document makes no claims about real LLM answer quality, latency, or cost, which have not been measured.

## CI/CD and Docker packaging

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests targeting `main`, and manual dispatch. The Python job installs locked dependencies, starts its PostgreSQL 17 and Qdrant 1.19.1 services, applies migrations, checks schema drift with `alembic check`, runs Ruff lint and formatting checks, and executes all Pytest tests against the explicit test database. The independent lint, formatting, and test steps still report their outcomes if another check fails. The same job validates workflow syntax, Python docstrings, local documentation links, and Bash/JSON example syntax. A separate blocking security job audits all locked runtime dependencies for known vulnerabilities with pip-audit, without installing those dependencies.

After both Python checks and the security audit succeed, the container job builds one runtime image, starts the Compose database, migration, Qdrant, and API services with `--no-build`, and indexes the knowledge base. `scripts/smoke_test.py --check-agent` validates `/health`, `/ready`, `/docs`, and `/openapi.json`, including the documented readiness operation. The additional `--check-api` flag validates all twelve business operations in OpenAPI and reads the four resource lists with `limit=1`; it also enables database readiness checks and performs no writes. With `--check-llm`, a status guard requires configured mock before any POST. The smoke explicitly creates three fictional fixture records, requests a preview, and verifies the ticket remains unchanged. `--check-agent` also includes the read-only `--check-knowledge` (mock embeddings, ready index, known top article, product isolation), adds a fictional license, runs the mock agent, checks the five-tool trace, the citation of a found article, and the pending suggestion, and reads the same run back through GET. CI uses `--check-review`, which additionally approves that suggestion and checks the delivered message, the ticket status, the finished workflow, and a 409 on a repeated decision. CI fixes mock mode and supplies no OpenAI credentials. An unexpected HTTP 503 fails immediately. The job then seeds demonstration records, runs `docker compose down` without `--volumes`, starts the stack again, reads the saved snapshot, the saved agent run, and the same search results, compares them with the originals, verifies that a second indexing run changes nothing, resumes a workflow that was left waiting before the recreation (`--review-run`), and runs the HTTP smoke again. This removes and recreates containers while preserving the named PostgreSQL and Qdrant volumes. Failure logs include all Compose services; cleanup removes the isolated CI stack and its volume.

The runtime image installs runtime dependencies only, runs as UID 10001, and includes Alembic configuration, migrations, and the knowledge articles. CI verifies UID 10001 and the absence of development tools in the runtime image. Its health check uses `/ready`, so a running HTTP process alone does not make the container healthy.

On a successful push to `main`, that same tested image is exported as an artifact, loaded and identified again in the publication job, and pushed to GHCR without rebuilding. Each commit receives a full SHA tag; only the current `main` tip updates `latest`. Publications queue sequentially. The workflow uses `GITHUB_TOKEN`, with `packages: write` limited to the publication job.

This implements continuous delivery of a container image. External server deployment is outside the chosen scope. Detailed operation is documented in [CI_CD.md](CI_CD.md). GitHub execution and publication must be confirmed from an actual workflow run, independently of local checks.

## Operator interface, evaluations and demo (Stages 08–10)

**Operator interface.** `operator_ui/` is a Streamlit application that talks only to the HTTP API through a small client with fixed timeouts, whitelisted routes, no redirects, and no automatic POST/PATCH retries. It needs neither `DATABASE_URL` nor model keys. Stage 08 added four read/write endpoints for it: whole-database dashboard aggregates, saved run history, ticket messages, and an explicit ticket status change. Forms mutate only on explicit submission, so a Streamlit rerun never repeats an analysis or a decision; a real-model or real-embedding call requires a fresh confirmation. The `reviewer_id` in the sidebar is an author mark, not authentication. The UI ships as a separate Docker target with only Streamlit and httpx; the API image does not contain Streamlit.

**Evaluations.** `evaluations/` builds an in-memory Qdrant index with the production indexing code and measures Recall@5, MRR@5, no-hit accuracy, and product-scope violations on 32 labelled questions, plus 12 agent scenarios that run the real `AgentRuntime` with the actual mock provider or scripted hostile turns (forged citations, prompt-injected `send_email`, foreign IDs, budget overruns). Thresholds were fixed before the first measurement, and CI fails on any violation. Sensitivity tests deliberately disable citation validation or the tool whitelist and require the suite to fail. The no-hit questions exposed mock-hash collisions, fixed by a mock-only lexical guard in `KnowledgeIndex.search`.

**Demo.** `scripts/demo_scenario.py` uses only the HTTP API and the standard library. It refuses to write unless both providers are mock and the index is ready, then creates or reuses stable fictional records and leaves one pending suggestion for a person. `--verify-review` checks both decision branches on separate tickets. Sequential reruns read existing results instead of creating new ones.

## Architecture

The implemented application follows this flow:

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

The interface communicates only through the API. Server-side services enforce the data scope and expose a restricted set of agent tools. The agent prepares a suggestion for human review. Stage 07 already uses LangGraph around the tool-calling loop from Stage 05 to pause for human review and resume with a decision.

All of these components are implemented. Authentication and roles, real message delivery, live OpenAI quality and cost measurement, publishing the UI image, and server deployment remain future work.

## Next steps

The ten learning stages are complete. Further work should start from one verifiable result at a time: a live OpenAI run on the same evaluation set once a key and budget are approved; authentication and per-operation access checks before real operators use the system; reliable delivery through a real email or helpdesk channel; and a separate deployment job with secrets, TLS, backups, and monitoring. The [demo guide](DEMO.md) lists the same steps.
