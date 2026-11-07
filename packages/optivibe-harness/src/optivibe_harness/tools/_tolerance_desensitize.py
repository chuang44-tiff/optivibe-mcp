"""tools/_tolerance_desensitize.py -- ``tolerance(mode="desensitize")``: the canned CONTROL recipe. NOT dispatchable.

One call = BEFORE (a production S-PF sensitivity run) -> pick the top hotspot's first modelable
perturbation (TSTX / TSTY / TTHI) -> build a 3-configuration lens (nominal, +delta, -delta) with copies
of the image-quality merit rows per perturbed config -> one ``Fixed_10_Cycles`` DLS -> TEAR EVERYTHING
DOWN and PROVE the lens restored (except the Variable values the DLS was asked to move) -> AFTER (a second
S-PF run on the single-config lens). The judge is the two S-PF runs; the recipe merit is reported with
``"note": "not the judge"`` and no verdict key exists.

The recipe CHANGES the prescription by design (the Variable cells the DLS moved); the change is DECLARED
(``prescription_changed``, a warning), never silent. It never writes a lens file and never calls
``ITolerancing.Save``.

WHAT THIS FILE OWNS ON EVERY FAILURE PATH: the build registers an undo BEFORE each mutation
(layout undos on ``ctx["undo"]``, the appended merit rows by RECONCILIATION against ``n_base``, the
configurations by a flag), and ``teardown`` runs in a ``finally`` in this measured order:

    1 remove the appended rows by count from the END   (before ANY evaluation)
    2 set_current_configuration(1)    3 reset_to_single_config    4 drop the leading CONF 1
    5 remove the CB layout            6 re-mark the Variable cells   7 re-impose the merit   8 proof

with the DEPENDENCY-AWARE stop rule (a failing step stops only the steps that depend on it; the proof
always runs). The two hazards it exists for, both measured live (P-M2): a plain merit evaluation on a
multi-config lens with no leading CONF inserts CONF 1 AND DELETES THE LAST ROW (so the build makes no
evaluation between ``add_configuration`` and the first ``set_config_variable``, and a guard proves it),
and ``reset_to_single_config`` bakes a per-config Variable as Fixed (so the Variables are re-marked).
"""
import copy
import math
import os
import time

from . import _analysis_common as _ac
from . import _layout_geometry as _geom
from . import _lens_state
from . import _mce_cells as _mce
from . import _merit_cells
from . import _optimize_common as _oc
from . import _run_budget as _rb
from . import _solve_cells as _sc
from . import _tolerance_layout as _tl
from . import mce_config, mce_reset, surface_solve
from ..enums import _resolve_enum
from ..errors import OptimizeError

TOOL = "tolerance"

# The image-quality merit operand types that are copied per perturbed config (the probe's 17).
IQ_TYPES = frozenset({"OPDX", "OPDC", "OPDM", "TRAC", "TRAR", "TRAX", "TRAY", "TRCX", "TRCY", "RWRE",
                      "RWRH", "RWCE", "RWCH", "RSCE", "RSCH", "RSRE", "RSRH"})
# The perturbations the recipe can model; anything else in the hotspot is skipped ``unmodelled_type``.
MODELLED = ("TSTX", "TSTY", "TTHI")
# The zero-delta canary: 10x the largest measured residual (1.50e-10, P-M2 Q1 / Q5).
CANARY_REL = 1.5e-9
DLS_CAP_S = 300.0
DLS_CANCEL_WAIT_S = 30.0
DLS_CYCLES = 10
# The default MCE floor row of a fresh / reset single-config lens: pinned by P-M3
# (``probe/pm3_mce_floor.json``, OpticStudio 241210, both bench lenses, three floor states). Compared as
# ``(type name, tuple(_mce_cells.read_param(row, k) for k in (1, 2, 3)))``, NEVER through ``GetCellAt``.
_MCE_FLOOR_ROW = ("MOFF", (0, 0, 0))
RECOVERY = "reload your saved candidate with load_design"
_PARAM_NOT_ALLOWED = ("tolerances", "include_mechanical", "strict", "cb_tilt", "cb_group")
_BAD_REASON = "build_failed"


class _Fail(Exception):
    """An expected recipe failure: the envelope ``family`` + a machine ``reason`` + an optional ``extra`` dict."""

    def __init__(self, message, *, family="tolerancing_run", reason=_BAD_REASON, extra=None):
        super().__init__(message)
        self.message = message
        self.family = family
        self.reason = reason
        self.extra = dict(extra or {})


def _as_fail(exc):
    """Normalise any exception into a ``_Fail`` (``LayoutError`` / ``ToleranceError`` carry family + reason)."""
    if isinstance(exc, _Fail):
        return exc
    reason = getattr(exc, "reason", None) or _BAD_REASON
    family = getattr(exc, "family", None)
    if not isinstance(family, str) or not family.startswith("tolerancing"):
        family = "tolerancing_run"
    extra = {}
    if getattr(exc, "value", None) is not None:
        extra["value"] = _tl._jsonable(exc.value)
    return _Fail(str(exc) or repr(exc), family=family, reason=reason, extra=extra)


def _checkpoint(name):
    """A no-op seam at every step boundary of ``run``: a test (FailAt) replaces it to raise at ``name``."""


def _mark(ctx, name):
    ctx["stage"] = name
    _checkpoint(name)


def _refusal(family, reason, message, **extra):
    return _ac.error_envelope(TOOL, family, message, reason=reason, mode="desensitize", **extra)


def _tool(fn, session, params, what):
    """Call a production tool; a raise or an ``ok:false`` envelope is a build failure carrying its text."""
    try:
        res = fn(session, params)
    except Exception as exc:  # noqa: BLE001 -- set_solve / clear_solve RAISE on refusal
        raise _Fail("%s failed: %s" % (what, exc), reason=_BAD_REASON) from exc
    if isinstance(res, dict) and res.get("ok") is False:
        raise _Fail("%s refused: %s" % (what, res.get("error")), reason=_BAD_REASON)
    return res if isinstance(res, dict) else {}


def _close(a, b):
    return a == b or (math.isfinite(a) and math.isfinite(b) and abs(a - b) <= 1e-12 * max(1.0, abs(a)))


# --------------------------------------------------------------------------- #
# validation: PURE reads, nothing opened or authored
# --------------------------------------------------------------------------- #
def _mce_state(system):
    """``(n_configs, n_operands, floor_row)`` with ``floor_row = (type name, (Param1..3))`` or ``None``."""
    mce = system.MCE
    n_conf, n_ops = int(mce.NumberOfConfigurations), int(mce.NumberOfOperands)
    floor = None
    if n_ops >= 1:
        op = mce.GetOperandAt(1)
        floor = (_tl._tok(op.Type).rsplit(".", 1)[-1], tuple(_mce.read_param(op, k) for k in (1, 2, 3)))
    return n_conf, n_ops, floor


