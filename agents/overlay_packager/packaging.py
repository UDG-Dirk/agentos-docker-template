"""Overlay Packager — run classification + slug validation (v0.3 Phase 3).

Boundary-level input guard for the packager workflow. ``customer_slug`` is
fail-loud, never defaulted — customer identity must be explicit, unlike
``component_code_generator.scaffolding.slugify()``'s own test-convenience
fallback to ``"customer"`` (that default is upstream-test-only; the packager
must reject a blank slug outright, never launder it into a bogus client-fork).

Run classification (first_run vs re_run) is read from the LIVE GitLab
client-forks subgroup, never inferred from local cache emptiness — the
persistent volume at ``HELIX_CLIENTS_REPO_ROOT`` is cache-only and can be
wiped independently of the remote (Phase 0 finding).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx

from agents.component_code_generator.scaffolding import slugify

ENV_TOKEN = "GITLAB_HELIX_CLIENTS_TOKEN"
ENV_REPO_URL = "HELIX_CODE_REPO_URL"
ENV_SUBGROUP_URL = "HELIX_CLIENTS_SUBGROUP_URL"
ENV_GIT_USERNAME = "HELIX_CLIENTS_GIT_USERNAME"

_HTTP_TIMEOUT_SECONDS = 20.0

RunType = Literal["first_run", "re_run"]


class PackagerInputError(Exception):
    """Fail-loud boundary guard. Carries the {code, message, remediation}
    blocking-warning payload (ratified convention — see fork_ops.ForkOpsError)."""

    def __init__(self, code: str, message: str, remediation: str):
        self.code = code
        self.message = message
        self.remediation = remediation
        super().__init__(message)


def validate_customer_slug(raw: str | None) -> str:
    """Fail-loud on blank/None — never defaulted. Returns the
    ``slugify()``-normalized slug so the client-fork's project path matches
    upstream namespacing (component_code_generator uses the same function)."""
    if not raw or not raw.strip():
        raise PackagerInputError(
            "packager_slug_missing",
            "customer_slug is blank/None; refusing to default it.",
            "Provide customer_slug in the run's additional_data.",
        )
    return slugify(raw)


def _api_base(repo_url: str) -> str:
    parts = urlsplit(repo_url)
    return f"{parts.scheme}://{parts.netloc}"


def _subgroup_path(subgroup_url: str) -> str:
    return urlsplit(subgroup_url).path.strip("/")


def client_fork_project_path(slug: str, *, subgroup_url: str) -> str:
    """``<subgroup>/<slug>``, e.g. ``msq-turbo/helix-code-clients/acme``."""
    return f"{_subgroup_path(subgroup_url)}/{slug}"


def _require_env(env: dict) -> tuple[str, str, str]:
    token = env.get(ENV_TOKEN)
    if not token:
        raise PackagerInputError(
            "packager_token_missing",
            f"{ENV_TOKEN} is not set.",
            f"Set {ENV_TOKEN} in the packager service's Coolify environment.",
        )
    repo_url = env.get(ENV_REPO_URL)
    if not repo_url:
        raise PackagerInputError(
            "packager_repo_url_missing",
            f"{ENV_REPO_URL} is not set.",
            f"Set {ENV_REPO_URL} (e.g. https://rmvc01.rm.udg.de/msq-turbo/helix-code).",
        )
    subgroup_url = env.get(ENV_SUBGROUP_URL)
    if not subgroup_url:
        raise PackagerInputError(
            "packager_subgroup_url_missing",
            f"{ENV_SUBGROUP_URL} is not set.",
            f"Set {ENV_SUBGROUP_URL} (e.g. https://rmvc01.rm.udg.de/msq-turbo/helix-code-clients).",
        )
    return token, repo_url, subgroup_url


def _get(client: httpx.Client, url: str, token: str) -> httpx.Response:
    try:
        return client.get(url, headers={"PRIVATE-TOKEN": token})
    except httpx.HTTPError as exc:
        raise PackagerInputError(
            "packager_gitlab_unreachable",
            f"could not reach GitLab at {url}: {exc}",
            "Verify HELIX_CODE_REPO_URL / HELIX_CLIENTS_SUBGROUP_URL and network access.",
        ) from exc


def detect_run_type(
    slug: str,
    *,
    env: dict | None = None,
    client: httpx.Client | None = None,
) -> RunType:
    """Two-stage GitLab probe:

    1. Parent subgroup existence (``GET /api/v4/groups/{subgroup_path}``) —
       a 404 here means the Dirk-owned prerequisite (create
       ``msq-turbo/helix-code-clients``) hasn't happened yet. NOT a run-type
       signal; fails loud pointing at that prerequisite (VT-9).
    2. Client project existence (``GET /api/v4/projects/{project_path}``) —
       404 = first_run (create + seed), 200 = re_run (clone + read lock).
       Anything else fails loud.
    """
    env = env if env is not None else dict(os.environ)
    token, repo_url, subgroup_url = _require_env(env)
    api_base = _api_base(repo_url)
    subgroup_path = _subgroup_path(subgroup_url)
    project_path = client_fork_project_path(slug, subgroup_url=subgroup_url)

    own_client = client is None
    http_client = client or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        group_resp = _get(http_client, f"{api_base}/api/v4/groups/{quote(subgroup_path, safe='')}", token)
        if group_resp.status_code == 404:
            raise PackagerInputError(
                "packager_subgroup_missing",
                f"client-forks subgroup {subgroup_path!r} does not exist on GitLab.",
                "Dirk-owned prerequisite: create the subgroup under msq-turbo "
                "(see task Phase 3 'Dirk-owned prerequisites').",
            )
        if group_resp.status_code != 200:
            raise PackagerInputError(
                "packager_subgroup_check_failed",
                f"unexpected GitLab response {group_resp.status_code} checking subgroup {subgroup_path!r}: "
                f"{group_resp.text[:500]}",
                "Investigate the GitLab API response before retrying.",
            )

        project_resp = _get(http_client, f"{api_base}/api/v4/projects/{quote(project_path, safe='')}", token)
        if project_resp.status_code == 404:
            return "first_run"
        if project_resp.status_code == 200:
            return "re_run"
        if project_resp.status_code in (401, 403):
            raise PackagerInputError(
                "packager_token_invalid",
                f"GitLab rejected {ENV_TOKEN} ({project_resp.status_code}) probing {project_path!r}.",
                f"Verify {ENV_TOKEN} is a valid, unexpired Group Access Token with api scope.",
            )
        raise PackagerInputError(
            "packager_run_detection_failed",
            f"unexpected GitLab response {project_resp.status_code} probing {project_path!r}: "
            f"{project_resp.text[:500]}",
            "Investigate the GitLab API response before retrying.",
        )
    finally:
        if own_client:
            http_client.close()


# =========================================================================== #
# Path-A / Path-B application + barrel regen (Phase 3 steps 4-6)
# =========================================================================== #

ELEMENTS_SRC_PREFIX = "packages/elements/src"
BARREL_REL_PATH = f"{ELEMENTS_SRC_PREFIX}/index.ts"
_BARREL_HEADER = (
    "// Public API of @helix/elements\n"
    "// Importing this module registers all custom elements in the browser.\n"
)
# Same shallow-regex discipline as scaffolding.py's _CUSTOM_ELEMENT_RE/_CLASS_RE
# (no full TS parse) — just enough to mirror a sibling's own type re-export.
_EXPORT_TYPE_NAMES_RE = re.compile(r"export\s+type\s*\{([^}]*)\}\s*from")


# Own-directory siblings that travel with a component (component_code_generator/
# scaffolding.py's own _SIBLING_FILES convention) — never applied to the flat
# organisms/Name.ts convention, only Name/Name.ts.
_SIBLING_FILES = ("types.ts", "index.ts")


def apply_generated_elements(fork_path: str, package_path: str, elements: list[dict]) -> list[str]:
    """Copy each generated element's file from the already-produced customer
    package (``package_path``, written upstream by component_code_generator)
    into the client-fork at the SAME repo-relative path. Grounded, not
    guessed: component_code_generator already writes using helix-code's own
    ``packages/elements/src/{atoms,molecules,organisms}/{Name}/{Name}.ts``
    convention (scaffolding.py's ``_path_candidates``), so no per-element
    atom/molecule/organism categorization is needed here — ``file_path``
    already encodes it. Own-directory components also carry their
    ``types.ts``/``index.ts`` siblings along (mirrors scaffolding.py's
    ``_SIBLING_FILES``; skipped for the flat convention, same as there).
    Each element dict needs at least ``file_path``; entries missing it are
    skipped (nothing to copy)."""
    written: list[str] = []
    for el in elements:
        rel = el.get("file_path")
        if not rel:
            continue
        rel_path = Path(rel)
        src = Path(package_path) / rel_path
        dest = Path(fork_path) / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        written.append(rel)

        if rel_path.stem == rel_path.parent.name:  # own-directory convention (Name/Name.ts)
            for sibling in _SIBLING_FILES:
                sib_src = src.parent / sibling
                if sib_src.exists():
                    sib_dest = dest.parent / sibling
                    sib_dest.write_bytes(sib_src.read_bytes())
                    written.append(str(rel_path.parent / sibling))
    return written


def _sibling_type_names(component_index_ts: Path) -> str | None:
    """Does the just-copied component's own ``index.ts`` re-export types
    (verified real convention — see e.g. helix-code's
    ``atoms/Icon/index.ts``: ``export type { IconName, IconSize } from
    "./types.js";``)? If so, return the bare name list so the top-level
    barrel can mirror it against its OWN import path (the sibling's `from`
    target is relative to itself, not to the top-level barrel, so only the
    names are reused, not the sibling's from-clause)."""
    if not component_index_ts.exists():
        return None
    match = _EXPORT_TYPE_NAMES_RE.search(component_index_ts.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else None


def regenerate_barrel(fork_path: str, elements: list[dict]) -> list[str]:
    """Append newly-applied elements to the fork's top-level barrel
    (``packages/elements/src/index.ts``). Convention verified against the
    real helix-code file (a curated ``// {Name}`` comment + named
    ``export {...} from "./{category}/{Name}/index.js"``, plus a mirrored
    ``export type`` line when the component's own sibling index.ts has one) —
    not a blanket ``export *``. Idempotent: an element whose ``class_name``
    already appears in the barrel text is skipped. Each element dict needs
    ``class_name`` + ``file_path``; entries missing either are skipped."""
    barrel_path = Path(fork_path) / BARREL_REL_PATH
    existing = barrel_path.read_text(encoding="utf-8") if barrel_path.exists() else _BARREL_HEADER

    added: list[str] = []
    blocks: list[str] = []
    for el in elements:
        class_name = el.get("class_name")
        file_path = el.get("file_path")
        if not class_name or not file_path or class_name in existing:
            continue
        rel = Path(file_path)
        try:
            rel_from_src = rel.relative_to(ELEMENTS_SRC_PREFIX)
        except ValueError:
            continue  # not under packages/elements/src — not a barrel-eligible element
        if len(rel_from_src.parts) < 2:
            continue  # flat convention (e.g. organisms/Name.ts) — no per-component sibling to point at
        category, comp_name = rel_from_src.parts[0], rel_from_src.parts[1]
        import_path = f"./{category}/{comp_name}/index.js"

        lines = [f"\n// {comp_name}", f'export {{ {class_name} }} from "{import_path}";']
        type_names = _sibling_type_names(Path(fork_path) / ELEMENTS_SRC_PREFIX / category / comp_name / "index.ts")
        if type_names:
            lines.append(f'export type {{{type_names}}} from "{import_path}";')
        blocks.append("\n".join(lines))
        added.append(class_name)

    if blocks:
        barrel_path.parent.mkdir(parents=True, exist_ok=True)
        barrel_path.write_text(existing.rstrip("\n") + "\n" + "\n".join(blocks) + "\n", encoding="utf-8")
    return added


def story_gen_hook(fork_path: str, elements: list[dict]) -> dict | None:
    """ponytail: pluggable no-op — Phase 4 (deterministic story generator from
    CEM, ``agents/_shared/story_gen/``) hasn't landed yet; the parent task's
    step 6 forward-references it before it exists (Desktop-confirmed
    sequencing bug, amendment 1). Returns None until Phase 4 wires the real
    call in here. KNOWN STUB — flag in the Phase 3 hand-back, not a silent gap."""
    return None


def cem_analyze_hook(fork_path: str) -> dict | None:
    """ponytail: pluggable no-op — Phase 5 (CEM organism indexing fix) hasn't
    landed yet; same forward-reference bug as ``story_gen_hook``. Returns None
    until Phase 5 wires the real ``analyze`` call in here. KNOWN STUB — flag
    in the Phase 3 hand-back, not a silent gap."""
    return None
