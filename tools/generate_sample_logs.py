"""Genera logs de ejemplo en `logs/` para poder probar la ingesta.

    python3 tools/generate_sample_logs.py [--out logs] [--lines 500] [--seed 7]

Cubre los tres formatos que el registry soporta, incluyendo los casos que rompen
un lector ingenuo:

* stack traces multilínea (WebLogic),
* líneas con acentos y emoji, para que el offset en bytes se pruebe de verdad,
* rotaciones con rename,
* una línea a propósito sin parsear, para que la cobertura no sea 100% por
  casualidad.

No es fixture de test: es material de demo. Los tests usan `tests/fixtures/`.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVICES = ["order-api", "billing-api", "gateway", "worker"]
LEVELS = ["INFO", "INFO", "INFO", "WARN", "ERROR"]
APACHE_PATHS = [
    "/api/orders",
    "/api/orders/4821",
    "/health",
    "/static/js/app.4f2c9.js",
    "/api/search?q=café ñoño",
]
JAVA_STACK = [
    "java.lang.NullPointerException: no se puede invocar getSession()",
    "\tat com.acme.order.OrderService.checkout(OrderService.java:142)",
    "\tat com.acme.api.OrderController.post(OrderController.java:57)",
    "\tat org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerAdapter.invokeHandlerMethod(RequestingHandlerAdapter.java:873)",
    "Caused by: java.sql.SQLException: connection timeout after 30000ms",
    "\tat oracle.jdbc.driver.OracleDriver.connect(OracleDriver.java:615)",
    "\tat weblogic.jdbc.common.internal.ConnectionPoolFactory.createPoolConnection(ConnectionPoolFactory.java:928)",
    "\t... 18 more",
]


def _timestamps(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(seconds=i * random.randint(1, 9)) for i in range(count)]


def apache_line(now: datetime) -> str:
    status = random.choices([200, 201, 304, 404, 500, 503], [50, 8, 10, 12, 6, 4])[0]
    remote = f"10.0.{random.randint(0, 3)}.{random.randint(2, 254)}"
    zone = now.astimezone()
    return (
        f'{remote} - - [{zone:%d/%b/%Y:%H:%M:%S %z}] '
        f'"GET {random.choice(APACHE_PATHS)} HTTP/1.1" {status} {random.randint(120, 9000)} '
        f'"https://app.example.com/" "Mozilla/5.0 (X11; Linux x86_64)"'
    )


def json_line(now: datetime) -> str:
    return json.dumps(
        {
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": random.choice(LEVELS),
            "service": random.choice(SERVICES),
            "logger": "com.acme.order.OrderService",
            "traceId": f"{random.getrandbits(64):016x}",
            "message": random.choice(
                [
                    "pedido creado",
                    "pago rechazado por el banco",
                    "reintento 2 de 5",
                    "caché invalidada",
                    "café ñoño 🚀 mensaje con emoji",
                ]
            ),
            "durationMs": random.randint(4, 4000),
        },
        ensure_ascii=False,
    )


def weblogic_line(now: datetime) -> str:
    return (
        f"{now:%d/%b/%Y} {now:%H:%M:%S}.{now.microsecond // 1000:03d} CEST "
        f"com.acme.order.OrderDao : [pool-{random.randint(1, 4)}-thread-{random.randint(1, 8)}] "
        f"{random.choice(['SEVERE', 'WARNING', 'INFO'])} | "
        f"{random.choice(['Pool exhausted: 0 de 20 disponibles', 'transacción revertida', 'timeout de consulta'])}"
    )


def build(out: Path, lines: int, seed: int) -> list[Path]:
    random.seed(seed)
    out.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc) - timedelta(minutes=lines // 40)

    handle = (out / "app.log").open("w", encoding="utf-8")

    for i, moment in enumerate(_timestamps(start, lines)):
        kind = random.choices(["apache", "json", "weblogic"], [40, 45, 15])[0]
        if kind == "apache":
            handle.write(apache_line(moment) + "\n")
        elif kind == "json":
            handle.write(json_line(moment) + "\n")
        else:
            handle.write(weblogic_line(moment) + "\n")
            # Stack trace: solo aparece en los errores, y siempre cuesta abajo.
            if random.random() < 0.18:
                for frame in JAVA_STACK:
                    handle.write(frame + "\n")

    # Una línea que ningún patrón reconoce: la cobertura no puede ser 100%.
    handle.write("[runtime] arranque del proceso, línea sin formato de log\n")
    handle.close()

    # Rotación simulada por rename: app.log -> app.log.1, y luego un archivo
    # nuevo. Es el caso que un esquema con `path` UNIQUE no sabe manejar.
    rotado = out / "app.log.1"
    shutil.copyfile(out / "app.log", rotado)

    nuevo = out / "app.log"
    with nuevo.open("w", encoding="utf-8") as fh:
        for moment in _timestamps(datetime.now(timezone.utc), 40):
            fh.write(json_line(moment) + "\n")

    return [nuevo, rotado]


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera logs de ejemplo para demo.")
    parser.add_argument("--out", default="logs", type=Path)
    parser.add_argument("--lines", default=500, type=int)
    parser.add_argument("--seed", default=7, type=int)
    args = parser.parse_args()

    rutas = build(args.out, args.lines, args.seed)
    for ruta in rutas:
        size = ruta.stat().st_size
        print(f"{ruta}  {size:,} bytes  {size / 1024:.1f} KB")
    print(f"\n{args.lines} líneas generadas en {args.out}/")
    print("Siguiente: python3 -m app.ingest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
