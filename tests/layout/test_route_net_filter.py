# SPDX-License-Identifier: Apache-2.0
"""A routing job asked for some nets is strict about them.

Found live on Altium 26.11: asked to route five nets, pcb_autoroute came
back with 438 tracks and 203 vias, 104 of them in pads, and every inner
layer read as a GND plane. The job had taken every pour's copper off the
board before routing, so each GND pad a top pour joined to its stitching
vias read as apart and was given a via of its own; and vias went into
BGA balls and exposed pads by default.

These boards are synthetic: four layers, a GND pour on MidLayer1 used as
a plane, a small GND pour on top joining three old GND pads to a
stitching via, a net joined whole by an old track, and a new part whose
GND and SDA pads nothing reaches yet.
"""
from __future__ import annotations

import pytest

from eda_agent.layout import geom
from eda_agent.layout.model import (Layer, LayoutBoard, Pad, PadCopper, Region, Rule,
                                    Track, Via)
from eda_agent.layout.route import engine
from eda_agent.layout.route.job import route_job

WHOLE = [(0, 0), (800, 0), (800, 500), (0, 500)]


def _rules() -> list[Rule]:
    return [Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                 "Clearance Constraint (Gap=6mil) (All),(All)"),
            Rule("Width", "1", "All", "All", 1, True, {},
                 "Width Constraint (Min=6mil) (Max=6mil) (Preferred=6mil) (All)"),
            Rule("RoutingVias", "2", "All", "All", 1, True, {},
                 "Routing Via (MinHoleWidth=10mil) (MaxHoleWidth=10mil) "
                 "(PreferredHoleWidth=10mil) (MinWidth=20mil) (MaxWidth=20mil) "
                 "(PreferedWidth=20mil) (All)")]


def _pad(comp, name, x, y, net, w=20.0, h=20.0) -> Pad:
    return Pad(comp, name, x, y, net=net, copper=[PadCopper("TopLayer", "rect", w, h)])


def _board() -> LayoutBoard:
    b = LayoutBoard(name="s.PcbDoc", outline=list(WHOLE),
                    layers=[Layer("TopLayer", "signal", 0), Layer("MidLayer1", "signal", 1),
                            Layer("MidLayer2", "signal", 2), Layer("BottomLayer", "signal", 3)],
                    rules=_rules())
    b.pads = [_pad("C1", "2", 60, 60, "GND"), _pad("C2", "2", 120, 60, "GND"),
              _pad("C3", "2", 180, 60, "GND"),
              _pad("R1", "1", 60, 400, "OLD"), _pad("R2", "1", 300, 400, "OLD"),
              _pad("U9", "1", 600, 250, "GND"), _pad("U9", "2", 600, 300, "SDA"),
              _pad("J1", "1", 400, 300, "SDA")]
    top = [(20, 20), (240, 20), (240, 120), (20, 120)]
    b.regions = [Region("TopLayer", top, [], "GND", "pour_boundary", source="polygon:TopGnd"),
                 Region("TopLayer", top, [], "GND", "pour", source="polygon:TopGnd"),
                 Region("MidLayer1", list(WHOLE), [], "GND", "pour_boundary",
                        source="polygon:L2"),
                 Region("MidLayer1", list(WHOLE), [], "GND", "pour", source="polygon:L2")]
    b.vias = [Via(220, 100, 20, 10, "TopLayer", "BottomLayer", "GND")]
    b.tracks = [Track("TopLayer", 60, 400, 300, 400, 6.0, "OLD")]
    return b


@pytest.fixture(params=engine.ENGINES)
def which(request, monkeypatch):
    monkeypatch.setenv(engine.ENV, request.param)
    return request.param


def test_a_net_the_pours_already_join_gets_no_new_via(which):
    res = route_job({"board": _board(), "nets": ["SDA", "GND"]})
    s = res["summary"]
    assert s["completion"] == 1.0 and s["violations"] == 0
    # One via: the new part's GND pad into the plane. C1-C3 reach it
    # through the top pour and its stitching via already.
    assert [(v["net"]) for v in res["vias"]] == ["GND"]
    assert res["vias"][0]["x"] > 500, res["vias"]
    assert {t["net_name"] for t in res["tracks"]} <= {"SDA", "GND"}


