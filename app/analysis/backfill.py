"""Backfill de `category` y `fingerprint` sobre eventos ya ingeridos.

La clasificación se hace al ingerir (ver `app/ingest/ingestor.py`), así que los
eventos escritos antes de que existieran —o los de un despliegue que arrancó
sin reglas— llegan con `fingerprint IS NULL`. Sin este módulo no hay forma de
alimentar el dashboard sin borrar y reingerir la base, que es perder el
histórico para arreglar una consulta.

Dos modos, y la diferencia importa:

* por defecto, sólo los que **no** tienen huella: nada de lo ya clasificado se
  toca.
* `--recalc`, todos: se usa cuando alguien cambió `error_rules.yaml` y quiere
  reclasificar el histórico. Es explícito porque reescribe datos — el motivo
  por el que `events.category` se guarda en el momento de la ingesta es que un
  cambio de reglas no debería mover el pasado sin que nadie lo decida.

Se procesa en lotes, y **cada lote es una transacción corta**. Cada `UPDATE`
por su cuenta sería una transacción por evento, pero una sola transacción para
los 100.000 eventos sería peor todavía: `BEGIN IMMEDIATE` retiene el único
escritor de SQLite durante toda la pasada y el watcher se queda sin ingerir.
Con un COMMIT por lote el progreso es durable y el lock se libera entre lotes.
`--recalc` resincroniza los incidentes en una transacción aparte, al final.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

from app.analysis.errors import ErrorRules
from app.analysis.fingerprint import event_fingerprint
from app.db.connection import transaction
from app.timeutil import parse_utc

_PENDING = (
    "SELECT id, message, raw_line, fingerprint FROM events "
    "WHERE fingerprint IS NULL AND id > ? ORDER BY id LIMIT ?"
)
_ALL = "SELECT id, message, raw_line, fingerprint FROM events WHERE id > ? ORDER BY id LIMIT ?"

# Prioridad de estados al fusionar dos incidentes que colapsan en la misma
# huella: OPEN exige acción, RESOLVED no. El estado más accionable gana.
_STATUS_PRIORITY = {"OPEN": 0, "ACKNOWLEDGED": 1, "RESOLVED": 2}


def _max_iso(values: list[str | None]) -> str | None:
    parsed = [(parse_utc(value), value) for value in values if value is not None]
    parsed = [(moment, raw) for moment, raw in parsed if moment is not None]
    if not parsed:
        return None
    return max(parsed, key=lambda item: item[0])[1]


def _merge_status(values: list[str]) -> str:
    return min(values, key=lambda value: _STATUS_PRIORITY.get(value, len(_STATUS_PRIORITY)))


def _resync_incidents(conn: sqlite3.Connection, mapping: dict[str, Counter[str]]) -> None:
    """Remapea incidentes tras `recalc` y los deja coherentes con `events`.

    Recalcular huellas puede cambiar a qué fingerprint pertenece un incidente,
    hacer que varios colapsen en uno solo o dejar huérfano al que ya no tiene
    eventos. Sin esta resincronización, `get_incident` busca la huella vieja y la
    siguiente detección crea un duplicado.
    """
    targets = {
        old: min(counts.items(), key=lambda item: (-item[1], item[0]))[0]
        for old, counts in mapping.items()
    }
    rows = conn.execute(
        "SELECT id, fingerprint, status, notified_at, notified_through, "
        "first_seen, last_seen FROM incidents ORDER BY id"
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        old = str(row["fingerprint"])
        groups.setdefault(targets.get(old, old), []).append(row)

    for target, group in groups.items():
        aggregate = conn.execute(
            """
            SELECT COUNT(*)          AS n,
                   MIN(timestamp_utc) AS first_seen,
                   MAX(timestamp_utc) AS last_seen,
                   MAX(severity)      AS severity,
                   MAX(category)      AS category,
                   MAX(message)       AS sample_message
            FROM events
            WHERE fingerprint = ?
            """,
            (target,),
        ).fetchone()
        if aggregate is None or int(aggregate["n"] or 0) == 0:
            for row in group:
                conn.execute("DELETE FROM incidents WHERE id = ?", (row["id"],))
            continue

        primary = group[0]
        for row in group[1:]:
            conn.execute("DELETE FROM incidents WHERE id = ?", (row["id"],))
        conn.execute(
            """
            UPDATE incidents
            SET fingerprint = ?,
                category = ?,
                severity = ?,
                sample_message = ?,
                first_seen = ?,
                last_seen = ?,
                event_count = ?,
                status = ?,
                notified_at = ?,
                notified_through = ?
            WHERE id = ?
            """,
            (
                target,
                aggregate["category"],
                int(aggregate["severity"] or 0),
                aggregate["sample_message"],
                aggregate["first_seen"] or primary["first_seen"],
                aggregate["last_seen"] or primary["last_seen"],
                int(aggregate["n"]),
                _merge_status([str(row["status"]) for row in group]),
                _max_iso([row["notified_at"] for row in group]),
                _max_iso([row["notified_through"] for row in group]),
                primary["id"],
            ),
        )


def _classify_batch(
    rows: list[sqlite3.Row],
    rules: ErrorRules,
    recalc: bool,
    mapping: dict[str, Counter[str]],
) -> list[tuple[str, str, int]]:
    """Clasifica fuera de la transacción: el trabajo de CPU no retiene el lock."""
    updates: list[tuple[str, str, int]] = []
    for row in rows:
        text = row["message"] or row["raw_line"] or ""
        category = rules.category(text)
        new_fingerprint = event_fingerprint(category, text)
        updates.append((category, new_fingerprint, row["id"]))
        if recalc and row["fingerprint"]:
            mapping.setdefault(str(row["fingerprint"]), Counter())[new_fingerprint] += 1
    return updates


def _store_batch(conn: sqlite3.Connection, updates: list[tuple[str, str, int]]) -> None:
    conn.executemany(
        "UPDATE events SET category = ?, fingerprint = ? WHERE id = ?",
        updates,
    )


def _run(conn: sqlite3.Connection, work) -> None:
    """Transacción corta, salvo que el llamador ya tenga una abierta."""
    if conn.in_transaction:
        work()
    else:
        with transaction(conn):
            work()


def backfill(
    conn: sqlite3.Connection,
    rules: ErrorRules | None = None,
    *,
    recalc: bool = False,
    batch_size: int = 2000,
) -> int:
    """Rellena huellas y categorías. Devuelve el nº de eventos tocados."""
    if batch_size <= 0:
        raise ValueError("batch_size debe ser > 0")

    rules = rules or ErrorRules.default()
    select = _ALL if recalc else _PENDING
    updated = 0
    last_id = 0
    mapping: dict[str, Counter[str]] = {}

    while True:
        rows = conn.execute(select, (last_id, batch_size)).fetchall()
        if not rows:
            break
        updates = _classify_batch(rows, rules, recalc, mapping)
        _run(conn, lambda updates=updates: _store_batch(conn, updates))
        last_id = rows[-1]["id"]
        updated += len(rows)
        if len(rows) < batch_size:
            break

    if recalc:
        _run(conn, lambda: _resync_incidents(conn, mapping))

    return updated


__all__ = ["backfill"]
