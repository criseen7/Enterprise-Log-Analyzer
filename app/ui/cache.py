"""Capa de acceso del dashboard: una conexión de corta vida por consulta + caché.

Dos decisiones que conviene no deshacer:

* **`st.cache_data` para datos, nunca `st.cache_resource` sobre una `Connection`.**
  La caché de datos serializa su resultado; una conexión SQLite no es
  serializable, y cachearla en modo datos falla en el segundo render. Ver
  `connection.py`: para consultas del dashboard, la conexión de corta vida es lo
  correcto — abrir una conexión WAL es barato y evita el estado compartido entre
  rerenders e hilos.
* **Las funciones devuelven diccionarios, no `sqlite3.Row`.** La caché de
  Streamlit *picklea* el valor devuelto y `sqlite3.Row` no es pickleable: devolverlo
  tal cual convierte una caché en un error de serialización en producción, no en
  desarrollo.

El TTL sale de `ui.cache_ttl_seconds` (30 s por defecto). No es un adorno: sin
él, cada clic en un widget relanza las agregaciones, y con él los números pueden
quedar desfasados hasta 30 s — que para este caso de uso es lo correcto.
"""

from __future__ import annotations

import streamlit as st

from app.analysis.anomalies import anomalies, hourly_counts
from app.analysis.metrics import (
    by_category,
    by_service,
    errors_by_hour,
    heatmap_day_hour,
    overview,
    timeline_by_severity,
    top_errors,
)
from app.analysis.patterns import recurring_patterns
from app.alerts.incidents import incident_counts as _incident_counts
from app.alerts.incidents import list_incidents
from app.config import get_settings
from app.db.connection import connection
from app.db.repository import recent_runs
from app.ui import data

_TTL = get_settings().ui.cache_ttl_seconds


def _dicts(rows) -> list[dict]:
    """`sqlite3.Row` -> dict. La caché picklea lo que se devuelve: Row no lo es."""
    return [dict(row) for row in rows]


@st.cache_data(ttl=_TTL, show_spinner=False)
def overview_stats(since: str | None = None, until: str | None = None) -> dict:
    with connection(read_only=True) as conn:
        stats = overview(conn, since=since, until=until)
    return {**stats.__dict__, "coverage": stats.coverage}


@st.cache_data(ttl=_TTL, show_spinner=False)
def errors_per_hour(
    since: str | None = None, until: str | None = None, min_severity: int = 45
) -> dict[str, int]:
    with connection(read_only=True) as conn:
        return errors_by_hour(conn, min_severity=min_severity, since=since, until=until)


@st.cache_data(ttl=_TTL, show_spinner=False)
def ranking_errors(
    since: str | None = None,
    until: str | None = None,
    min_severity: int = 45,
    limit: int = 10,
    min_count: int = 1,
) -> list[dict]:
    with connection(read_only=True) as conn:
        rows = top_errors(
            conn,
            limit=limit,
            min_severity=min_severity,
            min_count=min_count,
            since=since,
            until=until,
        )
    return [row.__dict__ for row in rows]


@st.cache_data(ttl=_TTL, show_spinner=False)
def severity_series(
    since: str | None = None, until: str | None = None, granularity: str = "hour"
) -> list[dict]:
    with connection(read_only=True) as conn:
        return [row.__dict__ for row in timeline_by_severity(conn, granularity=granularity, since=since, until=until)]


@st.cache_data(ttl=_TTL, show_spinner=False)
def heatmap(
    since: str | None = None, until: str | None = None, min_severity: int = 45
) -> list[dict]:
    with connection(read_only=True) as conn:
        return [row.__dict__ for row in heatmap_day_hour(conn, min_severity=min_severity, since=since, until=until)]


@st.cache_data(ttl=_TTL, show_spinner=False)
def categories(
    since: str | None = None, until: str | None = None, min_severity: int = 45
) -> list[dict]:
    with connection(read_only=True) as conn:
        return [row.__dict__ for row in by_category(conn, min_severity=min_severity, since=since, until=until)]


