-- Migración 002: identidad de archivo (inode, generation).
--
-- Problema: `log_files.path` era UNIQUE, así que la ruta *era* la identidad del
-- archivo. Eso es incorrecto y cuesta datos en producción:
--
--   * logrotate renombra `app.log` -> `app.log.1`: el contenido ya ingerido
--     queda huérfano y el lector puede re-leerlo.
--   * `copytruncate` deja la misma ruta con contenido nuevo: la fila vieja con su
--     `last_offset` hace que la lectura nueva se corte contra eventos antiguos.
--
-- La identidad correcta es (inode, generation): el inode sobrevive al rename, y
-- la generación distingue reescrituras que conservan el inode. La ruta pasa a
-- ser solo metadata, mutable.
--
-- Implementación: rebuild de la tabla con el patrón de 12 pasos de SQLite.
-- `foreign_keys` se apaga dentro del script: SQLite no permite cambiarla dentro
-- de una transacción, y sin esto el DROP intermedio viola la FK de events.

PRAGMA foreign_keys = OFF;

CREATE TABLE log_files_v2 (
    id           INTEGER PRIMARY KEY,
    -- Última ruta conocida. Puede no ser la original si el archivo se renombró.
    path         TEXT    NOT NULL,
    inode        INTEGER,
    -- 0 = contenido original; cada rotación o truncado incrementa el contador.
    generation   INTEGER NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL DEFAULT 0,
    last_offset  INTEGER NOT NULL DEFAULT 0,
    last_size    INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL,
    rotated_at   TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (inode, generation)
);

INSERT INTO log_files_v2 (
    id, path, inode, size, last_offset, last_size,
    first_seen, last_seen, rotated_at, is_active
)
SELECT id, path, inode, size, last_offset, last_size,
       first_seen, last_seen, rotated_at, is_active
FROM log_files;

DROP TABLE log_files;

ALTER TABLE log_files_v2 RENAME TO log_files;

CREATE INDEX IF NOT EXISTS idx_log_files_path   ON log_files (path);
CREATE INDEX IF NOT EXISTS idx_log_files_active ON log_files (is_active, path);

PRAGMA foreign_keys = ON;
