# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Fab capabilities, stack-ups and laminates, each figure the fab's own.

The data was read off JLCPCB's and PCBWay's capability pages, JLCPCB's
impedance page (parsed by scripts/fab_data_jlcpcb_stackups.py) and the
makers' datasheets on 2026-10-22. These tests hold the data to its rules
(a quote for every figure, a reason for every gap, the number inside its
own quote) and the code to the build it is asked about.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import re
from pathlib import Path

import pytest

from eda_agent.design import fab_library as fl
from eda_agent.design.fab_profile import Stackup, StackupLayer
from eda_agent.design.impedance_sizing import z0_microstrip, z0_stripline
from eda_agent.tools import pcb as pcb_tools

_ROOT = Path(__file__).resolve().parents[1]
_DATA = _ROOT / "src" / "eda_agent" / "design" / "fab_data"


def _vendor_files():
    for p in sorted(_DATA.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if "tiers" in data:
            yield data


# --- the data's own rules ------------------------------------------------

def test_every_figure_is_quoted_or_says_why_it_is_missing():
    for data in _vendor_files():
        assert data["source"]["url"].startswith("https://")
        dt.date.fromisoformat(data["source"]["retrieved"])
        for tier, figs in data["tiers"].items():
            for key, options in figs.items():
                for f in options:
                    where = (data["vendor"], tier, key)
                    if "unpublished" in f:
                        assert f["unpublished"].strip(), where
                        assert "mm" not in f and "mil" not in f, where
                    else:
                        assert f.get("quote", "").strip(), where


def test_each_published_number_is_inside_its_own_quote():
    """A typo in a transcribed value would otherwise look exactly like
    the fab's figure. A derived value must say how it was derived."""
    for data in _vendor_files():
        for tier, figs in data["tiers"].items():
            for key, options in figs.items():
                for f in options:
                    unit = "mm" if "mm" in f else "mil" if "mil" in f else None
                    if unit is None:
                        continue
                    if "derived" in f:
                        continue
                    numbers = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", f["quote"])}
                    assert f[unit] in numbers, (data["vendor"], tier, key, f[unit])


def test_a_derived_value_follows_from_its_published_parts():
    ring = fl.vendor("JLCPCB")["tiers"]["standard"]["min_annular_ring"][0]
    assert "0.15/0.25mm" in ring["quote"] and ring["mm"] == pytest.approx((0.25 - 0.15) / 2)


# --- profiles for one build ----------------------------------------------

def test_the_minimum_track_follows_the_build():
    two = fl.fab_profile("JLCPCB", copper_layers=2)["profile"]
    four = fl.fab_profile("JLCPCB", copper_layers=4)["profile"]
    heavy = fl.fab_profile("JLCPCB", copper_layers=4, outer_oz=2)["profile"]
    assert two.min_track_mils == pytest.approx(3.937, abs=1e-3)      # 0.10 mm
    assert four.min_track_mils == pytest.approx(3.543, abs=1e-3)     # 0.09 mm
    assert heavy.min_track_mils == pytest.approx(5.906, abs=1e-3)    # 0.15 mm
    assert heavy.min_mask_sliver_mils > four.min_mask_sliver_mils


def test_every_value_in_a_profile_has_its_source():
    got = fl.fab_profile("JLCPCB", copper_layers=4)
    dumped = got["profile"].model_dump()
    for field in fl._FIELDS:
        assert dumped[field] > 0
        assert got["provenance"][field].get("quote"), field
    assert "jlcpcb.com" in got["profile"].source
    assert {s.name for s in got["profile"].stackups} >= {"JLC04161H-7628"}


def test_an_unpublished_figure_refuses_the_profile_and_names_it():
    got = fl.fab_profile("PCBWay", copper_layers=4)
    assert got["ok"] is False
    assert {m["field"] for m in got["missing"]} == {
        "min_hole_to_hole_mils", "min_mask_sliver_mils"}
    assert all(m["reason"] for m in got["missing"])


def test_a_filled_figure_is_recorded_as_the_callers():
    got = fl.fab_profile("PCBWay", copper_layers=4, fill={
        "min_hole_to_hole_mils": 10, "min_mask_sliver_mils": 4})
    assert got["ok"]
    assert got["provenance"]["min_hole_to_hole_mils"] == {"supplied_by": "caller", "mils": 10.0}
    assert "caller supplied" in got["profile"].source


def test_a_layer_count_the_fab_does_not_list_is_refused():
    assert fl.fab_profile("PCBWay", copper_layers=16)["ok"] is False
    assert fl.fab_profile("PCBWay", copper_layers=16, tier="advanced_normal").get(
        "missing"), "advanced_normal lists 16 but has no ink silk figure"


def test_old_data_says_so_in_the_result():
    retrieved = dt.date.fromisoformat(fl.vendor("JLCPCB")["source"]["retrieved"])
    fresh = fl.fab_profile("JLCPCB", copper_layers=4, today=retrieved)
    old = fl.fab_profile("JLCPCB", copper_layers=4,
                         today=retrieved + dt.timedelta(days=fl.STALE_AFTER_DAYS + 1))
    assert fresh["age"]["stale"] is False
    assert old["age"]["stale"] is True and old["age"]["stale_note"]


# --- stack-ups -------------------------------------------------------------

def test_every_shipped_stackup_is_a_valid_board():
    names = [t["name"] for t in fl.stackup_templates("JLCPCB")]
    assert len(names) == 36
    for name in names:
        got = fl.stackup_template(name)
        n = got["stackup"].layers
        copper = [p for p in n if p.kind == "copper"]
        m = re.match(r"JLC0(\d)", name)
        if m:
            assert len(copper) == int(m.group(1)), name
        assert 1.4 <= got["total_mm"] <= 1.7, (name, got["total_mm"])
        for p in got["plies"]:
            if p["kind"] != "copper":
                assert p.get("er") or p.get("er_missing"), (name, p)


def test_a_dk_the_page_does_not_give_stays_missing():
    got = fl.stackup_template("JLC06161H-2116")
    assert got["missing_er"] and all("2313" in m for m in got["missing_er"])
    assert not any(p.er for p in got["stackup"].layers if "2313" in p.name)


def _parser():
    spec = importlib.util.spec_from_file_location(
        "fab_data_jlcpcb_stackups", _ROOT / "scripts" / "fab_data_jlcpcb_stackups.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PAGE = (
    "<p>Prepreg dielectric constant: Prepreg type Dielectric constant 7628 4.4 "
    "2116 4.16 2.Solder mask Parameters Coating Above Substrate C1 Coating Above "
    "Trace C2 Coating Between Traces C3 Coating Dielectric CEr 1.2mil 0.6mil 1.2mil "
    "3.8 3. Core dielectric constant Core dielectric constant 4.6 For your</p>"
    "<p>4-Layer Impedance Control Stackup Thickness 1.6mm</p>"
    "<p>1) JLC04161H-TEST Stackup</p> Layer Material Type Thickness "
    "Top Layer Copper 0.035mm Prepreg 7628*1 0.21040mm "
    "Inner Layer L2 Core Inner Layer L3 Copper Core Copper 0.0152mm 0.4mm 0.0152mm "
    "0.4mm H/HOZ without copper Prepreg 2116*1 0.11640mm Core Core 0.3mm 0.3 H/HOZ "
    "without copper Prepreg 2116*1 0.11640mm Bottom Layer Copper 0.035mm "
    "<footer>Online Chat</footer>")


def test_the_parser_keeps_a_core_with_no_copper_of_its_own():
    """The first parse dropped the bare core between two prepregs and
    still produced a board, 0.7 mm thinner."""
    plies = _parser().parse(_PAGE)["stackups"][0]["plies"]
    assert [(p["kind"], p["thickness_mm"]) for p in plies] == [
        ("copper", 0.035), ("prepreg", 0.2104), ("copper", 0.0152), ("core", 0.4),
        ("copper", 0.0152), ("prepreg", 0.1164), ("core", 0.3),
        ("prepreg", 0.1164), ("copper", 0.035)]
    assert [p["er"] for p in plies if p["kind"] != "copper"] == [4.4, 4.6, 4.16, 4.6, 4.16]


def test_the_parser_refuses_a_row_it_does_not_know():
    page = _PAGE.replace("Prepreg 2116*1 0.11640mm Bottom",
                         "Resin Fill 0.05mm Prepreg 2116*1 0.11640mm Bottom")
    with pytest.raises(SystemExit, match="unparsed"):
        _parser().parse(page)


# --- laminates -------------------------------------------------------------

def test_design_dk_is_the_design_value_never_the_process_value():
    assert fl.design_dk("RO4350B")["dk"] == 3.66
    lam = fl.laminate("RO4350B")
    assert {d["kind"] for d in lam["dk"]} == {"process", "design"}


def test_a_typical_dk_is_taken_at_the_frequency_asked():
    assert fl.design_dk("370HR", 1.0)["dk"] == 4.17
    assert fl.design_dk("370HR", 10.0)["dk"] == 3.92
    for lam in json.loads((_DATA / "laminates.json").read_text(encoding="utf-8"))["laminates"]:
        assert lam["source"]["url"].startswith("https://") and lam["source"]["document"]
        for d in lam["dk"]:
            assert d["kind"] in ("process", "design", "typical") and d["method"]


# --- comparing and applying ------------------------------------------------

def _board_from(name, *, tweak=None):
    calls = fl.plan_apply([{"name": f"L{i}", "order": i} for i in range(1, 5)], name)["calls"]
    layers = []
    for i, c in enumerate(calls, 1):
        layers.append({"name": c["layer"], "order": i,
                       "copper_thickness_mils": c["copper_thickness_mils"],
                       "dielectric_type": c.get("dielectric_type", "none"),
                       "dielectric_height_mils": c.get("dielectric_height_mils", 0),
                       "dielectric_constant": c.get("dielectric_constant", 0)})
    if tweak:
        tweak(layers)
    return {"layers": layers, "layer_count": len(layers)}


def test_a_board_built_from_the_template_matches_it():
    got = fl.compare_stackup(_board_from("JLC04161H-7628A"), "JLC04161H-7628A")
    assert got["matches"], got["findings"]


def test_a_multi_ply_gap_is_summed_before_it_is_compared():
    calls = fl.plan_apply([{"name": f"L{i}", "order": i} for i in range(1, 5)],
                          "JLC04161H-7628A")["calls"]
    # 7628 0.218 mm + 1080 0.0764 mm under the top copper.
    assert calls[0]["dielectric_height_mils"] == pytest.approx((0.218 + 0.0764) / 0.0254, abs=1e-3)
    er = (0.218 * 4.4 + 0.0764 * 3.91) / (0.218 + 0.0764)
    assert calls[0]["dielectric_constant"] == pytest.approx(er, abs=1e-3)


def test_a_wrong_height_is_found_and_a_count_mismatch_is_one_finding():
    def thin(layers):
        layers[0]["dielectric_height_mils"] *= 0.8
    got = fl.compare_stackup(_board_from("JLC04161H-7628", tweak=thin), "JLC04161H-7628")
    assert [f["what"] for f in got["findings"]] == ["dielectric height below"]
    six = fl.compare_stackup(_board_from("JLC04161H-7628"), "JLC06161H-7628")
    assert len(six["findings"]) == 1 and six["findings"][0]["what"] == "copper layer count"


def test_apply_refuses_a_different_copper_count():
    got = fl.plan_apply([{"name": "Top", "order": 1}, {"name": "Bottom", "order": 2}],
                        "JLC04161H-7628")
    assert got["ok"] is False and "Layer Stack Manager" in got["reason"]


# --- impedance from a stack-up ---------------------------------------------

def test_outer_widths_round_trip_to_the_target():
    got = fl.impedance_widths("JLC04161H-7628", [{"ohms": 50}])
    top = got["layers"][0]
    w = top["results"][0]["width_mils"]
    oz = 0.035 / 0.0254 / 1.378
    assert z0_microstrip(w, top["height_mils"], top["er"], copper_oz=oz) == pytest.approx(50, abs=0.2)


def test_an_off_centre_inner_layer_is_reported_not_sized():
    got = fl.impedance_widths("JLC04161H-7628", [{"ohms": 50}])
    assert "asymmetric" in got["layers"][1]["not_sized"]


def test_a_centred_inner_layer_is_sized_as_stripline():
    stk = Stackup(name="sym", layers=[
        StackupLayer(name="T", kind="copper", thickness_mils=1.378, copper_oz=1),
        StackupLayer(name="p1", kind="prepreg", thickness_mils=8, er=4.0),
        StackupLayer(name="S", kind="copper", thickness_mils=0.689, copper_oz=0.5),
        StackupLayer(name="p2", kind="prepreg", thickness_mils=8, er=4.0),
        StackupLayer(name="B", kind="copper", thickness_mils=1.378, copper_oz=1)])
    row = fl.impedance_widths(stk, [{"ohms": 50}])["layers"][1]
    assert row["geometry"] == "stripline"
    w = row["results"][0]["width_mils"]
    assert z0_stripline(w, row["height_mils"], 4.0, copper_oz=0.5) == pytest.approx(50, abs=0.2)


# The board-tool tests that stood here exercised this module's own
# pcb_check_stackup / pcb_apply_stackup_template wrappers. This fork keeps
# the pair in tools/stackup.py instead (tests/test_stackup_tools.py covers
# them), so the wrappers and their tests went together. Everything above
# tests fab_library itself, which design_get_stackup, design_list_stackups
# and design_get_laminate still read. Folding the two material datasets
# into one is its own change; see the CHANGELOG.
