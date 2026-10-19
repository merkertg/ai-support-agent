"""HTTP-клиент Streamlit с ограничениями времени и безопасными ошибками.

Адрес API задаёт администратор через окружение процесса. В браузере оператора
нет поля для произвольного URL; перенаправления и повторы POST отключены.
Каждый запрос несёт токен сотрудника из его сеанса: API по нему определяет
автора решения и права. Токен не попадает в сообщения об ошибках.
"""

import os
import re
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

PAGE_SIZE = 20
MAX_ID = 2**63 - 1
# Id запроса из ответа API: по нему администратор находит строку журнала.
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")
# Формат токена сотрудника (app/services/auth.py): префикс и 43 символа base64url.
STAFF_TOKEN = re.compile(r"asa_[A-Za-z0-9_-]{43}")
UNAUTHORIZED = (
    "Токен не принят: он неверный, отозван или истёк. "
    "Проверьте токен или получите новый у администратора."
)
FORBIDDEN = "Недостаточно прав: это действие недоступно вашей роли."
TOO_MANY = "Слишком много изменений подряд. Подождите немного и повторите."
# 409 у анализа и решения означает разные состояния: подсказка зависит от действия.
ANALYZE_CONFLICT = (
    "Анализ не запущен: обращение закрыто, уже анализируется или ждёт решения "
    "по предложению. Проверьте историю запусков и очередь проверки."
)
REVIEW_CONFLICT = (
    "Решение не сохранено: предложение уже рассмотрено или обращение закрыто "
    "(тогда предложение можно только отклонить). Обновите данные."
)


@dataclass
class APIError(Exception):
    """Передать интерфейсу понятную ошибку без сетевых адресов и секретов."""

    message: str
    status_code: int | None = None
    run_id: int | None = None
    request_id: str | None = None

    def __str__(self) -> str:
        """Показать только подготовленное сообщение, пригодное для оператора."""
        return self.message


def configured_api_url() -> str:
    """Проверить серверную настройку URL, не принимая её из формы пользователя."""
    value = os.environ.get("API_URL", "http://127.0.0.1:8001").strip().rstrip("/")
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError as error:
        raise APIError("В API_URL указан неверный адрес или порт.") from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise APIError("Неверная настройка API_URL. Обратитесь к администратору.")
    return value


def staff_token(value: str) -> str:
    """Проверить формат токена до запроса, не повторяя его в тексте ошибки."""
    token = value.strip()
    if not STAFF_TOKEN.fullmatch(token):
        raise APIError(
            "Токен сотрудника имеет неверный формат: он начинается с asa_ "
            "и содержит 47 символов. Скопируйте его целиком."
        )
    return token


def positive_id(value: int) -> int:
    """Не позволить подменить часть маршрута строкой или некорректным числом."""
    if type(value) is not int or not 1 <= value <= MAX_ID:
        raise APIError("Идентификатор должен быть положительным целым числом.")
    return value


