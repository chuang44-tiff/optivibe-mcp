"""tools/glass_explain.py -- ``explain_glass_swaps``.

ONE read-only tool: for a term the caller names (axial colour, lateral colour or Petzval)
it shows which glass ELEMENTS of the loaded lens carry the term. The paraxial maths is
``optivibe_reference.glass_math`` (engine-free); ``_glass_explain_read`` turns the live
lens into that library's input dict and checks the dict against the engine; this module
parses the arguments, forms the numbers of section 4, adds the Petzval prediction and
assembles the result. It WRITES NOTHING to the lens: every call is a property read or a
``GetOperandValue`` read (no property set, no solve change, no save, no configuration
switch).

The shares are first-order and often cancel to a small total, so they LOCATE carriers
and never rank glasses or forecast a swap. ``total`` is the paraxial DIRECT-TRACE value,
the shares' sum is served beside it, and the real-ray reading (``measured``,
``offsets_um``) is what to quote for the lens.

Refusals are ``error_envelope`` values and name the surface that could not be read; the
handler never raises (a module-local backstop returns ``explain_failed``).

Covered by a live ZOS-API integration test and by unit tests against capture-seeded fakes.
"""
import json

from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _glass_explain_read as _r
from . import lens_glass as _lg
from ._glass_explain_read import _Refuse

_TOOL = "explain_glass_swaps"
_TERMS = ("axial_color", "lateral_color", "petzval")
_PAIR_TOL_UM = 1e-3
_FLAG_ORDER = (
    "parts_cancel", "even_asphere", "material_solve", "reference_replaced",
    "multi_configuration", "index_adjust_on", "pair_ignored", "swap_ignored_for_colour",
    "primary_pair", "engine_paraxial_differs", "prediction_in_cemented_group",
    "prediction_parts_cancel",
)
_EQUATIONS = {
    "axial_color": {"value": "-1000 (axial_i[a] - axial_i[b]) / u'^2 (4.2a, 4.3)",
                    "total": "1000 (focus_direct[a] - focus_direct[b]) (2.1)"},
    "lateral_color": {"value": "+1000 (lateral_i[a] - lateral_i[b]) / u' (4.2a, 4.4)",
                      "total": "1000 (chief_image_height_direct[a] - [b]) (2.1)"},
    "petzval": {"value": "element Petzval sum (6.1a)", "total": "-1 / radius (6.1, 6.2)"},
}
_MEASURED_NOTE = {
    "lateral_color": "real chief ray at the image surface, +Hy edge field",
    "axial_color": "real-ray intercept at pupil zone 0.707; not best focus and not the "
                   "trial tool's per-wavelength best-focus shift",
    "petzval": "the engine's paraxial PETC; use render_field_curvature_distortion for "
               "real-ray field curvature",
}
_PREDICTION_LABEL = (
    "explanatory, first order (8.2): this element's power held, every other term "
    "unchanged; not a forecast of the re-optimised lens; compare with the trial's "
    "measured value"
)


def explain_glass_swaps(session, params):
    """Serve the per-element table for ``term``; NEVER raises (see the module docstring)."""
    try:
        return _explain(session, params if isinstance(params, dict) else {})
    except _Refuse as r:
        extra = {"reason": r.reason} if r.reason else {}
        return _ac.error_envelope(_TOOL, r.family, r.message, **extra)
    except Exception as exc:  # noqa: BLE001 -- never-raise backstop (aperture_ramp precedent)
        return _ac.error_envelope(_TOOL, "explain_failed", f"{_TOOL} fault ({exc!r})")


def _validate(params):
    term = params.get("term")
    if term not in _TERMS:
        raise _Refuse("tool_param",
                      "term must be axial_color, lateral_color or petzval; there is no default")
    pair = params.get("pair")
    if pair is not None and not isinstance(pair, str):
        raise _Refuse("tool_param", "pair must be a string: 'primary' or two wavelengths in um")
    swap = params.get("swap")
    if swap is None:
        return term, pair, None
    bad = _Refuse("tool_param", "swap must be 1 to 3 {surface, glass, catalog?} entries")
    if not isinstance(swap, list) or not 1 <= len(swap) <= 3:
        raise bad
    out = []
    for e in swap:
        s = e.get("surface") if isinstance(e, dict) else None
        if isinstance(s, float) and s.is_integer():
            s = int(s)
        g, c = (e.get("glass"), e.get("catalog")) if isinstance(e, dict) else (None, None)
        if (isinstance(s, bool) or not isinstance(s, int) or not isinstance(g, str)
                or not g.strip() or (c is not None and not isinstance(c, str))):
            raise bad
        out.append({"surface": s, "glass": g.strip(), "catalog": c})
    return term, pair, out


