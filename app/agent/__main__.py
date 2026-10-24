"""CLI de la capa agentic: `python -m app.agent "síntoma" [opciones]`.

Sin `ANTHROPIC_API_KEY` funciona igual: usa el plan heurístico y el sintetizador
determinista, y lo avisa por stderr. No hay un modo "demo" separado del modo
real; es la misma ruta de código.
"""

from __future__ import annotations

import argparse

from app.agent.cli import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Investiga un síntoma con el motor de LogLens y la capa agentic.",
    )
    parser.add_argument(
        "symptom",
        nargs="+",
        help="Descripción del síntoma, en lenguaje natural.",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-5",
        help="Modelo a usar si hay clave de API (por defecto: %(default)s).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Fuerza el modo determinista aunque haya clave.",
    )
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help="Permite proponer un cambio de estado (siempre queda pendiente de aprobación humana).",
    )
    parser.add_argument(
        "--traces",
        default=None,
        help="Directorio de trazas (por defecto: $LOGLENS_TRACES_DIR o ./traces).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emite el AgentRun completo en JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(
        " ".join(args.symptom),
        model=args.model,
        no_llm=args.no_llm,
        allow_mutations=args.allow_mutations,
        traces=args.traces,
        as_json=args.as_json,
    )


if __name__ == "__main__":
    raise SystemExit(main())
