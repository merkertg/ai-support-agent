# Этап 07. Процесс LangGraph и решение сотрудника

Цель — довести предложение агента до конца: сотрудник поддержки одобряет
или отклоняет черновик, а одобренный ответ «доставляется» сообщением в
обращении. Процесс описан графом **LangGraph**: после анализа граф
останавливается и ждёт решения человека. Ожидание может длиться сколько угодно
и переживает перезапуск API, потому что состояние графа хранится в PostgreSQL.

Как и раньше, основной сценарий работает в режиме `mock` без внешних запросов.
Цикл инструментов агента этапов 05–06 не переписан: LangGraph управляет
бизнес-процессом вокруг него. Доставка учебная — письмо клиенту не отправляется.

Контекст: [агент с инструментами](STAGE_05.md), [база знаний](STAGE_06.md),
[CI/CD](CI_CD.md). Фактические результаты проверок —
в [журнале разработки](DEVELOPMENT_LOG.md).

## 1. Что появилось и как это проверить

| Операция | Результат |
| --- | --- |
| `POST /api/users`, `GET /api/users`, `GET /api/users/{id}` | Сотрудники поддержки (`operator` или `admin`), которые принимают решения. |
| `POST /api/tickets/{id}/analyze` | Как раньше, `201` с запуском; теперь это первый узел графа. В ответе есть `workflow_thread_id`. |
| `GET /api/agent-runs/{id}/workflow` | Состояние графа из checkpoint: `awaiting_review`, `delivered`, `rejected` или `failed`. |
| `GET /api/suggestions?status=pending` | Очередь предложений, ожидающих решения. Фильтры: `status`, `ticket_id`. |
| `GET /api/suggestions/{id}` | Предложение, решение, сотрудник и доставленное сообщение. |
| `POST /api/suggestions/{id}/review` | Решение: `approve` (можно отредактировать текст) или `reject` (с причиной). Продолжает граф. |

Одобрение создаёт сообщение обращения от имени сотрудника и переводит
обращение в статус `waiting_customer`. Отклонение сохраняет причину и ничего
не меняет в обращении. Решение принимается один раз: повтор получает `409`.

## 2. Зачем LangGraph и что такое «человек в контуре»

**Человек в контуре** (human-in-the-loop) — шаг процесса, на котором решение
принимает человек, а не модель. Здесь это проверка черновика перед отправкой
клиенту: ИИ готовит ответ, сотрудник отвечает за то, что уходит наружу.

**LangGraph** описывает процесс как граф:

- **Узел** — шаг процесса, обычная функция Python.
- **Состояние** — словарь, который узлы читают и дополняют.
- **Ребро** — переход между узлами; условное ребро выбирает следующий узел
  по состоянию.
- **Поток** (thread) — один экземпляр процесса со своим `thread_id`.
- **Checkpointer** — хранилище: после каждого узла LangGraph сохраняет
  состояние потока. Здесь это `PostgresSaver` — таблицы в той же PostgreSQL.
- **interrupt()** — остановка внутри узла. Граф сохраняет состояние и
  завершает вызов; продолжение — `Command(resume=значение)`, и `interrupt()`
  возвращает это значение.

Без checkpointer ожидание решения жило бы только в памяти процесса и пропало
бы при перезапуске. С ним HTTP-запрос анализа и HTTP-запрос решения — два
отдельных вызова одного и того же сохранённого потока.

## 3. Граф процесса

```text
START
  │
  ▼
analyze ── агент этапов 05–06: инструменты, база знаний, AgentRun + pending
  │
  ├── outcome=failed ─────────────────────────────────────────▶ END
  ▼
human_review ── interrupt(): граф сохранён и ждёт решения сотрудника
  │                 POST /api/suggestions/{id}/review → Command(resume=решение)
  ├── approve ─▶ deliver ── approved + сообщение оператора + waiting_customer ─▶ END
  └── reject ──▶ reject ─── rejected + причина ─────────────────────────────▶ END
```

| Узел | Что делает |
| --- | --- |
| `analyze` | Вызывает `analyze_ticket()` этапа 05: запуск, инструменты, итог. Неудачный запуск сохраняется как `failed`, граф завершается. |
| `human_review` | Вызывает `interrupt()` с id запуска и предложения. При продолжении возвращает решение сотрудника. |
| `deliver` | Одной транзакцией: предложение `approved`, `final_content`, сотрудник и время, сообщение обращения, статус `waiting_customer`. |
| `reject` | Одной транзакцией: предложение `rejected`, сотрудник, время и причина. |

