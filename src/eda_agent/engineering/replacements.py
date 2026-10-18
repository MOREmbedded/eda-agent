# SPDX-License-Identifier: Apache-2.0
"""Judge candidate replacements for a part, on the evidence available.

:mod:`eda_agent.engineering.part_match` compares two parametric maps.
This adds the other three things a substitution turns on -- lifecycle,
stock, and whether the footprint still fits -- and orders the
candidates.

Kept pure, and separate from the tool that calls it, so the judging can
be tested without a credential or a network: the tool fetches, this
decides, and the two can be wrong independently.

WHAT IS NOT CHECKED, said here rather than left to be discovered. No
product-change notice, no errata, no manufacturer announcement. No
distributor API serves any of them -- not Digi-Key's, not Mouser's, not
Nexar's -- so nothing here reads them, and every result carries
``not_checked`` saying so. A PCN is the single most common reason a
part that passes every check below is still the wrong choice this
month, and a reader who assumed silence meant "clean" would be worse
off than one who never ran this at all.

FOOTPRINT FIT is package-NAME agreement, which is evidence and not
proof. A catalogue states "SOIC-8", never the land pattern, so two
parts agreeing here agree on a name that usually implies the same pads
and sometimes does not (SOT-23-5 and SOT-23-5 thermal; the several
things called SOIC-8). The geometric check exists and is good --
``lib_audit_footprint_vs_datasheet`` compares real pad geometry against
a transcribed land pattern -- but it needs the candidate's datasheet
read by a human or an agent, which is a step after this one, not part
of it. Each candidate therefore carries the datasheet URL and says
which check it has and has not had.

ORDERING is mechanical and stated: fewest blocking findings first, then
fewest unreviewed, then by MPN. It is not a relevance score and not a
quality ranking, for the same reason ``PartHit.sort_key`` refuses to be
one.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional

from eda_agent.engineering.part_match import (
    compare_parameters,
    summarise,
)

__all__ = [
    "NOT_CHECKED",
    "lifecycle_state",
    "rank_replacements",
]

#: Lifecycle vocabularies, folded to four states. Catalogues word this
#: freely ("Not Recommended for New Designs", "NRND", "Obsolete",
#: "Inactive", "Last Time Buy"), and the differences between the
#: spellings carry no information that the state does not.
#:
#: Matched as substrings of the lowercased text, FIRST MATCH WINNING,
#: so the phrases that contain another state's word come first: "not
#: recommended for new designs" contains neither "active" nor
#: "obsolete", but "not for new designs" and "end of life" both have to
#: beat the bare words that follow them.
_LIFECYCLE: tuple[tuple[str, str], ...] = (
    ("not recommended", "nrnd"),
    ("nrnd", "nrnd"),
    ("not for new design", "nrnd"),
    ("last time buy", "obsolete"),
    ("end of life", "obsolete"),
    ("end-of-life", "obsolete"),
    ("eol", "obsolete"),
    ("obsolete", "obsolete"),
    ("discontinued", "obsolete"),
    ("inactive", "obsolete"),
    ("not manufactured", "obsolete"),
    ("preliminary", "preproduction"),
    ("pre-production", "preproduction"),
    ("pre production", "preproduction"),
    ("new product", "active"),
    ("production", "active"),
    ("active", "active"),
)

#: Worse to better. A candidate below the original is a downgrade, and
#: "obsolete" blocks outright: a replacement you cannot buy next year
#: is not a replacement.
_LIFECYCLE_RANK = {"obsolete": 0, "nrnd": 1, "preproduction": 2,
                   "unknown": 3, "active": 4}


def lifecycle_state(text: Any) -> str:
    """One of ``active`` / ``nrnd`` / ``preproduction`` / ``obsolete`` /
    ``unknown``.

    ``unknown`` for anything unrecognised, including an empty string,
    and it is NOT treated as active anywhere below. A source that does
    not state a lifecycle has not told you the part is fine.
    """
    if not isinstance(text, str) or not text.strip():
        return "unknown"
    lowered = text.strip().lower()
    for phrase, state in _LIFECYCLE:
        if phrase in lowered:
            return state
    return "unknown"


def _package_evidence(comparison: dict[str, Any]) -> dict[str, Any]:
    """What the comparison says about the package, pulled out by name.

    The package is the one attribute that speaks to whether the board
    needs re-laying out, so it is reported in its own right rather than
    left as one line among thirty.
    """
    for record in comparison.get("attributes") or []:
        if record.get("canonical") == "package":
            return {
                "original": record.get("original", ""),
                "candidate": record.get("candidate", ""),
                "verdict": record.get("verdict", ""),
                "checked": "package name only, NOT pad geometry",
            }
    return {"original": "", "candidate": "", "verdict": "missing",
            "checked": "neither part states a package"}


def _stock_note(available: Any) -> tuple[Optional[int], str]:
    """``(units, note)``. ``None`` units means nobody said, which is not
    the same as none in stock and must not read as it."""
    if isinstance(available, bool) or not isinstance(available, (int, float)):
        return None, "no source stated a stock figure"
    units = int(available)
    if units <= 0:
        return units, "no stock at the sources queried"
    return units, ""


def _candidate_report(original: dict[str, Any], candidate: dict[str, Any],
                      *, rules: Optional[dict[str, str]],
                      rel_tol: float) -> dict[str, Any]:
    comparison = compare_parameters(
        original.get("parameters") or {},
        candidate.get("parameters") or {},
        labels=original.get("parameter_names") or {},
        rules=rules, rel_tol=rel_tol)

    original_state = lifecycle_state(original.get("lifecycle"))
    candidate_state = lifecycle_state(candidate.get("lifecycle"))
    units, stock_note = _stock_note(candidate.get("total_available"))

    blocking = list(comparison.get("blocking") or [])
    unreviewed = list(comparison.get("unreviewed") or [])
    concerns: list[str] = []

    if candidate_state == "obsolete":
        blocking.append("lifecycle")
        concerns.append(
            f"lifecycle is {candidate.get('lifecycle') or candidate_state!r}; "
            f"a part you cannot buy next year does not replace one you can")
    elif candidate_state == "unknown":
        unreviewed.append("lifecycle")
        concerns.append("no source stated a lifecycle, which is not the "
                        "same as it being active")
    elif (_LIFECYCLE_RANK[candidate_state]
          < _LIFECYCLE_RANK.get(original_state, 3)):
        concerns.append(
            f"lifecycle is {candidate_state} where the original is "
            f"{original_state}, so this trades one problem for a smaller "
            f"one rather than solving it")

    if stock_note:
        concerns.append(stock_note)

    return {
        "mpn": candidate.get("mpn", ""),
        "manufacturer": candidate.get("manufacturer", ""),
        "provider": candidate.get("provider", ""),
        "datasheet": candidate.get("datasheet", ""),
        "lifecycle": candidate.get("lifecycle", ""),
        "lifecycle_state": candidate_state,
        "stock": units,
        "package": _package_evidence(comparison),
        "blocking": blocking,
        "unreviewed": unreviewed,
        "concerns": concerns,
        "parameters": comparison,
        "summary": summarise(comparison),
        "status": "blocked" if blocking else "candidate",
    }


#: Stated in every result. Not a footnote: a reader who takes silence
#: for a clean bill of health is worse off than one who never ran this.
NOT_CHECKED = (
    "product-change notices (PCNs) -- no distributor API serves them",
    "errata and manufacturer advisories -- same reason",
    "pad geometry -- the package NAME was compared, not the land "
    "pattern; run lib_audit_footprint_vs_datasheet against the "
    "candidate's datasheet to check the pads",
    "anything the catalogues do not publish, which routinely includes "
    "the parameter that actually decides a substitution",
)


def rank_replacements(
        original: dict[str, Any],
        candidates: Iterable[dict[str, Any]],
        *,
        rules: Optional[dict[str, str]] = None,
        rel_tol: float = 0.0,
) -> dict[str, Any]:
    """Judge each candidate against the original and order them.

    ``original`` and each candidate are flat dicts in the shape the
    distributor providers publish: ``mpn``, ``manufacturer``,
    ``datasheet``, ``parameters``, ``parameter_names``, ``lifecycle``,
    ``total_available``.

    Returns ``{"original", "candidates", "not_checked", "note"}``.
    ``candidates`` is ordered by fewest blocking findings, then fewest
    unreviewed, then MPN -- a stated mechanical order, not a score.

    A candidate identical to the original by MPN is dropped: a part is
    not its own replacement, and leaving it in makes a perfect-looking
    first row that means nothing.
    """
    original = original if isinstance(original, dict) else {}
    original_mpn = str(original.get("mpn") or "").strip().lower()

    # Not `candidates or ()`: a non-iterable (a bare int from a
    # mis-shaped caller) would raise there instead of degrading, and
    # this is fed from remote catalogue data whose shape is not this
    # code's to assume.
    if isinstance(candidates, dict) or not isinstance(candidates, Iterable):
        candidates = ()

    reports = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        mpn = str(candidate.get("mpn") or "").strip()
        if not mpn or mpn.lower() == original_mpn:
            continue
        reports.append(_candidate_report(original, candidate,
                                         rules=rules, rel_tol=rel_tol))

    reports.sort(key=lambda r: (len(r["blocking"]), len(r["unreviewed"]),
                                r["mpn"].lower()))

    usable = [r for r in reports if r["status"] == "candidate"]
    return {
        "original": {
            "mpn": original.get("mpn", ""),
            "manufacturer": original.get("manufacturer", ""),
            "lifecycle": original.get("lifecycle", ""),
            "lifecycle_state": lifecycle_state(original.get("lifecycle")),
            "parameter_count": len(original.get("parameters") or {}),
        },
        "candidates": reports,
        "not_checked": list(NOT_CHECKED),
        "note": (
            f"{len(usable)} of {len(reports)} candidates have no blocking "
            f"finding. Ordered by fewest blocking findings, then fewest "
            f"unreviewed, then MPN -- a mechanical order, not a ranking. "
            f"Nothing here is an approval: see not_checked."),
    }
