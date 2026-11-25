"""tools/_wfd_gauge.py -- the vector tolerancing gauge's numeric core. Pure numpy; NO engine import.

CONTRACT FOR THE CALLERS (``_wfd_acquire`` builds the pack, ``_wfd_run`` serves the result)
------------------------------------------------------------------------------------------
Every public function returns a refusal dict ``{"ok": False, "error_family", "message"}`` instead of raising; test
``isinstance(x, dict) and x.get("ok") is False``. ``load_pack(doc)`` returns a ``Pack`` or a refusal.

Pack (``wfd_pack v1``, amended; only what the core reads is validated, extra metadata is ignored):
  ``zernike {terms: T}``, ``fields [{hx, hy, weight}]``, ``waves [{um, weight, primary}]``, ``z0 [F][W][T]``,
  ``native {nominal_criterion}``, ``lens {sha256, topology_digest}``, ``budget {rule_id, digest}``,
  ``composition {variant_id}``, ``bfl0``, ``symmetry {partners, mirror_sign, rot_sign}``,
  ``rows [{key, code, lo, hi, mode: edit|fold, width, z_lo?, z_hi?, bfl_lo?, bfl_hi?, native {w_lo, w_hi}}]``,
  ``columns {focus {h, z_lo, z_hi}, push_x? {r, z_lo, z_hi}, push_y?, tilt_x? {r, ...}, tilt_y?}`` (the pre-amendment
  top-level ``refocus`` is read as ``columns.focus``; any other column name refuses ``gauge_input``; ``COMP_LABELS`` is the
  one table), ``limits {focus: [lo, hi], push: r, tilt: r}`` (``evaluate`` only; ``push`` is read for the push labels and
  ``tilt`` for the tilt labels, each present when its columns are).
  Row arrays are ``[F][W][T]`` lists (``null`` on an absent side). Fold rows carry ``native`` readings (criterion in
  waves at lo / hi); their quadratic is built here (``fold_rms2``).

``evaluate(...)`` returns a dict with, beside the served keys, ``_arrays`` (numpy: ``worst`` (n,), ``field_rms``
(n,F), ``at_bound`` (n,)) which the caller MUST pop before serving; ``store_put(result)`` keeps those arrays under
``result["gauge"]["id"]`` (``"gauge-"`` prefix, ``STORE_MAX = 16`` slots) and ``paired(res_a, res_b)`` reads two
results that still carry ``_arrays`` (use ``store_get``). ``result["values"]`` / ``result["at_bound"]`` are the
per-trial worst-field values and push- or tilt-bound flags (``FAIL_LABELS``; a focus bound does not fail) as lists, for ``tolerance_run._mc_yield_block`` (not imported
here). ``result["yield_stats"]`` is the ``_tolerance_pick.stats`` dict, ``result["per_field"]`` the served block.
The ``nominal`` is the worst-field RMS at d = 0.

``result["gauge"]["compensators"]`` is the sorted ``[label, lo, hi]`` rows of the solved columns, focus included: two results
with different sets (or a set missing on either side) never pair; ``paired`` reports ``reason: "compensator"`` and serves no
verdict or diff block.
"""
import hashlib
import json
import math
import zlib
from collections import OrderedDict

import numpy as np

from . import _tolerance_common as _tc
from . import _tolerance_pick as _pick

SCHEMA = "wfd_pack v1"
SEED = 20261107
MODEL_ORDER = 1                          # 2 only when a rung record admits it, in the same commit
REFOCUS_OBJECTIVE = "worst_field"        # or "composite"; reported as gauge.refocus_objective
OBJECTIVES = ("worst_field", "composite")
STORE_MAX = 16
STORE_PREFIX = "gauge-"
SAT_REL = _tc.SAT_REL
GAP_TOL = 1e-7                           # waves^2, duality gap of the minimax solve
MAX_ITER = 200
STRAGGLER_ITER = 5000                    # extra iterations for the draws still over tolerance after MAX_ITER (E6)
NEG_EPS = 1e-12                          # total RMS^2 below -NEG_EPS waves^2 is an invalid model
FOLD_WARN, FOLD_REFUSE = 0.05, 0.10
DELTA0_WARN, DELTA0_REFUSE = 0.003, 0.05
MAX_TRIALS = 200000
N_SIGMA = 2.0
COMPOSITION = "own_drop"
P1B_HELDOUT_REL = 0.0205
BIAS_NOTE = ("own_drop was graded on the 10x start only (held-out error 2.05 % relative); the norm runs above the "
             "polychromatic RWCE on tilted states, so the gauge yield reads low")
Z95 = 1.959963984540054
PUSH_LABELS = ("push_x", "push_y")
TILT_LABELS = ("tilt_x", "tilt_y")
COMP_LABELS = ("focus",) + PUSH_LABELS + TILT_LABELS      # the one table of compensator columns, in pack order
COMP_LIMIT = {"focus": "focus", "push_x": "push", "push_y": "push", "tilt_x": "tilt", "tilt_y": "tilt"}   # label -> limits key
FAIL_LABELS = PUSH_LABELS + TILT_LABELS                  # a draw at a push or tilt bound fails; a focus bound does not
PAR_LABEL = {1: "push_x", 2: "push_y", 3: "tilt_x", 4: "tilt_y"}   # coordinate-break Par# -> column label
FAMILY_INPUT, FAMILY_PARAM, FAMILY_SCOPE = "gauge_input", "gauge_param", "gauge_scope"
FAMILY_FOLD, FAMILY_SYM, FAMILY_COMPOSITE = "gauge_fold_too_large", "gauge_not_symmetric", "gauge_composite_off"
FAMILY_SOLVE, FAMILY_MODEL = "gauge_solve", "gauge_model_invalid"


def refuse(family, message):
    return {"ok": False, "error_family": family, "message": message}


def _safe(fn):
    """The module contract: a public function never raises; an unexpected error is a ``gauge_model_invalid`` refusal."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                                       # noqa: BLE001 -- the contract
            return {"ok": False, "error_family": FAMILY_MODEL,
                    "message": "%s failed: %s: %s" % (fn.__name__, type(exc).__name__, exc)}
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def is_refusal(x):
    return isinstance(x, dict) and x.get("ok") is False


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


# --------------------------------------------------------------------------- the report's percentile rule
def percentile_from_sorted(sorted_vals, p):
    """The report's percentile of an ascending array: ``(x_k + x_{k+1}) / 2`` at an integer ``k = p n / 100``."""
    n = len(sorted_vals)
    k = p / 100.0 * n
    k_int = int(round(k))
    if abs(k - k_int) < 1e-6:
        if k_int < 1:
            return float(sorted_vals[0])
        if k_int >= n:
            return float(sorted_vals[n - 1])
        return 0.5 * (float(sorted_vals[k_int - 1]) + float(sorted_vals[k_int]))
    return float(sorted_vals[min(n, max(1, math.ceil(k))) - 1])


