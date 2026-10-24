# LogLens

**Un motor determinista de análisis de logs expuesto como servidor MCP, con una
capa de agente que investiga y redacta, pero nunca decide qué pasó.**

[English version](README.md)

LogLens ingiere archivos de log reales, calcula huellas de error, las clasifica,
compara ventanas temporales y calcula el alcance de un fallo — todo en SQL y
Python que se puede leer. Un LLM puede planificar la investigación sobre esos
resultados y convertirlos en un diagnóstico citado. No puede detectar nada que
el motor no haya calculado.

| | Motor | Agente |
|---|---|---|
| Encuentra el error, la huella, la ventana y los servicios | ✅ | ❌ |
| Decide si una huella es nueva o está elevada | ✅ | ❌ |
| Verifica que cada afirmación apunte a evidencia real | ✅ | ❌ |
| Elige qué preguntar después | ❌ | ✅ |
| Redacta la causa raíz | ❌ | ✅ |
| Cambia el estado de un incidente | ❌ | sólo con aprobación humana |

Si el modelo no está disponible, LogLens sigue produciendo un diagnóstico. Es
menos elocuente, no menos correcto.

---

## Demo

```console
$ loglens investigate "checkout falla tras el deploy" --no-llm
estado   : OK
causa    : pago rechazado por el banco
huella   : dacc3ca4b4d6710f
servicios: billing-api, gateway, order-api, worker

Afirmaciones:
  · (fact) La huella dacc3ca4b4d6710f concentra 33 errores (36.7% de la ventana) [fp:dacc3ca4b4d6710f]
  · (fact) El volumen es «new» frente al baseline [cmp:dacc3ca4b4d6710f]
  · (fact) El fallo afecta a 4 servicios y hay evidencia de cascada [blast:...]

Citas: 3 válidas, 0 inventadas (eliminadas), 0 sin cita
Traza   : traces/run-20261023T174810-b96b98b0.jsonl
```

Cada etiqueta entre corchetes es un `evidence_id`. Si una afirmación cita un id
que no existe, el validador la elimina antes de que la veas y el contador de
citas inventadas lo muestra. Hay una traza real en
[`docs/traces/`](docs/traces/).

---

## Cómo funciona

```
logs ──► parser (3 formatos declarativos) ──► SQLite (WAL)
                                                │
                             ┌──────────────────┴──────────────────┐
                             │          motor determinista          │
                             │ fingerprint · reglas · ventanas · blast│
                             └──────────────────┬──────────────────┘
                                                │ 10 tools MCP
                             ┌──────────────────┴──────────────────┐
                             │         servidor MCP (stdio/HTTP)    │
                             └──────┬──────────────────────┬────────┘
                                    │                      │
                          Claude Desktop / cliente MCP   bucle del agente
                                    plan → ejecutar → verificar → sintetizar
                                       → validar citas → gate HITL
```

El motor no tiene ninguna dependencia de LLM. Lo garantiza
[`tests/test_engine_purity.py`](tests/test_engine_purity.py): el paquete de
análisis no puede importar `anthropic`, ni el agente, ni clientes de red. No es
una convención: la suite falla si alguien cruza la línea.

El bucle del agente (`app/agent/`) son ~400 líneas a propósito, no un framework.
El razonamiento está en [ADR-007](docs/decisions/ADR-007-no-agent-framework.md).

---

## Superficie MCP

Diez tools, congeladas en v1. Nueve leen; exactamente una muta, y está
bloqueada por aprobación humana.

| Tool | Qué responde |
|---|---|
| `system_health` | ¿Puedo fiarme de este dataset? Cobertura, ventana, conteos. |
| `search_events` | Eventos crudos con filtros. |
| `top_errors` | Qué huellas dominan la ventana, con conteo y share. |
| `detect_anomalies` | Qué horas son anómalas (z-score leave-one-out). |
| `compare_windows` ⭐ | ¿Esta huella es **nueva** o está **elevada** frente a la ventana anterior? |
| `blast_radius` ⭐ | Qué servicios están afectados y si hay cascada. |
| `list_incidents` | Estado actual de incidentes. |
| `get_incident` ⭐ | Un incidente con su rastro de evidencia. |
| `explain_fingerprint` | Qué significa una huella, su categoría y su historia. |
| `set_incident_status` 🔒 | Cambia estado — **exige un token emitido por un humano**. |

Resources: `loglens://schema`, `loglens://rules`, `loglens://incident/{id}`,
`loglens://trace/{run_id}`. Prompts: `investigate_symptom`, `write_postmortem`.

### El HITL es un mecanismo, no un prompt

`set_incident_status` no muta en la primera llamada: registra la acción en
`agent_actions` y devuelve `pending_approval` con un `action_id`. Un humano
aprueba fuera de banda:

