# SPDX-License-Identifier: Apache-2.0
"""The SPICE model tools end to end, with a fake bridge and a fake LTspice."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from eda_agent.engineering import ltspice
from eda_agent.engineering.config import LTspicePolicy
from eda_agent.engineering.errors import NotFound, ValidationFailed
from eda_agent.tools import engineering as eng_module
from eda_agent.tools import spice_models as sm
from eda_agent.tools.registry import ToolRegistry

LIB = """\
* LT3080 adjustable LDO
.SUBCKT LT3080 IN OUT SET GND
R1 IN OUT 1k
.ENDS
.model BC847B NPN(BF=300)
"""

READINESS = {
    "ready": [{"designator": "U1", "spice_prefix": "X", "sim_model": "LT3080",
               "value": "LT3080EDD"},
              {"designator": "R1", "spice_prefix": "R", "value": "100k"},
              {"designator": "V1", "spice_prefix": "V", "value": "5"}],
    "no_sim_model": [], "needs_primitive": [],
    "needs_file": [{"designator": "U2", "comment": "LDO", "lib_ref": "LDO_DFN",
                    "manufacturer_part": "LT3080EDD#TRPBF"}],
}
PINS = {
    "U1": [{"pin_number": "1", "pin_name": "GND", "net": "GND"},
           {"pin_number": "2", "pin_name": "OUT", "net": "VOUT"},
           {"pin_number": "3", "pin_name": "IN", "net": "VIN"},
           {"pin_number": "4", "pin_name": "SET", "net": "NSET"}],
    "U2": [{"pin_number": "1", "pin_name": "GND", "net": "GND"},
           {"pin_number": "2", "pin_name": "OUT", "net": "VOUT"},
           {"pin_number": "3", "pin_name": "IN", "net": "VIN"},
           {"pin_number": "4", "pin_name": "SET", "net": "NSET"}],
    "R1": [{"pin_number": "1", "net": "NSET"}, {"pin_number": "2", "net": "GND"}],
    "V1": [{"pin_number": "1", "net": "VIN"}, {"pin_number": "2", "net": "GND"}],
}


class _Bridge:
    def __init__(self, model_file=""):
        self.calls = []
        self.model_file = model_file

    async def send_command_async(self, command, params=None, timeout=None):
        params = params or {}
        self.calls.append((command, params))
        if command == "generic.get_simulation_readiness":
            return READINESS
        if command == "project.get_connectivity_batch":
            want = params["designators"].split("~~")
            return {"components": [{"designator": d, "pins": PINS[d]} for d in want
                                   if d in PINS]}
        if command == "generic.get_component_models":
            return {"components": [{"designator": "U1", "models": [
                {"model_name": "LT3080", "model_type": "SIM",
                 "datafile_links": [{"entity_name": "LT3080", "file_kind": "SimModel",
                                     "location": self.model_file}]}]}]}
        if command == "generic.attach_spice_model":
            return {"success": True, **params}
        raise AssertionError(f"unexpected command {command}")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / ".eda-agent").mkdir(parents=True)
    (root / ".eda-agent" / "workspace.json").write_text("{}")
    (root / "models").mkdir()
    (root / "models" / "vendor.lib").write_text(LIB)
    monkeypatch.chdir(root)
    monkeypatch.delenv("EDA_AGENT_SPICE_MODEL_DIRS", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nolocal"))
    return root


@pytest.fixture
def tools(ws, monkeypatch):
    bridge = _Bridge(model_file=str(ws / "models" / "vendor.lib"))
    import eda_agent.bridge as bridge_pkg
    monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: bridge, raising=False)
    registry = ToolRegistry()
    sm.register_spice_model_tools(registry)
    eng_module.register_engineering_tools(registry, altium=True)
    return (lambda name: registry.get(name).fn), bridge


def _run(coro):
    return asyncio.run(coro)


def test_scan_writes_the_index_into_the_workspace(tools, ws):
    get, _ = tools
    r = _run(get("sim_scan_models")())
    assert r["ok"] and r["models"] == 2
    assert Path(r["index_file"]) == ws / ".eda-agent" / "spice-models" / "index.json"
    assert json.loads(Path(r["index_file"]).read_text())["version"] == 1


def test_scan_with_no_folder_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "x"))
    monkeypatch.delenv("EDA_AGENT_SPICE_MODEL_DIRS", raising=False)
    r = sm.scan_models(folders=[str(tmp_path / "missing")], workspace=tmp_path)
    assert r["index_file"] == "" and "hint" in r
    assert not (tmp_path / ".eda-agent").exists()


def test_find_needs_an_index_then_finds_by_mpn(tools):
    get, _ = tools
    assert _run(get("sim_find_models")("LT3080"))["ok"] is False
    _run(get("sim_scan_models")())
    r = _run(get("sim_find_models")("LT3080EDD#TRPBF"))
    assert r["matches"][0]["name"] == "LT3080"


def test_find_schematic_models_uses_the_mpn(tools):
    get, _ = tools
    _run(get("sim_scan_models")())
    r = _run(get("sim_find_schematic_models")())
    assert r["parts"][0]["designator"] == "U2"
    assert r["parts"][0]["candidates"][0]["name"] == "LT3080"


def test_pin_map_and_attach(tools, ws):
    get, bridge = tools
    _run(get("sim_scan_models")())
    plan = _run(get("sim_get_pin_map")("U2", "LT3080"))
    assert plan["ok"] and [n["pins"] for n in plan["mapping"]["nodes"]] == \
        [["3"], ["2"], ["4"], ["1"]]
    r = _run(get("sim_attach_library_model")("U2", "LT3080"))
    assert r["attached"] and r["file"] == str(ws / "models" / "vendor.lib")
    sent = [p for c, p in bridge.calls if c == "generic.attach_spice_model"]
    assert sent == [{"designator": "U2", "file_path": str(ws / "models" / "vendor.lib"),
                     "model_name": "LT3080", "primitive": "X"}]


def test_attach_refuses_an_unmatched_pin_map(tools, ws):
    get, bridge = tools
    (ws / "models" / "odd.lib").write_text(".subckt ODD A B C D\n.ends\n")
    _run(get("sim_scan_models")())
    r = _run(get("sim_attach_library_model")("U2", "ODD"))
    assert r["attached"] is False
    assert not [c for c, _ in bridge.calls if c == "generic.attach_spice_model"]


def test_attach_needs_allow_review_for_pin_number_order(tools, ws):
    get, bridge = tools
    (ws / "models" / "num.lib").write_text(".subckt NUM 1 2 3 4\n.ends\n")
    _run(get("sim_scan_models")())
    r = _run(get("sim_attach_library_model")("U2", "NUM"))
    assert r["attached"] is False and "allow_review" in r["reason"]
    r2 = _run(get("sim_attach_library_model")("U2", "NUM", allow_review=True))
    assert r2["attached"] is True


def test_saved_pin_map_is_checked_against_the_model(tools, ws):
    get, _ = tools
    (ws / "models" / "num.lib").write_text(".subckt NUM 1 2 3 4\n.ends\n")
    _run(get("sim_scan_models")())
    bad = _run(get("sim_set_pin_map")("NUM", ["1", "2"]))
    assert bad["ok"] is False
    ok = _run(get("sim_set_pin_map")("NUM", ["3", "2", "4", "1"]))
    assert ok["ok"] and Path(ok["file"]).parent == ws / ".eda-agent" / "spice-models"
    plan = _run(get("sim_get_pin_map")("U2", "NUM"))
    assert plan["mapping"]["method"] == "override" and plan["ok"]


def test_deck_from_schematic_uses_the_linked_model(tools, ws):
    get, _ = tools
    _run(get("sim_scan_models")())
    r = _run(get("eng_ltspice_deck_from_schematic")(analysis="op",
                                                     on_missing_model="placeholder"))
    assert r["ok"], r
    assert "XU1 VIN VOUT NSET 0 LT3080" in r["deck"]
    assert '.include "vendor.lib"' in r["deck"]
    assert r["model_files"] == [{"path": str(ws / "models" / "vendor.lib"),
                                 "include_as": "vendor.lib"}]


def test_deck_from_schematic_without_an_index_says_so(tools, ws):
    get, _ = tools
    r = _run(get("eng_ltspice_deck_from_schematic")(analysis="op",
                                                     on_missing_model="placeholder"))
    assert any("sim_scan_models" in n.get("note", "") for n in r.get("model_notes", []))


def test_export_writes_deck_and_models(tools, ws, tmp_path):
    get, _ = tools
    out = tmp_path / "export"
    deck = '* t\n.include "vendor.lib"\nV1 a 0 1\n.op\n.end\n'
    files = [{"path": str(ws / "models" / "vendor.lib"), "include_as": "vendor.lib"}]
    r = _run(get("eng_ltspice_export")(str(out), deck, files, name="ldo"))
    assert r["ok"] and (out / "ldo.cir").read_text() == deck
    assert (out / "vendor.lib").read_text() == LIB
    again = _run(get("eng_ltspice_export")(str(out), deck, files, name="ldo"))
    assert again["ok"] is False and "overwrite" in again["reason"]


def test_export_refuses_unsafe_decks_and_names(tools, ws, tmp_path):
    get, _ = tools
    out = tmp_path / "e2"
    r = _run(get("eng_ltspice_export")(str(out), ".ferret http://x/y\n.end\n"))
    assert r["ok"] is False
    r = _run(get("eng_ltspice_export")(str(out), ".op\n.end\n",
                                       [{"path": str(ws / "models" / "vendor.lib"),
                                         "include_as": "..\\evil.lib"}]))
    assert r["ok"] is False
    assert not (tmp_path / "evil.lib").exists()


# ---------------------------------------------------------------- the runner
class _Proc:
    def __init__(self, on_wait):
        self._on_wait = on_wait

    def wait(self, timeout=None):
        self._on_wait()
        return 0

    def kill(self):
        pass


def _runner(tmp_path, popen):
    return ltspice.LTspiceRunner(LTspicePolicy("LTspice.exe", tmp_path / "runs"), popen=popen)


def test_run_copies_model_files_beside_the_deck(tmp_path):
    model = tmp_path / "m.lib"
    model.write_text(LIB)
    seen = {}

    def popen(argv, cwd, **kw):
        seen["files"] = sorted(p.name for p in Path(cwd).iterdir())
        return _Proc(lambda: None)

    r = _runner(tmp_path, popen).run(
        "p", netlist_text='.include "vendor.lib"\n.op\n.end\n',
        model_files=[{"path": str(model), "include_as": "vendor.lib"}])
    assert seen["files"] == ["deck.cir", "vendor.lib"]
    assert r["model_files_copied"] == ["vendor.lib"]


def test_copy_refuses_missing_and_non_model_files(tmp_path):
    with pytest.raises(NotFound):
        ltspice.copy_model_files([{"path": str(tmp_path / "nope.lib")}], tmp_path)
    exe = tmp_path / "x.exe"
    exe.write_text("")
    with pytest.raises(ValidationFailed):
        ltspice.copy_model_files([{"path": str(exe)}], tmp_path)
    with pytest.raises(ValidationFailed):
        ltspice.copy_model_files([{"path": str(exe), "include_as": "a/b.lib"}], tmp_path)


def test_netlist_from_schematic_runs_ltspice_in_a_sandbox(tmp_path):
    src = tmp_path / "design"
    src.mkdir()
    (src / "ldo.asc").write_text("Version 4\n")
    (src / "my.asy").write_text("Version 4\n")
    seen = {}

    def popen(argv, cwd, **kw):
        seen["argv"] = argv

        def write():
            (Path(cwd) / "ldo.net").write_text("R1 a 0 1k\n.end\n")
        return _Proc(write)

    r = _runner(tmp_path, popen).netlist_from_schematic("compare", src / "ldo.asc")
    assert seen["argv"][1] == "-netlist"
    assert r["text"].startswith("R1 a 0 1k")
    assert not (src / "ldo.net").exists()           # nothing written beside the source
    run = Path(r["netlist_file"]).parent
    assert (run / "my.asy").exists()


def test_netlist_from_schematic_reports_a_failed_netlist(tmp_path):
    (tmp_path / "x.asc").write_text("Version 4\n")
    runner = _runner(tmp_path, lambda argv, cwd, **kw: _Proc(lambda: None))
    with pytest.raises(NotFound):
        runner.netlist_from_schematic("compare", tmp_path / "x.asc")


def test_compare_schematic_tool_reads_a_net_file(tools, tmp_path):
    get, _ = tools
    net = tmp_path / "ref.net"
    net.write_text("V1 N1 0 5\nXU1 N1 OUT N2 0 LT3080\nR1 N2 0 100k\n.end\n")
    r = _run(get("eng_ltspice_compare_schematic")(
        str(net), pin_maps={"U1": ["3", "2", "4", "1"]}))
    assert r["ok"], r
    assert r["only_in_altium"] == ["U2"]
    assert r["net_map"]["N1"] == "VIN" and r["wiring_differences"] == []


def test_compare_schematic_tool_refuses_a_relative_path(tools):
    get, _ = tools
    assert _run(get("eng_ltspice_compare_schematic")("ref.net"))["ok"] is False


@pytest.mark.parametrize("bad", ["C:evil.lib", "a|b.lib", "..", "x" * 90 + ".lib"])
def test_copy_refuses_names_that_are_not_plain(tmp_path, bad):
    model = tmp_path / "m.lib"
    model.write_text(LIB)
    dest = tmp_path / "d"
    dest.mkdir()
    with pytest.raises(ValidationFailed):
        ltspice.copy_model_files([{"path": str(model), "include_as": bad}], dest)


def test_measurements_are_read_whatever_the_case_of_at():
    """LTspice XVII writes 'at' in lower case; it was matched upper case only."""
    log = ltspice.parse_log("vout_final: v(out)=3.29983 at 0.001\n"
                            "vmax: MAX(v(out))=3.3 FROM 0 TO 0.001\n"
                            "trise: v(out)=1 from 1e-4 to 2e-4\n")
    m = log["measurements"]
    assert m["vout_final"]["value"] == pytest.approx(3.29983)
    assert m["vout_final"]["from"] == "0.001"
    assert m["trise"]["to"] == "2e-4" and m["vmax"]["value"] == 3.3


def test_the_ltspice_tools_find_ltspice_xvii(tmp_path, monkeypatch):
    exe = tmp_path / "XVIIx64.exe"
    exe.write_text("")
    monkeypatch.delenv("EDA_AGENT_LTSPICE_EXE", raising=False)
    import eda_agent.sim.ngspice as ng
    monkeypatch.setattr(ng, "find_ltspice", lambda: str(exe))
    runner = eng_module._ltspice_runner()
    assert runner.cfg.exe == str(exe)


def test_a_wrong_ltspice_override_is_reported(tmp_path, monkeypatch):
    """A typo in the override must not quietly fall back to another LTspice."""
    other = tmp_path / "XVIIx64.exe"
    other.write_text("")
    import eda_agent.sim.ngspice as ng
    monkeypatch.setattr(ng, "find_ltspice", lambda: str(other))
    monkeypatch.setenv("EDA_AGENT_LTSPICE_EXE", str(tmp_path / "missing.exe"))
    with pytest.raises(FileNotFoundError, match="missing.exe"):
        eng_module._ltspice_runner()
