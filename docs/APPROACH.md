# AI Support Agent — Solution Approach

## Problem and product goal

Support operators need customer information, license details, and technical documentation to investigate a ticket. The planned application will collect this information, prepare an answer supported by sources, and let an operator approve it before simulated delivery.

The project uses fictional TechFlow products and artificial support data.

## Current implementation: Stage 01

The first stage establishes a small, runnable API and a way to check its behavior.

```text
HTTP client → Uvicorn → FastAPI application → JSON response
```

`app/main.py` creates the FastAPI application and defines `GET /health`. The endpoint returns the service name and a fixed `ok` status. FastAPI exposes interactive documentation at `/docs` and an OpenAPI schema at `/openapi.json`.

`tests/test_health.py` checks the API through FastAPI's test client. These tests do not require a separately running development server.

`pyproject.toml` declares Python requirements, dependencies, and tool configuration. `uv.lock` records resolved dependencies. `uv sync --locked` prepares the local `.venv` and checks that the lockfile matches the project configuration.

## Why this first-stage stack

| Component | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.12+ | Shared foundation for the API and later agent code |
| API | FastAPI | HTTP routing, JSON responses, generated OpenAPI documentation |
| ASGI server | Uvicorn | Runs the application and handles local HTTP connections |
| Dependency management | uv | Creates an isolated environment and synchronizes locked dependencies |
| Testing | Pytest with FastAPI TestClient and HTTPX | Exercises HTTP behavior inside the test process |
| Code checks | Ruff | Checks formatting and common code issues |

The endpoint is small enough to keep in `main.py`. Service and repository layers will be introduced when there is business logic and database access to separate.

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

This is a development plan. PostgreSQL, Qdrant, LLM integration, agent tools, authentication, human review, and the operator interface are future stages.

## Verification

Use the commands in the [README](../README.md) to run automated API tests, lint checks, and formatting checks. Also start the development server and open `/health` and `/docs` to check the HTTP application.

Readiness criteria for this stage are a running server, accessible API documentation, and a passing automated health test. This document does not record measured agent quality, retrieval accuracy, latency, or cost; those measurements require later functionality and evaluation scenarios.

Local verification on 2026-10-17 with Python 3.12.14:

- `pytest`: 1 test passed. Starlette also emitted a deprecation warning for its HTTPX compatibility adapter.
- Ruff lint and formatting checks passed.
- `uv sync --locked --offline` confirmed the environment matches the lockfile.
- A running Uvicorn server returned HTTP 200 for `/health`, `/docs`, and `/openapi.json`; the health response and documented operation were checked.

## Current limitations

- `/health` returns a fixed application status. It does not check databases, external APIs, or infrastructure readiness.
- Data is not stored between requests. There are no customer, license, or ticket operations yet.
- The development server listens on the local loopback address. Authentication and deployment configuration are later work.
- There is no AI reasoning, document retrieval, or message delivery at this stage.

## Next step

Stage 02 will add PostgreSQL, SQLAlchemy models, and Alembic migrations. Its acceptance criterion is persistent data with a schema created through migrations.
