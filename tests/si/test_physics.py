# SPDX-License-Identifier: Apache-2.0
"""The line physics against exact limits and independent solvers."""
from __future__ import annotations

import math

import numpy as np
import pytest

from eda_agent.engineering.toolkit import fdsolver as fd
from eda_agent.si import eddy, materials as mat, mtl, rlgc
from eda_agent.si.section import microstrip, stripline

RHO = mat.RHO_CU
MIL = 25.4e-6


# -- materials -----------------------------------------------------------------

def test_djordjevic_sarkar_passes_through_the_data_sheet_point():
    e = mat.eps_ds(4.2, 0.02, 1e9, 1e9)
    assert e.real == pytest.approx(4.2, rel=1e-12)
    assert -e.imag / e.real == pytest.approx(0.02, rel=1e-12)


def test_djordjevic_sarkar_is_causal_er_falls_with_frequency():
    er = [mat.eps_ds(4.2, 0.02, 1e9, f).real for f in (1e6, 1e8, 1e9, 1e10, 1e11)]
    assert all(a > b for a, b in zip(er, er[1:]))
    assert mat.eps_ds(3.0, 0.0, 1e9, 1e10) == complex(3.0, 0.0)


def test_roughness_is_one_at_low_frequency_and_grows():
    cu = mat.copper("std")
    assert cu.rough(1e3) == pytest.approx(1.0, abs=1e-3)
    ks = [cu.rough(f) for f in (1e8, 1e9, 1e10, 5e10)]
    assert all(a < b for a, b in zip(ks, ks[1:]))
    assert mat.copper("smooth").rough(1e10) == 1.0
    with pytest.raises(ValueError):
        mat.copper("nope")
    with pytest.raises(ValueError):
        mat.laminate("nope")


# -- eddy currents ---------------------------------------------------------------

@pytest.fixture(scope="module")
def ms():
    return microstrip(0.2e-3, 0.1e-3, 35e-6, mat.laminate("fr4"))


@pytest.fixture(scope="module")
def ms_eddy(ms):
    return eddy.solve(ms, f_max=20e9)


def test_dc_resistance_is_exact(ms, ms_eddy):
    plane = [c for c in eddy.conductors(ms) if c.group < 0][0].metal
    expected = RHO / (0.2e-3 * 35e-6) + RHO / ((plane.x1 - plane.x0) * 35e-6)
    assert ms_eddy.R[0, 0, 0] == pytest.approx(expected, rel=2e-3)


def test_high_frequency_inductance_matches_the_electrostatic_solve(ms, ms_eddy):
    sol = fd.solve(ms.to_fd(), 40)
    assert ms_eddy.asymptote["l_inf"][0, 0] == pytest.approx(sol.L[0, 0], rel=0.01)


def test_skin_effect_loss_matches_wheelers_rule(ms, ms_eddy):
    """Two independent methods: the eddy-current field against the
    electrostatic surface current (incremental inductance)."""
    f = 2e9
    sol = fd.solve(ms.to_fd(), 80)
    z0 = 1 / (fd.C0 * math.sqrt(sol.C[0, 0] * sol.C0[0, 0]))
    rs = math.sqrt(math.pi * f * fd.MU0 * RHO)
    r_wheeler = 2 * z0 * fd.conductor_loss_np_per_m(sol, [1.0], z0, rs)
    R, _ = eddy.evaluate(ms_eddy, np.array([f]))
    assert R[0, 0, 0] == pytest.approx(r_wheeler, rel=0.06)


def test_resistance_rises_and_inductance_falls_with_frequency(ms_eddy):
    R, L = ms_eddy.R[:, 0, 0], ms_eddy.L[:, 0, 0]
    assert np.all(np.diff(R) > 0) and np.all(np.diff(L) < 0)


def test_a_pair_shares_its_return_resistance():
    pair = stripline(0.1e-3, 0.12e-3, 0.25e-3, 18e-6, mat.laminate("fr4"), n=2, s=0.12e-3)
    r = eddy.solve(pair, f_max=5e9, points=5)
    Z = r.Z[-1]
    assert np.allclose(Z, Z.T, rtol=1e-6, atol=1e-9)
    assert Z[0, 1].real > 0 and Z[0, 1].real < Z[0, 0].real
    assert Z[0, 0].real == pytest.approx(Z[1, 1].real, rel=0.02)


# -- RLGC --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ms_rlgc(ms):
    return rlgc.compute(ms, np.geomspace(1e7, 20e9, 25))


def test_lossless_impedance_matches_the_toolkit_calculator(ms_rlgc):
    from eda_agent.engineering import toolkit
    k = int(np.argmin(np.abs(ms_rlgc.f - 1e9)))
    ours = rlgc.figures(ms_rlgc, k)
    ref = toolkit.run("microstrip", {"w": 0.2e-3 / MIL, "h": 0.1e-3 / MIL, "t": "35 um",
                                     "er": mat.eps_ds(4.3, 0.02, 1e9, ms_rlgc.f[k]).real,
                                     "tand": 0.0, "frequency": ms_rlgc.f[k] / 1e9})
    z_ref = next(o["value"] for o in ref["outputs"] if o["key"] == "z0_static")
    assert ours["z0_qs"] == pytest.approx(z_ref, rel=0.005)
    assert ours["z0"] > ours["z0_qs"], "internal inductance raises the lossy impedance"


def test_homogeneous_dielectric_loss_is_its_tan_delta():
    sl = stripline(0.15e-3, 0.2e-3, 0.235e-3, 35e-6, mat.Laminate(3.8, 0.01, 2e9))
    r = rlgc.compute(sl, [2e9, 3e9])
    w = 2 * math.pi * r.f[0]
    assert r.G[0, 0, 0] / (w * r.C[0, 0, 0]) == pytest.approx(0.01, rel=1e-3)


