"""tools/_cb_cells.py — the coordinate-break cell substrate.

NOT dispatchable (no ``TOOL_SPECS``). The single place the CB primitive reads and
writes a coordinate-break surface's six PARAMETER cells type-aware, keyed on the
LIVE ``cell.DataType`` (the merit/tolerance Int/Double discriminator — Integer ->
``IntegerValue``, Double -> ``DoubleValue``; reading the WRONG accessor RAISES the
live ``ArgumentException``). It also houses the GetGlobalMatrix angle-extraction
proof-oracle math (Q6) and the ``is_coordinate_break`` predicate (the LensSpec guard
+ the CB tools both consult it).

This MIRRORS ``_merit_cells.write_verified_cell`` / ``_tol_cells.write_int_cell``
(the same ``cell.DataType`` rule + read-back firewall + integral-float-into-an-int-
cell rejection) but is a SEPARATE re-implementation: the Lens Data Editor's
coordinate-break Par cells are a DIFFERENT editor surface from the MFE/TDE, so we do
NOT cross-import those modules (the same decoupling the tol layer made).

Probe-grounded rules this module encodes (from the live probe captures, P1 +
Q5/Q6, all [Discovered-live-probed]):

- the six Par cells are addressed by ``row.GetSurfaceCell(SurfaceColumn.ParN)`` and
  discriminated by ``cell.DataType``: Par1 Decenter X, Par2 Decenter Y, Par3 Tilt
  About X, Par4 Tilt About Y, Par5 Tilt About Z = **Double**; Par6 Order =
  **Integer** (Q5: the discriminator extends verbatim to CB Par cells);
- the WRITE PATH IS THE CELL, never the typed ``row.SurfaceData.TiltAbout_*`` setter
  — that setter is a SILENT NO-OP (P1 ``typed_setter_is_noop: True``); a tool MUST
  read back through the cell AND through the global frame;
- ``LDE.GetGlobalMatrix(surf)`` returns a 13-tuple ``(success, R11..R33 row-major,
  X, Y, Z)``; the DECISIVE proof gate COMPOSES the expected rotation block as an
  Order-branched matrix PRODUCT from the authored tilts (Order 0 ->
  ``Rx(tx).Ry(ty).Rz(tz)``, Order 1 -> ``Rz(tz).Ry(ty).Rx(tx)``) and compares it
  ELEMENT-WISE to the measured block within 1e-9 (``verify_global_rotation`` /
  ``rotation_residual``; worst observed residual 1.67e-16). The atan2
  DECOMPOSITION (``tiltX = atan2(R32, R33)``, ...) is kept ONLY as the informational
  single-axis readout (``global_rotation_angles``) — it false-refuses combined
  multi-axis tilts and wraps past +/-180 deg, so it is NEVER the gate;
- ``is_coordinate_break(row)`` keys on the CB substring of ``str(row.Type)`` (the
  same idiom ``lens_describe`` reads ``row.Type`` with — read-surface does NOT expose
  the type).

Live ZOS-API integration: exercised by a live integration test; unit-tested
against the fixture-seeded fake LDE/cell doubles whose
accessors RAISE on the wrong type exactly like the live ``ArgumentException`` and
whose ``GetGlobalMatrix`` COMPUTES the rotation from the authored tilt (so a wrong
angle-extraction reddens).
"""
import math

from ..errors import SurfaceWriteError, ToolParamError

# CB §1: the surface-type substring that marks a coordinate-break row. ``row.Type``
# reads ``CoordinateBreak`` after ChangeType (P1); the describe layer keys on the CB
# substring too, so a future "Coordinate Break (...)" spelling still matches.
_CB_TYPE_SUBSTRING = "CoordinateBreak"

# CB §1: the ordered Par-cell map — name -> (SurfaceColumn member, expected Header,
# expected DataType). The SINGLE source of truth. The ``SurfaceColumn`` member NAME
# is resolved against the live enum at call time (``_surface_column`` — same
# live-enum-is-truth posture as ``enums._resolve_enum``); the fake injects the enum
# via ``system._enum_types["SurfaceColumn"]``.
CB_PARAMS = (
    ("decenter_x", "Par1", "Decenter X", "double"),
    ("decenter_y", "Par2", "Decenter Y", "double"),
    ("tilt_x", "Par3", "Tilt About X", "double"),
    ("tilt_y", "Par4", "Tilt About Y", "double"),
    ("tilt_z", "Par5", "Tilt About Z", "double"),
    ("order", "Par6", "Order", "int"),
)

