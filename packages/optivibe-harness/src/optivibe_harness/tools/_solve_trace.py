"""tools/_solve_trace.py — the RELATIONSHIP tier for the traceable ray solves.

NOT dispatchable (no ``TOOL_SPECS``). It answers ONE question that ``surface_solve``'s
T1 cannot: a solve's TYPE read back correctly — did the QUANTITY IT NAMES actually land?
For a ``SurfacePickup`` that question is closed arithmetically inside ``surface_solve._t2``
(the source cell is readable and the law is a closed form). For every OTHER solve type it
was answered with the single word ``not_applicable``, which is RETIRED here: a
non-pickup now gets either a MEASURED paraxial-operand verdict or a NAMED reason why no
verdict exists.

THE DECISION RULE, and it is the whole design
---------------------------------------------
    GATE where the DISAGREEMENT population is MEASURED and separated from the agreement
    population, so the threshold sits in a measured gap. DISCLOSE where no disagreement
    population has ever been observed — there, a threshold's false-positive rate is not
    merely unmeasured, it is UNMEASURABLE from the data in hand.

    | oracle              | agreement    | disagreement        | separation | verdict  |
    | ChiefRayAngle       | n=20, 2.8e-9 | n=5,  best 2.545e-1 | 9.09e+07   | **GATE** |
    | MarginalRayHeight   | n=15, 4.0e-13| n=0 — never observed| undefined  | disclose |
    | ChiefRayHeight      | n=15, 8.9e-16| n=0 — never observed| undefined  | disclose |

An INJECTED fault is NOT an observed disagreement population: its separation would be a
property of the magnitude we chose to inject. Promotion of a height row to a gate needs an
OBSERVED disagreement, never a test fixture.

WHAT THE THREE ORACLES ARE, AND WHY EACH CELL OF THE TABLE
---------------------------------------------------------
* ``PARB`` / ``PARY``, never ``REAY``. The solves are PARAXIAL: ``REAY`` (the familiar
  real-ray operand) is off by 0.048 and 0.495 where ``PARY`` gives 1e-15. A future
  verifier reaching for the real-ray operand will be confidently wrong. ``PARA`` reads
  0.0 everywhere and is not broken — it is the X direction cosine and every ray here is
  meridional; ``PARB`` is the Y cosine.
* The heights are read at ``surface + 1`` — the height is realised in the space AFTER the
  solved thickness — and the angle at ``surface``.
* The angle law is ``PARB == u / hypot(1, u)``, settled by an EIGHT-target sweep: its two
  rivals (``sin(u)``, ``u``) are wrong by 400x and 600x at the wide end. The form was NOT
  decidable from the two points an earlier round had.
* ``MarginalRayAngle`` shares the cell, the operand and the law and is STILL not gateable:
  the same 2x2 (both families x both signs, five magnitudes) reads 7.5e-08 / 8.5e-08 for
  the chief and 6.3e-05 / 1.5e-04 for the marginal. Exactness tracks the TYPE, not the
  sign. It is in ``NO_ORACLE_REASON``, not in ``ORACLES``.

NEVER-RAISES IS A CORRECTNESS PROPERTY HERE, NOT A PREFERENCE. ``_transact`` treats
ANY post-invoke exception as a failure and RESTORES, so a read fault inside a DISCLOSURE
row would roll back a CORRECT write. Every fault inside ``relation_for`` therefore becomes
``traced_unreadable``. The ONE raise-vs-return decision lives in ``surface_solve._verify``,
and it consults ``gates`` only on ``traced_mismatch``.

THE FOUR NON-PICKUP OUTCOMES ARE FOUR DIFFERENT FACTS AND MUST NOT BE COLLAPSED:
ABSENT (``no_oracle``) != DECLINED (``oracle_declined``) != UNREADABLE
(``traced_unreadable``) != DISAGREED (``traced_mismatch``). Collapsing any pair lets a
knowledge gap masquerade as a measurement, or an engine fault masquerade as a knowledge
gap. An unreadable operand is NEVER ``no_oracle``; an out-of-domain target is NEVER
``no_oracle``.

NO HAND-ROLLED 9-ARG ``GetOperandValue`` EXISTS HERE (the silent-0 class).
The read goes through ``_measurement_common.read_operand_slots`` with a NAMED slot map,
and the sentinel predicate is that module's ``suspicious_sentinel``. The stop hint reads
the stop through ``_structural_common._classify_stop`` — never a fresh ``IsStop`` scan
(ONE shared reader, never a second).

EVERY READ IS TAKEN AT THE SYSTEM'S PRIMARY WAVELENGTH, RESOLVED AT RUNTIME,
and an unresolvable primary DECLINES rather than falling back. The operands are strongly
wavelength-dependent — ``PARB`` spreads 1.59e-3 across one design's three waves against the
gated row's ``abs_tol`` of 1e-7 — and the solve TRACKS THE PRIMARY, so a hard-coded index
would make the gated row raise and ROLL BACK A CORRECT AUTHORING on any design whose primary
is not 1 (a d-line primary is the ordinary case). Absence of a readable primary ships as
absence of a verification, never as a verification against a guessed ray.

SCOPE, stated so no envelope key is read as more than it is: one engine, one version
(OpticStudio 2025 R1), one design, and two primary indices (1 and 2). A ``traced_match``
proves the quantity the solve NAMES reads back at the value the caller stated, on the ray we
chose, at the primary wavelength, at the MOMENT OF AUTHORING. It does not establish that the
solve is correct for the design, and it is not a standing invariant — a later upstream edit
re-drives the cell and nothing re-checks it.
"""
import math

