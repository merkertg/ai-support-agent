# syntax=docker/dockerfile:1

# Обе стадии используют один и тот же Python: перенос .venv остаётся корректным.
FROM python:3.12-slim AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

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
USER app

EXPOSE 8000

# Docker периодически проверяет HTTP и точное содержимое ответа /health.
# Используется стандартная библиотека Python: curl в образе не нужен.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import json, urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); payload = json.load(response); response.close(); assert response.status == 200 and payload == {'status': 'ok', 'service': 'ai-support-agent'}"]

# Exec-форма передаёт сигналы остановки напрямую серверу Uvicorn.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
