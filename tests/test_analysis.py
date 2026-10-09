"""Fase 3: clasificación, fingerprint, métricas, recurrencia y anomalías.

Dos de las pruebas de aceptación de la fase están aquí y no son accesorios:

* "errores por hora" debe coincidir con un conteo **escrito a mano** sobre un
  fixture — si el número sale de la misma query que produce el gráfico, el test
  no comprueba nada.
* 100 accesos fallidos idénticos salvo el código deben dar **1** fingerprint.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.analysis.anomalies import anomalies, hourly_counts
from app.analysis.backfill import backfill
from app.analysis.errors import ErrorRules, load_rules
from app.analysis.fingerprint import event_fingerprint, normalize_message
from app.analysis.metrics import by_category, errors_by_hour, overview, top_errors
from app.analysis.patterns import recurring_patterns
from app.config import ConfigError
from app.ingest.ingestor import ingest_file
from app.parser.registry import Registry
from app.parser.severity import ERROR, WARN
from tests.helpers import FIXTURES_DIR, PATTERNS_DIR, TempDbTestCase

STAMP = "2026-10-05T12:00:00.000Z"

# Contado a mano sobre tests/fixtures/analysis/errors_by_hour.log (8 líneas):
#   09:xx -> connection refused (ERROR) + failed login (ERROR) = 2
#   10:xx -> NullPointerException (ERROR) + timeout de consulta (ERROR) = 2
#   11:xx -> Pool exhausted (FATAL) = 1
# Los otros tres son WARN, WARN e INFO y no entran con severidad >= ERROR.
ESPERADO_ERRORES_POR_HORA = {
    "2026-10-05T09:00": 2,
    "2026-10-05T10:00": 2,
    "2026-10-05T11:00": 1,
}

# Con WARN (>= 40) sí entran los dos "reintento": 3 en 09, 3 en 10, 1 en 11.
ESPERADO_ERRORES_POR_HORA_WARN = {
    "2026-10-05T09:00": 3,
    "2026-10-05T10:00": 3,
    "2026-10-05T11:00": 1,
}


class FingerprintTests(unittest.TestCase):
    def test_lo_que_varia_se_sustituye(self):
        self.assertEqual(
            normalize_message(
                "user 1234 from 10.0.0.5 id a1b2c3d4e5f6a7b8 at 2026-10-05T14:03:22.481Z"
            ),
            "user <NUM> from <IP> id <HEX> at <TS>",
        )

    def test_uuid_y_version_tambien(self):
        self.assertEqual(
            normalize_message("clave 550e8400-e29b-41d4-a716-446655440000 v2"),
            "clave <UUID> v<NUM>",
        )

    def test_numeros_sin_limites_de_palabra(self):
        """`u123` o `30000ms` no llevan frontera de palabra: se igualan igual."""
        self.assertEqual(normalize_message("timeout after 30000ms"), "timeout after <NUM>ms")
        self.assertEqual(normalize_message("user u123 failed"), "user u<NUM> failed")
        self.assertEqual(normalize_message("order_9871 failed"), "order_<NUM> failed")

    def test_no_normaliza_el_caso(self):
        """Decisión: no se unen mensajes con distinto caso (ver ADR-004)."""
        self.assertNotEqual(normalize_message("Connection refused"), normalize_message("connection refused"))

    def test_mensaje_vacio_y_nulo_comparten_huella(self):
        self.assertEqual(event_fingerprint(None, ""), event_fingerprint(None, None))

    def test_categoria_forma_parte_de_la_huella(self):
        """El mismo texto en categorías distintas es dos problemas distintos."""
        self.assertNotEqual(
            event_fingerprint("auth", "rollback"),
            event_fingerprint("transaction", "rollback"),
        )

    def test_cien_accesos_fallidos_identicos_salvo_el_codigo_dan_una_huella(self):
        """Aceptación de la fase: 100 accesos fallidos -> 1 fingerprint."""
        rules = ErrorRules.default()
        huellas = set()
        for code in range(100):
            mensaje = (
                f"Failed login for user admin from 10.0.{code // 250}.{code % 250} "
                f"(code={code}, attempt={code})"
            )
            huellas.add(event_fingerprint(rules.category(mensaje), mensaje))
        self.assertEqual(len(huellas), 1, f"los 100 accesos deberían dar 1 huella, dieron {len(huellas)}")


class ErrorRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ela-rules-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, text: str) -> Path:
        path = self.dir / "rules.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_las_reglas_empaquetadas_cargan(self):
        rules = ErrorRules.default()
        self.assertGreater(len(rules), 0)
        self.assertIn("timeout", rules.categories)

    def test_primera_coincidencia_gana(self):
        rules = load_rules(
            self.write(
                "rules:\n"
                "  - id: generica\n    category: otra\n    patterns: ['fallo']\n"
                "  - id: concreta\n    category: timeout\n    patterns: ['fallo de timeout']\n"
            )
        )
        self.assertEqual(rules.category("fallo de timeout"), "otra", "el orden es la prioridad")

    def test_insensible_a_mayusculas(self):
        rules = ErrorRules.default()
        self.assertEqual(rules.category("FAILED LOGIN ATTEMPT"), "auth")

    def test_sin_coincidencia_da_none(self):
        self.assertIsNone(ErrorRules.default().category("todo va bien"))

    def test_regex_invalido_falla_al_cargar(self):
        path = self.write("rules:\n  - id: mala\n    category: x\n    patterns: ['[']\n")
        with self.assertRaises(ConfigError) as ctx:
            load_rules(path)
        self.assertIn("regex inválido", str(ctx.exception))
        self.assertIn("mala", str(ctx.exception))

    def test_falta_category(self):
        path = self.write("rules:\n  - id: sin_cat\n    patterns: ['x']\n")
        with self.assertRaises(ConfigError) as ctx:
            load_rules(path)
        self.assertIn("category", str(ctx.exception))

    def test_clave_desconocida(self):
        path = self.write(
            "rules:\n  - id: r\n    category: x\n    patterns: ['x']\n    severidad: 40\n"
        )
        with self.assertRaises(ConfigError) as ctx:
            load_rules(path)
        self.assertIn("desconocidas", str(ctx.exception))

    def test_patrones_vacios(self):
        path = self.write("rules:\n  - id: r\n    category: x\n    patterns: []\n")
        with self.assertRaises(ConfigError):
            load_rules(path)

    def test_ids_duplicados(self):
        path = self.write(
            "rules:\n"
            "  - id: r\n    category: a\n    patterns: ['x']\n"
            "  - id: r\n    category: b\n    patterns: ['y']\n"
        )
        with self.assertRaises(ConfigError) as ctx:
            load_rules(path)
        self.assertIn("duplicados", str(ctx.exception))

    def test_archivo_inexistente(self):
        with self.assertRaises(ConfigError):
            load_rules(self.dir / "no-existe.yaml")

    def test_raiz_no_es_mapeo(self):
        with self.assertRaises(ConfigError):
            load_rules(self.write("- id: r\n"))

    def test_reglas_vacias(self):
        with self.assertRaises(ConfigError):
            load_rules(self.write("rules: []\n"))


class AnalysisDbTest(TempDbTestCase):
    """Base: registry + reglas + una ingesta de fixture."""

    def setUp(self) -> None:
        super().setUp()
        self.registry = Registry.from_directory(PATTERNS_DIR)
        self.rules = ErrorRules.default()
        self.conn = self.connect()
        self._offset = 0

    def ingest_fixture(self, name: str):
        lines = (FIXTURES_DIR / name).read_text(encoding="utf-8").splitlines()
        path = self.write_log("app.log", lines)
        return ingest_file(self.conn, path, self.registry, rules=self.rules)

    def seed_file(self) -> int:
        return self.conn.execute(
            "INSERT INTO log_files (path, first_seen, last_seen) VALUES (?, ?, ?)",
            ("logs/seed.log", STAMP, STAMP),
        ).lastrowid

    def add_event(
        self,
        file_id: int,
        timestamp: str,
        *,
        severity: int = ERROR,
        message: str = "algo fue",
        category: str | None = None,
        fingerprint: str | None = None,
        parsed_ok: int = 1,
    ) -> int:
        self._offset += 1
        self.conn.execute(
            "INSERT INTO events (file_id, offset, timestamp_utc, severity, level_raw, message,"
            " raw_line, parsed_ok, category, fingerprint)"
            " VALUES (?, ?, ?, ?, 'ERROR', ?, ?, ?, ?, ?)",
            (
                file_id,
                self._offset,
                timestamp,
                severity,
                message,
                message,
                parsed_ok,
                category,
                fingerprint,
            ),
        )
        return self._offset


class IngestClassificationTest(AnalysisDbTest):
    def test_la_ingesta_asigna_categoria_y_huella(self):
        self.ingest_fixture("analysis/errors_by_hour.log")
        filas = self.conn.execute(
            "SELECT message, category, fingerprint FROM events ORDER BY offset"
        ).fetchall()
        self.assertTrue(all(r["fingerprint"] for r in filas), "toda fila debe tener huella")
        categorias = {r["message"]: r["category"] for r in filas}
        self.assertEqual(categorias["timeout de consulta"], "timeout")
        self.assertEqual(
            categorias["connection refused to 10.0.0.1"], "connection"
        )
        self.assertIsNone(categorias["pedido creado"], "un INFO sin regla no inventa categoría")

    def test_dos_ingestas_no_cambian_la_clasificacion(self):
        self.ingest_fixture("analysis/errors_by_hour.log")
        primero = [tuple(r) for r in self.conn.execute(
            "SELECT offset, category, fingerprint FROM events ORDER BY offset"
        )]
        self.ingest_fixture("analysis/errors_by_hour.log")
        segundo = [tuple(r) for r in self.conn.execute(
            "SELECT offset, category, fingerprint FROM events ORDER BY offset"
        )]
        self.assertEqual(primero, segundo)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 8)


class MetricsTest(AnalysisDbTest):
    def test_errores_por_hora_coincide_con_el_conteo_manual(self):
        """Aceptación: el número debe coincidir con el conteo hecho a mano."""
        self.ingest_fixture("analysis/errors_by_hour.log")
        self.assertEqual(errors_by_hour(self.conn), ESPERADO_ERRORES_POR_HORA)
        self.assertEqual(
            errors_by_hour(self.conn, min_severity=WARN), ESPERADO_ERRORES_POR_HORA_WARN
        )

    def test_rango_abierto_a_la_derecha(self):
        """Hasta `until` sin incluirlo: 09:00..10:30 son las horas 09 y 10."""
        self.ingest_fixture("analysis/errors_by_hour.log")
        obtenido = errors_by_hour(
            self.conn, since="2026-10-05T09:00:00Z", until="2026-10-05T10:30:00Z"
        )
        self.assertEqual(obtenido, {"2026-10-05T09:00": 2, "2026-10-05T10:00": 1})

    def test_las_dos_lineas_de_reintento_comparten_fila(self):
        """Dos reintento con distinto número son el mismo error agrupado."""
        self.ingest_fixture("analysis/errors_by_hour.log")
        filas = top_errors(self.conn, min_severity=WARN, min_count=2)
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].count, 2)
        self.assertEqual(filas[0].sample_message, "reintento 3 de 5")

    def test_overview(self):
        self.ingest_fixture("analysis/errors_by_hour.log")
        stats = overview(self.conn)
        self.assertEqual(stats.events, 8)
        self.assertEqual(stats.parsed, 8)
        self.assertEqual(stats.unparsed, 0)
        self.assertEqual(stats.errors, 5)
        # 8 eventos, los dos "reintento" comparten huella -> 7 distintas.
        self.assertEqual(stats.fingerprints, 7)
        self.assertEqual(stats.coverage, 1.0)
        self.assertEqual(stats.first_ts, "2026-10-05T09:05:00Z")

    def test_por_categoria_agrupa_y_respeta_la_severidad(self):
        self.ingest_fixture("analysis/errors_by_hour.log")
        categorias = {row.category: row.count for row in by_category(self.conn)}
        self.assertEqual(categorias["connection"], 1)
        self.assertEqual(categorias["auth"], 1)
        self.assertEqual(categorias["timeout"], 1)
        self.assertEqual(categorias["resource-exhausted"], 1)
        self.assertNotIn(None, categorias, "los INFO/WARN no deberían entrar con min_severity=ERROR")


class PatternsTest(AnalysisDbTest):
    def test_la_ventana_usa_el_ultimo_evento_no_el_reloj(self):
        """Un dataset histórico no puede devolver una ventana vacía por defecto."""
        file_id = self.seed_file()
        for i in range(4):
            self.add_event(
                file_id,
                f"2026-10-05T10:0{i}:00Z",
                message="timeout de consulta",
                category="timeout",
                fingerprint="fp-timeout",
            )
        filas = recurring_patterns(self.conn, window_minutes=15, min_count=2, min_severity=ERROR)
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].count, 4)
        self.assertEqual(filas[0].category, "timeout")
        self.assertEqual(filas[0].share, 1.0)

    def test_la_ventana_no_alcanza_lo_anticuado(self):
        file_id = self.seed_file()
        self.add_event(
            file_id, "2026-10-05T10:00:00Z", fingerprint="fp-viejo", message="viejo"
        )
        self.add_event(
            file_id, "2026-10-05T12:00:00Z", fingerprint="fp-nuevo", message="nuevo"
        )
        filas = recurring_patterns(self.conn, window_minutes=30, min_count=1, min_severity=ERROR)
        self.assertEqual([f.fingerprint for f in filas], ["fp-nuevo"])

    def test_min_count_filtra_lo_que_no_se_repite(self):
        file_id = self.seed_file()
        self.add_event(file_id, "2026-10-05T10:00:00Z", fingerprint="fp-unico")
        self.assertEqual(
            recurring_patterns(self.conn, window_minutes=60, min_count=2, min_severity=ERROR),
            [],
        )

    def test_ventana_invalida(self):
        with self.assertRaises(ValueError):
            recurring_patterns(self.conn, window_minutes=0)


class AnomaliesTest(AnalysisDbTest):
    def seed_series(self, counts: list[int], *, start: str = "2026-10-05") -> None:
        """Una fila por hora, con `counts[i]` eventos en la hora i."""
        file_id = self.seed_file()
        for hour, n in enumerate(counts):
            for k in range(n):
                self.add_event(
                    file_id,
                    f"{start}T{hour:02d}:{min(k, 59):02d}:00Z",
                    message=f"evento {hour}-{k}",
                )

    def test_pico_detectado(self):
        self.seed_series([3, 4, 3, 5, 12])
        encontradas = anomalies(self.conn, z_threshold=3.0, min_samples=5)
        self.assertEqual([a.bucket for a in encontradas], ["2026-10-05T04:00"])
        self.assertGreaterEqual(encontradas[0].zscore, 3.0)
        self.assertEqual(encontradas[0].count, 12)

    def test_serie_plana_no_produce_anomalias(self):
        self.seed_series([4, 4, 4, 4, 4])
        self.assertEqual(anomalies(self.conn, z_threshold=3.0, min_samples=5), [])

    def test_serie_demasiado_corta_se_descarta(self):
        self.seed_series([4, 20])
        self.assertEqual(anomalies(self.conn, z_threshold=3.0, min_samples=5), [])

    def test_el_pico_chico_no_cuenta(self):
        """`min_samples` es mínimo de eventos por bucket, no de buckets."""
        self.seed_series([1, 1, 1, 1, 4])
        self.assertEqual(anomalies(self.conn, z_threshold=3.0, min_samples=5), [])
        encontradas = anomalies(self.conn, z_threshold=3.0, min_samples=4)
        self.assertEqual([a.bucket for a in encontradas], ["2026-10-05T04:00"])

    def test_hasta_el_ultimo_digito(self):
        """Z exacta, leave-one-out: media 4.25 y desviación de los otros cuatro."""
        self.seed_series([3, 4, 3, 5, 12])
        (hallazgo,) = anomalies(self.conn, z_threshold=3.0, min_samples=5)
        self.assertAlmostEqual(hallazgo.mean, 3.75)
        self.assertAlmostEqual(hallazgo.stdev, (2.75 / 4) ** 0.5)
        self.assertAlmostEqual(hallazgo.zscore, (12 - 3.75) / (2.75 / 4) ** 0.5)

    def test_until_exclusivo_no_aniade_una_hora_vacia(self):
        self.seed_series([2, 2])
        serie = hourly_counts(
            self.conn,
            since="2026-10-05T00:00:00Z",
            until="2026-10-05T02:00:00Z",
        )
        self.assertEqual([p.bucket for p in serie], ["2026-10-05T00:00", "2026-10-05T01:00"])
        self.assertEqual([p.count for p in serie], [2, 2])

    def test_serie_rellena_las_horas_vacias(self):
        self.seed_series([2, 0, 3])
        serie = hourly_counts(self.conn, since="2026-10-05T00:00:00Z", until="2026-10-05T03:00:00Z")
        self.assertEqual([p.count for p in serie], [2, 0, 3])


class BackfillTest(AnalysisDbTest):
    def test_rellena_solo_lo_que_falta(self):
        file_id = self.seed_file()
        self.add_event(file_id, "2026-10-05T10:00:00Z", message="timeout de consulta")
        self.add_event(file_id, "2026-10-05T10:01:00Z", message="pedido creado")

        self.assertEqual(backfill(self.conn, self.rules), 2)
        self.assertEqual(backfill(self.conn, self.rules), 0, "la 2ª pasada no debe tocar nada")

        filas = self.conn.execute(
            "SELECT message, category, fingerprint FROM events ORDER BY offset"
        ).fetchall()
        self.assertEqual(filas[0]["category"], "timeout")
        self.assertIsNone(filas[1]["category"])
        self.assertTrue(all(r["fingerprint"] for r in filas))

    def test_recalc_reescribe_lo_ya_clasificado(self):
        file_id = self.seed_file()
        self.add_event(
            file_id,
            "2026-10-05T10:00:00Z",
            message="timeout de consulta",
            fingerprint="huella-vieja",
        )

        self.assertEqual(backfill(self.conn, self.rules), 0, "sin --recalc nada se toca")
        self.assertEqual(backfill(self.conn, self.rules, recalc=True), 1)

        huella = self.conn.execute("SELECT fingerprint FROM events").fetchone()[0]
        self.assertNotEqual(huella, "huella-vieja")
        self.assertEqual(self.conn.execute("SELECT category FROM events").fetchone()[0], "timeout")

    def test_batch_size_invalido(self):
        with self.assertRaises(ValueError):
            backfill(self.conn, self.rules, batch_size=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