# --------------------------------------------------------------------------- the Standard Zernike pieces (ported)
def noll_nm(j):
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
    rho, phi = np.asarray(rho, float), np.asarray(phi, float)
    n, m = noll_nm(j)
    if m == 0:
        return math.sqrt(n + 1) * radial(n, 0, rho)
    trig = np.cos(m * phi) if j % 2 == 0 else np.sin(m * phi)
    return math.sqrt(2 * (n + 1)) * radial(n, m, rho) * trig


def pupil_nodes(rings=6, arms=24):
    """Gauss-Legendre nodes in rho^2 x equispaced arms: ``(rho, phi, weight)``, weights summing to 1."""
    x, w = np.polynomial.legendre.leggauss(rings)
    u, wu = 0.5 * (x + 1.0), 0.5 * w
    return (np.repeat(np.sqrt(u), arms), np.tile(2.0 * np.pi * np.arange(arms) / arms, rings),
            np.repeat(wu, arms) / arms)


def zernike_matrix(terms, rho, phi):
    return np.stack([zernike_standard(j, rho, phi) for j in range(1, terms + 1)], axis=1)


def project_standard(opd, rho, phi, wt, terms):
    return (zernike_matrix(terms, rho, phi) * (np.asarray(wt)[:, None] * np.asarray(opd)[:, None])).sum(axis=0)


def rss_from(coefs, n_from=4, n_to=None):
    hi = len(coefs) if n_to is None else min(n_to, len(coefs))
    return math.sqrt(sum(float(c) * float(c) for c in coefs[n_from - 1:hi]))


# --------------------------------------------------------------------------- keys and widths
def row_key(identity):
    """'TYPE|s|s2|param': integers in original surface numbering, '-' for None; 13 and 13.0 give one string."""
    def part(v):
        if v is None:
            return "-"
        if isinstance(v, float) and v == int(v):
            return str(int(v))
        return str(v)
    t = list(identity) + [None] * (4 - len(identity))
    return "|".join(part(v) for v in t[:4])


def row_width_class(lo, hi):
    if not (_fin(lo) and _fin(hi)) or lo > hi:
        return "malformed"
    if lo == hi:
        return "zero" if lo == 0 else "fixed"
    if not (lo <= 0 <= hi):
        return "malformed"
    if lo == 0 or hi == 0:
        return "one_sided"
    return "two_sided"


def fit_term(lo, d_lo, hi, d_hi):
    """``(a, b, mode)`` of ``c(x) = a x + b x^2`` through (lo, d_lo), (0, 0), (hi, d_hi); one bound at 0 is linear."""
    if lo == 0.0 and hi == 0.0:
        return 0.0, 0.0, "zero_width"
    if lo == 0.0:
        return d_hi / hi, 0.0, "linear_one_sided"
    if hi == 0.0:
        return d_lo / lo, 0.0, "linear_one_sided"
    det = lo * hi * (hi - lo)
    return (d_lo * hi * hi - d_hi * lo * lo) / det, (lo * d_hi - hi * d_lo) / det, "quadratic"


# --------------------------------------------------------------------------- the pack
class Pack:
    """A validated pack: numpy arrays plus the row dicts. Built only by ``load_pack``."""


def _arr(x, shape, what):
    try:
        a = np.asarray(x, dtype=float)
    except (TypeError, ValueError):
        return refuse(FAMILY_INPUT, "%s is not a numeric array" % what)
    if a.shape != shape:
        return refuse(FAMILY_INPUT, "%s has shape %s, expected %s" % (what, a.shape, shape))
    if not np.isfinite(a).all():
        return refuse(FAMILY_INPUT, "%s holds a non-finite value" % what)
    return a


def _column(doc_col, shape, name, pr):
    if not isinstance(doc_col, dict):
        return refuse(FAMILY_INPUT, "columns.%s is not an object" % name)
    step = doc_col.get("h" if name == "focus" else "r")
    if not _fin(step) or step <= 0:
        return refuse(FAMILY_INPUT, "columns.%s step is missing or not positive" % name)
    zl, zh = _arr(doc_col.get("z_lo"), shape, name + ".z_lo"), _arr(doc_col.get("z_hi"), shape, name + ".z_hi")
    for z in (zl, zh):
        if is_refusal(z):
            return z
    return (zh - zl) / (2.0 * step)


def load_pack(doc, expect_lens_sha=None):
    """Validate ``doc`` (a ``wfd_pack v1`` dict) and return a ``Pack`` or a ``gauge_input`` refusal."""
    try:
        return _load_pack(doc, expect_lens_sha)
    except Exception as exc:                                               # the contract: never raise
        return refuse(FAMILY_INPUT, "the pack could not be read: %s: %s" % (type(exc).__name__, exc))


