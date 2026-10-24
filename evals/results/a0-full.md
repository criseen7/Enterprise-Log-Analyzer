# a0-full

- dataset: `evals/dataset/cases.jsonl` (hash `b8324730bcd5305d`)
- subset: `full` (60 casos)
- plan: `heuristic` · modelo: `none`
- ablación: `—` · prompt: `planner.v1`
- fecha: 2026-10-23T17:22:54+00:00

| Métrica | Valor | Meta |
|---|---|---|
| `root_cause_accuracy` | 0.95 | >= 0.8 ✅ |
| `category_accuracy` | 0.95 | >= 0.75 ✅ |
| `evidence_citation_rate` | 1.0 | >= 0.95 ✅ |
| `hallucination_rate` | 0.0 | <= 0.05 ✅ |
| `false_attribution_rate` | 0.0 | <= 0.1 ✅ |
| `tool_call_efficiency` | 5.0 | <= 8.0 ✅ |
| `token_cost_usd` | 0.0 | <= 0.05 ✅ |
| `retrieval_recall_at_5` | 1.0 | >= 0.9 ✅ |
| `misunderstood_rate` | 0.0 | <= 0.1 ✅ |
