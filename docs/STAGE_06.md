# Этап 06. База знаний, эмбеддинги и поиск в Qdrant (RAG)

Цель — дать помощнику доступ к статьям технической документации. Статьи лежат
в репозитории как Markdown-файлы, команда индексации делит их на фрагменты,
вычисляет эмбеддинги и записывает их в векторную БД Qdrant. Оператор может
искать по базе знаний через API, а агент этапа 05 получает пятый инструмент
`search_knowledge` и указывает в черновике ключи статей, на которые опирается.

Как и на этапе 05, пользователь оставил `mock`: основной сценарий работает
локально, без внешних запросов и расходов. Mock-эмбеддинг — лексический вектор
по словам текста, а не смысловая модель. Адаптер OpenAI для эмбеддингов
подготовлен и проверяется только с подменой HTTP-транспорта.

Контекст предыдущих шагов: [БД и миграции](STAGE_02.md),
[HTTP API ресурсов](STAGE_03.md), [LLM-клиент](STAGE_04.md),
[агент с инструментами](STAGE_05.md), [CI/CD и GHCR](CI_CD.md).
Фактические результаты проверок — в [журнале разработки](DEVELOPMENT_LOG.md).

> **Windows.** Команды в этом руководстве написаны для Bash (Linux, macOS).
> В Windows выполняй их в Git Bash или WSL 2 либо используй аналоги для PowerShell
> из страницы [Windows и PowerShell](instructions/windows.md).

## 1. Что появилось и как это проверить

| Операция | Результат |
| --- | --- |
| `python -m app.knowledge.ingest` | Прочитать `knowledge/*.md`, записать фрагменты в Qdrant и реестр статей в PostgreSQL. Повторный запуск без изменений ничего не переписывает. |
| `GET /api/knowledge/status` | Показать провайдера эмбеддингов, состояние коллекции, число точек Qdrant и статей реестра. Эмбеддинги не вычисляются. |
| `POST /api/knowledge/search` | Найти фрагменты статей по тексту запроса; необязательный `product_code` ограничивает выдачу статьями продукта и общими статьями. |
| `POST /api/tickets/{id}/analyze` | Как на этапе 05, но агент дополнительно вызывает `search_knowledge`, а результат содержит `sources`. |

Поиск только читает: он не меняет статьи, реестр и обращения. Агент по-прежнему
сохраняет черновик со статусом `pending` и ничего не отправляет клиенту.
`/ready` проверяет только PostgreSQL и миграции; состояние Qdrant показывает
`/api/knowledge/status`.

## 2. RAG, эмбеддинг и векторная БД простыми словами

**RAG** (Retrieval-Augmented Generation) — генерация ответа с опорой на найденные
документы. Сначала система ищет подходящие фрагменты, затем модель пишет ответ,
используя их текст, и указывает источники.

**Эмбеддинг** — список чисел фиксированной длины (здесь 256), вычисленный
из текста. Тексты с похожим содержанием дают похожие векторы. Похожесть
измеряется **косинусным сходством**: 1 — одинаковое направление, около 0 —
ничего общего.

**Векторная БД** хранит векторы и быстро находит ближайшие к вектору запроса.
Qdrant хранит **точки**: у каждой есть ID, вектор и **payload** — обычные поля,
по которым можно фильтровать (ключ статьи, продукт, текст фрагмента).

```text
knowledge/*.md
     │  python -m app.knowledge.ingest
     ▼
Разбор заголовка → фрагменты по разделам «## » → эмбеддинги
     │                                              │
     ▼                                              ▼
PostgreSQL: реестр knowledge_documents       Qdrant: точки с векторами
(контрольная сумма, число фрагментов)       и payload (ключ, раздел, текст)

POST /api/knowledge/search ──┐
search_knowledge агента ─────┴─→ эмбеддинг запроса → Qdrant (фильтр продукта)
                                                      → фрагменты + источники
```

Реестр в PostgreSQL отвечает на вопрос «какие статьи и какой версии
проиндексированы». Тексты и векторы хранит Qdrant. Если удалить том Qdrant,
реестр не помешает восстановлению: индексация сверяет число точек в самом Qdrant.

## 3. Статьи базы знаний

