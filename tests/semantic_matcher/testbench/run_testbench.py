"""3b Testbench — harness runner.

Loads the baseline, runs a selected INPUT SOURCE (mutations | golden_sets |
regression) through the deterministic scoring module + (mock) LLM escalation + an
inline post-validator, and emits per-token records plus aggregate metrics
answering the six architectural theses (A–F).

Run (either works):
    .venv/bin/python -m tests.semantic_matcher.testbench.run_testbench
    .venv/bin/python tests/semantic_matcher/testbench/run_testbench.py [--input-source=mutations]

Input sources live under ``input_sources/`` and are selected with --input-source
(default: mutations, which reproduces the parent-task behaviour). Writes
results/testbench_<iso>.json and results/testbench_<iso>.md. Mock LLM by default;
real LLM only if HELIX_TESTBENCH_USE_REAL_LLM=1 (see llm_path).
"""
from __future__ import annotations

# allow direct-script invocation (python .../run_testbench.py) as well as -m
if __package__ in (None, ""):  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parents[3]))  # repo root
    __package__ = "tests.semantic_matcher.testbench"

import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agents.semantic_matcher.scoring import score_candidates

from .input_sources import EMPTY_HINT, MUTATION_CLASSES, REGISTRY, get_source
from .input_sources._common import load_baseline, prefilter_candidates, type_family
from .llm_path import resolve_llm

_HERE = Path(__file__).resolve().parent
RESULTS_DIR = _HERE / "results"


# --------------------------------------------------------------------------- #
# post-validator
# --------------------------------------------------------------------------- #
def _is_alias(v: Any) -> bool:
    return isinstance(v, str) and v.strip().startswith("{")


