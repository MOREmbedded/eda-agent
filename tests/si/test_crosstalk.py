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

from .boards import LAYERS, STACKUP, pair_board, single_net

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
