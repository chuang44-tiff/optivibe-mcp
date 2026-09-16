"""The JUDGMENT record — an agent's ballpark call, bound to the bytes it judged.

V-INT Part 2.

WHY THIS EXISTS. Part 1 stopped the ceiling arm adjudicating: an over-ceiling gap now
books `REPORTED`, the audit publishes the measured centre / the stated limit / their
separation, and the AGENT makes the call. That is two of the contract's three layers. This
is the third — **the call is RECORDED, with its reason, against the exact bytes.**

It is ALSO the half that never depended on the oracle at all, and that half
is measured rather than argued: **ten designs were force-promoted and not one records
why.** `promote_best` stamps `forced` in the RETURNED ENVELOPE ONLY; it never reaches
disk. The reason was never even asked for.

▶ THIS MODULE IS PURE. No file IO, no engine, no import from `workspace`. It owns the
  VOCABULARY and the PREDICATES; `workspace` owns the IO and injects these. That split is
  not tidiness: `workspace.py` carries an OPEN size escalation from an earlier cycle
  artifact-identity cycle (+43.7 % statements, escalation still owned by a future split),
  so new machinery lands beside it rather than inside it. It also makes every rule here
  testable without a manifest, a session or a seat.

THE ACCEPTANCE SET IS ONE BODY. `validated_row` is used by the WRITER to refuse its
own row before it lands and by the READER to admit one. A row this writer cannot get past
this reader is evidence DEAD ON ARRIVAL — an earlier cycle shipped exactly that
defect and then served a remedy pointing the user at their filesystem while the manifest
was intact.

WHAT A JUDGMENT IS NOT. It is not a verdict, not a gate, and not an authorisation. Nothing
reads it to decide anything, and that is deliberate: the contract states the cost in terms —
this trades a mechanical guarantee for a recorded judgment, and a recorded judgment is
WEAKER, because it can be wrong and the record only tells you afterwards. It was accepted
on the evidence that the gate being given up had a net lifetime effect of ZERO blocked
designs. **A recorded reason is strictly more than nothing, which is what is there now.**

▶ VISION<->DESIGN CONTRACT, PHASE 2 STEP 2.
  Three edits and one receipt field, all of them about the SAME new fact: a judgment can
  now be a RESPONSE to a recorded vision finding, and a response has to say WHAT KIND of
  response it is.

  1. ``disposition`` — one of :data:`DISPOSITIONS`, REQUIRED iff ``finding_ids`` is
     non-empty and FORBIDDEN when it is empty. A disposition with nothing to dispose is
     malformed; the empty-ids path — the force-promote-with-no-findings judgment this
     file already calls *"an ordinary, legitimate judgment"* — is byte-identical to what
     it was, carries no new field and calls no resolver.
  2. ``conflict_key`` gains it IN THE SAME EDIT. Not as a follow-up, not as a later
     cycle: the invariant below has been broken three times in the sibling file and
     every one of those breaks was a field that got authority in one edit and entered
     the key in another.
  3. ``resolve_ids`` — a SECOND injected predicate on :func:`normalize_request`, no
     default. Until now ``finding_ids`` were free strings with nothing for them to point
     at; this is what makes them point.

  **The gate NEVER reads ``disposition``**. Recording it is
  disclosure; gating on it would be a self-certified token.
"""

JUDGMENT_EVENT = "judgment"
JUDGMENT_SCHEMA = 1

# The read states a judgment receipt may report. FROZEN.
#
# ▶ FIVE, NOT THE FOUR the contract FROZE, and the fifth is a DELIBERATE
#   extension recorded rather than smuggled. The spec's schema 4 lists
#   `absent | unreadable | digest_mismatch`. A CONFLICT — two validated rows for the same
#   bytes that disagree — is none of those, and folding it into `unreadable` would report
#   "I could not read it" when the truth is "I read two rows and they disagree". That is
# The rule that ABSENT is not UNREADABLE one token over, and a sibling of this file shipped
#   the harm it causes: a remedy that sends the user to their filesystem while the
#   manifest is perfectly intact. `conflicting` NAMES the state, and like every other
#   non-`ok` state it fails closed.
READ_STATES = ("ok", "absent", "unreadable", "digest_mismatch", "conflicting")