def _load_pack(doc, expect_lens_sha):
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return refuse(FAMILY_INPUT, "the pack schema is not %r" % SCHEMA)
    terms = (doc.get("zernike") or {}).get("terms")
    fields, waves = doc.get("fields"), doc.get("waves")
    if not (isinstance(terms, int) and not isinstance(terms, bool) and terms >= 4):
        return refuse(FAMILY_INPUT, "zernike.terms is missing or below 4")
    if not (isinstance(fields, list) and fields and isinstance(waves, list) and waves):
        return refuse(FAMILY_INPUT, "fields and waves must be non-empty lists")
    F, W = len(fields), len(waves)
    shape = (F, W, terms)
    z0 = _arr(doc.get("z0"), shape, "z0")
    if is_refusal(z0):
        return z0
    for what, items, keys in (("fields", fields, ("hx", "hy", "weight")), ("waves", waves, ("um", "weight"))):
        for i, it in enumerate(items):
            if not (isinstance(it, dict) and all(_fin(it.get(k)) for k in keys)):
                return refuse(FAMILY_INPUT, "%s[%d] lacks finite %s" % (what, i, "/".join(keys)))
    wgt = np.array([float(w["weight"]) for w in waves])
    if (wgt < 0).any() or wgt.sum() <= 0:
        return refuse(FAMILY_INPUT, "wave weights must be non-negative with a positive sum")
    fw = np.array([float(f["weight"]) for f in fields])
    if (fw < 0).any() or fw.sum() <= 0:
        return refuse(FAMILY_INPUT, "field weights must be non-negative with a positive sum")
    lens = doc.get("lens") or {}
    if expect_lens_sha is not None and lens.get("sha256") != expect_lens_sha:
        return refuse(FAMILY_INPUT, "the pack lens digest does not match the loaded lens")
    nominal = (doc.get("native") or {}).get("nominal_criterion")
    if not (_fin(nominal) and nominal >= 0):
        return refuse(FAMILY_INPUT, "native.nominal_criterion is missing or not a finite non-negative number")
    rows = doc.get("rows")
    if not isinstance(rows, list):
        return refuse(FAMILY_INPUT, "rows is not a list")
    K = len(rows)
    zlo, zhi = np.full((K,) + shape, np.nan), np.full((K,) + shape, np.nan)
    fix_a, fix_b, fix_c = np.zeros(K), np.zeros(K), np.zeros(K)
    keys, seen = [], set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or not isinstance(r.get("key"), str):
            return refuse(FAMILY_INPUT, "rows[%d] has no string key" % i)
        key, lo, hi = r["key"], r.get("lo"), r.get("hi")
        if key in seen:
            return refuse(FAMILY_INPUT, "row %s appears twice" % key)
        seen.add(key)
        keys.append(key)
        width = row_width_class(lo, hi)
        if width == "malformed":
            return refuse(FAMILY_INPUT, "row %s: malformed width (lo=%r, hi=%r; the bounds must be finite, ordered "
                                        "and bracket 0)" % (key, lo, hi))
        if r.get("width") != width or r.get("mode") not in ("edit", "fold"):
            return refuse(FAMILY_INPUT, "row %s: width %r / mode %r disagree with (lo, hi) = (%r, %r)"
                          % (key, r.get("width"), r.get("mode"), lo, hi))
        if r["mode"] == "edit":
            need = {"two_sided": ("z_lo", "z_hi"), "one_sided": ("z_hi",) if lo == 0 else ("z_lo",),
                    "fixed": ("z_lo",), "zero": ()}[width]
            for side in ("z_lo", "z_hi"):
                if side in need:
                    a = _arr(r.get(side), shape, "row %s %s" % (key, side))
                    if is_refusal(a):
                        return a
                    (zlo if side == "z_lo" else zhi)[i] = a
            if width == "fixed":
                zhi[i] = zlo[i]
        else:
            nat = r.get("native") or {}
            need = {"two_sided": ("w_lo", "w_hi"), "one_sided": ("w_hi",) if lo == 0 else ("w_lo",),
                    "fixed": ("w_lo",), "zero": ()}[width]
            if not all(_fin(nat.get(k)) for k in need):
                return refuse(FAMILY_INPUT, "fold row %s lacks finite native readings %s" % (key, list(need)))
            d_lo = (nat["w_lo"] ** 2 - nominal ** 2) if "w_lo" in need else 0.0
            d_hi = (nat["w_hi"] ** 2 - nominal ** 2) if "w_hi" in need else 0.0
            if width == "fixed":
                fix_c[i] = d_lo
            elif width != "zero":
                fix_a[i], fix_b[i], _ = fit_term(float(lo), d_lo, float(hi), d_hi)
    p = Pack()
    p.doc, p.keys, p.rows = doc, keys, rows
    p.F, p.W, p.T, p.K = F, W, terms, K
    p.fields, p.waves = fields, waves
    p.wave_w = wgt / wgt.sum()
    p.field_w = fw / fw.sum()
    p.z0, p.zlo, p.zhi = z0, zlo, zhi
    p.lo = np.array([float(r["lo"]) for r in rows])
    p.hi = np.array([float(r["hi"]) for r in rows])
    p.width = [r["width"] for r in rows]
    p.mode = [r["mode"] for r in rows]
    p.fold_a, p.fold_b, p.fold_c = fix_a, fix_b, fix_c
    p.nominal = float(nominal)
    p.bfl0 = float(doc["bfl0"]) if _fin(doc.get("bfl0")) else 0.0
    p.bfl_lo = np.array([float(r["bfl_lo"]) if _fin(r.get("bfl_lo")) else np.nan for r in rows])
    p.bfl_hi = np.array([float(r["bfl_hi"]) if _fin(r.get("bfl_hi")) else np.nan for r in rows])
    cols = dict(doc.get("columns") or {})
    if "focus" not in cols and isinstance(doc.get("refocus"), dict):
        cols["focus"] = doc["refocus"]
    unknown = sorted(repr(k) for k in cols if k not in COMP_LABELS)
    if unknown:
        return refuse(FAMILY_INPUT, "the pack carries unknown compensator column(s) %s; the gauge solves %s only"
                      % (unknown, list(COMP_LABELS)))
    labels, vecs = [], []
    for name in COMP_LABELS:
        if name in cols:
            c = _column(cols[name], shape, name, p)
            if is_refusal(c):
                return c
            labels.append(name)
            vecs.append(c)
    if not labels:
        return refuse(FAMILY_INPUT, "the pack has no compensator column (columns.focus is required)")
    p.comp_labels = labels
    p.C = np.stack(vecs, axis=-1)                                       # (F, W, T, p)
    p.limits = doc.get("limits") or {}
    p.topology_digest = lens.get("topology_digest")
    p.rule_id = (doc.get("budget") or {}).get("rule_id")
    p.variant_id = (doc.get("composition") or {}).get("variant_id")
    if p.variant_id not in (None, COMPOSITION):
        return refuse(FAMILY_INPUT, "the pack names composition %r; this gauge computes %r only" % (p.variant_id, COMPOSITION))
    sym = doc.get("symmetry") or {}
    p.partners = [list(x) for x in (sym.get("partners") or [])]
    p.mirror_sign = dict(sym.get("mirror_sign") or {})
    p.rot_sign = dict(sym.get("rot_sign") or {})
    p.sha = hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    p.G = _quad_form(p.wave_w, terms)
    return p


def _quad_form(om, T):
    """G (W*T, W*T) with ``own_drop``: rms2 = sum_w om_w sum_{n>=4} c_wn^2 + sum_w om_w |tau_w - tau_bar|^2."""
    W = len(om)
    G = np.zeros((W * T, W * T))
    for w in range(W):
        for n in range(3, T):
            G[w * T + n, w * T + n] += om[w]
    M = np.diag(om) - np.outer(om, om)
    for c in (1, 2):                                                    # Z2, Z3 (0-based 1, 2)
        for w in range(W):
            for w2 in range(W):
                G[w * T + c, w2 * T + c] += M[w, w2]
    return G


