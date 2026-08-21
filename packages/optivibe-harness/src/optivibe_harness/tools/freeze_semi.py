"""tools/freeze_semi.py — the zoom-FINALIZE helpers (zoom-ergonomics).

TWO dispatchable tools over the BUILT MCE / config-sweep substrate (D-1/D-2):

- ``freeze_semidiameters`` — freeze each optical surface's clear aperture (SemiDiameter)
  to the MAX over all configurations so the element draws at ONE size (a physically-correct
  zoom layout, dogfood gap #5). The probe (``probe_inc6_freeze_semi.py`` Q2) proved a SINGLE
  global LDE Fixed solve HOLDS identical across ALL configs — NO per-config SDIA pin needed.
  For each target surface: sweep configs reading the AUTO SemiDiameter, take the max, write a
  Fixed solve at that max (the #107 ``GetSurfaceCell(SurfaceColumn.SemiDiameter)`` +
  ``SetSolveData(CreateSolveType(SolveType.Fixed))`` idiom — ``set_surface`` does NOT write
  SemiDiameter), and READ-BACK-as-proof that the SemiDiameter reads the SAME value (== max)
  for EVERY config (D-6: the cross-config read, not the value just written). Restores the
  pre-call active config.

- ``verify_zoom`` — READ-ONLY flag (dogfood gap #8, the user's "s13 is the same across the
  whole zoom"): scan ``system.MCE`` THIC rows; a row that is a DECLARED zoom DOF (a per-config
  cell carries a Variable solve) yet whose per-config values are CONSTANT across configs is
  flagged ``constant_but_variable`` (declared to zoom, not zooming). A genuinely-zooming THIC
  is NOT flagged. NEVER mutates, NEVER raises (a wedged MCE -> empty).

NEW error families (D-8, STRING constants via ``error_envelope``): ``freeze_param`` (bad
envelope / surfaces) + ``surface_semi`` (a SemiDiameter write / read-back failure). The
handlers NEVER raise past their boundary (L26).

Live ZOS-API integration: the live verify test; unit-tested against
purpose-built freeze fakes (per-config floating SemiDiameter +
the Fixed-solve pin) and the MCE THIC fakes (the verify_zoom variable/constant matrix).
"""
from ..enums import _resolve_enum
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _config_common as _ccfg
from . import _optimize_common as _oc
from ._analysis_common import error_envelope

_FREEZE_PARAM = "freeze_param"      # bad envelope / surfaces / mode; pre-mutation, ZERO mutation
_SURFACE_SEMI = "surface_semi"      # SemiDiameter write / read-back failure
_ENVELOPES = ("max_over_configs",)  # v1: the only envelope mode
_MODES = ("freeze", "auto")        # S6: freeze (default, byte-identical) vs auto (un-freeze)
_SEMI_TOL = 1e-4                    # the cross-config "holds identical" tolerance (mm)


def _require_dict(params):
    return params if isinstance(params, dict) else {}


def _safe(value):
    """JSON-safe float (NaN/inf -> string sentinel via the tier-wide safe_float)."""
    from .._io import safe_float
    try:
        return safe_float(value)
    except Exception:  # noqa: BLE001 — a non-float passes through verbatim
        return value


# --------------------------------------------------------------------------- #
# Editor-enum resolvers — PROMOTED to the solve substrate.
#
# The two BODIES moved VERBATIM into ``tools/_solve_cells.py``; what remains here are
# two DELEGATES. They are defs that CALL THROUGH, never module-level aliases: an alias
# binds at import time, so a test monkeypatching the substrate would not be seen by this
# module and the two would silently split. Every in-module call site below
# (``:146/:172``, ``:149/:175``, ``:192``) is byte-unchanged.
#
# The direction is freeze_semi -> _solve_cells, i.e. tool -> substrate, which is the
# right way round: freeze_semi is a TOOL module (it imports ``ToolSpec`` above) and the
# substrate imports no tool module at all.
#
# THERE WAS NO CYCLE TO BREAK, and the text that stood here until recently said there
# was — it asserted a live FUNCTION-LOCAL
# ``from .freeze_semi import _surface_column_enum`` in
# ``_clearance_common``, and called the re-point a deferred follow-up. Both
# have since gone false: that import is now a MODULE-level
# ``from . import _solve_cells as _sc``, and the harness rebuilt to prove it measured the
# documented freeze -> clearance cycle NEVER BINDING at module level.
# --------------------------------------------------------------------------- #
def _surface_column_enum(system):
    """Resolve the live ``SurfaceColumn`` enum TYPE (delegates to ``_solve_cells``)."""
    from . import _solve_cells as _sc
    return _sc.surface_column_enum(system)


