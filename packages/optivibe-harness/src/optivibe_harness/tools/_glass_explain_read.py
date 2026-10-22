"""tools/_glass_explain_read.py -- the lens readers and checks behind ``explain_glass_swaps``.

PRIVATE (no public name; not dispatchable). It turns the live lens into the
``optivibe.paraxial-system/2`` dict, checks that dict exactly against the engine, and adds
the real-ray reads. Every call here is a property read or a
``GetOperandValue`` read: nothing is written to the lens.

Covered by a live ZOS-API integration test and by unit tests against recorded fakes.
"""
import math

from . import _analysis_common as _ac
from . import _asphere_cells as _asph
from . import _collimation as _col
from . import _config_common as _cfg
from . import _layout_geometry as _geom
from . import _measurement_common as _mc
from . import _optimize_common as _oc
from . import _solve_trace as _st
from . import analysis_measure as _am

_ZONE = 0.7071                    # real-ray marginal zone (the trial probe read AXCL here)
_RING = 6                         # RWCE ring density (analysis_measure._DEFAULT_SAMP)
_TOL_INDEX, _TOL_HEIGHT, _TOL_PETZVAL = 1e-9, 1e-7, 1e-9
_TOL_DIFFERS_MM = 1e-6            # flag engine_paraxial_differs above this
_ROW_TYPES = ("Standard", "EvenAspheric")
_PETC_ON_AXIS = ("not_applicable: the engine reads PETC as 0 when every field is on axis; the "
                 "total is the library's, unchecked against the engine (EFFL agreement is "
                 "under detail.checks.effl); add an off-axis field to check it")
_RAY = {"Surf": 2, "Wave": 3, "Hx": 4, "Hy": 5, "Px": 6, "Py": 7}
_SLOTS = {   # operand -> {header: GetOperandValue position}  (headers read from the live engine)
    "PARY": _RAY, "PARB": _RAY, "REAY": _RAY, "INDX": {"Surf": 2, "Wave": 3},
    "EFFL": {"Wave": 3}, "PETC": {"Wave": 3}, "AXCL": {"Wave1": 2, "Wave2": 3, "Zone": 4},
    "LACL": {"Minw": 2, "Maxw": 3}, "RWCE": {"Ring": 2, "Wave": 3, "Hx": 4, "Hy": 5},
}


class _Refuse(Exception):
    """An expected refusal: an ``error_envelope`` with this family / reason / message."""

    def __init__(self, family, message, reason=None, extra=None):
        super().__init__(message)
        self.family, self.message, self.reason = family, message, reason
        self.extra = dict(extra or {})          # further envelope fields, e.g. trial_glass_swaps' rebind


def _read(system, code, **named):
    """One operand read through the NAMED-slot reader; a suspicious value raises."""
    cols = _SLOTS[code]
    raw, suspicious = _mc.read_operand_slots(
        system, code, {cols[k]: (k, v) for k, v in named.items()})
    if suspicious:
        raise _Refuse("explain_failed", f"{code} {named} read a suspicious value ({raw!r})")
    return float(raw)


def _try(fn):
    try:
        return fn(), None
    except _Refuse as r:
        return None, r.message
    except Exception as exc:  # noqa: BLE001 -- an optional read that throws is an unreadable value
        return None, f"read fault ({exc!r})"


def _catalog_map(conn):
    """{UPPER catalog: [stored catalogs]} -- the database's stored spellings."""
    out = {}
    for (c,) in conn.execute("SELECT DISTINCT catalog FROM glass"):
        out.setdefault(c.upper(), []).append(c)
    return out


def _safe_str(fn):
    try:
        return str(fn())
    except Exception:  # noqa: BLE001 -- a descriptive read; never fatal
        return None


def _config_state(system):
    try:
        count = int(system.MCE.NumberOfConfigurations)
    except Exception:  # noqa: BLE001 -- a non-MCE read is one configuration
        count = 1
    return {"current": _cfg.safe_current_configuration(system), "count": max(count, 1)}


def _environment(system):
    out = {"adjust": False, "temperature": None, "pressure": None}
    try:
        env = system.SystemData.Environment
        out["adjust"] = bool(env.AdjustIndexToEnvironment)
        out["temperature"], out["pressure"] = float(env.Temperature), float(env.Pressure)
    except Exception:  # noqa: BLE001 -- unreadable: the index check decides
        pass
    return out


def _unsupported(message, reason):
    return _Refuse("unsupported_lens", message, reason)


