# Changelog

All notable changes to LogLens are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Append-only `hitl_audit` table with one event per HITL transition, actor and
  timestamp; readable with `loglens history <action_id>`.

### Changed

- Rejection now has its own `rejected_at`/`rejected_by` columns and no longer
  overwrites who approved the action (schema v9).
- GitHub Actions pinned to Node 24 releases and runners fixed to `ubuntu-26.04`.
- `approved_by`/`rejected_by` documented as audit labels, not authentication.
- Release builds pin `pip`, `build` and `twine`; `mcp` and `setuptools` are pinned
  exactly in `requirements.txt`/`pyproject.toml`. Dependabot proposes weekly
  updates for Actions and pip.
- Prerelease tags are published as prereleases instead of stable releases.
- Compose healthcheck for `analyzer` uses `--healthy` (read-only, fails until the
  schema is current) instead of `--status` (always returned 0).
- Compose services run with a read-only root filesystem, no new privileges and
  all Linux capabilities dropped; HITL/agent keys are passed only to `mcp`.

### Security

- `LOGLENS_HITL_SECRET` and the secret file are validated: at least 32 bytes,
  enough variety and no placeholder values. An invalid variable fails closed
  instead of falling back to the file.
- Actor labels are normalized and length-limited before being written to the
  audit trail; audit events cannot be updated or deleted from the application.
- Release notes are extracted with the tag validated as data (`awk -v`), never
  interpolated into the shell, and an empty changelog section fails the release.
- `permissions` default to `contents: read` in the Release workflow; only the job
  that publishes gets `contents: write`.
- Streamlit CORS is disabled; the CI image smoke really migrates a database and
  the compose job verifies the database survives `down`/`up`.

## [0.1.0] — 2026-10-23

First public release. The product is LogLens; the repository is
`Enterprise-Log-Analyzer` and the Python package remains `app/`.

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
