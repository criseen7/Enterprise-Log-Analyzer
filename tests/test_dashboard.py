"""Fase 4: humo del dashboard con `streamlit.testing.v1.AppTest`.

Estos tests se **saltan** si Streamlit no está instalado — el intérprete del
sistema no lo tiene, el `.venv` sí. La suite de `app/ui/data.py` no se salta
nunca: el SQL se prueba sin Streamlit.

Qué se comprueba aquí y por qué:

* Que las cuatro páginas se renderizan sin excepción con un dataset real.
* Que el auto-seed funciona: base vacía + logs en disco → eventos en la tabla,
  sin pasar por la CLI.
* Que el interruptor "sólo líneas sin parsear" devuelve la línea basura. Ese
  caso llegó a devolver una tabla vacía por culpa del filtro de severidad: las
  líneas sin parsear son UNKNOWN (0) y el filtro por defecto es ≥ 45.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import EnvSandbox, REPO_ROOT

HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None

SAMPLE_LOG = "".join(
    line + "\n"
    for line in [
        '10.0.0.7 - - [05/Oct/2026:14:03:22 -0600] "GET /api/orders HTTP/1.1" 500 1284 "-" "curl/8.5.0"',
        '{"timestamp":"2026-10-05T20:03:22Z","level":"ERROR","message":"pago rechazado","service":"billing"}',
        '{"timestamp":"2026-10-05T20:04:22Z","level":"INFO","message":"petición servida","service":"orders"}',
        "[runtime] línea suelta que ningún patrón reconoce",
        "05/oct/2026 14:05:22.481 CEST weblogic.database.ConnectionPool   : [pool-1] SEVERE | CONNECTION POOL EXHAUSTED",
        "\tat weblogic.jdbc.wrapper.PoolConnection.checkConnection(PoolConnection.java:120)",
    ]
)


def _page_script(module: str) -> str:
    return (
        "from app.ui.data import EventFilters\n"
        f"from app.ui.pages import {module}\n"
        f"{module}.render(EventFilters())\n"
    )


@unittest.skipUnless(HAS_STREAMLIT, "streamlit no instalado (correr con .venv/bin/python)")
class DashboardSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        import app.config as config
        from app.ui import cache

        self._tmp = TemporaryDirectory(prefix="ela-ui-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.log_dir = root / "logs"
        self.log_dir.mkdir()
        self.db_path = root / "data" / "ui.db"

        self._sandbox = EnvSandbox(
            ELA_DB_PATH=str(self.db_path), ELA_LOG_DIR=str(self.log_dir)
        )
        self._sandbox.__enter__()
        self.addCleanup(self._sandbox.__exit__, None, None, None)

        # `get_settings()` cachea a nivel de módulo y AppTest corre en el mismo
        # proceso: sin invalidar, el dashboard leería la base del repo.
        self.config = config
        config._cached = None
        self.cache = cache
        cache.clear_caches()

    def tearDown(self) -> None:
        self.config._cached = None
        self.cache.clear_caches()

    # -- utilidades ---------------------------------------------------------

    def write_logs(self) -> None:
        (self.log_dir / "app.log").write_text(SAMPLE_LOG, encoding="utf-8")

    def ingest(self) -> None:
        from app.analysis.errors import ErrorRules
        from app.config import get_settings
        from app.db.connection import connection
        from app.db.migrations import migrate
        from app.ingest.ingestor import discover, ingest_paths_transactional
        from app.parser.registry import Registry

        settings = get_settings()
        migrate(settings.paths.db_path)
        registry = Registry.from_directory(settings.paths.patterns_dir)
        rules = ErrorRules.load(settings.analysis.rules_path)
        with connection() as conn:
            ingest_paths_transactional(
                conn, registry, discover(settings), settings.ingest.chunk_bytes, rules
            )

    def count_events(self) -> int:
        from app.db.connection import connection

        with connection(read_only=True) as conn:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def assert_no_exception(self, at) -> None:
        self.assertFalse(
            at.exception, [f"{exc.type}: {exc.value}" for exc in at.exception]
        )

    # -- tests --------------------------------------------------------------

    def test_la_app_arranca_y_siembra_sola(self):
        from streamlit.testing.v1 import AppTest

        self.write_logs()
        at = AppTest.from_file(str(REPO_ROOT / "app" / "ui" / "app.py"), default_timeout=30)
        at.run()

        self.assert_no_exception(at)
        self.assertTrue(any(t.value == "Overview" for t in at.title))
        self.assertGreater(self.count_events(), 0, "el auto-seed no ingirió nada")

    def test_app_sin_logs_muestra_estado_vacio(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(REPO_ROOT / "app" / "ui" / "app.py"), default_timeout=30)
        at.run()

        self.assert_no_exception(at)
        self.assertEqual(self.count_events(), 0)
        self.assertTrue(
            any("Sin eventos en el rango" in h.value for h in at.subheader),
            [h.value for h in at.subheader],
        )

    def test_las_cuatro_paginas_se_renderizan(self):
        from streamlit.testing.v1 import AppTest

        self.write_logs()
        self.ingest()

        for module, title in [
            ("overview", "Overview"),
            ("events", "Events"),
            ("incidents", "Incidents"),
            ("settings", "Settings"),
        ]:
            with self.subTest(page=module):
                at = AppTest.from_string(_page_script(module), default_timeout=30)
                at.run()
                self.assert_no_exception(at)
                self.assertTrue(any(t.value == title for t in at.title))

    def test_events_pagina_y_filtra_sin_parsear(self):
        from streamlit.testing.v1 import AppTest

        self.write_logs()
        self.ingest()

        at = AppTest.from_string(_page_script("events"), default_timeout=30)
        at.run()
        self.assert_no_exception(at)

        # Con severidad ≥ 45 quedan el 500 de Apache, el ERROR de JSON y el
        # SEVERE de WebLogic; la línea basura no.
        captions = " ".join(c.value for c in at.caption)
        self.assertIn("3 eventos coinciden", captions)

        # El interruptor no puede quedar vacío: es el bug que este test fija.
        at.toggle[0].set_value(True).run()
        self.assert_no_exception(at)
        captions = " ".join(c.value for c in at.caption)
        self.assertIn("1 evento coincide", captions)

    def test_empty_state_de_events_cuando_no_hay_datos(self):
        from streamlit.testing.v1 import AppTest
        from app.db.migrations import migrate

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        migrate(self.db_path)

        at = AppTest.from_string(_page_script("events"), default_timeout=30)
        at.run()
        self.assert_no_exception(at)
        self.assertTrue(
            any("Ningún evento" in h.value for h in at.subheader),
            [h.value for h in at.subheader],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
