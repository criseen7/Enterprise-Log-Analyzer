"""Incidents: lo que está pasando *ahora*, no lo que pasó alguna vez.

La diferencia entre esta página y Overview es el tiempo. Overview resume el
dataset entero; aquí la pregunta es "¿qué se está repitiendo en la última
ventana?", "¿qué incidentes siguen abiertos?" y "¿hay alguna hora que se salga
de lo normal?".

Los umbrales salen de `config.yaml` (`analysis.window_minutes`,
`analysis.incident_threshold`, `analysis.anomaly_zscore`), no de constantes en
el código: en un despliegue real se ajustan sin tocar la aplicación.

Esta página es la única del dashboard que **escribe** además de leer: abrir el
incidente (evaluar) y mover su estado (OPEN/ACK/RESOLVED) son acciones del
usuario. Esas acciones sólo están habilitadas con `ui.admin_mode`, exigen una
palabra de confirmación y quedan registradas en la auditoría. Las lecturas
siguen con `read_only=True`; las escrituras van por `connection()` y limpian la
caché, igual que en Settings.
"""

from __future__ import annotations

import streamlit as st

from app.alerts import evaluate_incidents
from app.alerts.incidents import set_incident_status, status_label
from app.config import get_settings
from app.db.connection import connection
from app.reports import export_incidents_csv, export_incidents_html
from app.ui import audit, cache
from app.ui.components import charts, confirm, filters, kpis, tables
from app.ui.data import EventFilters
from app.ui.theme import format_int, shorten

# Cuántas filas de recurrencia se muestran. El conteo y el aviso de umbral usan
# la lista completa: recortar aquí es solo presentación.
DISPLAY_LIMIT = 50


def _banner(counts: dict[str, int]) -> None:
    """Banner de estado. Es lo primero que se ve y lo que decide si hay que actuar."""
    open_count = counts.get("OPEN", 0)
    ack = counts.get("ACKNOWLEDGED", 0)
    resolved = counts.get("RESOLVED", 0)

    if open_count:
        st.error(
            f"{open_count} incidente{'s' if open_count != 1 else ''} sin reconocer. "
            "Reconócelos abajo para silenciar el aviso, o resuélvelos si ya están atendidos.",
            icon="🚨",
        )
    elif ack:
        st.warning(
            f"{ack} incidente{'s' if ack != 1 else ''} reconocido{'s' if ack != 1 else ''} "
            "en curso. No hay incidentes nuevos sin reconocer.",
            icon="⚠️",
        )
    else:
        st.success("Sin incidentes abiertos.", icon="✅")

    columns = st.columns(3)
    columns[0].metric("OPEN", format_int(open_count))
    columns[1].metric("ACK", format_int(ack))
    columns[2].metric("RESOLVED", format_int(resolved))


def _evaluate_now(window_minutes: int, min_count: int, min_severity: int | None) -> None:
    """Evalúa la ventana visible y hace upsert de incidentes. Es una escritura."""
    with connection() as conn:
        result = evaluate_incidents(
            conn,
            window_minutes=window_minutes,
            min_count=min_count,
            min_severity=min_severity,
        )
    audit.record(
        "incident.evaluate",
        evaluated=bool(result["evaluated"]),
        upserted=result["upserted"],
        window_minutes=result["window_minutes"],
    )
    cache.clear_caches()
    if result["evaluated"]:
        st.success(
            f"{format_int(result['upserted'])} incidente(s) actualizado(s) "
            f"en la ventana de {result['window_minutes']} min.",
        )
    else:
        st.info(
            f"Ningún fingerprint alcanzó el umbral de "
            f"{result['incident_threshold']} ocurrencias en "
            f"{result['window_minutes']} min. No se abrió ningún incidente."
        )
    st.rerun()


def _manage(incidents: list[dict]) -> None:
    """Cambia el estado de un incidente concreto."""
    if not incidents:
        return
    labels = {
        row["id"]: f"#{row['id']} · {status_label(row['status'])} · "
        f"{format_int(row['event_count'])} eventos · {shorten(row['sample_message'], 70)}"
        for row in incidents
    }
    chosen = st.selectbox(
        "Incidente",
        options=list(labels),
        format_func=lambda i: labels[i],
    )
    row = next(r for r in incidents if r["id"] == chosen)
    current = row["status"]
    manage_ok = confirm.typed_confirmation(st, word="CONFIRMAR", key="confirm_incident_status")

    left, middle, right = st.columns(3)
    # `current` viene del snapshot con TTL de la caché: otra pestaña, la tool
    # MCP o el detector pueden haber movido el estado mientras tanto. El CAS
    # con expected_status evita que este clic pise ese cambio; el audit sólo
    # se escribe si la transición se aplicó de verdad.
    if left.button(
        "Reconocer (ACK)",
        width="stretch",
        disabled=not manage_ok or current == "ACKNOWLEDGED",
        help="Marca el incidente como visto sin cerrarlo.",
    ):
        with connection() as conn:
            changed = set_incident_status(conn, chosen, "ACKNOWLEDGED", expected_status=current)
        if changed:
            audit.record("incident.status", incident_id=chosen, status="ACKNOWLEDGED")
        cache.clear_caches()
        st.rerun()
    if middle.button(
        "Resolver",
        width="stretch",
        disabled=not manage_ok or current == "RESOLVED",
        help="Cierra el incidente. Un rebrote posterior lo reabrirá como nuevo.",
    ):
        with connection() as conn:
            changed = set_incident_status(conn, chosen, "RESOLVED", expected_status=current)
        if changed:
            audit.record("incident.status", incident_id=chosen, status="RESOLVED")
        cache.clear_caches()
        st.rerun()
    if right.button(
        "Reabrir (OPEN)",
        width="stretch",
        disabled=not manage_ok or current == "OPEN",
        help="Reabre y reinicia el cooldown: la próxima ocurrencia vuelve a notificar.",
    ):
        with connection() as conn:
            changed = set_incident_status(conn, chosen, "OPEN", expected_status=current)
        if changed:
            audit.record("incident.status", incident_id=chosen, status="OPEN")
        cache.clear_caches()
        st.rerun()


