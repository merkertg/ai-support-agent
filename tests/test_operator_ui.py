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
# Вымышленный токен правильного формата: FakeAPI его не проверяет.
TOKEN = "asa_" + "T" * 43


class FakeAPI:
    """Хранить учебные ответы HTTP-контракта и учитывать изменяющие действия UI."""

    def __init__(self):
        """Создать обращение и pending-предложение без подключения к API или модели."""
        self.posts = []
        self.reads = []
        self.role = "admin"
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

    def me(self):
        """Вернуть сотрудника токена; роль меняется тестом."""
        self.reads.append(("me",))
        return {"id": 7, "name": "Учебный сотрудник", "role": self.role}

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

    def erase_customer(self, customer_id, confirm_email):
        """Обезличить учебного клиента и вернуть счётчики, как API."""
        self.posts.append(("erase", customer_id, confirm_email))
        customer = self.records["customers"][customer_id]
        customer.update(
            name="Клиент удалён",
            email=f"erased-{customer_id}@erased.invalid",
            company=None,
            erased_at=STAMP,
        )
        return {
            "customer_id": customer_id,
            "erased_at": STAMP,
            "tickets": 1,
            "messages": 0,
            "agent_runs": 0,
            "suggestions": 0,
            "workflow_threads": 0,
            "licenses_kept": 0,
        }

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


def sign_in(screen, token=TOKEN):
    """Ввести токен сотрудника в боковой панели, как это делает человек."""
    screen.sidebar.text_input(key="api_token").input(token).run()
    assert not screen.exception
    return screen


@pytest.fixture
def desktop(monkeypatch):
    """Запустить реальный Streamlit script runner с детерминированным API-клиентом."""
    api = FakeAPI()
    tokens = []

    def client(token):
        """Запомнить токен, с которым страница создаёт HTTP-клиент."""
        tokens.append(token)
        return api

    monkeypatch.setattr(app, "make_client", client)
    screen = sign_in(AppTest.from_file(str(ENTRYPOINT), default_timeout=15).run())
    assert set(tokens) == {TOKEN}
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


def choose_ticket_parties(screen, customer_id="1", product_id="1"):
    """Выбрать существующего клиента и продукт перед вводом остальных полей."""
    screen.selectbox(key="create_ticket_customer_choice").set_value(customer_id).run()
    screen.selectbox(key="create_ticket_product_choice").set_value(product_id).run()
    assert not screen.exception


def fill_ticket_form(screen):
    """Заполнить обязательный текст без отправки изменяющего запроса."""
    find(screen.text_input, "Тема").set_value("Новая ошибка")
    find(screen.text_area, "Описание проблемы").set_value("Нужна помощь")


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
    choose_ticket_parties(screen)
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


def test_ticket_creation_requires_explicit_customer_and_product(desktop):
    """Обе записи выбираются явно; открытие страницы не подставляет первого клиента."""
    screen, api = desktop
    navigate(screen, "Обращения")
    assert screen.selectbox(key="create_ticket_customer_choice").value is None
    assert screen.selectbox(key="create_ticket_product_choice").value is None
    assert find(screen.button, "Создать обращение").disabled
    screen.selectbox(key="create_ticket_customer_choice").set_value("1").run()
    assert find(screen.button, "Создать обращение").disabled
    screen.selectbox(key="create_ticket_product_choice").set_value("1").run()
    assert not find(screen.button, "Создать обращение").disabled
    assert not screen.exception
    assert api.posts == []


def test_ticket_catalog_pages_are_independent_and_never_create_records(desktop):
    """Клиент и продукт со второй страницы доступны без загрузки всей таблицы и POST."""
    screen, api = desktop
    for resource in ("customers", "products"):
        original = api.records[resource][1]
        api.records[resource] = {
            record_id: {**original, "id": record_id, "name": f"Запись {record_id}"}
            for record_id in range(1, 24)
        }
    navigate(screen, "Обращения")
    screen.button(key="create_ticket_customer_next").click().run()
    assert not screen.exception
    assert screen.session_state["create_ticket_customer_offset"] == 20
    assert screen.session_state["create_ticket_product_offset"] == 0
    assert len(screen.selectbox(key="create_ticket_customer_choice").options) == 3
    assert len(screen.selectbox(key="create_ticket_product_choice").options) == 20
    screen.selectbox(key="create_ticket_customer_choice").set_value("23").run()
    screen.button(key="create_ticket_product_next").click().run()
    assert not screen.exception
    assert screen.selectbox(key="create_ticket_customer_choice").value == "23"
    assert screen.session_state["create_ticket_product_offset"] == 20
    screen.selectbox(key="create_ticket_product_choice").set_value("22").run()
    assert api.posts == []
    assert any(read[:3] == ("list", "customers", 20) for read in api.reads)
    assert any(read[:3] == ("list", "products", 20) for read in api.reads)
    fill_ticket_form(screen)
    find(screen.button, "Создать обращение").click().run()
    assert not screen.exception
    assert len(api.posts) == 1
    assert api.posts[0][2]["customer_id"] == 23
    assert api.posts[0][2]["product_id"] == 22
    assert screen.button(key="create_ticket_customer_next").disabled
    assert screen.button(key="create_ticket_product_next").disabled


