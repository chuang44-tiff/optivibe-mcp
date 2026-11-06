"""tools/_tolerance_hotspots.py -- PURE hotspot ranking for ``tolerance`` sensitivity (S2a).

NOT dispatchable (no ``TOOL_SPEC``), imports NO engine and no ``clr``: the ranking is a
function of (parsed sensitivity rows, an LDE topology read the caller already did, the
engine's own RSS).

Each sensitivity row is attributed to exactly one BUCKET (an element ``E<a>-<b>``, a
cemented group ``G<a>-<b>``, an air gap ``A<s>``, or the unattributed bucket ``U``), driven
by the catalog's ``cell_layout`` (no hand-typed token list beyond the three glass-body
tokens, see ``_GLASS_BODY``). A bucket's ``contribution`` is ``sqrt(sum c_r^2)`` with
``c_r = sqrt((min_change^2 + max_change^2) / 2)`` (the engine's own RSS term), so
``sqrt(sum contribution^2)`` reproduces the engine's ``Estimated change`` RSS.

A ranking is COMPLETE iff ``hotspots_withheld`` is None. Every condition that would make the
ranking incomplete (in a fixed order) withholds it; the raw ``sensitivity[]`` is the
caller's and is never touched here.

Seams the locked signatures left open (filled the simple way, listed in the build report):

- ``build_hotspots_detailed`` returns the reasons-after-the-first as warning strings;
  ``build_hotspots`` is the locked 3-tuple wrapper over it.
- ``topo=None`` means "the caller's ``read_topology`` threw" -> ``topology_unreadable``.
- ``recon=None`` fails closed (``reconcile_incomplete``).
- An unreadable (None / non-finite) change makes the row non-rankable; the identity is then
  computed over the finite-change rows only so it is always a float.
"""
import math

from . import _layout_geometry as _geom
from . import _tolerance_catalog as _cat

_RECON_KEYS = (
    "refused", "anomalous_rows", "unexpected_parsed_rows",
    "matched_operand_errors", "par_unresolved", "unattributed_errors",
)
_IDENTITY_TOL = 1e-3

# The ONLY token literal: the catalog gives TIND/TABB/TTHI the same ``family``
# ("scalar") and ``cell_layout`` ("single_surf") as the surface-form tokens, so the
# glass-body split cannot be read from it. ``_check_attribution_coverage`` asserts this set
# is a subset of the catalog's real single_surf tokens.
_GLASS_BODY = frozenset({"TIND", "TABB"})
_THICKNESS = "TTHI"

# cell_layout -> rule name. EXPLICIT for every layout the catalog carries, including the
# deliberately-unattributed ones, so a layout with no entry can never hide behind U.
_LAYOUT_RULE = {
    "single_surf": "single_surf",
    "surf_code": "surface_form",
    "surf_minmax_term": "surface_form",
    "surf_par": "surface_form",
    "surf_range": "range",
    "surf_range_roll": "range",
    "config_row": "unattributed",
    "iso": "unattributed",
    "surf_layr": "unattributed",
    "nps_object": "unattributed",
    "object_par": "unattributed",
    "control_only": "unattributed",
}
_UNATTRIBUTED_REASON = {
    "config_row": "multi-config row has no single element",
    "iso": "ISO 10110 row is not attributed to an element",
    "surf_layr": "coating row is not attributed to an element",
    "nps_object": "non-sequential object row",
    "object_par": "object parameter row",
    "control_only": "control row carries no perturbation",
}


def _check_attribution_coverage():
    """Import-time invariant: every authorable catalog token maps to an attribution rule.

    Raises ``AssertionError`` (at import) if an authorable token's ``cell_layout`` has no
    entry in ``_LAYOUT_RULE``, or if ``_GLASS_BODY`` / ``TTHI`` name a token that is not a
    ``single_surf`` catalog token.
    """
    single = {c for c, m in _cat.TOL_OPERAND_META.items() if m.cell_layout == "single_surf"}
    stray = (set(_GLASS_BODY) | {_THICKNESS}) - single
    assert not stray, f"glass-body literals are not single_surf catalog tokens: {sorted(stray)}"
    missing = sorted(
        c for c in _cat.supported_tokens()
        if _cat.TOL_OPERAND_META[c].cell_layout not in _LAYOUT_RULE
    )
    assert not missing, f"authorable tolerance tokens with no attribution rule: {missing}"


