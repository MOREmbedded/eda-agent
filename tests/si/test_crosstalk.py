# SPDX-License-Identifier: Apache-2.0
"""Board-wide crosstalk: the neighbours found on the board, the coupling
from the field solver, the sums, the net job's check and the board job's
placed finding."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from eda_agent import simulation as sim
from eda_agent.emc.stack import stack_from_stackup
from eda_agent.layout.model import DiffPair, Track
from eda_agent.si import board_jobs, crosstalk, extract, mtl, net_jobs, net_model, planner, rlgc
from eda_agent.si import channel as ch
from eda_agent.si.jobs import plan_options
from eda_agent.si.section import MIL

from .boards import (DUAL_LAYERS, DUAL_STACKUP, LAYERS, STACKUP, pair_board, single_net,
                     smd)

RISE = 100e-12


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SIM_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(tmp_path / "features.json"))
    monkeypatch.setattr(sim, "_loaded", None)


@pytest.fixture(scope="module")
def coupling():
    opt = plan_options({})
    stack = stack_from_stackup(STACKUP, LAYERS)
    info = {i["layer"]: i for i in planner.layer_sections(stack, opt)}
    return crosstalk.Coupling(info, opt, 0.35 / RISE)


def victim():
    """SIG: 1000 mil on top along y = 0 (5 mil wide), 2000 mil on the inner
    layer along x = 1000 (4 mil wide), 500 mil on top."""
    return single_net(with_stub=False)


def beside(b, net="AGG", gap=4.0, x1=100.0, x2=900.0, side=1, width=5.0):
    """A top-layer track ``gap`` from the victim's first run, edge to edge."""
    y = side * (gap + 0.5 * (5.0 + width))
    b.tracks.append(Track("TopLayer", x1, y, x2, y, width, net))
    return b


def of(b, coupling, rise=RISE, **kw):
    return crosstalk.of_path(b, extract.net_path(b, "SIG"), coupling, rise, **kw)


def test_the_formulas_agree_with_the_coupled_line_solved_in_full(coupling):
    """Two traces 4 mil apart for 800 mil on the top layer: the near-end and
    far-end peaks of the full four-port (eddy-current R and L, exact modes)
    against the closed forms on Kc and Kl."""
    f = ch.uniform_band(20e9, 300)
    sec = planner.build(coupling.info["TopLayer"], coupling.opt, 5 * MIL, 2, 4 * MIL)
    s = mtl.s_params(rlgc.compute(sec, f), 800 * MIL, 50.0)
    _, near = net_model.step_response(f, s[:, 1, 0], RISE)
    _, far = net_model.step_response(f, s[:, 3, 0], RISE)
    res = of(beside(victim()), coupling)
    a = res["aggressors"][0]
    assert a["next_pct"] == pytest.approx(100 * np.abs(near).max(), rel=0.05)
    assert a["fext_pct"] == pytest.approx(100 * np.abs(far).max(), rel=0.15)
    assert far[np.abs(far).argmax()] < 0          # a microstrip's far end is negative
    assert 8 < a["next_pct"] < 12 and 3 < a["fext_pct"] < 7


def test_only_copper_running_beside_the_net_within_reach_counts(coupling):
    b = beside(victim())                                          # the one that counts
    b.tracks += [
        Track("TopLayer", 500, -200, 500, 200, 5, "CROSSES"),     # at right angles
        Track("TopLayer", 100, 60, 900, 60, 5, "FAR"),            # 55 mil away
        Track("BottomLayer", 100, 9, 900, 9, 5, "BELOW"),         # another layer
        Track("TopLayer", 100, -9, 105, -9, 5, "SHORT"),          # 5 mil of it
        Track("TopLayer", 100, -12, 400, -12 - 300 * math.tan(math.radians(25)), 5, "LEAVES"),
        Track("TopLayer", 100, -9, 900, -9, 5, "SIG"),            # its own copper
    ]
    res = of(b, coupling)
    assert [a["aggressor"] for a in res["aggressors"]] == ["AGG"]
    a = res["aggressors"][0]
    assert a["gap_mils"] == pytest.approx(4.0) and a["length_mils"] == pytest.approx(800)
    assert a["layer"] == "TopLayer" and a["at_mils"] == [100.0, 0.0]
    # reach can be widened: the far track then counts, and adds little
    wide = of(b, coupling, reach_mils=80)
    names = {a["aggressor"]: a for a in wide["aggressors"]}
    assert set(names) == {"AGG", "FAR"}
    assert names["FAR"]["next_pct"] < 0.1 * names["AGG"]["next_pct"]


