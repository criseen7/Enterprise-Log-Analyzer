"""Tablas del dashboard.

Las filas llegan como diccionarios desde `app.ui.cache` y aquí se convierten en
`DataFrame`. Dos detalles deliberados:

* **`hide_index=True` siempre.** El índice de pandas es un artefacto de
  implementación: mostrarlo hace pensar que es un identificador del evento.
  Lo es `events.id`, y si se muestra, se muestra como columna con ese nombre.
* **Los nulos se sustituyen por `—`.** Una celda vacía en una tabla de incidentes
  se lee como un bug de la herramienta; el guion dice "este formato no lo trae".
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from app.parser.severity import severity_name
from app.ui.theme import shorten

_DASH = "—"


def _ts(values: list[str | None]) -> pd.Series:
    return pd.to_datetime(pd.Series(values), utc=True, errors="coerce")


def events_table(rows: list[dict]) -> None:
    frame = pd.DataFrame(
        {
            "ts": _ts([row["timestamp_utc"] for row in rows]),
            "sev": [severity_name(row["severity"]) for row in rows],
            "categoría": [row["category"] or _DASH for row in rows],
            "servicio": [row["service"] or _DASH for row in rows],
            "mensaje": [shorten(row["message"] or row["raw_line"], 140) for row in rows],
            "huella": [row["fingerprint"] or _DASH for row in rows],
            "origen": [
                Path(row["source_path"]).name if row["source_path"] else _DASH
                for row in rows
            ],
        }
    )
    st.dataframe(
        frame,
        use_container_width=True,
        hide_index=True,
        height=min(120 + 35 * max(len(frame), 1), 620),
        column_config={
            "ts": st.column_config.DatetimeColumn("timestamp (UTC)", format="YYYY-MM-DD HH:mm:ss"),
            "sev": st.column_config.TextColumn("sev", width="small"),
            "mensaje": st.column_config.TextColumn("mensaje", width="large"),
            "huella": st.column_config.TextColumn("fingerprint", width="small"),
        },
    )


def event_inspector(rows: list[dict]) -> None:
    """Visor de una fila completa: `raw_line` y `parse_error` no caben en la tabla.

    Sin esto, la única forma de ver por qué una línea no se parseó es consultar
    la base a mano — justo lo que el dashboard viene a evitar.
    """
    if not rows:
        return
    with st.expander("Inspeccionar un evento", expanded=False):
        labels = [
            f"{row['timestamp_utc']} · {severity_name(row['severity'])} · "
            f"{shorten(row['message'] or row['raw_line'], 70)}"
            for row in rows
        ]
        chosen = st.selectbox("Evento", range(len(labels)), format_func=lambda i: labels[i])
        row = rows[chosen]
        left, right = st.columns(2)
        with left:
            st.caption("Línea original (raw_line)")
            st.code(row["raw_line"] or "", language="text")
        with right:
            st.caption("Mensaje interpretado")
            st.code(row["message"] or "(sin mensaje)", language="text")
            meta = {
                "categoría": row["category"] or _DASH,
                "servicio": row["service"] or _DASH,
                "fingerprint": row["fingerprint"] or _DASH,
                "origen": row["source_path"] or _DASH,
                "parsed_ok": bool(row["parsed_ok"]),
                "parse_error": row["parse_error"] or _DASH,
            }
            st.json(meta, expanded=True)


def ranking_table(rows: list[dict], *, kind: str = "errores") -> None:
    frame = pd.DataFrame(
        {
            "n": [row["count"] for row in rows],
            kind: [row.get("category") or "sin categoría" for row in rows],
            "sev": [severity_name(row.get("severity")) for row in rows],
            "muestra": [shorten(row.get("sample_message"), 110) for row in rows],
            "huella": [row.get("fingerprint") or _DASH for row in rows],
            "primero": _ts([row.get("first_seen") for row in rows]),
            "último": _ts([row.get("last_seen") for row in rows]),
        }
    )
    st.dataframe(
        frame,
        use_container_width=True,
        hide_index=True,
        height=min(90 + 35 * max(len(frame), 1), 620),
        column_config={
            "primero": st.column_config.DatetimeColumn("primero", format="MM-DD HH:mm"),
            "último": st.column_config.DatetimeColumn("último", format="MM-DD HH:mm"),
            "muestra": st.column_config.TextColumn("mensaje de muestra", width="large"),
        },
    )


def spikes_table(rows: list[dict]) -> None:
    frame = pd.DataFrame(
        {
            "hora (UTC)": [row["bucket"] for row in rows],
            "eventos": [row["count"] for row in rows],
            "z": ["inf" if row["is_infinite"] else round(row["zscore"], 2) for row in rows],
            "media": [round(row["mean"], 1) for row in rows],
            "desv": [round(row["stdev"], 2) for row in rows],
        }
    )
    st.dataframe(frame, use_container_width=True, hide_index=True)


def runs_table(rows: list[dict]) -> None:
    if not rows:
        st.info("Todavía no hay corridas de ingesta registradas.")
        return
    frame = pd.DataFrame(
        {
            "#": [row["id"] for row in rows],
            "estado": [row["status"] for row in rows],
            "inicio": _ts([row["started_at"] for row in rows]),
            "archivos": [row["files_processed"] for row in rows],
            "líneas": [row["lines_read"] for row in rows],
            "+eventos": [row["events_inserted"] for row in rows],
            "omitidos": [row["events_skipped"] for row in rows],
            "sin parsear": [row["events_unparsed"] for row in rows],
            "ms": [row["duration_ms"] for row in rows],
        }
    )
    st.dataframe(
        frame,
        use_container_width=True,
        hide_index=True,
        column_config={"inicio": st.column_config.DatetimeColumn("inicio", format="MM-DD HH:mm:ss")},
    )


def files_table(rows: list[dict]) -> None:
    if not rows:
        st.info("No hay archivos vigilados todavía.")
        return
    frame = pd.DataFrame(
        {
            "archivo": [Path(row["path"]).name for row in rows],
            "ruta": [row["path"] for row in rows],
            "activo": [bool(row["is_active"]) for row in rows],
            "gen": [row["generation"] for row in rows],
            "bytes": [row["size"] for row in rows],
            "leído hasta": [row["last_offset"] for row in rows],
            "eventos": [row["events"] for row in rows],
            "visto": _ts([row["last_seen"] for row in rows]),
        }
    )
    st.dataframe(
        frame,
        use_container_width=True,
        hide_index=True,
        column_config={"visto": st.column_config.DatetimeColumn("visto", format="MM-DD HH:mm:ss")},
    )


__all__ = [
    "event_inspector",
    "events_table",
    "files_table",
    "ranking_table",
    "runs_table",
    "spikes_table",
]
