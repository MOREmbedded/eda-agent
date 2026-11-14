# SPDX-License-Identifier: Apache-2.0
"""Board SI: every high-speed net of a board, checked and reported.

A ``board-si`` job takes the board snapshot once and runs the routed-net
channel (:mod:`.net_jobs`) on each net or pair it is asked about, with
the limits of its target, and optionally the eye (:mod:`.eye_jobs`).
What fails becomes a *finding*: the net, the check, the value against the
limit, and *where on the board* it is:

* an impedance fault at the place the TDR sees it: the round-trip time to
  the TDR's worst point, halved and divided by the delay per mil, walked
  along the routed path;
* a stub at the point it leaves the path, a via stub at its via;
* a loss, skew or eye fault at the driving pin.

The findings go to ``findings.json`` (for the dashboard and for Altium
markers), ``summary.csv`` and a self-contained ``report.html``.
"""
from __future__ import annotations

import base64
import csv
import fnmatch
import html
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..layout.model import LayoutBoard
from . import crosstalk, extract
from .jobs import _metric, _num
from .net_jobs import NET_SPEC, run_net_channel

#: Keys a target may carry: the net job's limits and models, and the eye's.
_NET_KEYS = {k for k in NET_SPEC if k not in ("net", "pair", "from_pin", "to_pin", "rules")}
_EYE_KEYS = {"ber", "noise_rms_mv", "rj_rms_ps", "dj_pp_ps", "eye_at_ber_min_mv",
             "eye_at_ber_min_ui", "tx_ffe", "ctle_db", "ctle_peak_ghz", "dfe_taps", "dfe_max", "overshoot_max_pct", "ringback_max_pct", "edge_reversal_max_pct",
             "rise_ps", "amplitude_mv", "prbs", "tx_ibis", "tx_model", "rx_ibis", "rx_model",
             "tx_r_ohms", "tx_c_pf", "rx_r_ohms", "rx_c_pf"}
_TARGET_KEYS = {"match", "class", "kind", "eye", "eye_min_mv", "eye_min_ui",
                "xtalk_swing_mv", "delay_match_ps"} | _NET_KEYS | _EYE_KEYS

BOARD_SPEC: dict[str, tuple[str, bool, str]] = {
    "targets": ("list", False, "what to check: a list of {match (net or pair name glob) or "
                               "class (an Altium net class), kind (pair or net), and limits: "
                               "data_rate_gbps, z_target, z_tol_pct, il_max_db, rl_min_db, "
                               "skew_max_ps, stub_margin, delay_match_ps, eye, eye_min_mv, eye_min_ui, and the "
                               "eye's driver and receiver}; default every differential pair"),
    "data_rate_gbps": ("number", False, "data rate where a target gives none"),
    "eye": ("bool", False, "also run the eye of every item (default false)"),
    "max_items": ("number", False, "most nets and pairs checked in one job (default 40)"),
    "workers": ("number", False, "items solved at once (default 4)"),
    "f_max_ghz": ("number", False, "top of the band (default 3x the highest Nyquist, at least 10)"),
    "points": ("number", False, "frequency points (default 200)"),
    "temperature_c": ("number", False, "copper temperature of every trace"),
    "thermal_npz": ("text", False, "a thermal analysis' result.npz: copper temperature by place"),
    "tand": ("number", False, "loss tangent of every dielectric (default 0.02)"),
    "foil": ("text", False, "copper roughness preset"),
    "mask_mils": ("number", False, "solder mask on outer layers"),
    "etch_mils": ("number", False, "etch narrowing of the top face"),
    "xtalk": ("bool", False, "crosstalk from the copper beside each item (default true)"),
    "xtalk_max_pct": ("number", False, "allowed crosstalk where a target gives none (default 5)"),
    "xtalk_reach_mils": ("number", False, "how far a neighbour still counts"),
    "xtalk_quiet": ("list", False, "nets that do not switch, as name globs"),
    "return_path": ("bool", False, "gaps in the reference copper under each item (default true)"),
    "return_gap_min_mils": ("number", False, "shortest gap that is a finding (default 8)"),
    "xtalk_swing_mv": ("number", False, "swing of the neighbours, which turns crosstalk into noise "
                                        "on the eye (default the eye's own amplitude)"),
    "xtalk_aggressors": ("list", False, "neighbours with an edge or a swing of their own: "
                                        "{match, rise_ps, swing_mv}"),
    "xtalk_max_mv": ("number", False, "allowed crosstalk in mV where a target gives none"),
}

