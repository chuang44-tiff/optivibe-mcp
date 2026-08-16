"""tools/_solve_cells.py — the SURFACE-SOLVE substrate.

This module LANDED as a shared floor with ZERO user-visible change: the ``read_surface`` /
``describe_surfaces`` dicts and the write path wire into it, and until they did, every
symbol here was UNCALLED from production (the call-site ledger test asserted the count was
EXACTLY zero, so it reddened the day one was wired).

WHAT IS HERE, AND WHY EACH PIECE EXISTS
---------------------------------------
- ONE cell fetcher (``solve_cell``) over the COLUMN route. Not a style choice: the
  repo's shared, self-declared "single locus" SemiDiameter solve read
  (``_clearance_common.semi_solve_type_name``) uses ``row.GetSurfaceCell(col)``, and
  the agreement pin ties this block's ``Fixed`` semi_diameter to THAT reader — a
  property-route substrate would measure the pin across two different cell handles.
  Every sibling substrate agrees (``_cb_cells``, ``_asphere_cells``, ``_grin_cells``,
  ``freeze_semi``). The only property-route site in ``src/`` is
  ``optimize_variable.py:66``, via a three-token map that cannot reach semi_diameter or
  material at all.

  HONEST PROVENANCE: this substrate's entire evidence base (the default table, the 39
  field shapes, the ``Supports*`` flags, every available list) was gathered through the
  PROPERTY route. The live route-agreement falsifier is NON-WAIVABLE. If the routes
  disagree the catalog is RE-ANCHORED — that is a re-probe, not a patch.

- ONE fail-CLOSED ``_S_<Name>`` resolver body with TWO NAMED entry points:
  ``unwrap_or_raise`` (write/proof) and ``unwrap_or_none`` (never-raise read). The rule is
  ENFORCED AT THE SIGNATURE — there is no ``raise_on_fault=`` boolean, no mode string
  and no ``**kwargs``, because a flag lets an edit at a distance flip a never-raise READ
  caller into a raising one. The fork lives at the CALLER.

  It is cloned in SHAPE from ``_aperture_cells._typed_view`` — the audited house
  pattern with NO ``except``-and-return-the-bare-object path. The idiom this DELETES is
  ``cb_surface.py:986``'s ``getattr(solve, "_S_SurfacePickup", solve)``, whose
  fall-through hands back the un-unwrapped bare object so the three following field
  writes land on the wrong object (and whose 3-arg default swallows ``AttributeError``
  ONLY — a .NET throw still propagates, so it is not even uniform).

- A per-cell DEFAULT table and a per-cell SUPPRESSION set that are ROLE-INDEPENDENT
  (MEASURED: OBJECT, interior and IMAGE expose the same five cells, the same
  DataTypes, byte-identical available lists and the same defaults).

- A DRIVING set that is the COMPLEMENT of a small non-driving set — never an enumerated
  driving allow-list, which would fail OPEN on a 40th engine member.

- A probe-FROZEN ``(cell, type)`` field catalog (the ``_tolerance_catalog`` /
  ``_mce_catalog`` precedent) with a TAGGED THREE-WAY lookup, so "measured, no fields"
  can never masquerade as "never measured".

NO SEVENTH ``_readback_ok``. Six copies already exist; a seventh is the anti-pattern.
This module defines none and imports none (a source guard asserts the token is absent).

NO NEW ERROR CLASS AND NO NEW ``error_family``. Every raise here is an existing
``ToolParamError`` (client/param) or ``SurfaceWriteError`` (family ``surface_write``),
so the error taxonomy is byte-unchanged.
"""
import math

from ..enums import _resolve_enum
from ..errors import SurfaceWriteError, ToolParamError


# --------------------------------------------------------------------------- #
# The five-token vocabulary and the COLUMN map.
# --------------------------------------------------------------------------- #
#: ORDERED, never a set — this is the emit iteration order and therefore load-bearing
#: for the key-order half of the acceptance criterion.
CELL_TOKENS = ("radius", "thickness", "conic", "semi_diameter", "material")

#: token -> ``SurfaceColumn`` MEMBER name (the COLUMN route).
#:
#: The "SUPERSET of ``_CELL_TO_PROP``" relation holds over KEYS. The CODOMAIN deliberately
#: differs: ``_CELL_TO_PROP`` maps to ``ILDERow`` PROPERTY names, this maps to
#: ``SurfaceColumn`` member names. Stated rather than left to half-hold silently.
TOKEN_TO_COLUMN = {
    "radius": "Radius",
    "thickness": "Thickness",
    "conic": "Conic",
    "semi_diameter": "SemiDiameter",
    "material": "Material",
}


# --------------------------------------------------------------------------- #
# The default table (role-INDEPENDENT) and the derived suppression set.
# --------------------------------------------------------------------------- #
#: The solve a NEVER-AUTHORED cell of a freshly ``New()``ed system carries.
#:
#: MEASURED at the probe capture's ``default_table_fresh``.
#:
#: THE TRAP, NAMED SO IT CANNOT BE WALKED INTO. The same capture also carries a
#: MAJORITY-DERIVED default over WILD UNFILTERED loaded rows. Both live in the SAME file,
#: both are named like a default, and the second sits under a key literally called
#: ``derived_majority_default``. They DISAGREE on exactly the cell this whole asymmetric
#: rule turns on: fresh interior SemiDiameter is ``{"Automatic": 4}`` while the majority
#: over 175 loaded interior rows is ``Fixed`` (88 of 175). Keying suppression on
#: FREQUENCY derives ``Fixed`` for semi_diameter and INVERTS the rule — every untouched
#: interior surface would gain a block AND every ``freeze_semidiameters`` result would
#: be SUPPRESSED, which is the exact state this disclosure exists to read.
#:
#: A suppression rule may key only on PROVENANCE ("the solve on a never-authored cell of
#: a freshly-New()ed system"), never on FREQUENCY.
DEFAULT_SOLVE_BY_CELL = {
    "radius": "Fixed",
    "thickness": "Fixed",
    "conic": "Fixed",
    "semi_diameter": "Automatic",
    "material": "Fixed",
}

#: What is SUPPRESSED (not emitted) per cell: the default, plus the STRING ``"None"``.
#:
#: DERIVED from ``DEFAULT_SOLVE_BY_CELL`` — never a second literal that could drift.
#:
#: The ``"None"`` arm is UNEXERCISED and labelled as such: the member ``None`` is
#: offered by ZERO of the five cells' ``GetAvailableSolveTypes`` on a fresh build and
#: appears ZERO times in the 19-design corpus. It ships as defence in depth, proven
#: OFFLINE only. Note the set holds the STRING ``"None"``, not Python's ``None`` — a
#: frozenset containing the singleton type-checks, imports, and silently never matches.
SUPPRESSED_BY_CELL = {
    token: frozenset({DEFAULT_SOLVE_BY_CELL[token], "None"})
    for token in CELL_TOKENS
}


