"""tools/_solve_refs.py — which live solves REFERENCE a row that is about to be removed.

**OWNS:** which live solves ON SURVIVING ROWS reference surface ``at``, decided
PRE-mutation against a named row, plus a post-mutation confirm of exactly those rows,
for ONE removal.

**DOES NOT OWN:** retype survival on one known row (→ ``_cb_solve_guard.precheck`` /
``_cb_solve_guard.disclosure``); the index-free renumber census for a native insert
(→ ``_cb_solve_guard.census`` / ``losses`` / ``pickup_sources``). Three keyings, three
readers, shared primitives — never a duplicated predicate. That boundary is the
anti-drift measure this module was placed apart to keep: co-location was never the
load-bearing part, and ``_cb_solve_guard``'s own docstring warns against one keying
serving a question it was not written for.

WHY THIS EXISTS. Two blocking probes measured the renumber claim on BOTH cell
domains — the five geometry cells and the coordinate-break Par table —
and found that a raw ``InsertNewSurfaceAt`` / ``RemoveSurfaceAt`` leaves a
``SurfacePickup``'s reference and its driven value on the ORIGINAL SOURCE ROW. What the
probes DID find is the hazard this module reports: removing a row that a live pickup
names as its SOURCE destroys the solve — measured in the live probe captures for a
``thickness`` pickup and for a coordinate-break ``Par1``/``Par3`` pickup — while
``remove_surface`` returned ``{"ok": true, "result": {"at": 2, "count": 7}}`` and
disclosed nothing.

TWO CLAIMS THAT MUST NEVER BE MERGED IN ONE SENTENCE:

* the REFERENCE FACT — *these solves REFERENCE this row*. True regardless of what the
  removal does to them; the referent ceases to exist, so the relationship as authored
  cannot survive unchanged. This is what the verdict token ``"affected"`` claims.
* the CONSEQUENCE — *the solve was DELETED*. Measured for ``SurfacePickup`` only, and
  thereafter measured PER CALL by ``confirm_after``. This is what an entry's
  ``consequence`` field claims, and it never carries a prediction under a
  measured-sounding name.

NEVER-RAISE IS THIS MODULE'S OWN OBLIGATION, NOT ITS CALLEES'. ``_sc.solve_cell``,
``_cb.is_coordinate_break``, ``_cb._cb_cell`` and ``_sc.assert_wire_safe`` all RAISE, so
each public entry point carries its own ``except Exception`` — and ``Exception``, NEVER
``BaseException``: a ``KeyboardInterrupt`` must travel unchanged (the substrate's rule at
``_sc.unwrap_or_none``).

READ-ONLY. Nothing here calls ``SetSolveData``, ``MakeSolveVariable``, ``ChangeType`` or
any other writer, and a write spy proves it. It reads the CURRENT CONFIGURATION ONLY —
inherited from every cell read in this family (a known limit).

------------------------------------------------------------------------------------
WHY THIS MODULE IS AS LARGE AS IT IS
------------------------------------------------------------------------------------
The measurement is **258** ``ast.stmt``, and the size is deliberate rather than
incidental. The table below is EXCLUSIVE (every function in exactly one bucket) and
EXHAUSTIVE (the buckets sum to the module total):

* **124 — the scan and its readers.** ``_scan_cells`` 33, ``cb_par_refs`` 20,
  ``scan_removal`` 17, ``_hit`` 11, ``_ref_outcome`` 9, ``geometry_refs`` 7,
  ``_verdict`` 6, ``_par_fields`` 6, ``_geometry_fields`` 5, ``_field`` 4, ``_cause`` 4,
  ``_gap`` 2. The bulk is the fault taxonomy, enumerated line by line.

  THE NUMBER OF DISTINCT GAP REASONS IS NOT WRITTEN HERE, AND THAT IS THE THIRD ANSWER TO
  A QUESTION THAT GOT THE FIRST TWO WRONG. The original wording ("five … and five" = 10)
  was already incomplete by four when it was written, and went arithmetically FALSE the
  moment one reason moved off the fault channel. The replacement asserted "**measured by
  AST**: 13" — which was a HAND COUNT wearing the word "measured", off by one, in the
  paragraph explaining why hand counts rot. Both were caught by a later review pass, not
  by a reader. So the number now lives in a test that re-derives it from the AST on every
  run; a stale literal cannot survive there.

  WHAT THE EXTRACTION COVERS, since the two wrong answers disagreed about the BOUNDARY and
  not only the total: the ``reason`` argument of every ``_gap()`` call plus the two field
  readers that return one indirectly. ``pickup column unreadable`` is deliberately OUTSIDE
  it — it is a DEGRADATION recorded on a hit entry, not a gap, and a taxonomy that mixes
  the two is the ABSENT-vs-UNREADABLE merge one level up. "par table not audited" is
  outside it too: it left this taxonomy when the coverage channel was added and travels on
  the third return slot as a COVERAGE record. Every distinct reason costs an ``if`` plus an
  ``append`` plus a ``continue``. Collapsing them is the ABSENT-vs-UNREADABLE merge the
  taxonomy exists to prevent, so they were not collapsed.
* **42 — the wire contract** (the per-entry shape, the three-state partition, the
  per-value wire rules): ``_block`` 16, ``disclosure`` 13, ``_entry`` 7,
  ``_wire_num`` 2, ``_plain`` 2, ``_established`` 2.
* **29 — the confirm** (both halves, ``type_after`` AND the re-read ``ref_after``, plus
  the ORDERED TOTAL four-row consequence partition):
  ``_confirm_one`` 12, ``confirm_after`` 11, ``_consequence`` 6.
* **45 — the refusal prose** and its ordered three-tier ``(table, solve_type)`` routing:
  ``refusal_message`` 31, ``_remedy`` 10, ``_replay`` 4.
* **18 — module-level**: the docstring, four imports, the three frozen vocabularies and
  the nine constants the wire and the prose share.

124 + 42 + 29 + 45 + 18 = **258**.

THE LAST THREE CHANGES MOVED IT 246 -> 250 -> 255 -> 258, AND EACH WAS REVIEWED BEFORE IT
WAS MADE: (+4) the policy channel; (+5) the refusal channel; (+3) the positive allow-list.
The first two are recorded below with what they bought; the third is recorded at
``_UNAUDITED_PAR_TYPES``.

THE POLICY CHANNEL (+4). The non-CB/non-Standard branch reported a POLICY exclusion
through the FAULT channel, so one asphere row made ``none_affected`` unreachable and
``refuse_on_solve_refs`` refuse every removal on an advertised design class. The +4 is the
third return slot (``scan_removal``'s accumulator, **+1**) and its wire key (``_block``,
**+3**); the arms' five widened returns and the record's reuse of ``_gap`` cost **0**, and
the prose is free. The alternatives were refused on CORRECTNESS: a fourth ``VERDICTS``
token cannot exist (the tuple unpack at the vocabulary is a 3-way), and suppressing the
record — the shape the ``_PAR_DEFAULTS`` sibling used — would certify an unaudited table
clean on a premise that is UNMEASURED for these surface types.

THE REFUSAL CHANNEL (+5). ``refusal_message`` never read the new channel, so a design
carrying BOTH a hit and an unaudited table produced a refusal naming neither — and a
refused caller has no wire block to fall back on. That is the class this module had
already closed once (*"IT CARRIES THE HITS AND THE UNKNOWNS"*) reappearing one channel
over, and the sibling was created by the policy-channel fix itself. The +5 is one
accumulator step in the shape the other four already use. The alternative — document that
it deliberately does not — was refused: it would record a defect rather than close one.
Bucket ``refusal`` 40 -> 45.

WHAT THE GUARDS BOUGHT. Nearly every statement added to this module after its first
working version closes one instance of a single class — a fault arriving AFTER a fact was
established, converting that fact into ignorance:

* ``_scan_cells``: the RESIDUAL per-cell handler, so an UNENUMERATED fault costs ONE gap
  instead of unwinding the loop, and the ``_hit`` tuple unpack, so a degraded pickup field
  is disclosed as a gap BESIDE the hit it belongs to.
* ``_hit``: the ``to_wire`` guard, so an unrenderable ``Column`` can no longer cost the
  entire finding.
* ``geometry_refs`` and ``cb_par_refs``: the per-arm guard that makes each docstring's
  "NEVER raises" DECIDED rather than claimed, returning the accumulated hits rather than
  ``([], [])``, which would certify a clean audit of cells nobody read. The guard first
  covered ``_scan_cells`` and not the non-CB branch above it, so a second ``Type`` read
  that could not RENDER still raised out of a function documented NEVER to.
* ``_confirm_one``: the reference read in its OWN ``try``, so a wedge on the second
  ``GetSolveData()`` stops discarding a SUCCESSFUL type read.
* ``confirm_after``: the per-ENTRY guard, so one malformed entry cannot discard the
  later ones.
* ``disclosure`` / ``_established`` / ``_plain``: a late wire failure used to replace the
  WHOLE block, discarding every established row/cell/source identity at the one moment
  they are unrecoverable — the removal has happened and has no inverse. A block that fails
  ``assert_wire_safe`` cannot be shipped, so what ships is a minimal projection that is
  wire-safe by construction and re-asserted, with the bare block still beneath it.
* ``_verdict``: the accumulator hoist preserved the hits and left a hard-coded
  ``_COULD_NOT_SCAN`` beside them, so the wire published ``could_not_scan`` WITH a
  non-empty ``affected`` list. The two decisions are now ONE expression.
* ``_block``: once a FAULTED scan can legitimately read ``affected``, a ``reason`` emitted
  only in the ``could_not_scan`` state silently drops the record that the scan did not
  finish.
* ``_cause``: ONE guarded exception render for BOTH never-raise handlers that had an
  unguarded ``%r``, written once rather than twice, per the standing veto on a duplicated
  fault contract.
* ``_remedy``: the geometry-pickup tier NAMED ``set_solve`` and then withheld three of the
  four values ``set_solve`` takes, though ``column``, ``scale_factor`` and ``offset`` were
  already in the hit dict. They are rendered now, inside their OWN ``try``, because
  rendering a hostile parameter in the same expression as ``where`` would let a decorative
  field destroy an established identity.
* ``refusal_message``: the hits AND the unknowns together, plus the per-hit remedy loop —
  one undescribable hit inside a ``join`` generator used to drop every hit — with each
  piece appended by its own step, so "one erases the other" is not constructible. The
  steps read no other step's output; the count step got NO handler, because its input is a
  list this function built and a handler that cannot be reached cannot be falsified.
* ``_REPLAY_NULL`` / ``_replay``: a pickup parameter this scan could not put a number on
  is rendered as an instruction rather than as a value the re-author door would reject —
  ``set_solve`` REJECTS a non-finite offset, so a remedy printing ``Offset=None`` named a
  door the caller could not walk through.

NONE of it is new capability, and none of it is padding: each is the difference between a
report that discloses what it knows and one that throws it away. Every PRIVATE helper is
documented by ``#`` comment rather than by a docstring (a docstring is an ``ast.stmt``; a
comment is free), the two arms share ONE per-cell body so the taxonomy cannot drift
between them, and the accumulators are passed by reference so no arm pays for a merge. A
further ~5 could be had by inlining ``_gap`` / ``_wire_num`` / the two ``*_fields``
resolvers, at the cost of duplicating predicates the one-definition rule keeps single.

NO FLOOR IS CLAIMED. A floor is a statement about every possible implementation of this
contract, and nothing here measured one — only this implementation was measured. A
precise-sounding floor derived from one implementation is the same
inference-as-measurement error this module exists to correct.

NOTHING IN THE CONTRACT WAS CUT TO CHASE THE NUMBER. Cutting the taxonomy, the
``ref_after`` re-read, the totality of the consequence partition or the three-tier
routing would each ship exactly the defect class this module exists to close — a success
envelope that discloses less than it knows.
"""
from . import _cb_cells as _cb
from . import _solve_cells as _sc
from . import surface_solve as _ss
from ._tol_cells import is_integral_int

