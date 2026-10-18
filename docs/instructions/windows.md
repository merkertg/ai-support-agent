# Windows and PowerShell

[← README](../../README.md) · [All documentation](../../README.md#documentation) · [Quickstart](quickstart.md)

The guides show commands for Bash, the shell of Linux and macOS. On Windows you can run every guide in one of three ways:

| Way | When to use it |
| --- | --- |
| **PowerShell 7** (`pwsh`) | Recommended for everyday work. The commands below, and the PowerShell blocks in [DEMO](../DEMO.md), [Stage 08](../STAGE_08.md), [Stage 10](../STAGE_10.md) and [Using the API](usage.md), are written for it. |
| **Git Bash** | Installed with [Git for Windows](https://git-scm.com/downloads/win). Bash commands from any guide work as written. |
| **WSL 2** | A Linux distribution inside Windows. Clone the project inside WSL (for example in `~/ai-support-agent`), not under `/mnt/c`, and follow the Linux instructions. |

Windows PowerShell 5.1 runs most commands too, but it decodes JSON responses without an explicit charset as ISO-8859-1, so Russian text from the API can look broken. Use PowerShell 7 for API calls.

## Install the tools

1. [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) with the WSL 2 backend. Start it and wait until `docker version` shows the server.
2. [Git for Windows](https://git-scm.com/downloads/win). The repository's `.gitattributes` keeps text files with LF line endings, so no `core.autocrlf` change is needed.
3. [uv](https://docs.astral.sh/uv/getting-started/installation/), using the PowerShell installer from that page. Open a new terminal afterwards. `uv sync --locked` downloads Python 3.12 if it is missing.

## Prepare a PowerShell session

Run once per window so that Russian text from Python scripts and the API is shown correctly:

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location path\to\ai-support-agent
```

Replace `path\to\ai-support-agent` with the folder where you cloned the repository; it contains `pyproject.toml`. Command-line scripts of the project always write UTF-8, also into files and pipes.

## Bash constructs and their PowerShell equivalents

| Bash | PowerShell 7 |
| --- | --- |
| `cp -n .env.example .env` or `if [ ! -f .env ]; then cp .env.example .env; fi` | `if (-not (Test-Path .env)) { Copy-Item .env.example .env }` |
| `export NAME=value` | `$env:NAME = 'value'` |
| `NAME=value command` (only for one command) | `$env:NAME = 'value'; command; Remove-Item Env:NAME` |
| `unset NAME` | `Remove-Item Env:NAME` |
| Line continuation `\` | Backtick `` ` `` at the end of the line, or one long line |
| `curl ...` | `curl.exe ...` (plain `curl` is an alias of `Invoke-WebRequest` in Windows PowerShell) or `Invoke-RestMethod` |
| `curl --data '{"query":"Сервер"}'` | `Invoke-RestMethod` with a UTF-8 body, see [JSON requests](#json-requests-to-the-api) |
| `/tmp/file.json` | `"$env:TEMP\file.json"` |
| `grep 'text' file` | `Select-String 'text' file` |
| `cmp a b` | `(Get-FileHash a).Hash -eq (Get-FileHash b).Hash` |
| `cat file` | `Get-Content file` |
| `.venv/bin/python` | `.venv\Scripts\python.exe`; prefer `uv run --locked python` |
| `source .venv/bin/activate` | `.venv\Scripts\Activate.ps1`; not needed with `uv run` |
| `python3` | `uv run --locked python` |

`uv`, `docker`, `docker compose` and `git` commands are the same in both shells. Single-line commands from the guides that use only these tools and no Bash constructs work in PowerShell as written.

## Typical commands

Start the stack with the operator UI and index the knowledge base:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv sync --locked
docker compose up --build --detach --wait --wait-timeout 180 ui
docker compose exec -T api python -m app.knowledge.ingest
docker compose ps --all
```

Check this computer without Docker or a database, and run the unit tests:

```powershell
uv run --locked python scripts/check_local_start.py
uv run --locked pytest -q --ignore=tests/integration
uv run --locked python -m evaluations.run
```

Run the full suite against the separate test services:

```powershell
docker compose --profile test up --detach --wait db-test qdrant-test
$env:CI = 'true'
$env:TEST_DATABASE_URL = 'postgresql+psycopg://support:support_ci@127.0.0.1:5434/ai_support_test'
$env:TEST_QDRANT_URL = 'http://127.0.0.1:6336'
uv run --locked pytest -v
Remove-Item Env:CI, Env:TEST_DATABASE_URL, Env:TEST_QDRANT_URL
docker compose --profile test stop db-test qdrant-test
```

Check the running stack and read the API log:

```powershell
uv run --locked python scripts/smoke_test.py `
  --base-url http://127.0.0.1:8001 --timeout 30 --check-database --check-api
docker compose logs --no-color api | Select-String '"level": "ERROR"'
docker compose logs --no-color api | Select-String 'REQUEST_ID_FROM_UI'
```

## JSON requests to the API

The helper converts a hashtable to UTF-8 JSON, so Russian text arrives intact:

```powershell
function Invoke-Api {
  param([string]$Method, [string]$Path, [hashtable]$Body)
  $request = @{ Method = $Method; Uri = "http://127.0.0.1:8001$Path" }
  if ($Body) {
    $json = $Body | ConvertTo-Json -Depth 10
    $request.ContentType = 'application/json; charset=utf-8'
    $request.Body = [System.Text.Encoding]::UTF8.GetBytes($json)
  }
  Invoke-RestMethod @request
}

Invoke-Api GET /api/llm/status
Invoke-Api POST /api/knowledge/search @{
  query = 'Сервер не запускается после обновления'
  product_code = 'techflow-server'
  limit = 3
}
```

`Invoke-RestMethod` throws on HTTP 4xx and 5xx; the message shows the status code. To see an expected error such as 409 without an exception, use `curl.exe --silent --show-error --write-out '%{http_code}' ...`.

## What is checked on Windows

The CI job **Python on windows-2025** runs on every push: `uv sync --locked`, the dependency-age check, documentation checks (Bash examples are parsed by Git Bash, PowerShell examples by `pwsh`), Ruff, all unit tests, offline evaluations, and a real start of the API and UI with `scripts/check_local_start.py`. Docker images are built and smoke-tested on Linux; Docker Desktop on Windows runs the same Linux images.