def _surface_rows(system, conn, waves_ref_um):
    """Rows 1..N-2 as library surface dicts, plus the even-asphere and material-solve notes."""
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    cmap = _catalog_map(conn)
    surfaces, aspheres, solves = [], [], []
    for s in range(1, n - 1):
        row = lde.GetSurfaceAt(s)
        tname = _oc._base_token(row.Type)
        if tname not in _ROW_TYPES:
            shown = _oc._base_token(getattr(row, "TypeName", "") or tname)
            raise _unsupported(f"surface {s} is {shown}; only Standard and EvenAspheric "
                               "surfaces are read", "surface_class")
        radius = float(row.Radius)
        if math.isnan(radius):
            raise _unsupported(f"surface {s} has an unreadable radius (NaN)", "system")
        curv = 0.0 if (not math.isfinite(radius) or radius == 0.0) else 1.0 / radius
        if tname == "EvenAspheric":
            try:
                curv += 2.0 * _asph.read_computed_double_cell(system, row, "Par1", "2nd Order Term")
            except Exception as exc:  # noqa: BLE001 -- a read fault names its surface
                raise _Refuse("explain_failed",
                              f"surface {s}: the 2nd order term could not be read ({exc!r})")
            aspheres.append(s)
        thick = float(row.Thickness)
        if not math.isfinite(thick) or thick < 0.0:
            raise _unsupported(f"surface {s} has thickness {thick!r}; it must be finite and >= 0",
                               "system")
        medium = None
        mat = _oc._base_token(row.Material or "").strip()
        if mat:
            if _geom._is_mirror(mat):
                raise _unsupported(f"surface {s} is a mirror", "mirror")
            solve = _oc._base_token(row.MaterialCell.GetSolveData().Type)
            if solve == "MaterialModel":
                raise _unsupported(f"surface {s} is a model glass; it is not in the glass "
                                   "database", "glass")
            if solve != "Fixed":
                solves.append((s, solve))
            hits = cmap.get(str(row.MaterialCatalog or "").strip().upper(), [])
            if len(hits) != 1:
                raise _unsupported(f"surface {s}: catalog {row.MaterialCatalog} of {mat} is not in "
                                   f"the built glass database, or is ambiguous there", "glass")
            if s == n - 2:
                raise _unsupported(f"the image space is not air (surface {s} carries glass)",
                                   "not_in_air")
            medium = {"catalog": hits[0], "name": mat}
        surfaces.append({"surface": s, "curvature": curv, "thickness": thick, "medium": medium})
    return surfaces, aspheres, solves


def _extract(system, conn):
    """The live lens -> both library dicts (full band, primary only) plus the row maps."""
    if _oc._base_token(system.Mode).strip().lower() != "sequential":
        raise _unsupported("the system is not in sequential mode", "system")
    units = _ac._lens_unit_and_um_factor(system)[0]
    if units != "Millimeters":
        raise _unsupported(f"lens units are {units}; only millimetres are read", "system")
    rows, n_bad, ok = _am._read_wavelength_rows(system)
    if not ok or n_bad or not rows:
        raise _unsupported("the wavelength table could not be read", "system")
    prim_row, fault = _st._primary_wave(system)
    if fault:
        raise _unsupported(f"no single primary wavelength ({fault})", "system")
    order = sorted(rows, key=lambda r: r[1])
    waves = [um for _, um in order]
    if any(b <= a for a, b in zip(waves, waves[1:])):
        raise _unsupported("two wavelengths are equal", "system")
    row_of = [r for r, _ in order]
    ref = row_of.index(prim_row)
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    stop = int(lde.StopSurface)
    if n < 3 or not 1 <= stop <= n - 2:
        raise _unsupported(f"the stop (surface {stop}) is not an interior surface of a "
                           f"{n}-row lens", "system")
    if _col._read_afocal_image_space(system) is True:
        raise _unsupported("afocal image space is on", "afocal")
    obj = lde.GetSurfaceAt(0)
    t0 = float(obj.Thickness)
    if str(obj.Material or "").strip():
        raise _unsupported("the object space is not air (surface 0)", "not_in_air")
    surfaces, aspheres, solves = _surface_rows(system, conn, waves[ref])
    image_n = _read(system, "INDX", Surf=n - 2, Wave=prim_row)
    if abs(image_n - 1.0) > _TOL_INDEX:
        raise _unsupported(f"the image space is not air (surface {n - 2} reads index "
                           f"{image_n!r})", "not_in_air")
    named = dict(Wave=prim_row, Hx=0.0, Hy=1.0, Px=0.0, Py=0.0)
    if math.isnan(t0):
        raise _unsupported("the object distance is unreadable (NaN)", "system")
    inf_obj = math.isinf(t0) or abs(t0) > 1e9
    if inf_obj:
        b = _read(system, "PARB", Surf=0, **named)
        if abs(b) >= 1.0:
            raise _unsupported(f"the object-space chief direction cosine {b!r} is not below 1",
                               "system")
        field, how = {"slope": b / math.sqrt(1.0 - b * b)}, "PARB(Surf=0) direction cosine"
    else:
        field, how = {"height": _read(system, "PARY", Surf=0, **named)}, "PARY(Surf=0) object height"
    h_stop = _read(system, "PARY", Surf=stop, Wave=prim_row, Hx=0.0, Hy=0.0, Px=0.0, Py=1.0)
    base = {
        "schema": "optivibe.paraxial-system/2", "object_distance": "inf" if inf_obj else t0,
        "field": field, "stop_surface": stop, "stop_marginal_height": abs(h_stop),
        "image_space_index": image_n, "surfaces": surfaces,
    }
    return {
        "full": dict(base, wavelengths_um=waves, reference_index=ref),
        "one": dict(base, wavelengths_um=[waves[ref]], reference_index=0),
        "waves": waves, "row_of": row_of, "ref": ref, "prim_row": prim_row,
        "h_sign": 1.0 if h_stop >= 0 else -1.0, "aspheres": aspheres, "solves": solves,
        "field": {"type": _safe_str(lambda: system.SystemData.Fields.GetFieldType()),
                  "value": field, "read": how},
        "config": _config_state(system), "env": _environment(system), "image_n": image_n,
    }