def test_curves_count_arcs_as_aggressors_and_a_run_cut_into_short_chords(coupling):
    from eda_agent.layout.model import Arc
    # the victim turns a quarter circle of 500 mil radius; the aggressor follows
    # it on the outside, 4 mil away edge to edge
    b = victim()
    b.pads = [smd("U1", "1", 500, 0, "SIG"), smd("U2", "1", 0, 500, "SIG")]
    b.vias = []
    b.tracks = []
    b.arcs = [Arc("TopLayer", 0, 0, 500, 0, 90, 5, "SIG"),
              Arc("TopLayer", 0, 0, 509, 0, 90, 5, "AGG")]
    curved = of(b, coupling)["aggressors"][0]
    quarter = math.pi / 2 * 500
    assert curved["aggressor"] == "AGG" and curved["layer"] == "TopLayer"
    assert curved["length_mils"] == pytest.approx(quarter, rel=0.01)
    assert curved["gap_mils"] == pytest.approx(4.0, abs=0.1)
    assert of(b, coupling, quiet={"AGG"})["aggressors"] == []     # a quiet net's arcs too
    # the place given is where the curved stretch starts on the route (U1.1 at
    # 0 degrees), not where one of its chords does
    assert curved["at_mils"] == pytest.approx([500, 0], abs=0.5)
    # walked the other way (from U2.1), against the way the neighbour's arc was drawn
    back = crosstalk.of_path(b, extract.net_path(b, "SIG", "U2.1", "U1.1"), coupling, RISE)
    assert back["aggressors"][0]["at_mils"] == pytest.approx([0, 500], abs=0.5)
    assert back["aggressors"][0]["length_mils"] == pytest.approx(curved["length_mils"], rel=1e-6)
    # an aggressor beside the last third only: the stretch starts at 60 degrees
    b.arcs[1] = Arc("TopLayer", 0, 0, 509, 60, 90, 5, "AGG")
    third = of(b, coupling)["aggressors"][0]
    assert third["at_mils"] == pytest.approx([250, 500 * math.sin(math.radians(60))], abs=6)
    assert third["length_mils"] == pytest.approx(quarter / 3, rel=0.02)
    b.arcs[1] = Arc("TopLayer", 0, 0, 509, 0, 90, 5, "AGG")
    # as much as the same length side by side on a straight run
    straight = of(beside(victim(), x1=100, x2=100 + quarter), coupling)["aggressors"][0]
    assert curved["next_pct"] == pytest.approx(straight["next_pct"], rel=0.02)
    assert curved["fext_pct"] == pytest.approx(straight["fext_pct"], rel=0.03)
    # a straight neighbour drawn as 160 tracks of 5 mil is one run of 800 mil
    cut = victim()
    for i in range(160):
        beside(cut, x1=100 + 5 * i, x2=105 + 5 * i)
    whole = of(beside(victim()), coupling)["aggressors"][0]
    pieces = of(cut, coupling)["aggressors"][0]
    assert pieces["length_mils"] == pytest.approx(800)
    assert pieces["next_pct"] == pytest.approx(whole["next_pct"], rel=1e-6)
    assert pieces["at_mils"] == [100.0, 0.0]
    # two separate stretches beside one net: the longer one is the place
    two = of(beside(beside(victim(), x1=50, x2=150), x1=400, x2=900), coupling)["aggressors"][0]
    assert two["at_mils"] == [400.0, 0.0] and two["length_mils"] == pytest.approx(600)
    two = of(beside(beside(victim(), x1=50, x2=550), x1=800, x2=900), coupling)["aggressors"][0]
    assert two["at_mils"] == [50.0, 0.0]
    # an arc of another net that only crosses is still nothing
    b = victim()
    b.arcs = [Arc("TopLayer", 500, 300, 300, 180, 360, 5, "LOOP")]
    assert of(b, coupling)["aggressors"] == []


def test_a_track_at_a_slight_angle_still_runs_beside_and_only_where_it_overlaps(coupling):
    b = victim()
    # 5 degrees off, starting before the victim does: only x >= 0 overlaps
    dy = 400 * math.tan(math.radians(5))
    b.tracks.append(Track("TopLayer", -200, 12, 200, 12 + dy, 5, "AGG"))
    a = of(b, coupling, reach_mils=60)["aggressors"][0]
    assert a["length_mils"] == pytest.approx(200, abs=1)
    # gap read at the middle of the overlap (x = 100): centre 12 + 0.75 dy
    assert a["gap_mils"] == pytest.approx(12 + 0.75 * dy - 5, abs=0.3)


def test_coupling_falls_with_the_gap_and_a_stripline_has_no_far_end(coupling):
    last = None
    for gap in (3, 5, 8, 14):
        kc, kl, tpd = coupling.get("TopLayer", 5, gap)
        assert 0 < kc < kl < 0.5                 # a microstrip couples more by L than by C
        assert 5.0e-9 < tpd < 6.5e-9             # FR-4 microstrip, seconds per metre
        if last is not None:
            assert kc < last[0] and kl < last[1]
        last = (kc, kl)
    # between two solved gaps the reading lies between them
    k = math.floor(math.log(6.0) / math.log(crosstalk.GAP_STEP))
    lo, hi = crosstalk.GAP_STEP ** k, crosstalk.GAP_STEP ** (k + 1)
    assert coupling.get("TopLayer", 5, lo)[0] > coupling.get("TopLayer", 5, 6.0)[0] \
        > coupling.get("TopLayer", 5, hi)[0]
    kc, kl, _ = coupling.get("MidLayer1", 4, 4)
    assert kc == pytest.approx(kl, rel=0.03)     # one dielectric: Kc = Kl
    b = victim()
    b.tracks.append(Track("MidLayer1", 1008, 200, 1008, 1800, 4, "CLK"))
    a = of(b, coupling)["aggressors"][0]
    assert a["layer"] == "MidLayer1" and a["next_pct"] > 5 and a["fext_pct"] < 0.3


