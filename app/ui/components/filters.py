"""Filtros compartidos de la barra lateral.

Los widgets se declaran una sola vez, en `app.py`, y cada página recibe el
resultado. Si cada página construyera sus propios filtros, cambiar de pestaña
los reiniciaría y el dashboard obligaría a repetir el mismo clic para cada
gráfico — que es exactamente el motivo por el que nadie usa un dashboard.

Los rangos se calculan **a partir del último timestamp del dataset**, no del
reloj: ver `patterns.py`. Un dataset que termina hace tres días y un preset
"últimas 24 horas" relativo a hoy dan un dashboard vacío sin que nada esté roto.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import streamlit as st

from app.parser.normalization import to_utc_iso
from app.parser.severity import ERROR, FATAL, WARN
from app.ui import cache, data
from app.ui.theme import format_int

PRESETS: dict[str, int | None] = {
    "Todo el dataset": None,
    "Última hora": 1,
    "Últimas 24 horas": 24,
    "Últimos 7 días": 24 * 7,
}
CUSTOM = "Personalizado"
MIN_SEVERITIES: dict[str, int] = {
    "WARN (40)": WARN,
    "ERROR (45)": ERROR,
    "Sólo FATAL (50)": FATAL,
}
ANY = "(todos)"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_iso(moment: datetime) -> str:
    """Delega en `to_utc_iso` para no duplicar el formato de la base.

    `timestamp_utc` es TEXT: el orden lexicográfico solo coincide con el temporal
    si la longitud es fija (`.mmmZ`). Repetir aquí un formato sin milisegundos
    reintroduce el bug de los límites de rango.
    """
    return to_utc_iso(moment)


def _preset_range(hours: int, latest: str | None) -> tuple[str | None, str | None]:
    last = _parse_iso(latest)
    if last is None:
        return None, None
    # `until` es exclusivo: sumar 1 ms evita que el evento más reciente del
    # dataset (que es `latest`) quede fuera del preset por el `<`.
    return _to_iso(last - timedelta(hours=hours)), _to_iso(last + timedelta(milliseconds=1))


def sidebar_filters() -> data.EventFilters:
    options = cache.filter_options()
    latest = options["bounds"][1]

    with st.sidebar:
        st.header("Filtros")
        preset = st.selectbox("Rango temporal", [*PRESETS, CUSTOM], index=0)
        since: str | None = None
        until: str | None = None
        if preset in PRESETS and PRESETS[preset] is not None:
            since, until = _preset_range(int(PRESETS[preset]), latest)
        elif preset == CUSTOM:
            first = _parse_iso(options["bounds"][0]) or datetime(2026, 1, 1, tzinfo=UTC)
            last = _parse_iso(latest) or datetime.now(UTC)
            picked = st.date_input("Fechas (UTC)", value=(first.date(), last.date()))
            if isinstance(picked, tuple) and len(picked) == 2:
                start, end = picked
                # `until` es exclusivo: para incluir el día final hay que llegar
                # al día siguiente a las 00:00.
                since = _to_iso(datetime.combine(start, datetime.min.time(), tzinfo=UTC))
                until = _to_iso(datetime.combine(end, datetime.min.time(), tzinfo=UTC) + timedelta(days=1))

        level = st.selectbox("Severidad mínima", list(MIN_SEVERITIES), index=1)
        service = st.selectbox("Servicio", [ANY, *options["services"], data.BLANK_SERVICE])
        category = st.selectbox(
            "Categoría", [ANY, *options["categories"], data.BLANK_CATEGORY]
        )
        search = st.text_input("Buscar en el mensaje", placeholder="texto libre…")
        st.caption(
            f"{format_int(len(options['services']))} servicios · "
            f"{format_int(len(options['categories']))} categorías en el dataset"
        )

    return data.EventFilters(
        since=since,
        until=until,
        min_severity=MIN_SEVERITIES[level],
        service=None if service == ANY else service,
        category=None if category == ANY else category,
        search=search.strip() or None,
    )


def describe(filters: data.EventFilters) -> str:
    """Resumen legible de los filtros activos, para la cabecera de la página."""
    parts: list[str] = []
    if filters.since and filters.until:
        parts.append(f"{filters.since[:16]} → {filters.until[:16]} UTC")
    elif filters.since:
        parts.append(f"desde {filters.since[:16]} UTC")
    else:
        parts.append("todo el dataset")
    parts.append(f"severidad ≥ {filters.min_severity}")
    if filters.service:
        parts.append(f"servicio={filters.service}")
    if filters.category:
        parts.append(f"categoría={filters.category}")
    if filters.search:
        parts.append(f"busca «{filters.search}»")
    return " · ".join(parts)


__all__ = ["ANY", "CUSTOM", "MIN_SEVERITIES", "PRESETS", "describe", "sidebar_filters"]
