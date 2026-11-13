# Changelog

This file did not exist before this entry (no changelog convention was
established in the repository's history; releases were tracked only by
git tag and PyPI description). Added here using the common
[Keep a Changelog](https://keepachangelog.com/) format as a reasonable
default - adjust or drop if the project prefers a different convention.

Where an entry says UNVERIFIED, it means the code is covered by tests
but has not been watched working against a live Altium session or a real
Nexar account. Each such tool says so in its own docstring too; the
generated [`docs/TOOL_REFERENCE.md`](docs/TOOL_REFERENCE.md) carries a
maturity classification for every tool.

## [Unreleased]

### Added

- **Signal integrity: routed nets and pairs.** A routed net or differential
  pair is modelled from pin to pin: its path is found in the board's copper
  (tees, stubs and layer changes included), every trace section uses the RLGC
  of its own cross-section, vias are coaxial barrels with stubs, pad
  capacitance and the return-via loop when the reference changes, and the
  blocks are cascaded as exact chain matrices. New job kind `net-channel`;
  new tools `si_board_snapshot` (the board and stackup read once, content
  named, stale once the board is saved) and `si_net_channel` (S-parameters,
  TDR, delay, loss at Nyquist, skew, mode conversion, checks against limits,
  and the width, gap, uncoupled-length and length rules, written to Altium
  when asked). See [Routed nets and pairs](docs/SI_NETS.md).
- **Signal integrity: transmission lines and a stackup planner.** New
  package `eda_agent.si`. A 2D eddy-current solver gives R(f) and L(f)
  of traces and their reference planes with skin and proximity effect
  from DC up; the field solver gives C(f) and G(f) with causal
  Djordjevic-Sarkar dielectrics (mask included); Huray, Hammerstad or
  Groisse roughness; exact modal S-parameters of coupled lines with
  mixed mode and crosstalk. New job kinds `line-model` (also on
  scikit-rf, as a closed-form check of a single microstrip) and
  `stackup-plan`; new tools `si_line_model` and `si_stackup_plan` (widths
  and pair gaps per layer for target impedances, compared with or written
  as Altium impedance profiles); a Transmission line form, plots and
  result files on the dashboard's Simulation page. See
  [docs/SI_LINES.md](docs/SI_LINES.md).
- **Thermal analysis.** `sim_thermal_analysis` solves a board's steady
  temperature field in three dimensions from the power each part
  dissipates: the real copper of every layer (pours, planes with their
  antipads, tracks and pads with their exact areas), every plated hole,
  the laminate's anisotropic conduction, natural or forced convection
  and radiation from both faces, the edges and part cases. It reports
  the peak, hotspots with the nearest part, each powered part's pad,
  junction and case temperature against `tj_max_c`, the hottest
  unpowered parts, a heat balance and, with `refine`, a grid-convergence
  error estimate, and writes a heat map per layer. It takes the Joule
  heat map of an IR-drop run as an extra source. Checked against exact
  solutions (series slab, Bessel fin, Fourier spreading series,
  convection and radiation balance, via arrays). Copper that does not
  touch is kept apart inside a grid cell (a pad and the pour round it, a
  via in another net's pour), so the answer does not depend on the cell
  size resolving the clearances. Each run also writes a picture of the
  copper as it was solved. See [`docs/THERMAL.md`](docs/THERMAL.md).
  Run live on one populated six-layer board read from Altium.

- **Features and dependencies, and a one-command installer.** One
  registry (`eda_agent.features`) describes every optional part: the pip
  extras, the Rust wheels built from this repository, the programs some
  tools drive (openEMS, LTspice, ngspice, KiCad, FreeCAD, pcb-toolkit)
  and the simulation engines, each with its license and what it offers.
  `eda-agent features` shows what is installed with versions;
  `eda-agent install-deps --all` (or named features) installs the rest
  after showing its plan, one pip call per feature, GPL engines in their
  own environment, and prints the steps for programs. The same in the
  dashboard's new **Features** page (install buttons with live output,
  engine switches) and the `app_features` / `app_install_features` tools.
  New extra `si` (scikit-rf). New settings `EDA_AGENT_ENGINES_DIR`,
  `EDA_AGENT_FEATURES_FILE`.
- **Simulation engines and jobs.** Every simulator is an engine behind
  one job model (`eda_agent.simulation`): a job is a folder named by a
  hash of the problem, each engine writes normalised results beside its
  own files, and `sim_job_compare` sets the answers side by side. Engines
  today: this project's solvers (S-parameter quality checks, the 2D
  field solver), scikit-rf, and openEMS (run as its own process, as
  before). Job kinds: `touchstone-check`, `xsection-2d`, `em-3d`. Tools
  `sim_engines`, `sim_job_run`, `sim_job_status`, `sim_job_compare`,
  `sim_jobs`; the dashboard's new **Simulation** page. Other packages add
  engines and features through the `eda_agent.engines` and
  `eda_agent.features` entry points. New setting `EDA_AGENT_SIM_JOBS`.
  First step of the signal-integrity work (docs/SIMULATION.md).

