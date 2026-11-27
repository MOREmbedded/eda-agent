# SPDX-License-Identifier: Apache-2.0
"""Watch one ``hertzon-cli`` run and record how it ended.

Started by ``eda_agent.simulation.hertzon.start_solve`` as its own process
(``python -I hertzon_runner.py FOLDER -- CMD ...``), so a solve outlives
the eda-agent session that started it. Hertzon is GPL-2.0-or-later: it is
only ever run as a program, never imported, and this file uses the
standard library alone.

In ``FOLDER`` it writes:

- ``progress.jsonl``: Hertzon's progress events (its stderr, one JSON
  object per line with ``--progress json``),
- ``summary.json``: Hertzon's summary (its stdout with ``--json``),
- ``exit.json``: ``{"code": <exit code>, "finished": <time>}`` once the
  program has ended. Its presence is what marks the run as over.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def _write_exit(folder: str, code: int, error: str = "") -> None:
    data = {"code": int(code), "finished": time.time()}
    if error:
        data["error"] = error
    tmp = os.path.join(folder, "exit.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, os.path.join(folder, "exit.json"))


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: hertzon_runner.py FOLDER -- CMD [ARGS...]", file=sys.stderr)
        return 2
    folder, cmd = argv[0], argv[2:]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    extra = {"creationflags": flags} if flags else {}
    try:
        with open(os.path.join(folder, "summary.json"), "wb") as out, \
                open(os.path.join(folder, "progress.jsonl"), "wb") as err:
            proc = subprocess.Popen(cmd, cwd=folder, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, **extra)
            code = proc.wait()
    except OSError as exc:
        _write_exit(folder, 127, f"could not start {cmd[0]}: {exc}")
        return 127
    _write_exit(folder, code)
    return 0


if __name__ == "__main__":  # pragma: no cover - run as a program
    sys.exit(main(sys.argv[1:]))
