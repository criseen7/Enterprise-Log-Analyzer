"""Recurrencia: qué se está repitiendo *ahora*, no qué se repitió siempre.

`metrics.py` responde "cuántos errores hubo en el dataset". Este módulo
responde otra pregunta, la que importa cuando el dashboard está delante de
alguien: "¿qué está fallando en la última ventana?".

La diferencia no es cosmética: un error que ocurrió 5.000 veces el martes y
nada desde entonces es histórico, no un problema abierto. Agrupar por
`fingerprint` dentro de una ventana deslizante separa las dos cosas sin tocar
los datos — todo se resuelve en SQL sobre `idx_events_fp` y `idx_events_sev_ts`.

Sobre qué es "ahora": por defecto, el **último timestamp del dataset** y no el
reloj de pared. Un dataset de demo o de un fin de semana terminado hace días
quedaría con la ventana vacía y parecería que no hay nada, que es un falso
negativo más engañoso que no mostrar nada. Quien quiera el reloj real lo pasa
como `now=`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.parser.severity import UNKNOWN


@dataclass(frozen=True)
class RecurringPattern:
    fingerprint: str
    category: str | None
    sample_message: str | None
    service: str | None
    severity: int | None
    count: int
    share: float
    first_seen: str | None
    last_seen: str | None


def _data_now(conn: sqlite3.Connection, now: str | None) -> str | None:
    if now is not None:
        return now
    row = conn.execute("SELECT MAX(timestamp_utc) FROM events").fetchone()
    if row and row[0]:
        return str(row[0])
    return None


def _shift(iso: str, minutes: int) -> str:
    """Resta `minutes` a un timestamp ISO. Los de la base son UTC, sin DST."""
    text = iso.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        # Un timestamp con formato raro no debería tumbar una consulta del
        # dashboard: se trunca la ventana a lo que haya y se sigue.
        return iso
    return (moment - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def recurring_patterns(
    conn: sqlite3.Connection,
    *,
    window_minutes: int = 15,
    now: str | None = None,
    min_count: int = 2,
    min_severity: int = UNKNOWN,
    limit: int | None = None,
) -> list[RecurringPattern]:
    """Patrones que se repiten dentro de la ventana `(now - window, now]`.

    `share` es el peso del patrón dentro de la ventana: sin él, "5 ocurrencias"
    no dice nada si la ventana tiene 5 eventos o 5.000.

    `limit=None` (por defecto) devuelve **todas** las huellas de la ventana. Un
    tope implícito es un recorte silencioso: quien abre incidentes o cuenta
    fingerprints necesita la lista completa. Recortar para presentar es
    responsabilidad de la UI/CLI, y el orden `n DESC, last_seen DESC` lo permite.
    """
    if window_minutes <= 0:
        raise ValueError("window_minutes debe ser > 0")

    upper = _data_now(conn, now)
    if upper is None:
        return []
    lower = _shift(upper, window_minutes)
    total_row = conn.execute(
        """
        SELECT COUNT(*) FROM events
        WHERE timestamp_utc > ? AND timestamp_utc <= ?
        """,
        (lower, upper),
    ).fetchone()
    total = int(total_row[0]) if total_row else 0

    sql = """
        SELECT fingerprint,
               MAX(category)     AS category,
               MAX(message)      AS sample_message,
               MAX(service)      AS service,
               MAX(severity)     AS severity,
               COUNT(*)          AS n,
               MIN(timestamp_utc) AS first_seen,
               MAX(timestamp_utc) AS last_seen
        FROM events
        WHERE timestamp_utc > ?
          AND timestamp_utc <= ?
          AND severity >= ?
          AND fingerprint IS NOT NULL
        GROUP BY fingerprint
        HAVING COUNT(*) >= ?
        ORDER BY n DESC, last_seen DESC
    """
    params: list[object] = [lower, upper, min_severity, min_count]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    return [
        RecurringPattern(
            fingerprint=row["fingerprint"],
            category=row["category"],
            sample_message=row["sample_message"],
            service=row["service"],
            severity=row["severity"],
            count=int(row["n"]),
            share=(int(row["n"]) / total) if total else 0.0,
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )
        for row in rows
    ]


__all__ = ["RecurringPattern", "recurring_patterns"]
