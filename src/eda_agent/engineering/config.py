# SPDX-License-Identifier: Apache-2.0
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class LTspicePolicy:
    exe: str
    sim_root: Path
    model_roots: tuple[Path, ...] = ()
    timeout_s: float = 300.0
    max_parallel: int = 2
    max_points_returned: int = 2000
    exe_prefix: tuple[str, ...] = ()


