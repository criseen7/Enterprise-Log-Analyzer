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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime:
    # timestamp_utc viene como YYYY-MM-DDTHH:MM:SSZ
    if ts.endswith("Z"):
        return datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _format_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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
    # get existing
    row = conn.execute(
        """
        SELECT id, status, notified_at, event_count, first_seen, last_seen
        FROM incidents
        WHERE fingerprint = ?
        """,
        (c.fingerprint,),
    ).fetchone()
    if row is None:
        notify = _should_notify(now, None, cooldown_minutes)
        conn.execute(
            """
            INSERT INTO incidents (
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
                _format_iso(now) if notify else None,
            ),
        )
        inc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {
            "id": inc_id,
            "fingerprint": c.fingerprint,
            "status": "OPEN",
            "notified": notify,
        }
    else:
        inc_id, status, notified_at_cur, event_count_cur, first_seen_cur, last_seen_cur = row
        # update counts/timestamps, keep status unless explicitly managed
        notify = _should_notify(now, notified_at_cur, cooldown_minutes)
        new_notified = notified_at_cur
        if notify:
            new_notified = _format_iso(now)
        conn.execute(
            """
            UPDATE incidents
            SET category = ?,
                severity = ?,
                sample_message = ?,
                first_seen = CASE WHEN ? < first_seen THEN ? ELSE first_seen END,
                last_seen = CASE WHEN ? > last_seen THEN ? ELSE last_seen END,
                event_count = MAX(event_count, ?),
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
                new_notified,
                inc_id,
            ),
        )
        # restore first_seen/last_seen precisely per candidate boundaries? simpler: use candidate if extends
        conn.execute(
            "UPDATE incidents SET last_seen = ? WHERE id = ? AND ? > last_seen",
            (c.last_seen, inc_id, c.last_seen),
        )
        conn.execute(
            "UPDATE incidents SET first_seen = ? WHERE id = ? AND ? < first_seen",
            (c.first_seen, inc_id, c.first_seen),
        )
        # event_count: candidate count is total in window? store max or sum? better reflect event_count seen; but spec says "upsert of incident" - keep max or set to candidate
        conn.execute(
            "UPDATE incidents SET event_count = ? WHERE id = ? AND ? > event_count",
            (c.event_count, inc_id, c.event_count),
        )
        return {
            "id": inc_id,
            "fingerprint": c.fingerprint,
            "status": status,
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
    patterns = recurring_patterns(
        conn, window_minutes=win, min_count=thr, min_severity=sev_min
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
