"""H-15…H-19: una transacción larga no puede secuestrar el único escritor.

SQLite en WAL permite lectores concurrentes, pero un solo escritor. El backfill
mantenía `BEGIN IMMEDIATE` abierto durante *todos* los lotes, así que el watcher
se quedaba sin escribir durante minutos. Estos tests comprueban el comportamiento
con hilos y una base real, no con mocks de `sqlite3`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import unittest

from app.analysis.backfill import backfill
from app.db.connection import connect, transaction
from tests.helpers import TempDbTestCase


class BackfillSeedMixin:
    def _seed_events(self, count: int) -> int:
        conn = self.connect()
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(log_files)")}
            fields = {
                "path": "'/tmp/concurrency.log'",
                "inode": "1",
                "generation": "0",
                "size": "0",
                "last_offset": "0",
                "last_size": "0",
                "first_seen": "'2026-10-05T10:00:00.000Z'",
                "last_seen": "'2026-10-05T10:00:00.000Z'",
                "is_active": "1",
            }
            if "dev" in columns:
                fields["dev"] = "0"
            conn.execute(
                f"INSERT INTO log_files ({', '.join(fields)}) VALUES ({', '.join(fields.values())})"
            )
            file_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            for offset in range(count):
                conn.execute(
                    "INSERT INTO events (file_id, offset, message, raw_line, "
                    "timestamp_utc, severity, parsed_ok) "
                    "VALUES (?, ?, 'timeout de consulta', 'timeout de consulta', ?, 50, 1)",
                    (file_id, offset, f"2026-10-05T10:{offset:02d}:00.000Z"),
                )
            conn.commit()
            return file_id
        finally:
            conn.close()

    def _written(self) -> int:
        conn = self.connect()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE fingerprint IS NOT NULL"
                ).fetchone()[0]
            )
        finally:
            conn.close()


class BackfillBatchTests(BackfillSeedMixin, TempDbTestCase):
    def test_una_transaccion_corta_por_lote(self):
        self._seed_events(5)
        conn = self.connect()
        with unittest.mock.patch("app.analysis.backfill.transaction", wraps=transaction) as spy:
            self.assertEqual(backfill(conn, batch_size=2), 5)
        self.assertEqual(spy.call_count, 3, "3 lotes => 3 COMMIT, no uno solo")

    def test_la_resincronizacion_tiene_su_propia_transaccion(self):
        self._seed_events(3)
        conn = self.connect()
        with unittest.mock.patch("app.analysis.backfill.transaction", wraps=transaction) as spy:
            backfill(conn, recalc=True, batch_size=2)
        self.assertEqual(spy.call_count, 3, "2 lotes + 1 resincronización")

    def test_un_escritor_concurrente_avanza_entre_lotes(self):
        """H-15/H-19 con lock real: otro escritor entra tras el primer COMMIT."""
        file_id = self._seed_events(4)
        worker_done = threading.Event()
        outcome: dict[str, object] = {}

        class PacedRules:
            def __init__(self) -> None:
                self.calls = 0

            def category(self, text: str) -> str:
                self.calls += 1
                if self.calls == 3:
                    # Ventana después del primer COMMIT: el backfill sigue vivo
                    # (queda un lote) pero el lock ya debe estar libre.
                    time.sleep(0.4)
                return "timeout"

        def run() -> None:
            try:
                outcome["updated"] = backfill(self.connect(), PacedRules(), batch_size=2)
            except Exception as exc:  # pragma: no cover - se reporta abajo
                outcome["error"] = exc
            finally:
                worker_done.set()

        worker = threading.Thread(target=run)
        worker.start()
        try:
            observer = self.connect()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not worker_done.is_set():
                if self._written() >= 2:
                    writer = connect(self.db_path, busy_timeout_ms=300)
                    try:
                        with transaction(writer):
                            writer.execute(
                                "INSERT INTO events (file_id, offset, message, raw_line, "
                                "timestamp_utc, severity, parsed_ok) "
                                "VALUES (?, 999, 'otro', 'otro', "
                                "'2026-10-05T11:00:00.000Z', 50, 1)",
                                (file_id,),
                            )
                        outcome["writer"] = "ok"
                    except sqlite3.OperationalError as exc:
                        outcome["writer"] = exc
                    finally:
                        writer.close()
                    break
                time.sleep(0.01)
            observer.close()
        finally:
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "el backfill no terminó")
        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertEqual(
            outcome.get("writer"),
            "ok",
            "el escritor concurrente no pudo avanzar entre lotes: se retuvo el lock",
        )
        self.assertEqual(self._written(), 5, "el backfill y el escritor concurrente se perdieron")


class SustainedLockTests(BackfillSeedMixin, TempDbTestCase):
    def test_lock_sostenido_falla_limpio_y_se_recupera(self):
        """H-19: > busy_timeout bloquea, pero no deja la conexión en transacción."""
        file_id = self._seed_events(1)
        acquired = threading.Event()
        release = threading.Event()
        blocker = self.connect()

        def hold() -> None:
            with transaction(blocker):
                blocker.execute(
                    "UPDATE events SET category = 'bloqueado' WHERE file_id = ?", (file_id,)
                )
                acquired.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(acquired.wait(timeout=5))
            conn = connect(self.db_path, busy_timeout_ms=150)
            try:
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    with transaction(conn):
                        conn.execute(
                            "INSERT INTO events (file_id, offset, message, raw_line, "
                            "timestamp_utc, severity) "
                            "VALUES (?, 500, 'tarde', 'tarde', "
                            "'2026-10-05T12:00:00.000Z', 50)",
                            (file_id,),
                        )
                self.assertIn("locked", str(ctx.exception).lower())
                self.assertFalse(
                    conn.in_transaction, "un fallo de lock dejó la transacción abierta"
                )
            finally:
                conn.close()
        finally:
            release.set()
            thread.join(timeout=5)

        retry = self.connect()
        with transaction(retry):
            retry.execute(
                "INSERT INTO events (file_id, offset, message, raw_line, "
                "timestamp_utc, severity) "
                "VALUES (?, 500, 'tarde', 'tarde', '2026-10-05T12:00:00.000Z', 50)",
                (file_id,),
            )
        self.assertEqual(
            retry.execute("SELECT COUNT(*) FROM events WHERE offset = 500").fetchone()[0], 1
        )


if __name__ == "__main__":
    unittest.main()
