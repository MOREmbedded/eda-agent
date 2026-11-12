# SPDX-License-Identifier: Apache-2.0
"""A stand-in for CSXCAD that records what the runner asks of it.

Only the calls the eda-agent runner makes exist here; their signatures are
the ones openEMS's Python bindings document. Used by tests only."""

CALLS = []


class _Prop:
    def __init__(self, kind, name, kw):
        self.kind, self.name, self.kw = kind, name, kw

    def AddBox(self, start, stop, priority=0):
        CALLS.append(["box", self.name, list(start), list(stop), priority])

    def AddPolygon(self, points, norm_dir, elevation, priority=0):
        CALLS.append(["polygon", self.name, len(points[0]), norm_dir, float(elevation), priority])

    def AddCylinder(self, start, stop, radius, priority=0):
        CALLS.append(["cylinder", self.name, list(start), list(stop), radius, priority])


class _Grid:
    def __init__(self):
        self.lines = {"x": [], "y": [], "z": []}

    def SetDeltaUnit(self, unit):
        CALLS.append(["unit", unit])

    def AddLine(self, d, values):
        self.lines[d] += [float(v) for v in values]

    def SmoothMeshLines(self, d, res, ratio=1.5):
        CALLS.append(["smooth", d, res, ratio])


class ContinuousStructure:
    def __init__(self):
        self.grid = _Grid()

    def GetGrid(self):
        return self.grid

    def AddMaterial(self, name, **kw):
        CALLS.append(["material", name, kw])
        return _Prop("material", name, kw)

    def AddMetal(self, name):
        CALLS.append(["metal", name])
        return _Prop("metal", name, {})
