"""Clasificación de errores: reglas en YAML → `events.category`.

Una regla responde a una pregunta corta: *¿qué tipo de problema es este
mensaje?* De ahí sale la categoría, que forma parte del fingerprint y es la
etiqueta que agrupa el dashboard.

Esta es la segunda mitad del contrato declarativo de la Fase 1 (ADR-002): los
formatos son YAML y las categorías también. Agregar una categoría nueva no
toca Python.

El contrato de fallo es el de la configuración en este proyecto: **un YAML mal
formado rompe al cargar, nunca durante la ingesta.** Si una regla con regex
inválida se detectara al evaluar un evento, la mitad de los eventos quedarían
sin clasificar y nadie sabría por qué; si falla al cargar, el error incluye la
regla culpable y el proceso no arranca.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import yaml

from app.config import ConfigError

DEFAULT_RULES_PATH = Path(__file__).with_name("error_rules.yaml")

_RULE_KEYS = {"id", "category", "patterns", "description"}


@dataclass(frozen=True)
class ErrorRule:
    """Una regla ya compilada. Inmutable: se carga una vez y no cambia."""

    id: str
    category: str
    patterns: tuple[str, ...]
    description: str = ""
    compiled: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)

    @classmethod
    def build(
        cls, id: str, category: str, patterns: Sequence[str], description: str = ""
    ) -> "ErrorRule":
        compiled = tuple(_compile(p, rule_id=id) for p in patterns)
        return cls(
            id=id,
            category=category,
            patterns=tuple(patterns),
            description=description,
            compiled=compiled,
        )

    def matches(self, message: str) -> bool:
        return any(rx.search(message) for rx in self.compiled)


def _compile(pattern: str, *, rule_id: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ConfigError(
            f"error_rules: la regla {rule_id!r} tiene un regex inválido "
            f"{pattern!r}: {exc}"
        ) from exc


def _require(rule: dict[str, Any], key: str, rule_id: str) -> Any:
    if key not in rule:
        raise ConfigError(f"error_rules: la regla {rule_id!r} no declara {key!r}")
    return rule[key]


def _parse_rule(raw: Any, index: int) -> ErrorRule:
    if not isinstance(raw, dict):
        raise ConfigError(f"error_rules: la regla #{index + 1} debe ser un bloque, no {type(raw).__name__}")

    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise ConfigError(
            f"error_rules: la regla #{index + 1} tiene claves desconocidas {sorted(unknown)}"
        )

    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ConfigError(f"error_rules: la regla #{index + 1} necesita un 'id' de texto no vacío")

    category = _require(raw, "category", rule_id)
    if not isinstance(category, str) or not category.strip():
        raise ConfigError(f"error_rules: la regla {rule_id!r} necesita una 'category' de texto no vacío")

    patterns = _require(raw, "patterns", rule_id)
    if not isinstance(patterns, list) or not patterns:
        raise ConfigError(f"error_rules: la regla {rule_id!r} necesita 'patterns' con al menos un valor")
    if not all(isinstance(p, str) and p.strip() for p in patterns):
        raise ConfigError(f"error_rules: la regla {rule_id!r} tiene un patrón que no es texto no vacío")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"error_rules: la regla {rule_id!r} tiene un 'description' que no es texto")

    return ErrorRule.build(rule_id, category, patterns, description)


def load_rules(path: str | Path) -> "ErrorRules":
    """Carga y valida las reglas. Un fallo aquí es un error de configuración."""
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"error_rules: no existe el archivo {file}")

    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"error_rules: {file.name} no es YAML válido: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"error_rules: {file.name} debe tener un mapeo en la raíz")

    unknown = set(data) - {"rules"}
    if unknown:
        raise ConfigError(f"error_rules: {file.name} tiene claves desconocidas {sorted(unknown)}")

    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ConfigError(f"error_rules: {file.name} necesita una lista 'rules' no vacía")

    rules = [_parse_rule(raw, i) for i, raw in enumerate(raw_rules)]

    ids = [r.id for r in rules]
    duplicados = sorted({i for i in ids if ids.count(i) > 1})
    if duplicados:
        raise ConfigError(f"error_rules: ids duplicados {duplicados}; el orden ya define la prioridad")

    return ErrorRules(rules)


class ErrorRules:
    """Conjunto de reglas en orden de prioridad. Primera coincidencia gana."""

    def __init__(self, rules: Sequence[ErrorRule]):
        self._rules = tuple(rules)

    @classmethod
    def load(cls, path: str | Path) -> "ErrorRules":
        return load_rules(path)

    @classmethod
    def default(cls) -> "ErrorRules":
        """Las reglas empaquetadas. Fallback de quien no tiene config a mano."""
        return load_rules(DEFAULT_RULES_PATH)

    @property
    def rules(self) -> tuple[ErrorRule, ...]:
        return self._rules

    @property
    def categories(self) -> tuple[str, ...]:
        """Categorías distintas, en orden de aparición."""
        return tuple(dict.fromkeys(r.category for r in self._rules))

    def classify(self, message: str | None) -> ErrorRule | None:
        if not message:
            return None
        for rule in self._rules:
            if rule.matches(message):
                return rule
        return None

    def category(self, message: str | None) -> str | None:
        rule = self.classify(message)
        return rule.category if rule else None

    def __iter__(self) -> Iterator[ErrorRule]:
        return iter(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def __repr__(self) -> str:  # pragma: no cover - depuración
        return f"ErrorRules({len(self._rules)} reglas, {len(self.categories)} categorías)"


__all__ = ["DEFAULT_RULES_PATH", "ErrorRule", "ErrorRules", "load_rules"]
