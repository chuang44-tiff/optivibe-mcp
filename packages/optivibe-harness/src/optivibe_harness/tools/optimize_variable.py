"""tools/optimize_variable.py — set_variable / clear_variable (the optimizer's vars).

Two dispatchable tools that flip a surface's radius/thickness cell between a
Variable and a Fixed solve — the geometry degrees of freedom the local optimizer
drives. Both read-back-verify the solve actually flipped (the
``_lens_common`` silent-no-op canary): ``MakeSolveVariable()`` /
``MakeSolveFixed()`` return a ``bool``, but the TRUTH source is
``cell.GetSolveData().Type`` after the call.

- ``set_variable``   — ``cell.MakeSolveVariable()``; read-back ``Type == Variable``.
- ``clear_variable`` — ``cell.MakeSolveFixed()``;   read-back ``Type != Variable``.

A silent no-op (the bool says success but the solve did not flip) raises
``SurfaceWriteError`` carrying ``(field, intended, actual, surface)`` — the same
geometry firewall every mutator funnels through. The surface index is bounds-checked via
``_lens_common`` BEFORE the typed call.

Live ZOS-API integration: exercised by the live closed-loop test; unit-tested
against the fixture-seeded fake LDE/cell.
"""
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _lens_common as _lc
from . import _optimize_common as _oc

# The cell tokens a caller may target and the ILDERow property each maps to.
_CELL_TO_PROP = {
    "radius": "RadiusCell",
    "thickness": "ThicknessCell",
    "conic": "ConicCell",
}


def _require_cell(params):
    """Pull + validate the ``cell`` token (``"radius"`` | ``"thickness"`` | ``"conic"``)."""
    cell = params.get("cell")
    if cell not in _CELL_TO_PROP:
        raise ToolParamError(
            f"cell must be one of {sorted(_CELL_TO_PROP)}, got {cell!r}"
        )
    return cell


def _solve_type_name(cell):
    """Read ``cell.GetSolveData().Type`` as a string (the read-back truth source)."""
    return str(cell.GetSolveData().Type)


