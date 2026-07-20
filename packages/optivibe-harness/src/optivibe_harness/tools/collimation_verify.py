"""tools/collimation_verify.py — verify_collimation: the collimation residual grader.

A NEW first-class READ-ONLY dispatchable tool (afocal/collimation analysis, GAP-3):
grade a forward collimator / afocal output (image at infinity) by tracing a per-field
pupil grid of OUTPUT rays and reporting the RMS angular residual (mrad — how parallel
the output beam is), the chief-ray pointing direction, and the worst marginal slope,
with a verdict. THE tool for a collimated output where ``analyze_strehl`` /
``analyze_wavefront`` read meaningless image-plane numbers (a 973-wave RMS).

A thin envelope (§1.3/§1.5 never-raise) over ``_collimation.compute_collimation_residual``
(the residual math substrate, SHARED with the analyzer guard's detector). Routes the
per-field grade through ``_config_common.evaluate_over_configs`` (the cheap-grader class,
§1.4): ``config="all"`` sweeps every MCE config with the coverage reconcile.

NEVER raises: a bad ``pupil_density`` / ``tolerance_mrad`` is a HARD ``collimation_param``;
``trace_rays`` ``ok:false`` propagates as ``collimation_empty``; a total throw nets to
``collimation_unavailable`` (the outer net). ``ok:true`` whenever the evaluation RAN —
even a clean RED verdict (``not_collimated`` / ``collimation_indeterminate``) is
``ok:true`` (the ``verify_beam_path`` precedent: ``ok`` means the evaluation RAN).

Read-only: NO mutation; the verdict is measured against the traced rays, never a clean
call. A geometry+config fingerprint is byte-identical before/after.
"""
import math

from ..errors import ToolParamError
from ..server import ToolSpec
from . import _collimation as _col
from . import _config_common as _cfg


# New error families scoped to this tool (mirroring clearance_param / _unavailable;
# attached via the envelope, NO new error class).
_FAMILY_PARAM = "collimation_param"
_FAMILY_EMPTY = "collimation_empty"
_FAMILY_UNAVAILABLE = "collimation_unavailable"


def _error_envelope(family, message, **extra):
    out = {"ok": False, "tool": "verify_collimation", "error_family": family,
           "error": message}
    out.update(extra)
    return out


def _num_param(params, key, default):
    """Coerce an optional numeric param; reject bool / non-number / non-finite.

    Returns ``(value, error)``: a bad value yields ``(default, "<msg>")`` — the caller
    records the error in ``flags`` and falls back (the ``beam_verify._num_param``
    pattern). Used ONLY for the fall-back-able ``wave``/``to_surface`` knobs; the HARD
    ``pupil_density``/``tolerance_mrad`` gates are validated separately (§1.5).
    """
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


def _require_pupil_density(params):
    """Validate ``pupil_density`` — an int (or integral float) ``>= 2`` (§1.5, HARD).

    A 1×1 grid is a single chief ray (no residual); a bool / non-int / non-integral /
    ``< 2`` -> ``ToolParamError`` (-> ``collimation_param``, never a silent fall-back to
    a degenerate grid). Returns the int density.
    """
    value = params.get("pupil_density", 5)
    if isinstance(value, bool):
        raise ToolParamError(
            f"pupil_density must be an integer >= 2, not a bool ({value!r})"
        )
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            value = int(value)
        else:
            raise ToolParamError(
                f"pupil_density must be an integer >= 2, got non-integral {value!r}"
            )
    if not isinstance(value, int) or value < 2:
        raise ToolParamError(
            f"pupil_density must be an integer >= 2 (a 1x1 grid is a single chief "
            f"ray with no residual), got {value!r}"
        )
    return value


def _require_tolerance(params):
    """Validate ``tolerance_mrad`` — FINITE and ``>= 0`` (§1.5, HARD).

    A nan/inf/neg tolerance is an always-pass (never a silent always-pass); a bool /
    non-number -> ``ToolParamError`` (-> ``collimation_param``). Returns the float.
    """
    value = params.get("tolerance_mrad", 0.5)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"tolerance_mrad must be a finite number >= 0, got "
            f"{type(value).__name__} {value!r}"
        )
    fv = float(value)
    if not math.isfinite(fv) or fv < 0.0:
        raise ToolParamError(
            f"tolerance_mrad must be a finite number >= 0 (nan/inf/neg is an "
            f"always-pass), got {value!r}"
        )
    return fv