# The four disposition tokens. FROZEN, and the vocabulary is a
# SPEC-PHASE CHOICE stated as one: four, not five (B's `dropped_unreadable_stamp` is cut
# — a finding keyed on an unresolvable stamp is RECORDED as the eye stated it and
# dispositioned `declined` with the stamp named in the reason), and not three (C's
# `rejected`/`referred` lacks the token the design-scoped backlog needs).
#
# `superseded` is the one that has to exist: under the design-scoped ruling a finding
# survives the bytes it was made about, so the backlog needs its own token or it is
# indistinguishable from a real decline, and the boilerplate rate nobody has cannot be
# measured from the manifest. A FIFTH token is a schema change with its own edit to
# :func:`conflict_key`.
DISPOSITIONS = ("acted", "declined", "superseded", "referred")

# The refusal family. One family for a MALFORMED REQUEST, because every rejection of that
# kind is the caller's request being malformed — there is no engine and no filesystem in
# this module to blame.
JUDGMENT_PARAM = "judgment_param"

# ▶ THE SECOND FAMILY, and the one-family rule above is exactly why it is needed
#   The INJECTED `resolve_ids` reader HAS a filesystem. When it
#   answers ``None`` the request may be perfectly well-formed and the record it must
# point at could not be READ — one level out: an unreadable manifest is not "no
#   findings", and reporting it as `judgment_param` would send an author with a valid
#   judgment to re-type their own ids.
JUDGMENT_UNRESOLVABLE = "judgment_unresolvable"


def _nonempty_str(value):
    """True iff ``value`` is a ``str`` with at least one non-whitespace character.

    POSITIVELY DEFINED, and `if not value:` is BANNED here — it collapses `""`, `0`,
    `False`, `[]` and `None` into one answer, so a caller passing the integer `42` as a
    finding id would be refused for "being empty". The spec names this trap by hand;
    it is enforced by construction instead.
    """
    return isinstance(value, str) and value.strip() != ""


