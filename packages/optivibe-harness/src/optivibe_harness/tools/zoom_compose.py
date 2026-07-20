"""tools/zoom_compose.py — the ONE zoom/focus/conjugate/array composer ``set_zoom`` (MCE).

ONE dispatchable composer over the S1 multi-configuration primitive. A HARD INTERNAL
FORK on ``mode`` {zoom, focus, conjugate, array} (validated PRE-mutation, so a ``mode``
cannot reach the wrong falsifier — the OPEN-1 wrong-oracle structural defense, D8):

- ``zoom`` / ``conjugate`` — per-config measure-and-solve a chosen-gap ``THIC`` toward a
  per-config target (``EFFL`` for zoom, ``PMAG`` for conjugate) via the bounded bracketing
  secant on the MONOTONE map (``_zoom_solve``); the circularity defense gates on the
  INDEPENDENT ordering + PMAG/PIMH-DIFFERS check the solve never drove (D2/§4.2).
- ``focus`` — per-config best focus on the BACK gap: a supplied ``back_distances`` ->
  secant the back-gap THIC to it; ABSENT -> golden-section MINIMIZE the per-config defocus
  (``|REAY|``); the independent net is per-config PMAG/PIMH-DIFFERS (D7/§4.3). Does NOT
  nest ``with_best_focus`` (the per-config THIC matrix would be clobbered, §4.3).
- ``array`` — relocate the SAME element span (``first..last``) to a per-config 3-D position
  via the ELEMENT-COORDINATE family (``CADX``/``CADY`` + preceding-gap ``THIC``-Z, NEVER
  the CB family), DIRECT-authored, falsified by the REFERENCE-INDEPENDENT real-ray oracle:
  the chief-ray IMAGE-PLANE landing (``REAX`` for decenter_x, ``REAY`` for decenter_y)
  MUST move MONOTONICALLY with the authored decenters above a floor, AND
  ``beam_reaches_span`` reaches. NEVER ``RAGX`` global / ``GetGlobalMatrix`` as the gate —
  the probe (``probe_mce_s2d.py``) live-falsified that BOTH are reference-surface-dependent
  (they read 0 for a DOWNSTREAM decenter) while ``REAX`` at the image moves by the channel's
  actual sensor landing; ``RAGX`` global is a SECONDARY corroborator ONLY (disclosed, not
  gated). Discloses ``per_config_image_landing``/``per_config_image_shift`` (the
  multi-sensor channel landing) + a WARNING that each channel images at a DIFFERENT plane
  unless a per-config back-focus (``mode=focus``) is ALSO authored (D4 — no auto-chain).

ALL authoring DELEGATES to the S1 dispatchable handlers (``set_config_operand`` /
``set_config_value`` / ``set_current_configuration``) — the composer adds NO cell-write
code (D6); the S1 primitive owns INVARIANT-1, the DataType-keyed read-back, and the never-
raise envelope, inherited on every write. The whole compose runs inside ONE outer
SaveAs/LoadFile atomic checkpoint with a POST-RESTORE read-vs-read verify (the
``place_element`` / ``fold_beam`` shape, §6). The tool NEVER raises past its boundary; every path -> the structured envelope (§3.2/3.3/3.4). NO new error class (D9) —
the FIVE families (§7) are STRING constants via ``error_envelope``.

Live ZOS-API integration; unit-tested against fixture fakes (which COMPUTE
EFL-from-THIC + chief-ray-from-CADX + the GetGlobalMatrix-graded false-pass pin).
"""
import math

from ..enums import _resolve_enum
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _lens_common as _lc
from . import _zoom_array as _za
from . import _zoom_solve as _zs
from . import mce_config as _mce
from ._analysis_common import error_envelope
from ._mce_cells import current_configuration, number_of_configurations
from ._zoom_solve import _ZoomFloored, _ZoomUnverified
from .lens_system import _zosapi_enum

# The families (§7) — STRING constants attached via error_envelope (NO new class).
_ZOOM_PARAM = "zoom_param"                          # bad/missing param; pre-mutation, ZERO mutation
_ZOOM_SOLVE_UNVERIFIED = "zoom_solve_unverified"    # solve/gate miss -> rollback
_ZOOM_WRITE = "zoom_write"                          # delegated refusal / checkpoint / engine throw
_ARRAY_UNREACHED = _za._ARRAY_UNREACHED             # rays miss a downstream optic -> rollback
_ARRAY_NO_BITE = _za._ARRAY_NO_BITE                 # placement inert across configs -> rollback
# STOP-ZOOM (hold_fnum): the f/# falsifier missed (rolled back) / no f/# to hold.
_ZOOM_FNUM_UNHELD = "zoom_fnum_unheld"
_ZOOM_FNUM_INDETERMINATE = "zoom_fnum_indeterminate"

# The absolute f/# tolerance for the WFNO-per-config falsifier (D-5; probe held to 0.06).
_FNUM_HOLD_TOL = 0.1
# The minimum per-config EPD spread proving the stop ACTUALLY zoomed (a held f/# must not be
# an artifact of a frozen pupil — D-5 corollary). Below this the EPDs are indistinguishable.
_EPD_DIFFERS_MIN_SPREAD = 1e-6

_MODES = ("zoom", "focus", "conjugate", "array")

# The golden-section focus-minimize window half-width (lens units around the loaded THIC).
_FOCUS_SCAN_HALFWIDTH = 5.0


# --------------------------------------------------------------------------- #
# Shared validation.
# --------------------------------------------------------------------------- #
def _require_dict(params):
    """Never-raise: a non-dict ``params`` becomes ``{}`` (§3.4 floor)."""
    return params if isinstance(params, dict) else {}


