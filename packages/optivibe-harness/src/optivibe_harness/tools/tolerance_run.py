"""tools/tolerance_run.py — the ``tolerance`` tool (sensitivity / monte_carlo).

ONE dispatchable tool ``tolerance(mode, tolerances?, trials?, full?, output_file?, ...)``,
switched by ``mode={sensitivity, monte_carlo, desensitize, pick_push}`` (mirrors ``optimize``/``dry_run``).
v1 SCALAR perturbations only (radius TRAD / thickness-air TTHI / index TIND / Abbe
TABB), RMS-wavefront criterion (field-summed, in waves) with a nominal Strehl read
ALONGSIDE, default paraxial back-focus compensation.

S2a (``phase2-tool.md``) adds four optional params: ``budget`` (the
engine tolerance WIZARD with element decenter/tilt on, rows READ BACK), ``criterion``
('wavefront' | 'spot'), ``compensator`` ('paraxial' | 'dls', DLS cycle cap 3, needs
``budget``) and ``sampling`` (default 4); every success carries ``run_summary`` and every
sensitivity success ``hotspots[]`` (ranked by element / cemented group / air gap, withheld
unless complete). The default call is today's run at sampling 4.

The lifecycle:

1. Pre-flight (pure, validate-all, D14) — resolve ``mode``; build ``tolerances`` (or
   ``_default_tolerances(system)``); validate EVERY entry (collect all errors). Any
   error -> ``tolerancing_param``, NOTHING opened/authored.
2. LDE snapshot (D16) — capture the compact state vector.
3. TDE author-as-scratch (D10) — ``DeleteAllRows`` (floor-at-1) -> author one row per
   tolerance (``AddOperand``/``ChangeType``/``Param1``/``Min``/``Max``).
4. Open the single slot (D13) — ``with _tolerancing_session(system) as tol:``.
5. Configure — ``SetupMode``/``Criterion`` (getattr-resolved members); for MC
   ``NumberOfRuns``; ``SaveTolDataFile=False`` (D15); ``os.makedirs`` (D15/#58);
   ``OutputFile`` BEFORE the run.
6. Run — ``RunAndWaitForCompletion()``; read ``Succeeded``/``ErrorMessage`` WHILE open
   (engine-success canary, D11); echo ``trials`` from ``NumberOfRuns``.
7. ``Close()`` in ``finally`` (D13) — slot reaped on every path; ``.ZTD``/report
   glob-reap (D15).
8. Parse the UTF-16-LE report (D11 canary; never zero-fill).
9. TDE clear (D10) — ``DeleteAllRows`` in ``finally`` (floor-at-1).
10. LDE re-read + tripwire (D16) — assert byte-equality; set ``state_mutated``.
11. Strehl-alongside (D8) — nominal ``analyze_strehl`` read; null + warn on failure.
12. Build the envelope — never-raise (D17).

Error families: ``tolerancing_param``, ``tolerancing_unavailable``, ``tolerancing_run``,
``tolerancing_parse``.

Live ZOS-API integration: exercised by the live tolerancing test;
unit-tested against the fixture-seeded fakes whose
``RunAndWaitForCompletion`` writes the CAPTURED UTF-16 report to ``OutputFile`` so the
parser runs against REAL engine text.
"""
import functools
import math
import os
import re
import tempfile
import time

from .._io import is_finite_number, safe_call, safe_exc, safe_float
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _layout_geometry as _geom
from . import _lens_state
from . import _run_budget as _rb
from . import _solve_cells as _sc
from . import _tolerance_catalog as _cat
from . import _tol_cells as _cells
from . import _tolerance_common as _tc
from . import _tolerance_layout as _tl
from . import analysis_measure as _am

# The unexpected-engine-throw family (D17): an engine fault that escapes the body net
# degrades to ``tolerancing_run`` (keeps the envelope inside the frozen family set).
_ENGINE_ERROR_FAMILY = "tolerancing_run"

# The merit-layout guard tolerance. Neutralising the temporary layout's boundary
# rows must leave the loaded merit value-neutral within this relative bound, else the candidate cannot be
# judged. Measured: the largest ray-aiming artefact is 7.7e-7 (a double Gauss), the hazard 1.02e-2 (a wizard merit with boundary rows): 1e-5 is 13x over /
# 1000x under.
_MERIT_LAYOUT_REL = 1e-5
_MC_CAP_SETUP_S = 60      # setup (open, wizard, nominal) does not scale with trials; a live 3-trial push was capped at 30 s
# A merit at or above this is the engine's "could not evaluate" sentinel family.
_MERIT_UNUSABLE_AT = 9e9

# What a caller does when the engine may still be running a tolerancing analysis (spec 3.5).
_ENGINE_BUSY_RECOVERY = (
    "the engine may still be running a tolerancing analysis: do not edit this lens in this engine; "
    "release the engine (CloseApplication) and reload the lens in a fresh one")


class _EngineBusy(_tc.ToleranceError):
    """A cancelled capped run whose stop could not be confirmed: the handler must NOT edit the lens or the TDE."""

    def __init__(self, message):
        super().__init__(message, family="tolerancing_run", reason="cap_stop_failed")


# The Min/Max Double cell columns (probe capture: every perturbation op carries
# Min@col6 / Max@col7, ``DataType == "Double"``). Written via _tol_cells.write_double_
# verified (read-back-proven) — NOT the op.Min/op.Max typed property (the v2 cell path).
_MIN_COL = 6
_MAX_COL = 7

# The engine FLOORS ``TDE.NumberOfOperands`` at 1 (the base row survives
# ``DeleteAllRows``; probe section 1, and both test doubles model it). So a PROVEN-empty
# editor reads ``<= 1``, never ``0`` — asserting 0 would refuse every correct clear.
_TDE_EMPTY_FLOOR = 1


def _never_raise(tool_name):
    """Wrap the handler so it NEVER raises past its boundary (D17).

    A ``ToleranceError`` keeps its intended structured ``family`` (one of the four
    frozen families). ANY other ``Exception`` (pythonnet maps every .NET throw onto an
    ``Exception`` subclass) is netted to a typed ``tolerancing_run`` envelope so a
    disconnected engine mid-run becomes ``{ok:false}`` rather than a crash.
    ``RecursionError`` is in the caught set (the wrapper must never raise).
    ``BaseException`` (KeyboardInterrupt / SystemExit) is deliberately NOT caught — it
    propagates AFTER the slot-reaping ``finally`` (D13) has run.

    ROUND-13 -- EVERY READ OF ``exc`` INSIDE THE HANDLER IS GUARDED. This decorator
    IS the never-raise boundary and its f-string interpolated ``exc`` from inside the
    ``except``: an exception whose ``__str__`` throws made the HANDLER itself raise.
    MEASURED — it ESCAPED with ``RuntimeError``, the typed ``tolerancing_*`` family was
    LOST, and the dispatch envelope degraded to the generic ``internal`` family. Three
    reads are routed, not just the f-string: the bare ``str(exc)`` one line up is the
    same defect differently spelled, and ``getattr(exc, "family", ...)`` suppresses only
    ``AttributeError`` — a ``__getattr__`` raising anything else walks straight out
    (defense-in-depth: today's raiser is our own ``ToleranceError``, so that third arm
    needs a hostile subclass, unlike the other two).
    """
    def _decorate(handler):
        @functools.wraps(handler)
        def _wrapped(session, params, **kw):
            try:
                return handler(session, params, **kw)
            except _tc.ToleranceError as exc:
                # S2b: a recipe refusal carries a machine ``reason`` token; every pre-S2b raise has
                # none, so no existing envelope gains a key.
                reason = safe_call(lambda: getattr(exc, "reason", None), None)
                return _ac.error_envelope(
                    tool_name,
                    safe_call(lambda: getattr(exc, "family", "tolerancing"), "tolerancing"),
                    safe_exc(exc),
                    **({} if reason is None else {"reason": reason}),
                )
            except Exception as exc:  # noqa: BLE001 — D17: net any engine throw
                return _ac.error_envelope(
                    tool_name, _ENGINE_ERROR_FAMILY,
                    f"{tool_name} hit an unexpected engine error: {safe_exc(exc)}",
                )
        return _wrapped
    return _decorate


def _resolve_mode(params):
    """Resolve + validate the REQUIRED ``mode`` param (locked D1).

    ROUND-8 -- unbound ``dict.get``, see ``_bool_param``. ``mode`` selects the
    sensitivity / monte_carlo SETUP FORK (``_tc._MODE_TO_SETUP``) — the exact shape of
    the ``optimize_run._resolve_algorithm`` DLS/Hammer fork round 7 swept. Bare base slot
    (no ``isinstance`` conjunct) matches that precedent and keeps today's non-``dict``
    behaviour a raise. Nil reachability today (JSON params + the
    ``server.Dispatcher.call_tool`` non-``dict`` coercion at :517).
    """
    mode = dict.get(params, "mode")
    if mode not in _tc._MODE_TO_SETUP:
        raise _tc.ToleranceError(
            f"mode must be one of {sorted(_tc._MODE_TO_SETUP)}, got {mode!r}",
            family="tolerancing_param",
        )
    return mode


def _resolve_trials(params):
    """Resolve the optional ``trials`` (monte_carlo) — positive int / integral float.

    Default 20 (SIGNATURE). A bool / non-integral float / non-int / ``< 1`` ->
    ``tolerancing_param``. An integral float (a JSON round-trip can float an int) is
    coerced (``20.0`` -> 20).

    ROUND-8 -- unbound ``dict`` slots, see ``_bool_param``. A lying ``__contains__``
    silently runs 20 Monte-Carlo trials for an explicit ``trials=500`` and reports the
    short run as the caller's. Nil reachability today.
    """
    if not (isinstance(params, dict) and dict.__contains__(params, "trials")):
        return _tc._DEFAULT_TRIALS
    value = dict.__getitem__(params, "trials")
    if isinstance(value, bool):
        raise _tc.ToleranceError(
            f"trials must be an integer, not a bool ({value!r})",
            family="tolerancing_param",
        )
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            value = int(value)
        else:
            raise _tc.ToleranceError(
                f"trials must be an integer, got non-integral {value!r}",
                family="tolerancing_param",
            )
    if not isinstance(value, int):
        raise _tc.ToleranceError(
            f"trials must be an integer, got {type(value).__name__} {value!r}",
            family="tolerancing_param",
        )
    if value < 1:
        raise _tc.ToleranceError(
            f"trials must be >= 1, got {value}", family="tolerancing_param"
        )
    return value


def _bool_param(params, key, default):
    """Pull an optional bool param; reject a non-bool (loud).

    ROUND-8 -- MEMBERSHIP AND LOOKUP GO THROUGH THE UNBOUND ``dict``
    SLOTS. Round 7 applied this rule to ``optimize_run``'s copy of this door and left the
    ``optimize_merit`` / ``analysis_measure`` / ``tolerance_run`` copies on ``key not in
    params``, so four copies that used to AGREE started disagreeing. On a ``dict``
    SUBCLASS with a lying ``__contains__`` the door silently substitutes the default;
    measured in the ``optimize_run`` twin, ``require_free_stop=False`` came back **True**.
    Here the three keys are ``full`` / ``strict`` / ``include_mechanical`` — ``strict`` is
    the flag that decides whether a known-gap operand REJECTS the whole tolerance set or
    is merely disclosed, so a defaulted read turns a hard refusal into a soft warning. The
    rule ``_optimize_common.range_headers_supplied`` documents (:2772) is now at every
    copy of the door.

    REACHABILITY IS NIL TODAY AND THAT IS STATED, NOT ASSUMED: ``params`` arrives from
    JSON deserialization (and ``server.Dispatcher.call_tool`` coerces any non-``dict`` to
    ``{}`` at :517), so a ``dict`` subclass is structurally impossible on the shipped
    path. The ``isinstance`` conjunct matches ``optimize_run._bool_param``.
    """
    if not (isinstance(params, dict) and dict.__contains__(params, key)):
        return default
    value = dict.__getitem__(params, key)
    if not isinstance(value, bool):
        raise _tc.ToleranceError(
            f"{key!r} must be a boolean, got {type(value).__name__} {value!r}",
            family="tolerancing_param",
        )
    return value


def _positive_int(params, key, default):
    """A positive int / integral-float param (``bool``, non-integral, non-int, ``< 1`` refused).

    Unbound ``dict`` slots (see ``_bool_param``): a lying ``__contains__`` would
    silently substitute the default for an explicit value. Shares ``_resolve_trials``'s rules
    for the S2a ``sampling`` param (a present ``None`` is refused, like ``trials``).
    """
    if not (isinstance(params, dict) and dict.__contains__(params, key)):
        return default
    value = dict.__getitem__(params, key)
    if isinstance(value, bool):
        raise _tc.ToleranceError(
            f"{key} must be an integer, not a bool ({value!r})", family="tolerancing_param")
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            value = int(value)
        else:
            raise _tc.ToleranceError(
                f"{key} must be an integer, got non-integral {value!r}",
                family="tolerancing_param")
    if not isinstance(value, int):
        raise _tc.ToleranceError(
            f"{key} must be an integer, got {type(value).__name__} {value!r}",
            family="tolerancing_param")
    if value < 1:
        raise _tc.ToleranceError(f"{key} must be >= 1, got {value}", family="tolerancing_param")
    return value


def _resolve_sampling(params):
    """The engine ``CriterionSampling``: integral >= 1, default ``_DEFAULT_SAMPLING`` (4)."""
    return _positive_int(params, "sampling", _tc._DEFAULT_SAMPLING)


def _resolve_choice(params, key, allowed, default):
    """A string param restricted to ``allowed``. ``bool`` / non-str / unknown -> ``tolerancing_param``.

    Membership and lookup go through the unbound ``dict`` slots (see
    ``_bool_param``): a lying ``__contains__`` must not silently pick the default
    criterion / compensator for an explicit request.
    """
    if not (isinstance(params, dict) and dict.__contains__(params, key)):
        return default
    value = dict.__getitem__(params, key)
    if isinstance(value, bool) or not isinstance(value, str) or value not in allowed:
        raise _tc.ToleranceError(
            f"{key} must be one of {sorted(allowed)}, got {value!r}",
            family="tolerancing_param",
        )
    return value


_BUDGET_KEYS = ("vendor", "grade", "element_decenter", "element_tilt")


def _resolve_budget(params):
    """Resolve + validate the optional ``budget`` object -> a normalized dict or ``None``.

    All four keys REQUIRED (``vendor``, ``grade``, ``element_decenter``, ``element_tilt``),
    no others; vendor / grade from the wizard's own universe; the two values finite floats
    > 0 (lens units / degrees, X = Y); ``CellPhoneLens`` is a real preset for ``Generic``
    only (probe r4). Absent -> ``None`` (today's budget paths). Pure: nothing is opened.
    """
    if not (isinstance(params, dict) and dict.__contains__(params, "budget")):
        return None
    raw = dict.__getitem__(params, "budget")
    if not isinstance(raw, dict):
        raise _tc.ToleranceError(
            f"budget must be an object with keys {list(_BUDGET_KEYS)}, got "
            f"{type(raw).__name__} {raw!r}", family="tolerancing_param")
    missing = [k for k in _BUDGET_KEYS if not dict.__contains__(raw, k)]
    unknown = sorted(repr(k) for k in raw if k not in _BUDGET_KEYS)
    if missing or unknown:
        raise _tc.ToleranceError(
            f"budget needs exactly the keys {list(_BUDGET_KEYS)}; missing {missing}, "
            f"unknown {unknown}", family="tolerancing_param")
    out = {}
    problems = []
    for key, universe in (("vendor", _tc._WIZARD_VENDORS), ("grade", _tc._WIZARD_GRADES)):
        value = dict.__getitem__(raw, key)
        if not isinstance(value, str) or value not in universe:
            problems.append(f"budget.{key} must be one of {sorted(universe)}, got {value!r}")
        out[key] = value
    for key in ("element_decenter", "element_tilt"):
        value = dict.__getitem__(raw, key)
        try:
            ok = (not isinstance(value, bool) and isinstance(value, (int, float))
                  and math.isfinite(value) and value > 0)
            number = float(value) if ok else None
        except OverflowError:       # a JSON-legal integer too large for a float (10**400)
            ok, number = False, None
        if not ok:
            problems.append(f"budget.{key} must be a finite number > 0, got {repr(value)[:60]}")
        else:
            out[key] = number
    if not problems and out["grade"] == "CellPhoneLens" and out["vendor"] != "Generic":
        problems.append("budget.grade 'CellPhoneLens' is a real preset for vendor 'Generic' "
                        f"only, not {out['vendor']!r}")
    if problems:
        raise _tc.ToleranceError("; ".join(problems), family="tolerancing_param")
    return out


