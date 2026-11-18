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

    def test_streamlit_no_publica_el_puerto_de_cors(self):
        """El dashboard lee logs y no tiene auth: CORS abierto permite que una
        página ajena lea sus respuestas desde el navegador del operador."""
        config = tomllib.loads(STREAMLIT_CONFIG.read_text(encoding="utf-8"))
        self.assertFalse(config["server"]["enableCORS"])

    def test_el_contenedor_tambien_desactiva_cors(self):
        """El Dockerfile no copia .streamlit/: la config del contenedor va por
        entorno. Sin esta variable el dashboard Docker seguiría con CORS abierto."""
        self.assertIn("STREAMLIT_SERVER_ENABLECORS=false", DOCKERFILE.read_text(encoding="utf-8"))


class ComposeHealthcheckTests(unittest.TestCase):
    def test_el_healthcheck_del_analyzer_exige_el_esquema_al_dia(self):
        services = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]
        test = " ".join(str(part) for part in services["analyzer"]["healthcheck"]["test"])
        self.assertIn("--healthy", test)
        self.assertNotIn("--status", test, "--status devuelve 0 aunque falte migrar")


class ComposeHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]

    def test_ningun_servicio_puede_ganar_privilegios(self):
        for name, spec in self.services.items():
            with self.subTest(service=name):
                self.assertIn("no-new-privileges:true", spec.get("security_opt", []))

    def test_todos_los_servicios_sueltan_todas_las_capacidades(self):
        for name, spec in self.services.items():
            with self.subTest(service=name):
                self.assertIn("ALL", spec.get("cap_drop", []))

    def test_el_rootfs_es_de_solo_lectura_con_tmpfs(self):
        for name, spec in self.services.items():
            with self.subTest(service=name):
                self.assertTrue(spec.get("read_only"), f"{name} necesita rootfs de sólo lectura")
                self.assertIn("/tmp", spec.get("tmpfs", []))


class HitlSecretPassThroughTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]

    def test_el_servidor_mcp_recibe_el_secreto_hitl_y_la_clave_del_agente(self):
        env = self.services["mcp"]["environment"]
        self.assertIn("LOGLENS_HITL_SECRET", env)
        self.assertIn("ANTHROPIC_API_KEY", env)

    def test_un_valor_ausente_no_fuerza_un_secreto_debajo(self):
        """Vacío es falsy en `load_secret`: cae al fichero 0600 del volumen.
        Un `${VAR}` sin default inyectaría '' y también caería al fichero, pero
        documentar el default evita confundir '' con un secreto configurado."""
        env = self.services["mcp"]["environment"]
        self.assertEqual(str(env["LOGLENS_HITL_SECRET"]), "${LOGLENS_HITL_SECRET:-}")
        self.assertEqual(str(env["ANTHROPIC_API_KEY"]), "${ANTHROPIC_API_KEY:-}")