- **Euan Mutch's router as an opt-in autorouter engine.** `eda-agent
  router-use euan` (or `EDA_AGENT_ROUTER=euan`) routes `pcb_autoroute`
  jobs with the router from his fork (TechAUmNu/eda-agent, Apache-2.0);
  `eda-agent router-show` says which engine is in force and why. The
  default engine stays the default; what this work found wrong in it is
  under Fixed. Both engines' results go through the same verdict and our
  gloss, and the default router's via guards were ported into his.
  Where his router took for a join what the verdict does not
  (`join_existing`, `reuse_vias` through a blind via, or a plane pad
  "served" by a via that stands outside the plane), or let any via
  share a pad's solder mask opening under `pad_vias="centre"`, it no
  longer does. A narrow `neck_pads` branch keeps its width after the
  pour stage and in the attempt after a plane is given back (it was laid
  at the net's full width, into another net's clearance), and his coarser
  grid pitches route clean (see Fixed: a pad smaller than a cell). A
  setting that cannot be used routes with the default engine, and his
  router's own job parameters are range-checked before anything runs.
  `python -m eda_agent.layout.bench_run --fixtures DIR --router euan`
  benchmarks it on your own boards, and `scripts/bench_router_engines.py`
  compares both engines on synthetic ones. See
  [Autorouter engines](docs/ROUTER_ENGINES.md).

- **PCB toolkit: 44 calculators, a Calculators tab and `pcb_calc
  kind=toolkit`.** Controlled impedance (microstrip, embedded
  microstrip, symmetric and offset stripline, dual stripline, coplanar)
  and differential pairs (edge-coupled, broadside, coplanar) come from a
  new 2D field solver with solder mask, trapezoidal traces, mixed
  dielectrics, causal Er(f), dispersion, and conductor loss with
  roughness; it agrees with the exact conformal-mapping results within
  0.1 %. Trace and via current come from a thermal model of the board
  (planes, laminate, convection, radiation) in place of a chart. Also
  crosstalk, plane-pair capacitance and cavity impedance, PDN target,
  timing, junction temperature, Ohm's law and passives, padstacks, BGA
  lands and conversions. The dashboard tab (key 0) fills inputs from the
  open board's layer stack. See [`docs/PCB_TOOLKIT.md`](docs/PCB_TOOLKIT.md).

- **PDN analysis.** `sim_pdn_ir_drop` solves a power net and its return
  as a sparse resistor network of the board's real copper (pours and
  planes on a grid, tracks and arcs as exact resistors, via barrels from
  the stackup; pads met at their real edge and barrels at their hole
  wall, so the answer does not move with the grid) and reports the worst
  pins of every load, current density, loss per layer, overloaded tracks
  and vias, and a grid-convergence error estimate. `sim_pdn_impedance`
  finds a rail's decoupling capacitors (MLCC, polymer, tantalum and
  electrolytic defaults; the value from the comment, else from the
  part's parameters), works out each pin's mounting inductance from
  the copper that joins it to the planes, solves the power/ground plane
  pair as a field cavity on its real, perforated shape (FDFD, exact
  skin-effect surface impedance, Djordjevic-Sarkar laminate) and reports
  where |Z| at the load exceeds the target. Both are checked against
  closed-form references, and the IR-drop run writes its Joule heat map.
  Both ran live, read-only, on one six-layer board, with the capacitor
  values read from the parts' parameters. See [`docs/PDN.md`](docs/PDN.md).
- **SPICE model library and LTspice round trip.** `sim_scan_models`
  indexes vendor model files and LTspice symbols (including LTspice's own
  library, encrypted models through their symbols) into the design
  workspace; `sim_find_models` and `sim_find_schematic_models` find models
  by MPN; `sim_get_pin_map` and `sim_set_pin_map` match symbol pins to
  model nodes by name, never silently by pin number; and
  `sim_attach_library_model` links a model only after its pins check out.
  `eng_ltspice_deck_from_schematic` now emits subcircuits with the model's
  name and node order and includes their files (it used the Comment and
  pin-number order before, and dropped parts that had SpicePrefix but no
  SIM link). `eng_ltspice_run` copies model files into the run folder,
  `eng_ltspice_export` writes a deck with its models for the LTspice GUI,
  and `eng_ltspice_compare_schematic` checks an LTspice `.asc`/`.net`
  against the Altium schematic (parts, values, wiring). See
  [`docs/SPICE_MODELS.md`](docs/SPICE_MODELS.md). Checked live against
  LTspice XVII; UNVERIFIED against live Altium.

- **Symbol and footprint preview before placing a part.** The
  dashboard's Library pane has a "Look up a part" view: search the
  profile's scanned Altium 365 library offline (name, value,
  description, MPN), pick a part, and see its symbol and footprint as
  they are in the library, with pin and pad counts and where each comes
  from; a multi-part symbol shows one part at a time. A part from a
  .SchLib can be previewed by path and name (an .IntLib through its
  extracted .SchLib, never opened in Altium). New script read
  `library.get_symbol_geometry` loads the symbol the way placement does
  (`SchServer.LoadComponent` for a Workspace part, the open SchLib
  otherwise), answers its graphics, pins and models, and destroys a
  loaded part again, so nothing reaches a sheet. New tool `lib_preview_part` writes
  both drawings as SVG. A footprint link that names no file (the usual
  "any library" link) is resolved by reading the .PcbLibs beside the
  symbol's library from disk. Checked live on a Workspace part and a
  .SchLib part: symbols read in under a second (SchLib: 6 to 9 s, it is
  opened), nothing opened, closed, placed or marked modified. A
  Workspace part must be asked for with its revision (CMP-00000010-3).
  Not done yet: a Workspace part's footprint, which has no file to read;
  the preview says so and shows the symbol.

- **Fab and IPC class floors on a board's design rules, on demand.**
  New `pcb_rules_fab_floors` tool holds the board's existing rules up
  against one fab build's published minimums (the cited fab rules
  database) and, with `ipc_class`, the profile's IPC class floors
  (`std-floors`), and says rule by rule which ones sit below the floor
  and why, with the fab quote or IPC clause behind each floor. It reports
  by default; with `apply=True` it raises board-wide rules that are below
  the floor and nothing else. Rules already stricter are never loosened,
  scoped rules below the floor are left for the user to name, rules DRC
  does not check and kinds with no writer (annular ring) are reported,
  and nothing is created or deleted. Writes go into the board's rule log.
  Never runs on its own. UNVERIFIED against a live board.

- **EMI/EMC pre-scan and openEMS field solving.** `sim_emc_prescan`
  checks a routed board's fast nets with no external program. It finds
  tracks that cross a split or slot in their reference plane or have no
  plane beside them, fast tracks near the board edge, and layer changes
  with no return via. It also estimates differential-mode radiated
  emission per net (Ott's small-loop formula) against CISPR 32 or
  FCC Part 15 limit lines. `sim_em_build_model`, `sim_em_run` and
  `sim_em_results` cut a few nets with their return copper and the real
  stackup out of the board and solve them in openEMS, which runs as a
  separate process. Results come back as a Touchstone file and a summary:
  return loss, S21, and for antennas directivity, efficiency and pattern.
  `sim_read_touchstone` summarises any `.sNp` file. Checked against
  openEMS 0.37 on Windows: a 50 ohm microstrip's effective permittivity
  from two line lengths is within 1 % of Hammerstad-Jensen, |S21| 0.986,
  |S11| -33 dB (`tests/emc/test_openems_real_solver.py`, skipped where
  openEMS is not set up). The live board read is UNVERIFIED.
  See [`docs/EMC_RF.md`](docs/EMC_RF.md).

- **Fab design-rule database for thirteen PCB fabs.** JLCPCB, PCBWay,
  Technoservice, Satland, Eurocircuits, Würth Elektronik, Multi Circuit
  Boards, AISLER, Beta LAYOUT, NextPCB, PCBCart, OSH Park and Advanced
  Circuits, each with its published pages registered as a connector.
  Twelve have rules stored, every figure with the fab's quote, page and
  date; Würth Elektronik is pending (its site refuses scripts).
  New `fab-list`, `fab-rules`, `fab-compare` and `fab-refresh` commands
  and `design_list_fabs`, `design_fab_rules`, `design_fab_compare`
  tools give every rule of one build (board edge, V-score, mask,
  silkscreen, tolerances, aspect ratio and more, not only the seven
  minimums a fab profile carries). `fab-refresh` re-reads a fab's pages,
  obeying robots.txt, snapshots them outside the repository and checks
  every stored quote is still there; it never rewrites a figure. IPC
  class 2/3 floors come from the active profile (IPC values are
  licensed and never ship in the repository) and are refused without them.
  See [`docs/FAB_AND_CALCULATORS.md`](docs/FAB_AND_CALCULATORS.md).

- **Token usage of the EDA tools, counted.** Every MCP tool call is
  measured as the server sends it: the arguments the AI wrote and the
  result it reads back, turned into an estimated token count (about 4
  characters a token, images by their size as the model shrinks them),
  with the time taken. Kept per workspace in `.eda-agent/usage/` (which
  ignores itself for git), summed per tool, day, session and profile.
  Shown on a new **Stats** tab in the status window in Altium and a new
  **Usage** view in the dashboard (key 9). A setting, `usage_stats`
  (default on): **Token stats** on the Config tab, or per profile in the
  dashboard's Workspace view; `EDA_AGENT_USAGE_OFF=1` also turns it off.
  See [`docs/TOKEN_USAGE.md`](docs/TOKEN_USAGE.md).

- **`eda-agent start` brings one design workspace up in one command.**
  Each workspace keeps its own settings in `.eda-agent/workspace.json`
  (profile, dashboard port, the Altium 365 workspaces the tools may use)
  and its dashboard log in `.eda-agent/logs/`, so two workspaces share
  nothing and no client detail lands in this repository. Secrets stay in
  the user environment; the file refuses keys that look like one. Only an
  eda-agent process is stopped to free the dashboard port.

- **An offline copy of an Altium 365 managed library, kept in the profile.**
  `altium365_scan_library` reads every component, symbol, footprint and
  folder into `<profile>/library/<workspace host>/` as JSON and CSV, past
  the 10,000-row `totalCount` cap, with several readers on row ranges and
  pages halved when they time out; an interrupted scan continues.
  `altium365_update_library` rereads all but the bulk block at the end of
  the list and probes the row past it. `altium365_search_library` searches
  it offline and `lib_search` folds it in as `source: "altium365"`.
  `altium365_library_report` lists gaps and, given design folders, joins
  placements from `.SchDoc` files on the item id and flags older
  revisions. CLI: `altium365-scan-library`, `altium365-update-library`.
  Live-checked on 2026-11-10 against a 22,561-component workspace: the
  scan and the counts matched an independent scan, the refresh took
  1m49s. `profile.json` names the workspace in `altium365_workspaces`.
- **`altium365_scan_3d_models`** reads the library's 3D model items from
  Altium's Explorer panel, which the GraphQL API cannot list. UNVERIFIED:
  the live run was stopped before it finished.

- **Project stages in the dashboard.** The Workspace view's Settings card
  has a switch for the NPI gates (off by default; it writes the profile's
  `phase_gates.json`, as `phases-enable` does). With it on, a *Project
  stages* card shows each tracked `.PrjPcb` as a stepper from Feasibility
  to Production, passed, current, ahead or not run, and each stage opens
  its gate's checklist to mark Met, Unmet, Waived or Open with the
  evidence. Projects are tracked by path, or picked from those open in
  Altium; each has a revision, and changing it reopens the gates. The list
  and the answers live in `<workspace>/phase_gates/`, never in a
  repository, and the ledger is the one the `design_phase_*` tools write
  (`product` = the project's file name). See docs/NPI_PHASE_GATES.md.
- **A Research stage** between Feasibility and Prototype, with five
  checks: the architecture chosen, risks ranked and owned, the top risks
  retired on an eval kit or in simulation, reference designs read, and
  every requirement mapped to a block. 47 checks across six stages.
- **Per-profile settings.** A profile may carry `preferences.json` with the
  Altium panel's Config-tab keys (component value format, keep in view,
  dialogs, and the edits/deleting/closing/UI-automation safety switches)
  and `default_mode`, the design mode a run starts in. The profile wins
  over the panel's one-per-machine file for the value format and workflow
  settings; for the safety switches the stricter of the two wins, so a
  profile can add a restriction but never lift one. `default_mode` sits
  between `EDA_AGENT_MODE` and `eda-agent mode-use`. Refusals and the
  value-format tool name the profile file when it decided.
  `profile-validate` checks the file. See docs/PROFILES.md.
- **Settings card in the dashboard's Workspace mode (key 8).** Shows each
  of those settings, the value in effect and where it comes from (this
  profile, the Altium panel, the environment, `mode-use` or the default),
  and sets or clears it in the profile's `preferences.json`. The panel's
  own file is read, never written. The Altium panel itself still shows
  only its own choice; UNVERIFIED on a live Altium.

- **Dashboard Workspace mode.** A top-bar chip names the profile in
  effect and where it comes from; mode 8 shows the resolution order, a
  button per central profile to switch to it, the design-rule targets and
  the rules they produce, rulesets, ERC sets, fab limits, templates and
  conventions, each file viewable read-only, plus compare, preview and
  apply against what is open in Altium. See docs/PROFILES.md. The
  compare/apply buttons call the existing `profile_diff_all` and
  `profile_apply_all`; UNVERIFIED from the page against a live Altium.
- `profile-validate` now names why a ruleset is invalid, not only that it
  is. The check itself moved to `profile.validate_profile`, which the
  dashboard uses too.
- **Optional Rust compute (`--compute rust|auto|python`,
  `EDA_AGENT_COMPUTE`, `eda-agent compute-status`).** The routing
  planner's grid A* has a compiled port in a separate wheel,
  `rust/eda_agent_compute` (PyO3, Apache-2.0). On the 1090-pad board from
  `tests/route/test_scale.py`, a whole run takes 11 s instead of 184 s at
  a 10 mil grid, and 2.2 s instead of 32.5 s at 25 mil, with
  byte-identical output. Python stays the default. `rust` refuses to start
  without a matching wheel, and every routing result names its
  implementation in `summary.compute`. `tests/route/test_compute_parity.py`
  holds the two implementations to the same paths, verdicts and expansion
  counts. A new CI job builds the Windows wheel and uploads it. See
  `docs/RUST_COMPUTE.md`.

### Changed

- **The placer's annealing polish scores each move incrementally.** It
  used to re-score the whole board (every net's pins, every courtyard
  pair, the whole congestion grid) on every move; it now re-scores only
  the nets and parts the move touched. Same seed, same placement: every
  term is re-summed in the original order, so the score is bit-identical,
  and tests check that term by term over thousands of random moves.
  `construct_placement` on a 45-part board went from 37 s to 2.8 s, a
  124-part board from 82 s to 15 s (cloud Linux).
  `ConstructOptions(incremental_score=False)` restores full re-scoring.

- **The routing solution check rejects far-apart pairs by their bounding
  boxes before measuring.** Same violations, same order, same `checked`
  count. A box gap is never larger than the true distance, so no reported
  pair is skipped. The Python path on the 10 mil benchmark went from 184 s
  to 160 s.

- **The router sets up its jobs faster on boards with planes or excluded
  layers.** Each net's via guards worked out every item on those layers
  again: its clearance to the net's via and the grid cells it bars. Both
  are now worked out once and kept for the next net, and the barred cells
  are merged through one index per cell. Same cells for every net and via
  style, in the same order (checked on 792 jobs with both engines, and
  against a direct reckoning in the tests). On a board with 1,500 vias of
  other nets and two planes, setting up went from 26 s to 6.7 s.

### Removed

- **Upstream's sponsor note is gone from this fork** (`SCRIPT_VERSION`
  `2026.11.08.3`). The upstream merge in #20 brought in a "Finding
  eda-agent useful?" strip with a Sponsor button under the status
  window's log, a heart button above it, a "Support the project" section
  in the README, and upstream's GitHub Sponsors and Buy Me a Coffee
  handles in `.github/FUNDING.yml`. All four asked for money for
  upstream on this fork's behalf, which this fork had already undone
  once for FUNDING.yml (see the 2026-11-03 entry below). The note, the
  heart and their handlers are removed and the log hint gets back the
  width the heart took; FUNDING.yml is the blank template again. The
  update icon that came in the same upstream commit stays.
  `test_status_window_update_icon` and
  `test_fork_identity_points_at_morembedded` keep both from coming back
  at the next merge.

### Merged from upstream

- **27 commits, `214f5d3..5e126f6`.** Fifteen files conflicted. Four
  resolutions were not a choice of side:

  - `KnownPCBPropertyList` was unioned. The file's own comment warns that
    a list lagging the reader tells a caller a property does not exist
    when it does, so both sides' names are in it.
  - In `PCB_PlaceTracks`, upstream's block was a SECOND `Result`
    assignment after ours. Taking it would have dropped
    `skipped_existing`, `nets_not_found` and `connectivity_rebuilt`; ours
    is kept and upstream's current-layer report lifted into it.
  - `Gen_BatchDelete` took upstream's rewrite whole: our side had become a
    stale duplicate of the same logic under different variable names.
  - `proj_sync_pcb` moved onto upstream's `send_answering`, and pad-net
    checking folded into `_after_sync`, which reports mismatched pads as
    well as unbound ones. Our `verify_pad_nets` knob and `_verify_pad_nets`
    helper were deleted rather than left as a second way to say the same
    thing.

### Fixed

- **Fan-out vias keep off copper the router does not see.** A BGA's
  fan-out and the plane fan-out laid their vias outside the search, so
  they skipped its hole-to-hole and hidden-copper guards: a fan-out via
  could land on another net's copper on an excluded layer or a plane
  layer (a short), or too near a drilled hole. Both now check what the
  search checks. That check also got faster: it looks only at what lies
  near the via, so with 3,200 items on a layer no route uses, a clear
  spot takes 0.05 ms to check instead of 18 ms.
- **New vias stay out of keep-outs on layers no route uses.** The router
  draws a keep-out only on the layers it routes (and the Keep-Out
  layer's on all of them), but a via passes the others too: one could
  stand in a keep-out drawn on an excluded layer or a pour taken as a
  plane. The search and both fan-outs now keep out of those.
- **A via in a pad is checked where it is laid.** The search lays a via
  in a pad at the pad's centre, but checked the hole gap, hidden copper
  and mask web at the centre of the nearest grid cell, up to half a
  cell's diagonal away (3 mil on a 4.2 mil grid). It is now checked at
  the pad's centre as well.
- **New vias keep the mask web on an excluded outer layer.** The router's
  vias go through every copper layer, but its mask guard counted only
  the openings on the layers it routes: with the bottom layer excluded,
  a new via could stand 4 mil of mask from a via opening there where the
  sliver rule asks for 10. Both outer layers' openings now count.
- **A `pad_vias` the router does not know fails the routing job.** A
  misspelt value was routed as `"bga"` without a word; any case, and the
  spelling `"center"`, are now taken as meant, given as a dict or as a
  `StageOptions` object. `"centre"`, which only Euan's router takes, is
  routed as `"bga"` by the default engine, and the job's notes say so,
  and say when its fallback widened that to IC pins.
- **A new via's mask opening runs together only with its own net's.**
  Where a pad may take a via (a BGA's ball by default), the mask guard
  let a new via's opening merge into that pad's opening whatever the
  pad's net: on a 0.8 mm BGA a dog-bone via stood 7.3 mil from three
  other nets' balls, its opening one with theirs, no solder dam between
  them, and the verdict passed it. Openings that already meet count as
  one, and a new via's may merge only into one whose every pad and via
  is of its net, each pad one that may take a via, and never where it
  would join two nets' openings: next to another net's via whose opening
  ran into that net's pad, a new via shared the pad's opening too. Any
  other opening keeps the board's mask web.
- **Stage numbers are range-checked before routing.** A negative
  `lane_cost` gave the shortest path search negative weights, and the
  server process ran out of memory and aborted, with no job error; a
  `lane_depth` of 1e7 mil held a job worker for hours. `lane_cost`,
  `lane_depth`, `pair_skew`, `pair_width`, `pair_gap`, `fine_pitch` and
  `bus_min` out of range, or not numbers, now fail the job at once, and
  so does a number too large to compute with (a 400-digit `lane_cost`
  passed the check and overflowed mid-job); the engines get each as a
  plain float.
- **A differential pair's skew bump can no longer loop for ever.** When
  a bump had to be cut to its run, rounding could leave it a hair too
  long, and the same height came back on every pass: the job never
  returned (seen with a tight `pair_skew`).
- **A pad smaller than a grid cell is entered from a cell the router
  keeps clear.** When no cell centre falls in a pad, the track ends at
  the nearest cell's centre, outside the pad; that cell was taken as
  all pad copper and left out of the router's own clearance map, so
  another net could come within clearance of the track end. At Euan's
  coarser pitches 43 of 1,155 random small-pad boards broke a
  clearance; none do now. The two cells nearest two such pads of
  different nets are judged by the tracks that end on them, without the
  margin a diagonal step needs, and an escape stub from a pad by its own
  copper rather than a disc round each cell it crosses: random boards of
  0201 parts at pitch 15 and 20 route 100% and 97.5%, where the first
  version of this fix left 75% and 52% (and the router before it 89%
  and 82%, with clearance violations on 12 of 160 boards).
- **A blind via is no way into a plane or pour beyond its span.** A pad
  wired to a via that stops short of its net's plane layer was taken as
  already in the plane, and a pad on a blind via as joined to a pour on
  a layer the via never reaches: the pad was never routed, and the
  verdict found the net open. A via now counts only on the layers it
  passes, in the plane fan-out's count of pads already joined too, and
  the routing attempt after a plane is given back routes again a pad
  whose only way in was through that plane.
- **The DRC verdict joins a plane layer only through vias that reach
  it.** It took every via of an internal plane layer's net as joined to
  the plane, so a pad wired to a blind via that stops short of the
  plane counted as in it while no copper reached it, and a board left
  that way was reported complete.
- **The gloss keeps out of every keep-out.** It honoured keep-outs on the
  routed layer only and missed arcs, so it could straighten a route
  through a keep-out drawn as Keep-Out layer tracks (which bar every
  copper layer) or as a ring of arc round a mounting hole.
- **The `eng_ltspice_*` tools now find LTspice XVII.** They looked for
  LTspice 24 only, while the `sim_*` tools found either; a wrong
  `EDA_AGENT_LTSPICE_EXE` is now reported instead of being ignored.
- **`.MEAS` results with a lower-case `at`/`from`/`to`** (as LTspice XVII
  writes them) are read; they were dropped from `measurements`.
- **`rotation_audit` no longer lets a part on the other side block a
  turn.** A turned part's courtyard was checked against every other
  part's, so a bottom-side part under a top-side part was never offered
  a turn (and the other way round), though courtyards on opposite sides
  never meet. It now checks the same side only, the rule
  `placement_audit` already uses. Checked live on 2026-11-12 (Altium
  26.11.1.5) on a copy of BoosterTest: a backwards bottom-side
  WE-PMI 1008 under the same footprint on top got turn 180, saving
  173.2 mils; applied and read back, the pads swapped as predicted and
  the connection shortened by exactly 173.2 mils. Main suggested nothing
  there. With both parts on the bottom, every turn is still blocked.

- **`proj_run_erc` and `proj_get_erc_violations` stopped the bridge**
  (`SCRIPT_VERSION` `2026.11.10.1`). `Gen_GetErcViolations` built each
  violation with `IntToStr(ErrorLevel)` and `SuppressedText`, neither
  declared anywhere, so Altium 26.11.1.5 halted with "Undeclared
  identifier" on the first violation. `error_level` now carries the
  numeric level and the duplicate `suppressed` key is gone. The same scan
  found `FindPadNetByName`, whose definition a merge on 2026-10-10
  dropped while the `pcb.place_via` and `pcb.place_track` commands (given
  a net) and `pcb_rebind_copper_to_pad_nets` kept calling it; it is
  restored. A new
  lint rule, `undeclared-read`, fails the build on any bare identifier no
  unit declares and `scripts/altium/lint_external_names.txt` does not
  list. UNVERIFIED live: checked by the linter and tests, not yet run in
  Altium.

- **app_list_panels, app_open_panel and app_read_panel on AD 26.11.** They
  ran on the event loop thread without COM, and 26.11 has no
  TPopupPanelButton for a docked panel, so they saw 0 panels. They now run
  on a thread of their own and also find a panel by its caption; live on
  26.11 they list Projects, Properties and a floating Explorer, and read
  the Projects grid.
- **Altium 365 settings set with `setx` work without a restart**: the
  user's saved environment fills in what the process lacks.
  `EDA_AGENT_ALTIUM365_TIMEOUT_S` sets the request timeout.

- **Nexar sign-in works with a portal app.** `eda-agent altium365-login`
  sent the client id and PKCE verifier but no client secret, so an app made
  in the Nexar portal (a confidential client) was turned down with HTTP 400.
  `EDA_AGENT_NEXAR_CLIENT_SECRET`, when set, now goes with the code exchange
  and with each refresh. It is kept out of the config's printed form and
  masked if a reply echoes it. A refused token request now says what Nexar
  said (`invalid_client`, `invalid_grant`, ...) with a likely fix, instead
  of only the status. Checked live on 2026-11-09: the login succeeded with
  a portal app and its secret, and listed the account's workspaces.

- **The login says which workspaces it reaches.** After signing in,
  `eda-agent altium365-login` lists every Altium 365 workspace the account
  can reach, marks any that `EDA_AGENT_ALTIUM365_WORKSPACES` leaves out, and
  says how to keep the tools to some of them. The docs no longer send
  readers to an Admin > Developer token page that many plans lack; there,
  Nexar is the way in.

- **What the 2026-11-08 live verification found** (`SCRIPT_VERSION`
  `2026.11.08.5`). Each was seen on a live Altium 26.11.1.5 against
  scratch files:
  - `app_open_document` and `app_reload_document` could not open a
    harness wiring diagram: `.wirdoc` had no kind, so a reload closed the
    diagram and failed to reopen it, and `kind=HARNESS` reported success
    for a document `sch_get_harness` could not see. A .WirDoc now opens
    through the workspace manager, and the reply's `kind` is what Altium
    gave the document.
  - A harness wire, splice or cable answered `Name` with nothing, so
    `obj_delete filter="Name=W2"` matched nothing. It now answers the
    name its description carries, the same one `sch_get_harness` shows.
  - `profile_apply_all` wrote the ERC matrix of whichever project had
    focus, which with a free board in front was Altium's "Free
    Documents". Writing it now needs `project_path`, "Free Documents" is
    refused by the diff, preview and apply and by `proj_erc_apply`, and
    the dashboard's Workspace view passes the project its Compare read.
  - A dry run listed its sections under `applied`; they are under
    `would_apply` now. `design_list_modes` flags a selected mode that
    does not exist. `obj_modify` answers `modified:false` when every
    property it asked for was refused.

- **The merged pad-net check read only the first 500 pads.**
  `pcb.get_pad_properties` windows at 500 and reports `truncated`;
  `_after_sync` called it once with no paging and ignored that, so on any
  real board it would have reported a fraction of the unbound pads as if
  it were the whole board. That is the bug the check was written to catch,
  reappearing in a new place. It now goes through `_fetch_all_pads`, and
  its two sweep counters are named `pads_swept` and `sweep_complete`
  because `verify_eco` already answers `complete` and `pads_checked` with
  different meanings.

- **`proj_sync_pcb`'s `wait_seconds` had become a dead parameter.** The
  rewrite onto `send_answering` left it in the signature, and so in the
  tool's schema, with nothing reading it. It is passed as the waiter's
  timeout again.

- **The sync tests were environment-dependent.** `driver` returns a dialog
  presser only when Dialogs Automatic is on AND Altium's process can be
  named, so the tests took one path on a machine with Altium open and
  another in CI. They force it off now, and the fake bridge implements the
  `expecting` contract `send_answering` requires.

### Added

- **`pcb_get_overlay_tracks`, which makes two existing tools reachable.**
  `pcb_clone_silk_track_fragment` and `pcb_apply_silk_clip_plan` both
  identify a silkscreen track by its object address, and nothing in the
  bridge emitted one: the only place an overlay track address ever
  appeared was in clone's own reply, which you need an address to get.
  Both handlers were unreachable, which is why neither had ever been run
  against a board. The reader gives each track its address, layer, owning
  component, width and endpoints. The endpoints are RAW Altium
  coordinates, because a clip plan compares them as text against
  `IntToStr(Track.X1)` and a value converted to millimetres and back
  would never match; the same figures in mils are alongside under
  different names, for a human. Verified live on a real board: 8 tracks,
  addresses, and the owning designator. Script version `2026.11.04.13`.

  With it, both silk writers ran against a board for the first time. A
  real footprint was placed on a disposable board for its silkscreen,
  since the disposables carry no components and a component owner is
  required. `pcb_clone_silk_track_fragment` took the track count from 3
  to 4 and refused a fragment off the original segment with
  NOT_SUBSEGMENT. `pcb_apply_silk_clip_plan` validated 1 line, applied
  it, and reported it `already_applied` on a second pass, with the track
  gone from the overlay afterwards; a wrong assembly layer, a
  non-overlay layer, an unfocused board and a missing plan file were
  each refused by name.

- **`sdk-probe` can aim at any schematic object, not only a component.**
  The planner handled all 37 types all along; the CLI passed "component"
  and the Pascal filtered on `eSchComponent` whatever it was asked for,
  so the ten names declared on `ISch_GraphicalObject` itself, which are
  most of what the other thirty-one types were missing, could only ever
  be measured through a component. `--object-type` and `--index` now
  reach them, and a non-component is probed against the shared base block
  rather than the component chain, which would have reported every name
  missing.

- **`profile_diff_all` and `profile_apply_all`.** One call that puts
  everything a profile declares onto what is open: the design rules, the
  ERC matrix, and the schematic units and grid. A section the profile does
  not define is skipped rather than failed, because a profile declaring
  only an ERC policy is a complete profile. One section failing does not
  hide the others: the design rules need a board open and the ERC matrix
  needs a project, so a caller with only one of them still gets the
  other's answer, and `applied` and `failed` name the sections either way.
  `document_settings` is reported as apply-only, since nothing reads a
  schematic's units back to compare against.

- **Every writable project option can now be read back.**
  `proj_get_options` was missing four of the options `proj_project_setup`
  writes, so holding a project against a stored policy meant applying it
  with one tool and checking it with another that reported different
  fields. It now also reports `pin_swap_by_pin`, `pin_swap_by_netlabel`,
  `default_pcb_type` and `default_configuration_name`.

  `proj_project_setup` gained the two options it could not write,
  `default_pcb_type` and `default_configuration_name`, read back and
  listed in `not_applied` like the rest. They went there rather than into
  a new tool: a second writer over the same state, under its own option
  names, is one more place for the two to disagree about what a project
  is set to.

  Still not writable, and reported rather than attempted:
  `channel_designator_format` and `channel_room_separator`, for which the
  engine registers no setter under any spelling; and
  `netlist_single_pin_nets`, `log_folder_path` and the difference levels,
  which are `IProjectProperties` members while `IProject` is a sibling of
  that interface rather than a descendant, so calling one blind risks
  stopping the polling loop behind an exception dialog. Script version
  `2026.11.04.11`.

- **An ERC-matrix manager, run from the workspace profile.** Project
  Options > Error Reporting lives in the `.PrjPcb`, so every new project
  starts from Altium's defaults and a house policy survives only as long
  as somebody remembers to re-enter it. A profile's new `erc_levels.json`
  holds that policy, and three tools work over it: `proj_erc_plan` says
  what the profile asks for (pure Python, no Altium), `proj_erc_diff`
  says how the open project differs, and `proj_erc_apply` closes the gap.

  **Sets compose.** Named sets live in `erc_levels/<name>.json` and a set
  may `extends` another, resolved before anything is compared with the
  nearer file winning, so a company base policy and a per-workspace
  narrowing of it stay two files rather than two copies. A level of
  `null` drops an inherited entry, because "this workspace does not
  manage that type" is a different statement from "set it to no_report".

  **A type the set does not name is left alone**, and reported under
  `unmanaged`; `"unlisted": "<level>"` opts into pinning the whole matrix
  for an owner who wants that. A type this Altium version does not have
  is reported under `unknown` rather than skipped: the only name a
  violation type has is Altium's own prose, and a policy quietly missing
  half its entries is worse than one that says so. Matching folds exactly
  the way the Pascal writer folds, guarded by a test that reads the
  Pascal, so a diff saying "same" can never be followed by a write saying
  "no violation type by that name".

  Apply writes only the types that differ, so a second apply writes
  nothing, and saves the project afterwards because an unsaved matrix is
  lost when the project closes. `eda-agent profile-validate` resolves
  every set in the profile, including those reached only through
  `extends`.

  Verified live on a disposable project: a first apply wrote and saved
  all three types the set named, a second wrote nothing and read
  `in_step`, and the levels read back exactly as asked. A diff of a real
  project matched three types by Altium's own prose with nothing
  `unknown`, and a deliberately misspelt type came back `unknown` rather
  than being skipped.

- **A design-rule manager, run from the workspace profile.** A profile's
  new `design_rules.json` holds the TARGETS a board's Altium design rules
  are built from (which fab profile, per-class current, differential
  impedance), and three tools work over them: `pcb_rules_plan` says what
  the profile asks for (pure Python, no Altium), `pcb_rules_diff` says how
  the open board differs, and `pcb_rules_apply` closes the gap.

  **Apply is idempotent.** Altium's own `PCB_CreateDesignRule` sets the
  name and adds with no check for a name already there, so applying a
  rule set twice left two rules sharing a name and Altium's priority
  order picked the winner. A rule already on the board is now updated in
  place instead. Verified live: a first apply took a board from 35 rules
  to 39, a second left it at 39 with nothing written, and moving the
  fab's minimum gap from 7 to 9 mil updated the rule rather than adding
  one.

  **Nothing is ever deleted.** Three things are reported instead: a rule
  the profile does not define (Altium ships defaults on every board, and
  a rule somebody added deliberately is not drift), a rule the schematic
  owns (the next update from the schematic rewrites it), and a rule kind
  with no in-place writer (deleting and recreating would lose the rule's
  priority and any hand-edited scope).

  The file stores targets rather than computed rules because
  `design/rule_synthesis.py` asserts that every number it emits traces to
  a fab-profile field or a verified calculator; a stored number could
  only drift away from the calculator that justified it.

### Fixed

- **The two silk tools now say what a plan line must carry.** Running
  them against a board for the first time, which
  `pcb_get_overlay_tracks` made possible, turned up two requirements
  nothing documented. A plan line without `width` is refused with
  STALE_PLAN, because the missing field reads as a width that changed. A
  track with no component owner is refused with BAD_OWNER however
  complete the rest of the line is, so a board of free silk cannot
  exercise the tool at all. Both are in `pcb_apply_silk_clip_plan`'s
  docstring now, with the rest of the required fields, and guarded by
  tests that read the handler.


- **`design_list_laminates` listed a different, smaller, second-hand set
  than `design_get_laminate` answered for.** There were two laminate
  tables: `data/stackups/laminates.json`, which the list read, and
  `design/fab_data/laminates.json`, which the by-name lookup read. The
  first held three laminates transcribed from third-party reproductions
  of the Rogers data sheet; the second held four with the maker's own URL
  and document revision. So 370HR and MEGTRON 6 were missing from the
  list while asking for either by name worked perfectly well. The
  duplicate is deleted, both read one file, and a test fails if it comes
  back.

  Nothing was lost in the move: the Tg figures are carried across saying
  in the record that they came from that earlier transcription and were
  not re-read, the generic-FR4 range is kept as a convention rather than
  a laminate, and JLCPCB's figure for its own 7628 stack moved to
  `fab_figures`, out of a list whose note promises datasheet figures
  only. Two further corrections on the way: a laminate that publishes
  typical values by frequency and no process value now leaves
  `er_process` empty instead of borrowing a typical figure and labelling
  it a QC measurement, and every laminate carries `er_for_design`, the
  one number to size an impedance with, saying which kind of value it is.

- **A failed `default_configuration_name` write cleared the name already
  set.** Measured on a live project: naming a configuration the project
  does not have did not take AND blanked the existing name, from
  `Sources` to empty, so a write that failed left the project worse off
  than not calling at all. `proj_project_setup` now reads the previous
  value first and puts it back when the read-back disagrees. Verified:
  the invalid name is refused, `not_applied` names it, and `Sources`
  survives.

- **`proj_project_setup` reported a successful `output_path` as
  `not_applied`.** A relative output path is resolved against the project
  folder, so the read-back is the absolute path and the exact comparison
  never matched: `Outputs Test` came back as
  `<project folder>\Outputs Test` and was reported as not applied while
  the reader showed the new value. An absolute path still has to match in
  full.

- **`proj_get_erc_levels` reported 295 violation types where the project
  has 167.** `DM_ViolationTypeDescription` answers by index with no end
  marker, and past the end of the enum it returns whatever adjacent
  memory reads as a string, so a fixed 0..400 scan that kept anything
  non-empty picked up 128 extras: truncated fragments of other string
  tables (`Out o`, `Unconnec`, `Gray`) and, at index 350, a block of raw
  UTF-16 carrying NULs and an `=`. The reader now stops at the first
  empty description, which is the end of a contiguous enum, and reports
  `type_count` and `beyond_table` so the measurement that justifies the
  bound travels with every reply. `ErcKindFromText` refuses a kind number
  past the end for the same reason, since `proj_set_erc_levels` would
  otherwise have called `DM_SetErrorLevels` on an index outside the array
  it writes to. Each type now also carries its `group`, the section the
  Error Reporting dialog files it under. Script version `2026.11.04.10`.

  Verified live afterwards: the reader returns exactly 167 types, all of
  them printable, every one with a group in 0..16, ending at `Generic
  Component Not Replaced With Library Component`; `beyond_table` read 127
  on one pass and 128 on another, which is what uninitialised memory
  does. Writing a kind past the end is refused, with `set: 0` and
  `failed: 1`.

- **`pcb_rules_plan` and `pcb_rules_diff` were advertised as mutating.**
  Both the `pcb_` and `proj_` prefixes default to `silent`, and the
  readonly marker for a diff is only recognised with a trailing
  underscore, so a tool whose whole purpose is "show me what would
  change" was published to the discovery layer as one that changes
  something. Both are now `readonly`, and `pcb_rules_plan`, which reads a
  profile off local disk and never calls the bridge, is `offline`.

- **`drc_enabled` is now reported for every design rule.** A rule carries
  both `Enabled` and `DRCEnabled`, and only the second decides whether
  DRC checks it. `pcb_get_design_rules` and `pcb_get_rule_properties`
  reported the first. Measured on a real six-layer board: 37 of its 159
  rules read `enabled: true` while DRC ignored them, and the only sign
  was the word `(Disabled)` buried in Altium's descriptor prose, which is
  what `pcb_set_rules_enabled` had been writing all along.

- **A one-line routine body hid every global a unit declared after it.**
  The DelphiScript linter found the end of a routine by looking for
  `End;` in column 0, so a body written as a single
  `Begin DoIt(x); End;` line had no end it could see, and the unit-level
  `Var` block that followed was never harvested. Two of those arrived in
  `StatusForm.pas` ahead of the pump's `Var` block, and 22 correct
  assignments to the globals it declares were reported as undeclared.
  The linter now recognises a whole body written on one line.

- **An OutJob path that is not there is refused before Altium sees it.**
  Measured on AD 26.11: `proj_run_outjob` with an `outjob_path` that does
  not exist did not fail cleanly. Altium opened a new, empty OutJob at
  that path and made it the ACTIVE document, so the run wrote nothing
  and every later tool refused to act on the document now in focus. The
  path is checked while it is still a string, and nothing is sent.

- **`src/eda_agent/ui/panel_wheel.py` had no licence line.** It arrived
  from upstream without the SPDX identifier or the copyright notice
  every other file in the tree carries. Both restored; Apache-2.0
  section 4(c) asks for the second.

- **A layer only the Layer Stack Manager can have no longer deadlocks
  the two tools that read it.** Found by running them against a real
  six-layer board: the manager carries `Top Surface Finish` and
  `Bottom Surface Finish`, type `other`, and the board's physical stack
  has no layer object for either, so `stack_manager_out_of_step`
  reported them as layers the two sides disagree about.
  `pcb_move_layer` then refused and sent the caller to
  `pcb_sync_stack_manager`, which refused and sent them back to the
  manager, where the layers cannot be removed. Neither tool could run
  on the board at all, and `pcb_modify_layer`'s follow-up sync failed
  the same way on every write. A manager layer of type `other` is now
  reported as `manager_only` and blocks nothing; the comparison already
  dropped paste layers in the mirror direction for the same reason. A
  manager-only layer of any other type, and a board layer the manager
  lacks, still block as before. Verified live: the move reordered a
  symmetric six-layer stack and put it back.

### Changed

- **Merged from upstream (salitronic/eda-agent, 6 commits, 2026-11-04).**
  A Config tab on the Altium status panel, scrolled with the mouse wheel
  from outside the script engine; a writer for a text's `Rotation` and
  for any primitive's `Locked`; `pcb_special_strings`, which lists and
  resolves the board's special strings; component parameters read by
  group on AD 25; and `app_format_value`, a component value formatter in
  the RKM code of IEC 60062. Upstream's `SCRIPT_VERSION` had reached
  `2026.11.04.7` and this fork's `2026.11.03.6`; the merged bundle is
  neither, so it is `2026.11.04.8`.

- **This fork's own pointers now name this fork.** The update
  checker asked `salitronic/eda-agent` whether a pip install was
  current, the SARIF reports written by `review` carried upstream's
  `helpUri` and `informationUri`, four outbound User-Agent strings
  identified this fork's traffic as upstream's, and `.github/FUNDING.yml`
  put upstream's GitHub Sponsors and Buy Me a Coffee handles on this
  repository's Sponsor button. Those, the `pyproject` URLs and author,
  the README clone line, the issue template's security link, the
  report address in `SECURITY.md` and `CODE_OF_CONDUCT.md`, and the
  GitHub Actions workflow in `docs/HARDWARE_CI.md` now point at
  `MOREmbedded/eda-agent`. That workflow installed the package from
  PyPI, where the name `eda-agent` is upstream's and this fork cannot
  publish, so it installs from git instead; there is no
  `pip install eda-agent` that fetches this fork. The update
  checker's git path already
  followed whatever `origin` is, so clones were never affected; only
  the package-install path was. The Apache-2.0 SPDX headers are
  untouched, because retaining those notices is a licence condition
  rather than a branding choice, and the upstream project keeps one
  credit line in the README and its entries in this file.

- **Merged upstream through d5d1f2a (4 commits), and broke a version
  tie at `2026.11.03.6`.** Upstream released `2026.11.03.5` for its own
  script body hours after this fork had taken that number for a
  different one. Both sides reach the same constant, so git merges it
  without a conflict and the version quietly stops identifying a
  build: three distinct bundles would answer `app_ping` with `.5`.
  Bumped to `2026.11.03.6`. The four commits: a failed OutJob run
  reports instead of stopping the script, tracking what it opened
  through `RunOutJobsOpened`; `pcb_embedded_data` passes text through
  untouched; each `tool_guide` avoid entry carries its tool and
  reason; and the restart test writes the response before removing the
  request.

- **Merged upstream through 4f4d76f (22 commits), and reached
  mechanical 17 and up throughout this fork's own handlers.** Most of
  the batch is a second contributor's: clearance between two objects,
  a physical-connectivity walk from a seed, object counts from the
  primitive counter, selection that ignores the Selection Filter,
  named text records stored in the board file, the PCB editor's
  visible area, Online DRC through `pcb_edit_options`, region holes in
  `pcb_apply_geometry_map`, free-primitive nets taken from component
  pads, and reads and moves through the Layer Stack Manager. The
  Altium backend advertises 687 tools, 936 with EasyEDA too.

- **Mechanical layers 17 and above are named and walked everywhere.**
  Altium 25 allows 1024 of them; `GetLayerString` and the legacy
  `AllLayers` set stop at 16, so anything higher came back `Unknown`
  or was skipped. Upstream closed this on their side and left a guard
  test over the Pascal. It failed here on 28 call sites in handlers
  this fork wrote and upstream has never seen: 16 layer names and 12
  board walks in `PCB.pas`, plus one each in `Generic.pas` and
  `Library.pas`. All now use `LayerDisplayName` and
  `AddFilter_IPCB_LayerSet(LayerSet.AllLayers)`.

- **SCRIPT_VERSION is `2026.11.03.5`.** Upstream was at
  `2026.11.03.4`, and the two forks reached `2026.11.02.6`
  independently the day before with different code. A fork suffix was
  tried and dropped: the release guard reads any `20YY.MM.DD.N` in the
  procedure as a version naming itself, and a suffixed string contains
  one. What actually catches a collision is `health`, which compares
  deployed against tree BY CONTENT rather than by this number.

- **Merged upstream through 51d3899 (12 commits).** Mostly routing:
  `pcb_autoroute` takes pad-entry, neck and join options, reaches pads
  along their axes and necks down only at narrow pads, routes only what
  the board's copper leaves apart, and keeps new vias off holes, off
  copper the grid cannot see, and clear of solder-mask slivers. The
  layout DRC checks hole-to-hole clearance. Update checking hears of new
  commits within the hour rather than the day, and a reinstall survives
  a running server holding the launcher.

- **Copper asked onto a net the board does not have is refused, not
  placed unbound.** `pcb_place_tracks` counted such a segment and placed
  it netless; it is now counted in `failed` and `nets_not_found` and
  named in `unknown_nets`, and no copper goes down. Upstream's change,
  and their new guard over the Pascal (every handler that looks a net up
  by name and creates copper for it must act on the lookup finding
  nothing) found the same hole in `pcb_place_region_poly`, which is this
  fork's own handler and which upstream has never seen. Both refuse now.
  SCRIPT_VERSION is `2026.11.03.1`: both forks had independently bumped
  to `2026.11.02.6` with different contents.

- **Merged upstream through 89abcf9 (26 commits).** New on the board:
  `pcb_tidy_tracks` relays free tracks as the shortest clean 45/90 runs,
  one pour's poured copper is readable, a generic filter matches
  wildcard patterns and is walked only on the layer it names, and the
  geometry map's dump narrows by layer, net, selection, name and area.
  New in libraries: a symbol's Component Type is readable and settable
  through `obj_modify`, PcbLib pads can be moved, renumbered and
  deleted, and a footprint's text set. `sch_reload_document` closes and
  reopens one sheet so its nets compile from what the sheet holds.
  Update checking now reports on the next tool reply, says once when a
  check fails, and tells a pip install how to upgrade.

  Two conflicts were settled against this fork. `lib_create_symbol_in_parts`
  takes upstream's `shared_pins` shape, an object of `left_pins` and
  `right_pins` auto-placed on Part Zero, in place of this fork's flat
  list of pins carrying their own x and y: **a call written against the
  list form is now refused by shape**, with a message naming what to
  send. Upstream grew its own `altium365-logout`, so the one this fork
  carried across is dropped along with its duplicate test; `sdk-reach`
  stays out, as this fork reads the SDK through `sdk-coverage`.
  SCRIPT_VERSION is `2026.11.02.6`, which is neither fork's.

- **The NPI phase gates are off until an owner asks for them.** The five
  stages and their 42 checks shipped switched on, so every install
  advertised `design_phase_gate`, `design_phase_answer` and
  `design_phase_standing` and carried a stage vocabulary through every
  prompt, for a process most boards are not run under. They are now one
  key in the owner profile (`phase_gates.json`, `{"stages": [...]}`),
  with `eda-agent phases-show` / `phases-enable` / `phases-disable` to
  set it, and `EDA_AGENT_PHASES` or `--phases` to overrule it for one
  run. Off means **not advertised** rather than advertised and refusing:
  a tool that exists only to say it is switched off costs a call and a
  round trip to teach what its absence teaches for free, and `tool_guide`
  withholds the recipe for the same reason.

  Enabling a subset is a weaker claim than enabling all five, and the
  reply says so: `design_phase_standing` reviews only the stages in
  force and reports them in `stages_run`, so `in_production` can be read
  against what it rests on. A stage that is off is refused rather than
  answered, and so is an answer recorded against one - evidence nothing
  reviews is worse than no evidence. Switching stages off never touches
  the ledger; the answers count again the moment the stages do.

  The default Altium surface is 675 tools rather than 678, and the
  README now says so.

### Fixed

- **Two files had been committed CRLF while the whole tree is LF**, in
  the 0.8.0 merge, so git read every one of their 14,000 lines as
  rewritten and each conflicted whole-file on every upstream merge.
  Twenty-eight of those endings were doubled (`


