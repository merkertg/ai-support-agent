"""Цикл агента без PostgreSQL и сети: лимиты, дедлайн и безопасный прогресс."""

import copy
import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from app.agent.config import AgentSettings, get_agent_settings
from app.agent.contracts import AgentContext, AgentToolCall, AgentTurn
from app.agent.errors import (
    AgentConfigurationError,
    AgentContextTooLongError,
    AgentInvalidCitationError,
    AgentInvalidTurnError,
    AgentStepLimitError,
    AgentTimeoutError,
    AgentToolLimitError,
)
from app.agent.provider import MOCK_MARKER, MockAgentProvider
from app.agent.runtime import MAX_TRANSCRIPT_CHARACTERS, AgentRuntime
from app.llm.errors import LLMUnavailableError
from app.schemas.drafts import AgentDraftContent, DraftContent, TokenUsage

CONTEXT = AgentContext(7, 2, 3)
FINAL = DraftContent(
    summary="Проблема", category="other", draft_reply="Уточните версию."
)


def call_turn(*ids: str, usage: TokenUsage | None = None) -> AgentTurn:
    """Собрать пакет вызовов с сохраняемыми элементами протокола Responses."""
    calls = [AgentToolCall(value, "get_ticket", '{"ticket_id":7}') for value in ids]
    return AgentTurn(
        [
            {
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in calls
        ],
        calls,
        None,
        "test-model",
        usage,
    )


def final_turn(usage: TokenUsage | None = None) -> AgentTurn:
    """Вернуть завершённый структурированный черновик тестового провайдера."""
    return AgentTurn(
        [{"type": "message", "content": FINAL.model_dump_json()}],
        [],
        FINAL,
        "test-final-model",
        usage,
    )


class ScriptedProvider:
    """Записать входы и проиграть выбранные ответы без имитации HTTP-сети."""

    provider = "mock"
    model = "test-model"

    def __init__(self, turns: list[AgentTurn | Exception]) -> None:
        """Скопировать сценарий, чтобы тест не зависел от изменений списка."""
        self.turns = iter(turns)
        self.histories = []
        self.remaining = []

    def next_turn(self, context, history, tools, remaining_seconds) -> AgentTurn:
        """Сохранить полную историю и вернуть следующий исход сценария."""
        self.histories.append(copy.deepcopy(history))
        self.remaining.append(remaining_seconds)
        value = next(self.turns)
        if isinstance(value, Exception):
            raise value
        return value


def executor_calls() -> tuple[Callable, list]:
    """Получить исполнитель и журнал реально исполненных инструментов."""
    calls = []

    def execute(name: str, arguments: str) -> tuple[dict, dict]:
        """Показать, что исходный JSON проверяет отдельный исполнитель."""
        calls.append((name, arguments))
        return {"ticket_id": 7}, {"ok": True, "data": {"value": 9}}

    return execute, calls


def test_runtime_replays_all_items_and_matches_batch_results() -> None:
    """Передать reasoning, все функции и ответы с точными call_id второму шагу."""
    first = call_turn("a", "b")
    reasoning = {"type": "reasoning", "encrypted_content": "opaque", "summary": []}
    first.output_items.insert(0, reasoning)
    provider = ScriptedProvider([first, final_turn()])
    execute, calls = executor_calls()
    runtime = AgentRuntime(provider, execute)
    result = runtime.run(CONTEXT)
    assert result.final == FINAL
    assert result.model == "test-final-model"
    assert result.progress.steps == 2
    assert len(calls) == 2
    history = provider.histories[1]
    assert history[1:4] == first.output_items
    assert history[1] == reasoning
    assert [item["call_id"] for item in history[4:]] == ["a", "b"]
    assert all(item["type"] == "function_call_output" for item in history[4:])
    assert result.progress.to_dict()["tool_trace"][0]["arguments"] == {"ticket_id": 7}
    json.dumps(result.progress.to_dict())


def test_usage_sums_every_successful_model_turn() -> None:
    """Сохранить сумму расхода по шагам, а не только токены финального ответа."""
    usage = TokenUsage(input_tokens=5, output_tokens=3, total_tokens=8)
    provider = ScriptedProvider([call_turn("a", usage=usage), final_turn(usage)])
    execute, _ = executor_calls()
    result = AgentRuntime(provider, execute).run(CONTEXT)
    assert result.progress.usage == TokenUsage(
        input_tokens=10, output_tokens=6, total_tokens=16
    )
    assert result.progress.to_dict()["usage"]["total_tokens"] == 16


def test_steps_and_trace_remain_accessible_after_provider_failure() -> None:
    """Не терять уже выполненный SQL-инструмент при сбое следующего шага."""
    provider = ScriptedProvider([call_turn("a"), LLMUnavailableError()])
    execute, _ = executor_calls()
    runtime = AgentRuntime(provider, execute)
    with pytest.raises(LLMUnavailableError):
        runtime.run(CONTEXT)
    assert runtime.progress.steps == 2
    assert len(runtime.progress.tool_trace) == 1


def test_step_limit_stops_before_an_extra_provider_request() -> None:
    """Не создавать скрытый дополнительный запрос после исчерпания бюджета."""
    provider = ScriptedProvider([call_turn("a"), final_turn()])
    execute, calls = executor_calls()
    runtime = AgentRuntime(provider, execute, AgentSettings(agent_max_steps=1))
    with pytest.raises(AgentStepLimitError):
        runtime.run(CONTEXT)
    assert runtime.progress.steps == 1
    assert len(provider.histories) == len(calls) == 1


def test_batch_over_budget_executes_nothing() -> None:
    """Не исполнять первую функцию пакета, если весь пакет превышает лимит."""
    provider = ScriptedProvider([call_turn("a", "b")])
    execute, calls = executor_calls()
    runtime = AgentRuntime(provider, execute, AgentSettings(agent_max_tool_calls=1))
    with pytest.raises(AgentToolLimitError):
        runtime.run(CONTEXT)
    assert calls == []
    assert runtime.progress.tool_trace == []


def test_tool_budget_is_global_across_turns() -> None:
    """Считать вызовы всех шагов в общем бюджете одного запуска."""
    provider = ScriptedProvider([call_turn("a"), call_turn("b", "c")])
    execute, calls = executor_calls()
    runtime = AgentRuntime(provider, execute, AgentSettings(agent_max_tool_calls=2))
    with pytest.raises(AgentToolLimitError):
        runtime.run(CONTEXT)
    assert len(calls) == 1


@pytest.mark.parametrize("ids", [("a", "a"), ("",), ("  ",), ("x" * 201,)])
def test_invalid_batch_ids_execute_nothing(ids) -> None:
    """Отклонить повторные, пустые и слишком длинные идентификаторы пакета."""
    execute, calls = executor_calls()
    runtime = AgentRuntime(ScriptedProvider([call_turn(*ids)]), execute)
    with pytest.raises(AgentInvalidTurnError):
        runtime.run(CONTEXT)
    assert calls == []


def test_call_ids_cannot_repeat_in_a_later_turn() -> None:
    """Защитить соответствие Responses между вызовами разных шагов."""
    execute, calls = executor_calls()
    runtime = AgentRuntime(ScriptedProvider([call_turn("a"), call_turn("a")]), execute)
    with pytest.raises(AgentInvalidTurnError):
        runtime.run(CONTEXT)
    assert len(calls) == 1


@pytest.mark.parametrize("both", [False, True])
def test_turn_requires_exactly_calls_or_final(both: bool) -> None:
    """Не принимать пустой ответ и неоднозначный ответ с функциями и черновиком."""
    turn = AgentTurn(
        [], call_turn("a").calls if both else [], FINAL if both else None, "m"
    )
    execute, calls = executor_calls()
    with pytest.raises(AgentInvalidTurnError):
        AgentRuntime(ScriptedProvider([turn]), execute).run(CONTEXT)
    assert calls == []


def test_initial_context_limit_prevents_provider_call() -> None:
    """Проверять исходный JSON без молчаливого отрезания ключа или контекста."""
    execute, _ = executor_calls()
    provider = ScriptedProvider([])
    runtime = AgentRuntime(provider, execute)
    with pytest.raises(AgentContextTooLongError):
        runtime.run(AgentContext(7, 2, 3, "x" * MAX_TRANSCRIPT_CHARACTERS))
    assert provider.histories == []
    assert runtime.progress.steps == 0


def test_tool_output_limit_prevents_next_provider_call() -> None:
    """Не отправлять огромный результат инструмента на следующий платный шаг."""
    provider = ScriptedProvider([call_turn("a"), final_turn()])

    def execute(name, arguments) -> tuple[dict, dict]:
        """Создать слишком большой результат без доступа к базе данных."""
        return {}, {"ok": True, "data": "x" * MAX_TRANSCRIPT_CHARACTERS}

    runtime = AgentRuntime(provider, execute)
    with pytest.raises(AgentContextTooLongError):
        runtime.run(CONTEXT)
    assert len(provider.histories) == 1
    assert len(runtime.progress.tool_trace) == 1


def test_provider_output_limit_prevents_tool_execution() -> None:
    """Включать reasoning/output в предел перед исполнением нового инструмента."""
    turn = call_turn("a")
    turn.output_items.append({"reasoning": "x" * MAX_TRANSCRIPT_CHARACTERS})
    execute, calls = executor_calls()
    with pytest.raises(AgentContextTooLongError):
        AgentRuntime(ScriptedProvider([turn]), execute).run(CONTEXT)
    assert calls == []


def test_timeout_after_provider_prevents_tools() -> None:
    """Не исполнять SQL после модели, которая исчерпала общее время запуска."""
    now = [0.0]

    class SlowProvider(ScriptedProvider):
        """Передвинуть тестовые монотонные часы во время ответа модели."""

        def next_turn(self, *args) -> AgentTurn:
            """Вернуть инструменты лишь после истечения общего бюджета."""
            now[0] = 2.0
            return call_turn("a")

    execute, calls = executor_calls()
    runtime = AgentRuntime(
        SlowProvider([]),
        execute,
        AgentSettings(agent_timeout_seconds=1),
        clock=lambda: now[0],
    )
    with pytest.raises(AgentTimeoutError):
        runtime.run(CONTEXT)
    assert calls == []
    assert runtime.progress.steps == 1


def test_timeout_after_tool_keeps_trace_and_stops_batch() -> None:
    """Записать завершённый инструмент, но не запускать следующий после дедлайна."""
    now = [0.0]
    calls = []

    def execute(name, arguments) -> tuple[dict, dict]:
        """Потратить общий бюджет внутри первой функции тестового пакета."""
        calls.append(name)
        now[0] = 2.0
        return {}, {"ok": True}

    runtime = AgentRuntime(
        ScriptedProvider([call_turn("a", "b")]),
        execute,
        AgentSettings(agent_timeout_seconds=1),
        clock=lambda: now[0],
    )
    with pytest.raises(AgentTimeoutError):
        runtime.run(CONTEXT)
    assert len(calls) == len(runtime.progress.tool_trace) == 1


def test_soft_errors_continue_and_count_against_budget() -> None:
    """Позволить модели исправить аргументы после безопасной ошибки инструмента."""
    provider = ScriptedProvider([call_turn("a"), final_turn()])

    def execute(name, arguments) -> tuple[dict, dict]:
        """Вернуть контролируемую ошибку вместо исключения с исходными данными."""
        return {}, {
            "ok": False,
            "error": {"code": "invalid_arguments", "message": "Ошибка"},
        }

    result = AgentRuntime(provider, execute).run(CONTEXT)
    assert len(result.progress.tool_trace) == 1
    output = provider.histories[1][-1]
    assert json.loads(output["output"])["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    "hint,valid", [(None, None), ("LIC-TEST", True), ("LIC-TEST", False)]
)
def test_mock_uses_actual_tool_results_without_network(hint, valid) -> None:
    """Выполнить три шага mock и вывести фактический результат проверки лицензии."""
    names = []

    def execute(name, arguments) -> tuple[dict, dict]:
        """Вернуть ограниченные факты, различающие действующую и истёкшую лицензию."""
        names.append(name)
        data = {
            "get_ticket": {"ticket": {"subject": "Не запускается", "customer_id": 2}},
            "get_customer": {"customer": {"id": 2}},
            "get_ticket_history": {"tickets": [{"id": 1}]},
            "get_license": {"license": {"status": "active"}, "valid": valid},
            "search_knowledge": {
                "documents": [
                    {"document_key": "KB-SERVER-START", "title": "Сервер"},
                    {"document_key": "KB-SERVER-START", "title": "Повтор"},
                    {"document_key": "KB-SUPPORT-DATA", "title": "Сведения"},
                ]
            },
        }[name]
        return json.loads(arguments), {"ok": True, "data": data}

    result = AgentRuntime(MockAgentProvider(), execute).run(AgentContext(7, 2, 3, hint))
    assert result.progress.steps == 3
    assert MOCK_MARKER in result.final.draft_reply
    assert "Не запускается" in result.final.summary
    assert names[:3] == ["get_ticket", "get_customer", "get_ticket_history"]
    assert names[-1] == "search_knowledge"
    assert ("get_license" in names) == (hint is not None)
    # Повтор статьи не дублирует источник; цитируются только найденные ключи.
    assert result.final.sources == ["KB-SERVER-START", "KB-SUPPORT-DATA"]
    assert "KB-SERVER-START: Сервер" in result.final.draft_reply
    if hint is None:
        assert "Лицензия не проверялась" in result.final.draft_reply
    elif valid:
        assert "лицензия действующая" in result.final.draft_reply
    else:
        assert "лицензия недействующая" in result.final.draft_reply


def test_mock_never_turns_tool_error_into_license_success() -> None:
    """Не называть лицензию действующей при not_found или сбое SQL-инструмента."""

    def execute(name, arguments) -> tuple[dict, dict]:
        """Вернуть безопасные ошибки всех инструментов демонстрационного запуска."""
        return {}, {
            "ok": False,
            "error": {"code": "not_found", "message": "Недоступно"},
        }

    result = AgentRuntime(MockAgentProvider(), execute).run(
        AgentContext(7, 2, 3, "LIC")
    )
    assert "не подтверждена" in result.final.draft_reply
    assert "История клиента недоступна" in result.final.draft_reply
    assert "лицензия действующая" not in result.final.draft_reply
    assert "База знаний недоступна" in result.final.draft_reply
    assert result.final.sources == []


def search_turn() -> AgentTurn:
    """Вызвать search_knowledge одним пакетом протокола Responses."""
    call = AgentToolCall("search-1", "search_knowledge", '{"query":"сервер","limit":3}')
    return AgentTurn(
        [{"type": "function_call", "call_id": "search-1", "name": call.name}],
        [call],
        None,
        "test-model",
    )


def cited_final(*sources: str) -> AgentTurn:
    """Вернуть черновик со ссылками на указанные статьи."""
    final = AgentDraftContent(
        summary="Сбой", category="technical_issue", draft_reply="Ответ", sources=sources
    )
    return AgentTurn([{"type": "message"}], [], final, "test-model")


def search_executor(result: dict) -> Callable:
    """Вернуть заданный результат поиска для любого вызова инструмента."""

    def execute(name: str, arguments: str) -> tuple[dict, dict]:
        """Не обращаться к Qdrant: результат поиска задан тестом."""
        return {"query": "сервер", "limit": 3}, result

    return execute


FOUND = {"ok": True, "data": {"documents": [{"document_key": "KB-SERVER-START"}]}}


def test_sources_found_by_search_are_accepted() -> None:
    """Ссылка на статью из результата поиска сохраняется в итоге."""
    provider = ScriptedProvider([search_turn(), cited_final("KB-SERVER-START")])
    result = AgentRuntime(provider, search_executor(FOUND)).run(CONTEXT)
    assert result.final.sources == ["KB-SERVER-START"]


@pytest.mark.parametrize(
    ("search_result", "cited"),
    [
        # Статья существует в базе, но этот запуск её не находил.
        (FOUND, ("KB-PASSWORD-RESET",)),
        # Ошибка поиска не даёт права ссылаться на какую-либо статью.
        (
            {"ok": False, "error": {"code": "knowledge_unavailable"}},
            ("KB-SERVER-START",),
        ),
        # Результат не search_knowledge не считается найденным источником.
        ({"ok": True, "data": {"documents": []}}, ("KB-SERVER-START",)),
    ],
)
def test_sources_outside_search_results_stop_the_run(search_result, cited) -> None:
    """Модель не может процитировать статью, которой нет в результатах поиска."""
    provider = ScriptedProvider([search_turn(), cited_final(*cited)])
    runtime = AgentRuntime(provider, search_executor(search_result))
    with pytest.raises(AgentInvalidCitationError):
        runtime.run(CONTEXT)
    assert runtime.progress.steps == 2


def test_citation_without_any_search_is_rejected() -> None:
    """Даже первый шаг не может вернуть источники без вызова поиска."""
    provider = ScriptedProvider([cited_final("KB-SERVER-START")])
    executor, calls = executor_calls()
    with pytest.raises(AgentInvalidCitationError):
        AgentRuntime(provider, executor).run(CONTEXT)
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_max_steps", 0),
        ("agent_max_steps", 9),
        ("agent_max_tool_calls", 0),
        ("agent_max_tool_calls", 21),
        ("agent_timeout_seconds", 0),
        ("agent_timeout_seconds", 121),
        ("agent_timeout_seconds", float("inf")),
        ("agent_timeout_seconds", float("nan")),
    ],
)
def test_agent_settings_reject_unbounded_values(field, value) -> None:
    """Не разрешать выключить конечные лимиты через переменные окружения."""
    with pytest.raises(ValidationError):
        AgentSettings(_env_file=None, **{field: value})


def test_invalid_environment_is_masked(monkeypatch, tmp_path) -> None:
    """Вернуть постоянную ошибку конфигурации без строки из окружения."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_MAX_STEPS", "secret-not-an-integer")
    get_agent_settings.cache_clear()
    try:
        with pytest.raises(AgentConfigurationError) as error:
            get_agent_settings()
        assert "secret-not-an-integer" not in str(error.value)
    finally:
        get_agent_settings.cache_clear()