def _resolve_pair(pair, ctx):
    waves = ctx["waves"]
    fail = _Refuse("pair_unresolvable", f"pair {pair!r} does not name two wavelengths of this "
                   f"lens (lens has: {', '.join(repr(float(w)) for w in waves)})")
    if len(waves) < 2:
        raise fail
    if pair.strip().lower() == "primary":
        return 0, len(waves) - 1
    parts = [p.strip() for p in pair.split(",")]
    idx = []
    for p in parts:
        try:
            v = float(p)
        except ValueError:
            raise fail
        near = [i for i, w in enumerate(waves) if abs(w - v) <= _PAIR_TOL_UM]
        if len(parts) != 2 or len(near) != 1:
            raise fail
        idx.append(near[0])
    if idx[0] == idx[1]:
        raise fail
    return idx[0], idx[1]


def _pair_required(ctx, offs):
    items, first = [], True
    for i, um in enumerate(ctx["waves"]):
        if i == ctx["ref"]:
            tail = "primary"
        elif offs[i] is None:
            tail = "offset unreadable"
        else:
            tail = f"{offs[i]:+.3f}" + (" um from the primary" if first else "")
            first = False
        items.append(f"{um} ({tail})")
    return _Refuse("pair_required", "a colour term needs pair: 'primary' or two wavelengths in "
                   f"um; none is assumed. This lens: {', '.join(items)}")


# ---------------------------------------------------------------------------------- #
# the Petzval prediction (8.2)
# ---------------------------------------------------------------------------------- #
def _key(name):
    k = str(name or "").strip().upper()
    return k[:-4] if k.endswith(".AGF") else k


def _holders(conn, key, name):
    """Stored catalogs with this normalised key that hold ``name`` -> [(catalog, glass)]."""
    return [(c, g) for c, g in conn.execute(
        "SELECT catalog, name FROM glass WHERE name = ? COLLATE NOCASE", (name,))
        if _key(c) == key]


def _bind(system, conn, entry):
    """(catalog, glass, reason, owners) the swap would bind to; reason None when it resolves."""
    name = entry["glass"]
    try:
        in_use = [_key(c) for c in _lg._catalogs_in_use(system.SystemData.MaterialCatalogs)]
    except Exception:  # noqa: BLE001 -- unreadable in-use list: only the database decides
        in_use = []
    if entry["catalog"]:
        key = _key(entry["catalog"])
        for k in in_use + ([key] if key not in in_use else []):
            if k == key:
                break
            if _holders(conn, k, name):
                return None, None, "catalog_would_not_bind", None
        hit = _holders(conn, key, name)
        return (hit[0][0], hit[0][1], None, None) if hit else (None, None, "unknown_glass", None)
    for k in in_use:
        hit = _holders(conn, k, name)
        if hit:
            return hit[0][0], hit[0][1], None, None
    owners = list(dict.fromkeys(c for c, _ in conn.execute(
        "SELECT catalog, name FROM glass WHERE name = ? COLLATE NOCASE", (name,))))
    if len(owners) == 1:
        return owners[0], _holders(conn, _key(owners[0]), name)[0][1], None, None
    return (None, None, "ambiguous_glass", owners) if owners else (None, None, "unknown_glass", None)


def _index_at(conn, catalog, name, um):
    from optivibe_reference.glass_build import in_band
    from optivibe_reference.glass_dispersion import index_at
    row = conn.execute("SELECT formula, cd, min_wave, max_wave FROM glass WHERE catalog = ? "
                       "AND name = ?", (catalog, name)).fetchone()
    if row is None or row[1] is None or not in_band(row[2], row[3], um):
        return None
    return index_at(row[0], json.loads(row[1]), um)


