-- v8 — marca de agua de ocurrencia notificada
--
-- `notified_at` es un reloj de pared: sirve para el cooldown, no para decidir si
-- hubo un evento nuevo. Comparar la ventana actual contra él reabre y
-- renotifica un incidente resuelto cuyos eventos viejos siguen dentro de la
-- ventana, y pasada la ventana de cooldown la misma ventana estática vuelve a
-- avisar.
--
-- `notified_through` guarda el `last_seen` del incidente en el momento en que se
-- notificó. Sólo un evento con timestamp posterior es una ocurrencia nueva.
-- Las filas ya notificadas arrancan con la marca en su último evento para no
-- renotificar el histórico; las que nunca se notificaron quedan en NULL.
--
-- `ALTER TABLE ... ADD COLUMN` no acepta `IF NOT EXISTS`; el guard de la versión
-- 8 en `migrations/__init__.py` reafirma el relleno si el DDL se aplicó pero la
-- versión no llegó a registrarse.

ALTER TABLE incidents ADD COLUMN notified_through TEXT;

UPDATE incidents SET notified_through = last_seen WHERE notified_at IS NOT NULL;
