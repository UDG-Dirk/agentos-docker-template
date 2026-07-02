# Track B vs Track A — Button (node 57:645) extraction comparison

| Dimension | Track A (official MCP, OAuth) | Track B (Framelink, PAT, HEADLESS) |
|---|---|---|
| Auth | OAuth browser flow (human-in-loop) | **PAT, fully headless** (--figma-api-key) ✅ |
| Runs inside Agno agent? | n/a (Claude Code direct) | **YES** — MCPTools + StdioServerParameters, OpenAIChat |
| Tools | get_metadata, get_design_context, get_variable_defs, get_screenshot, search_design_system | get_figma_data, download_figma_images |
| Output shape | per-tool: XML metadata / React+Tailwind or Code Connect / {name:value} vars / PNG | one rich YAML: metadata+componentSets+components+nodes+globalVars |
| Variant matrix | full Variant×Size×State via symbol names | **full — 60 explicit variants** enumerated in COMPONENT_SET 57:766 ✅ |
| Design tokens | **named Figma Variables** (slash-paths) + composite alias tokens (Font(), FocusRing Effect); 47 names | **resolved hex/rgba values** + style ids (fill_1DOTQ5); 31 reconstructed --css-var names. NO named Variables. |
| Typed token catalog | YES — search_design_system ($type FLOAT, scopes, collection "semantic", libraryName) | **NO equivalent** |
| Code Connect | YES — Vue component snippet + TS props (Icon.vue) | **NO** (code_connect_present=false) |
| Designer docs / "Agent instructions" | YES — structured description block | Partial — only what's embedded in node/desc text; not a first-class field |
| Assets | get_screenshot (PNG URL) | download_figma_images tool |

## GAIN with Track B (community/PAT)
- Fully headless, PAT-injectable, zero human-in-loop → pipeline-automatable inside Agno. ✅ (the F1 question)
- Single-call rich YAML with the COMPLETE variant matrix, componentSets, and resolved global style values + image download tool.

## LOSE vs official MCP
- **Named Figma Variables / canonical token taxonomy** (slash-paths) → Track B returns RESOLVED values + style ids, and only heuristically reconstructed CSS-var names.
- **Composite alias tokens** (Font(...), FocusRing=Effect(...)) and their references.
- **Typed published catalog** (search_design_system: $type/scopes/collection) — official-only.
- **Code Connect** (Vue component mapping) and the first-class designer **Agent-instructions** block.

## NAMING NOTE
Track B's reconstructed names (--color-primary-500:#3388F0) align with Track A's designer **CSS-var contract**, NOT Track A's Figma **Variable slash-paths** (colors/interactive/surface/default). Same values, two name worlds — exactly the reconciliation the Token Normalizer must do.

## VERDICT
- **F1 PASS**: an Agno agent CAN call a PAT-based Figma MCP headlessly (Framelink, stdio). Proven end-to-end.
- **Output quality**: Track B is RICHER on raw structure/variants/resolved-values, POORER on semantic token naming/typing, and has NO Code Connect.
- **Recommendation**: HYBRID. Use Framelink (PAT, headless) as the pipeline backbone for geometry + full variant matrix + resolved values + assets. Use the official MCP (OAuth, one-time/interactive enrichment) for the canonical Variable taxonomy + typed catalog (search_design_system) + Code Connect + designer agent-instructions. If a fully-headless pipeline must avoid OAuth entirely, Framelink alone is sufficient for scaffolding geometry/variants but the Token Normalizer must derive the token registry from resolved values + the designer CSS-var contract (losing $type/alias fidelity).
