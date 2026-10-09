"""Fingerprint de evento: agrupar lo que en realidad es el mismo error.

El problema: "100 accesos fallidos" no son 100 errores distintos. Si cada
evento se agrupa por su mensaje tal cual, el dashboard muestra 100 barras de
una sola unidad y no se ve ningún patrón. La solución es normalizar el mensaje
—sustituir lo que varía de una ejecución a otra por un placeholder— y hashear
el resultado.

El orden de las sustituciones importa y no es cosmético:

    1. UUID, timestamp, IP     -> antes que los números, o se destrozan en
                                  pedazos y luego ya no se reconocen.
    2. hex largo / 0x...       -> antes que los números, por la misma razón:
                                  "0x1f" contiene dígitos y un "hex" tardío
                                  no recupera nada.
    3. números                 -> al final: ya no queda nada que proteger.

Nada se escribe en la base aquí: la función es pura y decide la huella de un
mensaje. La categoría entra en el hash a propósito — dos mensajes idénticos que
pertenecen a categorías distintas son errores distintos (ver ADR-004).
"""

from __future__ import annotations

import hashlib
import re

# Longitud de la huella en hex: 64 bits. Colisiones con un dataset de ~10^6
# eventos son despreciables y el string corto cabe en una tabla del dashboard.
FINGERPRINT_LENGTH = 16

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ISO_TS = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
# Formato de access log: 07/Oct/2026:18:11:13 +0000
_CLF_TS = re.compile(r"\b\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}\s*[+-]\d{4}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HEX_0X = re.compile(r"\b0x[0-9a-fA-F]+\b")
_HEX = re.compile(r"\b[0-9a-fA-F]{8,}\b")
# Sin límites de palabra: "user u123", "order_9871" o "30000ms" deben
# normalizarse aunque los dígitos peguen con letras. Con `\b` en ambos lados,
# "u123" no se toca y dos errores idénticos salvo el identificador acaban en
# fingerprints distintos, que es exactamente lo que la huella existe para evitar.
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def normalize_message(message: str | None) -> str:
    """Mensaje con lo variable sustituido por placeholders.

    Deliberadamente conservador: no se toca el caso ni se eliminan palabras.
    Agrupar "Connection refused" con "connection refused" es tentador, pero el
    mensaje puede llevar identificadores sensibles al caso y el error grave está
    en separar dos cosas distintas, no en unir de más.
    """
    if not message:
        return ""

    text = message
    text = _UUID.sub("<UUID>", text)
    text = _ISO_TS.sub("<TS>", text)
    text = _CLF_TS.sub("<TS>", text)
    text = _DATE.sub("<TS>", text)
    text = _IPV4.sub("<IP>", text)
    text = _HEX_0X.sub("<HEX>", text)
    text = _HEX.sub("<HEX>", text)
    text = _NUMBER.sub("<NUM>", text)
    return " ".join(text.split())


def event_fingerprint(category: str | None, message: str | None) -> str:
    """Huella estable de un evento: hash de `categoría + mensaje normalizado`.

    `category` entra en el hash porque un mensaje sin contexto no basta para
    identificar un error: la misma frase en categorías distintas es dos
    problemas distintos. Cuando no hay categoría el separador sigue ahí (cadena
    vacía), así que el resultado no cambia de esquema según venga o no de reglas.
    """
    payload = f"{category or ''}\n{normalize_message(message)}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


__all__ = ["FINGERPRINT_LENGTH", "event_fingerprint", "normalize_message"]
