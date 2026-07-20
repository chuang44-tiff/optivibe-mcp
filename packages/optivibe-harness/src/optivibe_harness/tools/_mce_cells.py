"""tools/_mce_cells.py — the DataType-keyed per-config cell read-back writer/reader (MCE S1 §3).

NOT dispatchable (no ``TOOL_SPECS``). The single place the MCE primitive reads and
writes a multi-config operand's per-config VALUE cell type-aware, keyed on the LIVE
``cell.DataType`` (Double -> ``DoubleValue``, Integer -> ``IntegerValue``, String ->
``Value``; reading/writing the WRONG accessor RAISES the live ``ArgumentException``).

This MIRRORS ``_cb_cells`` / ``_tol_cells`` / ``_merit_cells`` (the same
``cell.DataType`` discriminator + Header-match drift guard + read-back firewall + the
integral-float-into-an-int-cell rejection) but is a SEPARATE re-implementation: the
Multi-Configuration Editor is its OWN editor surface (distinct from the LDE / MFE /
TDE), so we do NOT cross-import those modules (the same decoupling each prior layer
made — findings §7). The MCE adds **String** (GLSS / MOFF / …) to the CB/asphere
Int/Double pair.

Probe-grounded rules this module encodes (from the live probe captures):

- the per-config cell is ``op.GetOperandCell(cfg)`` with ``cfg`` **1-based** (probe §2);
- the cell ``Header`` reads ``"Config N*"`` / ``"Config N"`` (the index + the active
  ``*`` vary), so the drift guard checks ``startswith("Config")``, NOT an exact-N parse;
- the discriminator is the LIVE ``cell.DataType`` (``"Double"`` / ``"Integer"`` /
  ``"String"``), NEVER the catalog declaration — so an engine drift is CAUGHT, not
  assumed (the table-says-Double-but-live-Integer case RAISES at ``_expect_layout``);
- ``CBOR`` is a Double cell (HZ-CBOR) — the catalog says Double and the live cell
  agrees, so a CBOR write goes through the Double accessor and ``1.0`` is accepted;
- an Integer cell (the 25 frozen Integer members) is a discrete flag — an integral
  float (``7.0``) is REJECTED (the strict CB L30 rule, NOT ``_tol_cells``'s integral-
  float acceptance);
- a String cell requires a non-empty ``str`` (an empty GLSS silently means "inherit"
  — refuse the ambiguous blank).

This module owns ONLY the per-config VALUE cell. The ``Param1/2/3`` integer selectors
are handled in ``set_config_operand`` (the tool), NOT here. Every refusal is a bare
``SurfaceWriteError`` with ``error_family="mce_cell"`` (NO new error class).

Live ZOS-API integration: exercised by a live integration test; unit-tested
against the fixture-seeded fake MCE doubles whose accessors
RAISE on the wrong type exactly like the live ``ArgumentException`` and whose
per-config cells COMPUTE their read-back from what was written.
"""
import math

from ..errors import SurfaceWriteError, ToolParamError


# The error family every ``_mce_cells`` refusal carries (§6 — NO new error class;
# a bare ``SurfaceWriteError`` with this family attached).
_MCE_CELL = "mce_cell"

# The per-config cell Header prefix (probe §2: ``"Config N*"`` / ``"Config N"``). The
# drift guard checks the PREFIX, not an exact-N parse (the index + ``*`` vary).
_CONFIG_HEADER_PREFIX = "Config"

# The read-back proof's absolute floor (the ``_cb_cells`` near-ULP floor that catches a
# >1e-15 collapse-to-zero). The ``rel_tol=1e-9`` governs normal magnitudes; the abs
# floor only matters in the near-zero regime.
_READBACK_ABS_TOL = 1e-15


def _mce_cell_error(message):
    """Build a bare ``SurfaceWriteError`` carrying ``error_family="mce_cell"``.

    The dispatch boundary surfaces ``error_family`` verbatim; we attach the
    ``"mce_cell"`` family by setting it on the instance (the ``cb_surface`` precedent —
    a plain ``SurfaceWriteError`` whose family is overridden, NO new subclass).
    """
    exc = SurfaceWriteError(message)
    exc.error_family = _MCE_CELL
    return exc