def normalize_request(block, *, writable, resolve_ids):
    """Validate a caller's ``judgment`` block. -> ``(normalized, error, family)``.

    Never raises, EXCEPT out of the injected ``resolve_ids`` — an injected callable that
    raises is the injector's defect, not this module's data handling, and this module has
    no `try` to hide it in.

    ▶ **`resolve_ids` IS REQUIRED AND HAS NO DEFAULT**, for the
      reason ``writable`` records below in terms: *a carrier parameter with a default is
      not a carrier*. ``resolve_ids(ids) -> list[str] | None`` answers the ids NOT carried
      by any validated `finding` row in scope, or ``None`` when the manifest could not be
      read. It is CALLED ONLY when ``finding_ids`` is non-empty: on the empty path there
      is nothing to resolve, and calling it anyway would make an unreadable manifest
      refuse a judgment that names no findings at all.

    ▶ **THE RETURN IS A THREE-TUPLE, and the widening is a SIGNATURE DEVIATION from the
      spec, reported rather than smuggled.** the contract mandates a SECOND refusal family
      (:data:`JUDGMENT_UNRESOLVABLE`) and then names no channel to carry it; a caller
      cannot pick the family off a message string without parsing prose. ``family`` is
      ``None`` on success and one of the two family constants on every refusal.

    THE ``writable`` PREDICATE IS REQUIRED AND HAS NO DEFAULT (a review M-1).

    It is the caller's own manifest-encodability predicate, INJECTED for the same reason
    ``exact_int`` and ``is_hex64`` are: this module must not hold a second copy of a rule
    the writer owns. A DEFAULT is refused deliberately -- a default is exactly how the
    omission it guards against gets acquired silently, and a sibling cycle already wrote
    that rule down (a carrier parameter with a default is not a carrier).

    **What it caught.** ``isinstance(str) and .strip()`` is a check on the OBJECT; what
    the record promises is a WRITE. Those come apart on a **lone surrogate** -- codepoint
    U+D800, a perfectly legal ``str``, emitted verbatim by
    ``json.dumps(ensure_ascii=False)`` and unencodable by the manifest's UTF-8 writer. On
    an OVERRIDING ``force=True`` promote the reason gate saw a non-empty string and
    passed, the BEST copy happened, the writer died ``not_written:UnicodeEncodeError``,
    and the promote still returned ``ok: true``. **A caller-controlled value completed a
    real override with no durable reason** -- precisely what this record exists to stop.

    ``workspace._writable_name`` already existed for this exact defect, in the same file,
    with a docstring that says *"a check on the OBJECT is not a check on the WRITE, and
    the two came apart on a lone surrogate"*. The lesson was learned there and not
    carried to this writer. It is carried now rather than re-derived.

    Applied to the ids as well as the reason: a surrogate in a ``finding_id`` kills the
    same write, and refusing only the reason would leave the identical hole one field
    over -- the sibling this programme keeps finding.

    Exactly one of the two is ``None``. ``normalized`` is
    ``{"finding_ids": [...], "reason": "..."}`` with the ids CANONICALISED — deduped and
    sorted — and the reason stored **VERBATIM**.

    WHY CANONICALISE THE IDS. Two calls naming the same findings in different orders must
    produce the SAME record, or `conflict_key` reports a conflict between two rows that
    agree. Sorting makes set-equality the comparison it looks like.

    WHY NOT THE REASON. The reason is the author's own words and is never rewritten — not
    stripped, not case-folded. The cost is stated rather than hidden: `"converged"` and
    `"converged "` are different records and WOULD conflict. That is the fail-closed
    direction (a conflict refuses; a silent merge would pick one author's words over
    another's), and it is reachable only by two separate judgments on the same bytes.

    `reason` HAS NO DEFAULT. Absent, non-string and whitespace-only are all refusals —
    the whole point of the record is that somebody said why.
    """
    if not isinstance(block, dict):
        return None, ("judgment must be an object with a `reason` and optional "
                      "`finding_ids`; got %r" % (type(block).__name__,)), JUDGMENT_PARAM

    unknown = sorted(k for k in block
                     if k not in ("finding_ids", "reason", "disposition"))
    if unknown:
        # A typo'd key is a judgment the caller believes they recorded and did not.
        return None, ("judgment carries unknown key(s) %s; the only keys are "
                      "`reason`, `finding_ids` and `disposition`"
                      % (unknown,)), JUDGMENT_PARAM

    reason = block.get("reason")
    if _nonempty_str(reason) and not writable(reason):
        # DISTINCT from the empty-reason refusal below: "you said nothing" and "we
        # cannot write down what you said" have different remedies, and collapsing them
        # would tell an author with a real reason that they gave none.
        return None, ("judgment.reason cannot be encoded by the manifest writer (a lone "
                      "surrogate or similar): it is a legal str whose UTF-8 write would "
                      "fail, so accepting it would report success for a row that never "
                      "landed. Re-state the reason in encodable text"), JUDGMENT_PARAM
    if not _nonempty_str(reason):
        return None, ("judgment.reason is REQUIRED and must be a non-empty string "
                      "(whitespace alone is not a reason); got %r. A judgment with no "
                      "stated reason is the exact thing this record exists to stop: ten "
                      "designs were force-promoted and not one records why"
                      % (reason,)), JUDGMENT_PARAM

    raw = block.get("finding_ids", [])
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        return None, ("judgment.finding_ids must be a list of non-empty strings; "
                      "got %r" % (type(raw).__name__,)), JUDGMENT_PARAM
    bad = [x for x in raw if not _nonempty_str(x)]
    if bad:
        return None, ("judgment.finding_ids must be non-empty strings; these are not: "
                      "%r" % (bad[:5],)), JUDGMENT_PARAM
    unwritable = [x for x in raw if not writable(x)]
    if unwritable:
        return None, ("judgment.finding_ids contains id(s) the manifest writer cannot "
                      "encode: %r. The same hazard as an unencodable reason, one field "
                      "over" % (unwritable[:5],)), JUDGMENT_PARAM

    ids = sorted(set(raw))

    # THE IFF RULE, both arms, in one place. `"disposition" in block` is asked
    # POSITIVELY and never through `block.get(...)` truthiness: a caller who sent
    # `disposition: None` said something, and it is not the same as saying nothing.
    if not ids:
        if "disposition" in block:
            return None, ("judgment.disposition is FORBIDDEN when finding_ids is empty: "
                          "a disposition with nothing to dispose is malformed. Either "
                          "name the finding id(s) it disposes of, or drop the field"
                          ), JUDGMENT_PARAM
        # BYTE-IDENTICAL TO HEAD on this path: no new field, and the resolver is not
        # called — there is nothing to resolve, and an unreadable manifest must not
        # refuse a judgment that names no findings.
        return {"finding_ids": [], "reason": reason}, None, None

    if "disposition" not in block:
        return None, ("judgment.disposition is REQUIRED when finding_ids names a "
                      "finding: it is the field that says WHAT KIND of response this "
                      "is. One of %s" % (list(DISPOSITIONS),)), JUDGMENT_PARAM
    disposition = block["disposition"]
    if disposition not in DISPOSITIONS:
        return None, ("judgment.disposition must be one of %s; got %r"
                      % (list(DISPOSITIONS), disposition)), JUDGMENT_PARAM

    # Only now, and only here. Shape first: a malformed request should not cost a
    # manifest read, and the resolver's answer is meaningless about a block that is
    # going to be refused anyway.
    unresolved = resolve_ids(ids)
    if unresolved is None:
        return None, ("judgment.finding_ids could not be resolved: the candidate "
                      "manifest holding the finding records could not be read, so "
                      "whether these ids name anything is UNKNOWN. This is not the same "
                      "as their naming nothing. Remedy: the manifest, not the request"
                      ), JUDGMENT_UNRESOLVABLE
    if unresolved:
        return None, ("judgment.finding_ids names id(s) no recorded finding carries: "
                      "%r. An id with nothing to point at records a response to nothing; "
                      "record the finding with `record_findings` first, or correct the id"
                      % (list(unresolved)[:5],)), JUDGMENT_PARAM

    return ({"finding_ids": ids, "reason": reason, "disposition": disposition},
            None, None)


