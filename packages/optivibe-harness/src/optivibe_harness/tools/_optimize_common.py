"""tools/_optimize_common.py — private shared helpers for the optimize tools.

NOT dispatchable (no ``TOOL_SPEC``). The analog of ``_analysis_common`` /
``_lens_common``: the probe-grounded optimizer-lifecycle / preflight / verdict /
live-enum code lives in exactly one place so the seven dispatchable optimize
tools (``tools/optimize_*.py``) reuse it.

- ``_preflight(system)`` — the NON-MUTATING dry-run gate (§e): counts LDE
  variable cells (``GetSolveData().Type == SolveType.Variable``) and checks the
  merit (``NumberOfOperands > 0`` AND a finite ``CalculateMeritFunction() > 0``).
  Opens NO optimizer. Returns ``(ok, family, variables, number_of_operands,
  merit)``.
- ``_count_variables(lde, variable_member, system=None)`` — scan the LDE for cells
  set Variable (Radius/Thickness; with ``system`` ALSO asphere coefficient Par cells).
- ``classify_verdict(before, after, ...)`` — the verdict classifier (§d),
  diverged-on-non-finite FIRST (on the RAW float, before ``safe_float``), then the
  ``math.isclose`` rel/abs tolerance gate. A no-op reads ``"stable"``, never
  ``"improved"``.
- ``_optimizer_session(...)`` — the open-once / configure / ``Close()``-in-finally
  optimizer context manager (§c — the L22 single-seat reap). A ``None``
  open is the ``optimize_unavailable`` signal (the manager never enters the
  try/finally on a ``None`` handle — there is nothing to Close).
- ``_readback_disagreement(...)`` — the tripwire (a TIGHTER tol than the
  verdict; non-fatal, never gates the verdict).
- the live-enum resolvers (``MeritOperandType`` / ``OptimizationAlgorithm``) via
  the ``_enum_types`` injection seam.

The ``error_envelope`` helper is the SAME one — re-exported from
``_analysis_common`` so the optimize tools build the ``{ok:false, error_family,
error, tool}`` failure dict the same way.

Live ZOS-API integration: exercised by the live closed-loop test; unit-tested
here against the fixture-seeded MUTATING fake optimizer.
"""
import math
from contextlib import contextmanager

from .._io import safe_float
from ..enums import _resolve_enum
from ..errors import OptimizeError, SurfaceWriteError, ToolParamError
from ._analysis_common import error_envelope  # noqa: F401 — re-export for the optimize tools
from ._structural_common import _INDETERMINATE, _classify_stop
from . import _merit_cells  # the type-aware MFE param reader (acyclic — _merit_cells
                            # imports only math/errors/_lens_common; none import _optimize_common)

# Verdict tolerance: a combined relative + absolute tolerance, the
# ``_readback_ok`` shape. Merit values are ~0.003, so an abs-only floor would
# mis-classify float jitter; the rel floor (1e-4) is the "is this delta real"
# gate and the abs floor (1e-12) is the merit-near-zero safety net.
_VERDICT_REL_TOL = 1e-4
_VERDICT_ABS_TOL = 1e-12

# The readback tripwire is a TIGHTER "should be bit-identical" check
# (the optimizer's CurrentMeritFunction and the MFE recompute match
# to full precision) — NOT the verdict's "is the move real" tolerance.
_READBACK_REL_TOL = 1e-6
_READBACK_ABS_TOL = 1e-12

# A FINITE merit >= this ceiling is the engine's "could-not-compute" band: the
# CalculateMeritFunction() 9e9 sentinel (a ray FAILS to trace -> the whole merit is
# flagged uncomputable) and undefined first-order operands (EFFL/TOTR ~1e10). Real RMS
# merits of buildable designs are NEVER >= 1e9 (probe assertion; the live gate pins the
# boundary). ONE locus, ONE literal (L30/L32) — the opt.IsValid branch reuses the SAME
# family constant, never a re-derived threshold.
_MERIT_UNCOMPUTABLE_CEILING = 1e9

# The cap on how many failing rows the uncomputable-envelope enumeration lists.
# A LOCKED constant (NOT a tool param — no new schema surface to validate). ``n_suspects``
# reports the FULL suspect count even when it exceeds this cap.
_UNCOMPUTABLE_ROW_CAP = 12


def _merit_is_uncomputable(merit):
    """True iff ``merit`` is a FINITE number >= the could-not-compute ceiling.

    The 9e9 sentinel (Zemax's "could-not-compute" merit) and ~1e10 undefined-first-order
    operands land here; a buildable design's RMS merit never does (probe). DISJOINT from
    ``no_merit``: a NON-number / NON-finite / <=0 merit is NOT "uncomputable" here — the
    existing ``no_merit`` gate owns those (it runs FIRST). Guarded so a non-number degrades
    to ``False`` (never raises out of the NON-MUTATING preflight count).

    The ``1e9`` ceiling cannot false-refuse a real design: ``CalculateMeritFunction()``
    returns a WEIGHT-NORMALIZED RMS — ``sqrt(Σwᵢ(vᵢ−tᵢ)² / Σwᵢ)`` — NOT an unnormalized
    operand sum, so a many-operand design does not accumulate into the ``>= 1e9`` band no
    matter how many operands it carries. Only the ``9e9`` could-not-compute sentinel and
    the ~``1e10`` undefined-first-order operands reach the ceiling.
    """
    if isinstance(merit, bool) or not isinstance(merit, (int, float)):
        return False
    if not math.isfinite(merit):
        return False
    return merit >= _MERIT_UNCOMPUTABLE_CEILING


# --------------------------------------------------------------------------- #
# Live-enum resolvers (the ``_enum_types`` injection seam).
# --------------------------------------------------------------------------- #
def _merit_operand_enum(system):
    """Resolve the live ``MeritOperandType`` enum TYPE.

    A fake system injects ``_enum_types["MeritOperandType"]`` so unit tests resolve
    without the backend; otherwise the live ``ZOSAPI.Editors.MFE`` namespace is
    imported. A resolution failure surfaces as a ``ToolParamError`` (a param-class
    problem), never an internal crash.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "MeritOperandType" in injected:
        return injected["MeritOperandType"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.MFE as _mfe  # type: ignore

        return _mfe.MeritOperandType
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve MeritOperandType from ZOSAPI.Editors.MFE: {exc}"
        )


def _optimization_algorithm_enum(system):
    """Resolve the live ``OptimizationAlgorithm`` enum TYPE.

    Injected via ``_enum_types["OptimizationAlgorithm"]`` for unit tests; otherwise
    the live ``ZOSAPI.Tools.Optimization`` namespace.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "OptimizationAlgorithm" in injected:
        return injected["OptimizationAlgorithm"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Tools.Optimization as _opt  # type: ignore

        return _opt.OptimizationAlgorithm
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            "could not resolve OptimizationAlgorithm from "
            f"ZOSAPI.Tools.Optimization: {exc}"
        )


# The discrete fixed-cycle members the live ``OptimizationCycles`` enum exposes
# (the enum has NO arbitrary-int member — the cycle count is chosen
# from this fixed ladder). ``RunAndWaitForCompletion()`` takes NO arguments; the
# count is set on ``opt.Cycles`` BEFORE the run. An int ``cycles`` param snaps to
# the nearest fixed rung (tie -> the larger, more thorough run).
_FIXED_CYCLE_LADDER = ((1, "Fixed_1_Cycle"), (5, "Fixed_5_Cycles"),
                       (10, "Fixed_10_Cycles"), (50, "Fixed_50_Cycles"))


def _optimization_cycles_enum(system):
    """Resolve the live ``OptimizationCycles`` enum TYPE.

    Injected via ``_enum_types["OptimizationCycles"]`` for unit tests; otherwise the
    live ``ZOSAPI.Tools.Optimization`` namespace. A resolution failure surfaces as a
    ``ToolParamError`` (a param-class problem), never an internal crash.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "OptimizationCycles" in injected:
        return injected["OptimizationCycles"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Tools.Optimization as _opt  # type: ignore

        return _opt.OptimizationCycles
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            "could not resolve OptimizationCycles from "
            f"ZOSAPI.Tools.Optimization: {exc}"
        )


def _cycles_member_name(cycles):
    """Snap an int ``cycles`` count to the nearest fixed ``OptimizationCycles`` rung.

    The live enum has only ``Fixed_{1,5,10,50}_Cycles`` (plus ``Automatic`` /
    ``Infinite``, not used here). An exact rung maps directly; anything between snaps
    to the nearest rung (ties break to the larger / more thorough run). Returns the
    member NAME (resolved against the live enum TYPE by ``_resolve_cycles_member``).
    """
    best_count, best_name = _FIXED_CYCLE_LADDER[0]
    for count, name in _FIXED_CYCLE_LADDER[1:]:
        dist = abs(count - cycles)
        best_dist = abs(best_count - cycles)
        # Nearest rung; a tie breaks to the LARGER count (the more thorough run),
        # which the ascending ladder gives us via ``<=``.
        if dist <= best_dist:
            best_count, best_name = count, name
    return best_name


def _resolve_cycles_member(system, cycles):
    """Resolve the int ``cycles`` count to a live ``OptimizationCycles`` MEMBER.

    Snaps the count to the nearest fixed rung (``_cycles_member_name``) and resolves
    that member off the live enum TYPE. A resolution failure surfaces as a
    ``ToolParamError``. Returns ``(member, member_name)``.
    """
    enum_type = _optimization_cycles_enum(system)
    name = _cycles_member_name(cycles)
    member = _resolve_enum(enum_type, name)
    return member, name


def _solve_type_variable_enum(system):
    """Resolve the live ``SolveType.Variable`` member.

    A fake system injects ``_enum_types["SolveType"]`` (a FakeEnum with a
    ``Variable`` member); otherwise the live ``ZOSAPI.Editors.SolveType`` namespace.
    Returns the ``Variable`` member so a cell's ``GetSolveData().Type`` can be
    compared against it. A resolution failure surfaces as a ``ToolParamError``.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "SolveType" in injected:
        return _resolve_enum(injected["SolveType"], "Variable")
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors as _ed  # type: ignore

        return _ed.SolveType.Variable
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve SolveType.Variable from ZOSAPI.Editors: {exc}"
        )


def _min_positive_target(mfe, token):
    """The MIN strictly-positive finite ``Target`` among LIVE MFE rows typed ``token``.

    The ONE shared operand-target reader consumed by BOTH the (a)
    build-time warning (a positive floor present ⟺ floored) and the (d) post-optimize
    audit's floor source (§D-FLOOR OPTION-ii / MIN). Scans the LIVE MFE for operands
    whose ``TypeName == token`` and whose ``Target`` is a positive finite float;
    returns the MIN such target, or ``None`` when none is readable.

    NEVER raises. Per-row guarded (a flaky row is skipped, never counted); a total
    scan failure (``NumberOfOperands`` throws) -> ``None``. ``None`` ⟺ "no positive
    floor of this token is readable" -> unfloored for (a) (warn, fail-closed) / use
    the fallback for (d). An inert ``Target 0`` floor does NOT count (the L28 bite).
    """
    try:
        n = int(mfe.NumberOfOperands)
    except Exception:  # noqa: BLE001 — total scan failure -> fail-safe None
        return None
    best = None
    for i in range(1, n + 1):
        try:
            op = mfe.GetOperandAt(i)
            if str(op.TypeName) != token:
                continue
            t = safe_float(op.Target)  # nan/inf -> non-numeric string sentinel
            if (
                isinstance(t, (int, float))
                and not isinstance(t, bool)
                and math.isfinite(t)
                and t > 0.0
            ):
                best = t if best is None else min(best, t)
        except Exception:  # noqa: BLE001 — unreadable row -> skip (NEVER count)
            continue
    return best