def _all_fields_on_axis(system):
    """True only when every field's X and Y read exactly 0; any read fault is False."""
    try:
        fields = system.SystemData.Fields
        pts = [fields.GetField(i) for i in range(1, int(fields.NumberOfFields) + 1)]
        return bool(pts) and all(float(p.X) == 0.0 and float(p.Y) == 0.0 for p in pts)
    except Exception:  # noqa: BLE001 -- unknown is not on axis: the mismatch refusal stands
        return False


def _library(gm, conn, ctx):
    """Both library calls; a ``GlassMathError`` becomes an ``unsupported_lens`` refusal."""
    labels = [r["surface"] for r in ctx["full"]["surfaces"]]
    try:
        return gm.contributions(conn, ctx["full"]), gm.contributions(conn, ctx["one"])
    except gm.GlassMathError as exc:
        where = exc.where or ""
        if exc.reason == "glass_not_evaluable":
            idx = int(where.split("[")[1].split("]")[0]) if where.startswith("surfaces[") else None
            at = f"surface {labels[idx]}: " if idx is not None else ""
            raise _unsupported(f"{at}{exc} (glass database)", "glass")
        if exc.reason == "degenerate_trace":
            if exc.where == "stop_surface":
                raise _unsupported("the stop is conjugate to the object; the chief ray cannot "
                                   "be aimed", "degenerate")
            raise _unsupported("the system is afocal at the primary wavelength (u'_k = 0)",
                               "afocal")
        raise _unsupported(str(exc), "system")


def _mismatch(reason, message):
    return _Refuse("lens_read_mismatch", message + "; the table would not describe this lens",
                   reason)


def _check_heights(system, ctx, one, key, hy, py, field):
    worst = (0.0, None)
    for r in one["surfaces"]:
        eng = _read(system, "PARY", Surf=r["surface"], Wave=ctx["prim_row"], Hx=0.0, Hy=hy,
                    Px=0.0, Py=py)
        lib = r[field] * (ctx["h_sign"] if field == "y" else 1.0)
        d = abs(eng - lib)
        if d > worst[0]:
            worst = (d, {"surface": r["surface"]})
        if d > _TOL_HEIGHT:
            raise _mismatch("ray_height", f"surface {r['surface']}, {key} ray height at "
                            f"{ctx['waves'][ctx['ref']]} um: engine {eng!r}, computed {lib!r}")
    return {"worst_abs": worst[0], "at": worst[1]}


