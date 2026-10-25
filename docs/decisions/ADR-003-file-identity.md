# ADR-003 — Identidad de archivo por `(inode, generation)`, no por ruta

**Estado:** aceptada
**Fecha:** 2026-10-07
**Fase:** 2 (Ingesta)
**Relacionada con:** D3 en [docs/PLAN.md](../PLAN.md)

---

## Contexto

La Fase 0 definió la idempotencia de eventos como `UNIQUE (file_id, offset)`, con
`file_id` apuntando a una fila de `log_files`. Lo que no se había decidido era
qué hace que dos lecturas del mismo archivo sean la misma fila.

La primera implementación usaba `path TEXT NOT NULL UNIQUE`: la ruta **era** la
identidad. Eso parece correcto hasta que aparece logrotate, que es siempre:

| Rotación | Qué pasa en disco | Qué ve `path` como identidad |
|---|---|---|
| `rename` (`app.log` → `app.log.1`) | cambia la ruta, el contenido y el inode sobreviven | fila nueva, contenido ya ingerido se relee |
| `copytruncate` | la ruta se queda igual, el inode se queda igual, el contenido cambia | fila vieja con `last_offset` alto; lo nuevo queda detrás de ese offset y **no se lee nunca** |

El segundo caso es el que pierde datos en silencio. No hay error, no hay
duplicado: simplemente `last_offset` apunta más allá de donde está el contenido
nuevo, y la ingesta reporta "0 líneas nuevas" mientras el archivo crece.

## Decisión

**La identidad de un archivo de log es `(inode, generation)`.** La ruta pasa a
ser metadata mutable: se actualiza al detectar un rename, pero no identifica nada.

- **`inode`** — sobrevive al `rename`. Es lo que hace que `app.log` y
  `app.log.1` sean el mismo archivo, y que la misma ruta apuntando a otro inode
  sea un archivo distinto.
- **`generation`** — entero que empieza en 0 y sube cada vez que el contenido de
  ese inode se reescribe. Cubre `copytruncate`, que conserva el inode y por eso
  el inode solo no basta.

`UNIQUE (inode, generation)` da una clave candidata estable en el tiempo, y
`events.file_id` sigue apuntando a esa fila: `UNIQUE (file_id, offset)` no cambia.

### Los cuatro caminos en `resolve_log_file()`

| Situación | Se detecta por | Acción |
|---|---|---|
| Archivo nuevo | inode desconocido | fila nueva, `generation` siguiente libre, offset 0 |
| Sigue creciendo | `size ≥ last_offset` | continúa en `last_offset` |
| Rotado por `rename` | inode conocido con otra ruta | se actualiza `path`, **no** se relee |
| Rotado por truncado | `size < last_offset` con el mismo inode | fila nueva con `generation + 1`, offset 0 |
| Inodo reciclado | inode conocido, otra primera línea | fila nueva con `generation + 1`, offset 0 |

El último caso se añadió en v7: al borrar un archivo, el SO puede reutilizar su
inodo para otro distinto. Sin más señal, la fila vieja parece un `rename` y su
`last_offset` deja el archivo nuevo sin leer. `log_files.head_hash` guarda el
SHA-256 de la **primera línea completa**; añadir líneas al final no lo cambia,
reescribir el archivo sí. Es la misma señal que cubre `copytruncate` de igual
tamaño, que el tamaño solo no puede distinguir.

La fila anterior queda `is_active = 0`, y lo mismo le pasa a cualquier otra fila
que ocupe la misma `path`: solo una fila con una ruta dada describe lo que hoy
existe ahí.

### El orden de las operaciones también es la decisión

```python
resolve_log_file()        # decide fila y offset
leer y parsear            # sólo líneas terminadas en \n
insert_events()           # INSERT OR IGNORE
update_progress()         # SOLO AHORA se avanza last_offset
```

Insertar antes de mover el offset es lo que hace recuperable un fallo a mitad de
pasada: si la transacción revienta, `last_offset` no se movió y la siguiente
pasada reintenta exactamente esas líneas. Al revés, un offset adelantado sin
eventos es pérdida irreversible, porque el archivo ya no volverá a entregar esas
líneas.

## Alternativas descartadas

**`path` como identidad (lo que había).** El fallo `copytruncate` es silencioso
y no tiene detección posible por tamaño. Además, cada rename generaba una fila
nueva con el offset a 0, volviendo a leer contenido ya ingerido.

**Inode solo, sin `generation`.** Detecta el `rename`, pero no el truncado: al
conservar el inode y el offset, el contenido nuevo queda inalcanzable. Es el
caso que más a menudo se confunde con "la ingesta funciona pero no lee".

