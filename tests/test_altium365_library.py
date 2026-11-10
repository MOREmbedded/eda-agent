# SPDX-License-Identifier: Apache-2.0
"""The offline Altium 365 library copy: scan, resume, update, search, report.

A fake session stands in for the service, behaving the way the live one
was seen to: the component list sorted by name, cursors that are the
base64 of a row index, and pages over a size that time out.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from eda_agent import altium365_library as lib
from eda_agent.altium365 import Altium365Error

WS = "https://example.365.altium.com/"


def _component(name: str, folder: str, mpn: str = "", symbol: str = "SYM-1",
               footprint: str = "FP-1", modified: str = "2026-01-01") -> dict:
    return {"id": "id-" + name, "name": name, "comment": name + " comment",
            "description": f"part {name}", "revisionId": "r", "isManaged": True,
            "createdAt": "2025-01-01T00:00:00Z", "modifiedAt": modified,
            "folder": {"path": folder}, "componentType": {"name": "T", "path": "T"},
            "revision": {"details": {"lifeCycleState": {"name": "Draft"},
                                     "releaseDate": ""}},
            "manufacturerParts": ([{"companyName": "Maker", "partNumber": mpn,
                                    "priority": 0}] if mpn else []),
            "model3D": None,
            "details": {"itemInternalId": "x",
                        "parameters": [{"name": "Value", "value": "10k"}],
                        "symbols": [{"id": "s", "name": symbol}] if symbol else [],
                        "footprints": [{"id": "f", "name": footprint}] if footprint else []}}


def _library(engineered: int = 30, generic: int = 120) -> list[dict]:
    rows = [_component(f"CMP-{i:08d}-1", "Components/IC", mpn=f"MPN{i}")
            for i in range(engineered)]
    rows += [_component(f"CMP-02-{i:06d}-2", "Generics/Capacitor", mpn=f"GRM{i}",
                        footprint="FP-0402") for i in range(generic)]
    return sorted(rows, key=lambda r: r["name"])


class FakeService:
    """Answers the library queries from lists; pages larger than
    ``timeout_over`` rows raise a timeout, as deep pages do."""

    def __init__(self, components: list[dict], timeout_over: int = 10_000,
                 index_cursors: bool = True):
        self.components = components
        self.symbols = [{"id": "s1", "name": "SYM-1"}, {"id": "s2", "name": "SYM-UNUSED"}]
        self.footprints = [{"id": "f1", "name": "FP-1"}, {"id": "f2", "name": "FP-0402"},
                           {"id": "f3", "name": "FP-UNUSED"}]
        self.timeout_over = timeout_over
        self.index_cursors = index_cursors
        self.calls = 0
        self.fail_after: int | None = None

    def permitted(self, url: str) -> str:
        return url

    def _cursor(self, index: int) -> str:
        if self.index_cursors:
            return base64.b64encode(str(index).encode()).decode()
        return base64.b64encode(f"opaque:{index}".encode()).decode()

    def _start(self, after) -> int:
        if not after:
            return 0
        text = base64.b64decode(after).decode()
        return int(text.split(":")[-1]) + 1

    def read(self, query: str, variables=None, remember: bool = True) -> dict:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise OSError("connection lost")
        if "folders" in query:
            return {"desLibrary": {"folders": [{"id": "1", "name": "IC",
                                                "path": "Components/IC"}]}}
        key = re.search(r"desLibrary\(workspaceUrl: \$ws\) \{ (\w+)", query).group(1)
        rows = {"components": self.components, "symbols": self.symbols,
                "footprints": self.footprints}[key]
        first = variables.get("first") or 50
        if first > self.timeout_over:
            raise Altium365Error("GraphQL errors: The request timed out")
        start = self._start(variables.get("after"))
        page = rows[start:start + first]
        end = start + len(page) - 1
        return {"desLibrary": {key: {
            "pageInfo": {"hasNextPage": start + len(page) < len(rows),
                         "endCursor": self._cursor(end) if page else None},
            "nodes": page}}}


def _scan(service, db, **kw):
    return lib.scan_library(service, WS, db, chunk_rows=kw.pop("chunk_rows", 40), **kw)


def test_cursor_round_trip():
    assert lib.row_cursor(0) is None
    assert lib.cursor_row(lib.row_cursor(12601)) == 12600
    assert lib.cursor_row("not base64!") is None


def test_full_scan_reads_every_row_past_the_10000_cap_shape(tmp_path):
    service = FakeService(_library())
    got = _scan(service, tmp_path, workers=3)
    assert got["complete"] is True
    comps = json.loads((tmp_path / "components.json").read_text("utf-8"))
    assert [c["name"] for c in comps] == [c["name"] for c in service.components]
    assert not (tmp_path / "scan").exists()
    meta = json.loads((tmp_path / "meta.json").read_text("utf-8"))
    assert meta["components"] == 150 and meta["workspace_url"] == WS
    footprints = {f["name"]: f for f in
                  json.loads((tmp_path / "footprints.json").read_text("utf-8"))}
    assert footprints["FP-0402"]["used_by_components"] == 120
    assert footprints["FP-UNUSED"]["used_by_components"] == 0
    head = (tmp_path / "components.csv").read_text("utf-8-sig").splitlines()[0]
    assert head.startswith("Name,Comment,Description,Folder")


def test_a_page_that_times_out_is_halved(tmp_path):
    service = FakeService(_library(), timeout_over=12)
    got = _scan(service, tmp_path, workers=2, page_size=50)
    assert got["complete"]
    assert len(json.loads((tmp_path / "components.json").read_text("utf-8"))) == 150


def test_a_failure_that_is_not_a_timeout_is_not_retried(tmp_path):
    class Refusing(FakeService):
        def read(self, query, variables=None, remember=True):
            raise Altium365Error("Altium 365 did not accept the token (HTTP 401)")
    with pytest.raises(Altium365Error, match="HTTP 401"):
        _scan(Refusing([]), tmp_path)


def test_a_scan_stopped_part_way_continues_without_rereading(tmp_path):
    service = FakeService(_library())
    service.fail_after = 6
    with pytest.raises(Exception):
        _scan(service, tmp_path, workers=1, page_size=20)
    partial = sum(len(r.get("nodes") or [])
                  for f in (tmp_path / "scan").glob("components.*.jsonl")
                  for r in lib._read_lines(f))
    assert partial > 0
    service.fail_after = None
    before = service.calls
    assert _scan(service, tmp_path, workers=1, page_size=20)["complete"]
    comps = json.loads((tmp_path / "components.json").read_text("utf-8"))
    assert len(comps) == 150
    # Rows already kept were not asked for again.
    assert service.calls - before < 150 // 20 + 8


def test_the_time_budget_stops_and_the_next_call_finishes(tmp_path, monkeypatch):
    service = FakeService(_library())
    ticks = iter(range(0, 10_000, 7))
    monkeypatch.setattr(lib, "_now", lambda: next(ticks))
    got = _scan(service, tmp_path, workers=1, page_size=10, max_seconds=20)
    assert got["complete"] is False and "call again" in got["note"]
    monkeypatch.setattr(lib, "_now", lambda: 0)
    assert _scan(service, tmp_path, workers=1, page_size=10)["complete"]


def test_opaque_cursors_fall_back_to_one_reader(tmp_path):
    service = FakeService(_library(), index_cursors=False)
    got = _scan(service, tmp_path, workers=4)
    assert got["complete"] and "one reader" in got["read_as"]
    assert len(json.loads((tmp_path / "components.json").read_text("utf-8"))) == 150


def test_a_scan_of_another_workspace_is_not_mixed_in(tmp_path):
    service = FakeService(_library())
    service.fail_after = 3
    with pytest.raises(Exception):
        _scan(service, tmp_path, workers=1, page_size=10)
    service.fail_after = None
    with pytest.raises(Altium365Error, match="unfinished scan"):
        lib.scan_library(service, "https://other.365.altium.com/", tmp_path)


def test_update_rereads_the_head_and_keeps_the_bulk_block(tmp_path):
    service = FakeService(_library())
    _scan(service, tmp_path, workers=2)
    assert lib.stable_families(
        json.loads((tmp_path / "components.json").read_text("utf-8"))) == ["CMP-02-"]
    service.components = sorted(
        [c for c in service.components if c["name"] != "CMP-00000003-1"]
        + [_component("CMP-00000003-2", "Components/IC"),
           _component("CMP-00000099-1", "Components/IC")], key=lambda r: r["name"])
    calls = service.calls
    got = lib.update_library(service, WS, tmp_path)
    assert got["full_scan_needed"] is False and got["stable_block"] == "unchanged"
    assert got["added"] == ["CMP-00000099"] and got["revised"] == ["CMP-00000003"]
    assert got["reread_components"] == 31 and got["kept_components"] == 120
    assert service.calls - calls < 20
    comps = json.loads((tmp_path / "components.json").read_text("utf-8"))
    assert "CMP-00000003-2" in {c["name"] for c in comps} and len(comps) == 151


def test_update_says_when_the_bulk_block_grew(tmp_path):
    service = FakeService(_library())
    _scan(service, tmp_path)
    service.components = service.components + [
        _component("CMP-02-999999-1", "Generics/Capacitor")]
    got = lib.update_library(service, WS, tmp_path)
    assert got["full_scan_needed"] is True and "grew" in got["stable_block"]


def test_update_needs_a_scan_first(tmp_path):
    with pytest.raises(Altium365Error, match="scan"):
        lib.update_library(FakeService([]), WS, tmp_path)


def test_search_matches_every_word(tmp_path):
    _scan(FakeService(_library()), tmp_path)
    found = lib.search_components([tmp_path], "grm12 capacitor")
    assert found["count"] == 1 and found["results"][0]["name"] == "CMP-02-000012-2"
    assert found["results"][0]["source"] == "altium365"
    assert lib.search_components([tmp_path], "10k", fields="name")["count"] == 0
    assert lib.search_components([tmp_path], "10k")["matches"] == 150
    assert lib.search_components([tmp_path], "", folder="Components/IC",
                                 limit=5)["truncated"] is True


def test_the_database_lives_in_the_profile(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "profile.json").write_text(json.dumps(
        {"name": "p", "altium365_workspaces": [WS]}), "utf-8")
    url, db = lib.resolve_target(profile_dir=profile)
    assert url == WS and db == profile / "library" / "example.365.altium.com"
    two = tmp_path / "two"
    two.mkdir()
    (two / "profile.json").write_text(json.dumps(
        {"altium365_workspaces": [WS, "https://b.365.altium.com"]}), "utf-8")
    with pytest.raises(Altium365Error, match="several workspaces"):
        lib.resolve_target(profile_dir=two)


def test_no_profile_and_no_folder_is_refused(monkeypatch):
    monkeypatch.setattr(lib, "_active_profile_dir", lambda: None)
    with pytest.raises(Altium365Error, match="no profile is active"):
        lib.resolve_target(WS)
    with pytest.raises(Altium365Error, match="not a workspace URL"):
        lib.workspace_host("https://exa mple/")


def test_known_databases_are_the_profiles_finished_scans(tmp_path):
    profile = tmp_path / "p"
    _scan(FakeService(_library()), profile / "library" / "example.365.altium.com")
    (profile / "library" / "half").mkdir()
    assert lib.known_databases(profile) == [profile / "library" / "example.365.altium.com"]


def test_base_item_drops_only_a_numeric_revision():
    assert lib.base_item("CMP-02-014133-4") == "CMP-02-014133"
    assert lib.base_item("PART") == "PART"
    assert lib.name_family("CMP-02-014133-4") == "CMP-02-"
    assert lib.name_family("CMP-00000001-3") == "CMP-"


def test_report_joins_placements_on_the_item(tmp_path, monkeypatch):
    _scan(FakeService(_library()), tmp_path)
    usage = {"roots": ["designs"], "sheets_read": 2, "sheet_errors": [], "placed": [
        {"design_item_id": "CMP-00000001-1", "item": "CMP-00000001",
         "source_library": "Lib", "projects": ["A"], "placements": 2, "designators": []},
        {"design_item_id": "CMP-02-000005-1", "item": "CMP-02-000005",
         "source_library": "Lib", "projects": ["A", "B"], "placements": 1,
         "designators": []},
        {"design_item_id": "GONE-1", "item": "GONE", "source_library": "Lib",
         "projects": ["B"], "placements": 1, "designators": []}]}
    monkeypatch.setattr(lib, "scan_design_usage", lambda roots: usage)
    report = lib.health_report(tmp_path, [tmp_path / "designs"])
    checks = report["checks"]
    assert checks["placed_with_an_older_revision"]["count"] == 1
    assert "CMP-02-000005-1" in checks["placed_with_an_older_revision"]["sample"][0]
    assert checks["placed_but_not_in_this_library"]["count"] == 1
    assert checks["footprints_used_by_no_component"]["sample"] == ["FP-UNUSED"]
    assert checks["without_3d_model_link"]["count"] == 150
    comps = {c["name"]: c for c in
             json.loads((tmp_path / "components.json").read_text("utf-8"))}
    assert comps["CMP-00000001-1"]["placements"] == 2
    assert comps["CMP-02-000005-2"]["placed_older_revision"] is True
    assert (tmp_path / "report.md").read_text("utf-8").startswith("# Library health")
    assert lib.read_meta(tmp_path)["design_roots"] == [str(tmp_path / "designs")]


def test_design_usage_reads_schematics_offline(tmp_path, monkeypatch):
    project = tmp_path / "Board"
    project.mkdir()
    (project / "Board.PrjPcb").write_text("", "utf-8")
    (project / "Main.SchDoc").write_bytes(b"")
    (project / "History").mkdir()
    (project / "History" / "Old.SchDoc").write_bytes(b"")
    seen = []

    def fake(path):
        seen.append(Path(path).name)
        return [{"designator": "U1", "design_item_id": "CMP-1-2", "lib_reference": "",
                 "source_library": "Lib", "managed": True, "revision_guid": ""},
                {"designator": "R1", "design_item_id": "R-LOCAL", "lib_reference": "",
                 "source_library": "R.SchLib", "managed": False, "revision_guid": ""}]
    monkeypatch.setattr(lib, "placements_on_sheet", fake)
    usage = lib.scan_design_usage([tmp_path])
    assert seen == ["Main.SchDoc"]
    assert usage["placed"] == [{"design_item_id": "CMP-1-2", "item": "CMP-1",
                                "source_library": "Lib", "projects": ["Board"],
                                "placements": 1, "designators": ["Board:U1"]}]


def test_transient_failures_are_told_apart():
    assert lib.transient(OSError("reset"))
    assert lib.transient(Altium365Error("Altium 365 answered HTTP 504: x"))
    assert not lib.transient(Altium365Error("did not accept the token (HTTP 403)"))


def test_explorer_subtree_follows_the_indent():
    from eda_agent.ui import explorer
    rows = [{"name": n, "indent": i} for n, i in (
        ("Components", 0), ("IC", 16), ("3D Models", 0), ("Connectors", 16),
        ("USB", 32), ("Passives", 16), ("Datasheets", 0), ("Other 3D", 0))]
    picked = explorer.subtree(rows, ["3d models", "Other 3D"])
    assert [r["name"] for r in picked] == ["3D Models", "Connectors", "USB",
                                           "Passives", "Other 3D"]
    assert explorer.folder_paths(picked) == [
        "3D Models", "3D Models/Connectors", "3D Models/Connectors/USB",
        "3D Models/Passives", "Other 3D"]
    assert explorer.split_row(";A3D-1;2;Box;desc;Released;") == [
        "A3D-1", "2", "Box", "desc", "Released", ""]


def test_3d_models_are_kept_next_to_the_library(tmp_path):
    got = {"folders": {"3D Models": 2, "3D Models/Gone": -1}, "items": [
        {"item_id": "A3D-1", "revision": "1", "name": "Box", "description": "",
         "state": "Released", "note": "", "folder": "3D Models"},
        {"item_id": "A3D-2", "revision": "1", "name": "Cyl", "description": "",
         "state": "Draft", "note": "", "folder": "3D Models"}]}
    out = lib.save_models3d(tmp_path, WS, got)
    assert out["models"] == 2 and out["folders_not_read"] == ["3D Models/Gone"]
    assert len(lib.load_models3d(tmp_path)) == 2
    assert (tmp_path / "models3d.csv").read_text("utf-8-sig").startswith("Item ID,")
    _scan(FakeService(_library()), tmp_path)
    assert lib.health_report(tmp_path)["models3d"]["count"] == 2


def test_saved_user_environment_fills_what_the_process_lacks(monkeypatch):
    """A value set with setx after the server started still counts, and
    the process's own value wins over it."""
    from eda_agent import altium365
    monkeypatch.setattr(altium365, "saved_user_environment", lambda names=None: {
        "EDA_AGENT_NEXAR_APP_ID": "saved-id", "EDA_AGENT_ALTIUM365_TIMEOUT_S": "120",
        "EDA_AGENT_ALTIUM365_WORKSPACES": "https://saved.365.altium.com"})
    monkeypatch.delenv("EDA_AGENT_NEXAR_APP_ID", raising=False)
    monkeypatch.delenv("EDA_AGENT_ALTIUM365_DOMAIN", raising=False)
    monkeypatch.setenv("EDA_AGENT_ALTIUM365_WORKSPACES", WS)
    monkeypatch.delenv("EDA_AGENT_ALTIUM365_TIMEOUT_S", raising=False)
    config = altium365.read_environment()
    assert config.nexar_app_id == "saved-id" and config.timeout_s == 120
    assert config.allowed_workspaces == (WS,)
    assert altium365.read_environment({"EDA_AGENT_ALTIUM365_TIMEOUT_S": "1"}).timeout_s == 5