Статьи находятся в `knowledge/`. Это вымышленные материалы о продуктах TechFlow;
каждая начинается пометкой об учебном характере.

| Ключ | Продукт | Тема |
| --- | --- | --- |
| `KB-SERVER-START` | `techflow-server` | Сервер не запускается после обновления |
| `KB-SERVER-LOGS` | `techflow-server` | Журналы и диагностический архив |
| `KB-BACKUP-RESTORE` | `techflow-server` | Резервное копирование и восстановление |
| `KB-DESKTOP-SYNC` | `techflow-desktop` | Desktop не синхронизирует проекты |
| `KB-LICENSE-ACTIVATION` | общая | Статусы и проверка лицензии |
| `KB-SUPPORT-DATA` | общая | Какие сведения приложить к обращению |
| `KB-PASSWORD-RESET` | общая | Сброс пароля пользователя |

Формат файла:

```markdown
---
document_key: KB-SERVER-START
title: TechFlow Server не запускается после обновления
product_code: techflow-server
product_version: 5.x
---

Вводный абзац относится к названию статьи.

## Частые причины

Текст раздела.
```

Правила заголовка:

- `document_key` — обязательный ключ вида `KB-СЛОВО-СЛОВО` из заглавных
  латинских букв и цифр. Его агент цитирует в `sources`.
- `title` — обязательное название до 300 символов.
- `product_code` — код продукта из каталога (`/api/products`). Без него статья
  **общая** и видна для любого продукта. Значение `general` зарезервировано:
  его нельзя использовать как код продукта или явно указывать в заголовке
  продуктовой статьи. Для общей статьи поле `product_code` нужно опустить.
- `product_version` — необязательная версия для справки.
- Неизвестное поле, повтор поля, файл не в UTF-8 или больше 200 000 байт —
  ошибка индексации с именем файла и причиной.

**Фрагмент** — единица поиска. Текст делится по заголовкам `## `; раздел длиннее
1200 символов делится по абзацам, слишком длинный абзац — по словам. Для
эмбеддинга к тексту фрагмента добавляются название статьи и раздела, поэтому
слова заголовков тоже помогают найти фрагмент.

## 4. Эмбеддинги: mock и OpenAI

`EMBEDDING_PROVIDER=mock` (по умолчанию) строит **лексический** вектор без сети:

1. Текст переводится в нижний регистр, `ё` заменяется на `е`.
2. Слова короче трёх букв и служебные слова («для», «после», «что») отбрасываются.
3. Каждое слово усекается до пяти букв: «ошибка», «ошибке», «ошибки» → «ошибк».
4. SHA-256 основы выбирает одну из 256 координат и знак +1 или −1.
5. Вектор нормируется до длины 1.

Такой вектор находит тексты с общими словами. Синонимы без общих слов mock
не распознаёт, а редкие совпадения хешей дают небольшой шум. Поэтому поиск
отбрасывает фрагменты со сходством ниже `KNOWLEDGE_MIN_SCORE=0.2`: на учебных
статьях подходящие фрагменты набирают примерно 0.22–0.65, посторонние — до 0.15.

`EMBEDDING_PROVIDER=openai` вызывает `embeddings.create` официального SDK
с явной моделью `OPENAI_EMBEDDING_MODEL` и размерностью `EMBEDDING_DIMENSIONS`.
Ключ берётся из `OPENAI_API_KEY`. Тексты отправляются пакетами по 64, повторов
SDK нет, ответ проверяется: порядок, число векторов, длина и конечные числа.

У каждого провайдера есть **подпись пространства векторов**, например
`mock:mock-hash-v1:256`. Она записывается в payload каждой точки и в реестр.
Поиск сравнивает только точки той же подписи: векторы разных моделей
несовместимы, даже если их длина совпадает. После смены провайдера или модели
выполни индексацию заново; при смене размерности укажи новую коллекцию
`QDRANT_COLLECTION`, иначе статус покажет `incompatible`.

## 5. Настройки

