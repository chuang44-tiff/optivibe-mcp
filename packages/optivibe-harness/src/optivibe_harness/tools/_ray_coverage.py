"""tools/_ray_coverage.py -- SAMPLED-ray coverage (native-layout-retool INC-2b Part B).

The claim, narrowed FIRST: this module grades the
rays OptiVibe SAMPLED -- fields x wavelengths x pupil points, ``Px = 0`` -- and says
which of THOSE reach the image. Their correspondence to the rays the vendor exporter
actually draws is UNVERIFIED on the native path, and every envelope says so
(``basis.exporter_correspondence``). A status never certifies the drawing: a sampled
ray that fails to reach the image is a ray the picture MAY omit or cut short, and
that one-sided statement is all a flag ever makes.

Three parts:

* ``native_coverage`` -- ONE image trace of the sampled set after a SUCCESSFUL native
  export (fields x ALL waves x ``NumberOfRays`` Py) plus the bounded per-surface
  locator for the failing rays only. It opens NO batch tool of its own: every trace
  goes through ``_layout_rays._trace_surface_all_rays`` (the open-site census stays at
  14 sites / 13 modules).
* ``self_coverage`` -- the SAME shape built from ``_layout_rays.read_field_rays``'s
  per-ray ``coverage`` (zero extra engine cost; on this path the sample IS the drawing).
* pure classification + the flag / figure-line inputs.

Never raises: ``native_coverage`` returns the ``unavailable`` shape on any fault. It is
NEVER a refusal and NEVER a fallback token.
"""
from time import perf_counter

from ..enums import _resolve_enum
from . import _layout_rays as _rays
from .analysis_raytrace import _opd_mode_member, _rays_type_enum

# --- the envelope status -- a status names what it measured ------------ #
STATUS_ALL = "sampled_all_traced"
STATUS_FAILURES = "sampled_failures"
STATUS_NO_RAYS = "no_rays_requested"
STATUS_UNAVAILABLE = "unavailable"
STATUSES = (STATUS_ALL, STATUS_FAILURES, STATUS_NO_RAYS, STATUS_UNAVAILABLE)
# --- the per-field state ----------------------------------------------- #
STATE_ALL = "all_sampled_traced"
STATE_PARTIAL = "sampled_partial"
STATE_NONE = "none_sampled_traced"
STATES = (STATE_ALL, STATE_PARTIAL, STATE_NONE)
# --- the per-field termination: the FROZEN 5-token set ---------- #
TERM_GRADED = "graded"
TERM_BUDGET = "budget"
TERM_FRAME = "frame"
TERM_READ_FAULT = "read_fault"
TERM_NONFINITE = "nonfinite"
FIELD_TERMINATIONS = (TERM_GRADED, TERM_BUDGET, TERM_FRAME, TERM_READ_FAULT,
                      TERM_NONFINITE)
