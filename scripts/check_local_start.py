"""Проверить на этом компьютере запуск API и интерфейса оператора без Docker.

Скрипт поднимает Uvicorn и Streamlit отдельными процессами на свободных портах
127.0.0.1, проверяет /health, ответ /ready без базы данных, заголовок
X-Request-ID, отказ 401 для /api/* без токена, сервер Streamlit и отрисовку
страницы оператора против запущенного API. Затем оба процесса останавливаются.
База данных, Qdrant, ключи и сеть не нужны: так проверяется переносимость
на Linux, macOS и Windows.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Токен правильного формата, которого нет ни в одной БД: страница с ним
# обращается к API, а API без базы данных отвечает понятной ошибкой 503.
SAMPLE_TOKEN = "asa_" + "0" * 43


def use_utf8_output() -> None:
    """Печатать UTF-8 и при перенаправлении вывода в Windows, где по умолчанию cp1252.

    Консоль Windows и так получает Unicode; меняется только поток в файл или pipe.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


class StartError(Exception):
    """Сообщить, какой шаг запуска не прошёл."""


def free_port() -> int:
    """Получить свободный локальный порт у операционной системы."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def child_environment(api_url: str) -> dict[str, str]:
    """Окружение без базы и ключей: результат не зависит от настроек компьютера."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATABASE_URL", "OPENAI_", "QDRANT_", "COVERAGE_RCFILE"))
    }
    env.update(
        LLM_PROVIDER="mock",
        EMBEDDING_PROVIDER="mock",
        RUN_RECOVERY_ENABLED="false",
        LANGSMITH_TRACING="false",
        API_URL=api_url,
        PYTHONUTF8="1",
    )
    # Процессы стартуют во временной папке, чтобы не прочитать .env рабочего
    # стенда; код проекта доступен через PYTHONPATH.
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return env


def fetch(url: str, timeout: float) -> tuple[int, Message, bytes]:
    """Выполнить GET и вернуть код, заголовки и тело, в том числе для 4xx/5xx.

    Заголовки остаются объектом Message: поиск по имени в нём без учёта регистра.
    """
    request = Request(url, headers={"User-Agent": "check-local-start"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        return error.code, error.headers, error.read()


def wait_for(url: str, process: subprocess.Popen, deadline: float) -> None:
    """Ждать первого HTTP-ответа сервера, пока процесс жив и не вышел срок."""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise StartError(f"процесс завершился с кодом {process.returncode}")
        try:
            fetch(url, timeout=2)
            return
        except (URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(0.3)
    raise StartError(f"нет ответа {url} за отведённое время")


@contextmanager
def running(command: list[str], env: dict[str, str], workdir: Path):
    """Запустить процесс и гарантированно остановить его при любом исходе."""
    process = subprocess.Popen(
        command,
        cwd=workdir,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


def check_api(base: str) -> list[str]:
    """Проверить /health, безопасный 503 /ready без БД и id запроса."""
    status, headers, body = fetch(f"{base}/health", timeout=5)
    if status != 200 or json.loads(body) != {
        "status": "ok",
        "service": "ai-support-agent",
    }:
        raise StartError(f"/health вернул {status}")
    if not headers.get("X-Request-ID"):
        raise StartError("в ответе нет заголовка X-Request-ID")
    status, _, body = fetch(f"{base}/ready", timeout=5)
    if status != 503 or "detail" not in json.loads(body):
        raise StartError(f"/ready без базы должен вернуть JSON 503, получен {status}")
    # Отказ без токена не требует базы данных: заголовок проверяется раньше SQL.
    status, headers, body = fetch(f"{base}/api/customers", timeout=5)
    if (
        status != 401
        or headers.get("WWW-Authenticate") != "Bearer"
        or "detail" not in json.loads(body)
    ):
        raise StartError(
            f"/api/customers без токена должен вернуть 401, получен {status}"
        )
    return [
        "OK API: /health 200, X-Request-ID есть",
        "OK API: /ready без БД — 503 JSON",
        "OK API: /api/* без токена — 401 Bearer",
    ]


def check_ui_server(base: str) -> str:
    """Проверить сервер Streamlit через его health-маршрут."""
    status, _, body = fetch(f"{base}/_stcore/health", timeout=5)
    if status != 200 or body.strip() != b"ok":
        raise StartError(f"Streamlit /_stcore/health вернул {status}")
    return "OK UI: сервер Streamlit отвечает"


def check_ui_page(api_url: str) -> str:
    """Отрисовать настоящую страницу оператора против запущенного API.

    Без токена страница просит его ввести и не обращается к API. С токеном
    без БД API отвечает 503: страница обязана показать понятную ошибку,
    а не упасть с исключением Python.
    """
    from streamlit.testing.v1 import AppTest

    os.environ["API_URL"] = api_url
    screen = AppTest.from_file(
        str(PROJECT_ROOT / "operator_ui" / "main.py"), default_timeout=30
    ).run()
    if screen.exception:
        raise StartError("страница оператора завершилась исключением")
    if screen.error or not screen.info:
        raise StartError("страница без токена не попросила ввести токен сотрудника")
    screen.sidebar.text_input(key="api_token").input(SAMPLE_TOKEN).run()
    if screen.exception:
        raise StartError("страница оператора завершилась исключением")
    if not screen.error:
        raise StartError("страница не показала ошибку недоступной базы данных")
    return "OK UI: страница оператора отрисована, ошибка API показана понятно"


def main(argv: list[str] | None = None) -> int:
    """Запустить оба сервера, выполнить проверки и вернуть 0 только при успехе."""
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=90.0)
    arguments = parser.parse_args(argv)
    api_port, ui_port = free_port(), free_port()
    api_url = f"http://127.0.0.1:{api_port}"
    ui_url = f"http://127.0.0.1:{ui_port}"
    env = child_environment(api_url)
    deadline = time.monotonic() + arguments.timeout
    api = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(api_port),
        "--no-access-log",
    ]
    ui = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "operator_ui" / "main.py"),
        "--server.headless",
        "true",
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(ui_port),
        "--browser.gatherUsageStats",
        "false",
    ]
    lines = [f"Платформа: {sys.platform}, Python {sys.version.split()[0]}"]
    try:
        with (
            # Windows может держать папку остановленного процесса ещё мгновение.
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir,
            running(api, env, Path(workdir)) as api_process,
        ):
            wait_for(f"{api_url}/health", api_process, deadline)
            lines += check_api(api_url)
            with running(ui, env, Path(workdir)) as ui_process:
                wait_for(f"{ui_url}/_stcore/health", ui_process, deadline)
                lines.append(check_ui_server(ui_url))
                lines.append(check_ui_page(api_url))
    except (StartError, OSError, ValueError) as error:
        print("\n".join(lines))
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    print("Локальный запуск API и UI: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