#: Checks whose failure is a warning rather than an error.
WARNING_CHECKS = {"return loss", "reciprocity"}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:60] or "item"


def select(board: LayoutBoard, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The items to check: (kind, name, the target's settings). Each net
    or pair is taken by the first target that matches it."""
    pairs = {d.name: d for d in board.diff_pairs}
    in_pair = {n for d in board.diff_pairs for n in (d.positive, d.negative)}
    nets = [n for n in board.nets() if n]
    targets = list(spec.get("targets") or [])
    if not nets:
        raise ValueError("the board has no nets with copper: there is nothing to check")
    if not targets:
        if not pairs:
            raise ValueError("the board has no differential pairs; name what to check in "
                             "targets (match a net or pair name, or an Altium net class)")
        targets = [{"match": "*", "kind": "pair"}]
    out: list[dict[str, Any]] = []
    taken: set[tuple[str, str]] = set()
    for i, tg in enumerate(targets):
        if not isinstance(tg, dict):
            raise ValueError(f"target {i + 1} is not an object")
        bad = set(tg) - _TARGET_KEYS
        if bad:
            raise ValueError(f"target {i + 1}: unknown keys {', '.join(sorted(bad))}")
        kind = str(tg.get("kind") or "").lower()
        if kind not in ("", "pair", "net"):
            raise ValueError(f"target {i + 1}: kind is pair or net")
        if tg.get("class"):
            cls = str(tg["class"])
            members = next((v for k, v in board.net_classes.items() if k.lower() == cls.lower()),
                           None)
            if members is None:
                raise ValueError(f"target {i + 1}: no net class {cls!r}; classes: "
                                 f"{', '.join(sorted(board.net_classes)) or 'none'}")
            names_p = [p for p, d in pairs.items() if d.positive in members and d.negative in members]
            names_n = [n for n in members if n not in in_pair]
        else:
            pat = str(tg.get("match") or "*")
            names_p = [p for p in pairs if fnmatch.fnmatchcase(p.upper(), pat.upper())]
            names_n = [n for n in nets if n not in in_pair
                       and fnmatch.fnmatchcase(n.upper(), pat.upper())]
        if kind == "net":
            names_p = []
        if kind == "pair":
            names_n = []
        for k, names in (("pair", names_p), ("net", names_n)):
            for name in sorted(names):
                if (k, name) in taken:
                    continue
                taken.add((k, name))
                out.append({"kind": k, "name": name, "target": i + 1, "settings": dict(tg)})
    return out


def _walk(path: extract.NetPath, one_way_s: float, tpd: dict[str, float], per_mil: float
          ) -> Optional[tuple[float, float, str, float]]:
    """The point a signal reaches ``one_way_s`` after leaving the start
    pin: each trace section at its own delay per mil (``tpd``, keyed as
    the net model names its cross-sections; ``per_mil`` where a section
    has none), vias taken as instant. Returns x, y, layer and the
    distance in mils."""
    left = max(one_way_s, 0.0)
    dist = 0.0
    last = None
    for s in path.sections:
        if s.kind != "line" or len(s.pts) < 2:
            continue
        rate = tpd.get(f"{s.layer} {round(s.width, 2):g} mil", per_mil)
        for a, b in zip(s.pts, s.pts[1:]):
            d = math.dist(a, b)
            last = (b[0], b[1], s.layer, dist + d)
            if left <= d * rate:
                u = left / (d * rate) if d else 0.0
                return (a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1]), s.layer,
                        dist + u * d)
            left -= d * rate
            dist += d
    return last


def _pin_xy(board: LayoutBoard, key: str) -> Optional[tuple[float, float]]:
    for p in board.pads:
        if p.key == key:
            return (p.x, p.y)
    return None


def _tdr_fault(folder: Path, z_target: float, rise_s: float, delay_s: float
               ) -> Optional[tuple[float, float]]:
    """(one-way time to the TDR's fault, its impedance). The TDR's time
    axis has the edge's 50 % point at the pad at t = 0, so a fault seen
    at t lies t / 2 down the line."""
    try:
        data = np.loadtxt(folder / "tdr.csv", delimiter=",", skiprows=1)
    except (OSError, ValueError):
        return None
    t = data[:, 0] * 1e-12
    z = data[:, 1]
    inside = (t > 0.5 * rise_s) & (t < 2 * delay_s)
    if not inside.any():
        return None
    dev = np.abs(z[inside] - z_target)
    worst = int(np.argmax(dev))
    # the fault starts where the TDR first moves half way to its worst
    # value: a tee or a long low-impedance stretch holds its dip until the
    # far side's reflection returns, so the worst point is its end
    k = worst
    while k > 0 and dev[k - 1] >= 0.5 * dev[worst]:
        k -= 1
    return float(t[inside][k]) / 2, float(z[inside][worst])


#: Further stretches outside the impedance band placed per net, beside the worst.
MAX_EXTRA_FAULTS = 5


def _tdr_stretches(folder: Path, z_target: float, tol: float, rise_s: float, delay_s: float,
                   skip_s: Optional[float] = None) -> list[tuple[float, float, float]]:
    """The stretches of the TDR outside ``z_target`` by more than ``tol``
    (a fraction): (one-way time of the start, one-way time of the end,
    the impedance furthest from the target). A stretch shorter than the
    rise time is the edge of a step, not a stretch of line; the one that
    holds ``skip_s`` (a one-way time) is left out: the worst fault is
    placed by ``_tdr_fault``."""
    try:
        data = np.loadtxt(folder / "tdr.csv", delimiter=",", skiprows=1)
    except (OSError, ValueError):
        return []
    t = data[:, 0] * 1e-12
    z = data[:, 1]
    inside = (t > 0.5 * rise_s) & (t < 2 * delay_s)
    t, z = t[inside], z[inside]
    if len(t) < 2:
        return []
    side = np.where(z > z_target * (1 + tol), 1, np.where(z < z_target * (1 - tol), -1, 0))
    out: list[tuple[float, float, float]] = []
    a = 0
    for b in range(1, len(side) + 1):
        if b < len(side) and side[b] == side[a]:
            continue
        if side[a] != 0 and t[b - 1] - t[a] >= rise_s:
            seg = z[a:b]
            worst = float(seg[int(np.argmax(np.abs(seg - z_target)))])
            t0, t1 = float(t[a]) / 2, float(t[b - 1]) / 2
            if skip_s is None or not (t0 <= skip_s <= t1):
                out.append((t0, t1, worst))
        a = b
    out.sort(key=lambda r: -abs(r[2] - z_target))
    return sorted(out[:MAX_EXTRA_FAULTS])


def _tpd(folder: Path) -> dict[str, float]:
    """Seconds per mil of each cross-section the net model wrote."""
    try:
        xs = json.loads((folder / "sections.json").read_text(encoding="utf-8"))
        return {k: v["delay_ps_per_in"] * 1e-12 / 1000
                for k, v in xs.get("cross_sections", {}).items() if "delay_ps_per_in" in v}
    except (OSError, ValueError, KeyError):
        return {}


def _more_impedance(item: dict[str, Any], res: dict[str, Any], folder: Path, rise_s: float,
                    z_target: float, tol: float, paths, first: dict[str, Any]
                    ) -> list[dict[str, Any]]:
    """The other stretches of the route outside the impedance band: a
    wide section in a line that is itself off target is a fault of its
    own, and the worst point alone would hide it."""
    path = paths[1] if item["kind"] == "pair" else paths[0]
    if path is None or not path.length or "delay_ps" not in res["metrics"]:
        return []
    delay = res["metrics"]["delay_ps"]["value"] * 1e-12
    hit = _tdr_fault(folder, z_target, rise_s, delay)
    tpd = _tpd(folder)
    out = []
    for t0, t1, z in _tdr_stretches(folder, z_target, tol, rise_s, delay,
                                    hit[0] if hit else None):
        a = _walk(path, t0, tpd, delay / path.length)
        b = _walk(path, t1, tpd, delay / path.length)
        if a is None or b is None:
            continue
        if first.get("at_mils") and math.dist(first["at_mils"], a[:2]) < 1.0:
            continue
        out.append({"check": "impedance", "severity": "error",
                    "detail": f"TDR {z:.1f} ohm over {max(b[3] - a[3], 0):.0f} mil, "
                              f"{z_target:g} ohm +/- {tol * 100:g} % wanted",
                    "at_mils": [round(a[0], 2), round(a[1], 2)], "layer": a[2],
                    "where": f"{a[3]:.0f} mil from {path.start} (TDR {z:.1f} ohm)"})
    return out


def _locate(board: LayoutBoard, item: dict[str, Any], v: dict[str, Any], res: dict[str, Any],
            folder: Path, rise_s: float, z_target: float, paths) -> dict[str, Any]:
    path = paths[1] if item["kind"] == "pair" else paths[0]
    start = _pin_xy(board, path.start) if path is not None else None
    where: dict[str, Any] = {}
    if start is not None:
        where = {"at_mils": [round(start[0], 2), round(start[1], 2)], "where": f"at {path.start}"}
    if path is None:
        return where
    if v["check"] in EDGE_CHECKS:
        # the edge is seen where it arrives
        end = _pin_xy(board, path.end)
        if end is not None:
            where = {"at_mils": [round(end[0], 2), round(end[1], 2)], "where": f"at {path.end}"}
    if v["check"] == "impedance" and "delay_ps" in res["metrics"] and path.length:
        delay = res["metrics"]["delay_ps"]["value"] * 1e-12
        hit = _tdr_fault(folder, z_target, rise_s, delay)
        tpd = _tpd(folder)
        if hit is not None:
            one_way, z = hit
            pt = _walk(path, one_way, tpd, delay / path.length)
            if pt is not None:
                where = {"at_mils": [round(pt[0], 2), round(pt[1], 2)], "layer": pt[2],
                         "where": f"{pt[3]:.0f} mil from {path.start} (TDR {z:.1f} ohm)"}
    elif v["check"] == "return path":
        rows = res.get("figures", {}).get("return_path") or []
        if rows:
            g = rows[0]
            where = {"at_mils": g["at_mils"], "layer": g["layer"],
                     "where": f"{g['distance_mils']:.0f} mil along the route, {g['text']}"}
    elif v["check"] in ("crosstalk", "crosstalk noise"):
        rows = res.get("figures", {}).get("crosstalk") or []
        if rows:
            a = rows[0]
            where = {"at_mils": a["at_mils"], "layer": a["layer"],
                     "where": (f"runs {a['length_mils']:.0f} mil across from {a['aggressor']} "
                               f"on {a['aggressor_layer']}, {a['gap_mils']:.1f} mil of "
                               "dielectric between" if a.get("aggressor_layer") else
                               f"runs {a['length_mils']:.0f} mil beside {a['aggressor']} at "
                               f"{a['gap_mils']:.1f} mil")}
    elif v["check"] == "stub resonance":
        blocks = res.get("figures", {}).get("blocks", [])
        worst = min((b for b in blocks if b.get("stub_resonance_ghz")),
                    key=lambda b: b["stub_resonance_ghz"], default=None)
        if worst is not None and worst.get("kind") == "stub" and worst.get("at_mils"):
            where = {"at_mils": worst["at_mils"], "layer": worst.get("layer", ""),
                     "where": f"stub of {worst.get('length_mils', 0):.0f} mil"
                              + (f" to {worst['to']}" if worst.get("to") else "")}
        elif worst is not None and worst.get("via") is not None:
            via = board.vias[int(worst["via"])]
            where = {"at_mils": [round(via.x, 2), round(via.y, 2)],
                     "where": f"via stub of {worst.get('stub_mils', 0):.0f} mil"}
    return where


#: Checks of the single edge at the receiver, placed at the receiving pin.
EDGE_CHECKS = ("overshoot", "ringback", "edge reversal")

#: The unit intervals an item's sweep covers when its eye is wanted, and
#: the most points it is given for that.
EYE_WINDOW_UI = 8.0
MAX_EYE_POINTS = 4000


def _delay_match(results: list[dict[str, Any]]) -> None:
    """Hold the items of each target that gives ``delay_match_ps`` to the
    slowest of them: a signal can be delayed by adding trace, never sped
    up, so the slowest sets the time the others are matched to."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for r in results:
        if r.get("delay_match_ps") is not None and "delay_ps" in r.get("metrics", {}):
            groups.setdefault(r["target"], []).append(r)
    for rs in groups.values():
        if len(rs) < 2:
            continue
        slow = max(rs, key=lambda r: r["metrics"]["delay_ps"])
        top = slow["metrics"]["delay_ps"]
        for r in rs:
            tol = float(r["delay_match_ps"])
            d = r["metrics"]["delay_ps"]
            lead = top - d
            r["metrics"]["delay_lead_ps"] = round(lead, 3)
            ok = lead <= tol
            if r is slow:
                detail = f"{d:.1f} ps, the slowest of the {len(rs)} matched to it"
            else:
                detail = (f"{lead:.1f} ps ahead of {slow['kind']} {slow['name']} "
                          f"({top:.1f} ps), {tol:g} ps allowed")
                length = r["metrics"].get("length_mils")
                if not ok and length and d > 0:
                    detail += f": about {lead * length / d:.0f} mil more trace matches it"
            r["verdicts"].append({"check": "delay match", "pass": bool(ok), "detail": detail})
            if not ok:
                r["findings"].append({"check": "delay match", "severity": "error",
                                      "detail": detail, **r.get("start", {})})


def _item_spec(item: dict[str, Any], spec: dict[str, Any], f_max: float, points: int
               ) -> dict[str, Any]:
    s = {k: v for k, v in item["settings"].items() if k in _NET_KEYS}
    for k in ("temperature_c", "thermal_npz", "tand", "foil", "mask_mils", "etch_mils",
              "xtalk", "xtalk_max_pct", "xtalk_reach_mils", "xtalk_quiet",
              "xtalk_aggressors", "xtalk_swing_mv", "xtalk_max_mv",
              "return_path", "return_gap_min_mils"):
        if k in spec and k not in s:
            s[k] = spec[k]
    if "data_rate_gbps" not in s and spec.get("data_rate_gbps") is not None:
        s["data_rate_gbps"] = spec["data_rate_gbps"]
    s.setdefault("f_max_ghz", f_max)
    s.setdefault("points", points)
    if item["settings"].get("eye", spec.get("eye", False)) and s.get("data_rate_gbps"):
        # an eye needs the sweep's step to cover several bits: a slow net
        # on a wide sweep gets more points, not a refused eye
        need = math.ceil(EYE_WINDOW_UI * float(s["f_max_ghz"]) / float(s["data_rate_gbps"]))
        s["points"] = min(max(int(s["points"]), need), MAX_EYE_POINTS)
    s["rules"] = False
    s["pair" if item["kind"] == "pair" else "net"] = item["name"]
    return s


def _one(item: dict[str, Any], spec: dict[str, Any], board_data: dict, stackup: dict,
         board: LayoutBoard, folder: Path, f_max: float, points: int) -> dict[str, Any]:
    sub = folder / "items" / _safe(f"{item['kind']}_{item['name']}")
    s = _item_spec(item, spec, f_max, points)
    out: dict[str, Any] = {"kind": item["kind"], "name": item["name"], "target": item["target"],
                           "folder": str(sub.relative_to(folder)), "findings": []}
    if item["settings"].get("delay_match_ps") is not None:
        out["delay_match_ps"] = float(item["settings"]["delay_match_ps"])
        if out["delay_match_ps"] < 0:
            raise ValueError(f"target {item['target']}: delay_match_ps is 0 or more")
    try:
        res = run_net_channel(s, board_data, stackup, sub)
    except Exception as exc:  # noqa: BLE001 - one bad net must not stop the board
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["findings"].append({"check": "modelled", "severity": "warning",
                                "detail": f"not modelled: {exc}"})
        return out
    out["metrics"] = {k: v["value"] for k, v in res["metrics"].items()}
    out["verdicts"] = res["verdicts"]
    out["files"] = {k: f"{out['folder']}/{v}" for k, v in res.get("files", {}).items()}
    pair = item["kind"] == "pair"
    try:
        if pair:
            pos, neg = extract.find_pair(board, item["name"])
            pp = extract.pair_path(board, item["name"], pos, neg)
            paths = (None, pp.p)
        else:
            paths = (extract.net_path(board, item["name"]), None)
    except Exception:  # noqa: BLE001
        paths = (None, None)
    first = paths[1] if pair else paths[0]
    if first is not None:
        xy = _pin_xy(board, first.start)
        if xy is not None:
            out["start"] = {"at_mils": [round(xy[0], 2), round(xy[1], 2)],
                            "where": f"at {first.start}"}
    z_ref = float(s.get("z_ref", 50))
    z_target = float(s.get("z_target", 2 * z_ref if pair else z_ref))
    rise = float(s.get("rise_ps", 35)) * 1e-12
    want_eye = bool(item["settings"].get("eye", spec.get("eye", False)))
    if want_eye and s.get("data_rate_gbps"):
        from . import eye_jobs
        es = {k: v for k, v in item["settings"].items() if k in _EYE_KEYS}
        es["data_rate_gbps"] = s["data_rate_gbps"]
        es["repeats"] = 1
        xt = out["metrics"].get("crosstalk_pct")
        if xt:
            # each neighbour at its own swing; the eye's amplitude where none is given
            swing = s.get("xtalk_swing_mv")
            swing = float(es.get("amplitude_mv", 800)) if swing is None else float(swing)
            es["noise_mv"] = crosstalk.noise_mv(
                {"aggressors": res.get("figures", {}).get("crosstalk") or []}, max(swing, 0.0))
        try:
            ts = sub / res["files"]["touchstone"]
            er = eye_jobs.run_convolution(es, ts, sub / "eye")
            em = {k: v["value"] for k, v in er["metrics"].items()}
            out["metrics"].update({f"eye_{k}" if not k.startswith(("eye", "worst", "bit_error")) else k: v
                                   for k, v in em.items() if k != "insertion_loss_db_at_nyquist"})
            if "plot" in er.get("files", {}):
                out["files"]["eye"] = f"{out['folder']}/eye/{er['files']['plot']}"
            lim_mv = item["settings"].get("eye_min_mv")
            lim_ui = item["settings"].get("eye_min_ui")
            h, w = em.get("eye_height_with_noise_mv", em["eye_height_mv"]), em["eye_width_ui"]
            noisy = (f" with {em['noise_mv']:g} mV of crosstalk on each rail "
                     f"({em['eye_height_mv']:.1f} mV without)" if "noise_mv" in em else "")
            ok = h > (float(lim_mv) if lim_mv is not None else 0.0) and \
                (lim_ui is None or w >= float(lim_ui))
            need = []
            if lim_mv is not None:
                need.append(f"{float(lim_mv):g} mV")
            if lim_ui is not None:
                need.append(f"{float(lim_ui):g} UI")
            out["verdicts"] += [v for v in er["verdicts"]
                                if v["check"] in EDGE_CHECKS + ("eye at the bit error rate",)]
            out["verdicts"].append({"check": "eye", "pass": bool(ok),
                                    "detail": f"{h:.1f} mV by {w:.3f} UI{noisy}"
                                              + (f", {' and '.join(need)} needed" if need
                                                 else ", must be open")})
        except Exception as exc:  # noqa: BLE001
            out["findings"].append({"check": "eye", "severity": "warning",
                                    "detail": f"eye not computed: {exc}"})
    for v in out["verdicts"]:
        if v["pass"]:
            continue
        f = {"check": v["check"],
             "severity": v.get("severity") or ("warning" if v["check"] in WARNING_CHECKS
                                               else "error"),
             "detail": v["detail"]}
        f.update(_locate(board, item, v, res, sub, rise, z_target, paths))
        out["findings"].append(f)
        if v["check"] == "return path":
            for g in (res.get("figures", {}).get("return_path") or [])[1:MAX_EXTRA_FAULTS + 1]:
                out["findings"].append({
                    "check": "return path",
                    "severity": "warning" if g["other_reference_solid"] else f["severity"],
                    "detail": f"{g['text']} on {g['layer']}"
                              + ("; the other reference plane is whole there"
                                 if g["other_reference_solid"] else ""),
                    "at_mils": g["at_mils"], "layer": g["layer"],
                    "where": f"{g['distance_mils']:.0f} mil along the route, {g['text']}"})
        if v["check"] == "impedance":
            tol = float(s.get("z_tol_pct", 10)) / 100
            out["findings"].extend(_more_impedance(item, res, sub, rise, z_target, tol, paths, f))
    return out


def run_board_si(spec: dict[str, Any], board_data: dict, stackup: Optional[dict],
                 folder: Path, progress=None, snapshot: str = "") -> dict[str, Any]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *_: None)
    if not stackup:
        raise ValueError("board SI needs the board's stackup")
    board = LayoutBoard.from_dict(board_data)
    items = select(board, spec)
    if not items:
        raise ValueError("no net or pair matches the targets")
    cap = int(_num(spec, "max_items", 40))
    skipped = items[cap:]
    items = items[:cap]
    rates = [float(it["settings"].get("data_rate_gbps", spec.get("data_rate_gbps") or 0))
             for it in items]
    for it, r in zip(items, rates):
        if r <= 0:
            raise ValueError(f"{it['kind']} {it['name']}: no data rate (give data_rate_gbps "
                             "in its target or for the job)")
    f_max = _num(spec, "f_max_ghz", max(10.0, 1.5 * max(rates)))
    points = int(_num(spec, "points", 200))
    workers = max(1, min(int(_num(spec, "workers", 4)), 8))
    say(f"checking {len(items)} nets and pairs", 0.02)
    done = [0]

    def work(it):
        r = _one(it, spec, board_data, stackup, board, folder, f_max, points)
        done[0] += 1
        say(f"{it['kind']} {it['name']}", 0.05 + 0.85 * done[0] / len(items))
        return r

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, items))
    _delay_match(results)

    findings = []
    for r in results:
        for f in r["findings"]:
            findings.append({"id": f"F{len(findings) + 1}", "kind": r["kind"], "item": r["name"],
                             "folder": r["folder"], **f})
    order = {"error": 0, "warning": 1}
    findings.sort(key=lambda f: (order.get(f["severity"], 2), f["item"], f["check"]))
    for i, f in enumerate(findings):
        f["id"] = f"F{i + 1}"
    errors = sum(f["severity"] == "error" for f in findings)
    warnings = sum(f["severity"] == "warning" for f in findings)
    failed = sorted({f["item"] for f in findings if f["severity"] == "error"})
    data = {"board": board.name, "snapshot": snapshot, "items": results, "findings": findings,
            "skipped": [f"{it['kind']} {it['name']}" for it in skipped]}
    (folder / "findings.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    _summary_csv(folder / "summary.csv", results)
    say("report", 0.95)
    (folder / "report.html").write_text(report_html(data, folder), encoding="utf-8")

    m: dict[str, Any] = {
        "items_checked": _metric(len(results), "", "Nets and pairs checked"),
        "items_failed": _metric(len(failed), "", "Nets and pairs with an error"),
        "errors": _metric(errors, "", "Findings that fail a limit"),
        "warnings": _metric(warnings, "", "Findings to look at"),
    }
    il = [r["metrics"]["insertion_loss_db"] for r in results if "metrics" in r]
    if il:
        m["worst_insertion_loss_db"] = _metric(round(max(il), 3), "dB",
                                               "Highest insertion loss at Nyquist")
    eyes = [r["metrics"]["eye_height_mv"] for r in results
            if "metrics" in r and "eye_height_mv" in r["metrics"]]
    if eyes:
        m["smallest_eye_mv"] = _metric(round(min(eyes), 2), "mV", "Smallest eye height")
    verdicts = [{"check": f"{r['kind']} {r['name']}",
                 "pass": not any(f["severity"] == "error" for f in r["findings"]),
                 "detail": "; ".join(f"{f['check']}: {f['detail']}" for f in r["findings"]
                                     if f["severity"] == "error") or "within its limits"}
                for r in results]
    notes = [f"{len(results)} items, each a net-channel model with its target's limits; "
             "findings are placed where the TDR, the stub or the driving pin puts them"]
    if skipped:
        notes.append(f"{len(skipped)} more items matched but were not checked (max_items "
                     f"{cap}): {', '.join(data['skipped'][:10])}"
                     + (" ..." if len(skipped) > 10 else ""))
    return {"metrics": m, "verdicts": verdicts, "notes": notes,
            "files": {"report": "report.html", "findings": "findings.json",
                      "summary": "summary.csv"},
            "figures": {"findings": findings}}


def _summary_csv(path: Path, results: list[dict[str, Any]]) -> None:
    keys = ["insertion_loss_db", "return_loss_db", "line_loss_db", "delay_ps", "tdr_min_ohms",
            "tdr_max_ohms", "delay_lead_ps", "skew_ps", "mode_conversion_db", "stub_resonance_ghz",
            "crosstalk_next_pct", "crosstalk_fext_pct", "return_gaps", "return_gap_mils",
            "length_mils", "vias", "eye_height_mv", "eye_width_ui"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "name", "errors", "warnings"] + keys)
        for r in results:
            mm = r.get("metrics", {})
            w.writerow([r["kind"], r["name"],
                        sum(f["severity"] == "error" for f in r["findings"]),
                        sum(f["severity"] == "warning" for f in r["findings"])]
                       + [mm.get(k, "") for k in keys])


