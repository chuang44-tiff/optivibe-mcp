"""tools/analysis_raytrace.py — trace_rays: batch ray trace (full 15-field return).

``trace_rays`` drives the single-instance batch ray-trace tool
(``sys.Tools.OpenBatchRayTrace`` -> ``CreateNormUnpol`` -> ``AddRay`` loop ->
``RunAndWaitForCompletion`` -> ``StartReadingResults`` -> ``ReadNextResult`` loop)
and returns the COMPLETE 15-field per-ray map (locked Verdict 3).

Batch discipline (§e):
- Open the batch tool EXACTLY ONCE. A 2nd ``OpenBatchRayTrace`` returns ``None``
  (a single-instance tool) -> the structured ``batch_unavailable`` envelope, never
  an ``AttributeError`` crash.
- ``CreateNormUnpol`` is a method ON THE TOOL (not a sub-object); ``to_surface=-1``
  is the image surface.
- ``opd_mode`` resolves via ``getattr(OPDMode, "None")`` — NEVER ``None_`` (the
  enum's first member is literally named ``None``, a Python keyword clash).
- ``ReadNextResult()`` returns a 15-tuple ``(success, rayNumber, errorCode,
  vignetteCode, X, Y, Z, L, M, N, l2, m2, n2, opd, intensity)``; ``tup[0]``
  (success) is the LOOP TERMINATOR (False ends reading) and is NOT emitted. Each
  read is arity-guarded for the EXACT locked arity (``len(tup) == 15``); a drift
  in either direction — a shorter tuple (under-arity, would IndexError) OR a
  longer tuple (over-arity, would silently drop fields) — surfaces the structured
  ``analysis_malformed`` envelope, never a raw IndexError, never a silent truncation.
- ``errorCode`` / ``vignetteCode`` are DATA — a vignetted/errored ray is SURFACED
  with its flags, never dropped, never raised.
- ``rt.Close()`` in a ``finally`` (the L22 analog for the batch tool) even on a
  mid-loop raise.

A ``short_read`` (returned < requested) is surfaced (``short_read:true``, still
``ok:true``); ``returned == 0`` while ``requested > 0`` is the ``analysis_empty``
canary.

Live ZOS-API integration: exercised by the live test (axis ray X=Y=0, N=1;
marginal Y~-0.0020878); unit-tested against a fixture-seeded fake batch tool.
"""
from .._io import safe_float
from ..enums import _resolve_enum
from ..errors import ToolParamError
from ..server import ToolSpec
from . import _analysis_common as _ac

# The 15-tuple field names in declaration order. Index 0
# (``success``) is the loop terminator and is NOT emitted as a per-ray field.
_RAY_FIELDS = (
    "success", "rayNumber", "errorCode", "vignetteCode",
    "X", "Y", "Z", "L", "M", "N", "l2", "m2", "n2", "opd", "intensity",
)


def _rays_type_enum(system):
    """Resolve the live ``RaysType`` enum off ``ZOSAPI.Tools.RayTrace``."""
    injected = getattr(system, "_raytrace_enums", None)
    if injected is not None and "RaysType" in injected:
        return injected["RaysType"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Tools.RayTrace as _rt  # type: ignore

        return _rt.RaysType
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve RaysType from ZOSAPI.Tools.RayTrace: {exc}"
        )


def _opd_mode_member(system, name):
    """Resolve an ``OPDMode`` member via ``getattr(OPDMode, name)`` (NEVER ``None_``).

    The enum's first member is literally named ``None`` (a Python keyword clash):
    ``getattr(OPDMode, "None")`` is the ONLY correct resolution.
    A fake system injects a ``_raytrace_enums["OPDMode"]`` seam for unit tests.
    """
    injected = getattr(system, "_raytrace_enums", None)
    opd_enum = None
    if injected is not None and "OPDMode" in injected:
        opd_enum = injected["OPDMode"]
    else:
        try:  # pragma: no cover - live backend path
            import ZOSAPI.Tools.RayTrace as _rt  # type: ignore

            opd_enum = _rt.OPDMode
        except Exception as exc:  # noqa: BLE001 — surface as a param error
            raise ToolParamError(
                f"could not resolve OPDMode from ZOSAPI.Tools.RayTrace: {exc}"
            )
    try:
        return getattr(opd_enum, name)
    except AttributeError:
        raise ToolParamError(
            f"unknown OPDMode member {name!r} (use 'None'/'Current'/'CurrentAndChief')"
        )


