"""Radio de impacto: qué servicios cubre una huella y si el fallo se propaga.

La pregunta que responde: "¿esto es un problema aislado de un servicio, o es una
cascada?" La respuesta cambia por completo la acción: un incidente aislado se
atiende en su servicio, una cascada se atiende en el que la origina.

Detectar la cascada a mano es trabajo de correlación: leer los logs de cada
servicio y buscar que sus ventanas de fallo se solapen. Eso es exactamente lo que
un agente hace mal, porque comparar timestamps en texto libre no es aritmética
que un modelo haga bien.

Aquí se decide con la base: se agrupa por servicio, se toman las ventanas de
fallo de cada uno, y se mide la distancia entre ellas. Si dos servicios fallan
dentro de la ventana de correlación, hay propagación.

La `cascade_note` es texto generado por el motor, no por un modelo: es
determinista y citable tal cual.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.analysis.anomalies import _enumerate_hours, _floor_hour
from app.timeutil import parse_utc as _parse

# Ventana de correlación por defecto, en minutos.
DEFAULT_CORRELATION_MINUTES = 15


@dataclass(frozen=True)
class ServiceImpact:
    service: str
    count: int
    first_seen: str | None
    last_seen: str | None
    share: float


@dataclass(frozen=True)
class TimeBucket:
    bucket_start: str
    count: int
    services: tuple[str, ...]


@dataclass(frozen=True)
class BlastRadius:
    fingerprint: str
    total_events: int
    distinct_services: int
    affected_services: tuple[ServiceImpact, ...]
    timeline: tuple[TimeBucket, ...]
    is_cascade: bool
    cascade_note: str
    correlation_window_minutes: int


def _gap_minutes(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> float | None:
    """Minutos entre dos intervalos `[inicio, fin]`. `None` si no se pueden ordenar.

    Solapados → 0. Si uno termina antes de que empiece el otro, la distancia es
    el hueco entre el fin del primero y el inicio del segundo.
    """
    (a0, a1), (b0, b1) = a, b
    if a1 < b0:
        return (b0 - a1).total_seconds() / 60.0
    if b1 < a0:
        return (a0 - b1).total_seconds() / 60.0
    return 0.0


def _timeline(
    conn: sqlite3.Connection, fingerprint: str, since: str, until: str
) -> tuple[TimeBucket, ...]:
    """Serie horaria de la huella con los servicios presentes en cada hora."""
    lo_dt, hi_dt = _parse(since), _parse(until)
    if lo_dt is None or hi_dt is None:
        return ()
    start = _floor_hour(lo_dt)
    # `until` es exclusivo: la última hora cubierta es la anterior a la que
    # marca el borde, no una hora vacía por construcción.
    end = _floor_hour(hi_dt - timedelta(microseconds=1))
    hours = _enumerate_hours(start, end)
    if not hours:
        return ()

    rows = conn.execute(
        """
        SELECT substr(timestamp_utc, 1, 13) || ':00' AS bucket,
               service,
               COUNT(*) AS n
        FROM events
        WHERE fingerprint = ? AND timestamp_utc >= ? AND timestamp_utc < ?
          AND service IS NOT NULL
        GROUP BY bucket, service
        """,
        (fingerprint, since, until),
    ).fetchall()

    per_bucket: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = str(row["bucket"])
        service = str(row["service"])
        per_bucket.setdefault(bucket, {})[service] = int(row["n"])

    return tuple(
        TimeBucket(
            # El bucket se calcula con el formato corto de `_enumerate_hours`
            # (`YYYY-MM-DDTHH:00`) para poder cruzarlo con la consulta, pero se
            # publica en el formato canónico completo: es un timestamp citable,
            # y el contrato no admite dos formatos para lo mismo.
            bucket_start=f"{hour}:00.000Z",
            count=sum(per_bucket.get(hour, {}).values()),
            services=tuple(sorted(per_bucket.get(hour, {}))),
        )
        for hour in hours
    )


def blast_radius(
    conn: sqlite3.Connection,
    fingerprint: str,
    *,
    since: str | None = None,
    until: str | None = None,
    correlation_window_minutes: int = DEFAULT_CORRELATION_MINUTES,
) -> BlastRadius | None:
    """Radio de impacto de una huella en una ventana.

    Devuelve `None` si la huella no tiene eventos en la ventana. Un radio de
    impacto vacío no es "cero impacto": es "no hay datos", y el llamante tiene que
    poder distinguir los dos casos.
    """
    clauses = ["fingerprint = ?", "timestamp_utc IS NOT NULL"]
    params: list[object] = [fingerprint]
    if since is not None:
        clauses.append("timestamp_utc >= ?")
        params.append(since)
    if until is not None:
        clauses.append("timestamp_utc < ?")
        params.append(until)
    where = " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT service,
               COUNT(*)             AS n,
               MIN(timestamp_utc)   AS first_seen,
               MAX(timestamp_utc)   AS last_seen
        FROM events
        WHERE {where} AND service IS NOT NULL
        GROUP BY service
        ORDER BY n DESC
        """,
        params,
    ).fetchall()

    if not rows:
        return None

    total = sum(int(row["n"]) for row in rows)
    impacts = tuple(
        ServiceImpact(
            service=str(row["service"]),
            count=int(row["n"]),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            share=(int(row["n"]) / total) if total else 0.0,
        )
        for row in rows
    )

    is_cascade = False
    note = ""
    if len(impacts) >= 2:
        intervals: list[tuple[datetime, datetime]] = []
        for impact in impacts:
            lo = _parse(impact.first_seen or "")
            hi = _parse(impact.last_seen or "")
            if lo is None or hi is None:
                continue
            intervals.append((lo, hi))

        closest: tuple[float, str, str] | None = None
        for i in range(len(intervals)):
            for j in range(i + 1, len(intervals)):
                gap = _gap_minutes(intervals[i], intervals[j])
                if gap is None:
                    continue
                if closest is None or gap < closest[0]:
                    closest = (gap, impacts[i].service, impacts[j].service)

        if closest is not None and closest[0] <= correlation_window_minutes:
            is_cascade = True
            gap, first, second = closest
            note = (
                f"La misma huella aparece en {len(impacts)} servicios y sus ventanas "
                f"de fallo se solapan o están a {gap:.0f} min "
                f"({first} / {second}), dentro de la ventana de correlación de "
                f"{correlation_window_minutes} min: el fallo probablemente se propaga, "
                f"y la causa raíz está en el servicio que falla primero."
            )
        else:
            note = (
                f"La misma huella aparece en {len(impacts)} servicios, pero sus ventanas "
                f"de fallo no se solapan dentro de los {correlation_window_minutes} min: "
                f"no hay evidencia de cascada."
            )
    else:
        note = (
            "La huella sólo aparece en un servicio: no hay Cascada posible con los datos actuales."
        )

    timeline: tuple[TimeBucket, ...] = ()
    if since is not None and until is not None:
        timeline = _timeline(conn, fingerprint, since, until)

    return BlastRadius(
        fingerprint=fingerprint,
        total_events=total,
        distinct_services=len(impacts),
        affected_services=impacts,
        timeline=timeline,
        is_cascade=is_cascade,
        cascade_note=note,
        correlation_window_minutes=correlation_window_minutes,
    )


__all__ = [
    "DEFAULT_CORRELATION_MINUTES",
    "BlastRadius",
    "ServiceImpact",
    "TimeBucket",
    "blast_radius",
]
