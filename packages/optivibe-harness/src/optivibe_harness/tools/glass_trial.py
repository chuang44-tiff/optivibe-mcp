"""tools/glass_trial.py -- ``trial_glass_swaps``.

Changes glass on the loaded lens, re-optimises, measures, saves each result as a candidate and puts
the lens back AS FOUND. It is the one MUTATING tool of the glass-matching family, so the restore is
the part that must be right: the lens as found is read at zero tolerance (``_lens_state``), saved
to a kept checkpoint, loaded back and compared BEFORE any arm runs, and every arm starts from that
proven loaded state. The final proof runs in ``finally``; a restore that cannot be proven is
reported as ``restored: false`` with ``checkpoint_path``, never as success.

An arm optimises in blocks (50 cycles, up to 10 passes and 60 s per block) until it stops by itself
or its own wall-time budget is spent. A self-stop is a resting point of this call, not a finished
lens, so only a trial that stopped by itself is compared with a control run the same way.
"""
import math
import os
import time
import uuid

from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _glass_explain_read as _ger
from . import _lens_state as _ls
from . import _measurement_common as _mc
from . import _optimize_common as _oc
from . import _solve_trace as _st
from . import _workspace_paths as _wsp
from . import analysis_measure as _am
from . import glass_explain as _ge
from . import lens_glass as _lg
from . import load_design as _ld
from .glass_explain import explain_glass_swaps as _explain
from .lens_glass import _load_catalog_proven
from .lens_glass import substitute_glass as _substitute_glass
from .optimize_run import dry_run as _dry_run
from .optimize_run import optimize as _optimize
from .workspace import _design_name_error as _name_error
from .workspace import save_candidate as _save_candidate

_Refuse = _ger._Refuse
_read_op = _mc.read_operand_slots          # module alias so a test can patch the operand reads
_read_state = _ls.read_state               # ... and the two state readers
_light_state = _ls.light_state

_TOOL = "trial_glass_swaps"
_MAX_SETS = 4
_MAX_ENTRIES = 3                         # glasses per set, each on its own element
_DEFAULT_BUDGET_S, _MIN_BUDGET_S, _MAX_BUDGET_S = 120.0, 10.0, 600.0
_MAX_CALL_S = 1800.0             # one call: (sets + 1) x time_budget_s, at most
_CYCLES, _BLOCK_PASSES, _BLOCK_S, _MIN_BLOCK_S = 50, 10, 60.0, 10.0
_GATE_PCT = 1.0
_UNUSABLE_WALL_S = 6.0           # charge for a block whose envelope wall time is unusable
_AXCL_ZONE = 0.707
_RING = 6
_FIT_STEP_FACTOR = 8.0            # h = 8 x primary wavelength x WFNO^2 (um)
_FIT_MAX_VERTEX_STEPS = 3.0
_ONLY_NOT_FINISHED = ("did not stop by itself within time_budget_s=%g s; rerun this set with a "
                      "larger time_budget_s")
# Two early verdicts, both on MERIT against the control.
# Calibrated on all 174 trial arms of the 20x dogfood runs: no arm more than 7.4% behind its control
# after the first block ever finished at or below it. On a 14-element
# 10x lens that did not hold (a swap dropped after one block ran to rest by hand and was measured), so the
# level alone no longer drops a trial; see _blocks.
_DROP_BEHIND_PCT = 10.0          # from block 2, a trial this far behind AND too slow is dropped
_SETTLED_GAIN_PCT = 0.5          # a block after the first that gains less than this is at rest
_AT_REST = ("stopped_by_itself", "settled")

# The bands (section 4): the smallest change that enters better/worse. A quantity whose band is
# None carries numbers and no better/worse until the live gate measures it (filled in one place).
_BANDS = {
    "strehl": 0.005, "lateral_color_um": 0.02,
    # measured at the live gate (each arm restarted from its candidate; about three
    # times the largest drift: 0.048 um, 7.0e-5 per mm, 3.9e-4 waves); total track and glass
    # thickness are layout numbers with no band
    "axial_color_um": 0.15, "petzval_per_mm": 2e-4, "rms_wavefront_waves": 0.0012,
}
_GATE_QUANTITIES = (("EFFL", "effl"), ("WFNO", "wfno"), ("ISNA", "isna"))

# Every tool-authored message lives here (the vocabulary test enumerates this table).
_MSG = {
    "swaps": "swaps must be 1 to 4 sets, each one {surface, glass, catalog} or a list of 1 to 3 "
             "of them on different elements",
    "free_stop": "require_free_stop must be true or false",
    "budget": "time_budget_s must be a number from 10 to 600",
    "budget_call": "time_budget_s is per arm (the control and each set) and one call is capped at "
                   "%g s: with %d sets each arm can have at most %g s",
    "name": "design_name must be a non-empty string",
    "not_single": "set %d: surface %d is not the single row of one glass element",
    "same_element": "set %d: two entries sit in element %d",
    "not_in_catalog": "set %d: glass %s is not in catalog %s",
    "multi_config": "the lens has %d configurations; trial by hand",
    "not_ready": "dry_run did not report ready: %s: %s",
    "material_solve": "surface %d's glass is under a %s solve; a swap would not hold",
    "no_bind": "set %d: %s, in use ahead of %s, also holds %s; the engine would bind %s",
    "checkpoint": "the checkpoint could not be saved or does not read as a lens file; nothing "
                  "was changed",
    "checkpoint_saved": "the checkpoint could not be saved or does not read as a lens file; the lens "
                        "cells were not changed, and the session's backing file may now be %s",
    "name_refused": "design_name is not usable: %s",
    "picture_failed": "save_candidate saved the lens file and its picture failed: %s",
    "control_budget": "the control did not stop by itself within time_budget_s=%g s: rerun one set "
                      "with a larger time_budget_s, or optimise the lens to rest first and run the "
                      "trial again",
    "restore_not_verified": "the lens was not back at the lens as found after trial %d; not run",
    "call_aborted": "the call stopped at trial %d: %s; not run",
    "swap_not_landed": "surface %d reads %s from %s; asked %s from %s",
    "merit_uncomputable": "the optimiser could not start from this swap (the merit function is "
                          "not computable); try a glass nearer the present one",
    "optimize_failed": "optimize refused (%s) or diverged",
    "ended_unphysical": "optimised into a lens that cannot be built: %s",
    "not_saved": "save_candidate failed: %s; no loadable file",
    "geometry": "the geometry audit did not certify the lens (%s)",
    "new_violation": "new clearance violation at surface %s (%s) the control does not have: the "
                     "merit function does not hold it; add the floor and run the trial again",
    "off_control": "%s is %.2f%% off the control (limit 1%%): the merit function does not hold "
                   "it; add the constraint and run the trial again",
    "unreadable": "%s could not be read on the %s",
    "first_load": "the checkpoint does not hold the lens as found; first differences: %s",
    "restore_unproven": "the lens could not be proven back at the lens as found (%s)",
    "failed": "trial_glass_swaps fault (%s)",
    "budget_cancel": "the optimizer stop could not be confirmed",
    "dropped": "dropped after %d blocks: merit %.6g is %.1f%% above the control's %.6g (limit %g%%) "
               "and at its last block's pace it needs %s more blocks of %.0f s, with %.0f s of its "
               "budget left; not measured",
}
_WARN_KEYS = ("thin_edge_warning", "buried_center_warning", "negative_bfl_warning",
              "negative_air_gap_warning", "warning")