# Fast lookups derived from CB_PARAMS (single source of truth).
_PARAM_TO_COL = {name: col for name, col, _h, _k in CB_PARAMS}
_PARAM_TO_HEADER = {name: header for name, _col, header, _k in CB_PARAMS}
_PARAM_TO_EXPECTED_KIND = {name: kind for name, _col, _h, kind in CB_PARAMS}

# The five Double param tokens + the single Integer (Order) token — the tilt/decenter
# DOFs a variable solve may target (Order is REFUSED, CB §3).
_DOUBLE_PARAMS = tuple(name for name, _c, _h, k in CB_PARAMS if k == "double")
_PARAM_NAMES = tuple(name for name, _c, _h, _k in CB_PARAMS)

# The proof-oracle gate (Q6): the achieved angle-recovery error was 3.55e-15 deg, so
# a gate at 1e-9 deg is nine orders of magnitude above the observed error.
GLOBAL_FRAME_TOL_DEG = 1e-9


class CBCellError(SurfaceWriteError):
    """A CB Par-cell layout/DataType mismatch — a ``SurfaceWriteError`` subclass.

    Inherits ``error_family == "surface_write"`` so it already converts to the
    structured envelope at the dispatch boundary. Raised when a cell's live Header
    does not match the expected Header, or its live ``cell.DataType`` disagrees with
    the CB_PARAMS expectation (the layout drifted — refuse rather than write the
    wrong cell).
    """


# --------------------------------------------------------------------------- #
# Enum resolution (live-enum-is-truth, same seam as _optimize_common).
# --------------------------------------------------------------------------- #
def _surface_column_enum(system):
    """Resolve the live ``SurfaceColumn`` enum TYPE (the Par-cell addressing enum).

    A fake system injects ``_enum_types["SurfaceColumn"]``; otherwise the live
    ``ZOSAPI.Editors.LDE.SurfaceColumn`` namespace. A resolution failure surfaces as
    a ``ToolParamError`` (a param-class problem), never an internal crash.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "SurfaceColumn" in injected:
        return injected["SurfaceColumn"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.LDE as _lde  # type: ignore

        return _lde.SurfaceColumn
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve SurfaceColumn from ZOSAPI.Editors.LDE: {exc}"
        )


def _surface_type_coordinate_break(system):
    """Resolve the live ``SurfaceType.CoordinateBreak`` member (P1 authoring enum).

    Injected via ``_enum_types["SurfaceType"]`` (a FakeEnum with a ``CoordinateBreak``
    member) for unit tests; otherwise the live ``ZOSAPI.Editors.LDE.SurfaceType``
    namespace. A resolution failure surfaces as a ``ToolParamError``.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "SurfaceType" in injected:
        from ..enums import _resolve_enum
        return _resolve_enum(injected["SurfaceType"], "CoordinateBreak")
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.LDE as _lde  # type: ignore

        return _lde.SurfaceType.CoordinateBreak
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            "could not resolve SurfaceType.CoordinateBreak from "
            f"ZOSAPI.Editors.LDE: {exc}"
        )


