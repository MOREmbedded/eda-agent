# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Tests for the headless BOM consolidation (roadmap V1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from eda_agent.fileio.altium_sch import read_schematic_components
from eda_agent.fileio.bom import bom_from_file, bom_to_csv, consolidate_bom
from eda_agent.fileio.review import review_project_file

_FIXDIR = Path(__file__).resolve().parent / "integration" / "fixtures"
FIXTURE = _FIXDIR / "main.SchDoc"
PRJ = _FIXDIR / "EDAAgentTest.PrjPcb"


def _c(designator, mpn="", value="", lib_reference="RES", manufacturer="M",
       datasheet="u"):
    return {"designator": designator, "mpn": mpn, "value": value,
            "lib_reference": lib_reference, "manufacturer": manufacturer,
            "datasheet": datasheet}


def test_identical_parts_merge_into_one_line():
    lines = consolidate_bom([
        _c("R1", value="10k"), _c("R2", value="10k"), _c("R3", value="10k")])
    assert len(lines) == 1
    assert lines[0]["quantity"] == 3
    assert lines[0]["designators"] == ["R1", "R2", "R3"]


def test_distinct_values_stay_separate():
    lines = consolidate_bom([_c("R1", value="10k"), _c("R2", value="1k")])
    assert len(lines) == 2
    assert all(line["quantity"] == 1 for line in lines)


def test_designators_sort_naturally():
    # R10 must come AFTER R2, not lexically before it.
    lines = consolidate_bom([
        _c("R10", value="10k"), _c("R2", value="10k"), _c("R1", value="10k")])
    assert lines[0]["designators"] == ["R1", "R2", "R10"]


def test_lines_ordered_by_first_designator():
    lines = consolidate_bom([
        _c("R5", value="1k"), _c("C1", value="100n", lib_reference="CAP")])
    # C1 line sorts before R5 line.
    assert lines[0]["designators"][0] == "C1"
    assert lines[1]["designators"][0] == "R5"


def test_component_without_designator_skipped():
    lines = consolidate_bom([_c("", value="10k"), _c("R1", value="10k")])
    assert sum(line["quantity"] for line in lines) == 1
    assert lines[0]["designators"] == ["R1"]


def test_mpn_distinguishes_same_value():
    # Same value but different MPN -> different orderable parts.
    lines = consolidate_bom([
        _c("R1", value="10k", mpn="RC0603-10K-A"),
        _c("R2", value="10k", mpn="RC0603-10K-B")])
    assert len(lines) == 2


def test_real_fixture_quantities_sum_to_component_count():
    comps = read_schematic_components(FIXTURE)
    lines = consolidate_bom(comps)
    # Every placed component appears exactly once across all BOM lines.
    assert sum(line["quantity"] for line in lines) == len(comps) == 14
    all_desigs = [d for line in lines for d in line["designators"]]
    assert len(all_desigs) == len(set(all_desigs)) == 14


def test_bom_to_csv_has_header_and_rows():
    lines = consolidate_bom([_c("R1", value="10k"), _c("R2", value="10k")])
    csv_text = bom_to_csv(lines)
    rows = csv_text.strip().split("\n")
    assert rows[0].startswith("Quantity,Designators,MPN")
    assert len(rows) == 2  # header + one consolidated line
    assert '"R1, R2"' in csv_text  # grouped designators quoted as one cell


def test_bom_from_schdoc_file():
    lines = bom_from_file(FIXTURE)
    assert sum(ln["quantity"] for ln in lines) == 14  # all placed components
    all_desigs = [d for ln in lines for d in ln["designators"]]
    assert len(all_desigs) == len(set(all_desigs)) == 14


def test_bom_from_project_file():
    # A .PrjPcb aggregates its sheets (here one sheet, main.SchDoc).
    lines = bom_from_file(PRJ)
    assert sum(ln["quantity"] for ln in lines) == 14


def test_bom_to_csv_empty_is_header_only():
    assert bom_to_csv([]).strip().split("\n") == ["Quantity,Designators,MPN,"
                                                  "Manufacturer,Value,"
                                                  "LibReference,Datasheet"]


# A file that cannot be read used to come back as an empty BOM, and an
# empty review, both reported as success.

def _project(tmp_path, structure=None):
    prj = tmp_path / "board.PrjPcb"
    prj.write_text("[Design]\n", encoding="utf-8")
    if structure is not None:
        (tmp_path / "board.PrjPcbStructure").write_text(
            structure, encoding="utf-8")
    return prj


@pytest.mark.parametrize("read", [bom_from_file, review_project_file])
def test_a_missing_file_is_an_error(tmp_path, read):
    with pytest.raises(FileNotFoundError):
        read(tmp_path / "absent.PrjPcb")


@pytest.mark.parametrize("read", [bom_from_file, review_project_file])
def test_a_board_file_is_not_read_as_an_empty_design(tmp_path, read):
    board = tmp_path / "board.PcbDoc"
    board.write_bytes(b"PCB")
    with pytest.raises(ValueError, match="cannot be read here"):
        read(board)


@pytest.mark.parametrize("read", [bom_from_file, review_project_file])
@pytest.mark.parametrize("structure", [
    None,
    "Record=TopLevelDocument|FileName=absent.SchDoc|SheetNumber=\n",
])
def test_a_project_with_no_sheets_to_read_is_an_error(tmp_path, read,
                                                     structure):
    with pytest.raises(ValueError, match="found no sheets"):
        read(_project(tmp_path, structure))


def test_the_fixture_project_still_has_parts():
    assert bom_from_file(PRJ)
