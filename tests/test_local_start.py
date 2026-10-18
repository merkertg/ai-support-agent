"""Переносимый запуск API и UI: настоящие процессы на текущей ОС без БД и сети."""

import pytest

from scripts import check_local_start as start


def test_child_environment_drops_database_and_secrets(monkeypatch) -> None:
    """Процессы проверки не видят адрес БД, ключи и Qdrant рабочего стенда."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/prod")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-check")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    env = start.child_environment("http://127.0.0.1:1")
    assert "DATABASE_URL" not in env
    assert not any(key.startswith(("OPENAI_", "QDRANT_")) for key in env)
    assert env["LLM_PROVIDER"] == env["EMBEDDING_PROVIDER"] == "mock"
    assert env["RUN_RECOVERY_ENABLED"] == "false"
    assert env["API_URL"] == "http://127.0.0.1:1"


def test_api_and_ui_start_on_this_platform(capsys) -> None:
    """Uvicorn и Streamlit поднимаются, отвечают и останавливаются на этой ОС."""
    assert start.main(["--timeout", "120"]) == 0
    output = capsys.readouterr().out
    assert "OK API: /health 200" in output
    assert "OK UI: страница оператора отрисована" in output
    assert "Локальный запуск API и UI: OK" in output


def test_failed_check_is_reported_and_processes_stop(monkeypatch, capsys) -> None:
    """Ошибка шага видна в stderr; код возврата 1, процессы не остаются."""

    def broken_api(base: str) -> list[str]:
        """Сымитировать неверный ответ /health."""
        raise start.StartError("/health вернул 500")

    monkeypatch.setattr(start, "check_api", broken_api)
    assert start.main(["--timeout", "120"]) == 1
    assert "FAIL: /health вернул 500" in capsys.readouterr().err


def test_dead_process_is_detected_before_deadline() -> None:
    """Сразу завершившийся процесс не ждёт весь тайм-аут."""

    class Exited:
        """Процесс, уже вышедший с ошибкой."""

        returncode = 3

        def poll(self) -> int:
            """Сообщить код завершения."""
            return self.returncode

    with pytest.raises(start.StartError, match="кодом 3"):
        start.wait_for("http://127.0.0.1:9/health", Exited(), deadline=1e18)
