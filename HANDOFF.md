# Handoff

State of this fork for the next session, whatever client or account runs
it. Last updated 2026-10-25. `CHANGELOG.md` has the detail; this is what
you need before touching anything.

## Repository and remotes

- `origin` is the fork, `MOREmbedded/eda-agent`. Push here only.
- `upstream` is `salitronic/eda-agent`. Fetch from it, never push.
- `alex` is `Rouniy/eda-agent`. Never push to it.
- `main` is at `279aaaa` (PR #1 merged), fully pushed.
- Work on a feature branch and merge only when the owner says so.
- Docs are English only. No personal names go in source files or docs.

## Running it

- Tests: `.venv/Scripts/python.exe -m pytest -q -p no:randomly`. Add
  `-n auto` to finish in about 6 minutes instead of 13 to 15. The last
  full run passed 7,992 tests, with 183 skipped and 1 xfailed.
- `docs/TOOL_REFERENCE.md` is generated. After adding or changing a
  tool, run `python scripts/gen_tool_reference.py`, or
  `test_gen_tool_reference` fails. Offline tools are classified in
  `src/eda_agent/tools/metadata.py` (`_OFFLINE_TOOLS`).
- Altium 26.11.1.5 loads the scripts from `.eda-local\scripts`. After a
  redeploy, the owner reloads them by hand: File > Run Script >
  Altium_API > Dispatcher.pas > StartMCPServer. Python-only changes
  need no reload.
- The server workspace is `%USERPROFILE%\EDA Agent\workspace`. Its
  `bridge_trace.log` and `last_fault.json` are the first place to look
  when a call times out.
- For live checks, build a `ToolRegistry`, call
  `register_backend(reg, "altium")` and call the tool's `.fn`. Work on
  copies under `.eda-local\live-verify\`, never on the owner's files.

## When the Altium script loop stops answering

The symptom is `AltiumTimeoutError: ... no progress heartbeat`. The
loop has either stopped or is stuck in a long command. Ask the owner
to dismiss any Altium error dialog, press Stop in the Script IDE
(Ctrl+F3), and run StartMCPServer again. Stopping is safe for
read-only commands. Pings sent while the loop is down wait in the
workspace as `request_*.json` and are harmless.

Known trigger: the owner's board `BDDFS2MWC01A.PcbDoc` is large. One
full `pcb.get_layout_model` read worked (one page took 37 s), but a
second full read right after it hung the loop on 2026-10-20. Ask before
running full-board reads (`pcb_layout_audit`, `design_find_loops` with
`expect_file`) on that design.

## Done in this round

1. **Upstream 0.7.1 merged** (24 commits; `SCRIPT_VERSION`
   `2026.10.19.1`). Altium reports that version, and the library,
   layout-model and project open/compile commands used below worked
   live. The rest of the 0.7.1 surface has still not been run live:
   see the CHANGELOG and `docs/RELEASE_VERIFICATION.md`.
2. **External Altium MCPs evaluated.**
   - `embedded-society/altium-designer-mcp` was not integrated. It is
     Rust and GPL-3.0, and it writes `.PcbLib`/`.SchLib` binaries
     directly, which this project never does (`docs/PART_SOURCING.md`).
     Its useful ideas were rebuilt here instead.
   - From `coffeenmusic/altium-mcp` (MIT), only `check_orientation`
     was missing. It was rebuilt as `rotation_audit`. Ideas from either
     project get reimplemented here; their code is not copied.
3. **PR #1, merged.** New tools and their live results:

   | Tool | What it does | Checked |
   |---|---|---|
   | `lib_extract_step_models` | Lists or extracts the STEP models embedded in a .PcbLib, with Altium closed | Two vendor libraries, 12 of 12 models |
   | `lib_write_libpkg` | Writes a .LibPkg in the layout Altium saves | Opened and compiled to an IntLib in Altium |
   | `lib_backup_library`, `lib_list_library_backups`, `lib_restore_library_backup` | Copies of one library file; restore needs `confirm=True` and backs up the file it replaces | Round trip on a real library copy |
   | `lib_render_footprint` | Draws a footprint to SVG and PNG from the live reads | WE-PMI footprint drawn correctly in Altium |
   | `lib_compare_footprints` | Diffs two footprints pad by pad and counts primitives per layer | WE-PMI 0603 H0.5 vs H1: height difference only, which is correct |
   | `rotation_audit` (a `pcb_layout_audit` check) | Suggests right-angle turns for two-pad parts, scored by airline length and distance to the nearest return pad | Sensible suggestions on BDDFS2MWC01A. BoosterTest has no connected two-pad parts, so it can't exercise this check |

## Open items

- **Symbol preview.** It needs a new live read of symbol graphics in
  the Pascal script. Today only the pins can be read.
- **Bottom-side parts in `rotation_audit`.** They are skipped until
  someone checks live which way a rotation value turns a mirrored part.
- **Altium 365 write tools** (`eng_a365_publish_model`,
  `eng_a365_release_component`, `eng_a365_update_parameters`). They
  have still not been run against a real Nexar account.
  `eng_a365_publish_model` uploads a library file from disk, so the
  library tools above feed it.

## Test material on this machine

- Vendor PcbLibs with embedded STEP models, written by Altium, are
  under `D:\Documents\Download\` (WE-PMI, TAOGLAS).
- `D:\Altium\Backups\` holds 44 LibPkgs saved by Altium.
- `.eda-local\test-project\` holds BoosterTest, a two-layer board, plus
  its libraries.
- There are no distributor credentials here, so
  `part_find_replacements` can't be checked live.
