# Changelog

All notable changes to LogLens are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-10-23

First public release. The repository was renamed from `Enterprise-Log-Analyzer`
to `loglens`; the Python package remains `app/`.

### Added

- **Deterministic engine**: declarative parsers for JSON, WebLogic and Apache
  CLF; error fingerprinting; rule-based classification; hourly anomalies with
  leave-one-out z-score; window comparison; blast radius and cascade detection.
- **MCP server** (SDK 2.x, stdio and streamable HTTP) with ten tools:
  `system_health`, `search_events`, `top_errors`, `detect_anomalies`,
  `compare_windows`, `blast_radius`, `list_incidents`, `get_incident`,
  `explain_fingerprint`, `set_incident_status`.
- **MCP resources and prompts**: `loglens://schema`, `loglens://rules`,
  `loglens://incident/{id}`, `loglens://trace/{run_id}`,
  `investigate_symptom`, `write_postmortem`.
- **Agent loop** (`app/agent/`): planner, executor with parallel steps,
  verifier, mechanical citation validation, deterministic fallback synthesis,
  JSONL traces and `agent_runs` accounting.
- **Human-in-the-loop mutations**: pending action → human approval →
  short-lived HMAC-SHA256 `confirm_token`; full audit in `agent_actions`.
- **Evaluation harness**: 60 committed synthetic cases across six failure
  mechanisms, real ingest, nine metrics, ablations A0–A4, response cache and
  per-case ground-truth validation.
- **Published evals**: A0 (no LLM) and A1 (no `compare_windows`) results, with
  A2–A4 marked as pending an API key instead of estimated.
- Dashboard (optional extra): overview, top errors, anomalies, incidents,
  exports.
- Docker Compose with three services sharing one SQLite database: `analyzer`,
  `dashboard`, `mcp`.
- `loglens` CLI: `mcp`, `investigate`, `tools`, `pending`, `approve`, `reject`.
- English `README.md` and Spanish `README.es.md`.

### Changed

- CLI entry point renamed from `ela`/`serve` to `loglens`/`mcp` (`serve` kept as
  a hidden alias).
- `pyproject.toml` declares the MCP server without dashboard dependencies;
  `dashboard`, `ingest`, `dev` and `agent` are extras.
- The agent's default analysis window is a moving hour at the end of the
  dataset, not the natural clock hour.
- Apache/WebLogic/JSON cases are generated in the formats the parser actually
  supports; the planned "syslog" format was dropped because no pattern exists.

### Fixed

- Weblogic logger names containing hyphens no longer break parsing.
- Fingerprint normalization no longer leaks numeric IDs from HTTP status
  codes into the hash (covered by golden snapshots).
- `traces_dir()` is now exported and honors `LOGLENS_TRACES_DIR`.
- Citation claims with `evidence_ids` pointing at missing evidence are removed
  before the diagnosis reaches the user.

### Security

- The engine package cannot import the agent or any LLM client
  (`tests/test_engine_purity.py`).
- MCP read tools open SQLite with `query_only=ON`.
- The HITL secret is never logged and defaults to a `0600` file.
- Trace resources reject path traversal in `run_id`.
