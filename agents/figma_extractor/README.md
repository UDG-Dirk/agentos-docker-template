# Figma Extractor — HELIX UC2 Pipeline Step 1

First production HELIX agent. Pulls design tokens, foundation styles, and component
variant matrices from a client's Figma file and emits a typed `FigmaExtractionResult`
for the next pipeline step (Token Normalizer).

Built from: `agents:figma-extractor:step1-spec` / `step2-instructions` / `step2-test-criteria`.
Feasibility proven by spikes S1 (extraction quality + headless Agno), S2 (Workflow spine), S3 (GitLab push).

**Status:** Deployed to prod — AgentOS on Coolify, as Workflow **Step 1** of `helix-figma-extractor`
(commit `b87f53b`; the retry-on-empty guard that recovers premature-finalize runs landed in `4fcd579`).
Live-verified end-to-end (extract → normalize → HITL → complete) against Figma file `8qPSyetzviLR6eF6bkpL44`.

## Files

| File | Purpose |
|---|---|
| `models.py` | `FigmaExtractionResult` + sub-models (`PageInfo`, `TokenEntry`, `ComponentVariant`, `ComponentEntry`, `AssetEntry`). The pipeline contract. |
| `agent.py` | The extractor. Two patterns (sequential / broadcast), enrichment loader, MCP-call capture, CLI. |
| `workflow_step.py` | Wraps the extractor as Workflow Step 1 + AgentOS registration (`--check`). |
| `runs/` | Run outputs (`{result, _meta}` JSON), logs, comparison, first-run report. |

## Two patterns

- **sequential** (Pattern B): one agent, sequential extraction. Cheap, deterministic baseline.
- **broadcast** (Pattern A): `Team(2 members, delegate_to_all_members=True)` + leader consensus.

Both produce the identical `FigmaExtractionResult` schema. Compared in `runs/COMPARISON.md`.

## I/O

**Input** — `session_state` dict: `figma_file_key`, `storybook_repo_url`, `cms_repo_url`,
`cms_type` (`storyblok`), `framework` (`vue`). The agent reads `figma_file_key` via
`add_session_state_to_context=True`.

