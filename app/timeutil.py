"""Normalización de timestamps ISO-8601.

Existe por una razón concreta: en la base, `timestamp_utc` es **texto**, y se
compara como texto. El formato que se guarda es de longitud fija
(`2026-10-13T05:42:00.720Z`, siempre con milisegundos y siempre UTC), y por eso
el orden lexicográfico coincide con el cronológico.

Si se introduce un bound con otro formato, el orden se rompe en silencio. El
caso real: `'2026-10-05T14:00:00Z'` ordena *después* de
`'2026-10-05T14:00:00.481Z'`, porque `':'` (0x3A) es mayor que `'.'` (0x2E). Una
ventana que empieza a las 14:00 excluiría los eventos de las 14:00:00.481, y
nadie vería el error: la consulta devuelve menos filas y parece correcto.

Por eso todo bound externo —el que llega de un agente, de un cliente HTTP o de
una CLI— pasa por `canonical()` antes de tocar SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Formato canónico de la base: longitud fija, milisegundos, `Z` al final.
CANONICAL_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def parse_utc(value: str | None) -> datetime | None:
    """Parsea un ISO-8601 a UTC. `None` si no es válido.

    Acepta `Z`, offset explícito, milisegundos opcionales y separador `T` o
    espacio. Un timestamp **sin zona** se asume UTC: los logs mezclan formatos y
    adivinar la zona local del servidor sería peor que asumir UTC explícito.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def to_canonical(moment: datetime) -> str:
    """Un `datetime` en el formato de la base."""
    return moment.strftime(CANONICAL_FORMAT)[:-3] + "Z"


def canonical(value: str | None) -> str | None:
    """Normaliza a formato canónico. `None` si no parsea.

    `None` en la entrada es `None` en la salida: "sin límite" y "límite
    inválido" son cosas distintas, y esta función no las mezcla.
    """
    if value is None:
        return None
    moment = parse_utc(value)
    return to_canonical(moment) if moment is not None else None


def canonical_bounds(since: str | None, until: str | None) -> tuple[str | None, str | None]:
    """Normaliza un par de extremos. Preserva `None` en los que no vienen.

    No valida el orden: comparar `since` con `until` es política de cada tool,
    no del formato.
    """
    return canonical(since), canonical(until)


__all__ = [
    "CANONICAL_FORMAT",
    "canonical",
    "canonical_bounds",
    "parse_utc",
    "to_canonical",
]
