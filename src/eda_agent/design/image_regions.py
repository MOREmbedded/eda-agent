# SPDX-License-Identifier: Apache-2.0
"""Raster image -> PCB region rectangles (logos, QR codes, artwork).

Putting a company logo on silkscreen is one of the few routine PCB jobs
with no API at all: Altium's own example script does it by converting a
bitmap into region primitives, and this is the same idea done as data
rather than as a dialog.

**Why rectangles rather than traced outlines.** A contour tracer gives
one polygon per shape, which sounds better until the details land: holes
(the middle of an "O") need a polygon that supports them or a cut, and a
pixel-faithful outline carries a vertex per pixel edge, so a 200x200
logo becomes one polygon with thousands of vertices. Decomposing into
rectangles instead is exact (no curve is approximated, so nothing drifts
from the source art), needs no hole handling at all (a hole is simply
where no rectangle was emitted), and each primitive stays four vertices.
The cost is primitive COUNT, which is why the run merging below matters
and why the caller gets the count before anything is placed.

The merge is two passes:

    row 0:  ####..####     two runs
    row 1:  ####..####     identical runs -> extend the rectangles down
    row 2:  ######....     different -> close the old ones, start new

Horizontal runs first (cheap, exact), then a vertical extension whenever
the next row's run has the same start and end. A solid block collapses
to one rectangle; dithered art does not collapse at all, which is a
property of the art rather than of this code, and is what the count is
there to warn about.

Pure and offline: no Altium, no bridge. The tool layer turns these
rectangles into pcb.place_region_poly calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: A rectangle in PIXEL space: x0 <= x < x1, y0 <= y < y1, y growing DOWN
#: (image convention). Converting to board coordinates flips it.
Rect = tuple[int, int, int, int]


def load_monochrome(path: str | Path, threshold: int = 128,
                    invert: bool = False) -> tuple[list[list[bool]], int, int]:
    """Read an image as a grid of "is this pixel ink".

    A pixel counts as ink when its luminance is BELOW ``threshold``, i.e.
    dark ink on a light background, which is how a logo is normally
    drawn; ``invert`` flips that for artwork supplied as white-on-black.

    Transparency counts as background regardless of colour. A transparent
    PNG whose logo is black would otherwise come out as a solid
    rectangle, because fully transparent pixels are usually black too.
    """
    from PIL import Image

    with Image.open(path) as handle:
        image = handle.convert("RGBA")
        width, height = image.size
        # PIL's own "L" conversion is the Rec. 601 luma, so the perceptual
        # grey comes from the library rather than from constants copied
        # here. Both bands are plain width*height byte strings in row
        # order, with no padding.
        grey = image.convert("L").tobytes()
        alpha = image.getchannel("A").tobytes()

    grid: list[list[bool]] = []
    for row in range(height):
        line: list[bool] = []
        base = row * width
        for column in range(width):
            index = base + column
            if alpha[index] < 128:
                line.append(False)
                continue
            ink = grey[index] < threshold
            line.append(not ink if invert else ink)
        grid.append(line)
    return grid, width, height


def _runs(row: list[bool]) -> list[tuple[int, int]]:
    """Maximal [start, end) spans of True in one row."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(row):
        if value and start is None:
            start = index
        elif not value and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(row)))
    return spans