# --------------------------------------------------------------------------- #
# Variable counting + the preflight gate (§e).
# --------------------------------------------------------------------------- #
def _cell_is_variable(cell, variable_member):
    """True if ``cell``'s solve is Variable.

    Reads ``cell.GetSolveData().Type`` and compares it to the live
    ``SolveType.Variable`` member. The comparison is done on the member's STRING
    name (the live .NET enum member str() is the member name) so a
    fake enum member and a live one compare identically. Guarded: a cell without a
    solve / a transient proxy gap degrades to ``False`` rather than raising — a
    counting gap must not crash the preflight.
    """
    try:
        solve = cell.GetSolveData()
        solve_type = solve.Type
    except Exception:  # noqa: BLE001 — a cell with no solve is simply not Variable
        return False
    return str(solve_type) == str(variable_member)


def _cell_is_variable_failclosed(cell, variable_member, surface, token):
    """Fail-CLOSED variant of ``_cell_is_variable`` for the apply RESET DETECTION (BUG-1).

    ``_cell_is_variable`` swallows a ``GetSolveData()`` throw (``return False``) — correct for
    the NON-MUTATING inventory/count paths (fail-open-to-not-Variable is the documented
    contract there). But the apply reset DETECTS-then-MUTATES: if detection swallows a wedged
    solve-read it SILENTLY treats a genuinely-Variable cell as not-Variable, SKIPS it, and the
    stale Variable SURVIVES the apply with ``applied:True`` and no disclosure — re-entering the
    curved-stop silent-wrong on a transient read hiccup, and shadowing the fail-closed proof
    path entirely.

    So the RESET detection fails CLOSED: a ``GetSolveData()`` throw PROPAGATES as
    ``SurfaceWriteError`` (-> the apply's atomic rollback). A clean read returns the
    Variable/not-Variable bool. ``surface``/``token`` are for the diagnostic message. Do NOT
    use this for the inventory/count callers — they want the fail-open contract.
    """
    try:
        solve_type = cell.GetSolveData().Type
    except Exception as exc:  # noqa: BLE001 — a wedged solve-read on a reset surface -> rollback
        raise SurfaceWriteError(
            f"could not read the {token} solve on surface {surface} during the reset scan "
            f"({exc!r}); the solve state is unverifiable — refusing rather than silently "
            "skipping a possibly-Variable solve",
            field=f"{token} solve", intended="detect", actual=None, surface=surface,
        ) from exc
    return str(solve_type) == str(variable_member)


def _cell_is_double(cell):
    """True iff ``cell.DataType`` reads the string ``"Double"`` (the continuous-DOF gate).

    The SAME Int/Double discriminator ``_mce_cells`` uses (the live cell exposes
    ``DataType`` as the member str ``"Double"``/``"Integer"``). The optimizer counts ONLY a
    Double cell's Variable solve as a real continuous DOF; the engine SILENTLY accepts
    ``MakeSolveVariable()`` on an Integer cell (the Q5 phantom-DOF trap) but the optimizer
    does NOT count it. GUARDED: a ``.DataType`` read throw degrades to ``False`` (skip the
    cell, never raise — this runs inside the NON-MUTATING preflight count).
    """
    try:
        return str(cell.DataType) == "Double"
    except Exception:  # noqa: BLE001 — an unreadable DataType -> not a countable Double DOF
        return False


def _safe_cell_value(cell):
    """Read a cell's numeric value GUARDED -> ``float`` or ``None`` (the inventory value).

    The inventory item carries a ``value`` for diagnostics only — a value read that throws
    (a wedged cell, a missing accessor) degrades to ``None`` and the item is KEPT (the
    value-read-failure rule: an item exists because the SOLVE is Variable,
    independent of whether its value reads back; the count must equal ``opt.Variables``
    regardless of a value-read hiccup). Tries ``DoubleValue`` then ``Value`` then ``int(...)``.
    A non-finite reads as ``None`` (a sentinel-ish value is not a useful diagnostic).
    """
    for attr in ("DoubleValue", "Value"):
        try:
            raw = getattr(cell, attr)
        except Exception:  # noqa: BLE001 — the wrong accessor / a wedged read -> try next
            continue
        try:
            v = float(raw)
        except Exception:  # noqa: BLE001 — a non-numeric value -> not a useful diagnostic
            return None
        return v if math.isfinite(v) else None
    return None


def _record_enumeration_fault(faults, source, reason):
    """Record a per-source DISCOVERY fault for the inventory's completeness signal.

    ``faults`` is an OPTIONAL mutable list threaded by the inventory consumers that need the
    completeness contract (``list_variables`` / ``clear_all_variables`` / the disclosure
    helper). When ``faults is None`` (the legacy counter path) this is a no-op — so the three
    preflight counters' fail-safe-skip behavior is byte-IDENTICAL (the count contract is
    untouched; only the NEW tools surface the fault). A discovery fault means a whole SOURCE's
    (or surface's) coverage was silently dropped — so a genuinely-Variable cell behind the
    fault is invisible to BOTH the walk AND a re-enumerate read-back proof (the silent
    ``cleared_all:true`` over a faulted enumeration).
    """
    if faults is None:
        return
    faults.append({"source": source, "reason": reason})


def _enumerate_asphere_variables(system, surf, variable_member, faults=None):
    """Emit the inventory ITEMS for a surface's ASPHERE COEFFICIENT Par cells (fail-safe).

    The itemizing TWIN of ``_count_asphere_variables`` (the count is now
    ``len(_enumerate_asphere_variables(...))``, so the count and the inventory cannot
    diverge by construction). Uses the SAME ``_asphere_cells`` ORDER MAP + the LIVE Max-Term
    gate for gated types (covers Even/Odd Par1..8 AND heavy-asphere gated
    Par15+), NEVER a 254-column brute scan. Returns a list of ``source=="asphere"`` items
    (shape: ``surface`` / ``term`` / ``par`` / ``value`` / ``solve``); possibly
    empty; NEVER raises (every per-cell read guarded exactly as the counter is — a wedged
    cell / Type-read throw contributes nothing).

    ``faults`` (optional): when a list is passed, a per-surface DISCOVERY fault (the asphere
    ``asphere_type_of`` / gate read deterministically throwing -> the surface's asphere
    coverage silently dropped) is RECORDED. ``faults is None`` (the counters) -> the
    fault is silently skipped exactly as before (byte-identical count contract).
    """
    from . import _asphere_cells as _asph

    items = []
    try:
        info = _asph.asphere_type_of(surf)
    except Exception:  # noqa: BLE001 — a Type-read throw -> not countable, never crash
        _record_enumeration_fault(
            faults, "asphere",
            f"asphere type discovery threw on surface {_safe_surface_index(surf)}",
        )
        return items
    if info is None:
        return items
    type_info = _asph.ASPHERE_TYPE_INFO.get(info)
    if type_info is None:
        return items
    # Determine how many coefficient cells are materialized: the live Max-Term gate for
    # the gated (Extended) types, else the fixed per-type term count. Every read is
    # guarded — a wedged gate / a Type-read hiccup contributes nothing.
    try:
        if type_info.gated:
            n_terms = _asph.read_gate_cell(system, surf, type_info)
        else:
            n_terms = type_info.max_terms
    except Exception:  # noqa: BLE001 — an unreadable gate -> count nothing, never crash
        _record_enumeration_fault(
            faults, "asphere",
            f"asphere Max-Term gate read threw on surface {_safe_surface_index(surf)} "
            "(gated coefficient coverage dropped)",
        )
        return items
    try:
        n_terms = int(n_terms)
    except Exception:  # noqa: BLE001 — a non-integral term count -> count nothing
        _record_enumeration_fault(
            faults, "asphere",
            f"asphere Max-Term gate non-integral on surface {_safe_surface_index(surf)}",
        )
        return items
    if n_terms <= 0:
        return items
    surface_index = _safe_surface_index(surf)
    for i in range(n_terms):
        col_name = type_info.coeff_par(i)
        try:
            cell = _asph._cell_by_col(system, surf, col_name)
        except Exception:  # noqa: BLE001 — an unreadable cell contributes nothing
            continue
        if _cell_is_variable(cell, variable_member):
            items.append({
                "source": "asphere",
                "surface": surface_index,
                "cell": "asphere",
                # ``term`` is the 1-based PHYSICAL r^k order (the ORDER MAP power); ``par``
                # is the column token the clear walk re-fetches from (the re-fetch handle).
                "term": type_info.power(i),
                "par": col_name,
                "value": _safe_cell_value(cell),
                "solve": "Variable",
            })
    return items


def _safe_surface_index(surf):
    """Read a surface row's own index for the inventory item, guarded -> ``None``.

    The LDE/asphere walks know the index from the loop; this is the fallback for the
    asphere emit-twin when called directly with a row (the count helper passes the row, not
    the index). A live ``ILDERow`` exposes ``SurfaceNumber``; a fake row exposes ``_index``.
    Guarded — a missing accessor -> ``None`` (the diagnostic is best-effort, never raises).
    """
    for attr in ("SurfaceNumber", "_index", "RowIndex"):
        try:
            v = getattr(surf, attr)
        except Exception:  # noqa: BLE001 — a missing accessor -> try the next
            continue
        try:
            return int(v)
        except Exception:  # noqa: BLE001 — a non-int index -> not useful
            return None
    return None


def _count_asphere_variables(system, surf, variable_member):
    """Count Variable solves on a surface's ASPHERE COEFFICIENT Par cells (fail-safe).

    The count is now the LENGTH of the emit-twin ``_enumerate_asphere_variables`` (the
    counter and the inventory share ONE walk so the count and the inventory can NEVER
    diverge). The public contract + signature are UNCHANGED (it is imported by other tiers,
    so it stays a named helper). Returns an int; NEVER raises.
    """
    return len(_enumerate_asphere_variables(system, surf, variable_member))