# --------------------------------------------------------------------------- #
# 1. the arguments
# --------------------------------------------------------------------------- #
def _validate(params):
    bad = _Refuse("tool_param", _MSG["swaps"])
    swaps = params.get("swaps")
    if not isinstance(swaps, list) or not 1 <= len(swaps) <= _MAX_SETS:
        raise bad
    sets = []
    for s in swaps:
        entries = [s] if isinstance(s, dict) else s
        if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_ENTRIES:
            raise bad
        norm = []
        for e in entries:
            surf = e.get("surface") if isinstance(e, dict) else None
            if isinstance(surf, float) and surf.is_integer():
                surf = int(surf)
            glass = e.get("glass") if isinstance(e, dict) else None
            cat = e.get("catalog") if isinstance(e, dict) else None
            if (isinstance(surf, bool) or not isinstance(surf, int) or not isinstance(glass, str)
                    or not glass.strip() or not isinstance(cat, str) or not cat.strip()):
                raise bad
            norm.append({"surface": surf, "glass": glass.strip(), "catalog": cat.strip()})
        sets.append(norm)
    budget = params.get("time_budget_s", _DEFAULT_BUDGET_S)
    try:
        usable = (not isinstance(budget, bool) and isinstance(budget, (int, float))
                  and math.isfinite(budget) and _MIN_BUDGET_S <= budget <= _MAX_BUDGET_S)
    except OverflowError:                    # an int too large for a float
        usable = False
    if not usable:
        raise _Refuse("tool_param", _MSG["budget"])
    if (len(sets) + 1) * budget > _MAX_CALL_S:   # the budget is per arm; the call is capped
        raise _Refuse("tool_param", _MSG["budget_call"] % (
            _MAX_CALL_S, len(sets), _MAX_CALL_S / (len(sets) + 1)))
    name = params.get("design_name", "glass_trial")
    if not isinstance(name, str) or not name.strip():
        raise _Refuse("tool_param", _MSG["name"])
    refusal = _name_error(name)              # the workspace's own validator, not a copy of its rules
    if refusal:
        raise _Refuse("tool_param", _MSG["name_refused"] % (refusal,))
    free_stop = dict.get(params, "require_free_stop", True)
    if not isinstance(free_stop, bool):          # never coerced, as optimize's own door
        raise _Refuse("tool_param", _MSG["free_stop"])
    return sets, float(budget), name, free_stop


# --------------------------------------------------------------------------- #
# 2/3. the gate (reads only) and the binding check
# --------------------------------------------------------------------------- #
def _refusal_of(r):
    return _Refuse(r.get("error_family") or "explain_failed", r.get("error") or "", r.get("reason"))


def _gate(session, term, pair, sets, free_stop):
    """Steps 2: the explain table, the element/group/solve checks, the Petzval predictions, dry_run."""
    system = session.system
    base = {"term": term}
    if pair is not None:
        base["pair"] = pair
    r = _explain(session, base)
    if not r.get("ok"):
        raise _refusal_of(r)
    count = int(r["detail"]["configuration"]["count"])
    if count > 1:
        raise _Refuse("glass_trial_unsupported", _MSG["multi_config"] % count, "multi_configuration")
    elements = r["elements"]
    single = {e["surfaces"][0]: k for k, e in enumerate(elements)
              if e["surfaces"][1] == e["surfaces"][0] + 1}
    lde = system.LDE
    for n, entries in enumerate(sets, 1):
        hits = []
        for e in entries:
            if e["surface"] not in single:
                raise _Refuse("tool_param", _MSG["not_single"] % (n, e["surface"]))
            hits.append(single[e["surface"]])
        for i, k in enumerate(hits):
            if k in hits[:i]:
                raise _Refuse("tool_param", _MSG["same_element"] % (n, elements[k]["surfaces"][0]))
        for e in entries:
            kind = _oc._base_token(lde.GetSurfaceAt(e["surface"]).MaterialCell.GetSolveData().Type)
            if kind != "Fixed":
                raise _Refuse("glass_trial_unsupported",
                              _MSG["material_solve"] % (e["surface"], kind), "material_solve")
    predictions = None
    if term == "petzval":
        predictions = []
        for entries in sets:
            p = _explain(session, {"term": term, "swap": [dict(e) for e in entries]})
            if not p.get("ok"):
                raise _refusal_of(p)
            predictions.append({"prediction": p.get("prediction"), "flags": [
                f for f in p.get("flags", []) if f.get("flag") == "prediction_in_cemented_group"]})
    d = _dry_run(session, {"require_free_stop": free_stop})
    if not (isinstance(d, dict) and d.get("ok") and d.get("ready") is True):
        d = d if isinstance(d, dict) else {}
        raise _Refuse("glass_trial_unsupported", _MSG["not_ready"] % (
            d.get("error_family"), d.get("error")), "not_ready")
    return r, predictions