#: The three verdict tokens. ORDERED and FROZEN. The emit AND the tests derive from this
#: constant; a test that copies the vocabulary proves nothing.
VERDICTS = ("none_affected", "affected", "could_not_scan")

#: The three CONSEQUENCE tokens, FROZEN under the same rule and derived the same way.
#: A FOURTH TOKEN CLAIMING THE RELATIONSHIP OUTLIVED THE REMOVAL IS DELIBERATELY ABSENT.
#: On THIS operation the authored relationship
#: cannot survive by definition — the hits are exactly the solves referencing row ``at``,
#: and ``at`` is the row being deleted, so whatever a retained type now points at is not
#: the row the author named. Such a token would be wrong in every reachable case, not
#: merely unproven in some. It is therefore absent from the vocabulary AND from this
#: module, so a reader cannot find the word and infer the claim was merely unproven.
CONSEQUENCES = ("solve_removed", "retained_type", "unmeasured")

_NONE_AFFECTED, _AFFECTED, _COULD_NOT_SCAN = VERDICTS
_SOLVE_REMOVED, _RETAINED_TYPE, _UNMEASURED = CONSEQUENCES

#: The two ``table`` values an entry or a gap can carry.
_GEOMETRY, _CB_PAR = "geometry", "cb_par"

#: The one solve family whose DELETION consequence was measured, and the only family the
#: hazard PROSE is scoped to. The catalog-driven scan reports FIVE more (``Position``,
#: ``CenterOfCurvature``, ``Compensator``, ``CocentricRadius``, ``CocentricSurface``) as
#: reference FACTS with ``consequence`` decided per call.
_PICKUP = "SurfacePickup"

#: Par-cell solve types that are a DEFAULT — not affected, no gap, no wire cost.
#:
#: ``"Variable"`` WAS ADDED ON A LIVE MEASUREMENT, NOT ON AN INFERENCE.
#: Before it, a coordinate-break Par cell carrying an optimizer DOF — authored by the
#: SHIPPED ``set_cb_variable``, i.e. the ordinary fold-optimisation workflow — gapped as
#: ``"par solve type unmeasured (Variable)"``, so an optimised CB design could NEVER read
#: ``none_affected`` and ``refuse_on_solve_refs=true`` refused EVERY removal on one. The
#: live measurement is that a ``Variable`` Par cell resolves to an ``ISolveVariable`` view
#: exposing NO ``INDEX_FIELDS`` member at all (``index_fields_exposed == []``): it
#: STRUCTURALLY cannot carry a surface reference, exactly like the geometry-cell
#: ``Variable`` that ``_sc._MEASURED`` already catalogues as ``Variable -> ()`` (clean, no
#: gap). The catalog rule pre-authorises exactly this — "that is a MEASUREMENT and the
#: catalog widens with it recorded".
#:
#: ``"Automatic"`` IS DELIBERATELY EXCLUDED, and this set is deliberately NOT
#: ``_sc.NON_DRIVING``. ``NON_DRIVING`` also contains ``"Automatic"``, which is UNMEASURED
#: on a Par cell — widening past the measurement to the nearest ready-made vocabulary is
#: the inference-as-measurement error this module exists to correct, one token over. An
#: ``Automatic`` Par cell therefore still GAPS, and stays a measurement to make.
_PAR_DEFAULTS = frozenset({"Fixed", "None", "Variable"})


#: Surface types whose Par table this package RECOGNISES and does not audit. A POSITIVE
#: ALLOW-LIST, and the polarity is the whole point.
#:
#: A REVIEW PASS PROVED THE DENYLIST FAILED OPEN, in two ways with one root.
#: The first cut sent EVERY non-CB, non-Standard type to the coverage channel, so the
#: branch could not tell "a type we chose not to audit" from "a coordinate break we failed
#: to recognise" or "a Type read that FAILED" -- and both of those now reached a channel
#: that does not alarm. The spaced-render instance below is CORRECTED; the fail-open
#: ARGUMENT it illustrates is unaffected, because the third instance is real and
#: unmeasured. And ``_field(row, "Type")`` defaults to ``None`` on a throw, which renders
#: as the string ``"None"`` -- a read FAULT wearing a coverage note. That one is still
#: UNMEASURED: no reproduction recipe exists, and the 0.1.7 live gate did not manufacture
#: one.
#:
#: CORRECTED BY LIVE MEASUREMENT -- the sentence that stood here claimed, as
#: MEASURED, that "a CB row whose Type renders ``"Coordinate Break"`` (spaced) misses
#: ``is_coordinate_break``'s substring test". **That is FALSE on OpticStudio 2025 R1.**
#: The 0.1.7 live gate captured both provenances for all 17 recognised types plus two
#: controls: ``"%s" % row.Type`` renders the UNSPACED enum member (``'CoordinateBreak'``,
#: ``'EvenAspheric'``, ``'Gradient2'``) and ``row.TypeName`` renders the SPACED display
#: string (``'Coordinate Break'``, ``'Even Asphere'``, ``'Gradient 2'``). The spaced string
#: is real -- but it lives on ``TypeName``, which NO production seam reads. This module
#: reads ``_field(row, "Type")``, so ``is_coordinate_break``'s substring test PASSES and
#: every one of the 17 tokens matches the allow-list exactly.
#: THE ORIGIN, identified during the same Completion Sync and worth recording because the
#: mechanism is reusable: an earlier gotcha captures a CB retype under the key
#: ``type_name_after``, whose VALUE is correct but whose NAME reads as though it were
#: ``row.Type``. It is not -- the solve-inventory probe reads ``str(row.TypeName)``.
#: So a correct measurement OF ``TypeName`` was later read as a measurement of ``Type``,
#: and the false claim descends from a capture KEY NAME, not from a bad reading. It is
#: corrected rather than deleted because a reader who meets the spaced string on
#: ``TypeName`` needs to know it was investigated and which seam it is not on.
#: (Also measured, and it sharpens the standing refusal to normalize: ``Gradium`` renders
#: ``TypeName`` = ``'GRADIUM'`` -- ALL-CAPS and UNSPACED. Space-stripping alone would not
#: even recover it, so the rejected remedy is worse than it was priced.)
#:
#: Under the allow-list both land in the ``else``: unrecognised -> GAP -> ``could_not_scan``
#: -> strict refuses. An unrecognised token costs a false refusal; an unrecognised token
#: waved through costs a destroyed relationship reported as clean. Only the first is
#: recoverable, and the ABSENT-vs-UNREADABLE rule governs the direction.
#:
#: DERIVED AGAINST THE **RECOGNITION** SETS, NOT THE AUTHORING ONES, AND THAT DISTINCTION IS
#: THE WHOLE GUARD. ``test_the_unaudited_par_types_are_DERIVED_from_the_authoring_modules``
#: rebuilds this set from ``_asphere_cells.ASPHERE_TYPE_INFO``,
#: ``_grin_cells.GRIN_FAMILY_TYPE_TOKENS`` and the grating token. The derivation lives in the
#: test because this module is at its declared ceiling and a comprehension here would cost
#: statements the test can spend for free.
#:
#: THE EARLIER WORDING HERE WAS FALSE IN BOTH HALVES, and the 0.1.6 external review found
#: what that bought. It claimed the test "asserts equality" (it asserts a SUBSET) and that it
#: derives from ``GRIN_TYPE_INFO`` -- the **authorable** GRIN types, of which there are TWO,
#: against TWELVE the package RECOGNISES. But this branch classifies a type READ BACK off a
#: loaded design, never one we authored, so the authorable set is the wrong universe by
#: construction: ten of the twelve recognised tokens are not authorable, eight of them had
#: been hand-added here, and ``Gradium`` / ``GridGradient`` were simply missed. A subset
#: assertion over a 7-member universe could not see it -- the set stayed green at 16 members
#: while a design carrying either token hit the ``else`` arm, reported a FAULT, and refused
#: every strict removal. That is the M1 outage reproduced on a type we recognise, and it is
#: precisely the "transcription that quietly falls behind" the test's own docstring promises
#: to prevent. Recognition is what must drive this set; authorability is a narrower thing.
#: ``EvenAsphere`` IS GONE (0.1.6 review), AND THE DIRECTION ARGUMENT RESOLVES THE
#: OPPOSITE WAY TO HOW IT WAS FIRST FRAMED. It was carried here as a "spelling variant that
#: guards an enum-rendering difference between engine versions" -- a rationale with no
#: measurement anywhere behind it. What ``_asphere_cells`` ACTUALLY records about that token
#: (lines 22-23, 216, 327) is the opposite kind of fact: it is the AUTHORING-side misspelling
#: trap, and ``getattr(SurfaceType, "EvenAsphere")`` RAISES. No engine, no version, no
#: capture in this tree has ever rendered it on a READ-BACK, which is the only side this set
#: classifies.
#:
#: An unmeasured member of an ALLOW-LIST is a WAIVER, and this list's own doctrine six
#: paragraphs up decides which direction a waiver may fail in: "an unrecognised token costs
#: a false refusal; one WAVED THROUGH costs a destroyed relationship reported as clean. Only
#: the first is recoverable." Keeping the token buys the recoverable failure ONLY if the
#: token never appears; if any engine ever did render it, the row lands in COVERAGE, the
#: verdict stays clean, and ``refuse_on_solve_refs=true`` PROCEEDS over an unaudited Par
#: table. That is the fail-OPEN half, bought against a hazard nobody measured.
#:
#: Deleting it rather than rewording it is what makes the equality assertions in
#: ``test_adv_solve_refs_ia_r1`` and ``release/public-authored/``
#: LOUD: with the token gone and any exemption left behind, both fail. If a real
#: rendering difference is ever MEASURED, it comes back with the capture beside it.
_UNAUDITED_PAR_TYPES = frozenset({
    "EvenAspheric", "OddAsphere", "ExtendedAsphere", "ExtendedOddAsphere",
    "Gradient1", "Gradient2", "Gradient3", "Gradient4", "Gradient5", "Gradient6",
    "Gradient7", "Gradient9", "Gradient10", "Gradient12", "Gradium", "GridGradient",
    "DiffractionGrating",
})

