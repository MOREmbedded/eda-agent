# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Provider-neutral part search and fetch.

Two tools rather than one per source, so adding a provider never grows
the tool surface (see the tool-count concern in issue #10), and so no
single source occupies a privileged name.
"""

from __future__ import annotations

from typing import Any

__all__ = ["register_parts_tools"]



#: Above this many characters the provider's detail is written to the
#: workspace and the reply carries its outline. An EasyEDA part's raw shapes
#: came to 89 KB, which Claude Code set aside in a file under the user's
#: profile and showed the path of.
_DETAIL_INLINE_CHARS = 20_000


def _outline(value: Any, depth: int = 0) -> Any:
    """``value`` with its short scalars kept and everything bulky counted."""
    if isinstance(value, dict):
        if depth >= 2:
            return f"<{len(value)} keys>"
        return {k: _outline(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return f"<{len(value)} entries>"
    if isinstance(value, str) and len(value) > 300:
        return f"<text, {len(value)} characters>"
    return value


def _fit_detail(detail: Any, provider: str, part_id: str) -> dict[str, Any]:
    """Nothing when ``detail`` fits one reply; else its outline in place of
    it, and the whole of it written to the workspace (``detail_path``)."""
    import json

    from ..config import get_config

    text = json.dumps(detail, default=str)
    if len(text) <= _DETAIL_INLINE_CHARS:
        return {}
    folder = get_config().workspace_dir / "parts"
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in f"{provider}_{part_id}")
    path = folder / f"{safe}.json"
    path.write_text(text, encoding="utf-8")
    return {"detail": _outline(detail), "detail_trimmed": len(text),
            "detail_path": str(path),
            "detail_note": ("the provider's full detail is in detail_path; "
                            "summary holds what a part choice needs")}

def register_parts_tools(mcp):
    """Register the provider-neutral part tools with the MCP server."""

    @mcp.tool()
    async def part_search(
        query: str = "",
        limit_per_provider: int = 20,
    ) -> dict[str, Any]:
        """Search EVERY enabled part provider and merge the results.

        All providers count equally; hits are ordered by provider then
        part, which is NOT relevance. A provider that cannot answer says
        why ("unavailable" is not "no such part"). Each hit has a
        ``kind``: "library" gives geometry (symbol or footprint),
        "catalogue" gives identity (MPN, datasheet, stock) and no
        geometry. Remote providers need the internet; local ones still
        answer without it. An empty query lists the providers.

        DATASHEET DISCIPLINE: a hit is a lead. Blank ``provenance`` or
        ``license`` means unknown; audit an imported footprint with
        lib_audit_footprint_vs_datasheet before trusting it.

        A hit may carry an `extra` dict with provider-specific data not
        shared across sources (e.g. Nexar's stock, pricing, lifecycle
        status and similar parts); absent when the provider has none.

        Args:
            query: Substring of part name, manufacturer or keywords.
            limit_per_provider: Cap per provider.

        Returns:
            {"ok", "count", "providers": {name: status}, "hits",
            "by_mpn"} (the same part across providers; wildcard part
            numbers are not merged), or {"ok", "providers"} for an empty
            query.
        """
        from eda_agent.libimport.providers import (
            available_providers,
            search_all,
        )

        if not str(query).strip():
            return {
                "ok": True,
                "providers": [
                    {
                        "name": p.name,
                        "description": p.description,
                        # "library" = geometry you can place.
                        # "catalogue" = identity and a datasheet, with
                        # nothing to import.
                        "kind": getattr(p, "kind", "library"),
                        # False = the endpoint was probed live but the
                        # request and response shapes have never been
                        # exercised with a real credential. Stated, not
                        # assumed.
                        "verified_live": bool(
                            getattr(p, "verified_live", True)),
                        "formats": list(getattr(p, "formats", ())),
                        # Which EDA tool can actually consume a fetch.
                        # There is an EasyEDA->Altium converter here but
                        # NO KiCad->Altium path, so a kicad-only source
                        # is a dead end for an Altium user and should
                        # say so before they spend time on it.
                        "usable_in": list(getattr(p, "usable_in", ())),
                    }
                    for p in available_providers()
                ],
                "note": ("No provider is a default; part_search queries "
                         "all of them equally. Select a subset with "
                         "EDA_AGENT_PART_PROVIDERS."),
            }

        result = search_all(query, limit_per_provider)
        result["ok"] = True
        if not result["count"]:
            # Distinguish "everything answered and found nothing" from
            # "nothing could answer", which look identical otherwise.
            reachable = [n for n, s in result["providers"].items()
                         if s.get("ok")]
            result["note"] = (
                f"no matches from {len(reachable)} reachable provider(s)"
                if reachable else
                "NO provider could answer; this is not evidence the part "
                "does not exist")
        return result

    @mcp.tool()
    async def part_fetch(
        part_id: str,
        provider: str = "",
        download_dir: str = "",
    ) -> dict[str, Any]:
        """Fetch one part's detail, by the ``ref`` a search returned.

        The ``ref`` is ``provider:part_id``; pass it through. A bare id
        with no ``provider`` and no known prefix is refused, never
        guessed; ``provider`` wins over a prefix.

        ``download_dir`` also writes the provider's library files there.
        A downloaded KiCad file in a newer format than the local KiCad
        reads comes back with a ``*_warning`` entry.

        Returns the provider's own detail plus a normalised summary
        (mpn, manufacturer, datasheet, provenance, license) where the
        provider supplies one, and ``files`` when a download was asked
        for.
        """
        from eda_agent.libimport.providers import (
            ProviderError,
            ProviderUnavailable,
            available_providers,
            get_provider,
        )

        # Unpack a "provider:part_id" ref. Split on the FIRST colon and
        # only when the prefix names a REGISTERED provider: part ids
        # legitimately contain colons ("Device:R",
        # "Lib.SchLib::Comp"), so an unconditional split would corrupt
        # them. An explicit provider argument wins.
        if not provider:
            prefix, sep, rest = (part_id or "").partition(":")
            if sep and rest:
                known = {p.name for p in available_providers()}
                if prefix in known:
                    provider, part_id = prefix, rest

        if not provider:
            return {
                "ok": False,
                "reason": (
                    f"cannot tell which source {part_id!r} came from. Pass "
                    "the ref from part_search (it looks like "
                    "'provider:part_id'), or name the provider. This is "
                    "not guessed: picking a source would make one of them "
                    "the silent default for every fetch."
                ),
            }

        try:
            source = get_provider(provider)
        except ProviderError as exc:
            return {"ok": False, "reason": str(exc)}

        try:
            detail = source.fetch(part_id)
        except ProviderUnavailable as exc:
            return {"ok": False, "unavailable": str(exc),
                    "provider": source.name}
        except ProviderError as exc:
            return {"ok": False, "reason": str(exc), "provider": source.name}

        summary = None
        describe = getattr(source, "describe", None)
        if callable(describe):
            try:
                summary = describe(part_id).to_dict()
            except Exception:  # noqa: BLE001 - detail already succeeded
                summary = None

        result: dict[str, Any] = {
            "ok": True,
            "provider": source.name,
            "part_id": part_id,
            "summary": summary,
            "detail": detail,
        }
        result.update(_fit_detail(detail, source.name, part_id))

        if download_dir:
            download = getattr(source, "download", None)
            if not callable(download):
                result["files"] = {}
                result["download_note"] = (
                    f"{source.name} offers no downloadable files")
            else:
                try:
                    result["files"] = download(part_id, download_dir)
                except (ProviderError, OSError) as exc:
                    # The detail already succeeded; report the download
                    # failure without discarding what did work.
                    result["files"] = {}
                    result["download_error"] = str(exc)
        return result

    @mcp.tool()
    async def part_find_replacements(
        mpn: str,
        candidates: str = "",
        max_candidates: int = 8,
        rel_tol: float = 0.0,
    ) -> dict[str, Any]:
        """Judge replacements for a part on parameters, lifecycle and stock.

        For each candidate: every parameter compared against the
        original's with the direction a designer would apply (more
        voltage rating is safe, more ESR is not, a narrower temperature
        range is not, a different dielectric is not), plus its lifecycle
        and what is in stock. Candidates are ordered by fewest blocking
        findings; that is a mechanical order, not a ranking.

        THIS DOES NOT APPROVE A SUBSTITUTION, and no wording in the
        result will suggest it does. Four things are NOT checked and
        every result repeats them:

        * product-change notices and errata -- no distributor API serves
          them, and a PCN is the commonest reason a part that passes
          everything here is still wrong this month;
        * pad geometry -- the package NAME is compared, never the land
          pattern, so run ``lib_audit_footprint_vs_datasheet`` against
          the candidate's datasheet before trusting the footprint;
        * anything the catalogues do not publish, which routinely
          includes the parameter that actually decides the swap;
        * an attribute the candidate simply does not state, which is
          reported as unreviewed and never as a pass.

        Candidate sources: the MPNs you pass, else the aggregator's own
        "similar parts" for the original. Each one costs a full search
        across every enabled provider, so ``max_candidates`` caps it.

        Args:
            mpn: the part being replaced.
            candidates: comma-separated MPNs to judge. Empty uses the
                similar parts the original's catalogue entry names.
            max_candidates: cap on how many are searched and judged.
            rel_tol: relative tolerance for "the same value", e.g. 0.05
                to accept 5%. The default 0 means exactly equal, which
                is what a capacitance or an output voltage should be.

        Returns:
            ``{ok, original, candidates: [...], not_checked, note}``,
            or ``{ok: False, reason}`` when the original itself cannot
            be found or publishes no parameters to compare against.
        """
        from eda_agent.engineering.replacements import rank_replacements
        from eda_agent.libimport.providers import normalise_mpn, search_all

        wanted = str(mpn or "").strip()
        if not wanted:
            return {"ok": False, "reason": "no part number given"}

        def collect(query: str) -> dict[str, Any]:
            """Everything the catalogues say about one MPN, merged.

            Merged rather than per-source because the sources are
            complementary, not competing: Nexar may carry the
            parametric map while Digi-Key carries the stock. First
            non-empty value wins, with the richest parametric map
            preferred, and ``sources`` records who answered so a
            surprising figure can be traced back.
            """
            found = search_all(query, 10)
            target = normalise_mpn(query)
            matches = [h for h in found.get("hits") or []
                       if normalise_mpn(h.get("mpn", "")) == target]
            if not matches:
                return {}
            matches.sort(key=lambda h: -len(
                (h.get("extra") or {}).get("parameters") or {}))
            merged: dict[str, Any] = {"sources": [], "providers_status":
                                      found.get("providers", {})}
            for hit in matches:
                extra = hit.get("extra") or {}
                merged["sources"].append(hit.get("provider", ""))
                for key, value in (("mpn", hit.get("mpn")),
                                   ("manufacturer", hit.get("manufacturer")),
                                   ("datasheet", hit.get("datasheet")),
                                   ("provider", hit.get("provider")),
                                   ("parameters", extra.get("parameters")),
                                   ("parameter_names",
                                    extra.get("parameter_names")),
                                   ("lifecycle", extra.get("lifecycle")),
                                   ("similar_parts",
                                    extra.get("similar_parts"))):
                    if value not in (None, "", {}, []) and not merged.get(key):
                        merged[key] = value

            # Stock is merged by MAXIMUM, not by first-wins. The figures
            # mean different things per source -- Nexar's is an aggregate
            # across distributors, Digi-Key's is Digi-Key's own shelf --
            # so summing them would double-count, and taking whichever
            # source happened to sort first would let a zero from one
            # distributor hide five thousand at another. The largest
            # credible figure answers the question actually being asked,
            # which is whether the part can be bought.
            #
            # A zero is a figure and is kept; "no source said" stays
            # absent, because _stock_note reads those two differently.
            stocks = [h.get("extra", {}).get("total_available")
                      for h in matches]
            stocks = [s for s in stocks
                      if isinstance(s, (int, float))
                      and not isinstance(s, bool)]
            if stocks:
                merged["total_available"] = max(stocks)
            return merged

        original = collect(wanted)
        if not original:
            return {"ok": False, "reason":
                    f"no catalogue returned {wanted!r}. That is not "
                    f"evidence the part does not exist: check "
                    f"part_search for which providers could answer."}
        if not original.get("parameters"):
            return {"ok": False, "reason":
                    f"{wanted!r} was found but no source published any "
                    f"parameters for it, so there is nothing to compare a "
                    f"replacement against. Digi-Key, Mouser, Nexar and "
                    f"element14 all publish them when credentialed; check "
                    f"part_search for which of them answered.",
                    "sources": original.get("sources", [])}

        names = [c.strip() for c in str(candidates or "").split(",")
                 if c.strip()]
        used_similar = False
        if not names:
            used_similar = True
            names = [s.get("mpn", "") for s in
                     original.get("similar_parts") or [] if s.get("mpn")]
        if not names:
            return {"ok": False, "reason":
                    f"no candidates to judge: none were given and no "
                    f"source named a similar part for {wanted!r}. Pass "
                    f"candidates='MPN1,MPN2'."}

        judged = []
        skipped = []
        for name in names[:max(1, int(max_candidates))]:
            data = collect(name)
            if data:
                judged.append(data)
            else:
                # A candidate nobody carries is worth naming: it is a
                # lead that went nowhere, not a part that failed.
                skipped.append(name)

        result = rank_replacements(original, judged, rel_tol=float(rel_tol))
        result["ok"] = True
        result["candidates_requested"] = names
        if skipped:
            result["not_found"] = skipped
        if used_similar:
            result["note"] += (
                " Candidates came from the aggregator's own 'similar "
                "parts' list, which is a catalogue's idea of similar, "
                "not an engineering judgement.")
        return result

    @mcp.tool()
    async def part_parse_lcsc_label(payload: str) -> dict[str, Any]:
        """Decode the QR payload printed on an LCSC reel or bag label.

        Pass the text a scanner produced, e.g.
        ``{pbn:PICK2403120102,on:GB2403120671,pc:C916399,pm:JSM2301S,qty:50,
        mc:,cc:1,pdi:107160244,hp:0,wc:ZH}``. Returns the LCSC part number,
        the manufacturer part number and the quantity, plus every other
        field verbatim.

        Only pc, pm and qty have a documented meaning, and that
        documentation is a community source rather than LCSC's own. The
        rest are returned under ``fields`` WITHOUT invented names, so a
        guess never arrives looking like a fact. ``quantity`` is null
        when the label did not state one, which is not the same as zero.

        This decodes text only: reading the QR code from an image is the
        scanner's job, not this tool's.
        """
        from ..libimport.lcsc_barcode import parse_lcsc_barcode
        return parse_lcsc_barcode(payload)

    @mcp.tool()
    async def part_cache_datasheet(url: str, mpn: str = "",
                             force: bool = False) -> dict[str, Any]:
        """Download a datasheet PDF to the local cache and return its path.

        Component values must come from the datasheet rather than from
        memory, and a cached PDF can be re-read, compared against a later
        revision and cited by path long after the conversation that found
        it. Fetching reuses the providers' hardened path: HTTPS only, a
        host allowlist, a byte cap and a timeout.

        The allowlist is DENY BY DEFAULT and comes from
        ``EDA_AGENT_DATASHEET_HOSTS`` (comma-separated). A datasheet URL
        usually comes from a model or a web page, so there is no built-in
        list of vendor domains to trust on your behalf; the refusal names
        the host so allowing it is one step. The payload must really be a
        PDF, checked by magic bytes, because a vendor page that redirects
        to a sign-in form is the common failure and its Content-Type
        often still says PDF.

        Re-requesting a cached URL returns the existing copy without
        fetching; pass force=True to replace it.
        """
        from ..libimport.datasheet_cache import cache_datasheet
        return cache_datasheet(url, mpn=mpn, force=force)

    @mcp.tool()
    async def part_list_cached_datasheets() -> dict[str, Any]:
        """Every datasheet in the local cache, newest first, with the URL,
        SHA-256 and fetch time each was stored with."""
        from ..libimport.datasheet_cache import list_cached
        entries = list_cached()
        return {"ok": True, "count": len(entries), "datasheets": entries}
