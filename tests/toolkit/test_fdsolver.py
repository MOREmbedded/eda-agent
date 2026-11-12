# SPDX-License-Identifier: Apache-2.0
"""The 2D field solver against exact and published results.

The references are independent of the solver: conformal-mapping results
that are exact (Cohn's stripline and coupled stripline, coplanar
waveguide), Hammerstad-Jensen's microstrip model (its authors: 0.2 %),
Wheeler's thick stripline (0.5 %), and the textbook stripline conductor
loss derived by Wheeler's incremental-inductance rule.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from eda_agent.engineering.toolkit import fdsolver as fd
from eda_agent.engineering.toolkit import impedance, tline

MIL = 25.4e-6


def _z(xs):
    s = impedance._solve(lambda _f: xs, 0.0, None)
    return 1.0 / (fd.C0 * math.sqrt(s.C[0, 0] * s.C0[0, 0])), s.C[0, 0] / s.C0[0, 0]


@pytest.mark.parametrize("w_over_b", [0.1, 0.3, 0.5611, 1.0, 2.0])
@pytest.mark.parametrize("er", [1.0, 4.2])
def test_stripline_matches_cohn_exact(w_over_b, er):
    b = 1e-3
    w = w_over_b * b
    xs = fd.CrossSection(metals=[fd.Metal(-w / 2, w / 2, b / 2, b / 2, 0)],
                         dielectrics=[fd.Dielectric(0, b, er)],
                         ground_bottom=0.0, ground_top=b)
    z, ee = _z(xs)
    assert z == pytest.approx(tline.stripline_cohn(w, b, er), rel=0.001)
    assert ee == pytest.approx(er, rel=1e-6)


def test_cohn_itself_at_the_self_dual_point():
    # K(k)/K(k') = 1 where k = k' = 1/sqrt(2): Z0 = eta0/4 exactly.
    b = 1.0
    w = 2.0 / math.pi * math.acosh(math.sqrt(2.0)) * b
    assert tline.stripline_cohn(w, b, 1.0) == pytest.approx(tline.ETA0 / 4.0, rel=1e-9)


@pytest.mark.parametrize("u", [0.2, 0.5, 1.0, 2.0, 5.0])
@pytest.mark.parametrize("er", [2.2, 4.2, 9.8])
def test_microstrip_matches_hammerstad_jensen(u, er):
    h = 0.2e-3
    w = u * h
    xs = fd.CrossSection(metals=[fd.Metal(-w / 2, w / 2, h, h, 0)],
                         dielectrics=[fd.Dielectric(0, h, er)], ground_bottom=0.0)
    z, ee = _z(xs)
    z_ref, ee_ref = tline.microstrip_hj(w, h, 0.0, er)
    assert z == pytest.approx(z_ref, rel=0.004)
    assert ee == pytest.approx(ee_ref, rel=0.004)


@pytest.mark.parametrize("w,s", [(0.3, 0.1), (0.5, 0.3), (0.2, 0.6)])
def test_coupled_stripline_matches_cohn_exact(w, s):
    b, er = 1e-3, 3.5
    w, s = w * b, s * b
    xs = fd.CrossSection(metals=[fd.Metal(-s / 2 - w, -s / 2, b / 2, b / 2, 0),
                                 fd.Metal(s / 2, s / 2 + w, b / 2, b / 2, 1)],
                         dielectrics=[fd.Dielectric(0, b, er)],
                         ground_bottom=0.0, ground_top=b, feature=min(w, s))
    sol = impedance._solve(lambda _f: xs, 0.0, None)
    C, C0 = sol.C, sol.C0
    ze_ref, zo_ref = tline.coupled_stripline_cohn(w, s, b, er)
    zo = 1.0 / (fd.C0 * math.sqrt((C[0, 0] - C[0, 1]) * (C0[0, 0] - C0[0, 1])))
    ze = 1.0 / (fd.C0 * math.sqrt((C[0, 0] + C[0, 1]) * (C0[0, 0] + C0[0, 1])))
    assert zo == pytest.approx(zo_ref, rel=0.0015)
    assert ze == pytest.approx(ze_ref, rel=0.0015)


def test_thick_stripline_matches_wheeler():
    for w, h, t in ((5, 5, 1.378), (8, 10, 0.7), (3, 4, 0.7), (12, 6, 2.756)):
        r = impedance.stripline(impedance.stripline.__wrapped_parse__(
            {"w": w, "h1": h, "h2": h, "t": f"{t} mil", "frequency": 0}))
        ref = tline.stripline_wheeler(w * MIL, (2 * h + t) * MIL, t * MIL, 4.2)
        assert r.value("z0") == pytest.approx(ref, rel=0.006)


def test_coplanar_in_air_matches_the_exact_conformal_map():
    # With no substrate the conformal map is exact: Z0 = (eta0/4) K(k')/K(k).
    r = impedance.coplanar(impedance.coplanar.__wrapped_parse__(
        {"t": "0 mil", "grounded": False, "frequency": 0, "gw": 600, "er": 1.0}))
    k0 = 10.0 / 22.0
    assert r.value("z0") == pytest.approx(tline.ETA0 / 4 / tline.k_ratio(k0), rel=0.002)
    assert r.value("er_eff") == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("grounded", [True, False])
def test_coplanar_on_a_substrate_agrees_with_the_published_formula(grounded):
    # Veyres and Fouad Hanna's finite-substrate formula (Simons 2001)
    # assumes a magnetic wall at the dielectric face, so it is good to a
    # percent or two, not exact.
    args = {"t": "0 mil", "grounded": grounded, "frequency": 0, "gw": 600}
    r = impedance.coplanar(impedance.coplanar.__wrapped_parse__(args))
    z_ref, ee_ref = tline.cpw(10 * MIL, 6 * MIL, 8 * MIL, 4.2, grounded)
    assert r.value("z0") == pytest.approx(z_ref, rel=0.015)
    assert r.value("er_eff") == pytest.approx(ee_ref, rel=0.025)


def test_capacitance_matrix_is_symmetric_and_physical():
    w, s, h = 5 * MIL, 5 * MIL, 4 * MIL
    xs = fd.CrossSection(metals=[fd.Metal(-s / 2 - w, -s / 2, h, h + 35e-6, 0),
                                 fd.Metal(s / 2, s / 2 + w, h, h + 35e-6, 1)],
                         dielectrics=[fd.Dielectric(0, h, 4.2, 0.02)], ground_bottom=0.0)
    sol = fd.solve(xs, 30)
    assert sol.C == pytest.approx(sol.C.T)
    assert sol.C[0, 0] > 0 and sol.C[0, 1] < 0          # Maxwell form
    assert sol.C[0, 0] == pytest.approx(sol.C[1, 1], rel=1e-6)   # mirror symmetry
    assert sol.L[0, 1] > 0
    # Dielectric only raises capacitance; G follows the lossy region only.
    assert np.all(np.diag(sol.C) > np.diag(sol.C0))
    assert 0 < sol.G[0, 0] < 0.02 * sol.C[0, 0]
    # L C0 = mu0 eps0 (TEM identity in vacuum)
    assert sol.L @ sol.C0 == pytest.approx(fd.MU0 * fd.EPS0 * np.eye(2), rel=1e-9, abs=1e-30)


def test_homogeneous_loss_tangent_gives_the_textbook_dielectric_loss():
    # alpha_d = pi f sqrt(er) tan d / c for any TEM line in one dielectric.
    r = impedance.stripline(impedance.stripline.__wrapped_parse__(
        {"tand": 0.02, "frequency": "1 GHz", "f_spec": "1 GHz", "roughness": "none"}))
    alpha = math.pi * 1e9 * math.sqrt(4.2) * 0.02 / fd.C0
    db_per_in = alpha * 20 / math.log(10) * 0.0254
    assert r.value("loss_dielectric") == pytest.approx(db_per_in, rel=0.002)


@pytest.mark.parametrize("w,h,t", [(5, 5, 1.378), (8, 10, 0.7), (3, 4, 0.7)])
def test_conductor_loss_matches_wheelers_incremental_inductance_rule(w, h, t):
    r = impedance.stripline(impedance.stripline.__wrapped_parse__(
        {"w": w, "h1": h, "h2": h, "t": f"{t} mil", "roughness": "none", "tand": 0,
         "frequency": "1 GHz", "f_spec": "1 GHz"}))
    z, er = r.value("z0"), 4.2
    b, T, W = (2 * h + t) * MIL, t * MIL, w * MIL
    rs = tline.surface_resistance(1e9)
    # Pozar, Microwave Engineering, eq. 3.181 (sqrt(er) Z0 < 120 ohm).
    A = 1 + 2 * W / (b - T) + (b + T) / (math.pi * (b - T)) * math.log((2 * b - T) / T)
    alpha = 2.7e-3 * rs * er * z / (30 * math.pi * (b - T)) * A
    assert r.value("loss_conductor") == pytest.approx(alpha * 8.686 * 0.0254, rel=0.03)


def test_solder_mask_lowers_microstrip_impedance():
    base = {"frequency": 0}
    bare = impedance.microstrip(impedance.microstrip.__wrapped_parse__(base)).value("z0")
    masked = impedance.microstrip(impedance.microstrip.__wrapped_parse__(
        dict(base, mask_t="1 mil", mask_er=3.5))).value("z0")
    assert 0.90 * bare < masked < 0.985 * bare


def test_etch_raises_impedance():
    base = {"frequency": 0}
    rect = impedance.microstrip(impedance.microstrip.__wrapped_parse__(base)).value("z0")
    trap = impedance.microstrip(impedance.microstrip.__wrapped_parse__(
        dict(base, etch="1 mil"))).value("z0")
    assert rect < trap < 1.06 * rect


def test_grid_refinement_converges_monotonically():
    b, w = 1e-3, 0.4e-3
    xs = fd.CrossSection(metals=[fd.Metal(-w / 2, w / 2, b / 2, b / 2, 0)],
                         dielectrics=[fd.Dielectric(0, b, 4.0)],
                         ground_bottom=0.0, ground_top=b)
    ref = tline.stripline_cohn(w, b, 4.0)
    errs = []
    for res in (20, 40, 80, 160):
        s = fd.solve(xs, res)
        errs.append(abs(1 / (fd.C0 * math.sqrt(s.C[0, 0] * s.C0[0, 0])) / ref - 1))
    assert errs == sorted(errs, reverse=True)
    # First order at a knife edge: each refinement halves the error.
    for coarse, fine in zip(errs, errs[1:]):
        assert 1.7 < coarse / fine < 2.4
    assert errs[-1] < 0.004


def test_the_mesh_march_always_arrives(monkeypatch):
    """The size field can never reach zero, whatever the growth rates.

    With the near growth above the far growth the size used to fall with
    distance and the march stalled; found by a seeded break that hung
    the suite instead of failing it.
    """
    monkeypatch.setattr(fd, "GROWTH", 0.4)
    monkeypatch.setattr(fd, "FAR_GROWTH", 0.05)
    line = fd._axis([0.0, 1e-4], -5e-3, 5e-3, 1e-6, 5e-4, (0.0, 1e-4))
    assert line[0] == -5e-3 and line[-1] == 5e-3
    steps = np.diff(line)
    assert steps.min() > 0 and len(line) < 20000
    assert {0.0, 1e-4} <= set(line.tolist())


def test_a_mesh_that_cannot_be_built_is_refused_not_marched_forever():
    """300 000 cells across one interval is a geometry no solve should
    be attempted on; the march stops and says what it could not cross."""
    with pytest.raises(ValueError, match="mesh could not be built"):
        fd._axis([0.0], 0.0, 0.3, 1e-6, 1e-6, (0.0, 0.3))
    # An ordinary interval of the same size field is untouched by the cap.
    assert len(fd._axis([0.0], 0.0, 0.01, 1e-6, 1e-6, (0.0, 0.01))) == pytest.approx(10001, abs=3)


def test_a_grid_too_large_to_solve_is_refused_with_its_size(monkeypatch):
    from eda_agent.engineering import toolkit
    assert toolkit.run("microstrip", {})["ok"]
    monkeypatch.setattr(fd, "MAX_NODES", 5000)
    got = toolkit.run("microstrip", {})
    assert not got["ok"] and "nodes" in got["reason"] and "5000" in got["reason"]
    # The widest and thinnest geometry stays far inside the real limit.
    monkeypatch.undo()
    sizes = []
    mesh = fd._mesh
    monkeypatch.setattr(fd, "_mesh", lambda xs, res: sizes.append(mesh(xs, res)) or sizes[-1])
    assert toolkit.run("microstrip", {"w": "500 mil", "h": "0.5 mil", "t": "0.1 mil"})["ok"]
    assert max(len(x) * len(y) for x, y in sizes) < fd.MAX_NODES / 5