def build_row(*, seq, design_name, filename, zmx_sha256, finding_ids, reason, ts,
              disposition=None):
    """The durable row, exactly as the contract schema 2 declares it.

    ONE construction site, so the writer cannot drift from what `validated_row` admits.

    THE IFF RULE: the key is present iff ``finding_ids`` is
    non-empty. On the empty path the row is BYTE-IDENTICAL to what it was before this
    edit — no key, whatever ``disposition`` was passed.

    ▶ ``disposition`` DEFAULTS TO ``None``, and that is a DEVIATION from the step-2
      handover diff, recorded rather than smuggled. That diff gave it no default on the
       Argument that a caller who forgot it would write a row this module's own
      :func:`validated_row` then refuses. The argument is sound and the outcome is
      UNCHANGED by the default — a forgotten ``disposition`` on the non-empty path
      produces ``disposition: None``, which :func:`validated_row` refuses just as loudly,
      and the ONE call site (`workspace._write_judgment_record`) validates its own row
      before writing. What the default buys is that a signature the spec does not
      constrain stops breaking
      three shipped tests that call this function positionally on the old shape. A
      strictness the spec does not ask for is not worth a red shipped suite.
    """
    ids = list(finding_ids)
    row = {
        "event": JUDGMENT_EVENT,
        "schema": JUDGMENT_SCHEMA,
        "seq": seq,
        "ts": ts,
        "design_name": design_name,
        "filename": filename,
        "digest_algo": "sha256",
        "zmx_sha256": zmx_sha256,
        "finding_ids": ids,
        "reason": reason,
    }
    if ids:
        row["disposition"] = disposition
    return row


