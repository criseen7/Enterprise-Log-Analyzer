# Plan de Implementación — Enterprise Log Analyzer

> Documento de diseño y hoja de ruta. Reemplaza la descripción informal del README.
> Si el código y este documento discrepan, este documento tiene la razón.

**Perfil del proyecto:** semi-empresarial. Se aplica ingeniería real (idempotencia, WAL,
rotación, tests) pero cada decisión se documenta como artefacto de aprendizaje y el
demo se prioriza sobre el hardeningrayado.

---

## 1. Decisiones ya tomadas

| # | Decisión | Motivo |
|---|---|---|
| D1 | SQLite + **WAL**, un solo escritor | Suficiente para semi-enterprise, cero operación, y es la lección más transferible del proyecto |
| D2 | Parsers **config-driven** con named groups | Agregar formato = agregar YAML, no código |
| D3 | Clave de idempotencia `(file_id, offset)` + `UNIQUE` | Único diseño que sobrevive a rotación |
| D4 | Timestamps **normalizados a UTC**, original conservado | Sin esto los gráficos por hora mienten |
| D5 | `pdf` **aplazado**; CSV + HTML en su lugar | Dep pesado, cobertura de necesidad ~90% |
| D6 | Fases 0–5 = MVP. Bloque "Features" = post-MVP | 3 funcionalidades extra son 3 proyectos |

Cada decisión tiene su ADR en `docs/decisions/`.

---

## 2. Arquitectura

El README original omite la capa de ingesta y no considera rotación ni concurrencia:

```
 logs/  (ro) data/app.db         exports/
 │                      │                   ▲
    ▼                      │                   │
┌────────────┐   ┌──────────────────────┐  ┌───┴────────────┐
│  Ingestor  │──►│  Parser Registry     │──►│ SQLite (WAL)   │
│  watchdog  │   │  config-driven       │  │ events         │
│  offsets   │   │  + multilínea        │  │ log_files      │
│  rotación  │   └──────────────────────┘  │ incidents      │
└────────────┘            │                │ ingest_runs    │
                         ▼                └───────┬────────┘
              ┌──────────────────────┐         │
              │  Analysis Engine     │◄────────┤  queries SQL
              │  errores · patrones  │         │  (no full-table)
              │  métricas · alertas  │         │
              └──────────┬───────────┘         │
                         │                     │
              ┌──────────▼───────────┐         │
              │  Streamlit Dashboard │─────────┘
              │  Overview / Events / │
              │  Incidents / Settings│         │
              └──────────────────────┘
```

**Servicios separados en Docker (Fase 6):** `analyzer` (watcher, único escritor) y
`dashboard` (Streamlit, solo lectura). Esto elimina la contención SQLite por diseño,
no por tuning.

---

## 3. Esquema de datos v1

```sql
CREATE TABLE schema_version (version INT PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE log_files (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, inode INTEGER,
  size INTEGER DEFAULT 0, last_offset INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT, rotated_at TEXT
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES log_files(id),
  offset INTEGER NOT NULL,
  fingerprint TEXT,
  timestamp_utc TEXT, timestamp_raw TEXT, source_tz TEXT,
  level_raw TEXT, level_norm TEXT, severity INT,
  service TEXT, logger TEXT, message TEXT, raw_line TEXT,
  parsed_ok INT DEFAULT 1,
  UNIQUE(file_id, offset)
);

CREATE TABLE incidents (...);
CREATE TABLE ingest_runs (...);
CREATE INDEX idx_events_ts  ON events(timestamp_utc);
CREATE INDEX idx_events_fp  ON events(fingerprint);
CREATE INDEX idx_events_sev ON events(severity, timestamp_utc);
```

`ingest_runs` es doble beneficio: **observabilidad del propio analizador** (demo) y
detección de regresiones de ingesta.

---

## 4. Estructura de módulos

```
app/
  config.py                     Settings dataclass · YAML + env
  ingest/
    reader.py                   lectura por offset, rotación, ensamblado multilínea
    watcher.py                  watchdog, debounce, shutdown limpio
  parser/
    registry.py                 PatternSpec · detect() · parse()
    patterns/{apachev2,iis,weblogic,json}.yaml
    normalization.py            ts → UTC · nivel → severity canónica
  db/
    connection.py               WAL · busy_timeout · contextmanager
    schema.sql  migrations/  repository.py
  analysis/
    errors.py  patterns.py  metrics.py  anomalies.py  alerts.py
  ui/
    app.py  pages/  components/  theme.py
  reports/
tools/generate_sample_logs.py   datos sintéticos para el demo
tests/
  fixtures/{apache,json}/*.log
  expected/{apache,json}/*.jsonl
docs/decisions/ADR-00N-*.md     artefactos de aprendizaje
```

