"""tools/_merit_cells.py — the type-aware MFE cell-access primitive (§2).

NOT dispatchable (no ``TOOL_SPECS``). The single place the merit-builder reads and
writes a merit operand's PARAMETER cells type-aware, keyed off the live
``cell``. ``add_operand``, ``serialize_merit`` and
``apply_merit_recipe`` all funnel their cell access through here.

The probe-grounded rules this module encodes:

- **The column->meaning map is operand-specific and ALREADY in the live layout**
  (EFFL's ``Wave`` is col 3, REAY's ``Surf`` is col 2; EFFL col 2 is an
  inert blank). We READ the column from the live cell ``Header``; we NEVER store a
  per-operand column table.
- **Each parameter cell is EITHER an Integer cell OR a Double cell**, and reading
  the WRONG accessor RAISES (``ArgumentException``). The discriminator is the live
  ``cell.DataType`` (the cell exposes the string ``"Integer"`` /
  ``"Double"``) — the PRIMARY, live-grounded rule (the L24 fix: the ``build_merit``
  wizard emits an ``MNEA`` ``Mode`` cell whose ``DataType`` is ``Integer`` but whose
  Header is outside the old closed Integer set, so a Header-only rule misread it as
  Double and a ``DoubleValue`` read RAISED ``ArgumentException``). Blank detection
  stays Header-first (a blank cell reports ``DataType`` ARBITRARILY — EFFL col-2
  blank reports ``Integer``, cols 4-9 blank report ``Double`` — so ``DataType`` can
  NOT decide blank). ``Surf``/``Surf1``/``Surf2``/``Wave`` (``_INT_HEADERS``) are
  DEMOTED to a FALLBACK used ONLY when the ``DataType`` read itself THROWS (defensive
  — keep the L26 firewall: a THROW -> structured ``SurfaceWriteError``).
- **``GetCellAt(0)`` is NEVER called** (it RAISES). The param columns are
  ``range(2, 10)`` (cols 2..9); col 1 is ``Type``, cols 10/11 are Target/Weight.
- **Every raw .NET read/write is THROW-guarded** the same way
  ``_structural_common._add_bound_operand`` guards its reads (the L26 firewall
  precedent): a ``GetCellAt``/``IntegerValue``/``DoubleValue``/``Header`` THROW
  re-raises a STRUCTURED ``SurfaceWriteError``, never an opaque dispatch
  ``internal``. A write is read back type-aware through the EXISTING
  ``_lens_common._verify_or_raise`` oracle (a silent no-op -> ``SurfaceWriteError``).
- **A Header the caller asks to write that does not match the live cell's Header at
  that column -> ``CellLayoutError``** (a ``SurfaceWriteError`` subclass — the
  layout shifted, refuse rather than write the wrong cell).

Live ZOS-API integration: exercised by the merit-recipe live test; unit-tested
against the fixture-seeded ``FakeCell``/``FakeOperand`` doubles
whose accessors RAISE on the wrong type exactly like the live ``ArgumentException``.
"""
import math

from ..errors import SurfaceWriteError
from . import _lens_common as _lc

# The Integer-labeled parameter Headers seen live. DEMOTED to a FALLBACK
# (the L24 fix): the PRIMARY discriminator is now the live ``cell.DataType``; this
# Header set is consulted ONLY when the ``DataType`` read THROWS. A NON-blank Header
# in this set falls back to Integer; any other non-blank Header falls back to Double
# (the default — Hx/Hy/Px/Py). Not deleted — it is the defensive throw-fallback.
_INT_HEADERS = frozenset({"Surf", "Surf1", "Surf2", "Wave"})

# An unused parameter column carries ``Header == " "`` (a single space).
_BLANK_HEADER = " "

# MM-1: a row-reference cell's Header is ``Op#``-prefixed
# (``Op#`` / ``Op#1`` / ``Op#2`` / any future ``Op#N``). This is the discriminator
# the operand-row-reference classifier keys on (NOT a per-operand table — the
# cycle-1 "read it from the live layout" philosophy).
_OP_REF_PREFIX = "Op#"

