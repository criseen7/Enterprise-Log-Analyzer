"""Comparación de una ventana contra su baseline.

El problema que resuelve: un agente que ve "412 errores ahora y 380 antes" tiene
que decidir si eso es una anomalía. Esa es aritmética, y los modelos de lenguaje
la hacen mal: un error de un 10 % en el ratio se convierte en "se cuadruplicó" y el
diagnóstico entero se va con él.

Aquí el cálculo se hace en el motor, con los datos de la base, y el agente recibe
un hecho: el ratio, el z-score y el veredicto ya resueltos.

El baseline por defecto es la ventana inmediatamente anterior del mismo largo.
Eso hace que la operación sea útil con una sola llamada, que es como la va a
usar el agente.

El z-score se calcula sobre **conteos por hora**, no sobre el total agregado: un
total único no tiene distribución, y comparar "un número" contra "una distribución"
no es un z-score. La serie horaria del baseline da la distribución y la serie
horaria de la ventana da el valor observado.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from app.analysis.anomalies import _enumerate_hours, _floor_hour
from app.parser.severity import ERROR
from app.timeutil import canonical as _canonical
from app.timeutil import parse_utc as _parse
from app.timeutil import to_canonical as _iso

# Ratio mínimo para considerar una ventana "elevada" sobre su baseline.
ELEVATED_RATIO = 3.0
# Horas mínimas de baseline para que el z-score signifique algo.
MIN_BASELINE_HOURS = 6
# Eventos mínimos en el baseline para no concluir sobre una base de tres eventos.
MIN_BASELINE_SAMPLES = 10

# El formato canónico vive en `app.timeutil`: lo comparten el motor, la capa
# MCP y cualquier tool que acepte un timestamp externo.


@dataclass(frozen=True)
class WindowStats:
    """Resumen de una ventana `[since, until)` para una huella concreta."""

    label: str
    since: str | None
    until: str | None
    count: int
    rate_per_hour: float
    share_of_errors: float
    distinct_services: int
    hours: int
    hourly: tuple[int, ...]
    stddev: float = 0.0


@dataclass(frozen=True)
class Comparison:
    """Resultado de comparar una ventana con su baseline."""

    fingerprint: str
    category: str | None
    sample_message: str | None
    severity: int
    window: WindowStats
    baseline: WindowStats
    ratio: float | None
    z_score: float | None
    verdict: str
    confidence: str
    affected_services: tuple[str, ...]


def _data_bounds(conn: sqlite3.Connection, fingerprint: str) -> tuple[str, str] | None:
    row = conn.execute(
        """
        SELECT MIN(timestamp_utc) AS lo, MAX(timestamp_utc) AS hi
        FROM events
        WHERE fingerprint = ? AND timestamp_utc IS NOT NULL
        """,
        (fingerprint,),
    ).fetchone()
    if not row or not row["lo"]:
        return None
    return str(row["lo"]), str(row["hi"])


def _resolve_window(
    conn: sqlite3.Connection, fingerprint: str, since: str | None, until: str | None
) -> tuple[str, str] | None:
    """Extremos efectiva de la ventana, o `None` si no hay nada que comparar.

    Si sólo viene uno de los dos, la ventana no tiene principio ni fin y no es
    medible: se devuelve `None` en vez de inventar el extremo que falta.
    """
    if (since is None) != (until is None):
        return None
    if since is not None and until is not None:
        lo, hi = _parse(since), _parse(until)
        if lo is None or hi is None or lo >= hi:
            return None
        return _iso(lo), _iso(hi)

    bounds = _data_bounds(conn, fingerprint)
    if bounds is None:
        return None
    lo_raw, hi_raw = bounds
    lo_dt, hi_dt = _parse(lo_raw), _parse(hi_raw)
    if lo_dt is None or hi_dt is None:
        return None
    return _iso(lo_dt), _iso(hi_dt + timedelta(seconds=1))


def _baseline_bounds(
    win_since: str,
    win_until: str,
    baseline_since: str | None,
    baseline_until: str | None,
) -> tuple[str, str] | None:
    """Extremos del baseline: los dados, o la ventana anterior de igual duración.

    Si sólo viene uno de los dos extremos del baseline, el rango no es medible
    y se devuelve `None`: es mejor declarar que no hay comparación que comparar
    contra una ventana inventada.
    """
    if (baseline_since is None) != (baseline_until is None):
        return None
    if baseline_since is None or baseline_until is None:
        lo, hi = _parse(win_since), _parse(win_until)
        if lo is None or hi is None:
            return None
        return _iso(lo - (hi - lo)), win_since
    lo_c = _canonical(baseline_since)
    hi_c = _canonical(baseline_until)
    if lo_c is None or hi_c is None:
        return None
    return lo_c, hi_c


def _hourly_series(
    conn: sqlite3.Connection, fingerprint: str, since: str, until: str
) -> tuple[int, ...]:
    """Conteos por hora de una huella, con los huecos rellenados con cero.

    Rellenar importa: sin los ceros, una hora sin eventos no aparece en la serie
    y la media del baseline queda inflada por el sesgo de sólo contar las horas
    con actividad.
    """
    lo_dt, hi_dt = _parse(since), _parse(until)
    if lo_dt is None or hi_dt is None:
        return ()
    start = _floor_hour(lo_dt)
    end = _floor_hour(hi_dt - timedelta(microseconds=1))
    if start > end:
        return ()
    hours = _enumerate_hours(start, end)

    rows = conn.execute(
        """
        SELECT substr(timestamp_utc, 1, 13) || ':00' AS bucket, COUNT(*) AS n
        FROM events
        WHERE fingerprint = ? AND timestamp_utc >= ? AND timestamp_utc < ?
        GROUP BY bucket
        """,
        (fingerprint, since, until),
    ).fetchall()
    counts = {str(row["bucket"]): int(row["n"]) for row in rows}
    return tuple(counts.get(hour, 0) for hour in hours)


def _window_stats(
    conn: sqlite3.Connection,
    fingerprint: str,
    label: str,
    since: str,
    until: str,
) -> tuple[WindowStats, sqlite3.Row]:
    """Agregados de una huella en una ventana, más la fila cruda del summary.

    `share_of_errors` usa el total de errores de esa misma ventana como
    denominador: un fingerprint con 40 de 4.000 errores y otro con 40 de 40
    pesan igual en el conteo, pero no en el mismo tipo de ventana.
    """
    row = conn.execute(
        """
        SELECT COUNT(*)                                        AS n,
               COUNT(DISTINCT service)                         AS services,
               MAX(category)                                   AS category,
               MAX(severity)                                   AS severity,
               MAX(message)                                    AS sample_message,
               MIN(timestamp_utc)                              AS first_seen,
               MAX(timestamp_utc)                              AS last_seen
        FROM events
        WHERE fingerprint = ? AND timestamp_utc >= ? AND timestamp_utc < ?
        """,
        (fingerprint, since, until),
    ).fetchone()

    total_errors = conn.execute(
        """
        SELECT COUNT(*) FROM events
        WHERE severity >= ? AND timestamp_utc >= ? AND timestamp_utc < ?
        """,
        (ERROR, since, until),
    ).fetchone()
    errors_in_window = int(total_errors[0]) if total_errors else 0

    hourly = _hourly_series(conn, fingerprint, since, until)
    hours = len(hourly) or 1
    count = int(row["n"] or 0)
    mean = (sum(hourly) / hours) if hourly else 0.0
    variance = (sum((x - mean) ** 2 for x in hourly) / hours) if hourly else 0.0

    stats = WindowStats(
        label=label,
        since=since,
        until=until,
        count=count,
        rate_per_hour=count / hours,
        share_of_errors=(count / errors_in_window) if errors_in_window else 0.0,
        distinct_services=int(row["services"] or 0),
        hours=hours,
        hourly=hourly,
        stddev=math.sqrt(variance),
    )
    return stats, row


def _z_score(window: WindowStats, baseline: WindowStats) -> float | None:
    """z-score de la ventana contra la distribución horaria del baseline.

    Se usa la media horaria de cada ventana y la desviación **poblacional** del
    baseline: el baseline es un periodo distinto, no incluye el valor observado,
    así que no aplica el leave-one-out que usa `analysis.anomalies` (allí el
    bucket se compara contra el resto de su propia serie).

    Sin desviación no hay z-score: se devuelve `None` en vez de infinito, porque
    `inf` no es JSON válido y el valor tiene que viajar por el protocolo MCP.
    El veredicto no depende sólo del z-score: cuando el baseline es plano, el
    ratio decide (ver `compare_windows`).
    """
    if not baseline.hourly or not window.hourly:
        return None
    mean_b = sum(baseline.hourly) / len(baseline.hourly)
    stddev = baseline.stddev
    if stddev == 0:
        return None
    mean_w = sum(window.hourly) / len(window.hourly)
    return (mean_w - mean_b) / stddev


def compare_windows(
    conn: sqlite3.Connection,
    fingerprint: str,
    *,
    since: str | None = None,
    until: str | None = None,
    baseline_since: str | None = None,
    baseline_until: str | None = None,
    elevated_ratio: float = ELEVATED_RATIO,
    z_threshold: float = 3.0,
) -> Comparison | None:
    """Compara una ventana de una huella contra su baseline.

    Devuelve `None` si la huella no existe o si la ventana no es medible (falta
    uno de los extremos, o el rango está vacío). El llamante decide cómo
    reportarlo: para una tool MCP eso es un `ToolError`, no un `None` silencioso.
    """
    resolved = _resolve_window(conn, fingerprint, since, until)
    if resolved is None:
        return None
    win_since, win_until = resolved

    bounds = _baseline_bounds(win_since, win_until, baseline_since, baseline_until)
    if bounds is None:
        return None
    base_since, base_until = bounds

    window, row = _window_stats(conn, fingerprint, "window", win_since, win_until)
    baseline, _ = _window_stats(conn, fingerprint, "baseline", base_since, base_until)

    ratio: float | None = None
    if baseline.rate_per_hour > 0:
        ratio = window.rate_per_hour / baseline.rate_per_hour

    z_score = _z_score(window, baseline)

    if baseline.count == 0 and window.count > 0:
        verdict = "new"
    elif ratio is not None and ratio >= elevated_ratio:
        # El z-score exige que el baseline tenga dispersión. Un baseline plano
        # —el mismo número de errores cada hora— no la tiene, y el z-score no
        # está definido. Pero un ratio de 5 contra un baseline perfectly flat
        # no es una afirmación dudosa: es el caso más claro que hay. Cuando el
        # z-score sí existe, se exige que pase el umbral: un baseline con
        # mucha varianza puede producir ratios altos sin que el pico sea real.
        if z_score is None:
            verdict = "elevated"
        elif z_score >= z_threshold:
            verdict = "elevated"
        else:
            verdict = "stable"
    elif ratio is not None and ratio < 0.5:
        verdict = "decreased"
    else:
        verdict = "stable"

    if baseline.hours >= 24 and baseline.count >= MIN_BASELINE_SAMPLES:
        confidence = "high"
    elif baseline.hours >= MIN_BASELINE_HOURS and baseline.count >= MIN_BASELINE_SAMPLES:
        confidence = "medium"
    else:
        confidence = "low"

    services = tuple(
        str(item["service"])
        for item in conn.execute(
            """
            SELECT service, COUNT(*) AS n
            FROM events
            WHERE fingerprint = ? AND timestamp_utc >= ? AND timestamp_utc < ?
              AND service IS NOT NULL
            GROUP BY service
            ORDER BY n DESC
            """,
            (fingerprint, win_since, win_until),
        )
    )

    return Comparison(
        fingerprint=fingerprint,
        category=row["category"],
        sample_message=row["sample_message"],
        severity=int(row["severity"] or 0),
        window=window,
        baseline=baseline,
        ratio=ratio,
        z_score=z_score,
        verdict=verdict,
        confidence=confidence,
        affected_services=services,
    )


__all__ = [
    "ELEVATED_RATIO",
    "MIN_BASELINE_HOURS",
    "MIN_BASELINE_SAMPLES",
    "Comparison",
    "WindowStats",
    "compare_windows",
]
