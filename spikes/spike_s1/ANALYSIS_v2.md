# Spike S1 — Track A FULL extraction — ANALYSIS v2 (real design-system data)

**Date:** 2026-06-29 · **File:** `Helix_Design` `8qPSyetzviLR6eF6bkpL44`
**Supersedes** `ANALYSIS.md` (Cover-only). Covers token pages (Colors, Typography, Layout, Icons) + 6 components (Button, Checkbox, Dropdown, FormField, Input, Toggle).
**Method:** main session did discovery + Typography/Layout/Button; 4 parallel subagents extracted the rest (raw files saved alongside this report).

> **Headline reversal:** The initial spike concluded "zero Variables, hardcoded values." That was an artifact of the Cover slide. **This file is a genuine, well-structured design system with a rich bound Figma Variable system.** `get_variable_defs` returns only the variables *consumed by the queried subtree* — querying empty/template/page nodes returns `{}`; querying token-consuming component frames returns the real token set.

---

## 1. OUTPUT STRUCTURE (per tool) — confirmed/updated

| Tool | Shape | Update vs v1 |
|---|---|---|
| `get_metadata` | XML tree (id/type/name/geometry). NO-nodeId lists only the FIRST page; full page list comes from the invalid-node ERROR path. | Component pages expose the **full variant matrix as named `<symbol>`s** (`Variant=…, Size=…, State=…`). |
| `get_design_context` | When Code Connect is mapped → **CodeConnectSnippet + TS props type + designer-authored description ("Agent instructions", "Token references")**, framework = **Vue** (`import Icon from "src/components/Icon/Icon.vue"`). When not mapped/empty → React+Tailwind fallback. | v1 only saw the Tailwind fallback (Cover had no Code Connect). |
| `get_variable_defs` | Flat `{name: value}` map of variables **consumed by the subtree**. Composite values are stringified `Font(...)` / `Effect(...)` that embed references to other token names. | v1 saw `{}` (Cover hardcoded). Real pages return rich maps. |
| `get_screenshot` | short-lived PNG URL (unchanged). | — |
| `search_design_system` (**newly used**) | Published-variable catalog WITH typed metadata: `variableType` (FLOAT…), `scopes` (CORNER_RADIUS…), `variableCollectionName` ("semantic"), `libraryName` ("Helix_Design"), keys. | NEW — the only source of `$type`/scope/collection metadata. |

## 2. TOKEN EXTRACTION QUALITY — NOW MEASURABLE → **high, DTCG-mappable**

**Yes, Variables are defined and extensive.** Observed union (across consumed frames):

- **Color** (`colors/{family}/{role}/{state}`): `control/{background,border,text-icon}/{default,hover,active,disabled,inactive,inactive-hover,neutral,bold,muted,inverse}`; `interactive/{surface,border,icon,link,focus,transparent}/{default,hover,active,disabled,inverse,subtle-hover,on-surface,inner,outer}`; `error/{border,icon,borde-subtle*,surfac-subtle*}`; `success/{icon,surface,surface-hover}`; `text-icon/{primary,brand,bold,muted,disabled}`; `surface/default`. Hex values, e.g. `interactive/surface/default:#3388f0` (hover/active `#1971c2`/`#1864ab`), `text-icon/brand:#125e9d`, `success/icon:#2f9e44`.
- **Dimension**: `typography/{heading,body,link}/{xl..xs}/{size,lineheight,lineheight-paragraph,regular,strong,weight}`; `spacing/components/{0,5xs:2,4xs,3xs:8,2xs:12,xs:16,sm:20,md:24}`; `size/control/{xs:24,sm:32,md:40,lg:48,xl:56}`; `size/icon/{2xs:8,sm:16,md:20,lg:24}`; `radius/{round:9999,sm,input,control-input}`; `stroke-weight/{sm,md:2}`; `layout/{breakpoint:1536,margin:48,gutter:24,columns:12}`.
- **Component**: `component/radius/{button:9999,button-input:4,input,checkbox}`.
- **Font families (TWO)**: `font/family/heading:"roboto"`, `font/family/body:"dm sans"`.
- **Composite / alias tokens**: `heading|body|link/{size}/{regular|strong}` = `Font(family: "font/family/heading", size: dimension/typography/.../size, weight: …, lineHeight: …)`; `FocusRing` / `ErrorRing` = `Effect(type: DROP_SHADOW, color: colors/interactive/focus/outer, spread: 4; …inner, spread: 2)`. **These embed references to other token names** — i.e. genuine token aliasing.

