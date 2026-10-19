"""glass_math.py -- first-order colour and field-curvature sums for a glass-matching screen.

Engine-free paraxial maths over the committed glass catalogue (doc/glass-matching/
glass-matching-math.md). Two public calls and one exception; a library, so it RAISES
``GlassMathError`` and the caller maps ``reason`` to a refusal:

- ``band_properties(conn, medium, short, reference, long, p, q)`` -> Abbe number (3.1)
  and partial dispersion (3.2) over the caller's own band;
- ``contributions(conn, system)`` -> per-surface and per-element axial / lateral colour
  and Petzval sums (4.1, 4.2a, 6.1, 6.1a), the conversions to focus and image-height
  shifts (4.3, 4.4), and a re-aimed direct trace to compare them with.

``system`` is the dict of ``SCHEMA``: mm, 1/mm, curvature positive with the centre to the
right, ``medium`` is the glass AFTER a surface (None = air). Elements and cemented groups
are DERIVED from ``medium`` (a run of rows with one medium; a run of non-air rows).

Every colour quantity is one column per wavelength RELATIVE TO the reference wavelength,
so any pair is a difference of two columns. Everything below the public calls is private.
"""
import json
import math

from .errors import ReferenceLayerError
from .glass_build import in_band
from .glass_dispersion import index_at

SCHEMA = "optivibe.paraxial-system/2"

# A trace is degenerate when the quantity it divides by is this small next to its scale.
_REL_TOL = 1e-12


class GlassMathError(ReferenceLayerError):
    """``reason``: invalid_system | glass_not_evaluable | degenerate_trace.

    ``where`` names the dict field path or argument at fault (or None).
    """

    error_family = "glass_math"

    def __init__(self, reason, message, where=None):
        super().__init__(f"{reason}: {message}" + (f" [{where}]" if where else ""))
        self.reason = reason
        self.where = where


def _fail(message, where=None):
    raise GlassMathError("invalid_system", message, where)


