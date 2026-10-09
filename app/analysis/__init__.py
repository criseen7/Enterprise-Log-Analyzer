"""Análisis de eventos: clasificación, métricas, recurrencia y anomalías.

La regla del paquete es la del resto del proyecto: la agregación ocurre en SQL
y la clasificación es configuración declarativa (YAML). Nada aquí carga la
tabla entera en memoria ni decide reglas desde código.
"""

from app.analysis.anomalies import Anomaly, anomalies, hourly_counts
from app.analysis.errors import ErrorRule, ErrorRules, load_rules
from app.analysis.fingerprint import event_fingerprint, normalize_message
from app.analysis.metrics import overview, top_errors
from app.analysis.patterns import RecurringPattern, recurring_patterns

__all__ = [
    "Anomaly",
    "ErrorRule",
    "ErrorRules",
    "RecurringPattern",
    "anomalies",
    "event_fingerprint",
    "hourly_counts",
    "load_rules",
    "normalize_message",
    "overview",
    "recurring_patterns",
    "top_errors",
]
