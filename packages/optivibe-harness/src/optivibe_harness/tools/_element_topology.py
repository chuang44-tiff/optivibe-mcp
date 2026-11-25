"""tools/_element_topology.py -- the glass-element split and cement behind ``insert_surface`` / ``remove_surface``.

NOT dispatchable (no ``TOOL_SPECS``): ``lens_surface`` dispatches to ``split`` when ``insert_surface``
carries ``glass`` and to ``cement`` when ``remove_surface`` carries ``cement``. Bare calls never reach here.

* ``split(session, params)``  -- the Standard glass element whose BACK face is row ``at`` becomes two glass
  parts behind a mean-curvature interface at half thickness. Every write is registered on an in-memory
  LIFO undo first; a failure after the first mutation is undone, PROVEN by the digest, and refused.
* ``cement(session, params)`` -- the Standard air gap ``at`` between two glass elements is closed by
  keeping one facing radius ("rear": the rear element's, a bare remove; "front": the front element's,
  written to row ``at+1`` first). Edges are predicted from the pre-op prescription and a non-positive
  edge refuses BEFORE anything changes. NO engine write follows ``RemoveSurfaceAt``.

Measured engine facts these rely on: ``probe/run.log`` and ``probe/run-r1.log`` of the cycle.
Every refusal is a ``TopologyRefusedError`` carrying one of ``REASONS``, except the existing families
(``tool_param``, ``solve_driven``, ``solve_refs_refused``, ``surface_write``).
"""
import math

from .. import _io
from ..errors import (HarnessError, SolveDrivenError, SurfaceWriteError, ToolParamError,
                      TopologyRefusedError)
from . import _clearance_common as _cl
from . import _config_common as _cc
from . import _error_note as _en
from . import _lens_common as _c
from . import _solve_cells as _sc
from . import _structural_common as _sx
from . import lens_glass as _lg

#: The frozen ``reason`` tokens of a ``topology_refused`` refusal.
REASONS = ("not_a_glass_element", "not_an_air_gap_between_elements", "surface_type_not_modelled",
           "multi_config", "geometry_unreadable", "edge_non_positive", "state_unreadable")

_EDGES_SOURCE_SPLIT = "sag at each face's own semi, live rows after the writes (gotcha 431)"
_EDGES_SOURCE_CEMENT = "predicted from the pre-op prescription at each face's own semi"
_ROLLED_BACK = " -- rolled back (proven)"


def _refuse(reason, message):
    assert reason in REASONS, reason
    raise TopologyRefusedError(message, reason=reason)


# --------------------------------------------------------------------------- #
# pure geometry
# --------------------------------------------------------------------------- #
def _edge_ok(e):
    """An edge PASSES only if it is finite and strictly positive (NaN fails; zero fails)."""
    return isinstance(e, (int, float)) and math.isfinite(e) and e > 0


def mean_curvature_radius(R_f, R_b):
    """Radius of the mean of two curvatures (``inf`` when the mean curvature is 0)."""
    c = ((0.0 if math.isinf(R_f) else 1.0 / R_f) + (0.0 if math.isinf(R_b) else 1.0 / R_b)) / 2.0
    return math.inf if c == 0 else 1.0 / c


def _check_domain(thicknesses, faces):
    """``None`` when the numbers are usable, else the reason text (pure).

    Every thickness is finite and > 0; every face is ``(radius, semi)`` with the semi finite-positive,
    the radius ``inf`` or finite non-zero, and the semi ``< |radius|`` for a finite radius. Without this
    the sag helper's 0.0 fallback could turn an invalid sag into an apparently positive edge.
    """
    for t in thicknesses:
        if not (isinstance(t, float) and math.isfinite(t) and t > 0):
            return "a thickness the operation uses is not finite and positive (%r)" % (t,)
    for R, h in faces:
        if not _cl._is_finite_positive(h):
            return "a semi-diameter the operation uses is not finite and positive (%r)" % (h,)
        if math.isnan(R) or R == 0:
            return "a radius the operation uses is not inf or finite non-zero (%r)" % (R,)
        if math.isfinite(R) and not h < abs(R):
            return "semi-diameter %r is not smaller than |radius| %r" % (h, R)
    return None