def _clear_solve_to_fixed_proven(cell, variable_member, surface, token,
                                 lde=None, cell_attr=None):
    """Fix a single LDE solve cell + read-back-prove it is no longer Variable (S1 §3).

    The apply hard-reset's MakeSolveFixed primitive: call ``cell.MakeSolveFixed()`` then
    re-read ``cell.GetSolveData().Type`` and assert it is NOT the live ``Variable`` member
    — a silent ``MakeSolveFixed`` no-op (the bool lied) or a read THROW both RAISE
    ``SurfaceWriteError`` (which routes into the apply's EXISTING atomic rollback). ``token``
    is the cell name (``"radius"``/``"thickness"``/``"conic"``) for the message; ``surface``
    is the diagnostic index. Returns the read-back solve-type string.

    Re-fetch discipline: when ``lde`` + ``cell_attr`` are passed, the read-back
    PROOF is read from a FRESHLY re-fetched cell (``getattr(lde.GetSurfaceAt(surface),
    cell_attr)``), NOT the same pre-mutation proxy ``cell`` the ``MakeSolveFixed`` was called
    on — matching ``set_variable`` / ``clear_variable`` / ``_clear_asphere_variable``. A
    stale pythonnet proxy could otherwise read a pre-mutation solve: a stale ``Fixed`` would
    MASK a silent no-op (the false-clean this primitive exists to prevent), a stale
    ``Variable`` would FALSE-rollback a successful apply. A re-fetch throw routes to the same
    ``surface_write`` rollback (fail-closed). ``lde``/``cell_attr`` default ``None`` keeps a
    bare-proxy read-back for any caller that lacks the addressing (back-compat).
    """
    try:
        cell.MakeSolveFixed()
    except Exception as exc:  # noqa: BLE001 — a clear THROW -> surface_write (-> rollback)
        raise SurfaceWriteError(
            f"could not clear the {token} solve on surface {surface} ({exc!r}); the "
            "engine rejected the MakeSolveFixed — refusing rather than leaving a stale "
            "Variable solve",
            field=f"{token} solve", intended="Fixed", actual=None, surface=surface,
        ) from exc
    # Re-fetch: prove from a FRESH cell handle, never the pre-mutation proxy.
    proof_cell = cell
    if lde is not None and cell_attr is not None:
        try:
            proof_cell = getattr(lde.GetSurfaceAt(surface), cell_attr)
        except Exception as exc:  # noqa: BLE001 — a re-fetch throw -> unverifiable -> rollback
            raise SurfaceWriteError(
                f"could not re-fetch the {token} cell on surface {surface} after the clear "
                f"({exc!r}); the clear is unverifiable — refusing rather than guessing",
                field=f"{token} solve", intended="Fixed", actual=None, surface=surface,
            ) from exc
    try:
        actual = str(proof_cell.GetSolveData().Type)
    except Exception as exc:  # noqa: BLE001 — a solve read THROW -> surface_write
        raise SurfaceWriteError(
            f"could not read back the {token} solve on surface {surface} after the clear "
            f"({exc!r}); the clear is unverifiable — refusing rather than guessing",
            field=f"{token} solve", intended="Fixed", actual=None, surface=surface,
        ) from exc
    if actual == str(variable_member):
        raise SurfaceWriteError(
            f"the {token} solve clear on surface {surface} did not take effect: solve "
            f"reads back {actual!r} (still Variable, a silent no-op); refusing rather "
            "than claiming a cleared solve",
            field=f"{token} solve", intended="Fixed", actual=actual, surface=surface,
        )
    return actual


def _check_conic_coeff_degeneracy(system, row, surface, variable_member):
    """Advisory warning when conic K AND asphere coefficients are both Variable.

    K contributes ~r^4 to sag, collinear with the 4th-order polynomial term.
    Both variable → rank-deficient normal matrix. Returns a warning string or
    None. THROW-GUARDED: degrades to None (advisory, never crashes the caller).
    """
    from . import _asphere_cells as _asph

    try:
        conic_cell = getattr(row, "ConicCell", None)
        if conic_cell is None:
            return None
        conic_is_var = _cell_is_variable(conic_cell, variable_member)
        if not conic_is_var:
            return None
        info_key = _asph.asphere_type_of(row)
        if info_key is None:
            return None
        type_info = _asph.ASPHERE_TYPE_INFO.get(info_key)
        if type_info is None:
            return None
        if type_info.gated:
            try:
                n_terms = int(_asph.read_gate_cell(system, row, type_info))
            except Exception:  # noqa: BLE001
                n_terms = 0
        else:
            n_terms = type_info.max_terms
        for i in range(n_terms):
            try:
                cell = _asph._cell_by_col(system, row, type_info.coeff_par(i))
            except Exception:  # noqa: BLE001
                continue
            if _cell_is_variable(cell, variable_member):
                return (
                    f"Conic K and asphere coefficient(s) are both variable on "
                    f"surface {surface}. K contributes ~r^4 sag, collinear with "
                    f"the 4th-order polynomial term — both variable is a "
                    f"degenerate subspace (rank-deficient normal matrix). "
                    f"Recommend freeing ONE (conic OR polynomial coefficients)."
                )
    except Exception:  # noqa: BLE001 — advisory, never crash
        pass
    return None


def _safe_type_name(op):
    """Read an MCE operand's ``TypeName`` guarded -> ``str`` or ``None`` (the diagnostic)."""
    try:
        return str(op.TypeName)
    except Exception:  # noqa: BLE001 — an unreadable type -> None (best-effort diagnostic)
        return None


def _enumerate_mce_variables(system, variable_member, faults=None):
    """Emit the inventory ITEMS for per-config MCE Variable cells (fail-safe). NEVER raises.

    The itemizing TWIN of ``_count_mce_variables`` (the count is now
    ``len(_enumerate_mce_variables(...))``). Walk ``system.MCE`` rows
    ``1..NumberOfOperands``; for each row walk configs ``1..NumberOfConfigurations``
    (``op.GetOperandCell(cfg)``, 1-based); emit one ``source=="mce"`` item per cell that is
    BOTH Variable-solved AND ``cell.DataType == "Double"`` (``_cell_is_double`` — the Q5
    phantom-DOF filter is MANDATORY: the engine SILENTLY accepts ``MakeSolveVariable()`` on
    an Integer per-config cell but the optimizer does NOT count it, so the inventory must NOT
    emit it or the count diverges from ``opt.Variables``). Every read GUARDED (a wedged
    MCE / row / cell contributes nothing); a missing ``system.MCE`` (a non-MCE backend) ->
    ``[]``. Bounded by the live ``NumberOfOperands`` x ``NumberOfConfigurations`` (never a
    brute scan). NO config switch needed — ``op.GetOperandCell(cfg)`` reads ANY config's cell
    (the ``_count_mce_variables`` precedent), so a Variable cell in a NON-current config is
    covered.
    """
    items = []
    mce = getattr(system, "MCE", None)
    if mce is None:
        return items
    try:
        n_operands = int(mce.NumberOfOperands)
    except Exception:  # noqa: BLE001 — a wedged MCE -> nothing, never crash
        _record_enumeration_fault(
            faults, "mce", "MCE NumberOfOperands read threw (whole MCE source dropped)",
        )
        return items
    try:
        n_configs = int(mce.NumberOfConfigurations)
    except Exception:  # noqa: BLE001 — an unreadable config count -> nothing
        _record_enumeration_fault(
            faults, "mce", "MCE NumberOfConfigurations read threw (whole MCE source dropped)",
        )
        return items
    if n_operands <= 0 or n_configs <= 0:
        return items
    for row in range(1, n_operands + 1):
        try:
            op = mce.GetOperandAt(row)
        except Exception:  # noqa: BLE001 — a missing row contributes nothing
            _record_enumeration_fault(
                faults, "mce", f"MCE GetOperandAt({row}) threw (row coverage dropped)",
            )
            continue
        type_name = _safe_type_name(op)
        for cfg in range(1, n_configs + 1):
            try:
                cell = op.GetOperandCell(cfg)
            except Exception:  # noqa: BLE001 — an unreadable cell contributes nothing
                _record_enumeration_fault(
                    faults, "mce",
                    f"MCE GetOperandCell(row={row}, config={cfg}) threw (cell coverage dropped)",
                )
                continue
            # Emit ONLY a Double cell's Variable solve — an Integer/String cell set Variable
            # is the Q5 phantom DOF the optimizer does NOT count (and ``set_config_variable``
            # refuses pre-mutation). Both predicates must hold.
            if _cell_is_double(cell) and _cell_is_variable(cell, variable_member):
                items.append({
                    "source": "mce",
                    "row": row,
                    "config": cfg,
                    "cell": "mce",
                    "type_name": type_name,
                    "value": _safe_cell_value(cell),
                    "solve": "Variable",
                })
    return items


def _count_mce_variables(system, variable_member):
    """Count Variable solves on per-config MCE cells (fail-safe). NEVER raises.

    The count is now the LENGTH of the emit-twin ``_enumerate_mce_variables`` (the
    counter and the inventory share ONE walk). The public contract + signature are
    UNCHANGED (it is imported by other tiers). Only a Double cell's Variable solve is
    counted (the Q5 phantom-DOF filter); an Integer/String per-config cell set Variable is
    NOT counted. Returns an int; NEVER raises.
    """
    return len(_enumerate_mce_variables(system, variable_member))


def _scan_per_config_thin(system):
    """Scan per-config MCE ``THIC`` rows for an unbuildable ``value <= 0`` (§b).

    The new ``optimize_per_config_thin`` preflight predicate (D-8, ONE shared locus consumed
    by BOTH ``dry_run`` and ``optimize``). Walk ``system.MCE`` rows ``1..NumberOfOperands``;
    for each row whose ``TypeName == "THIC"`` (a per-config thickness override — the zoom /
    compensator gap), read each config's cell (``op.GetOperandCell(cfg)``, 1-based) and collect
    any ``value <= 0`` as ``{row, surface, config, value}``. The danger this catches is the
    HIGH silent-wrong (#2): the optimizer drove a per-config air gap to e.g. -3 mm in a
    NON-current config (so the LDE / current-config readout looks fine) into a garbage basin.

    ONLY a ``DataType == "Double"`` cell is read (an Integer/String per-config cell is not a
    thickness value — ``_cell_is_double``, the merit/MCE discriminator). ``surface`` is read
    from ``op.Param1`` (THIC ``takes_surface``), guarded -> ``None`` if unreadable. EVERY read
    is GUARDED (a wedged MCE / row / cell contributes nothing) and a missing ``system.MCE``
    (a non-MCE / single-config backend) -> ``[]`` — this runs inside the NON-MUTATING preflight
    and must NEVER raise. Returns the offender list (possibly empty); the empty list means the
    gate does not fire (byte-identical to today for a single-config / non-zoom system).
    """
    mce = getattr(system, "MCE", None)
    if mce is None:
        return []
    # Fold-awareness (L30 shared predicate): a FOLDED system uses NEGATIVE thickness
    # by design (the fold leg), so a per-config THIC <= 0 is NOT unbuildable there — the
    # THIC<=0 audit is UNFOLDED-ONLY, exactly like check_clearance's per-gap thickness audit
    # (which reports folded gaps as informational, never a violation). Skip the scan on a
    # folded system rather than hard-refuse a valid folded multi-config design. NEVER raises.
    try:
        from . import _layout_geometry as _geom
        if _geom.system_is_folded(system):
            return []
    except Exception:  # noqa: BLE001 — a fold-read hiccup must not crash the scan
        pass
    try:
        n_operands = int(mce.NumberOfOperands)
        n_configs = int(mce.NumberOfConfigurations)
    except Exception:  # noqa: BLE001 — a wedged MCE -> no scan, never crash
        return []
    if n_operands <= 0 or n_configs <= 0:
        return []
    offenders = []
    for row in range(1, n_operands + 1):
        try:
            op = mce.GetOperandAt(row)
        except Exception:  # noqa: BLE001 — a missing row contributes nothing
            continue
        try:
            if str(op.TypeName) != "THIC":
                continue
        except Exception:  # noqa: BLE001 — an unreadable type -> skip this row
            continue
        try:
            surface = int(op.Param1)
        except Exception:  # noqa: BLE001 — surface is a diagnostic; absent -> None
            surface = None
        for cfg in range(1, n_configs + 1):
            try:
                cell = op.GetOperandCell(cfg)
            except Exception:  # noqa: BLE001 — an unreadable cell contributes nothing
                continue
            if not _cell_is_double(cell):
                continue
            try:
                value = float(cell.DoubleValue)
            except Exception:  # noqa: BLE001 — an unreadable value contributes nothing
                continue
            if not math.isfinite(value):
                continue
            if value <= 0:
                offenders.append(
                    {"row": row, "surface": surface, "config": cfg, "value": value}
                )
    return offenders


# --------------------------------------------------------------------------- #
# S5: the mutate-and-continue nudge of a collapsed per-config THIC cell.
# --------------------------------------------------------------------------- #
_NUDGE_EPS = 0.001   # probe T1: lifts a collapsed THIC > 0 so the re-scan passes and the
                     # optimizer opens; the heavy per-config floor (4a) + the THIC-as-DOF
                     # then walk the gap up to the floor. A nudge alone (no floor+DOF) only
                     # reaches a knife-edge / re-collapses — the nudge is HALF of a pair.


