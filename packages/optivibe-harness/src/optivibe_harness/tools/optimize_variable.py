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
from ..errors import SolveDrivenError, SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _lens_common as _lc
from . import _optimize_common as _oc
from . import _solve_cells as _sc

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


def _require_replace_solve(params):
    """Pull + validate ``replace_solve``. STRICT ``is True``; a non-bool is a param error.

    The ``promote_best`` ``force`` precedent. ``1``, ``"true"``, ``"yes"`` and ``[]`` must
    NOT force — a destructive override reached by truthiness is an override nobody chose.
    But a non-bool is not silently read as "no" either: that would let a caller who meant
    to override believe they had, and then destroy the solve on a later retry with a
    different spelling. It is refused, loudly, before any engine touch.

    WHICH LINE DECIDES, stated because a mutation measured it: the ``isinstance`` REFUSAL
    is what enforces strictness — replacing the ``is True`` below with ``bool(value)`` is
    INERT, because by then the domain is already exactly ``{True, False}``. The ``is True``
    is a redundant backstop that becomes load-bearing only if the refusal is ever relaxed.
    Recorded rather than claimed the other way round.
    """
    value = params.get("replace_solve", False)
    if not isinstance(value, bool):
        raise ToolParamError(
            f"replace_solve must be a boolean (true/false), got "
            f"{type(value).__name__} {value!r}; it is refused rather than read as "
            "false, because a caller who meant to override must not silently not have"
        )
    return value is True


def set_variable(session, params):
    """Make a surface's radius/thickness cell a Variable, with read-back proof.

    Bounds-checks ``surface`` (``1 <= surface <= N-1`` — the geometry range, OBJECT
    is refused) BEFORE any typed call, calls ``cell.MakeSolveVariable()``, then
    re-reads ``cell.GetSolveData().Type`` and verifies it is ``Variable``. A read-
    back that is still ``Fixed`` (the bool lied) -> ``SurfaceWriteError``.

    THE DRIVEN-CELL GUARD AND ``replace_solve``.

    A cell already carrying a DRIVING solve is REFUSED by default. This is a
    DELIBERATE BREAKING CHANGE, on the ``require_free_stop`` / grating-``reflective``
    precedent, and a probe measured why: ``vary([1..5], ["radius"])`` over a design with
    ``SurfacePickup`` on surfaces 2 and 4 returned

        ok:true, n_applied:5, n_refused:0, n_variables_now:5,
        inventory_matches_optimizer:true

    — a textbook-clean envelope for an operation that SILENTLY DELETED TWO DESIGN
    RELATIONSHIPS. ``list_variables`` then reported five LDE variables with nothing
    marking two of them as former pickups. Disclose-only was rejected precisely because
    THAT call already passed a clean envelope through a real bulk operation.

    ``replace_solve=true`` proceeds anyway and REPORTS the solve it replaced, so the
    legitimate re-vary workflow stays open. STRICT ``is True`` (the ``promote_best``
    ``force`` precedent): ``1``, ``"true"``, ``"yes"``, ``[]`` do NOT force. Anything
    other than a bool is a param error rather than a silent "no".

    THE GUARD RUNS AFTER the tool's own ``_require_geometry_index``, so its stricter
    domain (OBJECT refused) is preserved, and BEFORE ``MakeSolveVariable()``, so a
    refusal writes nothing.

    ROUTE NOTE, newly load-bearing: the guard reads through the COLUMN route while this
    writer acts through the PROPERTY route (``getattr(surf, _CELL_TO_PROP[token])``).
    The live route-agreement falsifier is what makes that sound, and it is NON-WAIVABLE — a
    disagreement re-anchors the catalog, it does not get patched here.
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    surface = _lc._require_int_index(params, "surface")
    _lc._require_geometry_index(surface, n)
    cell_token = _require_cell(params)
    replace_solve = _require_replace_solve(params)

    prior_solve = None
    probe = _sc.refuse_if_driven(system, lde, surface, cell_token)
    if probe.get("driven") is not False:
        # THE OVERRIDE PERMITS REPLACING A **KNOWN** DRIVING SOLVE, AND ONLY THAT.
        # ``driven is not False`` covers True AND None, and an earlier revision let
        # ``replace_solve`` past BOTH — so an UNKNOWN cell (the solve could not be read)
        # was WRITTEN, and because ``solve_type`` is ``None`` there the disclosure key was
        # suppressed and the envelope came back BYTE-IDENTICAL to a clean undriven cell's:
        #
        #     {'ok': True, 'surface': 1, 'cell': 'radius', 'solve_type': 'Variable'}
        #
        # That is the measured silent-wrong restored one branch over, and it inverted
        # the UNKNOWN-fails-CLOSED contract exactly at the point this guard enforces it:
        # refuse by default BECAUSE a clean envelope over a destroyed relationship is the
        # thing being fixed. An override may permit replacing a solve we CAN SEE and
        # report; it must not permit writing through one we cannot.
        #
        # It also restores the promise ``replaced_solves`` makes: past this gate
        # ``driven is True``, so ``solve_type`` is a real string and the key is emitted on
        # EVERY override — "present and empty" now means "nothing was replaced" as a
        # measured fact rather than as an artifact of a fail-open path.
        if not (replace_solve and probe.get("driven") is True):
            raise SolveDrivenError.from_probe(
                probe, cell_token=cell_token, surface=surface, tool="set_variable",
                intended="Variable")
        prior_solve = probe.get("solve_type")

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
    if prior_solve is not None:
        # DISCLOSE what was destroyed. The reading is the PRE-write probe's, taken before
        # ``MakeSolveVariable`` overwrote it — after the write it is unrecoverable, which
        # is exactly why the silent version of this operation was undetectable.
        result["replaced_solves"] = [
            {"surface": surface, "cell": cell_token, "prior_type": prior_solve}]
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
    param_types={"surface": "number", "cell": "string",
                 "replace_solve": "boolean"},
    description=(
        "Make a surface's radius/thickness/conic cell a Variable solve (an "
        "optimizer degree of freedom) with read-back proof of the solve type. "
        "cell='conic' varies the conic constant K — warns if asphere polynomial "
        "coefficients are also variable (K and A4 are collinear in r^4). "
        "REFUSES a cell already driven by a solve (error_family solve_driven): "
        "making it a variable would DELETE that relationship. Read `solves` on "
        "read_surface first; pass replace_solve=true to replace it deliberately "
        "(the prior solve type is reported back in replaced_solves)."
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
