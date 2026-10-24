"""`compare_windows` y `blast_radius`: las dos tools que hacen la aritmética.

Son el diferenciador del proyecto y la razón por la que el agente no tiene que
hacer los cálculos. Un modelo de lenguaje que ve "412 errores ahora y 380
antes" puede decidir que el ratio es 1.08, 1.8 o 4.12 según el día; un motor
que compara dos series horarias no puede equivocarse.

* `compare_windows` responde "¿esto está elevado sobre su baseline?" con un
  ratio, un z-score y un veredicto ya resueltos.
* `blast_radius` responde "¿esto es un problema aislado o una cascada?" con los
  servicios afectados y la distancia entre sus ventanas de fallo.

Ambas son deterministas de principio a fin: la misma base y la misma ventana
dan siempre el mismo resultado, que es lo que hace citable la respuesta.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from app.analysis.blast import DEFAULT_CORRELATION_MINUTES
from app.analysis.blast import blast_radius as engine_blast_radius
from app.analysis.windows import ELEVATED_RATIO
from app.analysis.windows import WindowStats as EngineWindowStats
from app.analysis.windows import compare_windows as engine_compare_windows
from app.mcp._context import read_connection, resolve_bounds
from app.mcp.schemas import (
    BlastRadiusResult,
    CompareWindowsResult,
    ServiceImpact,
    TimeBucket,
    WindowStats,
)
from app.mcp.tools._base import guard
from app.timeutil import parse_utc, to_canonical


def _stats(raw: EngineWindowStats) -> WindowStats:
    """Convierte el `WindowStats` del motor al modelo de salida."""
    return WindowStats(
        label=raw.label,
        since=raw.since,
        until=raw.until,
        count=raw.count,
        rate_per_hour=round(raw.rate_per_hour, 4),
        share_of_errors=round(raw.share_of_errors, 4),
        distinct_services=raw.distinct_services,
        hours=raw.hours,
        stddev=round(raw.stddev, 4),
    )


def _compare(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    since: str | None,
    until: str | None,
    baseline_since: str | None,
    baseline_until: str | None,
    elevated_ratio: float,
    z_threshold: float,
) -> CompareWindowsResult:
    """La comparación, calculada por el motor.

    La validación va dentro de la función guardada (ver `app/mcp/tools/_base.py`).
    """
    if elevated_ratio <= 0:
        raise ValueError("elevated_ratio debe ser > 0")
    if z_threshold <= 0:
        raise ValueError("z_threshold debe ser > 0")
    lo, hi = resolve_bounds(since, until)
    base_lo, base_hi = resolve_bounds(baseline_since, baseline_until)

    comparison = engine_compare_windows(
        conn,
        fingerprint,
        since=lo,
        until=hi,
        baseline_since=base_lo,
        baseline_until=base_hi,
        elevated_ratio=elevated_ratio,
        z_threshold=z_threshold,
    )
    if comparison is None:
        raise ValueError(
            f"no hay datos comparables para la huella {fingerprint!r}. "
            "La ventana debe tener ambos extremos (since y until) o ninguno, "
            "y la huella debe existir en la base."
        )

    return CompareWindowsResult(
        fingerprint=comparison.fingerprint,
        category=comparison.category,
        sample_message=comparison.sample_message,
        severity=comparison.severity,
        window=_stats(comparison.window),
        baseline=_stats(comparison.baseline),
        ratio=round(comparison.ratio, 4) if comparison.ratio is not None else None,
        z_score=round(comparison.z_score, 4) if comparison.z_score is not None else None,
        verdict=comparison.verdict,  # type: ignore[arg-type]
        confidence=comparison.confidence,  # type: ignore[arg-type]
        affected_services=list(comparison.affected_services),
    )


def compare_windows(
    fingerprint: str,
    since: str | None = None,
    until: str | None = None,
    baseline_since: str | None = None,
    baseline_until: str | None = None,
    elevated_ratio: float = ELEVATED_RATIO,
    z_threshold: float = 3.0,
) -> CompareWindowsResult:
    """Compara una ventana de una huella contra su baseline.

    Sin `baseline_since`/`baseline_until`, el baseline es **la ventana
    inmediatamente anterior del mismo largo**, que es lo que hace que la tool
    sirva con una sola llamada.

    `verdict` es uno de `new` (no había errores antes), `elevated`, `stable` o
    `decreased`. `confidence` sube con la cantidad de datos del baseline: un
    ratio calculado sobre seis eventos no sostiene la misma afirmación que uno
    calculado sobre seis mil, y el motor lo dice en vez de dejar que el agente
    lo infiera.

    El z-score usa la serie **horaria** de cada ventana, con los huecos
    rellenados con ceros. Comparar el total agregado contra el total agregado
    no es un z-score: un número no tiene distribución.
    """
    with read_connection() as conn:
        return guard(
            _compare,
            conn,
            fingerprint=fingerprint,
            since=since,
            until=until,
            baseline_since=baseline_since,
            baseline_until=baseline_until,
            elevated_ratio=elevated_ratio,
            z_threshold=z_threshold,
        )


def _bounds_for(conn: sqlite3.Connection, fingerprint: str) -> tuple[str | None, str | None]:
    """Rango temporal de una huella, para cuando el llamante no da ninguno.

    `until` es **exclusivo** en todo el motor, así que el último evento
    caería fuera de su propia ventana. Se le suma un milisegundo al máximo
    para incluirlo: el rango derivado tiene que cubrir los datos, no dejarlos
    en el borde.
    """
    row = conn.execute(
        """
        SELECT MIN(timestamp_utc) AS lo, MAX(timestamp_utc) AS hi
          FROM events
         WHERE fingerprint = ? AND timestamp_utc IS NOT NULL
        """,
        (fingerprint,),
    ).fetchone()
    if row is None or not row["lo"]:
        return None, None
    high = parse_utc(str(row["hi"]))
    if high is None:
        return str(row["lo"]), str(row["hi"])
    return str(row["lo"]), to_canonical(high + timedelta(milliseconds=1))


def _blast(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    since: str | None,
    until: str | None,
    correlation_window_minutes: int,
) -> BlastRadiusResult:
    """El radio de impacto, calculado por el motor.

    La validación va dentro de la función guardada (ver `app/mcp/tools/_base.py`).
    """
    if correlation_window_minutes <= 0:
        raise ValueError("correlation_window_minutes debe ser > 0")
    lo, hi = resolve_bounds(since, until)
    if lo is None or hi is None:
        # Sin ventana explícita, el radio de impacto se calcula sobre todo el
        # histórico de la huella: acotarlo al rango del dataset es lo único que
        # se puede hacer sin inventar un timeframe.
        data_lo, data_hi = _bounds_for(conn, fingerprint)
        if data_lo is None:
            raise ValueError(f"la huella {fingerprint!r} no tiene ningún evento en la base")
        lo, hi = data_lo, data_hi

    radius = engine_blast_radius(
        conn,
        fingerprint,
        since=lo,
        until=hi,
        correlation_window_minutes=correlation_window_minutes,
    )
    if radius is None:
        raise ValueError(f"la huella {fingerprint!r} no tiene eventos en la ventana [{lo}, {hi})")

    return BlastRadiusResult(
        fingerprint=radius.fingerprint,
        total_events=radius.total_events,
        distinct_services=radius.distinct_services,
        affected_services=[
            ServiceImpact(
                service=item.service,
                count=item.count,
                first_seen=item.first_seen,
                last_seen=item.last_seen,
                share=round(item.share, 4),
            )
            for item in radius.affected_services
        ],
        timeline=[
            TimeBucket(
                bucket_start=item.bucket_start,
                count=item.count,
                services=list(item.services),
            )
            for item in radius.timeline
        ],
        is_cascade=radius.is_cascade,
        cascade_note=radius.cascade_note,
        correlation_window_minutes=radius.correlation_window_minutes,
    )


def blast_radius(
    fingerprint: str,
    since: str | None = None,
    until: str | None = None,
    correlation_window_minutes: int = DEFAULT_CORRELATION_MINUTES,
) -> BlastRadiusResult:
    """Qué servicios cubre una huella y si el fallo se propaga.

    `is_cascade` es True cuando dos o más servicios fallan con la misma huella
    y sus ventanas de fallo se solapan o están a menos de
    `correlation_window_minutes` minutos. Eso cambia la acción: un incidente
    aislado se atiende en su servicio, una cascada se atiende en el que falla
    primero.

    `cascade_note` lo escribe el motor, no un modelo: es determinista y por
    tanto citable tal cual.
    """
    with read_connection() as conn:
        return guard(
            _blast,
            conn,
            fingerprint=fingerprint,
            since=since,
            until=until,
            correlation_window_minutes=correlation_window_minutes,
        )


__all__ = ["blast_radius", "compare_windows"]
