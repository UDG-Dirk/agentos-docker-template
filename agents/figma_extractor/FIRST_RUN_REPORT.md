# Figma Extractor — First Run Report (Step 3)

**Date:** 2026-06-29 · **Agent:** first HELIX production agent (Workflow Step 1)
**File:** `8qPSyetzviLR6eF6bkpL44` (Helix_Design) · **Model:** gpt-5.4 via LiteLLM · **MCP:** Framelink (figma-developer-mcp), PAT, stdio

## Verdict: **PASS** (core extraction) — production-viable as **sequential**, with 1 open HARDEN item (assets)

- Automated suite: **sequential 21/21 PASSED**; broadcast 19 passed + 2 skipped (team-leader MCP-capture limitation, documented).
- Quality gates QC-2..QC-6 **met** (Button 55 variants; enrichment 0.74; all 3 known typos caught; Button Code Connect present; empty-Colors-page gap flagged).
- QC-1 (≥80% token coverage vs S1 Track A) and QC-7 (output usable by Token Normalizer) are **Dirk's human judgment** — recommend sign-off; output looks sufficient (100 tokens across 6 categories + 6 component variant matrices + Code Connect).
- Recommended production pattern: **sequential** (see COMPARISON.md, CMP-5).

## What worked

- **Headless extraction end-to-end** — Agno + Framelink/PAT/stdio, `OpenAIChat`, `output_schema=FigmaExtractionResult`, dual-.env. No browser, no OAuth, no Docker.
- **Discovery-first + runtime node-ID resolution (RULE 1)** — agent called `get_figma_data` at `0:0` (doc root, depth 1) first, then resolved every page/component name→id from that discovery. All 6 priority components hit their **authoritative** ids with **zero hardcoding**. This is the defence against Dirk's off-by-one share-link ids (which the task spec itself repeated).
- **Rich, correct output** — 25 pages classified (6 foundation / 13 component / 6 other); 100 tokens (color/typography/spacing/sizing/effect); Button 55-variant matrix; enrichment matching at 0.74; both naming worlds preserved (CSS-var + Variable slash-path) per RULE 4.
- **Typo + gap honesty** — caught `borde-subtle`, `surfac-subtle`, `disbled` (the known ones) plus `Drodown`, `ToggelElements`, `FormFiled`, `Disabeld`; flagged empty Colors page, Input-on-FormField-page, missing Code Connect for some components.
- **PAT security (RULE 6)** — BR-2 green: PAT never in output/meta. Injected only at the MCP layer.
- **Workflow registration (A4)** — `helix-figma-extractor` registers in AgentOS; `GET /workflows` → 200, listed.

## What didn't (findings)

1. **As-built sequential under-extracted (FIXED, hardening round 1).** The single agent finalized its `output_schema` after ONE discovery call (it even wrote "retrying…" to gaps but stopped). Root cause: `output_schema` biases the model to emit the object early. **Fix applied:** instruction now forbids finalizing after one call and mandates ≥7 `get_figma_data` calls (discovery + per foundation page + per priority component). Re-run → 21/21.
2. **Asset download does not persist files (OPEN — HARDEN round 2).** 18–20 asset entries with plausible `local_path`s and `original_url="downloaded-via-mcp"`, but `runs/assets/` is **empty**. The agent asserted downloads that never landed. CT-7/BR-3 only verify the path is set and non-URL, so they pass falsely. **Action:** make the agent call `download_figma_images` with an explicit local path and verify the file exists; strengthen the test to assert on-disk existence; until fixed, treat `assets[]` as unreliable.
3. **Sequential context bloat (FinOps).** 465 K LLM tokens for 14 calls — each large YAML tool output accumulates in the single context. Bound it before scaling to many components/large files (per-page sub-agents, function-step extraction, or tool-output summarization).
4. **Agno Team under-reports (broadcast only).** `RunResponse.metrics` and `.tools` capture the **leader only**, not members → broadcast token cost (39 K reported) is fictitiously low and member calls are invisible. `_extract_mcp_calls` now walks `member_responses`; apply the same to metrics if broadcast is ever used.

## Recommended instruction changes (beyond round 1)

- **Assets:** "After identifying image nodes, call `download_figma_images` with explicit `nodes` + a local directory; set `local_path` ONLY to a path you confirmed the tool wrote. If you did not actually download, set `local_path='DOWNLOAD_FAILED'` — never invent a path."
- **Context economy (when scaling):** consider decomposing Phase 2/3 into per-node function steps so each large YAML payload is processed and discarded rather than retained across the whole conversation.

## Token-usage baseline (FinOps)

| Pattern | wall-clock | LLM tokens (true) | get_figma_data calls |
|---|---|---|---|
| sequential (hardened) | 221 s | **465,135** | 14 |
| broadcast (2-member) | 408 s | ≥ ~2× sequential (members not aggregated; 39,114 = leader only) | ~25 |

Baseline for the FinOps token-measurement roadmap: **~465 K tokens / ~3.7 min per full-file
extraction (sequential, gpt-5.4 via LiteLLM)** — dominated by accumulated YAML tool outputs, the
primary optimization target.

## Deliverables

`agents/figma_extractor/`: `models.py`, `agent.py` (both patterns + hardening), `workflow_step.py`,
`README.md`, `runs/` (run_sequential_001/002, run_broadcast_001 + .log, test_results_*, COMPARISON.md,
workflow_register_check.json, this report). `tests/`: `test_figma_extractor.py`, `conftest.py`.

## Next

- Dirk: QC-1 / QC-7 sign-off + confirm sequential as the production pattern.
- HARDEN round 2: fix asset download persistence (+ strengthen asset test to verify files on disk).
- Then pipeline Step 2: **Token Normalizer** (consumes `FigmaExtractionResult` → W3C DTCG tokens).
