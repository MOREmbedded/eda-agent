# SPDX-License-Identifier: Apache-2.0
"""Offline cover for the silent-truncation fix in the PCB bulk getters.

The failure this exists to prevent: on a 1087-pad board
``pcb_get_pad_properties`` returned ``{"pads":[...500...],"count":500}``
with no indication that 587 pads were missing. A caller reasoned about a
component that had simply fallen past the cut and concluded its pads had
no nets. A cap that produces confidently wrong conclusions is worse than
no data at all.

Two halves, mirroring the house pattern in test_pcb_collision_check.py:

1. A Python mirror of the windowing arithmetic in PCB.pas
   PCB_GetPadProperties, run through the shared simulator so the wire
   shape is exercised too.
2. Source-contract guards on the .pas itself for the parts not reachable
   from Python -- that the bare ``Break`` at the cap is gone, and that the
   response carries the fields a caller needs to detect a short answer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.altium_simulator import AltiumSimulator, MockBoard


REPO_ROOT = Path(__file__).resolve().parents[1]
PCB_PAS = REPO_ROOT / "scripts" / "altium" / "PCB.pas"


# ---------------------------------------------------------------------------
# Behaviour, through the simulator's mirror of the handler
# ---------------------------------------------------------------------------


@pytest.fixture()
def sim(tmp_path):
    s = AltiumSimulator(str(tmp_path))
    s.board = MockBoard("big.PcbDoc")
    # 1087 pads over 200 components, the live board's shape. R84 sits well
    # past the 500-pad cut, which is exactly where the wrong call was made.
    s.board.pads = [
        {"name": str((i % 4) + 1), "component": f"R{i // 4}",
         "net": "GND" if i % 3 == 0 else f"NET{i}", "layer": "TopLayer"}
        for i in range(1087)
    ]
    return s


def _pads(sim, **params):
    raw = sim._handle_pcb("get_pad_properties", params, "r" * 32)
    payload = json.loads(raw)
    assert payload["success"] is True, payload
    return payload["data"]


def test_default_window_announces_that_it_truncated(sim):
    res = _pads(sim)
    assert res["returned"] == 500
    assert res["count"] == 500          # legacy field keeps its meaning
    assert res["total_matching"] == 1087
    assert res["total_pads_on_board"] == 1087
    assert res["truncated"] is True
    assert res["next_offset"] == 500


def test_paging_reaches_pad_501_and_beyond(sim):
    collected = []
    offset, guard = 0, 0
    while guard < 10:
        guard += 1
        page = _pads(sim, offset=str(offset), limit="500")
        collected.extend(page["pads"])
        if not page["truncated"]:
            break
        offset = page["next_offset"]
    assert len(collected) == 1087
    # The component that was wrongly declared netless is reachable.
    assert any(p["component"] == "R84" for p in collected)


def test_designator_filter_reaches_a_component_past_the_cap(sim):
    # The cheap route a caller actually wants: ask for the part, not the
    # board. R84's pads are past index 500 in board order.
    res = _pads(sim, designator="R84")
    assert res["truncated"] is False
    assert res["total_matching"] == 4
    assert res["returned"] == 4
    assert {p["component"] for p in res["pads"]} == {"R84"}


def test_designator_filter_accepts_a_comma_separated_list(sim):
    res = _pads(sim, designator="R84,R200")
    assert res["truncated"] is False
    assert {p["component"] for p in res["pads"]} == {"R84", "R200"}
    assert res["total_matching"] == 8


def test_designator_filter_tolerates_spaces_in_the_list(sim):
    res = _pads(sim, designator="R84, R200")
    assert {p["component"] for p in res["pads"]} == {"R84", "R200"}


def test_net_filter_counts_every_match_not_just_the_window(sim):
    # GND is on ~1/3 of the pads, comfortably more than a 10-pad window.
    res = _pads(sim, net="GND", limit="10")
    assert res["returned"] == 10
    assert res["total_matching"] == 363
    assert res["truncated"] is True
    assert all(p["net"] == "GND" for p in res["pads"])


def test_a_complete_answer_is_not_flagged_truncated(sim):
    res = _pads(sim, limit="5000")
    assert res["returned"] == 1087
    assert res["truncated"] is False
    assert res["next_offset"] == 1087


def test_limit_is_clamped_to_the_hard_ceiling(sim):
    for requested in ("0", "-1", "999999"):
        res = _pads(sim, limit=requested)
        assert res["limit"] == 5000


def test_offset_past_the_end_returns_an_empty_untruncated_window(sim):
    res = _pads(sim, offset="5000")
    assert res["pads"] == []
    assert res["returned"] == 0
    assert res["total_matching"] == 1087
    assert res["truncated"] is False


def test_filter_matching_nothing_is_honest_about_it(sim):
    res = _pads(sim, designator="NOSUCHPART")
    assert res["pads"] == []
    assert res["total_matching"] == 0
    assert res["truncated"] is False


# ---------------------------------------------------------------------------
# Source contract on PCB.pas
# ---------------------------------------------------------------------------


def _handler_source(name: str) -> str:
    text = PCB_PAS.read_text(encoding="utf-8", errors="replace")
    start = text.index(f"Function {name}(Params : String")
    end = text.index("\nFunction ", start + 1)
    return text[start:end]


def test_pas_pad_handler_no_longer_breaks_at_the_cap():
    src = _handler_source("PCB_GetPadProperties")
    # The exact line that caused the bug.
    assert "If Count >= 500 Then Break" not in src
    # Nothing else may cut the iteration short either: the sweep has to
    # reach the last pad for total_matching to be true.
    assert not re.search(r"Count\s*>=\s*\d+\s*Then\s*Break", src)


def test_pas_pad_handler_reports_the_truncation_fields():
    src = _handler_source("PCB_GetPadProperties")
    for field in ('"total_matching"', '"truncated"', '"next_offset"',
                  '"returned"', '"offset"', '"limit"'):
        assert field in src, f"missing {field} in the response"


def test_pas_pad_handler_counts_matches_outside_the_window():
    # Inc(Matched) must happen before the window test, or total_matching
    # collapses back into the emitted count and the flag lies.
    src = _handler_source("PCB_GetPadProperties")
    assert src.index("Inc(Matched)") < src.index("Emit :=")


def test_pas_clearance_violations_still_reports_the_true_total():
    # The cap must not change the TOTAL: Count is incremented for every
    # violation found, outside whatever guard decides which rows are
    # emitted, so violation_count stays the real figure while the rows
    # are paged. Asserted as that property rather than as one spelling
    # of the guard -- the handler has since gained offset/limit paging,
    # and a literal "If Count < 200" check failed on an implementation
    # that satisfies the requirement perfectly well.
    src = _handler_source("PCB_GetClearanceViolations")
    assert 'violation_count' in src and 'IntToStr(Count)' in src, (
        "violation_count must report Count, the unguarded total")

    emit_guard = next((l for l in src.splitlines()
                       if "Returned < Limit" in l or "Count < 200" in l), "")
    assert emit_guard, "no emit guard found; has the cap gone entirely?"

    lines = [l.strip() for l in src.splitlines()]
    inc_count = next(i for i, l in enumerate(lines) if l == "Inc(Count);")
    guard = next(i for i, l in enumerate(lines)
                 if "Returned < Limit" in l or "Count < 200" in l)
    # Same indentation depth as the guard, not nested inside it.
    raw = src.splitlines()
    depth = lambda i: len(raw[i]) - len(raw[i].lstrip())
    assert depth(inc_count) <= depth(guard), (
        "Inc(Count) is nested inside the emit guard, so violation_count "
        "would report the page size rather than the true total")