**Enrichment** (optional, A5) — instruction-context injection from `spike_s1/*_variable_defs.json`
+ `*_design_context.json` (official-MCP proxy). If absent → extraction-only, `enrichment_coverage=0.0`,
gap flagged. Chosen over an Agno Knowledge object to avoid the LiteLLM knowledge-search tool route
(agno-dev gotcha #4).

**Output** — `runs/run_<pattern>_NNN.json`:
```jsonc
{ "result": { /* FigmaExtractionResult */ },
  "_meta": { "pattern", "model_id", "wall_clock_s", "enrichment_present",
             "mcp_calls": [{tool,args}], "metrics", "run_status" } }
```
`_meta.mcp_calls` lets the harness verify discovery-first (BR-1) and token capture (BR-5)
without parsing debug logs.

## Tools / stack

- **Framelink MCP** (`figma-developer-mcp`) over stdio via `MCPTools` + `StdioServerParameters`.
  Tools (Lane 1 — values + assets): `get_figma_data`, `download_figma_images`.
- **REST semantic layer** (Lane 2 — `agents/figma_extractor/semantic_layer.py`).
  Tool: `get_figma_semantic_layer(file_key)` — one composite tool that pulls three
  non-Enterprise PAT REST endpoints in parallel (`/component_sets`, `/components`,
  `/styles`) for the authored taxonomy, variant census, and Text/Effect/Grid styles
  that Framelink cannot see. Descriptions captured verbatim (naive, no parsing).
  Never raises — returns `{status, coverage_report, provenance, failure_reports, ...}`.
  Spec: `helix-poc-agno:spec:lane-2-semantic-layer-v0-1-draft`. Reconciliation:
  REST canonical for structural names, Framelink for resolved values.
- **PAT** injected ONLY at the MCP layer (`StdioServerParameters(env={"FIGMA_API_KEY": ...})`),
  never in `session_state`/logs/output (RULE 6).
- **Model**: `OpenAIChat` via LiteLLM (NOT `OpenAIResponses` — breaks tool round-trips, RULE 7).
- **Secrets**: `FIGMA_PAT` from `helix-poc-agno/.env`; model creds from `poc-agno-template/.env` (dual dotenv).

## Node IDs — discovery-first (RULE 1)

The agent resolves page **names → node IDs at runtime** from its own file-level discovery.
It NEVER trusts a hand-supplied name↔id list. Dirk's share-link IDs (and the step3 task spec)
are off-by-one; `spike_s1/page_index.json` is the MCP-verified oracle used only for test validation.
Authoritative: Button `57:645`, Checkbox `457:249`, Dropdown `457:729`, FormField `86:985`,
Input `100:1426`, Toggle `552:985`; Typography `600:425`, Colors `360:38`, Layout `105:2147`,
Icons `78:136`, ImageRatios `152:3814`, Text `54:2`.

## Run

```bash
cd ~/opencode/workbench/agno-setup/poc-agno-template
VENV=.venv/bin

# sequential (Pattern B)
$VENV/python ~/opencode/workbench/helix-poc-agno/agents/figma_extractor/agent.py \
    --pattern sequential --out <ABS>/runs/run_sequential_001.json

# broadcast (Pattern A)
$VENV/python .../agent.py --pattern broadcast --out <ABS>/runs/run_broadcast_001.json

# workflow registration smoke test
$VENV/dotenv run -- $VENV/python .../workflow_step.py --check
```

## Tests

```bash
cd ~/opencode/workbench/helix-poc-agno
<venv>/python -m pytest tests/test_figma_extractor.py --output=agents/figma_extractor/runs/run_sequential_001.json -q
```
Smoke (ST-1..6) + contract (CT-1..11) + behavioral (BR-1..5). No MCP mocking — tests run against
real run output. Quality (QC-*) and pattern comparison (CMP-*) are human-reviewed in the run reports.

## Known limitations

1. **Context bloat (540K tokens per full extraction).** Each get_figma_data YAML response (~37.5K tokens) accumulates in conversation history. 14 calls for 17-page file = 525K input tokens. Instruction-level optimization (Option C) was measured and disproven — this is architectural. Files with 30+ components will hit context window limits. Fix: subagent swarm architecture (roadmap:context-decomposition-architecture). Not blocking for PoC.

2. **Asset downloads verified for SVG only.** All 20 downloaded assets are SVG icons. PNG/JPEG download via `download_figma_images` with `pngScale` parameter is untested. Production files with raster assets may need additional handling.

3. **cwd sensitivity on deployment.** Asset downloads depend on `StdioServerParameters(cwd=...)` resolving to a writable directory (`/app/agents/figma_extractor/runs/assets/`, app-owned). Now deployed to the Coolify container and prod runs report assets successfully (21–23 SVGs per run). The container asset-**files-on-disk** verification (prod launch checklist item #13) is still outstanding — assets are reported in the result but the on-disk existence check hasn't been run in the container, and container storage is ephemeral (a redeploy wipes `runs/assets/`).

4. **Enrichment data is optional but improves coverage.** Without enrichment (OAuth-extracted Variables, Code Connect, designer docs), enrichment_coverage drops to 0.0 and token $type metadata is unavailable. The agent works in extraction-only mode but Token Normalizer output is lower quality.

5. **Sequential context is not resumable.** If the agent fails mid-extraction (budget cap, MCP error after call 9 of 14), all previous extraction work is lost. No checkpointing. Must re-run from scratch.

## HARDEN history

- **Round 1 (Step 3):** Agent finalized output_schema after one MCP call, under-extracting. Fixed: instruction now mandates ≥7 get_figma_data calls before finalizing.
- **Round 2 (Step 5):** Asset downloads fabricated (agent hallucinated file writes). Fixed: set cwd on StdioServerParameters, updated Phase 4 instructions, strengthened tests to assert file existence on disk. Context bloat measured (540K), Option C disproven, Option A (subagent swarm) deferred.

## FinOps baseline

| Metric | Value |
|--------|-------|
| Total tokens (sequential, 17-page file) | ~540K |
| Input tokens | ~526K |
| Output tokens | ~15K |
| Per-call input tokens | ~37.5K |
| Wall-clock time | ~230s |
| Model | gpt-5.4 via LiteLLM |
| Estimated cost per extraction | $1-2 (depends on model pricing) |

Primary cost driver: input tokens from accumulated YAML tool responses. Output tokens are negligible. Optimization target: context decomposition (subagent swarm) or delta extraction (memory-based re-run optimization).

## Production pattern

**Sequential** confirmed as production pattern. Broadcast (2-member Team) added 2x cost and latency for zero quality gain — one member silently failed, the other carried alone. See `runs/COMPARISON.md` for full analysis.

## Design Decisions

Numbered for stable reference (matches the Token Normalizer README convention). Several map to the
agent's hard `RULE`s in `step2-instructions`.

- **DD-1 — Discovery-first extraction (RULE 1).** The agent resolves page/component names → node IDs at
  runtime from its own file-level `get_figma_data` scan, before any node-specific extraction, and never
  from a human-supplied node-ID list. Those lists proved unreliable three separate times (off-by-one
  share-link labels); runtime discovery hit every authoritative node ID with zero hardcoding.
- **DD-2 — Sequential over broadcast (Team).** Equal output quality, roughly half the cost and latency,
  and full observability (a Team leader's RunResponse hides member calls/metrics). Broadcast's second
  member silently failed and added nothing. Sequential is the production pattern. See `runs/COMPARISON.md`.
- **DD-3 — Framelink (headless PAT) over the official Figma MCP.** The pipeline runs headless, which rules
  out the official MCP's interactive OAuth. Framelink loses the named Variable slash-paths / Code Connect,
  so that richer semantic data is captured separately, once per project, as enrichment by the application
  layer (hybrid OAuth+PAT architecture).
- **DD-4 — Two naming worlds preserved (RULE 4).** Every token carries BOTH its reconstructed CSS-var name
  (`name`) and, when available, its Variable slash-path (`enrichment_match`). The extractor does not
  reconcile them — reconciliation is a downstream (Token Normalizer / Semantic Matcher) concern. Dropping
  either world here would lose information the downstream needs.
- **DD-5 — Ephemeral asset URLs are never persisted; download immediately (RULE 3).** Figma's MCP asset
  URLs expire, so `download_figma_images` writes files to disk in the same run and the agent records only
  the local path; `original_url` is left empty. A persisted URL would be a dead reference.
- **DD-6 — Enrichment supplements, never replaces (graceful degradation).** With enrichment absent the
  agent runs extraction-only: `enrichment_coverage=0.0`, `$type` metadata unavailable, gap flagged — but
  it still produces a valid `FigmaExtractionResult`. Enrichment improves quality; it is not required to run.
- **DD-7 — Enrichment as instruction-context injection, not an Agno Knowledge object.** Injecting enrichment
  into the instructions avoids the LiteLLM knowledge-search tool round-trip (a known LiteLLM/agno failure
  mode) while still giving the agent the Variable taxonomy and Code Connect data.
- **DD-8 — PAT security: MCP layer only (RULE 6).** `FIGMA_PAT` is injected via
  `StdioServerParameters(env=...)` into the MCP child process only — never in `session_state` (persisted to
  db + visible in REST responses), logs, or output.
- **DD-9 — `OpenAIChat`, never `OpenAIResponses` (RULE 7).** As a tool + `output_schema` agent, it breaks on
  the OpenAIResponses → Anthropic-via-LiteLLM route ("sequence item 0: expected str instance, NoneType
  found"). `OpenAIChat` (chat/completions) round-trips tools correctly. Model id is centralized via
  `app.settings.default_chat_model()` (`OPENAI_MODEL_ID`, default `gpt-5.4`).
- **DD-10 — `cwd` on StdioServerParameters.** The Framelink server writes downloaded assets relative to its
  working directory, so the agent sets `cwd` to the agent dir to make `download_figma_images
  localPath="runs/assets"` resolve into `agents/figma_extractor/runs/assets/`. Without it, downloads landed
  nowhere and the agent fabricated paths (fixed in HARDEN round 2).
