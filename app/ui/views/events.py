"""Events: la tabla que reemplaza al `grep`.

Dos cosas que un `grep` no hace y aquí sí importan:

* **Paginación con `LIMIT`/`OFFSET`.** El dashboard nunca carga la tabla entera:
  con 10 eventos da igual, con 10 millones es la diferencia entre una tabla y un
  proceso muerto.
* **Inspección de la línea cruda.** El mensaje interpretado puede ocultar el
  problema; `raw_line` y `parse_error` están a un clic.
"""

from __future__ import annotations

import dataclasses
import math

import streamlit as st

from app.config import get_settings
from app.db.connection import connection
from app.ui import data
from app.ui.components import filters, kpis, tables
from app.ui.data import EventFilters


def render(selected: EventFilters) -> None:
    settings = get_settings()
    st.title("Events")
    st.caption(filters.describe(selected))

    only_unparsed = st.toggle(
        "Sólo líneas sin parsear (parsed_ok = 0)",
        value=selected.unparsed_only,
        help="Lo que ningún patrón reconoció. Se conserva y se reporta: es el "
        "indicador de pérdida de datos del dataset.",
    )
    if only_unparsed != selected.unparsed_only:
        # La severidad se pone a 0: estas líneas son UNKNOWN, y decir
        # "severidad ≥ 45" en la cabecera sería contradecir lo que se muestra.
        selected = dataclasses.replace(selected, unparsed_only=only_unparsed, min_severity=0)

    with connection(read_only=True) as conn:
        total = data.count_events_filtered(conn, selected)

        if not total:
            kpis.empty_state(
                "Ningún evento coincide con los filtros",
                "Amplía el rango temporal o baja la severidad mínima en la barra "
                "lateral. Si el dataset está vacío, ingiere logs primero.",
                command="python3 -m app.ingest --once",
            )
            return

        # La página se reinicia cuando cambian los filtros: mantener la página 12
        # de una consulta que ahora tiene 2 páginas devuelve una tabla vacía.
        signature = filters.describe(selected)
        if st.session_state.get("events_signature") != signature:
            st.session_state["events_signature"] = signature
            st.session_state["events_page"] = 1

        page_size = st.selectbox(
            "Filas por página",
            [50, 100, 250, 500],
            index=[50, 100, 250, 500].index(settings.ui.default_page_size)
            if settings.ui.default_page_size in (50, 100, 250, 500)
            else 1,
        )
        pages = max(1, math.ceil(total / page_size))
        page = st.number_input("Página", min_value=1, max_value=pages, step=1, key="events_page")

        rows = data.fetch_events(conn, selected, limit=page_size, offset=(page - 1) * page_size)

    coincidencias = (
        "1 evento coincide" if total == 1 else f"{total:,} eventos coinciden".replace(",", ".")
    )
    st.caption(
        f"{coincidencias} · mostrando {len(rows):,} (página {page} de {pages})".replace(",", ".")
    )
    tables.events_table([dict(row) for row in rows])
    tables.event_inspector([dict(row) for row in rows])
