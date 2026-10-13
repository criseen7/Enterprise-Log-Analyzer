# Enterprise Log Analyzer

Plataforma de observabilidad y análisis de logs para entornos empresariales. Carga logs
de forma incremental, los normaliza, detecta errores críticos, agrupa patrones
recurrentes y expone métricas en un dashboard interactivo con exportación de reportes.

**Estado:** en desarrollo activo. Fases 0–5 completadas (189 tests; 7 son smoke del dashboard y se saltan sin Streamlit) — ver
[docs/PLAN.md](docs/PLAN.md).

---

## Por qué este proyecto no es "otro visor de logs"

Un visor de logs que duplica eventos miente. Este proyecto se construye sobre tres
invariantes, y cada uno tiene una razón concreta:

| Invariante | Problema que resuelve |
|---|---|
| **Idempotencia de ingesta** | Sin ella, reiniciar el watcher duplica eventos y el dashboard reporta números falsos |
| **UTC como storage canónico** | Logs de distintos servicios llegan en zonas distintas; sin normalizar, "errores por hora" es incorrecto |
| **Líneas no parseadas se conservan** | Descartar lo que no se entiende hace la pérdida de datos invisible |

Las decisiones de arquitectura y sus alternativas descartadas están en
[`docs/decisions/`](docs/decisions/).

---

## Arquitectura

```mermaid
flowchart TD
    subgraph ingest
        L[logs/ read-only] --> R[Ingestor<br/>watchdog · offsets · rotación]
    end
    subgraph parse
        R --> P[Parser Registry<br/>config-driven · multilínea]
    end
    subgraph store
        P --> D[(SQLite WAL<br/>events · log_files<br/>incidents · ingest_runs)]
    end
    subgraph analysis
        D --> A[Analysis Engine<br/>errores · patrones<br/>métricas · anomalías · alertas]
    end
    subgraph present
        A --> U[Dashboard<br/>Streamlit + Plotly]
    end
    A --> X[exports/<br/>CSV · HTML]
    U --> X
```

En Docker (Fase 6) `Ingestor` y `Dashboard` son **servicios separados**: eso elimina la
contención de escritura de SQLite por diseño, no por ajuste de parámetros.

### Capa de ingesta (añadida al diseño original)

El flujo original iba de `logs/` directo al parser. Eso rompe en producción por dos
motivos, y por eso la ingesta es una capa propia:

- **Tracking de offset**: se lee solo lo nuevo, y se sabe exactamente dónde quedó cada archivo.
- **Detección de rotación**: un log rotado por tamaño se reinicia a 0; si no se detecta,
  todo su contenido se vuelve a insertar.

---

## Estructura

```
Enterprise-Log-Analyzer/
├── app/
│   ├── config.py               Settings tipados · YAML + env
│   ├── ingest/
│   │   ├── reader.py           offset, rotación, ensamblado multilínea
│   │   └── watcher.py          watchdog con debounce
│   ├── parser/
│   │   ├── registry.py         PatternSpec · detect() · parse()
│   │   ├── patterns/           apachev2.yaml · iis.yaml · weblogic.yaml · json.yaml
│   │   └── normalization.py    timestamp → UTC · nivel → severity
│   ├── db/
│   │   ├── connection.py       WAL · busy_timeout · contextmanager
│   │   ├── schema.sql  migrations/  repository.py
│   ├── analysis/
│   │   ├── error_rules.yaml     categorías de error · orden = prioridad
│   │   ├── errors.py            ErrorRules · validación al cargar
│   │   ├── fingerprint.py       normalización · sha256
│   │   ├── metrics.py  patterns.py  anomalies.py  backfill.py
│   │   └── __main__.py          CLI: python -m app.analysis
│   ├── ui/
│   │   ├── app.py  pages/  components/  theme.py
│   └── reports/                CSV · HTML
├── docs/
│   ├── PLAN.md                 hoja de ruta completa
│   └── decisions/              ADR-001, ADR-002, …
├── logs/                       entrada (read-only en Docker)
├── data/                       app.db
├── exports/                    reportes generados
├── tools/generate_sample_logs.py
├── tests/
│   ├── fixtures/               logs golden por formato
│   └── expected/               JSONL con la salida esperada
├── config.yaml  .env.example
├── requirements.txt  requirements-dev.txt
└── Dockerfile  docker-compose.yml  Makefile
```

