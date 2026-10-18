# AI Support Agent

**Начни с [инструкции этапа 06 на русском](docs/STAGE_06.md):**
база знаний, эмбеддинги, поиск в Qdrant и ссылки агента на найденные статьи.
[Этап 05](docs/STAGE_05.md) объясняет агента с инструментами и сохранённый запуск.
[Этап 04](docs/STAGE_04.md) объясняет предварительный черновик, mock и настройку OpenAI.
[Этап 03](docs/STAGE_03.md) объясняет создание и чтение записей через API.
[Этап 02](docs/STAGE_02.md) объясняет PostgreSQL, модели, миграции и запуск.
[Журнал разработки](docs/DEVELOPMENT_LOG.md) объясняет каждый выполненный шаг;
[инструкция этапа 01](docs/STAGE_01.md) разбирает основу FastAPI.

A Python learning project for a technical support assistant. The future product will help an operator investigate tickets, check customer licenses, search documentation, and review suggested replies before simulated delivery.

The current implementation covers **Stages 01–06**: FastAPI, PostgreSQL 17, Qdrant 1.19, nine SQLAlchemy models, three Alembic migrations, twelve business API operations, database readiness, Docker Compose, and automated checks. The GitHub Actions workflow checks the application and database before delivering the tested Docker image to GHCR on successful pushes to `main`. The API creates, lists, and reads customers, products, licenses, and tickets. Draft previews support a local mock and an explicitly configured OpenAI provider. A knowledge base of fictional Markdown articles is indexed into Qdrant and searchable through the API. A bounded agent reads the ticket, customer, license, and ticket history, searches the knowledge base through five read-only tools, and saves its run with a `pending` reply suggestion that cites only articles it actually found. LangGraph orchestration and human review decisions are the next stage.

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

`cp -n` preserves an existing `.env`. When reusing one, compare its settings with `.env.example`. The example contains credentials for local development. Compose starts PostgreSQL and Qdrant, waits for them to become healthy, applies Alembic migrations, and starts the API. Its final health check requires both a database connection and the current migration revision.

Index the knowledge base articles from `knowledge/` (explicit and idempotent):

```bash
docker compose exec -T api python -m app.knowledge.ingest
```

Default addresses:

