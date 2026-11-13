# SPDX-License-Identifier: Apache-2.0
"""The kinds of simulation job, and what each takes.

A kind is the problem; engines are ways to solve it. Two engines that
accept the same kind give comparable results under the same metric
names.
"""
from __future__ import annotations

from typing import Any

from ..si.jobs import LINE_SPEC, PLAN_SPEC
from ..si.eye_jobs import EYE_SPEC
from ..si.net_jobs import NET_SPEC
from .engine import JobKind

KINDS: dict[str, JobKind] = {k.id: k for k in (
    JobKind(
        "touchstone-check", "S-parameter quality",
        "Passivity, reciprocity and the main figures of a Touchstone file "
        "(.s1p to .sNp): is the data physical enough to simulate with?",
        inputs={"touchstone": "the .sNp file"},
        spec={"tolerance": ("number", False,
                            "allowed excess over 1 for passivity and over 0 for "
                            "reciprocity (default 1e-3)")}),
    JobKind(
        "xsection-2d", "Transmission-line cross-section",
        "Impedance, delay, loss and coupling of a line cross-section with the "
        "2D field solver: any calculator of the PCB toolkit's impedance group.",
        spec={"calculator": ("text", True, "toolkit calculator name, e.g. microstrip, "
                             "edge_coupled_stripline"),
              "inputs": ("object", False, "the calculator's inputs, as the "
                         "pcb_calc_toolkit tool takes them")}),
    JobKind(
        "em-3d", "3D full-wave S-parameters",
        "S-parameters of a few nets of the board in a 3D field solver, with the "
        "real stackup and return copper.",
        inputs={"board": "a saved LayoutBoard (.json)",
                "stackup": "a saved pcb_get_layer_stackup reply (.json), optional"},
        spec={"nets": ("list", True, "the nets to model"),
              "ports": ("list", False, "pads as DESIGNATOR.PIN"),
              "f_max_mhz": ("number", False, "top of the band (default 6000)"),
              "f_min_mhz": ("number", False, "bottom of the band (default 0)"),
              "margin_mils": ("number", False, "box margin round the nets"),
              "port_ohms": ("number", False, "port impedance (default 50)"),
              "max_cells_m": ("number", False, "refuse models above this many "
                              "million cells (default 20)"),
              "threads": ("number", False, "solver threads; 0 lets it choose")}),
    JobKind(
        "line-model", "Transmission line, wideband",
        "Frequency-dependent RLGC of one trace or coupled traces (eddy-current R and L, "
        "causal dielectrics, roughness), their impedances, delay and loss, and the "
        "S-parameters of a length of line.",
        inputs={"stackup": "a saved pcb_get_layer_stackup reply (.json), optional: "
                           "with spec.layer it sets the cross-section"},
        spec=LINE_SPEC),
    JobKind(
        "stackup-plan", "Stackup impedance plan",
        "For every signal layer of a stackup, the trace width for a single-ended "
        "target and the pair geometry for differential targets, from the field "
        "solver; optionally compared with Altium's impedance profiles.",
        inputs={"stackup": "a saved pcb_get_layer_stackup reply (.json)"},
        spec=PLAN_SPEC),
    JobKind(
        "net-channel", "Routed net or pair, end to end",
        "The routed path of a net or differential pair on the board as one channel: "
        "each trace section from its own cross-section, the vias with their stubs and "
        "return path, the stubs off the path; S-parameters, TDR, delay, loss and skew.",
        inputs={"board": "a saved LayoutBoard (.json), e.g. from si_board_snapshot",
                "stackup": "the board's pcb_get_layer_stackup reply (.json)"},
        spec=NET_SPEC),
    JobKind(
        "channel-eye", "Eye diagram of a channel",
        "The eye of a PRBS pattern through a channel's S-parameters (a 2-port line or a "
        "4-port pair), its height and width, and the worst-case eye; on SPICE also the "
        "channel as a passive subcircuit with an LTspice symbol.",
        inputs={"touchstone": "the channel's .s2p or .s4p (e.g. from a net-channel job)"},
        spec=EYE_SPEC),
)}


def kind(kind_id: str) -> JobKind:
    try:
        return KINDS[kind_id]
    except KeyError:
        raise KeyError(f"unknown job kind {kind_id!r}; known: {', '.join(sorted(KINDS))}") from None


_TYPES = {"number": (int, float), "text": (str,), "list": (list, tuple),
          "object": (dict,), "bool": (bool,)}


def check_spec(kind_id: str, spec: dict[str, Any], inputs: dict[str, str]) -> list[str]:
    """Problems with a spec against its kind: unknown or missing keys,
    wrong types, missing input files. Unknown keys are errors so that a
    misspelt option is not silently ignored."""
    k = kind(kind_id)
    problems = []
    for key in spec:
        if key not in k.spec:
            problems.append(f"unknown spec key {key!r} for {kind_id}; "
                            f"known: {', '.join(sorted(k.spec)) or 'none'}")
    for key, (type_name, required, _help) in k.spec.items():
        if key not in spec:
            if required:
                problems.append(f"spec key {key!r} is required for {kind_id}")
            continue
        v = spec[key]
        ok = isinstance(v, _TYPES[type_name])
        if type_name == "number" and isinstance(v, bool):
            ok = False
        if not ok:
            problems.append(f"spec key {key!r} must be a {type_name}")
    for role in inputs:
        if role not in k.inputs:
            problems.append(f"unknown input {role!r} for {kind_id}; "
                            f"known: {', '.join(sorted(k.inputs)) or 'none'}")
    for role, what in k.inputs.items():
        if role not in inputs and "optional" not in what:
            problems.append(f"input {role!r} ({what}) is required for {kind_id}")
    return problems
