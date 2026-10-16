"""Registry de patrones: agregar un formato es agregar un YAML, no escribir código.

Dos tipos de patrón, porque las fuentes de logs reales caen en dos categorías:

* `kind: regex` — logs orientados a línea (Apache, WebLogic). El regex
  **debe** definir el grupo con nombre `ts`; `level`, `service`, `logger`,
  `status` y `message` son opcionales. Es un contrato explícito: si el YAML
  olvida `ts`, `load_patterns` falla al arrancar, no a mitad de la ingesta.

* `kind: json` — logs estructurados. En vez de una regex, se declaran listas de
  claves candidatas por campo (soporta rutas con punto: `log.level`). El primer
  nombre presente gana, lo que cubre la variación entre servicios que escriben
  JSON con claves distintas para lo mismo.

Por qué no hardcodear un `if "GET" in line`: en la Fase 4 queremos IIS,
WebLogic y JSON. Con funciones, el cuarto formato obliga a tocar el parser y a
volver a testear todo. Con el registry, es un archivo nuevo.

`parse_line()` **nunca lanza por una línea mala**: devuelve siempre un
`ParseResult` con `parsed_ok=0` y el motivo en `parse_error`. Descartar en
silencio hace la pérdida de datos invisible, que es la peor clase de bug en
una herramienta de observabilidad.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import yaml

from app.parser.normalization import TimestampError, parse_timestamp, to_utc_iso
from app.parser.severity import UNKNOWN, canonical_level, severity_of

# Grupos con nombre que el registry reconoce en un patrón regex.
REQUIRED_GROUPS = ("ts",)
OPTIONAL_GROUPS = ("level", "service", "logger", "status", "message", "client")

KIND_REGEX = "regex"
KIND_JSON = "json"

# Flags de `re` que un patrón puede pedir por nombre desde el YAML.
_REGEX_FLAGS: dict[str, int] = {
    "ascii": re.ASCII,
    "dotall": re.DOTALL,
    "ignorecase": re.IGNORECASE,
    "multiline": re.MULTILINE,
    "verbose": re.VERBOSE,
}

# Para interpolar message_template sin que un grupo ausente rompa el formato.
class _Groups(dict):
    def __missing__(self, key: str) -> str:
        return ""


class PatternError(Exception):
    """Definición de patrón inválida. Falla al cargar, nunca durante la ingesta."""


@dataclass(frozen=True)
class PatternSpec:
    """Un formato de log, declarado por completo en YAML."""

    name: str
    kind: str = KIND_REGEX
    priority: int = 0
    description: str = ""
    # Soporte de entrada multilínea (Fase 2: ensamblado de stack traces).
    multiline: bool = False

    # --- kind: regex ---
    regex: re.Pattern[str] | None = None
    flags: int = 0
    # Plantilla de mensaje a partir de grupos, p.ej. '"{method} {path}" -> {status}'.
    message_template: str | None = None

    # --- kind: json ---
    fields: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # --- común ---
    timestamp_formats: tuple[str, ...] = ("iso8601",)
    # None = hereda del Registry (y de config.yaml). Fijarla en el patrón
    # sobreescribiría la config global sin avisar.
    default_timezone: str | None = None
    # Zonas abreviadas → offset numérico: {'CEST': '+0200', 'WIB': '+0700'}.
    # `strptime` no entiende abreviaturas de zona, y en logs empresariales son
    # la norma (CEST, MET, WIB, JST). El grupo `zone` se resuelve por aquí antes
    # de pasar el timestamp a strptime, así que el formato NO lleva `%z`.
    zone_map: dict[str, str] = field(default_factory=dict)
    english_months: bool = False
    default_level: str | None = None
    # Traducciones puntuales de nivel, por encima de los alias globales.
    level_map: dict[str, str] = field(default_factory=dict)
    # Umbrales de nivel por código de estado: [{from: 500, level: ERROR}, ...]
    status_levels: tuple[tuple[int, str], ...] = ()

    def compile_ok(self) -> bool:
        return self.kind == KIND_JSON or self.regex is not None


def _split_entry(text: str, spec: PatternSpec) -> tuple[str, str]:
    """Separa la cabecera (la línea con timestamp) del cuerpo de continuación.

    Solo para patrones `multiline`. El regex se aplica a la cabecera, que es una
    sola línea y por tanto encaja con cualquier patrón. Las líneas siguientes (el
    stack trace) no se parsean: se anexan al mensaje.

    Si el patrón no es multilínea, el texto entero va a la cabecera. Así una
    entrada JSON con un salto a mitad de línea no se trunca en un punto
    arbitrario, que es justo lo que un patrón de una línea no debería decidir.
    """
    if not spec.multiline or "\n" not in text:
        return text, ""

    head, _, continuation = text.partition("\n")
    return head, continuation


@dataclass(frozen=True)
class ParseResult:
    """Evento ya normalizado. Los mirrors de las columnas de `events`."""

    pattern: str
    parsed_ok: bool
    raw_line: str
    parse_error: str | None = None
    timestamp_utc: str | None = None
    timestamp_raw: str | None = None
    source_tz: str | None = None
    level_raw: str | None = None
    level_norm: str = "UNKNOWN"
    severity: int = UNKNOWN
    service: str | None = None
    logger: str | None = None
    message: str | None = None


# --- carga -------------------------------------------------------------


def _require(data: dict[str, Any], key: str, path: Path) -> Any:
    if key not in data:
        raise PatternError(f"{path.name}: falta la clave obligatoria '{key}'")
    return data[key]


def _as_str_tuple(value: Any, label: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise PatternError(f"{path.name}: {label} debe ser string o lista de strings")


def _timestamp_options(data: dict[str, Any], path: Path) -> tuple[tuple[str, ...], str | None, bool]:
    """Lee las opciones de timestamp aceptan tanto la forma anidada como la plana.

    Aceptado:
        timestamp: {formats: [...], default_timezone: UTC, english_months: true}
        timestamp_formats: [...]        # atajo plano

    `default_timezone` ausente queda en None para que el patrón herede el valor
    del Registry (y por tanto de config.yaml).
    """
    section = data.get("timestamp")
    if section is not None and not isinstance(section, dict):
        raise PatternError(f"{path.name}: 'timestamp' debe ser un bloque con 'formats'")

    section = section or {}
    formats = _as_str_tuple(section.get("formats", data.get("timestamp_formats")), "timestamp.formats", path)
    declared = section.get("default_timezone", data.get("default_timezone"))
    timezone = str(declared) if declared else None
    english_months = bool(section.get("english_months", data.get("english_months", False)))
    return formats or ("iso8601",), timezone, english_months


def _load_spec(path: Path) -> PatternSpec:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PatternError(f"{path.name}: YAML inválido: {exc}") from exc
    if not isinstance(data, dict):
        raise PatternError(f"{path.name}: la raíz debe ser un mapeo")

    name = str(_require(data, "name", path))
    kind = str(data.get("kind", KIND_REGEX)).lower()
    if kind not in (KIND_REGEX, KIND_JSON):
        raise PatternError(f"{path.name}: kind debe ser '{KIND_REGEX}' o '{KIND_JSON}', no {kind!r}")

    formats, timezone, english_months = _timestamp_options(data, path)
    # La zona se valida al cargar, no al parsear: una zona inválida debe romper
    # el arranque, no aparecer en producción como una tanda de eventos con
    # `parsed_ok=0` que nadie sabe explicar.
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except Exception as exc:  # noqa: BLE001 - ZoneInfo lanza varios tipos
            raise PatternError(f"{path.name}: default_timezone inválido {timezone!r}: {exc}") from exc

    spec = PatternSpec(
        name=name,
        kind=kind,
        priority=int(data.get("priority", 0)),
        description=str(data.get("description", "")),
        multiline=bool(data.get("multiline", False)),
        timestamp_formats=formats,
        default_timezone=timezone,
        english_months=english_months,
        default_level=(str(data["default_level"]) if data.get("default_level") else None),
        level_map={str(k): str(v) for k, v in (data.get("level_map") or {}).items()},
    )

    if kind == KIND_REGEX:
        pattern_text = _require(data, "regex", path)

        flags = 0
        for flag_name in _as_str_tuple(data.get("flags"), "flags", path):
            key = flag_name.lower()
            if key not in _REGEX_FLAGS:
                raise PatternError(
                    f"{path.name}: flag desconocido {flag_name!r}; "
                    f"disponibles: {sorted(_REGEX_FLAGS)}"
                )
            flags |= _REGEX_FLAGS[key]

        # Un regex escrito como bloque YAML (`regex: |`) llega con saltos de línea
        # reales. Sin VERBOSE esos saltos se literalan y el patrón deja de
        # coincidir contra una línea de una sola línea, sin error de sintaxis que
        # lo delate: el síntoma es "ningún patrón coincide", que no señala al
        # regex. Se asume VERBOSE para los multilínea y se documenta, porque en un
        # log no se quiere espacio literal: se usa `\s` o `[ ]`.
        if "\n" in pattern_text:
            flags |= re.VERBOSE

        try:
            compiled = re.compile(pattern_text, flags)
        except re.error as exc:
            raise PatternError(f"{path.name}: regex inválida: {exc}") from exc

        groups = set(compiled.groupindex)
        missing = [g for g in REQUIRED_GROUPS if g not in groups]
        if missing:
            raise PatternError(
                f"{path.name}: al regex le faltan los grupos con nombre {missing}. "
                f"Contrato del registry: {list(REQUIRED_GROUPS)} obligatorios, "
                f"{list(OPTIONAL_GROUPS)} opcionales. Grupos presentes: {sorted(groups)}"
            )
        template = data.get("message_template")
        if template:
            unknown = _template_groups(str(template)) - groups
            if unknown:
                raise PatternError(
                    f"{path.name}: message_template usa {sorted(unknown)}, "
                    "que no son grupos del regex"
                )

        spec = _replace(spec, regex=compiled, flags=flags, message_template=template)

    else:  # json
        raw_fields = data.get("fields") or {}
        if not isinstance(raw_fields, dict):
            raise PatternError(f"{path.name}: 'fields' debe ser un mapeo")
        parsed_fields = {
            str(k): _as_str_tuple(v, f"fields.{k}", path) for k, v in raw_fields.items()
        }
        if not any("ts" in k for k in parsed_fields):
            raise PatternError(f"{path.name}: 'fields' necesita una entrada de timestamp (ts)")
        spec = _replace(spec, fields=parsed_fields)

    status_levels: list[tuple[int, str]] = []
    for rule in data.get("status_levels") or []:
        if not isinstance(rule, dict) or "from" not in rule or "level" not in rule:
            raise PatternError(f"{path.name}: cada status_levels necesita 'from' y 'level'")
        status_levels.append((int(rule["from"]), str(rule["level"])))
    status_levels.sort(reverse=True)

    zone_map = {str(k).upper(): str(v) for k, v in (data.get("zone_map") or {}).items()}
    for zone, offset in zone_map.items():
        if not re.fullmatch(r"[+-]\d{4}", offset):
            raise PatternError(
                f"{path.name}: zone_map[{zone}]={offset!r} debe ser un offset numérico tipo '+0200'"
            )

    return _replace(spec, status_levels=tuple(status_levels), zone_map=zone_map)


def _template_groups(template: str) -> set[str]:
    """Nombres referenciados por un `message_template` tipo `{method} {path}`."""
    return set(re.findall(r"{(\w+)[^}]*}", template))


def _replace(spec: PatternSpec, **changes) -> PatternSpec:
    from dataclasses import replace

    return replace(spec, **changes)


def load_patterns(directory: str | Path) -> list[PatternSpec]:
    """Carga todos los `*.yaml` de un directorio, ordenados por nombre."""
    root = Path(directory)
    if not root.is_dir():
        raise PatternError(f"directorio de patrones inexistente: {root}")
    files = sorted([*root.glob("*.yaml"), *root.glob("*.yml")])
    if not files:
        raise PatternError(f"no hay patrones en {root}")

    specs = [_load_spec(path) for path in files]
    names = [s.name for s in specs]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise PatternError(f"nombres de patrón duplicados: {sorted(duplicates)}")
    return specs


# --- registro ----------------------------------------------------------


class Registry:
    """Colección de patrones que sabe convertir una línea en `ParseResult`."""

    def __init__(self, specs: list[PatternSpec], *, default_timezone: str = "UTC") -> None:
        if not specs:
            raise PatternError("el registry no puede estar vacío")
        # Mayor prioridad primero. Los formatos más específicos se detectan antes.
        self._specs = tuple(sorted(specs, key=lambda s: (-s.priority, s.name)))
        self._default_timezone = default_timezone
        self._compiled = tuple(
            (s, s.regex) for s in self._specs if s.kind == KIND_REGEX and s.regex is not None
        )

    @classmethod
    def from_directory(cls, directory: str | Path, *, default_timezone: str = "UTC") -> "Registry":
        return cls(load_patterns(directory), default_timezone=default_timezone)

    @property
    def specs(self) -> tuple[PatternSpec, ...]:
        return self._specs

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self._specs)

    def __iter__(self) -> Iterator[PatternSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def detect(self, line: str) -> PatternSpec | None:
        """Primer patrón que coincide, por orden de prioridad.

        En un patrón `multiline` se prueba solo la cabecera. Probar el texto
        completo fallaría: el stack trace arrastra saltos de línea que el regex no
        puede abarcar, así que una entrada ya ensamblada se clasificaría como
        "sin patrón" y se descartaría justo cuando contiene el incidente entero.
        """
        head = line.partition("\n")[0] if "\n" in line else line
        for spec, compiled in self._compiled:
            probe = head if spec.multiline else line
            if compiled.search(probe):
                return spec
        for spec in self._specs:
            if spec.kind == KIND_JSON and _looks_like_json(line):
                return spec
        return None

    def parse_line(self, line: str) -> ParseResult:
        """Convierte una línea en `ParseResult`. Nunca lanza por datos malos."""
        text = line.rstrip("\n").rstrip("\r")
        if not text.strip():
            return ParseResult(pattern="", parsed_ok=False, raw_line=text, parse_error="línea vacía")

        spec = self.detect(text)
        if spec is None:
            return ParseResult(
                pattern="",
                parsed_ok=False,
                raw_line=text,
                parse_error="ningún patrón coincide",
                message=text,
            )

        try:
            if spec.kind == KIND_REGEX:
                return self._parse_regex(spec, text)
            return self._parse_json(spec, text)
        except TimestampError as exc:
            # El nivel y el mensaje pueden haberse leído bien: se conservan.
            return ParseResult(
                pattern=spec.name,
                parsed_ok=False,
                raw_line=text,
                parse_error=f"timestamp: {exc}",
                timestamp_raw=text,
                message=text,
            )
        except Exception as exc:  # noqa: BLE001 - una línea no puede tumbar la ingesta
            return ParseResult(
                pattern=spec.name,
                parsed_ok=False,
                raw_line=text,
                parse_error=f"{type(exc).__name__}: {exc}",
                message=text,
            )

    @staticmethod
    def _resolve_zone(spec: PatternSpec, ts_raw: str, zone_raw: str | None) -> tuple[str, str | None]:
        """Sustituye una zona abreviada por su offset numérico, dentro del timestamp.

        `strptime` con `%z` no entiende `CEST`, y en logs de servidores
        aplicaciones esa forma es más común que `+0200`. El patrón declara
        `zone_map` para que esa traducción viva en la config y no en código.

        Se sustituye **en su sitio**, no se anexa: el grupo `ts` ya incluye la
        zona, y los formatos llevan `%z` en esa posición. Una línea que venga
        con `+0200` no pasa por aquí y no necesita ningún ajuste.

        Una zona no listada en `zone_map` se deja intacta a propósito: `strptime`
        fallará y el evento quedará como `parsed_ok=0`. Adivinar una zona
        produciría timestamps silenciosamente equivocados, que es peor que no
        parsear: el error queda visible en la cobertura y en `parse_error`.
        """
        if not spec.zone_map:
            return ts_raw, None

        zone = (zone_raw or "").strip()
        if not zone:
            return ts_raw, None

        mapped = spec.zone_map.get(zone.upper())
        if mapped is None:
            return ts_raw, None

        parts = ts_raw.split()
        if not parts or parts[-1].upper() != zone.upper():
            return ts_raw, None

        parts[-1] = mapped
        return " ".join(parts), mapped

    # --- implementaciones por kind ---

    def _parse_regex(self, spec: PatternSpec, text: str) -> ParseResult:
        head, continuation = _split_entry(text, spec)

        match = spec.regex.search(head)
        if match is None:  #pragma: no cover - detect() ya lo garantiza
            return ParseResult(spec.name, False, text, "el regex dejó de coincidir", message=text)
        groups = _Groups(match.groupdict())

        ts_raw = _clean(groups.get("ts"))
        ts_for_parse, source_tz_hint = self._resolve_zone(spec, ts_raw, groups.get("zone"))
        moment, source_tz = parse_timestamp(
            ts_for_parse,
            spec.timestamp_formats,
            default_timezone=spec.default_timezone or self._default_timezone,
            english_months=spec.english_months,
        )
        if source_tz is None:
            source_tz = source_tz_hint

        level_raw = _clean(groups.get("level"))
        level_norm = self._resolve_level(spec, level_raw, groups.get("status"))

        message = (
            spec.message_template.format_map(groups).strip()
            if spec.message_template
            else _clean(groups.get("message")) or head
        )

        # La continuación del stack trace se une al mensaje, no se parsea: forma
        # parte del mismo incidente y sin ella el evento es inútil para
        # diagnosticar. Se guarda tal cual, sin normalizar, porque un trace
        # alterado ya no sirve para buscar la línea exacta en el log original.
        if continuation:
            message = f"{message}\n{continuation}"

        return ParseResult(
            pattern=spec.name,
            parsed_ok=True,
            raw_line=text,
            timestamp_utc=to_utc_iso(moment),
            timestamp_raw=ts_raw,
            source_tz=source_tz,
            level_raw=level_raw,
            level_norm=level_norm,
            severity=severity_of(level_norm),
            service=_clean(groups.get("service")),
            logger=_clean(groups.get("logger")),
            message=message,
        )

    def _parse_json(self, spec: PatternSpec, text: str) -> ParseResult:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return ParseResult(
                spec.name, False, text, f"JSON inválido: {exc}", message=text
            )
        if not isinstance(payload, dict):
            return ParseResult(
                spec.name, False, text, "JSON de primer nivel no es un objeto", message=text
            )

        def pick(field_name: str) -> str | None:
            for candidate in spec.fields.get(field_name, ()):
                found = _dig(payload, candidate)
                if found is None:
                    continue
                # Un candidato que resuelve a un dict/list no es un valor de campo: se salta
                # y se sigue buscando. Sin esto, un JSON con
                # {"log": {"message": ...}} devolvería el dict entero serializado
                # como mensaje, porque `log` aparece antes que `log.message`.
                if isinstance(found, (dict, list)):
                    continue
                return str(found)
            return None

        ts_raw = pick("ts")
        if not ts_raw:
            return ParseResult(spec.name, False, text, "sin timestamp", message=text)
        moment, source_tz = parse_timestamp(
            ts_raw,
            spec.timestamp_formats,
            default_timezone=spec.default_timezone or self._default_timezone,
            english_months=spec.english_months,
        )

        level_raw = pick("level")
        status = pick("status")
        level_norm = self._resolve_level(spec, level_raw, status)

        return ParseResult(
            pattern=spec.name,
            parsed_ok=True,
            raw_line=text,
            timestamp_utc=to_utc_iso(moment),
            timestamp_raw=ts_raw,
            source_tz=source_tz,
            level_raw=level_raw,
            level_norm=level_norm,
            severity=severity_of(level_norm),
            service=pick("service"),
            logger=pick("logger"),
            message=pick("message") or text,
        )

    def _resolve_level(self, spec: PatternSpec, level_raw: str | None, status: str | None) -> str:
        if level_raw:
            mapped = spec.level_map.get(level_raw.strip(), level_raw)
            return canonical_level(mapped)
        if status and status.strip().isdigit():
            code = int(status)
            for threshold, level in spec.status_levels:
                if code >= threshold:
                    return canonical_level(spec.level_map.get(level, level))
        return canonical_level(spec.default_level)


def _looks_like_json(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("{") and stripped.endswith("}")


def _dig(payload: dict[str, Any], path: str) -> Any:
    """Busca `a.b.c` en un dict anidado. Devuelve None si algún tramo falta."""
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return None if current is None else current


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None