def split_edges(rows, t_front, t_rear):
    """``(e_front_part, e_rear_part)`` from the live ``[front, interface, back]`` rows (spec 4.1)."""
    f, i, b = rows
    return (_cl.edge_thickness_at(t_front, f["R"], f["conic"], i["R"], 0.0, f["semi"], i["semi"]),
            _cl.edge_thickness_at(t_rear, i["R"], 0.0, b["R"], b["conic"], i["semi"], b["semi"]))


def predict_cement_edges(rows, rule):
    """``(e_front_element, e_rear_element)`` from the pre-op ``[at-1, at, at+1, at+2]`` rows (spec 4.2)."""
    r0, r1, r2, r3 = rows
    kept = r2["R"] if rule == "rear" else r1["R"]
    h = r2["semi"]
    return (_cl.edge_thickness_at(r0["t"], r0["R"], r0["conic"], kept, 0.0, r0["semi"], h),
            _cl.edge_thickness_at(r2["t"], kept, 0.0, r3["R"], r3["conic"], h, r3["semi"]))


# --------------------------------------------------------------------------- #
# engine reads
# --------------------------------------------------------------------------- #
def _num(read):
    try:
        return float(read())
    except Exception:  # noqa: BLE001 -- an unreadable number is NaN; _check_domain refuses it
        return math.nan


def _read_rows(system, lde, indices):
    """``[{R, conic, t, semi, semi_solve, material, catalog, type, air, mirror}]`` for each index.

    A numeric read fault is NaN (the domain check refuses it); a Material read fault propagates
    (``_material_is_air``'s invariant: never fabricate "not air").
    """
    from . import _lens_state  # deferred: a module-level import drags the registration chain in (h4)
    out = []
    for i in indices:
        row = lde.GetSurfaceAt(i)
        try:
            kind = _lens_state._tok(row.Type)
        except Exception:  # noqa: BLE001 -- an unreadable type is "not Standard"
            kind = None
        material = _lens_state._tok(row.Material)
        try:
            semi_solve = _sc.read_solve_type(_sc.solve_cell(system, lde, i, "semi_diameter"))
        except Exception:  # noqa: BLE001 -- a solve read fault is disclosed as None
            semi_solve = None
        out.append({"R": _num(lambda: row.Radius), "conic": _num(lambda: row.Conic),
                    "t": _num(lambda: row.Thickness), "semi": _num(lambda: row.SemiDiameter),
                    "semi_solve": semi_solve, "material": material, "type": kind,
                    "catalog": _lg._bound_catalog(row), "air": _sx._material_is_air(row),
                    "mirror": material == "MIRROR"})
    return out


def _fail_open_count(mfe):
    """``mfe.NumberOfOperands`` as an int, or ``None`` on any read fault."""
    try:
        return int(mfe.NumberOfOperands)
    except Exception:  # noqa: BLE001 -- a disclosure read never raises
        return None


def _merit_count(system):
    try:
        return _fail_open_count(system.MFE)
    except Exception:  # noqa: BLE001 -- no MFE at all reads as unknown
        return None


def _write(lde, index, prop, value):
    """Write one LDE double, re-fetch the row, and read it back (raises ``SurfaceWriteError``)."""
    row = lde.GetSurfaceAt(index)
    if prop == "Radius":
        row.Radius = value
        actual = float(lde.GetSurfaceAt(index).Radius)
    else:
        row.Thickness = value
        actual = float(lde.GetSurfaceAt(index).Thickness)
    _c._verify_or_raise(prop.lower(), value, actual, surface=index)


def _require_one_config(system):
    count = _cc.strict_number_of_configurations(system)
    if (1 if count is None else count) != 1:
        _refuse("multi_config", "the design has %s configurations; element topology ops model one" % count)


def _is_glass(row):
    return not row["air"] and not row["mirror"]


def _not_standard(rows):
    return any(r["type"] != "Standard" for r in rows)


def _invalidate(session):
    from .optimize_merit import _invalidate_wizard_boundary
    _invalidate_wizard_boundary(session)


