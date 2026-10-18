# AI Support Agent

A Python learning project for a technical support assistant. It helps an operator investigate tickets, check customer licenses, search documentation, and review suggested replies before simulated delivery. TechFlow products and all support data are fictional.

The current implementation covers **Stages 01–07**: a FastAPI service with PostgreSQL 17 and Qdrant 1.19, REST APIs for customers, products, licenses, tickets, and support staff, LLM draft previews, and a bounded agent. The agent reads ticket data and searches a Markdown knowledge base through five read-only tools, then saves a `pending` reply suggestion that cites only the articles it found. A LangGraph workflow pauses until an employee approves or rejects the suggestion; approval delivers the reply as a ticket message. The default mock mode needs no API key; OpenAI providers require explicit credentials. GitHub Actions checks the application and publishes the tested Docker image to GHCR.

**Начни с [инструкции этапа 07 на русском](docs/STAGE_07.md)** или с [быстрого запуска](docs/instructions/quickstart.md).

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
| 08 | Streamlit operator interface | Planned |
| 09 | Agent integration tests, evaluations and security checks | Planned; relational database integration tests are part of Stage 02 |
| 10 | Packaging, CI/CD, and GitHub demonstration materials | Docker Compose and CI/GHCR delivery configured; demonstration materials planned |

## Documentation

Instructions ([docs/instructions/](docs/instructions/)):

- [Quickstart](docs/instructions/quickstart.md): requirements, Compose stack, knowledge base indexing, host run
- [Using the API](docs/instructions/usage.md): business records, draft previews, knowledge search, agent, human review
- [Testing and verification](docs/instructions/testing.md): lint, unit and integration tests
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
- [CI/CD and Docker guide](docs/CI_CD.md)

Design and history:

- [Solution approach](docs/APPROACH.md)
- [Development log](docs/DEVELOPMENT_LOG.md)