def _dls_preflight(system):
    """Refuse ``compensator='dls'`` on a lens with no air gap to move at the back.

    The wizard's COMP is the back-focus gap ``n - 2``. A glass surface there (an element
    touching the image, the topology) or a non-finite thickness (an infinite
    image distance) leaves the compensator nothing to move. A ZERO thickness is allowed (the
    zero-thickness air surface after the last glass IS the remedy). Pure LDE reads.
    """
    n = _tc._image_surface_index(system)
    back = None if n is None else n - 1
    if back is None or back < 1:
        raise _tc.ToleranceError(
            "compensator='dls' needs a back-focus air gap, but the LDE surface count could "
            "not be read or has no surface before the image", family="tolerancing_param")
    from . import _layout_geometry as _geom  # the shared air predicate ('' and the CB placeholder '-')
    material = _tc._surface_material(system, back)
    if material is None or not _geom._is_air_material(material):
        raise _tc.ToleranceError(
            f"compensator='dls': the surface before the image (surface {back}) is "
            f"{'unreadable' if material is None else 'glass (' + material.strip() + ')'}; "
            "the back-focus compensator needs an air gap; add a zero-thickness air surface "
            "after the last glass", family="tolerancing_param")
    try:
        thickness = float(system.LDE.GetSurfaceAt(back).Thickness)
    except Exception:  # noqa: BLE001 — unreadable -> fail closed
        thickness = float("nan")
    if not math.isfinite(thickness):
        raise _tc.ToleranceError(
            f"compensator='dls': the back gap (surface {back}) has a non-finite or unreadable "
            "thickness; an infinite image distance has no back-focus gap to move",
            family="tolerancing_param")


# --------------------------------------------------------------------------- #
# compensator="dls_cb_tilt" + cb_tilt={group, range_deg}
# --------------------------------------------------------------------------- #
_CB_TILT_KEYS = ("group", "range_deg")
_CB_GROUP_RE = re.compile(r"^([EG])(\d+)-(\d+)$")
_DLS_NEEDS_BUDGET = (
    "compensator='dls' needs budget={vendor, grade, element_decenter, element_tilt}; "
    "r5: DLS with no compensator row is worse than paraxial. Nothing was opened."
)
_CB_RECOVERY = "reload your saved candidate with load_design"
_CB_DISCLOSURE = (
    "cb_tilt: the DLS re-optimises the nominal onto the CPAR (measured: -0.006 deg, nominal -0.02 %); "
    "the numbers are for the layout with the group tilt compensator, not for the shipped lens "
    "with a back-focus compensator")
_CB_GROUP_KEYS = ("group", "decenter_mm", "tilt_deg")
_CB_GROUP_DISCLOSURE = (
    "cb_group: the DLS re-optimises the nominal onto the CPAR rows (measured on a double Gauss, group 3-5: nominal push -0.0018 / "
    "+0.0038 mm); the numbers are for the layout with the group decenter/tilt compensator, not for the "
    "shipped lens with a back-focus compensator")
_CB_GROUP_TRIAL_UNKNOWN = (
    "cb_group_trial_bounds_unknown: the per-trial Parameter values did not pair one per trial and axis; "
    "the at-bound counts are unknown, so a yield read from per_trial cannot exclude saturated trials")


def _cb_refusal(message, reason, family="tolerancing_param"):
    return _tc.ToleranceError(message, family=family, reason=reason)


def _resolve_cb_tilt(params, compensator, mode, budget, system):
    """Resolve + validate ``cb_tilt`` / ``compensator='dls_cb_tilt'``. ``None`` = not requested.

    Pure LDE / MCE / MFE READS only: nothing is inserted, authored or opened before this returns.
    Rows 1-4 refuse ``tolerancing_param``; rows 5-9 refuse ``tolerancing_run``; every refusal
    carries a machine ``reason``. Evaluation order: shape (2), range (1), budget (3), then the LDE
    rows: the group key's syntax, ``cb_present`` (6), ``multi_config`` (7), the key against the
    CURRENT partition (4), then 5, 8, 9. The coordinate-break and configuration refusals come
    before the partition lookup because they change what the partition means.
    """
    requested = isinstance(params, dict) and dict.__contains__(params, "cb_tilt")
    if compensator != "dls_cb_tilt" and not requested:
        return None
    if requested and compensator != "dls_cb_tilt":
        detail = ("compensator='paraxial' leaves the CPAR rows inert" if compensator == "paraxial"
                  else "use compensator='dls_cb_tilt'")
        raise _cb_refusal(
            f"cb_tilt needs compensator='dls_cb_tilt' ({detail}); nothing was opened",
            "cb_tilt_shape")
    if not requested:
        raise _cb_refusal(
            "compensator='dls_cb_tilt' needs cb_tilt={group, range_deg}; nothing was opened",
            "cb_tilt_shape")
    if mode != "monte_carlo":
        raise _cb_refusal(
            "cb_tilt runs under mode='monte_carlo' only (the sensitivity run would need a "
            "layout-to-original surface remap); nothing was opened", "cb_tilt_shape")
    raw = dict.__getitem__(params, "cb_tilt")
    if not isinstance(raw, dict):
        raise _cb_refusal(
            f"cb_tilt must be an object with keys {list(_CB_TILT_KEYS)}, got "
            f"{type(raw).__name__}; nothing was opened", "cb_tilt_shape")
    unknown = sorted(repr(k) for k in raw if k not in _CB_TILT_KEYS)
    if unknown or not dict.__contains__(raw, "group"):
        raise _cb_refusal(
            f"cb_tilt needs exactly the keys {list(_CB_TILT_KEYS)}; unknown {unknown}, "
            f"missing {[k for k in _CB_TILT_KEYS if not dict.__contains__(raw, k)]}; "
            "nothing was opened", "cb_tilt_shape")
    group = dict.__getitem__(raw, "group")
    if not isinstance(group, str):
        raise _cb_refusal("cb_tilt.group must be a string hotspot key (E<a>-<b> or G<a>-<b>); "
                          "nothing was opened", "cb_tilt_shape")
    rng = dict.get(raw, "range_deg")
    # is_finite_number: bool / non-number -> False BEFORE any float(), a too-large int -> False (no OverflowError)
    if not is_finite_number(rng) or rng <= 0:
        raise _cb_refusal(
            "cb_tilt.range_deg must be a finite number > 0 (degrees); the range is mechanical, "
            "so there is no default (MC saturates +-0.1 deg where sensitivity showed 0.063 deg); "
            "nothing was opened", "range_required")
    if budget is None:
        raise _tc.ToleranceError(_DLS_NEEDS_BUDGET, family="tolerancing_param")
    spec = _cb_lde_rows(system, group.strip())
    return {"name": "cb_tilt", **spec, "range_deg": float(rng), "pars": {3: float(rng), 4: float(rng)}}


def _resolve_cb(params, compensator, mode, budget, system):
    """Resolve the group compensator request: ``cb_group`` / ``dls_cb_group`` or ``cb_tilt`` / ``dls_cb_tilt``.

    ``None`` = not requested. A legacy call (neither ``cb_group`` nor ``dls_cb_group``) reaches
    ``_resolve_cb_tilt`` untouched, so its refusals, messages and order are today's.
    """
    has_group = isinstance(params, dict) and dict.__contains__(params, "cb_group")
    if has_group or compensator == "dls_cb_group":
        return _resolve_cb_group(params, compensator, mode, budget, system, has_group)
    return _resolve_cb_tilt(params, compensator, mode, budget, system)


def _cb_group_range(raw, key, unit):
    """One present ``cb_group`` range: a finite number > 0, else ``range_required`` (no default, ``None`` refuses)."""
    value = dict.__getitem__(raw, key)
    if not is_finite_number(value) or value <= 0:
        raise _cb_refusal(
            f"cb_group.{key} must be a finite number > 0 ({unit}); the range is mechanical, so there "
            "is no default; nothing was opened", "range_required")
    return float(value)


def _resolve_cb_group(params, compensator, mode, budget, system, has_group):
    """Resolve + validate ``cb_group={group, decenter_mm?, tilt_deg?}`` / ``compensator='dls_cb_group'``.

    Pure reads, like ``_resolve_cb_tilt``. Order: the two cross checks, shape, ranges,
    budget (3), then the LDE rows (4-9, ``_cb_lde_rows``). Shape refusals carry reason ``cb_group_shape``.
    """
    if has_group and compensator != "dls_cb_group":
        detail = ("compensator='paraxial' leaves the CPAR rows inert" if compensator == "paraxial"
                  else "dls_cb_tilt takes cb_tilt" if compensator == "dls_cb_tilt"
                  else "use compensator='dls_cb_group'")
        raise _cb_refusal(f"cb_group needs compensator='dls_cb_group' ({detail}); nothing was opened",
                          "cb_group_shape")
    if isinstance(params, dict) and dict.__contains__(params, "cb_tilt"):
        raise _cb_refusal("compensator='dls_cb_group' takes cb_group, not cb_tilt; nothing was opened",
                          "cb_group_shape")
    if not has_group:
        raise _cb_refusal(
            "compensator='dls_cb_group' needs cb_group={group, decenter_mm?, tilt_deg?}; nothing was opened",
            "cb_group_shape")
    if mode != "monte_carlo":
        raise _cb_refusal(
            "cb_group runs under mode='monte_carlo' only (the sensitivity run would need a "
            "layout-to-original surface remap); nothing was opened", "cb_group_shape")
    raw = dict.__getitem__(params, "cb_group")
    if not isinstance(raw, dict):
        raise _cb_refusal(
            f"cb_group must be an object with keys {list(_CB_GROUP_KEYS)}, got "
            f"{type(raw).__name__}; nothing was opened", "cb_group_shape")
    unknown = sorted(repr(k) for k in raw if k not in _CB_GROUP_KEYS)
    has_range = dict.__contains__(raw, "decenter_mm") or dict.__contains__(raw, "tilt_deg")
    if unknown or not dict.__contains__(raw, "group") or not has_range:
        raise _cb_refusal(
            f"cb_group takes the keys {list(_CB_GROUP_KEYS)} (group required, and at least one of "
            f"decenter_mm, tilt_deg); unknown {unknown}, group "
            f"{'present' if dict.__contains__(raw, 'group') else 'missing'}, ranges "
            f"{'present' if has_range else 'missing'}; nothing was opened", "cb_group_shape")
    group = dict.__getitem__(raw, "group")
    if not isinstance(group, str):
        raise _cb_refusal("cb_group.group must be a string hotspot key (E<a>-<b> or G<a>-<b>); "
                          "nothing was opened", "cb_group_shape")
    decenter = (_cb_group_range(raw, "decenter_mm", "lens units")
                if dict.__contains__(raw, "decenter_mm") else None)
    tilt = _cb_group_range(raw, "tilt_deg", "degrees") if dict.__contains__(raw, "tilt_deg") else None
    if budget is None:
        raise _tc.ToleranceError(_DLS_NEEDS_BUDGET, family="tolerancing_param")
    spec = _cb_lde_rows(system, group.strip(), "cb_group")
    pars = {}
    if decenter is not None:
        pars.update({1: decenter, 2: decenter})
    if tilt is not None:
        pars.update({3: tilt, 4: tilt})
    return {"name": "cb_group", **spec, "pars": pars, "decenter_mm": decenter, "tilt_deg": tilt}


def _multi_config_refusal(system, name):
    """Refuse a multi-configuration lens (spec 3.1 row 10; V8): ``NumberOfConfigurations > 1`` or any ``CONF`` row.

    Extracted verbatim from ``_cb_lde_rows`` (messages and order unchanged; ``name`` is the param the messages speak
    of). Pure reads: it must run BEFORE any ``CalculateMeritFunction``, which on a multi-config lens without a
    leading ``CONF`` row inserts one and deletes the last row (V8). Raises ``tolerancing_run`` / ``multi_config``.
    """
    try:
        n_conf = int(system.MCE.NumberOfConfigurations)
        conf_row = any(_lens_state._tok(system.MFE.GetOperandAt(i).TypeName) == "CONF"
                       for i in range(1, int(system.MFE.NumberOfOperands) + 1))
    except Exception as exc:  # noqa: BLE001 -- unreadable configuration state: fail closed
        raise _cb_refusal(f"{name} could not read the configuration state ({safe_exc(exc)}); "
                          "nothing was inserted", "multi_config", family="tolerancing_run")
    if n_conf > 1 or conf_row:
        raise _cb_refusal(f"{name} refuses a multi-configuration lens (folded and zoom lenses get "
                          "no group layout); nothing was inserted", "multi_config",
                          family="tolerancing_run")


def merit_has_tolr(system):
    """Refuse a merit that holds a ``TOLR`` row (spec 3.1 row 11; V17): every MC trial would run a nested sensitivity.

    Returns ``None`` when there is none; raises ``tolerancing_param`` / ``merit_has_tolr``. An unreadable merit
    cannot be scanned, so it is refused as ``merit_unusable`` (fail closed).
    """
    try:
        found = any(_lens_state._tok(system.MFE.GetOperandAt(i).TypeName) == "TOLR"
                    for i in range(1, int(system.MFE.NumberOfOperands) + 1))
    except Exception as exc:  # noqa: BLE001 -- an unreadable merit proves nothing: fail closed
        raise _tc.ToleranceError(
            f"the merit function could not be scanned for a TOLR row ({safe_exc(exc)}); nothing was opened",
            family="tolerancing_param", reason="merit_unusable")
    if found:
        raise _tc.ToleranceError(
            "the merit function holds a TOLR row: every Monte Carlo trial would run a nested sensitivity "
            "analysis; remove it (or judge on a merit without it); nothing was opened",
            family="tolerancing_param", reason="merit_has_tolr")


def merit_unusable(system):
    """The loaded merit value (spec 3.1 row 12), or ``tolerancing_param`` / ``merit_unusable``.

    Non-finite, <= 0 or >= 9e9 refuses: a zero merit has no spread to judge and 9e9 is the engine's
    could-not-evaluate value. Evaluates the merit once (``CalculateMeritFunction``); call AFTER the multi-config
    refusal. Returns the float, which the layout guard (3.4) and the picker keep as ``merit_before``.
    """
    try:
        value = float(system.MFE.CalculateMeritFunction())
    except Exception as exc:  # noqa: BLE001
        raise _tc.ToleranceError(
            f"the merit function could not be evaluated ({safe_exc(exc)}); nothing was opened",
            family="tolerancing_param", reason="merit_unusable")
    if not (math.isfinite(value) and 0 < value < _MERIT_UNUSABLE_AT):
        raise _tc.ToleranceError(
            f"the loaded merit function reads {value!r}: it must be finite, > 0 and < 9e9 to judge a lens on; "
            "nothing was opened", family="tolerancing_param", reason="merit_unusable")
    return value


def merit_preflight(system):
    """Rows 10-12 of spec 3.1 in order (multi-config, TOLR, usable value); returns ``merit_before``. One definition."""
    _multi_config_refusal(system, "criterion='merit'")
    merit_has_tolr(system)
    return merit_unusable(system)


_ELEMENT_ROW_TYPES = frozenset({"TEDX", "TEDY", "TETX", "TETY"})


def _merit_dmfs_guard(system, tde_rows):
    """Refuse a merit run that would read the tolerancer's regenerated default block (owner B).

    With a ``DMFS`` row in the merit (any weight, any position) AND an element ``TEDX/TEDY/TETX/TETY`` row in the
    TDE that will run, every trial is scored on the user's rows PLUS a perturbation-independent block, so the
    statistics are not the user's merit. Pure reads: the MFE row TYPES only (no ``CalculateMeritFunction``)
    and the TDE rows already read back. Call AFTER the TDE is written, BEFORE ``OpenTolerancing``. No mode is
    passed: a future merit-criterion sensitivity path hits the same call. Raises ``tolerancing_param`` /
    ``merit_dmfs_with_element_tolerances``; an unreadable merit is ``merit_unusable`` (fail closed).
    """
    if not any(r.get("type") in _ELEMENT_ROW_TYPES for r in tde_rows):
        return
    try:
        has_dmfs = any(_lens_state._tok(system.MFE.GetOperandAt(i).TypeName) == "DMFS"
                       for i in range(1, int(system.MFE.NumberOfOperands) + 1))
    except Exception as exc:  # noqa: BLE001 -- an unreadable merit proves nothing: fail closed
        raise _tc.ToleranceError(
            f"the merit function could not be scanned for a DMFS row ({safe_exc(exc)}); nothing was opened",
            family="tolerancing_param", reason="merit_unusable")
    if has_dmfs:
        raise _tc.ToleranceError(
            "the merit function holds a DMFS row and the tolerance set has element decenter/tilt rows "
            "(TEDX/TEDY/TETX/TETY): the tolerancer scores each trial on a regenerated default merit block as "
            "well, so the result is not your merit. Delete the DMFS row, or judge on an authored merit "
            "without one; nothing was opened",
            family="tolerancing_param", reason="merit_dmfs_with_element_tolerances")


