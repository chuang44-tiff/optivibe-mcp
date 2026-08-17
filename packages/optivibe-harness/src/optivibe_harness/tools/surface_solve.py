"""tools/surface_solve.py — the surface-SOLVE authoring door.

TWO dispatchable tools over the shared solve substrate:

- ``set_solve`` — author a RELATIONSHIP solve (a pickup, a ray-height/angle solve, an
  element-power solve...) on one of the five geometry cells, validated against that
  cell's OWN live legal set and proven by read-back.
- ``clear_solve`` — remove the solve, targeting each cell's OWN engine default
  (``Fixed`` on radius/thickness/conic/material, ``Automatic`` on ``semi_diameter``).
  It FREEZES the cell at its current value; it does not restore a default or an earlier
  number.

BOTH RAISE; SUCCESS RETURNS A DICT. That is the opposite polarity to the never-raise
envelope tools, and it is deliberate: it makes the double-envelope trap (a clean
outer ``ok: true`` wrapping an inner ``ok: false``) structurally impossible for these
two — for THESE tools the dispatch ``ok`` IS the truth. Dispatch classifies the raise
into ``tool_param`` / ``surface_write`` / ``solve_partial_state``.

WHAT ``set_solve`` DELIBERATELY DOES NOT AUTHOR, and why it is ROUTING rather than
capability (the five ``_NOT_AUTHORED_HERE`` tokens): ``Variable`` belongs to
``set_variable``, which owns the ``replace_solve`` override and the DOF-count semantics;
``Fixed`` and ``None`` belong to ``clear_solve``, because reaching ``Fixed`` has a
FREEZE consequence a caller who typed ``solve_type="Fixed"`` did not ask to be told
about; ``Automatic`` is ``semi_diameter``'s engine default, so reaching it IS clearing;
``ZPLMacro`` is refused on the type name alone (its ``Macro`` field names a file this
harness cannot validate). NOTE the reroute is a PUBLIC-PARAM gate only — ``clear_solve``
authors ``Fixed``/``Automatic`` through the same internal primitive, which sits BELOW it.

THE THREE PROOFS, and they decide different things:
  * **T1 (type)** — MANDATORY, a GATE. Re-fetches BOTH row and cell and compares
    enum MEMBERS. It is load-bearing, not belt-and-braces: in the measured refused-MCE
    arm three return values lied in one chain (``MakeSolveVariable`` -> True,
    ``CreateSolveType`` -> a non-None empty solve, ``SetSolveData`` -> ``Success``) with
    the cell unchanged, and ONLY the type read-back caught it.
  * **T2 (relation)** — MANDATORY for the pickup family, a GATE, computed from the
    CALLER'S STATED INTENT rather than from the read-back. TWO measured POST-mutation
    silent-coercions are invisible to every read-back-derived check and visible only to
    this one; see ``_t2``. A THIRD measured coercion — a NEGATIVE ``ScaleFactor`` on a
    ``semi_diameter`` pickup, which the engine discards — is owned by a DIFFERENT LAYER
    and never reaches T2: ``_require_pickup_fields`` REFUSES it PRE-mutation. That
    refusal is NOT redundant with T2 and deleting it is NOT covered by T2 — at a zero or
    sub-``abs_tol`` source the intent and the engine COINCIDE (``-0.0`` vs ``0.0``), so
    T2 would report ``verified`` over a relationship whose sign was thrown away.
  * **T3 (fields)** — DISCLOSURE ONLY, never a gate.

THE TRANSACTION IS CELL-LOCAL: capture the incumbent solve BEFORE authoring, and on any
POST-invoke failure restore it and PROVE the restoration. There is no ``SaveAs``
checkpoint (~87 ms/call, repoints ``SystemFile`` and un-blesses tolerancing, writes
a ``.ZDA``). A solve whose authoring perturbs OTHER cells or the downstream system
is NOT undone by this restore.

Live ZOS-API integration is covered by a live integration test.
"""
import math

from ..enums import _resolve_enum
from ..errors import (
    PARTIAL_STATE_ATTR,
    SolvePartialStateError,
    SurfaceWriteError,
    ToolParamError,
)
from ..server import ToolSpec
from . import _cb_cells as _cb
from . import _lens_common as _lc
from . import _solve_cells as _sc
from ._tol_cells import is_integral_int

#: The five tokens ``set_solve`` REROUTES rather than authors. ORDERED, one constant.
_NOT_AUTHORED_HERE = ("Variable", "Fixed", "None", "Automatic", "ZPLMacro")

#: Solve fields whose value is a SURFACE INDEX (integral, in ``0..N-1``).
#:
#: THE VOCABULARY MOVED TO ``_solve_cells.INDEX_FIELDS`` — it now
#: has a second consumer (``_solve_refs``'s removal scan, which intersects it with
#: ``_MEASURED``), and a leaf helper importing a tool module's PRIVATE name would make a
#: cross-module contract out of it. This alias keeps the one existing consumer
#: (``_require_index_field_value``) reading the same names it always did, at net zero
#: statements here.
_INDEX_FIELDS = _sc.INDEX_FIELDS

#: The one field whose value is a ``SurfaceColumn`` MEMBER rather than a scalar.
_COLUMN_FIELD = "Column"

#: Read-back field NAMES whose value is not wire-safe as read and must be canonicalised
#: before it leaves the harness. MEASURED, not guessed — see ``_sc.read_solve_fields``.
#:
#: A FROZENSET AND NOT A LITERAL ``"Column"`` TEST, because the consumers are plural and
#: the rule must extend to all of them at once. A parametrised test drives
#: every consumer PARAMETRISED OVER THIS SET, so adding a second name here automatically
#: extends the binding to all three sites rather than to whichever one the next author
#: remembers.
WIRE_COERCED_FIELDS = frozenset({"Column"})


def to_wire(name, value):
    """The ONE coercion from a ``_sc.read_solve_fields`` value to a wire-safe one.

    The operative half of the ``Column`` wire-value fix. The reported defect is a
    disagreement between two readers, but the MEASUREMENT turned up the sharper defect
    behind it: the ``Column`` canonicalisation was written out THREE separate times in
    ``surface_solve.py`` — in ``_render_field`` (the restore drift compare), in ``_t3``
    (twice, once for the read-back value and once for the requested one) and in
    ``_replaced_fields`` — with nothing binding them. That is the defining drift
    defect exactly: one rule, N sites, and a fix that lands on some of them. Three copies
    is how the fourth consumer gets written without one.

    WHY ``str`` AND NOT ``int``. Measured live on all five ``SurfacePickup`` pairs:
    ``int(Column)`` SUCCEEDS and returns the ORDINAL — and a DIFFERENT ordinal per cell
    (Radius 2, Thickness 3, Material 4, SemiDiameter 6, Conic 9) — so a coercion reflex
    emits a plausible integer in place of the token. ``str(Column)`` renders the clean
    token (``"Radius"``, ``"Thickness"``, …), which is also the vocabulary ``set_solve``
    ACCEPTS, so the value round-trips. The repr carries no ``" at 0x<hex>"``, so the MCP
    wire's address scrub would not fire and would ship ``"<SurfaceColumn.Radius: 2>"``.

    IT IS NOT ARITHMETIC. One named field, one ``str()``. A ``None`` passes through
    untouched: ``_sc.read_solve_fields`` emits ``None`` for a value it could not read or could
    not represent, and ``str(None)`` would manufacture the literal ``"None"`` — a readable
    token where the honest answer is "no value", which is the ABSENT-vs-UNREADABLE
    collapse one layer down.
    """
    if value is None or name not in WIRE_COERCED_FIELDS:
        return value
    return str(value)


#: The four fields a ``SurfacePickup`` requires EXPLICITLY (V10). All four, always: that
#: is what dissolves the same-type-re-author inheritance ambiguity (an omitted
#: ``ScaleFactor`` would INHERIT the live one, so an assumed default of 1.0 would compute
#: the wrong expectation and refuse a correct re-author).
_PICKUP_FIELDS = ("Surface", _COLUMN_FIELD, "ScaleFactor", "Offset")

#: T2's tolerance. Defined ONCE, here. Deliberately NOT ``_readback_ok`` — six copies of
#: that predicate already exist and a seventh is the anti-pattern; the solve checks
#: consume none of them (the token is asserted ABSENT from this module).
_T2_REL_TOL = 1e-9
_T2_ABS_TOL = 1e-9


def _is_finite_number(value):
    """A REAL, finite, non-``bool`` number — and the CONVERSION cannot escape.

    ONE predicate, consumed by V10 and V12 alike, because the two had independently
    written ``isinstance(...) and math.isfinite(value)`` and BOTH inherited the same
    defect: ``math.isfinite`` RAISES ``OverflowError`` on an ``int`` too large for a
    ``float`` (``10**400`` arrives intact through JSON), so an ORDINARY BAD INPUT escaped
    a pre-mutation validator as an unstructured exception and dispatch classified it
    ``internal`` — an infrastructure-looking failure for a validation case. A too-large
    integer is not a finite number, so the honest answer is ``False``: the caller gets
    the same named ``tool_param`` refusal every other bad number gets.

    The same shape is already handled one layer down in ``_solve_cells.read_solve_fields``
    (its fix); this is that lesson landing on the two validators that never got it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except Exception:  # noqa: BLE001 — OverflowError et al: not a finite number
        return False


def _safe_repr(value):
    """An EXACT ``str`` rendering of ``value``, or a placeholder naming the fault CLASS.

    ONE helper, not a per-site fix, and that is the precedent landing here: a
    ``__bool__``-raising .NET proxy escaped a guard there and the per-site repair bred a
    sibling defect; one guarded helper closed the class. ``"%r" % (exc,)`` EXECUTES
    ``__repr__`` **while the never-raise return value is being built**, so a hostile repr
    escapes ``_solve_trace.relation_for`` — whose never-raise contract is a CORRECTNESS
    property, because ``_transact`` treats any post-invoke exception as a failure and
    RESTORES. A string that could not be formatted would roll back a CORRECT write.

    THE TERMINATION CRITERION, and it is what makes this helper a guard rather than a
    hopeful one: **its return is an EXACT ``str`` BY CONSTRUCTION on every rung.** Grading
    a candidate shape by enumerating hostile behaviours is unbounded; exactness is
    decidable. A first cut shipped a bare ``return repr(value)``, which fails it — Python
    permits ``__repr__`` to return a ``str`` SUBCLASS, so the guard handed a live hostile
    object to a caller and the caller's own ``%s`` raised OUTSIDE this ``try``. Measured
    end-to-end: the gated ``ChiefRayAngle`` mismatch came back ``ok: True`` /
    ``traced_unreadable`` with the bad solve STILL COMMITTED — i.e. it reopened the gate
    hole through the rendering of the very disclosure that shipped with the fix for it.

    ``str.__str__(...)`` IS THE NORMALISER, and the two obvious alternatives are REJECTED
    on the same criterion. ``str(x)`` dispatches to ``type(x).__str__``, which is
    overridable and whose return may itself be another subclass — MEASURED: a subclass
    whose ``__str__`` returns a second hostile subclass survives ``str()`` intact, so the
    audit's own suggested ``str(repr(value))`` does not close this. ``repr(value)[:]``
    dispatches to ``__getitem__``, equally overridable. A base-class slot call bypasses the
    MRO entirely, and **for a subtype CPython COPIES the buffer into a genuine ``str``**
    (``unicode_result_unchanged`` -> ``_PyUnicode_Copy``), invoking nothing overridable.
    Verified: ``type(str.__str__(hostile_subclass)) is str``. DO NOT "simplify" this to
    ``str(...)`` — that is the exact edit that reintroduces the HIGH.

    THE FALLBACK CARRIES THE TYPE NAME, retrieved WITHOUT INVOKING THE HOSTILE OBJECT
    (``type(value).__name__`` touches the class, never the instance), so the reason still
    names the fault CLASS: information is lost only where it is genuinely unretrievable.
    Both fallback rungs satisfy the criterion too — ``str.__mod__`` over a literal template
    builds a NEW exact ``str`` regardless of what the interpolated object is, and rung 3 is
    a literal.

    THE SECOND ``try`` IS NOT DEFENSIVE PADDING, and that was MEASURED rather than
    argued. This helper is called from inside the outer net's own ``except`` handler,
    and an exception raised THERE escapes the net entirely — i.e. it would reintroduce the
    exact rollback this whole change exists to stop. It is ALREADY TOTAL against the
    hostile-METACLASS shape (a ``__name__`` property that raises, and a ``__name__`` that
    returns a hostile subclass whose ``%s`` then raises): both were driven at this function
    and both were caught here and returned rung 3's literal, exact-``str``. A review
    advisory to flatten this ladder to a bare literal was therefore DECLINED — it would
    have deleted a working diagnostic to close a hole that measurement shows is not open.

    ``Exception`` and NOT ``BaseException``, so an abort travels unchanged: this is
    envelope construction for an already-caught fault, and a ``KeyboardInterrupt`` from
    a pathological ``__repr__`` must still propagate on the abort path.
    """
    try:
        return str.__str__(repr(value))
    except Exception:  # noqa: BLE001 — a hostile __repr__ must never escape
        try:
            return "<a %s whose repr() raised>" % (type(value).__name__,)
        except Exception:  # noqa: BLE001 — even the class name was unreachable
            return "<a value whose repr() and type name both raised>"


#: Cell families MEASURED to mutate during a pre-``SetSolveData`` step, which therefore
#: need the conservative restore mark before ``CreateSolveType``.
#:
#: EMPTY BY MEASUREMENT, not by omission. All five families were re-run
#: through five transition arms — create-a-different-type, create-the-SAME-type
#: then mutate the detached object, ``UpdateStatus``, ``del`` + GC, and full abandonment
#: — with ``changed_keys`` computed from FRESHLY FETCHED row and cell over solve type,
#: fields AND cell value. The union is ``[]`` on every family, and the positive
#: control (commit the aged detached object) FIRED on every family, so the empty result
#: is a measurement rather than a dead probe. The branch below is therefore
#: present-but-never-taken at this engine, and is kept because the claim is a probe
#: result and not a theorem.
_CONSERVATIVE_FAMILIES = ()

#: The four non-``Variable`` reroute messages. Every one names the DOOR and, via
#: ``_refuse_type``, the cell's real live legal set — the cold-reader lesson: a rule
#: served without its domain made a LEGAL write undecidable, and the reader refused it.
_REROUTE = {
    "Fixed": (
        "set_solve does not author a Fixed solve: 'Fixed' is what a cell carries when "
        "nothing drives it. To remove the solve on this cell, call "
        "clear_solve(surface=%(n)s, cell='%(t)s') — it FREEZES the cell at its CURRENT "
        "value; it does not restore a default or an earlier number."),
    "None": (
        "'None' is the engine's own name for 'no solve', so set_solve — which authors a "
        "solve — has nothing to author. To remove the solve on this cell, call "
        "clear_solve(surface=%(n)s, cell='%(t)s')."),
    "Automatic": (
        "'Automatic' is the engine-default floating state (the semi_diameter default). "
        "To return a semi_diameter to it, call clear_solve(surface=%(n)s, "
        "cell='semi_diameter') — its semi arm re-floats the aperture, exactly as "
        "freeze_semidiameters(mode='auto') does. On other cells Automatic is not an "
        "authorable relationship."),
    "ZPLMacro": (
        "ZPLMacro is refused on the type name alone: its behaviour is entirely "
        "unmeasured and its Macro field names a file this harness cannot validate. No "
        "macro was executed; nothing was written."),
}

#: The ``Variable`` reroute, DOUBLE-cell arm — a real door exists.
_VARIABLE_DOUBLE = (
    "set_solve does not author a Variable solve: a Variable is the optimizer's degree of "
    "freedom, not a relationship, and it belongs to a different door. Use "
    "set_variable(surface=%(n)s, cell='%(t)s') — it refuses a cell already driven by a "
    "solve, and takes replace_solve=true to replace one deliberately.")

#: The ``Variable`` reroute, NON-Double arm — and it is LOAD-BEARING, not a nicety.
#: Without it the Double message would send ``cell='material'`` to a door that refuses
#: ``cell='material'`` outright: a dead-end remedy, which is inadmissible in every
#: branch. It therefore says plainly that NO door exists.
_VARIABLE_OTHER = (
    "Variable is not authorable on the '%(t)s' cell of surface %(n)s: its DataType is "
    "'%(dt)s', not 'Double'. Only a Double cell is a real optimizer degree of freedom — "
    "the engine can OFFER Variable on a non-Double cell and then silently refuse the "
    "write while reporting success (measured), or count a nonsensical DOF (measured). "
    "set_variable does not accept cell='%(t)s' either, so there is no door for this "
    "request.")


# --------------------------------------------------------------------------- #
# Domain refusals — every one names the LIVE domain.
# --------------------------------------------------------------------------- #
def _accepted_here(legal):
    """The subset of THIS cell's live legal set that ``set_solve`` will author."""
    return [n for n in legal if n not in _NOT_AUTHORED_HERE]