def _nudge_per_config_thin(system, offenders):
    """Mutate-and-continue nudge of collapsed per-config THIC cells (§2.2). NEVER raises.

    ``offenders`` is the ``_scan_per_config_thin`` list ``[{row, surface, config, value}]``.
    Returns ``(nudged, un_nudgeable)``:
      ``nudged``       = ``[{row, surface, config, from, to:0.001, fixed:bool}]``
      ``un_nudgeable`` = ``[{surface, config, reason}]`` with ``reason`` in
                         ``{pickup, unverifiable, non_double, write_no_op}``.

    Per offender:
      1. fresh handles ``op = MCE.GetOperandAt(row)`` / ``cell = op.GetOperandCell(config)``
         (guarded — an unreadable handle -> ``unverifiable`` skip, fail-closed);
      2. the solve-type gate — ``_mce_cells.solve_type_name(cell)`` (the GUARDED sibling,
         ``None`` on throw): a solve NOT in ``{Fixed, Variable}`` is a slaved pickup /
         unverifiable cell -> SKIP (``pickup`` if a solve name read, else ``unverifiable``),
         never independently nudged (probe T4);
      3. a defensive Double re-check (``_cell_is_double`` — an Integer/String cell is not a
         thickness value; the offenders are Double from the scan, so this only fires on a
         layout drift) -> ``non_double`` skip;
      4. write ``_NUDGE_EPS`` via the read-back-proven MCE writer
         (``_mce_cells.write_config_cell``, DataType-keyed fresh-handle read-back — the T25
         fresh-handle proof); a silent no-op / firewall raise -> ``write_no_op`` skip.

    A FOLD is NOT handled here — ``_scan_per_config_thin`` already returns ``[]`` on a
    folded system (the whole-system fold skip), so a folded THIC yields no offenders.
    """
    from . import _mce_cells as _mcecells

    nudged = []
    un_nudgeable = []
    for off in offenders:
        row = off.get("row")
        config = off.get("config")
        surface = off.get("surface")
        value = off.get("value")
        # (1) fresh handles, guarded (an unreadable handle -> fail-closed unverifiable).
        try:
            op = system.MCE.GetOperandAt(row)
            cell = op.GetOperandCell(config)
        except Exception:  # noqa: BLE001 — a wedged handle -> unverifiable, never raise
            un_nudgeable.append(
                {"surface": surface, "config": config, "reason": "unverifiable"}
            )
            continue
        # (2) the pickup / solve-type gate (probe T4). None-on-throw => unverifiable.
        st = _mcecells.solve_type_name(cell)
        if st not in ("Fixed", "Variable"):
            un_nudgeable.append(
                {
                    "surface": surface,
                    "config": config,
                    "reason": "pickup" if st else "unverifiable",
                }
            )
            continue
        # (3) defensive Double re-check (an Integer/String per-config cell is not a
        #     thickness value; _expect_layout would RAISE inside the write -> non_double).
        if not _cell_is_double(cell):
            un_nudgeable.append(
                {"surface": surface, "config": config, "reason": "non_double"}
            )
            continue
        # (4) write the epsilon via the read-back-proven writer (a fresh handle inside it;
        #     a silent no-op / any firewall raise -> the offender survives the re-scan).
        try:
            _mcecells.write_config_cell(system, op, config, "Double", _NUDGE_EPS)
        except Exception:  # noqa: BLE001 — a write no-op / layout raise -> not nudged
            un_nudgeable.append(
                {"surface": surface, "config": config, "reason": "write_no_op"}
            )
            continue
        nudged.append(
            {
                "row": row,
                "surface": surface,
                "config": config,
                "from": value,
                "to": _NUDGE_EPS,
                "fixed": (st == "Fixed"),
            }
        )
    return (nudged, un_nudgeable)


# --------------------------------------------------------------------------- #
# S1: the inert-DOF guard (an air<->air surface's radius/conic Variable is a
# zero-merit-sensitivity DOF — probe D).
# --------------------------------------------------------------------------- #
# Non-Standard Type substrings whose surface carries REAL power INDEPENDENT of its
# bounding materials, so an air<->air radius/conic on such a surface is a GENUINE DOF —
# the inert scan must NEVER flag it (BUG-2 / L-4). A diffraction GRATING, FRESNEL, PHASE
# (binary / extended-polynomial phase), and BINARY optic all diffract/refract from their
# own structure, not from a glass interface. Matched as upper-cased substrings (the same
# coarse-but-fail-safe idiom as COORD/BREAK; a false-POSITIVE only SPARES a surface from
# the inert flag, the conservative direction).
_POWERED_NONSTANDARD_TYPE_SUBSTRINGS = ("GRATING", "FRESNEL", "PHASE", "BINARY")


def _row_is_mirror_or_cb(row):
    """True iff ``row`` is a MIRROR / coordinate-break / powered non-Standard — None if unprovable (§4.2).

    A surface whose air<->air geometry is NOT inert because it carries power INDEPENDENT of
    its bounding materials:
      - a MIRROR (``Material == "MIRROR"``) — a fold mirror's radius is a real DOF;
      - a coordinate break (``Type`` contains ``"COORD"`` or ``"BREAK"``) — structural;
      - a powered/diffractive non-Standard type (``Type`` contains GRATING / FRESNEL / PHASE /
        BINARY — BUG-2/L-4) — a curved grating diffracts differently, a Fresnel/phase surface
        refracts from its own structure, so its air<->air substrate radius is a genuine DOF.
    Returns True (one of the above), False (positively a plain refractive surface), or ``None``
    when the read THROWS — the caller (``_surface_is_inert``) treats None as "do not refuse"
    (fail-closed: never false-refuse a real powered DOF on a read hiccup).
    """
    try:
        material = str(row.Material).strip().upper()
    except Exception:  # noqa: BLE001 — an unreadable Material -> unprovable
        return None
    if material == "MIRROR":
        return True
    try:
        type_name = str(row.Type).upper()
    except Exception:  # noqa: BLE001 — an unreadable Type -> unprovable
        return None
    if "COORD" in type_name or "BREAK" in type_name:
        return True
    # A powered/diffractive non-Standard type has real power independent of its materials.
    for sub in _POWERED_NONSTANDARD_TYPE_SUBSTRINGS:
        if sub in type_name:
            return True
    return False


def _surface_is_inert(lde, surface):
    """True iff ``surface`` is air on BOTH sides (a zero-power DOF) — None if unprovable (§4.2).

    The air<->air geometry signal (probe D): a radius/conic Variable on a surface whose own
    Material AND predecessor Material are both air ("") has ZERO merit sensitivity by
    construction (a flat air<->air surface contributes nothing). Returns:
      - ``True``  — the surface is provably air on both sides AND is NOT a mirror/CB.
      - ``False`` — at least one side is glass (a real lens vertex / boundary).
      - ``None``  — a read THREW (Material/Type/row fetch) OR the surface is a mirror/CB:
        the caller does NOT refuse on None (fail-closed — never false-refuse a real DOF).
    Reuses ``_structural_common._material_is_air`` (which RAISES on a Material read fault).
    """
    from ._structural_common import _material_is_air
    try:
        row = lde.GetSurfaceAt(surface)
        prev = lde.GetSurfaceAt(surface - 1)
    except Exception:  # noqa: BLE001 — a row-fetch throw -> unprovable (do not refuse)
        return None
    # A MIRROR / coordinate-break / powered-non-Standard surface (own OR predecessor) is NOT
    # an inert geometry DOF — a fold mirror's radius, a grating/Fresnel/phase substrate radius
    # (BUG-2) are real DOFs. Unprovable (None) -> do not refuse.
    own_special = _row_is_mirror_or_cb(row)
    prev_special = _row_is_mirror_or_cb(prev)
    if own_special is None or prev_special is None:
        return None
    if own_special or prev_special:
        return None
    try:
        own_air = _material_is_air(row)
        prev_air = _material_is_air(prev)
    except SurfaceWriteError:  # a Material read fault -> unprovable (do not refuse)
        return None
    return own_air and prev_air


def _scan_inert_dofs(system):
    """Scan for inert (air<->air) radius/conic Variable DOFs (S1 §4.1). NEVER raises.

    The new ``optimize_inert_dof`` preflight predicate (ONE shared locus consumed by
    BOTH ``dry_run`` and ``optimize``). Walks ``_variable_inventory`` and keeps an offender
    when an item is a Variable on an LDE/asphere RADIUS or CONIC cell (THICKNESS EXCLUDED — a
    thin air gap's thickness IS a real position DOF) on an INTERIOR surface
    (``1 <= surface <= n-2``) whose surface ``_surface_is_inert(...) is True`` (air on both
    sides, not a mirror/CB). Each offender is ``{source, surface, cell}``.

    The air<->air radius/conic Variable is a genuine zero-merit-sensitivity DOF (a flat
    zero-power surface contributes nothing to the merit), so the optimizer drifts it to
    garbage — the curved-stop silent-wrong (a dummy-stop radius made Variable). EVERY read is
    GUARDED (this runs inside the NON-MUTATING preflight and must NEVER raise); an unreadable
    LDE / inventory / surface contributes nothing (fail-closed — never refuse on a hiccup).
    Returns the offender list (possibly empty); an empty list means the gate does not fire.
    """
    try:
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — an unreadable LDE -> no scan, never crash
        return []
    try:
        variable_member = _solve_type_variable_enum(system)
    except Exception:  # noqa: BLE001 — an unresolvable Variable member -> no scan
        return []
    try:
        inventory = _variable_inventory(system, variable_member)
    except Exception:  # noqa: BLE001 — a wedged inventory -> no scan, never crash
        return []
    offenders = []
    for item in inventory:
        source = item.get("source")
        cell = item.get("cell")
        surface = item.get("surface")
        # Only an LDE/asphere RADIUS or CONIC variable on an interior surface can be inert.
        # THICKNESS is EXCLUDED (a thin air gap's thickness is a real position DOF).
        if source not in ("lde", "asphere"):
            continue
        if cell not in ("radius", "conic"):
            continue
        if not isinstance(surface, int) or isinstance(surface, bool):
            continue
        if not (1 <= surface <= n - 2):
            continue
        if _surface_is_inert(lde, surface) is True:
            offenders.append({"source": source, "surface": surface, "cell": cell})
    return offenders


# The curated ray-trace operand TypeName set + the ray-free-merit
# collapse WARN. A merit is "ray-free" when NO operand row's TypeName is in this set —
# i.e. the merit carries only boundary/first-order operands (EFFL/TTHI/MNCA/...) with no
# ray-based restoring force. Direction-of-error: a ray token wrongly
# OMITTED -> a MISSED warn; a boundary/first-order token wrongly INCLUDED -> a missed warn
# (never a false warn, because inclusion suppresses). So err toward COMPLETENESS for ray
# tokens; NEVER add a boundary/first-order token (EFFL/TTHI/MNCA/MXCA/MNCG/MNEG/BLNK/DMFS/
# CONF/CTGT/CTLT). OPDX is the SEQ-wizard's ray/wavefront marker (probe B2).
_RAY_TRACE_OPERANDS = frozenset({
    # OPD / wavefront (OPDX = the SEQ-wizard ray marker, probe B2)
    "OPDX", "OPDC", "OPDM", "OPDL",
    # transverse ray aberration
    "TRAR", "TRAX", "TRAY", "TRAC", "TRAI", "TRCX", "TRCY", "ANAR", "ANAX", "ANAY",
    # RMS spot / wavefront (centroid + chief)
    "RSCE", "RSCH", "RSRE", "RSRH", "RWCE", "RWCH", "RWRE", "RWRH", "RWEA",
    # real-ray coordinate / angle / height
    "REAX", "REAY", "REAZ", "RAGX", "RAGY", "RAGZ", "RAGA", "RAGB", "RAGC",
    "RANG", "RAID", "RAED", "RAEN", "RENA", "RSAG",
    # MTF (traces rays)
    "MTFT", "MTFS", "MTFA", "MTFN", "MTFX",
    # diffraction / encircled energy (traces rays)
    "DENC", "DENF", "GENC", "GENF",
})
_RAYFREE_COLLAPSE_MIN_VARS = 3   # >=3 free radius/conic vars = the collapse regime


