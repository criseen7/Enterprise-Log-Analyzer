"""Genera `docs/demo.gif` a partir de una transcripción real.

No es una grabación de pantalla: es un *replay* de la salida que produjo
`loglens investigate --no-llm` sobre logs sintéticos, renderizado con Pillow.
Se hace así porque el GIF tiene que ser reproducible desde el repositorio y no
depender de que alguien grabe su terminal.

    python tools/make_demo_gif.py [--transcript docs/demo-transcript.txt]

El texto se envuelve a 118 columnas y se colorea por prefijo. Si cambia el
formato de la salida, se actualiza la transcripción y se vuelve a ejecutar.
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1160, 880
PAD = 28
LINE_HEIGHT = 19
FONT_SIZE = 13
COLUMNS = 128

BG = (13, 17, 23)
CHROME = (22, 27, 34)
TEXT = (201, 209, 217)
DIM = (110, 118, 129)
GREEN = (63, 185, 80)
CYAN = (86, 182, 194)
YELLOW = (210, 153, 34)
RED = (248, 81, 73)
BOLD = (240, 246, 252)

FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
BOLD_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")


def _color_for(line: str) -> tuple[int, int, int]:
    stripped = line.lstrip()
    if stripped.startswith("$"):
        return GREEN
    if stripped.startswith("!"):
        return YELLOW
    if stripped.startswith("→"):
        return CYAN
    if stripped.startswith("Citas:"):
        return GREEN
    if stripped.startswith("LogLens —"):
        return BOLD
    if stripped.startswith("run ") or stripped.startswith("Traza"):
        return DIM
    if any(
        stripped.startswith(label)
        for label in ("estado", "confianza", "causa", "huella", "servicios", "Coste")
    ):
        return TEXT
    return TEXT


def _wrap(lines: list[str]) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        if len(line) <= COLUMNS:
            wrapped.append(line)
            continue
        indent = " " * (len(line) - len(line.lstrip()) + 2)
        chunks = textwrap.wrap(
            line, width=COLUMNS, subsequent_indent=indent, break_long_words=False
        )
        wrapped.extend(chunks or [""])
    return wrapped


def _frame(lines: list[str], font: ImageFont.FreeTypeFont, command: str | None) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, WIDTH, 46), radius=10, fill=CHROME)
    draw.rectangle((0, 30, WIDTH, 46), fill=CHROME)
    for index, color in enumerate(((248, 81, 73), (210, 153, 34), (63, 185, 80))):
        draw.ellipse((22 + index * 22, 16, 34 + index * 22, 28), fill=color)
    draw.text((100, 14), "loglens — investigate", font=font, fill=DIM)

    y = 72
    if command is not None:
        draw.text((PAD, y), "$ ", font=font, fill=GREEN)
        draw.text((PAD + 2 * 7.8, y), command, font=font, fill=BOLD)
        y += LINE_HEIGHT * 2
    for line in lines:
        if y > HEIGHT - PAD:
            break
        draw.text((PAD, y), line, font=font, fill=_color_for(line))
        y += LINE_HEIGHT
    return image


def build(transcript: Path, output: Path) -> int:
    font = ImageFont.truetype(str(FONT_PATH), FONT_SIZE)
    bold = ImageFont.truetype(str(BOLD_PATH), FONT_SIZE)
    raw = transcript.read_text(encoding="utf-8").splitlines()
    command = raw[0].removeprefix("$ loglens ").strip()
    body = _wrap(raw[1:])

    frames: list[Image.Image] = []
    # 1) Se escribe el comando.
    for length in range(0, len(command) + 1, 3):
        frames.append(_frame([], bold, command[:length]))
    frames.append(_frame([], bold, command))
    # 2) Aparece la salida de dos líneas en dos líneas.
    for count in range(2, len(body) + 1, 2):
        frames.append(_frame(body[:count], font, command))
    if len(body) % 2:
        frames.append(_frame(body, font, command))
    # 3) Pausa final para poder leer.
    for _ in range(45):
        frames.append(_frame(body, font, command))

    output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        output,
        save_all=True,
        append_images=frames[1:],
        duration=70,
        loop=0,
        optimize=True,
    )
    return len(frames)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera el GIF de demo desde una transcripción.")
    parser.add_argument("--transcript", type=Path, default=Path("docs/demo-transcript.txt"))
    parser.add_argument("--out", type=Path, default=Path("docs/demo.gif"))
    args = parser.parse_args(argv)
    frames = build(args.transcript, args.out)
    size = args.out.stat().st_size
    print(f"{args.out}: {frames} frames, {size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
