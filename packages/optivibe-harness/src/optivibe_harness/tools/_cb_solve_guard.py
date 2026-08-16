"""tools/_cb_solve_guard.py — the SOLVE-LOSS guards for the coordinate-break doors.

THREE served tools structurally mutate rows that can carry an authored solve — the two
retype doors and ``place_element``'s native wrap — and this module
owns the one question all three have to answer: *does this operation lose one?* It holds
TWO answers, because the two operations are genuinely different and the measurements say
so — a retype-in-place (``precheck`` / ``disclosure``) and a renumbering INSERT
(``census`` / ``losses`` / ``pickup_sources``).

The two answers are shaped differently for a MEASURED reason. A retype happens on ONE
KNOWN ROW, so the retype half is keyed on that surface and can name each cell. An insert
RENUMBERS every row downstream of it, non-uniformly, so the insert half is keyed on
nothing positional at all — see ``census``. Merging them into one "solve guard" reader
would force one keying on both, and it is the wrong keying for whichever it was not
written for.

BOTH coordinate-break doors retype a surface IN PLACE, and a retype was measured to
DISCARD an authored solve on the row while the envelope reported clean success. This
release makes an authored solve a first-class thing a user creates, so a served
tool that can irreversibly erase one and say ``ok:true`` is a data-destruction path, not
an incomplete disclosure.

THE MEASUREMENT THAT DECIDED THE SHAPE (a live CB-retype probe, 0 own-spawned
leftovers) — and it decided AGAINST preservation, which had to be
probed rather than assumed:

  * Through the SHIPPED ``add_coordinate_break``, on ONE row, in ONE call:
    ``radius`` SurfacePickup -> **Fixed** (DESTROYED), ``conic`` SurfacePickup ->
    **Fixed** (DESTROYED), ``thickness`` SurfacePickup -> **SurfacePickup** (SURVIVED),
    ``semi_diameter`` unchanged at its ``Automatic`` default. Envelope: ``ok: True``,
    five keys, no disclosure. **A retype is not one behaviour even WITHIN a single row**
    — the third time here that a retype claim has turned out not to be one thing.
  * REPLAY onto the retyped CoordinateBreak row was measured IMPOSSIBLE for exactly the
    two cells that get destroyed: the cells still fetch, ``GetAvailableSolveTypes``
    still OFFERS ``SurfacePickup`` on ``radius``, ``SetSolveData`` returns without
    complaint — and the cell reads back ``Fixed``. A capture/replay "preservation" would
    therefore SILENTLY NO-OP on radius and conic and report success, which is the
    precise silent-wrong class this cycle exists to close. **Preservation is NOT built,
    and this is why.**

SO THE GUARD REFUSES ON *ANY* NON-DEFAULT SOLVE, NOT ON THE TWO MEASURED CELLS.
"Which cells survive" is a prediction over 39 solve types x 5 cells from three
measurements of one type on one engine — the proxy/target gap this round exists to stop.
UNKNOWN resolves toward ALARM: the presence of an authored solve is the trigger,
and an UNREADABLE cell triggers it too. A ``thickness`` pickup that would in fact have
survived is a FALSE refusal — LOUD, zero-mutation, and one explicit opt-in away — which
is the recoverable direction.

AND THE DISCLOSURE IS MEASURED, NOT PREDICTED. On the opt-in path the row is
re-inventoried AFTER the retype and the two lists are a DIFF, so the envelope never says
a solve was destroyed without having looked. That is what lets the guard refuse on the
conservative predicate without the disclosure inheriting its conservatism.

WHY ITS OWN MODULE. ``cb_surface.py`` breached its statement ceiling carrying this (290 ->
364 ast.stmt against a 334 ceiling). The ceiling exists to force exactly this question,
and the answer here is structural rather than a re-baseline: the guard is a distinct
concern (inventory, decide, diff) with two callers, and one shared decision function is
also what stops the two CB doors drifting apart again — which is the defect four
consecutive rounds have re-created.
"""
from ._analysis_common import error_envelope

#: The refusal family. A retype that would discard a solve is NOT ``surface_write`` (no
#: write failed) and NOT ``cb_param`` (no parameter is wrong) — it is a refusal to
#: perform a destructive operation, and an agent branching on the remedy needs to tell it
#: from both.
CB_SOLVE_LOSS = "cb_solve_loss"