def test_near_end_grows_with_length_to_its_ceiling_however_the_run_is_cut(coupling):
    kc, kl, tpd = coupling.get("TopLayer", 5, 4)
    kb = 25 * (kc + kl)
    # 100 mil is shorter than half the 100 ps edge: 2 TD / tr of the ceiling
    short = of(beside(victim(), x1=100, x2=200), coupling)["aggressors"][0]
    assert short["next_pct"] == pytest.approx(kb * 2 * 100 * MIL * tpd / RISE, rel=1e-3)
    assert short["next_pct"] < 0.4 * kb
    whole = of(beside(victim(), x1=100, x2=900), coupling)["aggressors"][0]
    assert whole["next_pct"] == pytest.approx(kb, rel=1e-3)
    cut = victim()
    for i in range(8):
        beside(cut, x1=100 + 100 * i, x2=200 + 100 * i)
    pieces = of(cut, coupling)["aggressors"][0]
    assert pieces["next_pct"] == pytest.approx(whole["next_pct"], rel=1e-6)
    assert pieces["fext_pct"] == pytest.approx(whole["fext_pct"], rel=1e-6)
    assert pieces["length_mils"] == pytest.approx(800)
    # a further stretch at a wider gap does not lower the ceiling of the tightest
    two = beside(beside(victim(), x1=100, x2=800), gap=12.0, x1=800, x2=950)
    assert of(two, coupling)["aggressors"][0]["next_pct"] == pytest.approx(kb, rel=1e-3)
    # the far end keeps growing with length and with a faster edge
    fast = of(beside(victim()), coupling, rise=50e-12)["aggressors"][0]
    assert fast["fext_pct"] == pytest.approx(2 * whole["fext_pct"], rel=0.02)
    assert fast["next_pct"] == pytest.approx(whole["next_pct"], rel=0.02)


def test_aggressors_add_and_a_pair_beside_the_net_mostly_cancels(coupling):
    one = of(beside(victim()), coupling)
    two = of(beside(beside(victim()), "OTHER", side=-1), coupling)
    assert len(two["aggressors"]) == 2
    assert two["next_pct"] == pytest.approx(2 * one["next_pct"], rel=1e-3)
    # the same two tracks as the two sides of a pair: equal and opposite
    b = beside(beside(victim(), "A_P"), "A_N", side=-1)
    b.diff_pairs = [DiffPair("A", "A_P", "A_N")]
    res = of(b, coupling)
    assert [a["aggressor"] for a in res["aggressors"]] == ["pair A"]
    assert res["aggressors"][0]["nets"] == ["A_P", "A_N"]
    assert res["next_pct"] < 1e-6 and res["fext_pct"] < 1e-6
    # a pair routed beside it, its far side one pitch further: what is left
    b = beside(beside(victim(), "A_P"), "A_N", gap=13.0)
    b.diff_pairs = [DiffPair("A", "A_P", "A_N")]
    res = of(b, coupling)
    assert 0.3 * one["next_pct"] < res["next_pct"] < 0.9 * one["next_pct"]


def test_a_pair_hears_the_difference_between_its_sides(coupling):
    b = pair_board()            # D_P along y = 0, D_N along y = 9, 4 mil wide, inner layer
    # beside D_N only: 4 mil beyond it, so 13 mil from D_P's far edge
    b.tracks.append(Track("MidLayer1", 200, 17, 1800, 17, 4, "CLK"))
    path = extract.pair_path(b, "D", "D_P", "D_N")
    res = crosstalk.of_path(b, path, coupling, RISE)
    a = res["aggressors"][0]
    assert a["aggressor"] == "CLK" and a["gap_mils"] == pytest.approx(4.0)
    near_n = 25 * sum(coupling.get("MidLayer1", 4, 4)[:2])
    near_p = 25 * sum(coupling.get("MidLayer1", 4, 13)[:2])
    assert a["next_pct"] == pytest.approx(near_n - near_p, rel=0.01)
    assert 0 < a["next_pct"] < near_n
    # the pair's own two nets are not each other's aggressors
    assert all(x["aggressor"] != "pair D" for x in res["aggressors"])