`), which Python
  reads as two newlines, so four tool docstrings carried a blank line in
  the middle of a sentence - and a tool's docstring is what the model is
  served as its description. A `.gitattributes` now pins the tree to LF
  so a conflict resolved on Windows cannot do it again.

- **`pcb_tidy_tracks` threw at its caller** instead of reporting. It
  caught `WrongBoard` from `read_live_board` but not the `ValueError`
  that same function raises when no board is named, against this
  project's rule that a tool refuses or reports and never hands an MCP
  client a traceback. Found by the merge, and present upstream, which
  fixed it the same day; the fix in this tree is upstream's catch-all,
  the form the other six `read_live_board` call sites already used.

- **`--phases` on a subcommand would have printed nothing.** The
  `--backend` and `--toolset` flags re-exec so the new tool surface is
  registered at import, and `os.execv` on Windows does not replace the
  process: it starts a detached one and exits, so the subcommand's
  output goes nowhere and the shell sees exit 0. `--phases` re-execs
  only for `serve`, which is the only command whose answer depends on
  the surface; every other command reads the setting when it runs.
  `--backend` and `--toolset` still carry this, and are left as they
  are - MCP clients pass both by environment, which never re-execs.

- **Property coverage 36.4% to 73.7%, from counting before generating.**
  The next step after the `ISch_Component` chain looked like a
  generated chain per object type: 32 schematic, 14 board. Counting the
  gap first said otherwise. Of the 442 unreachable schematic read
  slots, ten names accounted for 310, and all ten are declared on
  `ISch_GraphicalObject`. On the board side 42 names were declared on
  all 14 types, of which 37 survive the type and symbol filters. Both
  base interfaces are already the parameter type of the chain that
  would call them, so one routine per side reaches those names for
  every object with no cast and no `ObjectId` test, where 46 per-type
  chains would have repeated the same lines. `GetSchGraphicalGenerated`
  (10 lines, schematic 45.3% to 75.1%) and `GetPCBPrimitiveGenerated`
  (37 lines, board 28.5% to 72.4%). A type's own chain is still tried
  first and the base chain is the fallback behind it; the two overlap
  deliberately, so either is correct alone, and a test asserts they
  spell every shared read the same way. Coverage credits a base chain
  to every type rather than to a constant, because no guard gates it.
  `CompilationMasked` is declared on both bases and excluded from both,
  by measurement rather than by type. Verified live on 2026-11-02
  against AD 26.11.1.5: all ten schematic reads off a wire and all 37
  board reads off a track, no fault, 41 of the 47 returning a value and
  the rest empty on objects that plausibly have none.

### Fixed

- **37 board property reads that the gate refused before they ran.**
  `GetPCBProperty` is not reached directly: `Gen_QueryObjects` refuses
  a name `IsKnownPCBProperty` does not recognise, because returning an
  empty string for a misspelling reads as "the data is not there" and
  had been reported three times as the bridge failing at something it
  could do. That guard is hand written, as is `KnownPCBPropertyList`
  beside it, and the comment above the list warns in as many words that
  a list lagging the reader tells a caller a property does not exist
  when it does. The generated reads were added without the names, so
  every one answered `UNKNOWN_PROPERTY` while the chain beneath could
  read all 37, and coverage reported 72.4% because it measures the
  chain and the chain was right. The gate is now generated from the
  same list the reads come from (`IsGeneratedPCBProperty`,
  `GeneratedPCBPropertyList`), so it cannot lag again. The schematic
  side has no such gate, which is why only one half was affected.

- **SDK coverage: one command, this fork's, with upstream's reach.**
  `eda-agent sdk-reach` (upstream) and `eda-agent sdk-coverage` (this
  fork) both counted how many of the properties Altium's SDK declares
  the four hand-written Pascal chains reach. This one is kept, against
  the rule the other merge duplicates followed, because three further
  commands and the code generator are built on it: `sdk-probe`,
  `sdk-symbols` and `sdk/generate.py` all read the reflected
  `reference/altium_sdk.json` that `sdk-coverage` reads, where
  `sdk-reach` read a separate index under `reference/altium_sdk/` whose
  dump script is not in the repository. Keeping upstream's would have
  meant rewriting all four onto a data source no clone can produce.
  Carried across: upstream counted names one call level below the
  chain, and this did not. That was the whole of the gap between the
  two figures. The chains answer most of what a pin, a port, a
  parameter or a dimension carries through routines of their own, and
  following them adds 116 reachable properties: 688 of 2207 (31.2%)
  becomes 804 of 2207 (36.4%). Upstream credited those names to every
  object type; here each goes only to the types the Pascal guards it
  on, either the `Obj.ObjectId` test on the branch or the owner table
  (`SchExtraFits`, `ExtraPCBPropertyFits`) that says which type owns
  each name. A routine called to decide whether a branch is taken, or
  called with a literal name, is not counted twice. The dotted-chain
  rule upstream documented (`Location.X` counts for `Location`) was
  already here. `docs/RELEASE_VERIFICATION.md` keeps its `sdk-reach`
  measurement, with a note that the two read different dumps of the SDK
  and so cannot be compared.

- **Multipart symbols: one tool, upstream's, with this fork's shared pins.**
  `lib_create_multipart_symbol` (this fork) and `lib_create_symbol_in_parts`
  (upstream) both made a multi-part symbol in one call, and neither did all
  of it. Upstream's is the base: it checks each step took, refuses a
  designator used on two parts, and draws every body before any pin, where
  this fork's drew the bodies last and so covered the pin names. What it
  lacked was this fork's `shared_pins`, pins on owner part 0 that Altium
  shows on every part, normally supply pins; it takes them now. A shared
  designator also used on a part, or given twice, is refused before
  anything reaches Altium. Checked end to end: the Pascal takes any explicit
  pin owner, 0 included (a body or an arc treats 0 as unset), and
  `_pins_payload` sends a 0 because it tests for None.

- **Phase gates: upstream's kept, this fork's NPI gates removed.** This
  fork's `design/npi.py` (`design_npi_gate`, `_record`, `_status`) and
  upstream's `design/phase_gates.py` (`design_phase_gate`, `_answer`,
  `_standing`) are the same 42 checks over the same five stages, split
  8/8/9/9/8 with 29 answered by tools in both. Seven of this fork's read as
  unmatched by wording; each is in upstream's set under another phrasing
  (`lead-times`, `stack-up`, `fits-enclosure`, `probe-points`,
  `return-paths`, `tests-traced`, `environment`). Upstream's adds an expiry
  per check and keeps the same three rules: answers expire, belong to a
  revision, and a waiver needs a reason. Its own test already resolves
  every tool a check names, folded members included, which was the guard
  this fork's test existed for. No ledger existed under
  `<workspace>/npi/`, so nothing recorded is lost.
  Carried across: the documentation. Upstream had none, so
  `docs/NPI_PHASE_GATES.md` now describes upstream's tools, keeping this
  fork's table of what failing each gate costs.

- **Altium 365: upstream's client kept, this fork's removed.** Both forks
  built the same thing: this fork's `engineering/a365.py` with fifteen
  `eng_a365_*` tools, upstream's `altium365.py` with thirteen `altium365_*`
  tools. They map almost one to one (this fork split two reads in two where
  upstream takes a flag), both reach a workspace directly or through Nexar,
  both send the upload's token as a bearer header to the workspace and as a
  `token` header to Nexar, and both refuse a `mutation` on the read path.
  Upstream's is kept because it is the one being developed:
  `sch_place_comment` was built on it on 2026-10-31, and keeping this fork's
  would have meant re-porting every such change. Nothing on the development
  machine depended on this fork's: no `EDA_AGENT_A365_*` variable was set
  and no saved login existed.
  Carried across: a logout. Upstream can sign in to Nexar but had no way to
  forget the login short of deleting the file by hand; `eda-agent
  altium365-logout` now does that. The environment variables to set are
  upstream's: `EDA_AGENT_ALTIUM365_DOMAIN`, `EDA_AGENT_ALTIUM365_TOKEN`,
  `EDA_AGENT_NEXAR_APP_ID` and `EDA_AGENT_ALTIUM365_WORKSPACES`.

### Added

- **Altium 365 can be reached directly, not only through Nexar.** Altium
  documents a GraphQL API on the workspace itself; this fork only ever
  spoke to Nexar, which proxies the design domain and carries none of
  the collaboration, workflow or script-execution contexts. Set
  `EDA_AGENT_A365_DOMAIN` and `EDA_AGENT_A365_TOKEN` and the same client
  posts to `https://{domain}/api/graphql/` with a bearer token created
  in the workspace under Admin > Developer.

  One client serves both, because the schema is the same `des*` one
  either way; what differs is the URL, where the token comes from, and
  that a workspace token has no refresh grant, so an expired one is
  reported rather than renewed against an identity server that never
  issued it. File uploads follow the route too: the Files Service at
  `/api/files` takes the bearer header, Nexar's upload service takes a
  `token` header, and sending the wrong one is a 401 with no clue.

