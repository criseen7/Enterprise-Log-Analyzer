# LogLens

**A deterministic log-analysis engine exposed as an MCP server, with an agent
layer that investigates and writes — but never decides what happened.**

[![CI](https://github.com/criseen7/loglens/actions/workflows/ci.yml/badge.svg)](https://github.com/criseen7/loglens/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-2.x-6b46c1.svg)](https://modelcontextprotocol.io)

LogLens ingests real log files, fingerprints and classifies errors, compares
time windows, and computes blast radius — all in SQL and Python you can read.
An LLM can plan the investigation over those results and turn them into a
cited diagnosis. It cannot detect anything the engine did not compute.

The distinction is the whole point:

| | Engine | Agent |
|---|---|---|
| Finds the error, the fingerprint, the window, the services | ✅ | ❌ |
| Decides whether a fingerprint is new or elevated | ✅ | ❌ |
| Validates that every claim points to real evidence | ✅ | ❌ |
| Chooses which questions to ask next | ❌ | ✅ |
| Writes the root-cause narrative | ❌ | ✅ |
| Changes incident state | ❌ | only with human approval |

If the model is unavailable, LogLens still produces a diagnosis. It is less
eloquent, not less correct.

---

## Demo

```console
$ loglens investigate "checkout is failing after the deploy" --no-llm
LogLens — checkout is failing after the deploy
run      : run-20261023T174810-b96b98b0
estado   : OK
confianza: low
causa    : pago rechazado por el banco
huella   : dacc3ca4b4d6710f
servicios: billing-api, gateway, order-api, worker

Afirmaciones:
  · (fact) La huella dacc3ca4b4d6710f concentra 33 errores (36.7% del total de la ventana) [fp:dacc3ca4b4d6710f]
  · (fact) El volumen es «new» frente al baseline (ratio None, z-score None) [cmp:dacc3ca4b4d6710f]
  · (fact) El fallo afecta a 4 servicio(s) y hay evidencia de cascada [blast:..., svc:...]

Siguiente paso:
  → revisar el servicio que originó la huella dacc3ca4b4d6710f
  → vigilar la huella en la próxima ventana

Huecos de evidencia:
  ! el log no identifica cuál de los servicios falló primero en origen
  ! la confianza del dataset es medium

Citas: 3 válidas, 0 inventadas (eliminadas), 0 sin cita

Coste   : $0.000000 (0 in / 0 out, 1725 ms)
Traza   : traces/run-20261023T174810-b96b98b0.jsonl
```

Every bracketed tag is an `evidence_id`. If a claim cites an id that does not
exist, the validator removes the claim before you see it, and the count of
invented citations is printed. A real trace is in
[`docs/traces/`](docs/traces/).

---

## How it works

```
logs ──► parser (3 declarative formats) ──► SQLite (WAL)
                                               │
                          ┌────────────────────┴────────────────────┐
                          │            deterministic engine          │
                          │  fingerprint · rules · windows · blast   │
                          └────────────────────┬────────────────────┘
                                               │ 10 MCP tools
                          ┌────────────────────┴────────────────────┐
                          │            MCP server (stdio/HTTP)       │
                          └───────┬─────────────────────────┬────────┘
                                  │                         │
                         Claude Desktop / any MCP client    │
                                                     agent loop
                                  plan → execute → verify → synthesize
                                     → validate citations → HITL gate
```

The engine has no LLM dependency at all. That is enforced by
[`tests/test_engine_purity.py`](tests/test_engine_purity.py): the analysis
package cannot import `anthropic`, the agent, or any network client. It is not
a convention — the suite fails if someone crosses the line.

The agent loop (`app/agent/`) is ~400 lines on purpose, not a framework. See
[ADR-007](docs/decisions/ADR-007-no-agent-framework.md) for why.

---

## MCP surface

Ten tools, frozen for v1. Nine read; exactly one mutates, and it is gated.

| Tool | What it answers |
|---|---|
| `system_health` | Can I trust this dataset? Coverage, window, counts. |
| `search_events` | Raw events with filters (service, severity, text, time). |
| `top_errors` | Which fingerprints dominate the window, with counts and share. |
| `detect_anomalies` | Which hourly buckets are statistically abnormal (leave-one-out z-score). |
| `compare_windows` ⭐ | Is this fingerprint **new** or **elevated** versus the previous window? |
| `blast_radius` ⭐ | Which services are hit, and is there evidence of a cascade? |
| `list_incidents` | Current incident state. |
| `get_incident` ⭐ | One incident with its evidence trail. |
| `explain_fingerprint` | What this fingerprint means, its category, its history. |
| `set_incident_status` 🔒 | Changes state — **requires a human-issued token**. |

Resources:

- `loglens://schema` — tables and columns the tools query
- `loglens://rules` — the classification rules and their order
- `loglens://incident/{id}` — one incident
- `loglens://trace/{run_id}` — the full JSONL trace of an agent run

Prompts: `investigate_symptom`, `write_postmortem`.

### Human in the loop is a mechanism, not a prompt

`set_incident_status` does not mutate on the first call. It records a pending
action in `agent_actions` and returns `pending_approval` plus an `action_id`.
A human approves out of band:

```bash
loglens pending
loglens approve <action_id>   # prints a short-lived HMAC-SHA256 confirm_token
```

Only then does the retry succeed. The agent cannot mint its own token: the
secret lives in `LOGLENS_HITL_SECRET` or in a `0600` file next to the database.
Every attempt, approval, rejection, and execution is appended to the audit
table.

---

## Evals

This is the part most agent repos skip. LogLens ships a synthetic dataset with
ground truth, a harness that runs the **real** ingest and the **real** tools,
and ablations that are published even when they contradict the pitch.

```console
$ python -m evals.run --subset full --plan heuristic
...
| `root_cause_accuracy`    | 0.95 | >= 0.8  ✅ |
| `hallucination_rate`     | 0.00 | <= 0.05 ✅ |
| `retrieval_recall_at_5`  | 1.00 | >= 0.9  ✅ |
```

| Run | What it is | `root_cause_accuracy` | `hallucination_rate` |
|---|---|---|---|
| **A0** | engine + deterministic synthesis, no LLM | **0.95** | **0.00** |
| **A1** | A0 without `compare_windows` | **0.00** | 0.00 |
| A2 | without mechanical citation validation | 0.95 | 0.00 (not informative without an LLM) |
| A3–A4 | prompt/model ablations | pending `ANTHROPIC_API_KEY` | pending |

Read the reasoning, the single A0 miss, and the dataset limitations in
[`docs/evals/RESULTS.md`](docs/evals/RESULTS.md). Short version: the harness is
honest about what it does **not** prove, and no table contains an estimated
model number.

The dataset itself is committed and reproducible:

```bash
python -m evals.dataset.build --verify   # fails if cases.jsonl drifts
```

60 cases, six failure mechanisms (pool exhaustion, bad deploy, disk full,
expired certificate, benign traffic spike, cascading timeout), three log
formats, three time zones. Ground-truth fingerprints are computed with the same
engine that reads the logs — never hand-written.

---

## Quickstart

```bash
git clone https://github.com/criseen7/loglens.git
cd loglens

python -m venv .venv && . .venv/bin/activate
pip install -e ".[agent,dev]"

# 1. Generate demo logs and ingest them
python tools/generate_sample_logs.py --out logs --lines 800
python -m app.ingest --once

# 2. Investigate without a model (always works)
loglens investigate "checkout is failing" --no-llm

# 3. Investigate with a model (optional)
export ANTHROPIC_API_KEY=...
loglens investigate "checkout is failing"

# 4. Serve MCP over stdio (Claude Desktop) or HTTP
loglens mcp
loglens mcp --http --host 0.0.0.0 --port 8000
```

Dashboard (optional, separate extra):

```bash
pip install -e ".[dashboard]"
streamlit run app/ui/app.py
```

Docker, three services — analyzer, dashboard, MCP — on one database:

```bash
docker compose up --build
# dashboard  http://localhost:8501
# MCP        http://localhost:8000/mcp
```

Claude Desktop config: [`docs/mcp-config.json`](docs/mcp-config.json).

---

## CLI reference

| Command | Purpose |
|---|---|
| `loglens mcp [--http]` | MCP server (stdio by default). |
| `loglens investigate "<symptom>" [--no-llm] [--json] [--allow-mutations]` | Full agent run with trace. |
| `loglens tools` | List registered MCP tools. |
| `loglens pending` | List actions waiting for approval. |
| `loglens approve <id>` / `loglens reject <id>` | Human decision on a mutation. |
| `python -m app.ingest [--once\|--watch\|--status]` | Ingest logs. |
| `python -m app.analysis` | Offline analysis (top errors, anomalies, patterns). |
| `python -m app.alerts` | Alert evaluation and incident state. |
| `python -m evals.run` | Evaluation harness. |

---

## Project layout

```
app/
  parser/        declarative patterns (JSON, WebLogic, Apache CLF)
  ingest/        incremental reader; identity is (inode, generation)
  analysis/      fingerprint, rules, windows, blast radius, anomalies
  db/            schema + forward-only migrations (SQLite, WAL)
  mcp/           10 tools, resources, prompts, HITL console
  agent/         planner, executor, verifier, citations, synthesis, trace
  ui/            Streamlit dashboard (optional extra)
evals/           dataset, generators, harness, metrics, ablations
docs/
  decisions/     ADR-001 … ADR-008
  evals/         methodology and published results
  traces/        a real, unedited agent trace
tests/           339 tests, including engine purity and golden snapshots
```

## Design decisions worth reading

- [ADR-001](docs/decisions/ADR-001-sqlite-wal.md) — SQLite in WAL mode is the concurrency model.
- [ADR-003](docs/decisions/ADR-003-file-identity.md) — file identity is `(inode, generation)`, so `copytruncate` doesn't lose data.
- [ADR-004](docs/decisions/ADR-004-event-fingerprint.md) — normalize before hashing; order matters.
- [ADR-005](docs/decisions/ADR-005-deterministic-engine.md) — the engine is the source of truth.
- [ADR-006](docs/decisions/ADR-006-mcp-boundary.md) — MCP as the only boundary, not a second API.
- [ADR-007](docs/decisions/ADR-007-no-agent-framework.md) — an own loop instead of LangGraph/CrewAI.

## Scope and non-goals

Deliberately **not** in v1: multi-cloud ingestion, multiple LLM providers,
vector search/RAG, multi-agent orchestration, fine-tuning, streaming responses,
and Kubernetes operators.

The tool list is frozen. What is not in the ten tools is not a missing feature;
it is a decision.

## License

MIT — see [LICENSE](LICENSE).

---

[Leer en español](README.es.md)