# --------------------------------------------------------------------------- #
# The DRIVING set.
# --------------------------------------------------------------------------- #
#: Solves that do NOT drive a value. ``driving`` is the COMPLEMENT over ``str(Type)``
#: — the other 35 members — never an enumerated driving allow-list, which fails OPEN on
#: a 40th engine member (a fail-open on a safety predicate).
#:
#: ``Automatic`` IS in this set and that is the operative decision (the two readings
#: disagree by exactly this one member). Interior SemiDiameter reads ``Automatic`` on 87
#: of 175 corpus rows and ``set_surface`` writes semi_diameter on EVERY apply, so a
#: three-member reading would refuse roughly half of every loaded design and falsify
#: this decision's own promise that the ordinary workflow is byte-unchanged.
NON_DRIVING = frozenset({"None", "Fixed", "Variable", "Automatic"})


# --------------------------------------------------------------------------- #
# The probe-FROZEN (cell, type) field catalog.
# --------------------------------------------------------------------------- #
# "<Cell>.<Type>" -> (curated field NAMES, supports_offset, supports_scale)
#
# The 39 MEASURED pairs: 35 from ``facts.solve_field_table`` (Radius 13, Thickness 12,
# SemiDiameter 5, Material 5; ZERO ConicCell) plus 4 from a SEPARATE CONIC field-shape
# probe capture. Frozen here as a literal on the
# ``_tolerance_catalog`` / ``_mce_catalog`` precedent (``src/`` must not import a test
# fixture); a test asserts this table equals the capture-derived one in
# BOTH directions, so the literal cannot drift from its evidence.
#
# ``fields`` (settable, emitted) and the ``Supports*`` capabilities (predictive,
# catalog-only) are DIFFERENT lists — the curated SurfacePickup shape is exactly
# ("Column", "Offset", "ScaleFactor", "Surface") with ``Supports*`` ABSENT, and both are
# correct. The capability values were read on a READ-BACK object; ``None`` means never
# read (see ``CAPABILITY_STAGE`).
_MEASURED = {
    "ConicCell.Fixed": ((), None, None),
    "ConicCell.SurfacePickup": (("Column", "Offset", "ScaleFactor", "Surface"), False, True),
    "ConicCell.Variable": ((), None, None),
    "ConicCell.ZPLMacro": (("Macro",), None, None),
    "MaterialCell.Fixed": ((), None, None),
    "MaterialCell.MaterialModel": (("AbbeVd", "IndexNd", "VaryAbbe", "VaryIndex", "VarydPgF", "dPgF"), None, None),
    "MaterialCell.MaterialOffset": (("NdOffset", "VdOffset"), None, None),
    "MaterialCell.MaterialSubstitute": (("Catalog",), None, None),
    "MaterialCell.SurfacePickup": (("Column", "Offset", "ScaleFactor", "Surface"), False, False),
    "RadiusCell.Aplanatic": ((), None, None),
    "RadiusCell.ChiefRayAngle": (("Angle",), None, None),
    "RadiusCell.ChiefRayNormal": ((), None, None),
    "RadiusCell.CocentricRadius": (("WithSurface",), None, None),
    "RadiusCell.CocentricSurface": (("AboutSurface",), None, None),
    "RadiusCell.ElementPower": (("Power",), None, None),
    "RadiusCell.FNumber": (("FNumber",), None, None),
    "RadiusCell.Fixed": ((), None, None),
    "RadiusCell.MarginalRayAngle": (("Angle",), None, None),
    "RadiusCell.MarginalRayNormal": ((), None, None),
    "RadiusCell.SurfacePickup": (("Column", "Offset", "ScaleFactor", "Surface"), False, True),
    "RadiusCell.Variable": ((), None, None),
    "RadiusCell.ZPLMacro": (("Macro",), None, None),
    "SemiDiameterCell.Automatic": ((), None, None),
    "SemiDiameterCell.Fixed": ((), None, None),
    "SemiDiameterCell.Maximum": ((), None, None),
    "SemiDiameterCell.SurfacePickup": (("Column", "Offset", "ScaleFactor", "Surface"), False, True),
    "SemiDiameterCell.ZPLMacro": (("Macro",), None, None),
    "ThicknessCell.CenterOfCurvature": (("RefSurface",), None, None),
    "ThicknessCell.ChiefRayHeight": (("Height",), None, None),
    "ThicknessCell.Compensator": (("RefSurface", "Sum"), None, None),
    "ThicknessCell.EdgeThickness": (("RadialHeight", "Thickness"), None, None),
    "ThicknessCell.Fixed": ((), None, None),
    "ThicknessCell.MarginalRayHeight": (("Height", "PupilZone"), None, None),
    "ThicknessCell.OpticalPathDifference": (("OPD", "PupilZone"), None, None),
    "ThicknessCell.Position": (("FromSurface", "Length"), None, None),
    "ThicknessCell.PupilPosition": ((), None, None),
    "ThicknessCell.SurfacePickup": (("Column", "Offset", "ScaleFactor", "Surface"), True, True),
    "ThicknessCell.Variable": ((), None, None),
    "ThicknessCell.ZPLMacro": (("Macro",), None, None),
}

#: The 39 measured ``"<Cell>.<Type>"`` keys.
MEASURED_PAIRS = frozenset(_MEASURED)

#: Solve fields whose value is a SURFACE INDEX (integral, in ``0..N-1``). It lives BESIDE
#: the catalog that classifies it (``_MEASURED``) because the scan intersects the two.
#:
#: MOVED HERE FROM ``surface_solve._INDEX_FIELDS``. Two consumers
#: now read one vocabulary: ``surface_solve``'s authoring door (which validates a supplied
#: index field) and ``_solve_refs``'s removal scan (which intersects these names with each
#: ``(cell, type)`` pair's ``_MEASURED`` field tuple to decide which solves reference a
#: named row). Two REJECTED alternatives, recorded so they are not re-proposed: a hand copy
#: in the scanner (two authorities for one vocabulary — the single-locus drift class), and the
#: scanner importing ``surface_solve._INDEX_FIELDS`` (which extends a tool module's PRIVATE
#: surface into a cross-module contract and couples a leaf helper to the heaviest tool
#: module in the family). ``surface_solve.py:72`` is now a one-statement alias onto this.
INDEX_FIELDS = ("Surface", "WithSurface", "AboutSurface", "RefSurface", "FromSurface")

