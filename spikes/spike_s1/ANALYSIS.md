# Spike S1 — Track A: Official Figma MCP Extraction — ANALYSIS

**Date:** 2026-06-29
**Track:** A (official Figma MCP `https://mcp.figma.com/mcp`, called directly by Claude Code via OAuth — NOT Agno, NOT PAT)
**Target file:** `Helix_Design` — key `8qPSyetzviLR6eF6bkpL44`
**Target node:** `216-152` (page) → content frame `216:157` ("Cover", 1920×1080)

> ⚠️ **HEADLINE CAVEAT:** The target file is a **single 1920×1080 marketing cover slide** — *not* a design system / component library. It has **one page, zero Figma Variables, zero components/variants**. So this spike validates the **MCP plumbing and the shape of the output**, but it **cannot** exercise the parts of the pipeline that matter most (token extraction, component mapping). Conclusions about Token Normalizer / Component agents are extrapolated and flagged as such. **A second extraction against a real design-system file is required** (see §9).

---

## 1. OUTPUT STRUCTURE (per tool)

| Tool | Shape | Notes |
|---|---|---|
| `get_metadata` | **XML** tree (node id, type, name, x/y/w/h only) | No styles/values. Good for discovery + drill-down. Page node (`216:152`) returns near-empty; the real tree is under frame `216:157`. With no `nodeId` → lists top-level pages. |
| `get_design_context` | **React + Tailwind JSX code string** + asset-URL constants + a trailing block of server instructions | This is the primary payload. NOT raw Figma JSON, NOT design tokens — it is *opinionated generated code*. Includes `data-node-id` attributes for traceability. |
| `get_variable_defs` | **JSON map** `{name: value}` | Returned **`{}` (empty)** here. On the page node it errored `"nothing selected"` — the tool needs a concrete frame/layer (or desktop selection), not a page/canvas id. |
| `get_screenshot` | **Short-lived PNG URL** + curl instructions + metadata (`width/height`, `original_width/height`) | Token-cheap by default (URL, not base64). Downloaded 1920×1080 RGBA PNG (56 KB), visually faithful to the code. |

**Key structural insight:** `get_design_context` is a *code generator*, not a data extractor. It bakes design decisions into Tailwind utility classes with absolute positioning and arbitrary-value literals (`text-[180px]`, `text-[#b5df2d]`, `top-[536px]`). The server itself flags "SUPER CRITICAL: convert to target stack" — i.e. the output is a starting draft requiring transformation, not ground truth.

## 2. TOKEN EXTRACTION QUALITY

**Result for this file: zero tokens.** `get_variable_defs` → `{}`. There are **no bound Figma Variables**; every value is a hardcoded literal embedded in Tailwind classes:

- color: `bg-black`, `text-black`, `bg-white`, `text-[#b5df2d]`
- type: `font-['Science_Gothic:Bold']` / `:Regular`, `text-[180px]`, `text-[120px]`
- layout: `left-[95px]`, `top-[536px]`, `size-[1725px]`, etc.

