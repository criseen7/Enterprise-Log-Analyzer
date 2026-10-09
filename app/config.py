"""Carga de configuración en tres capas, con prioridad ascendente:

    defaults (este módulo)  <  config.yaml  <  config.local.yaml  <  variables de entorno

Las rutas se resuelven a absolutas al cargar. Streamlit y Docker cambian el
directorio de trabajo, así que depender de rutas relativas rompería en silencio.

Cada campo declara su nombre de variable de entorno en `field(metadata=...)`,
de modo que la lista de `ELA_*` admitidas sale del propio código y no de la
documentación, que es lo único que garantiza que no se desincronice.
"""

from __future__ import annotations

import os
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILENAME = "config.yaml"
LOCAL_CONFIG_FILENAME = "config.local.yaml"
ENV_PREFIX = "ELA_"
PROJECT_ROOT_ENV = "ELA_PROJECT_ROOT"


class ConfigError(Exception):
    """Configuración ausente o mal formada. Falla temprano y de forma legible."""


@dataclass(frozen=True)
class PathsConfig:
    log_dir: Path = field(default=Path("logs"), metadata={"env": "ELA_LOG_DIR"})
    db_path: Path = field(default=Path("data/app.db"), metadata={"env": "ELA_DB_PATH"})
    export_dir: Path = field(default=Path("exports"), metadata={"env": "ELA_EXPORT_DIR"})
    patterns_dir: Path = field(
        default=Path("app/parser/patterns"), metadata={"env": "ELA_PATTERNS_DIR"}
    )


@dataclass(frozen=True)
class IngestConfig:
    # Globs de archivos a vigilar, relativos a `paths.log_dir`.
    #
    # `*.log.*` entra a propósito: los rotados se reconocen por inode y no se
    # reingieren, y sin ellos una rotación sería invisible hasta el arranque
    # siguiente. Las demás extensiones quedan fuera: un directorio de logs no es
    # un contenedor arbitrario y su contenido depende del usuario.
    include_patterns: tuple[str, ...] = field(
        default=("*.log", "*.log.*"),
        metadata={"env": "ELA_INCLUDE_PATTERNS"},
    )
    poll_interval_seconds: float = field(
        default=5.0, metadata={"env": "ELA_POLL_INTERVAL_SECONDS"}
    )
    debounce_seconds: float = field(default=2.0, metadata={"env": "ELA_DEBOUNCE_SECONDS"})
    chunk_bytes: int = field(default=1_048_576, metadata={"env": "ELA_CHUNK_BYTES"})
    default_timezone: str = field(default="UTC", metadata={"env": "ELA_DEFAULT_TIMEZONE"})


@dataclass(frozen=True)
class DatabaseConfig:
    journal_mode: str = field(default="wal", metadata={"env": "ELA_JOURNAL_MODE"})
    busy_timeout_ms: int = field(default=5000, metadata={"env": "ELA_BUSY_TIMEOUT_MS"})
    synchronous: str = field(default="NORMAL", metadata={"env": "ELA_SYNCHRONOUS"})
    max_size_gb: int = field(default=4, metadata={"env": "ELA_MAX_SIZE_GB"})


@dataclass(frozen=True)
class AnalysisConfig:
    # Reglas de clasificación de error (app/analysis/error_rules.yaml).
    #
    # Es Path y no un literal dentro del código porque las reglas son datos de
    # operación: un despliegue puede tener las suyas sin recompilar nada. Se
    # resuelve a ruta absoluta igual que el resto, así que el watcher no depende
    # del directorio de trabajo.
    rules_path: Path = field(
        default=Path("app/analysis/error_rules.yaml"), metadata={"env": "ELA_ERROR_RULES_PATH"}
    )
    min_incident_severity: int = field(
        default=40, metadata={"env": "ELA_MIN_INCIDENT_SEVERITY"}
    )
    window_minutes: int = field(default=15, metadata={"env": "ELA_WINDOW_MINUTES"})
    incident_threshold: int = field(default=50, metadata={"env": "ELA_INCIDENT_THRESHOLD"})
    cooldown_minutes: int = field(default=10, metadata={"env": "ELA_COOLDOWN_MINUTES"})
    anomaly_zscore: float = field(default=3.0, metadata={"env": "ELA_ANOMALY_ZSCORE"})
    anomaly_min_samples: int = field(default=5, metadata={"env": "ELA_ANOMALY_MIN_SAMPLES"})


