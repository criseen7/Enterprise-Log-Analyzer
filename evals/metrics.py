"""Métricas del harness. Funciones puras sobre resultados serializables.

Dos decisiones que se ven en los números:

* La unidad de una métrica de citas es la **claim**, no el caso. Un caso con
  diez claims y una inventada no es lo mismo que un caso con una y ninguna.
* `retrieval_recall@5` se calcula con el motor y sin LLM. Es la única métrica
  que responde "¿el sistema sabe la respuesta?" en vez de "¿el agente sabe
  decirla?".
"""

from __future__ import annotations

from typing import Any

METRIC_ORDER = (
    "root_cause_accuracy",
    "category_accuracy",
    "evidence_citation_rate",
    "hallucination_rate",
    "false_attribution_rate",
    "tool_call_efficiency",
    "token_cost_usd",
    "retrieval_recall_at_5",
    "misunderstood_rate",
)

# Metas de docs/evals.md §4.1. Se usan para `--fail-under` cuando se pasa una
# métrica sin umbral explícito, y para pintar el resultado en la tabla.
TARGETS: dict[str, tuple[str, float]] = {
    "root_cause_accuracy": (">=", 0.80),
    "category_accuracy": (">=", 0.75),
    "evidence_citation_rate": (">=", 0.95),
    "hallucination_rate": ("<=", 0.05),
    "false_attribution_rate": ("<=", 0.10),
    "tool_call_efficiency": ("<=", 8.0),
    "token_cost_usd": ("<=", 0.05),
    "retrieval_recall_at_5": (">=", 0.90),
    "misunderstood_rate": ("<=", 0.10),
}


def case_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Extrae de un resultado de caso los campos que agregan las métricas."""
    diagnosis = result.get("diagnosis") or {}
    plan = result.get("plan") or {}
    citations = result.get("citations") or {}
    claims = diagnosis.get("claims") or []
    forbidden = result.get("must_not_claim") or []

    # Las recomendaciones no necesitan cita y no son afirmaciones sobre el
    # sistema: incluirlas en el denominador de `evidence_citation_rate`
    # inflaría la métrica con texto que no se está evaluando.
    fact_claims = [
        claim
        for key in ("valid", "invalid", "uncited")
        for claim in citations.get(key, [])
        if claim.get("kind") == "fact"
    ]
    valid_facts = [claim for claim in citations.get("valid", []) if claim.get("kind") == "fact"]

    attributed = [
        phrase
        for phrase in forbidden
        if any(
            phrase.lower() in str(claim.get("text") or "").lower()
            for claim in claims
            if claim.get("kind") != "recommendation"
        )
    ]

    restatement = str(plan.get("restatement") or diagnosis.get("symptom") or "").lower()
    service = str(result.get("root_cause_service") or "").lower()
    onset = str(result.get("onset") or "")
    hour = onset[11:13]
    misunderstood = bool(service) and service not in restatement and hour not in restatement

    return {
        "root_cause_ok": diagnosis.get("root_cause_fingerprint")
        == result.get("root_cause_fingerprint"),
        "category_ok": diagnosis.get("root_cause_category") == result.get("root_cause_category"),
        "facts": len(fact_claims),
        "fact_valid": len(valid_facts),
        "invalid_claims": len(citations.get("invalid", [])),
        "all_claims": len(claims),
        "false_attribution": bool(attributed),
        "attributed_phrases": attributed,
        "tool_calls": int(result.get("tool_calls") or 0),
        "cost_usd": float(result.get("cost_usd") or 0.0),
        "retrieval_hit": bool(result.get("retrieval_hit")),
        "misunderstood": misunderstood,
        "evaluated": result.get("status") in {"OK", "BLOCKED", "NEEDS_APPROVAL"},
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Agrega una lista de resultados y devuelve métricas + denominadores."""
    rows = [case_metrics(result) for result in results]
    evaluated = [row for row in rows if row["evaluated"]]
    cases = len(evaluated)
    facts = sum(row["facts"] for row in evaluated)
    valid_facts = sum(row["fact_valid"] for row in evaluated)
    all_claims = sum(row["all_claims"] for row in evaluated)
    invalid = sum(row["invalid_claims"] for row in evaluated)

    def rate(hits: int, total: int) -> float:
        return round(hits / total, 4) if total else 0.0

    metrics: dict[str, Any] = {
        "cases": len(rows),
        "evaluated": cases,
        "root_cause_accuracy": rate(sum(row["root_cause_ok"] for row in evaluated), cases),
        "category_accuracy": rate(sum(row["category_ok"] for row in evaluated), cases),
        "evidence_citation_rate": rate(valid_facts, facts),
        "hallucination_rate": rate(invalid, all_claims),
        "false_attribution_rate": rate(sum(row["false_attribution"] for row in evaluated), cases),
        "tool_call_efficiency": (
            round(sum(row["tool_calls"] for row in evaluated) / cases, 2) if cases else 0.0
        ),
        "token_cost_usd": (
            round(sum(row["cost_usd"] for row in evaluated) / cases, 6) if cases else 0.0
        ),
        "retrieval_recall_at_5": rate(sum(row["retrieval_hit"] for row in evaluated), cases),
        "misunderstood_rate": rate(sum(row["misunderstood"] for row in evaluated), cases),
    }
    metrics["targets_met"] = targets_met(metrics)
    return metrics


def targets_met(metrics: dict[str, Any]) -> dict[str, bool]:
    met: dict[str, bool] = {}
    for name, (op, threshold) in TARGETS.items():
        value = metrics.get(name)
        if value is None:
            continue
        met[name] = value >= threshold if op == ">=" else value <= threshold
    return met


def check_thresholds(metrics: dict[str, Any], thresholds: dict[str, float]) -> list[str]:
    """`--fail-under metrica=valor`. Devuelve las métricas que incumplen."""
    failed: list[str] = []
    for name, threshold in thresholds.items():
        value = float(metrics.get(name) or 0.0)
        op = TARGETS.get(name, (">=", 0.0))[0]
        if (op == ">=" and value < threshold) or (op == "<=" and value > threshold):
            failed.append(f"{name}={value:.4f} (umbral {op} {threshold})")
    return failed


def determinism_regression(reference: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Compara `retrieval_recall@5` entre dos corridas.

    La tesis dice que cambiar de modelo no cambia lo que detecta el motor. Si
    esta diferencia no es cero, la tesis es falsa y hay que publicarlo.
    """
    left = float(reference.get("retrieval_recall_at_5") or 0.0)
    right = float(other.get("retrieval_recall_at_5") or 0.0)
    return {
        "reference": left,
        "other": right,
        "delta": round(right - left, 6),
        "identical": abs(right - left) < 1e-9,
    }


def format_table(metrics: dict[str, Any]) -> str:
    lines = ["| Métrica | Valor | Meta |", "|---|---|---|"]
    for name in METRIC_ORDER:
        value = metrics.get(name, 0.0)
        op, threshold = TARGETS[name]
        symbol = "✅" if metrics.get("targets_met", {}).get(name) else "❌"
        lines.append(f"| `{name}` | {value} | {op} {threshold} {symbol} |")
    return "\n".join(lines)


__all__ = [
    "METRIC_ORDER",
    "TARGETS",
    "aggregate",
    "case_metrics",
    "check_thresholds",
    "determinism_regression",
    "format_table",
    "targets_met",
]