def set_variable(session, params):
    """Make a surface's radius/thickness cell a Variable, with read-back proof.

    Bounds-checks ``surface`` (``1 <= surface <= N-1`` — the geometry range, OBJECT
    is refused) BEFORE any typed call, calls ``cell.MakeSolveVariable()``, then
    re-reads ``cell.GetSolveData().Type`` and verifies it is ``Variable``. A read-
    back that is still ``Fixed`` (the bool lied) -> ``SurfaceWriteError``.
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    surface = _lc._require_int_index(params, "surface")
    _lc._require_geometry_index(surface, n)
    cell_token = _require_cell(params)

    variable_member = _oc._solve_type_variable_enum(system)
    surf = lde.GetSurfaceAt(surface)
    cell = getattr(surf, _CELL_TO_PROP[cell_token])
    cell.MakeSolveVariable()

    # Read-back-as-proof: re-fetch the row + cell, read the solve type.
    surf = lde.GetSurfaceAt(surface)
    cell = getattr(surf, _CELL_TO_PROP[cell_token])
    actual = _solve_type_name(cell)
    if actual != str(variable_member):
        raise SurfaceWriteError(
            f"{cell_token} solve write did not take effect: intended=Variable "
            f"actual={actual!r} (silent no-op) surface={surface}",
            field=f"{cell_token} solve",
            intended="Variable",
            actual=actual,
            surface=surface,
        )
    result = {"ok": True, "surface": surface, "cell": cell_token, "solve_type": "Variable"}
    if cell_token == "conic":
        warning = _oc._check_conic_coeff_degeneracy(system, surf, surface, variable_member)
        if warning is not None:
            result["warning"] = warning
    return result


def _clear_asphere_variable(session, params):
    """Clear an asphere COEFFICIENT cell's Variable solve back to Fixed (§3).

    The partner of ``set_asphere_variable``. Params: ``surface`` (int) + ``term`` (the
    PHYSICAL order, validated via ``asphere_surface._require_term``). REFUSES a non-asphere
    surface (``asphere_type_of`` is None -> ``surface_asphere`` / ``SurfaceWriteError``;
    author it with ``set_asphere`` first). Resolves the materialized Par cell via the SAME
    ``asphere_surface._resolve_variable_cell`` ``set_asphere_variable`` uses (a term beyond
    the live Max-Term is refused there). ``cell.MakeSolveFixed()`` then read-back: a solve
    still Variable (a silent no-op) RAISES ``SurfaceWriteError``. ``_CELL_TO_PROP`` is NOT
    touched (the asphere arm short-circuits the radius/thickness/conic path). Returns
    ``{ok, surface, cell:"asphere", term, par, solve_type:"Fixed"}``.
    """
    from . import _asphere_cells as _asph
    from . import asphere_surface as _asph_tool

    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    surface = _lc._require_int_index(params, "surface")
    _lc._require_geometry_index(surface, n)
    term = _asph_tool._require_term(params.get("term"))

    row = lde.GetSurfaceAt(surface)
    info_key = _asph.asphere_type_of(row)
    if info_key is None:
        raise SurfaceWriteError(
            f"surface {surface} is not an asphere; cannot clear an asphere coefficient "
            "variable on it (author it with set_asphere first)",
            field="surface_type", intended="asphere", actual=None, surface=surface,
        )
    info = _asph.ASPHERE_TYPE_INFO[info_key]
    # Resolve the materialized Par cell via the SAME resolver set_asphere_variable uses
    # (a term beyond the live Max-Term / not in the type's order set is refused there).
    par, header = _asph_tool._resolve_variable_cell(system, row, info, term, surface)

    variable_member = _oc._solve_type_variable_enum(system)
    cell = _asph._cell_by_col(system, row, par)
    cell.MakeSolveFixed()

    # Read-back-as-proof: re-fetch the cell + read the solve type.
    cell = _asph._cell_by_col(system, lde.GetSurfaceAt(surface), par)
    actual = _solve_type_name(cell)
    if actual == str(variable_member):
        raise SurfaceWriteError(
            f"asphere coefficient {par!r} (the order-{term} term) solve clear did not "
            f"take effect on surface {surface}: solve reads back {actual!r} (still "
            "Variable, a silent no-op); refusing rather than claiming a cleared solve",
            field="asphere_variable", intended="Fixed", actual=actual, surface=surface,
        )
    return {
        "ok": True,
        "surface": surface,
        "cell": "asphere",
        "term": term,
        "par": par,
        "solve_type": "Fixed",
    }


def clear_variable(session, params):
    """Clear a surface's radius/thickness Variable solve (back to Fixed), with proof.

    Symmetric to ``set_variable``: ``cell.MakeSolveFixed()`` then read-back. A
    read-back that is still ``Variable`` (the clear silently no-opped) ->
    ``SurfaceWriteError``.

    ``cell="asphere"`` (with a ``term``) clears an asphere COEFFICIENT cell's
    Variable solve (the partner of ``set_asphere_variable``). Dispatched BEFORE
    ``_require_cell`` so the asphere arm short-circuits — ``_CELL_TO_PROP`` is NOT
    polluted with an "asphere" token.
    """
    system = session.system
    # The asphere-coefficient clear arm (short-circuit BEFORE _require_cell).
    if params.get("cell") == "asphere":
        return _clear_asphere_variable(session, params)
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    surface = _lc._require_int_index(params, "surface")
    _lc._require_geometry_index(surface, n)
    cell_token = _require_cell(params)

    variable_member = _oc._solve_type_variable_enum(system)
    surf = lde.GetSurfaceAt(surface)
    cell = getattr(surf, _CELL_TO_PROP[cell_token])
    cell.MakeSolveFixed()

    surf = lde.GetSurfaceAt(surface)
    cell = getattr(surf, _CELL_TO_PROP[cell_token])
    actual = _solve_type_name(cell)
    if actual == str(variable_member):
        raise SurfaceWriteError(
            f"{cell_token} solve clear did not take effect: intended!=Variable "
            f"actual={actual!r} (silent no-op) surface={surface}",
            field=f"{cell_token} solve",
            intended="Fixed",
            actual=actual,
            surface=surface,
        )
    return {"ok": True, "surface": surface, "cell": cell_token, "solve_type": "Fixed"}


SET_VARIABLE_SPEC = ToolSpec(
    name="set_variable",
    handler=set_variable,
    required_params=("surface", "cell"),
    param_types={"surface": "number", "cell": "string"},
    description=(
        "Make a surface's radius/thickness/conic cell a Variable solve (an "
        "optimizer degree of freedom) with read-back proof of the solve type. "
        "cell='conic' varies the conic constant K — warns if asphere polynomial "
        "coefficients are also variable (K and A4 are collinear in r^4)."
    ),
)

CLEAR_VARIABLE_SPEC = ToolSpec(
    name="clear_variable",
    handler=clear_variable,
    required_params=("surface", "cell"),
    param_types={"surface": "number", "cell": "string", "term": "number"},
    description=(
        "Clear a surface's radius/thickness/conic Variable solve (back to Fixed) "
        "with read-back proof. cell='asphere' (with term=the physical order, e.g. "
        "term=4) clears an asphere coefficient's Variable solve."
    ),
)

TOOL_SPECS = (SET_VARIABLE_SPEC, CLEAR_VARIABLE_SPEC)
