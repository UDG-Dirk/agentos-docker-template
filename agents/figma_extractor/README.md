# Figma Extractor — HELIX UC2 Pipeline Step 1

> **Step 1 is now DETERMINISTIC (no LLM)** — spec `helix-poc-agno:spec:figma-extractor-deterministic-v0-1-draft` (RATIFIED).
> `deterministic.py` (`run_deterministic_extraction`) replaced the LLM-orchestrated agent: reading
> structured data from a structured API is not the LLM's job (the LLM invented component names not in
> the authored roster — the fabrication surface). Fixed orchestration: Lane 5 meta → Lane 2 semantic
> roster → per-component_set Framelink `get_figma_data` via `ClientSession.call_tool` (NO LLM) → Lane 3
> bindings → asset download → Levenshtein+dictionary typo detection → compose. Same-input→same-output
> (bulletproof). Emits a `FigmaExtractionResult`-shaped dict (Token Normalizer contract preserved) with
> the §5 rich envelope nested under `deterministic_extraction`. `provenance.llm_involvement = "none"`.
> The old LLM agent (`agent.py` + the drill-guard in the workflow) is retained as dead code / git
> history per spec §9 (replace-in-place); removable in a cleanup follow-up.

### Framelink pinned to 0.13.2 + `--format json` (required)

The Framelink server is spawned as `npx -y figma-developer-mcp@0.13.2 --stdio --format json`
(`agent.py:_mcp_server_params`). Both the **pin** and the **format flag** are load-bearing:

- **Version pin** — the arg was previously unpinned (`figma-developer-mcp`), so `npx` resolved a
  stale **0.9.0** locally while the Dockerfile installs **0.13.2** globally in prod. That drift was
  the root cause of the prod "thinness" (local rich, prod empty). The pin here MUST match the
  Dockerfile's `npm install -g figma-developer-mcp@0.13.2`.
- **`--format json`** — Framelink **v0.13.0 flipped the default output format from YAML to `tree`**
  (PR #394). `tree` is a compact non-YAML format the distiller cannot parse (it surfaced as a YAML
  `ScannerError` → every set failed). `--format json` returns the parseable
  `[metadata, nodes, globalVars, elements]` schema. JSON (not `yaml`) is used deliberately — it
  avoids the YAML edge-case surface that the `tree` default exposed.