- [API readiness](http://127.0.0.1:8001/ready): `{"status":"ok","database":"ok"}`.
- [Application health](http://127.0.0.1:8001/health): `{"status":"ok","service":"ai-support-agent"}`.
- [Interactive API documentation](http://127.0.0.1:8001/docs).
- PostgreSQL for host tools: `127.0.0.1:5433`.
- Qdrant for host tools: `http://127.0.0.1:6335`.

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

## Search the knowledge base

`python -m app.knowledge.ingest` reads `knowledge/*.md`, splits each article into section chunks, computes embeddings, writes them to the Qdrant collection `support_knowledge`, and records each article in the PostgreSQL table `knowledge_documents` (checksum, chunk count, embedding signature). Unchanged articles are skipped; changed articles are reindexed and their old extra chunks removed; articles deleted from the folder are removed from Qdrant and the registry.

```bash
curl --silent --show-error --fail-with-body http://127.0.0.1:8001/api/knowledge/status
curl --silent --show-error --fail-with-body --request POST \
  http://127.0.0.1:8001/api/knowledge/search --header 'Content-Type: application/json' \
  --data '{"query":"Сервер не запускается после обновления","product_code":"techflow-server","limit":3}'
```

The status shows the embedding provider, collection state (`ready`, `missing`, `incompatible`, `unavailable`, or `not_configured`), Qdrant points, and registered articles. Search returns chunks with `document_key`, `title`, `section`, `text`, cosine `score`, `source_path`, and `scope`. With `product_code`, results are limited to that product's articles plus general articles.

The default `EMBEDDING_PROVIDER=mock` builds a local lexical vector from word stems: it needs no network or key and matches shared words, not meaning. Results below `KNOWLEDGE_MIN_SCORE` (0.2) are dropped. An OpenAI embeddings adapter (`EMBEDDING_PROVIDER=openai` with an explicit `OPENAI_EMBEDDING_MODEL`) is tested only with a substituted HTTP transport. Every point carries an embedding signature such as `mock:mock-hash-v1:256`, and search ignores vectors from other models; reindex after switching providers.

## Analyze a ticket with the agent

`POST /api/tickets/{resource_id}/analyze` runs a bounded tool-calling loop for an existing ticket and returns HTTP 201 with the saved run. The optional JSON body accepts only `license_key`; other fields return 422. The agent can call five read-only server functions: `get_ticket`, `get_customer` (no email), `get_license` (only within the ticket's customer and product, with a date-aware `valid` flag), `get_ticket_history` (up to five other tickets of the same customer and product), and `search_knowledge` (the ticket's product articles plus general articles; the server sets the product scope). Model-provided names and arguments are validated on the server and never become SQL or Python code; tool transactions are `READ ONLY` in PostgreSQL.

A successful run stores `completed`, the tool trace (license keys masked as `***`), the structured `summary`/`category`/`draft_reply`/`sources`, and exactly one `pending` suggestion. `sources` may contain only article keys returned by `search_knowledge` in the same run; any other citation stops the run with 502. If the knowledge base is unavailable, the tool returns an error and the agent answers without sources. The agent never changes the ticket or sends a reply. `GET /api/agent-runs/{resource_id}` reads the saved run without calling the model again. Every POST creates a new run.

Limits apply to both mock and OpenAI: `AGENT_MAX_STEPS` (default 6, at most 8), `AGENT_MAX_TOOL_CALLS` (default 12, at most 20), and `AGENT_TIMEOUT_SECONDS` (default 60, at most 120). A limit stop, timeout, or provider error after the run has started returns its safe error together with `run_id`; the run is saved as `failed` with partial progress and no suggestion. The mock agent needs three steps and five tool calls when a license key is given.

Check the whole agent path in a running mock stack:

```bash
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 60 --check-agent
```

`--check-agent` includes `--check-llm` and the read-only `--check-knowledge` (which requires an indexed knowledge base) and additionally creates a fictional license, an agent run, and a suggestion that must cite a found article. The [Stage 06 guide](docs/STAGE_06.md) covers the knowledge base; the [Stage 05 guide](docs/STAGE_05.md) walks through each request, the saved trace, error handling, migration `0002_agent_runtime`, and the OpenAI adapter, which is tested only with a substituted HTTP transport.

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
docker compose --profile test up --detach --wait db-test qdrant-test
TEST_DATABASE_URL='postgresql+psycopg://support:support_ci@127.0.0.1:5434/ai_support_test' \
TEST_QDRANT_URL=http://127.0.0.1:6336 \
  uv run --locked pytest -v
```

Integration fixtures reset the schema through Alembic. They accept only an explicitly provided `TEST_DATABASE_URL` whose database name ends in `_test`; they never fall back to the development `DATABASE_URL`. Knowledge tests use only an explicit `TEST_QDRANT_URL` and create and delete their own `test_*` collections. In CI, a missing test URL fails the checks. Unit tests use an in-process Qdrant (`:memory:`) and need neither service.

To inspect the development migration state separately:

```bash
uv run --locked alembic current
uv run --locked alembic check
```

## CI and delivery to GHCR

The workflow runs for pushes to `main`, pull requests targeting `main`, and manual checks:

```text
Locked dependencies + PostgreSQL + Qdrant + migrations + Ruff + all Pytest tests
                                  ↓
Docker build + Compose readiness + knowledge indexing + HTTP smoke + persistence check
                                  ↓
Publish the tested image to GHCR (push to main only)
```

The Python job applies migrations to a separate PostgreSQL test database and runs `alembic check` to detect differences between the schema and models. The container job builds once, starts `db → migrate`, `qdrant → api`, indexes the knowledge base, checks health/readiness, the business API, knowledge search, a mock draft, and a saved mock agent run with sources, and compares saved demo records, that agent run, and search results before and after removing and recreating the Compose containers while retaining the PostgreSQL and Qdrant volumes. A second indexing run must report no changes. CI explicitly selects mock with no OpenAI credentials. The draft and agent smoke checks create disposable fixture records only after confirming mock configuration. A failed check blocks publication. The publication job receives that same image as an artifact and does not rebuild it.

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
    agents.py            # Agent analysis and saved-run routes
    knowledge.py         # Knowledge search and index status routes
    dependencies.py      # Request sessions and safe database errors
  schemas/               # Validated resources, drafts and agent runs
  llm/                   # Independent settings, mock/OpenAI clients and safe errors
  agent/                 # Agent limits, loop, read-only tools and mock/OpenAI providers
  knowledge/             # Article parsing, embeddings, Qdrant index and ingest CLI
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
  versions/              # Stage 02 schema, Stage 05 agent runs, Stage 06 index registry
knowledge/               # Fictional Markdown support articles for the knowledge base
tests/
  integration/           # Real PostgreSQL, migrations and complete HTTP workflows
  test_health.py         # Standalone API behavior
  test_readiness.py      # Database readiness behavior
  test_config.py         # Environment configuration
  test_smoke_test.py     # Real HTTP smoke CLI regression tests
  test_llm.py            # Real SDK parser with a substituted HTTP transport
  test_drafts_api.py     # Draft/status HTTP behavior without paid requests
  test_agent_*.py        # Agent loop, tools and providers without network
  test_agents_api.py     # Agent HTTP contract and safe errors
  test_knowledge_*.py    # Articles, embeddings, in-memory Qdrant index and search API
scripts/
  smoke_test.py          # Health, readiness, business API, knowledge, draft and agent checks
docs/
  APPROACH.md            # Current design and future architecture
  CI_CD.md               # CI/CD guide in Russian
  STAGE_01.md            # FastAPI learning guide in Russian
  STAGE_02.md            # Database learning guide in Russian
  STAGE_03.md            # Business API learning guide in Russian
  STAGE_04.md            # LLM integration learning guide in Russian
  STAGE_05.md            # Agent with tools learning guide in Russian
  STAGE_06.md            # Knowledge base and RAG learning guide in Russian
  DEVELOPMENT_LOG.md     # Step-by-step implementation log
compose.yaml             # PostgreSQL, Qdrant, migration, API and isolated test services
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
| 05 | Tool calling and the agent execution loop | Implemented; mock agent saves runs and pending suggestions, live OpenAI validation requires credentials |
| 06 | RAG, embeddings, and Qdrant document search | Implemented; mock embeddings, product-scoped search and agent citations, live OpenAI embeddings require credentials |
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
- [Stage 05 learning guide](docs/STAGE_05.md)
- [Stage 06 learning guide](docs/STAGE_06.md)
- [Development log](docs/DEVELOPMENT_LOG.md)
- [CI/CD and Docker guide](docs/CI_CD.md)

TechFlow and the support scenarios are fictional. The default mock mode requires no LLM API key; OpenAI mode requires an explicit key and model. The business API persists customer, product, license, and ticket records. The draft preview is read-only; the agent saves runs and `pending` suggestions that cite knowledge base articles found in Qdrant. Approving or rejecting suggestions, authentication, and an operator interface remain future work.
