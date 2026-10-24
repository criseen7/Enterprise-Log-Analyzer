"""Construye `cases.jsonl` a partir de los generadores.

El archivo se commitea: el dataset es un artefacto del repositorio, no algo que
cada corrida inventa. `--verify` comprueba que el archivo versionado coincide
con lo que producen los generadores, así que un cambio accidental en un
parámetro no pasa desapercibido.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from evals.dataset.generators import (
    DATASET_VERSION,
    GENERATORS,
    LOG_FORMATS,
    TZ_OFFSETS,
    build_scenario,
    ground_truth,
    must_not_claim,
    symptom_for,
)

DEFAULT_PATH = Path("evals/dataset/cases.jsonl")
CASES_PER_GENERATOR = 10


def _subset(index: int) -> str:
    if index == 0:
        return "smoke"
    if index < 5:
        return "core"
    return "full"


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    number = 0
    for gen_index, generator in enumerate(GENERATORS):
        for index in range(CASES_PER_GENERATOR):
            number += 1
            seed = 1000 * (gen_index + 1) + index * 17
            scenario = build_scenario(
                generator,
                seed=seed,
                log_format=LOG_FORMATS[(index + gen_index) % len(LOG_FORMATS)],
                tz_offset=TZ_OFFSETS[(index + 2 * gen_index) % len(TZ_OFFSETS)],
            )
            truth = ground_truth(scenario)
            cases.append(
                {
                    "id": f"scn_{number:03d}_{generator}",
                    "version": DATASET_VERSION,
                    "subset": _subset(index),
                    "seed": seed,
                    "generator": generator,
                    "generator_params": {
                        "log_format": scenario.log_format,
                        "tz_offset": scenario.tz_offset,
                        "onset": scenario.onset.isoformat().replace("+00:00", "Z"),
                        "duration_minutes": int(scenario.duration.total_seconds() // 60),
                        "noise_ratio": scenario.noise_ratio,
                    },
                    "symptom": symptom_for(scenario),
                    "ground_truth": truth,
                    "assertions": {
                        "must_cite": [f"fp:{truth['root_cause_fingerprint']}"],
                        "must_not_claim": list(must_not_claim(generator)),
                        "min_confidence": "medium",
                    },
                }
            )
    return cases


def dumps(cases: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(case, ensure_ascii=False, sort_keys=True) for case in cases) + "\n"


def dataset_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_dataset(path: Path = DEFAULT_PATH) -> str:
    text = dumps(build_cases())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return dataset_hash(text)


def load_cases(path: str | Path = DEFAULT_PATH) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def subset_cases(cases: list[dict[str, Any]], subset: str) -> list[dict[str, Any]]:
    """`core` incluye el smoke; `full` incluye todo, como en cualquier suite."""
    if subset == "smoke":
        return [case for case in cases if case["subset"] == "smoke"]
    if subset == "core":
        return [case for case in cases if case["subset"] in {"smoke", "core"}]
    return list(cases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera o verifica el dataset de evals.")
    parser.add_argument("--out", type=Path, default=DEFAULT_PATH)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Falla si el archivo versionado no coincide con los generadores.",
    )
    parser.add_argument("--print-hash", action="store_true")
    args = parser.parse_args(argv)

    if args.verify:
        expected = dumps(build_cases())
        actual = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if actual != expected:
            print(f"{args.out} está desactualizado. Regenera con: python -m evals.dataset.build")
            return 1
        print(f"{args.out} OK ({dataset_hash(expected)})")
        return 0

    digest = write_dataset(args.out)
    if args.print_hash:
        print(digest)
    else:
        print(f"{len(build_cases())} casos escritos en {args.out} (hash {digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
