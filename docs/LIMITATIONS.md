# Known limitations and troubleshooting

Read this before using eda-agent on a design you have not backed up.

## Known limitations

**This tool is experimental. Please read this section before using on a design you haven't backed up.**

> Bridge changes are checked by Free Pascal and a linter before they ship, which cannot prove Altium's own DelphiScript engine accepts them: the two differ on which identifiers exist, and an undeclared one faults at runtime rather than at compile time. [Release verification](RELEASE_VERIFICATION.md) is the procedure for closing that gap on a release, starting with a self-test that runs inside Altium and needs no document.

### UI automation synthesises real keyboard and mouse input

Most of this project talks to Altium through the scripting bridge, which addresses a window handle directly and cannot affect anything else. The `app_*` UI automation tools are different, and are used where Altium offers no other route: `application.execute_menu` reports success while invoking nothing, `GetMenu` returns 0 on Altium's DevExpress bars, and whole dialogs (Update From Libraries, Preferences, the wizards) have no scripting API at all.

**Synthesised input is not addressed to a window.** `keybd_event` and `mouse_event` are delivered to whatever is active at the instant they fire, and a click lands on whatever is under the pointer. So these tools:

- **take focus.** Menus and clicks need Altium in front, so running them while you are typing will interrupt you
- **cannot be confirmed the way a property write can.** A keystroke has no read-back; anything that matters is verified afterwards with a bridge read
- could, without containment, deliver an event to another application if focus or the pointer moved

What contains that: a foreground check runs **immediately before every event**, including between a key press and its release, and refocuses Altium rather than failing; coordinates are refused unless the point is over a window belonging to Altium's own process; and no tool accepts a window handle from the caller, so an arbitrary window cannot be addressed. `tests/test_foreground_guard.py` enforces all three, checking the event guard per line of source rather than per function, because a function that checks once and then emits five events in a loop would pass a naive test while firing four unchecked events.

**To switch it off entirely:**

```
EDA_AGENT_UI_AUTOMATION=0
```

Every synthesised event is then refused. Reading stays available on purpose, because dialog detection is how the rest of the system notices Altium is blocked on a modal.

One case is not solvable: Altium's menu bar carries entries that are commands rather than menus (Place a Comment, Share, Open Home page, Preferences), and nothing distinguishes them. Measured across all 17 bar items: identical MSAA state including `HASPOPUP`, identical `accDefaultAction`, identical UIA control type. Listing such an entry clicks it, and clicking it runs it.

Full detail in [`docs/ui-automation.md`](ui-automation.md).

### Altium DelphiScript engine can crash

Some tool paths trigger DelphiScript compile or runtime errors ("Undeclared identifier…", "Could not convert variant of type (Dispatch) into type (OleStr)", etc.). When that happens, the script project halts mid-execution and the polling loop stops responding. You will see one of:

- An Altium error dialog stating the problem
- Your MCP client timing out waiting for a response

**Recovery is automatic** when the crash happens during a call. The server takes a screenshot of the error box, presses OK, takes a screenshot of the halted line, stops the script and restarts the loop. Other sessions' calls wait while it does. The call that crashed fails with a message saying the script crashed and where the screenshots and a crash record are (`logs/crashes` in the workspace). The handler may have applied part of its change, so read the design back before repeating it. For a script found halted some other way, call `app_recover_script`, or run `eda-agent relaunch --restart`.

**By hand:** in Altium Designer, open the script project tab and press the **red Stop** button in the Script IDE toolbar (equivalently **Run > Stop** from the menu, or **Ctrl+F3**; use **Ctrl+Pause/Break** if the script is stuck in an infinite loop). This stops the halted debugger. Then re-launch the polling loop via **File > Run Script... > StartMCPServer > Run**.

This is an ongoing reliability effort. Every identified crash is either fixed or guarded. If you hit a new one, the Altium error dialog tells you the exact identifier or line. Opening an issue with that text helps us harden the relevant path.

