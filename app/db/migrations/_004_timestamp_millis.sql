-- v4 — normaliza `timestamp_utc` a longitud fija (`.mmmZ`)
--
-- Hasta v3, `to_utc_iso` omitía los milisegundos cuando eran cero, así que la
-- base convivía con dos longitudes: `'...22Z'` (20) y `'...22.481Z'` (24). Como
-- `timestamp_utc` es TEXT con colación BINARY, `'.' < 'Z'` y los timestamps del
-- mismo segundo se ordenaban al revés: `MIN(timestamp_utc)` devolvía el más
-- nuevo y los filtros `>= ?` descartaban eventos del segundo de frontera.
--
-- Este delta rellena con `.000` las cadenas cortas. Es no destructivo e
-- idempotente: `length(...)=20` no vuelve a tocar una fila ya normalizada.
-- Se usa `substr` en vez de `strftime` a propósito: `strftime` devuelve NULL
-- ante un valor no parseable y borraría el timestamp; `substr` solo alarga.

UPDATE events
   SET timestamp_utc = substr(timestamp_utc, 1, 19) || '.000Z'
 WHERE timestamp_utc IS NOT NULL AND length(timestamp_utc) = 20;

UPDATE incidents
   SET first_seen = substr(first_seen, 1, 19) || '.000Z'
 WHERE first_seen IS NOT NULL AND length(first_seen) = 20;

UPDATE incidents
   SET last_seen = substr(last_seen, 1, 19) || '.000Z'
 WHERE last_seen IS NOT NULL AND length(last_seen) = 20;

UPDATE incidents
   SET notified_at = substr(notified_at, 1, 19) || '.000Z'
 WHERE notified_at IS NOT NULL AND length(notified_at) = 20;

UPDATE log_files
   SET first_seen = substr(first_seen, 1, 19) || '.000Z'
 WHERE first_seen IS NOT NULL AND length(first_seen) = 20;

UPDATE log_files
   SET last_seen = substr(last_seen, 1, 19) || '.000Z'
 WHERE last_seen IS NOT NULL AND length(last_seen) = 20;

UPDATE log_files
   SET rotated_at = substr(rotated_at, 1, 19) || '.000Z'
 WHERE rotated_at IS NOT NULL AND length(rotated_at) = 20;

UPDATE ingest_runs
   SET started_at = substr(started_at, 1, 19) || '.000Z'
 WHERE started_at IS NOT NULL AND length(started_at) = 20;

UPDATE ingest_runs
   SET finished_at = substr(finished_at, 1, 19) || '.000Z'
 WHERE finished_at IS NOT NULL AND length(finished_at) = 20;