# --------------------------------------------------------------------------- #
# Enum resolution (live-enum-is-truth, same seam as _cb_cells / _optimize_common).
# --------------------------------------------------------------------------- #
def _mce_operand_type_enum(system):
    """Resolve the live ``MultiConfigOperandType`` enum TYPE (the operand-code enum).

    A fake system injects ``_enum_types["MultiConfigOperandType"]``; otherwise the
    live ``ZOSAPI.Editors.MCE.MultiConfigOperandType`` namespace. A resolution failure
    surfaces as a ``ToolParamError`` (a param-class problem), never an internal crash.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "MultiConfigOperandType" in injected:
        return injected["MultiConfigOperandType"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.MCE as _mce  # type: ignore

        return _mce.MultiConfigOperandType
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            "could not resolve MultiConfigOperandType from ZOSAPI.Editors.MCE: "
            f"{exc}"
        )


def resolve_member(system, token):
    """Resolve ``token`` against the live ``MultiConfigOperandType`` enum.

    The live enum is the source of truth (``_resolve_enum`` — a guarded ``getattr``).
    An unknown / non-string token raises ``ToolParamError`` (a client param bug),
    never silently resolves to a stale member.
    """
    from ..enums import _resolve_enum

    enum_type = _mce_operand_type_enum(system)
    return _resolve_enum(enum_type, token)


# --------------------------------------------------------------------------- #
# MCE accessor helpers (no LDE coupling — §3.1).
# --------------------------------------------------------------------------- #
def add_mce_operand(system):
    """``system.MCE.AddOperand()`` -> the new operand, THROW-guarded -> ``mce_cell``."""
    try:
        return system.MCE.AddOperand()
    except Exception as exc:  # noqa: BLE001 — an AddOperand THROW -> mce_cell, never internal
        raise _mce_cell_error(
            f"could not add a new MCE operand row ({exc!r}); the MCE is unreadable — "
            "refusing rather than guessing"
        ) from exc


def change_operand_type(system, op, member):
    """``op.ChangeType(member)`` THROW-guarded -> ``mce_cell``.

    ``member`` is the resolved ``MultiConfigOperandType`` enum member. A silent
    ChangeType no-op is caught by the CALLER's ``op.Type`` read-back (the
    ``add_coordinate_break`` precedent); this helper only firewalls a raw THROW.
    """
    try:
        return op.ChangeType(member)
    except Exception as exc:  # noqa: BLE001 — a ChangeType THROW -> mce_cell, never internal
        raise _mce_cell_error(
            f"could not ChangeType the MCE operand to {member!r} ({exc!r}); refusing "
            "rather than guessing"
        ) from exc


def operand_cell(system, op, cfg):
    """``op.GetOperandCell(cfg)`` (1-based) THROW-guarded -> ``mce_cell``."""
    try:
        return op.GetOperandCell(cfg)
    except Exception as exc:  # noqa: BLE001 — a GetOperandCell THROW -> mce_cell
        raise _mce_cell_error(
            f"could not fetch the config-{cfg} cell of an MCE operand ({exc!r}); the "
            "cell layout is unreadable — refusing rather than guessing"
        ) from exc


def set_param(op, n, value):
    """Write ``op.Param{n} = int(value)`` read-back-proven; a no-op -> ``mce_param``.

    The ``Param1/2/3`` selectors are bare INTEGER properties (NOT cells). Writes the
    property, reads it back, and refuses a silent no-op. A non-int ``value`` is a
    client bug -> ``ToolParamError`` (the handler maps it to ``mce_param``).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolParamError(
            f"MCE Param{n} must be an exact integer, got {type(value).__name__} "
            f"{value!r}"
        )
    attr = f"Param{n}"
    try:
        setattr(op, attr, int(value))
    except Exception as exc:  # noqa: BLE001 — a selector write THROW -> mce_param
        raise ToolParamError(
            f"could not write MCE {attr}={value!r} ({exc!r})"
        ) from exc
    actual = read_param(op, n)
    if actual != int(value):
        raise ToolParamError(
            f"MCE {attr} write did not read back: wrote {int(value)!r}, read "
            f"{actual!r} — the engine silently rejected the selector write"
        )
    return actual


def read_param(op, n):
    """Read ``op.Param{n}`` -> ``int``, guarded -> ``mce_param``."""
    attr = f"Param{n}"
    try:
        return int(getattr(op, attr))
    except Exception as exc:  # noqa: BLE001 — a selector read THROW -> mce_param
        raise ToolParamError(
            f"could not read MCE {attr} ({exc!r})"
        ) from exc


def number_of_configurations(system):
    """``system.MCE.NumberOfConfigurations`` -> ``int``, THROW-guarded -> ``mce_cell``."""
    try:
        return int(system.MCE.NumberOfConfigurations)
    except Exception as exc:  # noqa: BLE001 — an MCE read THROW -> mce_cell
        raise _mce_cell_error(
            f"could not read MCE.NumberOfConfigurations ({exc!r}); refusing rather "
            "than guessing"
        ) from exc


