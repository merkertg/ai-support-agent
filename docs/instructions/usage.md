# Using the API

[← README](../../README.md) · [All documentation](../../README.md#documentation)

Authentication and roles, business records, LLM draft previews, knowledge base search, the agent, human review, and the operator endpoints. Start the stack and issue your first staff token with the [quickstart](quickstart.md) first. The same operations are available without curl in the [operator interface](http://127.0.0.1:8502), described in the [Stage 08 guide](../STAGE_08.md).

## Создание обращения в браузере

В [интерфейсе оператора](http://127.0.0.1:8502) войди по токену и открой
**Обращения** → **Создать обращение**. В полях **Клиент** и **Продукт**
выбери имя/email клиента и название/код продукта. Это позволяет различать
одноимённые записи; API получает их обычные числовые `customer_id` и
`product_id`, права и проверка ссылок сохраняются.

Каждый список загружает 20 записей и имеет независимые **Назад**/**Далее**.
Поиск внутри выпадающего списка ограничен текущей страницей: для поиска
в другой группе записей сначала перейди на неё. Первая запись автоматически
не выбирается, а отправка недоступна, пока не выбраны оба справочника.
Если список пуст, нужную запись сначала создают в **Справочники**.

Сначала закончи выбор и перелистывание, затем заполни тему, описание
и приоритет. Изменение списка или страницы обновляет экран и может сбросить
ещё не отправленный текст формы. Сам выбор делает только GET-запросы;
`POST /api/tickets` выполняется при нажатии **Создать обращение**.
[Полный проход](../STAGE_08.md) включает открытие карточки и запуск анализа.

## Authentication, roles and rate limit

Every route under `/api/` requires a staff token in the `Authorization: Bearer <token>` header. Only `/health`, `/ready`, `/docs`, `/redoc`, and `/openapi.json` stay open. A missing, unknown, expired, or revoked token and a token of an inactive employee all receive the same `401` with `WWW-Authenticate: Bearer`; the reason (`missing`, `malformed`, `unknown`, `revoked`, `expired`, `inactive`) is written only to the API log as `auth_failed`. The header is checked before the database is opened and before the request body is validated, so an anonymous request learns nothing about the data or the body schema. The examples below read the token from the `API_TOKEN` environment variable.

Roles form a ladder; each role can do everything the previous one can:

| Role | Allowed |
| --- | --- |
| `operator` | Read every `/api/*` resource; create customers, products, licenses, and tickets; draft previews; run the agent; search the knowledge base; change a ticket status. |
| `reviewer` | Additionally approve or reject agent suggestions. |
| `admin` | Additionally create employees with a role and issue, list, and revoke their tokens. |

A lower role receives `403`. The author of a review decision is the employee who owns the token: the body has no `reviewer_id`, and sending one returns 422, so nobody can approve on behalf of a colleague. The role is read from PostgreSQL on every request and checked again under the review lock, so demoting or deactivating an employee takes effect on the next request.

A token is `asa_` followed by 43 base64url characters (256 random bits). The table `api_tokens` (migration `0005_api_tokens`) stores only its SHA-256, a label, a 12-character display prefix, the expiry, and the revocation time; the secret itself is shown once when issued. Tokens expire after 90 days by default (1–365 days, no permanent tokens). The first administrator is created in the API container with `python -m app.tokens issue`; further work goes through the API:

```bash
auth="Authorization: Bearer $API_TOKEN"
curl --silent --show-error --fail-with-body --header "$auth" http://127.0.0.1:8001/api/auth/me
curl --silent --show-error --fail-with-body --request POST --header "$auth" \
  http://127.0.0.1:8001/api/users --header 'Content-Type: application/json' \
  --data '{"name":"Reviewer","email":"reviewer@example.com","role":"reviewer"}'
curl --silent --show-error --fail-with-body --request POST --header "$auth" \
  http://127.0.0.1:8001/api/users/2/tokens --header 'Content-Type: application/json' \
  --data '{"name":"laptop","expires_in_days":30}'
curl --silent --show-error --fail-with-body --header "$auth" \
  http://127.0.0.1:8001/api/users/2/tokens
curl --silent --show-error --fail-with-body --request DELETE --header "$auth" \
  http://127.0.0.1:8001/api/tokens/2
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
$auth = @{ Authorization = "Bearer $env:API_TOKEN" }
$json = 'application/json; charset=utf-8'
Invoke-RestMethod -Headers $auth http://127.0.0.1:8001/api/auth/me
$user = @{ name = 'Reviewer'; email = 'reviewer@example.com'; role = 'reviewer' } |
  ConvertTo-Json
Invoke-RestMethod -Method Post -Headers $auth -Uri http://127.0.0.1:8001/api/users `
  -ContentType $json -Body ([System.Text.Encoding]::UTF8.GetBytes($user))
$token = @{ name = 'laptop'; expires_in_days = 30 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Headers $auth `
  -Uri http://127.0.0.1:8001/api/users/2/tokens `
  -ContentType $json -Body ([System.Text.Encoding]::UTF8.GetBytes($token))
Invoke-RestMethod -Method Delete -Headers $auth -Uri http://127.0.0.1:8001/api/tokens/2
```

`GET /api/auth/me` shows the employee, role, and current token. `POST /api/users/{resource_id}/tokens` returns the new token once in the field `token`; give it to the employee over a secure channel. `GET …/tokens` lists labels, prefixes, and dates without secrets. `DELETE /api/tokens/{resource_id}` revokes a token (repeating it is safe); the next request with that token gets 401. The same operations are available in the API container:

```bash
docker compose exec -T api python -m app.tokens list --email reviewer@example.com
docker compose exec -T api python -m app.tokens revoke --token-id 2
```

`issue` creates the employee when `--name` is given and the email is new; it never changes the role of an existing employee. `list` and `revoke` print JSON without secrets.

Changes are rate-limited per employee: every `POST`, `PATCH`, and `DELETE` (including knowledge search, which may call paid embeddings) takes one permit from a bucket that holds `WRITE_RATE_LIMIT_PER_MINUTE` permits (default 60, from 1 to 10000) and refills at the same rate per minute. A short burst passes at once; a sustained stream above the limit receives `429` with `Retry-After`. Reads and requests rejected with 401 or 403 do not use permits, and a second token of the same employee shares the bucket. The limiter lives in the memory of one API process: it resets on restart, and with several Uvicorn workers or replicas each counts separately; a shared limit then belongs in a gateway or Redis.

The request line of the API log carries `user_id` of the authenticated employee. Tokens and the `Authorization` header are never logged; CI fails if a string shaped like a token appears in the log.

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

New tickets always start as `open`. License dates must include a time zone, and expiration cannot precede issuance. The API uses explicit transactions; a failed request must not leave a partial write. The resource API provides create/read operations. The agent and human-review workflow below implement analysis and controlled delivery; only the ticket status can be changed (`PATCH /api/tickets/{resource_id}`); general editing and deletion remain future work. Creating customers, products, licenses, and tickets requires the `operator` role; creating employees (`POST /api/users`, with `role` `operator`, `reviewer`, or `admin`) requires `admin`.

## Preview a reply with the LLM client

The default `LLM_PROVIDER=mock` requires no API key and makes no external requests. Check its configuration:

```bash
curl --silent --show-error --fail-with-body \
  --header "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8001/api/llm/status
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
$auth = @{ Authorization = "Bearer $env:API_TOKEN" }
Invoke-RestMethod -Headers $auth http://127.0.0.1:8001/api/llm/status
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

This explicit flag first rejects any provider other than configured mock, then creates **three fictional records** (customer, product, ticket) and checks the draft and unchanged ticket. It also includes database and business API checks, a 401 for a request without a token, and `GET /api/auth/me`. The smoke test reads the token only from `API_TOKEN` (never from arguments) and never prints it. Use the read-only smoke command from the [quickstart](quickstart.md) when you only want read checks.

## Search the knowledge base

`python -m app.knowledge.ingest` reads `knowledge/*.md`, splits each article into section chunks, computes embeddings, writes them to the Qdrant collection `support_knowledge`, and records each article in the PostgreSQL table `knowledge_documents` (checksum, chunk count, embedding signature). Unchanged articles are skipped; changed articles are reindexed and their old extra chunks removed; articles deleted from the folder are removed from Qdrant and the registry.

```bash
auth="Authorization: Bearer $API_TOKEN"
curl --silent --show-error --fail-with-body --header "$auth" \
  http://127.0.0.1:8001/api/knowledge/status
curl --silent --show-error --fail-with-body --request POST --header "$auth" \
  http://127.0.0.1:8001/api/knowledge/search --header 'Content-Type: application/json' \
  --data '{"query":"Сервер не запускается после обновления","product_code":"techflow-server","limit":3}'
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
$auth = @{ Authorization = "Bearer $env:API_TOKEN" }
Invoke-RestMethod -Headers $auth http://127.0.0.1:8001/api/knowledge/status
$body = @{
  query = 'Сервер не запускается после обновления'
  product_code = 'techflow-server'
  limit = 3
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Headers $auth `
  -Uri http://127.0.0.1:8001/api/knowledge/search `
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

List pending suggestions and decide with a `reviewer` or `admin` token; the decision is recorded under the employee who owns the token:

```bash
auth="Authorization: Bearer $API_TOKEN"
curl --silent --show-error --fail-with-body --header "$auth" \
  'http://127.0.0.1:8001/api/suggestions?status=pending'
curl --silent --show-error --fail-with-body --request POST --header "$auth" \
  http://127.0.0.1:8001/api/suggestions/1/review --header 'Content-Type: application/json' \
  --data '{"decision":"approve","final_content":"Hello! Please check port 8443."}'
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
$auth = @{ Authorization = "Bearer $env:API_TOKEN" }
$json = 'application/json; charset=utf-8'
Invoke-RestMethod -Headers $auth 'http://127.0.0.1:8001/api/suggestions?status=pending'
$review = @{
  decision = 'approve'
  final_content = 'Hello! Please check port 8443.'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Headers $auth `
  -Uri http://127.0.0.1:8001/api/suggestions/1/review `
  -ContentType $json -Body ([System.Text.Encoding]::UTF8.GetBytes($review))
```

`approve` may replace the text with `final_content`; it creates a ticket message from the employee (simulated delivery, no email is sent) and moves the ticket to `waiting_customer`. `reject` requires a `comment` and changes nothing in the ticket. A suggestion is decided once: a second decision returns 409. A `resolved` or `closed` ticket receives no reply: approval returns 409 before the workflow resumes, and the suggestion can only be rejected. A request without a token (401), with an `operator` token (403), or with a token of an inactive employee (401) is rejected before the workflow resumes, so it keeps waiting. The field `reviewer_id` is no longer accepted (422): the author comes only from the token. Suggestions created before Stage 07 have no workflow and are decided directly.

```bash
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 60 --check-review
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
uv run --locked python scripts/smoke_test.py `
  --base-url http://127.0.0.1:8001 --timeout 60 --check-review
```

`--check-review` needs an `admin` token in `API_TOKEN`. It includes `--check-agent`, then checks that a decision without a token gets 401, creates a fictional `operator`, issues a one-day token for it, checks its 403, revokes the token and checks the 401, and only then approves the new suggestion as the employee of `API_TOKEN`: the delivered message, its author, the ticket status, the `delivered` workflow, and the 409 on a repeated decision. `--review-run RUN_ID` approves an existing waiting run and needs only a `reviewer` token. The [Stage 07 guide](../STAGE_07.md) explains the graph, checkpoints, and reliability rules.

## Operator endpoints

Stage 08 added read models for the operator interface; any HTTP client can use them:

| Operation | Result |
| --- | --- |
| `GET /api/dashboard` | Aggregates over the whole database: tickets, open and high-priority tickets, analyzed tickets, completed/failed runs, suggestion decisions, average analysis time, approval rate. Averages and rates are `null` when there is no data yet. |
| `GET /api/agent-runs?ticket_id=…` | Saved runs, including `failed` and `running`, without calling the model. |
| `GET /api/tickets/{resource_id}/messages` | Messages of one ticket, paginated like other lists. |
| `PATCH /api/tickets/{resource_id}` | Body `{"status": "…"}` changes only the ticket status; other fields are rejected with 422. It does not start the agent or decide suggestions. Requires `operator` and counts against the rate limit. |
| `GET /api/auth/me` | The employee, role, and token of the current request; the operator UI uses it after sign-in. |

```bash
curl --silent --show-error --fail-with-body \
  --header "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8001/api/dashboard
```

In PowerShell on Windows ([equivalents](windows.md)):

```powershell
$auth = @{ Authorization = "Bearer $env:API_TOKEN" }
Invoke-RestMethod -Headers $auth http://127.0.0.1:8001/api/dashboard
```

To prepare a reproducible demonstration through these APIs, run `scripts/demo_scenario.py` with a token in `API_TOKEN` (`operator` for preparation, `reviewer` or `admin` for `--verify-review`); see the [Stage 10 guide](../STAGE_10.md).