def _check_all(system, ctx, full, one, term):
    """Checks 1 to 5 (section 3); each refuses on a miss and records its worst difference."""
    out, prim, rows = {}, ctx["prim_row"], ctx["row_of"]
    env = " (index adjustment to the environment is on)" if ctx["env"]["adjust"] else ""
    worst = (0.0, None)
    for e in full["index_table"]:
        for i, n_lib in enumerate(e["n"]):
            n_eng = _read(system, "INDX", Surf=e["surface"], Wave=rows[i])
            d = abs(n_eng - n_lib)
            if d > worst[0]:
                worst = (d, {"surface": e["surface"], "wavelength_um": ctx["waves"][i]})
            if d > _TOL_INDEX:
                raise _mismatch("index", f"surface {e['surface']}, index at {ctx['waves'][i]} um: "
                                f"engine {n_eng!r}, computed {n_lib!r}{env}")
    out["index"] = {"worst_abs": worst[0], "at": worst[1]}
    if term != "petzval":
        out["marginal_height"] = _check_heights(system, ctx, one, "marginal", 0.0, 1.0, "y")
        if term == "lateral_color":
            out["chief_height"] = _check_heights(system, ctx, one, "chief", 1.0, 0.0, "ybar")
    else:
        petc = _read(system, "PETC", Wave=prim)
        lib = one["totals"]["petzval"]
        out["petzval"] = {"abs": abs(-petc - lib)}
        out["petc"] = petc
        if petc == 0.0 and _all_fields_on_axis(system):
            # live gate: the engine reads PETC as exactly 0 when every field is on axis
            out["petzval"], out["petc"] = _PETC_ON_AXIS, None
        elif abs(-petc - lib) > _TOL_PETZVAL:
            raise _mismatch("petzval", f"Petzval sum: engine {-petc!r} per mm, computed {lib!r}")
    focal = one["totals"]["focal_length"]
    try:
        effl = _read(system, "EFFL", Wave=prim)
        out["effl"] = ((abs(focal / ctx["image_n"] - effl) / abs(effl))
                       if focal is not None and effl != 0.0 else "not_applicable")
    except _Refuse:
        out["effl"] = "not_applicable"
    return out


def _real_ray_offsets(system, ctx, term):
    """(offsets in um per sorted wavelength or None, raw chief heights or None)."""
    prim, rows = ctx["prim_row"], ctx["row_of"]
    if term == "lateral_color":
        img = _mc.image_surface_index(system)
        raw = [_try(lambda r=r: _read(system, "REAY", Surf=img, Wave=r, Hx=0.0, Hy=1.0,
                                      Px=0.0, Py=0.0))[0] for r in rows]
        base = raw[ctx["ref"]]
        return [None if v is None or base is None else 1000.0 * (v - base) for v in raw], raw
    offs = [0.0 if r == prim else _try(lambda r=r: 1000.0 * _read(
        system, "AXCL", Wave1=prim, Wave2=r, Zone=_ZONE))[0] for r in rows]
    return offs, None


def _measured(system, ctx, term, a, b, raw):
    rows = ctx["row_of"]
    if term == "lateral_color":
        va = None if raw[a] is None or raw[b] is None else 1000.0 * (raw[a] - raw[b])
        kind, why = "real_chief_ray_image_surface", "REAY at the image surface was unreadable"
    else:
        va, why = _try(lambda: 1000.0 * _read(system, "AXCL", Wave1=rows[b], Wave2=rows[a],
                                              Zone=_ZONE))
        kind = "real_ray_intercept_zone_0.707"
    out = {"value": va, "kind": kind}
    if va is None:
        out["reason"] = why or "unreadable"
    return out


def _engine_paraxial(system, ctx, term, a, b, total_um, flags):
    rows = ctx["row_of"]
    try:
        if term == "axial_color":
            read, eng = "AXCL zone 0", _read(system, "AXCL", Wave1=rows[b], Wave2=rows[a], Zone=0.0)
        else:
            read, eng = "LACL", _read(system, "LACL", Minw=rows[b], Maxw=rows[a])
    except _Refuse as r:
        return {"read": None, "unreadable": r.message}
    except Exception as exc:  # noqa: BLE001 -- a shown read that throws is unreadable, not fatal
        return {"read": None, "unreadable": f"read fault ({exc!r})"}
    diff = eng - total_um / 1000.0
    if abs(diff) > _TOL_DIFFERS_MM:
        flags["engine_paraxial_differs"] = (
            f"the engine's paraxial {read} differs from the total by {diff * 1000.0:+.4g} um "
            "(ray aiming Off makes LACL differ by design; otherwise the table may not describe "
            "the last surface or the image distance)")
    return {"read": read, "value_um": eng * 1000.0, "difference_um": diff * 1000.0}


def _rwce(system, ctx):
    return {str(um): _try(lambda r=r: _read(system, "RWCE", Ring=_RING, Wave=r, Hx=0.0,
                                            Hy=0.0))[0]
            for um, r in zip(ctx["waves"], ctx["row_of"])}