**Distance to W3C DTCG / TokenRegistry v0.1 — LIGHT-to-MEDIUM, rule-based:**
1. Slash-paths → nested groups: `dimension/typography/heading/xl/size` → `dimension.typography.heading.xl.size`. **Clean, deterministic.**
2. Infer `$type` from path prefix (`colors/`→color, `dimension/…/size|spacing|radius`→dimension, `font/family/`→fontFamily, weights→number/fontWeight, `Font(...)`→typography, `Effect(...)`→shadow). `get_variable_defs` carries **no `$type`** — but `search_design_system` does (FLOAT + scopes), so combine both sources.
3. Add units: raw `"48"`→`48px`, `"9999"`→pill/`9999px`, weights stay unitless.
4. Resolve references: the `Font(...)`/`Effect(...)` strings → DTCG `{alias.path}` references + composite `$type:"typography"|"shadow"` objects. Requires a **parse + alias-resolution pass** (the one non-trivial step).
5. Reconcile two naming worlds (see §7) and normalize typos.

→ **A Token Normalizer agent is very feasible**: deterministic path-mapping + type inference + unit addition + a composite/alias parser. Not heavy ML; structured transformation.

## 3. COMPONENT IDENTIFICATION — NOW MEASURABLE → **full variant/state matrix exposed**

`get_metadata` surfaces component sets as named `<symbol>` nodes with variant axes in the name. Evidence:
- **Button** (`57:766`): `Variant={Solid,Solid-Input,Outline,Ghost} × Size={xl,lg,md,sm} × State={Default,Hover,Active,Focus,Disabled}` (~55 symbols) + parallel **IconButton** set + **ButtonGroup** (`Spacing={0,sm,md}`).
- **Checkbox** (`457:249`): control + **error** state families + `ErrorRing`/`FocusRing`; `component/radius/checkbox`. 24 tokens.
- **Dropdown** (`457:729`, "Drodown"): 46 tokens; body typography sm/md/lg, `font/family/body`, `component/radius/input`, `interactive/surface/subtle-hover`.
- **FormField** (`86:985`): **composite** — composes label/input/helper; unions body+link+control+interactive; `text-icon/muted`. 44 tokens.
- **Toggle** (`552:985`): `control/background/{inactive,inactive-hover,disabled}` + **success** family + `FocusRing`; `radius/round`. Full vs switch-only frames give different subsets.
- **Input** (`100:1426`): **not instantiated in this working file** — it's a *published library* component_set (`componentKey a0933d5e…`); `get_variable_defs` can't union it. Tokens recovered only via `search_design_system` (`dimension/radius/input`, `dimension/radius/control-input`, collection "semantic").

→ Components map cleanly to Web-Component scaffolds: **Variant/Size/State axes → props**; the State axis means hover/active/focus/disabled are all first-class.

## 4. DESIGN CONTEXT RICHNESS → **states captured; plus Code Connect + authored docs**

- **States:** `get_design_context` on one variant symbol returns THAT state only; the full set is enumerated via the metadata sibling symbols. So "all states" = metadata-driven enumeration, not a single call.
- **Code Connect (mapped to Vue):** returns `<CodeConnectSnippet>` + a TS props type + the real component import (`Icon.vue`). Seen on Button, FormField, Toggle, Dropdown.
- **Designer-authored documentation** is returned inline: Purpose, Variants, Sizes, States, **"Token references"** (a CSS-var contract: `--color-primary-500/600/700`, `--radius-pill`, `--space-2`, `--color-focus-ring`), and **explicit "Agent instructions"** (e.g. Button: *"Never hardcode any color/spacing/size value; Ghost wraps icon in circular pill; Disabled via --color-disabled-bg not CSS opacity; props: variant,size,disabled,leadingIcon,trailingIcon,label,onClick"*). **This is gold for the pipeline** — the design file ships its own agent contract.
- Still absent: responsive intent is flattened to absolute px (Layout breakpoint frames are empty guides — tokens present, no content); motion not probed.

## 5. CONSISTENCY — skipped (proven deterministic in initial spike; only asset URLs vary).