### UNC network paths need current scripts

A UNC path (`\\server\team\board.PrjPcb`) opens like any other with the current scripts. Scripts deployed before the fix drop one leading backslash, so the file is not found and the error names a path that looks almost right. Run `eda-agent install-scripts --force` to update them, or use a mapped drive letter (`Z:\team\board.PrjPcb`) in the meantime.

### Text above Latin-1 becomes question marks

Altium's DelphiScript strings are single-byte, so the bridge carries text as one byte per character. Any character above U+00FF is replaced with `?` on the way in, silently. Accented Latin, the micro sign, and the degree sign are all below that boundary and survive; the ohm sign and any CJK text do not, so `10Ω` arrives as `10?`.

This shows up most often on imported parts: LCSC descriptions are frequently Chinese, and `lib_easyeda_import` passes the description straight through. If you need those fields readable, set them to a transliteration before importing, or edit them in Altium afterwards.

### Altium tool buttons relying on internal scripting pause while the server is running

Altium itself uses DelphiScript internally for many built-in commands (some ribbon buttons, panel actions, menu items). **While the `eda-agent` polling loop is active, those built-in commands may become temporarily unresponsive** because Altium's scripting engine is single-threaded and currently owned by our polling loop.

**The polling loop owns the scripting engine for as long as it's running.** While it runs, Altium's own script-backed buttons sit waiting. The loop exits when either:

- The MCP client calls `app_detach` (or the dashboard **Detach** button is clicked); `app_detach` saves the edited documents (the button saves nothing), the loop exits within ~500 ms, and Altium becomes fully responsive, OR
- **10 minutes of total silence** from the MCP client (no commands AND no keep-alive pings) triggers the built-in auto-shutdown

In practice, while an MCP client is attached and sending keep-alive pings every 30 s, the loop will never time out on its own; you need to either have the AI call `app_detach` or close the MCP client session entirely. After the client disconnects, expect up to ~10 minutes for the loop to auto-exit unless you use **Detach** to release it immediately.

### ECO (sch → PCB update) opens a modal, and there is no silent API

The Altium Schematic API exposes no scripted ECO executor: `IECO` only records proposed changes, no `DM_Execute` is documented, and no factory hands a script an `IECO`. `PCB:UpdatePCBFromProject` turned out not to be a real process id, so the handler that called it no-opped while reporting success. `proj_sync_pcb` now invokes `WorkspaceManager:Compare`, which does the real work and blocks on the change-order dialog.

Two consequences worth knowing before you call it.

It **blocks the polling loop** until the dialog is answered, so it is not an unattended call on its own. It also **refuses while a schematic is focused**: Altium answers that case with "Cannot compare a source document against its owner project" and changes nothing. Focus the board with `app_set_active_document` first.

What it reports is narrower than it looks. `components_in_sync` counts component **presence** only, so footprint swaps, designator and parameter edits and net changes are all invisible to it, and `dialog_outcome_verified` is always false because the handler cannot see which button was pressed.

**To drive it end to end**, use the GUI tools rather than answering by hand:

```
app_menu(how="drive", args={"menu_path": "Design|Update PCB Document <board>.PcbDoc", "allow_commit": True})
```

That clicks the menu and answers the dialogs in one call, which matters because a modal blocks the bridge and a second call would never arrive. It validates before executing, and without `allow_commit` it presents the change order instead of applying it.

### Tools vary in maturity

Not every one of these tools has been exercised on every Altium version or design size. The generic primitives (`obj_query`, `obj_modify`, `obj_create`, `obj_delete`, `obj_run_process`) and the core `application` / `project` tools are the best-tested. Some PCB modify operations (polygon repour, room creation, align-components) are less battle-tested. Queries are generally safer than mutations.

### What the bundled data does not cover

The data this project ships is transcribed, dated and sourced, and
several figures are **missing on purpose** because nobody publishes
them. They come back `null` with a reason rather than filled in with a
plausible number, and the tools that need them refuse rather than
default.

