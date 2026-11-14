# SPDX-License-Identifier: Apache-2.0
"""Time domain: vector fitting, the SPICE subcircuit, eyes, IBIS buffers."""
from __future__ import annotations

import asyncio
import json
import math
import shutil
import subprocess

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP

from eda_agent import simulation as sim
from eda_agent.emc.touchstone import write_touchstone
from eda_agent.si import channel as ch
from eda_agent.si import eye, ibis, spice_export, spice_eye, vectfit
from eda_agent.si.eye_jobs import terminations
from eda_agent.si.mtl import mixed_mode

from .ibis_text import R_OUT, REAL, TEXT, VCC
from .test_nets import _rlgc

NGSPICE = shutil.which("ngspice")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SIM_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(tmp_path / "features.json"))
    monkeypatch.setattr(sim, "_loaded", None)


def lossy_line(f, length=0.02, z0=55.0, loss=True):
    """A 55 ohm line of Er 4 with skin and dielectric loss, between 50 ohm
    ports: causal and passive, as a real channel is."""
    f = np.asarray(f, dtype=float)
    c = ch.Channel(f, 1)
    c.add(ch.line_block(_rlgc(f, z0=z0, loss=loss), length))
    return c.s(50.0)


def rational(f, poles, residues, d=0.0):
    s = 2j * np.pi * np.asarray(f)
    h = np.full(len(s), d, dtype=complex)
    for p, r in zip(poles, residues):
        h += r / (s - p)
    return h


# -- PRBS and edges --------------------------------------------------------------------

@pytest.mark.parametrize("order", [7, 9, 11])
def test_prbs_is_maximal_length(order):
    n = (1 << order) - 1
    bits = eye.prbs(order, 2 * n)
    assert np.array_equal(bits[:n], bits[n:])
    assert int(bits[:n].sum()) == (n + 1) // 2            # one more one than zeros
    for p in range(1, n):
        if n % p == 0:
            assert not np.array_equal(bits[:n - p], bits[p:n])
    with pytest.raises(ValueError):
        eye.prbs(8)


def test_step_of_a_flat_channel_is_the_ramp():
    f = ch.uniform_band(100e9, 2000)
    rise = 30e-12
    t, s = eye.step_response(f, np.ones(len(f), dtype=complex), rise)
    assert s[-1] == pytest.approx(1.0, abs=2e-3)
    t10 = np.interp(0.1, s[: np.argmax(s > 0.95)], t[: np.argmax(s > 0.95)])
    t90 = np.interp(0.9, s[: np.argmax(s > 0.95)], t[: np.argmax(s > 0.95)])
    assert t90 - t10 == pytest.approx(rise, rel=0.03)


# -- eyes ------------------------------------------------------------------------------

def test_flat_channel_eye_is_the_full_swing():
    f = ch.uniform_band(100e9, 2000)
    p = eye.pulse_response(f, np.ones(len(f), dtype=complex), 100e-12, 20e-12, 32)
    se = eye.simulated_eye(p, 0.8)
    wc = eye.worst_case_eye(p, 0.8)
    assert se["eye_height"] == pytest.approx(0.8, abs=4e-3)
    assert wc["eye_height"] == pytest.approx(0.8, abs=4e-3)
    # rising and falling linear edges cross at half swing, so a flat
    # channel's eye shuts only at that instant: open for all but a sample
    assert se["eye_width_ui"] == pytest.approx(1.0, abs=1.5 / 32)
    assert se["eye_width_ui"] <= 1.0


def test_an_eye_open_at_every_sample_is_under_one_interval_wide():
    """When the crossing falls between two samples every sample is open:
    the width is then a sample short of the interval, never more than it
    (it read 33 / 32 UI)."""
    spui = 32
    rows = np.vstack([np.full(spui, 0.4), np.full(spui, -0.4)] * 4)
    bits = np.array([1, 0] * 4)
    m = eye.eye_metrics(rows, bits, 1e-9)
    assert m["eye_width_ui"] == pytest.approx(31 / 32) and m["eye_width"] == pytest.approx(31 / 32 * 1e-9)
    # a closed sample anywhere: counted as before
    rows[:, 5] = 0.0
    assert eye.eye_metrics(rows, bits, 1e-9)["eye_width_ui"] == pytest.approx(31 / 32)
    rows[:, 6] = 0.0
    assert eye.eye_metrics(rows, bits, 1e-9)["eye_width_ui"] == pytest.approx(30 / 32)


def test_worst_case_eye_bounds_every_pattern():
    f = ch.uniform_band(40e9, 800)
    s = lossy_line(f, length=0.3)
    p = eye.pulse_response(f, s[:, 1, 0], 100e-12, 30e-12, 32)
    se = eye.simulated_eye(p, 0.8, order=9, repeats=1)
    wc = eye.worst_case_eye(p, 0.8)
    assert 0 < wc["eye_height"] < se["eye_height"] < 0.8
    # PDA's worst case is a pattern: send it and the waveform reaches it
    pk, spui = p.peak_index, p.spui
    idx = np.arange(pk % spui, len(p.p), spui)
    cur = p.p[idx]
    main = int(np.flatnonzero(idx == pk)[0])
    L = len(cur)
    sym = -np.sign(cur)
    sym[main] = 1.0
    # bit i meets cursor j = L - 1 - i at the decision of bit L - 1 - main
    bits = (sym[::-1] > 0).astype(int)
    _, v = eye.waveform(p, bits, 0.8)
    at = (L - 1 - main) * spui + pk
    assert v[at] == pytest.approx(0.4 * cur[main] - 0.4 * (np.abs(cur).sum() - abs(cur[main])),
                                  abs=1e-12)


# -- vector fitting ---------------------------------------------------------------------

def test_vector_fitting_recovers_known_poles():
    poles = np.array([-2e9, -1e9 + 3e10j, -1e9 - 3e10j, -5e9 + 9e10j, -5e9 - 9e10j])
    res = np.array([3e9, 2e9 + 1e9j, 2e9 - 1e9j, 4e9 - 2e9j, 4e9 + 2e9j])
    f = np.linspace(1e7, 30e9, 300)
    h = rational(f, poles, res, 0.1)
    S = np.zeros((len(f), 1, 1), dtype=complex)
    S[:, 0, 0] = h
    m = vectfit.fit(f, S, n_poles=6)
    assert vectfit.rms_error(m, f, S) < 1e-8
    for p in poles:
        assert np.min(np.abs(m.poles - p)) / abs(p) < 1e-5


def test_fit_grows_until_the_tolerance_and_stays_stable_and_reciprocal():
    f = np.linspace(50e6, 20e9, 200)
    s = lossy_line(f)
    m = vectfit.fit(f, s, tol=1e-3)
    assert m.info["rms_error"] <= 1e-3
    assert np.all(m.poles.real < 0)
    assert np.allclose(m.residues, np.transpose(m.residues, (0, 2, 1)))


def test_passivity_is_enforced_on_a_model_that_gains():
    f = np.linspace(50e6, 20e9, 200)
    s = lossy_line(f) * 1.04                  # over 1 at low frequency: not passive
    m = vectfit.fit(f, s, tol=1e-3)
    assert vectfit.max_singular(m, vectfit.check_grid(f))[0] > 1.0
    p = vectfit.enforce_passivity(m, f, s)
    assert p.info["passivity_before"] > 1.0
    assert p.info["passivity_after"] <= 1.0
    assert p.info["rms_error_after_enforcement"] < 0.03


# -- SPICE subcircuit -------------------------------------------------------------------

def test_subcircuit_uses_only_linear_elements_and_names_its_ports():
    f = np.linspace(50e6, 20e9, 120)
    m = vectfit.macromodel(f, lossy_line(f), 2e-3)
    sub = spice_export.subcircuit(m, "chan", 50.0, ["a", "b"])
    lines = [x for x in sub.splitlines() if x and not x.startswith("*")]
    assert lines[0] == ".subckt chan a b ref" and lines[-1] == ".ends chan"
    assert {x[0] for x in lines[1:-1]} <= set("RCEG")
    assert spice_export.element_count(sub) == len(lines) - 2
    with pytest.raises(ValueError):
        spice_export.subcircuit(m, "chan", 50.0, ["a"])


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
@pytest.mark.parametrize("ports", [2, 4])
def test_ngspice_ac_of_the_subcircuit_is_the_model(tmp_path, ports):
    f = np.linspace(50e6, 15e9, 120)
    s = lossy_line(f)
    if ports == 4:
        big = np.zeros((len(f), 4, 4), dtype=complex)
        big[:, 0, 2] = big[:, 2, 0] = big[:, 1, 3] = big[:, 3, 1] = s[:, 1, 0]
        big[:, 0, 1] = big[:, 1, 0] = big[:, 2, 3] = big[:, 3, 2] = 0.05 * s[:, 1, 0] ** 2
        s = big
    m = vectfit.macromodel(f, s, 2e-3)
    sub = spice_export.subcircuit(m, "channel")
    deck = spice_export.ac_check_deck(m, sub, [1e8, 15e9]).replace(".ac lin 2", ".ac lin 60")
    nodes = " ".join(f"v(n{k})" for k in range(ports))
    deck = deck.replace(".end\n", f".control\nrun\nwrdata {tmp_path}/out.txt {nodes}\n.endc\n.end\n")
    (tmp_path / "deck.cir").write_text(deck)
    subprocess.run([NGSPICE, "-b", str(tmp_path / "deck.cir")], capture_output=True, timeout=300,
                   check=False)
    data = np.loadtxt(tmp_path / "out.txt")
    ff = data[:, 0]
    me = m.eval(ff)
    for k in range(ports):
        v = data[:, 3 * k + 1] + 1j * data[:, 3 * k + 2]
        want = me[:, k, 0] + (1 if k == 0 else 0)
        assert np.max(np.abs(v - want)) < 1e-6, k