---

## 5. Fases, tareas y criterios de aceptación

### Fase 0 — Cimientos ✅ *completada 2026-10-05*

- [x] Estructura de paquetes
- [x] `requirements.txt` pinneado + `requirements-dev.txt`
- [x] `.gitignore`
- [x] `config.yaml` + `.env.example` + `app/config.py`
- [x] `schema.sql` v1 + runner de migraciones (`python -m app.db.migrations [--status]`)
- [x] `db/connection.py` (WAL, `busy_timeout`, `foreign_keys`, `query_only` para solo lectura)
- [x] `Makefile` (`migrate`, `test`, `check-db`, `seed`, `demo`, `dashboard`)
- [x] Tests de conexión, concurrencia, config y migraciones — **47 tests**
- [x] ADR-001 (SQLite WAL)

**Aceptación — verificada:**
- [x] `PRAGMA journal_mode` → `wal`
- [x] 2 escritores + 1 lector, 0 ocurrencias de `database is locked`
- [x] Migración aplica, es idempotente y rechaza una base más nueva que el código
- [x] `EXPLAIN QUERY PLAN` confirma que los índices de severidad y timestamp se usan

**Desviaciones y hallazgos registrados durante la implementación:**
- El test de estrés concurrente **no es evidencia de WAL**: con escrituras cortas SQLite
  aguanta incluso en `DELETE`. La verificación real es determinista: un lector con
  transacción abierta bloquea el `COMMIT` del escritor en `DELETE` y no en WAL
  (`test_delete_journal_would_block_the_writer` es la contraprueba).
- `executescript()` ejecuta un COMMIT implícito: envolverlo en `transaction()` falla
  con `cannot commit - no transaction is active`. El DDL es todo `IF NOT EXISTS`,
  así que no hace falta atomicidad.
- `mode=ro` de URI no sirve para solo lectura sobre WAL (necesita escribir en `-shm`).
  Se usa `PRAGMA query_only=ON`.

---

### Fase 1 — Parser ✅ *completada 2026-10-05*

- [x] `severity.py` — escala canónica (0..50) + alias de syslog / Java / IIS
- [x] `normalization.py` — timestamp → UTC, meses en inglés independientes de locale
- [x] `registry.py` — `PatternSpec`, carga validada, `detect()`, `parse_line()`
- [x] `patterns/apachev2.yaml` + `patterns/json.yaml`
- [x] Fixtures: 2 de Apache, 2 de JSON (tz mixta, claves alternativas)
- [x] Golden files en `tests/expected/` + regenerador con `--check`
- [x] Tests del parser — **38 tests**
- [x] ADR-002 (parsers declarativos)

**Aceptación — verificada:**
- [x] Ratio de parseo **100%** en los 4 fixtures (42 líneas) · umbral exigido 95%
- [x] Líneas no parseadas conservadas con `parsed_ok=0` y motivo en `parse_error`
- [x] Timestamps en UTC; el mismo instante escrito de 3 formas → un solo valor
- [x] `source_tz` siempre registrado, incluidas las zonas asumidas
- [x] Un patrón mal configurado rompe el arranque, no la ingesta

**Desviaciones y hallazgos registrados durante la implementación:**
- `re.VERBOSE` es obligatorio para escribir regex legibles en YAML: sin él, cada
  salto de línea del bloque se interpreta como literal y no matchea nada.
- `strptime` con `%b` depende de `LC_TIME`: un contenedor con locale `es_ES` deja
  de entender `Oct`. Se sustituye el mes por su número antes de parsear (`%m`).
- Un candidato de clave JSON que resuelve a un dict se salta; si no, `log` gana a
  `log.message` y el mensaje es el dict serializado.
- `default_timezone` ausente en un patrón significa "hereda de config.yaml".
  Declararlo sobreescribiría la config global en silencio.
- **WebLogic quedó sin implementar en la Fase 1 a propósito.** Su dificultad
  real es el ensamblado multilínea de stack traces, no el regex: implementarlo
  antes habría producido un parser que pasa los tests y parte el stack trace en
  N eventos basura. `PatternSpec.multiline` quedó reservado y se usó en la
  Fase 2, donde WebLogic sí está implementado (`patterns/weblogic.yaml`).

---

### Fase 2 — Ingesta ✅ *completada 2026-10-07*

**Tareas:** `reader.py` con offset tracking · detección de rotación (tamaño `< last_offset`
**o** inode cambiado) · ensamblado multilínea · `watcher.py` con debounce ·
inserción con `INSERT OR IGNORE` en transacción única · generador de logs
sintéticos · CLI (`python -m app.ingest`)

