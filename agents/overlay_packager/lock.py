"""Overlay Packager — helix-lock.json v1 (v0.3 Phase 3).

The lock file is the AUTHORITATIVE re-run signal, persisted at the client
fork's root — survives volume loss (Phase 0: never infer run type from local
cache emptiness, the volume is cache-only). Schema v1: ``helix_lock_version``,
``customer_slug``, ``forked_from`` (repo/ref/sha), ``runs`` (per-run
provenance), ``organisms`` (FM3 source-shift baseline), ``tokens`` (coverage
bookkeeping, e.g. leaf counts from token_substitution.py's CoverageReport).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOCK_FILENAME = "helix-lock.json"
LOCK_VERSION = 1


class LockError(Exception):
    """Fail-loud: missing, malformed, or version-mismatched helix-lock.json.
    Carries the {code, message, remediation} blocking-warning payload
    (ratified convention — see fork_ops.ForkOpsError)."""

    def __init__(self, code: str, message: str, remediation: str):
        self.code = code
        self.message = message
        self.remediation = remediation
        super().__init__(message)


@dataclass
class ForkedFrom:
    repo: str
    ref: str
    sha: str


@dataclass
class RunFigma:
    file_key: str
    revision: str


@dataclass
class RunRecord:
    run_id: str
    timestamp: str
    figma: RunFigma


@dataclass
class OrganismSource:
    source: str
    sha: str


@dataclass
class HelixLock:
    customer_slug: str
    forked_from: ForkedFrom
    runs: list[RunRecord] = field(default_factory=list)
    organisms: dict[str, OrganismSource] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)
    helix_lock_version: int = LOCK_VERSION

    def to_dict(self) -> dict:
        return {
            "helix_lock_version": self.helix_lock_version,
            "customer_slug": self.customer_slug,
            "forked_from": asdict(self.forked_from),
            "runs": [
                {"run_id": r.run_id, "timestamp": r.timestamp, "figma": asdict(r.figma)}
                for r in self.runs
            ],
            "organisms": {name: asdict(o) for name, o in self.organisms.items()},
            "tokens": dict(self.tokens),
        }

    @classmethod
    def from_dict(cls, data: dict) -> HelixLock:
        version = data.get("helix_lock_version")
        if version != LOCK_VERSION:
            raise LockError(
                "lock_version_unsupported",
                f"helix-lock.json version {version!r} unsupported "
                f"(packager understands v{LOCK_VERSION} only).",
                f"Regenerate {LOCK_FILENAME} with a v{LOCK_VERSION}-compatible packager build.",
            )
        try:
            forked_from = ForkedFrom(**data["forked_from"])
            runs = [
                RunRecord(run_id=r["run_id"], timestamp=r["timestamp"], figma=RunFigma(**r["figma"]))
                for r in data.get("runs", [])
            ]
            organisms = {name: OrganismSource(**o) for name, o in data.get("organisms", {}).items()}
        except (KeyError, TypeError) as exc:
            raise LockError(
                "lock_malformed",
                f"malformed {LOCK_FILENAME}: {exc}",
                f"Inspect {LOCK_FILENAME} against the schema v{LOCK_VERSION} shape.",
            ) from exc
        return cls(
            customer_slug=data["customer_slug"],
            forked_from=forked_from,
            runs=runs,
            organisms=organisms,
            tokens=dict(data.get("tokens", {})),
        )


def read_lock(fork_path: str) -> HelixLock:
    path = Path(fork_path) / LOCK_FILENAME
    if not path.exists():
        raise LockError(
            "lock_missing",
            f"{LOCK_FILENAME} not found at fork root {fork_path!r}.",
            "First-run should have written it; re-run detection may be misclassified — verify run_type.",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LockError(
            "lock_malformed",
            f"malformed {LOCK_FILENAME}: {exc}",
            f"Inspect {LOCK_FILENAME} against the schema v{LOCK_VERSION} shape.",
        ) from exc
    return HelixLock.from_dict(raw)


def write_lock(fork_path: str, lock: HelixLock) -> str:
    path = Path(fork_path) / LOCK_FILENAME
    path.write_text(json.dumps(lock.to_dict(), indent=2) + "\n", encoding="utf-8")
    return str(path)


@dataclass
class SourceShift:
    organism: str
    previous: str  # "<source>@<sha>"
    current: str


def detect_source_shift(
    lock: HelixLock,
    organism_sources: dict[str, OrganismSource],
) -> list[SourceShift]:
    """FM3: compare each incoming organism's canonical source (branch@sha vs
    master@sha, from 3d Path-A output) against the lock's recorded baseline.
    Returns one ``SourceShift`` per drifted organism — empty on first-run
    (nothing recorded yet to compare against) or when nothing moved."""
    shifts: list[SourceShift] = []
    for name, incoming in organism_sources.items():
        prior = lock.organisms.get(name)
        if prior is None:
            continue
        if prior.source != incoming.source or prior.sha != incoming.sha:
            shifts.append(
                SourceShift(
                    organism=name,
                    previous=f"{prior.source}@{prior.sha}",
                    current=f"{incoming.source}@{incoming.sha}",
                )
            )
    return shifts