def test_ticket_duplicate_names_identify_exact_customer_and_product(desktop):
    """Одинаковые названия различаются email и кодом, а API получает выбранные ID."""
    screen, api = desktop
    api.records["customers"][2] = {
        **api.records["customers"][1],
        "id": 2,
        "email": "second@example.org",
    }
    api.records["products"][2] = {
        **api.records["products"][1],
        "id": 2,
        "code": "server-second",
    }
    navigate(screen, "Обращения")
    assert screen.selectbox(key="create_ticket_customer_choice").options == [
        "Учебный клиент — demo@example.org",
        "Учебный клиент — second@example.org",
    ]
    assert screen.selectbox(key="create_ticket_product_choice").options == [
        "Сервер — server",
        "Сервер — server-second",
    ]
    choose_ticket_parties(screen, "2", "2")
    fill_ticket_form(screen)
    assert api.posts == []
    find(screen.button, "Создать обращение").click().run()
    assert not screen.exception
    assert len(api.posts) == 1
    assert api.posts[0][2]["customer_id"] == 2
    assert api.posts[0][2]["product_id"] == 2


def test_ticket_picker_preserves_bigint_without_rounding(desktop):
    """Предельный BIGINT передаётся целым без потери точности JavaScript-чисел."""
    screen, api = desktop
    record_id = 2**63 - 1
    for resource in ("customers", "products"):
        api.records[resource] = {
            record_id: {**api.records[resource][1], "id": record_id}
        }
    navigate(screen, "Обращения")
    choose_ticket_parties(screen, str(record_id), str(record_id))
    fill_ticket_form(screen)
    find(screen.button, "Создать обращение").click().run()
    assert not screen.exception
    assert len(api.posts) == 1
    assert api.posts[0][2]["customer_id"] == record_id
    assert api.posts[0][2]["product_id"] == record_id
    assert type(api.posts[0][2]["customer_id"]) is int
    assert type(api.posts[0][2]["product_id"]) is int


@pytest.mark.parametrize(
    "resource,label", [("customers", "Клиент"), ("products", "Продукт")]
)
def test_empty_ticket_catalog_disables_creation_with_instructions(
    desktop, resource, label
):
    """Пустой справочник объясняет, где создать запись, и не допускает отправку."""
    screen, api = desktop
    api.records[resource] = {}
    navigate(screen, "Обращения")
    assert not screen.exception
    assert find(screen.button, "Создать обращение").disabled
    selector = find(screen.selectbox, label)
    assert selector.value is None
    assert selector.disabled
    messages = [
        item.value
        for kind in (screen.info, screen.warning, screen.caption)
        for item in kind
    ]
    assert any(
        "Справочники" in message and "добавьте запись" in message
        for message in messages
    )
    assert api.posts == []


@pytest.mark.parametrize(
    "resource,key", [("customers", "customer"), ("products", "product")]
)
def test_removed_ticket_selection_clears_without_choosing_another_record(
    desktop, resource, key
):
    """Исчезнувшая запись очищает выбор и запрещает создание вместо подмены ID."""
    screen, api = desktop
    api.records[resource][2] = {
        **api.records[resource][1],
        "id": 2,
        "name": "Другая запись",
    }
    navigate(screen, "Обращения")
    choose_ticket_parties(screen)
    del api.records[resource][1]
    screen.run()
    assert not screen.exception
    assert screen.selectbox(key=f"create_ticket_{key}_choice").value is None
    assert find(screen.button, "Создать обращение").disabled
    assert api.posts == []