from . import _measurement_common as _mc
from . import _solve_cells as _sc
from . import _structural_common as _struct
# THE TOLERANCE IMPORT IS THE REASON THIS DIRECTION IS FIXED. The design requires the two height
# rows to use ``surface_solve``'s T2 tolerances BY IDENTITY (a copied ``1e-9`` literal is a
# second authority that drifts), so this module must import ``surface_solve`` at MODULE
# level to have the objects when ``ORACLES`` is built. ``surface_solve`` therefore imports
# THIS module inside its three consuming functions — the house pattern for exactly this
# cycle (``_solve_cells.read_solve_type``, ``_structural_common._add_bound_operand``), and
# the only shape under which neither module ever sees a partially-initialised sibling.
from . import surface_solve as _ss

#: The ray-operand positional-slot map: ``{9-arg position: Header}``. FROZEN by probe.
#:
#: It is the AUTHORITY, not a decoration: ``_slots`` places each named value AT the
#: position this map gives it, so permuting two entries genuinely moves the values into
#: different positional args and a coordinate-sensitive reader returns a different
#: quantity. (``read_operand_slots`` consumes only the VALUE half of each pair, so a map
#: consulted for its NAMES alone would make the permutation inert — which is exactly the
#: silent-0 class this shape exists to keep visible.)
RAY_SLOTS = {2: "Surf", 3: "Wave", 4: "Hx", 5: "Hy", 6: "Px", 7: "Py"}

#: The FROZEN eight relation states. The first three are the pickup family
#: (``surface_solve._t2``, byte-unchanged); the last five are this module's.
#:
#: ``not_applicable`` is RETIRED as a produced value. Keeping it for ``no_oracle``
#: alone would narrow its meaning SILENTLY for the three traced pairs. NOTE the token is
#: live in SEVEN unrelated modules (``catalog/metrics``, ``catalog/registry``,
#: ``loop/criteria``, ``loop/promotion_gate``, ``loop/referee``, ``analysis_measure``,
#: ``workspace``), so a repo-wide absence test would redden against all of them: scope any
#: such test to ``surface_solve.py`` + this module.
RELATION_STATES = (
    "verified", "mismatch", "not_computable",
    "traced_match", "traced_mismatch", "traced_unreadable",
    "oracle_declined", "no_oracle",
)

#: The three DERIVED fact keys, in ``relation_semantics``'s return order.
#:
#: They live HERE, and the emit site merges them by zipping this tuple onto that
#: function's return value — so the three names never appear as literals at an emit site
#: and the facts have exactly ONE producer.
DERIVED_KEYS = ("consequence_established", "disagreement_gated", "proof_method")

#: state -> ``(consequence_established, disagreement_gated, proof_method)``.
#:
#: The middle slot is ``None`` for the two states whose answer is the ROW's ``gates`` flag;
#: ``relation_semantics`` substitutes it. ``None`` rather than a placeholder ``False``
#: deliberately: if the substitution were ever deleted the key would emit ``null``, which
#: is loudly wrong, instead of a plausible ``false``.
#:
#: ``not_computable`` reads ``disagreement_gated: False`` because ``surface_solve``'s
#: pickup arm RETURNS that dict — it does not raise, so it does not roll back. An earlier
#: revision read ``True`` there: a policy proxy sold as an outcome fact.
_SEMANTICS = {
    "verified": (True, True, "source_cell_arithmetic"),
    "mismatch": (False, True, "source_cell_arithmetic"),
    "not_computable": (False, False, "source_cell_arithmetic"),
    "traced_match": (True, None, "paraxial_operand"),
    "traced_mismatch": (False, None, "paraxial_operand"),
    "traced_unreadable": (False, False, "paraxial_operand"),
    "oracle_declined": (False, False, "paraxial_operand"),
    "no_oracle": (False, False, None),
}

#: The states whose ``disagreement_gated`` is the ROW's flag rather than a constant.
_GATES_FROM_ROW = frozenset({"traced_match", "traced_mismatch"})

#: canonical -> the MEASURED reason no oracle exists. EXACTLY FOUR, and the distinction
#: this table carries is load-bearing: these four were ATTEMPTED and REJECTED; everything
#: else was never attempted. Collapsing the two would report a measured dead end and an
#: unexplored one as the same fact.
#:
#: THIS TABLE AND ITS EXACT-SET PIN ARE ONE CHANGE-UNIT: a row and its pin move
#: together or not at all.
NO_ORACLE_REASON = {
    "MarginalRayAngle": (
        "a candidate law was measured and REJECTED: relative residual 6.283e-05 to "
        "1.456e-04 in both sign directions, systematic and unexplained."),
    "ChiefRayNormal": (
        "measured and RETRACTED: RAID -> 0 held on 1 of 3 surfaces, and that surface was "
        "the degenerate flat air dummy where the solve produced a radius of -1.3e-15."),
    "MarginalRayNormal": (
        "RAID fell 12.071 -> 0.185, not to zero."),
    "FNumber": (
        "WFNO reads an IDENTICAL value at surfaces 5, 6 and 7; it is a whole-system "
        "scalar that does not localise to the solve's surface, so no per-surface oracle "
        "over it can exist."),
}

#: What every other type gets. NOT the same claim as a ``NO_ORACLE_REASON`` row.
_NEVER_ATTEMPTED = "no oracle has been ATTEMPTED for this type."

#: The measurement envelope, served on every traced verdict. ``%s`` is the RESOLVED
#: primary wavelength index the read actually used — never a literal.
_SCOPE = (
    "measured on OpticStudio 2025 R1, one design; this read was taken at the system's "
    "PRIMARY wavelength, index %s. A traced_match proves the quantity the solve NAMES "
    "reads back at the value you stated, on this ray, at that wavelength, at the MOMENT "
    "OF AUTHORING; it does not establish that the solve is correct for your design, and "
    "nothing re-checks it after a later upstream edit.")

