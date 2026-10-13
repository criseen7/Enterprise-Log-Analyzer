"""Vigilancia de `log_dir` con debounce.

Dos problemas que este módulo resuelve y que un `FileSystemEventHandler` solo
resuelve a medias:

**1. Eventos en ráfaga.** Un proceso que escribe una línea cada 50 ms genera un
evento por línea. Procesar la ingesta en cada uno es I/O por evento, y además
lee a medio escribir. Por eso todo lo que llega se acumula en una ventana de
calma (`debounce_seconds`) y solo cuando deja de llegarse se procesa el lote.

**2. Sin `watchdog` debe seguir funcionando.** No todos los entornos pueden
instalarlo (sin red, sin pip, Windows/WSL con permisos distintos). El polling es
más lento pero correcto y es el camino por defecto: una herramienta cuyo
comportamiento cambia según si hay una dependencia opcional instalada no es
reproducible.

Nada de esto hace la ingesta idempotente — eso ya lo garantiza
`UNIQUE (file_id, offset)`. El watcher solo decide *cuándo* dispara. Si se dispara
dos veces por la misma escritura, la segunda cuesta un conteo de líneas, no un
duplicado.
"""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.analysis.errors import ErrorRules
from app.ingest.ingestor import discover, ingest_paths_transactional


def _resolve_rules(config, rules: ErrorRules | None) -> ErrorRules:
    """Reglas efectivas de esta pasada.

    Se resuelve aquí, una sola vez por arranque, y no dentro de la bucle de
    polling: releer el YAML en cada ciclo haría que los eventos de una misma
    corrida pudieran clasificarse con reglas distintas si alguien edita el
    archivo mientras el watcher corre.
    """
    return rules if rules is not None else ErrorRules.load(config.analysis.rules_path)


@dataclass
class WatchState:
    """Ventana de calma: acumula paths y marca cuando toca procesar."""

    debounce_seconds: float
    pending: set[Path] = field(default_factory=set)
    last_event: float = 0.0

    def add(self, paths: Iterable[Path]) -> bool:
        """Añade paths. Devuelve True si la ventana ya venció y hay lote listo."""
        nuevos = {Path(p) for p in paths}
        if nuevos:
            self.pending |= nuevos
            self.last_event = time.monotonic()
        return self.ready()

    def ready(self) -> bool:
        """True solo si llegaron paths y ya no se está recibiendo ninguno nuevo."""
        if not self.pending:
            return False
        return (time.monotonic() - self.last_event) >= self.debounce_seconds

    def take(self) -> set[Path]:
        """Vacía la ventana y devuelve el lote a procesar."""
        out, self.pending = self.pending, set()
        return out


def ingest_due(
    config,
    conn,
    registry,
    state: WatchState | None = None,
    rules: ErrorRules | None = None,
) -> int:
    """Procesa la ventana si venció. Devuelve el nº de corridas (0 o 1)."""
    if state is None:
        state = WatchState(config.ingest.debounce_seconds)
    if not state.ready():
        return 0
    rutas = state.take()
    if not rutas:
        return 0
    ingest_paths_transactional(
        conn, registry, rutas, config.ingest.chunk_bytes, _resolve_rules(config, rules)
    )
    return 1


def run_polling(
    config,
    conn,
    registry,
    *,
    max_cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_cycle: Callable[[int], None] | None = None,
    rules: ErrorRules | None = None,
) -> int:
    """Bucle de polling: comprueba `log_dir` y ingerí lo que creció.

    El intervalo del propio polling ya actúa como debounce, así que aquí no se
    acumula nada. `max_cycles` y `sleep` son inyectables para poder testear sin
    dormir reales; en producción queda `None`.
    """
    efectivas = _resolve_rules(config, rules)
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        rutas = {p for p in discover(config) if p.is_file()}
        if rutas:
            ingest_paths_transactional(
                conn, registry, rutas, config.ingest.chunk_bytes, efectivas
            )
            cycles += 1
            if on_cycle:
                on_cycle(cycles)
        if max_cycles is None or cycles < max_cycles:
            sleep(config.ingest.poll_interval_seconds)
    return cycles


