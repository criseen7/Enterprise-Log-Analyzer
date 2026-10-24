"""Del síntoma a un `Plan`: camino con LLM y camino determinista.

El planner **no ve la base de datos**. Ve el catálogo de tools con sus schemas y
el estado de salud del sistema. Esa restricción es lo que impide que se invente
capacidades: si no está en el schema, no puede pedirlo.

El camino determinista no es un consuelo para cuando falla el modelo; es el
producto funcionando sin modelo. Un operador sin clave de API obtiene un plan
razonable, y en los evals es el control A0: si el plan heurístico ya acierta,
el valor está en el motor y no en el prompt.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from mcp import Client

from app.agent.contracts import Plan, PlanStep
from app.agent.llm import LLMClient, LLMResponse, LLMUnavailable
from app.agent.promptfiles import load_rendered
from app.mcp.server import build_server
from app.timeutil import parse_utc, to_canonical

# Sólo las tools de lectura pueden aparecer en un plan. La mutación no se
# planifica: se propone y pasa por aprobación humana (ver `loop.py`).
PLANNABLE_TOOLS = {
    "system_health",
    "search_events",
    "top_errors",
    "detect_anomalies",
    "compare_windows",
    "blast_radius",
    "explain_fingerprint",
    "list_incidents",
    "get_incident",
}

DEFAULT_MODEL = "claude-sonnet-4-5"
WINDOW_HOURS = 1

_catalog_cache: list[dict[str, Any]] | None = None


def tool_catalog() -> list[dict[str, Any]]:
    """Nombre, descripción y schema de entrada de cada tool, desde el servidor.

    Se lee del servidor MCP y no de una lista paralela: si se añade una tool y
    se olvida aquí, el planner no la vería, pero al menos no podría llamarla con
    un schema inventado. Una sola fuente de verdad vale más que una copia
    cómoda.
    """
    global _catalog_cache
    if _catalog_cache is None:

        async def _list() -> list[dict[str, Any]]:
            async with Client(build_server()) as client:
                listing = await client.list_tools()
                return [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in listing.tools
                ]

        _catalog_cache = asyncio.run(_list())
    return _catalog_cache


def catalog_for_prompt(allowed: set[str] | None = None) -> str:
    tools = [tool for tool in tool_catalog() if allowed is None or tool["name"] in allowed]
    return json.dumps(tools, ensure_ascii=False, indent=2)


def default_window(health: dict[str, Any]) -> tuple[str | None, str | None]:
    """Ventana de una hora al final del dataset.

    No se usa "ahora": el dataset puede ser histórico y una ventana contra el
    reloj devolvería vacío. El final del dataset es el único "ahora" honesto.
    """
    last = parse_utc(str(health.get("last_event_ts") or ""))
    first = parse_utc(str(health.get("first_event_ts") or ""))
    if last is None:
        return None, None
    until = to_canonical(last.replace(microsecond=999000))
    # Una hora móvil hacia atrás, no "la hora del reloj": si el incidente
    # terminó a las 14:25 y el último evento es de las 14:30, cortar en las
    # 14:00 dejaría fuera la mayor parte de la ventana.
    since_dt = last - timedelta(hours=1)
    if first is not None and first > since_dt:
        since_dt = first
    return to_canonical(since_dt), until


def heuristic_plan(
    symptom: str,
    *,
    health: dict[str, Any] | None = None,
    since: str | None = None,
    until: str | None = None,
    max_steps: int = 8,
) -> Plan:
    """Plan fijo: salud, top de errores y, sobre la huella principal, comparación,
    alcance y explicación.

    Se deriva la ventana del propio dataset (última hora) en lugar de pedirla al
    usuario. Si el plan devuelve vacío, `verify` lo re-planifica sin ventana.
    """
    if health is not None and since is None and until is None:
        since, until = default_window(health)

    window: dict[str, Any] = {}
    if since is not None:
        window["since"] = since
    if until is not None:
        window["until"] = until

    steps = [
        PlanStep(
            step_id=1,
            hypothesis="Comprobar cobertura y salud del dataset antes de concluir nada.",
            tool="system_health",
        ),
        PlanStep(
            step_id=2,
            hypothesis="Identificar la huella de error que concentra el volumen de la ventana.",
            tool="top_errors",
            params={"limit": 10, **window},
            depends_on=[1],
        ),
        PlanStep(
            step_id=3,
            hypothesis="Decidir si el volumen es nuevo, elevado o estable frente al baseline.",
            tool="compare_windows",
            params={"fingerprint": "$top_fingerprint", **window},
            depends_on=[2],
        ),
        PlanStep(
            step_id=4,
            hypothesis="Delimitar servicios afectados y si el fallo se propaga en cascada.",
            tool="blast_radius",
            params={"fingerprint": "$top_fingerprint", **window},
            depends_on=[2],
        ),
        PlanStep(
            step_id=5,
            hypothesis="Traducir la huella a una frase legible con ejemplos reales.",
            tool="explain_fingerprint",
            params={"fingerprint": "$top_fingerprint"},
            depends_on=[2],
        ),
    ][:max_steps]

    return Plan(
        symptom=symptom,
        # La reformulación heurística no inventa: repite el síntoma y declara qué
        # ventana va a mirar, que es lo único que el plan sabe de cierto.
        restatement=(
            f"{symptom.strip()} (ventana analizada: "
            f"{since or 'inicio del dataset'} → {until or 'último evento'})"
        ),
        steps=steps,
        max_steps=max_steps,
    )


def parse_plan(payload: dict[str, Any] | str) -> Plan:
    """Valida el JSON del modelo contra `Plan` y la lista blanca de tools."""
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"el plan no es JSON válido: {exc}") from exc
    else:
        data = payload
    plan = Plan.model_validate(data)
    plan.validate_shape(PLANNABLE_TOOLS)
    return plan


def llm_plan(
    symptom: str,
    *,
    llm: LLMClient,
    health: dict[str, Any],
    model: str = DEFAULT_MODEL,
    max_steps: int = 8,
    feedback: str | None = None,
) -> tuple[Plan, LLMResponse]:
    """Pide un plan al modelo y lo valida. Un plan inválido es un error, no un plan."""
    prompt, _ = load_rendered(
        "planner",
        "v1",
        symptom=symptom,
        health=json.dumps(health, ensure_ascii=False, indent=2),
        tools=catalog_for_prompt(PLANNABLE_TOOLS),
        max_steps=max_steps,
    )
    if feedback:
        prompt += f"\n\nEl intento anterior falló y hay que corregirlo:\n{feedback}\n"

    response = llm.complete(
        [{"role": "user", "content": prompt}],
        model=model,
        max_tokens=2048,
        temperature=0.0,
        response_schema=Plan,
    )
    plan = parse_plan(response.data if response.data is not None else response.text)
    return plan, response


def plan_with_fallback(
    symptom: str,
    *,
    llm: LLMClient | None,
    health: dict[str, Any],
    model: str = DEFAULT_MODEL,
    max_steps: int = 8,
) -> tuple[Plan, list[LLMResponse], str | None]:
    """Plan del LLM con un reintento; si no hay modelo o falla, plan heurístico.

    Devuelve `(plan, respuestas_del_modelo, error)`. El error no es un fallo de
    la corrida: es la razón por la que se usó la heurística, y va a la traza.
    """
    responses: list[LLMResponse] = []
    if llm is None:
        return heuristic_plan(symptom, health=health, max_steps=max_steps), responses, None

    feedback: str | None = None
    for attempt in range(2):
        try:
            plan, response = llm_plan(
                symptom,
                llm=llm,
                health=health,
                model=model,
                max_steps=max_steps,
                feedback=feedback,
            )
            responses.append(response)
            return plan, responses, None
        except (ValueError, LLMUnavailable) as exc:
            feedback = str(exc)
            if attempt == 0:
                continue

    plan = heuristic_plan(symptom, health=health, max_steps=max_steps)
    return plan, responses, f"plan del modelo inválido tras dos intentos: {feedback}"


__all__ = [
    "DEFAULT_MODEL",
    "PLANNABLE_TOOLS",
    "catalog_for_prompt",
    "default_window",
    "heuristic_plan",
    "llm_plan",
    "parse_plan",
    "plan_with_fallback",
    "tool_catalog",
]
