# Phase 3 — Consistency / Determinism Test

Tool: `get_design_context`, node `216:157`, `excludeScreenshot=true`, 3 consecutive runs.

## Verdict: DETERMINISTIC for code, NON-DETERMINISTIC for asset URLs

| Element | Across runs 1–3 (+ Phase 2 run) |
|---|---|
| JSX structure / tree | byte-identical |
| className strings (Tailwind) | byte-identical |
| Hardcoded literals (#b5df2d, 180px, 120px, bg-black, font names) | byte-identical |
| `data-node-id` attributes | byte-identical |
| Text content (HELIX Design / AI ENABLED) | byte-identical |
| Trailing server instructions block | byte-identical |
| **Asset URL constants** (`api/mcp/asset/<uuid>`) | **NEW UUID every call** |

## Implication for pipeline
- The *semantic* output (code, layout, values) is stable and safe to cache / diff / re-run.
- Asset URLs are ephemeral signed handles (7-day TTL, fresh per request). Any agent MUST
  download assets immediately and rewrite `src` to a local/permanent path — never persist the
  figma.com/api/mcp/asset URL as if stable. This is the one moving part to engineer around.