| Переменная | Начальное значение | Назначение |
| --- | --- | --- |
| `QDRANT_URL` | `http://127.0.0.1:6335` | Адрес Qdrant для Python на Mac. Пустое значение — база знаний не настроена. |
| `QDRANT_URL_DOCKER` | `http://qdrant:6333` | Адрес для `api` внутри Compose. |
| `QDRANT_PORT` / `QDRANT_TEST_PORT` | `6335` / `6336` | Опубликованные порты основного и тестового Qdrant. |
| `QDRANT_COLLECTION` | `support_knowledge` | Имя коллекции: латиница, цифры, `_` и `-`, до 64 символов. |
| `QDRANT_TIMEOUT_SECONDS` | `5` | От 1 до 30 секунд на запрос к Qdrant. |
| `EMBEDDING_PROVIDER` | `mock` | `mock` или `openai`. |
| `OPENAI_EMBEDDING_MODEL` | пусто | Для `openai` задаётся явно; платной модели по умолчанию нет. |
| `EMBEDDING_DIMENSIONS` | `256` | От 64 до 3072; одинаковая для индексации и поиска. |
| `KNOWLEDGE_MIN_SCORE` | `0.2` | Минимальное косинусное сходство от 0 до 1. |

Неверное значение даёт безопасный ответ `503` без значения из `.env`.
Qdrant в Compose запускается с `QDRANT__TELEMETRY_DISABLED=true`: учебный
стенд не отправляет анонимную телеметрию.

## 6. Подготовить окружение

Открой терминал в корне проекта и установи зависимости из `uv.lock`
(добавился `qdrant-client` 1.19.1). Команда выполняется из корня проекта:

```bash
uv sync --locked
```

Добавь в существующий `.env` недостающие переменные этапа 06 из `.env.example`.
Если `.env` нет, создай его из образца:

```bash
if [ ! -f .env ]; then
  cp .env.example .env
fi
grep -E '^(QDRANT|EMBEDDING|KNOWLEDGE)' .env.example
```

Для Compose достаточно значений по умолчанию; `QDRANT_URL` нужен Python на Mac.
Собери образ и запусти стенд:

```bash
docker compose up --build --detach --wait --wait-timeout 90 api
docker compose ps --all
docker compose exec -T api python -m alembic current
```

Порядок запуска: `db healthy → migrate exit 0`, `qdrant healthy → api healthy`.
Новая миграция `0003_knowledge_index` расширяет `knowledge_documents`.
Healthcheck Qdrant обращается к `/readyz` через встроенный `bash`: в образе
нет `curl`. Qdrant доступен на `127.0.0.1:6335`, данные хранятся в томе
`qdrant_data` и переживают обычный `docker compose down`.

## 7. Проиндексировать статьи

```bash
docker compose exec -T api python -m app.knowledge.ingest
```

Первый запуск выводит JSON такого вида:

```json
{
  "chunks": 27,
  "collection": "support_knowledge",
  "documents": 7,
  "embedding_signature": "mock:mock-hash-v1:256",
  "indexed": ["KB-BACKUP-RESTORE", "KB-DESKTOP-SYNC", "..."],
  "removed": [],
  "unchanged": []
}
```

| Поле | Значение |
| --- | --- |
| `documents`, `chunks` | Сколько статей и фрагментов найдено в каталоге. |
| `indexed` | Статьи, записанные в Qdrant заново: новые, изменённые или потерявшие точки. |
| `unchanged` | Статьи с той же контрольной суммой, подписью и числом точек — без записи векторов. |
| `removed` | Статьи, которых больше нет в каталоге: удалены из Qdrant и из реестра. |

Повтори команду: теперь все ключи окажутся в `unchanged`. Индексация
**идемпотентна**: ID точки — UUID5 от `ключ:номер фрагмента`, поэтому повторная
запись перезаписывает ту же точку, а лишние старые фрагменты сокращённой статьи
удаляются отдельным запросом.

Порядок записи выбран так, чтобы сбой не оставлял ложного «проиндексировано»:
сначала вычисляются все векторы статьи и записываются в Qdrant, затем отдельной
транзакцией обновляется реестр. Если commit реестра не удался, следующий запуск
увидит старую контрольную сумму и повторит индексацию. Во время эмбеддингов
и запросов к Qdrant SQL-соединение не удерживается.

Если в каталоге продуктов уже есть код из заголовка статьи (например,
`techflow-server` после `python -m app.db.demo seed`), реестр заполняет справочное
поле `product_id`. Область поиска от этого не зависит: её задаёт `product_code`.

## 8. Учебный проход: поиск по базе знаний

