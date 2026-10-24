"""`system_health`: el estado del dataset antes dediagnosticar nada.

Un agente que empieza a buscar la causa de un incidente sin mirar primero cuánto
de los logs se pudo parsear está diagnosticking sobre arena. Si el 40 % de las
líneas no se entendió, el 60 % que sí se entendió no describe la mitad del
problema, describe una parte arbitraria.

Por eso esta tool devuelve `trust_level` como un campo de primer orden: no es
una nota al pie, es parte del veredicto sobre lo que el resto de las tools van
a poder afirmar.
"""

from __future__ import annotations

import sqlite3

from app.analysis.metrics import overview
from app.db.repository import parse_coverage, recent_runs
from app.mcp._context import read_connection
from app.mcp.schemas import IngestRunHealth, ParseErrorCount, SystemHealthResult
from app.mcp.tools._base import guard

# Cobertura de parseo por debajo de la cual el dataset no sirve para
# diagnosticar. Es un umbral de confianza, no de calidad estética.
MIN_COVERAGE = 0.80
# Eventos a partir de los cuales una cobertura alta significa algo.
STRONG_SAMPLE = 1000


def _trust_level(total_events: int, coverage: float) -> str:
    """Nivel de confianza en lo que el dataset puede sostener.

    Tres estados y no más: `high` (se puede diagnosticar), `medium` (se puede
    diagnosticar con reservas) y `low` (no se puede concluir sin decirlo).
    """
    if total_events == 0:
        return "low"
    if coverage < MIN_COVERAGE:
        return "low"
    if coverage >= 0.95 and total_events >= STRONG_SAMPLE:
        return "high"
    return "medium"


def _collect(conn: sqlite3.Connection) -> SystemHealthResult:
    stats = overview(conn)
    total, parsed = parse_coverage(conn)
    coverage = (parsed / total) if total else 0.0

    top_parse_errors = [
        ParseErrorCount(
            parse_error=str(row["parse_error"]),
            count=int(row["n"]),
        )
        for row in conn.execute(
            """
            SELECT parse_error, COUNT(*) AS n
              FROM events
             WHERE parsed_ok = 0 AND parse_error IS NOT NULL
             GROUP BY parse_error
             ORDER BY n DESC
             LIMIT 5
            """
        )
    ]

    active_files = int(
        conn.execute("SELECT COUNT(*) FROM log_files WHERE is_active = 1").fetchone()[0]
    )

    last_run: IngestRunHealth | None = None
    runs = recent_runs(conn, limit=1)
    if runs:
        row = runs[0]
        last_run = IngestRunHealth(
            id=int(row["id"]),
            started_at=str(row["started_at"]),
            finished_at=row["finished_at"],
            status=str(row["status"]),
            events_inserted=int(row["events_inserted"] or 0),
            events_skipped=int(row["events_skipped"] or 0),
            events_unparsed=int(row["events_unparsed"] or 0),
            duration_ms=row["duration_ms"],
        )

    return SystemHealthResult(
        total_events=total,
        parsed_events=parsed,
        unparsed_events=total - parsed,
        parse_coverage=round(coverage, 4),
        top_parse_errors=top_parse_errors,
        first_event_ts=stats.first_ts,
        last_event_ts=stats.last_ts,
        active_log_files=active_files,
        last_ingest_run=last_run,
        trust_level=_trust_level(total, coverage),  # type: ignore[arg-type]
        errors=stats.errors,
        fingerprints=stats.fingerprints,
    )


def system_health() -> SystemHealthResult:
    """Estado del dataset: cobertura de parseo, volumen, rango temporal y salud
    de la última ingesta.

    **Llamarla primero.** Devuelve `trust_level`, que acota lo que el resto de
    las tools pueden afirmar: por debajo de 0.80 de cobertura, un diagnóstico
    sobre este dataset es una conjetura sobre la parte del log que sí se pudo
    leer.
    """
    with read_connection() as conn:
        return guard(_collect, conn)


__all__ = ["MIN_COVERAGE", "system_health"]