def _is_num(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:                                  # an int too large for a float
        return False


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# --- glass lookup -----------------------------------------------------------------------


def _medium_key(medium, where):
    """(catalog, name) of a ``{catalog, name}`` dict; the STORED forms, matched exactly."""
    if not isinstance(medium, dict):
        _fail("medium must be a {catalog, name} dict", where)
    for k in ("catalog", "name"):
        v = medium.get(k)
        if not isinstance(v, str) or not v.strip():
            _fail(f"medium '{k}' must be a non-blank string", where)
    return medium["catalog"], medium["name"]


def _lookup(conn, key, where):
    row = conn.execute(
        "SELECT formula, cd, min_wave, max_wave FROM glass WHERE catalog = ? AND name = ?",
        key,
    ).fetchone()
    if row is None:
        raise GlassMathError("glass_not_evaluable", f"no glass {key[1]!r} in {key[0]!r}", where)
    return row[0], (json.loads(row[1]) if row[1] else None), row[2], row[3]


def _index(glass, wave, where):
    formula, cd, lo, hi = glass
    n = index_at(formula, cd, wave) if cd is not None and in_band(lo, hi, wave) else None
    if n is None:
        raise GlassMathError(
            "glass_not_evaluable", f"no index at {wave} um (glass band {lo}-{hi})", where
        )
    return n


def band_properties(conn, medium, short_um, reference_um, long_um, p_um, q_um):
    """Abbe number (3.1) and partial dispersion P_{p,q} (3.2) over the band short..long.

    ``p_um`` and ``q_um`` may lie outside the band but must lie in the glass's own band.
    Both values are None when n(short) == n(long) exactly.
    """
    key = _medium_key(medium, "medium")
    for name, v in (("short_um", short_um), ("reference_um", reference_um),
                    ("long_um", long_um), ("p_um", p_um), ("q_um", q_um)):
        if not _is_num(v) or v <= 0:
            _fail(f"{name} must be a positive finite number", name)
    if not short_um < long_um:
        _fail("short_um must be below long_um", "long_um")
    if not short_um <= reference_um <= long_um:
        _fail("reference_um must lie within [short_um, long_um]", "reference_um")
    glass = _lookup(conn, key, "medium")
    ns, n0, nl, np_, nq = (_index(glass, w, "medium")
                           for w in (short_um, reference_um, long_um, p_um, q_um))
    if ns == nl:
        return {"abbe": None, "partial": None}
    return {"abbe": (n0 - 1.0) / (ns - nl), "partial": (np_ - nq) / (ns - nl)}


# --- validation -------------------------------------------------------------------------


def _validate(system):
    """Check the dict against the schema; return the parsed pieces."""
    if not isinstance(system, dict):
        _fail("system must be a dict")

    def get(d, key, path):
        if key not in d:
            _fail(f"'{key}' is required", path)
        return d[key]

    if get(system, "schema", "schema") != SCHEMA:
        _fail(f"schema must equal {SCHEMA!r}", "schema")
    waves = get(system, "wavelengths_um", "wavelengths_um")
    if not isinstance(waves, list) or not waves:
        _fail("wavelengths_um must be a non-empty list", "wavelengths_um")
    for i, w in enumerate(waves):
        if not _is_num(w) or w <= 0:
            _fail("wavelength must be a positive finite number", f"wavelengths_um[{i}]")
        if i and w <= waves[i - 1]:
            _fail("wavelengths_um must be strictly increasing", f"wavelengths_um[{i}]")
    ref = get(system, "reference_index", "reference_index")
    if not _is_int(ref) or not 0 <= ref < len(waves):
        _fail("reference_index must be an index into wavelengths_um", "reference_index")

    dist = get(system, "object_distance", "object_distance")
    if isinstance(dist, str):
        if dist != "inf":
            _fail("object_distance must be a number or 'inf'", "object_distance")
        dist = None
    elif not _is_num(dist) or dist <= 0:
        _fail("object_distance must be > 0 or 'inf'", "object_distance")
    field = get(system, "field", "field")
    want = "slope" if dist is None else "height"
    if not isinstance(field, dict) or list(field) != [want]:
        _fail(f"field must be exactly {{'{want}': number}} for this object", "field")
    if not _is_num(field[want]):
        _fail("field value must be a finite number", "field")

    height = get(system, "stop_marginal_height", "stop_marginal_height")
    if not _is_num(height) or height <= 0:
        _fail("stop_marginal_height must be > 0", "stop_marginal_height")
    image_n = get(system, "image_space_index", "image_space_index")
    if not _is_num(image_n) or image_n < 1:
        _fail("image_space_index must be >= 1", "image_space_index")

    rows = get(system, "surfaces", "surfaces")
    if not isinstance(rows, list) or not rows:
        _fail("surfaces must be a non-empty list", "surfaces")
    labels, curv, thick, keys = [], [], [], []
    for i, row in enumerate(rows):
        at = f"surfaces[{i}]"
        if not isinstance(row, dict):
            _fail("surface row must be a dict", at)
        label = get(row, "surface", f"{at}.surface")
        if not _is_int(label) or (labels and label <= labels[-1]):
            _fail("surface labels must be ints, strictly increasing", f"{at}.surface")
        c = get(row, "curvature", f"{at}.curvature")
        t = get(row, "thickness", f"{at}.thickness")
        if not _is_num(c):
            _fail("curvature must be a finite number", f"{at}.curvature")
        if not _is_num(t) or t < 0:
            _fail("thickness must be a finite number >= 0", f"{at}.thickness")
        med = get(row, "medium", f"{at}.medium")
        labels.append(label)
        curv.append(float(c))
        thick.append(float(t))
        keys.append(None if med is None else _medium_key(med, f"{at}.medium"))
    if keys[-1] is not None:
        _fail("the last surface's medium must be null", f"surfaces[{len(rows) - 1}].medium")
    stop = get(system, "stop_surface", "stop_surface")
    if not _is_int(stop) or stop not in labels:
        _fail("stop_surface must be the 'surface' label of one row", "stop_surface")
    return {
        "waves": waves, "ref": ref, "dist": dist, "field": float(field[want]),
        "stop": labels.index(stop), "height": float(height), "image_n": float(image_n),
        "labels": labels, "c": curv, "t": thick, "keys": keys,
    }


# --- paraxial trace ---------------------------------------------------------------------


def _trace(c, t, n_after, y, u):
    """Trace (y, u) from surface 1 (index 1 before it). Rows (y, u, n_before) AT each surface,
    then the image-surface height and the final slope (2.1)."""
    rows, nb = [], 1.0
    for cj, tj, na in zip(c, t, n_after):
        rows.append((y, u, nb))
        u = (nb * u - y * (na - nb) * cj) / na
        y += tj * u
        nb = na
    return rows, y, u


def _solve(s, n_after):
    """Aim the marginal and chief rays for one set of indices, by superposition of two
    trial rays; return their rows, the chief image-surface height and the final slopes."""
    if s["dist"] is None:
        m0, p0 = (1.0, 0.0), (0.0, s["field"])
    else:
        m0, p0 = (s["dist"], 1.0), (s["field"], 0.0)
    m_rows, _, _ = _trace(s["c"], s["t"], n_after, *m0)
    p_rows, _, _ = _trace(s["c"], s["t"], n_after, *p0)
    m_stop, p_stop = m_rows[s["stop"]][0], p_rows[s["stop"]][0]
    if abs(m_stop) <= _REL_TOL * max(abs(r[0]) for r in m_rows):
        raise GlassMathError("degenerate_trace", "the stop is conjugate to the object", "stop_surface")
    a, b = s["height"] / m_stop, p_stop / m_stop
    m_rows, _, mu = _trace(s["c"], s["t"], n_after, a * m0[0], a * m0[1])
    c_rows, c_img, cu = _trace(s["c"], s["t"], n_after, p0[0] - b * m0[0], p0[1] - b * m0[1])
    if abs(mu) <= _REL_TOL * max(abs(r[1]) for r in m_rows):
        raise GlassMathError("degenerate_trace", "the system is afocal (u'_k = 0)")
    return m_rows, c_rows, c_img, mu


def _runs(keys, same):
    """Number maximal runs of rows (1..) for which ``same`` holds with the previous row."""
    ids, count = [], 0
    for j, key in enumerate(keys):
        if key is None:
            ids.append(None)
            continue
        if j == 0 or keys[j - 1] is None or not same(keys[j - 1], key):
            count += 1
        ids.append(count)
    return ids


def _element_power(s, n_glass, first, last):
    """Power of the glass on rows first..last traced alone in air, through the row after."""
    rows = range(first, last + 2)
    n_after = [n_glass] * (last - first + 1) + [1.0]
    t = [s["t"][j] for j in rows[:-1]] + [0.0]
    _, _, u = _trace([s["c"][j] for j in rows], t, n_after, 1.0, 0.0)
    return -u


# --- contributions ----------------------------------------------------------------------


def contributions(conn, system):
    """Colour and Petzval sums of a paraxial system (section 4 of the schema contract).

    Returns ``{wavelengths_um, reference_index, reference_replaced, index_table, surfaces,
    elements, totals}``; the colour lists are None with a single wavelength.
    """
    s = _validate(system)
    waves, nw, c, keys, image_n = s["waves"], len(s["waves"]), s["c"], s["keys"], s["image_n"]
    ref, replaced = s["ref"], False
    if nw >= 3 and ref in (0, nw - 1):
        mid = 0.5 * (waves[0] + waves[-1])
        dist = [abs(waves[i] - mid) for i in range(1, nw - 1)]
        # The tie tolerance is relative to the mid wavelength, not to the distances: distances of
        # symmetric inputs differ by float noise (~1e-16), which a distance-relative bound would let decide.
        tie = 1e-12 * mid                                  # equal distances: the shorter wins
        ref = 1 + next(k for k, d in enumerate(dist) if d <= min(dist) + tie)
        replaced = True

    n_of = {}
    for j, key in enumerate(keys):
        if key is not None and key not in n_of:
            where = f"surfaces[{j}].medium"
            glass = _lookup(conn, key, where)
            n_of[key] = [_index(glass, w, where) for w in waves]

    def n_after(w):
        return [1.0 if k is None else n_of[k][w] for k in keys[:-1]] + [image_n]

    def dispersion(key, w):
        return 0.0 if key is None else (n_of[key][w] - n_of[key][ref]) / n_of[key][ref]

    na_ref = n_after(ref)
    m_rows, c_rows, _, mu = _solve(s, na_ref)
    direct = []
    for w in range(nw):
        mw, _, c_img, mu_w = _solve(s, n_after(w))
        direct.append((-mw[-1][0] / mu_w, c_img))

    nrow = len(keys)
    colour = nw > 1
    elem_ids = _runs(keys, lambda a, b: a == b)
    group_ids = _runs(keys, lambda a, b: True)
    n_elem = max([e for e in elem_ids if e is not None], default=0)
    elem_rows = {e: [j for j in range(nrow) if elem_ids[j] == e] for e in range(1, n_elem + 1)}
    elem_sum = {e: {"axial": [0.0] * nw, "lateral": [0.0] * nw, "petzval": 0.0}
                for e in elem_rows}

    surfaces = []
    tot_ax, tot_lat, tot_pz = [0.0] * nw, [0.0] * nw, 0.0
    for j in range(nrow):
        y, u, nb = m_rows[j]
        yb, ub, _ = c_rows[j]
        na = na_ref[j]
        key_a, key_b = keys[j], (keys[j - 1] if j else None)
        a_inv, ab_inv = nb * u + nb * y * c[j], nb * ub + nb * yb * c[j]
        ax, lat = [], []
        for w in range(nw):
            d_a, d_b = dispersion(key_a, w), dispersion(key_b, w)
            ax.append(a_inv * y * (d_a - d_b))
            lat.append(ab_inv * y * (d_a - d_b))
            if elem_ids[j] is not None:                    # glass after the surface
                elem_sum[elem_ids[j]]["axial"][w] += a_inv * y * d_a
                elem_sum[elem_ids[j]]["lateral"][w] += ab_inv * y * d_a
            if j and elem_ids[j - 1] is not None:          # glass before the surface
                elem_sum[elem_ids[j - 1]]["axial"][w] -= a_inv * y * d_b
                elem_sum[elem_ids[j - 1]]["lateral"][w] -= ab_inv * y * d_b
            tot_ax[w] += ax[-1]
            tot_lat[w] += lat[-1]
        pz = (na - nb) * c[j] / (nb * na)
        tot_pz += pz
        if elem_ids[j] is not None:
            elem_sum[elem_ids[j]]["petzval"] += c[j] * (1.0 - 1.0 / na)
        if j and elem_ids[j - 1] is not None:
            elem_sum[elem_ids[j - 1]]["petzval"] -= c[j] * (1.0 - 1.0 / nb)
        surfaces.append({
            "surface": s["labels"][j], "element": elem_ids[j], "group": group_ids[j],
            "y": y, "ybar": yb, "A": a_inv, "Abar": ab_inv,
            "axial": ax if colour else None, "lateral": lat if colour else None,
            "petzval": pz,
        })

    elements, power_abs = [], 0.0
    for e, rows in elem_rows.items():
        key = keys[rows[0]]
        power = _element_power(s, n_of[key][ref], rows[0], rows[-1])
        power_abs += abs(power)
        elements.append({
            "element": e, "group": group_ids[rows[0]],
            "surfaces": [s["labels"][j] for j in range(rows[0], rows[-1] + 2)],
            "catalog": key[0], "name": key[1], "power": power,
            "axial": elem_sum[e]["axial"] if colour else None,
            "lateral": elem_sum[e]["lateral"] if colour else None,
            "petzval": elem_sum[e]["petzval"],
        })

    p_rows, _, up = _trace(c, s["t"], na_ref, 1.0, 0.0)    # the parallel ray (1, 0): f = -1/u'_k
    parallel = abs(up) > _REL_TOL * max(abs(r[1]) for r in p_rows)
    focal = -1.0 / up if parallel else None
    scale_z, scale_h = -1.0 / (image_n * mu * mu), 1.0 / (image_n * mu)
    totals = {
        "axial": tot_ax if colour else None,
        "lateral": tot_lat if colour else None,
        "petzval": tot_pz,
        "focal_length": focal,
        "marginal_slope_image": mu,
        "back_focal_distance": -m_rows[-1][0] / mu,
        "power_sum": abs(focal) * power_abs if parallel else None,
        "petzval_radius": None if tot_pz == 0 else -1.0 / (image_n * tot_pz),
        "focus_shift_predicted": [scale_z * v for v in tot_ax] if colour else None,
        "image_height_shift_predicted": [scale_h * v for v in tot_lat] if colour else None,
        "focus_direct": [d[0] for d in direct],
        "chief_image_height_direct": [d[1] for d in direct],
    }
    return {
        "wavelengths_um": list(waves), "reference_index": ref, "reference_replaced": replaced,
        "index_table": [
            {"surface": s["labels"][j], "catalog": k[0], "name": k[1], "n": list(n_of[k])}
            for j, k in enumerate(keys) if k is not None
        ],
        "surfaces": surfaces, "elements": elements, "totals": totals,
    }
