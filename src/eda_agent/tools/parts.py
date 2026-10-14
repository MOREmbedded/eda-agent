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


def register_parts_tools(mcp):
    """Register the provider-neutral part tools with the MCP server."""

    @mcp.tool()
    async def part_search(
        query: str = "",
        limit_per_provider: int = 20,
    ) -> dict[str, Any]:
        """Search EVERY enabled part provider and merge the results.

        No provider is a default and none is preferred: all are queried
        equally, hits carry the provider that found them, and the merged
        order is alphabetical by provider then part. That ordering is
        NOT relevance, so do not read the first hit as the best one.

        Each provider reports its own status. A provider that cannot
        answer says why (endpoint withdrawn, software missing) instead
        of returning nothing, because "unavailable" and "no such part"
        are different answers and only one means stop looking.

        Call with an empty query to list the providers and what they are,
        including who operates them.

        TWO KINDS OF SOURCE, merged but never conflated. A `library`
        provider yields GEOMETRY: a symbol or footprint you can place. A
        `catalogue` provider (the distributors and aggregators) yields
        IDENTITY: the manufacturer part number, the datasheet, what is
        in stock, and NO geometry at all. Every hit carries its `kind`,
        because finding out that a distributor hit has no symbol after
        choosing the part is the expensive way to learn it.

        The two answer different questions rather than ranking against
        each other: a catalogue tells you WHICH part to use and hands
        you the datasheet every check here measures against, and a
        library tells you whether you can draw it.

        NOTE on maturity="offline": in this catalog that means "needs no
        Altium", which is true here. It does NOT mean "needs nothing":
        the remote providers want the internet. With no network the
        search still succeeds on whatever local providers are enabled
        (kicad_local reads libraries already on disk) and reports the
        remote ones as unavailable, so a degraded answer is still a
        useful one.

        DATASHEET DISCIPLINE: a hit is a lead, not a verified part. The
        `provenance` and `license` fields say what the provider claims
        about where its geometry came from, and a blank means unknown,
        not permissive. Audit any imported footprint against the
        manufacturer land pattern with
        ``lib_audit_footprint_vs_datasheet`` before trusting it.

        A hit may carry an `extra` dict with provider-specific data not
        shared across sources (e.g. Nexar's stock, pricing, lifecycle
        status and similar parts); absent when the provider has none.

        Args:
            query: substring matched against part name, manufacturer and
                keywords. Empty lists the providers instead.
            limit_per_provider: cap per source, so one large registry
                cannot crowd out the others.

        Returns:
            ``{"ok", "count", "providers": {name: status}, "hits": [...],
            "by_mpn": [...]}`` or, for an empty query,
            ``{"ok", "providers": [...]}``.

            ``by_mpn`` correlates the same part across sources, so you
            can see which providers carry it and what each states about
            provenance and license. Cosmetic spelling differences fold
            together; WILDCARD part numbers do not, because KiCad's
            ``...C8Tx`` is a family placeholder rather than a spelling
            of ``...C8T6``, and merging them would assert an equivalence
            that is not safe to assume.
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

        Pass the ``ref`` straight through: it is ``provider:part_id``,
        so the source travels with the id and the merged results behave
        like one catalogue. ``part_search`` puts a ``ref`` on every hit.

        The source is still explicit, never inferred. Nothing here picks
        a provider for you: a bare id with no ``provider`` and no
        recognised prefix is refused rather than guessed at, because
        guessing is how one source quietly becomes the answer to
        everything. ``provider`` may still be given separately, and wins
        over a prefix if both are present.

        Set ``download_dir`` to also write the provider's library files
        there, when it offers any. Off by default because it writes to
        disk, and a fetch should not do that unasked.

        Downloaded files are checked against the KiCad installed on this
        machine: a registry may publish a NEWER s-expression format than
        the local KiCad can open (observed live, format 20260206 against
        KiCad 10.0.1's 20251024, where the symbol parser refuses the file
        outright). Any such file comes back with a ``*_warning`` entry
        rather than looking like a clean download.

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