# --------------------------------------------------------------------------- draws
def draw_units(keys, n, seed=SEED, n_sigma=N_SIGMA):
    """(n, K) units in [-1, 1]: one stream per key, ``default_rng([seed, crc32(key)])``, truncated normal at +-n_sigma."""
    out = np.empty((n, len(keys)))
    for j, key in enumerate(keys):
        rng = np.random.default_rng([int(seed), zlib.crc32(str(key).encode("utf-8"))])
        z = rng.standard_normal(n)
        bad = np.abs(z) > n_sigma
        while bad.any():
            z[bad] = rng.standard_normal(int(bad.sum()))
            bad = np.abs(z) > n_sigma
        out[:, j] = z / n_sigma
    return out


def offsets(rows, U):
    """(n, K) physical offsets: centre + half * u (sigma = (hi - lo) / 4); fixed rows the constant lo; zero rows 0."""
    lo = np.array([float(r["lo"]) for r in rows])
    hi = np.array([float(r["hi"]) for r in rows])
    X = 0.5 * (lo + hi) + 0.5 * (hi - lo) * U
    const = np.array([r["width"] in ("fixed", "zero") for r in rows])
    return np.where(const[None, :], lo[None, :], X)


# --------------------------------------------------------------------------- Jacobian, vectors, RMS^2
def jacobian(pack, order):
    """``(J (F,W,T,K), H (F,W,T,K) | None, k (K,), C (F,W,T,p))``; rows that are folded, fixed or zero have J = 0."""
    F, W, T, K = pack.F, pack.W, pack.T, pack.K
    J = np.zeros((F, W, T, K))
    H = np.zeros((F, W, T, K)) if order == 2 else None
    k = np.zeros(K)
    for i in range(K):
        if pack.mode[i] != "edit":
            continue
        lo, hi, wd = pack.lo[i], pack.hi[i], pack.width[i]
        if wd == "two_sided":
            dl, dh = pack.zlo[i] - pack.z0, pack.zhi[i] - pack.z0
            if order == 2:
                a, b, _ = fit_term(lo, dl, hi, dh)
                J[..., i], H[..., i] = a, 2.0 * b
            else:
                J[..., i] = (pack.zhi[i] - pack.zlo[i]) / (hi - lo)
            bl, bh = pack.bfl_lo[i], pack.bfl_hi[i]
            k[i] = (bh - bl) / (hi - lo) if math.isfinite(bh - bl) else 0.0
        elif wd == "one_sided":
            b, z, bf = (hi, pack.zhi[i], pack.bfl_hi[i]) if lo == 0 else (lo, pack.zlo[i], pack.bfl_lo[i])
            J[..., i] = (z - pack.z0) / b
            k[i] = (bf - pack.bfl0) / b if math.isfinite(bf) else 0.0
    return J, H, k, pack.C


def _fixed_dz(pack):
    dz = np.zeros((pack.F, pack.W, pack.T))
    for i in range(pack.K):
        if pack.mode[i] == "edit" and pack.width[i] == "fixed":
            dz += pack.zlo[i] - pack.z0
    return dz


def field_vectors(pack, X, *, order):
    """(n, F, W, T): z0 + sum over fixed rows of dz + X J [+ 1/2 H x^2]. No compensator, no fold."""
    J, H, _k, _C = jacobian(pack, order)
    Z = pack.z0[None] + _fixed_dz(pack)[None] + np.einsum("nk,fwtk->nfwt", X, J)
    if H is not None:
        Z = Z + 0.5 * np.einsum("nk,fwtk->nfwt", X * X, H)
    return Z


def refocus_vectors(pack, X, Z):
    """Z + (X.k) * C_focus: the paraxial refocus of the row-gate comparison (dt = X.k through the focus column). It is
    NOT used by ``evaluate``, which solves its compensators per draw."""
    if "focus" not in pack.comp_labels:
        return refuse(FAMILY_INPUT, "the pack has no focus column")
    k = jacobian(pack, 1)[2]
    dt = X @ k
    return Z + dt[:, None, None, None] * pack.C[..., pack.comp_labels.index("focus")][None]


def poly_rms2(pack, Z):
    """(n, F): the ``own_drop`` polychromatic RMS^2 of the vectors only. Pure in Z; no fold."""
    z = np.asarray(Z, float)
    flat = z.reshape(z.shape[0], z.shape[1], -1)
    return np.einsum("nfi,ij,nfj->nf", flat, pack.G, flat)


def fold_rms2(pack, X):
    """(n,): sum over the fold rows of c2_i(x_i) at the identity draw; the ONLY place the fold is computed."""
    m = np.array([md == "fold" for md in pack.mode])
    if not m.any():
        return np.zeros(X.shape[0])
    x = X[:, m]
    return (pack.fold_a[m] * x + pack.fold_b[m] * x * x + pack.fold_c[m]).sum(axis=1)


def field_rms2(pack, X, Z):
    """(n, F): ``poly_rms2(Z)`` plus the fold, the per-user-field RMS^2 the worst-field criteria read."""
    return poly_rms2(pack, Z) + fold_rms2(pack, X)[:, None]


def _symmetry_images(pack, X):
    keys = {k: i for i, k in enumerate(pack.keys)}
    mir = X.copy()
    for k, s in pack.mirror_sign.items():
        if k in keys:
            mir[:, keys[k]] = s * X[:, keys[k]]
    rot, inv = X.copy(), X.copy()
    for kx, ky in pack.partners:
        if kx in keys and ky in keys:
            ix, iy = keys[kx], keys[ky]
            sx, sy = pack.rot_sign.get(kx, 1), pack.rot_sign.get(ky, 1)
            rot[:, ix], rot[:, iy] = sx * X[:, iy], sy * X[:, ix]
            inv[:, ix], inv[:, iy] = sy * X[:, iy], sx * X[:, ix]
    return mir, rot, inv


def composite(pack, X, Z_fn):
    """(n,) composite RMS^2, rule ``rot4/rms/points``: axis once, off-axis fields four times (identity, mirror,
    +/- rotation by the signed permutations of ``pack.symmetry``); the fold once; delta0 (if any) once."""
    mir, rot, inv = _symmetry_images(pack, X)
    on_axis = np.array([abs(f["hx"]) < 1e-12 and abs(f["hy"]) < 1e-12 for f in pack.fields])
    acc = poly_rms2(pack, Z_fn(X)).sum(axis=1)
    cnt = float(pack.F)
    for img in (mir, rot, inv):
        acc = acc + poly_rms2(pack, Z_fn(img))[:, ~on_axis].sum(axis=1)
        cnt += float((~on_axis).sum())
    d0 = (pack.doc.get("composite") or {}).get("delta0_rms2")
    return acc / cnt + fold_rms2(pack, X) + (float(d0) if _fin(d0) else 0.0)