FAILURE_ERROR = "error"
FAILURE_VIGNETTE = "vignette"
# --- basis vocabulary ----------------------------------------------------------- #
CORRESPONDENCE_UNVERIFIED = "unverified"     # native: the sample is not the drawing
CORRESPONDENCE_SELF = "self_drawn"           # self: the sampled rays ARE the drawn rays
FIELD_NORMALISATION = "y_over_max_abs_y_meridional"   # RAYS._read_fields' Hy rule
WAVES_ALL = "all_superset"   # [unmeasured choice: over-discloses, L-P3W measures]
WAVES_PRIMARY = "primary_only"
# --- unavailable reasons ---------------------------------------------------------- #
REASON_WEDGED = "tools_slot_wedged"
REASON_NO_FIELDS = "no fields"
REASON_BUDGET = "budget exhausted"
REASON_FRAME = "frame not ok"
REASON_READ_FAULT = "ray read fault"
REASON_NONFINITE = "non-finite ray coordinate"
REASON_TRACE_UNAVAILABLE = "ray trace unavailable"
REASON_FIELDS_UNREADABLE = "fields unreadable"
REASON_WAVES_UNREADABLE = "wavelengths unreadable"
REASON_BATCH_UNAVAILABLE = "batch trace unavailable (the open returned None or raised)"
REASON_SUPPRESSED = "rays suppressed (degraded axial geometry)"
#: A ray the loop never opened for is NOT a budget-exhausted ray --
#: its own reason, though the frozen field termination stays "budget".
REASON_NOT_TRACED = "rays not traced (the budget elapsed before their first surface)"
#: An exporter Field read-back outside -1..n_fields.
REASON_FIELD_OUT_OF_RANGE = "exporter field out of range"
#: the ungraded ray reasons -> the field termination (first in ray order)
_RAY_TO_TERM = {
    _rays.RAY_BUDGET: TERM_BUDGET, _rays.RAY_NOT_TRACED: TERM_BUDGET,
    _rays.RAY_FRAME: TERM_FRAME, _rays.RAY_READ_FAULT: TERM_READ_FAULT,
    _rays.RAY_NONFINITE: TERM_NONFINITE,
}
#: the ungraded ray reason -> the envelope reason (not_traced has its own).
_RAY_TO_REASON = {
    _rays.RAY_BUDGET: REASON_BUDGET, _rays.RAY_NOT_TRACED: REASON_NOT_TRACED,
    _rays.RAY_FRAME: REASON_FRAME, _rays.RAY_READ_FAULT: REASON_READ_FAULT,
    _rays.RAY_NONFINITE: REASON_NONFINITE,
}
_TERM_TO_REASON = {
    TERM_BUDGET: REASON_BUDGET, TERM_FRAME: REASON_FRAME,
    TERM_READ_FAULT: REASON_READ_FAULT, TERM_NONFINITE: REASON_NONFINITE,
}
_GRADED_RAYS = (_rays.RAY_REACHED_IMAGE, _rays.RAY_ERROR, _rays.RAY_VIGNETTE)
#: The per-field flag, in the auditor's wording (#2): the counts carry the
#: difference between a partial and an empty field; the picture consequence is
#: CONDITIONAL, never asserted.
FLAG_FIELD = ("field {f} (Y={y:g}): {n_traced}/{n_rays} sampled rays reached the "
              "image; the drawing may omit or cut them short; first sampled failure "
              "at surface {k} ({kind})")
FLAG_UNAVAILABLE = "sampled-ray coverage not graded ({reason})"
FLAG_FIELD_SETTING = ("native export: the exporter's Field setting read back {k} (not "
                      "0); its semantics are unmeasured -- coverage sampled {what}")
#: A read-back that is None / non-int / raised is NOT 0 -- disclosed, all graded.
FLAG_FIELD_UNREADABLE = "native export: exporter field setting unreadable; all fields graded"
#: The sampled pupil for the self path (RAYS._PUPIL_RAYS order: chief, +1, -1).
_SELF_PUPIL = [0.0, 1.0, -1.0]


def _basis(renderer, *, n_per_field=None, fields=(), waves=(), selection=None,
           pupil=(), field_setting=None, wave_setting=None):
    return {
        "renderer": renderer, "sampled": True,
        "exporter_correspondence": (CORRESPONDENCE_UNVERIFIED if renderer == "native"
                                    else CORRESPONDENCE_SELF),
        "n_rays_per_field": n_per_field, "fields": list(fields),
        "field_normalisation": FIELD_NORMALISATION,
        "wavelengths": list(waves), "wavelength_selection": selection,
        "pupil_py": list(pupil), "px": 0.0,
        "exporter_field_setting": field_setting,
        "exporter_wavelength_setting": wave_setting,
    }


def _envelope(status, reason, basis, fields):
    return {"status": status, "reason": reason, "basis": basis, "fields": fields}


def unavailable(reason, *, renderer="native", basis=None, fields=None):
    """The ``unavailable`` shape: NOT a clean bill -- ``reason`` names which fault."""
    return _envelope(STATUS_UNAVAILABLE, reason,
                     basis if basis is not None else _basis(renderer),
                     list(fields or []))


def no_rays(renderer, basis=None):
    return _envelope(STATUS_NO_RAYS, None,
                     basis if basis is not None else _basis(renderer), [])


def _state(n_traced, n_rays):
    if n_traced == n_rays:
        return STATE_ALL
    return STATE_NONE if n_traced == 0 else STATE_PARTIAL


def _first(events):
    """``(k, kind)`` for the smallest ``k`` among ``[(k, kind)]``; error wins a tie."""
    if not events:
        return None, None
    k = min(e[0] for e in events)
    kinds = {e[1] for e in events if e[0] == k}
    return k, (FAILURE_ERROR if FAILURE_ERROR in kinds else FAILURE_VIGNETTE)


