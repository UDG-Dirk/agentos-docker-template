"""Dimension parser: ``Npx`` | ``Nrem`` | ``Nem`` | ``N%`` | bare ``N`` → :class:`DTCGDimensionValue`.

A bare number (e.g. ``"0"`` from ``letter-spacing: 0``) is treated as ``default_unit``
(px by default). Anything non-numeric → ``None`` (caller classifies as UNRESOLVED).
"""
from __future__ import annotations

import re
from typing import Optional, cast

from models import DimensionUnit, DTCGDimensionValue

_DIM_RE = re.compile(r"^(?P<num>-?\d*\.?\d+)\s*(?P<unit>px|rem|em|%)?$", re.IGNORECASE)


def parse_dimension(value: str, default_unit: str = "px") -> Optional[DTCGDimensionValue]:
    m = _DIM_RE.match(value.strip())
    if not m:
        return None
    num = float(m.group("num"))
    unit = (m.group("unit") or default_unit).lower()
    return DTCGDimensionValue(value=num, unit=cast(DimensionUnit, unit))
