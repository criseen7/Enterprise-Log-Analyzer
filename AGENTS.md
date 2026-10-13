# AGENTS.md

Compact, repo-specific guidance for OpenCode sessions. Only include facts an
agent would likely get wrong without help.

## Setup

- **Python 3.14** runtime. Use the checked-in virtualenv: `.venv/bin/python`
  (the system interpreter lacks Streamlit and skips dashboard tests).
- `requirements.txt` pins `pandas==2.3.3` on purpose: 2.2.3 has no cp314 wheel.
- SQLite in **WAL** mode is the concurrency model (`app/db/connection.py`).
  `journal_mode` is persistent; `busy_timeout`/`synchronous` are per-connection.
- `data/`, `logs/`, `exports/` are resolved from `config.yaml`/env (`app/config.py`).
  `get_settings()` is module-cached; tests must reset `config._cached = None`.

## Exact commands

| Task | Command |
|---|---|
| Full suite | `.venv/bin/python -m unittest discover -s tests -t .` |
| Dashboard smoke (needs venv) | `.venv/bin/python -m unittest tests.test_dashboard -v` |
| UI query layer | `.venv/bin/python -m unittest tests.test_ui_data -v` |
| Alerts + export | `.venv/bin/python -m unittest tests.test_alerts -v` |
| One test | `.venv/bin/python -m unittest tests.test_ui_data.IndexUsageTests -v` |
| Bytecode sanity | `python3 -m compileall -q app tools tests` |
| Golden snapshots | `python3 -m tests.regenerate_golden --check` (`--regenerate` updates) |
| Ingest once / status | `python -m app.ingest --once` · `python -m app.ingest --status` |
| Analysis | `python -m app.analysis [--top N --hourly --anomalies --patterns --backfill]` |
| Alerts CLI | `python -m app.alerts [--list --status OPEN --export csv --window 60 --no-evaluate]` |
| Dashboard | `streamlit run app/ui/app.py` |
| Docker up/down | `make docker-up` · `make docker-down` (analyzer + dashboard) |

Always run the suite with `.venv/bin/python`; `pytest` also works but the
dashboard smoke tests are skipped without Streamlit.

## Architecture that is not obvious from filenames

- Entrypoints: `app/ui/app.py` (dashboard), `app/ingest/`, `app/analysis/`,
  `app/alerts/` (each has a `__main__.py` CLI).
- **Docker (Fase 6):** two services — `analyzer` (`python -m app.ingest --watch`,
  sole writer) and `dashboard` (`streamlit run app/ui/app.py`). `logs` is a
  read-only bind mount; `ela-data`/`ela-exports` are named volumes (survive
  `down`/`build`). `dashboard` `depends_on: service_healthy` so only one process
  applies the schema baseline. Paths come from `ELA_*` env (`/data/app.db`, etc.).
- **Ingest identity is `(inode, generation)`, not path** (`app/db/repository.py`).
  `UNIQUE(path)` breaks on `copytruncate`/rotation (ADR-003).
- **Ingest order matters:** resolve → read → insert → then advance `last_offset`.
  Advancing first loses data irreversibly.
- **Parsing is config-driven** (`app/parser/`, `config/patterns/`). Multiline
  assembly is a per-pattern decision (`multiline: true`); do not glue stack
  traces onto arbitrary JSON. Timestamps normalize `%b` via numeric month.
- **Fingerprint = normalize then hash** (`app/analysis/errors.py`). Order matters:
  UUID/timestamp/IP/hex before digits. Grouping on raw message = one group per event.
- **z-score uses leave-one-out** and padded hourly buckets (`app/analysis/anomalies.py`).
  Windows end at `MAX(timestamp_utc)`, not "now" (historical datasets).
- **`app/ui/data.py` is pure — no Streamlit import.** It decides SQL and is
  tested without Streamlit; `EXPLAIN QUERY PLAN` is asserted to avoid `SCAN events`.
- **UI caching:** `st.cache_data(ttl=30)` for serializable results only (returns
  dicts, never `sqlite3.Row`); never cache a `sqlite3.Connection`. Dashboard reads
  use `connection(read_only=True)`; writes only from Incidents/Settings.
- **Alerts:** `app/alerts/detection.py` evaluates window + upsert by fingerprint
  with cooldown via `notified_at`. `app/alerts/incidents.py` owns state
  management (`OPEN`/`ACKNOWLEDGED`/`RESOLVED`; UI shows `ACK`). Reopening resets
  `notified_at = NULL` so the next occurrence notifies again. Exports in
  `app/reports/`: CSV is `utf-8-sig`, HTML is self-contained (no `<script>`).
- **Migrations:** baseline (`app/db/schema.sql`) applies once at v0; deltas
  `_NNN_*.sql` carry existing DBs forward. Adding a delta requires updating
  `_deltas` + `_baseline_covers` and `SCHEMA_VERSION`. Mixing baseline `IF NOT
  EXISTS` with deltas makes two DBs claim the same version with different schemas.

## Testing quirks & gotchas

- `AppTest.from_string(page_script)` runs in isolation: the DB schema is **not**
  created. Tests must `migrate()` and seed `log_files` before inserting events
  with a `file_id` (FK fails otherwise). `AppTest.from_file(app.py)` runs bootstrap.
- Streamlit cache warnings in bare test mode are expected.
- Dashboard tests are skipped via `skipUnless(HAS_STREAMLIT)`.
- `sample_message` comes from `MAX(message)`; a fingerprint with a NULL message
  exports empty.
- `EventFilters.where()` intentionally drops `min_severity` when
  `unparsed_only=True` (unparsed = UNKNOWN/0).
- No lint/typecheck command is configured; `compileall` + the suite are the checks.

## Conventions

- Trust executable sources over prose when they conflict.
- `app/ui/data.py` pure; `app/ui/views/*` render-only (named `views/`, not
  `pages/`, to avoid Streamlit's auto-multipage warning with `st.navigation`);
  `app/ui/components/*` reusable UI; `app/ui/cache.py` wraps Streamlit cache;
  `app/db/*` schema/migrations.
- Writes in the dashboard are the exception and are explicit (Incidents, Settings):
  open a writable `connection()`, mutate, `cache.clear_caches()`, `st.rerun()`.
- Do not add comments or create docs unless asked.

## References

`README.md`, `docs/PLAN.md`, `docs/decisions/ADR-00{1,2,3,4}-*.md`,
`app/config.py`, `app/ui/data.py`, `app/alerts/`, `app/db/repository.py`.
