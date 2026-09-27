"""THE ONE CYCLE-SAFE DOOR from a tool module into ``workspace``'s path resolution.

Created in response to an audit finding. Four copies of two helpers were shipped by
the cycle whose whole thesis is ONE authority:

    _workspace_root : tools/layout_render.py:654  +  tools/optimize_run.py:859
                      (identical but for the tail of a noqa comment)
    _scratch_dir    : tools/layout_render.py:663  +  tools/analysis_graphic.py:133
                      (BYTE-IDENTICAL)

Each independently re-entered ``workspace._resolve_root(session)``, and two of those
copies are the exact call sites named in the ticket this cycle filed about
``render_layout`` resolving the root twice. **The cycle created the seventh instance of
the class it filed the seventh ticket about.**

WHY A LEAF MODULE AND NOT ``workspace.py`` ITSELF, which is what the audit suggested.
Measured before deviating: ``workspace.py:185`` does a MODULE-LEVEL
``from .layout_render import render_layout``, so ``layout_render`` cannot import
``workspace`` at module level without a cycle — which is precisely why all four copies
carried a DEFERRED ``from .workspace import _resolve_root`` inside the function body.
Putting the shared helpers in ``workspace`` would therefore force a deferred import at
every CALL site (two per consumer) and ADD statements rather than remove them. A leaf
module that imports nothing at module level breaks the cycle instead of routing around
it, and matches the shape the package already uses for ``_image_gate``,
``_layout_geometry`` and ``_clearance_common``.

THE DEFERRED IMPORT STAYS, and stays HERE. It is the cycle break, not a leftover: this
module is imported by ``optimize_run`` / ``layout_render`` / ``analysis_graphic`` at
module level, and it reaches ``workspace`` only at call time.

**WHAT THIS DOES NOT FIX.** Each function here still performs ONE resolution per call,
so a caller that wants the root AND a path derived from it still asks twice and can
still get two answers if ``_resolve_root`` is not a stable read. That is the open
two-independent-resolutions class, and consolidating the four copies into one does not
close it — it makes it fixable in one place instead of four. The two tickets
(``promote-best-resolves-the-root-twice``, ``render-layout-resolves-the-root-twice``)
remain the record of the unclosed half.
"""
import os
import uuid

from .._io import safe_repr
from ..artifact_sink import _safe_name
from ..errors import OptimizeError


def workspace_root(session):
    """The resolved workspace root, for an envelope disclosure. ``None`` on failure.

    A disclosure must not sink the tool it describes, so **every ``Exception``**
    reads ``None`` — an honest "not established", not a claim that no root exists.

    NARROWED (claim audit): this said "NEVER raises" and "every failure reads
    ``None``". The net below is ``except Exception``, which does NOT catch
    ``BaseException`` — a ``KeyboardInterrupt``, a ``SystemExit`` or a
    ``MemoryError``-class abort propagates, and should. So the guarantee is over
    ``Exception``, not over every way this can fail to return, and the wider
    sentence was the kind a message test would have pinned.
    """
    try:
        from .workspace import _resolve_root
        return _resolve_root(session)[0]
    except Exception:  # noqa: BLE001 — a disclosure must never sink its caller
        return None


def scratch_dir_state(session):
    """``(scratch, fault)`` -- the SAME question as ``scratch_dir``, answered honestly.

    ROUND 7 (external). ``scratch_dir`` collapses two different answers into one
    ``None``: "the resolver CLEANLY reports no workspace root" and "the resolver
    FAILED and we do not know". Both consumers then read ``if not scratch:`` and fall
    back to a FIXED name in the current directory -- ``layout.png`` /
    ``capture_<type>.png`` -- which is the one outcome the minting path exists to
    prevent, because a fixed name silently overwrites the previous figure.

    The auditor measured it by injecting a resolver exception and driving both real
    resolvers: ``('layout.png', False, None)`` and ``('capture_mtf.png', None)``.
    Neither refuses; neither reports ``workspace_unlistable``. **Resolution failure
    became permission to use a fixed name.**

    HONESTLY ATTRIBUTED: the S-1 hoist PRESERVED this, it did not introduce it -- both
    consumers behaved this way before the four copies were folded into one. What the
    hoist did do is make it MY module's fail-open to fix, and the S-1 tests proved only
    that an exception becomes ``None``, never that a caller handles that unknown
    safely. A test can pin the wrong half of a contract and read green.

    ``fault`` is a message when the resolution RAISED, ``None`` otherwise. A caller
    that has a refusal channel must use it; ``scratch_dir`` keeps the lossy shape for
    anything that genuinely cannot refuse.
    """
    try:
        from .workspace import _resolve_root
        root = _resolve_root(session)[0]
    except Exception as exc:  # noqa: BLE001 — UNKNOWN is reported, never swallowed
        return None, (
            f"the workspace root could not be resolved, so a free scratch figure "
            f"name cannot be minted and a fixed name in the working directory could "
            f"silently overwrite an existing figure: "
            f"{type(exc).__name__}: {exc}. Pass an explicit path.")
    if not root:
        return None, None
    return os.path.join(root, "candidates", "scratch"), None


