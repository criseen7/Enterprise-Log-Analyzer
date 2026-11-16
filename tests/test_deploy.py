"""El despliegue no publica superficies sin autenticación fuera del loopback.

Ni el dashboard ni el servidor MCP tienen autenticación propia, y ambos leen
logs. La única defensa de red es el binding, así que este archivo congela esa
decisión: un `8501:8501` o un `8000:8000` no puede volver por descuido, y el
camino sin Docker (Streamlit directo) también escucha sólo en 127.0.0.1.
"""

from __future__ import annotations

import pathlib
import tomllib
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
STREAMLIT_CONFIG = REPO_ROOT / ".streamlit" / "config.toml"
DOCKERFILE = REPO_ROOT / "Dockerfile"


class ComposeExposureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]

    def _ports(self, service: str) -> list[str]:
        entries = self.services[service].get("ports") or []
        self.assertTrue(entries, f"{service} debería publicar un puerto")
        return [str(entry) for entry in entries]

    def test_ningun_servicio_publica_fuera_del_loopback(self):
        published = [
            (service, str(entry))
            for service, spec in self.services.items()
            for entry in (spec.get("ports") or [])
        ]
        self.assertTrue(published)
        outside = sorted(
            f"{service}: {entry}"
            for service, entry in published
            if not entry.startswith("127.0.0.1:")
        )
        self.assertEqual(outside, [])

    def test_el_dashboard_publica_solo_en_loopback(self):
        for entry in self._ports("dashboard"):
            self.assertTrue(entry.startswith("127.0.0.1:"), entry)

    def test_el_mcp_publica_solo_en_loopback(self):
        for entry in self._ports("mcp"):
            self.assertTrue(entry.startswith("127.0.0.1:"), entry)

    def test_el_dashboard_arranca_en_solo_lectura(self):
        raw = str(self.services["dashboard"]["environment"]["ELA_ADMIN_MODE"])
        default = raw.rsplit(":-", 1)[-1].rstrip("}") if "${" in raw else raw
        self.assertEqual(default, "0")


class DashboardBindingTests(unittest.TestCase):
    def test_streamlit_directo_escucha_solo_en_loopback(self):
        config = tomllib.loads(STREAMLIT_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(config["server"]["address"], "127.0.0.1")

    def test_el_contenedor_escucha_en_todas_las_interfaces(self):
        """El binding abierto dentro del contenedor es intencional: el puerto se
        publica en loopback, y limitarlo a 127.0.0.1 rompería el port mapping."""
        self.assertIn("STREAMLIT_SERVER_ADDRESS=0.0.0.0", DOCKERFILE.read_text(encoding="utf-8"))