Выполняй блоки по порядку **в одном терминале**: переменные сохраняют ответы.

### 8.1. Проверить статус индекса

```bash
STAGE06_BASE_URL=http://127.0.0.1:8001
STAGE06_TMP=$(mktemp -d)
curl --silent --show-error --fail-with-body \
  "$STAGE06_BASE_URL/api/knowledge/status" --output "$STAGE06_TMP/status.json"
uv run --locked python - "$STAGE06_TMP/status.json" <<'PY'
import json
import sys

status = json.load(open(sys.argv[1]))
assert status["embedding_provider"] == "mock", "Сначала включи EMBEDDING_PROVIDER=mock"
assert status["index"] == "ready", "Выполни python -m app.knowledge.ingest"
assert status["points"] >= status["documents"] >= 7
print(f"Индекс готов: статей {status['documents']}, точек {status['points']}.")
PY
```

`index` принимает значения: `ready` — можно искать; `missing` — индексация
ещё не выполнялась; `incompatible` — коллекция создана с другой размерностью;
`unavailable` — Qdrant не отвечает; `not_configured` — нет `QDRANT_URL`.

### 8.2. Найти статью продукта

```bash
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE06_BASE_URL/api/knowledge/search" \
  --header 'Content-Type: application/json' \
  --data '{"query":"Сервер не запускается после обновления","product_code":"techflow-server","limit":3}' \
  --output "$STAGE06_TMP/server.json"
uv run --locked python - "$STAGE06_TMP/server.json" <<'PY'
import json
import sys

found = json.load(open(sys.argv[1]))
top = found["results"][0]
assert top["document_key"] == "KB-SERVER-START"
assert top["source_path"] == "knowledge/kb-server-start.md"
assert all(hit["scope"] in {"techflow-server", "general"} for hit in found["results"])
for hit in found["results"]:
    print(f"{hit['score']:.3f} {hit['document_key']} — {hit['section']}")
PY
```

Каждый результат — фрагмент: `document_key`, `title`, `section`, `text`,
`score`, `source_path` и `scope` (код продукта или `general`). Несколько
фрагментов одной статьи могут идти подряд: это разные разделы.

### 8.3. Убедиться, что фильтр продукта работает

```bash
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE06_BASE_URL/api/knowledge/search" \
  --header 'Content-Type: application/json' \
  --data '{"query":"Сервер не запускается после обновления","product_code":"techflow-desktop"}' \
  --output "$STAGE06_TMP/desktop.json"
uv run --locked python - "$STAGE06_TMP/desktop.json" <<'PY'
import json
import sys

found = json.load(open(sys.argv[1]))
assert all(hit["scope"] != "techflow-server" for hit in found["results"])
print("Статьи сервера не видны для Desktop:", len(found["results"]), "результатов.")
PY
```

Без `product_code` поиск идёт по всем статьям. Ограничения тела: `query`
от 3 до 300 символов, `limit` от 1 до 10 (по умолчанию 5), лишние поля — `422`.

## 9. Учебный проход: агент со ссылками на статьи

Агент ищет статьи продукта обращения. Создай демонстрационные клиента, продукт
`techflow-server` и лицензию `LIC-1054`, затем обращение по этому продукту:

```bash
docker compose exec -T api python -m app.db.demo seed
docker compose exec -T api python -m app.db.demo show > "$STAGE06_TMP/demo.json"
uv run --locked python - "$STAGE06_TMP/demo.json" > "$STAGE06_TMP/ticket-request.json" <<'PY'
import json
import sys

demo = json.load(open(sys.argv[1]))
print(json.dumps({
    "customer_id": demo["customer"]["id"],
    "product_id": demo["product"]["id"],
    "subject": "Сервер не запускается после обновления",
    "description": "После установки новой версии служба останавливается через несколько секунд.",
    "priority": "high",
}, ensure_ascii=False))
PY
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE06_BASE_URL/api/tickets" --header 'Content-Type: application/json' \
  --data-binary "@$STAGE06_TMP/ticket-request.json" \
  --output "$STAGE06_TMP/ticket.json"
STAGE06_TICKET_ID=$(uv run --locked python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["id"])' \
  "$STAGE06_TMP/ticket.json")
```