#: The in-flight ledger label for a retype, ONE definition for BOTH doors. The shipped
#: defect was that the two CB retypes had different ledger treatment AT ALL; a shared
#: literal is what stops them drifting apart again, and it is a SERVED string.
CHANGETYPE_ATTEMPTED = "changetype(surface=%s) — outcome UNKNOWN if this call threw"


def precheck(system, lde, surface, replace_solve, tool, partial_state_fields,
             audit=None):
    """Would this retype discard an authored solve? Shared by BOTH doors.

    Returns ``{"refuse": False, "before": <block or None>}`` to proceed — ``before`` is
    ``None`` exactly when the row carried nothing non-default, which is the ordinary
    ``insert_surface`` -> ``add_coordinate_break`` workflow and costs it ZERO extra
    engine reads and ZERO envelope keys (measured: every freshly inserted Standard
    surface, 0..6, emits ``{}``). Otherwise ``{"refuse": True, "envelope": {...}}``.

    ONE decision function for two doors on purpose: the shipped defect is that a fix
    landed on one CB path and not its sibling, four rounds running. A shared predicate
    cannot drift; two copies of this rule would be the fifth instance of the same class.

    ``partial_state_fields`` is passed IN rather than imported, because it belongs to the
    caller's ledger pair and this module deliberately owns no ledger of its own.

    ``audit`` is the caller's failure-path carrier, and it is filled HERE
    rather than at the call site for the reason ``precheck`` is shared at all: the moment
    a retype is PERMITTED to proceed over a solve-bearing row is the moment the failure
    arms acquire something to report, and those are the same moment. Filling it at each
    door would be two copies of that correspondence, free to drift — which is the defect
    four rounds here kept re-creating. A door that passes no carrier simply
    gets none; a future third door gets it by calling this function.

    NEVER raises — ``emit_solves_block`` is itself never-raise and fails CLOSED on an
    unusable index (all five cells ``solves_unreadable``), which this guard then reads as
    at-risk rather than as clean.
    """
    from . import _solve_cells as _sc

    before = _sc.emit_solves_block(system, lde, surface)
    at_risk = sorted(before.get("solves") or {})
    unreadable = sorted(before.get("solves_unreadable") or [])
    if not at_risk and not unreadable:
        return {"refuse": False, "before": None}
    if replace_solve:
        if audit is not None:
            audit.update({"surface": surface, "before": before})
        return {"refuse": False, "before": before}

    named = ", ".join(
        "%s (%s)" % (tok, ((before.get("solves") or {}).get(tok) or {}).get("type"))
        for tok in at_risk
    ) or "none readable"
    return {"refuse": True, "envelope": error_envelope(
        tool, CB_SOLVE_LOSS,
        "surface %s carries an authored solve that this retype may DISCARD, and the "
        "loss is not recoverable by this tool: %s%s. A coordinate-break retype was "
        "measured to destroy a radius/conic SurfacePickup while leaving a thickness one "
        "intact, and a solve cannot be replayed onto the retyped row (the write is "
        "accepted and the cell reads back Fixed), so nothing here can put it back. "
        "REFUSING with ZERO mutation. Read the row with read_surface(%s) first; then "
        "either re-point the solve elsewhere, or pass replace_solve=true to proceed "
        "deliberately — that path reports exactly which solves were lost and which "
        "survived, MEASURED after the retype."
        % (surface, named,
           (" ; UNREADABLE (treated as at risk): " + ", ".join(unreadable))
           if unreadable else "",
           surface),
        surface=surface,
        solves_at_risk=at_risk,
        solves_unreadable=unreadable,
        **partial_state_fields([], attempted=[]),
    )}


def _type_after(system, lde, surface, token):
    """The cell's live solve type after the retype, or ``None`` if it could not be read.

    A DIRECT per-cell read, deliberately NOT a second ``emit_solves_block``. The block
    SUPPRESSES a cell that reads its default, so a solve RESET to ``Fixed`` — which is
    the commonest outcome of a destructive retype, and the one this whole disclosure is
    about — would come back absent and be reported as ``type_after: null``, i.e. as
    "could not be measured". That is the ABSENT-vs-UNREADABLE collapse appearing
    inside the fix for a defect of that very shape; the guard's own test caught it, which is
    why the read is per-cell here rather than another block diff.
    """
    from . import _solve_cells as _sc
    try:
        return _sc.read_solve_type(_sc.solve_cell(system, lde, surface, token))
    except Exception:  # noqa: BLE001 — an unreadable cell is DISCLOSED as null
        return None


