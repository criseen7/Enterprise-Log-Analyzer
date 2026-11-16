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
from datetime import timedelta

from app.config import Settings, get_settings
from app.db.connection import connection
from app.timeutil import canonical, parse_utc, to_canonical

# Tope por defecto de una ventana que se materializa hora a hora. Treinta y un
# días son 744 buckets: suficiente para una investigación, muy lejos de los
# millones de strings que produciría un rango abierto. Se puede ajustar con
# `analysis.max_window_days`.
DEFAULT_MAX_WINDOW_DAYS = 31


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


def max_window() -> timedelta:
    """Span máximo configurado para una ventana que se enumera por horas."""
    return timedelta(days=current_settings().analysis.max_window_days)


def clamp_bounds(lo: str, hi: str, *, max_span: timedelta | None = None) -> tuple[str, str]:
    """Recorta `[lo, hi)` al tramo más reciente que quepa en el tope.

    Se usa cuando el rango lo deriva el propio código (el histórico de una
    huella, la ventana de un incidente): ahí no hay una petición externa que
    rechazar, sólo una serie que no puede crecer sin límite.
    """
    span = max_span or max_window()
    lo_dt, hi_dt = parse_utc(lo), parse_utc(hi)
    if lo_dt is None or hi_dt is None or hi_dt - lo_dt <= span:
        return lo, hi
    return to_canonical(hi_dt - span), hi


def bounded_bounds(
    conn: sqlite3.Connection,
    since: str | None,
    until: str | None,
    *,
    max_span: timedelta | None = None,
) -> tuple[str | None, str | None]:
    """Normaliza y acota una ventana antes de enumerarla hora a hora.

    Reglas:

    * Dos extremos explícitos: si el span supera el tope, **falla**. Recortar
      una petición explícita en silencio haría que el resultado describiera una
      ventana distinta de la pedida.
    * Un extremo o ninguno: se completa con los límites del dataset y se
      conserva el tramo más reciente que quepa. Un agente que pide "todo" no
      puede provocar que el servidor materialice años de buckets.
    """
    span = max_span or max_window()
    lo, hi = resolve_bounds(since, until)
    if lo is not None and hi is not None:
        lo_dt, hi_dt = parse_utc(lo), parse_utc(hi)
        if lo_dt is not None and hi_dt is not None and hi_dt - lo_dt > span:
            raise ValueError(
                f"ventana demasiado grande: máximo {span.days} días. "
                "Reduce since/until o divide la consulta en tramos."
            )
        return lo, hi

    row = conn.execute(
        """
        SELECT MIN(timestamp_utc) AS lo, MAX(timestamp_utc) AS hi
          FROM events
         WHERE timestamp_utc IS NOT NULL
        """
    ).fetchone()
    if row is None or not row["lo"]:
        return lo, hi

    data_lo = str(row["lo"])
    data_hi = parse_utc(str(row["hi"]))
    if data_hi is None:
        return lo, hi
    data_hi_exclusive = to_canonical(data_hi + timedelta(milliseconds=1))

    if hi is None:
        hi = data_hi_exclusive
    if lo is None:
        hi_dt = parse_utc(hi)
        lo = to_canonical(hi_dt - span) if hi_dt is not None else hi
    if lo < data_lo:
        lo = data_lo
    return lo, hi


def current_settings() -> Settings:
    """Configuración resuelta. Se relee en cada llamada a propósito: los tests
    cambian `ELA_*` entre casos y una caché de módulo los dejaría colgados."""
    return get_settings()


__all__ = [
    "DEFAULT_MAX_WINDOW_DAYS",
    "bounded_bounds",
    "clamp_bounds",
    "current_settings",
    "max_window",
    "read_connection",
    "resolve_bounds",
    "write_connection",
]