def test_grounds_planes_and_rails_are_quiet_and_more_can_be_named():
    b = victim()
    for i, net in enumerate(["GND", "AGND_ADC", "VCC", "+3V3", "1V8", "VDD_IO", "VBAT", "-12V",
                             "VREF", "RESET_N", "VSYNC", "V5_EN", "VIDEO"]):
        b.tracks.append(Track("BottomLayer", 0, 10 * i, 100, 10 * i, 5, net))
    quiet = crosstalk.quiet_nets(b)
    assert quiet == {"GND", "AGND_ADC", "VCC", "+3V3", "1V8", "VDD_IO", "VBAT", "-12V", "VREF"}
    assert crosstalk.quiet_nets(b, ["reset*"]) == quiet | {"RESET_N"}


def test_the_net_job_checks_crosstalk_against_its_limit(tmp_path):
    b = beside(beside(victim()), "GND", side=-1)
    b.tracks.append(Track("TopLayer", 100, 60, 900, 60, 5, "FAR"))     # 55 mil away
    spec = {"net": "SIG", "points": 80, "rules": False, "rise_ps": 100}
    res = net_jobs.run_net_channel(spec, b.to_dict(), STACKUP, tmp_path / "a")
    m = {k: v["value"] for k, v in res["metrics"].items()}
    assert m["aggressors"] == 1 and 8 < m["crosstalk_next_pct"] < 12
    assert m["crosstalk_pct"] == max(m["crosstalk_next_pct"], m["crosstalk_fext_pct"])
    v = next(x for x in res["verdicts"] if x["check"] == "crosstalk")
    assert not v["pass"] and "AGG" in v["detail"] and "5 % allowed" in v["detail"]
    saved = json.loads((tmp_path / "a" / "crosstalk.json").read_text())
    assert saved["aggressors"][0]["aggressor"] == "AGG" and saved["quiet"] == ["GND"]
    assert res["figures"]["crosstalk"] == saved["aggressors"]
    assert any("not switching: GND" in n for n in res["notes"])
    # a looser limit passes; a named quiet net is no aggressor; and it can be left out
    res = net_jobs.run_net_channel({**spec, "xtalk_max_pct": 15, "xtalk_reach_mils": 80},
                                   b.to_dict(), STACKUP, tmp_path / "b")
    assert next(x for x in res["verdicts"] if x["check"] == "crosstalk")["pass"]
    assert res["metrics"]["aggressors"]["value"] == 2      # the wider reach takes FAR in
    res = net_jobs.run_net_channel({**spec, "xtalk_quiet": ["AGG"]}, b.to_dict(), STACKUP,
                                   tmp_path / "c")
    v = next(x for x in res["verdicts"] if x["check"] == "crosstalk")
    assert v["pass"] and v["detail"] == "no switching net runs beside it"
    assert res["metrics"]["crosstalk_pct"]["value"] == 0
    res = net_jobs.run_net_channel({**spec, "xtalk": False}, b.to_dict(), STACKUP, tmp_path / "d")
    assert not any(x["check"] == "crosstalk" for x in res["verdicts"])
    assert "crosstalk_pct" not in res["metrics"] and "crosstalk" not in res["files"]
    with pytest.raises(ValueError, match="xtalk_reach_mils"):
        net_jobs.run_net_channel({**spec, "xtalk_reach_mils": 0}, b.to_dict(), STACKUP,
                                 tmp_path / "e")


def test_the_board_check_places_a_crosstalk_finding_at_the_coupled_stretch(tmp_path):
    b = beside(victim(), x1=300, x2=900)
    spec = {"targets": [{"match": "SIG", "kind": "net", "z_tol_pct": 60,
                         "data_rate_gbps": 2}],
            "points": 80, "f_max_ghz": 10}
    res = board_jobs.run_board_si(spec, b.to_dict(), STACKUP, tmp_path / "x", snapshot="t")
    f = next(x for x in res["figures"]["findings"] if x["check"] == "crosstalk")
    assert f["severity"] == "error" and f["item"] == "SIG"
    assert f["at_mils"] == [300.0, 0.0] and f["layer"] == "TopLayer"
    assert f["where"] == "runs 600 mil beside AGG at 4.0 mil"
    page = (tmp_path / "x" / "report.html").read_text()
    assert "xtalk %" in page and "beside AGG" in page
    head = (tmp_path / "x" / "summary.csv").read_text().splitlines()[0]
    assert "crosstalk_next_pct" in head and "crosstalk_fext_pct" in head
    # the board's own limit and switch reach every item
    res = board_jobs.run_board_si({**spec, "xtalk_max_pct": 30}, b.to_dict(), STACKUP,
                                  tmp_path / "y", snapshot="t")
    assert not any(x["check"] == "crosstalk" for x in res["figures"]["findings"])
    res = board_jobs.run_board_si({**spec, "xtalk": False}, b.to_dict(), STACKUP,
                                  tmp_path / "z", snapshot="t")
    assert not any(x["check"] == "crosstalk" for x in res["figures"]["findings"])
    items = json.loads((tmp_path / "z" / "findings.json").read_text())["items"]
    assert "crosstalk_pct" not in items[0]["metrics"] and "delay_ps" in items[0]["metrics"]