# -- terminations ----------------------------------------------------------------------

def test_matched_terminations_give_s21_and_sdd21():
    rng = np.random.default_rng(3)
    f = np.linspace(1e8, 1e10, 20)
    s = 0.2 * (rng.normal(size=(20, 4, 4)) + 1j * rng.normal(size=(20, 4, 4)))
    s = 0.5 * (s + np.transpose(s, (0, 2, 1)))
    assert np.allclose(eye.terminated_transfer(s, f, 50, 50, 0, 50, 0), mixed_mode(s)[:, 1, 0],
                       atol=1e-12)
    s2 = s[:, :2, :2]
    assert np.allclose(eye.terminated_transfer(s2, f, 50, 50, 0, 50, 0), s2[:, 1, 0], atol=1e-12)


def test_terminations_follow_the_circuit():
    f = np.linspace(1e8, 1e10, 20)
    thru = np.zeros((20, 2, 2), dtype=complex)
    thru[:, 0, 1] = thru[:, 1, 0] = 1.0
    assert np.allclose(eye.terminated_transfer(thru, f, 50, 50, 0, None, 0), 2.0)
    assert np.allclose(eye.terminated_transfer(thru, f, 50, 25, 0, 50, 0), 4 / 3)
    # a capacitor at the load in parallel with 50 ohm, from a 30 ohm source
    c = 1e-12
    zl = 1 / (1 / 50 + 2j * np.pi * f * c)
    want = 2 * zl / (30 + zl)
    assert np.allclose(eye.terminated_transfer(thru, f, 50, 30, 0, 50, c), want, rtol=1e-12)
    # the source pad capacitance on the same node
    assert np.allclose(eye.terminated_transfer(thru, f, 50, 30, c / 2, 50, c / 2), want,
                       rtol=1e-12)


# -- IBIS --------------------------------------------------------------------------------

def test_ibis_numbers_and_tables():
    assert ibis.number("3.5pF") == pytest.approx(3.5e-12)
    assert ibis.number("120m") == pytest.approx(0.12)
    assert ibis.number("2meg") == pytest.approx(2e6)
    assert ibis.number("1.2e-3") == pytest.approx(1.2e-3)
    assert ibis.number("NA") is None
    models = ibis.parse(TEXT)
    assert set(models) == {"DRV18", "RX18"}
    drv = models["DRV18"]
    assert drv.model_type == "Output" and drv.voltage == VCC
    assert drv.c_comp == pytest.approx([0.6e-12, 0.5e-12, 0.7e-12])
    assert drv.ramp_r == pytest.approx((1.08, 60e-12))
    d = ibis.driver(drv)
    assert d["r_out_ohms"] == pytest.approx(R_OUT, rel=1e-6)
    assert d["rise_s"] == pytest.approx(80e-12)         # 20-80 % of 60 ps is 80 ps 10-90 %
    assert d["swing_v"] == pytest.approx(1.8)
    assert ibis.receiver(models["RX18"])["c_comp_f"] == pytest.approx(0.4e-12)
    with pytest.raises(ValueError, match="name one"):
        ibis.pick(models)
    with pytest.raises(ValueError, match="no model"):
        ibis.pick(models, "NOPE")


def test_ibis_tables_shaped_like_a_vendors(tmp_path):
    models = ibis.parse(REAL)
    d = ibis.driver(models["DRV_REAL"])
    # the transistor's side of the table, not the clamp diode below the rail
    # (0.5 ohm) that the points either side of 0 V would give
    assert d["r_out_ohms"] == pytest.approx(R_OUT, rel=0.2)
    assert d["r_pulldown_ohms"] == pytest.approx(d["r_pullup_ohms"])
    # a pull-up that barely conducts near its rail is not averaged in
    n = ibis.driver(models["DRV_NPU"])
    assert n["r_out_ohms"] == pytest.approx(d["r_out_ohms"]) and "differ widely" in n["note"]
    assert n["r_pullup_ohms"] > 1000 * n["r_pulldown_ohms"]
    # an input does not drive; its clamp tables carry the on-die termination
    with pytest.raises(ValueError, match="RX_ODT is of type Input"):
        ibis.driver(models["RX_ODT"])
    assert ibis.receiver(models["RX_ODT"])["r_term_ohms"] == pytest.approx(50.0, rel=1e-6)
    assert "r_term_ohms" not in ibis.receiver(models["RX_HIZ"])
    assert "r_term_ohms" not in ibis.receiver(models["DRV_REAL"])
    path = tmp_path / "real.ibs"
    path.write_text(REAL)
    t = terminations({"tx_ibis": str(path), "tx_model": "DRV_REAL", "rx_ibis": str(path),
                      "rx_model": "RX_ODT"}, {"rise": 30e-12, "amp": 0.8}, 50.0, 2)
    assert t["rl"] == pytest.approx(50.0) and t["rs"] == pytest.approx(R_OUT, rel=0.2)
    assert any("on-die termination 50.0 ohm" in n for n in t["notes"])
    t = terminations({"rx_ibis": str(path), "rx_model": "RX_ODT", "rx_r_ohms": "open"},
                     {"rise": 30e-12, "amp": 0.8}, 50.0, 2)
    assert t["rl"] is None
    with pytest.raises(ValueError, match="does not drive"):
        terminations({"tx_ibis": str(path), "tx_model": "RX_HIZ"},
                     {"rise": 30e-12, "amp": 0.8}, 50.0, 2)


def test_ibis_buffers_set_the_terminations(tmp_path):
    path = tmp_path / "part.ibs"
    path.write_text(TEXT)
    st = {"rise": 30e-12, "amp": 0.8}
    t = terminations({"tx_ibis": str(path), "tx_model": "DRV18", "rx_ibis": str(path),
                      "rx_model": "RX18"}, st, 50.0, 2)
    assert t["rs"] == pytest.approx(R_OUT) and t["cs"] == pytest.approx(0.6e-12)
    assert t["rl"] is None and t["cl"] == pytest.approx(0.4e-12) and not t["matched"]
    assert st["rise"] == pytest.approx(80e-12) and st["amp"] == pytest.approx(VCC / 2)
    st4 = {"rise": 30e-12, "amp": 0.8}
    terminations({"tx_ibis": str(path), "tx_model": "DRV18"}, st4, 50.0, 4)
    assert st4["amp"] == pytest.approx(VCC)
    # what the spec names wins over the buffer
    st = {"rise": 30e-12, "amp": 0.8}
    t = terminations({"tx_ibis": str(path), "tx_model": "DRV18", "rise_ps": 30,
                      "amplitude_mv": 800, "rx_r_ohms": "50", "tx_r_ohms": 33}, st, 50.0, 2)
    assert st == {"rise": 30e-12, "amp": 0.8} and t["rs"] == 33 and t["rl"] == 50
    assert terminations({}, {"rise": 1, "amp": 1}, 50.0, 2)["matched"]
    with pytest.raises(ValueError):
        terminations({"tx_r_ohms": 0}, {"rise": 1, "amp": 1}, 50.0, 2)


def test_spice_deck_carries_the_terminations():
    f = np.linspace(50e6, 10e9, 60)
    m = vectfit.macromodel(f, lossy_line(f), 5e-3)
    term = {"rs": 40.0, "cs": 0.6e-12, "rl": None, "cl": 0.4e-12}
    deck = spice_eye.deck(m, [0, 1, 1, 0], 100e-12, 30e-12, 0.9, 50.0, False, "ngspice",
                          1e-12, 1e-9, term)
    assert "Rs s n1 40" in deck and "Cs n1 0 6e-13" in deck
    assert "Rl n2 0 1e+12" in deck and "Cl n2 0 4e-13" in deck
    plain = spice_eye.deck(m, [0, 1], 100e-12, 30e-12, 0.9, 50.0, False, "ngspice", 1e-12, 1e-9)
    assert "Rs s n1 50" in plain and "Rl n2 0 50" in plain and "Cs" not in plain


# -- jobs and tools ------------------------------------------------------------------------

def _channel(tmp_path, ports=2):
    f = ch.uniform_band(25e9, 250)
    s = lossy_line(f, length=0.03)
    if ports == 4:
        big = np.zeros((len(f), 4, 4), dtype=complex)
        big[:, 0, 2] = big[:, 2, 0] = big[:, 1, 3] = big[:, 3, 1] = s[:, 1, 0]
        big[:, 0, 0] = big[:, 1, 1] = big[:, 2, 2] = big[:, 3, 3] = s[:, 0, 0]
        s = big
    path = write_touchstone(tmp_path / ("chan" + (".s4p" if ports == 4 else ".s2p")), list(f),
                            s.tolist(), 50.0)
    return str(path)


def test_convolution_eye_job(tmp_path):
    ts = _channel(tmp_path)
    r = sim.submit("channel-eye", "eda-agent", {"data_rate_gbps": 10, "repeats": 1},
                   {"touchstone": ts}, "eye", True)
    assert r["state"] == "done", r
    res = sim.refresh(r["job"])["runs"]["eda-agent"]["results"]
    m = {k: v["value"] for k, v in res["metrics"].items()}
    assert 0 < m["worst_case_eye_height_mv"] <= m["eye_height_mv"] < 800
    f = ch.uniform_band(25e9, 250)
    il = -20 * math.log10(abs(lossy_line(f, length=0.03)[ch.nyquist_index(f, 5e9), 1, 0]))
    assert m["insertion_loss_db_at_nyquist"] == pytest.approx(il, abs=1e-3)
    folder = sim._jobs.get(r["job"]).work_folder("eda-agent")
    assert (folder / "waveform.csv").is_file()