def _refuse_type(message, legal):
    """Raise a type-domain refusal, always suffixed with the LIVE accepted set."""
    raise ToolParamError(
        "%s set_solve accepts these solve types on this cell: %s."
        % (message, _accepted_here(legal)))


def _reroute(canonical, cell, surface, token, legal):
    """The five ``_NOT_AUTHORED_HERE`` refusals. Always pre-mutation, zero engine write.

    The ``Variable`` branch reads ``cell.DataType`` THROUGH A GUARD and treats an
    unreadable DataType as non-Double — the conservative direction, because the
    non-Double message is the one that promises no door rather than the one that names
    a door which might then refuse.
    """
    dtype = ""
    if canonical == "Variable":
        try:
            dtype = str(cell.DataType)
        except Exception:  # noqa: BLE001 — unreadable DataType -> the no-door arm
            dtype = "unknown"
        template = _VARIABLE_DOUBLE if dtype == "Double" else _VARIABLE_OTHER
    else:
        template = _REROUTE[canonical]
    _refuse_type(template % {"n": surface, "t": token, "dt": dtype}, legal)


# --------------------------------------------------------------------------- #
# V5 — the tagged legal-set read, and its THREE distinct failure arms.
# --------------------------------------------------------------------------- #
def _legal_or_refuse(cell, surface, token):
    """The cell's legal solve set, or a refusal that says WHICH kind of failure it was.

    ALL THREE failure arms are DEFENSIVE — none has a live reproduction (a live probe
    deliberately attempting all three produced none of EMPTY_LIST / THROW /
    MISSING_METHOD, and the 39-row legal-type sweep produced none either). They ship
    because authoring against an engine that cannot report its own legal set is
    reckless, not because the shapes were observed.

    HONEST LIMIT: ``absent`` and ``unreadable`` are distinguished on the wire
    by MESSAGE only — a raise carries a family and a message and nothing else.
    """
    tag, names = _sc.available_solve_names(cell)
    if tag == "absent":
        raise SurfaceWriteError(
            "the engine build on this session does not expose GetAvailableSolveTypes on "
            "the '%s' cell of surface %s, so the legal solve set cannot be read; "
            "refusing rather than authoring blind. This is an engine-drift signal, not "
            "a problem with your design." % (token, surface),
            field=token, intended="a readable legal solve set", actual=None,
            surface=surface)
    if tag == "unreadable":
        raise SurfaceWriteError(
            "the legal solve set of the '%s' cell of surface %s could not be read; "
            "refusing rather than authoring blind. A PARTIAL prefix was recovered (%r) "
            "and is shown for diagnosis ONLY — a type absent from a truncated list is "
            "not a type the cell refuses, so it is never membership-tested."
            % (token, surface, list(names)),
            field=token, intended="a readable legal solve set", actual=None,
            surface=surface)
    if tag == "empty":
        raise ToolParamError(
            "the '%s' cell of surface %s reports an EMPTY legal solve set, so there is "
            "no solve type this tool could author on it; nothing was written."
            % (token, surface))
    return list(names)


def _refuse_if_par_cell(label, value):
    """THE ONE AUTHORITY over "this names a Par cell" — the predicate AND the message.

    MEASURED AT THE FINAL GATE. A live end-to-end run measured the corrected refusal
    UNREACHABLE through the door callers actually use. An earlier round widened
    ``_resolve_column``'s ``Column`` gate to the CB parameter tokens, and the gate then
    re-measured that ``_prelude``'s ``cell=`` door reached the corrected text for NO
    spelling at all — not for ``decenter_y``, and not for ``Par1`` either. Both doors now
    consume this function, so the two spellings and the two doors give the SAME answer.

    ONE AUTHORITY, NOT TWO, and the alternatives were rejected on that ground rather than
    on cost. Copying the ``if`` into ``_prelude`` would have been +2 and would have made
    this module carry two texts that must be edited together forever; the four-token
    literal considered earlier would have made it a second authority over the CB
    vocabulary. ``_cb_cells._PARAM_NAMES`` is CONSUMED (it is in that module's
    ``__all__``) and is never transcribed here — the identity-not-copy doctrine.

    THE MESSAGE IS TOOL-AGNOSTIC BECAUSE ``_prelude`` SERVES TWO TOOLS. ``set_solve`` and
    ``clear_solve`` share that door, so a text naming one of them would be false at the
    other; ``label`` names the PARAMETER the caller actually typed (``Column`` or
    ``cell``), which is the part a caller needs to act on.

    A NON-STRING VALUE FALLS THROUGH RATHER THAN RAISING HERE, and that is deliberate:
    ``_prelude``'s ``cell`` comes straight off the wire and may be ``None``, an ``int``,
    or an unhashable ``dict``. Everything non-``str`` is left to the caller's own shape
    gate, which is the function that owns that refusal.

    THE PREDICATE RUNS ON THE RAW VALUE, NOT ON ``TOKEN_TO_COLUMN.get(value, value)``,
    AND THAT IS THE SAME GUARD IN A CHEAPER SHAPE — MEASURED, not assumed. The mapping is
    a NO-OP for this question: it maps only the five geometry tokens to the five
    ``SurfaceColumn`` members, and NONE of those ten strings starts with ``Par`` or
    appears in ``_PARAM_NAMES``, so the predicate returns the same answer either side of
    it for every value in the whole vocabulary; for anything else ``.get`` is identity.
    A test pins that equivalence over the union of both vocabularies rather than
    leaving it to this paragraph. Dropping the mapping ALSO removes the ``TypeError`` an
    unhashable ``dict`` would have raised inside ``.get``, so the cheaper shape is
    strictly safer at the wire-facing door.

    THE STATEMENT WAS RECOVERED THIS WAY AND NOT THE OTHER WAYS. The approved statement
    ceiling allowed four more and the first cut measured one over, because a docstring is an
    ``ast.Expr`` and the +4 estimate had not counted it. It was NOT recovered by deleting
    this docstring, by inlining the ``if`` into both callers (two texts to edit forever),
    or by re-opening the ceiling — precedent in the suite is that a
    one-over cut is resolved by finding the same guard in a cheaper shape.

    THE TWO VOCABULARIES MUST STAY DISJOINT — pinned by a test row, because both arms
    of each caller terminate in a refusal and nothing downstream forks on which fired, so
    a collision would produce a wrong EXPLANATION with undefined precedence.

    THE MATCH IS CASE-INSENSITIVE, AND THE 0.1.6 RELEASE DOGFOOD IS WHY. Round 6 closed
    step 6b by making both doors consume this function, and the release dogfood then
    measured that the corrected text still reached only SOME spellings: ``Par1`` and
    ``decenter_x`` got it, while ``par1`` and ``PAR1`` fell through to the generic
    five-cells message. The missed spelling is the one a caller will actually type —
    every cell token this door accepts is lowercase (``radius``, ``thickness``,
    ``conic``, ``semi_diameter``, ``material``), so lowercase IS the house style and
    ``par1`` was the majority case losing the guidance. Same defect class as round 6's:
    the fix landed at one spelling and not its siblings.

    IT COSTS ZERO STATEMENTS — the predicate was already one expression, so no second
    ceiling review was owed. The disjointness above SURVIVES case-folding and that is
    checked, not assumed: none of the five geometry tokens, and none of the five
    ``SurfaceColumn`` members they map to, lowercases to something beginning ``par`` or
    colliding with ``_PARAM_NAMES``. ``Decenter X`` (the Header spelling, with a space)
    is deliberately still NOT matched — folding case is not the same as admitting a new
    token form, and widening the vocabulary is what would put the disjointness pin at
    risk.
    """
    if isinstance(value, str) and (value[:3].lower() == "par"
                                   or value.lower() in _cb._PARAM_NAMES):
        raise ToolParamError(
            "%s=%r addresses a Par cell. The solve-authoring door's domain is the five "
            "geometry cells %s. A CB Par cell DOES accept a PickupChiefRay solve: "
            "measured LEGAL on all four of decenter_x / decenter_y / tilt_x / tilt_y "
            "(absent from tilt_z and order), and measured DRIVING on decenter_y and "
            "tilt_x. On decenter_x and tilt_y the solve landed with Success but produced "
            "NO MOTION under all three meridional perturbations, so drive there is "
            "UNPROVEN — the probe cannot distinguish 'correctly zero' from inert. Either "
            "way this harness has no Par catalog, no Par default table and therefore no "
            "rollback for one, so it will not author it."
            % (label, value, list(_sc.CELL_TOKENS)))


def _prelude(session, params):
    """V1 shape + V2 range + V3 fetch + V5 legal set. READS ONLY; nothing is written.

    V2 uses ``_require_read_index`` (``0 <= surface <= N-1``), NOT
    ``_require_geometry_index``: surface 0 stays reachable, because the engine's own
    legal set is what decides what the OBJECT surface offers — never a hand-rolled index
    policy. ``is_integral_int`` is the WRITER's own predicate rather than a re-derived
    ``isinstance(surface, int)``: ``bool`` is an ``int`` subclass, so the naive guard
    admits ``True`` and silently addresses surface 1.
    """
    if not isinstance(params, dict):
        raise ToolParamError("params must be an object")
    surface = params.get("surface")
    token = params.get("cell")
    if not is_integral_int(surface):
        raise ToolParamError(
            "surface must be an exact integer index; got %r" % (surface,))
    _refuse_if_par_cell("cell", token)
    if token not in _sc.CELL_TOKENS:
        raise ToolParamError(
            "cell must be one of %s; got %r" % (list(_sc.CELL_TOKENS), token))
    system = session.system
    lde = system.LDE
    surface = int(surface)
    _lc._require_read_index(surface, int(lde.NumberOfSurfaces))
    cell = _sc.solve_cell(system, lde, surface, token)
    return system, lde, surface, token, cell, _legal_or_refuse(cell, surface, token)


# --------------------------------------------------------------------------- #
# THE SHARED AUTHORING PRIMITIVE (the seam ``cb_surface`` delegates into).
# --------------------------------------------------------------------------- #
def author_solve(system, cell, member, field_writes, *, surface=None, cell_label=None,
                 on_mutate=None, none_create_error):
    """create -> null-check -> unwrap -> live-kind-checked field writes -> bind -> mark
    -> invoke -> status. RAISES typed errors; it does NOT read back.

    THE CALLERS OWN T1/T2/T3 and the TRANSACTION. Their proof obligations genuinely
    differ (``add_return_cb`` proves ``return == -entry``; ``set_solve`` proves a closed
    form), and capture/restore is a decision about the caller's own contract.

    ``on_mutate`` IS THE ONE MUTATION-OBSERVATION CHANNEL ACROSS THIS SEAM. Without it
    the transaction is unimplementable: the handler cannot otherwise observe the invoke
    boundary, which is hidden inside this function. It is invoked EXACTLY ONCE, AFTER the
    ``SetSolveData`` BIND and immediately BEFORE the bound invocation, inside the ``try``.

    WHY THE BIND COMES FIRST, and it is not style. Python resolves the member BEFORE any
    call, so a shape that flips the flag and then writes ``cell.SetSolveData(solve)``
    has a window in which a member-lookup failure (a dead or stale proxy) sets the flag
    with the method never entered — an unenumerated PRE-entry exit that manufactures a
    restore, and possibly a ``solve_partial_state``, out of a zero-commit failure.
    Binding first makes that lookup its own enumerated pre-mutation exit.

    ``none_create_error`` is REQUIRED and has NO DEFAULT, because this function cannot
    know whose fault a ``None`` is: on ``set_solve`` the type came from the CALLER
    (``tool_param`` — the legal list lied), on ``clear_solve`` and the CB delegate it is
    HARNESS-chosen (``surface_write`` — engine drift). A default would let a fourth
    caller INHERIT a family instead of deciding one, and no handler ever catches and
    remaps a family back across this seam.
    """
    try:
        solve = cell.CreateSolveType(member)
    except Exception as exc:  # noqa: BLE001 — pre-mutation
        raise SurfaceWriteError(
            "CreateSolveType(%s) raised on the '%s' cell of surface %s (%r); nothing "
            "was written" % (member, cell_label, surface, exc),
            field=cell_label, intended=str(member), actual=None,
            surface=surface) from exc
    if solve is None:
        raise none_create_error()
    view = _sc.unwrap_or_raise(solve, surface=surface, cell_token=cell_label)
    _check_live_kinds(view, field_writes, surface, cell_label, member)
    for name, value in field_writes.items():
        try:
            setattr(view, name, value)
        except Exception as exc:  # noqa: BLE001 — lands on the DETACHED object
            raise SurfaceWriteError(
                "writing the %r field of the %s solve raised (%r); nothing was "
                "committed to surface %s" % (name, member, exc, surface),
                field=cell_label, intended=name, actual=None, surface=surface) from exc
    try:
        set_solve_data = cell.SetSolveData
    except Exception as exc:  # noqa: BLE001 — a PRE-entry lookup failure
        raise SurfaceWriteError(
            "the SetSolveData member could not be resolved on the '%s' cell of surface "
            "%s (%r) — the call was never entered and nothing was written"
            % (cell_label, surface, exc),
            field=cell_label, intended="SetSolveData", actual=None,
            surface=surface) from exc
    if on_mutate is not None:
        on_mutate()
    try:
        status = str(set_solve_data(solve))
    except Exception as exc:  # noqa: BLE001 — the call may have committed
        raise SurfaceWriteError(
            "SetSolveData raised on the '%s' cell of surface %s (%r)"
            % (cell_label, surface, exc),
            field=cell_label, intended=str(member), actual=None,
            surface=surface) from exc
    if status != "Success":
        # THE POST-HOC STOP DIAGNOSIS, and it is post-hoc on purpose. A PRE-gate would
        # refuse outside a band nobody has shown the engine rejects: every measured sweep
        # iterated ``range(..., n-1)``, so the IMAGE surface was never attempted and
        # ``stop <= surface <= N-2`` is a limit of the MEASUREMENT. This fires only on a
        # write the engine has ALREADY refused, so it cannot be wrong about whether the
        # write was allowed — and it is guarded end to end, because a decoration that
        # raises would replace the diagnosis it exists to enrich. It is scoped by
        # ``canonical``: ``SurfacePickup`` is absent from the eight, so the CB seam's
        # message stays byte-identical.
        hint = ""
        try:
            from . import _solve_trace as _st   # lazy: see the ``_t2`` cycle note

            hint = _st.stop_rule_hint(system.LDE, str(member), surface)
        except Exception:  # noqa: BLE001 — no hint is always better than a lost diagnosis
            pass
        raise SurfaceWriteError(
            "SetSolveData reported %r (not 'Success') authoring %s on the '%s' cell of "
            "surface %s%s" % (status, member, cell_label, surface, hint),
            field=cell_label, intended=str(member), actual=status, surface=surface)