**Aceptación — verificada:**
- [x] Re-procesar el mismo archivo 2 veces → `events_inserted=0` en la 2ª
- [x] Rotación simulada → nuevo `log_files.id`, cero duplicados
- [x] Stack trace Java → **1** evento con el trace completo en `message`
- [x] 112 tests en verde · golden files al día

**Implementado:**
- `app/ingest/reader.py` — offsets en **bytes**, sólo líneas terminadas en `\n`
  (una línea a medio escribir queda para la próxima pasada), ensamblado de
  entradas multilínea con tope de 4 MB por seguridad.
- `app/ingest/ingestor.py` — orquestación y orden de operaciones:
  resolver → leer/parsear → insertar → **entonces** avanzar `last_offset`.
- `app/ingest/watcher.py` — `WatchState` con debounce + watchdog si está
  disponible, polling como camino por defecto (más lento, pero no depende de una
  dependencia opcional).
- `app/ingest/__main__.py` — `python -m app.ingest [--once|--watch|--status]`.
- `app/db/repository.py` — `resolve_log_file()` con los cuatro caminos de rotación.
- `app/db/migrations/_002_log_files_identity.sql` + `SCHEMA_VERSION = 2`.
- `app/parser/patterns/weblogic.yaml` — habilitado por el ensamblado multilínea,
  con `zone_map` para zonas abreviadas (`CEST`, `WIB`…).
- `tools/generate_sample_logs.py`.
- ADR-003 (identidad de archivo por `(inode, generation)`).

**Hallazgos registrados durante la implementación:**

- **La identidad por `path` perdía datos en `copytruncate`.** `UNIQUE(path)` hace
  que la ruta sea el identificador; con truncado la ruta se mantiene y el
  `last_offset` viejo queda más allá del contenido nuevo, así que la ingesta
  reportaba "0 líneas nuevas" mientras el archivo crecía. Cambiado a
  `(inode, generation)` — ADR-003. La migración reconstruye la tabla; un
  `CREATE TABLE IF NOT EXISTS` no habría servido.
- **Los regex multilínea en bloque YAML necesitan `re.VERBOSE`.** Sin él, los
  saltos de línea del propio patrón se toman como literales y el regex deja de
  coincidir sin ningún error de sintaxis: el síntoma es "ningún patrón coincide",
  que no apunta al regex. Apache lo declaraba con `flags:` y WebLogic no, y
  fracasaba en silencio. Se aplica `VERBOSE` automáticamente cuando el patrón
  contiene saltos de línea.
- **Ensamblar multilínea rompe JSON.** Pegar líneas a un registro JSON produce
  un JSON inválido y el evento entero se pierde como `parsed_ok=0`. Por eso el
  ensamblado sólo aplica si el patrón declara `multiline: true`; una línea sin
  patrón en un formato de una línea se conserva como evento propio, no se descarta.
  Es el uso de `PatternSpec.multiline`, reservado en la Fase 1.
- **`detect()` probaba el regex contra el texto completo.** Con una entrada ya
  ensamblada, los saltos de línea la hacían no coincidir con ningún patrón — se
  clasificaba como "sin formato" justo cuando contenía el stack trace completo.
  Ahora sólo se prueba la cabecera cuando el patrón es `multiline`.
- **El orden de descubrimiento afecta al estado.** `discover()` ordena
  alfabéticamente, así que `app.log` se procesa antes que `app.log.1`; la fila
  que aún declaraba `app.log` se desactivaba al crear la del archivo nuevo. La
  rama de rename ahora reactiva `is_active = 1`.
- **`_coerce` no contemplaba `Path`.** Cualquier override por variable de
  entorno de una ruta quedaba como `str` y reventaba después en `.is_dir()`.
- **Deuda consciente:** `copytruncate` con exactamente el mismo tamaño es
  indistinguible de "no cambió" leyendo sólo el tamaño. Anotado en ADR-003.

---

### Fase 3 — Análisis

**Tareas:** reglas de error desde YAML · **fingerprint** = mensaje normalizado
(dígitos, UUIDs, hex, IPs, timestamps → placeholders) + categoría, hasheado ·
`patterns.py` (recurrencia por ventana) · `metrics.py` con agregaciones **en SQL** ·
`anomalies.py` versión MVP (z-score sobre conteo horario)

**Aceptación:**
- "Errores por hora" coincide con el conteo manual del fixture (valor esperado escrito a mano)
- 100 accesos fallidos idénticos salvo el código → **1** fingerprint

