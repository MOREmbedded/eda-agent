# SPDX-License-Identifier: Apache-2.0
"""Owner naming policy (config/ipc/naming.json).

Every symbol, footprint and component name that the system generates or accepts must match the owner's
pattern. Templates render names from tokens; unknown tokens are errors (never silently empty)."""
from __future__ import annotations

import json
import os
import re
import string
from pathlib import Path

from .errors import ValidationFailed

DEFAULT = Path(__file__).resolve().parent / "data" / "naming.json"


def _default_path() -> Path:
    d = os.environ.get("EDA_AGENT_IPC_DIR") or os.environ.get("ALTIUM_AI_IPC_DIR")
    return Path(d) / "naming.json" if d and (Path(d) / "naming.json").exists() else DEFAULT
KINDS = ("footprint", "symbol", "component")
_SAFE = re.compile(r"[^A-Za-z0-9_.+\-]")


class NamingPolicy:
    def __init__(self, path: Path | None = None, data: dict | None = None):
        self.data = data or json.loads(Path(path or _default_path()).read_text(encoding="utf-8"))
        for k in KINDS:
            if k not in self.data or "template" not in self.data[k] or "pattern" not in self.data[k]:
                raise ValidationFailed(f"naming policy: '{k}' needs template and pattern")
            re.compile(self.data[k]["pattern"])

    def mfr_short(self, manufacturer: str) -> str:
        ab = self.data.get("manufacturer_abbreviations", {})
        return ab.get(manufacturer, _SAFE.sub("", manufacturer.upper())[:8])

    def render(self, kind: str, **tokens) -> str:
        tpl = self.data[kind]["template"]
        if "manufacturer" in tokens and "mfr_short" not in tokens:
            tokens["mfr_short"] = self.mfr_short(tokens["manufacturer"])
        needed = {f for _, f, _, _ in string.Formatter().parse(tpl) if f}
        missing = sorted(needed - {k for k, v in tokens.items() if v not in (None, "")})
        if missing:
            raise ValidationFailed(f"naming template for {kind} needs tokens {missing}", details={"template": tpl})
        clean = {k: _SAFE.sub("_", str(v)).upper() for k, v in tokens.items()}
        name = tpl.format(**clean)
        self.validate(kind, name)
        return name

    def validate(self, kind: str, name: str) -> str:
        pat = self.data[kind]["pattern"]
        if not re.fullmatch(pat, name):
            raise ValidationFailed(f"{kind} name {name!r} violates owner naming pattern {pat}")
        return name

    def check_unique(self, kind: str, name: str, existing: list[str]) -> None:
        if name.upper() in {e.upper() for e in existing}:
            raise ValidationFailed(f"{kind} name {name} already exists in the target library")
