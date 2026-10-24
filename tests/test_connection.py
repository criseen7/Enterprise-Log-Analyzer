"""Aceptación de Fase 0: conexión, WAL y concurrencia.

El test de concurrencia es el que justifica D1. Si algún día alguien cambia
journal_mode a `delete`, estos tests son los que fallan primero.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import unittest

from app.db.connection import (
    DatabaseError,
    connect,
    connection,
    db_size_bytes,
    journal_mode,
    transaction,
)
from tests.helpers import TempDbTestCase

_FILE_ROW = (
    "INSERT INTO log_files (id, path, first_seen, last_seen) "
    "VALUES (1, 'logs/concurrent.log', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
)


class JournalModeTests(TempDbTestCase):
    def test_journal_mode_is_wal(self):
        self.assertEqual(journal_mode(self.db_path), "wal")

    def test_wal_creates_sidecar_files(self):
        # Si existen -wal y -shm, WAL está realmente activo, no solo declarado.
        conn = self.connect()
        conn.execute(_FILE_ROW)
        conn.execute("INSERT INTO events (file_id, offset, raw_line) VALUES (1, 1, 'x')")
        self.assertTrue(self.db_path.with_name("test.db-wal").exists())


class PragmaTests(TempDbTestCase):
    def test_busy_timeout_applies_per_connection(self):
        self.assertEqual(self.connect().execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_foreign_keys_on(self):
        # SQLite lo tiene OFF por defecto en cada conexión nueva.
        self.assertEqual(self.connect().execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_custom_busy_timeout(self):
        conn = connect(self.db_path, busy_timeout_ms=1234)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1234)

    def test_invalid_synchronous_rejected(self):
        with self.assertRaises(DatabaseError):
            connect(self.db_path, synchronous="DANGER; DROP TABLE events")

    def test_non_positive_busy_timeout_rejected(self):
        with self.assertRaises(DatabaseError):
            connect(self.db_path, busy_timeout_ms=0)

    def test_rows_are_mapped_by_name(self):
        self.assertEqual(self.connect().execute("SELECT 1 AS n").fetchone()["n"], 1)


class ConcurrencyTests(TempDbTestCase):
    """2 escritores + 1 lector simultáneos. Con WAL, esto no debe fallar nunca."""

    def test_two_writers_and_one_reader(self):
        errors: list[BaseException] = []
        start = threading.Barrier(3)
        iterations = 40

        conn = self.connect()
        conn.execute(_FILE_ROW)

        def writer(worker: int) -> None:
            local = connect(self.db_path, busy_timeout_ms=5000)
            try:
                start.wait(timeout=10)
                for i in range(iterations):
                    with transaction(local):
                        local.execute(
                            "INSERT INTO events (file_id, offset, raw_line, severity, level_norm) "
                            "VALUES (1, ?, ?, 30, 'INFO')",
                            (worker * 1_000_000 + i, f"w{worker}-{i}"),
                        )
                    time.sleep(0)
            except BaseException as exc:  # noqa: BLE001 - el test necesita ver cualquier fallo
                errors.append(exc)
            finally:
                local.close()

        def reader() -> None:
            local = connect(self.db_path, busy_timeout_ms=5000)
            try:
                start.wait(timeout=10)
                for _ in range(iterations):
                    local.execute("SELECT COUNT(*) FROM events").fetchone()
                    time.sleep(0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                local.close()

        threads = [
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=writer, args=(2,)),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"conexiones concurrentes fallaron: {errors}")
        self.assertFalse(any(t.is_alive() for t in threads), "un hilo quedó colgado (deadlock)")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], iterations * 2)

    def test_open_reader_transaction_does_not_block_writer_commit(self):
        """El motivo real de WAL: una query larga del dashboard no frena la ingesta.

        Con el journal por defecto (DELETE), un lector que mantiene su transacción
        de lectura abierta retiene un lock SHARED, y el COMMIT del escritor espera
        un EXCLUSIVE que no puede obtener. El escritor se queda esperando hasta
        agotar busy_timeout y la ingesta se detiene.
        """
        reader = connect(self.db_path, busy_timeout_ms=500)
        self.addCleanup(reader.close)
        reader.execute(_FILE_ROW)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM events").fetchone()  # retiene SHARED

        writer = connect(self.db_path, busy_timeout_ms=500)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO events (file_id, offset, raw_line) VALUES (1, 1, 'x')")

        started = time.monotonic()
        writer.execute("COMMIT")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.4, "el COMMIT del escritor esperó a un lector abierto")
        self.assertEqual(self.connect().execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        reader.execute("COMMIT")

    def test_delete_journal_would_block_the_writer(self):
        """Contraprueba: el test anterior tiene dientes porque DELETE sí falla.

        Si alguien 'optimiza' la conexión a journal_mode=DELETE, este test sigue
        pasando mientras que el anterior deja de ser cierto. Aquí se comprueba
        explícitamente el comportamiento contrario, con una DB cruda.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rollback.db")
            raw = sqlite3.connect(path, isolation_level=None, timeout=0.3)
            self.addCleanup(raw.close)
            raw.execute("PRAGMA journal_mode=DELETE")
            raw.execute("PRAGMA busy_timeout=300")
            raw.execute("CREATE TABLE t (a INTEGER)")

            reader = sqlite3.connect(path, isolation_level=None, timeout=0.3)
            self.addCleanup(reader.close)
            reader.execute("PRAGMA busy_timeout=300")
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM t").fetchone()

            writer = sqlite3.connect(path, isolation_level=None, timeout=0.3)
            self.addCleanup(writer.close)
            writer.execute("PRAGMA busy_timeout=300")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO t VALUES (1)")
            with self.assertRaises(sqlite3.OperationalError):
                writer.execute("COMMIT")

            reader.execute("COMMIT")


class TransactionTests(TempDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.connect().execute(_FILE_ROW)

    def test_commit_persists(self):
        conn = self.connect()
        with transaction(conn):
            conn.execute("INSERT INTO events (file_id, offset, raw_line) VALUES (1, 1, 'ok')")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_rollback_on_exception(self):
        conn = self.connect()
        with self.assertRaises(ValueError):
            with transaction(conn):
                conn.execute("INSERT INTO events (file_id, offset, raw_line) VALUES (1, 1, 'x')")
                raise ValueError("boom")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)


class ContextManagerTests(TempDbTestCase):
    def test_connection_is_closed_on_exit(self):
        with connection(self.settings()) as conn:
            conn.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_read_only_blocks_writes(self):
        with connection(self.settings(), read_only=True) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO events (file_id, offset, raw_line) VALUES (1, 1, 'no')")

    def test_db_size_includes_sidecars(self):
        self.assertGreater(db_size_bytes(self.db_path), 0)


if __name__ == "__main__":
    unittest.main()
