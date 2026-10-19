"""tools/_fold_gaps.py -- the per-gap audit of a FOLDED system (NOT dispatchable).

Pure-Python leaf module (``ticket-folded-gap-audit.md``). On a fold
the raw LDE thickness is the fold direction, so each adjacent gap is measured between
consecutive BOUNDING surfaces (coordinate-break rows and flat air-to-air dummies
collapsed): centre = sum(t) x reflection parity, edge = parity x the coaxial formula,
measured only when both bounding surfaces share a frame, and cross-checked per gap against
a batch ray trace (trace vs sag model + frames, FAIL CLOSED). Every adjacent pair that could
not be measured is listed under ``fold_audit.not_measured`` with ONE frozen reason token.

Measuredness is SELF-REPORTED by the evaluator that computed the number (``fold_sag_eval``,
``fold_centre_eval``, ``fold_edge_eval``); no parallel guard re-derives a condition the
executor already knows. Parity itself is the MIRROR-material rule and is NOT verified
by the ray step (the step carries the sign of ``t + dsag``). Non-adjacent collisions are
out of scope. Imports no ``clearance`` / ``workspace`` / ``optimize_run``.
"""
import math
import os
import time
from collections import namedtuple

from ..enums import _resolve_enum
from . import _asphere_cells as _asph
from . import _clearance_common as _cl
from . import _grin_cells as _grin
from . import _layout_geometry as _geom
from . import _layout_rays
from .analysis_raytrace import _opd_mode_member, _rays_type_enum

FOLD_AUDIT_VERSION = 1
FOLD_METHOD = "mirror_parity_shared_frame_ray_checked"
FOLD_PARITY_BASIS = "mirror_material_count"          # what parity rests on; NOT ray-verified
FOLD_SCOPE = "adjacent_gaps_only"
FOLD_OUT_OF_SCOPE = ("non_adjacent_collision", "beam_obstruction")
# The non-adjacent scope is tracked in the private ticket register; no ticket ID is ever served
# (the public release scrubs them -- owner).
ORACLE_VERIFIES = ("trace_vs_sag_model", "trace_vs_frames_thickness")

TOL_R = 1e-9
TOL_PERP = 1e-4
TOL_V = 1e-6
TOL_SAG = 1e-6
TOL_CENTRE = lambda sum_t: 2 * TOL_SAG + TOL_V * max(1.0, abs(sum_t))  # noqa: E731
OFF_AXIS_PUPIL = lambda spec: (spec[3], spec[4]) != (0.0, 0.0)          # noqa: E731
FOLD_ORACLE_BUDGET_S_DEFAULT = 12.0

FOLD_RAYS = (                              # (wave, Hx, Hy, Px, Py) -- ONE batch per bounding surface
    (1, 0.0, 0.0, 0.0, 0.0),
    (1, 0.0, 0.0, 0.0, 0.5), (1, 0.0, 0.0, 0.0, -0.5), (1, 0.0, 0.0, 0.5, 0.0),
    (1, 0.0, 0.0, 0.0, 0.9), (1, 0.0, 0.0, 0.0, -0.9),
    (1, 0.0, 0.7, 0.0, 0.0), (1, 0.0, -0.7, 0.0, 0.0),
)

FOLD_NOT_MEASURED_REASONS = frozenset({
    "parity_unknown", "bounding_row_unreadable", "geometry_cell_unreadable", "medium_unknown",
    "surface_type_not_modelled", "frame_unreadable", "frame_rotated", "frame_lateral_offset",
    "vertex_thickness_mismatch", "no_traceable_ray", "no_off_axis_ray", "ray_trace_unavailable",
    "ray_budget_exhausted", "ray_model_disagreement", "fold_signal_inconsistent", "gap_audit_failed",
    "fold_row_disagreement", "edge_height_unreadable", "edge_inputs_unreadable",
    "edge_model_domain_invalid",
})
CENTRE_ONLY_REASONS = frozenset({"surface_type_not_modelled", "edge_inputs_unreadable",
                                 "edge_height_unreadable", "edge_model_domain_invalid"})
RECORD_EDGE_REASONS = CENTRE_ONLY_REASONS | frozenset({"gap_audit_failed"})
FOLD_AUDIT_STATUS = ("complete", "partial", "not_run")
FOLD_AUDIT_REASONS = (None, "fold_signal_inconsistent", "too_few_surfaces", "audit_failed")

FoldMark = namedtuple("FoldMark", "ok is_cb is_mirror is_air")
SagEval = namedtuple("SagEval", "value inputs_valid domain_valid reason")
FoldResult = namedtuple("FoldResult", "gaps violations fold_audit flags")
PairResult = namedtuple("PairResult", "record not_measured violation flags")


