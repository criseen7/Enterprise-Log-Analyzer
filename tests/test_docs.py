"""B-1/B-2: la documentación deriva del código, no de la memoria.

Tres fuentes se comprueban contra el código que describen: la lista de variables
de entorno contra la metadata de `app/config.py`, el número de servicios contra
`docker-compose.yml` y el número de tests contra los propios archivos de test.
Si alguna se desincroniza, el test falla antes de que el error llegue a un
despliegue.
"""

from __future__ import annotations

import ast
import re
import unittest
from dataclasses import fields
from pathlib import Path

import yaml

from app.agent import llm, trace
from app.config import (
    PROJECT_ROOT_ENV,
    AnalysisConfig,
    DatabaseConfig,
    IngestConfig,
    PathsConfig,
    UIConfig,
)
from app.mcp.hitl import SECRET_ENV_VAR

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE = REPO_ROOT / "docker-compose.yml"


def _source_files() -> list[Path]:
    roots = (REPO_ROOT / "app", REPO_ROOT / "evals")
    return sorted(path for root in roots for path in root.rglob("*.py"))


def _literal_env_vars() -> set[str]:
    """Variables leídas con un literal en el código (las demás van por metadata)."""
    pattern = re.compile(r"""os\.environ(?:\.get)?\(\s*["']([A-Z][A-Z0-9_]*)["']""")
    found: set[str] = set()
    for path in _source_files():
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    return found


def _test_count() -> int:
    return sum(
        1
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


class EnvExampleTests(unittest.TestCase):
    def test_el_archivo_esta_generado_desde_la_metadata(self):
        from tools.generate_env_example import render

        self.assertEqual(ENV_EXAMPLE.read_text(encoding="utf-8"), render())

    def test_la_metadata_cubre_todas_las_variables_del_codigo(self):
        from tools.generate_env_example import DOCUMENTATION, config_variables, extra_variables

        declared = set(config_variables()) | set(extra_variables()) | {PROJECT_ROOT_ENV}
        self.assertEqual(set(DOCUMENTATION), declared)
        for section in (PathsConfig, IngestConfig, DatabaseConfig, AnalysisConfig, UIConfig):
            for field in fields(section):
                name = field.metadata.get("env")
                if name:
                    self.assertIn(name, DOCUMENTATION, f"{section.__name__}.{field.name}")
        for name in (SECRET_ENV_VAR, trace.TRACES_ENV_VAR, llm.API_KEY_ENV_VAR):
            self.assertIn(name, DOCUMENTATION)
        self.assertEqual(_literal_env_vars() - declared, set())


class FiguresTests(unittest.TestCase):
    def _figure(self, path: str, pattern: str) -> int:
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        match = re.search(pattern, text)
        self.assertIsNotNone(match, f"no hay cifra de tests documentada en {path}")
        return int(match.group(1))

    def test_las_cifras_de_tests_coinciden_con_los_tests_reales(self):
        count = _test_count()
        for path, pattern in (
            ("README.md", r"tests/\s+(\d+) tests"),
            ("README.es.md", r"tests/\s+(\d+) tests"),
            ("docs/PLAN.md", r"Suite (\d+) verde"),
            ("pyproject.toml", r"existente \((\d+) tests\)"),
            ("docs/decisions/ADR-005-deterministic-engine.md", r"con (\d+) tests"),
        ):
            with self.subTest(path=path):
                self.assertEqual(self._figure(path, pattern), count)

    def test_los_documentos_nombran_los_tres_servicios_de_compose(self):
        compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        services = set(compose["services"])
        self.assertEqual(services, {"analyzer", "dashboard", "mcp"})
        for path in ("docs/PLAN.md", "AGENTS.md"):
            text = (REPO_ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                for service in services:
                    self.assertIn(f"`{service}`", text)


class PinPolicyTests(unittest.TestCase):
    def test_la_documentacion_no_recomienda_rangos_de_mcp(self):
        paths = [REPO_ROOT / "pyproject.toml", REPO_ROOT / "requirements.txt"]
        paths += sorted((REPO_ROOT / "docs").rglob("*.md"))
        paths += [REPO_ROOT / "README.md", REPO_ROOT / "README.es.md", REPO_ROOT / "AGENTS.md"]
        for path in paths:
            with self.subTest(path=path):
                self.assertNotIn("mcp>=", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
