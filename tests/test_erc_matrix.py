# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""An ERC policy can be stored in a profile and applied to a project.

Project Options > Error Reporting lives in the .PrjPcb, so every new
project starts at Altium's defaults and a house policy survives only as
long as somebody re-enters it. The reader and the writer already existed;
what did not was anything that could say "this project is not at the
policy, and here is the list of writes that would get it there".

The sharp edge is that the only name a violation type has is Altium's own
prose, and the Pascal resolves it by an exact case-folded match. A
comparison here that folded any further would report "same" for a type the
write then refuses, so the folding is tested against the Pascal itself
rather than against what looks reasonable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from eda_agent.design.erc_matrix import (
    LEVEL_NAMES,
    UNLISTED_LEAVE,
    check_erc_levels,
    level_name,
    level_number,
    read_set,
    reconcile,
    set_names,
    write_items,
)
from tests.pascal_source import load

_PROJECT_PAS = (Path(__file__).resolve().parents[1] / "scripts" / "altium"
                / "Project.pas")


def _code(text: str) -> str:
    """The Pascal with its brace comments removed."""
    return re.sub(r"\{[^}]*\}", "", text)


# --- the level vocabulary, against the Pascal that will do the writing ---

def test_every_spelling_the_writer_accepts_is_accepted_here():
    """ErcLevelFromText is the gate a level passes through; a spelling this
    module rejects can never be written, and one it wrongly accepts is only
    found by a failed apply."""
    body = _code(load(_PROJECT_PAS)["ErcLevelFromText"])
    pairs = []
    for line in body.splitlines():
        m = re.search(r"Result := (\d)\s*$", line.strip())
        if not m:
            continue
        for literal in re.findall(r"'([^']*)'", line):
            pairs.append((literal, int(m.group(1))))
    assert len(pairs) >= 10, body
    for literal, number in pairs:
        assert level_number(literal) == number, literal


def test_the_level_names_are_the_ones_the_reader_reports():
    body = _code(load(_PROJECT_PAS)["ErcLevelName"])
    found = {int(n): name for n, name
             in re.findall(r"Level = (\d) Then Result := '([^']+)'", body)}
    assert found == dict(enumerate(LEVEL_NAMES))


@pytest.mark.parametrize("text", ["ERROR", " error ", "Fatal Error", "2"])
def test_a_level_is_folded_the_way_the_pascal_folds_it(text):
    assert level_number(text) is not None


@pytest.mark.parametrize("text", ["loud", "", None, "4", "-1", True])
def test_what_is_not_a_level_is_not_guessed_at(text):
    assert level_number(text) is None


def test_a_level_altium_would_not_report_is_passed_through_as_written():
    """ErcLevelName answers the number itself outside 0..3, and -1 is what
    it answers for a type whose level it would not give."""
    assert level_name("-1") == "-1"
    assert level_name(-1) == "-1"
    assert level_name(2) == "error"


# --- the files -----------------------------------------------------------

def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_the_default_set_is_the_file_at_the_profile_root(tmp_path):
    _write(tmp_path / "erc_levels.json",
           {"levels": {"Nets with multiple names": "no_report"}})
    got = read_set(tmp_path)
    assert got["ok"] is True
    assert got["levels"] == {"Nets with multiple names": "no_report"}
    assert got["unlisted"] == UNLISTED_LEAVE
    assert got["chain"] == ["erc_levels.json"]


def test_a_named_set_lives_beside_it(tmp_path):
    _write(tmp_path / "erc_levels" / "strict.json",
           {"levels": {"Nets with no driving source": "error"}})
    assert set_names(tmp_path) == ["strict"]
    assert read_set(tmp_path, "strict")["levels"] == {
        "Nets with no driving source": "error"}


def test_a_set_extends_another_and_the_nearer_file_wins(tmp_path):
    _write(tmp_path / "erc_levels" / "company.json",
           {"levels": {"A": "warning", "B": "error"}, "unlisted": "warning"})
    _write(tmp_path / "erc_levels" / "project.json",
           {"extends": "company", "levels": {"B": "fatal", "C": "no_report"}})
    got = read_set(tmp_path, "project")
    assert got["levels"] == {"A": "warning", "B": "fatal", "C": "no_report"}
    assert got["unlisted"] == "warning", "an unset field keeps the base's"
    assert got["chain"] == ["company.json", "project.json"], "base first"