def _scan_rayfree_merit(system):
    """WARN str-or-None: a merit with ZERO ray operands AND >= 3 free shape (radius/
    conic) variables -> geometry-collapse risk. NEVER raises -> None on any throw.

    Direction-of-error (curated set): a ray token wrongly OMITTED -> a MISSED warn; a
    boundary/first-order token wrongly INCLUDED -> a missed warn (never a false warn,
    because inclusion suppresses). Err toward completeness for ray tokens; NEVER add a
    boundary/first-order token (EFFL/TTHI/MNCA/MXCA/MNCG/MNEG/BLNK/DMFS/CONF/CTGT/CTLT).
    """
    try:
        mfe = system.MFE
        n_ops = int(mfe.NumberOfOperands)
        for i in range(1, n_ops + 1):
            try:
                if str(mfe.GetOperandAt(i).TypeName) in _RAY_TRACE_OPERANDS:
                    return None                     # has a ray anchor -> no warn
            except Exception:  # noqa: BLE001 — skip an unreadable row
                continue
        member = _solve_type_variable_enum(system)
        shape_vars = sum(
            1 for it in _variable_inventory(system, member)
            if it.get("source") in ("lde", "asphere") and it.get("cell") in ("radius", "conic")
        )
        if shape_vars >= _RAYFREE_COLLAPSE_MIN_VARS:
            return (
                f"ray-free merit + {shape_vars} free radius/conic variables: the merit "
                "has NO ray-trace operands (only boundary/first-order), so there is no "
                "ray-based restoring force — DLS can collapse the geometry to near-zero "
                "radii. Add a few low-pupil on-axis ray operands, or freeze the radii "
                "and free only focus/scale."
            )
    except Exception:  # noqa: BLE001
        return None
    return None


# The LDE cell tokens scanned per interior surface (the geometry DOFs), keyed to the
# inventory item's ``cell`` discriminator (the cell attr name -> the item token).
_LDE_CELL_TOKENS = (
    ("RadiusCell", "radius"),
    ("ThicknessCell", "thickness"),
    ("ConicCell", "conic"),
)


def _enumerate_lde_variables(system, variable_member, faults=None):
    """Emit the inventory ITEMS for the LDE geometry cells (radius/thickness/conic).

    Walk the INTERIOR surfaces ``1..NumberOfSurfaces-2`` (OBJECT s0 + IMAGE sN-1 EXCLUDED —
    matches ``_count_variables`` + ``set_variable``'s geometry firewall) and emit one
    ``source=="lde"`` item per ``RadiusCell``/``ThicknessCell``/``ConicCell`` set Variable.
    Surface-ascending; every read guarded (a missing surface / cell contributes nothing);
    NEVER raises. An MCE-overridden LDE cell reads ``Fixed`` at the LDE level (probe note A)
    so the LDE walk won't double-report it — only the MCE walk does (no special-casing).

    ``faults`` (optional): a per-surface ``GetSurfaceAt`` throw (the surface's LDE coverage
    silently dropped) is RECORDED when a list is passed; ``faults is None`` (the counters) ->
    skipped exactly as before (byte-identical).
    """
    items = []
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    for i in range(1, n - 1):
        try:
            surf = lde.GetSurfaceAt(i)
        except Exception:  # noqa: BLE001 — a missing surface contributes nothing
            _record_enumeration_fault(
                faults, "lde", f"LDE GetSurfaceAt({i}) threw (surface coverage dropped)",
            )
            continue
        for cell_attr, token in _LDE_CELL_TOKENS:
            cell = getattr(surf, cell_attr, None)
            if cell is None:
                continue
            if _cell_is_variable(cell, variable_member):
                items.append({
                    "source": "lde",
                    "surface": i,
                    "cell": token,
                    "value": _safe_cell_value(cell),
                    "solve": "Variable",
                })
    return items


def _variable_inventory(system, variable_member=None, faults=None):
    """Enumerate EVERY Variable-solved optimizer cell across LDE + asphere + MCE.

    The promoted single walk the three count summands itemize (ONE uniform
    ``cell.GetSolveData().Type`` read across all three sources). Returns a ``list[dict]`` of
    inventory ITEMS (shape: ``source``-discriminated; IDENTIFIERS not .NET
    proxies; a value-read failure -> ``value:None``, the item NEVER dropped). Possibly
    empty; NEVER raises (every per-cell read guarded exactly as the three counters are).

    Order: LDE items (surface-ascending), then per-interior-surface asphere items
    (surface-ascending), then the SYSTEM-GLOBAL MCE items. ``len(_variable_inventory(...))``
    equals ``_count_variables(system.LDE, vm, system=system)`` AND ``opt.Variables`` for the
    same system (the back-compat invariant — they share the SAME per-source emit walks).

    ``variable_member`` defaults to ``_solve_type_variable_enum(system)`` (resolve once,
    pass down).

    ``faults`` (optional): when a list is passed, a per-source DISCOVERY fault (an
    enumerator's per-source / per-surface read deterministically throwing so that whole
    source's coverage is silently dropped) is RECORDED into it — so a genuinely-Variable cell
    behind a deterministic discovery fault is no longer INVISIBLE to a ``cleared_all`` /
    completeness proof. ``faults is None`` (the three counters' delegation) -> byte-identical
    fail-safe-skip (the count contract is untouched; only ``list_variables`` /
    ``clear_all_variables`` / the disclosure helper surface the fault).
    """
    if variable_member is None:
        variable_member = _solve_type_variable_enum(system)
    items = list(_enumerate_lde_variables(system, variable_member, faults))
    # Asphere is PER-INTERIOR-SURFACE (the same surface walk as the LDE arm).
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    for i in range(1, n - 1):
        try:
            surf = lde.GetSurfaceAt(i)
        except Exception:  # noqa: BLE001 — a missing surface contributes nothing
            _record_enumeration_fault(
                faults, "asphere",
                f"LDE GetSurfaceAt({i}) threw on the asphere walk (surface coverage dropped)",
            )
            continue
        items.extend(_enumerate_asphere_variables(system, surf, variable_member, faults))
    # MCE is SYSTEM-GLOBAL once (NOT per-surface — a per-surface placement would emit the
    # per-config DOFs N times).
    items.extend(_enumerate_mce_variables(system, variable_member, faults))
    return items


def _count_variables(lde, variable_member, system=None):
    """Count LDE cells whose solve is Variable (the §e variables gate).

    Scans the INTERIOR surfaces' ``RadiusCell`` + ``ThicknessCell`` + ``ConicCell`` (these
    are the optimizer's geometry variables). ``opt.Variables`` is NOT used —
    the preflight does NOT open the optimizer, so the count comes from the scan. Per §e the
    scan is ``1..NumberOfSurfaces-2`` — OBJECT (surface 0) and IMAGE (surface N-1) are
    EXCLUDED, consistent with ``set_variable``'s geometry firewall. Each cell read is
    guarded.

    Two paths, gated on ``system`` (the back-compat invariant):

    - ``system is None`` (the legacy DIRECT-call signature): the LITERAL old Radius/
      Thickness/Conic-only LDE loop, byte-identical (NO asphere, NO MCE). This path must
      NEVER route through ``_variable_inventory`` (which NEEDS ``system``), so it stays the
      literal loop here — a mutate-fails test pins it.
    - ``system is not None`` (the production preflight): delegates to
      ``len(_variable_inventory(system, variable_member))`` — the SAME per-source emit walks
      the inventory uses, so the count and the inventory are identical BY CONSTRUCTION
      (radius/thickness/conic + asphere coefficients + per-config MCE DOFs).
    """
    if system is not None:
        return len(_variable_inventory(system, variable_member))
    # Legacy direct-call (system is None): the LITERAL Radius/Thickness/Conic-only LDE loop,
    # byte-identical (no asphere, no MCE — those summands NEED ``system``). DO NOT route this
    # through the enumerator.
    n = int(lde.NumberOfSurfaces)
    count = 0
    for i in range(1, n - 1):
        try:
            surf = lde.GetSurfaceAt(i)
        except Exception:  # noqa: BLE001 — a missing surface contributes nothing
            continue
        for cell_name in ("RadiusCell", "ThicknessCell", "ConicCell"):
            cell = getattr(surf, cell_name, None)
            if cell is None:
                continue
            if _cell_is_variable(cell, variable_member):
                count += 1
    return count


# --------------------------------------------------------------------------- #
# S1 variable-lifecycle: the inventory tally, the disclosure helper, the bulk-clear core.
# --------------------------------------------------------------------------- #
_VARIABLE_LIFECYCLE_FAMILY = "variable_lifecycle"


def _by_source_tally(inventory):
    """The per-source ``{lde, asphere, mce}`` tally of an inventory (envelope-level)."""
    tally = {"lde": 0, "asphere": 0, "mce": 0}
    for item in inventory:
        src = item.get("source")
        if src in tally:
            tally[src] += 1
    return tally


def _disclose_inherited_variables(system):
    """``(inherited_variables, n_inherited_variables)`` for the load/apply disclosure.

    Thin over ``_variable_inventory`` (the solves SURVIVED the load).
    GUARDED -> ``([], None)`` on any failure: a disclosure must NEVER break a successful
    load/apply, and ``None`` (not ``0``) distinguishes "could not read" from "read, zero
    inherited" (so the caller can stamp an ``inherited_variables_warning`` on a guarded
    fault). The happy path returns ``(items, len(items))``.

    (the L26 sibling of the ``cleared_all`` fix): thread a fresh ``faults``
    list so a DETERMINISTIC per-source discovery fault (asphere Type-read throws / MCE read
    throws) is SURFACED, not silently fail-safe-skipped. ``_variable_inventory`` swallows a
    discovery fault (returns a partial list, never raises), so the bare-``try`` here would
    NOT fire — it would ship a non-None UNDERCOUNT with NO warning while a real inherited
    Variable solve hides behind the fault (the same silent-wrong class closed for
    ``clear_all_variables``, on the disclosure path). When the enumeration is INCOMPLETE
    (``faults`` non-empty) we return ``([items], None)`` — the ``None`` makes the EXISTING
    warning<->None sync (load_design / lens_spec) stamp ``inherited_variables_warning``
    instead of shipping a short count. The happy path (no fault) is byte-identical
    (``(items, len(items))``, no warning).
    """
    try:
        member = _solve_type_variable_enum(system)
        faults = []
        inv = _variable_inventory(system, member, faults)
        if faults:
            # The enumeration was INCOMPLETE (a source's discovery deterministically faulted
            # -> [] for that source). A Variable cell behind the fault is invisible; refuse to
            # ship a non-None UNDERCOUNT. ``None`` routes through the warning<->None sync.
            return inv, None
        return inv, len(inv)
    except Exception:  # noqa: BLE001 — a disclosure fault must never break the load/apply
        return [], None