_check_attribution_coverage()


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #
def read_topology(system):
    """Per surface ``{material, type_name}`` (surface 0 forced to air). RAISES on a read failure.

    The caller catches and passes ``topo=None`` to ``build_hotspots`` (-> ``topology_unreadable``).
    """
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    topo = []
    for i in range(n):
        row = lde.GetSurfaceAt(i)
        material = "" if i == 0 else str(row.Material)
        topo.append({"material": material, "type_name": str(row.Type)})
    return topo


def _is_glass(topo, s):
    """The SAME predicate ``glass_groups`` uses: not mirror, not CB, not air."""
    r = topo[s]
    return (
        not _geom._is_mirror(r["material"])
        and not _geom._is_coordinate_break(r["type_name"])
        and not _geom._is_air_material(r["material"])
    )


def _as_index(value, n):
    """An in-range integer surface index, or None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < n else None


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #
def _element_key(a, b):
    return f"E{a}-{b}"


def _group_key(g):
    return f"G{g[0]}-{g[-1]}"


def _parent_of(pair, groups):
    """The key of the cemented group (3+ surfaces) containing the element ``pair``, else None."""
    for g in groups:
        if len(g) >= 3 and pair[0] in g and pair[1] in g:
            return _group_key(g)
    return None


def _glass_element_at(s, topo, elements):
    """The ``[s, s+1]`` element pair STARTING at glass surface ``s``, else None."""
    if not _is_glass(topo, s):
        return None
    pair = [s, s + 1]
    return pair if pair in [list(e) for e in elements] else None


def attribute_row(row, topo, groups, elements):
    """-> ``(key, kind, parent, reason)`` for one parsed sensitivity row. TOTAL: never raises.

    ``kind`` in {element, cemented_group, air_gap, unattributed}; ``reason`` is set on the
    unattributed bucket only. ``parent`` names the cemented group an element sits in.
    """
    try:
        return _attribute(row, topo, groups, elements)
    except Exception as exc:  # noqa: BLE001 -- total by contract
        return "U", "unattributed", None, f"attribution failed: {type(exc).__name__}"


def _u(reason):
    return "U", "unattributed", None, reason


def _elem(pair, groups):
    return _element_key(pair[0], pair[1]), "element", _parent_of(pair, groups), None


def _attribute(row, topo, groups, elements):
    token = row.get("type")
    meta = _cat.meta_for(token) if isinstance(token, str) else None
    if meta is None:
        return _u(f"{token}: not a catalogue operand")
    if meta.tier not in _cat._AUTHORABLE_TIERS or not meta.has_minmax:
        return _u(f"{token}: tier {meta.tier} is not a perturbation row")
    rule = _LAYOUT_RULE.get(meta.cell_layout)
    if rule is None or rule == "unattributed":
        return _u(_UNATTRIBUTED_REASON.get(meta.cell_layout, f"{token}: no attribution rule"))
    if not topo:
        return _u("topology unavailable")
    n = len(topo)
    pairs = [list(e) for e in elements]

    if rule == "range":
        s1, s2 = _as_index(row.get("surface"), n), _as_index(row.get("surface2"), n)
        if s1 is None or s2 is None:
            return _u(f"{token}: range surfaces unreadable or out of range")
        if s1 == s2:
            # The preset's SURFACE tilt/decenter is emitted as a TETX/TETY range with Surf1 == Surf2
            # (DG rows "Default surface dec/tilt tolerances N."): attribute it like any
            # surface-form row, to the surface's element or cemented group.
            rule = "single_surf"
            row = dict(row, surface=s1)
        else:
            if [s1, s2] in pairs:
                return _elem([s1, s2], groups)
            for g in groups:
                if len(g) >= 3 and [g[0], g[-1]] == [s1, s2]:
                    return _group_key(g), "cemented_group", None, None
            return _u(f"range {s1}-{s2} spans no single element or group")

    s = _as_index(row.get("surface"), n)
    if s is None:
        return _u(f"{token}: surface unreadable or out of range")

    if token in _GLASS_BODY:
        pair = _glass_element_at(s, topo, elements)
        if pair is None:
            return _u(f"no glass starts at surface {s}")
        return _elem(pair, groups)

    if token == _THICKNESS:
        if _is_glass(topo, s):
            pair = _glass_element_at(s, topo, elements)
            if pair is None:
                return _u(f"glass at surface {s} has no closing back surface")
            return _elem(pair, groups)
        return f"A{s}", "air_gap", None, None

    # surface-form tokens (single_surf non-glass-body, surf_code, surf_minmax_term, surf_par)
    if s >= 1 and _is_glass(topo, s) and _is_glass(topo, s - 1):
        for g in groups:
            if len(g) >= 3 and s in g[1:-1]:
                return _group_key(g), "cemented_group", None, None
        return _u(f"cemented interface at surface {s} is in no group")
    pair = _glass_element_at(s, topo, elements)
    if pair is not None:
        return _elem(pair, groups)
    if s >= 1:
        pair = _glass_element_at(s - 1, topo, elements)
        if pair is not None:
            return _elem(pair, groups)
    return _u(f"surface {s} belongs to no glass element")


# --------------------------------------------------------------------------- #
# Numbers
# --------------------------------------------------------------------------- #
def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _row_changes(row):
    """-> ``(min_change, max_change)`` or None when either is missing / non-finite."""
    lo = (row.get("min") or {}).get("change")
    hi = (row.get("max") or {}).get("change")
    if _finite(lo) and _finite(hi):
        return float(lo), float(hi)
    return None


def _c(lo, hi):
    return math.sqrt((lo * lo + hi * hi) / 2.0)


def _per_unit(change, delta):
    if not _finite(change) or not _finite(delta) or delta == 0:
        return None
    return float(change) / float(delta)


def _nkey(x):
    return (x is None, x if isinstance(x, (int, float)) and not isinstance(x, bool) else 0)


def _identity(cs, rss_change):
    recomputed = math.sqrt(math.fsum(c * c for c in cs))
    if not _finite(rss_change):
        return {"recomputed": recomputed, "rel": None, "ok": False}
    R = float(rss_change)
    if R == 0 and recomputed == 0:
        return {"recomputed": recomputed, "rel": 0.0, "ok": True}
    rel = abs(recomputed - R) / max(abs(R), recomputed)
    return {"recomputed": recomputed, "rel": rel, "ok": rel <= _IDENTITY_TOL}


def withhold_reasons(sensitivity_rows, topo, rss_change, *, dropped_row_count, recon, vendor):
    """The ordered ``[(code, detail)]`` of every withholding condition that holds.

    Pure; element [0] (if any) is the ``hotspots_withheld`` value. Order is fixed by the spec.
    """
    rows = list(sensitivity_rows or [])
    out = []
    if not _finite(rss_change):
        out.append(("rss_missing", "the report carries no finite 'Estimated change' RSS"))
    if not isinstance(dropped_row_count, int) or isinstance(dropped_row_count, bool) \
            or dropped_row_count > 0:
        out.append(("dropped_rows", f"dropped_row_count={dropped_row_count!r}: the sensitivity "
                    "table is incomplete"))
    if not isinstance(recon, dict):
        out.append(("reconcile_incomplete", "no reconcile result supplied"))
    else:
        bad = [k for k in _RECON_KEYS if recon.get(k)]
        if bad:
            out.append(("reconcile_incomplete", "reconcile channel(s) non-empty: " + ", ".join(bad)))
    if any(_row_changes(r) is None for r in rows):
        out.append(("nonfinite_change", "a row has a missing or non-finite change"))
    if topo is None or not isinstance(topo, list) or not topo:
        out.append(("topology_unreadable", "the LDE topology could not be read"))
    if not rows:
        out.append(("no_rows", "no sensitivity rows"))
    cs = [_c(*ch) for ch in (_row_changes(r) for r in rows) if ch is not None]
    if rows and math.fsum(c * c for c in cs) == 0:
        out.append(("zero_sensitivity", "every row changed the criterion by exactly zero"))
    if not _identity(cs, rss_change)["ok"] and _finite(rss_change):
        ident = _identity(cs, rss_change)
        out.append(("rss_identity_failed", f"recomputed RSS {ident['recomputed']:.8g} vs engine "
                    f"{float(rss_change):.8g} (rel {ident['rel']:.3g})"))
    if vendor == "Asphericon":
        out.append(("radius_unit_unverified", "the Asphericon radius tolerance unit is unverified"))
    return out


def build_hotspots_detailed(sensitivity_rows, topo, rss_change, units_by_type, *,
                            dropped_row_count, recon, vendor):
    """-> ``(hotspots, withheld, identity, notes)``; ``notes`` are warning strings (all reasons)."""
    rows = list(sensitivity_rows or [])
    reasons = withhold_reasons(rows, topo, rss_change, dropped_row_count=dropped_row_count,
                               recon=recon, vendor=vendor)
    changes = [_row_changes(r) for r in rows]
    cs = [_c(*ch) for ch in changes if ch is not None]
    identity = _identity(cs, rss_change)
    notes = [f"hotspots withheld ({code}): {detail}" for code, detail in reasons]
    if reasons:
        return [], reasons[0][0], identity, notes

    groups = _geom.glass_groups(topo, len(topo))
    elements = _geom.element_partition(groups)
    units_by_type = units_by_type or {}
    buckets = {}
    for r, (lo, hi) in zip(rows, changes):
        key, kind, parent, reason = attribute_row(r, topo, groups, elements)
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {"key": key, "kind": kind, "parent": parent, "rows": []}
        c = _c(lo, hi)
        delta_lo, delta_hi = (r.get("min") or {}).get("delta"), (r.get("max") or {}).get("delta")
        b["rows"].append({
            "type": r.get("type"),
            "surface": r.get("surface"),
            "surface2": r.get("surface2"),
            "param": r.get("param"),
            "units": units_by_type.get(r.get("type")),
            "min_change": lo,
            "max_change": hi,
            "max_abs_change": max(abs(lo), abs(hi)),
            "c": c,
            "per_unit": {"min": _per_unit(lo, delta_lo), "max": _per_unit(hi, delta_hi)},
            "reason": reason,
        })
    total = math.fsum(row["c"] ** 2 for b in buckets.values() for row in b["rows"])
    out = []
    for b in buckets.values():
        b["rows"].sort(key=lambda x: (-x["c"], x["type"] or "", _nkey(x["surface"]),
                                      _nkey(x["surface2"]), _nkey(x["param"])))
        sq = math.fsum(x["c"] ** 2 for x in b["rows"])
        key, kind = b["key"], b["kind"]
        if kind == "element":
            a, z = (int(p) for p in key[1:].split("-"))
            surfaces, material = [a, z], [topo[a]["material"]]
        elif kind == "cemented_group":
            g = next(g for g in groups if _group_key(g) == key)
            surfaces, material = [g[0], g[-1]], [topo[s]["material"] for s in g[:-1]]
        elif kind == "air_gap":
            s = int(key[1:])
            surfaces, material = [s, s], None
        else:
            surfaces, material = None, None
        out.append({
            "key": key, "kind": kind, "surfaces": surfaces, "material": material,
            "parent": b["parent"], "rank": 0, "share": sq / total,
            "contribution": math.sqrt(sq), "n_rows": len(b["rows"]), "rows": b["rows"],
        })
    out.sort(key=lambda h: (-h["contribution"], h["key"]))
    for i, h in enumerate(out, start=1):
        h["rank"] = i
    return out, None, identity, notes


def build_hotspots(sensitivity_rows, topo, rss_change, units_by_type, *,
                   dropped_row_count, recon, vendor):
    """The public signature: -> ``(hotspots, withheld, identity)``."""
    h, w, ident, _notes = build_hotspots_detailed(
        sensitivity_rows, topo, rss_change, units_by_type,
        dropped_row_count=dropped_row_count, recon=recon, vendor=vendor)
    return h, w, ident
