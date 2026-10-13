"""Incidents: lo que está pasando *ahora*, no lo que pasó alguna vez.

La diferencia entre esta página y Overview es el tiempo. Overview resume el
dataset entero; aquí la pregunta es "¿qué se está repitiendo en la última
ventana?" y "¿hay alguna hora que se salga de lo normal?".

Los umbrales salen de `config.yaml` (`analysis.window_minutes`,
`analysis.incident_threshold`, `analysis.anomaly_zscore`), no de constantes en
el código: en un despliegue real se ajustan sin tocar la aplicación.

> La gestión de incidentes (estado OPEN/ACK/RESOLVED, cooldown y export) es la
> Fase 5. Lo que hay aquí es la evidencia que la alimenta, no la entidad
> `incidents`: mostrarla antes de que exista sería prometer algo que la base
> todavía no garantiza.
"""

from __future__ import annotations

import streamlit as st

from app.config import get_settings
from app.ui import cache
from app.ui.components import charts, filters, kpis, tables
from app.ui.data import EventFilters
from app.ui.theme import format_int


def render(selected: EventFilters) -> None:
    settings = get_settings()
    st.title("Incidents")
    st.caption(filters.describe(selected))

    analysis = settings.analysis
    left, right = st.columns([2, 1])
    with left:
        window = st.slider(
            "Ventana de recurrencia (minutos)",
            min_value=1,
            max_value=24 * 60,
            value=int(analysis.window_minutes),
            step=5,
            help="La ventana termina en el último evento del dataset, no en el "
                 "reloj: un dataset histórico mostraría cero recurrencias si se "
                 "comparara contra 'ahora'.",
        )
    with right:
        min_count = st.number_input(
            "Ocurrencias mínimas",
            min_value=2,
            max_value=1000,
            value=max(2, min(analysis.incident_threshold // 10 or 2, 100)),
            step=1,
        )

    patterns = cache.recurrence(window_minutes=window, min_count=min_count,
                                min_severity=selected.min_severity)
    total_in_window = sum(row["count"] for row in patterns)

    columns = st.columns(3)
    columns[0].metric("Fingerprints activos", format_int(len(patterns)))
    columns[1].metric("Eventos en la ventana", format_int(total_in_window))
    columns[2].metric(
        "Umbral de incidente",
        format_int(analysis.incident_threshold),
        help="Eventos del mismo fingerprint en la ventana a partir de los cuales "
             "la Fase 5 abrirá un incidente.",
    )

    st.divider()
    st.subheader(f"Recurrencia en los últimos {window} minutos")
    if patterns:
        if any(row["count"] >= analysis.incident_threshold for row in patterns):
            st.error(
                "Hay fingerprints por encima del umbral de incidente "
                f"({analysis.incident_threshold}). Candidatos a incidente abierto.",
                icon="🚨",
            )
        else:
            st.success(
                "Ningún fingerprint supera todavía el umbral de incidente.", icon="✅"
            )
        tables.ranking_table(patterns, kind="categoría")
        st.caption(
            "La columna `n` es el número de ocurrencias del mismo fingerprint "
            "(categoría + mensaje normalizado) dentro de la ventana."
        )
    else:
        st.info(
            f"Nada se repite {min_count} veces o más en los últimos {window} minutos. "
            "Un dataset histórico suele dar cero aquí: es correcto, no un fallo."
        )

    st.divider()
    st.subheader("Horas anómalas (z-score)")
    series = cache.hourly_series(selected.since, selected.until, min_severity=selected.min_severity)
    spikes = cache.spikes(
        selected.since,
        selected.until,
        min_severity=selected.min_severity,
        z_threshold=analysis.anomaly_zscore,
        min_samples=analysis.anomaly_min_samples,
    )
    if len(series) < 3:
        kpis.empty_state(
            "Serie demasiado corta para detectar anomalías",
            "El z-score necesita al menos 3 horas y "
            f"{analysis.anomaly_min_samples} eventos por hora candidata. "
            "Un pico de pocos eventos frente a cero es ruido, no una incidencia.",
        )
    elif spikes:
        st.plotly_chart(charts.hourly_with_spikes(series, spikes), use_container_width=True)
        tables.spikes_table(spikes)
        st.caption(
            f"Media y desviación se calculan sin la hora evaluada (leave-one-out): "
            f"con la serie completa, el pico se disimula a sí mismo. Umbral "
            f"z ≥ {analysis.anomaly_zscore}, mínimo {analysis.anomaly_min_samples} "
            "eventos por hora."
        )
    else:
        st.plotly_chart(charts.hourly_with_spikes(series, []), use_container_width=True)
        st.info(
            f"Ninguna hora supera z ≥ {analysis.anomaly_zscore} con al menos "
            f"{analysis.anomaly_min_samples} eventos."
        )
