"""CSS ``box-shadow`` parser — parenthesis-aware.

The hard case (from delta analysis): FocusRing =
``box-shadow: 0px 0px 0px 4px rgba(211,128,11,1), 0px 0px 0px 2px rgba(255,255,255,1)``
A naive ``split(",")`` shatters the ``rgba(...)`` commas → 8 fragments instead of
2 layers. :func:`split_top_level` only splits on separators at paren-depth 0.

Per-layer grammar handled: ``<offsetX> <offsetY> [<blur>] [<spread>] <color>``
(color may appear in any position; remaining numeric tokens are taken in order as
offsetX, offsetY, blur, spread, with missing trailing values defaulting to 0px).

Returns a single :class:`DTCGShadowValue` for one layer, a ``list`` for multiple,
or ``None`` if any layer is unparseable (caller → UNRESOLVED).
"""
from __future__ import annotations

from typing import Optional, Union

from models import DTCGDimensionValue, DTCGShadowValue
from parsers.color import parse_color
from parsers.dimension import parse_dimension


def split_top_level(s: str, sep: str) -> list[str]:
    """Split ``s`` on ``sep`` characters that are NOT inside parentheses."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def _parse_layer(layer: str) -> Optional[DTCGShadowValue]:
    color = None
    dims: list[DTCGDimensionValue] = []
    for tok in split_top_level(layer, " "):
        if color is None:
            c = parse_color(tok)
            if c is not None:
                color = c
                continue
        d = parse_dimension(tok)
        if d is not None:
            dims.append(d)
    if color is None or len(dims) < 2:
        return None
    zero = DTCGDimensionValue(value=0, unit="px")
    return DTCGShadowValue(
        color=color,
        offsetX=dims[0],
        offsetY=dims[1],
        blur=dims[2] if len(dims) >= 3 else zero,
        spread=dims[3] if len(dims) >= 4 else zero,
    )


def parse_box_shadow(
    value: str,
) -> Optional[Union[DTCGShadowValue, list[DTCGShadowValue]]]:
    v = value.strip()
    if v.lower().startswith("box-shadow:"):
        v = v.split(":", 1)[1].strip()
    layers = split_top_level(v, ",")
    if not layers:
        return None
    shadows: list[DTCGShadowValue] = []
    for layer in layers:
        parsed = _parse_layer(layer)
        if parsed is None:
            return None
        shadows.append(parsed)
    return shadows[0] if len(shadows) == 1 else shadows
