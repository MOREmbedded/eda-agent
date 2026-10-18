# SPDX-License-Identifier: Apache-2.0
"""Bundled fab capabilities and stackup templates.

design/fab_profile.py ships zero capability numbers on purpose. This
module is the agreed relaxation of that, so most of these tests are
about the conditions the relaxation depends on: that every number
carries where it came from and when, that an unpublished figure stays
unpublished rather than being filled in, and that stale data says so.
"""
from __future__ import annotations

import json

import pytest

from eda_agent.design.stackup_library import (
    STALE_AFTER_DAYS,
    compare_stackup,
    data_dir,
    list_laminates,
    list_vendors,
    load_vendor,
    vendor_fab_profile,
)


# ---- provenance, which is the whole basis of shipping these ------------

def test_every_bundled_vendor_names_its_source_and_its_date():
    """Without both, a number here is indistinguishable from one
    somebody remembered."""
    vendors = list_vendors()
    assert vendors, "no vendor data is bundled at all"
    for vendor in vendors:
        assert vendor["source"].startswith("http"), vendor["vendor"]
        assert vendor["retrieved"], vendor["vendor"]
        assert vendor["age_days"] is not None, (
            f"{vendor['vendor']} has an unreadable retrieved date")


def test_a_vendor_reply_always_carries_its_caveats():
    loaded = load_vendor("jlcpcb")
    assert loaded["ok"]
    joined = " ".join(loaded["warnings"]).lower()
    assert "confirm before ordering" in joined
    assert loaded["source"] in joined


def test_staleness_is_reported_rather_than_left_to_the_reader(monkeypatch):
    """A fab changes its capabilities without changing this file."""
    import eda_agent.design.stackup_library as mod

    monkeypatch.setattr(mod, "_age_days", lambda _r: STALE_AFTER_DAYS + 1)
    loaded = mod.load_vendor("jlcpcb")
    assert loaded["stale"] is True
    assert any("STALE" in w for w in loaded["warnings"])


def test_an_unreadable_date_counts_as_stale_not_as_fresh(monkeypatch):
    import eda_agent.design.stackup_library as mod

    monkeypatch.setattr(mod, "_age_days", lambda _r: None)
    loaded = mod.load_vendor("jlcpcb")
    assert loaded["stale"] is True


# ---- what the vendor does not publish stays unpublished ----------------

def test_an_unpublished_figure_is_null_with_a_reason():
    """JLCPCB states no rigid hole-to-hole minimum and no mask sliver.
    Null, and the reason, rather than a plausible number."""
    loaded = load_vendor("jlcpcb")
    assert loaded["capabilities_mils"]["min_hole_to_hole_mils"] is None
    assert loaded["capabilities_mils"]["min_mask_sliver_mils"] is None
    assert "min_hole_to_hole_mm" in loaded["not_published"]
    assert loaded["not_published"]["min_hole_to_hole_mm"]


def test_null_is_said_to_be_neither_zero_nor_unlimited():
    joined = " ".join(load_vendor("jlcpcb")["warnings"])
    assert "not zero and not unlimited" in joined


def test_converting_to_a_fab_profile_refuses_rather_than_guessing():
    """A rule invented here would arrive looking exactly like one the
    fab published, and rule synthesis downstream cannot tell them
    apart."""
    result = vendor_fab_profile("jlcpcb")
    assert result["ok"] is False
    assert set(result["missing"]) == {"min_hole_to_hole_mils",
                                      "min_mask_sliver_mils"}
    assert "will guess" in result["reason"]
    # And says WHY each one is missing, so the reader knows to go ask.
    assert all(result["why"].values())


def test_the_caller_can_supply_what_the_fab_told_them():
    result = vendor_fab_profile(
        "jlcpcb", fill={"min_hole_to_hole_mils": 17.7,
                        "min_mask_sliver_mils": 4.0})
    assert result["ok"] is True
    assert result["profile"]["min_hole_to_hole_mils"] == 17.7