#: ``unscanned`` is capped; ``unscanned_count`` reports the TRUE total, never this length.
_UNSCANNED_CAP = 20

#: The key the MINIMAL PROJECTION is published under when the full block cannot be shipped.
#: NOT ``affected``: the wire contract makes ``affected`` present IFF ``state ==
#: "affected"``, and a ``could_not_scan`` block carrying an ``affected`` list is precisely
#: the contradiction an audit found one key over.
_ESTABLISHED = "established_before_the_failure"

#: A read-fault sentinel. ``None`` is a LEGITIMATE field value, so a bare ``None`` return
#: from a guarded ``getattr`` cannot distinguish "the field read back None" from "the read
#: threw" — the ABSENT-vs-UNREADABLE collapse, one layer down.
_UNREADABLE = object()

#: The scope, ON THE WIRE. It states what was audited, what was NOT, and — per entry
#: family — whether the disclosure is a recovery path or only a record.
#:
#: THE PAR RANGE WAS STALE AND IS NOW CORRECTED, AND IT SHIPS IN EVERY ENVELOPE. It
#: read "Par1/Par3 only", which was TRUE when written: the capture recorded
#: post-removal solve state
#: for exactly those two, and "all five die at once" was an INFERENCE. A live row was made
#: NON-WAIVABLE to retire that inference and it now asserts ``solve_removed`` plus a
#: non-null, non-driving ``type_after`` for ALL FIVE Par cells — and passes. So the
#: measurement widened and this string did not follow it; ``remove_surface``'s own
#: description already says "Par1-Par5". An understated scope is not the safe direction it
#: looks like: it tells a caller the other three cells are unmeasured when they were
#: measured, on the one string that ships with every disclosure.
#:
#: ``insert_surface``'s "Par1/Par3" is a DIFFERENT CLAIM and is correctly left alone —
#: reference TRACKING across an insert/remove of ANOTHER row, where Par2/Par4/Par5 remain
#: genuinely inferred. Two claims, two measurements, two ranges; a sweep that unified them
#: would launder an inference into a measurement (a test pins them apart).
#:
#: THE COVERAGE CLAUSE NAMES **BOTH** KEYS, AND THE SECOND HALF WAS MISSING (0.1.6 PR
#: review). ``SCOPE`` ships on EVERY envelope -- the degraded one included -- and the
#: clause said flatly that those surfaces "are named in ``par_refs_not_audited``". On the
#: degraded path that key is STRUCTURALLY ABSENT: ``_block`` never ran, and F-I's fix
#: publishes the same records under
#: ``established_before_the_failure.not_audited_identities`` instead. So the string was
#: directing a caller to read a key that cannot be there, on the one path where the removal
#: has already happened and the records are the caller's last handle.
#:
#: The amendment is ADDITIVE -- a parenthetical, no clause removed, no wording reflowed --
#: because the pins on this string are of two kinds and both must survive: substring
#: assertions on individual clauses, and identity assertions (``block["scope"] is/==
#: SCOPE``) that only care that one object ships everywhere. Rewriting the sentence would
#: have risked the first for no gain. ``test_the_scope_string_states_the_three_tier_re_author_truth``
#: pins the new key by substring, so deleting the parenthetical reddens rather than
#: silently restoring the dangling reference.
#:
#: THE OTHER TWO SERVED SURFACES ARE DELIBERATELY LEFT ALONE, and the distinction is what
#: bounds this fix. ``lens_surface``'s ``remove_surface`` description and
#: ``server_mcp``'s instructions also name ``par_refs_not_audited``, and they are CORRECT
#: as written: they describe the PRIMARY contract to an agent choosing a tool, and neither
#: is CO-SHIPPED with a degraded envelope -- so neither can point a caller at an absent key
#: in the moment they need it. Only ``SCOPE`` travels inside the block that lost the key.
#: Sweeping all three would have widened a boundary case into the headline description of
#: a door that almost never takes that path.
SCOPE = (
    "solve references FROM SURVIVING ROWS to the removed row, on the five geometry cells "
    "(catalog-driven: pickup and the five index-field solve types) plus coordinate-break "
    "parameter cells, current configuration only. Solves ON the removed row are not "
    "reported. NOT audited: merit-function, tolerance and multi-configuration surface "
    "references; asphere/grating/GRIN parameter cells -- those surfaces are named in "
    "par_refs_not_audited (or, when the disclosure could not be built, under "
    "established_before_the_failure.not_audited_identities), which is a COVERAGE "
    "statement and not a fault; a surface "
    "whose type could not be read or recognised is a FAULT and appears in unscanned "
    "instead. Strict mode (refuse_on_solve_refs) refuses on the fault, NOT on the "
    "coverage record. "
    "Consequence is measured per entry "
    "(type_after plus the re-read reference); the deletion hazard is measured for "
    "SurfacePickup on the thickness cell and on coordinate-break Par1-Par5. A "
    "GEOMETRY-cell pickup entry carries what set_solve needs to re-author it; a "
    "coordinate-break Par entry has NO re-author door (set_solve's domain is the five "
    "geometry cells), so it is a record, not a recovery "
    "path; an entry for another solve family carries its reference only, not that "
    "family's other parameters."
)


# --------------------------------------------------------------------------- #
# Shared readers. Every one of them is NEVER-RAISE by construction.
# --------------------------------------------------------------------------- #
# One gap record. ``cell`` is ``None`` for a whole-table gap (an unreadable row type).
def _gap(surface, cell, table, reason):
    return {"surface": surface, "cell": cell, "table": table, "reason": reason}


# A GUARDED read of one solve-view / row field. A throw answers ``default`` — and the
# caller chooses whether that default is distinguishable from a real ``None`` by passing
# ``_UNREADABLE``.
def _field(view, name, default=None):
    try:
        return getattr(view, name)
    except Exception:  # noqa: BLE001 — a never-raise reader absorbs nothing above Exception
        return default


# ``repr`` OF AN ARBITRARY EXCEPTION, GUARDED — ONE helper, BOTH never-raise handlers that
# render a cause (``scan_removal``'s and ``disclosure``'s). ``"%r" % (exc,)`` calls
# ``exc.__repr__``; the exceptions this module catches include whatever a hostile engine
# proxy raised from its own ``__str__``, and such an object may equally throw from its
# ``__repr__`` — a raise out of a handler in a function documented NEVER to raise. Written
# ONCE rather than inline at each site, because two inline ``try``s are how the two
# renderings drift (the standing veto on a duplicated fault contract).
def _cause(exc):
    try:
        return "%r" % (exc,)
    except Exception:  # noqa: BLE001 — the cause render may not itself become a raise
        return "<an exception whose repr could not be rendered>"


# A value that is ALREADY a plain wire type, else ``None``. IT COERCES NOTHING AND CANNOT
# RAISE — that is its whole purpose. ``str(value)`` on a proxy whose ``__str__`` throws is
# one of the faults the minimal projection exists to SURVIVE, so the projection must never
# call one. ``float`` is excluded on purpose (a non-finite one is the wire hazard
# ``_wire_num`` exists for, and no projected field is a float); ``bool`` is listed FIRST
# only for readability — ``assert_wire_safe`` accepts it either way.
def _plain(value):
    return value if isinstance(value, (bool, int, str)) else None


# A numeric wire field. Non-finite or non-numeric -> ``None``.
#
# THIS IS THE SHARED PREDICATE, NOT A HAND ``math.isfinite``. ``_is_finite_number`` exists
# because two validators had independently written ``isinstance(...) and
# math.isfinite(value)`` and BOTH inherited the same defect: ``math.isfinite`` RAISES
# ``OverflowError`` on an ``int`` too large for a ``float`` (``10**400`` arrives intact
# through JSON). Writing the guard by hand here would be the third instance of a defect
# whose fix is one import away. It matters twice over: a pickup's ``Offset`` reads back
# ``NaN`` on four of the five geometry cells and ``ScaleFactor`` reads ``NaN`` on
# ``material``, and ``_sc.assert_wire_safe`` RAISES on a non-finite float — that raise
# inside the never-raise scanner would degrade EVERY affected call to ``could_not_scan``.
def _wire_num(value):
    return value if _ss._is_finite_number(value) else None


# THE REFERENCE-VALUE ACCEPTANCE RULE, IN ONE PLACE, WITH BOTH ITS HALVES.
# ``(is_hit, gap_reason)`` — exactly one is set.
#
# 1. REPRESENTATION — ``_tol_cells.is_integral_int``, the SAME shared predicate the
#    authoring door uses (``surface_solve``'s "must be an exact integer surface index"),
#    and NO ``int()`` coercion anywhere. A ``str``, ``float``, ``bool``, ``None``, ``nan``
#    or a raw proxy is a GAP, never a hit and never a pass. It FAILS CLOSED: if the engine
#    ever hands back an int-like that is not a Python ``int``, the call reads
#    ``could_not_scan``, never a false ``none_affected`` — and that is a MEASUREMENT to
#    record and widen the rule against, not a prediction to pre-empt.
# 2. RANGE — ``0 <= raw <= n - 1``, the door's own domain (the set
#    ``_lens_common._require_read_index`` decides), so the guard and the executor accept
#    the same set. An in-representation, out-of-domain reference is a GAP, never "not
#    affected": it names neither ``at`` nor any surviving row, so calling it "not affected"
#    would certify an interpretation it does not have.
def _ref_outcome(view, field, at, n):
    raw = _field(view, field, _UNREADABLE)
    if raw is _UNREADABLE:
        return False, "ref field unreadable"
    if not is_integral_int(raw):
        return False, "ref field not an exact integer"
    if not 0 <= raw <= n - 1:
        return False, "ref field out of range"
    return raw == at, None