def _exports() -> None:
    with connection(read_only=True) as conn:
        csv_bytes = export_incidents_csv(conn)
        html_text = export_incidents_html(conn)
    left, right = st.columns(2)
    left.download_button(
        "Exportar CSV",
        data=csv_bytes,
        file_name="incidents.csv",
        mime="text/csv",
        width="stretch",
        help="UTF-8 con BOM: abre en Excel sin problemas de acentos.",
    )
    right.download_button(
        "Exportar HTML",
        data=html_text,
        file_name="incidents.html",
        mime="text/html",
        width="stretch",
        help="Archivo autocontenido: se abre sin conexión ni dependencias.",
    )


def render(selected: EventFilters) -> None:
    settings = get_settings()
    st.title("Incidents")
    st.caption(filters.describe(selected))

    counts = cache.incident_counts()
    _banner(counts)

    st.divider()
    st.subheader("Gestión de incidentes")
    st.caption(
        "`OPEN` espera reconocimiento; `ACK` está visto y en curso; `RESOLVED` "
        "está cerrado. Reabrir reinicia el cooldown para que la próxima "
        "ocurrencia vuelva a notificar."
    )
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

    action_left, action_right = st.columns([1, 2])
    admin = settings.ui.admin_mode
    evaluate_ok = (
        confirm.typed_confirmation(action_left, word="EVALUAR", key="confirm_evaluate")
        if admin
        else False
    )
    if action_left.button(
        "Evaluar ahora",
        width="stretch",
        disabled=not evaluate_ok,
        help="Recorre la ventana visible y abre/actualiza incidentes por fingerprint.",
    ):
        _evaluate_now(window, min_count, selected.min_severity)
    with action_right:
        _exports()

    incidents = cache.incidents()
    tables.incidents_table(incidents)
    if admin:
        _manage(incidents)
    else:
        st.info(
            "La gestión de estados está deshabilitada: el dashboard está en "
            "**sólo lectura**. Actívala con `ELA_ADMIN_MODE=1` (o "
            "`ui.admin_mode: true`).",
            icon="🔒",
        )

    st.divider()
    patterns = cache.recurrence(
        window_minutes=window, min_count=min_count, min_severity=selected.min_severity, limit=None
    )
    total_in_window = sum(row["count"] for row in patterns)

    columns = st.columns(3)
    columns[0].metric("Fingerprints activos", format_int(len(patterns)))
    columns[1].metric("Eventos en la ventana", format_int(total_in_window))
    columns[2].metric(
        "Umbral de incidente",
        format_int(min_count),
        help="Eventos del mismo fingerprint en la ventana a partir de los cuales "
        "se abre un incidente.",
    )

    st.divider()
    st.subheader(f"Recurrencia en los últimos {window} minutos")
    if patterns:
        if any(row["count"] >= min_count for row in patterns):
            st.error(
                "Hay fingerprints por encima del umbral de incidente "
                f"({min_count}). Pulsa «Evaluar ahora» para abrirlos.",
                icon="🚨",
            )
        else:
            st.success("Ningún fingerprint supera todavía el umbral de incidente.", icon="✅")
        tables.ranking_table(patterns[:DISPLAY_LIMIT], kind="categoría")
        if len(patterns) > DISPLAY_LIMIT:
            st.caption(f"Mostrando {DISPLAY_LIMIT} de {format_int(len(patterns))} fingerprints.")
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
        st.plotly_chart(charts.hourly_with_spikes(series, spikes), width="stretch")
        tables.spikes_table(spikes)
        st.caption(
            f"Media y desviación se calculan sin la hora evaluada (leave-one-out): "
            f"con la serie completa, el pico se disimula a sí mismo. Umbral "
            f"z ≥ {analysis.anomaly_zscore}, mínimo {analysis.anomaly_min_samples} "
            "eventos por hora."
        )
    else:
        st.plotly_chart(charts.hourly_with_spikes(series, []), width="stretch")
        st.info(
            f"Ninguna hora supera z ≥ {analysis.anomaly_zscore} con al menos "
            f"{analysis.anomaly_min_samples} eventos."
        )
