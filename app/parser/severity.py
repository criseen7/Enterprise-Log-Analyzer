"""Escala de severidad canónica.

Los niveles que escriben los distintos servicios no coinciden: IIS usa `Warning`
y `Critical`, Java usa `SEVERE` y `WARNING`, syslog usa `err` y `crit`. Todo se
normaliza a una sola escala numérica para poder filtrar y agregar con índices:

    UNKNOWN 0 · TRACE 10 · DEBUG 20 · INFO 30 · WARN 40 · ERROR 45 · FATAL 50

El número importa más que el nombre: el dashboard agrupa por `severity` y los
índices de `events` están construidos sobre ese entero.

`ERROR` es 45 y no 40 para que exista un hueco que permita insertar `ALERT`
(48) o `EMERGENCY` (55) después sin renumerar y invalidar el historial.
"""

from __future__ import annotations

UNKNOWN = 0
TRACE = 10
DEBUG = 20
INFO = 30
WARN = 40
ERROR = 45
FATAL = 50

SEVERITY_NAMES: dict[int, str] = {
    UNKNOWN: "UNKNOWN",
    TRACE: "TRACE",
    DEBUG: "DEBUG",
    INFO: "INFO",
    WARN: "WARN",
    ERROR: "ERROR",
    FATAL: "FATAL",
}

SEVERITY_BY_NAME: dict[str, int] = {name: value for value, name in SEVERITY_NAMES.items()}

# Alias observados en la práctica. Sin esto, cada formato nuevo trae su propio
# vocabulario y el dashboard termina con ocho nombres para el mismo concepto.
LEVEL_ALIASES: dict[str, str] = {
    # syslog
    "EMERGENCY": "FATAL",
    "EMERG": "FATAL",
    "ALERT": "FATAL",
    "CRIT": "FATAL",
    "CRITICAL": "FATAL",
    "PANIC": "FATAL",
    "ERR": "ERROR",
    "ERRO": "ERROR",
    "SEVERE": "ERROR",
    # Java / WebLogic
    "WARNING": "WARN",
    "FINE": "DEBUG",
    "FINER": "DEBUG",
    "FINEST": "TRACE",
    "CONFIG": "INFO",
    # IIS
    "INFORMATIONAL": "INFO",
    "INFORMATION": "INFO",
    "INFORMATIONAL_EVENT": "INFO",
    # syslog / generic
    "NOTICE": "INFO",
    "NOTE": "INFO",
    "VERBOSE": "DEBUG",
    "LOG_DEBUG": "DEBUG",
    "LOG_INFO": "INFO",
    "LOG_WARN": "WARN",
    "LOG_WARNING": "WARN",
    "LOG_ERROR": "ERROR",
    "LOG_ERR": "ERROR",
    "LOG_FATAL": "FATAL",
    "LOG_CRIT": "FATAL",
    "LOG_CRITICAL": "FATAL",
    # variantes con guiones/espacios que se limpian antes de buscar
    "LOGGING-LEVEL": "INFO",
}

# Niveles numéricos estilo syslog (facility.severity sin el facility).
#   0 emerg · 1 alert · 2 crit · 3 err · 4 warning · 5 notice · 6 info · 7 debug
_SYSLOG_SEVERITY = {
    0: "FATAL",
    1: "FATAL",
    2: "FATAL",
    3: "ERROR",
    4: "WARN",
    5: "INFO",
    6: "INFO",
    7: "DEBUG",
}


def canonical_level(raw: str | None) -> str:
    """Normaliza el texto de un nivel al nombre canónico.

    Devuelve `UNKNOWN` en lugar de fallar: un nivel irreconocible es un dato
    defectuoso, no una razón para descartar la línea.
    """
    if raw is None:
        return "UNKNOWN"
    token = str(raw).strip().strip("[]<>").strip()
    if not token:
        return "UNKNOWN"

    upper = token.upper()
    if upper in SEVERITY_BY_NAME:
        return upper
    if upper in LEVEL_ALIASES:
        return LEVEL_ALIASES[upper]

    # "TRACE2" / "DEBUG_1": base numérica + sufijo.
    stem = upper.rstrip("0123456789_").rstrip("_ ")
    if stem in SEVERITY_BY_NAME:
        return stem
    if stem in LEVEL_ALIASES:
        return LEVEL_ALIASES[stem]

    # syslog numérico, como string o como int.
    if upper.isdigit() and int(upper) in _SYSLOG_SEVERITY:
        return _SYSLOG_SEVERITY[int(upper)]

    return "UNKNOWN"


def severity_of(raw: str | None) -> int:
    """Nivel crudo (cualquier dialecto) → entero de la escala canónica."""
    return SEVERITY_BY_NAME[canonical_level(raw)]


def severity_name(value: int | None) -> str:
    if value is None:
        return "UNKNOWN"
    return SEVERITY_NAMES.get(value, "UNKNOWN")


def is_error_severity(value: int | None) -> bool:
    return (value or UNKNOWN) >= ERROR