# A CURATED token allow-list of value-less CONTROL operands (S1). These
# operands read a NON-FINITE Target/Weight on a fresh author (the inf/nan sentinel —
# probe PART A: a fresh CONF reads Target==Weight==inf), so the numeric Target/Weight
# read-back guard must be SKIPPED and the proof REDIRECTED to the operand's REAL semantic
# cell. The VALUE is the proof rule:
#   "cell:<Header>" -> prove via that semantic cell (read-back-as-proof on the cell);
#   "type"          -> prove via the TypeName read-back (the type IS the payload).
#
# A CURATED TOKEN list, NEVER an "if Target reads back inf -> skip" heuristic: the
# heuristic would silently exempt the 11 data/file operands that ALSO read a non-finite
# Target (BIPF/HACG/QOAC/FDMO/FDRE/IMSF/NPAF/NSRD/SVIG/CVIG/ENDX, probe PART B) and disarm
# the #10 silent-bad-write canary for every genuinely-numeric operand the engine failed
# to write. (Probe PART B classified 8 control/structural tokens; this cycle ships CONF
# only — the linchpin. The TypeName-only set {BLNK,DMFS,OOFF,USYM} and the Op#-ref set
# {GOTO,SKIN,SKIS} are DEFERRED, see §10; adding one is a data row + a
# helper branch.)
_VALUELESS_CONTROL_OPERANDS = {
    "CONF": "cell:Cfg#",   # the increment linchpin: the per-config number (Integer cell)
}

# The parameter columns 2..9 (cols vary BY operand type). ``GetCellAt(0)``
# RAISES and is NEVER called; col 1 is ``Type``; cols 10/11 are Target/Weight.
_PARAM_COLS = range(2, 10)


class CellLayoutError(SurfaceWriteError):
    """A cell's live ``Header`` did not match the Header the caller intended to write.

    A ``SurfaceWriteError`` subclass (§6/§7) so it already converts to a
    ``surface_write`` envelope at the dispatch boundary — no new exception-type
    plumbing. Raised when ``write_verified_cell`` is asked to write a param at a
    column whose LIVE Header is not the expected one (the operand layout shifted —
    refuse rather than silently writing the wrong cell).
    """


def _header_is_blank(header) -> bool:
    """True iff the cell ``Header`` marks an unused/blank column.

    Blank detection is ALWAYS Header-based and runs FIRST (the L24 caveat: a blank
    cell reports ``DataType`` ARBITRARILY — EFFL col-2 blank reports ``Integer``,
    cols 4-9 blank report ``Double`` — so ``DataType`` can NOT decide blank). An
    unused column carries ``Header == " "`` (a single space). ``header`` is
    normalized via ``str(...)`` (the live ``cell.Header`` is a .NET ``System.String``
    proxy).
    """
    return str(header).strip() == ""


def cell_kind(cell) -> str:
    """Classify a LIVE cell into ``"int"`` / ``"double"`` / ``"blank"`` (the L24 fix).

    The discriminator's INPUT is the LIVE ``cell`` (not a Header string): blank
    detection is Header-first, int/double comes from ``cell.DataType``.

    1. ``_header_is_blank(cell.Header)`` -> ``"blank"`` (FIRST — a blank cell's
       ``DataType`` is arbitrary, so it MUST be caught before the DataType read).
    2. PRIMARY: ``str(cell.DataType) == "Integer"`` -> ``"int"``, else -> ``"double"``
       (the live-grounded rule — the probe proved ``DataType`` returns the string
       ``"Integer"`` / ``"Double"``; this is what catches the wizard ``MNEA`` ``Mode``
       Integer cell the old Header-only set misread as Double).
    3. FALLBACK (only if the ``DataType`` read THROWS): ``cell.Header in _INT_HEADERS``
       -> ``"int"``, else -> ``"double"``. ``cell.IsBLNK`` is NOT used — it does NOT
       exist on ``IEditorCell`` on this engine build (the ``IsBLNK`` note was
       about the MFE ROW, not the cell).

    ``cell_kind`` SELF-GUARDS its own raw .NET reads (the ``cell.Header`` read
    AND the ``cell.DataType`` read) per §2 ("EVERY raw .NET read THROW-guarded ->
    structured ``SurfaceWriteError``"). As an exported primitive it does NOT rely on
    every caller pre-guarding: a ``cell.Header`` read THROW re-raises a structured
    ``SurfaceWriteError`` (never an opaque dispatch ``internal``). The ``cell.DataType``
    read THROW stays a DEFENSIVE Header fallback (the L24 posture); only a Header-read
    THROW — which leaves us with no discriminator at all — escalates to the firewall.
    """
    try:
        header = cell.Header
    except Exception as exc:  # noqa: BLE001 — a Header read THROW -> surface_write
        raise SurfaceWriteError(
            f"could not read a merit cell Header ({exc!r}); the cell kind is "
            "unclassifiable — refusing rather than guessing",
            field="cell_header",
            intended=None,
            actual=None,
            surface=None,
        ) from exc
    if _header_is_blank(header):
        return "blank"
    try:
        data_type = str(cell.DataType)
    except Exception:  # noqa: BLE001 — DataType read THROW -> Header fallback (defensive)
        return "int" if str(header) in _INT_HEADERS else "double"
    return "int" if data_type == "Integer" else "double"


