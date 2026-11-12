# SPDX-License-Identifier: Apache-2.0
"""A 50 ohm microstrip through the real openEMS, against Hammerstad-Jensen.

Runs only where openEMS is installed (``EDA_AGENT_OPENEMS_PYTHON`` set and
able to import it); skipped everywhere else. The line is 14 mil wide on
8 mil of er 4.2 over a solid ground layer, 1000 mil long, with a port at
each end. Its characteristic impedance and effective permittivity are
pulled out of the solved S-parameters (ABCD of a uniform line) and
compared with the closed-form microstrip formulas.
"""

from __future__ import annotations

import cmath
import math
import os
import time

import pytest

from eda_agent.emc import openems as em
from eda_agent.emc.stack import stack_from_stackup

from .boards import LAYERS, STACKUP, microstrip_board

pytestmark = pytest.mark.skipif(not os.environ.get("EDA_AGENT_OPENEMS_PYTHON"),
                                reason="openEMS not configured (EDA_AGENT_OPENEMS_PYTHON)")


def hammerstad(w_over_h: float, er: float) -> tuple[float, float]:
    """Z0 and effective permittivity of a zero-thickness microstrip."""
    u = w_over_h
    a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 \
        + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    eeff = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)
    f = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
    z0_air = 60 * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))
    return z0_air / math.sqrt(eeff), eeff


def test_microstrip_matches_closed_form(tmp_path):
    runs = os.environ.get("EDA_AGENT_EM_RUNS") or str(tmp_path)
    model = em.build_model(microstrip_board(), ["RF"], ["J1.1", "J2.1"],
                           stack_from_stackup(STACKUP, LAYERS), f_max_hz=3e9,
                           points=31)
    folder = em.write_model(model, runs, name="verify_microstrip")
    started = em.start_run(folder, python=os.environ["EDA_AGENT_OPENEMS_PYTHON"])
    assert started["ok"], started
    deadline = time.time() + 1800
    while em.run_status(folder)["state"] == "running" and time.time() < deadline:
        time.sleep(3)
    status = em.run_status(folder)
    assert status["state"] == "done", status
    out = em.collect_results(folder, plot=True)
    assert out["ok"]
    assert out["settled"], out.get("port_decay_db")
    from eda_agent.emc.touchstone import read_touchstone
    ts = read_touchstone(out["touchstone"])
    k = min(range(len(ts["freq_hz"])), key=lambda i: abs(ts["freq_hz"][i] - 1e9))
    f = ts["freq_hz"][k]
    (s11, s12), (s21, s22) = ts["s"][k]
    z = 50.0
    # S -> ABCD (Pozar table 4.2)
    d = 2 * s21
    A = ((1 + s11) * (1 - s22) + s12 * s21) / d
    B = z * ((1 + s11) * (1 + s22) - s12 * s21) / d
    C = ((1 - s11) * (1 - s22) - s12 * s21) / (z * d)
    z0 = cmath.sqrt(B / C).real
    gl = cmath.acosh(A)
    beta_l = abs(gl.imag)
    length_m = 1000 * 25.4e-6
    eeff = (beta_l * 299792458 / (2 * math.pi * f * length_m)) ** 2
    z_ref, e_ref = hammerstad(14 / 8, 4.2)
    print(f"f={f/1e6:.0f} MHz Z0={z0:.1f} (ref {z_ref:.1f}) eeff={eeff:.3f} (ref {e_ref:.3f}) "
          f"|S21|={abs(s21):.3f} folder={folder}")
    # Z0 through lumped ports carries the ports' own parasitics: 10 %.
    assert z0 == pytest.approx(z_ref, rel=0.10)
    assert eeff == pytest.approx(e_ref, rel=0.10)
    assert abs(s21) > 0.85
    assert abs(s11) < 0.2


def _s21_phase(length_mils: float, runs: str, f_hz: float) -> float:
    model = em.build_model(microstrip_board(length=length_mils), ["RF"], ["J1.1", "J2.1"],
                           stack_from_stackup(STACKUP, LAYERS), f_max_hz=3e9, points=31)
    folder = em.write_model(model, runs, name=f"verify_line_{int(length_mils)}")
    assert em.start_run(folder, python=os.environ["EDA_AGENT_OPENEMS_PYTHON"])["ok"]
    deadline = time.time() + 1800
    while em.run_status(folder)["state"] == "running" and time.time() < deadline:
        time.sleep(3)
    out = em.collect_results(folder, plot=False)
    assert out["ok"] and out["settled"], out
    from eda_agent.emc.touchstone import read_touchstone
    ts = read_touchstone(out["touchstone"])
    k = min(range(len(ts["freq_hz"])), key=lambda i: abs(ts["freq_hz"][i] - f_hz))
    return ts["freq_hz"][k], cmath.phase(ts["s"][k][1][0])


def test_two_lengths_give_the_effective_permittivity(tmp_path):
    """The phase difference of two line lengths cancels the ports, so the
    effective permittivity is held to 5 %."""
    runs = os.environ.get("EDA_AGENT_EM_RUNS") or str(tmp_path)
    f, p1 = _s21_phase(1000, runs, 1e9)
    _, p2 = _s21_phase(1500, runs, 1e9)
    dphi = (p1 - p2) % (2 * math.pi)
    beta = dphi / (500 * 25.4e-6)
    eeff = (beta * 299792458 / (2 * math.pi * f)) ** 2
    _, e_ref = hammerstad(14 / 8, 4.2)
    print(f"two-length eeff={eeff:.3f} (ref {e_ref:.3f})")
    assert eeff == pytest.approx(e_ref, rel=0.05)
