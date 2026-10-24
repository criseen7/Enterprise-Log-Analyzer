"""Ejecución de un caso de eval con el motor real.

El harness no reimplementa la ingesta ni las tools: materializa logs, llama a
`ingest_paths_transactional` y después al mismo `investigate` que usa producción.
Si la ingesta deja de ser idempotente o una tool cambia de forma, el eval lo
detecta antes que el usuario.

El ground truth se valida **antes** de puntuar: si la huella inyectada no está
en la base, el caso es inválido y el harness falla. Un 0 de accuracy causado por
un dataset roto es peor que un error ruidoso.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app import config
from app.agent.contracts import AgentRun, CitationReport
from app.agent.llm import LLMClient
from app.agent.loop import investigate
from app.analysis.errors import ErrorRules
from app.db.connection import connect
from app.db.migrations import migrate
from app.ingest.ingestor import ingest_paths_transactional
from app.parser.registry import Registry
from app.timeutil import to_canonical
from evals.dataset.build import load_cases
from evals.dataset.generators import build_scenario, write_logs

WORK_DIR = Path("evals/.work")
TRACES_DIR = Path("evals/.work/traces")


class HarnessError(RuntimeError):
    """El harness no puede puntuar: dataset inválido o ingesta rota."""


class InvalidGroundTruth(HarnessError):
    """La huella del ground truth no aparece en la base ingerida."""


@contextmanager
def _isolated_environment(log_dir: Path, db_path: Path, traces: Path) -> Iterator[None]:
    """Apunta el motor a las rutas del caso y restaura el proceso al salir."""
    keys = ("ELA_LOG_DIR", "ELA_DB_PATH", "LOGLENS_TRACES_DIR")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["ELA_LOG_DIR"] = str(log_dir)
    os.environ["ELA_DB_PATH"] = str(db_path)
    os.environ["LOGLENS_TRACES_DIR"] = str(traces)
    config._cached = None
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._cached = None


def prepare_case(case: dict[str, Any], work_dir: Path = WORK_DIR) -> tuple[Path, Path, Path]:
    """Materializa logs y devuelve `(log_dir, db_path, case_dir)`."""
    params = case["generator_params"]
    scenario = build_scenario(
        case["generator"],
        seed=int(case["seed"]),
        log_format=params["log_format"],
        tz_offset=params["tz_offset"],
    )
    expected = params["onset"].replace("Z", "+00:00")
    if scenario.onset.isoformat() != expected:
        raise HarnessError(
            f"{case['id']}: el onset no es reproducible ({scenario.onset} != {expected})"
        )

    case_dir = Path(work_dir) / case["id"]
    if case_dir.exists():
        shutil.rmtree(case_dir)
    log_dir = case_dir / "logs"
    write_logs(scenario, log_dir)
    return log_dir, case_dir / "data" / "db.sqlite", case_dir


def _retrieval_window(case: dict[str, Any]) -> tuple[str, str]:
    from datetime import timedelta

    from app.timeutil import parse_utc

    onset = parse_utc(case["ground_truth"]["first_bad_event_ts"])
    assert onset is not None
    minutes = int(case["generator_params"]["duration_minutes"])
    since = to_canonical(onset - timedelta(minutes=5))
    until = to_canonical(onset + timedelta(minutes=minutes + 5))
    assert since is not None and until is not None
    return since, until


def _retrieval(case: dict[str, Any]) -> tuple[bool, list[str]]:
    """Top-5 del motor, sin LLM. Es la métrica de la tesis."""
    from app.mcp.tools.events import top_errors

    since, until = _retrieval_window(case)
    result = top_errors(limit=5, since=since, until=until)
    fingerprints = [error.fingerprint for error in result.errors]
    return case["ground_truth"]["root_cause_fingerprint"] in fingerprints, fingerprints


def _citations_payload(run: AgentRun) -> dict[str, list[dict[str, Any]]]:
    report = run.citations or CitationReport()
    return {
        "valid": [claim.model_dump() for claim in report.valid],
        "invalid": [claim.model_dump() for claim in report.invalid],
        "uncited": [claim.model_dump() for claim in report.uncited],
    }


def run_case(
    case: dict[str, Any],
    *,
    llm: LLMClient | None = None,
    model: str = "claude-sonnet-4-5",
    drop_tools: frozenset[str] = frozenset(),
    enforce_citations: bool = True,
    work_dir: Path = WORK_DIR,
    traces_dir: Path = TRACES_DIR,
) -> dict[str, Any]:
    """Ejecuta un caso completo y devuelve un resultado serializable."""
    log_dir, db_path, case_dir = prepare_case(case, work_dir)
    truth = case["ground_truth"]

    with _isolated_environment(log_dir, db_path, traces_dir):
        migrate(db_path)
        settings = config.get_settings()
        registry = Registry.from_directory(
            settings.paths.patterns_dir,
            default_timezone=settings.ingest.default_timezone,
        )
        conn = connect(db_path)
        try:
            ingest = ingest_paths_transactional(
                conn,
                registry,
                sorted(log_dir.glob("*.log")),
                rules=ErrorRules.default(),
            )
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE fingerprint = ?",
                (truth["root_cause_fingerprint"],),
            ).fetchone()
            occurrences = int(row["n"]) if row else 0
        finally:
            conn.close()

        if occurrences == 0:
            raise InvalidGroundTruth(
                f"{case['id']}: la huella {truth['root_cause_fingerprint']} no aparece "
                f"tras ingerir {case_dir} (insertados={ingest.inserted})"
            )

        retrieval_hit, retrieval_top5 = _retrieval(case)
        agent_run = investigate(
            case["symptom"],
            llm=llm,
            model=model,
            trace_dir=traces_dir,
            drop_tools=drop_tools,
            enforce_citations=enforce_citations,
        )

    return {
        "case_id": case["id"],
        "generator": case["generator"],
        "status": agent_run.status,
        "error": agent_run.error,
        "run_id": agent_run.run_id,
        "trace_path": agent_run.trace_path,
        "plan": agent_run.plan.model_dump() if agent_run.plan else None,
        "diagnosis": agent_run.diagnosis.model_dump() if agent_run.diagnosis else None,
        "citations": _citations_payload(agent_run),
        "tool_calls": len(agent_run.evidence.tool_calls),
        "cost_usd": agent_run.cost_usd,
        "duration_ms": agent_run.duration_ms,
        "tokens_in": agent_run.tokens_in,
        "tokens_out": agent_run.tokens_out,
        "prompt_version": agent_run.prompt_version,
        "ground_truth_fingerprint": truth["root_cause_fingerprint"],
        "root_cause_fingerprint": truth["root_cause_fingerprint"],
        "root_cause_category": truth["root_cause_category"],
        "root_cause_service": truth["root_cause_service"],
        "onset": truth["first_bad_event_ts"],
        "must_not_claim": case["assertions"]["must_not_claim"],
        "retrieval_hit": retrieval_hit,
        "retrieval_top5": retrieval_top5,
        "ingest": {
            "inserted": ingest.inserted,
            "unparsed": ingest.unparsed,
            "status": ingest.status,
            "root_cause_occurrences": occurrences,
        },
    }


def load_subset(path: str | Path, subset: str) -> list[dict[str, Any]]:
    from evals.dataset.build import subset_cases

    return subset_cases(load_cases(Path(path)), subset)


__all__ = [
    "HarnessError",
    "InvalidGroundTruth",
    "TRACES_DIR",
    "WORK_DIR",
    "load_subset",
    "prepare_case",
    "run_case",
]