def row_targets(row, seq, filename, *, exact_int, design_name):
    """True iff ``row`` is a judgment row FOR this ``(design_name, seq, filename)``.

    ▶ ``design_name`` WAS ADDED BY a review (H-3), and its absence
      was the B12b defect one field over -- inside the function B12b was written to
      protect. The frozen identity is a FIVE-tuple; the selection compared TWO of it.
      `_judgment_receipt` accepted a `design_name`, never compared it to the row's, and
      passed the CALLER'S value straight into the receipt -- so a row recorded under one
      design was reported as belonging to whichever design happened to ask, with
      ``read_state: "ok"``. A receipt that echoes the request in ANY field reports
      something it did not read.

      With this compared and the digest re-bound against disk, the effective identity is
      the full five: ``zmx_dir`` is the manifest being read, ``design_name`` / ``seq`` /
      ``filename`` are compared here, and ``zmx_sha256`` is re-bound by the caller.

    ``filename`` is compared by RAW string equality, deliberately: the caller side is an
    already-resolved basename, and NOT basenaming the record side is what makes
    ``../0005_x.zmx`` and absolute paths FAIL rather than silently match. an earlier cycle sibling
    learned this the hard way — its rev-1 rule basenamed the record side, i.e. the
    validator performed its own test's mutation.

    ``exact_int`` is INJECTED rather than re-implemented so this module and `workspace`
    cannot disagree about whether ``True`` is the integer 1 (it is, in Python, and a
    ``seq`` of ``True`` matching seq 1 is a real hazard).
    """
    if not isinstance(row, dict) or row.get("event") != JUDGMENT_EVENT:
        return False
    row_seq = row.get("seq")
    if not exact_int(row_seq) or not exact_int(seq) or row_seq != seq:
        return False
    if row.get("design_name") != design_name:
        return False
    return row.get("filename") == filename


def validated_row(row, *, exact_int, is_hex64):
    """True iff ``row`` passes EVERY clause. A row failing ANY is not a record.

    THE ONE ACCEPTANCE SET — the writer refuses its own row with this, the reader
    admits with this. Every subscript any consumer performs is a key made REQUIRED here.

    The predicates are INJECTED for the same reason as in `row_targets`: `_exact_int` and
    `_is_hex64` are already shipped and already mutation-covered in `workspace`, and a
    second copy of "what is a sha256" is exactly the dead-constant drift this programme
    keeps finding (`schema.CEILING_STATES`, `schema.UNRESOLVED_PROVENANCE`).
    """
    if not isinstance(row, dict):
        return False
    schema = row.get("schema")
    if not exact_int(schema) or schema != JUDGMENT_SCHEMA:
        return False
    # EXACT: a "blake3" row is NEVER re-interpreted as sha256.
    if row.get("digest_algo") != "sha256":
        return False
    if not is_hex64(row.get("zmx_sha256")):
        return False
    if not _nonempty_str(row.get("design_name")):
        return False
    if not _nonempty_str(row.get("filename")):
        return False
    if not _nonempty_str(row.get("reason")):
        return False
    if not _nonempty_str(row.get("ts")):
        return False
    ids = row.get("finding_ids")
    # PRESENT and a list. An EMPTY list is VALID and is not the same as an absent key:
    # a force-promote with no vision findings at all is an ordinary, legitimate judgment.
    if not isinstance(ids, list):
        return False
    if any(not _nonempty_str(x) for x in ids):
        return False
    # THE SAME IFF RULE THE WRITER APPLIES. Asked in BOTH directions on purpose:
    # a row naming findings with no disposition is a response that never says what kind
    # of response it is, and a row naming NO findings while carrying one is a token with
    # nothing to dispose. Either is a row :func:`normalize_request` cannot produce, and a
    # reader that admitted one would be the wider-than-the-writer divergence this file's
    # own header calls evidence DEAD ON ARRIVAL.
    if ids:
        if "disposition" not in row:
            return False
        if row.get("disposition") not in DISPOSITIONS:
            return False
    elif "disposition" in row:
        return False
    return True