class MediumWalk:
    """Per-row medium / cumulative-parity state (section 3.2)."""

    def __init__(self, n):
        self.medium_after = ["unknown"] * n
        self.parity_after = [1] * n
        self.parity_known_after = [True] * n
        self.barrier_reason = [None] * n
        self.cb_glass = [False] * n
        self.saw_fold_row = False


def _tname(row):
    """The row's ``type_name`` as an EXACT ``str`` (``""`` otherwise) -- no ``str()`` dispatch on a cell."""
    tn = row.get("type_name")
    return tn if type(tn) is str else ""


def _mname(row):
    mn = row.get("material")
    return mn if type(mn) is str else ""


def _finite_num(v):
    """LOCAL copy of ``workspace._finite_reading``'s body (no workspace import)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return math.isfinite(v)


def ray_budget_s():
    """``OPTIVIBE_RAY_BUDGET_S`` with the positive-finite clamp (nan/inf/<=0/junk -> default)."""
    raw = os.environ.get("OPTIVIBE_RAY_BUDGET_S")
    if raw is None:
        return FOLD_ORACLE_BUDGET_S_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return FOLD_ORACLE_BUDGET_S_DEFAULT
    if not math.isfinite(value) or value <= 0:
        return FOLD_ORACLE_BUDGET_S_DEFAULT
    return value


# --------------------------------------------------------------------------- #
# Row classes, the barrier predicate, the medium / parity walk, the pairing.
# --------------------------------------------------------------------------- #
def read_fold_marks(lde, n):
    """D1: an INDEPENDENT per-row re-read of Type/Material -> ``[FoldMark(ok, is_cb, is_mirror, is_air)]``."""
    marks = []
    for i in range(n):
        try:
            row = lde.GetSurfaceAt(i)
            t, m = str(row.Type), str(row.Material)
            marks.append(FoldMark(True, _geom._is_coordinate_break(t), _geom._is_mirror(m),
                                  _geom._is_air_material(m)))
        except Exception:  # noqa: BLE001 -- one wedged row is an unreadable mark
            marks.append(FoldMark(False, False, False, False))
    return marks


def _row_class(row):
    role = row.get("role")
    return "cb" if role == "coordinate-break" else ("mirror" if role == "mirror" else "other")


def _mark_class(mark):
    return "cb" if mark.is_cb else ("mirror" if mark.is_mirror else "other")


def rows_have_fold(rows):
    """D1: any readable INTERIOR row classifies cb or mirror (the role stamps object/image first)."""
    return any(r.get("ok", True) and _row_class(r) != "other" for r in rows)


def _air_disagrees(row, mark):
    """The geometry row and the independent re-read disagree on glass-vs-air."""
    return _geom._is_air_material(_mname(row)) != mark.is_air


def barrier(rows, marks, k):
    """THE ONE barrier predicate -> ``(is_barrier, reason)``."""
    if not rows[k].get("ok", True) or k >= len(marks) or not marks[k].ok:
        return True, "bounding_row_unreadable"
    if _row_class(rows[k]) != _mark_class(marks[k]):
        return True, "fold_row_disagreement"
    if _air_disagrees(rows[k], marks[k]):                              # ADV-1: glass-vs-air disagreement
        return True, "fold_row_disagreement"
    return False, None


def medium_walk(rows, n, marks):
    """Section 3.2: medium_after[k], parity_after[k], parity_known_after[k], barrier_reason[k]."""
    w = MediumWalk(n)
    r0 = rows[0]
    if not r0.get("ok", True) or not marks or not marks[0].ok or _air_disagrees(r0, marks[0]):
        medium = "unknown"            # a degraded / unconfirmed row 0 is never "air"
    else:
        medium = "air" if _geom._is_air_material(_mname(r0)) else "glass"
    parity, known = 1, True
    w.medium_after[0] = medium
    for k in range(1, n - 1):
        is_b, why = barrier(rows, marks, k)
        row = rows[k]
        if is_b:
            medium, known = "unknown", False
            w.barrier_reason[k] = why
        elif _row_class(row) == "cb":
            w.saw_fold_row = True
            w.cb_glass[k] = medium == "glass"
        elif _row_class(row) == "mirror":
            w.saw_fold_row = True
            parity = -parity
        else:
            tn = _tname(row)
            if _grin.grin_type_of_name(tn) is not None:
                medium = "glass"
            elif _grin.grin_family_type_of_name(tn) is not None:
                medium = "unknown"
            elif _geom._is_air_material(_mname(row)):
                medium = "air"
            else:
                medium = "glass"
        w.medium_after[k], w.parity_after[k], w.parity_known_after[k] = medium, parity, known
    w.medium_after[n - 1], w.parity_after[n - 1], w.parity_known_after[n - 1] = medium, parity, known
    is_b, why = barrier(rows, marks, n - 1)                  # EXT-1: the image row is judged too
    if is_b:
        w.barrier_reason[n - 1] = why
    return w


def _is_dummy(rows, walk, k):
    """Flat air->air ``Standard`` row (section 3.1); a NaN radius is an UNREADABLE cell, never a plane."""
    row = rows[k]
    R, c, t = row.get("radius"), row.get("conic"), row.get("thickness")
    if _grin._exact_token_match(_tname(row), ("Standard",)) is None:
        return False
    if not (isinstance(R, float) and (math.isinf(R) or R == 0.0)):
        return False
    if not (_finite_num(c) and _finite_num(t)):
        return False
    return walk.medium_after[k - 1] == "air" and walk.medium_after[k] == "air"


def bounding_surfaces(rows, n, walk):
    """Section 3.1 / 3.3 -> ``(leading, bounding)``; a barrier row is BOUNDING, never collapsed."""
    bounding, leading, seen = [], [], False
    for k in range(1, n - 1):
        cls = _row_class(rows[k])
        collapsed = (walk.barrier_reason[k] is None and
                     (cls == "cb" or (cls == "other" and k <= n - 2 and _is_dummy(rows, walk, k))))
        if collapsed:
            if not seen:
                leading.append(k)
            continue
        seen = True
        bounding.append(k)
    bounding.append(n - 1)
    return leading, bounding


def fold_medium_kind(walk, i):
    """``"air"`` / ``"glass"`` / ``None`` (unknown) -- the informational ``folded_gaps`` kind."""
    m = walk.medium_after[i] if 0 <= i < len(walk.medium_after) else "unknown"
    return m if m in ("air", "glass") else None


# --------------------------------------------------------------------------- #
# Frames.
# --------------------------------------------------------------------------- #
def frame_relation(fr_i, fr_j):
    """Section 4 -> ``(token, along, perp)``; ``token`` is "shared" or a frame reason."""
    if not (fr_i.get("ok") and fr_j.get("ok")):
        return "frame_unreadable", None, None
    Ri, Rj = fr_i["R"], fr_j["R"]
    if max(abs(a - b) for a, b in zip(Ri, Rj)) > TOL_R:
        return "frame_rotated", None, None
    d = [b - a for a, b in zip(fr_i["vertex"], fr_j["vertex"])]
    z = (Ri[2], Ri[5], Ri[8])
    along = sum(a * b for a, b in zip(d, z))
    perp = math.sqrt(sum((a - along * b) ** 2 for a, b in zip(d, z)))
    if perp > TOL_PERP:
        return "frame_lateral_offset", along, perp
    return "shared", along, perp


# --------------------------------------------------------------------------- #
# The three EVALUATORS (measuredness is self-reported).
# --------------------------------------------------------------------------- #
def _planar_or_finite(R):
    return isinstance(R, float) and not math.isnan(R)


def _inputs_reason(row):
    """Height-INDEPENDENT validity of every input the executor will consume (None = valid)."""
    if _geom.sag_model_is_faithful(row.get("type_name")) is not True:
        return "surface_type_not_modelled"
    R, k = row.get("radius"), row.get("conic")
    if not _planar_or_finite(R) or not _finite_num(k):
        return "edge_inputs_unreadable"
    coeffs, norm, power = row.get("aspheric_coefficients"), row.get("asphere_norm_radius"), row.get("asphere_power")
    resolved = _asph.asphere_type_of_name(_tname(row))
    if row.get("coefficients_unreadable"):
        return "edge_inputs_unreadable"
    if coeffs:                                                       # exactly the executor's own test
        if not isinstance(coeffs, (list, tuple)):
            return "edge_inputs_unreadable"
        try:
            if not all(math.isfinite(float(a)) for a in coeffs):
                return "edge_inputs_unreadable"
        except (TypeError, ValueError):
            return "edge_inputs_unreadable"
        if norm is not None and not (_finite_num(norm) and norm > 0):
            return "edge_inputs_unreadable"
        if power is not None and not callable(power):
            return "edge_inputs_unreadable"
    if (resolved is not None) != bool(coeffs):                      # p2 metadata consistency
        return "edge_inputs_unreadable"
    if resolved is not None:                                         # p3 consumption consistent
        info = _asph.ASPHERE_TYPE_INFO[resolved]
        if not (power == info.power):                                # EQUALITY, never identity
            return "edge_inputs_unreadable"
        n_c = len(coeffs)
        if (n_c != info.max_terms) if info.gate_par is None else not (1 <= n_c <= info.max_terms):
            return "edge_inputs_unreadable"
        if info.norm_par is not None:
            if not (_finite_num(norm) and norm > 0):
                return "edge_inputs_unreadable"
        elif norm is not None:
            return "edge_inputs_unreadable"
    return None


def fold_sag_eval(row, rho):
    """``SagEval(value, inputs_valid, domain_valid, reason)``; NEVER raises."""
    try:
        reason = _inputs_reason(row)
        if reason is not None:
            return SagEval(None, False, False, reason)
        if not (_finite_num(rho) and rho >= 0.0):
            return SagEval(None, True, False, "edge_model_domain_invalid")
        R, k = row["radius"], row["conic"]
        if math.isfinite(R) and R != 0.0 and 1.0 - (1.0 + k) * rho * rho / (R * R) < 0.0:
            return SagEval(None, True, False, "edge_model_domain_invalid")
        import numpy as np
        coeffs = row.get("aspheric_coefficients") or None
        resolved = _asph.asphere_type_of_name(_tname(row))
        power = _asph.ASPHERE_TYPE_INFO[resolved].power if resolved is not None else row.get("asphere_power")
        z, _valid = _geom.sag_profile(R, k, np.array([float(rho)]), coeffs,
                                      norm_radius=row.get("asphere_norm_radius"), power=power)
        v = float(z[0])
        if not math.isfinite(v):
            return SagEval(None, True, False, "edge_model_domain_invalid")
        return SagEval(v, True, True, None)
    except Exception:  # noqa: BLE001 -- the evaluator never raises; an unevaluable sag is invalid
        return SagEval(None, False, False, "edge_inputs_unreadable")


def fold_centre_eval(rows, i, j, span, parity, parity_known):
    """``(value, valid, reason)``: centre = parity x fsum(t); the geometry cells must be characterised."""
    if not parity_known:
        return None, False, "parity_unknown"
    if not all(_finite_num(rows[r].get("thickness")) for r in span):
        return None, False, "geometry_cell_unreadable"
    for r in (i, j):
        if not _planar_or_finite(rows[r].get("radius")) or not _finite_num(rows[r].get("conic")):
            return None, False, "geometry_cell_unreadable"
    return parity * math.fsum(rows[r]["thickness"] for r in span), True, None


def _edge_h(semi_i, semi_j):
    if _cl.controlling_height_source(semi_i, semi_j) not in ("surface_i", "surface_ip1"):
        return None
    return _cl.controlling_height(semi_i, semi_j)


def fold_edge_eval(sum_t, parity, row_i, row_j, semi_i, semi_j):
    """``(value, valid, reason, h)``; the edge is parity x (sum_t + sag_j - sag_i) at h (no h, no edge)."""
    h = _edge_h(semi_i, semi_j)
    if h is None:
        return None, False, "edge_height_unreadable", None
    ei, ej = fold_sag_eval(row_i, h), fold_sag_eval(row_j, h)
    for ev in (ei, ej):
        if not (ev.inputs_valid and ev.domain_valid):
            return None, False, ev.reason, h
    return parity * (sum_t + ej.value - ei.value), True, None, h


# --------------------------------------------------------------------------- #
# The ray-step oracle.
# --------------------------------------------------------------------------- #
def trace_bounding(system, bounding, deadline):
    """ONE batch per bounding surface -> ``(traced, n_opens, exhausted, unavailable)`` (section 5.2)."""
    try:
        rays_real = _resolve_enum(_rays_type_enum(system), "Real")
        opd_none = _opd_mode_member(system, "None")
    except Exception:  # noqa: BLE001 -- unresolvable enums -> every pair ray_trace_unavailable
        return {}, 0, False, True
    traced, opens = {}, 0
    for k in bounding:
        if deadline is not None and time.perf_counter() >= deadline:
            return traced, opens, True, False
        opens += 1
        traced[k] = _layout_rays._trace_surface_all_rays(system, rays_real, opd_none, k, FOLD_RAYS)
    return traced, opens, False, False


def _oracle(i, j, rows, parity, sum_t, along, traced, unavailable, exhausted, h):
    """Section 5.3 -> ``(reason, oracle, min_ray_gap, crossing)``."""
    if unavailable:
        return "ray_trace_unavailable", None, None, None
    ti, tj = traced.get(i), traced.get(j)
    if (i not in traced or j not in traced) and exhausted:
        return "ray_budget_exhausted", None, None, None
    if ti is None or tj is None:
        return "no_traceable_ray", None, None, None
    used = off = vig = 0
    sag_res = cen_res = 0.0
    gaps, fracs, bad = [], [], False
    for r, spec in enumerate(FOLD_RAYS):
        a, b = ti[r], tj[r]
        if a is None or b is None or a[0] != 0 or b[0] != 0:
            continue
        ei = fold_sag_eval(rows[i], math.hypot(a[2], a[3]))
        ej = fold_sag_eval(rows[j], math.hypot(b[2], b[3]))
        if not (ei.domain_valid and ej.domain_valid):
            return "ray_model_disagreement", None, None, None
        used += 1
        off += 1 if OFF_AXIS_PUPIL(spec) else 0
        vig += 1 if (a[1] != 0 or b[1] != 0) else 0
        ri_, rj_ = abs(a[4] - ei.value), abs(b[4] - ej.value)
        c_r = parity * (along + b[4] - a[4] - (ej.value - ei.value))
        c_res = abs(c_r - parity * sum_t)
        sag_res, cen_res = max(sag_res, ri_, rj_), max(cen_res, c_res)
        if ri_ > TOL_SAG or rj_ > TOL_SAG or c_res > TOL_CENTRE(sum_t):
            bad = True
        gaps.append((parity * (along + b[4] - a[4]), spec, math.hypot(a[2], a[3]), math.hypot(b[2], b[3])))
        if h is not None:
            fracs.append(min(math.hypot(a[2], a[3]), math.hypot(b[2], b[3])) / h)
    if used == 0:
        return "no_traceable_ray", None, None, None
    if bad:
        return "ray_model_disagreement", None, None, None
    if off == 0:
        return "no_off_axis_ray", None, None, None
    oracle = {"rays_used": used, "off_axis_rays_used": off, "rays_vignetted": vig,
              "max_sag_residual": sag_res, "max_center_residual": cen_res,
              "max_rho_fraction": max(fracs) if (h is not None and fracs) else None}
    g, spec, rho_i, rho_j = min(gaps, key=lambda t: t[0])
    crossing = None
    if g < 0:
        ray = [spec[3], spec[4]] if (spec[3], spec[4]) != (0.0, 0.0) else [spec[1], spec[2]]
        crossing = {"ray": ray, "rho_i": rho_i, "rho_j": rho_j, "min_ray_gap": g}
    return None, oracle, g, crossing


# --------------------------------------------------------------------------- #
# One pair.
# --------------------------------------------------------------------------- #
def _nm(i, j, collapsed, kind, reason, centre=None, extra=None):
    e = {"surface": i, "next_surface": j, "collapsed_surfaces": collapsed, "kind": kind,
         "reason": reason, "center_measured": centre is not None, "center_thickness": centre,
         "path_length_unverified": None, "vertex_distance": None}
    if extra:
        e.update(extra)
    return e


def evaluate_pair(i, j, rows, walk, frames, traced, min_air, min_glass, *, n=None,
                  unavailable=False, exhausted=False, fold_inconsistent=False):
    """-> ``PairResult``; a throw is caught by the pair's OWN ``try``."""
    n = n if n is not None else len(rows)
    collapsed = list(range(i + 1, j))
    kind = fold_medium_kind(walk, i)
    ctx = {"centre": None, "orc": None}
    try:
        return _evaluate(i, j, rows, walk, frames, traced, min_air, min_glass, n, unavailable,
                         exhausted, fold_inconsistent, collapsed, kind, ctx)
    except Exception:  # noqa: BLE001 -- the exception-preserved state
        extra = {"path_length_unverified": ctx["info"]} if ctx.get("info") is not None else None
        bare = PairResult(None, _nm(i, j, collapsed, kind, "gap_audit_failed", extra=extra), None,
                          [f"fold gap S{i}->S{j} not measured: gap_audit_failed"])
        if ctx["centre"] is None:                  # ADV-3: no shared frame proven -> no verdict-driving centre
            return bare
        try:
            return _centre_only(i, j, rows, collapsed, kind, "gap_audit_failed", ctx, min_air,
                                min_glass, n)
        except Exception:  # noqa: BLE001 -- ADV-2: the handler itself never throws out of the audit
            return bare


