"""Resources MCP: contexto que el agente puede leer sin gastar una tool call.

Un *resource* es distinto de una *tool*: la tool ejecuta una consulta con
argumentos y devuelve un resultado; el resource entrega un documento que el
cliente puede adjuntar al contexto (el esquema de la tabla, el catálogo de
reglas, un incidente concreto). La diferencia importa para el coste: un agente
que manda `loglens://schema` al prompt inicial no necesita preguntar por la
forma de los datos en cada turno.

Los tres resources de v1 son deliberadamente **estáticos o de sólo lectura**.
Ninguno muta nada, y por eso no pasan por HITL.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ResourceError

from app.analysis.errors import ErrorRules
from app.mcp.tools.incidents import get_incident

SCHEMA_URI = "loglens://schema"
RULES_URI = "loglens://rules"
INCIDENT_URI_TEMPLATE = "loglens://incident/{incident_id}"
TRACE_URI_TEMPLATE = "loglens://trace/{run_id}"

# El nombre del run es el nombre del archivo, así que se valida antes de tocar
# el disco: sin esto, `loglens://trace/../../etc/passwd` sería una lectura
# arbitraria disfrazada de resource.
_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")

# El glosario es estático a propósito: describe la *semántica*, no la forma
# física de la tabla. La forma física cambia con las migraciones; el contrato
# que el agente necesita entender, no.
_EVENT_COLUMNS: dict[str, dict[str, Any]] = {
    "id": {
        "type": "integer",
        "nullable": False,
        "desc": "Identificador interno. No es estable entre bases.",
    },
    "timestamp_utc": {
        "type": "text",
        "nullable": True,
        "desc": "Instante normalizado, ISO-8601 UTC con milisegundos y sufijo Z. "
        "Todos los filtros temporales usan este campo.",
    },
    "timestamp_raw": {"type": "text", "nullable": True, "desc": "Texto original del log."},
    "source_tz": {
        "type": "text",
        "nullable": True,
        "desc": "Zona horaria declarada por el formato de origen.",
    },
    "level_raw": {
        "type": "text",
        "nullable": True,
        "desc": "Nivel tal como lo escribió la aplicación.",
    },
    "level_norm": {
        "type": "text",
        "nullable": True,
        "desc": "Nivel normalizado (DEBUG, INFO, WARN, ERROR, FATAL, UNKNOWN).",
    },
    "severity": {
        "type": "integer",
        "nullable": True,
        "desc": "0–50. UNKNOWN=0, DEBUG=10, INFO=20, WARN=30, ERROR=40, FATAL=50. "
        "Las comparaciones usan este entero, nunca el texto del nivel.",
    },
    "service": {
        "type": "text",
        "nullable": True,
        "desc": "Servicio de origen, cuando el formato lo aporta.",
    },
    "logger": {
        "type": "text",
        "nullable": True,
        "desc": "Logger o componente que emitió la línea.",
    },
    "message": {
        "type": "text",
        "nullable": True,
        "desc": "Mensaje del evento ya limpio de prefijos de formato.",
    },
    "fingerprint": {
        "type": "text",
        "nullable": True,
        "desc": "Hash de la plantilla normalizada (UUID/ip/números sustituidos). "
        "NULL en eventos sin mensaje. Es la unidad de agrupación de `top_errors`.",
    },
    "category": {
        "type": "text",
        "nullable": True,
        "desc": "Categoría asignada en la ingesta por `loglens://rules`. "
        "Congelada en el evento: cambiar las reglas no reescribe el histórico.",
    },
    "parsed_ok": {
        "type": "integer",
        "nullable": False,
        "desc": "0 si ningún patrón reconoció la línea.",
    },
    "parse_error": {
        "type": "text",
        "nullable": True,
        "desc": "Motivo del fallo de parseo, si lo hubo.",
    },
    "raw_line": {
        "type": "text",
        "nullable": False,
        "desc": "Línea original completa, preservada siempre.",
    },
}

_GLOSSARY: dict[str, Any] = {
    "table": "events",
    "columns": _EVENT_COLUMNS,
    "time_semantics": {
        "format": "YYYY-MM-DDTHH:MM:SS.mmmZ",
        "ranges": "Semiabiertos: [since, until). Un evento exactamente en `until` NO entra.",
        "ordering": "Comparar timestamps como texto ISO-8601 UTC es correcto; "
        "comparar `timestamp_raw` no lo es.",
    },
    "severity_scale": {
        "UNKNOWN": 0,
        "DEBUG": 10,
        "INFO": 20,
        "WARN": 30,
        "ERROR": 40,
        "FATAL": 50,
    },
    "fingerprint": {
        "definition": "sha256 de la plantilla del mensaje tras sustituir UUID, "
        "timestamps, IPs, hashes hex y números por marcadores.",
        "use": "Agrupa 'los mismos 100 fallos' en una unidad en lugar de cien.",
    },
    "counts": {
        "total_errors": "Errores de la ventana (severity >= 40), no todos los eventos.",
        "coverage": "parsed_ok / total. Una cobertura baja invalida conclusiones sobre categorías.",
    },
}


def schema_resource() -> str:
    """Esquema y glosario de `events`, en JSON."""
    return json.dumps(_GLOSSARY, ensure_ascii=False, indent=2, sort_keys=False)


def rules_resource() -> str:
    """Catálogo de reglas de clasificación, en orden de prioridad.

    El orden es la semántica: gana la primera regla que coincide, así que
    cambiar el orden cambia las categorías. Se expone explícitamente para que
    el agente pueda explicar por qué un mensaje cayó en una categoría y no en
    otra.
    """
    try:
        rules = ErrorRules.default()
    except Exception as exc:  # ConfigError, YAMLError, etc.
        raise ResourceError(f"no se pudieron cargar las reglas: {exc}") from exc

    payload = {
        "precedence": "first match wins; order is semantic",
        "catalog_frozen_at_ingest": True,
        "rules": [
            {
                "id": rule.id,
                "category": rule.category,
                "description": rule.description,
                "patterns": list(rule.patterns),
            }
            for rule in rules.rules
        ],
        "categories": list(rules.categories),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def incident_resource(incident_id: str) -> str:
    """Detalle de un incidente, con la misma forma que la tool `get_incident`."""
    try:
        parsed = int(incident_id)
    except (TypeError, ValueError) as exc:
        raise ResourceError(f"incident_id inválido: {incident_id!r}") from exc

    result = get_incident(parsed)
    if not result.found or result.incident is None:
        raise ResourceError(f"no existe el incidente {parsed}")
    return result.incident.model_dump_json(indent=2)


def trace_resource(run_id: str) -> str:
    """Traza JSONL de una corrida del agente.

    Se lee del disco y no de la base porque la traza es el artefacto completo:
    el JSONL es lo que se archiva junto al informe y lo que permite reconstruir
    la corrida paso a paso.
    """
    if not _RUN_ID.fullmatch(run_id):
        raise ResourceError(f"run_id inválido: {run_id!r}")

    directory = Path(os.environ.get("LOGLENS_TRACES_DIR", "traces"))
    path = directory / f"{run_id}.jsonl"
    if not path.is_file():
        raise ResourceError(f"no existe la traza {run_id!r} en {directory}")
    return path.read_text(encoding="utf-8")


__all__ = [
    "INCIDENT_URI_TEMPLATE",
    "RULES_URI",
    "SCHEMA_URI",
    "TRACE_URI_TEMPLATE",
    "incident_resource",
    "rules_resource",
    "schema_resource",
    "trace_resource",
]
