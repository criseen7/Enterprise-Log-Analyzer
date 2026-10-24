"""Carga de prompts versionados desde `app/agent/prompts/`.

Los prompts son archivos `.md` y no strings en Python por una razón concreta:
los evals hacen ablación de prompt (A3) y necesitan poder versionar, comparar y
citar el artefacto exacto que produjo cada resultado. Un string enterrado en una
función no se puede diffear ni atribuir.

El nombre del archivo es el contrato: `planner.v1.md` se carga con
`load("planner", "v1")`. La versión viaja en la traza (`prompt_version`), así
que un resultado se puede reproducir con el prompt que lo generó.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).with_name("prompts")
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class PromptError(RuntimeError):
    """El prompt no existe o le falta un valor que el template espera."""


def load(name: str, version: str = "v1") -> tuple[str, str]:
    """Devuelve `(texto, etiqueta_de_versión)`. La etiqueta va a la traza."""
    path = PROMPTS_DIR / f"{name}.{version}.md"
    if not path.is_file():
        raise PromptError(f"no existe el prompt {path.name}")
    return path.read_text(encoding="utf-8"), f"{name}.{version}"


def render(template: str, **values: object) -> str:
    """Sustituye `{{clave}}`. Una clave sin valor es un error, no un hueco.

    Dejar `{{evidence}}` sin sustituir en el prompt que ve el modelo produciría
    una respuesta inventada sobre un hueco: mejor fallar antes de llamar.
    """
    missing = sorted({name for name in PLACEHOLDER.findall(template) if name not in values})
    if missing:
        raise PromptError(f"faltan valores para el prompt: {', '.join(missing)}")
    return PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), template)


def load_rendered(name: str, version: str = "v1", **values: object) -> tuple[str, str]:
    template, label = load(name, version)
    return render(template, **values), label


__all__ = ["PLACEHOLDER", "PROMPTS_DIR", "PromptError", "load", "load_rendered", "render"]