def _read_header(op, col):
    """Read ``op.GetCellAt(col).Header`` THROW-guarded -> structured SurfaceWriteError.

    A raw .NET THROW on the ``GetCellAt`` / ``Header`` read re-raises a structured
    ``SurfaceWriteError`` (the L26 firewall) rather than escaping dispatch as an
    opaque ``internal``. Returns the Header as a ``str``.
    """
    try:
        return str(op.GetCellAt(col).Header)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not read the Header of merit cell col {col} ({exc!r}); the "
            "cell layout is unreadable — refusing rather than guessing",
            field="cell_header",
            intended=None,
            actual=None,
            surface=None,
        ) from exc


def read_cell_kind(op, col):
    """Read one cell's ``(header, kind)`` type-aware (Header-blank-first, then DataType).

    The kind-DERIVATION primitive shared by ``read_cell`` and ``read_param_map`` so the
    kind is derived ONCE from the LIVE cell (never re-derived from a Header string —
    that was the L24 Header-only bug). THROW-guarded the same way: a ``GetCellAt`` /
    ``DataType`` / ``Header`` THROW re-raises a structured ``SurfaceWriteError`` (the
    L26 firewall) rather than escaping dispatch as an opaque ``internal``.
    """
    header = _read_header(op, col)
    try:
        kind = cell_kind(op.GetCellAt(col))
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not classify merit cell {header!r} (col {col}) ({exc!r}); the "
            "cell kind is unreadable — refusing rather than guessing",
            field="cell_kind",
            intended=None,
            actual=None,
            surface=None,
        ) from exc
    return header, kind


def read_cell(op, col):
    """Type-aware READ of one parameter cell. Returns ``(header, value)``.

    Reads the cell's ``Header`` first (THROW-guarded), classifies the LIVE cell via
    ``cell_kind`` (Header-blank-first, then ``cell.DataType``), then reads the
    accessor MATCHING the kind:

    - ``"blank"`` -> ``(header, None)`` (an unused column; the caller skips it);
    - ``"int"``   -> ``(header, int(cell.IntegerValue))``;
    - ``"double"``-> ``(header, float(cell.DoubleValue))``.

    The accessor read is THROW-guarded the same way (a wrong-accessor read RAISES
    the live ``ArgumentException`` — but the kind dispatch means we always pick the
    RIGHT accessor; the guard re-raises a structured ``SurfaceWriteError`` only on a
    genuine engine fault). NEVER reads the wrong accessor (the bug the naive
    dual-accessor read swallowed, and the L24 ``MNEA.Mode`` Integer cell the old
    Header-only rule misread as Double).
    """
    header = _read_header(op, col)
    kind = "unknown"  # bound before the try so the error message is always safe
    try:
        cell = op.GetCellAt(col)
        kind = cell_kind(cell)
        if kind == "blank":
            return header, None
        if kind == "int":
            value = int(cell.IntegerValue)
        else:
            value = float(cell.DoubleValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not read the {kind} value of merit cell {header!r} (col {col}) "
            f"({exc!r}); the cell read is unverifiable — refusing rather than guessing",
            field="cell_value",
            intended=None,
            actual=None,
            surface=None,
        ) from exc
    return header, value