def _centre_only(i, j, rows, collapsed, kind, reason, ctx, min_air, min_glass, n):
    """A CENTRE-ONLY record + its ``not_measured`` twin (section 3.5); the centre survives."""
    orc = ctx["orc"]
    oracle, g, crossing = (orc[1], orc[2], orc[3]) if orc is not None else (None, None, None)
    rec, ce, worst = _record(i, j, rows, collapsed, kind, ctx["parity"], ctx["centre"], None, None,
                             False, reason, oracle, g, crossing, min_air, min_glass, n)
    flags = [f"fold gap S{i}->S{j} edge not measured: {reason}"]
    if _cl.controlling_height_source(rows[i]["semi_diameter"], rows[j]["semi_diameter"]) == "substituted_single_finite":
        flags.append(f"fold gap S{i}->S{j}: only one bounding semi-diameter is finite-positive; the fold "
                     "never substitutes a semi, so the edge is not measured")
    return PairResult(rec, _nm(i, j, collapsed, kind, reason, centre=ctx["centre"]),
                      _violation_entry(rec, ce, worst), flags)


def _record(i, j, rows, collapsed, kind, parity, centre, edge, h, edge_valid, reason, oracle,
            min_ray_gap, crossing, min_air, min_glass, n):
    threshold = min_glass if kind == "glass" else min_air
    finite = [v for v in (centre, edge) if v is not None and math.isfinite(v)]
    worst = min(finite) if finite else None
    ce = worst is not None and worst < threshold
    ray = min_ray_gap is not None and min_ray_gap < 0
    fz = [s for s in (i, j) if rows[s].get("semi_solve") == "Fixed"]
    src = _cl.controlling_height_source(rows[i]["semi_diameter"], rows[j]["semi_diameter"])
    rec = {
        "surface": i, "next_surface": j, "kind": kind,
        "center_thickness": centre, "edge_thickness": edge,
        "edge_height": h if edge_valid else None,
        "edge_height_source": src if edge_valid else None,
        "edge_approximate": False, "threshold": threshold, "violation": bool(ce or ray),
        "is_back_airgap": j == n - 1, "frozen_semi": bool(fz), "frozen_surfaces": fz,
        "collapsed_surfaces": collapsed, "parity": parity, "frame": "shared",
        "ends_at_mirror": rows[j].get("role") == "mirror", "oracle": oracle,
        "min_ray_gap": min_ray_gap, "crossing": crossing,
        "center_valid": True, "edge_valid": bool(edge_valid),
    }
    if not edge_valid:
        rec["edge_not_measured_reason"] = reason
    return rec, bool(ce), worst