def delta0(pack, composite_rms2_nominal):
    """The nominal match rule: ``(delta0_rms2 | None, verdict)``; verdict 'ok' (<= 0.3 %), 'offset' (<= 5 %) or a refusal."""
    nat = pack.nominal
    if nat <= 0:
        return None, refuse(FAMILY_COMPOSITE, "the native nominal criterion is zero")
    rel = abs(math.sqrt(max(composite_rms2_nominal, 0.0)) - nat) / nat
    if rel <= DELTA0_WARN:
        return None, "ok"
    if rel <= DELTA0_REFUSE:
        return nat * nat - composite_rms2_nominal, "offset"
    return None, refuse(FAMILY_COMPOSITE, "the gauge composite differs from the native nominal by %.1f %% (> 5 %%)"
                        % (100 * rel))


def folded_share(pack):
    """``{share, top}``: share of sum dW^2 carried by the fold rows; dW^2 of a row = mean of its two native changes."""
    tot, fold, rank = 0.0, 0.0, []
    for i, r in enumerate(pack.rows):
        nat = r.get("native") or {}
        dw = [(nat[k] - pack.nominal) ** 2 for k in ("w_lo", "w_hi") if _fin(nat.get(k))]
        s = sum(dw) / len(dw) if dw else 0.0
        tot += s
        if pack.mode[i] == "fold":
            fold += s
            rank.append((s, pack.keys[i]))
    top = [k for _s, k in sorted(rank, reverse=True)[:5]]
    return {"share": fold / tot if tot > 0 else 0.0, "top": top}


# --------------------------------------------------------------------------- compensators (4.2)
def field_quad(pack, U, C):
    """``a (n,F), b (n,F,p), A (F,p,p)``: rms2_f(u + C d) = a + 2 b.d + d.A.d for the PSD form v.G.v."""
    n, F = U.shape[0], U.shape[1]
    Uf = U.reshape(n, F, -1)
    Cf = C.reshape(F, -1, C.shape[-1])
    GU = np.einsum("ij,nfj->nfi", pack.G, Uf)
    return (np.einsum("nfi,nfi->nf", Uf, GU), np.einsum("fip,nfi->nfp", Cf, GU),
            np.einsum("fip,ij,fjq->fpq", Cf, pack.G, Cf))


def _dual(lam, a, b, A):
    S = np.einsum("nf,fpq->npq", lam, A)
    sb = np.einsum("nf,nfp->np", lam, b)
    d = -np.einsum("npq,nq->np", np.linalg.pinv(S, hermitian=True), sb)
    q = a + 2.0 * np.einsum("nfp,np->nf", b, d) + np.einsum("np,fpq,nq->nf", d, A, d)
    return d, q, (lam * q).sum(axis=1)


@_safe
def solve_comp(a, b, A, objective, weights=None, info=None, tol=GAP_TOL, max_iter=MAX_ITER, straggler_iter=STRAGGLER_ITER):
    """``(d (n,p), gap (n,))``. ``worst_field``: min_d max_f q_f by the dual over the simplex, d(lam) = -pinv(sum lam A)
    sum lam b (minimum norm; no ridge), batched exponentiated-gradient ascent with a per-draw step search, at most
    ``max_iter`` iterations, stop at gap <= ``tol``; the value is the primal max_f q_f(d(lam)). ``composite``:
    the weighted closed form (gap 0). A non-finite input is a ``gauge_solve`` refusal. ``info`` receives iterations."""
    if objective not in OBJECTIVES:
        return refuse(FAMILY_PARAM, "unknown refocus objective %r" % (objective,))
    if not (np.isfinite(a).all() and np.isfinite(b).all() and np.isfinite(A).all()):
        return refuse(FAMILY_SOLVE, "the quadratic form holds a non-finite value; nothing can be solved")
    n, F = a.shape
    if objective == "composite":
        w = np.full(F, 1.0 / F) if weights is None else np.asarray(weights, float) / float(np.sum(weights))
        S = np.einsum("f,fpq->pq", w, A)
        sb = np.einsum("f,nfp->np", w, b)
        d = -sb @ np.linalg.pinv(S, hermitian=True).T
        if info is not None:
            info["iterations"] = 0
        return d, np.zeros(n)
    lam = np.full((n, F), 1.0 / F)
    d, q, g = _dual(lam, a, b, A)
    eta = 1.0 / np.maximum(q.max(axis=1) - q.min(axis=1), 1e-12)
    it = 0
    for it in range(1, max_iter + straggler_iter + 1):      # past max_iter only the draws still over tol are iterated (E6)
        act = np.where(q.max(axis=1) - g > tol)[0]
        if act.size == 0:
            it -= 1
            break
        la, aa, ba = lam[act], a[act], b[act]
        qa, ga, ea = q[act], g[act], eta[act]
        best = None
        for k in (-2, 0, 2, 4):
            e = ea * 2.0 ** k
            new = la * np.exp(np.clip(e[:, None] * (qa - qa.max(axis=1, keepdims=True)), -60.0, 0.0))
            new = new / new.sum(axis=1, keepdims=True)
            d2, q2, g2 = _dual(new, aa, ba, A)
            if best is None:
                best = [new, d2, q2, g2, e]
            else:
                m = g2 > best[3]
                best = [np.where(m.reshape((-1,) + (1,) * (x.ndim - 1)), x, y)
                        for x, y in zip((new, d2, q2, g2, e), best)]
        ok = best[3] >= ga
        lam[act] = np.where(ok[:, None], best[0], la)
        d[act] = np.where(ok[:, None], best[1], d[act])
        q[act] = np.where(ok[:, None], best[2], qa)
        g[act] = np.where(ok, best[3], ga)
        eta[act] = np.where(ok, best[4], ea * 0.1)
    if info is not None:
        info["iterations"] = it
    return d, q.max(axis=1) - g


def any_clipped(at_min, at_max):
    """(n,) bool: a draw with ANY compensator (focus or push) at a bound (E7). The duality gap bounds the UNCLIPPED minimax
    solve, so a clipped draw's value is not covered by it."""
    return (np.asarray(at_min, bool) | np.asarray(at_max, bool)).any(axis=1)


