# Environment Variables — Inventory + Token Rotation

Single source of truth for every env var / token the deployed `helix-agents` pipeline uses, and —
critically — **when tokens expire** (a silent expiry → `build-cem` fails → reader falls back to
`CEM_MISSING`). Values are **never** recorded here (secrets live in Coolify / GitLab CI vars / a vault);
this doc records names, purpose, source-of-truth, and rotation. Complements
[`AUTH_KEYS.md`](AUTH_KEYS.md) (JWT keypair deep-dive) and the pipeline jobs in [`../.gitlab-ci.yml`](../.gitlab-ci.yml).

> Keep this current: **any env/token/pipeline change updates this file in the same MR.** `[FILL]` marks
> a value only the owner can supply (e.g. a token expiry) — replace it, don't leave it.

## 🔑 Token rotation schedule (check first)

| Token | Lives in | Scope/type | Expires | Owner | Rotation runbook |
|---|---|---|---|---|---|
| `BASELINE_REPO_TOKEN` (+ `BASELINE_REPO_USERNAME`) | msq-turbo **group** CI vars **and** Coolify service env | helix-code deploy token, `read_repository` | **2026-11-03** | Dirk | Regenerate deploy token on `msq-turbo/helix-code` (read_repository); update both group CI var + Coolify env. |
| read_api token (inside `HELIX_CODE_CEM_ARTIFACT_AUTH_HEADER`) | Coolify service env | msq-turbo **group access token**, `read_api` | `[FILL]` | Dirk | Regenerate group access token (`read_api`) at msq-turbo; update the Coolify `..._AUTH_HEADER` env. Needed for CEM artifact download. |
| `AGNO_PRIVATE_KEY` (RS256 signing key) | msq-turbo group CI var + vault + `~/.agno-keys` | RSA-2048 private PEM | none (rotate ~quarterly) | Dirk | See [`AUTH_KEYS.md`](AUTH_KEYS.md) → Rotate. Re-mint tokens after. |
| minted AgentOS JWTs (`Authorization: Bearer …`) | client-side (`~/.agno-keys/agno_mcp_token`, per person) | RS256 JWT, `--days` | ~30 d from mint (current service token ≈ **2026-08-23**) | each holder | Re-mint via the `mint-token` CI job or `scripts/mint_token.py`. |
| `FIGMA_PAT` | Coolify + local `.env` | Figma personal access token | `[FILL]` (verify — Figma PATs can be long-lived) | Dirk | Regenerate at figma.com → Settings → Personal access tokens; update Coolify + `.env`. |
| Dirk's `glpat-*` (glab CLI) | local `~/.config/glab-cli` | rmvc01 personal/group access token, `api` | `[FILL]` | Dirk | Regenerate at rmvc01 → user/group settings → access tokens; `glab auth login --stdin`. |

**Recommendation:** add a calendar reminder ~2 weeks before each dated expiry. The nearest hard one is
**`BASELINE_REPO_TOKEN` → 2026-11-03**; fill the read_api token + PAT expiries above.

## Env vars by category

Sensitivity: **secret** = credential/never-log; **config** = non-secret.

### Authentication + secrets
| Var | Purpose | Source of truth | Sensitivity | Breaks if stale/wrong |
|---|---|---|---|---|
| `OPENAI_API_KEY` | LiteLLM-proxy key (models + embeddings) | Coolify / `.env` | secret | agents + KB embedding 401 |
| `JWT_VERIFICATION_KEY` | RSA **public** PEM; verifies minted JWTs (prd only) | Coolify | config (public key) | every request 401s if it drifts from the signing key |
| `FIGMA_PAT` | Figma REST auth (extractor + baseline lanes) | Coolify / `.env` | secret | Figma calls 403 |
| `BASELINE_REPO_TOKEN` / `BASELINE_REPO_USERNAME` | helix-code shallow clone (reader) + `build-cem` clone | group CI vars + Coolify | secret | `BASELINE_AUTH_MISSING` / clone 403 |
| `HELIX_CODE_CEM_ARTIFACT_AUTH_HEADER` | auth for the CEM artifact download (holds a `read_api` token) | Coolify | secret | CEM fetch 401 → `CEM_MISSING` |
| `AGNO_PRIVATE_KEY` | RS256 signing key for the `mint-token` CI job | group CI var (+ vault) | secret | can't mint tokens |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` | optional Slack interface | Coolify | secret | Slack interface off (both unset = simply not loaded) |
| `PARALLEL_API_KEY` | raises WebSearch Parallel rate ceiling (optional) | Coolify | secret | lower rate ceiling only |

