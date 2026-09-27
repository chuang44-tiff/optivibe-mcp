"""tools/analysis_graphic.py — capture_graphic: render an analysis plot to a file.

``capture_graphic`` opens an analysis window, runs it, and renders the plot to an
image file via ``analysis.ToFile(path, show_settings)``, then applies a durability
gate (§5: PNG-magic bytes via the shared ``_is_png`` oracle + RETURN
``{ok:false}`` — NEVER raise).

``ToFile`` returns void and may silently no-op OR write a TEXT dump
named ``.png`` that a byte-count gate passed silently. The on-disk
gate is now ``_is_png(path)`` (the first 8 bytes are the PNG signature), the ONLY
source of truth. When it fails the post-ToFile branch returns the more honest
``headless_no_image`` family (pointing the caller to ``render_layout``). Capture is NON-load-bearing (the typed result path already carries the
data); a failed render must not abort the agent's reasoning — so this tool ALWAYS
returns a dict and never raises (mirrors ``save_snapshot``'s ``ok:false`` posture).

``analysis_type`` is restricted to a small allowlist (``"mtf"``/``"spot"``/
``"rayfan"``, case-insensitive) mapped to the live analysis factory; the output
path is sanitized via ``artifact_sink._safe_name`` (Windows-safe, blocks the ADS
0-byte trap).

Live ZOS-API integration: exercised by the live test (FftMtf PNG ~60903 B,
StandardSpot ~671 B, RayFan ~227 B — all > 128); unit-tested against a fake
analysis whose ToFile writes a stub of a controlled size.
"""
import os
import re

from .. import artifact_naming as _naming
from ..artifact_sink import _safe_name
from ..server import ToolSpec
from . import analysis_mtf as _amtf
from ._image_gate import _is_png
from . import _workspace_paths as _wsp  # S-1 — cycle-safe (never imports analysis_graphic)

# Durability-gate threshold (legacy; retained for the envelope's ``min_bytes``
# echo only). The REAL gate is now ``_is_png`` (PNG-magic bytes) per §5:
# headless ``ToFile`` writes a TEXT dump that a byte-count
# gate silently passed; the magic-byte gate rejects it. Kept as a field for
# backward-compatible callers reading ``min_bytes``.
_MIN_BYTES = 128

# The analysis-type allowlist (locked): a friendly name -> the live analysis
# factory call. ``mtf`` -> New_FftMtf; ``spot`` -> New_StandardSpot; ``rayfan`` ->
# New_RayFan. Restricting the surface keeps capture to the probed, durable set.
_ALLOWLIST = ("mtf", "spot", "rayfan")


def _open_analysis(system, analysis_type):
    """Open the allowlisted analysis window for ``analysis_type`` (case-insensitive).

    Returns the analysis window, or ``None`` if ``analysis_type`` is not on the
    allowlist (the caller turns that into an ``ok:false`` envelope — never raises).
    """
    key = analysis_type.strip().lower() if isinstance(analysis_type, str) else None
    if key == "mtf":
        return system.Analyses.New_FftMtf()
    if key == "spot":
        return system.Analyses.New_Analysis(_amtf._analysis_idm(system, "StandardSpot"))
    if key == "rayfan":
        return system.Analyses.New_RayFan()
    return None


