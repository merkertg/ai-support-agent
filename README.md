# AI Support Agent

A Python learning project for a technical support assistant. It helps an operator investigate tickets, check customer licenses, search documentation, and review suggested replies before simulated delivery. TechFlow products and all support data are fictional.

The project implements **all ten stages**: a FastAPI service with PostgreSQL 17 and Qdrant 1.19, REST APIs for customers, products, licenses, tickets, and support staff, LLM draft previews, and a bounded agent. The agent reads ticket data and searches a Markdown knowledge base through five read-only tools, then saves a `pending` reply suggestion that cites only the articles it found. A LangGraph workflow pauses until an employee approves or rejects the suggestion; approval delivers the reply as a ticket message. A Streamlit operator interface works on top of the HTTP API, offline evaluations measure search quality and server-side safety guarantees, and a repeatable demo scenario prepares a pending suggestion for a human decision. The default mock mode needs no API key; OpenAI providers require explicit credentials. GitHub Actions checks the application, builds and smoke-tests the API and UI images, runs the evaluations and the demo scenario, and publishes both tested images to GHCR after a push to main or a validated version tag. CI requires at least 85% combined line/branch coverage; abandoned agent runs recover automatically, and weekly dependency updates arrive as reviewable pull requests.

Linux, macOS (Intel and Apple Silicon) and Windows are supported: CI runs the Python checks and a real API/UI start on all three, and builds the Docker images for amd64 and arm64.

**Начни с [демонстрации](docs/DEMO.md)** или с [быстрого запуска](docs/instructions/quickstart.md). Интерфейс оператора после запуска: <http://127.0.0.1:8502>.

## Roadmap

| Stage | Work | Status |
| --- | --- | --- |
| 01 | Initialize Python project, FastAPI, health endpoint, first tests | Implemented |
| 02 | PostgreSQL, SQLAlchemy models, Alembic migrations | Implemented; includes readiness, demo records and database checks |
| 03 | Customer, product, license, and ticket REST APIs | Implemented: create, list and read with PostgreSQL integration tests |
| 04 | LLM draft previews and error handling | Implemented; mock and SDK transport tested, live OpenAI validation requires credentials |
| 05 | Tool calling and the agent execution loop | Implemented; mock agent saves runs and pending suggestions, live OpenAI validation requires credentials |
| 06 | RAG, embeddings, and Qdrant document search | Implemented; mock embeddings, product-scoped search and agent citations, live OpenAI embeddings require credentials |
| 07 | LangGraph workflow and human review | Implemented; PostgreSQL checkpoints, approve/reject with simulated delivery, resume after restart |
| 08 | Streamlit operator interface | Implemented; tickets, review queue, knowledge search, dashboard and catalogs over the HTTP API; separate UI image |
| 09 | Agent integration tests, evaluations and security checks | Implemented; offline retrieval metrics with fixed thresholds and 12 agent safety scenarios gate CI; live OpenAI quality is not measured |
| 10 | Packaging, CI/CD, and GitHub demonstration materials | Implemented; Compose stack with UI, idempotent demo scenario, demo guide, CI/GHCR delivery |

## Documentation

Instructions ([docs/instructions/](docs/instructions/)):

- [Quickstart](docs/instructions/quickstart.md): requirements, supported platforms, Compose stack with the operator UI, updating an existing stack, knowledge base indexing, host run
- [Windows and PowerShell](docs/instructions/windows.md): Git Bash, WSL or PowerShell; equivalents of every Bash construct used in the guides
- [Using the API](docs/instructions/usage.md): business records, draft previews, knowledge search, agent, human review
- [Testing and verification](docs/instructions/testing.md): lint, unit and integration tests, coverage gate, offline evaluations, portability checks
- [CI and delivery to GHCR](docs/instructions/ci-and-delivery.md)
- [Project structure](docs/instructions/project-structure.md)

Stage learning guides in Russian:

- [Stage 01: FastAPI foundation](docs/STAGE_01.md)
- [Stage 02: PostgreSQL, models and migrations](docs/STAGE_02.md)
- [Stage 03: business REST API](docs/STAGE_03.md)
- [Stage 04: LLM draft previews](docs/STAGE_04.md)
- [Stage 05: agent with tools](docs/STAGE_05.md)
- [Stage 06: knowledge base and RAG](docs/STAGE_06.md)
- [Stage 07: LangGraph workflow and human review](docs/STAGE_07.md)
- [Stage 08: Streamlit operator interface](docs/STAGE_08.md)
- [Stage 09: evaluations and security checks](docs/STAGE_09.md)
- [Stage 10: packaging and reproducible demo](docs/STAGE_10.md)
- [Demo walkthrough](docs/DEMO.md)
- [CI/CD and Docker guide](docs/CI_CD.md)
- [Наглядное руководство PDF](docs/guide/ai-support-agent-guide.pdf) и [его пересборка](docs/guide/README.md)
- [Полная проверка проекта](docs/VERIFICATION.md)
- [Восстановление зависших запусков](docs/RUN_RECOVERY.md)
- [Автоматические PR зависимостей](docs/DEPENDENCY_UPDATES.md)
- [Версии и публикация API/UI](docs/RELEASES.md)

Design and history:

- [Solution approach](docs/APPROACH.md)
- [Development log](docs/DEVELOPMENT_LOG.md)
