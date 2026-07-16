"""Threshold sensitivity sweep for the 3b deterministic gate.

Sweeps MARGIN_ACCEPT x NAME_ACCEPT x VALUE_VETO_COLOR_HEX (6 x 5 x 3 = 90 combos).
For each combo it re-scores every mutation case and records the deterministic
path's behaviour — deterministic_accept_rate vs false_accept_rate — so humans can
pick thresholds against the accept/safety tradeoff. Threshold auto-tuning is NOT
done here (task DO-NOT): this only produces the tradeoff surface.

The sweep is LLM-free by construction: deterministic_accept_rate and
false_accept_rate depend only on the deterministic gate, so no escalation is run
(fast: ~14k score calls in a few seconds).

Run (either works):
    .venv/bin/python -m tests.semantic_matcher.testbench.threshold_sweep
    .venv/bin/python tests/semantic_matcher/testbench/threshold_sweep.py

Writes results/threshold_sweep_<iso>.csv (+ .png if matplotlib is available).
"""
from __future__ import annotations

# allow direct-script invocation (python .../threshold_sweep.py) as well as -m
if __package__ in (None, ""):  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parents[3]))  # repo root
    __package__ = "tests.semantic_matcher.testbench"

import csv
from datetime import datetime, timezone
from pathlib import Path

from agents.semantic_matcher.scoring import (
    DEFAULT_THRESHOLDS,
    _value_veto_ok,
    score_candidates,
)

from .input_sources._common import load_baseline, prefilter_candidates
from .run_testbench import collect_cases

MARGIN_GRID = [0.1, 0.2, 0.25, 0.3, 0.4, 0.5]
NAME_GRID = [0.7, 0.8, 0.85, 0.9, 0.95]
VETO_COLOR_GRID = [4, 8, 12, 20]  # note: 4 values -> 6*5*4 = 120 rows (>= required 90)

_HERE = Path(__file__).resolve().parent
RESULTS_DIR = _HERE / "results"


def _precompute() -> list[dict]:
    """Score each case ONCE. Ranking / aggregate / margin / signals are
    threshold-INDEPENDENT (weights are fixed constants), so we cache the top-1
    facts the gate needs and re-apply only the cheap gate per combo. This turns a
    120x re-score (Levenshtein-heavy, minutes) into 120x trivial gate evals.
    """
    baseline = load_baseline()
    rows = []
    for case in collect_cases():
        ct = case["client_token"]
        cands = prefilter_candidates(ct, baseline)
        res = score_candidates(ct, cands)  # default thresholds; only signals/margin used
        top1 = res.top_candidates[0] if res.top_candidates else None
        top1_cand = next((c for c in cands if c.get("name") == top1.baseline_var), None) if top1 else None
        rows.append({
            "expected": case["expected_baseline_var"],
            "has_candidates": bool(cands),
            "margin": res.margin_top1_top2,
            "top1_name": top1.baseline_var if top1 else None,
            "top1_name_sim": top1.signals.name_similarity if top1 else 0.0,
            "top1_families": top1.signal_families_agreeing if top1 else 0,
            "client_type": ct.get("dtcg_type"),
            "client_value": ct.get("value"),
            "top1_value": top1_cand.get("value") if top1_cand else None,
        })
    return rows


def _gate(c: dict, th: dict) -> tuple[bool, bool]:
    """(deterministic_accepted, correct) for one precomputed case under thresholds th."""
    if not c["has_candidates"]:
        return False, False
    veto_ok = _value_veto_ok(c["client_type"], c["client_value"], c["top1_value"], th) is True
    deterministic = (
        c["margin"] >= th["MARGIN_ACCEPT"]
        and c["top1_name_sim"] >= th["NAME_ACCEPT"]
        and veto_ok
        and c["top1_families"] >= th["MIN_FAMILIES_AGREE"]
    )
    return deterministic, deterministic and (c["top1_name"] == c["expected"])


def _tally(cases: list[dict], th: dict) -> tuple[int, int, int]:
    """(n_deterministic, n_correct, n_wrong) over cases under thresholds th."""
    n_det = n_correct = n_wrong = 0
    for c in cases:
        det, corr = _gate(c, th)
        if det:
            n_det += 1
            n_correct += int(corr)
            n_wrong += int(not corr)
    return n_det, n_correct, n_wrong