`seed` идемпотентен: повторный запуск не создаёт копии. Запусти анализ с ключом
лицензии и проверь источники:

```bash
curl --silent --show-error --fail-with-body --request POST \
  "$STAGE06_BASE_URL/api/tickets/$STAGE06_TICKET_ID/analyze" \
  --header 'Content-Type: application/json' --data '{"license_key":"LIC-1054"}' \
  --output "$STAGE06_TMP/analyze.json"
STAGE06_RUN_ID=$(uv run --locked python - "$STAGE06_TMP/analyze.json" <<'PY'
import json
import sys

run = json.load(open(sys.argv[1]))
assert run["status"] == "completed"
names = [item["name"] for item in run["tool_trace"]]
assert names[-1] == "search_knowledge" and len(names) == 5
search = run["tool_trace"][-1]["result"]
assert search["ok"] is True
found = [item["document_key"] for item in search["data"]["documents"]]
assert run["result"]["sources"] and set(run["result"]["sources"]) <= set(found)
assert "KB-SERVER-START" in run["result"]["sources"]
assert run["suggestion"]["status"] == "pending"
print(run["id"])
PY
)
curl --silent --show-error --fail-with-body \
  "$STAGE06_BASE_URL/api/agent-runs/$STAGE06_RUN_ID" --output "$STAGE06_TMP/run.json"
cmp "$STAGE06_TMP/analyze.json" "$STAGE06_TMP/run.json"
uv run --locked python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["result"]["draft_reply"])' \
  "$STAGE06_TMP/run.json"
```

Mock-агент выполняет три шага. Во втором пакете к `get_customer`,
`get_ticket_history` и `get_license` добавляется `search_knowledge` с запросом
из темы и описания обращения. Финальный черновик перечисляет до трёх найденных
статей, а `sources` содержит их ключи. Пятый вызов расходует общий лимит
`AGENT_MAX_TOOL_CALLS`; начального значения 12 достаточно.

## 10. Как агент ищет и почему нельзя выдумать источник

Аргументы `search_knowledge` — только `query` (3–300 символов) и `limit` (1–5).
Код продукта модель не передаёт: сервер берёт его из обращения текущего запуска
и ищет статьи этого продукта и общие. Поиск возвращает лучший фрагмент каждой
статьи (до 800 символов текста), чтобы несколько результатов были разными
источниками.

SQL-сессия инструмента проверяет область обращения и читает код продукта,
затем закрывается; запрос эмбеддингов и Qdrant идёт без занятого соединения.
Тексты статей — недоверенные данные, как и текст обращения: системные инструкции
запрещают исполнять найденные в них указания.

Финальный результат агента теперь имеет поле `sources` — список ключей до пяти
без повторов. Runtime сверяет его с результатами `search_knowledge` **этого**
запуска. Ссылка на статью, которую поиск не вернул, останавливает запуск:
`502` и сохранённый `failed` с текстом «Черновик ссылается на источник, который
не был найден в базе знаний.». Пустой `sources` допустим: ответ без статей.

Если база знаний не настроена или Qdrant недоступен, инструмент возвращает
модели ошибку `knowledge_unavailable`, а анализ продолжается. Mock пишет
«База знаний недоступна: источники не использовались.» и оставляет `sources`
пустым. Сбой PostgreSQL по-прежнему прерывает запуск: без проверенных данных
обращения ответ готовить нельзя.

Сохранённые запуски этапа 05 не имели `sources`; при чтении они получают
пустой список.

## 11. Что хранится в Qdrant и PostgreSQL

Payload точки Qdrant:

| Поле | Назначение |
| --- | --- |
| `document_key`, `title`, `section`, `text` | Источник и текст фрагмента для выдачи. |
| `chunk_index` | Номер фрагмента; по нему удаляются лишние старые фрагменты. |
| `scope` | Код продукта или `general`; по нему фильтрует поиск. |
| `source_path`, `checksum`, `product_version` | Путь файла, его SHA-256 и версия продукта. |
| `embedding_signature` | Провайдер, модель и размерность вектора. |

Для `document_key`, `scope` и `embedding_signature` создаются keyword-индексы
payload: фильтры не перебирают все точки. Коллекция использует косинусное
расстояние.

Миграция `0003_knowledge_index` добавляет в `knowledge_documents`:

