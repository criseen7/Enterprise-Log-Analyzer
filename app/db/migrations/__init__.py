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

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.db.connection import connect
from app.db.schema import SCHEMA_PATH, SCHEMA_VERSION

try:  # pragma: no cover - Windows no tiene fcntl; allí el lock es un no-op
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

MIGRATIONS_DIR = Path(__file__).resolve().parent
_BASELINE_VERSION = 1

# Delta por versión. Solo las que existen: una versión sin script es un fallo
# de empaquetado, no algo que haya que saltarse.
_deltas: dict[int, Path] = {
    2: MIGRATIONS_DIR / "_002_log_files_identity.sql",
    3: MIGRATIONS_DIR / "_003_event_category.sql",
    4: MIGRATIONS_DIR / "_004_timestamp_millis.sql",
    5: MIGRATIONS_DIR / "_005_agent_actions.sql",
    6: MIGRATIONS_DIR / "_006_agent_runs.sql",
    7: MIGRATIONS_DIR / "_007_log_files_head_hash.sql",
    8: MIGRATIONS_DIR / "_008_incident_notified_through.sql",
    9: MIGRATIONS_DIR / "_009_hitl_audit.sql",
}

# Versiones que entrega la baseline. Debe ser exactamente los deltas cargados:
# si un delta se añade y esta lista no se actualiza, una base nueva nunca lo
# vería, y dos bases en "vN" tendrían esquemas distintos.
_baseline_covers = set(_deltas) | {_BASELINE_VERSION}


def _delta_003_already_applied(conn) -> bool:
    """True si `_003` ya surtió efecto sobre `events.category`.

    `ALTER TABLE ... ADD COLUMN` no acepta `IF NOT EXISTS`. Si el ALTER se aplicó
    pero la versión no llegó a registrarse (un fallo entre DDL y `_record`), un
    reintento abortaría con `duplicate column name` y la base quedaría atascada
    en v2. Cuando la columna ya existe, se reafirma el índice (idempotente) y se
    salta el ALTER.
    """
    if not _column_exists(conn, "events", "category"):
        return False
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_cat_ts ON events (category, timestamp_utc)")
    return True


def _delta_007_already_applied(conn) -> bool:
    """True si `_007` ya añadió `log_files.head_hash`.

    `ALTER TABLE ... ADD COLUMN` falla con `duplicate column name` si el DDL se
    aplicó pero la versión no llegó a registrarse. No hay nada más que reafirmar.
    """
    return _column_exists(conn, "log_files", "head_hash")


def _delta_008_already_applied(conn) -> bool:
    """True si `_008` ya añadió `incidents.notified_through`.

    Si el DDL se aplicó pero la versión no llegó a registrarse, se reafirma el
    relleno de las filas ya notificadas. Es idempotente: sólo toca las que
    siguen en NULL.
    """
    if not _column_exists(conn, "incidents", "notified_through"):
        return False
    conn.execute(
        "UPDATE incidents SET notified_through = last_seen "
        "WHERE notified_at IS NOT NULL AND notified_through IS NULL"
    )
    return True


# DDL idempotente de `_009`, para reafirmar una migración que se quedó a medias
# (columna añadida pero versión no registrada). Debe reflejar el SQL del delta.
_DELTA_009_SETTLE_DDL = """
CREATE TABLE IF NOT EXISTS hitl_audit (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id   TEXT NOT NULL,
    event       TEXT NOT NULL,
    actor       TEXT,
    occurred_at TEXT NOT NULL,
    detail      TEXT,
    FOREIGN KEY (action_id) REFERENCES agent_actions(id)
);
CREATE INDEX IF NOT EXISTS idx_hitl_audit_action ON hitl_audit (action_id, seq);
CREATE TRIGGER IF NOT EXISTS trg_hitl_audit_no_update
BEFORE UPDATE ON hitl_audit
BEGIN
    SELECT RAISE(ABORT, 'hitl_audit es append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_hitl_audit_no_delete
BEFORE DELETE ON hitl_audit
BEGIN
    SELECT RAISE(ABORT, 'hitl_audit es append-only');
END;
"""