def rectangles_from_grid(grid: list[list[bool]]) -> list[Rect]:
    """Decompose an ink grid into non-overlapping rectangles.

    Rectangles are emitted in top-to-bottom, left-to-right order, and
    together they cover exactly the ink pixels: no pixel is covered
    twice, and no background pixel is covered at all.
    """
    if not grid:
        return []
    # Open rectangles, keyed by the run they are currently extending.
    open_rects: dict[tuple[int, int], int] = {}  # (x0, x1) -> y0
    out: list[Rect] = []

    for y, row in enumerate(grid):
        spans = dict.fromkeys(_runs(row), True)
        # Close any open rectangle whose run does not continue on this row.
        for span in sorted(set(open_rects) - set(spans)):
            x0, x1 = span
            out.append((x0, open_rects.pop(span), x1, y))
        # Open a rectangle for every run that is new on this row.
        for span in spans:
            if span not in open_rects:
                open_rects[span] = y

    bottom = len(grid)
    for span in sorted(open_rects):
        x0, x1 = span
        out.append((x0, open_rects[span], x1, bottom))
    # Reading order, so a caller eyeballing the list sees the picture.
    return sorted(out, key=lambda r: (r[1], r[0]))


def rectangles_to_board(rects: list[Rect], origin_x: float, origin_y: float,
                        mils_per_pixel: float, image_height: int) -> list[dict[str, Any]]:
    """Pixel rectangles to board-space polygons, in mils.

    ``origin_x``/``origin_y`` place the image's BOTTOM-LEFT corner on the
    board. Image rows count downwards and board Y counts upwards, so the
    vertical axis is flipped here rather than at the call site, where
    getting it wrong silently mirrors the artwork.
    """
    out: list[dict[str, Any]] = []
    for x0, y0, x1, y1 in rects:
        left = origin_x + x0 * mils_per_pixel
        right = origin_x + x1 * mils_per_pixel
        # y0 is the TOP row of the rectangle, so it maps to the HIGHER
        # board coordinate.
        top = origin_y + (image_height - y0) * mils_per_pixel
        bottom = origin_y + (image_height - y1) * mils_per_pixel
        out.append({"vertices": [
            {"x": round(left, 4), "y": round(bottom, 4)},
            {"x": round(right, 4), "y": round(bottom, 4)},
            {"x": round(right, 4), "y": round(top, 4)},
            {"x": round(left, 4), "y": round(top, 4)},
        ]})
    return out


def plan_image_regions(path: str | Path, origin_x: float, origin_y: float,
                       target_width_mils: float, threshold: int = 128,
                       invert: bool = False) -> dict[str, Any]:
    """Everything needed to place an image, without placing it.

    Returns ``{ok, regions, region_count, ink_pixels, image, scale,
    bounds}``. The count is the number of primitives the board would
    gain, which is the figure worth looking at before committing:
    artwork that looks simple can decompose into thousands of pieces if
    it is dithered or anti-aliased, and a threshold change usually fixes
    that better than any amount of placing and undoing.
    """
    try:
        grid, width, height = load_monochrome(path, threshold=threshold, invert=invert)
    except FileNotFoundError:
        return {"ok": False, "reason": f"image not found: {path}"}
    except OSError as exc:
        return {"ok": False, "reason": f"cannot read {path}: {exc}"}

    if width == 0 or height == 0:
        return {"ok": False, "reason": "image has no pixels"}
    if target_width_mils <= 0:
        return {"ok": False, "reason": "target width must be greater than zero"}

    rects = rectangles_from_grid(grid)
    ink = sum(sum(1 for cell in row if cell) for row in grid)
    if not rects:
        return {"ok": False, "reason":
                f"no ink found at threshold {threshold}"
                f"{' (inverted)' if invert else ''}: every pixel read as "
                f"background, so there is nothing to place"}

    mils_per_pixel = target_width_mils / width
    regions = rectangles_to_board(rects, origin_x, origin_y, mils_per_pixel, height)
    return {
        "ok": True,
        "regions": regions,
        "region_count": len(regions),
        "ink_pixels": ink,
        "image": {"width_px": width, "height_px": height, "path": str(path)},
        "scale": {"mils_per_pixel": mils_per_pixel,
                  "width_mils": target_width_mils,
                  "height_mils": height * mils_per_pixel},
        "bounds": {"x1": origin_x, "y1": origin_y,
                   "x2": origin_x + target_width_mils,
                   "y2": origin_y + height * mils_per_pixel},
    }
