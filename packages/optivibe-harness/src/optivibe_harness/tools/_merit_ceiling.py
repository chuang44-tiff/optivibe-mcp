"""The ACTIVE-MERIT centre-thickness ceiling reader (V-INT D1-b).

A design carries its own manufacturing ceilings inside its merit function: an ``MXCG``
row declares a maximum CENTRE thickness for a glass gap, an ``MXCA`` row for an air gap.
Before this module those ceilings were reachable only if THIS ``check_clearance`` call
passed them (``basis:"caller"``) or THIS session had declared them at ``build_merit``
(``basis:"declared"``) -- so for every design loaded from disk the audit that would
refute or confirm a "this element looks too thick" finding had nothing to compare
against and booked ``NO_ORACLE``.

This reader supplies the third basis, ``active_merit``, and the claim it makes is
EXACTLY: **the ceilings the merit function declared ACROSS THE INTERVAL THIS READ
SAMPLED.** Nothing more, and in particular not "the ceilings the merit declares" -- see
``_verify_snapshot`` for the two mutation shapes (ABA and post-final-sample) that are
unclosable without a transaction the engine does not offer.

▶ THIS PARAGRAPH SAID SOMETHING WIDER UNTIL ROUND 5 CAUGHT IT. It read *"the ceilings the
merit function ACTIVE AT READ TIME declares... nothing can falsify it, so nothing needs to
guard it"* -- written before the reader had any verification at all, and left standing when
round 4 narrowed the claim in a comment 700 lines below. **The narrowing was applied at one
site and not at its sibling, and the sibling was the sentence a reader sees FIRST** -- the
same class as the defect the narrowing existed to describe. Pinned now by
``test_ext_r5_the_opening_contract_AGREES_with_the_narrowed_one``.

What IS still true by construction, and the reason there is no suppression flag: the basis
claims nothing about a merit OTHER than the one it read, so no later mutator can divorce
the claim from its subject the way ``load_merit`` can divorce ``declared``. ``server.py``'s
``IDENTITY_PRESERVING_TOOLS`` deliberately lists every merit mutator with a written
ruling that the ``declared`` claim was narrowed to the DECLARATION EVENT; a suppression
flag here would contradict that shipped ruling and would go stale in the permissive
direction the moment a future mutator was added.

WHY A NEW READER RATHER THAN ``serialize_merit``. ``optimize_merit_io`` returns an error
envelope for the WHOLE serialization when a single row read throws, so one unrelated
malformed operand would suppress every ceiling; per-row drop-and-continue is
unimplementable through that door. Measured on a 211-operand merit, that door costs
5154 ms against this reader's 227 ms, so the rejection is correct on behaviour and on
cost.

THIS MODULE NEVER READS ``op.Value`` OR ``op.Contribution``. That is not a preference:
it makes the stale-read trap STRUCTURALLY UNREACHABLE, because no
``CalculateMeritFunction()`` recompute is ever needed to answer the question this reader
asks. Pinned by an AST test over this file, not by this sentence.

Spec: ``
(contract rev 10) sections 3.1-3.6. Every rule number below (Rule 0..Rule 17, R-TAINT,
R-INCOMPLETE) is that document's decision list, and the ORDER is the contract.
"""

from __future__ import annotations

import math
import os
import time

from . import _merit_cells

# --------------------------------------------------------------------------- #
# The lookup vocabulary. THREE states, no fourth, no default.
# --------------------------------------------------------------------------- #
# A nullable scalar cannot carry this decision (spec 3.3): rev 2's
# ``limit_for -> float | None`` made ``None`` mean ABSENT while the taxonomy also needed
# a distinct UNREADABLE, so a caller receiving ``None`` could not tell them apart,
# omitted the ceiling object, and reached the floor arm -- manufacturing a refutation
# out of a source nobody could read. The state is the primary token and is NEVER
# inferred from the limit.
CEILING_LOOKUP_PRESENT = "present"
CEILING_LOOKUP_ABSENT = "absent"
CEILING_LOOKUP_SOURCE_UNREADABLE = "source_unreadable"
CEILING_LOOKUP_STATES = frozenset({
    CEILING_LOOKUP_PRESENT,
    CEILING_LOOKUP_ABSENT,
    CEILING_LOOKUP_SOURCE_UNREADABLE,
})

#: The two gap kinds this reader can answer about. A taint of "BOTH kinds" is a taint of
#: exactly these, so the phrase has a referent rather than being open-ended.
KINDS = ("air", "glass")

#: The boundary operands and the kind each one bounds. ``MXCG`` bounds a glass gap,
#: ``MXCA`` an air gap; a gap of the OTHER kind gets ABSENT, never a borrowed limit
#: (the shipped ``_center_ceiling`` rule, preserved).
BOUNDARY_TYPES = {"MXCG": "glass", "MXCA": "air"}

#: A ``CONF`` row opens a bracket scoping later rows to one configuration. This reader
#: does not track brackets (spec section 8.4, deliberately deferred), so its PRESENCE
#: taints -- see Rule 0.
CONF_TYPE = "CONF"

#: The merit wizard's "no budget" default target. A row at or above it declares nothing.
SENTINEL_TARGET = 1000.0

#: ``_verify_snapshot``'s verdict. NOT a bool: a bare True/False threw away WHY the
#: verification failed, so a clock fault during it was swallowed without an
#: ``internal_fault`` and an expiry during it lost ``budget_exhausted`` -- degraded reads
#: reported as ordinary degradation, inside the module whose subject is that distinction.
#: Any OTHER value is the ``_safe_repr`` of a swallowed exception.
VERIFY_OK = "ok"
VERIFY_CHANGED = "changed"
VERIFY_EXPIRED = "expired"
VERIFY_FAULT = "fault"

#: ``_verify_snapshot`` returns ``(status, fault_text)`` -- TWO channels, not one string.
#:
#: ▶ ROUND 4: *"fault text and control status share the same string domain. If an
#: exception's repr is ``"ok"``, ``_verify_snapshot`` returns ``VERIFY_OK`` and the caller
#: accepts failed verification."* Reachable only through a hostile ``__repr__`` -- but the
#: whole reason this net exists is hostile objects, and a control token multiplexed with
#: free text is the same two-meanings-in-one-channel defect this module was written to
#: prevent, in the module's own control flow. So the channels are separated by TYPE, and
#: no exception payload can impersonate a status whatever it says.