def test_crosstalk_closes_the_eye_by_its_noise_on_both_rails(tmp_path):
    from eda_agent.si import eye_jobs
    b = beside(victim(), x1=300, x2=900)
    target = {"match": "SIG", "kind": "net", "z_tol_pct": 60, "data_rate_gbps": 2, "eye": True,
              "xtalk_max_pct": 50}
    spec = {"targets": [target], "points": 200, "f_max_ghz": 10}

    def run(name, **more):
        board_jobs.run_board_si({"targets": [{**target, **more}], **{k: v for k, v in spec.items()
                                                                     if k != "targets"}},
                                b.to_dict(), STACKUP, tmp_path / name, snapshot="t")
        item = json.loads((tmp_path / name / "findings.json").read_text())["items"][0]
        return item["metrics"], next(v for v in item["verdicts"] if v["check"] == "eye")

    m, v = run("a")
    # the neighbours swing as the net does (800 mV) unless told otherwise
    assert m["eye_noise_mv"] == pytest.approx(m["crosstalk_pct"] / 100 * 800, abs=0.01)
    assert m["eye_height_with_noise_mv"] == pytest.approx(
        m["eye_height_mv"] - 2 * m["eye_noise_mv"], abs=0.01)
    assert v["pass"] and "of crosstalk on each rail" in v["detail"]
    assert f"{m['eye_height_with_noise_mv']:.1f} mV by" in v["detail"]
    # 3.3 V neighbours: four times the noise, and the limit is held against what is left
    m3, v3 = run("b", xtalk_swing_mv=3300, eye_min_mv=m["eye_height_with_noise_mv"] - 1)
    assert m3["eye_noise_mv"] == pytest.approx(m["eye_noise_mv"] * 3300 / 800, rel=1e-3)
    assert not v3["pass"] and m3["eye_height_mv"] == pytest.approx(m["eye_height_mv"])
    # no crosstalk asked, no noise
    m0, v0 = run("c", xtalk=False)
    assert "eye_noise_mv" not in m0 and "crosstalk" not in v0["detail"]
    # the eye job itself takes noise from any source
    ts = next((tmp_path / "a").rglob("channel.s2p"))
    res = eye_jobs.run_convolution({"data_rate_gbps": 2, "noise_mv": 50, "repeats": 1}, ts,
                                   tmp_path / "eye")
    em = {k: x["value"] for k, x in res["metrics"].items()}
    assert em["eye_height_with_noise_mv"] == pytest.approx(em["eye_height_mv"] - 100, abs=0.01)
    assert next(x for x in res["verdicts"] if x["check"] == "eye open with noise")["pass"]
    res = eye_jobs.run_convolution({"data_rate_gbps": 2, "noise_mv": 500, "repeats": 1}, ts,
                                   tmp_path / "eye2")
    assert res["metrics"]["eye_height_with_noise_mv"]["value"] == 0
    assert not next(x for x in res["verdicts"] if x["check"] == "eye open with noise")["pass"]
    with pytest.raises(ValueError, match="noise_mv"):
        eye_jobs.run_convolution({"data_rate_gbps": 2, "noise_mv": -1}, ts, tmp_path / "eye3")


# -- the facing signal layer (broadside) --------------------------------------------

@pytest.fixture(scope="module")
def dual():
    opt = plan_options({})
    stack = stack_from_stackup(DUAL_STACKUP, DUAL_LAYERS)
    info = {i["layer"]: i for i in planner.layer_sections(stack, opt)}
    return crosstalk.Coupling(info, opt, 0.35 / RISE, stack=stack)


def dual_victim():
    """The victim on a dual stripline board: its 2000 mil along x = 1000 is
    on Sig1, 8 mil of dielectric above Sig2."""
    b = victim()
    b.layers = list(DUAL_LAYERS)
    return b


def under(b, net="AGG", offset=0.0, y1=200.0, y2=1800.0, width=4.0, layer="MidLayer2"):
    """A track on the facing layer, ``offset`` to the side of the victim's
    inner run, centre to centre."""
    b.tracks.append(Track(layer, 1000 + offset, y1, 1000 + offset, y2, width, net))
    return b


def test_layers_face_each_other_only_with_no_plane_between(dual, coupling):
    assert dual.facing("MidLayer1") == [("MidLayer2", 8.0)]
    assert dual.facing("MidLayer2") == [("MidLayer1", 8.0)]
    assert dual.facing("TopLayer") == [] and dual.facing("BottomLayer") == []
    assert dual.facing("InternalPlane1") == [] and dual.facing("Nowhere") == []
    assert coupling.facing("MidLayer1") == []          # no stack given: same layer only
    stack = stack_from_stackup(STACKUP, LAYERS)        # a plane between every two signal layers
    assert crosstalk.Coupling(coupling.info, coupling.opt, 1e9, stack=stack).facing("MidLayer1") == []
    # two layers and no plane: nothing to solve the pair against
    two = stack_from_stackup({"layers": [STACKUP["layers"][0], STACKUP["layers"][-1]]},
                             [LAYERS[0], LAYERS[-1]])
    assert [x.name for x in two.layers] == ["TopLayer", "BottomLayer"]
    assert crosstalk.Coupling({}, coupling.opt, 1e9, stack=two).facing("TopLayer") == []