def sweep() -> list[dict]:
    cases = _precompute()
    total = len(cases)
    out: list[dict] = []
    for margin in MARGIN_GRID:
        for name in NAME_GRID:
            for veto in VETO_COLOR_GRID:
                th = {**DEFAULT_THRESHOLDS, "MARGIN_ACCEPT": margin,
                      "NAME_ACCEPT": name, "VALUE_VETO_COLOR_HEX": float(veto)}
                n_det, n_correct, n_wrong = _tally(cases, th)
                out.append({
                    "MARGIN_ACCEPT": margin,
                    "NAME_ACCEPT": name,
                    "VALUE_VETO_COLOR_HEX": veto,
                    "n_total": total,
                    "n_deterministic": n_det,
                    "deterministic_accept_rate": round(n_det / total, 4),
                    "false_accept_rate": round(n_wrong / n_det, 4) if n_det else 0.0,
                    "deterministic_correct": n_correct,
                })
    return out


# --------------------------------------------------------------------------- #
# v0.3 extension — the two axes v0.2 held constant (dimension_rel, min_families)
# --------------------------------------------------------------------------- #
ANCHORS = {
    "conservative": {"MARGIN_ACCEPT": 0.25, "NAME_ACCEPT": 0.85, "VALUE_VETO_COLOR_HEX": 8.0,
                     "VALUE_VETO_DIMENSION_REL": 0.05, "MIN_FAMILIES_AGREE": 2},
    "max_yield": {"MARGIN_ACCEPT": 0.1, "NAME_ACCEPT": 0.7, "VALUE_VETO_COLOR_HEX": 20.0,
                  "VALUE_VETO_DIMENSION_REL": 0.05, "MIN_FAMILIES_AGREE": 2},
}
DIM_REL_AXIS = [0.01, 0.02, 0.05, 0.10, 0.20]
MIN_FAM_AXIS = [1, 2, 3, 4]


def _run_axis(cases: list[dict], axis_param: str, axis_values: list) -> list[dict]:
    total = len(cases)
    rows: list[dict] = []
    for anchor_name, anchor in ANCHORS.items():
        for val in axis_values:
            th = {**DEFAULT_THRESHOLDS, **anchor, axis_param: val}
            n_det, n_correct, n_wrong = _tally(cases, th)
            rows.append({
                "sweep_axis": axis_param,
                "anchor": anchor_name,
                "axis_value": val,
                "MARGIN_ACCEPT": th["MARGIN_ACCEPT"], "NAME_ACCEPT": th["NAME_ACCEPT"],
                "VALUE_VETO_COLOR_HEX": th["VALUE_VETO_COLOR_HEX"],
                "VALUE_VETO_DIMENSION_REL": th["VALUE_VETO_DIMENSION_REL"],
                "MIN_FAMILIES_AGREE": th["MIN_FAMILIES_AGREE"],
                "n_total": total, "n_deterministic": n_det,
                "deterministic_accept_rate": round(n_det / total, 4),
                "false_accept_rate": round(n_wrong / n_det, 4) if n_det else 0.0,
                "deterministic_correct": n_correct,
            })
    return rows


def dimension_coverage(cases: list[dict]) -> dict:
    """How many test cases can actually exercise the dimension value-veto axis?"""
    dim_typed = [c for c in cases if c["client_type"] in ("number", "dimension")]
    veto_active = [c for c in dim_typed
                   if isinstance(c["client_value"], (int, float))
                   and isinstance(c["top1_value"], (int, float))]
    return {"dimension_typed_cases_in_set": len(dim_typed),
            "dimension_veto_exercisable_cases": len(veto_active),
            "total_cases": len(cases)}


def sweep_extension() -> dict:
    cases = _precompute()
    return {
        "dimension_rel": _run_axis(cases, "VALUE_VETO_DIMENSION_REL", DIM_REL_AXIS),
        "min_families": _run_axis(cases, "MIN_FAMILIES_AGREE", MIN_FAM_AXIS),
        "coverage": dimension_coverage(cases),
    }


