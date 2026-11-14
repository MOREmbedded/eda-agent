# SPDX-License-Identifier: Apache-2.0
"""Channel-eye jobs: from a Touchstone file to an eye, two ways.

The ``eda-agent`` engine convolves the channel's pulse response with a
PRBS pattern and adds the worst-case (peak distortion) eye. The ``spice``
engine fits the channel with a passive rational macromodel, writes it as
a SPICE subcircuit (kept as ``channel.sub``, with an LTspice symbol, for
use in any schematic) and runs the same pattern through ngspice or
LTspice. Both report ``eye_height_mv`` and ``eye_width_ps``, so the job
page sets them side by side.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..emc.touchstone import read_touchstone
from . import channel as ch
from . import eye as _eye
from . import vectfit
from .jobs import _metric, _num
from .mtl import mixed_mode
from .spice_export import subcircuit

EYE_SPEC: dict[str, tuple[str, bool, str]] = {
    "data_rate_gbps": ("number", True, "data rate (NRZ)"),
    "rise_ps": ("number", False, "transmitter 10-90 % edge (default 0.3 UI)"),
    "amplitude_mv": ("number", False, "transmitter swing into a matched load, peak to peak "
                                      "(differential for a pair; default 800)"),
    "noise_mv": ("number", False, "noise at the receiver from other sources (crosstalk, supply), "
                                  "peak, each side of zero: the eye loses twice this in height "
                                  "(default 0)"),
    "prbs": ("number", False, "PRBS order: 7, 9, 11, 15 (default 7)"),
    "repeats": ("number", False, "times the pattern runs after settling (default 2)"),
    "samples_per_ui": ("number", False, "time steps per unit interval (default 32)"),
    "spice_oversample": ("number", False, "SPICE steps per time step (default 4)"),
    "simulator": ("text", False, "spice engine: auto, ngspice or ltspice (default auto)"),
    "fit_tol": ("number", False, "macromodel RMS error allowed (default 0.002)"),
    "tx_ibis": ("text", False, "IBIS file of the driver: its output resistance, edge and "
                               "C_comp replace the defaults"),
    "tx_model": ("text", False, "[Model] of the driver in tx_ibis (needed when it has several)"),
    "rx_ibis": ("text", False, "IBIS file of the receiver: its C_comp loads the far end"),
    "rx_model": ("text", False, "[Model] of the receiver in rx_ibis"),
    "tx_r_ohms": ("number", False, "driver output resistance per side (default the reference "
                                   "impedance)"),
    "tx_c_pf": ("number", False, "driver pad capacitance per side (default 0)"),
    "rx_r_ohms": ("text", False, "receiver termination per side to ground, or 'open' (default "
                                 "the reference impedance; open with rx_ibis)"),
    "rx_c_pf": ("number", False, "receiver pad capacitance per side (default 0)"),
}


def load_channel(path: Path) -> tuple[np.ndarray, np.ndarray, float]:
    data = read_touchstone(path)
    f = np.asarray(data["freq_hz"], dtype=float)
    s = np.asarray(data["s"], dtype=complex).reshape(len(f), data["ports"], data["ports"])
    if data["ports"] not in (2, 4):
        raise ValueError("an eye needs a 2-port (one line) or a 4-port (a pair: ports 1, 2 "
                         "near P and N, 3, 4 far)")
    return f, s, float(data["z0"])


def uniform(f: np.ndarray, s: np.ndarray, tol: float) -> tuple[np.ndarray, np.ndarray,
                                                                 Optional[vectfit.Model]]:
    """The data on df, 2 df, ... N df; resampled through a fitted model
    when the file's grid is not like that."""
    df = f[0]
    if df > 0 and np.allclose(np.diff(f), df, rtol=1e-6) and abs(f[0] - df) < 1e-9 * df:
        return f, s, None
    keep = f > 0
    model = vectfit.macromodel(f[keep], s[keep], tol)
    step = float(np.min(np.diff(f[keep]))) if keep.sum() > 1 else float(f[keep][0])
    step = min(step, float(f[keep][0]))
    n = int(math.ceil(f[-1] / step))
    grid = ch.uniform_band(n * step, n)
    return grid, model.eval(grid), model


