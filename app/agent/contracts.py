"""Общие типы провайдера, исполнителя инструментов и сохраняемого результата."""

from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol

from app.schemas.drafts import DraftContent, TokenUsage


@dataclass(frozen=True)
class AgentContext:
    """Задать область доступа: одно обращение и его клиент и продукт."""

    ticket_id: int
    customer_id: int
    product_id: int
    license_key_hint: str | None = None


@dataclass(frozen=True)
class AgentToolCall:
    """Передать исходный JSON аргументов на серверную проверку инструмента."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolTrace:
    """Сохранить безопасные аргументы и результат реально выполненного вызова."""

    call_id: str
    name: str
    arguments: dict
    result: dict


@dataclass
class AgentProgress:
    """Собирать шаги, вызовы и токены даже при незавершённом запуске."""

    steps: int = 0
    tool_trace: list[ToolTrace] = field(default_factory=list)
    usage: TokenUsage | None = None

    def to_dict(self) -> dict:
        """Получить JSON-совместимый снимок для записи прогресса в PostgreSQL."""
        return {
            "steps": self.steps,
            "tool_trace": [asdict(item) for item in self.tool_trace],
            "usage": self.usage.model_dump() if self.usage is not None else None,
        }


@dataclass(frozen=True)
class AgentTurn:
    """Вернуть все элементы Responses API и один из двух исходов шага."""

    output_items: list[dict]
    calls: list[AgentToolCall]
    final: DraftContent | None
    model: str
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class AgentOutcome:
    """Передать черновик сервису, который отдельно сохраняет его в БД."""

    final: DraftContent
    model: str
    provider: Literal["mock", "openai"]
    progress: AgentProgress


class AgentProvider(Protocol):
    """Отделить управление циклом от SDK и выбранной языковой модели."""

    provider: Literal["mock", "openai"]
    model: str

    def next_turn(
        self,
        context: AgentContext,
        history: list[dict],
        tools: list[dict],
        remaining_seconds: float,
    ) -> AgentTurn:
        """Получить очередные вызовы функций либо проверенный финальный JSON."""
        ...
