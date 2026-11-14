# SPDX-License-Identifier: Apache-2.0
"""Phase 3: channel cascade, via model, net extraction, net channels,
snapshots, the net-channel job and the si_net_channel tool."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP

from eda_agent import simulation as sim
from eda_agent.emc.stack import stack_from_stackup
from eda_agent.layout.model import Track
from eda_agent.pdn import physics as pdnp
from eda_agent.si import channel as ch
from eda_agent.si import extract, mtl, net_model
from eda_agent.si import snapshot as snap
from eda_agent.si import via as V
from eda_agent.si.rlgc import RLGC

from .boards import STACKUP, pair_board, single_net

C = 299792458.0


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SIM_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(tmp_path / "features.json"))
    monkeypatch.setattr(sim, "_loaded", None)


def _rlgc(f, n=1, z0=50.0, er=4.0, loss=True):
    """A synthetic line of impedance z0 (single) and Er."""
    v = C / math.sqrt(er)
    L1, C1 = z0 / v, 1 / (z0 * v)
    nf = len(f)
    R = np.zeros((nf, n, n))
    G = np.zeros((nf, n, n))
    if n == 1:
        Lm, Cm = np.array([[L1]]), np.array([[C1]])
    else:
        Lm = np.array([[L1, 0.15 * L1], [0.15 * L1, L1]])
        Cm = np.array([[C1, -0.1 * C1], [-0.1 * C1, C1]])
    for k in range(nf):
        if loss:
            R[k] = np.eye(n) * (3 + 2e-4 * math.sqrt(f[k]))
            G[k] = Cm * 2 * math.pi * f[k] * 0.02
    return RLGC(np.asarray(f), R, np.broadcast_to(Lm, (nf, n, n)).copy(), G,
                np.broadcast_to(Cm, (nf, n, n)).copy(), Cm / er)


# -- channel cascade --------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2])
def test_cascade_of_two_lines_is_the_whole_line(n):
    f = np.linspace(1e8, 2e10, 40)
    r = _rlgc(f, n)
    c = ch.Channel(f, n)
    c.add(ch.line_block(r, 0.03))
    c.add(ch.line_block(r, 0.07))
    assert np.max(np.abs(c.s() - mtl.s_params(r, 0.1))) < 1e-12


def test_cascade_holds_for_an_asymmetric_pair():
    """Unequal traces: Z and Y do not commute, so A and D differ."""
    f = np.linspace(1e8, 2e10, 30)
    nf = len(f)
    Lm = np.array([[3.2e-7, 5e-8], [5e-8, 2.6e-7]])
    Cm = np.array([[1.1e-10, -1.8e-11], [-1.8e-11, 1.5e-10]])
    R = np.array([np.diag([4.0, 7.0]) + 1e-4 * math.sqrt(x) * np.eye(2) for x in f])
    G = np.array([Cm * 2 * math.pi * x * 0.015 for x in f])
    r = RLGC(f, R, np.broadcast_to(Lm, (nf, 2, 2)).copy(), G,
             np.broadcast_to(Cm, (nf, 2, 2)).copy(), Cm / 4)
    c = ch.Channel(f, 2)
    c.add(ch.line_block(r, 0.02))
    c.add(ch.line_block(r, 0.05))
    assert np.max(np.abs(c.s() - mtl.s_params(r, 0.07))) < 1e-12


def test_matched_lossless_line_is_transparent_and_delays():
    f = ch.uniform_band(10e9, 400)
    r = _rlgc(f, 1, 50.0, 4.0, loss=False)
    c = ch.Channel(f, 1)
    c.add(ch.line_block(r, 0.1))
    s = c.s(50.0)
    assert np.max(np.abs(s[:, 0, 0])) < 1e-9
    assert np.allclose(np.abs(s[:, 1, 0]), 1.0, atol=1e-9)
    t, step = net_model.step_response(f, s[:, 1, 0], 30e-12)
    assert net_model.delay_50(t, step) == pytest.approx(0.1 * 2 / C, rel=0.002)


def test_tdr_reads_the_line_impedance():
    f = ch.uniform_band(20e9, 800)
    r = _rlgc(f, 1, 65.0, 4.0, loss=False)
    c = ch.Channel(f, 1)
    c.add(ch.line_block(r, 0.1))
    t, z = net_model.tdr(f, c.s(50.0)[:, 0, 0], 50.0, 30e-12)
    mid = (t > 0.2e-9) & (t < 1.0e-9)       # inside the line (1.33 ns round trip)
    assert np.median(z[mid]) == pytest.approx(65.0, rel=0.01)


def test_open_stub_notches_at_a_quarter_wave():
    f = ch.uniform_band(4e9, 800)
    r = _rlgc(f, 1, 50.0, 4.0, loss=False)
    c = ch.Channel(f, 1)
    c.add(ch.stub_block(r, 0.01))           # quarter wave at C / (2 * 4 * 0.01) = 3.75 GHz
    s21 = np.abs(c.s()[:, 1, 0])
    assert f[np.argmin(s21)] == pytest.approx(C / 2 / (4 * 0.01), rel=0.01)
    assert s21.min() < 0.01            # sampled 5 MHz from the true zero


def test_per_conductor_blocks_stay_uncoupled():
    f = np.array([1e9])
    r = _rlgc(f, 1)
    a = ch.line_block(r, 0.02).mats[0]
    b = ch.line_block(r, 0.05).mats[0]
    m = ch.per_conductor([a, b])
    s = ch.abcd_to_s(m)
    assert abs(s[1, 0]) < 1e-12 and abs(s[3, 0]) < 1e-12 and abs(s[2, 1]) < 1e-12
    assert s[2, 0] == pytest.approx(ch.abcd_to_s(a)[1, 0], abs=1e-12)


# -- via ---------------------------------------------------------------------------------

def _via(**kw):
    g = dict(drill_mils=10, pad_mils=20, antipad_mils=36, z_entry=60, z_exit=40, z_low=0,
             z_high=60, er=4.0, tand=0.0, planes=[55, 20])
    g.update(kw)
    return V.ViaGeometry(**g)


def test_via_barrel_is_a_coax_and_the_stub_rings_where_it_should():
    g = _via()
    assert V.barrel_impedance(g) == pytest.approx(30 * math.log(3.6))
    f = ch.uniform_band(80e9, 1600)
    mats, info = V.two_port(g, f)
    assert info["stub_mils"] == pytest.approx(40)
    f_res = C / (4 * 40 * V.MIL * 2.0)
    assert info["stub_resonance_hz"] == pytest.approx(f_res)
    s21 = np.abs([ch.abcd_to_s(m)[1, 0] for m in mats])
    assert f[np.argmin(s21)] == pytest.approx(f_res, rel=0.05)


def test_backdrill_removes_the_stub():
    f = np.array([1e9, 30e9])
    _, info = V.two_port(_via(backdrill_to=40), f)
    assert info["stub_mils"] == 0 and info["stub_resonance_hz"] is None


def test_return_loop_uses_the_via_pair_formula():
    g = _via(return_height_mils=35, return_spacing_mils=40, return_drill_mils=10)
    _, info = V.two_port(g, [1e9])
    assert info["return_l_h"] == pytest.approx(pdnp.via_pair_loop_inductance(35, 40, 5))
    assert info["return_l_h"] > 0


def test_short_via_is_nearly_transparent_at_low_frequency():
    mats, _ = V.two_port(_via(z_entry=60, z_exit=55, planes=[]), [1e8])
    assert abs(ch.abcd_to_s(mats[0])[1, 0]) > 0.999


# -- extraction ----------------------------------------------------------------------------

def test_path_sections_vias_and_stub():
    p = extract.net_path(single_net(), "SIG")
    assert (p.start, p.end) == ("U1.1", "U2.1")
    kinds = [(s.kind, s.layer or s.to_layer, round(s.length)) for s in p.sections]
    # the run is cut at the tee, so the stub hangs 600 mil from U1, where it is
    assert kinds == [("line", "TopLayer", 600), ("line", "TopLayer", 400),
                     ("via", "MidLayer1", 0), ("line", "MidLayer1", 2000),
                     ("via", "TopLayer", 0), ("line", "TopLayer", 500)]
    assert len(p.stubs) == 1
    st = p.stubs[0]
    assert st.length == pytest.approx(300) and st.to == "TP1.1" and st.at == 0
    assert st.xy == pytest.approx((600, 0), abs=0.1)
    assert p.length == pytest.approx(3500)


def test_explicit_pins_and_a_stub_end_as_the_target():
    p = extract.net_path(single_net(), "SIG", "U1.1", "TP1.1")
    assert p.length == pytest.approx(900) and p.vias == 0
    # the rest of the route (400 + 2000 + 500 mil) hangs off the tee as one stub
    assert [round(s.length) for s in p.stubs] == [2900]
    assert p.stubs[0].to == "U2.1"


def test_tee_in_the_middle_of_a_track_is_split():
    b = single_net(with_stub=False)
    # one long track with a branch landing on its middle
    b.tracks[0:2] = [Track("TopLayer", 0, 0, 1000, 0, 5, "SIG")]
    # the branch lands 2 mil off the centre line, inside the 5 mil track
    b.tracks.append(Track("TopLayer", 400, 2, 400, 200, 5, "SIG"))
    p = extract.net_path(b, "SIG")
    assert p.length == pytest.approx(3500, abs=0.01)      # the 2 mil kink at the cut
    assert [round(s.length) for s in p.stubs] == [198]


def test_unrouted_and_unknown_pins_are_refused():
    b = single_net()
    b.tracks = [t for t in b.tracks if t.layer != "MidLayer1"]
    with pytest.raises(extract.ExtractError):
        extract.net_path(b, "SIG", "U1.1", "U2.1")
    with pytest.raises(extract.ExtractError):
        extract.net_path(single_net(), "SIG", "U9.9", "U2.1")
    with pytest.raises(extract.ExtractError):
        extract.net_path(single_net(), "NOPE")


def test_pair_is_matched_into_coupled_and_breakout_stretches():
    pp = extract.pair_path(pair_board(), "D", "D_P", "D_N")
    assert len(pp.zones) == 3
    coupled = pp.zones[1]["coupled"]
    assert len(coupled) == 1
    layer, w, gap, L = coupled[0]
    assert (layer, w, gap) == ("MidLayer1", 4, 5.0) and L == pytest.approx(1960, abs=10)
    assert not pp.zones[0]["coupled"] and not pp.zones[2]["coupled"]
    assert extract.find_pair(pair_board(), "D_N") == ("D_P", "D_N")
    with pytest.raises(extract.ExtractError):
        extract.find_pair(pair_board(), "X")


# -- channels on the board ------------------------------------------------------------------

@pytest.fixture(scope="module")
def single_model():
    b = single_net()
    stack = stack_from_stackup(STACKUP, b.layers)
    p = extract.net_path(b, "SIG")
    f = ch.uniform_band(12e9, 240)
    return net_model.single(b, stack, p, f), p


def test_single_channel_is_physical_and_sees_the_stub(single_model):
    m, p = single_model
    assert ch.passivity(m.s) <= 1 + 1e-9 and ch.reciprocity(m.s) < 1e-9
    top = m.sections["TopLayer 5 mil"]
    er_eff = (top["delay_ps_per_in"] * 1e-12 / 0.0254 * C) ** 2
    f_notch = C / (4 * 300 * V.MIL * math.sqrt(er_eff))
    s21 = np.abs(m.s[:, 1, 0])
    band = (m.f > 0.5 * f_notch) & (m.f < 1.5 * f_notch)
    assert m.f[band][np.argmin(s21[band])] == pytest.approx(f_notch, rel=0.08)
    figs = net_model.figures(m, 2e9, 35e-12)
    # the tee: two ~57 ohm lines in parallel
    assert 22 < figs["tdr_min_ohms"] < 35
    delay = (1500 * top["delay_ps_per_in"] + 2000 * m.sections["MidLayer1 4 mil"]
             ["delay_ps_per_in"]) / 1000
    # The 50 % point of a lossy, stubbed line's step comes a little after
    # its phase delay (the loss slows the edge), never before it.
    assert delay < figs["delay_ps"] < 1.08 * delay


def test_line_loss_is_the_lines_alone_and_the_stub_notch_is_named(single_model):
    m, p = single_model
    k = len(m.f) // 2
    per_in = {name: v["loss_db_per_in"] for name, v in m.sections.items()}
    # the sections' own loss figures are at the band's middle, as is k
    expect = (1500 * per_in["TopLayer 5 mil"] + 2000 * per_in["MidLayer1 4 mil"]) / 1000
    assert m.line_loss_db[k] == pytest.approx(expect, rel=1e-3)
    assert m.line_mils == pytest.approx(3500, abs=1)        # the stub is not a line of the path
    figs = net_model.figures(m, 2e9, 35e-12)
    assert figs["stub_resonance_of"] == "300 mil trace stub"
    s21 = np.abs(m.s[:, 1, 0])
    fq = figs["stub_resonance_hz"]
    band = (m.f > 0.6 * fq) & (m.f < 1.4 * fq)
    assert m.f[band][np.argmin(s21[band])] == pytest.approx(fq, rel=0.06)


def test_pair_line_loss_is_the_odd_mode():
    from eda_agent.si import rlgc as _r
    f = ch.uniform_band(10e9, 50)
    r = _rlgc(f, n=2)
    k = 25
    figs = _r.figures(r, k)
    assert abs(figs["loss_diff_db_per_in"] - figs["loss_common_db_per_in"]) > 0.01
    got = net_model.line_loss_db(r, 1000.0, odd=True)[k]
    assert got == pytest.approx(figs["loss_diff_db_per_in"], rel=1e-9)
    single = _rlgc(f)
    assert net_model.line_loss_db(single, 1000.0)[k] == pytest.approx(
        _r.figures(single, k)["loss_db_per_in"], rel=1e-9)


def test_pair_skew_follows_the_length_mismatch():
    b = pair_board(skew=0)
    stack = stack_from_stackup(STACKUP, b.layers)
    pp = extract.pair_path(b, "D", "D_P", "D_N")
    f = ch.uniform_band(8e9, 160)
    m = net_model.pair(b, stack, pp, f)
    figs = net_model.figures(m, 2e9, 35e-12)
    per_mil = m.sections["MidLayer1 4 mil"]["delay_ps_per_in"] / 1000
    expect = (pp.p.length - pp.n.length) * per_mil
    assert figs["skew_ps"] == pytest.approx(expect, abs=1.5)
    assert figs["skew_ps"] < -4
    assert ch.passivity(m.s) <= 1 + 1e-9
    b2 = pair_board(skew=50)
    pp2 = extract.pair_path(b2, "D", "D_P", "D_N")
    m2 = net_model.pair(b2, stack, pp2, f)
    figs2 = net_model.figures(m2, 2e9, 35e-12)
    assert abs(figs2["skew_ps"]) < 1.5
    assert figs2["scd21_db_at_nyquist"] < figs["scd21_db_at_nyquist"] - 10
    mm = m.sdd
    assert np.all(np.abs(mm[:, 1, 0]) <= 1)


def test_return_via_matters_when_the_reference_changes():
    b = single_net(with_stub=False, with_return_via=False)
    b.tracks[2] = Track("BottomLayer", 1000, 0, 1000, 2000, 4, "SIG")
    stack = stack_from_stackup(STACKUP, b.layers)
    p = extract.net_path(b, "SIG")
    m = net_model.single(b, stack, p, ch.uniform_band(6e9, 60))
    vias = [x for x in m.blocks if x["kind"] == "via"]
    assert vias and all(v["return_loop_nh"] > 0 for v in vias)
    assert any("no return via" in n for n in m.notes)
    b.vias += [extract_via(1030, 0), extract_via(1030, 2000)]
    m2 = net_model.single(b, stack, extract.net_path(b, "SIG"), ch.uniform_band(6e9, 60))
    near = [x["return_loop_nh"] for x in m2.blocks if x["kind"] == "via"]
    assert max(near) < min(v["return_loop_nh"] for v in vias)


def extract_via(x, y):
    from .boards import via
    return via(x, y, "GND")


# -- snapshot ---------------------------------------------------------------------------------

def test_snapshot_is_content_named_and_goes_stale(tmp_path):
    pcb = tmp_path / "b.PcbDoc"
    pcb.write_text("x")
    b = single_net()
    r1 = snap.save(b, STACKUP, str(pcb))
    r2 = snap.save(b, STACKUP, str(pcb))
    assert r1["id"] == r2["id"] and r1["has_stackup"]
    assert snap.status(r1)["stale"] is False
    os.utime(pcb, (time.time() + 10, time.time() + 10))
    assert snap.status(snap.load(r1["id"]))["stale"] is True
    b.tracks.pop()
    assert snap.save(b, STACKUP, str(pcb))["id"] != r1["id"]
    assert len(snap.listing()) == 2
    with pytest.raises(KeyError):
        snap.load("nope")


# -- job and tool -------------------------------------------------------------------------------

def _files(tmp_path, board):
    bp, sp = tmp_path / "board.json", tmp_path / "stackup.json"
    board.save(bp)
    sp.write_text(json.dumps(STACKUP), encoding="utf-8")
    return str(bp), str(sp)


def test_net_channel_job_writes_its_files(tmp_path):
    bp, sp = _files(tmp_path, single_net())
    r = sim.submit("net-channel", "eda-agent",
                   {"net": "SIG", "f_max_ghz": 8, "points": 80, "data_rate_gbps": 4,
                    "il_max_db": 30}, {"board": bp, "stackup": sp}, "t", True)
    assert r["state"] == "done", r
    st = sim.refresh(r["job"])
    res = st["runs"]["eda-agent"]["results"]
    assert res["metrics"]["vias"]["value"] == 2 and res["metrics"]["stubs"]["value"] == 1
    folder = sim._jobs.get(r["job"]).work_folder("eda-agent")
    for name in ("channel.s2p", "path.json", "tdr.csv", "sections.json"):
        assert (folder / name).is_file(), name
    checks = {v["check"]: v["pass"] for v in res["verdicts"]}
    assert checks["passivity"] and checks["reciprocity"] and checks["insertion loss"]
    assert checks["impedance"] is False        # the stub's tee
    rules = {s["args"]["rule_type"]: s["args"] for s in res["altium_steps"]}
    assert rules["width"]["value"] < rules["width"]["favored_value"] < rules["width"]["max_value"]
    assert rules["length"]["max_value"] > 3500 and rules["length"]["scope"] == "InNet('SIG')"


def test_pair_job_rules_hold_the_pair(tmp_path):
    # 49 mil mismatch, about 8 ps: over 5 % of the 125 ps unit interval at 8 Gb/s
    bp, sp = _files(tmp_path, pair_board(skew=0))
    r = sim.submit("net-channel", "eda-agent",
                   {"pair": "D", "f_max_ghz": 8, "points": 80, "data_rate_gbps": 8},
                   {"board": bp, "stackup": sp}, "t", True)
    assert r["state"] == "done", r
    res = sim.refresh(r["job"])["runs"]["eda-agent"]["results"]
    assert res["metrics"]["skew_ps"]["value"] == pytest.approx(-8.3, abs=1.5)
    skew = next(v for v in res["verdicts"] if v["check"] == "skew")
    assert skew["pass"] is False and "6.25 ps allowed" in skew["detail"]
    rules = {s["args"]["rule_type"]: s["args"] for s in res["altium_steps"]}
    assert rules["differential_pairs"]["scope"] == "InDifferentialPair('D')"
    assert rules["differential_pairs"]["max_uncoupled_length"] > 10


def _length_rule(board, rate=10.0, il=20.0):
    from eda_agent.si.net_jobs import run_net_channel
    import tempfile
    r = run_net_channel({"net": "SIG", "data_rate_gbps": rate, "il_max_db": il, "f_max_ghz": 12,
                         "points": 120}, board.to_dict(), STACKUP, tempfile.mkdtemp())
    rule = next(s for s in r["altium_steps"] if s["args"]["rule_type"] == "length")
    return rule, {v["check"]: v for v in r["verdicts"]}, r["metrics"]


def test_length_budget_spends_headroom_at_the_lines_loss():
    # vias, the stub and mismatch cost a fixed 0.6 dB here; scaling the
    # whole loss by length would charge it again for every extra mil
    rule, checks, m = _length_rule(single_net(inner=2000), rate=6.0)
    L = m["length_mils"]["value"]
    il, line = m["insertion_loss_db"]["value"], m["line_loss_db"]["value"]
    assert il - line > 0.3
    want = L + (20 - il) / (line / L)
    assert rule["args"]["max_value"] == pytest.approx(want, rel=5e-3)
    assert rule["args"]["max_value"] > 1.2 * 20 / il * L
    # and it moves little with the route
    other, _, _ = _length_rule(single_net(inner=5000), rate=6.0)
    assert other["args"]["max_value"] == pytest.approx(rule["args"]["max_value"], rel=0.1)
    assert "per 1000 mil" in rule["why"] and "notches at" in rule["why"]
    stub = checks["stub resonance"]
    assert stub["pass"] is False and "300 mil trace stub" in stub["detail"]
    _, slow, _ = _length_rule(single_net(inner=2000), rate=2.5)
    assert slow["stub resonance"]["pass"] is True
    # the limit follows the data rate, not the nearest point of the sweep
    assert "2 x Nyquist = 2.50 GHz needed" in slow["stub resonance"]["detail"]


def test_job_refuses_a_net_it_cannot_find(tmp_path):
    bp, sp = _files(tmp_path, single_net())
    r = sim.submit("net-channel", "eda-agent", {"net": "NOPE", "points": 40},
                   {"board": bp, "stackup": sp}, "t", True)
    assert r["state"] == "failed" and "NOPE" in r.get("error", json.dumps(r))


def _call(m, name, args):
    out = asyncio.run(m.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def test_tool_runs_from_files_and_applies_rules(tmp_path, monkeypatch):
    from eda_agent.tools import pcb as pcb_tools
    from eda_agent.tools.si import register_si_tools
    sent = []

    async def fake_edit(tool, args, command, params):
        sent.append((command, params))
        return {"ok": True}

    monkeypatch.setattr(pcb_tools, "_rule_edit", fake_edit)
    m = FastMCP("t")
    register_si_tools(m)
    bp, sp = _files(tmp_path, single_net())
    snap_reply = _call(m, "si_board_snapshot", {"board_json_path": bp, "stackup_json_path": sp})
    assert snap_reply["ok"] and snap_reply["has_stackup"]
    out = _call(m, "si_net_channel", {"net": "SIG", "snapshot": snap_reply["id"],
                                      "options": {"points": 40, "f_max_ghz": 6,
                                                  "il_max_db": 30},
                                      "apply_rules": True})
    assert out["ok"], out
    assert out["snapshot"]["id"] == snap_reply["id"]
    assert [c for c, _ in sent] == ["pcb.create_design_rule"] * len(out["altium_steps"])
    assert sent and all("name" in p for _, p in sent)
    # a snapshot of a board saved since: results come back, rules are not written
    pcb = tmp_path / "b.PcbDoc"
    pcb.write_text("x")
    from eda_agent.layout.model import LayoutBoard
    rec = snap.save(LayoutBoard.load(bp), STACKUP, str(pcb))
    os.utime(pcb, (time.time() + 10, time.time() + 10))
    sent.clear()
    stale = _call(m, "si_net_channel", {"net": "SIG", "snapshot": rec["id"],
                                        "options": {"points": 40, "f_max_ghz": 6,
                                                    "il_max_db": 30},
                                        "apply_rules": True})
    assert stale["snapshot"]["stale"] is True and stale["applied"] == [] and not sent
    assert "changed" in stale["error"]
    listed = _call(m, "si_board_snapshot", {"list_only": True})
    assert {x["id"] for x in listed["snapshots"]} == {snap_reply["id"], rec["id"]}
    assert _call(m, "si_net_channel", {})["ok"] is False