def _binding(system, sets, primary_um):
    """Step 3: every entry must bind to the named catalog, with a database index at the primary."""
    from optivibe_reference.glass_build import open_glass_catalog
    cats = system.SystemData.MaterialCatalogs
    in_use = _lg._catalogs_in_use(cats)
    universe = in_use + _lg._available_catalogs(cats)
    conn = open_glass_catalog()
    try:
        for n, entries in enumerate(sets, 1):
            for e in entries:
                key = _ge._key(e["catalog"])
                canon = next((c for c in universe if _ge._key(c) == key), None)
                members = {m.casefold() for m in cats.GetMaterialsInCatalog(canon)} if canon else set()
                held = _ge._holders(conn, key, e["glass"])
                n_new = _ge._index_at(conn, held[0][0], held[0][1], primary_um) if held else None
                if canon is None or e["glass"].casefold() not in members or n_new is None:
                    raise _Refuse("tool_param", _MSG["not_in_catalog"] % (n, e["glass"], e["catalog"]))
                for c in in_use + [canon]:
                    if c.casefold() == canon.casefold():
                        break
                    if e["glass"].casefold() in {m.casefold() for m in cats.GetMaterialsInCatalog(c)}:
                        raise _Refuse("glass_trial_unsupported", _MSG["no_bind"] % (
                            n, c, canon, e["glass"], c), "catalog_would_not_bind")
                e["canon"], e["n_new"] = canon, n_new
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 9. the measured vector
# --------------------------------------------------------------------------- #
def _get(system, code, slots):
    """One operand reading, or None when it is not available (sentinel, non-real, throw)."""
    try:
        raw, suspicious = _read_op(system, code, slots)
    except Exception:  # noqa: BLE001 -- an unreadable operand is "unavailable", never a crash
        return None
    if suspicious or isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return None
    return float(raw)


def _glass_thickness(system):
    lde = system.LDE
    vals = []
    for i in range(1, int(lde.NumberOfSurfaces) - 1):
        row = lde.GetSurfaceAt(i)
        mat = _oc._base_token(row.Material or "").strip()
        if mat and mat.upper() != "MIRROR":
            vals.append(float(row.Thickness))
    return max(vals) if vals else None


def _parabola_vertex(xs, ys):
    """Vertex of the parabola through three (x, y); None unless the curvature is positive."""
    (x0, x1, x2), (y0, y1, y2) = xs, ys
    h = x2 - x1
    a = (y2 + y0 - 2.0 * y1) / (2.0 * h * h)
    b = (y2 - y0) / (2.0 * h)
    if not math.isfinite(a) or a <= 1e-9 * max(y0, y1, y2) / (h * h):
        return None
    return x1 - b / (2.0 * a)


