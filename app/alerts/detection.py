"""Detección de incidentes basada en recurrencia por ventana.

Toma los patrones recurrentes (mismo fingerprint en ventana) y crea/actualiza
incidentes con upsert por fingerprint, respetando cooldown vía `notified_at`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


def _utcnow_iso() -> str:
    return to_utc_iso(datetime.now(timezone.utc))


def _parse_iso(ts: str) -> datetime:
    # `timestamp_utc` viene como YYYY-MM-DDTHH:MM:SS[.mmm]Z. `fromisoformat`
    # entiende las dos formas; el fallback cubre cadenas sin sufijo de zona.
    moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _format_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
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


def _upsert_incident(conn: sqlite3.Connection, c: IncidentCandidate, now: datetime, cooldown_minutes: int) -> dict:
    """Crea o actualiza un incidente por fingerprint.

    Reapertura automática: si el incidente estaba `RESOLVED` y vuelve a
    detectarse recurrencia, pasa a `OPEN` y reinicia `notified_at` (cooldown).
    `event_count` refleja el conteo de la ventana, no un máximo acumulado.
    """
    notify_on_insert = _should_notify(now, None, cooldown_minutes)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO incidents (
            fingerprint, category, severity, sample_message,
            first_seen, last_seen, event_count, status, notified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
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
        SELECT id, status, notified_at, event_count, first_seen, last_seen
        FROM incidents
        WHERE fingerprint = ?
        """,
        (c.fingerprint,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"incidente no insertado ni encontrado: {c.fingerprint!r}"
        )

    inc_id, status_cur, notified_at_cur = row["id"], row["status"], row["notified_at"]
    new_status = status_cur
    new_notified_at = notified_at_cur
    notify = False

    if status_cur == "RESOLVED":
        new_status = "OPEN"
        new_notified_at = None
    else:
        notify = _should_notify(now, notified_at_cur, cooldown_minutes)
        if notify:
            new_notified_at = _format_iso(now)
        else:
            new_notified_at = notified_at_cur

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
            notified_at = ?
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
    win = window_minutes or cfg.window_minutes
    thr = min_count or cfg.incident_threshold
    sev_min = min_severity or cfg.min_incident_severity

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

    now = datetime.now(timezone.utc)
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
