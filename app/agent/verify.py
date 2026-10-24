"""Chequeos deterministas entre etapas: el plan no avanza si los datos no dan.

Estos cuatro chequeos son la diferencia entre una demo y una herramienta. Un
agente que diagnostica sobre un dataset con el 40 % de las líneas sin parsear no
está diagnosticando: está describiendo una parte arbitraria del problema y
presentándola como el todo.

Ninguno llama a un modelo. Todos son aritmética sobre el bundle de evidencia, y
por eso se pueden testear con casos fijos.
"""

from __future__ import annotations

from app.agent.contracts import EvidenceBundle, Plan, VerifyResult

# Tipos de evidencia que describen el incidente. `health` e `incident` solos no
# bastan: uno describe el dataset y el otro un registro previo, no lo que está
# pasando en la ventana.
INCIDENT_KINDS = {"event", "fingerprint", "anomaly", "comparison", "service"}


def after_execution(plan: Plan, bundle: EvidenceBundle) -> VerifyResult:
    """Decide si se puede sintetizar, si hay que re-planificar o si se aborta."""
    checks = {
        "trust_sufficient": bundle.data_trust != "low",
        "has_evidence": bool(bundle.items),
        "has_incident_evidence": any(item.kind in INCIDENT_KINDS for item in bundle.items),
        "hypothesis_current": not _stale_hypothesis(plan, bundle),
    }

    if not checks["has_evidence"]:
        return VerifyResult(
            action="abort",
            reason="el bundle está vacío: no hay nada que citar",
            checks=checks,
        )
    if not checks["trust_sufficient"]:
        health = bundle.by_id.get("sys:health")
        coverage = ""
        if health is not None:
            ratio = health.fields.get("parse_coverage")
            if isinstance(ratio, int | float):
                coverage = f" (cobertura de parseo {ratio:.1%})"
        return VerifyResult(
            action="abort",
            reason=f"los datos no son fiables{coverage}: no se puede diagnosticar",
            checks=checks,
        )
    if not checks["has_incident_evidence"]:
        return VerifyResult(
            action="replan",
            reason="la ventana no tiene eventos de incidente; se amplía la búsqueda",
            checks=checks,
        )
    if not checks["hypothesis_current"]:
        return VerifyResult(
            action="replan",
            reason="el plan consultó una huella que el motor no encontró; se replantea",
            checks=checks,
        )
    return VerifyResult(action="continue", reason="la evidencia permite sintetizar", checks=checks)


def _stale_hypothesis(plan: Plan, bundle: EvidenceBundle) -> bool:
    """True si el plan fijó a mano un fingerprint que no aparece en la evidencia.

    El plan heurístico usa el placeholder `$top_fingerprint` y nunca puede caer
    aquí. Un plan del modelo que invente una huella sí: es exactamente el fallo
    que se quiere detectar antes de gastar una síntesis.
    """
    known = {
        item.evidence_id.removeprefix("fp:") for item in bundle.items if item.kind == "fingerprint"
    }
    for step in plan.steps:
        value = step.params.get("fingerprint")
        if not isinstance(value, str) or value.startswith("$"):
            continue
        if value not in known:
            return True
    return False


__all__ = ["INCIDENT_KINDS", "after_execution"]