def test_the_facing_section_stands_both_traces_between_their_planes(dual):
    sec = dual.facing_section("MidLayer1", 4, "MidLayer2", 6, 3)
    up, low = sec.traces                               # the first trace is the layer asked for
    assert sec.kind == "broadside" and len(sec.traces) == 2
    assert low.y == pytest.approx(5 * MIL) and low.width == pytest.approx(6 * MIL)
    assert low.x == pytest.approx(3 * MIL) and up.x == 0 and up.width == pytest.approx(4 * MIL)
    assert up.y == pytest.approx((5 + 0.7 + 8) * MIL)
    assert sec.plane_above == pytest.approx((5 + 0.7 + 8 + 0.7 + 5) * MIL)
    # asked from the other layer, the same picture with the traces swapped
    low2, up2 = dual.facing_section("MidLayer2", 6, "MidLayer1", 4, 3).traces
    assert (low2.y, low2.width, up2.y, up2.width) == (low.y, low.width, up.y, up.width)
    assert low2.x == 0 and up2.x == pytest.approx(3 * MIL)


def test_the_facing_formulas_agree_with_the_coupled_line_solved_in_full(dual):
    """A trace on Sig2 right under one on Sig1 for 800 mil, and 6 mil to its
    side: the near-end peak of the full four-port against the closed form."""
    f = ch.uniform_band(20e9, 300)
    for offset in (0.0, 6.0):
        sec = dual.facing_section("MidLayer1", 4, "MidLayer2", 4, offset)
        s = mtl.s_params(rlgc.compute(sec, f), 800 * MIL, 50.0)
        _, near = net_model.step_response(f, s[:, 1, 0], RISE)
        res = of(under(dual_victim(), offset=offset, y1=600, y2=1400), dual)
        a = res["aggressors"][0]
        assert a["next_pct"] == pytest.approx(100 * np.abs(near).max(), rel=0.05)
        assert a["fext_pct"] < 0.3                      # one dielectric: no far end
    assert 6 < a["next_pct"] < 9


def test_a_track_on_the_facing_layer_is_an_aggressor(dual, coupling):
    res = of(under(dual_victim()), dual)
    a, = res["aggressors"]
    assert a["aggressor"] == "AGG" and a["layer"] == "MidLayer1"
    assert a["aggressor_layer"] == "MidLayer2" and a["offset_mils"] == 0
    assert a["gap_mils"] == 8.0 and a["length_mils"] == 1600 and a["at_mils"] == [1000, 200]
    assert 9 < a["next_pct"] < 12
    # it falls as the track moves aside, the same on either side, and ends at the reach
    side = [of(under(dual_victim(), offset=o), dual)["worst_pct"] for o in (0, 8, -8, 16, 30)]
    assert side[0] > side[1] > side[3] > side[4] > 0 and side[1] == pytest.approx(side[2])
    assert side[1] == pytest.approx(6.2, abs=0.6) and side[4] < 0.5
    assert not of(under(dual_victim(), offset=40), dual)["aggressors"]
    assert len(of(under(dual_victim(), offset=40), dual, reach_mils=50)["aggressors"]) == 1
    # a wider track over it couples more
    assert of(under(dual_victim(), width=12), dual)["worst_pct"] > side[0]
    # the victim is the first trace of the section: its own width, on its own layer
    run = crosstalk.Run("AGG", "MidLayer1", 4.0, 0.0, 800.0, (0, 0), other="MidLayer2",
                        other_width=12.0, sep=8.0)
    assert dual.of(run) == dual.get_facing("MidLayer1", 4, "MidLayer2", 12, 0, 8)
    assert dual.of(run)[0] > 1.2 * dual.get_facing("MidLayer2", 12, "MidLayer1", 4, 0, 8)[0]
    # crossing at right angles, on a layer that does not face it, or with no
    # stack to say what faces what: nothing
    b = dual_victim()
    b.tracks.append(Track("MidLayer2", 600, 1000, 1400, 1000, 4, "AGG"))
    assert not of(b, dual)["aggressors"]
    assert not of(under(dual_victim(), layer="BottomLayer"), dual)["aggressors"]
    assert not of(under(dual_victim()), crosstalk.Coupling(dual.info, dual.opt, dual.f))["aggressors"]
    b = under(victim())
    b.tracks[-1].layer = "BottomLayer"                  # the plane PWR lies between
    stack = stack_from_stackup(STACKUP, LAYERS)
    c = crosstalk.Coupling(coupling.info, coupling.opt, coupling.f, stack=stack)
    assert not of(b, c)["aggressors"]