def validate(params, system):
    """Validate the call. Returns an error envelope, or ``None`` when the call may proceed."""
    if not isinstance(dict.get(params, "budget"), dict):
        return _refusal("tolerancing_param", "budget_required",
                        "mode='desensitize' needs budget={vendor, grade, element_decenter, element_tilt}: the "
                        "judge is the production S-PF run on the wizard budget. Nothing was opened.")
    comp = dict.get(params, "compensator")
    if comp not in (None, "paraxial"):
        return _refusal("tolerancing_param", "compensator_not_paraxial",
                        "mode='desensitize' judges with the paraxial S-PF runs; compensator must be absent "
                        "or 'paraxial'. Nothing was opened.")
    for key in _PARAM_NOT_ALLOWED:
        if dict.__contains__(params, key):
            return _refusal("tolerancing_param", "param_not_allowed:%s" % key,
                            "mode='desensitize' does not take %r. Nothing was opened." % key)
    try:
        n_conf, n_ops, floor = _mce_state(system)
        mfe = system.MFE
        n_rows = int(mfe.NumberOfOperands)
        conf_rows, ref_rows = [], []
        for i in range(1, n_rows + 1):
            op = mfe.GetOperandAt(i)
            if _tl._tok(op.TypeName) == "CONF":
                conf_rows.append(i)
            if _merit_cells.read_ref_map(op):
                ref_rows.append(i)
        n_surf = int(system.LDE.NumberOfSurfaces)
        cb_rows = [i for i in range(n_surf)
                   if _geom._is_coordinate_break(str(system.LDE.GetSurfaceAt(i).Type))]
    except Exception as exc:  # noqa: BLE001 -- an unreadable lens proves nothing: fail closed
        return _refusal("tolerancing_run", "state_unreadable",
                        "the lens state could not be read (%r); nothing was opened or changed" % (exc,))
    if n_conf > 1 or conf_rows:
        return _refusal("tolerancing_param", "multi_config",
                        "mode='desensitize' needs a single-configuration lens with no CONF row in the merit "
                        "(%d configurations, CONF rows %s). Nothing was opened." % (n_conf, conf_rows[:5]))
    if n_ops != 1 or floor != _MCE_FLOOR_ROW:
        return _refusal("tolerancing_param", "mce_not_default",
                        "the multi-configuration editor is not at its default floor (%d row(s), row 1 = %r, "
                        "expected one %r row): the recipe resets the editor and would lose authored rows. "
                        "Nothing was opened." % (n_ops, floor, _MCE_FLOOR_ROW))
    if ref_rows:
        return _refusal("tolerancing_param", "op_ref_rows",
                        "merit rows %s carry an Op#-prefixed row reference; whether the engine relocates "
                        "absolute row references when the recipe's leading CONF row shifts them is unprobed, "
                        "so the recipe refuses. Nothing was opened." % (ref_rows[:10],))
    if cb_rows:
        return _refusal("tolerancing_param", "cb_present",
                        "the lens holds coordinate-break surface(s) %s; the recipe's CB layout and surface "
                        "numbering do not support one. Nothing was opened." % (cb_rows[:5],))
    from . import tolerance_run as _tr          # lazy: tolerance_run imports this module lazily
    try:
        _tr._dls_preflight(system)
    except Exception as exc:  # noqa: BLE001 -- a ToleranceError carrying the S2a back-gap text
        return _refusal("tolerancing_param", "back_gap_unusable", str(exc))
    back = n_surf - 2
    solve = _tl._solve_of(system.LDE.GetSurfaceAt(back), "thickness")
    if solve not in ("Fixed", "Variable"):
        return _refusal("tolerancing_param", "back_gap_unusable",
                        "the back gap (surface %d) thickness carries a %s solve; the recipe needs it Fixed or "
                        "Variable. Nothing was opened." % (back, solve))
    return None


# --------------------------------------------------------------------------- #
# the perturbation walk
# --------------------------------------------------------------------------- #
def _sens_entry(sensitivity_rows, type_name, surface, surface2):
    for r in sensitivity_rows or ():
        if (r.get("type"), r.get("surface"), r.get("surface2")) == (type_name, surface, surface2):
            return r
    return None


