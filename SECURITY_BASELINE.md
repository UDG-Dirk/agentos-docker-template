# Container & Hosting Security Baseline

Drop-in reference for any human or coding agent (Claude Code, Codex, …) touching
deployment, Docker, CI/CD, or scaffolding in this repo. Compliance is **mandatory**
for every deployed application at MSQ DX (multiple supply-chain incidents + one
cryptomining RCE — remote code execution — via an outdated dependency prompted this).

> **Canonical source:** Confluence "Container and Hosting Security Baseline"
> (MSQDXAI, page 288982405) and the "GitLab Security Pipeline Templates — Developer
> Guide" (UDGSD, page 279838876). This file is a regenerated digest — if it drifts
> from Confluence, Confluence wins. Owner: Dirk Böhmerle.

## The 9 mandatory container rules

1. **No port exposure outside the Docker network.** Internal services talk over the
   Docker network by service name; external access only via the Traefik/Coolify
   reverse proxy. (Docker's network address translation bypasses the UFW host firewall — *not exposing* is the only reliable control.)
2. **Renovate bot on every repo**, covering all dependency manifests, so security
   updates never go stale. (See [`renovate.json`](renovate.json).)
3. **Security + SBOM pipelines** — SBOM = software bill of materials (a manifest of every
   dependency shipped). `security-scanner` (+ `sbom-generation` once
   Dependency-Track vars exist) as GitLab CI/CD Components. A prerequisite for
   deployment. (See [`.gitlab-ci.yml`](.gitlab-ci.yml).)
4. **rmvc01 GitLab only** for hosted projects — no personal repos, no ad-hoc hosting.
5. **Secrets never in the image or repo.** Environment variables or Coolify secret
   management only. `.env` is never committed (it is gitignored here).
6. **`.dockerignore` required** — exclude `.git`, `.env`, `node_modules`, build
   artifacts, and local config from the build context.
7. **Non-root user in the Dockerfile.** A `USER` directive is mandatory; running as
   root is allowed only as a documented exception.
8. **Health checks + log rotation.** A healthcheck in the compose config; log output
   bounded (`max-size`).
9. **Documented owner.** Each project/container has an identifiable responsible
   person, findable within minutes.

## Security pipeline (this repo)

`.gitlab-ci.yml` integrates `pipeline-templates/security/security-scanner` (pinned to
an exact tag, e.g. `@v4.0.1`):

- **Grype** — CVEs (publicly catalogued security vulnerabilities) in packages/images. Gate: **block on `critical`**.
- **Gitleaks** — secret detection. Gate: **block on any finding**. False positives
  are documented and narrowly allowlisted in [`.gitleaks.toml`](.gitleaks.toml)
  (`useDefault = true`, every ignore carries a reason + review date).
- **Checkov** (infrastructure-as-code / Dockerfile misconfig), **OSV** (dependency CVEs), **Socket**
  (supply-chain risk) — currently **report-only** (findings surface in the MR
  Security widget without blocking).
- **SBOM** (Syft → Dependency-Track) is deferred until `DTRACK_API_URL` /
  `DTRACK_API_KEY` CI/CD variables are provisioned.

Tighten a scanner to blocking by setting its `<tool>-gate-enabled: true` input.
Add narrow, justified ignores via `.grype.yaml` / `.gitleaks.toml` / `osv-scanner.toml`
only when a finding is consciously accepted (reason + review date, per the guide's
review checklist).

## When you change deployment surface

- **Dockerfile / compose:** verify rules 1, 5, 6, 7, 8.
- **CI/CD:** keep the security pipeline present and green (gates: secrets + critical CVEs).
- **New scaffolds:** this file, `.dockerignore`, `.gitignore` (covering `.env`),
  `renovate.json`, and the security pipeline from day one.
