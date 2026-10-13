"""Consultas con filtros del dashboard, sin depender de Streamlit.

Este módulo es la frontera: aquí se decide **qué** SQL corre. No importa
`streamlit` a propósito, por dos razones concretas:

* el filtrado se puede probar con `unittest` normal, sin la dependencia del
  dashboard instalada;
* el plan de ejecución (`EXPLAIN QUERY PLAN`) es verificable en un test, y el
  criterio de aceptación de la fase es justamente ese: los filtros deben usar
  índices, no re-escanear la tabla.

Regla que se respeta aquí y en todo el proyecto: **ningún valor se interpola en
el SQL**. Los filtros son bind parameters; lo único que se compone en el texto es
la forma de la cláusula (qué columnas, no qué valores).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.parser.severity import ERROR

# Centinela para "eventos sin categoría". La UI necesita distinguir "no filtrar
# por categoría" de "sólo los que no tienen categoría", y `None` sólo puede
# significar una cosa. Se traduce en `where()` a `category IS NULL`.
BLANK_CATEGORY = "(sin categoría)"
BLANK_SERVICE = "(sin servicio)"

EVENT_COLUMNS = (
    "e.id",
    "e.timestamp_utc",
    "e.severity",
    "e.category",
    "e.service",
    "e.message",
    "e.fingerprint",
    "e.parsed_ok",
    "e.parse_error",
    "e.raw_line",
    "lf.path AS source_path",
)


def _like(term: str) -> str:
    """Convierte texto libre en patrón LIKE, escapando los comodines.

    Sin escapar, buscar `100%` devuelve todo: `%` es comodín de LIKE. El usuario
    que busca un porcentaje está buscando un porcentaje.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass(frozen=True)
class EventFilters:
    """Filtros del dashboard. Inmutable: se construye una vez por render."""

    since: str | None = None
    until: str | None = None
    min_severity: int = ERROR
    service: str | None = None
    category: str | None = None
    search: str | None = None
    # Atajo que usa la página de eventos: ver sólo lo que ningún patrón reconoció.
    unparsed_only: bool = False

    def where(self, alias: str = "e") -> tuple[str, list[object]]:
        """Cláusula WHERE y parámetros. Vacía si no hay ningún filtro."""
        clauses: list[str] = []
        params: list[object] = []

        # `unparsed_only` anula el filtro de severidad a propósito: una línea
        # que ningún patrón reconoció tiene severidad UNKNOWN (0), así que
        # "sin parsear y severity >= 45" es siempre vacío. Sin esta excepción el
        # interruptor del dashboard devolvería una tabla en blanco sin explicar
        # por qué, y el usuario creería que no hay líneas sin parsear.
        if self.min_severity and not self.unparsed_only:
            clauses.append(f"{alias}.severity >= ?")
            params.append(self.min_severity)
        if self.since is not None:
            clauses.append(f"{alias}.timestamp_utc >= ?")
            params.append(self.since)
        if self.until is not None:
            # Abierto a la derecha, igual que en `analysis.metrics`: "hasta las
            # 12:00" no incluye lo de las 12:00.
            clauses.append(f"{alias}.timestamp_utc < ?")
            params.append(self.until)
        if self.service == BLANK_SERVICE:
            clauses.append(f"{alias}.service IS NULL")
        elif self.service:
            clauses.append(f"{alias}.service = ?")
            params.append(self.service)
        if self.category == BLANK_CATEGORY:
            clauses.append(f"{alias}.category IS NULL")
        elif self.category:
            clauses.append(f"{alias}.category = ?")
            params.append(self.category)
        if self.unparsed_only:
            clauses.append(f"{alias}.parsed_ok = 0")
        if self.search:
            clauses.append(f"({alias}.message LIKE ? ESCAPE '\\' OR {alias}.raw_line LIKE ? ESCAPE '\\')")
            params.extend((_like(self.search), _like(self.search)))

        text = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return text, params


def count_events_filtered(conn: sqlite3.Connection, filters: EventFilters) -> int:
    where, params = filters.where()
    row = conn.execute(f"SELECT COUNT(*) FROM events e{where}", params).fetchone()
    return int(row[0]) if row else 0


def fetch_events(
    conn: sqlite3.Connection,
    filters: EventFilters,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Página de eventos. El `LIMIT` no es cosmético: la tabla no se materializa.

    El orden es por timestamp descendente: en un dashboard se mira primero lo
    último que pasó.
    """
    if limit <= 0:
        raise ValueError("limit debe ser > 0")
    where, params = filters.where()
    columns = ", ".join(EVENT_COLUMNS)
    return list(
        conn.execute(
            f"""
            SELECT {columns}
            FROM events e
            LEFT JOIN log_files lf ON lf.id = e.file_id{where}
            ORDER BY e.timestamp_utc DESC, e.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
    )


def distinct_services(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT service FROM events WHERE service IS NOT NULL ORDER BY service"
    ).fetchall()
    return [str(row[0]) for row in rows]


def distinct_categories(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT category FROM events WHERE category IS NOT NULL ORDER BY category"
    ).fetchall()
    return [str(row[0]) for row in rows]


def data_time_bounds(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """Primer y último timestamp del dataset, para acotar el selector de fechas.

    El selector se acota al dataset y no a "hoy": un dataset histórico con un
    calendario hasta la fecha actual deja al usuario eligiendo un rango vacío sin
    entender por qué no ve nada.
    """
    row = conn.execute("SELECT MIN(timestamp_utc), MAX(timestamp_utc) FROM events").fetchone()
    if not row:
        return None, None
    return (row[0], row[1])


def explain_plan(conn: sqlite3.Connection, filters: EventFilters) -> list[str]:
    """Plan de ejecución del listado de eventos. Se muestra en Settings.

    Es la prueba visible de la promesa "los filtros usan índices": quien lo dude
    lo abre desde la propia aplicación en vez de leer el código.
    """
    where, params = filters.where()
    rows = conn.execute(
        f"""
        EXPLAIN QUERY PLAN
        SELECT e.id FROM events e
        LEFT JOIN log_files lf ON lf.id = e.file_id{where}
        ORDER BY e.timestamp_utc DESC, e.id DESC
        LIMIT ?
        """,
        [*params, 100],
    ).fetchall()
    return [str(row[3]) for row in rows]


__all__ = [
    "BLANK_CATEGORY",
    "BLANK_SERVICE",
    "EVENT_COLUMNS",
    "EventFilters",
    "count_events_filtered",
    "data_time_bounds",
    "distinct_categories",
    "distinct_services",
    "explain_plan",
    "fetch_events",
]