def test_the_facing_layer_adds_to_the_same_layer_and_the_row_tells_the_stronger(dual):
    b = under(dual_victim(), "BUS", y1=200, y2=600)
    b.tracks.append(Track("MidLayer1", 1012, 1000, 1012, 1800, 4, "BUS"))   # 8 mil beside
    a, = of(b, dual)["aggressors"]
    alone = of(under(dual_victim(), "BUS", y1=200, y2=600), dual)["aggressors"][0]
    assert a["length_mils"] == 1200 and a["next_pct"] >= alone["next_pct"]
    assert a["aggressor_layer"] == "MidLayer2" and a["gap_mils"] == 8.0
    assert a["at_mils"] == [1000, 200]
    # with the facing track far to the side, the row is the one beside it
    b = under(dual_victim(), "BUS", offset=24, y1=200, y2=600)
    b.tracks.append(Track("MidLayer1", 1012, 1000, 1012, 1800, 4, "BUS"))
    a, = of(b, dual)["aggressors"]
    assert "aggressor_layer" not in a and a["gap_mils"] == 8.0 and a["at_mils"] == [1000, 1000]


def test_a_dual_stripline_is_referenced_to_its_planes():
    opt = plan_options({})
    stack = stack_from_stackup(DUAL_STACKUP, DUAL_LAYERS)
    info = {i["layer"]: i for i in planner.layer_sections(stack, opt)}
    s1, s2 = info["MidLayer1"], info["MidLayer2"]
    assert s1["structure"] == "stripline"
    assert s1["reference"] == ["InternalPlane1", "InternalPlane2"] == s2["reference"]
    assert (s1["h_above_mils"], s1["h_below_mils"]) == (5.0, 13.0)
    assert (s2["h_above_mils"], s2["h_below_mils"]) == (13.0, 5.0)
    assert "signal layer Sig2 lies between" in s1["note"] and "Sig1 lies between" in s2["note"]
    assert "note" not in info["TopLayer"] and info["TopLayer"]["reference"] == "InternalPlane1"
    z = planner.impedances(planner.build(s1, opt, 4 * MIL), 1e9)["z0"]
    assert 54 < z < 60                                  # 5 mil to one plane, 13.7 to the other
    # with no plane on a side, the next layer is the reference as before
    two = stack_from_stackup({"layers": [STACKUP["layers"][0], STACKUP["layers"][-1]]},
                             [LAYERS[0], LAYERS[-1]])
    top = planner.layer_sections(two, opt)[0]
    assert top["reference"] == "BottomLayer" and "keep it solid" in top["note"]


def test_the_net_job_and_the_board_check_say_which_layer(tmp_path):
    b = under(dual_victim())
    spec = {"net": "SIG", "points": 80, "rules": False, "rise_ps": 100}
    res = net_jobs.run_net_channel(spec, b.to_dict(), DUAL_STACKUP, tmp_path / "a")
    v = next(x for x in res["verdicts"] if x["check"] == "crosstalk")
    assert not v["pass"]
    assert "1600 mil on MidLayer2 across 8.0 mil of dielectric from MidLayer1" in v["detail"], v["detail"]
    row = res["figures"]["crosstalk"][0]
    assert row["aggressor_layer"] == "MidLayer2" and row["offset_mils"] == 0
    spec = {"targets": [{"match": "SIG", "kind": "net", "z_tol_pct": 60, "data_rate_gbps": 2}],
            "points": 80, "f_max_ghz": 10}
    res = board_jobs.run_board_si(spec, b.to_dict(), DUAL_STACKUP, tmp_path / "x", snapshot="t")
    f = next(x for x in res["figures"]["findings"] if x["check"] == "crosstalk")
    assert f["at_mils"] == [1000.0, 200.0] and f["layer"] == "MidLayer1"
    assert f["where"] == "runs 1600 mil across from AGG on MidLayer2, 8.0 mil of dielectric between"


# -- what brings it within the limit ------------------------------------------------

def test_the_fix_is_the_gap_that_brings_the_worst_neighbour_within_the_limit(coupling):
    b = beside(victim())                                # 4 mil for 800 mil: about 10 %
    res = of(b, coupling, limit_pct=5.0)
    fix = res["fix"]
    assert fix["aggressor"] == "AGG" and fix["limit_pct"] == 5.0 and "rest_pct" not in fix
    g = fix["gap_mils"]
    assert 6 < g < 12
    # routed at that gap it passes, and a little closer it does not
    assert of(beside(victim(), gap=g), coupling)["worst_pct"] <= 5.0
    assert of(beside(victim(), gap=g - 0.4), coupling)["worst_pct"] > 5.0
    # a looser limit asks for less, and within the limit there is nothing to fix
    assert of(b, coupling, limit_pct=8.0)["fix"]["gap_mils"] < g
    assert "fix" not in of(b, coupling, limit_pct=15.0) and "fix" not in of(b, coupling)
    assert "fix" not in of(victim(), coupling, limit_pct=5.0)