def _finite(value, label):
    """Require a FINITE number (reject bool / non-number / inf / nan) -> float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{label} must be a finite number, got {type(value).__name__} {value!r}"
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"{label} must be a finite number (inf/-inf/nan are non-physical), got "
            f"{value!r}"
        )
    return coerced


# =========================================================================== #
# set_zoom — the dispatchable composer.
# =========================================================================== #
def set_zoom(session, params):
    """Zoom/focus/conjugate/array per-config composer over the MCE primitive (§3).

    Params (§3): ``mode`` (REQUIRED ∈ {zoom, focus, conjugate, array}); EFL family
    (``surface``, ``targets``); focus (``back_distances``); array (``first``, ``last``,
    ``positions``); shared (``tolerance``, ``design_name``). Per-mode params are
    CONDITIONALLY required (enforced here, the set_field XOR precedent — flat ``required``
    + the handler enforces the rest).

    Runs the validate-before-mutate firewall (§3.5, ZERO mutation on refusal), then the
    HARD ``mode`` fork inside ONE SaveAs/LoadFile atomic checkpoint (§6). Rolls back on
    ANY falsifier miss / fault with a POST-RESTORE read-vs-read verify. NEVER raises past
    the boundary — every path returns the structured envelope.
    """
    params = _require_dict(params)
    try:
        plan = _validate_zoom_params(session, params)
    except ToolParamError as exc:
        return error_envelope("set_zoom", _ZOOM_PARAM, str(exc),
                              mode=params.get("mode"))
    except _ZoomUnverified as exc:
        # A pre-mutation validate-side family-carrying refusal (e.g. z_to_thic on a
        # non-finite Z classified zoom_param) — ZERO mutation.
        return error_envelope("set_zoom", exc.error_family, str(exc),
                              mode=params.get("mode"))
    except Exception as exc:  # noqa: BLE001 — a pre-mutation read fault -> zoom_param
        return error_envelope(
            "set_zoom", _ZOOM_PARAM,
            f"could not validate the set_zoom request ({exc!r})",
            mode=params.get("mode"),
        )

    return _zoom_checkpointed(session, plan)


# --------------------------------------------------------------------------- #
# §3.5 the validate-before-mutate firewall (pre-checkpoint, ZERO mutation).
# --------------------------------------------------------------------------- #
def _validate_zoom_params(session, params):
    """Validate every param BEFORE the SaveAs checkpoint (§3.5). Returns a ``plan`` dict.

    A refusal mutates NOTHING (it runs pre-checkpoint). Resolves ``mode`` against the
    frozen table; reads ``NumberOfConfigurations`` live (>= 2 or refuse); validates the
    per-mode length/finiteness/surface-range invariants; resolves the gap surface for the
    EFL family. The plan carries everything ``_zoom_checkpointed`` needs.
    """
    system = session.system

    mode = params.get("mode")
    if not isinstance(mode, str) or mode not in _MODES:
        raise ToolParamError(
            f"mode is REQUIRED and must be one of {list(_MODES)} (no default — the four "
            f"modes have DIFFERENT operands, surfaces, and falsifiers); got {mode!r}. If "
            "the design intent does not state the mode, ask the user."
        )

    n_configs = number_of_configurations(system)
    if n_configs < 2:
        raise ToolParamError(
            f"set_zoom needs >= 2 configurations, the system has {n_configs}; add "
            "configurations first (add_configuration) before authoring a per-config zoom/"
            "focus/conjugate/array"
        )

    n_surfaces = int(system.LDE.NumberOfSurfaces)
    tolerance = _optional_finite(params, "tolerance")
    design_name = _optional_str(params, "design_name")

    plan = {
        "mode": mode,
        "n_configs": n_configs,
        "n_surfaces": n_surfaces,
        "tolerance": tolerance,
        "design_name": design_name,
        "active_before": current_configuration(system),
    }

    if mode in ("zoom", "conjugate"):
        _validate_efl_family(system, params, plan, mode, n_configs, n_surfaces)
    elif mode == "focus":
        _validate_focus(system, params, plan, n_configs, n_surfaces)
    else:  # array
        _validate_array(system, params, plan, n_configs, n_surfaces)

    # STOP-ZOOM (D-4): the default-ON hold_fnum coupling — ONLY for mode='zoom'
    # (the other modes do not change EFL the same way; the f/# coupling is a zoom concept).
    if mode == "zoom":
        _validate_hold_fnum(system, params, plan)
    return plan


def _validate_hold_fnum(system, params, plan):
    """Parse hold_fnum/fnum + read the current WFNO PRE-mutation (§D-4/D-5).

    ``hold_fnum`` (bool, default True) couples a per-config aperture so f/# holds across the
    zoom; ``fnum`` (optional finite > 0) is the target image-space f/# to hold — when omitted,
    hold the CURRENT working f/# read HERE (pre-mutation, so a later aperture change cannot
    perturb the reference). A non-finite/<=0 current WFNO with NO ``fnum`` -> a pre-mutation
    ``zoom_fnum_indeterminate`` refusal (the system has no readable f/# to hold; ZERO mutation).
    """
    hold_fnum = params.get("hold_fnum", True)
    if not isinstance(hold_fnum, bool):
        raise ToolParamError(
            f"hold_fnum must be a bool, got {type(hold_fnum).__name__} {hold_fnum!r}"
        )
    plan["hold_fnum"] = hold_fnum
    if not hold_fnum:
        plan["fnum"] = None
        plan["current_wfno"] = None
        return

    fnum = params.get("fnum")
    if fnum is not None:
        if isinstance(fnum, bool) or not isinstance(fnum, (int, float)):
            raise ToolParamError(
                f"fnum must be a number, got {type(fnum).__name__} {fnum!r}"
            )
        if not math.isfinite(float(fnum)) or float(fnum) <= 0.0:
            raise ToolParamError(f"fnum must be a finite f/number > 0, got {fnum!r}")
        plan["fnum"] = float(fnum)
    else:
        plan["fnum"] = None

    # Read the CURRENT working f/# (the reference when no explicit fnum). NON-MUTATING.
    current_wfno = None
    try:
        current_wfno = _zs.read_wfno_for_config(system)
    except Exception:  # noqa: BLE001 — an unreadable WFNO -> only an explicit fnum can proceed
        current_wfno = None
    plan["current_wfno"] = current_wfno
    if plan["fnum"] is None and (
        not isinstance(current_wfno, (int, float)) or isinstance(current_wfno, bool)
        or not math.isfinite(float(current_wfno)) or float(current_wfno) <= 0.0
    ):
        raise _ZoomUnverified(
            "hold_fnum=True but the current working f/number is unreadable/degenerate "
            f"({current_wfno!r}) and no explicit `fnum` was given — cannot determine the "
            "f/# to hold. Pass `fnum` (the target image-space f/number) or hold_fnum=False.",
            family=_ZOOM_FNUM_INDETERMINATE,
        )


def _validate_efl_family(system, params, plan, mode, n_configs, n_surfaces):
    """zoom / conjugate: targets length == N + finite; resolve + range-check the gap."""
    targets = params.get("targets")
    if not isinstance(targets, (list, tuple)):
        raise ToolParamError(
            f"mode={mode!r} requires a `targets` list (one target per configuration), "
            f"got {type(targets).__name__} {targets!r}"
        )
    if len(targets) != n_configs:
        raise ToolParamError(
            f"`targets` has {len(targets)} entries but the system has {n_configs} "
            f"configurations; supply exactly one target per config"
        )
    clean = []
    for i, t in enumerate(targets):
        tv = _finite(t, f"targets[{i}]")
        if mode == "zoom" and tv <= 0.0:
            raise ToolParamError(
                f"mode='zoom' target EFL targets[{i}]={tv} must be > 0"
            )
        clean.append(tv)
    plan["targets"] = clean

    surface = _optional_int(params, "surface")
    if mode == "zoom":
        if surface is None:
            raise ToolParamError(
                "mode='zoom' requires `surface` — the interior airgap to zoom (NO safe "
                "default; which gap is the zoom group is a design choice). Pass the "
                "surface number of the zoom airgap."
            )
        # zoom gap: an interior surface (refuse OBJECT 0 and IMAGE).
        if not (1 <= surface <= n_surfaces - 2):
            raise ToolParamError(
                f"zoom surface {surface} out of range; valid 1..{n_surfaces - 2} "
                f"(OBJECT 0, IMAGE {n_surfaces - 1}, and beyond are refused; "
                f"N={n_surfaces})"
            )
        gap_surface = surface
    else:  # conjugate
        # conjugate gap defaults to the OBJECT gap (surface 0); a supplied surface is
        # range-checked 0..N-2 (OBJECT 0 IS valid here — its gap is the object distance).
        gap_surface = _zs.resolve_zoom_surface(system, "conjugate", surface)
        if gap_surface is None:
            raise ToolParamError(
                "could not resolve the conjugate (object) gap surface"
            )
        if not (0 <= gap_surface <= n_surfaces - 2):
            raise ToolParamError(
                f"conjugate surface {gap_surface} out of range; valid 0..{n_surfaces - 2}"
            )
    plan["gap_surface"] = gap_surface
    plan["grading"] = "EFFL" if mode == "zoom" else "PMAG"


def _validate_focus(system, params, plan, n_configs, n_surfaces):
    """focus: optional back_distances (len==N + >0); resolve the back gap."""
    back_distances = params.get("back_distances")
    if back_distances is not None:
        if not isinstance(back_distances, (list, tuple)):
            raise ToolParamError(
                f"`back_distances` must be a list (one per config), got "
                f"{type(back_distances).__name__} {back_distances!r}"
            )
        if len(back_distances) != n_configs:
            raise ToolParamError(
                f"`back_distances` has {len(back_distances)} entries but the system has "
                f"{n_configs} configurations; supply exactly one per config"
            )
        clean = []
        for i, b in enumerate(back_distances):
            bv = _finite(b, f"back_distances[{i}]")
            if bv <= 0.0:
                raise ToolParamError(
                    f"back_distances[{i}]={bv} must be > 0 (a back-focus distance)"
                )
            clean.append(bv)
        plan["back_distances"] = clean
    else:
        plan["back_distances"] = None  # minimize path

    surface = _optional_int(params, "surface")
    gap_surface = _zs.resolve_zoom_surface(system, "focus", surface)
    if gap_surface is None:
        raise ToolParamError(
            "could not resolve the back-airgap surface for focus mode (need OBJECT + an "
            "airgap + IMAGE); pass `surface` explicitly"
        )
    if not (1 <= gap_surface <= n_surfaces - 2):
        raise ToolParamError(
            f"focus back-gap surface {gap_surface} out of range; valid 1..{n_surfaces - 2}"
        )
    plan["gap_surface"] = gap_surface
    plan["grading"] = "REAY"


def _validate_array(system, params, plan, n_configs, n_surfaces):
    """array: first/last range-checked; positions len==N + finite; Z-on-glass refused (D5)."""
    first = _optional_int(params, "first")
    last = _optional_int(params, "last")
    if first is None or last is None:
        raise ToolParamError(
            "mode='array' requires `first` and `last` — the element span to relocate per "
            "config"
        )
    if not (1 <= first <= n_surfaces - 1):
        raise ToolParamError(
            f"array first {first} out of range; valid 1..{n_surfaces - 1} (OBJECT 0 "
            f"refused; N={n_surfaces})"
        )
    if not (first <= last <= n_surfaces - 1):
        raise ToolParamError(
            f"array last {last} out of range; need first({first}) <= last <= "
            f"{n_surfaces - 1}"
        )
    plan["first"] = first
    plan["last"] = last

    positions = params.get("positions")
    if not isinstance(positions, (list, tuple)):
        raise ToolParamError(
            f"mode='array' requires a `positions` list (one dict per config), got "
            f"{type(positions).__name__} {positions!r}"
        )
    if len(positions) != n_configs:
        raise ToolParamError(
            f"`positions` has {len(positions)} entries but the system has {n_configs} "
            f"configurations; supply exactly one per config"
        )

    wants_dx = wants_dy = wants_z = False
    clean = []
    for i, p in enumerate(positions):
        if not isinstance(p, dict):
            raise ToolParamError(
                f"positions[{i}] must be a dict with optional decenter_x/decenter_y/z, "
                f"got {type(p).__name__} {p!r}"
            )
        entry = {}
        if "decenter_x" in p and p["decenter_x"] is not None:
            entry["decenter_x"] = _finite(p["decenter_x"], f"positions[{i}].decenter_x")
            wants_dx = True
        if "decenter_y" in p and p["decenter_y"] is not None:
            entry["decenter_y"] = _finite(p["decenter_y"], f"positions[{i}].decenter_y")
            wants_dy = True
        if "z" in p and p["z"] is not None:
            entry["z"] = _finite(p["z"], f"positions[{i}].z")
            wants_z = True
        clean.append(entry)
    if not (wants_dx or wants_dy or wants_z):
        raise ToolParamError(
            "no positions entry requests any decenter_x/decenter_y/z; an array with no "
            "per-config placement is a no-op — supply at least one placement coordinate"
        )
    plan["positions"] = clean
    plan["wants_dx"] = wants_dx
    plan["wants_dy"] = wants_dy
    plan["wants_z"] = wants_z

    # D5 firewall: a Z-on-glass (first-1 NOT an airgap) is refused PRE-mutation.
    if wants_z:
        z_gap = first - 1
        if z_gap < 1:
            raise ToolParamError(
                f"array Z requested but the preceding surface {z_gap} is the object/"
                "out of range; there is no preceding airgap to carry Z — insert a dummy "
                "airgap or omit z (D5)"
            )
        if not _is_airgap(system, z_gap):
            raise ToolParamError(
                f"array Z requested but surface {z_gap} (before first={first}) is NOT an "
                "airgap (it carries a glass/material); there is no preceding airgap to "
                "carry Z — insert a dummy airgap with insert_surface or omit z (D5)"
            )
        plan["z_gap"] = z_gap


def _is_airgap(system, surface):
    """True iff ``surface`` is an air gap (empty/no material), THROW-guarded.

    An unreadable material is treated as NOT an airgap (fail-closed — the D5 refusal
    fires rather than silently authoring a Z onto a glass).
    """
    try:
        mat = str(system.LDE.GetSurfaceAt(surface).Material).strip()
    except Exception:  # noqa: BLE001 — unreadable material -> fail-closed (not airgap)
        return False
    return mat == "" or mat == "-"


def _optional_int(params, key):
    if key not in params or params.get(key) is None:
        return None
    return _lc._require_int_index(params, key)


def _optional_finite(params, key):
    if key not in params or params.get(key) is None:
        return None
    return _finite(params[key], key)


def _optional_str(params, key):
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolParamError(f"{key} must be a string, got {type(value).__name__}")
    return value


# --------------------------------------------------------------------------- #
# §6 the atomic checkpointed body.
# --------------------------------------------------------------------------- #
def _zoom_checkpointed(session, plan):
    """Run the whole compose inside ONE SaveAs/LoadFile checkpoint (§6). Never raises.

    On ANY fault — a falsifier reject (``_ZoomUnverified``), a delegated S1-tool refusal,
    a generic engine throw — ``LoadFile`` rollback + a POST-RESTORE read-vs-read verify
    (``NumberOfSurfaces`` + ``NumberOfConfigurations`` + the active config index). A clean-
    but-didn't-restore LoadFile -> ``rolled_back:false, partial_state:true``. The temp
    ``.zmx`` + its ``.ZDA`` companion are reaped on every path (#59). The pre-call active
    config is restored on the happy path too (§6 / B-7).
    """
    import glob
    import os
    import tempfile

    from .optimize_merit_io import _unlink_quiet

    system = session.system
    mode = plan["mode"]

    checkpoint_path = None
    try:
        try:
            fd, checkpoint_path = tempfile.mkstemp(
                suffix=".zmx", prefix="optivibe_zoom_ckpt_"
            )
            os.close(fd)
            pre_snapshot = _zoom_pre_snapshot(system, plan)
            system.SaveAs(_fwd(checkpoint_path))
        except Exception as exc:  # noqa: BLE001 — a checkpoint SaveAs throw -> fail-closed
            _reap_checkpoint(checkpoint_path, glob, os, _unlink_quiet)
            checkpoint_path = None
            return error_envelope(
                "set_zoom", _ZOOM_WRITE,
                f"could not checkpoint the system before the zoom compose ({exc!r}); the "
                "system was NOT mutated — nothing was applied",
                mode=mode, rolled_back=False, checkpoint=False, partial_state=False,
            )

        try:
            if mode == "array":
                return _compose_array(session, plan)
            return _compose_efl_family(session, plan)
        except _ZoomUnverified as exc:
            return _rollback(
                system, checkpoint_path, pre_snapshot, mode=mode,
                family=exc.error_family, reason=str(exc), extra=exc.extra,
            )
        except (ToolParamError, SurfaceWriteError) as exc:
            return _rollback(
                system, checkpoint_path, pre_snapshot, mode=mode, family=_ZOOM_WRITE,
                reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — a generic engine throw -> rollback
            return _rollback(
                system, checkpoint_path, pre_snapshot, mode=mode, family=_ZOOM_WRITE,
                reason=f"unexpected engine fault in the zoom compose ({exc!r})",
            )
    finally:
        _reap_checkpoint(checkpoint_path, glob, os, _unlink_quiet)


# --------------------------------------------------------------------------- #
# The EFL family (zoom / focus / conjugate) orchestration (§4).
# --------------------------------------------------------------------------- #
def _compose_efl_family(session, plan):
    """zoom / conjugate / focus: author the THIC row + per-config solve + circularity gate.

    Authors the per-config THIC MCE row ONCE (S1 ``set_config_operand``), then per config:
    ``set_current_configuration`` (the BITE) -> secant the THIC to the config's target
    (or golden-section min for focus-minimize) -> set_config_value -> RE-READ the grading
    (FALSIFY-not-trust). The COMMIT gate is the direct within-tolerance check AND the
    INDEPENDENT ``independent_order_ok`` cross-config gate. Restores the pre-call active
    config (B-7). Raises ``_ZoomUnverified`` into the checkpointed caller on any miss.
    """
    system = session.system
    mode = plan["mode"]
    gap_surface = plan["gap_surface"]
    n_configs = plan["n_configs"]
    grading_code = plan["grading"]
    tol = plan["tolerance"]
    if tol is None:
        tol = _zs._DEFAULT_TOL[mode]

    # Author the per-config THIC row ONCE (S1 handler — INVARIANT-1 on writes, D6).
    row = _author_thic_row(session, gap_surface)

    per_config = []
    independent_values = []
    targets = plan.get("targets")  # None for focus-minimize
    is_focus_min = (mode == "focus" and plan.get("back_distances") is None)
    back_distances = plan.get("back_distances")

    nearest_configs = []  # the per-config solve floored to the nearest physical gap (#10)
    for cfg in range(1, n_configs + 1):
        _switch_config(session, cfg)

        # The per-config write closure (delegates to S1 set_config_value, read-back-proven).
        def _write(thic, _row=row, _cfg=cfg):
            return _set_thic_value(session, _row, _cfg, thic)

        nearest = None    # populated ONLY on a sub-floor _ZoomFloored (converge-to-nearest)
        try:
            if mode == "zoom":
                target = targets[cfg - 1]
                solved_thic, achieved, iters = _zs.solve_config_thic_to_target(
                    system, gap_surface, _write,
                    lambda: _zs.read_efl_for_config(system),
                    target, tol=tol,
                )
            elif mode == "conjugate":
                target = targets[cfg - 1]
                solved_thic, achieved, iters = _zs.solve_config_thic_to_target(
                    system, gap_surface, _write,
                    lambda: _zs.read_pmag_for_config(system),
                    target, tol=tol,
                )
            elif not is_focus_min:  # focus with supplied back_distances
                target = back_distances[cfg - 1]
                # Grading = the THIC value itself (the back-distance the user asked for).
                solved_thic, achieved, iters = _zs.solve_config_thic_to_target(
                    system, gap_surface, _write,
                    lambda: _read_thic(system, gap_surface),
                    target, tol=tol,
                )
            else:  # focus-minimize (no _ZoomFloored path — golden-section commits the floor)
                target = None
                solved_thic, achieved, iters = _focus_minimize(
                    session, system, gap_surface, _write,
                )
        except _ZoomFloored as fl:
            # #10 converge-to-nearest: the target is sub-floor — the gap is ALREADY authored
            # at the physical floor (_eval_at wrote gap_floor, SC-9). COMMIT the nearest
            # physical gap + disclose; do NOT roll back (F-E/F-F). DISTINCT from _ZoomUnverified
            # (a REAL fault still rolls back). gap_surface inside the solver == this gap_surface.
            target = fl.target
            solved_thic = fl.gap_floor
            achieved = fl.g_floor
            iters = -2          # sentinel: floored-nearest path (distinct from -1 minimize)
            nearest = {"target": fl.target, "achievable": fl.g_floor,
                       "shortfall": fl.shortfall, "gap_floor": fl.gap_floor,
                       "candidate": fl.candidate}

        # FALSIFY-not-trust: RE-READ the grading AFTER the final write (never the solver's
        # last eval — a desync between the solve loop and the committed cell is caught).
        final_grading = _final_grading(system, mode, grading_code, gap_surface,
                                       back_distances, cfg)
        # The INDEPENDENT corroborator — chosen DISCRIMINATING per mode (§13.2): the
        # per-config ACTIVE-gap THIC for zoom/focus (LDE geometry, independent of EFFL,
        # discriminating on a normal inf-conjugate on-axis system where PMAG/PIMH are 0);
        # PMAG for conjugate (genuinely differs on a finite object). Read AFTER the switch
        # so it reflects THIS config.
        independent = _read_independent(system, mode, gap_surface)
        independent_values.append(independent)

        within = (target is None) or (abs(final_grading - target) <= tol)
        rec = {
            "config": cfg,
            "target": _safe(target) if target is not None else None,
            "achieved": _safe(final_grading),
            "residual": _safe(abs(final_grading - target)) if target is not None else None,
            "solve_iterations": iters,
            "thic_value": _safe(solved_thic),
            "within_tolerance": bool(within),
            "independent_value": _safe(independent),
        }
        if nearest is not None:
            rec["nearest_achievable"] = True
            rec["achievable_efl"] = _safe(nearest["achievable"])    # the grading at the floor
            rec["shortfall"] = _safe(nearest["shortfall"])          # |g_floor - target|
            rec["gap_floor"] = _safe(nearest["gap_floor"])
            rec["requested_unphysical_thic"] = _safe(nearest["candidate"])  # the dogfood -3.5
            nearest_configs.append(cfg)
        per_config.append(rec)
        # The EXISTING under-solve rollback — but EXEMPT a nearest config (it intentionally did
        # not reach the target; it reached the nearest PHYSICAL gap). A genuine miss STILL
        # rolls back (the rollback path is unchanged for a real fault, F-H/§3.6).
        if not within and nearest is None:
            raise _ZoomUnverified(
                f"config {cfg} {mode} did not reach its target after the solve: target "
                f"{target}, achieved {final_grading} (residual {abs(final_grading - target)}"
                f" > {tol}) on the re-read — rolling back rather than shipping an "
                "under-solved config",
                extra={"failing_config": cfg, "requested": _safe(target),
                       "achieved": _safe(final_grading)},
            )

    # Restore the pre-call active config (B-7) BEFORE the commit gate so the system is
    # left where the agent was.
    _switch_config(session, plan["active_before"])

    # The COMMIT gate: the INDEPENDENT circularity defense (§4.2 / §13.2). For zoom the
    # ordering half is the PRIMARY non-collapse proof (a no-op switch cannot read N strictly
    # ordered EFLs); for focus/conjugate the ordering half is skipped (targets need not be
    # ordered). The DISCRIMINATING corroborator is the per-config ACTIVE-gap THIC (zoom/
    # focus) or PMAG (conjugate) — NOT PMAG/PIMH for zoom/focus (degenerate-0 on a normal
    # inf-conjugate on-axis system, the §13.2 live-caught false-reject). A LEGITIMATELY
    # all-identical-target solve is DISCLOSED + WARNED (degenerate), never hard-rejected.
    #
    # The non-independent gate honesty: focus with SUPPLIED back_distances is a
    # DIRECT-AUTHORING read-back — the tool sets the back-gap THIC to the supplied distance
    # and re-reads the SAME THIC; achieved==target IS the proof, there is NO solve toward a
    # DERIVED quantity, so there is no solve circularity to defend. The §13.2 corroborator
    # (active-gap THIC) would read the IDENTICAL driven cell — a non-independent check. So we
    # do NOT run (and do NOT falsely claim) the independent cross-config gate for that branch;
    # we disclose it honestly as a single-channel direct-authoring read-back. The no-op-switch
    # net for that branch is the S1 set_current_configuration read-back (it RAISES on a silent
    # switch no-op, caught upstream as zoom_write) plus the per-config direct within-tolerance
    # check on each authored distance.
    is_focus_back_dist = (mode == "focus" and plan.get("back_distances") is not None)

    warnings = []
    if is_focus_back_dist:
        # DIRECT-AUTHORING read-back — no independent gate (it would read the driven THIC).
        ind_status = "direct_authoring_readback"
        warnings.append(
            "focus back_distances is a DIRECT-AUTHORING read-back: each config's back-gap "
            "THIC is set to the supplied distance and re-read (achieved==target IS the "
            "proof). There is no solve toward a derived quantity, so the cross-config "
            "independent gate is NOT applied here (it would read the SAME driven THIC); the "
            "no-op-switch net is the set_current_configuration read-back."
        )
    else:
        # §3.5 ordering-gate relaxation (#10): a floored config's achieved == g_floor (the
        # nearest physical), which can BREAK strict-monotone EFL ordering by PHYSICS — so the
        # strict-ordering half is RELAXED (ordering_targets -> None) when ANY config floored.
        # The DISCRIMINATING active-gap-THIC corroborator (independent_values) STILL runs (a
        # floored config sits at the floor, distinct from a non-floored gap), so the no-op-switch
        # collapse net is NOT weakened.
        ordering_targets = (None if nearest_configs
                            else (plan.get("targets") if mode == "zoom" else None))
        if nearest_configs and mode == "zoom":
            warnings.append(
                "the strict cross-config EFL-ordering gate was RELAXED because one or more "
                f"configs floored to the nearest physical gap ({nearest_configs}); their "
                "achieved EFL is the physical-limit nearest, not the requested order. The "
                "independent active-gap-THIC corroborator still ran."
            )
        ind_label = _independent_label(mode)
        # The REQUESTED per-config targets (for the degeneracy test): zoom EFL / conjugate
        # PMAG (`targets`), or None for focus-minimize (degenerate-tolerant — configs may
        # share one best-focus plane).
        request_targets = (plan.get("targets") if mode in ("zoom", "conjugate")
                           else None)
        # §3.5 (#10): a FLOORED config could not reach its requested target — its EFFECTIVE
        # request is the nearest PHYSICAL grading (g_floor). Substitute the achieved nearest
        # for each floored config's request before the degeneracy test, so an ALL-floored set
        # (every config at the same physical floor) is correctly DEGENERATE (a legitimate WARN,
        # not a false collapse-refusal) while a MIXED set keeps the non-floored targets DISTINCT
        # (the active-gap-THIC corroborator still discriminates a real no-op-switch there).
        if nearest_configs and isinstance(request_targets, (list, tuple)):
            eff = list(request_targets)
            for r in per_config:
                if r.get("nearest_achievable"):
                    idx = r["config"] - 1
                    if 0 <= idx < len(eff):
                        eff[idx] = r.get("achievable_efl")
            request_targets = eff
        ind_ok, ind_reason, ind_status = _zs.independent_order_ok(
            ordering_targets, achieved_grading_list(per_config), independent_values,
            independent_label=ind_label, request_targets=request_targets,
        )
        if not ind_ok:
            raise _ZoomUnverified(
                f"the {mode} compose passed the per-config direct check but FAILED the "
                f"INDEPENDENT cross-config gate: {ind_reason}",
                extra={"independent_gate": ind_reason},
            )
        if ind_status == "degenerate":
            warnings.append(ind_reason)

    # STOP-ZOOM (D-4/D-5): the hold_fnum coupling — author the per-config aperture in
    # the SAME checkpoint as the EFL set, falsified TOGETHER on WFNO-per-config (a miss raises
    # _ZoomUnverified -> the checkpointed caller rolls back the aperture AND the THIC matrix).
    fnum_disclosure = None
    if mode == "zoom" and plan.get("hold_fnum"):
        fnum_disclosure = _apply_hold_fnum(session, plan, warnings)

    # §3.4 (#10): a nearest_achievable config keeps the whole set_zoom ok:true (with disclosure
    # — the dogfood wants the design to PROCEED at its physical limit, not be refused). Warn per
    # floored config so the agent sees WHICH target was unreachable + the recovery (a wider gap
    # range / a different zoom surface / a longer track). M-1: the disclosure KEY stays uniform
    # (achievable_efl, the spec-blessed name), but the WARNING prose names the right QUANTITY per
    # mode (EFL for zoom, magnification for conjugate, back-distance for focus) so a focus/
    # conjugate floor is not mislabelled "EFL".
    quantity = {"zoom": "EFL", "conjugate": "magnification (PMAG)",
                "focus": "back-distance"}.get(mode, "value")
    for r in per_config:
        if r.get("nearest_achievable"):
            warnings.append(
                f"config {r['config']}: the target {r['target']} requires a sub-floor "
                f"(negative/overlapping) airgap on surface {gap_surface}; authored the "
                f"NEAREST PHYSICAL gap instead (achievable {quantity} {r['achievable_efl']}, "
                f"shortfall {r['shortfall']} from target). The design is at its physical limit "
                "at this config — widen the gap range (a different zoom surface / a longer "
                "track) to reach this target."
            )

    result = {
        "ok": True,
        "mode": mode,
        "surface": gap_surface,
        "n_configs": n_configs,
        "per_config": per_config,
        # #10 converge-to-nearest: the configs that floored to the nearest physical gap (empty
        # for a fully-reachable zoom). ok stays true; each floored config carries its disclosure.
        "nearest_achievable_configs": nearest_configs,
        # focus-back_distances is a direct-authoring read-back (no independent gate run)
        # -> independent_order_ok is None (honestly "not applicable"), not a false True.
        "independent_order_ok": (None if is_focus_back_dist else True),
        "independent_check": ind_status,
        "active_configuration_restored": current_configuration(system),
        "index_shift": None,
        "checkpoint": None,
        "rolled_back": False,
        "warnings": warnings,
        # hold_fnum disclosure (False when opted out / not mode=zoom). When held, the
        # additive keys (held_fnum, aperture_type_set/_before, wfno/epd_per_config) ride here.
        "hold_fnum": bool(mode == "zoom" and plan.get("hold_fnum")),
    }
    if fnum_disclosure is not None:
        result.update(fnum_disclosure)
    return result


def _apply_hold_fnum(session, plan, warnings):
    """Set ImageSpaceFNum at the target f/# + FALSIFY WFNO per config (D-5/D-6).

    The PROBE-PICKED mechanism: a SINGLE GLOBAL ImageSpaceFNum aperture value holds
    WFNO across every config by construction (the EPD floats per config) — no per-config APER
    row. Sets the system aperture (read-back-as-proof, D-7) INSIDE the checkpoint, then reads
    WFNO per config and asserts ``|WFNO_k - target| <= _FNUM_HOLD_TOL`` for ALL configs (the
    INDEPENDENT first-order quantity, NEVER the APER cell read-back — D-5/D-6). Also asserts
    the per-config EPD DIFFERS (the stop ACTUALLY zoomed; a held f/# off a frozen pupil
    reddens). A miss RAISES ``_ZoomUnverified(family=zoom_fnum_unheld)`` -> the checkpoint
    rolls back the aperture + the THIC matrix. Restores the pre-call active config.
    Returns the disclosure dict.
    """
    system = session.system
    target = plan["fnum"] if plan.get("fnum") is not None else plan.get("current_wfno")

    aperture = system.SystemData.Aperture
    try:
        aperture_type_before = str(aperture.ApertureType)
    except Exception:  # noqa: BLE001 — a diagnostic read; absent -> None
        aperture_type_before = None

    # Resolve + set ImageSpaceFNum at the target f/# (getattr-only enum resolution, the
    # crash-safety precedent). Read-back-as-proof the type + value took.
    enum_type = _zosapi_enum(system, "ZemaxApertureType")
    member = _resolve_enum(enum_type, "ImageSpaceFNum")
    aperture.ApertureType = member
    aperture.ApertureValue = float(target)
    system.UpdateStatus()
    actual_type = str(aperture.ApertureType)
    actual_value = float(aperture.ApertureValue)
    # Exact member compare (the set_ray_aiming `str(member)` convention, not a substring)
    # — read-back-as-proof of the resolved ImageSpaceFNum member, live-confirmed clean.
    if actual_type != str(member) or abs(actual_value - float(target)) > 1e-6:
        raise _ZoomUnverified(
            "could not set the image-space f/number aperture to hold f/#: read back "
            f"type {actual_type!r} value {actual_value!r} (wanted ImageSpaceFNum "
            f"{target}) — refusing rather than claiming an unverified f/# hold",
            family=_ZOOM_FNUM_UNHELD,
        )

    # FALSIFY: WFNO per config (the independent first-order quantity). The EPD = EFL/WFNO
    # is read alongside to prove the pupil actually rescaled per config.
    n_configs = plan["n_configs"]
    wfno_per_config = []
    epd_per_config = []
    for cfg in range(1, n_configs + 1):
        _switch_config(session, cfg)
        wfno = _zs.read_wfno_for_config(system)
        efl = _zs.read_efl_for_config(system)
        wfno_per_config.append(wfno)
        epd_per_config.append(efl / wfno if wfno not in (0, 0.0) else float("nan"))
    _switch_config(session, plan["active_before"])

    bad = [(k + 1, w) for k, w in enumerate(wfno_per_config)
           if abs(w - float(target)) > _FNUM_HOLD_TOL]
    if bad:
        raise _ZoomUnverified(
            f"hold_fnum: the working f/number did NOT hold at {target} across all configs "
            f"after the aperture set (offending {bad}); the aperture did not rescale the "
            "pupil per config (an f/# drift the APER read-back would miss) — rolling back.",
            family=_ZOOM_FNUM_UNHELD,
            extra={"target_fnum": _safe(target),
                   "wfno_per_config": [_safe(w) for w in wfno_per_config]},
        )
    # Corollary (D-5): the EPD must DIFFER per config (a held f/# is not a frozen-pupil
    # artifact — the stop genuinely zoomed). A degenerate (all-equal-EFL) zoom is disclosed.
    finite_epd = [e for e in epd_per_config
                  if isinstance(e, (int, float)) and math.isfinite(e)]
    if len(finite_epd) >= 2 and (max(finite_epd) - min(finite_epd)) < _EPD_DIFFERS_MIN_SPREAD:
        warnings.append(
            "hold_fnum held f/# but the per-config entrance-pupil diameter did NOT vary "
            "(the EFLs are ~identical — a degenerate zoom); the stop did not meaningfully zoom"
        )

    return {
        "held_fnum": _safe(target),
        "aperture_type_set": "ImageSpaceFNum",
        "aperture_type_before": aperture_type_before,
        "wfno_per_config": [_safe(w) for w in wfno_per_config],
        "epd_per_config": [_safe(e) for e in epd_per_config],
    }


def achieved_grading_list(per_config):
    """The ACHIEVED grading vector (for the ordering gate) from the per_config records."""
    out = []
    for rec in per_config:
        ach = rec.get("achieved")
        out.append(ach if isinstance(ach, (int, float)) and not isinstance(ach, bool)
                   else float("nan"))
    return out


def _focus_minimize(session, system, gap_surface, write_fn):
    """Golden-section MINIMIZE the per-config defocus (|REAY|) on the back gap (§4.3).

    H-2 (the gap-floor SIBLING): a back airgap THIC can NEVER be negative (an
    overlapping-element, unmanufacturable system), so the search window's lower bound is
    floored at the physical minimum (``_zs._GAP_FLOOR`` >= 0) AND every golden-section
    candidate is clamped to the floor — symmetric with the secant path's
    ``_clamp_floor_or_refuse``. The same protection the EFL-5 -> THIC-70 live bug fix added
    to the secant path now guards the minimize path too: the committed best-focus THIC is
    ALWAYS >= 0.
    """
    t0 = _read_thic(system, gap_surface)
    floor = _zs._GAP_FLOOR
    lo = max(floor, t0 - _FOCUS_SCAN_HALFWIDTH)
    hi = t0 + _FOCUS_SCAN_HALFWIDTH
    if hi < lo:
        hi = lo
    best_t, best_v = _zs.golden_section_min_defocus(
        system, write_fn, lambda: _zs.read_marginal_for_config(system), lo, hi,
        gap_floor=floor,
    )
    if best_t is None:
        raise _ZoomUnverified(
            f"focus-minimize found no improving best-focus plane in the scan window "
            f"[{lo}, {hi}] on surface {gap_surface}; rolling back rather than shipping an "
            "un-focused config"
        )
    # The minimize NEVER commits a sub-floor (negative) back-gap (H-2): the floored window +
    # the per-candidate clamp guarantee best_t >= floor. A defensive final clamp keeps the
    # invariant explicit even if a future change loosens the search.
    if math.isfinite(best_t) and best_t < floor:
        best_t = floor
    # Park the gap at the best plane (the final write so the re-read sees it).
    write_fn(best_t)
    return best_t, best_v, -1  # -1 iterations = golden-section path (not a secant count)


def _final_grading(system, mode, grading_code, gap_surface, back_distances, cfg):
    """RE-READ the committed grading operand for the ACTIVE config (FALSIFY-not-trust)."""
    if mode == "zoom":
        return _zs.read_efl_for_config(system)
    if mode == "conjugate":
        return _zs.read_pmag_for_config(system)
    # focus
    if back_distances is not None:
        return _read_thic(system, gap_surface)
    return _zs.read_marginal_for_config(system)


def _read_independent(system, mode, gap_surface):
    """Read the DISCRIMINATING INDEPENDENT cross-config quantity for ``mode`` (§13.2).

    The §13.2 tightening (the live-caught false-reject): PMAG/PIMH read 0 on a NORMAL
    infinite-conjugate on-axis system (the live zoom fixture), so they are NOT a reliable
    collapse/distinctness signal there. The robust signal per mode:

    - ``zoom`` / ``focus``: the per-config ACTIVE-gap THIC (the LDE thickness of the solved
      gap). It is INDEPENDENT of the EFFL the solve drove (LDE geometry, not the merit
      operand), holds on an inf-conjugate on-axis system (PMAG/PIMH are 0 there), and is
      distinct whenever the solve produced distinct per-config values — and a no-op
      ``set_current_configuration`` leaves ALL configs reading the SAME active-gap geometry.
    - ``conjugate``: ``PMAG`` (genuinely DIFFERS on a finite-object system — probe §3
      −0.596 vs −1.143; the conjugate solve drives the object gap, so PMAG is the operand it
      did NOT drive directly and reflects the magnification the configs differ on).

    A degraded read returns NaN (the DIFFERS gate ignores NaNs — never fabricates a
    distinctness claim).
    """
    if mode == "conjugate":
        try:
            return _zs.read_pmag_for_config(system)
        except _ZoomUnverified:
            return float("nan")
    # zoom / focus: the per-config active-gap THIC (the discriminating LDE-geometry signal).
    return _zs.read_active_gap_thic(system, gap_surface)


def _independent_label(mode):
    """The human label for the INDEPENDENT corroborator in a gate message (§13.2)."""
    return "PMAG" if mode == "conjugate" else "active-gap THIC"


# --------------------------------------------------------------------------- #
# The ARRAY orchestration (§5).
# --------------------------------------------------------------------------- #
def _compose_array(session, plan):
    """array: author the element-coord rows + DIRECT placement + REAL-RAY falsify (§5).

    Authors the requested ``CADX``/``CADY`` rows on ``first`` and the ``THIC``-Z row on
    ``first-1`` (only the operands a position requests, §5.1), writes each per-config value
    (S1 ``set_config_value``, INVARIANT-1), then per config FALSIFIES: ``beam_reaches_span``
    (reach commit gate) + the RAGX/REAX-DIFFERS bite (the real-ray oracle, NEVER
    GetGlobalMatrix). Discloses ``per_config_image_shift`` + the WARNING (D4). Raises
    ``_ZoomUnverified`` into the checkpointed caller on any miss.
    """
    system = session.system
    first = plan["first"]
    last = plan["last"]
    n_configs = plan["n_configs"]
    positions = plan["positions"]

    authored = []
    row_dx = row_dy = row_z = None
    if plan["wants_dx"]:
        row_dx = _author_element_row(session, "CADX", first)
        authored.append("CADX")
    if plan["wants_dy"]:
        row_dy = _author_element_row(session, "CADY", first)
        authored.append("CADY")
    if plan["wants_z"]:
        row_z = _author_element_row(session, "THIC", plan["z_gap"])
        authored.append("THIC")

    # Author the per-config placement values (DIRECT — no solve).
    for cfg in range(1, n_configs + 1):
        p = positions[cfg - 1]
        if "decenter_x" in p and row_dx is not None:
            _set_element_value(session, row_dx, cfg, p["decenter_x"])
        if "decenter_y" in p and row_dy is not None:
            _set_element_value(session, row_dy, cfg, p["decenter_y"])
        if "z" in p and row_z is not None:
            _set_element_value(
                session, row_z, cfg, _za.z_to_thic(system, plan["z_gap"], p["z"])
            )

    # The bite AXES (H-1 fix): falsify EVERY authored LATERAL decenter axis, not just the
    # dominant one. The PRIMARY oracle per axis is the IMAGE-PLANE landing (REAX for a
    # decenter_x, REAY for a decenter_y) — the reference-INDEPENDENT real ray (probe
    # probe_mce_s2d.py). RAGX global is a SECONDARY corroborator ONLY (reference-surface-
    # dependent — it reads 0 for a DOWNSTREAM decenter, never the gate). A dual-axis array
    # BITES iff EVERY requested lateral axis bites (and tracks its own decenters
    # monotonically). The Z (THIC) axis does NOT move the lateral image landing (a pure-Z
    # displacement shifts focus ALONG the axis), so it cannot be bite-checked via REAX/REAY
    # — Z stays covered by the reach gate (beam_reaches_span) and is disclosed as
    # reach-verified, NOT image-landing-falsified.
    bite_axes = []
    if plan["wants_dx"]:
        bite_axes.append("x")
    if plan["wants_dy"]:
        bite_axes.append("y")

    authored_decenters = {
        axis: _authored_decenter_vec(positions, axis) for axis in bite_axes
    }

    # FALSIFY per config: switch -> reach gate -> read the IMAGE landing for EVERY authored
    # axis (REAX/REAY, the PRIMARY oracle) + RAGX global (the SECONDARY corroborator,
    # disclosed not gated).
    per_config = []
    # Per-axis per-config IMAGE landing vectors (the PRIMARY bite quantity, §5.2).
    image_landing_vecs = {axis: [] for axis in bite_axes}
    rag_x_vec = []          # RAGX global at the element (SECONDARY corroborator only)
    for cfg in range(1, n_configs + 1):
        _switch_config(session, cfg)

        reach = _za.reach_span_all_configs(system, first)
        if not (isinstance(reach, dict) and reach.get("reaches") is True):
            first_miss = reach.get("first_miss") if isinstance(reach, dict) else None
            raise _ZoomUnverified(
                f"array config {cfg}: the placed element's beam does NOT reach the "
                f"downstream optics (beam_reaches_span first_miss={first_miss!r}); the "
                "placement steers the beam off an optic (the §0 class) — rolling back",
                family=_ARRAY_UNREACHED,
                extra={"failing_config": cfg, "first_miss": first_miss},
            )

        landing_x = _za.read_image_landing_x(system) if "x" in bite_axes else None
        landing_y = _za.read_image_landing_y(system) if "y" in bite_axes else None
        if "x" in bite_axes:
            image_landing_vecs["x"].append(landing_x)
        if "y" in bite_axes:
            image_landing_vecs["y"].append(landing_y)
        rag_x = _za.read_ray_global_x(system, first)
        rag_x_vec.append(rag_x)
        per_config.append({
            "config": cfg,
            "placement": dict(positions[cfg - 1]),
            # The PRIMARY real-ray reading (the IMAGE landing on BOTH axes) — the bite proof
            # + the multi-sensor lateral landing the user wants. Disclosed as {x, y} so a
            # consumer can audit EACH authored axis independently (H-1).
            "image_landing": {
                "x": _safe(landing_x) if landing_x is not None else None,
                "y": _safe(landing_y) if landing_y is not None else None,
            },
            # The SECONDARY corroborator (RAGX global) — disclosed, explicitly NOT the
            # gate (reference-surface-dependent; reads 0 for a downstream decenter).
            "ray_global_xyz": [_safe(rag_x), None, None] if rag_x is not None else None,
        })

    # The DIFFERS bite per axis (the PRIMARY oracle = the IMAGE landing, monotone-above-floor
    # vs the authored decenters — NEVER RAGX global / GetGlobalMatrix, §5.2 / OPEN-1 REFINED).
    # The array BITES iff EVERY authored lateral axis bites — a dropped/inert CADX OR CADY
    # is a silent-wrong on its own axis (H-1: the CBDX-class hazard on EITHER axis).
    per_axis_delta = {}
    for axis in bite_axes:
        bites, max_delta, bite_reason = _za.array_placement_bites(
            image_landing_vecs[axis], authored_decenters[axis],
        )
        per_axis_delta[axis] = max_delta
        if not bites:
            raise _ZoomUnverified(
                f"array placement falsifier (axis {axis}): {bite_reason}",
                family=_ARRAY_NO_BITE,
                extra={"failing_axis": axis,
                       "max_image_landing_delta": _safe(max_delta)
                       if max_delta is not None else None},
            )

    # Disclose each channel's image-plane lateral shift relative to config 1 (D4), per axis —
    # the PRIMARY real-ray reading, the multi-sensor channel landing.
    image_shift = {axis: _image_shift(image_landing_vecs[axis]) for axis in bite_axes}
    for i, rec in enumerate(per_config):
        # A config DIFFERS from config 1 iff ANY authored axis's image landing moved.
        differs = False
        for axis in bite_axes:
            vec = image_landing_vecs[axis]
            if (i != 0 and vec[i] is not None and vec[0] is not None
                    and abs(vec[i] - vec[0]) >= _za._ARRAY_BITE_MIN_DELTA):
                differs = True
        rec["differs_from_config1"] = bool(i == 0 or differs)

    _switch_config(session, plan["active_before"])

    return {
        "ok": True,
        "mode": "array",
        "first": first,
        "last": last,
        "n_configs": n_configs,
        "authored_operands": authored,
        "bite_axes": bite_axes,
        "per_config": per_config,
        "rays_reach_all_configs": True,
        "reach_span": [max(0, first - 1), int(system.LDE.NumberOfSurfaces) - 1],
        # The PRIMARY disclosed real-ray reading + bite proof, per axis (the REAX/REAY image
        # landing per config — the reference-independent oracle, the multi-sensor channel
        # shift). BOTH axes disclosed so a consumer audits each authored axis (H-1).
        "per_config_image_landing": {
            axis: [_safe(v) for v in image_landing_vecs[axis]] for axis in bite_axes
        },
        "per_config_image_shift": image_shift,
        "max_image_landing_delta": {
            axis: _safe(per_axis_delta[axis]) for axis in bite_axes
        },
        # The Z axis (THIC) is reach-VERIFIED (beam_reaches_span), NOT image-landing-
        # falsified — a pure-Z displacement shifts focus along the axis, it does NOT move the
        # lateral image landing, so REAX/REAY cannot bite it (H-1 honest disclosure).
        "z_reach_verified_not_bite_falsified": bool(plan.get("wants_z")),
        # RAGX global is a SECONDARY corroborator ONLY (reference-surface-dependent —
        # reads 0 for a downstream decenter; NOT the gate, disclosed for transparency).
        "ray_global_x_secondary": [_safe(v) for v in rag_x_vec],
        "active_configuration_restored": current_configuration(system),
        "index_shift": None,
        "checkpoint": None,
        "rolled_back": False,
        "warning": (
            "each channel images at a DIFFERENT plane unless you ALSO author a per-config "
            "back-focus (mode=focus) — the placement relocates the element but does NOT "
            "co-locate the channels' image planes. set_zoom does not auto-chain "
            "array+focus; call mode=focus separately if a shared sensor is intended."
        ),
    }


def _authored_decenter_vec(positions, axis):
    """The per-config authored decenter on ``axis`` ('x'|'y') for the monotone bite check.

    Returns a per-config list (ONE per config, in order) of the requested decenter_x /
    decenter_y, or ``None`` where a config did not request that axis (treated as no
    ordering constraint at that config). Feeds ``array_placement_bites``'s sign-consistency
    check (the image landing must track the decenter ordering — probe magnitude caveat).
    """
    key = "decenter_x" if axis == "x" else "decenter_y"
    out = []
    for p in positions:
        v = p.get(key) if isinstance(p, dict) else None
        out.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                   else None)
    return out


def _image_shift(image_landing_vec):
    """Each channel's image-plane lateral landing relative to config 1 (D4 disclosure)."""
    if not image_landing_vec or image_landing_vec[0] is None:
        return [None for _ in image_landing_vec]
    base = image_landing_vec[0]
    return [
        _safe(v - base) if v is not None else None
        for v in image_landing_vec
    ]


# --------------------------------------------------------------------------- #
# S1-handler delegation helpers (every cell write goes through the S1 primitive, D6).
# --------------------------------------------------------------------------- #
def _author_thic_row(session, gap_surface):
    """Author the per-config THIC row for the EFL family -> the row handle (raise on refusal).

    Delegates to S1 ``set_config_operand`` (D6) for an interior gap. The CONJUGATE object
    gap is surface 0, which the S1 primitive REFUSES (it forbids OBJECT 0 for a surface-
    bearing operand) — but §3.5 explicitly ALLOWS OBJECT 0 for ``mode=conjugate`` (its gap
    IS the object distance). So a surface-0 gap authors the THIC row via the ``_mce_cells``
    substrate directly (the D6 "reads MAY go direct" extended to this ONE narrow write the
    S1 surface-firewall structurally cannot express), still read-back-proven (the
    ``set_config_operand`` ChangeType + Param read-back idiom). RESOLVED-AMBIGUITY: the S1
    surface-firewall vs the §3.5 OBJECT-0-for-conjugate allowance.
    """
    if gap_surface == 0:
        return _author_object_gap_thic_row(session)
    return _author_row(session, "THIC", gap_surface)


def _author_object_gap_thic_row(session):
    """Author a THIC MCE row on the OBJECT gap (surface 0) directly (conjugate only).

    The S1 ``set_config_operand`` refuses OBJECT 0; the conjugate object-distance gap IS
    surface 0 (§3.5 allowance). Authors via the ``_mce_cells`` substrate (ChangeType +
    Param1=0 read-back-proven, the ``set_config_operand`` recipe), with the orphan-row
    removal on any failure (transactional). Returns the 1-based row handle. Raises
    ``SurfaceWriteError`` on a refusal (rolled back by the checkpointed caller).
    """
    from . import _mce_cells as _mc
    system = session.system
    member = _mc.resolve_member(system, "THIC")
    op = _mc.add_mce_operand(system)
    try:
        row = int(op.OperandNumber)
        _mc.change_operand_type(system, op, member)
        type_readback = str(op.Type)
        if "THIC" not in type_readback:
            raise SurfaceWriteError(
                f"MCE operand row {row} is {type_readback!r} after ChangeType to THIC — "
                "the retype silently no-opped; rolling back rather than authoring the "
                "wrong operand",
                field="operand_type", intended="THIC", actual=type_readback, surface=0,
            )
        _mc.set_param(op, 1, 0)  # Param1 = surface 0 (the object gap), read-back-proven
    except Exception:
        try:
            system.MCE.RemoveOperandAt(int(op.OperandNumber))
        except Exception:  # noqa: BLE001 — best-effort orphan removal
            pass
        raise
    return row


def _author_element_row(session, operand, surface):
    """S1 set_config_operand(operand, surface=...) -> the row handle (raise on refusal)."""
    return _author_row(session, operand, surface)


def _author_row(session, operand, surface):
    res = _mce.set_config_operand(session, {"operand": operand, "surface": surface})
    if not (isinstance(res, dict) and res.get("ok") is True):
        fam = res.get("error_family") if isinstance(res, dict) else None
        err = res.get("error") if isinstance(res, dict) else repr(res)
        raise SurfaceWriteError(
            f"could not author the per-config {operand} row on surface {surface} "
            f"(delegate family={fam!r}: {err}); rolling back rather than authoring a "
            "half-compose",
            field="set_config_operand", intended=operand, actual=fam, surface=surface,
        )
    return res["row"]


def _set_thic_value(session, row, config, thic):
    """S1 set_config_value(row, config, thic) -> the read-back THIC (raise on refusal)."""
    return _set_config_value(session, row, config, thic)


def _set_element_value(session, row, config, value):
    """S1 set_config_value(row, config, value) -> read-back (raise on refusal)."""
    return _set_config_value(session, row, config, value)


def _set_config_value(session, row, config, value):
    res = _mce.set_config_value(
        session, {"row": row, "config": config, "value": float(value)}
    )
    if not (isinstance(res, dict) and res.get("ok") is True):
        fam = res.get("error_family") if isinstance(res, dict) else None
        err = res.get("error") if isinstance(res, dict) else repr(res)
        raise SurfaceWriteError(
            f"could not write the per-config value {value!r} to row {row} config {config} "
            f"(delegate family={fam!r}: {err}); rolling back rather than shipping a "
            "half-authored config",
            field="set_config_value", intended=value, actual=fam,
        )
    return res.get("written_value")


def _switch_config(session, config):
    """S1 set_current_configuration(config) -> raise on refusal (the BITE lever)."""
    res = _mce.set_current_configuration(session, {"config": config})
    if not (isinstance(res, dict) and res.get("ok") is True):
        fam = res.get("error_family") if isinstance(res, dict) else None
        err = res.get("error") if isinstance(res, dict) else repr(res)
        raise SurfaceWriteError(
            f"could not switch to configuration {config} (delegate family={fam!r}: "
            f"{err}); rolling back rather than grading the wrong config",
            field="set_current_configuration", intended=config, actual=fam,
        )
    return res


def _read_thic(system, gap_surface):
    """Read the current THIC (LDE thickness) of ``gap_surface``. Raises on degrade."""
    try:
        return float(system.LDE.GetSurfaceAt(gap_surface).Thickness)
    except Exception as exc:  # noqa: BLE001 — an unreadable THIC -> fail closed
        raise _ZoomUnverified(
            f"could not read the thickness of surface {gap_surface} ({exc!r}); rolling "
            "back rather than grading an unreadable gap"
        ) from exc


# --------------------------------------------------------------------------- #
# §6 rollback + reap + snapshot.
# --------------------------------------------------------------------------- #
def _zoom_pre_snapshot(system, plan=None):
    """A pre-mutation snapshot for the post-restore verify (never raises).

    Captures NumberOfSurfaces + NumberOfConfigurations + the active config index — enough
    that a LoadFile that loaded-but-didn't-restore the TOPOLOGY (the configs/surfaces still
    committed) is CAUGHT — PLUS a CONTENT fingerprint (H-3): the per-config geometry of the
    gap surface(s) the compose is about to mutate (the EFL-family gap THIC per config, or
    the array element/Z gaps). The §6 read-vs-read verify then catches a clean-but-DIDN'T-
    restore LoadFile that left the mutated cell content in place (topology matched, content
    diverged) — the ``place_element`` full-read-vs-read precedent. Returns a dict, or None
    if the topology is unreadable (the verify then treats it conservatively).
    """
    snap = {}
    try:
        snap["n_surfaces"] = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — unreadable count -> partial snapshot
        return None
    try:
        snap["n_configs"] = number_of_configurations(system)
        snap["active_config"] = current_configuration(system)
    except Exception:  # noqa: BLE001 — unreadable MCE -> count-only snapshot
        snap["n_configs"] = None
        snap["active_config"] = None
    # CONTENT fingerprint (H-3): the per-config THIC of the gap surface(s) the compose
    # mutates. A None content (degraded read / no plan) is honest — the verify treats an
    # un-captured content as "topology-only" rather than fabricating a faithful claim.
    surfaces = _gap_surfaces_for_plan(plan)
    snap["content_surfaces"] = surfaces
    snap["content"] = _capture_content_fingerprint(system, surfaces,
                                                   snap.get("n_configs"),
                                                   snap.get("active_config"))
    return snap


def _gap_surfaces_for_plan(plan):
    """The gap surface(s) a plan's compose will mutate (for the content fingerprint, H-3)."""
    if not isinstance(plan, dict):
        return []
    surfaces = []
    mode = plan.get("mode")
    if mode == "array":
        if plan.get("wants_z") and plan.get("z_gap") is not None:
            surfaces.append(plan["z_gap"])
    else:
        gap = plan.get("gap_surface")
        if gap is not None:
            surfaces.append(gap)
    return surfaces


def _capture_content_fingerprint(system, surfaces, n_configs, active_config):
    """Per-config THIC of the compose's gap surface(s) (the H-3 content read). Never raises.

    Reads each gap surface's LDE thickness PER CONFIG (switching the active config to read,
    then RESTORING the pre-call active config), so a clean LoadFile that restored the counts
    but left the mutated per-config cell content in place is caught by the read-vs-read. A
    degraded read / no gap surface / unknown config count -> None (honest: the verify then
    falls back to the topology-only check rather than fabricating a content claim).
    """
    if not surfaces or not isinstance(n_configs, int) or n_configs < 1:
        return None
    content = {}
    try:
        for cfg in range(1, n_configs + 1):
            res = _mce.set_current_configuration(_SessionShim(system), {"config": cfg})
            if not (isinstance(res, dict) and res.get("ok") is True):
                return None
            per_cfg = []
            for surf in surfaces:
                per_cfg.append(float(system.LDE.GetSurfaceAt(surf).Thickness))
            content[cfg] = per_cfg
    except Exception:  # noqa: BLE001 — any content-read fault -> None (topology-only verify)
        content = None
    finally:
        # Restore the pre-call active config (the snapshot read must be side-effect-free).
        if isinstance(active_config, int):
            try:
                _mce.set_current_configuration(_SessionShim(system),
                                               {"config": active_config})
            except Exception:  # noqa: BLE001 — best-effort restore (rollback re-restores)
                pass
    return content


class _SessionShim:
    """A tiny ``session``-shaped wrapper (``.system``) so the snapshot can call the S1
    ``set_current_configuration`` handler (which takes a session arg-0) for the content read.
    """

    def __init__(self, system):
        self.system = system


def _post_restore_problems(system, pre_snapshot):
    """Read the system AFTER LoadFile + compare to the pre-compose snapshot. Never raises.

    Returns a list of mismatch strings (empty == faithful restore). An unreadable
    post-restore state is itself a mismatch (we cannot prove the restore landed).
    """
    if not isinstance(pre_snapshot, dict):
        return ["the pre-compose snapshot was unreadable; the rollback cannot be verified"]
    problems = []
    try:
        n_now = int(system.LDE.NumberOfSurfaces)
    except Exception as exc:  # noqa: BLE001 — unreadable count post-restore -> mismatch
        return [f"could not read the surface count after the rollback ({exc!r})"]
    if n_now != pre_snapshot.get("n_surfaces"):
        problems.append(
            f"surface count after rollback is {n_now}, expected "
            f"{pre_snapshot.get('n_surfaces')} (the LoadFile did not restore the "
            "pre-compose surface count)"
        )
    if pre_snapshot.get("n_configs") is not None:
        try:
            ncfg_now = number_of_configurations(system)
            active_now = current_configuration(system)
        except Exception as exc:  # noqa: BLE001 — unreadable MCE post-restore -> mismatch
            problems.append(
                f"could not read the MCE after the rollback ({exc!r}); the restore "
                "cannot be verified"
            )
            return problems
        if ncfg_now != pre_snapshot.get("n_configs"):
            problems.append(
                f"configuration count after rollback is {ncfg_now}, expected "
                f"{pre_snapshot.get('n_configs')}"
            )
        if active_now != pre_snapshot.get("active_config"):
            problems.append(
                f"active configuration after rollback is {active_now}, expected "
                f"{pre_snapshot.get('active_config')}"
            )
    # CONTENT read-vs-read (H-3): a clean LoadFile that restored the TOPOLOGY but left the
    # mutated per-config gap geometry in place (config/surface counts + active index match,
    # but the cell content diverged) is caught here — the §6 "downstream frame" content half
    # the place_element precedent does. Only compared when a content fingerprint was captured
    # pre-compose (a None content was honestly un-captured -> topology-only).
    content_problems = _post_restore_content_problems(
        system, pre_snapshot, n_configs_now=(
            ncfg_now if pre_snapshot.get("n_configs") is not None else None
        ),
        active_now=(active_now if pre_snapshot.get("n_configs") is not None else None),
    )
    problems.extend(content_problems)
    return problems


def _post_restore_content_problems(system, pre_snapshot, *, n_configs_now, active_now):
    """Compare the per-config gap CONTENT to the pre-compose fingerprint (H-3). Never raises.

    Re-reads the captured gap surface(s)' per-config THIC AFTER the rollback (switching the
    active config to read, then restoring it) and compares to the pre-compose fingerprint. A
    divergence (a clean LoadFile that did NOT restore the per-config cell content) is a
    mismatch -> ``rolled_back:false, partial_state:true``. A None pre-fingerprint (un-captured)
    or an unreadable post-restore content -> no content problem (topology-only verify).
    """
    expected = pre_snapshot.get("content") if isinstance(pre_snapshot, dict) else None
    if not isinstance(expected, dict) or not expected:
        return []  # no fingerprint captured -> topology-only (honest)
    if not isinstance(n_configs_now, int) or n_configs_now < 1:
        return []  # MCE unreadable post-restore (already a topology problem)
    problems = []
    restore_active = active_now if isinstance(active_now, int) else \
        pre_snapshot.get("active_config")
    try:
        for cfg, exp_vals in expected.items():
            if not isinstance(cfg, int) or cfg < 1 or cfg > n_configs_now:
                problems.append(
                    f"config {cfg} from the pre-compose content fingerprint is out of "
                    f"range after the rollback (n_configs={n_configs_now})"
                )
                continue
            res = _mce.set_current_configuration(_SessionShim(system), {"config": cfg})
            if not (isinstance(res, dict) and res.get("ok") is True):
                return problems  # cannot re-read content -> stop (topology already verified)
            # The gap surfaces were captured by their LDE surface numbers, recoverable from
            # the plan-independent fingerprint: re-read at the SAME surfaces. The fingerprint
            # row is positional; we stored the surface numbers in the parallel key below.
            now_vals = _read_fingerprint_row(system, pre_snapshot, cfg)
            if now_vals is None:
                return problems
            for i, (exp, now) in enumerate(zip(exp_vals, now_vals)):
                if not _approx_equal(exp, now):
                    problems.append(
                        f"config {cfg} gap content after rollback is {now} at slot {i}, "
                        f"expected {exp} (the LoadFile restored the topology but NOT the "
                        "per-config gap geometry — the rollback was not faithful)"
                    )
    except Exception:  # noqa: BLE001 — a content re-read fault -> no extra problem
        return problems
    finally:
        if isinstance(restore_active, int):
            try:
                _mce.set_current_configuration(_SessionShim(system),
                                               {"config": restore_active})
            except Exception:  # noqa: BLE001 — best-effort active restore
                pass
    return problems


def _read_fingerprint_row(system, pre_snapshot, cfg):
    """Re-read the captured gap surfaces' THIC for the ACTIVE config (H-3 content re-read)."""
    surfaces = pre_snapshot.get("content_surfaces")
    if not surfaces:
        return None
    out = []
    for surf in surfaces:
        try:
            out.append(float(system.LDE.GetSurfaceAt(surf).Thickness))
        except Exception:  # noqa: BLE001 — an unreadable surface -> abort the content read
            return None
    return out


def _approx_equal(a, b, *, atol=1e-9, rtol=1e-9):
    """A float content match for the rollback fidelity check (H-3)."""
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if not (math.isfinite(fa) and math.isfinite(fb)):
        return repr(a) == repr(b)
    return abs(fa - fb) <= atol + rtol * abs(fb)


def _rollback(system, checkpoint_path, pre_snapshot, *, mode, family, reason,
              extra=None):
    """Restore from the checkpoint + POST-RESTORE read-back verify (never raises).

    A ``LoadFile`` THROW -> ``partial_state:true`` ("reload your .zmx"). A LoadFile that
    returns cleanly but loaded-but-DIDN'T-restore is caught by the post-restore read-vs-
    read -> ``rolled_back:false, partial_state:true``. Only a VERIFIED-faithful restore ->
    ``rolled_back:true``.
    """
    fields = {
        "mode": mode,
        "checkpoint": True,
        "rolled_back": False,
        "partial_state": False,
    }
    if extra:
        fields.update(extra)
    try:
        system.LoadFile(_fwd(checkpoint_path), False)
    except Exception as exc:  # noqa: BLE001 — a rollback LoadFile throw -> partial state
        fields.update({"rolled_back": False, "partial_state": True})
        return error_envelope(
            "set_zoom", family,
            f"set_zoom failed ({reason}); the ROLLBACK restore itself threw ({exc!r}) — "
            "the system is in an UNKNOWN state. Reload your design .zmx to recover.",
            **fields,
        )

    restore_problems = _post_restore_problems(system, pre_snapshot)
    if restore_problems:
        fields.update({"rolled_back": False, "partial_state": True})
        return error_envelope(
            "set_zoom", family,
            f"set_zoom failed ({reason}); the checkpoint LoadFile returned cleanly but "
            f"the post-restore read-back does NOT match the pre-compose snapshot "
            f"({restore_problems}) — the rollback did not faithfully restore. Reload your "
            "design .zmx to recover.",
            **fields,
        )

    fields.update({"rolled_back": True, "partial_state": False})
    return error_envelope(
        "set_zoom", family,
        f"set_zoom failed ({reason}); the system was ROLLED BACK to its pre-compose state "
        "via the temp checkpoint.",
        **fields,
    )


def _reap_checkpoint(checkpoint_path, glob, os, _unlink_quiet):
    """Reap the temp ``.zmx`` + the engine's ``.ZDA`` companion (#59). NEVER raises."""
    if not checkpoint_path:
        return
    _unlink_quiet(checkpoint_path)
    try:
        directory = os.path.dirname(checkpoint_path)
        base = os.path.basename(checkpoint_path)
        stem, _ext = os.path.splitext(base)
        for path in glob.glob(os.path.join(directory, glob.escape(stem) + "*")):
            _unlink_quiet(path)
    except Exception:  # noqa: BLE001 — a reap glob failure never masks the outcome
        pass


def _fwd(path):
    """Forward-slash a path for SaveAs/LoadFile (#73 — a backslash silently mis-writes)."""
    return path.replace("\\", "/") if isinstance(path, str) else path


def _safe(value):
    """JSON-safe float (NaN/inf -> string sentinel)."""
    from .._io import safe_float
    return safe_float(value)


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
SET_ZOOM_SPEC = ToolSpec(
    name="set_zoom",
    handler=set_zoom,
    required_params=("mode",),
    param_types={
        "mode": "string",
        "surface": "number",
        "targets": "array",
        "back_distances": "array",
        "first": "number",
        "last": "number",
        "positions": "array",
        "tolerance": "number",
        "design_name": "string",
        # STOP-ZOOM: hold f/# across the zoom (mode='zoom' only; default ON).
        "hold_fnum": "boolean",
        "fnum": "number",
    },
    description=(
        "Author a per-configuration zoom/focus/conjugate/array over a multi-config "
        "(MCE) system — ONE tool, REQUIRED mode (no default): mode='zoom' solves a chosen "
        "interior airgap (surface) so each config hits its targets EFL; mode='conjugate' "
        "solves the object gap so each config hits its targets PMAG; mode='focus' solves "
        "the back gap to a per-config back_distances OR (absent) minimizes per-config "
        "defocus; mode='array' relocates an element span (first..last) to a per-config "
        "positions list of {decenter_x, decenter_y, z}. Proves zoom/focus/conjugate by "
        "re-reading the grading operand per config AND an independent cross-config "
        "ordering/PMAG-differs gate; proves array by tracing the real chief ray to the "
        "IMAGE plane (its landing moves per config tracking the decenters AND the rays "
        "reach the optics), not by a coordinate read. mode='zoom' holds f/# across the "
        "zoom by DEFAULT (hold_fnum=True): it sets the system aperture to image-space f/# "
        "(the stop zooms so f/# holds while EFL changes), falsified on the working f/number "
        "per config; pass fnum to hold a specific f/#, or hold_fnum=False to opt out. A "
        "per-config target needing a sub-floor (negative/overlapping) airgap converges to the "
        "NEAREST PHYSICAL gap instead of failing — that config is flagged nearest_achievable "
        "with achievable_efl (the grading at the floor) + shortfall, and the whole call stays "
        "ok:true (the design ships at its physical limit). Atomic: "
        "rolls back the whole compose on any REAL miss. Gotcha: needs >= 2 configs "
        "(add_configuration first); array Z needs a preceding airgap (refused on glass); "
        "each array channel images at a different plane unless you also author a "
        "per-config focus. See add_configuration, set_config_value, "
        "set_current_configuration, describe_configurations."
    ),
)

TOOL_SPECS = (SET_ZOOM_SPEC,)
