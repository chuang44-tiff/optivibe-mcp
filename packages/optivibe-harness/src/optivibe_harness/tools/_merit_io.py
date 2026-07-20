"""tools/_merit_io.py — the native merit-file magic oracle + path resolution.

NOT dispatchable (no ``TOOL_SPECS``). Two pieces the ``save_merit`` / ``load_merit``
tools reuse:

- ``_is_merit_file(path)`` — the magic-byte durability oracle (§6, sibling
  to ``_image_gate._is_png``). The probe pinned the bytes: a real merit file starts
  ``FF FE 56 00 45 00 52 00 53 00`` = a UTF-16LE BOM followed by ``"VERS"``. A
  0-byte / wrong-magic file (the headless-``ToFile``-writes-TEXT trap that motivated
  ``_is_png``) fails the oracle.

  **CORRECTION (§6 prose was buggy):** the
  two BOM bytes (``FF FE``) MUST be SKIPPED before the UTF-16LE decode — decoding
  them yields a leading zero-width no-break space so ``decode(...).startswith("VERS")``
  fails and a VALID merit file is REJECTED. The oracle therefore checks
  ``head[:2] == b"\\xff\\xfe"`` AND ``head[2:].decode("utf-16-le").startswith("VERS")``.

- ``resolve_merit_path(session, path)`` — the path resolver (persistence-workspace
  D6): a RELATIVE path resolves under the workspace ROOT (``workspace._resolve_root``
  — FLAT under ``workspace_root``, else the legacy ``projects/`` root); the result is
  ``os.path.normpath``-unified; a leading ``projects/`` is DE-DUPED (the dogfooded
  agent habit ``projects/<name>/file.MF`` degrades gracefully instead of doubling); a
  RELATIVE path that escapes the root via ``..`` is REJECTED (traversal guard). An
  ABSOLUTE path is used as-is (unconfined BY DESIGN — explicit intent). An empty /
  non-str path is rejected (the caller turns the rejection into a ``merit_io_path``
  envelope).

Live ZOS-API integration: the oracle is byte-grounded by the captured ``.MF`` head;
unit-tested on the real magic
bytes + a text-named file (must reject). NEVER touches the backend.
"""
import os
import re

# The UTF-16LE byte-order mark every native merit file opens with.
_UTF16LE_BOM = b"\xff\xfe"

# D6: dedupe a single leading ``projects/`` (or ``projects\``) segment so the dogfooded
# agent habit ``projects/<name>/file.MF`` does not double under a projects/-ending root
# (and is simply rooted-flat under workspace_root). Anchored at the start only.
_LEADING_PROJECTS_RE = re.compile(r"^projects[\\/]+")

# After the BOM the file begins with the literal ``"VERS"`` (the engine's
# own versioned text merit format, e.g. ``"VERS 24"``). Read enough bytes to cover
# the BOM + the 4-char tag decoded as UTF-16LE (2 + 4*2 = 10 bytes; read a few more
# for safety).
_VERS_TAG = "VERS"
_HEAD_BYTES = 16


def _is_merit_file(path) -> bool:
    """True iff ``path`` is a regular non-empty file with the native merit magic head.

    The magic oracle (§6 + the BOM fix):

    - ``path`` must be a regular file (``os.path.isfile``) with size > 0;
    - its first bytes must be the UTF-16LE BOM ``FF FE`` AND, **after SKIPPING the 2
      BOM bytes**, the UTF-16LE-decoded head must ``startswith("VERS")``.

    NEVER raises (documented unconditionally safe, like ``_is_png``): a missing
    file, a directory, any ``OSError``, ``None`` (``TypeError``) or a NUL-embedded
    path (``ValueError``) all yield ``False``.
    """
    try:
        if not os.path.isfile(path):
            return False
        if os.path.getsize(path) <= 0:
            return False
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except (OSError, TypeError, ValueError):
        return False
    if head[:2] != _UTF16LE_BOM:
        return False
    # FIX: skip the 2 BOM bytes BEFORE decoding — decoding them yields a
    # leading zero-width no-break space, which would make startswith("VERS") fail on
    # a VALID merit file (the buggy §6 prose decoded the BOM too).
    decoded = head[2:].decode("utf-16-le", errors="ignore")
    return decoded.startswith(_VERS_TAG)


