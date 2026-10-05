# ADR-002 — Parsers declarativos en YAML, con contrato de grupos con nombre

**Estado:** aceptada
**Fecha:** 2026-10-05
**Fase:** 1 (Parser)
**Relacionada con:** D2 en [docs/PLAN.md](../PLAN.md)

---

## Contexto

El proyecto debe soportar cuatro formatos: Apache CLF, IIS, WebLogic y JSON.
La versión informal del README los trataba como un bloque de funcionalidades
(`Soporte multi-formato: Apache logs, IIS logs, WebLogic logs, JSON logs`), sin
decir cómo se implementaría.

La decisión de cómo implementarlo es la de mayor impacto del proyecto, porque
determina el coste de agregar el cuarto formato y el de testear el sistema
completo cada vez que se toca el parser.

## Decisión

**Cada formato es un archivo YAML en `app/parser/patterns/`.** El código contiene
un solo camino de parseo; agregar IIS o WebLogic es agregar un archivo, sin
escribir Python.

Dos tipos de patrón:

| `kind` | Para qué | Cómo declara los campos |
|---|---|---|
| `regex` | Logs orientados a línea (Apache, IIS, WebLogic) | Un regex con **grupos con nombre** |
| `json` | Logs estructurados | Listas de claves candidatas por campo, con rutas de punto |

### El contrato del regex

Un patrón `regex` **debe** definir el grupo con nombre `ts`. Opcionales:
`level`, `service`, `logger`, `status`, `message`, `client`.

El contrato se verifica **al cargar los patrones**, no durante la ingesta: un
YAML que olvida `ts` rompe el arranque con un mensaje que dice exactamente qué
falta, en lugar de producir una columna de timestamps nulos durante una hora.

### Prioridad y detección

Los patrones se prueban en orden de prioridad descendente y gana el primero que
coincide. Un formato específico (Apache, prioridad 50) se prueba antes que uno
genérico (JSON, prioridad 40).

## Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| Una función `parse_apache()` por formato | Agregar IIS obliga a tocar el parser y a re-testear todo. Es exactamente la deuda que D2 busca evitar. |
| Expresiones regulares gigantes en un solo archivo | Ilegible a los dos meses. Un error de escape se encuentra leyendo 400 líneas de regex en vez de 10. |
| Detección por heurística (`if line.startswith('{')` dentro del código) | La heurística acaba en el código, que es donde no se puede versionar ni probar. |
| Parsear JSON siempre primero, por ser "más específico" | El JSON es un *envoltorio*, no un formato: muchos sistemas lo usan para todo, incluido el CLF serializado. La prioridad la declara cada patrón. |
| `ply` / `pyparsing` (parser generators) | Mucha maquinaria para un problema que son 5 formatos con regex. El coste de aprendizaje no se paga. |

## Consecuencias

**Positivas**
- IIS y WebLogic son un YAML cada uno. Ya se puede estimar el esfuerzo: ~30 líneas.
- Los patrones se testean como datos, no como código: un test puede afirmar que
  los patrones cargados tienen nombres únicos y grupos obligatorios.
- Un formato experimental se prueba en 2 minutos: se suelta un YAML en el
  directorio y se ve si el ratio de parseo sube.
- El golden test compara contra una salida guardada, así que un cambio
  accidental en el parseo aparece como diff en el JSONL.

**Negativas / costes**
- `regex` sigue siendo `regex`: un CLF con formato desconocido sigue sin
  parsear. Declarar la configuración no elimina la complejidad de las
  expresiones regulares, solo la mueve a un archivo declarativo.
- La indirección (YAML → `PatternSpec` → `ParseResult`) hace que el flujo de
  control sea menos obvio de leer que un `if` directo. Se compensa con los
  mensajes de error de carga, que son explícitos.
- Un YAML puede tener un error de configuración que solo se manifiesta con
  datos reales. Los fixtures con formatos reales son parte de la mitigación.

## Detalles que resultaron no obvios al implementar

**1. `re.VERBOSE` es necesario para escribir regex legibles.**
El bloque YAML `regex: |` conserva los saltos de línea, y `re` los interpreta
como literales: un regex multilínea no matchea nada y el fallo es desconcertante.
La opción es declarar `flags: [verbose]` en el patrón.

**2. Los meses de Apache no se pueden parsear con `%b`.**
`strptime` resuelve `%b` con `LC_TIME`. Un contenedor con locale `es_ES.UTF-8`
deja de entender `Oct`, y Apache siempre escribe meses en inglés. La solución es
`english_months: true`, que sustituye el nombre del mes por su número *antes* de
llamar a `strptime`, y declara el formato con `%m`. Así el resultado no depende
del locale del proceso.

**3. `message_template` en vez de un grupo `message` obligatorio.**
CLF no tiene un campo de mensaje: tiene request, status y bytes. Declarar
`message_template: '{method} {path} -> {status}'` construye el mensaje a partir
de los grupos disponibles, y los grupos ausentes se sustituyen por cadena vacía
en vez de romper el formato.

**4. En JSON, un candidato que resuelve a un dict se salta.**
Con `{"log": {"message": "..."}}`, la clave `log` resuelve a un dict. Si se
aceptara el primer candidato presente, el mensaje sería el dict entero
serializado. La resolución salta contenedores y sigue con `log.message`.

**5. La zona por defecto se hereda, no se declara en el patrón.**
Un patrón con `default_timezone` propio sobreescribiría `config.yaml` en
silencio, volvería inútil la config global y haría imposible probar la
timezone con un valor distinto. Ausente significa "hereda del Registry".

**6. Un access log no tiene nivel, y no se inventa uno.**
CLF no escribe nivel: solo el código HTTP. `status_levels` mapea
`{from: 500} → ERROR` y `{from: 400} → WARN`. Un 2xx queda en `UNKNOWN`, que es
lo correcto: no es un error. Sin este mapeo, un access log con 3.000 errores 500
quedaría entero en `UNKNOWN` y el dashboard de severidad no serviría.

## Evidencia

- `tests/test_parser.py::PatternLoadingTests` — los fallos de configuración se
  detectan al cargar: falta `ts`, regex inválida, flag desconocido,
  `message_template` con grupo inexistente, nombres duplicados, zona inválida.
- `tests/test_parser.py::GoldenFileTests` — 4 fixtures, 42 líneas, ratio de
  parseo 100% (umbral exigido: 95%). Cada línea tiene su salida esperada en
  `tests/expected/*.jsonl`.
- `tests/test_parser.py::TimezoneEquivalenceTests` — el mismo instante escrito
  de tres formas distintas produce un único `timestamp_utc`.
- `python -m tests.regenerate_golden --check` falla si los golden están
  desactualizados, para que un cambio de salida no pase inadvertido.

## Deuda consciente

El formato WebLogic **no** está implementado, y su dificultad real no es el
regex: es que las entradas multilínea (stack traces de Java) ocupan N líneas y
un stack trace partido en N eventos es basura. Eso se resuelve en la Fase 2
(ensamblado multilínea), y por eso `PatternSpec` ya tiene un campo `multiline`
reservado. Implementar WebLogic antes de la Fase 2 habría producido un parser
que funciona en las pruebas y corrompe los datos en producción.