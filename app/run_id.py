"""Validación compartida del identificador de una corrida del agente.

El `run_id` es el nombre del fichero de traza, tanto al escribirlo
(`app.agent.trace`) como al servirlo (`loglens://trace/{run_id}`). La misma regla
debe aplicar en los dos extremos: un lector más permisivo que el escritor abre
la puerta a rutas inesperadas si algún día cambia el sufijo del fichero.

Vive aquí y no en `app.agent` porque el resource MCP se importa desde
`app.mcp.server`, y `app.agent` importa de vuelta ese módulo: dejar la función
en el paquete del agente crearía un import circular.
"""

from __future__ import annotations

import re

_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")


def validate_run_id(run_id: str) -> None:
    """Acepta sólo nombres simples de archivo, ni `"."` ni `".."`."""
    if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
        raise ValueError(f"run_id inválido: {run_id!r}")


__all__ = ["validate_run_id"]