Состояние графа хранит **только идентификаторы и итоги**: id обращения,
запуска, предложения и сообщения, решение и итоговый статус. Текст обращения
и ключ лицензии в состояние не попадают: checkpoint лежит в PostgreSQL
открытым JSON. Подсказку ключа узел `analyze` получает через контекст
выполнения LangGraph (`context`), который не сохраняется.

Код графа — `app/workflow/graph.py`; запуск и продолжение — `app/workflow/service.py`.

## 4. Где хранится состояние и почему миграция, а не `setup()`

`langgraph-checkpoint-postgres` создаёт свои таблицы методом `setup()`:
`checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`.
В проекте схему меняет только Alembic, поэтому миграция `0004_review_workflow`
содержит **замороженную копию** этого DDL для установленной версии 3.1.2.

- Индексы создаются без `CONCURRENTLY`: миграция идёт в транзакции, таблицы пусты.
- В `checkpoint_migrations` записываются версии 0–9, поэтому `setup()`,
  если его когда-нибудь вызовут, ничего не изменит.
- У таблиц `checkpoint_*` нет моделей SQLAlchemy. `migrations/env.py`
  исключает их из сравнения, иначе `alembic check` предложил бы их удалить.
- Тест сравнивает копию DDL с `BasePostgresSaver.MIGRATIONS` установленной
  библиотеки: при обновлении пакета с новой схемой нужна новая миграция.

Та же миграция добавляет поля решения:

| Таблица и поле | Назначение |
| --- | --- |
| `agent_runs.workflow_thread_id` | `thread_id` потока LangGraph; уникальное, NULL — запуск до этапа 07. |
| `agent_suggestions.final_content` | Текст, который одобрил сотрудник (может отличаться от черновика). |
| `agent_suggestions.review_comment` | Комментарий или причина отклонения. |
| `agent_suggestions.delivered_message_id` | Сообщение обращения, созданное доставкой; `ON DELETE SET NULL`. |

CHECK в PostgreSQL: `final_content` и `delivered_message_id` бывают только
у `approved`. Ограничение этапа 02 по-прежнему требует сотрудника и время
у любого рассмотренного предложения.

Checkpointer подключается к той же БД через пул psycopg (`app/workflow/checkpoint.py`).
Пул выдаёт соединение только на время записи или чтения checkpoint, поэтому
ожидание модели в узле `analyze` не занимает соединение.

## 5. Сотрудники

Сотрудник — запись `users` этапа 02: `name`, уникальный `email`, `role`
(`operator` по умолчанию или `admin`), `is_active`. Решение принимает
сотрудник, указанный в `reviewer_id`; неактивный сотрудник получает `409`.

**Это не авторизация.** API не проверяет, кто отправил запрос: `reviewer_id`
— учебная отметка ответственного. Вход сотрудников и права доступа — отдельная
будущая работа.

## 6. Правила решения

Тело `POST /api/suggestions/{id}/review`:

| Поле | Обязательно | Правило |
| --- | --- | --- |
| `decision` | да | `approve` или `reject`. |
| `reviewer_id` | да | Положительный целый id существующего активного сотрудника. |
| `final_content` | нет | Только для `approve`: отредактированный текст до 6000 символов. Без него доставляется черновик. |
| `comment` | для `reject` | Причина до 2000 символов; при `approve` — необязательный комментарий. |

Лишние поля и нарушения правил дают `422` до обращения к БД и графу.

## 7. Надёжность: одно решение, одна доставка

- **Проверка до продолжения.** Сервис сначала убеждается, что предложение
  `pending`, сотрудник существует и активен, а граф стоит на `human_review`.
  Только потом вызывает `Command(resume=...)`. Неверный `reviewer_id` получает
  `404`, а граф продолжает ждать — ожидание не «расходуется» ошибкой.
- **Очередь параллельных решений.** Advisory lock PostgreSQL по id предложения
  выстраивает одновременные решения в очередь. Внутри узла `SELECT … FOR UPDATE`
  блокирует предложение и обращение до commit. Второе решение видит
  `approved`/`rejected` и получает `409 Предложение уже рассмотрено`.
- **Повторное продолжение безопасно.** Resume уже завершённого потока
  LangGraph ничего не выполняет. Сервис сверяет уникальный `review_id` своего
  решения с `applied_review_id` в состоянии и отвечает `409`, если применилось
  чужое решение.
