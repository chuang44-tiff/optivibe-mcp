"""tools/_tol_cells.py — the TDE per-cell read-back-verified writer.

NOT dispatchable. The single place a tolerance operand's parameter cells are written
type-aware, keyed on the LIVE ``cell.DataType`` (Integer -> ``IntegerValue``, Double
-> ``DoubleValue``), with a Header match and READ-BACK-AS-PROOF (a write that does
not read back -> ``ToleranceError(family="tolerancing_run")`` — the silent-no-op trap
G-CELL).

This MIRRORS the merit ``_merit_cells.write_verified_cell`` discriminator discipline
(the ``cell.DataType`` Int/Double rule + the read-back firewall + the integral-float
``7.0``-into-an-int-cell rejection) but is a SEPARATE re-implementation: the
Tolerance Data Editor and the Merit Function Editor are DIFFERENT editors with
different cell-error semantics, so we do NOT cross-import the MFE module (D6).

The probe-grounded rules this module encodes (from
``m2_falsifier.authored[*].cells`` —
``label_DataType`` is ``"Integer"`` / ``"Double"`` and the WRONG accessor RAISES the
live ``ArgumentException``):

- the discriminator is the live ``cell.DataType`` string (``"Integer"`` -> int cell,
  else double cell); reading/writing the wrong accessor RAISES (the live
  ``ArgumentException``);
- an integral float (``7.0``) is REJECTED into an int cell (the L30 trap: ``7.0``
  slips a naive ``isinstance(int)`` guard yet coerces silently — the shared
  ``is_integral_int`` predicate the validator and writer BOTH key on so they cannot
  diverge);
- a non-finite double (``inf`` / ``nan``) is REJECTED pre-write (a non-physical
  perturbation bound);
- every write is read back through the MATCHING accessor and compared; a silent
  no-op / rejected write -> ``ToleranceError(family="tolerancing_run")``.
"""
import math

from ._tolerance_common import ToleranceError


_RUN = "tolerancing_run"


def _data_type(cell):
    """The live ``cell.DataType`` as a ``str`` (``"Integer"`` / ``"Double"``), THROW-guarded."""
    try:
        return str(cell.DataType)
    except Exception as exc:  # noqa: BLE001 — a DataType read THROW -> tolerancing_run
        raise ToleranceError(
            f"could not read a TDE cell DataType ({exc!r}); the cell kind is "
            "unclassifiable — refusing rather than guessing",
            family=_RUN,
        ) from exc


def cell_kind(cell) -> str:
    """Classify a LIVE TDE cell -> ``"int"`` / ``"double"`` from ``cell.DataType``.

    The PRIMARY discriminator is the live ``cell.DataType`` (Integer -> ``"int"``,
    else ``"double"``) — the same Int/Double rule the merit layer keys on, proven
    against the captured ``label_DataType`` signatures. A DataType read THROW ->
    ``ToleranceError(tolerancing_run)`` (never an opaque dispatch internal).
    """
    return "int" if _data_type(cell) == "Integer" else "double"