@pytest.mark.parametrize(
    "resource,key", [("customers", "customer"), ("products", "product")]
)
def test_exact_full_ticket_catalog_page_can_return_from_empty_next_page(
    desktop, resource, key
):
    """Ровно 20 записей допускают пустую следующую страницу и безопасный возврат."""
    screen, api = desktop
    original = api.records[resource][1]
    api.records[resource] = {
        record_id: {**original, "id": record_id} for record_id in range(1, 21)
    }
    navigate(screen, "Обращения")
    screen.selectbox(key=f"create_ticket_{key}_choice").set_value("20").run()
    screen.button(key=f"create_ticket_{key}_next").click().run()
    assert not screen.exception
    assert screen.selectbox(key=f"create_ticket_{key}_choice").value is None
    assert find(screen.button, "Создать обращение").disabled
    assert screen.button(key=f"create_ticket_{key}_next").disabled
    assert not screen.button(key=f"create_ticket_{key}_previous").disabled
    screen.button(key=f"create_ticket_{key}_previous").click().run()
    assert not screen.exception
    assert len(screen.selectbox(key=f"create_ticket_{key}_choice").options) == 20
    assert screen.selectbox(key=f"create_ticket_{key}_choice").value is None
    assert screen.button(key=f"create_ticket_{key}_previous").disabled
    assert api.posts == []


@pytest.mark.parametrize("resource", ["customers", "products"])
def test_unavailable_ticket_catalog_keeps_existing_tickets_accessible(
    desktop, monkeypatch, resource
):
    """Ошибка загрузки списка блокирует создание, но существующее обращение читается."""
    screen, api = desktop
    list_records = api.list_records

    def unavailable_catalog(requested_resource, *, offset=0, **filters):
        """Отказать только в списке одного справочника, сохранив остальные GET."""
        if requested_resource == resource:
            raise APIError("Справочник временно недоступен.", 503)
        return list_records(requested_resource, offset=offset, **filters)

    monkeypatch.setattr(api, "list_records", unavailable_catalog)
    open_ticket(screen)
    assert not screen.exception
    assert any("Справочник временно недоступен." in item.value for item in screen.error)
    assert any(item.value == "Не запускается сервер" for item in screen.text)
    assert ("get", "tickets", 1) in api.reads
    assert api.posts == []


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
            {"Название / имя": "Сотрудник", "Email": "reviewer@example.org"},
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
    if resource == "users":
        assert api.posts[0][2]["role"] == "operator"
    screen.run()
    assert len(api.posts) == 1


def test_empty_ticket_form_does_not_create_record(desktop):
    """Пустая тема или описание отклоняются до HTTP-запроса."""
    screen, api = desktop
    navigate(screen, "Обращения")
    choose_ticket_parties(screen)
    find(screen.button, "Создать обращение").click().run()
    assert not screen.exception
    assert any("Заполните" in error.value for error in screen.error)
    assert api.posts == []


def test_server_failure_remains_an_operator_message(monkeypatch):
    """Недоступность сервера не раскрывает traceback и не ломает Streamlit script."""

    def unavailable_client(token):
        """Сымитировать ожидаемую ошибку конфигурации до открытия соединения."""
        raise APIError("API временно недоступен.")

    monkeypatch.setattr(app, "make_client", unavailable_client)
    screen = sign_in(AppTest.from_file(str(ENTRYPOINT), default_timeout=15).run())
    assert screen.error[0].value == "API временно недоступен."


@pytest.mark.parametrize("status", ["resolved", "closed"])
def test_closed_ticket_disables_analysis(desktop, status):
    """Закрытое обращение показывает причину и не отправляет запрос анализа."""
    screen, api = desktop
    api.records["tickets"][1]["status"] = status
    open_ticket(screen)
    assert find(screen.button, "Запустить анализ").disabled
    assert any("Обращение закрыто" in item.value for item in screen.info)
    assert api.posts == []


def test_server_error_shows_request_code_for_administrator(monkeypatch):
    """Код запроса из ответа API помогает найти строку в журнале сервера."""

    def failing_client(token):
        """Сымитировать ответ 503 с id запроса."""
        raise APIError("Сервис временно недоступен.", 503, None, "req-0123456789")

    monkeypatch.setattr(app, "make_client", failing_client)
    screen = sign_in(AppTest.from_file(str(ENTRYPOINT), default_timeout=15).run())
    assert any("req-0123456789" in item.value for item in screen.caption)


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
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # Сравниваем части пути: в Windows разделитель — обратная косая черта.
    assert Path(result.stdout.strip()).parts[-2:] == ("operator_ui", "app.py")


def test_without_token_page_asks_for_it_and_sends_nothing(monkeypatch):
    """Пустое поле токена: страница объясняет вход и не создаёт HTTP-клиент."""
    created = []
    monkeypatch.setattr(app, "make_client", lambda token: created.append(token))
    screen = AppTest.from_file(str(ENTRYPOINT), default_timeout=15).run()
    assert not screen.exception
    assert created == []
    assert any("токен сотрудника" in item.value for item in screen.info)
    sign_in(screen, "   ")
    assert created == []


