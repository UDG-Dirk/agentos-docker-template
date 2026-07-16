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


def post_validate(client_token: dict, chosen: Optional[dict], rationale: Optional[str],
                  baseline_names: set[str]) -> dict:
    """Vocabulary + category alignment + value/type consistency + rationale checks."""
    checks = {
        "vocabulary": chosen is None or (chosen.get("name") in baseline_names),
        "category_alignment": chosen is None or (
            type_family(chosen.get("dtcg_type")) == type_family(client_token.get("dtcg_type"))
        ),
        "value_type_consistent": _value_shape_consistent(
            client_token.get("dtcg_type"), client_token.get("value")),
        "rationale_nonempty": bool(rationale and rationale.strip()),
    }
    return {"passed": all(checks.values()), "checks": checks}


# --------------------------------------------------------------------------- #
# per-case execution
# --------------------------------------------------------------------------- #
def run_case(case: dict, baseline: list[dict], baseline_by_name: dict, baseline_names: set[str],
             llm, thresholds: dict | None = None) -> dict:
    client = case["client_token"]
    expected = case["expected_baseline_var"]

    candidates = prefilter_candidates(client, baseline)
    result = score_candidates(client, candidates, thresholds=thresholds)

    llm_invoked = False
    llm_abstained = False
    llm_rationale = None
    confidence = None
    rationale_for_pv = result.deterministic_rationale

    if result.matched_via == "deterministic":
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

    if llm_abstained:
        # spec v3.1: skip vocabulary/category checks on abstention (no candidate to
        # validate); require only a non-empty rationale.
        rationale_ok = bool(rationale_for_pv and rationale_for_pv.strip())
        pv = {"passed": rationale_ok,
              "checks": {"vocabulary": "skipped", "category_alignment": "skipped",
                         "value_type_consistent": "skipped", "rationale_nonempty": rationale_ok}}
        final_actual = None
    else:
        chosen = baseline_by_name.get(actual) if actual else None
        pv = post_validate(client, chosen, rationale_for_pv, baseline_names)
        # post-validator can VETO a mapping to unmapped (a real 3b safeguard)
        final_actual = actual if pv["passed"] else None
        if not pv["passed"]:
            confidence = "unresolved"

    # why is this token unmapped (if it is)?
    if final_actual is not None:
        unmapped_reason = None
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
        "deterministic_accepted": result.matched_via == "deterministic",
        "llm_invoked": llm_invoked,
        "llm_abstained": llm_abstained,
        "llm_rationale": llm_rationale,
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
    llm = resolve_llm()
    return [run_case(c, baseline, baseline_by_name, baseline_names, llm, thresholds)
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
