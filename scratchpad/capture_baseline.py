"""Phase 0 capture for the 3b mutation testbench.

Produces two fixtures from the live helix-code clone @220a327:
  1. baseline_snapshot_220a327.json      — EXACTLY what Baseline Reader emits
                                            (deliverable #1; names/categories only)
  2. baseline_tokens_enriched_220a327.json — enriched token catalog WITH values,
                                            derived by walking the same DTCG leaves.
                                            Needed because BaselineInventory carries
                                            no values, but value-veto scoring (thesis
                                            B / FM-3b-5) and the mutation generators
                                            require them. Flagged as a deviation.

Enriched record (the testbench lingua franca for BOTH baseline candidates and
synthesized client tokens):
  {name, path, category, layer, dtcg_type, value}
    name     : --helix-* css var (Baseline Reader's _css_var_name)
    path     : slash-joined lowercase segments (for path_overlap Jaccard)
    category : path segment 0
    layer    : primitive | semantic | unknown (Baseline Reader's _classify_layer)
    dtcg_type: DTCG $type at the leaf
    value    : raw DTCG $value (hex str for color; scalar for number/dimension/...)
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agents.baseline_reader.reader import (  # noqa: E402
    _classify_layer,
    _css_var_name,
    _walk_dtcg,
    read_baseline,
)

HELIX = Path("/home/dirk/opencode/workbench/agno-setup/helix-code")
TOKEN_ROOT = HELIX / "packages" / "tokens" / "tokens"
OUT_DIR = REPO / "tests" / "semantic_matcher" / "fixtures"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def build_enriched() -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    for f in sorted(TOKEN_ROOT.rglob("*.tokens.json")):
        rel = f.relative_to(HELIX).as_posix()
        layer = _classify_layer(rel)
        data = json.loads(f.read_text(encoding="utf-8"))
        leaves: list[tuple[list[str], dict]] = []
        _walk_dtcg(data, [], leaves)
        for path_segs, leaf in leaves:
            if not path_segs:
                continue
            name = _css_var_name(path_segs)
            if name in seen:  # de-dupe across breakpoint files (same var name)
                continue
            seen.add(name)
            records.append({
                "name": name,
                "path": "/".join(s.lower() for s in path_segs),
                "category": path_segs[0].lower(),
                "layer": layer,
                "dtcg_type": leaf.get("$type"),
                "value": leaf.get("$value"),
            })
    return sorted(records, key=lambda r: r["name"])


def main() -> None:
    # 1) BaselineInventory (deliverable #1) — frozen timestamp for determinism
    from datetime import datetime, timezone
    inv = read_baseline(str(HELIX), _now=datetime(2026, 7, 15, tzinfo=timezone.utc))
    snap_path = OUT_DIR / "baseline_snapshot_220a327.json"
    snap_path.write_text(json.dumps(inv, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # 2) enriched catalog
    enriched = build_enriched()
    enr_path = OUT_DIR / "baseline_tokens_enriched_220a327.json"
    enr_path.write_text(json.dumps(enriched, indent=2) + "\n", encoding="utf-8")

    # ---- summary for design ----
    print(f"[snapshot] {snap_path}  ({snap_path.stat().st_size} bytes)")
    print(f"  commit={inv['meta']['commit']} branch={inv['meta']['branch']}")
    print(f"  css_var_names={len(inv['tokens']['css_var_names'])}")
    print(f"  categories={list(inv['tokens']['categories'].keys())}")
    print(f"  cem_present={inv['components']['cem_present']}")
    print(f"\n[enriched] {enr_path}  ({enr_path.stat().st_size} bytes)  records={len(enriched)}")
    by_type = Counter(r["dtcg_type"] for r in enriched)
    by_cat = Counter(r["category"] for r in enriched)
    by_layer = Counter(r["layer"] for r in enriched)
    print(f"  by dtcg_type: {dict(by_type)}")
    print(f"  by category : {dict(by_cat)}")
    print(f"  by layer    : {dict(by_layer)}")
    print("\n[value shapes by type] (one example each):")
    ex: dict[str, dict] = {}
    for r in enriched:
        ex.setdefault(r["dtcg_type"], r)
    for t, r in ex.items():
        print(f"  {t:12s} name={r['name']}  path={r['path']}  value={r['value']!r}")
    print("\n[sample records]")
    for r in enriched[:8]:
        print(f"  {json.dumps(r)}")


if __name__ == "__main__":
    main()
