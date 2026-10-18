"""Настоящий SDK Responses API с локальным транспортом вместо платной сети."""

import json
from collections.abc import Callable, Iterator

import httpx2
import openai
import pytest
from pydantic import ValidationError

from app.agent.contracts import AgentContext
from app.agent.provider import (
    SYSTEM_INSTRUCTIONS,
    MockAgentProvider,
    OpenAIAgentProvider,
    get_agent_provider,
)
from app.agent.runtime import AgentRuntime
from app.llm.config import LLMSettings, get_llm_settings
from app.llm.errors import (
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMRefusedError,
    LLMTimeoutError,
    LLMUnavailableError,
)

CONTEXT = AgentContext(7, 2, 3)
CONTENT = {
    "summary": "Сбой запуска",
    "category": "other",
    "draft_reply": "Уточните версию.",
}
FAKE_KEY = "test-placeholder-never-a-real-key"
ENV_FIELDS = (
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_OUTPUT_TOKENS",
    "OPENAI_BASE_URL",
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path) -> Iterator[None]:
    """Не читать локальный ключ пользователя ни в одном транспортном тесте."""
    for name in ENV_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_llm_settings.cache_clear()
    yield
    get_llm_settings.cache_clear()


def settings(**overrides) -> LLMSettings:
    """Настроить явную тестовую модель и заведомо ненастоящий API-ключ."""
    values = {
        "llm_provider": "openai",
        "openai_api_key": FAKE_KEY,
        "openai_model": "explicit-test-agent",
        "llm_timeout_seconds": 12,
        "llm_max_output_tokens": 700,
    }
    values.update(overrides)
    return LLMSettings(_env_file=None, **values)


