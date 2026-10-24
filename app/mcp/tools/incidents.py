"""Tools de incidentes: `list_incidents`, `get_incident`, `set_incident_status`.

Las dos primeras leen. La tercera **muta**, y por eso es la única que pasa por
aprobación humana.

El motivo de que exista una tool que cambia el estado de un incidente y no
basta con "el agente propone y el humano lo hace a mano": la propuesta tiene que
ser **ejecutable** para que el diagnóstico y la acción formen un ciclo
cerrado, y el ciclo cerrado tiene que ser auditable. Por eso la mutación deja
rastro en `agent_actions`: qué se pidió, quién lo aprobó y cuándo se ejecutó.
"""

from __future__ import annotations

import sqlite3

from mcp.server.mcpserver.exceptions import ToolError

from app.alerts.incidents import (
    VALID_STATUSES,
    incident_counts,
)
from app.alerts.incidents import (
    get_incident as engine_get_incident,
)
from app.alerts.incidents import (
    list_incidents as engine_list_incidents,
)
from app.alerts.incidents import (
    set_incident_status as engine_set_incident_status,
)
from app.analysis.anomalies import anomalies as detect
from app.analysis.fingerprint import normalize_message
from app.mcp import hitl
from app.mcp._context import read_connection, write_connection
from app.mcp.schemas import (
    AnomalyRecord,
    EventRecord,
    GetIncidentResult,
    IncidentDetail,
    IncidentRecord,
    ListIncidentsResult,
    PendingAction,
    SetIncidentStatusResult,
)
from app.mcp.tools._base import guard
from app.parser.severity import ERROR

# Máximo de incidentes devueltos en una página.
MAX_LIMIT = 200
# Eventos de evidencia que acompañan a un incidente. Suficiente para ver el
# patrón; no para volcar el histórico.
MAX_EVIDENCE = 20


def _to_incident(row: sqlite3.Row) -> IncidentRecord:
    return IncidentRecord(
        incident_id=int(row["id"]),
        fingerprint=str(row["fingerprint"]),
        category=row["category"],
        severity=int(row["severity"] or 0),
        status=row["status"],
        event_count=int(row["event_count"] or 0),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        sample_message=row["sample_message"],
    )


