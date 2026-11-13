# SPDX-License-Identifier: Apache-2.0
"""The same eye in a SPICE transient: ngspice or LTspice.

The channel's macromodel (:mod:`.vectfit`, :mod:`.spice_export`) is
driven by PWL sources that play the PRBS pattern with linear edges of
the given rise time, through the reference impedance, and loaded by it
at the far end. The received waveform is resampled on the convolution's
grid and folded the same way, so the two eyes are directly comparable:
two independent routes (an FFT convolution here, a circuit simulator
there) from one channel to one eye.

A driver's own model (an IBIS buffer, a vendor SPICE model) can replace
the PWL source in the same deck; that is the point of having the
channel as a subcircuit.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from ..sim import deck_runner
from . import eye as _eye
from .spice_export import subcircuit
from .vectfit import Model


def pwl(bits: Sequence[int], ui: float, rise: float, high: float, low: float) -> str:
    """PWL points of NRZ ``bits``: 0 before, linear edges of 10-90 % rise."""
    tr = rise / 0.8
    pts = [(0.0, 0.0)]
    level = 0.0
    for k, b in enumerate(bits):
        new = high if b else low
        t = k * ui
        if new != level or k == 0:
            pts.append((t, level))
            pts.append((t + tr, new))
            level = new
    pts.append((len(bits) * ui + tr, level))
    # PWL needs strictly increasing times
    out, last = [], -1.0
    for t, v in pts:
        if t <= last:
            t = last + 1e-15
        out.append(f"{t:.6e} {v:.6g}")
        last = t
    return " ".join(out)


def deck(model: Model, bits: Sequence[int], ui: float, rise: float, amplitude: float,
         z0: float, pair: bool, engine: str, dt: float, t_stop: float,
         term: Optional[dict[str, Any]] = None) -> str:
    """The transient deck. ``term`` (from :func:`.eye_jobs.terminations`)
    gives the source resistance ``rs`` and pad capacitance ``cs`` per side
    and the load ``rl`` (None: open) and ``cl``; the default is z0 at both
    ends."""
    term = term or {}
    rs = float(term.get("rs", z0))
    cs = float(term.get("cs", 0.0))
    rl = term.get("rl", z0)
    cl = float(term.get("cl", 0.0))

    def load(node: str, tag: str) -> list[str]:
        # an open receiver still gets a 1 Tohm path so every node has DC to ground
        out = [f"Rl{tag} {node} 0 {1e12 if rl is None else float(rl):.12g}"]
        if cl:
            out.append(f"Cl{tag} {node} 0 {cl:.6g}")
        return out

    def source_cap(node: str, tag: str) -> list[str]:
        return [f"Cs{tag} {node} 0 {cs:.6g}"] if cs else []

    sub = subcircuit(model, "chan", z0)
    P = model.ports
    lines = ["* channel eye: PRBS through the macromodel", sub.strip()]
    if pair:
        if P != 4:
            raise ValueError("a pair needs a 4-port model")
        # incident per side +/- A/4 (differential +/- A/2): V = 2 a
        lines.append(f"Vp sp 0 PWL({pwl(bits, ui, rise, amplitude / 2, -amplitude / 2)})")
        lines.append(f"Vn sn 0 PWL({pwl(bits, ui, rise, -amplitude / 2, amplitude / 2)})")
        lines += [f"Rsp sp n1 {rs:.12g}", f"Rsn sn n2 {rs:.12g}",
                  *source_cap("n1", "p"), *source_cap("n2", "n"),
                  *load("n3", "p"), *load("n4", "n"), "X1 n1 n2 n3 n4 0 chan"]
    else:
        if P != 2:
            raise ValueError("a single line needs a 2-port model")
        # incident +/- A/2: V = 2 a = +/- A
        lines.append(f"Vs s 0 PWL({pwl(bits, ui, rise, amplitude, -amplitude)})")
        lines += [f"Rs s n1 {rs:.12g}", *source_cap("n1", ""), *load("n2", ""),
                  "X1 n1 n2 0 chan"]
    lines.append(f".tran {dt:.6e} {t_stop:.6e} 0 {dt:.6e}")
    if engine == "ltspice":
        lines.append(".options plotwinsize=0 numdgt=15")
    lines.append(".end")
    return "\n".join(lines) + "\n"


def run(model: Model, pulse: _eye.Pulse, bits: np.ndarray, rise: float, amplitude: float,
        z0: float, pair: bool, workspace: Path, engine: str = "auto",
        timeout_s: float = 900, dt_divisor: int = 4,
        term: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Run the deck; the received waveform on the pulse's time grid. The
    simulator steps ``dt_divisor`` times finer than that grid (at 4 its
    trapezoidal error is well under 1 mV on 800 mV)."""
    choice = deck_runner.pick_simulator(engine)
    if not choice.get("engine"):
        return {"ok": False, **choice}
    ui, spui = pulse.ui, pulse.spui
    dt = ui / spui
    t_stop = len(bits) * ui + pulse.t[-1]
    text = deck(model, bits, ui, rise, amplitude, z0, pair, choice["engine"],
                dt / max(int(dt_divisor), 1), t_stop, term)
    res = deck_runner.simulate(text, Path(workspace), choice["engine"], timeout_s)
    out: dict[str, Any] = {"ok": False, "engine": res.get("engine"),
                           "run_folder": res.get("run_folder"),
                           "errors": res.get("errors", []), "reason": res.get("reason", "")}
    folder = Path(res.get("run_folder") or "")
    waves = []
    for raw in sorted(folder.glob("deck*.raw")):
        try:
            waves += deck_runner.parse_ascii_raw(deck_runner.decode_output(raw))
        except (ValueError, IndexError) as exc:
            out["errors"].append(f"{raw.name}: {exc}")
    tran = next((w for w in waves if "tran" in w.title.lower() and w.samples), None)
    if tran is None:
        out["reason"] = out["reason"] or "no transient result came back"
        out["log_tail"] = res.get("log_tail", [])
        return out
    names = [n.lower() for n, _ in tran.names]

    def col(name: str) -> Optional[np.ndarray]:
        for i, n in enumerate(names):
            if n in (f"v({name})", name):
                return np.asarray(tran.samples[i], dtype=float)
        return None

    t = np.abs(np.asarray(tran.samples[0], dtype=float))
    if pair:
        a, b = col("n3"), col("n4")
        v = None if a is None or b is None else a - b
    else:
        v = col("n2")
    if v is None:
        out["reason"] = f"the far-end voltage is not in the result ({', '.join(names[:8])})"
        return out
    grid = np.arange(len(bits) * spui) * dt + pulse.t[0]
    out.update(ok=True, t=grid, v=np.interp(grid, t, v, left=0.0), engine_path=res.get("engine_path"))
    return out


__all__ = ["pwl", "deck", "run"]
