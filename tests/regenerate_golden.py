"""Regenera los golden files a partir de los fixtures.

    python -m tests.regenerate_golden           # solo si cambia la salida
    python -m tests.regenerate_golden --check   # falla si están desactualizados

El flujo correcto es: cambiar el parser o un patrón, ejecutar esto, y revisar el
diff del JSONL línea por línea. Un golden file que se regenera sin mirar es
equivalente a no tener golden file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.parser.registry import Registry
from tests.helpers import PATTERNS_DIR

TESTS_DIR = Path(__file__).parent
FIXTURES = TESTS_DIR / "fixtures"
EXPECTED = TESTS_DIR / "expected"

PAIRS = [
    ("apachev2/access_combined.log", "apachev2/access_combined.jsonl"),
    ("apachev2/access_mixed_timezones.log", "apachev2/access_mixed_timezones.jsonl"),
    ("json/appliance.json", "json/appliance.jsonl"),
    ("json/weblogic_flat.json", "json/weblogic_flat.jsonl"),
]

# Importados desde los tests para que el formato no pueda divergir del que
# compara: si el test serializa de una forma y el regenerador de otra, el
# `--check` de CI fallaría siempre.
from tests.analysis_golden import render_blast, render_compare  # noqa: E402
from tests.test_parser import as_golden  # noqa: E402

# Golden del motor extendido: no son JSONL de parser sino salidas completas de
# una función, así que se comparan como un todo.
ANALYSIS = [
    ("compare_windows.json", render_compare),
    ("blast_radius.json", render_blast),
]


def render(fixture: Path, registry: Registry) -> str:
    lines = [line for line in fixture.read_text(encoding="utf-8").splitlines() if line.strip()]
    return "".join(
        json.dumps(as_golden(registry.parse_line(line)), ensure_ascii=False) + "\n"
        for line in lines
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.regenerate_golden")
    parser.add_argument(
        "--check", action="store_true", help="No escribe: sale con 1 si hay diferencias"
    )
    args = parser.parse_args(argv)

    registry = Registry.from_directory(PATTERNS_DIR)
    stale: list[str] = []

    jobs: list[tuple[Path, str]] = []
    for fixture_rel, expected_rel in PAIRS:
        jobs.append((EXPECTED / expected_rel, render(FIXTURES / fixture_rel, registry)))
    for name, renderer in ANALYSIS:
        jobs.append((EXPECTED / "analysis" / name, renderer()))

    for target, rendered in jobs:
        label = target.relative_to(EXPECTED).as_posix()

        if args.check:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            if current != rendered:
                stale.append(label)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        changed = (not target.is_file()) or target.read_text(encoding="utf-8") != rendered
        target.write_text(rendered, encoding="utf-8")
        print(f"{'actualizado' if changed else 'sin cambios'}: {label}")

    if stale:
        print("\ngolden files desactualizados:", file=sys.stderr)
        for name in stale:
            print(f"  - {name}", file=sys.stderr)
        print("\nRevisa el diff y ejecuta: python -m tests.regenerate_golden", file=sys.stderr)
        return 1
    if args.check:
        print("golden files al día")
    return 0


if __name__ == "__main__":
    sys.exit(main())