def disclosure(system, lde, surface, before):
    """The MEASURED post-retype diff. ``{}`` when nothing was at risk. NEVER raises.

    ``replaced_solves`` and ``preserved_solves`` are read from the row AFTER the retype,
    so neither is a prediction — which is what lets the refusal above be conservative
    without the report inheriting that conservatism. A cell that reads back UNREADABLE is
    filed under ``replaced_solves`` with ``type_after: null`` — UNKNOWN toward alarm,
    and the null is what distinguishes "measured gone" from "could not be
    measured", the two things this cycle keeps having to hold apart.

    A CELL UNREADABLE *BEFORE* IS MEASURED AFTER TOO. An earlier shape iterated only
    ``before["solves"]`` — the cells that READ — and emitted the unreadable ones as a bare
    name list, so the served promise ("reports exactly which solves were lost and which
    survived, MEASURED after the retype") was true for the readable half and false for
    precisely the half the refusal treats as most at risk. Those cells now get the same
    post-read, filed with ``type_before: null``: we still cannot say what was lost, but we
    can say what is there NOW, and saying nothing was the worse of the two. They are kept
    in a SEPARATE list rather than folded into ``replaced_solves``, because "measured to
    have changed" and "we never knew what it was" are different claims and the whole point
    of this module is not to collapse them.
    """
    if not before:
        return {}

    replaced, preserved = [], []
    for token, entry in sorted((before.get("solves") or {}).items()):
        was = (entry or {}).get("type")
        now = _type_after(system, lde, surface, token)
        if now is not None and now == was:
            preserved.append(token)
        else:
            replaced.append({"cell": token, "type_before": was, "type_after": now})
    out = {"solve_loss_audited": True}
    if replaced:
        out["replaced_solves"] = replaced
    if preserved:
        out["preserved_solves"] = preserved
    unreadable_before = sorted(before.get("solves_unreadable") or [])
    if unreadable_before:
        out["solves_unreadable_before"] = [
            {"cell": token, "type_before": None,
             "type_after": _type_after(system, lde, surface, token)}
            for token in unreadable_before
        ]
    return out


def failure_disclosure(session, audit):
    """The SAME measured diff, on a FAILURE path. ``{}`` when nothing was at risk.

    The second half. ``disclosure`` was spliced ONLY into the success return, so an
    opted-in retype that destroyed a solve and THEN failed carried no measured diff at
    all — the case in which an agent most needs to know what the editor is now holding.
    The refusal text promises the report unconditionally on the opt-in path; this is what
    makes that true.

    It is deliberately the SAME function underneath: a second "what did the retype
    do" reader is exactly the drift this module was split out to prevent. ``audit`` is the
    small carrier the impl fills in at the moment it decides to proceed; an empty carrier
    means the retype arm was never reached, which is the ordinary refusal case.

    NEVER raises: it runs inside an ``except`` arm that is already building an envelope
    for a failure, and a disclosure must never replace the diagnosis it decorates.
    """
    try:
        if not audit or not audit.get("before"):
            return {}
        system = session.system
        return disclosure(system, system.LDE, audit.get("surface"), audit.get("before"))
    except Exception:  # noqa: BLE001 — a decoration NEVER displaces the failure
        return {}