def _safe_repr(exc):
    """``repr(exc)``, bounded and ASCII-sanitised, and it CANNOT itself raise.

    Two problems with a bare ``repr(exc)`` in a never-raises net, both named by the
    Audit: a hostile ``__repr__`` **escapes the very net that called it**, and the
    text is disclosed verbatim into an envelope. So the call is guarded, the result is
    forced through the exact ``str`` type, non-ASCII is replaced rather than carried, and
    the length is capped.
    """
    fallback = "unrepresentable exception"
    text = None
    try:
        text = str.__str__(repr(exc))
    except BaseException:  # noqa: BLE001 - a hostile __repr__ must not escape the net
        try:
            text = type(exc).__name__
        except BaseException:  # noqa: BLE001 - even the type name can be hostile
            text = None

    # ▶ EXACT ``str``, ENFORCED (round 4): *"its fallback does not enforce an exact-string
    # result if a hostile metaclass ``__name__`` returns an OBJECT rather than raising;
    # such an object can escape ``_safe_repr`` and later raise during truth testing or
    # serialization."* A hostile ``__name__`` need not raise to be hostile -- it can simply
    # return something that is not a string, and every guard above was written against the
    # RAISING case only.
    if type(text) is not str:
        return fallback
    try:
        # PRINTABLE, not merely ASCII: a control character in an envelope is a payload the
        # reader did not ask for, and a NUL can truncate a downstream consumer.
        clean = "".join(
            ch if 32 <= ord(ch) < 127 else "?"
            for ch in text.encode("ascii", "replace").decode("ascii")
        )[:200]
    except BaseException:  # noqa: BLE001
        return fallback
    # NONEMPTY, so a caller can never read "" as "nothing went wrong".
    return clean if clean else fallback

# --------------------------------------------------------------------------- #
# The budget.
# --------------------------------------------------------------------------- #
_DEFAULT_CEILING_BUDGET_S = 2.0
_BUDGET_ENV = "OPTIVIBE_MERIT_CEILING_BUDGET_S"


def ceiling_budget_s():
    """Read ``OPTIVIBE_MERIT_CEILING_BUDGET_S`` (default 2.0) with the shipped clamp.

    POSITIVE-FINITE clamp, mirroring ``_spot_validity.validity_budget_s`` /
    ``layout_render._ray_budget_s`` / ``__main__._env_float``: a non-positive value
    (0 / negative) makes the deadline already-elapsed, so EVERY scan reads as incomplete
    and no design ever gets a merit basis; a non-finite value (nan / inf) DEFEATS the
    budget entirely (``perf_counter() >= nan`` is False -> unbounded). Any of those
    falls back to the default.

    HEADROOM, stated as the single measurement it is: 227 ms bought 211 operands, so
    2.0 s covers roughly 1800 -- an order of magnitude above the largest merit measured,
    on one merit and one machine. The default is a backstop, not a characterised
    distribution (spec section 8.3).
    """
    raw = os.environ.get(_BUDGET_ENV)
    if raw is None:
        return _DEFAULT_CEILING_BUDGET_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CEILING_BUDGET_S
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_CEILING_BUDGET_S
    return value


# --------------------------------------------------------------------------- #
# The row classifier -- the decision list, as one ordered chain.
# --------------------------------------------------------------------------- #
class _Unread:
    """The sentinel for a reading that THREW or was MISSING.

    Deliberately NOT ``None``: ``None`` is a value a cell could conceivably carry, and
    the whole subject of this module is that "absent" and "unreadable" must not collapse
    into one token. A distinct object cannot be confused with either.
    """

    __slots__ = ()

    def __repr__(self):  # pragma: no cover - diagnostic only
        return "<unread>"


UNREAD = _Unread()

# The per-row ACTIONS. A row's outcome is one of these, and the classifier is a total
# function onto them.
ACT_TAINT_BOTH = "taint_both"     # the kind itself is unknown -> neither kind is safe
ACT_TAINT_KIND = "taint_kind"     # the kind is known, the surface is not
ACT_SKIP = "skip"                 # not a boundary row: contributes nothing, not a drop
ACT_NOTHING = "nothing"           # a boundary row that declares no per-gap budget
ACT_UNREADABLE = "unreadable"     # this (surface, kind) cannot be answered
ACT_ADMIT = "admit"               # the only admitting action


class RowOutcome:
    """``(rule, action, kind, surface, limit)`` -- what ONE merit row contributes."""

    __slots__ = ("rule", "action", "kind", "surface", "limit")

    def __init__(self, rule, action, kind=None, surface=None, limit=None):
        self.rule = rule
        self.action = action
        self.kind = kind
        self.surface = surface
        self.limit = limit

    def __repr__(self):  # pragma: no cover - diagnostic only
        return "RowOutcome(%r, %r, kind=%r, surface=%r, limit=%r)" % (
            self.rule, self.action, self.kind, self.surface, self.limit)


def surface_key(surface):
    """A surface index rendered as an envelope KEY -> ``str``.

    STRING, because the envelope crosses the MCP boundary as JSON where object keys are
    strings BY THE FORMAT. An ``int`` key silently becomes ``"1"`` in transit, so a
    consumer doing ``limits_by_surface.get(surface)`` with an int gets the record
    in-process and ``None`` over the wire -- and ``None`` there reads as "no ceiling for
    this surface", which is ABSENT manufactured out of a serializer detail. Measured, not
    reasoned: the round-trip is asserted in ``test_a_limits_...``.

    Rendered through the shared base-slot normalizer, and the reason is UNIFORMITY rather
    than a live threat: every caller-controlled value used as a key goes through one
    spelling, which is the discipline ``test_r12_the_repo_wide_backlog_does_not_grow``
    encodes. **The value here is a ``range()`` loop index -- an exact ``int`` whose
    ``__str__`` cannot be overridden** -- so there is nothing to defeat and this call is a
    no-op. Stated plainly so a later reader does not infer that surface indices have been
    seen forged.
    """
    from . import _optimize_common as _oc   # local: _optimize_common imports _merit_cells

    return _oc._base_token(surface)


def _integral(value):
    """The integral-surface-index question -> ``int`` or ``None``.

    Delegates to ``_merit_cells.resolve_integral``, which that module's own docstring
    names as **THE ONE BODY** behind "is this an integral surface index?" and which any
    door built over it is required to use rather than re-implementing. It accepts
    an ``int`` at any magnitude and a ``float`` whose value is integral, rejects a
    ``bool`` FIRST (``True == 1`` would otherwise pass as surface 1), and refuses a
    conversion that does not repeat. It never raises.

    BUILD-TIME FINDING, recorded rather than silently resolved. SPEC-D1 section 3.4
    defines "integral" as *"an ``int``, or a ``float`` whose ``is_integer()`` is True. A
    ``bool`` is NOT integral"* and pins it with A-INTEGRAL (*"``Surf1 = 3.0`` is integral
    and admitted; ``Surf1 = True`` is NOT and taints"*) -- but its parenthetical names
    ``is_integral_int``, which is ``_tol_cells``' WRITER predicate and REJECTS ``3.0``
    outright. The two spec statements agree with each other and with
    ``resolve_integral``; the citation is the outlier, so the frozen BEHAVIOUR is
    implemented and the slip is reported in AUDIT-RESPONSE.md rather than papered over.
    """
    if value is UNREAD:
        return None
    resolved, reason = _merit_cells.resolve_integral(value)
    if reason != _merit_cells.NUMERIC_OK:
        return None
    return resolved


