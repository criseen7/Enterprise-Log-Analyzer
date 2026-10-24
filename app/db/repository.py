"""Acceso a la base. Aquí vive la idempotencia.

Dos funciones definen la correctitud de la ingesta:

* `resolve_log_file()` — decide qué fila de `log_files` corresponde al archivo que
  hay en disco ahora, y en qué offset hay que seguir leyendo.
* `insert_events()` — inserta con `INSERT OR IGNORE` sobre `UNIQUE(file_id, offset)`.

La clave de idempotencia es `(file_id, offset)`, y `file_id` se deriva de
`(inode, generation)` en vez de la ruta. Ver ADR-003 para el porqué: rotar un
log cambia la ruta, o la deja igual pero con el contenido truncado, y las dos
cosas rompen cualquier esquema que use la ruta como identidad.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.parser.registry import ParseResult

# Columnas de `events` que el ingestor rellena, en el orden del INSERT.
EVENT_COLUMNS = (
    "file_id",
    "offset",
    "timestamp_utc",
    "timestamp_raw",
    "source_tz",
    "level_raw",
    "level_norm",
    "severity",
    "service",
    "logger",
    "message",
    "fingerprint",
    "category",
    "raw_line",
    "parsed_ok",
    "parse_error",
)

_INSERT_EVENTS_SQL = (
    f"INSERT OR IGNORE INTO events ({', '.join(EVENT_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(EVENT_COLUMNS))})"
)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True)
class FileState:
    """Resultado de resolver un archivo en disco contra `log_files`."""

    file_id: int
    offset: int
    generation: int
    reason: str  # new | resumed | rotated | truncated | renamed


@dataclass
class InsertCounts:
    inserted: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.skipped


# --- resolución de archivo ---------------------------------------------


def _stat(path: Path) -> tuple[int | None, int]:
    """`(inode, size)`, o `(None, 0)` si el archivo desapareció entre check y read."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, 0
    return st.st_ino, st.st_size


def _latest_for_inode(conn: sqlite3.Connection, inode: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM log_files WHERE inode = ? ORDER BY generation DESC LIMIT 1",
        (inode,),
    ).fetchone()


def _next_generation(conn: sqlite3.Connection, path: str, inode: int | None) -> int:
    """Siguiente número de generación libre.

    Se mira también la ruta, no solo el inode: si un archivo reusa un inode que ya
    liberó otro, hay que seguir numerando para no chocar con la
    `UNIQUE (inode, generation)`.
    """
    rows = conn.execute(
        "SELECT MAX(generation) AS g FROM log_files WHERE inode = ? OR path = ?",
        (inode, path),
    ).fetchone()
    return int(rows["g"] or -1) + 1


def _supersede(conn: sqlite3.Connection, path: str, keep_id: int, stamp: str) -> None:
    """Marca como inactivas las demás filas que apuntan a `path`.

    Con `path` mutable y `UNIQUE (inode, generation)` puede haber varias filas
    históricas en la misma ruta. Solo una describe el archivo que hoy existe ahí:
    la que acabamos de resolver. Las demás referencian contenido que ya no está en
    esa ruta, y dejarlas activas tiene dos costes concretos:

      * el dashboard las contaría como archivos vigilados cuando no existen;
      * su `last_offset` apunta al contenido viejo, así que una pasada posterior
        podría reentrar por esa fila y leer desde una posición que ya no
        corresponde a nada.

    Solo se desactivan filas con inode conocido: una con `inode IS NULL` no
    permite decidir, y apagarla en ciegas podría ocultar un archivo real.
    """
    conn.execute(
        "UPDATE log_files SET is_active = 0, rotated_at = COALESCE(rotated_at, ?) "
        "WHERE path = ? AND id <> ? AND inode IS NOT NULL",
        (stamp, path, keep_id),
    )


