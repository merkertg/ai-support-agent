"""Проверить настоящее выполнение Streamlit через официальный AppTest без сети."""

import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from operator_ui import app
from operator_ui.client import APIError

ENTRYPOINT = Path(__file__).resolve().parents[1] / "operator_ui" / "main.py"
STAMP = "2026-10-18T12:00:00Z"


class FakeAPI:
    """Хранить учебные ответы HTTP-контракта и учитывать изменяющие действия UI."""

    def __init__(self):
        """Создать обращение и pending-предложение без подключения к API или модели."""
        self.posts = []
        self.reads = []
        self.provider = "mock"
        self.embedding_provider = "mock"
        self.run_error = None
        self.records = {
            "tickets": {
                1: {
                    "id": 1,
                    "customer_id": 1,
                    "product_id": 1,
                    "subject": "Не запускается сервер",
                    "description": "<script>customer</script>",
                    "priority": "high",
                    "status": "open",
                    "created_at": STAMP,
                    "updated_at": STAMP,
                }
            },
            "customers": {
                1: {
                    "id": 1,
                    "name": "Учебный клиент",
                    "email": "demo@example.org",
                    "company": None,
                    "created_at": STAMP,
                }
            },
            "products": {
                1: {
                    "id": 1,
                    "name": "Сервер",
                    "code": "server",
                    "description": None,
                    "created_at": STAMP,
                }
            },
            "licenses": {},
            "users": {},
            "agent-runs": {},
            "suggestions": {
                1: {
                    "id": 1,
                    "agent_run_id": 1,
                    "ticket_id": 1,
                    "suggestion_type": "reply",
                    "content": "Проверьте службу сервера.",
                    "status": "pending",
                    "reviewed_by": None,
                    "reviewed_at": None,
                    "final_content": None,
                    "review_comment": None,
                    "delivered_message_id": None,
                    "created_at": STAMP,
                }
            },
        }
        self.message_rows = []

    def __enter__(self):
        """Разрешить интерфейсу использовать тот же контракт context manager."""
        return self

    def __exit__(self, *args):
        """Не закрывать тестовые данные между обычными rerun страницы."""

    def status(self, area):
        """Вернуть только безопасную конфигурацию провайдера, без API-ключей."""
        self.reads.append(("status", area))
        if area == "llm":
            return {
                "provider": self.provider,
                "model": "mock-agent-v1",
                "configured": True,
            }
        return {
            "embedding_provider": self.embedding_provider,
            "configured": True,
            "index": "ready",
            "documents": 3,
        }

    def dashboard(self):
        """Вернуть фактические тестовые агрегаты и отсутствие средней статистики."""
        self.reads.append(("dashboard",))
        return {
            "tickets_total": 1,
            "open_tickets": 1,
            "high_priority_tickets": 1,
            "analyzed_tickets": 0,
            "completed_runs": 0,
            "failed_runs": 0,
            "pending_suggestions": 1,
            "approved_suggestions": 0,
            "rejected_suggestions": 0,
            "average_analysis_seconds": None,
            "approval_rate": None,
        }

    def list_records(self, resource, *, offset=0, **filters):
        """Выдать только страницу и применить совпадающие серверные фильтры."""
        self.reads.append(("list", resource, offset, filters))
        rows = list(self.records[resource].values())
        for key, value in filters.items():
            if value is not None:
                rows = [row for row in rows if row.get(key) == value]
        return deepcopy(rows[offset : offset + 20])

    def get_record(self, resource, record_id):
        """Прочитать карточку и сымитировать безопасный HTTP 404."""
        self.reads.append(("get", resource, record_id))
        try:
            return deepcopy(self.records[resource][record_id])
        except KeyError as error:
            raise APIError("Запись не найдена.", 404) from error

    def create_record(self, resource, payload):
        """Сохранить запись ровно один раз при явной отправке формы."""
        self.posts.append(("create", resource, deepcopy(payload)))
        record_id = max(self.records[resource], default=0) + 1
        record = {"id": record_id, **payload}
        if resource == "tickets":
            record.update(status="open", created_at=STAMP, updated_at=STAMP)
        self.records[resource][record_id] = record
        return deepcopy(record)

    def analyze(self, ticket_id, license_key):
        """Создать сохранённый итог анализа без реального LLM или платного вызова."""
        self.posts.append(("analyze", ticket_id, license_key))
        if self.run_error:
            raise self.run_error
        run = {
            "id": 1,
            "ticket_id": ticket_id,
            "status": "completed",
            "provider": self.provider,
            "model": "mock-agent-v1",
            "started_at": STAMP,
            "finished_at": STAMP,
            "steps": 3,
            "tool_trace": [],
            "usage": None,
            "error_message": None,
            "result": {
                "summary": "Проверен запуск сервера",
                "category": "technical_issue",
                "draft_reply": "Демонстрационный агент. Реальная модель не вызывалась.",
                "sources": ["KB-SERVER"],
            },
            "suggestion": {"id": 1, "status": "pending", "requires_human_review": True},
        }
        self.records["agent-runs"][1] = run
        return deepcopy(run)

    def messages(self, ticket_id, *, offset=0):
        """Прочитать страницу сообщений выбранного обращения."""
        self.reads.append(("messages", ticket_id, offset))
        return deepcopy(self.message_rows[offset : offset + 20])

    def update_ticket_status(self, ticket_id, status):
        """Запомнить единственное изменение статуса формы."""
        self.posts.append(("patch", ticket_id, status))
        self.records["tickets"][ticket_id]["status"] = status
        return deepcopy(self.records["tickets"][ticket_id])

    def review(self, suggestion_id, payload):
        """Применить решение к тестовой очереди и вернуть сохранённое состояние."""
        self.posts.append(("review", suggestion_id, deepcopy(payload)))
        suggestion = self.records["suggestions"][suggestion_id]
        suggestion["status"] = (
            "approved" if payload["decision"] == "approve" else "rejected"
        )
        return {"suggestion": deepcopy(suggestion)}

    def search(self, query, product_code, limit):
        """Запомнить явный поиск и возвратить безопасно отображаемый фрагмент."""
        self.posts.append(("search", query, product_code, limit))
        return {
            "query": query,
            "results": [
                {
                    "document_key": "KB-SERVER",
                    "title": "Запуск",
                    "section": "Проверка службы",
                    "text": "<img src=x onerror=alert(1)> текст статьи",
                    "score": 0.9,
                    "source_path": "knowledge/server.md",
                    "scope": "server",
                }
            ],
        }


