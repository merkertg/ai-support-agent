# Quickstart

[← README](../../README.md) · [All documentation](../../README.md#documentation)

Install the tools, start the Compose stack with the operator interface, index the knowledge base, and optionally run the API or the UI on the host.

## Requirements

- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for dependency and environment management.
- Docker with the `docker compose` command; start Docker Desktop on this Mac.

Run all commands from the project root, the directory containing `pyproject.toml`.

## Start the stack

```bash
cp -n .env.example .env
uv sync --locked
docker compose up --build --detach --wait --wait-timeout 180 ui
```

`cp -n` preserves an existing `.env`. When reusing one, compare its settings with `.env.example`. The example contains credentials for local development. Compose starts PostgreSQL and Qdrant, waits for them to become healthy, applies Alembic migrations, starts the API, and then the Streamlit operator UI. The API health check requires both a database connection and the current migration revision; the UI starts only after the API is healthy. To start only the backend, use `docker compose up --build --detach --wait api`.

Index the knowledge base articles from `knowledge/` (explicit and idempotent):

```bash
docker compose exec -T api python -m app.knowledge.ingest
```

Default addresses:

- [API readiness](http://127.0.0.1:8001/ready): `{"status":"ok","database":"ok"}`.
- [Application health](http://127.0.0.1:8001/health): `{"status":"ok","service":"ai-support-agent"}`.
- [Interactive API documentation](http://127.0.0.1:8001/docs).
- [Operator interface](http://127.0.0.1:8502) (`UI_PORT`); walkthrough in the [Stage 08 guide](../STAGE_08.md).
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

`seed` is explicit and idempotent; application startup does not create sample records. `show` only reads the saved customer, product, and license. The [Stage 02 guide](../STAGE_02.md) shows how to compare these records before and after removing and recreating the Compose containers while retaining the PostgreSQL volume.

Inspect or stop the stack:

```bash
docker compose logs --no-color db migrate api ui
docker compose down
```

Ordinary `down` retains the named PostgreSQL and Qdrant volumes and their data.

## Optional: run the API on the host

For Python development with automatic reload, start PostgreSQL and Qdrant in Docker and run the API from `.venv`. `.env` contains separate URLs for host Python (`127.0.0.1:5433`) and Compose services (`db:5432`). Host knowledge search uses `QDRANT_URL=http://127.0.0.1:6335`; Compose uses `QDRANT_URL_DOCKER=http://qdrant:6333`. These values are included in `.env.example`.

```bash
docker compose up --detach --wait db qdrant
uv run --locked alembic upgrade head
uv run --locked python -m app.knowledge.ingest
uv run --locked uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open [host API documentation](http://127.0.0.1:8000/docs) and stop Uvicorn with `Ctrl+C`.

## Optional: run the operator UI on the host

The UI needs only the `ui` dependency group and the API address. With the API in Compose:

```bash
uv sync --locked --group ui
API_URL=http://127.0.0.1:8001 uv run --locked --group ui streamlit run \
  operator_ui/main.py --server.address 127.0.0.1 --server.port 8501
```

Open [the host UI](http://127.0.0.1:8501). Inside Compose the UI uses `http://api:8000` instead.

## Prepare a demonstration

```bash
uv run --locked python scripts/demo_scenario.py --url http://127.0.0.1:8001
```

The script checks that both providers are `mock` and the index is ready, then creates or reuses fictional records and leaves one suggestion `pending` for a human decision. See [the demo walkthrough](../DEMO.md).

The Stage 01 standalone API still starts without a configured database: `/health`, `/docs`, and `/openapi.json` remain available. `/ready` returns HTTP 503 until PostgreSQL is reachable and its Alembic revision matches the application. `/health` checks the running application; `/ready` checks database readiness.
