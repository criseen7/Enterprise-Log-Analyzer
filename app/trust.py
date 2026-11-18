"""Fronteras de confianza y redacción de valores sensibles.

La capa agentic recibe texto de logs, del modelo y de trazas anteriores. Ninguno
de esos textos puede convertirse en instrucciones: se delimita en bloques
explícitamente no confiables y se limpia antes de entrar en un prompt. La misma
frontera sirve para publicar trazas, que también vuelven al modelo como
resource MCP.

La redacción no depende sólo del nombre de la clave: un secreto copiado en
texto libre (`Authorization: Bearer sk-…`) debe desaparecer igual.
"""

from __future__ import annotations

import re
from typing import Any

MAX_UNTRUSTED_CHARS = 8_000
MAX_TRACE_CHARS = 60_000
REDACTED = "[REDACTED]"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG = re.compile(r"<\s*/?\s*[a-zA-Z][a-zA-Z0-9_-]*")
_TAG_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_TRUST_NAME = re.compile(r"^[a-z][a-z0-9-]*$")

_SECRET_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|token|secret|password|passwd|credential|authorization)"
    r"\b\s*[:=]\s*[\"']?([^\s\"',;}\]]+)"
)
_SECRET_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")
_SECRET_SK = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}")


def clean_untrusted(text: str, limit: int = MAX_UNTRUSTED_CHARS) -> str:
    """Neutraliza etiquetas y caracteres de control, y acota la longitud."""
    cleaned = _CONTROL.sub("", text)
    cleaned = _TAG.sub(lambda match: "&lt;" + match.group(0)[1:].lower(), cleaned)
    if len(cleaned) > limit:
        return f"{cleaned[:limit]}…[truncado {len(cleaned) - limit} caracteres]"
    return cleaned


def untrusted_block(
    tag: str,
    trust: str,
    value: str,
    *,
    limit: int = MAX_UNTRUSTED_CHARS,
) -> str:
    """Envuelve un valor derivado de datos en un bloque que no es instrucción."""
    if not _TAG_NAME.match(tag) or not _TRUST_NAME.match(trust):
        raise ValueError("etiqueta o nivel de confianza inválido")
    opening = f'<{tag} trust="{trust}">'
    closing = f"</{tag}>"
    return f"{opening}\n{clean_untrusted(value, limit)}\n{closing}"


def redact_text(text: str) -> str:
    """Sustituye credenciales aunque aparezcan en medio de prosa o JSON."""
    redacted = _SECRET_VALUE.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    redacted = _SECRET_BEARER.sub(f"Bearer {REDACTED}", redacted)
    return _SECRET_SK.sub(REDACTED, redacted)


def redact_value(value: Any) -> Any:
    """Redacta por nombre de clave y por valor, recursivamente."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_secret_key(key) else redact_value(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _is_secret_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(
        marker in lowered
        for marker in (
            "token",
            "secret",
            "password",
            "passwd",
            "api_key",
            "apikey",
            "authorization",
            "credential",
        )
    )


__all__ = [
    "MAX_TRACE_CHARS",
    "MAX_UNTRUSTED_CHARS",
    "REDACTED",
    "clean_untrusted",
    "redact_text",
    "redact_value",
    "untrusted_block",
]