def _resolve_path(session, analysis_type, path):
    """Resolve the output path. Returns ``(path, error)``; ``path`` is None on error.

    A supplied ``path`` keeps its directory + extension but its STEM is run through
    ``_safe_name`` (Windows-safe; blocks the ADS ``:`` trap) and keeps today's
    overwrite semantics — the caller asked for that file. A null ``path`` mints a
    numbered SCRATCH name under ``<root>/candidates/scratch``.
    """
    if path:
        directory = os.path.dirname(path)
        base = os.path.basename(path)
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = ".png"
        safe_stem = _safe_name(stem)
        return (os.path.join(directory, f"{safe_stem}{ext}")
                if directory else f"{safe_stem}{ext}"), None
    # MINT a scratch name under ``<root>/candidates/scratch`` by the SAME rule
    # render_layout mints by: 1 + the max index the directory LISTING shows. If the
    # listing fails the tool REFUSES before ``ToFile`` and writes nothing — a
    # refusal cannot clobber an existing figure, and falling back to a fixed name is
    # exactly how the tenth capture overwrites the first.
    safe_type = _safe_name(str(analysis_type))
    # ONE interpolation of ``safe_type``, not two. The rewrite of this function split
    # the original single ``f"capture_{safe_type}.png"`` into a no-scratch fallback and
    # a numbered mint, which took the base-slot backlog from 124 to 125 -- a
    # RATCHET, and the answer to a ratchet is to remove the site, never to raise the
    # ceiling. Both names are now built from one stem.
    stem = f"capture_{safe_type}"
    #, the sibling site. Same rule, same reason: an unknown root refuses rather
    # than minting ``capture_<type>.png`` into the working directory, where the next
    # capture of the same analysis type would overwrite it.
    scratch, scratch_fault = _wsp.scratch_dir_state(session)
    if scratch_fault is not None:
        return None, scratch_fault
    if not scratch:
        return stem + ".png", None
    # CREATE THE DIRECTORY WE ARE ABOUT TO MINT A NAME IN. ``render_layout`` makedirs
    # its dirname; this tool had no ``makedirs`` anywhere in the file, so the FIRST
    # capture in a fresh workspace minted ``candidates/scratch/capture_<type>_0001.png``
    # into a directory that does not exist. The engine's ``ToFile`` is a SILENT NO-OP on
    # a nonexistent directory (the probe-A1 semantics this module's own docstring
    # cites), so nothing was written and the failure was reported as
    # ``headless_no_image`` -- blaming a headless session for a missing directory.
    #
    # BEFORE the listing, deliberately: ``next_index_in_dir`` answers 1 for an ABSENT
    # directory and ``None`` (-> refuse) for one that exists but cannot be LISTED, and
    # those must stay distinguishable. Creating it first means the listing below is a
    # real listing, so the G7b refusal keeps its meaning.
    #
    # GUARDED and NON-FATAL: if the directory cannot be made, the mint still returns a
    # name and the write fails downstream through the existing gate -- this is a
    # defect-closing convenience, not a new refusal path. ``ValueError`` is caught
    # beside ``OSError`` for the embedded-NUL case (the save_merit precedent).
    try:
        os.makedirs(scratch, exist_ok=True)
    except (OSError, ValueError):
        pass
    index = _naming.next_index_in_dir(
        scratch, lambda name: _capture_index(name, safe_type))
    if index is None:
        return None, (
            f"the scratch figure directory exists but could not be listed, so a "
            f"free name cannot be minted and an existing figure could be "
            f"overwritten: {scratch}. Pass an explicit path, or fix the "
            f"directory's permissions.")
    return os.path.join(scratch, f"{stem}_{index:04d}.png"), None


def _capture_index(name, safe_type):
    """The index a minted capture name carries for THIS analysis type, else None."""
    match = re.match(r"^capture_" + re.escape(safe_type) + r"_(\d+)\.png$", name)
    return int(match.group(1)) if match else None