- **`eng_a365_schema`** lists the fields of one type in the LIVE schema
  by introspection. That is how to reach the contexts Nexar does not
  proxy without anyone guessing a field name: start at `Query`, then
  name a type from the result. `eng_a365_query` already ran arbitrary
  read-only GraphQL, so no per-context tool had to be invented.

  UNVERIFIED against a live workspace. The twelve tests drive a fake
  transport, so what is proven is the request this code builds (URL,
  method, both headers, body, token precedence), not that Altium accepts
  it. Verifying needs a workspace domain and a token.


### Merged from upstream (salitronic/eda-agent, 63 commits, 2026-10-20 to 2026-10-26)

Both forks worked the same week, so several features existed twice. Where
they did, ONE survives. What was kept and what was dropped:

- **Decimal mils throughout.** Upstream's change: every handler now reads
  and writes fractional mils (`CoordToMilsF`, `MilsToCoordF`,
  `FloatToJsonStr`) instead of rounding to whole mils. Taken wholesale.
  Our extra JSON fields (`layer`, `bbox_basis`, `same_side_filter`,
  `bbox_includes`, `scanned_tracks`, `max_tracks`, `truncated`,
  `hole_size_mm`, `size_mm`) were folded back on top of it.

- **`rotation_audit`: upstream's engine, our measurement.** Both forks
  rewrote this check. Upstream's is broader: it scores every part with
  two or more pads rather than only two-pad parts, and it turns the
  part's courtyard and checks it against every other part's, so a
  suggestion can be applied as it stands. That engine was kept. Upstream
  withheld `new_rotation` on the bottom side, saying it needed an
  assumption about how a mirrored part's angle is counted; we measured
  that on 2026-10-25, so the value is now given on both sides and the
  measurement is recorded in the module and its test.