def _surface_pickup_solve_enum(system):
    """Resolve the live ``SolveType.SurfacePickup`` member (the return-CB idiom, P3).

    Injected via ``_enum_types["SolveType"]`` (a FakeEnum with a ``SurfacePickup``
    member) for unit tests; otherwise the live ``ZOSAPI.Editors.SolveType``
    namespace. A resolution failure surfaces as a ``ToolParamError``.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "SolveType" in injected:
        from ..enums import _resolve_enum
        return _resolve_enum(injected["SolveType"], "SurfacePickup")
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors as _ed  # type: ignore

        return _ed.SolveType.SurfacePickup
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve SolveType.SurfacePickup from ZOSAPI.Editors: {exc}"
        )


# --------------------------------------------------------------------------- #
# Cell access (DataType-keyed, RAISE-on-mismatch — mirrors _merit_cells).
# --------------------------------------------------------------------------- #
def _cb_cell(system, row, param):
    """Fetch ``row.GetSurfaceCell(SurfaceColumn.ParN)`` for ``param``, THROW-guarded.

    Resolves the ``ParN`` SurfaceColumn member off the live enum, fetches the cell,
    and returns it. A raw .NET THROW on the fetch (or an unknown ``param``) resolves
    to a structured ``CBCellError`` ("refuse rather than guess"), never an opaque
    dispatch ``internal``.
    """
    if param not in _PARAM_TO_COL:
        raise CBCellError(
            f"unknown coordinate-break parameter {param!r}; valid: "
            f"{list(_PARAM_NAMES)}",
            field="cb_param", intended=param, actual=None, surface=None,
        )
    col_name = _PARAM_TO_COL[param]
    enum_type = _surface_column_enum(system)
    try:
        from ..enums import _resolve_enum
        col_member = _resolve_enum(enum_type, col_name)
        return row.GetSurfaceCell(col_member)
    except ToolParamError:
        raise
    except Exception as exc:  # noqa: BLE001 — a fetch THROW -> surface_write, never internal
        raise CBCellError(
            f"could not fetch the {param!r} ({col_name}) cell of a coordinate-break "
            f"surface ({exc!r}); the cell layout is unreadable — refusing rather than "
            "guessing",
            field="cb_cell", intended=param, actual=None, surface=None,
        ) from exc


def _data_type(cell):
    """The live ``cell.DataType`` as a ``str`` (``"Integer"`` / ``"Double"``), guarded."""
    try:
        return str(cell.DataType)
    except Exception as exc:  # noqa: BLE001 — a DataType read THROW -> surface_write
        raise CBCellError(
            f"could not read a coordinate-break cell DataType ({exc!r}); the cell "
            "kind is unclassifiable — refusing rather than guessing",
            field="cb_cell_datatype", intended=None, actual=None, surface=None,
        ) from exc


def cell_kind(cell) -> str:
    """Classify a LIVE CB cell -> ``"int"`` / ``"double"`` from ``cell.DataType``.

    The PRIMARY discriminator is the live ``cell.DataType`` (Integer -> ``"int"``,
    else ``"double"``) — the SAME Int/Double rule the merit + tolerance layers key
    on, proven live for CB Par cells (Q5). Unlike the MFE there is no blank-cell case
    here (all six CB Par cells are real), so the kind is decided purely by DataType.
    A DataType read THROW -> ``CBCellError`` (never an opaque dispatch internal).
    """
    return "int" if _data_type(cell) == "Integer" else "double"


def is_integer_cb_cell(system, row, param) -> bool:
    """True iff the LIVE ``param`` cell is an Integer cell (the Order-cell guard, §3).

    The shared acceptance predicate ``set_cb_variable`` keys on (L30: the load-bearing
    DataType check, distinct from the friendly ``param == "order"`` name check). Reads
    the LIVE cell's ``DataType`` — NOT the CB_PARAMS expectation — so the Q5 silent
    trap (the engine lets a variable solve onto the Integer Order cell, and the
    read-back cleanly reports ``Variable``) is caught at the DataType, the one place
    the engine offers no guard.
    """
    cell = _cb_cell(system, row, param)
    return cell_kind(cell) == "int"


def _read_header(cell):
    """Read ``cell.Header`` -> ``str``, THROW-guarded -> ``CBCellError``."""
    try:
        return str(cell.Header)
    except Exception as exc:  # noqa: BLE001 — a Header read THROW -> surface_write
        raise CBCellError(
            f"could not read a coordinate-break cell Header ({exc!r}); the cell "
            "layout is unreadable — refusing rather than guessing",
            field="cb_cell_header", intended=None, actual=None, surface=None,
        ) from exc


def _expect_layout(cell, param):
    """Verify the live cell's Header + DataType match the CB_PARAMS expectation.

    A live Header that does not match the expected Header -> ``CBCellError`` (the
    layout shifted — refuse, do not read/write the wrong cell). A live DataType that
    disagrees with the CB_PARAMS kind -> ``CBCellError`` (the table drifted from the
    engine; refuse rather than read/write the wrong accessor — the ``_tol_cells``
    drift guard). Returns the verified live kind (``"int"`` / ``"double"``).
    """
    expected_header = _PARAM_TO_HEADER[param]
    live_header = _read_header(cell)
    if live_header != expected_header:
        raise CBCellError(
            f"coordinate-break cell layout mismatch for {param!r}: expected Header "
            f"{expected_header!r} but the live cell Header is {live_header!r} — "
            "refusing rather than reading/writing the wrong cell",
            field="cb_cell_layout", intended=expected_header, actual=live_header,
            surface=None,
        )
    expected_kind = _PARAM_TO_EXPECTED_KIND[param]
    live_kind = cell_kind(cell)
    if live_kind != expected_kind:
        raise CBCellError(
            f"coordinate-break cell {live_header!r} ({param}) is a {live_kind} cell "
            f"live but the CB catalog declared a {expected_kind} cell — the table "
            "drifted from the engine; refusing rather than the wrong accessor",
            field="cb_cell_datatype", intended=expected_kind, actual=live_kind,
            surface=None,
        )
    return live_kind


def read_cb_cell(system, row, param):
    """Type-aware READ of one CB Par cell (DataType-keyed, layout-verified).

    Reads the accessor MATCHING the verified live kind: ``"int"`` -> ``int(cell.
    IntegerValue)``, ``"double"`` -> ``float(cell.DoubleValue)``. The accessor read
    is THROW-guarded (a wrong-accessor read RAISES the live ``ArgumentException`` —
    but the kind dispatch means we always pick the RIGHT accessor; the guard re-raises
    a structured ``CBCellError`` only on a genuine engine fault). NEVER reads the
    wrong accessor.
    """
    cell = _cb_cell(system, row, param)
    kind = _expect_layout(cell, param)
    try:
        if kind == "int":
            return int(cell.IntegerValue)
        return float(cell.DoubleValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise CBCellError(
            f"could not read the {kind} value of coordinate-break cell {param!r} "
            f"({exc!r}); the cell read is unverifiable — refusing rather than guessing",
            field="cb_cell_value", intended=None, actual=None, surface=None,
        ) from exc


def _coerce_for_kind(param, kind, value):
    """Coerce ``value`` to the cell ``kind``; reject bool / non-number / non-finite /
    the integral-float-into-an-int-cell trap (the L30 shared rule).

    - a ``bool`` is an int subclass and a client miswrite -> rejected FIRST;
    - an ``"int"`` cell (Order) requires an EXACT integer (a true ``int``, NOT ``7.0``
      — the L30 trap an integral float slips under a naive ``isinstance(int)`` guard);
    - a ``"double"`` cell requires a FINITE number (int or float) -> ``float`` (inf/
      -inf/nan are non-physical and rejected pre-write).
    """
    if isinstance(value, bool):
        raise ToolParamError(
            f"coordinate-break parameter {param!r} must not be a bool ({value!r}); a "
            "bool is an int subclass — a client miswrite"
        )
    if kind == "int":
        # The Order cell: an EXACT integer only. ``7.0`` is REFUSED (it coerces
        # silently under a naive isinstance(int) guard — the L30 trap _tol_cells /
        # _merit_cells both reject; we do NOT accept the integral float here because
        # Order is a discrete 0/1 flag, never a JSON-round-tripped float).
        if isinstance(value, int):
            return int(value)
        raise ToolParamError(
            f"coordinate-break parameter {param!r} is an integer cell; got "
            f"{type(value).__name__} {value!r} (need an exact integer — refusing the "
            "integral-float coerce, L30)"
        )
    if not isinstance(value, (int, float)):
        raise ToolParamError(
            f"coordinate-break parameter {param!r} is a numeric cell; got "
            f"{type(value).__name__} {value!r} (need a number)"
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"coordinate-break parameter {param!r} is a numeric cell; got the "
            f"non-finite value {value!r} (inf/-inf/nan are non-physical and are "
            "rejected pre-write)"
        )
    return coerced


# The read-back proof's absolute floor. The prior 1e-12 was loose enough to
# certify a genuine sub-1e-12 write that the engine ROUNDED TO 0.0 as "written"
# (``_readback_ok(5e-13, 0.0)`` -> True). Tightened to 1e-15 (≈ double-precision ULP
# near 1.0) so a write that collapsed to zero is caught: any authored magnitude the
# engine actually stores is far above 1e-15, while a collapse-to-zero of a >1e-15
# value now fails the proof LOUD. The ``rel_tol=1e-9`` still governs normal magnitudes
# (the proof is exact to the bit for the values a CB carries); the abs floor only
# matters in the near-zero regime this guard protects.
_READBACK_ABS_TOL = 1e-15


def _readback_ok(intended, actual):
    """Read-back equality for a CB cell value (int exact, double tight tolerance)."""
    if isinstance(intended, float):
        return (
            actual is not None
            and isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and math.isfinite(actual)
            and math.isclose(actual, intended, rel_tol=1e-9, abs_tol=_READBACK_ABS_TOL)
        )
    return actual == intended


def write_cb_cell(system, row, param, value):
    """Type-aware WRITE of one CB Par cell, read-back-proven (the §1 firewall).

    Steps (mirroring ``_merit_cells.write_verified_cell`` / ``_tol_cells``):

    1. fetch the cell + verify Header + DataType match the CB_PARAMS expectation
       (``_expect_layout`` — a drifted layout RAISES, never the wrong cell);
    2. coerce ``value`` per the verified kind (bool / non-number / non-finite / the
       integral-float-into-Order trap all RAISE pre-write);
    3. write the accessor MATCHING the kind (Double -> ``DoubleValue``, Integer ->
       ``IntegerValue``); a write THROW -> ``CBCellError``;
    4. read it back type-aware and verify; a silent no-op (the typed-setter trap, or
       a rejected write) -> ``CBCellError``. NEVER trusts the write returned without
       the read-back.

    Returns the read-back value.
    """
    cell = _cb_cell(system, row, param)
    kind = _expect_layout(cell, param)
    coerced = _coerce_for_kind(param, kind, value)
    try:
        if kind == "int":
            cell.IntegerValue = coerced
        else:
            cell.DoubleValue = coerced
    except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_write, never internal
        raise CBCellError(
            f"could not write the {kind} value {coerced!r} to coordinate-break cell "
            f"{param!r} ({exc!r}); the engine rejected the write — refusing rather "
            "than shipping an unverified cell",
            field="cb_cell_write", intended=coerced, actual=None, surface=None,
        ) from exc

    actual = read_cb_cell(system, row, param)
    if not _readback_ok(coerced, actual):
        raise CBCellError(
            f"coordinate-break cell {param!r} write did not read back: wrote "
            f"{coerced!r}, read {actual!r} — the engine silently rejected the write "
            "(a no-op / the typed-setter trap), refusing rather than shipping an "
            "unverified cell",
            field="cb_cell_value", intended=coerced, actual=actual, surface=None,
        )
    return actual


# --------------------------------------------------------------------------- #
# The proof-oracle: GetGlobalMatrix COMPOSE-and-compare (the fix).
#
# Earlier testing falsified the original atan2 DECOMPOSITION oracle: it
# recovered each tilt via a single-axis atan2 formula, which is correct ONLY for a
# single-axis rotation and ONLY within the atan2 principal branch (-180, 180]. A
# COMBINED multi-axis tilt (tilt_x=10 AND tilt_y=20) composes a rotation block whose
# single-axis atan2 extraction != the authored angles (10.628 != 10.0) -> a FALSE
# refusal; and a single-axis tilt > 180 wraps the principal branch (200 -> -160) ->
# also a false refusal. The live re-probe (combined-tilt composition, oracle fix)
# derived the COMPOSE-and-compare oracle:
#
#   order==0 -> R = Rx(tx) . Ry(ty) . Rz(tz)
#   order==1 -> R = Rz(tz) . Ry(ty) . Rx(tx)   (the exact reverse-order product)
#
# and ELEMENT-WISE compares the expected product to the measured GetGlobalMatrix
# rotation block (max-abs residual <= 1e-9; the worst observed residual was 1.67e-16,
# machine epsilon). This is a strict SUPERSET of the single-axis case (a pure tilt_x
# composes as Rx(tx).Ry(0).Rz(0)) AND is immune to atan2 wraparound (no decomposition).
# --------------------------------------------------------------------------- #
def _rot_x(deg):
    """Right-handed rotation about X by ``deg`` degrees (row-major 3x3)."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]]