def abort_finding(session, audit, tool, exc, committed, attempted, recovery):
    """The partial-state finding for an ABORT, *carrying the measured diff*.

    WHY IT LIVES HERE AND NOT NEXT TO ITS ONE CALLER. ``cb_surface.py`` sat at 333
    ``ast.stmt`` against its 334 ceiling, so this fix breached it — and the ceiling
    exists to force exactly this question rather than to be re-baselined by whoever meets
    it. The answer is the same one that created this module: the measured retype diff is
    THIS module's concern, and the abort path is simply its third consumer, alongside the
    success return (``disclosure``) and the ordinary failure arms (``failure_disclosure``).
    Building the finding here keeps the third consumer reading the SAME measurement
    and costs the door module nothing.

    NEVER raises. It is called from an ``except BaseException`` arm whose entire contract
    is that the original abort travels UNCHANGED, so every failure here degrades to
    a finding WITHOUT the diff rather than to no finding at all — losing the measurement
    is bad, losing the ledger too would be worse, and displacing the abort would be worst.

    Returns the ``CbPartialStateError`` to attach, or ``None`` if even the ledger text
    could not be built.

    THE PAYLOAD IS ABSENT, NOT EMPTY, when nothing was measured. A consumer has to be able
    to tell "no retype ever happened" from "a retype happened and lost nothing"; an empty
    dict collapses those two claims, and keeping them apart is what this module is for.
    """
    from ..errors import CbPartialStateError

    try:
        lost = failure_disclosure(session, audit)
    except BaseException:  # noqa: BLE001 — a decoration NEVER displaces the abort
        lost = {}
    try:
        err = CbPartialStateError(
            "%s was interrupted (%r) with coordinate-break sub-steps already entered on "
            "the engine: committed=%r (read back as done), attempted=%r (outcome "
            "UNKNOWN).%s %s" % (
                tool, exc, list(committed), list(attempted),
                (" MEASURED solve diff: %r." % (dict(lost),)) if lost else "", recovery),
            field="cb_abort", intended=None, actual=None, surface=None)
        if lost:
            err.solve_loss = dict(lost)
        return err
    except BaseException:  # noqa: BLE001 — as above
        return None


