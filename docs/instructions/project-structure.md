# Project structure

[← README](../../README.md) · [All documentation](../../README.md#documentation)

Where each part of the application, tests, and documentation lives.

```text
.github/workflows/
  ci.yml                 # Checks on Linux/macOS/Windows, Docker (amd64, arm64), GHCR delivery
  dependency-updates.yml # Weekly dependency PR with the 14-day cutoff
.gitattributes           # LF line endings on every operating system
app/
  main.py                # /health, /ready, API lifecycle and router registration
  api/
    routes.py            # Thin HTTP handlers for five resources
    agents.py            # Agent analysis and saved-run routes
    knowledge.py         # Knowledge search and index status routes
    reviews.py           # Suggestion queue, review decision and workflow state
    operator.py          # Dashboard, run history, ticket messages and status for the UI
    auth.py              # Current employee, staff tokens: issue, list, revoke
    security.py          # Bearer token check, role ladder and write rate limit
    rate_limit.py        # In-process token bucket per employee
    dependencies.py      # Request sessions and safe database errors
  schemas/               # Validated resources, drafts and agent runs
  llm/                   # Independent settings, mock/OpenAI clients and safe errors
  agent/                 # Agent limits, loop, read-only tools and mock/OpenAI providers
  knowledge/             # Article parsing, embeddings, Qdrant index and ingest CLI
  workflow/              # LangGraph review graph, PostgreSQL checkpointer, service
  services/              # Business checks, queries and explicit transactions
                         # run_recovery.py finishes abandoned running analyses
                         # auth.py issues, checks and revokes hashed staff tokens
                         # privacy.py erases a customer and applies ticket retention
  config.py              # DATABASE_URL and the write rate limit from env or .env
  tokens.py              # CLI: python -m app.tokens issue | list | revoke
  privacy.py             # CLI: python -m app.privacy erase | retention
  console.py             # UTF-8 output for command-line tools on any OS
  observability.py       # JSON log, X-Request-ID middleware, safe error fields
  db/
    base.py              # DeclarativeBase and constraint naming
    session.py           # Lazy engine and request-scoped sessions
    demo.py              # Explicit idempotent seed and read-only show
  models/
    entities.py          # Ten relational models, including hashed API tokens
migrations/
  env.py                 # Alembic execution and database URL handling
  versions/              # Stage 02 schema, 05 agent runs, 06 index registry, 07 review,
                         # 0005 staff tokens and the reviewer role,
                         # 0006 erasure and retention stamps
knowledge/               # Fictional Markdown support articles for the knowledge base
operator_ui/
  main.py                # Streamlit entry point: streamlit run operator_ui/main.py
  app.py                 # Pages: overview, tickets, review queue, knowledge, catalogs
  client.py              # HTTP client with timeouts, whitelisted routes and safe errors
evaluations/
  run.py                 # Offline evaluation CLI: JSON report and threshold gates
  retrieval.py           # Labelled questions, Recall@k, MRR@k, no-hit and scope checks
  agent_contract.py      # Agent safety scenarios on the real runtime without SQL
  *_cases.jsonl          # 32 retrieval questions and 12 agent scenarios
  thresholds.json        # Gates fixed before the first measurement
tests/
  integration/           # Real PostgreSQL, migrations and complete HTTP workflows
                         # (test_backup_restore.py: backup → wipe → restore on db-test/qdrant-test;
                         # test_privacy_erasure.py: erasure and retention, every table searched)
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
  test_security_api.py   # 401 on every /api route, roles, rate limit, token checks
  test_operator_*.py     # Operator API, UI HTTP client and Streamlit AppTest
  test_evaluations.py    # Metrics, labels and evaluation CLI exit codes
  test_observability.py  # Request ids, JSON log lines, no secrets or SQL in logs
  test_image_pins.py     # Every third-party image pinned by the same digest
  test_dependency_age.py # Release age and exact documented security exceptions
  test_security_scenarios.py  # Agent scenarios and deliberate-regression checks
  test_demo_scenario.py  # Demo script against a fake HTTP API
  test_local_start.py    # Real Uvicorn and Streamlit start on the current OS
  test_run_recovery.py   # Abandoned run recovery and its background monitor
  test_release_delivery.py    # Release tags, image export and GHCR publication
  test_update_dependencies.py # Weekly dependency PR preparation and writer
  test_project_checks.py # Docstrings, links, Bash and PowerShell examples
  test_backup.py         # Backup, verify and restore with fake Docker and fake Qdrant
  test_privacy.py        # Trace and result scrubbing, erase API contract, privacy CLI
  conftest.py            # In-memory checkpointer and a test admin for unit tests
                         # (integration tests send real tokens instead)
scripts/
  smoke_test.py          # Health, readiness, API, knowledge, draft, agent, review, erasure
  check_project.py       # Docstrings, Markdown links, Bash, PowerShell and JSON examples
  check_local_start.py   # Start API and UI as real processes on this computer
  release_delivery.py    # Validate release tags, export and publish tested images
  update_dependencies.py # Prepare and publish the weekly dependency PR
  demo_scenario.py       # Idempotent mock demo through the HTTP API
  check_dependency_age.py # Release-age policy for uv.lock and exclude-newer
  build_guide.py         # Reproducible Russian PDF guide; explicit CLI only
  backup.py              # Backup, verify and restore PostgreSQL + Qdrant of a Compose stack
docs/
  instructions/          # Quickstart, Windows, API usage, testing, backup, privacy, CI, this file
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
  STAGE_08.md            # Streamlit operator interface guide in Russian
  STAGE_09.md            # Evaluations and security checks guide in Russian
  STAGE_10.md            # Packaging and reproducible demo guide in Russian
  DEMO.md                # 5–7 minute demonstration walkthrough
  RUN_RECOVERY.md        # Recovery of abandoned running analyses
  RELEASES.md            # Version tags and publication of the API and UI images
  DEPENDENCY_UPDATES.md  # Weekly dependency PRs and the 14-day rule
  VERIFICATION.md        # Checks actually run and their limits
  DEVELOPMENT_LOG.md     # Step-by-step implementation log
compose.yaml             # PostgreSQL, Qdrant, migration, API, UI and isolated test services
alembic.ini              # Migration configuration without a stored database URL
.env.example             # Local development settings
Dockerfile               # API runtime image (default) and separate UI target
.dockerignore            # Excludes local environments, secrets, backups and caches
backups/                 # Default folder for backups; ignored by Git and Docker, keep copies elsewhere
pyproject.toml           # Metadata, dependencies and tool settings
uv.lock                  # Resolved dependency versions
README.md                # Short description, roadmap and documentation index
```
