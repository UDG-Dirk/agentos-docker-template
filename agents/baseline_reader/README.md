# Baseline Reader — HELIX Pipeline Step 3a

Deterministic Python reader (zero LLM, zero Figma, zero network) that consumes a
**helix-code baseline repo** (or any `helix-<customer>` extension) and produces a
machine-readable **JSON inventory** of its tokens, components, Storybook
conventions, and known constraints.

The inventory is the **baseline-comparison input** for the rest of the pipeline —
it answers **GAP-10 ("no baseline comparison input")**. Downstream it feeds the
Semantic Matcher (3b), Theme Generator (3c), and New Component Scaffolder (3d),
which reason about how a client's design system diverges from the helix baseline.

Runs after the Token Normalizer (Step 2/3) and before the Semantic Matcher (3b).
Read-only on the baseline: it never writes to, and never builds, the repo it reads.

## I/O

| | |
|---|---|
| **Input** | A helix-code baseline (or `helix-<customer>` extension) checkout on disk — read-only |
| **Output** | Baseline inventory (`dict`) — `meta`, `tokens`, `components`, `storybook`, `constraints`, `blocking_warnings`, `warnings` |
| **Entry point** | `reader.read_baseline(baseline_repo_path, ref=None, cem_source=None, token_sources=None, storybook_config=None, output_dir=None) -> dict` |
| **Step type** | Agno `Step(executor=fn)` — a **function step, not an LLM agent** (no model, no tool calls) |

## Design principle — "Reader adapts to baseline layout, does not assume it"

Every structural path is a **config kwarg with a default matching today's
helix-code**, so a standard clone works with no arguments — but nothing is
hard-coded:

| kwarg | default (today's helix-code) |
|---|---|
| `cem_source` | `packages/elements/custom-elements.json` |
| `token_sources` | `packages/tokens/tokens/` |
| `storybook_config` | `packages/storybook/.storybook/` |

- When a default path is missing, **heuristic discovery** kicks in (searches the
  baseline for the corresponding artifact) rather than failing outright.
- `meta.resolved_paths` **echoes what was actually read**, so callers can see
  whether a default, an override, or a discovered path supplied each section.
- `ref` records the baseline commit / ref for provenance; it does not check out.

## Output — in-memory first, disk opt-in

The returned **`dict` is the PRIMARY surface**; callers should consume it directly.

`output_dir` is an **opt-in disk write** (per `lesson:agent-output-persistence-pattern`).
When set, the reader writes:

- `baseline-inventory-{commit}-{iso-timestamp}.json` — the immutable run artifact.
- `baseline-inventory-latest.json` — a pointer to the newest run (**symlink
  preferred, file copy as fallback** where symlinks are unavailable).

Top-level keys of the inventory:

```
meta               # resolved_paths, ref/commit, timestamp, reader version
tokens             # token inventory read from token_sources
components          # component inventory from the Custom Elements Manifest (CEM)
storybook          # Storybook conventions / config
constraints        # known baseline constraints
blocking_warnings  # [] normally; non-empty halts the pipeline at the HITL gate
warnings           # informational only; pipeline continues
```

## Warnings — two arrays, two behaviours

Per `decision:blocking-warnings-convention`, the reader **never raises** for
expected baseline problems. It reports them instead:

- **`warnings`** — informational strings. The pipeline continues.
- **`blocking_warnings`** — objects `{code, message, remediation}`. When this array
  is non-empty, the **workflow orchestrator halts at the HITL gate**; the reader
  itself still returns normally.

First defined blocking code: **`CEM_MISSING`**.

## ⚠️ Operational note — the CEM is a build artifact

The Custom Elements Manifest (`custom-elements.json`) is a **gitignored build
artifact**. A fresh helix-code clone does **not** contain it.

Before a full-fidelity run, the **operator** must generate it:

```bash
pnpm --filter @helix/elements run analyze
```

The reader **never runs pnpm itself** (it is strictly read-only on the baseline).
If the CEM is absent, the reader emits a `CEM_MISSING` **blocking_warning** and
returns an **empty `components` inventory** — the rest of the inventory still
populates, and the run halts at the HITL gate for the operator to generate the CEM
and re-run.

## Constraints

- **Never writes to the baseline repo.**
- **Never runs pnpm** (or any build).
- **helix-code is READ-ONLY.**
- No LLM, Figma, or network calls — fully deterministic.

## Usage

```python
from agents.baseline_reader.reader import read_baseline

# Minimal — in-memory only, defaults matched to today's helix-code
inventory = read_baseline("/path/to/helix-code")

print(inventory["meta"]["resolved_paths"])
print(len(inventory["components"]))        # 0 if CEM_MISSING
for bw in inventory["blocking_warnings"]:  # {code, message, remediation}
    print(bw["code"], "→", bw["remediation"])
```

```python
# Opt-in disk write — persists the run + updates baseline-inventory-latest.json
inventory = read_baseline(
    "/path/to/helix-code",
    ref="main",
    output_dir="runs/",
)
```

## Testing

`pytest`, two suites:

- **Determinism** — frozen fixtures under `tests/baseline_reader/fixtures/`; asserts
  byte-stable output for a fixed input.
- **Integration** — a live helix-code clone; shape assertions on the inventory.

```bash
python -m pytest tests/baseline_reader/
```

## Files

```
agents/baseline_reader/
  reader.py            # read_baseline() — deterministic fn step + entry point
  README.md            # this file
tests/baseline_reader/
  fixtures/            # frozen inputs for the determinism suite
```