#: The stage the ``Supports*`` values were read at. The flag/value correlation is
#: STAGE-DEPENDENT, correcting an earlier claim: it is NOT total pre-authoring — before
#: authoring, the ``Supports*`` flag predicts the ``ScaleFactor`` reading but NOT the
#: ``Offset`` one, because a freshly created, not-yet-authored ThicknessCell reads
#: ``SupportsOffset True`` WITH ``Offset`` NaN, while ``ScaleFactor`` pre-reads 1.0
#: wherever supported. On a READ-BACK object it is exact (9/9 and 4/4). Any acceptance
#: predicate built on it is defined ONLY here.
#:
#: PRIVATE. It and ``capabilities_of`` were public names ADDED beyond what this module
#: needs, and both had ZERO production consumers — only the acceptance test's own symbol
#: list made them look reachable. The capability PROVENANCE dimension is deliberately
#: deferred while the live oracle is kept: ``read_supports_flag`` stays public and
#: the provenance accessor is GONE — its data is still carried per-entry in ``_MEASURED``
#: for whichever consumer eventually needs it. Shipping an API no caller reaches is
#: how a substrate grows a surface it then has to keep.
_CAPABILITY_STAGE = "readback"


def lookup(cell_token, type_str):
    """The TAGGED THREE-WAY catalog lookup. Never a bare tuple-or-None.

    Returns one of::

        ("measured",       (field, ...))   -> emit  fields: [...]
        ("measured_empty", ())             -> emit  fields: []
        ("unmeasured",     None)           -> emit  fields: null + fields_unmeasured

    ``measured_empty`` is REAL, not a degenerate ``measured``: ``Fixed`` and ``Variable``
    each produce a LIVE object (``created_is_none`` false, ``unwrap_is_none`` false)
    whose curated field list is genuinely empty. Collapsing the two is FORBIDDEN —
    folding ``[]`` into ``null`` lets a knowledge gap masquerade as a measurement, and
    folding ``null`` into "unreadable" lets it masquerade as a wedged cell forever.
    """
    key = "%s.%s" % (_column_of(cell_token), type_str)
    entry = _MEASURED.get(key)
    if entry is None:
        return ("unmeasured", None)
    fields = entry[0]
    return ("measured_empty", ()) if not fields else ("measured", fields)


def _column_of(cell_token):
    """token -> ``"<Column>Cell"`` (the catalog key half). Unknown token -> the token."""
    col = TOKEN_TO_COLUMN.get(cell_token)
    return (col + "Cell") if col else str(cell_token)


def available_solve_names(cell):
    """The cell's LEGAL solve names as a TAGGED pair ``(tag, names)``. NEVER raises.

    Four tags, THREE of which are distinct failures. The distinction is the whole point:
    ``if not available:`` conflates all four, and a validator built on that conflation
    would treat an engine that cannot REPORT its legal set exactly like one that reports
    an empty one — refusing with the wrong diagnosis, or (worse) authoring blind::

        "ok"          clean call; ``names`` = the order-preserving de-dup on the
                      canonical ``str()`` render
        "empty"       clean call, the de-duplicated result is empty; ``names`` = ()
        "unreadable"  the call threw, OR iteration threw part-way; ``names`` = the
                      de-duped PARTIAL prefix — DECORATION-ONLY. A validator must NEVER
                      membership-test a partial list: a type absent from a truncated
                      prefix is not a type the cell refuses.
        "absent"      ``GetAvailableSolveTypes`` is missing or not callable — engine
                      drift, not a fault of the design; ``names`` = ()

    ORDER-PRESERVING, NEVER ``set()``: RadiusCell's 15-entry list carries
    ``CocentricSurface`` AND ``CocentricRadius`` TWICE each, and this list is shown to a
    HUMAN in a refusal message, so its order is part of the diagnosis. (The
    justification "``set()`` would keep all 15" was falsified live — members hash by
    VALUE, so ``set()`` keeps 13 too — but the ORDER argument stands and is the real one.
    The cardinality is derived from the capture, never transcribed as a literal.)

    THE FAULT-CONTRACT RULE APPLIED CORRECTLY: ONE body over ONE engine method, forking
    at the CALLER by which half of the tuple it reads — not by a ``raise_on_fault=``
    boolean an edit at a distance could flip. ``_deduped_available_names`` below is the
    never-fails message-decoration entry point; the authoring ladder is the entry point that
    reads the TAG and refuses on it.

    STATED SO NO TEST MISCOUNTS IT: the 15-reader census governs solve-NAME readers
    (``str(cell.GetSolveData().Type)``). This is an available-SET reader over a DIFFERENT
    engine method with its own fault contract; it does not join that census.

    ALL THREE FAILURE ARMS ARE DEFENSIVE — none has a live reproduction. A probe tried
    EMPTY_LIST, THROW and MISSING_METHOD and produced none of them, including via a
    deliberate stale handle; the 39-row legal-type sweep produced none either. The
    fakes express them behind explicit knobs; no test may claim to reproduce one.

    THE AUTHORING PATH SHIPPED IT WITH ITS CONSUMER. This substrate CUT the same helper
    precisely because its promised refusal had zero production call sites, and a
    validator nothing calls is a refusal that cannot fire.
    """
    out = []
    try:
        # THE LOOKUP IS INSIDE THE GUARD. It sat outside, so a
        # property-backed or dead proxy that RAISES while resolving the member escaped
        # this function through its own documented "NEVER raises" boundary and reached
        # dispatch as ``internal`` — an infrastructure-looking failure for a defensive
        # validation case. ``getattr(..., None)`` still swallows a plain
        # ``AttributeError``, so a genuinely MISSING method keeps the ``absent`` tag and
        # only a THROWING lookup becomes ``unreadable``: the ABSENT-vs-UNREADABLE
        # split survives the widening, which is the thing a careless fix would collapse.
        getter = getattr(cell, "GetAvailableSolveTypes", None)
        if not callable(getter):
            return ("absent", ())
        for member in getter():
            name = str(member)
            if name not in out:
                out.append(name)
    except Exception:  # noqa: BLE001 — a lookup/call/iteration fault is TAGGED, never raised
        return ("unreadable", tuple(out))
    return ("ok", tuple(out)) if out else ("empty", ())


def _deduped_available_names(cell):
    """The cell's LEGAL solve names for a MESSAGE. Returns ``[]`` on any fault.

    A 1-statement DELEGATE over ``available_solve_names``, and BYTE-IDENTICAL to the body
    it replaces for every existing caller: call throws -> ``[]``; method absent -> ``[]``;
    iteration throws part-way -> the partial prefix. It reads only the NAMES half of the
    tuple and discards the tag, which is exactly the fork that must live at the caller.

    It exists solely to make a refusal MESSAGE name the real legal set, so it NEVER raises
    — a message adornment must not convert a precise diagnosis into a crash.
    """
    return list(available_solve_names(cell)[1])