def _violation_entry(rec, ce, worst):
    """Entry invariant: ``worst < threshold``; ``worst`` is the value of the disjunct that FIRED."""
    if not rec["violation"]:
        return None
    basis, w = ("center_edge", worst) if ce else ("ray_crossing", rec["min_ray_gap"])
    return {"surface": rec["surface"], "next_surface": rec["next_surface"], "kind": rec["kind"],
            "center_thickness": rec["center_thickness"], "edge_thickness": rec["edge_thickness"],
            "worst": w, "threshold": rec["threshold"], "is_back_airgap": rec["is_back_airgap"],
            "basis": basis, "min_ray_gap": rec["min_ray_gap"]}


def _evaluate(i, j, rows, walk, frames, traced, min_air, min_glass, n, unavailable, exhausted,
              fold_inconsistent, collapsed, kind, ctx):
    span = range(i, j)
    parity = walk.parity_after[i]

    def nm(reason, extra=None):
        return PairResult(None, _nm(i, j, collapsed, kind, reason, extra=extra), None,
                          [f"fold gap S{i}->S{j} not measured: {reason}"])

    if fold_inconsistent:
        return nm("fold_signal_inconsistent")
    for s in (i, j):                                                    # rule 1 [barrier-touch]
        if walk.barrier_reason[s]:
            return nm(walk.barrier_reason[s])
        if not rows[s].get("ok", True):
            return nm("bounding_row_unreadable")
    if not walk.parity_known_after[i]:                                  # rule 2 [parity-propagation]
        return nm("parity_unknown")
    centre, valid, reason = fold_centre_eval(rows, i, j, span, parity, True)   # rule 3
    if not valid:
        return nm(reason)
    sum_t = math.fsum(rows[r]["thickness"] for r in span)
    ctx["info"], ctx["parity"] = parity * sum_t, parity          # informational until the frame is shared (4)
    if kind is None or any(walk.cb_glass[r] for r in collapsed):        # rule 4 [medium]
        return nm("medium_unknown")
    token, along, _perp = frame_relation(frames[i], frames[j])   # rule 5 [frame-test]
    if token != "shared":
        extra = {}
        if token in ("frame_rotated", "frame_lateral_offset"):
            extra = {"path_length_unverified": parity * sum_t,
                     "vertex_distance": math.dist(frames[i]["vertex"], frames[j]["vertex"])}
        return nm(token, extra)
    if abs(along - sum_t) > TOL_V * max(1.0, abs(sum_t)):               # rule 6 [vertex-consistency]
        return nm("vertex_thickness_mismatch")
    ctx["centre"] = centre                                              # shared frame proven (ADV-3)
    for s in (i, j):                                                    # rule 7 [model-applicability]
        ev = fold_sag_eval(rows[s], 0.0)
        if not ev.inputs_valid:
            return _centre_only(i, j, rows, collapsed, kind, ev.reason, ctx, min_air, min_glass, n)
    semi_i, semi_j = rows[i]["semi_diameter"], rows[j]["semi_diameter"]
    h = _edge_h(semi_i, semi_j)
    reason, oracle, g, crossing = _oracle(i, j, rows, parity, sum_t, along, traced, unavailable,   # rule 8
                                          exhausted, h)
    if reason is not None:
        return nm(reason)
    ctx["orc"] = (reason, oracle, g, crossing)
    edge, evalid, ereason, h = fold_edge_eval(sum_t, parity, rows[i], rows[j], semi_i, semi_j)   # rule 9
    if not evalid:
        return _centre_only(i, j, rows, collapsed, kind, ereason, ctx, min_air, min_glass, n)
    rec, ce, worst = _record(i, j, rows, collapsed, kind, parity, centre, edge, h, True, None,   # rule 10
                             oracle, g, crossing, min_air, min_glass, n)
    return PairResult(rec, None, _violation_entry(rec, ce, worst), [])