@pytest.fixture
def desktop(monkeypatch):
    """Запустить реальный Streamlit script runner с детерминированным API-клиентом."""
    api = FakeAPI()
    monkeypatch.setattr(app, "make_client", lambda: api)
    screen = AppTest.from_file(str(ENTRYPOINT), default_timeout=15).run()
    assert not screen.exception
    return screen, api


def find(elements, label):
    """Найти виджет по видимому русскому названию, не привязываясь к позиции."""
    return next(element for element in elements if element.label == label)


def navigate(screen, area):
    """Переключить раздел так же, как это делает оператор в боковой панели."""
    screen.sidebar.radio[0].set_value(area).run()
    assert not screen.exception


def open_ticket(screen):
    """Открыть карточку существующего обращения без изменяющего запроса."""
    navigate(screen, "Обращения")
    find(screen.text_input, "Открыть обращение по ID").set_value("1").run()
    assert not screen.exception


def open_suggestion(screen):
    """Открыть одно ожидающее предложение для проверки решения сотрудника."""
    navigate(screen, "Очередь проверки")
    find(screen.text_input, "Открыть предложение по ID").set_value("1").run()
    assert not screen.exception


def test_dashboard_and_reruns_only_read(desktop):
    """Первое открытие и обновление показывают реальные агрегаты без POST."""
    screen, api = desktop
    assert {metric.label: metric.value for metric in screen.metric}[
        "Всего обращений"
    ] == "1"
    assert [metric.value for metric in screen.metric].count("Нет данных") == 2
    screen.run()
    find(screen.button, "Обновить данные").click().run()
    assert not screen.exception
    assert api.posts == []


def test_navigation_and_customer_text_are_safe_reading(desktop):
    """Разделы читаются без изменений; HTML клиента выводится обычным текстом."""
    screen, api = desktop
    for area in ("Обращения", "Очередь проверки", "База знаний", "Справочники"):
        navigate(screen, area)
    open_ticket(screen)
    assert any(element.value == "<script>customer</script>" for element in screen.text)
    assert api.posts == []


def test_ticket_create_exactly_once(desktop):
    """Создание сохраняет данные формы, а последующий rerun не создаёт дубль."""
    screen, api = desktop
    navigate(screen, "Обращения")
    find(screen.text_input, "Тема").set_value("Новая ошибка")
    find(screen.text_area, "Описание проблемы").set_value("Нужна помощь")
    find(screen.button, "Создать обращение").click().run()
    assert not screen.exception
    assert api.posts == [
        (
            "create",
            "tickets",
            {
                "customer_id": 1,
                "product_id": 1,
                "subject": "Новая ошибка",
                "description": "Нужна помощь",
                "priority": "medium",
            },
        )
    ]
    screen.run()
    assert len(api.posts) == 1
    assert screen.session_state["selected_ticket"] == "2"


