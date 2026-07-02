# Cross-check: Dirk's node-reference list vs MCP metadata (authoritative)

Source of truth = the `name` attribute on each `<canvas>` returned by get_metadata.
Dirk's addendum list (`spike:S1-track-a-node-reference`) was hand-assembled from Figma UI dev-mode share links.

## RESULT: All node IDs valid; Dirk's NAME labels are systematically OFF-BY-ONE.

| nodeId | Dirk's inferred name | MCP authoritative name | content-verified? |
|---|---|---|---|
| 216:152 | Cover | Cover | ✓ (Cover slide, initial spike) |
| 54:2 | Governance Process | **Text** | — |
| 600:425 | Text | **Typography** | ✓ extracted heading tokens |
| 78:136 | Typography | **Icons** | — |
| 360:38 | Icons | **Colors** | ✓ swatch template page |
| 105:2147 | Colors | **Layout** | ✓ extracted breakpoint/margin/gutter/columns |
| 152:3814 | Layout | **ImageRatios** | — |
| 75:10 | ImageRatios | **❇️ Basic Components (header)** | — |
| 457:249 | Playground/Basic Components | **Checkbox** | — |
| 57:645 | Checkbox | **Button** | ✓ extracted Variant×Size×State matrix |
| 457:729 | Button | **Dropdown** ("Drodown" typo) | — |
| 86:985 | Dropdown | **FormField** | — |
| 86:942 | FormField | **HelperRow** | — |
| 100:1426 | HelperRow | **Input** | — |
| 457:324 | Input | **RadioButton** | — |
| 86:882 | RadioButton | **LabelRow** | — |
| 552:985 | LabelRow | **Toggle** | — |
| 736:31 | Toggle | **Chips** | — |

## Conclusions
1. **MCP returns the FULL page tree** — every ID Dirk listed appears, PLUS extras not in his list (Governance Process 245:273, Playground 0:1, dividers, Modules: ContentBlock 182:5072, MediaText 152:3809). The "is MCP returning the full tree?" check = PASS.
2. **Never trust hand-assembled name↔id mappings.** Resolve names from MCP metadata. My Phase 1/2 extractions used MCP-authoritative IDs, so the right pages were extracted (content-verified for Typography, Colors, Layout, Button).
3. Reinforces lesson `figma-mcp-file-level-first`: discovery-first, names from metadata only.
4. Extra quirk found this run: get_metadata with NO nodeId returned ONLY the first page ("Cover"); the FULL list came from the invalid-node-id ERROR path. Use a known page id or the error path to enumerate all pages reliably.
