"""La cadena de publicación no ejecuta datos del evento ni hereda permisos.

El workflow de release corre con un tag como entrada. Un tag de git válido puede
contener comillas y punto y coma, así que interpolarlo dentro de un `awk` entre
comillas dobles es ejecución de comandos. Estos tests ejecutan el propio bloque
`run:` contra un CHANGELOG en un directorio temporal: comprueban el comportamiento,
no la forma del script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
CI = WORKFLOWS / "ci.yml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"
MAKEFILE = REPO_ROOT / "Makefile"
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _step_with(run: dict, predicate) -> dict:
    return next(step for step in run["steps"] if predicate(step))


class ReleaseNotesTests(unittest.TestCase):
    """El tag es dato, nunca código, y un CHANGELOG sin notas no publica."""

    def setUp(self) -> None:
        release = _load(RELEASE)
        self.script = _step_with(
            release["jobs"]["release"], lambda step: step.get("id") == "changelog"
        )["run"]

    def _execute(self, tag: str, workdir: Path) -> subprocess.CompletedProcess[str]:
        shutil.copy(REPO_ROOT / "CHANGELOG.md", workdir / "CHANGELOG.md")
        env = {
            **os.environ,
            "GITHUB_REF_NAME": tag,
            "GITHUB_OUTPUT": str(workdir / "github_output"),
        }
        return subprocess.run(
            ["bash", "-c", self.script],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_tag_valido_extrae_las_notas_de_su_version(self):
        with tempfile.TemporaryDirectory(prefix="ela-release-") as tmp:
            workdir = Path(tmp)
            result = self._execute("v0.1.0", workdir)
            self.assertEqual(result.returncode, 0, result.stderr)
            notes = (workdir / "RELEASE_NOTES.md").read_text(encoding="utf-8")
            self.assertIn("Deterministic engine", notes)
            self.assertNotIn("## [0.1.0]", notes)

    def test_tag_con_comandos_incrustados_no_ejecuta_nada(self):
        with tempfile.TemporaryDirectory(prefix="ela-release-") as tmp:
            workdir = Path(tmp)
            result = self._execute('v1.0";touch PWNED;#', workdir)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((workdir / "PWNED").exists(), "el tag se interpoló en el shell")

    def test_version_ausente_del_changelog_falla_el_paso(self):
        with tempfile.TemporaryDirectory(prefix="ela-release-") as tmp:
            result = self._execute("v9.9.9", Path(tmp))
            self.assertNotEqual(result.returncode, 0, "publicó una release sin notas")


class ReleaseJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = _load(RELEASE)
        self.jobs = self.workflow["jobs"]

    def test_permisos_por_defecto_son_de_solo_lectura(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertNotIn("id-token", self.workflow["permissions"])

    def test_solo_el_job_que_publica_puede_escribir(self):
        self.assertEqual(self.jobs["release"]["permissions"], {"contents": "write"})
        self.assertNotIn("permissions", self.jobs["build"])

    def test_dependencias_de_build_estan_fijadas(self):
        build = _step_with(self.jobs["build"], lambda step: "twine" in step.get("run", ""))
        self.assertIn("build==", build["run"])
        self.assertIn("twine==", build["run"])
        self.assertNotIn("--upgrade pip", build["run"])

    def test_las_prereleases_no_se_publican_como_estables(self):
        publication = _step_with(
            self.jobs["release"], lambda step: "softprops" in str(step.get("uses", ""))
        )
        self.assertIn("github.ref_name", publication["with"]["prerelease"])


class CiGateTests(unittest.TestCase):
    def test_el_smoke_de_imagen_migra_de_verdad(self):
        ci = _load(CI)
        smoke = _step_with(
            ci["jobs"]["docker"], lambda s: s.get("name") == "la imagen arranca y migra"
        )
        self.assertIn("python -m app.db.migrations", smoke["run"])
        self.assertNotIn("--status", smoke["run"], "--status no migra nada")
        self.assertIn("--healthy", smoke["run"])

    def test_hay_un_job_que_verifica_la_persistencia_de_la_base(self):
        ci = _load(CI)
        jobs = "\n".join(
            step.get("run", "") or "" for job in ci["jobs"].values() for step in job["steps"]
        )
        self.assertIn("docker compose up -d --wait", jobs)
        self.assertIn("docker compose down", jobs)
        self.assertIn("docker compose up -d --wait", jobs[jobs.find("docker compose down") :])


class SupplyChainTests(unittest.TestCase):
    def test_mcp_esta_fijado_una_sola_vez(self):
        requirements = REQUIREMENTS.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"^mcp==(\S+)", requirements, re.M), ["2.3.0"])
        self.assertNotIn("mcp>=", requirements)

    def test_pyproject_fija_la_herramienta_de_build_y_la_frontera(self):
        pyproject = PYPROJECT.read_text(encoding="utf-8")
        self.assertIn('requires = ["setuptools==', pyproject)
        self.assertIn('"mcp==2.3.0"', pyproject)
        self.assertNotIn("mcp>=", pyproject)

    def test_pip_esta_fijado_en_make_y_en_el_dockerfile(self):
        self.assertIn("pip install pip==", MAKEFILE.read_text(encoding="utf-8"))
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("pip install pip==", dockerfile)
        self.assertNotIn("--upgrade pip", dockerfile)

    def test_dependabot_vigila_actions_y_pip_cada_semana(self):
        config = _load(DEPENDABOT)
        updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}
        self.assertEqual(set(updates), {"github-actions", "pip"})
        for entry in updates.values():
            self.assertEqual(entry["schedule"]["interval"], "weekly")
            self.assertEqual(entry["directory"], "/")


if __name__ == "__main__":
    unittest.main()
