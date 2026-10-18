# SPDX-License-Identifier: Apache-2.0
"""The calculators, pinned to published reference values.

A calculator tested against its own output is a regression test for a
typo and nothing else: it passes just as happily when the formula is
wrong from the first commit. So every number below comes from a wire
table, a standard, or a textbook figure, and the test says where.
"""
from __future__ import annotations

import pytest

from eda_agent.engineering.pcb_calc import (
    IPC2221_ENVIRONMENTS,
    awg_properties,
    fusing_current,
    ipc2221_spacing,
    padstack_geometry,
    pdn_target_impedance,
    reactance,
    resonant_frequency,
    spiral_inductor,
    via_parasitics,
    wavelength_in_dielectric,
)


# ---- AWG: exact by definition, so the standard table must reproduce ----

@pytest.mark.parametrize("awg,diameter_mm", [
    (-3, 11.684),   # 0000 / 4-0
    (0, 8.252),
    (10, 2.588),
    (12, 2.053),
    (20, 0.812),
    (24, 0.511),
    (36, 0.127),    # the anchor the definition is built on
])
def test_awg_diameters_match_the_standard_wire_table(awg, diameter_mm):
    assert awg_properties(awg)["diameter_mm"] == pytest.approx(
        diameter_mm, abs=0.001)


def test_awg_resistance_matches_the_standard_table():
    """AWG 24 copper is tabulated at 84.22 ohm/km."""
    assert awg_properties(24)["resistance_ohm_per_m"] == pytest.approx(
        0.08422, rel=0.01)


def test_awg_resistance_rises_with_temperature():
    cold = awg_properties(24, temperature_c=20.0)["resistance_ohm"]
    hot = awg_properties(24, temperature_c=85.0)["resistance_ohm"]
    assert hot > cold
    # +0.393 %/degC over 65 degC is about +25 %.
    assert hot / cold == pytest.approx(1.255, rel=0.02)


@pytest.mark.parametrize("bad", [-4, 41, "24", None, True])
def test_an_awg_outside_the_defined_range_is_refused(bad):
    assert awg_properties(bad)["ok"] is False


# ---- Onderdonk ---------------------------------------------------------

def test_fusing_current_reproduces_the_wire_case_it_was_written_for():
    """AWG 10 is 10380 circular mils; Onderdonk at 1 s gives about
    1.5 kA, and the equation scales linearly with area, so a PCB track
    of known area pins the same constant.

    A 100 mil 1 oz track is 137.8 square mils = 175.4 circular mils,
    which is 10380/175.4 = 59.2 times smaller, so 1518/59.2 = 25.6 A.
    """
    result = fusing_current(100, copper_oz=1.0, time_s=1.0)
    assert result["area_circular_mils"] == pytest.approx(175.4, rel=0.01)
    assert result["fusing_current_a"] == pytest.approx(25.6, rel=0.02)


def test_fusing_current_follows_the_inverse_root_of_time():
    """I * sqrt(t) is the invariant; four times the time is half the
    current."""
    one = fusing_current(50, time_s=1.0)["fusing_current_a"]
    four = fusing_current(50, time_s=4.0)["fusing_current_a"]
    assert four == pytest.approx(one / 2.0, rel=0.001)


def test_circular_mils_are_not_square_mils():
    """The conversion is pi/4, and getting it backwards scales every
    answer by 1.27 with nothing to show for it."""
    result = fusing_current(100, copper_oz=1.0)
    assert result["area_sq_mils"] == pytest.approx(137.8, rel=0.001)
    assert result["area_circular_mils"] == pytest.approx(
        137.8 * 4 / 3.14159265, rel=0.001)


def test_a_long_duration_says_the_formula_has_left_its_range():
    """Onderdonk is adiabatic. At ten seconds it returns 8 A for a track
    IPC-2221 rates at 5 A continuously, which is nonsense as a fusing
    current and has to be labelled rather than served."""
    short = fusing_current(100, time_s=0.5)
    long = fusing_current(100, time_s=10.0)
    assert "out_of_range" not in short
    assert "adiabatic" in long["out_of_range"].lower()


def test_fusing_is_never_mistakable_for_a_rating():
    assert "DESTROYS" in fusing_current(10)["warning"]


@pytest.mark.parametrize("kwargs", [
    {"width_mils": 0}, {"width_mils": -5}, {"width_mils": "10"},
    {"width_mils": 10, "copper_oz": 0}, {"width_mils": 10, "time_s": 0},
    {"width_mils": 10, "ambient_c": 1200},
])
def test_impossible_fusing_inputs_are_refused_not_raised(kwargs):
    assert fusing_current(**kwargs)["ok"] is False


