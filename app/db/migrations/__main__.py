"""CLI de migraciones. Uso: `python -m app.db.migrations [--status]`"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import get_settings
from app.db.connection import connect, journal_mode
from app.db.migrations import applied_versions, migrate, status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="app.db.migrations", description="Migraciones de la base de datos"
    )
    parser.add_argument("--status", action="store_true", help="Muestra el estado sin aplicar nada")
    parser.add_argument(
        "--db-path", type=Path, default=None, help="Sobrescribe db.path de la config"
    )
    args = parser.parse_args(argv)

    db_path = args.db_path or get_settings().paths.db_path
    exists = Path(db_path).exists()

    if args.status:
        current, target = status(db_path)
        print(f"base de datos : {db_path}")
        print(f"existe        : {exists}")
        print(f"versión       : {current} (objetivo {target})")
        if exists:
            conn = connect(db_path)
            try:
                print(f"aplicadas     : {applied_versions(conn) or 'ninguna'}")
            finally:
                conn.close()
            print(f"journal_mode  : {journal_mode(db_path)}")
        return 0 if current == target else 1

    applied = migrate(db_path)
    if applied:
        print(f"migraciones aplicadas a {db_path}: {applied}")
    else:
        print(f"{db_path} ya estaba en v{status(db_path)[0]}, nada que hacer")
    print(f"journal_mode  : {journal_mode(db_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
