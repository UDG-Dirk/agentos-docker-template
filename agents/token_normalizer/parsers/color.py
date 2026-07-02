"""Color parsers: hex and ``rgba()`` → :class:`DTCGColorValue` (sRGB, 0..1).

Rules (per spec §3 Value Normalization):
* hex ``#rgb`` / ``#rrggbb`` → sRGB components; alpha omitted (implicitly opaque).
* ``rgba()`` / ``rgb()`` → sRGB components; alpha preserved (the source stated it,
  even when 1.0 — shadow layers rely on this).
* components rounded to 4 dp for stable, diff-friendly golden output.
* anything else → ``None`` (caller classifies as UNRESOLVED).
"""
from __future__ import annotations

import re
from typing import Optional

from models import DTCGColorValue

_HEX_RE = re.compile(r"^#(?P<h>[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGBA_RE = re.compile(r"^rgba?\(\s*(?P<body>[^)]*)\)$", re.IGNORECASE)
_PRECISION = 4


def _r(x: float) -> float:
    return round(x, _PRECISION)


def parse_hex(value: str) -> Optional[DTCGColorValue]:
    m = _HEX_RE.match(value.strip())
    if not m:
        return None
    h = m.group("h").lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r = int(h[0:2], 16) / 255
    g = int(h[2:4], 16) / 255
    b = int(h[4:6], 16) / 255
    return DTCGColorValue(components=[_r(r), _r(g), _r(b)], hex=f"#{h}")


def parse_rgba(value: str) -> Optional[DTCGColorValue]:
    m = _RGBA_RE.match(value.strip())
    if not m:
        return None
    parts = [p.strip() for p in m.group("body").split(",") if p.strip() != ""]
    if len(parts) < 3:
        return None
    try:
        r, g, b = float(parts[0]), float(parts[1]), float(parts[2])
        alpha = float(parts[3]) if len(parts) >= 4 else None
    except ValueError:
        return None
    hex_mirror = "#%02x%02x%02x" % (
        max(0, min(255, int(round(r)))),
        max(0, min(255, int(round(g)))),
        max(0, min(255, int(round(b)))),
    )
    return DTCGColorValue(
        components=[_r(r / 255), _r(g / 255), _r(b / 255)],
        alpha=alpha,
        hex=hex_mirror,
    )


def parse_color(value: str) -> Optional[DTCGColorValue]:
    v = value.strip()
    if v.startswith("#"):
        return parse_hex(v)
    if v.lower().startswith("rgb"):
        return parse_rgba(v)
    return None