def _solve_type_enum(system):
    """Resolve the live ``SolveType`` enum TYPE (delegates to ``_solve_cells``)."""
    from . import _solve_cells as _sc
    return _sc.solve_type_enum(system)


# --------------------------------------------------------------------------- #
# freeze_semidiameters.
# --------------------------------------------------------------------------- #
def _read_semi(row):
    """Read ``row.SemiDiameter`` as a finite float, or ``None`` (a degraded read)."""
    import math
    try:
        v = float(row.SemiDiameter)
    except Exception:  # noqa: BLE001
        return None
    return v if math.isfinite(v) else None


def _switch_config(system, cfg):
    """Switch the active config + freshen; RETURN whether the active config IS now ``cfg``.

    A SILENT ``SetCurrentConfiguration`` throw must NOT leave the sweep reading
    the SAME stale config's SemiDiameter for every config (which would hollow the cross-config
    read-back falsifier — a partial engine degrade could pin a too-small aperture while the
    read-back passes). So the caller treats a config whose switch did NOT verify as a DEGRADED
    read (``None``), which fails the freeze closed rather than freezing to a stale value.
    """
    try:
        system.MCE.SetCurrentConfiguration(int(cfg))
        system.UpdateStatus()
    except Exception:  # noqa: BLE001 — a switch fault -> not-verified (the read degrades to None)
        return False
    try:
        return int(system.MCE.CurrentConfiguration) == int(cfg)
    except Exception:  # noqa: BLE001 — an unreadable active config -> not-verified
        return False


def _max_semi_over_configs(system, surface, n_configs):
    """Sweep configs reading surface's AUTO SemiDiameter -> (max, [per-config values]).

    A config whose switch did NOT verify (F-1) contributes ``None`` (a degraded read), NOT the
    stale current-config value — so the cross-config read-back cannot pass on an unvisited config.
    """
    vals = []
    for cfg in range(1, n_configs + 1):
        if not _switch_config(system, cfg):
            vals.append(None)
            continue
        row = system.LDE.GetSurfaceAt(surface)
        vals.append(_read_semi(row))
    finite = [v for v in vals if v is not None]
    return (max(finite) if finite else None), vals


def _write_fixed_semi(system, surface, value):
    """Write a Fixed-solve SemiDiameter (the #107 idiom). Raises SurfaceWriteError on fault."""
    try:
        row = system.LDE.GetSurfaceAt(surface)
        row.SemiDiameter = float(value)
        col_enum = _surface_column_enum(system)
        solve_enum = _solve_type_enum(system)
        col = _resolve_enum(col_enum, "SemiDiameter")
        fixed = _resolve_enum(solve_enum, "Fixed")
        cell = row.GetSurfaceCell(col)
        cell.SetSolveData(cell.CreateSolveType(fixed))
    except ToolParamError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SurfaceWriteError(
            f"could not write a Fixed SemiDiameter on surface {surface}: {exc!r}",
            field="semi_diameter", intended=value, actual=None, surface=surface,
        )


