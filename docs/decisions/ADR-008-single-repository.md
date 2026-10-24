# ADR-008 — Un solo repositorio, renombrado

**Estado:** aceptada
**Fecha:** 2026-10-21
**Fase:** 7 (Repositorio)
**Relacionada con:** ADR-006 y §3 de [docs/PLAN.md](../PLAN.md)

## Contexto

LogLens tiene tres capas: motor determinista (existente), servidor MCP (nueva) y
capa agentic (nueva), más un harness de evals.

Hay tres formas de organizarlas:

| Opción | Estructura |
|---|---|
| **A** | Un repo: `criseen7/loglens` con todo dentro |
| **B** | Dos repos: `Enterprise-Log-Analyzer` (motor) + `loglens-mcp` (MCP + agent + evals) |
| **C** | Monorepo con paquetes publicables separados: `loglens-core`, `loglens-mcp`, `loglens-agent` |

## Decisión

**Opción A: un solo repositorio, renombrado de `Enterprise-Log-Analyzer` a `loglens`.**

Estructura interna:

```
loglens/
├── app/
│   ├── config.py db/ ingest/ parser/ analysis/ alerts/ reports/ ui/   # motor
│   ├── mcp/        # servidor MCP
│   └── agent/      # capa agentic
├── evals/          # harness
├── docs/
└── tests/
```

GitHub redirige automáticamente los enlaces antiguos tras el rename, así que el
cambio no rompe ningún enlace existente.

## Razones

1. **Un repo = un contexto.** Un reclutador abre un repo y entiende el sistema
   entero, de la ingesta al diagnóstico. Dos repos exigen dos contextos y una
   explicación de qué va con qué, y en el mejor de los casos, el segundo repo no se
   abre.

2. **El motor y la capa agentica comparten el esquema SQLite, los tipos de retorno
   y los fixtures de test.** Separarlos obliga a duplicar tipos o a crear un
   paquete interno con su propio ciclo de release. Para un proyecto de portafolio,
   esa complejidad no compra nada.

3. **La narrativa es más fuerte en un solo repo.** *"Construí esto desde cero,
   incluyendo su motor"* convence más que *"hice un servidor MCP que usa una
   librería que escribí antes y está en otro repo"*.

4. **La opción C es la correcta en un entorno de equipo** (donde varios gente
   publican paquetes por separado con Semantic Release y changelogs independientes).
   En un portafolio individual, publicar tres paquetes en PyPI es mantenimiento
   distribuido sin recompensa.

5. **Un solo CI, un solo badge, una sola historia de commits.** El badge de CI
   verde en un solo repo es más fuerte que tres repos donde uno está rojo.

## Consecuencias

**A favor:**

- Un solo `docker compose up` levanta todo: motor, dashboard, MCP y evals.
- Un solo badge de CI, una sola cobertura, una sola release.
- El historial de commits cuenta una historia completa: motor → MCP → agente →
  evals → despliegue.
- Publicar en PyPI es opcional: `pip install git+https://github.com/criseen7/loglens`
  basta para el 95 % de los casos de uso.

**En contra:**

- El repo es grande y mezcla concerns. Mitigado por la separación de paquetes
  dentro de `app/` y por el guard de pureza del motor.
- Un cambio en el motor rompe los tests de MCP y de agent. Eso es **deseado**: es la
  señal de que el motor es la base de todo.
- No se puede dar semver independiente a cada capa.

## Mitigaciones de la mezcla de concerns

1. `app/mcp/` y `app/agent/` **sólo** importan de `app/analysis`, `app/alerts`,
   `app/db`, `app/config` y `app/parser`. Nunca al revés. Un test de CI verifica
   esta dirección de dependencia.
2. `tests/test_engine_purity.py` verifica que el motor no importa nada de LLM ni de
   MCP.
3. `docs/mcp-tools.md` es el contrato entre las dos mitades: si una tool cambia,
   ese documento cambia en el mismo commit.

## Renombre de los demás repos

Esta decisión se aplica a todo el portafolio (§9.2 de [docs/PLAN.md](../PLAN.md)). Regla única:
**`kebab-case`, en inglés, con sufijo de categoría.**

| Actual | Nuevo | Acción |
|---|---|---|
| `Enterprise-Log-Analyzer` | `loglens` | Renombrar |
| `ArquitecturaSistemasDistribuidos` | `distributed-systems-labs` | Renombrar + README |
| `SE` | `expert-system-agriculture` | Renombrar + README |
| `DiabetesIA` | `diabetes-ia` | Renombrar + README + métricas del modelo |
| `SubneteoFLSM` | — | Archivar (queda absorbido por `distributed-systems-labs`) |
| `Python`, `School`, `fpdf2`, `git-github-guia`, `OracleNextEducation-Alura`, `CodigoQR` | — | Archivar o borrar |

**Criterio de archivo:** si un repo no puede explicar en dos líneas qué problema
resuelve y por qué es tuyo, se archiva. Un portafolio de 5 repos sólidos se ve más
profesional que uno de 12 confusos.

## Alternativas consideradas

| Alternativa | Por qué se descartó |
|---|---|
| **B. Dos repos** | Duplica contexto, exige explicar la relación, y parte la prueba de que el sistema funciona |
| **C. Monorepo con paquetes separados** | Correcto para equipos. Sobre-ingeniería para un portafolio individual |
| **Mantener el nombre `Enterprise-Log-Analyzer`** | El nombre no dice "IA" ni "MCP", que es justo lo que posiciona en 2026. Además, "Enterprise" sugiere proyecto corporativo, no proyecto personal |
| **Renombrar a `loglens-mcp`** | Suena a un servidor MCP suelto, no a una plataforma. Y `loglens` está libre en PyPI (404 al 2026-10-21); `loglens-mcp` también, pero es un nombre peor |