def test_mock_analysis_once_and_persistent_history(desktop):
    """Анализ создаётся явной кнопкой, затем карточка читает сохранённый запуск."""
    screen, api = desktop
    open_ticket(screen)
    find(screen.button, "Запустить анализ").click().run()
    assert not screen.exception
    assert api.posts == [("analyze", 1, None)]
    assert any("KB-SERVER" in text.value for text in screen.text)
    screen.run()
    assert api.posts == [("analyze", 1, None)]
    assert ("get", "agent-runs", 1) in api.reads


def test_real_model_requires_fresh_explicit_confirmation(desktop):
    """OpenAI запрещён до отдельного checkbox; сами настройки модель не вызывают."""
    screen, api = desktop
    api.provider = "openai"
    open_ticket(screen)
    assert find(screen.button, "Запустить анализ").disabled
    assert api.posts == []
    find(screen.checkbox, "Подтверждаю вызов реального OpenAI API").check().run()
    assert not find(screen.button, "Запустить анализ").disabled
    assert api.posts == []
    find(screen.button, "Запустить анализ").click().run()
    assert not screen.exception
    assert api.posts == [("analyze", 1, None)]


def test_embeddings_require_confirmation_and_search_survives_rerun(desktop):
    """Поиск с OpenAI-эмбеддингами требует согласия и не повторяется при rerun."""
    screen, api = desktop
    api.embedding_provider = "openai"
    navigate(screen, "База знаний")
    assert find(screen.button, "Найти статьи").disabled
    find(screen.checkbox, "Подтверждаю вызов реального OpenAI API").check().run()
    find(screen.text_input, "Что найти?").set_value("ошибка сервера")
    find(screen.button, "Найти статьи").click().run()
    assert not screen.exception
    assert api.posts == [("search", "ошибка сервера", None, 5)]
    assert any("<img src=x" in text.value for text in screen.text)
    screen.run()
    assert len(api.posts) == 1
    assert any("<img src=x" in text.value for text in screen.text)


@pytest.mark.parametrize("decision", ["Одобрить", "Отклонить"])
def test_review_requires_confirmation_and_correct_payload(desktop, decision):
    """Сотрудник может отредактировать ответ или отклонить с обязательной причиной."""
    screen, api = desktop
    open_suggestion(screen)
    find(screen.radio, "Решение").set_value(decision)
    find(screen.text_area, "Итоговый ответ при одобрении").set_value(
        "Проверенный ответ"
    )
    find(screen.text_area, "Комментарий (обязателен при отклонении)").set_value(
        "Нужна дополнительная проверка"
    )
    find(screen.button, "Сохранить решение").click().run()
    assert not screen.exception
    assert api.posts == []
    find(screen.checkbox, "Я проверил текст и подтверждаю решение").check()
    find(screen.button, "Сохранить решение").click().run()
    assert not screen.exception
    assert api.posts == [
        (
            "review",
            1,
            {
                "decision": "approve" if decision == "Одобрить" else "reject",
                "reviewer_id": 1,
                "final_content": "Проверенный ответ"
                if decision == "Одобрить"
                else None,
                "comment": "Нужна дополнительная проверка",
            },
        )
    ]
    screen.run()
    assert len(api.posts) == 1


def test_reject_without_reason_is_not_sent(desktop):
    """Отсутствующая причина останавливает отказ до HTTP-запроса."""
    screen, api = desktop
    open_suggestion(screen)
    find(screen.radio, "Решение").set_value("Отклонить")
    find(screen.checkbox, "Я проверил текст и подтверждаю решение").check()
    find(screen.button, "Сохранить решение").click().run()
    assert not screen.exception
    assert api.posts == []
    assert any("причину" in error.value for error in screen.error)


def test_ticket_status_changes_only_on_submit(desktop):
    """Форма статуса отправляет PATCH один раз и затем читает сохранённое состояние."""
    screen, api = desktop
    open_ticket(screen)
    find(screen.selectbox, "Новый статус").set_value("В работе")
    find(screen.button, "Изменить статус").click().run()
    assert not screen.exception
    assert api.posts == [("patch", 1, "in_progress")]
    screen.run()
    assert len(api.posts) == 1


def test_failed_analysis_shows_saved_run_without_retry(desktop):
    """Ошибка анализа показывает сохранённый run_id, но не запускает новый запрос."""
    screen, api = desktop
    api.run_error = APIError("Время анализа истекло.", 504, 9)
    open_ticket(screen)
    find(screen.button, "Запустить анализ").click().run()
    assert not screen.exception
    assert any("№9" in info.value for info in screen.info)
    screen.run()
    assert api.posts == [("analyze", 1, None)]


