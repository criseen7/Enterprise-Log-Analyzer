-- v9 — auditoría append-only de HITL y separación del rechazo.
-- DDL y backfill van en una sola transacción: un fallo a mitad de script no
-- deja columnas huérfanas que el guard tendría que reconstruir a ciegas.
BEGIN;
ALTER TABLE agent_actions ADD COLUMN rejected_at TEXT;
ALTER TABLE agent_actions ADD COLUMN rejected_by TEXT;

CREATE TABLE IF NOT EXISTS hitl_audit (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id   TEXT NOT NULL,
    event       TEXT NOT NULL,
    actor       TEXT,
    occurred_at TEXT NOT NULL,
    detail      TEXT,
    FOREIGN KEY (action_id) REFERENCES agent_actions(id)
);

CREATE INDEX IF NOT EXISTS idx_hitl_audit_action ON hitl_audit (action_id, seq);

CREATE TRIGGER IF NOT EXISTS trg_hitl_audit_no_update
BEFORE UPDATE ON hitl_audit
BEGIN
    SELECT RAISE(ABORT, 'hitl_audit es append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_hitl_audit_no_delete
BEFORE DELETE ON hitl_audit
BEGIN
    SELECT RAISE(ABORT, 'hitl_audit es append-only');
END;

-- Hasta v8 el rechazo reutilizaba approved_at/approved_by. Se conserva como
-- rechazo; no se fabrica un evento APPROVED para esas filas porque no se sabe
-- si hubo una aprobación previa.
UPDATE agent_actions
   SET rejected_at = approved_at, rejected_by = approved_by
 WHERE status = 'REJECTED' AND approved_at IS NOT NULL AND rejected_at IS NULL;

INSERT INTO hitl_audit (action_id, event, actor, occurred_at, detail)
SELECT id, 'CREATED', NULL, created_at, tool
  FROM agent_actions
 WHERE NOT EXISTS (
    SELECT 1 FROM hitl_audit h WHERE h.action_id = agent_actions.id AND h.event = 'CREATED'
 );

INSERT INTO hitl_audit (action_id, event, actor, occurred_at, detail)
SELECT id, 'APPROVED', approved_by, approved_at, tool
  FROM agent_actions
 WHERE status IN ('APPROVED', 'EXECUTED') AND approved_at IS NOT NULL
   AND NOT EXISTS (
    SELECT 1 FROM hitl_audit h WHERE h.action_id = agent_actions.id AND h.event = 'APPROVED'
 );

INSERT INTO hitl_audit (action_id, event, actor, occurred_at, detail)
SELECT id, 'REJECTED', rejected_by, rejected_at, tool
  FROM agent_actions
 WHERE status = 'REJECTED' AND rejected_at IS NOT NULL
   AND NOT EXISTS (
    SELECT 1 FROM hitl_audit h WHERE h.action_id = agent_actions.id AND h.event = 'REJECTED'
 );

INSERT INTO hitl_audit (action_id, event, actor, occurred_at, detail)
SELECT id, 'EXECUTED', NULL, executed_at, tool
  FROM agent_actions
 WHERE executed_at IS NOT NULL
   AND NOT EXISTS (
    SELECT 1 FROM hitl_audit h WHERE h.action_id = agent_actions.id AND h.event = 'EXECUTED'
 );

COMMIT;