def _refetch_inventory_cell(system, item):
    """Re-fetch the live cell for an inventory ITEM from its IDENTIFIERS.

    NOT a stored proxy (a proxy goes stale across a load): the cell is re-fetched from
    ``source`` + ``surface``/``par``/``row``/``config``. Returns the live cell, or ``None``
    if the re-fetch throws (the bulk clear treats a None as a per-cell failure, leaving the
    residual for the read-back proof to catch). NEVER raises.
    """
    try:
        source = item.get("source")
        if source == "lde":
            surf = system.LDE.GetSurfaceAt(int(item["surface"]))
            attr = {"radius": "RadiusCell", "thickness": "ThicknessCell",
                    "conic": "ConicCell"}.get(item.get("cell"))
            if attr is None:
                return None
            return getattr(surf, attr, None)
        if source == "asphere":
            from . import _asphere_cells as _asph
            surf = system.LDE.GetSurfaceAt(int(item["surface"]))
            return _asph._cell_by_col(system, surf, item["par"])
        if source == "mce":
            op = system.MCE.GetOperandAt(int(item["row"]))
            return op.GetOperandCell(int(item["config"]))
    except Exception:  # noqa: BLE001 — a re-fetch throw -> None (the read-back proof catches it)
        return None
    return None


_CLEAR_ALL_VARIABLES_NOTE = (
    "clear_all_variables FIXES each cell at its CURRENT value — it does NOT reset "
    "to a prior value. If the design was mid-optimize at a bad value, those values "
    "are now locked in; restore a snapshot (save_snapshot / load_design) if the "
    "current values are bad."
)


def _clear_all_variables_core(system):
    """Bulk-clear EVERY optimizer variable to Fixed, re-enumerate-to-0 proven (the §clear core).

    ONE shared locus (L30) used by the ``clear_all_variables`` tool AND both reset hooks
    (load_design / apply_lens_spec ``reset_variables``). Behavior:

    1. resolve the Variable member; enumerate the inventory; record ``n_before`` + the
       per-source tally.
    2. for EACH item, re-fetch the cell from its IDENTIFIERS (NOT a stored proxy) and
       call ``cell.MakeSolveFixed()``. Each call is GUARDED — a per-cell throw does NOT
       abort the bulk op (clear as many as possible, then report via the read-back).
    3. RE-ENUMERATE -> 0 read-back proof: the clear is PROVEN only when the re-enumerate is
       empty. A non-empty re-enumerate (a silent ``MakeSolveFixed`` no-op / a per-cell throw
       left a residual) -> a REFUSAL envelope (``ok:false``, ``cleared_all:false``, the
       ``variable_lifecycle`` family, ``unclear_residual`` listing the cells that stayed
       Variable). The cells that DID clear STAY cleared (no rollback — a bulk clear has no
       checkpoint). The ``MakeSolveFixed`` bool is NEVER the proof; the re-enumerate-to-0 is.

    ``n_before == 0`` is a valid success (nothing was Variable). Returns the success/refusal
    envelope dict. NEVER raises past its boundary (L26 — a generic engine throw -> the
    ``variable_lifecycle`` family with the residual disclosed if readable).
    """
    try:
        member = _solve_type_variable_enum(system)
        before_faults = []
        before = _variable_inventory(system, member, before_faults)
        n_before = len(before)
        by_source = _by_source_tally(before)
        for item in before:
            cell = _refetch_inventory_cell(system, item)
            if cell is None:
                continue
            try:
                cell.MakeSolveFixed()
            except Exception:  # noqa: BLE001 — a per-cell throw does not abort the bulk op
                continue
        # RE-ENUMERATE-TO-0 read-back proof (the MakeSolveFixed bool is NEVER the proof).
        # Thread the fault signal so a SILENT per-source discovery fault (a source
        # whose enumeration deterministically faults -> [] for that source) can NEVER be
        # mistaken for a clean re-enumerate. A genuinely-Variable cell behind the fault is
        # invisible to BOTH the clear walk AND a naive empty-list read-back proof; a faulted
        # enumeration MUST NOT certify a clean clear.
        after_faults = []
        after = _variable_inventory(system, member, after_faults)
        n_after = len(after)
        enum_faults = before_faults + after_faults
        if n_after > 0:
            return error_envelope(
                "clear_all_variables", _VARIABLE_LIFECYCLE_FAMILY,
                f"{n_after} variable solve(s) did not clear (read-back: still Variable); "
                "refusing to claim a clean clear",
                cleared_all=False,
                n_cleared=n_before - n_after,
                by_source=by_source,
                n_before=n_before,
                n_after=n_after,
                unclear_residual=after,
                enumeration_complete=not enum_faults,
                enumeration_faults=enum_faults,
            )
        if enum_faults:
            # The re-enumerate read EMPTY, but a source's discovery deterministically faulted
            # -> the enumeration is INCOMPLETE; a Variable cell behind the fault would be
            # invisible. Refuse to claim a clean clear over a faulted enumeration.
            faulted_sources = sorted({f["source"] for f in enum_faults})
            return error_envelope(
                "clear_all_variables", _VARIABLE_LIFECYCLE_FAMILY,
                "variable enumeration was INCOMPLETE (discovery fault on source(s): "
                f"{', '.join(faulted_sources)}); refusing to claim a clean clear over a "
                "faulted enumeration — a Variable cell behind the fault would be invisible",
                cleared_all=False,
                n_cleared=n_before - n_after,
                by_source=by_source,
                n_before=n_before,
                n_after=n_after,
                unclear_residual=[],
                enumeration_complete=False,
                enumeration_faults=enum_faults,
            )
        return {
            "ok": True,
            "tool": "clear_all_variables",
            "n_cleared": n_before - n_after,
            "by_source": by_source,
            "cleared_all": True,
            "cleared_items": before,
            "frozen_at_current": True,
            "note": _CLEAR_ALL_VARIABLES_NOTE,
            "n_before": n_before,
            "n_after": n_after,
        }
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary (L26)
        return error_envelope(
            "clear_all_variables", _VARIABLE_LIFECYCLE_FAMILY,
            f"unexpected engine fault clearing variables ({exc!r}); refusing rather than "
            "claiming a clean clear",
            cleared_all=False,
        )


def _merit_configs_covered(system):
    """Scan the MFE's ``CONF`` rows for the config set a config-spanning merit covers (§2.2).

    A ``build_merit(span_configs=True)`` authors one ``CONF``-bracketed operand block per
    config (probe Q9: ``wizard.Configuration = 0`` -> N ``CONF`` rows). Each ``CONF`` row's
    config number lives in cell col 2, Header ``"Cfg#"``, DataType Integer
    (``op.GetCellAt(2).IntegerValue``). This reads the covered config set back so
    ``build_merit`` (INVARIANT-2 refusal) and ``optimize``/``dry_run`` (the
    ``merit_spans_configs`` disclosure + the single-config WARN) share ONE scan.

    THROW-GUARDED per row (fail-CLOSED): a row whose ``TypeName`` or ``Cfg#`` read throws
    is EXCLUDED from ``covered`` (so INVARIANT-2 catches the gap, never assume-present); a
    missing / wedged MFE -> ``[]``. On a SINGLE-config (non-spanning) build NO ``CONF`` rows
    are authored -> ``covered == []`` (correct + disclosed, NOT a coverage failure — the
    invariant applies ONLY on ``span_configs=True``). Returns a sorted list of ints.
    """
    mfe = getattr(system, "MFE", None)
    if mfe is None:
        return []
    try:
        n_operands = int(mfe.NumberOfOperands)
    except Exception:  # noqa: BLE001 — a wedged MFE -> no coverage read
        return []
    covered = set()
    for i in range(1, n_operands + 1):
        try:
            op = mfe.GetOperandAt(i)
        except Exception:  # noqa: BLE001 — a missing row contributes nothing
            continue
        try:
            type_name = str(op.TypeName)
        except Exception:  # noqa: BLE001 — an unreadable type -> skip this row
            continue
        if type_name != "CONF":
            continue
        try:
            cfg = int(op.GetCellAt(2).IntegerValue)
        except Exception:  # noqa: BLE001 — a degraded CONF row Cfg# read -> EXCLUDE (fail-closed)
            continue
        covered.add(cfg)
    return sorted(covered)