def _merit_layout_guard(system, ctx, m0):
    """Neutralise the layout's boundary rows and prove the merit value-neutral.

    Sets ``ctx.neutralised`` (the rows CHANGED from nonzero weight to 0: ``merit_neutralised_rows``). Raises
    ``tolerancing_run`` / ``merit_layout_mismatch`` when the merit moved by more than ``_MERIT_LAYOUT_REL``; the
    handler's teardown then restores the weights from the pre-build snapshot.
    """
    lay = ctx.layout
    touched = {lay["entry_cb"], lay["group"][1], lay["dummy_back"], lay["return_cb"], lay["dummy_forward"]}
    ctx.neutralised = _tl.neutralise_boundary(system, touched)
    m1 = float(system.MFE.CalculateMeritFunction())
    ctx.merit_layout_rel = abs(m1 - m0) / abs(m0) if math.isfinite(m1) else None     # None = non-finite (JSON-safe)
    if not (math.isfinite(m1) and abs(m1 - m0) <= _MERIT_LAYOUT_REL * abs(m0)):
        rel = format(ctx.merit_layout_rel, ".3g") if ctx.merit_layout_rel is not None else "a non-finite amount"
        raise _tc.ToleranceError(
            f"merit_layout_mismatch: the merit changed by {rel} when the layout went in (limit "
            f"{_MERIT_LAYOUT_REL:g}), so this candidate cannot be judged on this merit. That is more than a "
            "ray-aiming artefact can explain; OPDX and boundary rows are never layout-neutral and are the usual cause",
            family="tolerancing_run", reason="merit_layout_mismatch")


def _cb_lde_rows(system, group, name="cb_tilt"):
    """On the CURRENT lens: the group is a real, air-bounded, movable group.

    ``name`` ("cb_tilt" | "cb_group") is the param the messages speak of; with the default every string is
    today's. Returns ``{group, kind, a, b}``; the caller adds its ranges.
    """
    m = _CB_GROUP_RE.match(group)
    if m is None:
        raise _cb_refusal(
            f"{name}.group {group!r} is not a hotspot key E<a>-<b> or G<a>-<b>", "group_unknown")
    kind, a, b = m.group(1), int(m.group(2)), int(m.group(3))
    hot = _hot_module()
    topo = hot.read_topology(system)
    n = len(topo)
    if any(_geom._is_coordinate_break(t["type_name"]) for t in topo):
        raise _cb_refusal(
            f"{name} needs a lens with no coordinate break: the layout is built from a pair and "
            "the hotspot partition reads CBs as non-glass; nothing was inserted", "cb_present",
            family="tolerancing_run")
    _multi_config_refusal(system, name)
    groups = _geom.glass_groups(topo, n)
    elements = _geom.element_partition(groups)
    keys = {hot._element_key(e[0], e[1]): e for e in elements if len(e) == 2}
    keys.update({hot._group_key(g): g for g in groups if len(g) >= 3})
    if group not in keys:
        raise _cb_refusal(
            f"{name}.group {group!r} is not an element or cemented-group key of the current "
            f"lens (valid: {sorted(keys)})", "group_unknown")
    members = keys[group]
    if (members[0], members[-1]) != (a, b):
        raise _cb_refusal(f"{name}.group {group!r} does not span surfaces {a}..{b}", "group_unknown")
    try:
        stop = int(system.LDE.StopSurface)
    except Exception:  # noqa: BLE001
        stop = None
    if stop is None or a <= stop <= b:
        raise _cb_refusal(
            f"{name}.group {group!r} spans the stop (surface {stop}); "
            f"{'moving' if name == 'cb_group' else 'tilting'} the stop group "
            "re-aims the aperture; nothing was inserted", "group_spans_stop",
            family="tolerancing_run")
    if kind == "E":
        parent = hot._parent_of([a, b], groups)
        if parent is not None:
            raise _cb_refusal(
                f"{name}.group {group!r} is an element inside the cemented group {parent}; "
                f"{'use' if name == 'cb_group' else 'tilt'} the whole group ({parent}); nothing was inserted",
                "group_not_air_bounded",
                family="tolerancing_run")
    before_air = a >= 1 and _geom._is_air_material(topo[a - 1]["material"])
    behind_air = b < n and _geom._is_air_material(topo[b]["material"])
    if not (before_air and behind_air):
        raise _cb_refusal(
            f"{name}.group {group!r} is not air-bounded (glass "
            f"{('before surface %d' % a) if not before_air else ('behind surface %d' % b)}); "
            "nothing was inserted", "group_not_air_bounded", family="tolerancing_run")
    if b >= n - 2:
        raise _cb_refusal(
            f"{name}.group {group!r} ends at surface {b}, at or past the back-focus gap "
            f"(surface {n - 2}); nothing was inserted", "group_touches_back_gap",
            family="tolerancing_run")
    solve = _sc.read_solve_type(system.LDE.GetSurfaceAt(b).ThicknessCell)
    if solve not in ("Fixed", "Variable"):
        raise _cb_refusal(
            f"{name}.group {group!r}: the thickness behind surface {b} carries a {solve!r} "
            "solve; the layout rewrites it; nothing was inserted", "group_thickness_solved",
            family="tolerancing_run")
    return {"group": group, "kind": kind, "a": a, "b": b}


def _hot_module():
    # lazy, like ``_compute_hotspots``: the hotspots module is pure and engine-free
    from . import _tolerance_hotspots as _hot
    return _hot


class _CbRun:
    """The mutable record of one ``dls_cb_tilt`` layout build: what to undo, what to prove it against."""

    def __init__(self, spec):
        self.spec = spec
        self.undo = []
        self.found = None          # state_with_mce before the first mutation
        self.mfe0 = None           # mfe_snapshot before the first mutation
        self.layout = None
        self.zero_tilt_max_abs = None
        self.effective_range_deg = None
        self.effective = None      # cb_group: {par: read-back bound}
        self.layout_tde_rows = None
        self.restore = None        # filled by _cb_teardown
        self.neutralised = None    # merit only: the boundary rows the layout guard zeroed (spec 3.4)
        self.merit_layout_rel = None   # merit only: the guard's measured relative merit move (None = non-finite)


def _cb_build(session, ctx):
    """Capture ``found`` + the frames, insert the CB layout, prove it faithful at zero tilt."""
    system = session.system
    a, b = ctx.spec["a"], ctx.spec["b"]
    ctx.mfe0 = _tl.mfe_snapshot(system)     # BEFORE ``found``: a fault here leaves ``found`` None (nothing touched)
    try:
        ctx.found = _tl.state_with_mce(system, allow_no_variables=True)
    except _lens_state.StateUnreadable as exc:
        raise _tc.ToleranceError(
            f"cb_tilt could not read the lens state to prove a restore ({exc}); nothing was inserted",
            family="tolerancing_run", reason="state_unreadable")
    n0 = int(system.LDE.NumberOfSurfaces)
    g_img0, g_next0 = _tl.global_matrix(system, n0 - 1), _tl.global_matrix(system, b + 1)
    try:
        ctx.layout = _tl.insert_group_pair(session, a, b, ctx.undo)
        n1 = ctx.layout["n_surfaces"]
        ctx.zero_tilt_max_abs = _tl.zero_tilt_check(
            system, {n1 - 1: g_img0, ctx.layout["dummy_forward"] + 1: g_next0})
    except _tl.LayoutError as exc:
        raise _tc.ToleranceError(str(exc), family=exc.family, reason=exc.reason)


def _author_cpar_rows(system, cb1, pars):
    """One CPAR row per ``{par: range}`` on the entry CB, in ``sorted(pars)`` order, ``[-range, +range]``.

    Par# 1 / 2 = decenter X / Y (lens units), 3 / 4 = tilt X / Y (degrees) (P-C ``add_cpar``).
    """
    tde = system.TDE
    member = _tc._resolve_tol_enum(_tc._tolerance_operand_enum(system), "CPAR")
    meta = _cat.meta_for("CPAR")
    for par in sorted(pars):
        op = tde.AddOperand()
        if not bool(op.ChangeType(member)):
            raise _tc.ToleranceError("the engine rejected operand type CPAR during authoring",
                                     family="tolerancing_run", reason="cpar_not_written")
        for cell_spec, value in _cat.int_cell_writes(meta, {"type": "CPAR", "surface": cb1, "param": par}):
            _cells.write_int_cell(op, cell_spec, value)
        _cells.write_double_verified(op, _MIN_COL, "Min", -float(pars[par]))
        _cells.write_double_verified(op, _MAX_COL, "Max", float(pars[par]))


def _cb_layout_tde_rows(rows, layout):
    """How many of the wizard's rows sit on one of the five inserted / zeroed surfaces."""
    touched = {layout["entry_cb"], layout["group"][1], layout["dummy_back"], layout["return_cb"],
               layout["dummy_forward"]}
    n = 0
    for r in rows:
        if r["type"] == "CPAR":
            continue
        by = _tc._row_cells_by_header(r)
        surfaces = {by.get(h) for h in ("Surf", "Surf1", "Surf2", "Adjust")}
        if surfaces & touched:
            n += 1
    return n


def _cb_teardown(system, ctx):
    """Remove the layout, re-mark variables, re-impose the merit, PROVE the lens restored.

    Never raises. Returns ``{restored, steps_run, failed_step, n_differences, first_differences,
    mfe_reimposed_writes}``. The proof runs even when an earlier step failed (it then reports what
    differs); ``restored`` is true only when every step ran AND the proof passed.
    """
    steps, failed, writes = [], None, 0
    ok, failed_step, ran = _tl.run_undo(ctx.undo)
    steps.extend(ran)
    if not ok:
        failed = failed_step
    if ctx.found is None:       # nothing was mutated: the build never captured a baseline
        return {"restored": failed is None, "steps_run": steps, "failed_step": failed,
                "n_differences": 0, "first_differences": [], "mfe_reimposed_writes": 0}
    if failed is None:
        try:
            _tl.remark_variables(system, ctx.found)
            steps.append("remark_variables")
        except Exception:  # noqa: BLE001 -- named, then the proof reports what differs
            failed = "remark_variables"
    try:
        res = _tl.reimpose_mfe(system, ctx.mfe0)
        writes = res["n_writes"]
        steps.append("reimpose_mfe")
        if not res["restored"] and failed is None:
            failed = "reimpose_mfe"
    except Exception:  # noqa: BLE001
        if failed is None:
            failed = "reimpose_mfe"
    try:
        proof = _tl.diff_allowed(ctx.found, _tl.state_with_mce(system, allow_no_variables=True), allow_dofs=False)
    except Exception:  # noqa: BLE001 -- an unreadable state proves nothing: not restored
        return {"restored": False, "steps_run": steps, "failed_step": failed or "proof_unreadable",
                "n_differences": None, "first_differences": [], "mfe_reimposed_writes": writes}
    steps.append("proof")
    return {"restored": failed is None and proof["pass"], "steps_run": steps, "failed_step": failed,
            "n_differences": proof["n_differences"], "first_differences": proof["first_differences"],
            "mfe_reimposed_writes": writes}


def _resolve_output_path(session, params):
    """Resolve the report path (SIGNATURE / D15): explicit or a workspace temp.

    An explicit ``output_file`` (a non-empty str) is used as-is. Otherwise a workspace-
    resolved temp report path is built (a ``tempfile.mkstemp`` under the OS temp dir —
    reaped in ``finally``). The parent dir is ``makedirs``'d by the caller BEFORE the
    run (D15/#58: the engine SILENTLY writes nothing to a missing dir).
    """
    explicit = dict.get(params, "output_file")  # unbound base slot (see _bool_param)
    if explicit is not None:
        if not isinstance(explicit, str) or explicit.strip() == "":
            raise _tc.ToleranceError(
                f"output_file must be a non-empty string when provided, got {explicit!r}",
                family="tolerancing_param",
            )
        return os.path.abspath(explicit), False
    # A workspace-resolved temp report. mkstemp creates the (empty) file; the engine
    # overwrites it. The handler reaps it in finally (it is a tool-written temp).
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="optivibe_tol_")
    os.close(fd)
    return path, True


def _clear_tde(tde):
    """Clear the TDE via ``DeleteAllRows``, READ-BACK-PROVEN (floor-at-1; D10).

    Guarded: a clear THROW raises ``ToleranceError(family="tolerancing_run")`` so the
    handler nets it (the slot/TDE reaping happens in the caller's ``finally``).

    ROUND-13 -- THE CLEAR IS NOW PROVEN, NOT TRUSTED. Every CELL write on this same
    authoring path is read-back-proven (``_tol_cells.write_verified_cell`` /
    ``write_double_verified``, D6), yet the operation establishing the PRECONDITION for
    all of them — an empty editor — trusted the return of a void engine call. That is
    the ``AddCatalog`` / ``ScaleByUnits`` class: a clean call is not a
    proof. A SILENT no-op leaves the PREVIOUS run's scratch rows in place, this run
    authors after them, and the reconcile canary keys on
    ``(type, surface[, param])`` — so a leftover row of a type this run also authors is
    INDISTINGUISHABLE from this run's own, and the envelope still reports
    ``tde_cleared:true``. ``tde_cleared`` was honest on a throw and not on a no-op.

    The engine floors ``NumberOfOperands`` at 1 (the base row, probe section 1), so "empty" is
    ``<= _TDE_EMPTY_FLOOR``. An UNREADABLE count refuses rather than proceeding (
    absent would be "not applicable"; unreadable is UNKNOWN, and an unproven clear is
    exactly the indeterminate TDE this function already refuses to author onto).
    """
    try:
        tde.DeleteAllRows()
    except Exception as exc:  # noqa: BLE001 — a clear THROW -> tolerancing_run
        raise _tc.ToleranceError(
            f"clearing the Tolerance Data Editor threw "
            f"({safe_exc(exc, repr_form=True)}); refusing to author onto an "
            "indeterminate TDE",
            family="tolerancing_run",
        )
    try:
        remaining = int(tde.NumberOfOperands)
    except Exception as exc:  # noqa: BLE001 — the PROOF is unreadable -> refuse
        raise _tc.ToleranceError(
            "the Tolerance Data Editor row count could not be read after DeleteAllRows "
            f"({safe_exc(exc, repr_form=True)}); the clear is UNPROVEN — refusing to "
            "author onto an indeterminate TDE",
            family="tolerancing_run",
        )
    if remaining > _TDE_EMPTY_FLOOR:
        raise _tc.ToleranceError(
            "DeleteAllRows returned cleanly but the Tolerance Data Editor still holds "
            f"{remaining} rows (expected at most {_TDE_EMPTY_FLOOR}, the engine's "
            "floored base row); the clear SILENTLY no-opped — refusing to author onto "
            "leftover scratch rows",
            family="tolerancing_run",
        )


def _author_tolerance_set(tde, operand_enum, authored):
    """Author one TDE row per tolerance, operand-AGNOSTIC (D5/D8 — table-driven).

    ``AddOperand()`` -> ``ChangeType(_resolve_tol_enum(...))`` -> walk
    ``int_cell_writes(meta, entry)`` (the ordered ``(CellSpec, int)`` list the catalog
    derives from the resolved entry: Surf/Surf1 from ``surface``, Surf2 from
    ``surface2``, RollSurf from ``roll_surf``, Code/Par# from ``code``/``param``) writing
    each through ``_tol_cells.write_verified_cell`` (cell.DataType Int/Double
    discriminator + Header-match + read-back proof, D6). For a ``has_minmax`` operand the
    Min/Max are written via ``_tol_cells.write_double_verified`` into the Min@col6 / Max@col7
    Double CELLS (``_MIN_COL`` / ``_MAX_COL``, DataType Double, read-back-proven) — NOT the
    ``op.Min``/``op.Max`` typed property (the v2 cell path). A control/compensator/structural
    op (no ``min``/``max`` in its validated entry) authors its int cells ONLY (no perturbation).

    The compensator cells are authored for ``.zmx`` fidelity but the run discloses
    ``compensator_participates:false`` (D13/G-COMP-INERT). NO per-token code — the
    catalog metadata drives every write (operand-agnostic).

    A write that does not read back -> a raised error (the silent-no-op trap, D6); the
    handler nets it to ``tolerancing_run``. A non-list/empty ``authored`` cannot reach
    here (the pre-flight floors it).
    """
    for entry in authored:
        token = entry["type"]
        meta = _cat.meta_for(token)
        member = _tc._resolve_tol_enum(operand_enum, token)
        op = tde.AddOperand()
        if not bool(op.ChangeType(member)):
            raise _tc.ToleranceError(
                f"the engine rejected operand type {token} during authoring",
                family="tolerancing_run",
            )
        # (a) the int cells (Surf/Surf1/Surf2/RollSurf/Code/Par#) — table-driven, each
        # read-back-verified via _tol_cells (cell.DataType discriminator + Header-match).
        # int_cell_writes raises CatalogResolveError if a required cell is missing; the
        # validator already guards that, so it surfaces as tolerancing_run if it ever does.
        try:
            writes = _cat.int_cell_writes(meta, entry)
        except _cat.CatalogResolveError as exc:
            raise _tc.ToleranceError(
                f"could not resolve the int-cell author plan for {token} "
                f"({safe_exc(exc)}); the "
                "entry is missing a required cell",
                family="tolerancing_param",
            )
        for cell_spec, value in writes:
            _cells.write_int_cell(op, cell_spec, value)
        # (b) the ±perturbation channel (Min/Max Double) — only for has_minmax operands.
        if entry.get("min") is not None or entry.get("max") is not None:
            _cells.write_double_verified(op, _MIN_COL, "Min", float(entry["min"]))
            _cells.write_double_verified(op, _MAX_COL, "Max", float(entry["max"]))