def current_configuration(system):
    """``system.MCE.CurrentConfiguration`` -> ``int``, THROW-guarded -> ``mce_cell``."""
    try:
        return int(system.MCE.CurrentConfiguration)
    except Exception as exc:  # noqa: BLE001 — an MCE read THROW -> mce_cell
        raise _mce_cell_error(
            f"could not read MCE.CurrentConfiguration ({exc!r}); refusing rather than "
            "guessing"
        ) from exc


# --------------------------------------------------------------------------- #
# The discriminator + drift guard (HZ-ACCESSOR, HZ-CBOR, HZ-UNREADABLE — §3.2).
# --------------------------------------------------------------------------- #
def _data_type(cell):
    """The live ``cell.DataType`` as a ``str`` (Double/Integer/String), THROW-guarded."""
    try:
        return str(cell.DataType)
    except Exception as exc:  # noqa: BLE001 — a DataType read THROW -> mce_cell
        raise _mce_cell_error(
            f"could not read an MCE cell DataType ({exc!r}); the cell kind is "
            "unclassifiable — refusing rather than guessing"
        ) from exc


def cell_kind(cell) -> str:
    """Classify a LIVE MCE cell -> ``"double"`` / ``"int"`` / ``"string"`` from DataType.

    The PRIMARY discriminator is the live ``cell.DataType``: ``"Integer"`` -> ``"int"``,
    ``"String"`` -> ``"string"``, else ``"double"`` (the same Int/Double rule the CB +
    merit + tolerance layers key on, extended with String for the MCE). A DataType read
    THROW -> ``mce_cell`` (never an opaque dispatch internal).
    """
    dt = _data_type(cell)
    if dt == "Integer":
        return "int"
    if dt == "String":
        return "string"
    return "double"


def _datatype_to_kind(value_datatype):
    """Map a catalog ``value_datatype`` ("Double"/"Integer"/"String") -> the kind string."""
    if value_datatype == "Integer":
        return "int"
    if value_datatype == "String":
        return "string"
    return "double"


def _read_header(cell):
    """Read ``cell.Header`` -> ``str``, THROW-guarded -> ``mce_cell``."""
    try:
        return str(cell.Header)
    except Exception as exc:  # noqa: BLE001 — a Header read THROW -> mce_cell
        raise _mce_cell_error(
            f"could not read an MCE cell Header ({exc!r}); the cell layout is "
            "unreadable — refusing rather than guessing"
        ) from exc


def _expect_layout(cell, expected_datatype) -> str:
    """Verify the live cell's Header is a Config cell AND its DataType matches expected.

    (a) Header check — ``str(cell.Header).startswith("Config")`` (the live Header is
        ``"Config N*"`` / ``"Config N"``, probe §2; the index + ``*`` vary, so a
        ``startswith`` check, NOT an exact-N parse). A non-``Config`` Header -> RAISE
        (refuse the wrong cell).
    (b) DataType check — the live ``cell_kind(cell)`` must match the catalog's
        ``expected_datatype`` (CBOR=Double frozen). A drift (table-says-Double-but-
        live-Integer) -> RAISE (the catalog drifted from the engine; refuse the wrong
        accessor).

    Returns the verified live kind (``"double"`` / ``"int"`` / ``"string"``). A
    ``cell.DataType`` / ``cell.Header`` read THROW -> RAISE.
    """
    live_header = _read_header(cell)
    if not live_header.startswith(_CONFIG_HEADER_PREFIX):
        raise _mce_cell_error(
            f"MCE cell layout mismatch: expected a {_CONFIG_HEADER_PREFIX!r} header "
            f"but the live cell Header is {live_header!r} — refusing rather than "
            "reading/writing the wrong cell"
        )
    expected_kind = _datatype_to_kind(expected_datatype)
    live_kind = cell_kind(cell)
    if live_kind != expected_kind:
        raise _mce_cell_error(
            f"MCE cell {live_header!r} is a {live_kind} cell live but the catalog "
            f"declared a {expected_kind} cell ({expected_datatype}) — the table "
            "drifted from the engine; refusing rather than the wrong accessor"
        )
    return live_kind


