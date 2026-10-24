"""Capa agentic de LogLens.

El punto de entrada es `investigate()`: recibe un síntoma en lenguaje natural y
devuelve un `AgentRun` con diagnóstico, citas validadas, evidencia y traza. Todo
lo demás de este paquete existe para que esa función pueda demostrar lo que
afirma.

Sin `llm`, funciona: plan heurístico y síntesis determinista. El modelo es una
mejora opcional, no un requisito del producto.
"""

from __future__ import annotations

from app.agent.contracts import (
    AgentRun,
    CitationReport,
    Claim,
    Diagnosis,
    EvidenceBundle,
    EvidenceItem,
    Plan,
    PlanStep,
)
from app.agent.loop import investigate

__all__ = [
    "AgentRun",
    "CitationReport",
    "Claim",
    "Diagnosis",
    "EvidenceBundle",
    "EvidenceItem",
    "Plan",
    "PlanStep",
    "investigate",
]
