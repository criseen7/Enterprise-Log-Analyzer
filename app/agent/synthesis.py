"""Síntesis: del bundle de evidencia a un `Diagnosis`.

Hay dos sintetizadores y el segundo no es un parche:

* **LLM** — redacta con la evidencia delante. Su salida se valida mecánicamente
  después (ver `citations.py`).
* **Determinista** — construye el diagnóstico con reglas sobre los campos del
  motor. Es el que se usa sin clave de API y el que sirve de control frente al
  modelo. Si el determinista ya acierta, el valor está en el motor.

Los dos producen el mismo contrato, así que el resto del bucle —validador,
traza, gate— no sabe cuál se usó. Solamente lo dice la traza.
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.contracts import Claim, Diagnosis, EvidenceBundle, EvidenceItem
from app.agent.llm import LLMClient, LLMResponse
from app.agent.promptfiles import load_rendered
from app.trust import clean_untrusted, untrusted_block

DEFAULT_MODEL = "claude-sonnet-4-5"

# Una línea de log es entrada no confiable: puede pedirle al modelo que ignore
# las reglas o cerrar el bloque donde vive. Se delimita, se limpian los tags y
# se trunca antes de que llegue al prompt; el recorte total evita que un log
# gigante desplace al resto de la evidencia.
MAX_SUMMARY_CHARS = 400
MAX_VALUE_CHARS = 400
MAX_LIST_ITEMS = 20
MAX_CONTEXT_CHARS = 24_000
EVIDENCE_OPEN = '<evidence trust="untrusted-log-data">'
EVIDENCE_CLOSE = "</evidence>"


def _clean(text: str, limit: int) -> str:
    return clean_untrusted(text, limit)


def _sanitize(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _clean(value, MAX_VALUE_CHARS)
    if isinstance(value, dict):
        if depth >= 3:
            return _clean(str(value), MAX_VALUE_CHARS)
        return {_clean(str(key), 80): _sanitize(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple):
        items = [_sanitize(item, depth + 1) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            items.append(f"…[{len(value) - MAX_LIST_ITEMS} elementos omitidos]")
        return items
    return value


def evidence_context(bundle: EvidenceBundle) -> str:
    """La evidencia tal como se le entrega al modelo: ids y hechos, sin SQL.

    Va dentro de un bloque explícitamente no confiable. El bloque no es sólo
    decoración: `_clean` neutraliza cualquier `</evidence>` que venga en un
    mensaje de log para que no pueda cerrarlo y escribir instrucciones fuera.
    """
    budget = MAX_CONTEXT_CHARS - len(EVIDENCE_OPEN) - len(EVIDENCE_CLOSE) - 2
    lines: list[str] = []
    omitted = 0
    for item in bundle.items:
        entry = {
            "evidence_id": item.evidence_id,
            "kind": item.kind,
            "summary": _clean(item.summary, MAX_SUMMARY_CHARS),
            "ts": _sanitize(item.ts),
            "fields": _sanitize(item.fields),
        }
        line = json.dumps(entry, ensure_ascii=False)
        if len(line) > budget:
            omitted += 1
            continue
        lines.append(line)
        budget -= len(line) + 1
    body = "\n".join(lines)
    if omitted:
        body += f"\n[{omitted} elementos de evidencia omitidos por tamaño]"
    return f"{EVIDENCE_OPEN}\n{body}\n{EVIDENCE_CLOSE}"


def _top_fingerprint(bundle: EvidenceBundle) -> EvidenceItem | None:
    fingerprints = [item for item in bundle.items if item.kind == "fingerprint"]
    if not fingerprints:
        return None
    return min(fingerprints, key=_rank)


def _rank(item: EvidenceItem) -> int:
    """Orden estable: las huellas sin rango van al final, no comparan con `None`."""
    value = item.fields.get("rank")
    return value if isinstance(value, int) else 999


def _comparison(bundle: EvidenceBundle, fingerprint: str) -> EvidenceItem | None:
    return bundle.by_id.get(f"cmp:{fingerprint}")


def _blast(bundle: EvidenceBundle, fingerprint: str) -> EvidenceItem | None:
    return bundle.by_id.get(f"blast:{fingerprint}")


def synthesize_fallback(
    symptom: str,
    bundle: EvidenceBundle,
    *,
    reason: str | None = None,
) -> Diagnosis:
    """Diagnóstico por reglas: sin modelo, sin red y reproducible bit a bit."""
    top = _top_fingerprint(bundle)
    gaps: list[str] = []

    if top is None:
        return Diagnosis(
            symptom=symptom,
            confidence="low",
            claims=[],
            evidence_gaps=[
                "no se encontró ninguna huella de error en la ventana analizada",
                *([reason] if reason else []),
            ],
            recommended_actions=["ampliar la ventana temporal o revisar la cobertura de parseo"],
        )

    fingerprint = top.evidence_id.removeprefix("fp:")
    claims: list[Claim] = [
        Claim(
            text=(
                f"La huella {fingerprint} concentra {top.fields.get('count', 0)} errores "
                f"({float(top.fields.get('share') or 0.0):.1%} del total de la ventana)"
            ),
            evidence_ids=[top.evidence_id],
            kind="fact",
        )
    ]

    category = top.fields.get("category")
    category = category if isinstance(category, str) else None
    comparison = _comparison(bundle, fingerprint)
    confidence = "low"
    root_cause: str | None = None
    if comparison is None:
        gaps.append("no se pudo comparar la ventana contra un baseline")
    else:
        verdict = comparison.fields.get("verdict")
        ratio = comparison.fields.get("ratio")
        z_score = comparison.fields.get("z_score")
        confidence = str(comparison.fields.get("confidence") or "low")
        claims.append(
            Claim(
                text=(
                    f"El volumen es «{verdict}» frente al baseline "
                    f"(ratio {ratio}, z-score {z_score})"
                ),
                evidence_ids=[comparison.evidence_id],
                kind="fact",
            )
        )
        if verdict in {"new", "elevated"}:
            root_cause = str(top.fields.get("sample_message") or fingerprint)

    blast = _blast(bundle, fingerprint)
    affected: list[str] = []
    if blast is None:
        gaps.append("no se pudo delimitar el alcance por servicio")
    else:
        affected = sorted(
            item.fields["service"]
            for item in bundle.items
            if item.kind == "service"
            and item.fields.get("fingerprint") == fingerprint
            and item.fields.get("service")
        )
        cascade = bool(blast.fields.get("is_cascade"))
        claims.append(
            Claim(
                text=(
                    f"El fallo afecta a {blast.fields.get('distinct_services', 0)} servicio(s) "
                    f"y {'hay' if cascade else 'no hay'} evidencia de cascada"
                ),
                evidence_ids=[blast.evidence_id, *[f"svc:{fingerprint}:{s}" for s in affected]],
                kind="fact",
            )
        )
        if cascade:
            gaps.append("el log no identifica cuál de los servicios falló primero en origen")

    if root_cause is None:
        gaps.append("el volumen no es nuevo ni está elevado: la evidencia no señala una causa raíz")
    if bundle.data_trust != "high":
        gaps.append(f"la confianza del dataset es {bundle.data_trust}")
    if reason:
        gaps.append(reason)

    actions = ["vigilar la huella en la próxima ventana"]
    if root_cause is not None:
        actions.insert(0, f"revisar el servicio que originó la huella {fingerprint}")

    return Diagnosis(
        symptom=symptom,
        root_cause=root_cause,
        root_cause_fingerprint=fingerprint if root_cause else None,
        claims=claims,
        affected_services=affected,
        root_cause_category=category,
        confidence=confidence,  # type: ignore[arg-type]
        evidence_gaps=gaps,
        recommended_actions=actions,
    )


def synthesize_llm(
    symptom: str,
    bundle: EvidenceBundle,
    *,
    llm: LLMClient,
    model: str = DEFAULT_MODEL,
    feedback: str | None = None,
) -> tuple[Diagnosis, LLMResponse]:
    """Pide el diagnóstico al modelo y lo valida contra el contrato."""
    prompt, _ = load_rendered(
        "synthesizer",
        "v1",
        symptom=symptom,
        evidence=evidence_context(bundle),
    )
    if feedback:
        prompt += "\n\nCorrige esto y vuelve a emitir el diagnóstico:\n"
        prompt += untrusted_block(
            "feedback", "untrusted-llm-derived", feedback, limit=MAX_CONTEXT_CHARS
        )
        prompt += "\n"

    response = llm.complete(
        [{"role": "user", "content": prompt}],
        model=model,
        max_tokens=3000,
        temperature=0.0,
        response_schema=Diagnosis,
    )
    if response.data is not None:
        diagnosis = Diagnosis.model_validate(response.data)
    else:
        # El cliente devuelve texto cuando no hay structured output; Pydantic
        # necesita `model_validate_json`, no `model_validate`, para parsearlo.
        diagnosis = Diagnosis.model_validate_json(response.text)
    return diagnosis, response


__all__ = [
    "DEFAULT_MODEL",
    "evidence_context",
    "synthesize_fallback",
    "synthesize_llm",
]