- **Перезапуск.** Состояние — в PostgreSQL; новый процесс API продолжает
  поток по `thread_id` из `agent_runs`. CI проверяет это пересозданием контейнеров.
- **Неудачный анализ.** Запуск `failed` (например, лимит шагов) завершает граф:
  `GET .../workflow` показывает `failed`, предложения нет, решать нечего.
- **Ошибка до запуска** (нет обращения, неверные настройки) не оставляет
  пустой поток: сервис удаляет его из checkpoint.
- **Запуски до этапа 07** не имеют потока. Их ожидающие предложения решаются
  напрямую той же транзакцией, `workflow` в ответе — `null`.

## 8. Подготовить окружение

```bash
cd '/Users/arslanbakiev/Desktop/AI Forge/ai-support-agent'
uv sync --locked
docker compose up --build --detach --wait --wait-timeout 120 api
docker compose exec -T api python -m alembic current
docker compose exec -T api python -m app.knowledge.ingest
```

Ожидается `0004_review_workflow (head)`. Новые зависимости — `langgraph` 1.2.12
и `langgraph-checkpoint-postgres` 3.1.2. Compose задаёт `LANGSMITH_TRACING=false`:
трассы процесса во внешний сервис LangSmith не отправляются.

## 9. Учебный проход: одобрить ответ

Выполняй блоки по порядку **в одном терминале**.

### 9.1. Создать сотрудника и обращение

```bash
STAGE07_BASE_URL=http://127.0.0.1:8001
STAGE07_TMP=$(mktemp -d)
STAGE07_SUFFIX=$(uv run --locked python -c 'import uuid; print(uuid.uuid4().hex[:12])')
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE07_BASE_URL/api/users" --header 'Content-Type: application/json' \
  --data "{\"name\":\"Оператор этапа 07\",\"email\":\"stage07-$STAGE07_SUFFIX@example.com\"}" \
  --output "$STAGE07_TMP/user.json"
STAGE07_USER_ID=$(uv run --locked python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$STAGE07_TMP/user.json")
docker compose exec -T api python -m app.db.demo seed > "$STAGE07_TMP/demo.json"
uv run --locked python - "$STAGE07_TMP/demo.json" > "$STAGE07_TMP/ticket-request.json" <<'PY'
import json
import sys

demo = json.load(open(sys.argv[1]))
print(json.dumps({
    "customer_id": demo["customer"]["id"],
    "product_id": demo["product"]["id"],
    "subject": "Сервер не запускается после обновления",
    "description": "Служба останавливается через несколько секунд после старта.",
}, ensure_ascii=False))
PY
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE07_BASE_URL/api/tickets" --header 'Content-Type: application/json' \
  --data-binary "@$STAGE07_TMP/ticket-request.json" --output "$STAGE07_TMP/ticket.json"
STAGE07_TICKET_ID=$(uv run --locked python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$STAGE07_TMP/ticket.json")
```

`demo seed` идемпотентен и создаёт продукт `techflow-server`, по которому
агент найдёт статьи базы знаний.

### 9.2. Запустить анализ и увидеть ожидание

```bash
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE07_BASE_URL/api/tickets/$STAGE07_TICKET_ID/analyze" \
  --header 'Content-Type: application/json' --data '{"license_key":"LIC-1054"}' \
  --output "$STAGE07_TMP/run.json"
STAGE07_RUN_ID=$(uv run --locked python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$STAGE07_TMP/run.json")
STAGE07_SUGGESTION_ID=$(uv run --locked python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["suggestion"]["id"])' \
  "$STAGE07_TMP/run.json")
curl --silent --show-error --fail-with-body \
  "$STAGE07_BASE_URL/api/agent-runs/$STAGE07_RUN_ID/workflow" \
  --output "$STAGE07_TMP/waiting.json"
uv run --locked python - "$STAGE07_TMP/waiting.json" "$STAGE07_TMP/run.json" <<'PY'
import json
import sys

waiting, run = (json.load(open(path)) for path in sys.argv[1:])
assert waiting["state"] == "awaiting_review"
assert waiting["next"] == ["human_review"]
assert waiting["thread_id"] == run["workflow_thread_id"]
assert run["suggestion"]["status"] == "pending"
print("Граф ждёт решения:", waiting["thread_id"])
PY
curl --silent --show-error --fail-with-body \
  "$STAGE07_BASE_URL/api/suggestions?status=pending&ticket_id=$STAGE07_TICKET_ID"
```

Ответ `workflow` выглядит так (id и `thread_id` у тебя другие):

