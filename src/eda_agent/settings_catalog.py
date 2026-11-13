# SPDX-License-Identifier: Apache-2.0
"""Every setting a user can change, described in one place.

``scripts/gen_options_reference.py`` turns this, together with what the
code itself declares (the CLI parser, the Config-tab choices, the
workspace.json keys, the profile file keys, the part providers' credential
variables), into ``docs/OPTIONS.md``. Nothing here is read at run time.

The descriptions are hand-written because a variable name does not say
what it is for. Whether the list is complete is not left to the writer:
``tests/test_options_reference.py`` scans the source for every
``EDA_AGENT_*`` name and fails on one missing here, or on an entry here
that the code no longer reads.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvVar:
    name: str
    group: str
    default: str
    description: str


#: Section order in the reference.
GROUPS = (
    "Server and tool surface",
    "Workspace and Altium bridge",
    "Safety and what the AI may do",
    "Web dashboard",
    "Logs and token usage",
    "Profiles, policies and design modes",
    "Altium 365 and Nexar",
    "Parts, libraries and datasheets",
    "Simulation",
    "3D models and Altium SDK",
    "KiCad and EasyEDA Pro",
    "Updates and development",
)

ENV_VARS: tuple[EnvVar, ...] = (
    # ---- Server and tool surface ----------------------------------------
    EnvVar("EDA_AGENT_BACKEND", "Server and tool surface", "altium",
           "Which EDA tool to drive: `altium`, `kicad`, `easyeda`, or `both` "
           "(Altium and KiCad together). Same as `--backend`."),
    EnvVar("EDA_AGENT_TOOLSET", "Server and tool surface", "full",
           "`minimal` advertises only `tool_catalog` and `tool_invoke`, for "
           "MCP clients that cap the tool count. Every tool stays reachable. "
           "Same as `--toolset`."),
    EnvVar("EDA_AGENT_COMPUTE", "Server and tool surface", "python",
           "`python`, `rust` or `auto`: which implementation runs the "
           "CPU-heavy loops. See [Rust compute](RUST_COMPUTE.md). Same as "
           "`--compute`."),
    EnvVar("EDA_AGENT_REMINDER_EVERY", "Server and tool surface", "10",
           "How often `app_ping` / `app_attach` repeat the datasheet-first "
           "reminder: on the first call, then every Nth. `1` sends it every "
           "time."),
    EnvVar("EDA_AGENT_HEADLESS_REVIEW", "Server and tool surface", "off",
           "`1` enables the offline review, BOM and netlist commands and "
           "tools that read `.SchDoc`/`.PrjPcb` files with no Altium "
           "running. Same as `--offline` on those commands."),
    # ---- Workspace and Altium bridge ------------------------------------
    EnvVar("EDA_AGENT_WORKSPACE", "Workspace and Altium bridge",
           r"`%USERPROFILE%\EDA Agent\workspace`",
           "The folder Python and the Altium script exchange request and "
           "response files through. Must agree on both sides; "
           "`eda-agent health` checks it."),
    EnvVar("EDA_AGENT_POINTER_FILE", "Workspace and Altium bridge",
           r"`C:\ProgramData\eda-agent\workspace-path.txt`",
           "Where Python writes the resolved workspace path for the Altium "
           "script to read (DelphiScript cannot read environment variables). "
           "Mainly for tests."),
    EnvVar("EDA_AGENT_ALTIUM_PROCESS", "Workspace and Altium bridge",
           "`X2.exe`, then `DXP.exe`",
           "The Altium process name `doctor` and the bridge look for, if "
           "yours differs."),
    EnvVar("EDA_AGENT_ALTIUM_DIR", "Workspace and Altium bridge",
           "found from the registry",
           "Altium's install folder, used to read its translated menu and "
           "dialog captions. Set it to aim at a second Altium version."),
    EnvVar("EDA_AGENT_KEEP_BOARD_IN_VIEW", "Workspace and Altium bridge", "off",
           "`1` fits the board or sheet in view after each edit, at most "
           "every two seconds. Same as Keep board in view on the Config tab; "
           "either one turns it on."),
    # ---- Safety ---------------------------------------------------------
    EnvVar("EDA_AGENT_READONLY", "Safety and what the AI may do", "off",
           "`1` refuses every command that is not a read. The stricter of "
           "this and the Config tab's Edits switch wins."),
    EnvVar("EDA_AGENT_UI_AUTOMATION", "Safety and what the AI may do", "on",
           "`0` (or `false`, `no`, `off`) refuses every synthesised "
           "keystroke, click and menu pick. Reading dialogs stays on. Read "
           "on every event, so it takes effect at once."),
    # ---- Web dashboard --------------------------------------------------
    EnvVar("EDA_AGENT_NO_DASHBOARD", "Web dashboard", "off",
           "`1` starts the MCP server without the web dashboard. Same as "
           "`--no-dashboard`."),
    EnvVar("EDA_AGENT_DISABLE_DASHBOARD", "Web dashboard", "off",
           "Alias of `EDA_AGENT_NO_DASHBOARD`."),
    EnvVar("EDA_AGENT_HEADLESS", "Web dashboard", "off",
           "Alias of `EDA_AGENT_NO_DASHBOARD`. Same as `--headless`."),
    EnvVar("EDA_AGENT_DASHBOARD_ALLOWED_HOSTS", "Web dashboard", "loopback only",
           "Comma-separated host names (no ports) the dashboard accepts in "
           "`Host` and `Origin`, for a deliberate non-loopback `--host`."),
    # ---- Logs and token usage -------------------------------------------
    EnvVar("EDA_AGENT_LOG_DIR", "Logs and token usage",
           r"`%USERPROFILE%\EDA Agent\logs`",
           "Folder of the rotating, secret-redacting server log."),
    EnvVar("EDA_AGENT_LOG_MAX_MB", "Logs and token usage", "10",
           "Rotate a log file at this size."),
    EnvVar("EDA_AGENT_LOG_BACKUPS", "Logs and token usage", "5",
           "Rotated log files kept."),
    EnvVar("EDA_AGENT_LOG_TOTAL_MAX_MB", "Logs and token usage", "200",
           "Hard cap for the whole log folder. The oldest rotated files go "
           "first."),
    EnvVar("EDA_AGENT_LOG_MIN_FREE_MB", "Logs and token usage", "1024",
           "Below this much free disk, DEBUG and INFO logging stop and one "
           "CRITICAL line says why."),
    EnvVar("EDA_AGENT_USAGE_OFF", "Logs and token usage", "off",
           "`1` stops counting the tokens each tool call costs. Same as "
           "`usage_stats: off` in preferences. See "
           "[Token usage](TOKEN_USAGE.md)."),
    EnvVar("EDA_AGENT_USAGE_DIR", "Logs and token usage",
           "the design workspace",
           "Where the token usage files are kept."),
    # ---- Profiles, policies, modes --------------------------------------
    EnvVar("EDA_AGENT_PROFILE_DIR", "Profiles, policies and design modes",
           "unset",
           "A profile folder to use, ahead of a project-local "
           "`.eda-agent/profile/` and the central `profile-use` choice. "
           "See [Profiles](PROFILES.md)."),
    EnvVar("EDA_AGENT_PHASES", "Profiles, policies and design modes",
           "the profile's `phase_gates.json`, else none",
           "NPI stages to run, comma-separated (`evt,dvt,pvt`), `all`, or "
           "`none`. Overrules the profile. Same as `--phases`."),
    EnvVar("EDA_AGENT_MODE", "Profiles, policies and design modes",
           "the profile's `default_mode`, else `full_design`",
           "The design mode a run uses when it names none. Same as "
           "`--mode`. See [Design modes](DESIGN_MODES.md)."),
    EnvVar("EDA_AGENT_POLICY_DIR", "Profiles, policies and design modes",
           "unset (nothing imposed)",
           "Folder of company policy files: `naming.json` for "
           "`lib_check_naming` / `lib_make_name`, and `rules/*.json` for "
           "`design_check_company_rules`."),
    EnvVar("EDA_AGENT_IPC_DIR", "Profiles, policies and design modes",
           "the bundled data",
           "Folder with your own `ipc7351b_goals.json`, `company_goals.json` "
           "and `naming.json` for the IPC-7351 footprint tools. "
           "`ALTIUM_AI_IPC_DIR` is read as an older name for it."),
    # ---- Altium 365 -----------------------------------------------------
    EnvVar("EDA_AGENT_NEXAR_APP_ID", "Altium 365 and Nexar", "unset",
           "Your Nexar app's client id. Turns on the Nexar route; sign in "
           "once with `eda-agent altium365-login`. With the secret below it "
           "also serves the `nexar` part search when `NEXAR_CLIENT_ID` is "
           "unset."),
    EnvVar("EDA_AGENT_NEXAR_CLIENT_SECRET", "Altium 365 and Nexar", "unset",
           "The client secret of an app made in the Nexar portal. Sent only "
           "to Nexar's token endpoint. Set it with `setx`, never in a chat. "
           "The `nexar` part search uses it with the app id above when "
           "`NEXAR_CLIENT_SECRET` is unset."),
    EnvVar("EDA_AGENT_NEXAR_REDIRECT", "Altium 365 and Nexar",
           "`http://localhost:3000/login`",
           "The redirect URL registered on your Nexar app, if it differs."),
    EnvVar("EDA_AGENT_ALTIUM365_DOMAIN", "Altium 365 and Nexar", "unset",
           "Your workspace host, for the direct route to the workspace's "
           "own API. Wins over Nexar when set."),
    EnvVar("EDA_AGENT_ALTIUM365_TOKEN", "Altium 365 and Nexar", "unset",
           "A workspace API token for the direct route (only plans whose "
           "admin pages offer API tokens)."),
    EnvVar("EDA_AGENT_ALTIUM365_WORKSPACES", "Altium 365 and Nexar",
           "every workspace the account reaches",
           "Comma-separated workspace URLs the `altium365_` tools may act "
           "on. `eda-agent start` sets it from `workspace.json`."),
    EnvVar("EDA_AGENT_ALTIUM365_TIMEOUT_S", "Altium 365 and Nexar", "60",
           "Seconds each Altium 365 request may take."),
    # ---- Parts and libraries --------------------------------------------
    EnvVar("EDA_AGENT_PART_PROVIDERS", "Parts, libraries and datasheets",
           "none",
           "Comma-separated part providers to enable for `part_search` and "
           "`part_fetch`. See [Part sourcing](PART_SOURCING.md)."),
    EnvVar("EDA_AGENT_CACHE_DIR", "Parts, libraries and datasheets",
           r"`%LOCALAPPDATA%\eda-agent\parts-cache`",
           "Where provider indexes and cached datasheets are kept."),
    EnvVar("EDA_AGENT_DATASHEET_HOSTS", "Parts, libraries and datasheets",
           "none (deny by default)",
           "Comma-separated hosts `part_cache_datasheet` may download from."),
    EnvVar("EDA_AGENT_ALTIUM_LIBRARIES", "Parts, libraries and datasheets",
           r"`%PUBLIC%\Documents\Altium\Library` and the Library and Projects folders under `%USERPROFILE%\Documents\Altium`",
           "Folders the `altium_local` provider searches, separated by `;` "
           "on Windows."),
    EnvVar("EDA_AGENT_KICAD_SYMBOL_DIR", "Parts, libraries and datasheets",
           "`KICAD_SYMBOL_DIR`, then the KiCad install",
           "KiCad symbol libraries for the `kicad_local` provider."),
    EnvVar("EDA_AGENT_KICAD_FOOTPRINT_DIR", "Parts, libraries and datasheets",
           "`KICAD_FOOTPRINT_DIR`, then the KiCad install",
           "KiCad footprint libraries for the `kicad_local` provider."),
    EnvVar("EDA_AGENT_KICAD_3DMODEL_DIR", "Parts, libraries and datasheets",
           "`KICAD_3DMODEL_DIR`, then the KiCad install",
           "KiCad 3D models for the `kicad_local` provider."),
    # ---- Simulation -----------------------------------------------------
    EnvVar("EDA_AGENT_LTSPICE_EXE", "Simulation", "found on the machine",
           "Path to the LTspice executable."),
    EnvVar("EDA_AGENT_LTSPICE_RUNS", "Simulation",
           r"`%USERPROFILE%\EDA Agent\ltspice`",
           "Where each LTspice run gets its own folder."),
    EnvVar("EDA_AGENT_LTSPICE_MODEL_ROOTS", "Simulation", "none",
           "Extra folders of LTspice models a deck may include, separated "
           "by `;` on Windows."),
    EnvVar("EDA_AGENT_SPICE_MODEL_DIRS", "Simulation", "none",
           "Extra folders of SPICE models `design_simulate_deck` may "
           "include, separated by `;` on Windows."),
    EnvVar("EDA_AGENT_EM_RUNS", "Simulation", r"`%USERPROFILE%\EDA Agent\em`",
           "Where each openEMS field-solver run (`sim_em_build_model`) gets "
           "its own folder. Runs can be large: point it at a drive with room."),
    EnvVar("EDA_AGENT_OPENEMS_DIR", "Simulation", "none",
           "The unzipped openEMS folder (Windows build). Added to `PATH` and "
           "`PYTHONPATH` for the solver run."),
    EnvVar("EDA_AGENT_OPENEMS_PYTHON", "Simulation", "this Python, then `python` on PATH",
           "A Python that can `import openEMS`, used to run the solver."),
    # ---- 3D and SDK -----------------------------------------------------
    EnvVar("EDA_AGENT_STEP_WRITER", "3D models and Altium SDK", "cadquery",
           "`freecad` tries FreeCAD before CadQuery when building a STEP "
           "model. With neither, the body is extruded and the reply says so."),
    EnvVar("EDA_AGENT_SDK_JSON", "3D models and Altium SDK",
           "`reference/altium_sdk.json`",
           "The reflected Altium SDK that `sdk-coverage` reads."),
    EnvVar("EDA_AGENT_SDK_SYMBOLS", "3D models and Altium SDK",
           "Altium's `ScriptingSystem.dll`",
           "The scripting DLL `sdk-symbols` reads its symbol table from."),
    EnvVar("EDA_AGENT_PCB_TOOLKIT", "3D models and Altium SDK",
           "`pcb-toolkit` on `PATH`",
           "Path to a locally built pcb-toolkit binary for "
           "`calc-cross-check`."),
    # ---- KiCad and EasyEDA ----------------------------------------------
    EnvVar("EDA_AGENT_EASYEDA_HOST", "KiCad and EasyEDA Pro", "127.0.0.1",
           "Address the server listens on for the EasyEDA Pro extension."),
    EnvVar("EDA_AGENT_EASYEDA_PORT", "KiCad and EasyEDA Pro", "8787",
           "Port the server listens on for the EasyEDA Pro extension."),
    EnvVar("EDA_AGENT_EASYEDA_VERIFIED", "KiCad and EasyEDA Pro",
           "next to the extension build",
           "The file that records which EasyEDA tools were verified live."),
    # ---- Updates and development ----------------------------------------
    EnvVar("EDA_AGENT_UPDATE_CHECK", "Updates and development", "1",
           "`0` stops the hourly background check for new commits from "
           "reaching the network."),
    EnvVar("EDA_AGENT_ASSUME_YES", "Updates and development", "off",
           "`1` answers yes to overwrite prompts in the CLI, like `--force`."),
    EnvVar("EDA_AGENT_RELOAD", "Updates and development", "off",
           "`1` runs the server behind a reloading proxy, so Python edits "
           "take effect without reconnecting the client."),
)

#: Names that look like settings in the source and are not. Each says what
#: it is, so the list cannot quietly absorb a real setting.
NOT_SETTINGS: dict[str, str] = {
    "EDA_AGENT_HOLD": "a placeholder footprint name in the Pascal library code",
    "EDA_AGENT_BACKEND_REEXEC": "internal: `_EDA_AGENT_BACKEND_REEXEC` marks "
                                "the re-exec that applies `--backend`",
}

#: Variables the code reads that are not eda-agent's own names.
OTHER_ENV_VARS: tuple[EnvVar, ...] = (
    EnvVar("ALTIUM_AI_IPC_DIR", "Profiles, policies and design modes", "unset",
           "Older name for `EDA_AGENT_IPC_DIR`, still read."),
    EnvVar("KICAD_SYMBOL_DIR", "Parts, libraries and datasheets", "KiCad's own",
           "KiCad's standard variable, used when the `EDA_AGENT_` one is unset."),
    EnvVar("KICAD_FOOTPRINT_DIR", "Parts, libraries and datasheets", "KiCad's own",
           "KiCad's standard variable, used when the `EDA_AGENT_` one is unset."),
    EnvVar("KICAD_3DMODEL_DIR", "Parts, libraries and datasheets", "KiCad's own",
           "KiCad's standard variable, used when the `EDA_AGENT_` one is unset."),
)

#: The Config tab / preferences.json keys (eda_agent.preferences.CHOICES).
PREFERENCES: dict[str, str] = {
    "value_format": "How a component value is written when it is cleaned up: "
                    "`none` leaves it as written, `rkm` gives 4K7 / 2u2, "
                    "`si` gives 4.7k / 2.2u.",
    "value_unit": "Under `si`, `with` adds the unit (100nF, 3.3V; never on "
                  "resistors). Always `without` under `rkm`.",
    "edits": "`readonly` refuses every command that is not a read. Safety "
             "switch: the stricter setting wins.",
    "deleting": "`refused` turns away commands that delete or remove. Safety "
                "switch.",
    "closing": "`refused` turns away commands that close a document or "
               "project. Safety switch.",
    "ui_automation": "`off` refuses menus, dialogs and keystrokes. Safety "
                     "switch.",
    "keep_in_view": "`on` fits the board or sheet in view after each edit.",
    "dialogs": "`manual` leaves dialogs such as the change order for you to "
               "answer, instead of the tool answering them.",
    "usage_stats": "`off` stops counting the tokens each tool call costs.",
    "length_units": "The unit a new AI session gives and reads lengths in: "
                    "`mil` (default) or `mm`. Each session may change its "
                    "own with the `length_units` tool.",
    "theme": "The web dashboard's look: `system`, `light` or `dark`. Set in "
             "the profile only; the Altium panel has no switch for it.",
}

#: <workspace>/.eda-agent/workspace.json keys (eda_agent.workspace_start.KEYS).
WORKSPACE_KEYS: dict[str, str] = {
    "profile": "The owner profile `eda-agent start` activates for this "
               "workspace.",
    "dashboard_port": "The web dashboard's port for this workspace "
                      "(0 = the default).",
    "altium365_workspaces": "Altium 365 workspace URLs (https://) the "
                            "`altium365_` tools may act on here. Exported "
                            "as `EDA_AGENT_ALTIUM365_WORKSPACES`.",
}

#: Keys of profile files that the code checks key by key.
PROFILE_KEYS: dict[str, dict[str, str]] = {
    "footprint_defaults.json": {
        "pad_corner_radius_pct": "Pad corner rounding in percent (0 to "
                                 "100) used when a footprint tool is given "
                                 "none.",
    },
    "document_settings.json": {
        "schematic_units": "Units for new schematic documents.",
        "pcb_units": "Units for new boards (documentation only: no bridge "
                     "command sets them on a live board).",
        "schematic_snap_grid_mil": "Schematic snap grid, in mils.",
        "schematic_visible_grid_mil": "Schematic visible grid, in mils.",
    },
    "phase_gates.json": {
        "stages": "The NPI stages this owner runs, e.g. `[\"evt\", \"dvt\", "
                  "\"pvt\"]`. Absent means none.",
    },
}
