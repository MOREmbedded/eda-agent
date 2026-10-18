# SPDX-License-Identifier: Apache-2.0
"""Replacement finding: the judgement, and what it refuses to conclude.

Two layers, tested apart because they fail apart: rank_replacements
decides, the tool fetches. Everything here runs offline -- no
credential, no network -- which is the point of keeping the deciding
pure.
"""
from __future__ import annotations

import asyncio

import pytest

from eda_agent.engineering.replacements import (
    NOT_CHECKED,
    lifecycle_state,
    rank_replacements,
)
from eda_agent.tools import parts as parts_tools
from eda_agent.tools.registry import ToolRegistry


# ---- lifecycle vocabulary ----------------------------------------------

@pytest.mark.parametrize("text,state", [
    ("Active", "active"),
    ("Production", "active"),
    ("New Product", "active"),
    ("NRND", "nrnd"),
    ("Not Recommended for New Designs", "nrnd"),
    ("Obsolete", "obsolete"),
    ("End of Life", "obsolete"),
    ("Last Time Buy", "obsolete"),
    ("Discontinued at Digi-Key", "obsolete"),
    ("Preliminary", "preproduction"),
])
def test_each_catalogues_wording_folds_to_one_state(text, state):
    assert lifecycle_state(text) == state


@pytest.mark.parametrize("text", ["", "   ", None, 7, "Who knows"])
def test_an_unstated_lifecycle_is_unknown_and_not_active(text):
    """A source that says nothing has not told you the part is fine."""
    assert lifecycle_state(text) == "unknown"


def test_a_phrase_is_not_claimed_by_a_word_inside_it():
    """"Not Recommended for New Designs" must not read as active for
    containing neither word, and the ordering that makes phrases beat
    bare words is what keeps it that way."""
    assert lifecycle_state("Not recommended for new designs") == "nrnd"
    assert lifecycle_state("Active-Last Time Buy") == "obsolete"


# ---- the judgement ------------------------------------------------------

_ORIGINAL = {
    "mpn": "GRM188R71C104KA01D", "manufacturer": "Murata",
    "lifecycle": "Active", "total_available": 50000,
    "parameters": {
        "capacitance": "100 nF", "voltagerating_dc": "16 V",
        "tolerance": "10%", "dielectric": "X7R",
        "operatingtemperature": "-55°C ~ 125°C",
        "case_package": "0603",
    },
}


def _candidate(mpn, **over):
    base = {
        "mpn": mpn, "manufacturer": "Yageo", "lifecycle": "Active",
        "total_available": 120000, "provider": "nexar",
        "parameters": {
            "Capacitance": "0.1 uF", "Voltage - Rated": "50V",
            "Tolerance": "±10%", "Dielectric": "X7R",
            "Operating Temperature": "-55 ~ 125", "Package / Case": "0603",
        },
    }
    params = over.pop("parameters", None)
    base.update(over)
    if params:
        base["parameters"] = {**base["parameters"], **params}
    return base


def test_a_clean_candidate_has_nothing_blocking():
    r = rank_replacements(_ORIGINAL, [_candidate("CC0603KRX7R9BB104")])
    one = r["candidates"][0]
    assert one["status"] == "candidate"
    assert one["blocking"] == []
    assert one["stock"] == 120000


def test_a_worse_dielectric_blocks():
    r = rank_replacements(_ORIGINAL,
                          [_candidate("X", parameters={"Dielectric": "X5R"})])
    assert r["candidates"][0]["status"] == "blocked"
    # Named by the ORIGINAL's spelling: the report is about the part
    # being replaced, and the candidate's own label for the attribute
    # is already beside it in the per-attribute record.
    assert "dielectric" in r["candidates"][0]["blocking"]


def test_an_obsolete_candidate_blocks_however_well_it_matches():
    """Every parameter identical, and it is still not a replacement."""
    r = rank_replacements(_ORIGINAL,
                          [_candidate("X", lifecycle="Obsolete")])
    one = r["candidates"][0]
    assert one["status"] == "blocked"
    assert "lifecycle" in one["blocking"]
    assert any("next year" in c for c in one["concerns"])


