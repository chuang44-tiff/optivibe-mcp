"""tools/_tolerance_layout.py -- the S2b layout / undo / proof substrate for ``tolerance``. NOT dispatchable.

Private leaf shared by the two S2b features and nothing else:

- ``tolerance(compensator="dls_cb_tilt", cb_tilt=...)`` (``tolerance_run``) builds a temporary
  coordinate-break pair around a lens group, authors CPAR rows on it, and tears it down;
- ``tolerance(mode="desensitize")`` (``_tolerance_desensitize``) builds a one-surface CB pair for a
  TSTX / TSTY perturbation, then re-imposes the merit function and proves the lens restored.

It holds the pieces both need and neither owns: the 15-significant-digit compare, the merit-row
snapshot and re-impose, the allowed-difference proof (``diff_allowed``), the LIFO undo runner, the
two CB layout builders and the zero-tilt check that guards them.

TWO RULES THIS FILE EXISTS TO KEEP:

1. **Every ZOS double write rounds to 15 significant digits** (P-M2: an LDE thickness, an MFE
   weight / target / cell and an MCE cell all do). An engine round trip therefore returns a value
   one rounding away from the one written, so "restored" is ``g15``-equality for merit values and
   ``abs(a - b) <= 1e-12 * max(1, abs(a))`` for LDE doubles, never bit equality.
2. **Registration precedes mutation.** An undo is pushed BEFORE the engine call that mutates, and
   each undo reads the live state and acts only on what exists, so a primitive that mutated and
   then failed its read-back is still undone, and one that never mutated is not "undone" into a
   deleted real surface.
"""
import math
import re

from . import _asphere_cells as _asph
from . import _layout_geometry as _geom
from . import _lens_state
from . import _mce_cells as _mce
from . import _solve_cells as _sc
from . import cb_surface

# The merit-function row cells a snapshot reads (the engine's parameter columns).
_CELL_COLS = range(2, 10)

# LDE doubles compare at 1e-12 relative (floor 1): 2500x the measured one-ulp / 15th-digit
# residue (4e-16), and far below any optical meaning.
LDE_REL_TOL = 1e-12
# The zero-tilt layout check: the measured round trip is 3e-14; refuse past 1e-9.
ZERO_TILT_TOL = 1e-9

# LDE value cell -> its solve token (the ``_solve_cells.CELL_TOKENS`` spelling ``read_state`` uses).
_LDE_CELLS = {"Radius": "radius", "Thickness": "thickness", "Conic": "conic",
              "SemiDiameter": "semi_diameter"}
_LDE_KEY_RE = re.compile(r"^s(\d+)\.(Radius|Thickness|Conic|SemiDiameter)$")
_SOLVE_KEY_RE = re.compile(r"^s(\d+)\.(radius|thickness|conic|semi_diameter|material)\.solve$")
_SOLVE_FIELD_RE = re.compile(r"^s(\d+)\.(radius|thickness|conic|semi_diameter|material)\.(?!solve$)(.+)$")
_ASPHERE_KEY_RE = re.compile(r"^s(\d+)\.asphere\.(.+)$")
_MFE_ROW_RE = re.compile(r"^m(\d+)\.(type|target|weight|params)$")
# The property route to a surface's solve cell (the probe-proven one): token -> row attribute.
_CELL_TOKENS = ("radius", "thickness", "conic")


def _cell_of(row, token):
    """The typed solve cell of ``row`` for ``token`` (explicit attribute reads, no dynamic dispatch)."""
    if token == "radius":
        return row.RadiusCell
    if token == "thickness":
        return row.ThicknessCell
    return row.ConicCell
_BOUNDARY_PREFIXES = ("MN", "MX")

_ASPHERE_COL = {name: col for name, col, _h, _k in _asph.ASPHERE_PARAMS}


class LayoutError(Exception):
    """A layout / undo / proof failure carrying the envelope ``family`` and a machine ``reason``."""

    def __init__(self, message, *, family="tolerancing_run", reason=None, value=None):
        super().__init__(message)
        self.family = family
        self.reason = reason
        # a measured number the refusal is about (the zero-tilt residual); None otherwise
        self.value = value


# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #
def g15(x):
    """``x`` rounded to 15 significant digits (what every ZOS double write stores). Non-floats pass."""
    return float("%.15g" % x) if isinstance(x, float) and math.isfinite(x) else x


def _same(a, b):
    """Equal, or both NaN; sequences member by member."""
    if isinstance(a, float) and isinstance(b, float):
        return (a != a and b != b) or a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _same_g15(a, b):
    """``_same`` after rounding both sides to 15 significant digits."""
    if isinstance(a, float) and isinstance(b, float):
        return _same(g15(a), g15(b))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same_g15(x, y) for x, y in zip(a, b))
    return a == b


def _close_lde(a, b):
    """The LDE-double compare: equal, or within ``LDE_REL_TOL`` relative (floor 1)."""
    if _same(a, b):
        return True
    return (isinstance(a, float) and isinstance(b, float) and math.isfinite(a)
            and math.isfinite(b) and abs(a - b) <= LDE_REL_TOL * max(1.0, abs(a)))


def _tok(value):
    """An engine string through the base slot: the repo's one reader, ``_lens_state._tok``."""
    return _lens_state._tok(value)


# --------------------------------------------------------------------------- #
# the undo stack
# --------------------------------------------------------------------------- #
def run_undo(undo):
    """Run ``undo`` (a list of ``(step_name, fn)``) LIFO, popping each entry.

    Returns ``(ok, failed_step, steps_run)``. The FIRST failing step stops the run (the entries
    below it stay on the list, unrun): removing a surface a surviving row still references is
    the engine-crash class, so the caller names ``failed_step`` and does not continue blind.
    """
    steps_run = []
    while undo:
        name, fn = undo.pop()
        try:
            fn()
        except Exception:  # noqa: BLE001 -- any undo fault stops the run and is named
            return False, name, steps_run
        steps_run.append(name)
    return True, None, steps_run


# --------------------------------------------------------------------------- #
# merit-function snapshot / re-impose
# --------------------------------------------------------------------------- #
def _row_snapshot(op):
    cells = []
    for col in _CELL_COLS:
        try:
            cell = op.GetCellAt(col)
            kind = _tok(cell.DataType)
            if kind.endswith("Integer"):
                cells.append(("i", int(cell.IntegerValue)))
            elif kind.endswith("Double"):
                cells.append(("d", float(cell.DoubleValue)))
            else:
                cells.append((None, None))
        except Exception:  # noqa: BLE001 -- an unreadable cell is DATA ('x'), not a crash
            cells.append(("x", None))
    return {"type": _tok(op.TypeName), "cells": cells, "target": float(op.Target),
            "weight": float(op.Weight)}


def mfe_snapshot(system):
    """Every merit row as ``{type, cells: [(kind, value)] (cols 2..9), target, weight}``.

    ``kind`` is ``"i"`` (Integer), ``"d"`` (Double), ``None`` (any other / blank) or ``"x"`` (the
    cell threw). This is the form the re-impose writes back from and the guard compares.
    """
    mfe = system.MFE
    return [_row_snapshot(mfe.GetOperandAt(i)) for i in range(1, int(mfe.NumberOfOperands) + 1)]


def struct_key(row):
    """The structure of a snapshot row: type + integer cell values + cell kinds (no doubles)."""
    return (row["type"], tuple((kind, value if kind == "i" else None) for kind, value in row["cells"]))


def mfe_struct_equal(a, b):
    """True iff two snapshots have the same count and every row has the same ``struct_key``."""
    return len(a) == len(b) and all(struct_key(x) == struct_key(y) for x, y in zip(a, b))


def _row_values_equal(x, y):
    return (_same_g15(x["target"], y["target"]) and _same_g15(x["weight"], y["weight"])
            and all(_same_g15(cx[1], cy[1]) if cx[0] == "d" else cx[1] == cy[1]
                    for cx, cy in zip(x["cells"], y["cells"])))


