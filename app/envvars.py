"""Nombres de variables de entorno que no siguen el prefijo `ELA_`.

Están aquí y no dispersos por los módulos porque `.env.example` se genera desde
esta metadata: una variable nueva sin documentar debe romper el test, no llegar
silenciosamente a un despliegue.
"""

from __future__ import annotations

ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
HITL_SECRET = "LOGLENS_HITL_SECRET"
TRACES_DIR = "LOGLENS_TRACES_DIR"

__all__ = ["ANTHROPIC_API_KEY", "HITL_SECRET", "TRACES_DIR"]