def _check_live_kinds(view, field_writes, surface, token, member):
    """V12 — validate each supplied value against the LIVE kind on the DETACHED object.

    THE LIVE OBJECT IS THE ORACLE. No 26-name kind table exists here: a frozen
    table is a proxy that must be kept in sync, and a name-blind "refuse a bool" rule
    would refuse ``MaterialModel.VaryIndex``, which REQUIRES one.

    ``Column`` IS EXEMPT, and that exemption is exactly one name. Its live value is a raw
    ``SurfaceColumn`` proxy for which every ``isinstance`` int/str/float/bool is False,
    so the "unfamiliar kind -> refuse" arm would fire on it and a literal implementation
    would refuse EVERY pickup pre-mutation. ``Column`` is already FULLY validated
    upstream (a member NAME or a cell token, resolved getattr-only to the live MEMBER,
    which is the value written). The ``_INDEX_FIELDS`` pass through here UNEXEMPTED —
    their live kind is numeric and the upstream integral/bounds checks are additional,
    not a substitute. Generalising this to "anything validated upstream skips V12" is a
    widening and is forbidden.

    THE REFUSAL NAMES THE SOLVE TYPE. A first cut passed the literal ``"this"`` into the
    ``%s solve`` slot, so a served refusal read "the 'Angle' field of a **this** solve" —
    the message shipped to an agent, in the door whose whole job is to name the domain it
    is refusing against. ``member`` is the resolved live member the caller is authoring;
    it renders through ``str()`` (the wire rule), never ``repr``.
    """
    for name, value in field_writes.items():
        if name == _COLUMN_FIELD:
            continue
        try:
            live = getattr(view, name)
        except Exception as exc:  # noqa: BLE001 — an unreadable field is pre-mutation
            raise ToolParamError(
                "the %r field of this solve could not be read to classify its type "
                "(%r); refusing rather than writing an unvalidated value" % (name, exc))
        if isinstance(live, bool):
            ok = isinstance(value, bool)
        elif isinstance(live, (int, float)):
            ok = _is_finite_number(value)
        elif isinstance(live, str):
            ok = isinstance(value, str)
        else:
            ok = False
        if not ok:
            raise ToolParamError(
                "the %r field of a %s solve on the '%s' cell of surface %s takes a %s; "
                "got %r (%s). Nothing was written."
                % (name, member, token, surface, type(live).__name__, value,
                   type(value).__name__))


# --------------------------------------------------------------------------- #
# CAPTURE. NO CAPTURE, NO AUTHOR.
# --------------------------------------------------------------------------- #
def _read_value_tagged(row, token):
    """``(value, readable)`` — the cell's VALUE and whether the read SUCCEEDED.

    THE TAG IS THE POINT. ``None`` is a legitimate reading on this seam
    (a cell can genuinely hold nothing),
    so collapsing a FAULT to ``None`` makes "the glass is unchanged" and "the glass could
    not be read" the same wire value — and the shipped clear envelope then reported
    ``material: null`` beside ``glass_unchanged: true``, an out-of-contract success. It is
    the ABSENT-vs-UNREADABLE rule at value scale: ABSENT is not UNREADABLE, and only the
    second one is an
    alarm.

    CORRECTED BY LIVE MEASUREMENT. The parenthetical above used to read
    "``material`` on an air surface can read empty, a cell can genuinely hold nothing".
    **The first half was FALSE, and it was load-bearing** -- it was the stated origin of
    F-C's behavioural half (whether a legitimate null should be permitted rather than
    refused), which the 0.1.7 deferred on the grounds that its reachability
    "rests on a docstring, not a measurement". The 0.1.7 live gate took the measurement:

        _read_value_tagged(row, 'material')  ->  (value='', readable=True)
        type(value).__name__ == 'str'   value is None -> False   value == '' -> True
        CONTROL (glass surface): (value='N-BK7', readable=True)

    A real air surface's ``Material`` reads the empty STRING, not ``None``. Empty is not
    null, so this seam does not demonstrate the claim it was cited for, and ``_capture``'s
    ``value is None`` arm is NOT reachable by this route (it did not fire; the glass
    control proves the read path was live, so ``''`` is not a degraded read).
    The general claim is KEPT -- a cell can genuinely hold nothing, the tag is still the
    point, and this measures ONE route (an un-solved air material cell under a ``Fixed``
    solve). It does NOT prove no route yields ``None``.

    ONE literal: the row PROPERTY names and the ``SurfaceColumn`` MEMBER names
    coincide on all five geometry cells, so this consumes ``_sc.TOKEN_TO_COLUMN`` rather
    than opening a second table that could drift from it. The two codomains are
    conceptually different and a test pins the coincidence, so a future divergence
    reddens instead of silently reading the wrong property.
    """
    try:
        return getattr(row, _sc.TOKEN_TO_COLUMN[token]), True
    except Exception:  # noqa: BLE001 — an unreadable value is a fact, not a crash
        return None, False


def _read_value(row, token):
    """The VALUE half of ``_read_value_tagged``. ``None`` on a fault OR a null reading.

    KEPT as the one-statement delegate rather than deleted, for the callers whose
    decision genuinely does not depend on the difference — the capture (whose ``None``
    arm refuses for a non-driving prior either way) and the restore proof (which compares
    against that captured number). The callers that DO depend on it read the tagged pair;
    the fork lives at the caller, never behind a flag.
    """
    return _read_value_tagged(row, token)[0]


#: The second sentence of the no-restore-recipe refusal, PER DOOR.
#:
#: One refusal serves two callers whose REQUESTS are opposite, and a first cut shipped
#: only the authoring half: a caller who asked to REMOVE a solve was told the harness was
#: "refusing rather than authoring over it" — a sentence about a request nobody made —
#: and then given NO door at all. ``set_solve`` refuses ``ZPLMacro`` on the name alone, so
#: for that incumbent no authoring door exists either, and a dead-end remedy is ruled
#: INADMISSIBLE IN EVERY BRANCH by ``_VARIABLE_OTHER``'s own docstring. The clear arm
#: therefore says plainly that no tool here removes it and names the one door that DOES
#: change the state: reload a design without it.
_NO_RECIPE_DOOR = {
    "set_solve": ("Refusing BEFORE mutating anything rather than authoring over it. "
                  "Read `solves` on read_surface to see what is there."),
    "clear_solve": ("No tool in this harness removes this solve — set_solve refuses to "
                    "author over it for the same reason, so there is no door for this "
                    "request. Reload a design that does not carry it (load_design), or "
                    "remove it in OpticStudio. Nothing was written."),
}


def _capture(cell, row, token, surface, door="set_solve"):
    """The restore token and everything the restore needs. Every refusal is PRE-mutation.

    A captured solve OBJECT is a faithful restore token for type + fields + driven value
    and stays valid across arbitrary intervening work, so there is no per-field replay.

    AN UNREADABLE FIELD REFUSES HERE, PRE-MUTATION, and that placement is
    the whole fix. The restore proof can only compare what the capture SAW, so a field
    that read ``mismatched`` at capture time is one the proof must skip — and a skipped
    field is a field over which "restored and verified" is a claim about nothing. The
    audit's counterexample is exactly this shape: a ``ScaleFactor`` that faulted at
    capture and came back coerced still reported the relationship restored, because the
    type and the zero-valued target both legitimately agreed and the ONLY witness was the
    field that was never captured. Making it a refusal is the same rule the type and the
    value arms above already apply one step out — *you can only replace what you can
    see* — and it is decidable here, where nothing has been written yet, and decidable
    NOWHERE downstream, because after the mutation the pre-image is gone forever.

    THE ``prior_token is None`` CHECK IS AN IDENTITY CHECK AND IT IS LOAD-BEARING. A None
    token cannot be replayed, and ``SetSolveData(None)`` is the measured native
    ACCESS-VIOLATION path — it skips Python ``finally``, which is where the engine reap
    lives. Refusing here, pre-mutation, is what keeps that argument unreachable on the
    restore's replay arm.

    An unreadable incumbent is a REFUSAL, not a proceed: you can only replace what you
    can SEE and report back. A ``None``/``ZPLMacro``/uncatalogued prior refuses for the
    same reason one step further out — no measured restore recipe exists for it, so
    authoring over it would be a mutation this tool could not undo.
    """
    prior_type = _sc.read_solve_type(cell)
    if prior_type is None:
        raise SurfaceWriteError(
            "the current solve on the '%s' cell of surface %s could not be read, so it "
            "could not be captured — and we cannot restore what we cannot read. Read "
            "`solves` on read_surface: this cell will be listed under "
            "`solves_unreadable`. Nothing was written." % (token, surface),
            field=token, intended="a readable incumbent solve", actual=None,
            surface=surface)
    if prior_type in ("None", "ZPLMacro") or _sc.lookup(token, prior_type)[0] == "unmeasured":
        raise SurfaceWriteError(
            "the '%s' cell of surface %s currently carries a %s solve, for which this "
            "harness has no measured restore recipe — so a failed write could not be "
            "rolled back. %s"
            % (token, surface, prior_type, _NO_RECIPE_DOOR[door]),
            field=token, intended="a restorable incumbent solve", actual=prior_type,
            surface=surface)
    try:
        prior_token = cell.GetSolveData()
    except Exception as exc:  # noqa: BLE001 — no token, no restore, no author
        raise SurfaceWriteError(
            "the incumbent solve object of the '%s' cell of surface %s could not be "
            "captured (%r); nothing was written" % (token, surface, exc),
            field=token, intended="a capturable incumbent solve", actual=prior_type,
            surface=surface) from exc
    if prior_token is None:
        raise SurfaceWriteError(
            "the incumbent solve object of the '%s' cell of surface %s read back as "
            "None, so it is not a replayable restore token; nothing was written"
            % (token, surface),
            field=token, intended="a non-None restore token", actual=None,
            surface=surface)
    value, value_readable = _read_value_tagged(row, token)
    if value is None and prior_type in ("Fixed", "Variable"):
        # THE MESSAGE SAYS "reads as null", NOT "could not be read", AND THE DIFFERENCE IS
        # A FACT ABOUT THE ENGINE. This arm keys on `value is None`, not on
        # `not value_readable` — the tag is unpacked one line up and this arm ignores it —
        # so it fires on a SUCCESSFUL read that returned null as much as on a failed one.
        # `_read_value_tagged`'s own docstring names that as legitimate on this seam ("a
        # material cell can read empty"), and the material path is where it is reachable:
        # `set_solve(cell="material")` calls `_capture` unconditionally and an un-solved
        # material cell arrives with `prior_type == "Fixed"`. The old wording asserted a
        # read FAULT the harness had not observed (0.1.6 external review).
        #
        # WHAT THE REVIEW GOT WRONG, recorded so it is not "fixed" back: it also reported
        # this message as pointing the caller at `solves_unreadable`. It does not, and it
        # never did — that remedy belongs to the two arms where the read genuinely FAILED
        # (`_capture`'s unreadable-type arm and `clear_solve`'s), where it is correct.
        #
        # AND WHAT THE ROUND-1 REWORDING GOT WRONG, stripped in round 2. It illustrated the
        # legitimate-empty case as "(a material cell on an air surface)". This tree's
        # MEASURED convention is that air material reads `""` — an EMPTY STRING, not null —
        # so the parenthetical names a reading nothing here has observed, and it is worse
        # than idle: the deferral three lines below says in terms that no probe or fixture
        # in this tree has `Material` returning null and that it must be measured first,
        # while the SERVED text was answering that question with an example. A concrete
        # instance SOFTENS an alarm — an agent reading it concludes "ah, the ordinary air
        # case" and stops looking. The honest half ("either way there is no token to
        # restore") is what makes the refusal actionable and it is kept verbatim.
        #
        # STILL REFUSES, and the direction is deliberate: a non-driving solve OWNS its
        # number, so a null gives the transaction nothing to restore. Whether a legitimate
        # null should be PERMITTED here rather than refused is a behaviour question that
        # needs a live measurement of `Material` returning null — no probe or fixture in
        # this tree has one — and is ticketed rather than guessed at.
        raise SurfaceWriteError(
            "the '%s' cell of surface %s carries a non-driving %s solve whose VALUE "
            "reads as null — a non-driving solve OWNS its number, so a null one is an "
            "unrestorable one. This may be a legitimate empty reading rather than a read "
            "fault; either way there is no token to restore. Nothing was written."
            % (token, surface, prior_type),
            field=token, intended="a non-null incumbent value", actual=None,
            surface=surface)
    fields = _sc.read_solve_fields(_sc.unwrap_or_none(prior_token), token, prior_type)
    unread = sorted(n for n, (_v, state) in fields.items() if state == "mismatched")
    if unread:
        raise SurfaceWriteError(
            "the '%s' cell of surface %s carries a %s solve whose %s field(s) could not "
            "be READ, so a restore of it could never be proven — the proof would skip "
            "exactly the field it could not capture and report the relationship "
            "restored. Refusing BEFORE mutating anything. Read `solves` on read_surface "
            "to see what this cell reports."
            % (token, surface, prior_type, ", ".join(repr(n) for n in unread)),
            field=token, intended="a fully readable incumbent solve",
            actual=prior_type, surface=surface)
    return {"type": prior_type, "token": prior_token, "value": value,
            "value_readable": value_readable, "fields": fields}


# --------------------------------------------------------------------------- #
# THE RESTORE. Asymmetric on the PRIOR's class, TYPE-FIRST.
# --------------------------------------------------------------------------- #
def _observe(system, lde, surface, token):
    """BEST-EFFORT ``(type, value)`` as they read RIGHT NOW. NEVER raises, ever.

    The observed post-restore state the contract REQUIRES and a first cut never
    collected: its ``_partial`` hardcoded ``actual=None`` and its test asserted only
    ``"Fixed" in message and "type" in message`` — both satisfied by the PRIOR type and
    the failed-step LABEL, so the assertion could not establish that anything was
    observed at all.

    IT MUST NOT MASK THE ORIGINAL FAILURE. It runs while an exception is being handled, so
    every arm is guarded and an unreadable half degrades to ``None`` rather than
    displacing the diagnosis that sent us here. ``None`` in either slot renders as
    "unreadable", which is a fact about the cell and part of the diagnosis.
    """
    try:
        actual = _sc.read_solve_type(_sc.solve_cell(system, lde, surface, token))
    except Exception:  # noqa: BLE001 — an unreadable observation is still an observation
        actual = None
    try:
        value = _read_value(lde.GetSurfaceAt(surface), token)
    except Exception:  # noqa: BLE001
        value = None
    return actual, value


