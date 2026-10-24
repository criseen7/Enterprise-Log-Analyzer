"""Ensamblado del servidor MCP.

Aquí se decide **qué ve el modelo**: los nombres de las tools, sus
descripciones y sus anotaciones. El resto de la capa MCP se encarga de que el
motor haga los cálculos y de que los fallos previsibles lleguen al modelo como
mensajes en vez de como crashes.

Diez tools, agrupadas por lo que hacen:

   (system_health)                     estado del dataset: ¿se puede diagnosticar?
    (search_events top_errors           explorar qué pasó
     detect_anomalies)
    (compare_windows blast_radius)      comparar y correlacionar
    (explain_fingerprint)               entender una huella
    (list_incidents get_incident)      seguir un incidente
    (set_incident_status)               actuar — con aprobación humana

La última es la única que muta, y por eso es la única que no se puede usar sin
que alguien la apruebe (ver `app/mcp/hitl.py`).
"""

from __future__ import annotations

from mcp.server import MCPServer

from app.mcp.prompts import (
    INVESTIGATE_SYMPTOM,
    WRITE_POSTMORTEM,
    investigate_symptom,
    write_postmortem,
)
from app.mcp.resources import (
    INCIDENT_URI_TEMPLATE,
    RULES_URI,
    SCHEMA_URI,
    TRACE_URI_TEMPLATE,
    incident_resource,
    rules_resource,
    schema_resource,
    trace_resource,
)
from app.mcp.tools import comparison, events, explain, incidents, system
from app.mcp.tools._base import MUTATING, READ_ONLY

SERVER_NAME = "loglens"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
LogLens analiza logs de producción almacenados en SQLite y responde preguntas
de fiabilidad: qué falla, desde cuándo, en qué servicios y si es una cascada.

Reglas de uso que ahorran llamadas:

1. Empieza por `system_health`. Su campo `trust_level` acota lo que puedes
   afirmar: con cobertura de parseo baja, un diagnóstico describe sólo la parte
   del log que se pudo leer, y hay que decirlo.
2. Los conteos, ratios, z-scores y veredictos ya están calculados por el motor.
   No recalcules aritmética a partir de los números que recibes: usa los
   veredictos (`verdict`, `is_cascade`) en lugar de derivarlos.
3. Las referencias citables son `event_id` y `fingerprint`. Cita siempre el
   `event_id` del que afirmas algo.
4. Si necesitas la forma de los datos o el catálogo de categorías, lee
   `loglens://schema` y `loglens://rules` en lugar de deducirlos de los
   resultados.
5. `set_incident_status` no aplica el cambio a la primera: registra la petición
   y espera aprobación humana. Enséñale al usuario el `action_id` y pídele que
   lo apruebe; después repite la llamada con el `confirm_token` que devuelve la
   aprobación.
6. Cada corrida del agente deja una traza completa en `loglens://trace/{run_id}`
   (llamadas, parámetros, hallazgos y coste). Si el usuario pregunta "¿por qué
   dijiste eso?", la respuesta está ahí, no en tu memoria.
