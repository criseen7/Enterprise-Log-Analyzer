"""Migraciones concurrentes: el lock debe serializar el aplicador.

Sin lock, dos procesos que arrancan a la vez leen la misma versión y ambos
intentan aplicar la baseline; SQLite serializa las escrituras, pero no la
secuencia "leer versión → aplicar script → registrar".
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.db import migrations
from app.db.connection import connect
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


class IncidentWatermarkMigrationTests(unittest.TestCase):
    def test_v7_a_v8_rellena_la_marca_de_agua_de_lo_ya_notificado(self):
        with tempfile.TemporaryDirectory(prefix="ela-migrate-v8-") as tmp:
            db_path = Path(tmp) / "app.db"
            conn = connect(db_path)
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')",
                [(version,) for version in range(1, 8)],
            )
            conn.execute(
                "CREATE TABLE incidents ("
                "id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, "
                "last_seen TEXT NOT NULL, notified_at TEXT)"
            )
            conn.execute(
                "INSERT INTO incidents (fingerprint, last_seen, notified_at) "
                "VALUES ('notificado', '2026-10-05T12:00:00.000Z', "
                "'2026-10-05T12:01:00.000Z')"
            )
            conn.execute(
                "INSERT INTO incidents (fingerprint, last_seen, notified_at) "
                "VALUES ('sin-notificar', '2026-10-05T11:00:00.000Z', NULL)"
            )
            conn.close()

            self.assertIn(8, migrate(db_path, target=8))

            conn = connect(db_path)
            try:
                columns = {
                    str(row["name"])
                    for row in conn.execute("SELECT name FROM pragma_table_info('incidents')")
                }
                self.assertIn("notified_through", columns)
                rows = {
                    str(row["fingerprint"]): row
                    for row in conn.execute("SELECT fingerprint, notified_through FROM incidents")
                }
                self.assertEqual(rows["notificado"]["notified_through"], "2026-10-05T12:00:00.000Z")
                self.assertIsNone(rows["sin-notificar"]["notified_through"])
            finally:
                conn.close()


class HitlAuditMigrationTests(unittest.TestCase):
    def test_v8_a_v9_backfill_el_historial_y_separa_el_rechazo(self):
        with tempfile.TemporaryDirectory(prefix="ela-migrate-v9-") as tmp:
            db_path = Path(tmp) / "app.db"
            conn = connect(db_path)
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')",
                [(version,) for version in range(1, 9)],
            )
            conn.execute(
                "CREATE TABLE agent_actions ("
                "id TEXT PRIMARY KEY, tool TEXT NOT NULL, params TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'PENDING', confirm_token TEXT, "
                "created_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
                "approved_at TEXT, approved_by TEXT, executed_at TEXT, result TEXT)"
            )
            conn.executemany(
                "INSERT INTO agent_actions (id, tool, params, status, confirm_token, "
                "created_at, expires_at, approved_at, approved_by, executed_at, result) "
                "VALUES (?, 't', '{}', ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "rechazada",
                        "REJECTED",
                        None,
                        "2026-10-01T10:00:00.000Z",
                        "2026-10-01T10:15:00.000Z",
                        "2026-10-01T10:05:00.000Z",
                        "bob",
                        None,
                        None,
                    ),
                    (
                        "ejecutada",
                        "EXECUTED",
                        "abc",
                        "2026-10-01T11:00:00.000Z",
                        "2026-10-01T11:15:00.000Z",
                        "2026-10-01T11:05:00.000Z",
                        "alice",
                        "2026-10-01T11:06:00.000Z",
                        "ok",
                    ),
                ],
            )
            conn.close()

            self.assertIn(9, migrate(db_path))

            conn = connect(db_path)
            try:
                columns = {
                    str(row["name"])
                    for row in conn.execute("SELECT name FROM pragma_table_info('agent_actions')")
                }
                self.assertIn("rejected_at", columns)
                self.assertIn("rejected_by", columns)

                rows = {
                    str(row["id"]): row
                    for row in conn.execute(
                        "SELECT id, approved_by, rejected_at, rejected_by FROM agent_actions"
                    )
                }
                self.assertEqual(rows["rechazada"]["rejected_by"], "bob")
                self.assertEqual(rows["rechazada"]["rejected_at"], "2026-10-01T10:05:00.000Z")
                self.assertEqual(rows["ejecutada"]["approved_by"], "alice")
                self.assertIsNone(rows["ejecutada"]["rejected_by"])

                events = {
                    (str(row["action_id"]), str(row["event"]))
                    for row in conn.execute("SELECT action_id, event FROM hitl_audit")
                }
                self.assertIn(("rechazada", "REJECTED"), events)
                self.assertNotIn(("rechazada", "APPROVED"), events)
                self.assertIn(("ejecutada", "CREATED"), events)
                self.assertIn(("ejecutada", "APPROVED"), events)
                self.assertIn(("ejecutada", "EXECUTED"), events)

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("DELETE FROM hitl_audit WHERE action_id = 'rechazada'")
            finally:
                conn.close()

            self.assertEqual(migrate(db_path), [])


if __name__ == "__main__":
    unittest.main()
