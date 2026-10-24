"""Ejecución de un `Plan` contra las tools MCP, en proceso.

El agente no ejecuta SQL: llama a las mismas tools que un cliente MCP llamaría.
Eso garantiza que el contrato que se prueba en `tests/test_mcp_tools.py` es el
que usa el bucle, sin una segunda implementación que se desincronice.

Dos detalles que no son obvios:

* Los pasos sin dependencias mutuas se lanzan **en paralelo** (`asyncio.gather`)
  y cada llamada se registra por separado. La dependencia la declara el plan, no
  se adivina por el orden.
* El resultado de una tool se normaliza a `EvidenceItem` **antes** de que el
  modelo lo vea. El modelo nunca recibe el JSON crudo de la tool: recibe ids
  citables. Esa es la frontera que hace que la validación de citas sea mecánica.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from mcp import Client

from app.agent.contracts import EvidenceBundle, EvidenceItem, Plan, PlanStep, ToolCall
from app.agent.planner import PLANNABLE_TOOLS
from app.mcp.server import build_server

# Placeholder que el planner puede usar cuando no conoce la huella: se resuelve
# contra el resultado real de los pasos previos, nunca contra una invención.
TOP_FINGERPRINT = "$top_fingerprint"


class ExecutionError(RuntimeError):
    """El plan no se pudo ejecutar por una razón estructural (ciclo, placeholder)."""


class _State:
    def __init__(self, bundle: EvidenceBundle | None = None) -> None:
        self.bundle = bundle if bundle is not None else EvidenceBundle()
        self.health: dict[str, Any] = {}
        self.top_fingerprint: str | None = None
        for item in self.bundle.items:
            if item.kind == "fingerprint" and self.top_fingerprint is None:
                self.top_fingerprint = item.evidence_id.removeprefix("fp:")


def _resolve(value: Any, state: _State) -> Any:
    """Sustituye placeholders en los parámetros, recursivamente."""
    if value == TOP_FINGERPRINT:
        if not state.top_fingerprint:
            raise ExecutionError(
                "el plan usa $top_fingerprint pero ningún paso previo encontró huellas"
            )
        return state.top_fingerprint
    if isinstance(value, dict):
        return {key: _resolve(item, state) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, state) for item in value]
    return value


def _error_text(result: Any) -> str:
    """El mensaje de error que devolvió la tool, sea del bloque que sea."""
    if not result.content:
        return "error sin mensaje"
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            return text
    return "error sin mensaje"


def _hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _rows(tool: str, payload: dict[str, Any]) -> int:
    for key in ("events", "errors", "anomalies", "incidents", "services"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return 1 if payload else 0


def _evidence(tool: str, payload: dict[str, Any], state: _State) -> list[EvidenceItem]:
    """Traduce la salida de una tool a evidencia citable.

    Las claves de los payloads son las de `app/mcp/schemas.py`; si una tool
    cambia de forma, este es el punto que falla en los tests del agente.
    """
    if tool == "system_health":
        state.health = payload
        state.bundle.data_trust = payload.get("trust_level", "low")
        return [
            EvidenceItem(
                evidence_id="sys:health",
                kind="health",
                summary=(
                    f"{payload.get('total_events', 0)} eventos, cobertura "
                    f"{payload.get('parse_coverage', 0.0):.1%}, "
                    f"trust={payload.get('trust_level')}"
                ),
                ts=payload.get("last_event_ts"),
                fields={
                    "total_events": payload.get("total_events"),
                    "parse_coverage": payload.get("parse_coverage"),
                    "trust_level": payload.get("trust_level"),
                    "first_event_ts": payload.get("first_event_ts"),
                    "last_event_ts": payload.get("last_event_ts"),
                },
            )
        ]

    if tool == "top_errors":
        items = []
        for rank, error in enumerate(payload.get("errors") or [], start=1):
            fingerprint = str(error.get("fingerprint"))
            if state.top_fingerprint is None:
                state.top_fingerprint = fingerprint
            items.append(
                EvidenceItem(
                    evidence_id=f"fp:{fingerprint}",
                    kind="fingerprint",
                    summary=(
                        f"{fingerprint} — {error.get('count', 0)} errores "
                        f"({float(error.get('share') or 0.0):.1%} del total), "
                        f"categoría {error.get('category') or 'sin clasificar'}"
                    ),
                    ts=error.get("last_seen"),
                    fields={
                        "rank": rank,
                        "count": error.get("count"),
                        "share": error.get("share"),
                        "category": error.get("category"),
                        "severity": error.get("severity"),
                        "service": error.get("service"),
                        "sample_message": error.get("sample_message"),
                        "first_seen": error.get("first_seen"),
                        "last_seen": error.get("last_seen"),
                    },
                )
            )
        return items

    if tool == "search_events":
        return [
            EvidenceItem(
                evidence_id=f"ev:{event.get('event_id')}",
                kind="event",
                summary=str(event.get("message") or "")[:280],
                ts=event.get("timestamp_utc"),
                fields={
                    "severity": event.get("severity"),
                    "service": event.get("service"),
                    "category": event.get("category"),
                    "fingerprint": event.get("fingerprint"),
                    "level": event.get("level"),
                },
            )
            for event in payload.get("events") or []
        ]

    if tool == "detect_anomalies":
        return [
            EvidenceItem(
                evidence_id=f"anom:{item.get('fingerprint')}:{item.get('hour')}",
                kind="anomaly",
                summary=(
                    f"{item.get('count')} eventos en {item.get('hour')} (z={item.get('z_score')})"
                ),
                ts=item.get("hour"),
                fields={
                    "fingerprint": item.get("fingerprint"),
                    "count": item.get("count"),
                    "z_score": item.get("z_score"),
                },
            )
            for item in payload.get("anomalies") or []
        ]

    if tool == "compare_windows":
        fingerprint = str(payload.get("fingerprint"))
        item = EvidenceItem(
            evidence_id=f"cmp:{fingerprint}",
            kind="comparison",
            summary=(
                f"{fingerprint}: veredicto {payload.get('verdict')} "
                f"(ratio {payload.get('ratio')}, z {payload.get('z_score')}, "
                f"confianza {payload.get('confidence')})"
            ),
            ts=(payload.get("window") or {}).get("until"),
            fields={
                "fingerprint": fingerprint,
                "verdict": payload.get("verdict"),
                "ratio": payload.get("ratio"),
                "z_score": payload.get("z_score"),
                "confidence": payload.get("confidence"),
                "window_count": (payload.get("window") or {}).get("count"),
                "baseline_count": (payload.get("baseline") or {}).get("count"),
                "affected_services": payload.get("affected_services"),
            },
        )
        return [item]

    if tool == "blast_radius":
        fingerprint = str(payload.get("fingerprint"))
        items = [
            EvidenceItem(
                evidence_id=f"svc:{fingerprint}:{service.get('service')}",
                kind="service",
                summary=(
                    f"{service.get('service')}: {service.get('count')} eventos "
                    f"({float(service.get('share') or 0.0):.1%})"
                ),
                ts=service.get("first_seen"),
                fields={
                    "fingerprint": fingerprint,
                    "service": service.get("service"),
                    "count": service.get("count"),
                    "share": service.get("share"),
                    "first_seen": service.get("first_seen"),
                    "last_seen": service.get("last_seen"),
                },
            )
            for service in payload.get("affected_services") or []
        ]
        items.append(
            EvidenceItem(
                evidence_id=f"blast:{fingerprint}",
                kind="comparison",
                summary=str(payload.get("cascade_note") or ""),
                fields={
                    "fingerprint": fingerprint,
                    "is_cascade": payload.get("is_cascade"),
                    "distinct_services": payload.get("distinct_services"),
                    "total_events": payload.get("total_events"),
                },
            )
        )
        return items

    if tool == "explain_fingerprint":
        fingerprint = str(payload.get("fingerprint"))
        return [
            EvidenceItem(
                evidence_id=f"fp:{fingerprint}",
                kind="fingerprint",
                summary=str(payload.get("normalized_template") or fingerprint),
                fields={
                    "category": payload.get("category"),
                    "category_description": payload.get("category_description"),
                    "normalized_template": payload.get("normalized_template"),
                    "examples": payload.get("examples"),
                    "total_events": payload.get("total_events"),
                },
            )
        ]

    if tool == "get_incident":
        incident = payload.get("incident")
        if not payload.get("found") or not isinstance(incident, dict):
            return []
        incident_id = incident.get("incident_id")
        return [
            EvidenceItem(
                evidence_id=f"inc:{incident_id}",
                kind="incident",
                summary=(
                    f"incidente {incident_id}: {incident.get('sample_message')} "
                    f"({incident.get('status')}, {incident.get('event_count')} eventos)"
                ),
                ts=incident.get("first_seen"),
                fields=incident,
            )
        ]

    if tool == "list_incidents":
        return [
            EvidenceItem(
                evidence_id=f"inc:{incident.get('incident_id')}",
                kind="incident",
                summary=(
                    f"incidente {incident.get('incident_id')}: "
                    f"{incident.get('sample_message')} ({incident.get('status')})"
                ),
                ts=incident.get("first_seen"),
                fields=incident,
            )
            for incident in payload.get("incidents") or []
        ]

    return []


async def _run_step(client: Client, step: PlanStep, state: _State) -> None:
    started = time.perf_counter()
    call = ToolCall(step_id=step.step_id, tool=step.tool, params=step.params)
    try:
        params = _resolve(step.params, state)
        call = call.model_copy(update={"params": params})
        result = await client.call_tool(step.tool, params)
    except Exception as exc:  # el bucle sigue: un fallo es un gap, no un abort
        call.ok = False
        call.error = f"{type(exc).__name__}: {exc}"
        call.duration_ms = int((time.perf_counter() - started) * 1000)
        state.bundle.tool_calls.append(call)
        state.bundle.gaps.append(f"{step.tool} falló: {call.error}")
        return

    call.duration_ms = int((time.perf_counter() - started) * 1000)
    if getattr(result, "is_error", False):
        text = _error_text(result)
        call.ok = False
        call.error = text
        state.bundle.tool_calls.append(call)
        state.bundle.gaps.append(f"{step.tool} devolvió error: {text}")
        return

    payload = result.structured_content or {}
    call.result_hash = _hash(payload)
    call.rows = _rows(step.tool, payload)
    state.bundle.tool_calls.append(call)
    for item in _evidence(step.tool, payload, state):
        state.bundle.add(item)


async def _execute(plan: Plan, state: _State) -> _State:
    async with Client(build_server()) as client:
        remaining = list(plan.steps)
        done: set[int] = set()
        while remaining:
            wave = [step for step in remaining if all(dep in done for dep in step.depends_on)]
            if not wave:
                raise ExecutionError("el plan tiene un ciclo de dependencias")
            await asyncio.gather(*(_run_step(client, step, state) for step in wave))
            done.update(step.step_id for step in wave)
            remaining = [step for step in remaining if step.step_id not in done]
    return state


# Tools que pueden establecer `$top_fingerprint`. Un plan que usa el placeholder
# sin una de ellas antes es un error del planner, no una ventana vacía.
_FINGERPRINT_PRODUCERS = {"top_errors", "search_events"}


def _uses_top_fingerprint(value: Any) -> bool:
    if value == TOP_FINGERPRINT:
        return True
    if isinstance(value, dict):
        return any(_uses_top_fingerprint(item) for item in value.values())
    if isinstance(value, list):
        return any(_uses_top_fingerprint(item) for item in value)
    return False


def _check_placeholders(plan: Plan) -> None:
    """Rechaza un plan que referencia una huella que nadie va a producir.

    Se comprueba antes de ejecutar: fallar a mitad de una ola paralela deja un
    `ExceptionGroup` difícil de leer en la traza, y el fallo es del plan, no de
    los datos.
    """
    produced = False
    for step in plan.steps:
        if _uses_top_fingerprint(step.params) and not produced:
            raise ExecutionError(
                f"el paso {step.step_id} usa {TOP_FINGERPRINT} sin un paso previo "
                f"que lo produzca ({', '.join(sorted(_FINGERPRINT_PRODUCERS))})"
            )
        if step.tool in _FINGERPRINT_PRODUCERS:
            produced = True


def execute_plan(plan: Plan, bundle: EvidenceBundle | None = None) -> EvidenceBundle:
    """Ejecuta el plan completo y devuelve la evidencia acumulada.

    `bundle` permite encadenar fases (por ejemplo, tras una re-planificación) y
    conservar la evidencia ya recogida en lugar de tirarla.
    """
    plan.validate_shape(PLANNABLE_TOOLS)
    _check_placeholders(plan)
    state = asyncio.run(_execute(plan, _State(bundle)))
    return state.bundle


def seed_health(bundle: EvidenceBundle, payload: dict[str, Any]) -> None:
    """Incorpora el `system_health` previo al plan sin volver a llamar la tool."""
    state = _State(bundle)
    for item in _evidence("system_health", payload, state):
        bundle.add(item)


def call_tool(
    name: str, params: dict[str, Any] | None = None
) -> tuple[dict[str, Any], bool, str | None]:
    """Llama a una tool suelta y devuelve `(payload, ok, error)`.

    Se usa para el `system_health` previo al plan: el planner necesita la salud
    del sistema antes de decidir qué preguntar.
    """

    async def _run() -> tuple[dict[str, Any], bool, str | None]:
        async with Client(build_server()) as client:
            result = await client.call_tool(name, params or {})
            if getattr(result, "is_error", False):
                return {}, False, _error_text(result)
            return result.structured_content or {}, True, None

    return asyncio.run(_run())


__all__ = [
    "TOP_FINGERPRINT",
    "ExecutionError",
    "call_tool",
    "execute_plan",
    "seed_health",
]
