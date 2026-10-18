# syntax=docker/dockerfile:1

# Обе стадии используют один и тот же Python: перенос .venv остаётся корректным.
FROM python:3.12-slim AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Интерфейс оператора собирается отдельно: ему нужны только Streamlit и httpx.
# Цель выбирается явно (--target ui); без --target по-прежнему собирается API.
FROM python-base AS ui-builder
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv

WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

# --only-group ui не ставит FastAPI, драйвер PostgreSQL, LangGraph и SDK OpenAI.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --only-group ui

FROM python-base AS ui
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_CLIENT_TOOLBAR_MODE=minimal

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home app
COPY --from=ui-builder --chown=app:app /app/.venv /app/.venv
# Только код интерфейса: ни app/, ни миграций, ни статей в этом образе нет.
COPY --chown=app:app operator_ui ./operator_ui
USER app

EXPOSE 8501

# /_stcore/health отвечает ok, когда сервер Streamlit принимает соединения.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3); assert response.status == 200 and response.read() == b'ok'"]

CMD ["streamlit", "run", "operator_ui/main.py", "--server.address", "0.0.0.0", "--server.port", "8501"]

# В builder создаём окружение только с зависимостями работающего приложения.
FROM python-base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv

WORKDIR /app
# Используем Python базового образа и копируем файлы из кеша в .venv.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

# Изменение кода приложения не требует заново устанавливать библиотеки.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# В финальный образ не попадают uv, кеш сборки, Pytest и Ruff.
FROM python-base AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

# Постоянный UID упрощает запуск контейнера без прав root.
RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app
# Alembic нужен отдельному migration-сервису и проверке актуальности схемы.
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./alembic.ini
# Статьи базы знаний индексирует команда python -m app.knowledge.ingest.
COPY --chown=app:app knowledge ./knowledge
USER app

EXPOSE 8000

# Docker проверяет готовность API, соединение с PostgreSQL и версию схемы.
# Используется стандартная библиотека Python: curl в образе не нужен.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import json, urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3); payload = json.load(response); response.close(); assert response.status == 200 and payload == {'status': 'ok', 'database': 'ok'}"]

# Exec-форма передаёт сигналы остановки напрямую серверу Uvicorn.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
