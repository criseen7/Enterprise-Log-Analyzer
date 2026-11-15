"""Orquestación de la ingesta: estado en disco → eventos en SQLite.

El orden de las operaciones es lo que hace la ingesta recuperable:

1. Se abre el archivo **una sola vez** y se lee su identidad (`fstat` + hash de
   cabecera) del mismo descriptor.
2. `resolve_log_file()` — averigua la fila y el offset desde el que leer usando
   esa identidad.
3. Se leen y parsean las líneas nuevas, por lotes acotados.
4. Se insertan los eventos.
5. **Solo entonces** se avanza `last_offset`, en la misma transacción corta que
   la inserción del lote.

Si el paso 4 falla, el offset no se mueve y la siguiente pasada reintenta esas
líneas. Al revés, un offset avanzado sin eventos insertados perdería datos para
siempre: el archivo ya no volvería a entregar esas líneas.

Abrir una vez y leer del descriptor cierra el TOCTOU clásico: entre `stat(path)`
y `open(path)` el archivo puede rotar, y se acabaría resolviendo la identidad de
un contenido y leyendo otro. Con `os.open` + `os.fstat` ambos describen el mismo
archivo aunque la ruta cambie después.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.analysis.errors import ErrorRules
from app.analysis.fingerprint import event_fingerprint
from app.db.connection import transaction
from app.db.repository import (
    InsertCounts,
    count_events,
    event_row,
    finish_run,
    head_hash_from_fd,
    insert_events,
    parse_coverage,
    resolve_log_file,
    start_run,
    update_progress,
)
from app.ingest.reader import RawEntry, iter_entry_batches
from app.parser.registry import Registry


@dataclass
class FileResult:
    path: Path
    status: str  # ingested | up_to_date | rotated | vanished | error
    reason: str
    entries: int = 0
    counts: InsertCounts = field(default_factory=InsertCounts)
    unparsed: int = 0
    new_offset: int = 0


@dataclass
class RunResult:
    run_id: int
    files: list[FileResult] = field(default_factory=list)
    lines: int = 0
    inserted: int = 0
    skipped: int = 0
    unparsed: int = 0
    duration_ms: int = 0
    status: str = "OK"
    errors: list[str] = field(default_factory=list)


def _fstat_fd(fd: int) -> os.stat_result:
    """Indirección mínima para poder forzar un inodo en los tests de reciclado."""
    return os.fstat(fd)


def _parse_entries(
    registry: Registry,
    rules: ErrorRules,
    file_id: int,
    entries: list[RawEntry],
) -> tuple[list[tuple], int]:
    """Convierte entradas en filas de `events`. No toca la base."""
    rows: list[tuple] = []
    unparsed = 0
    for entry in entries:
        result = registry.parse_line(entry.text)
        if not result.parsed_ok:
            unparsed += 1
        # `message` puede ser None en una línea vacía: la huella se calcula
        # sobre lo que haya, igual que hace el backfill con COALESCE(message, raw_line).
        text = result.message or result.raw_line
        category = rules.category(text)
        rows.append(
            event_row(
                file_id,
                entry.offset,
                result,
                event_fingerprint(category, text),
                category,
            )
        )
    return rows, unparsed


def _commit_batch(
    conn: sqlite3.Connection,
    file_id: int,
    rows: list[tuple],
    *,
    offset: int,
    size: int,
    inode: int,
) -> InsertCounts:
    """Inserta un lote y avanza el offset en la misma transacción corta.

    Si el llamador ya abrió una transacción (los tests lo hacen para demostrar
    el rollback completo), no se anida: se participa de la externa y el COMMIT
    queda en manos de quien la abrió. En operación normal, cada lote es una
    transacción independiente: un archivo de millones de líneas no bloquea la
    base durante toda la pasada ni acumula las filas en memoria.
    """
    if conn.in_transaction:
        counts = insert_events(conn, rows)
        update_progress(conn, file_id, offset=offset, size=size, inode=inode)
        return counts

    with transaction(conn):
        counts = insert_events(conn, rows)
        update_progress(conn, file_id, offset=offset, size=size, inode=inode)
    return counts


def ingest_file(
    conn,
    path: Path,
    registry: Registry,
    chunk_bytes: int = 1_048_576,
    rules: ErrorRules | None = None,
) -> FileResult:
    """Ingesta un archivo. Idempotente: llamarlo dos veces no duplica eventos.

    `rules` clasifica cada evento en el momento de escribirlo (`category` +
    `fingerprint`). Si no se pasa, se usan las reglas empaquetadas: quien llame
    con `analysis.rules_path` custom debe pasarlas, si no, dos rutas de
    clasificación convivirían en la misma base.
    """
    rules = rules or ErrorRules.default()
    resolved = path.resolve()
    try:
        fd = os.open(resolved, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return FileResult(path, "vanished", "no existe en disco")

    entries = 0
    counts = InsertCounts()
    unparsed = 0
    new_offset = 0

    try:
        stat_result = _fstat_fd(fd)
        state = resolve_log_file(
            conn,
            path,
            stat_result=stat_result,
            head_hash=head_hash_from_fd(fd),
        )
        if state is None:
            return FileResult(path, "vanished", "no existe en disco")

        new_offset = state.offset
        handle = os.fdopen(fd, "rb")
        fd = -1  # el descriptor pasa a ser propiedad del handle
        with handle:
            batches = iter_entry_batches(handle, state.offset, registry, chunk_bytes)
            for batch, batch_end in batches:
                rows, batch_unparsed = _parse_entries(registry, rules, state.file_id, batch)
                batch_counts = _commit_batch(
                    conn,
                    state.file_id,
                    rows,
                    offset=batch_end,
                    size=max(stat_result.st_size, batch_end),
                    inode=stat_result.st_ino,
                )
                entries += len(batch)
                counts.inserted += batch_counts.inserted
                counts.skipped += batch_counts.skipped
                unparsed += batch_unparsed
                new_offset = batch_end
    finally:
        if fd >= 0:
            os.close(fd)

    status = "ingested" if entries else "up_to_date"
    if state.reason in {"truncated", "recycled", "copied"}:
        status = "rotated"

    return FileResult(
        path=path,
        status=status,
        reason=state.reason,
        entries=entries,
        counts=counts,
        unparsed=unparsed,
        new_offset=new_offset,
    )


def ingest_paths(
    conn,
    registry: Registry,
    paths: Iterable[Path],
    chunk_bytes: int = 1_048_576,
    rules: ErrorRules | None = None,
) -> RunResult:
    """Ingesta un conjunto de archivos y deja constancia en `ingest_runs`."""
    started = time.perf_counter()
    run_id = start_run(conn)
    files: list[FileResult] = []
    lines = inserted = skipped = unparsed = 0

    for path in paths:
        try:
            result = ingest_file(conn, path, registry, chunk_bytes, rules)
        except Exception as exc:  # noqa: BLE001 - un archivo malo no corta la pasada
            files.append(FileResult(path, "error", f"{type(exc).__name__}: {exc}"))
            continue

        files.append(result)
        lines += result.entries
        inserted += result.counts.inserted
        skipped += result.counts.skipped
        unparsed += result.unparsed

    duration_ms = int((time.perf_counter() - started) * 1000)
    errors = [f"{f.path.name}: {f.reason}" for f in files if f.status == "error"]
    status = "ERROR" if errors else "OK"
    finish_run(
        conn,
        run_id,
        files_processed=len(files),
        lines_read=lines,
        events_inserted=inserted,
        events_skipped=skipped,
        events_unparsed=unparsed,
        duration_ms=duration_ms,
        status=status,
        error="; ".join(errors) or None,
    )

    return RunResult(
        run_id=run_id,
        files=files,
        lines=lines,
        inserted=inserted,
        skipped=skipped,
        unparsed=unparsed,
        duration_ms=duration_ms,
        status=status,
        errors=errors,
    )


def discover(config) -> list[Path]:
    """Archivos candidatos a ingerir, en orden determinista.

    Delega en `Settings.log_files`, que ya aplica `ingest.include_patterns` y
    deduplica. No se mantiene una segunda implementación del listado: dos
    listas divergentes producen una ingesta que cambia según qué función se
    llame, que es la peor forma de bug de silencioso.
    """
    return list(config.log_files)


def summarize(conn: sqlite3.Connection, run: RunResult) -> str:
    total, ok = parse_coverage(conn)
    cov = f", cobertura {ok}/{total}" if total else ""
    return (
        f"run #{run.run_id}: {run.inserted} insertados, {run.skipped} omitidos, "
        f"{run.unparsed} sin parsear, {len(run.files)} archivos, {run.duration_ms}ms{cov}"
    )


def ingest_paths_transactional(
    conn: sqlite3.Connection,
    registry: Registry,
    paths: Iterable[Path],
    chunk_bytes: int = 1_048_576,
    rules: ErrorRules | None = None,
) -> RunResult:
    """Pasada de producción: inserción y offset atómicos **por lote**.

    Antes se envolvía la pasada completa en una transacción; eso garantizaba
    todo-o-nada a costa de mantener el lock de escritura durante la lectura y
    el parseo de todos los archivos, y de acumular todas las filas en memoria.
    Ahora la unidad atómica es el lote: cada uno inserta sus eventos y avanza
    `last_offset` en la misma transacción. Un fallo solo deja pendiente el lote
    en curso, nunca una fila con offset adelantado y sin eventos.

    Se conserva el nombre para no romper a los llamadores (CLI, watcher,
    dashboard y evals); la semántica de recuperación ya no depende de una
    transacción global.
    """
    return ingest_paths(conn, registry, paths, chunk_bytes, rules)


__all__ = [
    "FileResult",
    "RunResult",
    "count_events",
    "discover",
    "event_fingerprint",
    "ingest_file",
    "ingest_paths",
    "ingest_paths_transactional",
    "summarize",
]
