"""v0.3 Phase 3 verification tests — overlay packager workflow (offline-mockable
subset, VT-1..VT-12). VT-13..VT-15 are live-gated (real GitLab client-fork) and
are NOT in this file — they require the Dirk-owned prerequisites (subgroup +
Group Access Token) and run against a real scratch client-fork, never in CI.
"""

from __future__ import annotations

import httpx
import pytest

from agents.component_code_generator.scaffolding import slugify as ccg_slugify
from agents.overlay_packager import fork_ops, mr_ops, packaging
from agents.overlay_packager import lock as lock_mod
from agents.overlay_packager.fork_ops import ClientProject, ForkResult
from agents.overlay_packager.step import overlay_packager_executor

ENV = {
    packaging.ENV_TOKEN: "test-token",
    packaging.ENV_REPO_URL: "https://rmvc01.rm.udg.de/msq-turbo/helix-code",
    packaging.ENV_SUBGROUP_URL: "https://rmvc01.rm.udg.de/msq-turbo/helix-code-clients",
    packaging.ENV_GIT_USERNAME: "agno-helix-code",
}


class _FakeStepInput:
    """Minimal StepInput stand-in (same idiom as tests/baseline_reader/*)."""

    def __init__(self, additional_data=None, run_id=None):
        self.additional_data = additional_data or {}
        self.run_id = run_id


def _set_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


# --------------------------------------------------------------------------- #
# VT-1 — fail-loud on blank slug -> blocking_warning envelope
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_slug", [None, "", "   "])
def test_vt1_blank_slug_blocking_warning(bad_slug):
    out = overlay_packager_executor(_FakeStepInput({"customer_slug": bad_slug}))
    assert out.success is False
    assert out.content["blocking_warning"]["code"] == "packager_slug_missing"


# --------------------------------------------------------------------------- #
# VT-4 — source-shift detection triggers the ratified MR comment
# --------------------------------------------------------------------------- #
def test_vt4_source_shift_triggers_mr_comment():
    prior_lock = lock_mod.HelixLock(
        customer_slug="acme",
        forked_from=lock_mod.ForkedFrom(repo="msq-turbo/helix-code", ref="master", sha="a" * 40),
        organisms={"MediaText": lock_mod.OrganismSource(source="feature/organisms/MediaText", sha="abc123")},
    )
    incoming = {"MediaText": lock_mod.OrganismSource(source="master", sha="def456")}

    shifts = lock_mod.detect_source_shift(prior_lock, incoming)
    assert len(shifts) == 1
    comment = mr_ops.render_source_shift_comment(shifts[0])
    assert comment == (
        "MediaText source shifted from `feature/organisms/MediaText@abc123` to `master@def456` "
        "— review substantive changes."
    )


def test_vt4_no_shift_when_source_unchanged():
    prior_lock = lock_mod.HelixLock(
        customer_slug="acme",
        forked_from=lock_mod.ForkedFrom(repo="msq-turbo/helix-code", ref="master", sha="a" * 40),
        organisms={"MediaText": lock_mod.OrganismSource(source="master", sha="def456")},
    )
    incoming = {"MediaText": lock_mod.OrganismSource(source="master", sha="def456")}
    assert lock_mod.detect_source_shift(prior_lock, incoming) == []


# --------------------------------------------------------------------------- #
# VT-5 — MR description renders per the ratified format
# --------------------------------------------------------------------------- #
def test_vt5_mr_description_format():
    desc = mr_ops.render_mr_description(
        change_summary="Adds IconButton",
        run_id="run123",
        timestamp="2026-08-07T10:00:00.000Z",
        figma_source=mr_ops.FigmaSource(file_key="ABC123", revision="rev1"),
        predecessor_branch="feat/scaffolding-and-tokens-run-run123",
        gates=[mr_ops.GateOutcome(name="pnpm typecheck", passed=True, duration_seconds=12.4)],
    )
    assert "## Change summary" in desc
    assert "Adds IconButton" in desc
    assert "## Provenance" in desc
    assert "- HELIX run_id: run123" in desc
    assert "- Timestamp: 2026-08-07T10:00:00.000Z" in desc
    assert "- Figma source: ABC123@rev1" in desc
    assert "- Predecessor MR: feat/scaffolding-and-tokens-run-run123" in desc
    assert "## Structural gates" in desc
    assert "- `pnpm typecheck`: ✓ (12s)" in desc


