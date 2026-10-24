# ADR-005 — El motor determinista es la fuente de verdad

**Estado:** aceptada
**Fecha:** 2026-10-21
**Fase:** 7-8 (MCP + capa agentic)
**Relacionada con:** ADR-006, ADR-007 y §4-§6 de [docs/PLAN.md](../PLAN.md)

## Contexto

LogLens expone logs a un agente de IA para que investigue incidentes. La decisión
arquitectónica central es **dónde vive la lógica de detección**.

La respuesta mayoritaria en el ecosistema MCP de logs (incluidos los repos con más
estrellas) es: *en el LLM*. El servidor MCP es un `grep` sofisticado y el modelo
decide qué significa.

Esa decisión tiene consecuencias que un usuario de a pie no ve pero un equipo de
ingeniería sí:

1. **No es reproducible.** El mismo conjunto de logs produce diagnósticos
   distintos según el modelo, la temperatura o el prompt. No hay forma de responder
   "¿por qué ayer dijo X?".
2. **No es testeable.** Sin *ground truth* no hay test. Y sin *ground truth* no hay
   forma de saber si el sistema mejoró o empeoró.
3. **No es auditable.** Cuando el agente dice "fue el disco lleno", no hay forma de
   comprobar de dónde salió esa conclusión.
4. **El resultado depende de la decisión de compras.** Si el modelo cambia de
   versión, el producto cambia de comportamiento sin que nadie haya tocado código.

Al mismo tiempo, LogLens **sí** quiere un agente: lagoal de un SRE no es "cuál es el
error más frecuente", es "qué está pasando y qué hago".

## Decisión

**El motor determinista detecta. El LLM investiga y redacta.**

Concretamente:

1. La detección de anomalías, el fingerprinting, la correlación de incidentes, el
   baseline y el cálculo de z-score viven **exclusivamente** en `app/analysis/` y
   `app/alerts/`, sin ninguna dependencia de LLM.
2. Las tools MCP son funciones **puras** sobre SQLite. No hacen prompting, no
   detectan anomalías "a ojo", no llama a un modelo.
3. `app/agent/` consume las tools y produce el diagnóstico. Si el LLM no está
   disponible, el producto **sigue funcionando** con un plan heurístico fijo.
4. La afirmación del agente es validada mecánicamente contra la evidencia
   existente. Las claims que no se sostienen se eliminan o se degradan.
5. Un test de CI (`tests/test_engine_purity.py`) verifica que los paquetes del motor
   no importan `anthropic`, `openai`, `mcp`, `httpx` ni `litellm`.

## Consecuencias

**A favor:**

- Los evals tienen ground truth y una métrica de recuperación que no depende del
  modelo. Eso es lo que permite publicar números.
- El motor se puede testear con fixtures golden, que es exactamente lo que ya hace
  el repo con 207 tests.
- Cambiar de modelo no cambia lo que el sistema **detecta**, sólo cómo lo redacta.
  Verificable con el ablation A4.
- El producto funciona sin API key y sin coste por consulta.
- La tesis del proyecto es auditable: un test de CI la sostiene.

**En contra:**

- Hay que escribir el motor que faltaba (baseline, blast radius, z-score por
  ventana). Más trabajo upfront que "mete un prompt y ya".
- La capa agentic es más pequeña y menos vistosa que un grafo de agentes multi-especialista.
- Dos respuestas del agente para el mismo síntoma pueden diferir en la redacción.
  Se acepta: el contenido verificable es el mismo.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| **Detección en el LLM** (mayoría del ecosistema) | No reproducible, no testeable, no auditable. Ver arriba |
| **LLM + validación determinista al 100 %** | El LLM seguiría siendo el detector. La validación sólo detectaría el error, no lo evitaría, y el coste por consulta seguiría siendo el de un modelo grande |
| **Motor determinista + LLM opcional como *feature*** | Es lo que se hace aquí. La diferencia es que el LLM no es un adorno: es el que redacta el diagnóstico, y su salida se valida |
| **Sólo motor, sin LLM** | Pierde la mitad del valor (el diagnóstico legible) y la mitad de la señal de portafolio |