---

## Configuración

Todo se declara en `config.yaml`; las variables de entorno tienen prioridad para permitir
overrides en Docker sin editar archivos.

```bash
cp .env.example .env
cp config.yaml config.local.yaml   # opcional: overrides locales
```

Las claves de entorno siguen el patrón `ELA_` + ruta en mayúsculas:
`ELA_DB_PATH`, `ELA_LOG_DIR`, `ELA_POLL_INTERVAL`.

---

## Puesta en marcha

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 1. Crear la base de datos y aplicar el esquema
python -m app.db.migrations

# 2. Generar un dataset sintético para el demo
python tools/generate_sample_logs.py

# 3. Ingerir los logs (idempotente: una 2ª pasada inserta 0)
python -m app.ingest --once
python -m app.ingest --status     # cobertura de parseo y últimas pasadas

# 4. Analizar (sin necesidad de dashboard)
python -m app.analysis                  # resumen general
python -m app.analysis --top 8          # errores agrupados por fingerprint
python -m app.analysis --hourly         # errores por hora
python -m app.analysis --anomalies      # z-score sobre el conteo horario
python -m app.analysis --patterns       # patrones en la última ventana
python -m app.analysis --backfill       # rellena la huella de datos antiguos

# 5. Alertas: abrir/actualizar incidentes y exportarlos (Fase 5)
python -m app.alerts                    # evalúa la ventana y abre incidentes
python -m app.alerts --list --status OPEN
python -m app.alerts --export csv > incidents.csv

# 6. Lanzar el dashboard (Fase 4)
streamlit run app/ui/app.py
```

O en un solo paso: `make demo`.

### Verificar la instalación

```bash
make test        # o: python -m unittest discover -s tests -t .
make check-db    # debe imprimir: journal_mode = wal

# Golden files: falla si la salida del parser cambió sin regenerarlos.
python -m tests.regenerate_golden --check
python -m tests.regenerate_golden   # regenera (revisar el diff antes de aceptar)
```

---

## Stack

| Capa | Tecnología | Razón |
|---|---|---|
| Backend | Python, SQLite | Cero operación; SQLite con WAL resuelve concurrencia lector/escritor |
| Parsing | Regex compilados con *named groups*, config-driven | Agregar un formato es agregar YAML, no código |
| Análisis | SQL + Pandas | Agregación en SQL; nunca cargar la tabla completa en memoria |
| Frontend | Streamlit, Plotly | Dashboard declarativo, heatmap listo |
| Ingesta | watchdog | Detección de cambios en archivos |
| DevOps | Docker, Docker Compose | Servicios separados, volúmenes para `data/` y `exports/` |

---

## Funcionalidades

### En el MVP (fases 0–5)

1. ✅ **Parser inteligente** — detecta `timestamp`, `nivel`, `mensaje`, `servicio`,
   `logger` desde configuración, con normalización a UTC.
2. ✅ **Ingesta idempotente** — offsets, rotación, ensamblado multilínea (stack traces Java).
3. ✅ **Detección de errores críticos** — `timeout`, `connection refused`,
   `null pointer`, `out of memory`, `failed login`, con categorías (reglas en
   YAML) y severidad normalizada.
4. ✅ **Patrones recurrentes** — fingerprint por mensaje normalizado (dígitos, UUIDs,
   IPs y hex colapsan a placeholders) para agrupar lo que en realidad es el mismo
   error, más recurrencia por ventana y anomalías por z-score.
5. ✅ **Dashboard** — KPIs, errores por hora, top errores, timeline, heatmap día×hora (4 páginas: Overview/Events/Incidents/Settings).
6. ✅ **Alertas** — incidentes con dedup por fingerprint y cooldown (vía `notified_at`); banner + lista y estados OPEN/ACK/RESOLVED en el dashboard; export CSV (utf-8-sig) y HTML autocontenido; CLI `python -m app.alerts`.
7. **Métricas de la propia herramienta** — `ingest_runs` ya se registra en cada
   pasada; el panel llega con el dashboard.

### Post-MVP (backlog, ver [docs/PLAN.md](docs/PLAN.md) §6)

- Formato IIS
- Anomalías avanzadas (IQR, estacionalidad)
- Correlación de eventos
- Búsqueda estilo Splunk
- Export PDF · auth · multi-tenancy · PostgreSQL · Prometheus

---

## Formatos soportados

Cada formato es **un archivo YAML** en `app/parser/patterns/`. Agregar IIS o
WebLogic es agregar un archivo, no escribir Python — ver
[ADR-002](docs/decisions/ADR-002-parsers-declarativos.md).

| Formato | `kind` | Estado |
|---|---|---|
| Apache / Nginx (CLF) | `regex` | ✅ implementado |
| JSON (una línea por registro) | `json` | ✅ implementado |
| WebLogic / java.util.logging | `regex` | ✅ implementado (multilínea) |
| IIS | `regex` | backlog |

Un patrón `regex` **debe** declarar el grupo con nombre `ts`; `level`, `service`,
`logger`, `status` y `message` son opcionales. El contrato se verifica al cargar
los patrones, no durante la ingesta: un YAML incompleto rompe el arranque.

Para JSON se declaran listas de claves candidatas por campo, con rutas de punto:

```yaml
fields:
  level: [level, levelname, severity, loglevel, log.level]
  message: [message, msg, '@message', log, log.message]