def transfer(s: np.ndarray) -> tuple[np.ndarray, bool]:
    if s.shape[1] == 4:
        return mixed_mode(s)[:, 1, 0], True
    return s[:, 1, 0], False


def _settings(spec: dict[str, Any]) -> dict[str, Any]:
    rate = _num(spec, "data_rate_gbps", 0) * 1e9
    if rate <= 0:
        raise ValueError("data_rate_gbps must be more than 0")
    ui = 1 / rate
    out = {"ui": ui, "rate": rate,
           "rise": _num(spec, "rise_ps", 0.3 * ui * 1e12) * 1e-12,
           "amp": _num(spec, "amplitude_mv", 800) / 1000,
           "order": int(_num(spec, "prbs", 7)),
           "repeats": int(_num(spec, "repeats", 2)),
           "spui": int(_num(spec, "samples_per_ui", 32))}
    if out["order"] not in (7, 9, 11, 15):
        raise ValueError("prbs is 7, 9, 11 or 15")
    if not 8 <= out["spui"] <= 256:
        raise ValueError("samples_per_ui is 8 to 256")
    return out


def terminations(spec: dict[str, Any], st: dict[str, Any], z0: float, ports: int
                 ) -> dict[str, Any]:
    """Source and load of the eye: the reference impedance on both ends
    unless the spec names resistances, capacitances or IBIS buffers. An
    IBIS driver sets the output resistance (its V-I tables near the rail), C_comp and,
    unless rise_ps or amplitude_mv is given, the edge and the swing: rail
    to rail open circuit on each driven side, which is an amplitude of
    half the supply for a line (its source swings +/- amplitude) and the
    whole supply for a pair (each side swings +/- amplitude/2)."""
    from . import ibis
    notes: list[str] = []
    rs, cs, rl, cl = z0, 0.0, z0, 0.0
    tx_path = str(spec.get("tx_ibis") or "").strip()
    rx_path = str(spec.get("rx_ibis") or "").strip()
    if tx_path:
        model = ibis.pick(ibis.read(tx_path), str(spec.get("tx_model") or ""))
        d = ibis.driver(model)
        if "r_out_ohms" in d:
            rs = d["r_out_ohms"]
        cs = d.get("c_comp_f", 0.0)
        if spec.get("rise_ps") in (None, "") and d.get("rise_s"):
            st["rise"] = d["rise_s"]
        if spec.get("amplitude_mv") in (None, "") and model.voltage:
            st["amp"] = model.voltage if ports == 4 else model.voltage / 2
        notes.append(f"driver {model.name} from {Path(tx_path).name}: "
                     f"{rs:.1f} ohm, C_comp {cs * 1e12:.2f} pF, edge {st['rise'] * 1e12:.0f} ps"
                     + (f", {model.voltage:g} V supply" if model.voltage else "")
                     + " (linearised at the typical corner)"
                     + (f"; {d['note']}" if d.get("note") else ""))
    if rx_path:
        model = ibis.pick(ibis.read(rx_path), str(spec.get("rx_model") or ""))
        r = ibis.receiver(model)
        cl = r.get("c_comp_f", 0.0)
        rl = r.get("r_term_ohms")
        notes.append(f"receiver {model.name} from {Path(rx_path).name}: C_comp "
                     f"{cl * 1e12:.2f} pF, "
                     + (f"on-die termination {rl:.1f} ohm (from its clamp tables)" if rl
                        else "high impedance unless rx_r_ohms is given"))
    if spec.get("tx_r_ohms") not in (None, ""):
        rs = _num(spec, "tx_r_ohms", z0)
    if spec.get("tx_c_pf") not in (None, ""):
        cs = _num(spec, "tx_c_pf", 0) * 1e-12
    raw = spec.get("rx_r_ohms")
    if raw not in (None, ""):
        rl = None if str(raw).strip().lower() in ("open", "inf", "none") else _num(spec, "rx_r_ohms", z0)
    if spec.get("rx_c_pf") not in (None, ""):
        cl = _num(spec, "rx_c_pf", 0) * 1e-12
    if rs <= 0 or (rl is not None and rl <= 0) or cs < 0 or cl < 0:
        raise ValueError("terminations must be positive (rx_r_ohms may be 'open')")
    matched = abs(rs - z0) < 1e-9 and rl is not None and abs(rl - z0) < 1e-9 and not cs and not cl
    if not matched:
        notes.append(f"source {rs:.1f} ohm + {cs * 1e12:.2f} pF, load "
                     f"{'open' if rl is None else f'{rl:.1f} ohm'} + {cl * 1e12:.2f} pF per side")
    return {"rs": rs, "cs": cs, "rl": rl, "cl": cl, "matched": matched, "notes": notes}