def _rot_y(deg):
    """Right-handed rotation about Y by ``deg`` degrees (row-major 3x3)."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]]


def _rot_z(deg):
    """Right-handed rotation about Z by ``deg`` degrees (row-major 3x3)."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def _matmul(a, b):
    """3x3 row-major matrix product ``a . b``."""
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def expected_cb_rotation(tilt_x, tilt_y, tilt_z, order):
    """The expected 3x3 CB rotation block (row-major, FLAT 9-list) per Order (the fix).

    ``order == 0`` -> ``Rx(tilt_x) . Ry(tilt_y) . Rz(tilt_z)``; ``order == 1`` ->
    ``Rz(tilt_z) . Ry(tilt_y) . Rx(tilt_x)`` (the reverse-order product — Order flips
    the apply sequence, so the matrix factors reverse). Live-falsified to machine
    epsilon. The two-way branch is TOTAL: ``order`` is validated to
    {0, 1} upstream (``_require_order``), so no third branch is reachable.

    Returns the flat row-major list ``[R11 R12 R13 R21 R22 R23 R31 R32 R33]``.
    """
    # The exported helper must NOT silently pick a branch on an out-of-range
    # order. Every PRODUCTION caller validates {0,1} upstream (``_require_order``), but
    # this is exported (``__all__``) and carries no contract of its own — an ``else``
    # that swallows order==2 (or -1, or 7) would compose the Order-1 product for a
    # value the caller never meant. Refuse LOUD (reject-domain = anything not exactly
    # 0 or 1, including bool — True/False are int-equal to 1/0 but are a client
    # miswrite of the discrete flag).
    if isinstance(order, bool) or order not in (0, 1):
        raise CBCellError(
            f"coordinate-break order must be 0 or 1, got {order!r}; the exported "
            "rotation oracle refuses an out-of-range order rather than silently "
            "composing the wrong branch",
            field="cb_order", intended="0 or 1", actual=order, surface=None,
        )
    if order == 0:
        mat = _matmul(_matmul(_rot_x(tilt_x), _rot_y(tilt_y)), _rot_z(tilt_z))
    else:  # order == 1 (validated to exactly {0,1} above)
        mat = _matmul(_matmul(_rot_z(tilt_z), _rot_y(tilt_y)), _rot_x(tilt_x))
    return [mat[0][0], mat[0][1], mat[0][2],
            mat[1][0], mat[1][1], mat[1][2],
            mat[2][0], mat[2][1], mat[2][2]]


