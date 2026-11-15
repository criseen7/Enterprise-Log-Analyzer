"""Lectura incremental de un archivo de log y ensamblado de entradas lógicas.

Dos problemas que resuelve, y que un `readlines()` normal no resuelve:

**1. Una línea a medio escribir.** El archivo está creciendo mientras se lee: la
última línea puede estar incompleta. Consumirla produciría un evento con la mitad
del mensaje, y el offset avanzaría más allá: esa parte del log se perdería para
siempre. Por eso solo se consumen líneas terminadas en `\\n`, y `last_offset` nunca
pasa del inicio de la última línea incompleta.

**2. Entradas multilínea.** Un stack trace de Java ocupa N líneas: `Caused by:`
no empieza con timestamp, así que no matchea ningún patrón. Parseada línea a
línea, se convierte en N eventos basura. Aquí, una línea que no matchea ningún
patrón se concatena a la entrada anterior, formando un solo evento con el trace
completo.

El offset se mide en **bytes**, no en líneas: permite `seek()` directo y es lo que
hace estable la `UNIQUE(file_id, offset)` como clave de idempotencia.

El lector es *streaming*: `iter_entry_batches()` entrega entradas en lotes
acotados por número y por bytes, de modo que ingerir un archivo enorme no exige
tenerlo entero en memoria ni mantener una transacción abierta durante toda la
pasada. Las funciones de conveniencia (`read_complete_lines()`, `iter_entries()`)
siguen devolviendo el archivo completo y conservan su firma para los tests y los
consumidores existentes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from app.parser.registry import Registry

NEWLINE = b"\n"

# Tope de seguridad: una "entrada" de 4 MB es un archivo sin newlines, no un
# stack trace. Sin este límite, un binario colgado en logs/ se carga entero en
# memoria. Lo que exceda se corta y se marca, nunca se descarta en silencio.
MAX_ENTRY_BYTES = 4 * 1024 * 1024
TRUNCATION_NOTE = "\n[entrada truncada: excede el tamaño máximo por entrada]"

# Lote por defecto del pipeline. Se corta por lo que ocurra primero: 1000
# entradas o 8 MiB de texto. Ambos topes existen porque miden cosas distintas:
# mil líneas de 10 bytes y mil líneas de 10 KB son problemas de memoria
# diferentes, y una sola entrada gigante ya está acotada por MAX_ENTRY_BYTES.
DEFAULT_BATCH_ENTRIES = 1000
DEFAULT_BATCH_BYTES = 8 * 1024 * 1024


@dataclass
class RawEntry:
    """Una entrada lógica: una o varias líneas físicas."""

    offset: int
    lines: list[str] = field(default_factory=list)
    truncated: bool = False
    # Offset inmediatamente posterior al `\n` de la última línea de la entrada.
    # Es lo que permite avanzar `last_offset` por lote sin releer ni adelantar
    # una línea incompleta.
    end_offset: int = 0

    @property
    def text(self) -> str:
        body = "\n".join(self.lines)
        return body + TRUNCATION_NOTE if self.truncated else body

    @property
    def is_multiline(self) -> bool:
        return len(self.lines) > 1

    @property
    def size_bytes(self) -> int:
        return sum(len(line.encode("utf-8", errors="replace")) + 1 for line in self.lines)

    def append(self, line: str) -> None:
        self.lines.append(line)


def iter_raw_lines(
    handle: BinaryIO, start_offset: int, chunk_bytes: int = 1_048_576
) -> Iterator[tuple[int, str, int]]:
    """Itera `(offset_inicio, texto, offset_fin)` de cada línea completa.

    Solo emite líneas terminadas en newline: la última línea a medio escribir
    queda en el archivo y no consume offset. El offset final es el byte
    inmediatamente posterior al `\\n`, de modo que el llamador puede avanzar
    `last_offset` exactamente hasta ahí.

    La cabeza de la línea en curso se conserva acotada a `MAX_ENTRY_BYTES`. Un
    archivo binario sin un solo newline no puede consumir memoria sin límite: lo
    que exceda el tope se descarta mientras se busca el newline, y la línea se
    emite truncada cuando aparece.
    """
    handle.seek(start_offset)
    line_start = start_offset
    head = bytearray()
    line_bytes = 0
    oversized = False

    while True:
        chunk = handle.read(chunk_bytes)
        if not chunk:
            break

        cursor = 0
        while cursor < len(chunk):
            cut = chunk.find(NEWLINE, cursor)
            if cut == -1:
                segment = chunk[cursor:]
                line_bytes += len(segment)
                if not oversized:
                    head += segment
                    if len(head) > MAX_ENTRY_BYTES:
                        # Se conserva solo la cabeza; el resto de la línea se
                        # drena sin retenerlo en memoria.
                        oversized = True
                        del head[MAX_ENTRY_BYTES + 1 :]
                cursor = len(chunk)
            else:
                segment = chunk[cursor:cut]
                line_bytes += len(segment)
                if not oversized:
                    head += segment
                if oversized or len(head) > MAX_ENTRY_BYTES:
                    del head[MAX_ENTRY_BYTES + 1 :]
                end_offset = line_start + line_bytes + 1
                yield line_start, _decode(bytes(head)), end_offset
                line_start = end_offset
                head.clear()
                line_bytes = 0
                oversized = False
                cursor = cut + 1


def read_complete_lines(
    path: Path, start_offset: int, chunk_bytes: int = 1_048_576
) -> tuple[list[tuple[int, str]], int]:
    """Lee desde `start_offset`.

    Devuelve `([(byte_offset, texto)], offset_consumido)`. Solo devuelve líneas
    terminadas en newline: si el archivo no acaba en `\\n`, esa última línea queda
    pendiente para la próxima pasada y el offset no avanza sobre ella.

    El offset se calcula sobre bytes, no sobre caracteres, para que un mensaje con
    acentos o emoji no desplace la posición de las líneas siguientes.
    """
    lines: list[tuple[int, str]] = []
    consumed = start_offset
    try:
        with path.open("rb") as handle:
            for offset, text, end_offset in iter_raw_lines(handle, start_offset, chunk_bytes):
                lines.append((offset, text))
                consumed = end_offset
    except FileNotFoundError:
        # El archivo rotó entre el stat y el open. No es un error: el llamador lo
        # trata como "nada que leer" y lo verá con su nueva identidad en la
        # siguiente pasada.
        return [], start_offset
    return lines, consumed


def _decode(raw: bytes) -> str:
    """Decodifica una línea física y descarta el `\\r` de un CRLF."""
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    return raw.decode("utf-8", errors="replace")


def _finalize(entry: RawEntry) -> RawEntry:
    """Aplica el tope por entrada. Se hace al emitir, no al leer, para no perder
    la continuación de un stack trace que solo excede el tope por tamaño."""
    if entry.size_bytes > MAX_ENTRY_BYTES:
        entry.lines = entry.lines[:1]
        entry.truncated = True
    return entry


def assemble(lines: Iterable[tuple[int, str, int]], registry: Registry) -> list[RawEntry]:
    """Agrupa líneas físicas en entradas lógicas (variante no streaming)."""
    return list(_assemble_stream(lines, registry))


def _assemble_stream(
    lines: Iterable[tuple[int, str, int]], registry: Registry
) -> Iterator[RawEntry]:
    """Igual que `assemble`, pero emite cada entrada en cuanto se cierra.

    Solo se pegan las continuaciones si el patrón de la entrada declara
    `multiline: true`. Es una distinción necesaria, no un detalle:

    * Un stack trace de Java (WebLogic, log4j) son N líneas donde solo la
      primera tiene timestamp. Pegarlas produce un evento con el trace completo.
    * Un log JSON es **una línea por registro** por definición. Pegarle una línea
      suelta rompe el JSON y el evento entero se pierde como `parsed_ok=0`.
      Ahí, una línea que no matchea es su propia entrada: se conserva y se
      reporta, en vez de contaminar el registro anterior.
    """
    current: RawEntry | None = None
    multiline = False

    for offset, text, end_offset in lines:
        spec = registry.detect(text)

        if spec is not None:
            if current is not None:
                yield _finalize(current)
            current = RawEntry(offset=offset, lines=[text], end_offset=end_offset)
            multiline = spec.multiline
        elif current is not None and multiline:
            current.append(text)
            current.end_offset = end_offset
        else:
            # Línea sin patrón de apertura y el formato no admite multilínea:
            # entra como evento propio. No se descarta.
            if current is not None:
                yield _finalize(current)
            current = RawEntry(offset=offset, lines=[text], end_offset=end_offset)
            multiline = False

    if current is not None:
        yield _finalize(current)


def iter_entries(
    path: Path,
    start_offset: int,
    registry: Registry,
    chunk_bytes: int = 1_048_576,
) -> tuple[list[RawEntry], int]:
    """Camino completo: leer, filtrar líneas parciales, ensamblar entradas.

    Devuelve `(entradas, offset_consumido)`. El offset devuelto corresponde a la
    última línea **completa**, no al tamaño del archivo: una escritura en curso
    queda intacta para la próxima pasada.
    """
    try:
        with path.open("rb") as handle:
            entries = list(
                _assemble_stream(iter_raw_lines(handle, start_offset, chunk_bytes), registry)
            )
    except FileNotFoundError:
        return [], start_offset
    consumed = entries[-1].end_offset if entries else start_offset
    return entries, consumed


def iter_entry_batches(
    handle: BinaryIO,
    start_offset: int,
    registry: Registry,
    chunk_bytes: int = 1_048_576,
    *,
    max_entries: int = DEFAULT_BATCH_ENTRIES,
    max_bytes: int = DEFAULT_BATCH_BYTES,
) -> Iterator[tuple[list[RawEntry], int]]:
    """Ensambla entradas desde un descriptor ya abierto y las entrega por lotes.

    Cada elemento es `(entradas, offset_fin)`. El lote se corta en un límite de
    entrada (nunca a mitad de un stack trace) y el offset_fin corresponde a la
    última línea completa de ese lote: el llamador puede insertar el lote y
    avanzar `last_offset` con una transacción corta.

    Leer de un descriptor y no de una ruta es lo que elimina el TOCTOU de la
    identidad: el inode usado para resolver la fila es el del mismo archivo que
    se está leyendo, aunque la ruta se haya rotado mientras tanto.
    """
    if max_entries <= 0:
        raise ValueError("max_entries debe ser > 0")
    if max_bytes <= 0:
        raise ValueError("max_bytes debe ser > 0")

    batch: list[RawEntry] = []
    batch_bytes = 0

    for entry in _assemble_stream(iter_raw_lines(handle, start_offset, chunk_bytes), registry):
        batch.append(entry)
        batch_bytes += entry.size_bytes
        if len(batch) >= max_entries or batch_bytes >= max_bytes:
            yield batch, entry.end_offset
            batch = []
            batch_bytes = 0

    if batch:
        yield batch, batch[-1].end_offset