class _Run:
    """One call's working state: the plan, the found lens and the results so far."""

    def __init__(self, session, term, pair_um, budget, name, free_stop=True):
        self.session, self.term, self.budget, self.name = session, term, budget, name
        self.free_stop = free_stop
        self.pair_um = pair_um
        self.control_violations = set()
        self.ckpt = None
        self.stop_call = False

    @property
    def system(self):
        return self.session.system

    # ---- the wavelength / field plan --------------------------------------------------- #
    def plan(self):
        rows, bad, ok = _am._read_wavelength_rows(self.system)
        prim, fault = _st._primary_wave(self.system)
        if not ok or bad or fault or not rows:
            raise _Refuse("glass_trial_unsupported", _MSG["not_ready"] % ("wavelengths", fault),
                          "not_ready")
        self.prim = prim
        self.waves = sorted((um, r) for r, um in rows)           # [(um, engine row)]
        self.prim_um = next(um for um, r in self.waves if r == prim)
        self.fields = _mc.read_field_hy(self.system)

    # ---- the vector --------------------------------------------------------------------- #
    def _first_order(self, system, v, note):
        for code, key in _GATE_QUANTITIES + (("TOTR", "totr"),):
            v[key] = _get(system, code, _am._wave_slot(self.prim))
            if v[key] is None:
                note(key, "")
        v["glass_thickness"] = _glass_thickness(system)

    def _fields(self, system, v, note):
        v["strehl"], v["rms"] = {}, {}
        for k, (_i, _y, hy) in enumerate(self.fields, 1):
            v["strehl"][k] = _get(system, "STRH", _am._strh_slots(0, k))
            v["rms"][k] = _get(system, "RWCE", _am._rwce_slots(_RING, 0, 0.0, hy))
            for key in ("strehl", "rms"):
                if v[key][k] is None:
                    note(key, k)

    def _lateral(self, system, v, note):
        image = _mc.image_surface_index(system)
        ref = _get(system, "REAY", _am._reay_chief_slots(image, self.prim, 1.0))
        v["offsets"] = {}
        for um, row in self.waves:
            y = _get(system, "REAY", _am._reay_chief_slots(image, row, 1.0))
            v["offsets"][um] = None if y is None or ref is None else 1000.0 * (y - ref)
            if v["offsets"][um] is None:
                note("lateral_color_um", um)

    def _petzval(self, system, v, note):
        petc = _get(system, "PETC", _am._wave_slot(self.prim))
        v["petzval"] = None if petc is None else -petc
        if petc is None:
            note("petzval_per_mm", "")
        v["fcgs"], v["fcgt"] = {}, {}
        for k, (_i, _y, hy) in enumerate(self.fields, 1):
            slots = {3: ("Wave", self.prim), 4: ("Hx", 0.0), 5: ("Hy", hy)}
            v["fcgs"][k], v["fcgt"][k] = (_get(system, "FCGS", slots), _get(system, "FCGT", slots))

    def _axial_detail(self, system, v):
        v["axcl"], v["rwce_axial"] = {}, {}
        for um, row in self.waves:
            slots = _am._axcl_slots(self.prim, row)
            slots[4] = ("Zone", _AXCL_ZONE)
            a = _get(system, "AXCL", slots)
            v["axcl"][um] = None if a is None else 1000.0 * a
            v["rwce_axial"][um] = _get(system, "RWCE", _am._rwce_slots(_RING, row, 0.0, 0.0))

    def read_vector(self, system):
        v = {"unavailable": []}

        def note(quantity, at, reason="unavailable_reading"):
            v["unavailable"].append({"quantity": quantity, "at": at, "reason": reason})
        self._first_order(system, v, note)
        self._fields(system, v, note)
        if self.term == "lateral_color":
            self._lateral(system, v, note)
        elif self.term == "petzval":
            self._petzval(system, v, note)
        else:
            self._axial_detail(system, v)
        v["_note"] = note
        return v

    def fit_axial(self, system, v):
        """The LAST read of an arm: best-focus shift per wavelength by a three-point parabola."""
        note = v.pop("_note")
        v["offsets"] = {um: None for um, _r in self.waves}
        wfno = v.get("wfno")
        n = int(system.LDE.NumberOfSurfaces)
        row = system.LDE.GetSurfaceAt(n - 2)
        if wfno is None or row is None:
            return self._unsettled(v, note)
        try:
            cell = row.ThicknessCell
            if _oc._base_token(cell.GetSolveData().Type) not in ("Fixed", "Variable"):
                cell.MakeSolveFixed()
            t0 = float(system.LDE.GetSurfaceAt(n - 2).Thickness)
        except Exception:  # noqa: BLE001
            return self._unsettled(v, note)
        h = _FIT_STEP_FACTOR * self.prim_um * wfno * wfno          # um
        vertex = {}
        for um, wrow in self.waves:
            vertex[um] = self._vertex(system, n - 2, t0, wrow, h, 0.0, True)
        ref = vertex[self.prim_um]
        for um, _wrow in self.waves:
            if vertex[um] is None or ref is None:
                note("axial_color_um", um, "focus_fit_unsettled")
            else:
                v["offsets"][um] = vertex[um] - ref

    def _unsettled(self, v, note):
        for um, _r in self.waves:
            note("axial_color_um", um, "focus_fit_unsettled")

    def _vertex(self, system, surf, t0, wrow, h, centre, allow_refit):
        ys = []
        for k in (-1.0, 0.0, 1.0):
            target = t0 + (centre + k * h) * 1e-3
            lde = system.LDE
            lde.GetSurfaceAt(surf).Thickness = target
            if abs(float(lde.GetSurfaceAt(surf).Thickness) - target) > 1e-9 * max(1.0, abs(target)):
                return None
            r = _get(system, "RWCE", _am._rwce_slots(_RING, wrow, 0.0, 0.0))
            if r is None:
                return None
            ys.append(r * r)
        rel = _parabola_vertex((-h, 0.0, h), ys)
        if rel is None:
            return None
        if abs(rel) <= _FIT_MAX_VERTEX_STEPS * h:
            return centre + rel
        return self._vertex(system, surf, t0, wrow, h, centre + rel, False) if allow_refit else None


# --------------------------------------------------------------------------- #
# 8. one arm: the block loop
# --------------------------------------------------------------------------- #
def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _blocks_needed(before, after, control_merit):
    """Blocks a trial at ``after`` needs to reach ``control_merit`` at its last block's pace.

    The pace is the last block's relative gain g, applied again each block: after k more blocks
    the merit is after * (1 - g)^k. Infinite when it cannot get there (no gain, or a control at 0).
    """
    if before is None or before <= 0 or control_merit <= 0:
        return math.inf
    g = (before - after) / before
    if g <= 0:
        return math.inf
    if g >= 1:
        return 1.0
    return math.log(after / control_merit) / -math.log(1.0 - g)


def _blocks(session, budget, free_stop=True, control_merit=None):
    """Optimise in blocks until the arm stops by itself, settles, is dropped, or its budget is spent.

    ``control_merit`` is the control's final merit (None for the control itself). Any block after
    the first that gains less than _SETTLED_GAIN_PCT ends the arm ``settled`` (the first block's
    gain includes the swap's jump). From the second block on, a trial
    more than _DROP_BEHIND_PCT above the control that, at its last block's pace, cannot reach the
    control within its remaining budget is ``dropped``; the projection is stored on the last block
    as ``drop_projection``. Nothing is dropped after the first block: on a large lens one block
    says how far the swap knocked the lens, not where it settles (10x run 63f1bf16).
    Returns ``(stop, blocks, last_env, failure)``; ``failure`` is a refused/failed block's
    envelope (the stop is then None), else None.
    """
    spent, blocks, last = 0.0, [], None
    while True:
        request = min(_BLOCK_S, budget - spent)
        started = time.monotonic()
        r = _optimize(session, {"cycles": _CYCLES, "max_passes": _BLOCK_PASSES,
                                "time_budget_s": request, "require_free_stop": free_stop})
        local = time.monotonic() - started
        r = r if isinstance(r, dict) else {}
        if not r.get("ok") or r.get("verdict") == "diverged":
            return None, blocks, last, r
        last = r
        blocks.append({k: r.get(k) for k in ("passes", "budget_reached", "merit_before",
                                              "merit_after", "run_time_s", "wall_time_s")})
        wall = _num(r.get("wall_time_s"))
        # charge the larger of the envelope's wall time and our own clock, never under 1 s; an
        # envelope value that is missing, zero or negative is unusable and charges at least
        # _UNUSABLE_WALL_S, so the budget always runs down (and a runaway arm ends in <= 20 blocks)
        wall = request if wall is None else wall          # a missing value is charged as requested
        charge = max(wall, local, 1.0) if wall > 0 else max(local, _UNUSABLE_WALL_S)
        spent += charge
        passes = r.get("passes")
        if (r.get("budget_reached") is False and isinstance(passes, int)
                and not isinstance(passes, bool) and 1 <= passes < _BLOCK_PASSES):
            return "stopped_by_itself", blocks, last, None
        before, after = _num(r.get("merit_before")), _num(r.get("merit_after"))
        if (len(blocks) > 1 and before is not None and after is not None and before >= 0
                and (before == 0 or (before - after) / before * 100.0 < _SETTLED_GAIN_PCT)):
            return "settled", blocks, last, None    # a block from merit 0 cannot gain: at rest
        if len(blocks) > 1 and control_merit is not None and after is not None and control_merit >= 0 \
                and after > control_merit * (1.0 + _DROP_BEHIND_PCT / 100.0):
            needed = _blocks_needed(before, after, control_merit)
            if needed * charge > budget - spent:
                blocks[-1]["drop_projection"] = {"blocks_needed": needed, "block_s": charge,
                                                 "budget_left_s": budget - spent}
                return "dropped", blocks, last, None
        if budget - spent < _MIN_BLOCK_S:
            return "time_budget", blocks, last, None


