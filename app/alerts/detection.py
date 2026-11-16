"""Detección de incidentes basada en recurrencia por ventana.

Toma los patrones recurrentes (mismo fingerprint en ventana) y crea/actualiza
incidentes con upsert por fingerprint. El cooldown usa `notified_at` (reloj de
pared) y la ocurrencia real usa `notified_through` (último `last_seen`
notificado): sin las dos cosas, una ventana estática reabre y renotifica
incidentes sin ningún evento nuevo.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.analysis.patterns import RecurringPattern, recurring_patterns
from app.config import AnalysisConfig, get_settings
from app.db.connection import transaction
from app.parser.normalization import to_utc_iso


@dataclass(frozen=True)
class IncidentCandidate:
    fingerprint: str
    category: str
    severity: int
    sample_message: str
    first_seen: str
    last_seen: str
    event_count: int


def _parse_iso(ts: str) -> datetime:
    # `timestamp_utc` viene como YYYY-MM-DDTHH:MM:SS[.mmm]Z. `fromisoformat`
    # entiende las dos formas; el fallback cubre cadenas sin sufijo de zona.
    moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _format_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return to_utc_iso(dt)


def _candidates_from_patterns(
    patterns: list[RecurringPattern], cfg: AnalysisConfig
) -> list[IncidentCandidate]:
    candidates: list[IncidentCandidate] = []
    for p in patterns:
        if p.count < cfg.incident_threshold:
            continue
        if p.severity < cfg.min_incident_severity:
            continue
        candidates.append(
            IncidentCandidate(
                fingerprint=p.fingerprint,
                category=p.category,
                severity=p.severity,
                sample_message=p.sample_message,
                first_seen=p.first_seen,
                last_seen=p.last_seen,
                event_count=p.count,
            )
        )
    return candidates


def _should_notify(now: datetime, notified_at: str | None, cooldown_minutes: int) -> bool:
    if cooldown_minutes <= 0:
        return True
    if notified_at is None:
        return True
    n = _parse_iso(notified_at)
    if (now - n) >= timedelta(minutes=cooldown_minutes):
        return True
    return False


def _has_new_occurrence(last_seen: str, notified_through: str | None) -> bool:
    """True sólo si hay un evento posterior al último que ya se notificó.

    `notified_through` es un timestamp de datos, no el reloj: compararlo con el
    `last_seen` de la ventana actual distingue un rebrote real de la misma
    ventana estática que sigue conteniendo los eventos viejos.
    """
    if notified_through is None:
        return True
    return _parse_iso(last_seen) > _parse_iso(notified_through)


def _latest(a: str | None, b: str) -> str:
    """El mayor de dos timestamps ISO, sin retroceder la marca de agua."""
    if a is None:
        return b
    return b if _parse_iso(b) > _parse_iso(a) else a


def _upsert_incident(
    conn: sqlite3.Connection, c: IncidentCandidate, now: datetime, cooldown_minutes: int
) -> dict:
    """Crea o actualiza un incidente por fingerprint.

    Reapertura automática: un incidente `RESOLVED` sólo vuelve a `OPEN` si hay
    un evento posterior a `notified_through`, y en ese caso se notifica en esta
    misma pasada. La misma ventana estática no es un rebrote: sus eventos ya se
    notificaron y no justifican otra alerta. `event_count` refleja el conteo de
    la ventana, no un máximo acumulado.
    """
    notify_on_insert = _should_notify(now, None, cooldown_minutes)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO incidents (
            fingerprint, category, severity, sample_message,
            first_seen, last_seen, event_count, status,
            notified_at, notified_through
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
        """,
        (
            c.fingerprint,
            c.category,
            c.severity,
            c.sample_message,
            c.first_seen,
            c.last_seen,
            c.event_count,
            _format_iso(now) if notify_on_insert else None,
            c.last_seen if notify_on_insert else None,
        ),
    )
    if cursor.rowcount == 1:
        return {
            "id": cursor.lastrowid,
            "fingerprint": c.fingerprint,
            "status": "OPEN",
            "notified": notify_on_insert,
        }

    row = conn.execute(
        """
        SELECT id, status, notified_at, notified_through, event_count, first_seen, last_seen
        FROM incidents
        WHERE fingerprint = ?
        """,
        (c.fingerprint,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"incidente no insertado ni encontrado: {c.fingerprint!r}")

    inc_id, status_cur, notified_at_cur = row["id"], row["status"], row["notified_at"]
    notified_through_cur = row["notified_through"]
    has_new = _has_new_occurrence(c.last_seen, notified_through_cur)
    new_status = status_cur
    new_notified_at = notified_at_cur
    new_notified_through = notified_through_cur
    notify = False

    if status_cur == "RESOLVED":
        # Un rebrote real reabre y notifica en esta misma pasada. Si no hay
        # evento nuevo, el incidente sigue resuelto: la ventana sólo repite lo
        # que ya se notificó antes de cerrarlo.
        if has_new:
            new_status = "OPEN"
            notify = True
            new_notified_at = _format_iso(now)
            new_notified_through = _latest(notified_through_cur, c.last_seen)
    else:
        # El cooldown no basta: hace falta una ocurrencia posterior a la última
        # notificada. Si el evento nuevo llega durante el cooldown, la marca no
        # avanza y la siguiente evaluación sí avisa.
        notify = has_new and _should_notify(now, notified_at_cur, cooldown_minutes)
        if notify:
            new_notified_at = _format_iso(now)
            new_notified_through = _latest(notified_through_cur, c.last_seen)

    conn.execute(
        """
        UPDATE incidents
        SET category = ?,
            severity = ?,
            sample_message = ?,
            first_seen = CASE WHEN ? < first_seen THEN ? ELSE first_seen END,
            last_seen = CASE WHEN ? > last_seen THEN ? ELSE last_seen END,
            event_count = ?,
            status = ?,
            notified_at = ?,
            notified_through = ?
        WHERE id = ?
        """,
        (
            c.category,
            c.severity,
            c.sample_message,
            c.first_seen,
            c.first_seen,
            c.last_seen,
            c.last_seen,
            c.event_count,
            new_status,
            new_notified_at,
            new_notified_through,
            inc_id,
        ),
    )

    return {
        "id": inc_id,
        "fingerprint": c.fingerprint,
        "status": new_status,
        "notified": notify,
    }


def evaluate_incidents(
    conn: sqlite3.Connection,
    *,
    window_minutes: int | None = None,
    min_count: int | None = None,
    min_severity: int | None = None,
) -> dict:
    """Evalúa y actualiza incidentes. Devuelve resumen.

    Args:
        conn: conexión SQLite (escritura)
        window_minutes: override ventana
        min_count: override umbral
        min_severity: override severidad mínima para candidato
    """
    cfg = get_settings().analysis
    win = cfg.window_minutes if window_minutes is None else window_minutes
    thr = cfg.incident_threshold if min_count is None else min_count
    sev_min = cfg.min_incident_severity if min_severity is None else min_severity

    # get patterns
    # `limit=None`: cada huella sobre el umbral debe generar incidente. Un tope
    # aquí abriría solo los N primeros sin que nada lo advierta.
    patterns = recurring_patterns(
        conn, window_minutes=win, min_count=thr, min_severity=sev_min, limit=None
    )
    candidates = _candidates_from_patterns(
        patterns,
        AnalysisConfig(
            rules_path=cfg.rules_path,
            min_incident_severity=sev_min,
            window_minutes=win,
            incident_threshold=thr,
            cooldown_minutes=cfg.cooldown_minutes,
            anomaly_zscore=cfg.anomaly_zscore,
            anomaly_min_samples=cfg.anomaly_min_samples,
        ),
    )

    now = datetime.now(UTC)
    results = []
    with transaction(conn):
        for c in candidates:
            res = _upsert_incident(conn, c, now, cfg.cooldown_minutes)
            results.append(res)

    return {
        "evaluated": len(candidates),
        "upserted": len(results),
        "results": results,
        "window_minutes": win,
        "incident_threshold": thr,
        "cooldown_minutes": cfg.cooldown_minutes,
    }