def read_param_map(op):
    """Walk cols 2..9 type-aware -> ``{Header: {"col", "kind", "value"}}`` (non-blank only).

    The live param signature of one operand row, read type-aware (the P3 round-trip
    primitive). Blank columns (``Header == " "``) are SKIPPED — only the operand's
    REAL parameters appear. ``GetCellAt(0)`` is NEVER called (the walk starts at
    col 2). Each cell is read through ``read_cell`` (so every read is type-aware +
    THROW-guarded). Returns a dict keyed by the live Header name (the recipe's
    Header-keyed contract).
    """
    param_map = {}
    for col in _PARAM_COLS:
        header, kind = read_cell_kind(op, col)
        if kind == "blank":
            continue
        _, value = read_cell(op, col)
        param_map[header] = {"col": col, "kind": kind, "value": value}
    return param_map


def is_row_ref_header(header, kind) -> bool:
    """True iff a cell is an operand-ROW POINTER (MM-1): int kind AND Op#-prefixed.

    A PURE predicate (§3.1, R6). Takes the ``header`` + ``kind`` the
    caller ALREADY holds from ``read_param_map`` / ``read_cell_kind`` (NO new raw
    ``.NET`` read, NO re-classify, NO new THROW surface — it is a string/str
    comparison composed over the already-guarded ``cell_kind``).

    Operand-AGNOSTIC: knows ONLY that the cell points at a row, NOT what the operand
    MEANS (that is Phase-B reference-team grounding). BOTH conjuncts are load-bearing:

    - the ``Op#`` prefix discriminates (MM-1 counter-example: OSCD's ``Wave`` is an
      int cell that is NOT a ref — a "remap every int cell" rule would corrupt it);
    - the int-kind guard means a (hypothetical) non-int ``Op#``-named cell is never
      treated as a row pointer.

    Covers ``Op#`` / ``Op#1`` / ``Op#2`` / any future ``Op#N`` without a table.
    """
    return kind == "int" and str(header).startswith(_OP_REF_PREFIX)


def read_ref_map(op) -> dict:
    """``{Header: raw_live_row}`` for every Op#-prefixed int cell on this operand row.

    The serialize-side ref reader (§3.1, R6 — the symmetric reader for
    the parallel ``refs`` schema map). Walks cols 2..9 via the EXISTING
    ``read_cell_kind`` (Header-blank-first, THROW-guarded) + ``read_cell`` (type-aware
    int read), returning ONLY the ``Op#`` cells as ``{Header: raw_live_row}``.

    Reuses ``read_cell_kind`` + ``read_cell`` VERBATIM — adds NO new raw ``.NET``
    surface (every read is already L26 THROW-guarded -> ``SurfaceWriteError``). A
    non-``Op#`` cell (incl. OSCD's ``Wave`` int, MM-1) is SKIPPED — it stays a literal
    in ``params``.
    """
    refs = {}
    for col in _PARAM_COLS:
        header, kind = read_cell_kind(op, col)
        if not is_row_ref_header(header, kind):
            continue
        _, value = read_cell(op, col)
        refs[header] = int(value)
    return refs


class ParamCoercionError(ValueError):
    """A ``params`` value could not be coerced to its cell's kind (int/double).

    A plain ``ValueError`` subclass (NOT a ``SurfaceWriteError`` — this is a
    PRE-mutation param-class failure, §3/§7 ``merit_param``). The caller
    (``add_operand`` / ``apply_merit_recipe``) catches it and returns a
    ``merit_param`` ``error_envelope`` WITHOUT mutating the engine.
    """