# ---- IPC-2221 spacing --------------------------------------------------

@pytest.mark.parametrize("voltage,env,mm", [
    (12, "B1", 0.05),
    (30, "B1", 0.05),
    (31, "B1", 0.10),
    (48, "B2", 0.60),
    (100, "B3", 1.50),
    (150, "B4", 0.40),
    (250, "A5", 0.40),
    (500, "A6", 1.50),
])
def test_the_table_rows_are_what_the_published_table_says(voltage, env, mm):
    assert ipc2221_spacing(voltage, env)["spacing_mm"] == pytest.approx(mm)


def test_a_band_is_upper_inclusive():
    """15 V is in the first band and 16 V is in the second; an
    off-by-one here returns a spacing one row too small."""
    assert ipc2221_spacing(15, "A6")["spacing_mm"] == 0.13
    assert ipc2221_spacing(16, "A6")["spacing_mm"] == 0.25


def test_above_500_volts_adds_the_per_volt_slope():
    """1000 V external uncoated: 2.5 mm plus 500 V at 0.005 mm/V = 5 mm."""
    result = ipc2221_spacing(1000, "B2")
    assert result["spacing_mm"] == pytest.approx(5.0)
    assert "per volt" in result["band"]


def test_a_disputed_cell_reports_both_figures():
    """Two reproductions of Table 6-1 disagree here. The larger was
    taken, because a wider spacing is never the unsafe mistake, and the
    disagreement is surfaced instead of being decided silently."""
    result = ipc2221_spacing(280, "B2")
    assert result["spacing_mm"] == 2.5
    assert result["disputed"]["other_source_says"] == 1.25
    assert "never the unsafe mistake" in result["disputed"]["why"]


def test_an_undisputed_cell_carries_no_dispute_marker():
    assert "disputed" not in ipc2221_spacing(12, "B1")


def test_the_result_says_the_voltage_is_between_conductors():
    """The commonest way this table is misread: two opposite-polarity
    400 V rails are an 800 V problem, not a 400 V one."""
    assert "between CONDUCTORS" in ipc2221_spacing(100, "B1")["note"]


def test_every_environment_is_documented_and_reachable():
    for env in IPC2221_ENVIRONMENTS:
        result = ipc2221_spacing(100, env)
        assert result["ok"], env
        assert result["environment_meaning"]


def test_an_unknown_environment_lists_the_real_ones():
    result = ipc2221_spacing(100, "Z9")
    assert result["ok"] is False
    assert "B1" in result["reason"] and "internal" in result["reason"]


def test_the_result_names_its_ipc_revision():
    """IPC-2221C REDEFINES these columns rather than renumbering them:
    B4 becomes solder-mask covered, a new B5 appears, the A-series
    shifts and the above-500 V step changes. So a class letter does not
    carry across revisions, and reading "B4" here against a Rev C
    document silently selects a different environment. The reply says
    which revision it is and tells the reader to match on the
    description instead."""
    result = ipc2221_spacing(100, "B4")
    assert "A/B" in result["revision"]
    assert "environment_meaning" in result["revision"]
    assert result["environment_meaning"]


def test_the_source_admits_it_is_a_secondary_transcription():
    """A safety table nobody can check is worse than one that says so."""
    assert "secondary" in ipc2221_spacing(100, "B1")["source"]


# ---- via parasitics ----------------------------------------------------

def test_a_typical_via_lands_in_the_range_everyone_quotes():
    """0.3 mm drill through 1.6 mm FR4: about 1 nH and well under a
    picofarad is the figure every signal-integrity text gives."""
    result = via_parasitics(11.8, 63.0, pad_mils=23.6, antipad_mils=39.4)
    assert 0.8 < result["inductance_nh"] < 2.0
    assert 0.2 < result["capacitance_pf"] < 1.0
    assert 0.5 < result["barrel_resistance_mohm"] < 3.0


def test_a_longer_via_is_more_inductive():
    short = via_parasitics(12, 31)["inductance_nh"]
    long = via_parasitics(12, 63)["inductance_nh"]
    assert long > short


def test_an_antipad_no_larger_than_the_pad_is_explained_not_divided_by():
    """The published capacitance form divides by (antipad - pad)."""
    result = via_parasitics(12, 63, pad_mils=24, antipad_mils=24)
    assert result["ok"] is True
    assert "capacitance_pf" not in result
    assert "must be LARGER" in result["capacitance_note"]