# One PRE-mutation ``affected`` entry, as ``(entry, degraded_reason_or_None)``. Every value
# that leaves here is wire-safe: the ``Column`` proxy through the ONE shared rule
# ``surface_solve.to_wire`` (never a hand ``str()``, which would be the fourth copy, and
# never ``int()``, which SUCCEEDS and returns a per-cell ordinal where the token was
# intended), and both numerics through the non-finite guard.
#
# THE HIT IS ALREADY ESTABLISHED BY THE TIME THIS RUNS, SO NOTHING HERE MAY DESTROY IT —
# that is this module's governing class. ``to_wire`` ends in ``str(value)``,
# which RAISES on a proxy whose ``__str__`` throws; that raise used to travel out of
# ``_scan_cells``, out of the arm, into ``scan_removal``'s outer handler, and returned
# ``{"verdict": "could_not_scan", "hits": []}`` — a scan that KNEW a pickup referenced the
# row reporting that it knew nothing, because ONE decorative field was unreadable. The
# wire contract's state table rules the other way: a fact outranks missing knowledge, and
# the
# missing knowledge is disclosed BESIDE it. So the optional pickup extras are guarded, the
# unreadable one is emitted ``None``, and the caller is handed a GAP reason to append next
# to the hit it is keeping. Discarding the hit was the deviation; this is the conformance.
def _hit(surface, at, name, table, solve_type, field, view):
    entry = {"surface": surface,
             "surface_after": surface - 1 if surface > at else surface,
             "cell": name, "table": table, "solve_type": solve_type,
             "ref_field": field, "source": at}
    degraded = None
    if solve_type == _PICKUP:
        try:
            entry["column"] = _ss.to_wire("Column", _field(view, "Column"))
        except Exception:  # noqa: BLE001 — a decorative field may NEVER cost a known hit
            entry["column"] = None
            degraded = "pickup column unreadable"
        entry["scale_factor"] = _wire_num(_field(view, "ScaleFactor"))
        entry["offset"] = _wire_num(_field(view, "Offset"))
    return entry, degraded


# THE ONE PER-SURFACE BODY BOTH ARMS RUN, so the fault taxonomy cannot drift between them.
# It appends into the caller's accumulators. ``get_cell`` fetches one named cell (and may
# RAISE); ``fields_of`` answers ``(ref_fields, gap_reason)`` for one ``(name, solve_type)``.
#
# EVERY FAULT IS A GAP, NEVER A PASS: cannot-certify is never reported clean.
#
# AND EVERY FAULT IS SCOPED TO ONE CELL. The RESIDUAL handler below is the direct-invariant
# half of that same class: the enumerated taxonomy answers the faults it names, and
# anything UNENUMERATED (``read_solve_type``'s callee widening, a catalog ``lookup``
# throw — reproduced in review — an exotic proxy in ``_ref_outcome``) lands on
# ONE gap for ONE cell instead of unwinding the loop and taking every hit already in
# ``hits`` with it. Point-fixing the three reported sites would have left the fourth
# unenumerated fault to rediscover; this bounds the blast radius of ALL of them at the cell.
def _scan_cells(hits, gaps, surface, at, n, table, names, get_cell, fields_of):
    for name in names:
        try:
            cell = get_cell(name)
        except Exception:  # noqa: BLE001 — a per-cell fetch fault is DISCLOSED per cell
            gaps.append(_gap(surface, name, table, "solve cell unreadable"))
            continue
        try:
            solve_type = _sc.read_solve_type(cell)
            if solve_type is None:
                gaps.append(_gap(surface, name, table, "solve type unreadable"))
                continue
            fields, reason = fields_of(name, solve_type)
            if reason is not None:
                gaps.append(_gap(surface, name, table, reason))
                continue
            if not fields:
                continue      # catalogued, and it carries no surface reference at all
            try:
                view = _sc.unwrap_or_none(cell.GetSolveData())
            except Exception:  # noqa: BLE001
                view = None
            if view is None:
                gaps.append(_gap(surface, name, table, "solve view unresolvable"))
                continue
            for field in fields:
                hit, reason = _ref_outcome(view, field, at, n)
                if reason is not None:
                    gaps.append(_gap(surface, name, table, reason))
                elif hit:
                    entry, degraded = _hit(surface, at, name, table, solve_type, field,
                                           view)
                    hits.append(entry)
                    if degraded is not None:
                        gaps.append(_gap(surface, name, table, degraded))
        except Exception:  # noqa: BLE001 — RESIDUAL: an UNENUMERATED per-cell fault is ONE
            gaps.append(_gap(surface, name, table, "solve scan faulted"))


# THE GEOMETRY ARM'S FIELD RESOLUTION IS CATALOG-DRIVEN, NOT PICKUP-ONLY.
# ``SurfacePickup``'s own measured field tuple CONTAINS ``"Surface"``, which is in
# ``INDEX_FIELDS``, so this intersection SUBSUMES the pickup case with zero special-casing
# and additionally decides the five sibling index-field families
# (``CocentricRadius.WithSurface``, ``CocentricSurface.AboutSurface``,
# ``CenterOfCurvature.RefSurface``, ``Compensator.RefSurface``, ``Position.FromSurface``).
# A uniform loop is FEWER statements than "pickup special-case + a five-name limitation
# string", and the served limitation under pickup-only would have had to say that we saw a
# ``Position`` solve on the very cell we scanned and did not check whether it names the row
# being removed.
#
# AN UNMEASURED ``(cell, type)`` PAIR FAILS CLOSED: its field shape is unknown, so it MIGHT
# carry an index field this scan cannot name -> a GAP, mirroring ``lookup``'s own
# ``("unmeasured", None)`` tag and ``emit_solves_block``'s ``fields_unmeasured`` channel.
#
# A CATALOGUED pair with ``()`` fields (all five ``<Cell>.Fixed``, ``Radius``/``Conic``/
# ``Thickness`` ``.Variable``, ``SemiDiameter.Automatic``) reads "not affected" at zero
# wire cost and NO gap — which is what lets an ordinary clean design reach
# ``none_affected`` at all, given that state requires ``gaps == []``.
def _geometry_fields(token, solve_type):
    tag, fields = _sc.lookup(token, solve_type)
    if tag == "unmeasured":
        return (), "solve fields unmeasured"
    return tuple(f for f in fields if f in _sc.INDEX_FIELDS), None


# THE CB PAR ARM IS PICKUP-ONLY, AND THAT IS NOT AN INCONSISTENCY WITH THE ABOVE.
# ``_MEASURED`` catalogs the five GEOMETRY cells only — there is no Par catalog, so there
# is nothing to drive a catalog scan with. This arm detects ``SurfacePickup`` by type and
# reads ``.Surface`` (the one Par solve shape measured readable on all eight arms);
# any OTHER non-default Par solve type is a GAP, never a pass.
def _par_fields(_name, solve_type):
    if solve_type in _PAR_DEFAULTS:
        return (), None
    if solve_type != _PICKUP:
        return (), "par solve type unmeasured (%s)" % (solve_type,)
    return ("Surface",), None


def geometry_refs(system, lde, surface, at, n):
    """The five-cell CATALOG-DRIVEN arm for one surface. ``(hits, gaps, not_audited)``.

    NEVER raises. The third slot is always empty here: this arm's catalog covers all five
    geometry cells, so it has no by-policy exclusion to report. It exists so both arms
    share ONE shape and ``scan_removal``'s loop cannot special-case them apart.

    ``n`` IS A PARAMETER RATHER THAN A PER-SURFACE RE-READ. The range gate takes the
    surface count "from the count already in hand", i.e. the one ``scan_removal`` read
    once. The alternative — re-reading ``lde.NumberOfSurfaces`` inside the per-surface
    arm — would add an engine read per surface on the hottest structural path for no
    information.
    """
    hits, gaps = [], []
    try:
        _scan_cells(hits, gaps, surface, at, n, _GEOMETRY, _sc.CELL_TOKENS,
                    lambda token: _sc.solve_cell(system, lde, surface, token),
                    _geometry_fields)
    except Exception:  # noqa: BLE001 — the docstring's NEVER-RAISES, DECIDED not claimed
        gaps.append(_gap(surface, None, _GEOMETRY, "geometry scan faulted"))
    return hits, gaps, []


def cb_par_refs(system, lde, surface, at, n):
    """The Par1..Par6 arm, GATED on ``_cb_cells.is_coordinate_break``. NEVER raises.

    THE GATE IS A FIDELITY REQUIREMENT, NOT AN OPTIMISATION: a Standard row's Par1 is a
    STRING cell whose Double read RAISES live (``ArgumentException("Expected Double, got
    'String'")``), so an ungated sweep would put EVERY Standard row in
    ``gaps`` and flip the verdict of a clean 8-surface system to ``could_not_scan``.

    THE SPLIT HAS THREE OUTCOMES, NOT TWO, AND THE THIRD IS A POLICY RECORD — NOT A GAP.
    CB -> scanned; Standard -> nothing to scan and nothing to disclose; anything else
    (asphere / grating / GRIN) -> ``not_audited``; unreadable/unrenderable -> a GAP.

    THE PARAGRAPH THIS REPLACES WAS FALSE IN BOTH DIRECTIONS AND SHIPPED THAT WAY. It
    claimed the split "has no third option" and "inherits ``emit_solves_block``'s polarity
    exactly". Neither held: ``emit_solves_block`` does not gap at all — it emits a
    fail-OPEN boolean ``par_cell_solves_not_audited`` (a DIFFERENT key from this door's
    ``par_refs_not_audited`` — see ``_block``) — and its
    ``_FULLY_COVERED_TYPE_TOKENS`` is ``{"Standard"}``, so it discloses for a CB row too.
    The polarity is Standard-only-clean, not CB-scanned/Standard-clean. A sentence that
    names another module's behaviour is a claim about THAT module and has to be re-derived
    from it, never carried across by analogy.

    WHY THE POLICY RECORD LEFT THE GAP CHANNEL (a defect reproduced and measured
    live before it was believed). A gap makes ``_verdict`` return ``could_not_scan``, so ONE
    asphere row — an ordinary case this package ships tools to author — made
    ``none_affected`` STRUCTURALLY UNREACHABLE and ``refuse_on_solve_refs=true`` refuse
    EVERY removal on that design, with no override. Measured on the same Cooke triplet:
    all-Standard -> ``none_affected``, strict removal succeeds; one ``EvenAspheric`` ->
    ``could_not_scan``, strict removal refuses. It also inverted this module's own
    taxonomy at the top level: ``could_not_scan`` means WE TRIED AND COULD NOT READ, and
    "this package has no Par catalog for this surface type" is a POLICY statement about
    coverage — the ABSENT-vs-UNREADABLE distinction.

    IT IS NOT SUPPRESSED, AND THAT IS THE WHOLE POINT. The sibling fix at ``_PAR_DEFAULTS``
    made a ``Variable`` Par cell clean OUTRIGHT, licensed by a live measurement that such a
    view exposes no index field and so STRUCTURALLY cannot name a surface. No equivalent
    measurement exists for an asphere/grating/GRIN Par table — whether one can carry a
    pickup at all is UNMEASURED — so ruling it clean would be the inference-as-measurement
    error ``_PAR_DEFAULTS``'s own comment forbids. The table is still not audited; the
    caller is now TOLD so on a channel that does not claim a fault.

    ALL SIX PAR CELLS ARE SWEPT, Par6/``order`` included. Reading its solve type is
    benign — a default read is suppressed at zero wire cost and a throw is a gap — and
    "an Integer Par cell cannot carry a pickup" is exactly the kind of unmeasured claim
    this module refuses to encode (the probe captures record it as UNMEASURED).
    """
    try:
        row = lde.GetSurfaceAt(surface)
        is_cb = _cb.is_coordinate_break(row)
    except Exception:  # noqa: BLE001 — is_coordinate_break RAISES on a Type read throw
        return [], [_gap(surface, None, _CB_PAR, "surface type unreadable")], []
    if not is_cb:
        # THE NON-CB BRANCH IS INSIDE A GUARD TOO — the second half of the partial-state
        # finding. An earlier fix guarded ``_scan_cells`` and left this
        # branch outside it, so a SECOND ``Type`` read whose value cannot RENDER
        # (``str(kind)``, and the ``%s`` below) raised straight out of a function whose
        # docstring says NEVER raises — the fix-lands-at-one-site-not-its-sibling shape.
        # The render happens ONCE, here, so both the comparison and the message are covered.
        try:
            kind = "%s" % (_field(row, "Type"),)
        except Exception:  # noqa: BLE001 — an unrenderable Type is a GAP, never a raise
            return [], [_gap(surface, None, _CB_PAR, "surface type unrenderable")], []
        if kind == "Standard":
            return [], [], []
        if kind not in _UNAUDITED_PAR_TYPES:
            # UNRECOGNISED -> FAULT, never coverage. See _UNAUDITED_PAR_TYPES: this is the
            # arm that catches a mis-detected coordinate break and a Type read that failed,
            # both of which the first cut waved through as a coverage note.
            return [], [_gap(surface, None, _CB_PAR,
                             "surface type not recognised (%s)" % (kind,))], []
        # SLOT 3, NOT ``gaps`` -- see the docstring. The RECORD SHAPE is deliberately
        # ``_gap``'s: same four keys, so the wire-safety assert, the ``_plain`` projection
        # and every renderer keep working unchanged, and the only thing that moved is
        # WHICH channel it travels on. Reusing it also costs zero statements against a
        # module at its ceiling.
        return [], [], [_gap(surface, None, _CB_PAR,
                             "par table not audited for surface type %s" % (kind,))]
    hits, gaps = [], []
    try:
        _scan_cells(hits, gaps, surface, at, n, _CB_PAR, _cb._PARAM_NAMES,
                    lambda param: _cb._cb_cell(system, row, param), _par_fields)
    except Exception:  # noqa: BLE001 — the docstring's NEVER-RAISES, DECIDED not claimed
        gaps.append(_gap(surface, None, _CB_PAR, "par scan faulted"))
    return hits, gaps, []


