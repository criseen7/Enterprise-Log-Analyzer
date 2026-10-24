"""Golden tests del motor extendido: `compare_windows` y `blast_radius`.

Dos capas de comprobación, a propósito:

1. **Golden**: la salida actual coincide con la última revisada. Detecta
   cambios accidentales de umbral, orden o redondeo.
2. **Semántica**: los casos declaran lo que *deberían* significar. Sin esto,
   regenerar el golden tras un bug lo convertiría en la definición del bug.

Si un cambio de comportamiento es intencional, se regenera con
`python -m tests.regenerate_golden` y se revisa el diff.
"""

from __future__ import annotations

import unittest
from typing import Any

from app.analysis.blast import DEFAULT_CORRELATION_MINUTES
from tests.analysis_golden import (
    EXPECTED_DIR,
    load_cases,
    render_blast,
    render_compare,
    run_blast,
    run_compare,
)


def _tag(case: dict[str, Any]) -> list[dict[str, Any]]:
    fingerprint = case["args"]["fingerprint"]
    return [{**event, "fingerprint": fingerprint} for event in case["events"]]


CASES = load_cases()
COMPARE = {case["name"]: run_compare({**case, "events": _tag(case)}) for case in CASES["compare"]}
BLAST = {case["name"]: run_blast({**case, "events": _tag(case)}) for case in CASES["blast"]}


class GoldenWindowsTests(unittest.TestCase):
    def test_la_salida_coincide_con_el_golden(self):
        expected = EXPECTED_DIR / "compare_windows.json"
        self.assertTrue(expected.is_file(), f"falta el golden {expected}")
        self.assertEqual(render_compare(), expected.read_text(encoding="utf-8"))


class GoldenBlastTests(unittest.TestCase):
    def test_la_salida_coincide_con_el_golden(self):
        expected = EXPECTED_DIR / "blast_radius.json"
        self.assertTrue(expected.is_file(), f"falta el golden {expected}")
        self.assertEqual(render_blast(), expected.read_text(encoding="utf-8"))


class CompareVerdictTests(unittest.TestCase):
    """Los veredictos que el motor afirma, comprobados contra la semántica."""

    def test_sin_baseline_y_con_eventos_el_veredicto_es_new(self):
        self.assertEqual(COMPARE["new_burst"]["verdict"], "new")
        self.assertEqual(COMPARE["new_burst"]["baseline"]["count"], 0)
        self.assertIsNone(COMPARE["new_burst"]["ratio"])

    def test_mismo_volumen_es_stable(self):
        self.assertEqual(COMPARE["stable"]["verdict"], "stable")
        self.assertEqual(COMPARE["stable"]["ratio"], 1.0)

    def test_caida_a_menos_de_la_mitad_es_decreased(self):
        self.assertEqual(COMPARE["decreased"]["verdict"], "decreased")
        self.assertLess(COMPARE["decreased"]["ratio"], 0.5)

    def test_baseline_plano_usa_el_ratio_y_deja_el_z_score_nulo(self):
        """Un baseline sin desviación no tiene z-score: `inf` no es JSON válido."""
        case = COMPARE["elevated_flat_baseline"]
        self.assertEqual(case["baseline"]["stddev"], 0.0)
        self.assertIsNone(case["z_score"])
        self.assertEqual(case["verdict"], "elevated")

    def test_con_dispersion_se_exige_el_z_score(self):
        case = COMPARE["elevated_z_score"]
        self.assertIsNotNone(case["z_score"])
        self.assertGreaterEqual(case["z_score"], 3.0)
        self.assertEqual(case["verdict"], "elevated")

    def test_ratio_alto_con_z_score_bajo_no_es_elevated(self):
        """Un baseline con mucha varianza produce ratios altos sin pico real."""
        case = COMPARE["stable_high_ratio_high_variance"]
        self.assertGreaterEqual(case["ratio"], 3.0)
        self.assertLess(case["z_score"], 3.0)
        self.assertEqual(case["verdict"], "stable")

    def test_un_baseline_de_24_horas_da_confianza_alta(self):
        case = COMPARE["high_confidence"]
        self.assertGreaterEqual(case["baseline"]["hours"], 24)
        self.assertEqual(case["confidence"], "high")

    def test_sin_baseline_explicito_usa_la_ventana_anterior(self):
        case = COMPARE["auto_baseline"]
        self.assertEqual(case["baseline"]["since"], "2026-10-09T09:00:00.000Z")
        self.assertEqual(case["baseline"]["until"], "2026-10-09T10:00:00.000Z")

    def test_una_ventana_sin_extremos_no_es_medible(self):
        self.assertIsNone(COMPARE["not_measurable"])

    def test_todos_los_casos_se_ejecutaron(self):
        self.assertEqual(len(COMPARE), len(CASES["compare"]))
        self.assertNotIn(None, COMPARE)


class BlastSemanticsTests(unittest.TestCase):
    def test_un_solo_servicio_nunca_es_cascada(self):
        case = BLAST["single_service"]
        self.assertEqual(case["distinct_services"], 1)
        self.assertFalse(case["is_cascade"])
        self.assertIn("sólo aparece en un servicio", case["cascade_note"])

    def test_ventanas_solapadas_son_cascada(self):
        case = BLAST["cascade_overlap"]
        self.assertTrue(case["is_cascade"])
        self.assertIn("0 min", case["cascade_note"])

    def test_hueco_dentro_de_la_ventana_es_cascada_y_se_cita(self):
        case = BLAST["cascade_gap"]
        self.assertTrue(case["is_cascade"])
        self.assertIn("8 min", case["cascade_note"])

    def test_hueco_fuera_de_la_ventana_no_es_cascada(self):
        case = BLAST["no_cascade_gap"]
        self.assertFalse(case["is_cascade"])
        self.assertIn(str(DEFAULT_CORRELATION_MINUTES), case["cascade_note"])

    def test_la_ventana_de_correlacion_es_configurable(self):
        case = BLAST["wide_correlation_window"]
        self.assertEqual(case["correlation_window_minutes"], 60)
        self.assertTrue(case["is_cascade"])

    def test_la_linea_de_tiempo_solo_existe_si_hay_ventana(self):
        self.assertFalse(BLAST["no_bounds"]["timeline"])
        self.assertTrue(BLAST["cascade_gap"]["timeline"])
        self.assertEqual(
            BLAST["cascade_gap"]["timeline"][0]["bucket_start"], "2026-11-03T10:00:00.000Z"
        )

    def test_sin_eventos_el_resultado_es_nulo_no_cero(self):
        self.assertIsNone(BLAST["no_events"])