def _uncomputable_row_diagnostics(system, cap=_UNCOMPUTABLE_ROW_CAP):
    """Additive ``{'merit_row_diagnostics': {...}}`` for the optimize_merit_uncomputable envelope.

    Walks ``system.MFE`` (mirrors ``dump_merit_function`` optimize_merit.py:1069-1080 +
    reuses ``_merit_cells.read_param_map``), suspect-ranks zero-value corner rays, and
    returns a single structured diagnostic dict.

    NEVER raises -> returns ``{}`` on ANY fault (missing/wedged MFE, a ``NumberOfOperands``
    throw, a total-scan fault, an empty MFE). A single per-row read fault degrades THAT row
    (``param_read_error``) without aborting the scan. Contract mirrors ``_edge_audit_warnings``
    (optimize_run.py — the whole body wrapped in one outer try/except -> ``{}``).

    Suspect predicate (§2.5): ``is_ray AND value == 0.0 AND (weight finite AND > 0)``. The
    extremity test is the RANKING key only, NOT the predicate. Ranking (§2.5): suspects sorted
    by ``(corner_score DESC, number ASC)``, ``corner_score = hypot(Hx,Hy) + hypot(Px,Py)``, then
    capped at ``cap``. ``n_suspects`` reports the FULL suspect count (may exceed the cap). A
    suspect is a DISCLOSED heuristic shortlist, NEVER asserted as the culprit (deferred).

    Config tracking (§2.6): honest ``null`` for a single-config merit (no ``CONF`` rows) — the
    ``_merit_configs_covered`` CONF-``Cfg#`` idiom, NOT a defaulted ``1``. Multi-config brackets
    stamp each row with the bracket's ``Cfg#``.

    No-zero-suspects fallback (§2.7): the ``>= 1e9`` band is also reached by an undefined
    first-order/paraxial operand (~1e10) where NO row reads ``0.0``. In that case emit a
    ``sample`` list (mutually exclusive with ``suspects``): the top ``cap`` ray rows by
    ``corner_score`` when any ray row exists, else the first ``cap`` rows in row order.
    """
    try:
        mfe = getattr(system, "MFE", None)
        if mfe is None:
            return {}
        # Guard NumberOfOperands (mirror _merit_configs_covered:1279-1282 — a wedged read ->
        # {}). dump_merit_function reads it UNGUARDED; the helper must NOT copy that.
        try:
            n_operands = int(mfe.NumberOfOperands)
        except Exception:  # noqa: BLE001 — a wedged MFE -> the envelope degrades to today's payload
            return {}
        if n_operands < 1:
            return {}

        rows = []
        current_config = None
        multi_config = False
        for i in range(1, n_operands + 1):
            try:
                op = mfe.GetOperandAt(i)
            except Exception:  # noqa: BLE001 — a missing row contributes nothing (skip)
                continue
            try:
                type_name = str(op.TypeName)
            except Exception:  # noqa: BLE001 — an unreadable type -> best-effort ""
                type_name = ""

            # §2.6 config tracking — a CONF row updates current_config BEFORE it (and every
            # subsequent row) is stamped, so the CONF row carries its own bracket number.
            if type_name == "CONF":
                multi_config = True
                try:
                    current_config = int(op.GetCellAt(2).IntegerValue)
                except Exception:  # noqa: BLE001 — a degraded Cfg# read -> leave current_config
                    pass

            # Value / Weight, both THROW-guarded per row. ``*_num`` is the finite python float
            # (or None) for the predicate; ``*_wire`` is the safe_float JSON value for the wire.
            value_wire, value_num = _read_operand_number(op, "Value")
            weight_wire, weight_num = _read_operand_number(op, "Weight")

            # §2.4 params — read_param_map raises SurfaceWriteError on a bad cell; a per-row
            # fault degrades THAT row (params={}, param_read_error=True), row STILL listed.
            param_read_error = False
            try:
                params_raw = _merit_cells.read_param_map(op)
                # A non-finite (NaN/inf) param cell would break strict
                # json.dumps(allow_nan=False); coerce each value JSON-safe (a coordinate
                # -> null) so the wire never ships a raw non-finite float.
                params = {h: _json_safe_param(c["value"]) for h, c in params_raw.items()}
            except Exception:  # noqa: BLE001 — a bad cell degrades the row, never the scan
                params = {}
                param_read_error = True

            hx = _finite_or_zero(params.get("Hx"))
            hy = _finite_or_zero(params.get("Hy"))
            px = _finite_or_zero(params.get("Px"))
            py = _finite_or_zero(params.get("Py"))
            is_ray = any(k in params for k in ("Hx", "Hy", "Px", "Py"))
            field_r = math.hypot(hx, hy)
            pupil_r = math.hypot(px, py)
            corner_score = field_r + pupil_r

            suspect = bool(
                is_ray
                and value_num is not None and value_num == 0.0
                and weight_num is not None and weight_num > 0
            )

            row = {
                "number": i,
                "type": type_name,
                "config": current_config,
                "value": value_wire,
                "weight": weight_wire,
                "field_r": field_r,
                "pupil_r": pupil_r,
                "corner_score": corner_score,
                "params": params,
                "suspect": suspect,
                "_is_ray": is_ray,
            }
            if param_read_error:
                row["param_read_error"] = True
            rows.append(row)

        if not rows:  # every row unreadable -> degrade to today's payload
            return {}

        n_ray_operands = sum(1 for r in rows if r["_is_ray"])
        suspect_rows = [r for r in rows if r["suspect"]]
        n_suspects = len(suspect_rows)

        def _corner_key(r):
            return (-r["corner_score"], r["number"])

        result = {
            "n_operands": n_operands,
            "n_ray_operands": n_ray_operands,
            "n_suspects": n_suspects,
            "cap": cap,
            "multi_config": multi_config,
        }
        if multi_config:
            result["configs_suspect"] = sorted(
                {int(r["config"]) for r in suspect_rows if r["config"] is not None}
            )

        if n_suspects > 0:
            ranked = sorted(suspect_rows, key=_corner_key)[:cap]
            top_field_r = ranked[0]["field_r"]
            all_zero = (n_ray_operands > 0 and n_suspects == n_ray_operands)
            prefix = (
                f"ALL {n_ray_operands} ray rows read Value==0.0; " if all_zero else ""
            )
            result["headline"] = (
                f"{prefix}{n_suspects} suspect rows of {n_operands} (zero-value ray "
                f"operands, corner-ray family field_r~{top_field_r:.3g}); showing the "
                f"{len(ranked)} most extreme corner-first. A suspect reads Value==0.0 at "
                "a wide-field/high-pupil corner (heuristic, not a proven culprit) — the "
                "corner ray that fails to trace. Apply the vignetting->rebuild ESCAPE in "
                "this envelope's error text."
            )
            result["suspects"] = [_diag_row_wire(r, suspect=True) for r in ranked]
        else:
            if n_ray_operands > 0:
                ranked = sorted(
                    (r for r in rows if r["_is_ray"]), key=_corner_key
                )[:cap]
                result["headline"] = (
                    "no zero-value ray suspects — the merit may be uncomputable from an "
                    "undefined first-order/paraxial operand (EFFL/TOTR ~1e10) rather than "
                    "a corner-ray trace failure; showing the "
                    f"{len(ranked)} most extreme ray rows"
                )
            else:
                ranked = rows[:cap]
                result["headline"] = (
                    "no ray operands in this merit; showing the first "
                    f"{len(ranked)} rows"
                )
            result["sample"] = [_diag_row_wire(r, suspect=False) for r in ranked]

        # (disclosed-degrade contract §2.4/§3): a param_read_error row has empty
        # params -> is_ray False -> it is NEVER a suspect and NEVER in the ray-ranked
        # fallback, so it VANISHES undisclosed the moment a genuine suspect (or any ray row
        # in the fallback) coexists. It could BE the culprit (a corner ray whose cells
        # failed to read), so ALWAYS disclose it. Append any error row not already emitted,
        # flagged (suspect=False, param_read_error=True) and AFTER the capped shortlist so it
        # can never displace a real suspect from its cap slots. The append is itself capped;
        # the FULL count rides ``n_param_read_errors`` so a truncated flood stays disclosed.
        # ``present`` dedups a row already in ``sample`` (ray-free path) -> no double-listing;
        # an error row is never in ``suspects`` (is_ray False) so no double-append there.
        error_rows = [r for r in rows if r.get("param_read_error")]
        if error_rows:
            result["n_param_read_errors"] = len(error_rows)
            emitted = result["suspects"] if "suspects" in result else result["sample"]
            present = {r["number"] for r in emitted}
            appended = 0
            for r in error_rows:
                if appended >= cap:  # cap the APPEND too; n_param_read_errors discloses the rest
                    break
                if r["number"] in present:
                    continue
                emitted.append(_diag_row_wire(r, suspect=False))
                appended += 1

        return {"merit_row_diagnostics": result}
    except Exception:  # noqa: BLE001 — the load-bearing never-raise guarantee (degrade to {})
        return {}


def _read_operand_number(op, attr):
    """Read ``op.<attr>`` -> ``(wire, num)``: the safe_float wire value + a finite float|None.

    ``wire`` is the JSON-safe value (``safe_float``: a non-finite float becomes an "inf"/"nan"
    string) for the envelope; ``num`` is the raw finite python float (or ``None`` when the read
    throws / is non-numeric / non-finite) for the suspect predicate's exact-``0.0`` /
    ``weight > 0`` comparisons (which must NEVER compare against a string sentinel). NEVER raises.
    """
    try:
        raw = getattr(op, attr)
    except Exception:  # noqa: BLE001 — a degraded read -> no value
        return None, None
    wire = safe_float(raw)
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return wire, None
    return wire, (f if math.isfinite(f) else None)