def _strehl_nominal(session):
    """Read ONE nominal Strehl number ALONGSIDE (D8). NEVER fails the run.

    Calls the existing ``analyze_strehl`` path with ``best_focus=False`` (read-only,
    current image plane, NO mutation) and pulls the wave-1 STRH value. A read failure /
    a missing value degrades to ``(None, <warning>)`` — Strehl is an adjunct, not the
    deliverable (D8). Returns ``(strehl_value_or_None, warning_or_None)``.
    """
    try:
        result = _am.analyze_strehl(session, {"best_focus": False})
    except Exception as exc:  # noqa: BLE001 — D8: never let the adjunct fail the run
        return None, f"strehl_nominal read failed ({safe_exc(exc, repr_form=True)})"
    if not isinstance(result, dict) or not result.get("ok"):
        return None, "strehl_nominal unavailable (analyze_strehl returned no result)"
    at_image = result.get("at_image_plane") or []
    for entry in at_image:
        if isinstance(entry, dict) and entry.get("wave") == 1:
            value = entry.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(value):
                return float(value), None
            return None, "strehl_nominal: the wave-1 STRH reading was non-finite"
    return None, "strehl_nominal: no wave-1 STRH reading was returned"


def _default_mechanical(system):
    """A conservative opt-in mechanical default set (D17/include_mechanical=True).

    A per-element surface tilt (TETX, surf-range over a cemented pair / single element)
    + decenter (TEDX) + irregularity (TIRR) on each glass-bearing interior surface. NOT
    auto-expanded into the standing default (D17) — it is built ONLY when
    ``include_mechanical=True``. Pure-python (reads the LDE); NEVER opens a tool. The
    TETX/TEDX surface RANGE is ``surf .. surf`` (a single-surface element span, the
    minimal conservative choice — the engine accepts surf1 < surf2 only for a multi-
    surface element; a single surface uses surf..surf+1 against the next interface).

    ROUND-13 -- RETURNS ``(entries, skipped)``, AND THE DOCSTRING'S "each" WAS FALSE.
    The claim above is a tilt + decenter + irregularity set on EACH glass-bearing
    interior surface. It is not: on the LAST interior surface the ``nxt`` clamp collapses
    to ``nxt = surf``, the ``nxt > surf`` guard is then False, and only TIRR is authored
    — that element ships with NO tilt and NO decenter tolerance, undisclosed. For n=6
    that is surface 4 (a cover glass or window in contact with the image plane: unusual,
    entirely legal, and exactly the element a tilt tolerance matters for).

    The clamp is NOT the bug and is deliberately kept: TETX/TEDX are surface-RANGE
    operands, ``_tolerance_common`` requires ``surface < surface2 < image_surf``, so for
    ``surf == n - 2`` there is no legal ``surface2`` at all — authoring one would be a
    hard ``tolerancing_param`` refusal that fails the whole run. Widening the span
    backwards (``surf-1 .. surf``) would tolerance a DIFFERENT element's tilt and report
    it as this one's, which is worse than the gap. So the fix is disclosure: ``skipped``
    carries the surfaces that got irregularity only, and the caller warns.

    A STANDING LIMIT, not an open defect (measured on OpticStudio 25.1.0 with a 1 mm
    cover glass touching the image): no operand route yields a correct number there.
    TETX/TEDX with ``surface2`` = the image are refused client-side and, injected past
    that bound, the engine returns the FRONT-surface tilt number, about 47x the rigid-
    plate oracle; TSTX/TSDX on the single surface and TETX(7,8) (another element) read
    other quantities; TETX(s,s) and TUTX/TUDX are refused. A zero-thickness air surface
    after the last glass (identical geometry, identical nominal) makes the default set
    work, so that is the remedy the warning names. The tool does NOT insert one itself:
    that would change the prescription during a run that promises to leave it untouched.
    """
    try:
        n = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — unreadable -> no mechanical default
        return [], []
    out = []
    skipped = []
    for surf in range(1, n - 1):
        material = _tc._surface_material(system, surf)
        is_glass = bool(material) and str(material).strip() != ""
        if not is_glass:
            continue
        nxt = surf + 1
        if nxt >= n - 1:
            nxt = surf  # degenerate: no legal surface2 exists (see the docstring)
        # TETX / TEDX are surface-RANGE ops (Surf1/Surf2); TIRR is single-surf.
        if nxt > surf:
            out.append({"type": "TETX", "surface": surf, "surface2": nxt,
                        "delta": 0.1})
            out.append({"type": "TEDX", "surface": surf, "surface2": nxt,
                        "delta": 0.05})
        else:
            skipped.append(surf)
        out.append({"type": "TIRR", "surface": surf, "delta": 1.0})
    return out, skipped


def _configure_run(tol, system, mode, trials, output_path, *,
                   criterion="wavefront", compensator="paraxial",
                   sampling=_tc._DEFAULT_SAMPLING, cap_s=None):
    """Configure the tool's mode/criterion/compensator/sampling/trials + OutputFile, then RUN (steps 5/6).

    Resolves ``SetupMode`` / ``Criterion`` / ``CriterionComp`` members via getattr (D6), sets
    ``CriterionSampling`` (S2a: default 4, the engine's 2 under-resolves), the DLS cycle cap
    (``CriterionCycleIndex = 3`` + ``IsShowCompensatorsUsed``), MC ``NumberOfRuns`` and
    ``NumberToSave = 0`` (no generated MC lens files, when the member exists), disables
    data-retention saving (``SaveTolDataFile=False``, D15), ``makedirs`` the OutputFile parent
    sets ``OutputFile`` BEFORE the run.

    READ-BACK CANARY, BEFORE the run: the criterion member, ``CriterionCompIndex``
    (never ``str(CriterionComp)``: it aliases), ``CriterionSampling``, ``CriterionCycleIndex``
    under dls and ``NumberToSave`` under MC must each read back as written, else
    ``tolerancing_run`` ("the setting did not take; nothing was run") with
    ``RunAndWaitForCompletion`` never called. Then runs, and reads ``Succeeded`` /
    ``ErrorMessage`` WHILE open (the engine-success canary, D11).

    Returns ``(echoed_trials, readback)``: ``echoed_trials`` is re-read off ``NumberOfRuns``
    (never just the requested value, D12); ``readback`` is ``{criterion, comp_index, sampling,
    cycle_index, field_mode, number_to_save, wall_s}``. ``CriterionField`` is WRITTEN to the
    fresh-engine default ``XY_Symmetric`` before every run (Amendment 3: it persists across
    ``LoadFile`` in one engine) and READ BACK; the read-back, never a refusal, decides the mode
    (a lens the engine cannot run symmetric falls back to its own, e.g. the DG's UserDefined). Raises ``ToleranceError(family=
    "tolerancing_run")`` on a read-back miss or an engine-success canary miss.
    """
    spec = _tc._CRITERIA[criterion]
    comp_member, comp_index = _tc._COMPENSATORS[compensator]
    setup_enum = _tc._tol_namespace_enum(system, "SetupModes")
    crit_enum = _tc._tol_namespace_enum(system, "Criterions")
    comp_enum = _tc._tol_namespace_enum(system, "CriterionComps")
    tol.SetupMode = _tc._resolve_tol_enum(setup_enum, _tc._MODE_TO_SETUP[mode])
    tol.Criterion = _tc._resolve_tol_enum(crit_enum, spec.member)
    tol.CriterionComp = _tc._resolve_tol_enum(comp_enum, comp_member)  # set by NAME
    # Amendment 3: reset the field mode to the fresh-engine default (bench order: after the
    # compensator, before the sampling). The WHOLE write is one try: an enum-reach failure or a
    # raising setter is disclosed as a warning and the read-back still decides.
    field_write_error = None
    # Under ``merit`` the CriterionField write does not read back and the
    # merit's own rows carry their pupil sampling, so neither is written (nor read back, nor echoed).
    merit = criterion == "merit"
    if not merit:
        try:
            field_enum = _tc._tol_namespace_enum(system, "CriterionFields")
            tol.CriterionField = _tc._resolve_tol_enum(field_enum, _tc._DEFAULT_FIELD_MODE)
        except Exception as exc:  # noqa: BLE001
            field_write_error = safe_exc(exc, repr_form=True)
        tol.CriterionSampling = int(sampling)
    if _tc._is_dls(compensator):
        tol.CriterionCycleIndex = _tc._DLS_CYCLE_CAP
        tol.IsShowCompensatorsUsed = True

    echoed_trials = None
    has_number_to_save = False
    if mode == "monte_carlo":
        tol.NumberOfRuns = trials
        try:
            echoed_trials = int(tol.NumberOfRuns)  # echo from the live tool (D12)
        except Exception:  # noqa: BLE001 — fall back to the requested value
            echoed_trials = trials
        has_number_to_save = hasattr(tol, "NumberToSave")
        if has_number_to_save:
            tol.NumberToSave = 0

    # D15: do not persist the .ZTD data-retention file (we still glob-reap defensively).
    try:
        tol.SaveTolDataFile = False
    except Exception:  # noqa: BLE001 — not all builds expose it; the reap is the net
        pass

    # D15/#58: the engine SILENTLY writes nothing to a missing dir -> makedirs first.
    # Guarded -> a real unwritable parent becomes a structured tolerancing_run envelope.
    try:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    except (OSError, ValueError) as exc:
        raise _tc.ToleranceError(
            f"could not create the parent directory for the report {output_path!r} "
            f"({safe_exc(exc, repr_form=True)})",
            family="tolerancing_run",
        )
    tol.OutputFile = output_path

    # READ-BACK CANARY (before the run): each configured setting must read back as written.
    def _read(getter, cast):
        try:
            return cast(getter())
        except Exception:  # noqa: BLE001 — an unreadable setting is a miss
            return None

    criterion_back = _read(lambda: tol.Criterion, str)
    readback = {
        "criterion": spec.member,
        "comp_index": _read(lambda: tol.CriterionCompIndex, int),
        "sampling": None if merit else _read(lambda: tol.CriterionSampling, int),
        "cycle_index": (_read(lambda: tol.CriterionCycleIndex, int)
                        if _tc._is_dls(compensator) else None),
        "field_mode": None if merit else (_read(lambda: tol.CriterionField, str) or "unreadable"),
        "number_to_save": (_read(lambda: tol.NumberToSave, int)
                           if has_number_to_save else None),
        "field_write_error": field_write_error,
    }
    misses = []
    if not (criterion_back or "").endswith(spec.member):
        misses.append(f"Criterion reads back {criterion_back!r}, expected {spec.member!r}")
    if readback["comp_index"] != comp_index:
        misses.append(f"CriterionCompIndex reads back {readback['comp_index']!r}, "
                      f"expected {comp_index} ({comp_member})")
    if not merit and readback["sampling"] != int(sampling):
        misses.append(f"CriterionSampling reads back {readback['sampling']!r}, "
                      f"expected {int(sampling)}")
    if _tc._is_dls(compensator) and readback["cycle_index"] != _tc._DLS_CYCLE_CAP:
        misses.append(f"CriterionCycleIndex reads back {readback['cycle_index']!r}, "
                      f"expected {_tc._DLS_CYCLE_CAP}")
    if has_number_to_save and readback["number_to_save"] != 0:
        misses.append(f"NumberToSave reads back {readback['number_to_save']!r}, expected 0")
    if misses:
        raise _tc.ToleranceError(
            "the setting did not take; nothing was run: " + "; ".join(misses),
            family="tolerancing_run",
        )

    # RUN (synchronous, like the optimizer; probe §7).
    t0 = time.perf_counter()
    if cap_s is None:
        ran = bool(tol.RunAndWaitForCompletion())
        readback["wall_s"] = round(time.perf_counter() - t0, 3)
    else:
        run_exc = None
        try:
            status = _rb.run_status_name(tol.RunAndWaitWithTimeout(float(cap_s)))   # by NAME (V7)
        except Exception as exc:  # noqa: BLE001 -- a COM fault mid-run: the run state is unknown (PF1)
            run_exc, status = exc, None
        readback["wall_s"] = round(time.perf_counter() - t0, 3)
        readback["cap_s"] = float(cap_s)
        if run_exc is not None or status not in ("Completed", "TimedOut"):
            # PF1: neither a finished nor a cleanly timed-out run. ONE read-only probe of the run state: still
            # running (or unreadable) -> the no-edit busy path; stopped -> the unchanged ordinary path.
            try:
                still = bool(tol.IsRunning)
            except Exception as exc:  # noqa: BLE001 -- an unreadable run state is an unconfirmed stop
                raise _EngineBusy(f"the timed run did not report Completed or TimedOut and the run state could not "
                                  f"be read: {safe_exc(exc)}") from run_exc
            if still:
                raise _EngineBusy(f"the timed run did not report Completed or TimedOut (status {status!r}"
                                  f"{'' if run_exc is None else ', ' + safe_exc(run_exc)}) and the tool still "
                                  f"reads running") from run_exc
            if run_exc is not None:
                raise run_exc
        if status == "TimedOut":
            # A timed-out run reads Succeeded=True (V23) and leaves no usable report: stop it, prove the stop
            # BEFORE Close, and raise before the Succeeded canary or any report read.
            try:
                tol.Cancel()
                tol.WaitWithTimeout(_rb.CANCEL_WAIT_S)
                still_running = bool(tol.IsRunning)
            except Exception as exc:  # noqa: BLE001 -- a stop that throws is an unconfirmed stop
                raise _EngineBusy(f"capped at {float(cap_s):.0f} s and the stop could not be confirmed: "
                                  f"{safe_exc(exc)}")
            if still_running:
                raise _EngineBusy(f"capped at {float(cap_s):.0f} s and the run did not stop within "
                                  f"{_rb.CANCEL_WAIT_S:g} s of Cancel")
            raise _tc.ToleranceError(f"capped at {float(cap_s):.0f} s: unjudged", family="tolerancing_run",
                                     reason="capped")
        ran = status == "Completed"
    succeeded = bool(getattr(tol, "Succeeded", False))
    error_message = getattr(tol, "ErrorMessage", None)
    err_text = "" if error_message is None else str(error_message)
    # The engine-success canary (D11): read WHILE open, BEFORE parse.
    if not ran or not succeeded or (err_text and err_text.lower() not in ("", "none")):
        raise _tc.ToleranceError(
            f"the tolerancing run did not succeed (ran={ran}, Succeeded={succeeded}, "
            f"ErrorMessage={err_text!r}); nothing was parsed",
            family="tolerancing_run",
        )
    return echoed_trials, readback


def _compute_hotspots(system, parsed, recon, authored, vendor):
    """Rank the sensitivity rows by element / cemented group / air gap. Never raises.

    Returns ``(hotspots, withheld, identity, notes)``. The hotspots module is imported
    lazily (it is pure and engine-free); an unreadable LDE topology passes ``topo=None``,
    which it reports as ``topology_unreadable``.
    """
    try:
        # lazy: keeps this module importable standalone; INSIDE the try so an import failure
        # withholds (closed vocabulary 'topology_unreadable') instead of failing a finished run
        from . import _tolerance_hotspots as _hot
    except Exception as exc:  # noqa: BLE001
        return [], "topology_unreadable", None, [
            f"hotspots withheld (topology_unreadable): the hotspots module could not be "
            f"imported ({type(exc).__name__}: {exc})"]
    try:
        topo = _hot.read_topology(system)
    except Exception:  # noqa: BLE001 — an unreadable topology withholds, never fails the run
        topo = None
    units_by_type = {e["type"]: e.get("units") for e in authored if e.get("type")}
    kwargs = dict(dropped_row_count=parsed.get("dropped_row_count", 0), recon=recon,
                  vendor=vendor)
    detailed = getattr(_hot, "build_hotspots_detailed", None)
    if detailed is not None:
        return detailed(parsed["sensitivity"], topo, parsed.get("rss_estimated_change"),
                        units_by_type, **kwargs)
    hotspots, withheld, identity = _hot.build_hotspots(
        parsed["sensitivity"], topo, parsed.get("rss_estimated_change"), units_by_type,
        **kwargs)
    notes = [f"hotspots withheld ({withheld})"] if withheld else []
    return hotspots, withheld, identity, notes


