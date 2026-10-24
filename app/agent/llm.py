"""Interfaz mínima de LLM y sus implementaciones.

La interfaz tiene un solo método. Eso es deliberado: el bucle del agente no
necesita streaming, ni tool-use multi-turno, ni memoria. Necesita convertir un
prompt en una respuesta y contabilizar lo que costó.

`NullClient` no es un mock de conveniencia: es la implementación que usan los
tests y el modo sin clave de API. Devuelve respuestas grabadas en orden, de modo
que el CI prueba el bucle completo —plan, ejecución, citas, traza— sin red y sin
gastar un céntimo. La única diferencia con producción es de dónde viene el texto.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

# Precios aproximados en USD por millón de tokens (entrada, salida). Están aquí
# y no en `config.yaml` para que una corrida antigua se pueda recalcular con la
# tabla con la que se hizo, en vez de con la que esté vigente hoy. Se declaran
# aproximados a propósito: el coste que se publica es una estimación, no una
# factura.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-1": (15.0, 75.0),
}
DEFAULT_PRICING: tuple[float, float] = (3.0, 15.0)


class LLMUnavailable(RuntimeError):
    """No hay cliente utilizable: sin clave, sin paquete o sin respuestas."""


class Message(BaseModel):
    role: str
    content: str


# Los llamantes pueden pasar `Message` o un dict `{"role": ..., "content": ...}`.
# Aceptar las dos formas evita que cada sitio de llamada tenga que construir el
# modelo, y el cliente lo normaliza en un solo punto.
Messages = Sequence[Message | Mapping[str, str]]


def _normalize(messages: Messages) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for message in messages:
        model = message if isinstance(message, Message) else Message.model_validate(message)
        normalized.append(model.model_dump())
    return normalized


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    # Cuando la respuesta se pidió con schema, aquí queda el JSON ya parseado.
    data: dict[str, Any] | None = None


@runtime_checkable
class LLMClient(Protocol):
    def complete(
        self,
        messages: Messages,
        *,
        system: str = "",
        model: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.0,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse: ...


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Coste estimado en USD con la tabla de precios local."""
    price_in, price_out = PRICING_USD_PER_MTOK.get(model, DEFAULT_PRICING)
    return (tokens_in / 1_000_000) * price_in + (tokens_out / 1_000_000) * price_out


@dataclass
class NullClient:
    """Devuelve respuestas grabadas, en orden. Sin red y sin coste.

    Cada llamada se guarda en `calls` para que los tests puedan comprobar qué
    se le pidió al modelo (por ejemplo, que el prompt de feedback contiene el
    `evidence_id` inventado que hay que corregir).
    """

    responses: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = "null"

    def complete(
        self,
        messages: Messages,
        *,
        system: str = "",
        model: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.0,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "messages": _normalize(messages),
                "schema": response_schema.__name__ if response_schema else None,
            }
        )
        if not self.responses:
            raise LLMUnavailable("NullClient no tiene más respuestas grabadas")
        text = self.responses.pop(0)
        return LLMResponse(
            text=text,
            model=self.model or model or "null",
            tokens_in=len(text) // 4,
            tokens_out=len(text) // 4,
        )


@dataclass
class AnthropicClient:
    """Cliente real. Importa `anthropic` de forma perezosa.

    El import perezoso no es estilo: `app/agent` se importa en tests que corren
    sin el paquete instalado, y un import en el módulo los rompería todos por
    una dependencia que sólo se usa en producción.
    """

    api_key: str | None = None
    timeout: float = 60.0
    _client: Any = field(default=None, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise LLMUnavailable(
                "falta el paquete 'anthropic'; instala el extra: pip install -e '.[agent]'"
            ) from exc
        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMUnavailable("falta ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=key, timeout=self.timeout)
        return self._client

    def complete(
        self,
        messages: Messages,
        *,
        system: str = "",
        model: str = "claude-sonnet-4-5",
        max_tokens: int = 2048,
        temperature: float = 0.0,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": _normalize(messages),
        }
        if system:
            kwargs["system"] = system
        if response_schema is not None:
            # Structured output por tool-use forzado: es el mecanismo que
            # garantiza que la respuesta sea el JSON del schema y no prosa con
            # un bloque de código que haya que extraer a mano.
            kwargs["tools"] = [
                {
                    "name": "emit",
                    "description": f"Emite un {response_schema.__name__} válido.",
                    "input_schema": response_schema.model_json_schema(),
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": "emit"}

        response = client.messages.create(**kwargs)
        tokens_in = int(getattr(response.usage, "input_tokens", 0))
        tokens_out = int(getattr(response.usage, "output_tokens", 0))

        if response_schema is not None:
            block = next(
                (b for b in response.content if getattr(b, "type", "") == "tool_use"), None
            )
            if block is None:
                raise LLMUnavailable("el modelo no devolvió el bloque 'emit' solicitado")
            data = dict(block.input)
            text = json.dumps(data, ensure_ascii=False)
        else:
            data = None
            text = "".join(
                getattr(block, "text", "")
                for block in response.content
                if getattr(block, "type", "") == "text"
            )

        return LLMResponse(
            text=text,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=estimate_cost(model, tokens_in, tokens_out),
            data=data,
        )


def build_client(model: str = "claude-sonnet-4-5") -> LLMClient:
    """Cliente real si hay clave; error explícito si no.

    No cae a `NullClient` en silencio: un informe generado por respuestas
    grabadas en producción sería peor que un error, porque parecería real.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMUnavailable("falta ANTHROPIC_API_KEY; usa el planner heurístico o define la clave")
    return AnthropicClient()


__all__ = [
    "DEFAULT_PRICING",
    "PRICING_USD_PER_MTOK",
    "AnthropicClient",
    "LLMClient",
    "LLMResponse",
    "LLMUnavailable",
    "Message",
    "NullClient",
    "build_client",
    "estimate_cost",
]
