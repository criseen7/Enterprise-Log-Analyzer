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

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from app.parser.registry import ParseResult

_UNSET = object()

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
    reason: str  # new | resumed | truncated | renamed | recycled | copied
    # Inodo con el que se resolvió la fila. `update_progress` lo usa como
    # guarda: si el archivo cambió de identidad durante la lectura, no se
    # avanza el offset de una fila que ya no describe ese contenido.
    inode: int | None = None


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


_HEAD_BYTES = 4096


def _hash_head(head: bytes) -> str | None:
    """SHA-256 (truncado) de la primera línea completa, o `None` si no la hay.

    Si la primera línea aún no está completa (sin `\\n`) no se devuelve huella:
    todavía puede cambiar y compararla crearía generaciones espurias.
    """
    cut = head.find(b"\n")
    if cut < 0:
        return None
    return hashlib.sha256(head[: cut + 1]).hexdigest()[:16]


def head_hash_from_fd(fd: int) -> str | None:
    """Huella de la cabecera leída del **mismo** descriptor que se va a ingerir.

    Es la pieza que cierra el TOCTOU: si la ruta se renombra o se reemplaza
    entre el `stat` y la lectura, un `open(path)` leería otro archivo que el que
    resolvió la identidad. Aquí el hash y el `fstat` vienen del descriptor
    abierto, así que ambos describen el contenido que realmente se lee.
    """
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        head = os.read(fd, _HEAD_BYTES)
    except OSError:
        return None
    finally:
        os.lseek(fd, position, os.SEEK_SET)
    return _hash_head(head)


def _head_hash(path: Path) -> str | None:
    """Huella de la cabecera abriendo la ruta. Solo para llamadores sin fd."""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError:
        return None
    return _hash_head(head)


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


def _copy_source(
    conn: sqlite3.Connection, path: str, inode: int, head: str | None, size: int
) -> sqlite3.Row | None:
    """Fila de origen de una copia hecha por `copytruncate`, si existe.

    `copytruncate` hace `cp app.log app.log.1` y luego trunca `app.log`. El
    archivo nuevo (`app.log.1`) tiene un inode distinto, así que por identidad
    parece "archivo nuevo" y se leería desde cero: cada evento del prefijo se
    insertaría de nuevo, esta vez bajo otro `file_id`.

    Para reconocerlo se busca otra fila con la misma cabecera (`head_hash`) cuyo
    `last_offset` ya cubra contenido y quepa en la copia (`last_offset <= size`).
    La nueva fila nace con ese offset heredado, de modo que solo se lee lo que
    aún no se había ingerido. Exigir que el nombre sea una rotación del origen
    (`app.log` -> `app.log.1`) evita que dos logs distintos que casualmente
    empiezan igual se confundan.
    """
    if head is None:
        return None
    row = conn.execute(
        "SELECT * FROM log_files WHERE inode IS NOT NULL AND inode <> ? AND path <> ? "
        "AND head_hash = ? AND last_offset > 0 AND last_offset <= ? "
        "ORDER BY last_offset DESC, id DESC LIMIT 1",
        (inode, path, head, size),
    ).fetchone()
    if row is None:
        return None
    if not Path(path).name.startswith(Path(row["path"]).name + "."):
        return None
    return row