def read_global_matrix(system, lde, surf):
    """Read ``GetGlobalMatrix(surf)`` -> ``(R_flat[9] row-major, X, Y, Z)`` (raw).

    The 13-tuple is ``(success, R11..R33 ROW-MAJOR, X, Y, Z)``. Returns the rotation
    block as a flat 9-list ``[R11..R33]`` plus the three translation slots. A read
    THROW / a malformed (non-13) tuple / a non-float entry resolves to a structured
    ``CBCellError`` ("refuse rather than guess the frame") — never an opaque dispatch
    ``internal``.
    """
    try:
        ret = lde.GetGlobalMatrix(surf)
        seq = list(ret)
    except Exception as exc:  # noqa: BLE001 — a matrix read THROW -> surface_write
        raise CBCellError(
            f"could not read GetGlobalMatrix({surf}) ({exc!r}); the coordinate change "
            "is unverifiable — refusing rather than guessing the global frame",
            field="global_matrix", intended=None, actual=None, surface=surf,
        ) from exc
    if len(seq) != 13:
        raise CBCellError(
            f"GetGlobalMatrix({surf}) returned a {len(seq)}-tuple (expected the "
            "13-tuple success+R11..R33+X+Y+Z) — refusing rather than guessing the "
            "global frame",
            field="global_matrix", intended=13, actual=len(seq), surface=surf,
        )
    # The DECISIVE success flag (seq[0]). GetGlobalMatrix can return
    # success=False with stale/identity numeric slots — reading those numbers would
    # CERTIFY a frame the engine reported it could not compute (a FALSE PROOF through
    # the belt-and-suspenders gate). The success flag is the FIRST thing checked: a
    # missing/non-bool/falsey seq[0] all route to refusal (reject-domain complete — a
    # truthiness test on bool(seq[0]) covers False, 0, 0.0, "", None alike, and a
    # genuine success is the .NET True / numeric 1). Refuse rather than read the
    # stale numbers.
    try:
        success = bool(seq[0])
    except Exception as exc:  # noqa: BLE001 — an unconvertible flag -> refuse, not crash
        raise CBCellError(
            f"GetGlobalMatrix({surf}) returned a success flag that could not be "
            f"evaluated ({seq[0]!r}: {exc!r}); the global frame is unverifiable — "
            "refusing rather than guessing the frame",
            field="global_matrix", intended=True, actual=None, surface=surf,
        ) from exc
    if not success:
        raise CBCellError(
            f"GetGlobalMatrix({surf}) reported FAILURE (success flag {seq[0]!r}); the "
            "engine could not compute the global frame — refusing rather than reading "
            "the stale/identity numeric slots as if they were a real frame",
            field="global_matrix", intended=True, actual=bool(seq[0]), surface=surf,
        )
    try:
        r = [float(v) for v in seq[1:10]]
        x, y, z = (float(seq[10]), float(seq[11]), float(seq[12]))
    except Exception as exc:  # noqa: BLE001 — a marshalling THROW -> surface_write
        raise CBCellError(
            f"could not marshal the global frame from GetGlobalMatrix({surf}) "
            f"({exc!r}); the rotation block is unreadable — refusing rather than "
            "guessing the frame",
            field="global_matrix", intended=None, actual=None, surface=surf,
        ) from exc
    return (r, x, y, z)


