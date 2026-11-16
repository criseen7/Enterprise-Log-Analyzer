"""Auditoría de las acciones de escritura del dashboard.

El dashboard es de sólo lectura salvo que se active `ui.admin_mode`, y cuando
lo está cada escritura queda registrada. El registro no va a una tabla nueva:
las acciones de UI son pocas, no necesitan consultas y mezclarlas con los datos
operativos ensuciaría el esquema. Van al logger `loglens.audit`, que cualquier
despliegue puede enrutar a su colector.

Un logger propio con handler y `propagate=False`: el logging de Streamlit no
garantiza que los `INFO` lleguen a ningún sitio, y una auditoría que se pierde
en silencio no audita nada.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

AUDIT_LOGGER_NAME = "loglens.audit"

_logger = logging.getLogger(AUDIT_LOGGER_NAME)
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def record(action: str, *, detail: str = "", **fields: Any) -> None:
    """Registra una acción de escritura con su contexto.

    `action` es un identificador corto y estable (`ingest`, `backfill`,
    `incident.status`); `fields` son los datos que permiten reconstruir qué se
    hizo sin abrir la base.
    """
    rendered = " ".join(f"{key}={value!r}" for key, value in sorted(fields.items()))
    _logger.info("action=%s detail=%s %s", action, detail, rendered)


__all__ = ["AUDIT_LOGGER_NAME", "record"]