### External service config
| Var | Purpose | Source | Sensitivity |
|---|---|---|---|
| `OPENAI_BASE_URL` | LiteLLM proxy URL | Coolify / `.env` | config |
| `OPENAI_MODEL_ID` / `FIGMA_PARSER_MODEL_ID` / `OPENAI_EMBEDDER_ID` | model + embedder ids (keep `openai/` prefix on the embedder) | Coolify / `.env` | config |
| `AGENTOS_URL` | scheduler base URL (public domain in prod) | Coolify | config |
| `BASELINE_REPO_URL` | credential-free helix-code clone URL | Coolify (default in code) | config |
| `HELIX_CODE_CEM_ARTIFACT_URL` | **Cycle 3** CEM download URL fetched at startup. **Stable** generic-package URL: `…/api/v4/projects/<id>/packages/generic/cem/latest/custom-elements.json` (never changes; `build-cem` overwrites `latest` each run) | Coolify | config |
| `FIGMA_RATE_LIMIT_REFERENCE_FILE` | known-good file for sustained-429 scope cross-probe | Coolify (unset → scope `undetermined`) | config |

### Internal service config
| Var | Purpose | Source |
|---|---|---|
| `RUNTIME_ENV` | `prd` (auth on) / `dev` (auth off, hot-reload) | Coolify / `dev-up.sh` |
| `DB_HOST` `DB_PORT` `DB_USER` `DB_PASS` `DB_DATABASE` `DB_DRIVER` | Postgres/pgvector connection (`DB_PASS` = secret) | Coolify / `.env` |
| `AGNO_DEBUG` | verbose tool-call logging | Coolify / `.env` |

### Paths + volumes (persistent `/var/lib/helix`)
| Var | Purpose | Recommended value |
|---|---|---|
| `BASELINE_REPO_PATH` | helix-code checkout dir | `/var/lib/helix/baseline` (code default) |
| `BASELINE_OUTPUT_DIR` | inventory output dir | `/var/lib/helix/inventory` (code default) |
| `HELIX_CODE_CEM_PATH` | where the CI-built CEM is written (fetch) + read (reader) — **must be ABSOLUTE** | `/var/lib/helix/custom-elements.json` |

### Behavior / tuning (3b + rate-limit; env-overridable, sane code defaults)
`MARGIN_ACCEPT`, `NAME_ACCEPT`, `MIN_FAMILIES_AGREE`, `VALUE_VETO_COLOR_HEX`, `VALUE_VETO_DIMENSION_REL`
(semantic-matcher scoring thresholds); `FIGMA_RATE_LIMIT_CROSS_PROBE_MAX_PER_HOUR` (default 10). All config, non-secret.

## GitLab pipelines / CI jobs (`.gitlab-ci.yml`)
| Stage | Job | Trigger | Purpose |
|---|---|---|---|
| security_scanning | gitleaks / grype / socket (**gating**) + osv / checkov (allow_failure) | every push/MR | security gate (component `security-scanner@v4.0.1`) |
| mint | `mint-token` | web run on `main` + `MINT_USER` (`needs: []`) | mint an AgentOS JWT → 1-day artifact (uses `AGNO_PRIVATE_KEY`) |
| baseline | `build-cem` | web run + `BUILD_CEM=1` (+ optional `HELIX_CODE_REF`) (`needs: []`) | clone helix-code (uses `BASELINE_REPO_TOKEN`/`_USERNAME`), `pnpm analyze:flat`, then **publish `custom-elements.json` to the generic package registry** at `cem/latest` via `CI_JOB_TOKEN` (+ keep the job artifact for backward compat) |

CI-only vars (not app env): `MINT_USER` `MINT_DAYS` `MINT_SCOPES` (mint), `BUILD_CEM` `HELIX_CODE_REF` `HELIX_CODE_REPO_URL` `CEM_REL_PATH` (build-cem).

## Discrepancy / dead-entry flags
- **`HELIX_CODE_CEM_ARTIFACT_URL` (RESOLVED 2026-07-29 → stable package URL):** the old "latest-on-`main` `?job=build-cem`" artifact URL was fragile — it 404'd (build-cem is opt-in, not on `main`'s regular pipelines) and, once repointed, served a **12929-byte non-CEM page** → reader `CEM_MALFORMED`. **Fix (shipped):** `build-cem` now publishes to the **generic package registry** at a stable URL `…/api/v4/projects/<id>/packages/generic/cem/latest/custom-elements.json` (overwritten each run). No more pinned-job-URL re-pin. Download auth = a `read_api` token in `..._AUTH_HEADER` (unchanged; the registry download accepts `read_api`).
- **`AGNO_PRIVATE_KEY`** lives in *both* the group CI var (for `mint-token`) and Coolify (historical) — intentional (two consumers), but keep both in sync on rotation.
- No dead entries found: every code env var above maps to a real consumer.
