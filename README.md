# AI Support Agent

**Начни с [подробной инструкции на русском](docs/STAGE_01.md):**
запуск на этом Mac, назначение файлов, разбор функций и теста,
проверка API и решение типичных проблем.

A Python learning project for a technical support assistant. The future product will help an operator investigate tickets, check customer licenses, search documentation, and review suggested replies before simulated delivery.

The current implementation covers **Stage 01: a minimal FastAPI application**. It provides `GET /health`, interactive API documentation, and automated checks. The roadmap below describes later work.

## Requirements

- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for dependency and environment management.

Run all commands from the project root, the directory containing `pyproject.toml`.

## Run locally

Install the locked dependencies, including development tools:

```bash
uv sync --locked
```

Start the development server:

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1
```

Open:

- [Health endpoint](http://127.0.0.1:8000/health)
- [Interactive API documentation](http://127.0.0.1:8000/docs)

The health response is:

```json
{
  "status": "ok",
  "service": "ai-support-agent"
}
```

Stop the server with `Ctrl+C`.

If `.venv` has already been prepared with the project dependencies, the server can also run directly through that environment without `uv` on your shell's `PATH`:

```bash
.venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1
```

This alternative uses an existing environment; use `uv sync --locked` to create or synchronize one.

## Verify

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Stage 01 is ready when the server starts, `/docs` is available, and the automated health check passes. Run the commands above to verify your checkout.

## Project files

```text
app/
  __init__.py
  main.py                # FastAPI application and health endpoint
tests/
  test_health.py         # API checks
docs/
  APPROACH.md            # Current design and future architecture
  STAGE_01.md            # First-stage learning guide in Russian
pyproject.toml           # Project metadata, dependencies, tool settings
uv.lock                  # Resolved dependency versions
README.md
```

## Roadmap

| Stage | Work | Status |
| --- | --- | --- |
| 01 | Initialize Python project, FastAPI, health endpoint, first tests | Implemented; verify with the commands above |
| 02 | PostgreSQL, SQLAlchemy models, Alembic migrations | Planned |
| 03 | Customer, license, and ticket REST APIs | Planned |
| 04 | LLM API integration and error handling | Planned |
| 05 | Tool calling and the agent execution loop | Planned |
| 06 | RAG, embeddings, and Qdrant document search | Planned |
| 07 | LangGraph workflow and human review | Planned |
| 08 | Streamlit operator interface | Planned |
| 09 | Integration tests, agent evaluations, security checks | Planned |
| 10 | Docker Compose, CI, and GitHub demonstration materials | Planned |

## Documentation

- [Solution approach](docs/APPROACH.md)
- [Stage 01 learning guide](docs/STAGE_01.md)
- [FastAPI: first steps](https://fastapi.tiangolo.com/tutorial/first-steps/)
- [FastAPI: testing](https://fastapi.tiangolo.com/tutorial/testing/)

TechFlow and the support scenarios in the project description are fictional. Stage 01 requires no LLM API key or external database.