def _watch_with_watchdog(config, conn, registry, on_cycle, rules) -> int:  # noqa: ANN001
    """Camino con watchdog. Devuelve solo al recibir Ctrl-C."""
    from watchdog.events import FileSystemEventHandler  # type: ignore[import-untyped]
    from watchdog.observers import Observer  # type: ignore[import-untyped]

    state = WatchState(config.ingest.debounce_seconds)

    class Handler(FileSystemEventHandler):  # type: ignore[misc]
        def on_any_event(self, event):  # noqa: ANN001 - API de watchdog
            if getattr(event, "is_directory", False):
                return

            log_dir = config.paths.log_dir.resolve()
            allowed_names = set()
            for pattern in config.ingest.include_patterns:
                try:
                    allowed_names.update(p.name for p in log_dir.glob(pattern) if p.is_file())
                except Exception:
                    pass

            def _valid(raw: str | None) -> Path | None:
                if not raw:
                    return None
                try:
                    p = Path(raw).resolve()
                except Exception:
                    return None
                try:
                    if not p.is_relative_to(log_dir):
                        return None
                except AttributeError:
                    try:
                        p.relative_to(log_dir)
                    except Exception:
                        return None
                if p.name not in allowed_names:
                    try:
                        if not any(fnmatch.fnmatchcase(p.name, pat) for pat in config.ingest.include_patterns):
                            return None
                    except Exception:
                        pass
                return p

            src = _valid(getattr(event, "src_path", None))
            dest = _valid(getattr(event, "dest_path", None))
            # `moved` llega como src (origen) y dest (destino): se necesitan
            # ambos, porque el rename deja la identidad en el inode y la ruta
            # vieja ya no existe.
            candidatos = [p for p in (src, dest) if p]
            if not candidatos:
                return
            state.add(candidatos)

    observer = Observer()
    observer.schedule(Handler(), str(config.paths.log_dir), recursive=False)
    observer.start()

    cycles = 0
    try:
        while True:
            if state.ready():
                rutas = state.take()
                if rutas:
                    ingest_paths_transactional(
                        conn, registry, rutas, config.ingest.chunk_bytes, rules
                    )
                    cycles += 1
                    if on_cycle:
                        on_cycle(cycles)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
    return cycles


def _watch_polling(config, conn, registry, on_cycle, rules) -> int:  # noqa: ANN001
    """Fallback sin watchdog. Igual de correcto, solo más lento en detectar cambios."""
    cycles = 0
    try:
        while True:
            rutas = {p for p in discover(config) if p.is_file()}
            if rutas:
                ingest_paths_transactional(
                    conn, registry, rutas, config.ingest.chunk_bytes, rules
                )
                cycles += 1
                if on_cycle:
                    on_cycle(cycles)
            time.sleep(config.ingest.poll_interval_seconds)
    except KeyboardInterrupt:
        pass
    return cycles


def watch(
    config,
    conn,
    registry,
    on_cycle: Callable[[int], None] | None = None,
    rules: ErrorRules | None = None,
) -> int:
    """Bucle principal: watchdog si está disponible, polling si no.

    La elección es transparente para el resto del sistema: las dos rutas llaman a
    la misma `ingest_paths_transactional`, que es donde está la idempotencia.
    """
    efectivas = _resolve_rules(config, rules)
    try:
        from watchdog.observers import Observer  # noqa: F401
    except Exception:  # noqa: BLE001 - dependencia opcional, no control de flujo
        return _watch_polling(config, conn, registry, on_cycle, efectivas)
    return _watch_with_watchdog(config, conn, registry, on_cycle, efectivas)


__all__ = [
    "WatchState",
    "ingest_due",
    "run_polling",
    "watch",
]
