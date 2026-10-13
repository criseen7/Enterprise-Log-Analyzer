"""Detección y gestión de incidentes (Fase 5)."""

from __future__ import annotations

from .detection import IncidentCandidate, evaluate_incidents
from .incidents import (
    VALID_STATUSES,
    get_incident,
    incident_counts,
    list_incidents,
    set_incident_status,
    status_label,
)

__all__ = [
    "IncidentCandidate",
    "VALID_STATUSES",
    "evaluate_incidents",
    "get_incident",
    "incident_counts",
    "list_incidents",
    "set_incident_status",
    "status_label",
]
