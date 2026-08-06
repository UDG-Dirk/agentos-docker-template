# HELIX UC2 PoC — Architecture

HELIX UC2 is a Figma-to-design-system agentic pipeline: it derives design **tokens + components + CSS** from a client's Figma file, presents them via Storybook, and delivers them to a CMS. This doc is the shared pipeline-level context; per-agent specifics live in `agents/<agent>/README.md`.

---

## Pipeline Flow

```
Agno Workflow (headless: reads Figma with the Framelink community MCP + a Personal Access Token):

  Step 1  Figma Extractor      → FigmaExtractionResult          [BUILT + DEPLOYED]
                                  deterministic, ZERO LLM
  Step 2  Token Normalizer     → W3C DTCG tokens                [BUILT + DEPLOYED]
                                  pauses for a human review
  Step 3a Baseline Reader      → baseline inventory             [BUILT + DEPLOYED]
  Step 3b Semantic Matcher     → client→baseline match          [LIBRARY ONLY — no endpoint yet]
  Step 3c Theme Generator      → forked, customer-branded package [DEPLOYED, HISTORICAL — see note]
  Step 3d Component Code Generator → Lit + TypeScript components [BUILT + DEPLOYED]
  Overlay Packager             → opens the delivery MR          [PLANNED]
  Step 5  Quality Gate         → validation (no LLM)            [PLANNED]
  Step 6  Git Push             → GitLab REST API commit          [PLANNED]
```

**Step 1 is deterministic — no LLM in the extraction path.** It reads structured Figma data through
fixed Python "lanes" (structure/values, published taxonomy, variable bindings, versions, cross-file
references, and a Tokens Studio token catalog) — the earlier LLM-driven extractor was removed because it
invented component names that weren't in the file. Details: [`../agents/figma_extractor/README.md`](../agents/figma_extractor/README.md).

> **Deployed workflows** (all on Coolify): `helix-figma-extractor` (single published-library file),
> `helix-client-extractor` (a client file resolved against its libraries), `helix-composition-only-extractor`
> (a standalone/unpublished file), `helix-baseline-reader` (Step 3a on its own), `helix-theme-generator`
> (Step 3c) and `helix-component-code-generator` (Step 3d) — six workflows registered in `app/main.py`.
> Step 3b (Semantic Matcher) exists as a tested library, not yet a running endpoint — see **Output Model**
> below. The overlay packager (assembling everything into a delivery MR) is the one remaining planned step.

---

## Single Source of Truth — the derived design system

The **derived design system is the SSOT — not Storybook.** Storybook is the presentation layer; it renders
what the design system contains, it does not own it.

The SSOT is the full, npm-distributable artifact tree:

| Layer | Artifact |
|---|---|
| Tokens | W3C DTCG JSON (`.tokens.json`) — the foundation |
| Build | Style Dictionary config + scripts (`style-dictionary.config.js`, `npm run build:tokens`) |
| CSS | generated custom properties, theme overrides, per-client variant CSS |
| Components | Vue SFCs (baseline + client-specific new components) |
| Stories | Storybook `.stories.ts` (presentation only) |
| Package | `package.json`, `dist/`, `tokens/`, `components/`, `themes/` |

**Additive model:** Figma files are *inputs processed additively* into the existing design system, never
full replacements. Tokens (DTCG JSON, deep-mergeable by design) → Style Dictionary → CSS; components
consume tokens via CSS custom properties; Storybook presents the result. Provenance is tracked per
token/component via the `de.msqdx.helix` DTCG `$extensions` vendor key
(`generated-by:helix-pipeline` | `modified-by:human` | `authored-by:human`).

> Three additive-pipeline policy decisions remain open before a *second* Figma file enters the pipeline:
> token collision resolution (fail-loud+HITL vs last-write-wins), component-name collision (namespace vs
> HITL), and design-system drift detection.

---

## Output Model — Delta + Theming (GAP-09)

HELIX is a **delta-plus-theming** system, **not a greenfield generator**. It does not regenerate a design
system from scratch each run; it compares the extraction against an existing baseline and emits the delta.

- **Existing baseline components → CSS theme overrides** (into `themes/<client>/`), not new Vue SFCs.
- **New Vue SFCs only for genuinely new components** that don't exist in the baseline (and they must
  conform to the baseline's conventions).

The single "Component Scaffolder" (old Step 3/4/5) splits into four capabilities, now at different
stages of maturity:

| Step | Capability | Role | Status |
|---|---|---|---|
| 3a | **Baseline Reader** | read the existing baseline design-system repo into an inventory (tokens, components, conventions) | **Built + deployed** — runs alongside Step 1 and as the standalone `helix-baseline-reader` workflow |
| 3b | **Semantic Matcher** | match each client token/component to its baseline counterpart, with a confidence score and a human-review flag when unsure | **Library only** — the scoring logic is built and tested (`agents/semantic_matcher/`); no running endpoint/workflow yet |
| 3c | **Theme Generator** | fork the baseline into a customer package and substitute token values (see **Architecture B** below) | **Deployed, historical** — `helix-theme-generator` still runs, but is no longer a planned station; its job is being folded into the overlay packager (not yet built) |
| 3d | **Component Code Generator** | fork existing baseline components verbatim (Path A), or generate new ones from the Figma spec (Path B) | **Built + deployed** — `helix-component-code-generator`; generates Lit + TypeScript, not Vue — see [`agents/component_code_generator/README.md`](../agents/component_code_generator/README.md) |

Step 3b's matching quality still benefits from more real client Figma files (Bausch und Ströbel, GEMÜ,
Bosch) to widen its tested pattern space before it's wired as a live endpoint. Steps 1–2 (Extractor,
Normalizer) and 3a are independent of that. (Tracked internally as gap GAP-09; no public gap
register exists in this repo to link to.)

---

## Architecture B — how customer branding is designed to work

Earlier design (**Architecture A**, superseded) planned a thin customer package that only held the
diff from baseline. The pipeline instead ships **Architecture B**: a **full fork of helix-code**, with
customer branding applied as a **token value swap**, not a rename.

Concretely, when Step 3d forks an existing baseline component (**Path A**):

- The component's tag (e.g. `hx-button`), class name (e.g. `HxButton`), and CSS variable references
  (e.g. `var(--helix-color-primary)`) are copied **byte-for-byte, unchanged**.
- Only the **value** behind each CSS variable is meant to change later, in the fork's Style
  Dictionary (the tool that turns design-token JSON into CSS) — the variable *name* stays
  `--helix-*`. **This value-swap step itself is not built yet** — it's filed as Phase 2 of the
  overlay-packager work (see below) and hasn't run end-to-end. What's confirmed live today is the
  fork step's *preservation* of the unchanged names (Path A, next paragraph).
- This replaced an earlier approach that renamed those references (e.g. `--helix-*` →
  `--acme-*`). That broke components whose styling depends on the unrenamed `--helix-*` variables
  defined elsewhere in the fork — the renamed references pointed at nothing. Fixed 2026-08-03.

