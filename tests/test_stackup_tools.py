# SPDX-License-Identifier: Apache-2.0
"""The stackup tools, their CLI verbs, and the Pascal parse they rely on.

The last of those is the one that matters most. Setting a stackup goes
through PCB_ModifyLayer, which parsed copper thickness with
StrToIntDef: 1 oz copper is 1.378 mil, StrToIntDef('1.378', 0) returns
the DEFAULT, and asking for 1 oz silently set the layer to ZERO. The
tool reported success. A guard here is cheap and the failure it catches
is invisible.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from eda_agent import cli
from eda_agent.tools.registry import ToolRegistry
from eda_agent.tools.stackup import register_stackup_tools

PASCAL = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "PCB.pas"


# ---- the Pascal parse ---------------------------------------------------

def test_layer_thickness_is_parsed_as_a_float_not_an_integer():
    """No real foil weight is a whole number of mils: 1 oz is 1.378 and
    0.5 oz is 0.689. Parsing these as integers does not round them, it
    returns the default, so a 1 oz request set the copper to zero and
    reported success."""
    source = PASCAL.read_text(encoding="utf-8", errors="replace")
    for variable in ("ThickStr", "HeightStr"):
        assert f"StrToIntDef({variable}" not in source, (
            f"{variable} is parsed as an integer again; a fractional mil "
            f"value will silently become the default")
        assert f"StrToFloatDef({variable}" in source, variable


def test_the_thickness_setters_keep_sub_mil_resolution():
    """MilsToCoord takes an Integer and truncates; MilsToCoordF keeps
    1e-4 mil. Parsing a float and then passing it through the integer
    converter would undo the fix one line later."""
    source = PASCAL.read_text(encoding="utf-8", errors="replace")
    for line in source.splitlines():
        if "CopperThickness :=" in line or "DielectricHeight :=" in line:
            if "StrToFloatDef" in line:
                assert "MilsToCoordF(" in line, line.strip()


def test_the_read_back_check_uses_the_same_parse():
    """PCB_ModifyLayer verifies what it wrote by comparing the read-back
    against the request. Comparing a float read-back against an integer
    parse of the same string reports every fractional value as
    rejected."""
    source = PASCAL.read_text(encoding="utf-8", errors="replace")
    for line in source.splitlines():
        if "ThickBack -" in line or "HeightBack -" in line:
            assert "StrToFloatDef" in line, line.strip()


def test_the_script_version_was_bumped_for_a_bridge_change():
    """A Pascal fix only reaches a user who redeploys, and the version
    is how `eda-agent health` tells them to."""
    main = PASCAL.parent / "Main.pas"
    version = re.search(r"SCRIPT_VERSION\s*=\s*'([^']+)'",
                        main.read_text(encoding="utf-8", errors="replace"))
    assert version, "SCRIPT_VERSION is gone from Main.pas"
    assert version.group(1) > "2026.10.13.5", (
        "the stackup parse changed in PCB.pas but SCRIPT_VERSION did not "
        "move, so a user running the old script gets the old behaviour "
        "with no warning")


def test_the_bundle_was_rebuilt_from_the_modules():
    """Altium_MCP.pas is generated. Editing a module and forgetting to
    run build.py ships the old code to anyone using the single file."""
    bundle = (PASCAL.parent / "Altium_MCP.pas").read_text(
        encoding="utf-8", errors="replace")
    assert "StrToIntDef(ThickStr" not in bundle
    assert "StrToFloatDef(ThickStr" in bundle


# ---- the tools ----------------------------------------------------------

class _Bridge:
    def __init__(self, live=None, fail_on=None):
        self.calls = []
        self.live = live or {}
        self.fail_on = fail_on

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        if self.fail_on and command == self.fail_on:
            raise OSError("Altium is not answering")
        if command == "pcb.get_layer_stackup":
            return self.live
        if (self.fail_on
                and params and params.get("layer") == self.fail_on):
            raise OSError("that layer is locked")
        return {"success": True}


def _tools(monkeypatch=None, bridge=None, altium=True):
    if bridge is not None:
        import eda_agent.bridge as bridge_pkg
        monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: bridge,
                            raising=False)
    registry = ToolRegistry()
    register_stackup_tools(registry, altium=altium)
    return registry


def _live_4layer():
    return {"board_name": "B.PcbDoc", "layers": [
        {"name": "Top", "copper_thickness_mils": 1.378,
         "dielectric_type": "Prepreg", "dielectric_height_mils": 8.2835,
         "dielectric_constant": 4.6},
        {"name": "Inner1", "copper_thickness_mils": 0.5984,
         "dielectric_type": "Core", "dielectric_height_mils": 41.9291,
         "dielectric_constant": 4.6},
        {"name": "Inner2", "copper_thickness_mils": 0.5984,
         "dielectric_type": "Prepreg", "dielectric_height_mils": 8.2835,
         "dielectric_constant": 4.6},
        {"name": "Bottom", "copper_thickness_mils": 1.378},
    ]}


def test_the_vendor_list_is_reachable_as_a_tool():
    result = asyncio.run(_tools().get("design_list_fab_vendors").fn())
    assert result["ok"] and result["count"] >= 2
    assert all(v["source"] for v in result["vendors"])


def test_a_vendor_tool_reply_carries_the_caveats():
    result = asyncio.run(_tools().get("design_fab_vendor").fn("jlcpcb"))
    assert result["ok"] and result["warnings"]


def test_an_unknown_vendor_is_refused_by_the_tool():
    assert asyncio.run(
        _tools().get("design_fab_vendor").fn("nowhere"))["ok"] is False


def test_the_profile_tool_refuses_to_invent_a_rule():
    result = asyncio.run(
        _tools().get("design_fab_vendor_profile").fn("jlcpcb"))
    assert result["ok"] is False and result["missing"]


def test_comparing_against_a_matching_board_passes():
    result = asyncio.run(_tools().get("design_compare_stackup").fn(
        "jlcpcb", "JLC04161H-7628", _live_4layer()))
    assert result["ok"] and result["matches"]


def test_comparing_against_an_unbundled_stackup_lists_what_there_is():
    result = asyncio.run(_tools().get("design_compare_stackup").fn(
        "jlcpcb", "NOPE", _live_4layer()))
    assert result["ok"] is False and "JLC04161H-7628" in result["reason"]


def test_a_vendor_with_no_stackups_says_so_rather_than_erroring():
    result = asyncio.run(_tools().get("design_compare_stackup").fn(
        "pcbway", "anything", _live_4layer()))
    assert result["ok"] is False and "none at all" in result["reason"]


def test_the_altium_tools_only_exist_on_altium():
    assert _tools(altium=False).get("pcb_check_stackup") is None
    assert _tools(altium=True).get("pcb_check_stackup") is not None


def test_checking_the_open_board_reads_it_for_you(monkeypatch):
    bridge = _Bridge(live=_live_4layer())
    result = asyncio.run(_tools(monkeypatch, bridge).get(
        "pcb_check_stackup").fn("jlcpcb", "JLC04161H-7628"))
    assert result["ok"] and result["matches"]
    assert bridge.calls[0][0] == "pcb.get_layer_stackup"
    assert result["board_name"] == "B.PcbDoc"


def test_a_board_that_cannot_be_read_is_refused_not_raised(monkeypatch):
    bridge = _Bridge(fail_on="pcb.get_layer_stackup")
    result = asyncio.run(_tools(monkeypatch, bridge).get(
        "pcb_check_stackup").fn("jlcpcb", "JLC04161H-7628"))
    assert result["ok"] is False and "not answering" in result["reason"]


# ---- applying a template ------------------------------------------------

def test_applying_a_template_is_a_dry_run_by_default(monkeypatch):
    """A stack change can invalidate routing, so the default must not
    touch the board."""
    bridge = _Bridge(live=_live_4layer())
    result = asyncio.run(_tools(monkeypatch, bridge).get(
        "pcb_apply_stackup_template").fn("jlcpcb", "JLC04161H-7628"))
    assert result["ok"] and result["dry_run"] is True
    assert [c[0] for c in bridge.calls] == ["pcb.get_layer_stackup"]
    assert len(result["operations"]) == 4


def test_applying_sends_one_modify_per_copper_layer(monkeypatch):
    bridge = _Bridge(live=_live_4layer())
    result = asyncio.run(_tools(monkeypatch, bridge).get(
        "pcb_apply_stackup_template").fn("jlcpcb", "JLC04161H-7628",
                                         dry_run=False))
    assert result["ok"] and result["dry_run"] is False
    modifies = [c for c in bridge.calls if c[0] == "pcb.modify_layer"]
    assert len(modifies) == 4
    assert result["applied"] == ["Top", "Inner1", "Inner2", "Bottom"]


def test_the_operations_carry_fractional_mils(monkeypatch):
    """The whole point of the Pascal fix. If these round to integers
    here, the fix downstream buys nothing."""
    bridge = _Bridge(live=_live_4layer())
    result = asyncio.run(_tools(monkeypatch, bridge).get(
        "pcb_apply_stackup_template").fn("jlcpcb", "JLC04161H-7628"))
    top = result["operations"][0]
    assert top["copper_thickness_mils"] == pytest.approx(1.378, abs=0.001)
    assert top["dielectric_height_mils"] == pytest.approx(8.2835, abs=0.001)


def test_the_bottom_layer_gets_no_dielectric(monkeypatch):
    """There is nothing beneath it, and sending a height for it would
    add a ply the template does not have."""
    bridge = _Bridge(live=_live_4layer())
    result = asyncio.run(_tools(monkeypatch, bridge).get(
        "pcb_apply_stackup_template").fn("jlcpcb", "JLC04161H-7628"))
    assert "dielectric_height_mils" not in result["operations"][-1]


def test_a_different_layer_count_refuses_rather_than_restructuring(
        monkeypatch):
    """Adding a copper layer means inventing the name Altium's own stack
    manager would have chosen, on a board that has routing on it."""
    live = _live_4layer()
    live["layers"] = live["layers"][:2]
    bridge = _Bridge(live=live)
    result = asyncio.run(_tools(monkeypatch, bridge).get(
        "pcb_apply_stackup_template").fn("jlcpcb", "JLC04161H-7628",
                                         dry_run=False))
    assert result["ok"] is False
    assert "Layer Stack Manager" in result["reason"]
    assert not [c for c in bridge.calls if c[0] == "pcb.modify_layer"]


def test_a_failure_part_way_reports_what_did_change(monkeypatch):
    """A half-applied stackup is worse than none, and the reader has to
    know which half took."""
    bridge = _Bridge(live=_live_4layer(), fail_on="Inner2")
    result = asyncio.run(_tools(monkeypatch, bridge).get(
        "pcb_apply_stackup_template").fn("jlcpcb", "JLC04161H-7628",
                                         dry_run=False))
    assert result["ok"] is False
    assert result["applied"] == ["Top", "Inner1"]
    assert "PART applied" in result["warning"]


# ---- the CLI verbs ------------------------------------------------------

def test_stackup_vendors_lists_them(capsys):
    assert cli.cmd_stackup_vendors() == 0
    out = capsys.readouterr().out
    assert "jlcpcb" in out and "https://" in out


def test_stackup_show_marks_what_is_not_published(capsys):
    assert cli.cmd_stackup_show("jlcpcb") == 0
    assert "NOT PUBLISHED" in capsys.readouterr().out


def test_stackup_show_of_an_unknown_vendor_exits_one(capsys):
    assert cli.cmd_stackup_show("nowhere") == 1
    assert "nowhere" in capsys.readouterr().err


def test_stackup_profile_refuses_and_shows_the_command_to_fix_it(capsys):
    assert cli.cmd_stackup_profile("jlcpcb") == 1
    err = capsys.readouterr().err
    assert "--fill min_hole_to_hole_mils=" in err


def test_stackup_profile_accepts_the_fill_and_emits_json(capsys):
    code = cli.cmd_stackup_profile(
        "jlcpcb", fill=["min_hole_to_hole_mils=17.7",
                        "min_mask_sliver_mils=4"])
    assert code == 0
    profile = json.loads(capsys.readouterr().out)
    assert profile["min_hole_to_hole_mils"] == 17.7
    assert "supplied by the caller" in profile["source"]


@pytest.mark.parametrize("bad", ["nonsense", "min_track_mils=thin"])
def test_a_malformed_fill_exits_two(bad, capsys):
    assert cli.cmd_stackup_profile("jlcpcb", fill=[bad]) == 2
    assert capsys.readouterr().err


def test_laminates_shows_both_dielectric_constants(capsys):
    assert cli.cmd_laminates() == 0
    out = capsys.readouterr().out
    assert "er(proc)" in out and "er(design)" in out
    assert "3.48" in out and "3.66" in out


def test_the_json_forms_are_machine_readable(capsys):
    for fn in (lambda: cli.cmd_stackup_vendors(as_json=True),
               lambda: cli.cmd_laminates(as_json=True)):
        assert fn() == 0
        json.loads(capsys.readouterr().out)