- **Bottom-side batch placement: ours kept.** Upstream's commit "Place
  bottom-side parts exactly" fixed the SINGLE-component path, where each
  placement has its own `PreProcess`/`PostProcess`. Their
  `PCB_PlaceComponents` still sets `Comp.Layer` inside one transaction
  wrapping the whole batch, which is the configuration measured here as
  NOT mirroring the primitives. Our fix stands: load on the top layer,
  flip after registration with `PCB_FlipComponent`, pre-compensating the
  requested rotation because a flip rewrites it to `180 - R`.

- **Calculators: ours kept, one of theirs ported.** Upstream built
  `design/calculators.py` (9 functions) while we built
  `engineering/pcb_calc.py` (10, plus 10 CLI verbs and three test
  files). Ours is kept, theirs deleted, because it covers everything
  theirs did plus IPC-2221 conductor spacing and a cross-check, and it
  is reachable from the CLI. Upstream's `via_inductance` was PORTED
  rather than dropped: it is a different question from our
  `pcb_calc_via_parasitics`, which gives a via in isolation and says
  outright that it ignores the return path, where theirs names the
  return and gives the loop inductance between two planes (Johnson,
  "Via Inductance II"). Both tools now exist.
  NOT taken: upstream's spiral-inductance sheet expressions, which cover
  a circular spiral that ours does not. `pcb_calc_spiral_inductor`
  remains modified-Wheeler, square, hexagonal and octagonal only.

