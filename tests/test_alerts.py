import unittest

from tests.test_ui_data import UiDataTestCase
from app.alerts import evaluate_incidents
from app.alerts.incidents import (
    VALID_STATUSES,
    get_incident,
    incident_counts,
    list_incidents,
    set_incident_status,
    set_incident_status_by_fingerprint,
    status_label,
)
from app.reports import export_incidents_csv, export_incidents_html


class AlertsTests(UiDataTestCase):
    def _insert(
        self, offset: int, fingerprint: str, ts: str, *, severity: int = 50
    ) -> None:
        self.conn.execute(
            "INSERT INTO events ("
            "file_id, offset, fingerprint, category, timestamp_utc, timestamp_raw, "
            "level_raw, level_norm, severity, service, logger, message, raw_line, parsed_ok"
            ") VALUES (1, ?, ?, 'network', ?, ?, 'ERROR', 'ERROR', ?, 'api', 'log', "
            "'timeout occurred', 'timeout', 1)",
            (offset, fingerprint, ts, ts, severity),
        )

    def test_five_thousand_timeouts_single_incident_without_repeated_notifications(self):
        for i in range(5000):
            hour = 10 + (i // 60) % 10
            minute = i % 60
            self._insert(i, "fp-timeout", f"2026-10-05T{hour:02d}:{minute:02d}:00Z")
        self.conn.commit()

        first = evaluate_incidents(self.conn, window_minutes=480, min_count=50)
        self.assertTrue(first["results"][0]["notified"])

        second = evaluate_incidents(self.conn, window_minutes=480, min_count=50)
        self.assertFalse(second["results"][0]["notified"])

        count = self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_abre_un_incidente_por_cada_huella_sobre_el_umbral(self):
        """Sin tope implícito: 25 huellas sobre el umbral son 25 incidentes."""
        for fp in range(25):
            for j in range(2):
                self._insert(fp * 2 + j, f"fp-{fp:03d}", f"2026-10-05T12:00:0{j}.000Z")
        self.conn.commit()

        result = evaluate_incidents(self.conn, window_minutes=120, min_count=2)
        self.assertEqual(result["evaluated"], 25)
        count = self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 25)

    def test_incidente_resuelto_se_reabre_al_recurrir(self):
        """Un rebrote sobre un incidente RESOLVED lo reabre y reinicia el cooldown."""
        for i in range(60):
            self._insert(i, "fp-reabre", f"2026-10-05T12:{i:02d}:00Z")
        self.conn.commit()
        evaluate_incidents(self.conn, window_minutes=120, min_count=50)
        inc_id = int(
            self.conn.execute(
                "SELECT id FROM incidents WHERE fingerprint = 'fp-reabre'"
            ).fetchone()[0]
        )
        self.assertTrue(set_incident_status(self.conn, inc_id, "RESOLVED"))
        self.assertEqual(get_incident(self.conn, inc_id)["status"], "RESOLVED")

        evaluate_incidents(self.conn, window_minutes=120, min_count=50)
        row = get_incident(self.conn, inc_id)
        self.assertEqual(row["status"], "OPEN", "el rebrote debe reabrir el incidente")
        self.assertIsNone(row["notified_at"], "la reapertura reinicia el cooldown")

    def test_event_count_refleja_la_ventana_no_un_maximo(self):
        """`event_count` es el conteo de la ventana vigente, puede bajar."""
        for i in range(60):
            self._insert(i, "fp-window", f"2026-10-05T12:{i:02d}:00Z")
        self.conn.commit()
        evaluate_incidents(self.conn, window_minutes=120, min_count=50)
        inc_id = int(
            self.conn.execute(
                "SELECT id FROM incidents WHERE fingerprint = 'fp-window'"
            ).fetchone()[0]
        )
        self.assertEqual(get_incident(self.conn, inc_id)["event_count"], 60)

        # Ventana de 1 min: solo entran 12:58 y 12:59.
        evaluate_incidents(self.conn, window_minutes=1, min_count=2)
        self.assertEqual(get_incident(self.conn, inc_id)["event_count"], 2)

    def test_evaluar_dos_veces_no_duplica_ni_falla(self):
        """El upsert es atómico: reevaluar no lanza IntegrityError ni duplica filas."""
        for i in range(60):
            self._insert(i, "fp-idem", f"2026-10-05T12:{i:02d}:00Z")
        self.conn.commit()
        evaluate_incidents(self.conn, window_minutes=120, min_count=50)
        evaluate_incidents(self.conn, window_minutes=120, min_count=50)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE fingerprint = 'fp-idem'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_no_incident_below_threshold(self):
        for i in range(49):
            self._insert(i, "fp-low", f"2026-10-05T12:{i:02d}:00Z")
        self.conn.commit()
        result = evaluate_incidents(self.conn, window_minutes=120, min_count=50)
        self.assertEqual(result["evaluated"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0
        )

    def test_csv_has_utf8_sig_bom(self):
        for i in range(60):
            self._insert(i, "fp-csv", f"2026-10-05T12:{i:02d}:00Z")
        self.conn.commit()
        evaluate_incidents(self.conn, window_minutes=120, min_count=50)

        csv_bytes = export_incidents_csv(self.conn)
        self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(csv_bytes.decode("utf-8-sig").startswith("id,fingerprint"))

    def test_html_is_autocontained(self):
        for i in range(60):
            self._insert(i, "fp-html", f"2026-10-05T12:{i:02d}:00Z")
        self.conn.commit()
        evaluate_incidents(self.conn, window_minutes=120, min_count=50)

        html = export_incidents_html(self.conn)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("<table>", html)
        self.assertNotIn("<script", html.lower())


class IncidentManagementTests(UiDataTestCase):
    def _open_incident(self, fingerprint: str = "fp-mgmt") -> int:
        for i in range(60):
            self.conn.execute(
                "INSERT INTO events (file_id, offset, fingerprint, category, "
                "timestamp_utc, severity, message, raw_line, parsed_ok) "
                "VALUES (1, ?, ?, 'network', ?, 50, 'timeout', 'timeout', 1)",
                (i, fingerprint, f"2026-10-05T12:{i:02d}:00Z"),
            )
        self.conn.commit()
        evaluate_incidents(self.conn, window_minutes=120, min_count=50)
        return int(
            self.conn.execute(
                "SELECT id FROM incidents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()[0]
        )

    def test_counts_have_all_states_even_when_empty(self):
        counts = incident_counts(self.conn)
        self.assertEqual(set(counts), set(VALID_STATUSES))

    def test_ack_and_resolve_change_status(self):
        inc_id = self._open_incident()
        self.assertTrue(set_incident_status(self.conn, inc_id, "ACKNOWLEDGED"))
        self.assertEqual(get_incident(self.conn, inc_id)["status"], "ACKNOWLEDGED")
        self.assertTrue(set_incident_status(self.conn, inc_id, "RESOLVED"))
        self.assertEqual(get_incident(self.conn, inc_id)["status"], "RESOLVED")

    def test_reopen_resets_cooldown(self):
        inc_id = self._open_incident()
        set_incident_status(self.conn, inc_id, "RESOLVED")
        set_incident_status(self.conn, inc_id, "OPEN")
        row = get_incident(self.conn, inc_id)
        self.assertEqual(row["status"], "OPEN")
        self.assertIsNone(row["notified_at"])

    def test_status_by_fingerprint_and_invalid_status(self):
        self._open_incident("fp-by-fp")
        self.assertTrue(
            set_incident_status_by_fingerprint(self.conn, "fp-by-fp", "ACKNOWLEDGED")
        )
        self.assertFalse(set_incident_status_by_fingerprint(self.conn, "nope", "OPEN"))
        with self.assertRaises(ValueError):
            set_incident_status(self.conn, 1, "BOGUS")

    def test_list_filters_by_status(self):
        inc_id = self._open_incident()
        self.assertEqual(len(list_incidents(self.conn, status="OPEN")), 1)
        self.assertEqual(len(list_incidents(self.conn, status="RESOLVED")), 0)
        set_incident_status(self.conn, inc_id, "RESOLVED")
        self.assertEqual(len(list_incidents(self.conn, status="RESOLVED")), 1)

    def test_status_label_shortens_acknowledged(self):
        self.assertEqual(status_label("ACKNOWLEDGED"), "ACK")
        self.assertEqual(status_label("OPEN"), "OPEN")
        self.assertEqual(status_label("WEIRD"), "WEIRD")


if __name__ == "__main__":
    unittest.main()