def test_open_receiver_doubles_the_eye(tmp_path):
    ts = _channel(tmp_path)
    out = []
    for opt in ({}, {"rx_r_ohms": "open"}):
        r = sim.submit("channel-eye", "eda-agent", {"data_rate_gbps": 5, "repeats": 1, **opt},
                       {"touchstone": ts}, "eye", True)
        out.append(sim.refresh(r["job"])["runs"]["eda-agent"]["results"]["metrics"]
                   ["main_cursor_mv"]["value"])
    # S11 is under 10 %, so the open end comes close to doubling
    assert out[1] == pytest.approx(2 * out[0], rel=0.15)


def test_a_sweep_too_coarse_for_the_bit_is_refused(tmp_path):
    # 250 points to 25 GHz is a 100 MHz step: 10 ns of response
    ts = _channel(tmp_path)

    def run(**spec):
        r = sim.submit("channel-eye", "eda-agent", {"repeats": 1, **spec}, {"touchstone": ts},
                       "eye", True)
        return r["state"], str(r.get("error") or "")
    state, why = run(data_rate_gbps=0.2)                # a 5 ns bit
    assert state == "failed" and "one bit lasts 5 ns" in why and "66.7 MHz or finer" in why
    # a 1 ohm driver into an open end rings for longer than the sweep covers
    state, why = run(data_rate_gbps=1, tx_r_ohms=1, rx_r_ohms="open")
    assert state == "failed" and "still ringing after" in why
    assert run(data_rate_gbps=1, tx_r_ohms=40, rx_r_ohms="open")[0] == "done"


def test_pair_eye_is_the_differential_one(tmp_path):
    ts2, ts4 = _channel(tmp_path), _channel(tmp_path, 4)
    vals = []
    for ts in (ts2, ts4):
        r = sim.submit("channel-eye", "eda-agent", {"data_rate_gbps": 10, "repeats": 1},
                       {"touchstone": ts}, "eye", True)
        vals.append(sim.refresh(r["job"])["runs"]["eda-agent"]["results"]["metrics"]
                    ["eye_height_mv"]["value"])
    # uncoupled lines: SDD21 = S21, so the same differential swing gives the same eye
    assert vals[1] == pytest.approx(vals[0], rel=1e-3)


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
def test_spice_and_convolution_agree_with_ibis_buffers(tmp_path):
    ts = _channel(tmp_path)
    ibs = tmp_path / "part.ibs"
    ibs.write_text(TEXT)
    spec = {"data_rate_gbps": 10, "repeats": 1, "samples_per_ui": 16, "tx_ibis": str(ibs),
            "tx_model": "DRV18", "rx_ibis": str(ibs), "rx_model": "RX18"}
    for eng in ("eda-agent", "spice"):
        r = sim.submit("channel-eye", eng, spec, {"touchstone": ts}, "eye", True)
    st = sim.refresh(r["job"])
    for eng in ("eda-agent", "spice"):
        assert st["runs"][eng]["state"] == "done", st["runs"][eng].get("error")
    sp = st["runs"]["spice"]["results"]
    checks = {v["check"]: v["pass"] for v in sp["verdicts"]}
    assert all(checks.values()), sp["verdicts"]
    folder = sim._jobs.get(r["job"]).work_folder("spice")
    for name in ("channel.sub", "channel.asy", "macromodel.json", "waveform.csv"):
        assert (folder / name).is_file(), name
    cmp = sim.compare(r["job"])
    row = next(x for x in cmp["metrics"] if x["metric"] == "eye_height_mv")
    assert row["agree"], row


