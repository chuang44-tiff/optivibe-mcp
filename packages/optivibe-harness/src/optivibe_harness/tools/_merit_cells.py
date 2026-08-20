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


def _base_token(value):
    """This module's handle on the ONE base-slot normalizer (``_optimize_common``'s).

    ROUND-12 H-2. The round-10a helper closed the forging-``__str__`` door in
    ``_optimize_common`` / ``optimize_run`` / ``optimize_merit`` — but every merit-cell
    Header those modules reason about is READ HERE, one frame upstream, through a bare
    ``str(...)``. A Header whose true buffer is ``Mode`` and whose ``__str__`` forges
    ``Surf1`` came out of ``_read_header`` as an exact, FORGED ``'Surf1'``, so a
    downstream ``_base_token`` had nothing left to recover: the door was being shut on a
    wall that had already fallen. Normalizing at the READ is what makes the downstream
    normalization mean anything.

    The import is FUNCTION-LOCAL and that is deliberate, not laziness: ``_optimize_common``
    imports THIS module at its top (``from . import _merit_cells``), so a module-level
    import here would be a cycle. Delegating rather than re-implementing keeps the shape
    behind ONE name — the two obvious one-line spellings of it are both measurably
    wrong, which is the whole reason the helper exists.
    """
    from . import _optimize_common as _oc   # local: _optimize_common imports this module

    return _oc._base_token(value)


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
    unused column carries ``Header == " "`` (a single space). ``header`` is normalized
    through the BASE SLOT (``_base_token``), not ``str(...)`` — the live ``cell.Header``
    is a .NET ``System.String`` proxy, and ROUND-12 H-2 measured that a bare ``str(...)``
    here hands a FORGED token to the comparison (and, on a ``str`` subclass, hands the
    subclass's own ``__eq__`` the last word on whether the column is blank).
    """
    return _base_token(header).strip() == ""


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
        # ROUND-12 H-2: BASE SLOT on the DataType read. MEASURED with a bare ``str(...)``
        # here: a cell whose ``DataType`` forges ``"Double"`` over a true ``Integer``
        # buffer classified ``double`` and ``read_cell`` then took ``DoubleValue`` — the
        # exact wrong-accessor read this function's docstring says NEVER happens.
        data_type = _base_token(cell.DataType)
    except Exception:  # noqa: BLE001 — DataType read THROW -> Header fallback (defensive)
        return "int" if _base_token(header) in _INT_HEADERS else "double"
    return "int" if data_type == "Integer" else "double"


def _read_header(op, col):
    """Read ``op.GetCellAt(col).Header`` THROW-guarded -> structured SurfaceWriteError.

    A raw .NET THROW on the ``GetCellAt`` / ``Header`` read re-raises a structured
    ``SurfaceWriteError`` (the L26 firewall) rather than escaping dispatch as an
    opaque ``internal``. Returns the Header as an EXACT ``str``.

    **ROUND-12 H-2 — THE NORMALIZATION IS HERE, AT THE READ, NOT AT THE COMPARISON.**
    This used to be ``str(op.GetCellAt(col).Header)``. Round 10a put ``_base_token`` on
    the Header COMPARISONS one frame downstream (``_optimize_common._read_range_pair``'s
    ``match1``/``match2``), but the value those comparisons receive is whatever THIS
    function returned — and a bare ``str(...)`` had already dispatched
    ``type(header).__str__`` and FORGED it. Measured, all three ways it escaped:

    * a Header whose true buffer is ``NOT_A_RANGE`` and whose ``__str__`` returns
      ``Surf1`` came out of here as an exact, forged ``'Surf1'`` — irrecoverable by any
      downstream normalizer, so ``_read_range_pair`` read ``("WELL_FORMED", 1, 3)`` off
      two cells named ``Wave``/``Ring``;
    * on a ``str`` SUBCLASS with no ``__str__`` override the return was the SUBCLASS, so
      the docstring line above was false and ``read_param_map`` used it as a DICT KEY —
      a cell named ``Mode`` answered ``d.get("Surf1")``;
    * every consumer that compares the result (``write_verified_cell``'s layout guard,
      ``_INT_HEADERS`` membership) was therefore asking the value what it thought.

    Fixing it HERE means every consumer inherits it, which is why this is one line and
    not one line per consumer.
    """
    try:
        return _base_token(op.GetCellAt(col).Header)
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

    The kind-DERIVATION primitive shared by ``read_param_map``, ``read_ref_map`` and
    ``write_verified_cell``: the kind comes from the LIVE cell, never re-derived from
    a Header string (that was the Header-only bug). THROW-guarded the same way: a
    ``GetCellAt`` / ``DataType`` / ``Header`` THROW re-raises a structured
    ``SurfaceWriteError`` (the firewall) rather than escaping dispatch as an opaque
    ``internal``.

    **ROUND-12 — TWO CORRECTIONS TO THE SENTENCE THIS DOCSTRING USED TO CARRY.** It said
    the primitive was *"shared by ``read_cell`` and ``read_param_map`` so the kind is
    derived ONCE"*. ``read_cell`` does NOT call this function — it calls ``_read_header``
    and ``cell_kind`` itself — and nothing here is derived once per CELL either. Counted
    against instrumented accessors: this call costs 2 ``GetCellAt`` + 2 ``Header`` + 1
    ``DataType``; ``read_cell`` alone costs 2 ``GetCellAt`` + 2 ``Header`` + 1
    ``DataType`` + 1 value read. "ONCE" is true of the RULE (one discriminator, the live
    ``DataType``), not of the read count, and the read count is what a cost claim reads
    as. Pinned by ``test_r12_the_documented_cell_read_costs_are_the_measured_ones``.
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

    **ROUND-12 H-2, AND THE ONLY SITE IN THIS FILE THE SWEEP STRUCTURALLY COULD NOT FIND.**
    This was ``str(header).startswith(...)``. The normalization sweep decides comparisons,
    subscript keys, set/dict members, the ``float``/``int``/``sorted``/``set`` arguments,
    f-strings, ``.format()`` and ``%`` — it does NOT decide a ``str``-METHOD predicate, so
    a forging Header reaching a ``.startswith`` was invisible to the closure row while
    deciding whether a cell is an operand ROW POINTER (and therefore whether
    ``apply_merit_recipe`` remaps it). It is safe TODAY only because every in-repo caller
    passes a Header that ``_read_header`` has already normalized — which is a fact about
    the callers, not about this predicate, and this one is exported and documented as
    taking "the header the caller ALREADY holds".

    ``.startswith`` is itself overridable on a subclass, so the base slot has to sit on the
    RECEIVER: ``_base_token`` returns an exact ``str``, whose ``startswith`` is the builtin.
    """
    return kind == "int" and _base_token(header).startswith(_OP_REF_PREFIX)


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


#: The reason vocabulary of the TWO shared numeric resolvers below.
#:
#: **ONE token set, so that a range/authoring DOOR and the WRITER (``coerce_param_value``,
#: below) can share one CLASSIFICATION — that sharing is the whole point of the governing
#: ticket.** The two sides answer the same question ("is this an integral
#: surface index, and what exactly is it?") and used to answer it with two bodies; a
#: shared reason token is what lets ONE classification feed TWO different error channels
#: without either side re-deriving the other's rule (a door maps it to ``None`` / a
#: verdict code, the writer to a ``ParamCoercionError``). Merging them into one unbounded
#: acceptor was explicitly REJECTED — the *magnitude policy* stays with
#: each caller — so what is shared is the CLASSIFICATION and nothing else (the ticket's
#: option (b)).
NUMERIC_OK = "ok"
#: Not a number at all: a ``bool`` (an ``int`` subclass, and a client bug), a ``str``,
#: ``None``, a list — anything that is not an ``int``/``float`` instance.
NUMERIC_NOT_A_NUMBER = "not_a_number"
#: **The conversion DID NOT REPEAT**. Converting the value twice in a row
#: produced two different numbers, so the number this resolver would report is not the
#: number a LATER conversion will produce. Only reachable for an ``int``/``float``
#: SUBCLASS, because only a subclass can put user code (``__int__`` / ``__float__``) on
#: the conversion path — see the resolvers' own notes for the measurement.
NUMERIC_UNSTABLE = "unstable"
#: A number, but not a whole one (``2.5``) — or a non-finite, which is not integral
#: either. ``resolve_integral`` only.
NUMERIC_NOT_INTEGRAL = "not_integral"
#: ``inf`` / ``-inf`` / ``nan`` — a non-physical merit target. ``resolve_double`` only.
NUMERIC_NOT_FINITE = "not_finite"
#: **``float(value)`` could not convert the value AT ALL**: an ``int`` whose
#: magnitude exceeds the float range (``10**400``) raises ``OverflowError``.
#: ``resolve_double`` only.
NUMERIC_UNREPRESENTABLE = "unrepresentable"
#: ``float(value)`` converted but LOST exactness (the case, ``2**53 + 1``). DISTINCT
#: from ``NUMERIC_UNREPRESENTABLE`` on purpose: two different diagnoses with two
#: different messages, and acceptance item 3 forbids collapsing them.
#: ``resolve_double`` only.
NUMERIC_INEXACT_INT = "inexact_int"
#: The conversion RAISED something other than the overflow above — a hostile
#: ``__int__``/``__float__``, a ``__float__`` returning a non-float (``TypeError``), a
#: metaclass making ``isinstance`` raise. Reported rather than propagated so both
#: resolvers can promise NEVER to raise.
NUMERIC_CONVERSION_FAILED = "conversion_failed"


def _same_number(a, b):
    """Are two ALREADY-CONVERTED readings the same number, treating ``nan`` as equal?

    **``==`` is the wrong operator here and the bug it causes is a false accusation.**
    ``nan != nan`` is True by IEEE, so a plain ``a != b`` stability test reports EVERY
    ``nan``-carrying value as "the conversion did not repeat" — which is both wrong (the
    conversion repeated perfectly; it repeated ``nan``) and diagnostically worse than the
    truth, since the honest answer for that value is *non-finite*, an arm that already
    exists with its own message. Found by this cycle's own re-pointed adversarial row: a
    ``float`` subclass carrying ``nan`` took the UNSTABLE arm instead of the non-finite
    one.

    Both arguments are outputs of ``int()``/``float()`` on the same object, so they are
    exact builtins — no user code runs in this comparison.
    """
    if a == b:
        return True
    return isinstance(a, float) and isinstance(b, float) \
        and math.isnan(a) and math.isnan(b)


def resolve_integral(value):
    """``(int, NUMERIC_OK)`` iff ``value`` is an integral index; else ``(None, reason)``.

    **THE ONE BODY behind "is this an integral surface index?".** The WRITER
    (``coerce_param_value``'s ``int`` arm, below) calls THIS rather than implementing the
    rule, and any range/authoring door built over this module is required to do the same:
    the question gets ONE body, not one per caller. That is a RULE this file states, not a
    guarantee it can enforce — nothing here can see its callers, so a door that keeps its
    own copy breaks it silently, and one already did. The consequence was a
    ``{"Surf1": 2**53 + 1, "Surf2": 3}`` DESCENDING range authored under ``ok: true``: the
    door deferred to a writer refusal that never came, because the magnitude guard
    lived in the ``double`` arm only. The two bodies were brought back into agreement at
    an earlier round and nothing pinned the agreement, which is the drift surface this closes.

    **NEVER raises.** Every branch that can run user code is inside the outer catch, and
    an unclassifiable value is reported as ``NUMERIC_CONVERSION_FAILED`` — fail-CLOSED
    for every caller (a door resolves ``None`` and refuses/defers; the writer raises
    ``ParamCoercionError``).

    An ``int`` resolves **EXACTLY at any magnitude** and that is deliberate, not an
    oversight: the range decision needs only ORDERING, and Python ints compare exactly
    at any magnitude, so no ``float`` round-trip is required to make it. Routing an
    ``int`` through ``float(value) != int(value)`` is what produced the drift above
    (``2**53 + 1`` read as "not an index", ``10**400`` raised ``OverflowError``). The
    WRITER's magnitude question is a different one — what an Int32 cell can actually
    store — and it stays with the writer; this function does not answer it.

    ``int(first)`` / ``int(value)`` and never ``value``: this STRIPS an ``int`` SUBCLASS,
    so a hostile ``__le__`` / ``__eq__`` cannot reach a range door's ``0 <= surf1 <= surf2``
    comparison or a refusal message's ``.format()``.

    **THE STABILITY PROBE, and why it exists.** MEASURED on this
    interpreter: ``int(x)`` on an ``int`` SUBCLASS dispatches to ``__int__`` on EVERY
    call, so an object returning 2 then 9 is converted to two different numbers by two
    adjacent calls. The door judged conversion #1 (2, admitted) and the writer performed
    conversion #2 (9, authored) — cells ``[9, 3]``, DESCENDING and out of domain, with
    ``ok: True`` and no disclosure. So a value whose conversion does not REPEAT is
    refused here, in the one body rather than at any caller.

    **The probe reads the OVERRIDE and does not bypass it, and that distinction is
    load-bearing.** Reading ``int.__int__(value)`` to get the underlying value was
    measured to make the defect WORSE: with a subclass of value 2 whose
    ``__int__`` returns 9 unconditionally, the shipped code judges **9** — the number the
    writer actually authors — and refuses it on its ordering; a bypass would judge 2,
    admit, and let ``[9, 3]`` through. The probe preserves that: a stable override is
    still read at its override value and still refused downstream on ordering/domain.

    ``type(value) is int`` SKIPS the probe. That is not an optimisation for its own sake:
    an exact ``int`` has NO user code on its conversion path, so the second call cannot
    differ, and a range door reads live cells (exact marshalled numbers) on its hot path.

    **What this does NOT establish, stated rather than implied.** The probe converts
    TWICE. An adversary whose conversion is constant for the first *k* calls and differs
    afterwards is NOT caught, and no finite number of probes catches one — so this
    refuses *observably* unstable conversions, not "unstable values". An ``int`` subclass
    with a stable override (``enum.IntEnum`` is the ordinary case, and it is MEASURED
    stable) is unaffected.
    """
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, NUMERIC_NOT_A_NUMBER
        if isinstance(value, int):
            first = int(value)
            if type(value) is not int and int(value) != first:
                return None, NUMERIC_UNSTABLE
            return first, NUMERIC_OK
        # A float (or a float subclass). Resolve the integer FROM the probed conversion
        # rather than from the underlying value: MEASURED, ``int(float_subclass)`` does
        # NOT dispatch to ``__float__`` while ``float(float_subclass)`` does, so the two
        # can disagree. Deriving both the ordering decision and the integer from ONE
        # conversion is what makes a door and the writer agree by construction.
        first = float(value)
        if type(value) is not float and not _same_number(float(value), first):
            return None, NUMERIC_UNSTABLE
        if not math.isfinite(first) or first != int(first):
            return None, NUMERIC_NOT_INTEGRAL
        return int(first), NUMERIC_OK
    except Exception:  # noqa: BLE001 — see "NEVER raises" above; fail CLOSED
        return None, NUMERIC_CONVERSION_FAILED


def resolve_double(value):
    """``(float, NUMERIC_OK)`` iff ``value`` is a finite double; else ``(None, reason)``.

    **THE ONE BODY behind "is this a writable double?"**, and the fix for: the
    ORDER of its first two steps is the whole defect. The arm this replaces read

        1. ``coerced = float(value)``      <- RAISES for an int ``float()`` cannot convert
        2. ``math.isfinite(coerced)``
        3. the guard: an int whose ``float()`` does not round-trip -> refuse

    **Step 3 exists to refuse exactly this class of value and step 1 made it unreachable
    above a magnitude.** MEASURED: ``add_operand("MNEA", params={"Zone": 10**400})``
    raised ``OverflowError: int too large to convert to float`` out of a function whose
    only documented failure is ``ParamCoercionError``, with ``cell WRITES attempted:
    []``. ``2**53 + 1`` reached step 3 and was refused cleanly; ``10**400`` never got
    there. The guard was not wrong — it was DEAD above a magnitude, and the boundary
    between "refused cleanly" and "raises" was undocumented.

    So the conversion is attempted INSIDE a guard and an overflow becomes
    ``NUMERIC_UNREPRESENTABLE`` — a REFUSAL the caller can turn into the ``merit_param``
    message every other bad param already earns. The two int diagnoses stay DISTINCT
    (``NUMERIC_UNREPRESENTABLE`` = cannot convert at all; ``NUMERIC_INEXACT_INT`` = the
    Case, converts but loses exactness), because collapsing them would trade one
    honest message for two vaguer ones.

    **NEVER raises**, and carries the same stability probe as
    ``resolve_integral`` — with a different dunder, which is the point. MEASURED:
    ``float(int_subclass)`` dispatches to ``__float__`` and NOT to ``__int__``, so the
    ``double`` arm's instability vector is an independent one; a probe that only watched
    ``__int__`` would miss it entirely. ``math.isfinite`` is itself a third conversion on
    a subclass, which is why it is applied to the already-converted ``first`` and never
    to ``value``.

    The magnitude policy is NOT shared with ``resolve_integral``, and that split is
    deliberate: an ``int`` resolves exactly at any magnitude as an INDEX,
    while as a DOUBLE it must survive a float round-trip or the read-back firewall
    compares against an already-rounded value and passes while emitting
    numerically-different data.
    """
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, NUMERIC_NOT_A_NUMBER
        try:
            first = float(value)
        except OverflowError:
            # The raise the guard three lines down was written to prevent.
            return None, NUMERIC_UNREPRESENTABLE
        if type(value) not in (int, float) and not _same_number(float(value), first):
            return None, NUMERIC_UNSTABLE
        if not math.isfinite(first):
            return None, NUMERIC_NOT_FINITE
        # An int whose float() does NOT round-trip exactly is SILENTLY truncated,
        # and the read-back firewall compares against the already-rounded float so it
        # PASSES. Refuse pre-mutation so re-apply fidelity holds. Compared against the
        # ORIGINAL int (Python compares int and float exactly, at any magnitude).
        if isinstance(value, int) and first != value:
            return None, NUMERIC_INEXACT_INT
        return first, NUMERIC_OK
    except Exception:  # noqa: BLE001 — NEVER raises; fail CLOSED
        return None, NUMERIC_CONVERSION_FAILED


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

    **This function no longer implements either numeric rule — it CONSUMES
    ``resolve_integral`` / ``resolve_double`` and maps their reason to a message.** A
    range door consuming the same two bodies is what keeps the two sides from drifting,
    and honouring that is the DOOR's obligation — not something this function can
    establish from here. What lives here is the ERROR CHANNEL (a ``ParamCoercionError``
    naming the cell), which is the one thing only this function knows how to write.

    **It raises ``ParamCoercionError`` and NOTHING ELSE, with ONE named escape** — the
    ``OverflowError`` at ``10**400`` is now the ``NUMERIC_UNREPRESENTABLE``
    refusal below. The one residual raise is a hostile ``__repr__`` on a value
    interpolated into a message; the two NEW arms therefore name the value's TYPE rather
    than its ``repr``, and the pre-existing arms keep their ``{value!r}`` byte-identical.
    """
    if isinstance(value, bool):
        raise ParamCoercionError(
            f"parameter {header!r} must not be a bool ({value!r}); a bool param is "
            "a client bug (bool is an int subclass)"
        )
    if kind == "int":
        coerced, reason = resolve_integral(value)
        if reason == NUMERIC_OK:
            return coerced
        if reason in (NUMERIC_UNSTABLE, NUMERIC_CONVERSION_FAILED):
            raise ParamCoercionError(_unstable_param_message(header, value, "integer"))
        raise ParamCoercionError(
            f"parameter {header!r} is an integer cell; got "
            f"{type(value).__name__} {value!r} (need an exact integer)"
        )
    if kind == "double":
        coerced, reason = resolve_double(value)
        if reason == NUMERIC_OK:
            return coerced
        if reason in (NUMERIC_UNSTABLE, NUMERIC_CONVERSION_FAILED):
            raise ParamCoercionError(_unstable_param_message(header, value, "numeric"))
        if reason == NUMERIC_UNREPRESENTABLE:
            #. The magnitude is named by its DIGIT COUNT, not by dumping 401
            # digits into an error string — and not via ``{value!r}``, which a hostile
            # ``__repr__`` can make raise out of a function that promises to raise only
            # ``ParamCoercionError``.
            raise ParamCoercionError(
                f"parameter {header!r} is a numeric (double) cell; the supplied "
                f"{type(value).__name__} is too large to represent as a float at all "
                f"({_magnitude_hint(value)}), so it cannot be written — refusing it "
                "here rather than raising past the parameter firewall"
            )
        if reason == NUMERIC_NOT_FINITE:
            # A non-finite target is a client miswrite in the same class as the
            # bool-is-int trap: inf/-inf/nan are non-physical merit targets and are
            # rejected PRE-mutation rather than leaning on the read-back firewall's
            # ``nan != nan`` (which would catch only nan).
            raise ParamCoercionError(
                f"parameter {header!r} is a numeric cell; got the non-finite "
                f"value {value!r} (need a finite number — inf/-inf/nan are "
                "non-physical merit targets and are rejected pre-mutation)"
            )
        if reason == NUMERIC_INEXACT_INT:
            #. ``float(value)`` is re-evaluated for the message ONLY: this reason is
            # returned solely after that conversion already SUCCEEDED, so it cannot
            # raise here, and the text stays byte-identical to the pre-one.
            raise ParamCoercionError(
                f"parameter {header!r} is a numeric (double) cell; the integer "
                f"{value!r} cannot be represented exactly as a float "
                f"(float({value!r}) == {float(value)!r}); refusing the silent "
                "magnitude truncation that would break re-apply fidelity"
            )
        raise ParamCoercionError(
            f"parameter {header!r} is a numeric cell; got "
            f"{type(value).__name__} {value!r} (need a number)"
        )
    raise ParamCoercionError(
        f"parameter {header!r} maps to a non-writable {kind} cell"
    )


def _magnitude_hint(value):
    """A BOUNDED description of how big a number is, for a refusal message.

    Never interpolates the value itself: ``10**400`` is 401 characters of digits and a
    hostile ``__repr__`` raises. Digit count IS the magnitude, and it is one short
    phrase. Falls back to a bare phrase if even the length cannot be taken, so a message
    helper can never be the thing that raises.
    """
    try:
        return f"{len(str(abs(int(value))))} decimal digits; the float ceiling is ~1.8e308"
    except Exception:  # noqa: BLE001 — a message must never raise
        return "beyond the float range (~1.8e308)"


def _unstable_param_message(header, value, cell_word):
    """The refusal for a value whose numeric conversion did not REPEAT.

    Names the INSTABILITY rather than the value, because the value is precisely the
    thing that has no single answer — and because reading it is what is unsafe. Shared
    by both arms so the two cannot describe the same hazard two ways.
    """
    article = "an" if cell_word.startswith(("a", "e", "i", "o", "u")) else "a"
    return (
        f"parameter {header!r} is {article} {cell_word} cell; the supplied "
        f"{type(value).__name__} did not convert to a stable number (converting it "
        "twice in a row produced two different results, or the conversion raised). "
        "Refusing it: the value written would not be the value checked. Pass a plain "
        f"{'int' if cell_word == 'integer' else 'int or float'}."
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
    return _base_token(token) in _VALUELESS_CONTROL_OPERANDS   # base slot: hashed lookup


def valueless_control_proof_param(token):
    """The required proof-PARAM Header for a value-less control operand, or None.

    A ``"cell:<Header>"`` rule -> ``<Header>`` (the param that MUST be authored +
    validated: CONF -> ``"Cfg#"``); a ``"type"`` rule -> ``None`` (the proof is the
    TypeName read-back, no param). Assumes ``is_valueless_control(token)`` (the caller
    gates on it first).
    """
    rule = _VALUELESS_CONTROL_OPERANDS[_base_token(token)]   # base slot: hashed lookup
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
    # ROUND-12 H-1. This was ``str(live_header) != str(header)`` — a MUTATION guard whose
    # stated job is "refusing rather than writing the wrong cell", decided by asking the
    # live Header what it thought its own name was. MEASURED end-to-end: a Header whose
    # true buffer is ``Mode`` and whose ``__str__`` forges ``Surf1`` made
    # ``write_verified_cell(op, 2, 'Surf1', 5, operand_token='MNEA')`` RETURN 5 — it wrote
    # into the cell, and the read-back "proof" agreed because it re-read the same forged
    # name. The honest-mismatch control was refused correctly the whole time, so nothing
    # about the guard LOOKED dead. Both sides go through the base slot: reflected
    # ``__eq__``/``__ne__`` means normalizing one side is not enough, and ``header`` is
    # caller-supplied so it is not trusted either.
    if _base_token(live_header) != _base_token(header):
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
    # The ONE body per numeric question, consumed by ``coerce_param_value``
    # in this module. Exported because a range/authoring door in this package would be a
    # legitimate consumer, not because anything outside this package should call them.
    "resolve_integral",
    "resolve_double",
    "NUMERIC_OK",
    "NUMERIC_NOT_A_NUMBER",
    "NUMERIC_UNSTABLE",
    "NUMERIC_NOT_INTEGRAL",
    "NUMERIC_NOT_FINITE",
    "NUMERIC_UNREPRESENTABLE",
    "NUMERIC_INEXACT_INT",
    "NUMERIC_CONVERSION_FAILED",
    "apply_params",
    "CellLayoutError",
    "ParamCoercionError",
    "_INT_HEADERS",
    "_BLANK_HEADER",
    "_OP_REF_PREFIX",
    "_PARAM_COLS",
]
