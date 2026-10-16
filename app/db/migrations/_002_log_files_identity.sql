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
-- `foreign_keys` se apaga *antes* de abrir la transacción (SQLite lo ignora
-- dentro de una) y todo el rebuild va entre BEGIN/COMMIT: si algo aborta a
-- medias, el cierre de la conexión revierte y la base queda en v1 intacta, así
-- que la migración es recuperable volviéndola a correr.

PRAGMA foreign_keys = OFF;

-- Re-ejecutable: una corrida anterior pudo quedar a medias (un disco lleno, o un
-- `log_files_v2` huérfano creado por la versión antigua de esta migración). El
-- rebuild usa la tabla como transitoria, así que descartarla es seguro.
DROP TABLE IF EXISTS log_files_v2;

BEGIN;

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

-- En v1 la identidad era la ruta (`UNIQUE(path)`), así que el mismo archivo
-- físico (mismo inode) pudo quedar registrado en dos filas tras un rename. El
-- rebuild impone `UNIQUE (inode, generation)`. En vez de descartar el duplicado
-- —lo que dejaría huérfanos sus `events.file_id` y liberaría un `id` que SQLite
-- reutilizaría, atribuyendo eventos viejos a un archivo nuevo— se numera cada
-- duplicado con una generación distinta: todas las filas y sus eventos
-- sobreviven, y el contador coincide con el orden de alta (`id`). Sin inode no
-- hay identidad que colisionar, así que esas filas quedan en generación 0.
INSERT INTO log_files_v2 (
    id, path, inode, generation, size, last_offset, last_size,
    first_seen, last_seen, rotated_at, is_active
)
SELECT lf.id, lf.path, lf.inode,
       (
           SELECT COUNT(*)
           FROM log_files AS prev
           WHERE lf.inode IS NOT NULL
             AND prev.inode = lf.inode
             AND prev.id < lf.id
       ),
       lf.size, lf.last_offset, lf.last_size,
       lf.first_seen, lf.last_seen, lf.rotated_at, lf.is_active
FROM log_files AS lf
ORDER BY lf.id;

DROP TABLE log_files;

ALTER TABLE log_files_v2 RENAME TO log_files;

CREATE INDEX IF NOT EXISTS idx_log_files_path   ON log_files (path);
CREATE INDEX IF NOT EXISTS idx_log_files_active ON log_files (is_active, path);

COMMIT;

PRAGMA foreign_keys = ON;