def _list(conn: sqlite3.Connection, status: str | None, limit: int) -> ListIncidentsResult:
    """Lista los incidentes.

    La validación va **dentro** de la función guardada a propósito: si se
    lanzara `ValueError` fuera del `guard`, el SDK la trataría como un crash
    y el modelo recibiría un mensaje genérico en vez de saber qué parámetro
    está mal.
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status inválido: {status!r}, esperado uno de {VALID_STATUSES}")
    if limit <= 0 or limit > MAX_LIMIT:
        raise ValueError(f"limit debe estar entre 1 y {MAX_LIMIT}")
    rows = engine_list_incidents(conn, status=status, limit=limit)
    counts = incident_counts(conn)
    return ListIncidentsResult(
        incidents=[_to_incident(row) for row in rows],
        counts=counts,
        limit=limit,
    )


def list_incidents(status: str | None = None, limit: int = 20) -> ListIncidentsResult:
    """Lista los incidentes, del más grave y más reciente al más antiguo.

    `status` filtra por `OPEN`, `ACKNOWLEDGED` o `RESOLVED`. `counts` trae el
    total por estado, así que el cliente puede pintar el resumen sin una
    segunda llamada.
    """
    with read_connection() as conn:
        return guard(_list, conn, status, limit)


def _detail(conn: sqlite3.Connection, incident_id: int) -> GetIncidentResult:
    if incident_id <= 0:
        raise ValueError("incident_id debe ser > 0")
    row = engine_get_incident(conn, incident_id)
    if row is None:
        return GetIncidentResult(incident=None, found=False)

    fingerprint = str(row["fingerprint"])

    evidence = [
        EventRecord(
            event_id=int(item["id"]),
            timestamp_utc=item["timestamp_utc"],
            service=item["service"],
            severity=int(item["severity"] or 0),
            category=item["category"],
            fingerprint=item["fingerprint"],
            message=item["message"],
            parsed_ok=bool(item["parsed_ok"]),
            parse_error=item["parse_error"],
        )
        for item in conn.execute(
            """
            SELECT id, timestamp_utc, service, severity, category, fingerprint,
                   message, parsed_ok, parse_error
              FROM events
             WHERE fingerprint = ?
             ORDER BY timestamp_utc DESC, id DESC
             LIMIT ?
            """,
            (fingerprint, MAX_EVIDENCE),
        )
    ]

    services = [
        str(item["service"])
        for item in conn.execute(
            """
            SELECT service FROM events
             WHERE fingerprint = ? AND service IS NOT NULL
             GROUP BY service ORDER BY COUNT(*) DESC
            """,
            (fingerprint,),
        )
    ]

    # Las anomalías se limitan a la ventana del incidente: fuera de ella, un
    # pico no tiene nada que ver con este incidente.
    related = detect(
        conn,
        z_threshold=3.0,
        min_samples=10,
        min_severity=ERROR,
        since=row["first_seen"],
        until=row["last_seen"],
    )

    incident = IncidentDetail(
        incident_id=int(row["id"]),
        fingerprint=fingerprint,
        category=row["category"],
        severity=int(row["severity"] or 0),
        status=row["status"],
        event_count=int(row["event_count"] or 0),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        sample_message=row["sample_message"],
        normalized_template=normalize_message(row["sample_message"]),
        evidence=evidence,
        related_services=services,
        related_anomalies=[
            AnomalyRecord(
                bucket_start=item.bucket,
                count=item.count,
                z_score=round(item.zscore, 4),
                mean=round(item.mean, 4),
                stddev=round(item.stdev, 4),
                is_infinite=item.is_infinite,
            )
            for item in related
        ],
    )
    return GetIncidentResult(incident=incident, found=True)


def get_incident(incident_id: int) -> GetIncidentResult:
    """Detalle de un incidente: eventos de evidencia, servicios afectados,
    anomalías dentro de su ventana y la plantilla normalizada del mensaje.

    Devuelve `found=False` si no existe: un id inexistente no es un error, es
    un vacío, y el agente necesita distinguirlo de un fallo.
    """
    with read_connection() as conn:
        return guard(_detail, conn, incident_id)


def _pending(action: hitl.Action) -> PendingAction:
    """La acción pendiente tal como se le entrega al agente."""
    return PendingAction(
        action_id=action.id,
        tool=action.tool,
        params=dict(action.params),
        status=action.status,  # type: ignore[arg-type]
        created_at=action.created_at,
        expires_at=action.expires_at,
        approval_command=(f"python -m app.mcp approve {action.id}"),
    )


def _set_status(
    conn: sqlite3.Connection,
    incident_id: int,
    status: str,
    confirm_token: str | None,
) -> SetIncidentStatusResult:
    """Ejecuta o registra el cambio de estado de un incidente."""
    if status not in VALID_STATUSES:
        raise ValueError(f"status inválido: {status!r}, esperado uno de {VALID_STATUSES}")
    if incident_id <= 0:
        raise ValueError("incident_id debe ser > 0")

    row = engine_get_incident(conn, incident_id)
    if row is None:
        raise ToolError(f"no existe el incidente {incident_id}")

    params = {"incident_id": incident_id, "status": status}

    if confirm_token is None:
        action = hitl.create_action(
            conn,
            tool="set_incident_status",
            params=params,
        )
        return SetIncidentStatusResult(
            status="pending_approval",
            incident_id=incident_id,
            incident_status=row["status"],
            pending_action=_pending(action),
            message=(
                "El cambio está pendiente de aprobación humana. "
                f"Para aprobarlo: `python -m app.mcp approve {action.id}`. "
                "El comando devuelve un confirm_token; pásalo de vuelta en "
                "la llamada siguiente con confirm_token=..."
            ),
        )

    # El token tiene que autorizar *esta* acción: mismo id, misma tool,
    # mismos parámetros y no caducada.
    approved = _find_approved_action(conn, params, confirm_token)
    if approved is None:
        raise ToolError(
            "el confirm_token no autoriza esta acción. Puede haber caducado, "
            "ser de otra acción o no existir."
        )

    changed = engine_set_incident_status(conn, incident_id, status)
    if not changed:
        raise ToolError(f"no se pudo cambiar el estado del incidente {incident_id}")
    hitl.mark_executed(conn, approved.id, result=f"incident {incident_id} -> {status}")

    return SetIncidentStatusResult(
        status="executed",
        incident_id=incident_id,
        incident_status=status,  # type: ignore[arg-type]
        message=(
            f"incidente {incident_id} pasó a {status} "
            f"(aprobado por {approved.approved_by or 'operator'})."
        ),
    )


def _find_approved_action(
    conn: sqlite3.Connection,
    params: dict[str, object],
    token: str,
) -> hitl.Action | None:
    """Busca la acción aprobada cuyo token verifique contra estos parámetros.

    Se recorren las acciones aprobadas y se verifican una a una: el token es
    un HMAC y no se puede indexar por él, pero el conjunto de acciones
    aprobadas es pequeño y está acotado por la caducidad.
    """
    secret = hitl.load_secret()
    for action in hitl.list_actions(conn, status=hitl.APPROVED, limit=200):
        if action.tool != "set_incident_status":
            continue
        if action.params != params:
            continue
        if hitl.verify(
            action,
            tool="set_incident_status",
            params=params,
            token=token,
            secret=secret,
        ):
            return action
    return None


def set_incident_status(
    incident_id: int,
    status: str,
    confirm_token: str | None = None,
) -> SetIncidentStatusResult:
    """Cambia el estado de un incidente (`OPEN`, `ACKNOWLEDGED` o `RESOLVED`).

    **Esta tool no muta a la primera.** Registra la petición y devuelve
    `status="pending_approval"` con un `action_id`. Un humano la aprueba desde
    su terminal:

        python -m app.mcp approve <action_id>

    ese comando devuelve un `confirm_token`, que es la prueba de la aprobación.
    Al volver a llamar con `confirm_token=...`, la mutación se ejecuta y el
    resultado es `status="executed"`.

    El token es un HMAC sobre la acción completa, con caducidad, y sólo lo
    emite quien aprueba. Por eso el agente no puede auto-aprobar su propia
    mutación: no tiene la clave, y aunque la tuviera, el token no valdría para
    otra acción.
    """
    with write_connection() as conn:
        return guard(_set_status, conn, incident_id, status, confirm_token)


__all__ = ["get_incident", "list_incidents", "set_incident_status"]
