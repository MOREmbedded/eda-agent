// SPDX-License-Identifier: Apache-2.0
//! Compiled kernels for eda-agent.
//!
//! One kernel so far: the grid A* of `eda_agent.route.router._astar`. It is
//! a line-for-line port, not a reinterpretation. The Python function stays
//! the reference implementation, and the tests in eda-agent require both to
//! return the same path, the same budget verdict and the same expansion
//! count on the same problem. That only holds if every arithmetic step and
//! every tie-break is the same, so the comments below point at the Python
//! line each piece mirrors.
//!
//! Obstacle encoding (built by `RoutingProblem.dense_grid()`), one i32 per
//! (layer, ix, iy):
//!   -1      no owner: passable for every net
//!   k >= 0  owned by net id k only: passable for net k
//!   -2      owned by None or by two different nets: passable for nobody
//! which is exactly `all(o == net for o in owners)` with an empty set
//! meaning "free".

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
use std::hash::{BuildHasherDefault, Hasher};

/// Bumped whenever the call signature or the semantics change. The Python
/// side checks it before trusting a build.
const API_VERSION: u32 = 1;

/// `(path | None, budget_exhausted, expansions)`, as `_astar` returns it.
type Cell = (i64, i64, i64);
type SearchResult = (Option<Vec<Cell>>, bool, i64);

const EPS: f64 = 1e-6; // router._EPS
const DIR_NONE: usize = 8; // router._DIR_NONE
                           // router._DIRS followed by router._DIAGONAL_DIRS, in that order: the index
                           // is the arrival direction the bend penalty compares.
const DIRS: [(i64, i64); 8] = [
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
];

/// FxHash: the states are dense small integers, and SipHash's DoS
/// resistance buys nothing here.
#[derive(Default)]
struct Fx(u64);
impl Hasher for Fx {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        for b in bytes {
            self.write_u64(*b as u64);
        }
    }
    fn write_u64(&mut self, i: u64) {
        self.0 = (self.0.rotate_left(5) ^ i).wrapping_mul(0x51_7c_c1_b7_27_22_0a_95);
    }
}
type FxMap<K, V> = HashMap<K, V, BuildHasherDefault<Fx>>;

/// Heap entry ordered like Python's tuple `(f, g, counter, state)` in a
/// min-heap. `counter` is unique, so `state` never decides.
struct Entry {
    f: f64,
    g: f64,
    counter: u64,
    state: u64,
}
impl PartialEq for Entry {
    fn eq(&self, o: &Self) -> bool {
        self.cmp(o) == Ordering::Equal
    }
}
impl Eq for Entry {}
impl PartialOrd for Entry {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Entry {
    fn cmp(&self, o: &Self) -> Ordering {
        // Reversed: BinaryHeap is a max-heap.
        o.f.total_cmp(&self.f)
            .then_with(|| o.g.total_cmp(&self.g))
            .then_with(|| o.counter.cmp(&self.counter))
    }
}

struct Grid {
    nx: i64,
    ny: i64,
    layers: usize,
    blocked: Vec<i32>,
    via_blocked: Vec<i32>,
    pad_any: Vec<u8>,
    net: i32,
}

impl Grid {
    #[inline]
    fn idx(&self, li: usize, ix: i64, iy: i64) -> usize {
        (li as i64 * self.nx * self.ny + ix * self.ny + iy) as usize
    }
    #[inline]
    fn inside(&self, ix: i64, iy: i64) -> bool {
        0 <= ix && ix < self.nx && 0 <= iy && iy < self.ny
    }
    #[inline]
    fn owner_ok(&self, v: i32) -> bool {
        v == -1 || v == self.net
    }
    /// RoutingProblem.passable
    #[inline]
    fn passable(&self, li: usize, ix: i64, iy: i64) -> bool {
        self.inside(ix, iy) && self.owner_ok(self.blocked[self.idx(li, ix, iy)])
    }
    /// RoutingProblem.via_ok
    fn via_ok(&self, ix: i64, iy: i64, allow_in_pad: bool) -> bool {
        if !self.inside(ix, iy) {
            return false;
        }
        if !allow_in_pad && self.pad_any[(ix * self.ny + iy) as usize] != 0 {
            return false;
        }
        for li in 0..self.layers {
            let k = self.idx(li, ix, iy);
            if !self.owner_ok(self.blocked[k]) || !self.owner_ok(self.via_blocked[k]) {
                return false;
            }
        }
        true
    }
}

fn read_i32(py: Python<'_>, buf: &PyBuffer<i32>, want: usize, what: &str) -> PyResult<Vec<i32>> {
    if buf.item_count() != want {
        return Err(PyValueError::new_err(format!(
            "{what}: {} items, expected {want}",
            buf.item_count()
        )));
    }
    buf.to_vec(py)
}

/// Port of `router._astar`. Returns `(path | None, budget_exhausted,
/// expansions)`, the path as `(layer, ix, iy)` cells with consecutive
/// duplicates collapsed, exactly as `_reconstruct` returns it.
#[pyfunction]
#[pyo3(signature = (nx, ny, layers, blocked, via_blocked, pad_any, net,
                    starts, targets, allow_diagonal, bend_penalty, via_cost,
                    allow_via_in_pad, max_expansions))]