def _partial(prior, step, detail, surface=None, token=None, observed=None, entered=None):
    """The ``solve_partial_state`` refusal. Its FIRST clause states the UNKNOWN.

    ``observed`` is the ``(type, value)`` pair read AFTER the failed rollback and
    ``entered`` is whether the engine write call was ever ENTERED — the two facts that
    turn "your cell is in an unknown state" into a diagnosis a caller can act on. Both are
    OPTIONAL so the pre-``SetSolveData`` construction sites (which have nothing to observe
    yet) stay honest by omission rather than by fabricating a reading.

    ``entered`` IS ``set_entered``'S READER, and building it is what closed the gap:
    the flag was WRITTEN TWICE AND READ NOWHERE (AST-verified ``Load=0, Store=2``) while
    its own docstring argued at length that collapsing it into ``restore_needed`` "makes
    the conservative pre-set indistinguishable from a lie about whether the engine call
    was entered". Whether the call was entered is exactly what a partial-state report must
    state, so the flag now has the one consumer that makes the distinction visible.
    """
    extra = ""
    if observed is not None:
        got_type, got_value = observed
        extra += (" The cell now reads type=%s, value=%s."
                  % ("unreadable" if got_type is None else got_type,
                     "unreadable" if got_value is None else got_value))
    if entered is not None:
        extra += (" The engine write call WAS entered before this failure."
                  if entered else
                  " The engine write call was NEVER entered.")
    return SolvePartialStateError(
        "the solve state of the '%s' cell of surface %s is UNKNOWN: a write was "
        "attempted and the rollback to the prior %s solve could not be completed or "
        "proven (the %s step failed: %s).%s %s"
        % (token, surface, prior["type"], step, detail, extra,
           SolvePartialStateError.REMEDY),
        field=token, intended=prior["type"],
        actual=None if observed is None else observed[0], surface=surface)


def _material_note(prior, material, unchanged):
    """The material clear's prose, chosen BY the measurement it ships beside.

    Three outcomes, three sentences, and none of them is written before the comparison is
    made. An earlier cut measured LIVE that a ``MaterialModel`` clear CHANGES the glass
    and folded that into the structured field only, so the SAME success envelope could
    read ``glass_unchanged: false`` next to a note asserting "The surface KEEPS its
    glass" —
    the falsification reached the code and not the words. An agent that reads prose over
    fields (they do) was told the false branch could not happen.
    """
    if unchanged is True:
        return ("the %s solve was cleared and the glass is UNCHANGED (%r). Clearing a "
                "solve does not clear a material; use substitute_glass to change it "
                "('' is canonical air, not the literal 'AIR')."
                % (prior["type"], material))
    if unchanged is False:
        return ("the %s solve was cleared and the glass CHANGED, from %r to %r — some "
                "material solves own the glass string, so removing them does not leave "
                "it standing. Use substitute_glass to set the material you want ('' is "
                "canonical air, not the literal 'AIR')."
                % (prior["type"], prior["value"], material))
    return ("the %s solve was cleared. Whether the GLASS survived could NOT be "
            "established — one of the two material reads failed, so glass_unchanged is "
            "null rather than true. Read the material cell before relying on it, and "
            "use substitute_glass to set the material you want." % (prior["type"],))


def attach_partial_state(exc, err):
    """Attach ``err`` to ``exc`` on the EXPLICIT partial-state channel. NEVER raises.

    Shared by ``_restore`` and by ``cb_surface``'s abort arm, so the two producers of a
    partial-state finding and the single consumer (``Dispatcher._classify``) agree by
    construction rather than by two matching literals.

    ``setattr`` is guarded because the object is an arbitrary ``BaseException`` — a
    ``__slots__`` type, or a pathological one whose ``__setattr__`` raises, must not turn
    a diagnostic breadcrumb into the thing that replaces the abort. On that path the
    family degrades to ``internal``, which is the honest answer when the signal could not
    be attached, and the abort still travels unchanged.

    ``__context__`` IS A BREADCRUMB AND NOW LIVES INSIDE THE SAME GUARD. It sat one line
    below the call in ``_restore``, UNGUARDED, on the same arbitrary ``BaseException`` —
    so a type whose ``__setattr__`` raises was protected from the ``setattr`` above and
    then displaced the abort on the very next statement, which is the outcome this guard
    exists to prevent (0.1.6 external review). ``__context__`` is a C-level slot on
    ``BaseException``, so the ``__slots__`` half of the concern does not apply to it; the
    raising-``__setattr__`` half does, because a subclass override still intercepts it.
    MOVED rather than separately guarded: this helper is where "attach a breadcrumb
    without ever displacing the abort" is defined, and a second guarded copy at the call
    site is the drifting-sibling shape this module keeps paying for.

    CONSEQUENCE FOR THE OTHER CALLER — AND THE FIRST WORDING OF THIS PARAGRAPH WAS FALSE,
    WHICH IS WHY THE CORRECTION IS RECORDED RATHER THAN THE CONCLUSION. It claimed
    ``cb_surface``'s abort arm "NOW chains the finding too ... stated because it is a real
    change". It was not a change: at HEAD that arm ALREADY wrote ``exc.__context__ = err``
    in its own guarded ``try``, three lines below its ``attach_partial_state`` call. Nothing
    about the diagnostic it emits moved. What the MOVE actually does is make this helper the
    SINGLE OWNER of the write, which makes the call-site copy REDUNDANT — and the paragraph
    above names that copy as "the drifting-sibling shape this module keeps paying for", so
    the copy was deleted as the coherent completion of the move, not as an incidental tidy.

    WHAT THE DELETION LOSES, stated so nobody restores it by accident: the call site's
    independent retry could fire in exactly ONE state this helper cannot reach — a
    ``BaseException`` whose ``__setattr__`` raises for ``PARTIAL_STATE_ATTR`` and SUCCEEDS
    for ``__context__``, i.e. one that discriminates BY ATTRIBUTE NAME. That is unmeasured
    and contrived; the shared guard is one ``try`` for both writes, so on that object both
    breadcrumbs are now dropped together and the abort still travels unchanged.
    """
    try:
        setattr(exc, PARTIAL_STATE_ATTR, err)
        exc.__context__ = err
    except BaseException:  # noqa: BLE001 — a breadcrumb NEVER displaces the abort
        pass


def _emit_partial_signal(session, err):
    """Write the partial-state finding to the DURABLE interaction log. NEVER raises.

    THE ABORT PATH IS WHY THIS EXISTS. When rollback is interrupted by a
    ``BaseException`` the abort must travel unchanged (converting a
    ``KeyboardInterrupt`` into a ``SurfaceWriteError`` turns Ctrl-C into a return value),
    but the restore contract also promises the partial-state signal is never lost. Both
    hold only if the signal leaves by a channel that is not the return value: the durable
    log records it, ``__context__`` chains it onto the abort for anyone who catches it,
    and the abort
    itself is re-raised untouched.

    It routes through ``session._log``, the never-raise seam (logger when wired, stderr
    otherwise) — the same one the session's own orphan/slow-open breadcrumbs use. It is
    guarded again here because a breadcrumb must never displace the exception that is
    already travelling.

    THE CATCH IS ``BaseException`` AND THAT IS NOT THE SAME DECISION AS ``_restore``'s.
    The ruling on catch width — *on an abort's TRAVEL PATH re-raise a
    non-``Exception``; doing envelope construction for an ALREADY-CAUGHT exception, do
    not* — was written into ``_restore`` and then contradicted here, in the function the
    ruling itself called into existence: this docstring claimed "NEVER raises" over an
    ``except Exception``, so a ``_log`` raising ``KeyboardInterrupt``/``SystemExit``
    ESCAPED and REPLACED the original abort. The audit reproduced a rollback
    ``KeyboardInterrupt`` arriving at the caller as ``SystemExit(7)`` — the diagnosis
    swapped for the breadcrumb's own failure. This is not the travel path: the abort is
    caught, sitting in a local, and about to be re-raised two statements below. Swallowing
    a second abort raised BY THE LOGGER loses nothing (the original is re-raised
    immediately) and preserves the one thing that matters — that the exception which
    reaches the caller is the one that actually happened.
    """
    try:
        session._log("surface_solve: %s" % (err,))
    except BaseException:  # noqa: BLE001 — see above; a breadcrumb NEVER displaces the abort
        pass


def _restore(session, system, lde, surface, token, prior, entered=None):
    """Type first, then the value, then PROVE. Nothing escapes as an opaque ``internal``.

    ORDER SELECTED BY MEASUREMENT, not by argument, and the losing
    candidate is deleted rather than kept as an alternative. Value-first was measured
    STRUCTURALLY BROKEN on these cells: its value write lands on a cell still carrying
    the FAILED DRIVING solve, and a bare write onto a driving ``radius``/``thickness``
    cell is INERT — the value does not land and the solve is not converted — so the step
    silently does nothing and the later replay freezes the cell wherever the abandoned
    solve drove it. Type-first makes the cell non-driving first, and a bare write onto a
    ``Fixed`` or ``Variable`` cell was measured to LAND and preserve the type.

    STEP 2 IS LOAD-BEARING FOR ``Variable`` AS WELL AS ``Fixed``: a captured token replay
    restores the TYPE ONLY (measured — the value stayed at the intervening overwrite), so
    a type-only restore is not a rollback.

    NEVER a value write for an ``Automatic`` or a DRIVING prior: the former CONVERTS the
    solve (measured), and for the latter the captured token already carries the real
    state.

    THE CATCH IS ``BaseException`` AND THE ABORT IS RE-RAISED UNCHANGED. A first cut
    caught ``Exception``, so a ``KeyboardInterrupt`` during rollback reached dispatch as
    ``internal`` and the machine-readable partial-state family was LOST — while the
    restore contract promises nothing escapes as an opaque internal. Narrowing kept the
    abort unchanged and broke that promise; converting would keep the promise and change
    the abort. Both hold at once only by
    splitting the SIGNAL from the RETURN VALUE: build the partial-state error, emit it
    durably, chain it as the abort's ``__context__``, then let the abort travel.
    """
    step = "type"
    try:
        cell = _sc.solve_cell(system, lde, surface, token)
        if prior["type"] == "Automatic":
            fresh = cell.CreateSolveType(
                _resolve_enum(_sc.solve_type_enum(system), "Automatic"))
            if fresh is None:
                raise _partial(prior, step, "CreateSolveType(Automatic) returned None, "
                               "so SetSolveData was NEVER invoked on it", surface, token,
                               _observe(system, lde, surface, token), entered)
            cell.SetSolveData(fresh)
        else:
            cell.SetSolveData(prior["token"])
        if prior["type"] in ("Fixed", "Variable") and prior["value"] is not None:
            step = "value"
            setattr(lde.GetSurfaceAt(surface), _sc.TOKEN_TO_COLUMN[token],
                    prior["value"])
    except SolvePartialStateError:
        raise
    except BaseException as exc:  # noqa: BLE001 — nothing escapes as an opaque internal
        err = _partial(prior, step, repr(exc), surface, token,
                       _observe(system, lde, surface, token), entered)
        if isinstance(exc, Exception):
            raise err from exc
        _emit_partial_signal(session, err)
        # THE EXPLICIT channel. ``__context__`` is still set — a human reading
        # the traceback should see the finding chained — but it is no longer what decides
        # the family, because Python assigns ``__context__`` implicitly and an abort that
        # merely happened inside a ``HarnessError`` handler was being renamed by it.
        attach_partial_state(exc, err)
        raise
    _prove_restore(session, system, lde, surface, token, prior, entered)


def _field_drift(before, after):
    """Captured fields that no longer read the same. ``""`` when the replay was faithful.

    ``read_solve_fields`` tags every field it reads: ``matched`` (read, representable),
    ``not_representable`` (read, non-finite — ``Offset`` reads NaN on 9 of 13 CORRECT
    corpus pickups) and ``mismatched`` (could not be read at all). This function decides
    exactly TWO of those three, and the third is decided somewhere else entirely:

    * ``matched`` -> the post-read must ALSO be ``matched`` and must render the same AND
      compare ``==``. A first cut compared renderings alone, so two distinct objects
      sharing a ``repr`` were called equal; the guarded ``==`` is an independent second
      witness, and it cannot over-refuse a faithful replay because the one value class
      for which
      ``==`` is false against itself — a non-finite float — is never ``matched``.
    * ``not_representable`` -> the post-read must still be ``not_representable``. A first
      cut skipped this state ENTIRELY, so a field that read NaN before and became
      UNREADABLE after was reported as a faithful restore: readability had degraded and
      the check
      said nothing. The value itself still cannot be compared (NaN is not equal to
      itself, and the raw number was discarded upstream so the block can be dict-compared
      at all), so a NaN that came back ``+inf`` is NOT drift by this rule. That residual
      is DISCLOSED, not closed — closing it means changing the substrate's value contract,
      which the disclosure block consumes.
    * ``mismatched`` -> UNREACHABLE HERE. A capture that could not read a field now
      REFUSES in ``_capture``, pre-mutation, because after the write the pre-image is gone
      and no downstream check can ever recover it. A first cut skipped these too, which
      is the hole the audit reproduced.

    So the narrowed claim, stated to match what the code decides: *every field this
    function was given a readable pre-image for must still read, must still be
    representable if it was, and must still hold the same value; a field whose pre-image
    was non-finite is checked for continued readability only.*

    ``Column`` canonicalises through ``str()`` — its value is a raw ``SurfaceColumn``
    proxy, and the module's wire rule for it is already ``str`` in both other producers.
    """
    out = []
    for name in sorted(before):
        want, state = before[name]
        got, got_state = after.get(name, (None, "mismatched"))
        if state == "not_representable":
            if got_state != "not_representable":
                out.append("%s read a non-finite value before and is now %s"
                           % (name, got_state))
            continue
        if state != "matched":                       # mismatched: refused at capture
            continue
        if got_state != "matched":
            out.append("%s is now %s (it read %s before)"
                       % (name, got_state, _render_field(name, want)))
        elif _render_field(name, got) != _render_field(name, want) \
                or _definitely_unequal(want, got):
            out.append("%s reads %s, not %s"
                       % (name, _render_field(name, got), _render_field(name, want)))
    return ", ".join(out)