def rotation_residual(measured_R, tilt_x, tilt_y, tilt_z, order):
    """Max-abs ELEMENT-WISE residual between ``measured_R`` and the expected product.

    ``measured_R`` is the flat row-major 9-list from ``read_global_matrix``. The
    expected block is composed per ``order`` (``expected_cb_rotation``). Returns the
    scalar ``max(abs(measured - expected))`` over the 9 elements — the compose-compare
    oracle's distance. A non-finite measured element yields ``inf`` (a degenerate frame
    is never a pass).
    """
    expected = expected_cb_rotation(tilt_x, tilt_y, tilt_z, order)
    # A length guard BEFORE the zip. ``zip`` stops at the shorter sequence, so
    # an empty/short ``measured_R`` would never enter the loop, leave ``worst`` at
    # 0.0, and CERTIFY a malformed/partial measured block as a perfect match (the
    # exported oracle has no shape contract of its own). A measured block that is not
    # exactly the 9-element flat rotation block is NOT comparable -> ``inf`` (never a
    # pass). Reject-domain = any len != 9 (empty, short, OR over-long), so neither a
    # truncated nor a padded block can slip.
    measured_list = list(measured_R)
    if len(measured_list) != 9:
        return float("inf")
    worst = 0.0
    for m, e in zip(measured_list, expected):
        if not isinstance(m, (int, float)) or isinstance(m, bool) or not math.isfinite(m):
            return float("inf")
        worst = max(worst, abs(m - e))
    return worst


