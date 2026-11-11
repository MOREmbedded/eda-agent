# Development

How the code is laid out, how to run the tests, and the versioning rules that keep Altium and Python in step.

## Architecture

```
    +-----------------------------+
    |    MCP-compatible client    |
    +-----------------------------+
            |              ^
            v              |
       tool call       tool result
       (JSON-RPC)      (JSON-RPC)
            |              |
            v              |
    +-----------------------------+
    |     eda-agent (Python)      |
    | application / project / lib |
    | / generic / pcb / design    |
    |              |              |
    |     Altium bridge (IPC)     |
    +-----------------------------+
            |              ^
            v              |
   request_<id>.json   response_<id>.json
            |              |
            v              |
    +-----------------------------+
    |      Altium Designer        |
    |  DelphiScript polling loop  |
    |     (Altium_API.PrjScr)     |
    +-----------------------------+
```

All intelligence lives in Python. The DelphiScript side is a pass-through layer for object iteration, property access, and process execution.

## Running the tests

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

The test suite includes a Python Altium simulator for end-to-end integration tests, Free Pascal cross-validation that runs the actual DelphiScript functions against Python mirrors, and a regression suite for previously encountered edge cases.

Rebuild the monolithic DelphiScript file after editing sources under `scripts/altium/`:

```bash
cd scripts/altium
python build.py
```

## Project layout

```
eda-agent/
├── src/eda_agent/          Python package
│   ├── bridge/             Communication with each editor (Altium IPC, EasyEDA WebSocket)
│   ├── schemas/            Pydantic IPC envelope + per-command schemas
│   ├── tools/              MCP tool implementations
│   ├── core/               EDA-agnostic design snapshot and review
│   ├── design/             Design agent: plan / inventory / discipline / executor / validator
│   ├── layout/, placement/, route/   Board geometry, rules, placement and routing engines
│   ├── libimport/          Part providers and vendor library import
│   ├── render/, export/    Drawing designs to SVG/PNG; file-format exporters
│   ├── fileio/, sim/       Offline readers (opt-in fallback); simulation results and settings
│   ├── ui/                 Windows UI automation for dialogs and menus
│   ├── web/                Local web dashboard
│   ├── diag/               Health and doctor checks
│   ├── cli.py              CLI subcommands
│   └── server.py           MCP server entry point
├── scripts/altium/         DelphiScript sources (dev source of truth)
│   ├── Main.pas, Utils.pas, Dispatcher.pas, …
│   └── Altium_API.PrjScr   Altium script project
├── extensions/easyeda/     The EasyEDA Pro extension (the editor half of that backend)
├── skills/                 Client skill files (autodesign)
├── docs/                   Backends, tool reference, release verification and other guides
└── tests/                  Python + Free Pascal test suite
```

At wheel build time `scripts/altium/` is copied into `src/eda_agent/scripts/` inside the wheel (via Hatchling `force-include`), so `eda-agent install-scripts` always finds the scripts.

## Two version numbers, and why they differ

There are two, they cover different artifacts, and they move
independently. Seeing a year where you expect a semantic version is
the usual moment of confusion:

| Artifact | Example | Scheme | Where |
|---|---|---|---|
| The Python package | `0.7.1` | Semantic versioning | `pyproject.toml`, `eda_agent.__version__` |
| The Altium DelphiScript | `2026.10.25.2` | Date, `YYYY.MM.DD.N` | `SCRIPT_VERSION` in `scripts/altium/Main.pas` |

The package version is the one you install and the one that follows
semver. The script version exists to answer a different question:
**is the Altium session running today's script, or one from last
week?** `app_ping` reports what Altium has COMPILED, and
`eda-agent update` compares that against what is on disk.

A date suits that job in a way a release number would not. A `.pas`
file changes several times in an afternoon while the bridge is being
worked on, with no relationship to whether the Python package gained a
feature or broke an interface, and the only thing a reader needs from
it is whether it is newer than the copy Altium compiled. `2026.10.25.2`
answers that at a glance; `0.7.1` would not move at all.

**Bump `SCRIPT_VERSION` on every change to any `.pas` file**, even a
second edit on the same day: that is what the trailing `.N` is for.
Leaving it unchanged makes a reloaded Altium and a stale one
indistinguishable, and `update` will then report a session as current
while it runs the old code. That has caused real confusion here more
than once.

## Where the Altium scripts are deployed

`install-scripts`, `eda-agent health` and `eda-agent update` use the
folder Altium itself loads `Altium_API.PrjScr` from, read from the
Global Projects list Altium keeps in the registry
(`HKCU\Software\Altium\<install>\DesignExplorer\Preferences\ScriptingSystem\Global Projects`).
Only when no install registers it, or several installs register
*different* folders, do they fall back to `%USERPROFILE%\EDA Agent\scripts\`,
and `update` says which of those happened.

This used to be the fixed default regardless, so with the Global
Project registered elsewhere those commands reported "deployed scripts
current" about a folder Altium never reads, while Altium compiled
something days old. `eda-agent update` still pings Altium for the
version it actually **compiled** and compares the two, which catches
any gap the registry cannot explain. To deploy somewhere specific:

```bash
eda-agent install-scripts --force --dest <that folder>
```