- **No product-change notices and no errata.** `part_find_replacements`
  compares parameters, lifecycle and stock. No distributor API serves
  PCNs or errata, so nothing checks them, and every result repeats that.
  A PCN is the commonest reason a part that passes every check is still
  the wrong choice this month.
- **No pad geometry in a replacement check.** The package *name* is
  compared; a catalogue states "SOIC-8" and never states the pads. Use
  `lib_audit_footprint_vs_datasheet` against the candidate's datasheet.
- **JLCPCB publishes no rigid hole-to-hole minimum and no mask sliver.**
  Both are `null`, and `stackup-profile` refuses to build a fab profile
  without them. Supply them with `--fill` once the fab has told you, and
  they are recorded as yours, not theirs.
- **PCBWay publishes no named stackup**, so none is bundled.
- **The IPC-2221 spacing table is a secondary transcription.** The IPC
  document is not freely available, and two reproductions **disagree on
  two cells** (B2 at 251-300 V, A6 at 31-50 V). The larger value is
  taken, since a wider spacing is never the unsafe mistake, and both
  figures are reported in `disputed`. The classes are the **A/B** ones;
  revision C *redefines* those columns rather than renumbering them, so
  match on the description the result gives you, never on the letter.
- **The laminate list is deliberately short** - only entries read off a
  datasheet. Add your own to your profile.
- **Altium preferences cannot be templated.** There is no scripting API
  for the Preferences dialog. **Draftsman is unsupported entirely.**
- **PCB-side units and grid are documentation only.** No bridge command
  sets a board's display units or default snap grid; a project template
  sidesteps this for new projects.

## Timeout and server lifecycle

The server has **three independent timeout mechanisms**:

### 1. Per-command timeout (Python side)

When the MCP client calls a tool, the Python bridge writes a request file and waits up to **10 seconds by default** for a response. Fast queries typically complete in under 100 ms, so a 10 s ceiling surfaces stalls quickly while leaving plenty of margin for real work. Long-running tools that are expected to take longer (`app_save_all`, `pcb_get_unrouted_nets`) set their own larger timeouts internally.

Each request is published to its own `request_<id>.json` file; Altium replies in `response_<id>.json` with the matching ID. The bridge's keep-alive thread and MCP-client calls each use their own request IDs, so responses never race. The older single-`response.json` channel was retired in IPC v2.

### 2. Server auto-shutdown (Altium side)

The DelphiScript polling loop auto-stops after **10 minutes of inactivity** (`AutoShutdownMs = 600000`). If the MCP client disconnects and the keep-alive pings stop arriving, the server releases Altium's scripting engine after ten minutes and `StartMCPServer` returns. To resume, re-launch via **File → Run Script... → StartMCPServer → Run**.

### 3. Python keep-alive pings

While an MCP client is attached, the Python bridge pings Altium every 30 seconds so the 10-minute auto-shutdown never fires mid-session. The sequence:

- **AI issues command A** → Altium busy, then idle
- **30 s later, Python pings** → Altium responds "pong", idle timer resets
- **10 min later, still no AI activity and no ping** → Altium auto-shuts down

In practice: the server stays alive as long as an MCP client is connected, and exits cleanly ~10 minutes after the client fully disconnects. No manual stop needed in the common case. For a hard exit, the AI calls `app_detach`, which saves the documents that were edited, leaves clean ones untouched, and returns control to Altium within ~500 ms. The **Detach** button on the dashboard window stops the loop without saving.

### Several Claude sessions on one Altium

Every session runs its own eda-agent server, and all of them talk to the one script loop in Altium. Request ids carry a session tag and a sequence number. A session working alone is served straight through, in the order it sent its requests, with no time slots. When more than one session has requests waiting, the session being served keeps going for up to 30 seconds (`SESSION_SLOT_MS`), then the next waiting session gets one slot, and so on in turn, so no session gets a second slot before every waiting session has had one. A command already running is never interrupted: slots change between commands, so one long command (a large repour, say) still holds everyone else up until it finishes. While sessions contend, Altium refreshes `queue.json` in the workspace, and a waiting client keeps waiting instead of reporting the loop as dead.