def verify_collimation(session, params):
    """Grade a forward collimator / afocal output (image at infinity). NEVER raises.

    Traces a per-field N×N pupil grid of OUTPUT rays and reports the per-field RMS
    angular residual (mrad — how parallel the output beam is), the chief-ray pointing
    direction, and the worst marginal slope, with a ``collimated`` /
    ``not_collimated`` / ``collimation_indeterminate`` verdict. ``ok:true`` whenever
    the evaluation RAN (a clean RED verdict is still ``ok:true``).

    Pure-read: NO mutation. The residual references the per-field pupil MEAN (NOT the
    chief — ``chief_pointing_mrad`` is the SEPARATE bore-sight number). A RANG⟂cosine
    cross-check downgrades to ``collimation_indeterminate`` on a marshalling
    divergence (the L24 defense). ``config`` (None|int|"all") sweeps MCE configs.
    """
    if not isinstance(params, dict):  # never-raise: None OR a truthy non-dict (L26)
        params = {}

    try:
        # HARD param gates (a degenerate grid / an always-pass tolerance is a real bug).
        pupil_density = _require_pupil_density(params)
        tolerance_mrad = _require_tolerance(params)
    except ToolParamError as exc:
        return _error_envelope(_FAMILY_PARAM, str(exc))

    knob_flags = []
    wave, e = _num_param(params, "wave", 1.0)
    if e:
        knob_flags.append(e)
    to_surface_raw, e = _num_param(params, "to_surface", -1.0)
    if e:
        knob_flags.append(e)
    wave_i = int(wave) if wave >= 1 else 1
    to_surface = int(to_surface_raw)

    config = params.get("config")

    def _grade(sess):
        result = _col.compute_collimation_residual(
            sess.system,
            wave=wave_i,
            pupil_density=pupil_density,
            to_surface=to_surface,
            tolerance_mrad=tolerance_mrad,
            vignette_is_failure=True,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            # Propagate the trace failure structured (collimation_empty), never a throw.
            return result if isinstance(result, dict) else _error_envelope(
                _FAMILY_EMPTY, "the residual substrate returned a non-dict"
            )
        # Detector context (the AFocalImageSpace read, echoed — corroborating only).
        detector = _col.detect_collimated_output(
            sess.system, wave=wave_i, to_surface=to_surface
        )
        afocal = detector.get("afocal_image_space")

        result_flags = list(result.get("flags", []))
        result_flags.extend(knob_flags)
        return {
            "ok": True,
            "tool": "verify_collimation",
            "verdict": result["verdict"],
            "tolerance_mrad": tolerance_mrad,
            "wave": wave_i,
            "to_surface": to_surface,
            "pupil_density": pupil_density,
            "per_field": result["per_field"],
            "worst_field_residual_mrad": result["worst_field_residual_mrad"],
            "config_headline": result["config_headline"],
            "afocal_image_space": afocal,
            "cross_check_ok": result["cross_check_ok"],
            # The residual is the UNIT-DISK RMS (grid clipped to Px²+Py² <= 1).
            "pupil_domain": result.get("pupil_domain", "unit_disk"),
            "flags": result_flags,
        }

    try:
        return _cfg.evaluate_over_configs(session, config, _grade)
    except ToolParamError as exc:
        # A bad config selector from evaluate_over_configs -> collimation_param.
        return _error_envelope(_FAMILY_PARAM, str(exc))
    except Exception as exc:  # noqa: BLE001 — the outer never-raise net
        return _error_envelope(
            _FAMILY_UNAVAILABLE,
            f"verify_collimation hit an unexpected error: {type(exc).__name__}: {exc}",
        )


VERIFY_COLLIMATION_SPEC = ToolSpec(
    name="verify_collimation",
    handler=verify_collimation,
    required_params=(),
    param_types={
        "wave": "number",
        "pupil_density": "number",
        "to_surface": "number",
        "tolerance_mrad": "number",
        "config": "number",
    },
    description=(
        "Grade a forward collimator / afocal output (image at infinity): trace a "
        "pupil grid of OUTPUT rays per field and report the RMS angular residual "
        "(mrad - how parallel the output beam is), the chief-ray pointing direction, "
        "and the worst marginal slope, with a verdict. Use THIS for a "
        "collimated/afocal output, NOT analyze_strehl/analyze_wavefront (which assume "
        "a real image plane and read meaningless numbers - e.g. a 973-wave RMS - on a "
        "collimator). The real-ray slope check. Params: wave, pupil_density (NxN grid, "
        "default 5, >=2), to_surface (default -1=image), tolerance_mrad (RMS pass "
        "threshold, default 0.5), config. Pure-read. Gotcha: the residual references "
        "the per-field pupil MEAN, not the chief - chief_pointing is the SEPARATE "
        "bore-sight number; the residual is the UNIT-DISK RMS (the pupil grid is "
        "clipped to Px^2+Py^2<=1, not the full square). See trace_rays, verify_beam_path."
    ),
)

TOOL_SPECS = (VERIFY_COLLIMATION_SPEC,)