def test_an_unstated_lifecycle_is_unreviewed_rather_than_blocking():
    r = rank_replacements(_ORIGINAL, [_candidate("X", lifecycle="")])
    one = r["candidates"][0]
    assert "lifecycle" in one["unreviewed"]
    assert "lifecycle" not in one["blocking"]


def test_a_downgrade_in_lifecycle_is_a_concern_not_a_block():
    """NRND is buyable today. It is worth saying out loud and it is not
    this tool's place to refuse it."""
    r = rank_replacements(_ORIGINAL, [_candidate("X", lifecycle="NRND")])
    one = r["candidates"][0]
    assert one["status"] == "candidate"
    assert any("nrnd" in c for c in one["concerns"])


def test_zero_stock_and_unstated_stock_read_differently():
    """Only one of them means you cannot buy it."""
    none_said = rank_replacements(
        _ORIGINAL, [_candidate("X", total_available=None)])["candidates"][0]
    sold_out = rank_replacements(
        _ORIGINAL, [_candidate("X", total_available=0)])["candidates"][0]
    assert none_said["stock"] is None
    assert any("no source stated" in c for c in none_said["concerns"])
    assert sold_out["stock"] == 0
    assert any("no stock at the sources" in c for c in sold_out["concerns"])


def test_the_package_is_reported_on_its_own_and_says_what_it_checked():
    """It is the one attribute that decides whether the board has to be
    re-laid out, and it is also the one most likely to be mistaken for
    a geometric check."""
    package = rank_replacements(
        _ORIGINAL, [_candidate("X")])["candidates"][0]["package"]
    assert package["verdict"] == "same"
    assert "NOT pad geometry" in package["checked"]


def test_a_part_is_not_its_own_replacement():
    r = rank_replacements(_ORIGINAL, [
        _candidate("GRM188R71C104KA01D"), _candidate("CC0603KRX7R9BB104")])
    assert [c["mpn"] for c in r["candidates"]] == ["CC0603KRX7R9BB104"]


def test_candidates_are_ordered_by_fewest_blocking_findings():
    r = rank_replacements(_ORIGINAL, [
        _candidate("ZZZ-two-blocks", parameters={
            "Dielectric": "X5R", "Operating Temperature": "-40 ~ 85"}),
        _candidate("AAA-one-block", parameters={"Dielectric": "X5R"}),
        _candidate("MMM-clean"),
    ])
    assert [c["mpn"] for c in r["candidates"]] == [
        "MMM-clean", "AAA-one-block", "ZZZ-two-blocks"]


def test_every_result_states_what_it_did_not_check():
    """The user asked for errata and manufacturer announcements. No
    distributor API serves them, so the honest answer is to say so in
    the result rather than let silence imply a clean bill."""
    r = rank_replacements(_ORIGINAL, [_candidate("X")])
    assert r["not_checked"] == list(NOT_CHECKED)
    joined = " ".join(r["not_checked"]).lower()
    assert "product-change" in joined and "errata" in joined
    assert "lib_audit_footprint_vs_datasheet" in joined


def test_nothing_in_the_output_claims_an_equivalence():
    r = rank_replacements(_ORIGINAL, [_candidate("X")])
    text = " ".join([r["note"]] + [c["summary"] for c in r["candidates"]])
    for word in ("equivalent", "drop-in", "interchangeable", "approved"):
        assert word not in text.lower(), word


@pytest.mark.parametrize("junk", [None, "", 7, [], {}])
def test_junk_input_is_an_empty_result_rather_than_a_raise(junk):
    r = rank_replacements(junk, junk)
    assert r["candidates"] == []
    assert r["not_checked"]


# ---- the tool -----------------------------------------------------------