def test_via_parasitics_are_labelled_as_estimates():
    result = via_parasitics(12, 63)
    assert result["status"] == "estimate"
    assert "return path" in result["warning"]


# ---- spiral inductor ---------------------------------------------------

def test_a_square_spiral_is_the_most_inductive_layout():
    """It encloses the most area per turn, and Mohan's coefficients say
    so at every fill ratio."""
    for inner in (50, 200, 350):
        kw = dict(turns=10, outer_diameter_mils=400,
                  inner_diameter_mils=inner)
        square = spiral_inductor(shape="square", **kw)["inductance_nh"]
        others = [spiral_inductor(shape=s, **kw)["inductance_nh"]
                  for s in ("hexagonal", "octagonal")]
        assert square > max(others), inner


def test_hexagonal_and_octagonal_cross_over_with_fill_ratio():
    """Not a quirk of the implementation, a property of the published
    coefficients: hex is 2.33/(1+3.82r) and oct is 2.25/(1+3.55r), which
    are equal at r = 0.247 and swap either side of it.

    Pinned because it looks like a bug when you meet it, and the
    temptation is to "fix" it by reordering the table -- which would
    silently change every octagonal result.
    """
    def pair(inner):
        kw = dict(turns=10, outer_diameter_mils=400,
                  inner_diameter_mils=inner)
        return (spiral_inductor(shape="hexagonal", **kw)["inductance_nh"],
                spiral_inductor(shape="octagonal", **kw)["inductance_nh"])

    # r = 0.11, below the crossover: hexagonal wins.
    hexagonal, octagonal = pair(320)
    assert hexagonal > octagonal
    # r = 0.33, above it: octagonal wins.
    hexagonal, octagonal = pair(200)
    assert octagonal > hexagonal


def test_inductance_goes_as_the_square_of_the_turns():
    one = spiral_inductor(5, 400, 200)["inductance_nh"]
    two = spiral_inductor(10, 400, 200)["inductance_nh"]
    assert two == pytest.approx(one * 4.0, rel=0.001)


def test_an_inner_diameter_larger_than_the_outer_is_refused():
    result = spiral_inductor(10, 200, 400)
    assert result["ok"] is False
    assert "smaller than" in result["reason"]


def test_an_unknown_spiral_shape_lists_the_real_ones():
    result = spiral_inductor(10, 400, 200, shape="triangle")
    assert result["ok"] is False
    assert "square" in result["reason"]


# ---- reactance, resonance, wavelength ----------------------------------

def test_reactance_of_100nf_at_1mhz():
    """1/(2*pi*1e6*1e-7) = 1.5915 ohm."""
    result = reactance(1e6, capacitance_f=100e-9)
    assert result["capacitive_reactance_ohm"] == pytest.approx(1.5915,
                                                               rel=0.001)


def test_reactance_of_1uh_at_1mhz():
    """2*pi*1e6*1e-6 = 6.2832 ohm."""
    assert reactance(1e6, inductance_h=1e-6)[
        "inductive_reactance_ohm"] == pytest.approx(6.2832, rel=0.001)


def test_reactances_cross_at_resonance():
    """The definition of resonance, so the two functions must agree."""
    f = resonant_frequency(1e-6, 100e-9)["resonant_frequency_hz"]
    both = reactance(f, capacitance_f=100e-9, inductance_h=1e-6)
    assert both["capacitive_reactance_ohm"] == pytest.approx(
        both["inductive_reactance_ohm"], rel=1e-6)


def test_resonance_of_1uh_and_100nf():
    """1/(2*pi*sqrt(1e-13)) = 503.3 kHz."""
    assert resonant_frequency(1e-6, 100e-9)[
        "resonant_frequency_hz"] == pytest.approx(503292, rel=0.001)


def test_reactance_needs_something_to_work_with():
    assert reactance(1e6)["ok"] is False


def test_wavelength_in_fr4_at_1ghz():
    """c/(f*sqrt(er)) = 3e8/(1e9*2.0736) = 144.6 mm."""
    result = wavelength_in_dielectric(1e9, 4.3)
    assert result["wavelength_mm"] == pytest.approx(144.6, rel=0.002)
    assert result["quarter_wave_mils"] == pytest.approx(
        result["wavelength_mils"] / 4.0)


def test_the_twentieth_wave_rule_is_offered_because_it_is_the_real_question():
    assert "transmission line" in wavelength_in_dielectric(1e9)["note"]