def _finite_number(value):
    """A FINITE real reading -> ``float``, else ``None``. Rejects ``bool`` and UNREAD.

    Delegates to ``_merit_cells.resolve_double``, which that module names as **THE ONE
    BODY** behind "is this a usable double?". Written locally first, and replaced
    on measurement: the local version agreed with it on every reachable input and was
    WEAKER on two unreachable ones, which is exactly the shape a duplicated predicate
    takes before it drifts.

    What the shared body adds:

    - **the stability probe.** A ``__float__`` that returns a DIFFERENT value
      on successive calls is refused rather than read twice and used once. Measured
      there: ``float(int_subclass)`` dispatches ``__float__`` and not ``__int__``, so the
      double arm's instability vector is independent of the integer one.
    - **an ``OverflowError`` diagnosis** on a huge ``int`` instead of a generic catch.

    What it deliberately does NOT do, and this is the shared body's documented ruling
    rather than a gap: a STABLE ``__float__`` override is read AT its override value. A
    forging float subclass therefore reports its forged number -- correctly, because the
    writer converts the same way, so reading it there is agreement rather than
    divergence. It also cannot arrive from the engine: a .NET Double marshals to an exact
    ``float``, never a subclass. Contrast the ``TypeName`` read, where the forge IS
    defeated -- there the value is a STRING and ``_base_token`` can reach the base slot.
    """
    if value is UNREAD:
        return None
    resolved, reason = _merit_cells.resolve_double(value)
    if reason != _merit_cells.NUMERIC_OK:
        return None
    return resolved


def classify_row(type_name, surf1, surf2, weight, target, n_surfaces):
    """The DECISION LIST (spec 3.4 Pass 1) -- FIRST MATCH WINS, and the order is frozen.

    Every argument is either a live reading or ``UNREAD`` (threw / missing). The chain is
    total: every input reaches exactly one ``return``, which is what makes the partition
    disjoint (no input matches two outcomes) and exhaustive (none matches zero). A-PART
    asserts that by enumeration over a named corpus rather than by this argument.

    Three orderings are load-bearing and each closes a measured hole:

    - **Rule 6 before Rule 9.** Without Rule 6 an unreadable ``Surf2`` falls through to the range
      test as ``Surf2 != Surf1 -> ABSENT`` and MANUFACTURES "no ceiling declared" out of
      a source nobody could read. That was CRITICAL, and it is the reason
      the ``Surf2`` arm exists at all.
    - **Rule 11 before Rule 13.** An intentional ``+inf`` target and a NaN COERCED to ``+inf``
      are indistinguishable once stored (PROBE-vint section 14.2), so the non-finite test
      must run BEFORE the sentinel comparison; reversed, ``+inf >= 1000`` reads as the
      wizard's "no budget" default and a design silently loses its ceiling.
    - **Rule 7 before Rule 11.** A row driving nothing is inert WHATEVER its target, so a
      zero-weight ``+inf`` row is ABSENT, not unreadable. A--INF owns that cell.

    DEFENSIVE ARMS, labelled so a later reader does not infer an observed state.
    PROBE-vint section 14.2 measured that a NaN target/weight CANNOT be stored -- it is
    coerced to ``+inf`` before it arrives -- so the non-finite arms of Rule 5 and Rule 11 cover
    ``+inf`` in practice and NaN only in principle. The ``bool`` arms (inside Rule 5, and Rule 10)
    are likewise defensive: no measured corpus row carries one. R4b is defensive too and
    says so at its own branch.
    """
    # Rule 1 -- the TypeName read threw. The kind is unknown, so no surface of EITHER kind
    # can be exonerated: a row we cannot classify might have been this gap's row.
    if type_name is UNREAD:
        return RowOutcome("Rule 1", ACT_TAINT_BOTH)

    # Rule 2 -- not a boundary operand. Contributes nothing; this is a SKIP, not a drop, and
    # the distinction matters because a drop would be evidence of a fault and this is not.
    kind = BOUNDARY_TYPES.get(type_name)
    if kind is None:
        return RowOutcome("Rule 2", ACT_SKIP)

    # Rule 3 -- Surf1 threw / missing / not integral. A thrown Surf1 leaves NO surface to
    # taint, so the taint must WIDEN to the kind rather than vanish.
    s1 = _integral(surf1)
    if s1 is None:
        return RowOutcome("Rule 3", ACT_TAINT_KIND, kind=kind)

    # R4a -- the OBJECT surface. MEASURED BENIGN: 6 real rows across 3 designs of the 53
    # tracked .zmx repo-wide, always as a paired ``MXCA 0 0`` + ``MXCG 0 0`` wizard
    # pattern (spec section 16.1). ``_audit_gaps`` starts at i = 1, so the object gap is
    # never audited and a row there describes nothing. Tainting it would kill the merit
    # basis for 3 of 53 designs for no reason at all.
    if s1 == 0:
        return RowOutcome("R4a", ACT_NOTHING, kind=kind)

    # R4b -- out of the audited-gap domain (1 .. n-2). The upper bound is n-2, not n-1:
    # ``_audit_gaps`` walks i -> i+1, so a row on the IMAGE surface begins no gap. Being
    # out of domain is evidence that the merit's surface numbering no longer matches the
    # LDE, which makes the IN-domain rows suspect too -- hence a taint, not an ABSENT.
    # DEFENSIVE: 0 occurrences across all 53 tracked .zmx repo-wide (section 16.1).
    if s1 < 0 or s1 > n_surfaces - 2:
        return RowOutcome("R4b", ACT_TAINT_KIND, kind=kind)

    # Rule 5 -- the weight threw / is non-finite / is a bool. Only ``+inf`` is reachable.
    w = _finite_number(weight)
    if w is None:
        return RowOutcome("Rule 5", ACT_UNREADABLE, kind=kind, surface=s1)

    # Rule 6 -- THE ROUND-3 CRITICAL. See the ordering note in this docstring.
    s2 = _integral(surf2)
    if s2 is None:
        return RowOutcome("Rule 6", ACT_TAINT_KIND, kind=kind)

    # Rule 7 -- a row driving nothing is not a declared budget.
    if w == 0:
        return RowOutcome("Rule 7", ACT_NOTHING, kind=kind, surface=s1)

    # Rule 8 -- a negative weight is authorable with ``ok: true`` and what it MEANS to a
    # boundary operand was never measured. Fail-CLOSED deliberately; treating it as
    # ABSENT is defensible and is the spec's own section 8.1 open item.
    if w < 0:
        return RowOutcome("Rule 8", ACT_UNREADABLE, kind=kind, surface=s1)

    # Rule 9 -- a RANGE row. Gotcha: MXCA/MXCG over Surf1..Surf2 constrain thickness
    # SUMMED over the range, so the row is attributable to no single gap. It read fine;
    # it declares nothing PER GAP. Exercised by a real corpus row (section 15.1).
    if s2 != s1:
        return RowOutcome("Rule 9", ACT_NOTHING, kind=kind)

    # Rule 10 -- a bool target ahead of the finiteness test, so ``True == 1`` cannot reach
    # Rule 14 and be admitted as a 1 mm ceiling.
    if isinstance(target, bool):
        return RowOutcome("Rule 10", ACT_UNREADABLE, kind=kind, surface=s1)

    # Rule 11 -- THE ORDERING THAT RESOLVES +inf. See the docstring.
    t = _finite_number(target)
    if t is None:
        return RowOutcome("Rule 11", ACT_UNREADABLE, kind=kind, surface=s1)

    # Rule 12 -- not a budget and not the sentinel. ``-0.0`` lands here (``-0.0 <= 0``),
    # which A-NEG0 pins.
    if t <= 0:
        return RowOutcome("Rule 12", ACT_UNREADABLE, kind=kind, surface=s1)

    # Rule 13 -- the wizard's "no budget" default.
    if t >= SENTINEL_TARGET:
        return RowOutcome("Rule 13", ACT_NOTHING, kind=kind, surface=s1)

    # Rule 14 -- the ONLY admitting rule.
    return RowOutcome("Rule 14", ACT_ADMIT, kind=kind, surface=s1, limit=t)


