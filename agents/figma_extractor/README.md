# Figma Extractor — HELIX Pipeline Step 1 (DETERMINISTIC)

> **Deterministic Python. Zero LLM.** Reading structured data from a structured API is not the LLM's
> job — the fabrication surface is unacceptable (previous LLM agent invented component names not in
> the authored roster). Fixed orchestration, same-input→same-output, provenance on every emission,
> fail loud on anything unusable.
>
> **Spec:** `helix-poc-agno:spec:figma-extractor-deterministic-v0-1-draft` v0.1.1 (RATIFIED).
> `provenance.llm_involvement = "none"`.

## Architecture

**Two-file design system context** (multi-instance productization):
- **Core** (foundation) — atoms + molecules + Tokens Studio catalog. Shared substrate across all clients.
- **Client instances** — organism compositions consuming Core (Modules is the white-label reference).
  Reference Core (and optional additional libraries) via `remote:true` component instances.

**Extractor has two modes to match this reality:**

| Mode | Target | Mechanism | Entry point |
|---|---|---|---|
| **Library** | Files publishing a component library (e.g. Core) | Lanes 1+2+3+5+7 | `deterministic.run_deterministic_extraction` |
| **Composition** | Files with organisms as page frames (Modules, clients) | Pathway B + Lanes 1+5+6 (Lane 2 skipped; Lane 7 optional) | `composition_mode.run_composition_extraction` |

**Per-client orchestration** — `composition_mode.extract_client_design_system(core_file_key, client_file_key, additional_library_keys=[], freshness_threshold_days=7)`:
1. Library-mode Core extraction, **cached** per `(core_key, lastModified)`
2. Composition-mode client extraction (Pathway B → remote refs)
3. Lane 6 streaming resolution against `[core] + additional_library_keys` (UNBOUNDED N)
4. Unified output + `reconciliation_contract`

**Live-proven end-to-end** (Modules → Core, all 17 pages): 925 frames, 63 remote refs, **23/25 unique resolved (92%)**.

## Deployed workflows

| Workflow | Purpose | Invocation |
|---|---|---|
| `helix-figma-extractor` | Library-mode single-file extraction (Core substrate) | `POST /workflows/helix-figma-extractor/runs` |
| `helix-client-extractor` | Multi-file per-client extraction (Core + client + Lane 6 resolution) | `POST /workflows/helix-client-extractor/runs` |
| `helix-composition-only-extractor` | **Pattern 3** — self-contained/unpublished file (Pathway B only, Lane 6 skipped; e.g. DGX Brandportal) | `POST /workflows/helix-composition-only-extractor/runs` |

**Direct-REST invocation examples:**

```bash
# Library mode — Core-only extraction
curl -sN -H "Authorization: Bearer $AGNO_MCP_TOKEN" \
  -X POST https://poc-agno-api.services.plygrnd.tech/workflows/helix-figma-extractor/runs \
  --data-urlencode 'message=8qPSyetzviLR6eF6bkpL44' \
  --data-urlencode 'background=true'

# Composition mode — full per-client extraction (Core + client + Lane 6 streaming resolution)
curl -sN -H "Authorization: Bearer $AGNO_MCP_TOKEN" \
  -X POST https://poc-agno-api.services.plygrnd.tech/workflows/helix-client-extractor/runs \
  --data-urlencode 'message=core=8qPSyetzviLR6eF6bkpL44 client=qMi5B9YeqAf9Ik1yN6erw4' \
  --data-urlencode 'background=true'
```

Optional `helix-client-extractor` params via message: `additional=<key1,key2>`, `freshness=<days>`.
Lane 6 streaming events ride in the result's `client_extraction.resolution_events` in deterministic order.

**Missing-parameter prompt:** if the run message lacks the two required keys, the workflow returns a
structured `needs_parameters` prompt (required/optional params, `message_format`, examples, and any
keys it *did* detect) instead of a terse error — so MCP/REST callers see exactly what to supply and
re-invoke. (True interactive elicitation isn't available on the current surface — the agno-prod MCP
exposes a generic `run_workflow(workflow_id, message)` with no per-workflow typed params or
`elicitation/create`, and Agno HITL is output-review, not input-collection.)

