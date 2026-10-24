# ADR-007 — Orquestación propia en vez de un framework de agentes

**Estado:** aceptada
**Fecha:** 2026-10-21
**Fase:** 8 (Capa agentic)
**Relacionada con:** ADR-005 y §5 de [docs/PLAN.md](../PLAN.md)

## Contexto

La capa agentic de LogLens necesita orquestar un ciclo de plan → ejecución →
verificación → síntesis → gate → registro, con llamadas a tools, re-planificación
limitada, validación de evidencia y Human-in-the-Loop.

En 2026 el ecosistema ofrece frameworks maduros: LangGraph, CrewAI, AutoGen, el
Agents SDK de OpenAI, Pydantic AI. Las vacantes de "Junior AI / Agentic Engineer"
piden explícitamente experiencia con "LangGraph, CrewAI, AutoGen, Agents SDK **o
equivalente**".

La pregunta es si usar uno de ellos.

## Decisión

**El bucle principal se implementa a mano, sin framework de agentes.**

Concretamente:

1. `app/agent/loop.py` implementa el bucle en ~400 líneas, con dependencias de
   aplicación: `app/analysis`, `app/mcp`, `app/db`. Cero `langgraph`, cero
   `crewai`.
2. El bucle es **lineal y determinista en su estructura**. Los puntos de control
   (verify, cite, gate) son funciones puras con contratos explícitos.
3. La integración con el LLM pasa por una interfaz mínima de 8 líneas
   (`LLMClient.complete`), con implementaciones para Anthropic, OpenAI, Ollama y
   `NullClient` (tests).
4. **Opcionalmente**, un adaptador `LangGraphAdapter` puede replicar el mismo bucle
   como un grafo, para comparar. Es una variante, no una dependencia.

## Razones

1. **El bucle es pequeño.** Diez tools y un motor determinista. Un framework de
   grafos está optimizado para orquestar decenas de agentes con estado compartido.
   Aquí hay un agente, un motor y un gate. La abstracción no paga su coste.

2. **Se puede ver el flujo.** Con el bucle propio, un reclutador que abra
   `app/agent/loop.py` ve exactamente qué pasa y en qué orden. Con un grafo, ve
   nodos y aristas, y tiene que confiar en que la librería hace lo que dice. En una
   entrevista, "te enseño el fichero" vale más que "usé LangGraph".

3. **Los puntos de control deterministas son el producto.** `VERIFY`, `CITE` y
   `GATE` son donde está la ingeniería real: validan contra el motor, comprueban
   contra el bundle de evidencia, y exigen un token humano. Con un framework, estos
   puntos se implementan igual — como nodos o como hooks — pero el framework no los
   aporta.

4. **Menos superficie que puede romperse.** El SDK de MCP v2 es nuevo. Sumarle un
   framework de agentes que también es nuevo multiplica el riesgo de bloqueos por
   incompatibilidad.

5. **El ablation A0 necesita un plan no-LLM.** Con plan heurístico fijo, la
   comparación "con LLM vs sin LLM" es exacta y limpia. Un framework de agentes
   tiende a esconder el plan dentro de sus abstracciones.

## Consecuencias

**A favor:**

- ~400 líneas con contrato explícito y tests deterministas, sin red y sin API key.
- El `NullClient` permite testear todo el bucle sin coste.
- Se puede explicar el sistema entero en una entrevista sin diapositivas.
- El adaptador de LangGraph (si se añade) es una implementación más del mismo
  contrato, no una reescritura.

**En contra:**

- Se pierde el ecosistema: checkpoints, replay, streaming, debugging visual.
- Un reclutador que filtra por "LangGraph" en el CV podría no encontrarlo. **Se
  mitiga documentando la decisión en esta ADR** — que es exactamente el tipo de
  criterio de ingeniería que las entrevistas valoran, y que un README puede
  enlazar.
- Reimplementar streaming, persistencia de estado y cancelación sería trabajo
  real. **No se hace**: no son requisitos del problema.

## Alternativas consideradas

| Alternativa | Por qué se descartó como decisión principal |
|---|---|
| **LangGraph** | La opción más seria. Exige un grafo para un bucle lineal de 6 pasos. Aporta checkpointing y replay que no necesito. Se deja como adaptador opcional |
| **CrewAI / AutoGen** | Orientados a multi-agente. LogLens tiene un agente. Añadir roles sería estética |
| **OpenAI Agents SDK** | Bien diseñado, pero ataría la capa agentic a un proveedor, y el proyecto es provider-agnostic por decisión (ADR-005) |
| **Pydantic AI** | El más cercano a lo que quiero: tipado, provider-agnostic, pequeño. Se descarta por la misma razón que LangGraph — y por ser más reciente que el propio SDK de MCP |
| **Reescritura manual completa del loop de tool-calling** | Ya es lo que hace el SDK de MCP + el `LLMClient` propio. No hace falta reimplementar el loop de function-calling del modelo |
