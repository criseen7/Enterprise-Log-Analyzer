"""Traza de una corrida: JSONL para el detalle, `agent_runs` para listar.

La división no es arbitraria:

* El **JSONL** guarda todo lo que pasó, en orden, con el contenido completo de
  cada etapa. Es el artefacto que se publica en `docs/traces/` y el que permite
  reconstruir por qué el agente dijo lo que dijo.
* La **tabla** guarda lo que el dashboard necesita agregar sin abrir archivos:
  estado, coste, tokens, veredicto. Cargar un JSONL por corrida para pintar una
  lista sería convertir una consulta en un recorrido de disco.

La traza se escribe desde el primer momento (`run.start`) y se cierra siempre,
también cuando la corrida aborta: una corrida bloqueada que no deja rastro es
indistinguible de una que nunca ocurrió.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.contracts import AgentRun
from app.mcp._context import write_connection
from app.timeutil import to_canonical

DEFAULT_TRACES_DIR = Path("traces")


def traces_dir() -> Path:
    """Directorio de trazas: `LOGLENS_TRACES_DIR` o `traces/` en el cwd."""
    return Path(os.environ.get("LOGLENS_TRACES_DIR", DEFAULT_TRACES_DIR))


class TraceRecorder:
    """Escribe una línea JSON por evento. Se puede usar como context manager."""

    def __init__(self, run_id: str, directory: str | Path | None = None) -> None:
        self.run_id = run_id
        self.directory = Path(directory) if directory is not None else traces_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{run_id}.jsonl"
        self._handle = self.path.open("w", encoding="utf-8")

    def record(self, kind: str, **payload: Any) -> None:
        line = {
            "ts": to_canonical(datetime.now(UTC)),
            "kind": kind,
            **payload,
        }
        self._handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> TraceRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def record_run(run: AgentRun) -> None:
    """Inserta o actualiza la fila de resumen de la corrida."""
    if run.plan is None:
        plan_json = None
    else:
        plan_json = json.dumps(run.plan.model_dump(), ensure_ascii=False)
    with write_connection() as conn:
        conn.execute(
            """
            INSERT INTO agent_runs (
                run_id, symptom, status, started_at, finished_at, model,
                prompt_version, plan_json, root_cause_fingerprint, confidence,
                step_count, evidence_count, tokens_in, tokens_out, cost_usd, trace_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,
                finished_at=excluded.finished_at,
                root_cause_fingerprint=excluded.root_cause_fingerprint,
                confidence=excluded.confidence,
                step_count=excluded.step_count,
                evidence_count=excluded.evidence_count,
                tokens_in=excluded.tokens_in,
                tokens_out=excluded.tokens_out,
                cost_usd=excluded.cost_usd,
                trace_path=excluded.trace_path
            """,
            (
                run.run_id,
                run.symptom,
                run.status,
                to_canonical(datetime.now(UTC)),
                to_canonical(datetime.now(UTC)) if run.status != "RUNNING" else None,
                run.model,
                run.prompt_version,
                plan_json,
                run.diagnosis.root_cause_fingerprint if run.diagnosis else None,
                run.diagnosis.confidence if run.diagnosis else None,
                len(run.plan.steps) if run.plan else 0,
                len(run.evidence.items),
                run.tokens_in,
                run.tokens_out,
                run.cost_usd,
                run.trace_path,
            ),
        )


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Últimas corridas, de la más reciente a la más antigua."""
    with write_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = ["DEFAULT_TRACES_DIR", "TraceRecorder", "list_runs", "record_run", "traces_dir"]