def mfe_values_equal(a, b):
    """True iff two structurally-equal snapshots agree on every double / target / weight at 15 digits."""
    return mfe_struct_equal(a, b) and all(_row_values_equal(x, y) for x, y in zip(a, b))


def reimpose_mfe(system, snap):
    """Write ``snap``'s targets, weights and Double cells back; PROVE it by a fresh read.

    If the live merit's STRUCTURE (count, types, integer cells, cell kinds) differs from ``snap`` it
    writes NOTHING and returns ``restored: False`` (a structural mismatch must never be papered over
    by writing values onto the wrong rows). Otherwise it writes only the values that differ at 15
    significant digits, so a merit that already equals ``snap`` costs zero writes (Q4: the boundary
    block returns equal on its own), then re-reads and compares.

    Returns ``{restored, n_writes, writes (first 40), reason?}``.
    """
    cur = mfe_snapshot(system)
    if not mfe_struct_equal(cur, snap):
        return {"restored": False, "n_writes": 0, "writes": [], "reason": "structure differs",
                "n": [len(cur), len(snap)]}
    writes = []
    mfe = system.MFE
    for i, (c, s) in enumerate(zip(cur, snap), start=1):
        op = mfe.GetOperandAt(i)
        if not _same_g15(c["target"], s["target"]) and not math.isnan(s["target"]):
            op.Target = s["target"]
            writes.append({"row": i, "what": "target", "from": c["target"], "to": s["target"]})
        if not _same_g15(c["weight"], s["weight"]) and not math.isnan(s["weight"]):
            op.Weight = s["weight"]
            writes.append({"row": i, "what": "weight", "type": c["type"], "from": c["weight"],
                           "to": s["weight"]})
        for j, (cc, sc) in enumerate(zip(c["cells"], s["cells"])):
            if sc[0] == "d" and not _same_g15(cc[1], sc[1]) and not math.isnan(sc[1]):
                op.GetCellAt(j + 2).DoubleValue = sc[1]
                writes.append({"row": i, "what": "cell%d" % (j + 2), "from": cc[1], "to": sc[1]})
    proven = mfe_values_equal(mfe_snapshot(system), snap)
    return {"restored": proven, "n_writes": len(writes), "writes": writes[:40]}


def neutralise_boundary(system, surfaces):
    """Weight 0 on every ``MN*`` / ``MX*`` boundary row whose Surf cell is in ``surfaces`` (repair 1).

    A surface insertion makes the engine regenerate boundary rows for the new surfaces (Q5: 6 rows
    on the 10x); on the temporary CB / dummy surfaces they would fight the layout. Returns the rows
    changed, ``[{row, type, surf, weight}]`` with the PRIOR weight; the prior weights are restored
    from the pre-build snapshot by ``reimpose_mfe`` (never from a literal 1.0).
    """
    hits = []
    mfe = system.MFE
    wanted = set(surfaces)
    for i in range(1, int(mfe.NumberOfOperands) + 1):
        op = mfe.GetOperandAt(i)
        type_name = _tok(op.TypeName)
        if not type_name.startswith(_BOUNDARY_PREFIXES):
            continue
        try:
            surf = int(op.GetCellAt(2).IntegerValue)
        except Exception:  # noqa: BLE001 -- a row with no readable Surf cell is not ours
            continue
        weight = float(op.Weight)
        if surf in wanted and weight != 0:
            hits.append({"row": i, "type": type_name, "surf": surf, "weight": weight})
            op.Weight = 0.0
    return hits


