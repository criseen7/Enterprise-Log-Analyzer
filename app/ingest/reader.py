"""Lectura incremental de un archivo de log y ensamblado de entradas lógicas.

Dos problemas que resuelve, y que un `readlines()` normal no resuelve:

**1. Una línea a medio escribir.** El archivo está creciendo mientras se lee: la
última línea puede estar incompleta. Consumirla produciría un evento con la mitad
del mensaje, y el offset avanzaría más allá: esa parte del log se perdería para
siempre. Por eso solo se consumen líneas terminadas en `\n`, y `last_offset` nunca
pasa del inicio de la última línea incompleta.

**2. Entradas multilínea.** Un stack trace de Java ocupa N líneas: `Caused by:`
no empieza con timestamp, así que no matchea ningún patrón. Parseada línea a
línea, se convierte en N eventos basura. Aquí, una línea que no matchea ningún
patrón se concatena a la entrada anterior, formando un solo evento con el trace
completo.

El offset se mide en **bytes**, no en líneas: permite `seek()` directo y es lo que
hace estable la `UNIQUE(file_id, offset)` como clave de idempotencia.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.parser.registry import Registry

NEWLINE = b"\n"

# Tope de seguridad: una "entrada" de 4 MB es un archivo sin newlines, no un
# stack trace. Sin este límite, un binario colgado en logs/ se carga entero en
# memoria. Lo que exceda se corta y se marca, nunca se descarta en silencio.
MAX_ENTRY_BYTES = 4 * 1024 * 1024
TRUNCATION_NOTE = "\n[entrada truncada: excede el tamaño máximo por entrada]"


@dataclass
class RawEntry:
    """Una entrada lógica: una o varias líneas físicas."""

    offset: int
    lines: list[str] = field(default_factory=list)
    truncated: bool = False

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


def read_complete_lines(
    path: Path, start_offset: int, chunk_bytes: int = 1_048_576
) -> tuple[list[tuple[int, str]], int]:
    """Lee desde `start_offset`.

    Devuelve `([(byte_offset, texto)], offset_consumido)`. Solo devuelve líneas
    terminadas en newline: si el archivo no acaba en `\\n`, esa última línea queda
    pendiente para la próxima pasada y el offset no avanza sobre ella.

    El offset se calcula sobre bytes, no sobre caracteres, para que un mensaje con
    acentos o emoji no desplace la posición de las líneas siguientes.

    La cabeza de la línea en curso se conserva acotada a `MAX_ENTRY_BYTES`. Un
    archivo binario sin un solo newline no puede consumir memoria sin límite: lo
    que exceda el tope se descarta mientras se busca el newline, y la línea se
    emite truncada cuando aparece. El offset solo avanza sobre líneas completas.
    """
    lines: list[tuple[int, str]] = []
    line_start = start_offset
    head = b""
    line_bytes = 0
    oversized = False

    try:
        with path.open("rb") as handle:
            handle.seek(start_offset)
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
                                # Se conserva solo la cabeza; el resto de la
                                # línea se drena sin retenerlo en memoria.
                                oversized = True
                                head = head[: MAX_ENTRY_BYTES + 1]
                        cursor = len(chunk)
                    else:
                        segment = chunk[cursor:cut]
                        line_bytes += len(segment)
                        if not oversized:
                            head += segment
                        if oversized or len(head) > MAX_ENTRY_BYTES:
                            head = head[: MAX_ENTRY_BYTES + 1]
                        lines.append((line_start, _decode(head)))
                        line_start += line_bytes + 1
                        head = b""
                        line_bytes = 0
                        oversized = False
                        cursor = cut + 1

    except FileNotFoundError:
        # El archivo rotó entre el stat y el open. No es un error: el llamador lo
        # trata como "nada que leer" y lo verá con su nueva identidad en la
        # siguiente pasada.
        return [], start_offset

    return lines, line_start


def _decode(raw: bytes) -> str:
    """Decodifica una línea física y descarta el `\\r` de un CRLF."""
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    return raw.decode("utf-8", errors="replace")


def _split(buffer: bytes, base: int) -> list[tuple[int, str]]:
    """Parte por `\\n` estrictamente, con el offset real de cada línea.

    No se usa `bytes.splitlines()`: también parte por \\v, \\f, \\x1c y otros
    separadores Unicode, lo que generaría líneas con offsets que no corresponden
    a ningún salto real del archivo.

    El offset avanza por la longitud **física** de la línea, incluido el `\\r` de
    un CRLF. Contar solo el contenido desplazaba todas las líneas siguientes en un
    byte por cada CRLF.
    """
    out: list[tuple[int, str]] = []
    position = base
    for raw in buffer.split(NEWLINE)[:-1]:  # el último elemento va sin cerrar
        out.append((position, _decode(raw)))
        position += len(raw) + 1
    return out


def assemble(lines: list[tuple[int, str]], registry: Registry) -> list[RawEntry]:
    """Agrupa líneas físicas en entradas lógicas.

    Solo se pegan las continuaciones si el patrón de la entrada declara
    `multiline: true`. Es una distinción necesaria, no un detalle:

    * Un stack trace de Java (WebLogic, log4j) son N líneas donde solo la
      primera tiene timestamp. Pegarlas produce un evento con el trace completo.
    * Un log JSON es **una línea por registro** por definición. Pegarle una línea
      suelta rompe el JSON y el evento entero se pierde como `parsed_ok=0`.
      Ahí, una línea que no matchea es su propia entrada: se conserva y se
      reporta, en vez de contaminar el registro anterior.
    """
    entries: list[RawEntry] = []
    current: RawEntry | None = None
    multiline = False

    for offset, text in lines:
        spec = registry.detect(text)

        if spec is not None:
            if current is not None:
                entries.append(current)
            current = RawEntry(offset=offset, lines=[text])
            multiline = spec.multiline
        elif current is not None and multiline:
            current.append(text)
        else:
            # Línea sin patrón de apertura y el formato no admite multilínea:
            # entra como evento propio. No se descarta.
            if current is not None:
                entries.append(current)
            current = RawEntry(offset=offset, lines=[text])
            multiline = False

    if current is not None:
        entries.append(current)
    return entries


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
    raw_lines, new_offset = read_complete_lines(path, start_offset, chunk_bytes)
    entries: list[RawEntry] = []
    for entry in assemble(raw_lines, registry):
        if entry.size_bytes > MAX_ENTRY_BYTES:
            entry.lines = entry.lines[:1]
            entry.truncated = True
        entries.append(entry)
    return entries, new_offset