def test_ticket_pagination_is_bounded_and_filter_resets_offset(desktop):
    """Переключение запрашивает следующую страницу и сбрасывает её при фильтре."""
    screen, api = desktop
    original = api.records["tickets"][1]
    api.records["tickets"] = {
        record_id: {**original, "id": record_id} for record_id in range(1, 24)
    }
    navigate(screen, "Обращения")
    screen.button(key="tickets_next").click().run()
    assert not screen.exception
    assert any(read[:3] == ("list", "tickets", 20) for read in api.reads)
    find(screen.selectbox, "Статус обращения").set_value("Открыто").run()
    ticket_reads = [read for read in api.reads if read[:2] == ("list", "tickets")]
    assert ticket_reads[-1][2] == 0
    assert api.posts == []


def test_invalid_id_does_not_crash_or_send_mutation(desktop):
    """Неверный ID получает понятное сообщение без traceback и изменяющего запроса."""
    screen, api = desktop
    navigate(screen, "Обращения")
    find(screen.text_input, "Открыть обращение по ID").set_value("../../private").run()
    assert not screen.exception
    assert screen.error
    assert api.posts == []


def test_analysis_checks_embedding_cost_even_with_mock_model(desktop):
    """Mock-модель не отменяет отдельного согласия на реальные эмбеддинги агента."""
    screen, api = desktop
    api.embedding_provider = "openai"
    open_ticket(screen)
    assert find(screen.button, "Запустить анализ").disabled
    assert api.posts == []
    find(screen.checkbox, "Подтверждаю вызов реального OpenAI API").check().run()
    assert not find(screen.button, "Запустить анализ").disabled
    assert api.posts == []


@pytest.mark.parametrize(
    "catalog,fields,resource",
    [
        (
            "Клиенты",
            {
                "Название / имя": "Клиент",
                "Email": "new@example.org",
                "Компания (необязательно)": "Компания",
            },
            "customers",
        ),
        (
            "Продукты",
            {"Название / имя": "Модуль", "Код продукта": "module"},
            "products",
        ),
        ("Лицензии", {"Ключ новой лицензии": "TEST-KEY"}, "licenses"),
        (
            "Сотрудники",
            {"Название / имя": "Сотрудник", "Email": "operator@example.org"},
            "users",
        ),
    ],
)
def test_catalog_forms_create_once(desktop, catalog, fields, resource):
    """Каждая справочная форма создаёт разрешённый ресурс только при отправке."""
    screen, api = desktop
    navigate(screen, "Справочники")
    find(screen.selectbox, "Справочник").set_value(catalog).run()
    for label, value in fields.items():
        find(screen.text_input, label).set_value(value)
    find(screen.button, "Создать запись").click().run()
    assert not screen.exception
    assert len(api.posts) == 1
    assert api.posts[0][:2] == ("create", resource)
    if resource == "licenses":
        assert api.posts[0][2] == {
            "customer_id": 1,
            "product_id": 1,
            "license_key": "TEST-KEY",
        }
    screen.run()
    assert len(api.posts) == 1


def test_empty_ticket_form_does_not_create_record(desktop):
    """Пустая тема или описание отклоняются до HTTP-запроса."""
    screen, api = desktop
    navigate(screen, "Обращения")
    find(screen.button, "Создать обращение").click().run()
    assert not screen.exception
    assert any("Заполните" in error.value for error in screen.error)
    assert api.posts == []


def test_server_failure_remains_an_operator_message(monkeypatch):
    """Недоступность сервера не раскрывает traceback и не ломает Streamlit script."""

    def unavailable_client():
        """Сымитировать ожидаемую ошибку конфигурации до открытия соединения."""
        raise APIError("API временно недоступен.")

    monkeypatch.setattr(app, "make_client", unavailable_client)
    screen = AppTest.from_file(str(ENTRYPOINT), default_timeout=15).run()
    assert not screen.exception
    assert screen.error[0].value == "API временно недоступен."


def test_missing_provider_status_disables_analysis(desktop):
    """Неизвестный embedding provider не допускает потенциально платный вызов."""
    screen, api = desktop
    api.embedding_provider = None
    open_ticket(screen)
    assert find(screen.button, "Запустить анализ").disabled
    assert api.posts == []


def test_entrypoint_imports_package_outside_project_directory(tmp_path):
    """streamlit run кладёт в sys.path только operator_ui/; корень добавляет main.py."""
    code = (
        "import runpy, sys; "
        f"runpy.run_path({str(ENTRYPOINT)!r}, run_name='import_check'); "
        "print(sys.modules['operator_ui.app'].__file__)"
    )
    # -I не добавляет текущую папку и PYTHONPATH: как запуск из другого каталога.
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("operator_ui/app.py")