def _definitely_unequal(want, got):
    """``True`` only when ``==`` answered exactly ``False``. Indeterminate is NOT drift.

    The second witness beside the rendered compare: ``repr``/``str`` can
    collide across distinct objects, and a .NET proxy is free to render identically to a
    different one, so ``==`` is an independent channel over the same pair.

    IT IS DELIBERATELY ONE-SIDED, and the asymmetry is the point. A comparison that RAISES
    or returns something that is not a Python ``bool`` tells us NOTHING, and converting
    "we could not tell" into drift would raise ``solve_partial_state`` on a FAITHFUL
    replay — a measured warning, where the NaN ``Offset`` on 9 of 13 corpus pickups
    would have manufactured a failure on every correct restore. A witness that can only
    ACCUSE when it is certain adds evidence without adding false alarms; the rendered
    compare remains the primary, and the ``_capture`` refusal is what covers the fields
    neither of them can speak for.
    """
    try:
        verdict = want == got
    except Exception:  # noqa: BLE001 — an uncomparable pair proves nothing either way
        return False
    return verdict is False


def _render_field(name, value):
    """One rendering for BOTH sides of a field compare — so the compare cannot be lopsided.

    THE COERCION IS NOT DECIDED HERE. It was,
    and so were two other sites, each with its own copy of ``if name == Column: str(...)``
    — one rule written three times, which is the drift shape being closed here.
    ``to_wire`` owns it now; this function owns only the RENDERING.

    The compare is unchanged in what it decides: ``to_wire`` is injective on the token
    (two different ``Column`` members render two different strings, the same member the
    same string), so a pair that compared equal before still does and a pair that did not
    still does not. The message text for a ``Column`` drift now reads ``'Radius'`` rather
    than ``Radius``, which is the quoting ``repr`` gives every other field.
    """
    return repr(to_wire(name, value))


#: Relative agreement required of a restored NUMERIC cell value, with an absolute floor for
#: a prior of exactly 0.0.  See the long note at the compare site in ``_prove_restore`` for
#: why this is a correction of a measured-false premise rather than a loosened guard.
_RESTORE_REL_TOL = 1e-9
_RESTORE_ABS_TOL = 1e-12


def _value_restored(value, prior_value):
    """Did the cell come back to ``prior_value``?  Tolerant for numbers, exact otherwise.

    ``bool`` is excluded from the numeric arm deliberately: it is an ``int`` subclass, and
    a solve value that is genuinely ``True``/``False`` should compare identically, not
    numerically.  Anything non-numeric (a material string, ``None``) falls through to the
    exact compare this function replaced, so the only behaviour that changed is the one
    the dogfood proved wrong.
    """
    if not (isinstance(value, (int, float)) and not isinstance(value, bool)
            and isinstance(prior_value, (int, float))
            and not isinstance(prior_value, bool)):
        return value == prior_value
    return math.isclose(float(value), float(prior_value),
                        rel_tol=_RESTORE_REL_TOL, abs_tol=_RESTORE_ABS_TOL)


def _prove_restore(session, system, lde, surface, token, prior, entered=None):
    """Re-fetch BOTH row and cell and prove type, value AND the captured FIELDS.

    THE FIELD ARM IS THE CRITICAL ONE, and both audits found it from opposite sides: the
    proof compared TYPE and VALUE only and never consumed ``prior["fields"]``, which the
    capture stores — while NO test bound any of the three compares, so the blindness
    could not
    have surfaced from the suite. The counterexample is a ``semi_diameter`` pickup over a
    ZERO-valued source whose replayed ``ScaleFactor`` is silently coerced: the type still
    reads ``SurfacePickup``, the value is still zero, the proof PASSES, and the caller is
    told "the prior SurfacePickup solve was restored and verified" while the RELATIONSHIP
    is now different — it produces a different number the moment the source moves. A
    restore that changes the relationship it restored is not a rollback, and reporting it
    as one defeats the transaction's central safety claim.
    """
    try:
        cell = _sc.solve_cell(system, lde, surface, token)
        actual = _sc.read_solve_type(cell)
        value = _read_value(lde.GetSurfaceAt(surface), token)
        fields = _sc.read_solve_fields(
            _sc.unwrap_or_none(cell.GetSolveData()), token, prior["type"])
    except Exception as exc:  # noqa: BLE001
        raise _partial(prior, "proof", repr(exc), surface, token,
                       _observe(system, lde, surface, token), entered) from exc
    if actual != prior["type"]:
        raise _partial(prior, "proof",
                       "the type reads back %r, not %r" % (actual, prior["type"]),
                       surface, token, (actual, value), entered)
    # ``Automatic`` is engine-recomputed, so its number is not ours to compare. Every
    # other prior either OWNS its value (Fixed/Variable) or has it re-driven by the
    # replayed solve.
    #
    # THE COMPARE IS TOLERANT, AND THAT IS A CORRECTION, NOT A RELAXATION. This line used
    # to compare with ``!=`` on the claim that "doubles store verbatim". MEASURED at the
    # 0.1.6 dogfood, against a live engine, that claim is FALSE: a thickness captured as
    # 2.6435181215875687 reads back 2.64351812158757 -- the engine round-trips through
    # ~15 significant digits. The consequence was not a missed fault but a FABRICATED one:
    # an authoring that had already failed cleanly was reported as
    # ``solve_partial_state`` -- "the solve state is UNKNOWN ... reload your design" --
    # when the cell in fact held the prior value to every digit the engine keeps. The same
    # operation on the same surface with a short-decimal value (2.5) produced a clean
    # ``SurfaceWriteError`` with the rollback proven, which is what isolated the cause.
    #
    # A false UNKNOWN is not the safe direction. It tells a caller mid-session to discard
    # work over a difference of 1.3e-14 mm, and a guard that cries wolf at the 15th digit
    # is one people learn to ignore.
    #
    # 1e-9 RELATIVE is chosen to sit far above the round-trip (~5e-15 relative, measured)
    # and far below any restore failure worth catching: a rollback that did not happen
    # leaves a DIFFERENT number -- the value the failed write put there, or the engine's
    # recomputation -- not one agreeing to nine significant figures. The absolute floor
    # covers a prior of exactly 0.0, where a relative test has no meaning. Non-numeric
    # priors (``material`` is a string) keep the exact compare; ``isclose`` is only
    # reached when BOTH sides are real numbers.
    if prior["type"] != "Automatic" and prior["value"] is not None \
            and not _value_restored(value, prior["value"]):
        raise _partial(prior, "proof",
                       "the value reads back %r, not %r" % (value, prior["value"]),
                       surface, token, (actual, value), entered)
    drift = _field_drift(prior["fields"], fields)
    if drift:
        raise _partial(prior, "proof",
                       "the type and the value came back but the RELATIONSHIP did not: "
                       "%s" % (drift,), surface, token, (actual, value), entered)


# --------------------------------------------------------------------------- #
# T1 (gate), T2 (gate, pickup only), T3 (disclosure).
# --------------------------------------------------------------------------- #
def _t1(system, lde, surface, token, member, canonical):
    """MANDATORY type read-back. MEMBERS first, ``str()`` as the fallback.

    ``is`` IS WRONG AND IS NEVER USED, and that is now measured rather than asserted: two
    members resolved from the SAME live enum class are ``is``-FALSE and ``==``-TRUE, so
    an identity compare would fail on every correct author. The member compare also gets
    the alias pair right for free (one .NET value under two proxies).
    """
    actual = _sc.read_solve_type(_sc.solve_cell(system, lde, surface, token))
    try:
        matched = bool(_sc.solve_cell(system, lde, surface, token)
                       .GetSolveData().Type == member)
    except Exception:  # noqa: BLE001 — fall back to the canonical string compare
        matched = actual is not None and actual == canonical
    if not matched:
        raise SurfaceWriteError(
            "the %s solve on surface %s did not take: intended %s, reads back %s (a "
            "silent no-op — the engine reported Success)."
            % (token, surface, canonical, actual),
            field=token, intended=canonical, actual=actual, surface=surface)
    return actual


def _catalog_supports_offset(token):
    """Does the FROZEN substrate catalog record ``Offset`` participation for this cell?

    Measured: ``Offset`` participates ONLY on a ``thickness`` target and is silently
    dropped (reads back NaN) on radius / conic / semi_diameter. The substrate's catalog
    already encodes exactly that, keyed on the cell — the same key the arithmetic law was
    independently derived on — so this reads it rather than opening a second literal that
    could drift from it. A drift test pins the two together in both directions.

    This is NOT the runtime ``Supports*`` flag, which is DIAGNOSIS-only and never gates:
    that flag reads False on a CORRECT radius pickup too.
    """
    entry = _sc._MEASURED.get(
        "%sCell.SurfacePickup" % (_sc.TOKEN_TO_COLUMN.get(token, token),))
    return bool(entry and entry[1])


def _t2(system, lde, surface, token, canonical, fields, prior_type):
    """MANDATORY pickup RELATION gate, computed from the CALLER'S STATED INTENT.

    THE NON-PICKUP ARM IS NO LONGER A SINGLE WORD. It used to answer
    ``not_applicable`` for all 38 other types — one token covering "we measured a law and
    it failed", "we never looked", "the target is outside what was measured" and "the
    operand would not read", which are four different facts. It now DELEGATES to
    ``_solve_trace.relation_for``, which either runs a MEASURED paraxial-operand oracle or
    names which of those four it is. The pickup arm below is BYTE-UNCHANGED.

    ``prior_type`` IS REQUIRED AND HAS NO DEFAULT. The ``MarginalRayHeight`` oracle is
    declined on a same-type re-author (an omitted ``PupilZone`` INHERITS the live value
    rather than defaulting, and a written zone moves the relationship by ~0.5), so a
    defaulted ``None`` here would silently pass that precondition and certify a
    relationship the harness cannot see the inputs to.

    THE LAW IS PER-TARGET-CELL AND WAS MEASURED, not assumed. A ``radius`` TARGET scales
    CURVATURE — ``R_target = source / ScaleFactor`` — whatever column the source is read
    from; ``thickness`` / ``conic`` / ``semi_diameter`` scale the VALUE linearly. Every
    prior measurement had used ``ScaleFactor = +/-1``, where ``s * x`` and ``x / s``
    coincide EXACTLY, so the two models agreed at every point anyone had looked at. A
    single closed form ``s * source + offset`` computes 200.0 where the engine correctly
    produces 50.0, i.e. it REFUSES AND ROLLS BACK A CORRECT PICKUP for every
    ``|ScaleFactor| != 1``.

    WHY INTENT AND NOT READ-BACK — three properties, each of which has a measured
    counterexample behind it:
      * it removes the tautology (a read-back-derived prediction checks the engine
        against itself);
      * it sidesteps the NaN without pretending NaN is a number (``Offset`` reads NaN on
        9 of 13 CORRECT corpus pickups, so a literal read-back form refuses correct
        designs);
      * it is the ONLY instrument that catches a silently-dropped field write ON THE
        PATHS THAT REACH IT. TWO measured coercions pass T1 AND T3 and are visible only
        here: a dropped ``Offset: 2.5`` leaves a NaN read-back indistinguishable from a
        correct pickup (T2's residual is exactly 2.5); and an invalid ``Surface`` is
        coerced to 0 while the type reads back correctly.

    THE THIRD MEASURED COERCION IS NOT T2's, and this docstring said it was until it
    was corrected. A NEGATIVE
    ``ScaleFactor`` on a ``semi_diameter`` pickup IS silently discarded by the engine (the
    cell carries ``1.0 * source`` while the FIELD reads back the negative value asked
    for) — but ``_require_pickup_fields`` REFUSES it PRE-mutation, so it never arrives
    here. Reading this the other way round is the decision the stale text invited: that
    the refusal is redundant and can be deleted. It cannot. At a ZERO or sub-``abs_tol``
    source the two models COINCIDE — intent predicts ``-0.0``, the engine gives ``0.0``,
    ``math.isclose`` agrees — and T2 reports ``verified`` over a discarded sign. WHICH
    LAYER OWNS WHICH CASE is the load-bearing fact, not the count.

    A NO-MOVEMENT OUTCOME IS A PASS. T2 requires AGREEMENT, not CHANGE.
    """
    if canonical != "SurfacePickup":
        # LAZILY IMPORTED, and the direction is forced rather than stylistic: the tier
        # binds this module's ``_T2_*`` tolerances BY IDENTITY, so it must import
        # ``surface_solve`` at MODULE level. A module-level import here would close the
        # cycle onto a half-built module; a function-local one is the house pattern for
        # exactly that (``_solve_cells.read_solve_type``, ``_structural_common``).
        from . import _solve_trace as _st

        # THE OUTER NET. ``relation_for``'s never-raise contract is a
        # CORRECTNESS property, not a preference: this call sits INSIDE ``_transact``'s
        # window, which treats ANY post-invoke exception as a failure and RESTORES. So a
        # fault while GRADING a correct authoring rolls that authoring back.
        #
        # WHY A NET AND NOT A LONGER LIST OF GUARDS. Enumerating the raising expressions
        # is a race this side loses. ``_safe_repr`` closes eight ``%r`` sites; the block
        # builder ``_oracle_block`` sits above ``_trace``'s own ``try`` and is covered by
        # none of them; and the post-``try`` arithmetic tail (``abs(expected - traced)``)
        # runs unguarded against a ``float`` SUBCLASS that passes ``_is_finite_number``
        # and overrides its arithmetic dunders. THIS IS THE BOUNDARY WHERE THE ROLLBACK
        # CONSEQUENCE ACTUALLY EXISTS, which is what makes it the right place for the
        # invariant rather than a second authority competing with the tier's own arms.
        #
        # ``Exception``, NEVER ``BaseException``: a ``KeyboardInterrupt`` or a
        # ``SystemExit`` raised through this call must still travel the abort path. It is
        # the transaction's job to leave the cell consistent for those, and ``_transact``
        # re-raises a non-``Exception`` after restoring.
        #
        # THE REASON IS A DISTINCT THIRD FLAVOUR, and that is a requirement rather than
        # phrasing. The tier's two existing ``traced_unreadable`` sentences both say the
        # ENGINE's reading was unusable. A typo'd key here would become a ``KeyError``
        # wearing that sentence — a HARNESS defect reported as an engine fact, sending a
        # triager to the wrong layer. The state token is shared because the CONSEQUENCE is
        # identical (nothing established, nothing gated, nothing rolled back); only the
        # attribution differs, so only the attribution is written differently.
        try:
            return _st.relation_for(system, lde, surface, token, canonical, fields,
                                    prior_type)
        except Exception as exc:  # noqa: BLE001 — see the paragraph above
            return {"state": "traced_unreadable",
                    "reason": "the relation check could not be COMPLETED: this harness's "
                              "own verification tier raised %s while grading the solve. "
                              "That is a DEFECT IN THIS HARNESS, not a fact about your "
                              "engine or your design — the oracle may never have been "
                              "read, and no reading has been judged. The solve WAS "
                              "authored and its TYPE was proven; only its consequence is "
                              "unverified, and NOTHING was rolled back. Please report "
                              "this message." % (_safe_repr(exc),)}
    scale, offset = fields["ScaleFactor"], fields["Offset"]
    column = str(fields[_COLUMN_FIELD])
    source = {"surface": fields["Surface"], "column": column}
    # WHICH QUANTITY THE CALLER'S NUMBER ACTED ON — served on every relation verdict.
    #
    # The check the audit said was MISSING is at the INTERFACE, not the read-back. A
    # later review closed this as "uncatchable by any read-back", which is true and
    # irrelevant: no
    # oracle can distinguish "I meant twice the curvature" from "I meant twice the
    # radius", because nothing is malfunctioning — but the tool KNOWS the target cell is
    # ``radius`` and therefore KNOWS the caller's number will invert, before authoring.
    # It cannot guess intent (a refusal built on a guess blocks the correct call half the
    # time), so it STATES the quantity instead, on the same key the caller is already
    # reading for the verdict.
    #
    # MEASURED LIVE, both directions off 1 so the two laws are DISCRIMINATED
    # (a live probe): source radius 100.0, ScaleFactor 2.0 -> driven
    # 50.0; ScaleFactor 0.5 -> 200.0; and BOTH returned ``relation: verified``. At -1.0
    # the reciprocal and linear laws coincide exactly, which is why every earlier
    # measurement in this programme missed the inversion.
    scaled_quantity = "curvature" if token == "radius" else "value"
    relation_law = ("R_target = R_source / ScaleFactor"
                    if token == "radius" else "value_target = ScaleFactor * value_source")
    try:
        source_value = float(getattr(lde.GetSurfaceAt(fields["Surface"]), column))
        actual = float(getattr(lde.GetSurfaceAt(surface), _sc.TOKEN_TO_COLUMN[token]))
    except Exception as exc:  # noqa: BLE001 — a DISCLOSED fail-open, named as one
        return {"state": "not_computable", "source": source,
                "scaled_quantity": scaled_quantity, "relation_law": relation_law,
                "reason": "the source or target value could not be read as a number "
                          "(%r) — a Material-column pickup is the expected case" % (exc,)}
    if not math.isfinite(source_value):
        return {"state": "not_computable", "source": source,
                "scaled_quantity": scaled_quantity, "relation_law": relation_law,
                "reason": "the source value is non-finite (a plano source reads inf), so "
                          "no residual can be computed; the pickup is authored and "
                          "UNVERIFIED"}
    expected = (source_value / scale) if token == "radius" else (scale * source_value)
    if _catalog_supports_offset(token):
        expected += offset
    if not math.isfinite(expected):
        # ``ScaleFactor`` is validated FINITE and NON-ZERO pre-mutation, and on
        # a radius target it is a DIVISOR — so a legal SUBNORMAL (``5e-324``) passes V10
        # and ``100.0 / 5e-324`` overflows to ``inf``. A first cut finiteness-checked only
        # ``actual``, so a CORRECTLY driven value was classified a coercion mismatch and a
        # correct pickup was refused and rolled back. The intended value is the thing that
        # could not be computed, and saying so is honest: this is the SAME fail-open
        # vocabulary the plano source already uses, not a verification.
        return {"state": "not_computable", "source": source,
                "scaled_quantity": scaled_quantity, "relation_law": relation_law,
                "reason": "the value you asked for overflows double precision "
                          "(ScaleFactor %r against a source of %r), so no residual can "
                          "be computed; the pickup is authored and UNVERIFIED"
                          % (scale, source_value)}
    if not math.isfinite(actual):
        return {"state": "mismatch", "expected": expected, "actual": None,
                "source": source,
                "scaled_quantity": scaled_quantity, "relation_law": relation_law,
                "reason": "every intended input is finite while the driven value read "
                          "back NON-FINITE — the engine accepted the solve and silently "
                          "coerced a field"}
    if math.isclose(expected, actual, rel_tol=_T2_REL_TOL, abs_tol=_T2_ABS_TOL):
        return {"state": "verified", "expected": expected, "actual": actual,
                "source": source, "scaled_quantity": scaled_quantity,
                "relation_law": relation_law}
    return {"state": "mismatch", "expected": expected, "actual": actual, "source": source,
            "scaled_quantity": scaled_quantity, "relation_law": relation_law,
            "reason": "the driven value disagrees with the arithmetic you stated"}


