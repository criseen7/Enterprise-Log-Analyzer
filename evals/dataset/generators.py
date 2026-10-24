"""Generadores deterministas de incidentes sintéticos.

Cada escenario inyecta un mecanismo de fallo distinto y **sabe** qué mensaje,
en qué servicio y en qué instante lo inyectó. De ahí sale el ground truth sin
etiquetado humano.

Dos invariantes que el harness verifica y que conviene tener presentes al leer
este archivo:

* La huella de la causa raíz se calcula con `event_fingerprint` sobre el mensaje
  **tal y como el parser lo va a emitir**, no sobre la plantilla. En Apache, por
  ejemplo, el mensaje es `"GET /api/checkout -> 500"`: la plantilla semántica
  nunca llega a la base.
* Todo se deriva de `seed`. Dos personas con el mismo código obtienen los
  mismos bytes; por eso el dataset puede ir commiteado y verificarse con un hash.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.analysis.errors import ErrorRules
from app.analysis.fingerprint import event_fingerprint

DATASET_VERSION = "synthetic-v1"
SUBSETS = ("smoke", "core", "full")

# Sólo existen estos tres patrones en `app/parser/patterns/`. El plan hablaba de
# syslog, pero el motor no lo soporta: inventar un formato que el parser no lee
# convertiría el eval en una prueba de una ficción.
LOG_FORMATS = ("json", "weblogic", "apachev2")

TZ_OFFSETS = ("+00:00", "-06:00", "+02:00")

# El regex de `weblogic.yaml` exige un mes con nombre y lo normaliza a número
# antes de `strptime`. Se escribe en minúsculas, como en un log real de Java.
_MONTH_NAMES = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


@dataclass(frozen=True)
class Event:
    ts: datetime
    level: str
    service: str
    message: str
    path: str = "/api/app"
    status: int = 500


@dataclass(frozen=True)
class Scenario:
    """Los parámetros de un caso. Inmutable para que no se filtre entre casos."""

    name: str
    semantic_category: str
    service: str
    onset: datetime
    duration: timedelta
    template: str
    services_affected: tuple[str, ...]
    noise_ratio: float
    seed: int
    log_format: str
    tz_offset: str
    path: str

    @property
    def baseline_start(self) -> datetime:
        return self.onset - timedelta(hours=2)

    @property
    def incident_end(self) -> datetime:
        return self.onset + self.duration


# El mensaje que ve el parser en cada formato. Es la única fuente del ground
# truth: si cambia la plantilla, el fingerprint cambia con ella.
def symptom_for(scenario: Scenario) -> str:
    """El síntoma tal y como lo escribiría un operador, con la hora del onset."""
    when = f"{scenario.onset:%H:%M} UTC"
    return _CATALOG[scenario.name]["symptom"].format(when=when)


def root_message(scenario: Scenario) -> str:
    if scenario.log_format == "apachev2":
        return f"GET {scenario.path} -> 500"
    return scenario.template


def ground_truth(scenario: Scenario, rules: ErrorRules | None = None) -> dict[str, Any]:
    """Ground truth calculado con el mismo motor que va a leer los logs."""
    rules = rules or ErrorRules.default()
    message = root_message(scenario)
    category = rules.category(message)
    return {
        "root_cause_fingerprint": event_fingerprint(category, message),
        "root_cause_category": category,
        "root_cause_service": scenario.service,
        "first_bad_event_ts": scenario.onset.isoformat().replace("+00:00", "Z"),
        "affected_services": list(scenario.services_affected),
        "expected_tools": [
            "system_health",
            "top_errors",
            "compare_windows",
            "blast_radius",
        ],
    }


# --- catálogo de escenarios -------------------------------------------------

_CATALOG: dict[str, dict[str, Any]] = {
    "db_pool_exhaustion": {
        "semantic_category": "resource-exhausted",
        "service": "checkout",
        "template": "Pool exhausted: 0 de 20 disponibles en el pool de checkout",
        "services_affected": ("checkout", "payment", "cart"),
        "path": "/api/checkout",
        "must_not_claim": ["disco lleno", "disk full", "deploy", "certificado"],
        "symptom": ("checkout started returning 500s and customers see timeouts since {when}"),
    },
    "bad_deploy": {
        "semantic_category": "null-pointer",
        "service": "checkout",
        "template": (
            "NullPointerException: no se puede invocar getSession() en "
            "OrderService.checkout tras el deploy"
        ),
        "services_affected": ("checkout",),
        "path": "/api/checkout",
        "must_not_claim": ["disco lleno", "disk full", "certificado"],
        "symptom": "checkout returns 500 after the 14:00 deploy since {when}",
    },
    "disk_full": {
        "semantic_category": "storage",
        "service": "order-api",
        "template": "No space left on device: no se pudo escribir /var/log/order/app.log",
        "services_affected": ("order-api", "worker"),
        "path": "/api/orders",
        "must_not_claim": ["deploy", "pool de conexiones", "certificado"],
        "symptom": "order-api stopped writing logs and started failing since {when}",
    },
    "cert_expiry": {
        "semantic_category": "tls",
        "service": "payments",
        "template": ("SSLHandshakeException: certificate expired for api.payments.example.com"),
        "services_affected": ("payments", "checkout"),
        "path": "/api/payments",
        "must_not_claim": ["disco lleno", "disk full", "deploy"],
        "symptom": "payments fail to connect upstream since {when}",
    },
    "traffic_spike": {
        "semantic_category": "capacity",
        "service": "gateway",
        # El error correcto ya existía en el baseline: el incidente es el
        # volumen, no el mensaje. Un agente que confunde frecuencia con causa
        # puede atribuirlo a una regresión de código, y `must_not_claim` lo mide.
        "template": "timeout de consulta",
        "services_affected": ("gateway", "checkout"),
        "path": "/api/search",
        "must_not_claim": ["regression", "regresión", "deploy", "bug de código"],
        "symptom": "latency and 500s rose about 4x at {when}, same error as always",
    },
    "cascading_timeout": {
        "semantic_category": "timeout",
        "service": "inventory",
        "template": "Read timed out after 30000ms calling inventory",
        "services_affected": ("inventory", "checkout", "cart", "orders"),
        "path": "/api/inventory",
        "must_not_claim": ["disco lleno", "disk full", "deploy"],
        "symptom": ("timeouts propagate from inventory to checkout, cart and orders since {when}"),
    },
}

GENERATORS = tuple(_CATALOG)


def must_not_claim(generator: str) -> tuple[str, ...]:
    """Frases que no pueden aparecer en una claim de un caso de este generador."""
    return tuple(_CATALOG[generator]["must_not_claim"])


def build_scenario(
    generator: str,
    *,
    seed: int,
    log_format: str,
    tz_offset: str,
) -> Scenario:
    spec = _CATALOG[generator]
    # Onset determinista pero no siempre a la misma hora: una ventana fija
    # escondería bugs de zona horaria y de buckets de hora.
    onset = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(
        days=seed % 28, hours=8 + seed % 9, minutes=(seed * 7) % 60
    )
    return Scenario(
        name=generator,
        semantic_category=spec["semantic_category"],
        service=spec["service"],
        onset=onset,
        # 60 minutos exactos: la ventana por defecto del agente es la última
        # hora y `compare_windows` usa la hora anterior como baseline. Si el
        # incidente fuera más corto, el baseline incluiría su comienzo y el
        # veredicto saldría "stable"; si fuera más largo, la ventana no lo
        # cubriría. La diversidad del dataset viene del ruido, el formato y la
        # tasa, no de hacer que las ventanas no encajen.
        duration=timedelta(minutes=60),
        template=spec["template"],
        services_affected=tuple(spec["services_affected"]),
        noise_ratio=round(0.2 + 0.1 * (seed % 5), 2),
        seed=seed,
        log_format=log_format,
        tz_offset=tz_offset,
        path=spec["path"],
    )


# --- generación de eventos --------------------------------------------------


def _noise_event(rng: random.Random, ts: datetime) -> Event:
    level, message = rng.choice(
        [
            ("INFO", "heartbeat ok"),
            ("INFO", "cache hit ratio 0.94"),
            ("WARN", "GC pause 120ms"),
            ("INFO", "pedido creado"),
            ("WARN", "reintento 2 de 5"),
            # Ruido de error: existe, pero a una tasa que nunca compite con el
            # incidente. Sin él, "el más frecuente" sería trivialmente correcto.
            ("ERROR", "timeout de consulta"),
        ]
    )
    service = rng.choice(["order-api", "billing-api", "gateway", "worker"])
    return Event(ts=ts, level=level, service=service, message=message)


def generate_events(scenario: Scenario) -> list[Event]:
    """Construye el incidente completo: baseline, ventana y recuperación."""
    rng = random.Random(scenario.seed)
    events: list[Event] = []
    root_interval = 30 + (scenario.seed % 4) * 15
    cascade_interval = 120 + (scenario.seed % 3) * 30
    spike = scenario.name == "traffic_spike"

    # Baseline: dos horas. En `traffic_spike` ya contiene el mismo error, a un
    # cuarto de la tasa, que es lo que hace la comparación de ventanas útil.
    cursor = scenario.baseline_start
    bucket = 0
    while cursor < scenario.onset:
        for _ in range(1 + int(3 * scenario.noise_ratio)):
            events.append(_noise_event(rng, cursor + timedelta(seconds=rng.randint(0, 299))))
        if spike and bucket % 3 == 0:
            events.append(
                Event(
                    ts=cursor + timedelta(seconds=rng.randint(0, 59)),
                    level="ERROR",
                    service=scenario.service,
                    message=scenario.template,
                )
            )
        bucket += 1
        cursor += timedelta(minutes=5)

    # Ventana del incidente.
    cursor = scenario.onset
    while cursor < scenario.incident_end:
        events.append(
            Event(
                ts=cursor + timedelta(seconds=rng.randint(0, 9)),
                level="ERROR",
                service=scenario.service,
                message=scenario.template,
                path=scenario.path,
                status=500,
            )
        )
        for _ in range(1 + int(2 * scenario.noise_ratio)):
            events.append(_noise_event(rng, cursor + timedelta(seconds=rng.randint(0, 59))))
        cursor += timedelta(seconds=root_interval)

    # Cascada: el síntoma se propaga a otros servicios con un mensaje distinto,
    # así que no comparte fingerprint con la causa pero sí aparece en el blast
    # radius.
    cursor = scenario.onset
    while cursor < scenario.incident_end:
        for service in scenario.services_affected:
            if service == scenario.service:
                continue
            events.append(
                Event(
                    ts=cursor + timedelta(seconds=rng.randint(0, 59)),
                    level="ERROR",
                    service=service,
                    message=f"upstream failure: dependency {scenario.service} unavailable",
                    path=scenario.path,
                    status=503,
                )
            )
        cursor += timedelta(seconds=cascade_interval)

    return sorted(events, key=lambda event: event.ts)


# --- renderizado por formato ------------------------------------------------


def _zone(offset: str) -> timezone:
    sign = 1 if offset[0] == "+" else -1
    hours, minutes = int(offset[1:3]), int(offset[4:6])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _local(ts: datetime, offset: str) -> datetime:
    return ts.astimezone(_zone(offset))


def render_json(event: Event, offset: str) -> str:
    local = _local(event.ts, offset)
    payload = {
        "timestamp": local.isoformat(timespec="milliseconds"),
        "level": event.level,
        "service": event.service,
        "logger": f"com.acme.{event.service.replace('-', '.')}.Handler",
        "message": event.message,
    }
    return json.dumps(payload, ensure_ascii=False)


def render_weblogic(event: Event, offset: str) -> str:
    local = _local(event.ts, offset)
    level = {"ERROR": "SEVERE", "WARN": "WARNING", "INFO": "INFO"}[event.level]
    numeric = local.strftime("%z")
    month = _MONTH_NAMES[local.month - 1]
    return (
        f"{local:%d}/{month}/{local:%Y %H:%M:%S}.{local.microsecond // 1000:03d} "
        f"{numeric} com.acme.{event.service.replace('-', '.')}.Handler : [pool-1-thread-1] "
        f"{level} | {event.message}"
    )


def render_apache(event: Event, offset: str) -> str:
    local = _local(event.ts, offset)
    status = event.status if event.level == "ERROR" else (400 if event.level == "WARN" else 200)
    return (
        f"10.0.0.{event.ts.second % 250 + 1} - - "
        f"[{local:%d/%b/%Y:%H:%M:%S} {local:%z}] "
        f'"GET {event.path} HTTP/1.1" {status} 1284 "-" "curl/8.0"'
    )


_RENDERERS = {
    "json": render_json,
    "weblogic": render_weblogic,
    "apachev2": render_apache,
}


def render_logs(scenario: Scenario) -> dict[str, list[str]]:
    """Un archivo por servicio, con las líneas ya formateadas."""
    render = _RENDERERS[scenario.log_format]
    files: dict[str, list[str]] = {}
    for event in generate_events(scenario):
        files.setdefault(f"{event.service}.log", []).append(render(event, scenario.tz_offset))
    return files


def write_logs(scenario: Scenario, directory: str | Path) -> list[Path]:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, lines in render_logs(scenario).items():
        path = out / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(path)
    return sorted(paths)


__all__ = [
    "DATASET_VERSION",
    "Event",
    "GENERATORS",
    "LOG_FORMATS",
    "SUBSETS",
    "Scenario",
    "TZ_OFFSETS",
    "build_scenario",
    "generate_events",
    "ground_truth",
    "must_not_claim",
    "render_logs",
    "root_message",
    "symptom_for",
    "write_logs",
]
