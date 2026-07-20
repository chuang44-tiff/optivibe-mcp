"""tools/_image_gate.py — the shared PNG-magic durability oracle (NOT dispatchable).

Two consumers, ONE oracle (§5):

- ``render_layout``'s save gate (§3.8) — a temp file must pass ``_is_png`` before
  the atomic ``os.replace`` into the final path.
- ``capture_graphic``'s gate (§5) — replaces the old ``size >= 128`` byte gate.
  Headless ``ToFile`` writes a TEXT dump, so a >128-byte text file
  named ``*.png`` used to pass the byte gate silently. The magic-byte gate rejects
  it, closing the silent text-as-PNG bug.

This module is a pure helper: it exports no ``TOOL_SPEC``/``TOOL_SPECS`` and the
server does NOT register it. The oracle NEVER raises — a missing file, an OSError,
a directory, ``None`` (TypeError), or a NUL-embedded path (ValueError) all yield
``False``.
"""
import os

# The 8-byte PNG signature (§5). A real PNG always begins with these
# exact bytes; a text dump named ``*.png`` does not.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _is_png(path) -> bool:
    """True iff ``path`` is a regular file whose first 8 bytes are the PNG signature.

    NEVER raises (documented as unconditionally safe): a missing file, a directory,
    or any ``OSError`` returns ``False``; ``None`` (``TypeError``) and a NUL-embedded
    path (``ValueError``) — both of which escape a bare ``except OSError`` — return
    ``False`` too.
    """
    try:
        if not os.path.isfile(path):
            return False
        with open(path, "rb") as fh:
            return fh.read(8) == _PNG_MAGIC
    except (OSError, TypeError, ValueError):
        return False
