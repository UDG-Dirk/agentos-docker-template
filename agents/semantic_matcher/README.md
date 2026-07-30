# Semantic Matcher — HELIX Pipeline Step 3b

Written for a reader who has not used this toolchain. "3b" is just this stage's position in the HELIX
pipeline: the **Baseline Reader** (step 3a) reads a design system's baseline library; the **Semantic
Matcher** (3b) is the next step.

## What it does, in one sentence

Given a **client's** design token (a colour, a spacing value, a font, …), the Semantic Matcher decides
**which token in the baseline library that client token corresponds to** — or decides, honestly, that it
corresponds to none of them.

Concretely: it takes one client token, compares it against the baseline tokens of the same kind, scores
each candidate across four independent kinds of evidence, and then **either** accepts a match on its own
(when the evidence is strong and consistent) **or** hands the decision to an LLM (when it isn't) — and the
LLM is allowed to say "none of these" rather than being forced to guess.

The four kinds of evidence ("signal families", [`scoring.py`](scoring.py)):
- **name** — how similar the two token names are,
- **path** — how much their hierarchical paths overlap (e.g. `color/interactive/surface/…`),
- **value** — how close their actual values are (e.g. two colours' RGB distance),
- **layer** — whether they sit at the same layer/tier of the system.

## Current status — a tested *library*, not yet a live endpoint

**Important for anyone looking for a workflow to call:** there is **no deployed Semantic Matcher endpoint
today.** What exists is a **verified library** — the scoring logic — plus a test harness that exercises it.

- Files under `agents/semantic_matcher/`: [`scoring.py`](scoring.py) (the deterministic scorer),
  [`models.py`](models.py) (the result types), and `__init__.py`. There is **no `matcher.py`** and **no
  Agno Step/Workflow registered** — `app/main.py` registers only the four extractor/baseline workflows.
- The full "3b" Agno workflow wrapper is **deliberately deferred** ("later work" — see the note at the top
  of [`models.py`](models.py)). The scoring library is built and tested first so the eventual endpoint can
  reuse `score_candidates()` **verbatim**, with empirical grounding already in place.

So today you *run the library directly* (see "How to run it today"); you do not call a REST/MCP endpoint.

## What it will consume (inputs, when the endpoint lands)

- **Baseline corpus** = the **Baseline Reader (3a) output** — the inventory of the baseline library's
  tokens. (Test fixture: `tests/semantic_matcher/fixtures/baseline_snapshot_*.json`, described as
  "Baseline Reader output".)
  - The raw baseline snapshot carries **names but not resolved values**. Because the value-comparison
    evidence needs actual values, an **enriched** baseline (`{name, path, category, dtcg_type, layer,
    value}` per token) is derived from the baseline's DTCG token leaves and used for scoring.
- **Client side** = one **client token record** of the same shape: `{name, path, category, dtcg_type,
  value, layer}` — eventually produced by the Figma extraction pipeline. (Today the tests synthesise
  client tokens; the endpoint's live client source is not wired yet.)

## What it produces (outputs)

Two result types (defined in [`scoring.py`](scoring.py) and [`models.py`](models.py)):

**`ScoringResult`** — the deterministic scorer's output:
- `top_candidates` — the ranked baseline candidates, each a `ScoredCandidate` (`baseline_var`, per-family
  `signals`, `aggregate_score`, `signal_families_agreeing`),
- `margin_top1_top2` — how far ahead the best candidate is over the runner-up,
- `matched_via` — one of `deterministic` (accepted on evidence alone), `llm_required` (escalated), or
  `no_candidates`,
- `deterministic_confidence` — `authoritative` or `high` when accepted deterministically, else none,
- `deterministic_rationale` — a human-readable why.

**`LLMMatchResult`** — the output when the decision is escalated to the LLM:
- `outcome` — `map` (matched) or `unmappable` (the LLM declined to match),
- `baseline_var` — the chosen baseline token (required when `map`, must be null when `unmappable` — this
  is contract-enforced),
- `confidence`, `rationale`.

**Confidence vocabulary (the exact set):** `authoritative`, `high`, `medium`, `unresolved`. The
deterministic path emits only `authoritative` / `high`; `medium` / `unresolved` come from the LLM path and
abstentions.

**Unmapped register** — tokens that match nothing are recorded with a tagged reason:
`name_type_incoherent`, `llm_abstained`, `no_candidates`, or `post_validator_rejected`. (Today this
register is a concept in the test harness, not yet a typed model in the library.)

## The three-layer defense (why it doesn't produce confident-but-wrong matches)

The matcher is deliberately defensive: a wrong-but-confident match is worse than an honest "no match". Three
independent layers guard against three different failure modes. Order at decision time: Layer 2 → Layer 1 →
Layer 3 (post-validator).

- **Layer 1 — deterministic gate + LLM may abstain.** *Guards against forced or low-confidence matches.*
  A match is accepted automatically **only if all** of these hold: the best candidate leads the runner-up
  by a clear **margin** (≥ `MARGIN_ACCEPT`), the **names** are strongly similar (≥ `NAME_ACCEPT`), the
  **value check** passes, and **at least two** of the four evidence families agree (`MIN_FAMILIES_AGREE`).
  If any fails, the decision escalates to the LLM — which is explicitly allowed to answer "unmappable"
  rather than guess.
  - Inside Layer 1 is a **hard value veto**: for tokens with comparable values (colours, dimensions), a
    value that disagrees beyond tolerance **forces** escalation even when the names/paths/layers all agree.
    This is what stops a confident wrong match on a **name collision** (two unrelated tokens that happen to
    share a name). Tolerances: colour ≈ 8.0 RGB-distance (0–255 scale), dimension ≈ 5% relative.
- **Layer 2 — name-vs-declared-type coherence.** *Guards against a token that "lies about its own type"* —
  e.g. a name/path that say "colour" on a token whose declared type is numeric. This check runs **before**
  the LLM, so a structurally incoherent token is rejected without depending on the LLM choosing to abstain.
  It soft-passes when it genuinely can't judge (e.g. a mislabelled name but an intact path), to avoid false
  rejections. Rejection reason: `name_type_incoherent`.
- **Layer 3 — resolved-value check (post-validator).** *Guards against the "alias bypass"*: a baseline
  token whose value is an alias/reference (`"{a.b.c}"`) can trivially pass a value check because its raw
  value is opaque. This layer **resolves the alias transitively** and checks the *resolved* value against
  the token's declared type. Unresolvable aliases are skipped rather than trusted. Rejection reason:
  `post_validator_rejected`.

(Underpinning all three: each available evidence family votes for its own best candidate, and the count of
agreeing families feeds Layer 1's gate.)

## How to run it today (no endpoint needed)

Import and call the scorer directly:

```python
from agents.semantic_matcher.scoring import score_candidates
from agents.semantic_matcher.models import LLMMatchResult
# build a client-token dict + a list of baseline-candidate dicts (see the test helpers), then:
result = score_candidates(client_token, candidates)
```

Run the tests / harness:

```bash
# from the repo root:
source .venv/bin/activate
python -m pytest tests/semantic_matcher/ -q                       # unit + defense-layer + mutation tests
python -m tests.semantic_matcher.testbench.run_testbench          # harness with a MOCK LLM (<60s)
```

- The harness uses a **mock LLM by default** (no network, no cost). To exercise a **real** LLM, set
  `HELIX_TESTBENCH_USE_REAL_LLM=1` (costs money — off by default).
- Worked invocation patterns live in `tests/semantic_matcher/test_scoring.py`,
  `test_abstention.py`, and `test_defense_layers.py` (the `_tok()` / `_color()` helpers show how to build
  token and candidate records).

## What triggers building the live endpoint (`matcher.py`)

There is **no numbered gate in the code** for this (a repo-wide search for a "verification-task #17"-style
marker finds nothing). What the code *does* say: the 3b Agno Step wrapper is "later work per the parent
testbench task", and the scorer is built to be reused verbatim by that future agent. The nearest concrete
language is that the real-LLM abstention rate "is measured at HARDEN, not here" — implying a later HARDEN
phase — but no explicit condition/task number gating `matcher.py` construction currently lives in the code.
Treat the trigger as **coordinated at the spec/roadmap level**, not encoded here.

## Terms used in this document

| Term | Plain-language meaning |
|---|---|
| **3a / Baseline Reader** | The prior pipeline step that reads the baseline design library into an inventory. |
| **3b / Semantic Matcher** | This step — matches client tokens to baseline tokens. |
| **signal family** | One of the four kinds of evidence compared: name, path, value, layer. |
| **deterministic match** | A match the scorer accepts on evidence alone, no LLM. |
| **escalation** | Handing an uncertain match to the LLM to decide. |
| **abstention** | The LLM answering "none of these" instead of forcing a match. |
| **veto** | A check that blocks a match outright (e.g. values too far apart). |
| **DTCG** | The W3C Design Tokens format the baseline tokens are expressed in. |
| **alias** | A token whose value points at another token, written `{a.b.c}`. |