def _coerce_rays(rays):
    """Validate ``rays`` is a non-empty list of ``{wave,Hx,Hy,Px,Py}`` dicts.

    list-of-dicts (locked Verdict 4): named keys are self-documenting and cannot
    silently transpose Px/Py the way a positional list invites. Each dict must
    carry an integer ``wave`` (1-based) and numeric Hx/Hy/Px/Py. Returns a list of
    ``(wave, Hx, Hy, Px, Py)`` tuples.
    """
    if not isinstance(rays, (list, tuple)) or not rays:
        raise ToolParamError(
            "rays must be a non-empty list of {wave, Hx, Hy, Px, Py} dicts"
        )
    out = []
    for i, ray in enumerate(rays):
        if not isinstance(ray, dict):
            raise ToolParamError(
                f"rays[{i}] must be a dict with wave/Hx/Hy/Px/Py, got {ray!r}"
            )
        wave = ray.get("wave")
        if isinstance(wave, bool) or not isinstance(wave, int):
            if isinstance(wave, float) and wave == int(wave):
                wave = int(wave)
            else:
                raise ToolParamError(
                    f"rays[{i}].wave must be a 1-based integer, got {wave!r}"
                )
        coords = []
        for key in ("Hx", "Hy", "Px", "Py"):
            value = ray.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolParamError(
                    f"rays[{i}].{key} must be a number, got {value!r}"
                )
            coords.append(float(value))
        out.append((int(wave), coords[0], coords[1], coords[2], coords[3]))
    return out


def trace_rays(session, params):
    """Trace a batch of rays and return the full 15-field per-ray map (§a).

    Opens the batch tool ONCE (a None 2nd-open returns the ``batch_unavailable``
    envelope), creates the normalized-unpolarized data container, adds each ray,
    runs, then reads results until the success terminator. Every numeric field
    goes through ``safe_float``; errorCode/vignetteCode/rayNumber are int-cast.
    ``rt.Close()`` runs in a ``finally``. A short read is surfaced (``ok:true``);
    zero returned for a non-empty request is ``analysis_empty``.
    """
    system = session.system

    rays = _coerce_rays(params.get("rays"))
    requested = len(rays)

    rays_type_name = params.get("rays_type", "Real")
    rays_type = _resolve_enum(_rays_type_enum(system), rays_type_name)
    opd_mode_name = params.get("opd_mode", "None")
    opd_mode = _opd_mode_member(system, opd_mode_name)

    to_surface = params.get("to_surface", -1)
    if isinstance(to_surface, bool) or not isinstance(to_surface, int):
        if isinstance(to_surface, float) and to_surface == int(to_surface):
            to_surface = int(to_surface)
        else:
            raise ToolParamError(
                f"to_surface must be an integer, got {to_surface!r}"
            )

    rt = system.Tools.OpenBatchRayTrace()
    # GOTCHA (§e): a 2nd open (or an open while one is live)
    # returns None — never AttributeError your way into a crash.
    if rt is None:
        return _ac.error_envelope(
            "trace_rays", "batch_unavailable",
            "OpenBatchRayTrace returned None (the batch tool was already open)",
        )

    try:
        nun = rt.CreateNormUnpol(requested, rays_type, to_surface)
        nun.ClearData()
        for wave, hx, hy, px, py in rays:
            nun.AddRay(wave, hx, hy, px, py, opd_mode)
        rt.RunAndWaitForCompletion()
        nun.StartReadingResults()

        out_rays = []
        malformed = None
        while True:
            tup = nun.ReadNextResult()
            # Arity guard (BUG4 / NEW-2): the locked read is EXACTLY a 15-tuple
            # A drift in EITHER direction is an API/version change
            # the canary must surface: a shorter tuple would raise a raw IndexError
            # on tup[14] (intensity); a LONGER tuple (over-arity) would be read as
            # valid with its extra field silently dropped by the range() below
            # (greening a mismatched shape). Guard with `!=` so both drift
            # directions surface one structured `analysis_malformed` envelope —
            # never a crash, never a silent truncation to ok:true.
            if len(tup) != len(_RAY_FIELDS):
                malformed = (
                    f"ReadNextResult returned a {len(tup)}-tuple; expected the "
                    f"locked {len(_RAY_FIELDS)}-tuple (arity drift)"
                )
                break
            # tup[0] (success) is the LOOP TERMINATOR (False ends reading) — it is
            # NOT emitted as a per-ray field (§e).
            if not tup[0]:
                break
            ray = {}
            for idx in range(1, len(_RAY_FIELDS)):
                ray[_RAY_FIELDS[idx]] = safe_float(tup[idx])
            # errorCode/vignetteCode/rayNumber are integer DATA — cast after
            # safe_float (a finite float -> int; a sentinel string stays as-is).
            for ikey in ("rayNumber", "errorCode", "vignetteCode"):
                val = ray.get(ikey)
                if isinstance(val, (int, float)):
                    ray[ikey] = int(val)
            # Convenience booleans derived from the authoritative codes (locked
            # Verdict 3): errored == (errorCode != 0), vignetted == (vig != 0).
            ray["errored"] = ray.get("errorCode", 0) != 0
            ray["vignetted"] = ray.get("vignetteCode", 0) != 0
            out_rays.append(ray)
    finally:
        # rt.Close() in a finally (the L22 analog) even on a mid-loop raise.
        try:
            rt.Close()
        except Exception:  # noqa: BLE001 — batch-tool teardown must never raise
            pass

    # An arity-drift read surfaces a structured malformed error AFTER the batch
    # tool is reaped (the Close() above already ran in the finally) — never an
    # IndexError (BUG4).
    if malformed is not None:
        return _ac.error_envelope(
            "trace_rays", "analysis_malformed", malformed,
            requested=requested, returned=len(out_rays),
        )

    returned = len(out_rays)
    # Canary (§d): the trace ran but yielded nothing for a non-empty request.
    if returned == 0 and requested > 0:
        return _ac.error_envelope(
            "trace_rays", "analysis_empty",
            f"batch ray trace returned 0 rays for {requested} requested",
            requested=requested, returned=returned,
        )

    return {
        "ok": True,
        "linear_units": _ac._lens_units_string(system),
        # The OPD field's UNITS + MEANING
        # flip with the resolved opd_mode — surface them from the DATA, not memory.
        # "None" -> optical path LENGTH in the lens unit (~210, NOT a wavefront);
        # "Current" -> the ray's RAW ABSOLUTE OPL in waves (state-dependent, NOT
        # chief-referenced, NOT a wavefront error); "CurrentAndChief" -> the
        # chief-referenced WAVEFRONT ERROR in waves (the sub-wave WFE).
        "opd_mode": opd_mode_name,
        "opd_units": _opd_units_for(opd_mode_name, system),
        "requested": requested,
        "returned": returned,
        "short_read": returned < requested,
        "rays": out_rays,
    }


