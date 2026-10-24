"""CLI del harness: `python -m evals.run --subset smoke`.

Salidas:
    evals/results/<tag>.json   datos crudos, un objeto por caso
    evals/results/<tag>.md     tabla legible
    docs/evals/RESULTS.md      se actualiza a mano con los números reales

Sin `ANTHROPIC_API_KEY`, una corrida que necesita modelo termina en `SKIPPED`
con código 0 y un mensaje explícito. Nunca en verde falso: un skip se imprime
como skip y no escribe resultados.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.llm import LLMUnavailable, build_client
from evals.cache import DEFAULT_CACHE_DIR, CachedClient
from evals.dataset.build import DEFAULT_PATH as CASES_PATH
from evals.dataset.build import dataset_hash, load_cases, subset_cases
from evals.metrics import (
    aggregate,
    check_thresholds,
    determinism_regression,
    format_table,
)
from evals.runner import HarnessError, InvalidGroundTruth, run_case

RESULTS_DIR = Path("evals/results")

ABLATIONS: dict[str, dict[str, Any]] = {
    "A0": {
        "plan": "heuristic",
        "drop_tools": frozenset(),
        "enforce_citations": True,
        "needs_model": False,
        "note": "plan heurístico y síntesis determinista: el motor solo",
    },
    "A1": {
        "plan": "llm",
        "drop_tools": frozenset({"compare_windows"}),
        "enforce_citations": True,
        "needs_model": True,
        "note": "sin compare_windows",
    },
    "A2": {
        "plan": "llm",
        "drop_tools": frozenset(),
        "enforce_citations": False,
        "needs_model": True,
        "note": "sin validación mecánica de citas",
    },
    "A3": {
        "plan": "llm",
        "drop_tools": frozenset(),
        "enforce_citations": True,
        "needs_model": True,
        "note": "prompt alternativo",
    },
}


def _parse_fail_under(pairs: list[str]) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for pair in pairs:
        name, _, raw = pair.partition("=")
        if not name or not raw:
            raise SystemExit(f"--fail-under espera metrica=valor, no {pair!r}")
        thresholds[name] = float(raw)
    return thresholds


def _make_client(args: argparse.Namespace) -> tuple[Any, str]:
    """Devuelve `(cliente, estado)`; estado `"heuristic"` o `"llm"`."""
    if args.plan == "heuristic":
        return None, "heuristic"
    try:
        client = build_client(args.model)
    except LLMUnavailable as exc:
        print(f"SKIPPED: {exc}", file=sys.stderr)
        print(
            "SKIPPED: el modo con modelo necesita ANTHROPIC_API_KEY. "
            "El harness no marca como aprobada una corrida que no ocurrió.",
            file=sys.stderr,
        )
        raise SystemExit(0) from exc
    if args.no_cache:
        return client, "llm"
    return CachedClient(client, args.cache_dir), "llm"


def _write_results(tag: str, payload: dict[str, Any], results_dir: Path) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    json_path = results_dir / f"{tag}.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    metrics = payload["metrics"]
    front = [
        f"# {tag}",
        "",
        f"- dataset: `{payload['dataset']}` (hash `{payload['dataset_hash']}`)",
        f"- subset: `{payload['subset']}` ({payload['cases']} casos)",
        f"- plan: `{payload['plan']}` · modelo: `{payload['model']}`",
        f"- ablación: `{payload['ablation'] or '—'}` · prompt: `{payload['prompt']}`",
        f"- fecha: {payload['started_at']}",
        "",
        format_table(metrics),
        "",
    ]
    md_path = results_dir / f"{tag}.md"
    md_path.write_text("\n".join(front), encoding="utf-8")
    return json_path, md_path


def run(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    cases = subset_cases(cases, args.subset)
    if args.limit:
        cases = cases[: args.limit]

    ablation = ABLATIONS.get(args.ablation or "")
    if args.ablation and ablation is None:
        print(f"ablación desconocida: {args.ablation}", file=sys.stderr)
        return 2

    plan = args.plan
    drop: frozenset[str] = frozenset()
    enforce = True
    if ablation:
        if ablation["plan"] == "heuristic":
            plan = "heuristic"
        # `--plan heuristic` explícito se respeta: permite medir una ablación
        # con el sintetizador determinista, que es el único modo reproducible
        # sin clave de API.
        drop = ablation["drop_tools"]
        enforce = ablation["enforce_citations"]
    args.plan = plan

    client, mode = _make_client(args)
    tag = args.tag or f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{args.ablation or mode}"
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    model = args.model if mode == "llm" else "none"

    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case['id']} ... ", end="", flush=True)
        try:
            result = run_case(
                case,
                llm=client,
                model=model,
                drop_tools=drop,
                enforce_citations=enforce,
                work_dir=args.work_dir,
                traces_dir=args.traces_dir,
            )
        except InvalidGroundTruth as exc:
            print("INVALID_GROUND_TRUTH")
            print(f"{exc}", file=sys.stderr)
            return 2
        except HarnessError as exc:
            print("HARNESS_ERROR")
            print(f"{exc}", file=sys.stderr)
            return 2
        results.append(result)
        diagnosis = result.get("diagnosis") or {}
        hit = diagnosis.get("root_cause_fingerprint") == result["root_cause_fingerprint"]
        print(f"{result['status']} {'✓' if hit else '✗'}")

    metrics = aggregate(results)
    payload = {
        "tag": tag,
        "started_at": started_at,
        "duration_seconds": round(time.perf_counter() - started, 1),
        "dataset": str(args.cases),
        "dataset_hash": dataset_hash(args.cases.read_text(encoding="utf-8")),
        "subset": args.subset,
        "plan": mode,
        "model": model,
        "ablation": args.ablation,
        "prompt": args.prompt,
        "citations_enforced": enforce,
        "cases": len(results),
        "metrics": metrics,
        "results": results,
    }
    if isinstance(client, CachedClient):
        payload["cache"] = {"hits": client.hits, "misses": client.misses}

    json_path, md_path = _write_results(tag, payload, args.results_dir)
    print()
    print(format_table(metrics))
    print()
    print(f"casos: {metrics['cases']} · coste total: ${sum(r['cost_usd'] for r in results):.4f}")
    print(f"resultados: {json_path} · {md_path}")

    failed = check_thresholds(metrics, _parse_fail_under(args.fail_under))
    if failed:
        print()
        for line in failed:
            print(f"UMBRAL NO CUMPLIDO: {line}", file=sys.stderr)
        return 1
    return 0


def compare(args: argparse.Namespace) -> int:
    """A4: la diferencia de `retrieval_recall@5` entre dos corridas."""
    first = json.loads((args.results_dir / f"{args.compare[0]}.json").read_text(encoding="utf-8"))
    second = json.loads((args.results_dir / f"{args.compare[1]}.json").read_text(encoding="utf-8"))
    report = determinism_regression(first["metrics"], second["metrics"])
    print("| Corrida | retrieval_recall@5 |")
    print("|---|---|")
    print(f"| {args.compare[0]} | {report['reference']} |")
    print(f"| {args.compare[1]} | {report['other']} |")
    print()
    print("idéntica" if report["identical"] else f"REGRESIÓN: delta {report['delta']:+.4f}")
    return 0 if report["identical"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harness de evaluación de LogLens.")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--subset", default="smoke", choices=["smoke", "core", "full"])
    parser.add_argument("--model", default="claude-sonnet-4-5")
    parser.add_argument(
        "--plan",
        default="llm",
        choices=["llm", "heuristic"],
        help="heuristic = sin LLM (ablación A0).",
    )
    parser.add_argument("--prompt", default="planner.v1")
    parser.add_argument("--tag", default=None)
    parser.add_argument("--ablation", default=None, choices=sorted(ABLATIONS))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--work-dir", type=Path, default=Path("evals/.work"))
    parser.add_argument("--traces-dir", type=Path, default=Path("evals/.work/traces"))
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--fail-under",
        action="append",
        default=[],
        metavar="METRICA=VALOR",
        help="Falla si la métrica no cumple el umbral (repetible).",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("REF", "OTRA"),
        default=None,
        help="Compara retrieval_recall@5 entre dos tags (ablación A4).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.compare:
        return compare(args)
    if args.prompt != "planner.v1":
        print(
            f"SKIPPED: el prompt {args.prompt!r} no está versionado; "
            "sólo existe planner.v1. Añadirlo es parte de la ablación A3.",
            file=sys.stderr,
        )
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
