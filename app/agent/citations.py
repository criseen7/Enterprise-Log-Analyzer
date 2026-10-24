"""Validación mecánica de citas: el prompt no es la garantía, esto sí.

El sintetizador puede escribir «el servicio `payments` estuvo caído 40 minutos y
la causa fue el pool de conexiones» con total seguridad. La pregunta no es si
suena bien, sino si cada afirmación tiene detrás un `evidence_id` que exista en
el bundle.

Tres categorías y tres destinos:

* **válida** — todos sus ids existen. Se queda.
* **inválida** — cita al menos un id que no existe. Es una alucinación: se
  elimina del diagnóstico final y se cuenta.
* **sin cita** — es un hecho sin ningún id. No se elimina sin más: se degrada a
  `hypothesis`, porque la frase puede seguir siendo útil como conjetura
  explícita.

Las recomendaciones no necesitan evidencia: recomendar «revisa el pool» no es
afirmar un hecho. Las hipótesis tampoco.

Si más del 30 % de los hechos fallan, el bucle re-sintetiza una vez con el error
concreto como feedback; si vuelve a fallar, entrega lo que quedó y lo declara en
`evidence_gaps`.
"""

from __future__ import annotations

from app.agent.contracts import CitationReport, Claim, Diagnosis, EvidenceBundle

# Umbral de fallo de hechos a partir del cual merece la pena re-sintetizar.
RETRY_THRESHOLD = 0.30


def validate(diagnosis: Diagnosis, bundle: EvidenceBundle) -> CitationReport:
    """Clasifica cada claim contra los ids que existen."""
    report = CitationReport()
    for claim in diagnosis.claims:
        if claim.kind == "recommendation":
            report.valid.append(claim)
            continue
        if not claim.evidence_ids:
            report.uncited.append(claim)
            continue
        unknown = [
            evidence_id for evidence_id in claim.evidence_ids if evidence_id not in bundle.by_id
        ]
        if unknown:
            report.invalid.append(claim)
        else:
            report.valid.append(claim)
    return report


def apply(diagnosis: Diagnosis, report: CitationReport) -> Diagnosis:
    """Devuelve el diagnóstico sin claims alucinadas y con los huecos declarados."""
    kept: list[Claim] = []
    for claim in report.valid:
        kept.append(claim)
    for claim in report.uncited:
        if claim.kind == "fact":
            kept.append(
                claim.model_copy(
                    update={
                        "kind": "hypothesis",
                        "text": f"{claim.text} (sin evidencia que lo sustente)",
                    }
                )
            )
        else:
            kept.append(claim)

    gaps = list(diagnosis.evidence_gaps)
    if report.invalid:
        ids = sorted({eid for claim in report.invalid for eid in claim.evidence_ids})
        gaps.append(
            f"se eliminaron {len(report.invalid)} afirmaciones con evidencia inexistente "
            f"({', '.join(ids)}): no existen en este bundle"
        )
    if any(claim.kind == "fact" for claim in report.uncited):
        count = sum(1 for claim in report.uncited if claim.kind == "fact")
        gaps.append(f"{count} afirmaciones sin cita se degradaron a hipótesis")

    return diagnosis.model_copy(update={"claims": kept, "evidence_gaps": gaps})


def feedback_prompt(report: CitationReport) -> str:
    """Texto para el reintento: los ids concretos que no existen, no un reproche."""
    lines = ["El diagnóstico anterior citó evidencia que no existe. Corrige o elimina:"]
    for claim in report.invalid:
        lines.append(f"- «{claim.text}» cita {', '.join(claim.evidence_ids)}")
    for claim in report.uncited:
        if claim.kind == "fact":
            lines.append(
                f"- «{claim.text}» no cita nada; si es un hecho, cita o conviértelo en hipótesis"
            )
    lines.append("Usa sólo ids que aparezcan en la evidencia entregada.")
    return "\n".join(lines)


def should_retry(report: CitationReport) -> bool:
    """Re-sintetizar sólo si el fallo es de hechos y supera el umbral."""
    return bool(report.fact_claims) and report.fact_failure_rate > RETRY_THRESHOLD


__all__ = ["RETRY_THRESHOLD", "apply", "feedback_prompt", "should_retry", "validate"]