# --------------------------------------------------------------------------- #
# The table.
# --------------------------------------------------------------------------- #
class CeilingTable:
    """The per-(surface, kind) answer set, plus what the scan could not establish.

    Built ONLY by ``read_ceiling_table``. Pass 2 of the spec (Rule 15/Rule 16/Rule 17) and R-TAINT
    are realised HERE, in ``limit_for``, rather than baked into the stored data, so the
    precedence is visible in one place and a test can drive it directly.
    """

    __slots__ = ("_admitted", "_unreadable", "_tainted", "rows_scanned",
                 "boundary_rows", "budget_exhausted", "complete", "internal_fault")

    def __init__(self, admitted, unreadable, tainted, *, rows_scanned=0,
                 boundary_rows=0, budget_exhausted=False, complete=True):
        self._admitted = admitted        # {(surface, kind): set-of-limits}
        self._unreadable = unreadable    # {(surface, kind)}
        self._tainted = frozenset(tainted)
        self.rows_scanned = rows_scanned
        self.boundary_rows = boundary_rows
        self.budget_exhausted = bool(budget_exhausted)
        self.complete = bool(complete)
        #: Set ONLY by ``read_ceiling_table``'s outer net, and only when it swallowed
        #: something. ``None`` on every ordinary path, so its presence in the disclosure
        #: means exactly "a fault was masked here" and never "nothing went wrong".
        self.internal_fault = None

    @property
    def tainted_kinds(self):
        return self._tainted

    def limit_for(self, surface, kind):
        """-> ``(state, limit)``. ``limit`` is a finite float IFF state == PRESENT.

        ``limit`` is ``None`` in BOTH other states, and the state is NEVER inferred from
        the limit. This function returns a bare float NEVER, and ``None`` as an answer
        NEVER.

        THE ORDER IS THE PRECEDENCE, and it is the second half of CRITICAL:

        1. an unknown kind -- fail CLOSED. Unreachable by construction (``_gap_kind``
           yields only ``"air"`` / ``"glass"``), and if it ever became reachable, "I have
           no rule for this kind" is not the same as "no ceiling was declared".
        2. **R-TAINT.** A tainted kind has NO ``ABSENT`` and NO ``PRESENT`` answers at
           all -- the taint overrides Rule 15's ABSENT and overrides an otherwise-PRESENT
           lookup alike. Stated as a PROPERTY of "a rule that taints", never as a list
           of which rules those are: an enumeration goes stale by having something added
           elsewhere, which is exactly how R-INCOMPLETE came to say "taints both kinds"
           while sitting outside the list that made a taint dominate.
        3. Rule 5/Rule 8/Rule 10/Rule 11/Rule 12 recorded this (surface, kind) as unreadable. Checked BEFORE
           the admitted set, because an unreadable row for a gap that ALSO has an
           admitted row may have been a second, conflicting budget -- an Rule 17 conflict we
           cannot see.
        4. **Rule 16 / Rule 17.** One distinct admitted target -> PRESENT (identical duplicates
           admit once, by set membership). Two or more DIFFERENT targets -> we cannot say
           which governs -> SOURCE_UNREADABLE.
        5. **Rule 15.** No admitted row for THIS gap -> ABSENT. Per gap, never "covered by
           the family": a ceiling declared for surface 1 says nothing about surface 5.
        """
        if kind not in KINDS:
            return (CEILING_LOOKUP_SOURCE_UNREADABLE, None)
        if kind in self._tainted:
            return (CEILING_LOOKUP_SOURCE_UNREADABLE, None)
        key = (surface, kind)
        if key in self._unreadable:
            return (CEILING_LOOKUP_SOURCE_UNREADABLE, None)
        limits = self._admitted.get(key)
        if limits:
            if len(limits) == 1:
                return (CEILING_LOOKUP_PRESENT, next(iter(limits)))
            return (CEILING_LOOKUP_SOURCE_UNREADABLE, None)
        return (CEILING_LOOKUP_ABSENT, None)

    def declares_anything(self):
        """Can this table produce a NON-``ABSENT`` answer for any lookup at all?

        THE BASIS-IN-FORCE PREDICATE, and it is a BUILD-TIME decision the
        does not settle (recorded in AUDIT-RESPONSE.md rather than taken silently). A
        merit carrying no boundary row declares nothing -- that is ``ABSENT``, which is
        exactly "no ceiling in force", which is exactly today's behaviour -- so such a
        design must keep its byte-identical envelope and NOT gain a
        ``center_ceiling_audit`` block asserting a basis that bounds no gap. Claiming
        one would overload "a budget was applied" with "no budget exists", which is the
        ABSENT/UNREADABLE collapse this module exists to prevent, one level up.

        A-BUDGET-0 is what settles it, and it settles it the right way: an expiry before
        the first row must be DISTINGUISHABLE from a merit with no boundary rows. Under
        this predicate it is, structurally -- an expiry taints, a taint is an answer, so
        the block appears with every lookup ``source_unreadable``; a rowless merit is
        answerless, so no block appears at all. The two degrade in opposite directions
        and cannot be confused.
        """
        return bool(self._tainted or self._unreadable or self._admitted)

    def disclosure(self):
        """The additive envelope facts about the SCAN, never about the design."""
        out = {
            "rows_scanned": self.rows_scanned,
            "boundary_rows": self.boundary_rows,
            "scan_complete": self.complete,
        }
        if self.budget_exhausted:
            out["budget_exhausted"] = True
        if self._tainted:
            out["tainted_kinds"] = sorted(self._tainted)
        if self.internal_fault:
            # Emitted ONLY when the outer net actually caught something, so an envelope
            # carrying this key is a report of a MASKED DEFECT, not routine degradation.
            out["internal_fault"] = self.internal_fault
        return out


