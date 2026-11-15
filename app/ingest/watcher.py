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

Hay dos límites que no son obvios y que sí importan en operación:

* **Deadline.** Un log que nunca deja de crecer mantiene la ventana de calma
  abierta para siempre. `max_latency_seconds` fuerza el procesamiento de lo
  acumulado aunque sigan llegando eventos; el debounce no puede convertirse en
  inanición.
* **Barrido inicial y rescaneo.** `watchdog` solo informa de cambios posteriores
  a `start()`. Los archivos que ya existían al arrancar no generarían ningún
  evento, y un evento perdido (cola desbordada, FS de red) tampoco. El barrido
  inicial y el rescaneo periódico usan `discover()`, la misma fuente de verdad
  que la ingesta por polling.

Nada de esto hace la ingesta idempotente — eso ya lo garantiza
`UNIQUE (file_id, offset)`. El watcher solo decide *cuándo* dispara. Si se dispara
dos veces por la misma escritura, la segunda cuesta un conteo de líneas, no un
duplicado.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.analysis.errors import ErrorRules
from app.ingest.ingestor import discover, ingest_paths_transactional


def _resolve_rules(config, rules: ErrorRules | None) -> ErrorRules:
    """Reglas efectivas de esta pasada.

    Se resuelve aquí, una sola vez por arranque, y no dentro del bucle de
    polling: releer el YAML en cada ciclo haría que los eventos de una misma
    corrida pudieran clasificarse con reglas distintas si alguien edita el
    archivo mientras el watcher corre.
    """
    return rules if rules is not None else ErrorRules.load(config.analysis.rules_path)


@dataclass
class WatchState:
    """Ventana de calma con deadline: acumula paths y marca cuándo procesar."""

    debounce_seconds: float
    max_latency_seconds: float = 30.0
    pending: set[Path] = field(default_factory=set)
    last_event: float = 0.0
    window_started: float = 0.0

    def add(self, paths: Iterable[Path]) -> bool:
        """Añade paths. Devuelve True si la ventana ya venció y hay lote listo."""
        nuevos = {Path(p) for p in paths}
        if nuevos:
            now = time.monotonic()
            if not self.pending:
                self.window_started = now
            self.pending |= nuevos
            self.last_event = now
        return self.ready()

    def ready(self) -> bool:
        """True si hay lote y venció la calma **o** el deadline de latencia."""
        if not self.pending:
            return False
        now = time.monotonic()
        if (now - self.last_event) >= self.debounce_seconds:
            return True
        return (now - self.window_started) >= self.max_latency_seconds

    def take(self) -> set[Path]:
        """Vacía la ventana y devuelve el lote a procesar."""
        out, self.pending = self.pending, set()
        self.window_started = 0.0
        return out


def _ingest_paths(
    config,
    conn,
    registry,
    rules: ErrorRules,
    paths: Iterable[Path],
    on_cycle: Callable[[int], None] | None,
    cycles: int,
    on_error: Callable[[BaseException], None] | None,
) -> int:
    """Ingesta tolerante a fallos: un error no debe matar el watcher.

    Un archivo con permisos raros o un `database is locked` transitorio no
    justifica que el proceso deje de vigilar para siempre. Se informa y se
    sigue; la siguiente pasada reintenta.
    """
    pendientes = set(paths)
    if not pendientes:
        return cycles
    try:
        ingest_paths_transactional(conn, registry, pendientes, config.ingest.chunk_bytes, rules)
    except Exception as exc:  # noqa: BLE001 - el watcher sobrevive y reintenta
        if on_error is not None:
            on_error(exc)
        else:
            print(f"ingesta fallida: {type(exc).__name__}: {exc}", file=sys.stderr)
        return cycles
    cycles += 1
    if on_cycle is not None:
        on_cycle(cycles)
    return cycles


def _current_paths(config) -> set[Path]:
    try:
        return {p for p in discover(config) if p.is_file()}
    except OSError:
        return set()


def ingest_due(
    config,
    conn,
    registry,
    state: WatchState | None = None,
    rules: ErrorRules | None = None,
) -> int:
    """Procesa la ventana si venció. Devuelve el nº de corridas (0 o 1)."""
    if state is None:
        state = WatchState(config.ingest.debounce_seconds, config.ingest.max_latency_seconds)
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
    on_error: Callable[[BaseException], None] | None = None,
    rules: ErrorRules | None = None,
) -> int:
    """Bucle de polling: comprueba `log_dir` y ingiere lo que creció.

    El intervalo del propio polling ya actúa como debounce, así que aquí no se
    acumula nada. `max_cycles` y `sleep` son inyectables para poder testear sin
    dormir reales; en producción queda `None`.
    """
    efectivas = _resolve_rules(config, rules)
    cycles = 0
    iterations = 0
    while max_cycles is None or iterations < max_cycles:
        cycles = _ingest_paths(
            config, conn, registry, efectivas, _current_paths(config), on_cycle, cycles, on_error
        )
        iterations += 1
        if max_cycles is None or iterations < max_cycles:
            sleep(config.ingest.poll_interval_seconds)
    return cycles


