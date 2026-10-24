"""Ejecución de los casos golden de `compare_windows` y `blast_radius`.

El golden no compara contra números escritos a mano: compara la salida actual
del motor contra la última salida revisada. Sirve para que un cambio de umbral,
de redondeo o de orden produzca un diff visible en lugar de pasar inadvertido.

Los casos viven en `tests/fixtures/analysis/cases.json` y cada uno tiene su
propio día, para que un caso nuevo no cambie los agregados de los demás (el
`share_of_errors` cuenta todos los errores de la ventana, no sólo los de la
huella).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.analysis.blast import blast_radius
from app.analysis.windows import compare_windows
from app.db.connection import connect
from app.db.migrations import migrate

CASES_PATH = Path(__file__).parent / "fixtures" / "analysis" / "cases.json"
EXPECTED_DIR = Path(__file__).parent / "expected" / "analysis"


def load_cases() -> dict[str, list[dict[str, Any]]]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _insert(conn: sqlite3.Connection, events: list[dict[str, Any]]) -> None:
    timestamps = [event["at"] for event in events] or ["2026-01-01T00:00:00.000Z"]
    file_id = conn.execute(
        "INSERT INTO log_files (path, first_seen, last_seen) VALUES (?, ?, ?)",
        ("golden/app.log", min(timestamps), max(timestamps)),
    ).lastrowid
    for offset, event in enumerate(events):
        conn.execute(
            "INSERT INTO events (file_id, offset, timestamp_utc, severity, message,"
            " raw_line, parsed_ok, service, category, fingerprint)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'golden', ?)",
            (
                file_id,
                offset,
                event["at"],
                event["severity"],
                "golden message",
                event["at"],
                event.get("service"),
                event["fingerprint"],
            ),
        )


def run_compare(case: dict[str, Any]) -> dict[str, Any] | None:
    with tempfile.TemporaryDirectory(prefix="loglens-golden-") as tmp:
        db = Path(tmp) / "golden.db"
        migrate(db)
        conn = connect(db)
        try:
            _insert(conn, case["events"])
            result = compare_windows(conn, **case["args"])
        finally:
            conn.close()
    return asdict(result) if result is not None else None


def run_blast(case: dict[str, Any]) -> dict[str, Any] | None:
    with tempfile.TemporaryDirectory(prefix="loglens-golden-") as tmp:
        db = Path(tmp) / "golden.db"
        migrate(db)
        conn = connect(db)
        try:
            _insert(conn, case["events"])
            result = blast_radius(conn, **case["args"])
        finally:
            conn.close()
    return asdict(result) if result is not None else None


def _render(cases: list[dict[str, Any]], runner: Any) -> str:
    rendered = [
        {"name": case["name"], "result": runner({**case, "events": _tag(case)})} for case in cases
    ]
    return json.dumps(rendered, ensure_ascii=False, indent=2) + "\n"


def _tag(case: dict[str, Any]) -> list[dict[str, Any]]:
    fingerprint = case["args"]["fingerprint"]
    return [{**event, "fingerprint": fingerprint} for event in case["events"]]


def render_compare() -> str:
    return _render(load_cases()["compare"], run_compare)


def render_blast() -> str:
    return _render(load_cases()["blast"], run_blast)


__all__ = [
    "CASES_PATH",
    "EXPECTED_DIR",
    "load_cases",
    "render_blast",
    "render_compare",
    "run_blast",
    "run_compare",
]