**Hash del contenido como identidad.** Requiere leer el archivo entero en cada
pasada para decidir si es el mismo, que es exactamente lo que el offset por bytes
evita. Además un archivo que crece tiene un hash distinto en cada escritura. Lo
que sí se usa (desde v7) es el hash de solo la primera línea: no identifica por
sí solo, pero desambigua un inodo cuando el sistema de archivos lo recicla.

**`mtime` como señal de cambio.** No se decide identidad con eso: el mtime cambia
en cada `write()`, así que produciría una fila nueva por escritura. Sirve para
*disparar* una pasada, no para *clasificar* el archivo. Es por eso que el
watcher solo decide cuándo leer, y la idempotencia vive entera en la base.

**Borrar y recrear `log_files` en cada arranque.** Funciona en una demo y pierde
todo el histórico de offsets: cada arranque relee el dataset completo.

## Consecuencias

- Reingerir un archivo ya ingerido cuesta una consulta de `MAX(generation)`, no
  una lectura completa.
- La ruta ya no es única, así que cualquier consulta que la muestre debe filtrar
  por `is_active = 1` o agrupar por `file_id` — una ruta puede aparecer en varias
  filas históricas.
- Se añade un índice sobre `path` porque dejó de ser la clave.
- Añadir un formato de rotación nuevo significa añadir una detección en
  `resolve_log_file`, no cambiar el esquema.
- **Migración**: `SCHEMA_VERSION` sube de 1 a 2 con
  `migrations/_002_log_files_identity.sql`, que reconstruye `log_files`. Un
  archivo `CREATE TABLE IF NOT EXISTS` no habría servido: la tabla ya existe y la
  columna `generation` no aparece sola. Ver "Detalles no obvios" más abajo.
  `SCHEMA_VERSION` sube a 7 con `migrations/_007_log_files_head_hash.sql`, que
  añade `head_hash`; las filas históricas quedan en `NULL` y se rellenan en la
  primera pasada sin crear generación.

## Detalles que resultaron no obvios al implementar

**El orden de descubrimiento cambia el resultado.** `discover()` devuelve las
rutas ordenadas alfabéticamente, así que `app.log` se procesa antes que
`app.log.1`. Al crear la fila del archivo nuevo en `app.log`, la fila que aún
declaraba esa ruta (la que luego resultó ser el archivo renombrado) se
desactivaba. Por eso la rama de rename reactiva con `is_active = 1` explícitamente:
sin ese detalle, el archivo vivo aparecía como inactivo en el dashboard y su
`last_offset` quedaba huérfano.

**La detección de truncado no puede depender solo del tamaño.** Se compara
`size < last_offset`, que falla si el archivo se reescribe con **exactamente el
mismo tamaño**. Desde v7 lo cubre `head_hash`: la primera línea cambia aunque el
tamaño no. Queda un techo teórico —una reescritura que preserve también la
primera línea byte a byte— que ninguna señal barata puede distinguir; en un log
real la primera línea lleva timestamp, así que en la práctica cambia siempre.

**Leer la cabecera no es releer el archivo.** `head_hash` lee como mucho 4 KiB y
solo hasta el primer `\\n`. El coste por pasada es una lectura pequeña, no la
lectura completa que el offset por bytes evita. Mientras la primera línea esté a
medias no hay huella (`NULL`): comparar un prefijo incompleto crearía
generaciones espurias, y esas líneas todavía no se consumen de todos modos.

**Las generaciones vuelven a empezar en offset 0.** Dos generaciones del mismo
inode pueden tener eventos en offset 0. No chocan porque la clave es
`(file_id, offset)` y `file_id` es distinto — que es exactamente por qué la
idempotencia se define sobre `file_id` y no sobre `(path, offset)`.

## Evidencia

- `tests/test_ingest.py` — 25 tests: idempotencia, offsets en bytes, líneas
  parciales, truncado, rename, rename sobre ruta ocupada, stack trace multilínea,
  JSON no contaminado, corridas auditadas.
- `tests/test_config.py::MigrationTests::test_old_v1_database_upgrades_without_losing_schema`
  — una base v1 existente sube a v2 conservando los eventos.
- Verificación manual con `tools/generate_sample_logs.py`: primera pasada
  inserta, segunda inserta **0**, rotación por rename sin duplicados.

## Deuda consciente

- Una reescritura que conserve intacta la primera línea completa no se detecta;
  el tamaño y la cabecera son las dos señales baratas disponibles.
- `watchdog` no está instalado en el entorno actual, así que `watch()` cae en el
  camino de polling. Ambas rutas llaman a la misma
  `ingest_paths_transactional`, así que la idempotencia no depende de cuál se use;
  el debounce con watchdog no está cubierto por tests automatizados.
- `fingerprint` devuelve `None` a propósito: lo define la Fase 3. Ponerlo ahora
  sería adivinar y luego migrar datos.