def _transpose9(flat):
    """Transpose a flat row-major 9-list (for a rotation matrix, transpose == inverse).

    ``flat = [R11 R12 R13 R21 R22 R23 R31 R32 R33]`` ->
    ``[R11 R21 R31 R12 R22 R32 R13 R23 R33]``. A rotation matrix is orthonormal, so its
    inverse is its transpose — this is how the relative-rotation oracle removes the
    upstream frame WITHOUT a numerical matrix inversion.
    """
    return [flat[0], flat[3], flat[6],
            flat[1], flat[4], flat[7],
            flat[2], flat[5], flat[8]]


def _matmul_flat(a, b):
    """3x3 row-major flat-9 matrix product ``a . b`` -> flat-9 list."""
    return [sum(a[3 * i + k] * b[3 * k + j] for k in range(3)) for i in range(3)
            for j in range(3)]


def _is_orthonormal_rotation(flat, tol=1e-6):
    """True iff ``flat`` (row-major 9-list) is a proper orthonormal rotation block.

    BUG-1 firewall: the relative-rotation oracle removes the UPSTREAM frame by
    multiplying by its transpose (== inverse ONLY for an orthonormal matrix). If the
    upstream block read back DEGRADED (a non-orthonormal / non-rotation matrix — a
    skew/scale that GetGlobalMatrix would never produce for a real frame, but which a
    corrupt read could), ``R_upstream^T`` is NOT the inverse and the relative residual
    would be silently wrong. So the upstream block is VALIDATED orthonormal here; a
    failure routes to a refusal (fail CLOSED), never a fabricated relative pass.
    """
    if len(flat) != 9:
        return False
    for v in flat:
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return False
    # R^T . R must be the identity within tol (orthonormal columns/rows).
    rt = _transpose9(flat)
    prod = _matmul_flat(rt, flat)
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    for p, ie in zip(prod, identity):
        if abs(p - ie) > tol:
            return False
    return True


def relative_rotation_residual(upstream_R, measured_R, tilt_x, tilt_y, tilt_z, order):
    """Max-abs residual of THIS CB's CONTRIBUTION vs the authored tilt product (BUG-1).

    The absolute ``rotation_residual`` assumes the upstream frame is IDENTITY — true
    only for a FIRST CB. For a compound/second CB the measured post-CB block is
    ``R_upstream . R_thisCB`` (the prior fold's rotation pre-multiplies). To verify
    THIS CB's contribution we remove the upstream frame: ``R_thisCB =
    R_upstream^T . R_measured`` (transpose == inverse for an orthonormal rotation),
    then compare element-wise to ``expected_cb_rotation(thisCB)``.

    Fails CLOSED: a non-9 / non-finite ``measured_R`` OR a non-orthonormal
    ``upstream_R`` (the transpose would not be the inverse) yields ``inf`` (never a
    pass). For an IDENTITY upstream this reduces EXACTLY to ``rotation_residual`` (the
    single-CB backward-compat invariant).
    """
    measured_list = list(measured_R)
    upstream_list = list(upstream_R)
    if len(measured_list) != 9:
        return float("inf")
    for m in measured_list:
        if not isinstance(m, (int, float)) or isinstance(m, bool) or not math.isfinite(m):
            return float("inf")
    # The upstream block must be a proper rotation, or its transpose is not the inverse
    # and the relative product is meaningless — refuse (fail closed).
    if not _is_orthonormal_rotation(upstream_list):
        return float("inf")
    relative = _matmul_flat(_transpose9(upstream_list), measured_list)
    return rotation_residual(relative, tilt_x, tilt_y, tilt_z, order)