def _tool(monkeypatch, catalogue):
    """Register the tool with search_all answering from a dict."""
    import eda_agent.libimport.providers as providers

    def fake_search_all(query, limit_per_provider=20):
        hits = catalogue.get(query, [])
        return {"count": len(hits), "hits": hits,
                "providers": {"nexar": {"ok": True, "count": len(hits)}},
                "by_mpn": []}

    monkeypatch.setattr(providers, "search_all", fake_search_all)
    registry = ToolRegistry()
    parts_tools.register_parts_tools(registry)
    return registry.get("part_find_replacements").fn


def _hit(mpn, **extra):
    return {"provider": "nexar", "mpn": mpn, "manufacturer": "ACME",
            "datasheet": f"https://example.invalid/{mpn}.pdf",
            "kind": "catalogue", "extra": extra}


_PARAMS = {"capacitance": "100 nF", "dielectric": "X7R",
           "voltagerating_dc": "16 V"}


def test_the_tool_judges_the_candidates_it_is_given(monkeypatch):
    catalogue = {
        "ORIG-1": [_hit("ORIG-1", parameters=_PARAMS, lifecycle="Active")],
        "CAND-1": [_hit("CAND-1", parameters={**_PARAMS,
                                              "voltagerating_dc": "50 V"},
                        lifecycle="Active", total_available=999)],
    }
    result = asyncio.run(_tool(monkeypatch, catalogue)("ORIG-1", "CAND-1"))
    assert result["ok"]
    assert [c["mpn"] for c in result["candidates"]] == ["CAND-1"]
    assert result["candidates"][0]["blocking"] == []


def test_the_tool_falls_back_to_the_catalogues_similar_parts(monkeypatch):
    catalogue = {
        "ORIG-1": [_hit("ORIG-1", parameters=_PARAMS, lifecycle="Active",
                        similar_parts=[{"mpn": "SIM-1",
                                        "manufacturer": "ACME"}])],
        "SIM-1": [_hit("SIM-1", parameters=_PARAMS, lifecycle="Active")],
    }
    result = asyncio.run(_tool(monkeypatch, catalogue)("ORIG-1"))
    assert [c["mpn"] for c in result["candidates"]] == ["SIM-1"]
    # And says where the list came from, because a catalogue's idea of
    # similar is not an engineering judgement.
    assert "similar" in result["note"].lower()


def test_the_similar_parts_fallback_needs_the_structured_shape(monkeypatch):
    """The old flattened "<manufacturer> <mpn>" strings cannot be split
    back apart, which is why the provider now keeps the two halves."""
    catalogue = {"ORIG-1": [_hit("ORIG-1", parameters=_PARAMS,
                                 similar_parts=[{"mpn": "SIM-1",
                                                 "manufacturer": "ACME"}])],
                 "SIM-1": [_hit("SIM-1", parameters=_PARAMS)]}
    result = asyncio.run(_tool(monkeypatch, catalogue)("ORIG-1"))
    assert result["candidates_requested"] == ["SIM-1"]


def test_max_candidates_caps_the_searches(monkeypatch):
    catalogue = {"ORIG-1": [_hit("ORIG-1", parameters=_PARAMS)]}
    for i in range(10):
        catalogue[f"C-{i}"] = [_hit(f"C-{i}", parameters=_PARAMS)]
    names = ",".join(f"C-{i}" for i in range(10))
    result = asyncio.run(_tool(monkeypatch, catalogue)(
        "ORIG-1", names, max_candidates=3))
    assert len(result["candidates"]) == 3


def test_a_candidate_no_catalogue_carries_is_named_not_dropped(monkeypatch):
    """A lead that went nowhere is different from a part that failed,
    and a silently shorter list reads as the second."""
    catalogue = {"ORIG-1": [_hit("ORIG-1", parameters=_PARAMS)],
                 "REAL-1": [_hit("REAL-1", parameters=_PARAMS)]}
    result = asyncio.run(_tool(monkeypatch, catalogue)(
        "ORIG-1", "REAL-1,GHOST-1"))
    assert result["not_found"] == ["GHOST-1"]
    assert [c["mpn"] for c in result["candidates"]] == ["REAL-1"]


