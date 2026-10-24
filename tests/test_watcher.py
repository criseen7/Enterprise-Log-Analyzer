"""Watcher: coherencia entre el filtrado del handler y `discover()`.

El handler de watchdog valida las rutas de sus eventos con `_known_files`/
`_match_known`, que se apoyan en `Settings.log_files` — la misma fuente que usa
la ingesta por polling. Antes validaba por nombre base, así que con patrones que
llevan directorio (`arch/*.log`) ingería archivos de nivel superior que el
polling nunca vería. Estos tests fijan esa coherencia sin hilos ni watchdog.
"""

from __future__ import annotations

import dataclasses
import unittest

from app.config import IngestConfig
from app.ingest.watcher import _known_files, _match_known
from tests.helpers import TempDbTestCase

APACHE = '10.0.0.1 - - [05/Oct/2026:15:12:03 +0000] "GET /a HTTP/1.1" 200 12\n'


class KnownFilesTests(TempDbTestCase):
    def test_known_files_uses_patterns_is_file_and_resolution(self) -> None:
        (self.log_dir / "app.log").write_text(APACHE, encoding="utf-8")
        (self.log_dir / "app.log.1").write_text(APACHE, encoding="utf-8")
        (self.log_dir / "notes.txt").write_text("no es log\n", encoding="utf-8")
        (self.log_dir / "arch").mkdir()
        (self.log_dir / "arch" / "nested.log").write_text(APACHE, encoding="utf-8")

        conocidos = _known_files(self.settings())

        self.assertEqual(
            conocidos,
            {(self.log_dir / "app.log").resolve(), (self.log_dir / "app.log.1").resolve()},
            "solo los *.log de nivel superior; ni .txt ni subdirectorios",
        )

    def test_match_known_returns_resolved_path_for_known_file(self) -> None:
        path = self.log_dir / "app.log"
        path.write_text(APACHE, encoding="utf-8")
        conocidos = _known_files(self.settings())
        self.assertEqual(_match_known(str(path), conocidos), path.resolve())


class DirectoryPatternTests(TempDbTestCase):
    def _settings_with(self, patterns: tuple[str, ...]):
        base = self.settings()
        ingest: IngestConfig = dataclasses.replace(base.ingest, include_patterns=patterns)
        return dataclasses.replace(base, ingest=ingest)

    def test_top_level_file_not_matched_by_directory_pattern(self) -> None:
        """El caso que rompía la validación por nombre base.

        Con `arch/*.log`, `arch/app.log` aportaba el nombre `app.log`; un
        `app.log` de nivel superior pasaba el antiguo `fnmatch` por nombre y se
        ingería, aunque el glob nunca lo incluiría.
        """
        (self.log_dir / "arch").mkdir()
        nested = self.log_dir / "arch" / "app.log"
        nested.write_text(APACHE, encoding="utf-8")
        top = self.log_dir / "app.log"
        top.write_text(APACHE, encoding="utf-8")

        conocidos = _known_files(self._settings_with(("arch/*.log",)))

        self.assertEqual(conocidos, {nested.resolve()})
        self.assertIsNone(
            _match_known(str(top), conocidos),
            "el archivo de nivel superior no coincide con `arch/*.log`",
        )
        self.assertEqual(_match_known(str(nested), conocidos), nested.resolve())


class SymlinkTests(TempDbTestCase):
    def test_symlink_is_consistent_with_discover(self) -> None:
        outside = self.root / "outside.log"
        outside.write_text(APACHE, encoding="utf-8")
        link = self.log_dir / "app.log"
        try:
            link.symlink_to(outside)
        except OSError as exc:  # pragma: no cover - solo en FS sin symlinks
            self.skipTest(f"el sistema no soporta symlinks: {exc}")

        conocidos = _known_files(self.settings())

        self.assertEqual(conocidos, {outside.resolve()})
        self.assertEqual(
            _match_known(str(link), conocidos),
            outside.resolve(),
            "el symlink se resuelve y coincide con lo que ingiere `discover()`",
        )


class RejectionTests(TempDbTestCase):
    def test_rejects_empty_missing_and_outside_paths(self) -> None:
        known = self.log_dir / "app.log"
        known.write_text(APACHE, encoding="utf-8")
        conocidos = _known_files(self.settings())

        self.assertIsNone(_match_known(None, conocidos))
        self.assertIsNone(_match_known("", conocidos))

        outside = self.root / "elsewhere.log"
        outside.write_text(APACHE, encoding="utf-8")
        self.assertIsNone(_match_known(str(outside), conocidos))

    def test_rejects_cwd_file_when_not_in_log_dir(self) -> None:
        """`Path("")` es truthy: una ruta vacía no debe colar como el CWD."""
        conocidos = _known_files(self.settings())
        self.assertIsNone(_match_known("", conocidos))
        self.assertIsNone(_match_known(".", conocidos), "el CWD no está en log_dir")


if __name__ == "__main__":
    unittest.main()
