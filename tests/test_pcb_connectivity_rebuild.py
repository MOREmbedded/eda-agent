# SPDX-License-Identifier: Apache-2.0
"""Connectivity must be recomputed after programmatic copper changes.

Live failure: after pcb_place_tracks laid segments whose endpoints landed
exactly on pad centres, pcb_get_unrouted_nets still reported those nets as
unrouted -- one net with 4 pads reported 2 unrouted connections while all 5
of its segments demonstrably joined the pads. Other nets in the SAME batch
reported correctly. obj_refresh_document (a Zoom Redraw) did not clear it,
because a redraw repaints without recomputing.

Two halves: behaviour through the simulator's mirror, and source-contract
guards on the .pas for the parts not reachable from Python.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.altium_simulator import AltiumSimulator, MockBoard


REPO_ROOT = Path(__file__).resolve().parents[1]
PCB_PAS = REPO_ROOT / "scripts" / "altium" / "PCB.pas"
PCBGENERIC_PAS = REPO_ROOT / "scripts" / "altium" / "PCBGeneric.pas"
GENERIC_PAS = REPO_ROOT / "scripts" / "altium" / "Generic.pas"


@pytest.fixture()
def sim(tmp_path):
    s = AltiumSimulator(str(tmp_path))
    s.board = MockBoard("b.PcbDoc", nets=["NetL1001_2", "GND"])
    return s


def _call(sim, action, params):
    payload = json.loads(sim._handle_pcb(action, params, "r" * 32))
    assert payload["success"] is True, payload
    return payload["data"]


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_place_tracks_rebuilds_connectivity(sim):
    before = sim.board.connectivity_rebuilds
    res = _call(sim, "place_tracks",
                {"tracks": "0,0,100,0,10,TopLayer,NetL1001_2|"
                           "100,0,100,50,10,TopLayer,NetL1001_2"})
    assert res["placed"] == 2
    assert res["connectivity_rebuilt"] is True
    assert sim.board.connectivity_rebuilds == before + 1


def test_no_op_batch_does_not_pay_for_a_rebuild(sim):
    before = sim.board.connectivity_rebuilds
    res = _call(sim, "place_tracks", {"tracks": "bad,,,"})
    assert res["placed"] == 0
    assert res["connectivity_rebuilt"] is False
    assert sim.board.connectivity_rebuilds == before


def test_unknown_net_name_is_counted_not_swallowed(sim):
    # A segment naming a net the board does not have is placed netless, and
    # that is the usual reason a net still reads as unrouted afterwards.
    res = _call(sim, "place_tracks",
                {"tracks": "0,0,100,0,10,TopLayer,NO_SUCH_NET"})
    assert res["placed"] == 1
    assert res["nets_not_found"] == 1


def test_get_unrouted_nets_rebuilds_by_default(sim):
    before = sim.board.connectivity_rebuilds
    res = _call(sim, "get_unrouted_nets", {})
    assert res["connectivity_rebuilt"] is True
    assert sim.board.connectivity_rebuilds == before + 1


def test_get_unrouted_nets_can_skip_the_rebuild(sim):
    before = sim.board.connectivity_rebuilds
    res = _call(sim, "get_unrouted_nets", {"rebuild": "false"})
    assert res["connectivity_rebuilt"] is False
    assert sim.board.connectivity_rebuilds == before


def test_explicit_rebuild_command_exists(sim):
    before = sim.board.connectivity_rebuilds
    res = _call(sim, "rebuild_connectivity", {})
    assert res["rebuilt"] is True
    assert sim.board.connectivity_rebuilds == before + 1


# ---------------------------------------------------------------------------
# Source contract
# ---------------------------------------------------------------------------


def _handler(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    start = text.index(f"Function {name}(")
    end = text.index("\nFunction ", start + 1)
    return text[start:end]


def test_place_tracks_registers_each_track_individually():
    src = _handler(PCB_PAS, "PCB_PlaceTracks")
    # The regression: one board-level broadcast carrying NO object address.
    assert "PCBM_BoardRegisteration, c_NoEventData" not in src
    assert "PCBM_BoardRegisteration, Track.I_ObjectAddress" in src


def test_place_tracks_joins_the_net_primitive_list():
    src = _handler(PCB_PAS, "PCB_PlaceTracks")
    assert "BindPrimitiveToNet(FoundNet, Track)" in src


def test_place_tracks_rebuilds_after_postprocess_not_inside_it():
    # A rebuild issued while PCBServer is still mid-transaction cannot see
    # the finished state. The edit now closes through PcbEndEdit, which
    # reaches PostProcess only at the outermost level, so the handler must
    # hold exactly one pair and rebuild after it.
    src = _handler(PCB_PAS, "PCB_PlaceTracks")
    assert src.count("PcbBeginEdit(0)") == src.count("PcbEndEdit(0)") == 1
    assert src.index("PcbEndEdit(0)") < src.index(
        "RebuildPCBConnectivity(Board)")


def test_mutating_pcb_handlers_call_the_shared_rebuild():
    for path, name in (
        (PCB_PAS, "PCB_PlaceTracks"),
        (PCB_PAS, "PCB_DeleteObject"),
        (PCB_PAS, "PCB_GetUnroutedNets"),
        (GENERIC_PAS, "Gen_BatchDelete"),
    ):
        assert "RebuildPCBConnectivity" in _handler(path, name), name


def test_rebuild_helper_does_more_than_redraw():
    # obj_refresh_document issues a Zoom Redraw and did NOT clear the stale
    # ratsnest; the helper must actually run a connectivity process.
    src = PCBGENERIC_PAS.read_text(encoding="utf-8", errors="replace")
    helper = src[src.index("Function RebuildPCBConnectivity"):]
    assert "PCB:UpdateConnectivity" in helper
    assert "ViewManager_FullUpdate" in helper
    assert "Zoom" not in helper.split("End;")[0]


def test_batch_delete_only_rebuilds_when_a_pcb_op_ran():
    src = _handler(GENERIC_PAS, "Gen_BatchDelete")
    # A schematic-only batch must not pay for a board connectivity pass.
    assert "If PCBTouched Then" in src
    assert "If IsPCB Then PCBTouched := True" in src
