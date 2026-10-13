
## Qué aprendí

Las decisiones están en [`docs/decisions/`](docs/decisions/). Lo ya verificado:

**Fase 0 — concurrencia (ADR-001)**
- **WAL no es un detalle de configuración**: cambia el modelo de concurrencia entero.
  Un test de estrés *no* lo demuestra (SQLite aguanta escrituras cortas incluso en
  `DELETE`); lo demuestra el escenario determinista de un lector con transacción
  abierta bloqueando el `COMMIT` del escritor.
- **`executescript()` hace un COMMIT implícito**: envolverlo en una transacción
  explícita falla con `cannot commit - no transaction is active`.

**Fase 1 — parsing (ADR-002)**
- **`re.VERBOSE` es obligatorio** para escribir regex legibles en YAML: sin él, cada
  salto de línea del bloque se interpreta como literal.
- **`strptime` con `%b` depende de `LC_TIME`**: un contenedor con locale `es_ES`
  deja de entender `Oct`. Se sustituye el mes por su número antes de parsear.
- **Un access log no tiene nivel**: CLF solo da el código HTTP. Un 2xx se queda en
  `UNKNOWN` porque no es un error; inventarle un nivel sería mentir.
- **La idempotencia se diseña antes que el watcher**, no después. El orden importa.
- **Normalizar el timestamp es un problema de datos, no de formato.**

**Fase 2 — ingesta (ADR-003)**
- **La ruta no es identidad de archivo.** `UNIQUE(path)` da la sensación de ser
  correcto hasta que llega `copytruncate`: la ruta se queda, el offset también, y lo
  nuevo queda *detrás* de ese offset — la ingesta dice "0 líneas nuevas" mientras el
  archivo crece. Identidad = `(inode, generation)`.
- **El orden de operaciones es la decisión.** Resolver → leer → insertar → y sólo
  entonces avanzar `last_offset`. Un offset adelantado sin eventos es pérdida
  irreversible; el orden inverso hace el fallo recuperable.
- **Un regex multilínea en YAML falla en silencio sin `re.VERBOSE`.** No hay error de
  sintaxis: hay saltos de línea tratados como literales y el síntoma es "ningún
  patrón coincide". Apache declaraba `flags:`, WebLogic no, y no cuadraba.
- **No se puede ensamblar multilínea a ciegas.** Pegar líneas a un registro JSON lo
  invalida y se pierde el evento entero. El ensamblado es una decisión del patrón
  (`multiline: true`), no del lector.
- **El orden alfabético del descubrimiento cambia el estado.** `app.log` se procesa
  antes que `app.log.1`; sin reactivar `is_active` en la rama de rename, el archivo
  vivo aparecía como inactivo.

**Fase 3 — análisis (ADR-004)**
- **Agrupar es más difícil que contar.** `COUNT(*) GROUP BY message` devuelve un
  grupo por evento porque el mensaje trae un identificador distinto cada vez.
  La huella existe para eso: normalizar y *después* hashear.
- **El orden de la normalización es el algoritmo.** UUID, timestamp e IP van
  antes que los números (`0x1f` se partiría en dos) y los números van sin
  límite de palabra: con `\b`, `u123` no se toca y cada evento queda en su
  propia huella, que es exactamente lo que la huella viene a evitar.
- **Un z-score sobre la serie completa se auto-suprime.** El pico sube la
  desviación de todo y se disimula a sí mismo. Media y desviación tienen que
  ser *leave-one-out*, y las horas vacías tienen que estar rellenadas o seis
  horas con errores y cincuenta sin aparecer dan una media inflada donde
  cualquier cosa parece anómala.
- **La ventana no puede mirar el reloj.** Con datos históricos,
  `ahora - ventana` no toca ningún evento y el resultado es vacío sin estarlo.
  Por defecto la ventana acaba en el último timestamp del propio dataset.
- **La cobertura de parseo se puede mentir a sí misma.** Una línea sin formato
  pegada a un stack trace se ensambla como continuación: no se pierde, pero
  `parsed_ok` sale `1`. Un dataset de demo puede acabar en 100% teniendo una
  línea basura dentro; el generador antepone un registro JSON a propósito.
- **El generador decidía nivel y mensaje por separado** y producía "pago
  rechazado" a `INFO` y "pedido creado" a `ERROR`: las queries eran correctas
  sobre datos tramposos. Nivel y mensaje van juntos en la misma lista.


**Fase 4 — dashboard**
- **Los filtros cambian el SQL, no el render.** Conectarlos a `st.cache_data` obliga a pasar los filtros como claves (hacen parte del cache key). Un error de diseño aquí es duplicar el mismo cache para rangos distintos y nunca invalidarlo.
- **Una conexión por consulta.** Para el dashboard es mejor abrir/cerrar la conexión (WAL) que cachear una conexión: no es serializable y con `cache_data` explota. `connection(read_only=True)` es corta vida.
- **El estado vacío debe ser accionable.** Si "no hay datos" no explica qué hacer, el usuario abandona. Cada pantalla añade `command` o instrucción concreta.
- **Los presets de tiempo se anclan al último evento.** Con dataset histórico, "últimas 24h" relativo a hoy da cero; el preset calcula desde `MAX(timestamp_utc)`.
- **El `LIKE` con `%` del usuario debe escaparse.** Sin `ESCAPE '\'`, buscar `"100%"` se convierte en comodín y deja de encontrar lo buscado (test explícito).
- **`unparsed_only` anula `min_severity`.** Una línea sin parsear tiene severidad UNKNOWN (0); el filtro por defecto ≥45 y el toggle producían una tabla vacía sin explicación — ahora se fuerza `min_severity=0` al activar el toggle.

**Fase 5 — alertas**
- **Un incidente es una huella, no un evento.** 5.000 timeouts idénticos son 5.000 filas de `events` y **una** de `incidents`. El `UNIQUE(fingerprint)` hace el upsert trivial; contar por evento habría creado 5.000 incidentes.
- **El cooldown pertenece a la fila, no al proceso.** `notified_at` persiste para que reiniciar el servicio no vuelva a notificar lo mismo. Un cooldown en memoria se pierde con el primer reinicio.
- **Reabrir reinicia el cooldown.** Si `RESOLVED → OPEN` conserva `notified_at`, el primer rebrote queda silenciado. Un incidente reabierto es nuevo para el cooldown (`notified_at = NULL`).
- **El estado canónico no se abrevia en la base.** El esquema guarda `ACKNOWLEDGED` y la tabla muestra `ACK`; guardar la abreviatura habría dejado dos vocabularios para el mismo valor.
- **`utf-8-sig` no es cosmético.** El BOM es lo que hace que Excel abra el CSV con los acentos correctos. Sin él, "petición" se ve como mojibake y el export parece roto.
- **El HTML de export es autocontenido a propósito.** Estilos embebidos y sin `<script>`: un archivo que se abre sin conexión y sin dependencias es lo que se puede adjuntar a un ticket.
- **Escribir en el dashboard es una excepción señalada.** Incidents es la única página que muta (evaluar y cambiar estado); lecturas con `read_only=True`, escrituras con `connection()` + `clear_caches()`.