def _attempted_snapshot(system, lde, surface, token, canonical):
    """The ATTEMPTED state's fields, read BEFORE the restore. Never raises.

    It must run before the rollback: T3 runs only after T2 PASSES, so a post-restore read
    would describe the PRIOR solve rather than the failed attempt — and naming the prior's
    fields in a refusal about a coercion is worse than naming none. Member values render
    through ``str()`` (the wire rule) because the snapshot can contain a raw
    ``SurfaceColumn``.

    IT RE-FETCHES THE CELL. A first cut read the handle captured in ``_prelude``, before
    any mutation — exactly the shape the re-fetch rule exists to prohibit, in the one
    function whose entire docstring is about naming the RIGHT state. A stale handle
    reports what the cell looked like before
    the write it is describing, which is the failure this message exists to diagnose.
    """
    try:
        got = _sc.read_solve_fields(
            _sc.unwrap_or_none(
                _sc.solve_cell(system, lde, surface, token).GetSolveData()),
            token, canonical)
        return (", ".join("%s=%s" % (k, v[0]) for k, v in sorted(got.items()))
                or "no fields were readable")
    except Exception:  # noqa: BLE001 — disclosure-grade; it must not prevent the restore
        return "the attempted fields were unreadable"


def _t3(system, lde, surface, token, canonical, requested):
    """DISCLOSURE ONLY, tri-state, keyed on the READ-BACK VALUE. Never a gate.

    ``requested`` disambiguates two different claims that would otherwise share the word
    ``matched``: for a field the caller ASKED for it means "reads back what you asked
    for"; for one they did not it means only "readable and representable". Two meanings
    behind one word is the class this repo punishes.

    On a NON-pickup type a ``mismatched`` REQUESTED field is DISCLOSED, not refused — the
    volume of legitimately-differing read-backs is unmeasured, and promoting a disclosure
    to a gate on unmeasured ground is how correct designs start being refused.
    """
    try:
        view = _sc.unwrap_or_none(
            _sc.solve_cell(system, lde, surface, token).GetSolveData())
    except Exception:  # noqa: BLE001 — the write is already verified; this is disclosure
        view = None
    if view is None:
        return None
    out = {}
    for name, (value, state) in _sc.read_solve_fields(view, token, canonical).items():
        asked = name in requested
        # ONE coercion, applied to BOTH sides of the compare, from the SAME function.
        # The two sides used to canonicalise
        # through two separate inline copies of the same rule; a fix to either alone
        # silently makes this compare lopsided, which is how a requested Column starts
        # reading ``mismatched`` against the value it actually produced.
        value = to_wire(name, value)
        if asked and state == "matched":
            want = to_wire(name, requested[name])
            state = "matched" if value == want else "mismatched"
        out[name] = {"value": value, "state": state, "requested": asked}
    return out


# --------------------------------------------------------------------------- #
# Field resolution — V8 catalog, V9 names/kinds, V10 pickup completeness.
# --------------------------------------------------------------------------- #
def _resolve_column(system, value):
    """A ``SurfaceColumn`` MEMBER from a member NAME or a lowercase cell token.

    ``Par*`` columns are REFUSED through this PUBLIC domain, and the REASON changed
    even though the refusal did not. The served text said Par solve field
    shapes were "unmeasured"; a live probe MEASURED one — a CB Par cell DOES accept a
    ``PickupChiefRay`` solve — so that sentence had become false IN A STRING AN AGENT
    READS. The refusal now states what is actually missing: no Par catalog, no Par default
    table, and therefore no rollback. ``_capture`` refuses any incumbent whose
    ``(cell, type)`` pair is uncatalogued and all 39 ``_MEASURED`` keys are the five LDE
    cells, so a Par door would ship EITHER without capture/restore — weaker than the door
    already shipped, on the cells where a mis-step destroys a fold's whole inverse
    transform — or after measuring and cataloguing Par field shapes first. The internal
    seam is how a CB Par column flows, and it takes a resolved MEMBER rather than a name.

    THE FIRST CORRECTION OVERCORRECTED, and this is the second. The
    replacement sentence claimed the solve was measured DRIVING on all four Par cells,
    which the measurement does not support: on ``decenter_x`` and ``tilt_y`` the solve
    landed with ``Success`` but produced NO MOTION under all three meridional
    perturbations, and the probe cannot distinguish "correctly zero" from inert. LEGAL on
    four, DRIVING on two, UNPROVEN on two. A prose-parity row pins that the served string
    never attaches "driving" to ``decenter_x`` or ``tilt_y`` — a served string is the one
    surface no mutation, live gate or audit reaches.

    THE GATE WAS WIDENED, BECAUSE THE CORRECTED TEXT WAS BEHIND A SPELLING THE
    CALLER DOES NOT HAVE. The gate was ``name.startswith("Par")`` alone, and ``name`` is
    ``TOKEN_TO_COLUMN.get(value, value)`` over the FIVE geometry tokens, so it fired only
    on the literal ``SurfaceColumn`` member spelling ``Par1``..``ParN``. Measured at the
    FINAL GATE: ``Par1``/``Par3`` reached the corrected refusal;
    ``decenter_x``/``decenter_y``/``tilt_x``/``tilt_y`` fell to the bare "unknown Column"
    arm. Those four are the tokens EVERY other CB tool in this harness takes
    (``add_coordinate_break``'s own parameters, ``set_cb_variable(param=)``) — and the
    tokens THIS MESSAGE NAMES IN ITS OWN BODY. A message that says "measured LEGAL on all
    four of decenter_x / decenter_y / tilt_x / tilt_y" while being visible only to a
    caller who typed ``Par3`` teaches nothing to the caller who typed what it names.

    THE VOCABULARY IS CONSUMED, NEVER COPIED, and it is consumed in ONE PLACE:
    ``_refuse_if_par_cell`` owns both the predicate and the text. ``tilt_z`` and ``order``
    ride in for free and correctly — they are genuinely Par cells (Par5/Par6), so
    "addresses a Par cell" stays TRUE for them, and the body names them as the absent
    subset.

    THE TWO VOCABULARIES MUST STAY DISJOINT. This is a routing table for PROSE — both
    arms terminate in a REFUSAL and nothing downstream forks on which one fired — so it is
    benign only while ``CELL_TOKENS`` and ``_PARAM_NAMES`` do not intersect. Today's
    disjointness is ACCIDENTAL and precedence on a collision is undefined, so a test row
    pins the emptiness of that intersection rather than leaving it to be discovered.

    THE OTHER DOOR IS NOW FIXED TOO, and escalation is CLOSED.
    The live end-to-end run's first hop was ``set_solve(cell="decenter_y", ...)``,
    refused by ``_prelude``'s ``cell`` gate with the bare five-token domain message: a
    DIFFERENT function, which the earlier widening did not reach, and where NO spelling
    (``Par1`` included) reached the corrected text. The statement ceiling was reviewed
    and four more statements were APPROVED for the shared helper both doors now call,
    because a declared deliverable of this change IS "a corrected served refusal for Par
    cells" and shipping it unreachable through the door callers use would have closed
    the work claiming a deliverable with the gate's falsifying measurement in hand.
    """
    if not isinstance(value, str):
        raise ToolParamError(
            "Column takes a SurfaceColumn member name or a cell token; got %r" % (value,))
    name = _sc.TOKEN_TO_COLUMN.get(value, value)
    _refuse_if_par_cell("Column", value)
    if name not in tuple(_sc.TOKEN_TO_COLUMN.values()):
        raise ToolParamError(
            "unknown Column %r; valid: %s or the cell tokens %s"
            % (value, sorted(_sc.TOKEN_TO_COLUMN.values()), list(_sc.CELL_TOKENS)))
    return _resolve_enum(_sc.surface_column_enum(system), name)


def _require_pickup_fields(token, raw):
    """V10 — a pickup states ALL FOUR fields explicitly, and states them usefully.

    A ZERO ``ScaleFactor`` is refused because the radius law makes it a DIVISOR, so
    non-zero is load-bearing rather than tidy.

    A NON-ZERO ``Offset`` on a target the catalog says cannot carry one is REFUSED
    pre-mutation rather than silently dropped. RELAXING V10 INSTEAD WOULD HAVE BEEN THE
    SILENT-WRONG: T2 would omit the term, ``actual`` would match, and the tool would
    report ``verified`` while part of the caller's stated intent had been discarded — the
    tool agreeing with the engine about a number the user never asked for. ``Offset: 0.0``
    passes; it is a no-op.
    """
    missing = [name for name in _PICKUP_FIELDS if name not in raw]
    if missing:
        raise ToolParamError(
            "a SurfacePickup requires all four of %s explicitly (missing %s): a pickup's "
            "arithmetic is verified from your stated intent (e.g. ScaleFactor=-1.0, "
            "Offset=0.0), and an omitted field would INHERIT a live value the check "
            "cannot see." % (list(_PICKUP_FIELDS), missing))
    for name in ("ScaleFactor", "Offset"):
        value = raw[name]
        if not _is_finite_number(value):
            raise ToolParamError(
                "%s must be a finite number; got %r" % (name, value))
    if raw["ScaleFactor"] == 0:
        raise ToolParamError(
            "ScaleFactor must be non-zero: on a radius target the engine scales "
            "CURVATURE, so the factor is a DIVISOR and zero has no meaning.")
    if token == "semi_diameter" and raw["ScaleFactor"] < 0:
        # T2 CANNOT be the guard for it. MEASURED: a negative
        # ``ScaleFactor`` on a ``semi_diameter`` pickup is SILENTLY IGNORED — the cell
        # carries ``1.0 * source`` while the FIELD reads back the negative value asked
        # for. T2 normally catches that (it is one of the three coercions only an
        # intent-derived check can see), but at a ZERO or sub-``abs_tol`` source the two
        # models COINCIDE: intent predicts ``-0.0``, the engine gives ``0.0``, and
        # ``math.isclose`` agrees — so the tool reports ``verified`` on a relationship
        # that is already wrong and becomes visibly wrong the moment the source moves.
        # A field the engine is MEASURED to discard is refused pre-mutation, exactly as
        # this function refuses a non-zero ``Offset`` on a target that discards it, and
        # for the same reason: reporting ``verified`` while part of the stated intent was
        # thrown
        # away is the silent-wrong.
        raise ToolParamError(
            "the engine silently IGNORES a NEGATIVE pickup ScaleFactor on the "
            "'semi_diameter' cell — the aperture is driven by 1.0 x the source while the "
            "field reads back the %r you asked for. Pass a positive ScaleFactor, or pick "
            "a radius / thickness / conic target where the sign is honoured — refusing "
            "rather than reporting a verified pickup whose sign was thrown away."
            % (raw["ScaleFactor"],))
    if raw["Offset"] != 0 and not _catalog_supports_offset(token):
        raise ToolParamError(
            "the engine silently DISCARDS a pickup Offset on the '%s' cell (it reads "
            "back NaN); only a thickness target carries one. Pass Offset=0.0, or pick a "
            "thickness target — refusing rather than reporting a verified pickup whose "
            "stated offset was thrown away." % (token,))


