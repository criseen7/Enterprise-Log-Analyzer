"""El bucle del agente: síntoma → plan → evidencia → veredicto → diagnóstico.

Siete etapas y cuatro puntos de control deterministas. Nada de grafos, memoria
vectorial ni planners recursivos: con diez tools y un motor que ya calcula, la
complejidad del orquestador sólo añade formas de fallar.

El invariante que sostiene todo: **el modelo no toca la base de datos**. Recibe
evidencia con ids y devuelve un diagnóstico con citas. Entre las dos cosas hay
un validador que comprueba, sin interpretar el texto, que cada id existe.

Sin `llm`, el bucle funciona igual: plan heurístico y síntesis determinista. Es
el modo sin clave de API y el control de las ablaciones.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agent import citations
from app.agent.contracts import (
    AgentRun,
    Diagnosis,
    EvidenceBundle,
    Plan,
)
from app.agent.executor import call_tool, execute_plan, seed_health
from app.agent.llm import LLMClient, LLMUnavailable
from app.agent.planner import DEFAULT_MODEL, heuristic_plan, plan_with_fallback
from app.agent.synthesis import synthesize_fallback, synthesize_llm
from app.agent.trace import TraceRecorder, record_run
from app.agent.verify import after_execution

HEURISTIC_PROMPT = "heuristic"


def _new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


def _blocked_diagnosis(symptom: str, reason: str) -> Diagnosis:
    return Diagnosis(
        symptom=symptom,
        confidence="low",
        claims=[],
        evidence_gaps=[reason],
        recommended_actions=[
            "corregir la cobertura de parseo antes de diagnosticar",
            "revisar `system_health` y los patrones de `error_log` que fallan",
        ],
    )


def _record_llm(recorder: TraceRecorder, stage: str, response: Any) -> None:
    recorder.record(
        "llm.response",
        stage=stage,
        model=response.model,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        cost_usd=response.cost_usd,
    )


def investigate(
    symptom: str,
    *,
    llm: LLMClient | None = None,
    model: str = DEFAULT_MODEL,
    trace_dir: str | Path | None = None,
    allow_mutations: bool = False,
    max_steps: int = 8,
    run_id: str | None = None,
    drop_tools: frozenset[str] = frozenset(),
    enforce_citations: bool = True,
) -> AgentRun:
    """Investiga un síntoma y devuelve el diagnóstico trazado.

    `allow_mutations=False` por defecto: el bucle puede *proponer* un cambio de
    estado, pero convertirlo en acción ejecutable exige activarlo y, aun así,
    la tool devuelve `pending_approval` hasta que un humano apruebe. No existe
    un camino en el que el agente apruebe su propia acción.

    `drop_tools` y `enforce_citations` existen **sólo para las ablaciones** del
    harness de evaluación: permiten medir qué aporta cada pieza sin mantener una
    segunda copia del bucle. Producción usa los valores por defecto.
    """
    started = time.perf_counter()
    run_id = run_id or _new_run_id()
    bundle = EvidenceBundle()
    tokens_in = tokens_out = 0
    cost = 0.0
    responses: list[Any] = []
    planner_version = HEURISTIC_PROMPT
    synth_version = HEURISTIC_PROMPT

    recorder = TraceRecorder(run_id, trace_dir)
    recorder.record("run.start", run_id=run_id, symptom=symptom, model=model)

    run = AgentRun(run_id=run_id, symptom=symptom, status="RUNNING", model=model)
    try:
        # --- salud del sistema antes de gastar un solo token -----------------
        health, health_ok, health_error = call_tool("system_health")
        if health_ok:
            seed_health(bundle, health)
        else:
            bundle.data_trust = "low"
            bundle.gaps.append(f"system_health falló: {health_error}")
        recorder.record(
            "tool.call",
            step_id=0,
            tool="system_health",
            params={},
            ok=health_ok,
            error=health_error,
        )

        if not health_ok or bundle.data_trust == "low":
            reason = (
                f"no se pudo leer la salud del sistema: {health_error}"
                if not health_ok
                else f"datos no fiables: cobertura de parseo {health.get('parse_coverage', 0.0):.1%}"
            )
            diagnosis = _blocked_diagnosis(symptom, reason)
            recorder.record(
                "verify", action="abort", reason=reason, checks={"trust_sufficient": False}
            )
            return _finish(
                run,
                recorder,
                bundle,
                diagnosis,
                citations.CitationReport(),
                status="BLOCKED",
                started=started,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost=cost,
                prompt_version=HEURISTIC_PROMPT,
            )

        # --- 1. PLAN ---------------------------------------------------------
        plan, plan_responses, plan_error = plan_with_fallback(
            symptom, llm=llm, health=health, model=model, max_steps=max_steps
        )
        plan = _prune(plan, drop_tools)
        for response in plan_responses:
            tokens_in += response.tokens_in
            tokens_out += response.tokens_out
            cost += response.cost_usd
            _record_llm(recorder, "planner", response)
        if plan_responses:
            planner_version = "planner.v1"
        if plan_error:
            bundle.gaps.append(plan_error)
        recorder.record("plan", plan=plan.model_dump(), fallback_error=plan_error)

        # --- 2. EXECUTE ------------------------------------------------------
        bundle = execute_plan(plan, bundle)
        _record_calls(recorder, bundle)
        recorder.record("evidence", ids=bundle.ids(), count=len(bundle.items))

        # --- 3. VERIFY -------------------------------------------------------
        verdict = after_execution(plan, bundle)
        recorder.record(
            "verify", action=verdict.action, reason=verdict.reason, checks=verdict.checks
        )

        if verdict.action == "replan":
            replan = _prune(heuristic_plan(symptom, health=health, max_steps=max_steps), drop_tools)
            replan.restatement = f"{plan.restatement} | re-planificado: {verdict.reason}"
            bundle = execute_plan(replan, bundle)
            _record_calls(recorder, bundle)
            plan = replan
            verdict = after_execution(plan, bundle)
            recorder.record(
                "verify",
                action=verdict.action,
                reason=verdict.reason,
                checks=verdict.checks,
                attempt=2,
            )

        if verdict.action == "abort":
            diagnosis = _blocked_diagnosis(symptom, verdict.reason)
            return _finish(
                run,
                recorder,
                bundle,
                diagnosis,
                citations.CitationReport(),
                status="BLOCKED",
                started=started,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost=cost,
                prompt_version=_prompt_label(planner_version, synth_version),
                plan=plan,
            )

        # --- 4. SYNTHESIZE + 5. CITE ----------------------------------------
        diagnosis, report = _synthesize(
            symptom,
            bundle,
            llm=llm,
            model=model,
            recorder=recorder,
            responses=responses,
        )
        for response in responses:
            tokens_in += response.tokens_in
            tokens_out += response.tokens_out
            cost += response.cost_usd
            _record_llm(recorder, "synthesizer", response)
        if responses:
            synth_version = "synthesizer.v1"

        diagnosis = diagnosis.model_copy(
            update={"evidence_gaps": _merge_gaps(diagnosis.evidence_gaps, bundle.gaps)}
        )
        if enforce_citations:
            diagnosis = citations.apply(diagnosis, report)
        recorder.record(
            "citations",
            valid=len(report.valid),
            invalid=len(report.invalid),
            uncited=len(report.uncited),
            failure_rate=report.fact_failure_rate,
            removed=[claim.text for claim in report.invalid],
        )

        # --- 6. GATE ---------------------------------------------------------
        pending: dict[str, Any] | None = None
        status = "OK"
        if (
            allow_mutations
            and diagnosis.proposed_status
            and diagnosis.proposed_incident_id is not None
        ):
            pending, status = _gate(diagnosis, recorder)
        elif diagnosis.proposed_status:
            diagnosis = diagnosis.model_copy(
                update={
                    "evidence_gaps": [
                        *diagnosis.evidence_gaps,
                        "se propuso un cambio de estado pero no se solicitó: "
                        "relanza con allow_mutations para abrir la aprobación humana",
                    ]
                }
            )

        return _finish(
            run,
            recorder,
            bundle,
            diagnosis,
            report,
            status=status,
            started=started,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            prompt_version=_prompt_label(planner_version, synth_version),
            plan=plan,
            pending=pending,
        )
    except Exception as exc:  # la traza se cierra siempre; una corrida rota deja rastro
        recorder.record("run.error", error=f"{type(exc).__name__}: {exc}")
        run = run.model_copy(
            update={
                "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
                "evidence": bundle,
                "trace_path": str(recorder.path),
            }
        )
        recorder.record("run.end", status="FAILED", error=run.error)
        recorder.close()
        _safe_record(run)
        return run


def _synthesize(
    symptom: str,
    bundle: EvidenceBundle,
    *,
    llm: LLMClient | None,
    model: str,
    recorder: TraceRecorder,
    responses: list[Any],
) -> tuple[Diagnosis, citations.CitationReport]:
    """Sintetiza y valida citas, con un reintento si el fallo es de hechos."""
    if llm is None:
        diagnosis = synthesize_fallback(symptom, bundle)
        return diagnosis, citations.validate(diagnosis, bundle)

    try:
        diagnosis, response = synthesize_llm(symptom, bundle, llm=llm, model=model)
        responses.append(response)
        report = citations.validate(diagnosis, bundle)
        if not citations.should_retry(report):
            return diagnosis, report

        recorder.record(
            "citations.retry",
            reason=f"{report.fact_failure_rate:.0%} de los hechos no están sustentados",
        )
        diagnosis, response = synthesize_llm(
            symptom,
            bundle,
            llm=llm,
            model=model,
            feedback=citations.feedback_prompt(report),
        )
        responses.append(response)
        return diagnosis, citations.validate(diagnosis, bundle)
    except (LLMUnavailable, ValidationError, ValueError) as exc:
        bundle.gaps.append(f"síntesis del modelo no utilizable ({type(exc).__name__}): {exc}")
        recorder.record("llm.error", stage="synthesizer", error=f"{type(exc).__name__}: {exc}")
        diagnosis = synthesize_fallback(
            symptom, bundle, reason="se usó el sintetizador determinista"
        )
        return diagnosis, citations.validate(diagnosis, bundle)


def _gate(diagnosis: Diagnosis, recorder: TraceRecorder) -> tuple[dict[str, Any] | None, str]:
    """Pide la mutación. La tool la deja pendiente; el humano decide."""
    payload, ok, error = call_tool(
        "set_incident_status",
        {
            "incident_id": diagnosis.proposed_incident_id,
            "status": diagnosis.proposed_status,
        },
    )
    if not ok:
        recorder.record("gate", result="error", error=error)
        return None, "OK"

    recorder.record(
        "gate",
        result=payload.get("status"),
        action_id=(payload.get("pending_action") or {}).get("action_id"),
    )
    if payload.get("status") == "pending_approval":
        return payload, "NEEDS_APPROVAL"
    return None, "OK"


def _finish(
    run: AgentRun,
    recorder: TraceRecorder,
    bundle: EvidenceBundle,
    diagnosis: Diagnosis,
    report: citations.CitationReport,
    *,
    status: str,
    started: float,
    tokens_in: int,
    tokens_out: int,
    cost: float,
    prompt_version: str,
    plan: Plan | None = None,
    pending: dict[str, Any] | None = None,
) -> AgentRun:
    duration_ms = int((time.perf_counter() - started) * 1000)
    completed = run.model_copy(
        update={
            "status": status,
            "plan": plan,
            "diagnosis": diagnosis,
            "citations": report,
            "evidence": bundle,
            "pending_action": pending,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost, 6),
            "duration_ms": duration_ms,
            "prompt_version": prompt_version,
            "trace_path": str(recorder.path),
        }
    )
    recorder.record(
        "run.end",
        status=status,
        duration_ms=duration_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=round(cost, 6),
        diagnosis=diagnosis.model_dump(),
        pending_action=pending,
        evidence_gaps=diagnosis.evidence_gaps,
    )
    recorder.close()
    _safe_record(completed)
    return completed


def _prune(plan: Plan, drop_tools: frozenset[str]) -> Plan:
    """Quita pasos de una ablación y limpia las dependencias que apuntaban a ellos.

    Un paso que dependía del que se quitó pasa a no depender de nada: dejarlo
    colgado haría que el executor lo bloqueara por una dependencia inexistente.
    """
    if not drop_tools:
        return plan
    kept = [step for step in plan.steps if step.tool not in drop_tools]
    removed = {step.step_id for step in plan.steps if step.tool in drop_tools}
    for step in kept:
        step.depends_on = [dep for dep in step.depends_on if dep not in removed]
    return plan.model_copy(update={"steps": kept})


def _merge_gaps(declared: list[str], from_execution: list[str]) -> list[str]:
    """Une los huecos del modelo con los que el motor detectó.

    Un fallo de tool o un plan inválido no puede quedarse en la traza: si afecta
    a lo que se pudo concluir, tiene que aparecer en el diagnóstico que lee el
    operador.
    """
    merged = list(declared)
    for gap in from_execution:
        if gap not in merged:
            merged.append(gap)
    return merged


def _record_calls(recorder: TraceRecorder, bundle: EvidenceBundle) -> None:
    for call in bundle.tool_calls:
        recorder.record("tool.call", **call.model_dump())


def _safe_record(run: AgentRun) -> None:
    """La traza en disco ya existe; un fallo al escribir el resumen no la borra."""
    try:
        record_run(run)
    except sqlite3.Error:
        pass


def _prompt_label(planner: str, synthesizer: str) -> str:
    if planner == HEURISTIC_PROMPT and synthesizer == HEURISTIC_PROMPT:
        return HEURISTIC_PROMPT
    return f"{planner}+{synthesizer}"


__all__ = ["HEURISTIC_PROMPT", "investigate"]
