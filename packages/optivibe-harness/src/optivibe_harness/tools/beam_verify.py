"""tools/beam_verify.py — verify_beam_path: the rays-reach-the-optics falsifier.

A NEW first-class READ-ONLY dispatchable tool:
the "did the beam actually make it through the fold" check = the user's "simulate
beam pass" deliverable. A thin envelope+figure wrapper over
``_beam_reach.evaluate_beam_path`` (the substrate that does the trace + reach
evaluation); ``_beam_reach.beam_reaches_span`` is the commit gate.

NEVER raises (the ``describe_surfaces`` envelope precedent). ``ok`` is True whenever
the evaluation RAN — even a clean RED verdict (``all_rays_survive:false``) is
``ok:true``; ``ok:false`` is reserved for being unable to evaluate at all (no system
/ total trace failure). The figure is BEST-EFFORT and NON-fatal: a figure failure
sets ``figure_error`` and leaves ``figure_path:None`` — the verdict is the
rays-reach result, never the plot.

Read-only: no mutation; the verdict is measured against the independent global frame,
never a clean call (L28).
"""
from ..server import ToolSpec
from . import _beam_reach
from . import layout_render as _layout_render


def _num_param(params, key, default):
    """Coerce an optional numeric param; reject bool / non-number / non-finite.

    Returns ``(value, error)``: a bad value yields ``(default, "<msg>")`` — the caller
    records the error in ``flags`` and falls back, NEVER raises (the never-raise
    contract). A genuine number passes through.
    """
    import math

    if key not in params or params[key] is None:
        return default, None
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default, (
            f"{key!r} must be a number, got {type(value).__name__} {value!r}; "
            f"using default {default!r}"
        )
    fv = float(value)
    if not math.isfinite(fv):
        return default, (
            f"{key!r} must be finite, got {value!r}; using default {default!r}"
        )
    return fv, None


def _emit_figure(session, params):
    """Best-effort fold-coherent layout figure (rays ON). Returns ``(path, error)``.

    Delegates to ``render_layout`` with ``draw_rays=True`` (the existing fold-coherent
    draw path — folded systems are drawn in the global frame, coherent with the rays).
    A figure failure is NON-fatal: returns ``(None, "<msg>")``; the verdict is
    unaffected. NEVER raises.
    """
    try:
        render_params = {"draw_rays": True}
        name = params.get("design_name")
        if isinstance(name, str) and name:
            # Mirror render_layout's path convention: a stem under the workspace.
            render_params["title"] = name
        result = _layout_render.render_layout(session, render_params)
        if isinstance(result, dict) and result.get("ok"):
            return result.get("path"), None
        err = (
            result.get("error")
            if isinstance(result, dict)
            else "render_layout returned a non-dict"
        )
        return None, f"figure not rendered: {err}"
    except BaseException as exc:  # noqa: BLE001 — a figure throw is NON-fatal
        return None, f"figure render failed: {type(exc).__name__}: {exc}"


def verify_beam_path(session, params):
    """Trace the chief + marginal rays through the whole system; report whether the beam reaches each optic.

    Returns the ``evaluate_beam_path`` dict (``all_rays_survive`` + ``first_failure``
    + ``per_surface`` + ``geometric_misses``) plus ``figure_path``/``figure_error``.
    ``ok:true`` even when ``all_rays_survive:false`` (a clean RED verdict); ``ok:false``
    only when the tool cannot evaluate at all. NEVER raises.
    """
    if not isinstance(params, dict):  # never-raise: None OR a truthy non-dict (L26)
        params = {}

    knob_flags = []
    wave, e = _num_param(params, "wave", 1.0)
    if e:
        knob_flags.append(e)
    margin, e = _num_param(params, "margin", 0.0)
    if e:
        knob_flags.append(e)
    floor, e = _num_param(params, "floor", 1.0)
    if e:
        knob_flags.append(e)

    vignette_is_failure = params.get("vignette_is_failure", False)
    if not isinstance(vignette_is_failure, bool):
        knob_flags.append(
            "'vignette_is_failure' must be a bool; using default False"
        )
        vignette_is_failure = False

    draw = params.get("draw", True)
    if not isinstance(draw, bool):
        knob_flags.append("'draw' must be a bool; using default True")
        draw = True

    try:
        wave_i = int(wave) if wave >= 1 else 1
        result = _beam_reach.evaluate_beam_path(
            session.system,
            wave=wave_i,
            margin=margin,
            floor=floor,
            vignette_is_failure=vignette_is_failure,
        )
    except BaseException as exc:  # noqa: BLE001 — belt-and-braces; substrate is wrapped
        return {
            "ok": False,
            "error_family": "beam_path_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "all_rays_survive": False,
            "figure_path": None,
            "figure_error": None,
        }

    if not isinstance(result, dict):
        result = {
            "ok": False,
            "error_family": "beam_path_unavailable",
            "error": "the substrate returned a non-dict",
            "all_rays_survive": False,
        }

    # Merge the knob fallback notes into the result's flags (additive, non-breaking).
    # Guarded: a pathological non-list ``flags`` from the substrate (or a list() that
    # somehow throws) must NEVER raise past the never-raise boundary (L26). On any
    # fault we fall back to just the knob flags so the notes are never silently lost.
    if knob_flags:
        try:
            existing = result.get("flags", [])
            flags = list(existing) if isinstance(existing, list) else []
            flags.extend(knob_flags)
            result["flags"] = flags
        except BaseException:  # noqa: BLE001 — never raise past the boundary
            result["flags"] = list(knob_flags)

    # Best-effort figure (only when the evaluation ran AND draw is requested). A figure
    # failure is NON-fatal — it never flips ok or the verdict.
    figure_path = None
    figure_error = None
    if draw and result.get("ok"):
        figure_path, figure_error = _emit_figure(session, params)
    result["figure_path"] = figure_path
    result["figure_error"] = figure_error
    return result


VERIFY_BEAM_PATH_SPEC = ToolSpec(
    name="verify_beam_path",
    handler=verify_beam_path,
    required_params=(),
    param_types={
        "wave": "number",
        "margin": "number",
        "floor": "number",
        "vignette_is_failure": "boolean",
        "draw": "boolean",
        "design_name": "string",
    },
    description=(
        "Trace the chief + marginal rays through the whole system in the global frame "
        "and report whether every ray reaches each optic (the rays-reach / 'did the "
        "beam actually make it through the fold' check). Params: wave, margin, floor, "
        "vignette_is_failure, draw. Returns all_rays_survive + the first surface a ray "
        "misses/fails + a per-surface table + a layout figure. Pure-read. Gotcha: a "
        "geometric miss is an early warning; the errorCode failure is decisive. See "
        "render_layout, describe_surfaces."
    ),
)

TOOL_SPECS = (VERIFY_BEAM_PATH_SPEC,)