#[allow(clippy::too_many_arguments)]
fn astar(
    py: Python<'_>,
    nx: i64,
    ny: i64,
    layers: usize,
    blocked: PyBuffer<i32>,
    via_blocked: PyBuffer<i32>,
    pad_any: PyBuffer<u8>,
    net: i32,
    starts: Vec<(i64, i64, i64)>,
    targets: Vec<(i64, i64, i64)>,
    allow_diagonal: bool,
    bend_penalty: f64,
    via_cost: f64,
    allow_via_in_pad: bool,
    max_expansions: i64,
) -> PyResult<SearchResult> {
    if nx <= 0 || ny <= 0 || layers == 0 {
        return Err(PyValueError::new_err("empty grid"));
    }
    let cells = (nx * ny) as usize;
    let grid = Grid {
        nx,
        ny,
        layers,
        blocked: read_i32(py, &blocked, cells * layers, "blocked")?,
        via_blocked: read_i32(py, &via_blocked, cells * layers, "via_blocked")?,
        pad_any: {
            if pad_any.item_count() != cells {
                return Err(PyValueError::new_err("pad_any: wrong size"));
            }
            pad_any.to_vec(py)?
        },
        net,
    };
    for &(li, _, _) in starts.iter().chain(targets.iter()) {
        if li < 0 || li as usize >= layers {
            return Err(PyValueError::new_err(format!("layer {li} out of range")));
        }
    }
    Ok(py.allow_threads(|| {
        search(
            &grid,
            &starts,
            &targets,
            allow_diagonal,
            bend_penalty,
            via_cost,
            allow_via_in_pad,
            max_expansions,
        )
    }))
}

