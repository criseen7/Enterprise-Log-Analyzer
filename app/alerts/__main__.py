"""CLI de alertas: `python -m app.alerts [opciones]`

Contraparte de `python -m app.analysis` para incidentes. Sin este punto de
entrada, evaluar la ventana y exportar incidentes obligaría a abrir el
dashboard; un operador que audita por SSH no lo tiene.

    python3 -m app.alerts                    # evalúa y muestra el resumen
    python3 -m app.alerts --list             # sólo lista los incidentes
    python3 -m app.alerts --status OPEN      # filtra por estado
    python3 -m app.alerts --export csv       # vuelca a stdout
    python3 -m app.alerts --window 60        # ventana distinta
    python3 -m app.alerts --no-evaluate      # no escribe, sólo lee
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.alerts.detection import evaluate_incidents
from app.alerts.incidents import status_label, list_incidents
from app.config import load_settings
from app.db.connection import connect
from app.db.migrations import migrate
from app.parser.severity import severity_name
from app.reports import export_incidents_csv, export_incidents_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.alerts", description="Detección y gestión de incidentes."
    )
    parser.add_argument("--list", action="store_true", help="listar incidentes")
    parser.add_argument(
        "--status",
        choices=["OPEN", "ACKNOWLEDGED", "RESOLVED"],
        default=None,
        help="filtrar el listado por estado",
    )
    parser.add_argument(
        "--no-evaluate",
        action="store_true",
        help="no recalcular la ventana; sólo leer lo ya persistido",
    )
    parser.add_argument("--window", type=int, default=None, metavar="MIN", help="ventana en minutos")
    parser.add_argument(
        "--export",
        choices=["csv", "html"],
        default=None,
        help="escribir los incidentes en stdout y salir",
    )
    parser.add_argument("--config", type=Path, default=None, help="ruta a config.yaml")
    return parser


def _print_incidents(rows) -> None:
    if not rows:
        print("sin incidentes")
        return
    print(f"{'id':>4}  {'sev':<6} {'estado':<13} {'n':>6}  mensaje")
    for row in rows:
        level = severity_name(row["severity"])
        message = (row["sample_message"] or "").replace("\n", " ")[:60]
        print(
            f"{row['id']:>4}  {level:<6} {status_label(row['status']):<13} "
            f"{row['event_count']:>6}  {message}"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_settings(config_path=args.config)

    migrate(config.paths.db_path)
    conn = connect(config.paths.db_path)
    try:
        if not args.no_evaluate:
            result = evaluate_incidents(conn, window_minutes=args.window)
            print(
                f"evaluados={result['evaluated']} actualizados={result['upserted']} "
                f"ventana={result['window_minutes']}min "
                f"umbral={result['incident_threshold']}"
            )

        if args.export == "csv":
            sys.stdout.write(export_incidents_csv(conn).decode("utf-8-sig"))
            return 0
        if args.export == "html":
            sys.stdout.write(export_incidents_html(conn))
            return 0

        if args.list or args.no_evaluate:
            _print_incidents(list_incidents(conn, status=args.status))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
