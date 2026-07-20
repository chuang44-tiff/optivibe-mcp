"""_envelope.py — the reference-layer expected-failure envelope.

Re-implemented (NOT imported) from the harness ``_analysis_common.error_envelope``
shape so the reference package stays decoupled. A handler constructs this for an
EXPECTED failure class (unknown operand, malformed FTS query); it does NOT raise
past its own boundary for those.
"""


def error_envelope(tool, error_family, error, **extra):
    """Build the locked expected-failure envelope (never raised past boundary).

    ``{"ok": False, "tool": tool, "error_family": error_family, "error": error}``
    plus any tool-specific diagnostic fields in ``extra`` (e.g. ``query=...``).
    Mirrors the harness 4-key+extra shape exactly.
    """
    env = {"ok": False, "tool": tool, "error_family": error_family, "error": error}
    env.update(extra)
    return env
