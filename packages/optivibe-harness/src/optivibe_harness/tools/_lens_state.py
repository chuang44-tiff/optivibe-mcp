"""tools/_lens_state.py -- private leaf for ``trial_glass_swaps`` (loop A1): the WIDENED lens-state
reader and the ZERO-tolerance compare. NOT dispatchable (no ``TOOL_SPEC``).

``read_state`` flattens everything a restore could leave different into one ``{key: value}`` dict:
per surface the radius, thickness, conic, semi-diameter, material, catalog and type; each of the
five solve cells' type and (for a DRIVING solve) its data fields; every even-asphere Par1..Par8
cell; per merit row the type, target, weight and EVERY parameter cell; the wavelength table,
catalogs in use, aperture and fields; and the optimizer-DOF fingerprint of ``aperture_ramp``.

``diff`` compares two states with NO tolerance. A number differs unless it is equal, except that
two NaN are the same reading; ``abs(nan - x) > 0`` is False, so a numeric-difference compare would
let a NaN on one side pass as "no difference" (the probe's ``diff_state`` had that hole). The
fingerprint is compared exactly too, not through the ramp's toleranced ``_fingerprints_match``.

Any read that cannot be made raises ``StateUnreadable``: an unreadable state proves nothing, so a
caller treats it as "not restored", never as "equal".
"""
import math

from . import _asphere_cells as _asph
from . import _merit_cells as _mcells
from . import _optimize_common as _oc
from . import _solve_cells as _sc
from . import aperture_ramp as _ramp
from . import lens_glass as _lg


class StateUnreadable(Exception):
    """A piece of the lens state could not be read; the state proves nothing."""


def _tok(value):
    """An engine string through the BASE slot (the repo-wide normalization rule)."""
    return _oc._base_token("" if value is None else value)


def _same(a, b):
    """Zero-tolerance equality: equal, or both NaN; containers compared member by member."""
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return True
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b or (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
        and not isinstance(a, bool) and not isinstance(b, bool) and a == b)


def diff(a, b):
    """``[(key, a_value, b_value)]`` for every key the two states do not hold identically.

    An item that is a mapping on both sides (the variable fingerprint) is reported member by
    member (``"fingerprint ('lde', 1, 'radius')"``), so a difference names the one cell.
    """
    out = []
    for k in sorted(set(a) | set(b)):
        if k in a and k in b and _same(a[k], b[k]):
            continue
        x, y = a.get(k, "<absent>"), b.get(k, "<absent>")
        if isinstance(x, dict) and isinstance(y, dict):
            out.extend(("%s %r" % (k, m), x.get(m, "<absent>"), y.get(m, "<absent>"))
                       for m in sorted(set(x) | set(y), key=repr)
                       if m not in x or m not in y or not _same(x[m], y[m]))
        else:
            out.append((k, x, y))
    return out


def _surface_core(system, lde, i, row, items, with_fields):
    p = "s%d." % i
    items[p + "Radius"], items[p + "Thickness"] = float(row.Radius), float(row.Thickness)
    items[p + "Conic"], items[p + "SemiDiameter"] = float(row.Conic), float(row.SemiDiameter)
    items[p + "Material"] = _tok(row.Material)
    items[p + "Catalog"] = _tok(row.MaterialCatalog)
    items[p + "Type"] = _tok(row.Type)
    for token in _sc.CELL_TOKENS:
        cell = _sc.solve_cell(system, lde, i, token)
        kind = _sc.read_solve_type(cell)
        if kind is None:
            raise StateUnreadable("surface %d: the %s solve type is unreadable" % (i, token))
        items[p + token + ".solve"] = kind
        if with_fields and _sc.is_driving(kind):
            view = _sc.unwrap_or_none(cell.GetSolveData())
            if view is None:
                raise StateUnreadable("surface %d: the %s %s data is unreadable" % (i, token, kind))
            for field, (value, state) in sorted(_sc.read_solve_fields(view, token, kind).items()):
                items[p + token + "." + field] = (value, state)


def _merit_rows(mfe, items):
    n = int(mfe.NumberOfOperands)
    items["mfe.n"] = n
    for r in range(1, n + 1):
        op = mfe.GetOperandAt(r)
        q = "m%d." % r
        items[q + "type"] = _tok(op.TypeName)
        items[q + "target"] = float(op.Target)
        items[q + "weight"] = float(op.Weight)
        cells = _mcells.read_param_map(op)
        items[q + "params"] = tuple(sorted(
            (h, d["col"], d["kind"], d["value"]) for h, d in cells.items()))


def _system_items(system, items):
    data = system.SystemData
    waves = data.Wavelengths
    items["waves"] = tuple(
        (float(waves.GetWavelength(i).Wavelength), float(waves.GetWavelength(i).Weight),
         bool(waves.GetWavelength(i).IsPrimary))
        for i in range(1, int(waves.NumberOfWavelengths) + 1))
    items["catalogs"] = tuple(_tok(c) for c in _lg._catalogs_in_use(data.MaterialCatalogs))
    items["aperture"] = (_tok(data.Aperture.ApertureType), float(data.Aperture.ApertureValue))
    fields = data.Fields
    items["fields"] = (_tok(fields.GetFieldType()), tuple(
        (float(fields.GetField(i).X), float(fields.GetField(i).Y), float(fields.GetField(i).Weight))
        for i in range(1, int(fields.NumberOfFields) + 1)))


def read_state(system):
    """The full widened state, ``{key: value}``. Raises on ANY unreadable piece."""
    try:
        items = {}
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
        items["n_surfaces"] = n
        for i in range(n):
            row = lde.GetSurfaceAt(i)
            _surface_core(system, lde, i, row, items, True)
            if _asph.is_even_asphere(row):
                for name in _asph._PARAM_NAMES:
                    items["s%d.asphere.%s" % (i, name)] = _asph.read_asphere_cell(system, row, name)
        _merit_rows(system.MFE, items)
        _system_items(system, items)
        fingerprint = _ramp._variable_fingerprint(system)
        if fingerprint is None:
            raise StateUnreadable("the optimizer-variable fingerprint is unreadable")
        items["fingerprint"] = dict(fingerprint)
        return items
    except StateUnreadable:
        raise
    except Exception as exc:  # noqa: BLE001 -- any read fault: the state proves nothing
        raise StateUnreadable("the lens state could not be read (%r)" % (exc,)) from exc


def light_state(system):
    """The cheap per-arm check (about 0.1 s): geometry, materials, solve types and the merit."""
    try:
        items = {}
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
        items["n_surfaces"] = n
        for i in range(n):
            _surface_core(system, lde, i, lde.GetSurfaceAt(i), items, False)
        merit = float(system.MFE.CalculateMeritFunction())
        if not math.isfinite(merit):
            raise StateUnreadable("the merit value is not finite")
        items["merit"] = merit
        return items
    except StateUnreadable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StateUnreadable("the lens state could not be read (%r)" % (exc,)) from exc