# =========================================================================== #
# NATIVE -- one image trace + the bounded locator
# =========================================================================== #
def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def native_coverage(system, res, *, draw_rays, deadline=None):
    """``(ray_coverage, flags)`` for one SUCCESSFUL native export. Never raises.

    Run ONLY after the PNG gate passed and ONLY when the session's tool-slot latch is
    not set (the caller's order). ``deadline`` is the shared
    ``OPTIVIBE_RAY_BUDGET_S`` ``perf_counter`` deadline, checked before EVERY open.
    """
    try:
        return _native(system, res, draw_rays=draw_rays, deadline=deadline)
    except Exception as exc:  # noqa: BLE001 -- coverage never sinks the render
        return (unavailable(f"coverage fault ({type(exc).__name__})",
                            renderer="native"), [])


def _elapsed(deadline):
    return deadline is not None and perf_counter() >= deadline


def _native(system, res, *, draw_rays, deadline):
    flags = []
    field_setting = _int_or_none(getattr(res, "field_setting", None))
    wave_setting = _int_or_none(getattr(res, "wavelength_setting", None))
    basis = _basis("native", selection=WAVES_ALL, field_setting=field_setting,
                   wave_setting=wave_setting)
    if not draw_rays:
        return no_rays("native", basis), flags
    n_py = _int_or_none(getattr(res, "n_rays", None))
    if n_py is None or n_py < 1:
        return no_rays("native", basis), flags
    try:
        hy_list, raw_y, _ff = _rays._read_fields(system)
    except Exception:  # noqa: BLE001
        return unavailable(REASON_FIELDS_UNREADABLE, basis=basis), flags
    if not hy_list:
        return unavailable(REASON_NO_FIELDS, basis=basis), flags   # #7
    field_ids = list(range(1, len(hy_list) + 1))
    # Any read-back other than 0 is disclosed [unmeasured semantics:
    # production never writes Field]. -1 = all fields (graded, never a silent
    # superset); 1..n = that field only; anything else is out of range.
    if field_setting is None:
        flags.append(FLAG_FIELD_UNREADABLE)                 # never silently 0
    elif field_setting != 0:
        if field_setting == -1:
            flags.append(FLAG_FIELD_SETTING.format(k=-1, what="all fields"))
        elif 1 <= field_setting <= len(hy_list):
            flags.append(FLAG_FIELD_SETTING.format(
                k=field_setting, what="field %d only" % field_setting))
            field_ids = [field_setting]
        else:
            flags.append(FLAG_FIELD_SETTING.format(k=field_setting, what="nothing"))
            return unavailable(REASON_FIELD_OUT_OF_RANGE, basis=basis), flags
    try:
        n_waves = int(system.SystemData.Wavelengths.NumberOfWavelengths)
    except Exception:  # noqa: BLE001
        return unavailable(REASON_WAVES_UNREADABLE, basis=basis), flags
    if n_waves < 1:
        return unavailable(REASON_WAVES_UNREADABLE, basis=basis), flags
    waves = list(range(1, n_waves + 1))
    # Py in linspace(LowerPupil, UpperPupil, NumberOfRays) = the written -1 .. +1
    # [unmeasured choice: the natural meridional fan incl. the chief and both
    # marginals -- an earlier pre-check used exactly this set].
    pupil = ([0.0] if n_py == 1
             else [-1.0 + 2.0 * i / (n_py - 1) for i in range(n_py)])
    basis.update(n_rays_per_field=n_waves * n_py, fields=field_ids, wavelengths=waves,
                 pupil_py=pupil)
    keys, specs = [], []
    for f in field_ids:
        for w in waves:
            for pi, py in enumerate(pupil):
                keys.append((f, w, pi))
                specs.append((w, 0.0, hy_list[f - 1], 0.0, py))
    if _elapsed(deadline):
        return unavailable(REASON_BUDGET, basis=basis), flags
    try:
        rays_real = _resolve_enum(_rays_type_enum(system), "Real")
        opd_none = _opd_mode_member(system, "None")
    except Exception:  # noqa: BLE001
        return unavailable(REASON_BATCH_UNAVAILABLE, basis=basis), flags
    # ONE open to the image (toSurface = -1, the _spot_validity idiom)
    results = _rays._trace_surface_all_rays(system, rays_real, opd_none, -1, specs)
    if results is None:
        return unavailable(REASON_BATCH_UNAVAILABLE, basis=basis), flags
    kind = []                       # per ray: None (good) | error | vignette | "row"
    for row in results:
        if row is None:
            kind.append("row")      # un-returned = BAD (the _spot_validity rule)
        elif row[0] != 0:
            kind.append(FAILURE_ERROR)
        elif row[1] != 0:
            kind.append(FAILURE_VIGNETTE)
        else:
            kind.append(None)
    failing = [i for i, k in enumerate(kind) if k is not None]
    located = _locate(system, rays_real, opd_none, specs, failing, deadline)
    fields = []
    for fi, f in enumerate(field_ids):
        idx = [i for i, key in enumerate(keys) if key[0] == f]
        n_rays = len(idx)
        n_traced = sum(1 for i in idx if kind[i] is None)
        state = _state(n_traced, n_rays)
        k = fk = None
        term = TERM_GRADED
        if state != STATE_ALL:
            found = [located[i] for i in idx if i in located
                     and located[i][1] in (FAILURE_ERROR, FAILURE_VIGNETTE)]
            open_ = [located[i] for i in idx if i in located
                     and located[i][1] not in (FAILURE_ERROR, FAILURE_VIGNETTE)]
            k, fk = _first(found)
            first_open = min(open_, key=lambda e: e[0]) if open_ else None
            if first_open is not None and (k is None or first_open[0] < k):
                # an unresolved ray might fail before the smallest found surface
                k, fk, term = None, None, first_open[1]
        if term != TERM_GRADED:
            state = None      # a locator budget/read fault certifies no state
        fields.append({
            "field": f, "field_y": raw_y[f - 1], "state": state,
            "n_traced": n_traced, "n_rays": n_rays,
            "first_failing_surface": k, "first_failure": fk,
            "error_codes": {
                "error": sum(1 for i in idx if kind[i] == FAILURE_ERROR),
                "vignette": sum(1 for i in idx if kind[i] == FAILURE_VIGNETTE)},
            "termination": term,
        })
    ungraded = [e for e in fields if e["termination"] != TERM_GRADED]
    if ungraded:                                   # never a graded envelope
        return unavailable(_TERM_TO_REASON.get(ungraded[0]["termination"],
                                               REASON_READ_FAULT),
                           basis=basis, fields=fields), flags
    status = (STATUS_ALL if all(e["state"] == STATE_ALL for e in fields)
              else STATUS_FAILURES)
    return _envelope(status, None, basis, fields), flags


