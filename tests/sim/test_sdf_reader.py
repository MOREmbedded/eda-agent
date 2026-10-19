# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The results reader returns what the circuit must produce.

Both fixtures are results files Altium 26.10.1.6 wrote for two small
SPICE decks run with ADVSIM:RunSimulator:

  divider_op_tran.sdf  V1 in 0 DC 10 PULSE(0 10 0 1u 1u 5u 10u),
                       R1 in out 1k, R2 out 0 1k, C1 out 0 1n;
                       .OP and .TRAN 0.1u 20u
  rc_ac_dc.sdf         the same network with V1 DC 10 AC 1;
                       .AC DEC 5 1k 10meg and .DC V1 0 10 2.5

Every expected value below is worked out from the circuit, not copied
from a reading, so a reader that decodes the wrong bytes cannot pass.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from eda_agent.sim.sdf import (SdfError, latest_charts, parse_sdf, read_sdf,
                               summarise)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "sim"

#: R1 || R2 driving C1: the Thevenin resistance and the corner frequency.
R_TH = 500.0
C1 = 1e-9
F_C = 1.0 / (2 * math.pi * R_TH * C1)


def _chart(charts, name):
    found = [c for c in charts if c.name == name]
    assert found, f"no {name!r} chart in {[c.name for c in charts]}"
    return found[-1]


def test_the_operating_point_is_the_divider_and_its_current():
    op = _chart(read_sdf(FIX / "divider_op_tran.sdf"), "Operating Point")
    assert op.wave("v(in)").y == [10.0]
    assert op.wave("v(out)").y == [5.0]
    # 10 V across 2 kOhm, flowing out of the source's positive terminal.
    assert op.wave("i(v1)").y[0] == pytest.approx(-5e-3, rel=1e-12)


def test_the_transient_follows_the_pulse_and_settles_at_half():
    tran = _chart(read_sdf(FIX / "divider_op_tran.sdf"), "Transient Analysis")
    time = tran.wave("time")
    vin, vout = tran.wave("v(in)"), tran.wave("v(out)")
    assert time.independent and not vout.independent
    assert time.points == vin.points == vout.points > 100
    assert time.x[0] == 0.0 and time.x[-1] == pytest.approx(20e-6)
    assert max(vin.y) == pytest.approx(10.0)
    # Five time constants (2.5 us) into the 5 us high: within 1% of 5 V.
    tau = R_TH * C1
    settled = [y for t, y in zip(vout.x, vout.y) if 1e-6 + 5 * tau <= t <= 6e-6]
    assert settled and all(4.95 <= y <= 5.0 + 1e-9 for y in settled)


def test_the_ac_magnitude_and_phase_are_a_first_order_low_pass():
    ac = _chart(read_sdf(FIX / "rc_ac_dc.sdf"), "AC Analysis")
    vout = ac.wave("v(out)")
    assert vout.y_complex and vout.x_complex
    assert vout.points == 21          # 5 points a decade over 4 decades
    for f, h in zip(vout.x, vout.y):
        f = f.real
        w = 2 * math.pi * f * R_TH * C1
        assert abs(h) == pytest.approx(0.5 / math.sqrt(1 + w * w), rel=1e-6)
        assert math.degrees(math.atan2(h.imag, h.real)) == pytest.approx(
            -math.degrees(math.atan(w)), abs=1e-4)


def test_the_dc_sweep_halves_the_source():
    dc = _chart(read_sdf(FIX / "rc_ac_dc.sdf"), "DC Sweep")
    sweep, vout = dc.wave("v(sweep)"), dc.wave("v(out)")
    assert sweep.independent
    assert sweep.y == [0.0, 2.5, 5.0, 7.5, 10.0]
    assert vout.y == [0.0, 1.25, 2.5, 3.75, 5.0]


def test_summarise_reports_magnitude_and_phase_for_a_complex_wave():
    ac = _chart(read_sdf(FIX / "rc_ac_dc.sdf"), "AC Analysis")
    s = summarise(ac.wave("v(out)"), max_points=3)
    assert s["complex"] and s["sample_columns"] == ["x", "magnitude", "phase_deg"]
    assert len(s["samples"]) == 3
    assert s["samples"][0][0] == pytest.approx(1e3)
    assert s["samples"][-1][0] == pytest.approx(1e7)
    assert s["magnitude"]["max"] == pytest.approx(0.5, rel=1e-5)


def test_summarise_keeps_first_and_last_and_stats_over_every_point():
    tran = _chart(read_sdf(FIX / "divider_op_tran.sdf"), "Transient Analysis")
    vout = tran.wave("v(out)")
    s = summarise(vout, max_points=4)
    assert len(s["samples"]) == 4
    assert s["samples"][0] == [vout.x[0], vout.y[0]]
    assert s["samples"][-1] == [vout.x[-1], vout.y[-1]]
    assert s["max"] == max(vout.y) and s["min"] == min(vout.y)


def test_a_second_run_of_the_same_analysis_supersedes_the_first():
    data = (FIX / "divider_op_tran.sdf").read_bytes()
    start = data.index(b"Chart: Operating Point", data.index(b"#ChartSetupsEnd"))
    end = data.index(b"Chart: Transient Analysis", start)
    first = data[start:end]
    second = first.replace(b"\x00\x00\x00\x00\x00\x00\x14\x40",
                           b"\x00\x00\x00\x00\x00\x00\x18\x40", 1)  # 5.0 -> 6.0
    charts = parse_sdf(data + b"\n" + second)
    ops = [c for c in charts if c.name == "Operating Point"]
    assert len(ops) == 2
    latest = [c for c in latest_charts(charts) if c.name == "Operating Point"]
    assert len(latest) == 1 and latest[0] is ops[-1]
    assert 6.0 in [w.y[0] for w in latest[0].waves]


def test_a_file_that_is_not_results_is_refused():
    with pytest.raises(SdfError):
        parse_sdf(b"<?xml version='1.0'?><ProjectConfig/>")


def test_a_truncated_wave_is_refused_rather_than_read_short():
    data = (FIX / "rc_ac_dc.sdf").read_bytes()
    cut = data.index(b"WaveExtendedDataStart") + 60
    with pytest.raises(SdfError):
        parse_sdf(data[:cut])
