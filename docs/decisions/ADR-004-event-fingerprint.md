# ADR-004 — Fingerprint: `sha256(categoría + mensaje normalizado)`

**Estado:** aceptada
**Fecha:** 2026-10-08
**Fase:** 3 (Análisis)
**Relacionada con:** Fase 3 en [docs/PLAN.md](../PLAN.md) · ADR-002 (parsers declarativos)

---

## Contexto

La Fase 2 dejó la ingesta cerrada: eventos idempotentes, ordenados en el tiempo,
con severidad canónica. Lo que no hacía era responder a la pregunta que un
responsable de plataforma se hace primero: **¿esto es uno o son mil?**

`5000` eventos de "timeout de consulta" son `5000` filas en `events`. Cada una
es correcta y ninguna aporta. Un "top errores" agrupado por `message` crudo no
sirve: el mensaje trae un identificador distinto por línea (`code=4`,
`10.0.3.78`, `u123`, `30000ms`, un UUID), así que `COUNT(*) GROUP BY message`
devuelve 5000 grupos de 1. El agrupamiento tiene que sobrevivir a todo lo que
varía entre dos ocurrencias del mismo error y a nada más.

Había que decidir dos cosas: **qué se considera el mismo error** (fingerprint) y
**qué tipo de error es** (categoría). Están acopladas: sin categoría, "pago
rechazado" y "rollback" en el mismo servicio se agruparían solo por texto; sin
normalización, la categoría no basta porque sigue habiendo un identificador por
línea.

## Decisión

```python
fingerprint = sha256(f"{category}\n{normalize_message(message)}").hexdigest()[:16]
```

Se calcula **una vez, al insertar**, y se guarda en la base en dos columnas
nuevas (`events.category`, `events.fingerprint`) — migración `v3`. Cualquier
consulta agrupa por `fingerprint`; nunca vuelve a leer el mensaje.

### 1. Qué sustituye `normalize_message`

Orden fijo, no cosmético — cada paso depende de que los anteriores ya se
hicieron:

| # | Sustituto | Patrón | Por qué en ese lugar |
|---|---|---|---|
| 1 | `<UUID>` | `8-4-4-4-12` | antes que los números, o se parte en pedazos |
| 2 | `<TS>` | ISO 8601, CLF `07/Oct/2026:18:11:13 +0000`, fecha `YYYY-MM-DD` | contiene dígitos y guiones |
| 3 | `<IP>` | IPv4 | `10.0.3.78` se destrozaría en cuatro números |
| 4 | `<HEX>` | `0x1f`, hex largo ≥8 | `0x1f` contiene dígitos: un hex tardío no recupera nada |
| 5 | `<NUM>` | `\d+(\.\d+)?` **sin `\b`** | al final ya no queda nada que proteger |
| — | colapso de espacios | `" ".join(text.split())` | la sustitución deja huecos dobles |

`NUMBER` va sin límites de palabra **a propósito**: `u123`, `order_9871` y
`30000ms` tienen que normalizarse. Con `\b` en ambos lados no lo harían y dos
errores idénticos salvo el identificador acabarían en huellas distintas.

### 2. Qué es la categoría

`app/analysis/error_rules.yaml`, cargado en `ErrorRules`: lista ordenada donde
**el orden es la prioridad** y gana la primera que coincide. Las claves son
`id`, `category` (obligatorios), `patterns` (no vacío) y `description`
(opcional): cualquier otra clave es un error de configuración. Regex con
`re.IGNORECASE`.

La clasificación se aplica sobre lo que se va a mostrar (`result.message`, o
`raw_line` si el evento no se parseó), no sobre el mensaje ya normalizado: la
regla `"Failed login for .*"` tiene que poder ver el identificador que acaba de
llegar.

### 3. La categoría entra en el hash

Dos mensajes idénticos en categorías distintas son dos problemas distintos
(`rollback` como `transaction` frente a `rollback` como otra cosa). Cuando no
hay categoría, el separador sigue ahí con cadena vacía: el esquema no cambia
según vengan o no de reglas.

### 4. Escritura en dos momentos

- **Ingesta** (`ingest_file`): clasifica y hashea por evento, en la misma
  transacción que inserta.
- **Backfill** (`python -m app.analysis --backfill [--recalc]`): para datos
  anteriores a la Fase 3 (`fingerprint IS NULL`) o para reclasificar con las
  reglas actuales. Batch en transacción única.

## Alternativas descartadas

**Agrupar por `message` crudo.** No agrupa nada: el identificador por línea
hace que cada evento sea su propio grupo. Es el caso exacto que la huella existe
para evitar.