def _value_shape_consistent(dtcg_type: Optional[str], value: Any) -> bool:
    """True unless the value's concrete shape CONTRADICTS the declared type.

    Alias references ("{...}") are opaque -> never a contradiction. This is what
    catches cross_category mutants (a color-dict declared as number, or a scalar
    declared as color) without punishing legit alias-valued tokens.
    """
    t = dtcg_type or ""
    if _is_alias(value):
        return True
    if t == "color":
        if isinstance(value, dict) and ("components" in value or "hex" in value):
            return True
        if isinstance(value, str) and value.startswith("#"):
            return True
        return False
    if t in ("number", "dimension", "fontWeight"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t in ("string", "fontFamily"):
        return isinstance(value, str)
    if t == "typography":
        return isinstance(value, dict) and "fontFamily" in value
    return True  # unknown declared type -> don't block


# --------------------------------------------------------------------------- #
# Layer 2 — name-vs-declared-type coherence (spec v3.2, Surface A)
# --------------------------------------------------------------------------- #
# The name/path category segment implies a TYPE FAMILY. If the token's declared
# type family contradicts what its own name says it is ("the token lies about its
# own type"), it is incoherent and routed to unmapped BEFORE the LLM is invoked —
# so structural rejection never depends on LLM abstention (Layer 1) alone.
#
# Family is used (not the exact dtcg type) so it lines up with the harness'
# ``type_family`` and the 3 dtcg types the helix baseline actually carries
# (color / number / string). Mapping derived empirically from the @220a327
# baseline. A category LABEL (from the name body or the path) is normalised to a
# lowercase, '-'-joined string and prefix-matched against this table. Ordered
# LONGEST-FIRST so compound labels (component-dimensions, stroke-weight,
# font-family) win over their roots. Note: names use '-' separators while paths
# use '/' and '_' (e.g. name '--helix-component-dimensions-*' has path
# 'component_dimensions/*'); normalisation unifies both.
_HELIX_PREFIX = "--helix-"
_CATEGORY_FAMILY_PREFIXES = [
    ("component-colors", "color"),
    ("component-dimensions", "numeric"),
    ("stroke-weight", "numeric"),
    ("font-family", "string"),
    ("font", "numeric"),        # font-size / -weight / -lineheight / -letterspacing / -paragraph
    ("colors", "color"),
    ("color", "color"),
    ("dimension", "numeric"),
    ("spacing", "numeric"),
    ("radius", "numeric"),
    ("stroke", "numeric"),
    ("shadow", "numeric"),
    ("size", "numeric"),
]


def _normalize_label_text(text: Any) -> str:
    return str(text).strip().lower().replace("_", "-").replace("/", "-") if text else ""


def _family_from_text(text: Any) -> Optional[str]:
    """Type family implied by a category label (name body or path), or None."""
    t = _normalize_label_text(text)
    if not t:
        return None
    for prefix, fam in _CATEGORY_FAMILY_PREFIXES:
        if t == prefix or t.startswith(prefix + "-"):
            return fam
    return None


def _name_body(name: Optional[str]) -> Optional[str]:
    if not name or not name.startswith(_HELIX_PREFIX):
        return None
    return name[len(_HELIX_PREFIX):]


def _name_category(name: Optional[str]) -> Optional[str]:
    """Raw name category label for reporting (first segment, or first two for the
    two ambiguous roots component-* / font-*)."""
    body = _name_body(name)
    segs = [s for s in body.split("-") if s] if body else []
    if not segs:
        return None
    if segs[0] in ("component", "font") and len(segs) > 1:
        return f"{segs[0]}-{segs[1]}"
    return segs[0]


def check_name_type_coherence(client_token: dict) -> Optional[dict]:
    """Layer 2. Compare the client name's implied type family vs its declared type.

    Returns a coherence_check dict (verdict "coherent"/"incoherent") or None when
    the check cannot be applied confidently (SOFT-PASS). We only FLAG when the
    implied family is TRUSTWORTHY and contradicts the declared type. Trust rules:
      * declared type family unknown/other (e.g. synthesized 'typography') -> soft-pass;
      * name is a --helix- token:
          - if a path is present it must POSITIVELY corroborate the name (its family
            is recognised AND equals the name's) — otherwise soft-pass. This is what
            keeps name_collision (deliberately mislabelled NAME, intact PATH of a
            different family, e.g. name '--helix-color-token-15' with path
            'component_dimensions/...') from being false-flagged, while
            cross_category / scenario-10 (name AND path intact, only the declared
            TYPE lies) keep name==path and ARE flagged;
          - if no path at all, trust the name alone (spec fallback);
      * name not a --helix- token: fall back to the path family if recognised;
      * neither usable -> soft-pass.
    """
    declared_family = type_family(client_token.get("dtcg_type"))
    if declared_family == "other":
        return None  # unknown declared type -> cannot judge -> soft-pass

    name = client_token.get("name")
    path = client_token.get("path")
    name_family = _family_from_text(_name_body(name))
    path_present = bool(path and str(path).strip())
    path_family = _family_from_text(path) if path_present else None

    if name_family is not None:
        if path_present:
            if path_family is None or path_family != name_family:
                return None  # path cannot corroborate the name -> untrusted -> soft-pass
            implied_family, source = name_family, "name+path"
        else:
            implied_family, source = name_family, "name"  # no path -> trust the name
        category = _name_category(name)
    elif path_family is not None:
        implied_family, source = path_family, "path"
        seg0 = [s for s in _normalize_label_text(path).split("-") if s]
        category = seg0[0] if seg0 else None
    else:
        return None  # neither name nor path usable -> soft-pass

    verdict = "coherent" if implied_family == declared_family else "incoherent"
    return {
        "name_category": category,
        "name_category_family": implied_family,
        "declared_type": client_token.get("dtcg_type"),
        "declared_family": declared_family,
        "source": source,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# Layer 3 — enriched baseline value/type check (spec v3.2, Surface B)
# --------------------------------------------------------------------------- #
def build_baseline_enrichment(baseline: list[dict]) -> dict[str, dict]:
    """Resolve each baseline token's value to a concrete (non-alias) value.

    The @220a327 baseline stores DTCG alias references ("{a.b.c}") verbatim in
    ``value``; 111/510 tokens are aliases (55 alias-of-alias). This resolves them
    transitively by following {a.b.c} -> path a/b/c, so the post-validator can
    inspect a resolved value instead of treating the alias as opaque.

    Returns name -> {"dtcg_type", "value" (resolved or None), "unresolved_alias"}.
    ``unresolved_alias`` is True when the chain hits a missing target or a cycle;
    callers must NOT trust an unresolved value (fall back to skipping the check).
    """
    by_path = {r["path"]: r for r in baseline if r.get("path")}
    by_name = {r["name"]: r for r in baseline}

    def _resolve(rec: dict, seen: set[str]) -> tuple[Any, bool]:
        val = rec.get("value")
        if not _is_alias(val):
            return val, False
        ref = val.strip().strip("{}").strip()
        tgt_path = ref.replace(".", "/")
        tgt = by_path.get(tgt_path) or by_name.get(_HELIX_PREFIX + tgt_path.replace("/", "-"))
        if tgt is None or tgt["name"] in seen:
            return None, True  # missing target or cycle -> unresolved
        return _resolve(tgt, seen | {tgt["name"]})

    out: dict[str, dict] = {}
    for r in baseline:
        value, unresolved = _resolve(r, {r["name"]})
        out[r["name"]] = {
            "dtcg_type": r.get("dtcg_type"),
            "value": value,
            "unresolved_alias": unresolved,
        }
    return out


def post_validate(client_token: dict, chosen: Optional[dict], rationale: Optional[str],
                  baseline_names: set[str], enrichment: Optional[dict] = None) -> dict:
    """Vocabulary + category alignment + value/type consistency + rationale checks.

    Layer 3 (Surface B): when a baseline token is chosen, also check its ENRICHED
    (alias-resolved) value against its declared type — closing the "alias opacity"
    bypass where an alias-valued baseline token trivially passed the value/type
    check because the raw value was opaque. Unresolved aliases fall back to a skip
    (an unresolved value is never trusted).
    """
    checks: dict[str, Any] = {
        "vocabulary": chosen is None or (chosen.get("name") in baseline_names),
        "category_alignment": chosen is None or (
            type_family(chosen.get("dtcg_type")) == type_family(client_token.get("dtcg_type"))
        ),
        "value_type_consistent": _value_shape_consistent(
            client_token.get("dtcg_type"), client_token.get("value")),
        "rationale_nonempty": bool(rationale and rationale.strip()),
    }

    # Layer 3 — enriched baseline value/type consistency
    if chosen is not None and enrichment is not None:
        ev = enrichment.get(chosen.get("name"))
        if ev is None or ev.get("unresolved_alias"):
            checks["baseline_value_type_consistent"] = "skipped"  # no/untrusted value
        else:
            checks["baseline_value_type_consistent"] = _value_shape_consistent(
                ev.get("dtcg_type"), ev.get("value"))
    else:
        checks["baseline_value_type_consistent"] = "skipped"

    passed = all(v is True or v == "skipped" for v in checks.values())
    return {"passed": passed, "checks": checks}


# --------------------------------------------------------------------------- #
# per-case execution
# --------------------------------------------------------------------------- #
def run_case(case: dict, baseline: list[dict], baseline_by_name: dict, baseline_names: set[str],
             llm, thresholds: dict | None = None, enrichment: dict | None = None) -> dict:
    client = case["client_token"]
    expected = case["expected_baseline_var"]

    candidates = prefilter_candidates(client, baseline)
    result = score_candidates(client, candidates, thresholds=thresholds)

    # Layer 2 (spec v3.2, Surface A): name-vs-declared-type coherence. Fires BEFORE
    # the LLM so incoherent tokens ("the token lies about its own type") never reach
    # it — precedence over Layer 1 abstention, and it also covers the deterministic
    # path defensively.
    coherence = check_name_type_coherence(client)
    incoherent = bool(coherence and coherence["verdict"] == "incoherent")

    llm_invoked = False
    llm_abstained = False
    llm_rationale = None
    confidence = None
    rationale_for_pv = result.deterministic_rationale

    if incoherent:
        actual = None
        confidence = "unresolved"
    elif result.matched_via == "deterministic":
        actual = result.top_candidates[0].baseline_var
        confidence = result.deterministic_confidence
    elif result.matched_via == "llm_required":
        llm_invoked = True
        llm_res = llm.match(client, candidates,
                            expected_baseline_var=expected, scoring_result=result)
        llm_rationale = llm_res.rationale
        rationale_for_pv = llm_res.rationale
        if llm_res.outcome == "unmappable":
            llm_abstained = True
            actual = None
            confidence = "unresolved"
        else:
            actual = llm_res.baseline_var
            confidence = llm_res.confidence
    else:  # no_candidates
        actual = None
        confidence = "unresolved"

    if incoherent:
        # Layer 2 pre-empts the mapping; the post-validator is not reached.
        pv = {"passed": False,
              "checks": {"vocabulary": "skipped", "category_alignment": "skipped",
                         "value_type_consistent": "skipped", "rationale_nonempty": "skipped",
                         "baseline_value_type_consistent": "skipped",
                         "name_type_coherence": "incoherent"}}
        final_actual = None
    elif llm_abstained:
        # spec v3.1: skip vocabulary/category checks on abstention (no candidate to
        # validate); require only a non-empty rationale.
        rationale_ok = bool(rationale_for_pv and rationale_for_pv.strip())
        pv = {"passed": rationale_ok,
              "checks": {"vocabulary": "skipped", "category_alignment": "skipped",
                         "value_type_consistent": "skipped", "rationale_nonempty": rationale_ok,
                         "baseline_value_type_consistent": "skipped"}}
        final_actual = None
    else:
        chosen = baseline_by_name.get(actual) if actual else None
        pv = post_validate(client, chosen, rationale_for_pv, baseline_names, enrichment)
        # post-validator can VETO a mapping to unmapped (a real 3b safeguard)
        final_actual = actual if pv["passed"] else None
        if not pv["passed"]:
            confidence = "unresolved"

    # why is this token unmapped (if it is)?
    if final_actual is not None:
        unmapped_reason = None
    elif incoherent:
        unmapped_reason = "name_type_incoherent"
    elif llm_abstained:
        unmapped_reason = "llm_abstained"
    elif result.matched_via == "no_candidates":
        unmapped_reason = "no_candidates"
    else:
        unmapped_reason = "post_validator_rejected"

    top1 = result.top_candidates[0] if result.top_candidates else None
    sig = top1.signals if top1 else None
    return {
        "mutation_class": case["mutation_class"],
        "client_name": client.get("name"),
        "expected_baseline_var": expected,
        "raw_actual": actual,
        "actual_baseline_var": final_actual,
        "matched_via": result.matched_via,
        "confidence": confidence,
        "deterministic_accepted": result.matched_via == "deterministic" and not incoherent,
        "llm_invoked": llm_invoked,
        "llm_abstained": llm_abstained,
        "llm_rationale": llm_rationale,
        "name_type_incoherent": incoherent,
        "coherence_check": coherence,
        "unmapped_reason": unmapped_reason,
        "post_validator_passed": pv["passed"],
        "post_validator_checks": pv["checks"],
        "correct": final_actual == expected,
        "margin": result.margin_top1_top2,
        "signals": None if sig is None else {
            "name_similarity": sig.name_similarity,
            "path_overlap": sig.path_overlap,
            "value_distance": sig.value_distance,
            "layer_alignment": sig.layer_alignment,
        },
        "note": case.get("note", ""),
    }


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


def compute_metrics(records: list[dict]) -> dict:
    total = len(records)
    det = [r for r in records if r["deterministic_accepted"]]
    det_wrong = [r for r in det if not r["correct"]]
    correct = [r for r in records if r["correct"]]
    exp_unmapped = [r for r in records if r["expected_baseline_var"] is None]
    act_unmapped = [r for r in records if r["actual_baseline_var"] is None]
    unmapped_tp = [r for r in act_unmapped if r["expected_baseline_var"] is None]
    llm = [r for r in records if r["llm_invoked"]]
    llm_correct = [r for r in llm if r["correct"]]
    abstained = [r for r in records if r.get("llm_abstained")]
    incoherent = [r for r in records if r.get("unmapped_reason") == "name_type_incoherent"]
    unmapped_by_reason = dict(
        Counter(r["unmapped_reason"] for r in records if r.get("unmapped_reason")))
    return {
        "total": total,
        "deterministic_accept_rate": _rate(len(det), total),
        "false_accept_rate_on_deterministic": _rate(len(det_wrong), len(det)),
        "correct_mapping_rate": _rate(len(correct), total),
        "unmapped_precision": _rate(len(unmapped_tp), len(act_unmapped)),
        "unmapped_recall": _rate(len(unmapped_tp), len(exp_unmapped)),
        "llm_invocation_rate": _rate(len(llm), total),
        "llm_correctness_on_escalated": _rate(len(llm_correct), len(llm)),
        "llm_abstention_count": len(abstained),
        "llm_abstention_rate": _rate(len(abstained), total),
        "name_type_incoherent_count": len(incoherent),
        "name_type_incoherent_rate": _rate(len(incoherent), total),
        "unmapped_by_reason": unmapped_by_reason,
    }


def confidence_bucket_metrics(records: list[dict]) -> dict:
    buckets: dict[str, dict] = {}
    for r in records:
        b = r["confidence"] or "unresolved"
        d = buckets.setdefault(b, {"n": 0, "correct": 0})
        d["n"] += 1
        d["correct"] += int(r["correct"])
    return {b: {"n": d["n"], "correctness": _rate(d["correct"], d["n"])}
            for b, d in sorted(buckets.items())}


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if vx == 0 or vy == 0:
        return None
    return round(cov / (vx * vy), 4)


def signal_correlation_matrix(records: list[dict]) -> dict:
    """4x4 Pearson across signal families, pairwise-complete (thesis C)."""
    fams = ["name_similarity", "path_overlap", "value_distance", "layer_alignment"]

    def series(r: dict, f: str) -> Optional[float]:
        s = r.get("signals")
        if not s or s.get(f) is None:
            return None
        v = s[f]
        return float(v) if not isinstance(v, bool) else (1.0 if v else 0.0)

    matrix: dict[str, dict[str, Optional[float]]] = {}
    for a in fams:
        matrix[a] = {}
        for b in fams:
            pairs = [(series(r, a), series(r, b)) for r in records]
            pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
            matrix[a][b] = 1.0 if a == b and pairs else _pearson(
                [x for x, _ in pairs], [y for _, y in pairs])
    return matrix


def margin_histogram(records: list[dict], bin_size: float = 0.05) -> dict:
    bins: dict[str, int] = {}
    for r in records:
        m = max(0.0, min(1.0, r["margin"]))
        lo = min(int(m / bin_size), int(1.0 / bin_size) - 1)
        key = f"{lo * bin_size:.2f}-{(lo + 1) * bin_size:.2f}"
        bins[key] = bins.get(key, 0) + 1
    return dict(sorted(bins.items()))


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def build_report(records: list[dict], llm_name: str, thresholds: dict) -> dict:
    by_class: dict[str, list[dict]] = {}
    for r in records:
        by_class.setdefault(r["mutation_class"], []).append(r)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "llm_path": llm_name,
        "thresholds": thresholds,
        "overall": compute_metrics(records),
        "per_class": {c: compute_metrics(rs) for c, rs in sorted(by_class.items())},
        "confidence_buckets": confidence_bucket_metrics(records),
        "signal_correlation_matrix": signal_correlation_matrix(records),
        "margin_histogram": margin_histogram(records),
        "n_records": len(records),
    }


def _fmt(v: Any) -> str:
    return "n/a" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))


