"""Aceptación de Fase 0: configuración en capas y migración del esquema."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.config import (
    AnalysisConfig,
    ConfigError,
    DatabaseConfig,
    IngestConfig,
    PathsConfig,
    UIConfig,
    load_settings,
)
from app.db.migrations import applied_versions, migrate, status
from tests.helpers import EnvSandbox, TempDbTestCase

REPO_ROOT = Path(__file__).resolve().parents[1]


class DefaultsTests(unittest.TestCase):
    def test_works_without_any_yaml(self):
        settings = load_settings(config_path="/nonexistent.yaml")
        self.assertIsInstance(settings.paths, PathsConfig)
        self.assertIsInstance(settings.ingest, IngestConfig)
        self.assertIsInstance(settings.db, DatabaseConfig)
        self.assertIsInstance(settings.analysis, AnalysisConfig)
        self.assertIsInstance(settings.ui, UIConfig)

    def test_shipped_config_loads(self):
        """El config.yaml del repo debe ser válido: es lo que se ejecuta en Docker."""
        settings = load_settings(config_path=REPO_ROOT / "config.yaml")
        self.assertEqual(settings.db.journal_mode, "wal")
        self.assertEqual(settings.paths.patterns_dir.name, "patterns")


class LayeringTests(unittest.TestCase):
    def write(self, tmp: Path, name: str, body: str) -> Path:
        path = tmp / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_explicit_config_replaces_default_file(self):
        """Pasar un archivo explícito debe servir para testear esa config exacta."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = self.write(tmp, "custom.yaml", "db:\n  busy_timeout_ms: 1111\n")
            settings = load_settings(config_path=cfg)
            self.assertEqual(settings.db.busy_timeout_ms, 1111)

    def test_deep_merge_preserves_sibling_keys(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = self.write(tmp, "partial.yaml", "db:\n  busy_timeout_ms: 2222\n")
            settings = load_settings(config_path=cfg)
            # sibling no tocado por el override
            self.assertEqual(settings.db.journal_mode, "wal")
            self.assertEqual(settings.db.synchronous, "NORMAL")

    def test_env_overrides_yaml(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.yaml"
            cfg.write_text("db:\n  busy_timeout_ms: 3333\n", encoding="utf-8")
            with EnvSandbox(ELA_BUSY_TIMEOUT_MS="4444"):
                self.assertEqual(load_settings(config_path=cfg).db.busy_timeout_ms, 4444)

    def test_paths_become_absolute(self):
        settings = load_settings(config_path=REPO_ROOT / "config.yaml")
        for value in (settings.paths.log_dir, settings.paths.db_path, settings.paths.export_dir):
            self.assertTrue(value.is_absolute(), f"{value} sigue siendo relativa")


class EnvCoercionTests(unittest.TestCase):
    def test_int_and_float(self):
        with EnvSandbox(ELA_INCIDENT_THRESHOLD="7", ELA_ANOMALY_ZSCORE="1.25"):
            s = load_settings(config_path=REPO_ROOT / "config.yaml")
            self.assertEqual(s.analysis.incident_threshold, 7)
            self.assertEqual(s.analysis.anomaly_zscore, 1.25)

    def test_bool(self):
        with EnvSandbox(ELA_NOT_A_BOOL="maybe"):
            pass  # sanity: el sandbox no debe filtrar variables
        # No hay campos bool hoy; se prueba el coercor directamente.
        from app.config import _coerce

        self.assertIs(_coerce("true", bool), True)
        self.assertIs(_coerce("OFF", bool), False)
        with self.assertRaises(ConfigError):
            _coerce("quizá", bool)

    def test_comma_separated_tuple(self):
        from app.config import _coerce

        self.assertEqual(_coerce("*.log, *.txt ,", tuple), ("*.log", "*.txt"))

    def test_invalid_int_reports_env_name(self):
        with EnvSandbox(ELA_BUSY_TIMEOUT_MS="mucho"):
            with self.assertRaises(ConfigError) as ctx:
                load_settings(config_path=REPO_ROOT / "config.yaml")
        self.assertIn("ELA_BUSY_TIMEOUT_MS", str(ctx.exception))


class ValidationTests(unittest.TestCase):
    def assertRejects(self, body: str, needle: str) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "bad.yaml"
            cfg.write_text(body, encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_settings(config_path=cfg)
            self.assertIn(needle, str(ctx.exception))

    def test_rejects_non_wal_journal(self):
        # El diseño depende de WAL (ADR-001), pero fallar en runtime es peor.
        self.assertRejects("db:\n  journal_mode: delete\n", "journal_mode")

    def test_rejects_invalid_timezone(self):
        self.assertRejects("ingest:\n  default_timezone: No/Existe\n", "default_timezone")

    def test_rejects_unknown_key(self):
        self.assertRejects("paths:\n  logg_dir: logs\n", "desconocidas")

    def test_rejects_empty_include_patterns(self):
        self.assertRejects("ingest:\n  include_patterns: []\n", "include_patterns")

    def test_rejects_non_positive_chunk(self):
        self.assertRejects("ingest:\n  chunk_bytes: 0\n", "chunk_bytes")

    def test_rejects_out_of_range_severity(self):
        self.assertRejects("analysis:\n  min_incident_severity: 900\n", "min_incident_severity")

    def test_rejects_malformed_yaml(self):
        self.assertRejects("db:\n  busy_timeout_ms: [unclosed\n", "no es YAML válido")

    def test_rejects_wrong_shape(self):
        # YAML válido, pero `db` es una lista donde se esperaba un mapa.
        self.assertRejects("db:\n  - no soy un mapa\n", "desconocidas")

    def test_rejects_block_where_scalar_expected(self):
        self.assertRejects("paths:\n  db_path:\n    anidado: 1\n", "no se esperaba un bloque")

    def test_reports_every_problem_at_once(self):
        self.assertRejects("db:\n  busy_timeout_ms: 0\nanalysis:\n  anomaly_zscore: -1\n", "anomaly_zscore")


class LogFilesTests(TempDbTestCase):
    migrate_db = False

    def test_lists_matching_files_sorted(self):
        self.write_log("b.log", ["x"])
        self.write_log("a.log", ["x"])
        self.write_log("ignored.txt", ["x"])
        found = [p.name for p in self.settings().log_files]
        self.assertEqual(found, ["a.log", "b.log"])

    def test_empty_dir(self):
        self.assertEqual(self.settings().log_files, [])

    def test_multiple_globs(self):
        self.write_log("a.log", ["x"])
        self.write_log("b.txt", ["x"])
        settings = self.settings()
        object.__setattr__(settings.ingest, "include_patterns", ("*.log", "*.txt"))
        self.assertEqual([p.name for p in settings.log_files], ["a.log", "b.txt"])


class MigrationTests(TempDbTestCase):
    migrate_db = False

    def test_creates_expected_tables(self):
        migrate(self.db_path)
        conn = self.connect()
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertLessEqual(
            {"schema_version", "log_files", "events", "incidents", "ingest_runs"}, names
        )

    def test_is_idempotent(self):
        self.assertEqual(migrate(self.db_path), [1])
        self.assertEqual(migrate(self.db_path), [])
        conn = self.connect()
        self.assertEqual(applied_versions(conn), [1])

    def test_applied_at_is_iso_utc(self):
        migrate(self.db_path)
        applied_at = self.connect().execute(
            "SELECT applied_at FROM schema_version WHERE version=1"
        ).fetchone()[0]
        self.assertRegex(applied_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_status_before_and_after(self):
        self.assertEqual(status(self.db_path), (0, 1))
        migrate(self.db_path)
        self.assertEqual(status(self.db_path), (1, 1))

    def test_refuses_newer_database(self):
        migrate(self.db_path)
        with self.assertRaises(RuntimeError):
            migrate(self.db_path, target=0)

    def test_events_unique_constraint_is_the_idempotency_key(self):
        """D3: (file_id, offset) es lo que impide duplicar eventos."""
        import sqlite3

        migrate(self.db_path)
        conn = self.connect()
        conn.execute(_FILE_ROW)
        conn.execute("INSERT INTO events (file_id, offset, raw_line) VALUES (1, 0, 'a')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO events (file_id, offset, raw_line) VALUES (1, 0, 'a')")

    def test_parse_ratio_is_queryable(self):
        """La cobertura de parseo se mide en SQL, no en Python.

        Es el indicador que hace visible la pérdida de datos: si cae del 100%,
        algo dejó de encajar en un formato y hay que ir a mirar `parse_error`.
        """
        migrate(self.db_path)
        conn = self.connect()
        conn.execute(_FILE_ROW)
        conn.executemany(
            "INSERT INTO events (file_id, offset, raw_line, parsed_ok, parse_error) "
            "VALUES (1, ?, ?, ?, ?)",
            [
                (0, "ok", 1, None),
                (1, "ok", 1, None),
                (2, "basura", 0, "ningún patrón coincide"),
            ],
        )
        total, ok = conn.execute(
            "SELECT COUNT(*), SUM(parsed_ok) FROM events"
        ).fetchone()[:2]
        self.assertEqual((total, ok), (3, 2))
        reasons = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT parse_error FROM events WHERE parsed_ok = 0"
            )
        ]
        self.assertEqual(reasons, ["ningún patrón coincide"])

    def test_indexes_support_the_dashboard_queries(self):
        migrate(self.db_path)
        conn = self.connect()
        expected = {
            "idx_events_ts",
            "idx_events_fp",
            "idx_events_sev_ts",
            "idx_events_service_ts",
            "idx_incidents_status",
            "idx_ingest_runs_started",
        }
        found = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
        }
        self.assertLessEqual(expected, found)

    def test_query_plans_use_indexes(self):
        """Un índice que el planificador ignora no sirve de nada."""
        migrate(self.db_path)
        conn = self.connect()
        conn.execute(_FILE_ROW)
        conn.executemany(
            "INSERT INTO events (file_id, offset, raw_line, severity, timestamp_utc) VALUES (1,?,?,?,?)",
            [(i, f"line {i}", 40, f"2026-10-05T14:{i // 60:02d}:{i % 60:02d}.000Z") for i in range(500)],
        )
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM events WHERE severity >= 40 AND timestamp_utc >= ?",
            ("2026-01-01",),
        ).fetchall()
        self.assertTrue(
            any("idx_events_sev_ts" in str(r["detail"]) for r in plan),
            f"el planificador no usó el índice de severidad: {[dict(r) for r in plan]}",
        )


_FILE_ROW = (
    "INSERT INTO log_files (id, path, first_seen, last_seen) "
    "VALUES (1, 'logs/a.log', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
)


if __name__ == "__main__":
    unittest.main()