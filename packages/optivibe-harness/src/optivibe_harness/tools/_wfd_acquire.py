"""Acquisition for the vector tolerancing gauge (``tolerance(mode="gauge")``): the engine side.

Builds one ``wfd_pack v1`` (the spec of ``gauge-build.md`` as amended) from the lens the caller
loaded. The numeric core (``_wfd_gauge``, pure numpy) reads the pack; nothing here evaluates draws.

PREPARED COPY. The loaded design is prepared IN PLACE and restored with a digest proof, the ``_cb_build`` /
``_cb_teardown`` pattern: one coordinate-break pair per group (TEDX/TEDY/TETX/TETY rows and the push group), then one
per TSTX/TSTY surface, inserted back to front so the original surface numbers stay addressable through ``IndexMap``.
Teardown runs in a ``finally``: ``run_undo`` LIFO, ``remark_variables``, ``reimpose_mfe`` and ``diff_allowed``
(``tolerance_run._cb_teardown`` itself, on a ``_CbRun``). A failed proof refuses ``state_mutated``.

READ ROUTE. The batch route only: ``"CurrentAndChief"`` OPD on 6 rings x 24 arms, projected onto 37 Standard Zernike
terms in numpy. Per state that is F x W x 144 rays and no chief ray (the ``own_drop`` composition reads none).

STATES. nominal; each edit row at its non-zero bounds; the focus column at +-h on the back gap; the push columns at
+-r on Par 1 / Par 2 (decenter) and / or Par 3 / Par 4 (tilt) of the push group's entry CB (2 states per Par). One state at a time, every state restored exactly.

THE ROW GATE (locked 1.7, run on every call). For every edit row above 1 % of the native RSS the gauge's composite
change at each bound is compared with the native sensitivity row; either side off by more than 5 %, or a sign flip,
demotes the row to the fold channel (``c2`` from the native readings). TIRR is always folded. The numeric core's
``evaluate`` refuses ``gauge_fold_too_large`` above 10 %.

Never names a commercial lens-design product other than Zemax OpticStudio.
"""
import hashlib
import math
import time

import numpy as np

from . import _lens_state
from . import _tolerance_common as _tc

T = 37                          # Standard Zernike terms, batch route (P1b: rung 0a 1.68e-3 waves)
RINGS, ARMS = 6, 24             # 6 Gauss-Legendre rings in rho^2 x 24 equispaced arms
OPD_MODE = "CurrentAndChief"    # chief-referenced wavefront error
FOCUS_H = 0.005                 # mm, the focus column half-step (halved once if the column is not linear)
FOCUS_LINEAR_REL = 0.01         # |z(+h) + z(-h) - 2 z0| / |z(+h) - z(-h)| above this halves h once
GATE_SHARE = 0.01               # rows above this share of the native RSS are gated
GATE_REL = 0.05                 # a gated row off by more than this is demoted
FOLD_WARN = 0.05                # folded share above this warns (the numeric core refuses above 10 %)
RSS_REL = 0.02                  # gauge RSS vs native RSS
SYMMETRY_REL = 1e-6             # nominal mirror / rotated RWCE vs the user value
PUSH_CHECK_REL = 0.05           # push column vs the TEDX/TEDY row column: warn above this
RWCE_RING = 6
TFRN_SIGN = -1.0                # P1b: swapped against the native min/max pairing on all three TFRN rows
OFFSET_TOL = 1e-6               # MaterialOffset read-back, relative (the read-back is display-rounded to 7 digits)
TOP_N = 10                      # the top rows by share that may not flip sign

GROUP_CODES = ("TEDX", "TEDY", "TETX", "TETY")
SURFACE_CODES = ("TSTX", "TSTY")
EDIT_CODES = GROUP_CODES + SURFACE_CODES + ("TTHI", "TIND", "TABB", "TRAD", "TFRN")
FOLD_CODES = ("TIRR",)
PARTNERS = {"TSTX": "TSTY", "TEDX": "TEDY", "TETX": "TETY"}
_PAR_OF = {"TEDX": "decenter_x", "TEDY": "decenter_y", "TETX": "tilt_x", "TETY": "tilt_y",
           "TSTX": "tilt_x", "TSTY": "tilt_y"}
# the pack's mirror rule (locked 3.4): M flips the sign of the y-asymmetric rows, identity on the rest
MIRROR_FLIP = ("TSTX", "TEDY", "TETX")

RELOAD_REMEDY = "reload the design with load_design: the prepared copy could not be proven restored"


class Edit:
    """One row edit on the prepared copy: ``apply()`` writes it, ``restore()`` puts every cell back."""

    def __init__(self, apply, restore, failures=None, **info):
        self.apply, self.restore, self.failures, self.info = apply, restore, failures if failures is not None else [], info


class Capped(Exception):
    """``cap_s`` ran out between two engine calls."""


def refusal(family, message, reason=None, **extra):
    out = {"ok": False, "error_family": family, "message": message}
    if reason is not None:
        out["reason"] = reason
    out.update(extra)
    return out


class _Refuse(Exception):
    """An internal refusal; ``acquire_pack`` turns it into a ``refusal`` dict."""

    def __init__(self, family, message, reason=None, **extra):
        super().__init__(message)
        self.family, self.message, self.reason, self.extra = family, message, reason, extra


def _g():
    """The numeric core (lazy: it is a sibling module written in parallel and the tests stub it)."""
    from . import _wfd_gauge
    return _wfd_gauge


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# --------------------------------------------------------------------------- #
# the Standard Zernike set in numpy (orthonormal on the unit disc; Noll ordering)
# --------------------------------------------------------------------------- #
def noll_nm(j):
    """(n, |m|) of Noll term j (1-based): piston 1, tilts 2 and 3, defocus 4; even j is the cosine member."""
    if j < 1:
        raise ValueError("Noll index starts at 1")
    n = 0
    while j > (n + 1) * (n + 2) // 2:
        n += 1
    k = j - n * (n + 1) // 2 - 1
    ms = []
    for m in range(n % 2, n + 1, 2):
        ms += [m] if m == 0 else [m, m]
    return n, ms[k]


