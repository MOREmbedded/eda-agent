# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Check a built wheel carries what an install needs, then use it.

Run after ``python -m build``: ``python scripts/verify_distribution.py dist``.
It opens the wheel and checks for the DelphiScript files and the EasyEDA
extension source, installs it into a fresh virtual environment, and runs
the commands a user runs first. A file left out of the wheel only shows
up after somebody installs it, so CI does it first.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

REQUIRED = [
    "eda_agent/scripts/Main.pas",
    "eda_agent/scripts/Altium_API.PrjScr",
    "eda_agent/scripts/StatusForm.dfm",
    "eda_agent/easyeda_extension/main.js",
    "eda_agent/easyeda_extension/extension.json",
    "eda_agent/easyeda_extension/build.py",
    "eda_agent/server.py",
]


def main(dist: str) -> int:
    wheels = sorted(Path(dist).glob("eda_agent-*.whl"))
    if len(wheels) != 1:
        print(f"expected one wheel in {dist}, found {len(wheels)}")
        return 1
    wheel = wheels[0]
    names = set(zipfile.ZipFile(wheel).namelist())
    missing = [n for n in REQUIRED if n not in names]
    if missing:
        print(f"{wheel.name} is missing: {missing}")
        return 1
    print(f"{wheel.name}: {len(names)} files, all required present")

    with tempfile.TemporaryDirectory() as tmp:
        venv.create(tmp, with_pip=True)
        bindir = Path(tmp) / ("Scripts" if sys.platform == "win32" else "bin")
        python = bindir / ("python.exe" if sys.platform == "win32" else "python")
        subprocess.run([str(python), "-m", "pip", "install", "-q", str(wheel)],
                       check=True)
        exe = bindir / ("eda-agent.exe" if sys.platform == "win32" else "eda-agent")
        for args in (["--version"], ["scripts-path"],
                     ["easyeda-extension", "path"]):
            done = subprocess.run([str(exe), *args], capture_output=True,
                                  text=True)
            print(f"eda-agent {' '.join(args)} -> {done.returncode}: "
                  f"{(done.stdout or done.stderr).strip()}")
            if done.returncode != 0:
                return 1
            if args[0] == "easyeda-extension" and "site-packages" not in done.stdout:
                print("the installed command found a source checkout, not "
                      "the wheel's own copy")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist"))