# ---- PDN ---------------------------------------------------------------

def test_pdn_target_for_a_3v3_rail():
    """3.3 V, 5 % ripple, 2 A step: 0.165/2 = 82.5 mohm."""
    result = pdn_target_impedance(3.3, 5.0, 2.0)
    assert result["target_impedance_mohm"] == pytest.approx(82.5)
    assert result["ripple_v"] == pytest.approx(0.165)


def test_a_ripple_of_a_hundred_percent_or_more_is_refused():
    assert pdn_target_impedance(3.3, 100, 2)["ok"] is False


def test_the_pdn_target_says_what_it_assumed():
    assert "ENTIRE ripple budget" in pdn_target_impedance(3.3, 5, 2)[
        "warning"]


# ---- padstack ----------------------------------------------------------

def test_a_pad_is_the_drill_plus_two_rings():
    result = padstack_geometry(12, 6)
    assert result["pad_diameter_mils"] == 24
    assert result["pad_diameter_mm"] == pytest.approx(0.6096)


def test_the_copper_left_between_neighbours_is_reported():
    result = padstack_geometry(12, 6, pitch_mils=50)
    assert result["copper_gap_mils"] == pytest.approx(26.0)


def test_the_diagonal_neighbour_is_reported_because_it_is_forgotten():
    """On a grid array the orthogonal pair clears and the diagonal pair
    is the one nobody checks -- it is further apart, so it is the pair
    that passes when the orthogonal one does not, and reporting both
    stops a designer deriving one from the other by hand."""
    result = padstack_geometry(12, 6, pitch_mils=50)
    assert result["diagonal_gap_mils"] == pytest.approx(
        50 * 2 ** 0.5 - 24, rel=1e-6)


def test_overlapping_pads_are_called_out():
    result = padstack_geometry(40, 10, pitch_mils=50)
    assert result["copper_gap_mils"] < 0
    assert "OVERLAP" in result["warning"]


# ---- the shared contract -----------------------------------------------

_ALL_OK_CALLS = [
    (awg_properties, (24,), {}),
    (fusing_current, (10,), {}),
    (ipc2221_spacing, (100,), {}),
    (padstack_geometry, (12, 6), {}),
    (pdn_target_impedance, (3.3, 5, 2), {}),
    (reactance, (1e6,), {"capacitance_f": 1e-7}),
    (resonant_frequency, (1e-6, 1e-7), {}),
    (spiral_inductor, (10, 400, 200), {}),
    (via_parasitics, (12, 63), {}),
    (wavelength_in_dielectric, (1e9,), {}),
]


@pytest.mark.parametrize("fn,args,kwargs", _ALL_OK_CALLS,
                         ids=lambda v: getattr(v, "__name__", ""))
def test_every_calculator_states_its_confidence(fn, args, kwargs):
    """exact / published / estimate. Presenting a rule of thumb and an
    algebraic identity as the same kind of answer is how the first one
    ends up in a specification."""
    result = fn(*args, **kwargs)
    assert result["ok"] is True
    assert result["status"] in ("exact", "published", "estimate")


@pytest.mark.parametrize("fn,args,kwargs", _ALL_OK_CALLS,
                         ids=lambda v: getattr(v, "__name__", ""))
def test_no_calculator_raises_on_junk(fn, args, kwargs):
    """These are reached from a tool surface, where a traceback helps
    nobody and a refusal with a reason helps immediately.

    -1 and 0 are deliberately NOT in this list: AWG counts down through
    zero into the 0/00/000 gauges, so -1 is a real wire there while it
    is nonsense everywhere else. A shared junk list that included it
    would be asserting a rule one of these functions should not follow.
    """
    for junk in (None, "x", True, float("nan"), float("inf"), []):
        result = fn(*([junk] * len(args)), **{k: junk for k in kwargs})
        assert isinstance(result, dict)
        assert result.get("ok") is False, (fn.__name__, junk)
        assert result.get("reason")


@pytest.mark.parametrize("fn,args,kwargs", [
    c for c in _ALL_OK_CALLS if c[0] is not awg_properties],
    ids=lambda v: getattr(v, "__name__", ""))
def test_a_zero_or_negative_dimension_is_refused(fn, args, kwargs):
    """Everything but AWG measures a physical size, and none of them has
    a meaningful answer at or below zero."""
    for junk in (0, -1):
        result = fn(*([junk] * len(args)), **{k: junk for k in kwargs})
        assert result.get("ok") is False, (fn.__name__, junk)