def undetermined(primal, gap, clipped, threshold):
    """(n,) bool: a draw still over ``GAP_TOL`` whose outcome could flip (E6). ``primal`` is max_f q_f (an upper bound on
    the minimax value), so the true value lies in [primal - gap, primal]; the draw is undetermined when that interval
    straddles threshold^2, or when any compensator is clipped (E7: the gap bounds the unclipped solve; ``clipped`` is
    ``any_clipped``). A converged draw is never undetermined; an unconverged, unclipped draw whose whole interval is on one
    side of threshold^2 keeps its value."""
    primal, gap = np.asarray(primal, float), np.asarray(gap, float)
    over = gap > GAP_TOL
    t2 = float(threshold) ** 2
    straddle = (primal - gap <= t2) & (primal > t2)
    return over & (straddle | np.asarray(clipped, bool))


def clip_comp(d, limits):
    """``(d_clipped, at_min, at_max)``; ``limits`` is (p, 2) of (lo, hi). A DOF is at a bound when
    ``d >= hi (1 - SAT_REL)`` / ``d <= lo (1 - SAT_REL)`` (the ``cpar_trial_bounds`` rule); the value is clipped."""
    lim = np.asarray(limits, float)
    lo, hi = lim[:, 0], lim[:, 1]
    at_max = d >= hi * (1.0 - SAT_REL)
    at_min = d <= lo * (1.0 - SAT_REL)
    return np.clip(d, lo, hi), at_min, at_max


# --------------------------------------------------------------------------- criteria and statistics
def wilson(k, n, z=Z95):
    if n <= 0:
        return [None, None]
    p = k / n
    den = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [max(0.0, c - h), min(1.0, c + h)]


def p90_se(values, at_bound=None):
    """(u - l) / 4 of the +-2 sigma rank band of the 0.9 order statistic (a failed trial sorts as +inf, like P90*);
    None when a band end is not finite."""
    if at_bound is not None:
        values = [math.inf if f else v for v, f in zip(values, at_bound)]
    lo, hi = _pick.p90_interval(values)
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return None
    return (hi - lo) / 4.0


def criteria(worst, at_bound, *, threshold):
    """The per-trial worst-field statistics: ``{stats, mean, std, best, worst, cumulative_probability, ses, wilson,
    tail}``. ``stats`` is ``_tolerance_pick.stats`` unchanged; the composite statistics are not part of the output."""
    vals, ab = [float(v) for v in worst], [bool(x) for x in at_bound]
    n = len(vals)
    st = _pick.stats(vals, ab, threshold)
    srt = np.sort(np.asarray(vals))
    mean, sd = float(srt.mean()), float(srt.std(ddof=1))
    tail = float(((np.asarray(vals) > 2.0 * threshold) | np.asarray(ab)).mean())
    p_y = st["yield"]
    return {"stats": st, "mean": mean, "std": sd, "best": float(srt[0]), "worst": float(srt[-1]),
            "cumulative_probability": {"%d%%" % p: percentile_from_sorted(srt, p) for p in (90, 80, 50, 20, 10)},
            "ses": {"yield": math.sqrt(p_y * (1 - p_y) / n), "tail": math.sqrt(tail * (1 - tail) / n),
                    "mean": sd / math.sqrt(n), "std": sd / math.sqrt(2 * (n - 1)), "p90_se": p90_se(vals, ab)},
            "wilson": wilson(st["n_pass"], n), "tail": tail}


def per_field(rms_f, at_bound, fields, threshold):
    """The served ``per_field`` block: one ``_tolerance_pick.stats`` call per field; ``worst_field`` = the lowest
    yield (a tie goes to the higher P90)."""
    out = []
    ab = [bool(x) for x in at_bound]
    for f in range(rms_f.shape[1]):
        s = _pick.stats([float(v) for v in rms_f[:, f]], ab, threshold)
        out.append({"index": f, "hx": fields[f]["hx"], "hy": fields[f]["hy"], "n_trials": s["n_trials"],
                    "n_pass": s["n_pass"], "yield": s["yield"], "p90": s["p90"]})
    worst = min(range(len(out)), key=lambda i: (out[i]["yield"], -out[i]["p90"]))
    return _pick._clean({"threshold": threshold, "fields": out, "worst_field": worst})


def _check_limits(pack, p_labels):
    lim = pack.limits
    rows = []
    for lab in p_labels:
        if lab == "focus":
            v = lim.get("focus")
            if not (isinstance(v, (list, tuple)) and len(v) == 2 and all(_fin(x) for x in v) and v[0] < v[1]):
                return refuse(FAMILY_INPUT, "limits.focus must be [lo, hi]")
            rows.append((float(v[0]), float(v[1])))
        else:
            key = COMP_LIMIT[lab]
            r = lim.get(key)
            if not (_fin(r) and r > 0):
                return refuse(FAMILY_INPUT, "limits.%s must be a positive number" % key)
            rows.append((-float(r), float(r)))
    return np.array(rows)


def _gauge_id(pack, seed, n, threshold, order, objective):
    blob = json.dumps([pack.sha, seed, n, threshold, order, pack.variant_id, objective], sort_keys=True)
    return STORE_PREFIX + hashlib.sha256(blob.encode()).hexdigest()[:32]


def evaluate(pack, *, threshold, n=10000, order=None, objective=REFOCUS_OBJECTIVE, full=False):
    """The in-loop gauge: draws, vectors, the per-draw compensator solve, clip and fail, then the statistics of the
    judge's per-trial value ``max_f sqrt(rms2_f(d) + fold)``. Never raises."""
    try:
        return _evaluate(pack, threshold, n, MODEL_ORDER if order is None else order, objective, full)
    except Exception as exc:
        return refuse(FAMILY_MODEL, "the evaluation failed: %s: %s" % (type(exc).__name__, exc))


