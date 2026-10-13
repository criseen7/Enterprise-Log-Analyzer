"""Gestión de incidentes: consulta, conteo y cambio de estado.

Separado de `detection.py` a propósito: detectar (leer eventos, agrupar,
decidir umbral) y gestionar (mover una fila de `incidents` entre estados) son
dos responsabilidades distintas. `detection.py` escribe incidentes como
consecuencia de un análisis; aquí vive lo que el usuario hace con ellos.

Los estados canónicos son los del esquema: `OPEN`, `ACKNOWLEDGED`, `RESOLVED`.
El plan los abrevia como `ACK`, pero la base guarda la palabra completa y en la
UI se muestra la abreviatura. Guardar `ACK` habría dejado dos vocabularios para
el mismo estado.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

VALID_STATUSES = ("OPEN", "ACKNOWLEDGED", "RESOLVED")

# Abreviatura para pantalla. El valor persistido es siempre el canónico.
_STATUS_LABELS = {
    "OPEN": "OPEN",
    "ACKNOWLEDGED": "ACK",
    "RESOLVED": "RESOLVED",
}

INCIDENT_COLUMNS = (
    "id",
    "fingerprint",
    "category",
    "severity",
    "sample_message",
    "first_seen",
    "last_seen",
    "event_count",
    "status",
    "notified_at",
)


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_incidents(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Incidentes ordenados por severidad y última aparición.

    El orden no es cosmético: lo primero que quiere ver quien abre la página es
    lo más grave y lo más reciente, no el primero que se insertó.
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status inválido: {status!r}, esperado uno de {VALID_STATUSES}")
    sql = f"SELECT {', '.join(INCIDENT_COLUMNS)} FROM incidents"
    params: list[object] = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY severity DESC, last_seen DESC, id DESC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def incident_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Conteo por estado, con las tres claves siempre presentes.

    Devolver claves fijas (a 0) evita que la UI tenga que defenderse de un
    `KeyError` cuando todavía no hay ningún incidente resuelto.
    """
    counts = {status: 0 for status in VALID_STATUSES}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM incidents GROUP BY status"
    ):
        counts[str(row["status"])] = int(row["n"])
    return counts


def get_incident(conn: sqlite3.Connection, incident_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {', '.join(INCIDENT_COLUMNS)} FROM incidents WHERE id = ?",
        (incident_id,),
    ).fetchone()


def set_incident_status(
    conn: sqlite3.Connection, incident_id: int, status: str
) -> bool:
    """Mueve un incidente de estado. Devuelve si existía.

    Reabrir (`RESOLVED` -> `OPEN`) reinicia `notified_at` a NULL a propósito: un
    incidente reabierto es un incidente nuevo para el cooldown. Sin eso, el
    primer rebrote queda silenciado por la notificación anterior y el incidente
    aparece abierto sin haberse avisado.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"status inválido: {status!r}, esperado uno de {VALID_STATUSES}")
    row = get_incident(conn, incident_id)
    if row is None:
        return False
    if status == "OPEN" and row["status"] != "OPEN":
        conn.execute(
            "UPDATE incidents SET status = ?, notified_at = NULL WHERE id = ?",
            (status, incident_id),
        )
    else:
        conn.execute("UPDATE incidents SET status = ? WHERE id = ?", (status, incident_id))
    return True


def set_incident_status_by_fingerprint(
    conn: sqlite3.Connection, fingerprint: str, status: str
) -> bool:
    """Igual que `set_incident_status`, pero por huella.

    La detección indexa por fingerprint (`UNIQUE`), así que la UI, que conoce la
    huella de la fila, no necesita acarrear el `id`.
    """
    row = conn.execute(
        "SELECT id FROM incidents WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if row is None:
        return False
    return set_incident_status(conn, int(row["id"]), status)


def mark_notified(conn: sqlite3.Connection, incident_id: int, when: str | None = None) -> None:
    conn.execute(
        "UPDATE incidents SET notified_at = ? WHERE id = ?",
        (when or _utcnow_iso(), incident_id),
    )


__all__ = [
    "INCIDENT_COLUMNS",
    "VALID_STATUSES",
    "get_incident",
    "incident_counts",
    "list_incidents",
    "mark_notified",
    "set_incident_status",
    "set_incident_status_by_fingerprint",
    "status_label",
]
