"""Typography composite parser.

Input form (semicolon-delimited ``key: value`` pairs, from the Figma Extractor)::

    font-family: roboto; font-weight: 900; font-size: 20px; line-height: 1.4em

→ :class:`DTCGTypographyValue`. Handles mixed units (``px`` vs ``em``) and a
missing ``letter-spacing`` (the ``heading-xs-strong`` case). If a required field
(font-family / font-weight / font-size / line-height) is missing or unparseable,
returns ``None`` so the caller classifies the token as UNRESOLVED rather than
emitting a malformed composite.
"""
from __future__ import annotations

from typing import Optional

from models import DTCGFontFamilyValue, DTCGTypographyValue
from parsers.dimension import parse_dimension

# CSS-wide keywords that are NOT real font families. A token whose value is one
# of these is not a usable family → route to UNRESOLVED rather than emit a bogus
# fontFamily (addendum-parser-scope check #4). Match is case-insensitive.
_NON_FAMILY_KEYWORDS = {"normal", "inherit", "initial", "unset", "none", "revert"}


def parse_font_family(value: str) -> Optional[DTCGFontFamilyValue]:
    """Parse a font-family value. Rejects CSS-wide keywords (→ ``None``) and
    preserves multi-word families verbatim (e.g. ``"dm sans"`` is not split)."""
    v = value.strip()
    if not v or v.lower() in _NON_FAMILY_KEYWORDS:
        return None
    return DTCGFontFamilyValue(value=v)


def _key_values(composite: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in composite.split(";"):
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        out[key.strip().lower()] = val.strip()
    return out


def parse_typography(value: str) -> Optional[DTCGTypographyValue]:
    kv = _key_values(value)
    family = kv.get("font-family")
    weight = kv.get("font-weight")
    size = kv.get("font-size")
    line = kv.get("line-height")
    spacing = kv.get("letter-spacing")

    if family is None or weight is None or size is None or line is None:
        return None

    font_size = parse_dimension(size)
    line_height = parse_dimension(line)
    if font_size is None or line_height is None:
        return None

    font_weight: object = int(weight) if weight.isdigit() else weight
    letter_spacing = parse_dimension(spacing) if spacing is not None else None

    return DTCGTypographyValue(
        fontFamily=family,
        fontWeight=font_weight,  # type: ignore[arg-type]
        fontSize=font_size,
        lineHeight=line_height,
        letterSpacing=letter_spacing,
    )