#: The shared half of the two ``MarginalRayHeight`` declines, reproduced VERBATIM in
#: both: a decline is only useful if it is WALKABLE.
_PUPIL_ZONE_REMEDY = (
    "this harness cannot distinguish an inherited engine default from an "
    "explicitly-written zone — a written zone changes the relationship by ~0.5 "
    "(measured). clear_solve then set_solve to get a proven relation.")

#: The EIGHT ray solve types — the SCOPE of the stop-rule diagnosis, and nothing else.
#:
#: The five geometry types (EdgeThickness, Position, CenterOfCurvature, Compensator,
#: ElementPower) authored on EVERY interior surface with the stop at 5, so they can never
#: see this rule. ``SurfacePickup`` is deliberately ABSENT, which is what keeps the CB
#: seam's refusal message byte-unchanged.
RAY_SOLVE_TYPES = frozenset({
    "MarginalRayHeight", "ChiefRayHeight", "MarginalRayAngle", "ChiefRayAngle",
    "MarginalRayNormal", "ChiefRayNormal", "FNumber", "Aplanatic",
})

#: The POST-HOC stop diagnosis. It fires only on a write the engine has ALREADY refused,
#: so it cannot be wrong about whether the write was allowed — and the caution that kept it
#: post-hoc is now RETRO-PROVEN: every stop-rule sweep iterated ``range(..., n-1)``,
#: so ``stop <= surface <= N-2`` was a limit of the MEASUREMENT, and the engine has since
#: been measured ACCEPTING a ``MarginalRayHeight`` solve AT THE IMAGE SURFACE. A pre-gate —
#: which was considered and rejected — would today refuse a write the
#: engine accepts. ONLY THE LOWER BOUND is asserted, because only it was measured as a rule.
#:
#: The stale sentence this replaced ("the image surface was never attempted") was a SERVED
#: STRING, which no mutation, live gate or audit can catch — only a re-reading of the
#: measurement it cites.
_STOP_RULE = (
    " MEASURED: a %s solve is accepted only at or after the STOP surface, which is "
    "surface %s on this system; you asked for surface %s. Author it at surface %s or "
    "later, or move the stop with set_stop_surface. (Measured on OpticStudio 2025 R1 by "
    "moving the stop to 3, 5 and 7 and watching the accepted set follow it exactly, for "
    "all eight ray solve types. The IMAGE surface accepts a MarginalRayHeight solve, so "
    "the upper limit is the engine's, not a rule this harness knows.)")


def law_chief_ray_angle(u):
    """``u / math.hypot(1.0, u)`` — the measured ``ChiefRayAngle`` law.

    ``hypot`` AND NOT ``u / math.sqrt(1 + u*u)``, which returns **0.0** at
    ``u = 1.4e154`` (``u*u`` overflows) where this returns **1.0**. The two are identical
    across the whole measured range; ``hypot`` is chosen because it is stable where the
    other silently is not.

    THE CLAIM IS NARROW: the domain precondition makes the overflow region UNREACHABLE
    through the door, so this is defense-in-depth, not a live-reachable fix.
    """
    return u / math.hypot(1.0, u)


def _law_stated_target(target):
    """The height law: ``PARY == Height``. The operand reads the stated target itself."""
    return target


#: NO ``wave`` COLUMN, and its ABSENCE is the point. The reads are taken at
#: the system's PRIMARY wavelength, RESOLVED AT RUNTIME — a per-row literal is exactly the
#: hard-coded ``1`` this rule retires. Measured: the paraxial operands are strongly
#: wavelength-dependent (``PARB`` spreads 1.59e-3 across the Cooke's three waves against
#: the gated row's ``abs_tol`` of 1e-7, ~16000x) and the solve TRACKS THE PRIMARY (moving
#: the primary to 2 moved the exact reading with it). On a design whose primary is not
#: index 1 — the ORDINARY case, a d-line primary — a hard-coded wave would make the GATED
#: row see a residual ~5.0e-3 against 1e-7, raise, and ROLL BACK A CORRECT AUTHORING.
ORACLES = {
    "RadiusCell.ChiefRayAngle": {
        "operand": "PARB",
        # read at the solve's OWN surface.
        "offset": 0,
        # the chief ray: full field, on axis in the pupil.
        "ray": (0.0, 1.0, 0.0, 0.0),
        "field": "Angle",
        "law": law_chief_ray_angle,
        "law_text": "PARB == Angle / hypot(1, Angle)",
        # rel 1e-6: worst CORRECT relative residual 8.535e-08, ~12x margin.
        # abs 1e-7: it SEPARATES the two measured populations — 35.73x above the worst
        # correct (2.799e-09) and 2.545e+06x below the best wrong (2.545e-01). It is NOT a
        # detection floor: an error inside 3e-09..1e-07 is certified traced_match.
        "rel_tol": 1e-6,
        "abs_tol": 1e-7,
        "gates": True,
        # every ChiefRayAngle target ever authored: +/-0.02 .. +/-0.3, 0.05 .. 0.2, 0.1
        # and 10.0. Angle = 0.0 (image-space telecentricity) is OUTSIDE it and is declined
        # BY RULE, not by threshold.
        "domain": (0.02, 10.0),
        "domain_abs": True,
    },
    "ThicknessCell.MarginalRayHeight": {
        "operand": "PARY",
        # the height is realised in the space AFTER the solved thickness.
        "offset": 1,
        # the FULL-PUPIL marginal ray.
        "ray": (0.0, 0.0, 0.0, 1.0),
        "field": "Height",
        "law": _law_stated_target,
        "law_text": "PARY == Height",
        # BY IDENTITY, never a copied literal: these are ``surface_solve``'s own T2
        # tolerances. Zero-end margin 2955x (3.384e-13 against abs 1e-9);
        # relative-end margin 13613x (7.346e-14 at Height = 1e6 against rel 1e-9).
        # The MIXED form is required at both ends: absolute-only passes 2 of 4 at
        # 1e3..1e6, and relative is UNDEFINED at Height = 0.
        "rel_tol": _ss._T2_REL_TOL,
        "abs_tol": _ss._T2_ABS_TOL,
        "gates": False,
        # INCLUDES zero: Height = 0.0 is the canonical focus solve and the residual is a
        # constant ~3.3e-13 ABSOLUTE floor across five orders of magnitude of target.
        "domain": (0.0, 1e6),
        "domain_abs": False,
    },
    "ThicknessCell.ChiefRayHeight": {
        "operand": "PARY",
        "offset": 1,
        # the CHIEF ray, not the marginal one — the two rows differ in exactly this cell.
        "ray": (0.0, 1.0, 0.0, 0.0),
        "field": "Height",
        "law": _law_stated_target,
        "law_text": "PARY == Height",
        "rel_tol": _ss._T2_REL_TOL,
        "abs_tol": _ss._T2_ABS_TOL,
        "gates": False,
        "domain": (0.0, 10.0),
        "domain_abs": False,
    },
}