# --------------------------------------------------------------------------- #
# The reader.
# --------------------------------------------------------------------------- #
_WANTED_HEADERS = ("Surf1", "Surf2")


def _read_surface_params(op):
    """The EARLY-EXIT Header walk -> ``(surf1, surf2)``, either possibly ``UNREAD``.

    ``read_param_map`` walks cols 2..9 and reads EVERY non-blank one type-aware, costing
    10.5 ms per row -- 65.5% of the whole reader -- to obtain two values. This stops the
    moment both wanted Headers are in hand, using THE SAME Header-first accessors
    (``read_cell_kind`` / ``read_cell``). Column position is never the discriminator: a
    blank cell reports its ``DataType`` arbitrarily, which is why ``_merit_cells`` is
    Header-keyed by design.

    IT WAS VERIFIED BEFORE IT WAS PREFERRED: ``full_vs_early: true``, zero mismatches
    across all 22 boundary rows of the corpus merit (PROBE-vint section 14.4). A-EQUIV
    keeps that check running rather than trusting this paragraph.

    ``op.Param1`` is NOT an option and is recorded so a later round does not re-propose
    it: ``AttributeError: 'IMFERow' object has no attribute 'Param1'`` (section 14.4).

    EARLY EXIT MUST NOT MASK Rule 6. A walk that ends without finding ``Surf2`` returns
    ``UNREAD`` for it -- MISSING, not absent-and-fine -- so the row takes Rule 6 and taints.
    "I stopped looking" and "it is not there" are never the same outcome here.

    A THROW ANYWHERE IN THE WALK returns BOTH as ``UNREAD``, so the row takes Rule 3 (F-4).
    Stated because the likely implementations all land there by inference and a coder
    could equally have routed it to a generic row-drop.
    """
    found = {}
    try:
        for col in _merit_cells._PARAM_COLS:
            header, cell_kind = _merit_cells.read_cell_kind(op, col)
            if cell_kind == "blank":
                continue
            if header in _WANTED_HEADERS:
                _, value = _merit_cells.read_cell(op, col)
                found[header] = value
                if len(found) == len(_WANTED_HEADERS):
                    break
    except Exception: # noqa: BLE001 - F-4: a throw anywhere -> both unresolved -> Rule 3
        return (UNREAD, UNREAD)
    return (found.get("Surf1", UNREAD), found.get("Surf2", UNREAD))


def _read_scalar(op, name):
    """Read ``Target`` / ``Weight`` -> the value, or ``UNREAD`` on a throw."""
    try:
        return getattr(op, name)
    except Exception:  # noqa: BLE001 - a read fault is UNREAD, never a fabricated number
        return UNREAD


def read_ceiling_table(system, n_surfaces, *, budget_s=None, clock=None):
    """Read the ACTIVE merit's ceilings -> ``CeilingTable``. NEVER raises.

    TWO PHYSICAL PASSES over ONE clock, and both halves of that sentence are frozen
    (spec 3.4, an earlier finding -- a cold reader said they would not write the main loop without it).

    **Pass 0 is its own walk over every row and COMPLETES before any row is admitted in
    Pass 1.** It is not a flag set opportunistically during admission. It reads
    ``TypeName`` only, which is the cheap walk: ``GetOperandAt`` + the TypeName marshal
    over 211 operands is 107 ms of the reader's 227 (section 14.4), and Pass 1's Header
    walk then runs only on the boundary rows Pass 0 identified.

    **Rule 0** -- ANY ``CONF`` row taints both kinds. A ``CONF`` row opens a bracket scoping
    later rows to one configuration, and a bracket-blind reader attributes them to
    whatever configuration happens to be active. MEASURED LOAD-BEARING, not defensive: a
    tracked multi-config keeper carries 10 CONF rows and 37 boundary rows while the
    single-config design carries zero, so this costs the common case nothing and saves
    the real one (section 15.2). It is deliberately keyed on CONF PRESENCE and never on
    configuration COUNT -- A-CONF-PRESENCE pins that with an AUTHORED fixture, because no
    tracked design breaks the confound (all 5 multi-config designs carry CONF rows).

    **R-INCOMPLETE** -- a scan that does not run to completion, for ANY reason, budget or
    fault, leaves the Rule 0 question UNANSWERED rather than answered "no", and taints BOTH
    kinds. It SUBSUMES the budget rule rather than sitting beside it, so there is exactly
    one "I did not finish" behaviour instead of two that must agree. In particular an
    expiry NEVER preserves already-resolved answers: a ``PRESENT`` read at row 3 does not
    survive an expiry at row 40, because the rows after the cut are exactly the ones that
    might have contained the CONF row, or a conflicting duplicate.

    A timeout therefore cannot fall through to the floor arm and manufacture a
    refutation, and it needs no new vocabulary -- its degrade path is a state the
    taxonomy already had.

    ONE MALFORMED ROW DOES NOT ABORT THE SCAN (A-ROWDROP): every row read is inside its
    own guard and contributes its own outcome. That is the behaviour ``serialize_merit``
    cannot offer and the reason this reader exists.
    """
    try:
        return _read_ceiling_table(system, n_surfaces, budget_s=budget_s, clock=clock)
    except Exception as exc:  # noqa: BLE001 - see below; the contract is NEVER RAISES
        # ▶ THE OUTER NET, and it closes a CLASS rather than the instance that prompted
        # it (a review, PART 1): *"a fault from the injectable clock is outside an
        # enclosing guard and can escape instead of becoming R-INCOMPLETE, so the literal
        # 'ANY reason' contract is not structurally complete."*
        #
        # Every engine read below already has its own guard, so this catches what those
        # cannot: the clock, the arithmetic, a container operation, and anything a future
        # edit adds between them. Guarding the clock alone would have fixed the instance
        # and left the next one to the next audit -- which is this cycle's whole subject.
        #
        # An escape lands on R-INCOMPLETE, which is the only honest answer: whatever went
        # wrong, the scan did not finish, so the Rule 0 CONF determination is UNKNOWN and both
        # kinds taint. It can never fall through to "the merit declares nothing".
        #
        # ▶ AND IT DISCLOSES WHAT IT SWALLOWED (round 2): *"the outer `except Exception`
        # does hide programming errors -- an internal AttributeError, or TypeError from an
        # invalid budget_s, now becomes an R-INCOMPLETE table. That is fail-closed for the
        # ceiling answer but masks defects that previously surfaced."* Correct, and the
        # net cannot simply be narrowed: this layer's contract is NEVER RAISES, and a
        # degraded .NET proxy throws AttributeError and TypeError too, so a type-based
        # exclusion would re-open the hole for the very faults it exists to catch.
        #
        # So the net STAYS and the masking becomes VISIBLE. ``internal_fault`` carries the
        # exception's repr into the disclosure, where a reader sees it beside the
        # incomplete verdict instead of inferring an engine problem from a silent taint.
        # Fail-closed for the ANSWER, loud for the CAUSE.
        table = CeilingTable({}, set(), set(KINDS), rows_scanned=0, boundary_rows=0,
                             budget_exhausted=False, complete=False)
        table.internal_fault = _safe_repr(exc)
        return table