def test_a_net_not_asked_for_is_never_touched(which):
    res = route_job({"board": _board(), "nets": ["SDA"]})
    assert {t["net_name"] for t in res["tracks"]} == {"SDA"}
    assert all(v["net"] == "SDA" for v in res["vias"])


def test_copper_of_a_net_not_asked_for_is_never_sent(monkeypatch):
    """Whatever the router comes back with, a job asked for some nets
    sends copper of those nets only."""
    import eda_agent.layout.route.job as job_mod
    monkeypatch.setenv(engine.ENV, "default")
    real = job_mod.route_adaptive

    def leaky(*a, **k):
        r = real(*a, **k)
        apply = r.apply

        def apply_and_stray(*aa, **kk):
            out = apply(*aa, **kk)
            out.tracks.append(Track("BottomLayer", 60, 450, 300, 450, 6.0, "OLD"))
            out.vias.append(Via(700, 450, 20, 10, "TopLayer", "BottomLayer", "OLD"))
            return out
        r.apply = apply_and_stray
        return r

    monkeypatch.setattr(job_mod, "route_adaptive", leaky)
    res = route_job({"board": _board(), "nets": ["SDA"]})
    assert {t["net_name"] for t in res["tracks"]} == {"SDA"}
    assert not [v for v in res["vias"] if v["net"] != "SDA"]
    assert any("OLD" in n and "dropped" in n for n in res["notes"])


def _exposed_pad_board() -> LayoutBoard:
    """A QFN-like part whose exposed pad is GND, over a GND plane."""
    b = LayoutBoard(name="q.PcbDoc", outline=list(WHOLE),
                    layers=[Layer("TopLayer", "signal", 0), Layer("MidLayer1", "signal", 1),
                            Layer("BottomLayer", "signal", 2)],
                    rules=_rules())
    b.pads = [_pad("U5", "1", 320, 200, "N1"), _pad("U5", "2", 320, 300, "N2"),
              _pad("U5", "3", 480, 200, "N3"), _pad("U5", "4", 480, 300, "N4"),
              _pad("U5", "5", 400, 250, "GND", 100.0, 100.0)]
    b.regions = [Region("MidLayer1", list(WHOLE), [], "GND", "pour_boundary",
                        source="polygon:L2")]
    return b


def _in_pad(v: dict, pad: Pad) -> bool:
    via = geom.circle(v["x"], v["y"], v["size"] / 2)
    return geom.clearance(via, pad.shape_on("TopLayer")) <= 0


def test_no_via_goes_in_a_pad_unless_asked(which):
    ep = _exposed_pad_board().pads[4]
    # The engine's own default ("bga") does put the plane via in the pad:
    # the case this test is about is real.
    res = route_job({"board": _exposed_pad_board(), "nets": ["GND"]})
    assert any(_in_pad(v, ep) for v in res["vias"])
    res = route_job({"board": _exposed_pad_board(), "nets": ["GND"],
                     "stages": {"pad_vias": "none"}})
    assert res["summary"]["completion"] == 1.0 and res["summary"]["violations"] == 0
    assert res["vias"] and not [v for v in res["vias"] if _in_pad(v, ep)]
    assert not res["via_in_pad"]


def test_an_inner_layer_is_the_plane_of_the_net_its_copper_is(which):
    """MidLayer2 is a 3V3 plane: its 3V3 pour poured the whole layer, over
    a board-wide GND pour drawn under it. Judged by the two outlines, the
    layer was GND's and 3V3 only a split of it."""
    b = _board()
    b.pads += [_pad("U9", "3", 650, 400, "3V3"), _pad("C9", "1", 700, 400, "3V3")]
    b.regions += [Region("MidLayer2", list(WHOLE), [], "GND", "pour_boundary",
                         source="polygon:L3gnd"),
                  Region("MidLayer2", list(WHOLE), [], "3V3", "pour_boundary",
                         source="polygon:L3"),
                  Region("MidLayer2", list(WHOLE), [], "3V3", "pour", source="polygon:L3")]
    res = route_job({"board": b, "nets": ["SDA"]})
    planes = res["summary"]["planes"]
    if which == "default":
        assert "MidLayer2:3V3" in planes and "MidLayer2:GND" not in planes, planes
    assert "MidLayer1:GND" in planes