#[allow(clippy::too_many_arguments)]
fn search(
    grid: &Grid,
    starts: &[(i64, i64, i64)],
    targets: &[(i64, i64, i64)],
    allow_diagonal: bool,
    bend_penalty: f64,
    via_cost: f64,
    allow_via_in_pad: bool,
    max_expansions: i64,
) -> SearchResult {
    if targets.is_empty() {
        return (None, false, 0);
    }
    let (nx, ny) = (grid.nx, grid.ny);
    // Target membership. A target outside the grid can never be popped
    // (passable() refuses it), so leaving it out changes nothing.
    let mut is_target = vec![false; grid.layers * (nx * ny) as usize];
    for &(li, ix, iy) in targets {
        if grid.inside(ix, iy) {
            is_target[grid.idx(li as usize, ix, iy)] = true;
        }
    }
    let bx1 = targets.iter().map(|t| t.1).min().unwrap();
    let bx2 = targets.iter().map(|t| t.1).max().unwrap();
    let by1 = targets.iter().map(|t| t.2).min().unwrap();
    let by2 = targets.iter().map(|t| t.2).max().unwrap();
    let sqrt2 = 2.0f64.sqrt(); // math.sqrt(2.0)
    let h = |ix: i64, iy: i64| -> f64 {
        let dx = 0.max(bx1 - ix).max(ix - bx2);
        let dy = 0.max(by1 - iy).max(iy - by2);
        if allow_diagonal {
            dx.max(dy) as f64 + (sqrt2 - 1.0) * dx.min(dy) as f64
        } else {
            (dx + dy) as f64
        }
    };
    let enc = |li: usize, ix: i64, iy: i64, d: usize| -> u64 {
        (((li as i64 * nx + ix) * ny + iy) as u64) * 9 + d as u64
    };
    let dec = |s: u64| -> (usize, i64, i64, usize) {
        let d = (s % 9) as usize;
        let c = (s / 9) as i64;
        let iy = c % ny;
        let ix = (c / ny) % nx;
        let li = (c / ny / nx) as usize;
        (li, ix, iy, d)
    };

    let mut counter: u64 = 0;
    let mut heap: BinaryHeap<Entry> = BinaryHeap::new();
    let mut best: FxMap<u64, f64> = FxMap::default();
    let mut parent: FxMap<u64, u64> = FxMap::default();
    const ROOT: u64 = u64::MAX;

    // for (li, ix, iy) in sorted(set(starts)): the caller sorts and dedups.
    for &(li, ix, iy) in starts {
        let li = li as usize;
        if !grid.passable(li, ix, iy) {
            continue;
        }
        let st = enc(li, ix, iy, DIR_NONE);
        best.insert(st, 0.0);
        parent.insert(st, ROOT);
        heap.push(Entry {
            f: h(ix, iy),
            g: 0.0,
            counter,
            state: st,
        });
        counter += 1;
    }

    let ndirs = if allow_diagonal { 8 } else { 4 };
    let mut expansions: i64 = 0;
    while expansions < max_expansions {
        let Some(Entry { g, state: st, .. }) = heap.pop() else {
            break;
        };
        if g > best.get(&st).copied().unwrap_or(f64::INFINITY) + EPS {
            continue;
        }
        let (li, ix, iy, d) = dec(st);
        if is_target[grid.idx(li, ix, iy)] {
            return (Some(reconstruct(&parent, st, dec)), false, expansions);
        }
        expansions += 1;

        for (di, &(dx, dy)) in DIRS.iter().enumerate().take(ndirs) {
            let (jx, jy) = (ix + dx, iy + dy);
            if !grid.passable(li, jx, jy) {
                continue;
            }
            if dx != 0
                && dy != 0
                && (!grid.passable(li, ix + dx, iy) || !grid.passable(li, ix, iy + dy))
            {
                continue;
            }
            let mut ng = g + if dx != 0 && dy != 0 { sqrt2 } else { 1.0 };
            if d != DIR_NONE && d != di {
                ng += bend_penalty;
            }
            let nst = enc(li, jx, jy, di);
            if ng < best.get(&nst).copied().unwrap_or(f64::INFINITY) - EPS {
                best.insert(nst, ng);
                parent.insert(nst, st);
                heap.push(Entry {
                    f: ng + h(jx, jy),
                    g: ng,
                    counter,
                    state: nst,
                });
                counter += 1;
            }
        }

        if grid.layers > 1 && grid.via_ok(ix, iy, allow_via_in_pad) {
            for l2 in 0..grid.layers {
                if l2 == li {
                    continue;
                }
                let ng = g + via_cost;
                let nst = enc(l2, ix, iy, DIR_NONE);
                if ng < best.get(&nst).copied().unwrap_or(f64::INFINITY) - EPS {
                    best.insert(nst, ng);
                    parent.insert(nst, st);
                    heap.push(Entry {
                        f: ng + h(ix, iy),
                        g: ng,
                        counter,
                        state: nst,
                    });
                    counter += 1;
                }
            }
        }
    }
    (
        None,
        !heap.is_empty() && expansions >= max_expansions,
        expansions,
    )
}

/// router._reconstruct
fn reconstruct(
    parent: &FxMap<u64, u64>,
    last: u64,
    dec: impl Fn(u64) -> (usize, i64, i64, usize),
) -> Vec<(i64, i64, i64)> {
    let mut cells: Vec<(i64, i64, i64)> = Vec::new();
    let mut st = last;
    loop {
        let (li, ix, iy, _) = dec(st);
        cells.push((li as i64, ix, iy));
        st = parent[&st];
        if st == u64::MAX {
            break;
        }
    }
    cells.reverse();
    cells.dedup();
    cells
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("API_VERSION", API_VERSION)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(astar, m)?)?;
    Ok(())
}
