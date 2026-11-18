"""Fase 2: ingesta idempotente, rotación y ensamblado multilínea.

Cada test corre contra un directorio temporal propio (`TempDbTestCase`): la
idempotencia y la rotación dependen del inode y del offset real, así que un mock
no probaría nada de lo que importa aquí.
"""

from __future__ import annotations

import io
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from app.db.connection import transaction
from app.db.repository import (
    count_events,
    event_row,
    insert_events,
    last_offset,
    parse_coverage,
    recent_runs,
    resolve_log_file,
    update_progress,
)
from app.ingest.ingestor import discover, ingest_file, ingest_paths, ingest_paths_transactional
from app.ingest.reader import (
    MAX_ENTRY_BYTES,
    TRUNCATION_NOTE,
    iter_entries,
    iter_entry_batches,
    read_complete_lines,
)
from app.parser.registry import Registry
from tests.helpers import PATTERNS_DIR, TempDbTestCase

WLS = "05/oct/2026 14:03:2{n}.481 CEST com.acme.Svc : [main] SEVERE | fallo numero {n}\n"
JSON = '{{"timestamp":"2026-10-05T10:00:0{n}Z","level":"INFO","message":"mensaje {n}"}}\n'


class IngestBase(TempDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = Registry.from_directory(PATTERNS_DIR)
        self.conn = self.connect()
        self.log = self.log_dir / "app.log"

    def append(self, text: str, path: Path | None = None) -> Path:
        target = path or self.log
        with target.open("a", encoding="utf-8") as fh:
            fh.write(text)
        return target

    def messages(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT message FROM events ORDER BY offset")]


class SymlinkTests(IngestBase):
    def test_un_symlink_en_logs_no_se_ingiere(self):
        fuera = self.root / "secreto.txt"
        fuera.write_text(JSON.format(n=1), encoding="utf-8")
        enlace = self.log_dir / "app.log"
        try:
            os.symlink(fuera, enlace)
        except (OSError, NotImplementedError):
            self.skipTest("el sistema no admite symlinks")

        result = ingest_file(self.conn, enlace, self.registry)
        self.assertEqual(result.status, "error")
        self.assertEqual(count_events(self.conn), 0)

    def test_log_files_excluye_symlinks(self):
        real = self.write_log("app.log", [JSON.format(n=1)])
        try:
            os.symlink(real, self.log_dir / "copia.log")
        except (OSError, NotImplementedError):
            self.skipTest("el sistema no admite symlinks")
        self.assertEqual(self.settings().log_files, [real])


class IdempotenciaTest(IngestBase):
    def test_dos_pasadas_no_duplican(self):
        self.append(WLS.format(n=1))
        first = ingest_file(self.conn, self.log, self.registry)
        second = ingest_file(self.conn, self.log, self.registry)
        self.assertEqual(first.counts.inserted, 1)
        self.assertEqual(second.counts.inserted, 0)
        self.assertEqual(second.entries, 0)
        self.assertEqual(count_events(self.conn), 1)

    def test_offset_solo_avanza_con_lineas_completas(self):
        self.append(WLS.format(n=1))
        ingest_file(self.conn, self.log, self.registry)

        # Escritura en curso: la línea todavía no termina en newline.
        self.append("05/oct/2026 14:03:22.481 CEST com.acme.S : [m] INFO | a med")
        partial = ingest_file(self.conn, self.log, self.registry)
        self.assertEqual(partial.entries, 0)
        self.assertEqual(partial.new_offset, len(WLS.format(n=1)))

        self.append("ias\n")
        completa = ingest_file(self.conn, self.log, self.registry)
        self.assertEqual(completa.counts.inserted, 1)
        self.assertIn("a medias", self.messages()[1])

    def test_offset_en_bytes_no_en_caracteres(self):
        # 'ñ' ocupa 2 bytes: un offset por caracteres desplazaría la segunda línea.
        self.append('{"timestamp":"2026-10-05T10:00:00Z","level":"INFO","message":"año ñ 中文"}\n')
        ingest_file(self.conn, self.log, self.registry)
        self.append(JSON.format(n=1))
        segunda = ingest_file(self.conn, self.log, self.registry)
        self.assertEqual(segunda.entries, 1)
        self.assertEqual(count_events(self.conn), 2)

    def test_transaccion_revierte_la_pasada_entera(self):
        self.append(WLS.format(n=1))
        with self.assertRaises(sqlite3.IntegrityError):
            with transaction(self.conn):
                ingest_file(self.conn, self.log, self.registry)
                # Violación de la FK: debe abortar la transacción completa.
                self.conn.execute(
                    "INSERT INTO events (file_id, offset, raw_line) VALUES (9999, 0, 'x')"
                )
        self.assertEqual(count_events(self.conn), 0)

    def test_eventos_omitidos_se_cuentan(self):
        """Un offset ya ingerido debe omitirse, no reinsertarse."""
        self.append(WLS.format(n=1))
        ingest_file(self.conn, self.log, self.registry)

        state = resolve_log_file(self.conn, self.log)
        resultado = self.registry.parse_line(WLS.format(n=1))
        counts = insert_events(self.conn, [event_row(state.file_id, 0, resultado)])

        self.assertEqual((counts.inserted, counts.skipped), (0, 1))
        self.assertEqual(count_events(self.conn), 1)


class RotacionTest(IngestBase):
    def test_truncado_crea_generacion_nueva(self):
        self.append(WLS.format(n=1) + WLS.format(n=2))
        ingest_file(self.conn, self.log, self.registry)

        # copytruncate: mismo inode, contenido más corto.
        self.log.write_text(WLS.format(n=3), encoding="utf-8")
        r = ingest_file(self.conn, self.log, self.registry)

        self.assertEqual(r.reason, "truncated")
        self.assertEqual(r.counts.inserted, 1)
        self.assertEqual(count_events(self.conn), 3, "el truncado no puede perder eventos")

        filas = list(
            self.conn.execute("SELECT generation, is_active FROM log_files ORDER BY generation")
        )
        self.assertEqual(len(filas), 2)
        self.assertEqual([f[1] for f in filas], [0, 1], "la generación anterior queda inactiva")

    def test_offsets_repetidos_entre_generaciones_no_chocan(self):
        """Ambas generaciones empiezan en offset 0: file_id las distingue.

        El truncado se detecta por `size < last_offset`, así que el contenido
        nuevo debe ser más corto. La reescritura de igual tamaño se cubre con
        `head_hash` (ver el test siguiente).
        """
        self.append(WLS.format(n=1))
        ingest_file(self.conn, self.log, self.registry)
        self.log.write_text(WLS.format(n=9).replace("numero 9", "n"), encoding="utf-8")
        ingest_file(self.conn, self.log, self.registry)

        files = [
            r[0] for r in self.conn.execute("SELECT DISTINCT file_id FROM events ORDER BY file_id")
        ]
        self.assertEqual(len(files), 2)
        self.assertEqual(count_events(self.conn), 2)

    def test_reescritura_del_mismo_tamano_se_detecta_por_la_cabecera(self):
        """`copytruncate` que reescribe con los mismos bytes no puede pasar inadvertido.

        Comparar solo el tamaño lo confundiría con "no ha cambiado" y el archivo
        nuevo quedaría detrás de `last_offset`. `head_hash` sí cambia.
        """
        self.append(WLS.format(n=1))
        ingest_file(self.conn, self.log, self.registry)
        self.assertEqual(len(WLS.format(n=1)), len(WLS.format(n=2)))

        self.log.write_text(WLS.format(n=2), encoding="utf-8")
        r = ingest_file(self.conn, self.log, self.registry)

        self.assertEqual(r.reason, "truncated")
        self.assertEqual(r.counts.inserted, 1)
        self.assertEqual(count_events(self.conn), 2)

    def test_inodo_reciclado_es_un_archivo_nuevo_y_no_un_rename(self):
        """El SO puede reutilizar el inodo de un archivo borrado para uno nuevo.

        Sin `head_hash`, la fila vieja parece un rename y su `last_offset` deja
        el archivo nuevo sin leer. Es el fallo intermitente que solo aparece en
        los sistemas de archivos que reciclan inodos; aquí se fuerza el número
        para que el test sea determinista.
        """
        anterior = self.log_dir / "app.log.1"
        anterior.write_text(WLS.format(n=0), encoding="utf-8")
        ingest_file(self.conn, anterior, self.registry)
        inodo_viejo = os.stat(anterior).st_ino

        self.append(WLS.format(n=1))
        ingest_file(self.conn, self.log, self.registry)
        os.rename(self.log, anterior)
        ingest_file(self.conn, anterior, self.registry)

        nuevo = self.log_dir / "app.log"
        nuevo.write_text(WLS.format(n=2), encoding="utf-8")
        real_fstat = os.fstat

        def fstat_con_inodo_reciclado(fd: int) -> os.stat_result:
            st = real_fstat(fd)
            return os.stat_result(
                (
                    st.st_mode,
                    inodo_viejo,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )

        with mock.patch("app.ingest.ingestor._fstat_fd", side_effect=fstat_con_inodo_reciclado):
            r = ingest_file(self.conn, nuevo, self.registry)

        self.assertEqual(r.reason, "recycled")
        self.assertEqual(r.counts.inserted, 1)
        self.assertEqual(count_events(self.conn), 3)

    def test_rename_no_reingiere_el_contenido_ya_ingerido(self):
        self.append(WLS.format(n=1))
        ingest_file(self.conn, self.log, self.registry)

        rotado = self.log_dir / "app.log.1"
        os.rename(self.log, rotado)
        # El rotado ya estaba ingerido: se reconoce por inode y no se relee.
        r_rotado = ingest_file(self.conn, rotado, self.registry)
        self.assertEqual(r_rotado.counts.inserted, 0)

        self.append(WLS.format(n=2))
        r_nuevo = ingest_file(self.conn, self.log, self.registry)
        self.assertEqual(r_nuevo.reason, "new")
        self.assertEqual(count_events(self.conn), 2)

    def test_rename_sobre_una_ruta_ya_ocupada_desactiva_la_fila_vieja(self):
        """logrotate pisa `app.log.1`; la fila que ocupaba esa ruta debe salir."""
        # app.log.1 ya existe de una rotación anterior, con su propio inode.
        anterior = self.log_dir / "app.log.1"
        anterior.write_text(WLS.format(n=0), encoding="utf-8")
        ingest_file(self.conn, anterior, self.registry)

        # logrotate renombra app.log -> app.log.1 y pisa la fila anterior.
        self.append(WLS.format(n=1))
        ingest_file(self.conn, self.log, self.registry)
        os.rename(self.log, anterior)

        r = ingest_file(self.conn, anterior, self.registry)
        nuevo = self.log_dir / "app.log"
        nuevo.write_text(WLS.format(n=2), encoding="utf-8")
        r_nuevo = ingest_file(self.conn, nuevo, self.registry)

        activas = [
            row[0]
            for row in self.conn.execute(
                "SELECT path FROM log_files WHERE is_active = 1 ORDER BY path"
            )
        ]
        self.assertEqual(len(activas), 2, "las dos rutas vivas deben quedar activas")
        self.assertEqual(r.counts.inserted, 0, "el rotado no se relee")
        self.assertEqual(r_nuevo.counts.inserted, 1)
        self.assertEqual(count_events(self.conn), 3)

        activas_paths = {
            row[0] for row in self.conn.execute("SELECT path FROM log_files WHERE is_active=1")
        }
        self.assertEqual(activas_paths, {str(anterior.resolve()), str(nuevo.resolve())})

    def test_archivo_inexistente_no_rompe_la_pasada(self):
        r = ingest_file(self.conn, self.log_dir / "nunca-existio.log", self.registry)
        self.assertEqual(r.status, "vanished")
        self.assertEqual(count_events(self.conn), 0)


class MultilineaTest(IngestBase):
    def test_stack_trace_producece_un_solo_evento(self):
        self.append(
            WLS.format(n=1)
            + "\tat com.acme.dao.OrderDao.insert(OrderDao.java:64)\n"
            + "Caused by: java.sql.SQLException: connection timeout after 30000ms\n"
            + "\t... 18 more\n"
            + WLS.format(n=2)
        )
        r = ingest_file(self.conn, self.log, self.registry)

        self.assertEqual(r.entries, 2, "7 líneas físicas deben dar 2 eventos")
        self.assertEqual(count_events(self.conn), 2)

        mensaje = self.messages()[0]
        self.assertIn("Caused by:", mensaje)
        self.assertIn("... 18 more", mensaje)
        self.assertIn("\tat com.acme.dao.OrderDao.insert", mensaje)

    def test_el_evento_conserva_el_timestamp_de_la_cabecera(self):
        self.append(WLS.format(n=1) + "Caused by: java.sql.SQLException: timeout\n")
        ingest_file(self.conn, self.log, self.registry)
        row = self.conn.execute("SELECT timestamp_utc, severity, logger FROM events").fetchone()
        self.assertEqual(row[0], "2026-10-05T12:03:21.481Z")
        self.assertEqual(row[1], 45)
        self.assertEqual(row[2], "com.acme.Svc")

    def test_json_no_se_contamina_con_lineas_sueltas(self):
        """Una línea sin timestamp no puede romper el JSON anterior."""
        self.append(JSON.format(n=0))
        self.append("linea suelta que no es un log\n")
        r = ingest_file(self.conn, self.log, self.registry)

        self.assertEqual(r.entries, 2)
        total, ok = parse_coverage(self.conn)
        self.assertEqual((total, ok), (2, 1), "la línea suelta se conserva como no parseada")
        self.assertIn("mensaje 0", self.messages()[0])

    def test_entrada_sobre_el_tope_se_trunca(self):
        self.append(WLS.format(n=1) + "X" * (MAX_ENTRY_BYTES + 1000) + "\n")
        ingest_file(self.conn, self.log, self.registry)
        total, _ = parse_coverage(self.conn)
        self.assertEqual(total, 1)
        self.assertIn(TRUNCATION_NOTE.strip(), self.messages()[0])


class CorridaTest(IngestBase):
    def test_audita_la_pasada(self):
        self.append(WLS.format(n=1))
        run = ingest_paths(self.conn, self.registry, [self.log])
        self.assertEqual((run.inserted, run.status), (1, "OK"))

        fila = recent_runs(self.conn)[0]
        self.assertEqual(fila["events_inserted"], 1)
        self.assertEqual(fila["files_processed"], 1)
        self.assertEqual(fila["events_unparsed"], 0)

    def test_registra_lineas_sin_parsear(self):
        self.append("nada parseable aqui\n")
        run = ingest_paths(self.conn, self.registry, [self.log])
        self.assertEqual(run.unparsed, 1)
        self.assertEqual(recent_runs(self.conn)[0]["events_unparsed"], 1)

    def test_una_pasada_repetida_no_registra_inserciones(self):
        self.append(WLS.format(n=1))
        ingest_paths(self.conn, self.registry, [self.log])
        segunda = ingest_paths(self.conn, self.registry, [self.log])
        self.assertEqual((segunda.inserted, segunda.skipped), (0, 0))
        self.assertEqual(count_events(self.conn), 1)

    def test_forma_transaccional(self):
        self.append(WLS.format(n=1))
        run = ingest_paths_transactional(self.conn, self.registry, [self.log])
        self.assertEqual(run.inserted, 1)
        self.assertEqual(count_events(self.conn), 1)

    def test_varios_archivos_en_una_pasada(self):
        self.append(WLS.format(n=1))
        self.append(WLS.format(n=2), self.log_dir / "otro.log")
        run = ingest_paths(self.conn, self.registry, [self.log, self.log_dir / "otro.log"])
        self.assertEqual(run.inserted, 2)
        self.assertEqual(len(run.files), 2)

    def test_discover_es_determinista(self):
        for name in ("c.log", "a.log", "b.log"):
            self.append(WLS.format(n=1), self.log_dir / name)
        config = self.settings()
        encontrados = discover(config)
        self.assertEqual(encontrados, sorted(encontrados))
        self.assertEqual([p.name for p in encontrados], ["a.log", "b.log", "c.log"])


class ReaderTest(IngestBase):
    def test_no_devuelve_linea_sin_newline(self):
        self.log.write_bytes(b'{"a":1}\n{"b":')
        lines, offset = read_complete_lines(self.log, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(offset, 8)

    def test_ninguna_linea_se_pierde_entre_chunks(self):
        datos = b"".join(b'{"n":%d}\n' % i for i in range(50))
        self.log.write_bytes(datos)
        lines, offset = read_complete_lines(self.log, 0, chunk_bytes=7)
        self.assertEqual(len(lines), 50)
        self.assertEqual(offset, len(datos))

    def test_caracter_multibyte_partido_entre_chunks(self):
        # El corte cae dentro de 'ñ': si se decodifica por chunk, sale roto.
        self.log.write_text('{"m":"ñññ"}\n{"m":"x"}\n', encoding="utf-8")
        lines, _ = read_complete_lines(self.log, 0, chunk_bytes=8)
        self.assertEqual(len(lines), 2)
        self.assertIn("ñññ", lines[0][1])

    def test_archivo_inexistente(self):
        lines, offset = read_complete_lines(self.log_dir / "nope.log", 0)
        self.assertEqual((lines, offset), ([], 0))

    def test_crlf_conserva_los_offsets_reales(self):
        self.log.write_bytes(b"line-one\r\nline-two\r\nline-three\r\n")
        lines, offset = read_complete_lines(self.log, 0)
        self.assertEqual([pos for pos, _ in lines], [0, 10, 20])
        self.assertEqual([text for _, text in lines], ["line-one", "line-two", "line-three"])
        self.assertEqual(offset, 32)

    def test_finales_de_linea_mixtos(self):
        self.log.write_bytes(b"a\r\nb\nc\r\n")
        lines, offset = read_complete_lines(self.log, 0)
        self.assertEqual([pos for pos, _ in lines], [0, 3, 5])
        self.assertEqual([text for _, text in lines], ["a", "b", "c"])
        self.assertEqual(offset, 8)

    def test_crlf_a_traves_de_chunks_de_un_byte(self):
        self.log.write_bytes(b"aa\r\nbb\r\n")
        lines, offset = read_complete_lines(self.log, 0, chunk_bytes=1)
        self.assertEqual([pos for pos, _ in lines], [0, 4])
        self.assertEqual([text for _, text in lines], ["aa", "bb"])
        self.assertEqual(offset, 8)

    def test_linea_sin_newline_no_avanza_el_offset(self):
        self.log.write_bytes(b"X" * 64)
        lines, offset = read_complete_lines(self.log, 0)
        self.assertEqual((lines, offset), ([], 0))

    def test_linea_gigante_se_emite_truncada_al_encontrar_el_newline(self):
        with mock.patch("app.ingest.reader.MAX_ENTRY_BYTES", 32):
            self.log.write_bytes(b"X" * 100 + b"\n")
            lines, offset = read_complete_lines(self.log, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(offset, 101)
        self.assertLessEqual(len(lines[0][1]), 33)

    def test_iter_entries_respeta_el_offset_inicial(self):
        self.append(WLS.format(n=1) + WLS.format(n=2))
        primero = self.log.stat().st_size
        entries, _ = read_complete_lines(self.log, 0)
        largo_primero = len(entries[0][1]) + 1

        tail, offset = iter_entries(self.log, largo_primero, self.registry)
        self.assertEqual(len(tail), 1)
        self.assertEqual(offset, primero)
        self.assertIn("numero 2", tail[0].lines[0])


class ProgressGuardTests(IngestBase):
    """`update_progress` no puede mover una fila que ya no es ese archivo."""

    def test_no_toca_una_fila_cuyo_inodo_cambio(self):
        self.append(WLS.format(n=1))
        state = resolve_log_file(self.conn, self.log)
        inode = os.stat(self.log).st_ino

        self.assertTrue(update_progress(self.conn, state.file_id, offset=10, size=10, inode=inode))
        self.assertFalse(
            update_progress(self.conn, state.file_id, offset=20, size=20, inode=inode + 1),
            "un inodo distinto no debe avanzar el offset",
        )
        self.assertEqual(last_offset(self.conn, state.file_id), 10)

    def test_rotacion_durante_la_lectura_conserva_la_identidad(self):
        """Rotar entre resolver y leer no puede mezclar dos archivos.

        Es el TOCTOU que hacía inoperante la guarda anterior: se resolvía el
        inode de `app.log`, se rotaba, y luego `open(path)` leía el archivo
        nuevo mientras se actualizaba la fila del viejo. El lector ahora usa el
        descriptor abierto *antes* de resolver, así que sigue leyendo el
        contenido cuya identidad registró.
        """
        self.append(WLS.format(n=1))
        real_resolve = resolve_log_file

        def resolve_then_rotate(conn, path, **kwargs):
            state = real_resolve(conn, path, **kwargs)
            os.rename(path, path.with_name("app.log.1"))
            path.write_text(WLS.format(n=2), encoding="utf-8")
            return state

        with mock.patch("app.ingest.ingestor.resolve_log_file", side_effect=resolve_then_rotate):
            first = ingest_file(self.conn, self.log, self.registry)

        self.assertEqual(first.counts.inserted, 1)
        rotated = self.log_dir / "app.log.1"
        old_inode = os.stat(rotated).st_ino
        row = self.conn.execute(
            "SELECT last_offset FROM log_files WHERE inode = ?", (old_inode,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(row["last_offset"], 0, "el progreso del contenido leído debe avanzar")

        second = ingest_file(self.conn, self.log, self.registry)
        self.assertEqual(second.counts.inserted, 1, "el archivo nuevo se ingiere aparte")
        again = ingest_file(self.conn, rotated, self.registry)
        self.assertEqual(again.counts.inserted, 0, "el rotado no se reingiere")
        self.assertEqual(count_events(self.conn), 2, "no hay eventos duplicados ni perdidos")
        inodes = self.conn.execute("SELECT COUNT(DISTINCT inode) FROM log_files").fetchone()[0]
        self.assertEqual(inodes, 2, "cada archivo conserva su propia fila")


class CopytruncateTest(IngestBase):
    def test_copia_no_reingiere_el_prefijo_ya_ingerido(self):
        """`cp app.log app.log.1` + truncar app.log no puede duplicar eventos.

        La copia tiene un inode nuevo, así que por identidad parece un archivo
        nuevo. Sin heredar el offset de la fila con la misma cabecera, se
        releería todo el prefijo bajo otro `file_id`.
        """
        self.append(WLS.format(n=1) + WLS.format(n=2))
        ingest_file(self.conn, self.log, self.registry)
        self.assertEqual(count_events(self.conn), 2)

        copia = self.log_dir / "app.log.1"
        copia.write_bytes(self.log.read_bytes())  # cp
        self.log.write_text("", encoding="utf-8")  # truncate
        self.append(WLS.format(n=3))

        ingest_paths(self.conn, self.registry, [self.log, copia])

        self.assertEqual(count_events(self.conn), 3, "el prefijo copiado no se reingiere")
        fila = self.conn.execute(
            "SELECT last_offset FROM log_files WHERE path = ?", (str(copia.resolve()),)
        ).fetchone()
        self.assertIsNotNone(fila)
        self.assertGreater(fila["last_offset"], 0, "la copia hereda el offset del original")


class BatchReaderTest(IngestBase):
    def test_lotes_respetan_el_limite_y_no_cortan_entradas(self):
        self.append(
            "".join(WLS.format(n=n) for n in range(1, 6))
            + "Caused by: java.sql.SQLException: timeout\n"
        )
        with self.log.open("rb") as handle:
            batches = list(iter_entry_batches(handle, 0, self.registry, max_entries=2))

        self.assertTrue(all(len(batch) <= 2 for batch, _ in batches))
        offsets = [entry.offset for batch, _ in batches for entry in batch]
        self.assertEqual(offsets, sorted(offsets), "las entradas se emiten en orden")
        self.assertEqual(len(offsets), 5)
        self.assertEqual(batches[-1][1], self.log.stat().st_size)

    def test_lote_por_bytes_tambien_acota(self):
        self.append("".join(WLS.format(n=n) for n in range(1, 6)))
        with self.log.open("rb") as handle:
            batches = list(
                iter_entry_batches(
                    handle,
                    0,
                    self.registry,
                    max_entries=1000,
                    max_bytes=len(WLS.format(n=1).encode()) * 2,
                )
            )
        self.assertGreater(len(batches), 1)


class PartialCommitTest(IngestBase):
    def test_fallo_en_un_lote_conserva_lo_confirmado(self):
        """El offset solo avanza para los lotes que llegaron a insertarse."""
        self.append("".join(WLS.format(n=n) for n in range(1, 4)))
        real_batches = iter_entry_batches
        real_insert = insert_events
        calls = {"n": 0}

        def one_entry_per_batch(handle, start_offset, registry, chunk_bytes):
            return real_batches(handle, start_offset, registry, chunk_bytes, max_entries=1)

        def insert_first_then_fail(conn, rows):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("fallo simulado")
            return real_insert(conn, rows)

        with (
            mock.patch(
                "app.ingest.ingestor.iter_entry_batches",
                side_effect=one_entry_per_batch,
            ),
            mock.patch(
                "app.ingest.ingestor.insert_events",
                side_effect=insert_first_then_fail,
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            ingest_file(self.conn, self.log, self.registry)

        self.assertEqual(count_events(self.conn), 1)
        self.assertEqual(
            resolve_log_file(self.conn, self.log).offset,
            len(WLS.format(n=1)),
            "el offset queda en el final del último lote confirmado",
        )

        run = ingest_file(self.conn, self.log, self.registry)
        self.assertEqual(run.counts.inserted, 2, "la siguiente pasada reintenta el resto")
        self.assertEqual(count_events(self.conn), 3)


class TimezoneCliTest(TempDbTestCase):
    def test_cli_aplica_default_timezone(self):
        """El timestamp naive debe interpretarse en la zona configurada."""
        from contextlib import redirect_stdout

        from app.ingest.__main__ import main as ingest_main

        log = self.log_dir / "app.log"
        log.write_text(
            '{"timestamp":"2026-10-05 08:03:22","level":"INFO","message":"naive"}\n',
            encoding="utf-8",
        )
        config = self.root / "config.yaml"
        config.write_text(
            "paths:\n"
            f"  log_dir: {self.log_dir}\n"
            f"  db_path: {self.db_path}\n"
            f"  export_dir: {self.export_dir}\n"
            "ingest:\n"
            "  default_timezone: America/Mexico_City\n",
            encoding="utf-8",
        )

        with redirect_stdout(io.StringIO()):
            code = ingest_main(["--once", "--config", str(config)])
        self.assertEqual(code, 0)

        conn = self.connect()
        stamp = conn.execute("SELECT timestamp_utc FROM events").fetchone()[0]
        self.assertEqual(stamp, "2026-10-05T14:03:22.000Z")


class StatusCliTest(TempDbTestCase):
    migrate_db = False

    def test_status_no_migra_ni_crea_la_base(self):
        from contextlib import redirect_stdout

        from app.ingest.__main__ import main as ingest_main

        config = self.root / "config.yaml"
        config.write_text(
            "paths:\n"
            f"  log_dir: {self.log_dir}\n"
            f"  db_path: {self.db_path}\n"
            f"  export_dir: {self.export_dir}\n",
            encoding="utf-8",
        )

        out = io.StringIO()
        with redirect_stdout(out):
            code = ingest_main(["--status", "--config", str(config)])

        self.assertEqual(code, 0)
        self.assertIn("sin migrar", out.getvalue())
        self.assertFalse(self.db_path.exists(), "--status es de solo lectura")


if __name__ == "__main__":
    unittest.main(verbosity=2)