# --------------------------------------------------------------------------- #
# Coercion (HZ-L30 — §3.3): the _cb_cells rule + String.
# --------------------------------------------------------------------------- #
def _coerce_for_kind(kind, value):
    """Coerce ``value`` to the cell ``kind``; reject bool / the L30 trap / non-finite /
    a non-str-or-empty string (the ``_cb_cells`` rule + String — §3.3).

    - reject ``bool`` FIRST (an int subclass — a client miswrite), for ALL kinds;
    - ``kind == "int"``: an EXACT integer only — ``7.0`` REJECTED (the L30 integral-
      float trap; the 25 Integer MCE cells are discrete flags, never a JSON-round-
      tripped float — the strict CB rule, NOT ``_tol_cells``'s integral-float accept);
    - ``kind == "double"``: a FINITE number (int or float) -> ``float``; inf/-inf/nan
      REJECTED pre-write;
    - ``kind == "string"``: a non-empty ``str`` only (GLSS / MOFF / COTN / …). A
      non-str / empty-str -> RAISE (an empty GLSS silently means "inherit" — refuse the
      ambiguous blank; the catalog-in-use auto-load is DEFERRED).
    """
    if isinstance(value, bool):
        raise _mce_cell_error(
            f"MCE cell value must not be a bool ({value!r}); a bool is an int subclass "
            "— a client miswrite"
        )
    if kind == "int":
        # An EXACT integer only. ``7.0`` is REFUSED (it coerces silently under a naive
        # isinstance(int) guard — the L30 trap; the Integer MCE cells are discrete
        # flags, never a JSON-round-tripped float).
        if isinstance(value, int):
            return int(value)
        raise _mce_cell_error(
            f"MCE cell is an integer cell; got {type(value).__name__} {value!r} (need "
            "an exact integer — refusing the integral-float coerce, L30)"
        )
    if kind == "string":
        if not isinstance(value, str):
            raise _mce_cell_error(
                f"MCE cell is a string cell; got {type(value).__name__} {value!r} "
                "(need a string)"
            )
        if value == "":
            raise _mce_cell_error(
                "MCE cell is a string cell; got an empty string — an empty value "
                "silently means 'inherit' on the engine; refusing the ambiguous blank "
                "(D17)"
            )
        return value
    # kind == "double"
    if not isinstance(value, (int, float)):
        raise _mce_cell_error(
            f"MCE cell is a numeric cell; got {type(value).__name__} {value!r} (need a "
            "number)"
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise _mce_cell_error(
            f"MCE cell is a numeric cell; got the non-finite value {value!r} "
            "(inf/-inf/nan are non-physical and are rejected pre-write)"
        )
    return coerced


# --------------------------------------------------------------------------- #
# The read-back-proven READ + WRITE (HZ-DROP, HZ-ACCESSOR — §3.4).
# --------------------------------------------------------------------------- #
def _read_value(cell, kind):
    """Type-aware READ of one cell through the accessor MATCHING ``kind``, THROW-guarded.

    ``"double"`` -> ``float(cell.DoubleValue)``; ``"int"`` -> ``int(cell.IntegerValue)``;
    ``"string"`` -> ``str(cell.Value)``. A read THROW -> ``mce_cell``.
    """
    try:
        if kind == "int":
            return int(cell.IntegerValue)
        if kind == "string":
            return str(cell.Value)
        return float(cell.DoubleValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> mce_cell, never internal
        raise _mce_cell_error(
            f"could not read the {kind} value of an MCE cell ({exc!r}); the cell read "
            "is unverifiable — refusing rather than guessing"
        ) from exc


def read_config_cell(system, op, cfg, value_datatype):
    """Type-aware READ of one per-config value cell (DataType-keyed, layout-verified).

    Fetches ``op.GetOperandCell(cfg)`` (1-based), verifies the Config-header + the
    DataType-vs-catalog drift (``_expect_layout`` RAISES on drift), then reads the
    accessor MATCHING the verified live kind. NEVER reads the wrong accessor.
    """
    cell = operand_cell(system, op, cfg)
    kind = _expect_layout(cell, value_datatype)
    return _read_value(cell, kind)


def _readback_ok(intended, actual):
    """Read-back equality for an MCE cell value (int/string exact, double tight tol)."""
    if isinstance(intended, float):
        return (
            actual is not None
            and isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and math.isfinite(actual)
            and math.isclose(actual, intended, rel_tol=1e-9, abs_tol=_READBACK_ABS_TOL)
        )
    return actual == intended


def write_config_cell(system, op, cfg, value_datatype, value):
    """Type-aware WRITE of one per-config value cell, read-back-proven (the §3.4 firewall).

    Steps (mirroring ``_cb_cells.write_cb_cell`` / ``_tol_cells``):

    1. fetch ``op.GetOperandCell(cfg)`` (1-based, THROW-guarded);
    2. ``_expect_layout`` — verify the Config-header + the DataType-vs-catalog match (a
       drifted layout RAISES, never the wrong cell);
    3. ``_coerce_for_kind`` — bool / the L30 integral-float / non-finite double /
       empty-or-non-str string all RAISE pre-write;
    4. write the accessor MATCHING the kind (Double -> ``DoubleValue``, Integer ->
       ``IntegerValue``, String -> ``Value``); a write THROW RAISES;
    5. read it back type-aware and verify (int/string EXACT; double ``math.isclose``
       per the ``_cb_cells`` near-ULP floor). A silent no-op -> RAISE.

    Returns the read-back value. ``cfg`` is **1-based** (probe §2).
    """
    cell = operand_cell(system, op, cfg)
    kind = _expect_layout(cell, value_datatype)
    coerced = _coerce_for_kind(kind, value)
    try:
        if kind == "int":
            cell.IntegerValue = coerced
        elif kind == "string":
            cell.Value = coerced
        else:
            cell.DoubleValue = coerced
    except Exception as exc:  # noqa: BLE001 — a write THROW -> mce_cell, never internal
        raise _mce_cell_error(
            f"could not write the {kind} value {coerced!r} to an MCE config-{cfg} cell "
            f"({exc!r}); the engine rejected the write — refusing rather than shipping "
            "an unverified cell"
        ) from exc

    actual = _read_value(cell, kind)
    if not _readback_ok(coerced, actual):
        raise _mce_cell_error(
            f"MCE config-{cfg} cell write did not read back: wrote {coerced!r}, read "
            f"{actual!r} — the engine silently rejected the write (a no-op), refusing "
            "rather than shipping an unverified cell"
        )
    return actual


def solve_type_name(cell):
    """GUARDED sibling of ``mce_config._solve_type_name``: the cell's solve-type name.

    Returns ``str(cell.GetSolveData().Type)`` or ``None`` on a read throw. ``None`` means
    "unverifiable — do NOT nudge" (fail-closed). NEVER raises (unlike
    ``mce_config._solve_type_name``, which RAISES ``mce_variable`` — WRONG for a
    never-raise preflight/nudge). S5 §2.4: the 4b nudge skips any cell whose solve is not
    ``Fixed``/``Variable`` (a slaved ``*Pickup``/``ConfigPickup``/``Thermal*`` cell must
    never be independently nudged — probe T4); a ``None`` read is treated as un-nudgeable.
    """
    try:
        return str(cell.GetSolveData().Type)
    except Exception:  # noqa: BLE001 — a solve read throw -> unverifiable, never raise
        return None


def reconcile_one_cell(system, op, cfg, value_datatype, intended) -> bool:
    """INVARIANT-1: an INDEPENDENT FRESH-handle re-read asserts the cell holds ``intended``.

    Re-fetches ``op.GetOperandCell(cfg)`` (a NEW cell handle, NOT the write-time
    handle), reads it through the ``value_datatype`` accessor, and asserts it equals
    ``intended`` (DataType-correct: int/string exact; double ``math.isclose`` per the
    near-ULP floor). The fresh-handle re-read closes the "write-handle-stale-but-cell-
    dropped" gap — the ``ok:true => the authored (operand,config) cell reads
    back`` contract.

    Returns ``True`` iff the fresh read-back matches ``intended``; the caller turns a
    ``False`` into a ``config_reconcile`` refusal (the tool layer, §4). A read THROW
    propagates as ``mce_cell`` (never a silent True).
    """
    actual = read_config_cell(system, op, cfg, value_datatype)
    # Coerce ``intended`` to the same shape the cell stored so the compare is exact:
    # a Double cell stored a float; an Int cell an int; a String cell a str.
    kind = _datatype_to_kind(value_datatype)
    if kind == "double":
        return _readback_ok(float(intended), actual)
    return actual == intended


__all__ = [
    "cell_kind",
    "read_config_cell",
    "write_config_cell",
    "reconcile_one_cell",
    "solve_type_name",
    "add_mce_operand",
    "change_operand_type",
    "operand_cell",
    "set_param",
    "read_param",
    "number_of_configurations",
    "current_configuration",
    "resolve_member",
    "_mce_operand_type_enum",
    "_expect_layout",
    "_coerce_for_kind",
    "_MCE_CELL",
]