def _violations(env):
    summary = env.get("clearance_summary")
    rows = summary.get("violations") if isinstance(summary, dict) else None
    out = set()
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            out.add((row.get("surface"), row.get("kind")))
    return out


def _arm(run, entries, label, control):
    """Steps 7 to 9 for one arm (``entries`` None = the control). Returns the arm record."""
    system = run.session.system
    arm = {"status": "not_measured", "reason": None, "message": None, "stop": None, "vec": None,
           "candidate_path": None, "detail": {}, "violations": set()}

    def fail(reason, message):
        arm.update(reason=reason, message=message)
        return arm
    if entries is not None:
        landed = _swap(run, entries, arm)
        if landed is not None:
            return fail("swap_not_landed", landed)
    stop, blocks, last, failed = _blocks(run.session, run.budget, run.free_stop,
                                         _control_merit(control))
    arm["stop"], d = stop, arm["detail"]
    d["blocks"] = blocks
    if stop == "dropped":
        merit, ref = blocks[-1]["merit_after"], _control_merit(control)
        proj = blocks[-1]["drop_projection"]
        needed = proj["blocks_needed"]
        arm.update(status="dropped", reason="behind_control", message=_MSG["dropped"] % (
            len(blocks), merit, (merit - ref) / ref * 100.0 if ref else math.inf, ref,
            _DROP_BEHIND_PCT, "%.1f" % needed if math.isfinite(needed) else "unbounded",
            proj["block_s"], max(proj["budget_left_s"], 0.0)))
        return arm
    if failed is not None:
        err = str(failed.get("error") or "")
        if err.startswith("the run passed its time budget"):
            run.stop_call = True
        if failed.get("error_family") == "optimize_merit_uncomputable" and not blocks:
            return fail("merit_uncomputable", _MSG["merit_uncomputable"])
        return fail("optimize_failed", _MSG["optimize_failed"] % (failed.get("error_family"),))
    verdict = str(last.get("verdict"))
    gain = None
    if len(blocks) and _num(blocks[-1]["merit_before"]) and _num(blocks[-1]["merit_after"]) is not None:
        gain = (blocks[-1]["merit_before"] - blocks[-1]["merit_after"]) / blocks[-1]["merit_before"]
    ga = last.get("geometry_audit")
    d.update(last_block_gain_rel=gain, verdict=verdict, merit_verdict=last.get("merit_verdict"),
             geometry_audit_status=ga.get("status") if isinstance(ga, dict) else None)
    d.update({k: last[k] for k in _WARN_KEYS if k in last})
    if verdict.endswith("_unphysical"):
        summary = (ga.get("reason") or ga.get("status")) if isinstance(ga, dict) else None
        return fail("ended_unphysical", _MSG["ended_unphysical"] % (summary or verdict,))
    saved = _save_candidate(run.session, {"design_name": run.name, "label": label, "render": True})
    saved = saved if isinstance(saved, dict) else {}
    if not saved.get("ok"):
        if saved.get("zmx_ok") is True and saved.get("zmx_path"):     # the lens file landed
            arm["candidate_path"] = saved["zmx_path"]
            return fail("candidate_not_saved", _MSG["picture_failed"] % (saved.get("error"),))
        return fail("candidate_not_saved", _MSG["not_saved"] % (saved.get("error"),))
    arm["candidate_path"] = saved.get("zmx_path")
    d["clearance"] = {"ok": saved.get("clearance_ok"), "summary": saved.get("clearance_summary")}
    arm["violations"] = _violations(saved)
    if verdict.endswith("_unverified"):
        return fail("geometry_or_clearance", _MSG["geometry"] % (verdict,))
    fresh = sorted(arm["violations"] - run.control_violations, key=repr) if control else []
    if fresh:
        return fail("geometry_or_clearance", _MSG["new_violation"] % fresh[0])
    vec = arm["vec"] = run.read_vector(system)
    bad = _gate_first_order(run, vec, control, d, arm)
    if bad:
        return fail(*bad)
    if run.term == "axial_color":
        run.fit_axial(run.session.system, vec)
    else:
        vec.pop("_note", None)
    arm["status"] = "measured" if stop in _AT_REST else "not_finished"
    if arm["status"] == "not_finished":
        arm["message"] = _ONLY_NOT_FINISHED % run.budget
    return arm


def _control_merit(control):
    """The control's final merit, or None (no control yet, or no usable reading)."""
    blocks = (control or {}).get("detail", {}).get("blocks") or []
    return _num(blocks[-1].get("merit_after")) if blocks else None