# --------------------------------------------------------------------------- #
# split
# --------------------------------------------------------------------------- #
def split(session, params):
    """``insert_surface(at, glass)``: split the glass element whose back face is ``at`` (spec 1.1, 2.1, 5.1)."""
    from . import _tolerance_layout as _tl  # deferred (h4): module level drags in _lens_state
    glass = params.get("glass")
    if not isinstance(glass, str) or not glass.strip():
        raise ToolParamError("glass must be a non-empty string naming the rear part's glass, got %r" % (glass,))
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    at = _c._require_int_index(params, "at")
    _c._require_insert_at(at, n)
    if at < 2:
        raise ToolParamError("a split needs the element's front face at 'at'-1 >= 1; got at=%d" % at)
    if at == n - 1:  # IMAGE is not a back face
        _refuse("not_a_glass_element", "surface %d is the IMAGE surface; 'at' must be the back face of a glass "
                "element, so the last valid split position is %d" % (at, n - 2))
    _require_one_config(system)
    front, back = _read_rows(system, lde, (at - 1, at))
    if not _is_glass(front):
        _refuse("not_a_glass_element", "surface %d is not a glass surface (air or mirror); 'at' must be the "
                "back face of a glass element whose front face is 'at'-1" % (at - 1))
    if _not_standard((front, back)):
        _refuse("surface_type_not_modelled", "surfaces %d and %d must both be Standard" % (at - 1, at))
    bad = _check_domain([front["t"]], [(front["R"], front["semi"]), (back["R"], back["semi"])])
    if bad:
        _refuse("geometry_unreadable", "surfaces %d and %d: %s" % (at - 1, at, bad))
    probe = _sc.refuse_if_driven(system, lde, at - 1, "thickness")
    if probe.get("driven") is not False:
        raise SolveDrivenError.from_probe(probe, cell_token="thickness", surface=at - 1,
                                          tool="insert_surface", intended=front["t"] / 2.0)
    canonical, _catalog = _lg._resolve_canonical(system.SystemData.MaterialCatalogs, glass)
    if canonical is None:
        raise ToolParamError("unknown glass %r: not in any in-use catalog; load its catalog with "
                             "load_catalog, then retry" % glass)
    from . import _lens_state  # deferred (h4); bound before the try so the except clause can name it
    try:
        found = _tl.state_with_mce(system, allow_no_variables=True)
    except _lens_state.StateUnreadable as exc:
        _refuse("state_unreadable", "the lens state could not be read, so a rollback could not be proven: %s" % exc)

    t_parent = front["t"]
    t_half = t_parent / 2.0
    r_int = mean_curvature_radius(front["R"], back["R"])
    identity = canonical.casefold() == front["material"].casefold()
    mfe0 = _tl.mfe_snapshot(system)
    m0 = _merit_count(system)
    undo = []
    _tl._restore_thickness(system, at - 1, t_parent, "split.front_t", undo)
    try:
        try:
            _tl._insert_with_undo(system, at, n + 1, undo, "split.insert")
        except _tl.LayoutError as exc:   # the bare insert_surface family for a count that did not grow
            raise SurfaceWriteError(str(exc), field="count", intended=n + 1, actual=int(lde.NumberOfSurfaces),
                                    surface=at) from exc
        _write(lde, at, "Radius", r_int)
        _write(lde, at, "Thickness", t_half)
        _write(lde, at - 1, "Thickness", t_half)
        glass_args = {"surface": at, "glass": canonical, "auto_load": False}
        if identity and front["catalog"]:
            glass_args["catalog"] = front["catalog"]
        back_glass = _lg.substitute_glass(session, glass_args)
        live = _read_rows(system, lde, (at - 1, at, at + 1))
        t_front, t_rear = live[0]["t"], live[1]["t"]
        bad = _check_domain([t_front, t_rear], [(r["R"], r["semi"]) for r in live])
        if bad:
            _refuse("geometry_unreadable", "after the split: %s" % bad)
        edges = split_edges(live, t_front, t_rear)
        if not (_edge_ok(edges[0]) and _edge_ok(edges[1])):
            _refuse("edge_non_positive", "the split would leave a non-positive edge: front part %.6f, rear part "
                    "%.6f (interface radius %r)" % (edges[0], edges[1], _io.safe_float(live[1]["R"])))
    except BaseException as exc:                      # KeyboardInterrupt travels after the bookkeeping
        _split_rollback(system, undo, mfe0, found, exc)
        if isinstance(exc, HarnessError):
            _en.raise_with_note(exc, _ROLLED_BACK)
        raise
    _invalidate(session)
    m1 = _merit_count(system)
    interface = live[1]
    return {"at": at, "count": int(lde.NumberOfSurfaces), "split": {
        "front": at - 1, "interface": at, "back": at + 1,
        "glass": {"front": {"material": front["material"], "catalog": front["catalog"]}, "back": back_glass},
        "interface_radius": None if math.isinf(interface["R"]) else interface["R"],
        "thickness": [t_front, t_rear], "interface_semi": interface["semi"],
        "interface_semi_solve": interface["semi_solve"], "edges": list(edges),
        "edges_source": _EDGES_SOURCE_SPLIT, "merit_rows": {"before": m0, "after": m1}}}