**Distance to W3C DTCG / TokenRegistry v0.1:** N/A on this file (nothing to map). **What we *can* infer about the tool contract:** when variables *do* exist, `get_variable_defs` returns a flat `{path/name: value}` map (e.g. `{'icon/default/secondary': '#949494'}` per the tool's own doc). That is **light-to-medium** mapping to DTCG: the slash-path naming (`icon/default/secondary`) maps cleanly to nested token groups, but the flat map gives **no type metadata** (is `16` spacing, fontSize, radius?) and **no `$type`/`$value` structure**. A Token Normalizer would need to **infer token type from name/usage** and re-nest the flat keys — that's real work, not a trivial passthrough.

> ⚠️ Unverified against a variable-rich file. This is the single most important thing the next spike must measure.

## 3. COMPONENT IDENTIFICATION

**None present.** The frame contains primitives only: two flattened SVG images (`helix-svgrepo-com 1/2`), masked rectangles (`Rectangle 1/2`), and two text layers. No `INSTANCE` nodes, no component sets, no variants, no props. `get_design_context` emits a single functional component `Cover()` named after the frame — i.e. it wraps *whatever frame you point at* in one React function; it does **not** surface Figma's own component/variant model as structured data. To map Figma components → Web Component scaffolds we would rely on `get_metadata` (layer type `INSTANCE`/`COMPONENT`) plus possibly `get_code_connect_map` — **neither exercised here** because the file has no components.

## 4. DESIGN CONTEXT RICHNESS

**Captured well (static visual fidelity):** exact geometry (px positions/sizes), fill colors, font family + weight + size, layering/z-order, alpha masks (`maskImage`, `mask-size`, `mask-position`), and named layers carried as `data-name`.

**Absent (by nature of a static cover):** responsive behavior (everything is absolute px against 1920×1080), interaction states (hover/active/disabled/focus), auto-layout/flex intent, breakpoints, animation/motion, accessibility annotations (alt text emitted as `alt=""`), and semantic roles. Auto-layout, if present in other files, is *flattened* into absolute positioning here — a known lossy behavior to watch.

## 5. CONSISTENCY (see CONSISTENCY.md)

**Code = deterministic; asset URLs = non-deterministic.** Across 3 runs (+ the Phase-2 call) the JSX, classNames, literals, `data-node-id`s, text, and trailing instructions were **byte-identical**. The **only** variance: each call mints fresh `figma.com/api/mcp/asset/<uuid>` URLs (signed, ~7-day TTL). **Pipeline rule:** treat the code as cacheable/diffable; download assets immediately and rewrite `src` to a permanent path — never persist the MCP asset URL as stable.

## 6. COVERAGE

For *this* cover slide: **~90–95%** of what a dev needs to reproduce the static visual is in the combined output (code + screenshot). Residual manual work: real font licensing/availability ("Science Gothic"), accessible alt text, and converting absolute layout to responsive.

For a *real product UI / design system*: **coverage is unmeasured and expected to be materially lower** — tokens (untested), component/variant semantics (untested), states & interactions (absent), responsive intent (lossy). Do not generalize the 90% figure.

## 7. GAPS (what the pipeline needs that the output lacks)

1. **Structured design tokens with type metadata** — flat name→value map at best; no `$type`. Needs inference.
2. **Component/variant/prop model as data** — not surfaced; must be reconstructed from metadata + Code Connect.
3. **Interaction states** (hover/active/disabled/focus) — entirely absent.
4. **Responsive / auto-layout intent** — flattened to absolute px.
5. **Accessibility** — no roles, no meaningful alt text.
6. **Asset permanence** — ephemeral URLs; mandatory immediate download step.
7. **Motion/animation** — none (would need `get_motion_context`, untested).

## 8. FIGMA FILE ASSESSMENT

**This file is poorly suited as a design-system extraction baseline** (though fine as a "does the pipe work?" smoke test):

- ✅ Layers are sensibly named (`Cover`, `HELIX Design`, `AI ENABLED`, `helix-svgrepo-com`).
- ✅ Single clean frame, predictable geometry.
- ❌ **Zero Figma Variables** (no tokenization).
- ❌ **Zero components / variants / instances**.
- ❌ Logo is a **flattened vector→image** (7 raw `<vector>`s collapsed into an `<img>` asset), so even the brand mark isn't a reusable component.
- ❌ One page only — no system, no library, no states.

It is a brand cover, not a UI kit. Good for proving MCP+OAuth works; wrong artifact for assessing agent feasibility.

## 9. RECOMMENDATION (agent design + scope)

**On the MCP itself — green light on plumbing:**
- Official Figma MCP over OAuth works headless-ish from Claude Code; 4 read tools behave per spec; output is stable. The **Figma Extractor agent** can rely on: `get_metadata` (discover tree) → `get_design_context` (code+context) → `get_variable_defs` (tokens) → `get_screenshot` (visual check). Build in a **mandatory asset-download+rewrite step** and a **page/frame-id resolution step** (page ids fail `get_variable_defs`; resolve to a concrete frame first).

**On the agents — scope realistically, but RE-SPIKE before committing:**
- **Token Normalizer:** expect a flat `{path:value}` map, not DTCG. Plan for **type inference + re-nesting**, not a passthrough. *Cannot be validated on this file* — needs a variable-rich file.
- **Component/Scaffold agent:** `get_design_context` gives a per-frame React draft, not a variant-aware component model. To produce real Web Component scaffolds, combine `get_metadata` (instance/component detection) + Code Connect. *Untested here.*
- **`get_design_context` is a draft, not truth** — the server explicitly says "convert to target stack." Agents must treat it as input to transformation, with the screenshot as the visual oracle and `data-node-id` as the join key.

**Next action (blocking the agent-design decision):** Re-run this exact 4-phase spike against a **real HELIX design-system file that actually defines Figma Variables and component sets** (target a frame containing component instances + bound tokens). That run — not this cover — is what should drive Token Normalizer and Component agent scope.

---

### Artifacts in `spike_s1/`
- `tools_manifest.json` — tool names, schemas, server config
- `metadata_node_216-152.xml` — `get_metadata` tree
- `design_context_node_216-152.json` — `get_design_context` code + assets + literals
- `variable_defs_node_216-152.json` — empty `{}` + the page-node selection-error finding
- `screenshot_node_216-152.png` — 1920×1080 render (visually verified)
- `design_context_run_{1,2,3}.json` + `CONSISTENCY.md` — determinism evidence
- `ANALYSIS.md` — this report
