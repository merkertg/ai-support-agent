# Using the API

[← README](../../README.md) · [All documentation](../../README.md#documentation)

Business records, LLM draft previews, knowledge base search, the agent, human review, and the operator endpoints. Start the stack with the [quickstart](quickstart.md) first. The same operations are available without curl in the [operator interface](http://127.0.0.1:8502), described in the [Stage 08 guide](../STAGE_08.md).

## Work with the business API

Open [Swagger UI](http://127.0.0.1:8001/docs). Each resource supports these operations:

| Resource | Create | List | Read one |
| --- | --- | --- | --- |
| Customers | `POST /api/customers` | `GET /api/customers` | `GET /api/customers/{resource_id}` |
| Products | `POST /api/products` | `GET /api/products` | `GET /api/products/{resource_id}` |
| Licenses | `POST /api/licenses` | `GET /api/licenses` | `GET /api/licenses/{resource_id}` |
| Tickets | `POST /api/tickets` | `GET /api/tickets` | `GET /api/tickets/{resource_id}` |

Start by creating a customer and product. Use the returned IDs to create their license and ticket; existing demo records mean IDs may differ between machines. The [Russian Stage 03 guide](../STAGE_03.md) walks through complete requests and responses.

Creation returns HTTP 201; reads return 200. Invalid input returns 422, an unknown record or missing creation reference returns 404, a duplicate unique value returns 409, and an unavailable database returns a safe 503. Lists return JSON arrays ordered by ID, with `offset=0` and `limit=20` by default (maximum 100). License lists support `customer_id`, `product_id`, and `license_key`; ticket lists support `customer_id`, `product_id`, and `status`. Filters without matches return an empty array.

New tickets always start as `open`. License dates must include a time zone, and expiration cannot precede issuance. The API uses explicit transactions; a failed request must not leave a partial write. The resource API provides create/read operations. The agent and human-review workflow below implement analysis and controlled delivery; only the ticket status can be changed (`PATCH /api/tickets/{resource_id}`); general editing, deletion, and authentication remain future work.

## Preview a reply with the LLM client

The default `LLM_PROVIDER=mock` requires no API key and makes no external requests. Check its configuration:

```bash
curl --silent --show-error --fail-with-body http://127.0.0.1:8001/api/llm/status
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/llm/status
```

The response is `{"provider":"mock","model":"mock-draft-v1","configured":true}`. In Swagger, open `POST /api/tickets/{resource_id}/draft`, enter an existing ticket ID, and execute it without a request body. The response includes `summary`, `category`, `draft_reply`, provider/model metadata, and `requires_human_review: true`. Mock replies explicitly state that no real model was called. This preview does not save a draft or change the ticket.

To enable OpenAI, set `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and an explicit `OPENAI_MODEL` in your local `.env`, then recreate the API service:

```bash
docker compose up --detach --no-build --force-recreate --wait api
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
docker compose up --detach --no-build --force-recreate --wait api
```

`configured: true` only confirms that the required settings exist. The [Stage 04 guide](../STAGE_04.md) explains real-provider setup, structured output, timeouts, token usage, safe errors, and the first manual request. No real OpenAI request has been verified in this iteration because no API key is configured. Automated tests use mock responses and the real SDK with a substituted HTTP transport.

For a complete container smoke test in mock mode only:

```bash
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 30 --check-llm
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
uv run --locked python scripts/smoke_test.py `
  --base-url http://127.0.0.1:8001 --timeout 30 --check-llm
```

This explicit flag first rejects any provider other than configured mock, then creates **three fictional records** (customer, product, ticket) and checks the draft and unchanged ticket. It also includes database and business API checks. Use the read-only smoke command from the [quickstart](quickstart.md) when you only want read checks.

## Search the knowledge base

`python -m app.knowledge.ingest` reads `knowledge/*.md`, splits each article into section chunks, computes embeddings, writes them to the Qdrant collection `support_knowledge`, and records each article in the PostgreSQL table `knowledge_documents` (checksum, chunk count, embedding signature). Unchanged articles are skipped; changed articles are reindexed and their old extra chunks removed; articles deleted from the folder are removed from Qdrant and the registry.

```bash
curl --silent --show-error --fail-with-body http://127.0.0.1:8001/api/knowledge/status
curl --silent --show-error --fail-with-body --request POST \
  http://127.0.0.1:8001/api/knowledge/search --header 'Content-Type: application/json' \
  --data '{"query":"Сервер не запускается после обновления","product_code":"techflow-server","limit":3}'
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/knowledge/status
$body = @{
  query = 'Сервер не запускается после обновления'
  product_code = 'techflow-server'
  limit = 3
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/api/knowledge/search `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

The status shows the embedding provider, collection state (`ready`, `missing`, `incompatible`, `unavailable`, or `not_configured`), Qdrant points, and registered articles. Search returns chunks with `document_key`, `title`, `section`, `text`, cosine `score`, `source_path`, and `scope`. With `product_code`, results are limited to that product's articles plus general articles.

The default `EMBEDDING_PROVIDER=mock` builds a local lexical vector from word stems: it needs no network or key and matches shared words, not meaning. Results below `KNOWLEDGE_MIN_SCORE` (0.2) are dropped. An OpenAI embeddings adapter (`EMBEDDING_PROVIDER=openai` with an explicit `OPENAI_EMBEDDING_MODEL`) is tested only with a substituted HTTP transport. Every point carries an embedding signature such as `mock:mock-hash-v1:256`, and search ignores vectors from other models; reindex after switching providers.

## Analyze a ticket with the agent

`POST /api/tickets/{resource_id}/analyze` runs a bounded tool-calling loop for an existing ticket and returns HTTP 201 with the saved run. The optional JSON body accepts only `license_key`; other fields return 422. The agent can call five read-only server functions: `get_ticket`, `get_customer` (no email), `get_license` (only within the ticket's customer and product, with a date-aware `valid` flag), `get_ticket_history` (up to five other tickets of the same customer and product), and `search_knowledge` (the ticket's product articles plus general articles; the server sets the product scope). Model-provided names and arguments are validated on the server and never become SQL or Python code; tool transactions are `READ ONLY` in PostgreSQL.

A successful run stores `completed`, the tool trace (license keys masked as `***`), the structured `summary`/`category`/`draft_reply`/`sources`, and exactly one `pending` suggestion. `sources` may contain only article keys returned by `search_knowledge` in the same run; any other citation stops the run with 502. If the knowledge base is unavailable, the tool returns an error and the agent answers without sources. The agent never changes the ticket or sends a reply. `GET /api/agent-runs/{resource_id}` reads the saved run without calling the model again. A new POST creates a new run only when the ticket is not `resolved` or `closed`, has no `pending` suggestion, and has no analysis started within `AGENT_TIMEOUT_SECONDS` + 60 seconds; otherwise it returns 409 without creating a run. The check runs under a row lock on the ticket, so parallel requests cannot create two drafts. A failed run leaves no suggestion and does not block a retry.

Limits apply to both mock and OpenAI: `AGENT_MAX_STEPS` (default 6, at most 8), `AGENT_MAX_TOOL_CALLS` (default 12, at most 20), and `AGENT_TIMEOUT_SECONDS` (default 60, at most 120). A limit stop, timeout, or provider error after the run has started returns its safe error together with `run_id`; the run is saved as `failed` with partial progress and no suggestion. The mock agent needs three steps and five tool calls when a license key is given.

Check the whole agent path in a running mock stack:

```bash
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 60 --check-agent
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
uv run --locked python scripts/smoke_test.py `
  --base-url http://127.0.0.1:8001 --timeout 60 --check-agent
```

`--check-agent` includes `--check-llm` and the read-only `--check-knowledge` (which requires an indexed knowledge base) and additionally creates a fictional license, an agent run, and a suggestion that must cite a found article. The [Stage 06 guide](../STAGE_06.md) covers the knowledge base; the [Stage 05 guide](../STAGE_05.md) walks through each request, the saved trace, error handling, migration `0002_agent_runtime`, and the OpenAI adapter, which is tested only with a substituted HTTP transport.

## Review suggestions (human in the loop)

Since Stage 07, `POST /api/tickets/{resource_id}/analyze` is the first node of a LangGraph workflow. After analysis the graph pauses at `human_review` and stores its state in PostgreSQL (`checkpoint_*` tables created by migration `0004_review_workflow`). The run response contains `workflow_thread_id`; `GET /api/agent-runs/{resource_id}/workflow` shows `awaiting_review`, `delivered`, `rejected`, or `failed`.

Create an employee, list pending suggestions, and decide:

```bash
curl --silent --show-error --fail-with-body --request POST http://127.0.0.1:8001/api/users \
  --header 'Content-Type: application/json' --data '{"name":"Operator","email":"operator@example.com"}'
curl --silent --show-error --fail-with-body 'http://127.0.0.1:8001/api/suggestions?status=pending'
curl --silent --show-error --fail-with-body --request POST \
  http://127.0.0.1:8001/api/suggestions/1/review --header 'Content-Type: application/json' \
  --data '{"decision":"approve","reviewer_id":1,"final_content":"Hello! Please check port 8443."}'
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
$json = 'application/json; charset=utf-8'
$user = @{ name = 'Operator'; email = 'operator@example.com' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/api/users `
  -ContentType $json -Body ([System.Text.Encoding]::UTF8.GetBytes($user))
Invoke-RestMethod 'http://127.0.0.1:8001/api/suggestions?status=pending'
$review = @{
  decision = 'approve'
  reviewer_id = 1
  final_content = 'Hello! Please check port 8443.'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/api/suggestions/1/review `
  -ContentType $json -Body ([System.Text.Encoding]::UTF8.GetBytes($review))
```

`approve` may replace the text with `final_content`; it creates a ticket message from the employee (simulated delivery, no email is sent) and moves the ticket to `waiting_customer`. `reject` requires a `comment` and changes nothing in the ticket. A suggestion is decided once: a second decision returns 409. A `resolved` or `closed` ticket receives no reply: approval returns 409 before the workflow resumes, and the suggestion can only be rejected. Invalid or inactive reviewers are rejected before the workflow resumes, so it keeps waiting. `reviewer_id` is not authentication. Suggestions created before Stage 07 have no workflow and are decided directly.

```bash
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 60 --check-review
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
uv run --locked python scripts/smoke_test.py `
  --base-url http://127.0.0.1:8001 --timeout 60 --check-review
```

`--check-review` includes `--check-agent` and additionally approves the new suggestion, checks the delivered message, the ticket status, the `delivered` workflow, and the 409 on a repeated decision. `--review-run RUN_ID` performs the same checks for an existing waiting run. The [Stage 07 guide](../STAGE_07.md) explains the graph, checkpoints, and reliability rules.

## Operator endpoints

Stage 08 added read models for the operator interface; any HTTP client can use them:

| Operation | Result |
| --- | --- |
| `GET /api/dashboard` | Aggregates over the whole database: tickets, open and high-priority tickets, analyzed tickets, completed/failed runs, suggestion decisions, average analysis time, approval rate. Averages and rates are `null` when there is no data yet. |
| `GET /api/agent-runs?ticket_id=…` | Saved runs, including `failed` and `running`, without calling the model. |
| `GET /api/tickets/{resource_id}/messages` | Messages of one ticket, paginated like other lists. |
| `PATCH /api/tickets/{resource_id}` | Body `{"status": "…"}` changes only the ticket status; other fields are rejected with 422. It does not start the agent or decide suggestions. |

```bash
curl --silent --show-error --fail-with-body http://127.0.0.1:8001/api/dashboard
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/dashboard
```

To prepare a reproducible demonstration through these APIs, run `scripts/demo_scenario.py`; see the [Stage 10 guide](../STAGE_10.md).