def _locate(system, rays_real, opd_none, specs, failing, deadline):
    """``{ray_index: (k, error|vignette|budget|read_fault)}`` -- the FIRST surface
    each failing ray fails at, one open per ``k = 1 .. N-1`` over the still-unresolved
    failing rays only (the ``read_field_rays`` idiom), the deadline checked before
    every open. Ascending ``k``, so a found ``k`` is the smallest. Never raises."""
    out = {}
    if not failing:
        return out
    try:
        n = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001
        return {i: (0, TERM_READ_FAULT) for i in failing}
    todo = list(failing)
    for k in range(1, n):
        if not todo:
            break
        if _elapsed(deadline):
            for i in todo:
                out[i] = (k, TERM_BUDGET)
            return out
        rows = _rays._trace_surface_all_rays(
            system, rays_real, opd_none, k, [specs[i] for i in todo])
        if rows is None:
            for i in todo:
                out[i] = (k, TERM_READ_FAULT)
            return out
        left = []
        for i, row in zip(todo, rows):
            if row is None:
                out[i] = (k, TERM_READ_FAULT)
            elif row[0] != 0:
                out[i] = (k, FAILURE_ERROR)          # error wins when both are set
            elif row[1] != 0:
                out[i] = (k, FAILURE_VIGNETTE)
            else:
                left.append(i)
        todo = left
    for i in todo:                  # failed at the image but never per surface
        out[i] = (n, TERM_READ_FAULT)
    return out


# =========================================================================== #
# SELF -- from the reader's per-ray coverage (no second trace)
# =========================================================================== #
def self_coverage(ray_data, *, draw_rays, latched=False, suppressed=False):
    """``ray_coverage`` for the self renderer. Pure; never raises."""
    try:
        return _self(ray_data, draw_rays=draw_rays, latched=latched,
                     suppressed=suppressed)
    except Exception as exc:  # noqa: BLE001
        return unavailable(f"coverage fault ({type(exc).__name__})", renderer="self")


