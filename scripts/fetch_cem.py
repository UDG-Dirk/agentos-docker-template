#!/usr/bin/env python3
"""Cycle 3: startup fetch of the CI-built Custom Elements Manifest (delivery mechanism 1b-ii).

Called from `scripts/entrypoint.sh` BEFORE uvicorn boots. Fetches the CEM artifact that the
`build-cem` CI job produced and writes it to `HELIX_CODE_CEM_PATH`, so the Baseline Reader serves
real baseline data instead of `CEM_MISSING`. Cache strategy A: fetch once at startup (no timer).

Discipline:
- **SP-9 env-only:** the artifact URL + auth are CONFIG, never hardcoded here.
- **SP-6 fail-loud + graceful:** on missing config or fetch failure, log LOUDLY and RETURN 0 so the
  container still starts — the reader then reports `CEM_MISSING` (its existing fail-loud fallback).
  This script NEVER blocks startup.
- **Deterministic:** no jitter, no retries-with-random-timing.

Env:
  HELIX_CODE_CEM_ARTIFACT_URL          full artifact URL (operator/CI supplies; no assembly here).
  HELIX_CODE_CEM_ARTIFACT_AUTH_HEADER  optional full header line, e.g. "PRIVATE-TOKEN: <token>"
                                       or "Authorization: Bearer <token>". Omit for a public/job-token URL.
  HELIX_CODE_CEM_PATH                  target file path in the container (same var the reader reads).
"""
from __future__ import annotations

import logging
import os
import urllib.request

log = logging.getLogger("fetch_cem")

URL_ENV = "HELIX_CODE_CEM_ARTIFACT_URL"
AUTH_ENV = "HELIX_CODE_CEM_ARTIFACT_AUTH_HEADER"
PATH_ENV = "HELIX_CODE_CEM_PATH"
_TIMEOUT_S = 30


def fetch(url: str, dest: str, auth_header: str | None = None) -> int:
    """GET `url` (optional single auth header "Key: Value") and write bytes to `dest`.
    Returns bytes written. Raises on any HTTP/IO error (caller decides how loud)."""
    req = urllib.request.Request(url)
    if auth_header and ":" in auth_header:
        key, val = auth_header.split(":", 1)
        req.add_header(key.strip(), val.strip())
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        data = resp.read()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)
    return len(data)


def main() -> int:
    url = os.environ.get(URL_ENV)
    dest = os.environ.get(PATH_ENV)
    if not url or not dest:
        log.warning(
            "%s / %s not set — SKIPPING CEM startup fetch. The Baseline Reader will report "
            "CEM_MISSING (fail-loud fallback) until a CEM is delivered. Set both to enable Cycle-3 "
            "delivery (see docs/AUTH_KEYS.md / cycle-3 result).", URL_ENV, PATH_ENV,
        )
        return 0
    try:
        n = fetch(url, dest, os.environ.get(AUTH_ENV))
        log.info("CEM fetched: %d bytes -> %s", n, dest)
    except Exception as exc:  # noqa: BLE001 — never block container startup on a fetch failure
        log.warning(
            "CEM startup fetch FAILED (%r) — continuing WITHOUT a CEM; Baseline Reader will report "
            "CEM_MISSING. Check %s reachability + %s scope (artifact download needs read_api, not a "
            "read_repository-only deploy token).", exc, URL_ENV, AUTH_ENV,
        )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[fetch_cem] %(levelname)s %(message)s")
    raise SystemExit(main())
