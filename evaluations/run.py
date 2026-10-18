"""CLI автономных оценок: JSON-отчёт, краткий итог и ненулевой код при регрессии."""

import argparse
import json
import platform
import sys
from pathlib import Path

from app.knowledge.config import DEFAULT_KNOWLEDGE_DIR
from app.knowledge.errors import KnowledgeError
from evaluations.agent_contract import AGENT_DATASET, evaluate_agent_contract
from evaluations.retrieval import (
    CUTOFF,
    DEFAULT_DATASET,
    DIMENSIONS,
    MIN_SCORE,
    corpus_manifest,
    dataset_sha256,
    evaluate_retrieval,
    load_cases,
    local_index,
)

THRESHOLDS_PATH = Path(__file__).with_name("thresholds.json")


def evaluate_gates(retrieval: dict, agent: dict, thresholds: dict) -> list[dict]:
    """Сравнить метрики с порогами, зафиксированными до первого измерения."""
    checks = [
        ("recall_at_5", retrieval["recall_at_5"], ">=", thresholds["recall_at_5_min"]),
        ("mrr_at_5", retrieval["mrr_at_5"], ">=", thresholds["mrr_at_5_min"]),
        (
            "no_hit_accuracy",
            retrieval["no_hit_accuracy"],
            ">=",
            thresholds["no_hit_accuracy_min"],
        ),
        (
            "scope_violations",
            retrieval["scope_violations"],
            "<=",
            thresholds["scope_violations_max"],
        ),
        (
            "agent_contract_pass_rate",
            agent["pass_rate"],
            ">=",
            thresholds["agent_contract_pass_rate_min"],
        ),
    ]
    return [
        {
            "metric": name,
            "value": value,
            "operator": operator,
            "threshold": threshold,
            "passed": value >= threshold if operator == ">=" else value <= threshold,
        }
        for name, value, operator, threshold in checks
    ]


def run_evaluations(
    dataset: Path = DEFAULT_DATASET,
    knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR,
) -> dict:
    """Построить отчёт без чтения .env, API-ключей, PostgreSQL и сетевых сервисов."""
    documents, manifest = corpus_manifest(knowledge_dir)
    cases = load_cases(dataset, documents)
    thresholds = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    with local_index(documents) as index:
        retrieval = evaluate_retrieval(index, cases)
        agent = evaluate_agent_contract(index)
    gates = evaluate_gates(retrieval, agent, thresholds)
    return {
        "schema_version": 1,
        "passed": all(gate["passed"] for gate in gates),
        "environment": {
            "python": platform.python_version(),
            "network": False,
            "postgresql": False,
            "real_llm": False,
        },
        "dataset": {
            "retrieval_sha256": dataset_sha256(dataset),
            "agent_sha256": dataset_sha256(AGENT_DATASET),
            "total_cases": len(cases) + agent["case_count"],
            "corpus": manifest,
        },
        "configuration": {
            "cutoff": CUTOFF,
            "min_score": MIN_SCORE,
            "embedder": "mock-hash-v1",
            "dimensions": DIMENSIONS,
            "thresholds": thresholds,
        },
        "retrieval": retrieval,
        "agent_contract": agent,
        "gates": gates,
        "not_measured": {
            "real_llm_answer_correctness": None,
            "real_llm_task_success": None,
            "real_llm_cost_usd": None,
            "real_llm_latency_seconds": None,
            "llm_judge": None,
        },
        "limitations": [
            "MockEmbedder измеряет лексические совпадения и коллизии хеша, не смысл.",
            "Разметка содержит вымышленные вопросы к семи учебным статьям.",
            "Набор создан по известным статьям; это не независимый production holdout.",
            "MockAgentProvider и сценарные ответы не измеряют качество реальной LLM.",
            "Опасные вызовы проверяют серверный контракт, "
            "не иммунитет LLM к injection.",
            "Автономный набор не заменяет интеграционные тесты PostgreSQL и HTTP API.",
        ],
    }


def readable_summary(report: dict) -> str:
    """Показать основные метрики и все нарушения порогов без сокрытия промахов."""
    retrieval = report["retrieval"]
    agent = report["agent_contract"]
    lines = [
        f"Offline evaluations: {'PASS' if report['passed'] else 'FAIL'} "
        f"({report['dataset']['total_cases']} scenarios; no real LLM)",
        f"Retrieval: Recall@5={retrieval['recall_at_5']:.4f}, "
        f"MRR@5={retrieval['mrr_at_5']:.4f}, "
        f"no-hit accuracy={retrieval['no_hit_accuracy']:.4f}, "
        f"scope violations={retrieval['scope_violations']}",
        f"Agent contract: {agent['passed_count']}/{agent['case_count']} passed",
        "Real LLM answer correctness/task success/cost: NOT MEASURED",
    ]
    for gate in report["gates"]:
        if not gate["passed"]:
            lines.append(
                f"FAILED {gate['metric']}: {gate['value']:.4f} "
                f"{gate['operator']} {gate['threshold']}"
            )
    misses = [
        item["id"]
        for item in retrieval["cases"]
        if item["kind"] == "positive"
        and item["at_5"]["recall"] < 1
        or item["kind"] == "no_hit"
        and not item["no_hit_correct"]
    ]
    if misses:
        lines.append("Retrieval misses (preserved in JSON): " + ", ".join(misses))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Записать JSON и вернуть 0 при успехе, 1 при регрессии, 2 при неверном наборе."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/evaluations.json"))
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--knowledge-dir", type=Path, default=DEFAULT_KNOWLEDGE_DIR)
    arguments = parser.parse_args(argv)
    try:
        report = run_evaluations(arguments.dataset, arguments.knowledge_dir)
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError, KnowledgeError) as error:
        print(f"Offline evaluations: invalid input: {error}", file=sys.stderr)
        return 2
    print(readable_summary(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
