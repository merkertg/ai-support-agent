"""Сквозные автономные сценарии и проверка чувствительности security-набора."""

import json

import pytest

from app.agent.runtime import AgentRuntime
from app.knowledge.config import DEFAULT_KNOWLEDGE_DIR
from evaluations import agent_contract
from evaluations.retrieval import corpus_manifest, local_index

CASES = [
    json.loads(line) for line in agent_contract.AGENT_DATASET.read_text().splitlines()
]


@pytest.fixture(scope="module")
def index():
    """Использовать реальный локальный индекс общих и продуктовых учебных статей."""
    documents, _ = corpus_manifest(DEFAULT_KNOWLEDGE_DIR)
    with local_index(documents) as value:
        yield value


@pytest.mark.parametrize("case", CASES, ids=[item["id"] for item in CASES])
def test_scripted_security_and_actual_mock_contract(index, case) -> None:
    """Исполнить размеченный сценарий через настоящий runtime и серверные проверки."""
    result = agent_contract.evaluate_agent_case(index, case)
    assert result["passed"], result


def test_suite_detects_disabled_citation_validation(index, monkeypatch) -> None:
    """Набор действительно краснеет, если сервер разрешил выдуманный источник."""

    def accept_all_sources(self, sources) -> None:
        """Смоделировать регрессию проверки источников без изменения рабочего кода."""

    monkeypatch.setattr(AgentRuntime, "_check_sources", accept_all_sources)
    case = next(item for item in CASES if item["scenario"] == "forged_citation")
    result = agent_contract.evaluate_agent_case(index, case)
    assert result["passed"] is False
    assert result["observed_error"] is None
    assert result["sources"] == ["KB-PASSWORD-RESET"]


def test_suite_detects_removed_tool_whitelist(index, monkeypatch) -> None:
    """Разрешённая по ошибке отправка письма не может пройти security-оценку."""

    def accept_dangerous_tool(context):
        """Смоделировать удалённый whitelist на границе инструментов."""

        def accept(name, arguments) -> tuple[dict, dict]:
            """Вернуть ложный успех запрещённого действия вместо серверного отказа."""
            return {}, {"ok": True, "data": {"sent": True}}

        return accept

    monkeypatch.setattr(agent_contract, "rejected_call_executor", accept_dangerous_tool)
    case = next(item for item in CASES if item["scenario"] == "injected_send")
    result = agent_contract.evaluate_agent_case(index, case)
    assert result["passed"] is False
    assert "unknown_tool" not in result["tool_errors"]


def test_unexpected_scenario_failure_is_visible_in_report(index, monkeypatch) -> None:
    """Ошибка кода сценария записывается как провал, не превращается в успех."""

    def broken_source(*args, **kwargs):
        """Прервать исполнение, имитируя ошибку адаптера данных."""
        raise AssertionError("fixture source is broken")

    monkeypatch.setattr(agent_contract.FixtureToolSource, "__call__", broken_source)
    result = agent_contract.evaluate_agent_case(index, CASES[0])
    assert result["passed"] is False
    assert result["observed_error"] == "AssertionError"