def _same_reading(was, now_val):
    """Are two readings of one cell the SAME OBSERVATION? Type-tagged, not ``==``.

    ▶ ROUND 4: *"Python equality collapses classifier-distinct observations such as
    ``1.0`` and ``True``. Pass 1 can admit ``Target=1.0``; verification can see
    ``Target=True``; ``True != 1.0`` is false, so the old PRESENT publishes even though
    reclassification would take Rule 10."*

    Exactly right, and it is the ABSENT/UNREADABLE collapse wearing a different hat: two
    values the CLASSIFIER treats as different must not compare equal to the check that
    decides whether the classification is still valid. ``bool`` is the reachable case
    because it is an ``int`` subclass; the rule is stated over the TYPE generally rather
    than special-casing it, so the next subtype does not need a new patch.

    ``UNREAD`` compares by identity -- it is a singleton, and a stably-unreadable cell IS
    the same observation twice.

    ▶ AND SO DOES ``NaN``, WHICH IS THE OTHER WAY A CELL FAILS TO YIELD A USABLE NUMBER.
    Found by A-LIVE, and it is this cycle's own class for the sixth time: the identity arm
    was written for ``UNREAD`` and NOT for its sibling, **in the predicate whose entire
    subject is that two readings the classifier treats alike must not compare as
    different**. ``nan != nan``, so a BOUNDARY row carrying a stable NaN target or weight
    made ``_verify_snapshot`` report CHANGED on every read -- and the taint is
    whole-table, so ONE unrelated malformed row cost the design every ceiling it had,
    permanently and deterministically (measured: the clean surface-3 ceiling went
    ``source_unreadable`` beside it).

    It over-refuses rather than publishing a wrong ceiling, which is exactly the residual
    class auditor named and declined to chase -- and why 147 offline rows and
    five external rounds all missed it. Reachability, measured over the whole corpus:
    102,644 boundary rows, ZERO non-finite. Latent, not live. But the live engine DOES
    return NaN from these accessors (5 ``BLNK`` rows on the first design A-LIVE loaded),
    so it is the corpus's finite targets keeping it latent, not the engine's marshalling.
    """
    if was is UNREAD or now_val is UNREAD:
        return was is now_val
    if type(was) is not type(now_val):
        return False
    try:
        # The type check above already proved both sides are the same type, so one
        # ``isnan`` guard cannot admit a mixed pair. A hostile subclass that raises here
        # falls to the ``except`` and reads as CHANGED -- the fail-closed direction.
        if isinstance(was, float) and math.isnan(was) and math.isnan(now_val):
            return True
        return bool(was == now_val)
    except Exception:  # noqa: BLE001 - a comparison that cannot be made is not a match
        return False


def _verify_snapshot(mfe, snapshot, signatures, now, deadline):
    """Did the table hold STILL for the whole scan? -> a ``VERIFY_*`` verdict.

    Re-reads every row's ``TypeName`` through the SAME normalizer Pass 0 used and compares
    the whole vector. Normalized-to-normalized, deliberately: comparing a normalized token
    against a raw one would report a mismatch for a table that never moved.

    Returns ``(status, fault_text)`` -- never raises, never partially trusts. The status is
    ``VERIFY_CHANGED`` when the length or any element moved, ``VERIFY_EXPIRED`` when the
    budget ran out during the check, and ``VERIFY_FAULT`` (with the text) when a read or
    the clock threw. Every one of those means the same thing for the ANSWER: what was
    computed above describes a table that is not the one in front of us now.

    (This paragraph said ``Returns False`` until round 5 -- stale from before the status
    and the diagnostic text were split into separate channels.)
    """
    try:
        if int(mfe.NumberOfOperands) != len(snapshot):
            return (VERIFY_CHANGED, None)
    except Exception as exc:  # noqa: BLE001 - unverifiable IS unverified; fail closed
        return (VERIFY_FAULT, _safe_repr(exc))

    for i, recorded in enumerate(snapshot, start=1):
        try:
            if now() >= deadline:
                return (VERIFY_EXPIRED, None)
        except Exception as exc:  # noqa: BLE001 - a clock fault is not a stable table
            return (VERIFY_FAULT, _safe_repr(exc))
        # PER-ROW, because "this row is unreadable" is a STABLE OBSERVATION and must
        # compare equal to itself. A single try around the whole loop made an unreadable
        # row -- which Pass 0 records as ``UNREAD`` and handles under Rule 1 -- look like a
        # table that moved, so every scan containing one reported INCOMPLETE. The row
        # this broke (A-ROWDROP's) is the contract that one malformed row does not abort
        # the scan, and it went red the moment the verify pass landed.
        try:
            fresh = _merit_cells._base_token(mfe.GetOperandAt(i).TypeName)
        except Exception:  # noqa: BLE001 - still unreadable: the same observation
            fresh = UNREAD
        if not _same_reading(recorded, fresh):
            return (VERIFY_CHANGED, None)

    # ▶ AND THE ADMISSION-RELEVANT SIGNATURES, because a matching TYPE VECTOR is not a
    # matching TABLE (round 3, PART 3, and it is NOT the ABA case):
    #
    #   * two same-``TypeName`` rows REORDER between their Pass-1 reads, so the same row
    #     is read twice and the other is never read at all. The type vector is identical
    #     afterwards, and a real declaration silently publishes as ABSENT.
    #   * an already-read row's TARGET changes while its ``TypeName`` stays. The vector
    #     matches and the OLD limit publishes as PRESENT.
    #
    # Both are stable, repeatable mutations that a type-only check cannot see, so the
    # check covers everything ``classify_row`` consumed. Cost is one more Header walk over
    # the BOUNDARY rows only (measured 10.5 ms/row, 22 rows on the corpus merit), well
    # inside the 2.0 s backstop.
    #
    # ▶ WHAT THIS READER CLAIMS, EXACTLY, AND WHAT IT CANNOT (round 4, and it is a
    # CONTRACT NARROWING rather than a fix, because no amount of re-reading closes it).
    #
    # The claim is about the table AS SAMPLED. Two shapes escape it, and they are the same
    # shape seen twice:
    #
    #   * ABA -- any field that changes and changes back BETWEEN two samples reads
    #     identical;
    #   * POST-FINAL-SAMPLE -- anything that changes AFTER a row's last read. Round 4's
    #     instance: a non-boundary row matches during the type walk and then becomes
    #     ``CONF`` while the signatures are being checked. It carries no signature, so
    #     verification returns OK and an admission publishes although a ``CONF`` row is
    #     now present.
    #
    # BOTH are unclosable by ADDING READS, because every check has a last read and
    # anything can change after it. Only a transaction closes them and the engine offers
    # none, so each re-read NARROWS the window and none closes it. The honest statement is
    # therefore the scope of the claim, not a fifth pass: **this reader reports the
    # ceilings the merit declared ACROSS THE INTERVAL IT SAMPLED, and a mutation
    # concurrent with that interval may escape detection.**
    #
    # In the shipped deployment the interval has no concurrent writer at all -- dispatch
    # is serialized and single-threaded -- so the reachable vector is an external editor
    # (a GUI attached to the same engine) racing one call.
    for index, type_name, surf1, surf2, weight, target in signatures:
        try:
            if now() >= deadline:
                return (VERIFY_EXPIRED, None)
        except Exception as exc:  # noqa: BLE001
            return (VERIFY_FAULT, _safe_repr(exc))
        try:
            op = mfe.GetOperandAt(index)
            fresh_type = _merit_cells._base_token(op.TypeName)
        except Exception:  # noqa: BLE001 - readable in Pass 1, not now: the table moved
            return (VERIFY_CHANGED, None)
        if fresh_type != type_name:
            return (VERIFY_CHANGED, None)
        fresh_s1, fresh_s2 = _read_surface_params(op)
        for was, now_val in ((surf1, fresh_s1), (surf2, fresh_s2),
                             (weight, _read_scalar(op, "Weight")),
                             (target, _read_scalar(op, "Target"))):
            if not _same_reading(was, now_val):
                return (VERIFY_CHANGED, None)

    return (VERIFY_OK, None)


