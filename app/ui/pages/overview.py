"""Overview: el estado del sistema en una pantalla.

El orden de los bloques sigue el recorrido mental de quien abre el dashboard:
primero volumen y calidad del dato (KPIs), luego **qué** está fallando (top
errores y categorías) y después **cuándo** (series y heatmap). La cobertura de
parseo va arriba porque si el parseo falla, todo lo de abajo cuenta de menos.
"""

from __future__ import annotations

import streamlit as st

from app.ui import cache
from app.ui.components import charts, filters, kpis, tables
from app.ui.data import EventFilters


def render(selected: EventFilters) -> None:
    st.title("Overview")
    st.caption(filters.describe(selected))

    stats = cache.overview_stats(selected.since, selected.until)
    if not stats["events"]:
        kpis.empty_state(
            "Sin eventos en el rango seleccionado",
            "El dataset existe pero ningún evento cae dentro de los filtros "
            "actuales. Prueba a ampliar el rango temporal en la barra lateral.",
        )
        return

    kpis.kpi_row(stats)
    kpis.coverage_warning(stats)
    st.divider()

    ranking = cache.ranking_errors(
        selected.since, selected.until, min_severity=selected.min_severity, limit=8
    )
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Errores más repetidos")
        if ranking:
            st.plotly_chart(
                charts.top_errors_bar(ranking, height=8 * 38 + 40),
                use_container_width=True,
            )
        else:
            st.info("No hay errores por encima de la severidad filtrada.")
    with right:
        st.subheader("Por categoría")
        by_cat = cache.categories(
            selected.since, selected.until, min_severity=selected.min_severity
        )
        if by_cat:
            st.plotly_chart(charts.category_bar(by_cat), use_container_width=True)
        else:
            st.info("Sin categorías: ninguna regla ha coincidido con estos eventos.")

    st.divider()
    hourly = cache.errors_per_hour(
        selected.since, selected.until, min_severity=selected.min_severity
    )
    st.subheader("Errores por hora")
    if hourly:
        st.plotly_chart(charts.errors_over_time(hourly), use_container_width=True)
        st.caption(
            "Hora en UTC. Sólo aparecen las horas con errores; para la serie completa "
            "con huecos, ver Incidents (es la que alimenta el z-score)."
        )
    else:
        st.info("No hay errores en el rango seleccionado.")

    st.divider()
    granularity = st.radio(
        "Granularidad del timeline", ["hour", "day"], horizontal=True, index=0
    )
    series = cache.severity_series(selected.since, selected.until, granularity)
    st.subheader("Timeline por severidad")
    if series:
        st.plotly_chart(charts.severity_area(series), use_container_width=True)
    else:
        st.info("Sin eventos con timestamp en el rango.")

    st.divider()
    st.subheader("Heatmap día × hora")
    cells = cache.heatmap(selected.since, selected.until, min_severity=selected.min_severity)
    if cells:
        st.plotly_chart(charts.heatmap_figure(cells), use_container_width=True)
        st.caption(
            "Día de la semana × hora del día, en UTC. Sólo errores ≥ "
            f"{selected.min_severity}. Un patrón diario repetido suele ser un job "
            "programado, no una incidencia."
        )
    else:
        st.info("Sin errores que dibujar en el heatmap.")

    st.divider()
    st.subheader("Detalle del ranking")
    if ranking:
        tables.ranking_table(ranking)
    else:
        st.info("Nada que listar: ningún error agrupado en el rango seleccionado.")
    st.caption(
        f"Los conteos usan la severidad mínima {selected.min_severity} de la barra "
        "lateral; la agrupación es por `fingerprint` (ver ADR-004)."
    )