def _run_pick_push(session, params):
    """The ``mode='pick_push'`` hookup (compensator-picker spec 3.2): the picker module sequences the inner calls."""
    from . import _tolerance_pick as _pick
    return _pick.run(session, params, tolerance)


@_never_raise("tolerance")
def tolerance(session, params, *, cap_s=None):
    """Run a tolerance analysis (sensitivity OR monte_carlo). See the module docstring.

    ``mode`` (REQUIRED) selects sensitivity (the both-sign asymmetric per-parameter
    table + worst offenders + an RSS estimate + ``hotspots[]``) or monte_carlo (mean/std/
    best/worst + cumulative-probability percentiles). ``tolerances`` (optional) is a list of
    ``{type, surface, delta}`` (omit for the default budget). ``trials`` (monte_carlo
    only, default 20) is the engine ``NumberOfRuns``. ``full`` (monte_carlo) includes
    the per-trial array. ``output_file`` (optional) overrides the temp report path.

    S2a: ``budget={vendor, grade, element_decenter, element_tilt}`` drives the engine
    tolerance wizard (mutually exclusive with ``tolerances`` / ``include_mechanical``);
    ``criterion`` ('wavefront' default | 'spot'), ``compensator`` ('paraxial' default | 'dls',
    which needs ``budget``) and ``sampling`` (default 4) set the run. Every success carries
    ``run_summary``.

    Gotcha: the default criterion is the field-summed RMS WFE in waves; a nominal Strehl is
    reported ALONGSIDE (D8). monte_carlo is STRUCTURALLY non-deterministic
    (``deterministic:false`` / ``seed_supported:false`` — the engine exposes no seed,
    probe §5). The tool CLEARS the Tolerance Data Editor (author-as-scratch, D10). The
    LDE prescription is left untouched (a side-effect tripwire flags a regression).

    The catalogue breakdown is in the served description. Pass a RESOLVED code (ground
    tolerance intent via ``lookup_operand`` with ``domain="tolerance"``, or
    ``search_reference`` for chapter context). A
    CB/NSC-required operand (TUTX/TUDX/TNPS …) is a LABELED ``known_gap`` refusal (run
    the valid operands + disclose loud unless ``strict=True``); a crash-class operand
    (TNPA/TNMA) is a HARD refusal (the tool never opens). ``include_mechanical=True``
    adds a conservative per-element TETX/TEDX/TIRR set to the DEFAULT budget; ``strict``
    rejects the whole set on ANY gap. Never raises past the handler.
    """
    # (S2b) the canned desensitize recipe is intercepted FIRST, before ``_resolve_mode``, so the
    # S2a bad-mode message stays byte-identical. The recipe calls THIS handler (the never-raise
    # wrapped one) for its BEFORE and AFTER sensitivity runs. Lazy import: the S2a path never loads it.
    if isinstance(params, dict) and dict.get(params, "mode") == "desensitize":
        from . import _tolerance_desensitize as _desens
        return _desens.run(session, params, tolerance)
    # (compensator-picker) mode='pick_push' is intercepted at the same point, for the same reason.
    if isinstance(params, dict) and dict.get(params, "mode") == "pick_push":
        # The picker sets its own per-candidate cap, so a caller's ``cap_s`` is echoed-ignored (and a
        # malformed one still refused, before the picker opens anything).
        if not dict.__contains__(params, "cap_s"):
            return _run_pick_push(session, params)
        pick_cap = dict.__getitem__(params, "cap_s")
        if isinstance(pick_cap, bool) or not is_finite_number(pick_cap) or pick_cap <= 0:
            raise _tc.ToleranceError(
                f"cap_s must be a finite number > 0 (seconds), got {pick_cap!r}; nothing was opened",
                family="tolerancing_param", reason="cap_s_invalid")
        env = _run_pick_push(session, params)
        if isinstance(env, dict):
            env["warnings"] = list(env.get("warnings") or []) + [
                "'cap_s' was passed with mode='pick_push' (the picker sets its own per-candidate cap); echoed-ignored"]
        return env
    system = session.system

    # (1) the REQUIRED mode + optional knobs (bad value -> tolerancing_param).
    mode = _resolve_mode(params)
    trials = _resolve_trials(params)
    full = _bool_param(params, "full", False)
    strict = _bool_param(params, "strict", False)
    include_mechanical = _bool_param(params, "include_mechanical", False)
    # The criterion / compensator / sampling / wizard budget. All PURE.
    criterion = _resolve_choice(params, "criterion", _tc._CRITERIA, "wavefront")
    compensator = _resolve_choice(params, "compensator", _tc._COMPENSATORS, "paraxial")
    sampling = _resolve_sampling(params)
    budget = _resolve_budget(params)
    if criterion == "merit" and mode != "monte_carlo":
        raise _tc.ToleranceError(
            "criterion='merit' runs under mode='monte_carlo' only (the sensitivity report has no merit "
            "RSS line); nothing was opened", family="tolerancing_param", reason="merit_mc_only")
    warnings = []
    # unbound base slot. A lying ``get`` returning None here swaps the caller's
    # EXPLICIT tolerance budget for the auto-derived default one, silently.
    supplied = dict.get(params, "tolerances")
    if budget is None and _tc._is_dls(compensator):
        return _ac.error_envelope(
            "tolerance", "tolerancing_param",
            "compensator='dls' needs budget={vendor, grade, element_decenter, element_tilt}; "
            "r5: DLS with no compensator row is worse than paraxial. Nothing was opened.",
        )
    if budget is not None:
        if supplied is not None:
            return _ac.error_envelope(
                "tolerance", "tolerancing_param",
                "budget (the wizard's own rows) cannot be combined with an explicit "
                "tolerances list; pass one or the other. Nothing was opened.",
            )
        if include_mechanical:
            return _ac.error_envelope(
                "tolerance", "tolerancing_param",
                "budget (the wizard's own rows) cannot be combined with "
                "include_mechanical=true; the wizard already authors element decenter/tilt. "
                "Nothing was opened.",
            )
        if strict:
            warnings.append(
                "'strict' was passed with budget=...; the wizard authors no known-gap "
                "operand, so it has nothing to reject; echoed-ignored"
            )
        if budget["vendor"] == "Asphericon":
            warnings.append(
                "radius_unit_unverified: the Asphericon preset's radius tolerance unit "
                "(percent vs lens units) is unverified; hotspots are withheld and the TRAD "
                "rows in authored_operands carry units: null"
            )
    if _tc._is_dls(compensator):
        _dls_preflight(system)  # a glass / infinite back gap: refused BEFORE the wizard runs
    # (S2b) the CB-tilt compensator: every refusal here is pure reads, before anything is opened.
    cb_spec = _resolve_cb(params, compensator, mode, budget, system)
    # (compensator-picker 3.1 rows 10-12) the loaded merit must be judgeable: single config, no TOLR, usable value.
    merit_before = merit_preflight(system) if criterion == "merit" else None
    # A trials/full passed with mode=sensitivity is echoed-ignored + warned (D-vector
    # flat-required convention) — never a hard reject.
    if mode == "sensitivity":
        for key in ("trials", "full"):
            if dict.__contains__(params, key):  # unbound base slot
                warnings.append(
                    f"{key!r} was passed with mode='sensitivity' (it applies only to "
                    "monte_carlo); echoed-ignored"
                )
    mc_threshold = None
    if dict.__contains__(params, "threshold"):
        if mode == "monte_carlo":
            # Plain monte_carlo scores the run under the threshold (``yield`` block). The rule is the
            # picker's row 7, restated here because sharing it would mean editing ``_tolerance_pick``.
            mc_threshold = dict.__getitem__(params, "threshold")
            if not is_finite_number(mc_threshold) or mc_threshold <= 0:
                raise _tc.ToleranceError(
                    f"threshold must be a finite number > 0 (criterion units), got {mc_threshold!r}; "
                    "nothing was opened", family="tolerancing_param", reason="threshold_invalid")
            mc_threshold = float(mc_threshold)
        else:
            warnings.append(
                f"'threshold' was passed with mode={mode!r} (it applies only to mode='monte_carlo' or 'pick_push'); echoed-ignored")

    # The wall cap. Precedence: the caller keyword (the picker's inner call), then ``params["cap_s"]``,
    # then the monte_carlo default (trials x the picker's per-trial cap). Resolved before anything is opened.
    cap_source = None
    if cap_s is not None:
        cap_s, cap_source = float(cap_s), "caller"
    if dict.__contains__(params, "cap_s"):
        if mode == "monte_carlo":
            param_cap = dict.__getitem__(params, "cap_s")
            if isinstance(param_cap, bool) or not is_finite_number(param_cap) or param_cap <= 0:
                raise _tc.ToleranceError(
                    f"cap_s must be a finite number > 0 (seconds), got {param_cap!r}; nothing was opened",
                    family="tolerancing_param", reason="cap_s_invalid")
            if cap_s is None:
                cap_s, cap_source = float(param_cap), "param"
        else:
            warnings.append(
                f"'cap_s' was passed with mode={mode!r} (it applies only to mode='monte_carlo'); echoed-ignored")
    if cap_s is None and mode == "monte_carlo":
        from . import _tolerance_pick as _pick
        cap_s, cap_source = float(trials * _pick.S_PER_TRIAL_CAP + _MC_CAP_SETUP_S), "default"

    # (1) pre-flight: build + validate the tolerance set (PURE; D14/G17). Open NOTHING.
    # The wizard path (budget) skips this: its rows are READ BACK from the editor after OK().
    authored, known_gaps = [], []
    grin_perturbation = grin_coverage_warning = None
    if budget is None:
        if supplied is None:
            to_validate = _tc._default_tolerances(system)
            if include_mechanical:
                # OPT-IN conservative mechanical set (D17): a per-element surface tilt /
                # decenter / irregularity on each glass-bearing interior surface — EXCEPT
            # where the surface-RANGE operands have no legal surface2. That
                # element is under-toleranced, so say so rather than let a clean envelope
                # imply the whole mechanical budget was authored.
                mech, mech_skipped = _default_mechanical(system)
                to_validate = list(to_validate) + mech
                if mech_skipped:
                    warnings.append(
                        "mechanical_default_partial: glass surface(s) "
                        + ", ".join(str(s) for s in mech_skipped)
                        + " carry only a TIRR irregularity tolerance — no TETX tilt and no "
                        "TEDX decenter. Those are surface-RANGE operands needing "
                        "surface < surface2 < image, and the next interface here IS the "
                        "image surface, so no legal range exists. This is a STANDING LIMIT "
                        "of the tool, not a fault of this design: no operand route gives a "
                        "correct tilt or decenter number for a glass element that touches "
                        "the image (the image-surface route returns the tilt of the element's "
                        "single FRONT surface, not the element's tilt: about 47x too large "
                        "on the measured case). That element's "
                        "tilt and decenter sensitivity is UNBOUNDED in this run. REMEDY: add "
                        "a zero-thickness air surface after the last glass surface (the "
                        "geometry is unchanged) and run again — the default set then covers "
                        "it."
                    )
            if not to_validate:
                return _ac.error_envelope(
                    "tolerance", "tolerancing_param",
                    "no default tolerance budget could be built (the LDE has no powered or "
                    "glass interior surface); supply an explicit 'tolerances' list",
                )
        else:
            to_validate = supplied
        authored, errors, validate_warnings, known_gaps = _tc.validate_tolerances(
            system, to_validate, strict=strict
        )
        warnings.extend(validate_warnings)
        if errors:
            return _ac.error_envelope(
                "tolerance", "tolerancing_param",
                "the tolerance set failed validation; see 'errors'. No tool was opened and "
                "nothing was authored.",
                errors=errors,
                known_gaps=known_gaps,
            )

    # (GRIN §1.3e) the GRIN Par# perturbation disclosures + the variable-gradient
        # coverage warning — computed post-validation PRE-run (both read-only; never raise).
    # Threaded ONLY into the SUCCESS + reconcile-ESCALATION envelopes (v2).
        grin_perturbation = _grin_perturbation_disclosures(system, authored)
        grin_coverage_warning = _grin_coverage_warning(system, authored)

    # preflight WARN-and-run: classify system.SystemFile. A non-blessed
    # backing file (empty / New-default / a harness checkpoint) WARNS that the
    # run will likely produce no report (load_design a saved design) — never a refuse
    # (cond F: pre-run blessing is unpredictable; the post-run empty-report net is the
    # real guarantee).
    _sys_file = _tc._safe_system_file(system)
    _blessed_hint, _preflight_warn = _tc.classify_system_file(_sys_file)
    if _preflight_warn:
        warnings.append(_preflight_warn)

    # (2) the LDE side-effect snapshot (D16) — BEFORE any author/run.
    lde_before = _tc.snapshot_lde(system)
    # The side files that already exist: the finally reap keeps them.
    side_patterns = _tc._tol_side_patterns(system)  # resolved ONCE: listing + reap share it
    pre_side_files = (None if side_patterns is None  # no path read: reap no side file
                      else _tc.existing_tol_side_files(system, side_patterns))

    operand_enum = _tc._tolerance_operand_enum(system)  # TYPE reach -> tolerancing_unavailable
    tde = system.TDE

    # (S2a) the same-judge key + the LDE topology key (pure LDE reads) and the back gap the
    # wizard compensates (n - 2; read BEFORE the run for the MC compensator-movement proof).
    rule_id = _tc.budget_rule_id(budget, supplied, include_mechanical)
    try:
        topology_digest = _tc.topology_digest(system)
    except Exception:  # noqa: BLE001 — an unreadable LDE must not fail the run
        topology_digest = None
        warnings.append("topology_digest could not be read from the LDE; run_summary "
                        "carries null (hotspots cannot be joined across runs)")
    image_index = _tc._image_surface_index(system)
    back_surface = None if image_index is None else image_index - 1
    back_thickness = None
    if back_surface is not None and back_surface >= 1:
        try:
            back_thickness = float(system.LDE.GetSurfaceAt(back_surface).Thickness)
        except Exception:  # noqa: BLE001 — only the MC at-bound warning needs it
            back_thickness = None

    output_path, is_temp = _resolve_output_path(session, params)
    echoed_trials = None
    readback = None
    comp_bounds = None
    budget_digest = None
    n_budget_rows = None
    report_bytes = None
    tde_cleared = True
    cb_ctx = None if cb_spec is None else _CbRun(cb_spec)
    cb_error = None
    engine_busy = False      # a capped run whose stop is unconfirmed: nothing below may edit the lens or the TDE
    try:
        # (S2b) the temporary CB layout, BEFORE the wizard so its rows see the layout's surfaces.
        if cb_ctx is not None:
            _cb_build(session, cb_ctx)
            if criterion == "merit":
                _merit_layout_guard(system, cb_ctx, merit_before)     # spec 3.4: fail closed per candidate
        # (3) TDE author-as-scratch (D10): clear (floor-at-1) then author each row — or,
        # on the wizard path, let the engine's own wizard write them and READ THEM BACK.
        if budget is not None:
            wiz = _tc.apply_wizard_budget(system, budget)
            effective = _tc.check_wizard_rows(wiz["rows"], wiz["back_surface"], budget)
            # The engine writes the element rows ROUNDED (0.0167 -> 0.017); the same judge is
            # the same ROWS, so the rule id records the effective values (Amendment 1).
            eff_budget = dict(budget, **effective)
            rule_id = _tc.budget_rule_id(eff_budget, supplied, include_mechanical)
            for axis in ("element_decenter", "element_tilt"):
                if not math.isclose(effective[axis], budget[axis], rel_tol=1e-9, abs_tol=1e-12):
                    warnings.append(
                        f"element_value_rounded: {axis} requested {budget[axis]!r} but the "
                        f"wizard's rows carry {effective[axis]!r} (the engine rounds the row "
                        "value); the run used the effective value")
            authored = wiz["authored"]
            back_surface = wiz["back_surface"]
            comp_bounds = wiz["comp"]
            if cb_ctx is not None:
                # the CPAR rows on the entry CB; their read-back is the proof they were written
                _author_cpar_rows(system, cb_ctx.layout["entry_cb"], cb_spec["pars"])
                cb_ctx.layout_tde_rows = _cb_layout_tde_rows(wiz["rows"], cb_ctx.layout)
        else:
            _clear_tde(tde)
            _author_tolerance_set(tde, operand_enum, authored)
        # The budget digest is over the READ-BACK on EVERY path: it reflects what the
        # engine actually held, not what we meant to author.
        tde_rows = _tc.read_tde_rows(tde)
        if cb_ctx is not None and cb_spec["name"] == "cb_group":
            cb_ctx.effective = _tc.verify_cpar_axes(tde_rows, cb_ctx.layout["entry_cb"], cb_spec["pars"])
            rule_id = _cb_group_rule_id(rule_id, cb_spec, cb_ctx.effective)
        elif cb_ctx is not None:
            cb_ctx.effective_range_deg = _tc.verify_cpar_rows(
                tde_rows, cb_ctx.layout["entry_cb"], cb_spec["range_deg"])
            rule_id = (f"{rule_id}:cbtilt={cb_spec['group']}"
                       f"@{format(cb_ctx.effective_range_deg, '.12g')}")
        budget_digest = _tc.budget_digest(tde_rows)
        n_budget_rows = len(tde_rows)
        if criterion == "merit":
            _merit_dmfs_guard(system, tde_rows)      #: the TDE as it will run, before the tool opens

        # (4) open the single slot (D13: None -> tolerancing_unavailable, nothing run).
        with _tc._tolerancing_session(system) as tol:
            # (5/6) configure + run; (6) engine-success canary read WHILE open.
            echoed_trials, readback = _configure_run(
                tol, system, mode, trials, output_path,
                criterion=criterion, compensator=compensator, sampling=sampling, cap_s=cap_s)
        # (7) slot reaped by the context manager's finally. Read the report bytes off
        # disk NOW (it persists after Close), BEFORE the reap removes a temp report.
        report_bytes = _tc.read_report_bytes(output_path)
    except Exception as exc:  # noqa: BLE001 -- on the CB path the layout teardown below decides the envelope
        engine_busy = isinstance(exc, _EngineBusy)
        if cb_ctx is None and not engine_busy:
            raise
        cb_error = exc
    finally:
        if engine_busy:
            # (spec 3.5) the engine may still be running the analysis: touch NOTHING. No TDE clear, no
            # artifact reap, no layout teardown. The session's own ``Close()`` already ran (its throw is swallowed).
            tde_cleared = False
            if cb_ctx is not None:
                cb_ctx.restore = {"restored": False, "steps_run": [], "failed_step": "engine_still_running",
                                  "n_differences": None, "first_differences": [], "mfe_reimposed_writes": 0}
        else:
            # (9) TDE clear in finally — leave a clean floored editor. Track the
            # ACTUAL result so ``tde_cleared`` in the envelope is HONEST, never hardcoded: a
            # clear throw here -> tde_cleared:false + a warning (the editor may carry leftover
            # scratch rows; reload your .zmx before the next author).
            try:
                tde.DeleteAllRows()
            except Exception as exc:  # noqa: BLE001 — a TDE teardown throw must not mask body
                tde_cleared = False
                warnings.append(
                    f"the Tolerance Data Editor could not be cleared after the run "
                    f"({safe_exc(exc, repr_form=True)}); "
                    "it may carry leftover scratch rows — reload your .zmx before re-authoring"
                )
            # (7/D15) reap the engine's .ZTD side files on EVERY path, except those that
            # existed before the run; reap the report only when it is the tool's own
            # temp (an explicit output_file is the caller's to keep).
            try:
                _tc.reap_tol_artifacts(system, output_path, reap_report=is_temp,
                                       keep=pre_side_files, patterns=side_patterns)
            except Exception:  # noqa: BLE001 — a reap throw must not mask the body
                pass
            # The layout comes out AFTER the TDE rows that reference its surfaces are gone.
            if cb_ctx is not None:
                cb_ctx.restore = _cb_teardown(system, cb_ctx)

    if cb_error is not None:
        if engine_busy:
            return _engine_busy_envelope(cb_error, cb_ctx, warnings)
        return _cb_failure_envelope(cb_error, cb_ctx, warnings, tde_cleared)

    def _finish(env):
        """The CB path never returns ``ok:true`` (or a bare refusal) over a layout it could not remove."""
        if cb_ctx is None or cb_ctx.restore["restored"]:
            return env
        return _cb_unrestored_envelope(env, cb_ctx, warnings, tde_cleared)

    try:
        if budget is not None:
            # The wizard path's authored set is known only now (read back after OK()).
            grin_perturbation = _grin_perturbation_disclosures(system, authored)
            grin_coverage_warning = _grin_coverage_warning(system, authored)

        # the EMPTY-REPORT safety-net (the load-bearing DISJOINT split):
        # the run Succeeded (the engine-success canary in _configure_run already gated
        # ran/Succeeded) yet wrote a 0-byte report -> the system was not opened from a
        # blessed on-disk original (probe-frozen). Divert to a PRECISE
        # ``tolerancing_empty_report`` envelope BEFORE decode_tol_report. A NON-empty file
        # (short / wrong-BOM / garbage) still flows to the BOM-gate/canary -> the disjoint
        # ``tolerancing_parse`` diagnosis. The two acceptance sets are disjoint by len==0.
        if len(report_bytes or b"") == 0:
            return _finish(_ac.error_envelope(
                "tolerance", _tc._EMPTY_REPORT_FAMILY,
                "the headless tolerancing run succeeded but produced an empty (0-byte) "
                "report; nothing was parsed.",
                system_file=_tc._safe_system_file(system),
                blessed_for_tolerancing=_blessed_hint,
                ran=True,
                succeeded=True,
                report_size=0,
                recovery=_tc._EMPTY_REPORT_RECOVERY,
                warnings=warnings,
            ))

        # (8) decode + parse the report (the canary; never zero-fill). Pure on the in-memory
        # bytes read above — a parse-canary RAISE surfaces AFTER the slot + TDE were reaped.
        # The trial-count cross-check uses the ECHOED NumberOfRuns (read off the live tool),
        # NOT the requested trials (D11.4/D14): an engine that clamps N must not trigger a
        # false tolerancing_parse. Sensitivity ignores the count.
        cross_check_trials = (
            echoed_trials if (mode == "monte_carlo" and echoed_trials is not None)
            else trials
        )
        text = _tc.decode_tol_report(report_bytes)
        parsed = _tc.parse_tol_report(
            text, mode=mode, trials=cross_check_trials, criterion=criterion,
            compensator=compensator, back_surface=back_surface,
            cpar_surface=None if cb_ctx is None else cb_ctx.layout["entry_cb"],
            cpar_pars=(3, 4) if cb_ctx is None else tuple(sorted(cb_spec["pars"])))
        cb_group_summary = None
        at_bound = None
        if cb_ctx is not None and cb_spec["name"] == "cb_group":
            warnings.append(_CB_GROUP_DISCLOSURE)
            cb_group_summary, at_bound = _cb_group_report(text, cb_ctx, cb_spec, parsed, full, warnings)
        elif cb_ctx is not None:
            warnings.append(_CB_DISCLOSURE)
        if criterion == "merit" and mode == "monte_carlo":
            # spec 3.3 / 3.7a: the plain door REPORTS a fixed-offset run, the picker judges it.
            values = parsed.get("per_trial") or []
            spread = _tc.rel_spread(values)
            if spread is not None and spread < _tc.SPREAD_FLOOR:
                warnings.append(_tc.near_identical_warning(
                    sum(1 for v in values if v is not None and math.isfinite(v)), spread))
        if readback.get("field_write_error"):
            warnings.append(
                "field_mode_write_failed: could not write CriterionField="
                f"{_tc._DEFAULT_FIELD_MODE} before the run ({readback['field_write_error']}); "
                f"run_summary.field_mode is the engine's read-back ({readback['field_mode']!r})")
        # Surface any parser-side warnings (a dropped/incomplete sensitivity table, D-row).
        parser_warnings = parsed.get("warnings")
        if parser_warnings:
            warnings.extend(parser_warnings)
        if compensator == "paraxial" and _tc._PARAXIAL_FOCUS_HEADER not in text:
            warnings.append(
                "the report lacks the 'Paraxial Focus compensation only.' header; "
                "compensator_participates:false may not hold for this engine version"
            )
        if mode == "monte_carlo" and _tc._is_dls(compensator):
            # The bench's mode_proof: did the back-gap compensator actually MOVE? (warn only)
            warnings.extend(_tc.mc_dls_proof(text, back_surface, back_thickness, comp_bounds)[0])

        # (10) LDE re-read + tripwire (D16).
        lde_after = _tc.snapshot_lde(system)
        state_mutated = not _tc.lde_unchanged(lde_before, lde_after)
        if cb_ctx is not None and not cb_ctx.restore["restored"]:
            state_mutated = True
        if state_mutated:
            warnings.append(
                "the tolerancing run unexpectedly mutated the LDE prescription; reload "
                "your .zmx (a future engine regression surfaced here)"
            )


        # (10b) the authored-vs-parsed RECONCILE canary (D12/G-RECON — the CENTERPIECE).
        # PURE partition: ran / refused / unaccounted. A SUPPORTED-tier unaccounted operand
        # (the engine silently omitted an authored op) escalates to tolerancing_reconcile
        # (ok:false) — the over-optimistic class. The invariant ok:true ⇒ unaccounted == [].
        # Reconcile keys on the per-operand SENSITIVITY table; in monte_carlo mode the report
        # has no per-operand row table, so reconcile is a no-op (the operand_errors are still
        # surfaced via the refused/unattributed channels).
        operand_errors = parsed.get("operand_errors") or []
        if mode == "sensitivity":
            recon = _tc.reconcile_authored_vs_parsed(authored, parsed)
        else:
            # monte_carlo has NO per-operand sensitivity table, so reconcile is structurally
            # a no-op (D12 note). BUG-3 honesty: an operand_error line that IS attributable to
            # an AUTHORED operand must NOT be mislabeled "not attributable" — partition the MC
            # error lines into refused (attributed to an authored op) vs unattributed (type=None
            # or a type we never authored) exactly as sensitivity reconcile would.
            authored_types = {e.get("type") for e in authored}
            mc_refused = []
            mc_unattributed = []
            for e in operand_errors:
                etype = e.get("type")
                if etype and etype in authored_types:
                    mc_refused.append({"type": etype, "reason_line": e["line"]})
                else:
                    mc_unattributed.append(e["line"])
            recon = {"ran": [], "refused": mc_refused, "unaccounted": [],
                     "escalate": False, "unattributed_errors": mc_unattributed}
        # An attributed engine error (a known authored op the engine refused at run) is
        # surfaced HONESTLY as a refusal, not as "not attributable".
        for r in recon.get("refused", []):
            warnings.append(
                f"the engine refused authored operand {r.get('type')!r} at run "
                f"(surfaced not swallowed): {r.get('reason_line')!r}"
            )
        for line in recon.get("unattributed_errors", []):
            warnings.append(
                f"an engine error line was not attributable to any authored operand "
                f"(surfaced not swallowed): {line!r}"
            )
        # A sensitivity row that could not be keyed to a specific authored
        # operand (an unreadable Surf column, or a dropped-corrupt row) is a PARSE ANOMALY —
        # surface it loud (it does NOT mark any authored op ran; the op it would have satisfied
        # then escalates fail-closed).
        for anom in recon.get("anomalous_rows", []):
            warnings.append(
                f"a sensitivity row could not be reconciled to a specific authored operand "
                f"(surfaced not swallowed): {anom.get('detail')!r}"
            )
        # LOW: a keyed parsed row matching no authored (type, surface) is an
        # engine/author desync — surfaced, never silently dropped.
        for extra in recon.get("unexpected_parsed_rows", []):
            warnings.append(
                f"a parsed sensitivity row matched no authored operand "
                f"(engine/author desync, surfaced not swallowed): {extra!r}"
            )
        # Surface the count-fallback disclosure AND the matched-row
        # error lines ABOVE the escalation return — a MIXED result (one group count-fallback +
        # another escalating) must NOT drop these warnings into the void (the escalation
        # envelope already threads warnings=warnings).
        for w in recon.get("par_unresolved", []):
            warnings.append(w)
        for me in recon.get("matched_operand_errors", []):
            warnings.append(
                f"the engine emitted an error line for authored operand {me.get('type')!r} "
                f"whose row ALSO reconciled (surfaced not swallowed): "
                f"{me.get('reason_line')!r}"
            )
        if recon.get("escalate"):
            # The coverage warning rides BOTH the SUCCESS
            # AND the reconcile-ESCALATION envelopes — a variable GRIN gradient with no coefficient
            # TPAR (unbounded fabrication sensitivity) must NOT be silently lost when the run ALSO
            # escalates. Conditional top-level key (matching _build_envelope's non-None posture).
            escalation_extra = {}
            if grin_coverage_warning is not None:
                escalation_extra["grin_tolerance_coverage_warning"] = grin_coverage_warning
            return _finish(_ac.error_envelope(
                "tolerance", _tc._RECONCILE_FAMILY,
                "one or more SUPPORTED authored operands produced NEITHER a sensitivity row "
                "NOR an engine error line (the engine silently omitted them); refusing the "
                "over-optimistic verdict.",
                unaccounted_operands=recon["unaccounted"],
                ran_operands=recon["ran"],
                refused_operands=recon["refused"],
                operand_errors=operand_errors,
                authored_operands=_authored_summary(authored),
                grin_perturbation=grin_perturbation,
                reconciled_operand_keys=recon.get("reconciled_operand_keys", []),
                reconciliation_mode=recon.get("reconciliation_mode"),
                warnings=warnings,
                **escalation_extra,
            ))

        # The Config#-vs-current-config WARN (tmco_config_mismatch) — the
        # silent-zero guard: the headless sensitivity run analyzes ONLY the current config
        # (probe Q12). A TMCO whose mce_config != the run's CurrentConfiguration reads a
        # SILENT ZERO sensitivity (it RAN, it appears in the report, it just bit nothing
        # at the analyzed config). NOT a refusal (a multi-config campaign legitimately authors
        # rows across configs); it is a loud WARN naming the recovery.
        _emit_tmco_config_mismatch_warnings(system, authored, warnings)

        # The hotspots: sensitivity only (the MC shape is today's + run_summary).
        hotspots = hotspots_withheld = identity = None
        if mode == "sensitivity":
            hotspots, hotspots_withheld, identity, hotspot_notes = _compute_hotspots(
                system, parsed, recon, authored, budget["vendor"] if budget else None)
            warnings.extend(hotspot_notes)

        # (11) Strehl alongside (D8) — nominal, separate read; null + warn on failure.
        strehl_nominal, strehl_warning = _strehl_nominal(session)
        if strehl_warning:
            warnings.append(strehl_warning)

        run_summary = {
            "criterion": readback["criterion"],
            "criterion_units": parsed.get("criterion_units"),
            "compensator": compensator,
            "compensators": parsed.get("compensators") or [],
            "cycle_cap": readback["cycle_index"],
            "sampling": readback["sampling"],
            "field_mode": readback["field_mode"],
            "budget_rule_id": rule_id,
            "budget_digest": budget_digest,
            "n_budget_rows": n_budget_rows,
            "topology_digest": topology_digest,
            "nominal": safe_float(parsed["nominal_criterion"]),
            "rss_change": (safe_float(parsed.get("rss_estimated_change"))
                           if mode == "sensitivity" else None),
            "rss_identity": identity,
            "wall_s": readback["wall_s"],
        }
        if cap_s is not None:
            run_summary["cap_s"] = float(cap_s)
            run_summary["cap_source"] = cap_source

        if cb_group_summary is not None:
            run_summary["cb_group"] = cb_group_summary
        elif cb_ctx is not None:
            run_summary["cb_tilt"] = {
                "group": cb_spec["group"], "range_deg": cb_spec["range_deg"],
                "effective_range_deg": cb_ctx.effective_range_deg, "axes": ["x", "y"],
                "layout": {"entry_cb": cb_ctx.layout["entry_cb"], "group": list(cb_ctx.layout["group"]),
                           "dummy_back": cb_ctx.layout["dummy_back"],
                           "return_cb": cb_ctx.layout["return_cb"],
                           "dummy_forward": cb_ctx.layout["dummy_forward"]},
                "zero_tilt_max_abs": cb_ctx.zero_tilt_max_abs,
                "layout_tde_rows": cb_ctx.layout_tde_rows,
                "restored": bool(cb_ctx.restore["restored"]),
            }

        # (12) build the envelope.
        return _finish(_build_envelope(
            mode, parsed, authored, trials, echoed_trials, full, strehl_nominal,
            state_mutated, warnings, output_path, tde_cleared, recon, known_gaps,
            grin_perturbation=grin_perturbation,
            grin_coverage_warning=grin_coverage_warning,
            criterion=criterion, compensator=compensator, run_summary=run_summary,
            hotspots=hotspots, hotspots_withheld=hotspots_withheld,
            compensator_statistics=parsed.get("compensator_statistics"),
            yield_block=(None if mc_threshold is None else _mc_yield_block(
                parsed.get("per_trial"), compensator, at_bound, mc_threshold, _tc._CRITERIA[criterion].member)),
        ))
    except Exception as exc:  # noqa: BLE001 -- on the CB path the restore status rides EVERY failure
        if cb_ctx is None:
            raise
        return _cb_failure_envelope(exc, cb_ctx, warnings, tde_cleared)


