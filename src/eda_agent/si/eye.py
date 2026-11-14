# SPDX-License-Identifier: Apache-2.0
"""Eye diagrams of a channel: by convolution and by worst-case analysis.

The channel is linear, so the received waveform of any bit pattern is a
sum of shifted pulse responses. The pulse response comes from the
channel's transfer function (S21, or SDD21 for a pair) driven by one
unit interval of the transmitter's edge: a linear ramp of the rise time,
the shape a SPICE PWL source makes, so the two can be compared sample
for sample.

Two eyes come out of one pulse response:

* the **simulated eye** of a PRBS pattern, folded onto one unit interval;
* the **worst-case eye** by peak distortion analysis: at each sampling
  phase the main cursor less the sum of every other cursor's magnitude
  (J. Casper et al., "An accurate and efficient analysis method for
  multi-Gb/s chip-to-chip signaling schemes", VLSI Circuits 2002). No
  pattern can close the eye further, so this is the guaranteed opening.

Amplitudes are the transmitter's incident swing into a matched load: a
single line driven with +/- A/2 receives S21 * A/2; a pair driven with
+/- A/2 on each side receives SDD21 * A differentially.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

#: Feedback taps of the maximal-length PRBS generators (ITU-T O.150).
PRBS_TAPS = {7: (7, 6), 9: (9, 5), 11: (11, 9), 15: (15, 14), 23: (23, 18), 31: (31, 28)}


def prbs(order: int, n_bits: Optional[int] = None) -> np.ndarray:
    """PRBS bits (0/1) from the standard Fibonacci LFSR, seed all ones."""
    if order not in PRBS_TAPS:
        raise ValueError(f"PRBS order is one of {sorted(PRBS_TAPS)}")
    a, b = PRBS_TAPS[order]
    n = n_bits if n_bits is not None else (1 << order) - 1
    state = (1 << order) - 1
    out = np.empty(n, dtype=np.int8)
    for i in range(n):
        bit = ((state >> (a - 1)) ^ (state >> (b - 1))) & 1
        state = ((state << 1) | bit) & ((1 << order) - 1)
        out[i] = bit
    return out


def ramp_spectrum(f: np.ndarray, rise: float) -> np.ndarray:
    """Spectrum of a 0->1 linear ramp of duration rise / 0.8 (10-90 % =
    ``rise``), divided by that of an ideal step: sinc and its delay."""
    tr = rise / 0.8
    x = np.pi * f * tr
    return np.sinc(f * tr) * np.exp(-1j * x)


def step_response(f: np.ndarray, h: np.ndarray, rise: float, pad: int = 8,
                  t_pre: Optional[float] = None) -> tuple[np.ndarray, np.ndarray]:
    """Response to a linear-ramp step starting at t = 0, from a transfer
    function sampled at f = df .. N df. The band edge is tapered (the top
    tenth by a half cosine) so its truncation does not ring."""
    N = len(f)
    df = f[0]
    if not np.allclose(np.diff(f), df, rtol=1e-6, atol=df * 1e-6):
        raise ValueError("a step response needs the uniform band df, 2 df, ... N df")
    t_pre = t_pre if t_pre is not None else 4.0 / f[-1]
    ff = np.concatenate([[0.0], f])
    H = np.concatenate([[h[0].real], h]) * ramp_spectrum(ff, rise)
    taper = np.ones_like(ff)
    edge = ff > 0.9 * ff[-1]
    taper[edge] = 0.5 * (1 + np.cos(np.pi * (ff[edge] - 0.9 * ff[-1]) / (0.1 * ff[-1])))
    H = H * taper * np.exp(-2j * np.pi * ff * t_pre)
    n = 2 * N * pad
    step = np.cumsum(np.fft.irfft(H, n))
    t = (np.arange(n) + 0.5) / (n * df) - t_pre
    keep = t < 0.95 / df - t_pre
    return t[keep], step[keep]


@dataclass
class Pulse:
    t: np.ndarray            # sample times (s), uniform
    p: np.ndarray            # response to one UI of +1 (incident units)
    ui: float
    spui: int                # samples per UI

    @property
    def peak_index(self) -> int:
        return int(np.argmax(np.abs(self.p)))

    @property
    def center_index(self) -> int:
        """Middle of the span where the pulse is over half its peak: the
        sampling reference. On a flat-topped pulse the peak sample can sit
        at either end of the top, which would put the eye's window off
        centre."""
        pk = self.peak_index
        half = 0.5 * abs(self.p[pk])
        a = pk
        while a > 0 and abs(self.p[a - 1]) >= half:
            a -= 1
        b = pk
        while b < len(self.p) - 1 and abs(self.p[b + 1]) >= half:
            b += 1
        return (a + b) // 2


#: A pulse response needs the sweep to cover this many unit intervals.
MIN_WINDOW_UI = 3.0
#: ... and the step to move less than this part of its final value over the
#: last 15 % of that time.
UNSETTLED = 0.05


def pulse_response(f: np.ndarray, h: np.ndarray, ui: float, rise: float,
                   spui: int = 64) -> Pulse:
    t, step = step_response(f, h, rise)
    # the sweep's step df sets how long a time the response covers (1/df).
    # A bit that does not fit, or a response still ringing at the end of
    # that time, wraps round onto itself: the eye would be an artefact.
    window = t[-1] - t[0]
    if window < MIN_WINDOW_UI * ui:
        raise ValueError(
            f"the channel's frequency step ({f[0] / 1e6:.4g} MHz) covers {window * 1e9:.3g} ns, "
            f"and one bit lasts {ui * 1e9:.3g} ns: sweep the channel with a step of "
            f"{1e-6 / (MIN_WINDOW_UI * ui):.3g} MHz or finer (more points, or a lower f_max)")
    tail = step[t > t[-1] - 0.15 * window]
    final = abs(float(tail[-1]))
    if final > 0 and float(tail.max() - tail.min()) > UNSETTLED * final:
        raise ValueError(
            f"the channel is still ringing after {window * 1e9:.3g} ns, all the time its "
            f"frequency step ({f[0] / 1e6:.4g} MHz) covers: sweep it with a finer step (more "
            "points, or a lower f_max), or terminate it")
    dt = ui / spui
    tt = np.arange(t[0], t[-1] - ui, dt)
    s = np.interp(tt, t, step)
    s_late = np.interp(tt - ui, t, step, left=0.0)
    return Pulse(tt, s - s_late, ui, spui)


def waveform(pulse: Pulse, bits: Sequence[int], amplitude: float) -> tuple[np.ndarray, np.ndarray]:
    """Received waveform of NRZ ``bits`` (+/- amplitude/2 incident)."""
    sym = (2 * np.asarray(bits, dtype=float) - 1) * amplitude / 2
    up = np.zeros(len(sym) * pulse.spui)
    up[::pulse.spui] = sym
    v = np.convolve(up, pulse.p)[: len(up)]
    t = pulse.t[0] + np.arange(len(v)) * (pulse.ui / pulse.spui)
    return t, v


def fold(t: np.ndarray, v: np.ndarray, ui: float, spui: int, skip_bits: int, phase0: int
         ) -> np.ndarray:
    """(bits, spui) array of the waveform cut into unit intervals."""
    start = skip_bits * spui + phase0
    usable = (len(v) - start) // spui
    return v[start:start + usable * spui].reshape(usable, spui)


def eye_metrics(eye: np.ndarray, bits: np.ndarray, ui: float, threshold: float = 0.0
                ) -> dict[str, Any]:
    """Height and width of a folded eye whose rows are labelled by the
    bit sent in that interval."""
    ones, zeros = eye[bits == 1], eye[bits == 0]
    if not len(ones) or not len(zeros):
        raise ValueError("the pattern needs both ones and zeros")
    low_ones = ones.min(axis=0)
    high_zeros = zeros.max(axis=0)
    height = low_ones - high_zeros
    k = int(np.argmax(height))
    open_ = height > 0
    width = 0
    if open_[k]:
        i = k
        while open_[(i - 1) % len(open_)] and width < len(open_):
            i -= 1
            width += 1
        j = k
        while open_[(j + 1) % len(open_)] and width < len(open_):
            j += 1
            width += 1
        width += 1
    spui = eye.shape[1]
    return {"eye_height": float(max(height[k], 0.0)), "eye_width_ui": width / spui,
            "eye_width": width / spui * ui, "phase": k,
            "level_one": float(np.mean(ones[:, k])), "level_zero": float(np.mean(zeros[:, k]))}


def simulated_eye(pulse: Pulse, amplitude: float, order: int = 7, repeats: int = 2
                  ) -> dict[str, Any]:
    pattern = prbs(order)
    bits = np.tile(pattern, repeats + 1)
    t, v = waveform(pulse, bits, amplitude)
    span = len(pulse.p) // pulse.spui + 1
    skip = min(span, len(pattern))
    # sampling phase reference: the middle of the pulse
    c = pulse.center_index
    phase0 = (c - pulse.spui // 2) % pulse.spui
    shift = (c - pulse.spui // 2) // pulse.spui
    eye = fold(t, v, pulse.ui, pulse.spui, skip + shift, phase0)
    labels = bits[skip: skip + len(eye)]
    m = eye_metrics(eye, labels, pulse.ui)
    m.update(eye=eye, bits=labels, phase0=phase0, start_bits=skip + shift)
    return m


def worst_case_eye(pulse: Pulse, amplitude: float) -> dict[str, Any]:
    """Peak distortion analysis over every sampling phase of the UI."""
    spui = pulse.spui
    p = pulse.p * amplitude / 2
    pk = pulse.peak_index
    heights = np.empty(spui)
    for ph in range(spui):
        c = pk - spui // 2 + ph
        idx = np.arange(c % spui, len(p), spui)
        main = p[c] if 0 <= c < len(p) else 0.0
        isi = np.sum(np.abs(p[idx])) - abs(main)
        heights[ph] = 2 * (main - isi)
    k = int(np.argmax(heights))
    open_ = heights > 0
    width = int(np.sum(open_)) if open_[k] else 0
    return {"eye_height": float(max(heights[k], 0.0)), "eye_width_ui": width / spui,
            "eye_width": width / spui * pulse.ui, "main_cursor": float(p[pk]),
            "isi_sum": float(np.sum(np.abs(p[np.arange(pk % spui, len(p), spui)])) - abs(p[pk]))}


def plot_eye(path, eye: np.ndarray, ui: float, title: str, extra: Optional[np.ndarray] = None
             ) -> bool:
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except Exception:  # noqa: BLE001 - optional extra
        return False
    fig = Figure(figsize=(7, 4.5), dpi=110)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    spui = eye.shape[1]
    t = np.arange(2 * spui) / spui * ui * 1e12
    rows = np.hstack([eye[:-1], eye[1:]])
    for r in rows[:: max(1, len(rows) // 400)]:
        ax.plot(t, r * 1e3, color="C0", alpha=0.15, lw=0.8)
    if extra is not None:
        rows2 = np.hstack([extra[:-1], extra[1:]])
        for r in rows2[:: max(1, len(rows2) // 400)]:
            ax.plot(t, r * 1e3, color="C1", alpha=0.12, lw=0.8)
    ax.set_xlabel("Time (ps)")
    ax.set_ylabel("mV")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path)
    return True


def terminated_transfer(s: np.ndarray, f: np.ndarray, z0: float, rs: float, cs: float,
                        rl: Optional[float], cl: float) -> np.ndarray:
    """The channel's transfer with the real source and load.

    Port k is closed by a Norton source: current ``V_s / R_s`` in parallel
    with ``1/R_s + j w C_s`` at the near ports, ``1/R_L + j w C_L`` (no
    resistor when ``rl`` is None: a high-impedance receiver) at the far
    ports. With waves of reference ``z0``, v = a + b and the current into
    the port (a - b)/z0, so (G+ - G- S) a = I_n with G+/- = 1/z0 +/- Y_t,
    and v = (I + S) a; no inversion of I + S is needed.

    The result is normalised so that it equals S21 (SDD21 for a pair)
    when ``rs`` and ``rl`` are ``z0`` and both capacitances are zero:
    V_far / (V_s / 2) for a line, (V3 - V4) / ((Vp - Vn) / 2) for a pair
    driven by +V_s/2 and -V_s/2.
    """
    n, P, _ = s.shape
    if P not in (2, 4):
        raise ValueError("a terminated transfer needs 2 or 4 ports")
    w = 2 * np.pi * np.asarray(f, dtype=float)
    near = [0] if P == 2 else [0, 1]
    far = [1] if P == 2 else [2, 3]
    drive = np.array([1.0] if P == 2 else [0.5, -0.5])
    out = np.empty(n, dtype=complex)
    eye_p = np.eye(P)
    for k in range(n):
        yt = np.zeros(P, dtype=complex)
        for i in near:
            yt[i] = 1 / rs + 1j * w[k] * cs
        for i in far:
            yt[i] = (0.0 if rl is None else 1 / rl) + 1j * w[k] * cl
        inj = np.zeros(P, dtype=complex)
        for i, d in zip(near, drive):
            inj[i] = d / rs                      # V_s = 1
        gp = np.diag(1 / z0 + yt)
        gm = np.diag(1 / z0 - yt)
        a = np.linalg.solve(gp - gm @ s[k], inj)
        v = (eye_p + s[k]) @ a
        out[k] = (v[1] if P == 2 else v[2] - v[3]) / 0.5
    return out


__all__ = ["prbs", "ramp_spectrum", "step_response", "Pulse", "pulse_response", "waveform",
           "fold", "eye_metrics", "simulated_eye", "worst_case_eye", "plot_eye", "terminated_transfer"]