def _key(cell_token, canonical):
    """``"<Column>Cell.<Type>"`` — the SAME key shape ``_solve_cells._MEASURED`` uses.

    Consumed rather than re-derived so the two tables cannot drift into disagreeing about
    what a pair is called; ``ORACLES`` is a strict subset of ``_MEASURED``'s keys and
    a test pins it.
    """
    return "%s.%s" % (_sc._column_of(cell_token), canonical)


def has_oracle(cell_token, canonical):
    """Is a relation oracle CATALOGUED for this ``(cell, type)`` pair?"""
    return _key(cell_token, canonical) in ORACLES


def gates_for(cell_token, canonical):
    """Does this row GATE — i.e. would a DISAGREEMENT raise and roll back?

    A plain per-row Boolean. It is NEVER target-conditional: a target the measurement does
    not cover is a PRECONDITION question (``oracle_declined``), not a reason to soften a
    threshold, and a per-row flag that varied with the target would be a threshold wearing
    a Boolean's clothes.

    An uncatalogued pair answers ``False`` — nothing ran, so nothing can have disagreed.
    """
    spec = ORACLES.get(_key(cell_token, canonical))
    return bool(spec and spec["gates"])


def relation_semantics(state, gates):
    """``(consequence_established, disagreement_gated, proof_method)``. FAIL-CLOSED.

    ``disagreement_gated`` IS A PROPERTY OF THE CHECK, and its definition ships on the
    key: *a DISAGREEMENT detected by this check would have raised and rolled back.* It is
    NOT a report of what happened.

    An unknown state RAISES rather than defaulting: every state reaching here comes from a
    frozen producer, so an unrecognised one means a producer and this table have diverged —
    and the safe answer to that is a loud failure, not a fabricated triple that would
    certify a consequence nobody established.
    """
    if state not in _SEMANTICS:
        raise ValueError(
            "unknown relation state %r; the frozen vocabulary is %s — a state and its "
            "semantics row move together or not at all"
            % (state, list(RELATION_STATES)))
    established, gated, method = _SEMANTICS[state]
    if state in _GATES_FROM_ROW:
        gated = bool(gates)
    return established, gated, method


def _no_oracle_reason(cell_token, canonical):
    """The ABSENT reason: measured-and-rejected, or never attempted. Never the same word."""
    return ("no relation oracle is catalogued for a %s solve on the '%s' cell, so its "
            "TYPE is proven and its CONSEQUENCE is not: %s"
            % (canonical, cell_token,
               NO_ORACLE_REASON.get(canonical, _NEVER_ATTEMPTED)))


def _decline_reason(spec, canonical, resolved, prior_type):
    """The DECLINE reason, or ``None`` to run the oracle. PURE — it reads NO engine state.

    Evaluated BEFORE any read, so a declined call performs ZERO OPERAND reads and every
    reason names BOTH the constraint and the supplied value.

    EVERY CALLER-SUPPLIED VALUE IS RENDERED THROUGH ``_ss._safe_repr``, and in THIS
    function that is not belt-and-braces. The docstring claims purity and never-raising,
    and both arms below format an object the CALLER chose: ``PupilZone`` is arbitrary by
    construction, and ``target`` on the FINITENESS arm has JUST FAILED ``_is_finite_number``, so
    it is by definition an arbitrary object. The DOMAIN arm formats a ``target`` that
    PASSED that predicate — which is not the same as safe: ``_is_finite_number`` uses
    ``isinstance``, so a ``float`` SUBCLASS overriding ``__repr__`` passes it (measured)
    and reaches the ``%r`` here. A raise inside a "pure" function that is called to build
    a never-raise return value is what rolls back a correct write.

    A GENERIC NEAR-ZERO RULE IS FORBIDDEN. ``|expected| <= abs_tol => cannot verify`` would
    decline ``Height = 0.0`` — the canonical focus solve — on the very row where the
    evidence is strongest (2955x margin). The trigger is the measured DOMAIN, per row.
    """
    if canonical == "MarginalRayHeight" and "PupilZone" in resolved:
        return ("the MarginalRayHeight oracle is DECLINED because you wrote a PupilZone "
                "(%s): %s Nothing was read."
                % (_ss._safe_repr(resolved["PupilZone"]), _PUPIL_ZONE_REMEDY))
    if canonical == "MarginalRayHeight" and prior_type == canonical:
        return ("the MarginalRayHeight oracle is DECLINED because this cell ALREADY "
                "carried a MarginalRayHeight solve, so an omitted PupilZone INHERITS the "
                "live value rather than defaulting: %s Nothing was read."
                % (_PUPIL_ZONE_REMEDY,))
    target = resolved.get(spec["field"])
    if not _ss._is_finite_number(target):
        return ("no relation was checked: the %r field was not supplied as a finite "
                "number (got %s), so the solve INHERITS a live value this check cannot "
                "see. State it explicitly to get a proven relation. Nothing was read."
                % (spec["field"], _ss._safe_repr(target)))
    lo, hi = spec["domain"]
    if not lo <= (abs(target) if spec["domain_abs"] else target) <= hi:
        # ``lo``/``hi`` come from the FROZEN table and are ordinary floats, so they keep
        # their plain ``%r``. ``target`` does not: it passed ``_is_finite_number``, which
        # is ``isinstance``-based and therefore admits a ``float`` SUBCLASS with a hostile
        # ``__repr__`` (measured — it also passes ``math.isfinite`` and ``math.isclose``).
        return ("the %s oracle is DECLINED: it is measured ONLY for %s in [%r, %r] and "
                "you asked for %s. Outside that range neither the law nor its tolerance "
                "has been measured, so no verdict is available and nothing was read."
                % (canonical,
                   ("|%s|" if spec["domain_abs"] else "%s") % (spec["field"],),
                   lo, hi, _ss._safe_repr(target)))
    return None