```json
{
  "thread_id": "7fbe3421a9814ffabfe9b9df73bfd391",
  "state": "awaiting_review",
  "next": ["human_review"],
  "run_id": 2,
  "suggestion_id": 2
}
```

### 9.3. Одобрить с правкой текста

```bash
uv run --locked python - "$STAGE07_USER_ID" > "$STAGE07_TMP/review.json" <<'PY'
import json
import sys

print(json.dumps({
    "decision": "approve",
    "reviewer_id": int(sys.argv[1]),
    "final_content": "Здравствуйте! Проверьте, свободен ли порт 8443, и пришлите фрагмент server.log.",
    "comment": "Сократил черновик",
}, ensure_ascii=False))
PY
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE07_BASE_URL/api/suggestions/$STAGE07_SUGGESTION_ID/review" \
  --header 'Content-Type: application/json' --data-binary "@$STAGE07_TMP/review.json" \
  --output "$STAGE07_TMP/result.json"
uv run --locked python - "$STAGE07_TMP/result.json" "$STAGE07_USER_ID" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1]))
user_id = int(sys.argv[2])
suggestion, message = result["suggestion"], result["message"]
assert suggestion["status"] == "approved" and suggestion["reviewed_by"] == user_id
assert message["author_type"] == "operator" and message["user_id"] == user_id
assert message["content"] == suggestion["final_content"]
assert suggestion["delivered_message_id"] == message["id"]
assert result["ticket_status"] == "waiting_customer"
assert result["workflow"]["state"] == "delivered" and result["workflow"]["next"] == []
print("Ответ доставлен сообщением", message["id"])
PY
```

### 9.4. Убедиться, что повторное решение невозможно

```bash
curl --silent --output /dev/null --write-out '%{http_code}\n' --request POST \
  "$STAGE07_BASE_URL/api/suggestions/$STAGE07_SUGGESTION_ID/review" \
  --header 'Content-Type: application/json' \
  --data "{\"decision\":\"reject\",\"reviewer_id\":$STAGE07_USER_ID,\"comment\":\"Поздно\"}"
curl --silent --show-error --fail-with-body \
  "$STAGE07_BASE_URL/api/tickets/$STAGE07_TICKET_ID"
```

Первая команда печатает `409`. Обращение имеет статус `waiting_customer`.

## 10. Учебный проход: отклонить и продолжить после перезапуска

Новый анализ того же обращения создаёт новый запуск и новый поток. Перезапусти
API между анализом и решением: граф продолжится из checkpoint.

```bash
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE07_BASE_URL/api/tickets/$STAGE07_TICKET_ID/analyze" \
  --output "$STAGE07_TMP/run2.json"
STAGE07_SUGGESTION2_ID=$(uv run --locked python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["suggestion"]["id"])' \
  "$STAGE07_TMP/run2.json")
docker compose restart api
docker compose up --detach --wait --wait-timeout 120 api
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE07_BASE_URL/api/suggestions/$STAGE07_SUGGESTION2_ID/review" \
  --header 'Content-Type: application/json' \
  --data "{\"decision\":\"reject\",\"reviewer_id\":$STAGE07_USER_ID,\"comment\":\"Нужна другая статья\"}" \
  --output "$STAGE07_TMP/rejected.json"
uv run --locked python - "$STAGE07_TMP/rejected.json" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1]))
assert result["suggestion"]["status"] == "rejected"
assert result["suggestion"]["review_comment"] == "Нужна другая статья"
assert result["message"] is None
assert result["workflow"]["state"] == "rejected"
print("Отклонено после перезапуска API; статус обращения:", result["ticket_status"])
PY
```

Отклонение не создаёт сообщение и не меняет статус обращения.

## 11. Ошибки и порядок диагностики

| HTTP | Причина | Что проверить |
| --- | --- | --- |
| `404` | Нет предложения, сотрудника, запуска или процесса; запуск до этапа 07 для `/workflow`. | id из фактических ответов, `GET /api/users`. |
| `409` | Предложение уже рассмотрено; сотрудник неактивен; граф не ждёт решения; повтор email сотрудника. | `GET /api/suggestions/{id}` и `GET .../workflow`. |
| `422` | `reject` без `comment`, `final_content` при `reject`, неверный `decision`, лишние поля. | Тело запроса по разделу 6. |
| `502`/`504` | Ошибка анализа (как на этапе 05): запуск `failed`, граф завершён. | `run_id` из ответа и `GET .../workflow` → `failed`. |
| `503` | PostgreSQL недоступна (в том числе для checkpointer). | `/ready`, логи `db` и `api`. |

