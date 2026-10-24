"""Contratos de la capa agentic.

Todo lo que cruza una frontera —del LLM al bucle, del bucle a la traza, de la
traza al dashboard— está aquí y validado con Pydantic. La razón no es el tipado
por el tipado: un LLM devuelve texto libre, y si el plan no valida antes de
ejecutarse, una alucinación se convierte en una llamada a una tool que no
existe o en una mutación con parámetros inventados.

`EvidenceBundle.by_id` es la pieza que hace mecánica la validación de citas: el
validador no interpreta el texto del modelo, comprueba que cada `evidence_id`
existe en un diccionario. Si el modelo inventa `ev:9999`, no está, y la claim se
marca como no sustentada.

Los floats de confianza y de coste son `float` y no `Decimal` a propósito: el
coste se publica con 6 decimales y el ratio con los que salgan de la división;
no hay dinero real que redondear aquí.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

EvidenceKind = Literal[
    "event", "fingerprint", "incident", "anomaly", "comparison", "service", "health"
]
ClaimKind = Literal["fact", "hypothesis", "recommendation"]
Confidence = Literal["high", "medium", "low"]
RunStatus = Literal["RUNNING", "OK", "FAILED", "BLOCKED", "NEEDS_APPROVAL"]
VerifyAction = Literal["continue", "replan", "abort"]


class PlanStep(BaseModel):
    """Un paso del plan: qué se quiere comprobar, con qué tool y con qué args."""

    step_id: int
    hypothesis: str
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)

    @field_validator("step_id")
    @classmethod
    def _positive_step_id(cls, value: int) -> int:
        if value < 1:
            raise ValueError("step_id debe ser >= 1")
        return value

    @field_validator("hypothesis")
    @classmethod
    def _hypothesis_is_specific(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("hypothesis no puede estar vacía: el plan debe declarar qué comprueba")
        return value


class Plan(BaseModel):
    """Salida del planner, ya validada contra el catálogo de tools."""

    symptom: str
    restatement: str
    steps: list[PlanStep]
    max_steps: int = 8

    @field_validator("steps")
    @classmethod
    def _has_steps(cls, steps: list[PlanStep]) -> list[PlanStep]:
        if not steps:
            raise ValueError("el plan necesita al menos un paso")
        return steps

    def validate_shape(self, allowed_tools: set[str]) -> None:
        """Comprueba el plan contra la lista blanca y la aritmética de pasos.

        Se ejecuta *antes* de ninguna llamada: un plan con una tool inventada o
        con una dependencia imposible se rechaza sin gastar tokens ni tiempo.
        """
        if len(self.steps) > self.max_steps:
            raise ValueError(
                f"el plan tiene {len(self.steps)} pasos; el máximo es {self.max_steps}"
            )

        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError(f"step_id duplicados: {sorted(i for i in ids if ids.count(i) > 1)}")

        known = set(ids)
        seen: set[int] = set()
        for step in self.steps:
            if step.tool not in allowed_tools:
                raise ValueError(f"tool no permitida en un plan: {step.tool!r}")
            unknown = [dep for dep in step.depends_on if dep not in known]
            if unknown:
                raise ValueError(f"step {step.step_id} depende de pasos inexistentes: {unknown}")
            if step.step_id in step.depends_on:
                raise ValueError(f"step {step.step_id} depende de sí mismo")
            later = [dep for dep in step.depends_on if dep not in seen]
            if later:
                raise ValueError(
                    f"step {step.step_id} depende de pasos posteriores o no ejecutables: {later}"
                )
            seen.add(step.step_id)


class EvidenceItem(BaseModel):
    """Un hecho que el motor devolvió y que se puede citar."""

    evidence_id: str
    kind: EvidenceKind
    summary: str
    ts: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """Traza de una llamada del plan: qué se pidió y qué costó."""

    step_id: int
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0
    ok: bool = True
    error: str | None = None
    rows: int = 0
    result_hash: str = ""


class EvidenceBundle(BaseModel):
    """Todo lo que el motor devolvió en una corrida. La única fuente citable."""

    items: list[EvidenceItem] = Field(default_factory=list)
    by_id: dict[str, EvidenceItem] = Field(default_factory=dict)
    data_trust: Confidence = "low"
    tool_calls: list[ToolCall] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    def add(self, item: EvidenceItem) -> None:
        """Añade o fusiona evidencia. Fusionar importa: `top_errors` y
        `explain_fingerprint` describen la misma huella y el agente necesita
        ambas vistas bajo el mismo id."""
        current = self.by_id.get(item.evidence_id)
        if current is None:
            self.items.append(item)
            self.by_id[item.evidence_id] = item
            return
        merged = current.model_copy(
            update={
                "summary": item.summary or current.summary,
                "ts": item.ts or current.ts,
                "fields": {**current.fields, **item.fields},
            }
        )
        self.items[self.items.index(current)] = merged
        self.by_id[item.evidence_id] = merged

    def ids(self) -> list[str]:
        return list(self.by_id)


class Claim(BaseModel):
    """Una afirmación del diagnóstico. Sólo las de tipo `fact` exigen cita."""

    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    kind: ClaimKind = "fact"

    @field_validator("text")
    @classmethod
    def _text_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("una claim vacía no afirma nada")
        return value


class Diagnosis(BaseModel):
    """Lo que el agente entrega. `evidence_gaps` es obligatorio."""

    symptom: str
    root_cause: str | None = None
    root_cause_fingerprint: str | None = None
    # Categoría que el motor le asignó a la huella raíz (p. ej. "storage").
    # No la deduce el modelo: sale de `error_rules.yaml` en la ingesta. Es lo
    # que permite medir `category_accuracy` sin un juez humano ni otro LLM.
    root_cause_category: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    affected_services: list[str] = Field(default_factory=list)
    confidence: Confidence = "low"
    evidence_gaps: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    # Propuesta de mutación. El bucle no la ejecuta: la convierte en una acción
    # pendiente de aprobación humana.
    proposed_incident_id: int | None = None
    proposed_status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"] | None = None


class CitationReport(BaseModel):
    """Resultado de la validación mecánica de citas."""

    valid: list[Claim] = Field(default_factory=list)
    invalid: list[Claim] = Field(default_factory=list)
    uncited: list[Claim] = Field(default_factory=list)

    @property
    def fact_claims(self) -> list[Claim]:
        return [c for c in [*self.valid, *self.invalid, *self.uncited] if c.kind == "fact"]

    @property
    def fact_failure_rate(self) -> float:
        facts = self.fact_claims
        if not facts:
            return 0.0
        failed = [c for c in facts if c in self.invalid or c in self.uncited]
        return len(failed) / len(facts)

    @property
    def hallucinated(self) -> list[Claim]:
        return self.invalid


class VerifyResult(BaseModel):
    """Veredicto de los chequeos deterministas entre etapas."""

    action: VerifyAction
    reason: str
    checks: dict[str, bool] = Field(default_factory=dict)


class AgentRun(BaseModel):
    """Resultado completo de una corrida, más lo que hace falta para auditarla."""

    run_id: str
    symptom: str
    status: RunStatus
    plan: Plan | None = None
    diagnosis: Diagnosis | None = None
    citations: CitationReport | None = None
    evidence: EvidenceBundle = Field(default_factory=EvidenceBundle)
    pending_action: dict[str, Any] | None = None
    model: str | None = None
    prompt_version: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    trace_path: str | None = None
    error: str | None = None


__all__ = [
    "AgentRun",
    "CitationReport",
    "Claim",
    "Confidence",
    "Diagnosis",
    "EvidenceBundle",
    "EvidenceItem",
    "EvidenceKind",
    "Plan",
    "PlanStep",
    "RunStatus",
    "ToolCall",
    "VerifyAction",
    "VerifyResult",
]