@st.cache_data(ttl=_TTL, show_spinner=False)
def services(
    since: str | None = None, until: str | None = None, min_severity: int = 45, limit: int = 10
) -> list[dict]:
    with connection(read_only=True) as conn:
        return [row.__dict__ for row in by_service(conn, limit=limit, min_severity=min_severity, since=since, until=until)]


@st.cache_data(ttl=_TTL, show_spinner=False)
def hourly_series(
    since: str | None = None, until: str | None = None, min_severity: int = 45
) -> list[dict]:
    """Serie horaria **rellenada**: es la que necesita el z-score."""
    with connection(read_only=True) as conn:
        return [row.__dict__ for row in hourly_counts(conn, min_severity=min_severity, since=since, until=until)]


@st.cache_data(ttl=_TTL, show_spinner=False)
def spikes(
    since: str | None = None,
    until: str | None = None,
    min_severity: int = 45,
    z_threshold: float = 3.0,
    min_samples: int = 5,
) -> list[dict]:
    with connection(read_only=True) as conn:
        found = anomalies(
            conn,
            z_threshold=z_threshold,
            min_samples=min_samples,
            min_severity=min_severity,
            since=since,
            until=until,
        )
    return [{**item.__dict__, "is_infinite": item.is_infinite} for item in found]


@st.cache_data(ttl=_TTL, show_spinner=False)
def recurrence(
    window_minutes: int = 15,
    min_count: int = 2,
    min_severity: int = 45,
    limit: int | None = None,
) -> list[dict]:
    """Recurrencia en la ventana. `now` no se pasa: la ventana acaba en el último
    evento del dataset, que es lo que hace que un dataset histórico muestre algo.

    `limit=None` trae todas las huellas: los contadores de la vista no pueden
    calcularse sobre una lista recortada."""
    with connection(read_only=True) as conn:
        return [row.__dict__ for row in recurring_patterns(conn, window_minutes=window_minutes, min_count=min_count, min_severity=min_severity, limit=limit)]


@st.cache_data(ttl=_TTL, show_spinner=False)
def incidents(status: str | None = None, limit: int = 200) -> list[dict]:
    """Incidentes ya persistidos. `status=None` los trae todos."""
    with connection(read_only=True) as conn:
        return _dicts(list_incidents(conn, status=status, limit=limit))


@st.cache_data(ttl=_TTL, show_spinner=False)
def incident_counts() -> dict[str, int]:
    with connection(read_only=True) as conn:
        return _incident_counts(conn)


@st.cache_data(ttl=_TTL, show_spinner=False)
def ingest_runs(limit: int = 10) -> list[dict]:
    with connection(read_only=True) as conn:
        return _dicts(recent_runs(conn, limit=limit))


@st.cache_data(ttl=_TTL, show_spinner=False)
def watched_files() -> list[dict]:
    with connection(read_only=True) as conn:
        return _dicts(
            conn.execute(
                """
                SELECT path, inode, generation, size, last_offset, last_seen, is_active,
                       (SELECT COUNT(*) FROM events e WHERE e.file_id = lf.id) AS events
                FROM log_files lf
                ORDER BY is_active DESC, last_seen DESC
                """
            )
        )


@st.cache_data(ttl=_TTL, show_spinner=False)
def filter_options() -> dict[str, list[str]]:
    with connection(read_only=True) as conn:
        return {
            "services": data.distinct_services(conn),
            "categories": data.distinct_categories(conn),
            "bounds": list(data.data_time_bounds(conn)),
        }


def clear_caches() -> None:
    """Invalida la caché de consultas (botón de Settings / tras ingerir)."""
    st.cache_data.clear()


__all__ = [
    "categories",
    "clear_caches",
    "errors_per_hour",
    "filter_options",
    "heatmap",
    "hourly_series",
    "incident_counts",
    "incidents",
    "ingest_runs",
    "overview_stats",
    "ranking_errors",
    "recurrence",
    "services",
    "severity_series",
    "spikes",
    "watched_files",
]