def coerce_param_value(header, kind, value):
    """Coerce a ``params`` value to its cell ``kind`` (§3 step 4).

    The bool-is-int trap is closed FIRST: a ``bool`` is an ``int`` subclass, and a
    bool param into either an int or double cell is a client bug (the same trap
    ``_lens_common._require_int_index`` guards). Then:

    - ``"int"`` kind requires an EXACT integer (an ``int``, or an integral ``float``
      like ``7.0`` a JSON round-trip can produce); a non-integral / non-number ->
      ``ParamCoercionError``;
    - ``"double"`` kind requires a FINITE number (``int`` or ``float``) -> coerced
      to ``float``; a non-number -> ``ParamCoercionError``; a non-finite value
      (``inf`` / ``-inf`` / ``nan``) -> ``ParamCoercionError`` (a non-physical merit
      target is a client miswrite in the same class as the bool-is-int trap §3 — it
      is rejected PRE-mutation, never written silently, and ``inf`` and ``nan`` are
      both refused EXPLICITLY here rather than relying on the read-back firewall's
      ``nan != nan`` to catch only ``nan``);
    - ``"blank"`` kind is never a writable param -> ``ParamCoercionError``.

    Returns the coerced value (an ``int`` for an int cell, a ``float`` for a double
    cell). Raises ``ParamCoercionError`` on any mismatch (caller -> ``merit_param``).
    """
    if isinstance(value, bool):
        raise ParamCoercionError(
            f"parameter {header!r} must not be a bool ({value!r}); a bool param is "
            "a client bug (bool is an int subclass)"
        )
    if kind == "int":
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ParamCoercionError(
            f"parameter {header!r} is an integer cell; got "
            f"{type(value).__name__} {value!r} (need an exact integer)"
        )
    if kind == "double":
        if isinstance(value, (int, float)):
            coerced = float(value)
            # Reject a non-finite target PRE-mutation (inf/-inf/nan are non-physical
            # merit targets — a client miswrite, same family as the bool-is-int trap).
            # math.isfinite is False for inf, -inf, AND nan, so both are refused here
            # explicitly rather than leaning on the read-back firewall's nan != nan.
            if not math.isfinite(coerced):
                raise ParamCoercionError(
                    f"parameter {header!r} is a numeric cell; got the non-finite "
                    f"value {value!r} (need a finite number — inf/-inf/nan are "
                    "non-physical merit targets and are rejected pre-mutation)"
                )
            # Fix: an int whose float() does NOT round-trip exactly (a magnitude
            # > 2^53 like 9007199254740993) is SILENTLY truncated by float(), and the
            # read-back firewall compares against the already-rounded float so it
            # PASSES — emitting numerically-different data. Reject it pre-mutation so
            # re-apply fidelity holds (§3/§4). Legitimate finite floats are untouched
            # (this guard only fires for an int input that float() cannot represent).
            if isinstance(value, int) and float(coerced) != value:
                raise ParamCoercionError(
                    f"parameter {header!r} is a numeric (double) cell; the integer "
                    f"{value!r} cannot be represented exactly as a float "
                    f"(float({value!r}) == {coerced!r}); refusing the silent "
                    "magnitude truncation that would break re-apply fidelity"
                )
            return coerced
        raise ParamCoercionError(
            f"parameter {header!r} is a numeric cell; got "
            f"{type(value).__name__} {value!r} (need a number)"
        )
    raise ParamCoercionError(
        f"parameter {header!r} maps to a non-writable {kind} cell"
    )