# --------------------------------------------------------------------------- #
# The audit.
# --------------------------------------------------------------------------- #
def _oracle_block(opens, budget, exhausted):
    return {"wave": 1, "field": [0, 0], "rays": [[s[1], s[2], s[3], s[4]] for s in FOLD_RAYS],
            "verifies": list(ORACLE_VERIFIES), "off_axis_rule": "pupil_input_nonzero",
            "tol_sag": TOL_SAG, "tol_perp": TOL_PERP, "tol_rot": TOL_R, "tol_vertex": TOL_V,
            "n_trace_opens": opens, "budget_s": budget, "budget_exhausted": bool(exhausted)}


def _fa(status, reason, n, leading, bounding, not_measured, oracle):
    return {"version": FOLD_AUDIT_VERSION, "status": status, "audit_reason": reason,
            "method": FOLD_METHOD, "parity_basis": FOLD_PARITY_BASIS, "image_surface": n - 1,
            "leading_collapsed": leading, "bounding_surfaces": bounding,
            "not_measured": not_measured, "oracle": oracle, "scope": FOLD_SCOPE,
            "out_of_scope": list(FOLD_OUT_OF_SCOPE),
            "ceiling_applied": False}


def audit_folded_gaps(system, lde, rows, n, min_air, min_glass, *, frames, marks, fold_inconsistent,
                      slot_latched=False):
    """``FoldResult(gaps, violations, fold_audit, flags)``; the SOLE producer of ``audit_failed``."""
    budget = ray_budget_s()
    try:
        walk = medium_walk(rows, n, marks)
        leading, bounding = bounding_surfaces(rows, n, walk)
        if len(bounding) < 2:
            return FoldResult([], [], _fa("not_run", "too_few_surfaces", n, leading, bounding, [],
                                          _oracle_block(0, budget, False)), [])
        traced, opens, exhausted, unavailable = {}, 0, False, False
        if slot_latched:                     # owner ruling 11: a latched tool slot opens NOTHING
            unavailable = True
        elif not fold_inconsistent:
            traced, opens, exhausted, unavailable = trace_bounding(
                system, bounding, time.perf_counter() + budget)
        gaps, viols, nms, flags = [], [], [], []
        for q in range(len(bounding) - 1):
            pr = evaluate_pair(bounding[q], bounding[q + 1], rows, walk, frames, traced, min_air,
                               min_glass, n=n, unavailable=unavailable, exhausted=exhausted,
                               fold_inconsistent=fold_inconsistent)
            if pr.record is not None:
                gaps.append(pr.record)
            if pr.not_measured is not None:
                nms.append(pr.not_measured)
            if pr.violation is not None:
                viols.append(pr.violation)
            flags.extend(pr.flags)
        if slot_latched:
            flags.append("fold audit: ray trace not attempted (this session's tool slot is latched wedged)")
        if exhausted:
            flags.append("fold audit: ray budget exhausted (OPTIVIBE_RAY_BUDGET_S); remaining gaps not measured")
        complete = not nms and len(gaps) == len(bounding) - 1 and not fold_inconsistent
        fa = _fa("complete" if complete else "partial",
                 "fold_signal_inconsistent" if fold_inconsistent else None, n, leading, bounding,
                 nms, _oracle_block(opens, budget, exhausted))
        return FoldResult(gaps, viols, fa, flags)
    except Exception:  # noqa: BLE001 -- the SOLE producer of audit_failed
        return FoldResult([], [], _fa("not_run", "audit_failed", n, [], [], [],
                                      _oracle_block(0, budget, False)),
                          ["fold audit failed to run: audit_failed"])