def _read_ceiling_table(system, n_surfaces, *, budget_s=None, clock=None):
    """The body of ``read_ceiling_table``. See its docstring; this may raise, it does not."""
    budget = ceiling_budget_s() if budget_s is None else budget_s
    now = time.perf_counter if clock is None else clock
    deadline = now() + budget

    admitted = {}
    unreadable = set()
    tainted = set()
    rows_scanned = 0
    budget_exhausted = False
    complete = True

    def _incomplete(internal_fault=None, **kw):
        """R-INCOMPLETE: taint BOTH kinds and say the scan did not finish."""
        table = CeilingTable(admitted, unreadable, set(KINDS),
                             rows_scanned=rows_scanned, boundary_rows=0,
                             complete=False, **kw)
        table.internal_fault = internal_fault
        return table

    try:
        mfe = system.MFE
        n_ops = int(mfe.NumberOfOperands)
    except Exception:  # noqa: BLE001 - the table could not even be sized -> no scan ran
        return _incomplete()

    # A NEGATIVE count is a CORRUPT READ, not an empty table (internal).
    # ``range(1, n + 1)`` on a negative n is empty, so without this the reader would walk
    # zero rows and report ``complete: True`` with no taint -- "I read the whole merit and
    # it declares nothing", asserted about a table it never looked at. That collapses
    # A-BUDGET-0's distinction in the one direction it forbids: a degraded read must not
    # be indistinguishable from a merit with no boundary rows. The GRIN sub-1
    # configuration count is the same guard for the same reason.
    #
    # ZERO is NOT corrupt and is deliberately admitted: an empty merit genuinely declares
    # nothing, which is ABSENT, which is the honest answer.
    if n_ops < 0:
        return _incomplete()

    # ---- Pass 0: TypeName only, every row, Rule 0 + Rule 1 + Rule 2 ------------------- #
    boundary = []
    #: The FULL per-row type vector, not just the boundary rows. See ``_verify_snapshot``.
    snapshot = []
    for i in range(1, n_ops + 1):
        if now() >= deadline:
            budget_exhausted = True
            complete = False
            break
        try:
            type_name = _merit_cells._base_token(mfe.GetOperandAt(i).TypeName)
        except Exception: # noqa: BLE001 - Rule 1: the kind is unknown -> neither kind safe
            type_name = UNREAD

        # ▶ ONE APPEND SITE, BEFORE ANY BRANCH, AND THE STRUCTURE IS THE FIX.
        #
        # Every row observed goes into the snapshot -- unreadable, CONF, boundary or
        # ordinary alike -- because the vector's whole job is to say what the table looked
        # like, and a row omitted from it makes the length disagree with
        # ``NumberOfOperands`` and turns a perfectly stable merit INCOMPLETE.
        #
        # THIS IS THE THIRD TIME THAT OMISSION HAPPENED, AND THE REASON IT IS NOW
        # STRUCTURAL. The first cut omitted the unreadable row. The fix for that appended
        # in the ``except`` branch -- with a comment explaining exactly why omitting it was
        # wrong -- and left the IDENTICAL omission in the ``CONF`` branch eleven lines
        # below, in the same function, in the same edit. Measured consequence: every
        # multi-config design in the corpus read ``complete: False``. A third append would
        # have invited a fourth branch to forget it, so there is now exactly ONE append and
        # no branch can reach a ``continue`` without passing it.
        rows_scanned += 1
        snapshot.append(type_name)

        if type_name is UNREAD:
            # Rule 1 -- the kind is unknown, so no surface of either kind can be exonerated.
            tainted.update(KINDS)
            continue
        if type_name == CONF_TYPE:
            # Rule 0. The scan CONTINUES rather than exiting early: Pass 0 is specified as a
            # walk over every row, and while an early exit would be observationally
            # identical here (both kinds are already tainted and a taint dominates), the
            # disclosed ``rows_scanned`` would stop meaning what it says.
            tainted.update(KINDS)
            continue
        if type_name in BOUNDARY_TYPES:
            boundary.append((i, type_name))
        # else: Rule 2 -- not a boundary row, contributes nothing to the ANSWER. Its type is
        # still in the snapshot, recorded above with every other row, and that is the
        # Finding: a row this pass dismissed as ``EFFL`` can be ``CONF`` by the
        # time Pass 1 runs, and Rule 0 is a WHOLE-TABLE question.

    if not complete:
        return _incomplete(budget_exhausted=budget_exhausted)

    # ---- Pass 1: the boundary rows only, Rule 3..Rule 14 -------------------------- #
    #: Per-row ``(index, type, surf1, surf2, weight, target)`` for every boundary row
    #: Pass 1 actually read. Verified afterwards -- see ``_verify_snapshot``.
    signatures = []
    for index, type_name in boundary:
        if now() >= deadline:
            budget_exhausted = True
            complete = False
            break
        try:
            op = mfe.GetOperandAt(index)
            # ▶ RE-READ THE TYPE, NEVER TRUST THE PASS-0 CACHE (a review, PART 3).
            #
            # Pass 1 fetches the row AGAIN, so the token Pass 0 recorded is a COPY of a
            # value that has since been re-read -- and the first cut passed that copy to
            # ``classify_row`` while the freshly-fetched row supplied every other field.
            # If the row changed in between, the kind and the numbers come from different
            # states of the table: an ``MXCG`` that became ``MXCA`` publishes an AIR
            # ceiling as a GLASS one, with a limit and a surface that are both perfectly
            # real. Nothing downstream can detect it, because every field is individually
            # valid.
            #
            # THIS IS THE CYCLE'S OWN DEFECT CLASS INSIDE ITS OWN FIX. The Pass-0 read
            # was normalized through ``_base_token`` two rounds ago precisely so a forged
            # type could not be trusted -- and the CONSUMER went on reading a cached copy
            # of it, so the normalization protected the first read and nothing else.
            #
            # The mismatch is routed through R-INCOMPLETE rather than tainting locally:
            # a table that changed under the scan means the Pass-0 answers -- including
            # the Rule 0 CONF determination, which is a WHOLE-TABLE question -- describe a
            # merit that no longer exists. "I did not finish reading a coherent table" is
            # exactly what R-INCOMPLETE says, and it already taints both kinds.
            fresh_type = _merit_cells._base_token(op.TypeName)
        except Exception: # noqa: BLE001 - the row vanished between passes -> Rule 1's shape
            tainted.update(KINDS)
            continue
        if fresh_type != type_name:
            complete = False
            break
        surf1, surf2 = _read_surface_params(op)
        weight = _read_scalar(op, "Weight")
        target = _read_scalar(op, "Target")
        # THE ADMISSION-RELEVANT SIGNATURE, recorded from the reads just performed rather
        # than re-read for the purpose. Everything ``classify_row`` consumes is in it, so
        # a change to ANY field the verdict depended on is visible to the verify pass.
        signatures.append((index, fresh_type, surf1, surf2, weight, target))
        outcome = classify_row(
            type_name, surf1, surf2, weight, target, n_surfaces,
        )
        if outcome.action == ACT_TAINT_BOTH:
            tainted.update(KINDS)
        elif outcome.action == ACT_TAINT_KIND:
            tainted.add(outcome.kind)
        elif outcome.action == ACT_UNREADABLE:
            unreadable.add((outcome.surface, outcome.kind))
        elif outcome.action == ACT_ADMIT:
            admitted.setdefault((outcome.surface, outcome.kind), set()).add(outcome.limit)
        # ACT_SKIP / ACT_NOTHING contribute nothing, which Rule 15 then reports as ABSENT.

    if not complete:
        return _incomplete(budget_exhausted=budget_exhausted)

    # ---- Pass 2: VERIFY THE WHOLE SNAPSHOT, not just the rows we used ----- #
    #
    # ▶ ROUND 2's FINDING, AND IT IS ROUND 1's FIX ONE LEVEL OUT. Round 1 re-read the
    # TypeName of every BOUNDARY row and compared the operand COUNT. Both were right and
    # both were too narrow, because Pass 1 iterates ``boundary`` only:
    #
    # * an ``EFFL`` row that becomes ``CONF`` after Pass 0 is never revisited, so Rule 0 --
    #     the WHOLE-TABLE gate -- silently fails to fire and a clean boundary row still
    #     publishes PRESENT;
    #   * an ``EFFL`` that becomes ``MXCG`` is not in ``boundary``, so a real declaration
    #     publishes as ABSENT;
    #   * and the count is blind to same-count REPLACEMENT or REORDERING, and collapses
    #     distinct corrupt raw values (``True`` and ``1``) through ``int()``.
    #
    # I fixed the rows the scan USED and left the rows it DISMISSED. That is the same
    # fix-at-one-site-not-its-sibling class fix was itself written for -- the
    # eighth instance in this cycle, and the second in code.
    #
    # So the check is the FULL per-row type vector, which subsumes all of it: a length
    # change catches insert/remove, an element change catches replacement, reordering and
    # the EFFL->CONF / EFFL->MXCG transitions alike. Cost is one more TypeName walk
    # (measured 107 ms of the reader's 227), well inside the 2.0 s backstop.
    #
    # WHAT IT STILL CANNOT CATCH, stated rather than implied: an ABA change -- a row that
    # changes and changes back between the two samples reads identical. No sampling scheme
    # closes that; only a transaction would, and the engine offers none.
    verdict, fault_text = _verify_snapshot(mfe, snapshot, signatures, now, deadline)
    if verdict != VERIFY_OK:
        # ▶ THE VERIFY PASS KEEPS ITS DIAGNOSTICS (round 3). The first cut returned a bare
        # bool, so a clock fault during verification was swallowed with no
        # ``internal_fault`` and an expiry during verification lost ``budget_exhausted`` --
        # the same "a degraded read reports as ordinary degradation" shape this module
        # exists to prevent, in the module's own reporting.
        return _incomplete(
            budget_exhausted=(verdict == VERIFY_EXPIRED),
            internal_fault=fault_text,
        )

    return CeilingTable(admitted, unreadable, tainted, rows_scanned=rows_scanned,
                        boundary_rows=len(boundary), budget_exhausted=False,
                        complete=True)


__all__ = [
    "CEILING_LOOKUP_PRESENT",
    "CEILING_LOOKUP_ABSENT",
    "CEILING_LOOKUP_SOURCE_UNREADABLE",
    "CEILING_LOOKUP_STATES",
    "KINDS",
    "BOUNDARY_TYPES",
    "CONF_TYPE",
    "SENTINEL_TARGET",
    "UNREAD",
    "RowOutcome",
    "CeilingTable",
    "ceiling_budget_s",
    "classify_row",
    "read_ceiling_table",
]
