"""Fase 4: capa de consultas del dashboard (`app/ui/data.py`).

Este archivo se prueba **sin Streamlit instalado**, y eso es parte del diseño:
`data.py` decide qué SQL corre y no importa Streamlit a propósito. El criterio de
aceptación de la fase —"los filtros usan índices"— se comprueba aquí con
`EXPLAIN QUERY PLAN`, que es una aserción verificable y no una promesa.
"""

from __future__ import annotations

import unittest

from app.ui import data
from tests.helpers import TempDbTestCase


class UiDataTestCase(TempDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = self.connect()
        self.seed_file()

    def seed_file(self) -> int:
        return self.conn.execute(
            "INSERT INTO log_files (path, first_seen, last_seen) VALUES (?, ?, ?)",
            ("logs/app.log", "2026-10-05T12:00:00Z", "2026-10-05T12:00:00Z"),
        ).lastrowid

    def add(
        self,
        *,
        ts: str = "2026-10-05T10:00:00Z",
        severity: int = 45,
        message: str = "algo fue",
        service: str | None = None,
        category: str | None = None,
        parsed_ok: int = 1,
        file_id: int = 1,
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO events (file_id, offset, timestamp_utc, severity, message, raw_line,"
            " parsed_ok, service, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] + 1,
                ts,
                severity,
                message,
                message,
                parsed_ok,
                service,
                category,
            ),
        )
        return cursor.lastrowid


class WhereClauseTests(UiDataTestCase):
    def test_sin_filtros_solo_la_severidad_por_defecto(self):
        where, params = data.EventFilters().where()
        self.assertEqual(where, " WHERE e.severity >= ?")
        self.assertEqual(params, [45])

    def test_el_rango_es_abierto_a_la_derecha(self):
        where, params = data.EventFilters(
            since="2026-10-05T00:00:00Z", until="2026-10-06T00:00:00Z"
        ).where()
        self.assertIn("e.timestamp_utc >= ?", where)
        self.assertIn("e.timestamp_utc < ?", where)
        self.assertEqual(params, [45, "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z"])

    def test_none_y_el_centinela_son_distintos(self):
        """`None` es "no filtrar"; el centinela es "sólo los que no tienen valor"."""
        where, params = data.EventFilters(
            service=data.BLANK_SERVICE, category=data.BLANK_CATEGORY
        ).where()
        self.assertIn("e.service IS NULL", where)
        self.assertIn("e.category IS NULL", where)
        self.assertEqual(params, [45])

    def test_busqueda_escapa_los_comodines_de_like(self):
        where, params = data.EventFilters(search="100%_x").where()
        self.assertIn("ESCAPE", where)
        self.assertEqual(params, [45, "%100\\%\\_x%", "%100\\%\\_x%"])

    def test_solo_sin_parsear(self):
        where, params = data.EventFilters(unparsed_only=True, min_severity=0).where()
        self.assertEqual(where, " WHERE e.parsed_ok = 0")
        self.assertEqual(params, [])

    def test_solo_sin_parsear_anula_la_severidad(self):
        """Una línea sin parsear es UNKNOWN (0): con severidad mínima 45 la
        combinación sería siempre vacía y el interruptor parecería roto."""
        where, _ = data.EventFilters(unparsed_only=True, min_severity=45).where()
        self.assertNotIn("severity", where)


class FetchEventsTests(UiDataTestCase):
    def test_ordena_lo_mas_reciente_primero(self):
        self.add(ts="2026-10-05T09:00:00Z", message="primero")
        self.add(ts="2026-10-05T11:00:00Z", message="tercero")
        self.add(ts="2026-10-05T10:00:00Z", message="segundo")
        rows = data.fetch_events(self.conn, data.EventFilters())
        self.assertEqual([row["message"] for row in rows], ["tercero", "segundo", "primero"])

    def test_paginacion(self):
        for hour in range(5):
            self.add(ts=f"2026-10-05T1{hour}:00:00Z", message=f"evento {hour}")
        first = data.fetch_events(self.conn, data.EventFilters(), limit=2, offset=0)
        second = data.fetch_events(self.conn, data.EventFilters(), limit=2, offset=2)
        self.assertEqual([row["message"] for row in first], ["evento 4", "evento 3"])
        self.assertEqual([row["message"] for row in second], ["evento 2", "evento 1"])
        self.assertEqual(len(data.fetch_events(self.conn, data.EventFilters(), limit=10)), 5)

    def test_limit_invalido(self):
        with self.assertRaises(ValueError):
            data.fetch_events(self.conn, data.EventFilters(), limit=0)

    def test_trae_la_ruta_de_origen(self):
        self.add(message="con ruta")
        row = data.fetch_events(self.conn, data.EventFilters())[0]
        self.assertEqual(row["source_path"], "logs/app.log")

    def test_filtra_por_servicio_y_categoria(self):
        self.add(service="orders", category="timeout", message="del servicio")
        self.add(service="billing", category="payment", message="de otro")
        rows = data.fetch_events(
            self.conn, data.EventFilters(service="orders", category="timeout")
        )
        self.assertEqual([row["message"] for row in rows], ["del servicio"])

    def test_filtra_por_busqueda(self):
        self.add(message="pago rechazado")
        self.add(message="timeout de consulta")
        rows = data.fetch_events(self.conn, data.EventFilters(search="rechaz"))
        self.assertEqual([row["message"] for row in rows], ["pago rechazado"])

    def test_solo_sin_parsear_trae_lo_no_reconocido(self):
        self.add(message="[runtime] línea suelta", parsed_ok=0, severity=0)
        self.add(message="FATAL real")
        rows = data.fetch_events(self.conn, data.EventFilters(unparsed_only=True))
        self.assertEqual([row["message"] for row in rows], ["[runtime] línea suelta"])

    def test_la_busqueda_no_se_come_el_porcentaje(self):
        """El caso que justifica escapar el LIKE: `%` no es comodín para el usuario."""
        self.add(message="uso de disco al 100%")
        self.add(message="esto no coincide")
        rows = data.fetch_events(self.conn, data.EventFilters(search="100%"))
        self.assertEqual([row["message"] for row in rows], ["uso de disco al 100%"])


class CatalogTests(UiDataTestCase):
    def test_conteo_con_filtros(self):
        self.add(severity=45)
        self.add(severity=30)
        self.assertEqual(data.count_events_filtered(self.conn, data.EventFilters()), 1)
        self.assertEqual(
            data.count_events_filtered(self.conn, data.EventFilters(min_severity=0)), 2
        )

    def test_servicios_y_categorias_distintos_y_ordenados(self):
        self.add(service="orders", category="timeout")
        self.add(service="billing", category="auth")
        self.add(service="orders", category="timeout")
        self.assertEqual(data.distinct_services(self.conn), ["billing", "orders"])
        self.assertEqual(data.distinct_categories(self.conn), ["auth", "timeout"])

    def test_limites_del_dataset(self):
        self.add(ts="2026-10-05T09:00:00Z")
        self.add(ts="2026-10-07T18:30:00Z")
        self.assertEqual(
            data.data_time_bounds(self.conn), ("2026-10-05T09:00:00Z", "2026-10-07T18:30:00Z")
        )

    def test_dataset_vacio(self):
        self.assertEqual(data.data_time_bounds(self.conn), (None, None))
        self.assertEqual(data.count_events_filtered(self.conn, data.EventFilters()), 0)


class IndexUsageTests(UiDataTestCase):
    """El criterio de aceptación de la fase, comprobado y no prometido."""

    def test_el_rango_y_la_severidad_usan_indice(self):
        plan = "\n".join(
            data.explain_plan(
                self.conn,
                data.EventFilters(
                    since="2026-10-05T00:00:00Z", until="2026-10-06T00:00:00Z"
                ),
            )
        )
        self.assertNotIn("SCAN e", plan, f"la consulta escanea la tabla:\n{plan}")
        self.assertIn("USING INDEX idx_events_ts", plan, plan)

    def test_el_filtro_por_servicio_usa_su_indice(self):
        plan = "\n".join(
            data.explain_plan(
                self.conn, data.EventFilters(service="orders", min_severity=40)
            )
        )
        self.assertNotIn("SCAN e", plan, f"la consulta escanea la tabla:\n{plan}")
        self.assertIn("USING INDEX idx_events_service_ts", plan, plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