def _primary_wave(system):
    """``(index, fault)`` — the system's PRIMARY wavelength index, or WHY not. Never raises.

    ``SystemData.Wavelengths.Primary`` DOES NOT EXIST — it raises ``AttributeError``
    (measured). The primary is the wavelength whose ``IsPrimary`` flag is set, and this
    requires EXACTLY ONE of them: "the first True" would let a system with two flags — or
    with none — silently verify against a ray nobody chose.

    IT NEVER FALLS BACK TO INDEX 1. A fallback is the shape this rule exists to delete:
    on a design whose primary is 2 it reads the wrong ray, and on the GATED row that means
    rolling back a CORRECT authoring. An unresolvable primary is a DECLINE at the caller —
    absence of a verification, not a verification against a guess.
    """
    try:
        waves = system.SystemData.Wavelengths
        primary = [w for w in range(1, int(waves.NumberOfWavelengths) + 1)
                   if bool(waves.GetWavelength(w).IsPrimary)]
    except Exception as exc:  # noqa: BLE001 — an unreadable wavelength set is a DECLINE
        return None, ("the system's wavelength set could not be read (%s)"
                      % (_ss._safe_repr(exc),))
    if len(primary) != 1:
        return None, ("%d of the system's wavelengths report IsPrimary, not exactly one "
                      "(flagged: %s)" % (len(primary), primary))
    return primary[0], None


def _slots(spec, read_at, wave):
    """The NAMED 9-arg slot map for this read: ``{position: (Header, value)}``.

    Each value is placed AT the position ``RAY_SLOTS`` names for it, so the frozen map is
    what decides where the numbers land.
    """
    hx, hy, px, py = spec["ray"]
    named = {"Surf": read_at, "Wave": wave,
             "Hx": hx, "Hy": hy, "Px": px, "Py": py}
    return {pos: (name, named[name]) for pos, name in RAY_SLOTS.items()}


def _oracle_block(spec, read_at, target, wave):
    """The served evidence block, with the two MEASURED slots left ``null`` until read.

    They are filled in by ``_trace`` and stay ``null`` on ``traced_unreadable`` — a reading
    that could not be used is reported as absent and DESCRIBED in ``reason``, never
    coerced into the field a caller reads as the measurement (and a sentinel or non-finite
    reading is not wire-safe in the first place).
    """
    hx, hy, px, py = spec["ray"]
    return {"state": None, "operand": spec["operand"], "read_at_surface": read_at,
            "ray": {"hx": hx, "hy": hy, "px": px, "py": py, "wave": wave},
            "law": spec["law_text"], "target": target, "traced": None, "residual": None,
            "rel_tol": spec["rel_tol"], "abs_tol": spec["abs_tol"],
            "scope": _SCOPE % (wave,)}


