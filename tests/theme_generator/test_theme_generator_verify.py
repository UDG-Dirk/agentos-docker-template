"""The executable verification gate must stay green (Phase 4 riff)."""
from __future__ import annotations

from agents.theme_generator import verify


def test_verify_gate_all_checks_pass():
    report = verify.build_report()
    failed = [(vt, desc) for vt, desc, ok in report if not ok]
    assert not failed, f"verification gate has failing checks: {failed}"
    assert len(report) >= 9  # the offline-closable VT set


def test_verify_main_exit_zero(capsys):
    assert verify.main() == 0
    out = capsys.readouterr().out
    assert "checks passed" in out
    assert "FAIL" not in out