def test_a_caller_supplied_figure_is_recorded_as_theirs():
    """The provenance has to survive the merge, or the profile claims
    the fab published something it did not."""
    result = vendor_fab_profile("jlcpcb",
                                fill={"min_hole_to_hole_mils": 17.7,
                                      "min_mask_sliver_mils": 4.0})
    source = result["profile"]["source"]
    assert "supplied by the caller, not by the vendor" in source
    assert "min_hole_to_hole_mils" in source


@pytest.mark.parametrize("fill", [
    {"min_track_mils": 0}, {"min_track_mils": -1},
    {"min_track_mils": "thin"}, {"min_track_mils": True},
    {"not_a_rule": 5},
])
def test_a_bad_fill_is_refused(fill):
    assert vendor_fab_profile("jlcpcb", fill=fill)["ok"] is False


def test_the_result_validates_against_the_real_fab_profile_model():
    """Not a lookalike dict: it goes through load_fab_profile, so a
    bundled stackup whose copper count contradicts the layer counts is
    caught here and not by a user."""
    from eda_agent.design.fab_profile import load_fab_profile

    built = vendor_fab_profile("jlcpcb",
                               fill={"min_hole_to_hole_mils": 17.7,
                                     "min_mask_sliver_mils": 4.0})
    assert load_fab_profile(built["profile"])["ok"] is True


# ---- the data files themselves -----------------------------------------

@pytest.mark.parametrize("path", sorted(data_dir().glob("*.json")),
                         ids=lambda p: p.stem)
def test_every_bundled_file_is_valid_json(path):
    assert json.loads(path.read_text(encoding="utf-8"))


def test_a_bundled_stackup_adds_up_to_the_thickness_it_claims():
    """JLC04161H-7628 is a 1.6 mm board; its plies must sum to roughly
    that, or one of them was transcribed wrong."""
    stackup = load_vendor("jlcpcb")["stackups"][0]
    total = sum(ply["thickness_mm"] for ply in stackup["layers"])
    assert total == pytest.approx(stackup["finished_thickness_mm"], abs=0.05)


def test_a_bundled_stackup_alternates_copper_and_dielectric():
    """A stack with two dielectrics in a row, or two coppers, was
    mistranscribed."""
    for ply_list in (s["layers"] for s in load_vendor("jlcpcb")["stackups"]):
        kinds = ["copper" if p["kind"] == "copper" else "dielectric"
                 for p in ply_list]
        assert kinds[0] == kinds[-1] == "copper"
        for first, second in zip(kinds, kinds[1:]):
            assert first != second, kinds


def test_an_unknown_vendor_says_where_to_put_its_data():
    result = load_vendor("eurocircuits")
    assert result["ok"] is False
    assert "fab_profiles/" in result["reason"]
    assert "jlcpcb" in result["reason"]


def test_an_empty_vendor_name_is_refused():
    assert load_vendor("")["ok"] is False


# ---- comparing a template against a live board -------------------------

def _live(copper=(1.378, 0.5984, 0.5984, 1.378),
          dielectrics=(("Prepreg", 8.2835, 4.6), ("Core", 41.9291, 4.6),
                       ("Prepreg", 8.2835, 4.6), ("", 0, None))):
    return {"board_name": "Board.PcbDoc", "layer_count": len(copper),
            "layers": [
                {"name": f"L{i}", "order": i, "copper_thickness_mils": cu,
                 "dielectric_type": d[0], "dielectric_height_mils": d[1],
                 "dielectric_constant": d[2]}
                for i, (cu, d) in enumerate(zip(copper, dielectrics), 1)]}


@pytest.fixture
def template():
    return load_vendor("jlcpcb")["stackups"][0]


def test_a_board_built_to_the_template_matches(template):
    result = compare_stackup(template, _live())
    assert result["matches"] is True
    assert result["error_count"] == 0
    assert result["board_thickness_mm"] == pytest.approx(1.586, abs=0.01)