def _split_rollback(system, undo, mfe0, found, cause):
    """Undo, then PROVE the lens equals ``found``; raise ``SurfaceWriteError`` unless it is proven."""
    from . import _tolerance_layout as _tl  # deferred (h4), as in ``split``
    stage = "split.undo"
    try:
        ok, failed, _ = _tl.run_undo(undo)
        if not ok:
            raise SurfaceWriteError("ROLLBACK FAILED at %s; lens state unknown; reload your design" % failed,
                                    field="rollback", intended=None, actual=failed)
        stage = "split.merit"
        restored = _tl.reimpose_mfe(system, mfe0)
        if not restored["restored"]:
            raise SurfaceWriteError("ROLLBACK FAILED at split.merit (%s); lens state unknown; reload your design"
                                    % restored.get("reason"), field="rollback", intended=None, actual="split.merit")
        stage = "split.proof"
        verdict = _tl.diff_allowed(found, _tl.state_with_mce(system, allow_no_variables=True), allow_dofs=False)
        if not verdict["pass"]:
            raise SurfaceWriteError("ROLLBACK FAILED at split.proof; first differences: %r; lens state unknown; "
                                    "reload your design" % (verdict["first_differences"][:3],),
                                    field="rollback", intended=None, actual="split.proof")
    except SurfaceWriteError as exc:
        raise exc from cause
    except Exception as proof_exc:  # noqa: BLE001 -- StateUnreadable, an MFE read fault, anything
        raise SurfaceWriteError("ROLLBACK FAILED at %s (%r); lens state unknown; reload your design"
                                % (stage, proof_exc), field="rollback", intended=None, actual=stage) from cause


