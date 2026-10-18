"""Проверки метрик, разметки и CLI оценок; службы и ключи не нужны."""

import copy
import json
import socket

import pytest

from app.knowledge.config import DEFAULT_KNOWLEDGE_DIR
from app.knowledge.index import KnowledgeIndex
from evaluations import run
from evaluations.retrieval import (
    DEFAULT_DATASET,
    RetrievalCase,
    corpus_manifest,
    load_cases,
    ranking_metrics,
)


@pytest.fixture(scope="module")
def report() -> dict:
    """Измерить один неизменённый набор на настоящем Qdrant в памяти."""
    return run.run_evaluations()


def test_dataset_covers_all_articles_products_and_non_answers() -> None:
    """Набор включает все семь статей, общие инструкции и вопросы без ответа."""
    documents, manifest = corpus_manifest(DEFAULT_KNOWLEDGE_DIR)
    cases = load_cases(DEFAULT_DATASET, documents)
    assert len(cases) == 32
    assert sum(case.kind == "no_hit" for case in cases) == 6
    assert {key for case in cases for key in case.expected_articles} == {
        item["document_key"] for item in manifest
    }
    assert {case.product_code for case in cases} == {
        "techflow-server",
        "techflow-desktop",
        "other-product",
    }


def test_recall_and_mrr_have_distinct_denominators() -> None:
    """Два релевантных источника не превращают первый найденный в полный recall."""
    assert ranking_metrics(["A", "B"], ["X", "B", "Y"], 5) == {
        "recall": 0.5,
        "reciprocal_rank": 0.5,
    }
    assert ranking_metrics(["A"], ["B", "A"], 1) == {
        "recall": 0,
        "reciprocal_rank": 0,
    }
    assert ranking_metrics([], [], 5)["recall"] is None


def test_report_preserves_known_partial_recall_and_real_llm_limits(report) -> None:
    """Неполный ответ R25 остаётся промахом; качество реальной модели не выдумано."""
    assert report["passed"] is True
    assert report["dataset"]["total_cases"] == 44
    assert report["retrieval"]["recall_at_5"] < 1
    combined = next(
        item for item in report["retrieval"]["cases"] if item["id"] == "R25"
    )
    assert combined["expected_articles"] == ["KB-BACKUP-RESTORE", "KB-SERVER-LOGS"]
    assert combined["at_5"]["recall"] == 0.5
    assert all(value is None for value in report["not_measured"].values())
    assert "R25" in run.readable_summary(report)
    json.dumps(report, allow_nan=False)


def test_cli_writes_json_and_ignores_external_provider_environment(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Переменные платной модели и внешних служб не меняют автономный запуск."""
    for name, value in {
        "LLM_PROVIDER": "openai",
        "EMBEDDING_PROVIDER": "openai",
        "OPENAI_API_KEY": "must-never-be-used",
        "OPENAI_MODEL": "must-never-be-called",
        "DATABASE_URL": "postgresql://forbidden",
        "QDRANT_URL": "https://forbidden",
    }.items():
        monkeypatch.setenv(name, value)

    def forbid_network(*args, **kwargs) -> None:
        """Превратить случайный внешний запрос в явный провал теста."""
        raise AssertionError("offline evaluation attempted network")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    target = tmp_path / "reports" / "evaluations.json"
    assert run.main(["--output", str(target)]) == 0
    serialized = target.read_text(encoding="utf-8")
    assert "must-never-be-used" not in serialized
    assert json.loads(serialized)["environment"]["real_llm"] is False
    assert "PASS" in capsys.readouterr().out


def test_cli_exits_one_and_writes_report_when_search_regresses(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Потеря найденных статей реально делает команду CI неуспешной."""

    def empty_search(self, *args, **kwargs) -> list:
        """Смоделировать регрессию индекса, возвращающего пустую выдачу всегда."""
        return []

    monkeypatch.setattr(KnowledgeIndex, "search", empty_search)
    target = tmp_path / "regression.json"
    assert run.main(["--output", str(target)]) == 1
    failed = json.loads(target.read_text(encoding="utf-8"))
    assert failed["passed"] is False
    assert failed["retrieval"]["recall_at_5"] == 0
    assert failed["agent_contract"]["passed_count"] < 12
    assert "FAILED recall_at_5" in capsys.readouterr().out


@pytest.mark.parametrize(
    "metric,value",
    [
        ("recall_at_5", 0.749),
        ("mrr_at_5", 0.649),
        ("no_hit_accuracy", 0.799),
        ("scope_violations", 1),
    ],
)
def test_each_retrieval_threshold_can_fail_independently(report, metric, value) -> None:
    """Ни одна метрика не служит декоративным полем без влияния на результат CI."""
    retrieval = copy.deepcopy(report["retrieval"])
    retrieval[metric] = value
    gates = run.evaluate_gates(
        retrieval, report["agent_contract"], report["configuration"]["thresholds"]
    )
    assert next(gate for gate in gates if gate["metric"] == metric)["passed"] is False


def test_agent_threshold_rejects_even_one_security_failure(report) -> None:
    """Для контракта безопасности нет допустимого процента провалов."""
    agent = {"pass_rate": 11 / 12}
    gates = run.evaluate_gates(
        report["retrieval"], agent, report["configuration"]["thresholds"]
    )
    assert gates[-1]["passed"] is False


@pytest.mark.parametrize(
    "update",
    [
        {"expected_articles": []},
        {"expected_articles": ["KB-UNKNOWN"]},
        {"product_code": "other-product"},
    ],
)
def test_invalid_labels_fail_before_any_search(tmp_path, update) -> None:
    """Нельзя принять неизвестную статью или серверную статью для чужого продукта."""
    documents, _ = corpus_manifest(DEFAULT_KNOWLEDGE_DIR)
    lines = DEFAULT_DATASET.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0]) | update
    lines[0] = json.dumps(first)
    target = tmp_path / "cases.jsonl"
    target.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(target, documents)


def test_duplicate_case_ids_are_rejected(tmp_path) -> None:
    """Повтор вопроса не должен незаметно увеличивать его вес в средних метриках."""
    documents, _ = corpus_manifest(DEFAULT_KNOWLEDGE_DIR)
    target = tmp_path / "cases.jsonl"
    text = DEFAULT_DATASET.read_text(encoding="utf-8")
    target.write_text(text + text.splitlines()[0] + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="уникальными"):
        load_cases(target, documents)


def test_cli_invalid_dataset_returns_two(tmp_path, capsys) -> None:
    """Ошибка входных данных отличается от измеренной регрессии поиска."""
    assert run.main(["--dataset", str(tmp_path / "missing.jsonl")]) == 2
    assert "invalid input" in capsys.readouterr().err


def test_positive_label_cannot_duplicate_expected_article() -> None:
    """Каждая релевантная статья учитывается один раз в знаменателе recall."""
    with pytest.raises(ValueError, match="повтор источника"):
        RetrievalCase(
            id="duplicate",
            product_code="techflow-server",
            query="сервер",
            expected_articles=["KB-SERVER-START", "KB-SERVER-START"],
            kind="positive",
            rationale="Ошибка разметки",
        )