```bash
loglens pending
loglens approve <action_id>   # imprime un confirm_token HMAC-SHA256 con TTL
```

Sólo entonces el reintento funciona. El agente no puede fabricar su propio
token: el secreto vive en `LOGLENS_HITL_SECRET` o en un fichero `0600` junto a
la base. Cada intento, aprobación, rechazo y ejecución se audita.

---

## Evals

La parte que casi ningún repo de agentes publica. LogLens incluye un dataset
sintético con ground truth, un harness que ejecuta la **ingesta real** y las
**tools reales**, y ablaciones que se publican aunque contradigan la hipótesis.

| Corrida | Qué es | `root_cause_accuracy` | `hallucination_rate` |
|---|---|---|---|
| **A0** | motor + síntesis determinista, sin LLM | **0.95** | **0.00** |
| **A1** | A0 sin `compare_windows` | **0.00** | 0.00 |
| A2 | sin validación mecánica de citas | 0.95 | 0.00 (no informativa sin LLM) |
| A3–A4 | ablaciones de prompt/modelo | pendiente de `ANTHROPIC_API_KEY` | pendiente |

El detalle, el único fallo de A0 y las limitaciones del dataset están en
[`docs/evals/RESULTS.md`](docs/evals/RESULTS.md). Ninguna tabla contiene números
de modelo estimados.

60 casos, seis mecanismos de fallo (agotamiento de pool, deploy malo, disco
lleno, certificado expirado, pico de tráfico benigno, cascada de timeouts), tres
formatos y tres zonas horarias. Las huellas del ground truth se calculan con el
mismo motor que lee los logs — nunca a mano. El dataset está commiteado y es
reproducible:

```bash
python -m evals.dataset.build --verify
```

---

## Arranque rápido

```bash
git clone https://github.com/criseen7/loglens.git
cd loglens

python -m venv .venv && . .venv/bin/activate
pip install -e ".[agent,dev]"

python tools/generate_sample_logs.py --out logs --lines 800
python -m app.ingest --once

loglens investigate "checkout falla" --no-llm   # siempre funciona
export ANTHROPIC_API_KEY=...
loglens investigate "checkout falla"            # con modelo
loglens mcp                                     # stdio para Claude Desktop
loglens mcp --http --host 0.0.0.0 --port 8000   # streamable HTTP
```

Dashboard opcional: `pip install -e ".[dashboard]" && streamlit run app/ui/app.py`.

Docker (analyzer + dashboard + MCP sobre la misma base):

```bash
docker compose up --build
# dashboard  http://localhost:8501
# MCP        http://localhost:8000/mcp
```

Config para Claude Desktop: [`docs/mcp-config.json`](docs/mcp-config.json).

---

## Estructura

```
app/parser/      patrones declarativos (JSON, WebLogic, Apache CLF)
app/ingest/      lector incremental; identidad (inode, generation)
app/analysis/    fingerprint, reglas, ventanas, blast radius, anomalías
app/db/          esquema + migraciones hacia adelante (SQLite, WAL)
app/mcp/         10 tools, resources, prompts, consola HITL
app/agent/       planner, executor, verifier, citas, síntesis, traza
app/ui/          dashboard Streamlit (extra opcional)
evals/           dataset, generadores, harness, métricas, ablaciones
docs/            ADR-001…008, metodología de evals, trazas reales
tests/           339 tests, incluidos pureza del motor y goldens
```

## Decisiones de diseño

- [ADR-001](docs/decisions/ADR-001-sqlite-wal.md) — SQLite en WAL como modelo de concurrencia.
- [ADR-003](docs/decisions/ADR-003-file-identity.md) — identidad `(inode, generation)` para no perder datos en `copytruncate`.
- [ADR-004](docs/decisions/ADR-004-event-fingerprint.md) — normalizar antes de hashear; el orden importa.
- [ADR-005](docs/decisions/ADR-005-deterministic-engine.md) — el motor es la fuente de verdad.
- [ADR-006](docs/decisions/ADR-006-mcp-boundary.md) — MCP como única frontera, no una segunda API.
- [ADR-007](docs/decisions/ADR-007-no-agent-framework.md) — bucle propio en vez de LangGraph/CrewAI.

## Alcance y no-objetivos

Fuera de v1, a propósito: multi-cloud, múltiples proveedores de LLM, RAG/vector
store, orquestación multi-agente, fine-tuning, respuestas en streaming y
operadores de Kubernetes.

La lista de tools está congelada. Lo que no está en las diez tools no es una
feature que falta: es una decisión.

## Licencia

MIT — ver [LICENSE](LICENSE).

---

[Read in English](README.md)
