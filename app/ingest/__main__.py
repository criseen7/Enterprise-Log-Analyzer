"""CLI de la Fase 2: `python -m app.ingest [--once] [--watch]`

Por defecto hace una pasada y termina, que es el comportamiento útil para
scripts y para CI. `--watch` deja el proceso vivo y delega en `watcher`.

    python3 tools/generate_sample_logs.py
    python3 -m app.ingest                 # una pasada
    python3 -m app.ingest --watch         # sigue los cambios
    python3 -m app.ingest --status        # qué hay en la DB, sin ingerir
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.analysis.errors import ErrorRules
from app.config import load_settings
from app.db.connection import connect
from app.db.migrations import migrate
from app.db.repository import parse_coverage, recent_runs
from app.ingest.ingestor import ingest_paths_transactional, summarize
from app.parser.registry import Registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest", description="Ingesta logs en la base SQLite."
    )
    parser.add_argument("--once", action="store_true", help="una pasada y salir (por defecto)")
    parser.add_argument("--watch", action="store_true", help="vigilar cambios y ingerir")
    parser.add_argument("--status", action="store_true", help="mostrar estado y salir")
    parser.add_argument("--config", type=Path, default=None, help="ruta a config.yaml")
    return parser


def show_status(conn) -> int:
    total, ok = parse_coverage(conn)
    print(f"eventos        : {total}")
    print(f"parseados      : {ok}")
    if total:
        print(f"cobertura      : {ok / total:.1%}")
    runs = recent_runs(conn, limit=5)
    if not runs:
        print("corridas       : ninguna todavía")
    else:
        print("últimas corridas:")
        for r in runs:
            print(
                f"  #{r['id']} {r['finished_at'] or r['started_at']} "
                f"{r['status']:<5} +{r['events_inserted']} "
                f"omitidos={r['events_skipped']} sin_parsear={r['events_unparsed']} "
                f"{r['duration_ms']}ms"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_settings(config_path=args.config)

    migrate(config.paths.db_path)
    conn = connect(config.paths.db_path)
    try:
        if args.status:
            return show_status(conn)

        registry = Registry.from_directory(config.paths.patterns_dir)
        # Carga aquí (no dentro del bucle): un YAML de reglas mal formado debe
        # romper al arrancar, con la regla culpable en el mensaje.
        rules = ErrorRules.load(config.analysis.rules_path)
        if args.watch:
            from app.ingest.watcher import watch

            print(f"vigilando {config.paths.log_dir} (Ctrl-C para salir)")
            cycles = watch(config, conn, registry, on_cycle=lambda n: print(f"  corrida {n}"))
            print(f"terminado: {cycles} corridas")
            return 0

        from app.ingest.ingestor import discover

        rutas = [p for p in discover(config) if p.is_file()]
        if not rutas:
            print(f"no hay archivos en {config.paths.log_dir}")
            print("genera unos con: python3 tools/generate_sample_logs.py")
            return 0

        run = ingest_paths_transactional(conn, registry, rutas, config.ingest.chunk_bytes, rules)
        print(summarize(conn, run))
        if run.errors:
            for error in run.errors:
                print(f"  ! {error}", file=sys.stderr)
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