# --------------------------------------------------------------------------- #
# cement
# --------------------------------------------------------------------------- #
def cement(session, params):
    """``remove_surface(at, cement)``: close the air gap ``at`` between two glass elements (spec 1.2, 2.2, 5.2)."""
    from . import _tolerance_layout as _tl  # deferred (h4): module level drags in _lens_state
    rule = params.get("cement")
    if rule not in ("front", "rear") or not isinstance(rule, str):                            # C1
        raise ToolParamError("cement must be exactly 'front' or 'rear', got %r" % (rule,))
    refuse_strict = _c._require_bool_param(params, "refuse_on_solve_refs")                    # C2 (zero reads)
    refuse_unaudited = _c._require_bool_param(params, "refuse_on_unaudited")                  # C2 (zero reads)
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    at = _c._require_int_index(params, "at")
    _c._require_remove_at(at, n)                                                              # C3
    if at + 2 > n - 2:
        raise ToolParamError("cement needs a surface after the rear element that is not the image: at+2=%d "
                             "must be <= %d" % (at + 2, n - 2))
    _require_one_config(system)                                                               # C4
    rows = _read_rows(system, lde, (at - 1, at, at + 1, at + 2))
    if not (_is_glass(rows[0]) and rows[1]["air"] and _is_glass(rows[2])):                    # C5
        _refuse("not_an_air_gap_between_elements", "surface %d must be an air gap between glass elements "
                "%d and %d" % (at, at - 1, at + 1))
    if _not_standard(rows) or rows[1]["conic"] != 0 or rows[2]["conic"] != 0:                 # C6
        _refuse("surface_type_not_modelled", "surfaces %d..%d must be Standard and the facing faces %d and %d "
                "must have conic 0" % (at - 1, at + 2, at, at + 1))
    bad = _check_domain([rows[0]["t"], rows[2]["t"]],                                         # C6b
                        [(rows[0]["R"], rows[0]["semi"]), (rows[1]["R"], rows[2]["semi"]),
                         (rows[2]["R"], rows[2]["semi"]), (rows[3]["R"], rows[3]["semi"])])
    if bad:
        _refuse("geometry_unreadable", "surfaces %d..%d: %s" % (at - 1, at + 2, bad))
    if rule == "front":                                                                       # C7
        probe = _sc.refuse_if_driven(system, lde, at + 1, "radius")
        if probe.get("driven") is not False:
            raise SolveDrivenError.from_probe(probe, cell_token="radius", surface=at + 1,
                                              tool="remove_surface", intended=rows[1]["R"])
    both = {r: predict_cement_edges(rows, r) for r in ("rear", "front")}                      # C8
    edges = both[rule]
    if not (_edge_ok(edges[0]) and _edge_ok(edges[1])):
        _refuse("edge_non_positive", "cement=%r would leave a non-positive edge. Predicted edges (front "
                "element, rear element): rule rear (%.6f, %.6f), rule front (%.6f, %.6f)"
                % (rule, *both["rear"], *both["front"]))
    from . import lens_surface
    m0 = _merit_count(system)
    kept = rows[2]["R"] if rule == "rear" else rows[1]["R"]
    undo = []
    try:
        if rule == "front":
            old = rows[2]["R"]
            undo.append(("cement.front_radius", lambda: _restore_radius(lde, at + 1, old)))
            _write(lde, at + 1, "Radius", rows[1]["R"])
        out = lens_surface.remove_surface(session, {"at": at, "refuse_on_solve_refs": refuse_strict,
                                                    "refuse_on_unaudited": refuse_unaudited})
    except BaseException as exc:                      # KeyboardInterrupt travels after the bookkeeping
        try:
            count = int(lde.NumberOfSurfaces)
        except Exception as read_exc:  # noqa: BLE001
            raise SurfaceWriteError("unknown state: the surface count could not be read after a failure (%r)"
                                    % (read_exc,), field="count", intended=n, actual=None, surface=at) from exc
        if count == n:                  # the row is still there: undo the radius write, proven
            ok, failed, _ = _tl.run_undo(undo)
            if not ok:
                raise SurfaceWriteError("ROLLBACK FAILED at %s; lens state unknown; reload your design" % failed,
                                        field="rollback", intended=None, actual=failed) from exc
            raise
        if count == n - 1:              # the row is gone: a write now would hit the wrong row
            raise
        raise SurfaceWriteError("unknown state, count %d (expected %d or %d)" % (count, n, n - 1),
                                field="count", intended=n - 1, actual=count, surface=at) from exc
    _invalidate(session)
    m1 = _merit_count(system)
    note = _EDGES_SOURCE_CEMENT + ("; interface semi is Automatic and may move"
                                   if rows[2]["semi_solve"] == "Automatic" else "")
    return {**out, "cement": {
        "kept": rule, "interface": at, "radius": _io.safe_float(kept), "removed_gap": _io.safe_float(rows[1]["t"]),
        "interface_semi": rows[2]["semi"], "interface_semi_solve": rows[2]["semi_solve"],
        "edges": list(edges), "edges_source": note, "merit_rows": {"before": m0, "after": m1}}}


def _restore_radius(lde, index, value):
    """Write ``value`` back on ``index`` and raise unless it reads back (the cement undo)."""
    lde.GetSurfaceAt(index).Radius = value
    actual = float(lde.GetSurfaceAt(index).Radius)
    if not _c._readback_ok(value, actual):
        raise SurfaceWriteError("cement.front_radius did not restore: intended=%r actual=%r" % (value, actual),
                                field="radius", intended=value, actual=actual, surface=index)
