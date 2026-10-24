-- v6 — corridas del agente
--
-- Una fila por corrida cerrada. La traza JSONL guarda el detalle (cada tool,
-- cada token, cada cita); esta tabla existe para lo que necesita el dashboard:
-- listar corridas, compararlas por coste y ver el veredicto sin abrir el JSONL.
--
-- Por qué no está todo en la traza: el dashboard consulta con SQL y agregar
-- sobre archivos JSONL obliga a cargarlos enteros. Por qué no está todo aquí:
-- el detalle de una corrida es un documento, no una fila, y no merece una tabla
-- por evento.
--
-- Idempotente: `IF NOT EXISTS` en tabla e índices.

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id                 TEXT    PRIMARY KEY,
    symptom                TEXT    NOT NULL,
    restatement            TEXT,
    -- RUNNING | OK | FAILED | BLOCKED | NEEDS_APPROVAL
    status                 TEXT    NOT NULL DEFAULT 'RUNNING',
    started_at             TEXT    NOT NULL,
    finished_at            TEXT,
    model                  TEXT,
    prompt_version         TEXT,
    -- JSON del plan validado, para poder reproducir la corrida sin el LLM.
    plan_json              TEXT,
    root_cause_fingerprint TEXT,
    confidence             TEXT,
    step_count             INTEGER,
    evidence_count         INTEGER,
    tokens_in              INTEGER NOT NULL DEFAULT 0,
    tokens_out             INTEGER NOT NULL DEFAULT 0,
    cost_usd               REAL    NOT NULL DEFAULT 0,
    trace_path             TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status  ON agent_runs (status, started_at DESC);
