"""Validación mecánica de citas: el prompt no es la garantía, esto sí.

El sintetizador puede escribir «el servicio `payments` estuvo caído 40 minutos y
la causa fue el pool de conexiones» con total seguridad. La pregunta no es si
suena bien, sino si cada afirmación tiene detrás un `evidence_id` que exista en
el bundle.

Cuatro categorías y cuatro destinos:

* **válida** — todos sus ids existen y sus cifras aparecen en esa evidencia. Se
  queda.
* **inválida** — cita al menos un id que no existe. Es una alucinación: se
  elimina del diagnóstico final y se cuenta.
* **sin cita** — es un hecho sin ningún id. No se elimina sin más: se degrada a
  `hypothesis`, porque la frase puede seguir siendo útil como conjetura
  explícita.
* **no sustentada** — los ids existen, pero la claim afirma una cifra que no
  aparece en la evidencia citada. Una cita válida sólo prueba existencia; esto
  comprueba que el número no lo haya inventado el modelo. También se elimina.

Las recomendaciones no necesitan evidencia: recomendar «revisa el pool» no es
afirmar un hecho. Las hipótesis tampoco.

Si más del 30 % de los hechos fallan, el bucle re-sintetiza una vez con el error
concreto como feedback; si vuelve a fallar, entrega lo que quedó y lo declara en
`evidence_gaps`.
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.contracts import (
    CitationReport,
    Claim,
    Diagnosis,
    EvidenceBundle,
    EvidenceItem,
)
from app.trust import clean_untrusted

# Umbral de fallo de hechos a partir del cual merece la pena re-sintetizar.
RETRY_THRESHOLD = 0.30
# El feedback vuelve al prompt: una claim larga no puede desplazar a la evidencia.
MAX_FEEDBACK_CLAIM_CHARS = 400

# Las cifras de una claim tienen que existir en la evidencia citada. Es una
# comprobación de soporte numérico, no de significado: no puede demostrar que
# «el pool se agotó» se deduce de un contador, pero sí impide que el modelo
# invente el contador. Los porcentajes se comparan también contra fracciones
# (37 % ↔ 0.37) para no castigar la misma cifra escrita de otra forma.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_MAX_NUMBER_CHARS = 40


def _to_float(raw: str) -> float | None:
    if len(raw) > _MAX_NUMBER_CHARS:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _numbers(text: str) -> list[tuple[float, bool]]:
    """Números del texto y si van seguidos de `%` (ignorando espacios)."""
    found: list[tuple[float, bool]] = []
    for match in _NUMBER.finditer(text):
        value = _to_float(match.group(0))
        if value is None:
            continue
        rest = text[match.end() :]
        percent = rest.lstrip()[:1] == "%"
        found.append((value, percent))
    return found


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= max(abs(right), 1.0) * 0.01


def _collect(value: Any, into: set[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        number = float(value)
        into.add(number)
        if 0 < number < 1:
            into.add(number * 100)
        return
    if isinstance(value, str):
        for number, _ in _numbers(value):
            into.add(number)
            if 0 < number < 1:
                into.add(number * 100)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect(item, into)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _collect(item, into)


def _evidence_numbers(items: list[EvidenceItem]) -> set[float]:
    into: set[float] = set()
    for item in items:
        _collect(item.evidence_id, into)
        _collect(item.summary, into)
        _collect(item.ts, into)
        _collect(item.fields, into)
    return into


def _numbers_supported(claim: Claim, items: list[EvidenceItem]) -> bool:
    """Un hecho con cifras sólo es válido si todas aparecen en su evidencia."""
    numbers = _numbers(claim.text)
    if not numbers:
        return True
    corpus = _evidence_numbers(items)
    for value, percent in numbers:
        candidates = [value, value / 100] if percent else [value]
        if not any(_close(candidate, known) for candidate in candidates for known in corpus):
            return False
    return True


def validate(diagnosis: Diagnosis, bundle: EvidenceBundle) -> CitationReport:
    """Clasifica cada claim contra los ids que existen y sus cifras."""
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
            continue
        cited = [bundle.by_id[evidence_id] for evidence_id in claim.evidence_ids]
        if claim.kind == "fact" and not _numbers_supported(claim, cited):
            report.unsupported.append(claim)
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
    if report.unsupported:
        count = len(report.unsupported)
        gaps.append(
            f"se eliminaron {count} afirmaciones con cifras que no aparecen en la evidencia citada"
        )
    if any(claim.kind == "fact" for claim in report.uncited):
        count = sum(1 for claim in report.uncited if claim.kind == "fact")
        gaps.append(f"{count} afirmaciones sin cita se degradaron a hipótesis")

    return diagnosis.model_copy(update={"claims": kept, "evidence_gaps": gaps})


def feedback_prompt(report: CitationReport) -> str:
    """Texto para el reintento: los ids concretos que fallan, no un reproche."""
    lines = ["El diagnóstico anterior no está sustentado por la evidencia. Corrige o elimina:"]

    def text(claim: Claim) -> str:
        return clean_untrusted(claim.text, MAX_FEEDBACK_CLAIM_CHARS)

    for claim in report.invalid:
        lines.append(f"- «{text(claim)}» cita {', '.join(claim.evidence_ids)}")
    for claim in report.unsupported:
        lines.append(f"- «{text(claim)}» cita evidencia real, pero sus cifras no aparecen en ella")
    for claim in report.uncited:
        if claim.kind == "fact":
            lines.append(
                f"- «{text(claim)}» no cita nada; si es un hecho, cita o conviértelo en hipótesis"
            )
    lines.append("Usa sólo ids que aparezcan en la evidencia entregada.")
    return "\n".join(lines)


def should_retry(report: CitationReport) -> bool:
    """Re-sintetizar sólo si el fallo es de hechos y supera el umbral."""
    return bool(report.fact_claims) and report.fact_failure_rate > RETRY_THRESHOLD


__all__ = ["RETRY_THRESHOLD", "apply", "feedback_prompt", "should_retry", "validate"]