def is_integral_int(value) -> bool:
    """True iff ``value`` is an EXACT integer writable into an int cell (L30 shared).

    The shared predicate the validator and the writer BOTH key on so they cannot
    diverge (the ``7.0``-into-an-int-cell trap: an integral float coerces silently
    under a naive ``isinstance(int)`` guard — this rejects it). A ``bool`` is an int
    subclass and is a client miswrite -> rejected FIRST. Only a true ``int`` (not a
    float, not ``7.0``) passes.
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, int)


def _coerce_int(header, value):
    """Coerce ``value`` for an INTEGER cell; reject the integral-float / bool trap."""
    if not is_integral_int(value):
        raise ToleranceError(
            f"TDE cell {header!r} is an integer cell; got "
            f"{type(value).__name__} {value!r} — refusing the integral-float / bool "
            "coerce (a 7.0-into-an-int-cell silent miswrite, L30)",
            family=_RUN,
        )
    return int(value)


def _coerce_double(header, value):
    """Coerce ``value`` for a DOUBLE cell; reject bool / non-number / non-finite."""
    if isinstance(value, bool):
        raise ToleranceError(
            f"TDE cell {header!r} is a numeric cell; got a bool {value!r} "
            "(a bool is an int subclass — a client miswrite)",
            family=_RUN,
        )
    if not isinstance(value, (int, float)):
        raise ToleranceError(
            f"TDE cell {header!r} is a numeric cell; got "
            f"{type(value).__name__} {value!r} (need a number)",
            family=_RUN,
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToleranceError(
            f"TDE cell {header!r} is a numeric cell; got the non-finite value "
            f"{value!r} (inf/-inf/nan are non-physical perturbation bounds and are "
            "rejected pre-write)",
            family=_RUN,
        )
    return coerced


def _read_header(cell):
    """Read ``cell.Header`` -> ``str``, THROW-guarded -> ``tolerancing_run``."""
    try:
        return str(cell.Header)
    except Exception as exc:  # noqa: BLE001 — a Header read THROW -> tolerancing_run
        raise ToleranceError(
            f"could not read a TDE cell Header ({exc!r}); the cell layout is "
            "unreadable — refusing rather than guessing",
            family=_RUN,
        ) from exc


def _read_value(cell, kind, header):
    """Type-aware READ of a cell through the accessor MATCHING ``kind``, THROW-guarded."""
    try:
        if kind == "int":
            return int(cell.IntegerValue)
        return float(cell.DoubleValue)
    except Exception as exc:  # noqa: BLE001 — a wrong/raw accessor THROW -> tolerancing_run
        raise ToleranceError(
            f"could not read the {kind} value of TDE cell {header!r} ({exc!r}); the "
            "cell read is unverifiable — refusing rather than guessing",
            family=_RUN,
        ) from exc


def _verify(header, intended, actual):
    """Read-back oracle: a silent no-op / rejected write -> ``tolerancing_run`` (G-CELL).

    A double-cell compares with a tight tolerance (float round-trip); an int-cell
    compares exactly. A ``nan`` never equals itself, so a non-finite slipping through
    would fail here too (defense in depth — but the coercers already reject non-finite).
    """
    if isinstance(intended, float):
        ok = (
            actual is not None
            and math.isfinite(actual)
            and math.isclose(actual, intended, rel_tol=1e-9, abs_tol=1e-12)
        )
    else:
        ok = actual == intended
    if not ok:
        raise ToleranceError(
            f"TDE cell {header!r} write did not read back: wrote {intended!r}, read "
            f"{actual!r} — the engine silently rejected the write (a no-op / "
            "wrong-cell miswrite), refusing rather than shipping an unverified cell",
            family=_RUN,
        )


def _write_kind(cell, kind, header, value):
    """Write the accessor matching ``kind`` (already-coerced ``value``), THROW-guarded."""
    try:
        if kind == "int":
            cell.IntegerValue = value
        else:
            cell.DoubleValue = value
    except Exception as exc:  # noqa: BLE001 — a write THROW -> tolerancing_run
        raise ToleranceError(
            f"could not write the {kind} value {value!r} to TDE cell {header!r} "
            f"({exc!r}); the engine rejected the write — refusing rather than "
            "shipping an unverified cell",
            family=_RUN,
        ) from exc


def write_int_cell(op, cell_spec, value):
    """Write ``value`` into the operand's int cell at ``cell_spec`` (Header-matched, read-back).

    Steps (mirroring the merit firewall over a TDE row's ``op.GetCellAt(col)``):

    1. read the live cell at ``cell_spec.col``; its Header MUST equal
       ``cell_spec.header`` (the layout shifted -> refuse, do not write the wrong cell);
    2. the live ``cell.DataType`` MUST be ``Integer`` (the catalog said this is an int
       cell — if the live engine disagrees, the table drifted; refuse);
    3. coerce ``value`` (reject the integral-float / bool trap, L30);
    4. write ``IntegerValue``, read it back through the int accessor, verify (a silent
       no-op -> ``tolerancing_run`` — G-CELL).

    Returns the read-back int.
    """
    cell = _get_cell(op, cell_spec.col)
    live_header = _read_header(cell)
    if live_header != str(cell_spec.header):
        raise ToleranceError(
            f"TDE cell layout mismatch: intended to write {cell_spec.header!r} at "
            f"col {cell_spec.col} but the live cell Header is {live_header!r} — "
            "refusing rather than writing the wrong cell",
            family=_RUN,
        )
    kind = cell_kind(cell)
    if kind != "int":
        raise ToleranceError(
            f"TDE cell {live_header!r} (col {cell_spec.col}) is a {kind} cell live but "
            "the catalog declared an integer cell — the table drifted from the "
            "engine; refusing rather than writing the wrong accessor",
            family=_RUN,
        )
    coerced = _coerce_int(live_header, value)
    _write_kind(cell, "int", live_header, coerced)
    actual = _read_value(cell, "int", live_header)
    _verify(live_header, coerced, actual)
    return actual


def write_double_verified(op, col, header, value):
    """Write ``value`` into the operand's DOUBLE cell at ``col`` (Header-matched, read-back).

    The Min/Max (and any other Double VALUE) channel: Header-match + DataType-must-be-
    Double + reject bool/non-number/non-finite + write ``DoubleValue`` + read-back
    verify (a silent no-op -> ``tolerancing_run``). Returns the read-back float.
    """
    cell = _get_cell(op, col)
    live_header = _read_header(cell)
    if live_header != str(header):
        raise ToleranceError(
            f"TDE cell layout mismatch: intended to write {header!r} at col {col} but "
            f"the live cell Header is {live_header!r} — refusing rather than writing "
            "the wrong cell",
            family=_RUN,
        )
    kind = cell_kind(cell)
    if kind != "double":
        raise ToleranceError(
            f"TDE cell {live_header!r} (col {col}) is a {kind} cell live but a numeric "
            "(double) write was intended — the table drifted from the engine; refusing "
            "rather than writing the wrong accessor",
            family=_RUN,
        )
    coerced = _coerce_double(live_header, value)
    _write_kind(cell, "double", live_header, coerced)
    actual = _read_value(cell, "double", live_header)
    _verify(live_header, coerced, actual)
    return actual


def _get_cell(op, col):
    """``op.GetCellAt(col)`` THROW-guarded -> ``tolerancing_run`` (never an internal)."""
    try:
        return op.GetCellAt(col)
    except Exception as exc:  # noqa: BLE001 — a GetCellAt THROW -> tolerancing_run
        raise ToleranceError(
            f"could not read TDE cell at col {col} ({exc!r}); the cell layout is "
            "unreadable — refusing rather than guessing",
            family=_RUN,
        ) from exc


__all__ = [
    "cell_kind",
    "is_integral_int",
    "write_int_cell",
    "write_double_verified",
]