def resolve_log_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    stat_result: os.stat_result | None = None,
    head_hash: object = _UNSET,
) -> FileState | None:
    """Determina la fila de `log_files` para `path` y el offset desde el que leer.

    Devuelve `None` si el archivo no existe (puede haberse rotado entre el
    listado y el read).

    `stat_result` y `head_hash` permiten inyectar la identidad leída de un
    descriptor ya abierto. El ingestor los usa para que el `fstat`/hash y la
    lectura posterior describan el mismo archivo aunque la ruta rote entre
    ambos pasos. Sin ellos se abre la ruta, que es lo correcto para un llamador
    que solo quiere consultar el estado.

    Los seis casos, en orden de comprobación:

    | Situación                     | Detección                          | Acción                |
    |-------------------------------|------------------------------------|-----------------------|
    | Archivo nuevo                 | inode desconocido                  | nueva fila, offset 0  |
    | Copia de `copytruncate`       | inode nuevo + misma cabecera       | offset heredado       |
    | Sigue creciendo               | tamaño ≥ `last_offset`             | continuar en el offset|
    | Rotado por rename             | inode conocido con otra ruta       | actualizar la ruta    |
    | Rotado por truncado           | tamaño < `last_offset`             | nueva generación      |
    | Inodo reciclado               | mismo inode, otra cabecera         | nueva generación      |

    El caso de rename es el que evita duplicados: cuando logrotate renombra
    `app.log` a `app.log.1`, el contenido ya fue ingerido con otra ruta. Reconocer
    el inode impide leerlo otra vez.

    El inodo solo no basta: al borrar un archivo, el SO puede dar su número a uno
    nuevo. Se compara además `head_hash` (primera línea completa): si cambió y ya había
    contenido ingerido, es otro archivo, no un rename. La comparación también
    detecta la reescritura de igual tamaño que el tamaño solo no ve.
    """
    resolved = path.resolve()
    if stat_result is None:
        inode, size = _stat(resolved)
        if inode is None:
            return None
    else:
        inode, size = stat_result.st_ino, stat_result.st_size

    head = _head_hash(resolved) if head_hash is _UNSET else cast(str | None, head_hash)
    stamp = now_iso()
    latest = _latest_for_inode(conn, inode)

    if latest is None:
        copy_source = _copy_source(conn, str(resolved), inode, head, size)
        offset = int(copy_source["last_offset"]) if copy_source is not None else 0
        reason = "copied" if copy_source is not None else "new"
        generation = _next_generation(conn, str(resolved), inode)
        cursor = conn.execute(
            "INSERT INTO log_files (path, inode, generation, size, last_offset, last_size, "
            "head_hash, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(resolved), inode, generation, size, offset, offset, head, stamp, stamp),
        )
        _supersede(conn, str(resolved), cursor.lastrowid, stamp)
        return FileState(cursor.lastrowid, offset, generation, reason, inode)

    renamed = latest["path"] != str(resolved)
    # Solo se compara cuando ya se ingirió contenido (`last_offset > 0`): un
    # archivo recién creado y vacío cambia de cabecera al recibir sus primeras
    # líneas y sigue siendo el mismo archivo. Un `head_hash` NULL es una fila
    # anterior a v7: se rellena abajo sin crear generación.
    head_changed = (
        latest["last_offset"] > 0
        and latest["head_hash"] is not None
        and head is not None
        and latest["head_hash"] != head
    )

    if size < latest["last_offset"] or head_changed:
        # Truncado o reescrito en el mismo inode: el contenido cambió, es una
        # generación nueva. NO se reinicia el offset sobre la misma fila: la
        # UNIQUE(file_id, offset) descartaría los eventos nuevos como duplicados.
        # Si además cambió la ruta, el inodo fue reciclado por otro archivo.
        generation = latest["generation"] + 1
        reason = "recycled" if renamed else "truncated"
        cursor = conn.execute(
            "INSERT INTO log_files (path, inode, generation, size, last_offset, last_size, "
            "head_hash, first_seen, last_seen, rotated_at) "
            "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?)",
            (str(resolved), inode, generation, size, head, stamp, stamp, stamp),
        )
        conn.execute(
            "UPDATE log_files SET is_active = 0, rotated_at = ? WHERE id = ?",
            (stamp, latest["id"]),
        )
        _supersede(conn, str(resolved), cursor.lastrowid, stamp)
        return FileState(cursor.lastrowid, 0, generation, reason, inode)

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
        "UPDATE log_files SET size = ?, last_size = ?, last_seen = ?, is_active = 1, "
        "head_hash = COALESCE(?, head_hash) WHERE id = ?",
        (size, size, stamp, head, latest["id"]),
    )
    # Una fila anterior que ocupaba esta ruta y no es este inode: ya no refleja
    # lo que hay en disco ahora, aunque su nombre sea el mismo.
    _supersede(conn, str(resolved), latest["id"], stamp)
    return FileState(
        latest["id"],
        latest["last_offset"],
        latest["generation"],
        "renamed" if renamed else "resumed",
        inode,
    )


def update_progress(
    conn: sqlite3.Connection, file_id: int, *, offset: int, size: int, inode: int | None
) -> bool:
    """Guarda hasta dónde se leyó. Se llama tras confirmar la inserción.

    El offset solo avanza si los eventos se insertaron: si la transacción falla,
    `last_offset` no se mueve y la próxima pasada reintenta esas líneas. Ese
    orden (insertar, luego avanzar el offset) es el que hace segura la recuperación
    de una corrida interrumpida.

    `inode` es el del descriptor desde el que se leyó, no el que hay en la
    ruta ahora. La condición `inode = ?` es la guarda real: si otra pasada
    creó una generación nueva (o la fila fue desactivada por una rotación), el
    UPDATE no toca una fila que ya no describe el contenido leído. Con el
    lector por descriptor, el inodo del `fstat` y el de la lectura coinciden por
    construcción; la guarda protege del caso en que otra escritura mueva la
    fila entre resolver y confirmar.

    Devuelve si la fila se actualizó. Un `False` no es un error: es la señal de
    que el llamador leyó un archivo que dejó de ser el mismo.
    """
    cursor = conn.execute(
        "UPDATE log_files SET last_offset = ?, last_size = ?, size = ?, last_seen = ? "
        "WHERE id = ? AND inode = ? AND is_active = 1",
        (offset, size, size, now_iso(), file_id, inode),
    )
    return cursor.rowcount == 1


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
