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

# Los eventos sin `service` no se descartan: son impacto real, aunque no se sepa
# qué componente lo emitió. Se agrupan bajo una etiqueta explícita.
NO_SERVICE = "(sin servicio)"


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


def _cascade_evidence(
    conn: sqlite3.Connection,
    fingerprint: str,
    *,
    since: str | None,
    until: str | None,
) -> tuple[float, str, str] | None:
    """Par de eventos de servicios distintos más cercano en el tiempo.

    Se recorre la serie ordenada una sola vez. Comparar intervalos `[MIN, MAX]`
    por servicio convertía un evento a las 00:00 y otro a las 23:00 en una
    "ventana de fallo" que solapaba con cualquier cosa del día; dos eventos
    reales y adyacentes no tienen ese problema.
    """
    clauses = ["fingerprint = ?", "timestamp_utc IS NOT NULL"]
    params: list[object] = [fingerprint]
    if since is not None:
        clauses.append("timestamp_utc >= ?")
        params.append(since)
    if until is not None:
        clauses.append("timestamp_utc < ?")
        params.append(until)
    rows = conn.execute(
        f"""
        SELECT timestamp_utc,
               COALESCE(service, ?) AS service
        FROM events
        WHERE {" AND ".join(clauses)}
        ORDER BY timestamp_utc, service
        """,
        [NO_SERVICE, *params],
    ).fetchall()

    previous_moment: datetime | None = None
    previous_service: str | None = None
    closest: tuple[float, str, str] | None = None
    for row in rows:
        moment = _parse(str(row["timestamp_utc"]))
        if moment is None:
            continue
        service = str(row["service"])
        if previous_moment is not None and previous_service != service:
            gap = (moment - previous_moment).total_seconds() / 60.0
            if closest is None or gap < closest[0]:
                closest = (gap, str(previous_service), service)
        previous_moment, previous_service = moment, service
    return closest


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
               COALESCE(service, ?) AS service,
               COUNT(*) AS n
        FROM events
        WHERE fingerprint = ? AND timestamp_utc >= ? AND timestamp_utc < ?
        GROUP BY 1, 2
        """,
        (NO_SERVICE, fingerprint, since, until),
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
        SELECT COALESCE(service, ?) AS service,
               COUNT(*)             AS n,
               MIN(timestamp_utc)   AS first_seen,
               MAX(timestamp_utc)   AS last_seen
        FROM events
        WHERE {where}
        GROUP BY 1
        ORDER BY n DESC, first_seen, service
        """,
        [NO_SERVICE, *params],
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
        closest = _cascade_evidence(conn, fingerprint, since=since, until=until)
        if closest is not None and closest[0] <= correlation_window_minutes:
            is_cascade = True
            gap, first, second = closest
            note = (
                f"La misma huella aparece en {len(impacts)} servicios y hay eventos "
                f"de dos de ellos a {gap:.0f} min ({first} / {second}), dentro de "
                f"la ventana de correlación de {correlation_window_minutes} min: "
                f"el fallo probablemente se propaga, y la causa raíz está en el "
                f"servicio que falla primero."
            )
        else:
            note = (
                f"La misma huella aparece en {len(impacts)} servicios, pero no hay "
                f"eventos de dos servicios dentro de los "
                f"{correlation_window_minutes} min: no hay evidencia de cascada."
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
    "NO_SERVICE",
    "BlastRadius",
    "ServiceImpact",
    "TimeBucket",
    "blast_radius",
]
