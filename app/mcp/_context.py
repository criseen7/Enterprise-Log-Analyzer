"""Puente entre las tools MCP y la base de datos.

Dos responsabilidades, y ninguna más:

* **Abrir conexiones con el modo correcto.** Las tools de lectura usan
  `read_only=True`, que activa `PRAGMA query_only=ON`: un `UPDATE` dentro de
  una tool que sólo debía leer falla en SQLite en vez de colarse en silencio.
  La única tool que escribe usa una conexión con escritura.

* **Normalizar los tiempos en un solo sitio.** Los bounds temporales llegan de
  un agente, de un cliente HTTP o de una CLI, y llegan en cualquier formato.
  El motor los compara como texto contra el formato canónico de la base, así
  que convertirlos en un único punto es la diferencia entre "el agente pidió
  las 14:00" y "el agente pidió desde las 14:00 hasta el final del histórico".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import Settings, get_settings
from app.db.connection import connection
from app.timeutil import canonical


@contextmanager
def read_connection(settings: Settings | None = None) -> Iterator[sqlite3.Connection]:
    """Conexión de sólo lectura para las tools consultoras."""
    with connection(settings, read_only=True) as conn:
        yield conn


@contextmanager
def write_connection(settings: Settings | None = None) -> Iterator[sqlite3.Connection]:
    """Conexión con escritura. La usa sólo la tool mutante."""
    with connection(settings) as conn:
        yield conn


def _canonical_or_fail(value: str | None, field: str) -> str | None:
    """Normaliza un extremo temporal, o falla si no es un ISO-8601 válido.

    Falla en vez de devolver `None`: un bound malformado que pasara por
    `None` convertiría "el agente pidió un rango concreto" en "el agente pidió
    todo el histórico", que es un error silencioso y difícil de detectar.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise ValueError(f"{field} no puede estar vacío")
    parsed = canonical(text)
    if parsed is None:
        raise ValueError(
            f"{field} no es un ISO-8601 válido: {value!r}. "
            "Se espera algo como '2026-10-05T14:00:00Z'."
        )
    return parsed


def resolve_bounds(since: str | None, until: str | None) -> tuple[str | None, str | None]:
    """Normaliza un par de extremos y comprueba que la ventana no está vacía."""
    lo = _canonical_or_fail(since, "since")
    hi = _canonical_or_fail(until, "until")
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError(
            f"ventana vacía: since ({lo}) no es anterior a until ({hi}). "
            "El rango es semiabierto [since, until)."
        )
    return lo, hi


def current_settings() -> Settings:
    """Configuración resuelta. Se relee en cada llamada a propósito: los tests
    cambian `ELA_*` entre casos y una caché de módulo los dejaría colgados."""
    return get_settings()


__all__ = [
    "current_settings",
    "read_connection",
    "resolve_bounds",
    "write_connection",
]