def _write_auto_semi(system, surface):
    """Re-float a frozen SemiDiameter (the symmetric inverse of ``_write_fixed_semi``, S6 §1.2).

    Resolve the SemiDiameter column + the ``SolveType.Automatic`` member (probe: "Automatic"=25;
    "Default" is ABSENT on this enum), then ``cell.SetSolveData(cell.CreateSolveType(Automatic))``
    so the engine recomputes the per-config clear aperture. Does NOT write ``row.SemiDiameter``
    (the value is engine-owned once Automatic — writing it is meaningless). Raises
    ``SurfaceWriteError`` on a fault.
    """
    try:
        row = system.LDE.GetSurfaceAt(surface)
        col_enum = _surface_column_enum(system)
        solve_enum = _solve_type_enum(system)
        col = _resolve_enum(col_enum, "SemiDiameter")
        auto = _resolve_enum(solve_enum, "Automatic")
        cell = row.GetSurfaceCell(col)
        cell.SetSolveData(cell.CreateSolveType(auto))
    except ToolParamError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SurfaceWriteError(
            f"could not re-float (auto) the SemiDiameter on surface {surface}: {exc!r}",
            field="semi_diameter", intended="auto", actual=None, surface=surface,
        )


# --------------------------------------------------------------------------- #
# The SHARED solve-type read (the L30 single locus lives in _clearance_common).
# --------------------------------------------------------------------------- #
def _semi_solve_name(system, surface):
    """The SemiDiameter cell solve-type NAME of ``surface`` ('Fixed'|'Automatic'|...|None)."""
    from . import _clearance_common as _cl
    return _cl.semi_solve_type_name(system, system.LDE, surface)


