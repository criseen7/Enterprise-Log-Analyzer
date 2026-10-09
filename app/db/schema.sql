-- Enterprise Log Analyzer — esquema v1
--
-- Convenciones:
--   * Timestamps como TEXT ISO-8601 en UTC ('2026-10-05T14:03:22.481Z').
--     SQLite no tiene tipo timestamp nativo; TEXT ordena cronológicamente porque
--     el formato es de longitud fija y UTC no tiene DST.
--   * `severity` es un entero (0 UNKNOWN .. 50 FATAL) para poder filtrar y
--     agregar con índices. El nivel original se conserva en `level_raw`.
--   * Nada se borra en cascada al borrar un log_file: los eventos son el
--     registro de auditoría y deben sobrevivir.

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- Archivos ingeridos y su posición de lectura.
--
-- La identidad de una fila NO es la ruta: es (inode, generation). Ver ADR-003.
-- La ruta cambia cuando el archivo se renombra (logrotate) y el contenido
-- sobrevive; el inode sobrevive al rename. Y una rotación por *truncado*
-- conserva el inode pero cambia el contenido, de ahí la generación.
CREATE TABLE IF NOT EXISTS log_files (
    id           INTEGER PRIMARY KEY,
    -- Última ruta conocida. Puede no ser la original si el archivo se renombró.
    path         TEXT    NOT NULL,
    inode        INTEGER,
    -- 0 = contenido original; cada rotación o truncado incrementa el contador.
    generation   INTEGER NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL DEFAULT 0,
    -- Offset (bytes) hasta donde ya se leyó. Base de la idempotencia.
    -- Solo avanza sobre líneas completas: una línea a medio escribir NO se consume.
    last_offset  INTEGER NOT NULL DEFAULT 0,
    -- Tamaño cuando se hizo la última lectura; permite detectar truncamiento.
    last_size    INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL,
    rotated_at   TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (inode, generation)
);

CREATE INDEX IF NOT EXISTS idx_log_files_path    ON log_files (path);
CREATE INDEX IF NOT EXISTS idx_log_files_active  ON log_files (is_active, path);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES log_files(id),
    -- Offset en bytes de la línea. La UNIQUE con file_id es la clave de
    -- idempotencia: reingerir el mismo archivo no puede duplicar eventos (D3).
    offset         INTEGER NOT NULL,
    fingerprint    TEXT,
    -- Categoría asignada por `analysis/error_rules.yaml` en el momento de la
    -- ingesta: queda congelada en el evento para que un cambio posterior de
    -- reglas no reescriba el histórico. Se recalcula sólo con --backfill.
    category       TEXT,
    timestamp_utc  TEXT,
    timestamp_raw  TEXT,
    source_tz      TEXT,
    level_raw      TEXT,
    level_norm     TEXT,
    severity       INTEGER,
    service        TEXT,
    logger         TEXT,
    message        TEXT,
    raw_line       TEXT NOT NULL,
    -- 0 = línea no reconocida por ningún patrón. Se conserva y se reporta:
    -- descartar en silencio hace la pérdida de datos invisible.
    parsed_ok      INTEGER NOT NULL DEFAULT 1,
    -- Motivo del fallo de parseo. Sin esto, una cobertura del 80% no es
    -- accionable: hay que poder leer por qué falló.
    parse_error    TEXT,
    UNIQUE (file_id, offset)
);

CREATE INDEX IF NOT EXISTS idx_events_ts          ON events (timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_events_fp          ON events (fingerprint);
CREATE INDEX IF NOT EXISTS idx_events_sev_ts      ON events (severity, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_events_service_ts  ON events (service, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_events_parsed      ON events (parsed_ok, parse_error);
CREATE INDEX IF NOT EXISTS idx_events_cat_ts      ON events (category, timestamp_utc);
-- El índice de UNIQUE(file_id, offset) lo crea SQLite automáticamente.

-- Incidentes: un registro por fingerprint problemático, no por evento.
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY,
    fingerprint     TEXT    NOT NULL UNIQUE,
    category        TEXT,
    severity        INTEGER NOT NULL DEFAULT 40,
    sample_message  TEXT,
    first_seen      TEXT    NOT NULL,
    last_seen       TEXT    NOT NULL,
    event_count     INTEGER NOT NULL DEFAULT 0,
    -- OPEN | ACKNOWLEDGED | RESOLVED
    status          TEXT    NOT NULL DEFAULT 'OPEN',
    -- Momento de la última notificación: implementa el cooldown.
    notified_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status, severity DESC);

-- Auditoría de la propia herramienta. Sin esto no hay forma de demostrar que
-- los números del dashboard son confiables (y no se puede detectar una regresión).
CREATE TABLE IF NOT EXISTS ingest_runs (
    id                 INTEGER PRIMARY KEY,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    files_processed    INTEGER NOT NULL DEFAULT 0,
    lines_read         INTEGER NOT NULL DEFAULT 0,
    events_inserted    INTEGER NOT NULL DEFAULT 0,
    -- Líneas ya presentes: la métrica que delata regresiones de idempotencia.
    events_skipped     INTEGER NOT NULL DEFAULT 0,
    events_unparsed    INTEGER NOT NULL DEFAULT 0,
    duration_ms        INTEGER,
    status             TEXT NOT NULL DEFAULT 'RUNNING',
    error              TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_runs_started ON ingest_runs (started_at DESC);