0.13.0 also **deduplicates** styles (a file's `globalVars.styles` shrinks vs 0.9.0's per-node
duplicates) and adds an additive `elements` key whose `layout` fields are *references* into
`globalVars.styles`. `_distill_tokens_from_globalvars` reads `globalVars.styles` and, via
`_infer_style_category`, also distils **named** styles that carry no known prefix (e.g.
`link/md/regular` → typography, `FocusRing` → effect) by inspecting the value shape — so token
coverage is by-value, robust to naming/version changes, and never fabricated (opaque styleId name +
resolved value). Note: a lower raw token count under 0.13.x is expected — it reflects dedup, not loss
(0.9.0's ~733 raw tokens were ~67 distinct).

**Rate-limit handling under load (0.13.x):** Framelink surfaces a Figma **429** two different ways —
0.9.x as error *text* (`Too Many Requests`), **0.13.x as an *empty envelope*** (`metadata.components`
and `globalVars.styles` both empty). `_default_get_figma_data` retries with backoff `(1,3,8)s` on
**both** signals (`_is_rate_limited` OR `_is_empty_envelope`) — safe because a *component_set* is never
legitimately empty. `_NODE_CONCURRENCY` is **2** (lowered from 3) to reduce throttle pressure on heavy
subtrees (Input ≈ 233 KB, Button ≈ 141 KB). If a set is still empty after retries it is surfaced loud
(`empty_response`), never silently dropped — the P1a fail-loud guard stays intact.

**Schema-guard test** (`tests/test_deterministic_extractor.py::test_framelink_0_13_2_schema_guard`,
backed by `tests/fixtures/framelink_0_13_2_node_57_766.json`) asserts the top-level shape stays
`[metadata, nodes, globalVars]` (+`elements`) — it fails loud if a future Framelink bump silently
changes the schema, closing the process gap that let the default-format flip reach prod.

### Two-mode extraction + per-client orchestration (Path C rev.3.1) — ADDITIVE, Phase A

The extractor now has **two modes**:
- **Library mode** (`deterministic.py`) — published-library files like **Core** (Lanes 1/2/3/5/7).
- **Composition mode** (`composition_mode.py`) — client / **Modules** files whose organisms live as
  page frames (0 published entities). Runs **Pathway B** (`pathway_b_traversal.py`) + Lane 5 + **Lane 6**;
  **skips Lane 2** (0/0/0 on composition files); Lane 7 optional (no token blob → handled gracefully).

**Pathway B** (`pathway_b_traversal.run_pathway_b`) — deterministic page-frame walk (page order, then
depth-first — matches Lane 6 traversal order), bounded depth (default 20) + per-page frame cap. Emits a
`composition_tree` + the `remote:true` references Lane 6 consumes. Fail-loud: `file_inaccessible`,
`empty_composition_file`, `traversal_depth_exceeded`, `malformed_node_data`.

**Per-client entry point** — `composition_mode.extract_client_design_system(core_file_key,
client_file_key, additional_library_keys=[], freshness_threshold_days=7)`:
1. Library-mode Core extraction, **cached** per `(core_key, lastModified)` (MLOps/FinOps win),
2. Composition-mode client extraction (Pathway B → remote refs),
3. Lane 6 streaming resolution against `[core] + additional_library_keys` (UNBOUNDED N),
4. Unified output + reconciliation contract. `emit` callback forwards Lane 6 events to SSE. Zero LLM.

Proven end-to-end live (Modules→Core, all 17 pages): 925 frames, 63 remote refs, **23/25 unique
resolved (92%)**. **Scope note:** the deployed AgentOS *workflow* registration of
`extract_client_design_system` is the remaining thin wire (the orchestration function + SSE `emit`
callback are complete and live-proven; registering a second Agno Workflow entry is a follow-up).

### Lane 6 — Cross-File Library Resolution (STREAMING) — ADDITIVE, Phase A

`cross_file_resolution.py` (`resolve_stream` / `resolve`) resolves `remote:true` component references
from a **composition file** (Modules / client files) against **registered foundation libraries**
(Core + optional additional, UNBOUNDED N) — proving where each organism's Core dependencies live.
Spec: `spec:lane-6-cross-file-library-resolution-v0-1-draft` v0.1.1 (STREAMING). Zero LLM.

**Streaming** — `resolve_stream(...)` is an async generator yielding SSE-compatible events in
deterministic order (`page_then_depth_first` traversal × registration-order library priority):
`resolution_started` → `library_registered`/`library_registration_failed` (per lib) →
`library_dependency_cycle` (bounded direct+one-hop) → per-reference `resolved_reference` /
`library_key_collision` / `unresolved_reference` (+ `third_library_suspect` when ≥3 unresolved cluster)
→ `resolution_complete` (summary + per-library SLI metrics). `resolve(...)` is a batch-bridge that
collects the stream for non-streaming consumers.

**Mechanism** (proven 7/9 on Modules MediaText → Core): remote instances carry a global component
`key`; exact-match against a registered library's `/components`(+`/component_sets`) map, **first match
in registration-order priority** (Core first). Library maps cached per `(lib_key, lastModified)`.

**Scope note:** Lane 6 is the resolution **engine** — it consumes an enumerated reference list. Producing
that list from a composition file is **Pathway B** (a separate, not-yet-built work stream; the current
published-library extractor yields nothing on composition files). A minimal `enumerate_remote_references`
helper is included for the live smoke only — NOT the production traversal. Phase A: emits events, no
downstream consumer yet, safe single-MR revert.

### Lane 7 — Token Catalog (Tokens Studio) — ADDITIVE, Phase A

`token_catalog.py` (`run_token_catalog`) extracts the **authoritative** design-token catalog from
`document.sharedPluginData.tokens` — a **Tokens Studio** export (DTCG-claimed, legacy `name/value/type`
keys), **PAT-accessible, no Enterprise** (the Figma Variables REST API is Enterprise-gated). The
`values` blob is **LZString-UTF16** compressed and decompressed by a **pure-Python** port (zero deps,
no subprocess, byte-identical to the `lz-string` npm reference). Spec: `spec:lane-7-token-catalog-v0-1-draft`
v0.1.2.

Emits two additive top-level fields on the extractor output (Phase A — no downstream consumer yet):
- **`token_catalog`** — 720 tokens with `name`, `type`, `value_raw` + `value_resolved` (aliases emitted
  BOTH ways; multi-hop bounded to 10 with cycle detection; deterministic resolution order = `primitive/Core`
  then semantic sets alphabetical), `mode` (set-per-breakpoint: `semantic-dimension/{Tablet,Phone,Desktop,Wide}`;
  dark-mode-ready via multi-variant-category detection), `$extensions` (scopes, hiddenFromPublishing),
  plus `freshness_status`, `divergence_status`, `token_sets`, `modes_detected`, `unrecognized_schema_fields`.
- **`reconciliation_contract`** — the `authoritative_catalog_with_usage_evidence_fallback` policy downstream
  stations follow to reconcile Lane 7 (authoritative names) with Lane 1 (usage-derived values).

**Fail-loud error classes (§7):** `missing_shared_plugin_data`, `tokens_studio_major_version_incompatible`,
`lzstring_{empty,truncated,invalid_encoding,corrupt_data,iteration_bound_exceeded}_input`, `dtcg_parse_error`,
`unrecognized_schema_field` / `unrecognized_extension_namespace`, `freshness_suspect`, `catalog_divergence_suspect`.
Lane 7 is **self-contained**: its status/failure_reports live under `token_catalog`; it does **not** flip the
main extraction status (Phase A additive — a stale-blob soft signal must not turn every run partial).

**Version discipline:** parser verified against Tokens Studio `2.11.5`. patch bump → warn; minor → `partial`;
**major → `failure`** (no parse). Freshness threshold: 7 days default, override via workflow param or
`HELIX_LANE7_FRESHNESS_THRESHOLD_DAYS`. Divergence (Step 7.9): signal-only count-delta vs Lane 3's unique
VariableIDs (>20% more referenced than catalogued → `catalog_divergence_suspect`).

### Fail-loud per-set enrichment (spec §3 — "fail loud, not silent")

Per component_set, `get_figma_data` enrichment is classified before any data is emitted
(`_classify_gfd_response`). A set is marked **failed** — added to `coverage_report.component_sets_failed`
+ a structured `failure_report` + a `gaps_detected` entry — and emits **nothing** (anti-fabrication: no
empty shell is backfilled) when Framelink returned no usable structure:

| `error_class` | Trigger | http_status |
|---|---|---|
| `rate_limit_exhausted` | 429 marker survived `_default_get_figma_data`'s backoff | 429 |
| `malformed` | YAML could not be parsed | — |
| `empty_response` | non-dict payload, **or** `metadata.components` **and** `globalVars.styles` both empty | — |
| `server_error` | `get_figma_data` raised | — |
| `client_error` | component_set had no `node_id` | — |

**Thin-but-valid is NOT a failure:** a set with **either** components **or** styles present is treated as
valid (e.g. a single-component set with no local styles), so genuinely sparse sets are never false-failed.
The `error_class` names refine Lanes 2/3/5's generic `empty`/`malformed` for diagnostic precision.

**Status aggregation:** any failed set → `partial`; **all** sets failed → `failure` (only the Lane-2
skeleton survived); roster failed → `failure`. This is what made the prod-vs-local thinness
(35 vs 733 tokens) surface as `partial`/`failure` + gaps instead of a silent `success`.

Pulls design tokens, foundation styles, and component variant matrices from a client's Figma file
and emits a typed `FigmaExtractionResult` for the next pipeline step (Token Normalizer).

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
- **REST cache/versioning** (Lane 5 — `agents/figma_extractor/cache_versioning.py`).
  Two atomic tools: `get_figma_file_meta(file_key)` — cheap (~965 B) change-detection
  probe (`version` + `last_touched_at`, no full tree); `get_figma_file_versions(file_key,
  page_size=30)` — historical version list (reproducibility/audit). Same PAT/retry/
  provenance pattern as Lane 2 v0.2 (error_class incl. `not_found`/`client_error`); single
  `failure_report` object (not array). Primitives only — change-detection *logic* lives in
  the workflow layer. Spec: `helix-poc-agno:spec:lane-5-cache-versioning-v0-1-draft`.
- **REST binding topology** (Lane 3 — `agents/figma_extractor/binding_topology.py`).
  Tool: `get_figma_binding_topology(file_key, node_ids, depth?, geometry?)` — node-scoped
  `getFileNodes` call parsing `boundVariables` at node level (fills/strokes/effects/layout/
  spacing) AND componentProperty level, returning a `{node_id → property → VariableID}` map
  + `binding_summary`. Variable IDs surfaced OPAQUE (`VariableID:X:Y`) — resolution is
  downstream. Missing requested ids = success with coverage gap (not failure). 30s timeout;
  same v0.2 error_class/retry/provenance pattern; single `failure_report` object. Spec:
  `helix-poc-agno:spec:lane-3-binding-topology-v0-1-draft`.
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
