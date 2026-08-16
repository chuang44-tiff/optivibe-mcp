"""tools/mce_config.py — the multi-configuration (MCE) authoring PRIMITIVE.

SIX dispatchable Multi-Configuration-Editor tools (NOT a LensSpec extension — the flat
``SurfaceSpec`` schema cannot carry the per-config cell matrix; the LensSpec guard arm in
``lens_spec`` REFUSES a multi-config round-trip rather than silently FLATTENING N configs
to config 1, the CB/asphere/grating silent-flatten precedent):

- ``add_configuration`` — ``MCE.AddConfiguration(seed_from_current)``; read-back-proven via
  the ``NumberOfConfigurations`` increment (NOT the lying-return bool, F4/D14).
- ``set_config_operand`` — author a NEW MCE row: ``AddOperand`` -> ``ChangeType(member)`` ->
  read-back the type -> set the Param selectors (surface/param/param3 as INTEGER properties,
  read-back-proven). The catalog (``_mce_catalog``) drives WHICH selectors are valid + the
  author boundary (nsc/unsupported tiers REFUSED fail-closed, naming ``meta.reason``). ZERO
  net mutation on any refusal (the orphan row is removed — the ``add_operand`` transactional
  precedent). Returns the 1-based ``row`` handle. Does NOT write a value (AXIS-1 separability).
- ``set_config_value`` — the CENTERPIECE: write ONE per-config cell (DataType-keyed,
  read-back-proven via ``_mce_cells.write_config_cell``) THEN INVARIANT-1 (§4) — an
  INDEPENDENT fresh-handle re-read asserts the authored (operand,config) cell reads back.
- ``set_config_variable`` — make a per-config Double cell an optimizer Variable, proved via
  the optimizer's own ``opt.Variables`` increment (the CB Q5 lesson). REFUSES an Integer/
  String cell pre-mutation (the L30 / CB-Order phantom-DOF class — the live ``cell.DataType``
  is the only guard; the read-back is not a safety net).
- ``set_current_configuration`` — ``MCE.SetCurrentConfiguration(config)`` (re-evaluates the
  optics — THE BITE), read-back-proven ``CurrentConfiguration == config``.
- ``describe_configurations`` — the read tool: every operand x config value vector + which
  configs carry a Variable. Fail-OPEN per-row (a row/cell read throw degrades + is DISCLOSED,
  never a stale value) — the READ tool ONLY; every WRITE path stays fail-closed.

Every handler returns the uniform never-raise envelope and NEVER raises past its boundary
(the L26 firewall): an EXPECTED failure (bad param / out-of-range / a read-back mismatch) is
a structured ``{ok:false}`` dict; an unexpected engine throw is caught broad and resolved to
the tool's family. NO new error class (D10) — reuse ``SurfaceWriteError`` + ``ToolParamError``;
the families are STRING constants (§6) attached via ``error_envelope`` (the ``cb_surface``
precedent).

Live ZOS-API integration; unit-tested against fixture-seeded fakes
(``FakeMCE``/``FakeMCEOperand``/``FakeMCECell``/``FakeOptimizer``).
"""
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _mce_catalog as _cat
from . import _mce_cells as _mc
from ._analysis_common import error_envelope

# Family tokens (§6, D11) — STRING constants attached via error_envelope (D10).
_MCE_PARAM = "mce_param"                          # a bad/missing/extra Param selector
_MCE_CONFIG = "mce_config"                        # a config/row index out of range; a count no-op
_MCE_UNSUPPORTED = "mce_unsupported_operand"      # meta_for None / nsc / unsupported tier
_MCE_CELL = "mce_cell"                            # _mce_cells DataType/accessor/coercion/no-op
_CONFIG_RECONCILE = "config_reconcile"            # INVARIANT-1: the fresh-handle re-read drop
_MCE_VARIABLE = "mce_variable"                    # a phantom DOF (Variable but count unchanged)
_MCE_VARIABLE_INT = "mce_variable_integer_cell"   # a Variable solve on an Integer/String cell


# --------------------------------------------------------------------------- #
# Shared validation helpers.
# --------------------------------------------------------------------------- #
def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _fail(family, message, *, field=None, intended=None, actual=None, surface=None,
          row=None, config=None):
    """Build a ``SurfaceWriteError`` carrying a DISTINCT ``error_family`` (D10).

    ``SurfaceWriteError.error_family`` is a fixed CLASS attribute ("surface_write") and
    its constructor does NOT take ``error_family``; per D10 (NO new error class, families
    are STRING constants) the family is attached as an INSTANCE attribute that shadows the
    class default, so the handler's ``getattr(exc, "error_family", ...)`` reads the chosen
    MCE family. Extra reconcile diagnostics (``row``/``config``) are attached too so the
    ``_reconcile_extra`` surfacer can read them off the exception.
    """
    exc = SurfaceWriteError(
        message, field=field, intended=intended, actual=actual, surface=surface,
    )
    exc.error_family = family
    if row is not None:
        exc.row = row
    if config is not None:
        exc.config = config
    return exc


def _require_int(value, label):
    """Require an int (coercing an integral float, the number contract).

    Rejects a bool (an int subclass — a client miswrite), a non-integral float
    (``1.5``), a string, NaN/inf LOUD -> ``ToolParamError`` -> the caller's
    ``mce_param``/``mce_config`` family. The MCP/JSON round-trip delivers an integral
    float (``3`` -> ``3.0``); ``3.0`` is accepted + coerced.
    """
    import math
    if isinstance(value, bool):
        raise ToolParamError(f"{label} must be an integer, not a bool ({value!r})")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return int(value)
        raise ToolParamError(
            f"{label} must be an integer, got non-integral float {value!r}"
        )
    raise ToolParamError(
        f"{label} must be an integer, got {type(value).__name__} {value!r}"
    )