## 6. COVERAGE — reassessed
- **Tokens:** high — color/typography/spacing/size/radius/stroke/layout families + composites all extractable, PROVIDED you query consuming frames and union across components (+ `search_design_system` for published/typed catalog and non-instantiated components like Input).
- **Components:** high — variants, states, props, composition, and authored usage docs all available.
- **Residual manual work:** reconciling the two naming worlds (§7), fixing token-name typos, resolving Input/published-library tokens, responsive behavior, motion, true alt text.

## 7. GAPS / RISKS — with real evidence
1. **No single "all tokens" call.** `get_variable_defs` is subtree-scoped; the dedicated **Colors page returns `{}`** (it's an empty `Color Swatch Template` with placeholder `token-name`/`{raw_value}`). Full palette must be unioned from components or pulled via `search_design_system`.
2. **No `$type`/unit in `get_variable_defs`** — values are unitless strings; types come only from `search_design_system`.
3. **Two naming worlds:** Figma Variable slash-paths (`colors/interactive/surface/default`) vs the doc CSS-var contract (`--color-primary-500`). Normalizer must map between them (or pick one as canonical).
4. **Token-name typos in source:** `colors/control/border/disbled`, `colors/error/borde-subtle`, `colors/error/surfac-subtle`. Must normalize/flag.
5. **Published-library components not instantiated** (Input) are invisible to `get_variable_defs`; need `search_design_system` / library access.
6. **Possible value collisions:** `surface/hover == surface/active` (`#1971c2`); two color scales (`text-icon/brand #125e9d` vs `interactive #3388f0`).
7. Ephemeral asset URLs (unchanged): download immediately, rewrite `src`.

## 8. FIGMA FILE ASSESSMENT → **well-structured for automated extraction (beyond Cover)**
Strong: bound Variables with consistent semantic taxonomy; variant-organized component sets with an explicit State axis; composite typography/effect alias tokens; **designer-authored component docs + agent instructions**; **Code Connect wired to a Vue codebase**; published "semantic" collection. Weak: minor token-name typos; documentation pages (Colors, Icons) render templates/visuals rather than machine-readable data; Input only published (not placed); dual naming worlds. **Net: a high-quality, automation-friendly source — the opposite of the Cover-only impression.**

## 9. RECOMMENDATION — agent design with real data

**Figma Extractor agent (feasibility: HIGH).** Mandatory pipeline:
1. **Step 0 discovery** — file-level page tree (use a known page id or the invalid-node error path; the no-arg call under-reports). Resolve page → concrete frame before any `get_variable_defs` (page ids fail).
2. **Tokens** — call `search_design_system` for the typed published catalog, AND `get_variable_defs` on token-consuming component frames, then **union + dedupe**. Don't rely on the Colors page.
3. **Components** — enumerate the Variant×Size×State matrix via `get_metadata`; `get_design_context` per component for Code Connect snippet + the authored description/agent-instructions; download assets immediately.

**Token Normalizer agent (feasibility: HIGH, medium effort).** Deterministic transform: slash-path→DTCG nesting · `$type` inference (merge `search_design_system` types) · unit addition · **composite `Font()`/`Effect()` parser → DTCG `typography`/`shadow` + `{alias}` references** · typo normalization · reconcile Variable-paths vs `--css-var` contract (recommend Variable slash-paths as canonical, emit CSS-var aliases). Not ML-heavy.

**Component/Scaffold agent (feasibility: HIGH).** Consume the file's own **"Agent instructions" + props list** directly; map variant axes → component props/states; honor **Code Connect (Vue)** — aligns with the Storybook target-stack decision (`helix-poc-agno:decision:storybook-target-stack` — verify Vue/Web-Components fit). The embedded designer contract substantially de-risks scaffolding.

**Bottom line:** The pipeline premise is validated on real data. Token extraction and component scaffolding are both HIGH-feasibility; the real engineering is the Normalizer transform (path→DTCG + alias/composite resolution + naming reconciliation + typo handling) and disciplined asset/discovery handling — not raw capture.

---
### New artifacts (this run)
`page_index.json`, `CROSSCHECK_node_ids.md`, `colors_metadata.xml`, `colors_design_context.json`, `colors_palette_variable_defs.json` (`{}`), `typography_variable_defs.json`, `typography_copy_variable_defs.json`, `typography_design_context.json`, `layout_metadata.xml`, `layout_variable_defs.json`, `layout_design_context.json`, `icons_metadata.xml`, `icons_design_context.json`, `component_{button,checkbox,dropdown,formfield,input,toggle}_{metadata.xml,variable_defs.json,design_context.json}`.