def _finite_or_zero(value):
    """A param value coerced to a finite float, else ``0.0`` (the §2.4 Hx/Hy/Px/Py default)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _json_safe_param(value):
    """A param cell value made strict-JSON-safe: a non-finite float -> ``None`` (null), else as-is.

    Params (Hx/Hy/Px/Py/Wave/Surf#) ship on the wire; a NaN/inf coordinate would break
    ``json.dumps(allow_nan=False)``. A null coordinate is the honest degrade (unlike ``value``/
    ``weight``, which keep the ``safe_float`` "nan"/"inf" sentinel — a coordinate has no
    diagnostic use for the sentinel). Finite floats, ints, strings, and ``None`` pass through
    untouched, so the ranking (which re-coerces via ``_finite_or_zero``) is undisturbed."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _diag_row_wire(row, *, suspect):
    """Project an internal scan row to its wire per-row dict (drops the ``_is_ray`` scratch key)."""
    wire = {
        "number": row["number"],
        "type": row["type"],
        "config": row["config"],
        "value": row["value"],
        "weight": row["weight"],
        "field_r": row["field_r"],
        "pupil_r": row["pupil_r"],
        "corner_score": row["corner_score"],
        "params": row["params"],
        "suspect": suspect,
    }
    if row.get("param_read_error"):
        wire["param_read_error"] = True
    return wire


def _check_stop_convention(system):
    """The dummy-stop guard (§6.2): is the aperture stop on a glass vertex?

    Delegates to ``_structural_common._classify_stop`` (the SAME two-Material-read +
    IsStop detection ``normalize_stop`` uses). Returns ``(ok, family, stop_idx,
    stop_material)``:

    - classification ``"on_glass_vertex"`` -> ``(False, "stop_on_glass_vertex",
      stop_idx, <glass>)`` — a real silent-unbuildable trap (the stop coincides
      with a lens vertex, denying the optimizer the stop-position DOF; probe (a)).
    - classification ``"indeterminate"`` (a Material read FAILED) -> ``(False,
      "stop_indeterminate", stop_idx, "")`` — REFUSE-to-optimize, but a DISTINCT
      family from ``stop_on_glass_vertex`` so the ``auto_normalize`` orchestration
      (which fires ONLY on ``stop_on_glass_vertex``) NEVER mutates a system whose
      stop we could not even read. A transient read does not become a hard crash
      (it is a structured refusal) and it does not authorize a mutation.
    - ANY other classification (``"free_airspace"`` OR ``"no_stop"`` — a stopless/
      afocal system passes the gate untouched; the guard NEVER blocks spuriously)
      -> ``(True, None, stop_idx, None)``.

    NON-MUTATING; opens nothing. The ``stop_material`` is read for the refusal
    envelope's diagnostic (guarded — a read failure degrades to ``""``).
    """
    stop_idx, classification = _classify_stop(system.LDE)
    if classification == _INDETERMINATE:
        # A Material read failed. Refuse (never guess a vertex), under a DISTINCT
        # family so auto_normalize cannot mutate an already-valid system on a hiccup.
        return (False, "stop_indeterminate", stop_idx, "")
    if classification != "on_glass_vertex":
        return (True, None, stop_idx, None)
    try:
        stop_material = str(system.LDE.GetSurfaceAt(stop_idx).Material)
    except Exception:  # noqa: BLE001 — the diagnostic read must never crash the gate
        stop_material = ""
    return (False, "stop_on_glass_vertex", stop_idx, stop_material)


def _preflight(system, *, require_free_stop=True):
    """The NON-MUTATING dry-run gate (§e + §6.2). Opens NO optimizer.

    Three gates, evaluated in order (the FIRST failure is reported — so a more
    fundamental structural gap surfaces before the stop-convention nuance):

    - **variables gate:** at least one LDE cell set Variable (the LDE scan, not
      ``opt.Variables`` — the optimizer is never opened here). ``count == 0`` ->
      ``no_variables``.
    - **merit gate:** ``mfe.NumberOfOperands > 0`` AND ``mfe.CalculateMeritFunction()``
      finite AND ``> 0`` (a 0.0 merit is the fresh-lens placeholder; a non-finite
      merit is a broken system). Either false -> ``no_merit``.
    - **merit-uncomputable gate:** AFTER the merit gate, BEFORE the stop gate.
      A finite ``> 0`` merit ``>= _MERIT_UNCOMPUTABLE_CEILING`` (the engine's 9e9
      could-not-compute sentinel / ~1e10 undefined first-order) -> ``merit_uncomputable``
      (DISJOINT from ``no_merit``, which owns non-finite/<=0). Non-mutating.
    - **stop-convention gate (§6.2):** ONLY when ``require_free_stop`` is True,
      a POSITIVE ``on_glass_vertex`` detection -> ``stop_on_glass_vertex``. A
      free-standing / stopless system passes untouched (the guard never blocks
      spuriously). ``require_free_stop=False`` skips this gate entirely.

    Returns ``(ok: bool, family: str|None, variables: int, number_of_operands: int,
    merit: float, stop_idx: int|None, stop_material: str|None)``. ``merit`` is the
    RAW float read (the caller passes it through ``safe_float`` for the wire).
    ``stop_idx``/``stop_material`` are populated only on the ``stop_on_glass_vertex``
    family (else ``None``) for the §6.4 refusal envelope.
    """
    variable_member = _solve_type_variable_enum(system)
    variables = _count_variables(system.LDE, variable_member, system=system)

    mfe = system.MFE
    number_of_operands = int(mfe.NumberOfOperands)
    merit = mfe.CalculateMeritFunction()

    if variables == 0:
        return (False, "no_variables", variables, number_of_operands, merit,
                None, None)

    merit_ok = (
        number_of_operands > 0
        and isinstance(merit, (int, float))
        and not isinstance(merit, bool)
        and math.isfinite(merit)
        and merit > 0
    )
    if not merit_ok:
        return (False, "no_merit", variables, number_of_operands, merit, None, None)

    # NEW: the uncomputable-merit sentinel gate. A finite >0 merit that is >= the
    # could-not-compute ceiling (9e9 sentinel / ~1e10 undefined first-order) is the engine's
    # "ray failed to trace" signal — the optimizer would reject it as "Input settings are
    # invalid". Caught HERE so dry_run stops lying (ready->hard-fail). After no_merit
    # (disjoint family), BEFORE the stop gate. Non-mutating; opens nothing. The merit value
    # is CARRIED through for the envelope diagnostic (it IS the 9e9 number).
    if _merit_is_uncomputable(merit):
        return (False, "merit_uncomputable", variables, number_of_operands, merit,
                None, None)

    # §6.2: the stop-convention gate, AFTER the two more-fundamental gates and
    # only when enforced. A free/stopless system passes; a vertex stop refuses.
    if require_free_stop:
        stop_ok, stop_family, stop_idx, stop_material = _check_stop_convention(system)
        if not stop_ok:
            return (False, stop_family, variables, number_of_operands, merit,
                    stop_idx, stop_material)

    return (True, None, variables, number_of_operands, merit, None, None)


# --------------------------------------------------------------------------- #
# The verdict classifier (§d — diverged-on-non-finite FIRST).
# --------------------------------------------------------------------------- #
def _is_nonfinite(value):
    """True if ``value`` is a non-finite float (NaN / +inf / -inf), guarded.

    Decided on the RAW float BEFORE ``safe_float`` stringifies it (§d): a
    non-finite merit means the optimizer drove the system into a degenerate /
    unraytraceable state. A non-number (the engine returned something odd) is
    treated as non-finite too — it cannot be a valid merit.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return not math.isfinite(value)


def classify_verdict(before, after, *, rel_tol=_VERDICT_REL_TOL, abs_tol=_VERDICT_ABS_TOL):
    """Classify a before/after merit pair into ``improved`` / ``stable`` / ``diverged``.

    Evaluated in THIS order (§d):

    1. **diverged (non-finite FIRST):** a non-finite ``after`` OR ``before`` (RAW
       float, before ``safe_float``) -> ``"diverged"``.
    2. **diverged (worse):** ``after > before`` AND NOT
       ``math.isclose(after, before, rel_tol, abs_tol)`` -> ``"diverged"``.
    3. **improved:** ``after < before`` AND NOT ``math.isclose(...)`` -> ``"improved"``.
    4. **stable:** otherwise (within tol — a no-op / sub-tol drift) -> ``"stable"``.

    A no-op (``after == before``) MUST read ``"stable"``, never ``"improved"`` (the
    load-bearing assertion of the classifier).
    """
    # (1) non-finite FIRST — checked on the raw float.
    if _is_nonfinite(after) or _is_nonfinite(before):
        return "diverged"
    close = math.isclose(after, before, rel_tol=rel_tol, abs_tol=abs_tol)
    # (2) worse (and not within tol).
    if after > before and not close:
        return "diverged"
    # (3) better (and not within tol).
    if after < before and not close:
        return "improved"
    # (4) within tol — a no-op / sub-tol move.
    return "stable"


def _readback_disagreement(opt_current, mfe_recalc):
    """The tripwire: do the optimizer + MFE merit reads disagree?

    Returns ``True`` only when the two reads differ beyond the TIGHTER readback
    tolerance (a "should be bit-identical" check). A non-finite value on
    either side counts as a disagreement (a finite/non-finite pair is never
    bit-identical). NON-FATAL: the caller flags it + warns but never lets it change
    ``ok`` or the verdict.
    """
    if _is_nonfinite(opt_current) or _is_nonfinite(mfe_recalc):
        return True
    return not math.isclose(
        opt_current, mfe_recalc, rel_tol=_READBACK_REL_TOL, abs_tol=_READBACK_ABS_TOL
    )


# --------------------------------------------------------------------------- #
# The optimizer-lifecycle context manager (§c — the L22 single-seat reap).
# --------------------------------------------------------------------------- #
def _clamp_cores(opt, cores):
    """Clamp ``cores`` to ``[1, opt.MaxCores]`` and apply it (§6).

    ``cores is None`` -> leave ``opt.NumberOfCores`` at the engine default (do NOT
    force a value). Otherwise read ``opt.MaxCores`` (only meaningful AFTER open),
    clamp to ``[1, MaxCores]``, set ``opt.NumberOfCores``, and return the clamped
    value. ``MaxCores`` is read guarded; if it is unavailable the lower clamp (>=1)
    still applies.
    """
    if cores is None:
        return None
    lo = 1
    try:
        hi = int(opt.MaxCores)
    except Exception:  # noqa: BLE001 — no MaxCores -> only the lower clamp applies
        hi = None
    clamped = max(lo, int(cores))
    if hi is not None and hi >= lo:
        clamped = min(clamped, hi)
    opt.NumberOfCores = clamped
    return clamped


@contextmanager
def _optimizer_session(system, *, algorithm_member=None, cores=None,
                       cycles_member=None):
    """Open the local optimizer ONCE, configure it, and ALWAYS ``Close()`` it.

    Locked §c lifecycle:

    1. ``opt = system.Tools.OpenLocalOptimization()``. If ``opt is None`` the
       single-instance optimizer is ALREADY open -> raise
       ``OptimizeError(family="optimize_unavailable")`` WITHOUT entering the
       try/finally (there is nothing to Close).
    2. configure (``Algorithm``, ``Cycles``, ``NumberOfCores`` clamped to
       ``MaxCores``). ``Cycles`` is an ``OptimizationCycles`` enum MEMBER — the
       cycle COUNT is set here, NOT passed to
       ``RunAndWaitForCompletion()`` (which takes no arguments).
    3. yield ``(opt, cores_used)`` for the caller to run + read merit.
    4. ``finally: opt.Close()`` ALWAYS — guarded so a teardown failure never masks
       the run outcome (the ``_run_analysis``-style guard). This is the L22
       single-seat reap: spawned 1 / reaped 1 / leftover 0.

    A second ``OpenLocalOptimization()`` returning ``None`` is the ONLY "unavailable"
    signal — no name-sweep, no kill, no retry, never the user's interactive session.
    """
    opt = system.Tools.OpenLocalOptimization()
    if opt is None:
        # Do NOT enter the try/finally — there is no handle to Close.
        raise OptimizeError(
            "OpenLocalOptimization returned None (optimizer already open)",
            family="optimize_unavailable",
        )
    try:
        if algorithm_member is not None:
            opt.Algorithm = algorithm_member
        if cycles_member is not None:
            opt.Cycles = cycles_member
        cores_used = _clamp_cores(opt, cores)
        yield opt, cores_used
    finally:
        try:
            opt.Close()
        except Exception:  # noqa: BLE001 — optimizer teardown must never raise
            pass


@contextmanager
def _hammer_session(system, *, run_time_m=None, cores=None):
    """Open the Hammer (global-search) optimizer ONCE, configure it, ALWAYS ``Close()``.

    The SIBLING of ``_optimizer_session`` for the
    ``algorithm='Hammer'`` fork: Hammer opens a DIFFERENT ``Tools`` resource
    (``OpenHammerOptimization``) and is NOT an ``OptimizationAlgorithm`` member, so it needs
    its own open/reap but reuses the same ``_clamp_cores`` guard + the L22 single-seat
    discipline.

    Lifecycle:

    1. ``ham = system.Tools.OpenHammerOptimization()``. ``None`` -> a ``Tools`` resource is
       ALREADY open -> raise ``OptimizeError(family="optimize_unavailable")`` WITHOUT
       entering the try/finally (there is nothing to Close).
    2. ``ham.AutomaticOptimization = True`` FIRST — the LOAD-BEARING flag (probe round 2):
       without it ``RunAndWaitForCompletion()`` silently NO-OPS (returns False, merit
       unchanged, no commit). Then set ``TargetRunTimeM`` (a wall-time CAP, minutes; auto
       mode stops early on converge) and clamp cores.
    3. yield ``(ham, cores_used)`` for the caller to run ONE ``RunAndWaitForCompletion()``
       + read ``CurrentMeritFunction``.
    4. ``finally: ham.Close()`` ALWAYS (guarded — a teardown failure never masks the run
       outcome). This is the L22 single-seat reap.

    HARD constraints (each a probe-falsified trap — the manager DELIBERATELY does not expose
    them): call ONLY ``RunAndWaitForCompletion()``. NEVER ``Run()`` / ``RunAndWaitWithTimeout()``
    / ``Cancel()`` — they improve an INTERNAL copy that never commits to the live LDE AND WEDGE
    the single-instance ``Tools`` resource (the next ``OpenHammerOptimization()`` -> None),
    even after ``Cancel()`` (probe rounds 3, 4). ``ham.Systems`` is an int (0), NOT a
    candidate collection — no copy-back path.
    """
    ham = system.Tools.OpenHammerOptimization()
    if ham is None:
        # Do NOT enter the try/finally — there is no handle to Close.
        raise OptimizeError(
            "OpenHammerOptimization returned None (a Tools resource is already open)",
            family="optimize_unavailable",
        )
    try:
        # AutomaticOptimization FIRST — load-bearing on all three axes (probe round 2/5):
        # it makes RunAndWaitForCompletion RUN, COMMIT to the live LDE, AND release cleanly.
        ham.AutomaticOptimization = True
        if run_time_m is not None:
            ham.TargetRunTimeM = float(run_time_m)  # a wall CAP; auto mode stops early
        cores_used = _clamp_cores(ham, cores)       # reuse (guards a missing MaxCores)
        yield ham, cores_used
    finally:
        try:
            ham.Close()                             # L22 single-seat reap
        except Exception:  # noqa: BLE001 — teardown must never mask the run outcome
            pass


__all__ = [
    "error_envelope",
    "classify_verdict",
    "_preflight",
    "_merit_is_uncomputable",
    "_MERIT_UNCOMPUTABLE_CEILING",
    "_check_stop_convention",
    "_classify_stop",
    "_count_variables",
    "_count_mce_variables",
    "_count_asphere_variables",
    "_variable_inventory",
    "_disclose_inherited_variables",
    "_clear_all_variables_core",
    "_clear_solve_to_fixed_proven",
    "_scan_inert_dofs",
    "_scan_rayfree_merit",
    "_RAY_TRACE_OPERANDS",
    "_surface_is_inert",
    "_row_is_mirror_or_cb",
    "_VARIABLE_LIFECYCLE_FAMILY",
    "_scan_per_config_thin",
    "_merit_configs_covered",
    "_uncomputable_row_diagnostics",
    "_UNCOMPUTABLE_ROW_CAP",
    "_optimizer_session",
    "_hammer_session",
    "_readback_disagreement",
    "_merit_operand_enum",
    "_optimization_algorithm_enum",
    "_optimization_cycles_enum",
    "_cycles_member_name",
    "_resolve_cycles_member",
    "_solve_type_variable_enum",
    "_VERDICT_REL_TOL",
    "_VERDICT_ABS_TOL",
]