def test_null_stops_managing_a_type_the_base_manages(tmp_path):
    """Not the same statement as no_report: one says the policy has nothing
    to say about the type, the other says the policy switches it off."""
    _write(tmp_path / "erc_levels" / "company.json",
           {"levels": {"A": "warning", "B": "error"}})
    _write(tmp_path / "erc_levels" / "project.json",
           {"extends": "company", "levels": {"B": None}})
    assert read_set(tmp_path, "project")["levels"] == {"A": "warning"}


def test_null_drops_the_entry_however_the_base_spelled_it(tmp_path):
    _write(tmp_path / "erc_levels" / "company.json",
           {"levels": {"Nets With Multiple Names": "warning"}})
    _write(tmp_path / "erc_levels" / "project.json",
           {"extends": "company", "levels": {"nets with multiple names": None}})
    assert read_set(tmp_path, "project")["levels"] == {}


def test_a_set_that_extends_itself_is_refused_by_name(tmp_path):
    _write(tmp_path / "erc_levels" / "a.json",
           {"extends": "b", "levels": {"A": "warning"}})
    _write(tmp_path / "erc_levels" / "b.json",
           {"extends": "a", "levels": {"B": "warning"}})
    got = read_set(tmp_path, "a")
    assert got["ok"] is False and "loop" in got["reason"]


def test_a_base_that_is_not_there_names_the_set_that_wanted_it(tmp_path):
    _write(tmp_path / "erc_levels" / "project.json",
           {"extends": "nosuchset", "levels": {"A": "warning"}})
    got = read_set(tmp_path, "project")
    assert got["ok"] is False
    assert "project.json" in got["reason"] and "nosuchset" in got["reason"]


def test_a_set_that_is_not_there_is_refused_by_path(tmp_path):
    got = read_set(tmp_path)
    assert got["ok"] is False and "erc_levels.json" in got["reason"]


def test_a_level_that_is_not_a_level_is_refused_before_anything_is_written(tmp_path):
    _write(tmp_path / "erc_levels.json", {"levels": {"A": "loud"}})
    got = read_set(tmp_path)
    assert got["ok"] is False and "loud" in got["reason"]


@pytest.mark.parametrize("data,expected", [
    ({"levels": {"A": "warning"}}, []),
    ({"levels": {"A": None}}, []),
    ({}, ["no levels"]),
    ({"levels": []}, ["must be an object"]),
    ({"levels": {}, "unlisted": "loud"}, ["unlisted is"]),
    ({"levels": {}, "unlisted": "leave"}, []),
    ({"levels": {}, "unlisted": "warning"}, []),
    ({"levels": {}, "extends": ""}, ["extends must name"]),
    ({"levels": {}, "lvels": {}}, ["unknown setting"]),
    ({"levels": {}, "_comment": "fine"}, []),
])
def test_what_profile_validate_reports(data, expected):
    problems = check_erc_levels(data)
    assert len(problems) == len(expected), problems
    for problem, fragment in zip(problems, expected):
        assert fragment in problem


# --- reconciling ---------------------------------------------------------

def _matrix(*rows):
    return [{"kind": kind, "description": description, "level": level}
            for kind, description, level in rows]

_PROJECT = _matrix(
    (0, "Duplicate Net Names", "warning"),
    (1, "Nets with multiple names", "warning"),
    (2, "Nets with no driving source", "warning"),
    (3, "Floating power object", "error"),
)


def test_a_type_already_at_its_level_is_left_alone():
    got = reconcile({"Floating power object": "error"}, _PROJECT)
    assert got["change"] == []
    assert [e["description"] for e in got["same"]] == ["Floating power object"]
    assert got["in_step"] is True


def test_a_type_at_another_level_is_a_change_carrying_its_kind():
    got = reconcile({"Nets with multiple names": "no_report"}, _PROJECT)
    assert got["change"] == [{
        "type": "Nets with multiple names", "kind": 1,
        "description": "Nets with multiple names",
        "want": "no_report", "got": "warning"}]
    assert got["in_step"] is False