def resolve_merit_path(session, path):
    """Resolve a merit-file ``path`` (persistence-workspace D6).

    Returns ``(resolved_path, error_or_None)``. Rules:

    - an EMPTY / non-``str`` ``path`` -> ``(None, <message>)`` (the caller turns the
      message into a ``merit_io_path`` envelope);
    - an ABSOLUTE ``path`` -> ``os.path.normpath`` then used as-is (unconfined BY
      DESIGN — explicit intent, no traversal guard);
    - a RELATIVE ``path`` -> resolved UNDER the workspace ROOT
      (``workspace._resolve_root(session)`` — FLAT under ``workspace_root``, else the
      legacy ``projects/`` root), with: a leading ``projects/`` segment DE-DUPED (the
      ``projects/<name>/file`` agent habit no longer doubles under a projects/-ending
      root, bug 4); the join ``os.path.normpath``-unified (collapses the mixed
      ``\\projects\\projects/`` separators bug 4 also showed); and a TRAVERSAL GUARD —
      a relative path that ``..``-escapes the root is REJECTED (``merit_io_path``).

    NEVER raises: a resolution helper failure degrades to the CWD-anchored
    ``projects/`` root (the same last-resort the resolver already uses); a deleted CWD
    degrades to the ``merit_io_path`` error tuple, never an escape.
    """
    if not isinstance(path, str) or path.strip() == "":
        return None, f"path must be a non-empty string, got {path!r}"
    if os.path.isabs(path):
        # Absolute: normalize (unify separators / collapse ``.``/``..``) but DO NOT
        # confine — an explicit absolute path is the documented escape hatch.
        return os.path.normpath(path), None
    # Relative: resolve under the workspace root. Import lazily to avoid any
    # import-time coupling into the workspace tool module.
    try:
        from .workspace import _resolve_root

        root, _flat = _resolve_root(session)
    except Exception:  # noqa: BLE001 — a resolver failure degrades to a CWD anchor
        # M5 fix: os.getcwd() itself can raise OSError (a deleted CWD) — that would
        # break the documented "NEVER raises" contract. Guard the fallback so a
        # deleted-CWD degrades to the merit_io_path error tuple, not an escape.
        try:
            root = os.path.join(os.getcwd(), "projects")
        except OSError as exc:
            return None, (
                f"could not resolve the workspace root for {path!r} "
                f"({exc!r}); the current working directory is unavailable"
            )
    # D6 DEDUPE: strip a single leading ``projects/`` so ``projects/<name>/file.MF``
    # (the dogfooded agent habit) does not double under a projects/-ending legacy root
    # — and is simply rooted flat under a workspace_root. Anchored at the start only.
    rel = _LEADING_PROJECTS_RE.sub("", path, count=1)
    # D6 NORMPATH: collapse mixed separators (the ``\projects\projects/...`` bug-4
    # artifact) + redundant ``.``/``..`` into the platform form.
    resolved = os.path.normpath(os.path.join(root, rel))
    # D6 TRAVERSAL GUARD: a RELATIVE path that escapes the root via ``..`` is rejected
    # (absolute paths are unconfined above). os.path.commonpath raises ValueError on
    # mixed drives (Windows) — treat that as an escape too. The root is itself
    # normpath'd so the comparison is apples-to-apples.
    norm_root = os.path.normpath(root)
    try:
        if os.path.commonpath([norm_root, resolved]) != norm_root:
            return None, (
                f"merit path {path!r} resolves to {resolved!r}, which escapes the "
                f"workspace root {norm_root!r}; a relative merit path must stay under "
                "the workspace (use an absolute path for an out-of-tree write)"
            )
    except ValueError:
        # Different drives / un-comparable -> the relative path escaped the root.
        return None, (
            f"merit path {path!r} resolves to {resolved!r}, which is not under the "
            f"workspace root {norm_root!r}"
        )
    return resolved, None


__all__ = ["_is_merit_file", "resolve_merit_path"]
