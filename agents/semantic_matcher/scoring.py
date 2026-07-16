"""HELIX 3b Semantic Matcher — deterministic scoring module.

Pure functions, zero LLM, zero I/O. This is the "deterministic-first" half of the
3b "deterministic-first with LLM escalation" architecture (spec
helix-poc-agno:spec:semantic-matcher-v1-draft). It scores a single client token
against a category-prefiltered list of baseline candidates across four independent
signal families (name, path, value, layer), then decides whether the match is
strong enough to accept deterministically or must escalate to the LLM path.

It is consumed by:
  - the mutation testbench (tests/semantic_matcher/mutation_testbench/) — NOW
  - the real 3b Agno Step wrapper — LATER (reuses score_candidates verbatim)

Design invariants
-----------------
* Value-veto is HARD: for scalar types, a value disagreement beyond the veto
  threshold forces escalation even if name+path+layer all agree (mitigates
  FM-3b-5 — "confident wrong match on a name collision").
* Signals are computed independently so the testbench can measure whether they
  are actually independent (thesis C). No signal is derived from another.
* Missing signals degrade gracefully: a client token with no path (provenance
  stripped) simply has one fewer family voting; it never crashes.

Token record shape (both client_token and each baseline candidate), see
tests/semantic_matcher/mutation_testbench/mutations/_common.py:
    {name, path|None, category, layer|None, dtcg_type, value}
Value shapes: color -> {components:[r,g,b] 0..1, hex}; number -> scalar;
string -> str.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

SCORING_VERSION = "0.1.0"

# --- default thresholds (conservative: favor escalation) --------------------
DEFAULT_THRESHOLDS: dict[str, float] = {
    "MARGIN_ACCEPT": 0.25,
    "NAME_ACCEPT": 0.85,
    "VALUE_VETO_COLOR_HEX": 8.0,      # RGB-Euclidean distance in 0..255 units
    "VALUE_VETO_DIMENSION_REL": 0.05,  # 5% relative
    "MIN_FAMILIES_AGREE": 2,
}

# aggregate-score weights per signal family (renormalized over available signals)
_WEIGHTS = {"name": 0.40, "value": 0.30, "path": 0.20, "layer": 0.10}

_COLOR_NORM = 441.6729559300637  # sqrt(3 * 255^2), max RGB-Euclidean distance


# --------------------------------------------------------------------------- #
# typed contract
# --------------------------------------------------------------------------- #
class ScoringSignals(BaseModel):
    name_similarity: float               # 0..1, Levenshtein-normalized
    path_overlap: float                  # 0..1, set-Jaccard on slash-tokenized paths
    value_distance: Optional[float]      # 0..1 (0=identical), None if non-scalar/unavailable
    layer_alignment: Optional[bool]      # None if either side lacks a layer signal


class ScoredCandidate(BaseModel):
    baseline_var: str
    signals: ScoringSignals
    aggregate_score: float
    signal_families_agreeing: int        # families whose top pick is this candidate


class ScoringResult(BaseModel):
    top_candidates: list[ScoredCandidate]  # ranked, top-N
    margin_top1_top2: float
    matched_via: Literal["deterministic", "llm_required", "no_candidates"]
    deterministic_confidence: Optional[Literal["authoritative", "high"]] = None
    deterministic_rationale: Optional[str] = None


# --------------------------------------------------------------------------- #
# signal primitives
# --------------------------------------------------------------------------- #
def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def name_similarity(a: str, b: str) -> float:
    """1 - Levenshtein(a,b)/max(len). 1.0 == identical."""
    a = a or ""
    b = b or ""
    if not a and not b:
        return 1.0
    m = max(len(a), len(b))
    return 1.0 - (_levenshtein(a, b) / m) if m else 1.0


def path_overlap(a: Optional[str], b: Optional[str]) -> float:
    """Jaccard similarity of slash-tokenized (lowercased) path segment sets."""
    if not a or not b:
        return 0.0
    sa = {t for t in a.lower().split("/") if t}
    sb = {t for t in b.lower().split("/") if t}
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def _rgb_0_255(value: Any) -> Optional[tuple[float, float, float]]:
    """Extract an (r,g,b) 0..255 triple from a DTCG color value or hex string."""
    if isinstance(value, dict):
        comps = value.get("components")
        if isinstance(comps, list) and len(comps) >= 3:
            return tuple(float(c) * 255.0 for c in comps[:3])  # type: ignore[return-value]
        value = value.get("hex")
    if isinstance(value, str) and value.startswith("#") and len(value) == 7:
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _color_euclidean_0_255(av: Any, bv: Any) -> Optional[float]:
    ra, rb = _rgb_0_255(av), _rgb_0_255(bv)
    if ra is None or rb is None:
        return None
    return (sum((x - y) ** 2 for x, y in zip(ra, rb))) ** 0.5


def value_distance(dtcg_type: Optional[str], av: Any, bv: Any) -> Optional[float]:
    """Normalized 0..1 value distance (0=identical). None => non-scalar/unavailable."""
    t = dtcg_type or ""
    if t == "color":
        d = _color_euclidean_0_255(av, bv)
        return None if d is None else min(1.0, d / _COLOR_NORM)
    if t == "fontWeight":
        a, b = _as_number(av), _as_number(bv)
        if a is None or b is None:
            return None
        return min(1.0, abs(a - b) / 1000.0)
    if t in ("number", "dimension"):
        a, b = _as_number(av), _as_number(bv)
        if a is None or b is None:
            return None
        denom = max(abs(a), abs(b))
        return 0.0 if denom == 0 else min(1.0, abs(a - b) / denom)
    if t in ("string", "fontFamily"):
        if isinstance(av, str) and isinstance(bv, str):
            return 0.0 if av.strip().lower() == bv.strip().lower() else 1.0
        return None
    return None  # typography/shadow/other composites: non-scalar here


def _value_veto_ok(dtcg_type: Optional[str], av: Any, bv: Any, th: dict) -> Optional[bool]:
    """HARD gate. True=within tolerance, False=veto fires, None=not applicable (non-scalar)."""
    t = dtcg_type or ""
    if t == "color":
        d = _color_euclidean_0_255(av, bv)
        return None if d is None else d <= th["VALUE_VETO_COLOR_HEX"]
    if t in ("number", "dimension", "fontWeight"):
        a, b = _as_number(av), _as_number(bv)
        if a is None or b is None:
            return None
        denom = max(abs(a), abs(b))
        rel = 0.0 if denom == 0 else abs(a - b) / denom
        return rel <= th["VALUE_VETO_DIMENSION_REL"]
    if t in ("string", "fontFamily"):
        if isinstance(av, str) and isinstance(bv, str):
            return av.strip().lower() == bv.strip().lower()
        return None
    return None


# --------------------------------------------------------------------------- #
# main entry point
# --------------------------------------------------------------------------- #
def score_candidates(
    client_token: dict,
    baseline_candidates: list[dict],
    thresholds: dict | None = None,
    top_n: int = 5,
) -> ScoringResult:
    """Score one client token against pre-filtered baseline candidates.

    Returns a ScoringResult with ranked candidates and a deterministic-vs-LLM
    verdict. See module docstring for the acceptance gate.
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if not baseline_candidates:
        return ScoringResult(top_candidates=[], margin_top1_top2=0.0,
                             matched_via="no_candidates",
                             deterministic_rationale="no candidates after pre-filter")

    c_name = client_token.get("name") or ""
    c_path = client_token.get("path")
    c_layer = client_token.get("layer")
    c_type = client_token.get("dtcg_type")
    c_value = client_token.get("value")

    # per-candidate signals
    rows: list[dict] = []
    for cand in baseline_candidates:
        ns = name_similarity(c_name, cand.get("name") or "")
        po = path_overlap(c_path, cand.get("path"))
        vd = value_distance(c_type, c_value, cand.get("value"))
        cand_layer = cand.get("layer")
        la = None if (c_layer is None or cand_layer is None) else (c_layer == cand_layer)

        # availability of each family (does it carry a usable signal?)
        avail = {
            "name": True,
            "path": bool(c_path) and bool(cand.get("path")),
            "value": vd is not None,
            "layer": la is not None,
        }
        # per-family score in 0..1
        fam_score = {
            "name": ns,
            "path": po,
            "value": (1.0 - vd) if vd is not None else 0.0,
            "layer": (1.0 if la else 0.0) if la is not None else 0.0,
        }
        # aggregate over AVAILABLE families, renormalized
        wsum = sum(_WEIGHTS[f] for f, ok in avail.items() if ok) or 1.0
        agg = sum(_WEIGHTS[f] * fam_score[f] for f, ok in avail.items() if ok) / wsum

        rows.append({
            "cand": cand,
            "signals": ScoringSignals(name_similarity=ns, path_overlap=po,
                                      value_distance=vd, layer_alignment=la),
            "fam_score": fam_score,
            "avail": avail,
            "aggregate": agg,
        })

    # family voting: each available family votes for its argmax candidate
    families = ("name", "path", "value", "layer")
    family_vote: dict[str, Optional[int]] = {}
    for fam in families:
        idxs = [i for i, r in enumerate(rows) if r["avail"][fam]]
        if not idxs:
            family_vote[fam] = None
            continue
        # argmax of family score; deterministic tie-break by candidate name
        family_vote[fam] = max(
            idxs, key=lambda i: (rows[i]["fam_score"][fam], -_ord_name(rows[i]["cand"]))
        )

    for i, r in enumerate(rows):
        r["families_agreeing"] = sum(1 for fam in families if family_vote[fam] == i)

    # rank by aggregate (tie-break: more families agreeing, then name asc)
    order = sorted(range(len(rows)),
                   key=lambda i: (-rows[i]["aggregate"], -rows[i]["families_agreeing"],
                                  rows[i]["cand"].get("name") or ""))
    ranked = [rows[i] for i in order]

    scored = [
        ScoredCandidate(
            baseline_var=r["cand"].get("name") or "",
            signals=r["signals"],
            aggregate_score=round(r["aggregate"], 6),
            signal_families_agreeing=r["families_agreeing"],
        )
        for r in ranked[:top_n]
    ]

    top1 = ranked[0]
    top2_agg = ranked[1]["aggregate"] if len(ranked) > 1 else 0.0
    margin = round(top1["aggregate"] - top2_agg, 6)

    # --- deterministic acceptance gate --------------------------------------
    veto = _value_veto_ok(c_type, c_value, top1["cand"].get("value"), th)
    gate = {
        "margin_ok": margin >= th["MARGIN_ACCEPT"],
        "name_ok": top1["signals"].name_similarity >= th["NAME_ACCEPT"],
        # scalar value must be within veto tolerance; non-scalar (None) can't accept on value
        "value_ok": veto is True,
        "families_ok": top1["families_agreeing"] >= th["MIN_FAMILIES_AGREE"],
    }
    matched_via: Literal["deterministic", "llm_required", "no_candidates"]
    confidence: Optional[Literal["authoritative", "high"]] = None
    if all(gate.values()):
        matched_via = "deterministic"
        confidence = "authoritative" if margin >= 0.5 else "high"
        rationale = (f"det-accept: name={top1['signals'].name_similarity:.2f}, "
                     f"margin={margin:.2f}, families={top1['families_agreeing']}, value-veto=pass")
    else:
        matched_via = "llm_required"
        failed = [k for k, v in gate.items() if not v]
        veto_note = "value-veto=FIRED" if veto is False else ("value=n/a" if veto is None else "")
        rationale = f"escalate: failed {failed}" + (f"; {veto_note}" if veto_note else "")

    return ScoringResult(
        top_candidates=scored,
        margin_top1_top2=margin,
        matched_via=matched_via,
        deterministic_confidence=confidence,
        deterministic_rationale=rationale,
    )


def _ord_name(cand: dict) -> int:
    """Stable numeric tie-break key from a candidate name (lower name wins argmax ties)."""
    name = cand.get("name") or ""
    return sum(ord(c) for c in name[:8])
