"""tools/clearance.py — the post-optimize manufacturability / detector-clearance audit.

ONE dispatchable READ-ONLY tool ``check_clearance`` (geometry-readouts cycle).
Fixes the iterative Cooke gap #1 (the DETECT-side net for a negative/thin
center or EDGE thickness) + Cassegrain gap #2 (a folded system's raw back-airgap
``back_focal_length`` is NOT the behind-primary clearance — report the GLOBAL BFD).

The audit is geometry from READ-BACK only (LDE radius/conic/thickness/semi-diameter
+ ``GetGlobalMatrix``); it NEVER mutates the LDE and NEVER raises past its boundary.
It REUSES ``_layout_geometry`` (the sag profile, the global-frame read, the fold
predicates, the role classifier) + ``_clearance_common`` (the edge-thickness math)
so the geometry reads can never drift from the renderer.

Structure:

1. **Fold detection** — ``folded = True`` if ANY surface (ALL indices, incl. 0 and
   n-1) is a coordinate-break OR a mirror, via the ONE shared predicate
   ``_layout_geometry.lde_is_folded`` (NOT the role classifier — which stamps
   object/image at the ends and would miss a CB at surface 0 / a mirror at the image
   surface). The SAME predicate ``get_first_order`` uses, so the two readout tools'
   fold decision can never drift. Drives the per-gap audit's applicability.
2. **Per-gap thickness audit (UNFOLDED only)** — for each real gap ``i -> i+1``
   (skip the object gap; skip + flag a gap with no finite-positive aperture):
   ``kind`` glass/air; ``center_thickness`` = LDE thickness_i; ``edge_thickness`` via
   the probe formula; a ``violation`` when center OR edge ``< threshold`` (min_glass
   for glass, min_air for air) or NEGATIVE. The back-airgap (last optic -> image) is
   audited as an air gap. A read-only audit REPORTS (``ok:true``), never refuses.
3. **Folded systems** — emit NO per-gap thickness VIOLATIONS (the -525.7 fold trap);
   report per-gap ``center_thickness`` as INFORMATIONAL ``folded_gaps`` + a ``note``.
4. **Global BFD (BOTH)** — ``image_global_z`` (slot [12]); ``behind_first_optic`` /
   ``behind_last_optic`` (image_global_z - first/last OPTICAL surface global z); the
   first/last optical surface numbers. A degraded global frame -> that field ``None``
   + a flag (never a bogus number).

Envelope: ``{ok, folded, gaps, violations, folded_gaps, global_bfd, flags}``.
``min_air``/``min_glass`` validated (finite >= 0; nan/inf/negative/bool ->
``clearance_param``); a total geometry-read failure -> ``clearance_unavailable``.

Live ZOS-API integration: exercised by the geometry-readouts live test (the Cooke
BEST 0-violations + ~1.12 flint edge; the Cassegrain folded behind_first_optic
~160); unit-tested against the ``_describe_render_fakes`` LDE/GetGlobalMatrix doubles.
"""
import math

from ..errors import ToolParamError
from ..server import ToolSpec
from . import _asphere_cells as _asph
from . import _clearance_common as _cl
from . import _config_common as _cfg
from . import _layout_geometry as _geom
# The STRICT active-config primitive. NOT a second derivation: this is the SAME
# function ``_config_common.safe_current_configuration`` wraps -- one read, two
# consumption policies (see ``resolve_evaluated_config``).
from . import _mce_cells as _mc
# V-INT D1-b: the ACTIVE-MERIT ceiling reader. Owns the.. decision list, the
# three-state lookup vocabulary and the scan budget; this module owns only WHEN to ask.
from . import _merit_ceiling as _mceil
from ._analysis_common import error_envelope

_CL_FAMILY = "clearance_unavailable"   # a total geometry-read failure
_CL_PARAM = "clearance_param"          # a bad min_air/min_glass value

_DEFAULT_MIN_AIR = 0.5     # matches build_merit's MNCA air floor
_DEFAULT_MIN_GLASS = 1.0   # matches build_merit's MNCG glass floor


def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _finite_nonneg(value, label, default):
    """Validate a clearance threshold: a FINITE number ``>= 0`` (locked §5).

    Rejects ``bool`` (an int subclass — a client miswrite), a non-number, inf/-inf/
    nan, and a negative value -> ``ToolParamError`` (the caller envelopes it as
    ``clearance_param``). A missing key uses ``default``. Returns the float.

    THE INVARIANT THIS FUNCTION ESTABLISHES: together with
    ``resolve_floors``, **the ONLY exception class that can leave the resolver is
    ``ToolParamError``**. That single-class guarantee is what licenses
    ``workspace._effective_floors``' single-class ``except`` — and ``save_candidate``,
    which documents "NEVER raises", has NO outer net, so a second escaping class there
    is a broken contract, not a cosmetic nit.

    The coercion is guarded because the type gate ADMITS values ``float()`` cannot
    represent: a huge Python ``int`` (measured: ``10**400``) passes
    ``isinstance(value, (int, float))`` and then ``float(value)`` raises
    ``OverflowError``, which is NOT a ``ToolParamError``. Fixed HERE, at the root, and
    not at the two call sites — two patches for one root is how this project has
    repeatedly bred siblings.
    """
    if value is None:
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{label} must be a finite number >= 0, got {type(value).__name__} "
            f"{value!r}"
        )
    try:
        coerced = float(value)
    except Exception:
        # ROUND 3 — the CONVERGED finding (an external review + an internal one
        # which DEMONSTRATED it). This was ``except (OverflowError, ValueError)``,
        # which is NOT exhaustive over what ``float()`` can raise: the type gate above
        # admits ``int``/``float`` SUBCLASSES, and a subclass whose ``__float__``
        # misbehaves raises something else entirely. Both measured, by fuzzing the real
        # tool:
        #
        #     save_candidate min_air=<int subclass, __float__ -> str>  -> TypeError
        #     save_candidate min_air=<float subclass, __float__ raises> -> ZeroDivisionError
        #
        # Each escaped this resolver, escaped ``_effective_floors``' single-class
        # ``except``, and escaped ``save_candidate`` — which has NO outer net precisely
        # BECAUSE of the single-class guarantee documented above. So the guarantee was
        # false, and the docstring's categorical claim was an OVERCLAIM of exactly the
        # kind this cycle exists to close.
        #
        # Broadened rather than narrowing the claim: NOTHING else is inside this ``try``,
        # and every possible failure of ``float(value)`` means the same thing — the
        # caller named a threshold this module cannot apply — which is already the
        # ``ToolParamError`` answer. So widening makes the stated invariant TRUE instead
        # of documenting a hole. (Not reachable across the MCP boundary, where JSON
        # yields plain ints/floats; reachable in-process, and the claim was categorical.)
        raise ToolParamError(
            f"{label} must be a finite number >= 0 (this value cannot be "
            f"represented as a float), got {type(value).__name__}"
        )
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"{label} must be finite >= 0 (inf/-inf/nan are not a clearance "
            f"threshold), got {value!r}"
        )
    if coerced < 0.0:
        raise ToolParamError(f"{label} must be >= 0, got {coerced}")
    return coerced


def resolve_floors(params):
    """The ``(min_air, min_glass)`` this module will apply. RAISES ``ToolParamError``.

    AND NOTHING ELSE FROM ITS OWN LOGIC. Every refusal this function itself reaches
    is a ``ToolParamError`` — the guarantee
    ``workspace._effective_floors``' single-class ``except`` rests on, and therefore
    the guarantee that ``save_candidate`` (no outer net, documented "NEVER raises")
    keeps its contract.
    ``test_a5_resolve_floors_raises_ONLY_ToolParamError`` asserts the CLASS over a
    pathological corpus of values, not a message. That regression lives in the
    development suite and is not shipped with this package.

    THE LIMIT, MEASURED — and stated in full because TWO successively weaker versions
    of this claim have already been written here and BOTH were still false. The
    guarantee is over what this function DECIDES. It is not a guarantee about
    everything that can leave the frame, because building a refusal MESSAGE runs code
    the caller owns. Two escapes are known; neither is closed here:

    * ``__repr__`` RUNS DURING THE REFUSAL. Both ``{value!r}`` sites below format the
      rejected value, so a hostile ``__repr__`` propagates whatever it raises — from
      inside a PLAIN ``dict``, which is what makes this the sharper of the two.
      Measured: a non-number whose ``__repr__`` raises leaves as ``RuntimeError`` at
      the type gate; an ``inf`` ``float`` SUBCLASS whose ``__repr__`` raises leaves as
      ``ZeroDivisionError`` at the finiteness refusal. Both ``{value!r}`` sites are
      already-published code, so the repr-safe message is a follow-up rather than an
      edit smuggled in under a prose fix.
    * ``_require_dict`` accepts any ``dict`` SUBCLASS and the two ``params.get(...)``
      calls below are unguarded, so a subclass whose ``get`` raises escapes as well.

    No caller constructs either — ``params`` arrives off the wire as a plain ``dict``
    of JSON scalars — but the honest predicate is "total over well-behaved values",
    never "total".

    THE single resolver. ``check_clearance`` calls it, and
    ``workspace._effective_floors`` calls it so the audit RECORD a save writes and the
    guard a promote applies come from ONE acceptance set. Sharing the constants and the
    validation PRIMITIVE is NOT sharing a resolver — the defaulting, the param lookup
    and the tuple construction must live here too, or the producer and the guard have
    two acceptance sets that can diverge.

    ``_finite_nonneg`` always returns a ``float`` (``float(default)`` / ``float(value)``)
    and already rejects ``bool``, so the returned pair is ``(float, float)`` BY
    CONSTRUCTION — which is what lets the record's floors be validated as exact floats
    (``True == 1.0`` and ``False == 0.0`` hold in Python, so
    an int/bool record would otherwise satisfy an "exact" floor comparison).
    """
    params = _require_dict(params)
    min_air = _finite_nonneg(params.get("min_air"), "min_air", _DEFAULT_MIN_AIR)
    min_glass = _finite_nonneg(
        params.get("min_glass"), "min_glass", _DEFAULT_MIN_GLASS
    )
    return (min_air, min_glass)