# THE VERDICT, DERIVED ONCE, FOR **BOTH** OF ``scan_removal``'S RETURNS — the other half
# of the partial-state finding. An earlier fix hoisted the accumulators above the ``try``
# so a fault could no
# longer throw away what had already been found — and left the handler's HARD-CODED
# ``_COULD_NOT_SCAN`` untouched. The result was a wire state that contradicted its own
# payload: ``{"verdict": "could_not_scan", "hits": [<a real hit>]}``, which ``disclosure``
# then published as ``state == "could_not_scan"`` BESIDE a non-empty ``affected`` list,
# breaking the present-IFF-affected contract the wire rules rest on. A caller branching on
# the ONE token that carries the guarantee was told nothing had been concluded while being
# handed conclusions. That is the textbook
# fix-preserves-the-data-and-leaves-the-co-located-decision-stale shape, and the fix is not
# to correct the second literal but to REMOVE the second decision: there is now ONE
# expression, and a future edit cannot desynchronise two conditionals that no longer exist.
#
# ``completed`` IS A THIRD INPUT AND IT IS NOT OPTIONAL. A total scan failure can carry
# ZERO hits and ZERO gaps (the surface-count read is the first thing that can raise, before
# anything is accumulated), and ``(hits=[], gaps=[])`` is EXACTLY the shape that means
# ``none_affected``. Deriving from the two lists ALONE would therefore publish a scan that
# never ran as a CLEAN one — the false-clean this module's every other guard exists to
# prevent (cannot-certify is never reported clean). The alternative considered and
# rejected was to synthesise a gap on the fault path: ``gaps`` means "cells that could not
# be scanned" and ``refusal_message`` renders ``len(gaps)`` as "%d cell(s)", so a whole-scan
# fault appended there would both inflate a cell count with a non-cell and need a third
# ``table`` token the wire contract does not have.
#
# AFFECTED STILL DOMINATES, on BOTH paths. A fault arriving after a hit was established
# does not un-establish it (that is the class these guards close), so the verdict reads
# ``affected``
# and the fault is disclosed BESIDE it — as ``gaps`` when it is a cell fault, and via
# ``_block``'s ``reason``, which is why that emit had to stop keying on the state token.
def _verdict(hits, gaps, completed):
    if hits:
        return _AFFECTED
    if gaps or not completed:
        return _COULD_NOT_SCAN
    return _NONE_AFFECTED


def scan_removal(system, lde, at):
    """Which live solves ON ROWS OTHER THAN ``at`` reference surface ``at``?

    PRE-mutation. NEVER raises. Returns::

        {"verdict": <one of VERDICTS>,
         "hits":    [{surface, surface_after, cell, table, solve_type, ref_field, source,
                      column, scale_factor, offset}, ...],
         "gaps":    [{surface|None, cell|None, table, reason}, ...],
         "not_audited": [{surface, cell|None, table, reason}, ...]}

    ``not_audited`` IS NOT A FAULT AND ``_verdict`` DOES NOT CONSUME IT. It carries the
    surfaces whose Par table this package has no catalog for (asphere / grating / GRIN),
    which is a statement about COVERAGE, not about a failed read. It shares ``_gap``'s
    four keys so every renderer and the wire-safety assert keep working unchanged; the
    channel, not the shape, is what distinguishes it.

    ``verdict`` is ``"affected"`` whenever ``hits`` is non-empty EVEN IF ``gaps`` is
    non-empty — AFFECTED DOMINATES, because a token that understated a known hit because
    something ELSE was unreadable would convert a fact into ignorance. ``none_affected``
    requires ``hits == [] and gaps == []`` — the CONJUNCTION.

    THE ROW ``at`` ITSELF IS SKIPPED. A solve ON the row being removed is destroyed by the
    removal of its OWN row, not by losing a referent, and the caller asked for that row to
    go; reporting it would put a row into ``hits`` that ``confirm_after`` cannot re-read
    (its post-index would name the unrelated SUCCESSOR row, whose type would then be
    published as the removed solve's consequence). Consequence: ``p != at`` holds for every
    hit BY CONSTRUCTION, which is what makes the confirm's index arithmetic total. It is a
    real reporting gap and is stated as one.

    PRE-MUTATION BY NECESSITY, NOT PREFERENCE: after the removal the solve reads ``Fixed``
    with all four pickup fields ``None`` (measured on both tables), so nothing is left to
    diff. A post-mutation "diff" would have to compare against a pre-state, which IS a
    pre-mutation scan under another name.

    An unreadable ``NumberOfSurfaces`` degrades the WHOLE scan, not one row — an audit that
    could not run must not be reported as one that passed.

    THE ACCUMULATORS ARE HOISTED ABOVE THE ``try``, AND THE VERDICT BESIDE THEM IS
    DERIVED, NOT RE-DECIDED. The hoist was the THIRD candidate site of that class and
    the one NOT reported by either audit: the handler returned literal ``[]``/``[]``, so a
    fault arriving after N rows had been scanned would have thrown their findings away.
    **It bred its own sibling.** Preserving the lists while leaving the handler's
    hard-coded ``_COULD_NOT_SCAN`` in place produced a return that contradicted itself —
    ``could_not_scan`` carrying a real hit — which a later review reproduced and rated a
    wire-contract violation, because ``disclosure`` published it as ``state ==
    "could_not_scan"`` beside a non-empty ``affected`` list. Both returns now call
    ``_verdict``; see its comment for why a third input (``completed``) is required and why
    synthesising a gap instead was rejected. The state partition is still DELIBERATELY NOT
    widened to a fourth shape: that is a contract change, not an audit fix.

    THE PATH IS UNREACHABLE TODAY AND IS CLOSED ANYWAY. With the per-arm guards in place
    (and the non-CB branch of ``cb_par_refs`` inside one) neither arm can raise,
    so the only reachable raise is the count read — before anything is accumulated, which
    is why the census row reverting the hoist is honestly INERT. "Unreachable given a guard
    added later" is a property of the current call graph, not of this function; the
    sibling above is what it cost to learn that the second time.
    """
    hits, gaps, unaudited = [], [], []
    try:
        n = int(lde.NumberOfSurfaces)
        if n < 2:
            raise ValueError("implausible surface count %r" % (n,))
        for surface in range(n):
            if surface == at:
                continue
            for arm in (geometry_refs, cb_par_refs):
                arm_hits, arm_gaps, arm_na = arm(system, lde, surface, at, n)
                hits.extend(arm_hits)
                gaps.extend(arm_gaps)
                unaudited.extend(arm_na)
    except Exception as exc:  # noqa: BLE001 — a total scan failure is a VERDICT, not a raise
        return {"verdict": _verdict(hits, gaps, False), "hits": hits, "gaps": gaps,
                "not_audited": unaudited,
                "reason": "the pre-mutation scan could not run (%s)" % (_cause(exc),)}
    return {"verdict": _verdict(hits, gaps, True), "hits": hits, "gaps": gaps,
            "not_audited": unaudited}


# Re-read ONE hit's cell at its POST-remove index. ``(type_after, ref_after)``.
#
# ``ref_after`` obeys the acceptance rule's REPRESENTATION half ONLY, and is DELIBERATELY
# NOT range-filtered: the scan's range gate is a DECISION about what can be interpreted,
# while this is a REPORT of what the reference NOW reads, and an out-of-range post-removal
# value is the strongest evidence a caller could get that the relationship is broken.
# Nulling it would rebuild the type-only proxy this re-read exists to replace.
#
# THE TWO READS ARE INDEPENDENT AND ARE READ INDEPENDENTLY — the same class again.
# They used to share ONE ``try``, and ``_confirm_one`` calls ``GetSolveData()``
# TWICE — once inside ``read_solve_type`` and once here for the reference — so a proxy that
# answered the first and wedged on the second discarded a SUCCESSFUL type read: the entry
# shipped ``type_after: null`` / ``consequence: "unmeasured"`` for a cell whose type had
# just been MEASURED ``"Fixed"``, i.e. a measured DELETION reported as unknown. The cell
# resolution stays in the first ``try`` because without a cell there is nothing to read;
# the reference read gets its own, so its failure costs only itself.
def _confirm_one(system, lde, entry):
    surface = entry["surface_after"]
    try:
        if entry["table"] == _GEOMETRY:
            cell = _sc.solve_cell(system, lde, surface, entry["cell"])
        else:
            cell = _cb._cb_cell(system, lde.GetSurfaceAt(surface), entry["cell"])
        type_after = _sc.read_solve_type(cell)
    except Exception:  # noqa: BLE001
        return None, None
    try:
        raw = _field(_sc.unwrap_or_none(cell.GetSolveData()), entry["ref_field"],
                     _UNREADABLE)
    except Exception:  # noqa: BLE001 — the reference half fails ALONE, keeping type_after
        raw = _UNREADABLE
    return type_after, (raw if is_integral_int(raw) else None)