def _maybe_plot(rows: list[dict], png_path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    # accept vs false-accept, coloured by MARGIN_ACCEPT (name fixed at 0.85, veto at 8)
    sub = [r for r in rows if r["NAME_ACCEPT"] == 0.85 and r["VALUE_VETO_COLOR_HEX"] == 8]
    sub.sort(key=lambda r: r["MARGIN_ACCEPT"])
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot([r["MARGIN_ACCEPT"] for r in sub], [r["deterministic_accept_rate"] for r in sub],
            marker="o", label="deterministic_accept_rate")
    ax.plot([r["MARGIN_ACCEPT"] for r in sub], [r["false_accept_rate"] for r in sub],
            marker="s", label="false_accept_rate")
    ax.set_xlabel("MARGIN_ACCEPT (NAME_ACCEPT=0.85, VETO_COLOR_HEX=8)")
    ax.set_ylabel("rate")
    ax.set_title("3b deterministic gate: accept vs false-accept tradeoff")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(png_path, dpi=110)
    plt.close(fig)
    return True


def main_extension() -> None:
    """v0.3 per-axis extension: sweep the two axes v0.2 held constant."""
    ext = sweep_extension()
    rows = ext["dimension_rel"] + ext["min_families"]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ").replace(":", "").replace("-", "")
    csv_path = RESULTS_DIR / f"threshold_sweep_ext_{ts}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    cov = ext["coverage"]
    print(f"[ext] {len(rows)} combos -> {csv_path}")
    print(f"[ext] dimension coverage: {cov}")
    for axis_name, axis_rows in (("VALUE_VETO_DIMENSION_REL", ext["dimension_rel"]),
                                 ("MIN_FAMILIES_AGREE", ext["min_families"])):
        print(f"\n[ext] === {axis_name} ===")
        for r in axis_rows:
            print(f"  anchor={r['anchor']:12s} {axis_name}={r['axis_value']:<5} "
                  f"det_accept={r['deterministic_accept_rate']} false_accept={r['false_accept_rate']} "
                  f"(n_det={r['n_deterministic']})")
        # activity check: does det_accept_rate vary along the axis within an anchor?
        for anchor in ("conservative", "max_yield"):
            vals = [r["deterministic_accept_rate"] for r in axis_rows if r["anchor"] == anchor]
            fa = [r["false_accept_rate"] for r in axis_rows if r["anchor"] == anchor]
            active = (len(set(vals)) > 1) or (len(set(fa)) > 1)
            print(f"  [{anchor}] parameter_active={active} "
                  f"(det_accept varies={len(set(vals))>1}, false_accept varies={len(set(fa))>1})")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="3b threshold sweep")
    ap.add_argument("--mode", choices=["grid", "extension"], default="grid",
                    help="grid = full 120-combo parent sweep (default); "
                         "extension = v0.3 per-axis sweep of dimension_rel + min_families")
    args = ap.parse_args()
    if args.mode == "extension":
        main_extension()
        return
    rows = sweep()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ").replace(":", "").replace("-", "")
    csv_path = RESULTS_DIR / f"threshold_sweep_{ts}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    png_path = RESULTS_DIR / f"threshold_sweep_{ts}.png"
    plotted = _maybe_plot(rows, png_path)

    print(f"[sweep] {len(rows)} combos -> {csv_path}")
    if plotted:
        print(f"[sweep] plot -> {png_path}")
    else:
        print("[sweep] matplotlib unavailable -> CSV only (plot is optional)")
    # headline: best accept rate at zero false-accept
    safe = [r for r in rows if r["false_accept_rate"] == 0.0]
    if safe:
        best = max(safe, key=lambda r: r["deterministic_accept_rate"])
        print(f"[sweep] best zero-false-accept combo: margin={best['MARGIN_ACCEPT']} "
              f"name={best['NAME_ACCEPT']} veto={best['VALUE_VETO_COLOR_HEX']} "
              f"-> accept_rate={best['deterministic_accept_rate']}")
    span = (min(r['deterministic_accept_rate'] for r in rows),
            max(r['deterministic_accept_rate'] for r in rows))
    print(f"[sweep] deterministic_accept_rate spans {span[0]}..{span[1]} across the grid")


if __name__ == "__main__":
    main()
