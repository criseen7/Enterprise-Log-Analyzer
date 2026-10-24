"""Resources y prompts MCP: el contexto que no cuesta una tool call.

Se comprueba lo que el cliente MCP ve de verdad: la lista publicada, los
argumentos de cada prompt y que un resource de plantilla resuelve contra la
base. Un resource que no está en `list_resources` no existe para el agente,
aunque la función esté escrita.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp import Client

from app.mcp.server import build_server
from tests.test_mcp_tools import McpTestCase


def _error_text(exc: BaseException) -> str:
    """Texto de un error, aplanando el `ExceptionGroup` de anyio."""
    if isinstance(exc, BaseExceptionGroup):
        return " | ".join(_error_text(inner) for inner in exc.exceptions)
    return str(exc)


class ResourceCatalogTests(McpTestCase):
    def test_se_publican_los_resources_y_la_plantilla(self):
        async def _list() -> tuple[list[str], list[str]]:
            async with Client(build_server()) as client:
                resources = await client.list_resources()
                templates = await client.list_resource_templates()
                return (
                    [str(item.uri) for item in resources.resources],
                    [item.uri_template for item in templates.resource_templates],
                )

        uris, templates = asyncio.run(_list())
        self.assertEqual(set(uris), {"loglens://schema", "loglens://rules"})
        self.assertEqual(
            templates,
            ["loglens://incident/{incident_id}", "loglens://trace/{run_id}"],
        )

    def test_el_esquema_es_json_con_la_semantica_de_tiempo_y_severidad(self):
        async def _read() -> str:
            async with Client(build_server()) as client:
                result = await client.read_resource("loglens://schema")
                return result.contents[0].text

        data = json.loads(asyncio.run(_read()))
        self.assertEqual(data["table"], "events")
        self.assertIn("fingerprint", data["columns"])
        # La semántica que el agente no puede deducir de los datos:
        self.assertIn("[since, until)", data["time_semantics"]["ranges"])
        self.assertEqual(data["severity_scale"]["ERROR"], 40)

    def test_las_reglas_conservan_el_orden_de_prioridad(self):
        async def _read() -> str:
            async with Client(build_server()) as client:
                result = await client.read_resource("loglens://rules")
                return result.contents[0].text

        data = json.loads(asyncio.run(_read()))
        self.assertTrue(data["rules"])
        self.assertEqual(data["precedence"].split(";")[0], "first match wins")
        ids = [rule["id"] for rule in data["rules"]]
        self.assertEqual(len(ids), len(set(ids)), "ids duplicados en el catálogo")
        for rule in data["rules"]:
            self.assertIn("category", rule)
            self.assertTrue(rule["patterns"])

    def test_leer_un_resource_inexistente_es_un_error(self):
        async def _read() -> Any:
            async with Client(build_server()) as client:
                return await client.read_resource("loglens://no-existe")

        with self.assertRaises(Exception) as ctx:
            asyncio.run(_read())
        self.assertIn("no-existe", _error_text(ctx.exception))


class IncidentResourceTests(McpTestCase):
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

    def _read(self, uri: str) -> Any:
        async def _run() -> Any:
            async with Client(build_server()) as client:
                return await client.read_resource(uri)

        return asyncio.run(_run())

    def test_la_plantilla_resuelve_al_mismo_contenido_que_la_tool(self):
        resource = json.loads(self._read(f"loglens://incident/{self.incident_id}").contents[0].text)
        tool = self.payload("get_incident", {"incident_id": self.incident_id})["incident"]
        self.assertEqual(resource["incident_id"], tool["incident_id"])
        self.assertEqual(resource["fingerprint"], tool["fingerprint"])
        self.assertEqual(resource["normalized_template"], tool["normalized_template"])

    def test_un_incidente_inexistente_es_un_error_de_resource(self):
        with self.assertRaises(Exception) as ctx:
            self._read("loglens://incident/9999")
        self.assertIn("9999", _error_text(ctx.exception))


class TraceResourceTests(McpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.traces = self.root / "traces"
        self.traces.mkdir()
        self._saved = os.environ.get("LOGLENS_TRACES_DIR")
        os.environ["LOGLENS_TRACES_DIR"] = str(self.traces)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop("LOGLENS_TRACES_DIR", None)
        else:
            os.environ["LOGLENS_TRACES_DIR"] = self._saved

    def _read(self, uri: str) -> str:
        async def _run() -> str:
            async with Client(build_server()) as client:
                result = await client.read_resource(uri)
                return result.contents[0].text

        return asyncio.run(_run())

    def test_una_traza_existente_se_publica_tal_cual(self):
        content = '{"kind": "run.start"}\n{"kind": "run.end"}\n'
        (self.traces / "run-abc.jsonl").write_text(content, encoding="utf-8")
        self.assertEqual(self._read("loglens://trace/run-abc"), content)

    def test_una_traza_inexistente_es_un_error(self):
        with self.assertRaises(Exception) as ctx:
            self._read("loglens://trace/run-nope")
        self.assertIn("run-nope", _error_text(ctx.exception))

    def test_un_run_id_con_ruta_no_es_un_resource(self):
        from app.mcp.resources import trace_resource

        with self.assertRaises(Exception) as ctx:
            trace_resource("../../etc/passwd")
        self.assertIn("inválido", str(ctx.exception))

    def test_un_run_id_codificado_no_alcanza_el_disco(self):
        with self.assertRaises(Exception) as ctx:
            self._read("loglens://trace/..%2F..%2Fetc%2Fpasswd")
        text = _error_text(ctx.exception)
        self.assertIn("resource", text.lower())


class PromptCatalogTests(McpTestCase):
    def test_se_publican_los_dos_prompts_con_sus_argumentos(self):
        async def _list() -> dict[str, list[str]]:
            async with Client(build_server()) as client:
                listing = await client.list_prompts()
                return {
                    prompt.name: [arg.name for arg in (prompt.arguments or [])]
                    for prompt in listing.prompts
                }

        prompts = asyncio.run(_list())
        self.assertEqual(set(prompts), {"investigate_symptom", "write_postmortem"})
        self.assertEqual(prompts["investigate_symptom"][0], "symptom")
        self.assertIn("incident_id", prompts["write_postmortem"])

    def test_el_prompt_de_investigacion_fija_el_orden_y_la_regla_de_evidencia(self):
        async def _get() -> str:
            async with Client(build_server()) as client:
                result = await client.get_prompt(
                    "investigate_symptom", {"symptom": "errores 500 en checkout"}
                )
                return result.messages[0].content.text

        text = asyncio.run(_get())
        self.assertIn("errores 500 en checkout", text)
        # El orden no es decorativo: compare_windows antes de afirmar un cambio.
        self.assertLess(text.index("system_health"), text.index("compare_windows"))
        self.assertIn("no afirmes", text.lower())
        self.assertIn("fingerprint", text)

    def test_el_postmortem_no_permite_atribuir_causa_sin_evidencia(self):
        async def _get() -> str:
            async with Client(build_server()) as client:
                result = await client.get_prompt("write_postmortem", {"incident_id": "7"})
                return result.messages[0].content.text

        text = asyncio.run(_get())
        self.assertIn("incident_id=7", text)
        self.assertIn("no determinada por los datos disponibles", text)
        self.assertIn("correlación temporal", text)