"""


def build_server() -> MCPServer:
    """Construye el servidor con las diez tools registradas."""
    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
    )

    server.add_tool(
        system.system_health,
        name="system_health",
        title="Estado del dataset",
        description=(
            "Estado del dataset: cobertura de parseo, número de eventos, rango "
            "temporal y salud de la última ingesta. Devuelve `trust_level`, que "
            "acota lo que el resto de las tools pueden afirmar. Llámala primero."
        ),
        annotations=READ_ONLY,
    )

    server.add_tool(
        events.search_events,
        name="search_events",
        title="Buscar eventos",
        description=(
            "Busca eventos con filtros (servicio, categoría, texto libre, rango "
            "temporal, severidad mínima) y devuelve una página de más reciente a "
            "más antigua. La ventana es semiabierta [since, until). `total` es el "
            "número de coincidencias, no el de la página."
        ),
        annotations=READ_ONLY,
    )

    server.add_tool(
        events.top_errors,
        name="top_errors",
        title="Errores más repetidos",
        description=(
            "Los errores más repetidos agrupados por huella (fingerprint), con su "
            "conteo, categoría, severidad y fracción del total de errores de la "
            "ventana. La huella es lo que agrupa '100 accesos fallidos' en una "
            "unidad y no en cien."
        ),
        annotations=READ_ONLY,
    )

    server.add_tool(
        events.detect_anomalies,
        name="detect_anomalies",
        title="Horas anómalas",
        description=(
            "Horas con volumen de errores anormalmente alto, con su z-score. El "
            "motor usa z-score leave-one-out sobre la serie horaria rellenada "
            "con ceros, y descarta las horas con pocos eventos."
        ),
        annotations=READ_ONLY,
    )

    server.add_tool(
        comparison.compare_windows,
        name="compare_windows",
        title="Comparar ventana contra baseline",
        description=(
            "Compara una ventana de una huella contra su baseline y devuelve el "
            "ratio, el z-score y el veredicto ya calculados (`new`, `elevated`, "
            "`stable`, `decreased`) con un nivel de confianza. Sin baseline "
            "explícito, usa la ventana inmediatamente anterior del mismo largo. "
            "Úsala antes de afirmar que algo 'se ha disparado'."
        ),
        annotations=READ_ONLY,
    )

    server.add_tool(
        comparison.blast_radius,
        name="blast_radius",
        title="Radio de impacto",
        description=(
            "Qué servicios cubre una huella y si el fallo se propaga en cascada. "
            "Devuelve los servicios afectados, su línea de tiempo y `is_cascade`, "
            "más una nota determinista que explica el veredicto."
        ),
        annotations=READ_ONLY,
    )

    server.add_tool(
        explain.explain_fingerprint,
        name="explain_fingerprint",
        title="Explicar una huella",
        description=(
            "Explica una huella: plantilla normalizada del mensaje (con "
            "<UUID>, <IP>, <NUM> en lugar de los valores), categoría, descripción "
            "de la categoría y ejemplos de líneas reales. Convierte un hash "
            "opaco en una frase legible."
        ),
        annotations=READ_ONLY,
    )

    server.add_tool(
        incidents.list_incidents,
        name="list_incidents",
        title="Listar incidentes",
        description=(
            "Lista los incidentes, del más grave y más reciente al más antiguo, "
            "con el conteo por estado. Filtra por `status`: OPEN, "
            "ACKNOWLEDGED o RESOLVED."
        ),
        annotations=READ_ONLY,
    )

    server.add_tool(
        incidents.get_incident,
        name="get_incident",
        title="Detalle de un incidente",
        description=(
            "Detalle de un incidente: eventos de evidencia, servicios afectados, "
            "anomalías dentro de su ventana temporal y la plantilla normalizada "
            "del mensaje."
        ),
        annotations=READ_ONLY,
    )

    server.add_tool(
        incidents.set_incident_status,
        name="set_incident_status",
        title="Cambiar estado de un incidente",
        description=(
            "Cambia el estado de un incidente a OPEN, ACKNOWLEDGED o RESOLVED. "
            "Requiere aprobación humana: la primera llamada registra la "
            "petición y devuelve `pending_approval` con un `action_id`; un "
            "operador lo aprueba con `python -m app.mcp approve <action_id>`; "
            "después se repite la llamada con el `confirm_token` devuelto."
        ),
        annotations=MUTATING,
    )

    # --- Resources ---------------------------------------------------------
    # Documentos que el cliente puede adjuntar al contexto sin gastar una
    # llamada a tool. Los tres son de sólo lectura. Se registran con el
    # decorador porque `add_resource` espera un objeto `Resource` ya construido.
    @server.resource(
        SCHEMA_URI,
        name="schema",
        title="Esquema y glosario de events",
        description=(
            "Columnas de `events`, escala de severidad y semántica de tiempos y "
            "huellas. Léelo antes de interpretar cualquier cifra."
        ),
        mime_type="application/json",
    )
    def _schema() -> str:
        return schema_resource()

    @server.resource(
        RULES_URI,
        name="rules",
        title="Catálogo de reglas de clasificación",
        description=(
            "Reglas de `error_rules.yaml` en orden de prioridad. La primera que "
            "coincide gana; el orden es semántica."
        ),
        mime_type="application/json",
    )
    def _rules() -> str:
        return rules_resource()

    @server.resource(
        INCIDENT_URI_TEMPLATE,
        name="incident",
        title="Detalle de un incidente",
        description="Mismo contenido que `get_incident`, como documento.",
        mime_type="application/json",
    )
    def _incident(incident_id: str) -> str:
        return incident_resource(incident_id)

    @server.resource(
        TRACE_URI_TEMPLATE,
        name="trace",
        title="Traza de una corrida del agente",
        description=(
            "JSONL con cada plan, llamada, veredicto y diagnóstico de una "
            "corrida. Es el artefacto que permite auditar por qué se dijo algo."
        ),
        mime_type="application/jsonl",
    )
    def _trace(run_id: str) -> str:
        return trace_resource(run_id)

    # --- Prompts -----------------------------------------------------------
    @server.prompt(
        name=INVESTIGATE_SYMPTOM,
        title="Investigar un síntoma",
        description=(
            "Plantilla de investigación guiada: orden de tools, reglas de "
            "evidencia y formato de la respuesta."
        ),
    )
    def _investigate(
        symptom: str,
        service: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, object]]:
        return investigate_symptom(symptom, service=service, since=since, until=until)

    @server.prompt(
        name=WRITE_POSTMORTEM,
        title="Redactar un postmortem",
        description=(
            "Estructura de postmortem a partir de un incidente, con la regla de "
            "no atribuir causa raíz sin evidencia."
        ),
    )
    def _postmortem(incident_id: str, occurred_at: str | None = None) -> list[dict[str, object]]:
        return write_postmortem(incident_id, occurred_at=occurred_at)

    return server


def run(
    transport: str = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Arranca el servidor con el transport indicado."""
    server = build_server()
    if transport == "stdio":
        server.run(transport="stdio")
    elif transport == "streamable-http":
        server.run(transport="streamable-http", host=host, port=port)
    else:
        raise ValueError(
            f"transport desconocido: {transport!r}, esperado 'stdio' o 'streamable-http'"
        )


__all__ = ["INSTRUCTIONS", "SERVER_NAME", "SERVER_VERSION", "build_server", "run"]