@dataclass(frozen=True)
class UIConfig:
    page_title: str = field(
        default="Enterprise Log Analyzer", metadata={"env": "ELA_PAGE_TITLE"}
    )
    page_icon: str = field(default="📊", metadata={"env": "ELA_PAGE_ICON"})
    cache_ttl_seconds: int = field(default=30, metadata={"env": "ELA_CACHE_TTL_SECONDS"})
    default_page_size: int = field(default=100, metadata={"env": "ELA_DEFAULT_PAGE_SIZE"})


@dataclass(frozen=True)
class Settings:
    """Configuración completa y ya resuelta. Inmutable a propósito."""

    project_root: Path
    paths: PathsConfig = field(default_factory=PathsConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    @property
    def log_files(self) -> list[Path]:
        """Archivos de log que el ingestor debe considerar, ordenados."""
        if not self.paths.log_dir.is_dir():
            return []
        seen: dict[Path, None] = {}
        for pattern in self.ingest.include_patterns:
            seen.update({p.resolve(): None for p in self.paths.log_dir.glob(pattern) if p.is_file()})
        return sorted(seen)


def _project_root() -> Path:
    root = os.environ.get(PROJECT_ROOT_ENV)
    if root:
        return Path(root).expanduser().resolve()
    # app/config.py -> app/ -> raíz del repo
    return Path(__file__).resolve().parents[1]


def _coerce(raw: str, target: Any) -> Any:
    """Convierte un valor de entorno (siempre str) al tipo declarado del campo."""
    if target is bool:
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"valor booleano inválido: {raw!r}")
    if target is Path or (isinstance(target, type) and issubclass(target, Path)):
        # Sin esto, un override por env de una ruta queda como `str` y revienta
        # después en `.is_dir()`, que es justo donde ya es tarde para explicar
        # qué pasó. La ruta se resuelve contra la raíz en `_build_section`, igual
        # que la que viene del YAML.
        #
        # Se comprueba con `issubclass` y no con `is Path`: el default es
        # `PosixPath`/`WindowsPath`, nunca la clase base.
        value = Path(raw).expanduser()
        if not str(value).strip():
            raise ConfigError(f"ruta de entorno vacía: {raw!r}")
        return value
    if target is int:
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"valor entero inválido: {raw!r}") from exc
    if target is float:
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"valor decimal inválido: {raw!r}") from exc
    if target is tuple:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return raw


def _apply_env(cls: type, instance: Any) -> None:
    """Sobrescribe los campos de `instance` con las variables de entorno presentes."""
    for f in fields(instance):
        env_name = f.metadata.get("env")
        if not env_name or env_name not in os.environ:
            continue
        target = f.type
        if isinstance(target, str):  # postponed annotations -> castear desde el default
            target = type(f.default) if f.default is not MISSING else str
        try:
            object.__setattr__(instance, f.name, _coerce(os.environ[env_name], target))
        except ConfigError as exc:
            raise ConfigError(f"{env_name}: {exc}") from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} no es YAML válido: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path.name}: la raíz debe ser un mapeo")
    return loaded


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge profundo: las claves de `override` ganan, sin perder las hermanas."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build_section(cls: type, data: dict[str, Any], root: Path):
    """Instancia un dataclass de sección aplicando defaults, YAML y entorno."""
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"{cls.__name__}: claves desconocidas {sorted(unknown)}")

    kwargs: dict[str, Any] = {}
    for name, f in known.items():
        if name not in data:
            continue
        value = data[name]
        declared = f.default if f.default is not MISSING else None
        if isinstance(declared, Path) and isinstance(value, str):
            value = Path(value)
        elif isinstance(declared, tuple) and isinstance(value, list):
            value = tuple(value)
        elif isinstance(value, dict):
            raise ConfigError(f"{cls.__name__}.{name}: no se esperaba un bloque aquí")
        kwargs[name] = value

    instance = cls(**kwargs)
    _apply_env(cls, instance)

    # Las rutas son relativas a la raíz del proyecto y deben quedar absolutas.
    for name, f in known.items():
        value = getattr(instance, name)
        if isinstance(value, Path) and not value.is_absolute():
            object.__setattr__(instance, name, (root / value).resolve())
    return instance


