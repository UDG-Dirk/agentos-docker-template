# scripts/

Operational helpers for the AgentOS service.

| Script | Purpose |
|---|---|
| `mint_token.py` | Mint a signed RS256 JWT for AgentOS auth (see below). |
| `build_image.sh` | Build the prod container image. |
| `entrypoint.sh` | Container entrypoint. |
| `format.sh` / `validate.sh` | `ruff format` / `ruff check`. |
| `generate_requirements.sh` | Regenerate `requirements.txt`. |
| `venv_setup.sh` | Create the local `.venv`. |

---

## AGNO MCP token — mint & wire into Claude Code

Prod AgentOS (`RUNTIME_ENV=prd`) requires a **Bearer RS256 JWT**, verified server-side against
`JWT_VERIFICATION_KEY` (the RSA *public* key). `mint_token.py` signs a token with the matching
**private** key so you (or Claude Code's `agno-prod` HTTP MCP) can authenticate.

### 🔑 Security rules (non-negotiable)
- The **private key is never in this repo** and must never be committed. It lives at
  `~/.agno-keys/agno_private.pem` (`chmod 600`) and is delivered **out-of-band** (from the
  key-holder / a secrets vault). `.gitignore` blocks `*.pem` and `*_token*` as a backstop.
- A **minted token is a live credential** — don't commit it, don't paste it into any
  version-controlled config. Prefer **one token per person** (`--user <handle>`) so tokens are
  attributable and individually revocable (rotate the keypair to revoke).

### 1. Clone + env
```bash
git clone git@rmvc01.rm.udg.de:customer-udg-ai/projects/poc-agno-docker.git
cd poc-agno-docker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # ships pyjwt + cryptography
```

### 2. Place the signing key (given to you separately — NOT from git)
```bash
mkdir -p ~/.agno-keys && chmod 700 ~/.agno-keys
cp /path/to/agno_private.pem ~/.agno-keys/agno_private.pem
chmod 600 ~/.agno-keys/agno_private.pem
```
The key must match the server's `JWT_VERIFICATION_KEY`, or every token `401`s.

### 3. Mint
```bash
python3 scripts/mint_token.py --user <your-handle> --days 30 > ~/.agno-keys/agno_mcp_token
chmod 600 ~/.agno-keys/agno_mcp_token
```
| Flag | Default | Meaning |
|---|---|---|
| `--user` | `dirk` | JWT `sub` (who it's for) |
| `--days` | `30` | lifetime → `exp` (re-mint when it lapses) |
| `--scopes` | full set | space-separated; narrow for least-privilege |
| `--key` | `~/.agno-keys/agno_private.pem` | private-key path |

Inspect the payload without exposing the secret:
```bash
python3 -c "import jwt,pathlib; t=pathlib.Path('$HOME/.agno-keys/agno_mcp_token').read_text().strip(); print(jwt.decode(t, options={'verify_signature': False}))"
```

### 4. Wire into Claude Code (VSCode)
```bash
claude mcp add --transport http --scope user agno-prod \
  https://poc-agno-api.services.plygrnd.tech/mcp \
  --header "Authorization: Bearer $(cat ~/.agno-keys/agno_mcp_token)"
claude mcp list          # agno-prod … (HTTP) - ✓ Connected
```
In VSCode: reload the window, run `/mcp`, smoke-test `mcp__agno-prod__whoami`.

### Troubleshooting
| Symptom | Fix |
|---|---|
| `401 Unauthorized` | token expired, or key doesn't match `JWT_VERIFICATION_KEY` → re-mint |
| `✗ Failed to connect`, URL loads | missing/typo'd `Authorization` header or dropped `Bearer ` prefix |
| connects, no tools | reload VSCode window; check `/mcp` |
| can't reach host | VPN / network to `plygrnd.tech` |