def _finite_nonzero(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x != 0


def _precondition(type_name, surface, surface2, system, found):
    """The skip reason for a modelled row, or ``None`` when its build preconditions hold."""
    n = int(system.LDE.NumberOfSurfaces)
    back = n - 2

    def _thick(i):
        return found.get("s%d.Thickness" % i)

    def _solve(i):
        return found.get("s%d.thickness.solve" % i)

    if type_name in ("TSTX", "TSTY"):
        s = surface
        if not isinstance(s, int) or not 1 <= s <= n - 3:
            return "surface_out_of_range"
        # a cemented interface (glass on both sides) is not modelable; a front or back face is (erratum E1)
        if (not _geom._is_air_material(str(found.get("s%d.Material" % s, "")))
                and not _geom._is_air_material(str(found.get("s%d.Material" % (s - 1), "")))):
            return "glass_behind"
        if found.get("stop") == s or int(system.LDE.StopSurface) == s:
            return "stop_in_group"
        if _solve(s) not in ("Fixed", "Variable"):
            return "solve_on_cell"
        if not (isinstance(_thick(s), float) and math.isfinite(_thick(s))):
            return "degenerate_row"
        return None
    # TTHI
    if not isinstance(surface2, int) or isinstance(surface2, bool):
        return "no_adjust_surface"
    if back in (surface, surface2):
        return "back_gap_conflict"
    for i in (surface, surface2):
        if not isinstance(i, int) or not 1 <= i <= n - 3:
            return "surface_out_of_range"
    if _solve(surface) not in ("Fixed", "Variable") or _solve(surface2) not in ("Fixed", "Variable"):
        return "solve_on_cell"
    for i in (surface, surface2):
        if not (isinstance(_thick(i), float) and math.isfinite(_thick(i))):
            return "degenerate_row"
    return None


def select_row(hotspot, sensitivity_rows, system, found):
    """Walk the top hotspot's rows in ``c`` order; return ``(row | None, skipped)``.

    ``row`` = ``{type, surface, surface2, plus, minus, units, hotspot_key, share}`` with ``plus`` the
    report's ``max.delta`` and ``minus`` its ``min.delta`` (signed). Every row walked past goes to
    ``skipped`` as ``{type, surface, surface2, reason}``.
    """
    skipped = []
    for r in (hotspot or {}).get("rows") or ():
        t, s, s2 = r.get("type"), r.get("surface"), r.get("surface2")

        def _skip(reason, t=t, s=s, s2=s2):
            skipped.append({"type": t, "surface": s, "surface2": s2, "reason": reason})

        if t not in MODELLED:
            _skip("unmodelled_type")
            continue
        reason = _precondition(t, s, s2, system, found)
        if reason is not None:
            _skip(reason)
            continue
        entry = _sens_entry(sensitivity_rows, t, s, s2)
        plus = ((entry or {}).get("max") or {}).get("delta")
        minus = ((entry or {}).get("min") or {}).get("delta")
        if not (_finite_nonzero(plus) and _finite_nonzero(minus)):
            _skip("degenerate_row")
            continue
        return ({"type": t, "surface": s, "surface2": s2, "plus": float(plus), "minus": float(minus),
                 "units": r.get("units"), "hotspot_key": hotspot.get("key"), "share": hotspot.get("share")},
                skipped)
    return None, skipped


# --------------------------------------------------------------------------- #
# TTHI: the paraxial back-focus shift
# --------------------------------------------------------------------------- #
def paraxial_bfl_shift(session, found, s, adj, plus, minus, undo):
    """``dbfl`` for the +delta / -delta TTHI states: a MarginalRayHeight (height 0) solve on the back gap.

    Runs on the LIVE single-config system (the probe reloaded a copy) under its OWN undo, registered on
    ``undo`` BEFORE the first write and run -- and removed from ``undo`` -- here in ``finally``, so the
    shift never outlives this call. Read-back: the three thicknesses equal the recorded values within
    rel 1e-12 and the back solve equals the recorded type, else ``bfl_probe_restore``.
    Returns ``{dbfl_plus, dbfl_minus, bfl: {0, +1, -1}, t_s, t_adj, t_back, back_solve}``.
    """
    system = session.system
    lde = system.LDE
    back = int(lde.NumberOfSurfaces) - 2
    t_s, t_adj = float(lde.GetSurfaceAt(s).Thickness), float(lde.GetSurfaceAt(adj).Thickness)
    t_back = float(lde.GetSurfaceAt(back).Thickness)
    back_solve = found.get("s%d.thickness.solve" % back)

    def _restore():
        row = lde.GetSurfaceAt(back)
        if _tl._solve_of(row, "thickness") not in ("Fixed", "Variable"):
            _tool(surface_solve.clear_solve, session, {"surface": back, "cell": "thickness"}, "clear_solve")
        if back_solve == "Variable":
            lde.GetSurfaceAt(back).ThicknessCell.MakeSolveVariable()
        lde.GetSurfaceAt(s).Thickness = t_s
        lde.GetSurfaceAt(adj).Thickness = t_adj
        lde.GetSurfaceAt(back).Thickness = t_back

    entry = ("bfl_probe_restore", _restore)
    undo.append(entry)                                    # registration precedes mutation
    bfl = {}
    body_error = None
    try:
        for sign, d in ((0, 0.0), (1, plus), (-1, minus)):
            lde.GetSurfaceAt(s).Thickness = t_s + d
            lde.GetSurfaceAt(adj).Thickness = t_adj - d
            _tool(surface_solve.set_solve, session,
                  {"surface": back, "cell": "thickness", "solve_type": "MarginalRayHeight",
                   "fields": {"Height": 0.0}}, "set_solve(MarginalRayHeight)")
            _checkpoint("4a.after_solve")
            bfl[sign] = float(lde.GetSurfaceAt(back).Thickness)
    except Exception as exc:  # noqa: BLE001 -- restored below, then re-raised
        body_error = exc
    finally:
        if entry in undo:
            undo.remove(entry)
        try:
            _restore()
            ok = (all(_close(a, b) for a, b in ((float(lde.GetSurfaceAt(s).Thickness), t_s),
                                                (float(lde.GetSurfaceAt(adj).Thickness), t_adj),
                                                (float(lde.GetSurfaceAt(back).Thickness), t_back)))
                  and _tl._solve_of(lde.GetSurfaceAt(back), "thickness") == back_solve)
            restore_error = None if ok else "thicknesses / back solve differ from the recorded values"
        except Exception as exc:  # noqa: BLE001
            restore_error = repr(exc)
    if restore_error is not None:
        raise _Fail("the paraxial back-focus probe could not restore the lens (%s)" % restore_error,
                    reason="bfl_probe_restore")
    if body_error is not None:
        raise body_error
    return {"dbfl_plus": bfl[1] - bfl[0], "dbfl_minus": bfl[-1] - bfl[0], "bfl": {0: bfl[0], 1: bfl[1],
            -1: bfl[-1]}, "t_s": t_s, "t_adj": t_adj, "t_back": t_back, "back_solve": back_solve}


# --------------------------------------------------------------------------- #
# the configurations
# --------------------------------------------------------------------------- #
def guard(pre, post):
    """The no-evaluate-window guard: ``post`` must be ``pre`` with exactly one leading CONF 1."""
    first = post[0] if post else None
    return {"row1_conf1": bool(first and first["type"] == "CONF" and first["cells"][0] == ("i", 1)),
            "n_is_pre_plus_1": len(post) == len(pre) + 1,
            "rest_is_pre": [_tl.struct_key(r) for r in post[1:]] == [_tl.struct_key(r) for r in pre],
            "last_row_kept": bool(post and pre and _tl.struct_key(post[-1]) == _tl.struct_key(pre[-1]))}


def _write_values(session, ctx, cfgs, values_by_cfg):
    for cfg in cfgs:
        for name, value in values_by_cfg[cfg].items():
            _tool(mce_config.set_config_value, session,
                  {"row": ctx["row_ids"][name], "config": cfg, "value": value},
                  "set_config_value(%s, config %d)" % (name, cfg))


def build_configs(session, ctx):
    """Add configs 2 and 3, author the per-config rows and values, make the back gap Variable (4g-4i).

    Makes NO merit evaluation until the guard has passed. ``ctx["configs_started"]`` is set
    BEFORE the first ``add_configuration`` (registration precedes mutation: the teardown resets the
    configurations iff it is set). The configuration undo is an explicit teardown step (the
    dependency-aware teardown order cannot be one LIFO entry).
    """
    system = session.system
    _mark(ctx, "4g")
    ctx["configs_started"] = True
    for _ in (2, 3):
        _tool(mce_config.add_configuration, session, {"seed_from_current": True}, "add_configuration")
    _checkpoint("4g.after_add_2")
    _mark(ctx, "4h")
    ctx["row_ids"] = {}
    for spec in ctx["rows"]:
        params = {"operand": spec["operand"], "surface": spec["surface"]}
        if spec.get("param") is not None:
            params["param"] = spec["param"]
        res = _tool(mce_config.set_config_operand, session, params, "set_config_operand(%s)" % spec["name"])
        if not isinstance(res.get("row"), int):
            raise _Fail("set_config_operand(%s) returned no row handle" % spec["name"], reason=_BAD_REASON)
        ctx["row_ids"][spec["name"]] = res["row"]
    _write_values(session, ctx, (1, 2, 3), ctx["values"])
    _mark(ctx, "4i")
    cfgs = [2, 3] + ([1] if ctx["back_solve"] == "Variable" else [])
    _tool(mce_config.set_config_variable, session, {"row": ctx["row_ids"]["back"], "config": cfgs[0]},
          "set_config_variable(config %d)" % cfgs[0])
    g = guard(ctx["pre"], _tl.mfe_snapshot(system))
    ctx["guard"] = g
    if not all(g.values()):
        raise _Fail("the merit function changed shape across the configuration build (%s); an evaluation or "
                    "an engine change touched the merit inside the no-evaluate window" % g,
                    reason="merit_shape_changed", extra={"guard": g})
    for cfg in cfgs[1:]:
        _tool(mce_config.set_config_variable, session, {"row": ctx["row_ids"]["back"], "config": cfg},
              "set_config_variable(config %d)" % cfg)
    return {"guard": g, "cfgs_variable": cfgs}


# --------------------------------------------------------------------------- #
# the merit copy
# --------------------------------------------------------------------------- #
def identify_iq_rows(system):
    """Scan the LIVE editor (after the last ``set_config_variable``): the rows to copy + a census."""
    mfe = system.MFE
    n = int(mfe.NumberOfOperands)
    rows, inf_rows, zero_w, types = [], 0, 0, []
    for i in range(1, n + 1):
        op = mfe.GetOperandAt(i)
        t = _tl._tok(op.TypeName)
        if t not in IQ_TYPES:
            continue
        w = float(op.Weight)
        if not math.isfinite(w):
            inf_rows += 1
        elif w > 0:
            rows.append(i)
            types.append(t)
        else:
            zero_w += 1
    return rows, {"n_rows_at_identification": n, "n_iq": len(rows), "n_rows_skipped_infinite": inf_rows,
                  "n_iq_zero_weight": zero_w, "copied_types": sorted(set(types))}


def _copy_row(system, src, dst):
    dst.ChangeType(src.Type)
    for col in _tl._CELL_COLS:
        try:
            sc, dc = src.GetCellAt(col), dst.GetCellAt(col)
            kind = _tl._tok(sc.DataType)
            if kind.endswith("Integer"):
                dc.IntegerValue = int(sc.IntegerValue)
            elif kind.endswith("Double"):
                dc.DoubleValue = float(sc.DoubleValue)
        except Exception:  # noqa: BLE001 -- a blank / unreadable column is not part of the row
            pass
    dst.Target = float(src.Target)
    dst.Weight = float(src.Weight)


def append_copies(system, iq_rows, *, on_base=None):
    """Append ``CONF 2`` + copies, then ``CONF 3`` + copies at the END; verify every pair.

    ``on_base(n_base)`` is called with the row count re-read immediately before the FIRST insert (the
    teardown reconciles against it). Each ``InsertNewOperandAt`` is followed by a count read-back. Returns
    ``{blocks: {2: [first, last], 3: [...]}, n_base, n_iq, mismatches: [..]}``; a count miss raises
    ``_Fail(copy_build)``. Verification (type, integer cells, doubles / target / weight at 15 digits, block
    length == ``n_iq``) happens here, before any evaluation; the CALLER refuses on ``mismatches``.
    """
    mfe = system.MFE
    conf = _resolve_enum(_oc._merit_operand_enum(system), "CONF")
    n_base = int(mfe.NumberOfOperands)
    if on_base is not None:
        on_base(n_base)                                   # registration precedes mutation

    def _insert(at):
        before = int(mfe.NumberOfOperands)
        row = mfe.InsertNewOperandAt(at)
        if int(mfe.NumberOfOperands) != before + 1:
            raise _Fail("InsertNewOperandAt(%d) did not grow the merit function" % at, reason="copy_build")
        return row

    blocks, mism = {}, []
    for cfg in (2, 3):
        n = int(mfe.NumberOfOperands)
        head = _insert(n + 1)
        head.ChangeType(conf)
        head.GetCellAt(2).IntegerValue = cfg
        start = n + 2
        for j, i in enumerate(iq_rows):
            _copy_row(system, mfe.GetOperandAt(i), _insert(start + j))
            if j == len(iq_rows) // 2:
                _checkpoint("4j.mid_block_%d" % cfg)
        blocks[cfg] = [start, start + len(iq_rows) - 1]
        h = mfe.GetOperandAt(n + 1)                       # read back fresh: which config does the block run under?
        if (_tl._tok(h.TypeName), int(h.GetCellAt(2).IntegerValue)) != ("CONF", cfg):
            mism.append({"cfg": cfg, "head_row": n + 1})
        for j, i in enumerate(iq_rows):
            a, b = _tl._row_snapshot(mfe.GetOperandAt(i)), _tl._row_snapshot(mfe.GetOperandAt(start + j))
            if not _tl.mfe_values_equal([a], [b]):
                mism.append({"cfg": cfg, "src_row": i, "copy_row": start + j, "src_type": a["type"],
                             "copy_type": b["type"]})
        if int(mfe.NumberOfOperands) - n != len(iq_rows) + 1:
            mism.append({"cfg": cfg, "block_length": int(mfe.NumberOfOperands) - n - 1,
                         "expected": len(iq_rows)})
    return {"blocks": blocks, "n_base": n_base, "n_iq": len(iq_rows), "mismatches": mism}


def block_rms(system, rows):
    """Weight-weighted RMS of ``value - target`` over ``rows`` with finite weight > 0 (``None`` if none)."""
    num = den = 0.0
    mfe = system.MFE
    for i in rows:
        op = mfe.GetOperandAt(i)
        w = float(op.Weight)
        if w > 0 and math.isfinite(w):
            num += w * (float(op.Value) - float(op.Target)) ** 2
            den += w
    return math.sqrt(num / den) if den else None


def zero_delta_check(session, ctx, iq_rows, blocks):
    """The zero-perturbation canary: at zero perturbation the copies must reproduce the nominal.

    Writes the NOMINAL into configs 2 and 3, evaluates (safe: CONF 1 is in place), compares each copy
    block's RMS with config 1's, then writes ``plus`` / ``minus`` and the seeds back and evaluates once more
    (``merit_before``). Returns ``{rel, rms, merit_before, ok, worst}``; ``ok`` is ``rel <= CANARY_REL``.
    """
    system = session.system
    nominal = {c: dict(ctx["nominal"]) for c in (2, 3)}
    changed = {c: {k: v for k, v in nominal[c].items() if v != ctx["values"][c][k]} for c in (2, 3)}
    for c in (2, 3):
        for name, value in changed[c].items():
            _tool(mce_config.set_config_value, session,
                  {"row": ctx["row_ids"][name], "config": c, "value": value},
                  "set_config_value(%s, config %d)" % (name, c))
    system.MFE.CalculateMeritFunction()
    _checkpoint("4k.evaluated")       # E6: the gate's independent canary read; configs 2/3 hold the nominal
    mfe = system.MFE
    rms = {1: block_rms(system, iq_rows)}
    rel, worst = 0.0, []
    for c in (2, 3):
        a, b = blocks[c]
        rms[c] = block_rms(system, range(a, b + 1))
        if not rms[1] or rms[c] is None:
            rel = float("inf")
        else:
            r = abs(rms[c] - rms[1]) / rms[1]
            rel = max(rel, r if math.isfinite(r) else math.inf)     # NaN must fail, not vanish in max()
        for j, i in enumerate(iq_rows):
            worst.append((abs(float(mfe.GetOperandAt(a + j).Value) - float(mfe.GetOperandAt(i).Value)), c, i,
                          a + j))
    worst.sort(reverse=True)
    for c in (2, 3):
        for name in changed[c]:
            _tool(mce_config.set_config_value, session,
                  {"row": ctx["row_ids"][name], "config": c, "value": ctx["values"][c][name]},
                  "set_config_value(%s, config %d)" % (name, c))
    merit_before = float(system.MFE.CalculateMeritFunction())
    return {"rel": rel, "rms": {str(k): v for k, v in rms.items()}, "merit_before": merit_before,
            "ok": rel <= CANARY_REL, "worst": [list(w) for w in worst[:5]]}


# --------------------------------------------------------------------------- #
# the optimizer step
# --------------------------------------------------------------------------- #
def _head(system):
    mfe = system.MFE
    n = int(mfe.NumberOfOperands)
    return n, (_tl._tok(mfe.GetOperandAt(1).TypeName) if n else None)


def run_dls(system):
    """One ``Fixed_10_Cycles`` DLS, cores explicit, ``DLS_CAP_S`` cap; returns observables, never a verdict.

    The row count and row-1 type are read before the open and after the close (Q2: a DLS run inserts
    nothing); ``trace.unchanged`` False is a ``merit_shape_changed`` for the caller. ``cycles_run`` is
    ``None``: the engine does not expose how many cycles ran.
    """
    n_before, row1_before = _head(system)
    algo = _resolve_enum(_oc._optimization_algorithm_enum(system), "DampedLeastSquares")
    cycles = _oc._resolve_cycles_member(system, DLS_CYCLES)[0]
    out = {"algorithm": "DLS", "cycles_requested": DLS_CYCLES, "cycles_run": None, "timed_out": False}
    try:
        with _oc._optimizer_session(system, algorithm_member=algo, cores=os.cpu_count() or 1,
                                    cycles_member=cycles) as (opt, cores_used):
            out["cores"] = int(opt.NumberOfCores) if cores_used is not None else None
            try:
                out["max_cores"] = int(opt.MaxCores)
            except Exception:  # noqa: BLE001 -- unreadable -> None
                out["max_cores"] = None
            out["n_variables"] = int(opt.Variables)
            out["initial_mf"] = float(opt.InitialMeritFunction)
            t0 = time.perf_counter()
            out["run_status"] = _rb.run_status_name(opt.RunAndWaitWithTimeout(DLS_CAP_S))
            if out["run_status"] == "TimedOut":
                out["timed_out"] = True
                opt.Cancel()
                opt.WaitWithTimeout(DLS_CANCEL_WAIT_S)
            out["wall_s"] = round(time.perf_counter() - t0, 3)
            out["final_mf"] = float(opt.CurrentMeritFunction)
    except OptimizeError as exc:
        raise _Fail(str(exc), reason="optimizer_unavailable") from exc
    n_after, row1_after = _head(system)
    out["trace"] = {"n_before": n_before, "row1_before": row1_before, "n_after": n_after,
                    "row1_after": row1_after, "unchanged": (n_before, row1_before) == (n_after, row1_after)}
    return out


# --------------------------------------------------------------------------- #
# the teardown
# --------------------------------------------------------------------------- #
def remove_appended(system, n_base):
    """Remove every row past ``n_base`` from the END by reconciliation (teardown step 1).

    ``k = NumberOfOperands - n_base``. ``k == 0``: nothing. ``k < 0`` or (``k >= 2`` and the head row is not
    ``CONF 2``): refused, NOTHING removed (a row of ours or theirs vanished / the block is not ours).
    ``k == 1``: the lone owned row goes WHATEVER its type (inserted but not yet typed, or typed a copy of its
    neighbour). Never reads past ``n``. Returns ``{ok, removed, n_before, n_after, refused?}``.
    """
    mfe = system.MFE
    n = int(mfe.NumberOfOperands)
    k = n - n_base
    out = {"ok": True, "removed": 0, "n_before": n, "n_after": n}
    if k == 0:
        return out
    if k < 0:
        out.update(ok=False, refused="the merit has %d rows, fewer than the build's base %d" % (n, n_base))
        return out
    head = n_base + 1
    if k >= 2:
        op = mfe.GetOperandAt(head)
        try:
            cell2 = int(op.GetCellAt(2).IntegerValue)
        except Exception:  # noqa: BLE001
            cell2 = None
        if _tl._tok(op.TypeName) != "CONF" or cell2 != 2:
            out.update(ok=False, refused="row %d is %s %s, not CONF 2" % (head, _tl._tok(op.TypeName), cell2))
            return out
    for i in range(n, head - 1, -1):
        mfe.RemoveOperandAt(i)
    out["n_after"] = int(mfe.NumberOfOperands)
    out["removed"] = n - out["n_after"]
    if out["n_after"] != n_base:
        out.update(ok=False, refused="after removal the merit has %d rows, expected %d" % (out["n_after"], n_base))
    return out


def teardown(session, ctx):
    """Undo everything in the fixed, dependency-aware teardown order and PROVE it; never raises.

    Returns ``restore{}``: ``{restored, steps_run, steps_skipped, failed_step, failed_error,
    variable_cells_changed, allowed_differences, n_differences, first_differences, mfe_reimposed_writes,
    after_run_unchanged}``. ``ctx["now_state"]`` receives the post-teardown digest.
    """
    system = session.system
    found, snap0 = ctx["found"], ctx["snap0"]
    run, skipped = [], []
    failed = {"step": None, "error": None}

    def _step(name, fn):
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 -- a teardown fault is named, never raised
            if failed["step"] is None:
                failed["step"], failed["error"] = name, str(exc) or repr(exc)
            return False, None
        run.append(name)
        return True, result

    def _check(name, ok):
        if not ok:
            raise _Fail("%s: read-back failed" % name, reason="teardown_failed")

    ok2 = ok3 = True
    if ctx.get("n_base") is not None:
        def _remove():
            r = remove_appended(system, ctx["n_base"])
            if not r["ok"]:
                raise _Fail("remove_appended refused: %s" % r.get("refused"), reason="teardown_failed")
        _step("remove_appended", _remove)               # stops nothing: no later step reads the appended rows
    if ctx.get("configs_started"):
        try:
            n_conf = _mce.number_of_configurations(system)
        except Exception as exc:  # noqa: BLE001 -- the count is unreadable: neither dependent step can run
            n_conf = None
            failed["step"] = failed["step"] or "set_current_configuration"
            failed["error"] = failed["error"] or repr(exc)
            ok2 = ok3 = False
            skipped.extend(["reset_to_single_config", "layout_removal"])
        if n_conf is not None and n_conf > 1:
            ok2, _ = _step("set_current_configuration",
                           lambda: _tool(mce_config.set_current_configuration, session, {"config": 1},
                                         "set_current_configuration"))
            if not ok2:                                 # the reset would bake the ACTIVE (perturbed) config
                ok3 = False
                skipped.extend(["reset_to_single_config", "layout_removal"])
            else:
                def _reset():
                    _tool(mce_reset.reset_to_single_config, session, {}, "reset_to_single_config")
                    _check("reset_to_single_config", _mce.number_of_configurations(system) == 1)
                ok3, _ = _step("reset_to_single_config", _reset)
                if not ok3:                             # removing a surface a live MCE row references crashes
                    skipped.append("layout_removal")

    def _leading_conf():
        mfe = system.MFE
        if (int(mfe.NumberOfOperands) and _tl._tok(mfe.GetOperandAt(1).TypeName) == "CONF"
                and (not snap0 or snap0[0]["type"] != "CONF")):
            mfe.RemoveOperandAt(1)
    try:                            # E2: only on a live single-config lens (Q2); an unreadable count is named
        single = _mce.number_of_configurations(system) == 1
    except Exception:  # noqa: BLE001
        single = False
    if single:
        _step("remove_leading_conf", _leading_conf)
    else:
        skipped.append("remove_leading_conf")

    if ctx["undo"]:
        if ok2 and ok3:
            ok_u, bad, steps = _tl.run_undo(ctx["undo"])
            run.extend("layout:%s" % s for s in steps)
            if not ok_u and failed["step"] is None:     # a failing sub-step stops the rest of step 5 only
                failed["step"], failed["error"] = "layout:%s" % bad, "undo step %s raised" % bad
    if ctx["freeze_undo"]:
        ok_f, bad, steps = _tl.run_undo(ctx["freeze_undo"])
        run.extend("freeze:%s" % s for s in steps)
        if not ok_f and failed["step"] is None:
            failed["step"], failed["error"] = "freeze:%s" % bad, "undo step %s raised" % bad
    _step("remark_variables", lambda: _tl.remark_variables(system, found))
    reimp = {"n_writes": 0}

    def _reimpose():
        r = _tl.reimpose_mfe(system, snap0)
        reimp["n_writes"] = r["n_writes"]
        if not r["restored"]:
            raise _Fail("the merit function could not be re-imposed (%s)" % r.get("reason", "values differ"),
                        reason="teardown_failed")
    _step("reimpose_mfe", _reimpose)

    proof = None
    try:
        now = _tl.state_with_mce(system)
        ctx["now_state"] = now
        # The Variable cells may differ ONLY when the DLS reached a Completed / TimedOut status
        dofs = (ctx.get("optimizer") or {}).get("run_status") in ("Completed", "TimedOut")
        row = ctx["row"]            # the cells the recipe froze, in ORIGINAL numbering (never ctx["frozen"])
        pinned = {(row[k], "thickness") for k in ("surface", "surface2") if row.get(k) is not None}
        proof = _tl.diff_allowed(found, now, allow_dofs=dofs, pinned=pinned)
        run.append("proof")
    except Exception as exc:  # noqa: BLE001
        if failed["step"] is None:
            failed["step"], failed["error"] = "proof", str(exc) or repr(exc)
    restored = bool(proof and proof["pass"] and failed["step"] is None)
    return {"restored": restored, "steps_run": run, "steps_skipped": skipped, "failed_step": failed["step"],
            "failed_error": failed["error"],
            "variable_cells_changed": proof["variable_cells_changed"] if proof else 0,
            "allowed_differences": proof["lde_allowed"] if proof else 0,
            "n_differences": proof["n_differences"] if proof else None,
            "first_differences": _tl._jsonable(proof["first_differences"]) if proof else [],
            "mfe_reimposed_writes": reimp["n_writes"], "after_run_unchanged": None}


# --------------------------------------------------------------------------- #
# the build
# --------------------------------------------------------------------------- #
def _build(session, ctx, row):
    system = session.system
    lde = system.LDE
    found = ctx["found"]
    n0 = int(lde.NumberOfSurfaces)
    back0 = n0 - 2
    ctx["back_solve"] = found.get("s%d.thickness.solve" % back0)
    t, s, s2 = row["type"], row["surface"], row["surface2"]
    plus, minus = row["plus"], row["minus"]
    frozen = []
    if t == "TTHI":
        _mark(ctx, "4a")
        bfl = paraxial_bfl_shift(session, found, s, s2, plus, minus, ctx["undo"])
        ctx["bfl"] = bfl
        _mark(ctx, "4e")
        for i in (s, s2):
            prior = _tl.freeze_thickness(system, i, ctx["freeze_undo"])
            frozen.append({"surface": i, "cell": "thickness", "prior_solve": prior})
        t_s, t_adj, t_back = bfl["t_s"], bfl["t_adj"], bfl["t_back"]
        ctx["rows"] = [{"name": "s", "operand": "THIC", "surface": s},
                       {"name": "adj", "operand": "THIC", "surface": s2},
                       {"name": "back", "operand": "THIC", "surface": back0}]
        ctx["nominal"] = {"s": t_s, "adj": t_adj, "back": t_back}
        ctx["values"] = {1: dict(ctx["nominal"]),
                         2: {"s": t_s + plus, "adj": t_adj - plus, "back": t_back + bfl["dbfl_plus"]},
                         3: {"s": t_s + minus, "adj": t_adj - minus, "back": t_back + bfl["dbfl_minus"]}}
        ctx["seeds"] = {"back_gap": [t_back, t_back + bfl["dbfl_plus"], t_back + bfl["dbfl_minus"]],
                        "dbfl": [0.0, bfl["dbfl_plus"], bfl["dbfl_minus"]]}
    else:
        _mark(ctx, "4b")
        g_img0, g_next0 = _tl.global_matrix(system, n0 - 1), _tl.global_matrix(system, s + 1)
        lay = _tl.insert_surface_pair(session, s, ctx["undo"])
        ctx["layout"] = lay
        frozen = [dict(f, surface=s) for f in lay["frozen"]]      # disclosed in ORIGINAL numbering (the layout is gone)
        _mark(ctx, "4c")
        ctx["zero_tilt"] = _tl.zero_tilt_check(system, {lay["n_surfaces"] - 1: g_img0, s + 3: g_next0})
        _mark(ctx, "4d")
        ctx["neutralised"] = _tl.neutralise_boundary(system, {lay["cb1"], lay["s"], lay["cb2"]})
        back2 = lay["n_surfaces"] - 2
        t_back = float(lde.GetSurfaceAt(back2).Thickness)
        ctx["rows"] = [{"name": "pram", "operand": "PRAM", "surface": lay["cb1"],
                        "param": 3 if t == "TSTX" else 4},
                       {"name": "back", "operand": "THIC", "surface": back2}]
        ctx["nominal"] = {"pram": 0.0, "back": t_back}
        ctx["values"] = {1: dict(ctx["nominal"]), 2: {"pram": plus, "back": t_back},
                         3: {"pram": minus, "back": t_back}}
        ctx["seeds"] = {"back_gap": [t_back] * 3, "dbfl": None}
    ctx["frozen"] = frozen
    _mark(ctx, "4f")
    ctx["pre"] = _tl.mfe_snapshot(system)
    build_configs(session, ctx)
    _mark(ctx, "4j")
    iq_rows, census = identify_iq_rows(system)
    ctx["census"] = census
    if not iq_rows:
        raise _Fail("the merit function holds no copyable image-quality row (finite weight > 0)",
                    reason="no_iq_rows")

    def _on_base(n):
        ctx["n_base"] = n
    app = append_copies(system, iq_rows, on_base=_on_base)
    if app["mismatches"]:
        raise _Fail("a copied merit row differs from its source at 15 significant digits, or a block count "
                    "differs from n_iq (%s)" % (app["mismatches"][:3],), reason="copy_mismatch",
                    extra={"mismatches": app["mismatches"][:10]})
    _mark(ctx, "4k")
    z = zero_delta_check(session, ctx, iq_rows, app["blocks"])
    ctx["zero_delta"] = z
    if not z["ok"]:
        raise _Fail("the perturbed copies do not reproduce the nominal at zero perturbation (rel %.3g > %.1e); "
                    "nothing was optimized" % (z["rel"], CANARY_REL), reason="zero_delta_canary",
                    extra={"zero_delta": _tl._jsonable(z)})
    _mark(ctx, "4l")
    ctx["dls_attempted"] = True
    dls = run_dls(system)
    ctx["optimizer"] = dls
    if not dls["trace"]["unchanged"]:
        raise _Fail("the optimizer run changed the merit function's row count or first row (%s)" % dls["trace"],
                    reason="merit_shape_changed")
    if dls.get("run_status") not in ("Completed", "TimedOut"):
        raise _Fail("the optimizer returned status %r" % (dls.get("run_status"),),
                    reason="optimizer_%s" % dls.get("run_status"))


# --------------------------------------------------------------------------- #
# the envelope
# --------------------------------------------------------------------------- #
def _before_block(before_env):
    rs = (before_env or {}).get("run_summary") or {}
    hs = (before_env or {}).get("hotspots") or []
    top = hs[0] if hs else {}
    return {"nominal": (before_env or {}).get("nominal_criterion"),
            "rss_change": (before_env or {}).get("rss_estimated_change"),
            "rss_estimated_criterion": (before_env or {}).get("rss_estimated_criterion"),
            "top_hotspot": {"key": top.get("key"), "share": top.get("share")},
            "budget_rule_id": rs.get("budget_rule_id"), "budget_digest": rs.get("budget_digest"),
            "topology_digest": rs.get("topology_digest")}


def _after_block(after_env, key):
    rs = after_env.get("run_summary") or {}
    share = None
    for h in after_env.get("hotspots") or ():
        if h.get("key") == key:
            share = h.get("share")
    return {"nominal": after_env.get("nominal_criterion"), "rss_change": after_env.get("rss_estimated_change"),
            "rss_estimated_criterion": after_env.get("rss_estimated_criterion"),
            "share_of_perturbed_hotspot": share, "budget_rule_id": rs.get("budget_rule_id"),
            "budget_digest": rs.get("budget_digest"), "topology_digest": rs.get("topology_digest")}


def _desens_block(ctx):
    """``desensitize{}`` populated as far as the build got."""
    row = ctx.get("row") or {}
    d = {"stage": ctx.get("stage")}
    if row:
        d["perturbation"] = {"type": row["type"], "surface": row["surface"], "surface2": row["surface2"],
                             "hotspot_key": row["hotspot_key"], "share": row["share"], "plus": row["plus"],
                             "minus": row["minus"], "units": row.get("units")}
    d["skipped_rows"] = ctx.get("skipped", [])
    if "seeds" in ctx:
        d["configs"] = 3
        d["seeds"] = _tl._jsonable(ctx["seeds"])
    census = ctx.get("census") or {}
    d["copied_types"] = census.get("copied_types", [])
    d["n_rows_copied"] = census.get("n_iq", 0)
    d["n_rows_skipped_infinite"] = census.get("n_rows_skipped_infinite", 0)
    d["n_boundary_rows_neutralised"] = len(ctx.get("neutralised") or [])
    d["frozen_cells"] = ctx.get("frozen", [])
    if "guard" in ctx:
        d["guard"] = {k: ctx["guard"][k] for k in ("row1_conf1", "n_is_pre_plus_1", "rest_is_pre")}
    z = ctx.get("zero_delta")
    if z:
        d["zero_delta_rel"], d["zero_delta_threshold"] = _tl._jsonable(z["rel"]), CANARY_REL
        d["recipe_merit"] = None
    opt = ctx.get("optimizer")
    if opt:
        d["optimizer"] = _tl._jsonable({k: v for k, v in opt.items() if k != "trace"})
        if z:
            before, after = z["merit_before"], opt.get("final_mf")
            d["recipe_merit"] = _tl._jsonable({"before": before, "after": after,
                                               "rel_change": ((after - before) / before) if before else None,
                                               "note": "perturbed multi-config merit; not the judge"})
    d["before"] = ctx.get("before_block")
    d["after"] = ctx.get("after_block")
    d["tolerancing_opens"] = ctx.get("tolerancing_opens", 0)
    return d


def _failure(ctx, fail):
    """An ``ok:false`` envelope once the build started. Teardown failure outranks the build reason."""
    restore = ctx["restore"]
    extra = dict(fail.extra) if fail else {}
    if not restore["restored"]:
        family = "tolerancing_run"
        reason = "teardown_failed" if restore["failed_step"] else "restore_unproven"
        message = ("the recipe could not prove the lens restored (%s); reload your saved candidate"
                   % (restore["failed_error"] or "differences remain"))
        if fail:
            extra["stopped_by"] = {"reason": fail.reason, "message": fail.message}
    else:
        family, reason, message = fail.family, fail.reason, fail.message
    env = _ac.error_envelope(TOOL, family, message, reason=reason, mode="desensitize",
                             restored=restore["restored"], restore=restore, desensitize=_desens_block(ctx),
                             prescription_changed=restore["variable_cells_changed"] > 0,
                             state_mutated=not restore["restored"], **extra)
    if ctx.get("before_env") is not None:
        env["before"] = ctx["before_block"]
    if not restore["restored"] or ctx.get("dls_attempted"):
        env["recovery"] = RECOVERY
    return env


def _handler_params(params, output_file=None):
    p = {"mode": "sensitivity", "budget": copy.deepcopy(dict.get(params, "budget")), "compensator": "paraxial"}
    for key in ("criterion", "sampling", "trials", "full"):      # trials / full: echoed-ignored by the handler
        if dict.__contains__(params, key):
            p[key] = dict.get(params, key)
    if output_file is not None:
        p["output_file"] = output_file
    return p


def _call_handler(handler, session, p):
    try:
        env = handler(session, p)
    except Exception as exc:  # noqa: BLE001 -- the handler never raises; a double might
        return _refusal("tolerancing_run", "handler_raised", "the sensitivity run raised (%r)" % (exc,))
    return env if isinstance(env, dict) else _refusal("tolerancing_run", "handler_raised",
                                                      "the sensitivity run returned %r" % (env,))


def run(session, params, tolerance_handler):
    """``tolerance(mode="desensitize")``: the whole recipe. Never raises."""
    try:
        return _run(session, params, tolerance_handler)
    except Exception as exc:  # noqa: BLE001 -- the dispatch net
        return _refusal("tolerancing_run", "internal_error",
                        "the desensitize recipe hit an unexpected fault (%r); if it began building, reload "
                        "your saved candidate with load_design" % (exc,))


def _run(session, params, handler):
    system = session.system
    err = validate(params, system)
    if err is not None:
        return err
    try:                                                                      # step 1
        # the flag is only here: past this check the lens has variables, so a later missing fingerprint is a fault
        found = _tl.state_with_mce(system, allow_no_variables=True)
        if not found.get("fingerprint"):
            return _refusal("tolerancing_run", "no_variables",
                            "the lens has no Variable cell; desensitize re-optimizes your Variable cells against "
                            "perturbed copies, so mark the cells it may move (set_variable) and run it again; "
                            "nothing was opened or changed")
        snap0 = _tl.mfe_snapshot(system)
    except _lens_state.StateUnreadable as exc:
        return _refusal("tolerancing_run", "state_unreadable",
                        "the lens state could not be read (%s); nothing was changed" % exc)
    ctx = {"found": found, "snap0": snap0, "undo": [], "freeze_undo": [], "n_base": None,
           "configs_started": False, "dls_attempted": False, "stage": "before", "tolerancing_opens": 0}
    before = _call_handler(handler, session, _handler_params(params))         # step 2
    ctx["tolerancing_opens"] = 1
    if not before.get("ok"):
        out = dict(before)
        out["mode"] = "desensitize"
        out["desensitize"] = {"stage": "before"}
        return out
    if before.get("hotspots_withheld") is not None:
        return _refusal("tolerancing_run", "hotspots_withheld",
                        "the BEFORE run withheld its hotspot ranking (%r); the recipe has no perturbation to "
                        "model" % (before.get("hotspots_withheld"),), value=before.get("hotspots_withheld"))
    try:
        mid = _tl.diff_allowed(found, _tl.state_with_mce(system), allow_dofs=False)
    except _lens_state.StateUnreadable as exc:
        return _refusal("tolerancing_run", "state_unreadable", "the lens state could not be re-read (%s)" % exc)
    if not mid["pass"] or before.get("state_mutated"):
        return _refusal("tolerancing_run", "before_run_mutated",
                        "the BEFORE sensitivity run changed the lens (%s); refusing to build on it; reload your "
                        "saved candidate with load_design" % (mid["first_differences"][:3],),
                        state_mutated=True, recovery=RECOVERY)
    ctx["before_env"], ctx["before_block"] = before, _before_block(before)
    hs = before.get("hotspots") or []
    row, skipped = select_row(hs[0] if hs else None, before.get("sensitivity"), system, found)  # step 3
    ctx["skipped"] = skipped
    if row is None:
        return _refusal("tolerancing_run", "no_modelable_row",
                        "no row of the top hotspot is modelable (TSTX / TSTY / TTHI with their preconditions); "
                        "nothing was changed", skipped_rows=skipped, before=ctx["before_block"])
    ctx["row"] = row
    fail = None
    try:                                                                      # step 4
        _build(session, ctx, row)
    except Exception as exc:  # noqa: BLE001 -- named after the teardown has run
        fail = _as_fail(exc)
    finally:                                                                  # step 5
        ctx["restore"] = teardown(session, ctx)
    restore = ctx["restore"]
    if fail is not None or not restore["restored"]:                           # step 6
        return _failure(ctx, fail)
    out_file = dict.get(params, "output_file")                                # step 7
    after = _call_handler(handler, session, _handler_params(params, out_file))
    ctx["tolerancing_opens"] = 2
    ctx["after_block"] = _after_block(after, row["hotspot_key"])
    changed = restore["variable_cells_changed"]
    try:                                                                      # step 7b
        post = _tl.diff_allowed(ctx["now_state"], _tl.state_with_mce(system), allow_dofs=False)
        unchanged = post["pass"]
    except _lens_state.StateUnreadable:
        unchanged = False
    restore["after_run_unchanged"] = bool(unchanged)
    if not after.get("ok"):
        out = dict(after)
        out.update(mode="desensitize", restored=restore["restored"], restore=restore, desensitize=_desens_block(ctx),
                   prescription_changed=changed > 0, recovery=RECOVERY)
        if not unchanged:
            out["state_mutated"] = True
        return out
    if not (unchanged and after.get("state_mutated") is False and after.get("tde_cleared") is True):
        return _ac.error_envelope(
            TOOL, "tolerancing_run",
            "the AFTER sensitivity run changed the restored lens or left the Tolerance Data Editor dirty; "
            "reload your saved candidate with load_design", reason="after_run_mutated", mode="desensitize",
            restored=True, restore=restore, desensitize=_desens_block(ctx), prescription_changed=changed > 0,
            state_mutated=True, tde_cleared=bool(before.get("tde_cleared") and after.get("tde_cleared")),
            recovery=RECOVERY)
    ctx["stage"] = "done"
    env = dict(after)                                                         # step 8
    warnings = list(after.get("warnings") or [])
    if changed > 0:
        label = "%s %s%s" % (row["type"], row["surface"], "" if row["surface2"] is None else " %s" % row["surface2"])
        warnings.insert(0, "desensitize changed %d Variable cells against perturbed copies of `%s`; judge it "
                           "with the monte_carlo confirm under compensator='dls' before keeping it; reload "
                           "your saved candidate to revert." % (changed, label))
    else:
        warnings.insert(0, "optimizer_no_motion: the DLS moved no Variable cell, so the lens is unchanged and "
                           "the AFTER run equals BEFORE")
    warnings.extend("before run: %s" % w for w in (before.get("warnings") or []))
    env.update(mode="desensitize", control_arm=True, prescription_changed=changed > 0, state_mutated=False,
               tde_cleared=bool(before.get("tde_cleared", True) and after.get("tde_cleared", True)),
               desensitize=_desens_block(ctx), restore=restore, warnings=warnings)
    return env
