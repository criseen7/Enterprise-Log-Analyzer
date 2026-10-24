# Resultados de evaluación

> Estado: **parcial y honesto**. Las corridas sin LLM están ejecutadas y sus
> números son reales. Las que necesitan modelo (A3, A4 y la comparación entre
> modelos) **no se han ejecutado**: en el entorno donde se construyó esto no hay
> `ANTHROPIC_API_KEY`. No hay filas estimadas ni "esperadas" en las tablas.

- Dataset: `evals/dataset/cases.jsonl` (`synthetic-v1`, 60 casos, hash `b8324730bcd5305d`)
- Subsets: `smoke` = 6 casos (uno por generador), `core` = 30, `full` = 60
- Generadores: `db_pool_exhaustion`, `bad_deploy`, `disk_full`, `cert_expiry`,
  `traffic_spike`, `cascading_timeout`
- Formatos: JSON, WebLogic y Apache CLF (los tres que soporta el parser)
- Zonas horarias: UTC, `-06:00`, `+02:00`, mezcladas por caso
- Ingesta: la de producción (`ingest_paths_transactional`), sin atajos

Cada caso se materializa en `evals/.work/<id>/`, se ingiere en una SQLite nueva
y el harness verifica que la huella del ground truth exista antes de puntuar. Si
no existe, la corrida termina con `INVALID_GROUND_TRUTH`, no con un 0.

## Corridas ejecutadas

Reproducibles con los comandos indicados. Coste real: **$0.00**.

### A0 — plan heurístico, síntesis determinista, sin LLM

```bash
python -m evals.run --subset full --plan heuristic --tag a0-full
```

| Métrica | Valor | Meta |
|---|---|---|
| `root_cause_accuracy` | 0.95 | ≥ 0.80 ✅ |
| `category_accuracy` | 0.95 | ≥ 0.75 ✅ |
| `evidence_citation_rate` | 1.00 | ≥ 0.95 ✅ |
| `hallucination_rate` | 0.00 | ≤ 0.05 ✅ |
| `false_attribution_rate` | 0.00 | ≤ 0.10 ✅ |
| `tool_call_efficiency` | 5.0 | ≤ 8 ✅ |
| `token_cost_usd` | 0.00 | ≤ 0.05 ✅ |
| `retrieval_recall_at_5` | 1.00 | ≥ 0.90 ✅ |
| `misunderstood_rate` | 0.00 | ≤ 0.10 ✅ |

El único fallo (1 de 60) es `cascading_timeout`: el sintetizador determinista
elige la huella más frecuente de la ventana y, en ese caso, el error de cascada
(`upstream failure: dependency inventory unavailable`) supera en volumen a la
causa (`Read timed out ... calling inventory`). La huella correcta **sí** está en
el top-5 del motor (`retrieval_recall@5 = 1.00`): el motor sabía la respuesta y
el agente eligió el síntoma. Es exactamente la distinción que el harness mide y
la razón de reportar las dos métricas.

### A1 — sin `compare_windows`

```bash
python -m evals.run --subset full --ablation A1 --plan heuristic --tag a1-full
```

| Métrica | A0 | A1 | Δ |
|---|---|---|---|
| `root_cause_accuracy` | 0.95 | **0.00** | −0.95 |
| `category_accuracy` | 0.95 | 0.95 | 0 |
| `retrieval_recall_at_5` | 1.00 | 1.00 | 0 |
| `tool_call_efficiency` | 5.0 | 4.0 | −1.0 |

**Hipótesis del plan:** la accuracy cae ≥ 0.10. **Resultado: cae 0.95.** Sin la
comparación contra el baseline, la síntesis determinista puede nombrar la huella
más frecuente pero no puede afirmar que sea nueva o esté elevada, así que no
declara causa raíz. El resto de métricas no cambia: el motor sigue recuperando
la huella correcta; lo que falta es la aritmética que la convierte en causa.

### A2 — sin validación mecánica de citas

```bash
python -m evals.run --subset full --ablation A2 --plan heuristic --tag a2-full
```

| Métrica | Con validación | Sin validación |
|---|---|---|
| `hallucination_rate` | 0.00 | 0.00 |
| `evidence_citation_rate` | 1.00 | 1.00 |

**Esta ablación no es informativa en modo determinista y se publica igual.** El
sintetizador por reglas sólo cita `evidence_id`s que acaba de leer del bundle, de
modo que no hay citas inventadas que eliminar. La hipótesis del plan
(`hallucination_rate` sube ≥ 3×) sólo se puede probar con un modelo redactando;
sin clave de API, no se ha probado. El flag `enforce_citations=False` ya existe
en el bucle y es el que usará la corrida con modelo.

## Pendiente de ejecutar (requiere `ANTHROPIC_API_KEY`)

| Ablación | Qué mide | Estado |
|---|---|---|
| A3 | `planner.v1` frente a un prompt alternativo | **No ejecutada.** Sólo existe `planner.v1`. |
| A4 | Sonnet frente a Opus: `retrieval_recall@5` idéntica | **No ejecutada.** `python -m evals.run --compare <tag-sonnet> <tag-opus>` ya calcula la tabla cuando existan las corridas. |
| Modelo real | `root_cause_accuracy`, `hallucination_rate`, coste | **No ejecutada.** El runner escribe `SKIPPED` y no resultados cuando falta la clave. |

Ninguna tabla de este documento contiene números de modelo estimados. Cuando se
ejecuten, se añadirán aquí con su tag y su hash de dataset.

## Limitaciones conocidas del dataset

1. **El ground truth de la categoría es la categoría del motor.** Se calcula con
   `error_rules.yaml` sobre el mensaje inyectado, igual que la huella. Comparar
   contra una etiqueta semántica escrita a mano mediría otra cosa y no sería
   reproducible sin el motor. Consecuencia: `category_accuracy` mide si el
   agente **reporta** la categoría del motor, no si la taxonomía es la ideal.
2. **En Apache el mensaje es `GET <path> -> 500`.** El parser no ve la plantilla
   semántica, así que los casos en ese formato tienen categoría `http-5xx`. Es
   fiel al motor y hace que el dataset no dependa de un formato, pero esos casos
   no prueban "causa no obvia".
3. **La causa raíz es el error más frecuente por construcción.** El generador
   inyecta el fallo a una tasa que domina el ruido, así que A0 tiene un techo
   trivial alto. La parte no trivial la miden `retrieval_recall@5`, el caso
   `cascading_timeout` y, sobre todo, las ablaciones con LLM.
4. **`traffic_spike` no rompe al plan heurístico.** Su ground truth es el
   fingerprint del error que ya existía; la trampa (no atribuirlo a una
   regresión de código) se mide con `must_not_claim`/`false_attribution_rate`, y
   el sintetizador determinista no comete esa atribución. El caso está pensado
   para la síntesis con modelo.
5. **Smoke tiene 6 casos, no 5.** El plan decía 5 pero enumeraba 6 escenarios; se
   incluye uno por generador para que ningún mecanismo de fallo quede fuera del
   CI.

## Cómo se comprueba que estos números son de este código

```bash
python -m evals.dataset.build --verify   # el dataset commiteado coincide
python -m unittest tests.test_evals -v   # ground truth, métricas y determinismo
```

Los resultados crudos viven en `evals/results/<tag>.json`; las tablas de este
documento salen de esos archivos, no de una hoja de cálculo paralela.