def channel_transfer(f: np.ndarray, s: np.ndarray, z0: float, term: dict[str, Any]
                     ) -> tuple[np.ndarray, bool]:
    h, pair = transfer(s)
    if not term["matched"]:
        h = _eye.terminated_transfer(s, f, z0, term["rs"], term["cs"], term["rl"], term["cl"])
    return h, pair


def _eye_metrics(m: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    return {
        f"{prefix}eye_height_mv": _metric(round(m["eye_height"] * 1e3, 3), "mV",
                                          "Eye height at the best sampling phase"),
        f"{prefix}eye_width_ps": _metric(round(m["eye_width"] * 1e12, 3), "ps", "Eye width"),
        f"{prefix}eye_width_ui": _metric(round(m["eye_width_ui"], 4), "UI", "Eye width"),
    }


def _with_noise(spec: dict[str, Any], height: float, m: dict[str, Any],
                verdicts: list[dict[str, Any]], notes: list[str]) -> None:
    """The eye of ``height`` volts with ``noise_mv`` on both rails."""
    noise = _num(spec, "noise_mv", 0)
    if noise < 0:
        raise ValueError("noise_mv is 0 or more")
    if noise == 0:
        return
    left = max(height * 1e3 - 2 * noise, 0.0)
    m["noise_mv"] = _metric(round(noise, 3), "mV", "Noise at the receiver, peak")
    m["eye_height_with_noise_mv"] = _metric(round(left, 3), "mV",
                                            "Eye height with the noise on both rails")
    verdicts.append({"check": "eye open with noise", "pass": left > 0,
                     "detail": f"{left:.1f} mV left of {height * 1e3:.1f} mV with "
                               f"{noise:g} mV of noise on each rail"})
    notes.append(f"noise of {noise:g} mV peak lowers the high rail and raises the low rail "
                 "by that much, whenever it arrives: the worst case")


def _write_wave(path: Path, t: np.ndarray, v: np.ndarray) -> None:
    np.savetxt(path, np.column_stack([t * 1e12, v * 1e3]), delimiter=",",
               header="time_ps,mv", comments="", fmt="%.6g")


def run_convolution(spec: dict[str, Any], touchstone: Path, folder: Path, progress=None
                    ) -> dict[str, Any]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *_: None)
    st = _settings(spec)
    f, s, z0 = load_channel(touchstone)
    term = terminations(spec, st, z0, s.shape[1])
    f, s, model = uniform(f, s, _num(spec, "fit_tol", 2e-3))
    h, pair = channel_transfer(f, s, z0, term)
    say("pulse response", 0.2)
    pulse = _eye.pulse_response(f, h, st["ui"], st["rise"], st["spui"])
    say("PRBS eye", 0.5)
    se = _eye.simulated_eye(pulse, st["amp"], st["order"], st["repeats"])
    wc = _eye.worst_case_eye(pulse, st["amp"])
    k = ch.nyquist_index(f, st["rate"] / 2)
    m = _eye_metrics(se)
    m["worst_case_eye_height_mv"] = _metric(round(wc["eye_height"] * 1e3, 3), "mV",
                                            "Worst-case eye height (peak distortion analysis)")
    m["worst_case_eye_width_ps"] = _metric(round(wc["eye_width"] * 1e12, 3), "ps",
                                           "Worst-case eye width")
    m["main_cursor_mv"] = _metric(round(wc["main_cursor"] * 1e3, 3), "mV", "Main cursor")
    m["isi_mv"] = _metric(round(wc["isi_sum"] * 1e3, 3), "mV", "Sum of the other cursors")
    m["insertion_loss_db_at_nyquist"] = _metric(round(float(-ch.db(h[k])), 3), "dB",
                                                f"Insertion loss at {f[k] / 1e9:g} GHz")
    bits = np.tile(_eye.prbs(st["order"]), st["repeats"] + 1)
    t, v = _eye.waveform(pulse, bits, st["amp"])
    _write_wave(folder / "waveform.csv", t, v)
    files = {"waveform": "waveform.csv"}
    title = (f"{'pair' if pair else 'line'}, {st['rate'] / 1e9:g} Gb/s, PRBS{st['order']}, "
             f"convolution")
    if _eye.plot_eye(folder / "eye.png", se["eye"], st["ui"], title):
        files["plot"] = "eye.png"
    verdicts = [{"check": "eye open", "pass": se["eye_height"] > 0,
                 "detail": f"{se['eye_height'] * 1e3:.1f} mV by {se['eye_width'] * 1e12:.1f} ps"},
                {"check": "worst case open", "pass": wc["eye_height"] > 0,
                 "detail": "peak distortion analysis: no pattern closes it"
                 if wc["eye_height"] > 0 else "some pattern can close the eye"}]
    notes = [f"pulse response from {'SDD21' if pair else 'S21'} with a linear "
             f"{st['rise'] * 1e12:g} ps edge; PRBS{st['order']}, "
             f"{len(bits)} bits; amplitude {st['amp'] * 1e3:g} mV (incident, into a matched load)"]
    notes += term["notes"]
    if model is not None:
        notes.append(f"the file's frequencies were not evenly spaced from df; resampled "
                     f"through a {len(model.poles)}-pole fit")
    _with_noise(spec, se["eye_height"], m, verdicts, notes)
    return {"metrics": m, "verdicts": verdicts, "notes": notes, "files": files}