```

---

## Sistema de severidad

Niveles crudos (diferentes por formato) se normalizan a una escala única:

```
TRACE 10 · DEBUG 20 · INFO 30 · WARN 40 · ERROR 45 · FATAL 50 · UNKNOWN 0
```

`UNKNOWN` se asigna a líneas no parseadas. Se conservan y se hacen visibles en el
dashboard como "cobertura de parseo", en lugar de descartarse en silencio.

---

## Clasificación y agrupación

Dos preguntas distintas, dos columnas nuevas en `events`:

- **`category` — ¿qué tipo de problema es?** Sale de
  `app/analysis/error_rules.yaml`: una lista ordenada donde **gana la primera
  regla que coincide** (`failed login` → `auth`, `connection refused` →
  `connection`, 5xx → `http-5xx`). Añadir una categoría es añadir una regla,
  no código. Un YAML mal formado falla **al cargar**, nunca durante la ingesta.
- **`fingerprint` — ¿es otra vez el mismo error?** `sha256` truncado a 16 hex
  sobre `categoría + mensaje normalizado`, donde lo que varía entre ejecuciones
  se sustituye por `<UUID> <TS> <IP> <HEX> <NUM>`. 100 accesos fallidos que
  sólo cambian en `code=` son **1** fingerprint, no 100 barras de una unidad.

El orden de esas sustituciones y las alternativas descartadas están en
[ADR-004](docs/decisions/ADR-004-event-fingerprint.md).

### Métricas

Todo se agrega en SQL; ninguna carga la tabla `events` entera en memoria.

| Comando | Qué responde |
|---|---|
| `python -m app.analysis` / `--overview` | cuántos eventos, cuántos parseados, cuántos errores, fingerprints distintos |
| `--hourly` | errores por hora, rango `[since, until)` |
| `--top N` | los N errores más repetidos, agrupados por fingerprint |
| `--patterns --window MIN` | qué se repite en la ventana; por defecto acaba en el **último evento** del dataset, no en el reloj |
| `--anomalies` | qué hora se sale de lo normal: z-score leave-one-out, sólo picos |
| `--backfill [--recalc]` | rellena `fingerprint`/`category` de datos antiguos; `--recalc` reescribe lo ya clasificado |

---

Los puntos de `cache_data`, filtros sin re-parseo y cooldown de alertas quedan
recogidos aquí; `docs/pitfalls.md` los consolida al cerrar la fase 6.
---

## Licencia

Por definir.