def _trace(system, spec, read_at, target, wave):
    """Run the catalogued oracle and grade it.

    ITS ONE ARM NEVER RAISES ON AN ENGINE FAULT; THE NEVER-RAISE CONTRACT ITSELF IS
    ENFORCED AT THE CALL-SITE NET, NOT HERE — and the distinction is load-bearing enough
    that the headline was corrected rather than leaving a word doing work it cannot do.
    FOUR spans of this function sit outside its ``try``, and that is now a
    DESIGN rather than a leftover: ``_oracle_block``, the LAW, the SLOT BUILDER, and the
    tail's ``abs(expected - out["traced"])``. A defective tier (a typo'd frozen-table key)
    or hostile-subclass ARITHMETIC — a finite ``float`` subclass whose ``__sub__`` raises,
    which survives ``_is_finite_number`` by construction — therefore escapes THIS function
    and is attributed by the net as a HARNESS defect, which is what it is. The rule that
    puts them there is stated at the ``try`` itself: an internal ``except`` covers exactly
    what its sentence can truthfully describe.

    THE CONTRACT IS PINNED WHERE IT IS ENFORCED. ``surface_solve._t2``'s non-pickup arm
    wraps this call in the outer net, and that net's own battery is the pin — a test
    drives a fault into the span BEFORE this ``try`` and
    asserts the write survives. A reader looking for "who guarantees ``relation_for`` does
    not roll back a correct authoring" should look there, never at this docstring.

    EVERY VALUE THIS FUNCTION RENDERS IS ENGINE- OR CALLER-DERIVED, so every one goes
    through ``_ss._safe_repr``. ``raw`` arrives from the engine; ``expected`` is the law
    applied to the CALLER's target, so a ``float`` subclass with a hostile ``__repr__``
    survives ``_is_finite_number`` and propagates into it; and ``residual`` is arithmetic
    over that same object. ``out["traced"]`` is exempt from the hazard by construction —
    ``float(raw)`` NORMALISES a subclass away to a builtin — but is rendered the same way
    so no reader has to re-derive which of three adjacent slots is the safe one. The
    frozen-table values (``operand``, ``field``, the two tolerances) keep plain formatting.

    THE GUARD IS NOT THE WHOLE STORY, and pretending otherwise is how the seventh site
    gets written. What the per-site ``_safe_repr`` calls buy is the QUALITY of the degraded
    REASON — a specific sentence naming the operand and the fault instead of the net's
    generic one — and NOT the never-raise contract, which the paragraph above locates at
    the net. Both claims are true and they are different claims; conflating them is what
    let an earlier revision's per-site rows describe their own redness incorrectly.
    """
    out = _oracle_block(spec, read_at, target, wave)
    # THE ``try`` BELOW COVERS THE ENGINE CALL AND NOTHING ELSE, and the
    # two statements hoisted above it are what makes its ONE sentence TRUE.
    #
    # THE GENERALIZABLE RULE, and it is what terminates this class: AN INTERNAL ``except``
    # MAY COVER EXACTLY THE OPERATIONS WHOSE FAULTS IT CAN TRUTHFULLY DESCRIBE; EVERYTHING
    # ELSE FALLS THROUGH TO THE BOUNDARY. Attribution then holds BY ROUTING rather than by
    # enumeration — a future harness computation added outside this ``try`` is attributed
    # correctly with no sweep to remember.
    #
    # WHAT IT COST TO GET WRONG: the handler renders *"the oracle RAN and its reading did
    # not arrive: reading PARB at surface 6 raised ..."*, and for a fault in ``spec["law"]``
    # or in ``_slots`` that is THREE FALSE CLAIMS — the oracle did not run, there was no
    # reading, and the named operand was never reached. A triager is sent to the engine and
    # to the design for a defect in this harness's own arithmetic. The governing rule's own
    # worked example (a ``KeyError`` from a typo'd frozen-table key) lands in ``_slots``,
    # which was an ARGUMENT to the read and therefore INSIDE this ``try``: the net's third
    # flavour was written for that case and could never see it.
    #
    # THE NARROWING IS ATTRIBUTION-ONLY AND THAT WAS MEASURED, not reasoned. Under both
    # shapes a law/slots fault ends as ``traced_unreadable`` with the write STANDING —
    # before, via this arm; now, via ``surface_solve._t2``'s net, which is the only caller
    # and has no inner net of its own. Same state, same rollback disposition, and a
    # net-produced ``traced_unreadable`` never gates. ``BaseException`` behaviour is
    # likewise unchanged: arm and net both catch ``Exception`` only.
    #
    # A THIRD FLAVOUR *HERE* WAS REJECTED. Two renderers of "the tier itself failed" is a
    # second authority for the very text that was made a requirement, and it drifts on the
    # first edit.
    expected = spec["law"](target)
    slots = _slots(spec, read_at, wave)
    try:
        raw, suspicious = _mc.read_operand_slots(system, spec["operand"], slots)
    except Exception as exc:  # noqa: BLE001 — a read fault must NEVER roll back the write
        out["state"] = "traced_unreadable"
        out["reason"] = ("the oracle RAN and its reading did not arrive: reading %s at "
                         "surface %s raised %s. The solve was authored and its TYPE "
                         "proven; its consequence is UNVERIFIED, and nothing was rolled "
                         "back." % (spec["operand"], read_at, _ss._safe_repr(exc)))
        return out
    if suspicious or not _ss._is_finite_number(raw) or not math.isfinite(expected):
        out["state"] = "traced_unreadable"
        out["reason"] = ("the oracle RAN and its reading was UNUSABLE: %s at surface %s "
                         "read %s (a sentinel, a non-finite value or a non-number) "
                         "against an expected %s. The solve was authored and its TYPE "
                         "proven; its consequence is UNVERIFIED, and nothing was rolled "
                         "back." % (spec["operand"], read_at, _ss._safe_repr(raw),
                                    _ss._safe_repr(expected)))
        return out
    out["traced"] = float(raw)
    out["residual"] = abs(expected - out["traced"])
    agreed = math.isclose(expected, out["traced"],
                          rel_tol=spec["rel_tol"], abs_tol=spec["abs_tol"])
    out["state"] = "traced_match" if agreed else "traced_mismatch"
    if not agreed:
        out["reason"] = ("%s at surface %s reads %s where the %s you stated predicts %s "
                         "(absolute residual %s, against rel_tol %r / abs_tol %r)."
                         % (spec["operand"], read_at, _ss._safe_repr(out["traced"]),
                            spec["field"], _ss._safe_repr(expected),
                            _ss._safe_repr(out["residual"]),
                            spec["rel_tol"], spec["abs_tol"]))
    return out