def symbol(name: str, ports: list[str]) -> str:
    """An LTspice symbol (.asy) for the subcircuit: ports left and right,
    the reference at the bottom."""
    half = (len(ports) + 1) // 2
    h = 32 * max(half, 1) + 32
    lines = ["Version 4", "SymbolType BLOCK", f"RECTANGLE Normal -64 0 64 {h}",
             "WINDOW 0 0 -16 Bottom 2", f"WINDOW 3 0 {h + 16} Top 2",
             f"SYMATTR Value {name}", "SYMATTR Prefix X",
             f"SYMATTR ModelFile {name}.sub",
             f"SYMATTR Description {len(ports)}-port channel macromodel from eda-agent"]
    order = 0
    for i, p in enumerate(ports):
        left = i < half
        y = 32 + 32 * (i if left else i - half)
        x = -64 if left else 64
        lines += [f"PIN {x} {y} {'LEFT' if left else 'RIGHT'} 8", f"PINATTR PinName {p}",
                  f"PINATTR SpiceOrder {order + 1}"]
        order += 1
    lines += [f"PIN 0 {h} BOTTOM 8", "PINATTR PinName ref", f"PINATTR SpiceOrder {order + 1}"]
    return "\n".join(lines) + "\n"


def write_model(model: vectfit.Model, folder: Path, name: str, z0: float) -> dict[str, str]:
    ports = (["p_near", "n_near", "p_far", "n_far"] if model.ports == 4
             else [f"port{i + 1}" for i in range(model.ports)])
    sub = subcircuit(model, name, z0, ports)
    (folder / f"{name}.sub").write_text(sub, encoding="utf-8")
    (folder / f"{name}.asy").write_text(symbol(name, ports), encoding="utf-8")
    (folder / "macromodel.json").write_text(json.dumps(model.to_dict()), encoding="utf-8")
    return {"subcircuit": f"{name}.sub", "ltspice_symbol": f"{name}.asy",
            "macromodel": "macromodel.json"}


