"""La capa MCP (`app/mcp/`): las diez tools, servidas en memoria.

Los tests usan el `Client` del SDK contra una instancia de `MCPServer` en el
mismo proceso. Eso prueba las tres cosas que importan:

1. Que el servidor arranca y publica exactamente las diez tools acordadas.
2. Que cada tool **valida** sus entradas y devuelve un error que el modelo
   puede leer, no un crash opaco.
3. Que el contrato de salida es el que dice el schema, no el que el
   implementador recuerda.

Se prueba también el flujo HITL completo: petición → aprobación humana →
ejecución. Sin la parte humana, la tool no muta nada, y eso es exactamente lo
que hay que comprobar.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any

from mcp import Client

from app import config
from app.mcp import hitl
from app.mcp.server import build_server
from tests.helpers import TempDbTestCase

EXPECTED_TOOLS = {
    "system_health",
    "search_events",
    "top_errors",
    "detect_anomalies",
    "compare_windows",
    "blast_radius",
    "explain_fingerprint",
    "list_incidents",
    "get_incident",
    "set_incident_status",
}


class McpTestCase(TempDbTestCase):
    """Base con la configuración apuntando a la base temporal."""

    def setUp(self) -> None:
        super().setUp()
        self._saved_db = os.environ.get("ELA_DB_PATH")
        os.environ["ELA_DB_PATH"] = str(self.db_path)
        config._cached = None
        self.addCleanup(self._restore_config)

        self.conn = self.connect()
        self.file_id = self.conn.execute(
            "INSERT INTO log_files (path, first_seen, last_seen) VALUES (?, ?, ?)",
            ("logs/app.log", "2026-10-01T00:00:00.000Z", "2026-10-20T00:00:00.000Z"),
        ).lastrowid
        self._offset = 0

    def _restore_config(self) -> None:
        if self._saved_db is None:
            os.environ.pop("ELA_DB_PATH", None)
        else:
            os.environ["ELA_DB_PATH"] = self._saved_db
        config._cached = None

    def add_event(
        self,
        *,
        ts: str,
        message: str,
        severity: int = 45,
        service: str | None = "checkout",
        category: str | None = "db_pool",
        fingerprint: str = "fp_pool",
        parsed_ok: int = 1,
        parse_error: str | None = None,
    ) -> int:
        self._offset += 1
        return int(
            self.conn.execute(
                "INSERT INTO events (file_id, offset, timestamp_utc, severity, message,"
                " raw_line, parsed_ok, parse_error, service, category, fingerprint)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.file_id,
                    self._offset,
                    ts,
                    severity,
                    message,
                    f"{ts} {message}",
                    parsed_ok,
                    parse_error,
                    service,
                    category,
                    fingerprint,
                ),
            ).lastrowid
        )

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoca una tool contra un servidor en memoria."""

        async def _run() -> Any:
            async with Client(build_server()) as client:
                return await client.call_tool(name, arguments or {})

        return asyncio.run(_run())

    def payload(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoca una tool y devuelve su contenido estructurado."""
        result = self.call(name, arguments)
        self.assertFalse(
            getattr(result, "is_error", False),
            f"{name} falló: {result.content}",
        )
        return result.structured_content


class ToolCatalogTests(McpTestCase):
    def test_se_publican_exactamente_las_diez_tools(self):
        async def _list() -> list[str]:
            async with Client(build_server()) as client:
                listing = await client.list_tools()
                return [tool.name for tool in listing.tools]

        names = asyncio.run(_list())
        self.assertEqual(set(names), EXPECTED_TOOLS)
        self.assertEqual(len(names), 10)

    def test_cada_tool_publica_schema_de_entrada_y_de_salida(self):
        async def _schemas() -> list[tuple[str, bool, bool]]:
            async with Client(build_server()) as client:
                listing = await client.list_tools()
                return [
                    (tool.name, bool(tool.input_schema), bool(tool.output_schema))
                    for tool in listing.tools
                ]

        for name, has_input, has_output in asyncio.run(_schemas()):
            self.assertTrue(has_input, f"{name} no publica input_schema")
            self.assertTrue(has_output, f"{name} no publica output_schema")

    def test_la_tool_mutante_se_declara_destructiva(self):
        async def _annotations() -> dict[str, Any]:
            async with Client(build_server()) as client:
                listing = await client.list_tools()
                return {tool.name: tool.annotations for tool in listing.tools}

        annotations = asyncio.run(_annotations())
        mutante = annotations["set_incident_status"]
        self.assertFalse(mutante.read_only_hint)
        self.assertTrue(mutante.destructive_hint)
        for name, annotation in annotations.items():
            if name == "set_incident_status":
                continue
            self.assertTrue(annotation.read_only_hint, f"{name} debería declararse de sólo lectura")


class SystemHealthTests(McpTestCase):
    def test_devuelve_cobertura_y_nivel_de_confianza(self):
        self.add_event(
            ts="2026-10-01T10:00:00.000Z",
            message="no se pudo conectar al pool",
        )
        self.add_event(
            ts="2026-10-01T10:05:00.000Z",
            message="linea sin parsear",
            severity=0,
            category=None,
            parsed_ok=0,
            parse_error="sin patrón",
        )
        data = self.payload("system_health")
        self.assertEqual(data["total_events"], 2)
        self.assertEqual(data["parsed_events"], 1)
        self.assertEqual(data["unparsed_events"], 1)
        self.assertAlmostEqual(data["parse_coverage"], 0.5)
        self.assertEqual(data["trust_level"], "low")
        self.assertEqual(data["top_parse_errors"][0]["parse_error"], "sin patrón")

    def test_un_dataset_vacio_es_de_confianza_baja(self):
        data = self.payload("system_health")
        self.assertEqual(data["total_events"], 0)
        self.assertEqual(data["trust_level"], "low")


class SearchEventsTests(McpTestCase):
    def setUp(self) -> None:
        super().setUp()
        for index in range(7):
            self.add_event(
                ts=f"2026-10-01T10:0{index}:00.000Z",
                message=f"fallo numero {index}",
            )

    def test_pagina_y_declara_el_total(self):
        data = self.payload("search_events", {"limit": 3})
        self.assertEqual(len(data["events"]), 3)
        self.assertEqual(data["total"], 7)
        self.assertTrue(data["has_more"])

    def test_el_orden_es_de_mas_reciente_a_mas_antigua(self):
        data = self.payload("search_events", {"limit": 2})
        self.assertEqual(
            [event["timestamp_utc"] for event in data["events"]],
            ["2026-10-01T10:06:00.000Z", "2026-10-01T10:05:00.000Z"],
        )

    def test_la_ventana_es_semiabierta(self):
        data = self.payload(
            "search_events",
            {
                "since": "2026-10-01T10:02:00Z",
                "until": "2026-10-01T10:04:00Z",
            },
        )
        # 10:02 y 10:03 entran; 10:04 queda fuera.
        self.assertEqual(data["total"], 2)

    def test_un_rango_sin_milisegundos_no_excluye_el_evento_del_borde(self):
        """El bug que justifica `app/timeutil.py`.

        El texto de la base siempre lleva milisegundos. Un `since` sin ellos
        ordenaría *después* del evento del borde y lo dejaría fuera, en
        silencio.
        """
        data = self.payload("search_events", {"since": "2026-10-01T10:00:00Z"})
        self.assertEqual(data["total"], 7)

    def test_el_texto_libre_escapa_los_comodines(self):
        self.conn.execute("DELETE FROM events")
        self.add_event(ts="2026-10-01T10:00:00.000Z", message="uso de CPU al 100% del nodo")
        self.add_event(ts="2026-10-01T10:01:00.000Z", message="uso de CPU al 50% del nodo")
        data = self.payload("search_events", {"search": "100%"})
        self.assertEqual(data["total"], 1)

    def test_un_limite_invalido_es_un_error_legible(self):
        result = self.call("search_events", {"limit": 0})
        self.assertTrue(result.is_error)
        self.assertIn("limit", result.content[0].text)


class TopErrorsTests(McpTestCase):
    def test_agrupa_por_huella_y_calcula_la_fraccion(self):
        for index in range(3):
            self.add_event(
                ts=f"2026-10-01T10:0{index}:00.000Z",
                message="pool agotado",
                fingerprint="fp_pool",
            )
        self.add_event(
            ts="2026-10-01T10:05:00.000Z",
            message="timeout de disco",
            fingerprint="fp_disk",
            category="storage",
        )
        data = self.payload("top_errors")
        self.assertEqual(data["total_errors"], 4)
        self.assertEqual(data["errors"][0]["fingerprint"], "fp_pool")
        self.assertEqual(data["errors"][0]["count"], 3)
        self.assertAlmostEqual(data["errors"][0]["share"], 0.75)


class AnomalyTests(McpTestCase):
    def test_encuentra_una_hora_con_pico(self):
        for hour in range(24):
            for index in range(2):
                self.add_event(
                    ts=f"2026-10-02T{hour:02d}:{index:02d}:00.000Z",
                    message="error de fondo",
                )
        for index in range(40):
            self.add_event(
                ts=f"2026-10-02T12:{index:02d}:00.000Z",
                message="error de fondo",
            )
        data = self.payload("detect_anomalies", {"z_threshold": 3.0})
        buckets = [item["bucket_start"] for item in data["anomalies"]]
        self.assertIn("2026-10-02T12:00", buckets)


class CompareWindowsTests(McpTestCase):
    def _seed_baseline(self) -> None:
        """Siete días de actividad con algo de dispersión.

        La dispersión importa: un baseline completamente plano no tiene
        desviación, y entonces el z-score no está definido. Es un caso real
        (un servicio que falla siempre igual) y por eso tiene su propio test,
        pero para el camino normal hace falta un baseline con varianza.
        """
        for day in range(1, 8):
            for hour in range(24):
                self.add_event(
                    ts=f"2026-10-{day:02d}T{hour:02d}:00:00.000Z",
                    message="pool agotado",
                    fingerprint="fp_pool",
                )
                if hour % 4 == 0:
                    self.add_event(
                        ts=f"2026-10-{day:02d}T{hour:02d}:30:00.000Z",
                        message="pool agotado",
                        fingerprint="fp_pool",
                    )

    def test_detecta_una_ventana_elevada(self):
        self._seed_baseline()
        for index in range(120):
            self.add_event(
                ts=f"2026-10-08T11:{index % 60:02d}:{index // 60:02d}.000Z",
                message="pool agotado",
                fingerprint="fp_pool",
            )
        data = self.payload(
            "compare_windows",
            {
                "fingerprint": "fp_pool",
                "since": "2026-10-08T00:00:00Z",
                "until": "2026-10-09T00:00:00Z",
            },
        )
        self.assertEqual(data["verdict"], "elevated")
        self.assertGreater(data["ratio"], 1)
        self.assertIsNotNone(data["z_score"])
        self.assertGreater(data["z_score"], 3.0)

    def test_un_baseline_plano_no_impide_detectar_la_elevacion(self):
        """Un baseline plano no tiene desviación, y el z-score no existe.

        El veredicto no puede depender sólo del z-score: un baseline
        perfectamente constante es el caso más fácil de reconocer y el más
        claro que hay, no el más dudoso.
        """
        for hour in range(24):
            self.add_event(
                ts=f"2026-10-07T{hour:02d}:00:00.000Z",
                message="pool agotado",
                fingerprint="fp_pool",
            )
        for index in range(120):
            self.add_event(
                ts=f"2026-10-08T11:{index % 60:02d}:{index // 60:02d}.000Z",
                message="pool agotado",
                fingerprint="fp_pool",
            )
        data = self.payload(
            "compare_windows",
            {
                "fingerprint": "fp_pool",
                "since": "2026-10-08T00:00:00Z",
                "until": "2026-10-09T00:00:00Z",
            },
        )
        self.assertIsNone(data["z_score"])
        self.assertEqual(data["baseline"]["stddev"], 0.0)
        self.assertEqual(data["verdict"], "elevated")

    def test_una_huella_inexistente_es_un_error_legible(self):
        result = self.call("compare_windows", {"fingerprint": "no_existe"})
        self.assertTrue(result.is_error)
        self.assertIn("no_existe", result.content[0].text)

    def test_una_ventana_a_solo_extremo_es_un_error_legible(self):
        """Falta `until`: la ventana no tiene fin y no es medible.

        Inventar el extremo sería peor que fallar: devolvería el histórico
        entero con la etiqueta de una hora.
        """
        self._seed_baseline()
        result = self.call(
            "compare_windows",
            {"fingerprint": "fp_pool", "since": "2026-10-08T00:00:00Z"},
        )
        self.assertTrue(result.is_error)
        self.assertIn("until", result.content[0].text)


class BlastRadiusTests(McpTestCase):
    def test_detecta_una_cascada(self):
        self.add_event(
            ts="2026-10-01T10:00:00.000Z",
            message="timeout de pool",
            service="checkout",
            fingerprint="fp_pool",
        )
        self.add_event(
            ts="2026-10-01T10:02:00.000Z",
            message="timeout de pool",
            service="payment",
            fingerprint="fp_pool",
        )
        data = self.payload("blast_radius", {"fingerprint": "fp_pool"})
        self.assertTrue(data["is_cascade"])
        self.assertEqual(data["distinct_services"], 2)
        self.assertIn("cascade_note", data)
        self.assertTrue(data["cascade_note"])

    def test_un_solo_servicio_no_es_cascada(self):
        self.add_event(
            ts="2026-10-01T10:00:00.000Z",
            message="timeout de pool",
            service="checkout",
            fingerprint="fp_pool",
        )
        data = self.payload("blast_radius", {"fingerprint": "fp_pool"})
        self.assertFalse(data["is_cascade"])
        self.assertEqual(data["distinct_services"], 1)

    def test_servicios_lejanos_no_son_cascada(self):
        self.add_event(
            ts="2026-10-01T10:00:00.000Z",
            message="timeout de pool",
            service="checkout",
            fingerprint="fp_pool",
        )
        self.add_event(
            ts="2026-10-01T18:00:00.000Z",
            message="timeout de pool",
            service="payment",
            fingerprint="fp_pool",
        )
        data = self.payload("blast_radius", {"fingerprint": "fp_pool"})
        self.assertFalse(data["is_cascade"])


class ExplainTests(McpTestCase):
    def test_devuelve_la_plantilla_normalizada(self):
        self.add_event(
            ts="2026-10-01T10:00:00.000Z",
            message="connection to 10.0.0.5 failed after 30 retries",
            fingerprint="fp_pool",
        )
        data = self.payload("explain_fingerprint", {"fingerprint": "fp_pool"})
        self.assertIn("<IP>", data["normalized_template"])
        self.assertIn("<NUM>", data["normalized_template"])
        self.assertEqual(data["total_events"], 1)
        self.assertEqual(data["services"], ["checkout"])
        self.assertTrue(data["sample_raw_lines"])


class IncidentTests(McpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_event(
            ts="2026-10-01T10:00:00.000Z",
            message="pool agotado",
            fingerprint="fp_pool",
        )
        self.incident_id = int(
            self.conn.execute(
                "INSERT INTO incidents (fingerprint, category, severity, sample_message,"
                " first_seen, last_seen, event_count, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "fp_pool",
                    "db_pool",
                    45,
                    "pool agotado",
                    "2026-10-01T10:00:00.000Z",
                    "2026-10-01T10:00:00.000Z",
                    1,
                    "OPEN",
                ),
            ).lastrowid
        )

    def test_lista_los_incidentes_con_el_conteo_por_estado(self):
        data = self.payload("list_incidents")
        self.assertEqual(len(data["incidents"]), 1)
        self.assertEqual(data["incidents"][0]["incident_id"], self.incident_id)
        self.assertEqual(data["counts"]["OPEN"], 1)
        self.assertEqual(data["counts"]["RESOLVED"], 0)

    def test_filtrar_por_un_estado_invalido_es_un_error_legible(self):
        result = self.call("list_incidents", {"status": "CERRADO"})
        self.assertTrue(result.is_error)
        self.assertIn("CERRADO", result.content[0].text)

    def test_el_detalle_incluye_evidencia_y_plantilla(self):
        data = self.payload("get_incident", {"incident_id": self.incident_id})
        self.assertTrue(data["found"])
        self.assertEqual(data["incident"]["fingerprint"], "fp_pool")
        self.assertTrue(data["incident"]["evidence"])
        self.assertIn("pool", data["incident"]["normalized_template"])

    def test_un_incidente_inexistente_no_es_un_error(self):
        data = self.payload("get_incident", {"incident_id": 9999})
        self.assertFalse(data["found"])
        self.assertIsNone(data["incident"])


class HitlTests(McpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_event(
            ts="2026-10-01T10:00:00.000Z",
            message="pool agotado",
            fingerprint="fp_pool",
        )
        self.incident_id = int(
            self.conn.execute(
                "INSERT INTO incidents (fingerprint, category, severity, sample_message,"
                " first_seen, last_seen, event_count, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "fp_pool",
                    "db_pool",
                    45,
                    "pool agotado",
                    "2026-10-01T10:00:00.000Z",
                    "2026-10-01T10:00:00.000Z",
                    1,
                    "OPEN",
                ),
            ).lastrowid
        )

    def _status(self, incident_id: int) -> str:
        row = self.conn.execute(
            "SELECT status FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        return str(row["status"])

    def test_sin_aprobacion_no_muta_nada(self):
        data = self.payload(
            "set_incident_status",
            {"incident_id": self.incident_id, "status": "ACKNOWLEDGED"},
        )
        self.assertEqual(data["status"], "pending_approval")
        self.assertIsNotNone(data["pending_action"])
        # El token es la prueba de la aprobación: no puede existir antes de que
        # alguien apruebe, o el agente podría auto-aprobar su propia mutación.
        self.assertNotIn("confirm_token", data["pending_action"])
        self.assertEqual(self._status(self.incident_id), "OPEN")

    def test_el_flujo_completo_aplica_el_cambio(self):
        pending = self.payload(
            "set_incident_status",
            {"incident_id": self.incident_id, "status": "ACKNOWLEDGED"},
        )
        action_id = pending["pending_action"]["action_id"]

        # El acto humano: aprobar desde fuera del bucle del agente.
        with self.connect() as conn:
            action = hitl.approve_action(conn, action_id, approved_by="tester")
        self.assertEqual(action.status, hitl.APPROVED)
        self.assertIsNotNone(action.confirm_token)

        data = self.payload(
            "set_incident_status",
            {
                "incident_id": self.incident_id,
                "status": "ACKNOWLEDGED",
                "confirm_token": action.confirm_token,
            },
        )
        self.assertEqual(data["status"], "executed")
        self.assertEqual(data["incident_status"], "ACKNOWLEDGED")
        self.assertEqual(self._status(self.incident_id), "ACKNOWLEDGED")

    def test_un_token_invalido_no_muta_nada(self):
        result = self.call(
            "set_incident_status",
            {
                "incident_id": self.incident_id,
                "status": "ACKNOWLEDGED",
                "confirm_token": "0" * 64,
            },
        )
        self.assertTrue(result.is_error)
        self.assertEqual(self._status(self.incident_id), "OPEN")

    def test_un_token_de_otra_accion_no_sirve(self):
        """El token ata la aprobación a una acción concreta.

        Aprobar el cambio a ACKNOWLEDGED no autoriza un RESOLVED: son
        acciones distintas y la aprobación es por acción.
        """
        pending = self.payload(
            "set_incident_status",
            {"incident_id": self.incident_id, "status": "ACKNOWLEDGED"},
        )
        action_id = pending["pending_action"]["action_id"]
        with self.connect() as conn:
            action = hitl.approve_action(conn, action_id, approved_by="tester")

        result = self.call(
            "set_incident_status",
            {
                "incident_id": self.incident_id,
                "status": "RESOLVED",
                "confirm_token": action.confirm_token,
            },
        )
        self.assertTrue(result.is_error)
        self.assertEqual(self._status(self.incident_id), "OPEN")

    def test_la_accion_queda_auditada(self):
        pending = self.payload(
            "set_incident_status",
            {"incident_id": self.incident_id, "status": "ACKNOWLEDGED"},
        )
        action_id = pending["pending_action"]["action_id"]
        with self.connect() as conn:
            hitl.approve_action(conn, action_id, approved_by="tester")
            action = hitl.get_action(conn, action_id)
        self.assertIsNotNone(action)
        self.assertEqual(action.status, hitl.APPROVED)
        self.assertEqual(action.approved_by, "tester")
        self.assertIsNotNone(action.approved_at)

    def test_un_estado_invalido_es_un_error_legible(self):
        result = self.call(
            "set_incident_status",
            {"incident_id": self.incident_id, "status": "CERRADO"},
        )
        self.assertTrue(result.is_error)
        self.assertIn("CERRADO", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