def test_a_description_matches_the_way_the_writer_matches_it():
    """ErcKindFromText lower-cases both sides and trims the caller's text."""
    got = reconcile({"  nets WITH multiple names ": "no_report"}, _PROJECT)
    assert got["change"] and got["change"][0]["kind"] == 1
    assert got["unknown"] == []


def test_a_kind_number_names_the_same_type_a_description_does():
    """Any non-negative integer is taken as a kind number by the writer
    before it looks at a single description, so it has to be here too."""
    got = reconcile({"1": "no_report"}, _PROJECT)
    assert got["change"][0]["description"] == "Nets with multiple names"


def test_a_type_this_altium_does_not_have_is_reported_not_skipped():
    """A policy written against another Altium version, half of it quietly
    doing nothing, is the failure this exists to prevent."""
    got = reconcile({"Net has no namne": "error"}, _PROJECT)
    assert got["change"] == []
    assert got["unknown"][0]["type"] == "Net has no namne"
    assert got["in_step"] is False


def test_a_level_that_is_not_a_level_is_reported_rather_than_written():
    got = reconcile({"Duplicate Net Names": "loud"}, _PROJECT)
    assert got["invalid"][0]["type"] == "Duplicate Net Names"
    assert got["change"] == [] and got["in_step"] is False


def test_what_the_set_does_not_name_is_untouched_and_counted():
    got = reconcile({"Duplicate Net Names": "warning"}, _PROJECT)
    assert got["unmanaged_count"] == 3
    assert [s["kind"] for s in got["unmanaged"]] == [1, 2, 3]
    assert got["change"] == [] and got["in_step"] is True


def test_unlisted_pins_the_rest_of_the_matrix():
    got = reconcile({"Duplicate Net Names": "warning"}, _PROJECT,
                    unlisted="warning")
    assert [e["kind"] for e in got["change"]] == [3], (
        "only the one type that is not already at warning")
    assert all(e.get("unlisted") for e in got["change"])


def test_a_level_altium_would_not_report_counts_as_a_difference():
    project = _matrix((7, "Something", "-1"))
    got = reconcile({"Something": "error"}, project)
    assert got["change"][0]["got"] == "-1"


def test_an_unlisted_that_is_not_a_level_is_refused_not_applied():
    got = reconcile({}, _PROJECT, unlisted="loud")
    assert got["change"] == []
    assert got["invalid"][0]["type"] == "unlisted"


def test_reconciling_a_matrix_against_itself_is_in_step():
    wanted = {row["description"]: row["level"] for row in _PROJECT}
    got = reconcile(wanted, _PROJECT, unlisted=UNLISTED_LEAVE)
    assert got["in_step"] is True and got["unmanaged"] == []


# --- turning changes into the write --------------------------------------

def test_a_change_is_written_by_kind_number_not_by_description():
    """set_erc_levels packs its items into one string with ~~ and splits
    each at its first =, so a description carrying either would be
    mangled; the diff has already resolved every type to a number."""
    items = write_items([{"kind": 1, "want": "no_report", "description": "x"},
                         {"kind": 3, "want": "error", "description": "y"}])
    assert items == ["1=no_report", "3=error"]


def test_an_entry_with_no_kind_is_not_written():
    assert write_items([{"kind": None, "want": "error"}]) == []


@pytest.mark.parametrize("name", ["../secrets", "sub/dir", "..",
                                  r"C:\Windows\win.ini"])
def test_a_set_is_named_not_pathed(tmp_path, name):
    """extends is hand-written in a file that may be committed to a
    project's repo and shared, so it reaches the profile's own erc_levels/
    and nowhere else."""
    _write(tmp_path / "erc_levels.json",
           {"extends": name, "levels": {"A": "warning"}})
    got = read_set(tmp_path)
    assert got["ok"] is False and "plain file name" in got["reason"]
    assert check_erc_levels({"levels": {}, "extends": name})


def test_asking_for_a_set_by_a_path_is_refused_too(tmp_path):
    got = read_set(tmp_path, "../../somewhere")
    assert got["ok"] is False and "plain file name" in got["reason"]
