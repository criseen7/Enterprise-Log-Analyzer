"""Utilidades compartidas por los tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import TestCase

from app.config import Settings, load_settings
from app.db.connection import connect
from app.db.migrations import migrate

REPO_ROOT = Path(__file__).resolve().parents[1]
PATTERNS_DIR = REPO_ROOT / "app" / "parser" / "patterns"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TempDbTestCase(TestCase):
    """Base con una DB temporal ya migrada y un log_dir vacío.

    Cada test corre contra su propio directorio: los tests de concurrencia y de
    idempotencia necesitan Aislamiento real, no mocks.
    """

    migrate_db = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ela-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.log_dir = self.root / "logs"
        self.export_dir = self.root / "exports"
        self.log_dir.mkdir()
        self.export_dir.mkdir()

        self.db_path = self.root / "data" / "test.db"
        if self.migrate_db:
            migrate(self.db_path)

    def settings(self, **db_overrides) -> Settings:
        """Settings apuntando al sandbox temporal.

        Se construyen explícitamente en vez de tocar variables de entorno porque
        `get_settings()` cachea: mutar os.environ contaminaría otros tests.
        """
        base = load_settings()
        return Settings(
            project_root=self.root,
            paths=type(base.paths)(
                log_dir=self.log_dir,
                db_path=self.db_path,
                export_dir=self.export_dir,
                patterns_dir=base.paths.patterns_dir,
            ),
            ingest=base.ingest,
            db=type(base.db)(**{**base.db.__dict__, **db_overrides}),
            analysis=base.analysis,
            ui=base.ui,
        )

    def connect(self) -> connect:
        conn = connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def write_log(self, name: str, lines: list[str]) -> Path:
        path = self.log_dir / name
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        return path


class EnvSandbox:
    """Aplica variables de entorno y las restaura al salir."""

    def __init__(self, **values: str) -> None:
        self._values = {k: v for k, v in values.items() if v is not None}
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> EnvSandbox:
        for key, value in self._values.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc) -> None:
        for key, previous in self._saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