def judgment_subject(row):
    """WHAT this row judges, as a comparable tuple. The row's SUBJECT.

    ▶ SPLIT OUT OF ``conflict_key`` BY a review (H-2).

    The original key was ``(finding_ids, reason)`` -- subject AND claim together -- and
    any two rows differing in either were declared a conflict. That is right for a
    ``candidate_audit`` row, which is a SINGLETON statement of record about some bytes:
    two that disagree means one is wrong, so refuse.

    **A judgment row is not a singleton, and the spec's own two producers prove it.**
    ``save_candidate`` writes a ballpark call on a REPORTED ceiling row; then
    ``promote_best(force=True)`` writes a different judgment, about a different finding,
    on the SAME bytes. Both are true. Under the old key they collided, and the successful
    promote's receipt reported no recorded ids at all.

    So the model is now: rows ACCUMULATE across subjects, and CONFLICT only within one.
    Two rows naming the same findings with different reasons is a real disagreement and
    still refuses; two rows about different findings are two facts.

    Compared as a TUPLE with ``!=``, never hashed and never a ``set()``, so a hand-edited
    unhashable value cannot raise out of a caller that has no ``try``. ``finding_ids`` is
    NOT re-sorted here -- ``normalize_request`` already canonicalised it, and re-sorting
    would hide a hand-edited row whose ids are out of canonical order.
    """
    return tuple(row.get("finding_ids") or ())


def conflict_key(row):
    """The fields a judgment row AUTHORISES, as a comparable tuple.

    ▶ THE INVARIANT THIS SERVES HAS BEEN BROKEN THREE TIMES IN THE SIBLING FILE, and its
      own comment says so: *"EVERY field a usable record AUTHORISES belongs in the
      conflict key … was already written down, in this file, in those words, and the
      cycle that granted a new field authority did not re-ask it."* Part 2 re-asked it.
      A judgment authorises exactly two things — WHAT was judged and WHY — so both are
      here, and adding a third field to the row means adding it here in the same edit.

    ▶ **AND A THIRD FIELD WAS ADDED, IN THE SAME EDIT** (the contract). ``disposition`` is not decoration on the reason: `acted` and
      `declined` on the same finding with the same words are two DIFFERENT
      authorisations, and under the two-field key they merged silently — the exact shape
      of the three earlier breaks. It enters here in the same commit that gives it
      authority, and the contract J2/P6d redden if it is taken back out.

      ``row.get("disposition")`` answers ``None`` on the empty-ids path, where the field
      is FORBIDDEN, so the key stays a total function of the row and the empty path's
      comparisons are unchanged.

    Compared as a TUPLE with ``!=``, never hashed and never a ``set()``, so a hand-edited
    unhashable value cannot raise out of a caller that has no ``try``.

    ``finding_ids`` is compared as a tuple of its ALREADY-CANONICAL contents. It is NOT
    re-sorted here: re-sorting would hide a hand-edited row whose ids are out of
    canonical order, and this key's whole job is to notice that two rows differ.
    """
    return (tuple(row.get("finding_ids") or ()), row.get("reason"),
            row.get("disposition"))


