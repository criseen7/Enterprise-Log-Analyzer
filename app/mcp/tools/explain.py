"""`explain_fingerprint`: qué significa una huella.

Un fingerprint es un hash: opaco por diseño. `a3f9c2...` no dice nada, y un
agente que sólo ve hashes termina describiendo el hash en vez del problema.

Esta tool devuelve la **plantilla normalizada** del mensaje —el mensaje con lo
variable sustituido por `<UUID>`, `<IP>`, `<NUM>`— junto con la categoría y su
descripción. Eso es lo que convierte una huella en una frase legible: "no se
pudo conectar al pool de base de datos (<IP>:<NUM>)" en vez de un identificador
hexadecimal.
"""

from __future__ import annotations

import sqlite3

from app.analysis.errors import ErrorRules
from app.analysis.fingerprint import normalize_message
from app.config import get_settings
from app.mcp._context import read_connection
from app.mcp.schemas import ExplainResult
from app.mcp.tools._base import guard

# Cuántos ejemplos crudos devolver. Suficiente para reconocer el patrón y poco
# para no inundar el contexto del agente con líneas repetidas.
MAX_SAMPLES = 3
# Longitud máxima de una línea de ejemplo. Las líneas de log reales pueden ser
# kilobytes, y cinco de ésas unproductive.
MAX_SAMPLE_CHARS = 400


def _collect(conn: sqlite3.Connection, fingerprint: str, *, rules: ErrorRules) -> ExplainResult:
    row = conn.execute(
        """
        SELECT COUNT(*)                  AS n,
               MAX(category)             AS category,
               MAX(message)              AS sample_message,
               MIN(timestamp_utc)        AS first_seen,
               MAX(timestamp_utc)        AS last_seen
          FROM events
         WHERE fingerprint = ?
        """,
        (fingerprint,),
    ).fetchone()
    total = int(row["n"] or 0) if row else 0
    if total == 0:
        raise ValueError(
            f"no existe ninguna huella {fingerprint!r} en la base. "
            "Se obtiene de `top_errors` o de `search_events`."
        )

    services = [
        str(item["service"])
        for item in conn.execute(
            """
            SELECT service FROM events
             WHERE fingerprint = ? AND service IS NOT NULL
             GROUP BY service ORDER BY COUNT(*) DESC
            """,
            (fingerprint,),
        )
    ]

    raw_lines = [
        str(item["raw_line"])[:MAX_SAMPLE_CHARS]
        for item in conn.execute(
            """
            SELECT raw_line FROM events
             WHERE fingerprint = ?
             ORDER BY id
             LIMIT ?
            """,
            (fingerprint, MAX_SAMPLES),
        )
    ]

    template = normalize_message(row["sample_message"])
    category = row["category"]
    description = None
    for rule in rules.rules:
        if rule.category == category:
            description = rule.description or None
            break

    return ExplainResult(
        fingerprint=fingerprint,
        category=category,
        category_description=description,
        normalized_template=template,
        total_events=total,
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        sample_raw_lines=raw_lines,
        services=services,
    )


def explain_fingerprint(fingerprint: str) -> ExplainResult:
    """Explica una huella: plantilla normalizada, categoría y ejemplos reales.

    `normalized_template` es el mensaje con lo variable sustituido por
    placeholders (`<UUID>`, `<IP>`, `<NUM>`, `<HEX>`, `<TS>`). Es la forma en
    que el mensaje se repite, y por tanto la forma en que conviene reportarlo.

    `sample_raw_lines` son líneas tal cual llegaron, recortadas: sirven para
    confirmar el patrón cuando la plantilla no basta.
    """
    with read_connection() as conn:
        settings = get_settings()
        rules = ErrorRules.load(settings.analysis.rules_path)
        return guard(_collect, conn, fingerprint, rules=rules)


__all__ = ["explain_fingerprint"]