_CSS = """
:root{--bg:#fff;--fg:#1d1f23;--mut:#5d6470;--line:#d9dde3;--err:#b42318;--warn:#a15c07;
--ok:#1a7f37;--card:#f6f7f9}
@media (prefers-color-scheme:dark){:root{--bg:#15171a;--fg:#e6e8eb;--mut:#9aa1ab;
--line:#2c3036;--err:#ff7b72;--warn:#e3b341;--ok:#56d364;--card:#1c1f23}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;
padding:24px 16px;max-width:1100px;margin:auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.mut{color:var(--mut)}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}
.error{color:var(--err);font-weight:600}.warning{color:var(--warn);font-weight:600}
.ok{color:var(--ok)}.tiles{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.tile{background:var(--card);border-radius:8px;padding:10px 14px;min-width:120px}
.tile b{display:block;font-size:22px}details{margin:6px 0}
img{max-width:100%;border-radius:6px;background:#fff}.wrap{overflow-x:auto}
"""


def _img(folder: Path, rel: Optional[str]) -> str:
    if not rel:
        return ""
    p = folder / rel
    try:
        data = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f'<img alt="{html.escape(rel)}" src="data:image/png;base64,{data}">'


def report_html(data: dict[str, Any], folder: Path) -> str:
    e = html.escape
    items = data["items"]
    findings = data["findings"]
    errs = sum(f["severity"] == "error" for f in findings)
    warns = sum(f["severity"] == "warning" for f in findings)
    failed = len({f["item"] for f in findings if f["severity"] == "error"})
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Board SI report</title><style>{_CSS}</style></head><body>",
           f"<h1>Board SI report: {e(data['board'] or 'board')}</h1>",
           f"<div class=mut>snapshot {e(data.get('snapshot') or 'from files')}</div>",
           "<div class=tiles>",
           f"<div class=tile><b>{len(items)}</b><span class=mut>checked</span></div>",
           f"<div class=tile><b class={'error' if failed else 'ok'}>{failed}</b>"
           f"<span class=mut>failing</span></div>",
           f"<div class=tile><b>{errs}</b><span class=mut>errors</span></div>",
           f"<div class=tile><b>{warns}</b><span class=mut>warnings</span></div></div>"]
    out.append("<h2>Findings</h2>")
    if findings:
        out.append("<div class=wrap><table><tr><th>ID</th><th>Severity</th><th>Item</th>"
                   "<th>Check</th><th>Detail</th><th>Where</th></tr>")
        for f in findings:
            at = f.get("at_mils")
            where = e(f.get("where", ""))
            if at:
                where += f" <span class=mut>({at[0]:.0f}, {at[1]:.0f} mil"
                where += f", {e(f['layer'])})" if f.get("layer") else ")"
                where += "</span>"
            out.append(f"<tr><td>{e(f['id'])}</td><td class={e(f['severity'])}>"
                       f"{e(f['severity'])}</td><td>{e(f['kind'])} {e(f['item'])}</td>"
                       f"<td>{e(f['check'])}</td><td>{e(f['detail'])}</td><td>{where}</td></tr>")
        out.append("</table></div>")
    else:
        out.append("<p class=ok>Every net and pair is within its limits.</p>")
    cols = [("insertion_loss_db", "IL dB"), ("return_loss_db", "RL dB"), ("delay_ps", "delay ps"),
            ("tdr_min_ohms", "TDR min"), ("tdr_max_ohms", "TDR max"), ("skew_ps", "skew ps"),
            ("stub_resonance_ghz", "stub GHz"), ("crosstalk_pct", "xtalk %"), ("eye_height_mv", "eye mV")]
    out.append("<h2>Nets and pairs</h2><div class=wrap><table><tr><th>Item</th><th>Status</th>"
               + "".join(f"<th class=num>{e(c)}</th>" for _, c in cols) + "</tr>")
    for r in items:
        mm = r.get("metrics", {})
        bad = [f for f in r["findings"] if f["severity"] == "error"]
        status = (f"<span class=error>{len(bad)} error{'s' if len(bad) != 1 else ''}</span>"
                  if bad else ("<span class=warning>not modelled</span>" if "error" in r
                               else "<span class=ok>pass</span>"))
        cells = "".join(f"<td class=num>{mm[k]:.4g}</td>" if isinstance(mm.get(k), (int, float))
                        else "<td class=num>-</td>" for k, _ in cols)
        out.append(f"<tr><td>{e(r['kind'])} {e(r['name'])}</td><td>{status}</td>{cells}</tr>")
    out.append("</table></div><h2>Details</h2>")
    for r in items:
        files = r.get("files", {})
        out.append(f"<details><summary>{e(r['kind'])} {e(r['name'])}</summary>")
        for v in r.get("verdicts", []):
            cls = "ok" if v["pass"] else "error"
            out.append(f"<div><span class={cls}>{'pass' if v['pass'] else 'fail'}</span> "
                       f"{e(v['check'])}: {e(v['detail'])}</div>")
        if r.get("error"):
            out.append(f"<div class=warning>{e(r['error'])}</div>")
        out.append(_img(folder, files.get("plot")))
        out.append(_img(folder, files.get("eye")))
        out.append("</details>")
    if data.get("skipped"):
        out.append(f"<p class=mut>Not checked (over max_items): {e(', '.join(data['skipped']))}</p>")
    out.append("</body></html>")
    return "\n".join(out)


__all__ = ["BOARD_SPEC", "select", "run_board_si", "report_html"]