def _gate_first_order(run, vec, control, detail, arm):
    """The 1% gate. Returns ``(reason, message)`` or None."""
    who = "control" if control is None else "trial"
    table = {}
    for code, key in _GATE_QUANTITIES:
        value = vec.get(key)
        ref = control["vec"][key] if control is not None else None
        pct = None
        if value is not None and ref is not None:
            pct = 0.0 if value == ref else (abs(value - ref) / abs(ref) * 100.0 if ref else math.inf)
        table[key] = {"value": value, "control": ref, "pct_off_control": pct}
        if value is None or (control is not None and ref is None):
            detail["first_order"] = table
            reason = "control_not_usable" if control is None else "first_order_off_control"
            return reason, _MSG["unreadable"] % (code, who)
        if pct is not None and pct > _GATE_PCT:
            detail["first_order"] = table
            return "first_order_off_control", _MSG["off_control"] % (code, pct)
    detail["first_order"] = table
    return None


def _swap(run, entries, arm):
    """Step 7: write the swap and read it back. Returns a message when it did not land."""
    system = run.session.system
    cats = system.SystemData.MaterialCatalogs
    reads = []
    try:
        for e in entries:
            if not cats.IsCatalogInUse(e["canon"]):
                _load_catalog_proven(cats, e["canon"])
        for e in entries:
            _substitute_glass(run.session, {"surface": e["surface"], "glass": e["glass"],
                                            "auto_load": False})
    except Exception:  # noqa: BLE001 -- the read-back below says what the lens holds
        pass
    for e in entries:
        row = run.session.system.LDE.GetSurfaceAt(e["surface"])
        mat = _oc._base_token(row.Material or "")
        cat = _ge._key(_oc._base_token(row.MaterialCatalog or ""))
        n_eng = _get(run.session.system, "INDX", {2: ("Surf", e["surface"]), 3: ("Wave", run.prim)})
        reads.append({"surface": e["surface"], "material": mat, "catalog": cat, "index": n_eng})
        if (mat.casefold() != e["glass"].casefold() or cat != _ge._key(e["catalog"])
                or n_eng is None or abs(n_eng - e["n_new"]) > 1e-9):
            arm["detail"]["swap_readback"] = reads
            return _MSG["swap_not_landed"] % (e["surface"], mat, cat, e["glass"], e["catalog"])
    arm["detail"]["swap_readback"] = reads
    return None


# --------------------------------------------------------------------------- #
# 10/11. the restore
# --------------------------------------------------------------------------- #
def _fwd(path):
    return str(path).replace("\\", "/")


def _load(run):
    """The ONE engine load of the checkpoint (steps 5, 10 and 11 all go through here)."""
    run.session.system.LoadFile(run.ckpt, False)


def _final_proof(run, found):
    """Step 11 (in ``finally``; never raises): load, full compare, optimiser inactivity."""
    proof = {"items": len(found), "n_differences": None, "first_differences": [],
             "optimizer_confirmed": False}
    try:
        try:
            _load(run)
        except BaseException as first:       # an abort: try the load once more, then re-raise it
            if isinstance(first, Exception):
                raise
            try:
                _load(run)
            except BaseException:  # noqa: BLE001
                pass
            raise
        diffs = _ls.diff(found, _read_state(run.session.system))
        proof["n_differences"] = len(diffs)
        proof["first_differences"] = [[k, repr(a), repr(b)] for k, a, b in diffs[:5]]
    except Exception as exc:  # noqa: BLE001
        proof["first_differences"] = [["read", repr(exc), ""]]
    try:
        with _oc._optimizer_session(run.session.system):
            pass
        proof["optimizer_confirmed"] = True
    except Exception:  # noqa: BLE001 -- an optimizer that is still open is NOT confirmed inactive
        pass
    proof["restored"] = proof["n_differences"] == 0 and proof["optimizer_confirmed"]
    return proof


# --------------------------------------------------------------------------- #
# the call
# --------------------------------------------------------------------------- #
def trial_glass_swaps(session, params):
    """Trial 1 to 4 glass swaps; restores the lens as found; NEVER raises."""
    state = {"touched": False, "checkpoint_path": None}
    try:
        return _run(session, params if isinstance(params, dict) else {}, state)
    except _Refuse as r:
        extra = {"reason": r.reason} if r.reason else {}
        return _ac.error_envelope(_TOOL, r.family, r.message, **extra)
    except Exception as exc:  # noqa: BLE001 -- never-raise backstop (aperture_ramp precedent)
        extra = {"checkpoint_path": state["checkpoint_path"]} if state["touched"] else {}
        return _ac.error_envelope(_TOOL, "glass_trial_failed", _MSG["failed"] % (repr(exc),),
                                  restored=not state["touched"], **extra)


def _checkpoint_refused(run, found):
    """SaveAs was issued but the checkpoint is unusable: NEVER load it; prove the lens is untouched."""
    proof = {"restored": False}
    try:
        diffs = _ls.diff(found, _read_state(run.session.system))
        with _oc._optimizer_session(run.session.system):
            pass
        proof["restored"] = not diffs
    except Exception:  # noqa: BLE001 -- unprovable
        pass
    extra = {} if proof["restored"] else {"checkpoint_path": run.ckpt}
    return _ac.error_envelope(_TOOL, "checkpoint_failed", _MSG["checkpoint_saved"] % (run.ckpt,),
                              restored=proof["restored"], **extra)


