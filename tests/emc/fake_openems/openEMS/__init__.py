# SPDX-License-Identifier: Apache-2.0
"""A stand-in for openEMS: answers like an ideal 2-port line.

S11 = 0.1 on the driven port; the other port sees 0.9 with a delay of
1 ns. Records every call in CSXCAD.CALLS and writes them to
``calls.json`` beside the run on ``Run``. Used by tests only."""

import json
import os

import numpy as np

from CSXCAD import CALLS


class _Port:
    def __init__(self, number, R, start, stop, excite):
        self.number, self.R, self.excite = number, R, excite
        self.start, self.stop = list(start), list(stop)

    def CalcPort(self, sim_path, freq, ref_impedance=None):
        freq = np.asarray(freq)
        if self.excite:
            self.uf_inc = np.ones(len(freq), dtype=complex)
            self.uf_ref = 0.1 * np.ones(len(freq), dtype=complex)
        else:
            self.uf_inc = np.zeros(len(freq), dtype=complex)
            self.uf_ref = 0.9 * np.exp(-2j * np.pi * freq * 1e-9)
        self.P_acc = 0.5 * np.ones(len(freq))


class _NF2FF:
    def CalcNF2FF(self, sim_path, f, theta, phi, center=None):
        class R:
            pass
        r = R()
        r.Dmax = np.array([2.0])
        r.Prad = np.array([0.4])
        e = np.ones((len(theta), len(phi)))
        e[0, 0] = 0.5
        r.E_norm = [e]
        return r


class openEMS:
    def __init__(self, **kw):
        CALLS.append(["fdtd", kw])
        self.ports = []

    def SetGaussExcite(self, f0, fc):
        CALLS.append(["gauss", f0, fc])

    def SetBoundaryCond(self, bc):
        CALLS.append(["boundary", list(bc)])

    def SetCSX(self, csx):
        self.csx = csx

    def AddLumpedPort(self, number, R, start, stop, p_dir, excite=0, priority=0):
        CALLS.append(["port", number, R, list(start), list(stop), p_dir, excite, priority])
        p = _Port(number, R, start, stop, excite)
        self.ports.append(p)
        return p

    def CreateNF2FFBox(self):
        CALLS.append(["nf2ff"])
        return _NF2FF()

    def Run(self, sim_path, cleanup=False, verbose=0, numThreads=0):
        os.makedirs(sim_path, exist_ok=True)
        for p in self.ports:  # probe files that have died down 60 dB
            with open(os.path.join(sim_path, f"port_ut_{p.number}"), "w") as fh:
                fh.write("% time voltage\n0 0\n1 1\n" + "".join(
                    f"{k} 1e-3\n" for k in range(2, 30)))
        lines = self.csx.GetGrid().lines
        with open(os.path.join(os.path.dirname(sim_path), "calls.json"), "w") as fh:
            json.dump({"calls": CALLS, "lines": lines}, fh)
