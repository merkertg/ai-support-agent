# Quickstart

[← README](../../README.md) · [All documentation](../../README.md#documentation)

Install the tools, start the Compose stack with the operator interface, index the knowledge base, and optionally run the API or the UI on the host.

## Requirements

- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for dependency and environment management.
- Docker with the `docker compose` command: Docker Desktop on macOS and Windows, Docker Engine or Desktop on Linux.

Run all commands from the project root, the directory containing `pyproject.toml`.

## Supported platforms

| | Linux | macOS (Intel, Apple Silicon) | Windows 10/11 |
| --- | --- | --- | --- |
| Compose stack | Docker Engine or Desktop | Docker Desktop | Docker Desktop with the WSL 2 backend |
| Image architecture | amd64 or arm64: local build and GHCR | arm64 on Apple Silicon, amd64 on Intel | amd64 (arm64 on ARM laptops) |
| Python on the host | `uv sync --locked`, Python 3.12 | same | same |
| Shell for the guides | Bash | Bash or zsh | Git Bash or WSL; PowerShell commands below |

CI checks every push on all three systems (`Python on ubuntu-24.04`, `macos-15`, `windows-2025`): the same `uv.lock` installs, documentation checks, Ruff, unit tests, offline evaluations, and a real start of the API and UI. Docker images are built and smoke-tested for amd64 and, under emulation, for arm64; both are published under one GHCR tag, so Docker pulls the native image. Integration tests with PostgreSQL and Qdrant run on Linux.

Check your own computer without Docker, a database, or keys:

```bash
uv sync --locked
uv run --locked python scripts/check_local_start.py
```

The script starts Uvicorn and Streamlit as separate processes on free local ports from a temporary folder (so your `.env` is not read), checks `/health`, a JSON 503 from `/ready` without a database, the `X-Request-ID` header, the Streamlit server, and renders the operator page against the running API, then stops both processes. `Локальный запуск API и UI: OK` is printed on success.

On Windows in PowerShell, the stack starts with:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv sync --locked
docker compose up --build --detach --wait --wait-timeout 180 ui
docker compose exec -T api python -m app.knowledge.ingest
uv run --locked python scripts/check_local_start.py
```

The repository stores text files with LF line endings (`.gitattributes`), so Windows checkouts do not get CRLF in shell examples, Markdown articles, or Python files. A working copy cloned before this rule may still contain CRLF; re-create it with `git rm --cached -r .` followed by `git reset --hard`. Command-line scripts write UTF-8 even when Windows redirects their output to a file or pipe.

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

## Read the API log

The API writes one JSON line per event to stdout: requests (method, path without the query string, status, duration), agent runs, resource and database errors, failed LLM calls, and workflow checkpoint repairs. Every line carries `request_id`, which the API also returns in the `X-Request-ID` response header; the operator UI shows it as «Код запроса для администратора» next to an error.

```bash
docker compose logs --no-color api | grep '"level": "ERROR"'
docker compose logs --no-color api | grep 'REQUEST_ID_FROM_UI'
```

Bodies, query strings, SQL, driver messages, and SDK response texts are not logged, so license keys and personal data stay out of the log; database errors are described by class and SQLSTATE, provider errors by class, HTTP status, and the provider's request id. Unexpected exceptions return `{"detail": ...}` with HTTP 500 and the request id, while the traceback goes only to the log. Uvicorn's own access log is disabled because it would print query strings. Set `LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) in `.env`; successful `/health` and `/ready` checks are not logged.

## Update an existing stack

After pulling new code, rebuild and restart without `down`:

```bash
docker compose up --build --detach --wait --wait-timeout 180 ui
curl --silent --show-error --fail-with-body http://127.0.0.1:8001/ready
curl --silent --show-error --fail-with-body http://127.0.0.1:8001/api/knowledge/status
```

Compose rebuilds the API and UI images, applies pending migrations through `migrate`, and recreates only the containers whose image or settings changed. The PostgreSQL and Qdrant volumes are reused, so records, waiting reviews, and the search index survive. Run `ingest` again only when articles changed; unchanged articles are skipped. Never add `--volumes` to `down` on a stack whose data you need.

If the command stops with `dependency failed to start` or a missing `docker.sock`, Docker Desktop itself has stopped. Start it, wait until `docker version` shows the server, and run the same command again; the volumes are not affected.

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