def _cb_group_rule_id(rule_id, cb_spec, effective):
    """``<rule>:cbgrp=<group>[@dec<d>mm][@tilt<t>deg]`` on the READ-BACK bounds (Par 2 / Par 4 = last row of a pair)."""
    out = f"{rule_id}:cbgrp={cb_spec['group']}"
    if cb_spec["decenter_mm"] is not None:
        out += f"@dec{format(effective[2], '.12g')}mm"
    if cb_spec["tilt_deg"] is not None:
        out += f"@tilt{format(effective[4], '.12g')}deg"
    return out


_YIELD_KEYS = ("n_trials", "n_pass", "yield", "n_at_bound", "p90")



def _mc_yield_block(values, compensator, at_bound, threshold, criterion_member):
    """The ``yield`` block of a ``monte_carlo`` run under ``threshold``, scored by the picker's ``stats``.

    ``dls_cb_group``: a trial fails when it is at a push bound or has no finite value. Any other compensator has no
    push bound: a trial fails only when it has no finite value. Never a guess: an unpairable list is a named reason.
    """
    from . import _tolerance_pick as _pick
    push = compensator == "dls_cb_group"
    block = {"threshold": threshold, "criterion": criterion_member,
             "failure_rule": "at_push_bound_or_untraceable" if push else "untraceable"}
    if not isinstance(values, list) or not values:
        return dict(block, **{"yield": None, "reason": "no_per_trial"})
    if not push:
        at_bound = [False] * len(values)
    elif (not isinstance(at_bound, list) or len(at_bound) != len(values)
            or not all(isinstance(f, bool) for f in at_bound)):
        return dict(block, **{"yield": None, "reason": "at_bound_unknown"})
    got = _pick.stats(values, at_bound, threshold)
    return dict(block, **_pick._clean({k: got[k] for k in _YIELD_KEYS}))


