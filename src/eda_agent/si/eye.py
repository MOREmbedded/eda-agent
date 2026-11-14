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


def dc_matrix(s: np.ndarray) -> np.ndarray:
    """The S-matrix at 0 Hz of a channel made of conductors, read back from
    its first points on the band df, 2 df, ...

    At DC such a channel is a small resistance in series between each port
    and its far end, and nothing between the lines. So each transmission
    is real and positive, and its magnitude, which changes slowly and does
    not turn with the channel's delay, is carried back along the line
    through its first two points; each reflection is 1 less the
    transmissions out of its port (r / (r + 2 z0) beside 2 z0 / (r + 2 z0)
    for a series r). Coupling between lines falls with frequency and comes
    out as nothing.

    The real and imaginary parts themselves turn with the delay: on a
    long channel swept with a coarse step the first point is far from DC
    (the cosine of a radian or more), and a step response that takes it
    as DC slopes over its whole window."""
    mag = np.abs(s[0]) if len(s) < 2 else 2.0 * np.abs(s[0]) - np.abs(s[1])
    out = np.clip(np.asarray(mag, dtype=float), 0.0, 1.0)
    if out.ndim == 2:
        np.fill_diagonal(out, 0.0)
        np.fill_diagonal(out, np.clip(1.0 - out.sum(axis=0), 0.0, 1.0))
    return out