def _run(session, params, state):
    sets, budget, name, free_stop = _validate(params)
    term, pair = params.get("term"), params.get("pair")
    x1, predictions = _gate(session, term, pair, sets, free_stop)
    run = _Run(session, term, x1.get("pair_um"), budget, name, free_stop)
    run.plan()
    _binding(session.system, sets, run.prim_um)
    system = session.system
    saveas_issued = False
    try:                                             # step 4: the checkpoint, the first write
        found, light_found = _read_state(system), _light_state(system)
        folder = os.path.join(_wsp.workspace_root(session), "candidates", "trail", "glass_trial",
                              uuid.uuid4().hex[:12])
        os.makedirs(folder, exist_ok=True)
        run.ckpt = _fwd(os.path.join(folder, "checkpoint.zmx"))
        state.update(touched=True, checkpoint_path=run.ckpt)    # BEFORE SaveAs: it repoints the file
        saveas_issued = True
        system.SaveAs(run.ckpt)
        refused = _ld._precheck_file(run.ckpt)[0]
    except Exception:  # noqa: BLE001
        refused = True
    if refused and not saveas_issued:
        state["touched"] = False
        raise _Refuse("checkpoint_failed", _MSG["checkpoint"])
    if refused:
        return _checkpoint_refused(run, found)
    state.update(touched=True, checkpoint_path=run.ckpt)
    res = {"control": None, "arms": [], "halt": None, "fault": None, "first_load": None}
    try:
        try:
            _execute(run, sets, found, light_found, res)
        except Exception as exc:  # noqa: BLE001 -- a body fault: the finally below restores
            res["fault"] = (len(res["arms"]) + 1 if res["control"] else 0, repr(exc))
    finally:
        proof = _final_proof(run, found)
    proof["restored"] = proof["restored"] and not res["first_load"]
    return _assemble(run, sets, res, proof, predictions)


def _diffs(diffs):
    return [[k, repr(a), repr(b)] for k, a, b in diffs[:5]]


def _execute(run, sets, found, light_found, res):
    """Steps 5 to 10: prove the checkpoint, run the control, then each set."""
    _load(run)
    first = _ls.diff(found, _read_state(run.session.system))
    if first:
        res["first_load"] = _diffs(first)
        return
    control = None
    for j, entries in enumerate([None] + list(sets)):
        if res["halt"] or (j and control["status"] != "measured"):
            if j:
                res["arms"].append(None)
            continue
        label = "control" if j == 0 else "t%d_%s" % (j, "_".join(
            "S%d_%s" % (e["surface"], e["glass"]) for e in entries))
        arm = _arm(run, entries, label, control)
        if j == 0:
            control = res["control"] = arm
            run.control_violations = arm["violations"]
        else:
            res["arms"].append(arm)
        if run.stop_call:
            res["halt"] = ("abort", j, _MSG["budget_cancel"])
        elif control["status"] == "measured" and j < len(sets) and _back(run, light_found):
            res["halt"] = ("restore", j)


def _back(run, light_found):
    """Step 10: reload the checkpoint, compare the light state. True when NOT back as found."""
    try:
        _load(run)
        return bool(_ls.diff(light_found, _light_state(run.session.system)))
    except Exception:  # noqa: BLE001
        return True


# --------------------------------------------------------------------------- #
# the result
# --------------------------------------------------------------------------- #
def _offsets(vec):
    off = vec.get("offsets") if vec else None
    return None if off is None else {str(um): off[um] for um in off}


def _term_value(run, vec):
    if vec is None:
        return None
    if run.term == "petzval":
        return vec.get("petzval")
    off = vec.get("offsets")
    a, b = (off.get(um) for um in run.pair_um) if off else (None, None)
    return None if a is None or b is None else a - b


def _movers(run, c, t):
    """better / worse: quantities with a band, both sides available, changed beyond the band."""
    better, worse = [], []

    def add(measure, at, cv, tv, higher):
        band = _BANDS[measure]
        if band is None or cv is None or tv is None:
            return
        gain = (tv - cv) if higher else (cv - tv)
        entry = {"measure": measure, "at": at, "control": cv, "trial": tv}
        if gain > band:
            better.append(entry)
        elif gain < -band:
            worse.append(entry)

    def mag(x):
        return None if x is None else abs(x)
    for k in c["strehl"]:
        add("strehl", k, c["strehl"][k], t["strehl"].get(k), True)
        add("rms_wavefront_waves", k, c["rms"][k], t["rms"].get(k), False)
    if run.term == "petzval":
        add("petzval_per_mm", "", mag(c["petzval"]), mag(t["petzval"]), False)
    else:
        name = "lateral_color_um" if run.term == "lateral_color" else "axial_color_um"
        for um in c["offsets"]:
            add(name, um, mag(c["offsets"][um]), mag(t["offsets"].get(um)), False)
    return better, worse


def _arm_detail(arm):
    d = dict(arm["detail"])
    if arm["vec"] is not None:
        d["vector"] = {k: v for k, v in arm["vec"].items() if k not in ("unavailable", "_note")}
        d["unavailable"] = arm["vec"]["unavailable"]
    return d


def _trial_row(run, i, entries, res, cvec, cvalue, prediction):
    control, arms = res["control"], res["arms"]
    a = arms[i] if i < len(arms) else None
    reason = message = better = worse = tvec = tvalue = layout = None
    status = "not_measured"
    if control is None or control["status"] != "measured":
        reason = "control_not_usable" if control else "call_aborted"
        message = (_MSG["control_budget"] % run.budget if control and control["status"] == "not_finished"
                   else (control["message"] if control else _MSG["call_aborted"] % res["fault"]))
    elif a is None:
        halt, fault = res["halt"], res["fault"]
        reason, message = (("restore_not_verified", _MSG["restore_not_verified"] % halt[1])
                           if halt and halt[0] == "restore" else
                           ("call_aborted", _MSG["call_aborted"] % (halt[1], halt[2]))
                           if halt else ("call_aborted", _MSG["call_aborted"] % fault))
    else:
        status, reason, message, tvec = a["status"], a["reason"], a["message"], a["vec"]
        if status in ("measured", "not_finished"):
            tvalue = _term_value(run, tvec)
            layout = {"total_track_mm": {"control": cvec["totr"], "trial": tvec["totr"]},
                      "largest_glass_thickness_mm": {"control": cvec["glass_thickness"],
                                                     "trial": tvec["glass_thickness"]}}
        if status == "measured":
            better, worse = _movers(run, cvec, tvec)
    colour = run.term != "petzval"
    return {"swaps": [{k: e[k] for k in ("surface", "glass", "catalog")} for e in entries], "status": status, "reason": reason, "message": message,
            "value": {"control": cvalue, "trial": tvalue,
                      "change": None if tvalue is None or cvalue is None else tvalue - cvalue},
            "offsets_um": {"control": _offsets(cvec) if colour else None,
                           "trial": _offsets(tvec) if colour and status != "not_measured" else None},
            "better": better, "worse": worse, "layout": layout,
            "candidate_path": a["candidate_path"] if a else None,
            "prediction_source": "none" if colour else "explanatory",
            "predicted": None if colour else ((prediction or {}).get("prediction") or {}).get("petzval_sum")}


