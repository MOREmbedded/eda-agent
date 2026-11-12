# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Altium 365 tools: workspaces, projects and the managed library, read
through the workspace's own GraphQL API or through Nexar (eda_agent/altium365.py
says how to set up either), and three changes to the library that are only
sent with confirm=True.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Optional


def _session():
    from ..altium365 import Altium365Session
    return Altium365Session()


async def _answer(work: Callable[[], dict]) -> dict[str, Any]:
    """Run blocking work off the event loop; failures come back as reasons."""
    from ..altium365 import Altium365Error
    try:
        result = await asyncio.to_thread(work)
    except Altium365Error as exc:
        return {"ok": False, "reason": str(exc)}
    except OSError as exc:
        return {"ok": False, "reason": f"Altium 365 could not be reached: {exc}"}
    return {"ok": True, **result}


def _preview(operation: str, what: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "sent": False, "would_send": operation, **extra,
            "note": f"nothing was {what}; call again with confirm=True to send it"}


def register_altium365_tools(mcp) -> None:

    @mcp.tool()
    async def altium365_check_setup() -> dict[str, Any]:
        """Which Altium 365 route is set up, whether the live schema has the
        fields the read tools use, and which changes it offers.

        Routes: the workspace's own API (EDA_AGENT_ALTIUM365_DOMAIN, and a
        workspace token in EDA_AGENT_ALTIUM365_TOKEN, on plans that offer
        one), or Nexar (EDA_AGENT_NEXAR_APP_ID, plus
        EDA_AGENT_NEXAR_CLIENT_SECRET for a portal app, then
        ``eda-agent altium365-login``).
        """
        return await _answer(lambda: _session().check_setup())

    @mcp.tool()
    async def altium365_list_workspaces() -> dict[str, Any]:
        """The Altium 365 workspaces the token sees (url, id, name,
        description); with EDA_AGENT_ALTIUM365_WORKSPACES set, only those."""
        return await _answer(lambda: {"workspaces": _session().list_workspaces()})

    @mcp.tool()
    async def altium365_list_projects(workspace_url: str, name_contains: str = "",
                                      limit: int = 200) -> dict[str, Any]:
        """Projects of one workspace (id, name, description), optionally those
        whose name contains a text."""
        return await _answer(
            lambda: _session().list_projects(workspace_url, name_contains, limit))

    @mcp.tool()
    async def altium365_get_project(project_id: str) -> dict[str, Any]:
        """One Altium 365 project by id."""
        return await _answer(lambda: {"project": _session().get_project(project_id)})

    @mcp.tool()
    async def altium365_list_library_folders(workspace_url: str) -> dict[str, Any]:
        """Folders of a workspace's managed library (id, name, path); publishing
        names its folder by id."""
        return await _answer(
            lambda: {"folders": _session().list_library_folders(workspace_url)})

    @mcp.tool()
    async def altium365_list_components(workspace_url: str, name_contains: str = "",
                                        with_parameters: bool = False,
                                        limit: int = 500) -> dict[str, Any]:
        """Managed components of a workspace (id, name), and their parameters
        when asked, read page by page until ``limit`` matches are found or
        the library ends (``limit=0``: the whole library, which can take
        minutes; altium365_scan_library keeps a whole library offline).

        Returns:
            ``components``, ``pages_read``, and ``cut_short`` when more
            matches were left unread.
        """
        return await _answer(lambda: _session().list_components(
            workspace_url, name_contains, with_parameters, limit))

    @mcp.tool()
    async def altium365_get_component(component_id: str) -> dict[str, Any]:
        """One managed component and its parameters."""
        return await _answer(lambda: {"component": _session().get_component(component_id)})

    @mcp.tool()
    async def altium365_read_object(query_field: str, object_id: str) -> dict[str, Any]:
        """Any object a ``des...ById`` query returns (a release, a part choice,
        a comment thread), asking for the fields the live schema lists, never
        guessed ones. altium365_describe_type("Query") lists the queries.

        Returns:
            ``type``, ``fields``, and ``nested_fields`` to read with
            altium365_run_query.
        """
        return await _answer(lambda: _session().read_object(query_field, object_id))

    @mcp.tool()
    async def altium365_describe_type(type_name: str = "Query") -> dict[str, Any]:
        """One type of the live Altium 365 schema with its fields, their
        arguments and types: start at "Query" and follow the types named."""
        return await _answer(lambda: {"type": _session().describe_type(type_name)})

    @mcp.tool()
    async def altium365_run_query(query: str,
                                  variables: Optional[dict] = None) -> dict[str, Any]:
        """Run a reading GraphQL query against Altium 365. Queries that change
        anything (mutation, subscription) are refused; library changes are
        altium365_publish_library_file, altium365_release_component and
        altium365_set_component_parameters."""
        return await _answer(lambda: {"data": _session().read(query, variables)})

    @mcp.tool()
    async def altium365_scan_library(workspace_url: str = "", database_dir: str = "",
                                     workers: int = 4, max_seconds: int = 600,
                                     restart: bool = False) -> dict[str, Any]:
        """Read a workspace's whole managed library into an offline database:
        every component (folder, lifecycle, parameters, manufacturer parts,
        symbols, footprints, 3D link), symbol, footprint and folder, as JSON
        and CSV. Read-only: nothing in the workspace changes.

        The database belongs to the active profile, in
        ``<profile>/library/<workspace host>/``; ``workspace_url`` may be
        left out when the profile's profile.json lists one workspace under
        ``altium365_workspaces``. A large library takes several minutes:
        the scan stops after ``max_seconds`` with ``complete: false`` and
        the next call continues from there. Then altium365_update_library
        keeps it fresh, altium365_search_library and lib_search search it.

        Args:
            workspace_url: The workspace, or "" for the profile's one.
            database_dir: Somewhere else to keep it (default: the profile).
            workers: Readers working on different ranges at once (1-16).
            max_seconds: Time budget for this call; 0 for none.
            restart: Drop an unfinished scan and start over.
        """
        from ..altium365_library import resolve_target, scan_library

        def scan() -> dict:
            url, db = resolve_target(workspace_url, database_dir)
            return scan_library(_session(), url, db, workers=workers,
                                max_seconds=max_seconds or None, restart=restart)
        return await _answer(scan)

    @mcp.tool()
    async def altium365_update_library(workspace_url: str = "", database_dir: str = "",
                                       stable_prefixes: Optional[list[str]] = None
                                       ) -> dict[str, Any]:
        """Refresh a library scanned by altium365_scan_library, in a minute or
        two instead of a full read.

        The list is sorted by name, so the bulk block at its end (a generic
        value range, say) is kept, and everything before it is read again,
        with every symbol, footprint and folder. The row just past the last
        known one is then read: ``full_scan_needed`` is true when the bulk
        block grew or shrank. ``added``, ``removed`` and ``revised`` name
        what changed.

        Args:
            workspace_url: The workspace, or "" for the profile's one.
            database_dir: Where the scan was kept (default: the profile).
            stable_prefixes: Name prefixes of the block to keep (e.g.
                ["RES-", "CAP-"]); by default the one the last update
                used, else the run of components at the end of the list
                under one top folder.
        """
        from ..altium365_library import resolve_target, update_library

        def update() -> dict:
            url, db = resolve_target(workspace_url, database_dir)
            return update_library(_session(), url, db, stable_prefixes=stable_prefixes)
        return await _answer(update)

    @mcp.tool()
    async def altium365_search_library(query: str, workspace_url: str = "",
                                       fields: str = "all", folder: str = "",
                                       lifecycle: str = "",
                                       limit: int = 50) -> dict[str, Any]:
        """Search the active profile's scanned Altium 365 libraries offline,
        no Altium and no network needed. Every word of ``query`` must
        appear in a component's name, comment, description, folder,
        manufacturer, MPN or parameter values. The answer says when each
        library was read; refresh with altium365_update_library.

        Args:
            query: Words to find, e.g. "100nF 0402 X7R".
            workspace_url: One workspace's library, or "" for all of them.
            fields: "all", "name" (name and comment), "description", or
                "mpn" (name, description, folder, manufacturer parts).
            folder: Only components whose folder path contains this.
            lifecycle: Only this lifecycle state, e.g. "Released".
            limit: Most results returned (``matches`` counts them all).
        """
        from ..altium365_library import (known_databases, resolve_target,
                                         search_components)

        def search() -> dict:
            if workspace_url:
                databases = [resolve_target(workspace_url)[1]]
            else:
                databases = known_databases()
            if not databases:
                return {"count": 0, "results": [], "databases": [],
                        "note": "no scanned library in the active profile: run "
                                "altium365_scan_library first"}
            return search_components(databases, query, fields=fields, limit=limit,
                                     folder=folder, lifecycle=lifecycle)
        return await _answer(search)

    @mcp.tool()
    async def altium365_library_report(workspace_url: str = "", database_dir: str = "",
                                       design_roots: Optional[list[str]] = None
                                       ) -> dict[str, Any]:
        """Health report of a scanned Altium 365 library, offline: components
        without a symbol, footprint, manufacturer part, 3D link or
        description, symbols and footprints no component uses, MPNs on
        several components, lifecycle and folder counts.

        Given design_roots (folders holding projects), every .SchDoc under
        them is read from disk, and each placed managed part is joined to
        the library on its item id without the revision: placements of an
        older revision, and parts placed but missing from the library, are
        listed, and each component gets its projects and placement count.
        The roots are remembered for the next report. Writes report.json and
        report.md next to the database.
        """
        from ..altium365_library import health_report, read_meta, resolve_target

        def report() -> dict:
            _url, db = resolve_target(workspace_url, database_dir)
            roots = design_roots if design_roots is not None else                 read_meta(db).get("design_roots")
            return health_report(db, [Path(r) for r in roots] if roots else None)
        return await _answer(report)

    @mcp.tool()
    async def altium365_scan_3d_models(folders: Optional[list[str]] = None,
                                       workspace_url: str = "",
                                       database_dir: str = "") -> dict[str, Any]:
        """List the 3D model items of the managed library, which the Altium
        365 API cannot list, by reading Altium's Explorer panel.

        Altium must be running and signed in to the workspace, with
        Explorer open (View > Panels > Explorer) and not minimised. Each
        folder under ``folders`` is selected in Explorer's folder tree, and its item grid is read: item id, revision, name,
        description, state and note per model. Nothing is clicked, typed
        or opened; selecting a folder row is the only change on screen.
        Writes models3d.json and models3d.csv next to the library database.

        Args:
            folders: Folders holding 3D models, by name or path (default
                ["3D Models"], e.g. ["Models/3D Models"]); every folder
                under them is read too. To find one, the folders on its
                path, or those whose name mentions "model", are expanded;
                whatever was expanded is folded back afterwards.
            workspace_url: Which database to write to, or "" for the
                profile's one.
            database_dir: Somewhere else to keep it (default: the profile).
        """
        from ..altium365_library import resolve_target, save_models3d
        from ..ui import explorer, panels
        from .uiauto import _altium_pid

        _pid, problem = _altium_pid()
        if problem is not None:
            return problem
        if not panels.available():
            return {"ok": False, "reason": "UI automation is unavailable here"}
        roots = [f for f in (folders or ["3D Models"]) if f.strip()]
        try:
            url, db = resolve_target(workspace_url, database_dir)
        except Exception as exc:  # noqa: BLE001 - the reason is the answer
            return {"ok": False, "reason": str(exc)}
        got = await asyncio.to_thread(panels.on_com_thread,
                                      lambda: explorer.scan_models(roots))
        if not got.get("ok"):
            return got
        return {"ok": True, **save_models3d(db, url, got)}

    @mcp.tool()
    async def altium365_publish_library_file(kind: str, workspace_url: str, folder_id: str,
                                             name: str, file_path: str,
                                             confirm: bool = False) -> dict[str, Any]:
        """Publish a symbol (.SchLib) or footprint (.PcbLib) into a folder of a
        workspace's managed library.

        Without ``confirm`` this shows what would be sent. With it the file
        is uploaded and the item created, and its ``id`` comes back.
        """
        from ..altium365 import Altium365Error, publish_operation, reported_errors
        if kind not in ("symbol", "footprint"):
            return {"ok": False, "reason": "kind is symbol or footprint"}
        source = Path(file_path)
        if not source.is_file() or source.suffix.lower() not in (".schlib", ".pcblib"):
            return {"ok": False, "reason": "file_path names an existing .SchLib or .PcbLib"}
        field_name, operation = publish_operation(kind)
        values = {"fileId": "<the uploaded file>", "relativePath": f"Released/{source.name}",
                  "folderId": folder_id, "ws": workspace_url, "name": name}
        if not confirm:
            return _preview(operation, "published", variables=values)

        def publish() -> dict:
            session = _session()
            session.require_change(f"publish_{kind}")
            values["ws"] = session.permitted(workspace_url)
            values["fileId"] = session.upload_file(source)
            data = session.run_change(operation, values)
            made = (data.get(field_name) or {}).get("id")
            problems = reported_errors(data, field_name)
            if problems or not made:
                raise Altium365Error(f"{field_name} created nothing: "
                                     + "; ".join(problems or ["no id came back"]))
            return {"sent": True, "id": made, "file_id": values["fileId"]}
        return await _answer(publish)

    @mcp.tool()
    async def altium365_release_component(release: dict,
                                          confirm: bool = False) -> dict[str, Any]:
        """Release a managed component (desReleaseComponent). ``release`` is
        that operation's input as Altium documents it, with at least
        ``workspaceUrl`` and ``componentItemName``. Without ``confirm`` this
        shows what would be sent."""
        from ..altium365 import Altium365Error, release_operation, reported_errors
        if not isinstance(release, dict) or not all(
                release.get(k) for k in ("workspaceUrl", "componentItemName")):
            return {"ok": False, "reason": "release needs workspaceUrl and componentItemName"}
        try:
            operation = release_operation(release)
        except Altium365Error as exc:
            return {"ok": False, "reason": str(exc)}
        if not confirm:
            return _preview(operation, "released")

        def send() -> dict:
            session = _session()
            session.require_change("release_component")
            session.permitted(release["workspaceUrl"])
            problems = reported_errors(session.run_change(operation), "desReleaseComponent")
            if problems:
                raise Altium365Error("desReleaseComponent: " + "; ".join(problems))
            return {"sent": True, "released": release["componentItemName"]}
        return await _answer(send)

    @mcp.tool()
    async def altium365_set_component_parameters(
            component_id: str, parameters: list[dict], release_note: str,
            replace_existing: bool = False, confirm: bool = False) -> dict[str, Any]:
        """Add or change a managed component's parameters as a new revision.

        ``parameters`` is [{"name", "value"}]; ``replace_existing`` drops
        every parameter the component has first. The new revision has a new
        component id, which comes back. Without ``confirm`` this shows what
        would be sent.
        """
        from ..altium365 import Altium365Error, parameters_operation
        if not parameters or any(not isinstance(p, dict) or not p.get("name")
                                 for p in parameters):
            return {"ok": False, "reason": "parameters is a list of {name, value}"}
        if not release_note.strip():
            return {"ok": False, "reason": "release_note says why the revision is made"}
        try:
            operation = parameters_operation(component_id, parameters, release_note,
                                             replace_existing)
        except Altium365Error as exc:
            return {"ok": False, "reason": str(exc)}
        if not confirm:
            return _preview(operation, "changed")

        def send() -> dict:
            session = _session()
            session.require_change("set_parameters")
            data = session.run_change(operation)
            revised = (data.get("desUpdateComponentRevisionParameters") or {}).get(
                "componentId")
            if not revised:
                raise Altium365Error("the change returned no new component id")
            return {"sent": True, "new_component_id": revised}
        return await _answer(send)