def resolve_log_file(conn: sqlite3.Connection, path: Path) -> FileState | None:
    """Determina la fila de `log_files` para `path` y el offset desde el que leer.

    Devuelve `None` si el archivo no existe (puede haberse rotado entre el
    listado y el read).

    Los cuatro casos, en orden de comprobación:

    | Situación                     | Detección                          | Acción                |
    |-------------------------------|------------------------------------|-----------------------|
    | Archivo nuevo                 | inode desconocido                  | nueva fila, offset 0  |
    | Sigue creciendo               | tamaño ≥ `last_offset`             | continuar en el offset|
    | Rotado por rename             | inode conocido con otra ruta       | actualizar la ruta    |
    | Rotado por truncado           | tamaño < `last_offset`             | nueva generación     |

    El caso de rename es el que evita duplicados: cuando logrotate renombra
    `app.log` a `app.log.1`, el contenido ya fue ingerido con otra ruta. Reconocer
    el inode impide leerlo otra vez.
    """
    resolved = path.resolve()
    inode, size = _stat(resolved)
    if inode is None:
        return None

    stamp = now_iso()
    latest = _latest_for_inode(conn, inode)

    if latest is None:
        generation = _next_generation(conn, str(resolved), inode)
        cursor = conn.execute(
            "INSERT INTO log_files (path, inode, generation, size, last_offset, last_size, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, 0, 0, ?, ?)",
            (str(resolved), inode, generation, size, stamp, stamp),
        )
        _supersede(conn, str(resolved), cursor.lastrowid, stamp)
        return FileState(cursor.lastrowid, 0, generation, "new")

    renamed = latest["path"] != str(resolved)

    if size < latest["last_offset"]:
        # Truncado en el mismo inode: el contenido cambió, es una generación nueva.
        # NO se reinicia el offset sobre la misma fila: la UNIQUE(file_id, offset)
        # descartaría los eventos nuevos como duplicados.
        generation = latest["generation"] + 1
        cursor = conn.execute(
            "INSERT INTO log_files (path, inode, generation, size, last_offset, last_size, "
            "first_seen, last_seen, rotated_at) VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?)",
            (str(resolved), inode, generation, size, stamp, stamp, stamp),
        )
        conn.execute(
            "UPDATE log_files SET is_active = 0, rotated_at = ? WHERE id = ?",
            (stamp, latest["id"]),
        )
        _supersede(conn, str(resolved), cursor.lastrowid, stamp)
        return FileState(cursor.lastrowid, 0, generation, "truncated")

    if renamed:
        # `is_active = 1` no es redundante: el orden de descubrimiento importa.
        # Si `app.log` se procesa antes que `app.log.1`, el archivo nuevo pisa
        # la ruta que esta fila aún declaraba y `_supersede` la desactiva; aquí
        # se sabe que el inode sigue vivo en esta ruta, así que vuelve a activarse.
        conn.execute(
            "UPDATE log_files SET path = ?, last_seen = ?, is_active = 1 WHERE id = ?",
            (str(resolved), stamp, latest["id"]),
        )

    conn.execute(
        "UPDATE log_files SET size = ?, last_size = ?, last_seen = ?, is_active = 1 WHERE id = ?",
        (size, size, stamp, latest["id"]),
    )
    # Una fila anterior que ocupaba esta ruta y no es este inode: ya no refleja
    # lo que hay en disco ahora, aunque su nombre sea el mismo.
    _supersede(conn, str(resolved), latest["id"], stamp)
    return FileState(
        latest["id"],
        latest["last_offset"],
        latest["generation"],
        "renamed" if renamed else "resumed",
    )


def update_progress(
    conn: sqlite3.Connection, file_id: int, *, offset: int, size: int, inode: int | None
) -> None:
    """Guarda hasta dónde se leyó. Se llama tras confirmar la inserción.

    El offset solo avanza si los eventos se insertaron: si la transacción falla,
    `last_offset` no se mueve y la próxima pasada reintenta esas líneas. Ese
    orden (insertar, luego avanzar el offset) es el que hace segura la recuperación
    de una corrida interrumpida.
    """
    conn.execute(
        "UPDATE log_files SET last_offset = ?, last_size = ?, size = ?, last_seen = ?, inode = ? "
        "WHERE id = ?",
        (offset, size, size, now_iso(), inode, file_id),
    )


# --- inserción de eventos ----------------------------------------------


def event_row(
    file_id: int,
    offset: int,
    result: ParseResult,
    fingerprint: str | None = None,
    category: str | None = None,
):
    """Fila de `events` en el orden de `EVENT_COLUMNS`.

    `fingerprint` y `category` se calculan fuera, en el ingestor: decidirlos aquí
    obligaría a que `event_row` cargara las reglas, que son configuración, y a
    que todo caller las tuviera. Quien inserta decide cómo clasificar; la base
    sólo guarda lo decidido.
    """
    return (
        file_id,
        offset,
        result.timestamp_utc,
        result.timestamp_raw,
        result.source_tz,
        result.level_raw,
        result.level_norm,
        result.severity,
        result.service,
        result.logger,
        result.message,
        fingerprint,
        category,
        result.raw_line,
        1 if result.parsed_ok else 0,
        result.parse_error,
    )


def insert_events(conn: sqlite3.Connection, rows: list[tuple]) -> InsertCounts:
    """Inserta eventos ignorando los que ya están.

    `INSERT OR IGNORE` es lo que hace idempotente la ingesta. `skipped` cuenta
    los descartados: es el número que delata una regresión, porque en operación
    normal debería ser 0 salvo al recuperar una corrida interrumpida.
    """
    if not rows:
        return InsertCounts()
    before = conn.total_changes
    conn.executemany(_INSERT_EVENTS_SQL, rows)
    inserted = conn.total_changes - before
    return InsertCounts(inserted=inserted, skipped=len(rows) - inserted)


def count_events(conn: sqlite3.Connection, file_id: int | None = None) -> int:
    if file_id is None:
        return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    return int(
        conn.execute("SELECT COUNT(*) FROM events WHERE file_id = ?", (file_id,)).fetchone()[0]
    )


def last_offset(conn: sqlite3.Connection, file_id: int) -> int:
    row = conn.execute("SELECT last_offset FROM log_files WHERE id = ?", (file_id,)).fetchone()
    return int(row["last_offset"]) if row else 0


# --- auditoría de corridas ---------------------------------------------


def start_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO ingest_runs (started_at, status) VALUES (?, 'RUNNING')", (now_iso(),)
    )
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    files_processed: int,
    lines_read: int,
    events_inserted: int,
    events_skipped: int,
    events_unparsed: int,
    duration_ms: int,
    status: str = "OK",
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE ingest_runs SET finished_at = ?, files_processed = ?, lines_read = ?, "
        "events_inserted = ?, events_skipped = ?, events_unparsed = ?, duration_ms = ?, "
        "status = ?, error = ? WHERE id = ?",
        (
            now_iso(),
            files_processed,
            lines_read,
            events_inserted,
            events_skipped,
            events_unparsed,
            duration_ms,
            status,
            error,
            run_id,
        ),
    )


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,)))


def parse_coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """`(total, ok)`. La cobertura del dashboard sale de aquí, no de Python."""
    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(parsed_ok), 0) FROM events").fetchone()
    return int(row[0]), int(row[1])
