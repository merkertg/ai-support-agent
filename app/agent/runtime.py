"""Ограниченный цикл: модель → проверенные инструменты → черновик оператору."""

import json
import time
from collections.abc import Callable
from dataclasses import asdict

from app.agent.config import AgentSettings, get_agent_settings
from app.agent.contracts import (
    AgentContext,
    AgentOutcome,
    AgentProgress,
    AgentProvider,
    AgentTurn,
    ToolTrace,
)
from app.agent.errors import (
    AgentContextTooLongError,
    AgentInvalidCitationError,
    AgentInvalidTurnError,
    AgentStepLimitError,
    AgentTimeoutError,
    AgentToolLimitError,
)
from app.schemas.drafts import TokenUsage

MAX_TRANSCRIPT_CHARACTERS = 64_000
ToolExecutor = Callable[[str, str], tuple[dict, dict]]


class AgentRuntime:
    """Завершать каждый запуск за конечные шаги, вызовы и общее время."""

    def __init__(
        self,
        provider: AgentProvider,
        execute_tool: ToolExecutor,
        settings: AgentSettings | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Принять провайдер и исполнитель без сетевых вызовов в конструкторе."""
        self.provider = provider
        self.execute_tool = execute_tool
        self.settings = settings if settings is not None else get_agent_settings()
        self.clock = clock
        self.progress = AgentProgress()

    def _remaining(self, deadline: float) -> float:
        """Отклонить дальнейшую работу после общего монотонного дедлайна."""
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise AgentTimeoutError()
        return remaining

    def _check_transcript(self, history: list[dict]) -> None:
        """Считать полный JSON, включая reasoning и результаты инструментов."""
        if len(json.dumps(history, ensure_ascii=False)) > MAX_TRANSCRIPT_CHARACTERS:
            raise AgentContextTooLongError()

    def _add_usage(self, usage: TokenUsage | None) -> None:
        """Суммировать фактические токены всех полученных шагов провайдера."""
        if usage is None:
            return
        previous = self.progress.usage
        self.progress.usage = TokenUsage(
            input_tokens=usage.input_tokens
            + (previous.input_tokens if previous else 0),
            output_tokens=usage.output_tokens
            + (previous.output_tokens if previous else 0),
            total_tokens=usage.total_tokens
            + (previous.total_tokens if previous else 0),
        )

    def _found_documents(self) -> set[str]:
        """Собрать ключи статей из успешных вызовов search_knowledge этого запуска."""
        found: set[str] = set()
        for item in self.progress.tool_trace:
            if item.name != "search_knowledge" or item.result.get("ok") is not True:
                continue
            documents = item.result.get("data", {}).get("documents", [])
            found.update(
                document["document_key"]
                for document in documents
                if isinstance(document, dict) and "document_key" in document
            )
        return found

    def _check_sources(self, sources: list[str]) -> None:
        """Разрешить ссылаться только на статьи, реально возвращённые поиском."""
        if not set(sources) <= self._found_documents():
            raise AgentInvalidCitationError()

    def _validate_turn(self, turn: AgentTurn, seen_ids: set[str]) -> None:
        """Проверить весь пакет до первого SQL-запроса, не исполняя его частично."""
        if (
            bool(turn.calls) == (turn.final is not None)
            or not isinstance(turn.model, str)
            or not turn.model
        ):
            raise AgentInvalidTurnError()
        if any(
            not isinstance(call.name, str) or not isinstance(call.arguments, str)
            for call in turn.calls
        ):
            raise AgentInvalidTurnError()
        batch_ids = [call.call_id for call in turn.calls]
        if (
            any(
                not isinstance(value, str) or not value.strip() or len(value) > 200
                for value in batch_ids
            )
            or len(batch_ids) != len(set(batch_ids))
            or any(value in seen_ids for value in batch_ids)
        ):
            raise AgentInvalidTurnError()
        if len(seen_ids) + len(batch_ids) > self.settings.agent_max_tool_calls:
            raise AgentToolLimitError()

    def run(self, context: AgentContext) -> AgentOutcome:
        """Прочитать факты через разрешённые функции и вернуть один черновик."""
        # Ленивый импорт не связывает контракт провайдера с SQLAlchemy-инструментами.
        from app.agent.tools import TOOL_DEFINITIONS

        self.progress = AgentProgress()
        deadline = self.clock() + self.settings.agent_timeout_seconds
        history = [
            {
                "role": "user",
                "content": json.dumps(asdict(context), ensure_ascii=False),
            }
        ]
        seen_ids: set[str] = set()
        self._check_transcript(history)
        for _ in range(self.settings.agent_max_steps):
            remaining = self._remaining(deadline)
            self.progress.steps += 1
            turn = self.provider.next_turn(
                context, history, TOOL_DEFINITIONS, remaining
            )
            self._add_usage(turn.usage)
            self._remaining(deadline)
            self._validate_turn(turn, seen_ids)
            # Сохраняем ВСЕ элементы: reasoning/encrypted_content нельзя потерять
            # при stateless Responses API, где store=False и нет previous_response_id.
            history.extend(turn.output_items)
            self._check_transcript(history)
            if turn.final is not None:
                # Модель не может «процитировать» статью, которую не находила.
                self._check_sources(getattr(turn.final, "sources", []))
                return AgentOutcome(
                    final=turn.final,
                    model=turn.model,
                    provider=self.provider.provider,
                    progress=self.progress,
                )
            for call in turn.calls:
                seen_ids.add(call.call_id)
                self._remaining(deadline)
                safe_arguments, result = self.execute_tool(call.name, call.arguments)
                self.progress.tool_trace.append(
                    ToolTrace(call.call_id, call.name, safe_arguments, result)
                )
                self._remaining(deadline)
                history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )
                self._check_transcript(history)
        raise AgentStepLimitError()