def _opd_units_for(opd_mode_name, system):
    """Resolve the units + MEANING of the per-ray ``opd`` field for ``opd_mode_name``.

    Re-probe truth:
      - ``"None"`` -> optical path LENGTH in the system linear unit (e.g. mm) —
        NOT a wavefront error.
      - ``"Current"`` -> the ray's RAW ABSOLUTE OPL in waves (= OPL/lambda):
        state-dependent/unreliable, NOT chief-referenced, NOT a wavefront error.
      - ``"CurrentAndChief"`` -> the chief-referenced WAVEFRONT ERROR in waves
        (the sub-wave WFE; the chief ray reads 0.0; self-references the chief so a
        lone non-chief ray returns a valid WFE).
    An unrecognized mode is reported as ``"unknown"`` (never guessed). Keyed on the
    RESOLVED mode so the label comes from the data, not from memory.
    """
    if opd_mode_name == "None":
        unit = _ac._lens_units_string(system)
        return f"{unit} (optical path length, NOT wavefront error)"
    if opd_mode_name == "Current":
        return "waves (raw absolute OPL, NOT chief-referenced, NOT wavefront error)"
    if opd_mode_name == "CurrentAndChief":
        return "waves (chief-referenced wavefront error)"
    return "unknown"


TRACE_RAYS_SPEC = ToolSpec(
    name="trace_rays",
    handler=trace_rays,
    required_params=("rays",),
    param_types={
        "rays": "array",
        "rays_type": "string",
        "opd_mode": "string",
        "to_surface": "number",
    },
    description=(
        "Trace a batch of rays through the system. Submit rays as "
        "[{wave, Hx, Hy, Px, Py}, ...]. Returns the full 15-field per-ray result "
        "(position, direction cosines, OPD, intensity, error/vignette codes). "
        "Gotcha: the opd field's UNITS and MEANING depend on opd_mode (echoed as "
        "opd_units). Default opd_mode='None' returns optical path LENGTH in the "
        "lens unit (~210 mm, NOT a wavefront error). For a chief-referenced "
        "WAVEFRONT ERROR in WAVES use opd_mode='CurrentAndChief' (self-references "
        "the chief; lone-ray-safe; the chief ray reads 0.0). opd_mode='Current' is "
        "the ray's RAW ABSOLUTE OPL in waves (state-dependent, NOT chief-referenced, "
        "NOT a wavefront error). For RMS wavefront use analyze_wavefront (RWCE/RWRE)."
    ),
)

TOOL_SPECS = (TRACE_RAYS_SPEC,)