def test_the_sources_are_merged_rather_than_one_being_preferred(monkeypatch):
    """Nexar may carry the parametric map while Digi-Key carries the
    stock. Taking only the first hit would drop whichever came second."""
    catalogue = {
        "ORIG-1": [_hit("ORIG-1", parameters=_PARAMS)],
        "CAND-1": [
            {**_hit("CAND-1"), "provider": "digikey",
             "extra": {"total_available": 4242, "lifecycle": "Active"}},
            {**_hit("CAND-1"), "provider": "nexar",
             "extra": {"parameters": _PARAMS}},
        ],
    }
    result = asyncio.run(_tool(monkeypatch, catalogue)("ORIG-1", "CAND-1"))
    one = result["candidates"][0]
    assert one["stock"] == 4242
    assert one["lifecycle_state"] == "active"
    assert one["parameters"]["compared"] == len(_PARAMS)


def test_stock_is_merged_by_maximum_not_by_whichever_answered_first(
        monkeypatch):
    """A zero from one distributor must not hide five thousand at
    another.

    The figures mean different things per source -- Nexar's is an
    aggregate across distributors, Digi-Key's is Digi-Key's own shelf --
    so summing would double-count, and first-wins would turn the answer
    into an accident of sort order. The largest credible figure answers
    the question actually being asked, which is whether it can be
    bought.
    """
    catalogue = {
        "ORIG-1": [_hit("ORIG-1", parameters=_PARAMS)],
        "CAND-1": [
            {**_hit("CAND-1"), "provider": "digikey",
             "extra": {"total_available": 0, "parameters": _PARAMS}},
            {**_hit("CAND-1"), "provider": "mouser",
             "extra": {"total_available": 5000}},
        ],
    }
    result = asyncio.run(_tool(monkeypatch, catalogue)("ORIG-1", "CAND-1"))
    assert result["candidates"][0]["stock"] == 5000


def test_a_stock_of_zero_survives_and_does_not_read_as_unstated(monkeypatch):
    """The two are different answers and only one of them means you
    cannot buy the part."""
    catalogue = {
        "ORIG-1": [_hit("ORIG-1", parameters=_PARAMS)],
        "ZERO-1": [_hit("ZERO-1", parameters=_PARAMS, total_available=0)],
        "QUIET-1": [_hit("QUIET-1", parameters=_PARAMS)],
    }
    tool = _tool(monkeypatch, catalogue)
    zero = asyncio.run(tool("ORIG-1", "ZERO-1"))["candidates"][0]
    quiet = asyncio.run(tool("ORIG-1", "QUIET-1"))["candidates"][0]
    assert zero["stock"] == 0
    assert quiet["stock"] is None
    assert any("no stock at the sources" in c for c in zero["concerns"])
    assert any("no source stated" in c for c in quiet["concerns"])


def test_an_original_with_no_parameters_refuses_rather_than_compare_nothing(
        monkeypatch):
    """Comparing against an empty map would report every candidate as
    perfect, which is the most dangerous possible output."""
    catalogue = {"ORIG-1": [_hit("ORIG-1", lifecycle="Active")],
                 "CAND-1": [_hit("CAND-1", parameters=_PARAMS)]}
    result = asyncio.run(_tool(monkeypatch, catalogue)("ORIG-1", "CAND-1"))
    assert result["ok"] is False
    assert "no source published any parameters" in result["reason"]


def test_an_unfindable_original_says_that_is_not_proof_of_absence(monkeypatch):
    result = asyncio.run(_tool(monkeypatch, {})("NOSUCHPART"))
    assert result["ok"] is False
    assert "not evidence the part does not exist" in result["reason"]


def test_no_candidates_at_all_says_how_to_supply_them(monkeypatch):
    catalogue = {"ORIG-1": [_hit("ORIG-1", parameters=_PARAMS)]}
    result = asyncio.run(_tool(monkeypatch, catalogue)("ORIG-1"))
    assert result["ok"] is False
    assert "candidates=" in result["reason"]


def test_an_empty_mpn_is_refused(monkeypatch):
    assert asyncio.run(_tool(monkeypatch, {})("   "))["ok"] is False
