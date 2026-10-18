# Project structure

[← README](../../README.md) · [All documentation](../../README.md#documentation)

Where each part of the application, tests, and documentation lives.

```text
.github/workflows/
  ci.yml                 # Python/database checks, Compose and GHCR delivery
app/
  main.py                # /health, /ready, API lifecycle and router registration
  api/
    routes.py            # Thin HTTP handlers for five resources
    agents.py            # Agent analysis and saved-run routes
    knowledge.py         # Knowledge search and index status routes
    reviews.py           # Suggestion queue, review decision and workflow state
    dependencies.py      # Request sessions and safe database errors
  schemas/               # Validated resources, drafts and agent runs
  llm/                   # Independent settings, mock/OpenAI clients and safe errors
  agent/                 # Agent limits, loop, read-only tools and mock/OpenAI providers
  knowledge/             # Article parsing, embeddings, Qdrant index and ingest CLI
  workflow/              # LangGraph review graph, PostgreSQL checkpointer, service
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
  versions/              # Stage 02 schema, 05 agent runs, 06 index registry, 07 review
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
  test_workflow.py       # LangGraph graph on an in-memory checkpointer
  test_review_api.py     # Review and staff HTTP validation
  conftest.py            # In-memory checkpointer for every test by default
scripts/
  smoke_test.py          # Health, readiness, API, knowledge, draft, agent and review checks
  check_project.py       # Docstrings, Markdown links, Bash and JSON examples
  build_guide.py         # Reproducible Russian PDF guide; explicit CLI only
docs/
  instructions/          # Quickstart, API usage, testing, CI and this file
  guide/                 # PDF guide, build instructions and verification evidence
  APPROACH.md            # Current design and future architecture
  CI_CD.md               # CI/CD guide in Russian
  STAGE_01.md            # FastAPI learning guide in Russian
  STAGE_02.md            # Database learning guide in Russian
  STAGE_03.md            # Business API learning guide in Russian
  STAGE_04.md            # LLM integration learning guide in Russian
  STAGE_05.md            # Agent with tools learning guide in Russian
  STAGE_06.md            # Knowledge base and RAG learning guide in Russian
  STAGE_07.md            # LangGraph workflow and human review guide in Russian
  DEVELOPMENT_LOG.md     # Step-by-step implementation log
compose.yaml             # PostgreSQL, Qdrant, migration, API and isolated test services
alembic.ini              # Migration configuration without a stored database URL
.env.example             # Local development settings
Dockerfile               # Runtime image with application and migrations
.dockerignore            # Excludes local environments, secrets and caches
pyproject.toml           # Metadata, dependencies and tool settings
uv.lock                  # Resolved dependency versions
README.md                # Short description, roadmap and documentation index
```
