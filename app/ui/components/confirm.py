"""Confirmación tipada para las acciones que escriben.

Un botón se pulsa por accidente; escribir una palabra concreta no. La palabra
se muestra en la propia etiqueta para que no haya que memorizarla, y el botón
permanece deshabilitado hasta que coincide exactamente.
"""

from __future__ import annotations

from typing import Any


def typed_confirmation(container: Any, *, word: str, key: str) -> bool:
    """Pide `word` en `container` y devuelve si se escribió exactamente."""
    value = container.text_input(
        f"Confirma escribiendo «{word}»",
        key=key,
        placeholder=word,
    )
    return value.strip() == word


__all__ = ["typed_confirmation"]