| Поле | PostgreSQL | Назначение |
| --- | --- | --- |
| `product_code` | `VARCHAR(64)`, NULL | Область статьи; NULL — общая. |
| `chunk_count` | `INTEGER NOT NULL`, CHECK `>= 0` | Сколько фрагментов записано. |
| `embedding_signature` | `VARCHAR(300)`, NULL | Пространство векторов индекса. |
| `indexed_at` | `TIMESTAMPTZ`, NULL | Время последней записи векторов. |

Существующие строки получают `chunk_count=0` и пустые сведения: такие статьи
будут проиндексированы. Откат миграции удаляет только эти поля; точки в Qdrant
он не трогает. Проверку отката выполняют только тесты на одноразовой БД.

## 12. Ошибки и порядок диагностики

| HTTP | Причина | Что проверить |
| --- | --- | --- |
| `422` | Неверное тело поиска или лишнее поле. | Длину `query`, диапазон `limit`, поля JSON. |
| `502` | Эмбеддинги вернули неверный ответ; агент сослался на ненайденную статью. | Сохранённый trace запуска; размерность модели OpenAI. |
| `503` | Нет `QDRANT_URL`, неверные настройки, индекс не создан, другая размерность, Qdrant недоступен. | `/api/knowledge/status`, логи `qdrant`, повторный `ingest`. |
| `504` | Qdrant или эмбеддинги не ответили вовремя. | `QDRANT_TIMEOUT_SECONDS`, доступность сервиса. |

Тексты ошибок постоянные: адрес Qdrant, ключ и ответ SDK наружу не выдаются.
Ошибки статьи при индексации называют файл и причину, но не печатают текст.

## 13. OpenAI-эмбеддинги без ключа в CI

Тесты вызывают настоящий метод `embeddings.create` установленного SDK
с `httpx2.MockTransport`: проверяются отправленные модель, размерность и тексты,
пакеты по 64, сортировка по `index`, неверный размер вектора и ошибки 400/401/
404/429/500 и тайм-аут. Реальный ключ и сеть не нужны.

Чтобы позже включить OpenAI, задай `EMBEDDING_PROVIDER=openai`,
`OPENAI_API_KEY`, `OPENAI_EMBEDDING_MODEL` и размерность, которую поддерживает
модель. Затем пересоздай `api` и выполни индексацию. Это отправит тексты статей
выбранному провайдеру и будет платным. Текущие проверки не подтверждают доступ
к модели и качество поиска с настоящими эмбеддингами.

## 14. Файлы и порядок чтения кода

| Файл | Назначение |
| --- | --- |
| `knowledge/*.md` | Вымышленные статьи базы знаний. |
| `app/knowledge/documents.py` | Разбор заголовка, контрольная сумма, деление на фрагменты. |
| `app/knowledge/embeddings.py` | Mock-вектор, OpenAI-адаптер, подпись пространства векторов. |
| `app/knowledge/index.py` | Коллекция Qdrant, запись и удаление статьи, поиск с фильтрами. |
| `app/knowledge/ingest.py` | Команда индексации и сверка с реестром PostgreSQL. |
| `app/knowledge/config.py`, `errors.py` | Настройки и безопасные ошибки. |
| `app/schemas/knowledge.py`, `app/services/knowledge.py`, `app/api/knowledge.py` | Поиск и статус через HTTP. |
| `app/agent/tools.py`, `provider.py`, `runtime.py` | Инструмент `search_knowledge`, mock со ссылками, проверка `sources`. |
| `app/schemas/drafts.py` | `AgentDraftContent` с обязательным `sources`. |
| `migrations/versions/0003_knowledge_index.py` | Поля реестра индексации. |
| `compose.yaml`, `Dockerfile` | Сервисы `qdrant` и `qdrant-test`; статьи в образе. |

Начни с одной статьи в `knowledge/` и `load_document()`, затем прочитай
`MockEmbedder.vector()` и `KnowledgeIndex.replace_document()`/`search()`.
После этого — `ingest()` и инструмент `_search()` в `app/agent/tools.py`.

## 15. Автоматические проверки

