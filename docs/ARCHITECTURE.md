# HELIX UC2 PoC — Architecture

HELIX UC2 is a Figma-to-design-system agentic pipeline: it derives design **tokens + components + CSS** from a client's Figma file, presents them via Storybook, and delivers them to a CMS. This doc is the shared pipeline-level context; per-agent specifics live in `agents/<agent>/README.md`.

---

## Pipeline Flow

```
Application Layer (OAuth + project config)
  └─ Enrichment extraction via OFFICIAL Figma MCP (OAuth):
       Variables taxonomy + $type, Code Connect Vue snippets,
       designer agent-instructions, composite alias tokens
         └─ stored as the project knowledge base
              (format TBD; currently spike_s1/ proxy files)

Agno Workflow (headless, Framelink MCP + PAT):

  Step 1  Figma Extractor      → FigmaExtractionResult          [BUILT + DEPLOYED]
  Step 2  Token Normalizer     → W3C DTCG tokens                [BUILT + DEPLOYED]
                                  [HITL: requires_output_review]
  Step 3  Component Scaffolder → theme CSS + new Vue SFCs       [PLANNED — restructured,
                                  + Storybook stories             see Output Model below]
  Step 4  CMS Model Generator  → Storyblok bloks                [PLANNED]
  Step 5  Quality Gate         → validation (function steps,    [PLANNED]
                                  zero-LLM)
  Step 6  Git Push             → GitLab REST API                [PLANNED]
                                  (api-scope PAT, atomic
                                  multi-file commit)
```

> Steps 1–2 are built and **deployed to prod** (Agno Workflow `helix-figma-extractor` on Coolify);
> Steps 3–6 are planned. Step 3 (Component Scaffolder) is being restructured — see **Output Model** below.

---

## Single Source of Truth — the derived design system

The **derived design system is the SSOT — not Storybook.** Storybook is the presentation layer; it renders
what the design system contains, it does not own it. (See `decision:storybook-as-source-of-truth`.)

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

**Planned restructure (not yet built):** the single "Component Scaffolder" (old Step 3/4/5) splits into
four capabilities:

| Step | Capability | Role |
|---|---|---|
| 3a | **Baseline Reader** | index the existing baseline repo (tokens, components, interfaces) |
| 3b | **Semantic Matcher / Reconciler** | LLM agent; map client → baseline with confidence + HITL flags |
| 3c | **Theme Generator** | produce CSS overrides for `themes/<client>/` |
| 3d | **New Component Scaffolder** | scaffold only genuinely new components, to baseline conventions |

This restructure is **pending Sascha's baseline repo + a three-way meeting**, and the Semantic Matcher
needs multiple client Figmas (Bausch und Ströbel, GEMÜ, Bosch) to validate its pattern space. Steps 1–2
(Extractor, Normalizer) are unaffected. (Source: FE-DEV gap register, GAP-09.)

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

**Resolution:** the application layer does OAuth once per project and extracts the rich data as **enrichment**; the pipeline runs Framelink+PAT per run; enrichment bridges the gap. (See decision: `hybrid-figma-extraction-architecture`.)

---

## Enrichment Concept

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

| Decision | One-liner | Memory pointer |
|---|---|---|
| Sequential extraction, not broadcast | Equal quality, ~½ cost/latency, full observability | `agents:figma-extractor:step3-result`, `runs/COMPARISON.md` |
| Vue for the PoC | Matches the file's Code Connect (Lit/Web Components is the production target) | `decision:storybook-target-stack-update` |
| Storyblok as CMS target | — | `decision:cms-target` |
| Agno Workflow as pipeline spine | — | `lesson:agno-workflow-patterns`, `spike:S2-result` |
| GitLab REST API for output | PAT with `api` scope | `lesson:gitlab-pat-scopes-self-hosted`, `spike:S3-result` |
| Secrets in Coolify env vars (interim) | Agents stay in helix-poc-agno during PoC | `decision:agent-repo-location`, `infra:coolify-server-organization` |

---

## Known Scaling Constraints

| Constraint | Detail | Status |
|---|---|---|
| **Context bloat** | ~37.5K tokens per `get_figma_data` call, accumulating linearly in one context (~540K for the 17-page test file). Instruction-level fix disproven; real fix is a subagent-swarm architecture. | Planned — see `roadmap:context-decomposition-architecture` |
| **Delta extraction** | Re-runs extract the whole file every time; a memory-based delta-detection optimization is planned. | Planned |
| **No checkpointing** | Sequential extraction is not resumable: a mid-run failure loses all prior work. | Planned |