Each session also keeps its own document. Most tools act on the focused document, and any session can move the focus between two calls of another. So every server sends, with each command, the document its session last had in front, and Altium puts that document back in front before running the command, in the same step so nothing can come between. A command's reply carries `session_document` when that happened. This holds even when you click to another document yourself: the next command of a busy session brings its own back, because a write on a document the session never chose is worse than a view that moves. To point a session at the document you are looking at, tell it to; `app_get_active_document` reports what is really in front, and focusing a document makes it the session's.

Things that affect every session at once: a deploy, a relaunch or a restart of Altium, and `app_detach`, which saves every document a tool edited, including another session's. To restart the loop for a deploy, run `eda-agent install-scripts --force` and then `eda-agent relaunch --restart`. It waits for the command in progress to finish, and other sessions' calls wait through the restart instead of failing, so nobody needs to pause. A session whose eda-agent server started before the deploy gets `server_outdated` on its replies until it reconnects (`/mcp` in Claude Code). To stop the loop without saving, put an empty file named `stop` in the workspace.

### Why this matters for Altium UI responsiveness

The polling loop goes into idle mode after a second or two of no MCP commands (150 empty polls). In idle mode it polls every 30 ms with a `ProcessMessages` yield in between, so Altium's UI stays responsive continuously. In active mode the loop polls every 10 ms (`ProcessMessages` every 5th tick), giving sub-50 ms round-trip latency for back-to-back commands. For a full release, call `app_detach` or click **Detach** on the dashboard.

## Troubleshooting

**"Altium Designer is not running"**: open Altium before invoking MCP tools.

**"Script not responding" / MCP tools time out**: confirm the script project is loaded and `StartMCPServer` is running. Re-launch it via **File > Run Script... > StartMCPServer > Run**. Check `%USERPROFILE%\EDA Agent\workspace\` is writable.

**Altium error dialog "Undeclared identifier: ..." or "Could not convert variant..."**: a DelphiScript crash in one of the bridge handlers. In Altium's Script IDE toolbar, press the red **Stop** button (or **Run > Stop** / **Ctrl+F3**; use **Ctrl+Pause/Break** if the script is stuck in an infinite loop) to halt the debugger. Then re-launch the polling loop via **File > Run Script... > StartMCPServer > Run**. If the identifier is one the scripts themselves define, Altium is running an older compile of a unit: run `app_ping`, which names it in `stale_units`, then close the script project in the Projects panel (right-click `Altium_API.PrjScr`, **Close Project**), reopen it and run StartMCPServer. Otherwise report the identifier or error text as an issue.

**Some Altium buttons don't respond while the server is running**: expected while the AI is actively issuing commands. Built-in Altium functions that depend on DelphiScript wait for the polling loop to yield. The loop enters an idle/yield mode a second or two after the last AI command; if a button is still unresponsive after that, call `app_detach` from the MCP client to fully release the scripting engine.

**Altium's main window disappears while the status window stays up**: the main window has been found hidden, not minimized, with no taskbar button. The bridge checks after every reply, keep-alive pings included, and shows it again without taking the focus, so it comes back within about 30 s. Each time, `bridge_trace.log` gets a `FRAME_SHOWN` line naming the last command, and UI tools log `UI tool=<name>` lines so they can be named too. If it keeps happening, report that line as an issue.

**Command timeouts on very large boards**: default is 10 s so stalls surface fast. Tools known to take longer (`app_save_all`, `pcb_get_unrouted_nets`) set their own, longer timeouts. If you hit a timeout on a custom long-running operation, embed the bridge directly and pass a higher `timeout=` to `send_command_async`. The polling loop itself adapts (10 ms active, 30 ms idle) so it doesn't add latency.