def _known_files(config) -> set[Path]:
    """Archivos que `discover()`/`Settings.log_files` considera ingeribles.

    Es la fuente de verdad para el watcher: `Settings.log_files` aplica el mismo
    glob, el mismo `is_file()` y la misma resolución que la ingesta por polling.
    """
    return set(config.log_files)


def _match_known(raw: str | None, conocidos: set[Path]) -> Path | None:
    """Ruta resuelta si está en `conocidos`; `None` si no aplica.

    Validar por nombre base divergía de `discover()` con patrones que llevan
    directorio (p. ej. `arch/*.log`) y con enlaces simbólicos.
    """
    if not raw:
        return None
    try:
        p = Path(raw).resolve()
    except OSError:
        return None
    return p if p in conocidos else None


def _watch_with_watchdog(
    config,
    conn,
    registry,
    on_cycle,
    rules,
    *,
    on_error: Callable[[BaseException], None] | None = None,
    max_cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    initial_sweep: bool = True,
) -> int:  # noqa: ANN001
    """Camino con watchdog. Devuelve solo al recibir Ctrl-C (o al llegar a max_cycles).

    El barrido inicial y el rescaneo periódico existen porque `watchdog` solo
    entrega cambios posteriores a `start()`: sin ellos, arrancar con logs ya
    escritos no ingería nada hasta la siguiente modificación.
    """
    from watchdog.events import FileSystemEventHandler  # type: ignore[import-untyped]
    from watchdog.observers import Observer  # type: ignore[import-untyped]

    state = WatchState(config.ingest.debounce_seconds, config.ingest.max_latency_seconds)
    cycles = 0

    if initial_sweep:
        cycles = _ingest_paths(
            config, conn, registry, rules, _current_paths(config), on_cycle, cycles, on_error
        )

    class Handler(FileSystemEventHandler):  # type: ignore[misc]
        def on_any_event(self, event):  # noqa: ANN001 - API de watchdog
            if getattr(event, "is_directory", False):
                return

            try:
                conocidos = _known_files(config)
            except Exception:
                return

            src = _match_known(getattr(event, "src_path", None), conocidos)
            dest = _match_known(getattr(event, "dest_path", None), conocidos)
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

    next_rescan = time.monotonic() + config.ingest.poll_interval_seconds
    iterations = 0
    try:
        while max_cycles is None or iterations < max_cycles:
            if state.ready():
                rutas = state.take()
                if rutas:
                    cycles = _ingest_paths(
                        config, conn, registry, rules, rutas, on_cycle, cycles, on_error
                    )

            now = time.monotonic()
            if now >= next_rescan:
                # Red de seguridad: ningún evento perdido ni archivo preexistente
                # depende de que watchdog lo haya visto.
                cycles = _ingest_paths(
                    config,
                    conn,
                    registry,
                    rules,
                    _current_paths(config),
                    on_cycle,
                    cycles,
                    on_error,
                )
                next_rescan = now + config.ingest.poll_interval_seconds

            iterations += 1
            if max_cycles is not None and iterations >= max_cycles:
                break
            sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
    return cycles


def _watch_polling(
    config,
    conn,
    registry,
    on_cycle,
    rules,
    *,
    on_error: Callable[[BaseException], None] | None = None,
    max_cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:  # noqa: ANN001
    """Fallback sin watchdog. Igual de correcto, solo más lento en detectar cambios."""
    cycles = 0
    iterations = 0
    try:
        while max_cycles is None or iterations < max_cycles:
            cycles = _ingest_paths(
                config,
                conn,
                registry,
                rules,
                _current_paths(config),
                on_cycle,
                cycles,
                on_error,
            )
            iterations += 1
            if max_cycles is None or iterations < max_cycles:
                sleep(config.ingest.poll_interval_seconds)
    except KeyboardInterrupt:
        pass
    return cycles


def watch(
    config,
    conn,
    registry,
    on_cycle: Callable[[int], None] | None = None,
    rules: ErrorRules | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> int:
    """Bucle principal: watchdog si está disponible, polling si no.

    La elección es transparente para el resto del sistema: las dos rutas llaman a
    la misma `ingest_paths_transactional`, que es donde está la idempotencia.
    """
    efectivas = _resolve_rules(config, rules)
    try:
        from watchdog.observers import Observer  # noqa: F401
    except Exception:  # noqa: BLE001 - dependencia opcional, no control de flujo
        return _watch_polling(config, conn, registry, on_cycle, efectivas, on_error=on_error)
    return _watch_with_watchdog(config, conn, registry, on_cycle, efectivas, on_error=on_error)


__all__ = [
    "WatchState",
    "ingest_due",
    "run_polling",
    "watch",
]