class APIClient:
    """Отправлять ровно один HTTP-запрос на каждое явное действие интерфейса."""

    def __init__(
        self,
        token: str,
        base_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Создать клиент одного запуска страницы с токеном и конечным тайм-аутом."""
        headers = {"Authorization": f"Bearer {staff_token(token)}"}
        self._client = httpx.Client(
            base_url=base_url or configured_api_url(),
            headers=headers,
            timeout=httpx.Timeout(130, connect=5, write=10, pool=5),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )

    def __enter__(self) -> Self:
        """Вернуть клиент для гарантированного закрытия в конце запуска страницы."""
        return self

    def __exit__(self, *args: object) -> None:
        """Освободить сетевые соединения даже при ошибке отрисовки."""
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        expected: type = dict,
        conflict: str | None = None,
    ) -> Any:
        """Прочитать JSON и скрыть непредвиденный HTML, traceback или сетевой адрес."""
        try:
            response = self._client.request(method, path, params=params, json=payload)
        except httpx.TimeoutException as error:
            message = "API не ответил вовремя."
            if method in {"POST", "PATCH"}:
                message += (
                    " Действие могло сохраниться: обновите карточку или очередь,"
                    " затем решите, нужен ли повтор."
                )
            raise APIError(message) from error
        except httpx.RequestError as error:
            raise APIError(
                "Нет соединения с API. Проверьте запуск сервисов."
            ) from error
        if not response.is_success:
            self._raise_error(response, conflict)
        try:
            value = response.json()
        except ValueError as error:
            raise APIError("API вернул ответ в неожиданном формате.") from error
        if not isinstance(value, expected):
            raise APIError("API вернул ответ в неожиданном формате.")
        return value

    @staticmethod
    def _raise_error(response: httpx.Response, conflict: str | None = None) -> None:
        """Сопоставить код ответа безопасной подсказке без вывода тела ошибки."""
        messages = {
            401: UNAUTHORIZED,
            403: FORBIDDEN,
            404: "Запись не найдена. Проверьте её идентификатор.",
            409: "Запись уже существует или решение уже принято. Обновите данные.",
            422: "API отклонил форму. Проверьте обязательные поля и ограничения.",
            429: TOO_MANY,
            502: "Внешний сервис вернул некорректный ответ. Попробуйте позже.",
            503: "Сервис временно недоступен. Проверьте состояние API, БД и индекса.",
            504: "Время выполнения истекло. Сначала проверьте историю запусков.",
        }
        run_id = None
        try:
            candidate = response.json().get("run_id")
            if type(candidate) is int and candidate > 0:
                run_id = candidate
        except (ValueError, AttributeError):
            pass
        if conflict is not None:
            messages[409] = conflict
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isascii() and retry_after.isdigit():
            messages[429] = (
                f"Слишком много изменений подряд. Повторите через {retry_after} с."
            )
        request_id = response.headers.get("X-Request-ID", "")
        raise APIError(
            messages.get(response.status_code, "API не смог выполнить действие."),
            response.status_code,
            run_id,
            request_id if REQUEST_ID.fullmatch(request_id) else None,
        )

    def me(self) -> dict:
        """Узнать имя и роль сотрудника, которому принадлежит токен."""
        return self._request("GET", "/api/auth/me")

    def status(self, area: str) -> dict:
        """Прочитать только один из заранее известных статусов сервисов."""
        if area not in {"llm", "knowledge"}:
            raise APIError("Неизвестный сервис.")
        return self._request("GET", f"/api/{area}/status")

    def list_records(
        self, resource: str, *, offset: int = 0, **filters: Any
    ) -> list[dict]:
        """Загрузить одну страницу из 20 записей, исключив пустые фильтры."""
        if resource not in {
            "tickets",
            "customers",
            "products",
            "licenses",
            "users",
            "agent-runs",
            "suggestions",
        }:
            raise APIError("Неизвестный список.")
        if type(offset) is not int or not 0 <= offset <= MAX_ID:
            raise APIError("Неверная страница списка.")
        params = {key: value for key, value in filters.items() if value is not None}
        params.update(offset=offset, limit=PAGE_SIZE)
        return self._request("GET", f"/api/{resource}", params=params, expected=list)

    def get_record(self, resource: str, record_id: int) -> dict:
        """Прочитать карточку по проверенному идентификатору."""
        if resource not in {
            "tickets",
            "customers",
            "products",
            "licenses",
            "users",
            "agent-runs",
            "suggestions",
        }:
            raise APIError("Неизвестная карточка.")
        return self._request("GET", f"/api/{resource}/{positive_id(record_id)}")

    def create_record(self, resource: str, payload: dict) -> dict:
        """Создать запись только в одном из разрешённых справочников или обращений."""
        if resource not in {"tickets", "customers", "products", "licenses", "users"}:
            raise APIError("Создание этого ресурса недоступно.")
        return self._request("POST", f"/api/{resource}", payload=payload)

    def analyze(self, ticket_id: int, license_key: str | None) -> dict:
        """Запустить анализ один раз; результат и предложение сохранит сервер."""
        return self._request(
            "POST",
            f"/api/tickets/{positive_id(ticket_id)}/analyze",
            payload={"license_key": license_key or None},
            conflict=ANALYZE_CONFLICT,
        )

    def review(self, suggestion_id: int, payload: dict) -> dict:
        """Отправить одно решение; повтор после сетевой ошибки остаётся ручным."""
        return self._request(
            "POST",
            f"/api/suggestions/{positive_id(suggestion_id)}/review",
            payload=payload,
            conflict=REVIEW_CONFLICT,
        )

    def messages(self, ticket_id: int, *, offset: int = 0) -> list[dict]:
        """Прочитать ограниченную страницу сообщений текущего обращения."""
        if type(offset) is not int or not 0 <= offset <= MAX_ID:
            raise APIError("Неверная страница сообщений.")
        return self._request(
            "GET",
            f"/api/tickets/{positive_id(ticket_id)}/messages",
            params={"offset": offset, "limit": PAGE_SIZE},
            expected=list,
        )

    def search(self, query: str, product_code: str | None, limit: int) -> dict:
        """Выполнить явный поиск; эмбеддинги могут обращаться к реальному провайдеру."""
        return self._request(
            "POST",
            "/api/knowledge/search",
            payload={
                "query": query,
                "product_code": product_code or None,
                "limit": limit,
            },
        )

    def dashboard(self) -> dict:
        """Прочитать серверные метрики без расчётов по ограниченной странице списка."""
        return self._request("GET", "/api/dashboard")

    def update_ticket_status(self, ticket_id: int, status: str) -> dict:
        """Изменить только статус обращения по явному действию оператора."""
        return self._request(
            "PATCH",
            f"/api/tickets/{positive_id(ticket_id)}",
            payload={"status": status},
        )
