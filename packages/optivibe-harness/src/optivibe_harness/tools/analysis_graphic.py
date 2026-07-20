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

from ..artifact_sink import _safe_name
from ..server import ToolSpec
from . import analysis_mtf as _amtf
from ._image_gate import _is_png

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
    """Resolve the output path: caller-supplied (sanitized stem) or minted.

    A supplied ``path`` keeps its directory + extension but its STEM is run through
    ``_safe_name`` (Windows-safe; blocks the ADS ``:`` trap). A null ``path`` mints
    ``<artifact dir>/<seq?>_capture_<type>.png`` under the run's artifact dir when a
    sink is wired, else the current dir.
    """
    if path:
        directory = os.path.dirname(path)
        base = os.path.basename(path)
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = ".png"
        safe_stem = _safe_name(stem)
        return os.path.join(directory, f"{safe_stem}{ext}") if directory else f"{safe_stem}{ext}"
    # Mint a default name under the artifact sink's run dir if available.
    safe_type = _safe_name(str(analysis_type))
    sink = getattr(session, "artifact_sink", None)
    run_dir = getattr(sink, "run_dir", None) if sink is not None else None
    filename = f"capture_{safe_type}.png"
    if run_dir:
        return os.path.join(run_dir, filename)
    return filename


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
        }

    try:
        path = _resolve_path(session, analysis_type, params.get("path"))
        analysis = _open_analysis(session.system, analysis_type)
        if analysis is None:
            return {
                "ok": False,
                "error_family": "graphic_gate_failed",
                "error": f"could not open analysis {analysis_type!r}",
                "path": path,
                "size_bytes": 0,
                "min_bytes": _MIN_BYTES,
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
            }
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
        }
    except Exception as exc:  # noqa: BLE001 — capture is non-load-bearing; never raise
        return {
            "ok": False,
            "error_family": "graphic_gate_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "path": params.get("path"),
            "size_bytes": 0,
            "min_bytes": _MIN_BYTES,
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
        "Capture an analysis plot (mtf/spot/rayfan) to an image file. Requires an "
        "interactive GUI session — in headless mode, ToFile writes text not a PNG "
        "(returns headless_no_image). For HEADLESS analysis data: use get_mtf "
        "(frequency/modulation grid + at_frequencies summary), get_spot (per-field "
        "scatter), or analyze_wavefront/analyze_strehl. For a layout figure use "
        "render_layout."
    ),
)

TOOL_SPECS = (CAPTURE_GRAPHIC_SPEC,)
