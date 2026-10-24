"""Las tools del servidor MCP.

Cada módulo agrupa las tools que consultan el mismo recurso:

* `system` — `system_health`: estado del dataset y de la ingesta.
* `events` — `search_events`, `top_errors`, `detect_anomalies`.
* `comparison` — `compare_windows`, `blast_radius`: las dos tools que hacen
  la aritmética que un modelo haría mal.
* `incidents` — `list_incidents`, `get_incident`, `set_incident_status`.
* `explain` — `explain_fingerprint`.

Las funciones de cada módulo son **planas**: abren su conexión, leen del motor
y devuelven un modelo de Pydantic. No saben nada de MCP. Eso las hace
testeables sin servidor, y mantiene la frontera en un solo sitio: `server.py`.
"""

from app.mcp.tools import comparison, events, explain, incidents, system

# Orden de registro: primero las que dan contexto general, después las que
# responden preguntas concretas, y al final la única que muta.
TOOL_MODULES = (system, events, comparison, explain, incidents)

__all__ = ["TOOL_MODULES"]
