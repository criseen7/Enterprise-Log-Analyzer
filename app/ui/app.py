"""Punto de entrada del dashboard: `streamlit run app/ui/app.py`

Estructura del archivo, y el orden importa:

1. `set_page_config()` — obligatorio antes de cualquier otro comando de Streamlit.
2. Bootstrap — migración del esquema y, si la base está vacía y hay logs en
   `paths.log_dir`, una ingesta inicial. Un dashboard en blanco sin explicación
   es la peor primera impresión posible.
3. Filtros de la barra lateral — se construyen **una vez aquí** para que
   persistan al cambiar de página. Si cada página declarara los suyos, cambiar
   de pestaña los reiniciaría y obligaría a repetir el mismo clic por gráfico.
4. `st.navigation(...).run()` — cada página es un `render(filtros)`.

El dashboard **sólo lee**: `read_only=True` en todas las consultas. Las únicas
escrituras (ingerir, backfill) las dispara el usuario desde Settings, que es
donde se esperan. Eso es lo que permite en la Fase 6 separar `analyzer` y
`dashboard` en procesos distintos sin cambiar una línea.
"""

from __future__ import annotations

from functools import partial

import streamlit as st

from app.analysis.errors import ErrorRules
from app.config import Settings, get_settings
from app.db.connection import connection
from app.db.migrations import migrate
from app.db.repository import count_events
from app.ingest.ingestor import discover, ingest_paths_transactional, summarize
from app.parser.registry import Registry
from app.ui import cache
from app.ui.components import filters
from app.ui.pages import events, incidents, overview
from app.ui.pages import settings as settings_page
from app.ui.theme import apply_theme, format_int


def _bootstrap(settings: Settings) -> None:
    """Migra el esquema y auto-siembra si la base está vacía.

    El auto-seed corre una sola vez por sesión y sólo si hay archivos que
    ingerir. No se ingiere en cada render: sería una escritura por interacción,
    justo lo que el diseño evita (un solo escritor, y es el watcher).
    """
    migrate(settings.paths.db_path)
    if st.session_state.get("ela_bootstrapped"):
        return
    st.session_state["ela_bootstrapped"] = True

    with connection(read_only=True) as conn:
        if count_events(conn):
            return

    paths = discover(settings)
    if not paths:
        return

    registry = Registry.from_directory(settings.paths.patterns_dir)
    rules = ErrorRules.load(settings.analysis.rules_path)
    with st.spinner(f"Ingiriendo {len(paths)} archivo(s) de {settings.paths.log_dir}…"):
        with connection() as conn:
            run = ingest_paths_transactional(
                conn, registry, paths, settings.ingest.chunk_bytes, rules
            )
            summary = summarize(conn, run)
        cache.clear_caches()
    st.toast(f"Auto-seed: {summary}", icon="🌱")


def _sidebar_footer() -> None:
    """Contexto del dataset al pie de la barra: sobre qué se está mirando."""
    stats = cache.overview_stats()
    with st.sidebar:
        st.divider()
        if stats["first_ts"] and stats["last_ts"]:
            st.caption(
                f"{format_int(stats['events'])} eventos · "
                f"{stats['first_ts'][:10]} → {stats['last_ts'][:10]} UTC"
            )
        st.caption("Datos en UTC · el dashboard lee, nunca escribe.")


def main() -> None:
    settings = get_settings()
    st.set_page_config(
        page_title=settings.ui.page_title,
        page_icon=settings.ui.page_icon,
        layout="wide",
    )
    apply_theme()
    _bootstrap(settings)

    selected = filters.sidebar_filters()
    _sidebar_footer()

    navigation = st.navigation(
        [
            st.Page(
                partial(overview.render, selected),
                title="Overview",
                icon="📊",
                default=True,
                url_path="overview",
            ),
            st.Page(
                partial(events.render, selected),
                title="Events",
                icon="🧾",
                url_path="events",
            ),
            st.Page(
                partial(incidents.render, selected),
                title="Incidents",
                icon="🚨",
                url_path="incidents",
            ),
            st.Page(
                partial(settings_page.render, selected),
                title="Settings",
                icon="⚙️",
                url_path="settings",
            ),
        ]
    )
    navigation.run()


if __name__ == "__main__":
    main()