def load_settings(*, config_path: str | Path | None = None) -> Settings:
    """Carga la configuración aplicando defaults < YAML < YAML local < entorno.

    `config_path` reemplaza al archivo por defecto en lugar de competir con él:
    pasar un archivo explícito debe servir para testear esa configuración exacta.
    """
    root = _project_root()
    data: dict[str, Any] = {}
    if config_path is not None:
        candidates = [Path(config_path).expanduser()]
    else:
        candidates = [root / DEFAULT_CONFIG_FILENAME]
    candidates.append(root / LOCAL_CONFIG_FILENAME)

    for candidate in candidates:
        data = _merge(data, _read_yaml(candidate))

    settings = Settings(
        project_root=root,
        paths=_build_section(PathsConfig, data.get("paths", {}), root),
        ingest=_build_section(IngestConfig, data.get("ingest", {}), root),
        db=_build_section(DatabaseConfig, data.get("db", {}), root),
        analysis=_build_section(AnalysisConfig, data.get("analysis", {}), root),
        ui=_build_section(UIConfig, data.get("ui", {}), root),
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    """Falla temprano: un umbral absurdo debe fallar al arrancar, no en producción."""
    problems: list[str] = []

    journal = settings.db.journal_mode.lower()
    if journal != "wal":
        problems.append(
            f"db.journal_mode={journal!r}. El diseño depende de WAL: sin él, el "
            "watcher y el dashboard se bloquean entre sí (ver ADR-001)."
        )
    if settings.db.busy_timeout_ms <= 0:
        problems.append("db.busy_timeout_ms debe ser > 0")
    if settings.ingest.chunk_bytes <= 0:
        problems.append("ingest.chunk_bytes debe ser > 0")
    if settings.ingest.debounce_seconds < 0:
        problems.append("ingest.debounce_seconds no puede ser negativo")
    if not settings.ingest.include_patterns:
        problems.append("ingest.include_patterns no puede estar vacío")

    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(settings.ingest.default_timezone)
    except Exception as exc:  # noqa: BLE001 - tz puede fallar por muchas razones
        problems.append(f"ingest.default_timezone inválido: {settings.ingest.default_timezone!r} ({exc})")

    if not settings.analysis.rules_path.is_file():
        problems.append(
            f"analysis.rules_path no existe: {settings.analysis.rules_path} "
            "(sin reglas no hay fingerprint ni categoría)"
        )
    if not 0 <= settings.analysis.min_incident_severity <= 100:
        problems.append("analysis.min_incident_severity debe estar en 0..100")
    if settings.analysis.incident_threshold <= 0:
        problems.append("analysis.incident_threshold debe ser > 0")
    if settings.analysis.anomaly_zscore <= 0:
        problems.append("analysis.anomaly_zscore debe ser > 0")

    if problems:
        raise ConfigError("configuración inválida:\n  - " + "\n  - ".join(problems))


_cached: Settings | None = None


def get_settings() -> Settings:
    """Settings compartidos del proceso. Streamlit re-ejecuta el script por cada
    interacción, así que releer YAML + config en cada render sería un desperdicio."""
    global _cached
    if _cached is None:
        _cached = load_settings()
    return _cached