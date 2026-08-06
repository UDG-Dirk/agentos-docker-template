"""Overlay Packager (v0.3 Phase 2+) — fork-then-overlay customer packaging.

Architecture B (Dirk-ratified rev.8.5.3, 2026-08-06): customer branding is a
token VALUE swap in the fork's Style Dictionary source. Token NAMES and
``--helix-*`` CSS var refs are preserved throughout (Correction #18, on main
via MR !47). 3c is no longer a separate station for this step — the packager
owns it directly. See ``token_substitution.py`` (the swap + FM1 coverage
gate) and ``fork_ops.py`` (fork addressing; Phase 2 offline-testable subset).
"""
