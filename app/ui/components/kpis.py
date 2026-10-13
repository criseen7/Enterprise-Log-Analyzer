"""Filas de KPIs y ayudas para estados vacíos.

Los KPIs van en `st.metric` y no en tarjetas HTML propias porque Streamlit ya
resuelve los deltas y el responsive; reinventarlo añade CSS que hay que mantener
y rompe con el modo oscuro.
"""

from __future__ import annotations

import streamlit as st

from app.parser.severity import ERROR, severity_name
from app.ui.theme import format_int


def kpi_row(stats: dict) -> None:
    """Cinco KPIs. El orden es el del recorrido mental: volumen → riesgo → calidad."""
    columns = st.columns(5)
    columns[0].metric("Eventos", format_int(stats["events"]))
    columns[1].metric(
        f"Errores (≥ {severity_name(ERROR)})",
        format_int(stats["errors"]),
    )
    columns[2].metric("Fingerprints", format_int(stats["fingerprints"]))
    columns[3].metric(
        "Cobertura",
        f"{stats['coverage']:.1%}" if stats["events"] else "-",
        delta=f"{format_int(stats['unparsed'])} sin parsear" if stats["unparsed"] else None,
        # Más líneas sin parsear es peor: el color del delta se invierte.
        delta_color="inverse",
        help="Porcentaje de líneas reconocidas por algún patrón. Es el indicador "
             "de pérdida de datos: lo que no se parsea no entra en las métricas.",
    )
    columns[4].metric(
        "Ventana",
        f"{stats['first_ts'][:10]}" if stats["first_ts"] else "-",
        delta=f"→ {stats['last_ts'][:10]}" if stats["last_ts"] else None,
        delta_color="off",
        help="Primer y último timestamp del dataset (UTC), con los filtros aplicados.",
    )


def coverage_warning(stats: dict) -> None:
    """Avisa cuando hay líneas sin parsear, con el siguiente paso concreto."""
    if not stats["events"] or not stats["unparsed"]:
        return
    st.warning(
        f"{format_int(stats['unparsed'])} de {format_int(stats['events'])} líneas "
        f"no coinciden con ningún patrón ({1 - stats['coverage']:.1%}). "
        "No se han descartado: están en Events con severidad UNKNOWN y el motivo "
        "en `parse_error`. Ver Events → «Sólo sin parsear».",
        icon="⚠️",
    )


def empty_state(title: str, message: str, command: str | None = None) -> None:
    """Estado vacío accionable: qué pasa, por qué y qué hacer.

    Un "no hay datos" sin el comando a ejecutar deja al usuario mirando una
    pantalla en blanco sin salida.
    """
    st.subheader(title)
    st.info(message)
    if command:
        st.code(command, language="bash")


__all__ = ["coverage_warning", "empty_state", "kpi_row"]
