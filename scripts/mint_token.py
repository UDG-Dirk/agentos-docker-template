#!/usr/bin/env python3
"""Mint a signed JWT for AgentOS BYO-keypair auth (RS256).

The AgentOS service verifies these tokens with its ``JWT_VERIFICATION_KEY`` (the RSA *public*
key). This script signs a token with the matching *private* key so a developer / CI / an MCP
client (e.g. Claude Code's ``agno-prod`` HTTP MCP) can authenticate against prod.

SECURITY — read once:
  * The private key is NOT in this repo and MUST NEVER be committed. Keep it at
    ``~/.agno-keys/agno_private.pem`` (chmod 600), delivered out-of-band. `.gitignore` blocks
    ``*.pem`` / ``*_token*`` so it can't land here by accident.
  * The minted token is a live credential — don't commit it, don't paste it into version-
    controlled config. Prefer one token per person (``--user <handle>``) for attribution.

Usage:
    pip install -r requirements.txt            # ships pyjwt + cryptography
    python3 scripts/mint_token.py --user <handle> --days 30 > ~/.agno-keys/agno_mcp_token
    chmod 600 ~/.agno-keys/agno_mcp_token

Full setup (clone -> mint -> wire into Claude Code): see scripts/README.md.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

try:
    import jwt
except ImportError:
    raise SystemExit("Install PyJWT: pip install pyjwt cryptography") from None

DEFAULT_PRIVATE_KEY_PATH = Path.home() / ".agno-keys" / "agno_private.pem"

# Full scope set the AgentOS surface understands. Narrow with --scopes for least-privilege tokens.
DEFAULT_SCOPES = [
    # Studio / core
    "agents:read", "agents:run",
    "teams:read", "teams:run",
    "workflows:read", "workflows:run",
    "components:read", "components:write",
    "registry:read",
    "config:read", "config:write",
    # Sessions & observability
    "sessions:read", "sessions:write",
    "traces:read",
    # Knowledge
    "knowledge:read", "knowledge:write",
    # Learning / memory
    "memories:read", "memories:write",
    # Metrics
    "metrics:read", "metrics:write",
    # Evaluation
    "evals:read", "evals:write",
    # Approvals (human-in-the-loop)
    "approvals:read", "approvals:write",
    # Scheduler
    "schedules:read", "schedules:write",
]


def mint(user: str, days: int, scopes: list[str], key_path: Path) -> str:
    if not key_path.exists():
        raise SystemExit(
            f"Private key not found at {key_path}. Place agno_private.pem there (chmod 600) — "
            "it is delivered out-of-band, never from git. See scripts/README.md."
        )
    private_key = key_path.read_text()
    now = int(time.time())
    payload = {
        "sub": user,
        "scopes": scopes,
        "iat": now,
        "exp": now + 86400 * days,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mint an AgentOS RS256 JWT")
    parser.add_argument("--days", type=int, default=30, help="token lifetime in days (default 30)")
    parser.add_argument("--user", default="dirk", help="JWT sub / who the token is for")
    parser.add_argument("--scopes", nargs="+", default=DEFAULT_SCOPES, help="space-separated scopes")
    parser.add_argument("--key", type=Path, default=DEFAULT_PRIVATE_KEY_PATH,
                        help="path to the RSA private key (default ~/.agno-keys/agno_private.pem)")
    args = parser.parse_args()

    print(mint(args.user, args.days, args.scopes, args.key))
