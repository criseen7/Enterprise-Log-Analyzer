"""Tema visual y formato del dashboard.

Dos reglas que valen para todo el paquete `ui`:

* **El color de una severidad es el mismo en todas las pantallas.** Un heatmap
  no tiene etiquetas: si el rojo significa `FATAL` en el heatmap y `ERROR` en el
  timeline, el dashboard se vuelve ilegible sin que nadie lo note. La paleta vive
  aquí y nadie la redefine en una página.
* **Se formatean números en un solo sitio.** Los conteos son enteros grandes
  (`1.284.912`): el separador de miles no se decide por pantalla.
"""

from __future__ import annotations

import streamlit as st

from app.parser.severity import (
    DEBUG,
    ERROR,
    FATAL,
    INFO,
    TRACE,
    UNKNOWN,
    WARN,
    severity_name,
)

# Paleta por severidad. El rojo de verdad se reserva para FATAL: si ERROR y
# FATAL comparten color, un incidente crítico deja de distinguirse.
SEVERITY_COLORS: dict[str, str] = {
    "UNKNOWN": "#9e9e9e",
    "TRACE": "#7e57c2",
    "DEBUG": "#5c6bc0",
    "INFO": "#42a5f5",
    "WARN": "#ffb300",
    "ERROR": "#ef5350",
    "FATAL": "#b71c1c",
}

# Orden de apilado estable (de menos a más grave) para las series temporales.
SEVERITY_ORDER: tuple[int, ...] = (TRACE, DEBUG, INFO, WARN, ERROR, FATAL, UNKNOWN)

DAY_NAMES: tuple[str, ...] = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

ACCENT = "#42a5f5"
MUTED = "#9e9e9e"

_CSS = """
<style>
  /* Menos aire arriba: el dashboard es una herramienta, no una landing. */
  .block-container { padding-top: 2.2rem; padding-bottom: 2rem; }
  footer { visibility: hidden; }
  /* Las tablas de eventos son densas a propósito: caben más filas por pantalla. */
  [data-testid="stDataFrame"] { border-radius: 6px; }
</style>
"""


def severity_color(value: int | None) -> str:
    return SEVERITY_COLORS[severity_name(value)]


def severity_label(value: int | None) -> str:
    return severity_name(value)


def apply_theme() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def format_int(value: int | None) -> str:
    """Separador de miles con punto (convención es-ES del resto del proyecto)."""
    if value is None:
        return "-"
    return f"{value:,}".replace(",", ".")


def format_share(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def format_coverage(parsed: int, total: int) -> str:
    return "-" if not total else f"{parsed / total:.1%}"


def shorten(text: str | None, limit: int = 90) -> str:
    """Una línea, sin saltos y sin reventar la tabla.

    Los mensajes de un stack trace traen `\\n`: en una celda de tabla hay que
    colapsarlos o `st.dataframe` muestra un scroll vertical dentro de la fila.
    """
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