def confirm_after(system, lde, at, scan):
    """Re-read ONLY the rows ``scan["hits"]`` named, at their POST-remove indices.

    ``{(surface_before, cell): (type_after, ref_after)}``. ZERO engine reads when ``hits``
    is empty — i.e. every call where nothing was at risk. NEVER raises, NEVER gates, and
    has NO rollback (``RemoveSurfaceAt`` has no inverse in this harness).

    THE REFERENCE FIELD IS RE-READ, NOT ONLY THE TYPE. The scan already knows WHICH field
    matched, so re-reading it costs one field read per hit — on the only rows that were
    ever at risk — and it is the difference between a measurement and a proxy. A retained
    ``"SurfacePickup"`` token proves nothing on its own: the capture records a live
    ``SurfacePickup`` whose ``Surface`` had been coerced to ``0`` with ``ScaleFactor``
    silently reset ``2.0 -> 1.0``, i.e. the exact shape of a type still standing while its
    relationship no longer holds.

    THE INDEX ARITHMETIC IS THE DEFINITION OF THE OPERATION, NOT AN INFERENCE:
    ``p_after = p - 1 if p > at else p``, directly measured on BOTH tables (the pickup row
    moved 5 -> 4 in both probe captures). There is no ``p == at`` case because
    ``scan_removal`` skips the row ``at`` — the two are one decision, made once.

    THE FAULT IS SCOPED TO ONE ENTRY. This is the FOURTH site of that same class, found by
    asking what the previous text PERMITTED rather than by a third report:
    the loop used to sit inside ONE ``try``, so a malformed entry (a missing
    ``surface_after``, an index arithmetic surprise) ABORTED the loop and every LATER hit —
    each of them individually confirmable — silently took ``_entry``'s ``(None, None)``
    default and shipped ``consequence: "unmeasured"``. It returned what it had measured so
    far, so it was never a false clean; it was the same conversion of knowledge into
    ignorance, one loop over.
    """
    out = {}
    try:
        hits = list(scan["hits"] or ())   # ``list`` so a non-iterable faults HERE, in scope
    except Exception:  # noqa: BLE001 — a malformed scan yields an empty confirm, not a raise
        return out
    for entry in hits:
        try:
            out[(entry["surface"], entry["cell"])] = _confirm_one(system, lde, entry)
        except Exception:  # noqa: BLE001 — one entry's fault may not discard the others
            continue
    return out


# THE CONSEQUENCE PARTITION: ORDERED, and TOTAL over ``type_after`` (which is ANY member
# the engine reports, so a three-case table would leave a fourth reachable).
#
#   1. the read FAILED                       -> "unmeasured"   (tested FIRST)
#   2. a NON-driving token                   -> "solve_removed"  measured, this call
#   3. driving AND == the authored type      -> "retained_type"  the TYPE is still there;
#                                               the RELATIONSHIP is not claimed intact
#   4. driving AND != the authored type      -> "unmeasured"   the RESIDUAL bucket
#
# Row 1 is tested first deliberately, and the two orderings happen to AGREE:
# ``is_driving(None)`` returns True (fail-closed), so an unreadable ``type_after`` reaching
# row 4 would also land on ``"unmeasured"``. Testing ``None`` first is still required,
# because the agreement is a property of THIS mapping and not of the predicate. It also
# dodges the trap ``is_driving``'s own docstring names: ``str(None)`` is the string
# ``"None"``, which IS in ``NON_DRIVING``, so a ``str()`` coercion anywhere in this
# derivation would publish an unreadable cell as a MEASURED removal.
#
# The two roads to ``"unmeasured"`` stay DISTINGUISHABLE on the wire — row 1 emits
# ``type_after: null``, row 4 emits a token that is not ``solve_type`` — so one token
# serves both without inventing a fourth vocabulary member for a difference already
# visible in the data.
#
# THIS IS NOT THE HIT PREDICATE. The hit predicate is the ``INDEX_FIELDS`` n
# ``_MEASURED`` intersection; this is the CONSEQUENCE classifier, over a token already in
# hand, after the mutation. One shared predicate, two questions — named as distinct so a
# later de-duplication sweep cannot merge them.
def _consequence(solve_type, type_after):
    if type_after is None:
        return _UNMEASURED
    if not _sc.is_driving(type_after):
        return _SOLVE_REMOVED
    return _RETAINED_TYPE if type_after == solve_type else _UNMEASURED


# One wire entry: the pre-mutation hit plus BOTH halves of the confirm.
def _entry(hit, confirmed):
    type_after, ref_after = confirmed.get((hit["surface"], hit["cell"]), (None, None))
    entry = dict(hit)
    entry["type_after"] = type_after
    entry["ref_after"] = ref_after
    entry["consequence"] = _consequence(hit["solve_type"], type_after)
    return entry


# The block itself, pre-wire-safety. The state partition is exhaustive, pairwise disjoint
# and ALARM-DOMINANT:
#
#   hits non-empty | any gaps  -> "affected"        affected present, unscanned iff gaps
#   hits empty     | no gaps   -> "none_affected"   affected ABSENT, unscanned ABSENT
#   hits empty     | gaps      -> "could_not_scan"  affected ABSENT, unscanned + reason
#
# ``par_refs_not_audited`` is ORTHOGONAL to all three rows and may appear beside
# any of them: it records coverage, not a finding, so it neither creates a state nor is
# excluded by one. It is absent when empty, like ``affected`` and unlike a falsy ``[]``.
#
# ``reason`` IS EMITTED WHENEVER THE SCAN CARRIES ONE, NOT ONLY IN THE ``could_not_scan``
# STATE. This is the THIRD site of that same class and it was found by asking what
# the ``_verdict`` fix PERMITS rather than by a report: once a scan that FAULTED can
# legitimately read ``affected`` (because a hit outranks the fault that followed it), an
# emit gated on ``state == "could_not_scan"`` silently drops the only record that the scan
# did not finish — a fact preserved by one fix and discarded by the stale decision beside
# it, which is exactly the shape the fix was closing. The ``could_not_scan`` arm keeps its
# synthesised fallback text; the new arm publishes the scan's own reason verbatim.
#
# ``affected`` is ABSENT outside the ``affected`` state — not ``[]``, not ``null``. That is
# ``emit_solves_block``'s own shipped rule, and ``[]``/``null`` are different wire bytes
# with different dict-``==`` behaviour. It closes the careless SUBSCRIPT read
# (``if not env["solve_refs"]["affected"]`` raises ``KeyError``); it does NOT close the
# class, because ``.get("affected")`` and a JS property access are both falsy. What carries
# the guarantee is the REQUIRED ``state`` token, which has no falsy member and cannot be
# reached by a truthiness test at all — and no prose may promote absence past that.
def _block(scan, confirmed):
    hits, gaps = scan["hits"], scan["gaps"]
    # ``.get``, not ``[...]``: a caller may hand ``_block`` a scan dict built before this
    # key existed (more than one producer is planned), and a KeyError here would
    # convert a disclosure gap into a wire failure.
    unaudited = scan.get("not_audited") or []
    block = {"state": scan["verdict"], "scope": SCOPE}
    if hits:
        block["affected"] = [_entry(h, confirmed) for h in hits]
    if gaps:
        block["unscanned"] = gaps[:_UNSCANNED_CAP]
        block["unscanned_count"] = len(gaps)
    if unaudited:
        # A DISTINCT NAME FROM THE READ DOOR'S ``par_cell_solves_not_audited``, and the
        # first cut got this wrong. It reused that spelling, arguing "an agent meets one
        # concept under one name" -- but they are not one concept, and two review
        # passes said so independently. The read door asks "was this surface's Par SOLVE
        # STATE inspected?"; this door asks "was this surface's Par table SEARCHED FOR
        # REFERENCES to the row being removed?". Measured on one system, the two make
        # OPPOSITE claims under the shared name for a coordinate break: the read door says
        # not-audited (True) while this door DID audit it and emits nothing. A shared name
        # whose two emitters disagree about the same surface is worse than two names.
        # Same defect as the docstring three functions up: a sentence about another
        # module's behaviour, carried across by analogy instead of re-derived.
        #
        # The SHAPE follows the door -- a list, because ``affected`` and ``unscanned`` are
        # lists and this scan is system-scope. NOT capped: it is bounded by the surface
        # count, unlike a fault channel a wedged engine could flood.
        block["par_refs_not_audited"] = unaudited
    if scan["verdict"] == _COULD_NOT_SCAN:
        block["reason"] = scan.get("reason") or (
            "the pre-mutation scan could not read %d cell(s); nothing can be concluded "
            "about solve references to this row" % (len(gaps),))
    elif scan.get("reason"):
        block["reason"] = scan["reason"]
    return block


