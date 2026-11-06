# Optional Rust compute

eda-agent is pure Python. An optional wheel, **eda-agent-compute**
(`rust/eda_agent_compute`), carries compiled ports of the loops that were
measured to be CPU-bound. You turn it on with a flag. Without the flag,
nothing changes.

**Rust is never required.** `pip install -e .` and the eda-agent wheel are
pure Python (`py3-none-any`). They do not build, fetch or import anything
Rust. Without the compute wheel, the default and `auto` run the Python
code, and only an explicit `rust` refuses to start. This was checked in a
clean virtual environment with no Rust toolchain on `PATH`: an editable
install, a wheel install, `compute-status` in every mode, and a full
routing run with `auto`.

```
eda-agent --compute rust     # always Rust; refuses to start without the wheel
eda-agent --compute auto     # Rust when a matching wheel is installed, else Python
eda-agent --compute python   # the default
eda-agent compute-status     # what is installed, what is requested, what runs
```

`EDA_AGENT_COMPUTE=python|rust|auto` does the same thing. Use it for MCP
clients that start the server with no arguments. The setting is read on
every call, so a child process inherits it.

## What runs in Rust today

| Part | Why | Measured (1090-pad board from `tests/route/test_scale.py`) |
|---|---|---|
| Grid A* of the routing planner (`route/router.py`, `_astar`) | Pure-Python heap search, 150 M dict lookups per run | 25 mil grid: 32.5 s → 2.2 s. 10 mil grid: 184 s → 11 s |

Both columns time the whole routing call (`router.route_geometry`), including problem setup,
copper emission and the solution check. The Rust output is **byte-identical**
to the Python output on that board at both pitches. The numbers come from a
4-core Linux cloud machine; expect the same ratio on Windows.

The same change also made the Python path faster. The solution check
(`validate_solution`) compared every track against every obstacle with the
exact distance. It now rejects a pair first by the gap between their
bounding boxes. That gap is never larger than the true distance, so the
result is unchanged; on the 10 mil run the Python path went from 184 s to
160 s.

## What is deliberately not in Rust

Measured, not guessed:

- **DRC** (`layout/drc.py`): 13k objects in 0.45 s, 95k in 3.9 s.
- **SchDoc parsing**: 200k records in 2.1 s. The PcbLib reader only reads
  the table of contents.
- **The layout router** (`layout/route/`): its heavy work is already in
  scipy, which is compiled C.
- **The test suite**: 11.7 min serially, with no single hot spot. Use
  `pytest -n auto` (pytest-xdist is already a dev dependency).
- **Anything that talks to Altium.** Each call is a file round trip to a
  DelphiScript loop that polls every 10-30 ms and runs interpreted script.
  Rust on the Python side cannot make Altium answer sooner.

The next candidate is the annealing placer (`design/pcb_placement.py`). It
re-scores the whole board on every move. The first fix there is to score
only what the move changed, in Python. Port it to Rust only if that is not
enough.

## Same answer, enforced

`tests/route/test_compute_parity.py` runs both implementations and requires
the following to be equal:

- each search call: the path, the "ran out of budget" verdict and the
  expansion count, over five option sets (diagonals, via-in-pad, zero bend
  penalty, a budget small enough to run out);
- each whole routing run, byte for byte, including the 1090-pad board;
- the flat obstacle map the kernel reads, compared cell by cell against the
  Python maps, and the incrementally updated map compared against one built
  afresh after a full run.

The comparison was checked against two deliberate bugs. Reversing the
heap's tie-break failed 31 tests. Ignoring the via-in-pad rule failed 20.
A further test shows that changing one knob on the Rust side is noticed.

The Rust kernel is a line-for-line port. The same arithmetic in the same
order, the same tie-breaks (`(f, g, insertion counter)`) and the same
direction order are what make identical results possible. When you change
`_astar`, change `rust/eda_agent_compute/src/lib.rs` with it. If you change
the contract, bump `API_VERSION` in both `lib.rs` and
`eda_agent/compute.py` (`EXPECTED_API`). A wheel with a different API
version counts as not installed: `auto` falls back to Python and `rust`
refuses to run. A stale wheel left behind by an update is therefore never
used with the wrong semantics.

## How the pieces fit

- `eda_agent/compute.py`: reads the mode, loads the wheel once, checks its
  API version and reports status. `native()` returns the module or None.
- `RoutingProblem.dense_grid()` (`route/model.py`): flat `int32` copies of
  the `blocked` / `via_blocked` maps and a `pad_any` byte map, built on
  first use. From then on, each rasterizer write updates them as well.
  Each cell holds `-1` (free), a net id (only that net may pass), or `-2`
  (unnetted copper, or two different nets: nobody passes). That is exactly
  `all(o == net for o in owners)`. A direct write into the dicts changes
  the key count, and that triggers a rebuild.
- `router._astar_native`: hands the flat maps and the call's arguments to
  the kernel. It reads them through the buffer protocol, so there is no
  numpy dependency, and releases the GIL while it searches.
- Every routing result says which implementation produced it, in
  `summary.compute`.

## Installing the wheel

**From CI (no Rust needed).** The `rust-compute` job of the tests workflow
builds a Windows wheel for every PR and push. Download the
`eda-agent-compute-wheel` artifact from the run, then:

```
python -m pip install eda_agent_compute-0.1.0-cp311-abi3-win_amd64.whl
eda-agent --compute rust compute-status
```

The wheel is `abi3`: one file serves every CPython from 3.11 up.

**From source.** You need Rust (`rustup`, MSVC toolchain) and the Visual
Studio Build Tools (C++ workload):

```
python -m pip install ./rust/eda_agent_compute
```

`eda-agent update` reinstalls eda-agent from the clone; it does not rebuild
this wheel. After an update that changes the kernel, `compute-status`
reports an API mismatch until you reinstall the wheel. `--compute auto`
keeps working on Python in the meantime.

## Licence

The crate is Apache-2.0 like the rest of eda-agent. Its only dependency is
PyO3 (Apache-2.0 OR MIT), and it builds with maturin (Apache-2.0 OR MIT).
No GPL code is involved.
