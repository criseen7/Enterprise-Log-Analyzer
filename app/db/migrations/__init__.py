"""Migraciones: `python -m app.db.migrations [--status]`

Dos mecanismos, porque no significan lo mismo:

* **Baseline** (`schema.sql`) — crea una base nueva desde cero, en el esquema
  actual. Solo se usa cuando no existe `schema_version`.
* **Deltas** (`_NNN_*.sql`) — llevan una base existente hasta el esquema actual.
  Un delta escribe solo lo que cambió respecto al anterior.

Mezclarlos da lugar a trampas difíciles de ver: si la baseline es `CREATE TABLE
IF NOT EXISTS`, una base nueva registrada como v1 y una base antigua llegan al
mismo `schema_version` con esquemas distintos — la primera con `generation` y la
segunda sin ella. Los tests pasan porque arrancan desde la baseline, y el fallo
solo aparece en la máquina donde había datos viejos. Por eso la baseline se aplica
una sola vez, en v0, y desde ahí solo los deltas.
"""

from __future__ import annotations

from pathlib import Path

from app.db.connection import connect, journal_mode
from app.db.schema import SCHEMA_PATH, SCHEMA_VERSION

MIGRATIONS_DIR = Path(__file__).resolve().parent
_BASELINE_VERSION = 1

# Delta por versión. Solo las que existen: una versión sin script es un fallo
# de empaquetado, no algo que haya que saltarse.
_deltas: dict[int, Path] = {
    2: MIGRATIONS_DIR / "_002_log_files_identity.sql",
    3: MIGRATIONS_DIR / "_003_event_category.sql",
}

# Versiones que entrega la baseline. Debe ser exactamente los deltas cargados:
# si un delta se añade y esta lista no se actualiza, una base nueva nunca lo
# vería, y dos bases en "vN" tendrían esquemas distintos.
_baseline_covers = set(_deltas) | {_BASELINE_VERSION}


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


def _record(conn, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) "
        "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (version,),
    )


def _ensure_meta_tables(conn) -> None:
    """Crea `schema_version` sin eso, `_current_version` no puede decidir nada."""
    if not _table_exists(conn, "schema_version"):
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")


def _apply_script(conn, script: Path, version: int) -> None:
    if not script.is_file():
        raise FileNotFoundError(f"falta el script de migración: {script}")
    # No se envuelve en `transaction()` a propósito: executescript() ejecuta un
    # COMMIT implícito, así que un BEGIN/COMMIT explícito alrededor falla con
    # "cannot commit - no transaction is active". Tampoco hace falta atomicidad:
    # todo el DDL es `IF NOT EXISTS` o un rebuild que deja la tabla nueva antes
    # de borrar la vieja, así que una aplicación parcial se recupera volviendo a
    # correr el script.
    conn.executescript(script.read_text(encoding="utf-8"))
    _record(conn, version)


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

        if current == 0 and target >= _BASELINE_VERSION:
            # Base nueva: schema.sql es el esquema completo, y se registra toda
            # la cobertura de golpe. Aquí no se puede apilar baseline + deltas,
            # porque los deltas asumen tablas ya creadas con `IF NOT EXISTS`.
            _ensure_meta_tables(conn)
            _apply_script(conn, SCHEMA_PATH, _BASELINE_VERSION)
            for version in sorted(_baseline_covers):
                if version > target:
                    continue
                if version != _BASELINE_VERSION:
                    _record(conn, version)
                applied.append(version)
            current = _current_version(conn)

        for version in sorted(_deltas):
            if version <= current or version > target:
                continue
            _apply_script(conn, _deltas[version], version)
            applied.append(version)
    finally:
        conn.close()
    return applied