_DELTA_009_BACKFILL = """
UPDATE agent_actions
   SET rejected_at = approved_at, rejected_by = approved_by
 WHERE status = 'REJECTED' AND approved_at IS NOT NULL AND rejected_at IS NULL;

INSERT INTO hitl_audit (action_id, event, actor, occurred_at, detail)
SELECT id, 'CREATED', NULL, created_at, tool FROM agent_actions
 WHERE NOT EXISTS (
    SELECT 1 FROM hitl_audit h WHERE h.action_id = agent_actions.id AND h.event = 'CREATED'
 );

INSERT INTO hitl_audit (action_id, event, actor, occurred_at, detail)
SELECT id, 'APPROVED', approved_by, approved_at, tool FROM agent_actions
 WHERE status IN ('APPROVED', 'EXECUTED') AND approved_at IS NOT NULL
   AND NOT EXISTS (
    SELECT 1 FROM hitl_audit h WHERE h.action_id = agent_actions.id AND h.event = 'APPROVED'
 );

INSERT INTO hitl_audit (action_id, event, actor, occurred_at, detail)
SELECT id, 'REJECTED', rejected_by, rejected_at, tool FROM agent_actions
 WHERE status = 'REJECTED' AND rejected_at IS NOT NULL
   AND NOT EXISTS (
    SELECT 1 FROM hitl_audit h WHERE h.action_id = agent_actions.id AND h.event = 'REJECTED'
 );

INSERT INTO hitl_audit (action_id, event, actor, occurred_at, detail)
SELECT id, 'EXECUTED', NULL, executed_at, tool FROM agent_actions
 WHERE executed_at IS NOT NULL
   AND NOT EXISTS (
    SELECT 1 FROM hitl_audit h WHERE h.action_id = agent_actions.id AND h.event = 'EXECUTED'
 );
"""


def _delta_009_already_applied(conn) -> bool:
    """Reafirma `_009` si dejó algún efecto, y devuelve si debe saltarse.

    Basta con que exista una de las dos columnas o la tabla para concluir que el
    script corrió: comprobar sólo `rejected_by` dejaba atascada una base cuyo
    primer `ALTER` (`rejected_at`) se aplicó y el segundo no. Las piezas que
    falten se crean aquí; el backfill es idempotente y se reafirma siempre.
    """
    has_at = _column_exists(conn, "agent_actions", "rejected_at")
    has_by = _column_exists(conn, "agent_actions", "rejected_by")
    has_table = _table_exists(conn, "hitl_audit")
    if not (has_at or has_by or has_table):
        return False
    if not has_at:
        conn.execute("ALTER TABLE agent_actions ADD COLUMN rejected_at TEXT")
    if not has_by:
        conn.execute("ALTER TABLE agent_actions ADD COLUMN rejected_by TEXT")
    conn.executescript(_DELTA_009_SETTLE_DDL + _DELTA_009_BACKFILL)
    return True


# Deltas que no son re-ejecutables en SQL puro. El guard detecta si el efecto ya
# está presente y, de ser así, sólo reafirma su parte idempotente.
_DELTA_GUARDS: dict[int, Callable[[sqlite3.Connection], bool]] = {
    3: _delta_003_already_applied,
    7: _delta_007_already_applied,
    8: _delta_008_already_applied,
    9: _delta_009_already_applied,
}


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pragma_table_info(?) WHERE name = ?", (table, column)
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
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )


def _apply_script(conn, script: Path, version: int) -> None:
    if not script.is_file():
        raise FileNotFoundError(f"falta el script de migración: {script}")
    # No se envuelve en `transaction()` a propósito: executescript() ejecuta un
    # COMMIT implícito, así que un BEGIN/COMMIT explícito alrededor falla con
    # "cannot commit - no transaction is active". La atomicidad, si hace falta,
    # la declara el propio script (`_002` abre su BEGIN/COMMIT). Un delta que no
    # sea re-ejecutable en SQL puro se protege con `_DELTA_GUARDS`.
    conn.executescript(script.read_text(encoding="utf-8"))
    _record(conn, version)


@contextmanager
def _migration_lock(db_path: str | Path) -> Iterator[None]:
    """Serializa `migrate()` entre procesos.

    El baseline y los deltas asumen un único aplicador: dos contenedores que
    arrancan a la vez (analyzer + dashboard) pueden intentar crear el esquema
    con `executescript()` y competir por el mismo `schema_version`. SQLite
    serializa escrituras, pero no la secuencia "leer versión → aplicar script →
    registrar": el segundo proceso lee v0 y vuelve a aplicar deltas ya
    aplicados. Un lock de archivo alrededor de toda la migración lo evita.

    En `:memory:` no hay archivo que compartir y en plataformas sin `fcntl`
    (Windows) se degrada a no-op; el caso real del despliegue es Linux.
    """
    path = Path(db_path)
    if fcntl is None or str(path) == ":memory:":
        yield
        return

    lock_path = path.with_name(path.name + ".migrate.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def migrate(db_path: str | Path, *, target: int | None = None) -> list[int]:
    """Aplica las migraciones faltantes. Idempotente y serializada entre procesos."""
    with _migration_lock(db_path):
        return _migrate_locked(db_path, target=target)


def _migrate_locked(db_path: str | Path, *, target: int | None = None) -> list[int]:
    """Cuerpo de `migrate()`; el llamador ya tiene el lock."""
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
            guard = _DELTA_GUARDS.get(version)
            if guard is not None and guard(conn):
                _record(conn, version)
                applied.append(version)
                continue
            _apply_script(conn, _deltas[version], version)
            applied.append(version)
    finally:
        conn.close()
    return applied