def _assemble(run, sets, res, proof, predictions):
    control, arms = res["control"], res["arms"]
    cvec = control["vec"] if control else None
    cvalue = _term_value(run, cvec)
    colour = run.term != "petzval"
    stops = [a["stop"] if a else None for a in arms] + [None] * (len(sets) - len(arms))
    cstop = control["stop"] if control else None
    summary = ("control_not_finished" if cstop not in _AT_REST else
               "some_trials_not_finished" if any(s not in _AT_REST + ("dropped",) for s in stops)
               else "all_stopped_by_themselves")
    trials = [_trial_row(run, i, e, res, cvec, cvalue, predictions[i] if predictions else None)
              for i, e in enumerate(sets)] if not res["first_load"] else []
    cmsg = _MSG["control_budget"] % run.budget if control and control["status"] == "not_finished" \
        else (control["message"] if control else None)
    body = {
        "stops": {"control": cstop, "trials": stops, "summary": summary}, "term": run.term,
        "pair_um": run.pair_um if colour else None, "unit": "um" if colour else "1/mm",
        "control": {"status": control["status"] if control else "not_measured",
                    "reason": control["reason"] if control else None, "message": cmsg,
                    "value": cvalue, "offsets_um": _offsets(cvec) if colour else None,
                    "candidate_path": control["candidate_path"] if control else None},
        "trials": trials, "bands": dict(_BANDS),
        "effort": {"cycles": _CYCLES, "block_passes": _BLOCK_PASSES, "block_s": int(_BLOCK_S),
                   "time_budget_s": run.budget, "call_cap_s": _MAX_CALL_S,
                   "reduced": run.budget < _DEFAULT_BUDGET_S,
                   "require_free_stop": run.free_stop,
                   "drop_behind_control_pct": _DROP_BEHIND_PCT,
                   "settled_block_gain_pct": _SETTLED_GAIN_PCT},
        "detail": {"control": _arm_detail(control) if control else None,
                   "trials": [_arm_detail(a) if a else None for a in arms],
                   "prediction": None if colour else predictions,
                   "restore_proof": {k: proof[k] for k in ("items", "n_differences",
                                                           "first_differences", "optimizer_confirmed")}}}
    restored, ckpt = proof["restored"], ({} if proof["restored"] else {"checkpoint_path": run.ckpt})
    if res["fault"]:
        return _ac.error_envelope(_TOOL, "glass_trial_failed", _MSG["failed"] % (res["fault"][1],),
                                  restored=restored, **body, **ckpt)
    if not restored:
        why = res["first_load"] or proof["first_differences"] or "optimizer not confirmed inactive"
        text = (_MSG["first_load"] if res["first_load"] else _MSG["restore_unproven"]) % (why,)
        return _ac.error_envelope(_TOOL, "restore_unproven", text, restored=False, **body, **ckpt)
    return {"ok": True, "tool": _TOOL, "restored": True, **body}


TRIAL_GLASS_SWAPS_SPEC = ToolSpec(
    name=_TOOL,
    handler=trial_glass_swaps,
    required_params=("term", "swaps"),
    param_types={"term": "string", "pair": "string", "swaps": "array",
                 "time_budget_s": "number", "design_name": "string",
                 "require_free_stop": "boolean"},
    description=(
        "Changes glass, re-optimises, measures, saves and puts the lens back as found. swaps: 1 to "
        "4 sets, e.g. [{\"surface\":5,\"glass\":\"N-SK16\",\"catalog\":\"SCHOTT\"}]; a set may be a "
        "dict, or a list of up to three on different elements, in any groups [[{...},{...}]]; one glass "
        "per set is the usual step, a bigger set re-optimises more glass at once and is slower. A "
        "catalog matches "
        "without \".AGF\" and without case. time_budget_s (default 120, up to 600) is the budget of "
        "each arm, the control and every set; (sets + 1) x time_budget_s may not pass 1800 s. term "
        "and pair as explain_glass_swaps. value is the named term (offset of the pair's "
        "first wavelength minus the second, or the Petzval sum): nearer zero is better, and it is "
        "not in better/worse. An offset in neither list did not move beyond its band or could not be "
        "read. Read in order: "
        "restored; stops; each status. Only an arm at rest (stopped by itself, or settled: a block "
        "after the first gained under 0.5% merit) is a measurement: better "
        "and worse compare it with a control re-optimised the same way, beyond the bands listed. "
        "time_budget means not finished: rerun that set with a larger time_budget_s; never did "
        "not help. detail last_block_gain_rel is the merit gain over the whole last optimise block, "
        "not one pass. dropped: from the second block on, the trial's merit was over 10% above the "
        "control's and, at its last block's pace, could not reach it within its budget; it is not "
        "measured. Both early verdicts judge merit, your objective, not the term. In the "
        "vector isna is the image-space NA, glass_thickness the largest centre "
        "thickness of any glass element. layout gives total track and largest glass thickness for "
        "control and trial, with no better or worse: compare them with the design's own targets. "
        "design_name (default glass_trial) names the saved candidate files. require_free_stop "
        "(default true) is passed to every optimise; pass false to trial a lens whose stop sits on "
        "a glass vertex. not_measured gives a "
        "reason and says nothing about whether the glass helps. Nothing is applied: load_design "
        "applies your choice."
    ),
)

TOOL_SPEC = TRIAL_GLASS_SWAPS_SPEC