def run_spice(spec: dict[str, Any], touchstone: Path, folder: Path, progress=None
              ) -> dict[str, Any]:
    from . import spice_eye
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *_: None)
    st = _settings(spec)
    f0, s0, z0 = load_channel(touchstone)
    term = terminations(spec, st, z0, s0.shape[1])
    keep = f0 > 0
    say("fitting the macromodel", 0.1)
    model = vectfit.macromodel(f0[keep], s0[keep], _num(spec, "fit_tol", 2e-3))
    files = write_model(model, folder, "channel", z0)
    # the pulse grid of the convolution, from the model itself
    f, s, _ = uniform(f0, s0, _num(spec, "fit_tol", 2e-3))
    s_model = model.eval(f)
    h_model, pair = channel_transfer(f, s_model, z0, term)
    pulse = _eye.pulse_response(f, h_model, st["ui"], st["rise"], st["spui"])
    bits = np.tile(_eye.prbs(st["order"]), st["repeats"] + 1)
    over = int(_num(spec, "spice_oversample", 4))
    say("SPICE transient", 0.3)
    res = spice_eye.run(model, pulse, bits, st["rise"], st["amp"], z0, pair, folder,
                        str(spec.get("simulator") or "auto"), dt_divisor=over, term=term)
    if not res.get("ok"):
        raise ValueError(res.get("reason") or "; ".join(res.get("errors", [])) or "SPICE failed")
    v = res["v"]
    t = res["t"]
    _write_wave(folder / "waveform.csv", t, v)
    files["waveform"] = "waveform.csv"
    span = len(pulse.p) // pulse.spui + 1
    skip = min(span, (1 << st["order"]) - 1)
    c = pulse.center_index
    phase0 = (c - pulse.spui // 2) % pulse.spui
    shift = (c - pulse.spui // 2) // pulse.spui
    eye = _eye.fold(t, v, st["ui"], st["spui"], skip + shift, phase0)
    labels = bits[skip: skip + len(eye)]
    em = _eye.eye_metrics(eye, labels, st["ui"])
    # the same macromodel through the convolution: the two routes must agree
    _, vc = _eye.waveform(pulse, bits, st["amp"])
    n = min(len(vc), len(v))
    rms = float(np.sqrt(np.mean((vc[:n] - v[:n]) ** 2)))
    m = _eye_metrics(em)
    m["spice_vs_convolution_rms_mv"] = _metric(round(rms * 1e3, 4), "mV",
                                               "RMS difference from the convolution of the "
                                               "same macromodel")
    m["macromodel_poles"] = _metric(len(model.poles), "", "Poles of the macromodel")
    m["macromodel_rms_error"] = _metric(round(model.info["rms_error_after_enforcement"], 6), "",
                                        "RMS error of the macromodel's S")
    title = (f"{'pair' if pair else 'line'}, {st['rate'] / 1e9:g} Gb/s, PRBS{st['order']}, "
             f"{res.get('engine')}")
    if _eye.plot_eye(folder / "eye.png", eye, st["ui"], title):
        files["plot"] = "eye.png"
    tol = 0.02 * st["amp"]
    verdicts = [
        {"check": "macromodel passive", "pass": model.info["passivity_after"] <= 1.0,
         "detail": f"largest singular value {model.info['passivity_after']:.5f} "
                   f"(before enforcement {model.info['passivity_before']:.5f})"},
        {"check": "macromodel fit", "pass": model.info["rms_error_after_enforcement"]
         <= max(_num(spec, "fit_tol", 2e-3) * 2, 1e-3),
         "detail": f"RMS error {model.info['rms_error_after_enforcement']:.2e} with "
                   f"{len(model.poles)} poles"},
        {"check": "SPICE agrees with convolution", "pass": rms <= tol,
         "detail": f"{rms * 1e3:.2f} mV RMS over the pattern, {tol * 1e3:.1f} mV allowed"},
        {"check": "eye open", "pass": em["eye_height"] > 0,
         "detail": f"{em['eye_height'] * 1e3:.1f} mV by {em['eye_width'] * 1e12:.1f} ps"},
    ]
    notes = [f"{res.get('engine')} transient of the macromodel ({len(model.poles)} poles, "
             f"written as channel.sub with an LTspice symbol channel.asy), PWL source with "
             f"linear {st['rise'] * 1e12:g} ps edges, step {st['ui'] / st['spui'] / over * 1e12:.3g} ps",
             *term["notes"],
             "place channel.asy in an LTspice schematic (with channel.sub beside it) to drive "
             "the channel with real driver and receiver models"]
    _with_noise(spec, em["eye_height"], m, verdicts, notes)
    return {"metrics": m, "verdicts": verdicts, "notes": notes, "files": files}