def scratch_dir(session):
    """``<root>/candidates/scratch``, or ``None`` when no root resolves.

    LOSSY BY CONSTRUCTION and kept that way for callers with no refusal channel: a
    clean "no root" and a FAILED resolution both read ``None`` here. A caller that can
    refuse should use ``scratch_dir_state`` -- see its docstring for what that
    collapse cost.
    """
    return scratch_dir_state(session)[0]


def trail_sink(session, run_id):
    """The per-run optimize trail sink, through the SAME cycle-safe door.

    Folded in with the two path helpers because it is the same question asked of the
    same authority — *where does this run's output live?* — and because it carried the
    THIRD deferred ``from .workspace import ...`` in ``optimize_run``. Routing it here
    leaves exactly one import edge from that module into ``workspace`` instead of three.

    RAISES on an unwritable root, deliberately and unlike its two neighbours: the caller
    (``optimize_run._resolve_sink``) already owns that except-and-warn, and swallowing it
    here would silently hand back ``None`` for a condition the caller reports.
    """
    from .workspace import _get_trail_sink
    return _get_trail_sink(session, run_id)


def trail_run_id(params):
    """The optimize trail's ``run_id``: the caller's, or a generated one. A bad one -> ``optimize_param``.

    ``run_id`` names the trail DIRECTORY (``<root>/candidates/trail/<run_id>``), and
    ``ArtifactSink`` joins it with ``os.path.join`` -- so an absolute path (drive, UNC,
    POSIX) DISCARDS the base and a ``..`` walks out of it. Measured before this door
    existed: ``run_id="../zmx"`` wrote the trail into ``candidates/zmx`` as ownerless rows
    that ``promote_best`` then resolved for ANY design name, and an absolute id wrote
    outside the workspace root.

    REFUSE, NEVER REWRITE -- the same fixed-point rule ``design_name`` follows. A rewritten
    id would make the echoed ``run_id`` disagree with the directory actually used. A
    ``_safe_name`` fixed point already excludes every escape shape: ``/``, backslash and ``:``
    are illegal characters (so absolute, UNC and drive forms change), ``.``/``..`` strip to
    empty and become ``snapshot``, and a reserved device name gains a prefix. Read EARLY,
    beside the other gate-before-anything params, so a bad id refuses with ZERO mutation.

    LIVES HERE, not in ``optimize_run``: that module is at its statement band with zero
    spare, and this is the same question this module answers -- where a run's output lives.
    """
    value = dict.get(params, "run_id") if isinstance(params, dict) else None
    if value is None or value == "":
        return f"optimize_{uuid.uuid4().hex[:12]}"
    if not isinstance(value, str) or type(value) is not str:
        raise OptimizeError(
            f"'run_id' must be a string, not {type(value).__name__}", family="optimize_param")
    safe = _safe_name(value)
    if safe != value:
        raise OptimizeError(
            f"'run_id' {safe_repr(value)} is not a plain directory name -- it names the trail "
            f"folder under candidates/trail/, so it may not contain a slash, backslash, colon or other illegal "
            f"characters, be . or .., end in a dot or space, or be a reserved device name. "
            f"Nothing was written. Use a plain name such as {safe_repr(safe)}, or omit "
            f"run_id to get a generated one.",
            family="optimize_param")
    return value