# --------------------------------------------------------------------------- #
# Consumer predicates (ONE body shared by workspace and optimize_run).
# --------------------------------------------------------------------------- #
def fold_audit_ran(fa):
    """"ATTEMPTED and the block is well-formed" (section 7); any inconsistency reads False."""
    if not isinstance(fa, dict) or fa.get("status") not in ("complete", "partial"):
        return False
    nm = fa.get("not_measured")
    return (isinstance(nm, list) and (fa["status"] == "complete") == (nm == [])
            and isinstance(fa.get("bounding_surfaces"), list))


def _exact_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def fold_gap_run_complete(gaps, fa):
    """The fold chain check: the records tile the producer's own bounding list."""
    if not isinstance(fa, dict) or not isinstance(gaps, list):
        return False
    b = fa.get("bounding_surfaces")
    if not isinstance(b, list) or len(b) < 2 or not all(_exact_int(x) for x in b):
        return False
    if any(b[k] >= b[k + 1] for k in range(len(b) - 1)) or b[-1] != fa.get("image_surface"):
        return False
    if fa.get("leading_collapsed") != list(range(1, b[0])) or len(gaps) != len(b) - 1:
        return False
    for k, g in enumerate(gaps):
        if not isinstance(g, dict):
            return False
        s, ns = g.get("surface"), g.get("next_surface")
        if not (_exact_int(s) and _exact_int(ns)) or (s, ns) != (b[k], b[k + 1]):
            return False
        if g.get("collapsed_surfaces") != list(range(b[k] + 1, b[k + 1])):
            return False
    return gaps[-1].get("is_back_airgap") is True