def _self(ray_data, *, draw_rays, latched, suppressed):
    basis = _basis("self", n_per_field=len(_SELF_PUPIL), waves=[1],
                   selection=WAVES_PRIMARY, pupil=_SELF_PUPIL)
    if not draw_rays:
        return no_rays("self", basis)
    if latched:
        return unavailable(REASON_WEDGED, basis=basis)
    if suppressed:
        return unavailable(REASON_SUPPRESSED, basis=basis)
    if not isinstance(ray_data, dict):
        return unavailable(REASON_TRACE_UNAVAILABLE, basis=basis)
    run = ray_data.get("termination")
    if run == _rays.RUN_TOTAL_FAILURE or run is None:
        return unavailable(REASON_TRACE_UNAVAILABLE, basis=basis)
    out_fields = list(ray_data.get("fields") or [])
    if not out_fields:
        return unavailable(REASON_NO_FIELDS, basis=basis)
    coverage = list(ray_data.get("coverage") or [])
    basis["fields"] = [int(e["field_index"]) + 1 for e in out_fields]
    fields = []
    reasons = []            # per field: the first ungraded ray's reason, or None
    for entry in out_fields:
        fi = entry["field_index"]
        rays = [c for c in coverage if c.get("field_index") == fi]
        term = TERM_GRADED
        why = None
        for c in rays:
            if c.get("terminated_by") not in _GRADED_RAYS:
                term = _RAY_TO_TERM.get(c.get("terminated_by"), TERM_READ_FAULT)
                why = _RAY_TO_REASON.get(c.get("terminated_by"), REASON_READ_FAULT)
                break
        n_rays = len(rays)
        n_traced = sum(1 for c in rays if c.get("terminated_by") == _rays.RAY_REACHED_IMAGE)
        fails = [(c.get("at_k"), c.get("terminated_by")) for c in rays
                 if c.get("terminated_by") in (_rays.RAY_ERROR, _rays.RAY_VIGNETTE)]
        k, fk = _first(fails)
        graded = term == TERM_GRADED and n_rays > 0
        fields.append({
            "field": fi + 1, "field_y": entry.get("field_y"),
            "state": _state(n_traced, n_rays) if graded else None,
            "n_traced": n_traced, "n_rays": n_rays,
            "first_failing_surface": k, "first_failure": fk,
            "error_codes": {
                "error": sum(1 for c in rays if c.get("terminated_by") == _rays.RAY_ERROR),
                "vignette": sum(1 for c in rays
                                if c.get("terminated_by") == _rays.RAY_VIGNETTE)},
            "termination": term if n_rays > 0 else TERM_READ_FAULT,
        })
        reasons.append(why if n_rays > 0 else REASON_READ_FAULT)
    first = next((r for r in reasons if r is not None), None)
    if run == _rays.RUN_BUDGET_EXHAUSTED:
        return unavailable(first or REASON_BUDGET, basis=basis, fields=fields)
    if first is not None:
        return unavailable(first, basis=basis, fields=fields)
    status = (STATUS_ALL if all(e["state"] == STATE_ALL for e in fields)
              else STATUS_FAILURES)
    return _envelope(status, None, basis, fields)


# =========================================================================== #
# flags + the figure-line inputs
# =========================================================================== #
def field_flag(entry):
    k = entry.get("first_failing_surface")
    return FLAG_FIELD.format(
        f=entry["field"], y=float(entry.get("field_y") or 0.0),
        n_traced=entry["n_traced"], n_rays=entry["n_rays"],
        k=(k if k is not None else f"unknown ({entry.get('termination')})"),
        kind=(entry.get("first_failure") or "unknown"))


def coverage_flags(cov, *, only_none=False):
    """One flag per affected field (``only_none``: the self path's
    ``none_sampled_traced`` rule), or ONE ``unavailable`` flag."""
    status = cov.get("status")
    if status == STATUS_UNAVAILABLE:
        return [FLAG_UNAVAILABLE.format(reason=cov.get("reason"))]
    if status != STATUS_FAILURES:
        return []
    wanted = (STATE_NONE,) if only_none else (STATE_PARTIAL, STATE_NONE)
    return [field_flag(e) for e in cov.get("fields", []) if e.get("state") in wanted]


def missing_summary(cov):
    """``(field numbers, n_failed, n_sampled)`` for ``_S_RAYS_MISSING``, or ``None``
    unless the status is ``sampled_failures``."""
    if cov.get("status") != STATUS_FAILURES:
        return None
    fields = cov.get("fields", [])
    bad = [e for e in fields if e.get("state") in (STATE_PARTIAL, STATE_NONE)]
    if not bad:
        return None
    n_failed = sum(e["n_rays"] - e["n_traced"] for e in bad)
    n_sampled = sum(e["n_rays"] for e in fields)
    return [e["field"] for e in bad], n_failed, n_sampled


__all__ = ["native_coverage", "self_coverage", "unavailable", "coverage_flags",
           "missing_summary", "STATUSES", "STATES", "FIELD_TERMINATIONS"]
