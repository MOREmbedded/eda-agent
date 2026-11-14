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
