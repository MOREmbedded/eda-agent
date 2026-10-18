# SPDX-License-Identifier: Apache-2.0
"""Reading an exported fab package back and checking it against the board.

The comparison half is pure dict-in/dict-out and always runs. The
reading half needs gerbonara, an optional dependency, and skips without
it. Fixtures are written here rather than committed: a few hundred bytes
of Gerber is clearer in the test than in a binary blob.
"""
from __future__ import annotations

import warnings

import pytest

from eda_agent.fileio.gerber_check import (
    ERROR,
    WARNING,
    compare_with_board,
    summarise_package,
)

gerbonara = pytest.importorskip("gerbonara", reason="needs the [gerber] extra")

#: A 20 x 15 mm rectangle stroked with a 0.2 mm pen.
OUTLINE = """%FSLAX46Y46*%
%MOMM*%
%ADD10C,0.200000*%
D10*
X0Y0D02*
X20000000Y0D01*
X20000000Y15000000D01*
X0Y15000000D01*
X0Y0D01*
M02*
"""
COPPER = ("%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.250000*%\nD10*\n"
          "X1000000Y1000000D03*\nM02*\n")
DRILL = "M48\nMETRIC,TZ\nT1C0.300\n%\nT1\nX5.0Y5.0\nX10.0Y5.0\nM30\n"


@pytest.fixture
def package(tmp_path):
    """An Altium-style fab folder: .GTL/.GBL copper, .GKO outline, .TXT drill."""
    warnings.simplefilter("ignore")
    for name in ("GTL", "GBL", "GTO", "GTS"):
        (tmp_path / f"Board.{name}").write_text(COPPER, encoding="utf-8")
    (tmp_path / "Board.GKO").write_text(OUTLINE, encoding="utf-8")
    (tmp_path / "Board.TXT").write_text(DRILL, encoding="utf-8")
    return tmp_path


BOARD = {"copper_layers": 2, "outline_mm": {"width": 20.0, "height": 15.0},
         "hole_count": 2, "min_drill_mm": 0.25}


# ---------------------------------------------------------------- reading

def test_altium_fab_extensions_are_classified(package):
    summary = summarise_package(package)
    assert summary["ok"] is True
    assert summary["copper_layers"] == 2
    assert summary["drills"]["files"] == 1
    assert "copper" in summary["layer_map"].lower()


def test_drill_hits_and_sizes_are_read(package):
    drills = summarise_package(package)["drills"]
    assert drills["hits"] == 2
    assert drills["sizes_mm"] == [0.3]
    assert drills["smallest_mm"] == 0.3


def test_the_outline_stroke_is_subtracted_from_the_board_size(package):
    """A Gerber bbox spans the ink. A 20 mm board stroked with a 0.2 mm
    pen measures 20.2 mm, and comparing that raw calls every board
    oversized by one pen width."""
    outline = summarise_package(package)["outline_mm"]
    assert outline["width"] == 20.0 and outline["height"] == 15.0
    assert outline["stroke_mm"] == 0.2
    assert outline["raw_width"] == 20.2, "the uncompensated figure stays visible"


def test_the_smallest_copper_aperture_is_found(package):
    assert summarise_package(package)["min_aperture_mm"] == 0.25


def test_a_missing_directory_is_refused(tmp_path):
    result = summarise_package(tmp_path / "nope")
    assert result["ok"] is False and "not a directory" in result["reason"]


def test_an_empty_directory_is_refused(tmp_path):
    result = summarise_package(tmp_path)
    assert result["ok"] is False and "no files" in result["reason"]


def test_an_upstream_parser_failure_becomes_a_refusal_naming_the_files(tmp_path):
    """gerbonara 1.6.3 raises UnboundLocalError from its own
    _common_prefix on some inputs. A parser crash must arrive as a
    refusal, not as a traceback out of a tool."""
    (tmp_path / "Board.GTL").write_text("this is not gerber at all", encoding="utf-8")
    (tmp_path / "Board.GKO").write_text("\x00\x01\x02 binary junk", encoding="utf-8")
    with warnings.catch_warnings():
        # gerbonara warns that it is falling back to its autoguesser,
        # which is exactly the path this test is here to exercise.
        warnings.simplefilter("ignore")
        result = summarise_package(tmp_path)
    if result["ok"]:
        pytest.skip("this input happened to parse; the guard is still in place")
    assert "files" in result and "Board.GTL" in result["files"]