def step_response(f: np.ndarray, h: np.ndarray, rise: float, pad: int = 8,
                  t_pre: Optional[float] = None, dc: Optional[float] = None
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Response to a linear-ramp step starting at t = 0, from a transfer
    function sampled at f = df .. N df. The band edge is tapered (the top
    tenth by a half cosine) so its truncation does not ring. ``dc`` is the
    transfer at 0 Hz (default the real part of the first point)."""
    N = len(f)
    df = f[0]
    if not np.allclose(np.diff(f), df, rtol=1e-6, atol=df * 1e-6):
        raise ValueError("a step response needs the uniform band df, 2 df, ... N df")
    t_pre = t_pre if t_pre is not None else 4.0 / f[-1]
    ff = np.concatenate([[0.0], f])
    H = np.concatenate([[h[0].real if dc is None else dc], h]) * ramp_spectrum(ff, rise)
    taper = np.ones_like(ff)
    edge = ff > 0.9 * ff[-1]
    taper[edge] = 0.5 * (1 + np.cos(np.pi * (ff[edge] - 0.9 * ff[-1]) / (0.1 * ff[-1])))
    H = H * taper * np.exp(-2j * np.pi * ff * t_pre)
    n = 2 * N * pad
    step = np.cumsum(np.fft.irfft(H, n))
    t = (np.arange(n) + 0.5) / (n * df) - t_pre
    keep = t < 0.95 / df - t_pre
    return t[keep], step[keep]


def edge_figures(t: np.ndarray, v: np.ndarray) -> dict[str, float]:
    """What a step response shows of the edge at the receiver, as fractions
    of the level it settles to (the mean of its last twentieth):

    - ``overshoot``: how far the highest point stands above that level;
    - ``ringback``: how far it falls back below the level after that
      highest point;
    - ``reversal``: the deepest fall on the way up, between first reaching
      10 % and first reaching 90 % of the level. An edge that turns back
      there can clock a receiver twice;
    - ``settle``: seconds from first reaching half the level until it
      stays within 5 % of it.
    """
    v = np.asarray(v, dtype=float)
    out = {"final": 0.0, "overshoot": 0.0, "ringback": 0.0, "reversal": 0.0, "settle": 0.0}
    tail = max(len(v) // 20, 1)
    final = float(v[-tail:].mean())
    if len(v) < 4 or abs(final) < 1e-12:
        return out
    u = v / final                                   # settles to 1, whatever the sign
    out["final"] = final
    k = int(np.argmax(u))
    out["overshoot"] = max(float(u[k]) - 1.0, 0.0)
    out["ringback"] = max(1.0 - float(u[k:].min()), 0.0)
    up10 = np.nonzero(u >= 0.1)[0]
    up90 = np.nonzero(u >= 0.9)[0]
    if len(up10) and len(up90) and up90[0] > up10[0]:
        seg = u[up10[0]:up90[0] + 1]
        out["reversal"] = max(float((np.maximum.accumulate(seg) - seg).max()), 0.0)
    half = np.nonzero(u >= 0.5)[0]
    away = np.nonzero(np.abs(u - 1.0) > 0.05)[0]
    if len(half) and len(away) and away[-1] + 1 < len(u) and away[-1] >= half[0]:
        out["settle"] = float(t[away[-1] + 1] - t[half[0]])
    return out


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


# -- equalisation -------------------------------------------------------------------

#: The second pole of the receiver's CTLE, in first poles.
CTLE_POLE_RATIO = 4.0


def ctle(f: np.ndarray, boost_db: float, f_peak: float) -> np.ndarray:
    """A receiver's continuous-time linear equaliser, one zero and two
    poles: g (1 + j f / fz) / ((1 + j f / fp) (1 + j f / (4 fp))) with
    g = 10^(-boost/20) and fz = g fp. Its gain is g at DC and rises from
    the zero to about 1 between the poles. ``boost_db`` is what the high
    frequencies gain over DC; ``f_peak`` is the first pole fp, usually the
    Nyquist frequency."""
    g = 10.0 ** (-boost_db / 20.0)
    f = np.asarray(f, dtype=float)
    fz = f_peak * g
    return g * (1 + 1j * f / fz) / ((1 + 1j * f / f_peak) * (1 + 1j * f / (CTLE_POLE_RATIO * f_peak)))


def ffe(pulse: Pulse, taps: Sequence[float]) -> Pulse:
    """The pulse as a transmitter with a feed-forward equaliser sends it:
    the sum of the pulse weighted by each tap, one unit interval apart.
    The taps are scaled so their magnitudes add up to 1 (the driver's peak
    swing stays what it is); the largest is the main one, those before it
    are pre-cursor taps."""
    c = np.asarray(list(taps), dtype=float)
    total = float(np.abs(c).sum())
    if not len(c) or total <= 0:
        raise ValueError("tx_ffe needs one tap or more that is not zero")
    c = c / total
    out = np.zeros(len(pulse.p) + (len(c) - 1) * pulse.spui)
    for k, w in enumerate(c):
        out[k * pulse.spui:k * pulse.spui + len(pulse.p)] += w * pulse.p
    t = pulse.t[0] + np.arange(len(out)) * (pulse.ui / pulse.spui)
    return Pulse(t, out, pulse.ui, pulse.spui)


def best_ffe(pulse: Pulse, pre: int, post: int, dfe_taps: int = 0) -> list[float]:
    """The transmitter taps (``pre`` before the main one, ``post`` after)
    that leave the largest worst-case eye at the pulse's sampling point,
    with their magnitudes adding up to 1.

    At a fixed sampling point the worst-case opening, main cursor less the
    magnitudes of all the others, is piecewise linear in the taps, so the
    best taps are the solution of a linear programme: exact, not a search.
    The first ``dfe_taps`` cursors after the main one are left out, as a
    DFE takes them off.
    """
    from scipy.optimize import linprog
    n = pre + 1 + post
    if n == 1:
        return [1.0]
    spui = pulse.spui
    c0 = pulse.center_index
    first = c0 % spui
    samples = pulse.p[first::spui]                      # the cursors, main at index m0
    m0 = (c0 - first) // spui
    # cursor k of the equalised pulse (k = 0 the main one): sum over taps j
    # (-pre..post) of tap_j * samples[m0 + k - j]
    ks = np.arange(-m0 - post, len(samples) - m0 + pre)
    a = np.zeros((len(ks), n))
    for col, j in enumerate(range(-pre, post + 1)):
        idx = m0 + ks - j
        ok = (idx >= 0) & (idx < len(samples))
        a[ok, col] = samples[idx[ok]]
    main = a[ks == 0][0]
    rest = a[(ks != 0) & ~((ks >= 1) & (ks <= dfe_taps))]
    rest = rest[np.any(rest != 0, axis=1)]
    m = len(rest)
    # variables: taps as plus and minus parts, then one bound t_k per cursor
    cost = np.concatenate([-main, main, np.ones(m)])
    # between equal eyes, the one with the least equalisation
    side = np.ones(n)
    side[pre] = 0.0
    cost[:2 * n] += 1e-6 * np.concatenate([side, side])
    upper = np.block([[rest, -rest, -np.eye(m)], [-rest, rest, -np.eye(m)],
                      [np.ones((1, 2 * n)), np.zeros((1, m))]])
    res = linprog(cost, A_ub=upper, b_ub=np.concatenate([np.zeros(2 * m), [1.0]]),
                  bounds=[(0, None)] * (2 * n + m), method="highs")
    if not res.success:
        return [0.0] * pre + [1.0] + [0.0] * post
    taps = res.x[:n] - res.x[n:2 * n]
    total = float(np.abs(taps).sum())
    if total <= 0 or abs(taps[pre]) < np.max(np.abs(taps)):
        # the main tap is not the largest: the pulse is no channel to equalise
        return [0.0] * pre + [1.0] + [0.0] * post
    return [float(x) for x in np.round(taps / total, 4)]


def dfe(pulse: Pulse, taps: int, limit: float = 0.5) -> tuple[Pulse, list[float]]:
    """The pulse after a decision-feedback equaliser of ``taps`` taps, and
    the taps (in the pulse's units). Tap k takes off what the pulse still
    holds k unit intervals after its sampling point, for the whole of that
    interval, as the receiver does with the bit it decided k bits ago. A
    tap reaches at most ``limit`` of the main cursor. Decisions are taken
    as right: error propagation is not modelled."""
    c = pulse.center_index
    main = abs(float(pulse.p[c]))
    p = pulse.p.copy()
    got: list[float] = []
    for k in range(1, int(taps) + 1):
        i = c + k * pulse.spui
        if i >= len(p):
            break
        d = float(np.clip(pulse.p[i], -limit * main, limit * main))
        got.append(d)
        a = i - pulse.spui // 2
        p[a:a + pulse.spui] -= d
    return Pulse(pulse.t, p, pulse.ui, pulse.spui), got


#: A pulse response needs the sweep to cover this many unit intervals.
MIN_WINDOW_UI = 3.0
#: ... and the step to move less than this part of its final value over the
#: last 15 % of that time.
UNSETTLED = 0.05


def pulse_response(f: np.ndarray, h: np.ndarray, ui: float, rise: float,
                   spui: int = 64, dc: Optional[float] = None) -> Pulse:
    """The response to one bit: a step and the same step one interval
    later, taken off. ``dc`` is the transfer at 0 Hz, the level the step
    settles to (default the real part of the first point)."""
    t, step = step_response(f, h, rise, dc=dc)
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
        # open at every sample: the crossing lies between two samples, so the
        # eye is a little under one interval wide, never more
        width = min(width + 1, len(open_) - 1)
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
    return {"eye_height": float(max(heights[k], 0.0)), "margin": float(heights[k]),
            "eye_width_ui": width / spui,
            "eye_width": width / spui * pulse.ui, "main_cursor": float(p[pk]),
            "isi_sum": float(np.sum(np.abs(p[np.arange(pk % spui, len(p), spui)])) - abs(p[pk]))}


#: Voltage bins of the statistical eye, and the lowest bit error rate it is
#: asked for.
STAT_BINS = 32768
BER_FLOOR = 1e-18
#: How far the lower tail is lifted against the body for the transform.
STAT_TILT = 1e9
#: The least Gaussian width the transform is given, in bins: it takes the
#: spectrum to rounding at the grid's highest frequency, so that the steps of
#: a density made of ISI alone do not ring across the grid.
STAT_MIN_SIGMA_BINS = 2.8


def _log_cos_terms(order: int = 16) -> np.ndarray:
    """Coefficients c_n of ln cos x = -sum c_n x^(2n), n = 1..order."""
    from scipy.special import bernoulli, factorial
    n = np.arange(1, order + 1)
    b = np.abs(bernoulli(2 * order)[2 * n])
    return 2.0 ** (2 * n - 1) * (2.0 ** (2 * n) - 1) * b / (n * factorial(2 * n))


#: A cursor under this part of a voltage bin goes into the series: its
#: argument then stays under pi/4 over the whole grid.
STAT_SERIES_BINS = 0.25
_LOG_COS = _log_cos_terms()


def _small_cursors(cur: np.ndarray, w: np.ndarray) -> np.ndarray:
    """ln of the product of cos(w a) over the cursors ``cur``, all small:
    from the series of ln cos, which needs only the sums of their even
    powers. One pass whatever their number."""
    out = np.zeros(len(w), dtype=complex)
    if not len(cur):
        return out
    scale = float(np.max(np.abs(cur)))
    rel = cur / scale
    z = (w * scale) ** 2
    power = np.ones(len(w), dtype=complex)
    for n, c in enumerate(_LOG_COS, start=1):
        power = power * z
        out -= c * float(np.sum(rel ** (2 * n))) * power
    return out


def jitter_weights(dt: float, rj_rms: float, dj_pp: float, most: int) -> np.ndarray:
    """Probability of each sampling offset, in steps of ``dt`` from -n to n:
    Gaussian random jitter around the two points of the deterministic jitter
    (dual Dirac). Each step holds the probability of the offsets nearest to
    it, so the tails are kept. ``most`` bounds n."""
    if rj_rms <= 0 and dj_pp <= 0:
        return np.ones(1)
    from scipy.special import ndtr
    n = int(min(most, max(1, np.ceil((8.5 * rj_rms + dj_pp / 2) / dt + 0.5))))
    edges = (np.arange(-n, n + 2) - 0.5) * dt
    w = np.zeros(2 * n + 1)
    for centre in (-dj_pp / 2, dj_pp / 2):
        if rj_rms > 0:
            cdf = ndtr((edges - centre) / rj_rms)
        else:
            cdf = (edges > centre).astype(float)
        cdf[0], cdf[-1] = 0.0, 1.0                     # what lies beyond goes to the ends
        w += 0.5 * np.diff(cdf)
    return w


def _level_density(main: float, cur: np.ndarray, sigma: float, span: float) -> np.ndarray:
    """Probability of each voltage bin, over -span..span, for the level
    ``main`` plus each of ``cur`` with a random sign plus a Gaussian of
    ``sigma``. Right from the lower tail to past the middle; the upper
    part, which no contour is read from, falls under the rounding and is
    left out."""
    dv = 2 * span / STAT_BINS
    omega = 2 * np.pi * np.fft.rfftfreq(STAT_BINS, dv)
    v = (np.arange(STAT_BINS) - STAT_BINS // 2) * dv
    small = STAT_SERIES_BINS * dv
    big = cur[np.abs(cur) >= small]
    tiny = cur[(np.abs(cur) < small) & (cur != 0)]
    # The density is transformed tilted by exp(-theta (x - main)), which
    # lifts its lower tail by STAT_TILT against the body, and tilted back
    # afterwards: the transform's rounding, level with the body, then
    # falls away along the tail the contour is read from.
    theta = np.log(STAT_TILT) / (float(np.sum(np.abs(cur))) + 7.0 * sigma)
    w_ = omega - 1j * theta
    phi = np.exp(-0.5 * (w_ * sigma) ** 2 + _small_cursors(tiny, w_))
    for j in range(0, len(big), 64):
        phi = phi * np.prod(np.cos(big[j: j + 64, None] * w_[None, :]), axis=0)
    # at w = 0 it is the tilted density's whole weight: taken out for the
    # transform, so that its rounding is known, and put back after
    weight = float(phi[0].real)
    # 0 V sits mid-grid
    phi = phi / weight * np.exp(-1j * omega * (main + span))
    tilted = np.fft.irfft(phi, STAT_BINS)
    # the transform's rounding, about level across the grid
    floor = 20 * np.finfo(float).eps * np.log2(STAT_BINS) * np.sqrt(
        2 * float(np.sum(np.abs(phi) ** 2)) / STAT_BINS)
    tilted[tilted < floor] = 0.0
    with np.errstate(over="ignore"):
        return tilted * weight * np.exp(np.clip(theta * (v - main), None, 60.0))


def statistical_eye(pulse: Pulse, amplitude: float, ber: float = 1e-12, noise_rms: float = 0.0,
                    rj_rms: float = 0.0, dj_pp: float = 0.0) -> dict[str, Any]:
    """The eye at a bit error rate, from the pulse response.

    At each sampling phase the received level of a one is its main cursor
    plus the other cursors, each with the sign of an independent random bit.
    The characteristic function of that sum is the product of cos(w a_k),
    times the Gaussian's for the noise; its transform is the level's
    probability density, exact but for the grid. Jitter mixes the densities
    of the phases it reaches. The inner contour is the level a one falls
    below with probability ``ber``; a zero mirrors it.
    """
    if not BER_FLOOR <= ber <= 0.1:
        raise ValueError(f"ber is {BER_FLOOR:g} to 0.1")
    spui = pulse.spui
    p = pulse.p * amplitude / 2
    pk = pulse.peak_index
    w = jitter_weights(pulse.ui / spui, rj_rms, dj_pp, spui // 2)
    reach = len(w) // 2
    offsets = np.arange(-reach, spui + reach)
    # every sample belongs to one bit position of one phase
    first = pk - spui // 2 + offsets
    rows = []
    for c in first:
        idx = np.arange(c % spui, len(p), spui)
        cur = p[idx]
        main = p[c] if 0 <= c < len(p) else 0.0
        if 0 <= c < len(p):
            cur = np.delete(cur, (c - c % spui) // spui)
        rows.append((main, cur))
    reachv = max(abs(m) + np.sum(np.abs(c)) for m, c in rows)
    span = max(reachv + 12.0 * noise_rms, 1e-12) * 1.05
    dv = 2 * span / STAT_BINS
    omega = 2 * np.pi * np.fft.rfftfreq(STAT_BINS, dv)
    v = (np.arange(STAT_BINS) - STAT_BINS // 2) * dv
    sigma = max(noise_rms, STAT_MIN_SIGMA_BINS * dv)
    pdfs = np.empty((len(rows), STAT_BINS))
    for i, (main, cur) in enumerate(rows):
        pdfs[i] = _level_density(main, cur, sigma, span)
    if reach:
        pdfs = sum(w[j] * pdfs[j: j + spui] for j in range(len(w)))
    cdf = np.cumsum(pdfs, axis=1)
    # the level a one stays above but for ``ber``: interpolated in the bin
    inner = np.empty(spui)
    errors = np.empty(spui)
    zero = STAT_BINS // 2
    for i in range(spui):
        k = int(np.searchsorted(cdf[i], ber))
        lo = cdf[i, k - 1] if k else 0.0
        hi = cdf[i, k]
        if lo > 0:                                      # a tail climbs by a factor a bin
            frac = np.log(ber / lo) / np.log(hi / lo)
        else:
            frac = ber / max(hi, 1e-300)
        inner[i] = v[k] - dv / 2 + frac * dv
        errors[i] = cdf[i, zero - 1] + 0.5 * pdfs[i, zero]
    heights = 2 * inner
    k = int(np.argmax(heights))
    open_ = heights > 0
    width = 0
    if open_[k]:                                        # the run of open phases around the best
        a = b = k
        while a > 0 and open_[a - 1]:
            a -= 1
        while b < spui - 1 and open_[b + 1]:
            b += 1
        width = b - a + 1
    return {"eye_height": float(max(heights[k], 0.0)), "eye_width_ui": width / spui,
            "eye_width": width / spui * pulse.ui, "phase": k,
            "bit_error_rate": float(max(np.min(errors), BER_FLOOR / 10)),
            "heights": heights, "errors": errors}


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
