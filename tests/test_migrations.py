"""Migraciones concurrentes: el lock debe serializar el aplicador.

Sin lock, dos procesos que arrancan a la vez leen la misma versión y ambos
intentan aplicar la baseline; SQLite serializa las escrituras, pero no la
secuencia "leer versión → aplicar script → registrar".
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.db import migrations
from app.db.migrations import migrate, status
from app.db.schema import SCHEMA_VERSION


class MigrationLockTests(unittest.TestCase):
    def test_dos_migraciones_concurrentes_no_aplican_la_baseline_dos_veces(self):
        with tempfile.TemporaryDirectory(prefix="ela-migrate-") as tmp:
            db_path = Path(tmp) / "data" / "app.db"
            eventos: list[tuple[str, int]] = []
            real_apply = migrations._apply_script

            def apply_lento(conn, script, version):
                eventos.append(("inicio", version))
                time.sleep(0.05)
                try:
                    return real_apply(conn, script, version)
                finally:
                    eventos.append(("fin", version))

            errores: list[BaseException] = []

            def runner() -> None:
                try:
                    migrate(db_path)
                except BaseException as exc:  # noqa: BLE001 - el test debe verlo
                    errores.append(exc)

            with mock.patch.object(migrations, "_apply_script", side_effect=apply_lento):
                threads = [threading.Thread(target=runner) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=15)

            self.assertEqual(errores, [])
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertEqual(
                [evento for evento in eventos if evento[0] == "inicio"],
                [("inicio", migrations._BASELINE_VERSION)],
                "solo un proceso aplica la baseline",
            )
            self.assertEqual(status(db_path)[0], SCHEMA_VERSION)
            lock_file = db_path.with_name(db_path.name + ".migrate.lock")
            self.assertTrue(lock_file.exists())

    def test_migrate_es_idempotente(self):
        with tempfile.TemporaryDirectory(prefix="ela-migrate-") as tmp:
            db_path = Path(tmp) / "app.db"
            self.assertIn(1, migrate(db_path))
            self.assertEqual(migrate(db_path), [])
            self.assertEqual(status(db_path), (SCHEMA_VERSION, SCHEMA_VERSION))


if __name__ == "__main__":
    unittest.main()