def _call(m, name, args):
    out = asyncio.run(m.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def test_tools_eye_spice_model_and_ibis(tmp_path):
    from eda_agent.tools.si import register_si_tools
    m = FastMCP("t")
    register_si_tools(m)
    ts = _channel(tmp_path)
    r = _call(m, "si_channel_eye", {"data_rate_gbps": 10, "touchstone_file": ts,
                                    "engines": ["eda-agent"], "options": {"repeats": 1}})
    assert r["ok"] and r["runs"][0]["state"] == "done", r
    out = tmp_path / "model"
    r = _call(m, "si_spice_model", {"touchstone_file": ts, "out_folder": str(out),
                                    "name": "link"})
    assert r["ok"] and r["passivity"]["after"] <= 1.0 and r["ports"] == ["port1", "port2", "ref"]
    assert (out / "link.sub").read_text().startswith("* ")
    assert "SYMATTR ModelFile link.sub" in (out / "link.asy").read_text()
    assert _call(m, "si_spice_model", {"touchstone_file": ts, "name": "1 bad"})["ok"] is False
    ibs = tmp_path / "part.ibs"
    ibs.write_text(TEXT)
    r = _call(m, "si_ibis_models", {"ibis_file": str(ibs)})
    assert r["ok"]
    by = {x["name"]: x for x in r["models"]}
    assert by["DRV18"]["driver"]["r_out_ohms"] == pytest.approx(R_OUT)
    assert by["RX18"]["model_type"] == "Input"


# -- the single edge: overshoot, ringback, a reversal on the way up ------------------

def test_edge_figures_of_known_steps():
    t = np.linspace(0, 10e-9, 2001)
    w, a = 2 * math.pi * 1e9, 0.4e9
    ring = 1 - np.exp(-a * t) * np.cos(w * t)          # rings about 1: peaks at half periods
    e = eye.edge_figures(t, 3.0 * ring)
    assert e["final"] == pytest.approx(3.0, rel=0.01)
    assert e["overshoot"] == pytest.approx(math.exp(-a * 0.5e-9), abs=0.01)     # first peak
    assert e["ringback"] == pytest.approx(math.exp(-a * 1.0e-9), abs=0.01)      # the dip after
    assert e["reversal"] == 0
    # within 5 % once exp(-a t) < 0.05, counted from the half level
    assert e["settle"] == pytest.approx(math.log(20) / a, rel=0.1)
    # a falling edge is the same picture upside down
    assert eye.edge_figures(t, -3.0 * ring)["overshoot"] == pytest.approx(e["overshoot"])
    # an overdamped edge has none of it
    slow = eye.edge_figures(t, 1 - np.exp(-t / 0.5e-9))
    assert slow["overshoot"] < 1e-3 and slow["ringback"] == 0 and slow["reversal"] == 0
    assert slow["settle"] == pytest.approx(0.5e-9 * (math.log(20) - math.log(2)), rel=0.05)
    # the level is read at the very end: an edge that is slow to arrive has no overshoot
    late = eye.edge_figures(t, 1 - np.exp(-t / 2e-9))
    assert late["final"] == pytest.approx(1 - math.exp(-4.875), abs=2e-3) and late["overshoot"] < 2e-3
    # a shelf that dips on the way up: half way, back by 0.15, then on to the level
    shelf = np.interp(t, [0, 1e-9, 2e-9, 3e-9, 4e-9, 10e-9], [0, 0.5, 0.35, 0.5, 1.0, 1.0])
    e = eye.edge_figures(t, shelf)
    assert e["reversal"] == pytest.approx(0.15, abs=0.005) and e["overshoot"] < 1e-6
    # a dip after the level is reached is ringback's business, not a reversal
    late = np.interp(t, [0, 1e-9, 2e-9, 3e-9, 10e-9], [0, 1.2, 0.8, 1.0, 1.0])
    e = eye.edge_figures(t, late)
    assert e["reversal"] == 0 and e["overshoot"] == pytest.approx(0.2, abs=0.005)
    assert e["ringback"] == pytest.approx(0.2, abs=0.005)
    # nothing to measure
    assert eye.edge_figures(t, np.zeros_like(t))["overshoot"] == 0
    assert eye.edge_figures(t[:2], np.ones(2))["final"] == 0


def _edge_job(ts, **opt):
    r = sim.submit("channel-eye", "eda-agent", {"data_rate_gbps": 1, "repeats": 1, **opt},
                   {"touchstone": ts}, "edge", True)
    assert r["state"] in ("done", "failed"), r
    return sim.refresh(r["job"])["runs"]["eda-agent"]


def test_a_strong_driver_into_an_open_end_overshoots_and_a_matched_one_does_not(tmp_path):
    f = ch.uniform_band(20e9, 400)
    s = lossy_line(f, length=0.1, z0=50.0)             # 4 inches, about 0.67 ns one way
    ts = str(write_touchstone(tmp_path / "long.s2p", list(f), s.tolist(), 50.0))
    res = _edge_job(ts)["results"]
    m = {k: v["value"] for k, v in res["metrics"].items()}
    assert m["overshoot_pct"] < 0.5 and m["ringback_pct"] < 0.5 and m["edge_reversal_pct"] < 0.5
    assert not any(v["check"] in ("overshoot", "ringback") for v in res["verdicts"])
    # 15 ohm into an open end: the first wave is 2 x 50 / 65 of the level, 54 % over,
    # and it comes back (15 - 50) / (15 + 50) of that later
    res = _edge_job(ts, tx_r_ohms=15, rx_r_ohms="open", rise_ps=100, overshoot_max_pct=20,
                    ringback_max_pct=40)["results"]
    m = {k: v["value"] for k, v in res["metrics"].items()}
    assert m["overshoot_pct"] == pytest.approx(100 * (2 * 50 / 65 - 1), abs=4)
    assert m["ringback_pct"] == pytest.approx(100 * (1 - (100 / 65) * (1 - 35 / 65)), abs=4)
    assert m["settling_ps"] > 2 * 1340
    v = {x["check"]: x for x in res["verdicts"]}
    assert not v["overshoot"]["pass"] and v["ringback"]["pass"]
    assert v["overshoot"]["detail"].startswith(
        f"{m['overshoot_pct']:.1f} % of the settled level, 20 % allowed; ")
    # a weak driver climbs in steps and never overshoots
    res = _edge_job(ts, tx_r_ohms=75, rx_r_ohms="open", rise_ps=100, overshoot_max_pct=5)
    m = {k: v["value"] for k, v in res["results"]["metrics"].items()}
    assert m["overshoot_pct"] < 2
    assert next(x for x in res["results"]["verdicts"] if x["check"] == "overshoot")["pass"]
    bad = _edge_job(ts, overshoot_max_pct=-1)
    assert bad["state"] == "failed" and "overshoot_max_pct is 0 or more" in json.dumps(bad)


def test_a_reflection_inside_the_edge_is_told(tmp_path):
    # a 20 ohm stretch in a 50 ohm line, a weak driver, an open end: the edge climbs in
    # steps, and the wave the open end sent back returns inverted from the stretch
    f = ch.uniform_band(10e9, 800)
    c = ch.Channel(f, 1)
    for z, length in ((50.0, 0.03), (20.0, 0.03), (50.0, 0.03)):
        c.add(ch.line_block(_rlgc(f, z0=z, loss=False), length))
    ts = str(write_touchstone(tmp_path / "step.s2p", list(f), c.s(50.0).tolist(), 50.0))
    opt = {"data_rate_gbps": 0.2, "tx_r_ohms": 100, "rx_r_ohms": "open", "rise_ps": 100}
    res = _edge_job(ts, **opt, edge_reversal_max_pct=5)["results"]
    rev = res["metrics"]["edge_reversal_pct"]["value"]
    assert 15 < rev < 30 and res["metrics"]["overshoot_pct"]["value"] < 2
    assert any(f"turns back by {rev:.1f} %" in n for n in res["notes"])
    v = next(x for x in res["verdicts"] if x["check"] == "edge reversal")
    assert not v["pass"] and v["detail"] == f"{rev:.1f} % of the settled level, 5 % allowed"
    # the same line between matched ends climbs once
    res = _edge_job(ts, data_rate_gbps=0.2, rise_ps=100, edge_reversal_max_pct=5)["results"]
    assert res["metrics"]["edge_reversal_pct"]["value"] < 2
    assert not any("turns back" in n for n in res["notes"])
    assert next(x for x in res["verdicts"] if x["check"] == "edge reversal")["pass"]


def test_the_step_settles_to_the_dc_of_the_s_matrix():
    from eda_agent.si import eye_jobs
    f = ch.uniform_band(20e9, 400)
    s = lossy_line(f, length=0.1, z0=50.0, loss=False)
    s0 = eye.dc_matrix(s)                               # a lossless line at DC: a through
    assert s0 == pytest.approx(np.array([[0.0, 1.0], [1.0, 0.0]]), abs=2e-3)
    assert eye.dc_matrix(s[:1]) == pytest.approx(np.array([[0.0, 1.0], [1.0, 0.0]]), abs=1e-9)
    # a long line swept coarsely: the first point is nowhere near DC, the magnitude is
    f2 = ch.uniform_band(30e9, 200)
    far = lossy_line(f2, length=0.5, z0=50.0, loss=False)
    assert far[0, 1, 0].real < -0.9
    assert eye.dc_matrix(far) == pytest.approx(np.array([[0.0, 1.0], [1.0, 0.0]]), abs=2e-3)
    lossy = eye.dc_matrix(lossy_line(f2, length=0.5, z0=50.0))
    assert 0.96 < lossy[1, 0] < 1.0 and lossy[0, 0] == pytest.approx(1 - lossy[1, 0])
    # a pair: each side to its own far end, nothing across
    four = np.zeros((2, 4, 4), dtype=complex)
    four[:, 0, 2] = four[:, 2, 0] = four[:, 1, 3] = four[:, 3, 1] = [0.95j, -0.93]
    four[:, 0, 1] = four[:, 1, 0] = [0.02, 0.04]
    for k in range(4):
        four[:, k, k] = [0.3, 0.25j]                    # a reflection in the band says nothing of DC
    d4 = eye.dc_matrix(four)
    assert d4[2, 0] == pytest.approx(0.97) and d4[1, 0] == 0 and d4[0, 0] == pytest.approx(0.03)
    assert s[0, 1, 0].real < 0.98                       # the first point is not DC
    # matched it settles to 1, into an open end to 2, whatever the driver: also a weak one,
    # whose transfer is a low-pass with its corner near the frequency step
    for term, level in (({"rs": 50.0, "cs": 0.0, "rl": 50.0, "cl": 0.0, "matched": True}, 1.0),
                        ({"rs": 15.0, "cs": 0.0, "rl": None, "cl": 0.0, "matched": False}, 2.0),
                        ({"rs": 215.0, "cs": 0.0, "rl": None, "cl": 0.0, "matched": False}, 2.0),
                        ({"rs": 25.0, "cs": 0.0, "rl": 75.0, "cl": 1e-12, "matched": False}, 1.5)):
        t, v = eye_jobs._step(f, s, 50.0, term, 100e-12)
        assert v[-20:].mean() == pytest.approx(level, rel=0.01), term
    # the first point taken as DC leaves a slope on a pure delay
    t, v = eye.step_response(f, s[:, 1, 0], 100e-12)
    late = v[t > 2e-9]
    assert late.max() - late.min() > 0.015
    t, v = eye_jobs._step(f, s, 50.0, {"rs": 50.0, "cs": 0.0, "rl": 50.0, "cl": 0.0,
                                       "matched": True}, 100e-12)
    late = v[t > 2e-9]
    assert late.max() - late.min() < 0.01


def test_a_failed_overshoot_names_the_series_resistor_that_cures_it(tmp_path):
    from eda_agent.si import eye_jobs
    f = ch.uniform_band(20e9, 400)
    s = lossy_line(f, length=0.1, z0=50.0, loss=False)
    ts = str(write_touchstone(tmp_path / "long.s2p", list(f), s.tolist(), 50.0))
    opt = {"tx_r_ohms": 15, "rx_r_ohms": "open", "rise_ps": 100}
    res = _edge_job(ts, **opt, overshoot_max_pct=10)["results"]
    r = res["metrics"]["series_r_ohms"]["value"]
    # into an open end the first wave is 2 Z / (R + Z) of the level: 10 % over at R = 40.9
    assert r == pytest.approx(2 * 50 / 1.1 - 50 - 15, abs=3) and r == int(r)
    v = next(x for x in res["verdicts"] if x["check"] == "overshoot")
    assert v["detail"].endswith(f"; {r:.0f} ohm in series at the driver brings it within 10 %")
    # with it in, the edge passes; one ohm and a half less, it does not
    ok = _edge_job(ts, **{**opt, "tx_r_ohms": 15 + r}, overshoot_max_pct=10)["results"]
    assert next(x for x in ok["verdicts"] if x["check"] == "overshoot")["pass"]
    assert "series_r_ohms" not in ok["metrics"]
    assert "in series" not in next(x for x in ok["verdicts"] if x["check"] == "overshoot")["detail"]
    less = _edge_job(ts, **{**opt, "tx_r_ohms": 15 + r - 1.5}, overshoot_max_pct=10)["results"]
    assert not next(x for x in less["verdicts"] if x["check"] == "overshoot")["pass"]
    # a tighter limit asks for more; no limit, no resistor
    tight = _edge_job(ts, **opt, overshoot_max_pct=3)["results"]["metrics"]["series_r_ohms"]["value"]
    assert tight > r
    assert "series_r_ohms" not in _edge_job(ts, **opt)["results"]["metrics"]
    # overshoot that the line makes itself is not the driver's to cure: a short
    # high-impedance stretch before an open end rings whatever the source
    c = ch.Channel(f, 1)
    for z, length in ((50.0, 0.05), (150.0, 0.012)):
        c.add(ch.line_block(_rlgc(f, z0=z, loss=False), length))
    term = {"rs": 50.0, "cs": 0.0, "rl": None, "cl": 2e-12}
    sp = c.s(50.0)
    got = eye_jobs.series_resistor(f, sp, 50.0, term, 100e-12, 0.0005)
    assert got is None
    ts2 = str(write_touchstone(tmp_path / "ring.s2p", list(f), sp.tolist(), 50.0))
    res = _edge_job(ts2, rx_r_ohms="open", rx_c_pf=2, rise_ps=100, overshoot_max_pct=0.05)["results"]
    v = next(x for x in res["verdicts"] if x["check"] == "overshoot")
    assert not v["pass"] and v["detail"].endswith("a series resistor at the driver does not bring it within")
    assert "series_r_ohms" not in res["metrics"]


def test_a_pairs_series_resistor_is_each_sides(tmp_path):
    ts = _channel(tmp_path, ports=4)
    res = _edge_job(ts, data_rate_gbps=2, tx_r_ohms=10, rx_r_ohms="open", rise_ps=60,
                    overshoot_max_pct=10)["results"]
    v = next(x for x in res["verdicts"] if x["check"] == "overshoot")
    r = res["metrics"]["series_r_ohms"]["value"]
    assert not v["pass"] and f"{r:.0f} ohm in series at the driver (each side) brings" in v["detail"]


def test_the_eye_takes_its_dc_level_from_the_s_matrix(tmp_path):
    """The pulse is a step less the same step a bit later. With the first
    frequency point taken as DC the step slopes, every bit leaves a small
    offset behind, and the offsets of all the bits add up as ISI that is
    not there; behind a weak driver the slope was steep enough to be
    refused as a channel still ringing."""
    from eda_agent.si import eye_jobs
    f = ch.uniform_band(20e9, 400)
    s = lossy_line(f, length=0.1, z0=50.0, loss=False)          # a pure delay
    term = {"rs": 50.0, "cs": 0.0, "rl": 50.0, "cl": 0.0, "matched": True}
    h, _ = eye_jobs.channel_transfer(f, s, 50.0, term)
    assert eye_jobs.dc_level(s, 50.0, term) == pytest.approx(1.0, abs=1e-3)
    good = eye.worst_case_eye(eye.pulse_response(f, h, 1e-9, 100e-12, 32, dc=1.0), 0.8)
    old = eye.worst_case_eye(eye.pulse_response(f, h, 1e-9, 100e-12, 32), 0.8)
    assert good["isi_sum"] < 0.004 and old["isi_sum"] > 2 * good["isi_sum"]
    assert good["eye_height"] == pytest.approx(0.8, abs=0.006)
    # the job: a matched lossless line gives the full swing
    ts = str(write_touchstone(tmp_path / "delay.s2p", list(f), s.tolist(), 50.0))
    res = _edge_job(ts)["results"]
    m = {k: v["value"] for k, v in res["metrics"].items()}
    assert m["worst_case_eye_height_mv"] == pytest.approx(800, abs=6) and m["isi_mv"] < 4
    # 150 ohm into an open end climbs in steps and settles inside the window: it runs,
    # and settles to twice the incident wave
    run = _edge_job(ts, tx_r_ohms=150, rx_r_ohms="open", rise_ps=100)
    assert run["state"] == "done", run
    weak = {"rs": 150.0, "cs": 0.0, "rl": None, "cl": 0.0, "matched": False}
    assert eye_jobs.dc_level(s, 50.0, weak) == pytest.approx(2.0, rel=0.005)
    hw, _ = eye_jobs.channel_transfer(f, s, 50.0, weak)
    with pytest.raises(ValueError, match="still ringing"):
        eye.pulse_response(f, hw, 1e-9, 100e-12, 32)             # the first point as DC
    eye.pulse_response(f, hw, 1e-9, 100e-12, 32, dc=2.0)


# -- equalisation: transmitter FFE, receiver CTLE and DFE ----------------------------

def _tri_pulse(cursors, spui=16, ui=1e-10, lead=2):
    """A pulse of triangles half an interval wide each side, their tops ``cursors`` at the
    sampling points: the first is ``lead`` intervals in."""
    n = (len(cursors) + lead + 2) * spui
    p = np.zeros(n)
    x = np.arange(n)
    for k, c in enumerate(cursors):
        centre = (lead + k) * spui
        p += c * np.clip(1 - np.abs(x - centre) / (spui // 2), 0, None)
    return eye.Pulse(np.arange(n) * ui / spui, p, ui, spui)


def test_ctle_gains_its_boost_from_dc_to_the_poles():
    f = np.array([0.0, 1e7, 5e9, 1e10, 4e10, 1e12])
    g = eye.ctle(f, 6.0, 5e9)
    assert g[0] == pytest.approx(10 ** (-6 / 20)) and g[0].imag == 0
    assert abs(g[1]) == pytest.approx(abs(g[0]), rel=1e-3)
    # by hand at the first pole: g (1 + j / g) / ((1 + j) (1 + j / 4))
    k = 10 ** (-6 / 20)
    assert g[2] == pytest.approx(k * (1 + 1j / k) / ((1 + 1j) * (1 + 0.25j)))
    assert 0.8 < abs(g[3]) < 1.0                        # near 1 between the poles
    assert abs(g[5]) < 0.03                             # and it rolls off above the second
    assert abs(eye.ctle(f, 12.0, 5e9)[0]) == pytest.approx(10 ** (-12 / 20))
    assert abs(eye.ctle(np.array([8e9]), 6.0, 8e9)[0]) == pytest.approx(abs(g[2]))


def test_ffe_adds_the_pulse_tap_by_tap_and_keeps_the_peak_swing():
    p = _tri_pulse([1.0], spui=16)
    out = eye.ffe(p, [3.0, -1.0])                       # scaled to 0.75, -0.25
    c = p.center_index
    assert out.p[c] == pytest.approx(0.75) and out.p[c + 16] == pytest.approx(-0.25)
    assert len(out.p) == len(p.p) + 16 and out.spui == 16 and out.ui == p.ui
    assert out.center_index == c
    # a pre-cursor tap comes first: the main cursor is then one interval later
    out = eye.ffe(p, [-0.1, 0.7, -0.2])
    assert [out.p[c + 16 * k] for k in range(3)] == pytest.approx([-0.1, 0.7, -0.2])
    assert out.center_index == c + 16
    assert eye.ffe(p, [5.0]).p == pytest.approx(p.p)    # one tap: the pulse itself
    for bad in ([], [0.0, 0.0]):
        with pytest.raises(ValueError, match="tx_ffe"):
            eye.ffe(p, bad)
    # de-emphasis on a flat channel: full swing at a transition, 0.75 - 0.25 after it
    flat = eye.Pulse(np.arange(64) * 1e-10 / 16, np.r_[np.ones(16), np.zeros(48)], 1e-10, 16)
    m = eye.simulated_eye(eye.ffe(flat, [0.75, -0.25]), 0.8)
    assert m["eye_height"] == pytest.approx(0.5 * 0.8)
    # the post-cursor of a channel is cancelled by the tap that mirrors it
    chan = _tri_pulse([1.0, 0.3])
    eq = eye.ffe(chan, [1.0, -0.3])
    c = eq.center_index
    assert eq.p[c + 16] == pytest.approx(0.0, abs=1e-12)
    assert eye.worst_case_eye(eq, 1.0)["isi_sum"] < eye.worst_case_eye(chan, 1.0)["isi_sum"]


def test_dfe_takes_off_the_post_cursors_up_to_its_limit():
    p = _tri_pulse([0.2, 1.0, 0.4, -0.1, 0.05], lead=1)
    c = p.center_index
    assert p.p[c] == pytest.approx(1.0)
    out, taps = eye.dfe(p, 2)
    assert taps == pytest.approx([0.4, -0.1])
    assert out.p[c + 16] == pytest.approx(0.0, abs=1e-12)
    assert out.p[c + 32] == pytest.approx(0.0, abs=1e-12)
    assert out.p[c + 48] == pytest.approx(0.05)         # beyond its taps
    assert out.p[c] == pytest.approx(1.0) and out.p[c - 16] == pytest.approx(0.2)   # nor before
    # each tap holds for its whole interval, as the decided bit does
    assert out.p[c + 16 - 8] == pytest.approx(-0.4) and out.p[c + 16 + 7] == pytest.approx(
        p.p[c + 16 + 7] - 0.4)
    assert out.p[c + 16 - 9] == pytest.approx(p.p[c + 16 - 9])
    before, after = eye.worst_case_eye(p, 2.0), eye.worst_case_eye(out, 2.0)
    assert before["isi_sum"] == pytest.approx(0.2 + 0.4 + 0.1 + 0.05)
    assert after["isi_sum"] == pytest.approx(0.2 + 0.05)
    assert after["eye_height"] == pytest.approx(2 * (1 - 0.25))
    # a tap is held to its limit, and taps past the pulse's end are not made up
    out, taps = eye.dfe(p, 1, limit=0.25)
    assert taps == [0.25] and out.p[c + 16] == pytest.approx(0.15)
    out, taps = eye.dfe(p, 20)
    assert len(taps) < 20 and taps[:3] == pytest.approx([0.4, -0.1, 0.05])
    assert eye.dfe(p, 0)[1] == [] and eye.dfe(p, 0)[0].p == pytest.approx(p.p)


def _long_channel(tmp_path):
    f = ch.uniform_band(40e9, 800)
    s = lossy_line(f, length=0.8, z0=50.0)             # 32 inches: about 15 dB at 5 GHz
    return str(write_touchstone(tmp_path / "long.s2p", list(f), s.tolist(), 50.0))


def test_equalisation_opens_the_eye_of_a_long_channel(tmp_path):
    ts = _long_channel(tmp_path)

    def run(**opt):
        res = _edge_job(ts, data_rate_gbps=10, **opt)["results"]
        return {k: v["value"] for k, v in res["metrics"].items()}, res
    plain, res0 = run()
    # some pattern closes it: more ISI than main cursor
    assert plain["worst_case_eye_height_mv"] == 0 and plain["isi_mv"] > plain["main_cursor_mv"]
    assert "dfe_tap1_mv" not in plain and not any("equalisation" in n for n in res0["notes"])
    ffe, res = run(tx_ffe=[-0.1, 0.7, -0.2])
    assert ffe["worst_case_eye_height_mv"] > 80 and ffe["eye_height_mv"] > 3 * plain["eye_height_mv"]
    assert ffe["isi_mv"] < 0.4 * plain["isi_mv"] and ffe["main_cursor_mv"] < plain["main_cursor_mv"]
    assert any("transmitter FFE taps -0.100, +0.700, -0.200" in n for n in res["notes"])
    ctle, res = run(ctle_db=6)
    assert ctle["worst_case_eye_height_mv"] > 10 and ctle["eye_height_mv"] > 1.5 * plain["eye_height_mv"]
    assert ctle["eye_width_ui"] > plain["eye_width_ui"]
    assert any("receiver CTLE: 6 dB of boost over DC, first pole at 5 GHz" in n
               for n in res["notes"])
    assert run(ctle_db=6, ctle_peak_ghz=2)[0]["eye_height_mv"] != ctle["eye_height_mv"]
    dfe, res = run(dfe_taps=4)
    assert dfe["worst_case_eye_height_mv"] > 80
    assert dfe["main_cursor_mv"] == plain["main_cursor_mv"]           # it adds no loss
    assert dfe["dfe_tap1_mv"] == pytest.approx(plain["isi_mv"] - run(dfe_taps=1)[0]["isi_mv"],
                                               abs=0.5)
    assert any(n.startswith(f"receiver DFE taps (mV): +{dfe['dfe_tap1_mv']:.1f}, ")
               for n in res["notes"])
    held = run(dfe_taps=4, dfe_max=0.05)[0]
    assert held["dfe_tap1_mv"] == pytest.approx(0.05 * plain["main_cursor_mv"], abs=0.3)
    assert plain["eye_height_mv"] < held["eye_height_mv"] < dfe["eye_height_mv"]
    # the channel's own figures stay: loss and the edge are not the equaliser's
    for got in (ffe, ctle, dfe):
        assert got["insertion_loss_db_at_nyquist"] == plain["insertion_loss_db_at_nyquist"]
        assert got["overshoot_pct"] == plain["overshoot_pct"]
    assert any("after equalisation" in n for n in res["notes"])


@pytest.mark.parametrize("opt, word", [
    ({"tx_ffe": [0, 0]}, "tx_ffe needs one tap"), ({"tx_ffe": ["a"]}, "tx_ffe is a list"),
    ({"tx_ffe": [1] * 9}, "8 taps at most"), ({"ctle_db": -1}, "ctle_db is 0 to 30"),
    ({"ctle_db": 31}, "ctle_db is 0 to 30"), ({"ctle_db": 3, "ctle_peak_ghz": 0}, "ctle_peak_ghz"),
    ({"dfe_taps": 1.5}, "dfe_taps is a whole number"), ({"dfe_taps": 33}, "dfe_taps"),
    ({"dfe_taps": 2, "dfe_max": 0}, "dfe_max"), ({"dfe_taps": 2, "dfe_max": 1.5}, "dfe_max"),
    ({"tx_ffe": "best"}, "tx_ffe is a list of tap weights"),
    ({"tx_ffe": "auto", "tx_ffe_pre": 4}, "tx_ffe_pre is a whole number, 0 to 3"),
    ({"tx_ffe": "auto", "tx_ffe_post": 1.5}, "tx_ffe_post 0 to 4"),
    ({"tx_ffe": "auto", "tx_ffe_pre": 0, "tx_ffe_post": 0}, "needs a tap beside the main one"),
    ({"ctle_db": "most"}, "ctle_db"), ({"ctle_db": "auto", "ctle_max_db": 0}, "ctle_max_db"),
    ({"ctle_db": "auto", "ctle_max_db": 31}, "ctle_max_db is a whole number, 1 to 30")])
def test_equaliser_settings_are_checked(opt, word):
    from eda_agent.si import eye_jobs
    with pytest.raises(ValueError, match=word):
        eye_jobs.equaliser({"data_rate_gbps": 10, **opt}, {"rate": 10e9})


def test_equaliser_defaults_and_the_spice_engine_refuses_it(tmp_path):
    from eda_agent.si import eye_jobs
    eq = eye_jobs.equaliser({}, {"rate": 8e9})
    assert eq == {"ffe": [], "ctle_db": 0.0, "ctle_peak": 4e9, "dfe_taps": 0, "dfe_max": 0.5,
                  "auto_ffe": None, "auto_ctle": None}
    auto = eye_jobs.equaliser({"tx_ffe": "Auto", "ctle_db": "auto"}, {"rate": 8e9})
    assert auto == {**eq, "auto_ffe": (1, 1), "auto_ctle": 12}
    auto = eye_jobs.equaliser({"tx_ffe": "auto", "tx_ffe_pre": 0, "tx_ffe_post": 3,
                               "ctle_db": "auto", "ctle_max_db": 6}, {"rate": 8e9})
    assert auto["auto_ffe"] == (0, 3) and auto["auto_ctle"] == 6
    # the counts and the ceiling mean nothing without "auto"
    assert eye_jobs.equaliser({"tx_ffe_post": 3, "ctle_max_db": 6}, {"rate": 8e9}) == eq
    assert eye_jobs.equaliser({"tx_ffe": [], "dfe_taps": 3, "ctle_peak_ghz": 6},
                              {"rate": 8e9}) == {**eq, "dfe_taps": 3, "ctle_peak": 6e9}
    ts = _channel(tmp_path)
    for opt in ({"tx_ffe": [0.8, -0.2]}, {"ctle_db": 3}, {"dfe_taps": 1}, {"tx_ffe": "auto"},
                {"ctle_db": "auto"}):
        with pytest.raises(ValueError, match="eda-agent engine only"):
            eye_jobs.run_spice({"data_rate_gbps": 5, **opt}, ts, tmp_path / "s")


def _margin(p, taps, dfe_taps=0):
    q = eye.ffe(p, taps)
    if dfe_taps:
        q = eye.dfe(q, dfe_taps, 1.0)[0]
    return eye.worst_case_eye(q, 2.0)["margin"]


def test_best_ffe_is_the_optimum_of_the_worst_case_eye():
    clean = _tri_pulse([1.0])
    assert eye.best_ffe(clean, 0, 0) == [1.0]
    assert eye.best_ffe(clean, 1, 2) == [0.0, 1.0, 0.0, 0.0]
    # one post-cursor: a tap that cancels it costs the main cursor as much as it gains,
    # and between equal eyes the least equalisation is taken
    assert eye.best_ffe(_tri_pulse([1.0, 0.3]), 0, 1) == [1.0, 0.0]
    p = _tri_pulse([0.2, 1.0, 0.4, 0.1], lead=3)
    taps = eye.best_ffe(p, 1, 1)
    assert sum(abs(x) for x in taps) == pytest.approx(1.0, abs=2e-4)
    assert taps[1] > 0.6 and taps[0] < -0.1 and taps[2] < -0.1
    best = _margin(p, taps)
    assert best > _margin(p, [1.0]) + 0.3 and best == pytest.approx(0.931, abs=0.002)
    # no other taps do better: a grid of them, and steps around the answer
    rng = np.random.default_rng(3)
    for a in np.arange(-0.4, 0.41, 0.05):
        for b in np.arange(-0.4, 0.41, 0.05):
            assert _margin(p, [a, 1 - abs(a) - abs(b), b]) <= best + 1e-3, (a, b)
    for _ in range(40):
        near = np.array(taps) + rng.normal(0, 0.02, 3)
        assert _margin(p, list(near)) <= best + 1e-3
    # more taps never do worse
    assert _margin(p, eye.best_ffe(p, 1, 2)) >= best - 1e-4
    assert _margin(p, eye.best_ffe(p, 2, 2)) >= _margin(p, eye.best_ffe(p, 1, 2)) - 1e-4
    # what a DFE takes off is left to it: the post-cursor tap is not spent on it
    with_dfe = eye.best_ffe(p, 1, 1, dfe_taps=2)
    assert with_dfe[2] == 0.0
    assert _margin(p, with_dfe, 2) >= _margin(p, taps, 2) - 1e-4
    for a in np.arange(-0.4, 0.41, 0.05):
        assert _margin(p, [a, 1 - abs(a), 0.0], 2) <= _margin(p, with_dfe, 2) + 1e-3


@pytest.mark.parametrize("rate", [5e9, 2e9])
def test_best_ffe_works_where_the_eye_is_tallest(rate):
    # reflections put the tallest eye a few samples off the pulse's middle; taps worked
    # out at the middle would leave a smaller eye than none
    f = ch.uniform_band(10e9, 1000)
    s = np.asarray(lossy_line(f, length=0.0765, z0=85.0))
    p = eye.pulse_response(f, s[:, 1, 0], 1 / rate, 0.3 / rate, 32)
    wc = eye.worst_case_eye(p, 1.0)
    c = wc["sample_index"]
    assert c != p.center_index
    # it is the sample the worst-case eye is read at: main cursor less all the others
    others = np.sum(np.abs(p.p[c % 32::32])) - abs(p.p[c])
    assert wc["margin"] == pytest.approx(p.p[c] - others, rel=1e-12)
    taps = eye.best_ffe(p, 1, 1)
    assert taps[1] < 0.995 and _margin(p, taps) > _margin(p, [1.0]) + 0.002


def test_the_eye_job_chooses_its_equaliser(tmp_path):
    ts = _long_channel(tmp_path)

    def run(**opt):
        res = _edge_job(ts, data_rate_gbps=10, **opt)["results"]
        return {k: v["value"] for k, v in res["metrics"].items()}, res
    hand = run(tx_ffe=[-0.1, 0.7, -0.2])[0]
    assert "ffe_main_tap" not in hand and "ctle_db" not in hand
    auto, res = run(tx_ffe="auto")
    assert auto["worst_case_eye_height_mv"] > hand["worst_case_eye_height_mv"] + 20
    assert 0.5 < auto["ffe_main_tap"] < 0.7
    note = [n for n in res["notes"] if n.startswith("transmitter FFE taps")][0]
    assert note.endswith("(magnitudes add up to 1) (chosen for the largest worst-case eye)")
    taps = [float(x) for x in note.split("taps ")[1].split(" (")[0].split(", ")]
    assert len(taps) == 3 and max(taps) == pytest.approx(auto["ffe_main_tap"], abs=1e-3)
    # the taps it names, given back by hand, are the same eye; taps beside them are worse
    again = run(tx_ffe=taps)[0]
    assert again["worst_case_eye_height_mv"] == pytest.approx(auto["worst_case_eye_height_mv"],
                                                              abs=0.5)
    for d in (-0.04, 0.04):
        worse = run(tx_ffe=[taps[0] + d, taps[1], taps[2] - d])[0]
        assert worse["worst_case_eye_height_mv"] < auto["worst_case_eye_height_mv"]
    # more taps are counted when asked for
    more, res = run(tx_ffe="auto", tx_ffe_pre=2, tx_ffe_post=3)
    assert more["worst_case_eye_height_mv"] >= auto["worst_case_eye_height_mv"] - 0.5
    assert [n for n in res["notes"] if n.startswith("transmitter FFE")][0].count(",") == 5
    # the CTLE: the best whole dB, which each one tried by hand confirms
    ctle, res = run(ctle_db="auto")
    each = {b: run(ctle_db=b)[0]["worst_case_eye_height_mv"] for b in range(0, 13)}
    assert ctle["ctle_db"] == max(each, key=each.get) and ctle["ctle_db"] > 6
    assert ctle["worst_case_eye_height_mv"] == each[int(ctle["ctle_db"])]
    assert any(f"receiver CTLE: {ctle['ctle_db']:g} dB of boost" in n and n.endswith(
        "(chosen for the largest worst-case eye)") for n in res["notes"])
    low = run(ctle_db="auto", ctle_max_db=4)[0]
    assert low["ctle_db"] == 4 and low["worst_case_eye_height_mv"] == each[4]
    # with a DFE the taps are worked out for what it leaves: the post-cursor tap is not
    # spent, and the eye beats the taps chosen without it
    with_dfe, res = run(tx_ffe="auto", dfe_taps=4)
    note = [n for n in res["notes"] if n.startswith("transmitter FFE taps")][0]
    assert note.split("taps ")[1].split(" (")[0].endswith(", +0.000")
    assert with_dfe["worst_case_eye_height_mv"] > \
        run(tx_ffe=taps, dfe_taps=4)[0]["worst_case_eye_height_mv"] + 5
    # both, with a DFE: no worse than either alone
    both = run(tx_ffe="auto", ctle_db="auto", dfe_taps=4)[0]
    assert both["worst_case_eye_height_mv"] >= max(
        run(tx_ffe="auto", dfe_taps=4)[0]["worst_case_eye_height_mv"],
        run(ctle_db="auto", dfe_taps=4)[0]["worst_case_eye_height_mv"]) - 0.01


@pytest.mark.parametrize("rate", [10, 1])
def test_chosen_taps_never_leave_a_smaller_eye_than_none(tmp_path, rate):
    # an 85 ohm line between 50 ohm ends: the ISI is reflections, which taps one bit
    # apart seldom meet, and the eye's best sampling point is not the pulse's middle
    f = ch.uniform_band(10e9, 1000)
    s = lossy_line(f, length=0.0765, z0=85.0)
    ts = str(write_touchstone(tmp_path / "refl.s2p", list(f), s.tolist(), 50.0))

    def worst(**opt):
        res = _edge_job(ts, data_rate_gbps=rate, **opt)["results"]
        return res["metrics"]["worst_case_eye_height_mv"]["value"]
    plain = worst()
    for more in ({}, {"tx_ffe_pre": 2, "tx_ffe_post": 3}, {"ctle_db": "auto"}):
        assert worst(tx_ffe="auto", **more) >= plain, more
    assert worst(tx_ffe="auto", dfe_taps=4) >= worst(dfe_taps=4)
    if rate == 1:
        assert worst(tx_ffe="auto") > plain + 5         # here a post-cursor tap does help


def test_a_clean_channel_is_given_no_equaliser(tmp_path):
    f = ch.uniform_band(20e9, 400)
    s = lossy_line(f, length=0.02, z0=50.0)
    ts = str(write_touchstone(tmp_path / "short.s2p", list(f), s.tolist(), 50.0))
    plain = _edge_job(ts)["results"]
    res = _edge_job(ts, tx_ffe="auto", ctle_db="auto")["results"]
    m = {k: v["value"] for k, v in res["metrics"].items()}
    assert m["ctle_db"] == 0 and m["ffe_main_tap"] > 0.97
    assert m["worst_case_eye_height_mv"] >= plain["metrics"]["worst_case_eye_height_mv"]["value"] - 0.01
    assert any(n == "receiver CTLE: none; no boost up to 12 dB leaves a larger worst-case eye"
               for n in res["notes"])


# --- the eye at a bit error rate -------------------------------------------------------

def test_jitter_weights_hold_the_whole_probability_where_it_falls():
    dt = 1e-12
    assert eye.jitter_weights(dt, 0, 0, 8).tolist() == [1.0]
    w = eye.jitter_weights(dt, 1.5e-12, 0, 40)
    n = len(w) // 2
    x = np.arange(-n, n + 1) * dt
    assert np.sum(w) == pytest.approx(1.0, abs=1e-15) and w == pytest.approx(w[::-1])
    assert n >= 13                                       # past 8.5 sigma
    assert np.sum(w * x ** 2) == pytest.approx(1.5e-12 ** 2 + dt ** 2 / 12, rel=1e-3)
    # the tail beyond 7 sigma is still there
    from scipy.special import ndtr
    assert np.sum(w[x > 10e-12]) == pytest.approx(ndtr(-10.5 / 1.5), rel=1e-6)
    # deterministic jitter alone: two points
    w = eye.jitter_weights(dt, 0, 6e-12, 40)
    assert w[len(w) // 2 - 3] == w[len(w) // 2 + 3] == 0.5 and np.sum(w) == 1.0
    # both: the Gaussian around each of the two
    w = eye.jitter_weights(dt, 1e-12, 6e-12, 40)
    n = len(w) // 2
    assert w[n + 3] == pytest.approx(0.5 * (ndtr(0.5) - ndtr(-0.5)), rel=1e-6)
    assert w[n] == pytest.approx(ndtr(-2.5) - ndtr(-3.5), rel=1e-6)
    # what the reach allowed cannot hold lands on its ends
    w = eye.jitter_weights(dt, 3e-12, 0, 2)
    assert len(w) == 5 and np.sum(w) == pytest.approx(1.0) and w[0] == pytest.approx(ndtr(-0.5))


def test_statistical_eye_of_noise_alone_is_the_gaussian_quantile():
    from scipy.special import ndtr, ndtri
    p = _tri_pulse([1.0])
    for ber in (1e-3, 1e-6, 1e-12, 1e-15, 1e-18):
        r = eye.statistical_eye(p, 0.8, ber, noise_rms=0.02)
        assert r["eye_height"] == pytest.approx(2 * (0.4 + ndtri(ber) * 0.02), rel=2e-6), ber
    # the triangle is over the level for the part of the bit the quantile leaves
    r = eye.statistical_eye(p, 0.8, 1e-12, noise_rms=0.02)
    open_part = 1 + ndtri(1e-12) * 0.02 / 0.4
    assert r["eye_width_ui"] == pytest.approx(open_part, abs=1.5 / 16)
    assert r["phase"] == 8 and r["eye_width"] == pytest.approx(r["eye_width_ui"] * 1e-10)
    # and the error rate at the threshold is the tail below it
    r = eye.statistical_eye(p, 0.8, 1e-12, noise_rms=0.1)
    assert r["bit_error_rate"] == pytest.approx(ndtr(-4.0), rel=1e-4, abs=0)
    assert r["eye_height"] == 0.0 and r["eye_width_ui"] == 0.0
    assert eye.statistical_eye(p, 0.8, 1e-12, noise_rms=0.05)["bit_error_rate"] == \
        pytest.approx(ndtr(-8.0), rel=1e-3, abs=0)          # 6e-16
    for bad in (0.2, 1e-19, 0.0):
        with pytest.raises(ValueError, match="ber is"):
            eye.statistical_eye(p, 0.8, bad)


def test_statistical_eye_of_isi_alone_steps_through_the_patterns():
    # four cursors beside the main one: 16 patterns, each 1 in 16
    p = _tri_pulse([0.05, 1.0, 0.2, -0.1, 0.03], lead=2)
    wc = eye.worst_case_eye(p, 0.8)
    assert wc["eye_height"] == pytest.approx(0.8 * (1 - 0.38))
    # rarer than any pattern: the worst case, less the grid's own width
    for ber in (1e-3, 1e-6, 1e-12, 1e-18):
        r = eye.statistical_eye(p, 0.8, ber)
        assert wc["eye_height"] - 0.004 < r["eye_height"] <= wc["eye_height"] + 1e-4, ber
    # between 1 and 2 patterns in 16: the second worst, which has the smallest cursor turned
    r = eye.statistical_eye(p, 0.8, 0.1)
    assert r["eye_height"] == pytest.approx(0.8 * (1 - 0.38 + 0.06), abs=0.002)
    # the threshold is crossed by no pattern
    assert r["bit_error_rate"] == eye.BER_FLOOR / 10
    # noise rounds the steps off: lower at 1e-12, and more so further down
    a = eye.statistical_eye(p, 0.8, 1e-12, noise_rms=0.01)["eye_height"]
    b = eye.statistical_eye(p, 0.8, 1e-15, noise_rms=0.01)["eye_height"]
    assert b < a < wc["eye_height"] - 2 * 6 * 0.01
    # yet no lower than the worst pattern with the noise's own quantile off it
    from scipy.special import ndtri
    assert a > wc["eye_height"] + 2 * ndtri(1e-12) * 0.01 - 1e-3
    # sixteen cursors of 0.07: all against the bit close the eye, once in 65536 bits; 14 or
    # more against it come 137 times in 65536, 15 or more 17 times, so 1e-3 is 14 against
    many = _tri_pulse([1.0] + [0.07, -0.07] * 8)
    assert eye.worst_case_eye(many, 0.8)["eye_height"] == 0.0
    r = eye.statistical_eye(many, 0.8, 1e-3)
    assert r["eye_height"] == pytest.approx(0.8 * (1 - 12 * 0.07), abs=0.004)
    assert r["bit_error_rate"] == pytest.approx(1 / 65536, rel=1e-6, abs=0)
    assert eye.statistical_eye(many, 0.8, 1e-6)["eye_height"] == 0.0


def test_small_cursors_go_through_the_series_and_come_out_the_same(monkeypatch):
    from scipy.special import ndtri
    # ln cos by its series, against cos itself, over the range it is used in
    x = np.array([0.05, 0.4, np.pi / 4, 0.6 - 0.3j])
    assert np.exp(eye._small_cursors(np.array([1.0]), x)) == pytest.approx(np.cos(x), rel=1e-10)
    both = eye._small_cursors(np.array([0.3, -0.5, 1.0]), x)
    assert np.exp(both) == pytest.approx(np.cos(0.3 * x) * np.cos(0.5 * x) * np.cos(x), rel=1e-10)
    assert eye._small_cursors(np.array([]), x) == pytest.approx(0)
    # one level's density: cursors of 0.24 and of 0.6 of a step lie either side of where
    # the series ends; it is the same with every cursor multiplied out
    span = 0.5
    dv = 2 * span / eye.STAT_BINS
    main = 9830 * dv                                     # on a bin, about 0.3
    cur = np.array([0.24 * dv] * 60 + [0.6 * dv] * 40 + [0.05, -0.02, 0.0])
    pdf = eye._level_density(main, cur, 3 * dv, span)
    v = (np.arange(eye.STAT_BINS) - eye.STAT_BINS // 2) * dv
    # the lower half is half the probability (the upper part is not kept: no contour is
    # read from it), and its spread is that of the sum
    k = eye.STAT_BINS // 2 + 9830
    assert np.sum(pdf[:k]) + pdf[k] / 2 == pytest.approx(0.5, abs=1e-9)
    assert 2 * np.sum(pdf[:k] * (v[:k] - main) ** 2) == pytest.approx(
        np.sum(cur ** 2) + (3 * dv) ** 2, rel=1e-6)
    with monkeypatch.context() as mp:
        mp.setattr(eye, "STAT_SERIES_BINS", 0.0)
        assert eye._level_density(main, cur, 3 * dv, span)[:k + 500] == pytest.approx(
            pdf[:k + 500], abs=1e-12)
    # 2000 cursors of 15 millionths: each a fifth of a voltage step, together a Gaussian
    # of 0.4 x 15e-6 x sqrt(2000) = 0.27 mV beside the grid's own 2.8 steps
    base = _tri_pulse([1.0, 0.2])
    tail = _tri_pulse([1.0, 0.2] + [1.5e-5, -1.5e-5] * 1000)
    a = eye.statistical_eye(base, 0.8, 1e-12)["eye_height"]
    b = eye.statistical_eye(tail, 0.8, 1e-12)["eye_height"]
    dv = 2 * 0.4 * 1.23 * 1.05 / eye.STAT_BINS
    own, added = 2.8 * dv, 0.4 * 1.5e-5 * math.sqrt(2000)
    assert 0.4 * 1.5e-5 < eye.STAT_SERIES_BINS * dv
    assert a - b == pytest.approx(-2 * ndtri(1e-12 * 4) * (math.hypot(own, added) - own), rel=0.05)
    # the same with every cursor multiplied out one by one
    monkeypatch.setattr(eye, "STAT_SERIES_BINS", 0.0)
    assert eye.statistical_eye(tail, 0.8, 1e-12)["eye_height"] == pytest.approx(b, abs=1e-6)


def test_jitter_takes_the_eye_where_the_sampling_instant_can_be():
    p = _tri_pulse([1.0])                                # 16 steps a bit: 1/8 off per step
    step = 1e-10 / 16
    flat = eye.statistical_eye(p, 0.8, 1e-12)
    assert flat["eye_height"] == pytest.approx(0.8, abs=0.004)
    # two points 3 steps either side: the level there, whichever it is
    r = eye.statistical_eye(p, 0.8, 1e-12, dj_pp=6 * step)
    assert r["eye_height"] == pytest.approx(0.8 * (1 - 3 / 8), abs=0.004)
    assert r["eye_width_ui"] == pytest.approx(flat["eye_width_ui"] - 6 / 16, abs=1.01 / 16)
    # random jitter of half a step: 4 steps off holds 2.6e-12 of it (past 7 sigma, both
    # sides) and 5 steps next to nothing, so 1e-12 is 4 steps off; 2 steps off holds
    # 2.7e-3 and 3 steps 5.7e-7, so 1e-3 is 2 steps off
    deep = eye.statistical_eye(p, 0.8, 1e-12, rj_rms=0.5 * step)
    shallow = eye.statistical_eye(p, 0.8, 1e-3, rj_rms=0.5 * step)
    assert deep["eye_height"] == pytest.approx(0.8 * (1 - 4 / 8), abs=0.004)
    assert shallow["eye_height"] == pytest.approx(0.8 * (1 - 2 / 8), abs=0.004)
    assert deep["eye_width_ui"] < shallow["eye_width_ui"] < flat["eye_width_ui"]
    both = eye.statistical_eye(p, 0.8, 1e-12, rj_rms=0.5 * step, dj_pp=6 * step)
    assert both["eye_height"] < min(deep["eye_height"], r["eye_height"])


def test_the_eye_job_gives_the_statistical_eye_when_asked(tmp_path):
    ts = _long_channel(tmp_path)

    def run(**opt):
        res = _edge_job(ts, data_rate_gbps=5, **opt)["results"]
        return {k: v["value"] for k, v in res["metrics"].items()}, res
    plain, res = run()
    assert "eye_height_at_ber_mv" not in plain and "bit_error_rate" not in plain
    assert not any(v["check"] == "eye at the bit error rate" for v in res["verdicts"])
    m, res = run(ber=1e-12)
    # between the worst case and the pattern's eye
    assert plain["worst_case_eye_height_mv"] - 3 < m["eye_height_at_ber_mv"] < plain["eye_height_mv"]
    assert m["eye_width_at_ber_ps"] == pytest.approx(m["eye_width_at_ber_ui"] * 200, abs=0.05)
    assert m["bit_error_rate"] == 1e-19
    v = {x["check"]: x for x in res["verdicts"]}["eye at the bit error rate"]
    assert v["pass"] and v["detail"] == (f"{m['eye_height_at_ber_mv']:.1f} mV by "
                                         f"{m['eye_width_at_ber_ui']:.3f} UI at 1e-12, must be open")
    assert any(n.startswith("statistical eye: ISI as independent random bits; jitter")
               for n in res["notes"])
    # noise and jitter each cost, and are named
    n, res = run(noise_rms_mv=10)
    assert " UI at 1e-12, " in {x["check"]: x for x in res["verdicts"]}[
        "eye at the bit error rate"]["detail"]              # the rate where none is given
    assert n["eye_height_at_ber_mv"] == run(noise_rms_mv=10, ber=1e-12)[0]["eye_height_at_ber_mv"]
    assert n["eye_height_at_ber_mv"] < run(noise_rms_mv=10, ber=1e-9)[0]["eye_height_at_ber_mv"]
    # at most its own 7 sigma off each rail, and less than that beside the ISI's spread
    assert m["eye_height_at_ber_mv"] - 2 * 7.04 * 10 < n["eye_height_at_ber_mv"] \
        < m["eye_height_at_ber_mv"] - 2 * 3 * 10
    j, res = run(rj_rms_ps=3, dj_pp_ps=20, noise_rms_mv=10)
    assert j["eye_height_at_ber_mv"] < n["eye_height_at_ber_mv"]
    assert j["eye_width_at_ber_ui"] < n["eye_width_at_ber_ui"] - 0.1
    assert any("10 mV RMS of Gaussian noise, 3 ps RMS of random jitter, 20 ps peak to peak of "
               "deterministic jitter; jitter is taken in steps of 6.25 ps" in x
               for x in res["notes"])
    # a looser error rate opens it
    assert run(ber=1e-6, noise_rms_mv=10)[0]["eye_height_at_ber_mv"] > n["eye_height_at_ber_mv"]
    # peak noise comes off both rails
    pk = run(noise_rms_mv=10, noise_mv=20)[0]
    assert pk["eye_height_at_ber_mv"] == pytest.approx(n["eye_height_at_ber_mv"] - 40, abs=0.01)
    assert pk["eye_width_at_ber_ui"] < n["eye_width_at_ber_ui"]
    # limits
    lim = n["eye_height_at_ber_mv"]
    for opt, ok, tail in (({"eye_at_ber_min_mv": lim - 1}, True, f", {lim - 1:g} mV needed"),
                          ({"eye_at_ber_min_mv": lim + 1}, False, f", {lim + 1:g} mV needed"),
                          ({"eye_at_ber_min_ui": 0.99}, False, ", 0.99 UI needed"),
                          ({"eye_at_ber_min_ui": 0.1, "eye_at_ber_min_mv": 5}, True,
                           ", 5 mV and 0.1 UI needed")):
        v = {x["check"]: x for x in run(noise_rms_mv=10, **opt)[1]["verdicts"]}
        v = v["eye at the bit error rate"]
        assert v["pass"] is ok and v["detail"].endswith(tail), opt
    # enough noise closes it, and the error rate says how badly
    shut, res = run(noise_rms_mv=150)
    assert shut["eye_height_at_ber_mv"] == 0 and shut["eye_width_at_ber_ui"] == 0
    assert 1e-6 < shut["bit_error_rate"] < 0.1
    assert not {x["check"]: x for x in res["verdicts"]}["eye at the bit error rate"]["pass"]


@pytest.mark.parametrize("opt, text", [
    ({"ber": 0.5}, "ber is 1e-18 to 0.1"), ({"ber": 1e-20}, "ber is 1e-18 to 0.1"),
    ({"noise_rms_mv": -1}, "noise_rms_mv is 0 or more"), ({"rj_rms_ps": -1}, "rj_rms_ps is 0"),
    ({"dj_pp_ps": -2}, "dj_pp_ps is 0 or more"), ({"eye_at_ber_min_ui": 1.5}, "eye_at_ber_min_ui"),
    ({"eye_at_ber_min_mv": -1}, "eye_at_ber_min_mv"),
    ({"rj_rms_ps": 60}, "the jitter reaches 510.0 ps"),
    ({"dj_pp_ps": 1200}, "more than half the 1000 ps bit"),
])
def test_statistical_eye_settings_are_checked(tmp_path, opt, text):
    f = ch.uniform_band(20e9, 400)
    ts = str(write_touchstone(tmp_path / "l.s2p", list(f), lossy_line(f, 0.05, 50.0).tolist(), 50.0))
    run = _edge_job(ts, **opt)
    assert run["state"] == "failed" and text in run["error"], run