- **ERC violations: upstream's handler, our field documentation.**
  Theirs adds paging (`offset`, `next_offset`, `include_suppressed`),
  ours added severity counts. The Pascal handler is upstream's, taken
  whole rather than hybridised, because it is live-only code that cannot
  be tested here. The tool now reports `violation_count`,
  `suppressed_count`, `not_reported_count` and `raw_violation_count`.
  LOST in the process: the per-severity `warning_count`, `error_count`
  and `fatal_count` this fork briefly had.

- **ECO: both.** Our refusal to fire while a modal is already open, and
  our bounded wait, are kept; upstream's `after_sync` report is folded
  in, so the board is read back against the schematic (`footprint_mismatch`,
  `missing_on_board`, `diff_pairs_removed`) rather than the dialog's
  outcome being trusted.

- **Footprint render and compare: upstream's kept.** Both forks built
  `lib_render_footprint` and `lib_compare_footprints`. Theirs takes
  `courtyard_layer` and `assembly_layer`; ours did not. Ours removed.
  Note the parameter rename: `output_path` is now `path`, `tol_mm` is
  now `tolerance_mm`.

- **Fab and stackup TOOLS: ours kept. The DATA is still duplicated.**
  Both forks shipped `design_list_fab_vendors`,
  `design_fab_vendor_profile`, `pcb_check_stackup` and
  `pcb_apply_stackup_template`. Ours (`tools/stackup.py` over
  `design/stackup_library.py`) is kept and upstream's four definitions
  were removed, so `pcb_check_stackup` keeps our `vendor` parameter
  rather than upstream's `template` / `thickness_tol_pct` / `er_tol`.

  But BOTH material datasets are still live, because upstream's
  `design_get_stackup`, `design_list_stackups` and `design_get_laminate`
  read theirs:

  | dataset | laminates |
  |---|---|
  | `data/stackups/laminates.json` (ours) | RO4003C, RO4350B, JLCPCB 7628 prepreg |
  | `design/fab_data/laminates.json` (upstream) | RO4003C, RO4350B, 370HR, MEGTRON 6 |

  Neither is a superset: each holds material the other does not, and
  they agree on only two. A board checked through our tools and one
  checked through theirs can therefore see different material. KNOWN
  GAP, not closed in this merge: the two want folding into one
  materials library.

- **The automatic schematic layout engine is gone**, with upstream's
  retirement of it: 84 files, including `design/layout.py`, the
  force-directed and Sugiyama placers, the quality model and the
  training scripts. All 84 were upstream's own code and this fork had
  never modified any of them. Four tools go with it:
  `design_layout_schematic`, `design_execute_plan`,
  `design_preview_plan` and `design_learn_from_layout`. The replacement
  is upstream's hand-template route, `sch_preview_template` and
  `sch_draw_template`.

- Other arrivals: 3D bodies in footprints (`lib_set_3d_body`,
  `lib_create_3d_model`, `lib_export_3d_model`), `lib_rotate_footprint`,
  `pcb_get_impedance_profiles`, ERC levels, `proj_create_outjob`,
  `proj_export_jlc_assembly`, `pcb_update_footprints_from_library`, a
  generic property layer for keepouts, polygon pours, primitive nets and
  via/pad tenting, and `server_update`, which offers updates once a day
  and installs them after you agree.

- Dead code removed on the way: `_execute_command_async`, which
  upstream's inline executor replaced as the only caller.
  `_poll_response_async` stays, since a test exercises it directly.

Tool count after the merge: **655 on the Altium backend**, 741 with both
registered, 990 across all backends. `SCRIPT_VERSION` is `2026.10.26.4`.

### Changed

- **The two version numbers are documented.** The package follows
  semantic versioning (`0.7.1`) and the Altium script is dated
  (`SCRIPT_VERSION`, `YYYY.MM.DD.N`), they cover different artifacts and
  they move independently, and nothing said so anywhere. A reader
  opening `Main.pas` met a year where a release number was expected.
  The README now gives both, says the script version exists to answer
  "is Altium running today's script", and says to bump the trailing
  `.N` on every `.pas` edit -- leaving it makes a reloaded Altium
  indistinguishable from a stale one, which has caused real confusion
  here more than once.

- `rotation_audit` now checks **bottom-side parts**. They were skipped
  while it was unknown whether a rotation value turns a mirrored part
  clockwise or anticlockwise in board coordinates. Measured against
  Altium 26.11.1.5 on 2026-10-25: four parts flipped with
  `pcb_flip_component` and read back turn their pads in world
  coordinates by exactly the delta applied to `Rotation`, the same as a
  top-side part, at 45 degrees as well as at right angles. The scoring
  never needed a side term -- it rotates the pad's WORLD position about
  the component origin, and the mirroring is already in the position
  the board reports.

### Fixed

- **`app_capability_probe` and `app_get_report` now ask Altium rather
  than the process table.** Both carried the shape the doctor entry
  below describes. `app_capability_probe(probe_live=True)` set
  `bridge_response` to None whenever the scan said Altium was absent, so
  every live feature read as missing while the bridge was answering.
  `app_get_report` gated the ping, the script version, the version match
  and the round-trip timing on that same scan, which is the worse of the
  two: that tool exists to say what is wrong with the plumbing, and on a
  machine whose process table cannot name a running Altium it reported
  the application down, with no version and no round trip.

  Both now ping first and keep the scan as a reported observation. The
  first attempt keeps the process gate, because where Altium really is
  absent it turns an IPC timeout into an immediate answer; only when
  that returns nothing does the ungated retry go out. `altium_running`
  is true when the scan saw Altium OR the bridge answered, and a new
  `process_scan_saw_altium` key carries what the scan alone said, so a
  caller can still see the two disagree. Checked against a live Altium
  with the scan forced to report nothing: script version, version match
  and a 38 ms round trip all came back where the old code returned
  None.

