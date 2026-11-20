# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Reading a large board without walking it again for every page.

MEASURED 2026-11-20 on a board with 2,474 pads, 57,962 copper objects,
463 nets and 709 net classes: reading all of it took about 14 minutes.
Each copper page walked the board from the start (566 s for the section),
every class was asked about every net (142 s), and pads and components
copied their growing reply on every entry (77 s and 43 s).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from eda_agent.layout import read_altium
from tests.pascal_source import load

PCB = Path(__file__).resolve().parent.parent / "scripts" / "altium" / "PCB.pas"


@pytest.fixture(scope="module")
def pcb() -> dict[str, str]:
    return load(PCB)


# --- the reader ------------------------------------------------------------

def _pager(total: int, big: bool):
    calls: list[tuple[int, int]] = []

    def send(command, params):
        offset, limit = int(params["offset"]), int(params["limit"])
        calls.append((offset, limit))
        chunk = [{"i": i} for i in range(offset, min(total, offset + limit))]
        reply = {"copper": chunk, "count": len(chunk),
                 "more": offset + len(chunk) < total}
        if big:
            reply["big_pages"] = True
        return reply

    return send, calls


def test_the_rest_of_a_section_comes_in_big_pages_once_the_script_can():
    send, calls = _pager(70000, big=True)
    items = read_altium._paged(send, "copper", "copper", read_altium.PAGE)
    assert [i["i"] for i in items] == list(range(70000))
    big = read_altium.BIG_PAGE
    assert calls == [(0, 1500), (1500, big), (1500 + big, big),
                     (1500 + 2 * big, big)]


def test_an_older_script_keeps_the_page_it_was_asked_for():
    """It appends each entry to one string, so a big page would cost the
    square of its size: 2,474 pads took 239 s in one call, 77 s in two."""
    send, calls = _pager(4000, big=False)
    items = read_altium._paged(send, "copper", "copper", 1500)
    assert len(items) == 4000
    assert [limit for _, limit in calls] == [1500, 1500, 1500]


def test_a_page_asked_bigger_than_the_big_page_stays_as_asked():
    send, calls = _pager(100, big=True)
    read_altium._paged(send, "copper", "copper", 40)
    assert [limit for _, limit in calls] == [40, read_altium.BIG_PAGE]


# --- the script ------------------------------------------------------------

@pytest.mark.parametrize("name, grown", [
    ("LmPadsSection", "Items"),
    ("LmCopperSection", "Items"),
    ("LmComponentsSection", "Items"),
    ("LmClassesSection", "Classes"),
    ("LmClassMembers", "Result"),
    ("PCB_GetNetClasses", "JsonItems"),
])
def test_a_reply_is_joined_once_not_grown_entry_by_entry(pcb, name, grown):
    body = pcb[name]
    assert not re.search(rf"\b{grown}\s*:=\s*{grown}\s*\+", body), name
    assert ".Text" in body, f"{name} joins its entries with a list's Text"


@pytest.mark.parametrize("name", ["LmPadsSection", "LmCopperSection"])
def test_pads_and_copper_tell_the_reader_a_big_page_is_cheap(pcb, name):
    assert '"big_pages":true' in pcb[name]


@pytest.mark.parametrize("name, obj", [("LmPadsSection", "Pad"),
                                       ("LmCopperSection", "Obj")])
def test_earlier_pages_are_stepped_over_with_nothing_else(pcb, name, obj):
    lean = (rf"While \({obj} <> Nil\) And \(Idx < Offset\) Do\s+Begin\s+"
            rf"Inc\(Idx\);\s+{obj} := Iter\.NextPCBObject;\s+End;")
    assert re.search(lean, pcb[name]), name


def test_the_lean_skip_is_only_taken_when_every_object_counts(pcb):
    """Selected only, or one wide layer, leaves objects out of Idx."""
    body = pcb["LmCopperSection"]
    at = body.index("While (Obj <> Nil) And (Idx < Offset)")
    assert body[:at].rstrip().endswith("If Not SelectedOnly And Not Wide Then")


def test_a_class_is_read_from_its_own_list_and_asked_when_that_fails(pcb):
    body = pcb["LmClassMembers"]
    assert "Cls.MemberName[I]" in body
    assert "Cls.IsMember(Net)" in body
    ask = re.search(r"Ask := (.*?);", body, re.S).group(1)
    for reason in ("Exact", "Repeated", "Super", "Listed > 0", "Not Works"):
        assert reason in ask, reason
    for name in ("LmClassesSection", "PCB_GetNetClasses"):
        assert "LmClassMembers(" in pcb[name], name
        assert "IsMember" not in pcb[name], f"{name} asks through LmClassMembers"


def test_the_slow_class_read_can_still_be_asked_for(pcb):
    assert ("LmClassesSection(Board, LowerCase(ExtractJsonValue(Params, "
            "'exact')) = 'true')") in pcb["PCB_GetLayoutModel"]
    assert "ExtractJsonValue(Params, 'exact')" in pcb["PCB_GetNetClasses"]


def test_repeated_names_are_escaped_once_per_read(pcb):
    assert "LmEsc(Safe, LmNetName(Obj))" in pcb["LmCopperSection"]
    assert "LmEsc(Safe, LmNetName(Pad))" in pcb["LmPadsSection"]
    assert "LmEsc(Safe, Comp.Pattern)" in pcb["LmComponentsSection"]
    esc = pcb["LmEsc"]
    # A sorted lookup compares by locale; a hit is confirmed exactly.
    assert "(Length(Safe[I]) = Length(Name)) And (Pos(Name, Safe[I]) = 1)" in esc
    assert "EscapeJsonString(Name)" in esc


def test_no_list_is_cleared_with_the_undeclared_clear(pcb):
    for name in ("LmClassMembers", "LmClassesSection", "PCB_GetNetClasses"):
        assert ".Clear" not in pcb[name], name
