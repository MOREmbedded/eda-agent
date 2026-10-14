# SPDX-License-Identifier: Apache-2.0
"""Raster image to PCB region rectangles.

The decomposition is the part worth testing hard: it has to cover the
ink exactly, cover no background, handle holes without knowing they are
holes, and flip the vertical axis exactly once.
"""
from __future__ import annotations

import pytest

from eda_agent.design.image_regions import (
    load_monochrome,
    plan_image_regions,
    rectangles_from_grid,
    rectangles_to_board,
)


def _grid(*rows: str) -> list[list[bool]]:
    """"#" is ink, anything else is background."""
    return [[c == "#" for c in row] for row in rows]


def _covered(rects, width, height) -> set[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    for x0, y0, x1, y1 in rects:
        for y in range(y0, y1):
            for x in range(x0, x1):
                assert (x, y) not in cells, "rectangles must not overlap"
                cells.add((x, y))
    return cells


def _ink(grid) -> set[tuple[int, int]]:
    return {(x, y) for y, row in enumerate(grid)
            for x, cell in enumerate(row) if cell}


# ---------------------------------------------------------------- decomposition

def test_an_empty_grid_yields_nothing():
    assert rectangles_from_grid([]) == []
    assert rectangles_from_grid(_grid("...", "...")) == []


def test_a_solid_block_collapses_to_one_rectangle():
    rects = rectangles_from_grid(_grid("####", "####", "####"))
    assert rects == [(0, 0, 4, 3)]


def test_a_single_pixel_is_a_unit_rectangle():
    assert rectangles_from_grid(_grid(".#.")) == [(1, 0, 2, 1)]


def test_two_runs_on_a_row_stay_separate():
    rects = rectangles_from_grid(_grid("##..##"))
    assert rects == [(0, 0, 2, 1), (4, 0, 6, 1)]


def test_identical_runs_merge_down_but_a_changed_run_closes_them():
    grid = _grid(
        "##..##",
        "##..##",
        "######",
    )
    rects = rectangles_from_grid(grid)
    # The two columns merge across rows 0-1, then row 2 is its own run.
    assert (0, 0, 2, 2) in rects and (4, 0, 6, 2) in rects
    assert (0, 2, 6, 3) in rects
    assert _covered(rects, 6, 3) == _ink(grid)


@pytest.mark.parametrize("rows", [
    ("#.#", ".#.", "#.#"),                      # checkerboard, worst case
    ("####", "#..#", "#..#", "####"),           # a ring: the hole matters
    ("..##..", ".####.", "######", ".####."),   # a diamond
    ("#", ),                                     # one pixel
    ("#####",),                                  # one row
    ("#", "#", "#"),                             # one column
])
def test_the_decomposition_covers_exactly_the_ink(rows):
    grid = _grid(*rows)
    rects = rectangles_from_grid(grid)
    assert _covered(rects, len(grid[0]), len(grid)) == _ink(grid)


def test_a_hole_is_simply_not_covered():
    """The middle of an "O" needs no special handling: nothing is emitted
    there, so no rectangle has to carry a hole."""
    grid = _grid("####", "#..#", "####")
    rects = rectangles_from_grid(grid)
    covered = _covered(rects, 4, 3)
    assert (1, 1) not in covered and (2, 1) not in covered
    assert covered == _ink(grid)


def test_rectangles_come_back_in_reading_order():
    rects = rectangles_from_grid(_grid(".#", "#."))
    assert rects == sorted(rects, key=lambda r: (r[1], r[0]))


# ---------------------------------------------------------------- board mapping

def test_the_vertical_axis_is_flipped_exactly_once():
    """Image rows count down, board Y counts up. A logo placed mirrored
    is the classic silkscreen embarrassment."""
    # Ink on the TOP row of a 1x2 image.
    rects = [(0, 0, 1, 1)]
    regions = rectangles_to_board(rects, origin_x=0, origin_y=0,
                                  mils_per_pixel=10, image_height=2)
    ys = [v["y"] for v in regions[0]["vertices"]]
    # Top image row -> the HIGHER board coordinates (y 10..20), not 0..10.
    assert min(ys) == 10 and max(ys) == 20


def test_scaling_and_origin_are_applied():
    regions = rectangles_to_board([(0, 0, 2, 1)], origin_x=100, origin_y=50,
                                  mils_per_pixel=5, image_height=1)
    xs = [v["x"] for v in regions[0]["vertices"]]
    ys = [v["y"] for v in regions[0]["vertices"]]
    assert min(xs) == 100 and max(xs) == 110
    assert min(ys) == 50 and max(ys) == 55


def test_each_region_is_a_closed_four_vertex_polygon():
    regions = rectangles_to_board([(0, 0, 1, 1)], 0, 0, 1, 1)
    vertices = regions[0]["vertices"]
    assert len(vertices) == 4
    assert len({(v["x"], v["y"]) for v in vertices}) == 4


# ---------------------------------------------------------------- image loading

def _png(tmp_path, rows, name="logo.png"):
    from PIL import Image
    height, width = len(rows), len(rows[0])
    image = Image.new("RGB", (width, height), (255, 255, 255))
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == "#":
                image.putpixel((x, y), (0, 0, 0))
    path = tmp_path / name
    image.save(path)
    return path


def test_dark_pixels_are_ink_and_light_ones_are_not(tmp_path):
    path = _png(tmp_path, ["#.", ".#"])
    grid, width, height = load_monochrome(path)
    assert (width, height) == (2, 2)
    assert grid == [[True, False], [False, True]]


def test_invert_flips_which_pixels_are_ink(tmp_path):
    path = _png(tmp_path, ["#."])
    grid, _w, _h = load_monochrome(path, invert=True)
    assert grid == [[False, True]]


def test_transparent_pixels_are_background_whatever_their_colour(tmp_path):
    """A transparent PNG's unused area is usually black, which would
    otherwise read as a solid rectangle of ink."""
    from PIL import Image
    image = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
    image.putpixel((0, 0), (0, 0, 0, 255))
    path = tmp_path / "t.png"
    image.save(path)

    grid, _w, _h = load_monochrome(path)
    assert grid == [[True, False]]


# ---------------------------------------------------------------- planning

def test_plan_reports_geometry_scale_and_count(tmp_path):
    path = _png(tmp_path, ["####", "####"])
    plan = plan_image_regions(path, origin_x=0, origin_y=0, target_width_mils=400)
    assert plan["ok"] is True
    assert plan["region_count"] == 1
    assert plan["ink_pixels"] == 8
    assert plan["scale"]["mils_per_pixel"] == 100
    assert plan["scale"]["height_mils"] == 200
    assert plan["bounds"] == {"x1": 0, "y1": 0, "x2": 400, "y2": 200}


def test_plan_refuses_artwork_with_no_ink_and_says_why(tmp_path):
    path = _png(tmp_path, ["..", ".."])
    plan = plan_image_regions(path, 0, 0, 100)
    assert plan["ok"] is False
    assert "threshold" in plan["reason"]


def test_plan_refuses_a_missing_file(tmp_path):
    plan = plan_image_regions(tmp_path / "nope.png", 0, 0, 100)
    assert plan["ok"] is False and "not found" in plan["reason"]


def test_plan_refuses_a_non_positive_width(tmp_path):
    path = _png(tmp_path, ["#"])
    assert plan_image_regions(path, 0, 0, 0)["ok"] is False
    assert plan_image_regions(path, 0, 0, -5)["ok"] is False


def test_a_dithered_image_reports_its_true_primitive_count(tmp_path):
    """Checkerboard art cannot merge at all; the count is what warns a
    caller before the board gains a thousand primitives."""
    rows = ["".join("#" if (x + y) % 2 == 0 else "." for x in range(10))
            for y in range(10)]
    path = _png(tmp_path, rows)
    plan = plan_image_regions(path, 0, 0, 100)
    assert plan["region_count"] == 50