def resolve_records(records, *, subject):
    """The row recording ``subject``, within one artifact identity. -> ``(row, state)``.

    ``state`` is ``"absent"`` | ``"ok"`` | ``"conflicting"``. NEVER resolves by append
    order -- that is the ``[-1]`` hazard the sibling file has been bitten by three times.

    ▶ REWRITTEN AFTER a review (H-2). It used to demand that EVERY
      row for these bytes agree, which made two legitimate judgments about different
      findings a conflict. See :func:`judgment_subject`.

    ``subject`` is REQUIRED and has NO DEFAULT. A caller asking "did the judgment land"
    must say WHICH judgment; a default would silently restore the every-row-must-agree
    behaviour this repairs, which is exactly how the defect would come back.

    Three outcomes, and the middle one is the one worth naming:

    * no row names this subject -> ``absent``. Rows for OTHER subjects are not evidence
      about this one, and reporting them would be answering a question nobody asked.
    * rows name it and DISAGREE on the reason -> ``conflicting``. A genuine
      disagreement: somebody judged the same finding two ways. Refused, never resolved
      by order.
    * rows name it and agree -> ``ok``. Duplicate appends of the same record are the
      same record, however many times it was written.
    """
    if not records:
        return None, "absent"
    matching = [r for r in records if judgment_subject(r) == tuple(subject)]
    if not matching:
        # Rows exist for these bytes, but none about this subject. ABSENT, not
        # conflicting: 's distinction one level in -- "somebody else judged
        # something else" is not "I could not tell what was judged".
        return None, "absent"
    first = conflict_key(matching[0])
    for rec in matching[1:]:
        if conflict_key(rec) != first:
            return None, "conflicting"
    return matching[0], "ok"


def receipt(*, zmx_dir, design_name, seq, filename, zmx_sha256,
            recorded_finding_ids, recorded_reason, read_state,
            recorded_disposition=None):
    """Schemas 3 and 4 - the SAME shape, produced ONLY from a re-read.

    ``recorded_reason`` WAS ADDED BY a review (H-4), and the
    omission it repairs is the sharpest one in the cycle. The receipt used to carry the
    ids alone, so ``save_candidate``'s obligation could compare the ids alone: a row
    whose REASON on disk differed from the request read back ``ok`` and the caller was
    told its judgment landed. The reason is the ONLY field this record exists to make
    durable -- ten designs were force-promoted and not one recorded why -- so a read-back
    blind to it certifies the wrong half.

    The spec said to compare ``normalized_judgment_request``, i.e. the WHOLE request.
    Half of it shipped, and the frozen four-schema block had no slot for the other half;
    the schema is widened here rather than the obligation quietly narrowed to fit it.

    A receipt is never built from the request. The request says what the caller asked to
    record; the receipt says what is ON DISK and still bound to these bytes. Those differ
    exactly when something went wrong, which is the only time a receipt is worth having.

    ``recorded_finding_ids`` is ``None`` on every non-``ok`` state — schema 4 — so a
    reader cannot mistake "no ids recorded" for "the record is absent/unreadable".

    ▶ ``recorded_disposition`` IS THE SAME REPAIR ONE FIELD OVER (the contract), and it is added in the edit that creates the field rather than in the
      one that notices the gap. H-4's finding was that a receipt blind to a field the
      caller asked to record certifies the wrong half; ``disposition`` is now a field the
      caller asks to record, so a read-back that compared ids and reason alone would
      report `ok` on a row whose disposition on disk said `acted` where the request said
      `declined`. It drops with the other two on every non-``ok`` state, for the reason
      stated below: they are three parts of ONE statement — what was recorded.

      It carries a ``None`` DEFAULT for the reason :func:`build_row`'s ``disposition``
      does, and with the same consequence: on the empty-ids path the recorded disposition
      IS ``None``, so a caller that omits it reads back exactly what the row says.
    """
    if read_state not in READ_STATES:
        # Defensive, and it fails toward the alarm: an unenumerated state is UNKNOWN,
        # and an UNKNOWN receipt must never carry ids that look established.
        read_state = "unreadable"
        recorded_finding_ids = None
        recorded_reason = None
        recorded_disposition = None
    if read_state != "ok":
        # ALL THREE payload fields drop together. They are three parts of ONE statement
        # -- what was recorded -- and a receipt carrying a reason or a disposition beside
        # a null id list would read as a partial success on a state that established
        # nothing.
        recorded_finding_ids = None
        recorded_reason = None
        recorded_disposition = None
    return {
        "identity": {
            "zmx_dir": zmx_dir,
            "design_name": design_name,
            "seq": seq,
            "filename": filename,
            "zmx_sha256": zmx_sha256,
        },
        "recorded_finding_ids": recorded_finding_ids,
        "recorded_reason": recorded_reason,
        "recorded_disposition": recorded_disposition,
        "read_state": read_state,
    }