def _predict(system, conn, ctx, one, swap, flags):
    elems = one["elements"]
    um = ctx["waves"][ctx["ref"]]
    owner = {s: k for k, e in enumerate(elems) for s in e["surfaces"][:-1]}
    hits = [owner.get(e["surface"]) for e in swap]
    swaps, total = [], 0.0
    for e, k in zip(swap, hits):
        row = {"surface": e["surface"], "glass": e["glass"], "catalog": e["catalog"],
               "n_old": None, "n_new": None, "change": None, "reason": None}
        swaps.append(row)
        el = None if k is None else elems[k]
        if el is None:
            row["reason"] = "no_glass_on_surface"
        elif hits.count(k) > 1:
            row["reason"] = "duplicate_element"
        elif len(el["surfaces"]) != 2:
            row["reason"] = "element_spans_rows"
        else:
            try:
                cat, glass, reason, owners = _bind(system, conn, e)
                n_new = None if reason else _index_at(conn, cat, glass, um)
            except (UnicodeError, ValueError):   # a name the database cannot even be asked for
                cat, glass, reason, owners, n_new = None, None, "unknown_glass", None, None
            if reason is None and n_new is None:
                reason = "unknown_glass"
            if reason:
                row["reason"] = reason
                if owners:
                    row["owners"] = owners
                continue
            n_old = next(t["n"][0] for t in one["index_table"] if t["surface"] == el["surfaces"][0])
            row.update(catalog=cat, glass=glass, n_old=n_old, n_new=n_new,
                       change=el["power"] * (1.0 / n_new - 1.0 / n_old))
            total += row["change"]
            if sum(1 for x in elems if x["group"] == el["group"]) > 1:
                flags["prediction_in_cemented_group"] = (
                    "the shared surface couples this element to its neighbour; holding one "
                    "element's power is not realisable on its own (trial probe: a group rescale "
                    "can need a negative factor)")
    ok = all(r["change"] is not None for r in swaps)
    if ok and len(swaps) > 1 and max(abs(r["change"]) for r in swaps) > abs(total):
        flags["prediction_parts_cancel"] = ("at least one swap's change exceeds the set's total: the "
                                 "changes partly cancel across the set")
    s_p = one["totals"]["petzval"]
    return {"petzval_sum": s_p + total if ok else None, "change": total if ok else None,
            "swaps": swaps, "label": _PREDICTION_LABEL}


# ---------------------------------------------------------------------------------- #
# the call
# ---------------------------------------------------------------------------------- #
def _explain(session, params):
    term, pair, swap = _validate(params)
    try:
        from optivibe_reference import glass_math as gm
        from optivibe_reference.glass_build import open_glass_catalog
        conn = open_glass_catalog()
    except Exception:  # noqa: BLE001 -- any open fault is the same refusal
        raise _Refuse("reference_unavailable", "the glass database could not be opened")
    try:
        return _serve(session.system, conn, gm, term, pair, swap)
    finally:
        conn.close()


def _notes(ctx, colour, pair, swap):
    flags = {}
    if ctx["aspheres"]:
        flags["even_asphere"] = (f"surfaces {ctx['aspheres']} are Even Asphere: curvature read "
                                 "as 1/R + 2 x (2nd order term); higher orders and the conic "
                                 "are paraxially inert")
    if ctx["solves"]:
        flags["material_solve"] = ("material under a solve, read as the catalog glass shown: "
                                   + ", ".join(f"surface {s} ({t})" for s, t in ctx["solves"]))
    cfg = ctx["config"]
    if cfg["count"] > 1:
        flags["multi_configuration"] = (f"{cfg['count']} configurations; this table describes "
                                        f"configuration {cfg['current']}")
    if ctx["env"]["adjust"]:
        flags["index_adjust_on"] = ("index adjustment to the environment is on (temperature "
                                    f"{ctx['env']['temperature']}, pressure "
                                    f"{ctx['env']['pressure']}); the index check decides")
    if not colour and pair is not None:
        flags["pair_ignored"] = "pair applies to the colour terms only"
    if colour and swap is not None:
        flags["swap_ignored_for_colour"] = "swap applies to the Petzval term only; no prediction is served"
    return flags


def _numbers(term, src, a, b, mu):
    """(element values, parts_sum, total) of section 4 for the term."""
    t = src["totals"]
    if term == "axial_color":
        vals = [-1000.0 * (e["axial"][a] - e["axial"][b]) / mu ** 2 for e in src["elements"]]
        return (vals, -1000.0 * (t["axial"][a] - t["axial"][b]) / mu ** 2,
                1000.0 * (t["focus_direct"][a] - t["focus_direct"][b]))
    if term == "lateral_color":
        vals = [1000.0 * (e["lateral"][a] - e["lateral"][b]) / mu for e in src["elements"]]
        return (vals, 1000.0 * (t["lateral"][a] - t["lateral"][b]) / mu,
                1000.0 * (t["chief_image_height_direct"][a] - t["chief_image_height_direct"][b]))
    return [e["petzval"] for e in src["elements"]], t["petzval"], t["petzval"]


