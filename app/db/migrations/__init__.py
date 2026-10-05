"""Migraciones: `python -m app.db.migrations [--status]`

Un solo archivo de esquema en v1 (`schema.sql`). El runner existe desde el
principio a propósito: con SQLite, cambiar una columna después de que haya
datos significa reescribir la tabla, y descubrir eso en la fase 5 es caro.
La v1 es ya el contrato que lazily hará el resto del sistema.
"""

from __future__ import annotations

from pathlib import Path

from app.db.connection import connect, journal_mode
from app.db.schema import SCHEMA_PATH, SCHEMA_VERSION

_migrations: dict[int, Path] = {
    SCHEMA_VERSION: SCHEMA_PATH,
}


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def applied_versions(conn) -> list[int]:
    if not _table_exists(conn, "schema_version"):
        return []
    return [int(r[0]) for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]


def _current_version(conn) -> int:
    versions = applied_versions(conn)
    return versions[-1] if versions else 0


def status(db_path: str | Path) -> tuple[int, int]:
    """Devuelve (versión actual, versión objetivo)."""
    path = Path(db_path)
    if not path.exists():
        return 0, SCHEMA_VERSION
    conn = connect(path)
    try:
        return _current_version(conn), SCHEMA_VERSION
    finally:
        conn.close()


def migrate(db_path: str | Path, *, target: int | None = None) -> list[int]:
    """Aplica las migraciones faltantes. Idempotente: correrla dos veces no hace nada."""
    # `is None` y no `or`: target=0 es un valor legítimo (pedir v0 explícitamente)
    # y con `or` se convertiría silenciosamente en SCHEMA_VERSION.
    target = SCHEMA_VERSION if target is None else target
    applied: list[int] = []

    conn = connect(db_path)
    try:
        current = _current_version(conn)
        if current > target:
            raise RuntimeError(
                f"la base está en v{current}, más nueva que el código (v{target}). "
                "Actualiza el código o usa otra base."
            )
        for version in sorted(_migrations):
            if version <= current or version > target:
                continue
            script = _migrations[version]
            if not script.is_file():
                raise FileNotFoundError(f"falta el script de migración: {script}")
            # No se envuelve en `transaction()` a propósito: executescript() ejecuta
            # un COMMIT implícito, así que un BEGIN/COMMIT explícito alrededor falla
            # con "cannot commit - no transaction is active". Y no hace falta
            # atomicidad: todo el DDL es `CREATE TABLE IF NOT EXISTS`, así que una
            # aplicación parcial se recupera simplemente volviendo a correr el script.
            conn.executescript(script.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (version,),
            )
            applied.append(version)
    finally:
        conn.close()
    return applied