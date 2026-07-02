# Token Normalizer — HELIX UC2 Workflow Step 2

Deterministic Python transformer (zero LLM, zero tokens, sub-second) that consumes
the Figma Extractor's `FigmaExtractionResult` and produces a **W3C DTCG 2025.10**
compliant token tree plus a normalization quality report.

The `token_tree` is the **foundation layer of the derived design system (the SSOT)**.
Downstream it feeds Style Dictionary (deterministic transforms → CSS custom
properties) and the future Semantic Matcher agent (LLM reasoning about design intent).

**Status:** Deployed to prod — AgentOS on Coolify, as **Workflow Step 2** of `helix-figma-extractor`
(commit `1ea648b`). Wired as `Step(executor=normalize_step_executor, requires_output_review=True)`;
Step 1 (extract) flows straight into it and the single HITL gate sits here. Live-verified against a real
prod extraction: `in=74 out=74 | authoritative=0 high=40 medium=34 unresolved=0`, with `token_tree` +
`normalization_report` + `component_token_map` in the run output and 93 component paths all resolving.
(The DEV reference run below, `run_sequential_003`, differs because extraction is non-deterministic
run-to-run — see `shared-results:verify-prod-normalizer-output`. The normalizer itself is deterministic.)

## I/O

| | |
|---|---|
| **Input** | `FigmaExtractionResult` (from `agents/figma_extractor/models.py`) — tokens + components + enrichment metadata |
| **Output** | `NormalizedTokens` (`models.py`) — `token_tree` (dict → `.tokens.json`), `normalization_report`, `component_token_map` |
| **Entry point** | `normalizer.normalize_tokens(extraction) -> NormalizedTokens` |
| **Workflow** | `workflow_step.py` — `Step(executor=fn, requires_output_review=True)` (HITL gate) |

## Pipeline (7 phases)

1. **Type Assignment** — `enrichment_type` → DTCG type (AUTHORITATIVE); else value-pattern + name-convention inference (HIGH if both agree, else MEDIUM); else UNRESOLVED.
2. **Path Derivation** — enrichment slash-path → dotted path (authoritative); else CSS-var name parsed → dotted path. `colors/*` normalises to the `color` group so both naming worlds converge.
3+4. **Value Normalization & Composite Decomposition** — modular per-type parsers in `parsers/` (color, dimension, typography, shadow, paths). Each is a pure, individually testable function.
5. **Tree Assembly** — nested dict; every leaf carries `$value`, `$type`, and `$extensions.de.msqdx.helix` provenance.
6. **Report Generation** — coverage, confidence/type distribution, duplicate value groups, composites, collisions, typos, full per-token audit trail.
7. **HITL Review Gate** — Workflow-level (`requires_output_review=True`), not in the function. Reviewer inspects the report before advancing.

## Run

```bash
VENV=~/opencode/workbench/agno-setup/poc-agno-template/.venv
cd ~/opencode/workbench/helix-poc-agno

# Normalize the reference run → writes runs/design.tokens.json + report + full output
$VENV/bin/python agents/token_normalizer/normalizer.py \
  --input agents/figma_extractor/runs/run_sequential_003.json

# Tests (39 automated + 1 golden regression)
$VENV/bin/python -m pytest tests/test_token_normalizer.py \
  --output=agents/figma_extractor/runs/run_sequential_003.json -q

# Workflow registration + HITL smoke check (needs template .env / db)
$VENV/bin/dotenv run -- $VENV/bin/python agents/token_normalizer/workflow_step.py --check
```

## Reference run results (`run_sequential_003.json`)

```
in=100  out=100  | authoritative=74  high=26  medium=0  unresolved=0
types: color 52, dimension 35, fontFamily 2, fontWeight 4, typography 5, shadow 2
composites decomposed: 7 (5 typography + 2 shadow)
duplicate value groups: 21 (semantic aliases preserved — e.g. #1971c2 ×6, 24px ×6, 4px ×6)
path collisions: 0 | fallback warnings: 0 | typos flagged: 9
components mapped: Button 14, Input 15, Toggle 8, Checkbox 12, Dropdown 13, FormField 13
tests: 57 passed (47 main incl. golden + parser-scope BT-19..22, + 10 adversarial mutation tests)
```

Mutation suite (`tests/test_token_normalizer_mutations.py`) deep-copies the real run and
injects 10 unseen value patterns (hsl/oklch/calc/gradient/keyword/border-shorthand,
enrichment fallback, empty-string match, unknown composite prop, duplicate path). All 10
run without exception and degrade gracefully — proving the intake funnel generalises beyond
the one calibration file.