def _record_complete(g):
    """Section 7 step 5 (a)-(n): every operand finiteness-guarded -- can never throw."""
    fin = _finite_num
    o = g.get("oracle")
    th = g.get("threshold")
    viol = g.get("violation")
    if not (fin(g.get("center_thickness")) and fin(g.get("edge_thickness"))):             # (a)(b)
        return False
    if g.get("edge_valid") is not True or g.get("center_valid") is not True:               # (c)(d)
        return False
    if "edge_not_measured_reason" in g or g.get("edge_approximate") is not False:          # (e)(f)
        return False
    if not isinstance(viol, bool):                                                         # (g)
        return False
    if not (fin(g.get("edge_height")) and g["edge_height"] > 0):                           # (h)
        return False
    if not isinstance(o, dict):                                                            # (i)
        return False
    ru, ro, rv = o.get("rays_used"), o.get("off_axis_rays_used"), o.get("rays_vignetted")
    if not all(_exact_int(x) for x in (ru, ro, rv)) or not (1 <= ro <= ru and 0 <= rv <= ru):
        return False
    if g.get("kind") not in ("air", "glass") or not fin(th):                               # (j)(k)
        return False
    p = g.get("parity")
    if not (_exact_int(p) and p in (1, -1)) or g.get("frame") != "shared":                 # (l)(m)
        return False
    mrg = g.get("min_ray_gap")
    if viol is False and fin(mrg) and mrg < 0:                                             # (n1)
        return False
    if viol is False and any(fin(v) and v < th for v in (g.get("center_thickness"), g.get("edge_thickness"))):
        return False                                                                       # (n2)
    return True


def fold_coverage_complete(env):
    """COMPLETE MEASURED FOLD-GAP COVERAGE -> ``(bool, token)`` (section 7)."""
    fa = env.get("fold_audit")
    if not fold_audit_ran(fa):
        return False, "not_run"
    if fa["not_measured"]:
        return False, "gap_not_measured"
    gaps = env.get("gaps")
    if not isinstance(gaps, list) or not gaps:
        return False, "no_gaps"
    bfd = env.get("global_bfd")
    img = bfd.get("image_surface") if isinstance(bfd, dict) else None
    if (not fold_gap_run_complete(gaps, fa) or not _exact_int(fa.get("image_surface"))
            or not _exact_int(img) or img != fa["image_surface"]):
        return False, "chain_incomplete"
    if not all(isinstance(g, dict) and _record_complete(g) for g in gaps):
        return False, "record_incomplete"
    return True, None