# --------------------------------------------------------------------------- #
# the digest and the allowed-difference proof
# --------------------------------------------------------------------------- #
def state_with_mce(system, *, allow_no_variables=False):
    """``_lens_state.read_state`` + the MCE tuple + the stop index: the restore digest.

    ``mce`` = ``(NumberOfConfigurations, NumberOfOperands, CurrentConfiguration, row1_type,
    row1_params)`` where ``row1_params`` is ``(Param1, Param2, Param3)`` read through
    ``_mce_cells.read_param`` (the floor row's identity: P-M3 measured that a one-config reset can
    SILENTLY replace an authored lone row, with the count unchanged). Raises ``StateUnreadable``
    instead of returning a digest that proves nothing. ``allow_no_variables`` is ``read_state``'s.
    """
    state = _lens_state.read_state(system, **({"allow_no_variables": True} if allow_no_variables else {}))
    try:
        mce = system.MCE
        n_conf, n_ops, current = (int(mce.NumberOfConfigurations), int(mce.NumberOfOperands),
                                  int(mce.CurrentConfiguration))
        row1_type = row1_params = None
        if n_ops >= 1:
            op = mce.GetOperandAt(1)
            row1_type = _tok(op.Type).rsplit(".", 1)[-1]
            row1_params = tuple(_mce.read_param(op, k) for k in (1, 2, 3))
        state["mce"] = (n_conf, n_ops, current, row1_type, row1_params)
        state["stop"] = int(system.LDE.StopSurface)
    except _lens_state.StateUnreadable:
        raise
    except Exception as exc:  # noqa: BLE001 -- any read fault: the digest proves nothing
        raise _lens_state.StateUnreadable("the MCE / stop state could not be read (%r)" % (exc,)) from exc
    return state


def _params_compare(a, b):
    """(struct_equal, values_equal) for two ``m<i>.params`` tuples ``(header, col, kind, value)``."""
    if not (isinstance(a, tuple) and isinstance(b, tuple)) or len(a) != len(b):
        return False, False
    struct = values = True
    for (ha, ca, ka, va), (hb, cb, kb, vb) in zip(a, b):
        if (ha, ca, ka) != (hb, cb, kb) or (ka == "int" and va != vb):
            struct = False
        if ka == "double" and not _same_g15(va, vb):
            values = False
        elif ka not in ("double", "int") and va != vb:
            values = False
    return struct, values


def _is_free(token, solve):
    """Whether a cell's VALUE may move under an optimizer run, given its ``found`` solve type."""
    if token == "semi_diameter":
        return solve == "Automatic" or _sc.is_driving(solve)
    return solve != "Fixed"