Без внешних сервисов:

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -m 'not integration' -q
```

Unit-тесты используют Qdrant **в памяти процесса** (`QdrantClient(":memory:")`):
разбор статей, mock-векторы, OpenAI SDK с подменой транспорта, индекс, фильтры,
HTTP-маршруты, инструмент агента и проверку `sources`.

Полный набор требует отдельные одноразовые PostgreSQL и Qdrant:

```bash
docker compose --profile test up --detach --wait db-test qdrant-test
TEST_DATABASE_URL=postgresql+psycopg://support:support_ci@127.0.0.1:5434/ai_support_test \
TEST_QDRANT_URL=http://127.0.0.1:6336 \
  uv run --locked pytest -q
```

`qdrant-test` хранит данные в памяти контейнера. Каждый тест создаёт
уникальную коллекцию `test_*` и удаляет её. Без `TEST_QDRANT_URL` интеграционные
тесты базы знаний локально пропускаются; в CI отсутствие адреса — ошибка.
Проверяются индексация и реестр, идемпотентность, изменение и удаление статьи,
восстановление потерянных точек, удаление «осиротевших» точек, поиск и статус
через HTTP, агент со ссылками на настоящий Qdrant и миграция `0002 → 0003`.

Проверка работающего стенда (после `ingest`):

```bash
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 60 --check-knowledge
uv run --locked python scripts/smoke_test.py \
  --base-url http://127.0.0.1:8001 --timeout 60 --check-agent
```

`--check-knowledge` только читает: требует mock-эмбеддинги и готовый индекс,
находит `KB-SERVER-START` первой для `techflow-server` и убеждается, что для
`techflow-desktop` статьи сервера не видны. `--check-agent` включает его и
дополнительно создаёт учебные записи; агент должен процитировать найденную
общую статью. Не направляй `--check-agent` на базу с рабочими данными.

## 16. Что теперь проверяет CI/CD

1. `Python checks`: PostgreSQL и Qdrant 1.19.1 как сервисы runner, ожидание
   `/readyz`, миграции, `alembic check`, Ruff и полный Pytest с
   `TEST_QDRANT_URL`. Эмбеддинги и модель — mock.
2. `Build and smoke test Docker`: сборка образа, Compose с `qdrant`, индексация,
   smoke `--check-agent`, затем `down` без `--volumes` и новый `up`. После
   пересоздания совпадают демо-данные, запуск агента и выдача поиска, а повторная
   индексация ничего не переписывает.
3. `Publish tested image to GHCR`: публикация того же проверенного образа.

## 17. Частые проблемы

**Статус `missing`, поиск даёт `503`.** Выполни
`docker compose exec -T api python -m app.knowledge.ingest`.

**Статус `incompatible`.** Коллекция создана с другой `EMBEDDING_DIMENSIONS`.
Верни прежнюю размерность или укажи новое `QDRANT_COLLECTION` и переиндексируй.

**Поиск ничего не находит.** Mock ищет по общим словам. Проверь формулировку,
`product_code` и порог `KNOWLEDGE_MIN_SCORE`. Слишком низкий порог вернёт шум.

**Агент пишет «База знаний недоступна».** Проверь статус и сервис `qdrant`.
Для API на Mac задай `QDRANT_URL=http://127.0.0.1:6335` в `.env`.

**После правки статьи выдача прежняя.** Выполни индексацию: изменённая статья
окажется в `indexed`. Статьи входят в образ, поэтому для Compose сначала
пересобери его (`docker compose up --build --detach --wait api`).

**Ошибка «Ключ … повторяется».** У двух файлов одинаковый `document_key`.

## 18. Критерий готовности и следующая граница

Этап готов, когда статьи индексируются идемпотентно, поиск находит статьи
продукта и общие и не показывает статьи других продуктов, а агент ссылается
только на найденные в своём запуске статьи. Индекс переживает пересоздание
контейнера, полный набор тестов проходит на настоящих PostgreSQL и Qdrant.

Дальше по плану — LangGraph и решения сотрудника по предложениям (одобрить
или отклонить), затем интерфейс оператора. В этой итерации нет загрузки статей
через API, смыслового поиска без ключа OpenAI, переранжирования результатов
и оценки качества поиска на размеченном наборе вопросов.

Результаты локальных проверок и статус удалённого запуска — в
[журнале разработки](DEVELOPMENT_LOG.md).
