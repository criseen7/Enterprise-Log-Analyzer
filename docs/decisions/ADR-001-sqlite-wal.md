# ADR-001 — SQLite en modo WAL con un solo escritor

**Estado:** aceptada
**Fecha:** 2026-10-05
**Fase:** 0 (Cimientos)
**Relacionada con:** D1 en [docs/PLAN.md](../PLAN.md)

---

## Contexto

El sistema tiene dos consumidores naturalmente simultáneos de la misma base:

1. **El ingestor** (`watcher`) escribe eventos de forma continua.
2. **El dashboard** (Streamlit) lee y agrega de forma interactiva.

Con el modo por defecto de SQLite (`journal_mode=DELETE`), toda escritura
requiere un lock **EXCLUSIVE**, que es incompatible con cualquier lector.
El resultado es `database is locked` de forma intermitente y difícil de
reproducir: el dashboard funciona hasta que el watcher escribe.

Además, en la Fase 6 el diseño tiene **dos procesos** distintos accessing al
mismo archivo de base: el contenedor `analyzer` y el contenedor `dashboard`.

## Decisión

**Migrar la base a `journal_mode=WAL` y aceptar explícitamente el modelo
"un solo escritor, N lectores".**

Implicaciones aceptadas:

- WAL permite N lectores concurrentes con 1 escritor.
- WAL **no** permite 2 escritores. Por eso el watcher es el único que escribe,
  y por eso la Fase 6 separa `analyzer` y `dashboard` en procesos distintos.
- `busy_timeout` y `synchronous` se fijan **en cada conexión** (`journal_mode`
  es persistente en la cabecera del archivo; los otros dos no lo son).
- `PRAGMA query_only=ON` se usa para el modo "solo lectura" en lugar de
  `mode=ro` de URI: una conexión `mode=ro` sobre una DB en WAL necesita
  escribir en el archivo `-shm` y falla aunque no se pretenda escribir.

## Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| `journal_mode=DELETE` (default) | Bloqueo lector/escritor. Es el problema que estamos resolviendo. |
| `journal_mode=TRUNCATE` / `PERSIST` | Mismo modelo de bloqueo; solo cambia cómo se recicla el journal. |
| **PostgreSQL** | Correcto y escalable, pero implica operar un servicio. Desproporcionado para el alcance y para el perfil semi-empresarial del proyecto. Se deja como opción de salida documentada si el proyecto crece. |
| Mantener SQLite en un directorio montado (NFS/SMB) | WAL **no funciona** sobre la mayoría de filesystems de red porque requiere locks POSIX fiables. En Docker, `data/` debe ser un volumen local, no un bind mount a un share de red. |
| Escrituras serializadas en un solo proceso (cola) | Añade un bus de mensajes para un problema que WAL ya resuelve. |

## Consecuencias

**Positivas**
- Lectores concurrentes sin bloqueo. Un `EXPLAIN` de agregados del dashboard
  no detiene la ingesta.
- Los `SELECT` nunca ven lecturas sucias: ven el estado del último COMMIT.
- Recuperación tras crash más rápida que con el journal de rollback.

**Negativas / costes**
- El archivo `-wal` crece si nadie hace checkpoint. Mitigación:
  `journal_mode` se revisa periódicamente y el tamaño total (incluyendo
  `-wal` y `-shm`) se mide con `db_size_bytes()`.
- WAL no funciona sobre filesystems de red. Restricción real de despliegue.
- Sigue habiendo un solo escritor. Si en el futuro hubiera varias fuentes
  escribiendo, esta decisión se revisa (PostgreSQL).

## Evidencia

El comportamiento que justifica la decisión está verificado en dos tests, no
solo asumido:

- `tests/test_connection.py::test_open_reader_transaction_does_not_block_writer_commit`
  comprueba que un lector con transacción abierta **no** bloquea el `COMMIT`
  del escritor (debe completarse en <0.4s).

- `tests/test_connection.py::test_delete_journal_would_block_the_writer` es la
  contraprueba: sobre una base cruda en `journal_mode=DELETE`, ese mismo
  escenario **sí** falla con `database is locked`. Si alguien "optimiza" la
  conexión y quita WAL, el segundo test seguiría pasando mientras que el
  primero dejaría de ser cierto — por eso existen los dos.

`tests/test_connection.py::test_two_writers_and_one_reader` cubre el escenario
mixto (2 escritores + 1 lector) que cubre el resto de la aplicación.

## Nota sobre un error frecuente

Un test de estrés "muchos escritores y lectores" **no** es evidencia suficiente
de WAL: en la práctica, con escrituras cortas, SQLite reintenta y el sistema
aguanta incluso en modo `DELETE`. El síntoma aparece solo bajo carga real.
Por eso la verificación se apoya en el escenario determinista del lector
abierto, no en el test de estrés.