def relation_for(system, lde, surface, cell_token, canonical, resolved, prior_type):
    """The SOLE producer of a non-pickup ``relation``. **NEVER RAISES ON A WELL-FORMED
    CALL**, and the absolute guarantee is enforced ONE LAYER OUT.

    THE QUALIFICATION IS DELIBERATE AND IS NOT A WEAKENING. This contract read a bare
    "NEVER RAISES", and a review pass was right that the bare form is false:
    a non-mapping ``resolved`` dies in ``_decline_reason``'s ``"PupilZone" in resolved``
    and a non-numeric ``surface`` dies in ``surface + spec["offset"]``, both BEFORE any of
    the three guarded reads. Neither is reachable through ``set_solve`` — ``_prelude``
    returns ``int(surface)`` and ``_resolve_fields`` returns a dict — but the CONTRACT was
    written without its precondition, and a contract that is false for reasons it does not
    name is how the next reader builds on it wrongly.

    So the absolute never-raise property now lives at ``surface_solve._t2``'s non-pickup
    arm, WHERE THE ROLLBACK CONSEQUENCE ACTUALLY IS: that call sits inside ``_transact``'s
    window, which treats any post-invoke exception as a failure and RESTORES, so a fault
    while GRADING would roll back a CORRECT authoring. Putting a second net here as well
    would make two authorities for one invariant and give the outer one nothing to catch —
    the failure it exists for (a defect in this module's own logic) is exactly the one an
    inner net would swallow first, and swallow SILENTLY, since it would report the tier's
    own crash in the tier's own engine-fault vocabulary.

    THIS FUNCTION STILL ENUMERATES EVERY FAULT IT CAN NAME — the three guarded reads and
    the ``_safe_repr`` renders — because the outer net's reason can only say "the harness
    crashed", while these say WHICH read failed and what it returned. The net is the floor,
    not the plan.

    Returns a dict whose ``state`` is one of the five non-pickup tokens. NO OPERAND IS EVER
    READ unless every precondition passed: a decline and an absence both cost zero MFE
    calls, which is what makes ``oracle_declined`` safe to return from inside the
    transaction window.

    THE FOUR PRECONDITIONS RUN CHEAPEST-FIRST — the pure ones (domain, pupil zone), then
    the LDE read that bounds the read LOCATION, then the wavelength set. A call the pure
    rules already exclude never pays for an engine read.
    """
    spec = ORACLES.get(_key(cell_token, canonical))
    if spec is None:
        return {"state": "no_oracle",
                "reason": _no_oracle_reason(cell_token, canonical)}
    declined = _decline_reason(spec, canonical, resolved, prior_type)
    if declined is not None:
        return {"state": "oracle_declined", "reason": declined}
    # THE READ LOCATION IS BOUNDED, AND AN OUT-OF-RANGE READ IS THE SILENT ONE.
    # Measured: ``PARY`` at the image surface, at ``N`` and at ``N+2`` all
    # return the IDENTICAL finite number — the engine SILENTLY CLAMPS to the last surface
    # and the reading is not sentinel-flagged, so ``suspicious_sentinel`` cannot see it.
    # And the path is REACHABLE, not theoretical: the engine ACCEPTS a MarginalRayHeight
    # solve at the IMAGE surface, whose height row then reads at ``surface + 1`` = ``N``.
    # The worst case is a coincidental agreement reported as ``traced_match`` — certifying
    # a relationship measured at the WRONG surface.
    #
    # It is ``oracle_declined`` and NOT ``traced_unreadable`` because it is known BEFORE
    # reading: the read would SUCCEED and LIE.
    #
    # THE COUNT READ HAS TWO EXITS AND THEY ARE NOT THE SAME FACT. An
    # earlier revision declined on BOTH, and because the count-UNREADABLE exit fires on
    # EVERY row regardless of offset it silently DISABLED THE GATE: a ``ChiefRayAngle``
    # that would have raised and rolled back instead returned ``ok: true`` /
    # ``oracle_declined`` with the bad solve COMMITTED and — because
    # ``traced_relation_warning`` is set only on ``traced_mismatch`` — no warning key at
    # all. FAIL-CLOSED AGAINST A ROLLBACK IS FAIL-OPEN AGAINST THE GATE. Two sentences
    # that licensed that defect are retired with it: the earlier *"always in range
    # — there is NO rollback hazard from this"* and this comment's own former *"this
    # never fires on it"*. Both are MEASURED FALSE for the unreadable exit.
    #
    # FOR OFFSET 0 THE LOCATION PROOF IS ``_prelude``'S BOUND; THE RE-READ IS CONSULTED
    # ONLY FOR THE CHANGED CASE WHEN READABLE. ``_prelude`` bounded ``surface`` against a
    # count it read SUCCESSFULLY moments earlier in this same call, so at offset 0 — where
    # the read location IS ``surface`` — that proof is already in hand and an unreadable
    # re-read cannot unprove it. Discarding a fact already established is what the earlier
    # shape did. This is a NARROWING of an over-broad precondition to its evidenced scope.
    #
    # IT IS NOT "UNREADABLE -> ASSUME OK", and the distinction is the whole fix: a count
    # that READS and no longer covers the surface still DECLINES at offset 0, because that
    # is a CHANGED system rather than an unmeasurable one. The two exits are separated on
    # the read, never merged. At offset 1 the count is the ONLY proof that exists for
    # ``surface + 1``, so BOTH exits decline there, exactly as before.
    #
    # RESIDUAL, recorded as the assumption it is: if an unreadable count is the first
    # symptom of a dying remoting channel, the following operand read most likely THROWS
    # (measured throws, not garbage) -> ``traced_unreadable`` -> the write stands. A
    # dying channel returning GARBAGE is unmeasured and would gate.
    read_at = surface + spec["offset"]
    unbounded = None
    try:
        last = int(lde.NumberOfSurfaces) - 1
    except Exception as exc:  # noqa: BLE001 — an unprovable location is a DECLINE
        # THE RENDER SITE THAT MOVED. It previously lived INSIDE the decline string
        # a few lines down; the restructure hoisted it here, which is why the ``%r`` sweep
        # was re-derived against the POST-restructure tree rather than against the audit's
        # line numbers — enumerating first would have covered a line that no longer exists.
        last, unbounded = None, _ss._safe_repr(exc)
    # KEYED ON ``offset``, AND THE KEY IS THE FINDING RATHER THAN A STYLE CHOICE.
    # ``offset`` IS the location-proof provenance: offset 0 means the read location
    # IS ``surface``, which ``_prelude`` already bounded against a successfully-read count
    # in this same call; offset 1 means ``surface + 1``, for which the re-read is the ONLY
    # proof that exists. ``spec["gates"]`` is CONSEQUENCE POLICY and answers a different
    # question — the two agree on all three shipped rows BY ACCIDENT OF THE TABLE, and
    # swapping the key for ``spec["gates"] is False`` measures INERT across the whole
    # suite. A future gating row at offset 1 would then PROCEED on an unbounded read,
    # re-opening the silent engine clamp that the read-location bound closed.
    #
    # A PER-ROW ``location_proven_by_prelude`` FIELD WAS REJECTED: it is a second
    # encoding of ``offset == 0`` that must agree with the arithmetic it summarises. The
    # guard is the suite's two SYNTHETIC rows, one per direction.
    if unbounded is not None and spec["offset"]:
        return {"state": "oracle_declined",
                "reason": "the %s oracle is DECLINED: its reading would be taken at "
                          "surface %s — the space AFTER the solved thickness, which no "
                          "earlier check has bounded — and the surface count could not be "
                          "read (%s), so that location could not be shown to be inside "
                          "the system. An out-of-range read does not fail — it silently "
                          "returns a number about a DIFFERENT surface — so it is refused "
                          "before reading. The operand was NEVER read."
                          % (canonical, read_at, unbounded)}
    if last is not None and read_at > last:
        return {"state": "oracle_declined",
                "reason": "the %s oracle is DECLINED: its reading is taken at surface %s "
                          "(the space AFTER the solved thickness) and the last surface on "
                          "this system is %s. An out-of-range read does NOT fail: the "
                          "engine SILENTLY CLAMPS to the last surface and returns a "
                          "finite, un-flagged number about a DIFFERENT surface (measured), "
                          "so this harness refuses to verify against it. The solve was "
                          "authored and its TYPE proven; the operand was NEVER read."
                          % (canonical, read_at, last)}
    # THE WAVELENGTH PRECONDITION IS LAST because the two PURE rules above cost nothing,
    # so a call they already exclude never pays for an engine read at all.
    #
    # IT IS NOT "the only one that touches the engine", which is what this comment said
    # until the invariant was corrected: the read-location bound immediately above
    # reads ``lde.NumberOfSurfaces``, and a height solve at the image surface DECLINES
    # having already paid for it. The invariant that actually holds — and the only one a
    # test may assert — is that THE ORACLE OPERAND is never read on a declined call.
    #
    # It DECLINES rather than defaulting: an unresolvable primary is a fact about the
    # system, and the honest report of it is "no verdict", never a verdict taken at a
    # guessed index (which on the gated row would roll back a correct authoring).
    wave, wave_fault = _primary_wave(system)
    if wave is None:
        return {"state": "oracle_declined",
                "reason": "the %s oracle is DECLINED: its reading must be taken at the "
                          "system's PRIMARY wavelength and that index could not be "
                          "established — %s. The operand was NEVER read; this harness "
                          "will not verify against a guessed wavelength."
                          % (canonical, wave_fault)}
    # THE PROCEED-PATH DISCLOSURE. A tier that proceeds on a DEGRADED read
    # must SAY SO — a silent version would contradict this module's own thesis, and the
    # gate this branch keeps alive is the one place where proceeding matters most. The key
    # is emitted on exactly ONE of the four cells (offset 0, count unreadable) and names
    # BOTH facts a reader needs: that the re-read failed, and what the location proof
    # actually was. Merged rather than assigned so the whole tail stays one statement.
    return dict(
        _trace(system, spec, read_at, resolved[spec["field"]], wave),
        **({} if unbounded is None else {"read_location_proof": (
            "the surface count could not be RE-read (%s), so this reading's location was "
            "not re-proven here. It rests on the bound taken against a successfully-read "
            "count BEFORE this solve was authored: this row reads at the solve's OWN "
            "surface (offset 0), which that bound already placed inside the system. A "
            "count that READS and no longer covers the surface still declines."
            % (unbounded,))}))


def stop_rule_hint(lde, canonical, surface):
    """The post-hoc stop-rule sentence, or ``""``. NEVER raises.

    ``""`` unless ``canonical`` is one of the EIGHT ray solve types AND the stop resolves to
    an index AND ``surface < stop``. The stop is read through the shared
    ``_classify_stop`` — never a second ``IsStop`` scan — and BOTH of its
    ``(None, ...)`` flavours (``no_stop`` and ``indeterminate``) omit the sentence: a
    diagnosis nobody can substantiate is worse than none.

    It never decides anything. The refusal it decorates has already happened, and its
    family, raise site and transaction shape are unchanged.
    """
    if canonical not in RAY_SOLVE_TYPES:
        return ""
    try:
        stop_idx = _struct._classify_stop(lde)[0]
    except Exception:  # noqa: BLE001 — a decoration NEVER displaces the diagnosis
        return ""
    if stop_idx is None or not isinstance(surface, int) or surface >= stop_idx:
        return ""
    return _STOP_RULE % (canonical, stop_idx, surface, stop_idx)
