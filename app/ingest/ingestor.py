"""Orquestación de la ingesta: estado en disco → eventos en SQLite.

El orden de las operaciones es lo que hace la ingesta recuperable:

1. `resolve_log_file()` — averigua la fila y el offset desde el que leer.
2. Se leen y parsean las líneas nuevas.
3. Se insertan los eventos.
4. **Solo entonces** se avanza `last_offset`.

Si el paso 3 falla, el offset no se mueve y la siguiente pasada reintenta esas
líneas. Al revés, un offset avanzado sin eventos insertados perdería datos para
siempre: el archivo ya no volvería a entregar esas líneas.
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
from app.db.repository import (
    InsertCounts,
    count_events,
    event_row,
    finish_run,
    insert_events,
    parse_coverage,
    resolve_log_file,
    start_run,
    update_progress,
)
from app.ingest.reader import iter_entries
from app.parser.registry import Registry


@dataclass
class FileResult:
    path: Path
    status: str  # ingested | up_to_date | rotated | vanished
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
    state = resolve_log_file(conn, path)
    if state is None:
        return FileResult(path, "vanished", "no existe en disco")

    rules = rules or ErrorRules.default()
    entries, new_offset = iter_entries(path, state.offset, registry, chunk_bytes)

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
                state.file_id,
                entry.offset,
                result,
                event_fingerprint(category, text),
                category,
            )
        )

    counts = insert_events(conn, rows)
    if rows:
        update_progress(
            conn,
            state.file_id,
            offset=new_offset,
            size=path.stat().st_size,
            inode=os.stat(path).st_ino,
        )

    status = "ingested" if rows else "up_to_date"
    if state.reason in {"rotated", "truncated", "recycled"}:
        status = "rotated"

    return FileResult(
        path=path,
        status=status,
        reason=state.reason,
        entries=len(entries),
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
    """Como `ingest_paths`, pero con los cambios de DB en una transacción.

    Es la forma de usar en producción: o queda registrada la pasada completa con
    sus eventos, o no queda nada. Sin esto, un fallo a mitad deja `last_offset`
    adelantado en un archivo y sin eventos, es decir, datos perdidos.
    """
    from app.db.connection import transaction

    with transaction(conn):
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