# THE MINIMAL, GUARANTEED-WIRE-SAFE PROJECTION OF WHAT THE SCAN ESTABLISHED.
#
# It exists for ONE path: the disclosure could not be built, the removal HAS ALREADY
# HAPPENED and has no inverse, and the caller is therefore holding a mutated system with —
# before this — nothing but an existential ``could_not_scan`` and a sentence. A later
# review reproduced it: an ``affected`` scan plus a forced late wire failure returned a
# block naming the pre-mutation verdict and carrying NO ``affected`` and NO ``unscanned``
# entries at all. The row/cell/source identities are the only actionable thing left, and
# the removal has made them unrecoverable by any later call.
#
# WHY A PROJECTION AND NOT "SHIP THE BLOCK ANYWAY". The block FAILED ``assert_wire_safe``;
# that guard exists precisely to stop a non-wire-safe block reaching the wire, and
# defeating it would trade a disclosure defect for a protocol one. So this rebuilds the
# identities from primitives that cannot be non-finite or unrenderable — every value
# through ``_plain``, which COERCES NOTHING and never calls ``str()`` on a proxy — and the
# caller re-asserts wire safety on the result. It carries NO confirm half
# (``type_after``/``ref_after``/``consequence``) and no pickup extras: those are the values
# a late fault is most likely to be ABOUT, and a projection that reintroduced them would
# inherit the failure it exists to survive.
#
# IT IS NOT PUBLISHED UNDER ``affected``. The wire contract makes that key present IFF
# ``state == "affected"``, and an audit's OTHER finding was a block emitting that pair —
# so parking the identities there would close one finding by committing the other.
# ``state`` stays ``could_not_scan``: the normative rule governs the STATE (a post-mutation
# failure is REPORTED, never refused, and never retroactively re-verdicted), and this does
# not touch it. What that rule never licensed was discarding the identities alongside it.
def _established(scan):
    return {"affected_identities":
                [{"surface": _plain(h.get("surface")),
                  "surface_after": _plain(h.get("surface_after")),
                  "cell": _plain(h.get("cell")), "table": _plain(h.get("table")),
                  "solve_type": _plain(h.get("solve_type")),
                  "ref_field": _plain(h.get("ref_field")),
                  "source": _plain(h.get("source"))}
                 for h in scan["hits"]],
            "unscanned_identities":
                [{"surface": _plain(g.get("surface")), "cell": _plain(g.get("cell")),
                  "table": _plain(g.get("table")), "reason": _plain(g.get("reason"))}
                 for g in scan["gaps"][:_UNSCANNED_CAP]],
            "unscanned_count": _plain(len(scan["gaps"])),
            # THE COVERAGE RECORDS TRAVEL THE DEGRADED PATH TOO. Omitting them here was the
            # same discard this projection exists to prevent, one channel over: `SCOPE`
            # ships its coverage clause on EVERY envelope including this one, so a caller
            # was being told to read a key that is structurally absent whenever the block
            # fails to build. And this path runs POST-mutation, where re-scanning is
            # impossible — it is the worst place to drop a fact already in hand, not the
            # most acceptable one.
            #
            # THE QUOTE THIS COMMENT USED TO CARRY IS NOW STALE, AND THAT IS THE POINT
            # (0.1.6 review). It justified this list by quoting `SCOPE` as saying
            # "those surfaces are named in ``par_refs_not_audited``" — which is exactly the
            # half-truth the fix left behind: the records moved here, and the served
            # sentence went on naming only the key they moved OFF. `SCOPE` now names BOTH
            # (see its own comment), so this justification is paraphrased rather than
            # quoted — a comment that quotes a string it does not own drifts the moment
            # that string is corrected, which is what happened here.
            # Capped and counted like `unscanned`, so a truncation here cannot be silent.
            "not_audited_identities":
                [{"surface": _plain(u.get("surface")), "cell": _plain(u.get("cell")),
                  "table": _plain(u.get("table")), "reason": _plain(u.get("reason"))}
                 for u in (scan.get("not_audited") or [])[:_UNSCANNED_CAP]],
            "not_audited_count": _plain(len(scan.get("not_audited") or []))}


def disclosure(scan, confirmed):
    """The additive ``{"solve_refs": {...}}`` block. FRESH per call. NEVER raises.

    THE ONE ASYMMETRY, STATED RATHER THAN HIDDEN. There is a second, later way to reach
    ``could_not_scan``: the disclosure itself can fail to build AFTER the removal (a
    wire-safety failure, a confirm read wedging). So the strict gate — which reads the
    PRE-MUTATION verdict — and the final wire ``state`` do not accept the same set, and
    that cannot be fixed by making them agree: the gate must be pre-mutation (a refusal
    after ``RemoveSurfaceAt`` is not a refusal, there being no inverse), while the wire
    must report what actually happened.

    WHAT IS FIXED IS THE LOSS OF INFORMATION. On a post-mutation-only failure the
    ``reason`` NAMES THE PHASE and carries the pre-mutation verdict forward, so a strict
    caller who was correctly let through is never afterwards told "we do not know whether
    there were references" — it is told the scan was clean and the REPORT failed.

    AND THE DEGRADED BLOCK CARRIES WHAT THE SCAN HAD ALREADY ESTABLISHED. The reason
    alone is an existential statement; the removal has happened and cannot be re-scanned,
    so the row/cell/source identities are the caller's only remaining handle. They are
    published under ``established_before_the_failure`` — never under ``affected``, whose
    presence is reserved to its own state — as a MINIMAL projection re-asserted wire-safe
    in its own right (see ``_established``). THE FALLBACK STILL HAS A FALLBACK: if even the
    projection cannot be built or cannot be certified, the bare block ships exactly as
    before. Degrading is acceptable; degrading SILENTLY past a fact already in hand is the
    class these guards exist to close.
    """
    try:
        block = _block(scan, confirmed)
        _sc.assert_wire_safe(block)
    except Exception as exc:  # noqa: BLE001 — a wire failure is a REPORT, never a raise
        block = {"state": _COULD_NOT_SCAN, "scope": SCOPE,
                 "reason": "the pre-mutation scan completed with verdict %s; the "
                           "disclosure could not be built: %s"
                           % (_plain(scan.get("verdict")) if isinstance(scan, dict)
                              else None, _cause(exc))}
        try:
            established = _established(scan)
            _sc.assert_wire_safe(established)
            # THE THIRD DISJUNCT IS LOAD-BEARING, and its absence was a SECOND, independent
            # loss: a scan whose only finding was a coverage record built the projection,
            # certified it wire-safe, and then threw it away because the other two lists
            # were empty — leaving state + scope + reason alone. That is precisely the M1
            # design (one asphere, nothing else) meeting a post-mutation wire failure.
            if (established["affected_identities"] or established["unscanned_identities"]
                    or established["not_audited_identities"]):
                block[_ESTABLISHED] = established
        except Exception:  # noqa: BLE001 — the projection may not raise out of here either
            pass
    return {"solve_refs": block}


# ONE entry's refusal prose, with its re-author door routed on ``(table, solve_type)``.
#
# THE ROUTING IS ORDERED AND TOTAL, AND BOTH CONJUNCTS OF THE FIRST TIER ARE LOAD-BEARING.
# A sibling-family hit (``Position``, ``CenterOfCurvature``, ``Compensator``,
# ``CocentricRadius``, ``CocentricSurface``) is read by the GEOMETRY arm and so ALSO
# carries ``table == "geometry"``; branching on ``table`` ALONE swallows it into the pickup
# tier and hands a ``Position`` caller a recovery path for a disclosure that does not carry
# ``Sum``/``Length`` and cannot re-author it. That is precisely the
# remedy-names-a-door-that-fails defect, one family over.
#
#   geometry + SurfacePickup -> set_solve. It genuinely re-authors one from
#                               (surface, cell, Surface, Column, ScaleFactor, Offset),
#                               all of which the entry carries. A RECOVERY PATH.
#   cb_par                   -> NO door. ``set_solve`` refuses a Par cell on BOTH of its
#                               doors (the cell-token domain and the ``Column`` resolver),
#                               and BOTH now refuse from ONE shared refusal rather than
#                               only the ``Column`` one. The honest statement is that the
#                               refusal PREVENTED the loss. A RECORD.
#   anything else            -> the RESIDUAL. State the reference only, name no door.
#
# THE PICKUP TIER CARRIES THE PARAMETERS THAT DOOR NEEDS. It named
# ``set_solve`` and then withheld three of the four values ``set_solve`` takes: the hit
# dict ALREADY carries ``column``, ``scale_factor`` and ``offset`` (``_hit`` puts them
# there for the wire entry) and this prose rendered none of them, plus ``surface_after``,
# which is where the carrying row lands once the removal shifts it. The refusal RAISES, so
# there is no wire block to fall back on and this string is the caller's ONLY channel —
# naming a door while withholding what the door needs is the same
# remedy-that-cannot-be-walked defect the routing above exists to prevent, one field-set
# over.
#
# THE PARAMETERS ARE RENDERED IN THEIR OWN ``try`` AND APPENDED TO AN ALREADY-BUILT
# ``where``. In production every one of them is already wire-safe (``column`` through the
# guarded ``to_wire``, the two numerics through ``_wire_num``), but a hit reaching here is
# whatever the scan dict carries, and rendering a hostile one inside the SAME expression as
# ``where`` would let a decorative parameter destroy the established identity — the exact
# class closed at five other sites. So a parameter that cannot be rendered costs the
# PARAMETERS, never the hit.
#
# ``type_after`` / ``ref_after`` / ``consequence`` ARE DELIBERATELY ABSENT. This prose is
# PRE-mutation: nothing was removed, so nothing was measured, and stating them here would
# be a merge of the reference FACT with a CONSEQUENCE that does not exist yet.
#: What ``set_solve`` will accept for a pickup parameter this scan could not put a NUMBER
#: on. NOT a value -- a short instruction, because there IS no value to give.
_REPLAY_NULL = ("NOT AVAILABLE from this scan -- either this cell does not support the "
                "parameter or its value could not be read, and this refusal CANNOT TELL "
                "WHICH; set_solve requires all four fields and refuses a non-finite one, "
                "and 0.0 is its documented no-op, but pass 0.0 ONLY after confirming from "
                "the live cell that there is no value, because substituting it for a real "
                "one re-authors a DIFFERENT relationship")