## 12. Файлы и порядок чтения кода

| Файл | Назначение |
| --- | --- |
| `app/workflow/graph.py` | Состояние, контекст, узлы `analyze`, `human_review`, `deliver`, `reject`, условные рёбра. |
| `app/workflow/service.py` | Запуск графа, чтение состояния, проверка и продолжение решением. |
| `app/workflow/checkpoint.py` | Пул psycopg и `PostgresSaver`; адрес из `DATABASE_URL`. |
| `app/services/reviews.py` | Очередь, проверка, атомарное решение и учебная доставка. |
| `app/api/reviews.py`, `app/schemas/reviews.py` | Маршруты решения, очереди и состояния графа; схемы. |
| `app/api/routes.py`, `app/schemas/resources.py` | API сотрудников. |
| `migrations/versions/0004_review_workflow.py`, `migrations/env.py` | Поля решения, таблицы checkpointer и их исключение из `alembic check`. |
| `tests/conftest.py` | Checkpointer в памяти для всех тестов: состояние не пишется в development-БД. |

Начни с `build_graph()` и `human_review()`, затем прочитай `start_analysis()`
и `review_suggestion()` в сервисе: там видно, когда граф запускается, где
останавливается и чем продолжается. После этого — `apply_review()`.

## 13. Автоматические проверки

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -m 'not integration' -q
docker compose --profile test up --detach --wait db-test qdrant-test
TEST_DATABASE_URL=postgresql+psycopg://support:support_ci@127.0.0.1:5434/ai_support_test \
TEST_QDRANT_URL=http://127.0.0.1:6336 \
  uv run --locked pytest -q
```

Unit-тесты проверяют граф на `InMemorySaver`: остановку, обе ветви решения,
неудачный анализ, повторный resume, отсутствие ключа лицензии в состоянии,
удаление пустого потока и совпадение замороженного DDL с библиотекой.
Интеграционные тесты используют настоящий `PostgresSaver` на `ai_support_test`:
одобрение с правкой, отклонение, неверный и неактивный сотрудник, повтор,
продолжение с новым пулом (имитация перезапуска), два параллельных решения
(ровно одна доставка), старые предложения без потока, CHECK, `setup()` без
изменений и миграция `0003 → 0004` с сохранением строк.

Проверка работающего стенда:

```bash
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 60 --check-review
```

`--check-review` включает `--check-agent`, затем проверяет ожидание графа,
создаёт учебного сотрудника, одобряет предложение с правкой, проверяет
сообщение, `waiting_customer`, итог `delivered` и `409` на повтор.
`--review-run RUN_ID` выполняет те же проверки для уже существующего запуска.

## 14. Что теперь проверяет CI/CD

Контейнерное задание запускает `--check-review`, затем оставляет ещё один
запуск в ожидании решения. После `down` без `--volumes` и нового `up`
сравниваются демо-данные, рассмотренный запуск и выдача поиска, а
`--review-run` продолжает отложенный граф уже в новых контейнерах. Это
подтверждает, что ожидание решения переживает перезапуск. Задание `checks`
выполняет новые unit- и интеграционные тесты на PostgreSQL.

## 15. Частые проблемы

**`409 Процесс проверки не ожидает решения`.** Граф уже завершён или анализ
не удался. Посмотри `GET /api/agent-runs/{id}/workflow`.

**`404` для `/workflow` старого запуска.** Запуск создан до этапа 07 и потока
не имеет; его предложение всё равно можно рассмотреть.

**`alembic check` предлагает удалить `checkpoint_*`.** Проверь фильтр
`include_name` в `migrations/env.py`.

**После обновления `langgraph-checkpoint-postgres` упал тест DDL.** Новая
версия изменила схему checkpoint: нужна новая миграция с её изменениями.

## 16. Критерий готовности и следующая граница

Этап готов, когда анализ останавливается в ожидании решения, одобрение
создаёт ровно одно сообщение и переводит обращение в `waiting_customer`,
отклонение сохраняет причину без изменений обращения, повтор получает `409`,
а ожидание переживает перезапуск API. Полный набор тестов проходит на PostgreSQL.

Дальше по плану — интерфейс оператора на Streamlit поверх этих операций.
В этой итерации нет входа сотрудников и прав доступа, настоящей отправки
писем, переназначения и сроков проверки, а также оценки качества ответов.

Результаты локальных проверок и статус удалённого запуска — в
[журнале разработки](DEVELOPMENT_LOG.md).