def _resolve_fields(system, lde, surface, token, canonical, raw):
    """V8 catalog gate + V9 field names, index kinds and the self-pickup refusal."""
    if not isinstance(raw, dict):
        raise ToolParamError("fields must be an object; got %r" % (type(raw).__name__,))
    tag, names = _sc.lookup(token, canonical)
    if tag == "unmeasured":
        raise ToolParamError(
            "the engine offers %r on the '%s' cell but this harness has never measured "
            "its field shape; refusing rather than authoring an unvalidatable "
            "relationship."
            % (canonical, token))
    extra = sorted(k for k in raw if k not in names)
    if extra:
        raise ToolParamError(
            "a %s solve on the '%s' cell takes no field(s) %s; its fields are %s"
            % (canonical, token, extra, list(names))
            if names else
            "a %s solve on the '%s' cell takes no parameters; got %s"
            % (canonical, token, extra))
    total = int(lde.NumberOfSurfaces)
    out = {}
    for name in names:
        if name not in raw:
            continue
        value = raw[name]
        if name in _INDEX_FIELDS:
            if not is_integral_int(value):
                raise ToolParamError(
                    "%s must be an exact integer surface index; got %r" % (name, value))
            value = int(value)
            _lc._require_read_index(value, total)
            if name == "Surface" and value == surface:
                raise ToolParamError(
                    "a pickup cannot reference its OWN surface (%s): the engine accepts "
                    "it, silently no-ops, and still returns Success." % (surface,))
        elif name == _COLUMN_FIELD:
            value = _resolve_column(system, value)
        out[name] = value
    return out


# --------------------------------------------------------------------------- #
# The envelope — wire safety: the first failure DEGRADES, a second one RAISES.
# --------------------------------------------------------------------------- #
def _null_unsafe(mapping, get, put):
    """Null every value in ``mapping`` the wire cannot carry. Returns ``degraded``.

    ONE predicate over BOTH field producers (the sibling an earlier sweep missed).
    ``fields_readback`` was sanitised and ``replaced_solve["fields"]`` was NOT, though
    both are built from the same ``read_solve_fields`` reader over the same field values:
    a prior field carrying an unwire-safe value therefore turned a LANDED, T1/T2-VERIFIED
    write into a wire REFUSAL at the very last line of the tool — the plano
    ``frozen_at`` defect one site over. ``get``/``put`` are the two shapes' accessors
    (``{name: {"value": v}}`` and ``{name: v}``); the DECISION lives here once, so a third
    producer cannot inherit half of it.
    """
    degraded = False
    for name in mapping:
        try:
            _sc.assert_wire_safe(get(mapping, name))
        except Exception:  # noqa: BLE001 — null the offender and disclose
            put(mapping, name, None)
            degraded = True
    return degraded


def _sanitise_readback(readback):
    """Null any read-back value the wire cannot carry. ``(readback, degraded)``.

    A wire fault on a VERIFIED success DEGRADES the REPORT; it does not raise and it does
    not restore. ``ok`` states the DESIGN truth: the write landed and was T1/T2-proven,
    and an ``ok: false`` over a landed verified write invites a retry loop, while a
    rollback would convert a print defect into a design mutation through the
    least-exercised code in the tool.
    """
    if readback is None:
        return None, False
    return readback, _null_unsafe(
        readback,
        lambda m, k: m[k]["value"],
        lambda m, k, v: m[k].__setitem__("value", v))


def _replaced_fields(prior_fields):
    """``replaced_solve["fields"]`` — rendered, then sanitised. ``(fields, degraded)``.

    The third of the three sites that each carried their own copy of the ``Column``
    coercion; it now reads the shared rule.
    Note the ``_null_unsafe`` pass below is NOT redundant with it: ``to_wire`` handles the
    ONE field measured to need coercion, and ``_null_unsafe`` is the backstop for anything
    unmeasured (``ZPLMacro``'s ``Macro`` is the pair no live run has reached).
    """
    out = {k: to_wire(k, v[0]) for k, v in prior_fields.items()}
    return out, _null_unsafe(out, lambda m, k: m[k],
                             lambda m, k, v: m.__setitem__(k, v))


def _emit(envelope, surface, token):
    """Final wire assertion over the WHOLE envelope. A second failure RAISES.

    Its first clause states that the write LANDED and was verified, because the caller's
    design is fine and only the report is not.
    """
    try:
        _sc.assert_wire_safe(envelope)
    except Exception as exc:  # noqa: BLE001 — the second failure raises
        raise SurfaceWriteError(
            "the write LANDED on the '%s' cell of surface %s and was verified, but the "
            "success report could not be made wire-safe even after nulling the offending "
            "field values (%r). Re-read the cell with read_surface; nothing was rolled "
            "back." % (token, surface, exc),
            field=token, intended="a wire-safe envelope", actual=None,
            surface=surface) from exc
    return envelope


# --------------------------------------------------------------------------- #
# The shared transaction. ONE window; both handlers run it.
# --------------------------------------------------------------------------- #
def _transact(session, system, lde, surface, token, cell, member, canonical, resolved,
              prior, none_create_error, verify):
    """Author inside the window, run T1 and ``verify``, restore on ANY post-invoke failure.

    THE TWO FLAGS ARE SPLIT ON PURPOSE. ``set_entered`` is the truth about the invoke and
    flips ONLY through ``on_mutate``; ``restore_needed`` is what the ``except`` consults
    and is ADDITIONALLY pre-set for a family MEASURED to mutate during a pre-invoke step.
    Collapsing them into one flag makes the conservative pre-set indistinguishable from a
    lie about whether the engine call was entered.

    ``set_entered`` IS NOW READ: it is handed to the restore, which puts it in
    the partial-state message. Until then the flag was written twice and read NOWHERE, so
    the distinction the paragraph above argues for was one nothing could observe — the
    docstring described a design the code did not implement.

    THE SIX PRE-``SetSolveData`` EXITS NEED NO RESTORE on every family here, and that is a
    probe result rather than a theorem: ``_CONSERVATIVE_FAMILIES`` is empty BY MEASUREMENT
    and the branch stays for the engine where it is not.
    """
    set_entered = False
    restore_needed = token in _CONSERVATIVE_FAMILIES

    def _mark():
        nonlocal set_entered, restore_needed
        set_entered = True
        restore_needed = True

    try:
        author_solve(system, cell, member, resolved, surface=surface, cell_label=token,
                     on_mutate=_mark, none_create_error=none_create_error)
        _t1(system, lde, surface, token, member, canonical)
        return verify()
    except BaseException as exc:
        if not restore_needed:
            raise
        _restore(session, system, lde, surface, token, prior, set_entered)
        if not isinstance(exc, Exception):
            raise
        raise SurfaceWriteError(
            "%s The prior %s solve was restored and verified."
            % (exc, prior["type"]),
            field=token, intended=canonical, actual=None, surface=surface) from exc


# =========================================================================== #
# set_solve
# =========================================================================== #
def set_solve(session, params):
    """Author a relationship solve. RAISES on every refusal; returns a dict on success."""
    from . import _solve_trace as _st          # lazy: see the ``_t2`` cycle note
    system, lde, surface, token, cell, legal = _prelude(session, params)
    requested_type = params.get("solve_type")
    if not isinstance(requested_type, str) or not requested_type:
        raise ToolParamError(
            "solve_type must be a non-empty string; got %r" % (requested_type,))
    # V4 — resolve getattr-only and canonicalise BEFORE any cell is touched. This step IS
    # the alias normaliser: ``ConcentricRadius`` renders ``CocentricRadius``, one .NET
    # value under two proxies, so no alias table exists anywhere in this design.
    member = _resolve_enum(_sc.solve_type_enum(system), requested_type)
    canonical = str(member)
    if canonical in _NOT_AUTHORED_HERE:
        _reroute(canonical, cell, surface, token, legal)
    if canonical not in legal:
        # Canonical on BOTH sides: the live list entries are already canonical renders and
        # V4 canonicalised the caller's spelling, so a legal alias can never be falsely
        # refused here.
        _refuse_type(
            "the '%s' cell of surface %s does not offer a %s solve; its live legal set "
            "is %s." % (token, surface, canonical, legal), legal)
    raw = params.get("fields")
    raw = {} if raw is None else raw
    if canonical == "SurfacePickup" and isinstance(raw, dict):
        _require_pickup_fields(token, raw)
    resolved = _resolve_fields(system, lde, surface, token, canonical, raw)
    prior = _capture(cell, lde.GetSurfaceAt(surface), token, surface)

    def _verify():
        """THE SOLE RAISE-VS-RETURN DECISION SITE for a relation verdict.

        Both arms live here and nowhere else, so "which disagreements roll back" is one
        readable decision rather than a policy distributed across two modules. The tier
        itself NEVER raises: a read fault inside it would otherwise reach ``_transact``,
        which treats any post-invoke exception as a failure and RESTORES — rolling back a
        correct write because an operand would not read.

        ``gates`` IS CONSULTED ONLY ON ``traced_mismatch`` — only when the oracle actually
        RAN and actually disagreed. On ``traced_match`` it decides nothing (it describes
        the check, and that description is the ``disagreement_gated`` key), and on the
        three degraded states there is no disagreement to gate.
        """
        relation = _t2(system, lde, surface, token, canonical, resolved, prior["type"])
        if relation["state"] == "mismatch":
            raise SurfaceWriteError(
                "the %s pickup on surface %s did not do what you asked: expected %r, the "
                "cell reads %r. The engine accepted it and reported Success. The state it "
                "actually wrote was [%s]."
                % (token, surface, relation["expected"], relation["actual"],
                   _attempted_snapshot(system, lde, surface, token, canonical)),
                field=token, intended=canonical, actual=None, surface=surface)
        if relation["state"] == "traced_mismatch" and _st.gates_for(token, canonical):
            # THE DISCLOSURE TRAVELS THIS ROUTE TOO. An earlier amendment bought
            # ``read_location_proof`` for the one cell where the tier PROCEEDS on a
            # degraded read, and wired it only to the tail that RETURNS — i.e. to
            # the exit where proceeding is harmless, and not to this one, where proceeding
            # ROLLS BACK a correct authoring. A caller was told "your solve did not do what
            # you asked" with the one fact that would let them distrust that verdict
            # withheld. Appended as an EXPRESSION (+0 statements; the file is at its
            # statement ceiling).
            #
            # SO THE DISCLOSURE NOW HAS TWO RENDERINGS AND ONE PRODUCER: an envelope key on
            # the disclose paths, this message text on the gated raise. That asymmetry is
            # accepted rather than unified — a shared carrier costs statements the ceiling
            # does not have — and is recorded here so nobody rediscovers it as a defect.
            #
            # IT IS SAFE TO INTERPOLATE ONLY BECAUSE OF THE ``_safe_repr`` FIX ABOVE: the
            # proof is built from guarded renders, so it is an exact ``str``. Against the
            # Helper this very line could have raised inside ``_verify`` and been
            # converted by the net into a false ``traced_unreadable``. Order matters.
            raise SurfaceWriteError(
                "the %s solve on surface %s did not do what you asked: %s. The engine "
                "accepted it and reported Success. This row GATES because a disagreement "
                "population was MEASURED and separated from the agreement population "
                "(9.09e+07 apart), so the threshold sits in a measured gap.%s"
                % (canonical, surface, relation.get("reason"),
                   (" " + relation["read_location_proof"])
                   if relation.get("read_location_proof") else ""),
                field=token, intended=canonical, actual=None, surface=surface)
        return relation

    relation = _transact(session, system, lde, surface, token, cell, member, canonical,
                         resolved, prior,
                         lambda: ToolParamError(
                             "CreateSolveType(%s) returned None on the '%s' cell of "
                             "surface %s even though the cell's own legal set lists it "
                             "(%s) — the legal list and the factory disagree. Nothing was "
                             "written." % (canonical, token, surface, legal)),
                         _verify)
    readback, degraded = _sanitise_readback(
        _t3(system, lde, surface, token, canonical, resolved))
    envelope = {"ok": True, "surface": surface, "cell": token,
                "solve_type": canonical}
    if requested_type != canonical:
        envelope["requested_type"] = requested_type
    envelope["prior_solve"] = prior["type"]
    if not _sc.is_default_solve(token, prior["type"]):
        replaced_fields, replaced_degraded = _replaced_fields(prior["fields"])
        degraded = degraded or replaced_degraded
        envelope["replaced_solve"] = {
            "type": prior["type"], "was_driving": _sc.is_driving(prior["type"]),
            "fields": replaced_fields}
    if prior["type"] == canonical:
        envelope["same_type_reauthor"] = True
    envelope["fields_readback"] = readback
    if readback is None:
        envelope["fields_readback_unreadable"] = True
    omitted = [n for n in _sc.lookup(token, canonical)[1] if n not in resolved]
    if omitted:
        envelope["fields_inherited" if prior["type"] == canonical
                 else "fields_defaulted"] = omitted
    if degraded:
        envelope["fields_unwire_safe"] = True
    if canonical == "SurfacePickup" and resolved.get("Surface", -1) > surface:
        envelope["forward_reference"] = True
    # THE THREE DERIVED FACTS, from their ONE producer, zipped onto their frozen names.
    # Written this way and not as three literal assignments so the key names exist in
    # exactly one module: a fact asserted at an emit site is a second authority, and the
    # difference between "this check would have rolled back" and "this check did roll
    # back" is precisely the kind of claim that drifts when it is written twice.
    relation.update(zip(_st.DERIVED_KEYS,
                        _st.relation_semantics(relation["state"],
                                               _st.gates_for(token, canonical))))
    if relation["state"] == "traced_mismatch":
        # Reachable ONLY on a NON-gating row: a gating traced_mismatch raised inside
        # ``_verify`` and never returns here. ``ok`` stays true and nothing was rolled
        # back, so the disagreement has to be loud in the envelope or an agent reading
        # only ``ok`` proceeds on a solve that did not deliver.
        # THE PROVENANCE FIGURE IS ATTRIBUTED TO BOTH SCOPES IN ONE SENTENCE.
        # It read "n=0 across 30 authorings" about "this row", but 30 is the total
        # across BOTH height rows and each row's own population is 15. Swapping one
        # literal for the other would just be falsified differently the day a third row is
        # added, so both scopes are stated. The figure is PROVENANCE:
        # the test pins this SENTENCE and never asserts the population numerically.
        envelope["traced_relation_warning"] = (
            "the %s solve on the '%s' cell of surface %s LANDED and its TYPE was proven, "
            "but the paraxial oracle DISAGREED: %s No disagreement population has ever "
            "been OBSERVED on this row (n=0 on this row: 15 authorings; 30 across both "
            "height rows), so it does not gate — nothing was rolled back and ok is true. "
            "Read `relation` for the operand, the ray and the residual."
            % (canonical, token, surface, relation.get("reason")))
    envelope["relation"] = relation
    if _sc._mce_overridden(system):
        envelope["mce_overrides_not_audited"] = True
    return _emit(envelope, surface, token)


