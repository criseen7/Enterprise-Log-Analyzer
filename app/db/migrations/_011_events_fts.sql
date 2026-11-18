-- Migración 011: índice FTS5 para la búsqueda de texto de eventos.
--
-- `LIKE '%término%'` obliga a recorrer `events` aunque exista un índice por
-- timestamp: no hay forma de buscar un substring con un B-tree. La tabla FTS
-- externa comparte rowid con `events` y cubre `message` y `raw_line`, que son
-- exactamente las dos columnas que busca la UI y la tool MCP.
--
-- La baseline crea la misma tabla y los mismos triggers. Aquí, además, se
-- reconstruye el índice para que los eventos anteriores a v11 sean buscables.
-- `rebuild` es idempotente, igual que el resto del script, así que un reintento
-- tras un fallo entre el DDL y el registro de versión no deja la base atascada.

BEGIN;

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    message, raw_line,
    content='events', content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, message, raw_line)
    VALUES (new.id, new.message, new.raw_line);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, message, raw_line)
    VALUES ('delete', old.id, old.message, old.raw_line);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, message, raw_line)
    VALUES ('delete', old.id, old.message, old.raw_line);
    INSERT INTO events_fts(rowid, message, raw_line)
    VALUES (new.id, new.message, new.raw_line);
END;

INSERT INTO events_fts(events_fts) VALUES ('rebuild');

COMMIT;