def _evaluate(pack, threshold, n, order, objective, full):
    if is_refusal(pack):
        return pack
    if not _fin(threshold) or threshold <= 0:
        return refuse(FAMILY_PARAM, "threshold must be a positive finite number")
    if isinstance(n, bool) or not isinstance(n, int) or not 2 <= n <= MAX_TRIALS:
        return refuse(FAMILY_PARAM, "trials must be an integer in [2, %d]" % MAX_TRIALS)
    if order not in (1, 2):
        return refuse(FAMILY_PARAM, "order must be 1 or 2")
    if objective not in OBJECTIVES:
        return refuse(FAMILY_PARAM, "unknown refocus objective %r" % (objective,))
    fs = folded_share(pack)
    warnings = []
    if fs["share"] > FOLD_REFUSE:
        return refuse(FAMILY_FOLD, "folded rows carry %.1f %% of the change (> 10 %%): %s"
                      % (100 * fs["share"], ", ".join(fs["top"])))
    if fs["share"] > FOLD_WARN:
        warnings.append("folded_share %.1f %% (> 5 %%): %s" % (100 * fs["share"], ", ".join(fs["top"])))
    lim = _check_limits(pack, pack.comp_labels)
    if is_refusal(lim):
        return lim
    U = draw_units(pack.keys, n)
    X = offsets(pack.rows, U)
    Z = field_vectors(pack, X, order=order)
    fold = fold_rms2(pack, X)
    a, b, A = field_quad(pack, Z, pack.C)
    info = {}
    sol = solve_comp(a, b, A, objective, weights=pack.field_w, info=info, tol=GAP_TOL, max_iter=MAX_ITER)
    if is_refusal(sol):
        return sol
    d, gap = sol
    if not np.isfinite(d).all() or not np.isfinite(gap).all():
        return refuse(FAMILY_SOLVE, "the compensator solve returned a non-finite value")
    dc, at_min, at_max = clip_comp(d, lim)
    q = a + 2.0 * np.einsum("nfp,np->nf", b, dc) + np.einsum("np,fpq,nq->nf", dc, A, dc) + fold[:, None]
    und = undetermined(q.max(axis=1), gap, any_clipped(at_min, at_max), threshold)
    n_unconverged = int((gap > GAP_TOL).sum())
    if und.any():
        return refuse(FAMILY_SOLVE, "%d of %d draws are over the duality-gap tolerance %.0e after %d iterations and their "
                      "pass/fail is undetermined (the interval [primal - gap, primal] straddles threshold^2, or the draw has a "
                      "compensator clipped at a bound); max gap %.2e" % (int(und.sum()), n, GAP_TOL, MAX_ITER + STRAGGLER_ITER,
                                                                         float(gap[und].max())))
    if (q < -NEG_EPS).any():
        return refuse(FAMILY_MODEL, "a total RMS^2 below -%.0e waves^2 (min %.3e): the linear model is invalid"
                      % (NEG_EPS, float(q.min())))
    n_clamped = int(((q < 0) & (q >= -NEG_EPS)).sum())
    rms_f = np.sqrt(np.maximum(q, 0.0))
    worst = rms_f.max(axis=1)
    push_cols = [i for i, lab in enumerate(pack.comp_labels) if lab in FAIL_LABELS]
    at_bound = ((at_min | at_max)[:, push_cols].any(axis=1) if push_cols else np.zeros(n, bool))
    vals, ab = [float(v) for v in worst], [bool(x) for x in at_bound]
    crit = criteria(worst, at_bound, threshold=threshold)
    st = crit["stats"]
    nom = float(np.sqrt(poly_rms2(pack, pack.z0[None])[0].max()))
    focus_i = pack.comp_labels.index("focus") if "focus" in pack.comp_labels else None
    fb = int((at_min | at_max)[:, focus_i].sum()) if focus_i is not None else 0
    comp_stats = [{"label": lab, "nominal": 0.0, "mean": float(dc[:, i].mean()), "std_dev": float(dc[:, i].std(ddof=1))}
                  for i, lab in enumerate(pack.comp_labels)]
    axes = [{"label": pack.comp_labels[i], "n_at_min": int(at_min[:, i].sum()), "n_at_max": int(at_max[:, i].sum())}
            for i in range(len(pack.comp_labels))]
    gid = _gauge_id(pack, SEED, n, threshold, order, objective)
    res = {"ok": True, "nominal_criterion": nom,
           "monte_carlo": {"mean": crit["mean"], "std_dev": crit["std"], "best": crit["best"], "worst": crit["worst"],
                           "cumulative_probability": crit["cumulative_probability"]},
           "yield_stats": _pick._clean({k: st[k] for k in ("n_trials", "n_pass", "yield", "n_at_bound", "p90")}),
           "per_field": per_field(rms_f, at_bound, pack.fields, threshold),
           "compensator_statistics": comp_stats, "axes": axes, "values": vals, "at_bound": ab,
           "gauge": {"role": "screen", "id": gid, "order": order, "refocus_objective": objective, "seed": SEED,
                     "n": n, "threshold": threshold, "pack_sha": pack.sha, "topology_digest": pack.topology_digest,
                     "pairing_key": [pack.rule_id, SEED, n, sorted([k, w] for k, w in zip(pack.keys, pack.width))],
                     "composition": {"variant_id": COMPOSITION, "route": "batch_opd", "terms": pack.T,
                                     "p1b_heldout_rel": P1B_HELDOUT_REL, "bias_note": BIAS_NOTE},
                     "folded_share": fs["share"],
                     "compensators": sorted([lab, float(lo), float(hi)] for lab, (lo, hi) in zip(pack.comp_labels, lim)),
                     "focus_at_bound": fb, "warnings": warnings,
                     "solve": {"objective": objective, "max_gap": float(gap.max()), "n_unconverged": n_unconverged, "iterations": info.get("iterations"),
                               "n_clamped": n_clamped},
                     "ses": crit["ses"], "wilson": crit["wilson"], "tail": crit["tail"]},
           "_arrays": {"worst": worst, "field_rms": rms_f, "at_bound": at_bound}}
    if full:
        res["per_trial"], res["per_trial_at_bound"] = vals, ab
    return res


# --------------------------------------------------------------------------- the result store (3.8)
_STORE = OrderedDict()
STORE_MAX_BYTES = 32 * 1024 * 1024   # the byte budget over every stored result's `_arrays` (beside the STORE_MAX slot count)


def _store_bytes():
    return sum(int(a.nbytes) for v in _STORE.values() for a in (v.get("_arrays") or {}).values())


@_safe
def store_put(result):
    """Keep a result's identity block and per-draw arrays under ``gauge.id``; the oldest beyond ``STORE_MAX`` goes."""
    if is_refusal(result) or "_arrays" not in result:
        return refuse(FAMILY_PARAM, "only an evaluated gauge result can be stored")
    gid = result["gauge"]["id"]
    _STORE.pop(gid, None)
    _STORE[gid] = {"gauge": result["gauge"], "yield_stats": result["yield_stats"], "_arrays": result["_arrays"]}
    # Oldest first until both limits hold. The newest result is always kept, even when it alone exceeds the byte budget.
    while len(_STORE) > 1 and (len(_STORE) > STORE_MAX or _store_bytes() > STORE_MAX_BYTES):
        _STORE.popitem(last=False)
    return {"ok": True, "id": gid}


def store_get(gid):
    """The stored result for ``gid`` or a ``gauge_param`` refusal with the rerun remedy (a non-gauge id refuses too)."""
    got = _STORE.get(gid) if isinstance(gid, str) and gid.startswith(STORE_PREFIX) else None
    if got is None:
        return refuse(FAMILY_PARAM, "baseline %r is not in the gauge store (unknown or expired); rerun the baseline "
                                    "gauge in this session" % (gid,))
    return got


