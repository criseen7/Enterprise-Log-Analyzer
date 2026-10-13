"""Detección y gestión de incidentes (Fase 5)."""

from __future__ import annotations

from .detection import IncidentCandidate, evaluate_incidents

__all__ = [
    "IncidentCandidate",
    "evaluate_incidents",
]