def _jsonable(value):
    """``value`` with every dict key a string and every tuple a list: safe for ``json.dumps``.

    The digests hold dicts keyed by tuples (the optimizer-variable fingerprint) and tuples of
    tuples (merit parameter maps); a difference carrying one must still serialise into an envelope.
    """
    if isinstance(value, dict):
        return {(k if isinstance(k, str) else repr(k)): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


def _covered_by_digest(dof, found):
    """Whether a moved fingerprint DOF is already counted under an LDE / even-asphere digest key."""
    kind = dof[0] if isinstance(dof, tuple) and dof else None
    if kind == "lde":
        return True
    if kind == "asphere" and len(dof) == 3:
        return any(col == dof[2] and "s%s.asphere.%s" % (dof[1], name) in found
                   for name, col in _ASPHERE_COL.items())
    return False


def diff_allowed(found, now, *, allow_dofs, pinned=frozenset()):
    """Compare two ``state_with_mce`` digests; the ONLY proof that a lens was restored.

    Pure (no engine). Exact: surface count, types, materials, catalogs, every solve TYPE, the stop,
    the MCE tuple, waves / fields / aperture, the optimizer-variable key set. LDE doubles within
    ``LDE_REL_TOL``. MFE count / types / integer cells exact; MFE doubles, targets, weights equal at
    15 significant digits. Any key this function does not know compares exactly (fail closed).

    ``allow_dofs=True`` (the DLS ran): the VALUE (never the solve type) of an LDE cell whose
    ``found`` solve is not Fixed, a driving-solve numeric field, an Automatic semi-diameter, an
    asphere cell with a variable in ``found`` and a fingerprint value may differ. Never a Fixed
    cell, a solve type, the MFE, the MCE tuple or the stop. A ``pinned`` ``(surface, token)`` cell (frozen
    for the call) compares as Fixed.

    Returns ``{lde, lde_allowed, variable_cells_changed, mfe, stop_equal, mce_equal, n_differences,
    first_differences, pass}``. ``lde`` lists the DISALLOWED non-merit differences; ``lde_allowed``
    counts every permitted difference (it INCLUDES driving-solve outputs and Automatic
    semi-diameters); ``variable_cells_changed`` counts only cells whose ``found`` solve was Variable
    (or an asphere cell Variable in ``found``) and whose value moved, WHETHER OR NOT the move was allowed.
    """
    diffs = []              # [(key, found_value, now_value)] -- every DISALLOWED non-merit difference
    allowed = changed = 0
    mfe = {"struct_equal": True, "equal": True, "n": [found.get("mfe.n"), now.get("mfe.n")],
           "n_struct_diff": 0, "n_value_diff": 0}
    mfe_diffs = []
    fp_found = found.get("fingerprint") if isinstance(found.get("fingerprint"), dict) else {}
    keys = sorted(set(found) | set(now), key=str)
    for key in keys:
        a, b = found.get(key, "<absent>"), now.get(key, "<absent>")
        if key in ("mce", "stop"):
            continue
        row = _MFE_ROW_RE.match(key)
        if key == "mfe.n" or row:
            if a == "<absent>" or b == "<absent>":
                mfe["struct_equal"] = False
                mfe["n_struct_diff"] += 1
                mfe_diffs.append((key, a, b))
                continue
            if key == "mfe.n" or row.group(2) == "type":
                bad = a != b
                if bad:
                    mfe["struct_equal"] = False
                    mfe["n_struct_diff"] += 1
            elif row.group(2) == "params":
                struct, values = _params_compare(a, b)
                bad = not (struct and values)
                if not struct:
                    mfe["struct_equal"] = False
                    mfe["n_struct_diff"] += 1
                elif not values:
                    mfe["n_value_diff"] += 1
            else:
                bad = not _same_g15(a, b)
                if bad:
                    mfe["n_value_diff"] += 1
            if bad:
                mfe_diffs.append((key, a, b))
            continue
        if key == "fingerprint":
            if not (isinstance(a, dict) and isinstance(b, dict)) or set(a) != set(b):
                diffs.append((key, a, b))
                continue
            for dof in sorted(a, key=str):
                if _close_lde(a[dof], b[dof]):
                    continue
                if not _covered_by_digest(dof, found):
                    changed += 1  # a fingerprint-only DOF (odd asphere, GRIN, MCE): no digest key counts it (E3)
                if allow_dofs:
                    continue
                diffs.append(("fingerprint %r" % (dof,), a[dof], b[dof]))
            continue
        lde = _LDE_KEY_RE.match(key)
        if lde:
            if _close_lde(a, b):
                continue
            token = _LDE_CELLS[lde.group(2)]
            solve = found.get("s%s.%s.solve" % (lde.group(1), token))
            changed += solve == "Variable"        # OBSERVED movement, whatever the permission (E3)
            if (allow_dofs and isinstance(solve, str) and _is_free(token, solve)
                    and (int(lde.group(1)), token) not in pinned):
                allowed += 1
            else:
                diffs.append((key, a, b))
            continue
        field = _SOLVE_FIELD_RE.match(key)
        if field and not _SOLVE_KEY_RE.match(key):
            av = a[0] if isinstance(a, tuple) and len(a) == 2 else a
            bv = b[0] if isinstance(b, tuple) and len(b) == 2 else b
            if _close_lde(av, bv) and (not isinstance(a, tuple) or not isinstance(b, tuple)
                                       or a[1:] == b[1:]):
                continue
            solve = found.get("s%s.%s.solve" % (field.group(1), field.group(2)))
            if (allow_dofs and isinstance(solve, str) and _sc.is_driving(solve)
                    and isinstance(a, tuple) and isinstance(b, tuple) and a[1:] == b[1:]):
                allowed += 1
            else:
                diffs.append((key, a, b))
            continue
        asph = _ASPHERE_KEY_RE.match(key)
        if asph:
            if _close_lde(a, b):
                continue
            col = _ASPHERE_COL.get(asph.group(2))
            tracked = col is not None and ("asphere", int(asph.group(1)), col) in fp_found
            changed += tracked                    # OBSERVED movement, whatever the permission (E3)
            if allow_dofs and tracked:
                allowed += 1
            else:
                diffs.append((key, a, b))
            continue
        if not _lens_state._same(a, b):      # solve types, materials, waves, fields, ... : exact
            diffs.append((key, a, b))
    mfe["equal"] = mfe["struct_equal"] and mfe["n_value_diff"] == 0
    stop_equal = found.get("stop") == now.get("stop")
    mce_equal = found.get("mce") == now.get("mce")
    all_diffs = diffs + mfe_diffs
    if not stop_equal:
        all_diffs.append(("stop", found.get("stop"), now.get("stop")))
    if not mce_equal:
        all_diffs.append(("mce", found.get("mce"), now.get("mce")))
    return {"lde": diffs, "lde_allowed": allowed, "variable_cells_changed": changed, "mfe": mfe,
            "stop_equal": stop_equal, "mce_equal": mce_equal, "n_differences": len(all_diffs),
            "first_differences": [_jsonable(list(d)) for d in all_diffs[:10]],
            "pass": not all_diffs}


# --------------------------------------------------------------------------- #
# solve cells: freeze / re-mark
# --------------------------------------------------------------------------- #
def _solve_of(row, token):
    return _sc.read_solve_type(_cell_of(row, token))


def freeze_thickness(system, s, undo):
    """Make surface ``s``'s thickness solve Fixed for the run; returns the PRIOR solve name.

    A Fixed cell is left alone (no engine call, nothing to undo). A Variable cell is made Fixed and
    its undo (``MakeSolveVariable``) is pushed BEFORE the write. Any other solve (a pickup, a
    ray-height solve ...) is refused ``solve_on_cell``: changing it would not be undone by this step.
    """
    row = system.LDE.GetSurfaceAt(int(s))
    prior = _solve_of(row, "thickness")
    if prior is None:
        raise LayoutError("surface %d: the thickness solve type is unreadable" % s,
                          reason="solve_on_cell")
    if prior == "Fixed":
        return prior
    if prior != "Variable":
        raise LayoutError("surface %d: the thickness carries a %s solve; refusing to freeze it" % (s, prior),
                          reason="solve_on_cell")

    def _undo(s=int(s)):
        system.LDE.GetSurfaceAt(s).ThicknessCell.MakeSolveVariable()
    undo.append(("unfreeze_thickness_%d" % s, _undo))
    row.ThicknessCell.MakeSolveFixed()
    now = _solve_of(system.LDE.GetSurfaceAt(int(s)), "thickness")
    if now != "Fixed":
        raise LayoutError("surface %d: the thickness did not freeze (solve reads %r)" % (s, now),
                          reason="freeze_failed")
    return prior


def remark_variables(system, found):
    """Re-mark every radius / thickness / conic cell that was Variable in ``found`` and no longer is.

    ``reset_to_single_config`` bakes a per-config Variable as Fixed, and the build froze cells; both
    are repaired here from the ``state_with_mce`` digest. Each write is read back. Returns
    ``[(surface, token)]`` re-marked. Raises ``LayoutError`` if the surface count differs from
    ``found`` (the layout was not fully removed: the indices no longer line up) or a re-mark fails.
    """
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    if n != found.get("n_surfaces"):
        raise LayoutError("surface count is %d, expected %s: cannot re-mark variables by index"
                          % (n, found.get("n_surfaces")), reason="remark_surface_count")
    remarked = []
    for i in range(n):
        row = lde.GetSurfaceAt(i)
        for token in _CELL_TOKENS:
            if found.get("s%d.%s.solve" % (i, token)) != "Variable" or _solve_of(row, token) == "Variable":
                continue
            _cell_of(row, token).MakeSolveVariable()
            if _solve_of(lde.GetSurfaceAt(i), token) != "Variable":
                raise LayoutError("surface %d: the %s cell did not return to Variable" % (i, token),
                                  reason="remark_failed")
            remarked.append((i, token))
    return remarked


# --------------------------------------------------------------------------- #
# global frame / zero-tilt check
# --------------------------------------------------------------------------- #
def global_matrix(system, s):
    """The 12 floats (the 3x3 rotation, then X, Y, Z) of ``LDE.GetGlobalMatrix(s)``; an unsuccessful or malformed read raises."""
    f = _geom._read_one_global_frame(system.LDE, int(s))
    if not f["ok"]:
        raise LayoutError("the global frame of surface %d could not be read (unsuccessful or malformed "
                          "GetGlobalMatrix)" % int(s), reason="layout_unfaithful")
    return list(f["R"]) + list(f["vertex"])


def zero_tilt_check(system, expected):
    """Max abs difference between live global frames and ``expected`` ``{surface: [12 floats]}``.

    Returns the residual when it is within ``ZERO_TILT_TOL``; raises ``LayoutError`` (reason
    ``layout_unfaithful``, ``.value`` = the residual) when it is not: the inserted layout does not
    reproduce the downstream frames at zero tilt, so nothing may be run on it.
    """
    worst = 0.0
    for surface, want in expected.items():
        got = global_matrix(system, surface)
        for g, w in zip(got, want):
            worst = max(worst, abs(g - w))
    if not worst <= ZERO_TILT_TOL:
        raise LayoutError(
            "the temporary CB layout does not reproduce the downstream frames at zero tilt "
            "(max |delta| %.3g > %.0e); refusing to run on it" % (worst, ZERO_TILT_TOL),
            reason="layout_unfaithful", value=worst)
    return worst


# --------------------------------------------------------------------------- #
# surface insertion with guarded undos
# --------------------------------------------------------------------------- #
def _insert_with_undo(system, index, n_after, undo, name):
    """``InsertNewSurfaceAt(index)`` with its removal registered FIRST and guarded by the count.

    ``n_after`` is the surface count once this insert (and every earlier one of the build) has
    happened. The undo removes the surface only if the count says the insert landed
    (``n == n_after``); if the insert never happened (``n == n_after - 1``) it does nothing, so a
    failed insert is never "undone" into a deleted real surface. Any other count raises.
    """
    lde = system.LDE

    def _undo(index=index, n_after=n_after, name=name):
        n = int(lde.NumberOfSurfaces)
        if n == n_after - 1:
            return
        if n != n_after:
            raise LayoutError("undo %s: the surface count is %d, expected %d or %d"
                              % (name, n, n_after, n_after - 1), reason="undo_surface_count")
        lde.RemoveSurfaceAt(index)
        if int(lde.NumberOfSurfaces) != n_after - 1:
            raise LayoutError("undo %s: RemoveSurfaceAt(%d) did not shrink the lens" % (name, index),
                              reason="undo_surface_count")
    undo.append((name, _undo))
    lde.InsertNewSurfaceAt(index)
    if int(lde.NumberOfSurfaces) != n_after:
        raise LayoutError("InsertNewSurfaceAt(%d) did not grow the lens to %d surfaces" % (index, n_after),
                          reason="insert_failed")


def _restore_thickness(system, index, value, name, undo):
    """Register 'write ``value`` back on ``index``' BEFORE the caller mutates that thickness."""
    def _undo(index=index, value=value):
        system.LDE.GetSurfaceAt(index).Thickness = value
    undo.append((name, _undo))


def _cb_step(envelope, what):
    """Check a ``cb_surface`` envelope; a refusal is a build failure carrying its own text."""
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        text = envelope.get("error") if isinstance(envelope, dict) else repr(envelope)
        raise LayoutError("%s refused: %s" % (what, text), reason="layout_build_failed")
    shift = envelope.get("index_shift")
    if isinstance(shift, dict) and shift.get("delta") not in (0, None):
        raise LayoutError("%s reported an unexpected surface-index shift %r" % (what, shift),
                          reason="layout_build_failed")
    return envelope


def insert_group_pair(session, a, b, undo):
    """Wrap the air-bounded group ``a..b`` in a CB pair.

    ``cb1`` at ``a`` (the group's front vertex), the group moves to ``a+1 .. b+1`` with its last
    thickness zeroed, a dummy ``-L`` back to the front vertex, ``cb2`` (the atomic inverse), and a
    dummy ``L + t_last`` forward again; four inserted surfaces, downstream unchanged at zero tilt.
    Returns ``{entry_cb, group, dummy_back, return_cb, dummy_forward, L, t_last, t_last_solve,
    n_surfaces, frozen}``. Every mutation has its undo pushed first (they pop in teardown order).
    """
    system = session.system
    lde = system.LDE
    n0 = int(lde.NumberOfSurfaces)
    length = math.fsum(float(lde.GetSurfaceAt(i).Thickness) for i in range(a, b))
    t_last = float(lde.GetSurfaceAt(b).Thickness)

    _insert_with_undo(system, a, n0 + 1, undo, "remove_entry_cb")
    _cb_step(cb_surface.add_coordinate_break(session, {"surface": a}), "add_coordinate_break")
    last = b + 1
    prior = freeze_thickness(system, last, undo)
    _restore_thickness(system, last, t_last, "restore_group_last_thickness", undo)
    lde.GetSurfaceAt(last).Thickness = 0.0

    d1 = b + 2
    _insert_with_undo(system, d1, n0 + 2, undo, "remove_dummy_back")
    lde.GetSurfaceAt(d1).Thickness = -length
    cb2 = b + 3
    _insert_with_undo(system, cb2, n0 + 3, undo, "remove_return_cb")
    _cb_step(cb_surface.add_return_cb(session, {"entry_surface": a, "return_surface": cb2}),
             "add_return_cb")
    d2 = b + 4
    _insert_with_undo(system, d2, n0 + 4, undo, "remove_dummy_forward")
    lde.GetSurfaceAt(d2).Thickness = length + t_last
    return {"entry_cb": a, "group": [a + 1, b + 1], "dummy_back": d1, "return_cb": cb2,
            "dummy_forward": d2, "L": length, "t_last": t_last, "t_last_solve": prior,
            "n_surfaces": int(lde.NumberOfSurfaces),
            "frozen": [{"surface": last, "cell": "thickness", "prior_solve": prior}]}


def insert_surface_pair(session, s, undo):
    """Wrap ONE surface ``s`` in a CB pair for a TSTX / TSTY perturbation.

    ``cb1`` at ``s``, the surface moves to ``s+1`` with its thickness zeroed (and frozen), and ``cb2``
    at ``s+2`` carries the original thickness ``t``: two inserted surfaces. Returns
    ``{cb1, s, cb2, t, t_solve, n_surfaces, frozen}`` with ``s`` the surface's NEW index.
    """
    system = session.system
    lde = system.LDE
    n0 = int(lde.NumberOfSurfaces)
    t = float(lde.GetSurfaceAt(s).Thickness)

    _insert_with_undo(system, s, n0 + 1, undo, "remove_entry_cb")
    _cb_step(cb_surface.add_coordinate_break(session, {"surface": s}), "add_coordinate_break")
    moved = s + 1
    prior = freeze_thickness(system, moved, undo)
    _restore_thickness(system, moved, t, "restore_surface_thickness", undo)
    lde.GetSurfaceAt(moved).Thickness = 0.0
    cb2 = s + 2
    _insert_with_undo(system, cb2, n0 + 2, undo, "remove_return_cb")
    _cb_step(cb_surface.add_return_cb(session, {"entry_surface": s, "return_surface": cb2}),
             "add_return_cb")
    lde.GetSurfaceAt(cb2).Thickness = t
    return {"cb1": s, "s": moved, "cb2": cb2, "t": t, "t_solve": prior,
            "n_surfaces": int(lde.NumberOfSurfaces),
            "frozen": [{"surface": moved, "cell": "thickness", "prior_solve": prior}]}