def test_lossless_dielectric_has_no_conductance():
    sl = stripline(0.15e-3, 0.2e-3, 0.235e-3, 35e-6, mat.Laminate(3.8, 0.0))
    r = rlgc.compute(sl, [1e9, 2e9])
    assert np.all(np.abs(r.G) < 1e-15)


def test_roughness_adds_loss(ms):
    rough = microstrip(0.2e-3, 0.1e-3, 35e-6, mat.laminate("fr4"), copper=mat.copper("std"))
    f = [1e8, 1e10]
    a = rlgc.compute(ms, f)
    b = rlgc.compute(rough, f)
    assert b.R[0, 0, 0] == pytest.approx(a.R[0, 0, 0], rel=0.05)
    assert b.R[-1, 0, 0] > 1.3 * a.R[-1, 0, 0]


# -- coupled lines ------------------------------------------------------------------

def test_single_line_matches_scikit_rf(ms_rlgc):
    skrf = pytest.importorskip("skrf")
    from skrf.media import DefinedGammaZ0
    f = ms_rlgc.f
    w = 2 * np.pi * f
    Z = ms_rlgc.R[:, 0, 0] + 1j * w * ms_rlgc.L[:, 0, 0]
    Y = ms_rlgc.G[:, 0, 0] + 1j * w * ms_rlgc.C[:, 0, 0]
    med = DefinedGammaZ0(skrf.Frequency.from_f(f, unit="hz"), gamma=np.sqrt(Z * Y),
                         z0=np.sqrt(Z / Y), z0_port=50)
    ref = med.line(0.1, unit="m").s
    assert np.max(np.abs(mtl.s_params(ms_rlgc, 0.1) - ref)) < 1e-9


def test_a_line_in_two_halves_is_the_whole_line(ms_rlgc):
    def abcd(s):
        a = ((1 + s[0, 0]) * (1 - s[1, 1]) + s[0, 1] * s[1, 0]) / (2 * s[1, 0])
        b = 50 * ((1 + s[0, 0]) * (1 + s[1, 1]) - s[0, 1] * s[1, 0]) / (2 * s[1, 0])
        c = ((1 - s[0, 0]) * (1 - s[1, 1]) - s[0, 1] * s[1, 0]) / (2 * s[1, 0] * 50)
        d = ((1 - s[0, 0]) * (1 + s[1, 1]) + s[0, 1] * s[1, 0]) / (2 * s[1, 0])
        return np.array([[a, b], [c, d]])
    half = mtl.s_params(ms_rlgc, 0.05)
    whole = mtl.s_params(ms_rlgc, 0.1)
    for k in (0, 12, 24):
        assert np.allclose(abcd(half[k]) @ abcd(half[k]), abcd(whole[k]), rtol=1e-7, atol=1e-9)


def test_matched_lossless_line_only_delays():
    sl = stripline(0.15e-3, 0.2e-3, 0.235e-3, 35e-6, mat.Laminate(3.8, 0.0))
    r = rlgc.compute(sl, [1e9, 2e9])
    r.R[:] = 0.0
    z0 = math.sqrt(r.L[0, 0, 0] / r.C[0, 0, 0])
    s = mtl.s_params(r, 0.07, z_ref=z0)
    assert abs(s[0, 0, 0]) < 1e-9 and abs(s[0, 1, 0]) == pytest.approx(1.0, abs=1e-9)
    beta = 2 * math.pi * 1e9 * math.sqrt(r.L[0, 0, 0] * r.C[0, 0, 0])
    assert np.angle(s[0, 1, 0]) == pytest.approx(np.angle(np.exp(-1j * beta * 0.07)), abs=1e-9)


def test_pair_figures_and_mixed_mode_conserve_power():
    pair = microstrip(0.12e-3, 0.1e-3, 35e-6, mat.laminate("fr4"), n=2, s=0.15e-3)
    r = rlgc.compute(pair, np.geomspace(1e8, 10e9, 9))
    fig = rlgc.figures(r, 4)
    assert fig["z_diff_qs"] == pytest.approx(2 * fig["z_odd_qs"])
    assert fig["z_even_qs"] > fig["z_odd_qs"] and fig["delay_even_ps_per_in"] > fig["delay_odd_ps_per_in"]
    # The quasi-static odd and even impedances against the modal solution
    # (different path: characteristic impedance matrix of the lossy line).
    hi = rlgc.figures(r, len(r.f) - 1)
    assert hi["z_odd"] == pytest.approx(hi["z_odd_qs"], rel=0.02)
    assert hi["z_even"] == pytest.approx(hi["z_even_qs"], rel=0.02)
    s = mtl.s_params(r, 0.1)
    assert np.max(np.abs(s - np.transpose(s, (0, 2, 1)))) < 1e-12
    assert np.max(np.linalg.svd(s, compute_uv=False)) <= 1.0 + 1e-9
    m = mtl.mixed_mode(s)
    assert np.allclose(np.linalg.svd(m, compute_uv=False), np.linalg.svd(s, compute_uv=False))


def test_inductance_tends_to_the_electrostatic_one(ms):
    """At a frequency where the internal inductance is negligible, L is
    the electrostatic solve's mu0 eps0 C0^-1 (the eddy grid's own value
    is replaced by it)."""
    r = rlgc.compute(ms, [1e9, 1e14])
    l_es = fd.MU0 * fd.EPS0 * np.linalg.inv(r.C0)[0, 0]
    assert r.L[-1, 0, 0] == pytest.approx(l_es, rel=3e-4)
    assert r.L[0, 0, 0] > r.L[-1, 0, 0]