def store_clear():
    _STORE.clear()


# --------------------------------------------------------------------------- pairing and the keep cascade (3.8)
def _diff(value, se):
    ok = value is not None and se is not None and math.isfinite(value) and math.isfinite(se)
    return {"value": value if ok else None, "se": se if ok else None,
            "resolved": bool(ok and abs(value) > 2.0 * se)}


def _yield_diff(pass_a, pass_b, paired_mode):
    n_a, n_b = len(pass_a), len(pass_b)
    if paired_mode:
        b, c = int((pass_b & ~pass_a).sum()), int((pass_a & ~pass_b).sum())
        dp = (b - c) / n_a
        se = math.sqrt(max(b + c - (b - c) ** 2 / n_a, 0.0)) / n_a
        out = _diff(dp, se)
        out["resolved"] = bool(out["resolved"] and b + c >= 10)
        return out
    pa, pb = pass_a.mean(), pass_b.mean()
    pooled = (pass_a.sum() + pass_b.sum()) / (n_a + n_b)
    return _diff(float(pb - pa), math.sqrt(pooled * (1 - pooled) * (1.0 / n_a + 1.0 / n_b)))


def _compensators_differ(ga, gb):
    """True when the solved compensator sets (label, lo, hi) differ, or either side does not carry them (a missing set
    never pairs, like a None topology)."""
    ca, cb = ga.get("compensators"), gb.get("compensators")
    return ca is None or cb is None or ca != cb


def _mismatch(ra, rb):
    ga, gb = ra["gauge"], rb["gauge"]
    pa, pb = ga["pairing_key"], gb["pairing_key"]
    for name, i in (("rule_id", 0), ("seed", 1), ("n", 2), ("row set", 3)):
        if pa[i] != pb[i]:
            return name
    if _compensators_differ(ga, gb):
        return "compensator"
    if ga.get("topology_digest") is None or ga.get("topology_digest") != gb.get("topology_digest"):
        return "topology"
    if ga.get("threshold") != gb.get("threshold"):
        return "threshold"
    return None


@_safe
def paired(res_a, res_b):
    """The keep cascade on two results' per-draw arrays (A = baseline, B = candidate)."""
    for r in (res_a, res_b):
        if is_refusal(r) or "_arrays" not in r:
            return refuse(FAMILY_PARAM, "paired needs two gauge results that carry their per-draw arrays")
    reason = _mismatch(res_a, res_b)
    mode = reason is None
    ar, br = res_a["_arrays"], res_b["_arrays"]
    if ar["field_rms"].shape[1] != br["field_rms"].shape[1]:
        return refuse(FAMILY_PARAM, "the two results have different field counts")
    n_a, n_b = len(ar["worst"]), len(br["worst"])
    thr_a, thr_b = res_a["gauge"]["threshold"], res_b["gauge"]["threshold"]

    def passed(arr, f, thr):
        v = arr["worst"] if f is None else arr["field_rms"][:, f]
        return (~arr["at_bound"]) & (v <= thr)

    d_yield = _yield_diff(passed(ar, None, thr_a), passed(br, None, thr_b), mode)
    p_a, p_b = res_a["yield_stats"]["yield"], res_b["yield_stats"]["yield"]
    sa, sb = res_a["gauge"]["ses"]["p90_se"], res_b["gauge"]["ses"]["p90_se"]
    pa90, pb90 = res_a["yield_stats"]["p90"], res_b["yield_stats"]["p90"]
    if None in (sa, sb, pa90, pb90):
        d_p90 = _diff(None, None)
    else:
        d_p90 = _diff(float(pb90 - pa90), math.sqrt(sa * sa + sb * sb))
    if mode:
        dd = br["worst"] - ar["worst"]
        d_mean = _diff(float(dd.mean()), float(dd.std(ddof=1)) / math.sqrt(n_a))
        td = ((br["worst"] > 2 * thr_b) | br["at_bound"]).astype(float) - ((ar["worst"] > 2 * thr_a) | ar["at_bound"])
        d_tail = {"value": float(td.mean()), "se": float(td.std(ddof=1)) / math.sqrt(n_a)}
    else:
        sda, sdb = float(ar["worst"].std(ddof=1)), float(br["worst"].std(ddof=1))
        d_mean = _diff(float(br["worst"].mean() - ar["worst"].mean()), math.sqrt(sda ** 2 / n_a + sdb ** 2 / n_b))
        ta, tb = res_a["gauge"]["tail"], res_b["gauge"]["tail"]
        d_tail = {"value": tb - ta, "se": math.sqrt(ta * (1 - ta) / n_a + tb * (1 - tb) / n_b)}
    if 0.05 <= p_a <= 0.95 and 0.05 <= p_b <= 0.95:
        stat, d, sign = "yield", d_yield, 1
    elif d_p90["resolved"]:
        stat, d, sign = "p90", d_p90, -1
    else:
        stat, d, sign = "mean", d_mean, -1
    verdict = "tie"
    if d["resolved"]:
        verdict = "gain" if d["value"] * sign > 0 else "loss"
    fields = []
    for f in range(ar["field_rms"].shape[1]):
        fields.append({"index": f, "d_yield": _yield_diff(passed(ar, f, thr_a), passed(br, f, thr_b), mode)})
    no_loss = not any(x["d_yield"]["resolved"] and x["d_yield"]["value"] < 0 for x in fields)
    topo_a, topo_b = res_a["gauge"].get("topology_digest"), res_b["gauge"].get("topology_digest")
    wrong_bar = thr_a != thr_b
    wrong_lens = topo_a is None or topo_a != topo_b
    wrong_comp = _compensators_differ(res_a["gauge"], res_b["gauge"])
    if wrong_bar or wrong_lens or wrong_comp:   # different bars / lenses / compensator sets, whatever `reason` names first
        verdict, no_loss, stat = None, None, None   # `statistic` names the block that would decide the verdict
        # the bar-dependent diffs compare two different bars; on a different lens every diff compares two lenses
        d_yield, d_tail = None, None
        for entry in fields:
            entry["d_yield"] = None
    if wrong_lens or wrong_comp:   # a different compensator set changes every value, so no diff of the two is a diff of the lens
        d_p90, d_mean = None, None
    return {"pairing": "paired" if mode else "independent", "reason": reason, "d_yield": d_yield, "d_p90": d_p90,
            "d_mean": d_mean, "d_tail": d_tail, "statistic": stat, "verdict": verdict,
            "n_common": min(n_a, n_b) if mode else 0,
            "topology_same": topo_a is not None and topo_a == topo_b,
            "fields": fields, "no_field_loss": no_loss}
