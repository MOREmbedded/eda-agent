# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The PCB property names the bridge reads, checked before a call is sent.

A name the script does not know used to reach Altium, where the refusal
itself stopped the script loop ("Wrong number of params") and the bridge
had to be restarted. The names are checked here first, so a typo or a
member the bridge does not read (BoundingRectangle) is answered with the
names that are, and never reaches the script.

PCB_PROPERTIES is the script's own list (IsKnownPCBProperty,
ExtraPCBPropertyOwner and the generated reads in PCBGeneric.pas); a test
fails when the two drift apart.
"""
from __future__ import annotations

import difflib
from typing import Any, Optional

PCB_PROPERTIES: frozenset[str] = frozenset({
    "Address", "AllowGlobalEdit", "ArcApproximation", "ArcPourMode",
    "ArrowLength", "ArrowLineWidth", "ArrowPosition", "ArrowSize",
    "AutoGenerateName", "AvoidObsticles", "BarCodeFontName",
    "BarCodeFullHeight", "BarCodeFullWidth", "BarCodeInverted",
    "BarCodeKind", "BarCodeMinWidth", "BarCodeRenderMode",
    "BarCodeShowText", "BarCodeXMargin", "BarCodeYMargin", "BodyOpacity3D",
    "Bold", "BorderSpaceType", "BorderWidth", "BotXSize", "BotYSize",
    "BoundsBottom", "BoundsBottom_mm", "BoundsLeft", "BoundsLeft_mm",
    "BoundsRight", "BoundsRight_mm", "BoundsTop", "BoundsTop_mm",
    "ChannelOffset", "ClipAcuteCorners", "Comment", "Comment.Text",
    "CommentAutoPosition", "Component", "ComponentDesignator",
    "ComponentHeight", "ComponentKind", "ConvertedString",
    "DefaultPCB3DModel", "Descriptor", "DescriptorString", "Designator",
    "Designator.Text", "DetailString", "DrawAsPreview", "DrawDeadCopper",
    "DrawRemovedIslands", "DrawRemovedNecks", "DRCError", "DrillType",
    "Enabled", "Enabled_Direct", "Enabled_vComponent",
    "Enabled_vCoordinate", "Enabled_vDimension", "Enabled_vNet",
    "Enabled_vPolygon", "EnableDraw", "EnablePartSwapping",
    "EnablePinSwapping", "EndAngle", "ExpandOutline", "ExtensionLineWidth",
    "ExtensionOffset", "ExtensionPickGap", "FlippedOnLayer", "FontID",
    "FontName", "FootprintDescription", "Grid", "GroupNum", "Handle",
    "Height", "HoleNegativeTolerance", "HolePositiveTolerance",
    "HoleRotation", "HoleSize", "HoleSize_mm", "HoleType", "HoleWidth",
    "Identifier", "IgnoreViolations", "InBoard", "InComponent",
    "InCoordinate", "InDimension", "InNet", "InPolygon", "Inverted",
    "InvertedTTTextBorder", "InvRectHeight", "InvRectWidth",
    "IsAssyTestpoint_Bottom", "IsAssyTestpoint_Top", "IsBGA", "IsComment",
    "IsCounterHole", "IsDesignator", "IsElectricalPrim", "IsHidden",
    "IsKeepout", "IsKeepoutForCopper", "IsKeepoutForSMDPad",
    "IsKeepoutForTHPad", "IsKeepoutForTrack", "IsKeepoutForVia",
    "IslandAreaThreshold", "IsPreRoute", "IsRedundant", "IsTenting",
    "IsTenting_Bottom", "IsTenting_Top", "IsTestpoint_Bottom",
    "IsTestpoint_Top", "IsVirtualPin", "Italic", "JumperID",
    "JumpersVisible", "Kind", "Layer", "Layer1", "Layer2", "Locked",
    "LockStrings", "MinTrack", "MirrorFlag", "MiscFlag1", "MiscFlag2",
    "MiscFlag3", "MitreCorners", "Mode", "Moveable", "Multiline",
    "MultilineTextAutoPosition", "MultilineTextHeight",
    "MultilineTextWidth", "Name", "NameAutoPosition", "NeckWidthThreshold",
    "Net", "Net.Name", "ObjectId", "ObjectIDString", "OverallHeight",
    "OverrideColor", "OwnerPart_ID", "PackageSpecificHash",
    "PadCacheRobotFlag", "PadName", "PasteMaskEnabled",
    "PasteMaskExpansion", "PasteMaskExpansionMode", "PasteMaskPercent",
    "PasteMaskUsePercent", "Pattern", "Plated", "PolygonOutline",
    "PolygonType", "PolyHatchStyle", "Poured", "PourIndex", "PourOver",
    "PowerPlaneClearance", "PowerPlaneReliefExpansion", "PrimitiveLock",
    "Radius", "Radius_mm", "RegionKind", "ReliefAirGap",
    "ReliefConductorWidth", "ReliefEntries", "RemoveDead",
    "RemoveIslandsByArea", "RemoveNarrowNecks", "Rotation", "Selected",
    "Size", "SizeBottom", "SizeMid", "SizeTop", "SolderMaskBottomExpansion",
    "SolderMaskBottomExpansion_mm", "SolderMaskExpansion",
    "SolderMaskExpansion_mm", "SolderMaskExpansionMode",
    "SolderMaskFromHoleEdge", "SolderMaskManual", "SolderMaskSeparate",
    "SourceComponentLibrary", "SourceDescription", "SourceDesignator",
    "SourceFootprintLibrary", "SourceHierarchicalPath",
    "SourceLibReference", "SourceUniqueId", "StandoffHeight", "StartAngle",
    "StrokeWidth", "SwapID_Pad", "SwapID_Part", "SwappedPadName",
    "TearDrop", "Text", "TextDimensionUnit", "TextFormat", "TextGap",
    "TextHeight", "TextKind", "TextLineWidth", "TextPosition",
    "TextPrecision", "TextPrefix", "TextSuffix", "Texture",
    "TextureRotation", "TextWidth", "TopShape", "TopShapeName", "TopXSize",
    "TopXSize_mm", "TopYSize", "TopYSize_mm", "TrackSize",
    "TTFInvertedTextJustify", "TTFOffsetFromInvertedRect",
    "UnderlyingString", "UnionIndex", "UniqueID", "Used",
    "UseInvertedRectangle", "UseOctagons", "UserRouted",
    "UseSeparateMaskExpansions", "UseTTFonts", "Width", "Width_mm", "X",
    "X1", "X1_mm", "X2", "X2_mm", "X_mm", "XCenter", "XCenter_mm", "Y",
    "Y1", "Y1_mm", "Y2", "Y2_mm", "Y_mm", "YCenter", "YCenter_mm",
})

#: Members of Altium's API the bridge does not read as such, and the
#: names that give the same answer.
ALIASES: dict[str, tuple[str, ...]] = {
    "boundingrectangle": ("BoundsLeft", "BoundsBottom", "BoundsRight", "BoundsTop"),
    "boundingrect": ("BoundsLeft", "BoundsBottom", "BoundsRight", "BoundsTop"),
    "linewidth": ("Width",),
    "centerx": ("XCenter",),
    "centery": ("YCenter",),
    "sweepangle": ("StartAngle", "EndAngle"),
    "netname": ("Net",),
}

#: Names the script answers on some object types only (by the plain type
#: word, see type_word); on any other it answers an empty string. From
#: GetPCBProperty's branches.
ONLY_ON: dict[str, frozenset[str]] = {
    **{name: frozenset({"arc"}) for name in (
        "Radius", "Radius_mm", "StartAngle", "EndAngle",
        "XCenter", "XCenter_mm", "YCenter", "YCenter_mm")},
    "HoleSize": frozenset({"pad", "via"}),
    "TopXSize": frozenset({"pad"}),
    "TopYSize": frozenset({"pad"}),
}


def type_word(object_type: str) -> str:
    """``eArcObject``, ``ArcObject`` and ``arc`` alike as ``arc``."""
    s = str(object_type or "").strip()
    if s[:1] == "e" and s[1:2].isupper():
        s = s[1:]
    s = s.lower()
    return s[:-len("object")] if s.endswith("object") and s != "object" else s


def split_names(properties: str) -> list[str]:
    """The names of a comma-separated property list, blanks dropped."""
    return [p.strip() for p in str(properties or "").split(",") if p.strip()]


def suggestions(name: str) -> list[str]:
    """Known names that ``name`` was probably meant to be."""
    alias = ALIASES.get(name.replace("_", "").lower())
    if alias:
        return list(alias)
    folded = {p.lower(): p for p in PCB_PROPERTIES}
    if name.lower() in folded:
        return [folded[name.lower()]]
    return difflib.get_close_matches(name, sorted(PCB_PROPERTIES), n=3, cutoff=0.75)


def check_properties(properties: str, object_type: str = "") -> tuple[Optional[dict[str, Any]], list[str]]:
    """``(refusal, warnings)`` for a property list asked of ``object_type``.

    The refusal is a reply to give instead of calling the bridge, or None
    when every name is known. The warnings name the known properties the
    script answers empty on this type.
    """
    names = split_names(properties)
    unknown = [n for n in names if n not in PCB_PROPERTIES]
    if unknown:
        hints = {n: suggestions(n) for n in unknown}
        return ({
            "ok": False,
            "error": "UNKNOWN_PROPERTY",
            "reason": "Not a PCB property the bridge reads: " + ", ".join(unknown)
                      + ". Nothing was sent to Altium.",
            "unknown": unknown,
            "did_you_mean": {n: h for n, h in hints.items() if h},
            "valid_properties": sorted(PCB_PROPERTIES, key=str.lower),
        }, [])
    warnings = []
    kind = type_word(object_type)
    for n in names:
        types = ONLY_ON.get(n)
        if types and kind and kind not in types:
            warnings.append(f"{n} is read on {' and '.join(sorted(types))} objects "
                            f"only; it comes back empty on {object_type}.")
    return None, warnings