def _replay(hit, name):
    """The parameter as the caller must PASS IT BACK, never as the wire happened to carry it.

    This closes the FIFTH instance of ONE class: *a remedy naming a door the caller cannot
    walk through.* An earlier fix closed the half where the refusal WITHHELD the parameters
    ``set_solve`` needs. That fix then printed them verbatim -- and on a ``radius`` /
    ``conic`` / ``semi_diameter`` pickup that prints ``Offset=None``, which ``set_solve``
    REJECTS: *"Offset must be a finite number; got None"* (``surface_solve``'s
    guard in the raw-field validator, ~:1596 -- ANCHORED ON THE MESSAGE, because the earlier
    citation here read ``:1200-1204`` and had rotted onto unrelated float-precision prose).
    Handing a
    finite-number guard). Handing a caller a value the door refuses is the same defect as
    handing them nothing, one field over -- and it was invisible to that fix's test, which
    used a ``thickness`` pickup, the ONE cell where ``Offset`` is supported and finite.

    WHY A PHRASE AND NOT ``0.0``. A ``None`` here has TWO provenances -- the cell does not
    SUPPORT this parameter (measured: ``Offset`` is supported only on ``ThicknessCell``,
    ``ScaleFactor`` on every cell but ``MaterialCell``; unsupported reads back ``NaN`` and
    the wire guard emits ``null``), or the read FAILED (``_field`` returns its default on
    ANY exception, so a wedged property is indistinguishable here from an absent one).
    The ABSENT-vs-UNREADABLE rule forbids collapsing those, and this function cannot tell
    them apart from the wire
    value alone. Printing ``0.0`` would be right for the first and a FABRICATED
    measurement for the second.

    **AND THE FIRST ATTEMPT AT THIS PHRASE MADE EXACTLY THAT MISTAKE -- the SIXTH instance
    of this module's one recurring class, committed inside the fix for the fifth.** The
    first wording was *"this cell reads it back as NaN"*, which is TRUE for an unsupported
    parameter and FALSE when the read failed; an independent review named the consequence
    precisely: on a SUPPORTED ``thickness`` cell whose real non-zero ``Offset`` was
    unreadable, a caller following the advertised ``0.0`` re-authors a valid but
    NON-EQUIVALENT relationship. Worse, the row written to forbid provenance claims
    REQUIRED the offending word (``assert "NaN" in text``) -- one author wrote the phrase
    and its guard, so the guard enforced the mistake. **A test and the thing it checks must
    not share an author's single reading; that is what an independent review is for.**

    The phrase now NAMES BOTH possibilities and asserts neither, and the ``0.0`` advice
    carries its PRECONDITION rather than being offered unconditionally -- because the
    dangerous case is not the caller who cannot re-author, it is the one who re-authors
    successfully into something else.

    Distinguishing them properly needs the ``_MEASURED`` ``Supports*`` capability flags,
    which are deliberately PRIVATE to the substrate. That is a real improvement and a real
    coupling decision, so it is deferred rather than taken here.

    **``column`` IS THE THIRD SIBLING, AND THE FIX LANDED ON TWO OF THREE (0.1.7 internal
    review, finding 4 — the SIXTH instance of this class in this
    lineage).** ``_remedy``
    applied this helper to ``scale_factor`` and ``offset`` and printed ``hit.get("column")``
    RAW, so a null column rendered ``Column=None`` and ``set_solve`` REJECTS it:
    *"Column takes a SurfaceColumn member name or a cell token; got None"*
    (``surface_solve.py``'s *"Column takes a SurfaceColumn member name or a cell token"*
    guard, ~:1562 -- the earlier ``:1541-1543`` citation had rotted). Reachable on the
    ``pickup column unrenderable``
    provenance, which is PRE-EXISTING and not something the 0.1.7 pickup work introduced.
    ``test_r5_replay_is_used_for_every_pickup_parameter_that_can_be_null`` was GREEN with
    the defect present because its universe was the two fields it happened to know about —
    an enumeration where the claim was universal. It now derives that universe, and an AST
    row forbids a raw ``hit.get(...)`` of ANY nullable pickup parameter inside this render.

    **KNOWN RESIDUAL, stated because a caller ACTS on this text.** ``_REPLAY_NULL`` is
    worded for the two NUMERIC parameters: its *"refuses a non-finite one, and 0.0 is its
    documented no-op"* clause is not advice a ``Column`` caller can follow — ``Column``
    takes a member NAME, and there is no zero-equivalent. Both halves that DO apply are
    still correct for it (the value is not available from this scan, and substituting a
    guess re-authors a different relationship), and serving a numeric-flavoured phrase is
    strictly better than serving a value the door rejects. A column-specific phrase costs a
    module-level constant (+1 statement) against a ceiling pinned by EQUALITY at 258, so
    it is DISCLOSED here rather than trimmed in, and ticketed.
    """
    value = hit.get(name)
    return _REPLAY_NULL if value is None else value


def _remedy(hit):
    where = ("surface %s %s (%s) carries a %s solve whose %s references surface %s"
             % (hit["surface"], hit["cell"], hit["table"], hit["solve_type"],
                hit["ref_field"], hit["source"]))
    if hit["table"] == _GEOMETRY and hit["solve_type"] == _PICKUP:
        try:
            params = (" (that row becomes surface %s after the removal; the solve's "
                      "set_solve parameters are Column=%s, ScaleFactor=%s, Offset=%s, and "
                      "its Surface reference is the row being removed, so a surviving row "
                      "must be named in its place)"
                      % (hit.get("surface_after"), _replay(hit, "column"),
                         _replay(hit, "scale_factor"),
                         _replay(hit, "offset")))
        except Exception:  # noqa: BLE001 — a parameter may cost the PARAMETERS, not the hit
            params = " (its set_solve parameters could not be rendered)"
        return where + " - re-author it with set_solve once the removal has been made" + params
    if hit["table"] == _CB_PAR:
        return where + (" - NO public re-author door exists for a coordinate-break "
                        "parameter cell, so this "
                        "refusal is the only prevention")
    return where + (" - the disclosure records the reference only and does not carry "
                    "that solve family's other parameters, so it is not a re-author path")


def refusal_message(at, scan):
    """The strict-mode refusal prose. NEVER raises.

    Names each referencing solve (row, cell, table, solve type, reference field, source)
    and THE RE-AUTHOR DOOR PER ENTRY — not per table (see ``_remedy``).

    THE REFUSAL RAISES, so this prose is the only channel that reaches the caller — a known
    limit: there is no machine-readable detail on the refusal path.

    IT CARRIES THE HITS **AND** THE UNKNOWNS, NEVER ONE INSTEAD OF THE OTHER. The previous
    text was ``detail or (unknown + ...)``, so a non-empty ``detail`` DISCARDED the
    unscanned count — and the caller who set ``refuse_on_solve_refs=true`` is exactly the
    one who has "asked for UNKNOWN to be treated as alarm". The wire block already
    discloses both (``affected`` beside ``unscanned``); this channel now matches it,
    because a refused caller has NO wire block to fall back on.

    THE FIFTH SITE OF THE SAME CLASS is here too: ``"; ".join(_remedy(h) for h in ...)``
    put every remedy inside ONE generator, so a single undescribable hit (a proxy whose
    ``__str__`` throws inside ``_remedy``'s ``%s``) emptied ``detail`` and dropped EVERY
    hit, falling through to the unknown clause. The loop below describes them one at a
    time, and a hit it cannot render is still COUNTED and named as present.

    THE UNKNOWNS ARE NOW STRUCTURALLY INDEPENDENT, WHICH IS NOT THE SAME AS GUARDED.
    An earlier version built both unknowns in ONE list-comprehension inside ONE ``try``:
    tuple evaluation is left-to-right and ABORTS, so a ``reason`` that could not render
    stopped the second element — the INDEPENDENTLY KNOWN gap count — from ever being
    constructed, and the outer handler then replaced both with a generic string. That is
    the same class closed twice before at this same helper, so a third point-fix (widen
    the render guard) was refused: it would leave "one erases the other" EXPRESSIBLE and
    merely unreached.

    The shape below is the one the hits loop uses, applied to the unknowns:
    ONE accumulator, and each piece appended by its OWN step that reads no other step's
    output. A step that faults appends its own clause and cannot unwind a clause already in
    the list, so the erasure is not avoided — it is not constructible. The gap count is then
    derived from a list THIS function built, so it needs no handler at all and does not get
    a decorative one (an unfalsifiable guard reads as INERT).
    """
    parts, told, hits, gaps, unaudited = [], [], [], [], []
    # STEP 1 — the hits. A fault here costs the hits, and says so.
    try:
        hits = list(scan["hits"] or ())
    except Exception:  # noqa: BLE001
        told.append("the referencing solves could not be read from the scan")
    # STEP 2 — the gap records. Independent of step 1: a scan that could not yield its
    # hits may still yield its gaps, and vice versa.
    try:
        gaps = list(scan["gaps"] or ())
    except Exception:  # noqa: BLE001
        told.append("the unscanned-cell records could not be read from the scan")
    # STEP 3 — the scan's own reason. RENDERED INSIDE THE ``try``: ``reason`` is
    # whatever the scan dict carries, and the concatenation at the end of this function is
    # outside every handler, so a non-string would raise out of a function documented NEVER
    # to. THE TRUTHINESS TEST IS INSIDE THE GUARD TOO: ``if scan.get("reason")`` calls
    # ``__bool__``, which a hostile proxy raises from just as readily as ``__str__``.
    try:
        if scan.get("reason"):
            told.append("%s" % (scan.get("reason"),))
    except Exception:  # noqa: BLE001
        told.append("the scan's own reason could not be read")
    # STEP 4 — the count. NO GUARD, DELIBERATELY: ``gaps`` is a ``list`` this function
    # built, so ``len`` and the truthiness cannot raise, and a handler that cannot be
    # reached is a guard that cannot be falsified.
    #
    # A TOTAL scan failure carries its own reason and has ZERO gaps, so an UNCONDITIONAL
    # count would report "0 cell(s) could not be scanned" for a scan that did not run at
    # all -- an understatement in exactly the direction this tool exists to avoid.
    if gaps:
        told.append("%d cell(s) could not be scanned" % (len(gaps),))
    # STEP 5 — THE COVERAGE RECORDS, ADDED ONE REVIEW AFTER THE CHANNEL ITSELF.
    # A REFUSED CALLER HAS NO WIRE BLOCK TO FALL BACK ON — this string is the whole of
    # what they get — so omitting the channel meant the one caller who explicitly asked
    # for unknowns to be surfaced was the only caller told nothing about the unaudited
    # tables. That is the class this function's own docstring says it closed ("IT CARRIES
    # THE HITS **AND** THE UNKNOWNS, NEVER ONE INSTEAD OF THE OTHER"), reappearing one
    # channel over the moment a third channel existed.
    #
    # ITS OWN STEP, reading no other step's output, exactly like steps 1-3: a fault here
    # costs this clause and cannot unwind one already appended.
    try:
        unaudited = list(scan.get("not_audited") or ())
    except Exception:  # noqa: BLE001
        told.append("the not-audited records could not be read from the scan")
    if unaudited:
        told.append("%d surface(s) carry a parameter table this package does not audit, "
                    "so whether THEY reference the row is not known" % (len(unaudited),))
    # Every element was appended as a rendered ``str``, so the join cannot raise either.
    unknown = "; ".join(told) or None
    for hit in hits:
        try:
            parts.append(_remedy(hit))
        except Exception:  # noqa: BLE001 — an undescribable hit is still a DISCLOSED hit
            parts.append("a solve on a surviving row references surface %s but could not "
                         "be described" % (at,))
    detail = "; ".join(parts)
    if detail and unknown:
        detail += ("; and additionally " + unknown + ", so whether any OTHER solve "
                   "references this row is UNKNOWN")
    elif not detail:
        detail = ((unknown or "the scan could not be completed")
                  + ", so whether any solve references this row is UNKNOWN")
    return (
        "refusing to remove surface %s with refuse_on_solve_refs=true: %s. No surface was "
        "removed and nothing was mutated. Pass refuse_on_solve_refs=false (the default) to "
        "remove it anyway; the solve_refs disclosure then carries these references in "
        "full, every cell the scan could not read, and the measured post-removal "
        "consequence of each reference." % (at, detail)
    )