For components that don't exist in the baseline yet, **Path B** generates new Lit + TypeScript source
from the Figma spec directly (LLM-assisted, validated against a structural gate before it's accepted).

The **overlay packager** (not yet built) is the step that will assemble every generated file — forked
components, generated components, the customer token values — into one client fork of helix-code and
open the delivery merge request for the front-end team to review.

---

## session_state Contract

Passed as a **dict** (not a Pydantic model) to `workflow.run(session_state=...)`.

| Field | Type | Notes |
|---|---|---|
| `figma_file_key` | `str` | Figma file identifier |
| `storybook_repo_url` | `str` | GitLab HTTPS URL |
| `cms_repo_url` | `str` | GitLab HTTPS URL |
| `cms_type` | `"storyblok"` | Extensible |
| `framework` | `"vue"` | Extensible |

**FORBIDDEN:** Never put PATs or any secret in `session_state` — it is persisted to the db and visible in REST API responses. Secrets are injected at the MCP/tool layer only.

---

## Hybrid OAuth/PAT Extraction Architecture

The pipeline uses two Figma MCP clients with complementary roles:

- The **OFFICIAL Figma MCP** returns rich semantic data (named Variables with `$type`, Code Connect, designer docs) but requires interactive OAuth — unsuitable for a headless pipeline.
- The **FRAMELINK community MCP** (`figma-developer-mcp`) runs headless with a PAT but loses the named Variable slash-paths, Code Connect, and designer docs (it reconstructs CSS-var names + resolved values + geometry).

**Resolution (as designed):** the application layer does OAuth once per project and extracts the rich data as **enrichment**; the pipeline runs Framelink+PAT per run; enrichment bridges the gap. (See decision: `hybrid-figma-extraction-architecture`.)

> **Today:** only the Framelink+PAT half runs. The OAuth/enrichment half is not wired (see the inactive
> note under *Enrichment Concept*). What the deterministic extractor recovers instead of the OAuth
> Variable names is the **Tokens Studio token catalog**, read straight from the file's plugin data — an
> authoritative token list without needing OAuth or an Enterprise Figma tier.

### Custom Elements Manifest (CEM) delivery

Step 3a (Baseline Reader) needs the baseline's Custom Elements Manifest — a generated file that is *not*
committed to the baseline repo. A GitLab CI job (`build-cem`) clones the baseline read-only, builds the
manifest with `pnpm`, and publishes it to the project's package registry at a stable address. On startup
the container fetches that file (`scripts/fetch_cem.py`, driven by the `HELIX_CODE_CEM_*` environment
variables) so the reader serves real component data. If the fetch is unset or fails, the reader falls
back loudly to a "manifest missing" state rather than guessing. Details:
[`../agents/baseline_reader/README.md`](../agents/baseline_reader/README.md).

---

## Enrichment Concept

> **Currently inactive.** "Enrichment" (the extra naming/typing layer described below) is **not populated
> by today's deterministic pipeline** — `enrichment_coverage` is hard-set to `0.0` and no token carries
> enrichment fields. It's kept in the schema, and documented here, as a reserved concept for a future
> Code Connect / Variable-slash-path integration. Treat the table below as the *intended* design, not
> current behaviour.

| Aspect | Detail |
|---|---|
| **What** | Variables taxonomy + `$type`, Code Connect Vue snippets, designer agent-instructions, composite alias tokens (`Font()`/`Effect()`) |
| **From** | Application layer via official Figma MCP (OAuth) |
| **Stored** | Project knowledge base artifact (format TBD; currently the `spike_s1/` extraction files act as a proxy) |
| **Consumed** | Agents load it as **instruction context** (not an Agno Knowledge object — that avoids the LiteLLM knowledge-search tool round-trip) |
| **Optional** | Without enrichment, `enrichment_coverage=0.0` and `$type` metadata is unavailable |

---

## Workflow Integration (Agno)

- **Typed Steps** with Pydantic `output_schema` models flowing between steps (Step N reads `previous_step_content`).
- **HITL gates:** Step 2 (Token Normalization) uses `requires_output_review=True` → run pauses (status `PAUSED`). Resume via REST `POST /workflows/{id}/runs/{run_id}/continue` with the `StepRequirement` confirmed.
- `session_state` passed as a dict (use `.model_dump()` if starting from a model).
- **REST API:**
  - `POST /workflows/{id}/runs` — form field is `message`, not `input`
  - `GET .../runs/{run_id}`
  - `POST .../continue`, `.../resume`, `.../cancel`
- `db=` (agentos-db Postgres) is **REQUIRED** for HITL resume — without it, pause works but `continue_run` raises `ValueError`.
- Use `OpenAIChat`, never `OpenAIResponses` (`OpenAIResponses` breaks tool round-trips via LiteLLM).

---

## Key Architectural Decisions

| Decision | Why |
|---|---|
| Sequential extraction, not broadcast | Equal quality at roughly half the cost/latency, with full observability |
| Vue for the PoC | Matches the file's Code Connect (Lit / Web Components is the production target) |
| Storyblok as CMS target | — |
| Agno Workflow as the pipeline spine | — |
| GitLab REST API for output | Needs a Personal Access Token with `api` scope |
| Secrets in Coolify env vars | The pipeline now lives in one repo, `msq-turbo/helix-agents` (transferred from the earlier layout) |

---

## Known Scaling Constraints

| Constraint | Detail | Status |
|---|---|---|
| **Context bloat** *(historical — LLM extractor only)* | The old LLM-driven extractor accumulated ~37.5K tokens per Figma call in one context (~540K for the 17-page test file). The **deterministic Step-1 rewrite removed this** — there is no LLM in the extraction path anymore, so it no longer applies. Kept here for history. | Resolved by the deterministic rewrite |
| **Delta extraction** | Re-runs extract the whole file every time; a memory-based change-detection optimization is planned. | Planned |
| **No checkpointing** | A run isn't resumable: a mid-run failure loses prior work. (The sustained-rate-limit guard does retry within a run.) | Planned |
