"""Agregaciones de métricas. Todo se calcula en SQL, nunca en Python.

Dos razones, y ninguna es micro-optimización:

* `events` crece sin límite. Cargarla entera en un DataFrame para contar errores
  por hora convierte un dashboard ágil en un proceso que muere con el primer
  dataset real, y SQLite ya tiene índices para esto (`idx_events_sev_ts`).
* Si el conteo vive en Python, dos pantallas pueden discrepar tras un filtro
  distinto. Una sola query con los mismos parámetros es la única forma de que
  "errores por hora" y "total de errores" digan lo mismo.

Los filtros de rango usan `[since, until)`: abierto a la derecha para que
"09:00 → 12:00" signifique las horas 09, 10 y 11 y no se cuele el mediodía.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.parser.severity import ERROR

# `?` y parámetros separados: ningún filtro se interpola en el SQL. Los valores
# son strings ISO o enteros, pero el hábito es el mismo aquí que en el resto.


def _range(since: str | None, until: str | None) -> tuple[str, list[str]]:
    """Cláusula WHERE de rango sobre `timestamp_utc` (+ `IS NOT NULL`)."""
    clauses: list[str] = []
    params: list[str] = []
    if since is not None:
        clauses.append("timestamp_utc >= ?")
        params.append(since)
    if until is not None:
        clauses.append("timestamp_utc < ?")
        params.append(until)
    if clauses:
        return " AND " + " AND ".join(clauses), params
    return "", []


@dataclass(frozen=True)
class Overview:
    events: int
    parsed: int
    unparsed: int
    errors: int
    fingerprints: int
    first_ts: str | None
    last_ts: str | None

    @property
    def coverage(self) -> float:
        return self.parsed / self.events if self.events else 0.0


def overview(
    conn: sqlite3.Connection, *, since: str | None = None, until: str | None = None
) -> Overview:
    """Totales del dataset. La cobertura es el indicador de pérdida de datos."""
    where, params = _range(since, until)
    row = conn.execute(
        f"""
        SELECT COUNT(*)                                        AS events,
               SUM(parsed_ok)                                  AS parsed,
               SUM(CASE WHEN parsed_ok = 0 THEN 1 ELSE 0 END)  AS unparsed,
               SUM(CASE WHEN severity >= ? THEN 1 ELSE 0 END)  AS errors,
               COUNT(DISTINCT fingerprint)                     AS fingerprints,
               MIN(timestamp_utc)                              AS first_ts,
               MAX(timestamp_utc)                              AS last_ts
        FROM events WHERE 1 = 1{where}
        """,
        [ERROR, *params],
    ).fetchone()
    return Overview(
        events=int(row["events"] or 0),
        parsed=int(row["parsed"] or 0),
        unparsed=int(row["unparsed"] or 0),
        errors=int(row["errors"] or 0),
        fingerprints=int(row["fingerprints"] or 0),
        first_ts=row["first_ts"],
        last_ts=row["last_ts"],
    )


def errors_by_hour(
    conn: sqlite3.Connection,
    *,
    min_severity: int = ERROR,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, int]:
    """Conteo de errores por hora, en la forma `{ '2026-10-05T09:00': n }`.

    Sólo aparecen las horas con al menos un error: no se rellenan los huecos
    porque la pregunta típica es "¿cuándo hubo errores?" y una hora vacía no es
    una respuesta. Para la serie completa (necesaria en anomalías) está
    `analysis.anomalies.hourly_counts`, que sí rellena.
    """
    where, params = _range(since, until)
    rows = conn.execute(
        f"""
        SELECT substr(timestamp_utc, 1, 13) || ':00' AS bucket, COUNT(*) AS n
        FROM events
        WHERE severity >= ? AND timestamp_utc IS NOT NULL{where}
        GROUP BY bucket
        ORDER BY bucket
        """,
        [min_severity, *params],
    ).fetchall()
    return {row["bucket"]: int(row["n"]) for row in rows}


@dataclass(frozen=True)
class TopError:
    fingerprint: str
    category: str | None
    count: int
    severity: int | None
    sample_message: str | None
    service: str | None
    first_seen: str | None
    last_seen: str | None


def top_errors(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    min_severity: int = ERROR,
    min_count: int = 1,
    since: str | None = None,
    until: str | None = None,
) -> list[TopError]:
    """Errores agrupados por fingerprint, del más repetido al menos.

    El `message` de la fila es el del evento en el que `MAX(message)` ocurrió,
    que en SQLite es una propiedad documentada de los bare columns junto a un
    agregado mínimo/máximo: sirve de muestra representativa sin una subquery
    más.
    """
    where, params = _range(since, until)
    rows = conn.execute(
        f"""
        SELECT fingerprint,
               MAX(category)   AS category,
               MAX(severity)   AS severity,
               MAX(message)    AS sample_message,
               MAX(service)    AS service,
               COUNT(*)        AS n,
               MIN(timestamp_utc) AS first_seen,
               MAX(timestamp_utc) AS last_seen
        FROM events
        WHERE severity >= ?
          AND fingerprint IS NOT NULL
          AND timestamp_utc IS NOT NULL{where}
        GROUP BY fingerprint
        HAVING COUNT(*) >= ?
        ORDER BY n DESC, first_seen ASC
        LIMIT ?
        """,
        [min_severity, *params, min_count, limit],
    ).fetchall()
    return [
        TopError(
            fingerprint=row["fingerprint"],
            category=row["category"],
            count=int(row["n"]),
            severity=row["severity"],
            sample_message=row["sample_message"],
            service=row["service"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class SeverityBucket:
    bucket: str
    severity: int
    count: int


def timeline_by_severity(
    conn: sqlite3.Connection,
    *,
    granularity: str = "hour",
    since: str | None = None,
    until: str | None = None,
) -> list[SeverityBucket]:
    """Serie temporal apilada por severidad. `granularity` es hour | day."""
    if granularity not in {"hour", "day"}:
        raise ValueError(f"granularidad no soportada: {granularity!r}")
    length = 13 if granularity == "hour" else 10  # 2026-10-05T14 / 2026-10-05
    where, params = _range(since, until)
    rows = conn.execute(
        f"""
        SELECT substr(timestamp_utc, 1, ?) AS bucket, severity, COUNT(*) AS n
        FROM events
        WHERE severity IS NOT NULL AND timestamp_utc IS NOT NULL{where}
        GROUP BY bucket, severity
        ORDER BY bucket, severity
        """,
        [length, *params],
    ).fetchall()
    return [
        SeverityBucket(bucket=row["bucket"], severity=int(row["severity"]), count=int(row["n"]))
        for row in rows
    ]


@dataclass(frozen=True)
class DayHour:
    day: int  # 0 = lunes .. 6 = domingo (strftime %w es domingo=0; se normaliza)
    hour: int
    count: int


def heatmap_day_hour(
    conn: sqlite3.Connection,
    *,
    min_severity: int = ERROR,
    since: str | None = None,
    until: str | None = None,
) -> list[DayHour]:
    """Mapa de calor día×hora en SQLite, no en pandas.

    `strftime('%w')` devuelve 0 para domingo; se pasa a 0=lunes con
    `CASE WHEN CAST(strftime('%w') AS INTEGER) = 0 THEN 6 ELSE ... - 1`.
    """
    where, params = _range(since, until)
    rows = conn.execute(
        f"""
        SELECT CASE CAST(strftime('%w', timestamp_utc) AS INTEGER)
                 WHEN 0 THEN 6 ELSE CAST(strftime('%w', timestamp_utc) AS INTEGER) - 1
               END AS day,
               CAST(substr(timestamp_utc, 12, 2) AS INTEGER) AS hour,
               COUNT(*) AS n
        FROM events
        WHERE severity >= ? AND timestamp_utc IS NOT NULL{where}
        GROUP BY day, hour
        ORDER BY day, hour
        """,
        [min_severity, *params],
    ).fetchall()
    return [DayHour(day=int(row["day"]), hour=int(row["hour"]), count=int(row["n"])) for row in rows]


@dataclass(frozen=True)
class CategoryCount:
    category: str | None
    count: int


def by_category(
    conn: sqlite3.Connection,
    *,
    min_severity: int = ERROR,
    since: str | None = None,
    until: str | None = None,
) -> list[CategoryCount]:
    """Distribución de errores por categoría (`error_rules.yaml`).

    Los sin categoría salen como `None`: es una respuesta honesta y es la que
    permite ver que una regla dejó de aplicar a algo que antes cubría.
    """
    where, params = _range(since, until)
    rows = conn.execute(
        f"""
        SELECT category, COUNT(*) AS n
        FROM events
        WHERE severity >= ? AND timestamp_utc IS NOT NULL{where}
        GROUP BY category
        ORDER BY n DESC
        """,
        [min_severity, *params],
    ).fetchall()
    return [CategoryCount(category=row["category"], count=int(row["n"])) for row in rows]


def by_service(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    min_severity: int = ERROR,
    since: str | None = None,
    until: str | None = None,
) -> list[CategoryCount]:
    """Top servicios que generan errores. Reutiliza `CategoryCount`: mismo formato."""
    where, params = _range(since, until)
    rows = conn.execute(
        f"""
        SELECT service, COUNT(*) AS n
        FROM events
        WHERE severity >= ? AND timestamp_utc IS NOT NULL{where}
        GROUP BY service
        ORDER BY n DESC
        LIMIT ?
        """,
        [min_severity, *params, limit],
    ).fetchall()
    return [CategoryCount(category=row["service"], count=int(row["n"])) for row in rows]


__all__ = [
    "CategoryCount",
    "DayHour",
    "Overview",
    "SeverityBucket",
    "TopError",
    "by_category",
    "by_service",
    "errors_by_hour",
    "heatmap_day_hour",
    "overview",
    "timeline_by_severity",
    "top_errors",
]