def row_ref_int(value):
    """The effective integer of a row-ref ``Op#`` value, or ``None`` if not integral.

    The SHARED integer-acceptance rule the row-reference dangling guards (§5.3 recipe
    + §6 ``add_operand``) MUST key on so they cannot diverge from the WRITER they
    gate: ``coerce_param_value`` accepts an EXACT ``int`` AND an integral
    ``float`` (``7.0`` -> ``int(7)``, the JSON-round-trip case) into an int cell, so a
    guard that keyed on ``isinstance(value, int)`` alone let an integral float slip the
    range/non-zero check and the raw row was written unverified (the MM-4 silent-wrong
    dangling-ref hazard). This computes the SAME effective integer the writer will
    store:

    - a ``bool`` -> ``None`` (the bool-is-int trap ``coerce_param_value`` rejects FIRST;
      never treat ``True``/``False`` as a row pointer);
    - an exact ``int`` -> the int;
    - an integral ``float`` (``value.is_integer()``) -> ``int(value)``;
    - anything else (a non-integral float / non-number) -> ``None`` (NOT a row-ref the
      guard double-handles — ``coerce_param_value`` will reject it as a non-integer cell
      write; the guard simply does not range-check it).

    Returns the effective ``int`` ONLY when the value would be written as that exact
    integer; otherwise ``None`` (the caller skips the range check and lets
    ``coerce_param_value`` own the non-integral rejection).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def is_valueless_control(token) -> bool:
    """True iff ``token`` is a curated value-less CONTROL operand (CONF this cycle).

    Pure string membership over ``_VALUELESS_CONTROL_OPERANDS``. The caller
    (``add_operand``, ``_author_recipe_operand``, ``serialize_merit``,
    ``_phase1_validate``) uses it to decide whether to SKIP the numeric Target/Weight
    read-back and redirect the proof to the operand's semantic cell.
    """
    return str(token) in _VALUELESS_CONTROL_OPERANDS


def valueless_control_proof_param(token):
    """The required proof-PARAM Header for a value-less control operand, or None.

    A ``"cell:<Header>"`` rule -> ``<Header>`` (the param that MUST be authored +
    validated: CONF -> ``"Cfg#"``); a ``"type"`` rule -> ``None`` (the proof is the
    TypeName read-back, no param). Assumes ``is_valueless_control(token)`` (the caller
    gates on it first).
    """
    rule = _VALUELESS_CONTROL_OPERANDS[str(token)]
    if rule.startswith("cell:"):
        return rule.split(":", 1)[1]
    return None


def validate_valueless_control(token, params, *, n_configs):
    """PRE-mutation validation of a value-less control operand's proof param.

    Returns an error MESSAGE string (the caller turns it into a ``merit_param`` /
    ``merit_recipe_invalid`` refusal), or ``None`` when valid.

    For CONF (proof param ``"Cfg#"``): ``params`` MUST carry ``"Cfg#"``, an INTEGER
    (accepts an integral float ``2.0``, rejects a bool — the SAME ``row_ref_int``
    acceptance the writer ``coerce_param_value`` uses, L30 no-divergence), in
    ``1..n_configs``. A MISSING or OUT-OF-RANGE Cfg# is refused LOUD — the redirected
    read-back-as-proof IS the Cfg# cell, so a CONF with no/bad config number is a SILENT
    HOLE if authored (it would pin a dangling config) and must be refused, NEVER authored.

    A ``"type"``-only operand (proof param None) has no param to validate -> ``None``
    (its proof is the TypeName read-back at author time; the DEFER set lands here).
    """
    proof = valueless_control_proof_param(token)
    if proof is None:
        return None
    params = params or {}
    if not isinstance(params, dict) or proof not in params:
        return (
            f"operand {token} is a value-less control operand and REQUIRES a {proof!r} "
            f"parameter (the config number, 1..{n_configs}); none was given — refusing "
            "rather than authoring a control operand with no config pin"
        )
    cfg = row_ref_int(params[proof])   # exact int OR integral float; bool -> None
    if cfg is None:
        return (
            f"operand {token} parameter {proof!r} must be an integer config number, got "
            f"{params[proof]!r}"
        )
    if not (1 <= cfg <= n_configs):
        return (
            f"operand {token} parameter {proof!r}={cfg} is out of range; the system has "
            f"{n_configs} configuration(s) (valid 1..{n_configs}) — refusing a dangling "
            "config reference"
        )
    return None


def write_verified_cell(op, col, header, value, *, operand_token):
    """Type-aware WRITE of one parameter cell, read-back-proven (the §2 firewall).

    The generalization of ``_structural_common._add_bound_operand``'s firewall
    shape: write the accessor MATCHING the cell's kind, read it back type-aware,
    funnel through ``_lens_common._verify_or_raise`` (the EXISTING oracle). A silent
    no-op -> ``SurfaceWriteError``.

    Steps:

    1. Read the cell's LIVE ``Header`` (THROW-guarded). If it does not match the
       intended ``header`` -> ``CellLayoutError`` (the layout shifted, refuse — do
       NOT write the wrong cell).
    2. Classify the LIVE cell via ``cell_kind`` (the SAME Header-blank-first /
       ``cell.DataType`` rule ``read_cell`` reads with — read/write symmetry: a Double
       param writes ``DoubleValue``, an Integer param ``IntegerValue``) and write the
       matching accessor. The write is THROW-guarded -> structured
       ``SurfaceWriteError`` on a raw .NET throw (L26). A ``"blank"`` target is a
       layout error (you cannot write a blank col).
    3. Read the value back type-aware (THROW-guarded) and verify it through
       ``_verify_or_raise`` (a silent no-op / rejected write -> ``SurfaceWriteError``).

    ``value`` is assumed already coerced to the right Python type by the caller
    (``add_operand`` / ``apply_merit_recipe`` coerce per kind BEFORE this call).
    Returns the read-back value.
    """
    live_header = _read_header(op, col)
    if str(live_header) != str(header):
        raise CellLayoutError(
            f"merit cell layout mismatch for operand {operand_token}: intended to "
            f"write {header!r} at col {col} but the live cell Header is "
            f"{live_header!r} — refusing rather than writing the wrong cell",
            field="cell_layout",
            intended=header,
            actual=live_header,
            surface=None,
        )

    # Derive the kind from the LIVE cell (the SAME DataType rule read_cell uses —
    # read/write symmetry), THROW-guarded -> structured SurfaceWriteError (L26).
    _, kind = read_cell_kind(op, col)
    if kind == "blank":
        raise CellLayoutError(
            f"merit cell at col {col} for operand {operand_token} is a blank/unused "
            f"column (Header={live_header!r}) — refusing to write a parameter there",
            field="cell_layout",
            intended=header,
            actual=live_header,
            surface=None,
        )

    # (2) write the accessor matching the cell kind, THROW-guarded (L26).
    try:
        cell = op.GetCellAt(col)
        if kind == "int":
            cell.IntegerValue = value
        else:
            cell.DoubleValue = value
    except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not write the {kind} value {value!r} to merit cell {header!r} "
            f"(col {col}) of operand {operand_token} ({exc!r}); the engine rejected "
            "the write — refusing rather than shipping an unverified cell",
            field="cell_write",
            intended=value,
            actual=None,
            surface=None,
        ) from exc

    # (3) read-back type-aware + verify (a silent no-op -> SurfaceWriteError).
    _, actual = read_cell(op, col)
    _lc._verify_or_raise(f"{operand_token}.{header}", value, actual, surface=None)
    return actual


def apply_params(op, params, *, operand_token):
    """Validate + coerce + write a ``{Header: value}`` ``params`` dict (§3).

    The SHARED params-authoring core ``add_operand`` and ``apply_merit_recipe`` both
    call on a freshly-typed operand row. Flow (all NAME validation BEFORE any write):

    1. ``read_param_map(op)`` -> the operand's LIVE valid Header signature.
    2. EVERY ``params`` key NOT in the live signature -> ``ParamCoercionError``
       (the param-name-typo guard) BEFORE any cell write. The caller turns this into
       a ``merit_param`` envelope (no mutation).
    3. For each valid ``(header, value)`` -> coerce per kind (``coerce_param_value``)
       then ``write_verified_cell`` (read-back-proven). A coercion failure ->
       ``ParamCoercionError`` (``merit_param``); a silent no-op / layout mismatch ->
       ``SurfaceWriteError`` / ``CellLayoutError`` (``surface_write``).

    Returns ``{header: written_value}`` (the echo of what stuck). Raises
    ``ParamCoercionError`` (param-class, pre/non-mutating per name) or
    ``SurfaceWriteError`` (engine write firewall). NEVER swallows a failure.
    """
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ParamCoercionError(
            f"params must be a dict {{Header: value}}, got {type(params).__name__}"
        )

    live = read_param_map(op)
    valid_headers = sorted(live)
    # (2) param-name-typo guard: reject EVERY unknown name BEFORE any write.
    unknown = [k for k in params if k not in live]
    if unknown:
        raise ParamCoercionError(
            f"operand {operand_token} has no parameter "
            f"{unknown[0]!r}; valid: {valid_headers}"
        )

    written = {}
    for header, value in params.items():
        kind = live[header]["kind"]
        coerced = coerce_param_value(header, kind, value)
        col = live[header]["col"]
        written[header] = write_verified_cell(
            op, col, header, coerced, operand_token=operand_token
        )
    return written


__all__ = [
    "cell_kind",
    "read_cell_kind",
    "read_cell",
    "read_param_map",
    "is_row_ref_header",
    "read_ref_map",
    "row_ref_int",
    "is_valueless_control",
    "valueless_control_proof_param",
    "validate_valueless_control",
    "_VALUELESS_CONTROL_OPERANDS",
    "write_verified_cell",
    "coerce_param_value",
    "apply_params",
    "CellLayoutError",
    "ParamCoercionError",
    "_INT_HEADERS",
    "_BLANK_HEADER",
    "_OP_REF_PREFIX",
    "_PARAM_COLS",
]
