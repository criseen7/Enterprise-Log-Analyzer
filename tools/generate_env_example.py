"""Genera `.env.example` desde la metadata declarativa de configuración.

Los nombres `ELA_*` salen de los `field(metadata={"env": ...})` de `app/config.py`
y los que no llevan prefijo de `app/envvars.py`. Las descripciones viven aquí,
junto al generador: una variable nueva sin explicación rompe la generación en vez
de aparecer en blanco en el archivo que luego se copia a producción.

Uso: `make env-example` para regenerar, `python tools/generate_env_example.py
--check` para comprobar que está al día.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from app.config import (
    ENV_PREFIX,
    PROJECT_ROOT_ENV,
    AnalysisConfig,
    DatabaseConfig,
    IngestConfig,
    PathsConfig,
    UIConfig,
)
from app.envvars import ANTHROPIC_API_KEY, HITL_SECRET, TRACES_DIR

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / ".env.example"

SECTIONS: tuple[tuple[str, tuple[type, ...]], ...] = (
    ("Rutas", (PathsConfig,)),
    ("Ingesta", (IngestConfig,)),
    ("Base de datos", (DatabaseConfig,)),
    ("Análisis", (AnalysisConfig,)),
    ("Dashboard", (UIConfig,)),
)

EXTRA_SECTION = "Agente y HITL (sólo el contenedor mcp)"

EXTRA_VALUES: dict[str, str] = {
    HITL_SECRET: "cambia-esto-por-32-bytes-aleatorios-como-minimo",
    TRACES_DIR: "/data/traces",
    ANTHROPIC_API_KEY: "",
}

DOCUMENTATION: dict[str, tuple[str, ...]] = {
    PROJECT_ROOT_ENV: (
        "Ruta base del proyecto. Default: la raíz del repo (padre de app/).",
        "Necesario en Docker, donde el CWD no es el repo.",
    ),
    "ELA_LOG_DIR": ("Directorio de logs (solo lectura en Docker).",),
    "ELA_DB_PATH": ("Ruta del archivo SQLite. En Docker debe vivir en el volumen compartido.",),
    "ELA_EXPORT_DIR": ("Directorio de reportes exportados.",),
    "ELA_PATTERNS_DIR": ("Directorio de patrones de parseo declarativos.",),
    "ELA_INCLUDE_PATTERNS": (
        "Globs de logs a vigilar, separados por comas, relativos a ELA_LOG_DIR.",
    ),
    "ELA_POLL_INTERVAL_SECONDS": (),
    "ELA_DEBOUNCE_SECONDS": (),
    "ELA_MAX_LATENCY_SECONDS": ("Tope de espera aunque no llegue calma; evita lotes estancados.",),
    "ELA_CHUNK_BYTES": (),
    "ELA_DEFAULT_TIMEZONE": ("Timezone asumida si el log no declara la suya.",),
    "ELA_JOURNAL_MODE": (),
    "ELA_BUSY_TIMEOUT_MS": (),
    "ELA_SYNCHRONOUS": (),
    "ELA_ERROR_RULES_PATH": ("Reglas de clasificación de errores.",),
    "ELA_MIN_INCIDENT_SEVERITY": ("Severidad mínima para candidata a incidente (40 = WARN).",),
    "ELA_WINDOW_MINUTES": ("Ventana de evaluación de umbrales, en minutos.",),
    "ELA_INCIDENT_THRESHOLD": (
        "Eventos del mismo fingerprint en la ventana para abrir incidente.",
    ),
    "ELA_COOLDOWN_MINUTES": ("Silencio antes de re-notificar, en minutos.",),
    "ELA_ANOMALY_ZSCORE": ("Z-score mínimo para marcar un pico horario como anomalía.",),
    "ELA_ANOMALY_MIN_SAMPLES": ("Muestras mínimas por bucket antes de calcular el z-score.",),
    "ELA_MAX_WINDOW_DAYS": ("Tope de ventanas enumeradas hora a hora, en días (1..366).",),
    "ELA_PAGE_TITLE": (),
    "ELA_PAGE_ICON": (),
    "ELA_CACHE_TTL_SECONDS": (),
    "ELA_DEFAULT_PAGE_SIZE": (),
    "ELA_ADMIN_MODE": ("0 = sólo lectura (default). 1 habilita las acciones que escriben.",),
    HITL_SECRET: (
        "El servidor MCP y la CLI de aprobación comparten esta clave dentro del",
        "contenedor. Si se omite, se usa un fichero 0600 junto a la base (/data)",
        "que sobrevive a `docker compose down`. Un valor presente pero débil (<32",
        "bytes, poco variado o placeholder) hace fallar el arranque: no cae al fichero.",
    ),
    TRACES_DIR: ("Directorio de trazas JSONL del agente.",),
    ANTHROPIC_API_KEY: (
        "Sin clave el agente usa el planner heurístico y la síntesis determinista.",
    ),
}

HEADER = """# Enterprise Log Analyzer — overrides por variable de entorno
#
#   cp .env.example .env
#
# Las variables de entorno tienen PRIORIDAD sobre config.yaml.
# Solo es necesario definir lo que quieras sobreescribir.
# Generado con `make env-example`; no editar a mano.
"""


def _example(name: str, default: Any) -> str:
    if name == PROJECT_ROOT_ENV:
        return "/app"
    if isinstance(default, bool):
        return "1" if default else "0"
    if isinstance(default, tuple):
        return ",".join(str(item) for item in default)
    if default is None:
        return ""
    return str(default)


def config_variables() -> dict[str, str]:
    """Nombre de variable → valor de ejemplo, en el orden de la metadata."""
    result = {PROJECT_ROOT_ENV: _example(PROJECT_ROOT_ENV, None)}
    for _title, classes in SECTIONS:
        for cls in classes:
            for f in fields(cls):
                name = f.metadata.get("env")
                if not name:
                    continue
                if not name.startswith(ENV_PREFIX):
                    raise ValueError(f"{cls.__name__}.{f.name}: {name} no usa {ENV_PREFIX}")
                default = f.default if f.default is not MISSING else None
                result[name] = _example(name, default)
    return result


def extra_variables() -> dict[str, str]:
    return dict(EXTRA_VALUES)


def _section(title: str) -> str:
    header = f"# --- {title} "
    return header + "-" * max(0, 78 - len(header))


def render() -> str:
    variables = config_variables()
    extra = extra_variables()
    documented = set(DOCUMENTATION)
    if documented != set(variables) | set(extra):
        raise ValueError(
            f"DOCUMENTATION desincronizada: "
            f"faltan {sorted((set(variables) | set(extra)) - documented)}, "
            f"sobran {sorted(documented - set(variables) - set(extra))}"
        )

    lines = [HEADER.rstrip("\n"), ""]
    for index, (title, classes) in enumerate(SECTIONS):
        lines.append(_section(title))
        names = [PROJECT_ROOT_ENV] if index == 0 else []
        for cls in classes:
            names.extend(f.metadata["env"] for f in fields(cls) if f.metadata.get("env"))
        for name in names:
            lines.extend(f"# {comment}" for comment in DOCUMENTATION[name])
            lines.append(f"#{name}={variables[name]}")
        lines.append("")

    lines.append(_section(EXTRA_SECTION))
    for name, value in extra.items():
        lines.extend(f"# {comment}" for comment in DOCUMENTATION[name])
        lines.append(f"#{name}={value}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="falla si .env.example no está al día")
    args = parser.parse_args(argv)
    content = render()
    if args.check:
        if OUTPUT.read_text(encoding="utf-8") != content:
            print(".env.example desactualizado: ejecuta `make env-example`", file=sys.stderr)
            return 1
        print(".env.example al día")
        return 0
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"escrito {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