## Modules

| Module | Purpose |
|---|---|
| `deterministic.py` | Library-mode extractor. Fixed orchestration Lane 5 → Lane 2 → per-set Framelink → Lane 3 → assets → typos → compose. Emits `FigmaExtractionResult`-shaped dict + `deterministic_extraction` rich envelope. |
| `composition_mode.py` | Composition-mode extractor + `extract_client_design_system` per-client entry point. Pathway B + Lanes 5/6, Lane 2 skipped, Lane 7 optional. Core-extraction caching. `emit` callback forwards Lane 6 events. |
| `pathway_b_traversal.py` | Deterministic page-frame walk for composition files. Bounded depth (default 20) + per-page frame cap. Emits `composition_tree` + `remote:true` reference list for Lane 6 consumption. Page fetches use **(1,3,8)s 429 backoff** (mirrors Lane 1's `_default_get_figma_data`; the per-client run shares one PAT so the composition tail gets throttled) — exhausted retries surface fail-loud as `malformed_node_data`, never silent. |
| `cross_file_resolution.py` | Lane 6 — streaming cross-file library resolution. `resolve_stream` (async generator, 8 SSE event types) + `resolve` (batch bridge). Registration-order priority, cache per `(lib_key, lastModified)`, cycle detection (bounded direct + one-hop), third-library suspect clustering. |
| `token_catalog.py` | Lane 7 — Tokens Studio DTCG catalog via `sharedPluginData`. Pure-Python LZString-UTF16 decompression. 720 tokens with name/type/value_raw/value_resolved/mode/extensions. `reconciliation_contract` emission for downstream. |
| `semantic_layer.py` | Lane 2 — REST semantic layer (`get_figma_semantic_layer`). Parallel fetch of `/component_sets`, `/components`, `/styles` for authored taxonomy + Text/Effect/Grid styles Framelink can't see. |
| `binding_topology.py` | Lane 3 — REST binding topology (`get_figma_binding_topology`). Node-scoped `getFileNodes` parsing `boundVariables` at node + componentProperty level. Variable IDs surfaced opaque; resolution downstream. |
| `cache_versioning.py` | Lane 5 — REST cache/versioning primitives. `get_figma_file_meta` (change-detection probe) + `get_figma_file_versions` (audit trail). |
| `models.py` | `FigmaExtractionResult` + sub-models. Token Normalizer contract preserved. |
| `agent.py` | **LEGACY** — LLM-orchestrated extractor. Retained as dead code / git history per spec §9 (replace-in-place); removable in cleanup follow-up. Also hosts the shared Framelink `figma_mcp_tools` MCP client used by the deterministic lanes. |

> Workflow registration lives in `app/workflows/` (`helix_figma_extractor.py` Library mode,
> `helix_client_extractor.py` per-client mode), wired into AgentOS in `app/main.py`.

## Framelink pinned to 0.13.2 + `--format json` (required)

The Framelink server is spawned as `npx -y figma-developer-mcp@0.13.2 --stdio --format json`
(`_mcp_server_params`). Both the **pin** and the **format flag** are load-bearing:

- **Version pin** — arg was previously unpinned (`figma-developer-mcp`), so `npx` resolved a stale
  **0.9.0** locally while the Dockerfile installs **0.13.2** globally in prod. That drift was the root
  cause of prod "thinness" (local rich, prod empty). Pin here MUST match Dockerfile's
  `npm install -g figma-developer-mcp@0.13.2`.
- **`--format json`** — Framelink **v0.13.0 flipped the default output format from YAML to `tree`**
  (PR #394). `tree` is a compact non-YAML format the distiller cannot parse (surfaces as YAML
  `ScannerError` — every set fails). `--format json` returns the parseable
  `[metadata, nodes, globalVars, elements]` schema. JSON (not `yaml`) chosen deliberately — avoids the
  YAML edge-case surface `tree` exposed.

0.13.0 also **deduplicates** styles (`globalVars.styles` shrinks vs 0.9.0's per-node duplicates) and
adds an additive `elements` key whose `layout` fields are *references* into `globalVars.styles`.
`_distill_tokens_from_globalvars` reads `globalVars.styles` and, via `_infer_style_category`, distils
**named** styles that carry no known prefix (e.g. `link/md/regular` → typography, `FocusRing` → effect)
by value shape — so token coverage is by-value, robust to naming/version changes, never fabricated.
Note: lower raw token count under 0.13.x is EXPECTED — reflects dedup, not loss (0.9.0's ~733 raw
tokens were ~67 distinct).

### Rate-limit handling under load (0.13.x)

Framelink surfaces Figma **429** two different ways — 0.9.x as error *text* (`Too Many Requests`),
**0.13.x as an *empty envelope*** (`metadata.components` and `globalVars.styles` both empty).
`_default_get_figma_data` retries with backoff `(1,3,8)s` on **both** signals (`_is_rate_limited` OR
`_is_empty_envelope`) — safe because a *component_set* is never legitimately empty.
`_NODE_CONCURRENCY` is **2** (lowered from 3) to reduce throttle pressure on heavy subtrees
(Input ≈ 233 KB, Button ≈ 141 KB). If a set is still empty after retries it is surfaced loud
(`empty_response`), never silently dropped — P1a fail-loud guard stays intact.

### Schema-guard test

`tests/test_deterministic_extractor.py::test_framelink_0_13_2_schema_guard` (backed by
`tests/fixtures/framelink_0_13_2_node_57_766.json`) asserts the top-level shape stays
`[metadata, nodes, globalVars]` (+`elements`) — fails loud if a future Framelink bump silently
changes the schema, closing the process gap that let the default-format flip reach prod.

## Lane 6 — Cross-File Library Resolution (STREAMING)

`cross_file_resolution.py` resolves `remote:true` component references from a composition file
(Modules / client files) against registered foundation libraries (Core + optional additional,
UNBOUNDED N). Spec: `spec:lane-6-cross-file-library-resolution-v0-1-draft` v0.1.1.

**Streaming** — `resolve_stream(...)` is an async generator yielding SSE-compatible events in
deterministic order (`page_then_depth_first` traversal × registration-order library priority):

- `resolution_started` → `library_registered` / `library_registration_failed` (per lib) →
- `library_dependency_cycle` (bounded direct + one-hop) →
- per-reference `resolved_reference` / `library_key_collision` / `unresolved_reference` /
  `internal_link_reference` (+ `third_library_suspect` when ≥3 unresolved cluster) →
- `resolution_complete` (summary + per-library SLI metrics).

**Reference-type classification (v0.1.3):** a `remote:true` ref tagged `ref_type="internal_link"`
(evidence-based — enumerated from prototype `reactions[].action.destinationId` or TEXT `hyperlink`
fields, NOT the components map) emits an `internal_link_reference` event, skips library resolution, and
is **excluded** from third-library clustering. Component-instance refs (variant name + `componentSetId`)
stay on the resolve path. Note: on the Helix_Modules file, REST enumeration surfaces **zero** internal-link
refs at depth 4, and the 2 recurring non-Core keys are genuine **component variants**
(`Size=…`, `componentSetId 72:2195`/`385:11416`) — they remain `unresolved_reference` (a real
third-library question for Peter/Mel), NOT internal links.

`resolve(...)` is a batch-bridge that collects the stream for non-streaming consumers.

**Mechanism** (proven 7/9 on Modules MediaText → Core; 23/25 = 92% end-to-end on full Modules):
remote instances carry a global component `key`; exact-match against a registered library's
`/components`(+`/component_sets`) map, **first match in registration-order priority** (Core first).
Library maps cached per `(lib_key, lastModified)`.

## Lane 7 — Token Catalog (Tokens Studio)

`token_catalog.py` extracts the **authoritative** design-token catalog from
`document.sharedPluginData.tokens` — a **Tokens Studio** export (DTCG-claimed, legacy
`name/value/type` keys), **PAT-accessible, no Enterprise** (Figma Variables REST API is
Enterprise-gated for our tier). The `values` blob is **LZString-UTF16** compressed and decompressed
by a **pure-Python** port (zero deps, no subprocess, byte-identical to `lz-string` npm reference).
Spec: `spec:lane-7-token-catalog-v0-1-draft` v0.1.2.

**Emits two additive top-level fields** (Phase A — no downstream consumer yet):

- **`token_catalog`** — 720 tokens with `name`, `type`, `value_raw` + `value_resolved` (aliases
  emitted BOTH ways; multi-hop bounded to 10 with cycle detection; deterministic resolution order =
  `primitive/Core` then semantic sets alphabetical), `mode` (set-per-breakpoint:
  `semantic-dimension/{Tablet,Phone,Desktop,Wide}`; dark-mode-ready via multi-variant-category
  detection), `$extensions` (scopes, hiddenFromPublishing), plus `freshness_status`,
  `divergence_status`, `token_sets`, `modes_detected`, `unrecognized_schema_fields`.
- **`reconciliation_contract`** — the `authoritative_catalog_with_usage_evidence_fallback` policy
  downstream stations follow to reconcile Lane 7 (authoritative names) with Lane 1 (usage-derived
  values).

**Fail-loud error classes:** `missing_shared_plugin_data`, `tokens_studio_major_version_incompatible`,
`lzstring_{empty,truncated,invalid_encoding,corrupt_data,iteration_bound_exceeded}_input`,
`dtcg_parse_error`, `unrecognized_schema_field` / `unrecognized_extension_namespace`,
`freshness_suspect`, `catalog_divergence_suspect`.

Lane 7 is **self-contained**: its status/failure_reports live under `token_catalog`; does **not**
flip main extraction status (Phase A additive — a stale-blob soft signal must not turn every run
partial).

**Version discipline:** parser verified against Tokens Studio `2.11.5`. patch bump → warn;
minor → `partial`; **major → `failure`** (no parse). Freshness threshold: 7 days default, override
via workflow param or `HELIX_LANE7_FRESHNESS_THRESHOLD_DAYS`. Divergence detection (Step 7.9):
signal-only count-delta vs Lane 3's unique VariableIDs (>20% more referenced than catalogued →
`catalog_divergence_suspect`).

## Fail-loud per-set enrichment (spec §3 — "fail loud, not silent")

Per component_set, `get_figma_data` enrichment is classified before any data is emitted
(`_classify_gfd_response`). A set is marked **failed** — added to `coverage_report.component_sets_failed`
+ structured `failure_report` + `gaps_detected` entry — and emits **nothing** (anti-fabrication: no
empty shell backfilled) when Framelink returned no usable structure:

| `error_class` | Trigger | http_status |
|---|---|---|
| `rate_limit_exhausted` | 429 marker survived `_default_get_figma_data`'s backoff | 429 |
| `malformed` | YAML could not be parsed | — |
| `empty_response` | non-dict payload, **or** `metadata.components` **and** `globalVars.styles` both empty | — |
| `server_error` | `get_figma_data` raised | — |
| `client_error` | component_set had no `node_id` | — |

**Thin-but-valid is NOT a failure:** a set with **either** components **or** styles present is
treated as valid (e.g. a single-component set with no local styles), so genuinely sparse sets are
never false-failed.

**Status aggregation:** any failed set → `partial`; **all** sets failed → `failure` (only Lane-2
skeleton survived); roster failed → `failure`. This is what made the prod-vs-local thinness
(35 vs 733 tokens) surface as `partial`/`failure` + gaps instead of silent `success`.

## Tools / stack

- **Framelink MCP** (`figma-developer-mcp@0.13.2` pinned + `--format json`) over stdio via
  `MCPTools` + `StdioServerParameters`. Tools (Lane 1 — values + assets): `get_figma_data`,
  `download_figma_images`.
- **Lane 2** — `get_figma_semantic_layer(file_key)` composite REST tool for authored taxonomy.
- **Lane 3** — `get_figma_binding_topology(file_key, node_ids, depth?, geometry?)` for opaque
  Variable ID surfacing.
- **Lane 5** — `get_figma_file_meta` + `get_figma_file_versions` for change-detection + audit.
- **Lane 6** — `resolve_stream` async generator + `resolve` batch bridge for cross-file resolution.
- **Lane 7** — `run_token_catalog` for Tokens Studio DTCG catalog.
- **PAT** injected ONLY at the MCP layer (`StdioServerParameters(env={"FIGMA_API_KEY": ...})`),
  never in `session_state` / logs / output.
- **Secrets** — `FIGMA_PAT` from `.env`.

## Reconciliation contract (downstream consumers)

Extractor emits BOTH usage-derived tokens (Lane 1, ~61 Framelink globalVars-keyed) AND authoritative
catalog (Lane 7, 720 Tokens Studio-named). Extractor does **NOT** attempt automatic reconciliation
(no clean join key across the two naming conventions). Downstream stations follow the
`reconciliation_contract`:

| Consumer scenario | Rule |
|---|---|
| Token in Lane 7 catalog | **PREFER** Lane 7 (authoritative name + resolved value) |
| Token in client file but NOT in Lane 7 catalog | `client_override` category, client provenance preserved |
| Component in Lane 2 (published library) | **PREFER** Lane 2 definition; client references are instances |
| Component in client file with NO `remote:true` | `client_custom_component` category |
| Remote key resolves to Core (Lane 6) | Use Core's authoritative definition |
| Remote key resolves to third library | Use resolved definition with library provenance |
| Remote key does NOT resolve | `unresolved_reference` for cross-team investigation; do NOT fabricate |
| Library key collision (same key across libraries) | Emit `library_key_collision` warning; downstream reviews |

Every emitted element carries file provenance: `{file_key, file_role, extracted_at, resolved_via?}`.

## Tests

```bash
cd ~/opencode/workbench/agno-setup/poc-agno-template
source .venv/bin/activate
python -m pytest tests/test_deterministic_extractor.py -q
python -m pytest tests/test_composition_mode.py -q      # composition mode + Pathway B traversal
python -m pytest tests/test_cross_file_resolution.py -q  # Lane 6
python -m pytest tests/test_token_catalog.py -q          # Lane 7
python -m pytest tests/test_helix_client_extractor.py -q # deployed helix-client-extractor workflow
```

Contract tests (per-lane), anti-fabrication guards (every emission traceable), cross-run consistency
(byte-identical output modulo timestamps), streaming semantics (Lane 6 event order determinism),
cache correctness (Lane 6 library maps + Core-extraction caching), pathological input handling
(Lane 7 LZString edge cases), and live smokes against real files (gated on `FIGMA_PAT`).

## Known limitations

1. **Asset downloads verified for SVG only.** All downloaded assets in observed runs are SVG icons.
   PNG/JPEG download via `download_figma_images` with `pngScale` is untested. Production files with
   raster assets may need additional handling.

2. **Container asset persistence not verified.** Assets download to
   `agents/figma_extractor/runs/assets/` (app-owned via `StdioServerParameters(cwd=...)`), and prod
   runs report assets successfully. Container asset-**files-on-disk** verification is still
   outstanding — assets are reported in the result but on-disk existence check hasn't been run in the
   container, and container storage is ephemeral (a redeploy wipes `runs/assets/`).

3. **Lane 3 bindings off-by-default in composition mode.** Rate-limit prudence on large composition
   files (925-frame Modules test showed sufficient pressure). Available via flag; enable per-need.

4. **Divergence detection is signal-only, not authoritative.** Step 7.9 uses count-delta heuristic
   (Lane 3 unique VariableIDs vs Lane 7 catalog count). Cannot detect if designer edits native Figma
   Variables without re-syncing Tokens Studio (blob `updatedAt` stays old). Sascha ping REQUIRED to
   confirm Tokens Studio source-of-truth policy.

5. **Third-library detection is signal-only.** 2 recurring non-Core keys (`4af67109a6a4`,
   `58b2fb9471fe`) suggest an unregistered third library referenced by Modules. Emitted as
   `third_library_suspect` for cross-team investigation.

## Design Decisions

Numbered for stable reference. Deterministic-era DDs supersede prior LLM-era DDs.

- **DD-3 — Framelink over official Figma MCP.** Pipeline runs headless, ruling out the official
  MCP's interactive OAuth. Framelink loses named Variable slash-paths, addressed by Lane 7's
  Tokens Studio catalog. Hybrid architecture: Framelink (headless PAT) for structural + Lane 7
  (`sharedPluginData`) for authoritative token names.

- **DD-5 — Ephemeral asset URLs never persisted; download immediately.** Figma MCP asset URLs
  expire, so `download_figma_images` writes files to disk in the same run and only the local path
  is recorded; `original_url` left empty. Persisted URL would be a dead reference.

- **DD-8 — PAT security: MCP layer only.** `FIGMA_PAT` injected via
  `StdioServerParameters(env=...)` into the MCP child process only — never in `session_state`
  (persisted to db + visible in REST responses), logs, or output.

- **DD-10 — `cwd` on StdioServerParameters.** Framelink server writes downloaded assets relative
  to its working directory, so the composition-mode + Library-mode extractors set `cwd` to the
  agent dir. Without it, downloads land nowhere and would silently fail.

- **DD-11 — Deterministic-first extraction (replaces prior DD-1 discovery-first / RULE 1).**
  Reading structured data from a structured API is not the LLM's job. Fixed orchestration in Python
  (Lane 5 → Lane 2 → per-set Framelink → Lane 3 → assets → typos → compose). Same-input →
  same-output, always. Anti-fabrication guard: every emission traces to REST payload node_id.

- **DD-12 — Two-mode extraction (Library + Composition).** Library-mode for published-library
  files (Core), Composition-mode with Pathway B for organism-composition files (Modules, clients).
  Different mechanisms because different file structures — publishing 0/0/0 to a library while
  organisms live only as page frames requires page-frame walk instead of library REST.

- **DD-13 — Version-pin discipline extends to invocation args.** `npx -y figma-developer-mcp`
  (unpinned) drifted from Dockerfile's pinned 0.13.2. Local resolved to cached 0.9.0; prod ran
  0.13.2 with different default format. Silent env parity break took hours to diagnose. Pin +
  format flag now explicit; schema-guard test catches silent format changes on future bumps.

- **DD-14 — UNBOUNDED N libraries with observability.** Lane 6 supports any number of registered
  foundation libraries per client extraction. No hard architectural limit. Observability tracks
  per-library resolution rate + resolution latency; empirical monitoring is the safety net rather
  than static bounds. If load reveals issues, bounds can be added retroactively.

- **DD-15 — Streaming Lane 6 resolution.** Cross-file resolution emits SSE events as they occur
  (8 event types) rather than batch-collecting-then-emitting. Aligns with existing direct-REST
  background+SSE workflow primitive. Enables real-time downstream progress observability; batch
  bridge available for non-streaming consumers via `resolve(...)`.

- **DD-16 — Tokens Studio via `sharedPluginData` as authoritative catalog source.** Figma Variables
  REST API is Enterprise-gated for our tier (403 on `/v1/files/{key}/variables/local`); Enterprise
  dependency violates the productization constraint. Tokens Studio catalog lives in
  `document.sharedPluginData.tokens` — PAT-accessible with `file_content:read` (which we have).
  Full 720 authoritative tokens without Enterprise.

- **DD-17 — Reconciliation contract downstream, not in extractor.** Lane 1 (usage-derived) and
  Lane 7 (authoritative catalog) use different naming conventions with no clean join key.
  Extractor emits both separately + explicit `reconciliation_contract` policy so all downstream
  stations reconcile consistently. Prevents divergent semantics across consumers.

- **DD-18 — Contract-driven parsing, NOT shape-assumed.** External-dependency schema version-check
  informs parsing decisions. Tokens Studio blob uses non-`$` legacy keys (`name/value/type`, NOT
  `$name/$value/$type`) despite `tokenFormat=dtcg` claim. Parser branches on Tokens Studio version;
  major bump escalates rather than silently continues. Applies same discipline as DD-13 to schema
  contracts beyond invocation args.
