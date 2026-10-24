"""Apertura y configuración de conexiones SQLite.

Decisión central (ADR-001): la base usa **WAL** y la aplicación tiene
**un solo escritor** (el ingestor). Eso cambia por completo el modelo de
concurrencia:

    rollback journal (default)  ->  UN escritor. Un lector bloquea al escritor.
    WAL                          ->  UN escritor + N lectores simultáneos.

Sin WAL, el watcher escribiendo y el dashboard leyendo se bloquean entre sí con
`database is locked`. WAL no lo elimina: elimina la contención entre lectores y
escritor, y acota el escritor a uno solo. Por eso el diseño de la Fase 6 separa
`analyzer` y `dashboard` en procesos distintos.

Detalles que importan y no son evidentes:

* `journal_mode` es persistente (vive en la cabecera del archivo), pero
  `busy_timeout` y `synchronous` son por conexión: hay que fijarlos en cada una.
* `journal_mode=WAL` falla si se ejecuta dentro de una transacción abierta.
* Para "solo lectura" se usa `PRAGMA query_only=ON` y **no** `mode=ro` de URI:
  una conexión `mode=ro` sobre una DB en WAL necesita escribir en el archivo
  `-shm`, así que falla incluso sin intentar escribir.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.config import Settings, get_settings

_VALID_SYNCHRONOUS = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})


class DatabaseError(Exception):
    """Fallo de configuración o apertura de la base de datos."""


def _quote(value: str) -> str:
    """Valida un valor destinado a un PRAGMA.

    SQLite no permite bind parameters en PRAGMA. En vez de interpolar a ciegas
    se restringe el alfabeto: solo tiene sentido para un puñado de keywords.
    """
    normalized = value.upper()
    if normalized not in _VALID_SYNCHRONOUS:
        raise DatabaseError(
            f"synchronous inválido: {value!r}, esperado uno de {sorted(_VALID_SYNCHRONOUS)}"
        )
    return normalized


def _configure(conn: sqlite3.Connection, *, synchronous: str, busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise DatabaseError("busy_timeout_ms debe ser > 0")

    # Debe ir fuera de cualquier transacción: SQLite lo rechaza dentro de una.
    conn.execute("PRAGMA journal_mode = WAL")

    # Estos sí son por conexión.
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute(f"PRAGMA synchronous = {_quote(synchronous)}")
    conn.execute("PRAGMA foreign_keys = ON")

    actual = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(actual).lower() != "wal":
        raise DatabaseError(
            f"no se pudo activar WAL (journal_mode={actual!r}). "
            "O bien el filesystem no lo soporta (networked FS, ej. NFS/SMB), "
            "o bien la ruta no es un archivo real."
        )


def connect(
    db_path: str | Path,
    *,
    busy_timeout_ms: int = 5000,
    synchronous: str = "NORMAL",
) -> sqlite3.Connection:
    """Abre una conexión configurada. El caller es dueño del cierre."""
    path = Path(db_path).expanduser()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(path),
        # Streamlit ejecuta el script en un hilo distinto al que abrió la conexión.
        # Sin esto, cualquier acceso posterior lanza ProgrammingError.
        check_same_thread=False,
        # Detecta truncamientos de logs en cuanto ocurren; sin esto, el rollback
        # journal podría corromper la DB tras un corte de energía.
        isolation_level=None,
        timeout=busy_timeout_ms / 1000,
    )
    conn.row_factory = sqlite3.Row
    _configure(conn, synchronous=synchronous, busy_timeout_ms=busy_timeout_ms)
    return conn


@contextmanager
def connection(
    settings: Settings | None = None,
    *,
    read_only: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Conexión de corta vida. Para consultas del dashboard, preferible a cachear.

    `read_only` usa `query_only` en vez de `mode=ro` (ver nota del módulo).
    """
    settings = settings or get_settings()
    conn = connect(
        settings.paths.db_path,
        busy_timeout_ms=settings.db.busy_timeout_ms,
        synchronous=settings.db.synchronous,
    )
    try:
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transacción explícita.

    `isolation_level=None` deja a SQLite en autocommit, así que el BEGIN/COMMIT
    hay que emitirlos a mano. Es lo que permite hacer `executemany` de miles de
    eventos en una sola transacción en vez de uno por uno.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def journal_mode(db_path: str | Path) -> str:
    """Modo de journal efectivo. Útil para `make check-db` y diagnóstico."""
    conn = sqlite3.connect(str(db_path))
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def db_size_bytes(db_path: str | Path) -> int:
    """Tamaño en disco incluyendo `-wal` y `-shm`, que pueden pesar bastante."""
    path = Path(db_path)
    return sum(
        p.stat().st_size
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))
        if p.exists()
    )