def _resolve_surfaces(params, n_surfaces):
    """Resolve the target surface list (default interior optical 1..N-2). Raises ToolParamError."""
    requested = params.get("surfaces")
    interior = list(range(1, n_surfaces - 1))
    if requested is None:
        return interior
    if not isinstance(requested, (list, tuple)):
        raise ToolParamError(
            f"surfaces must be a list of surface numbers, got "
            f"{type(requested).__name__} {requested!r}"
        )
    out = []
    for s in requested:
        if isinstance(s, bool) or not isinstance(s, (int, float)):
            raise ToolParamError(f"surfaces entries must be integers, got {s!r}")
        if isinstance(s, float):
            if s != int(s):
                raise ToolParamError(f"surfaces entries must be integers, got {s!r}")
            s = int(s)
        if not (1 <= s <= n_surfaces - 2):
            raise ToolParamError(
                f"surface {s} out of range; freeze targets interior optical surfaces "
                f"1..{n_surfaces - 2} (OBJECT 0 / IMAGE {n_surfaces - 1} excluded)"
            )
        out.append(s)
    # Dedupe while preserving order (a duplicate surface would otherwise be frozen twice,
    # the second pass reading a now-pinned was_floating:False — a misleading double entry).
    seen = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def freeze_semidiameters(session, params):
    """Freeze each optical surface's SemiDiameter to the max over all configs (§1).

    Params: ``envelope`` (default ``"max_over_configs"``, the only v1 mode) + ``surfaces``
    (optional list; default all interior optical surfaces). For each surface, sweep configs
    reading the AUTO SemiDiameter, write a Fixed solve at the MAX, then read back per config
    and PROVE it reads identical (== max) across ALL configs (D-6 cross-config read-back).
    Restores the pre-call active config. NEVER raises past the boundary.

    (S6 §1.1) ``mode`` (default ``"freeze"``, byte-identical) selects: ``"freeze"`` pins the
    SemiDiameter to the max-over-configs; ``"auto"`` RE-FLOATS a frozen SemiDiameter back to the
    engine's per-config automatic solve (the inverse). A bad ``mode`` -> ``freeze_param`` (ZERO
    mutation; validated before any LDE touch).
    """
    params = _require_dict(params)
    system = session.system
    mode = params.get("mode", "freeze")
    if mode not in _MODES:
        return error_envelope(
            "freeze_semidiameters", _FREEZE_PARAM,
            f"mode must be one of {list(_MODES)}, got {mode!r}")
    try:
        if mode == "auto":
            return _unfreeze_impl(system, params)
        return _freeze_impl(system, params)
    except ToolParamError as exc:
        return error_envelope("freeze_semidiameters", _FREEZE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        # freeze/auto raise ONLY their own SemiDiameter write / read-back SurfaceWriteErrors ->
        # the DISTINCT surface_semi family (not the default surface_write the exc carries).
        return error_envelope("freeze_semidiameters", _SURFACE_SEMI, str(exc))
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary (L26)
        return error_envelope(
            "freeze_semidiameters", _SURFACE_SEMI,
            f"unexpected fault ({mode}) freezing/unfreezing semi-diameters ({exc!r})")


def _freeze_impl(system, params):
    envelope = params.get("envelope", "max_over_configs")
    if envelope not in _ENVELOPES:
        raise ToolParamError(
            f"envelope must be one of {list(_ENVELOPES)}, got {envelope!r}")
    n_surfaces = int(system.LDE.NumberOfSurfaces)
    targets = _resolve_surfaces(params, n_surfaces)
    n_configs = _ccfg.safe_number_of_configurations(system)
    active_before = _ccfg.safe_current_configuration(system)

    frozen = []
    skipped = []
    try:
        for surface in targets:
            max_semi, pre_vals = _max_semi_over_configs(system, surface, n_configs)
            if max_semi is None:
                skipped.append({"surface": surface, "reason": "no finite SemiDiameter read"})
                continue
            finite_pre = [v for v in pre_vals if v is not None]
            was_floating = (len(finite_pre) >= 2
                            and (max(finite_pre) - min(finite_pre)) > _SEMI_TOL)
            _write_fixed_semi(system, surface, max_semi)
            # READ-BACK-as-proof (D-6): the SemiDiameter holds == max across ALL configs.
            _, post_vals = _max_semi_over_configs(system, surface, n_configs)
            holds = all(
                v is not None and abs(v - max_semi) <= _SEMI_TOL for v in post_vals)
            if not holds:
                # F-2: disclose the surfaces ALREADY frozen before this failure (a Fixed solve
                # at the max is a benign, valid state) alongside the failing surface.
                return error_envelope(
                    "freeze_semidiameters", _SURFACE_SEMI,
                    f"froze surface {surface} SemiDiameter to {max_semi} but it did NOT "
                    f"hold across configs (read back {post_vals}); the engine re-floated it "
                    "(or a config switch did not verify) — refusing rather than claiming a "
                    "frozen aperture",
                    failed_surface=surface,
                    frozen=frozen,
                    n_configs=n_configs,
                )
            frozen.append({
                "surface": surface,
                "semi_diameter": max_semi,
                "was_floating": bool(was_floating),
            })
    finally:
        _switch_config(system, active_before)

    return {
        "ok": True,
        "envelope": envelope,
        "n_configs": n_configs,
        "frozen": frozen,
        "skipped": skipped,
        "active_configuration_restored": _ccfg.safe_current_configuration(system),
    }


# --------------------------------------------------------------------------- #
# freeze_semidiameters mode="auto" — the UN-freeze (S6 §1.4).
# --------------------------------------------------------------------------- #
def _unfreeze_impl(system, params):
    """Re-float each target surface's SemiDiameter to the engine's per-config auto solve (§1.4).

    Symmetric with ``_freeze_impl`` (resolves its OWN targets/n_configs/active_before). For each
    target surface: read the PRE solve type; SKIP a degraded (``None``) solve read (fail-open,
    never write blindly); WHITELIST-by-INCLUSION (H-1) — write Automatic ONLY when the PRE solve
    is ``Fixed`` (the frozen case) or ``Automatic`` (idempotent), SKIP+disclose EVERY other solve
    type (``Variable`` an optimizer DOF, a ``Pickup``/``Position``/other relationship — overwriting
    it with Automatic would silently DESTROY it); then write the Automatic solve and read-back-PROVE
    the solve type now reads ``Automatic`` (the UNIVERSAL gate
    — every system, single/multi-config). A corroborating per-config-DIFFERS disclosure runs on a
    multi-config system (NEVER a gate — a single-config / non-zooming surface legitimately does not
    differ). Restores the pre-call active config.
    """
    n_surfaces = int(system.LDE.NumberOfSurfaces)
    targets = _resolve_surfaces(params, n_surfaces)
    n_configs = _ccfg.safe_number_of_configurations(system)
    active_before = _ccfg.safe_current_configuration(system)

    unfrozen = []
    skipped = []
    try:
        for surface in targets:
            pre = _semi_solve_name(system, surface)
            if pre is None:
                # A degraded solve read (fail-open): skip, do NOT write blindly.
                skipped.append({"surface": surface, "reason": "degraded_solve_read"})
                continue
            # H-1 whitelist-by-INCLUSION (the L26-sibling fix): mode='auto' writes Automatic
            # ONLY when the PRE solve is Fixed (the frozen case the unfreeze is FOR) or
            # Automatic (a harmless idempotent no-op). EVERY OTHER solve type — Variable
            # (an optimizer DOF), Pickup/Position/any non-Fixed/Automatic relationship — is
            # SKIPPED + disclosed (overwriting it with Automatic would SILENTLY DESTROY that
            # solve, the same silent-drop class as the Variable guard). A whitelist-by-EXCLUSION
            # (skip only Variable) would have re-introduced the silent drop on a Pickup solve.
            if pre not in ("Fixed", "Automatic"):
                if pre == "Variable":
                    note = (f"surface {surface} SemiDiameter is a Variable (optimizer DOF) — "
                            "NOT unfrozen (mode=auto would drop the variable solve); clear it "
                            "with clear_variable first if you want it auto")
                    reason = "variable"
                else:
                    note = (f"surface {surface} SemiDiameter carries a non-Fixed solve "
                            f"({pre!r}) — NOT unfrozen (mode=auto would overwrite the "
                            f"{pre} solve with Automatic, silently destroying it); preserved")
                    reason = "non_fixed_solve"
                skipped.append({"surface": surface, "reason": reason,
                                "solve_type": pre, "note": note})
                continue

            _write_auto_semi(system, surface)               # raises SurfaceWriteError on fault
            # READ-BACK-as-proof (the UNIVERSAL gate, F-A/F-B): the solve type now reads
            # Automatic. THIS is the commit gate (every system, single/multi-config).
            now = _semi_solve_name(system, surface)
            if now != "Automatic":
                return error_envelope(
                    "freeze_semidiameters", _SURFACE_SEMI,
                    f"wrote an Automatic solve on surface {surface}'s SemiDiameter but it "
                    f"read back {now!r} (NOT 'Automatic'); the engine ignored the re-float "
                    "(or the cell is wedged) — refusing rather than claiming a re-floated "
                    "aperture",
                    failed_surface=surface, unfrozen=unfrozen, n_configs=n_configs,
                )
            # CORROBORATOR (multi-config only, DISCLOSURE — NEVER the gate, F-B): does the
            # per-config aperture resume differing? A single-config / non-zooming surface
            # legitimately does NOT differ — so this NEVER refuses; it only discloses.
            _, post_vals = _max_semi_over_configs(system, surface, n_configs)
            finite_post = [v for v in post_vals if v is not None]
            refloated_differs = (
                len(finite_post) >= 2 and (max(finite_post) - min(finite_post)) > _SEMI_TOL
            )
            unfrozen.append({
                "surface": surface,
                "was_frozen": bool(pre == "Fixed"),
                "solve_after": "Automatic",
                "refloated_differs": (bool(refloated_differs) if n_configs >= 2 else None),
                "per_config_semi": [_safe(v) for v in post_vals],
            })
    finally:
        _switch_config(system, active_before)

    return {
        "ok": True,
        "mode": "auto",
        "n_configs": n_configs,
        "unfrozen": unfrozen,        # mirrors the freeze ``frozen`` list shape
        "skipped": skipped,
        "active_configuration_restored": _ccfg.safe_current_configuration(system),
    }


# --------------------------------------------------------------------------- #
# verify_zoom (read-only).
# --------------------------------------------------------------------------- #
def verify_zoom(session, params):
    """Flag a declared-variable THIC that is CONSTANT across configs (§2, read-only).

    Scans ``system.MCE`` THIC rows; a row whose per-config cells carry a Variable solve yet
    whose per-config values are identical across configs is flagged ``constant_but_variable``
    (declared to zoom but not zooming — the user's catch). A genuinely-zooming THIC (values
    differ) is NOT flagged. NEVER mutates, NEVER raises (a wedged MCE / missing row -> empty).
    """
    import math
    system = session.system
    mce = getattr(system, "MCE", None)
    rows = []
    if mce is None:
        return {"ok": True, "thic_rows": [], "flagged": []}
    try:
        n_operands = int(mce.NumberOfOperands)
        n_configs = int(mce.NumberOfConfigurations)
    except Exception:  # noqa: BLE001
        return {"ok": True, "thic_rows": [], "flagged": []}
    if n_operands <= 0 or n_configs <= 0:
        return {"ok": True, "thic_rows": [], "flagged": []}

    try:
        variable_member = _oc._solve_type_variable_enum(system)
    except Exception:  # noqa: BLE001 — without the Variable member, treat nothing as variable
        variable_member = None

    for row in range(1, n_operands + 1):
        try:
            op = mce.GetOperandAt(row)
            # ROUND-12 (H-3 triage, owned file). BASE SLOT on the row-type token: a bare
            # ``str(...)`` lets a forging ``TypeName`` decide which MCE rows this scan
            # even looks at — a real THIC forging something else drops out of the zoom
            # audit entirely (the user's catch goes silent), and a non-THIC forging THIC
            # gets flagged ``constant_but_variable``.
            if _oc._base_token(op.TypeName) != "THIC":
                continue
        except Exception:  # noqa: BLE001
            continue
        surface = None
        try:
            surface = int(op.Param1)
        except Exception:  # noqa: BLE001
            surface = None
        values = []
        is_variable = False
        for cfg in range(1, n_configs + 1):
            try:
                cell = op.GetOperandCell(cfg)
                values.append(float(cell.DoubleValue))
            except Exception:  # noqa: BLE001
                values.append(None)
                continue
            # ROUND-12 F-5. THIS GUARD IS ITS OWN `try`, NOT AN EXTENSION OF THE ONE
            # ABOVE, and the reason is a bug the obvious re-indent would have shipped:
            # the handler above does `values.append(None)`, so folding this line into it
            # would append a SECOND entry for a config whose value read had already
            # succeeded, silently desynchronising `per_config_values` from the config
            # index. `verify_zoom`'s docstring says "NEVER mutates, NEVER raises"; this
            # call was the only unguarded thing between it and that promise.
            try:
                if variable_member is not None and _oc._cell_is_variable(
                        cell, variable_member):
                    is_variable = True
            except Exception:  # noqa: BLE001 — read-only scan: an unreadable solve is not Variable
                pass
        finite = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
        constant = (len(finite) >= 2 and (max(finite) - min(finite)) < 1e-6)
        flagged = bool(is_variable and constant)
        rows.append({
            "row": row,
            "surface": surface,
            "per_config_values": values,
            "is_variable": is_variable,
            "constant_but_variable": flagged,
        })

    return {
        "ok": True,
        "thic_rows": rows,
        "flagged": [r["row"] for r in rows if r["constant_but_variable"]],
    }


FREEZE_SEMIDIAMETERS_SPEC = ToolSpec(
    name="freeze_semidiameters",
    handler=freeze_semidiameters,
    required_params=(),
    param_types={"envelope": "string", "surfaces": "array", "mode": "string"},
    description=(
        "Freeze each optical surface's clear aperture (SemiDiameter) to the MAX over all "
        "configurations so a zoom's elements draw at ONE size (a physically-correct layout) "
        "— a finalize step before presenting a multi-config design. Writes a Fixed "
        "SemiDiameter solve (read-back proven to hold identical across every config). "
        "envelope defaults to max_over_configs; surfaces defaults to all interior surfaces. "
        "Pass mode='auto' to RE-FLOAT (un-freeze) the apertures back to the engine's "
        "per-config automatic solve (the inverse of the default freeze). "
        "See verify_zoom, describe_configurations, render_layout."
    ),
)

VERIFY_ZOOM_SPEC = ToolSpec(
    name="verify_zoom",
    handler=verify_zoom,
    required_params=(),
    param_types={},
    description=(
        "Check a multi-config zoom for a declared-variable thickness that is CONSTANT "
        "across configs (a gap declared to zoom but not actually zooming). Read-only; "
        "returns each per-config THIC row + the flagged rows. See describe_configurations, "
        "set_zoom, freeze_semidiameters."
    ),
)

TOOL_SPECS = (FREEZE_SEMIDIAMETERS_SPEC, VERIFY_ZOOM_SPEC)
