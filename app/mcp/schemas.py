"""Modelos de entrada y salida de las tools MCP.

Los tipos de este módulo son el contrato público del servidor. El SDK de MCP
genera el JSON Schema de entrada a partir de las anotaciones de las funciones de
tool, y valida la salida contra el modelo de retorno: por eso las tools devuelven
siempre un modelo de Pydantic y nunca un `dict` suelto.

Los modelos son deliberadamente planos. Un agente (o un LLM) lee mejor ocho
parámetros opcionales que un objeto anidado, y el coste de tokens de la
serialización es parte del presupuesto del harness de evaluación.

Convención de los rangos temporales: `since`/`until` son ISO-8601 en UTC y la
ventana es **semiabierta**, `[since, until)`. "Desde las 09:00 hasta las 12:00"
son las horas 09, 10 y 11, no las 09, 10, 11 y 12.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Literales compartidos. Un `Literal` en la anotación de retorno hace que el
# SDK genere un `enum` en el schema de salida: el cliente sabe qué valores son
# válidos sin tener que adivinarlos.

TrustLevel = Literal["high", "medium", "low"]
IncidentStatus = Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"]
Verdict = Literal["new", "elevated", "stable", "decreased"]
ActionState = Literal["PENDING", "APPROVED", "EXECUTED", "REJECTED", "EXPIRED"]


class WindowStats(BaseModel):
    """Estadísticas de una ventana temporal `[since, until)`.

    `stddev` es la desviación **poblacional** de los conteos por hora. Vale
    cero cuando la ventana es plana (el mismo número de eventos cada hora), y
    en ese caso el z-score no está definido: la comparación se apoya en el
    ratio.
    """

    label: str
    since: str | None = None
    until: str | None = None
    count: int = 0
    rate_per_hour: float = 0.0
    share_of_errors: float = 0.0
    distinct_services: int = 0
    hours: int = 0
    stddev: float = 0.0


class ParseErrorCount(BaseModel):
    parse_error: str
    count: int


class IngestRunHealth(BaseModel):
    id: int
    started_at: str
    finished_at: str | None = None
    status: str
    events_inserted: int = 0
    events_skipped: int = 0
    events_unparsed: int = 0
    duration_ms: int | None = None


class SystemHealthResult(BaseModel):
    """Estado global del dataset y de la ingesta.

    La cobertura de parseo es el indicador de pérdida de datos: por debajo de 0.80
    el dataset no sirve para diagnosticar, y el agente debe decirlo en vez de
    emitir un diagnóstico sobre datos incompletos. Por eso `trust_level` es un
    campo de primer orden y no una nota al pie.
    """

    total_events: int = 0
    parsed_events: int = 0
    unparsed_events: int = 0
    parse_coverage: float = 0.0
    top_parse_errors: list[ParseErrorCount] = Field(default_factory=list)
    first_event_ts: str | None = None
    last_event_ts: str | None = None
    active_log_files: int = 0
    last_ingest_run: IngestRunHealth | None = None
    trust_level: TrustLevel = "low"
    errors: int = 0
    fingerprints: int = 0


class EventRecord(BaseModel):
    """Evento individual. `event_id` y `fingerprint` son las referencias citables."""

    event_id: int
    timestamp_utc: str | None = None
    service: str | None = None
    severity: int = 0
    category: str | None = None
    fingerprint: str | None = None
    message: str | None = None
    parsed_ok: bool = True
    parse_error: str | None = None


class SearchEventsResult(BaseModel):
    events: list[EventRecord] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0
    has_more: bool = False


class TopErrorRecord(BaseModel):
    fingerprint: str
    category: str | None = None
    count: int = 0
    severity: int = 0
    sample_message: str | None = None
    service: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    share: float = 0.0


class TopErrorsResult(BaseModel):
    errors: list[TopErrorRecord] = Field(default_factory=list)
    total_errors: int = 0
    limit: int = 10


class AnomalyRecord(BaseModel):
    """Una hora con volumen anómalo.

    `z_score` es **nullable a propósito**: cuando la desviación de referencia
    era cero, el z-score no está definido y el motor lo devuelve como infinito.
    Pydantic serializa `inf` a `null` en modo JSON (`inf` no es JSON válido),
    así que el campo tiene que admitir `null` en el schema o el cliente
    rechazaría la respuesta al validarla. `is_infinite` dice cuál de los dos
    casos es.
    """

    bucket_start: str
    count: int = 0
    z_score: float | None = None
    mean: float = 0.0
    stddev: float = 0.0
    is_infinite: bool = False


class AnomaliesResult(BaseModel):
    anomalies: list[AnomalyRecord] = Field(default_factory=list)
    z_threshold: float = 3.0
    min_samples: int = 10
    buckets_examined: int = 0


class CompareWindowsResult(BaseModel):
    """Comparación de una ventana contra su baseline.

    Es la tool que evita que el agente haga aritmética: el motor entrega el ratio,
    el z-score y el veredicto ya calculados. Un agente que recibiera "412 errores
    ahora, 380 antes" tendría que decidir el ratio, y ese es justo el paso en el
    que los modelos fallan.
    """

    fingerprint: str
    category: str | None = None
    sample_message: str | None = None
    severity: int = 0
    window: WindowStats
    baseline: WindowStats
    ratio: float | None = None
    z_score: float | None = None
    verdict: Verdict = "stable"
    confidence: TrustLevel = "low"
    affected_services: list[str] = Field(default_factory=list)


class ServiceImpact(BaseModel):
    service: str
    count: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    share: float = 0.0


class TimeBucket(BaseModel):
    bucket_start: str
    count: int = 0
    services: list[str] = Field(default_factory=list)


class BlastRadiusResult(BaseModel):
    """Qué servicios cubre una huella y si el fallo se propaga.

    `cascade_note` es texto generado por el motor, no por un modelo: es
    determinista y por tanto citable tal cual.
    """

    fingerprint: str
    total_events: int = 0
    distinct_services: int = 0
    affected_services: list[ServiceImpact] = Field(default_factory=list)
    timeline: list[TimeBucket] = Field(default_factory=list)
    is_cascade: bool = False
    cascade_note: str = ""
    correlation_window_minutes: int = 15


class IncidentRecord(BaseModel):
    incident_id: int
    fingerprint: str
    category: str | None = None
    severity: int = 0
    status: IncidentStatus = "OPEN"
    event_count: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    sample_message: str | None = None


class ListIncidentsResult(BaseModel):
    incidents: list[IncidentRecord] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    limit: int = 20


class IncidentDetail(BaseModel):
    incident_id: int
    fingerprint: str
    category: str | None = None
    severity: int = 0
    status: IncidentStatus = "OPEN"
    event_count: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    sample_message: str | None = None
    normalized_template: str = ""
    evidence: list[EventRecord] = Field(default_factory=list)
    related_services: list[str] = Field(default_factory=list)
    related_anomalies: list[AnomalyRecord] = Field(default_factory=list)


class GetIncidentResult(BaseModel):
    incident: IncidentDetail | None = None
    found: bool = False


class ExplainResult(BaseModel):
    """Qué es una huella: plantilla normalizada, categoría y ejemplos reales."""

    fingerprint: str
    category: str | None = None
    category_description: str | None = None
    normalized_template: str = ""
    total_events: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    sample_raw_lines: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)


class PendingAction(BaseModel):
    """Acción que espera aprobación humana.

    **No lleva `confirm_token`**: el token es la prueba de la aprobación, así que
    no puede existir antes de que alguien apruebe. Si se emitiera aquí, el
    agente podría auto-aprobar su propia mutación.
    """

    action_id: str
    tool: str
    params: dict[str, object] = Field(default_factory=dict)
    status: ActionState = "PENDING"
    created_at: str = ""
    expires_at: str = ""
    approval_command: str = ""


class SetIncidentStatusResult(BaseModel):
    """Resultado de pedir un cambio de estado en un incidente.

    `status` describe en qué punto del flujo de aprobación está la petición:

    * `pending_approval` — registrada; falta la aprobación de una persona.
    * `executed` — aprobada y aplicada.

    Los demás casos (incidente inexistente, token que no autoriza, estado
    inválido) **no** son resultados: son errores, y llegan como `ToolError`.
    La mutación no ocurrió, y devolver un resultado con aspecto de éxito sería
    la forma más fácil de que un agente creyera que algo que no ocurrió sí
    ocurrió.
    """

    status: Literal["executed", "pending_approval"]
    incident_id: int | None = None
    incident_status: IncidentStatus | None = None
    pending_action: PendingAction | None = None
    message: str = ""


__all__ = [
    "ActionState",
    "AnomaliesResult",
    "AnomalyRecord",
    "BlastRadiusResult",
    "CompareWindowsResult",
    "EventRecord",
    "ExplainResult",
    "GetIncidentResult",
    "IncidentDetail",
    "IncidentRecord",
    "IngestRunHealth",
    "ListIncidentsResult",
    "ParseErrorCount",
    "PendingAction",
    "SearchEventsResult",
    "ServiceImpact",
    "SetIncidentStatusResult",
    "SystemHealthResult",
    "TimeBucket",
    "TopErrorRecord",
    "TopErrorsResult",
    "TrustLevel",
    "Verdict",
    "WindowStats",
]
