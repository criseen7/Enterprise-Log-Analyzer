-- Migración 010: el dispositivo entra en la identidad del archivo.
--
-- `(inode, generation)` no identifica un archivo de forma única: el número de
-- inodo sólo tiene sentido dentro de un dispositivo. Dos mounts distintos pueden
-- entregar el mismo inodo, y entonces la fila del segundo parece un rename del
-- primero: se reutiliza su `last_offset` y se pierden líneas sin avisar. La
-- identidad pasa a ser `(dev, inode, generation)`.
--
-- Las filas históricas no tienen dispositivo conocido: reciben el centinela 0,
-- no NULL. SQLite considera distintos todos los NULL en una UNIQUE, así que con
-- NULL la nueva clave no agruparía nada y el bug seguiría vivo. Con 0, la clave
-- `(0, inode, generation)` es equivalente a la antigua `(inode, generation)`.
--
-- El rebuild reordena `log_files` al orden exacto de `schema.sql` (head_hash
-- antes que is_active): así una base migrada y una nueva son columna por columna
-- idénticas. Todo va entre BEGIN/COMMIT con `foreign_keys` apagado fuera, igual
-- que `_002`: un fallo a mitad revierte y la base sigue en v9, recuperable.
-- El guard del runner reutiliza este mismo script para reparar aplicaciones
-- parciales, por lo que debe seguir siendo idempotente.

PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS log_files_v10;

BEGIN;

-- Una base real siempre trae `log_files`, pero las bases sintéticas que
-- someten a pruebas aisladas los deltas de HITL pueden no tenerla. Crearla vacía
-- con la forma v9 deja el rebuild idempotente y hace que v10 pueda completarse
-- sin depender de tablas que no le competen. En una base real no hace nada.
CREATE TABLE IF NOT EXISTS log_files (
    id           INTEGER PRIMARY KEY,
    path         TEXT    NOT NULL,
    inode        INTEGER,
    generation   INTEGER NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL DEFAULT 0,
    last_offset  INTEGER NOT NULL DEFAULT 0,
    last_size    INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL,
    rotated_at   TEXT,
    head_hash    TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (inode, generation)
);

CREATE TABLE log_files_v10 (
    id           INTEGER PRIMARY KEY,
    -- Última ruta conocida. Puede no ser la original si el archivo se renombró.
    path         TEXT    NOT NULL,
    -- st_dev del descriptor. 0 = fila histórica de antes de v10 (todo dispositivo
    -- es compatible con ella; se sustituye por el real al volver a ver el archivo).
    dev          INTEGER NOT NULL DEFAULT 0,
    inode        INTEGER,
    -- 0 = contenido original; cada rotación o truncado incrementa el contador.
    generation   INTEGER NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL DEFAULT 0,
    -- Offset (bytes) hasta donde ya se leyó. Base de la idempotencia.
    last_offset  INTEGER NOT NULL DEFAULT 0,
    -- Tamaño cuando se hizo la última lectura; permite detectar truncamiento.
    last_size    INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL,
    rotated_at   TEXT,
    -- SHA-256 truncado de la primera línea completa. Distingue "mismo archivo,
    -- otra ruta" de "otro archivo con el mismo inodo".
    head_hash    TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (dev, inode, generation)
);

-- Se copia el `id` tal cual: `events.file_id` apunta a él y no se puede liberar
-- ni reutilizar. Con dev=0 para todas las filas la UNIQUE nueva no colisiona
-- donde la antigua no colisionaba.
INSERT INTO log_files_v10 (
    id, path, dev, inode, generation, size, last_offset, last_size,
    first_seen, last_seen, rotated_at, head_hash, is_active
)
SELECT id, path, 0, inode, generation, size, last_offset, last_size,
       first_seen, last_seen, rotated_at, head_hash, is_active
FROM log_files
ORDER BY id;

DROP TABLE log_files;

ALTER TABLE log_files_v10 RENAME TO log_files;

CREATE INDEX IF NOT EXISTS idx_log_files_path   ON log_files (path);
CREATE INDEX IF NOT EXISTS idx_log_files_active ON log_files (is_active, path);

COMMIT;

PRAGMA foreign_keys = ON;