def verify_global_rotation(measured_R, tilt_x, tilt_y, tilt_z, order,
                           tol=GLOBAL_FRAME_TOL_DEG):
    """True iff the measured rotation block matches the authored tilt+order product.

    Composes the expected block per ``order`` and ELEMENT-WISE compares to
    ``measured_R`` (the flat row-major 9-list). Passes iff ``max(abs(measured -
    expected)) <= tol``. Decenter does NOT affect the rotation block (proven), so the
    decenter is verified separately (the cell read-back + the translation slots), never
    here.
    """
    return rotation_residual(measured_R, tilt_x, tilt_y, tilt_z, order) <= tol


def global_rotation_angles(system, lde, surf):
    """Read ``GetGlobalMatrix(surf)`` -> ``(tiltX, tiltY, tiltZ, X, Y, Z)`` (Q6).

    A SINGLE-AXIS angle READOUT for the envelope's ``global_frame`` display + the
    single-axis unit/live tests. Recovers each tilt from the row-major rotation block
    via the principal-branch atan2 formulas (machine-exact 3.55e-15 deg for a
    single-axis rotation):

    - ``tiltX = atan2(R32, R33)`` = ``atan2(R[7], R[8])``
    - ``tiltY = atan2(-R31, R11)`` = ``atan2(-R[6], R[0])``
    - ``tiltZ = atan2(R21, R11)`` = ``atan2(R[3], R[0])``

    NOTE: this is NOT the proof gate (it is decomposition, which is exact ONLY for a
    single-axis rotation and wraps past +/-180 deg). The DECISIVE gate is
    ``verify_global_rotation`` (compose-and-compare). This readout is informational
    display only — never the pass/fail oracle.
    """
    r, x, y, z = read_global_matrix(system, lde, surf)
    try:
        # Row-major: r = [R11 R12 R13 R21 R22 R23 R31 R32 R33].
        tilt_x = math.degrees(math.atan2(r[7], r[8]))   # atan2(R32, R33)
        tilt_y = math.degrees(math.atan2(-r[6], r[0]))  # atan2(-R31, R11)
        tilt_z = math.degrees(math.atan2(r[3], r[0]))   # atan2(R21, R11)
    except Exception as exc:  # noqa: BLE001 — a marshalling THROW -> surface_write
        raise CBCellError(
            f"could not extract the global-frame angles from GetGlobalMatrix({surf}) "
            f"({exc!r}); the rotation block is unreadable — refusing rather than "
            "guessing the frame",
            field="global_matrix", intended=None, actual=None, surface=surf,
        ) from exc
    return (tilt_x, tilt_y, tilt_z, x, y, z)


# --------------------------------------------------------------------------- #
# The CB-surface predicate (the LensSpec guard + the CB tools both consult it).
# --------------------------------------------------------------------------- #
def is_coordinate_break(row) -> bool:
    """True iff ``row`` is a coordinate-break surface (CB §1).

    Keys on the CB substring of ``str(row.Type)`` (the SAME idiom ``lens_describe``
    reads ``row.Type`` with — ``read_surface`` does not expose the type). A
    ``row.Type`` read THROW -> ``CBCellError`` ("refuse rather than guess the type")
    so a guard that consults this never silently treats an unreadable row as
    non-CB.
    """
    try:
        type_name = str(row.Type)
    except Exception as exc:  # noqa: BLE001 — a Type read THROW -> surface_write
        raise CBCellError(
            f"could not read a surface Type ({exc!r}); cannot classify it as a "
            "coordinate-break — refusing rather than guessing",
            field="surface_type", intended=None, actual=None, surface=None,
        ) from exc
    return _CB_TYPE_SUBSTRING in type_name


__all__ = [
    "CB_PARAMS",
    "GLOBAL_FRAME_TOL_DEG",
    "CBCellError",
    "cell_kind",
    "is_integer_cb_cell",
    "read_cb_cell",
    "write_cb_cell",
    "global_rotation_angles",
    "read_global_matrix",
    "expected_cb_rotation",
    "rotation_residual",
    "relative_rotation_residual",
    "verify_global_rotation",
    "is_coordinate_break",
    "_surface_column_enum",
    "_surface_type_coordinate_break",
    "_surface_pickup_solve_enum",
    "_cb_cell",
    "_PARAM_NAMES",
    "_DOUBLE_PARAMS",
]