def test_vt5_mr_description_no_predecessor_for_base_mr():
    desc = mr_ops.render_mr_description(
        change_summary="Scaffolding",
        run_id="run1",
        timestamp="2026-08-07T00:00:00.000Z",
        figma_source=mr_ops.FigmaSource(file_key="F", revision="r"),
        predecessor_branch=None,
        gates=[],
    )
    assert "(none — base of the stack)" in desc


# --------------------------------------------------------------------------- #
# VT-6 — MR comment renders per the ratified format for flagged incompatibilities
# --------------------------------------------------------------------------- #
def test_vt6_incompatibility_comment_format():
    text = mr_ops.render_incompatibility_comment("color.brand.primary", "color.brand.accent")
    assert text == (
        "Token `color.brand.primary` in customer Figma has no HELIX-compatible mapping; "
        "using nearest neighbor `color.brand.accent`. Please review."
    )


# --------------------------------------------------------------------------- #
# VT-7 — coverage-gate failure surfaces in the run result + fails the run
# --------------------------------------------------------------------------- #
def test_vt7_coverage_gate_failure_surfaces_in_result(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    fork = tmp_path / "fork"
    token_dir = fork / "packages" / "tokens" / "tokens" / "semantic-color"
    token_dir.mkdir(parents=True)
    (token_dir / "light-mode.tokens.json").write_text(
        '{"colors": {"brand": {"primary": {"$type": "color", "$value": "#FFFFFF"}, '
        '"secondary": {"$type": "color", "$value": "#000000"}}}}',
        encoding="utf-8",
    )
    lock_mod.write_lock(
        str(fork),
        lock_mod.HelixLock(
            customer_slug="acme",
            forked_from=lock_mod.ForkedFrom(repo="msq-turbo/helix-code", ref="master", sha="a" * 40),
        ),
    )

    monkeypatch.setattr(packaging, "detect_run_type", lambda slug, **kw: "re_run")
    monkeypatch.setattr(
        fork_ops, "get_client_project",
        lambda project_path, api_base, token, **kw: ClientProject(
            id=0, path_with_namespace=project_path, http_url_to_repo="", web_url="",
        ),
    )
    monkeypatch.setattr(fork_ops, "clone_or_fetch_existing_fork", lambda project, **kw: str(fork))

    import agents.overlay_packager.step as step_mod
    monkeypatch.setattr(
        step_mod, "run_all_structural_gates",
        lambda fork_path: {"pnpm build:tokens": step_mod.BuildGateResult(ran=True, passed=True, duration_seconds=1.0)},
    )

    out = overlay_packager_executor(
        _FakeStepInput({
            "customer_slug": "acme",
            "customer_token_map": {"colors.brand.primary": "#0000FF"},  # colors.brand.secondary left unmatched
        })
    )
    assert out.success is False
    assert out.content["status"] == "failure"
    assert out.content["coverage_failure"] is not None
    assert "colors.brand.secondary" in out.content["coverage_failure"]


# --------------------------------------------------------------------------- #
# VT-8 — missing GITLAB_HELIX_CLIENTS_TOKEN produces an actionable fail-loud
# --------------------------------------------------------------------------- #
def test_vt8_missing_token_fails_loud(monkeypatch):
    monkeypatch.delenv(packaging.ENV_TOKEN, raising=False)
    env = {k: v for k, v in ENV.items() if k != packaging.ENV_TOKEN}
    with pytest.raises(packaging.PackagerInputError) as exc_info:
        packaging.detect_run_type("acme", env=env)
    assert exc_info.value.code == "packager_token_missing"


# --------------------------------------------------------------------------- #
# VT-9 — subgroup non-existence (404 on the parent group) is distinct from a
# first-run project 404, and points at the Dirk-owned prerequisite
# --------------------------------------------------------------------------- #
def test_vt9_subgroup_missing_fails_loud():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/groups/" in request.url.path
        return httpx.Response(404, json={"message": "404 Group Not Found"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(packaging.PackagerInputError) as exc_info:
        packaging.detect_run_type("acme", env=ENV, client=client)
    assert exc_info.value.code == "packager_subgroup_missing"
    assert "prerequisite" in exc_info.value.remediation.lower()


def test_vt9_subgroup_present_project_missing_is_first_run():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/groups/" in request.url.path:
            return httpx.Response(200, json={"id": 999})
        return httpx.Response(404, json={"message": "404 Project Not Found"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert packaging.detect_run_type("acme", env=ENV, client=client) == "first_run"


# --------------------------------------------------------------------------- #
# VT-10 — helix-lock.json schema v1 round-trip preserves integrity
# --------------------------------------------------------------------------- #
def test_vt10_lock_roundtrip(tmp_path):
    original = lock_mod.HelixLock(
        customer_slug="acme",
        forked_from=lock_mod.ForkedFrom(repo="msq-turbo/helix-code", ref="master", sha="a" * 40),
        runs=[
            lock_mod.RunRecord(
                run_id="r1", timestamp="2026-08-07T00:00:00.000Z",
                figma=lock_mod.RunFigma(file_key="F1", revision="rev1"),
            )
        ],
        organisms={"MediaText": lock_mod.OrganismSource(source="master", sha="b" * 40)},
        tokens={"semantic_color_light_leaf_count": 210},
    )
    lock_mod.write_lock(str(tmp_path), original)
    reread = lock_mod.read_lock(str(tmp_path))
    assert reread == original


def test_vt10_lock_version_mismatch_fails_loud(tmp_path):
    (tmp_path / lock_mod.LOCK_FILENAME).write_text('{"helix_lock_version": 2}', encoding="utf-8")
    with pytest.raises(lock_mod.LockError):
        lock_mod.read_lock(str(tmp_path))


def test_vt10_lock_missing_fails_loud(tmp_path):
    with pytest.raises(lock_mod.LockError):
        lock_mod.read_lock(str(tmp_path))


# --------------------------------------------------------------------------- #
# VT-11 — slugify() reuse: literally the same function as component_code_generator
# --------------------------------------------------------------------------- #
def test_vt11_slugify_is_the_same_function_as_component_code_generator():
    assert packaging.slugify is ccg_slugify
    assert packaging.validate_customer_slug("Acme Corp") == ccg_slugify("Acme Corp")


# --------------------------------------------------------------------------- #
# VT-12 — baseline_ref -> SHA resolution format is consistent with what
# baseline_reader's own checkout mechanism produces (git rev-parse/ls-remote
# both yield a 40-hex-char SHA-1; fork_ops's `git ls-remote` parse must match)
# --------------------------------------------------------------------------- #
def test_vt12_resolved_sha_format_matches_baseline_reader():
    sha = "ab1234ef" * 5  # 40 hex chars — full SHA-1 length

    class _LsRemoteProc:
        returncode = 0
        stdout = f"{sha}\trefs/heads/master\n"
        stderr = ""

    result = fork_ops.create_or_fetch_client_fork(
        "acme", "master", _git_runner=lambda args, **kw: _LsRemoteProc()
    )
    assert result.resolved_sha == sha
    assert len(result.resolved_sha) == 40  # same SHA-1 hex length as `git rev-parse HEAD`


# --------------------------------------------------------------------------- #
# VT-2 — first-run creates project + seeds + writes initial lock
# (GitLab REST mocked via httpx.MockTransport; git plumbing faked — Phase 2
# already proves the ls-remote/clone/push mechanics in isolation)
# --------------------------------------------------------------------------- #
def _gitlab_transport(project_body: dict, *, project_exists: bool = True):
    """``project_exists`` controls the ``GET .../projects/{path}`` probe
    ``detect_run_type`` makes BEFORE any project is created — False for a
    first-run test (404 -> first_run), True for a re-run test (200 ->
    re_run). Independent of the ``POST /projects`` create response, which
    always succeeds once called."""
    state = {"mr_iid": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and "/groups/" in path:
            return httpx.Response(200, json={"id": 999})
        if method == "POST" and path.endswith("/projects"):
            return httpx.Response(201, json=project_body)
        if method == "GET" and "/projects/" in path and "/repository/" not in path and "merge_requests" not in path:
            if project_exists:
                return httpx.Response(200, json=project_body)
            return httpx.Response(404, json={"message": "404 Project Not Found"})
        if method == "POST" and path.endswith("/repository/branches"):
            return httpx.Response(201, json={})
        if method == "POST" and path.endswith("/merge_requests"):
            state["mr_iid"] += 1
            n = state["mr_iid"]
            return httpx.Response(201, json={"iid": n, "web_url": f"https://rmvc01.rm.udg.de/mr/{n}"})
        if method == "POST" and path.endswith("/notes"):
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": f"unhandled test route: {method} {path}"})

    return httpx.MockTransport(handler)


def _patch_transport(monkeypatch, transport):
    # fork_ops.httpx and mr_ops.httpx are the SAME shared module object (one entry
    # in sys.modules) — capture the real class first, or the lambda's own
    # `httpx.Client(...)` call recurses into itself once patched.
    real_client_cls = httpx.Client
    factory = lambda *a, **kw: real_client_cls(transport=transport)
    monkeypatch.setattr(fork_ops.httpx, "Client", factory)
    monkeypatch.setattr(mr_ops.httpx, "Client", factory)


def _patch_no_op_gates(monkeypatch):
    import agents.overlay_packager.step as step_mod
    monkeypatch.setattr(
        step_mod, "run_all_structural_gates",
        lambda fork_path: {"pnpm build:tokens": step_mod.BuildGateResult(ran=True, passed=True, duration_seconds=1.0)},
    )


def test_vt2_first_run_creates_project_seeds_and_writes_lock(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    project_body = {
        "id": 42,
        "path_with_namespace": "msq-turbo/helix-code-clients/acme",
        "http_url_to_repo": "https://rmvc01.rm.udg.de/msq-turbo/helix-code-clients/acme.git",
        "web_url": "https://rmvc01.rm.udg.de/msq-turbo/helix-code-clients/acme",
    }
    _patch_transport(monkeypatch, _gitlab_transport(project_body, project_exists=False))
    _patch_no_op_gates(monkeypatch)

    fork_dir = tmp_path / "fork"
    monkeypatch.setattr(
        fork_ops, "create_or_fetch_client_fork",
        lambda slug, ref, **kw: ForkResult(slug=slug, baseline_ref=ref, resolved_sha="c" * 40, fork_path=str(fork_dir)),
    )
    seeded = {"called": False}

    def _fake_seed(project, clean_url, resolved_sha, **kw):
        seeded["called"] = True

    monkeypatch.setattr(fork_ops, "seed_client_project", _fake_seed)

    def _fake_clone_or_fetch(project, **kw):
        fork_dir.mkdir(exist_ok=True)
        return str(fork_dir)

    monkeypatch.setattr(fork_ops, "clone_or_fetch_existing_fork", _fake_clone_or_fetch)

    out = overlay_packager_executor(_FakeStepInput({"customer_slug": "acme"}, run_id="run-vt2"))

    assert seeded["called"] is True
    assert out.content["run_type"] == "first_run"
    assert out.content["project"]["path_with_namespace"] == "msq-turbo/helix-code-clients/acme"
    lock_path = fork_dir / lock_mod.LOCK_FILENAME
    assert lock_path.exists()
    written_lock = lock_mod.read_lock(str(fork_dir))
    assert written_lock.customer_slug == "acme"
    assert written_lock.forked_from.sha == "c" * 40
    assert len(written_lock.runs) == 1
    assert written_lock.runs[0].run_id == "run-vt2"
    assert "merge_requests" in out.content
    assert len(out.content["merge_requests"]) == 4


# --------------------------------------------------------------------------- #
# VT-3 — re-run detects lock, applies changes, opens 4 stacked MRs
# --------------------------------------------------------------------------- #
def test_vt3_re_run_detects_lock_and_opens_stacked_mr_bundle(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    fork_dir = tmp_path / "fork"
    fork_dir.mkdir()
    lock_mod.write_lock(
        str(fork_dir),
        lock_mod.HelixLock(
            customer_slug="acme",
            forked_from=lock_mod.ForkedFrom(repo="msq-turbo/helix-code", ref="master", sha="a" * 40),
        ),
    )
    project_body = {
        "id": 42,
        "path_with_namespace": "msq-turbo/helix-code-clients/acme",
        "http_url_to_repo": "https://rmvc01.rm.udg.de/msq-turbo/helix-code-clients/acme.git",
        "web_url": "https://rmvc01.rm.udg.de/msq-turbo/helix-code-clients/acme",
    }
    _patch_transport(monkeypatch, _gitlab_transport(project_body))
    _patch_no_op_gates(monkeypatch)
    monkeypatch.setattr(packaging, "detect_run_type", lambda slug, **kw: "re_run")
    monkeypatch.setattr(fork_ops, "clone_or_fetch_existing_fork", lambda project, **kw: str(fork_dir))

    out = overlay_packager_executor(_FakeStepInput({"customer_slug": "acme"}, run_id="run-vt3"))

    assert out.content["run_type"] == "re_run"
    assert "merge_requests" in out.content
    mrs = out.content["merge_requests"]
    assert list(mrs.keys()) == list(mr_ops.MR_STACK_ORDER)
    assert [mrs[stage]["iid"] for stage in mr_ops.MR_STACK_ORDER] == [1, 2, 3, 4]

    updated_lock = lock_mod.read_lock(str(fork_dir))
    assert len(updated_lock.runs) == 1
    assert updated_lock.runs[0].run_id == "run-vt3"