def test_rejected_token_shows_message_without_pages(monkeypatch):
    """Ответ 401 на /api/auth/me показывает подсказку и не открывает разделы."""

    class RejectedAPI(FakeAPI):
        """Сымитировать отозванный токен."""

        def me(self):
            """Отказать так же, как APIClient при HTTP 401."""
            raise APIError("Токен не принят.", 401)

    api = RejectedAPI()
    monkeypatch.setattr(app, "make_client", lambda token: api)
    screen = sign_in(AppTest.from_file(str(ENTRYPOINT), default_timeout=15).run())
    assert [item.value for item in screen.error] == ["Токен не принят."]
    assert not screen.sidebar.radio
    assert api.reads == [] and api.posts == []


def test_signed_in_staff_name_and_role_are_shown(desktop):
    """Боковая панель показывает, от чьего имени выполняются действия."""
    screen, api = desktop
    captions = [item.value for item in screen.sidebar.caption]
    assert "Вы: Учебный сотрудник · Администратор" in captions
    assert ("me",) in api.reads


def test_operator_role_cannot_open_review_form(desktop):
    """Оператор видит предложение, но форма решения для его роли скрыта."""
    screen, api = desktop
    api.role = "operator"
    open_suggestion(screen)
    assert not [
        button for button in screen.button if button.label == "Сохранить решение"
    ]
    assert any("рецензент или администратор" in item.value for item in screen.info)
    assert api.posts == []


def test_operator_role_cannot_create_staff(desktop):
    """Создавать сотрудников и назначать роли может только администратор."""
    screen, api = desktop
    api.role = "operator"
    navigate(screen, "Справочники")
    find(screen.selectbox, "Справочник").set_value("Сотрудники").run()
    assert not [button for button in screen.button if button.label == "Создать запись"]
    assert any("администратор" in item.value for item in screen.info)


def test_admin_chooses_role_of_new_staff(desktop):
    """Администратор выбирает роль нового сотрудника в форме."""
    screen, api = desktop
    navigate(screen, "Справочники")
    find(screen.selectbox, "Справочник").set_value("Сотрудники").run()
    find(screen.text_input, "Название / имя").set_value("Анна")
    find(screen.text_input, "Email").set_value("anna@example.org")
    find(screen.selectbox, "Роль").set_value("Рецензент")
    find(screen.button, "Создать запись").click().run()
    assert not screen.exception
    assert api.posts == [
        (
            "create",
            "users",
            {"name": "Анна", "email": "anna@example.org", "role": "reviewer"},
        )
    ]


def open_customer_erasure(screen):
    """Открыть справочник клиентов, где администратор видит форму обезличивания."""
    navigate(screen, "Справочники")
    find(screen.selectbox, "Справочник").set_value("Клиенты").run()
    assert not screen.exception


def test_admin_erases_customer_only_after_confirmation(desktop):
    """Без флажка запрос не уходит; с флажком UI передаёт id и email."""
    screen, api = desktop
    open_customer_erasure(screen)
    find(screen.text_input, "ID клиента для обезличивания").set_value("1")
    find(screen.text_input, "Текущий email клиента для подтверждения").set_value(
        "demo@example.org"
    )
    find(screen.button, "Обезличить клиента").click().run()
    assert not screen.exception
    assert api.posts == []
    assert any("необратимо" in item.value for item in screen.error)
    find(screen.checkbox, "Понимаю, что восстановить данные нельзя").check()
    find(screen.button, "Обезличить клиента").click().run()
    assert not screen.exception
    assert api.posts == [("erase", 1, "demo@example.org")]
    assert any("Клиент №1 обезличен" in item.value for item in screen.success)
    assert "demo@example.org" not in str([item.value for item in screen.success])


def test_operator_cannot_see_customer_erasure(desktop):
    """Форма обезличивания скрыта для ролей ниже администратора."""
    screen, api = desktop
    api.role = "operator"
    open_customer_erasure(screen)
    assert not [b for b in screen.button if b.label == "Обезличить клиента"]
    assert any("администратор" in item.value for item in screen.caption)
    assert api.posts == []


def test_anonymized_ticket_hides_status_and_analysis(desktop):
    """Обезличенное обращение показывает причину и не предлагает изменений."""
    screen, api = desktop
    api.records["tickets"][1].update(
        status="closed",
        anonymized_at=STAMP,
        anonymized_reason="retention",
    )
    open_ticket(screen)
    assert any("по сроку хранения" in item.value for item in screen.info)
    assert not [b for b in screen.button if b.label == "Изменить статус"]
    assert find(screen.button, "Запустить анализ").disabled
    assert not any("смените статус" in item.value for item in screen.info)
