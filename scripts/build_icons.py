# SPDX-License-Identifier: Apache-2.0
"""Build the eda-agent app icon from its SVG sources.

Sources live in ``assets/icon``: ``eda-agent.svg`` is the master drawing
used for the large sizes, and ``eda-agent-<n>.svg`` are hand-hinted
redraws for the small sizes (16, 20, 24, 32 px), where every edge sits on
the pixel grid so the taskbar shows a crisp mark instead of a blur.

Outputs:

* ``assets/icon/png/eda-agent-<n>.png``: one PNG per size (committed, so
  the ICO steps below need no SVG renderer).
* ``src/eda_agent/web/dashboard_static/eda-agent.ico`` and ``.svg``: the
  web dashboard's favicon, also the desktop shortcut's icon.
* ``scripts/altium/StatusForm.dfm``: the status window's ``Icon.Data``,
  which Windows shows on its taskbar button and title bar.

Usage::

    python scripts/build_icons.py            # render PNGs (needs cairosvg), write all
    python scripts/build_icons.py --no-render # rebuild ICOs and the DFM from the PNGs
    python scripts/build_icons.py --check     # exit 1 if any output is stale
"""
from __future__ import annotations

import argparse
import io
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets" / "icon"
PNG_DIR = SRC / "png"
STATIC = ROOT / "src" / "eda_agent" / "web" / "dashboard_static"
WEB_ICO = STATIC / "eda-agent.ico"
WEB_SVG = STATIC / "eda-agent.svg"
DFM = ROOT / "scripts" / "altium" / "StatusForm.dfm"

#: Every size rendered to PNG.
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
#: Sizes drawn separately on the pixel grid.
HINTED = (16, 20, 24, 32)
#: The favicon and shortcut icon: every size; 256 stored as PNG.
WEB_SIZES = SIZES
#: The status window icon: classic bitmap entries only, which every VCL
#: TIcon reads, from 16 px (title bar) to 48 px (large taskbar buttons).
DFM_SIZES = (16, 20, 24, 32, 40, 48)

DFM_KEY = "Icon.Data"


def source_for(size: int) -> Path:
    return SRC / (f"eda-agent-{size}.svg" if size in HINTED else "eda-agent.svg")


def render_pngs() -> None:
    try:
        import cairosvg
    except ImportError:
        sys.exit("Rendering needs cairosvg (pip install cairosvg); "
                 "or pass --no-render to rebuild from the committed PNGs.")
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        png = cairosvg.svg2png(bytestring=source_for(size).read_bytes(),
                               output_width=size, output_height=size)
        (PNG_DIR / f"eda-agent-{size}.png").write_bytes(_normalise_png(png))


def _normalise_png(data: bytes) -> bytes:
    """Re-encode through Pillow so the bytes do not depend on the renderer."""
    from PIL import Image
    out = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGBA").save(out, "PNG", optimize=True)
    return out.getvalue()


def _rgba(size: int):
    from PIL import Image
    img = Image.open(PNG_DIR / f"eda-agent-{size}.png").convert("RGBA")
    if img.size != (size, size):
        raise ValueError(f"eda-agent-{size}.png is {img.size}, not {size}x{size}")
    return img


def _bmp_entry(size: int) -> bytes:
    """A 32-bit BGRA DIB with its AND mask, as an ICO image entry holds it."""
    img = _rgba(size)
    px = img.load()
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    colour = bytearray()
    mask = bytearray()
    row_bytes = ((size + 31) // 32) * 4
    for y in range(size - 1, -1, -1):  # bottom-up
        bits = bytearray(row_bytes)
        for x in range(size):
            r, g, b, a = px[x, y]
            colour += bytes((b, g, r, a))
            if a == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    return header + bytes(colour) + bytes(mask)


def _png_entry(size: int) -> bytes:
    return (PNG_DIR / f"eda-agent-{size}.png").read_bytes()


def ico_bytes(sizes, png_from: int = 256) -> bytes:
    """An ICO file: bitmap entries below ``png_from`` px, PNG entries from it."""
    images = [(s, _png_entry(s) if s >= png_from else _bmp_entry(s)) for s in sizes]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for size, data in images:
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        out += data
    return bytes(out)


def dfm_block(data: bytes, indent: str = "  ") -> str:
    """``Icon.Data = {...}`` the way Delphi writes binary properties."""
    hexed = data.hex().upper()
    lines = [hexed[i:i + 64] for i in range(0, len(hexed), 64)]
    body = "\n".join(indent + "  " + line for line in lines)
    return f"{indent}{DFM_KEY} = {{\n{body}}}\n"


_BLOCK_RE = re.compile(r"^  Icon\.Data = \{\n(?:    [0-9A-F]+\n)*    [0-9A-F]+\}\n", re.M)


def dfm_with_icon(text: str, data: bytes) -> str:
    block = dfm_block(data)
    if _BLOCK_RE.search(text):
        return _BLOCK_RE.sub(lambda _m: block, text, count=1)
    # First build: the form's own properties end at its first child object.
    anchor = re.search(r"^  FormStyle = .*\n", text, re.M)
    if not anchor:
        raise ValueError("StatusForm.dfm has no FormStyle line to place Icon.Data after")
    return text[:anchor.end()] + block + text[anchor.end():]


def dfm_icon(text: str) -> bytes | None:
    """The ICO bytes a DFM's form-level ``Icon.Data`` holds, or None."""
    m = _BLOCK_RE.search(text)
    if not m:
        return None
    hexed = re.sub(r"[^0-9A-F]", "", m.group(0).split("{", 1)[1])
    return bytes.fromhex(hexed)


def outputs() -> dict[Path, bytes]:
    dfm_text = DFM.read_text(encoding="utf-8")
    return {
        WEB_ICO: ico_bytes(WEB_SIZES),
        WEB_SVG: (SRC / "eda-agent.svg").read_bytes(),
        DFM: dfm_with_icon(dfm_text, ico_bytes(DFM_SIZES)).encode("utf-8"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-render", action="store_true",
                    help="skip SVG rendering; build from the committed PNGs")
    ap.add_argument("--check", action="store_true",
                    help="write nothing; exit 1 when an output is out of date")
    args = ap.parse_args(argv)
    if not args.check and not args.no_render:
        render_pngs()
    stale = []
    for path, data in outputs().items():
        current = path.read_bytes() if path.exists() else b""
        if path.suffix != ".ico":  # text: a Windows checkout may hold CRLF
            current = current.replace(b"\r\n", b"\n")
        if current == data:
            continue
        stale.append(path.relative_to(ROOT).as_posix())
        if not args.check:
            path.write_bytes(data)
    if args.check:
        for p in stale:
            print(f"out of date: {p}")
        return 1 if stale else 0
    print("written: " + (", ".join(stale) if stale else "nothing changed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
