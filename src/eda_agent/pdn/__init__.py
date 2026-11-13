# SPDX-License-Identifier: Apache-2.0
"""Power distribution network analysis of a routed board.

``ir_drop``: DC voltage drop, current density and Joule loss of a power
net and its return, from the board's real copper.
``impedance``: AC impedance at the load against a target impedance, with
the plane pair as a field-solved cavity and every capacitor's mounting.
``decap``: capacitor models. ``physics``: the closed forms they rest on.
"""

from . import decap, impedance, ir_drop, physics

__all__ = ["decap", "impedance", "ir_drop", "physics", "run_folder", "runs_dir"]


def runs_dir(named: str = "", kind: str = "pdn"):
    """Where result files go: ``named``, else ``EDA_AGENT_PDN_RUNS``, else
    ``~/EDA Agent/pdn``."""
    import os
    from pathlib import Path
    base = named or os.environ.get("EDA_AGENT_PDN_RUNS", "")
    return Path(base) if base else Path.home() / "EDA Agent" / kind


def run_folder(named: str, board_name: str, net: str):
    """A new folder for one run's files, made here so that two runs never
    share one: ``<board>_<net>_<date>-<time>-<ms>`` under ``runs_dir``,
    with a counter when even that is taken. The names are cleaned of
    what Windows will not have in a file name."""
    import time
    from .ir_drop import safe_name
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
    stem = f"{safe_name(board_name or 'board')}_{safe_name(net)}_{stamp}"
    base = runs_dir(named)
    for k in range(1, 1000):
        folder = base / (stem if k == 1 else f"{stem}_{k}")
        try:
            folder.mkdir(parents=True, exist_ok=False)
            return folder
        except FileExistsError:
            continue
    raise OSError(f"no free run folder under {base}")
