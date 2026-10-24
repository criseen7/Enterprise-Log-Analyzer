"""Servidor MCP de LogLens.

Este paquete es la **frontera** entre el motor determinista y los agentes que
lo usan (ver `docs/decisions/0006-mcp-como-frontera.md`).

La regla que gobierna el diseño: el motor calcula, la capa MCP transporta, el
agente redacta. Ninguna tool decide si algo es un error, ni cuántas veces
ocurrió, ni si una ventana está elevada sobre su baseline. Eso lo hace
`app/analysis/`, y lo hace en SQL, con las mismas reglas para todos.

Las tools devuelven modelos de Pydantic (`app/mcp/schemas.py`): el SDK genera
el JSON Schema de salida a partir del modelo de retorno y valida la respuesta
contra él, así que el contrato de cada tool es comprobable, no una convención.

Módulos:

* `_context.py` — conexiones a la base y normalización de tiempos.
* `hitl.py` — aprobación humana de la única tool que muta.
* `schemas.py` — los modelos de entrada y salida.
* `tools/` — las diez tools, agrupadas por el recurso que consultan.
* `server.py` — ensamblado del servidor y los transports.
"""
