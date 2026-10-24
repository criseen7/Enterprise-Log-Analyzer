"""Salida de consola para una investigación del agente.

Dos formatos y una regla: el texto legible no oculta nada que el JSON no diga.
Si el diagnóstico tiene tres huecos de evidencia, la consola los imprime; si
costó tokens, lo dice. Un informe que se ve limpio porque esconde lo que falta
es peor que un error.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from app.agent.contracts import AgentRun
from app.agent.llm import LLMUnavailable, build_client
from app.agent.loop import investigate

EXIT_OK = 0
EXIT_FAILED = 1


def run(
    symptom: str,
    *,
    model: str = "claude-sonnet-4-5",
    no_llm: bool = False,
    allow_mutations: bool = False,
    traces: str | None = None,
    as_json: bool = False,
) -> int:
    """Ejecuta la investigación y devuelve el código de salida del proceso."""
    llm = None
    if not no_llm:
        try:
            llm = build_client(model)
        except LLMUnavailable as exc:
            # Aviso, no error: sin clave el producto sigue funcionando con el
            # planner heurístico y el sintetizador determinista.
            print(f"aviso: {exc}; se usa el modo determinista", file=sys.stderr)

    agent_run = investigate(
        symptom,
        llm=llm,
        model=model,
        trace_dir=traces,
        allow_mutations=allow_mutations,
    )

    if as_json:
        print(agent_run.model_dump_json(indent=2))
    else:
        print(format_run(agent_run))
    return EXIT_OK if agent_run.status in {"OK", "NEEDS_APPROVAL"} else EXIT_FAILED


def format_run(agent_run: AgentRun) -> str:
    """Resumen legible: veredicto, evidencia citada, huecos y coste."""
    lines: list[str] = []
    diagnosis = agent_run.diagnosis
    lines.append(f"LogLens — {agent_run.symptom}")
    lines.append(f"run      : {agent_run.run_id}")
    lines.append(f"estado   : {agent_run.status}")
    if agent_run.error:
        lines.append(f"error    : {agent_run.error}")
    if diagnosis is None:
        lines.append("")
        lines.append("Sin diagnóstico: la corrida no llegó a sintetizar.")
        return "\n".join(lines)

    lines.append(f"confianza: {diagnosis.confidence}")
    if diagnosis.root_cause:
        lines.append(f"causa    : {diagnosis.root_cause}")
        lines.append(f"huella   : {diagnosis.root_cause_fingerprint}")
    else:
        lines.append("causa    : no determinada por la evidencia disponible")
    if diagnosis.affected_services:
        lines.append(f"servicios: {', '.join(diagnosis.affected_services)}")
    lines.append("")

    if diagnosis.claims:
        lines.append("Afirmaciones:")
        for claim in diagnosis.claims:
            cites = f" [{', '.join(claim.evidence_ids)}]" if claim.evidence_ids else ""
            lines.append(f"  · ({claim.kind}) {claim.text}{cites}")
    else:
        lines.append("Afirmaciones: ninguna (no hay evidencia que las sostenga)")

    if diagnosis.recommended_actions:
        lines.append("")
        lines.append("Siguiente paso:")
        for action in diagnosis.recommended_actions:
            lines.append(f"  → {action}")

    if diagnosis.evidence_gaps:
        lines.append("")
        lines.append("Huecos de evidencia:")
        for gap in diagnosis.evidence_gaps:
            lines.append(f"  ! {gap}")

    citations = agent_run.citations
    if citations is not None:
        lines.append("")
        lines.append(
            "Citas: "
            f"{len(citations.valid)} válidas, "
            f"{len(citations.invalid)} inventadas (eliminadas), "
            f"{len(citations.uncited)} sin cita"
        )

    lines.append("")
    lines.append(
        f"Coste   : ${agent_run.cost_usd:.6f} "
        f"({agent_run.tokens_in} in / {agent_run.tokens_out} out, "
        f"{agent_run.duration_ms} ms)"
    )
    lines.append(f"Traza   : {agent_run.trace_path}")

    if agent_run.pending_action:
        pending: dict[str, Any] = agent_run.pending_action.get("pending_action") or {}
        lines.append("")
        lines.append("ACCIÓN PENDIENTE DE APROBACIÓN HUMANA")
        lines.append(f"  action_id: {pending.get('action_id')}")
        lines.append(f"  comando  : {pending.get('approval_command')}")

    return "\n".join(lines)


def dumps(agent_run: AgentRun) -> str:
    """JSON estable para scripts."""
    return json.dumps(agent_run.model_dump(), ensure_ascii=False, indent=2, default=str)


__all__ = ["EXIT_FAILED", "EXIT_OK", "dumps", "format_run", "run"]
