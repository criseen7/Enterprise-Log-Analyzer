"""Apertura de ficheros sensibles sin seguir enlaces simbólicos.

Dos usos, y ambos comparten el mismo riesgo: el secreto del HMAC HITL y las
trazas del agente viven en disco, y una ruta puede ser sustituida por un symlink
antes de que el proceso la abra. Abrir con `O_NOFOLLOW` y comprobar con `fstat`
convierte ese ataque en un error explícito en vez de una escritura sobre otro
fichero.

Los permisos se fijan sobre el descriptor (`fchmod`), no sobre la ruta: entre el
`open` y el `chmod` la ruta puede cambiar de dueño.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_SYMLINK_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK})


def ensure_private_dir(path: Path) -> None:
    """Crea el directorio si falta y lo deja en 0700.

    El `chmod` es best-effort: en sistemas sin permisos POSIX (o sobre un bind
    mount que no los conserva) no hay nada que endurecer.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _chmod(path, 0o700)


def open_read_no_follow(path: Path) -> int:
    """Abre un fichero regular para lectura sin seguir symlinks."""
    try:
        descriptor = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise ValueError(f"{path}: no se sigue un enlace simbólico") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{path}: no es un fichero regular")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_private(path: Path) -> bytes:
    """Lee un fichero sensible y lo deja en 0600."""
    descriptor = open_read_no_follow(path)
    try:
        _fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def open_private_for_write(path: Path) -> int:
    """Abre (crea o trunca) un fichero sensible en 0600 sin seguir symlinks."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise ValueError(f"{path}: no se sigue un enlace simbólico") from exc
        raise
    _fchmod(descriptor, 0o600)
    return descriptor


def _fchmod(descriptor: int, mode: int) -> None:
    try:
        os.fchmod(descriptor, mode)
    except (AttributeError, NotImplementedError, OSError):
        pass


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except (NotImplementedError, OSError):
        pass


__all__ = [
    "ensure_private_dir",
    "open_private_for_write",
    "open_read_no_follow",
    "read_private",
]