def _optional_int(params, key, label):
    """Pull an OPTIONAL int selector: absent/None -> None, else ``_require_int``."""
    if key not in params or params.get(key) is None:
        return None
    return _require_int(params[key], label)


# =========================================================================== #
# §5.1 add_configuration
# =========================================================================== #
def add_configuration(session, params):
    """Add a new MCE configuration, read-back-proven via the count increment (§5.1).

    Params: ``seed_from_current`` (bool, default False) — False -> an independent
    default new config (``AddConfiguration(False)``); True -> inherit the prior config's
    cell values (``AddConfiguration(True)``, probe §4).

    Read-back-as-proof (NOT the lying-return bool, F4/D14): ``NumberOfConfigurations``
    must increment by exactly 1; a no-op count -> ``mce_config`` refusal. Returns
    ``{ok, configuration_index, number_of_configurations, seeded_from_prior,
    current_configuration}``. NEVER raises past the boundary.
    """
    params = _require_dict(params)
    try:
        return _add_configuration_impl(session, params)
    except ToolParamError as exc:
        return error_envelope("add_configuration", _MCE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "add_configuration", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> mce_config (L26)
        return error_envelope(
            "add_configuration", _MCE_CONFIG,
            f"unexpected engine fault adding a configuration ({exc!r}); refusing rather "
            "than claiming an unverified config",
        )


def _add_configuration_impl(session, params):
    system = session.system

    seed = params.get("seed_from_current", False)
    if not isinstance(seed, bool):
        raise ToolParamError(
            f"seed_from_current must be a bool, got {type(seed).__name__} {seed!r}"
        )

    before = _mc.number_of_configurations(system)
    # AddConfiguration returns a LYING bool (F4/D14) — the count read-back is the proof.
    system.MCE.AddConfiguration(seed)
    after = _mc.number_of_configurations(system)
    if after != before + 1:
        raise _fail(
            _MCE_CONFIG,
            f"add_configuration did not increment NumberOfConfigurations "
            f"({before} -> {after}); the AddConfiguration call silently no-opped "
            "(its return bool is not a reliable proof — refusing rather than claiming "
            "a config that was not added)",
            field="number_of_configurations", intended=before + 1, actual=after,
        )
    return {
        "ok": True,
        "configuration_index": after,        # the new (1-based) config index
        "number_of_configurations": after,
        "seeded_from_prior": bool(seed),
        "current_configuration": _mc.current_configuration(system),
    }


# =========================================================================== #
# §5.2 set_config_operand
# =========================================================================== #
def set_config_operand(session, params):
    """Author a NEW MCE operand row (ChangeType + Param selectors), ZERO-mutation refuse (§5.2).

    Params: ``operand`` (str, REQUIRED — the MultiConfigOperandType CODE the agent picks
    from intent), ``surface`` (int, optional), ``param`` (int, optional — PRAM's parameter
    index), ``param3`` (int, optional).

    Validates against the catalog (``meta_for``): an absent member OR a nsc/unsupported
    tier -> ``mce_unsupported_operand`` (naming ``meta.reason``), ZERO mutation. A
    ``takes_surface`` operand REQUIRES ``surface`` bounds-checked ``1 <= surface <= N-1``;
    a surface-less operand handed a ``surface`` -> ``mce_param``; a ``takes_param`` operand
    (PRAM) requires ``param``. ANY refusal removes the orphan row (the ``add_operand``
    transactional precedent). Returns the 1-based ``row`` handle (does NOT write a value —
    AXIS-1 separability). NEVER raises past the boundary.
    """
    params = _require_dict(params)
    try:
        return _set_config_operand_impl(session, params)
    except ToolParamError as exc:
        return error_envelope("set_config_operand", _MCE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "set_config_operand", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> mce_param (L26)
        return error_envelope(
            "set_config_operand", _MCE_PARAM,
            f"unexpected engine fault authoring the MCE operand ({exc!r}); refusing "
            "rather than shipping an unverified operand row",
        )


def _set_config_operand_impl(session, params):
    system = session.system

    # --- Validate the operand CODE + selectors BEFORE any mutation. ---
    operand = params.get("operand")
    if not isinstance(operand, str) or not operand.strip():
        raise ToolParamError(
            f"operand must be a non-empty MultiConfigOperandType code string, got "
            f"{type(operand).__name__} {operand!r}"
        )
    operand = operand.strip()

    meta = _cat.meta_for(operand)
    if meta is None:
        # An absent enum member (live drift / typo) — fail-closed, ZERO mutation.
        raise _fail(
            _MCE_UNSUPPORTED,
            f"{operand!r} is not a known MultiConfigOperandType code (no catalog entry); "
            "refusing rather than authoring an unknown operand",
            field="operand", intended=operand, actual=None,
        )
    if not _cat.is_authorable(meta):
        # nsc / unsupported tier — fail-closed, naming meta.reason, ZERO mutation.
        reason = meta.reason or (
            f"the {operand!r} operand (tier {meta.tier!r}) is not authorable in MCE"
        )
        raise _fail(
            _MCE_UNSUPPORTED,
            f"{operand!r} is a non-authorable {meta.tier!r}-tier operand: {reason}",
            field="operand", intended=operand, actual=meta.tier,
        )

    surface = _optional_int(params, "surface", "surface")
    param_idx = _optional_int(params, "param", "param")
    param3 = _optional_int(params, "param3", "param3")
    field = _optional_int(params, "field", "field")

    n_surfaces = int(system.LDE.NumberOfSurfaces)

    # §2: the optional ``field`` selector (0-based Param1) is VALID ONLY for the
    # field-vignetting operands (FVDX/FVDY/FVCX/FVCY). A non-field-selector operand handed
    # ``field`` is refused ``mce_param`` (D8 disjoint) PRE-mutation (ZERO mutation — the
    # firewall runs BEFORE AddOperand, so no orphan row is ever created). ``field`` is
    # DISTINCT from ``param`` (which writes Param2): field -> Param1, NEVER conflated.
    if field is not None:
        if not _cat.selects_field(operand):
            raise _fail(
                _MCE_PARAM,
                f"operand {operand!r} is not a field-vignetting selector "
                f"(FVDX/FVDY/FVCX/FVCY); a field={field} selector is meaningless for it — "
                "refusing rather than authoring a wrong selector",
                field="field", intended=operand, actual=None,
            )
        # Validate the field index CLIENT-SIDE (no engine-clamp reliance, §2). The live
        # field count source is system.SystemData.Fields.NumberOfFields.
        n_fields = int(system.SystemData.Fields.NumberOfFields)
        if not (1 <= field <= n_fields):
            raise ToolParamError(
                f"field {field} out of range for operand {operand!r}; valid 1..{n_fields} "
                f"(NumberOfFields={n_fields})"
            )

    # A field-vignetting selector's SOLE selector is ``field`` (Param1); ``param`` (Param2)
    # and ``param3`` (Param3) are MEANINGLESS for it -> REFUSE PRE-mutation (D8 disjoint;
    # the mis-author fix — the handler used to write param3 unconditionally,
    # authoring a wrong Param3 on an FVDx row). ZERO mutation (before AddOperand). Mirrors
    # the non-surface-operand surface refusal below.
    if _cat.selects_field(operand):
        if param_idx is not None:
            raise _fail(
                _MCE_PARAM,
                f"operand {operand!r} is a field-vignetting selector; its sole selector is "
                f"field (Param1) — a param={param_idx} (Param2) is meaningless for it; "
                "refusing rather than authoring a wrong selector",
                field="param", intended=operand, actual=None,
            )
        if param3 is not None:
            raise _fail(
                _MCE_PARAM,
                f"operand {operand!r} is a field-vignetting selector; its sole selector is "
                f"field (Param1) — a param3={param3} (Param3) is meaningless for it; "
                "refusing rather than authoring a wrong selector",
                field="param3", intended=operand, actual=None,
            )

    # The catalog drives WHICH selectors are valid (§2.4), validated PRE-mutation so an
    # invalid selector never authors an orphan row at all.
    if meta.takes_surface:
        if surface is None:
            raise ToolParamError(
                f"operand {operand!r} requires a surface number (1..{n_surfaces - 1}); "
                "none supplied"
            )
        # The geometry firewall: OBJECT 0 refused (HZ-PARAM0); IMAGE N-1 allowed.
        if not (1 <= surface <= n_surfaces - 1):
            raise ToolParamError(
                f"surface {surface} out of range for operand {operand!r}; valid "
                f"1..{n_surfaces - 1} (OBJECT 0 and beyond IMAGE refused; N={n_surfaces})"
            )
    else:
        if surface is not None:
            raise ToolParamError(
                f"operand {operand!r} takes no surface (it is a system/field/wavelength "
                f"operand); a surface={surface} selector was supplied — refusing rather "
                "than authoring a meaningless selector"
            )
    if meta.takes_param:
        if param_idx is None:
            raise ToolParamError(
                f"operand {operand!r} requires a parameter index (param=...); none "
                "supplied"
            )

    # --- Author the row, then read-back-prove the ChangeType + Param selectors. The
    # orphan-row-removal transaction (the add_operand precedent): ANY failure after
    # AddOperand removes the row so a refusal is ZERO net mutation. ---
    member = _mc.resolve_member(system, operand)
    op = _mc.add_mce_operand(system)
    row = None
    try:
        row = int(op.OperandNumber)
        _mc.change_operand_type(system, op, member)
        # Read-back-as-proof: ChangeType genuinely took (a silent no-op leaves the prior
        # type whose selectors/cells are wrong — caught HERE, the add_coordinate_break
        # precedent).
        type_readback = str(op.Type)
        if str(member) not in type_readback and operand not in type_readback:
            raise _fail(
                _MCE_PARAM,
                f"MCE operand row {row} is {type_readback!r} after ChangeType to "
                f"{operand!r} — the retype silently no-opped; refusing rather than "
                "authoring the wrong operand",
                field="operand_type", intended=operand, actual=type_readback,
            )
        # Set the Param selectors (INTEGER properties, read-back-proven; a no-op write
        # -> mce_param). Unsupplied selectors default 0 (the engine default).
        if meta.takes_surface and surface is not None:
            _mc.set_param(op, 1, surface)
        # The field selector (§2) writes Param1 = field - 1 (0-based, probe §3),
        # read-back-proven by set_param. A field-selector operand is takes_surface=False,
        # so the surface branch above never ran — no Param1 conflict (field NEVER conflated
        # with param/Param2).
        if field is not None:
            _mc.set_param(op, 1, field - 1)
        if meta.takes_param and param_idx is not None:
            _mc.set_param(op, 2, param_idx)
        if param3 is not None:
            _mc.set_param(op, 3, param3)
    except Exception:
        # ZERO net mutation: remove the orphan row before re-raising (transactional).
        _remove_orphan(system, op)
        raise

    return {
        "ok": True,
        "operand": operand,
        "row": row,
        "surface": surface,
        "param": param_idx,
        "param3": param3,
        "field": field,
        "value_datatype": meta.value_datatype,
        "tier": meta.tier,
        "n_configs": _mc.number_of_configurations(system),
        "type_readback": type_readback,
    }


def _remove_orphan(system, op):
    """Remove a just-authored orphan MCE row on a refusal (transactional, never raises).

    Mirrors the ``add_operand`` orphan-row removal: a refusal AFTER ``AddOperand`` must
    leave ZERO net mutation. THROW-SWALLOWED — a teardown that itself throws must not
    mask the original refusal (the cleanup is best-effort, the L26 firewall still routes
    the original cause).
    """
    try:
        system.MCE.RemoveOperandAt(int(op.OperandNumber))
    except Exception:  # noqa: BLE001 — best-effort cleanup; never mask the real cause
        pass


# =========================================================================== #
# §5.3 set_config_value (the centerpiece — INVARIANT-1 lives here)
# =========================================================================== #
def set_config_value(session, params):
    """Write ONE per-config cell + INVARIANT-1 the fresh-handle re-read (§5.3 / §4).

    Params: ``row`` (int, REQUIRED — the 1-based operand row from set_config_operand),
    ``config`` (int, REQUIRED — the 1-based configuration index), ``value`` (number|str,
    REQUIRED).

    ``row`` bounds-checked vs ``NumberOfOperands``; ``config`` firewalled
    ``1 <= config <= NumberOfConfigurations`` BEFORE the cell fetch (HZ-CFGRANGE, ZERO
    mutation). Writes via ``_mce_cells.write_config_cell`` (DataType-keyed, read-back-
    proven; HZ-ACCESSOR/CBOR/L30 enforced there) THEN INVARIANT-1 — an INDEPENDENT fresh
    ``op.GetOperandCell(config)`` re-read must equal the written value (a drop ->
    ``config_reconcile``). ``written_value`` is the READ-BACK value, never echoed. NEVER
    raises past the boundary.
    """
    params = _require_dict(params)
    try:
        return _set_config_value_impl(session, params)
    except ToolParamError as exc:
        return error_envelope("set_config_value", _MCE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "set_config_value", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
            **_reconcile_extra(exc),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> mce_cell (L26)
        return error_envelope(
            "set_config_value", _MCE_CELL,
            f"unexpected engine fault writing the per-config value ({exc!r}); refusing "
            "rather than claiming an unverified cell",
        )


def _reconcile_extra(exc):
    """Surface the INVARIANT-1 diagnostic fields on a config_reconcile failure (§4)."""
    extra = {}
    for key in ("row", "config", "intended", "actual"):
        value = getattr(exc, key, None)
        if value is not None:
            extra[key] = value
    return extra


def _set_config_value_impl(session, params):
    system = session.system

    row = _require_int(params.get("row"), "row")
    config = _require_int(params.get("config"), "config")
    if "value" not in params:
        raise ToolParamError("value is required")
    value = params["value"]

    # row bounds-check vs NumberOfOperands (D14) -> mce_config.
    n_operands = int(system.MCE.NumberOfOperands)
    if not (1 <= row <= n_operands):
        raise _fail(
            _MCE_CONFIG,
            f"row {row} out of range; valid 1..{n_operands} "
            f"(NumberOfOperands={n_operands})",
            field="row", intended=row, actual=n_operands,
        )
    # config firewall BEFORE the cell fetch (HZ-CFGRANGE) -> mce_config, ZERO mutation.
    n_configs = _mc.number_of_configurations(system)
    if not (1 <= config <= n_configs):
        raise _fail(
            _MCE_CONFIG,
            f"config {config} out of range; valid 1..{n_configs} "
            f"(NumberOfConfigurations={n_configs})",
            field="config", intended=config, actual=n_configs,
        )

    op = system.MCE.GetOperandAt(row)
    # Resolve the operand's value_datatype from the catalog (the live cell DataType is the
    # discriminator inside _mce_cells; the catalog gives the EXPECTED kind for the drift
    # guard). A row whose operand code is unknown to the catalog -> mce_cell (refuse to
    # guess the accessor).
    code = _operand_code(op)
    meta = _cat.meta_for(code) if code is not None else None
    if meta is None:
        raise _fail(
            _MCE_CELL,
            f"row {row} carries operand code {code!r} which has no catalog entry; "
            "refusing rather than guessing the per-config cell accessor",
            field="operand", intended=code, actual=None,
        )

    # The DataType-keyed, read-back-proven write (HZ-ACCESSOR/CBOR/L30 inside _mce_cells).
    written = _mc.write_config_cell(system, op, config, meta.value_datatype, value)

    # INVARIANT-1 (§4): the INDEPENDENT fresh-handle re-read. write_config_cell already
    # read-back-proved at WRITE time on its own handle; the reconcile is the second read
    # on a FRESH op.GetOperandCell(config) — closes the "write-handle-stale-but-cell-
    # dropped" gap (ok:true => the authored (operand,config) cell reads back).
    if not _mc.reconcile_one_cell(system, op, config, meta.value_datatype, written):
        actual = _safe_reread(system, op, config, meta.value_datatype)
        raise _fail(
            _CONFIG_RECONCILE,
            f"INVARIANT-1: row {row} config {config} did not read back on the "
            f"independent fresh-handle re-read (wrote {written!r}, re-read {actual!r}); "
            "the cell silently dropped — refusing rather than claiming an authored value",
            field="reconcile", intended=written, actual=actual,
            row=row, config=config,
        )

    return {
        "ok": True,
        "row": row,
        "config": config,
        "operand": code,
        "value_datatype": meta.value_datatype,
        "written_value": _safe(written),
        "reconciled": True,
    }


def _operand_code(op):
    """Read the operand's MultiConfigOperandType CODE as a string (THROW-guarded -> None).

    Resolves the catalog token from the live operand. The MCE operand exposes its type
    as ``op.Type`` (an enum member whose ``str`` is the token); some builds expose
    ``op.TypeName``. Returns the matched catalog code, or ``None`` (the caller refuses).
    """
    for attr in ("Type", "TypeName"):
        try:
            raw = str(getattr(op, attr))
        except Exception:  # noqa: BLE001 — try the next accessor
            continue
        # The str may be the bare token or a qualified name; match against the catalog.
        if raw in _cat.MCE_OPERAND_META:
            return raw
        for code in _cat.MCE_OPERAND_META:
            if raw.endswith(code) or raw == code:
                return code
    return None


def _safe_reread(system, op, config, value_datatype):
    """Best-effort fresh re-read for the reconcile diagnostic (never raises)."""
    try:
        return _mc.read_config_cell(system, op, config, value_datatype)
    except Exception:  # noqa: BLE001 — the diagnostic read must not mask the refusal
        return None


# =========================================================================== #
# §5.4 set_config_variable
# =========================================================================== #
def set_config_variable(session, params):
    """Make a per-config Double cell an optimizer Variable, opt.Variables-proven (§5.4).

    Params: ``row`` (int, REQUIRED), ``config`` (int, REQUIRED).

    ``row``/``config`` firewalled (§5.3). REFUSES an Integer/String cell PRE-mutation (the
    L30 / CB-Order phantom-DOF class — the live ``cell.DataType`` is the only guard, the
    read-back is NOT a safety net): ``mce_variable_integer_cell``, ZERO mutation. ONLY a
    Double cell may be a variable. On a Double cell: ``cell.MakeSolveVariable()`` proved
    BOTH ways — the solve reads back ``Variable`` AND ``opt.Variables`` increments by
    exactly 1 (a phantom DOF -> ``mce_variable``). NEVER raises past the boundary.
    """
    params = _require_dict(params)
    try:
        # The prior-solve DISCLOSE stamp, at the PUBLIC entry, so no
        # success return inside the impl can be added later and quietly miss it.
        # Function-local import, the house style already used in this module for
        # ``_optimize_common`` (it imports these tool modules back).
        from . import _optimize_common as _oc_stamp
        return _oc_stamp._stamp_prior_solve_unchecked(
            _set_config_variable_impl(session, params), "per-configuration MCE")
    except ToolParamError as exc:
        return error_envelope("set_config_variable", _MCE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "set_config_variable", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> mce_variable (L26)
        return error_envelope(
            "set_config_variable", _MCE_VARIABLE,
            f"unexpected engine fault setting the per-config variable ({exc!r}); "
            "refusing rather than shipping an unverified DOF",
        )


def _set_config_variable_impl(session, params):
    system = session.system

    row = _require_int(params.get("row"), "row")
    config = _require_int(params.get("config"), "config")

    n_operands = int(system.MCE.NumberOfOperands)
    if not (1 <= row <= n_operands):
        raise _fail(
            _MCE_CONFIG,
            f"row {row} out of range; valid 1..{n_operands} "
            f"(NumberOfOperands={n_operands})",
            field="row", intended=row, actual=n_operands,
        )
    n_configs = _mc.number_of_configurations(system)
    if not (1 <= config <= n_configs):
        raise _fail(
            _MCE_CONFIG,
            f"config {config} out of range; valid 1..{n_configs} "
            f"(NumberOfConfigurations={n_configs})",
            field="config", intended=config, actual=n_configs,
        )

    op = system.MCE.GetOperandAt(row)
    cell = _mc.operand_cell(system, op, config)

    # THE LOAD-BEARING refusal (L30): an Integer/String cell is NEVER a continuous DOF.
    # The CB Q5 trap — MakeSolveVariable on an Integer cell does NOT raise, reads back
    # Variable, AND the engine may count it; so the ONLY guard is the live cell.DataType,
    # read BEFORE any mutation.
    kind = _mc.cell_kind(cell)
    if kind != "double":
        return error_envelope(
            "set_config_variable", _MCE_VARIABLE_INT,
            f"the row {row} config {config} cell is a {kind!r} cell; only a Double "
            "(continuous) cell may be an optimizer variable — refusing a variable solve "
            "on a discrete Integer/String cell (the engine accepts it silently as a real "
            "DOF; the read-back is not a safety net, L30). Zero mutation.",
            row=row, config=config,
        )

    # DOUBLE-VARY IDEMPOTENCY (#11, the set_asphere_variable template): an already-Variable
    # Double cell is benign — the 2nd MakeSolveVariable does NOT increment opt.Variables, so
    # baselining + demanding +1 would mis-flag it a phantom DOF (probe E). The kind!=double
    # refusal already ran ABOVE (an Integer/String cell never reaches here), so a Double
    # already-Variable cell IS a genuine continuous DOF. Return the honest idempotent
    # envelope (the L30 invariant preserved — the guard cannot smuggle a phantom). The
    # envelope shape MATCHES set_asphere_variable exactly (a uniform idempotent contract).
    variable_member = _variable_member(system)
    if _solve_type_name(cell) == str(variable_member):
        return {
            "ok": True, "row": row, "config": config,
            "is_variable": True, "dof_proven": True, "was_variable": True,
            "variables_before": None, "variables_after": None,
        }

    # Baseline the optimizer var count BEFORE the solve so the increment is the proof
    # (the Q5 authority — opt.Variables, not the cell read-back).
    before = _open_count_close_variables(system)

    try:
        cell.MakeSolveVariable()
    except Exception as exc:  # noqa: BLE001 — a solve THROW -> mce_variable
        raise _fail(
            _MCE_VARIABLE,
            f"could not make the row {row} config {config} cell a variable ({exc!r}); "
            "the engine rejected the solve",
            field="config_variable", intended="Variable", actual=None,
        ) from exc

    # Read back the solve Type == Variable (the cell-level proof) ...
    variable_member = _variable_member(system)
    solve_name = _solve_type_name(cell)
    if solve_name != str(variable_member):
        raise _fail(
            _MCE_VARIABLE,
            f"the row {row} config {config} variable solve did not take effect: solve "
            f"Type reads {solve_name!r} (silent no-op); refusing rather than claiming a "
            "DOF that does not exist",
            field="config_variable", intended="Variable", actual=solve_name,
        )
    # ... AND confirm the optimizer's own DOF count incremented (the Q5 falsification — a
    # flag that only reads back Variable is NOT a real DOF). dof_proven is POSITIVELY
    # established ONLY when BOTH counts are readable ints AND the increment is exactly +1.
    after = _open_count_close_variables(system)
    dof_proven = (
        isinstance(before, int) and isinstance(after, int) and after == before + 1
    )
    warning = None
    if isinstance(before, int) and isinstance(after, int) and not dof_proven:
        # The cell reads Variable but the optimizer count did NOT increment — a phantom
        # DOF; refuse.
        raise _fail(
            _MCE_VARIABLE,
            f"the row {row} config {config} cell reads back Variable but the optimizer "
            f"DOF count did not increment ({before} -> {after}); the solve is not a real "
            "optimizer variable — refusing rather than claiming a phantom DOF",
            field="config_variable_dof", intended=before + 1, actual=after,
        )
    if not dof_proven:
        # EITHER count is non-int (an asymmetric optimizer flake): the cell reads back
        # Variable but the opt.Variables increment proof was NOT established — warn +
        # surface dof_proven:false (the cb_surface path).
        warning = (
            "the optimizer DOF count could not be confirmed to have incremented by one "
            f"(the optimizer may have been unavailable: before={before!r}, "
            f"after={after!r}); the cell reads back Variable but the opt.Variables "
            "increment proof was NOT established — treat this DOF as UNCONFIRMED"
        )

    result = {
        "ok": True,
        "row": row,
        "config": config,
        "is_variable": True,
        "dof_proven": dof_proven,
        "variables_before": before if isinstance(before, int) else None,
        "variables_after": after if isinstance(after, int) else None,
    }
    if warning is not None:
        result["warning"] = warning
    return result


def _variable_member(system):
    """The live ``SolveType.Variable`` member (reused from the optimize tier)."""
    from . import _optimize_common as _oc
    return _oc._solve_type_variable_enum(system)


def _solve_type_name(cell):
    """Read ``cell.GetSolveData().Type`` as a string (the read-back truth source).

    THROW-guarded -> ``SurfaceWriteError`` (mce_variable): a read THROW on the proof
    leaves the DOF unverifiable — refuse rather than guess it took.
    """
    try:
        return str(cell.GetSolveData().Type)
    except Exception as exc:  # noqa: BLE001 — a solve read THROW -> mce_variable
        raise _fail(
            _MCE_VARIABLE,
            f"could not read back the solve type of an MCE cell ({exc!r}); the variable "
            "is unverifiable — refusing rather than guessing it took",
            field="config_variable", intended="Variable", actual=None,
        ) from exc


def _open_count_close_variables(system):
    """Open the optimizer, read ``opt.Variables`` (the DOF count), close it (Q5/L22).

    Copied from ``cb_surface._open_count_close_variables``: a cell that merely reads back
    Variable is NOT proof it is a real optimizer DOF — ``ILocalOptimization.Variables`` is
    the authority. Opens ONCE, reads, ``Close()`` in finally (the L22 single-seat reap).
    Returns the int count, or ``None`` if the optimizer is unavailable / the count is
    unreadable (a non-fatal degradation — the caller treats None as "could not confirm").
    """
    opt = None
    try:
        opt = system.Tools.OpenLocalOptimization()
        if opt is None:
            return None
        try:
            return int(opt.Variables)
        except Exception:  # noqa: BLE001 — an unreadable count degrades to None
            return None
    except Exception:  # noqa: BLE001 — the optimizer being unavailable is non-fatal
        return None
    finally:
        if opt is not None:
            try:
                opt.Close()
            except Exception:  # noqa: BLE001 — teardown must never raise (L22)
                pass


# =========================================================================== #
# §5.5 set_current_configuration
# =========================================================================== #
def set_current_configuration(session, params):
    """Switch the active MCE configuration, read-back-proven (THE BITE, §5.5).

    Params: ``config`` (int, REQUIRED — the 1-based config to make active).

    ``config`` firewalled ``1 <= config <= NumberOfConfigurations`` BEFORE the call.
    ``MCE.SetCurrentConfiguration(config)`` re-evaluates the optics (probe §4) and is
    read-back-proven ``CurrentConfiguration == config`` (a silent no-op caught). Out of
    range / a read-back mismatch -> ``mce_config``. The lever every config-aware analysis
    leans on. NEVER raises past the boundary.
    """
    params = _require_dict(params)
    try:
        return _set_current_configuration_impl(session, params)
    except ToolParamError as exc:
        return error_envelope("set_current_configuration", _MCE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "set_current_configuration", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> mce_config (L26)
        return error_envelope(
            "set_current_configuration", _MCE_CONFIG,
            f"unexpected engine fault switching the configuration ({exc!r}); refusing "
            "rather than claiming an unverified switch",
        )


def _set_current_configuration_impl(session, params):
    system = session.system

    config = _require_int(params.get("config"), "config")
    n_configs = _mc.number_of_configurations(system)
    if not (1 <= config <= n_configs):
        raise _fail(
            _MCE_CONFIG,
            f"config {config} out of range; valid 1..{n_configs} "
            f"(NumberOfConfigurations={n_configs})",
            field="config", intended=config, actual=n_configs,
        )

    system.MCE.SetCurrentConfiguration(config)
    # Read-back-as-proof: a silent no-op (the active config did not change) is caught.
    current = _mc.current_configuration(system)
    if current != config:
        raise _fail(
            _MCE_CONFIG,
            f"SetCurrentConfiguration({config}) did not take: CurrentConfiguration reads "
            f"{current} (silent no-op); refusing rather than claiming an unverified "
            "config switch",
            field="current_configuration", intended=config, actual=current,
        )
    return {
        "ok": True,
        "config": config,
        "number_of_configurations": n_configs,
        "current_configuration": current,
    }


# =========================================================================== #
# §5.6 describe_configurations (the read tool — fail-OPEN per-row, D16)
# =========================================================================== #
def describe_configurations(session, params):
    """Read the MCE ground truth: per-operand per-config value vectors (§5.6).

    Read-only. Walks ``MCE.NumberOfOperands`` rows x ``NumberOfConfigurations`` configs,
    reporting each operand's code + Param selectors + ``value_datatype``/``tier`` from the
    catalog + the per-config value vector + which configs carry a Variable solve.

    Fail-OPEN per-row (the READ tool ONLY, D16): a per-ROW read throw degrades that one row
    to ``{row, operand:None, error}``; a per-CELL read throw -> that cell's value is
    ``null`` + the config index added to ``unreadable_cells`` (DISCLOSED, never a stale
    value). An unknown operand code -> ``tier:"unknown"``. NEVER crashes the whole describe;
    NEVER mutates. NEVER raises past the boundary.
    """
    params = _require_dict(params)
    try:
        return _describe_configurations_impl(session)
    except Exception as exc:  # noqa: BLE001 — even the top-level read is guarded (L26)
        return error_envelope(
            "describe_configurations", _MCE_CONFIG,
            f"unexpected engine fault reading the MCE ({exc!r})",
        )


def _describe_configurations_impl(session):
    system = session.system
    n_configs = _mc.number_of_configurations(system)
    current = _mc.current_configuration(system)
    n_operands = int(system.MCE.NumberOfOperands)

    operands = []
    for row in range(1, n_operands + 1):
        operands.append(_describe_row(system, row, n_configs))

    return {
        "ok": True,
        "number_of_configurations": n_configs,
        "current_configuration": current,
        "operands": operands,
    }


def _describe_row(system, row, n_configs):
    """Read ONE MCE operand row fail-open (a row-level throw degrades that one row, D16)."""
    try:
        op = system.MCE.GetOperandAt(row)
        code = _operand_code(op)
        meta = _cat.meta_for(code) if code is not None else None
        value_datatype = meta.value_datatype if meta is not None else None
        tier = meta.tier if meta is not None else "unknown"
        surface = _read_param_safe(op, 1)
        param = _read_param_safe(op, 2)
        param3 = _read_param_safe(op, 3)

        values = []
        variable_configs = []
        unreadable_cells = []
        for cfg in range(1, n_configs + 1):
            value, is_var, ok = _read_cell_safe(system, op, cfg, value_datatype)
            if not ok:
                values.append(None)
                unreadable_cells.append(cfg)
            else:
                values.append(_safe(value))
                if is_var:
                    variable_configs.append(cfg)

        return {
            "row": row,
            "operand": code,
            "surface": surface,
            "param": param,
            "param3": param3,
            "value_datatype": value_datatype,
            "tier": tier,
            "values": values,
            "variable_configs": variable_configs,
            "unreadable_cells": unreadable_cells,
        }
    except Exception as exc:  # noqa: BLE001 — fail-open the ONE row (D16)
        return {"row": row, "operand": None, "error": f"{exc!r}"}


def _read_param_safe(op, n):
    """Read ``op.ParamN`` (THROW-guarded -> None; a describe read never crashes)."""
    try:
        return int(_mc.read_param(op, n))
    except Exception:  # noqa: BLE001 — an unreadable selector degrades to None
        return None


def _read_cell_safe(system, op, cfg, value_datatype):
    """Read one (op,cfg) cell value + Variable flag fail-open -> (value, is_var, ok).

    A per-CELL read throw -> ``(None, False, False)`` (the caller marks the cell
    unreadable). When ``value_datatype`` is None (an unknown operand) the cell is read
    via the LIVE ``cell.DataType`` discriminator inside ``_mce_cells`` (passing the
    catalog kind is the EXPECTED-kind drift hint; absent that, the live kind governs).
    """
    try:
        cell = _mc.operand_cell(system, op, cfg)
    except Exception:  # noqa: BLE001 — an unreadable cell handle
        return (None, False, False)
    # The variable flag (THROW-GUARDED — an unreadable solve reads as not-variable; the
    # guard and its fail-open semantics are UNCHANGED, because ``_read_cell_safe``'s whole
    # contract is to degrade per cell rather than raise).
    #
    # EXACT TOKEN, NOT ``"Variable" in str(...)``.
    # This is NOT a bug fix and must not be described as one: computed over
    # the LIVE roster (a live probe, 37 distinct rendered member names)
    # the ONLY name containing ``"Variable"`` as a substring IS ``"Variable"``, so the
    # substring form was correct BY CONSTRUCTION for the current engine build. It is
    # structural hygiene: a containment test is a PROXY for member identity, and the
    # roster is engine-version data, so a 38th member named e.g. ``"VariablePickup"`` or
    # ``"NotVariable"`` would silently start counting as an optimizer DOF it is not.
    #
    # ``== "Variable"`` IS THE RIGHT COMPARISON AND THAT IS MEASURED, not assumed: the
    # live ``str()`` of an MCE cell's solve object renders the BARE TOKEN (``"Variable"``
    # on a ``MakeSolveVariable``'d Double cell, ``"Fixed"`` on its sibling config) — not a
    # ``SolveType.Variable``-style qualified render an equality test would miss. The
    # identical exact-token fix already shipped one module over
    # (``cb_surface.py``'s ``_pickup_solve_type_name`` check); this closes the asymmetry
    # that was created deliberately and ticketed.
    is_var = False
    try:
        is_var = str(cell.GetSolveData().Type) == "Variable"
    except Exception:  # noqa: BLE001 — no readable solve -> not a variable
        is_var = False
    try:
        live_kind = _mc.cell_kind(cell)
        expected = value_datatype if value_datatype is not None else {
            "double": "Double", "int": "Integer", "string": "String",
        }.get(live_kind, "Double")
        value = _mc.read_config_cell(system, op, cfg, expected)
        return (value, is_var, True)
    except Exception:  # noqa: BLE001 — an unreadable cell value -> disclosed null
        return (None, is_var, False)


# --------------------------------------------------------------------------- #
def _safe(value):
    """JSON-safe value: numbers via the tier-wide safe_float; strings pass through."""
    if isinstance(value, str):
        return value
    from .._io import safe_float
    return safe_float(value)


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
ADD_CONFIGURATION_SPEC = ToolSpec(
    name="add_configuration",
    handler=add_configuration,
    required_params=(),
    param_types={"seed_from_current": "boolean"},
    description=(
        "Add a multi-configuration (zoom/multi-config) configuration to the system "
        "(seed_from_current=true inherits the current config's per-config values, false "
        "[default] starts a fresh default config). Proves the add by the configuration "
        "count incrementing, NOT the engine's return flag. Returns the new "
        "configuration_index + number_of_configurations. Gotcha: a fresh system has 1 "
        "config; the MCE per-config cell matrix is authored with set_config_operand / "
        "set_config_value, and read with describe_configurations. See "
        "set_config_operand, set_current_configuration."
    ),
)

SET_CONFIG_OPERAND_SPEC = ToolSpec(
    name="set_config_operand",
    handler=set_config_operand,
    required_params=("operand",),
    param_types={
        "operand": "string",
        "surface": "number",
        "param": "number",
        "param3": "number",
        "field": "number",
    },
    description=(
        "Author a multi-config operand ROW (e.g. THIC/CRVT/GLSS/APER per-config) — pass "
        "the MultiConfigOperandType CODE plus the surface it acts on (required for "
        "geometry/coord-break operands; refused for a system/field/wavelength operand) "
        "and PRAM's parameter index. For a per-config field-vignetting operand "
        "(FVDX/FVDY/FVCX/FVCY) pass the 1-based field selector (field=N targets field N); "
        "field is refused for any other operand. Returns the 1-based row handle to write "
        "values with set_config_value. Proves the operand type by read-back; refuses an "
        "unknown / non-sequential-component (NSC) / unsupported operand and a wrong/missing "
        "surface or field selector with ZERO mutation. Gotcha: this authors the row only — "
        "set the per-config values separately with set_config_value. See set_config_value, "
        "set_vignetting, describe_configurations."
    ),
)

SET_CONFIG_VALUE_SPEC = ToolSpec(
    name="set_config_value",
    handler=set_config_value,
    required_params=("row", "config", "value"),
    param_types={"row": "number", "config": "number", "value": "number"},
    description=(
        "Set ONE per-config value of an MCE operand row (row from set_config_operand, "
        "config is the 1-based configuration index, value is the number — or a glass "
        "NAME string for a GLSS operand). DataType-keyed + read-back-proven, then an "
        "INDEPENDENT fresh-handle re-read confirms the cell actually holds the value "
        "(a silent drop is refused). Returns the read-back written_value, never the "
        "echoed request. Gotcha: an Integer cell rejects an integral float written as a "
        "float; a Double operand handed a string is refused. See set_config_operand, "
        "set_config_variable."
    ),
)

SET_CONFIG_VARIABLE_SPEC = ToolSpec(
    name="set_config_variable",
    handler=set_config_variable,
    required_params=("row", "config"),
    param_types={"row": "number", "config": "number"},
    description=(
        "Make a per-config cell (row, config) an optimizer Variable, proved by the "
        "optimizer's own DOF count incrementing. Gotcha: REFUSES an Integer/String cell "
        "— only a continuous Double cell may be a variable; the engine silently accepts "
        "a variable on a discrete Integer flag and the read-back is not a safety net "
        "(L30). See set_config_value, set_variable."
    ),
)

SET_CURRENT_CONFIGURATION_SPEC = ToolSpec(
    name="set_current_configuration",
    handler=set_current_configuration,
    required_params=("config",),
    param_types={"config": "number"},
    description=(
        "Switch the ACTIVE multi-config configuration (1-based) — re-evaluates the "
        "optics so every subsequent analysis reads THAT config's design. Read-back-"
        "proven (a silent no-op is refused); out-of-range is refused. This is the lever "
        "for grading a zoom/multi-config design config-by-config. See "
        "describe_configurations, add_configuration."
    ),
)

DESCRIBE_CONFIGURATIONS_SPEC = ToolSpec(
    name="describe_configurations",
    handler=describe_configurations,
    required_params=(),
    param_types={},
    description=(
        "Read the multi-config ground truth: every MCE operand row x configuration "
        "value vector, plus which configs carry a Variable solve and the active config. "
        "Best-effort per row (a row/cell that cannot be read is disclosed with a null / "
        "unreadable_cells, never a stale value) and never mutates. Use this to see the "
        "per-config design before authoring with set_config_operand / set_config_value."
    ),
)

TOOL_SPECS = (
    ADD_CONFIGURATION_SPEC,
    SET_CONFIG_OPERAND_SPEC,
    SET_CONFIG_VALUE_SPEC,
    SET_CONFIG_VARIABLE_SPEC,
    SET_CURRENT_CONFIGURATION_SPEC,
    DESCRIBE_CONFIGURATIONS_SPEC,
)
