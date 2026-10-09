"""Anomalías sobre el conteo horario — z-score, versión MVP.

La pregunta: ¿hubo alguna hora que se saliera de lo normal?

La respuesta aquí es deliberadamente simple y no oculta sus límites:

* **Z-score por hora, con media y desviación leave-one-out.** Para juzgar una
  hora se calculan la media y la desviación del resto, sin ella. Con la serie
  completa incluida, un pico grande sube la desviación de todo y termina
  disimulándose a sí mismo: el outlier se auto-suprime. Con pocas horas eso es
  la diferencia entre detectar el pico y no detectar nada.
* **Serie rellenada.** Sin las horas vacías no hay distribución: seis horas con
  errores y cincuenta sin aparecer darían una media inflada y una desviación
  casi nula, y cualquier cosa parecería anómala.
* **Sólo picos.** Un desplome de tráfico no es una incidencia, así que las
  negativas se ignoran a propósito.
* **Se descarta lo demasiado pequeño.** Un pico de 3 eventos no es un pico, es
  ruido de fondo; `anomaly_min_samples` existe para eso. Y con menos de
  `_MIN_BUCKETS` horas la media no significa nada.

Lo que este módulo NO hace: detectar tendencias, estacionalidad ni cambios de
nivel (CUSUM, IQR, modelos por día de la semana). Eso es post-MVP; aquí se
documenta la limitación en vez de fingir que un z-score sobre una serie con
patrón diario es una detección robusta.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.parser.severity import ERROR

# Mínimo de buckets para que media y desviación signifiquen algo. Con dos
# puntos la leave-one-out siempre mira a un solo valor: desviación 0 por
# construcción y z infinito para cualquiera de los dos.
_MIN_BUCKETS = 3


@dataclass(frozen=True)
class HourlyCount:
    bucket: str
    count: int


@dataclass(frozen=True)
class Anomaly:
    bucket: str
    count: int
    mean: float
    stdev: float
    zscore: float

    @property
    def is_infinite(self) -> bool:
        return math.isinf(self.zscore)


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _floor_hour(moment: datetime) -> str:
    return moment.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def _hour_range(
    conn: sqlite3.Connection, since: str | None, until: str | None
) -> tuple[str, str] | None:
    """Primer y último bucket cubierto, o `None` si no hay nada que medir.

    `until` es **exclusivo**: si llega `2026-10-05T12:00`, la última hora
    cubierta es la 11. Sin esa corrección la serie terminaría en una hora 12
    vacía por construcción, y esa hora cero entraría en la media como si
    hubiera sido medida.

    Si no se dan ambos extremos, el hueco lo fija el propio dataset (de la
    primera a la última marca): un rango a medias no tiene principio.
    """
    if (since is None) != (until is None):
        return None

    if since is not None and until is not None:
        lo, hi = _parse(since), _parse(until)
        if lo is None or hi is None:
            return None
        start = _floor_hour(lo)
        end = _floor_hour(hi - timedelta(microseconds=1))
    else:
        row = conn.execute(
            """
            SELECT MIN(timestamp_utc) AS lo, MAX(timestamp_utc) AS hi
            FROM events
            WHERE timestamp_utc IS NOT NULL
            """
        ).fetchone()
        if not row or not row["lo"]:
            return None
        lo, hi = _parse(row["lo"]), _parse(row["hi"])
        if lo is None or hi is None:
            return None
        start, end = _floor_hour(lo), _floor_hour(hi)

    return (start, end) if start <= end else None


def _enumerate_hours(start: str, end: str) -> list[str]:
    first = datetime.strptime(start, "%Y-%m-%dT%H:%M")
    last = datetime.strptime(end, "%Y-%m-%dT%H:%M")
    if last < first:
        return []
    hours: list[str] = []
    moment = first
    while moment <= last:
        hours.append(moment.strftime("%Y-%m-%dT%H:%M"))
        moment += timedelta(hours=1)
    return hours


def hourly_counts(
    conn: sqlite3.Connection,
    *,
    min_severity: int = ERROR,
    since: str | None = None,
    until: str | None = None,
) -> list[HourlyCount]:
    """Serie horaria **rellenada**: una fila por hora, incluidas las vacías."""
    span = _hour_range(conn, since, until)
    if span is None:
        return []
    lo, hi = span
    hours = _enumerate_hours(lo, hi)
    if not hours:
        return []

    where = "timestamp_utc IS NOT NULL AND severity >= ?"
    params: list[object] = [min_severity]
    if since is not None:
        where += " AND timestamp_utc >= ?"
        params.append(since)
    if until is not None:
        where += " AND timestamp_utc < ?"
        params.append(until)

    rows = conn.execute(
        f"""
        SELECT substr(timestamp_utc, 1, 13) || ':00' AS bucket, COUNT(*) AS n
        FROM events
        WHERE {where}
        GROUP BY bucket
        """,
        params,
    ).fetchall()

    counts = {row["bucket"]: int(row["n"]) for row in rows}
    return [HourlyCount(bucket=hour, count=counts.get(hour, 0)) for hour in hours]


def _leave_one_out_z(values: list[int], index: int) -> tuple[float, float, float]:
    """`(z, media, desviación)` del bucket `index` contra el resto."""
    candidate = values[index]
    others = values[:index] + values[index + 1 :]
    mean = sum(others) / len(others)
    variance = sum((x - mean) ** 2 for x in others) / len(others)
    stdev = math.sqrt(variance)
    if stdev == 0:
        # Todo lo demás idéntico: o no se desvía en absoluto, o el candidato
        # es el único distinto. `inf` es la respuesta honesta.
        z = 0.0 if candidate == mean else math.inf if candidate > mean else -math.inf
        return z, mean, stdev
    return (candidate - mean) / stdev, mean, stdev


def anomalies(
    conn: sqlite3.Connection,
    *,
    z_threshold: float = 3.0,
    min_samples: int = 5,
    min_severity: int = ERROR,
    since: str | None = None,
    until: str | None = None,
) -> list[Anomaly]:
    """Horas cuyo conteo se sale `z_threshold` desviaciones por encima del resto.

    El orden de los descartes importa y no es arbitrario:

    1. Serie demasiado corta -> sin anomalías (no hay distribución).
    2. Bucket con menos de `min_samples` eventos -> descartado aunque su z sea
       gigante: 3 eventos frente a 0 son ruido, no un pico.
    3. Resto -> z leave-one-out y comparación con el umbral.
    """
    if z_threshold <= 0:
        raise ValueError("z_threshold debe ser > 0")

    serie = hourly_counts(conn, min_severity=min_severity, since=since, until=until)
    if len(serie) < _MIN_BUCKETS:
        return []

    values = [point.count for point in serie]
    result: list[Anomaly] = []
    for index, point in enumerate(serie):
        if point.count < min_samples:
            continue
        z, mean, stdev = _leave_one_out_z(values, index)
        if z >= z_threshold:
            result.append(
                Anomaly(
                    bucket=point.bucket,
                    count=point.count,
                    mean=mean,
                    stdev=stdev,
                    zscore=z,
                )
            )
    result.sort(key=lambda a: a.bucket)
    return result


__all__ = ["Anomaly", "HourlyCount", "anomalies", "hourly_counts"]