# --------------------------------------------------------------------------- #
# The RENUMBERING-INSERT half. A DIFFERENT question, measured.
# --------------------------------------------------------------------------- #
def census(system, lde):
    """A whole-system, INDEX-FREE inventory of authored solves. NEVER raises.

    ``({(cell_token, solve_type): count}, unreadable_cell_count)``.

    WHY INDEX-FREE, and it is the whole design. ``place_element`` wraps the engine's
    native ``RunTool_TiltDecenterElements``, which INSERTS surfaces and RENUMBERS
    everything downstream — measured, a 5-surface system becomes 8 and a row at index 2
    lands at 3 while a row at 4 lands at 7. A before/after comparison keyed on SURFACE
    INDEX would therefore report a loss on every single call, including the correct ones:
    the guard would be pure noise and would be turned off, which is worse than not
    having it. Keying on ``(token, type)`` counts is immune to the renumber by
    construction, and no index map has to be maintained against a native tool whose
    allocation rules are the composer's most re-measured fact.

    WHAT IT CAN AND CANNOT DECIDE, stated rather than implied. It answers "did an
    authored solve of this kind go missing?" — a DECREASE is real destruction. It does
    NOT answer "is each surviving solve still on the right row", because two rows of the
    same kind are interchangeable to a multiset. That second claim needs an index map,
    the index map is exactly what the renumber makes unreliable, and a guard that
    over-claims is the defect five rounds here have been spent prosecuting. The
    ``Surface`` REFERENCE a pickup names is checked separately (``losses`` below), which
    is the part that was measured to be at risk and is not decidable from a count.

    A CELL THAT STOPS BEING READABLE IS ALREADY COVERED, and this was got wrong once and
    caught by a fake, which is the honest place to record it. The first cut carried a
    global unreadable COUNT and flagged any increase — but this operation INSERTS rows, so
    that count rises on every correct call simply because there are more of them, and the
    guard rolled back every placement on a fixture whose rows expose no solve cells. The
    real signal needs no separate arm: a cell that was READABLE before contributes a
    ``(token, type)`` count, so if it becomes unreadable that count FALLS and the loss arm
    fires. A cell ALREADY unreadable before the wrap is unknown in BOTH directions, and
    flagging it would refuse forever on no measurement at all.

    What DOES stay fail-closed is the census failing outright: an unreadable
    ``NumberOfSurfaces`` returns the ``-1`` sentinel, and ``losses`` reads that as a
    finding, because an audit that could not run must not be reported as one that passed.
    """
    from . import _solve_cells as _sc

    counts, unreadable = {}, 0
    try:
        n = int(lde.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — an unreadable system is maximally unknown
        return {}, -1
    for surface in range(n):
        block = _sc.emit_solves_block(system, lde, surface)
        for token, entry in ((block or {}).get("solves") or {}).items():
            key = (token, (entry or {}).get("type"))
            counts[key] = counts.get(key, 0) + 1
        unreadable += len((block or {}).get("solves_unreadable") or [])
    return counts, unreadable


def losses(before, after):
    """What the census says went missing. ``[]`` when nothing did. NEVER raises.

    Returns a list of ``{"cell", "type", "before", "after"}`` for every ``(token, type)``
    whose count FELL. Counts that ROSE are ignored on purpose: the native generator
    authors its own solves (measured — a ``Position`` solve on the back-up spacer and a
    scale ``-1`` ``SurfacePickup`` on the return CB), and treating its machinery as a
    finding would make the guard fire on every correct call.

    The ONLY unreadable finding is the ``-1`` sentinel — the census could not run at all.
    A rise in the per-cell unreadable count is NOT a finding: this operation inserts rows,
    so it rises on every correct call, and a readable->unreadable transition already shows
    up as a fallen count. See ``census`` for the version of this that was wrong.
    """
    try:
        counts_before, unreadable_before = before
        counts_after, unreadable_after = after
    except Exception:  # noqa: BLE001 — a malformed census is maximally unknown
        return [{"cell": None, "type": None, "before": None, "after": None}]
    out = [
        {"cell": token, "type": kind, "before": n, "after": counts_after.get(key, 0)}
        for key, n in sorted(counts_before.items(), key=lambda kv: (str(kv[0][0]),
                                                                   str(kv[0][1])))
        for token, kind in (key,)
        if counts_after.get(key, 0) < n
    ]
    if unreadable_before < 0 or unreadable_after < 0:
        out.append({"cell": None, "type": "<census-unavailable>",
                    "before": unreadable_before, "after": unreadable_after})
    return out


def pickup_sources(system, lde):
    """Per cell token, how many pickups have a READABLE source. NEVER raises.

    ``{(token, "readable"|"unreadable"): count}``.

    THE SECOND ARM, and its scope was CUT BY THE LIVE GATE rather than by argument — the
    correction is worth more than the arm. A pickup names its source by SURFACE NUMBER,
    so the obvious check is "the set of ``(token, source)`` relationships is conserved".
    That was the first cut, and it is WRONG for exactly the reason ``census`` is
    index-free: **the source NUMBER is itself renumbered by the insert.** The probe's own
    data says so — a pickup at surface 2 naming source **1** becomes surface 3 naming
    source **2** — so a source-number multiset changes on every CORRECT placement. Offline
    every row passed, because the fake did not model the re-pointing; the live gate
    refused two correct placements on the first run. The census was made index-free and
    its companion arm was left index-BOUND, one function apart.

    SO THIS DECIDES A NARROWER THING, and the narrowing is the honest half: **no pickup
    lost a readable source.** A pickup whose reference is destroyed or becomes unreadable
    while the TYPE still reads ``SurfacePickup`` is invisible to the type census and is
    caught here. A pickup RE-POINTED to a different, readable row is NOT a finding — and
    cannot be one without an index map, which is precisely what the renumber makes
    unreliable. That residual mode ("re-pointed to the WRONG row") is UNGUARDED and said
    so in ``_audit_solve_survival``; naming it beats a check that fires on every correct
    call and gets deleted.

    MEASURED by a live probe: the native
    ``RunTool_TiltDecenterElements`` re-points correctly on every arm probed.
    """
    from . import _solve_cells as _sc

    out = {}
    try:
        n = int(lde.NumberOfSurfaces)
    except Exception:  # noqa: BLE001
        return out
    for surface in range(n):
        block = _sc.emit_solves_block(system, lde, surface)
        for token, entry in ((block or {}).get("solves") or {}).items():
            if (entry or {}).get("type") != "SurfacePickup":
                continue
            state = "unreadable"
            try:
                cell = _sc.solve_cell(system, lde, surface, token)
                data = cell.GetSolveData()
                view = _sc.unwrap_or_none(data) or data
                # The ``int()`` is the READ, and its raising is the whole signal. Written
                # as a plain statement rather than as a condition: ``int(x) is not None``
                # is a tautology, and a guard whose test can never be false is the shape
                # this repo spends rounds removing.
                int(view.Surface)
                state = "readable"
            except Exception:  # noqa: BLE001 — an unreadable source keeps the default
                pass
            out[(token, state)] = out.get((token, state), 0) + 1
    return out
