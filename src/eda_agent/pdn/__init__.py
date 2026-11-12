# SPDX-License-Identifier: Apache-2.0
"""Power distribution network analysis of a routed board.

``ir_drop``: DC voltage drop, current density and Joule loss of a power
net and its return, from the board's real copper.
``impedance``: AC impedance at the load against a target impedance, with
the plane pair as a field-solved cavity and every capacitor's mounting.
``decap``: capacitor models. ``physics``: the closed forms they rest on.
"""

from . import decap, impedance, ir_drop, physics

__all__ = ["decap", "impedance", "ir_drop", "physics", "runs_dir"]


def runs_dir(named: str = "", kind: str = "pdn"):
    """Where result files go: ``named``, else ``EDA_AGENT_PDN_RUNS``, else
    ``~/EDA Agent/pdn``."""
    import os
    from pathlib import Path
    base = named or os.environ.get("EDA_AGENT_PDN_RUNS", "")
    return Path(base) if base else Path.home() / "EDA Agent" / kind