# --------------------------------------------------------------------------- #
# The two PROMOTED editor-enum resolvers.
# --------------------------------------------------------------------------- #
# Moved VERBATIM from ``freeze_semi`` (which now keeps two 3-line DELEGATES — defs that
# CALL through, never module-level aliases: an alias binds at import time, so a test
# monkeypatching this substrate would not be seen by freeze_semi and the two would
# silently split).
#
# THE DIRECTION IS freeze_semi -> _solve_cells, i.e. tool -> substrate. This module
# imports NEITHER freeze_semi NOR lens_surface, and an AST test asserts it.
#
# AND THERE WAS NO CYCLE TO BREAK. The text that stood here until it was corrected
# asserted that ``_clearance_common`` kept a FUNCTION-LOCAL
# ``from .freeze_semi import _surface_column_enum`` "to break the cycle".
# That import is now a MODULE-level ``from . import _solve_cells as _sc``,
# and the harness rebuilt to prove it measured the documented freeze -> clearance cycle
# NEVER BINDING at module level: the promotion supplied a re-point target safe by ASSERTED
# INVARIANT, not one that repaired a live cycle. The stale sentence outlived its subject long
# enough to be quoted onward as corroborating evidence, and was then RETRACTED
# — which is why this family's standing rule is that no in-source comment is admissible as
# evidence without re-derivation from the binding.
def surface_column_enum(system):
    """Resolve the live ``SurfaceColumn`` enum TYPE (ZOSAPI.Editors.LDE; fake-injectable)."""
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