**Guardar el mensaje normalizado y agrupar por él.** Funciona en SQL, pero el
texto normalizado es largo, hay que indexarlo, y la tabla del dashboard muestra
huellas cortas. El hash es la misma agrupación a 16 caracteres.

**Normalizar sólo dígitos (sin IP/UUID/hex).** Es la mitad del trabajo: `code=4`
se arregla pero `10.0.3.78` no, y dos accesos fallidos desde distinta IP siguen
siendo dos grupos.

**Clustering difuso / similitud de cadenas.** No determinista (mismo dataset,
dos ejecuciones → grupos distintos), coste cuadrático y no indexable en SQL.
Descartado para un MVP; queda en el backlog si algún día hace falta.

**Incluir la severidad en la huella.** Rompe la agrupación que interesa: el
mismo timeout que aparece como `WARN` y luego como `ERROR` es el mismo incidente
creciendo. La severidad se mira al contar, no al agrupar.

**Agrupar sin categoría (sólo mensaje).** Ahorraría el paso de clasificación,
pero dos errores con texto idéntico y origen distinto se fusionarían, y la
Fase 5 deduplicaría incidentes que son distintos. Cuesta una columna y se paga.

## Consecuencias

- `events.fingerprint` **no es única**: la misma huella a distintas horas es la
  normalidad. Nada debe declararla `UNIQUE`.
- La normalización se aplica a una copia en memoria del mensaje; `message` en la
  base conserva el texto original tal cual llegó (auditable).
- `500` y `502` comparten huella: mismo texto normalizado, misma categoría
  `http-5xx`. Es correcto para "errores 5xx" y malo si queremos distinguirlos —
  la distinción vive en `status` del evento, no en la huella.
- Colisión: `sha256` truncado a 64 bits. Con ~10^6 eventos, la probabilidad de
  que dos mensajes distintos colisionen es despreciable (~10^-11); se acepta por
  el coste de guardar 64 hex en la tabla.
- **El caso no se normaliza**: `"Connection refused"` y `"connection refused"`
  son dos huellas. Es conservador a propósito (un identificador puede depender
  del caso) y el efecto visible es un grupo duplicado, nunca un grupo falso.
- `normalize_message` no está en la base: si mañana cambian las reglas de
  normalización, las huellas viejas no coinciden con las nuevas. El backfill
  `--recalc` es la herramienta, y es explícito.
- Escalar columnas nuevas sobre `events` es una migración `ALTER TABLE ADD
  COLUMN` + índice. `SCHEMA_VERSION` sube a 3; una base v2 existente la aplica
  sin tocar los eventos.

## Detalles que resultaron no obvios al implementar

**La primera línea del generador de demo no se puede elegir al azar.** Una línea
sin patrón justo detrás de un stack trace se toma como *continuación* de esa
entrada multilínea y acaba dentro del evento anterior: sigue sin parsearse, pero
`parsed_ok` sale `1` y la cobertura sale falsa (100% en un dataset con una línea
basura). El generador antepone un registro JSON a propósito para que la línea
suelta tenga su propia entrada. No es un bug de `assemble()`: es la regla de
Fase 2, y aquí se hacía visible porque la cobertura era el número que se miraba.

**`reintento 2 de 5` y `reintento 3 de 5` caen en la misma huella.** Es lo
buscado, pero no se ve hasta que el test de `top_errors` espera 1 fila y salen 2.

**El orden de las reglas es el algoritmo.** No hay "regla más específica gana":
gana la primera que coincide, en el orden del YAML. Una regla genérica arriba
se come a las específicas de abajo, y el síntoma es una categoría rara, no un
error.

## Evidencia

- `tests/test_analysis.py` — 40 tests: normalización (orden, hex, IP, números
  sin `\b`, caso), **100 accesos fallidos → 1 huella**, carga/validación de
  reglas (12 casos: 8 de fallo → `ConfigError`, 4 de clasificación),
  `errors_by_hour` contra un conteo **escrito a mano** sobre
  `tests/fixtures/analysis/errors_by_hour.log`, anomalías (pico detectado con
  valor esperado calculado a mano, serie plana, serie corta, `min_samples`),
  recurrencia por ventana, backfill y recalc.
- Verificación E2E con dataset sintético: 542 eventos, 541 parseados, 28
  fingerprints, idempotente en 2ª pasada.

## Deuda consciente

- Reglas y normalización están en código/archivos separados pero se aplican en
  el mismo sitio; si mañana una regla necesita el mensaje *ya* normalizado habrá
  que decidir en qué orden se aplica cada cosa.