def render_markdown(report: dict, records: list[dict]) -> str:
    o = report["overall"]
    L = [
        "# 3b Mutation Testbench — Results",
        "",
        f"- generated: `{report['generated_at']}`  ·  LLM path: **{report['llm_path']}**  "
        f"·  cases: **{report['n_records']}**",
        "",
        "## Summary (overall)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| deterministic accept rate (A) | {_fmt(o['deterministic_accept_rate'])} |",
        f"| false-accept rate on deterministic (B / FM-3b-5) | {_fmt(o['false_accept_rate_on_deterministic'])} |",
        f"| correct mapping rate | {_fmt(o['correct_mapping_rate'])} |",
        f"| unmapped precision | {_fmt(o['unmapped_precision'])} |",
        f"| unmapped recall | {_fmt(o['unmapped_recall'])} |",
        f"| LLM invocation rate (D) | {_fmt(o['llm_invocation_rate'])} |",
        f"| LLM correctness on escalated (D) | {_fmt(o['llm_correctness_on_escalated'])} |",
        f"| LLM abstention count / rate (Layer 1) | {o['llm_abstention_count']} / {_fmt(o['llm_abstention_rate'])} |",
        f"| name/type incoherent count / rate (Layer 2) | {o['name_type_incoherent_count']} / {_fmt(o['name_type_incoherent_rate'])} |",
        f"| unmapped by reason | {o['unmapped_by_reason']} |",
        "",
        "## Per mutation class",
        "",
        "| class | n | det_accept | false_accept_det | correct | unmapped_prec | unmapped_rec | llm_rate | llm_correct |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for c, m in report["per_class"].items():
        L.append(
            f"| {c} | {m['total']} | {_fmt(m['deterministic_accept_rate'])} | "
            f"{_fmt(m['false_accept_rate_on_deterministic'])} | {_fmt(m['correct_mapping_rate'])} | "
            f"{_fmt(m['unmapped_precision'])} | {_fmt(m['unmapped_recall'])} | "
            f"{_fmt(m['llm_invocation_rate'])} | {_fmt(m['llm_correctness_on_escalated'])} |")

    L += ["", "## Confidence buckets (F)", "", "| bucket | n | correctness |", "|---|--:|--:|"]
    for b, d in report["confidence_buckets"].items():
        L.append(f"| {b} | {d['n']} | {_fmt(d['correctness'])} |")

    L += ["", "## Signal correlation matrix (C — are the 4 families independent?)", "",
          "| | name | path | value | layer |", "|---|--:|--:|--:|--:|"]
    fams = ["name_similarity", "path_overlap", "value_distance", "layer_alignment"]
    short = {"name_similarity": "name", "path_overlap": "path",
             "value_distance": "value", "layer_alignment": "layer"}
    mtx = report["signal_correlation_matrix"]
    for a in fams:
        row = " | ".join(_fmt(mtx[a][b]) for b in fams)
        L.append(f"| {short[a]} | {row} |")

    L += ["", "## Margin histogram (top1−top2 aggregate, bin 0.05)", "",
          "| bin | count |", "|---|--:|"]
    for k, v in report["margin_histogram"].items():
        L.append(f"| {k} | {v} |")

    L += ["", "## Reading guide",
          "- **A** deterministic accept rate: how often the deterministic path is confident enough to skip the LLM.",
          "- **B** false-accept on deterministic: the safety-critical number — deterministic accepts that were WRONG (target ~0).",
          "- **C** correlation matrix: high off-diagonal = redundant signals; near-0 = independent (the design assumption).",
          "- **D** LLM path: invocation rate × correctness on the escalated subset (does escalation earn its cost).",
          "- **F** confidence buckets: is 'authoritative' actually more correct than 'high'/'medium' (calibration).",
          ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def collect_cases(input_source: str = "mutations") -> list[dict]:
    """Flatten the selected input source's TestCases into the harness's working
    record shape (mutation_class / client_token / expected_baseline_var / note).

    The dict shape is preserved from the parent task so the harness internals and
    existing tests are unchanged; only the SOURCE of the cases is now pluggable.
    """
    src = get_source(input_source)
    cases: list[dict] = []
    for tc in src.iter_test_cases():
        meta = tc.metadata or {}
        cases.append({
            "mutation_class": meta.get("mutation_class", input_source),
            "client_token": tc.client_token,
            "expected_baseline_var": tc.expected_baseline_var,
            "note": meta.get("note", ""),
        })
    return cases


def run_records(thresholds: dict | None = None, input_source: str = "mutations") -> list[dict]:
    """Per-case records for the selected input source (no file writes). Exposed so
    tests/analysis can inspect record-level outcomes without re-implementing the loop."""
    baseline = load_baseline()
    baseline_by_name = {r["name"]: r for r in baseline}
    baseline_names = set(baseline_by_name)
    enrichment = build_baseline_enrichment(baseline)
    llm = resolve_llm()
    return [run_case(c, baseline, baseline_by_name, baseline_names, llm, thresholds, enrichment)
            for c in collect_cases(input_source)]


def run(thresholds: dict | None = None, write: bool = True,
        input_source: str = "mutations") -> dict:
    llm_name = resolve_llm().name
    records = run_records(thresholds, input_source)
    report = build_report(records, llm_name, {**({} if thresholds is None else thresholds)})
    report["input_source"] = input_source

    if write and records:  # never write an empty run
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = report["generated_at"].replace(":", "").replace("-", "")
        (RESULTS_DIR / f"testbench_{ts}.json").write_text(
            json.dumps({"report": report, "records": records}, indent=2, default=str) + "\n")
        (RESULTS_DIR / f"testbench_{ts}.md").write_text(render_markdown(report, records) + "\n")
        report["_written"] = ts
    return report


def main() -> None:
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Run the 3b testbench over an input source.")
    ap.add_argument("--input-source", default="mutations", choices=sorted(REGISTRY),
                    help="which input source to run (default: mutations)")
    args = ap.parse_args()
    src_name = args.input_source

    t0 = time.time()
    report = run(input_source=src_name)
    dt = time.time() - t0
    if report["n_records"] == 0:
        print(EMPTY_HINT.get(src_name, f"No cases configured for input source '{src_name}'."))
        return
    o = report["overall"]
    print(f"[testbench] {report['n_records']} cases from '{src_name}' via "
          f"'{report['llm_path']}' LLM in {dt:.2f}s")
    print(f"  deterministic_accept_rate = {o['deterministic_accept_rate']}")
    print(f"  false_accept_rate_on_deterministic = {o['false_accept_rate_on_deterministic']}")
    print(f"  correct_mapping_rate = {o['correct_mapping_rate']}")
    print(f"  unmapped precision/recall = {o['unmapped_precision']}/{o['unmapped_recall']}")
    print(f"  llm_invocation_rate = {o['llm_invocation_rate']}  llm_correct = {o['llm_correctness_on_escalated']}")
    print("  per-class correct_mapping_rate:")
    for c, m in report["per_class"].items():
        print(f"    {c:22s} n={m['total']:3d} det={m['deterministic_accept_rate']} "
              f"false_accept={m['false_accept_rate_on_deterministic']} correct={m['correct_mapping_rate']}")
    print(f"  wrote results/testbench_{report.get('_written')}.json + .md")


if __name__ == "__main__":
    main()
