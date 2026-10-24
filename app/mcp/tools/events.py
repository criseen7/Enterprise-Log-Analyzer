"""Tools de exploración: `search_events`, `top_errors`, `detect_anomalies`.

Las tres leen del motor y nada más. En particular, `detect_anomalies` no
"detecta anomalías" por su cuenta: llama a `analysis.anomalies.anomalies`, que
usa el z-score leave-one-out sobre la serie horaria rellenada, y devuelve
exactamente lo que el motor calculó.

Lo que la capa MCP añade aquí es el **formato del contrato**: los bounds
temporales se normalizan, los resultados se ordenan y se recortan, y los
recuentos vienen con el contexto necesario para no malinterpretarlos (`total`
junto a la página, `share` junto al conteo).
"""

from __future__ import annotations

import sqlite3
from typing import TypedDict

from app.analysis.anomalies import anomalies as detect
from app.analysis.metrics import top_errors as engine_top_errors
from app.mcp._context import read_connection, resolve_bounds
from app.mcp.schemas import (
    AnomaliesResult,
    AnomalyRecord,
    EventRecord,
    SearchEventsResult,
    TopErrorRecord,
    TopErrorsResult,
)
from app.mcp.tools._base import guard
from app.parser.severity import ERROR
from app.ui.data import EventFilters, count_events_filtered, fetch_events

# Tope duro de página. Sin él, un agente que pide `limit=100000` se lleva el
# dataset entero en el contexto y la sesión se queda sin ventana.
MAX_LIMIT = 500


class _ErrorRow(TypedDict):
    """Fila normalizada de error agrupado.

    El motor devuelve dataclasses y la consulta SQL devuelve `sqlite3.Row`;
    ambos caminos se normalizan aquí antes de construir el modelo Pydantic.
    """

    fingerprint: str
    category: str | None
    severity: int | None
    sample_message: str | None
    service: str | None
    n: int
    first_seen: str | None
    last_seen: str | None


def _to_event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        event_id=int(row["id"]),
        timestamp_utc=row["timestamp_utc"],
        service=row["service"],
        severity=int(row["severity"] or 0),
        category=row["category"],
        fingerprint=row["fingerprint"],
        message=row["message"],
        parsed_ok=bool(row["parsed_ok"]),
        parse_error=row["parse_error"],
    )