def _cb_group_report(text, cb_ctx, cb_spec, parsed, full, warnings):
    """``(run_summary.cb_group, per-trial at-bound flags or None)`` for one ``dls_cb_group`` MC report; appends its
    warnings to ``warnings``. The flags are returned whatever ``full`` is (only their emission is gated on it).

    Saturation (per axis, D2) reads the MC statistics blocks' Minimum / Maximum; the at-bound counts and the
    per-trial flags (Amendment B) read the per-trial CPAR lines. Each is ``None`` when unprovable, with a
    warning, never zero-filled.
    """
    cb1, eff = cb_ctx.layout["entry_cb"], cb_ctx.effective
    tail = text[text.index("Monte Carlo Analysis:"):]
    axes = _tc.cpar_saturation(_tc.parse_compensator_statistics(tail, extremes=True), cb1, eff)
    tb = _tc.cpar_trial_bounds(text, cb1, eff, n_expected=parsed.get("parsed_trial_count"))
    for a in axes:
        counts = tb["axes"][a["par"]] if tb is not None else {"n_at_min": None, "n_at_max": None}
        a["n_at_min"], a["n_at_max"] = counts["n_at_min"], counts["n_at_max"]
        label = f"Par# {a['par']} ({a['axis']})"
        if a["saturated"] is True:
            warnings.append(
                f"cb_group_saturated: {label} reached both bounds +-{a['range']:g} {a['units']} in the Monte "
                "Carlo; the result then depends on the declared range, so report that range with the yield")
        elif a["saturated"] is None:
            warnings.append(
                f"cb_group_saturation_unknown: {label} saturation could not be decided (an extremum is "
                "missing or within the 6-decimal print step of the bound, or the range is below it); read "
                "the echoed minimum and maximum")
    if tb is None:
        warnings.append(_CB_GROUP_TRIAL_UNKNOWN)
    summary = {
        "group": cb_spec["group"], "decenter_mm": cb_spec["decenter_mm"], "tilt_deg": cb_spec["tilt_deg"],
        "effective_decenter_mm": eff.get(2), "effective_tilt_deg": eff.get(4),
        "axes": axes,
        "saturated": any(a["saturated"] is True for a in axes),
        "n_trials_at_bound": None if tb is None else tb["n_trials_at_bound"],
        "layout": {"entry_cb": cb1, "group": list(cb_ctx.layout["group"]),
                   "dummy_back": cb_ctx.layout["dummy_back"], "return_cb": cb_ctx.layout["return_cb"],
                   "dummy_forward": cb_ctx.layout["dummy_forward"]},
        "zero_tilt_max_abs": cb_ctx.zero_tilt_max_abs,
        "layout_tde_rows": cb_ctx.layout_tde_rows,
        "restored": bool(cb_ctx.restore["restored"]),
    }
    if full and tb is not None:
        summary["per_trial_at_bound"] = list(tb["at_bound"])
    if cb_ctx.neutralised is not None:      # merit only (spec 3.4, M4): the pinned wavefront key set is unchanged
        summary["merit_neutralised_rows"] = len(cb_ctx.neutralised)
        summary["merit_layout_rel"] = cb_ctx.merit_layout_rel
    return summary, (None if tb is None else list(tb["at_bound"]))


def _cb_restore_fields(ctx):
    r = ctx.restore
    out = {"restored": bool(r["restored"]), "restore": r}
    if ctx.neutralised is not None:         # merit only: a guard failure has no run_summary to carry the count
        out["merit_neutralised_rows"] = len(ctx.neutralised)
        out["merit_layout_rel"] = ctx.merit_layout_rel
    return out


def _engine_busy_envelope(exc, ctx, warnings):
    """The envelope for a capped run whose stop could not be confirmed (spec 3.5). NEVER ``ok:true``.

    ``tolerancing_run`` / ``cap_stop_failed``; nothing was cleaned up (``tde_cleared`` False, ``restored`` False,
    ``state_mutated`` True) and the recovery is to release the engine. On the CB path it carries the ``restore``
    record and the layout, which is STILL in the lens.
    """
    extra = {"restored": False}
    if ctx is not None:
        extra.update(_cb_restore_fields(ctx))
        if ctx.layout is not None:
            extra["layout"] = {"entry_cb": ctx.layout["entry_cb"], "group": list(ctx.layout["group"]),
                               "dummy_back": ctx.layout["dummy_back"], "return_cb": ctx.layout["return_cb"],
                               "dummy_forward": ctx.layout["dummy_forward"]}
    return _ac.error_envelope(
        "tolerance", "tolerancing_run", safe_exc(exc), reason="cap_stop_failed", state_mutated=True,
        tde_cleared=False, warnings=warnings, recovery=_ENGINE_BUSY_RECOVERY, **extra)


def _cb_failure_envelope(exc, ctx, warnings, tde_cleared):
    """The envelope for a failure AFTER the CB layout began: the layout is already torn down.

    ``restored`` is the teardown proof, never an assumption; a lens not proven restored carries the
    reload recovery. The exception's own family / reason are kept (a ``LayoutError`` or a
    ``ToleranceError``); anything else is the unexpected-engine-error family, as ``_never_raise``.
    """
    family = safe_call(lambda: getattr(exc, "family", None), None)
    reason = safe_call(lambda: getattr(exc, "reason", None), None)
    if not isinstance(exc, (_tc.ToleranceError, _tl.LayoutError)):
        family, reason = _ENGINE_ERROR_FAMILY, None
        message = f"tolerance hit an unexpected engine error: {safe_exc(exc)}"
    else:
        message = safe_exc(exc)
    extra = _cb_restore_fields(ctx)
    if reason is not None:
        extra["reason"] = reason
    inner = _ac.error_envelope("tolerance", family or _ENGINE_ERROR_FAMILY, message,
                               state_mutated=not ctx.restore["restored"], tde_cleared=tde_cleared,
                               warnings=warnings, **extra)
    return inner if ctx.restore["restored"] else _cb_unrestored_envelope(inner, ctx, warnings, tde_cleared)


def _cb_unrestored_envelope(result, ctx, warnings, tde_cleared):
    """A finished result over a layout the teardown could not prove removed: NEVER ``ok:true``."""
    failed = ctx.restore["failed_step"]
    return _ac.error_envelope(
        "tolerance", "tolerancing_run",
        "the temporary CB layout could not be proven removed "
        + (f"(step {failed!r} failed)" if failed else "(the post-run state differs from the original)")
        + "; the inner result is under unrestored_result (any numbers in it were measured on a modified lens)",
        reason="teardown_failed" if failed else "restore_unproven",
        state_mutated=True, tde_cleared=tde_cleared, warnings=warnings, recovery=_CB_RECOVERY,
        unrestored_result=result, **_cb_restore_fields(ctx))


def _current_configuration_guarded(system):
    """Read ``system.MCE.CurrentConfiguration`` guarded -> ``None`` on a fault.

    The headless tolerancing run analyzes only the current config (Q12); this is the
    config the TMCO Config#-mismatch WARN is measured against. A non-MCE / wedged read
    degrades to ``None`` (the WARN is then skipped — disclosure, never load-bearing).
    """
    try:
        return int(system.MCE.CurrentConfiguration)
    except Exception:  # noqa: BLE001 — a non-MCE / wedged read -> no mismatch WARN
        return None


def _emit_tmco_config_mismatch_warnings(system, authored, warnings):
    """Append a ``tmco_config_mismatch`` WARN per authored TMCO whose Config# != current.

    The silent-zero guard (MCE): a headless sensitivity run analyzes ONLY
    the current config, so a TMCO whose ``mce_config`` != the run's CurrentConfiguration
    reads a SILENT ZERO sensitivity (it ran + appears in the report, it just bit nothing
    at the analyzed config). NOT a refusal — a TMCO whose Config# == current is NORMAL
    (bites, no warn). A non-MCE / wedged config read skips the check (no false WARN).
    """
    tmco_entries = [
        e for e in (authored or [])
        if isinstance(e, dict) and e.get("type") == "TMCO"
    ]
    if not tmco_entries:
        return
    current = _current_configuration_guarded(system)
    if current is None:
        return
    for e in tmco_entries:
        cfg = e.get("mce_config")
        row = e.get("mce_row")
        if cfg is not None and int(cfg) != current:
            warnings.append(
                f"tmco_config_mismatch: TMCO row {row} targets config {cfg} but the "
                f"tolerancing run analyzed config {current} (the headless engine "
                "analyzes only the current config); this TMCO reads a SILENT ZERO "
                f"sensitivity — set_current_configuration({cfg}) and re-run, or author "
                f"the TMCO for config {current}."
            )


def _authored_summary(authored):
    """The compact per-operand authored summary for the envelope (D12/§3.2)."""
    out = []
    for e in authored:
        row = {"type": e.get("type"), "surface": e.get("surface"),
               "tier": e.get("tier"), "units": e.get("units")}
        if e.get("surface2") is not None:
            row["surface2"] = e["surface2"]
        # GRIN display carry: name the Par# so the escalation envelope's
        # authored_operands / unaccounted_operands identify the coefficient.
        if e.get("param") is not None:
            row["param"] = e["param"]
        out.append(row)
    return out


def _grin_quantity_for_par(info, par):
    """Map an integer Par# to its quantity dict via the RESOLVED ``GrinTypeInfo.params``.

    (GRIN §1.3b) Walks ``info.params`` rows ``(token, "ParN", header, dtype, kind,
    power)``:
      token "n0"        -> base_index_n0
      kind == "coeff"   -> index_profile_coefficient (coefficient = the live Header)
      else (Delta T)    -> trace_step_delta_t (a numerical trace step, NOT an
                           index-profile quantity; never counts toward gradient coverage)
    A Par# beyond THIS type's table -> grin_par_unmapped (disclosed, never silent).
    """
    target = f"Par{par}"
    for row in info.params:
        token = row[0]
        par_str = row[1]
        header = row[2]
        kind = row[4]
        if par_str != target:
            continue
        if token == "n0":
            return {"quantity": "base_index_n0", "par": par, "coefficient": "n0"}
        if kind == "coeff":
            return {"quantity": "index_profile_coefficient", "par": par,
                    "coefficient": header}
        return {"quantity": "trace_step_delta_t", "par": par, "coefficient": header,
                "note": "numerical trace step, NOT an index-profile quantity; does not "
                        "count toward gradient coverage"}
    return {"quantity": "grin_par_unmapped", "par": par}


def _grin_perturbation_disclosures(system, authored):
    """One disclosure entry per authored GRIN Par# perturbation (GRIN §1.3c).

    Returns a list, NEVER raises. One entry per authored op passing the SHARED
    ``is_param_perturbation_op`` predicate (NOT ``parser_secondary_key``,
    which also selects TEDV/CPAR/CEDV/CNPA; a control/compensator with a supplied
    ``param`` must NEVER earn a ``grin_perturbation`` entry) whose LDE row resolves GRIN
    via ``_grin_cells.grin_type_of`` (the AUTHORABLE resolver — the disclosure needs the
    per-type cell map). Non-GRIN -> no entry; a per-op read throw -> skip that op (never
    a false GRIN claim, never fails the run).
    """
    out = []
    for e in authored:
        if not isinstance(e, dict):
            continue
        token = e.get("type")
        if not _cat.is_param_perturbation_op(token):
            continue
        param = e.get("param")
        if param is None:
            continue
        surface = e.get("surface")
        try:
            from . import _grin_cells as _grin  # lazy — circular-import avoidance
            row = system.LDE.GetSurfaceAt(surface)
            key = _grin.grin_type_of(row)     # the resolved GRIN_TYPE_INFO KEY (or None)
            info = _grin.GRIN_TYPE_INFO.get(key) if key is not None else None
        except Exception:  # noqa: BLE001 — a per-op read throw -> skip (no false claim)
            continue
        if info is None:
            continue                       # non-GRIN surface -> no entry
        entry = dict(_grin_quantity_for_par(info, param))
        entry["surface"] = surface
        entry["type"] = token
        out.append(entry)
    return out


# (GRIN §1.3d) the INDETERMINATE coverage string — a NON-EMPTY faults list
# of ANY source (or the inventory throwing) can never read as clean: a shared LDE
# GetSurfaceAt(i) throw is recorded source:"asphere" yet the `continue` skips the GRIN walk
# for that surface too, so a "grin"-scoped filter reads a false clean over an unenumerated
# GRIN surface. The any-fault rule costs nothing (the warning is advisory).
_GRIN_COVERAGE_INDETERMINATE = (
    "grin_tolerance_coverage: the variable inventory could not be fully enumerated; "
    "gradient tolerance coverage is INDETERMINATE for this run — re-run after resolving "
    "the enumeration fault, or author TPAR per optimized GRIN coefficient to be safe."
)


def _grin_surface_covered(system, authored, surface):
    """True iff an authored TPAR bounds a gradient coefficient on ``surface`` (§1.3d).

    Covered iff an authored **TPAR** (TPAI does NOT certify — the brief-literal trigger)
    with ``surface==surface`` has a ``param`` the RESOLVED GRIN map classifies
    ``index_profile_coefficient`` (map-driven via ``_grin_quantity_for_par`` — no {3..8}
    literal; n0 / Delta-T / unmapped never cover). A Type-resolve throw -> not covered
    (advisory-safe: over-warn). NEVER raises.
    """
    try:
        from . import _grin_cells as _grin  # lazy — circular-import avoidance
        row = system.LDE.GetSurfaceAt(surface)
        key = _grin.grin_type_of(row)         # the resolved GRIN_TYPE_INFO KEY (or None)
        info = _grin.GRIN_TYPE_INFO.get(key) if key is not None else None
    except Exception:  # noqa: BLE001 — cannot resolve -> not covered (advisory over-warn)
        return False
    if info is None:
        return False
    for e in authored:
        if not isinstance(e, dict):
            continue
        token = e.get("type")
        # coverage is the FOURTH consumer of the authorable-perturbation
        # predicate — gate on ``is_param_perturbation_op`` (NOT a hand-coded token literal)
        # so a catalog drift (a TPAR row that loses has_minmax / drifts to a control tier)
        # no longer falsely certifies coverage. KEEP the TPAR-only rule (TPAI does NOT
        # certify — the brief-literal trigger) AND inherit the catalog-drift safety.
        if token != "TPAR" or not _cat.is_param_perturbation_op(token):
            continue
        if e.get("surface") != surface:
            continue
        param = e.get("param")
        if param is None:
            continue
        if _grin_quantity_for_par(info, param).get("quantity") \
                == "index_profile_coefficient":
            return True
    return False