# --------------------------------------------------------------------------- #
# Tier-1 (the oracle gap): the CENTRE-THICKNESS CEILING channel.
#
# THE ONE RULE THIS WHOLE BLOCK EXISTS TO KEEP (probe P-4): a boundary operand's
# ``value`` is NOT a measurement -- a SATISFIED ``MXCA`` reports its TARGET, a violated
# one reports the actual, so reading it as a thickness is wrong on exactly the gaps that
# are FINE. Nothing here ever reads the MFE. The MEASUREMENT is this module's own
# ``center_thickness``; the CEILING is the DECLARED budget; the comparison is made here.
#
# Ceilings are CENTRE-ONLY (probe P-3): the wizard authors ``MNEA``/``MNEG`` for a
# minimum EDGE but there is no maximum-edge operand anywhere in the block, so an edge
# ceiling would be a NEW mechanism, not a mirror of an existing one.
# --------------------------------------------------------------------------- #
#: The four ``center_ceiling.state`` tokens. ``unreadable`` is FIRST-CLASS, not a
#: degenerate ``within``: ``nan > ceiling`` is ``False``, so a degraded centre would
#: otherwise read as "within budget" -- the manufactured verdict this cycle killed twice.
CEILING_WITHIN = "within"
CEILING_OVER = "over"
CEILING_UNREADABLE = "unreadable"
#: V-INT D1-b. THE CEILING SOURCE did not read -- as opposed to ``unreadable``, which
#: says THE CENTRE THICKNESS did not read. The two provenances stay APART because their
#: remedies differ (re-read the geometry vs repair/declare the budget), and collapsing
#: them is the ABSENT-vs-UNREADABLE error one level up.
CEILING_SOURCE_UNREADABLE = "source_unreadable"

#: FROZEN as a SET so a fifth token cannot be introduced without reddening a test
#: (A-STATES). The joiner branches on exactly these.
CEILING_STATES = frozenset({
    CEILING_WITHIN, CEILING_OVER, CEILING_UNREADABLE, CEILING_SOURCE_UNREADABLE,
})

#: ``center_ceiling_audit.status``.
CEILING_STATUS_COMPLETE = "complete"
CEILING_STATUS_PARTIAL = "partial"
CEILING_STATUS_NO_GAPS = "no_gaps"
CEILING_STATUS_FOLDED = "not_applicable_folded"

#: ``center_ceiling_audit.unresolved[].reason``. Two provenances, kept APART because
#: they have different remedies: the row was read and its centre is not a number
#: (``center_unreadable``, kind KNOWN) vs the row was never read at all
#: (``row_unreadable``, kind ``None`` -- audit-1: the degraded sentinel has already
#: replaced the unknown material with air-like data, so claiming a kind there would be
#: FABRICATED evidence).
#: V-INT D1-b adds a THIRD provenance: the gap was read fine and its BUDGET could not be
#: established (a tainted kind, a conflicting duplicate, an unreadable operand cell). Its
#: remedy is a merit repair or an explicit ``max_air``/``max_glass``, not a geometry
#: re-read -- which is why it is not folded into either of the two above.
CEILING_REASON_CENTER = "center_unreadable"
CEILING_REASON_ROW = "row_unreadable"
CEILING_REASON_SOURCE = "ceiling_source_unreadable"

#: ``center_ceiling_audit.basis``. ``"caller"`` = this call's own explicit
#: ``max_air``/``max_glass``; ``"declared"`` = the budget DECLARED FOR THIS DESIGN at
#: ``build_merit`` and carried on the session (owner ruling 1).
#:
#: ``"active_merit"`` (V-INT D1-b) = **the ceilings the merit function ACTIVE AT READ
#: TIME declares.** That is the whole claim, and it is true on every path BY
#: CONSTRUCTION -- which is why, unlike ``declared``, it needs no suppression machinery
#: and gets none. ``declared``'s claim had to be narrowed to the DECLARATION EVENT
#: precisely because ``load_merit`` / ``clear_merit`` / ``apply_merit_recipe(mode=
#: "replace")`` can divorce the active merit from the call that declared the budget; a
#: basis that claims only what the ACTIVE merit says cannot be divorced from it. A
#: suppression flag here would contradict ``server.py``'s shipped ruling that
#: ``IDENTITY_PRESERVING_TOOLS`` lists every merit mutator, and would go stale in the
#: permissive direction the moment a future mutator was added -- the exact defect it
#: would have been meant to prevent.
BASIS_CALLER = "caller"
BASIS_DECLARED = "declared"
BASIS_ACTIVE_MERIT = "active_merit"

#: ``center_ceiling_audit.active_merit_not_offered`` -- FROZEN reasons. The
#: ``active_merit`` basis is a SINGLE table for the configuration evaluated now, so it
#: is offered only where that is the question being asked (spec 3.7).
NOT_OFFERED_SWEEP = "multi_config_sweep"
NOT_OFFERED_NON_ACTIVE = "non_active_config"
NOT_OFFERED_CONFIG_UNREADABLE = "active_config_unreadable"

_FLOOR_KEYS = ("min_air", "min_glass")
_CEILING_KEYS = ("max_air", "max_glass")