- **`eda-agent doctor` no longer reports a running Altium as absent.**
  The check printed `[FAIL] altium process running, no process named
  x2.exe found` with `fix: Start Altium Designer.` while Altium
  Designer 26.11.1.5 was on screen and answering bridge commands, and
  a FAIL there returned from `run_doctor_checks` before the bridge was
  ever pinged. So the weakest signal in the file set the exit code and
  silenced every better one, including the round trip that would have
  contradicted it.

  The doctor ran its own `psutil.process_iter` loop over one hardcoded
  name, while the bridge gates every command on `AltiumProcessManager`,
  which tries a Toolhelp snapshot first. On the reported machine those
  two disagreed: the bridge found Altium, and the doctor, `tasklist`,
  `Get-Process x2` and a scan over window titles all came back empty,
  so the executable was not merely renamed. The loop also looked only
  for `X2.exe`, never `DXP.exe`.

  The doctor now asks the same scan the command gate uses, and that
  scan no longer gates anything. A bridge round trip, or an
  `activity.log` entry under five minutes old, outranks it. With
  either, the check is a minor WARN that says the process table cannot
  see a running Altium and names the usual reasons - elevation, a
  different session or integrity level, restricted enumeration -
  instead of prescribing a fix for a state that is not the fault.
  `Start Altium Designer.` survives only where the scan finds nothing
  and nothing answers.

  `ping_with_version(require_process=False)` makes this reachable:
  without it the bridge's own gate reads the same process table the
  doctor cannot trust, refuses the ping before any IPC, and the run
  reports a healthy Altium as unresponsive. Only the doctor passes it.

- `pcb_place_components` documents that **`layer` sets the layer
  without mirroring**. A part placed with `layer="BottomLayer"` comes
  out with its pads on bottom copper in TOP-SIDE geometry, so its two
  pads are on the wrong sides; the same part through
  `pcb_flip_component` is mirrored properly and its rotation moves to
  180 minus the original. Found while measuring the rotation sign
  above. The docstring now says to place on the top and flip. The
  Pascal side still does not mirror, so this is a documented
  limitation rather than a repair.

### Merged from upstream 0.7.1

This fork absorbed the 24 commits it was behind
`salitronic/eda-agent`, which is where the version jumps 0.6.1 to
0.7.1. What arrives with them: footprint vias and via edits, via
tenting and the pad paste flag persisting, SchLib symbol shapes drawn
through the library so they show and query at once, region create and
delete on the geometry map, selective repour and the repour
preference, board save-copy, Altium 21 support, Unicode across the
bridge, library layer counts and origin reads, per-unit build stamps,
session time-slotting between Claude sessions sharing one Altium, loop
analysis, sheet fit, smart DRC and ngspice loop simulation.