def _serve(system, conn, gm, term, pair, swap):
    colour = term != "petzval"
    ctx = _r._extract(system, conn)
    full, one = _r._library(gm, conn, ctx)
    src = full if colour else one
    flags = _notes(ctx, colour, pair, swap)
    a = b = offs = raw = rwce = None
    if colour and pair is not None:
        a, b = _resolve_pair(pair, ctx)
        if pair.strip().lower() == "primary":
            flags["primary_pair"] = "primary colour usually yields to ordinary optimisation"
    checks = _r._check_all(system, ctx, full, one, term)
    if colour:
        offs, raw = _r._real_ray_offsets(system, ctx, term)
        if pair is None:
            raise _pair_required(ctx, offs)
        rwce = _r._rwce(system, ctx) if term == "axial_color" else None
    values, parts, total = _numbers(term, src, a, b, full["totals"]["marginal_slope_image"])
    el_out = [{"surfaces": [e["surfaces"][0], e["surfaces"][-1]], "group": e["group"],
               "glass": e["name"], "catalog": e["catalog"], "value": v, "power": e["power"]}
              for e, v in zip(src["elements"], values)]
    t, per_wave = src["totals"], None
    if colour:
        ref, prim = full["reference_index"], ctx["ref"]   # direct columns: against the PRIMARY
        per_wave = [{"wavelength_um": um, "axial_paraxial": t["axial"][i],
                     "lateral_paraxial": t["lateral"][i],
                     "focus_direct_um": 1000.0 * (t["focus_direct"][i] - t["focus_direct"][prim]),
                     "chief_height_direct_um": 1000.0 * (t["chief_image_height_direct"][i]
                                                         - t["chief_image_height_direct"][prim])}
                    for i, um in enumerate(full["wavelengths_um"])]
        measured = _r._measured(system, ctx, term, a, b, raw)
        engine = _r._engine_paraxial(system, ctx, term, a, b, total, flags)
        if full["reference_replaced"]:
            flags["reference_replaced"] = (f"the primary {ctx['waves'][ctx['ref']]} um is an end "
                                           "wavelength: columns are relative to "
                                           f"{full['wavelengths_um'][ref]} um")
    else:
        petc = checks.pop("petc")
        value = None if petc is None else -petc   # None: on-axis-only lens, see checks.petzval
        measured = {"value": value, "kind": "engine_paraxial_petc"}
        if petc is None:
            measured["reason"] = checks["petzval"]
        engine = {"read": "PETC", "value": value}
    if el_out and max(abs(x["value"]) for x in el_out) > abs(total):
        flags["parts_cancel"] = ("at least one element's share exceeds the paraxial total: the "
                                 "parts locate carriers, the total measures the term")
    out = {
        "ok": True, "tool": _TOOL, "term": term,
        "pair_um": [ctx["waves"][a], ctx["waves"][b]] if colour else None,
        "unit": "um" if colour else "1/mm", "elements": el_out, "total": total,
        "parts_sum": parts, "first_order_residual": parts - total, "measured": measured,
        "offsets_um": {str(um): offs[i] for i, um in enumerate(ctx["waves"])} if colour else None,
    }
    if not colour:
        out["petzval_radius"] = t["petzval_radius"]
        if swap is not None:
            out["prediction"] = _predict(system, conn, ctx, one, swap, flags)
    out["flags"] = [{"flag": f, "text": flags[f]} for f in _FLAG_ORDER if f in flags]
    out["detail"] = {
        "wavelengths_um": ctx["waves"], "primary_um": ctx["waves"][ctx["ref"]],
        "reference_um": full["wavelengths_um"][full["reference_index"]],
        "configuration": ctx["config"], "field": ctx["field"],
        "stop_surface": ctx["full"]["stop_surface"],
        "stop_marginal_height": ctx["full"]["stop_marginal_height"],
        "surfaces": [{k: s[k] for k in ("surface", "element", "group", "y", "ybar", "A", "Abar")}
                     for s in one["surfaces"]],
        "checks": checks, "per_wavelength": per_wave, "rwce_waves": rwce,
        "engine_paraxial": engine, "focal_length": t["focal_length"], "power_sum": t["power_sum"],
        "equations": dict(_EQUATIONS[term], measured=_MEASURED_NOTE[term]),
    }
    return out


EXPLAIN_GLASS_SWAPS_SPEC = ToolSpec(
    name=_TOOL,
    handler=explain_glass_swaps,
    required_params=("term",),
    param_types={"term": "string", "pair": "string", "swap": "array"},
    description=(
        "Read-only; writes nothing. For a term you name, shows which glass elements carry it "
        "on the loaded lens. term: axial_color, lateral_color or petzval. A colour term needs "
        "pair: 'primary' or two lens wavelengths in um ('0.656,0.546'); with no pair it "
        "refuses and lists the wavelengths with their real-ray offsets. Returns one row per "
        "element (surface range, glass, catalog, share with sign, power), the paraxial "
        "direct-trace total, the sum of the shares, and for colour the real-ray offsets per "
        "wavelength. Shares are first-order and often cancel to a small total: use them to "
        "locate carriers, never to rank glasses or forecast a swap. swap (petzval only; 1 to 3 "
        "entries on different elements) adds an explanatory first-order prediction, summed. Refuses lenses it cannot read, naming the "
        "surface."
    ),
)

TOOL_SPEC = EXPLAIN_GLASS_SWAPS_SPEC
