"""Prompts MCP: plantillas que el cliente ofrece al usuario.

Un prompt es texto, no código: no consulta la base ni decide nada. Su valor es
fijar el **protocolo de investigación** en el sitio donde el modelo lo va a
leer, en lugar de confiar en que el usuario lo recuerde. Las dos plantillas
reptien la misma regla porque es la que sostiene el proyecto: el motor calcula,
el modelo redacta, y una afirmación sin una llamada que la respalde no se
escribe.
"""

from __future__ import annotations

from typing import Any

INVESTIGATE_SYMPTOM = "investigate_symptom"
WRITE_POSTMORTEM = "write_postmortem"

_RULES = """\
Reglas de la investigación, sin excepciones:

1. Llama primero a `system_health`. Si `trust_level` es `low`, dilo antes de
   cualquier conclusión: una cobertura de parseo baja invalida lo que se diga
   sobre categorías.
2. No afirmes que algo "se ha disparado", "ha empeorado" o "es nuevo" sin
   llamar antes a `compare_windows`. El veredicto (`new`, `elevated`, `stable`,
   `decreased`) y la confianza los calcula el motor; no los estimes.
3. No estimes el alcance de un fallo a ojo. `blast_radius` devuelve los
   servicios afectados y si hay cascada.
4. Cada cifra que escribas tiene que venir de una llamada concreta. Cita la
   tool y sus argumentos junto al número. Si no tienes el dato, escribe que no
   lo tienes; no lo redondees ni lo deduzcas.
5. Separa lo que el motor midió de lo que tú interpretas. La interpretación es
   bienvenida; la invención no.
6. Si una tool devuelve un error, dilo y explica qué falta. No la reintentes
   con los mismos argumentos.
"""


def investigate_symptom(
    symptom: str,
    service: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Plantilla de investigación guiada para un síntoma descrito por el usuario."""
    context = [f"Síntoma: {symptom}"]
    if service:
        context.append(f"Servicio, si se conoce: {service}")
    if since or until:
        context.append(f"Ventana: since={since or '(sin límite)'} until={until or '(sin límite)'}")

    body = f"""\
{chr(10).join(context)}

Investiga el síntoma con las tools de LogLens y entrega un diagnóstico.
Sigue este orden:

1. `system_health` — estado del dataset y cobertura de parseo.
2. `search_events` o `top_errors` — identifica la huella (fingerprint) que
   corresponde al síntoma. Si hay varias, no elijas una: repórtalas.
3. `explain_fingerprint` — traduce la huella a una frase legible y mira
   ejemplos reales antes de describirla.
4. `compare_windows` — decide si el volumen es nuevo, está elevado o estable,
   comparando contra el baseline que corresponda.
5. `blast_radius` — delimita qué servicios están afectados y si el fallo se
   propaga en cascada.

{_RULES}
Formato de la respuesta:

- **Diagnóstico**: una o dos frases. Qué está pasando, no qué podría pasar.
- **Evidencia**: lista de `tool(args) → dato`. Sólo datos del motor.
- **Alcance**: servicios y periodo, con la fuente de cada cifra.
- **Confianza**: alta, media o baja, y por qué. Usa la que devuelva
  `compare_windows` cuando exista.
- **Siguiente paso**: la comprobación más barata que descartaría la hipótesis
  principal.
"""
    return [{"role": "user", "content": body}]


def write_postmortem(
    incident_id: str,
    occurred_at: str | None = None,
) -> list[dict[str, Any]]:
    """Plantilla para redactar un postmortem a partir de un incidente resuelto.

    `incident_id` entra como texto porque los argumentos de un prompt MCP son
    siempre cadenas, pero el cuerpo lo usa como identificador numérico.
    """
    try:
        identifier = int(incident_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"incident_id inválido: {incident_id!r}") from exc
    when = f" Fecha del incidente, si se conoce: {occurred_at}." if occurred_at else ""

    body = f"""\
Redacta un postmortem del incidente {identifier}.{when}

Empieza llamando a `get_incident` con `incident_id={identifier}`. Si
`found` es `false`, detente y dilo: no hay postmortem sin incidente.
Usa `explain_fingerprint` para describir el fallo y `blast_radius` para el
alcance. Si el incidente está `RESOLVED`, `compare_windows` sirve para
confirmar que el volumen volvió al baseline.

{_RULES}
Estructura del postmortem:

1. **Resumen**: qué pasó, en dos frases, y cuánto duró.
2. **Impacto**: servicios afectados y volumen de eventos. Sólo datos medidos.
3. **Línea de tiempo**: del primer evento al cierre, con timestamps del motor.
4. **Causa raíz**: sólo si la evidencia la sostiene. Si no, escribe
   "no determinada por los datos disponibles" y enumera lo que falta.
5. **Detección**: cómo se detectó y cuánto tardó.
6. **Acciones**: separa las que se hicieron de las que se proponen.

No atribuyas causa raíz a partir de correlación temporal: `blast_radius`
dice qué servicios comparten la huella, no cuál lo provocó.
"""
    return [{"role": "user", "content": body}]


__all__ = ["INVESTIGATE_SYMPTOM", "WRITE_POSTMORTEM", "investigate_symptom", "write_postmortem"]
