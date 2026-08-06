# Component code generator — HELIX pipeline step 3d

Written for a reader who has not used this toolchain. "3d" is just this stage's position in the
HELIX pipeline: after the **Theme Generator** (step 3c) has decided which client components map to
baseline components and which don't, the **Component Code Generator** (3d) produces the actual
source code.

## What it does, in one sentence

Given step 3c's output, it produces a customer package of real Lit (a web-components library) and
TypeScript source: existing baseline components are copied over unchanged, and components that
don't exist in the baseline yet are generated from the Figma design.

**Status:** v0.1 Phase 1 (deterministic path only) built and deployed. Workflow id
`helix-component-code-generator`, registered in `app/main.py`.

## The two generation paths

Every component is handled one of two ways, decided automatically per component:

- **Path A, fork** — used when step 3c found a matching baseline component. The baseline
  component's source is copied from helix-code (the baseline design-system repository; read-only,
  never modified) into the customer package. No AI model is involved; the same input always
  produces the same output.
- **Path B, from-spec** — used when there's no matching baseline component. A language model
  generates new Lit + TypeScript source directly from the Figma design data, then checks it against
  a set of structural rules ("the structural gate") before accepting it. If a component fails the
  gate twice, it is flagged for human review rather than shipped broken.

Phase 1 (what's built today) implements Path A. Path B lands in Phase 2.

## How Path A branding actually works ("Architecture B")

This is the part most likely to confuse a newcomer, so it gets its own section.

An earlier design renamed each baseline component's tag, class name, and CSS variables to match
the customer (e.g. `hx-button` → `acme-button`). That approach was dropped on 2026-08-03: those
renamed CSS variable references pointed at nothing, because the rest of the baseline's styling
still used the original `--helix-*` names.

The current approach ("Architecture B", the ratified design):

1. The baseline component's source is copied **byte-for-byte** into the customer fork. Its tag
   (`hx-button`), its class name (`HxButton`), and its CSS variable references
   (`var(--helix-color-primary)`) all stay exactly as they were.
2. Customer branding happens separately, later: only the **value** each CSS variable resolves to
   changes, set in the customer fork's copy of Style Dictionary (the tool that turns design-token
   JSON into CSS). The variable name stays `--helix-color-primary`; what colour it points to differs
   per customer.

So "forking a component" in this pipeline means an unmodified copy, not a renamed one. Branding is
applied at the token layer, not by rewriting component source.

## What it consumes

The 3c element descriptors and token layer (step 3c's output), via the workflow's `additional_data`:

```json
{
  "customer_slug": "acme",
  "scope": "msq-dx",
  "elements": [
    {"slot": "Button", "baseline_ref": "Button", "derivation": "forked_from_baseline", "confidence": "authoritative"}
  ],
  "tokens_json": "...",
  "helix_code_root": "/optional/override/path",
  "output_dir": "/optional/path/to/materialise/the/package"
}
```

Invoke: `POST /workflows/helix-component-code-generator/runs` with the above in `additional_data`.

## What it produces

- Real `.ts` component source files, laid out like helix-code:
  `packages/{customer_slug}-elements/src/elements/{element}/{ClassName}.ts`.
- A Custom Elements Manifest (`custom-elements.json`, a machine-readable inventory of the web
  components in the package — used by tooling like Storybook to know what components exist and
  what properties they take).
- A `PROVENANCE.md` documenting which components came from the baseline (forked, and from which
  branch/path) versus generated from the Figma spec.
- A cost summary (token counts, estimated spend) for any Path B generation calls.

## Files

```
agents/component_code_generator/
  models.py        # ComponentCodeGeneratorOutput envelope + provenance/cost contracts
  scaffolding.py    # fork_component, find_baseline_source, find_sibling_sources, package layout
  generation.py     # Path B: Generator protocol, MockGenerator (tests) / AgentGenerator (real LLM)
  step.py           # generate_component_code (testable core) + Agno step executors
  verify.py         # runnable verification gate — python -m agents.component_code_generator.verify
app/workflows/
  helix_component_code_generator.py   # the 2-step workflow (id helix-component-code-generator)
tests/component_code_generator/
  test_ccg_phase1.py, test_ccg_phase2.py, test_ccg_phase3.py, test_ccg_path1.py, test_ccg_verify.py
```

See [`VERIFICATION.md`](VERIFICATION.md) for the full verification-task ledger and test evidence.

## Running the verification gate locally

```bash
source .venv/bin/activate
python -m agents.component_code_generator.verify
```

Runs the deterministic pipeline over a small built-in fixture (no helix-code checkout, no LLM
calls) and prints pass/fail per verification task. Exit 0 means green.

## Terms used in this document and in VERIFICATION.md

| Term | Plain-language meaning |
|---|---|
| **3c / Theme Generator** | The prior pipeline step; decides which client components map to baseline components. |
| **3d / Component Code Generator** | This step; produces the actual component source code. |
| **helix-code** | The baseline design-system component library. Read-only from this pipeline's side; never modified in place. |
| **Path A / fork** | Copy an existing baseline component's source unchanged into the customer package. |
| **Path B / from-spec** | Generate new component source with a language model, when no baseline match exists. |
| **Architecture B** | The current branding approach: fork components verbatim, apply customer branding as a token value swap, not a rename. |
| **structural gate** | The set of rules a Path-B generated component must pass before it's accepted (valid Lit syntax, correct token usage). |
| **verbatim fork** | A copy with no changes to tag names, class names, or CSS variable references. |
| **token value swap** | Changing what a CSS variable resolves to, while keeping its name unchanged. |
| **`--helix-*` refs** | CSS custom properties defined by helix-code's Style Dictionary; preserved unchanged in a verbatim fork so they keep resolving correctly. |
| **Custom Elements Manifest (CEM)** | A machine-readable JSON inventory of a web-components package's elements and their properties. |
| **provenance** | A record of where each output file came from (forked from baseline, or generated from spec). |