**None of it has run against a live Altium from this fork.** The
suite passes and the Pascal guards pass; neither is the same claim.
Treat the first session on this build as verification, and see
[`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md).

`SCRIPT_VERSION` is `2026.10.19.1`, ahead of both sides, because the
merged script is neither of their builds and a session running either
one must see a mismatch rather than a false match. **Redeploy and
reload** before using it: `eda-agent update` reports the gap between
what is on disk and what Altium has compiled.

The merge commit records the decisions it needed, including three
places where taking one side verbatim would have broken something
quietly: the profile's pad corner radius, `Gen_BatchDelete`'s
variable name, and the `truncated` field `proj_get_nets` pages on.


### Added

- Library file tools, the gaps found comparing against
  embedded-society/altium-designer-mcp, written here rather than taken
  from it (it is GPL-3.0 and writes Altium's binary formats, which this
  project does not). None writes a .SchLib or .PcbLib:
  - `lib_extract_step_models` lists or extracts the STEP models embedded
    in a .PcbLib with Altium closed (zlib streams indexed by an ASCII
    table). **Checked on two vendor libraries written by Altium** (one
    model and eleven): every model came out as a STEP file.
  - `lib_write_libpkg` writes a .LibPkg grouping libraries, in the layout
    measured from 44 packages Altium saved. **Checked live on
    2026-10-20**: a written package opened in Altium and compiled to an
    IntLib.
  - `lib_backup_library`, `lib_list_library_backups` and
    `lib_restore_library_backup` keep byte copies of one library file,
    for libraries `app_checkpoint` does not reach. A restore backs up the
    file it replaces first and needs `confirm=True`.
  - `lib_render_footprint` draws a footprint to SVG and PNG from the live
    pad and primitive reads; `lib_compare_footprints` diffs two footprints
    pad by pad and counts primitives per layer. **Checked live on
    2026-10-20** against a vendor library: a WE-PMI footprint drew
    correctly, and 0603 H0.5 against H1 reported the height difference
    and nothing else, which is what those two parts differ by.
- `rotation_audit`, an advisory check in `pcb_layout_audit`: each
  two-pad top-side part is tried at 0/90/180/270 degrees about its
  origin, scored by net airline (minimum spanning tree) for routable
  nets and by distance to the nearest return pad for plane nets, and
  reported when another rotation beats the current one by 25 mils.
  The idea comes from coffeenmusic/altium-mcp's `check_orientation`,
  reimplemented on the LayoutBoard model. Bottom-side parts are skipped
  until the rotation sign of a mirrored part is checked. **Checked live
  on 2026-10-20** against a real board (BDDFS2MWC01A), where its
  suggestions were sensible. Still covered by synthetic-board tests for
  the scoring itself, and the TOP-SIDE path is all that has been
  exercised: a board with connected two-pad parts on the bottom has not
  been run.

#### Owner profiles

A per-workspace house style, so one machine can work for several
companies without re-stating their conventions on every call. See
[`docs/PROFILES.md`](docs/PROFILES.md).

- A profile directory carries naming conventions, design rules,
  fab/stackup profiles, footprint-authoring defaults, footprint-policy
  pins, schematic unit/grid settings, and project templates. Formats
  that already existed (`naming.json`, rulesets, fab profiles) are
  reused rather than reinvented.
- Found from `EDA_AGENT_PROFILE_DIR`, then a project-local
  `.eda-agent/profile/` discovered by walking up from the working
  directory, then the centrally active profile. **The project-local path
  is cwd-relative**, so it reaches the CLI but usually not the MCP
  server, whose client launches it from elsewhere; an MCP setup wants
  the central profile or the environment variable.
- CLI: `profile-init`, `profile-list`, `profile-use`, `profile-show`,
  `profile-validate`, `profile-new-project`.
- Tools: `profile_show`, `profile_list`, `eng_list_rulesets`,
  `eng_load_ruleset`, `eng_profile_fab_profile`,
  `profile_apply_document_settings`.
- `profile_new_document`: create a schematic or board from one of the
  profile's `templates/documents/` files and open it. Altium's own New
  Document makes a blank sheet and so does the bridge's
  `create_document`; there is no API that applies a template, so this
  copies the file, which is what applying one amounts to on disk. The
  template's own extension wins over the destination's, an existing
  file is refused unless `overwrite=True`, and a copy that Altium then
  declines to open still reports the path it wrote. Altium
  **preferences** stay out of reach (no scripting API at all) and
  **Draftsman** is unsupported, both stated plainly in
  [`docs/PROFILES.md`](docs/PROFILES.md) rather than left to be
  discovered.
- `lib_add_footprint_pad` / `lib_create_standard_footprint` /
  `lib_add_footprint_pads` take their pad corner radius from the active
  profile instead of a hardcoded 25%, and
  `lib_audit_footprint_policies` uses the profile's footprint policy
  when none is passed.

#### `eda-agent update`

Updating meant four manual steps and no way to tell which of them you
had missed. The checkout, the installed package, the deployed
DelphiScript files and the copy Altium has COMPILED each go stale
independently, and the symptom of all four is the same: a tool
behaving like an older build.

`update` reports all four and fixes the one it safely can, redeploying
the scripts when they differ from the tree. Two it deliberately does
not do:

- **Only your own remote is checked**, the one you push to. A checkout
  can have your fork, the project it was forked from and a colleague's
  copy all configured at once, and reporting a third party's commits
  every run turns the to-do list into something nobody reads. Name one
  with `--from <remote>` when you want it.
- **`--from <folder>` updates from a local copy**, with no git and no
  network in it: another checkout, a copy on a share, a release
  directory. It reads that folder's package version and
  `SCRIPT_VERSION`, compares both, and deploys its Altium scripts.
  Both a checkout layout and an unpacked package are accepted. It will
  not `pip install` for you, which is a bigger change than copying a
  script, so it prints the command.
- **It will not pull** unless asked. A command called "update"
  silently rebasing someone's uncommitted work is a bad trade, so
  being behind is reported and `--pull` opts in. A pull onto a dirty
  tree is refused outright rather than handing back a conflict.
- **It cannot reload Altium's script project.** There is no API for
  it, so that step is reported as an action for you rather than
  quietly omitted, which would leave you on an old build believing
  you were current.

The Altium check is a plain ping. `doctor`'s canary calls
`application.save_all`, which saves every open document; an update
that silently did that to read a version number would be a worse trade
than not reading it.

Exit codes: 0 current, 1 something needs you, 2 an error, so CI can
branch without parsing prose. `--check` changes nothing, `--fetch`
refreshes remote refs so "up to date" is not an answer from last
week's fetch, and `--json` gives the steps as data.

#### Cross-checking against pcb-toolkit

`calc-cross-check` / `pcb_calc_cross_check` runs a calculator through
this implementation and through a locally built
[pcb-toolkit](https://github.com/akiselev/pcb-toolkit), and reports
where they differ. Optional, and absent for most people, because
upstream's CLI is not published as a binary; the refusal when it is
missing says how to build it rather than just reporting a missing file.

Six calculators are mapped, with every flag read from pcb-toolkit's own
clap definitions rather than guessed, since a guessed flag name fails
at the moment a user runs it. Four are deliberately unmapped and carry
the reason, because "no mapping" and "I forgot" look identical
otherwise.

Fields pair BY NAME rather than through a translation this project has
not verified: a pair sharing a name and not a meaning reports as a
disagreement, which is the safe direction, and anything unpaired is
listed as not compared.

#### Fab capabilities, stackup templates and laminates

JLCPCB and PCBWay capability figures and stackup templates ship with
eda-agent, each carrying the page it was transcribed from and the date,
with a staleness flag after a year. CLI: `stackup-vendors`,
`stackup-show`, `stackup-profile`, `laminates`. Tools:
`design_list_fab_vendors`, `design_fab_vendor`,
`design_fab_vendor_profile`, `design_list_laminates`,
`design_compare_stackup`, plus `pcb_check_stackup` and
`pcb_apply_stackup_template` on Altium.

`design/fab_profile.py` ships zero capability numbers on purpose. This
is a deliberate relaxation of that policy, and three rules keep its
point intact:

- **An unpublished figure stays unpublished.** JLCPCB states no rigid
  hole-to-hole minimum and no mask sliver; both are null with a reason,
  and converting to a fab profile REFUSES and names them. Supply them
  with `--fill` once the fab has told you, and they are recorded in the
  profile's `source` as yours rather than the vendor's. Nothing guesses
  a design rule.
- **Nothing pretends to be current**, with the staleness check in the
  result rather than in a README.
- **A fab's figure for its own stack is not a material property.**
  JLCPCB's er of 4.6 is not carried to anyone else's FR-4.

Laminates carry BOTH dielectric constants. Manufacturers publish a
process value (one frequency, clamped stripline) and often a design
value (over a band); RO4350B is 3.48 and 3.66, about 5% apart, and the
design value is the one to size an impedance with. A table with a
single `er` silently picks one. The list is short on purpose: only
entries read off a datasheet are in it, rather than the 45 presets
pcb-toolkit extracted from the Saturn binary and labels as
compatibility data.

`pcb_apply_stackup_template` sets a board's ply table from a template,
dry run by default. It modifies existing layers only and refuses when
the layer counts differ, because adding one means inventing the name
Altium's Layer Stack Manager would have chosen on a board that has
routing on it. A failure part way through reports which layers did
change.

#### Ten calculators, on the CLI as well as the tool surface

`calc-spacing`, `calc-fusing`, `calc-via`, `calc-spiral`,
`calc-reactance`, `calc-resonance`, `calc-wavelength`, `calc-pdn`,
`calc-padstack` and `calc-wire`, each also an MCP tool
(`pcb_calc_conductor_spacing` and so on). The maths lives in
`engineering/pcb_calc.py` so it is testable without a tool surface, and
the argparse subcommands are generated from one table so a flag cannot
drift from the parameter it fills.

- **Every result states how much to trust it**, in a `status` field:
  `exact` for a definition, `published` for a named standard or
  textbook closed form, `estimate` for a rule of thumb. The convention
  is borrowed from pcb-toolkit, which had the right instinct: an
  algebraic identity and a rule of thumb must not arrive looking alike,
  or the rule of thumb ends up in a specification.
- **Conductor spacing is transcribed from secondary reproductions** of
  IPC-2221 Table 6-1, because the IPC document is not freely available,
  and two reproductions disagree on two cells. The larger value is
  taken wherever they conflict, since a wider spacing is never the
  unsafe mistake, and both figures are reported in `disputed` rather
  than the disagreement being settled silently.
- **Fusing current says when it has left its range.** Onderdonk is
  adiabatic; past a second or so a track sheds heat into the laminate
  and the answer falls below the track's own continuous IPC-2221
  rating, which is the formula failing rather than a result.
- Implemented in Python rather than by calling
  [pcb-toolkit](https://github.com/akiselev/pcb-toolkit) (Rust, MIT OR
  Apache-2.0), whose coverage is wider and upstream-validated, because
  its CLI is not published as a binary: using it would mean every user
  installs a Rust toolchain and builds from source.

#### Finding a replacement part

- `part_find_replacements`: judges candidate substitutes on
  parameters, lifecycle and stock. Every attribute carries a direction,
  because the direction is the whole safety argument: more voltage
  rating is safe, more ESR is not, a narrower temperature range is not,
  a different dielectric is not. Values parse into base SI with their
  unit, so `100 nF` and `0.1 uF` compare equal while `16 V` and `16 A`
  are refused rather than compared, and attribute names are matched
  across catalogues so Nexar's `voltagerating_dc` meets Digi-Key's
  `Voltage - Rated`.

  It does not say two parts are equivalent. It reports evidence per
  attribute and names what it did not check: **product-change notices
  and errata** (no distributor API serves them), **pad geometry** (the
  package name is compared, never the land pattern - that is
  `lib_audit_footprint_vs_datasheet`'s job), anything the catalogues do
  not publish, and any attribute the candidate does not state. An
  attribute with no rule is reported as unreviewed, never as a pass,
  and there is no default direction: defaulting to "equal" would block
  harmless differences and defaulting to "at least" would approve
  dangerous ones.

#### Fab output verification, and finding where time goes

- `pcb_check_fab_output`: reads an exported Gerber/Excellon package back
  and compares it against the live board - copper layer count vs the
  stackup, outline size, drill hits vs pads and vias, and minimum drill
  and feature size against fab limits you supply. eda-agent could write
  a fab package but never read one, so the artifact that actually gets
  manufactured was unverified. Needs the optional `[gerber]` extra
  (gerbonara, Apache-2.0, Python 3.12+). The outline check subtracts the
  stroke width, since a Gerber bounding box spans the ink and a 20 mm
  board drawn with a 0.2 mm pen measures 20.2 mm.
- `eda-agent perf`: reports where call time goes by joining the two logs
  already being written - `bridge_trace.log` (the round trip the caller
  saw) and `activity.log` (the handler time inside Altium) - on the
  request id. Every existing view read only the second, so the gap
  between them, which is IPC plus time queued behind another call, had
  never been visible.

#### Board artwork, offline library reads, part labels and datasheets

- `pcb_place_image`: raster artwork (silkscreen logo, QR code) as PCB
  region primitives. Decomposed into non-overlapping rectangles rather
  than traced outlines, so nothing is approximated away from the source
  art and holes need no special handling. Plans by default
  (`dry_run=True`) and reports the primitive count first, with a
  `max_regions` cap. Verified on a live board: an asymmetric test glyph
  placed at a known origin and read back with `pcb_query_region`
  confirmed the scale, the bottom-left origin corner, the layer, and
  that the art is neither mirrored nor flipped.
- `lib_read_pcblib_headers`: a `.PcbLib`'s footprint names, pad counts,
  heights and descriptions with Altium closed, for library hygiene in
  CI. Reads the ASCII streams only; pad geometry lives in the binary
  region and stays a live read through `lib_get_pad_geometry`.
- `part_parse_lcsc_label`: decodes the QR payload on an LCSC reel or
  bag. Only `pc`, `pm` and `qty` have a documented meaning, so the
  remaining keys are returned verbatim rather than given invented names.
- `part_cache_datasheet` / `part_list_cached_datasheets`: keep the PDF
  on disk with its URL, SHA-256 and fetch time. The host allowlist is
  deny-by-default from `EDA_AGENT_DATASHEET_HOSTS`, and the payload is
  verified to be a PDF by magic bytes rather than by `Content-Type`.

#### Altium Booster 3.2.0 feature integration

- IPC-7351B footprint validation against a live SchLib/PcbLib pair
  (`eng_validate_library_live`), plus a CSV/HTML report writer
  (`eng_write_library_validation_report`) and standalone IPC-name
  parsing (`eng_parse_ipc_footprint_name`).
- Pre-ECO schematic/PCB diff, both offline (`eng_eco_preview`) and fed
  from live Altium reads (`eng_eco_preview_live`).
- LTspice batch runs no longer flash a window on Windows.
- Nexar Supply search (`NexarProvider`) now returns pricing, stock,
  lifecycle status and similar parts, not just identity and datasheet.
- Altium 365 additions: `update_parameters`/`entity`/`workspace_infos`/
  `library_components_params` on the Nexar client, and three new write
  tools (`eng_a365_publish_model`, `eng_a365_release_component`,
  `eng_a365_update_parameters`), each off unless `confirm=True`.
  **UNVERIFIED against a real Nexar account**: covered by a test double
  only.
- Rotating, size-capped, secret-redacting activity log
  (`activity_log.py`), alongside the existing stderr logging.
- `Lib_GetFootprintPads` (DelphiScript) extended to also return track
  coordinates (for courtyard/silkscreen checks) and a 3D-body flag.
  Verified against a live Altium session.
- **Experimental, UNVERIFIED**: `eda-agent eco-execute`, a CLI command
  that auto-presses Altium's Engineering Change Order dialog via the
  existing reactive dialog driver. Off unless `--confirm`.

#### Documented gaps

A README section listing what the bundled data does NOT cover, because
a missing figure that is simply absent reads as an oversight rather
than as a decision: no PCNs or errata (no API serves them), no pad
geometry in a replacement check, JLCPCB's unpublished hole-to-hole and
mask sliver, PCBWay's absent stackup table, the IPC-2221 table's
secondary provenance and its two disputed cells, Altium preferences
and Draftsman, and PCB-side units and grid.

Also where the Altium scripts are deployed, and the trap behind it: if
the Global Project is registered somewhere other than the default,
`health` and `install-scripts` report a directory Altium never reads
and will say "deployed scripts current" while Altium compiles
something years old. `eda-agent update` catches it because it pings
for the version Altium actually COMPILED and compares the two, which
is how a stale 2026.10.13.5 was found running against a 2026.10.18.1
on disk.

### Fixed

- **`install-scripts`, `health` and `update` reported on a folder
  Altium might never read.** They all used
  `%USERPROFILE%\EDA Agent\scripts\`, so with the Global Project
  registered anywhere else they said "deployed scripts current" while
  Altium compiled a build days old. They now use the folder Altium's
  own Global Projects list (in the registry) loads `Altium_API.PrjScr`
  from, falling back to the old default only when no install
  registers it or several register different folders; `update` says
  which. `install-scripts` also stopped deriving the workspace pointer
  from the destination (`<dest>\..\workspace`), which was right only
  for the old default and would have pointed Altium at a directory the
  server never writes to.
- **`pcb_modify_layer` advertised its thicknesses as integers.** The
  Pascal fix below was unreachable from an MCP client that validates
  against the schema: 1.378 was refused or rounded to 1 before the call
  left it. Both are numbers now. Found by the live run of
  `RELEASE_VERIFICATION.md`'s copper-thickness step, which also
  confirmed the Pascal fix, `pcb_apply_stackup_template` and
  `pcb_check_stackup` on Altium 26.11.1.5.
- **A test for the merge's Pascal breaks.** Every routine in
  `scripts/altium/` must close each `Begin`/`Try`/`Case`/`Record` it
  opens and end on `End;`. Neither FPC nor the linter caught the four
  breaks the 0.7.1 merge introduced; this does, including both that
  reached a commit, and its self-tests delete real `End;` lines to
  prove it.
- **The IPC-2221 spacing table did not say which revision it was.**
  Investigating pcb-toolkit's interface surfaced that it targets
  IPC-2221**C**, whose classes are b1-b5 and a6-a8, while this carries
  the A/B table with B1-B4 and A5-A7. Revision C does not renumber
  those columns, it REDEFINES them: B4 becomes solder-mask covered, a
  new B5 appears, the A-series shifts and the above-500 V step
  changes. So a class letter does not carry across revisions, and
  reading "B4" here against a Rev C document silently selects a
  different environment. Every reply now names its revision and tells
  the reader to match on `environment_meaning` rather than the letter.
- **Setting a layer's copper thickness silently set it to ZERO.**
  `PCB_ModifyLayer` parsed `copper_thickness_mils` and
  `dielectric_height_mils` with `StrToIntDef`, and no real foil weight
  is a whole number of mils: 1 oz is 1.378, 0.5 oz is 0.689.
  `StrToIntDef('1.378', 0)` does not round, it returns the DEFAULT, so
  asking for 1 oz copper set the layer to zero and the tool reported
  success. The same applied to any fractional prepreg height. Both now
  parse as floats through the locale-safe helper and convert with
  `MilsToCoordF`, which keeps 1e-4 mil, and the read-back verification
  uses the same parse so a fractional value is no longer reported as
  rejected. **Redeploy the Altium scripts to pick this up**;
  SCRIPT_VERSION moved to 2026.10.18.1 and `eda-agent health` reports
  the deployed one.
- **Ten new calculators were invisible on the Altium backend.** They
  were added to `register_calc_tools`, which runs only for KiCad and
  EasyEDA because the names in it duplicate what `tools/pcb.py` already
  gives Altium. These ten have no Altium counterpart, so the opposite
  rule applies and they now register on every backend. Caught by the
  generated tool reference, not by any test of the tools themselves.
- **Four of the five distributor providers described data their parsers
  never read.** Digi-Key's, Mouser's and element14's class descriptions
  promised lifecycle or stock and none of the three requested any
  field; TME's promised stock that `Search.json` does not return at
  all. The three that can answer now do (lifecycle, stock and the full
  parametric map, in one shared key shape), TME's description says
  plainly that it cannot, and a guard test fails any description that
  gets ahead of its parser again. An absent figure is an absent key
  rather than a zero, because "no stock figure" and "none in stock"
  must not read alike.
- **Nexar's parametric map was built and then thrown away.** The query
  always asked for every `spec`, and the parser always assembled them,
  but only `lifecyclestatus` was published. The whole map is what a
  parametric comparison runs on, so dropping it made the richest source
  the least useful one.
- **Nexar's similar parts were flattened and truncated.** They were
  joined into `"<manufacturer> <mpn>"` strings, which cannot be split
  back apart when either half contains a space, and capped at five,
  which silently truncates the candidate set a replacement search runs
  on. Both halves are kept separate now, and all of them.
- The README claimed "~10 ms per call in active mode". That is the
  poll interval, not a measured round trip, and the Pascal side notes
  the timer floor is really about 16 ms. Both occurrences now say what
  the number is, and point at `eda-agent perf` for the measured one.
- Five CLI commands carried whole paragraphs in argparse's `help=`,
  which it reprints in the top-level command list, making the list
  unreadable. Moved to `epilog=`, so each subcommand's own `--help`
  still carries every word.
- Reading an owner profile no longer raises. It is read during server
  startup and on the pad-authoring path, where a malformed JSON file
  took the whole MCP server down and broke `lib_add_footprint_pad`, and
  a non-numeric corner radius was forwarded to Altium verbatim by one
  tool while raising `TypeError` in another. A present-but-unusable file
  now degrades to its default; `profile-validate` is where such problems
  are reported, and it applies the same checks the consuming tools do.
