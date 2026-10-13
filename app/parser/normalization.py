"""Normalización de timestamps y niveles.

Dos reglas que el resto del sistema da por hechas:

1. **Todo timestamp que entra a la base es UTC ISO-8601.** El valor original y
   la zona detectada se conservan aparte. Sin esto, "errores por hora" mezcla
   horas de servicios en zonas distintas y el gráfico no significa nada.
2. **Un timestamp ilegible no descarta la línea.** Se devuelve `None` y el
   evento queda con `parsed_ok=0`; se conserva y se reporta.

El manejo de meses en inglés merece una nota: `datetime.strptime` resuelve `%b`
con la configuración regional del proceso (`LC_TIME`). Un contenedor con locale
`es_ES.UTF-8` deja de entender `Oct`, y Apache siempre escribe meses en inglés.
Por eso, cuando el patrón declara `english_months: true`, los nombres de mes se
sustituyen por su número *antes* de llamar a `strptime`, y el formato se declara
con `%m`. Así el parseo no depende del locale.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ISO8601 = "iso8601"

_MONTHS_EN = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = re.compile(r"\b(" + "|".join(_MONTHS_EN) + r")\b", re.IGNORECASE)

# Formatos con zona explícita: el resultado ya es aware y no hay que adivinar.
_TZ_AWARE = re.compile(r"(%|Z|[+-]\d{2}:?\d{2})")


class TimestampError(Exception):
    """El timestamp no se pudo interpretar con los formatos declarados."""


def replace_english_months(value: str) -> str:
    """`10/Oct/2026` → `10/10/2026`. Locale-independent."""
    return _MONTH_RE.sub(lambda m: f"{_MONTHS_EN[m.group(1).lower()]:02d}", value)


def _has_timezone(fmt: str) -> bool:
    return bool(_TZ_AWARE.search(fmt))


def _assume_timezone(value: str, default_tz: str) -> datetime:
    """Adjunta la zona por defecto cuando el formato no declara ninguna."""
    try:
        tz = ZoneInfo(default_tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TimestampError(f"zona por defecto inválida: {default_tz!r}") from exc
    return value.replace(tzinfo=tz)


def parse_timestamp(
    raw: str,
    formats: tuple[str, ...],
    *,
    default_timezone: str = "UTC",
    english_months: bool = False,
) -> tuple[datetime, str]:
    """Interpreta `raw` y devuelve `(momento en UTC, zona de origen)`.

    `zone_of_origin` es el offset que declaraba el log si lo declaraba, o el
    nombre de la zona asumida si hubo que suponerlo.
    """
    if raw is None:
        raise TimestampError("timestamp vacío")

    value = str(raw).strip()
    if not value:
        raise TimestampError("timestamp vacío")

    if english_months:
        value = replace_english_months(value)

    errors: list[str] = []
    for fmt in formats:
        try:
            if fmt == ISO8601:
                parsed = datetime.fromisoformat(value)
            else:
                parsed = datetime.strptime(value, fmt)
        except ValueError as exc:
            errors.append(f"{fmt}: {exc}")
            continue

        assumed = False
        if parsed.tzinfo is None:
            parsed = _assume_timezone(parsed, default_timezone)
            assumed = True

        source = default_timezone if assumed else _format_offset(parsed)
        return parsed.astimezone(UTC), source

    raise TimestampError(
        f"ningún formato coincide con {raw!r}: " + "; ".join(errors)
    )


def _format_offset(value: datetime) -> str:
    """`+0200` / `-07:00`, para poder reconstruir la zona original."""
    offset = value.utcoffset()
    if offset is None:
        return "UTC"
    if offset.total_seconds() == 0:
        return "UTC"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}{minutes:02d}"


def to_utc_iso(value: datetime) -> str:
    """Formato canónico que usa la base: `2026-10-05T14:03:22.481Z`.

    Longitud fija y sufijo `Z` constante: es lo que permite que el orden
    lexicográfico de TEXT coincida con el orden cronológico en SQLite.

    Los milisegundos se emiten siempre, incluso cuando son `.000`. Omitirlos
    rompe la premisa: `'...22.481Z' < '...22Z'` porque `.` < `Z`, así que dos
    timestamps del mismo segundo se ordenarían al revés.
    """
    as_utc = value.astimezone(UTC)
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def floor_to_bucket(value: datetime, bucket: str) -> datetime:
    """Trunca a una granularidad: `hour` o `day`.

    Se usa en las agregaciones del dashboard para no agrupar por microsegundo.
    """
    as_utc = value.astimezone(UTC)
    if bucket == "hour":
        return as_utc.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return as_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "minute":
        return as_utc.replace(second=0, microsecond=0)
    raise ValueError(f"granularidad no soportada: {bucket!r}")