def test_the_fix_moves_only_the_worst_and_says_when_that_is_not_enough(coupling):
    # a second neighbour on the other side, further off: the first has to go further
    b = beside(beside(victim()), "B", gap=12, side=-1)
    alone = of(beside(victim()), coupling, limit_pct=5.0)["fix"]["gap_mils"]
    fix = of(b, coupling, limit_pct=5.0)["fix"]
    assert fix["aggressor"] == "AGG" and fix["gap_mils"] > alone + 1
    moved = beside(beside(victim(), gap=fix["gap_mils"]), "B", gap=12, side=-1)
    assert of(moved, coupling, reach_mils=60)["worst_pct"] == pytest.approx(5.0, abs=0.15)
    # two close neighbours: with one gone the other is still over
    b = beside(beside(victim()), "B", gap=4.5, side=-1)
    fix = of(b, coupling, limit_pct=5.0)["fix"]
    assert fix["aggressor"] == "AGG" and "gap_mils" not in fix
    assert fix["rest_pct"] == pytest.approx(of(beside(victim(), "B", gap=4.5, side=-1),
                                               coupling)["worst_pct"], abs=0.1)


def test_the_fix_moves_a_pair_as_it_is_routed_and_a_facing_track_to_the_side(coupling, dual):
    b = victim()
    for net, y in (("D_P", 9.0), ("D_N", 18.0)):        # a pair 4 mil from the net, 4 mil gap
        b.tracks.append(Track("TopLayer", 100, y, 900, y, 5, net))
    b.diff_pairs.append(DiffPair("D", "D_P", "D_N"))
    res = of(b, coupling, limit_pct=1.0, reach_mils=60)
    assert res["worst_pct"] > 1.0
    fix = res["fix"]
    assert fix["aggressor"] == "pair D" and 4.0 < fix["gap_mils"] < 20
    shift = fix["gap_mils"] - 4.0
    for t in b.tracks[-2:]:
        t.y1 += shift
        t.y2 += shift
    assert of(b, coupling, reach_mils=60)["worst_pct"] == pytest.approx(1.0, abs=0.1)
    # on the facing layer it is the offset to the side
    fix = of(under(dual_victim()), dual, limit_pct=5.0)["fix"]
    assert fix["aggressor_layer"] == "MidLayer2" and "gap_mils" not in fix
    assert 8 < fix["offset_mils"] < 20
    assert of(under(dual_victim(), offset=fix["offset_mils"]), dual)["worst_pct"] <= 5.0
    assert of(under(dual_victim(), offset=fix["offset_mils"] - 1), dual)["worst_pct"] > 5.0


def test_the_net_job_and_the_board_check_say_the_fix(tmp_path):
    b = beside(victim())
    spec = {"net": "SIG", "points": 80, "rules": False, "rise_ps": 100}
    res = net_jobs.run_net_channel(spec, b.to_dict(), STACKUP, tmp_path / "a")
    v = next(x for x in res["verdicts"] if x["check"] == "crosstalk")
    saved = json.loads((tmp_path / "a" / "crosstalk.json").read_text())
    g = saved["fix"]["gap_mils"]
    assert v["detail"].endswith(f"; AGG at {g:.1f} mil or more brings it within 5 %"), v["detail"]
    res = net_jobs.run_net_channel({**spec, "xtalk_max_pct": 8}, b.to_dict(), STACKUP,
                                   tmp_path / "b")
    v = next(x for x in res["verdicts"] if x["check"] == "crosstalk")
    assert "brings it within 8 %" in v["detail"]
    assert json.loads((tmp_path / "b" / "crosstalk.json").read_text())["fix"]["gap_mils"] < g
    res = net_jobs.run_net_channel({**spec, "xtalk_max_pct": 15}, b.to_dict(), STACKUP,
                                   tmp_path / "c")
    v = next(x for x in res["verdicts"] if x["check"] == "crosstalk")
    assert v["pass"] and "brings" not in v["detail"]
    assert net_jobs._fix_text({"aggressor": "B", "rest_pct": 6.123}, 5) == \
        "; moving B away is not enough, the other neighbours alone give 6.12 %"
    assert net_jobs._fix_text({"aggressor": "B", "offset_mils": 14.0}, 5) == \
        "; B 14.0 mil or more to the side brings it within 5 %"
    spec = {"targets": [{"match": "SIG", "kind": "net", "z_tol_pct": 60, "data_rate_gbps": 2}],
            "points": 80, "f_max_ghz": 10}
    res = board_jobs.run_board_si(spec, b.to_dict(), STACKUP, tmp_path / "x", snapshot="t")
    f = next(x for x in res["figures"]["findings"] if x["check"] == "crosstalk")
    assert f["detail"].endswith("mil or more brings it within 5 %") and "; AGG at " in f["detail"]