def test_the_two_shapes_are_reconciled_before_comparing(template):
    """pcb_get_layer_stackup is COPPER-CENTRIC -- one entry per copper
    layer carrying the dielectric beneath it -- and a template is a flat
    ply list. Comparing them without flattening first pairs copper
    against prepreg and reports a correct board as entirely wrong."""
    result = compare_stackup(template, _live())
    assert result["matches"], [f["message"] for f in result["findings"]]


def test_a_thinner_core_is_an_error(template):
    board = _live(dielectrics=(("Prepreg", 8.2835, 4.6), ("Core", 28.0, 4.6),
                               ("Prepreg", 8.2835, 4.6), ("", 0, None)))
    result = compare_stackup(template, board)
    assert result["matches"] is False
    assert any(f["check"] == "thickness" for f in result["findings"])


def test_a_different_dielectric_constant_is_a_warning_not_an_error(template):
    """It does not stop the board being built, and it changes every
    impedance on it, so it is worth saying and not worth blocking."""
    board = _live(dielectrics=(("Prepreg", 8.2835, 4.1), ("Core", 41.9291, 4.6),
                               ("Prepreg", 8.2835, 4.6), ("", 0, None)))
    result = compare_stackup(template, board)
    finding = next(f for f in result["findings"]
                   if f["check"] == "dielectric_constant")
    assert finding["severity"] == "warning"
    assert "impedance" in finding["message"]


def test_a_different_layer_count_is_one_finding_not_many(template):
    """A 6-layer board against a 4-layer template is one fact. Pairing
    the plies off and calling the overhang a difference would bury it
    under six."""
    board = _live(copper=(1.378, 0.6, 0.6, 0.6, 0.6, 1.378),
                  dielectrics=(("Prepreg", 8.0, 4.6), ("Core", 10.0, 4.6),
                               ("Prepreg", 8.0, 4.6), ("Core", 10.0, 4.6),
                               ("Prepreg", 8.0, 4.6), ("", 0, None)))
    result = compare_stackup(template, board)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["check"] == "copper_layer_count"


def test_the_comparison_says_what_it_did_not_check(template):
    note = compare_stackup(template, _live())["note"]
    assert "impedance" in note and "fab will press it" in note


@pytest.mark.parametrize("bad", [None, "x", 7, []])
def test_comparing_junk_is_refused_not_raised(bad, template):
    assert compare_stackup(bad, bad)["ok"] is False
    assert compare_stackup(template, {})["ok"] is False


# ---- laminates ---------------------------------------------------------

def test_both_dielectric_constants_are_carried():
    """Rogers publishes a PROCESS er (one frequency, clamped stripline)
    and a DESIGN er (over a band). RO4350B is 3.48 and 3.66; they differ
    by 5 percent, the design value is the one to size an impedance with,
    and a table carrying a single er silently picks one."""
    laminates = {l["name"]: l for l in list_laminates()["laminates"]}
    ro4350b = laminates["RO4350B"]
    assert ro4350b["er_process"] == 3.48
    assert ro4350b["er_design"] == 3.66
    assert ro4350b["er_process_conditions"] and ro4350b["er_design_conditions"]


def test_a_laminate_with_no_published_design_er_says_null_not_the_other_one():
    laminates = {l["name"]: l for l in list_laminates()["laminates"]}
    jlc = laminates["JLCPCB 7628 prepreg"]
    assert jlc["er_process"] == 4.6
    assert jlc["er_design"] is None


def test_every_laminate_cites_a_source():
    for laminate in list_laminates()["laminates"]:
        assert laminate["source"], laminate["name"]
        assert laminate["retrieved"], laminate["name"]


def test_generic_fr4_is_a_range_and_says_it_is_not_a_specification():
    """FR-4 names a flammability rating, not a formulation. A single
    number for it would read as a datasheet figure."""
    convention = list_laminates()["conventions"][0]
    assert convention["er_low"] < convention["er_high"]
    assert "NOT a material specification" in convention["note"]


def test_the_laminate_list_explains_why_it_is_short():
    assert "datasheet" in list_laminates()["note"]
