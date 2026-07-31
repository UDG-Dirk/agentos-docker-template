# AgentOS auth keys — mint / rotate / revoke runbook

How prod AgentOS auth works, and how the squad mints tokens **without** the private key ever
touching git or living on one laptop. Bus-factor doc — this is the source of truth for key custody.

## The keypair (RS256, self-generated — "BYO")

Auth is a **self-made RSA-2048 keypair**. There is no download from Agno/os.agno.com.

| Half | Role | Where it lives |
|---|---|---|
| **private** (`agno_private.pem`) | **signs** tokens (`scripts/mint_token.py`) | a **protected CI/CD variable** + a secrets vault + (optionally) the key-holder's `~/.agno-keys/`. **NEVER in git.** |
| **public** (`agno_public.pem`) | **verifies** tokens | Coolify prod env var `JWT_VERIFICATION_KEY`. Safe to expose. |

They are cryptographically bound: `JWT_VERIFICATION_KEY` only verifies tokens signed by the matching
private key. Prod enforces this whenever `RUNTIME_ENV=prd` (`app/main.py`: `authorization = runtime_env == "prd"`); local dev (`RUNTIME_ENV=dev` — see [`SETUP.md`](SETUP.md)) has auth **off**.

## Shared custody (the de-bottleneck)

The private key is shared with the AI-squad via a **protected, File-type GitLab CI/CD variable**, so
minting is self-serve and survives anyone's departure — **without committing the secret**.

**One-time setup (key-holder):**
1. `msq-turbo` group → **Settings → CI/CD → Variables → Add variable**
   - Key: `AGNO_PRIVATE_KEY`
   - Type: **File**
   - Value: paste the full `agno_private.pem` (PEM, incl. BEGIN/END lines)
   - Flags: **Protected** ✔ (exposed only on protected refs like `main`); **Expand variable reference** off
   - Scope: group level → shared by every project under `msq-turbo` (survives transfers).
2. Keep a second copy in a **team secrets vault** (1Password/Bitwarden/Vault) as the durable backup +
   for rotation. Do **not** rely on a single laptop.

> Why not commit it? Git history is permanent and travels with every clone/backup/**transfer** (this
> repo just moved namespaces); the key signs *all* prod tokens (full impersonation blast radius); and
> committing it means allowlisting it past gitleaks. Restricted-repo ≠ safe-for-secrets. Use the
> variable/vault — same squad access, none of the downsides.

### ⚠️ GitLab variable gotchas (the settings that actually work)

Setting `AGNO_PRIVATE_KEY` (or any PEM value) trips people up because GitLab's **Add variable** form
defaults to **Masked**, and Masked **rejects multi-line PEMs**. Use exactly:

| Setting | Value | Why |
|---|---|---|
| **Type** | **File** | Multi-line content; the job reads it via the `$AGNO_PRIVATE_KEY` *path* (`mint_token.py --key "$AGNO_PRIVATE_KEY"`), not inline. |
| **Protect variable** | **✔ checked** | The real security guarantee — exposed only to pipelines on protected branches (`main`); untrusted MR pipelines can't read it. |
| **Visibility** | **Visible** (NOT Masked, NOT "Masked and hidden") | Masked requires a base64-safe, whitespace-free value — a multi-line PEM can't satisfy its regex. A File variable doesn't need masking: it's written to a file path and read by reference, never echoed into logs. "Visible" = revealable in the CI/CD settings UI by authorized users — same threat model as a local `.pem`. |

**The mistake (and the error you'll see):** saving with **Visibility = Masked** →
> `Unable to create masked variable because: The value cannot contain the following characters: whitespace characters`

**Do NOT** "fix" this by stripping the PEM header/footer lines (the `-----`-wrapped `BEGIN PRIVATE KEY`
/ `END PRIVATE KEY` lines) or the newlines — those are **required** for PEM parsing (the key won't load without them).
The fix is simply **Visibility → Visible** (keep the full PEM, markers and all). If you staged the
base64 copy (`agno_private.pem.b64`), that's for a *vault* field that dislikes multi-line — the GitLab
**File** variable wants the **raw PEM**, not the base64.

**Verify after saving** — click **Reveal** and confirm the value:
- starts with the `-----`-wrapped `BEGIN PRIVATE KEY` header line
- ends with the `-----`-wrapped `END PRIVATE KEY` footer line
- has the base64 body lines between those markers, newlines intact

## Mint a token — three ways

### A. Self-serve via CI (no key handling) — preferred
GitLab → **Build → Pipelines → Run pipeline** on `main`, set variables:
- `MINT_USER=<handle>` (required), `MINT_DAYS=30` (optional), `MINT_SCOPES="agents:run workflows:run …"` (optional).
Run → open the **`mint-token`** job → download the **`agno_mcp_token`** artifact (expires in 1 day). Never printed to logs, never committed. Then wire into Claude Code per [`../scripts/README.md`](../scripts/README.md).

### B. Key-holder mints locally
```bash
python3 scripts/mint_token.py --user <handle> --days 30   # uses ~/.agno-keys/agno_private.pem
```
Send the token over a secure channel.

### C. Requester self-mints (only if they hold the key)
`pip install pyjwt cryptography` (NOT `-r requirements.txt`), place the key at `~/.agno-keys/agno_private.pem` (chmod 600), run as in B. Spreads the secret — avoid unless necessary.

## Rotate (new keypair) — for handover or after a suspected leak
```bash
openssl genpkey -algorithm RSA -pkcs8 -pkeyopt rsa_keygen_bits:2048 -out agno_private.pem
openssl pkey -in agno_private.pem -pubout -out agno_public.pem
```
1. Update Coolify prod env `JWT_VERIFICATION_KEY` = new `agno_public.pem` (single line OK; no literal `\n`), **redeploy**.
2. Update the `AGNO_PRIVATE_KEY` group CI variable + vault copy = new `agno_private.pem`.
3. Re-mint everyone's tokens (all old tokens `401` after the swap — that's the revocation lever).

## Revoke
- **One person / all:** rotate the keypair (above) — invalidates every token signed by the old key.
- **Soft:** keep token lifetimes short (`--days`) so exposure self-limits; per-person `--user` makes tokens attributable.

## ⚠️ Prerequisites on `msq-turbo/helix-agents` (post-transfer)
The CI mint job needs the new project's CI to be live:
1. `AGNO_PRIVATE_KEY` set as a protected File variable (group scope).
2. `main` marked **protected** (so protected vars are exposed).
3. A CI **runner** available to the project.
4. The `pipeline-templates/security` component access re-granted to `msq-turbo/helix-agents` (the
   grant was project-scoped to the old path; until then the whole pipeline may fail to resolve the
   `include:` and no jobs — including `mint-token` — run). Owner: Marcello Pabst.

## Handover checklist (before the current key-holder leaves)
- [ ] `AGNO_PRIVATE_KEY` stored as a `msq-turbo` **group** File variable (not just a laptop).
- [ ] Private key also in the team **vault** (durable backup).
- [ ] A named successor has vault + GitLab-group access.
- [ ] CI mint job verified working on `main` (prereqs above satisfied).
- [ ] Consider a **rotation** so the successor owns a fresh key and the departing key is retired.
