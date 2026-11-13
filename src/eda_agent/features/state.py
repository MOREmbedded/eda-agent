# SPDX-License-Identifier: Apache-2.0
"""Which switchable features the user turned off.

Kept per user in ``~/.eda-agent/features.json`` (``EDA_AGENT_FEATURES_FILE``
names another file), as ``{"off": ["openems", ...]}``. Only features marked
``switchable`` can be off; everything else is on when it is installed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def state_file() -> Path:
    named = os.environ.get("EDA_AGENT_FEATURES_FILE", "").strip()
    return Path(named) if named else Path.home() / ".eda-agent" / "features.json"


def switched_off() -> set[str]:
    try:
        data = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    off = data.get("off") if isinstance(data, dict) else None
    return {str(x) for x in off} if isinstance(off, list) else set()


def set_switch(feature_id: str, on: bool) -> set[str]:
    """Turn a switchable feature on or off; returns the features now off."""
    from .registry import all_features
    feats = {f.id: f for f in all_features()}
    if feature_id not in feats:
        raise KeyError(f"unknown feature {feature_id!r}")
    if not feats[feature_id].switchable:
        raise ValueError(f"{feature_id!r} cannot be switched off: it is on whenever "
                         "it is installed")
    off = switched_off()
    (off.discard if on else off.add)(feature_id)
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"off": sorted(off)}, indent=1), encoding="utf-8")
    from ..atomicfile import replace_with_retry
    replace_with_retry(tmp, path)
    return off


def is_on(feature_id: str) -> bool:
    return feature_id not in switched_off()