# =========================================================================== #
# clear_solve
# =========================================================================== #
def clear_solve(session, params):
    """Remove the solve, targeting the cell's OWN engine default. RAISES on refusal.

    THE TARGET IS PER-CELL, NOT A BLANKET ``Fixed``: ``semi_diameter``'s default is
    ``Automatic``, so its arm RE-FLOATS the aperture and agrees BY RULE with
    ``freeze_semidiameters(mode='auto')``. A blanket-``Fixed`` table would freeze the
    aperture at whatever number it happened to hold and call that "cleared".

    A CLEAR FREEZES; IT DOES NOT RESET. The cell keeps its CURRENT value, and the
    relationship the solve expressed is gone.

    THE V7/V8 GATES RUN ON THE HARNESS-CHOSEN TARGET and a failure is ``surface_write``,
    never ``tool_param``: the caller typed no type, so a GATE failure is engine drift and
    the ``accepted_here`` remedy suffix (a ``set_solve`` remedy) is not appended. Every
    one of the five targets is present in its cell's measured legal set AND catalogued,
    so the gates refuse nothing legitimate — they are drift armour, and they are what
    makes a wrong future edit to the default table refuse LOUD instead of authoring blind.

    THAT SENTENCE IS ABOUT THE GATES, AND F-E IS WHY THE QUALIFIER IS NOW EXPLICIT. An
    unresolvable ``SolveType`` enum is an ENVIRONMENT fault, not a gate verdict, and it
    now reaches the caller as ``tool_param`` — the same family ``set_solve`` has always
    served for it. Read broadly, the paragraph above used to promise the opposite about a
    condition it was not written for (0.1.6 PR #8 review batch, F-E).
    """
    system, lde, surface, token, cell, legal = _prelude(session, params)
    target = _sc.DEFAULT_SOLVE_BY_CELL[token]
    # F-E — THE ENUM RESOLUTION IS HOISTED OUT OF THE ``try`` ON PURPOSE, and the +1
    # statement is the whole fix. ``_sc.solve_type_enum`` raises ``ToolParamError`` on a
    # live-backend IMPORT failure ("could not resolve SolveType from ZOSAPI.Editors"),
    # which is an ENVIRONMENT fault, not a write fault. Inside the try it was re-familied
    # as ``surface_write`` here while ``set_solve`` — which calls the SAME resolver bare,
    # one function down — served ``tool_param`` for the identical condition. Two doors,
    # one fault, two families is a machine-readable signal an agent cannot branch on.
    # Only ``_resolve_enum``'s member-absent case is re-familied now, which is ALSO what
    # makes the message below TRUE: it asserts "the enum has no 'Fixed' member", and
    # before the hoist it said that about an enum that could not be RESOLVED AT ALL.
    #
    # THE REVIEW ALSO CLAIMED A ``SurfaceColumn`` SPLIT OF THE SAME SHAPE. That is
    # REFUTED (§1, F-E): both doors resolve SurfaceColumn through the shared
    # ``_prelude``, so there is one family and nothing to split. Do not "fix" it.
    solve_enum = _sc.solve_type_enum(system)
    try:
        member = _resolve_enum(solve_enum, target)
    except ToolParamError as exc:
        raise SurfaceWriteError(
            "the engine's SolveType enum has no %r member, so the '%s' cell's own "
            "default cannot be authored (%s); nothing was written"
            % (target, token, exc),
            field=token, intended=target, actual=None, surface=surface) from exc
    canonical = str(member)
    if canonical not in legal or _sc.lookup(token, canonical)[0] == "unmeasured":
        raise SurfaceWriteError(
            "the '%s' cell of surface %s does not offer its own default %s solve (live "
            "legal set %s) or the pair is uncatalogued — engine drift on a "
            "harness-chosen target. Nothing was written."
            % (token, surface, canonical, legal),
            field=token, intended=canonical, actual=None, surface=surface)
    prior_type = _sc.read_solve_type(cell)
    if prior_type is None:
        raise SurfaceWriteError(
            "the current solve on the '%s' cell of surface %s could not be read, so "
            "nothing can be reported as cleared and nothing could be restored. Read "
            "`solves` on read_surface: this cell will be listed under "
            "`solves_unreadable`. Nothing was written." % (token, surface),
            field=token, intended="a readable incumbent solve", actual=None,
            surface=surface)
    if prior_type in _sc.SUPPRESSED_BY_CELL[token]:
        # The idempotent no-op. ``solve_type`` is the HONEST READ-BACK — emitting the
        # TARGET here would be a false read-back on a ``"None"`` incumbent, which is the
        # one reading in this arm that differs from the target.
        return _emit({"ok": True, "surface": surface, "cell": token,
                      "prior_solve": prior_type, "cleared": False,
                      "solve_type": prior_type, "frozen_at": None,
                      "already_default": True,
                      "is_default_now": _sc.is_default_solve(token, prior_type),
                      "note": "the '%s' cell already carries its default %s solve; "
                              "nothing was written." % (token, prior_type)},
                     surface, token)
    prior = _capture(cell, lde.GetSurfaceAt(surface), token, surface, "clear_solve")
    _transact(session, system, lde, surface, token, cell, member, canonical, {}, prior,
              lambda: SurfaceWriteError(
                  "CreateSolveType(%s) returned None on the '%s' cell of surface %s — "
                  "the engine will not build this cell's own default solve. Engine "
                  "drift; nothing was written." % (canonical, token, surface),
                  field=token, intended=canonical, actual=None, surface=surface),
              lambda: None)
    refloated = canonical == "Automatic"
    frozen_cell = not (refloated or token == "material")
    value, readable = _read_value_tagged(lde.GetSurfaceAt(surface), token) \
        if frozen_cell else (None, True)
    # A PLANO surface's radius is a genuine, ordinary ``inf`` — and the wire refuses a
    # non-finite float, because a NaN in an emitted block makes two identically-produced
    # blocks compare UNEQUAL. So the number is reported as null WITH the substrate's own
    # ``not_representable`` vocabulary rather than being coerced, dropped silently, or
    # (the first cut's behaviour) escalated into a refusal of a perfectly good clear.
    # The prose ``note`` still names the real reading.
    #
    # ``frozen_at: null`` NOW HAS TWO DISJOINT CAUSES AND SAYS WHICH. A
    # non-finite reading is a FACT about a perfectly good plano cell; a FAILED read is an
    # alarm — the number the caller is told is "now baked in" was never read at all. A
    # first cut emitted the identical ``frozen_at: null`` for both.
    # THE POST-CLEAR TYPE READ GOES THROUGH ``_observe``, WHICH NEVER RAISES, AND THAT IS A
    # DISCLOSURE-DISCIPLINE FIX, NOT A STYLE ONE. It read `_sc.solve_cell(...)` directly —
    # a RAISING fetch whose own message is "refusing rather than reading an unknown cell" —
    # executed AFTER `_transact` has run `_t1` and PROVEN the clear landed. A throw there
    # served a caller the word "refusing" about a write that had already succeeded, and
    # routed it to `internal` with no clause saying the cell was changed. The file states
    # the opposite rule twice and implements it on `set_solve`'s tail (`_t3` guards the
    # identical read; `_emit` degrades a wire fault on a VERIFIED success); `clear_solve`'s
    # tail was the one place it was missing (0.1.6 external review).
    representable = not (isinstance(value, float) and not math.isfinite(value))
    envelope = {"ok": True, "surface": surface, "cell": token,
                "prior_solve": prior["type"], "cleared": True,
                "solve_type": _observe(system, lde, surface, token)[0],
                "frozen_at": value if (representable and readable) else None}
    if not readable:
        envelope["frozen_at_unreadable"] = True
    elif not representable:
        envelope["frozen_at_not_representable"] = True
    # F-B, THE DISCLOSURE HALF. The correctness half (``is_default_now`` reading null
    # rather than a manufactured ``false``) is twenty lines down and was already closed;
    # this is what stops the resulting ``solve_type: null`` being SILENT. The precedent is
    # the two-cause ``frozen_at`` pair directly above: a null that a caller cannot
    # distinguish from a fact needs a key saying which it is.
    #
    # THE KEY MEANS "THE READ FAILED", AND ONLY THAT. ``read_solve_type`` returns ``None``
    # ONLY on a throw — there is no ABSENT reading, because every cell has a solve type
    # and ``_observe`` is the never-raise reader that degrades a throw to null. So this is
    # NOT the ABSENT-vs-UNREADABLE split applied to a third case; it is the
    # UNREADABLE arm of it, named, on a call that otherwise reports ``ok`` and ``cleared``.
    if envelope["solve_type"] is None:
        envelope["solve_type_unreadable"] = True
    if refloated:
        envelope["refloated"] = True
    if prior["type"] == "Variable":
        envelope["cleared_variable"] = True
    # ``is_default_now`` IS NULL WHEN THE TYPE COULD NOT BE READ, NOT ``false``.
    # `is_default_solve` returns False for any non-`str`, so an unreadable type produced
    # `{"solve_type": null, "is_default_now": false}` — an AFFIRMATIVE claim that the cell
    # is NOT at its default, manufactured from a fault. Worse, it contradicted a proof this
    # same call already holds: `_t1` read the type back and REFUSED unless it equalled
    # `canonical`, which IS the cell's default, three statements earlier. This is not the
    # ABSENT-vs-UNREADABLE conflation the review filed it as — `read_solve_type` has no
    # ABSENT reading, every cell has a type and `None` means fault and nothing else — it is
    # a false assertion derived from one. Null is the honest answer; the `frozen_at` pair
    # one line up already models the same discipline (0.1.6 external review).
    envelope["is_default_now"] = (
        _sc.is_default_solve(token, envelope["solve_type"])
        if envelope["solve_type"] is not None else None)
    if refloated:
        envelope["note"] = (
            "the %s solve was cleared and the semi-diameter RE-FLOATS — the engine now "
            "recomputes it (the freeze_semidiameters(mode='auto') state)."
            % (prior["type"],))
    elif token == "material":
        # THE NOTE IS BUILT FROM THE MEASUREMENT, NOT AHEAD OF IT. An earlier cut proved
        # LIVE that a ``MaterialModel`` clear CHANGES the glass — the author rewrites the
        # string to ``1.50,60.0`` and the clear leaves ``K7`` — and folded that into the
        # structured field while leaving this sentence promising the opposite, so ONE
        # success envelope could carry ``glass_unchanged: false`` beside "The surface
        # KEEPS its glass." The note is therefore deferred until the comparison below has
        # actually been made, and each of the three outcomes gets its own words.
        envelope["note"] = None
    else:
        envelope["note"] = (
            "the %s solve was cleared. The cell is now FROZEN at its current value %s — "
            "clearing does not restore a default or an earlier number, and the "
            "relationship the solve expressed is gone."
            % (prior["type"], value if readable else "(the value could not be read back)"))
    if token == "material":
        # ``glass_unchanged: true`` was UNCONDITIONAL — asserted, never
        # measured — so a clear that changed ``N-BK7`` to ``""`` reported the glass
        # preserved, and a FAILED read reported ``material: null`` beside the same true
        # claim. The claim is now EARNED by comparing the captured incumbent against the
        # read-back, and an unreadable read-back is UNKNOWN (``null``), never ``true`` and
        # never ``false`` — the ABSENT-vs-UNREADABLE split at claim scale.
        material, material_readable = _read_value_tagged(
            lde.GetSurfaceAt(surface), token)
        envelope["material"] = material if material_readable else None
        # BOTH SIDES MUST HAVE BEEN READ. A first cut earned the claim from the AFTER
        # read and left the BEFORE read collapsing a FAULT to ``None`` — the capture's
        # refusal covers only ``Fixed``/``Variable``, so a driving ``MaterialModel`` prior
        # whose pre-read faulted reached here with a FABRICATED ``None`` to compare
        # against. Measured consequence: an UNCHANGED ``N-BK7`` reported
        # ``glass_unchanged: false`` and warned the glass changed "from None to 'N-BK7'".
        # A comparison is only as true as its worse half, so an unreadable EITHER side is
        # UNKNOWN — the same split as above, applied to both operands instead of one.
        both_read = material_readable and prior.get("value_readable", True)
        envelope["glass_unchanged"] = (
            material == prior["value"] if both_read else None)
        if not material_readable:
            envelope["material_unreadable"] = True
        elif not both_read:
            envelope["material_prior_unreadable"] = True
        elif envelope["glass_unchanged"] is False:
            envelope["material_changed_warning"] = (
                "clearing the solve CHANGED the glass from %r to %r — that is not what "
                "this tool promises. Use substitute_glass to set the material you want."
                % (prior["value"], material))
        envelope["note"] = _material_note(prior, material,
                                          envelope["glass_unchanged"])
    if _sc._mce_overridden(system):
        envelope["mce_overrides_not_audited"] = True
    return _emit(envelope, surface, token)


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
SET_SOLVE_SPEC = ToolSpec(
    name="set_solve",
    handler=set_solve,
    required_params=("surface", "cell", "solve_type"),
    param_types={"surface": "number", "cell": "string", "solve_type": "string",
                 "fields": "object"},
    description=(
        "Author a relationship solve on a surface's radius / thickness / conic / "
        "semi_diameter / material cell — the engine then computes that value from the "
        "relationship instead of holding a fixed number. Validates solve_type against "
        "the cell's OWN live legal set and refuses a type it does not offer, naming the "
        "set. The field names are the ones read_surface prints under solves.<cell>.fields. "
        "A SurfacePickup needs Surface, Column, ScaleFactor and Offset all stated "
        "explicitly; the tool then verifies the arithmetic you stated against an "
        "independent read of the source cell, and rolls back if it disagrees. Column takes "
        "a cell token (radius, thickness, conic, semi_diameter, material) or the "
        "equivalent SurfaceColumn name (Radius, Thickness, Conic, SemiDiameter, Material) "
        "— those ten are the whole vocabulary; anything else is refused and the refusal "
        "names the set. Replaces an "
        "existing solve and reports what it replaced (replaced_solve) — that relationship "
        "is unrecoverable after. Gotcha: it does NOT author Variable (use set_variable), "
        "Fixed or None (use clear_solve), or Automatic (clear_solve on semi_diameter). "
        "GOTCHA, the one that silently authors the wrong design: a radius pickup scales "
        "CURVATURE, so the formula is R_target = R_source / ScaleFactor. ScaleFactor=2 "
        "gives HALF the source radius; for TWICE the radius pass ScaleFactor=0.5. The "
        "tool verifies the relationship you EXPRESSED, not the one you meant, so a "
        "wrong-way factor returns relation:verified — check scaled_quantity in the result "
        "(it reads 'curvature' on a radius target, 'value' otherwise). An Offset "
        "is only honoured on a thickness target. On failure after the write it restores "
        "the prior solve and says so; error_family solve_partial_state means the cell's "
        "state is UNKNOWN and you should reload your last saved design. See clear_solve, "
        "read_surface, set_variable."
    ),
)

CLEAR_SOLVE_SPEC = ToolSpec(
    name="clear_solve",
    handler=clear_solve,
    required_params=("surface", "cell"),
    param_types={"surface": "number", "cell": "string"},
    description=(
        "Remove the solve on a surface's radius / thickness / conic / semi_diameter / "
        "material cell, returning it to that cell's own engine default. On radius, "
        "thickness and conic it FREEZES the cell at its CURRENT value (frozen_at reports "
        "the number now baked in) — it does not restore a default or an earlier number, "
        "and the relationship the solve expressed is gone. On semi_diameter it instead "
        "re-floats the aperture, the same state freeze_semidiameters(mode='auto') "
        "produces. On material it clears the SOLVE and REPORTS whether the glass "
        "survived (glass_unchanged true/false/null, with the before and after strings) — "
        "some material solves own the glass string, so it is not always preserved; use "
        "substitute_glass to set the glass you want. Reports cleared:false without writing "
        "anything when the cell is already at its default, and cleared_variable when the "
        "solve it removed was an optimizer degree of freedom. Read solves on read_surface "
        "first to see what is there. See set_solve, read_surface."
    ),
)

TOOL_SPECS = (SET_SOLVE_SPEC, CLEAR_SOLVE_SPEC)