def message(content: dict | None = None) -> dict:
    """Создать финальное сообщение Responses в официальном JSON-формате."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": json.dumps(content or CONTENT, ensure_ascii=False),
            }
        ],
    }


def function(call_id: str = "a", arguments: str = '{"ticket_id":7}') -> dict:
    """Сформировать вызов функции, в том числе с намеренно неверным JSON."""
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": "get_ticket",
        "arguments": arguments,
    }


def payload(output: list[dict] | None = None, **overrides) -> dict:
    """Передать SDK завершённый ответ и проверяемые счётчики расхода токенов."""
    value = {
        "id": "resp_test",
        "object": "response",
        "created_at": 1_700_000_000,
        "status": "completed",
        "error": None,
        "model": "test-model-snapshot",
        "output": [message()] if output is None else output,
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }
    value.update(overrides)
    return value


def sdk_factory_for(
    handler: Callable,
    clients: list | None = None,
    options: list | None = None,
) -> Callable[..., openai.OpenAI]:
    """Подменить только HTTP-транспорт, оставив реальный парсер и сериализацию."""

    def factory(**kwargs) -> openai.OpenAI:
        """Закрепить локальный обработчик; реальный сетевой запрос невозможен."""
        client = httpx2.Client(transport=httpx2.MockTransport(handler))
        if clients is not None:
            clients.append(client)
        if options is not None:
            options.append(kwargs)
        return openai.OpenAI(http_client=client, **kwargs)

    return factory


def provider_for(value: dict) -> OpenAIAgentProvider:
    """Получить провайдер с одним предопределённым ответом через SDK."""

    def handler(request) -> httpx2.Response:
        """Вернуть тестовый JSON без соединения с настоящим OpenAI."""
        return httpx2.Response(200, json=value)

    return OpenAIAgentProvider(settings(), sdk_factory=sdk_factory_for(handler))


def test_real_sdk_multiturn_replays_reasoning_batch_and_outputs() -> None:
    """Проверить полный цикл двух Responses-запросов на настоящем SDK-парсере."""
    bodies = []
    clients = []
    options = []
    reasoning = {
        "id": "rs_a",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque-reasoning-test-placeholder",
    }
    first_output = [reasoning, function("a"), function("b")]

    def handler(request) -> httpx2.Response:
        """Сохранить оба HTTP-тела и вернуть функции, затем финальный JSON."""
        bodies.append(json.loads(request.content))
        output = first_output if len(bodies) == 1 else [message()]
        return httpx2.Response(200, json=payload(output))

    def execute(name, arguments) -> tuple[dict, dict]:
        """Вернуть проверяемый результат инструмента без PostgreSQL."""
        return {"ticket_id": 7}, {"ok": True, "data": {"ticket": {"id": 7}}}

    provider = OpenAIAgentProvider(
        settings(), sdk_factory=sdk_factory_for(handler, clients, options)
    )
    result = AgentRuntime(provider, execute).run(CONTEXT)
    assert result.final.model_dump() == CONTENT
    assert result.provider == "openai"
    assert result.model == "test-model-snapshot"
    assert result.progress.usage.total_tokens == 28
    assert len(bodies) == 2
    for body in bodies:
        assert body["model"] == "explicit-test-agent"
        assert body["instructions"] == SYSTEM_INSTRUCTIONS
        assert body["store"] is False
        assert body["truncation"] == "disabled"
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["max_output_tokens"] == 700
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["additionalProperties"] is False
        assert "previous_response_id" not in body
        assert {tool["name"] for tool in body["tools"]} == {
            "get_ticket",
            "get_customer",
            "get_license",
            "get_ticket_history",
        }
        assert all(tool["strict"] is True for tool in body["tools"])
    replay = bodies[1]["input"]
    assert replay[1:4] == first_output
    assert [item["call_id"] for item in replay[4:]] == ["a", "b"]
    assert all(item["type"] == "function_call_output" for item in replay[4:])
    assert all(client.is_closed for client in clients)
    assert all(option["max_retries"] == 0 for option in options)
    assert all(option["base_url"] == "https://api.openai.com/v1" for option in options)
    assert "encrypted_content" not in json.dumps(result.progress.to_dict())


def test_sdk_create_keeps_invalid_function_arguments_for_server_validation() -> None:
    """Не вызывать parse: неверный JSON функции должен стать безопасной tool-ошибкой."""
    turn = provider_for(payload([function(arguments="{broken-json")])).next_turn(
        CONTEXT, [], [], 5
    )
    assert turn.calls[0].arguments == "{broken-json"
    assert turn.final is None


def test_sdk_timeout_uses_remaining_common_budget_and_closes_client() -> None:
    """Не дать отдельному запросу превысить остаток общего бюджета runtime."""
    options = []
    clients = []

    def handler(request) -> httpx2.Response:
        """Вернуть финал через локальный транспорт и захватить настройки SDK."""
        return httpx2.Response(200, json=payload())

    provider = OpenAIAgentProvider(
        settings(), sdk_factory=sdk_factory_for(handler, clients, options)
    )
    provider.next_turn(CONTEXT, [], [], 2.5)
    assert options[0]["timeout"] == 2.5
    assert clients[0].is_closed


@pytest.mark.parametrize(
    "value,error",
    [
        (payload([], status="incomplete"), LLMInvalidResponseError),
        (payload([], status="failed"), LLMInvalidResponseError),
        (
            payload([], error={"code": "server_error", "message": "secret"}),
            LLMInvalidResponseError,
        ),
        (payload([]), LLMInvalidResponseError),
        (payload([function(), message()]), LLMInvalidResponseError),
        (payload([message({**CONTENT, "extra": "secret"})]), LLMInvalidResponseError),
        (
            payload([message({**CONTENT, "category": "invented"})]),
            LLMInvalidResponseError,
        ),
        (payload([message({**CONTENT, "summary": ""})]), LLMInvalidResponseError),
        (payload([message()], model=""), LLMInvalidResponseError),
        (
            payload(
                [message()],
                usage={"input_tokens": -1, "output_tokens": 0, "total_tokens": 0},
            ),
            LLMInvalidResponseError,
        ),
    ],
)
def test_invalid_responses_are_safe(value, error) -> None:
    """Отклонить незавершённый, неоднозначный и не соответствующий схеме ответ."""
    with pytest.raises(error) as raised:
        provider_for(value).next_turn(CONTEXT, [], [], 5)
    assert "secret" not in str(raised.value)


def test_refusal_is_not_a_usable_draft() -> None:
    """Не сохранять отказ модели как полезный черновик и скрыть текст отказа."""
    refusal = message()
    refusal["content"] = [{"type": "refusal", "refusal": "secret-provider-message"}]
    with pytest.raises(LLMRefusedError) as error:
        provider_for(payload([refusal])).next_turn(CONTEXT, [], [], 5)
    assert "secret-provider-message" not in str(error.value)


def test_invalid_json_is_masked() -> None:
    """Не выпускать JSONDecodeError с текстом ответа модели наружу."""
    invalid = message()
    invalid["content"][0]["text"] = "secret-not-json"
    with pytest.raises(LLMInvalidResponseError) as error:
        provider_for(payload([invalid])).next_turn(CONTEXT, [], [], 5)
    assert "secret-not-json" not in str(error.value)


@pytest.mark.parametrize(
    "status,error",
    [
        (401, LLMConfigurationError),
        (403, LLMConfigurationError),
        (429, LLMUnavailableError),
        (500, LLMUnavailableError),
        (400, LLMUnavailableError),
    ],
)
def test_sdk_http_errors_have_no_retries_or_sensitive_body(status, error) -> None:
    """Преобразовать ошибку API в безопасный текст и не повторять платную попытку."""
    attempts = []
    clients = []

    def handler(request) -> httpx2.Response:
        """Посчитать реальные попытки SDK при ошибочном HTTP-статусе."""
        attempts.append(request)
        return httpx2.Response(status, json={"error": {"message": "secret-api-body"}})

    provider = OpenAIAgentProvider(
        settings(), sdk_factory=sdk_factory_for(handler, clients)
    )
    with pytest.raises(error) as raised:
        provider.next_turn(CONTEXT, [], [], 5)
    assert len(attempts) == 1
    assert clients[0].is_closed
    assert "secret-api-body" not in str(raised.value)


def test_sdk_timeout_is_safe_and_not_retried() -> None:
    """Преобразовать реальный тип тайм-аута HTTPX2 и закрыть транспорт SDK."""
    attempts = []
    clients = []

    def handler(request) -> httpx2.Response:
        """Имитировать только сетевое исключение; не подключаться к интернету."""
        attempts.append(request)
        raise httpx2.ReadTimeout("secret-timeout", request=request)

    provider = OpenAIAgentProvider(
        settings(), sdk_factory=sdk_factory_for(handler, clients)
    )
    with pytest.raises(LLMTimeoutError) as error:
        provider.next_turn(CONTEXT, [], [], 5)
    assert len(attempts) == 1
    assert clients[0].is_closed
    assert "secret-timeout" not in str(error.value)


def test_factory_is_mock_without_key_and_makes_no_network_request() -> None:
    """Сохранить согласованный пользователем mock как исходный режим этапа05."""
    assert isinstance(get_agent_provider(), MockAgentProvider)


@pytest.mark.parametrize("missing", ["openai_api_key", "openai_model"])
def test_openai_requires_explicit_key_and_model(missing) -> None:
    """Не выбирать платную модель и не пытаться угадывать ключ пользователя."""
    with pytest.raises(LLMConfigurationError):
        OpenAIAgentProvider(settings(**{missing: None}))


def test_factory_masks_bad_llm_environment(monkeypatch) -> None:
    """Не раскрыть невалидные значения LLM-настроек из окружения."""
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "secret-invalid-timeout")
    with pytest.raises(LLMConfigurationError) as error:
        get_agent_provider()
    assert "secret-invalid-timeout" not in str(error.value)


def test_pydantic_model_rejects_infinite_llm_timeout() -> None:
    """Не использовать бесконечный SDK-тайм-аут даже при прямой конфигурации."""
    with pytest.raises(ValidationError):
        settings(llm_timeout_seconds=float("inf"))