def _finite_pos_or_none(value, label):
    """An OPTIONAL ceiling: ``None`` passes through, else a FINITE number ``> 0``.

    The ceiling counterpart of ``_finite_nonneg``, and deliberately STRICTER in one
    place: a floor of ``0`` is the documented opt-out, but a ceiling of ``0`` is not a
    budget -- it is a box no design can be inside, so it is refused as
    ``clearance_param`` rather than adjudicating every gap ``over``.

    Rejects ``bool`` (an int subclass -- a client miswrite), a non-number, inf/-inf/nan,
    and ``<= 0``. The ``float()`` coercion is guarded for the same measured reason
    ``_finite_nonneg`` guards it (a huge ``int``, an ``int``/``float`` SUBCLASS whose
    ``__float__`` misbehaves): every failure of ``float(value)`` means the caller named a
    ceiling this module cannot apply, which is already the ``ToolParamError`` answer.

    RAISES ``ToolParamError`` AND NOTHING ELSE from its own logic -- the same
    single-class guarantee ``resolve_floors`` documents, with the same two named limits
    (a hostile ``__repr__`` runs inside the refusal message; a ``dict`` subclass whose
    ``get`` raises escapes upstream of here).
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{label} must be a finite number > 0, got {type(value).__name__} "
            f"{value!r}"
        )
    try:
        coerced = float(value)
    except Exception:  # noqa: BLE001 - see _finite_nonneg: EVERY float() failure means
        raise ToolParamError(  # the caller named a ceiling this module cannot apply
            f"{label} must be a finite number > 0 (this value cannot be represented "
            f"as a float), got {type(value).__name__}"
        )
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"{label} must be finite > 0 (inf/-inf/nan are not a clearance ceiling), "
            f"got {value!r}"
        )
    if coerced <= 0.0:
        raise ToolParamError(
            f"{label} must be > 0 (a zero/negative ceiling is a box no design can be "
            f"inside, not a budget), got {coerced}"
        )
    return coerced


def _has_explicit(params, keys):
    """True iff the caller NAMED any of ``keys`` with a non-``None`` value.

    ``None`` counts as ABSENT because that is already what ``_finite_nonneg`` /
    ``_finite_pos_or_none`` mean by it (a ``None`` floor takes the default), so
    ``check_clearance(min_air=None)`` must not read as hand-driving.
    """
    return any(params.get(k) is not None for k in keys)


def _validate_box(min_air, min_glass, max_air, max_glass):
    """Refuse an EMPTY box: a supplied ceiling below its own floor (spec 3.2).

    ``max <= 0`` is already refused by ``_finite_pos_or_none``. THE SAME predicate runs
    at BOTH doors -- here on the explicit path, and at the ``build_merit`` record step
    before anything is armed -- so "the empty box is refused" holds on every path that
    can produce a verdict, not only the consuming one. Raises ``ToolParamError``.
    """
    for label, ceiling, floor, floor_label in (
        ("max_air", max_air, min_air, "min_air"),
        ("max_glass", max_glass, min_glass, "min_glass"),
    ):
        if ceiling is not None and ceiling < floor:
            raise ToolParamError(
                f"{label}={ceiling} is below {floor_label}={floor}: that is an EMPTY "
                "box (no thickness can satisfy both), not a budget"
            )


def _record_ceilings(record):
    """The CEILING PAIR a session record supplies -- ``(max_air, max_glass)`` -- or ``None``.

    **FLOORS NEVER TRAVEL. OWNER RULING.** REV 3's 3.8c recorded the build's
    floors beside its ceilings and applied the box whole, and a review's
    argued that only in the RAISING direction. In the LOWERING direction the same rule
    EMPTIED ``violations``: the agent measured one design, one session, a
    0.05 mm air gap --

        no budget -> min_air 0.5, violations [an earlier cycle]
        after build_merit(min_air=0, min_glass=0, max_glass=20)
                                                      -> min_air 0.0, violations []

    -- and fed both to the shipped joiner, where the ``looks_tight`` row is
    ``{True: MATCHED, False: CONTRADICTED}``. So a TRUE "that gap looks tight"
    observation was AUTHORITATIVELY REFUTED by the ceiling feature, on the one channel
    AXIS 2 froze. ``min_air=0``/``min_glass=0`` is not contrived: it is the documented
    target-0 opt-out ``build_merit``'s own prose tells micro-optic callers to pass.

    So the record carries ``max_air``/``max_glass`` and NOTHING else. Floors always come
    from the call's own params or defaults, exactly as before this cycle, and a session
    budget can no longer move ``violations`` in either direction.

    **WHAT REPLACES 's GUARANTEE, stated here because it is the cost of the ruling.**
     Wanted the whole box to travel so a ceiling was always validated against the floor
    it was declared with. Under ceilings-only that is gone at RECORD time, so
    ``resolve_ceilings`` re-runs the EMPTY-BOX validation at APPLY time against THE
    CALL'S floors: a recorded ``max_air=0.3`` meeting a call with ``min_air=0.5`` is a
    box no thickness can satisfy, and it refuses -- fail-closed to no-oracle, never a
    verdict in either direction.

    Every field is re-checked here even though the record step validated it at the door.
    That is not a second acceptance set -- it is a REFUSAL to trust a mutable attribute
    on a session object that any in-process caller can write. A record that fails any
    check yields ``None``, which is the no-oracle path, never a verdict.
    """
    if not isinstance(record, dict):
        return None
    ceilings = []
    for key in ("max_air", "max_glass"):
        v = record.get(key)
        if v is None:
            ceilings.append(None)
            continue
        # An exact float: ``True == 1.0`` in Python, so a bool would satisfy a numeric
        # compare while meaning something else entirely (the C7 shape).
        if isinstance(v, bool) or not isinstance(v, float) or not math.isfinite(v):
            return None
        if v <= 0.0:
            return None
        ceilings.append(v)
    if ceilings[0] is None and ceilings[1] is None:
        return None                      # a record with no ceiling declares no budget
    return tuple(ceilings)


def ceiling_is_dark_under_default_floors(max_air, max_glass):
    """True iff a recorded ceiling can NEVER be applied by a BARE ``check_clearance()``.

    THE PREDICATE IS THE REAL ONE, NOT A PROXY FOR IT. It does not compare against a
    hardcoded 0.5/1.0: it runs **the same ``_validate_box`` the apply path runs**, against
    **the same floors ``resolve_floors`` produces for a call that names none**. So if the
    shipped defaults ever move, or the empty-box rule ever changes, this answer moves with
    them by construction. A threshold literal here would be the proxy class this cycle has
    already found five times.

    WHY IT EXISTS (owner ruling). Under ceilings-only a ceiling FINER than the
    default floors is permanently unapplicable, and every route is closed for a coherent
    reason: a bare call meets the default floor and the box is empty; a call that names a
    floor is hand-driving, so the record is not consulted at all; and a call that names the
    ceiling is ``basis:"caller"`` and never needed the record. That is the micro-optic
    class this repo explicitly supports, so **the limitation stands and the SILENCE does
    not** -- ``build_merit`` discloses it at the moment of declaring.
    """
    try:
        _validate_box(*resolve_floors({}), max_air, max_glass)
    except ToolParamError:
        return True
    return False


def resolve_ceilings(params, floors, session_record=None):
    """THE single ceiling resolver -> ``((min_air, min_glass, max_air, max_glass), basis)``.

    ``basis`` is ``"caller"``, ``"declared"``, or ``None`` (no ceiling in force).

    **ALL-OR-NOTHING (spec 3.8c).** A call that names ANY floor or ceiling param is
    hand-driving, so the session record is NOT consulted at all: explicit ceilings
    resolve against THIS CALL's floors (``basis:"caller"``), explicit floors alone give a
    floor-only audit with no ceiling keys. Mixing recorded ceilings with hand-passed
    floors would recreate the cross-source defect one call later.

    **FLOORS NEVER COME FROM THE RECORD (owner ruling).** The record supplies
    ``max_air``/``max_glass`` only; ``min_air``/``min_glass`` are always this call's own
    params or defaults. See ``_record_ceilings`` for the measured verdict-manufacturing
    path that ruling closes, and for the apply-time empty-box check that replaces the
    guarantee the full-box rule used to provide.

    Only a fully BARE call reaches the record, and only a record whose four application
    preconditions already held (no explicit params, dispatch epoch equal, shape equal,
    config covered -- checked by the caller, which owns the live reads) is passed in.
    ``session_record=None`` is therefore a positive statement, and the keeper gate passes
    it BY NAME (3.8e).

    RAISES ``ToolParamError`` ONLY (the caller envelopes it as ``clearance_param``). A
    bad RECORD never raises -- it degrades to no ceiling, because a stale/corrupt record
    is an applicability question, not a caller error.
    """
    params = _require_dict(params)
    min_air, min_glass = floors
    max_air = _finite_pos_or_none(params.get("max_air"), "max_air")
    max_glass = _finite_pos_or_none(params.get("max_glass"), "max_glass")

    if _has_explicit(params, _FLOOR_KEYS) or max_air is not None or max_glass is not None:
        if max_air is None and max_glass is None:
            return (min_air, min_glass, None, None), None
        _validate_box(min_air, min_glass, max_air, max_glass)
        return (min_air, min_glass, max_air, max_glass), BASIS_CALLER

    ceilings = _record_ceilings(session_record)
    if ceilings is None:
        return (min_air, min_glass, None, None), None
    rec_air, rec_glass = ceilings
    try:
        # THE REPLACEMENT FOR 's GUARANTEE (owner ruling, ceilings-only).
        # The recorded ceilings were validated against the BUILD's floors at the record
        # door; they have never been checked against THIS call's floors, which is now the
        # only place the two meet. A recorded ceiling below the applied floor is an
        # empty box, and an empty box must never adjudicate.
        _validate_box(min_air, min_glass, rec_air, rec_glass)
    except ToolParamError:
        # Fail-CLOSED, and deliberately NOT a raise: the caller passed nothing wrong, so
        # refusing the CALL would be blaming them for a record they never saw. The
        # budget simply does not apply -> no ceiling keys -> NO_ORACLE downstream.
        return (min_air, min_glass, None, None), None
    return (min_air, min_glass, rec_air, rec_glass), BASIS_DECLARED


# --------------------------------------------------------------------------- #
# Geometry read (guarded per-surface, never raises).
# --------------------------------------------------------------------------- #
def _read_rows(lde, n, system=None):
    """Read every surface's geometry + role (guarded). Returns a list of dicts.

    Each entry is the ``read_geometry_row`` shape (raw float radius/thickness/conic/
    semi_diameter + type_name/material/is_stop + ``aspheric_coefficients``) PLUS the
    shared ``role``. A surface whose read THROWS degrades to a sentinel dict marked
    ``ok:False`` (its gap is skipped + flagged) so one wedged surface never sinks the
    whole audit.

    Asphere S2 SAGMATH: ``system`` is threaded into ``read_geometry_row`` so an
    EvenAspheric row carries its 8 even-asphere coefficients (the edge audit models the
    FULL sag). A non-asphere row carries ``aspheric_coefficients = None``.
    """
    rows = []
    for i in range(n):
        try:
            row = _geom.read_geometry_row(lde, i, system=system)
            row["ok"] = True
            row["role"] = _geom.classify_role(
                i, n, row["type_name"], row["material"], row["is_stop"]
            )
            # S6 Delta-2: the SemiDiameter cell solve-type (Fixed=frozen / Automatic / Variable /
            # None=degraded). A guarded PURE read (fail-OPEN -> None), via the ONE shared
            # primitive both freeze + clearance consume (L30). A frozen (Fixed) SemiDiameter's
            # edge clearance is the FROZEN max-over-configs aperture, not the true per-config
            # auto — flagged below (it ANNOTATES, never changes the violation verdict).
            row["semi_solve"] = _cl.semi_solve_type_name(system, lde, i)
        except Exception:  # noqa: BLE001 — one wedged surface degrades; scan continues
            row = {
                "ok": False, "radius": float("nan"), "thickness": float("nan"),
                "conic": float("nan"), "semi_diameter": float("nan"),
                "type_name": "", "material": "", "is_stop": False, "role": "air",
                "aspheric_coefficients": None, "asphere_norm_radius": None,
                "asphere_power": None, "semi_solve": None,
            }
        rows.append(row)
    return rows


def _gap_kind(row_i):
    """``"glass"`` if surface ``i``'s material is a real glass else ``"air"`` (locked §2).

    A real glass = material NOT in {air "", CB "-", MIRROR}. A mirror/CB/air gap is
    an AIR clearance (it carries no glass thickness floor).

    An AUTHORABLE GRIN primitive (Gradient2/Gradient3) is a SOLID element
    whose FOLLOWING gap IS its gradient-medium body (the sequential representation: a
    surface's Thickness is the medium to the next surface: the GRIN at surf 2's
    body is gap 2->3). Its Material reads air-like "" -> classify glass so its edge/center
    is audited at min_glass. Keyed on the AUTHORABLE resolver (the probe-proven set), NOT
    the family recognizer -> a loaded NON-authorable member (Gradium/GridGradient/
    Gradient6...) is NOT classified here (un-probed representation) -> disclosed not-audited
    at the result level (§2.3.4). No try/except-to-material fall-through: a (near-impossible)
    resolver throw over a stored string must NOT silently classify a GRIN as air.
    ``_gap_kind`` only ever sees OK rows with a real ``type_name`` string (a degraded row
    is ``ok:False`` and is ``continue``'d upstream in ``_audit_gaps``), so this cannot throw.
    """
    from . import _grin_cells as _grin
    if _grin.grin_type_of_name(str(row_i.get("type_name", ""))) is not None:
        return "glass"
    mat = str(row_i.get("material", ""))
    if _geom._is_air_material(mat) or _geom._is_mirror(mat):
        return "air"
    return "glass"


# --------------------------------------------------------------------------- #
# Per-gap audit (UNFOLDED).
# --------------------------------------------------------------------------- #
def _ceiling_lookup(surface, kind, max_air, max_glass, ceiling_table):
    """WHICH ceiling bounds this gap -> ``(state, limit)`` in the frozen 3-state set.

    The one place the two ceiling shapes meet. ``caller`` / ``declared`` are UNIFORM
    scalars, so their answer is a pure function of the kind; ``active_merit`` is
    PER-SURFACE, so its answer comes from the table's own ``limit_for``, which owns
    // and R-TAINT.

    A scalar basis can never answer ``source_unreadable``: the caller handed us the
    number, or the session record did, and neither can be half-read. That asymmetry is
    real and is why the state is returned rather than inferred downstream.
    """
    if ceiling_table is not None:
        return ceiling_table.limit_for(surface, kind)
    limit = max_glass if kind == "glass" else max_air
    if limit is None:
        return (_mceil.CEILING_LOOKUP_ABSENT, None)
    return (_mceil.CEILING_LOOKUP_PRESENT, limit)


def _center_ceiling(center, lookup_state, limit):
    """The per-gap ``{"limit", "state"}`` object, or ``None`` when no ceiling applies.

    ``max_glass`` bounds a glass gap, ``max_air`` an air gap; the OTHER kind gets no
    object at all (never a borrowed limit) -- that rule now lives in ``_ceiling_lookup``,
    which resolves it to ``absent``.

    THE STATE IS COMPUTED FROM THE RAW FLOAT, BEFORE any ``_safe()`` stringification,
    and ``unreadable`` is decided FIRST. This ordering is the whole point: ``nan >
    ceiling`` is ``False`` in Python, so a degraded centre falling through to the
    comparison would be published as ``within`` -- a confident "inside budget" derived
    from a cell nobody could read. Equality is ``within``; the exceedance predicate is
    strictly ``center > limit``.

    **EXHAUSTIVE DISPATCH, and the ``raise`` is the point (A-EXHAUST).** This is the ONE
    consumer of a ``limit_for`` answer, and it branches on all three lookup states
    explicitly. A consumer that handled PRESENT and let everything else fall to a single
    ``else`` would re-create at the call site the very defect the tagged return type was
    introduced to fix -- ``absent`` and ``source_unreadable`` are opposite facts with
    opposite remedies, and a fourth token must not be silently dropped into either.
    """
    if lookup_state == _mceil.CEILING_LOOKUP_ABSENT:
        return None
    if lookup_state == _mceil.CEILING_LOOKUP_SOURCE_UNREADABLE:
        # ``limit`` is None HERE and must not be read: the ceiling itself is unknown,
        # which is the opposite of ``unreadable`` below (where the limit is known and
        # the CENTRE is not). Only ``state`` separates them, so ``state`` is branched
        # on first, always.
        return {"limit": None, "state": CEILING_SOURCE_UNREADABLE}
    if lookup_state != _mceil.CEILING_LOOKUP_PRESENT:
        raise AssertionError(lookup_state)
    if (isinstance(center, bool) or not isinstance(center, (int, float))
            or not math.isfinite(center)):
        state = CEILING_UNREADABLE
    elif float(center) > limit:
        state = CEILING_OVER
    else:
        state = CEILING_WITHIN
    return {"limit": limit, "state": state}


def _audit_gaps(rows, n, min_air, min_glass, max_air=None, max_glass=None,
                ceiling_table=None):
    """Per-gap center + edge audit for an UNFOLDED system (locked §2).

    Walks each gap ``i -> i+1`` for ``i`` in ``1 .. n-2`` (skip the object gap at
    ``i=0``; the last real gap ``n-2 -> n-1`` IS the back-airgap, audited as air).
    Returns ``(gaps, violations, flags, ceiling)``.

    ``ceiling_table`` (V-INT D1-b) is the PER-SURFACE ``active_merit`` basis. When it is
    present the scalar ``max_air``/``max_glass`` are not in play at all -- the per-gap
    limit comes from the table -- and the returned ceiling channel gains
    ``limits_by_surface``: the limits ACTUALLY APPLIED, built from the gaps this walk
    evaluated rather than from the raw table, so it can never advertise a budget for a
    surface no gap started.

    ``ceiling`` is ``None`` when neither maximum is in force -- and then every gap record
    is BYTE-IDENTICAL to what this function has always emitted. Otherwise it is
    ``{"exceedances": [...], "unresolved": [...]}``, the two lists the top-level
    ``center_ceiling_audit`` block is assembled from.

    CEILING EXCEEDANCES NEVER JOIN ``violations`` (AXIS 2, FROZEN). 13 measured
    consumers read that list as "below the manufacturability FLOOR" -- ``promote_best``
    HARD-REFUSES on it -- so folding a "too long" fact into it would make an unedited
    ``promote_best`` refuse a design for being long, a behaviour change nobody asked for
    and reachable without touching ``promote_best`` at all.
    """
    gaps = []
    violations = []
    flags = []
    audit_ceiling = (
        max_air is not None or max_glass is not None or ceiling_table is not None
    )
    exceedances = []
    unresolved = []
    limits_by_surface = {}
    for i in range(1, n - 1):
        ri = rows[i]
        rip1 = rows[i + 1]
        skipped = None
        if not ri.get("ok", True):
            skipped = i
        elif not rip1.get("ok", True):
            skipped = i + 1
        if skipped is not None:
            flags.append(
                f"gap {i}->{i + 1} skipped (surface {skipped} geometry unreadable)"
            )
            if audit_ceiling:
                # audit-1: ``kind`` is ``None``, NOT a guess. The degraded sentinel
                # has already replaced the unknown material with air-like data
                # (``"material": ""``, ``"role": "air"``), so stamping a kind here would
                # mislabel a possibly-GLASS gap as air -- fabricated evidence in the one
                # record whose entire job is to say "this location was not adjudicated".
                # The location is still recorded: an unaudited gap must be VISIBLE, not
                # silently absent from a "complete" audit.
                unresolved.append({
                    "surface": i, "next_surface": i + 1, "kind": None,
                    "reason": CEILING_REASON_ROW,
                })
            continue
        kind = _gap_kind(ri)
        threshold = min_glass if kind == "glass" else min_air
        center = ri["thickness"]

        h = _cl.controlling_height(ri["semi_diameter"], rip1["semi_diameter"])
        edge_approximate = False
        approx_surfaces = []
        if h is None:
            # No finite-positive aperture on either bounding surface -> no edge to
            # measure. Report the center (still auditable) + flag the missing edge.
            edge = None
            flags.append(
                f"gap {i}->{i + 1} edge skipped (no finite-positive semi-diameter on "
                f"surface {i} or {i + 1}); center audited only"
            )
        else:
            # Asphere S2 SAGMATH: feed each bounding surface's even-asphere coefficients
            # into the edge math so the edge is modelled from the FULL sag (sphere +
            # conic base + polynomial), not the base only. A non-asphere row carries
            # ``aspheric_coefficients = None`` -> the conic-only byte-identical edge.
            edge = _cl.edge_thickness(
                center, ri["radius"], ri["conic"], rip1["radius"], rip1["conic"], h,
                coeffs_i=ri.get("aspheric_coefficients"),
                coeffs_ip1=rip1.get("aspheric_coefficients"),
                norm_i=ri.get("asphere_norm_radius"),
                power_i=ri.get("asphere_power"),
                norm_ip1=rip1.get("asphere_norm_radius"),
                power_ip1=rip1.get("asphere_power"),
            )
            # (FIX 2) Conic sphere-fallback honesty: if EITHER bounding surface used
            # a NON-ZERO conic whose sag radical went negative at the controlling
            # aperture h, sag_profile fell back to the paraxial sphere term — finite
            # but an APPROXIMATION that can UNDER-estimate a steep-conic sag (the real
            # edge may be THINNER than computed). Disclose it; the violation logic is
            # UNCHANGED (still flag on the computed edge).
            if _cl.conic_sphere_fallback_fired(ri["radius"], ri["conic"], h):
                approx_surfaces.append(i)
            if _cl.conic_sphere_fallback_fired(rip1["radius"], rip1["conic"], h):
                approx_surfaces.append(i + 1)
            if approx_surfaces:
                edge_approximate = True
                surf_list = ", ".join(f"S{s}" for s in approx_surfaces)
                flags.append(
                    f"edge thickness for gap S{i}->S{i + 1} is APPROXIMATE (conic sag "
                    f"beyond its radical at the controlling aperture on {surf_list} — "
                    "sphere fallback; the true edge may be thinner)"
                )

        # The worst (min) of the finite center/edge values is the offending number.
        candidates = [v for v in (center, edge) if v is not None and math.isfinite(v)]
        worst = min(candidates) if candidates else None
        is_violation = worst is not None and worst < threshold

        # S6 Delta-2: a per-gap FROZEN-SemiDiameter flag (Fixed-solve ONLY — a Variable/
        # Automatic aperture is the REAL clear aperture, not a frozen value). It ANNOTATES
        # (never changes the violation verdict): the frozen aperture is the LARGER max-over-
        # configs one, so a thin edge there is genuinely thin; the flag just discloses the
        # number is the frozen aperture, not the true per-config auto.
        frozen_i = (ri.get("semi_solve") == "Fixed")
        frozen_ip1 = (rip1.get("semi_solve") == "Fixed")
        frozen_semi = bool(frozen_i or frozen_ip1)
        frozen_surfaces = [s for s, f in ((i, frozen_i), (i + 1, frozen_ip1)) if f]

        gap = {
            "surface": i,
            "next_surface": i + 1,
            "kind": kind,
            "center_thickness": _safe(center),
            "edge_thickness": _safe(edge) if edge is not None else None,
            "edge_height": _safe(h) if h is not None else None,
            "edge_approximate": bool(edge_approximate),
            "threshold": threshold,
            "violation": bool(is_violation),
            "is_back_airgap": (i == n - 2),
            "frozen_semi": frozen_semi,
            "frozen_surfaces": frozen_surfaces,
        }
        # Tier-1: the ADDITIVE nested ceiling object. Computed from the RAW ``center``
        # above, never from the ``_safe()``-stringified value in the record. Absent
        # entirely when this gap's kind carries no stated limit, so a no-budget
        # envelope is byte-identical.
        lookup_state, lookup_limit = _ceiling_lookup(
            i, kind, max_air, max_glass, ceiling_table
        )
        cc = _center_ceiling(center, lookup_state, lookup_limit)
        if cc is not None:
            gap["center_ceiling"] = cc
            if cc["state"] == CEILING_OVER:
                exceedances.append({
                    "surface": i, "next_surface": i + 1, "kind": kind,
                    "center_thickness": _safe(center), "limit": cc["limit"],
                })
            elif cc["state"] == CEILING_UNREADABLE:
                unresolved.append({
                    "surface": i, "next_surface": i + 1, "kind": kind,
                    "reason": CEILING_REASON_CENTER,
                })
            elif cc["state"] == CEILING_SOURCE_UNREADABLE:
                # A DIFFERENT provenance from the line above, and the whole reason the
                # two reasons are separate tokens: there the centre did not read, here
                # the BUDGET did not. Same location, opposite remedy.
                unresolved.append({
                    "surface": i, "next_surface": i + 1, "kind": kind,
                    "reason": CEILING_REASON_SOURCE,
                })
            if lookup_state == _mceil.CEILING_LOOKUP_PRESENT and ceiling_table is not None:
                # STRING keys, and the reason is measured rather than stylistic. This
                # block crosses the MCP boundary as JSON, where object keys are strings
                # BY THE FORMAT -- so an int key silently becomes ``"1"`` in transit and
                # a consumer doing ``limits_by_surface.get(surface)`` with an int gets
                # ``None`` over the wire while getting the record in-process. ``None``
                # there reads as "no ceiling for this surface", which is ABSENT
                # manufactured out of a key-type mismatch -- this cycle's own defect
                # class, arriving through the serializer instead of through a cell.
                # One representation on both sides; no shipped envelope keys a dict by
                # int (``per_config`` is a LIST of records for the same reason).
                limits_by_surface[_mceil.surface_key(i)] = {
                    "kind": kind, "limit": lookup_limit}
        gaps.append(gap)
        if frozen_semi:
            flags.append(
                f"gap S{i}->S{i + 1} edge is at a FROZEN (Fixed-solve) SemiDiameter "
                f"(surface(s) {frozen_surfaces}); the edge clearance is the FROZEN "
                "max-over-configs aperture, NOT the true per-config auto aperture — re-float "
                "with freeze_semidiameters(mode='auto') for the true clearance"
            )
        if is_violation:
            violations.append({
                "surface": i,
                "next_surface": i + 1,
                "kind": kind,
                "center_thickness": _safe(center),
                "edge_thickness": _safe(edge) if edge is not None else None,
                "worst": _safe(worst),
                "threshold": threshold,
                "is_back_airgap": (i == n - 2),
            })
    ceiling = (
        {"exceedances": exceedances, "unresolved": unresolved}
        if audit_ceiling else None
    )
    if ceiling is not None and ceiling_table is not None:
        # Only on the per-surface basis. A scalar basis has ONE limit per kind and
        # already publishes it as ``limits``; emitting this there would be a second
        # encoding of the same fact.
        ceiling["limits_by_surface"] = limits_by_surface
    return gaps, violations, flags, ceiling


def _folded_gaps(rows, n):
    """INFORMATIONAL per-gap center thickness for a FOLDED system (locked §3).

    NO violation flag (the raw LDE thickness is the fold direction, not a clearance
    — the -525.7 trap). Returns the ``folded_gaps`` list.
    """
    out = []
    for i in range(1, n - 1):
        ri = rows[i]
        if not ri.get("ok", True):
            continue
        out.append({
            "surface": i,
            "next_surface": i + 1,
            "kind": _gap_kind(ri),
            "center_thickness": _safe(ri["thickness"]),
            "role": ri.get("role"),
        })
    return out


# --------------------------------------------------------------------------- #
# Global BFD (BOTH folded + unfolded).
# --------------------------------------------------------------------------- #
def _optical_surfaces(rows, n):
    """The first/last OPTICAL (powered) surface numbers (locked §4).

    Optical = role in {glass, mirror} (NOT object/image/CB/flat-air-dummy/stop). A
    grating reads "glass" (catalog material) or "mirror" (reflective). Returns
    ``(first, last)`` surface numbers, or ``(None, None)`` if none exist (a degenerate
    all-flat-air / all-CB system).
    """
    optical = [
        i for i in range(n)
        if rows[i].get("ok", True) and _cl.is_optical_surface(rows[i])
    ]
    if not optical:
        return None, None
    return optical[0], optical[-1]


def _global_z(frames, i):
    """The global vertex Z (slot [12]) of surface ``i`` from a read frames list.

    A degraded / out-of-range frame -> ``None`` (the caller flags it, never a bogus
    number). ``read_global_frames`` already returns ``vertex=(x,y,z)`` per surface.
    """
    if i is None or not (0 <= i < len(frames)):
        return None
    fr = frames[i]
    if not fr.get("ok"):
        return None
    vertex = fr.get("vertex")
    if not vertex or len(vertex) != 3:
        return None
    z = vertex[2]
    if not (isinstance(z, (int, float)) and math.isfinite(z)):
        return None
    return float(z)


def _global_bfd(lde, rows, n):
    """The ``global_bfd`` block from ``GetGlobalMatrix`` (locked §4). Never raises.

    ``image_global_z`` (slot [12] of the image surface); ``behind_first_optic`` /
    ``behind_last_optic`` = image_global_z - first/last optical-surface global z. A
    degraded/unreadable frame -> that field ``None`` + a flag (never a bogus number).
    """
    flags = []
    frames = _geom.read_global_frames(lde, n)
    image_idx = n - 1
    image_z = _global_z(frames, image_idx)
    if image_z is None:
        flags.append(
            "global_bfd: the image surface global frame is degraded/unreadable; "
            "BFD distances unavailable"
        )

    first_opt, last_opt = _optical_surfaces(rows, n)
    first_z = _global_z(frames, first_opt)
    last_z = _global_z(frames, last_opt)

    if first_opt is None:
        flags.append(
            "global_bfd: no optical (powered) surface found; behind_first_optic/"
            "behind_last_optic unavailable"
        )
    else:
        if first_z is None:
            flags.append(
                f"global_bfd: the first optical surface ({first_opt}) global frame is "
                "degraded; behind_first_optic unavailable"
            )
        if last_z is None:
            flags.append(
                f"global_bfd: the last optical surface ({last_opt}) global frame is "
                "degraded; behind_last_optic unavailable"
            )

    behind_first = (
        image_z - first_z if (image_z is not None and first_z is not None) else None
    )
    behind_last = (
        image_z - last_z if (image_z is not None and last_z is not None) else None
    )

    return {
        "image_global_z": _safe(image_z) if image_z is not None else None,
        "behind_first_optic": _safe(behind_first) if behind_first is not None else None,
        "behind_last_optic": _safe(behind_last) if behind_last is not None else None,
        "first_optic_surface": first_opt,
        "last_optic_surface": last_opt,
        "image_surface": image_idx,
        "units": "mm",
    }, flags


def _min_gap_clearance(gaps):
    """The smallest finite clearance over a gap list (the per-config headline, D3).

    Reads each gap's ``center_thickness`` + (when present) ``edge_thickness``; returns
    the min over the FINITE numeric values (a ``safe_float`` string sentinel is skipped),
    or ``None`` when there is nothing finite to compare. The per-config headline that
    makes the ``"all"`` sweep's ``config_differs`` exact for a zoom whose gaps move.
    """
    candidates = []
    for gap in gaps or []:
        if not isinstance(gap, dict):
            continue
        for key in ("center_thickness", "edge_thickness"):
            v = gap.get(key)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and math.isfinite(v):
                candidates.append(float(v))
    return min(candidates) if candidates else None


def _safe(value):
    """JSON-safe float (NaN/inf -> string sentinel via the tier-wide safe_float)."""
    from .._io import safe_float

    try:
        return safe_float(value)
    except Exception:  # noqa: BLE001 — a non-float passes through verbatim
        return value


# =========================================================================== #
# check_clearance
# =========================================================================== #

# --------------------------------------------------------------------------- #
# The session-declared budget: its IDENTITY BINDING (spec 3.8b/c).
#
# ``check_clearance`` was a pure function of the loaded design plus its explicit
# arguments. Under owner ruling 1 it also reads session history, which is the
# second-resolution-path shape -- a value reachable two ways, where the defect lives in
# the DISAGREEMENT between them. Everything below exists to make one of those two ways
# REFUSE TO ANSWER whenever it cannot prove it still speaks for the loaded design.
#
# Two layers, and the failure direction of each is deliberate:
#   layer 1  the dispatch epoch (server.IDENTITY_PRESERVING_TOOLS) -- an ALLOW-LIST, so a
#            tool nobody classified bumps the epoch and the budget honestly evaporates.
#   layer 2  the shape stamp -- catches a swap that defeated layer 1 whenever the swap
#            changed shape.
# Neither can be satisfied by silence: a mismatch DELETES the record, an unreadable
# stamp APPLIES NOTHING. There is no path here that produces a verdict on a doubt.
# --------------------------------------------------------------------------- #
def _forget_declared_budget(session):
    """Positively delete the session record. Guarded -- never breaks a read-only audit."""
    try:
        session.declared_budget = None
    except Exception:  # noqa: BLE001 - a session that refuses the write keeps its record,
        pass           # and every applicability check below still refuses to apply it


def _live_shape(system, n_surfaces):
    """``{n_surfaces, n_configs}`` from two cheap reads, or ``None``.

    ``None`` means a component could not be READ, which is NOT a mismatch: an
    unreadable stamp applies nothing and deletes nothing, because deleting on a
    transient read fault would retire a budget that is still perfectly valid.

    **BOTH components are read STRICTLY, and that is what makes the sentence above true.**
    It was false until: ``n_configs`` came through
    ``_config_common.safe_number_of_configurations``, which absorbs a read fault as
    ``1``, so an unreadable count on a multi-config design was reported as a legitimate
    single-config stamp and DELETED the record. Identical root cause to the CRIT fixed
    one field over in ``resolve_evaluated_config`` -- a disclosure-only ``safe_*`` reader
    used as identity evidence.

    A full prescription digest would be the WRONG stamp here: the loop's whole purpose
    is to move thicknesses, so any value-derived digest breaks on the first optimization
    step and the mechanism goes permanently dark. Identity here means LINEAGE.

    **``system_file`` IS DELIBERATELY NOT IN THE STAMP, and its absence is the ruling.**
    The Tier-1 spec named it as the third component. It cannot be identity material, and
    this repo had already paid for that lesson in ``aperture_ramp.py``:

      ``:137-140`` -- *"A ``_snapshot_last_good`` ``SaveAs`` REPOINTS
      ``system.SystemFile`` to the checkpoint path, so ``SystemFile ==
      checkpoint["path"]`` is VACUOUSLY true BEFORE the restore ``LoadFile`` runs ... So
      NEITHER a SystemFile-match NOR a count-match can discriminate a REAL restore from
      a gotcha- SILENT NO-OP ``LoadFile``"*

      ``:419-420`` -- *"NOTE (accepted): SaveAs REPOINTS system.SystemFile at this temp
      checkpoint, which the finally then reaps -> SystemFile is left dangling after a
      clean ramp."*

    So the field is repointed by ANY save and can DANGLE at a reaped temp path. Concretely
    it moves under ``save_candidate`` / ``promote_best`` (``workspace._save_as_seam``) and
    under ``optimize``'s Hammer fork (``optimize_run.py:1533``) -- all three of which are
    correctly IDENTITY-PRESERVING, so including it would have DELETED the budget on an
    ordinary save of an unchanged design.

    Two reasons dropping it is safe, and both are load-bearing:

    1. **Layer 2 is a BACKSTOP, not the primary.** The sound mechanism is layer 1, the
       dispatch epoch (``server.IDENTITY_PRESERVING_TOOLS``), and it is untouched.
    2. **A stamp that FALSE-INVALIDATES on every ``save_candidate`` is strictly worse
       than a weaker one.** It makes the oracle dark in ordinary operation -- which is
       this ticket's own defect ("a rule that reads decisive and cannot fire") re-created
       one layer out. A weaker backstop that fires correctly beats a stronger one that
       fires constantly and wrongly.

    The residual is named rather than hidden: a same-``n_surfaces``, same-``n_configs``
    design swap now defeats layer 2, so it is caught by layer 1 alone -- i.e. it requires
    the affirmative MIS-LISTING that ``test_cc20`` / ``V-43`` exist to catch.
    """
    try:
        # STRICT, for the same reason ``resolve_evaluated_config`` is strict, and this is
        # the OTHER HALF of that fix rather than a new idea. ``safe_number_of_configurations``
        # is throw-GUARDED and degrades an unreadable count to ``1``, so it never raises:
        # the ``return None`` below was UNREACHABLE through the only component that can
        # fault, and a transient MCE wedge on a 3-config design produced the stamp
        # ``{n_configs: 1}`` -- a MISMATCH against the recorded ``3``, which POSITIVELY
        # DELETED a valid record, permanently, after the fault had healed. The docstring
        # above promised the opposite. Measured by the agent (at2/at2b).
        n_configs = _mc.number_of_configurations(system)
    except Exception:  # noqa: BLE001 — an unreadable count is not a mismatch
        return None
    if not isinstance(n_configs, int) or isinstance(n_configs, bool):
        return None
    if not isinstance(n_surfaces, int) or isinstance(n_surfaces, bool):
        return None
    return {"n_surfaces": n_surfaces, "n_configs": n_configs}


def resolve_evaluated_config(system):
    """The ACTIVE configuration for the applicability PROOF -- or ``None``: UNREADABLE.

    **THE ONE READER FOR PRECONDITION (4), AT BOTH DOORS**, exported so
    ``optimize_merit`` records ``config_scope`` from the same read this module compares
    it against. Two derivations of one identity is the class this binding exists to
    prevent, not to add.

    **WHY IT IS NOT ``_config_common.safe_current_configuration``, which is what this
    used to call.** That function's own docstring calls it *"the disclosure-only
    active-config read"* and says *"A read fault degrades to ``1``"*. Using it as an
    applicability proof made a WEDGED config read silently satisfy "config 1 is
    covered", so a budget declared for config 1 would adjudicate a design whose active
    configuration nobody could read -- a value that ABSORBS degradation used as evidence
    that nothing was degraded. That is the manufactured-verdict shape this cycle has
    already killed three times, found by a review as a CRIT.

    So this consumes the STRICT primitive ``_mce_cells.current_configuration`` -- the one
    ``safe_current_configuration`` itself wraps -- and keeps the fault instead of
    swallowing it. ``None`` means "I could not read this", which is DISTINCT from "it is
    1" and is never treated as a covered configuration.

    The cost is named rather than hidden: on a system whose ``MCE`` cannot be read at all
    the session budget goes dark and every bare call books the no-oracle path. That is
    the correct direction -- a spurious ``NO_ORACLE`` costs an unadjudicated finding, a
    stale verdict costs a wrong one.
    """
    try:
        config = _mc.current_configuration(system)
    except Exception:  # noqa: BLE001 - UNREADABLE, and it stays unreadable
        return None
    if isinstance(config, bool) or not isinstance(config, int):
        return None
    if config < 1:
        return None
    return config


def active_merit_offered(system, params):
    """Is the ``active_merit`` basis offered for THIS call? -> ``(bool, reason|None)``.

    TWO INDEPENDENT MULTI-CONFIG GATES EXIST AND NEITHER SUBSUMES THE OTHER (spec 3.7).
    ``_merit_ceiling``'s is about which configuration a ROW belongs to; this is about
    which configuration the CALLER asked about. A merit with no ``CONF`` row passes
    and can still be the wrong question to ask under a sweep.

    - ``config=None`` -> OFFERED. ``None`` means "current", which IS the active
      configuration, so nothing needs comparing -- and deliberately so: on a system whose
      MCE cannot be read at all a bare call still gets its merit basis, because no read
      was required to answer the question.
    - ``config=<int>`` -> offered IFF it names the configuration that is active NOW.
      ``evaluate_over_configs`` runs a ``single`` grade INSIDE ``with_configuration``, so
      on the healthy path the requested config IS the active one and this passes. Where
      it does NOT pass, the switch did not take -- and that is precisely a call whose
      merit rows would be attributed to the wrong configuration.
    - ``config="all"`` -> REFUSED. Stated honestly: on a multi-config design with no
      ``CONF`` rows this is conservative beyond strict necessity, since unbracketed rows
      do apply to every configuration. It is kept because the reader produces ONE table
      while a sweep needs a verdict per configuration, and because under-delivering
      costs an unadjudicated finding while over-delivering costs a wrong verdict. A
      deliberate under-delivery, not an oversight.
    - the active configuration cannot be read -> REFUSED.
    """
    config = params.get("config")
    if config is None:
        return (True, None)
    if isinstance(config, str):
        # Only the exact spelling ``"all"`` survives ``resolve_config_selector``; any
        # other string already raised as a param error before this call site.
        return (False, NOT_OFFERED_SWEEP)
    active = resolve_evaluated_config(system)
    if active is None:
        return (False, NOT_OFFERED_CONFIG_UNREADABLE)
    try:
        requested = _cfg._coerce_config_int(config)
    except Exception:  # noqa: BLE001 - already validated upstream; fail CLOSED anyway
        return (False, NOT_OFFERED_NON_ACTIVE)
    if requested != active:
        return (False, NOT_OFFERED_NON_ACTIVE)
    return (True, None)


def build_shape_stamp(system, n_surfaces):
    """The shape stamp as recorded at ``build_merit`` -- the SAME builder both doors use.

    Exported so ``optimize_merit`` cannot record a stamp this module would then compare
    against a differently-derived one; two derivations of one identity is the class this
    binding exists to prevent, not to add.
    """
    return _live_shape(system, n_surfaces)


def _applicable_record(session, system, n_surfaces, evaluated_config):
    """The session budget record, ONLY if it still speaks for this design and config.

    Returns the record dict or ``None``. The three checks it owns (the fourth, "no
    explicit params", belongs to ``resolve_ceilings``):

    2. **epoch** -- ``Dispatcher.dispatch`` bumps ``session.design_epoch`` at call ENTRY
       for every tool NOT in ``IDENTITY_PRESERVING_TOOLS``. Unequal => DELETE.
    3. **shape** -- ``{n_surfaces, n_configs}``. Unequal => DELETE.
       Unreadable => apply nothing, keep the record.
    4. **config scope** -- a non-spanning build covers ONE configuration. An uncovered
       config => apply nothing but KEEP the record, because switching back re-enables a
       budget that never stopped being true; only an IDENTITY mismatch deletes. An
       UNREADABLE config (``evaluated_config is None``) is not a covered one and never
       becomes one -- see ``resolve_evaluated_config``.

    Layer 1 also has a POISON LATCH. ``Dispatcher._bump_design_epoch`` sets
    ``session.design_epoch_unusable`` when it could not increment the epoch: from that
    moment the epoch proves nothing, so no record may be applied through it again. The
    latch is checked FIRST, before any read, because the whole point of layer 1 is that
    its failure direction is a spurious NO_ORACLE and never a stale verdict.
    """
    record = getattr(session, "declared_budget", None)
    if not isinstance(record, dict):
        return None

    if getattr(session, "design_epoch_unusable", False):
        _forget_declared_budget(session)
        return None

    live_epoch = getattr(session, "design_epoch", 0)
    rec_epoch = record.get("epoch")
    if (isinstance(rec_epoch, bool) or not isinstance(rec_epoch, int)
            or isinstance(live_epoch, bool) or not isinstance(live_epoch, int)
            or rec_epoch != live_epoch):
        _forget_declared_budget(session)
        return None

    shape = _live_shape(system, n_surfaces)
    if shape is None:
        return None                       # unreadable != mismatch (see _live_shape)
    if record.get("shape") != shape:
        _forget_declared_budget(session)
        return None

    scope = record.get("config_scope")
    # ``evaluated_config`` is ``None`` when the active configuration could not be READ.
    # ``None`` is never equal to a recorded ``int`` and never equal to ``"all"``, so an
    # unreadable config falls through to the no-oracle return by CONSTRUCTION -- and the
    # ``"all"`` arm is guarded explicitly below so a spanning budget cannot adjudicate a
    # design whose active configuration is unknown either.
    if evaluated_config is None:
        return None                       # UNREADABLE != covered. Never a verdict.
    if scope != "all" and scope != evaluated_config:
        return None                       # scope miss: NOT a verdict, and NOT a deletion
    return record


def _ceiling_status(folded, gaps, ceiling):
    """The ``center_ceiling_audit.status`` token."""
    if folded:
        return CEILING_STATUS_FOLDED
    if ceiling is not None and ceiling["unresolved"]:
        return CEILING_STATUS_PARTIAL
    if not gaps:
        return CEILING_STATUS_NO_GAPS
    return CEILING_STATUS_COMPLETE


def check_clearance(session, params):
    """Audit per-gap center/edge clearance + the global back-focal distance.

    Params: ``min_air`` (default 0.5) / ``min_glass`` (default 1.0) — the clearance
    floors (match build_merit). READ-ONLY (no LDE mutation). For an UNFOLDED system,
    per-gap center + edge thickness with violations (the back-airgap audited as air);
    for a FOLDED system, informational ``folded_gaps`` + a note (the raw LDE thickness
    is the fold, not a clearance). ALWAYS a ``global_bfd`` block (behind the first /
    last optical surface). NEVER raises past the boundary.

    ``config`` (None|int|"all") selects the configuration: None=current,
    int=that config, ``"all"`` sweeps every config (cheap geometry) into a ``per_config``
    vector + coverage reconcile + ``config_differs`` (the min edge clearance headline). A
    bad ``config`` -> ``clearance_param``.

    **Tier-1 CENTRE-THICKNESS CEILINGS (additive).** ``max_air`` / ``max_glass`` declare
    a maximum CENTRE thickness (finite ``> 0``, and never below the matching floor). When
    a ceiling is in force each gap of that kind gains
    ``center_ceiling: {"limit", "state"}`` with ``state`` in ``within|over|unreadable``,
    and the envelope gains a ``center_ceiling_audit`` block. Exceedances live THERE and
    NEVER in ``violations`` (which stays a FLOOR list for its 13 consumers). With no
    ceiling in force the envelope is byte-identical to before.

    **THIS HANDLER IS STATEFUL (owner ruling 1).** A call with NO explicit floor or
    ceiling param applies the budget the session DECLARED at ``build_merit`` (basis
    ``"declared"``) -- but only while a dispatch epoch, a shape stamp and the config
    scope all still prove it speaks for the loaded design. Any doubt applies nothing.
    Naming ANY floor or ceiling param is hand-driving and bypasses the record entirely.
    ``check_clearance_floor_only`` is the entry for callers that must never inherit it.
    """
    return _check_clearance(session, params, consult_session_budget=True)


def check_clearance_floor_only(session, params):
    """The keeper gate's entry: IDENTICAL floor audit, session budget NEVER consulted.

    Exists so the suppression is EXPLICIT at the seam whose own docstring warns about
    two independent resolutions -- the gate opts out by NAMING it, not by hoping.

    ``save_candidate`` / ``promote_best`` call the shared handler BARE, so under session
    carriage they would INHERIT a budget declared mid-run: keeper records, the identity
    ladder and all 13 ``violations`` consumers would start moving under a declaration
    that has nothing to do with the manufacturability floor they gate on. This entry
    passes ``session_record=None`` by name, runs no identity reads, and deletes nothing
    -- so the gate is byte-identical to a session that never declared a budget.
    """
    return _check_clearance(session, params, consult_session_budget=False)


def _check_clearance(session, params, consult_session_budget):
    params = _require_dict(params)
    try:
        # ONE resolver, shared with workspace._effective_floors.
        # Do NOT re-inline the _finite_nonneg calls here — two copies is two acceptance
        # sets, and the guard would then be able to disagree with the producer.
        floors = resolve_floors(params)
        # Tier-1: run THE ceiling resolver once, here, purely to REFUSE a bad explicit
        # box before any config sweep starts. Its answer is discarded; the binding
        # resolution happens once per evaluated config inside ``_impl`` (which is the
        # only place that knows the live epoch/shape/config). Doing it here is what
        # makes ``max_air=-1`` a ``clearance_param`` refusal instead of a degraded
        # ``per_config`` entry under ``config="all"``, where ``_grade_safe`` swallows a
        # grader throw into ``ok:false``.
        resolve_ceilings(params, floors, session_record=None)
    except ToolParamError as exc:
        return error_envelope("check_clearance", _CL_PARAM, str(exc))

    config = params.get("config")

    def _grade(sess):
        return _impl(sess, floors, params, consult_session_budget)

    try:
        return _cfg.evaluate_over_configs(session, config, _grade)
    except ToolParamError as exc:  # a bad config / param raised deeper -> clearance_param
        return error_envelope("check_clearance", _CL_PARAM, str(exc))
    except Exception as exc:  # noqa: BLE001 — a total geometry-read failure -> envelope
        return error_envelope(
            "check_clearance", _CL_FAMILY,
            f"could not read the system geometry for a clearance audit ({exc!r})",
        )


def _impl(session, floors, params, consult_session_budget=False):
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    # Tier-1: resolve the box for THIS evaluation. Precondition (1) of 3.8c -- the
    # all-or-nothing rule -- is checked HERE so a hand-driven call costs no identity
    # reads at all; ``resolve_ceilings`` enforces it again over the record it is handed,
    # so the rule holds even if this guard were deleted.
    record = None
    if consult_session_budget and not (
        _has_explicit(params, _FLOOR_KEYS) or _has_explicit(params, _CEILING_KEYS)
    ):
        record = _applicable_record(
            session, system, n, resolve_evaluated_config(system)
        )
    (min_air, min_glass, max_air, max_glass), ceiling_basis = resolve_ceilings(
        params, floors, session_record=record
    )

    # V-INT D1-b: the THIRD basis, and THE ONLY CALL SITE. At most ONE reader invocation
    # per ``check_clearance`` call, reached only when ``caller`` and ``declared`` both
    # yielded nothing -- so a hand-driven or session-declared call costs zero merit reads.
    #
    # THE KEEPER GATE OPTS OUT THROUGH THE SAME NAMED SWITCH, and that is a BUILD-TIME
    # decision the does not cover (recorded in AUDIT-RESPONSE.md, not
    # silently taken). ``check_clearance_floor_only`` exists so ``save_candidate`` /
    # ``promote_best`` are byte-identical to a session that never declared a budget; the
    # ACTIVE MERIT is a design-derived budget, so letting it in through the back door
    # would defeat an opt-out whose whole point is that it is EXPLICIT. Measured, the
    # keeper verdict itself cannot move (``workspace.py`` reads no ceiling key at all and
    # exceedances never join ``violations``) -- the change would be to the envelope and
    # to a 227 ms read on every save. Both are refused here for nothing gained.
    ceiling_table = None
    active_merit_not_offered = None
    if ceiling_basis is None and consult_session_budget:
        offered, not_offered_reason = active_merit_offered(system, params)
        if offered:
            table = _mceil.read_ceiling_table(system, n)
            # A merit that declares NOTHING is not a basis -- see
            # ``CeilingTable.declares_anything``. Without this the block would appear on
            # every bare call in the repo, asserting a basis that bounds no gap.
            if table.declares_anything():
                ceiling_table = table
                ceiling_basis = BASIS_ACTIVE_MERIT
        else:
            active_merit_not_offered = not_offered_reason

    rows = _read_rows(lde, n, system=system)
    # ONE shared fold predicate (geometry-readouts §1): scan ALL surfaces (incl. 0
    # and n-1) against the raw Type/Material. The role classifier stamps object/image
    # at the ends BEFORE the CB/mirror checks, so a CB at surface 0 / a MIRROR at the
    # image surface would read UNFOLDED — use the shared predicate so this can never
    # drift from get_first_order's fold decision.
    folded = _geom.lde_is_folded(lde, n)

    flags = []
    # The invalidation-failure DISCLOSURE (a review, HIGH). A dispatch that could
    # not increment the design epoch has latched this session: layer 1 can no longer
    # prove anything, so any declared budget was retired and none will be applied again.
    # It is emitted in the ARTIFACT rather than only logged, because a silent
    # invalidation failure is indistinguishable from a session that never declared a
    # budget -- and those two have opposite remedies.
    if getattr(session, "design_epoch_unusable", False):
        _faults = getattr(session, "design_epoch_faults", None)
        flags.append(
            "the design-identity epoch could not be maintained for this session"
            + (f" (failed on: {_faults})" if isinstance(_faults, list) and _faults else "")
            + "; any session-declared centre-thickness budget has been RETIRED and no "
            "further one will be applied — pass max_air/max_glass explicitly to audit "
            "against a ceiling"
        )
    gaps = []
    violations = []
    gap_ceiling = None
    folded_gaps = None
    note = None

    # A system needs OBJECT + >=1 optical + IMAGE (>=3 surfaces) for any gap to audit.
    if n < 3:
        flags.append(
            f"system has {n} surface(s); no optical gap to audit (need OBJECT + an "
            "optic + IMAGE)"
        )

    if folded:
        folded_gaps = _folded_gaps(rows, n)
        note = (
            "system is folded (a coordinate-break or mirror is present); the per-gap "
            "edge/clearance VIOLATION audit is unfolded-only (the raw LDE thickness is "
            "the fold direction, not a clearance) — use global_bfd.behind_first_optic "
            "for the detector clearance. Global-frame per-gap clearance for a fold is "
            "future work."
        )
    elif n >= 3:
        gaps, violations, gap_flags, gap_ceiling = _audit_gaps(
            rows, n, min_air, min_glass, max_air, max_glass,
            ceiling_table=ceiling_table,
        )
        flags.extend(gap_flags)

    bfd, bfd_flags = _global_bfd(lde, rows, n)
    flags.extend(bfd_flags)

    # SAGMATH sag disclosure (REAL geometry — replaces the S1 flag-only
    # ``asphere_sag_ignored``): the per-gap edge audit above now models the FULL sag
    # (sphere + conic base + the polynomial Σα₂ₙr²ⁿ term) for an EvenAspheric surface whose
    # coefficients were read (``aspheric_coefficients`` populated). So a steep asphere that
    # thins the edge below ``min_glass`` is a REAL ``violation:true`` (the detect-side
    # payoff), NOT just a warning.
    #
    # The disclosure splits the EvenAspheric surfaces by whether their sag was MODELLED:
    #   - MODELLED (coefficients read OK) -> ``asphere_sag_modelled`` (informational; the
    #     edge IS the full asphere sag, no caveat needed).
    #   - UNREADABLE (a degraded/throwing coefficient cell, or a wedged row that could be an
    #     asphere) -> FAIL-CLOSED: flag it approximate. The edge was computed from the
    #     sphere+conic base only because the polynomial term could not be read — a thin-edge
    #     violation may be MISSED. NEVER silently treated as a faithful asphere edge.
    asphere_sag_modelled = []
    asphere_sag_approximate = []
    for i in range(n):
        if not rows[i].get("ok", True):
            # The geometry read threw — we could not characterize this surface's Type. It
            # MIGHT be an asphere whose polynomial sag the audit could not model; disclose
            # conservatively (fail-closed).
            asphere_sag_approximate.append(i)
            continue
        if _asph.asphere_type_of_name(str(rows[i].get("type_name", ""))) is None:
            continue  # not a Tier-1 asphere
        # A Tier-1 asphere: MODELLED iff its coefficients read back (a real list); else
        # fail-closed approximate (a degraded/unreadable coefficient cell).
        coeffs = rows[i].get("aspheric_coefficients")
        if isinstance(coeffs, (list, tuple)) and not rows[i].get(
            "coefficients_unreadable", False
        ):
            asphere_sag_modelled.append(i)
        else:
            asphere_sag_approximate.append(i)

    for s in asphere_sag_modelled:
        flags.append(
            f"surface {s} is an even asphere; its edge is modelled from the FULL sag "
            "(sphere + conic + polynomial term)"
        )
    for s in asphere_sag_approximate:
        if not rows[s].get("ok", True):
            flags.append(
                f"surface {s} could not be characterized (geometry read failed); it MAY "
                "be an even asphere whose polynomial sag the edge audit could not model — "
                "a thin-edge violation may be MISSED; verify in OpticStudio"
            )
        else:
            flags.append(
                f"surface {s} is an even asphere but its coefficients could not be read; "
                "the edge was computed from the sphere+conic base ONLY (the polynomial "
                "term is unavailable) — a thin-edge violation may be MISSED; verify in "
                "OpticStudio"
            )

    # (clearance-provenance, DQ-6) SAG-MODEL APPLICABILITY disclosure. The per-gap
    # edge audit computes every sag from the radius/conic/polynomial model; for a surface
    # whose TYPE that model was never written for, the bounding gaps were computed from
    # the base model instead of the real geometry. the probe measured this failing in
    # BOTH directions on a Tilted surface (a real 0.926 mm violation erased to 2.600 AND a
    # false 0.300 mm one invented), so the disclosure is CONSUMED by the save/promote
    # classifier, not merely emitted. A degraded row is SKIPPED here — it is already in
    # ``asphere_sag_approximate`` (the shipped channel); never double-reported.
    sag_model_unfaithful = []
    for i in range(n):
        if not rows[i].get("ok", True):
            continue        # already in asphere_sag_approximate — the shipped channel
        if _geom.sag_model_is_faithful(rows[i].get("type_name")) is not True:
            sag_model_unfaithful.append(i)
    # The claim is CONDITIONAL ("where it bounds"). This scan runs
    # over ALL surfaces (``range(n)``) while ``_audit_gaps`` walks only ``1 .. n-2``, so a
    # named surface may bound NO audited gap — reproduced on surface 0 ``Tilted`` on a
    # design with gaps 1->2 and 2->3 only, and the old unconditional "the gaps bounding it
    # were computed" asserted a computation that never occurred. Scoping the EMISSION
    # would empty this list on a fold (a fold never calls ``_audit_gaps``, so ``gaps ==
    # []``) and destroy the paired assertion, so scoping is deferred with its
    # measurement attached.
    # This string and ``workspace._COVERAGE_TYPE`` are TWO COPIES OF ONE CLAIM — a
    # hazard — and both were edited together; a test parametrised over both sites pins it.
    for s in sag_model_unfaithful:
        flags.append(
            f"surface {s} is a {rows[s].get('type_name')}; its geometry is NOT one the "
            "radius/conic/polynomial sag model this audit uses was written for, so where "
            "it bounds an audited gap that gap's edge was computed from that base model "
            "instead of its real geometry — a violation may be MISSED and a false one may "
            "be REPORTED (measured for a Tilted surface: a real 0.926 mm violation erased, "
            "a false 0.300 mm one invented). Verify in OpticStudio."
        )

    # GRIN disclosure: a GRIN surface's INTERNAL index profile is not
    # drawn/audited — the audited geometry here is the Standard sphere/conic base (live-
    # probed: the index profile does NOT perturb the sag, residual 0.0, so the clearance edge audit
    # is CORRECT on the base geometry; the flag is informational). NO ``_modelled``/
    # ``_approximate`` split (index profile ⊥ sag). A degraded/unreadable row routes to the
    # EXISTING degraded channel above — never claimed GRIN. Additive key + one flag, emitted
    # ONLY when non-empty (a non-GRIN system is byte-for-byte unchanged). Fail-safe: a
    # resolver throw -> no GRIN disclosure (never crashes the read-only audit).
    grin_index_profile_not_drawn = []
    try:
        from . import _grin_cells as _grin
        for i in range(n):
            if not rows[i].get("ok", True):
                continue  # degraded -> the existing degraded channel, never claimed GRIN
            if _grin.grin_type_of_name(str(rows[i].get("type_name", ""))) is not None:
                grin_index_profile_not_drawn.append(i)
    except Exception:  # noqa: BLE001 — a GRIN resolver hiccup -> no GRIN disclosure, never raise
        grin_index_profile_not_drawn = []
    if grin_index_profile_not_drawn:
        # This flag makes NO edge/center-audited claim — that claim is
        # keyed on the FAMILY/type surface list, which would falsely assert "audited" for a
        # primitive whose gap was SKIPPED (it lands in grin_not_audited, reason gap_unaudited).
        # The per-surface audited/not-audited truth is the GAP-DERIVED structured evidence
        # (grin_geometric_audit.audited vs grin_not_audited); this flag only discloses the
        # index profile (true for every recognized primitive regardless of gap coverage).
        flags.append(
            "GRIN: internal index profile not drawn (surfaces "
            f"{grin_index_profile_not_drawn}); bulk-index manufacturability is not audited. "
            "See grin_geometric_audit for the surfaces whose edge/center clearance WAS audited "
            "at min_glass (solid-medium policy) and grin_not_audited for any that were not"
        )

    # Positive evidence + unconditional not-audited disclosure, derived from the ACTUAL
    # evaluated gap records, NOT the
    # family-recognized surface list: a classified primitive whose gap was SKIPPED (its next
    # surface unreadable -> not in ``gaps``) is placed in ``grin_not_audited`` (never counted
    # as "audited as solid"). A recognized FAMILY member that is NOT an authorable primitive
    # (a loaded Gradium/GridGradient/…) is UNCONDITIONALLY disclosed not-audited (its
    # representation is un-probed — never classified glass, §2.3.4). Both keys emitted ONLY
    # when non-empty (a non-GRIN system is byte-for-byte unchanged). Fail-safe: the assembly
    # never breaks the read-only audit.
    grin_audited = []
    grin_not_audited = []
    try:
        from . import _grin_cells as _grin
        gap_by_surface = {g["surface"]: g for g in gaps}
        for i in range(n):
            r = rows[i]
            if not r.get("ok", True):
                continue                                   # degraded -> existing degraded channel
            tn = str(r.get("type_name", ""))
            is_prim = _grin.grin_type_of_name(tn) is not None
            is_fam = _grin.grin_family_type_of_name(tn) is not None
            if is_prim:
                g = gap_by_surface.get(i)
                if (g is not None and g.get("kind") == "glass"
                        and g.get("threshold") == min_glass):
                    grin_audited.append({
                        "surface": i, "next_surface": g["next_surface"], "kind": "glass",
                        "threshold": min_glass, "center_thickness": g["center_thickness"],
                        "edge_thickness": g["edge_thickness"], "violation": g["violation"],
                    })
                else:  # a classified primitive whose gap was SKIPPED/unmatched -> NOT audited
                    grin_not_audited.append(
                        {"surface": i, "type": tn, "reason": "gap_unaudited"})
            elif is_fam:  # family member, NOT authorable -> un-probed -> UNCONDITIONAL not-audited
                grin_not_audited.append(
                    {"surface": i, "type": tn, "reason": "grin_family_non_authorable"})
    except Exception:  # noqa: BLE001 — evidence assembly never breaks the read-only audit
        grin_audited, grin_not_audited = [], []

    if grin_audited:
        flags.append(
            f"GRIN: element(s) at surface(s) {[e['surface'] for e in grin_audited]} are a "
            f"SOLID medium, audited (edge AND center) at min_glass ({min_glass}) under the "
            "solid-medium geometric policy (tunable via min_glass; NOT a catalog-glass "
            "assertion). Their internal index profile is not drawn."
        )
    if grin_not_audited:
        flags.append(
            f"GRIN: surface(s) {[e['surface'] for e in grin_not_audited]} are a recognized "
            "GRIN family member NOT covered by the solid-medium geometric audit "
            "(loaded/non-authorable representation, un-probed, OR its gap was skipped) — NOT "
            "audited for edge/center manufacturability. Treat their edge and center "
            "clearance as UNKNOWN and check them in OpticStudio."
        )

    # (MCE, D3) The per-config divergence headline: the SMALLEST finite
    # gap clearance (min over each gap's center+edge). A zoom's gaps move per config,
    # so this differs across configs on a real sweep (a repeated value across configs
    # is the silent-wrong switch signature the driver's config_differs surfaces).
    config_headline = _min_gap_clearance(gaps if not folded else folded_gaps)

    # S6 Delta-2: the union of all Fixed-SemiDiameter (frozen) surface numbers (additive, the
    # global counterpart to the per-gap frozen flag). Populated on a FOLDED system too (the
    # semi_solve is read per-row regardless; a fold has no per-gap edge to flag). Empty for an
    # all-auto/all-variable (un-frozen) system. A Fixed solve is GLOBAL (one solve, not per-
    # config), so every config reads the same set — no config-sweep reconcile concern.
    frozen_semi_surfaces = sorted({
        i for i in range(n) if rows[i].get("semi_solve") == "Fixed"
    })

    result = {
        "ok": True,
        "tool": "check_clearance",
        "folded": bool(folded),
        "min_air": min_air,
        "min_glass": min_glass,
        "config_headline": config_headline,
        "gaps": gaps,
        "violations": violations,
        "folded_gaps": folded_gaps,
        "frozen_semi_surfaces": frozen_semi_surfaces,
        "global_bfd": bfd,
        # (additive, non-breaking): the EvenAspheric surface numbers whose
        # edge was modelled from the FULL asphere sag (the S1 ``asphere_sag_ignored`` flag
        # is REPLACED by real geometry). Empty for an all-spherical system.
        "asphere_sag_modelled": asphere_sag_modelled,
        # Fail-closed disclosure: EvenAspheric surfaces whose coefficients could NOT be read
        # (a degraded/unreadable row) — their edge is conic-only-approximate, never silently
        # presented as faithful. Empty when every asphere modelled cleanly.
        "asphere_sag_approximate": asphere_sag_approximate,
        "flags": flags,
    }
    # (clearance-provenance, DQ-6): the additive sag-model-applicability list — emitted
    # ONLY when non-empty (the shipped ``grin_not_audited`` precedent), so a
    # Standard/asphere system's envelope stays byte-identical.
    if sag_model_unfaithful:
        result["sag_model_unfaithful"] = sag_model_unfaithful
    # GRIN: the additive index-not-audited surface list — emitted ONLY when
    # non-empty (a non-GRIN system stays byte-for-byte unchanged).
    if grin_index_profile_not_drawn:
        result["grin_index_profile_not_drawn"] = grin_index_profile_not_drawn
    # The additive positive-audit + unconditional not-audited keys, emitted
    # ONLY when non-empty (a non-GRIN system stays byte-for-byte unchanged). ``audited`` maps
    # one-to-one to a real evaluated kind:"glass" min_glass gap (audited-and-passed
    # violation:false vs audited-and-violated violation:true vs never-examined absent).
    if grin_audited:
        result["grin_geometric_audit"] = {
            "basis": "grin_surface_type",   # NOT an AGF/catalog glass assertion
            "threshold": min_glass,          # the applied solid-medium floor (tunable)
            "audited": grin_audited,         # one entry per REAL evaluated kind:"glass" min_glass gap
        }
    if grin_not_audited:
        result["grin_not_audited"] = grin_not_audited
    # Tier-1 (AXIS 3): the additive ``center_ceiling_audit`` block. Emitted ONLY when a
    # budget was APPLIED -- in force AND all four preconditions held -- so an envelope
    # with no budget is BYTE-IDENTICAL to the shipped one, per-gap keys included.
    # ``limits`` are the RESOLVED DECLARED values, never re-derived from the gap records
    # (a limit re-derived from what it adjudicated cannot falsify anything).
    if ceiling_basis is not None:
        # ``limits`` is a SCALAR pair and is correct for ``caller``/``declared``, which
        # really are uniform. It is MEANINGLESS for a per-surface basis, so on
        # ``active_merit`` it is emitted as ``null`` and ``limits_by_surface`` (inside
        # the gap-ceiling channel) carries the real answer. ``null`` is honest -- there
        # is no single limit -- and a consumer that reads ``limits["glass"]`` anyway gets
        # a failure rather than a confidently wrong number.
        result["center_ceiling_audit"] = {
            "basis": ceiling_basis,
            "limits": (
                None if ceiling_table is not None
                else {"air": max_air, "glass": max_glass}
            ),
            "status": _ceiling_status(folded, gaps, gap_ceiling),
            # A FOLD suppresses the per-gap audit entirely (the raw LDE thickness is the
            # fold direction, not a length), so both lists are empty and the status says
            # so -- never an empty ``exceedances`` read as "nothing is over budget".
            "exceedances": gap_ceiling["exceedances"] if gap_ceiling else [],
            "unresolved": gap_ceiling["unresolved"] if gap_ceiling else [],
        }
        if ceiling_table is not None:
            # The per-surface answer, plus what the SCAN could not establish -- kept
            # separate from what the DESIGN says, because "no ceiling is declared here"
            # and "I could not finish reading" are opposite facts.
            result["center_ceiling_audit"]["limits_by_surface"] = (
                gap_ceiling.get("limits_by_surface", {}) if gap_ceiling else {}
            )
            result["center_ceiling_audit"]["scan"] = ceiling_table.disclosure()
    elif active_merit_not_offered is not None:
        # No basis at all, but the reason is not "nobody declared one" -- the design may
        # well declare ceilings this call could not honestly ATTRIBUTE. Disclosed so the
        # downstream NO_ORACLE is legible rather than mute.
        #
        # A TOP-LEVEL key, deliberately NOT a ``center_ceiling_audit`` block with a null
        # basis. The presence of that block has always meant "a budget was applied"; a
        # half-shaped one carrying no ``status``, ``exceedances`` or ``limits`` would
        # overload the same key with the opposite fact, which is the collapse this whole
        # cycle exists to prevent -- one level up from the ABSENT/UNREADABLE one.
        result["active_merit_not_offered"] = active_merit_not_offered
    if note is not None:
        result["note"] = note
    return result


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
CHECK_CLEARANCE_SPEC = ToolSpec(
    name="check_clearance",
    handler=check_clearance,
    required_params=(),
    param_types={
        "min_air": "number",
        "min_glass": "number",
        "max_air": "number",
        "max_glass": "number",
        # config is a UNION None|int|"all"; advertised "number" (the dominant int type
        # — the MCP reparse shim preserves the "all" string via raw-fallback).
        "config": "number",
    },
    description=(
        "Audit a design's manufacturability and detector clearance (read-only, never "
        "mutates). For an UNFOLDED system: per-gap CENTER and EDGE thickness with "
        "violations flagged when a gap falls below min_air (default 0.5) for an air "
        "gap or min_glass (default 1.0) for a glass element (a NEGATIVE edge means a "
        "steep surface crosses its neighbour) — the back-airgap is audited as air. For "
        "a FOLDED system (any coordinate-break or mirror): the per-gap thickness audit "
        "is suppressed (a folded LDE thickness is the fold direction, NOT a clearance) "
        "and informational folded_gaps + a note are returned instead. ALWAYS returns a "
        "global_bfd block (the image plane's global distance behind the first and last "
        "optical surface) — for a folded/Cassegrain system this behind_first_optic is "
        "the true behind-primary clearance, NOT the misleading raw back-airgap "
        "thickness that get_first_order.back_focal_length reports. "
        "Optional max_air/max_glass declare a maximum CENTRE thickness: each gap of that "
        "kind then carries center_ceiling {limit, state in within|over|unreadable} and the "
        "envelope carries a center_ceiling_audit block. A ceiling exceedance is reported "
        "THERE and never in violations (which stays the manufacturability FLOOR list), so "
        "promote_best never refuses a design for being long. Omit both and a budget "
        "declared at build_merit for this design is applied automatically while it still "
        "provably describes it; pass explicit values to override. "
        "Run after optimize to catch a thin/negative gap a merit floor missed. An authored "
        "GRIN element (Gradient2/Gradient3) is audited as a solid (glass) element at "
        "min_glass; see grin_geometric_audit. A non-authorable GRIN family member is listed "
        "under grin_not_audited. See get_first_order, describe_surfaces."
    ),
)

TOOL_SPECS = (CHECK_CLEARANCE_SPEC,)
