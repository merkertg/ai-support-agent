# Резервное копирование и восстановление

[← README](../../README.md) · [All documentation](../../README.md#documentation) · [Quickstart](quickstart.md) · [Windows](windows.md)

`scripts/backup.py` сохраняет и восстанавливает все данные Compose-стенда:

- **PostgreSQL** — все таблицы схемы `public`: клиенты, продукты, лицензии,
  обращения и сообщения, сотрудники и SHA-256 их токенов, запуски агента,
  предложения и решения, реестр базы знаний, состояние графов LangGraph
  (ожидающие решения запуски продолжаются после восстановления) и `alembic_version`;
- **Qdrant** — коллекцию `support_knowledge` с векторами и payload.

Не сохраняются `.env`, Docker-образы, журналы и сами статьи `knowledge/`:
они лежат в Git или создаются заново. Скрипт использует только стандартную
библиотеку Python 3.12, поэтому работает и через `uv run`, и системным `python3`
3.12 (так его запускает CI). Docker нужен для `create` и `restore`, но не для `verify`.

| Подкоманда | Что делает |
| --- | --- |
| `create` | `pg_dump` внутри контейнера `db`, snapshot коллекции через API Qdrant, манифест. Snapshot сразу удаляется с сервера Qdrant. |
| `verify` | Без Docker: структура манифеста, размер и SHA256 каждого файла, подпись дампа `PGDMP`, tar-архив snapshot. |
| `restore` | Проверки цели, `pg_restore` во временную БД, сверка строк, загрузка snapshot, затем замена рабочей БД. |

## Что лежит в копии

Каждый запуск `create` создаёт новую папку `backups/<проект>-<время UTC>/`,
например `backups/ai-support-agent-20261019T053000Z/`:

| Файл | Содержимое |
| --- | --- |
| `postgres.dump` | Дамп `pg_dump --format=custom` (сжатый, читается только `pg_restore`). |
| `qdrant.snapshot` | Snapshot коллекции Qdrant (tar-архив сегментов). Нет, если коллекция ещё не создана индексацией. |
| `manifest.json` | Формат `ai-support-backup/1`, время создания, ревизия Alembic, версии PostgreSQL и Qdrant, число строк каждой таблицы, число точек и SHA256/размер обоих файлов. |

Файлы сначала пишутся в скрытую папку `.<имя>.partial` и переименовываются
только после успеха: прерванный запуск не оставит похожую на копию папку.
`create` считает строки таблиц и точки Qdrant до и после копирования. Если
во время копии кто-то менял данные, копия отклоняется и удаляется.

На учебном стенде дамп занимает около 50 КБ. Размер snapshot зависит
от числа сегментов Qdrant больше, чем от числа точек: в проверках для тех же
27 точек получилось 0,7 МБ и 37 МБ.

## Копия секретна: где её хранить

Хотя токены сотрудников записаны только как SHA-256 и сам токен из копии
не восстановить, в копии лежат персональные данные клиентов, ключи лицензий,
переписка, решения сотрудников и хеши, по которым API принимает токены.
Обращайся с копией как с паролем от рабочей БД:

- **Никогда не коммить копии.** Путь `backups/` указан в `.gitignore`
  и `.dockerignore`: копии не попадут ни в Git, ни в контекст сборки образа.
  Проверить: `git check-ignore backups/` должен напечатать `backups/`.
- **Храни вне репозитория** — на зашифрованном диске (FileVault, BitLocker,
  LUKS) или в хранилище с шифрованием и ограниченным доступом. Папка `backups/`
  внутри проекта подходит только для временной копии перед обновлением.
  Если папка проекта синхронизируется облаком (например, «Рабочий стол»
  в iCloud или OneDrive), сразу укажи `--output-dir` за её пределами.
- На Linux и macOS скрипт создаёт папку копии с правами `0700`, а файлы —
  `0600`: читать их может только владелец. На Windows права наследуются
  от родительской папки; ограничь их командой `icacls` из раздела PowerShell.
- Не отправляй копии в чат, тикет или артефакт CI. CI делает копию во временном
  каталоге runner и не загружает её.
- Старые копии удаляй вместе с папкой; храни столько, сколько требует
  политика хранения данных.

Восстановленная старая копия снова делает действующими токены, которые были
отозваны **после** её создания. После восстановления проверь список токенов
сотрудников (`GET /api/users/{id}/tokens`) и повторно отзови лишние.

Так же копия возвращает данные клиентов, обезличенных **после** её создания.
После восстановления повтори для них `python -m app.privacy erase`
([персональные данные](privacy.md#ограничения)); старые копии не храни дольше,
чем этого требует срок хранения.

## Создать копию

Копию можно делать на работающем стенде: `pg_dump` читает согласованный
снимок PostgreSQL. Чтобы пара PostgreSQL + Qdrant гарантированно совпадала
и `create` не отклонил копию из-за одновременной записи, на время копии
останови API и интерфейс:

```bash
docker compose stop api ui
uv run --locked python scripts/backup.py create --output-dir "$HOME/ai-support-backups"
docker compose up --detach --wait ui
```

Без `--output-dir` копия попадает в `backups/` проекта. Скрипт печатает отчёт
JSON без данных: путь, время, ревизию Alembic, число таблиц, строк и точек,
размеры файлов. Ошибка (например, остановленный `db` или Qdrant) печатается
одной строкой `Ошибка: …` с кодом выхода `1`.

## Проверить копию

```bash
backup="$(ls -d "$HOME"/ai-support-backups/ai-support-agent-* | tail -n 1)"
uv run --locked python scripts/backup.py verify "$backup"
```

`verify` не обращается к Docker: его можно выполнить там, где копия хранится.
Повреждённый или изменённый файл даёт код `1` и список проблем.
Те же суммы можно сверить вручную: `shasum -a 256` на macOS
(`sha256sum` на Linux) должен совпасть с полем `files` манифеста.

```bash
shasum -a 256 "$backup/postgres.dump" "$backup/qdrant.snapshot"
python3 -m json.tool "$backup/manifest.json"
```

## Восстановить в чистые тома

Например, на новом компьютере или после потери томов. Запусти только
PostgreSQL и Qdrant, восстанови копию, затем запусти остальное:

```bash
docker compose up --detach --wait db qdrant
uv run --locked python scripts/backup.py restore "$backup"
docker compose up --detach --wait ui
```

До любых изменений `restore` проверяет:

1. копию — так же, как `verify`;
2. **версию Alembic**: ревизия из манифеста должна быть в цепочке
   `migrations/versions` текущего кода. Копию, созданную более новой версией
   проекта, старый код не восстанавливает: сначала переключись на код той
   версии или новее;
3. что сервисы `api` и `migrate` проекта остановлены — иначе API мог бы
   писать во время замены;
4. что версия PostgreSQL цели не старше версии, создавшей дамп;
5. что цель **пуста**: в БД нет таблиц, коллекции Qdrant нет. Иначе `restore`
   перечисляет, что было бы заменено (таблицы, строки, ревизия, точки),
   и завершается без изменений.

Затем дамп применяется к временной БД `<имя>_restoring` одной транзакцией,
и её строки сверяются с манифестом. После этого загружается snapshot
(Qdrant сам сверяет его SHA256), число точек сверяется с манифестом,
и только в конце рабочая БД заменяется временной. Ошибка на любом шаге до
замены оставляет рабочую БД без изменений и удаляет временную.

Если ревизия копии старше текущего кода, отчёт содержит
`"migration_needed": true`: следующий `docker compose up` запустит `migrate`,
и схема обновится до head автоматически. Если статьи `knowledge/` менялись
после создания копии, выполни индексацию заново:
`docker compose exec -T api python -m app.knowledge.ingest`.

## Заменить данные работающего стенда

`--force` разрешает заменить непустые PostgreSQL и Qdrant. Перед этим сделай
свежую копию текущего состояния — она позволит вернуться назад. Восстанавливаемую
копию укажи явно по имени папки: свежая копия новее, и `ls | tail` выбрал бы её.

```bash
backup="$HOME/ai-support-backups/ai-support-agent-20261019T053000Z"
uv run --locked python scripts/backup.py verify "$backup"
docker compose stop api ui
uv run --locked python scripts/backup.py create --output-dir "$HOME/ai-support-backups"
uv run --locked python scripts/backup.py restore "$backup" --force
docker compose up --detach --wait ui
```

## Откатить неудачное обновление

`alembic downgrade` есть у каждой миграции, но откат схемы удаляет данные
новых таблиц (например, `0005_api_tokens` при откате удаляет все токены).
Надёжный откат обновления — копия, сделанная **перед** обновлением:

1. Перед `git pull` и `docker compose up --build` создай копию, как выше.
2. Если новая версия работает неправильно, переключись на прежний код
   (`git checkout <прежний commit или тег>`) и пересобери образы:
   `docker compose build api ui`.
3. Останови `api ui` и восстанови копию с `--force`. Её ревизия Alembic
   есть в коде прежней версии, поэтому проверка версии пройдёт.
4. `docker compose up --detach --wait ui` запустит прежнюю версию на данных
   до обновления.

## Другие стенды и контейнеры

| Параметр | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `--project-name` | `COMPOSE_PROJECT_NAME` или `ai-support-agent` | Compose-проект, например изолированный `ai-support-ci`. |
| `--db-service`, `--qdrant-service` | `db`, `qdrant` | Сервисы проекта; адрес Qdrant берётся из `docker compose port`. |
| `--db-container` | — | Контейнер PostgreSQL вне Compose (`docker exec`); тогда нужен `--qdrant-url`, а остановку API проверяет человек. |
| `--database` | `POSTGRES_DB` контейнера | Имя БД внутри сервера. |
| `--qdrant-url`, `--collection` | опубликованный порт, `support_knowledge` | Явный адрес Qdrant и коллекция. |
| `--output-dir` (`create`) | `backups/` проекта | Куда положить папку копии. |
| `--force` (`restore`) | выключен | Заменить непустую цель. |

Пароль PostgreSQL скрипту не нужен: `pg_dump`, `pg_restore` и `psql`
запускаются в контейнере БД и подключаются через его локальный сокет
от имени `POSTGRES_USER`. SQL передаётся через stdin, а не аргументами процесса.

## Как это проверяется

- `tests/test_backup.py` — unit-тесты без Docker: подменённый контейнер
  PostgreSQL и локальный HTTP-сервер с маршрутами snapshot Qdrant, включая отказы,
  повреждённые файлы, неверный манифест, одновременную запись и ошибки на каждом шаге.
- `tests/integration/test_backup_restore.py` — настоящий круг «копия → очистка →
  восстановление» на тестовых сервисах `db-test` и `qdrant-test` (порты 5434
  и 6336): совпадают число строк и MD5 содержимого каждой таблицы, SHA256 файлов,
  все точки Qdrant с векторами; сохранённый токен снова принимается.
  Контейнер тестовой БД задаёт `TEST_POSTGRES_CONTAINER` или он находится
  по опубликованному порту `TEST_DATABASE_URL` (см. [тестирование](testing.md)).
- Задание `container` в CI делает копию проекта `ai-support-ci`, удаляет его тома,
  восстанавливает копию в чистые тома и повторяет smoke тем же токеном
  (см. [CI and delivery](ci-and-delivery.md)).

## PowerShell

Подготовь сессию по [инструкции для Windows](windows.md). Копия в папку
профиля, затем права только для текущего пользователя:

```powershell
$backups = "$env:USERPROFILE\ai-support-backups"
docker compose stop api ui
uv run --locked python scripts/backup.py create --output-dir $backups
docker compose up --detach --wait ui
icacls $backups /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F"
```

Проверить последнюю копию, в том числе вручную по SHA256 из манифеста:

```powershell
$backup = (Get-ChildItem $backups -Directory | Sort-Object Name | Select-Object -Last 1).FullName
uv run --locked python scripts/backup.py verify $backup
$manifest = Get-Content "$backup\manifest.json" -Raw | ConvertFrom-Json
(Get-FileHash "$backup\postgres.dump" -Algorithm SHA256).Hash.ToLower() -eq $manifest.files.'postgres.dump'.sha256
```

Восстановить в чистые тома:

```powershell
docker compose up --detach --wait db qdrant
uv run --locked python scripts/backup.py restore $backup
docker compose up --detach --wait ui
```

Заменить данные работающего стенда выбранной по имени копией, предварительно
сохранив текущее состояние:

```powershell
$backup = "$backups\ai-support-agent-20261019T053000Z"
docker compose stop api ui
uv run --locked python scripts/backup.py create --output-dir $backups
uv run --locked python scripts/backup.py restore $backup --force
docker compose up --detach --wait ui
```
