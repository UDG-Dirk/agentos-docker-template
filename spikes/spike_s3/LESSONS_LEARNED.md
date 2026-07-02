# Spike S3 — Lessons Learned: GitLab REST push from an Agno agent (F4)

Self-hosted GitLab at `rmvc01.rm.udg.de`, project `customer-udg-ai/projects/helix-poc-agno` (id **2545**).

## THE BIG ONE — PAT scope
- **The repository REST API needs the `api` scope (or `read_api` for reads). `read_repository`/`write_repository` are NOT enough** — on this instance they authorize **git-over-HTTPS only**, and every REST repository endpoint (tree, branches, commits, files) returns **403 `insufficient_scope`** with them. The 403 body lists the accepted scopes: `api / read_api / ai_workflows`.
- 403 `insufficient_scope` (valid token, right project) ≠ 401 (bad token) ≠ 404 (bad path). Diagnose by the error code.
- **Fix:** issue the PAT with `api` scope. After rescoping, `GET /projects/:id` → 200 and all repo writes work.

## Auth + URL encoding
- Header: `PRIVATE-TOKEN: <pat>`. (PAT loaded via dotenv from helix .env; never logged.)
- **Nested project path must be URL-encoded**: `customer-udg-ai/projects/helix-poc-agno` → `customer-udg-ai%2Fprojects%2Fhelix-poc-agno`. Or use the numeric id (2545).
- **Branch names and file paths in GET endpoints must be URL-encoded too**: `spike/s3-test` → `spike%2Fs3-test`; `components/Button.vue` → `components%2FButton.vue` (`urllib.parse.quote(x, safe='')`).

## SSL
- **Valid cert** — `verify=True` works; no self-signed workaround needed (the SOP-4 concern did not materialize). Kept a `GITLAB_SSL_VERIFY=false` escape hatch in the client just in case.

## Empty-repo bootstrap (test-project artifact)
- The test project was **empty** (`empty_repo: true`, no branches, `default_branch: main` declared but with no commit).
- First commit to a branch with **no `start_branch`** creates that branch fresh (works on a truly empty repo).
- Once any branch exists, creating a NEW branch via the commits API requires `start_branch` pointing at an existing branch — otherwise **400 "You can only create or edit files when you are on a branch"**.
- We initialized `main` once (from the first spike branch) so the repo behaves like a real one; real pipeline repos already have `main`, so this is not a pipeline concern.

## Endpoints used (all confirmed working with `api` scope)
- `GET /projects/:id` → metadata (default_branch, empty_repo, id).
- `GET /projects/:id/repository/branches` and `/branches/:branch` (branch verify).
- `POST /projects/:id/repository/branches?branch=X&ref=Y` (explicit branch create).
- **`POST /projects/:id/repository/commits`** — the workhorse. Body:
  ```json
  {"branch":"spike/x","commit_message":"...","start_branch":"main",
   "actions":[{"action":"create","file_path":"a/b.ts","content":"..."}]}
  ```
  One request commits **multiple files atomically** (one action per file; `create`/`update`/`delete`/`move`). `start_branch` creates `branch` from that ref if it doesn't exist.
- `GET /projects/:id/repository/files/:url_encoded_path?ref=branch` (verify a file landed).

## Agno agent integration
- Tool defined with `from agno.tools import tool`; `@tool(requires_confirmation=True)` on the write/commit op = an **HITL gate**: `agent.run(...)` returns with `is_paused=True`, the pending call in `run.tools_requiring_confirmation` (each has `.confirmed`). Approve by setting `.confirmed=True` then `agent.continue_run(run_response=run)` → completes. (Same HITL family as S2's workflow gates, at the tool level.)
- `OpenAIChat` (not `OpenAIResponses`) — the agent made multi-tool round-trips (commit → verify) cleanly through LiteLLM.
- Latency: REST commit round-trip **~0.5–0.8s**; trivial vs LLM time.

## Reusable pattern for the pipeline
- A small `GitLabClient` (httpx, PRIVATE-TOKEN header) with `commit_files(branch, files, message, start_branch)` is all the output channel needs — push Storybook + CMS files as one atomic multi-file commit per repo, onto a feature branch off `main`, optionally gated by `requires_confirmation` for human review before push. Keep the PAT injected at the tool layer, never in session_state/logs.