def capture_graphic(session, params):
    """Render an allowlisted analysis to an image; durability-gated. NEVER raises.

    Returns ``{ok:true, path, size_bytes, min_bytes}`` when the gate passes, or
    ``{ok:false, error_family:"graphic_gate_failed", error, path, size_bytes,
    min_bytes}`` when ``ToFile`` produced no durable artifact (locked Verdict 2).
    A bad ``analysis_type`` or any internal failure also returns an ``ok:false``
    dict (this tool never raises into dispatch).
    """
    if not isinstance(params, dict):  # never-raise: None OR a truthy non-dict (§0.8, L26)
        params = {}
    analysis_type = params.get("analysis_type")
    show_settings = bool(params.get("show_settings", False))

    if not isinstance(analysis_type, str) or analysis_type.strip().lower() not in _ALLOWLIST:
        return {
            "ok": False,
            "error_family": "graphic_gate_failed",
            "error": f"analysis_type must be one of {_ALLOWLIST}, got {analysis_type!r}",
            "path": None,
            "size_bytes": 0,
            "min_bytes": _MIN_BYTES,
            "residue_removed": False,
        }

    try:
        path, mint_err = _resolve_path(session, analysis_type, params.get("path"))
        if mint_err is not None:
            # REFUSED BEFORE ``ToFile``. Nothing is written, so no existing figure
            # can be destroyed by a name this tool could not prove was free.
            return {
                "ok": False,
                "error_family": "workspace_unlistable",
                "error": mint_err,
                "path": None,
                "size_bytes": 0,
                "min_bytes": _MIN_BYTES,
                "residue_removed": False,
            }
        # This tool newly permits itself to DELETE a file, so it may delete only
        # one it created in THIS call. Read pre-existence before ``ToFile`` — nothing
        # pre-creates the file, so this is a plain, coherent test on both the minted
        # and the user-supplied branch.
        try:
            existed_before = os.path.isfile(path)
        except (OSError, ValueError):
            existed_before = True   # unknown -> never delete
        analysis = _open_analysis(session.system, analysis_type)
        if analysis is None:
            return {
                "ok": False,
                "error_family": "graphic_gate_failed",
                "error": f"could not open analysis {analysis_type!r}",
                "path": path,
                "size_bytes": 0,
                "min_bytes": _MIN_BYTES,
                "residue_removed": False,
            }
        try:
            analysis.ApplyAndWaitForCompletion()
            analysis.ToFile(path, show_settings)
        finally:
            try:
                analysis.Close()
            except Exception:  # noqa: BLE001 — window teardown must never raise
                pass

        # Durability gate (§5): PNG-magic bytes via the shared oracle,
        # NOT a byte count. ToFile may silently no-op (void) OR write a TEXT dump
        # named ``.png`` that the old ``>=128`` gate passed
        # silently. ``_is_png`` is the only truth source.
        is_file = os.path.isfile(path)
        size = os.path.getsize(path) if is_file else 0
        if _is_png(path):
            return {
                "ok": True,
                "path": path,
                "size_bytes": size,
                "min_bytes": _MIN_BYTES,
                "residue_removed": False,
            }
        # The gate FAILED, so whatever is at ``path`` is not an image — headless
        # ``ToFile`` writes a text dump named ``.png``. Remove it IFF this call
        # created it; a pre-existing file at a user-supplied path is not ours.
        residue_removed = False
        if is_file and not existed_before:
            try:
                os.remove(path)
                residue_removed = True
            except OSError:
                residue_removed = False
        # The post-ToFile durability branch now reports the more honest
        # ``headless_no_image`` family: headless ToFile wrote a text dump (or
        # nothing), not a real image — point the caller to render_layout for
        # layouts. (The bad-analysis_type / could-not-open branches keep
        # ``graphic_gate_failed``.)
        return {
            "ok": False,
            "error_family": "headless_no_image",
            "error": (
                "ToFile produced no real image (failed the PNG-magic gate — "
                "headless ToFile writes a text dump, not a PNG). For a layout "
                "figure, use render_layout. For HEADLESS analysis data: use "
                "get_mtf (frequency/modulation grid + at_frequencies summary), "
                "get_spot (per-field scatter), or analyze_wavefront/analyze_strehl."
            ),
            "path": path,
            "size_bytes": size,
            "min_bytes": _MIN_BYTES,
            "residue_removed": residue_removed,
        }
    except Exception as exc:  # noqa: BLE001 — capture is non-load-bearing; never raise
        return {
            "ok": False,
            "error_family": "graphic_gate_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "path": params.get("path"),
            "size_bytes": 0,
            "min_bytes": _MIN_BYTES,
            "residue_removed": False,
        }


CAPTURE_GRAPHIC_SPEC = ToolSpec(
    name="capture_graphic",
    handler=capture_graphic,
    required_params=("analysis_type",),
    param_types={
        "analysis_type": "string",
        "path": "string",
        "show_settings": "boolean",
    },
    description=(
        "Capture an analysis plot (mtf/spot/rayfan) to an image file. With no path "
        "the file is minted under candidates/scratch/ as a numbered scratch name; if "
        "that directory cannot be listed the call REFUSES (workspace_unlistable) "
        "before writing anything. When the image gate fails, a file this call itself "
        "created is deleted (residue_removed:true) — a file that already existed at a "
        "path you supplied is never touched. Requires an "
        "interactive GUI session — in headless mode, ToFile writes text not a PNG "
        "(returns headless_no_image). For HEADLESS analysis data: use get_mtf "
        "(frequency/modulation grid + at_frequencies summary), get_spot (per-field "
        "scatter), or analyze_wavefront/analyze_strehl. For a layout figure use "
        "render_layout."
    ),
)

TOOL_SPECS = (CAPTURE_GRAPHIC_SPEC,)