def _grin_coverage_warning(system, authored):
    """The ``grin_tolerance_coverage_warning`` string, or None (GRIN §1.3d).

    NEVER raises. Universe: ``_optimize_common._variable_inventory(system, faults=faults)``
    items ``source=="grin"`` (reuse) -> the surfaces with a VARIABLE (optimized)
    index profile. Fault posture (ANY-source fail-closed): a NON-EMPTY faults
    list of ANY source, OR the inventory call itself throwing, -> the INDETERMINATE
    string, NEVER a silent None. Clean (no grin items, no fault) -> None. Uncovered
    surfaces -> a string NAMING each uncovered surface.

    ROUND-13 -- THE OUTER NET NOW FAILS CLOSED TOO. It was ``except Exception: return
    None``, and ``None`` on this channel ALSO means "fully covered" — so a fault in the
    body (the lazy import, the set comprehension, ``_grin_surface_covered``, the join)
    produced a silent all-clear on the exact question the function exists to answer, in
    direct contradiction of the fault posture two paragraphs up. Both siblings already
    fail closed: ``optimize_run._grin_index_audit_warnings`` refuses ``{}`` on a
    total-body throw and returns a static ``grin_index_audit_failed``, and
    ``_grin_surface_covered`` errs toward UNCOVERED. This one now matches them.
    """
    try:
        from . import _optimize_common as _oc  # lazy — circular-import avoidance
        faults = []
        try:
            inv = _oc._variable_inventory(system, faults=faults)
        except Exception:  # noqa: BLE001 — an inventory throw -> INDETERMINATE
            return _GRIN_COVERAGE_INDETERMINATE
        if faults:
            return _GRIN_COVERAGE_INDETERMINATE   # ANY-source fault -> INDETERMINATE
        grin_surfaces = sorted({
            it.get("surface") for it in inv
            if isinstance(it, dict) and it.get("source") == "grin"
            and it.get("surface") is not None
        })
        if not grin_surfaces:
            return None                            # no variable GRIN -> nothing to warn
        uncovered = [s for s in grin_surfaces
                     if not _grin_surface_covered(system, authored, s)]
        if not uncovered:
            return None
        names = ", ".join(str(s) for s in uncovered)
        return (
            f"grin_tolerance_coverage: the optimized (variable) index gradient on "
            f"surface(s) {names} has NO gradient-coefficient TPAR (Par#=3..8) tolerance "
            "authored; the fabrication sensitivity of the index gradient is UNBOUNDED in "
            "this run — author TPAR(surface=S, param=<Par#>) per optimized coefficient "
            "(Par2=n0 is the base index; Par3..Par8 are the profile coefficients)."
        )
    except Exception:  # noqa: BLE001 — never raise; but INDETERMINATE, never a clean None
        return _GRIN_COVERAGE_INDETERMINATE


def _units_by_family(authored):
    """Map each authored FAMILY -> its unit, ONLY where that family AGREES (H-1).

    Returns ``(agreed, ambiguous)``: ``agreed`` is ``{family: unit}`` for the families
    whose authored operands all carry the SAME catalog unit; ``ambiguous`` is
    ``{family: [sorted units]}`` for those that do not.

    ROUND-13 H-1 -- A FAMILY DOES NOT DETERMINE A UNIT, AND THE OLD ROLLUP PICKED BY
    AUTHORING ORDER. It was ``out.setdefault(fam, e["units"])``, so whichever operand of
    a family happened to be authored FIRST won. Measured against the frozen catalog, SIX
    families are ambiguous, not one:

      scalar                 lens_units | dimensionless | fringes   (TRAD/TTHI · TABB/
                                                                     TCON/TCUR/TIND · TFRN)
      surface_tilt_decenter  degrees | lens_units                   (TETX/Y/Z · TEDR/X/Y)
      roll                   degrees | lens_units                   (TARR/X/Y · TRLR/X/Y)
      sag                    degrees | lens_units                   (TSTX/Y · TSDI/R/X/Y)
      parameter              dimensionless | None                   (TPAI/TPAR · TEDV)

    and BOTH default paths walk straight into it: ``_default_tolerances`` authors TRAD +
    TTHI per powered surface and TIND + TABB per glass (all family ``scalar``), while
    ``_default_mechanical`` authors TETX (degrees) beside TEDX (lens_units). So every
    glass system got a wrong physical unit for a family, silently, with ``ok:true``.

    A wrong unit is worse than an absent one, so an ambiguous family is now OMITTED from
    ``units_by_family`` and DISCLOSED under ``units_ambiguous_families``. The
    unambiguous answer was already in the envelope and still is: ``authored_operands[i]
    ["units"]`` is per-OPERAND and always correct — that is the field a consumer should
    read, and the rollup is only ever a convenience over it.
    """
    seen = {}
    for e in authored:
        fam = e.get("family")
        if fam is not None and e.get("units") is not None:
            seen.setdefault(fam, set()).add(e["units"])
    agreed = {fam: sorted(units)[0] for fam, units in seen.items() if len(units) == 1}
    ambiguous = {fam: sorted(units) for fam, units in seen.items() if len(units) > 1}
    return agreed, ambiguous


def _build_envelope(mode, parsed, authored, trials, echoed_trials, full,
                    strehl_nominal, state_mutated, warnings, output_path,
                    tde_cleared=True, recon=None, known_gaps=None,
                    grin_perturbation=None, grin_coverage_warning=None, *,
                    criterion="wavefront", compensator="paraxial", run_summary=None,
                    hotspots=None, hotspots_withheld=None, compensator_statistics=None, yield_block=None):
    """Build the locked SENSITIVITY / MONTE_CARLO result envelope (D8/D9/D11/D12/D13/D16).

    S2a: ``criterion`` / ``compensator`` (the REQUESTED values, defaulting to today's
    wavefront / paraxial so a direct caller of the old signature gets today's envelope),
    ``run_summary`` (both modes), and — sensitivity only — ``hotspots`` /
    ``hotspots_withheld`` (omitted when ``hotspots`` is None = not computed) plus
    ``compensator_statistics`` (dls only). Monte-Carlo carries NEITHER hotspots key.
    """
    recon = recon or {"ran": [], "refused": [], "unaccounted": []}
    units_agreed, units_ambiguous = _units_by_family(authored)
    spec = _tc._CRITERIA[criterion]
    criterion_units = parsed.get("criterion_units") or spec.units or parsed.get("lens_units")
    # The criterion's own words for the note (the wavefront wording is pinned by the
    # back-focus tests: 'already includes per-perturbation paraxial back-focus refocus').
    criterion_label = ("RMS wavefront error in waves" if criterion == "wavefront"
                       else "the loaded merit function value" if criterion == "merit"
                       else f"RMS spot radius in {criterion_units}")
    # D13/G-COMP-INERT + S2a: a user compensator is INERT under Paraxial Focus (false) and
    # PARTICIPATES under Optimize All (DLS) — but only once the report's own header proved
    # the compensator ran (parse_tol_report refuses otherwise), never from the request alone.
    participates = bool(_tc._is_dls(compensator) and parsed.get("compensators"))
    if _tc._is_dls(compensator):
        compensation_note = (
            f"The reported criterion ({criterion_label}) was computed with the back-focus "
            "air gap re-optimized per perturbation by damped least squares "
            "(compensator='dls', cycle cap 3): the wizard's COMP row PARTICIPATES "
            "(compensator_participates=true), proven by the report's own header. The "
            "numbers are refocused. "
            "Do NOT assume as-built performance is better than these numbers on the "
            "back-focus axis, and do not compare them with a compensator='paraxial' run."
        )
    else:
        compensation_note = (
            f"The reported criterion ({criterion_label}) ALREADY includes "
            "per-perturbation paraxial back-focus refocus (engine default). "
            "compensator_participates=false means ONLY user-defined COMP/CPAR operands "
            "are inert headless; it does NOT mean the criterion is un-refocused. "
            "Do NOT assume as-built performance is better than these numbers on the "
            "back-focus axis. Additional compensators (air gaps, element tilt/decenter) "
            "are NOT applied under compensator='paraxial'."
        )
    base = {
        "ok": True,
        "tool": "tolerance",
        "mode": mode,
        "criterion": spec.member,
        "criterion_units": criterion_units,
        "compensation": "dls_back_focus" if _tc._is_dls(compensator) else "paraxial_back_focus",
        "compensator_participates": participates,
        # The reported criterion ALREADY reflects per-perturbation paraxial back-focus
        # refocus (the engine default for a sensitivity/MC run). Derived-constant-true:
        # disclosed so a design agent cannot DOUBLE-COUNT back focus by reading
        # compensator_participates=False as "the numbers are un-refocused, so as-built is
        # better". (Probe: the TTHI+0.3 criterion matches the BEST-FOCUS value, the
        # back-focus-change block is non-zero, and the perturbed criterion can sit BELOW
        # nominal — impossible at a fixed image plane.)
        "criterion_includes_back_focus_refocus": True,
        "compensation_note": compensation_note,
        # D16/G-UNITS-ECHO: echoed from the report "Units are ..." line (never "mm").
        "lens_units": parsed.get("lens_units"),
        "nominal_criterion": safe_float(parsed["nominal_criterion"]),
        "strehl_nominal": safe_float(strehl_nominal) if strehl_nominal is not None
        else None,
        "state_mutated": state_mutated,
        "tde_cleared": tde_cleared,
        "warnings": warnings,
        "report_path": output_path,
        # D12/§3.2 — the no-blind-spot reconcile fields. The INVARIANT: ok:true =>
        # unaccounted_operands == [] (escalation already returned ok:false above).
        "authored_operands": _authored_summary(authored),
        "ran_operands": recon.get("ran", []),
        "refused_operands": recon.get("refused", []),
        "unaccounted_operands": recon.get("unaccounted", []),
        "operand_errors": parsed.get("operand_errors") or [],
        "known_gaps": known_gaps or [],
        # ROUND-13 H-1: only the families whose authored operands AGREE on a unit.
        # A family with a mixed unit set is OMITTED here and named below — the old
        # rollup answered by AUTHORING ORDER, which made every glass default run
        # report a wrong physical unit for family "scalar". Per-operand units are
        # in ``authored_operands`` and are always right.
        "units_by_family": units_agreed,
        "units_ambiguous_families": units_ambiguous,
        # (GRIN §1.3f) the GRIN Par# perturbation disclosures + the per-op reconcile
        # proof fields. ``grin_perturbation`` + coverage are authored-set-derived (both
        # modes); ``reconciliation_mode`` is None in MC (no per-op table — reconcile is a
        # structural no-op there).
        "grin_perturbation": grin_perturbation or [],
        "reconciled_operand_keys": recon.get("reconciled_operand_keys", []),
        "reconciliation_mode": (
            recon.get("reconciliation_mode") if mode == "sensitivity" else None
        ),
    }
    if run_summary is not None:
        base["run_summary"] = run_summary
    if _tc._is_dls(compensator):
        base["compensator_statistics"] = compensator_statistics
    # (GRIN §1.3f) the coverage warning is a CONDITIONAL top-level key (the
    # glass_floor_warning posture) — present ONLY when non-None, never flips ok.
    if grin_coverage_warning is not None:
        base["grin_tolerance_coverage_warning"] = grin_coverage_warning

    if mode == "sensitivity":
        base["sensitivity"] = parsed["sensitivity"]
        base["worst_offenders"] = parsed.get("worst_offenders", [])
        base["rss_estimated_change"] = safe_float(parsed.get("rss_estimated_change"))
        base["rss_estimated_criterion"] = safe_float(
            parsed.get("rss_estimated_criterion")
        )
        base["back_focus_change"] = parsed.get("back_focus_change")
        if hotspots is not None:
            # A ranking is COMPLETE iff hotspots_withheld is null.
            base["hotspots"] = hotspots
            base["hotspots_withheld"] = hotspots_withheld
        return base

    # monte_carlo: STRUCTURAL determinism honesty (D12 — always present, always false).
    base["trials"] = int(echoed_trials) if echoed_trials is not None else int(trials)
    base["deterministic"] = False
    base["seed_supported"] = False
    base["monte_carlo"] = parsed["monte_carlo"]
    if base["trials"] < _tc._MIN_STABLE_TRIALS:
        warnings.append(
            f"trials={base['trials']} (< {_tc._MIN_STABLE_TRIALS}): the Monte-Carlo "
            "statistics may be unstable; increase trials for a reliable estimate"
        )
    if yield_block is not None:
        base["yield"] = yield_block
    if full:
        base["per_trial"] = parsed.get("per_trial", [])
    return base


TOLERANCE_SPEC = ToolSpec(
    name="tolerance",
    handler=tolerance,
    required_params=("mode",),
    param_types={
        "mode": "string",
        "tolerances": "array",
        "trials": "number",
        "full": "boolean",
        "output_file": "string",
        "include_mechanical": "boolean",
        "strict": "boolean",
        "budget": "object",
        "criterion": "string",
        "compensator": "string",
        "sampling": "number",
        "cb_tilt": "object",
        "cb_group": "object",
        "threshold": "number",
        "cap_s": "number",
    },
    description=(
        "Run a tolerance analysis. mode='sensitivity' gives per-parameter both-sign changes, "
        "worst offenders, an RSS estimate and hotspots[]: the rows grouped by element, "
        "cemented group or air gap, ranked by share of the RSS (quadrature) with "
        "contribution in criterion units; join hotspots across runs by key only when "
        "run_summary.topology_digest is equal, and compare runs only when budget_rule_id, "
        "criterion, compensator and sampling are equal. mode='monte_carlo' gives "
        "mean/std/best/worst + cumulative percentiles (trials, default 20; full=true adds "
        "per-trial values; non-deterministic, no seed; threshold adds a yield block, an untraceable or at-push-bound trial failing; capped at 10 s/trial plus 60 s unless cap_s is given, then reason capped, no statistics; an untraceable trial is the stall trigger; criterion='merit' with a DMFS row in the merit and element decenter/tilt rows is refused, merit_dmfs_with_element_tolerances). Both return run_summary. "
        "mode='desensitize' (budget required) is the CONTROL recipe, not the main path: 10 DLS "
        "cycles against the top hotspot's perturbation (TSTX/TSTY/TTHI), then sensitivity again; "
        "it CHANGES the prescription by design (reload to revert) and judges nothing; confirm "
        "with monte_carlo dls. mode='pick_push' (budget, cb_group={decenter_mm}) picks the push compensator: "
        "the 3 eligible top decenter/tilt hotspots each get one capped dls_cb_group Monte Carlo on 'merit' "
        "(trials, default 200; a trial at the push limit fails), ranked by yield under threshold, else by P90; "
        "near ties are flagged and judge_call (MC2000) returned; "
        "1 sensitivity run + up to 3 caps inside a 25 min wall budget. Budget, one "
        "of: budget={vendor, grade, element_decenter, element_tilt} drives the engine "
        "tolerance wizard with element decenter/tilt on; tolerances is a list of {type:<TDE "
        "code>, surface, surface2?, code?, param?, roll_surf?, delta>0 | min+max} (pass a "
        "RESOLVED code, never a phrase; range ops need surface2; TEZI/TEXI REQUIRE "
        "max_term+min_term); or omit both for the legacy default, which has no tilt or "
        "irregularity rows (include_mechanical=true adds a conservative set). Of the 62 "
        "catalogue operands, 36 are perturbation operands: 29 in the real-change tier and 7 "
        "that read a structural zero on a sequential system. Ground tolerance intent via "
        "lookup_operand with domain='tolerance' (the structured tolerance catalog); calling "
        "lookup_operand without domain='tolerance' mis-routes to merit operands. For chapter "
        "context use search_reference. Criterion: 'wavefront' (RMS, waves; default), "
        "'spot' (RMS radius, lens units) or 'merit' (monte_carlo only: the loaded merit function; compare only "
        "on the same merit). Compensator: 'paraxial' (default) refocuses back "
        "focus paraxially and user COMP/CPAR rows are inert; 'dls' (budget required) "
        "optimizes the wizard's back-focus COMP by damped least squares, cycle cap 3; "
        "'dls_cb_tilt' (monte_carlo, budget) adds a group alignment-tilt compensator, "
        "cb_tilt={group: a hotspot key E<a>-<b>|G<a>-<b>, range_deg: your mechanical range, "
        "never from sensitivity}; 'dls_cb_group' (same rules) takes cb_group={group, decenter_mm?, "
        "tilt_deg?}, at least one, lens units / degrees, mechanical: one group's lateral push and/or tilt; "
        "run_summary.cb_group marks an axis saturated when it hits both bounds; "
        "no other compensator is admitted. Every criterion includes back-focus refocus: do not "
        "tell the user as-built is better on that axis. sampling (default 4; 2 "
        "under-resolves) is the engine CriterionSampling. output_file keeps the raw report "
        "(never deleted). Units are per row in authored_operands[].units. A CB/NSC-required "
        "operand is a labeled known_gap (run the rest unless strict=true); TNPA/TNMA never "
        "open. TMCO analyses the current configuration only. GRIN: TPAR/TPAI take param (n0 "
        "is 2, profile coefficients 3..8), disclosed in grin_perturbation. This CLEARS the "
        "Tolerance Data Editor and leaves the prescription untouched (except "
        "mode='desensitize'). Gotcha: tolerance a "
        "design LOADED from disk via load_design; an unsaved in-memory system gives "
        "tolerancing_empty_report. See load_design, optimize, analyze_strehl, "
        "analyze_wavefront."
    ),
)

TOOL_SPECS = (TOLERANCE_SPEC,)
