# SPDX-License-Identifier: Apache-2.0
"""A rational S-parameter model as a SPICE subcircuit.

Neither LTspice nor ngspice reads a Touchstone file in a transient run.
A pole-residue model (:mod:`.vectfit`) is turned here into a subcircuit
of resistors, capacitors and linear controlled sources only, which every
SPICE accepts and simulates exactly as the model says.

With waves scaled as voltages, a = (v + z0 i) / 2 and b = (v - z0 i) / 2,
each port j is a resistor z0 from the port to a voltage source of value
2 b_j; then v - z0 i = 2 b holds, and a_j = v_j - b_j. The response
b = S(s) a is a state-space system:

* a real pole p of input j is a node x with a capacitor k to the
  reference, a conductance -p k and a current a_j k into it, so
  dx/dt = p x + a_j;
* a pole pair sigma +/- j omega is two such nodes with the block
  [[sigma, omega], [-omega, sigma]] and input [2, 0] (real form);
* b_i = sum_j D_ij a_j + sum over input j's states of their real
  residue coefficients, summed as currents into a 1 ohm node.

``k`` scales the capacitors to picofarads so the element values stay
of order one for a solver that works in seconds.
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from .vectfit import Model

K_CAP = 1e-12


def _g(name: str, node: str, ctrl_p: str, ctrl_n: str, gain: float) -> str:
    """Current ``gain * v(ctrl_p, ctrl_n)`` flowing into ``node`` from 0."""
    # SPICE: G n+ n- c+ c- g puts current g*v(c) from n+ to n- through the
    # source, i.e. out of n+; "0 node" makes it flow into node.
    return f"{name} 0 {node} {ctrl_p} {ctrl_n} {gain:.12g}"


def subcircuit(model: Model, name: str = "channel", z0: float = 50.0,
               port_names: Optional[Sequence[str]] = None, title: str = "") -> str:
    P = model.ports
    ports = list(port_names or [f"p{i + 1}" for i in range(P)])
    if len(ports) != P:
        raise ValueError(f"{P} port names needed")
    ref = "ref"
    lines = [f"* {title or name}: {P}-port rational macromodel, {len(model.poles)} poles",
             f"* reference impedance {z0:g} ohm; ports: {' '.join(ports)}, reference node {ref}",
             f".subckt {name} {' '.join(ports)} {ref}"]
    n = 0

    def nm(prefix: str) -> str:
        nonlocal n
        n += 1
        return f"{prefix}{n}"

    # port j: z0 from the port to e_j, e_j = 2 b_j (VCVS of the sum node w_j)
    for j, p in enumerate(ports):
        lines.append(f"R{nm('p')} {p} e{j} {z0:.12g}")
        lines.append(f"E{nm('e')} e{j} {ref} w{j} {ref} 1")
        lines.append(f"R{nm('w')} w{j} {ref} 1")
    # a_j = v_j - b_j = v(p_j) - 0.5 v(w_j): every use of a_j is two sources
    k = K_CAP

    def from_a(prefix: str, node: str, j: int, gain: float) -> list[str]:
        return [_g(nm(prefix), node, ports[j], ref, gain),
                _g(nm(prefix), node, f"w{j}", ref, -0.5 * gain)]

    # states, per input j
    states: list[tuple[int, str, int, float, float]] = []   # (j, node, pole index, re, im part)
    for j in range(P):
        i = 0
        while i < len(model.poles):
            p = model.poles[i]
            if p.imag == 0:
                x = f"x{j}_{i}"
                lines.append(f"C{nm('c')} {x} {ref} {k:.6g}")
                lines.append(f"R{nm('r')} {x} {ref} {1 / (-p.real * k):.12g}")
                lines += from_a("G", x, j, k)
                states.append((j, x, i, 1.0, 0.0))
                i += 1
            else:
                x1, x2 = f"x{j}_{i}a", f"x{j}_{i}b"
                sg, om = p.real, p.imag
                for x in (x1, x2):
                    lines.append(f"C{nm('c')} {x} {ref} {k:.6g}")
                    lines.append(f"R{nm('r')} {x} {ref} {1 / (-sg * k):.12g}")
                lines.append(_g(nm("G"), x1, x2, ref, om * k))
                lines.append(_g(nm("G"), x2, x1, ref, -om * k))
                lines += from_a("G", x1, j, 2 * k)
                states.append((j, x1, i, 1.0, 0.0))
                states.append((j, x2, i, 0.0, 1.0))
                i += 2
    # outputs: current into w_i (1 ohm) = 2 b_i
    for i in range(P):
        for j in range(P):
            dij = float(model.d[i, j])
            if dij:
                lines += from_a("G", f"w{i}", j, 2 * dij)
        for j, x, pi, cr, ci in states:
            r = model.residues[pi][i, j]
            coef = r.real if cr else r.imag
            if coef:
                lines.append(_g(nm("G"), f"w{i}", x, ref, 2 * coef))
    lines.append(f".ends {name}")
    return "\n".join(lines) + "\n"


def ac_check_deck(model: Model, sub: str, f: Sequence[float], z0: float = 50.0,
                  drive: int = 0, name: str = "channel") -> str:
    """An AC deck that drives port ``drive`` through z0 with an incident
    wave of 1 V and terminates the others: v(p_k) gives S_k,drive (minus
    1 at the driven port)."""
    P = model.ports
    f = np.asarray(f, dtype=float)
    lines = ["* S-parameter check of the macromodel", sub.strip()]
    for k in range(P):
        if k == drive:
            lines.append(f"V1 s{k} 0 AC 2")
            lines.append(f"Rs{k} s{k} n{k} {z0:.12g}")
        else:
            lines.append(f"Rt{k} n{k} 0 {z0:.12g}")
    lines.append(f"X1 {' '.join(f'n{k}' for k in range(P))} 0 {name}")
    pts = len(f)
    lines.append(f".ac lin {pts} {f[0]:.12g} {f[-1]:.12g}")
    lines.append(".end")
    return "\n".join(lines) + "\n"


def element_count(sub: str) -> int:
    return sum(1 for line in sub.splitlines() if line and line[0] in "RCEG")


__all__ = ["subcircuit", "ac_check_deck", "element_count", "K_CAP"]