def _search(
    conn: sqlite3.Connection,
    *,
    service: str | None,
    category: str | None,
    search: str | None,
    since: str | None,
    until: str | None,
    min_severity: int,
    limit: int,
    offset: int,
    unparsed_only: bool,
) -> SearchEventsResult:
    """Los eventos que cumplen los filtros, con su total.

    La validación va dentro de la función guardada: si un `ValueError` se
    lanzara fuera del `guard`, el SDK lo trataría como un crash y el modelo
    vería un mensaje genérico en vez de saber qué parámetro está mal.
    """
    if limit <= 0 or limit > MAX_LIMIT:
        raise ValueError(f"limit debe estar entre 1 y {MAX_LIMIT}")
    if offset < 0:
        raise ValueError("offset no puede ser negativo")
    lo, hi = resolve_bounds(since, until)
    filters = EventFilters(
        since=lo,
        until=hi,
        min_severity=min_severity,
        service=service,
        category=category,
        search=search,
        unparsed_only=unparsed_only,
    )
    total = count_events_filtered(conn, filters)
    rows = fetch_events(conn, filters, limit=limit, offset=offset)
    return SearchEventsResult(
        events=[_to_event(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
    )


def search_events(
    service: str | None = None,
    category: str | None = None,
    search: str | None = None,
    since: str | None = None,
    until: str | None = None,
    min_severity: int = ERROR,
    limit: int = 50,
    offset: int = 0,
    unparsed_only: bool = False,
) -> SearchEventsResult:
    """Busca eventos con filtros y devuelve una página, de más reciente a más
    antigua.

    La ventana es semiabierta: `since=2026-10-05T09:00:00Z` y
    `until=2026-10-05T12:00:00Z` incluyen las horas 09, 10 y 11, y excluyen
    la 12. `total` es el número de eventos que cumplen los filtros, no el de
    la página: sin él no hay forma de saber que faltan.

    `search` busca en el mensaje y en la línea original, con los comodines de
    LIKE escapados: buscar `100%` encuentra el 100 %, no todo.
    """
    with read_connection() as conn:
        return guard(
            _search,
            conn,
            service=service,
            category=category,
            search=search,
            since=since,
            until=until,
            min_severity=min_severity,
            limit=limit,
            offset=offset,
            unparsed_only=unparsed_only,
        )


def _grouped_rows(
    conn: sqlite3.Connection,
    *,
    lo: str | None,
    hi: str | None,
    service: str | None,
    category: str | None,
    limit: int,
) -> list[_ErrorRow]:
    """Errores agrupados por huella, con los filtros de servicio y categoría.

    El motor no expone esta consulta porque sus filtros son de tiempo, no de
    dimensión: agrupar por huella y filtrar por servicio son dos operaciones
    distintas y el motor sólo hace la primera.
    """
    clauses = ["fingerprint IS NOT NULL"]
    params: list[object] = []
    if lo:
        clauses.append("timestamp_utc >= ?")
        params.append(lo)
    if hi:
        clauses.append("timestamp_utc < ?")
        params.append(hi)
    if service:
        clauses.append("service = ?")
        params.append(service)
    if category:
        clauses.append("category = ?")
        params.append(category)
    where = " AND ".join(clauses)
    fetched = conn.execute(
        f"""
        SELECT fingerprint,
               MAX(category)       AS category,
               MAX(severity)       AS severity,
               MAX(message)        AS sample_message,
               MAX(service)        AS service,
               COUNT(*)            AS n,
               MIN(timestamp_utc)  AS first_seen,
               MAX(timestamp_utc)  AS last_seen
          FROM events
         WHERE {where}
         GROUP BY fingerprint
         ORDER BY n DESC, first_seen ASC
         LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [
        {
            "fingerprint": row["fingerprint"],
            "category": row["category"],
            "severity": row["severity"],
            "sample_message": row["sample_message"],
            "service": row["service"],
            "n": int(row["n"]),
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
        }
        for row in fetched
    ]


def _top_errors(
    conn: sqlite3.Connection,
    *,
    limit: int,
    since: str | None,
    until: str | None,
    service: str | None,
    category: str | None,
) -> TopErrorsResult:
    """Los errores agrupados por huella, con su conteo y su fracción.

    La validación va dentro de la función guardada (ver `search_events`).
    """
    if limit <= 0 or limit > MAX_LIMIT:
        raise ValueError(f"limit debe estar entre 1 y {MAX_LIMIT}")
    lo, hi = resolve_bounds(since, until)

    # `metrics.top_errors` agrupa por huella sin filtros de servicio ni de
    # categoría: esos filtros se aplican sobre el conjunto, no dentro del
    # agrupamiento. Cuando vienen, la consulta se hace aquí; cuando no, se
    # delega en el motor. En los dos casos las filas se normalizan al mismo
    # dict, porque el motor devuelve dataclasses y la consulta devuelve
    # `sqlite3.Row`, y mezclarlos a mitad de camino es un bug esperando.
    if service or category:
        rows: list[_ErrorRow] = _grouped_rows(
            conn, lo=lo, hi=hi, service=service, category=category, limit=limit
        )
    else:
        rows = [
            {
                "fingerprint": item.fingerprint,
                "category": item.category,
                "severity": item.severity,
                "sample_message": item.sample_message,
                "service": item.service,
                "n": item.count,
                "first_seen": item.first_seen,
                "last_seen": item.last_seen,
            }
            for item in engine_top_errors(conn, limit=limit, since=lo, until=hi)
        ]

    total_errors = 0
    count_row = conn.execute(
        """
        SELECT COUNT(*) FROM events
         WHERE severity >= ? AND fingerprint IS NOT NULL
           AND (? IS NULL OR timestamp_utc >= ?)
           AND (? IS NULL OR timestamp_utc < ?)
           AND (? IS NULL OR service = ?)
           AND (? IS NULL OR category = ?)
        """,
        (ERROR, lo, lo, hi, hi, service, service, category, category),
    ).fetchone()
    if count_row:
        total_errors = int(count_row[0])

    records = [
        TopErrorRecord(
            fingerprint=str(row["fingerprint"]),
            category=row["category"],
            count=int(row["n"]),
            severity=int(row["severity"] or 0),
            sample_message=row["sample_message"],
            service=row["service"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            share=(int(row["n"]) / total_errors) if total_errors else 0.0,
        )
        for row in rows
    ]

    return TopErrorsResult(errors=records, total_errors=total_errors, limit=limit)


def top_errors(
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
    service: str | None = None,
    category: str | None = None,
) -> TopErrorsResult:
    """Los errores más repetidos, agrupados por huella (fingerprint).

    La huella es lo que hace que esto sea útil: agrupa "100 accesos fallidos"
    como **una** unidad y no como cien. `share` es la fracción del total de
    errores de la ventana que representa cada huella, y `total_errors` el
    denominador.
    """
    with read_connection() as conn:
        return guard(
            _top_errors,
            conn,
            limit=limit,
            since=since,
            until=until,
            service=service,
            category=category,
        )


def _anomalies(
    conn: sqlite3.Connection,
    *,
    z_threshold: float,
    min_samples: int,
    since: str | None,
    until: str | None,
) -> AnomaliesResult:
    """Horas con volumen de errores anormalmente alto.

    La validación va dentro de la función guardada (ver `search_events`).
    """
    if z_threshold <= 0:
        raise ValueError("z_threshold debe ser > 0")
    if min_samples < 0:
        raise ValueError("min_samples no puede ser negativo")
    lo, hi = resolve_bounds(since, until)
    found = detect(
        conn,
        z_threshold=z_threshold,
        min_samples=min_samples,
        min_severity=ERROR,
        since=lo,
        until=hi,
    )
    records = [
        AnomalyRecord(
            bucket_start=item.bucket,
            count=item.count,
            z_score=item.zscore,
            mean=round(item.mean, 4),
            stddev=round(item.stdev, 4),
            is_infinite=item.is_infinite,
        )
        for item in found
    ]
    buckets = len({item.bucket for item in found})
    return AnomaliesResult(
        anomalies=records,
        z_threshold=z_threshold,
        min_samples=min_samples,
        buckets_examined=buckets,
    )


def detect_anomalies(
    z_threshold: float = 3.0,
    min_samples: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> AnomaliesResult:
    """Horas con un volumen de errores anormalmente alto.

    El motor usa el z-score **leave-one-out** sobre la serie horaria rellenada
    con ceros: una hora vacía cuenta como cero y no se la salta, porque
    saltársela inflaría la media de referencia y escondería precisamente el
    hueco que se está buscando.

    `min_samples` descarta las horas con pocos eventos: tres errores frente a
    cero son ruido, no un pico. `is_infinite` marca los casos en que la
    desviación de referencia era cero —el z-score no está definido y el motor
    devuelve infinito en vez de NaN.
    """
    with read_connection() as conn:
        return guard(
            _anomalies,
            conn,
            z_threshold=z_threshold,
            min_samples=min_samples,
            since=since,
            until=until,
        )


__all__ = ["detect_anomalies", "search_events", "top_errors"]
