"""Utilidades comunes a las tools MCP.

Dos cosas viven aquí y ninguna más:

* Las **anotaciones** de las tools, que el cliente lee para decidir cómo
  presentarlas: si una tool sólo lee, si destruye datos, si es idempotente.
  `set_incident_status` es la única que declara `destructiveHint=True`, y esa
  distinción es la que permite a un cliente pedir confirmación al usuario.

* El `guard`, que convierte fallos previsibles en `ToolError`.

Sobre por qué `guard` vive *dentro* del cuerpo de la tool y no envuelve la
función: `ToolError` es la única excepción cuyo mensaje llega al modelo (el SDK
la convierte en `is_error=True` con el texto en `content`); cualquier otra
excepción se trata como un crash y el modelo ve un mensaje genérico sin
detalle. Un wrapper exterior resolvería ese problema, pero rompería otro: el SDK
genera el JSON Schema de salida a partir del modelo de retorno, y resuelve las
anotaciones contra `__globals__` de la función. En un wrapper exterior eso es
el módulo base, no el de la tool, y el modelo de retorno dejaría de resolverse.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

MUTATING = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)

T = TypeVar("T")


def guard(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Ejecuta `fn` convirtiendo fallos en `ToolError` con un mensaje útil."""
    try:
        return fn(*args, **kwargs)
    except ToolError:
        raise
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise ToolError(f"error de base de datos: {exc}") from exc
    except Exception as exc:
        raise ToolError(f"fallo inesperado ({type(exc).__name__}): {exc}") from exc


__all__ = ["MUTATING", "READ_ONLY", "guard"]
