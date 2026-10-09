"""CLI del análisis: `python -m app.analysis [opciones]`

Es la contraparte de `python -m app.ingest`: la ingesta escribe, este módulo
lee y clasifica. Se puede usar a mano para revisar números sin abrir el
dashboard, y es lo que hace un operador cuando el dashboard no cuadra.

    python3 -m app.analysis                       # resumen
    python3 -m app.analysis --backfill            # huellas que faltan
    python3 -m app.analysis --backfill --recalc   # reclasificar todo (reescribe)
    python3 -m app.analysis --top 10 --hourly     # errores agrupados y por hora
    python3 -m app.analysis --anomalies           # picos horarios
    python3 -m app.analysis --patterns --window 60
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.analysis.anomalies import anomalies
from app.analysis.backfill import backfill
from app.analysis.errors import ErrorRules
from app.analysis.metrics import errors_by_hour, overview, top_errors
from app.analysis.patterns import recurring_patterns
from app.config import load_settings
from app.db.connection import connect
from app.db.migrations import migrate
from app.parser.severity import SEVERITY_NAMES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.analysis", description="Métricas y clasificación de eventos."
    )
    parser.add_argument("--backfill", action="store_true", help="rellenar huellas que faltan")
    parser.add_argument(
        "--recalc",
        action="store_true",
        help="con --backfill: reclasificar todos los eventos (reescribe el histórico)",
    )
    parser.add_argument("--overview", action="store_true", help="totales del dataset")
    parser.add_argument("--top", type=int, default=0, metavar="N", help="N errores más repetidos")
    parser.add_argument("--hourly", action="store_true", help="errores por hora")
    parser.add_argument("--anomalies", action="store_true", help="horas anómalas (z-score)")
    parser.add_argument("--patterns", action="store_true", help="patrones de la ventana")
    parser.add_argument("--window", type=int, default=15, metavar="MIN", help="ventana en minutos")
    parser.add_argument("--config", type=Path, default=None, help="ruta a config.yaml")
    return parser


def _print_overview(conn) -> None:
    stats = overview(conn)
    print(f"eventos        : {stats.events}")
    print(f"parseados      : {stats.parsed} ({stats.coverage:.1%})")
    print(f"sin parsear    : {stats.unparsed}")
    print(f"errores (>=45) : {stats.errors}")
    print(f"fingerprints   : {stats.fingerprints}")
    if stats.first_ts:
        print(f"rango          : {stats.first_ts} .. {stats.last_ts}")


def _print_top(conn, limit: int) -> None:
    rows = top_errors(conn, limit=limit)
    if not rows:
        print("sin errores que mostrar")
        return
    print(f"{'n':>6}  {'categoría':<18} {'sev':<6} mensaje")
    for row in rows:
        level = SEVERITY_NAMES.get(row.severity or 0, "UNKNOWN")
        message = (row.sample_message or "").replace("\n", " ")[:70]
        print(f"{row.count:>6}  {row.category or '-':<18} {level:<6} {message}")


def _print_hourly(conn) -> None:
    counts = errors_by_hour(conn)
    if not counts:
        print("sin errores por hora")
        return
    top = max(counts.values())
    for bucket, n in counts.items():
        bar = "#" * max(1, round(n * 40 / top))
        print(f"{bucket}  {n:>5}  {bar}")


def _print_anomalies(conn, config) -> None:
    found = anomalies(
        conn,
        z_threshold=config.analysis.anomaly_zscore,
        min_samples=config.analysis.anomaly_min_samples,
    )
    if not found:
        print(f"sin picos: z >= {config.analysis.anomaly_zscore} con >= "
              f"{config.analysis.anomaly_min_samples} eventos/hora")
        return
    for item in found:
        z = "inf" if item.is_infinite else f"{item.zscore:.2f}"
        print(
            f"{item.bucket}  {item.count} eventos  z={z}  "
            f"(media {item.mean:.1f}, desv {item.stdev:.2f})"
        )


def _print_patterns(conn, config, window: int) -> None:
    found = recurring_patterns(conn, window_minutes=window, min_count=2, min_severity=40)
    if not found:
        print(f"sin recurrencias en los últimos {window} minutos")
        return
    for item in found:
        print(
            f"{item.count:>5}  {item.share:>5.1%}  {item.category or '-':<18} "
            f"{(item.sample_message or '').replace(chr(10), ' ')[:60]}"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_settings(config_path=args.config)

    migrate(config.paths.db_path)
    conn = connect(config.paths.db_path)
    try:
        rules = ErrorRules.load(config.analysis.rules_path)

        if args.backfill:
            touched = backfill(conn, rules, recalc=args.recalc)
            modo = "reclasificados" if args.recalc else "rellenados"
            print(f"eventos {modo}: {touched}")
            if not args.overview and not args.top and not args.hourly and not args.anomalies and not args.patterns:
                return 0

        if not any([args.overview, args.top, args.hourly, args.anomalies, args.patterns]):
            _print_overview(conn)
            return 0

        if args.overview:
            _print_overview(conn)
        if args.top:
            _print_top(conn, args.top)
        if args.hourly:
            _print_hourly(conn)
        if args.anomalies:
            _print_anomalies(conn, config)
        if args.patterns:
            _print_patterns(conn, config, args.window)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
