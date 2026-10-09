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

Se procesa en lotes y en una sola transacción: cada `UPDATE` por su cuenta
sería una transacción por evento, y 100.000 eventos tardarían minutos y
dejarían la base a medias si algo fallara a mitad.
"""

from __future__ import annotations

import sqlite3

from app.analysis.errors import ErrorRules
from app.analysis.fingerprint import event_fingerprint
from app.db.connection import transaction

_PENDING = "SELECT id, message, raw_line FROM events WHERE fingerprint IS NULL AND id > ? ORDER BY id LIMIT ?"
_ALL = "SELECT id, message, raw_line FROM events WHERE id > ? ORDER BY id LIMIT ?"


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

    with transaction(conn):
        while True:
            rows = conn.execute(select, (last_id, batch_size)).fetchall()
            if not rows:
                break
            for row in rows:
                text = row["message"] or row["raw_line"] or ""
                category = rules.category(text)
                conn.execute(
                    "UPDATE events SET category = ?, fingerprint = ? WHERE id = ?",
                    (category, event_fingerprint(category, text), row["id"]),
                )
                updated += 1
            last_id = rows[-1]["id"]
            if len(rows) < batch_size:
                break

    return updated


__all__ = ["backfill"]