## Design Decisions

- **DD-1 — `$type` is leaf-explicit (no group-level inheritance).** Every leaf states its own `$type`. Unambiguous, simplest to consume, and trivially consistent (BT-9). Group `$type` inheritance can be layered on later without breaking consumers.
- **DD-2 — UNRESOLVED tokens go to the report, NOT the tree.** A DTCG tree leaf must have a valid `$type`; an unresolved token has none, so inserting it would make `token_tree` spec-invalid. Unresolved tokens are recorded in `normalization_report.unresolved_tokens` and `all_entries` (with the raw value in `original_value`), but kept out of the tree. The reference run has **0 unresolved**, so this only affects malformed/edge inputs. ⚠️ *This is a spec interpretation — flagged for HITL/QA confirmation (the spec text says "passthrough raw value as string, log in report", which we read as report-only).*
- **DD-3 — Semantic aliases preserved, never deduplicated.** N input tokens with the same value → N output leaves at distinct paths. The report lists duplicate value groups for HITL awareness only.
- **DD-4 — `alpha` policy.** Omitted for hex colors (implicitly opaque); preserved for `rgba()` colors even when 1.0 (the source stated it — shadow layers rely on this).
- **DD-5 — Hand-rolled CSS parsers, no new dependency.** `tinycss2` is not in the venv and the "stdlib-only" constraint applies. The box-shadow splitter is parenthesis-aware (`split_top_level`) so `rgba(...)` commas don't shatter layers (FocusRing → exactly 2 layers).
- **DD-6 — Provenance scoping.** `$extensions.de.msqdx.helix` carries `confidence`, `sourceName`, `enrichmentPath`, `originalCategory`, `normalizerVersion`. QA-only fields (`originalValue`, `suspectedTypo`) live in the report ONLY, never in the tree (CT-15).
- **DD-7 — Import hygiene.** Both this agent and `figma_extractor` ship a `models.py`. `normalizer.py` puts only its own dir on `sys.path` and loads the figma input schema by explicit file path, re-exporting `FigmaExtractionResult`/`TokenEntry`/`ComponentEntry`. So `from normalizer import …` is the single, collision-free import surface for callers and tests.
- **DD-8 — `enrichment_type` governs type-confidence; `enrichment_match` governs path (independent signals).** A token can be AUTHORITATIVE on type while deriving its path from the css-var name (e.g. an empty `enrichment_match` but a present `enrichment_type`). Confidence reflects *type*-assignment certainty, not path provenance. The "no enrichment at all" case (type absent → not authoritative) is the BT-14 path. Verified by mutation M7 (type removed, match kept → high, path from enrichment) and M8 (match emptied, type kept → authoritative, path from css-var).
- **DD-9 — fontFamily keyword-guard.** `parse_font_family` rejects CSS-wide keywords (`normal/inherit/initial/unset/none/revert`) → UNRESOLVED, so a keyword never masquerades as a font family even when the token name infers fontFamily (addendum-parser-scope check #4; tests BT-22, M5).

## Limitations / known gaps

- Parser rules are tuned to the evidence in `run_sequential_003`. Unseen naming conventions or composite formats land in the UNRESOLVED bucket (the safety net) and surface at the HITL gate — they do not crash. Add new parser patterns as new client Figmas appear (GAP-11 Semantic Matcher handles non-DTCG-compliant client files downstream).
- `get_step_output` for **non-adjacent** steps is unverified here (Step 2 reads Step 1, which is adjacent). It must be confirmed for the Scaffolder (Step 3+) before relying on it; fallback is passthrough fields.
- DTCG dimension officially allows only `px`/`rem`; we widen to `em`/`%` for source fidelity. Verify Style Dictionary v4 handling during the prod spike.

## Files

```
agents/token_normalizer/
  models.py            # frozen contract: DTCG value types, NormalizedTokens, report, audit entry
  normalizer.py        # 7-phase pipeline + CLI (normalize_tokens is the public API)
  workflow_step.py     # Agno Workflow Step 2 wrapper + HITL gate + --check
  parsers/             # color, dimension, typography, shadow, paths (pure functions)
  runs/                # design.tokens.json, normalization_report.json, .golden.json, ...
tests/
  test_token_normalizer.py            # smoke/contract/behavioral + golden + parser-scope (BT-19..22)
  test_token_normalizer_mutations.py  # 10 adversarial input mutations (M1..M10)
  _normalizer_helpers.py              # resolve_dtcg_path, iter_leaves, synthetic fixture factory
```