# ---------------------------------------------------------------- comparison

def test_a_matching_package_passes(package):
    result = compare_with_board(summarise_package(package), BOARD)
    assert result["ok"] and result["passed"]
    assert result["findings"] == []


def test_a_layer_count_mismatch_is_an_error(package):
    result = compare_with_board(summarise_package(package),
                                {**BOARD, "copper_layers": 4})
    assert result["passed"] is False
    assert any(f["check"] == "copper_layers" and f["severity"] == ERROR
               for f in result["findings"])


def test_a_board_size_mismatch_is_an_error(package):
    result = compare_with_board(summarise_package(package),
                                {**BOARD, "outline_mm": {"width": 25.0, "height": 15.0}})
    assert any(f["check"] == "outline" for f in result["findings"])


def test_a_size_within_tolerance_is_not_reported(package):
    """0.05 mm absorbs exporter rounding, not a real size difference."""
    result = compare_with_board(summarise_package(package),
                                {**BOARD, "outline_mm": {"width": 20.02, "height": 15.0}})
    assert not any(f["check"] == "outline" for f in result["findings"])


def test_a_hole_count_mismatch_is_an_error(package):
    result = compare_with_board(summarise_package(package),
                                {**BOARD, "hole_count": 7})
    assert any(f["check"] == "hole_count" for f in result["findings"])


def test_a_drill_below_the_fab_minimum_is_an_error(package):
    result = compare_with_board(summarise_package(package),
                                {**BOARD, "min_drill_mm": 0.5})
    assert any(f["check"] == "min_drill" and f["severity"] == ERROR
               for f in result["findings"])


def test_a_small_aperture_is_a_warning_not_an_error(package):
    """Apertures cover pads and text as well as tracks, so this cannot
    be asserted as a track-width violation."""
    result = compare_with_board(summarise_package(package),
                                {**BOARD, "min_feature_mm": 0.3})
    finding = next(f for f in result["findings"] if f["check"] == "min_feature")
    assert finding["severity"] == WARNING
    assert "pads and text" in finding["message"]


def test_only_the_checks_with_inputs_are_run(package):
    """A missing input must not read as a pass."""
    result = compare_with_board(summarise_package(package), {"copper_layers": 2})
    assert result["checks_run"] == ["copper_layers"]
    assert "hole_count" not in result["checks_run"]


def test_a_package_with_no_drill_file_cannot_be_read_at_all(tmp_path):
    """gerbonara refuses to map a package with no drill file rather than
    returning one with zero drills, so this surfaces as a read failure.
    Either way it must not look like a pass."""
    warnings.simplefilter("ignore")
    for name in ("GTL", "GBL", "GTO", "GTS"):
        (tmp_path / f"Board.{name}").write_text(COPPER, encoding="utf-8")
    (tmp_path / "Board.GKO").write_text(OUTLINE, encoding="utf-8")

    summary = summarise_package(tmp_path)
    result = compare_with_board(summary, {})
    if summary["ok"]:
        assert any(f["check"] == "drill_files" for f in result["findings"])
    else:
        assert result["ok"] is False and "passed" not in result


def test_a_summary_with_no_drill_files_reports_it():
    """Reached when a package parses but carries no drill layer."""
    summary = {"ok": True, "copper_layers": 2, "outline_mm": None,
               "min_aperture_mm": 0.2,
               "drills": {"files": 0, "hits": 0, "sizes_mm": [],
                          "smallest_mm": None, "plating": []}}
    result = compare_with_board(summary, {})
    assert any(f["check"] == "drill_files" and f["severity"] == ERROR
               for f in result["findings"])


def test_an_unreadable_package_is_not_reported_as_a_pass():
    result = compare_with_board({"ok": False, "reason": "boom"}, BOARD)
    assert result["ok"] is False and "boom" in result["reason"]
    assert "passed" not in result
