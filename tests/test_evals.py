"""El harness de evaluación se prueba a sí mismo.

Un eval que no detecta sus propios errores no sirve, así que estos tests cubren
las three cosas que pueden invalidar una corrida:

* que el dataset commiteado sea reproducible (si no, los números no son de este
  código);
* que el ground truth exista en la base ingerida (si no, un 0 de accuracy sería
  un dataset roto disfrazado);
* que las métricas sean aritmética pura y no una opinión.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evals.dataset.build import (
    DEFAULT_PATH,
    build_cases,
    dataset_hash,
    dumps,
    load_cases,
    subset_cases,
)
from evals.dataset.generators import (
    GENERATORS,
    LOG_FORMATS,
    build_scenario,
    generate_events,
    render_logs,
)
from evals.metrics import (
    aggregate,
    case_metrics,
    check_thresholds,
    determinism_regression,
)
from evals.runner import InvalidGroundTruth, run_case


class DatasetTests(unittest.TestCase):
    def test_el_archivo_versionado_coincide_con_los_generadores(self):
        self.assertEqual(
            DEFAULT_PATH.read_text(encoding="utf-8"),
            dumps(build_cases()),
            "cases.jsonl está desactualizado: python -m evals.dataset.build",
        )

    def test_el_dataset_es_reproducible(self):
        self.assertEqual(dataset_hash(dumps(build_cases())), dataset_hash(dumps(build_cases())))

    def test_hay_casos_para_los_seis_generadores(self):
        cases = build_cases()
        self.assertEqual(len(cases), 60)
        self.assertEqual({case["generator"] for case in cases}, set(GENERATORS))

    def test_los_subsets_incluyen_el_anterior(self):
        cases = load_cases()
        self.assertEqual(len(subset_cases(cases, "smoke")), 6)
        self.assertEqual(len(subset_cases(cases, "core")), 30)
        self.assertEqual(len(subset_cases(cases, "full")), 60)

    def test_el_mismo_seed_produce_los_mismos_bytes(self):
        first = build_scenario("db_pool_exhaustion", seed=7, log_format="json", tz_offset="+00:00")
        second = build_scenario("db_pool_exhaustion", seed=7, log_format="json", tz_offset="+00:00")
        self.assertEqual(generate_events(first), generate_events(second))
        self.assertEqual(render_logs(first), render_logs(second))

    def test_el_sintoma_incluye_la_hora_del_onset(self):
        case = build_cases()[0]
        hour = case["ground_truth"]["first_bad_event_ts"][11:16]
        self.assertIn(hour, case["symptom"])

    def test_los_tres_formatos_soportados_aparecen(self):
        formats = {case["generator_params"]["log_format"] for case in build_cases()}
        self.assertEqual(formats, set(LOG_FORMATS))

    def test_el_ground_truth_usa_la_categoria_del_motor(self):
        for generator in GENERATORS:
            case = next(c for c in build_cases() if c["generator"] == generator)
            truth = case["ground_truth"]
            self.assertTrue(truth["root_cause_fingerprint"])
            self.assertTrue(truth["root_cause_category"], generator)
            self.assertEqual(
                case["assertions"]["must_cite"],
                [f"fp:{truth['root_cause_fingerprint']}"],
            )


class EngineGroundTruthTests(unittest.TestCase):
    """La huella inyectada tiene que existir en la base, formato a formato."""

    def _run(self, log_format: str) -> dict:
        case = next(
            c
            for c in build_cases()
            if c["generator_params"]["log_format"] == log_format
            and c["generator"] == "db_pool_exhaustion"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run_case(
                case,
                work_dir=Path(tmp) / "work",
                traces_dir=Path(tmp) / "traces",
            )
        self.assertGreater(result["ingest"]["root_cause_occurrences"], 0)
        self.assertTrue(result["retrieval_hit"])
        self.assertEqual(result["status"], "OK")
        return result

    def test_json(self):
        result = self._run("json")
        self.assertEqual(
            result["diagnosis"]["root_cause_fingerprint"], result["root_cause_fingerprint"]
        )

    def test_weblogic(self):
        result = self._run("weblogic")
        self.assertEqual(
            result["diagnosis"]["root_cause_fingerprint"], result["root_cause_fingerprint"]
        )

    def test_apachev2(self):
        result = self._run("apachev2")
        self.assertEqual(
            result["diagnosis"]["root_cause_fingerprint"], result["root_cause_fingerprint"]
        )

    def test_un_ground_truth_que_no_existe_falla_el_caso(self):
        case = build_cases()[0]
        case["ground_truth"]["root_cause_fingerprint"] = "0" * 16
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InvalidGroundTruth):
                run_case(
                    case,
                    work_dir=Path(tmp) / "work",
                    traces_dir=Path(tmp) / "traces",
                )


class MetricsTests(unittest.TestCase):
    def _result(self, **overrides):
        base = {
            "status": "OK",
            "root_cause_fingerprint": "abc",
            "root_cause_category": "timeout",
            "root_cause_service": "checkout",
            "onset": "2026-09-01T14:00:00Z",
            "must_not_claim": ["disk full"],
            "retrieval_hit": True,
            "tool_calls": 5,
            "cost_usd": 0.01,
            "diagnosis": {
                "root_cause_fingerprint": "abc",
                "root_cause_category": "timeout",
                "claims": [
                    {"text": "el pool se agotó", "kind": "fact"},
                    {"text": "revisar el pool", "kind": "recommendation"},
                ],
            },
            "plan": {"restatement": "checkout falla desde las 14:00"},
            "citations": {"valid": [{"kind": "fact"}], "invalid": [], "uncited": []},
        }
        base.update(overrides)
        return base

    def test_un_acierto_completo_cumple_las_metas(self):
        metrics = aggregate([self._result()])
        self.assertEqual(metrics["root_cause_accuracy"], 1.0)
        self.assertEqual(metrics["retrieval_recall_at_5"], 1.0)
        self.assertEqual(metrics["hallucination_rate"], 0.0)
        self.assertTrue(all(metrics["targets_met"].values()))

    def test_una_cita_inventada_cuenta_como_alucinacion(self):
        result = self._result(
            citations={
                "valid": [],
                "invalid": [{"kind": "fact", "text": "causa: disco"}],
                "uncited": [],
            }
        )
        metrics = aggregate([result])
        # El denominador de `hallucination_rate` son todas las claims, tal y
        # como define docs/evals.md §4.1: una recomendación también cuenta.
        self.assertEqual(metrics["hallucination_rate"], 0.5)
        self.assertEqual(metrics["evidence_citation_rate"], 0.0)
        self.assertFalse(metrics["targets_met"]["hallucination_rate"])

    def test_atribuir_algo_prohibido_es_falsa_atribucion(self):
        result = self._result(
            diagnosis={
                "root_cause_fingerprint": "abc",
                "claims": [{"text": "fue un disk full del nodo", "kind": "fact"}],
            }
        )
        self.assertTrue(case_metrics(result)["false_attribution"])
        self.assertEqual(aggregate([result])["false_attribution_rate"], 1.0)

    def test_una_recomendacion_no_cuenta_como_atribucion(self):
        result = self._result(
            diagnosis={
                "root_cause_fingerprint": "abc",
                "claims": [{"text": "comprobar disk full", "kind": "recommendation"}],
            }
        )
        self.assertFalse(case_metrics(result)["false_attribution"])

    def test_el_restatement_que_ignora_servicio_y_hora_es_entendido_mal(self):
        result = self._result(plan={"restatement": "algo va mal"})
        self.assertTrue(case_metrics(result)["misunderstood"])

    def test_umbral_incumplido_se_reporta(self):
        metrics = aggregate([self._result(root_cause_fingerprint="zzz")])
        failed = check_thresholds(metrics, {"root_cause_accuracy": 0.9})
        self.assertTrue(failed)

    def test_la_determinacion_entre_modelos_es_explicita(self):
        same = determinism_regression(
            {"retrieval_recall_at_5": 0.95}, {"retrieval_recall_at_5": 0.95}
        )
        self.assertTrue(same["identical"])
        self.assertEqual(same["delta"], 0.0)
        different = determinism_regression(
            {"retrieval_recall_at_5": 0.95}, {"retrieval_recall_at_5": 0.9}
        )
        self.assertFalse(different["identical"])


class ResultFileTests(unittest.TestCase):
    def test_el_resultado_es_json_serializable(self):
        case = build_cases()[0]
        with tempfile.TemporaryDirectory() as tmp:
            result = run_case(
                case,
                work_dir=Path(tmp) / "work",
                traces_dir=Path(tmp) / "traces",
            )
        payload = json.loads(json.dumps(result, default=str))
        self.assertEqual(payload["case_id"], case["id"])
        self.assertIn("metrics", {"metrics": aggregate([result])})


if __name__ == "__main__":
    unittest.main()
