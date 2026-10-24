# ADR-006 — MCP como frontera, no una API REST propia

**Estado:** aceptada
**Fecha:** 2026-10-21
**Fase:** 7-8 (MCP + capa agentic)
**Relacionada con:** ADR-005 y §4 de [docs/PLAN.md](../PLAN.md)

## Contexto

LogLens necesita que *otros programas* consulten el motor de logs: un agente de
Claude, Cursor, Continue, o un cliente HTTP propio.

La alternativa obvia es exponer una API REST con FastAPI. El repo ya tiene FastAPI
en el stack del perfil de GitHub, así que es una opción familiar.

El MCP (Model Context Protocol) es un protocolo abierto, estándar, respaldado por
Anthropic y con SDK oficial en Python, TypeScript, Go, Java, Rust, C# y Kotlin. Los
hosts de agentes más usados (Claude Desktop, Claude Code, Cursor, Continue, ChatGPT)
ya hablan MCP.

## Decisión

**MCP es la frontera pública del motor.**

1. El servidor MCP se implementa con el SDK oficial: `mcp` v2 (`pip install "mcp"`),
   `from mcp.server import MCPServer`.
2. Se exponen **tools** (operaciones parametrizadas), **resources** (contexto
   estático: esquema, catálogo de reglas, detalle de incidente, traza) y **prompts**
   (plantillas de trabajo).
3. Se soportan los dos transports: `stdio` (clientes locales) y `streamable-http`
   (demo pública y clientes remotos).
4. El SDK queda **aislado** en `app/mcp/_sdk.py`. Si la API del SDK cambia, el
   cambio toca un archivo, no diez.

**REST no se descarta: se reserva para el dashboard y para la integración web.** El
dashboard de Streamlit y la API de health check usan HTTP propio. Lo que se decide
es que **la frontera para agentes es MCP**, no un endpoint HTTP que cada agente
tenga que traducir a su propio formato.

## Consecuencias

**A favor:**

- **Distribución real.** Cualquier host MCP puede usar LogLens sin escribir código.
  Es la diferencia entre "un proyecto que se usa" y "un proyecto que se integra".
- **Es la señal de portafolio más accionable de 2026.** "Construir o consumir
  servidores MCP" aparece explícitamente en vacantes junior de backend y de IA
  (red6.ai, Auric AI, Critical Cloud, Schibsted).
- El SDK genera el JSON Schema de las tools desde los type hints: menos código
  boilerplate y menos superficie de error.
- La validación de argumentos y la de output schema vienen de serie.

**En contra:**

- El SDK está en v2 y es reciente (v2.0.0 salió en julio 2026). Hay churn. Se
  mitiga con `mcp>=2.3,<3` y con el aislamiento en `_sdk.py`.
- `FastMCP` fue renombrado a `MCPServer`: el código publicado en blogs de 2025 usa
  la API vieja. Hay que seguir la doc actual.
- No todos los clientes MCP son iguales en capacidades (elicitation, roots,
  sampling). El diseño usa sólo el subconjunto común: tools, resources y prompts.

## Notas de implementación (SDK v2.3.0)

- `FastMCP` → `MCPServer`. `from mcp.server.fastmcp import FastMCP` lanza
  `ModuleNotFoundError`.
- **Sólo `ToolError` llega al modelo.** Cualquier otra excepción produce
  `Error executing tool <name>` y el motivo no sale. Hay que elevar `ToolError` a
  propósito en cada fallo esperado.
- `MCPError` es error de protocolo: el modelo nunca lo ve.
- La configuración de transporte (`host`, `port`) se mueve a `run()`, no al
  constructor.
- `structured_output` se autodetecta del return type: devolver un modelo Pydantic
  produce `structuredContent` validado.
- Límite de 4 MiB en cuerpos Streamable HTTP (`max_request_body_size`).
- `from mcp import Client` permite pruebas in-memory sin levantar un servidor.
- `ToolAnnotations` permite declarar `readOnlyHint`, `destructiveHint` e
  `idempotentHint`: se usa para marcar la única tool mutante.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| **API REST propia con FastAPI** | Funciona, pero cada agente necesita un adaptador. El host ya habla MCP; obligar a traducir es trabajo del vendedor, no del comprador |
| **GraphQL** | Añade una capa de schema que no aporta nada aquí: las consultas son de lectura con filtros, no un grafo de relaciones |
| **gRPC** | Sobredimensionado para un servidor local. Y peor soporte en hosts de agentes |
| **Sólo CLI** | Un CLI es excelente para humanos y terrible para agentes: no hay descubrimiento de capacidades ni salida estructurada |