def radial(n, m, rho):
    out = np.zeros_like(rho, dtype=float)
    for s in range((n - m) // 2 + 1):
        coef = ((-1) ** s) * math.factorial(n - s) / (
            math.factorial(s) * math.factorial((n + m) // 2 - s) * math.factorial((n - m) // 2 - s))
        out = out + coef * rho ** (n - 2 * s)
    return out


def zernike_standard(j, rho, phi):
    """Standard (orthonormal) term j: sqrt(n+1) R for m = 0, sqrt(2(n+1)) R trig(m phi) otherwise."""
    rho, phi = np.asarray(rho, float), np.asarray(phi, float)
    n, m = noll_nm(j)
    if m == 0:
        return math.sqrt(n + 1) * radial(n, 0, rho)
    trig = np.cos(m * phi) if j % 2 == 0 else np.sin(m * phi)
    return math.sqrt(2 * (n + 1)) * radial(n, m, rho) * trig


def pupil_nodes(rings=RINGS, arms=ARMS):
    """Gauss-Legendre nodes in rho^2 x ``arms`` equispaced arms: (rho, phi, weight); the weights sum to 1."""
    x, w = np.polynomial.legendre.leggauss(rings)
    u, wu = 0.5 * (x + 1.0), 0.5 * w
    return (np.repeat(np.sqrt(u), arms), np.tile(2.0 * np.pi * np.arange(arms) / arms, rings),
            np.repeat(wu, arms) / arms)


def zernike_matrix(terms, rho, phi):
    return np.stack([zernike_standard(j, rho, phi) for j in range(1, terms + 1)], axis=1)


def project_standard(opd, rho, phi, wt, terms):
    """Coefficients c_1..c_T of ``opd`` on the Standard set under the quadrature weights ``wt``."""
    return (zernike_matrix(terms, rho, phi) * (np.asarray(wt)[:, None] * np.asarray(opd)[:, None])).sum(axis=0)


# --------------------------------------------------------------------------- #
# rows: width class, states, TFRN, fold fit, shares (all pure)
# --------------------------------------------------------------------------- #
def width_class(lo, hi):
    """``two_sided | one_sided | fixed | zero | malformed`` of a row's (lo, hi) (locked 3.3)."""
    if not (_finite(lo) and _finite(hi)) or lo > hi:
        return "malformed"
    if lo == hi:
        return "zero" if lo == 0 else "fixed"
    if not (lo <= 0 <= hi):
        return "malformed"
    return "one_sided" if (lo == 0 or hi == 0) else "two_sided"


def state_values(lo, hi, width):
    """``[(side, value)]`` the row acquires: two sides, the one non-zero bound, the fixed value, or nothing."""
    if width == "two_sided":
        return [("lo", lo), ("hi", hi)]
    if width == "one_sided":
        return [("hi", hi)] if lo == 0 else [("lo", lo)]
    if width == "fixed":
        return [("lo", lo)]
    return []


def tfrn_delta_c(fringes, wavelength_um, aperture_radius):
    """Curvature change of N test-plate fringes: dC = N lambda / r^2 (manual 7.2.1.1.8); lambda in um, r in lens units."""
    return fringes * (wavelength_um * 1e-3) / (aperture_radius * aperture_radius)


def tfrn_radius(radius, fringes, wavelength_um, aperture_radius):
    """The radius carrying ``fringes`` fringes at ``aperture_radius``: 1 / (1/R + dC)."""
    return 1.0 / (1.0 / radius + tfrn_delta_c(fringes, wavelength_um, aperture_radius))


def fold_fit(lo, hi, w_lo, w_hi, w0):
    """``{a, b, fit}`` of c2(x) = a x + b x^2 through (lo, w_lo^2 - w0^2), (0, 0), (hi, w_hi^2 - w0^2) (locked 1.6).

    Exactly one bound at 0: linear through the other point (``linear_one_sided``); both 0: no term (``zero_width``);
    a pair that does not bracket 0 raises ``ValueError``. A fixed-width row (lo == hi != 0) is the constant
    ``w^2 - w0^2``: ``{a: 0, b: 0, const, fit: "constant"}``.
    """
    d_lo, d_hi = w_lo * w_lo - w0 * w0, w_hi * w_hi - w0 * w0
    if lo == hi == 0:
        return {"a": 0.0, "b": 0.0, "fit": "zero_width"}
    if lo == hi:
        return {"a": 0.0, "b": 0.0, "const": d_lo, "fit": "constant"}
    if lo > 0 or hi < 0 or lo > hi:
        raise ValueError("the bounds [%r, %r] do not bracket the nominal (0)" % (lo, hi))
    if lo == 0:
        return {"a": d_hi / hi, "b": 0.0, "fit": "linear_one_sided"}
    if hi == 0:
        return {"a": d_lo / lo, "b": 0.0, "fit": "linear_one_sided"}
    det = lo * hi * (hi - lo)
    return {"a": (d_lo * hi * hi - d_hi * lo * lo) / det, "b": (lo * d_hi - hi * d_lo) / det, "fit": "quadratic"}


def row_contribution(row):
    """sqrt((min_change^2 + max_change^2) / 2): the row's share of the native RSS (the hotspots module's c)."""
    lo = (row.get("min") or {}).get("change")
    hi = (row.get("max") or {}).get("change")
    if not (_finite(lo) and _finite(hi)):
        return 0.0
    return math.sqrt((float(lo) ** 2 + float(hi) ** 2) / 2.0)


def classify_rows(rows, nominal):
    """Sensitivity rows -> pack row skeletons, or ``_Refuse``. ``mode`` is edit / fold, ``width`` per locked 3.3."""
    g = _g()
    out, shares_sq = [], []
    for r in rows:
        code = r.get("type")
        ident = (code, r.get("surface"), r.get("surface2"), r.get("param"))
        if r.get("surface") is None or r.get("param_anomalous") or r.get("surface2_anomalous"):
            raise _Refuse("gauge_input", "the sensitivity row %s has an unreadable surface or selector" % (ident,))
        lo, hi = (r.get("min") or {}).get("delta"), (r.get("max") or {}).get("delta")
        width = width_class(lo, hi)
        if width == "malformed":
            raise _Refuse("gauge_input", "the sensitivity row %s has malformed bounds (%r, %r): they must be finite and "
                          "bracket 0" % (ident, lo, hi))
        if code in EDIT_CODES:
            mode = "edit"
            if code in GROUP_CODES and (r.get("surface2") is None or r["surface2"] <= r["surface"]):
                raise _Refuse("gauge_row_unmapped", "%s on surface %s has no element range (surface2 %r): only an "
                              "element or group range can be moved as a pair" % (code, r["surface"], r.get("surface2")))
        elif code in FOLD_CODES:
            mode = "fold"
        else:
            raise _Refuse("gauge_row_unmapped", "no gauge edit for tolerance code %s on surface %s; run it native"
                          % (code, r.get("surface")))
        w_lo = (r.get("min") or {}).get("criterion")
        w_hi = (r.get("max") or {}).get("criterion")
        row = {"key": g.row_key(ident), "code": code, "surface": r["surface"], "surface2": r.get("surface2"),
               "lo": float(lo), "hi": float(hi), "mode": mode, "width": width,
               "native": {"w_lo": w_lo, "w_hi": w_hi, "change_lo": (r.get("min") or {}).get("change"),
                          "change_hi": (r.get("max") or {}).get("change")},
               "c2": None, "gate": {"err_lo": None, "err_hi": None, "verdict": None}}
        out.append(row)
        shares_sq.append(row_contribution(r) ** 2)
    total = math.fsum(shares_sq)
    for row, s in zip(out, shares_sq):
        row["share"] = s / total if total > 0 else 0.0
        row["c2"] = None
    keys = [r["key"] for r in out]
    if len(set(keys)) != len(keys):
        raise _Refuse("gauge_input", "two sensitivity rows share one identity key")
    return out


def attach_folds(rows, nominal):
    """``c2`` for every fold row from its native readings; a row without both criterion readings refuses."""
    for row in rows:
        if row["mode"] != "fold":
            continue
        nat = row["native"]
        if row["width"] == "zero":
            row["c2"] = {"a": 0.0, "b": 0.0, "fit": "zero_width"}
            continue
        w_lo = nat["w_lo"] if _finite(nat["w_lo"]) else None
        w_hi = nat["w_hi"] if _finite(nat["w_hi"]) else None
        if row["width"] in ("one_sided",):
            w_lo = nominal if row["lo"] == 0 else w_lo
            w_hi = nominal if row["hi"] == 0 else w_hi
        if row["width"] == "fixed":
            w_hi = w_lo
        if w_lo is None or w_hi is None:
            raise _Refuse("gauge_input", "the fold row %s has no native criterion reading" % row["key"])
        row["c2"] = fold_fit(row["lo"], row["hi"], float(w_lo), float(w_hi), float(nominal))


def derive_layout(rows, push=None):
    """``{groups, surfaces}``: the group spans (rows plus the push group) and the TSTX/TSTY surfaces; refuses an overlap
    (``gauge_scope``) or a row whose partner is missing (``gauge_not_symmetric``)."""
    groups = {(r["surface"], r["surface2"]) for r in rows if r["code"] in GROUP_CODES}
    if push is not None:
        groups.add((int(push["a"]), int(push["b"])))
    surfaces = {r["surface"] for r in rows if r["code"] in SURFACE_CODES}
    ordered = sorted(groups)
    for g1, g2 in zip(ordered, ordered[1:]):
        if g2[0] <= g1[1]:
            raise _Refuse("gauge_scope", "the element ranges %s and %s overlap; the layout inserts disjoint groups only"
                          % (list(g1), list(g2)), reason="overlapping_groups")
    have = {(r["code"], r["surface"], r["surface2"]) for r in rows}
    missing = sorted("%s|%s|%s" % (x, s, s2) for (x, s, s2) in have if PARTNERS.get(x)
                     and (PARTNERS[x], s, s2) not in have)
    if missing:
        raise _Refuse("gauge_not_symmetric", "the X rows %s have no Y partner on the same surfaces; the composite's "
                      "symmetry map needs both" % missing)
    return {"groups": ordered, "surfaces": sorted(surfaces)}


class IndexMap:
    """Original surface -> current LDE index through the insertions, plus the inserted markers.

    A group pair on current surfaces a..b adds 4 surfaces: entry CB at a, the group at a+1..b+1, dummy back b+2,
    return CB b+3, dummy forward b+4 (everything after b moves by 4). A surface pair at current s adds 2: entry CB
    at s, the surface at s+1, return CB at s+2 (everything after s moves by 2).
    """

    def __init__(self, n_surfaces):
        self.cur = {k: k for k in range(n_surfaces)}
        self.n = n_surfaces

    def group_pair(self, a, b, name):
        for k, c in self.cur.items():
            if a <= c <= b:
                self.cur[k] = c + 1
            elif c > b:
                self.cur[k] = c + 4
        self.cur["g%s_cb" % name], self.cur["g%s_ret" % name], self.cur["g%s_fwd" % name] = a, b + 3, b + 4
        self.n += 4

    def surface_pair(self, s, name):
        for k, c in self.cur.items():
            if c == s:
                self.cur[k] = c + 1
            elif c > s:
                self.cur[k] = c + 2
        self.cur["s%s_cb" % name], self.cur["s%s_ret" % name] = s, s + 2
        self.n += 2

    def thickness_carrier(self, surface, groups, surfaces):
        """The index of the cell that carries the thickness AFTER original ``surface`` in the prepared lens."""
        for a, b in groups:
            if surface == b:
                return self.cur["g%d_%d_fwd" % (a, b)]
        if surface in surfaces:
            return self.cur["s%d_ret" % surface]
        return self.cur[surface]


def push_column_check(row_col, push_col):
    """``||J_row - C_push|| / ||C_push||`` (locked-amendment 4.3); None when the push column is zero or unreadable."""
    if row_col is None or push_col is None:
        return None
    a, b = np.asarray(row_col, float), np.asarray(push_col, float)
    n = float(np.linalg.norm(b))
    if not math.isfinite(n) or n == 0.0 or a.shape != b.shape:
        return None
    return float(np.linalg.norm(a - b) / n)


# the push columns: coordinate-break Par# -> (push_column_check key, the tolerance row whose own column it must match)
PUSH_CHECK = {1: ("x", "TEDX"), 2: ("y", "TEDY"), 3: ("tilt_x", "TETX"), 4: ("tilt_y", "TETY")}
PUSH_PAR_SETS = ((1, 2), (3, 4), (1, 2, 3, 4))      # decenter, tilt, or both on the one group


def push_check_warnings(check):
    code_of = {axis: code for axis, code in PUSH_CHECK.values()}
    return ["push_column_nonlinear: the %s push column differs from the %s row's own column by %.1f %% (> %.0f %%); the "
            "push is read from its own +-r states, so this warns and never refuses" %
            (axis, code_of.get(axis, axis), 100.0 * v, 100.0 * PUSH_CHECK_REL)
            for axis, v in sorted((check or {}).items()) if _finite(v) and v > PUSH_CHECK_REL]


def push_pars(push):
    """``{par: range}`` of the resolved ``cb_group`` (Par 1/2 decenter in lens units, Par 3/4 tilt in degrees), validated;
    ``gauge_input`` otherwise. The pack and the states are built from this one dict."""
    pars = (push or {}).get("pars")
    ok = (isinstance(pars, dict) and tuple(sorted(pars)) in PUSH_PAR_SETS
          and all(_finite(v) and v > 0 for v in pars.values())
          and pars.get(1) == pars.get(2) and pars.get(3) == pars.get(4))
    if not ok:
        raise _Refuse("gauge_input", "the push carries no usable pars (Par 1+2 decenter and/or Par 3+4 tilt, each a pair of "
                      "one positive range); got %r" % (pars,))
    return {int(k): float(v) for k, v in pars.items()}


# --------------------------------------------------------------------------- #
# the engine: one Rig per call
# --------------------------------------------------------------------------- #
class Rig:
    """Every ZOS-API touch of the acquisition. ``session`` is the harness session; ``check`` raises ``Capped``."""

    def __init__(self, session, cap_s=None, clock=time.monotonic):
        from . import _cb_cells as cbc
        from . import _measurement_common as mc
        from . import _solve_cells as sc
        from . import _tolerance_layout as tl
        from . import analysis_measure as am
        from . import surface_solve
        self.session, self.system = session, session.system
        self.cbc, self.mc, self.sc, self.tl, self.am, self.surface_solve = cbc, mc, sc, tl, am, surface_solve
        self.lde = self.system.LDE
        self.cap_s, self.clock, self.t_start = cap_s, clock, clock()
        self.undo, self.touched = [], {}
        self.map = None
        self.groups, self.surfaces = [], []
        self.back = self.t0 = None
        self.fields, self.waves = [], []
        self.nodes = pupil_nodes()
        self.zmat = zernike_matrix(T, self.nodes[0], self.nodes[1])
        self.weighted = self.zmat * self.nodes[2][:, None]
        self.found = self.mfe0 = None
        self.n_traces = 0

    # ---- time -------------------------------------------------------------
    def check_time(self):
        if self.cap_s is not None and self.clock() - self.t_start > self.cap_s:
            raise Capped("cap_s %.0f s reached" % self.cap_s)

    def used_s(self):
        return self.clock() - self.t_start

    # ---- tables -----------------------------------------------------------
    def tables(self):
        sd = self.system.SystemData
        f = sd.Fields
        raw = [(float(f.GetField(i).X), float(f.GetField(i).Y), float(f.GetField(i).Weight))
               for i in range(1, int(f.NumberOfFields) + 1)]
        rmax = max((math.hypot(x, y) for x, y, _ in raw), default=1.0) or 1.0
        self.fields = [{"field": i + 1, "hx": x / rmax, "hy": y / rmax, "weight": w} for i, (x, y, w) in enumerate(raw)]
        wl = sd.Wavelengths
        self.waves = []
        for i in range(1, int(wl.NumberOfWavelengths) + 1):
            w = wl.GetWavelength(i)
            try:
                primary = bool(w.IsPrimary)
            except Exception:  # noqa: BLE001 -- not every build exposes the flag
                primary = (i == 1)
            self.waves.append({"wave": i, "um": float(w.Wavelength), "weight": float(w.Weight), "primary": primary})
        return self.fields, self.waves

    def rwce(self, hx, hy, wave=0, ring=RWCE_RING):
        self.check_time()
        raw, _sus = self.mc.read_operand_slots(self.system, "RWCE", self.am._rwce_slots(ring, wave, hx, hy))
        return float(raw) if _finite(raw) else None

    # ---- the batch trace ---------------------------------------------------
    def trace(self, rays):
        """rays = [(wave, hx, hy, px, py)] -> list of {opd, err, vig}; through the shared ``trace_rays`` batch tool."""
        self.check_time()
        from . import analysis_raytrace as art
        env = art.trace_rays(self.session, {"rays": [{"wave": w, "Hx": hx, "Hy": hy, "Px": px, "Py": py}
                                                     for w, hx, hy, px, py in rays], "opd_mode": OPD_MODE})
        if not (isinstance(env, dict) and env.get("ok") is True):
            raise _Refuse("tolerancing_run", "the batch ray trace refused: %s" % ((env or {}).get("error") or env))
        if env.get("short_read"):
            raise _Refuse("tolerancing_run", "the batch ray trace returned %s of %s rays" % (env.get("returned"), env.get("requested")))
        self.n_traces += 1
        return [{"opd": r["opd"], "err": r["errorCode"], "vig": r["vignetteCode"]} for r in env["rays"]]

    # ---- thickness / back gap ---------------------------------------------------
    def set_thickness(self, index, value):
        self.lde.GetSurfaceAt(index).Thickness = float(value)

    def get_thickness(self, index):
        return float(self.lde.GetSurfaceAt(index).Thickness)

    def prepare_back_gap(self):
        n = int(self.lde.NumberOfSurfaces)
        self.back = n - 2
        solve = self.tl._solve_of(self.lde.GetSurfaceAt(self.back), "thickness")
        if solve == "Variable":
            self.tl.freeze_thickness(self.system, self.back, self.undo)
        elif solve != "Fixed":
            raise _Refuse("gauge_scope", "the back-focus gap (surface %d) carries a %r thickness solve; the gauge "
                          "moves that gap and cannot restore a solve it did not place" % (self.back, solve),
                          reason="back_gap_solved")
        self.t0 = self.get_thickness(self.back)

    # ---- layout ------------------------------------------------------------
    def snapshot(self):
        """The proof baseline, taken BEFORE the first mutation (a fault here leaves ``found`` None)."""
        self.mfe0 = self.tl.mfe_snapshot(self.system)
        self.found = self.tl.state_with_mce(self.system, allow_no_variables=True)

    def build_layout(self, groups, surfaces):
        """Every group pair (back to front), then every surface pair (back to front); indices tracked."""
        self.groups, self.surfaces = [tuple(g) for g in groups], list(surfaces)
        n0 = int(self.lde.NumberOfSurfaces)
        imap = IndexMap(n0)
        self.map = imap
        for a, b in sorted(self.groups, reverse=True):
            self.tl.insert_group_pair(self.session, imap.cur[a], imap.cur[b], self.undo)
            imap.group_pair(imap.cur[a], imap.cur[b], "%d_%d" % (a, b))
            self.check_time()
        for s in sorted(self.surfaces, reverse=True):
            self.tl.insert_surface_pair(self.session, imap.cur[s], self.undo)
            imap.surface_pair(imap.cur[s], str(s))
            self.check_time()
        n1 = int(self.lde.NumberOfSurfaces)
        expected = n0 + 4 * len(self.groups) + 2 * len(self.surfaces)
        if n1 != expected:
            raise _Refuse("tolerancing_run", "the prepared layout has %d surfaces, expected %d" % (n1, expected))
        markers = [k for k in imap.cur if isinstance(k, str) and k.endswith(("_cb", "_ret"))]
        bad = [k for k in markers if not self.cbc.is_coordinate_break(self.lde.GetSurfaceAt(imap.cur[k]))]
        if bad:
            raise _Refuse("tolerancing_run", "layout markers %s are not coordinate breaks" % bad)
        return {"n_before": n0, "n_after": n1, "n_groups": len(self.groups), "n_surfaces": len(self.surfaces)}

    def marker(self, row):
        if row["code"] in GROUP_CODES:
            return "g%d_%d_cb" % (row["surface"], row["surface2"])
        return "s%d_cb" % row["surface"]

    def cb_row(self, marker):
        return self.lde.GetSurfaceAt(self.map.cur[marker])

    def write_par(self, marker, par, value):
        return self.cbc.write_cb_cell(self.system, self.cb_row(marker), par, float(value))

    def set_assign(self, assign):
        """Zero every touched CB cell not in ``assign``, then write ``assign`` {(marker, par): value}."""
        for key in list(self.touched):
            if key not in assign:
                self.write_par(key[0], key[1], 0.0)
                self.touched.pop(key)
        for key, v in assign.items():
            self.write_par(key[0], key[1], v)
            self.touched[key] = v

    def pickup_canaries(self, row_group=None, row_surface=None):
        """A write on an entry CB must read back negated on its return CB (the inverse pickup)."""
        out = []
        for marker, par, val in ((row_surface, "tilt_x", 0.01), (row_group, "decenter_x", 0.005)):
            if marker is None:
                continue
            ret = marker[:-3] + "_ret"
            self.write_par(marker, par, val)
            got = self.cbc.read_cb_cell(self.system, self.cb_row(ret), par)
            self.write_par(marker, par, 0.0)
            out.append({"marker": marker, "par": par, "written": val, "return_reads": got})
        return out

    # ---- row edits (each returns {apply, restore}) -----------------------------------------------------------------
    def edit_for(self, row, value, twav_um=None):
        code = row["code"]
        if code in _PAR_OF:
            key = (self.marker(row), _PAR_OF[code])
            return Edit(lambda: self.set_assign({key: value}), lambda: self.set_assign({}))
        if code == "TTHI":
            return self.edit_thickness(row["surface"], row["surface2"], value)
        if code in ("TIND", "TABB"):
            return self.edit_material_offset(row["surface"], value if code == "TIND" else 0.0,
                                             value if code == "TABB" else 0.0)
        if code == "TRAD":
            return self.edit_radius_delta(row["surface"], value)
        if code == "TFRN":
            if not _finite(twav_um):
                raise _Refuse("gauge_input", "TFRN needs the wizard's TWAV test wavelength and it was not read")
            return self.edit_fringes(row["surface"], value, twav_um)
        raise _Refuse("gauge_row_unmapped", "no gauge edit for tolerance code %s" % code)

    def edit_thickness(self, s, s2, delta):
        """TTHI: t_s += delta; t_adjust -= delta only when the row declares an adjust surface (surface2 != s)."""
        i = self.map.thickness_carrier(s, self.groups, self.surfaces)
        adjust = s2 not in (None, 0) and s2 != s
        j = self.map.thickness_carrier(s2, self.groups, self.surfaces) if adjust else None
        ti = self.get_thickness(i)
        tj = self.get_thickness(j) if j is not None else None

        def apply():
            self.set_thickness(i, ti + delta)
            if j is not None:
                self.set_thickness(j, tj - delta)

        def restore():
            self.set_thickness(i, ti)
            if j is not None:
                self.set_thickness(j, tj)
        return Edit(apply, restore, cells=[i] + ([j] if j is not None else []))

    def edit_material_offset(self, s, nd, vd):
        """TIND (NdOffset) / TABB (absolute VdOffset) as a MaterialOffset solve; the original is restored."""
        i = self.map.cur[s]
        row = self.lde.GetSurfaceAt(i)
        mat0 = str(row.Material)
        solve0 = self.sc.read_solve_type(row.MaterialCell)
        if solve0 is not None and not str(solve0).endswith("Fixed"):
            raise _Refuse("gauge_scope", "surface %d: the material carries a %s solve; the gauge cannot restore it" % (s, solve0))
        failures = []

        def apply():
            env = self.surface_solve.set_solve(self.session, {"surface": i, "cell": "material", "solve_type": "MaterialOffset",
                                                              "fields": {"NdOffset": float(nd), "VdOffset": float(vd)}})
            bad = offset_write_mismatch(env, {"NdOffset": float(nd), "VdOffset": float(vd)})
            if bad:
                failures.append({"surface": s, "mismatch": bad})

        def restore():
            self.surface_solve.clear_solve(self.session, {"surface": i, "cell": "material"})
            if _lens_state._tok(self.lde.GetSurfaceAt(i).Material) != _lens_state._tok(mat0):
                self.lde.GetSurfaceAt(i).Material = mat0
            if _lens_state._tok(self.lde.GetSurfaceAt(i).Material) != _lens_state._tok(mat0):
                raise _Refuse("state_mutated", "surface %d: the material did not return to %s" % (s, mat0))
        return Edit(apply, restore, failures)

    def edit_radius_delta(self, s, delta):
        i = self.map.cur[s]
        r0 = float(self.lde.GetSurfaceAt(i).Radius)

        def apply():
            self.lde.GetSurfaceAt(i).Radius = r0 + float(delta)

        def restore():
            self.lde.GetSurfaceAt(i).Radius = r0
        return Edit(apply, restore)

    def semi_diameter(self, s):
        v = float(self.lde.GetSurfaceAt(self.map.cur[s]).SemiDiameter)
        if _finite(v) and v > 0:
            return v
        raise _Refuse("gauge_input", "surface %d has no usable semi-diameter for TFRN" % s)

    def edit_fringes(self, s, fringes, twav_um):
        """TFRN: N fringes at the semi-diameter, lambda = TWAV; the sign is swapped against the native pairing (P1b)."""
        i = self.map.cur[s]
        r0 = float(self.lde.GetSurfaceAt(i).Radius)
        r_new = tfrn_radius(r0, TFRN_SIGN * float(fringes), twav_um, self.semi_diameter(s))

        def apply():
            self.lde.GetSurfaceAt(i).Radius = float(r_new)

        def restore():
            self.lde.GetSurfaceAt(i).Radius = r0
        return Edit(apply, restore, radius_new=r_new)

    def edit_focus(self, dt):
        return Edit(lambda: self.set_thickness(self.back, self.t0 + dt), lambda: self.set_thickness(self.back, self.t0))

    # ---- teardown ----------------------------------------------------------------
    def teardown(self):
        """Zero the CB cells, then ``_cb_teardown`` (undo LIFO, variables, merit, digest proof). Never raises."""
        for key in list(self.touched):
            try:
                self.write_par(key[0], key[1], 0.0)
            except Exception:  # noqa: BLE001 -- the undo log removes the inserts anyway
                pass
        self.touched.clear()
        from . import tolerance_run as tr
        ctx = tr._CbRun({})
        ctx.undo, ctx.found, ctx.mfe0 = self.undo, self.found, self.mfe0
        return tr._cb_teardown(self.system, ctx)


def offset_write_mismatch(envelope, wanted, tol=OFFSET_TOL):
    """The MaterialOffset fields that did not read back as written ([] when every one did); relative to the value."""
    rb = envelope.get("fields_readback") if isinstance(envelope, dict) else None
    bad = []
    for name, want in wanted.items():
        cell = (rb or {}).get(name)
        got = cell.get("value") if isinstance(cell, dict) else cell
        if not _finite(got) or abs(float(got) - float(want)) > tol * (1.0 + abs(float(want))):
            bad.append({"field": name, "wrote": float(want), "read_back": got if _finite(got) else None})
    return bad


# --------------------------------------------------------------------------- #
# the spec's 1.8 reads (ctx is the Rig)
# --------------------------------------------------------------------------- #
def read_state(ctx):
    """The (F, W, T) Standard coefficients of the CURRENT state, waves, from one batch trace."""
    rho, phi, _wt = ctx.nodes
    px, py = rho * np.cos(phi), rho * np.sin(phi)
    n = len(rho)
    rays = [(w["wave"], f["hx"], f["hy"], float(px[k]), float(py[k])) for f in ctx.fields for w in ctx.waves for k in range(n)]
    res = ctx.trace(rays)
    if len(res) != len(rays) or any(r["err"] or r["vig"] for r in res):
        raise _Refuse("gauge_scope", "the batch route met an errored or vignetted ray; the gauge reads unvignetted "
                      "wavefronts only", reason="vignetted_node")
    opd = np.array([r["opd"] for r in res], float).reshape(len(ctx.fields) * len(ctx.waves), n)
    return (opd @ ctx.weighted).reshape(len(ctx.fields), len(ctx.waves), T)


def paraxial_focus(ctx):
    """The back-gap thickness at paraxial focus for the CURRENT state (row gate only); the gap returns to Fixed at t0."""
    ctx.check_time()
    try:
        ctx.surface_solve.set_solve(ctx.session, {"surface": ctx.back, "cell": "thickness", "solve_type": "MarginalRayHeight",
                                                  "fields": {"Height": 0.0}})
        tb = ctx.get_thickness(ctx.back)
    finally:
        ctx.lde.GetSurfaceAt(ctx.back).ThicknessCell.MakeSolveFixed()
        ctx.set_thickness(ctx.back, ctx.t0)
    if not ctx.tl._close_lde(ctx.get_thickness(ctx.back), ctx.t0):
        raise _Refuse("state_mutated", "the back gap did not return to its nominal thickness")
    return tb


def push_states(ctx, group_pair, pars):
    """``{par: (z_lo, z_hi)}`` for each Par of ``pars`` ({par: r}; 1/2 decenter, 3/4 tilt) on the push group's ENTRY CB
    at -r / +r, one cell at a time, each cleared after its read."""
    a, b = group_pair
    marker = "g%d_%d_cb" % (a, b)
    out = {}
    for par in sorted(pars):
        name, r = _tc._CPAR_AXES[par][0], float(pars[par])
        pair = []
        for v in (-r, r):
            ctx.check_time()
            try:
                ctx.set_assign({(marker, name): v})
                pair.append(read_state(ctx))
            finally:
                ctx.set_assign({})
        out[par] = (pair[0], pair[1])
    return out


def symmetry_check(ctx):
    """Refuse ``gauge_not_symmetric`` when a mirrored or rotated off-axis point reads differently from the user field
    (> 1e-6 relative on the polychromatic RWCE at the nominal)."""
    worst = 0.0
    for f in ctx.fields:
        hx, hy = f["hx"], f["hy"]
        h = math.hypot(hx, hy)
        if h < 1e-12:
            continue
        ref = ctx.rwce(hx, hy)
        for px, py in ((-hx, -hy), (hy, -hx), (-hy, hx)):
            v = ctx.rwce(px, py)
            if ref is None or v is None:
                raise _Refuse("gauge_not_symmetric", "the nominal RWCE at a mirror point could not be read")
            worst = max(worst, abs(v - ref) / max(abs(ref), 1e-30))
    if worst > SYMMETRY_REL:
        raise _Refuse("gauge_not_symmetric", "the nominal mirror/rotated RWCE differs from the user field by %.2e "
                      "(> %.0e relative); the 13-point composite is rebuilt by symmetry and the lens is not "
                      "rotationally symmetric" % (worst, SYMMETRY_REL))
    return worst


def wizard_reads(system, budget):
    """TWAV (um) and the COMP bounds as the wizard writes them; the TDE is cleared afterwards."""
    from . import tolerance_run as tr
    res = _tc.apply_wizard_budget(system, budget)
    try:
        twav = None
        row = next((r for r in res["rows"] if str(r["type"]).endswith("TWAV")), None)
        if row:
            cands = [(h, v) for h, v in row["cells"].values() if isinstance(v, float) and math.isfinite(v) and 0.1 < v < 20.0]
            twav = next((v for h, v in cands if "wave" in _lens_state._tok(h).lower()), cands[0][1] if cands else None)
        return {"twav_um": twav, "comp": res.get("comp")}
    finally:
        tr._clear_tde(system.TDE)


# --------------------------------------------------------------------------- #
# the row gate (locked 1.7) on the assembled pack
# --------------------------------------------------------------------------- #
def _loaded(pack):
    """``load_pack`` of the numeric core on the CURRENT dict (the gate edits the dict, so it reloads)."""
    loaded = _g().load_pack(pack)
    if isinstance(loaded, dict) and loaded.get("ok") is False:
        raise _Refuse(loaded.get("error_family") or "gauge_input", loaded.get("message") or "the pack did not load")
    return loaded


def gauge_composite(pack, X):
    """RMS (waves) of the composite under each row of ``X`` (n, K), refocus applied (the gate reads Delta t = X.(k + back_fix)).

    Everything numeric is the numeric core's: ``jacobian`` supplies the focus column C[..., 0] and k, ``field_vectors``
    the vectors, ``composite`` the 4-image symmetry sum (RMS^2, fold and delta0 included)."""
    g = _g()
    lp = _loaded(pack)
    _J, _H, k, C = g.jacobian(lp, 1)
    kcol = np.asarray(C)[..., 0]
    # A TTHI edit that moves the back gap has already set that gap by ``back_move * x``; the paraxial ``k`` is the ABSOLUTE
    # focus shift of the edited state, so the refocus is (bfl - bfl0) - back_move * x, not (bfl - bfl0) (gate only).
    back_fix = np.array([-float(r.get("back_move") or 0.0) for r in pack["rows"]])

    def z_fn(Xp):
        u = np.asarray(g.field_vectors(lp, Xp, order=1))
        dt = np.asarray(Xp) @ (np.asarray(k) + back_fix)
        return u + dt[:, None, None, None] * kcol[None]
    return np.sqrt(np.maximum(np.asarray(g.composite(lp, np.asarray(X, float), z_fn)), 0.0))


def _row_index(pack):
    return {r["key"]: i for i, r in enumerate(pack["rows"])}


def row_gate(pack, nominal, native_rss=None):
    """Delta0, demotions and the canary, in place on ``pack``. Returns ``{canary, delta0, warnings, folded_share, demoted}``.

    ``nominal`` is the native nominal criterion. The numeric core owns the delta0 rule (``delta0``: <= 0.3 % none, <= 5 %
    an RMS^2 offset, above that ``gauge_composite_off``) and the folded share (``folded_share``; its evaluation refuses
    above 10 %); this gate only reads them."""
    g = _g()
    rows = pack["rows"]
    K = len(rows)
    warnings = []
    x0 = np.zeros((1, K))
    comp0 = float(gauge_composite(pack, x0)[0])
    delta0, verdict = g.delta0(_loaded(pack), comp0 ** 2)
    if isinstance(verdict, dict):
        raise _Refuse(verdict.get("error_family") or "gauge_composite_off", verdict.get("message") or "the composite is off")
    rel = abs(comp0 - nominal) / nominal if nominal else float("inf")
    if delta0 is not None:
        pack["composite"]["delta0_rms2"] = delta0
        warnings.append("composite_delta0: the nominal composite is %.2f %% off the native value; delta0 = %.3e waves^2 "
                        "is added to the composite bookkeeping" % (100 * rel, delta0))
        comp0 = float(gauge_composite(pack, x0)[0])
    gated, flips, errs = [], [], {}
    idx = _row_index(pack)
    ranked = sorted((r for r in rows if r["mode"] == "edit"), key=lambda r: -r["share"])
    top = {r["key"] for r in ranked[:TOP_N]}
    plan = [(r, side, val) for r in ranked for side, val in state_values(r["lo"], r["hi"], r["width"])]
    X = np.zeros((max(len(plan), 1), K))
    for n, (r, _side, val) in enumerate(plan):
        X[n, idx[r["key"]]] = val
    changes = (gauge_composite(pack, X)[:len(plan)] - comp0) if plan else np.zeros(0)
    by_row = {}
    for (r, side, _val), dg in zip(plan, changes):
        by_row.setdefault(r["key"], {})[side] = float(dg)
    gauge_c = {}
    for r in ranked:
        have = by_row.get(r["key"], {})
        nat = {"lo": r["native"]["change_lo"], "hi": r["native"]["change_hi"]}
        row_err, flipped = {}, False
        for side, dg in have.items():
            nv = nat[side]
            if not _finite(nv):
                continue
            row_err[side] = abs(dg - nv) / max(abs(nv), 1e-4)
            flipped = flipped or (dg * nv < 0)
        r["gate"] = {"err_lo": row_err.get("lo"), "err_hi": row_err.get("hi"), "verdict": "pass"}
        gauge_c[r["key"]] = math.sqrt((have.get("lo", 0.0) ** 2 + have.get("hi", 0.0) ** 2) / 2.0)
        if r["share"] > GATE_SHARE:
            gated.append(r["key"])
            if flipped:
                flips.append(r["key"])
            if flipped or any(e > GATE_REL for e in row_err.values()):
                r["gate"]["verdict"] = "demoted"
                errs[r["key"]] = row_err
    demoted = [r["key"] for r in rows if r["gate"]["verdict"] == "demoted"]
    for r in rows:
        if r["gate"]["verdict"] == "demoted":
            r["mode"] = "fold"
            nat = r["native"]
            w_lo = nat["w_lo"] if _finite(nat["w_lo"]) else nominal
            w_hi = nat["w_hi"] if _finite(nat["w_hi"]) else nominal
            r["c2"] = fold_fit(r["lo"], r["hi"], float(w_lo if r["lo"] != 0 else nominal),
                               float(w_hi if r["hi"] != 0 else nominal), float(nominal))
            for k in ("z_lo", "z_hi", "bfl_lo", "bfl_hi"):
                r[k] = None
    fs = g.folded_share(_loaded(pack))
    share = float(fs["share"])
    if share > FOLD_WARN:
        warnings.append("folded_share: %.1f %% of the sensitivity is carried by the fold channel (> %.0f %%): %s"
                        % (100 * share, 100 * FOLD_WARN, ", ".join(fs["top"])))
    # the gauge RSS: edit rows by their gauge change, folded rows by their native one
    sq = 0.0
    for r in rows:
        if r["mode"] == "edit":
            sq += gauge_c.get(r["key"], 0.0) ** 2
        else:
            c = (r["native"]["change_lo"], r["native"]["change_hi"])
            sq += math.sqrt(sum(float(v) ** 2 for v in c if _finite(v)) / 2.0) ** 2
    rss_gauge = math.sqrt(sq)
    rss_native = native_rss if _finite(native_rss) and native_rss > 0 else math.sqrt(
        math.fsum(row_contribution({"min": {"change": r["native"]["change_lo"]}, "max": {"change": r["native"]["change_hi"]}}) ** 2
                  for r in rows))
    rss_rel = abs(rss_gauge - rss_native) / rss_native if rss_native else None
    top_flips = [k for k in flips if k in top]
    if rss_rel is not None and rss_rel > RSS_REL:
        warnings.append("rss_canary: the gauge RSS differs from the native RSS by %.1f %% (> %.0f %%)" % (100 * rss_rel, 100 * RSS_REL))
    if top_flips:
        warnings.append("row_gate_flip: top-%d rows flipped sign and were folded: %s" % (TOP_N, top_flips))
    canary = {"n_gated": len(gated), "demoted": demoted, "top_flips": top_flips, "rss_gauge": rss_gauge,
              "rss_native": rss_native, "rss_rel": rss_rel, "nominal_gauge": comp0, "nominal_native": nominal,
              "nominal_rel": rel, "folded_share": share, "errors": errs}
    return {"canary": canary, "delta0": delta0, "warnings": warnings, "folded_share": share, "demoted": demoted}


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #
def _ls(a):
    return None if a is None else np.asarray(a, float).tolist()


def _sha(obj):
    return hashlib.sha256(repr(obj).encode()).hexdigest()


def focus_column(ctx, z0, h=FOCUS_H):
    """(h, z_lo, z_hi) of the back-gap column; ``h`` is halved once when the column is not linear to 1 %."""
    for attempt in (0, 1):
        pair = []
        for dt in (-h, h):
            ctx.check_time()
            e = ctx.edit_focus(dt)
            try:
                e.apply()
                pair.append(read_state(ctx))
            finally:
                e.restore()
        span = float(np.linalg.norm(pair[1] - pair[0]))
        curv = float(np.linalg.norm(pair[1] + pair[0] - 2.0 * z0))
        if span > 0 and curv / span <= FOCUS_LINEAR_REL or attempt == 1:
            return h, pair[0], pair[1], (curv / span if span > 0 else None)
        h = h / 2.0
    return None


def acquire_pack(session, sens_env, *, push=None, cap_s=None, budget=None, rig_factory=None):
    """Acquire one ``wfd_pack v1`` for the loaded lens, or return a refusal dict (``ok: False``). Never raises.

    ``sens_env`` is the inner sensitivity envelope (rows, nominal criterion, run summary). ``push`` is None for
    refocus-only, else ``{"group", "a", "b", "decenter_mm", "pars"}`` of the resolved ``cb_group`` (``decenter_mm`` is None for
    a tilt-only push; ``pars`` is the resolver's ``{par: range}`` and the one source of the columns). ``budget`` is the wizard
    budget (TWAV and the COMP bounds are read from it; spec 1.8 has no such parameter and needs one).
    """
    t_total = time.monotonic()
    rig = None
    try:
        rows_in = sens_env.get("sensitivity") or []
        nominal = sens_env.get("nominal_criterion")
        if not rows_in or not _finite(nominal):
            return refusal("gauge_input", "the sensitivity envelope carries no rows or no finite nominal criterion")
        rows = classify_rows(rows_in, float(nominal))
        attach_folds(rows, float(nominal))
        if push is not None:
            push_pars(push)                      # a malformed push refuses before a rig is built
        layout = derive_layout(rows, push)
        rig = (rig_factory or Rig)(session, cap_s)
        fields, waves = rig.tables()
        reads = wizard_reads(rig.system, budget) if budget is not None else {"twav_um": None, "comp": None}
        if any(r["code"] == "TFRN" and r["width"] != "zero" for r in rows) and not _finite(reads["twav_um"]):
            return refusal("gauge_input", "TFRN rows are present and the wizard's TWAV test wavelength was not read")
        comp = reads.get("comp") or {}
        if not (_finite(comp.get("min")) and _finite(comp.get("max"))):
            return refusal("gauge_input", "the wizard's COMP row carries no usable Min/Max (the focus limits)")
        rig.snapshot()
        body, failure = None, None
        try:
            body = _walk(rig, rows, layout, push, reads, comp, fields, waves, sens_env, float(nominal), budget)
        except BaseException as exc:  # noqa: BLE001 -- held until the teardown has run; re-dispatched below
            failure = exc
        td = rig.teardown()
        if not (td or {}).get("restored"):
            return refusal("state_mutated", "the prepared copy could not be proven restored (failed step %r, %s "
                           "difference(s)); %s" % ((td or {}).get("failed_step"), (td or {}).get("n_differences"),
                                                  RELOAD_REMEDY), reason="teardown_unproven", restore=td,
                           recovery=RELOAD_REMEDY)
        if failure is not None:
            if isinstance(failure, Capped):
                return refusal("capped", "%s; the prepared copy was torn down and no statistics were produced" % failure,
                               reason="capped")
            raise failure
        pack, extra = body
        pack["canaries"]["teardown"] = td
        pack["canaries"]["acquire"] = extra
        pack["timings"] = dict(pack["timings"], total_s=round(time.monotonic() - t_total, 3))
        return pack
    except _Refuse as exc:
        return refusal(exc.family, exc.message, exc.reason, **exc.extra)
    except Capped as exc:
        return refusal("capped", str(exc), reason="capped")
    except Exception as exc:  # noqa: BLE001 -- the engine / .NET net: typed, never raised past the boundary
        return refusal("tolerancing_run", "the gauge acquisition hit an unexpected fault (%s: %s)" % (type(exc).__name__, exc),
                       reason="acquire_fault")


def _back_move(edit, back):
    """How far one unit of the edit moves the back gap itself: +1 when the edit's own cell is the back gap, -1 when its
    adjust cell is (``edit_thickness`` writes ``t_s += x`` and ``t_adjust -= x``); 0 for every other row."""
    cells = (getattr(edit, "info", None) or {}).get("cells") or []
    if back is None:
        return 0.0
    return float((1 if cells[:1] == [back] else 0) - (1 if cells[1:2] == [back] else 0))


def _walk(rig, rows, layout, push, reads, comp, fields, waves, sens_env, nominal, budget):
    t_prep = time.monotonic()
    info = rig.build_layout(layout["groups"], layout["surfaces"])
    rig.prepare_back_gap()
    first_group = next((r for r in rows if r["code"] in GROUP_CODES), None)
    first_surf = next((r for r in rows if r["code"] in SURFACE_CODES), None)
    pick = rig.pickup_canaries(rig.marker(first_group) if first_group else None, rig.marker(first_surf) if first_surf else None)
    if any(abs(c["return_reads"] + c["written"]) > 1e-12 for c in pick):
        raise _Refuse("tolerancing_run", "a coordinate-break return surface did not pick up the negated entry value: %s" % pick)
    prepare_s = time.monotonic() - t_prep
    symmetry = symmetry_check(rig)
    t_walk = time.monotonic()
    z0 = read_state(rig)
    bfl0 = paraxial_focus(rig)
    n_states = 1
    failures = []
    for r in rows:
        r["z_lo"] = r["z_hi"] = r["bfl_lo"] = r["bfl_hi"] = None
        if r["mode"] != "edit":
            continue
        vals = dict(state_values(r["lo"], r["hi"], r["width"]))
        for side, val in vals.items():
            rig.check_time()
            e = rig.edit_for(r, val, reads["twav_um"])
            try:
                e.apply()
                z = read_state(rig)
                tb = paraxial_focus(rig)
            finally:
                e.restore()
            move = _back_move(e, getattr(rig, "back", None))
            if move:
                r["back_move"] = move
            n_states += 1
            failures += e.failures
            r["z_" + side], r["bfl_" + side] = z, tb
        if r["width"] == "fixed":
            r["z_hi"], r["bfl_hi"] = r["z_lo"], r["bfl_lo"]
    if failures:
        raise _Refuse("gauge_input", "a MaterialOffset write did not read back as written: %s" % failures, reason="offset_write")
    h, zlo, zhi, curv = focus_column(rig, z0)
    n_states += 2
    columns = {"focus": {"h": h, "z_lo": _ls(zlo), "z_hi": _ls(zhi), "nonlinearity": curv}}
    check = {}
    pars = push_pars(push) if push is not None else {}
    if push is not None:
        ps = push_states(rig, (int(push["a"]), int(push["b"])), pars)
        n_states += 2 * len(pars)
        for par in sorted(pars):
            columns[_g().PAR_LABEL[par]] = {"r": pars[par], "z_lo": _ls(ps[par][0]), "z_hi": _ls(ps[par][1])}
            axis, code = PUSH_CHECK[par]
            row = next((r for r in rows if r["code"] == code and (r["surface"], r["surface2"]) == (int(push["a"]), int(push["b"]))
                        and r["z_lo"] is not None and r["z_hi"] is not None and r["hi"] != r["lo"]), None)
            if row is not None:
                jr = (row["z_hi"] - row["z_lo"]) / (row["hi"] - row["lo"])
                cp = (ps[par][1] - ps[par][0]) / (2.0 * pars[par])
                check[axis] = push_column_check(jr, cp)
    walk_s = time.monotonic() - t_walk
    rs = sens_env.get("run_summary") or {}
    pack_rows = []
    for r in rows:
        pr = {k: r[k] for k in ("key", "code", "surface", "surface2", "lo", "hi", "mode", "width", "native", "c2", "gate", "share")}
        for k in ("z_lo", "z_hi"):
            pr[k] = _ls(r.get(k))
        for k in ("bfl_lo", "bfl_hi"):
            pr[k] = r.get(k)
        if r.get("back_move"):
            pr["back_move"] = r["back_move"]
        pack_rows.append(pr)
    keyed = {r["key"]: r for r in rows}
    pack = {
        "schema": "wfd_pack v1",
        "lens": {"path": None, "sha256": None, "topology_digest": rs.get("topology_digest")},
        "budget": {"rule_id": rs.get("budget_rule_id"), "digest": rs.get("budget_digest")},
        "criterion": {"kind": "wavefront", "sampling": rs.get("sampling"), "threshold": None},
        "fields": [{"hx": f["hx"], "hy": f["hy"], "weight": f["weight"]} for f in fields],
        "waves": [{"um": w["um"], "weight": w["weight"], "primary": w["primary"]} for w in waves],
        "zernike": {"type": "standard", "terms": T, "read": "batch_opd", "opd_mode": OPD_MODE, "quad": [RINGS, ARMS]},
        "composition": {"variant_id": "own_drop", "params": {}},
        "composite": {"rule": "rot4/rms/points", "delta0_rms2": None},
        "symmetry": {"rotational": True,
                     "partners": [[k, _partner_key(k, keyed)] for k, r in keyed.items() if r["code"] in PARTNERS
                                  and _partner_key(k, keyed) is not None],
                     "mirror_sign": {k: (-1 if r["code"] in MIRROR_FLIP else 1) for k, r in keyed.items()},
                     "rot_sign": _rot_signs(keyed)},
        "rows": pack_rows,
        "z0": _ls(z0), "bfl0": bfl0,
        "columns": columns,
        "limits": _limits(comp, pars),
        "push": _push_block(push, pars),
        "native": {"nominal_criterion": nominal, "rss": sens_env.get("rss_estimated_change")},
        "canaries": {"layout": info, "pickup": pick, "symmetry_rel": symmetry, "push_column_check": check,
                     "teardown": None},
        "timings": {"prepare_s": round(prepare_s, 3), "walk_s": round(walk_s, 3), "n_states": n_states,
                    "per_state_ms": round(1000.0 * walk_s / max(n_states, 1), 1), "total_s": None},
    }
    gate = row_gate(pack, nominal, sens_env.get("rss_estimated_change"))
    pack["canaries"]["row_gate"] = gate["canary"]
    return pack, {"warnings": gate["warnings"] + push_check_warnings(check), "delta0": gate["delta0"],
                  "folded_share": gate["folded_share"], "demoted": gate["demoted"], "twav_um": reads["twav_um"],
                  "push_column_check": check}


def _limits(comp, pars):
    """``limits.push`` is always present (None without a decenter); ``limits.tilt`` only when a tilt is solved, so a
    decenter-only or refocus-only pack keeps its identity."""
    out = {"focus": [float(comp["min"]), float(comp["max"])], "push": pars.get(1)}
    if 3 in pars:
        out["tilt"] = pars[3]
    return out


def _push_block(push, pars):
    if not push:
        return None
    out = {"group": push["group"]}
    if 1 in pars:
        out["decenter_mm"] = pars[1]
    if 3 in pars:
        out["tilt_deg"] = pars[3]
    return out


def _rot_signs(keyed):
    """The rotation map's signs (locked 3.4, pinned by the spec's row 6): a +90 degree rotation sends (x, y) to (-y, x), so
    TSTY+ at the rotated point reads as TSTX- at the user point and TEDX+ as TEDY+; every X row of a partner pair carries -1 and
    its Y partner +1 (tilts about x / y rotate as the decenters do)."""
    out = {}
    for key, r in keyed.items():
        if r["code"] in PARTNERS and _partner_key(key, keyed) is not None:
            out[key] = -1
            out[_partner_key(key, keyed)] = 1
    return out


def _partner_key(key, keyed):
    r = keyed[key]
    want = PARTNERS.get(r["code"])
    if want is None:
        return None
    return next((k for k, o in keyed.items() if o["code"] == want and o["surface"] == r["surface"] and o["surface2"] == r["surface2"]), None)
