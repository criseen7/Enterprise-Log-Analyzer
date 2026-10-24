-- v5 — acciones del agente con aprobación humana (HITL)
--
-- La tabla `agent_actions` es la memoria compartida entre el servidor MCP y la
-- CLI de operador. El servidor registra la mutación que un agente quiere
-- hacer; el operador la aprueba desde su terminal; al aprobarse se emite el
-- `confirm_token`, que es la prueba criptográfica de esa aprobación.
--
-- Por qué vive en la base y no en memoria del proceso: el servidor MCP y la
-- CLI son dos procesos distintos. Si la acción pendiente viviera en memoria,
-- el token no podría cruzar entre ellos y el flujo de aprobación no existiría
-- fuera de las pruebas.
--
-- Idempotente por construcción: `IF NOT EXISTS` en la tabla y en los índices.
-- Una base que ya tuviera la tabla (por ejemplo creada a mano para probar)
-- puede aplicar este delta sin error.

CREATE TABLE IF NOT EXISTS agent_actions (
    id            TEXT    PRIMARY KEY,
    tool          TEXT    NOT NULL,
    params        TEXT    NOT NULL,
    -- PENDING | APPROVED | EXECUTED | REJECTED | EXPIRED
    status        TEXT    NOT NULL DEFAULT 'PENDING',
    confirm_token TEXT,
    created_at    TEXT    NOT NULL,
    expires_at    TEXT    NOT NULL,
    approved_at   TEXT,
    approved_by   TEXT,
    executed_at   TEXT,
    result        TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_actions_status  ON agent_actions (status, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_actions_expires ON agent_actions (expires_at);
