"""Caché en disco de respuestas del modelo, indexada por el prompt exacto.

El objetivo no es ahorrar unos céntimos: es hacer que las ablaciones sean
repetibles y gratuitas. Sin caché, comparar dos prompts obliga a pagar dos
veces el mismo dataset y desincentiva publicar resultados que contradicen la
hipótesis.

La clave incluye el texto del prompt, el modelo y el nombre del schema. Si
alguno cambia, cambia la clave: una caché que devuelve una respuesta de otro
prompt es peor que no tener caché.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.agent.llm import LLMClient, LLMResponse, Messages, _normalize

DEFAULT_CACHE_DIR = Path("evals/.cache")


class CachedClient:
    """Envuelve un cliente y sirve de disco lo ya respondido."""

    def __init__(self, inner: LLMClient, directory: str | Path = DEFAULT_CACHE_DIR) -> None:
        self.inner = inner
        self.directory = Path(directory)
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

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
        payload = {
            "system": system,
            "messages": _normalize(messages),
            "model": model,
            "schema": response_schema.__name__ if response_schema else None,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        key = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        path = self._path(key)
        if path.is_file():
            self.hits += 1
            stored: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return LLMResponse(
                text=stored["text"],
                data=stored.get("data"),
                model=stored["model"],
                # Un hit no gasta tokens: el coste que se publica de una corrida
                # cacheada es el real, no el que habría costado.
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
            )

        self.misses += 1
        response = self.inner.complete(
            messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            response_schema=response_schema,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"text": response.text, "data": response.data, "model": response.model},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return response


__all__ = ["DEFAULT_CACHE_DIR", "CachedClient"]
