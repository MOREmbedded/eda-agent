# SPDX-License-Identifier: Apache-2.0
"""Cross-check a calculator against pcb-toolkit, when it is installed.

`pcb-toolkit <https://github.com/akiselev/pcb-toolkit>`_ (Rust, MIT OR
Apache-2.0) implements the same published closed forms as
``engineering/pcb_calc.py``, and upstream validates several of them
against independent evaluations. A second implementation disagreeing
is the cheapest evidence that one of them is wrong.

It is OPTIONAL and will not be present for most people: the crate on
crates.io is library-only, so the CLI has to be built from source with
a Rust toolchain. Nothing here depends on it, nothing degrades without
it, and the refusal when it is absent says how to get it rather than
just reporting a missing file.

HOW THE COMPARISON AVOIDS GUESSING. The two tools' JSON replies do not
share field names, and this project has not run the binary, so a
hardcoded "their field X is our field Y" mapping would be invented
rather than known. Instead both replies are returned in full, and
fields are paired by NORMALISED NAME -- the same fold
``engineering/part_match.py`` uses on catalogue attributes. Pairs that
match are reported as agreements, pairs that differ as disagreements,
and anything unpaired is listed so the reader can see what was not
compared. A pair that happens to share a name and not a meaning shows
up as a disagreement, which is the safe direction for it to fail.

The ARGUMENT mapping is not guessed either: every flag below was read
from pcb-toolkit's own clap definitions. Four calculators are
deliberately unmapped and say why.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Optional

__all__ = [
    "UNMAPPED",
    "cross_check",
    "find_binary",
]

#: Our calculator -> (pcb-toolkit argv after the subcommand is chosen,
#: built from our keyword arguments).
#: Flags transcribed from the clap `Args` structs in
#: crates/pcb-toolkit-cli/src/commands/. A `Length` or `Freq` argument
#: accepts a unit suffix, which is why several values are sent with one
#: rather than as a bare number in whatever unit the flag defaults to.
_MAPPING: dict[str, dict[str, Any]] = {
    "ipc2221_spacing": {
        "subcommand": ["spacing"],
        "build": lambda kw: ["--voltage", str(kw["voltage"]),
                             "--device-type", str(kw.get("environment",
                                                         "B2")).lower()],
        "caveat": ("pcb-toolkit targets IPC-2221C and takes classes "
                   "b1-b5 / a6-a8; this module carries the A/B table "
                   "with b1-b4 / a5-a7. Revision C REDEFINES those "
                   "columns, so the same letter is a different "
                   "environment and a disagreement here may be the "
                   "revision rather than an error."),
    },
    "fusing_current": {
        "subcommand": ["fusing"],
        "build": lambda kw: ["--width", f"{kw['width_mils']}mil",
                             "--copper", f"{kw.get('copper_oz', 1.0)}oz",
                             "--time", str(kw.get("time_s", 1.0)),
                             "--ambient", str(kw.get("ambient_c", 25.0))],
        "caveat": ("pcb-toolkit also models plating and etch factor, "
                   "which this does not, so its defaults are used"),
    },
    "wavelength_in_dielectric": {
        "subcommand": ["wavelength"],
        "build": lambda kw: ["--freq", f"{kw['frequency_hz']}Hz",
                             "--er", str(kw.get("dielectric_constant",
                                                4.3))],
        "caveat": "",
    },
    "reactance": {
        "subcommand": ["reactance"],
        "build": lambda kw: (
            ["--freq", f"{kw['frequency_hz']}Hz"]
            + (["--cap", f"{kw['capacitance_f']}F"]
               if kw.get("capacitance_f") else [])
            + (["--ind", f"{kw['inductance_h']}H"]
               if kw.get("inductance_h") else [])),
        "caveat": "",
    },
    "padstack_geometry": {
        "subcommand": ["padstack", "thru-hole"],
        "build": lambda kw: ["--hole", f"{kw['drill_mils']}mil",
                             "--ring", f"{kw['annular_ring_mils']}mil",
                             "--isolation",
                             f"{kw.get('clearance_mils') or 8}mil"],
        "caveat": ("pcb-toolkit's thru-hole padstack requires an "
                   "isolation figure; 8 mil is sent when none is given, "
                   "and it does not take a neighbour pitch"),
    },
    "awg_properties": {
        "subcommand": ["wire-gauge"],
        "build": lambda kw: ["--awg", str(kw["awg"])],
        "caveat": ("pcb-toolkit reports resistance per 1000 ft; this "
                   "reports it per metre, so those two will not pair"),
    },
}

#: Calculators deliberately NOT mapped, with the reason. Listed rather
#: than omitted: "no mapping" and "I forgot" look identical otherwise.
UNMAPPED = {
    "via_parasitics": ("pcb-toolkit's `via` flags were not read from "
                       "its source, and a guessed flag name fails at "
                       "the moment someone runs it"),
    "spiral_inductor": ("same: `inductor`'s flags were not read"),
    "pdn_target_impedance": (
        "pcb-toolkit's `pdn` takes a richer model (current step, "
        "plane area, spreading distance, frequency) than this one's "
        "voltage/ripple/current definition. They answer different "
        "questions, so pairing them would compare two things that are "
        "not the same quantity"),
    "resonant_frequency": (
        "pcb-toolkit folds resonance into `reactance`; cross-check "
        "`reactance` instead, which is mapped"),
}

#: Where to look for the binary when PATH does not have it.
_ENV_VAR = "EDA_AGENT_PCB_TOOLKIT"

_BUILD_HELP = (
    "pcb-toolkit is optional and is not published as a binary: the "
    "crate on crates.io is library-only. To build it:\n"
    "  git clone https://github.com/akiselev/pcb-toolkit\n"
    "  cd pcb-toolkit && cargo build --release --workspace\n"
    "then put target/release/pcb-toolkit on PATH, or point "
    f"{_ENV_VAR} at it. Nothing in eda-agent needs it; it is a second "
    "opinion, not a dependency."
)


def _hidden_window_kwargs() -> dict:
    """Keep a console window from flashing on Windows.

    Same treatment the LTspice runner already needed: a short-lived
    child process that pops a window is disruptive when something calls
    it in a loop.
    """
    import sys

    if sys.platform != "win32":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": startup,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW",
                                     0x08000000)}


def find_binary() -> Optional[str]:
    """Path to a pcb-toolkit binary, or None.

    The environment variable wins over PATH so a locally built one can
    be used without installing it anywhere.
    """
    override = os.environ.get(_ENV_VAR, "").strip()
    if override:
        return override if os.path.isfile(override) else None
    return shutil.which("pcb-toolkit")


def _normalise(key: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "", str(key or "").lower())


def _numbers(payload: Any) -> dict[str, float]:
    """Flatten a reply to {normalised key: number}, one level deep.

    One level on purpose: pairing a nested field against a top-level
    one of the same name would compare two different quantities, and
    the point of this is to catch exactly that kind of mistake rather
    than make it.
    """
    out: dict[str, float] = {}
    if not isinstance(payload, dict):
        return out
    for key, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        normalised = _normalise(key)
        if normalised:
            out.setdefault(normalised, float(value))
    return out


def cross_check(calculator: str, kwargs: dict[str, Any], *,
                rel_tol: float = 0.01,
                timeout_s: float = 20.0) -> dict[str, Any]:
    """Run one calculator here and in pcb-toolkit, and compare.

    Returns both replies in full plus ``agreements``, ``disagreements``
    and ``unpaired``. Fields pair by normalised name; nothing is
    translated through a mapping this project has not verified.
    """
    from . import pcb_calc

    if calculator in UNMAPPED:
        return {"ok": False, "reason":
                f"{calculator} is not mapped to a pcb-toolkit command: "
                f"{UNMAPPED[calculator]}",
                "unmapped": sorted(UNMAPPED)}
    entry = _MAPPING.get(calculator)
    if entry is None:
        return {"ok": False, "reason":
                f"unknown calculator {calculator!r}; mapped: "
                f"{sorted(_MAPPING)}; deliberately unmapped: "
                f"{sorted(UNMAPPED)}"}

    binary = find_binary()
    if not binary:
        return {"ok": False, "reason":
                "pcb-toolkit is not installed, so there is nothing to "
                "cross-check against.\n" + _BUILD_HELP}

    try:
        ours = getattr(pcb_calc, calculator)(**kwargs)
    except TypeError as exc:
        # The arguments come from a tool call or a CLI flag, so a
        # missing or misspelled one is ordinary user input and belongs
        # in a refusal rather than a traceback.
        return {"ok": False, "reason":
                f"{calculator} cannot be called with those arguments: "
                f"{exc}", "given": sorted(kwargs)}
    if not ours.get("ok"):
        return {"ok": False, "reason":
                f"the local calculator refused before anything was "
                f"compared: {ours.get('reason')}"}

    try:
        argv = [binary, "--json"] + list(entry["subcommand"]) \
            + list(entry["build"](kwargs))
    except KeyError as exc:
        return {"ok": False, "reason":
                f"the cross-check needs {exc} for {calculator}"}

    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            check=False, **_hidden_window_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": f"could not run pcb-toolkit: {exc}",
                "command": argv}
    if completed.returncode != 0:
        return {"ok": False, "reason":
                f"pcb-toolkit exited {completed.returncode}: "
                f"{(completed.stderr or '').strip()[:400]}",
                "command": argv, "ours": ours}
    try:
        theirs = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {"ok": False, "reason":
                f"pcb-toolkit's output was not JSON: {exc}",
                "command": argv, "stdout": (completed.stdout or "")[:400],
                "ours": ours}

    mine = _numbers(ours)
    other = _numbers(theirs)
    agreements, disagreements = [], []
    for key in sorted(set(mine) & set(other)):
        a, b = mine[key], other[key]
        close = abs(a - b) <= max(abs(a), abs(b)) * rel_tol
        record = {"field": key, "ours": a, "theirs": b,
                  "delta": round(b - a, 6)}
        (agreements if close else disagreements).append(record)

    return {
        "ok": True,
        "calculator": calculator,
        "binary": binary,
        "command": argv,
        "ours": ours,
        "theirs": theirs,
        "agreements": agreements,
        "disagreements": disagreements,
        "unpaired_ours": sorted(set(mine) - set(other)),
        "unpaired_theirs": sorted(set(other) - set(mine)),
        "rel_tol": rel_tol,
        "caveat": entry.get("caveat", ""),
        "note": ("fields pair by NAME, not through a verified mapping. "
                 "A pair sharing a name and not a meaning shows up as a "
                 "disagreement, which is the safe direction. Unpaired "
                 "fields were not compared at all."),
    }
