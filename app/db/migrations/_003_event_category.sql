-- v3 — `events.category`: clasificación de error por evento
--
-- La categoría la asigna `analysis/error_rules.yaml` en el momento de la
-- ingesta y se guarda en el evento: si alguien cambia las reglas después, el
-- histórico no cambia retroactivamente (auditabilidad). Recalcular es una
-- decisión explícita, `python -m app.analysis --backfill --recalc`.
--
-- Ojo con la posición de la columna: en una base migrada `category` queda la
-- última, mientras que en una creada desde `schema.sql` va junto a
-- `fingerprint`. Ningún INSERT depende de la posición (usan lista de columnas
-- explícita) y ninguna lectura debería hacer `SELECT *` sin nombre de columna.

ALTER TABLE events ADD COLUMN category TEXT;

CREATE INDEX IF NOT EXISTS idx_events_cat_ts ON events (category, timestamp_utc);
