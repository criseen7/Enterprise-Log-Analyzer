"""Settings: configuración efectiva, auditoría de la herramienta y acciones.

Esta página existe por una razón concreta de credibilidad: `ingest_runs` y el
plan de ejecución de las consultas están a la vista. Un dashboard que pide que
confíes en sus números sin dejar auditarlos es indistinguible de uno que los
inventa.

Las acciones (ingerir, backfill) escriben en la base. Se ejecutan dentro de una
transacción y **después** se limpia la caché: sin eso, el dashboard seguiría
mostrando los números anteriores y parecería que el botón no hizo nada.
"""

from __future__ import annotations

import streamlit as st

from app.analysis.backfill import backfill
from app.analysis.errors import ErrorRules
from app.config import get_settings
from app.db.connection import connection
from app.db.migrations import applied_versions
from app.db.repository import count_events
from app.ingest.ingestor import discover, ingest_paths_transactional, summarize
from app.parser.registry import Registry
from app.ui import cache, data
from app.ui.components import tables
from app.ui.data import EventFilters
from app.ui.theme import format_int


def _run_ingest() -> str:
    settings = get_settings()
    registry = Registry.from_directory(settings.paths.patterns_dir)
    rules = ErrorRules.load(settings.analysis.rules_path)
    paths = discover(settings)
    if not paths:
        return f"no hay archivos que coincidan con {settings.ingest.include_patterns} en {settings.paths.log_dir}"
    with connection() as conn:
        run = ingest_paths_transactional(
            conn, registry, paths, settings.ingest.chunk_bytes, rules
        )
        return summarize(conn, run)


def render(selected: EventFilters) -> None:
    settings = get_settings()
    st.title("Settings")

    st.subheader("Estado del dataset")
    with connection(read_only=True) as conn:
        total = count_events(conn)
        versions = applied_versions(conn)
        plan = data.explain_plan(conn, selected)

    left, right = st.columns(2)
    with left:
        st.metric("Eventos en la base", format_int(total))
        st.code(
            "\n".join(
                [
                    f"proyecto     : {settings.project_root}",
                    f"db           : {settings.paths.db_path}",
                    f"logs         : {settings.paths.log_dir}",
                    f"exports      : {settings.paths.export_dir}",
                    f"patrones     : {settings.paths.patterns_dir}",
                    f"reglas       : {settings.analysis.rules_path}",
                    f"journal_mode : {settings.db.journal_mode}",
                    f"busy_timeout : {settings.db.busy_timeout_ms} ms",
                    f"esquema      : v{max(versions) if versions else 0} (aplicadas: {versions})",
                ]
            ),
            language="text",
        )
    with right:
        st.metric("TTL de caché", f"{settings.ui.cache_ttl_seconds} s")
        st.code(
            "\n".join(
                [
                    f"min_incident_severity : {settings.analysis.min_incident_severity}",
                    f"window_minutes        : {settings.analysis.window_minutes}",
                    f"incident_threshold    : {settings.analysis.incident_threshold}",
                    f"cooldown_minutes      : {settings.analysis.cooldown_minutes}",
                    f"anomaly_zscore        : {settings.analysis.anomaly_zscore}",
                    f"anomaly_min_samples   : {settings.analysis.anomaly_min_samples}",
                ]
            ),
            language="text",
        )

    st.divider()
    st.subheader("Acciones")
    st.caption(
        "Escrituras sobre la base. La ingesta es idempotente: pulsar de nuevo "
        "inserta 0 eventos si no hay líneas nuevas."
    )
    ingest_col, backfill_col, recalc_col, cache_col = st.columns(4)
    if ingest_col.button("Ingerir ahora", use_container_width=True):
        with st.spinner("Leyendo logs…"):
            message = _run_ingest()
        cache.clear_caches()
        st.success(message)
        st.rerun()
    if backfill_col.button("Rellenar huellas", use_container_width=True):
        rules = ErrorRules.load(settings.analysis.rules_path)
        with connection() as conn:
            touched = backfill(conn, rules)
        cache.clear_caches()
        st.success(f"eventos rellenados: {format_int(touched)}")
        st.rerun()
    if recalc_col.button("Reclasificar todo", use_container_width=True, help="--recalc: reescribe la categoría de todos los eventos"):
        rules = ErrorRules.load(settings.analysis.rules_path)
        with connection() as conn:
            touched = backfill(conn, rules, recalc=True)
        cache.clear_caches()
        st.success(f"eventos reclasificados: {format_int(touched)}")
        st.rerun()
    if cache_col.button("Limpiar caché", use_container_width=True):
        cache.clear_caches()
        st.success("Caché de consultas vaciada.")
        st.rerun()

    st.divider()
    st.subheader("Clasificación (error_rules.yaml)")
    rules = ErrorRules.load(settings.analysis.rules_path)
    st.write(
        f"{len(rules)} reglas · {len(rules.categories)} categorías · "
        "gana la primera coincidencia, en el orden del archivo."
    )
    st.dataframe(
        {
            "regla": [rule.id for rule in rules],
            "categoría": [rule.category for rule in rules],
            "patrones": [" · ".join(rule.patterns) for rule in rules],
        },
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("Plan de ejecución de la consulta de Events")
    st.caption(
        "Con los filtros de la barra lateral aplicados. El criterio de la fase es "
        "que los filtros usen índices: si aparece un `SCAN events`, el filtro "
        "elegido no tiene índice detrás."
    )
    st.code("\n".join(plan) or "(sin plan)", language="text")

    st.divider()
    st.subheader("Últimas corridas de ingesta")
    tables.runs_table(cache.ingest_runs(limit=10))

    st.divider()
    st.subheader("Archivos vigilados")
    tables.files_table(cache.watched_files())