def solve_type_enum(system):
    """Resolve the live ``SolveType`` enum TYPE (ZOSAPI.Editors; fake-injectable).

    NO membership validation and NO cross-reference against the 39-member roster. The
    natural instinct once a roster exists would reject the three-member enum the test
    fake injects and redden every test that relies on that fake. The roster is the
    FAKE's contract, never a gate on the live enum.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "SolveType" in injected:
        return injected["SolveType"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors as _ed  # type: ignore

        return _ed.SolveType
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve SolveType from ZOSAPI.Editors: {exc}"
        )


# --------------------------------------------------------------------------- #
# THE ONE cell fetcher (COLUMN route).
# --------------------------------------------------------------------------- #
def solve_cell(system, lde, surface, cell_token):
    """Fetch surface ``surface``'s ``cell_token`` cell. Re-fetches BOTH row and cell.

    NEVER hold a proxy across a read-back boundary — every call re-fetches the row
    AND the cell, so a caller cannot accidentally read a stale pre-mutation handle.

    Raises ``ToolParamError`` on an unknown token (listing ``CELL_TOKENS``);
    ``SurfaceWriteError`` FAIL-CLOSED naming the token when the ``SurfaceColumn`` member
    is absent (future engine drift), and on a fetch throw.
    """
    if cell_token not in TOKEN_TO_COLUMN:
        raise ToolParamError(
            f"unknown solve cell token {cell_token!r}; valid: {list(CELL_TOKENS)}"
        )
    col_name = TOKEN_TO_COLUMN[cell_token]
    col_enum = surface_column_enum(system)
    try:
        col = _resolve_enum(col_enum, col_name)
    except ToolParamError as exc:
        raise SurfaceWriteError(
            f"the SurfaceColumn member {col_name!r} (cell {cell_token!r}) is absent on "
            f"the live enum ({exc}); refusing rather than guessing a column",
            field="solve_cell", intended=col_name, actual=None, surface=surface,
        ) from exc
    try:
        return lde.GetSurfaceAt(surface).GetSurfaceCell(col)
    except Exception as exc:  # noqa: BLE001 — a cell fetch throw -> surface_write
        raise SurfaceWriteError(
            f"could not fetch the {cell_token!r} cell of surface {surface} "
            f"({exc!r}); refusing rather than reading an unknown cell",
            field="solve_cell", intended=col_name, actual=None, surface=surface,
        ) from exc


# --------------------------------------------------------------------------- #
# The fail-CLOSED resolver: ONE body, TWO NAMED entry points.
# --------------------------------------------------------------------------- #
def _unwrap_core(solve_data):
    """``(view, fault_reason|None)``. THREE arms, each of which is a real failure.

    THE KEY IS ALWAYS ``str(solve_data.Type)`` — the LIVE spelling, never a
    caller-supplied one. That is what makes the alias case work: a solve created from the
    ``ConcentricRadius`` spelling reports ``Type`` as ``CocentricRadius`` (the same .NET
    value under its canonical render) and resolves through ``_S_CocentricRadius``.

    NEVER a 3-arg ``getattr``. The 3-arg form is the fail-open defect deleted from
    ``cb_surface.py:986``, and it swallows ``AttributeError`` ONLY — a .NET throw
    still propagates — so it is not even uniform in what it hides.
    """
    # arm 1 — no object at all, or its Type is unreadable.
    if solve_data is None:
        return None, "the solve data object is None"
    try:
        type_name = str(solve_data.Type)
    except Exception as exc:  # noqa: BLE001
        return None, f"the solve Type could not be read ({exc!r})"
    attr = "_S_" + type_name
    # arm 2 — the typed accessor is absent / threw.
    try:
        view = getattr(solve_data, attr)
    except Exception as exc:  # noqa: BLE001
        return None, f"the typed solve view {attr!r} is absent ({exc!r})"
    # arm 3 — the accessor answered, with nothing.
    if view is None:
        return None, f"the typed solve view {attr!r} resolved to None"
    return view, None


def unwrap_or_raise(solve_data, *, surface=None, cell_token=None):
    """Resolve the ``_S_<TypeName>`` view, FAILING CLOSED. The write/proof entry point.

    There is NO ``except``-and-return-the-bare-object path here: the only return is the
    resolved view; all three arms RAISE a bare ``SurfaceWriteError`` (family
    ``surface_write``). Cloned in SHAPE from ``_aperture_cells._typed_view``.

    ENFORCED AT THE SIGNATURE: this and ``unwrap_or_none`` are two NAMED functions over one
    body. There is no boolean, no mode string and no ``**kwargs`` — a
    ``raise_on_fault=`` parameter would let a never-raise READ caller be flipped by an
    edit at a distance, which is the fault-contract flattening this rule vetoes.
    """
    view, fault = _unwrap_core(solve_data)
    if fault is not None:
        raise SurfaceWriteError(
            f"{fault}; refusing rather than falling through to the bare solve object "
            "(the un-unwrapped object accepts the field writes and silently drops them)",
            field="solve_view", intended=cell_token, actual=None, surface=surface,
        )
    return view


def unwrap_or_none(solve_data):
    """The never-raise READ entry point over the SAME body. Cannot raise.

    Catches ``Exception``, NOT ``BaseException`` — an abort travelling this path must
    not be absorbed.
    """
    try:
        view, _fault = _unwrap_core(solve_data)
    except Exception:  # noqa: BLE001 — a never-raise reader absorbs nothing above Exception
        return None
    return view


# --------------------------------------------------------------------------- #
# The fault-aware solve-type reader.
# --------------------------------------------------------------------------- #
def read_solve_type(cell):
    """``str(cell.GetSolveData().Type)`` or ``None`` on ANY throw. NEVER raises.

    CONSUMES ``_mce_cells.solve_type_name`` rather than inlining a SIXTEENTH
    ``str(cell.GetSolveData().Type)`` with a sixteenth fault contract (there is
    a standing veto on adding one).

    Consumers must test ``is None``, never truthiness — the empty string and the string
    ``"None"`` are both falsy-adjacent readings that mean something quite different from
    "the read failed".
    """
    from . import _mce_cells as _mce
    return _mce.solve_type_name(cell)


# --------------------------------------------------------------------------- #
# The two predicates. They are INDEPENDENT and must stay so.
# --------------------------------------------------------------------------- #
def is_default_solve(cell_token, type_name):
    """True iff ``type_name`` is SUPPRESSED for ``cell_token`` (the emit gate).

    An unknown token or an unreadable type answers False, i.e. EMIT — the disclosing
    direction. A suppression predicate that fails open would hide state; this one fails
    towards showing it.
    """
    if not isinstance(type_name, str):
        return False
    return type_name in SUPPRESSED_BY_CELL.get(cell_token, frozenset())


def is_driving(type_name):
    """True iff ``type_name`` is a DRIVING solve (the complement of ``NON_DRIVING``).

    A non-string (notably Python's ``None``, meaning "the read failed") answers True —
    UNKNOWN is treated as driving, the fail-CLOSED direction for a safety predicate.
    Note the trap this dodges: ``str(None)`` is the string ``"None"``, which IS in
    ``NON_DRIVING``, so a naive ``str()`` coercion would call an unreadable cell
    non-driving and fail OPEN.

    THIS IS NOT THE SUPPRESSION PREDICATE, and the two are not interchangeable:
    ``semi_diameter``/``Fixed`` is EMITTED yet NON-driving; ``semi_diameter``/
    ``Automatic`` is SUPPRESSED and also non-driving; ``radius``/``Fixed`` is both.
    Reusing one set for the other makes a frozen semi-diameter either invisible or
    falsely refused.
    """
    if not isinstance(type_name, str):
        return True
    return type_name not in NON_DRIVING


# --------------------------------------------------------------------------- #
# The driven-cell advisory. RAISES NOTHING.
# --------------------------------------------------------------------------- #
def refuse_if_driven(system, lde, surface, cell_token):
    """Is this cell driven by a solve? ``{driven, solve_type, reason}``. NEVER raises.

    Its consumers are ``set_surface``'s write loop and ``_apply_object_fields``,
    plus the variable-authoring tools. The call sites own the raise into a refusal;
    keeping the raise at the CALLER is what keeps the error taxonomy byte-unchanged and
    keeps the fault-contract fork where it belongs.

    TWO CORRECTIONS to the earlier text, annotated beside rather than renumbered:
    ``_apply_object_fields`` is DEFINED at ``lens_spec.py:789``; ``:643`` is one of its
    two CALL SITES, and guarding the call site would leave ``set_surface(surface=0)``
    (via ``lens_surface.py:281``) unguarded — the shipped-once bypass. And the
    variable-authoring surface is SIX tools, not five (the sixth is
    ``set_grin_variable``). The call-site ledger declares this function's live call count.

    ``driven`` is a TRI-STATE. An unreadable solve returns ``None`` (UNKNOWN), never
    ``False`` — ABSENT and UNREADABLE are different facts and only one of them is safe
    to proceed on.

    EXACTLY four parameters, no options. Resolves the row through ``_require_read_index``
    (``0 <= surface <= N-1``), NOT ``_require_geometry_index`` — the latter refuses
    surface 0 and would re-break the finite-conjugate incremental build.

    The surface acceptance predicate is the WRITER's own ``is_integral_int``, not a
    re-derived ``isinstance(surface, int)`` — ``bool`` is an ``int`` subclass, so the
    naive guard admits ``True``/``False`` and silently reads row 1 / the OBJECT.
    """
    from ._lens_common import _require_read_index
    from ._tol_cells import is_integral_int

    if not is_integral_int(surface):
        return {"driven": None, "solve_type": None,
                "reason": f"surface {surface!r} is not an exact integer index"}
    try:
        n = int(lde.NumberOfSurfaces)
        _require_read_index(surface, n)
    except Exception as exc:  # noqa: BLE001 — never raises past this boundary
        return {"driven": None, "solve_type": None,
                "reason": f"the surface index could not be validated ({exc!r})"}
    try:
        cell = solve_cell(system, lde, surface, cell_token)
    except Exception as exc:  # noqa: BLE001
        return {"driven": None, "solve_type": None,
                "reason": f"the {cell_token!r} cell could not be fetched ({exc!r})"}
    type_name = read_solve_type(cell)
    if type_name is None:
        return {"driven": None, "solve_type": None,
                "reason": f"the {cell_token!r} solve type could not be read"}
    driven = is_driving(type_name)
    return {
        "driven": driven,
        "solve_type": type_name,
        "reason": (f"{cell_token} is driven by a {type_name} solve" if driven
                   else f"{cell_token} carries a non-driving {type_name} solve"),
    }


# --------------------------------------------------------------------------- #
# The consequence of the defective closed form: TWO primitives and NO arithmetic.
# --------------------------------------------------------------------------- #
# The T2 closed form (``value == scale*source + offset``) IS DEFECTIVE, and this substrate
# does not implement it. Measured over all 13 live pickups in the stock corpus,
# ``Offset`` is NaN on 9 of 13, so T2 as written would RAISE on 3 CORRECT radius pickups
# in ``Relay lens.zmx`` — the closed form's own worked example class. This substrate must not ship
# ``pickup_predicted_value()``, or the write path inherits a defect wrapped in a green test.
#
# **REFUSING IT WAS MORE RIGHT THAN THE FIRST DEFECT MADE IT LOOK.** The T2 that DID ship
# in ``surface_solve`` derives from the CALLER'S STATED INTENT rather than from a read-back,
# so a NaN ``Offset`` is never read and the 3 correct radius pickups are never refused.
# A later probe then found a SECOND defect in the same closed form: the law is keyed on the
# TARGET cell, and a ``radius`` target scales CURVATURE (``R = source / ScaleFactor``), so
# the single linear form computes 200.0 where the engine produces 50.0 for every
# ``|ScaleFactor| != 1``. Every prior measurement had used +/-1, where the two models
# COINCIDE. The warning is kept, with BOTH defects named, because "the thing this
# substrate refused to ship turned out to be wrong in TWO ways".
#
# RECORDED, NOT ENCODED: the offset term is REAL (authoring 2.5 moved the target to 6.5
# from a source of 4.0; residual 0.0 with the term and 2.5 without). The corpus alone
# could not discriminate, because every corpus offset is 0.0. And T3 must NOT be applied
# to a freshly-created not-yet-authored object, whose Offset reads NaN even where
# ``SupportsOffset`` is True.
def read_solve_fields(inner, cell_token, type_name):
    """``{field: (value, state)}`` — a PURE PASSTHROUGH of read-back values. No maths.

    ``state`` is::

        "matched"           the field read back a representable value
        "not_representable" the read succeeded but the value is non-finite
                            (``math.isfinite`` so +/-inf is caught too) -> value null
        "mismatched"        the field could not be READ at all

    ``matched`` IS A CLAIM ABOUT READABILITY, NOT ABOUT WIRE SAFETY, and conflating the
    two is what a filed report described: this reader scores a
    raw ``SurfaceColumn`` proxy ``matched`` (it is neither ``int`` nor ``float``, so it
    takes the ``not isinstance(...)`` branch) while ``assert_wire_safe`` REFUSES exactly
    that object. Both are right about their own question. The reader is a PURE
    PASSTHROUGH and coercing here would exceed a substrate's remit, so the
    coercion lives at the EMIT boundary — in ``to_wire`` below, which every consumer of
    this function's values must route through.

    THE SCOPE IS MEASURED AND CLOSED, not assumed (two live probes, all 39 catalogued
    pairs authored plus the 8 geometry-dependent ones a planar fixture cannot reach):
    ``Column`` is the ONLY field in the whole catalog that is ``matched`` here and
    refused by the wire gate. Every other field reads back ``int`` / ``float`` /
    ``bool`` / ``str`` and is wire-safe.
    There is no disagreement in the other direction either — nothing is ``mismatched``
    yet wire-safe. ``ZPLMacro``'s ``Macro`` field is the one pair no live measurement
    reached (it needs a macro file) and is therefore UNMEASURED, not cleared.

    NON-FINITE, MEASURED: the only kind ever observed live is ``nan`` (5 of 5 readings,
    all ``Offset``, plus ``ScaleFactor`` on a Material column). No ``+inf``/``-inf``
    reading was produced by any pair, which is the standing measurement behind the LOW
    severity of the non-finite discriminator.

    NON-FINITE IS CONVERTED TO ``None`` BEFORE THE DICT IS BUILT, and that ordering is a
    CONTRACT, not a style choice. ``float('nan') != float('nan')``, but dict equality
    takes a per-value IDENTITY shortcut first: ``{'a': x} == {'a': x}`` is True for the
    SAME nan object and False for two distinct ones. The acceptance criterion is
    dict-equality and Offset reads NaN on 9 of 13 corpus pickups, so a block that
    embedded a live NaN would compare UNEQUAL to an identically-produced sibling and the
    criterion would fail on CORRECT code.
    """
    tag, fields = lookup(cell_token, type_name)
    if tag == "unmeasured":
        return {}
    out = {}
    for name in fields:
        try:
            value = getattr(inner, name)
        except Exception:  # noqa: BLE001 — an unreadable field is disclosed, never faked
            out[name] = (None, "mismatched")
            continue
        # THE CLASSIFICATION is inside its own guard, not only the ``getattr``.
        # ``math.isfinite`` raises ``OverflowError`` on an int too large for a float, and
        # an earlier check sat OUTSIDE the try — so a read that SUCCEEDED could still
        # escape this never-fake reader as an unstructured exception past its own
        # documented boundary. ``not_representable`` (not ``mismatched``) is the honest
        # verdict: the read worked, the VALUE does not fit.
        try:
            representable = (isinstance(value, bool)
                             or not isinstance(value, (int, float))
                             or math.isfinite(value))
        except Exception:  # noqa: BLE001 — OverflowError et al: unrepresentable, not unread
            representable = False
        out[name] = (value, "matched") if representable else (None, "not_representable")
    return out


def read_supports_flag(inner, field):
    """``True`` / ``False`` / ``None`` — the RUNTIME capability oracle. Never raises.

    ``None`` means the flag could not be read: a missing flag is UNKNOWN, never False.
    This is the direct per-object invariant and needs no per-column table — which
    matters, because whether the discriminator is fundamentally the FLAG or the COLUMN
    is NOT resolved by any measurement (``SupportsOffset`` is True exactly on Thickness
    columns across every pickup measured, so both hypotheses fit every data point).
    """
    try:
        value = getattr(inner, "Supports" + field)
    except Exception:  # noqa: BLE001 — an absent/throwing flag is UNKNOWN, never False
        return None
    return bool(value) if isinstance(value, bool) else None


# --------------------------------------------------------------------------- #
# Wire safety.
# --------------------------------------------------------------------------- #
#: The recursion bound. An emitted solve block is at most 3 levels deep
#: (``solves`` -> token -> entry), so 32 is ~10x the deepest legitimate structure while
#: staying far below Python's own limit — the bound refuses a pathological input, it
#: does not constrain a real one.
_WIRE_MAX_DEPTH = 32


def assert_wire_safe(block, _path="block", _depth=0):
    """Recursively assert every value in ``block`` is a wire-safe OUTPUT type.

    ALLOW-LIST: ``str``, ``bool``, ``int``, finite ``float``, ``None``, ``list``,
    ``dict``. Anything else RAISES. It NEVER attempts a coercion.

    DEPTH-BOUNDED. Unbounded recursion made a self-referential or ~1000-deep
    block raise ``RecursionError`` — NOT the structured ``SurfaceWriteError`` every
    other refusal in this module raises, and not a family the dispatch envelope
    classifies. Unreachable through ``emit_solves_block``'s shallow output; reachable
    through the PUBLIC symbol, which is the one this module exports.

    WHY A TYPE ASSERTION AND NOT THE WIRE'S SCRUB. ``Column`` is a raw
    ``ZOSAPI.Editors.LDE.SurfaceColumn``: ``json.dumps`` RAISES on it, every
    ``isinstance`` int/str/float/bool is False, and its ``repr`` carries NO
    ``" at 0x<hex>"`` — so the MCP wire's address scrub demonstrably would NOT fire and
    would emit the literal ``"<SurfaceColumn.Radius: 2>"``. Worse, ``int(Column)``
    SUCCEEDS and returns 2, so a coercion reflex silently emits ``2`` where the token
    ``"Radius"`` was intended. The type assertion is the operative half.

    ``bool`` is checked BEFORE ``int``: ``isinstance(True, int)`` is True, so an
    int-first allow-list silently reclassifies every boolean.
    """
    if _depth > _WIRE_MAX_DEPTH:
        raise SurfaceWriteError(
            f"emitted block nests deeper than {_WIRE_MAX_DEPTH} at {_path}; refusing "
            "rather than recursing — a self-referential structure would otherwise "
            "escape as RecursionError, which no error family classifies",
            field="wire_safety", intended=f"depth <= {_WIRE_MAX_DEPTH}",
            actual=str(_depth),
        )
    if block is None or isinstance(block, (str, bool)):
        return True
    if isinstance(block, int):
        return True
    if isinstance(block, float):
        if not math.isfinite(block):
            raise SurfaceWriteError(
                f"non-finite float at {_path} ({block!r}); a NaN in an emitted block "
                "makes two identically-produced blocks compare UNEQUAL under dict-==",
                field="wire_safety", intended="finite float", actual=repr(block),
            )
        return True
    if isinstance(block, list):
        for i, item in enumerate(block):
            assert_wire_safe(item, "%s[%d]" % (_path, i), _depth + 1)
        return True
    if isinstance(block, dict):
        for key, item in block.items():
            if not isinstance(key, str):
                raise SurfaceWriteError(
                    f"non-string dict key at {_path}: {key!r} ({type(key).__name__})",
                    field="wire_safety", intended="str key", actual=repr(key),
                )
            assert_wire_safe(item, "%s[%r]" % (_path, key), _depth + 1)
        return True
    raise SurfaceWriteError(
        f"non-wire-safe value at {_path}: {type(block).__name__} "
        f"(repr {block!r}); refusing rather than coercing — int() on a live .NET enum "
        "proxy SUCCEEDS and would silently emit an ordinal in place of the token",
        field="wire_safety", intended="str|bool|int|finite float|None|list|dict",
        actual=type(block).__name__,
    )


# --------------------------------------------------------------------------- #
# The shared EMIT body. THE SUBSTRATE INCREMENT SHIPPED IT UNWIRED.
# --------------------------------------------------------------------------- #
#: The surface types whose solve state IS fully covered by the five cells — a small,
#: EVIDENCED set, and the disclosure is its COMPLEMENT.
#:
#: THIS WAS AN ENUMERATED ALLOW-LIST AND IT FAILED OPEN. An earlier revision listed the
#: eight Par-cell-bearing types it happened to think of and disclosed only for those, so
#: Toroidal / Biconic / ZernikeStandardSag / ExtendedPolynomial / Irregular / Gradium /
#: GridGradient / QTypeAsphere / Gradient1/4/5/6/7/9/10/12 — every type nobody listed —
#: answered False and the emitted block CLAIMED A COMPLETE SOLVE AUDIT on a surface
#: whose solve state the five-cell scope never touched. That is precisely the shape this
#: module forbids twice in its own words, forty lines apart:
#:
#:   ":43  a DRIVING set that is the COMPLEMENT of a small non-driving set — never an
#:         enumerated driving allow-list, which would fail OPEN on a 40th engine member"
#:   ":130 never an enumerated driving allow-list, which fails OPEN on a 40th engine
#:         member (a fail-open on a safety predicate)"
#:
#: It shipped because the corpus is 211 Standard + 2 EvenAspheric, so no unlisted
#: type was ever read. The EVIDENCE for ``Standard`` is exactly that corpus; anything
#: else discloses until a measurement says otherwise.
#:
#: (A blast-radius rule elsewhere was the SAME mistake made at the same time — an
#: enumerated token allow-list standing in for "everything except what is declared" —
#: and got the same fix. One shape, two instances; naming that here is
#: what stops a third being written by someone who saw two point fixes.)
_FULLY_COVERED_TYPE_TOKENS = frozenset({"Standard"})


def _par_cell_row(row):
    """True if this row's solves are NOT fully covered by the five cells. FAIL-OPEN.

    An unreadable ``row.Type`` answers True — the OPPOSITE polarity to every other guard
    in this module, and deliberately so: the key is a DISCLOSURE that the audit
    is incomplete, so "I could not tell" must disclose, not stay silent. Stated here so
    a sibling sweep does not "fix" it into a refusal.

    An UNRECOGNISED type answers True for the same reason, and that is the fix:
    a type this module has never measured is a type whose Par-cell state is unknown.
    """
    try:
        type_name = str(row.Type)
    except Exception:  # noqa: BLE001 — an unreadable Type -> disclose (fail-OPEN)
        return True
    return type_name not in _FULLY_COVERED_TYPE_TOKENS


def _mce_overridden(system):
    """True if more than one configuration exists. FAIL-OPEN on an unreadable MCE."""
    try:
        return int(system.MCE.NumberOfConfigurations) > 1
    except Exception:  # noqa: BLE001 — an unreadable MCE -> disclose (fail-OPEN)
        return True


def _assert_exhaustive_partition(emitted, unreadable, suppressed, surface=None):
    """emitted u suppressed u unreadable == ``CELL_TOKENS``, PAIRWISE DISJOINT. Or RAISE.

    Asserted over the FROZEN ``CELL_TOKENS`` tuple, NEVER over the keys the loop
    happened to produce — the latter makes the invariant true by construction and
    therefore worthless.

    The tolerance reconcile precedent is the reason this exists: without it a
    ``continue`` on an unexpected shape yields an absent key, an EMPTY unreadable list,
    and a patch that reads exactly like a healthy default system. At an emit rate near
    50% a silently-short block is unremarkable to a reader, so the failure would not be
    noticed by inspection.

    It is a NAMED helper rather than an inline check so the invariant can be driven
    directly. Through ``emit_solves_block`` alone it is unreachable — every loop path
    classifies its token — which would leave the guard permanently unexercised and
    unprovable: a proof that cannot fire.
    """
    expected = list(CELL_TOKENS)
    buckets = list(emitted) + list(unreadable) + list(suppressed)
    if sorted(buckets) != sorted(expected):
        raise SurfaceWriteError(
            f"the solve emit partition is not exhaustive for surface {surface}: "
            f"emitted={sorted(emitted)} unreadable={sorted(unreadable)} "
            f"suppressed={sorted(suppressed)} over {expected} — refusing rather than "
            "shipping a silently-short block that reads like a healthy default system",
            field="solves_partition", intended=expected,
            actual=sorted(buckets), surface=surface,
        )


def _unreadable_block():
    """The block for a surface whose solve state DID NOT REACH THE CALLER. FRESH per call.

    AN EXTERNAL LOW: the summary line used to read "the block for a surface NOTHING
    could be read from", which the paragraph below then spent itself contradicting. A
    correction that leaves the false HEADLINE in place is the one a reader takes away,
    because a summary line is what a reader reads. Fixed at the headline, once.

    It is deliberately the SAME shape a wedged real surface produces — the emit body
    freezes 0..4 top-level keys and adding a fifth for this case would move a contract
    the read path consumes, for a distinction the caller cannot act on differently.

    THE TWO KEYS ARE NOT UNIFORMLY LITERAL, and an earlier revision of this text said they
    were (corrected here, not renumbered). On the INVALID-INDEX path both really are
    true: no cell was read and no row was inspected. But the read path wired this same
    block as the CONSUMER-side substitute for a WHOLE-BLOCK assembly fault —
    ``_assert_exhaustive_partition`` or ``assert_wire_safe`` raising AFTER every cell
    was read successfully — where "no cell
    was read" is FALSE. The two provenances are indistinguishable BY DESIGN, so the
    honest reading of ``solves_unreadable`` is "this cell's solve state did not reach you",
    never "this cell's fetch failed". No consumer may infer the latter.
    """
    return {"solves_unreadable": sorted(CELL_TOKENS),
            "par_cell_solves_not_audited": True}


def emit_solves_block(system, lde, surface):
    """The solve PATCH for one surface: 0..4 top-level keys, FROZEN order, all additive.

    Its consumers are ``lens_surface._read_surface_dict`` and
    ``lens_describe._describe_one_surface`` — the two frozen insertion points, now
    wired (the earlier "unwired" tense is spent — the call-site ledger declares the
    live count as EXACTLY 2 and reddens if a third appears).

    CONFIG-BLIND, and it says so: the read is of the CURRENT configuration only, which
    is why ``mce_overrides_not_audited`` exists. The two eventual callers already differ
    on config handling (``describe_surfaces`` is in ``_CONFIG_REFUSES``, ``read_surface``
    is in neither set), so the block states its own scope rather than inheriting theirs.

    Every returned structure is FRESH per call — no module-level table is ever handed to
    a caller (the ``_CONFIG_ALL_SCHEMA`` aliasing LOW and the composite ``param_types``
    footgun are the two in-repo precedents).

    This function NEVER calls ``SetSolveData``. The test fake
    carries a shipped read-only SPY (``row._semi_solve_writes`` must stay ``[]``) and the
    contract test asserts it against this body.

    IT VALIDATES ITS SURFACE INDEX, and the earlier decision not to is REVERSED
    because the evidence cited for it was falsified. That decision rested on the shipped
    far-out-of-range control, which asserted live "already fails CLOSED" — every cell
    unreadable plus the disclosure key. The live gate measured the opposite:
    ``GetSurfaceAt(10**9)`` returns a NON-None **dud row proxy** whose ``.Type`` reads
    ``"Standard"`` and whose five solve cells all read clean DEFAULTS, so every token is
    classified *suppressed*, nothing reaches *unreadable*, and the block comes back
    ``{}`` — BYTE-IDENTICAL to a healthy all-default surface. That is a live silent-wrong:
    a confident, clean, empty solve block attributed to a surface that does not exist.
    Live also silently accepts a ``bool`` (``True`` means surface 1). The offline fake
    raises on all of it, so NO offline test could ever have seen the difference.

    THE PREDICATES ARE THE WRITER'S OWN, NOT A RE-DERIVED BOUNDS CHECK: the exact
    two this function's sibling ``refuse_if_driven`` already uses. The asymmetry between
    the siblings WAS the defect — ``bool`` is an ``int`` subclass, so a hand-rolled
    ``isinstance(surface, int)`` admits ``True``, and ``_require_read_index`` (not
    ``_require_geometry_index``) is what keeps surface 0 reachable for the
    finite-conjugate build.

    THE REFUSAL SHAPE IS THE EXISTING DISCLOSURE, not a new key: all five cells
    ``solves_unreadable`` plus ``par_cell_solves_not_audited``. Both statements are TRUE
    of an invalid index — nothing was read and no row was inspected — and the frozen
    0..4-key contract is untouched, so its consumers need no change.

    A CORRECTION: the sentence that followed — "ZERO user-visible change today: this
    function still has no production call site" — WAS true when written and is now FALSE.
    This function is wired at BOTH frozen insertion points (``lens_surface._read_surface_dict``
    and ``lens_describe._describe_one_surface``); the call-site ledger declares the count
    as EXACTLY 2 and reddens if it moves. The silent-wrong this docstring describes was
    closed BEFORE that wiring, which was the point of closing it in the substrate.
    """
    from ._lens_common import _require_read_index
    from ._tol_cells import is_integral_int

    if not is_integral_int(surface):
        return _unreadable_block()
    try:
        _require_read_index(surface, int(lde.NumberOfSurfaces))
    except Exception:  # noqa: BLE001 — an unusable index is DISCLOSED, never raised
        return _unreadable_block()

    solves, unreadable, suppressed = {}, [], []
    for token in CELL_TOKENS:
        try:
            cell = solve_cell(system, lde, surface, token)
        except Exception:  # noqa: BLE001 — a per-cell fetch fault is DISCLOSED per cell
            unreadable.append(token)
            continue
        type_name = read_solve_type(cell)
        if type_name is None:
            unreadable.append(token)
            continue
        if is_default_solve(token, type_name):
            suppressed.append(token)
            continue
        tag, fields = lookup(token, type_name)
        entry = {"type": type_name,
                 "fields": None if tag == "unmeasured" else sorted(fields)}
        if tag == "unmeasured":
            entry["fields_unmeasured"] = True
        solves[token] = entry

    _assert_exhaustive_partition(list(solves), unreadable, suppressed, surface)

    out = {}
    # (1) KEY ABSENT ENTIRELY when nothing is non-default — never
    #     ``"solves": {}``, which is different wire bytes and different dict-== behaviour.
    if solves:
        out["solves"] = solves
    # (2) PER-CELL, not a scalar: a scalar cannot carry a five-cell enumeration and lets
    #     a swallowed per-cell fault ship a false clean.
    if unreadable:
        out["solves_unreadable"] = sorted(unreadable)
    try:
        row = lde.GetSurfaceAt(surface)
    except Exception:  # noqa: BLE001 — an unreadable row -> disclose (fail-OPEN)
        row = None
    if row is None or _par_cell_row(row):
        out["par_cell_solves_not_audited"] = True
    if _mce_overridden(system):
        out["mce_overrides_not_audited"] = True
    assert_wire_safe(out)
    return out