---

### Fase 4 — Dashboard

**Tareas:** 4 páginas (Overview / Events / Incidents / Settings) · Plotly: línea
errores-hora, barra top errores, **heatmap día×hora**, area apilada por severidad ·
filtros en sidebar · `st.cache_data(ttl=30)` para queries, `st.cache_resource` **solo**
para conexiones · estados vacíos accionables · auto-seed si la DB está vacía

**Aceptación:**
- Navegar el dataset sintético completo sin errores
- Los filtros no obligan a re-parsear (usan índices)
- Cero estado vacío sin explicación

> `cache_data` → queries (serializables). `cache_resource` → conexiones y singletons.
> Cachear una `Connection` con `cache_data` falla: no es serializable.

---

### Fase 5 — Alertas

**Tareas:** reglas umbral+ventana · upsert de incidente por fingerprint ·
**cooldown** vía `notified_at` · banner visual + lista de incidentes ·
export **CSV** y **HTML** autocontenido · estados OPEN/ACK/RESOLVED

**Aceptación:**
- 5.000 timeouts idénticos → **1 incidente**, 0 notificaciones repetidas
- El export CSV abre correctamente en Excel (encoding `utf-8-sig`)

---

### Fase 6 — Docker *(post-MVP)*

Multi-stage · `python:3.12-slim` pinneado · usuario no-root · `.dockerignore` ·
compose con **volúmenes** para `logs:ro`, `data`, `exports` · servicios `analyzer` y
`dashboard` separados · healthchecks.

**Aceptación:** `docker compose up` desde repo limpio; `data/app.db` sobrevive a `down` + `build`.

---

## 6. Explícitamente fuera de alcance (post-MVP)

- Formato IIS
- Anomalías avanzadas (IQR, seasonalidad)
- Correlación de eventos (ventana temporal + entidades comunes)
- Búsqueda estilo Splunk (lenguaje de consultas propio)
- Export PDF
- Auth / multi-tenancy · PostgreSQL · Prometheus

---

## 7. Artefactos de aprendizaje

- `docs/decisions/ADR-00N-*.md` — una por cada decisión D1–D6: contexto, opciones
  descartadas, consecuencia
- `docs/parsing-cookbook.md` — cómo se construyó cada regex y por qué
- `docs/pitfalls.md` — WAL, idempotencia, tz, multilínea, `cache_data`
- **README** — diagrama Mermaid, quickstart, capturas, sección "Qué aprendí"

---

## 8. Estrategia de demo (ángulo portfolio)

1. `make demo` — un comando: genera logs → ingesta → levanta el dashboard
2. **Escenario narrativo:** "jueves 14:00, el pool de conexiones se agota →
   3.000 NullPointerException → el z-score dispara la anomalía → incidente crítico
   exportado a CSV"
3. Las decisiones de ingeniería se cuentan como argumento de venta: *"maneja rotación
   e idempotencia, así que los números del dashboard son confiables"*
4. `ingest_runs` visible en Settings → la herramienta se monitorea a sí misma

---

## 9. Riesgos

| Riesgo | Mitigación |
|---|---|
| Scope creep hacia "Features" | Regla dura: no se toca Fase 6 sin cerrar 0–5 |
| WebLogic multilínea | Resuelto en Fase 2 (`multiline` + `re.VERBOSE`); IIS sigue fuera de alcance |
| Dashboard lento con muchos logs | Agregación en SQL + índices + caché; nunca `read_sql` completo |
| Bugs de timezone | Fixtures con tz mixta y test dedicado |
| Datos duplicados | Test de regresión en CI, no opcional |
| Frode de métricas (líneas no parseadas) | `parsed_ok=0` visible en el dashboard como "cobertura de parseo" |

---

## 10. Verificación por fase

| Fase | Comando de aceptación |
|---|---|
| 0 | `python -m unittest discover tests -v` · `PRAGMA journal_mode` = `wal` |
| 1 | `python -m unittest tests.test_parser -v` |
| 2 | `python -m unittest tests.test_ingest -v` · doble ingesta = 0 inserts |
| 3 | `python -m unittest tests.test_analysis -v` |
| 4 | `streamlit run app/ui/app.py` con dataset sintético |
| 5 | `python -m unittest tests.test_alerts -v` · 5k errores → 1 incidente |
| 6 | `docker compose up --build` |

**Orden de ejecución:** 0 → 1 → 2 → 3 → 4 → 5. Las fases 2 y 1 son el núcleo de
dificultad; si el tiempo aprieta, el punto de corte honesto es "parser + ingesta
sólida" sin dashboard.