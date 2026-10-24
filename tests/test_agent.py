"""La capa agentic: plan, ejecución, verificación, citas y traza.

Los tests no llaman a ningún modelo. `NullClient` devuelve respuestas grabadas y
el resto del bucle es el mismo que corre en producción, así que lo que se prueba
es el bucle real: validación del plan, ejecución contra las tools MCP, chequeos
deterministas, validación mecánica de citas y escritura de la traza.

La prueba central de la tesis es simple y no necesita red: un diagnóstico cuyas
citas no existen en el bundle **no puede** salir del bucle. Si esa propiedad
falla, el proyecto entero deja de tener sentido.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from app.agent import citations, executor, planner, verify
from app.agent.contracts import (
    Claim,
    Diagnosis,
    EvidenceBundle,
    EvidenceItem,
    Plan,
    PlanStep,
)
from app.agent.llm import LLMResponse, LLMUnavailable, NullClient, estimate_cost
from app.agent.loop import investigate
from app.agent.promptfiles import PromptError, load_rendered, render
from app.db.connection import connect
from tests.test_mcp_tools import McpTestCase


def _plan_payload() -> str:
    return json.dumps(
        {
            "symptom": "checkout falla",
            "restatement": "checkout falla al pagar desde la última hora",
            "max_steps": 8,
            "steps": [
                {
                    "step_id": 1,
                    "hypothesis": "Confirmar que el dataset permite diagnosticar.",
                    "tool": "system_health",
                    "params": {},
                    "depends_on": [],
                },
                {
                    "step_id": 2,
                    "hypothesis": "Encontrar la huella que concentra los errores.",
                    "tool": "top_errors",
                    "params": {"limit": 5},
                    "depends_on": [1],
                },
                {
                    "step_id": 3,
                    "hypothesis": "Ver si el volumen es nuevo contra el baseline.",
                    "tool": "compare_windows",
                    "params": {"fingerprint": "$top_fingerprint"},
                    "depends_on": [2],
                },
            ],
        }
    )


def _diagnosis_payload(evidence_id: str = "fp:fp_pool") -> str:
    return json.dumps(
        {
            "symptom": "checkout falla",
            "root_cause": "pool de conexiones agotado",
            "root_cause_fingerprint": "fp_pool",
            "claims": [
                {
                    "text": "El pool de conexiones se agotó en checkout",
                    "evidence_ids": [evidence_id],
                    "kind": "fact",
                }
            ],
            "affected_services": ["checkout"],
            "confidence": "medium",
            "evidence_gaps": [],
            "recommended_actions": ["revisar el pool"],
        }
    )


class AgentFixture(McpTestCase):
    """Base con una hora de baseline y una ráfaga en la ventana."""

    def setUp(self) -> None:
        super().setUp()
        for hour in (9, 10):
            for minute in (10, 40):
                self.add_event(ts=f"2026-10-01T{hour:02d}:{minute:02d}:00.000Z", message="pool")
        for minute in range(20):
            self.add_event(ts=f"2026-10-01T11:{minute:02d}:00.000Z", message="pool agotado")
        self.trace_dir = self.root / "traces"


class PlanValidationTests(unittest.TestCase):
    def _plan(self, steps: list[PlanStep]) -> Plan:
        return Plan(symptom="x", restatement="x", steps=steps)

    def test_un_plan_valido_pasa(self):
        plan = planner.heuristic_plan("checkout falla")
        plan.validate_shape(planner.PLANNABLE_TOOLS)
        self.assertEqual(len(plan.steps), 5)

    def test_una_tool_fuera_de_la_lista_blanca_se_rechaza(self):
        plan = self._plan(
            [
                PlanStep(
                    step_id=1,
                    hypothesis="Mutar el incidente",
                    tool="set_incident_status",
                )
            ]
        )
        with self.assertRaises(ValueError):
            plan.validate_shape(planner.PLANNABLE_TOOLS)

    def test_una_dependencia_posterior_se_rechaza(self):
        plan = self._plan(
            [
                PlanStep(step_id=1, hypothesis="a", tool="system_health", depends_on=[2]),
                PlanStep(step_id=2, hypothesis="b", tool="top_errors"),
            ]
        )
        with self.assertRaises(ValueError):
            plan.validate_shape(planner.PLANNABLE_TOOLS)

    def test_depender_de_si_mismo_se_rechaza(self):
        plan = self._plan(
            [PlanStep(step_id=1, hypothesis="a", tool="system_health", depends_on=[1])]
        )
        with self.assertRaises(ValueError):
            plan.validate_shape(planner.PLANNABLE_TOOLS)

    def test_step_id_duplicado_se_rechaza(self):
        plan = self._plan(
            [
                PlanStep(step_id=1, hypothesis="a", tool="system_health"),
                PlanStep(step_id=1, hypothesis="b", tool="top_errors"),
            ]
        )
        with self.assertRaises(ValueError):
            plan.validate_shape(planner.PLANNABLE_TOOLS)

    def test_una_hipotesis_vacia_se_rechaza(self):
        with self.assertRaises(ValueError):
            PlanStep(step_id=1, hypothesis="   ", tool="system_health")

    def test_un_plan_sin_pasos_se_rechaza(self):
        with self.assertRaises(ValueError):
            Plan(symptom="x", restatement="x", steps=[])

    def test_el_plan_heuristico_usa_el_placeholder_no_una_huella_inventada(self):
        plan = planner.heuristic_plan("checkout falla")
        fingerprints = [step.params.get("fingerprint") for step in plan.steps]
        self.assertIn(executor.TOP_FINGERPRINT, fingerprints)
        self.assertNotIn("fp_inventado", fingerprints)

    def test_el_plan_heuristico_no_muta(self):
        plan = planner.heuristic_plan("checkout falla")
        tools = {step.tool for step in plan.steps}
        self.assertNotIn("set_incident_status", tools)


class ExecutorTests(AgentFixture):
    def test_el_placeholder_se_resuelve_con_la_huella_del_motor(self):
        plan = planner.heuristic_plan("checkout falla")
        bundle = executor.execute_plan(plan)
        self.assertIn("fp:fp_pool", bundle.by_id)
        self.assertIn("cmp:fp_pool", bundle.by_id)
        compared = bundle.by_id["cmp:fp_pool"]
        self.assertEqual(compared.fields["fingerprint"], "fp_pool")

    def test_el_bundle_queda_con_ids_citables_y_la_salud_del_dataset(self):
        bundle = executor.execute_plan(planner.heuristic_plan("checkout falla"))
        self.assertIn("sys:health", bundle.ids())
        self.assertIn("fp:fp_pool", bundle.ids())
        self.assertIn(bundle.data_trust, {"medium", "high"})

    def test_cada_llamada_queda_registrada_con_su_resultado(self):
        bundle = executor.execute_plan(planner.heuristic_plan("checkout falla"))
        tools = [call.tool for call in bundle.tool_calls]
        self.assertIn("top_errors", tools)
        self.assertTrue(all(call.ok for call in bundle.tool_calls))
        self.assertTrue(all(call.result_hash for call in bundle.tool_calls))

    def test_un_placeholder_sin_huella_previa_es_un_error(self):
        plan = Plan(
            symptom="x",
            restatement="x",
            steps=[
                PlanStep(
                    step_id=1,
                    hypothesis="Comparar sin haber buscado",
                    tool="compare_windows",
                    params={"fingerprint": executor.TOP_FINGERPRINT},
                )
            ],
        )
        with self.assertRaises(executor.ExecutionError):
            executor.execute_plan(plan)


class VerifyTests(AgentFixture):
    def test_sin_confianza_suficiente_se_aborta(self):
        bundle = EvidenceBundle(data_trust="low")
        bundle.add(EvidenceItem(evidence_id="sys:health", kind="health", summary="x"))
        verdict = verify.after_execution(planner.heuristic_plan("x"), bundle)
        self.assertEqual(verdict.action, "abort")
        self.assertIn("fiables", verdict.reason)

    def test_sin_evidencia_se_aborta(self):
        bundle = EvidenceBundle(data_trust="high")
        verdict = verify.after_execution(planner.heuristic_plan("x"), bundle)
        self.assertEqual(verdict.action, "abort")

    def test_una_ventana_vacia_replanifica(self):
        bundle = EvidenceBundle(data_trust="high")
        bundle.add(EvidenceItem(evidence_id="sys:health", kind="health", summary="x"))
        verdict = verify.after_execution(planner.heuristic_plan("x"), bundle)
        self.assertEqual(verdict.action, "replan")

    def test_una_huella_inventada_por_el_plan_replanifica(self):
        bundle = EvidenceBundle(data_trust="high")
        bundle.add(EvidenceItem(evidence_id="sys:health", kind="health", summary="x"))
        bundle.add(EvidenceItem(evidence_id="fp:real", kind="fingerprint", summary="x"))
        plan = Plan(
            symptom="x",
            restatement="x",
            steps=[
                PlanStep(
                    step_id=1,
                    hypothesis="huella adivinada",
                    tool="compare_windows",
                    params={"fingerprint": "fp_inventado"},
                )
            ],
        )
        verdict = verify.after_execution(plan, bundle)
        self.assertEqual(verdict.action, "replan")
        self.assertFalse(verdict.checks["hypothesis_current"])

    def test_con_evidencia_real_se_continua(self):
        bundle = executor.execute_plan(planner.heuristic_plan("checkout falla"))
        verdict = verify.after_execution(planner.heuristic_plan("checkout falla"), bundle)
        self.assertEqual(verdict.action, "continue")


class CitationTests(unittest.TestCase):
    def _bundle(self) -> EvidenceBundle:
        bundle = EvidenceBundle(data_trust="high")
        bundle.add(
            EvidenceItem(
                evidence_id="fp:abc",
                kind="fingerprint",
                summary="3 errores",
                fields={"count": 3},
            )
        )
        return bundle

    def test_una_cita_existente_es_valida(self):
        diagnosis = Diagnosis(
            symptom="x",
            claims=[Claim(text="hubo 3 errores", evidence_ids=["fp:abc"], kind="fact")],
        )
        report = citations.validate(diagnosis, self._bundle())
        self.assertEqual(len(report.valid), 1)
        self.assertEqual(report.invalid, [])

    def test_una_cita_inventada_es_una_alucinacion(self):
        diagnosis = Diagnosis(
            symptom="x",
            claims=[Claim(text="hubo 3 errores", evidence_ids=["ev:9999"], kind="fact")],
        )
        report = citations.validate(diagnosis, self._bundle())
        self.assertEqual(len(report.invalid), 1)
        self.assertEqual(report.fact_failure_rate, 1.0)

    def test_un_hecho_sin_cita_se_degrada_a_hipotesis(self):
        diagnosis = Diagnosis(
            symptom="x",
            claims=[Claim(text="la causa fue el pool", kind="fact")],
        )
        bundle = self._bundle()
        report = citations.validate(diagnosis, bundle)
        cleaned = citations.apply(diagnosis, report)
        self.assertEqual(cleaned.claims[0].kind, "hypothesis")
        self.assertTrue(any("degradaron" in gap for gap in cleaned.evidence_gaps))

    def test_una_alucinacion_se_elimina_y_se_declara_en_los_huecos(self):
        diagnosis = Diagnosis(
            symptom="x",
            claims=[Claim(text="causa: disco", evidence_ids=["ev:9999"], kind="fact")],
        )
        bundle = self._bundle()
        cleaned = citations.apply(diagnosis, citations.validate(diagnosis, bundle))
        self.assertEqual(cleaned.claims, [])
        self.assertTrue(any("ev:9999" in gap for gap in cleaned.evidence_gaps))

    def test_una_recomendacion_no_necesita_evidencia(self):
        diagnosis = Diagnosis(
            symptom="x",
            claims=[Claim(text="revisa el pool", kind="recommendation")],
        )
        report = citations.validate(diagnosis, self._bundle())
        self.assertEqual(report.uncited, [])
        self.assertEqual(len(report.valid), 1)

    def test_solo_se_reintenta_si_falla_una_fraccion_de_los_hechos(self):
        ok = Claim(text="a", evidence_ids=["fp:abc"], kind="fact")
        bad = Claim(text="b", evidence_ids=["ev:1"], kind="fact")
        report = citations.CitationReport(valid=[ok], invalid=[bad])
        self.assertTrue(citations.should_retry(report))
        small = citations.CitationReport(valid=[ok, ok, ok], invalid=[bad])
        self.assertFalse(citations.should_retry(small))


class LoopWithoutModelTests(AgentFixture):
    def test_el_bucle_termina_con_diagnostico_sin_llm(self):
        run = investigate("checkout falla al pagar", trace_dir=self.trace_dir)
        self.assertEqual(run.status, "OK", run.error)
        self.assertIsNotNone(run.diagnosis)
        self.assertEqual(run.diagnosis.root_cause_fingerprint, "fp_pool")
        self.assertTrue(run.diagnosis.claims)
        self.assertEqual(run.citations.invalid, [])

    def test_todas_las_citas_del_diagnostico_existen_en_el_bundle(self):
        run = investigate("checkout falla", trace_dir=self.trace_dir)
        for claim in run.diagnosis.claims:
            for evidence_id in claim.evidence_ids:
                self.assertIn(evidence_id, run.evidence.by_id)

    def test_se_escribe_la_traza_jsonl(self):
        run = investigate("checkout falla", trace_dir=self.trace_dir)
        path = pathlib.Path(run.trace_path)
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        kinds = [line["kind"] for line in lines]
        self.assertIn("run.start", kinds)
        self.assertIn("plan", kinds)
        self.assertIn("evidence", kinds)
        self.assertIn("verify", kinds)
        self.assertIn("run.end", kinds)

    def test_se_registra_la_corrida_en_agent_runs(self):
        run = investigate("checkout falla", trace_dir=self.trace_dir)
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT status, step_count, evidence_count FROM agent_runs WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["step_count"], len(run.plan.steps))

    def test_un_dataset_poco_fiable_bloquea_el_diagnostico(self):
        for index in range(20):
            self.add_event(
                ts=f"2026-10-01T12:{index:02d}:00.000Z",
                message="linea rota",
                parsed_ok=0,
                parse_error="sin patrón",
                category=None,
                fingerprint=None,
            )
        run = investigate("checkout falla", trace_dir=self.trace_dir)
        self.assertEqual(run.status, "BLOCKED")
        self.assertEqual(run.diagnosis.claims, [])
        self.assertTrue(any("fiables" in gap for gap in run.diagnosis.evidence_gaps))

    def test_el_gate_no_se_abre_sin_permiso_explicito(self):
        run = investigate("checkout falla", trace_dir=self.trace_dir)
        self.assertIsNone(run.pending_action)


class LoopWithModelTests(AgentFixture):
    def test_un_plan_del_modelo_se_valida_y_se_ejecuta(self):
        llm = NullClient(responses=[_plan_payload(), _diagnosis_payload()])
        run = investigate("checkout falla", llm=llm, trace_dir=self.trace_dir)
        self.assertEqual(run.status, "OK", run.error)
        self.assertEqual(len(run.plan.steps), 3)
        self.assertEqual(run.diagnosis.root_cause_fingerprint, "fp_pool")
        self.assertEqual(run.citations.invalid, [])
        self.assertIn("planner.v1", run.prompt_version)
        self.assertIn("synthesizer.v1", run.prompt_version)

    def test_una_cita_inventada_se_corrige_en_el_reintento(self):
        llm = NullClient(
            responses=[
                _plan_payload(),
                _diagnosis_payload("ev:9999"),
                _diagnosis_payload("fp:fp_pool"),
            ]
        )
        run = investigate("checkout falla", llm=llm, trace_dir=self.trace_dir)
        self.assertEqual(run.citations.invalid, [])
        self.assertEqual(len(llm.calls), 3)
        self.assertIn("ev:9999", llm.calls[2]["messages"][0]["content"])

    def test_si_el_reintento_tambien_alucina_se_entrega_limpio(self):
        llm = NullClient(
            responses=[
                _plan_payload(),
                _diagnosis_payload("ev:9999"),
                _diagnosis_payload("ev:8888"),
            ]
        )
        run = investigate("checkout falla", llm=llm, trace_dir=self.trace_dir)
        self.assertEqual(run.diagnosis.claims, [])
        self.assertTrue(any("evidencia inexistente" in gap for gap in run.diagnosis.evidence_gaps))

    def test_un_plan_invalido_se_reintenta_y_luego_cae_a_la_heuristica(self):
        llm = NullClient(
            responses=[
                '{"symptom": "x", "restatement": "x", "steps": []}',
                '{"symptom": "x", "restatement": "x", "steps": []}',
                _diagnosis_payload(),
            ]
        )
        run = investigate("checkout falla", llm=llm, trace_dir=self.trace_dir)
        self.assertEqual(run.status, "OK", run.error)
        self.assertTrue(
            any("plan del modelo inválido" in gap for gap in run.diagnosis.evidence_gaps)
        )

    def test_el_gate_pide_aprobacion_humana_cuando_se_autoriza(self):
        incident_id = int(
            self.conn.execute(
                "INSERT INTO incidents (fingerprint, category, severity, sample_message,"
                " first_seen, last_seen, event_count, status)"
                " VALUES ('fp_pool', 'db_pool', 45, 'pool agotado',"
                " '2026-10-01T11:00:00.000Z', '2026-10-01T11:19:00.000Z', 20, 'OPEN')"
            ).lastrowid
        )
        diagnosis = json.loads(_diagnosis_payload())
        diagnosis["proposed_incident_id"] = incident_id
        diagnosis["proposed_status"] = "ACKNOWLEDGED"
        llm = NullClient(responses=[_plan_payload(), json.dumps(diagnosis)])

        run = investigate("checkout falla", llm=llm, trace_dir=self.trace_dir, allow_mutations=True)
        self.assertEqual(run.status, "NEEDS_APPROVAL")
        self.assertEqual(run.pending_action["status"], "pending_approval")
        pending = self.conn.execute(
            "SELECT status, tool FROM agent_actions WHERE id = ?",
            (run.pending_action["pending_action"]["action_id"],),
        ).fetchone()
        self.assertEqual(pending["status"], "PENDING")
        self.assertEqual(pending["tool"], "set_incident_status")

    def test_sin_permiso_la_propuesta_queda_como_recomendacion(self):
        diagnosis = json.loads(_diagnosis_payload())
        diagnosis["proposed_incident_id"] = 1
        diagnosis["proposed_status"] = "RESOLVED"
        llm = NullClient(responses=[_plan_payload(), json.dumps(diagnosis)])
        run = investigate("checkout falla", llm=llm, trace_dir=self.trace_dir)
        self.assertEqual(run.status, "OK")
        self.assertIsNone(run.pending_action)
        self.assertTrue(any("allow_mutations" in gap for gap in run.diagnosis.evidence_gaps))


class CliTests(AgentFixture):
    def test_el_resumen_imprime_citas_huecos_y_traza(self):
        from app.agent.cli import format_run

        run = investigate("checkout falla", trace_dir=self.trace_dir)
        text = format_run(run)
        self.assertIn("checkout falla", text)
        self.assertIn("OK", text)
        self.assertIn("fp_pool", text)
        self.assertIn("[fp:fp_pool]", text)
        self.assertIn("Citas: 3 válidas, 0 inventadas", text)
        self.assertIn("Huecos de evidencia", text)
        self.assertIn(str(self.trace_dir), text)

    def test_el_modo_json_emite_el_run_completo(self):
        import io
        from contextlib import redirect_stdout

        from app.agent.cli import run as cli_run

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli_run("checkout falla", no_llm=True, traces=str(self.trace_dir), as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["diagnosis"]["root_cause_fingerprint"], "fp_pool")


class LlmHelpersTests(unittest.TestCase):
    def test_el_coste_se_estima_con_la_tabla_local(self):
        self.assertGreater(estimate_cost("claude-sonnet-4-5", 1_000_000, 0), 0)
        self.assertAlmostEqual(estimate_cost("desconocido", 1_000_000, 0), 3.0)

    def test_un_llm_sin_respuestas_falla_explicitamente(self):
        with self.assertRaises(LLMUnavailable):
            NullClient(responses=[]).complete([])

    def test_la_respuesta_lleva_el_modelo_y_los_tokens(self):
        response: LLMResponse = NullClient(responses=["hola"]).complete([], model="x")
        self.assertEqual(response.text, "hola")
        self.assertGreater(response.tokens_in, 0)


class PromptTests(unittest.TestCase):
    def test_se_cargan_los_prompts_versionados(self):
        template, label = load_rendered(
            "planner", "v1", symptom="x", health="{}", tools="[]", max_steps=8
        )
        self.assertEqual(label, "planner.v1")
        self.assertIn("x", template)

    def test_un_hueco_sin_valor_es_un_error(self):
        with self.assertRaises(PromptError):
            render("hola {{falta}}", otro="x")

    def test_un_prompt_inexistente_es_un_error(self):
        with self.assertRaises(PromptError):
            load_rendered("no-existe", "v1")


if __name__ == "__main__":
    unittest.main()
