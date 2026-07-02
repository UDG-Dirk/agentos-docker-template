"""Path derivation — two naming worlds → one DTCG dotted path.

* Enrichment slash-path is authoritative when present: ``a/b/c`` → ``a.b.c``.
* Otherwise parse the CSS-var name: ``--a-b-c`` → ``a.b.c``.

Minimal group-name normalisation reconciles the only divergence seen in evidence:
enrichment pluralises the colour group (``colors/...``) while CSS-vars use the
singular (``--color-...``). Both collapse to ``color`` so the two worlds land on
one tree branch. Kept deliberately small — extend only when new evidence appears.
"""
from __future__ import annotations

_GROUP_NORMALIZE = {"colors": "color"}


def _seg(s: str) -> str:
    return s.strip().lower()


def _normalize_first(segs: list[str]) -> list[str]:
    if segs:
        segs[0] = _GROUP_NORMALIZE.get(segs[0], segs[0])
    return segs


def enrichment_path_to_dotted(slash_path: str) -> str:
    segs = _normalize_first([_seg(s) for s in slash_path.split("/") if s.strip()])
    return ".".join(segs)


def css_var_to_dotted(name: str) -> str:
    raw = name.strip()
    if raw.startswith("--"):
        raw = raw[2:]
    segs = _normalize_first([_seg(s) for s in raw.split("-") if s.strip()])
    return ".".join(segs)
