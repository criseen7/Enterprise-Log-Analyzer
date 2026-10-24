"""Guarda de pureza del motor (ADR-005).

La tesis del proyecto cabe en una frase: **el motor determinista es la fuente de
la verdad; el LLM investiga y redacta, nunca detecta**. Esa tesis no se
sostiene por convención, se sostiene por una comprobación automática.

Este test recorre el código del motor y verifica que **no importa** ninguna
dependencia de LLM ni de transporte: ni `anthropic`, ni `openai`, ni `litellm`,
ni `httpx`, ni el propio SDK de MCP. Tampoco importa la capa agentic
(`app.mcp`, `app.agent`): la dependencia va en un solo sentido, de la capa
agentic hacia el motor, nunca al revés.

Por qué importa que sea un test y no un comentario: un comentario se puede
dejar sin leer y una convención se puede romper sin querer —un `import
anthropic` de tres líneas para "probar algo" es un accidente típico. Un test
que falla en CI no.

El escaneo es por **AST**, no por `sys.modules`: así se ven también los imports
dentro de funciones, en bloques `TYPE_CHECKING` y en `try/except ImportError`,
que son justo los sitios donde se cuela una dependencia sin que nadie lo note.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"

# Dependencias que el motor no puede tener. `httpx` y `httpx2` están aquí
# porque un cliente HTTP es el primer paso de "voy a preguntar a un modelo".
FORBIDDEN_ROOTS = frozenset({"anthropic", "openai", "litellm", "httpx", "httpx2", "mcp"})

# Paquetes de `app/` que forman la capa agentic. Pueden (y deben) depender de
# un LLM y del SDK de MCP: para eso existen.
AGENTIC_PACKAGES = ("app.mcp", "app.agent")


def _is_agentic(module: str) -> bool:
    return any(
        module == package or module.startswith(package + ".") for package in AGENTIC_PACKAGES
    )


def _imported_modules(tree: ast.AST) -> set[str]:
    """Módulos absolutos importados en un árbol de sintaxis.

    Los imports relativos (`from . import x`) se ignoran: no pueden salir de
    `app/`.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")
    return modules


def _engine_modules() -> list[Path]:
    """Módulos de `app/` que forman el motor."""
    found: list[Path] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        relative = path.relative_to(APP_DIR)
        if relative.parts and relative.parts[0] in {"mcp", "agent"}:
            continue
        found.append(path)
    return found


def _violations(path: Path, source: str) -> list[str]:
    """Dependencias prohibidas que aparecen en un módulo."""
    tree = ast.parse(source, filename=str(path))
    found: list[str] = []
    for module in sorted(_imported_modules(tree)):
        root = module.split(".")[0]
        if root in FORBIDDEN_ROOTS:
            found.append(module)
        elif _is_agentic(module):
            found.append(module)
    return found


class EnginePurityTests(unittest.TestCase):
    def test_el_motor_no_importa_dependencias_de_llm_ni_de_transporte(self):
        offenders: list[str] = []
        for path in _engine_modules():
            source = path.read_text(encoding="utf-8")
            for module in _violations(path, source):
                relative = path.relative_to(REPO_ROOT)
                offenders.append(f"{relative} importa {module}")
        self.assertEqual(
            offenders,
            [],
            "El motor debe permanecer determinista. Dependencias prohibidas "
            "encontradas:\n  " + "\n  ".join(offenders),
        )

    def test_la_capa_agentic_si_puede_usar_el_sdk_de_mcp(self):
        """Comprobación de que la guarda no está prohibiendo de más.

        Si el guard se endureciera sin querer y el servidor MCP dejara de poder
        importar `mcp`, este test seguiría pasando. Éste no.
        """
        server = APP_DIR / "mcp" / "server.py"
        self.assertTrue(server.is_file(), "falta app/mcp/server.py")
        modules = _imported_modules(ast.parse(server.read_text(encoding="utf-8")))
        self.assertTrue(
            any(m == "mcp" or m.startswith("mcp.") for m in modules),
            "app/mcp/server.py debería importar el SDK de MCP",
        )

    def test_el_escaner_detecta_una_violacion(self):
        """Auto-prueba del propio escáner.

        Un guard que no puede fallar no es un guard: si el AST dejara de
        reportar imports, este test seguiría en verde mientras el motor se
        llenaba de dependencias prohibidas.
        """
        with tempfile.TemporaryDirectory(prefix="purity-") as tmp:
            sample = Path(tmp) / "sample.py"
            sample.write_text(
                "import anthropic\n"
                "from openai import OpenAI\n"
                "from app.mcp import hitl\n"
                "import sqlite3\n",
                encoding="utf-8",
            )
            found = _violations(sample, sample.read_text(encoding="utf-8"))
        self.assertIn("anthropic", found)
        self.assertIn("openai", found)
        self.assertIn("app.mcp", found)
        self.assertNotIn("sqlite3", found)


class DependencyDirectionTests(unittest.TestCase):
    def test_la_capa_agentic_depende_del_motor_y_al_reves_no(self):
        """El motor no puede importar la capa agentic, y la capa agentic sí
        puede importar el motor. La flecha va en un solo sentido."""
        engine_imports_agentic = False
        agentic_imports_engine = False

        for path in _engine_modules():
            for module in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
                if _is_agentic(module):
                    engine_imports_agentic = True

        for package in ("mcp", "agent"):
            directory = APP_DIR / package
            if not directory.is_dir():
                continue
            for path in directory.rglob("*.py"):
                for module in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
                    if module.startswith("app.") and not _is_agentic(module):
                        agentic_imports_engine = True

        self.assertFalse(
            engine_imports_agentic,
            "el motor no debe importar la capa agentic: la dependencia va "
            "de la capa agentic hacia el motor",
        )
        self.assertTrue(
            agentic_imports_engine,
            "la capa agentic debería apoyarse en el motor, no reimplementarlo",
        )


if __name__ == "__main__":
    unittest.main()
