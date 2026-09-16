"""The FINDING record — a vision reviewer's declaration, bound to the bytes AND the picture.

Vision<->design contract, Phase 2 step 1.

WHAT THIS IS. One durable row per declared finding, written into the candidate manifest
and bound by digest to the candidate's own `.zmx` and its paired PNG. Plus the pure
predicates the promote-time RESPONSE question is decided with.

WHAT THIS IS NOT. It is not a claim that a review happened, not a claim that a finding
is honest, and not a claim that anything was done about one. Under the owner's Q1
ruling a candidate with zero recorded findings promotes freely, so this contract is
satisfiable by recording nothing. What it makes durable is a RESPONSE, once a finding
has been recorded. Nothing here may be described as *reasoned*, *acted on*, *the agent
saw it*, *the design was reviewed* or *visually clean* — the gate decides RESPONSE
PRESENCE and says so.

THIS MODULE IS PURE. No file IO, no engine, no import from `workspace`, no import from
`loop`. It owns the VOCABULARY and the PREDICATES; `workspace` owns the IO and injects
these. That split mirrors `_judgment.py:15-20` and its reason is unchanged: `workspace.py`
carries an OPEN size escalation, so new machinery lands beside it rather than inside it.
It also makes every rule here testable without a manifest, a session or a seat.

INJECTED, NEVER RE-IMPLEMENTED. `exact_int`, `is_hex64`, `writable`,
`judgment_row_targets` and `judgment_conflict_key` are all parameters, all keyword-only,
and NONE has a default. `_judgment.py:71-78` records why in terms: *"a carrier parameter
with a default is not a carrier"* — a default is exactly how the omission it guards
against gets acquired silently — and a second copy of "what is a sha256" is the
dead-constant drift this programme keeps finding.

THE ACCEPTANCE SET IS ONE BODY.:func:`validated_row` is used by the WRITER to
refuse its own row before it lands and by the READER to admit one. A row this writer
cannot get past this reader is evidence DEAD ON ARRIVAL — an earlier cycle
shipped exactly that defect.

NEVER RAISES, with ONE stated exception. Every public function returns a value on
hostile DATA — a non-dict, an unhashable member, a value whose ``__eq__`` raises.
Callers have no `try`. The exception is an INJECTED CALLABLE that raises: that is the
injector's defect, not this module's data handling, and :func:`evaluate_gate`'s outer
net is where it is caught and turned into a refusal. Nothing here can manufacture a
permission from an exception.

ABSENCE IS A CONCLUSION, NOT A DEFAULT. The reader's own positive `absent` token
is what routes to `finding_not_applicable`; an empty row list never is. Every non-`ok`
read state fails closed.
"""

import hashlib
import json
from collections import namedtuple

# ---------------------------------------------------------------------------
# Vocabulary — FROZEN.
# ---------------------------------------------------------------------------

FINDING_EVENT = "finding"
FINDING_SCHEMA = 1

#: The two scopes the reviewer's own contract offers (`reviewer-prompt.md:63-79`).
SCOPES = ("figure", "system")

#: The nine direction classes, VERBATIM from `design-vision-review/SKILL.md:116-120`.
DIRECTIONS = ("looks_tight", "looks_generous", "too_thin", "too_thick", "asymmetric",
              "steep_bend", "wrong_sign_suspected", "not_the_expected_form",
              "wrong_kind_element")

#: At `system` scope the instrument accepts only these two (`reviewer-prompt.md:76-78`).
#: The other seven describe a gap or a surface, and using one at system scope is a way
#: of avoiding pinning something that could have been pinned. Admitting all nine here
#: would record objects the reviewer contract itself rejects.
SYSTEM_DIRECTIONS = ("not_the_expected_form", "wrong_kind_element")

#: Refusal families for the recording tool.
FINDING_PARAM = "finding_param"                      # malformed request or finding shape
FINDING_UNBOUND = "finding_unbound"                  # no/ambiguous audit row, owner, digest
FINDING_FIGURE_UNBOUND = "finding_figure_unbound"    # png_sha256 null on row, or != caller's

#: The clause tokens :func:`normalize_findings` names in `bad_finding:<index>:<clause>`.
#: FROZEN: a new refusal reason is a new token here, never a re-used one.
FINDING_CLAUSES = ("not_an_object", "unknown_key", "config", "where", "key_required",
                   "key_forbidden", "key", "direction", "direction_scope", "note",
                   "unencodable")

#: The token, returned when `findings` is not a list at all.
BAD_PARAM = "bad_param"

# ---------------------------------------------------------------------------
# Gate verdicts — SEVEN, frozen.
# ---------------------------------------------------------------------------

(FINDING_NOT_APPLICABLE, FINDING_ANSWERED, REFUSE_FINDING_UNANSWERED,
 REFUSE_FINDING_UNREADABLE, REFUSE_FINDING_CONFLICTING,
 REFUSE_FINDING_NAME_UNPROVEN, REFUSE_FINDING_GATE_INTERNAL) = GATE_VERDICTS = (
    "finding_not_applicable", "finding_answered", "refuse_finding_unanswered",
    "refuse_finding_unreadable", "refuse_finding_conflicting",
    "refuse_finding_name_unproven", "refuse_finding_gate_internal")

#: The spelling `finding_not_applicable` is deliberate: `not_applicable` alone collides
#: with `metrics.STATUS_NOT_APPLICABLE`, which `promotion_gate.py:66-72` had to import
#: rather than type.

#: TWO wire families, not one per arm (`promotion_gate.py:160-163` precedent): a reason
#: token inside a family already names the arm; a family per arm is public surface for
#: no information.
PROMOTE_FINDING_UNANSWERED = "promote_finding_unanswered"
PROMOTE_FINDING_UNRESOLVED = "promote_finding_unresolved"

#: The gate may proceed. A POSITIVE allow-list, never a deny-list: `promotion_gate.py`
#: records that a DENY-list once let an unrecognised token PROMOTE.
_PERMITTING = frozenset({FINDING_NOT_APPLICABLE, FINDING_ANSWERED})
#: Every refusal, by SET DIFFERENCE — so a token added to `GATE_VERDICTS` is a refusal
#: automatically and cannot silently acquire permission.
_REFUSING = frozenset(GATE_VERDICTS) - _PERMITTING
_FAMILY_BY_VERDICT = dict(
    [(v, PROMOTE_FINDING_UNRESOLVED) for v in _REFUSING - {REFUSE_FINDING_UNANSWERED}]
    + [(REFUSE_FINDING_UNANSWERED, PROMOTE_FINDING_UNANSWERED)])
_ALL_VERDICTS = _PERMITTING | _REFUSING

#: the contract's frozen result. `detail` is disclosure ONLY — nothing branches on it.
FindingGateOutcome = namedtuple(
    "FindingGateOutcome", ("permits", "verdict", "family", "detail"))

#: The reader's own three states, as `_scan_manifest_records` reports them.
READ_OK, READ_ABSENT, READ_UNREADABLE = "ok", "absent", "unreadable"

_ID_LEN = 16
_FINDING_KEYS = ("config", "where", "key", "direction", "note")
_CORE_FIELDS = ("config", "where", "direction", "note")


# ---------------------------------------------------------------------------
# Small total helpers.
# ---------------------------------------------------------------------------

def _nonempty_str(value):
    """True iff ``value`` is a ``str`` with at least one non-whitespace character.

    POSITIVELY DEFINED, and `if not value:` is BANNED here — it collapses `""`, `0`,
    `False`, `[]` and `None` into one answer, so a caller passing the integer `42` as a
    note would be refused for "being empty" (`_judgment.py:57-65`).
    """
    return isinstance(value, str) and value.strip() != ""


def _eq(left, right):
    """``left == right``, answering ``False`` when the comparison itself raises.

    A hand-edited manifest row can carry a value whose ``__eq__`` raises. This module
    has no `try` at its call sites and its callers have none either, so a comparison is
    never allowed to escape. Answering ``False`` is the FAIL-CLOSED direction
    everywhere it is used: two things that cannot be compared are treated as DIFFERENT,
    which makes a conflict louder and coverage narrower, never the reverse.
    """
    try:
        return bool(left == right)
    except Exception:                                    # noqa: BLE001 — fail closed
        return False


def _contains(haystack, needle):
    """Membership by :func:`_eq`, never by hashing (`_judgment.py:274-277`).

    A `set()` or an `in` against one RAISES on an unhashable member, and the member
    comes from a file a human may have edited.
    """
    for item in haystack or ():
        if _eq(item, needle):
            return True
    return False


def _distinct(values):
    """The number of pairwise-distinct members of ``values``, compared by :func:`_eq`.

    Never hashed and never a `set()`, for the reason :func:`_contains` gives.
    """
    seen = []
    for value in values:
        if not _contains(seen, value):
            seen.append(value)
    return len(seen)


# ---------------------------------------------------------------------------
# the contract — identity, a content hash computed by the tool.
# ---------------------------------------------------------------------------

def _canonical_json(core):
    """``core`` as canonical JSON, or ``None`` when it cannot be canonicalised.

    Sorted keys, no whitespace, ``ensure_ascii=False`` — the last of those matched to
    the manifest writer's own `json.dumps` so an id is computed over the same text the
    row will carry. A core that cannot be serialised (a set, a mixed-type key map) has
    NO identity; it answers ``None`` rather than raising, and every consumer treats a
    ``None`` id as a refusal.
    """
    try:
        return json.dumps(core, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    except Exception:                                    # noqa: BLE001 — never raises
        return None


def finding_id(core, png_sha256):
    """``sha256(canonical_json(core) + png_sha256).hexdigest()[:16]``, or ``None``.

    ▶ **The signature in the contract says `-> str`. It returns `str | None`,
      and the widening is REQUIRED by the module's own never-raise rule** — a core that
      cannot be canonicalised, or a `png_sha256` that is not a `str`, has no identity,
      and the only alternatives are raising (banned) or fabricating a digest (worse: a
      fabricated id would be RECOMPUTABLE and would therefore VALIDATE). Every consumer
      here treats ``None`` as "not a record".

    **Properties, and why each was chosen**. TOTAL — it works at `system` scope,
    where `(direction, key, config)` is not a key at all because `key` is absent by
    contract. NO REGISTRY, NO COUNTER, NO MINT STATE — nothing on disk is read to make
    an id, so recording is idempotent at finding granularity for free. RECOMPUTABLE —
    :func:`validated_row` recomputes it from the row's own fields, so a hand-edited row
    is not a record. CALLER-AGNOSTIC — no author, no session, no timestamp enters it.

    **The cost, stated** (`RESEARCH.md:436`): reproducing an id needs the reviewer's
    reply PLUS the audit row's `png_sha256`, and it inherits that digest's provenance. A
    paraphrased finding gets a fresh, self-consistent id — the contract makes the
    declaration DURABLE, not FAITHFUL.
    """
    if not isinstance(png_sha256, str):
        return None
    blob = _canonical_json(core)
    if blob is None:
        return None
    try:
        raw = (blob + png_sha256).encode("utf-8")
    except Exception:                                    # noqa: BLE001 — never raises
        return None
    return hashlib.sha256(raw).hexdigest()[:_ID_LEN]


# ---------------------------------------------------------------------------
# the contract — the finding shape, validated against the instrument's own contract.
# ---------------------------------------------------------------------------

def _canonical_key(value, exact_int):
    """``(canonical_key, True)`` or ``(None, False)``.

    ONE label is an exact int; the space between TWO bounding labels is a list of two,
    CANONICALISED ASCENDING so `[4, 3]` and `[3, 4]` are the same finding and therefore
    the same id. `exact_int` excludes `bool`, so `True` is not the label 1.
    """
    if exact_int(value):
        return value, True
    if isinstance(value, (list, tuple)) and len(value) == 2:
        first, second = value[0], value[1]
        if exact_int(first) and exact_int(second):
            return ([first, second] if first <= second else [second, first]), True
    return None, False


def _normalize_one(item, writable, exact_int):
    """``(core, None)`` or ``(None, clause)`` for ONE finding object. Never raises."""
    if not isinstance(item, dict):
        return None, "not_an_object"
    for name in item:
        if not _contains(_FINDING_KEYS, name):
            # A typo'd key is a finding the caller believes they stated and did not.
            return None, "unknown_key"

    where = item.get("where")
    if not _contains(SCOPES, where):
        return None, "where"

    if "config" not in item:
        return None, "config"
    config = item["config"]
    if config is not None and not exact_int(config):
        return None, "config"

    key = None
    if where == "figure":
        if "key" not in item:
            return None, "key_required"
        key, ok = _canonical_key(item["key"], exact_int)
        if not ok:
            return None, "key"
    elif "key" in item:
        # `reviewer-prompt.md:73` — *"Omit key entirely at "system" scope."*
        return None, "key_forbidden"

    direction = item.get("direction")
    if not _contains(DIRECTIONS, direction):
        return None, "direction"
    if where == "system" and not _contains(SYSTEM_DIRECTIONS, direction):
        return None, "direction_scope"

    note = item.get("note")
    if not _nonempty_str(note):
        return None, "note"
    if not writable(note):
        # DISTINCT from the empty-note refusal: "you said nothing" and "we cannot write
        # down what you said" have different remedies (`_judgment.py:126-133`).
        return None, "unencodable"

    core = {"config": config, "where": where, "direction": direction, "note": note}
    if where == "figure":
        core["key"] = key
    return core, None


def normalize_findings(findings, *, writable, exact_int):
    """Validate the reviewer's `findings` ARRAY. -> ``(cores, error)``, never raises.

    Exactly one of the two is ``None``. ``cores`` is a list of the contract core dicts in REPLY
    ORDER — never deduped here, because the error token names an INDEX into the caller's
    own array and a dedup would make that index a lie. Byte-identical findings collapse
    later, at the id, which is where they are actually the same finding.

    ▶ **`exact_int` is a SIGNATURE DEVIATION from the contract**, which lists
      `writable` only. It is required: `config` is `int|null` and `key` is `int` or a
      pair of them, and a LOCAL "what is an exact int" here would be a second copy of a
      rule:func:`validated_row` gets INJECTED — the drift exists to stop, one field
      over. The spec's own the contract injection paragraph enumerates five predicates and then
      calls them *"the four"*, so its signature list is indicative rather than exhaustive.
      No default, on the `_judgment.py:71-78` rule.

    `findings` may legitimately be `[]` — silence is not a row — and that
    answers ``([], None)``. A non-list answers the token `bad_param`: passing the
    reviewer's whole reply OBJECT here is that case, and it is a caller error, not a
    finding error.
    """
    if not isinstance(findings, (list, tuple)):
        return None, BAD_PARAM
    cores = []
    for index, item in enumerate(findings):
        core, clause = _normalize_one(item, writable, exact_int)
        if clause is not None:
            # ALL-OR-NOTHING: the caller is told which element and which clause, and
            # nothing is written. A partial write would record a reply that was never
            # made.
            return None, "bad_finding:%d:%s" % (index, clause)
        cores.append(core)
    return cores, None


# ---------------------------------------------------------------------------
# the contract — the durable row.
# ---------------------------------------------------------------------------

def build_row(*, seq, design_name, filename, zmx_sha256, png_sha256, core, ts):
    """The durable row, exactly as the contract declares it.

    ONE construction site, so the writer cannot drift from what :func:`validated_row`
    admits. Every identity field is HANDED IN from the validated `candidate_audit` row —
    this function does not know the request exists, which is what makes it structurally
    impossible for a caller-supplied `filename`, `zmx_sha256` or `finding_id` to reach a
    row.

    `filename` is stored and compared by RAW string equality, never basenamed
    (`_judgment.py:194-199`): NOT basenaming the record side is what makes
    `../0005_x.zmx` and absolute paths FAIL rather than silently match.
    """
    body = core if isinstance(core, dict) else {}
    row = {
        "event": FINDING_EVENT,
        "schema": FINDING_SCHEMA,
        "ts": ts,
        "seq": seq,
        "design_name": design_name,
        "filename": filename,
        "digest_algo": "sha256",
        "zmx_sha256": zmx_sha256,
        "png_sha256": png_sha256,
        "finding_id": finding_id(body, png_sha256),
    }
    for name in _CORE_FIELDS:
        row[name] = body.get(name)
    if body.get("where") == "figure":
        row["key"] = body.get("key")
    return row


def _core_of(row):
    """The contract core, read back OUT of a row, for id recomputation."""
    core = {}
    for name in _CORE_FIELDS:
        core[name] = row.get(name)
    if row.get("where") == "figure":
        core["key"] = row.get("key")
    return core


def _key_well_formed(row, exact_int):
    """`key` present iff `figure` scope, and in CANONICAL form when present.

    Canonical is asserted rather than re-derived on the read side: if this admitted
    `[4, 3]`, the recomputation would canonicalise it, the ids would match, and a
    non-canonical row would become a second valid record of the same finding.
    """
    where = row.get("where")
    if where != "figure":
        return "key" not in row
    if "key" not in row:
        return False
    value = row["key"]
    if exact_int(value):
        return True
    return (isinstance(value, list) and len(value) == 2
            and exact_int(value[0]) and exact_int(value[1])
            and value[0] <= value[1])


def validated_row(row, *, exact_int, is_hex64):
    """True iff ``row`` passes EVERY clause of the contract Never raises.

    THE ONE ACCEPTANCE SET — the writer refuses its own row with this, the reader
    admits with this. Every subscript any consumer performs is a key made REQUIRED here.

    `png_sha256` is REQUIRED hex64, not optional-or-hex64: a finding about a picture
    that cannot be bound is not recordable at all, which is what refuses at the tool
    boundary and what this clause makes unrepresentable on disk.

    The last clause is A's T2 moved to READ time: `finding_id` is RECOMPUTED from the
    row's own fields and compared. A hand-edited note, key, direction, config or
    `png_sha256` therefore fails, and — because the ladder in `workspace` fails the
    WHOLE read closed on an invalid PARSED row that targets the scope — a tampered
    finding row refuses a promote rather than disappearing from it.

    ▶ **AMENDMENT A1 — THREE CLAUSES ADDED, and the reason is
      the divergence this very docstring names.** As locked, the contract checked `where in
      SCOPES` and `direction in DIRECTIONS` INDEPENDENTLY, so a `system`-scope row
      carrying `too_thin` VALIDATED ON READ while :func:`normalize_findings` REFUSED IT
      ON WRITE (`bad_finding:<i>:direction_scope`) — the reader admitting a row the
      writer cannot produce, which is *evidence dead on arrival* read backwards. the contract
      also named neither `event` nor `config`. The three:

      1. `event == FINDING_EVENT` — a `judgment` row with a finding's other fields is
         not a finding record. The row selectors ask this, so the acceptance set must
         too, or the WRITER's self-check (`workspace._write_finding_record`) would
         admit a row its own reader only reaches through `row_targets_*`.
      2. `config` PRESENT, and `None` or an exact int. Presence is
         `reviewer-prompt.md:65` — *"**config** — {config}. Write that number,
         unchanged, into every finding."* — which requires the FIELD, not a value; the
         type half is what refuses with the `config` clause, and without it a row
         carrying `config: "1"` or `config: True` recomputes a self-consistent id and
         would be admitted. **The spec's amendment text states only presence; presence
         alone does not close the divergence and is reported as a residual.**
      3. THE DIRECTION↔SCOPE CLAUSE — at `where == "system"`, `direction` must be in
         :data:`SYSTEM_DIRECTIONS`. Grounded in `reviewer-prompt.md:76-78`, read and
         quoted verbatim: *"At `system` scope only `not_the_expected_form` and
         `wrong_kind_element` are accepted — the others describe a gap or a surface,
         and using one at system scope is a way of avoiding pinning something you could
         have pinned."*

      **The property, not the three clauses** (`_judgment.py:22-27`): ANY finding object
      :func:`normalize_findings` refuses on write is refused HERE on read, even when the
      row carrying it is built with a self-consistent id. That is what
      `test_a1_the_acceptance_set_is_one_body_in_both_directions` proves; the clause
      count is an implementation detail of it.
    """
    if not isinstance(row, dict):
        return False
    # A1 clause 1 — is this a finding row at all? Compared with :func:`_eq`, never a
    # bare `!=`: a hand-edited manifest can carry a value whose `__ne__` raises, and
    # this module has no `try` at its call sites.
    if not _eq(row.get("event"), FINDING_EVENT):
        return False
    schema = row.get("schema")
    if not exact_int(schema) or schema != FINDING_SCHEMA:
        return False
    # EXACT: a "blake3" row is NEVER re-interpreted as sha256.
    if row.get("digest_algo") != "sha256":
        return False
    if not is_hex64(row.get("zmx_sha256")):
        return False
    if not is_hex64(row.get("png_sha256")):
        return False
    if not _nonempty_str(row.get("design_name")):
        return False
    if not _nonempty_str(row.get("filename")):
        return False
    if not _nonempty_str(row.get("note")):
        return False
    if not _nonempty_str(row.get("ts")):
        return False
    if not exact_int(row.get("seq")):
        return False
    if not _contains(SCOPES, row.get("where")):
        return False
    if not _key_well_formed(row, exact_int):
        return False
    # A1 clause 2 — `config` is REQUIRED, may be null, and is never a free string.
    if "config" not in row:
        return False
    config = row["config"]
    if config is not None and not exact_int(config):
        return False
    if not _contains(DIRECTIONS, row.get("direction")):
        return False
    # A1 clause 3 — the direction<->scope clause (`reviewer-prompt.md:76-78`).
    if (_eq(row.get("where"), "system")
            and not _contains(SYSTEM_DIRECTIONS, row.get("direction"))):
        return False
    recomputed = finding_id(_core_of(row), row.get("png_sha256"))
    return recomputed is not None and row.get("finding_id") == recomputed


# ---------------------------------------------------------------------------
# the contract — the two row selectors the readers inject as `targets`.
# ---------------------------------------------------------------------------

def row_targets_design(row, *, design_names):
    """True iff ``row`` is a `finding` row whose `design_name` is in the subject set."""
    if not isinstance(row, dict) or row.get("event") != FINDING_EVENT:
        return False
    return _contains(design_names, row.get("design_name"))


def row_targets_any(row):
    """True iff ``row`` is a `finding` row, whatever design it names (Rule 0, Rule 1)."""
    return isinstance(row, dict) and row.get("event") == FINDING_EVENT


# ---------------------------------------------------------------------------
# the contract — ANCHORING. A judgment row is a response only at a SAVED identity.
# ---------------------------------------------------------------------------

def anchored_judgment_rows(judgment_rows, audit_rows, *, judgment_row_targets,
                           exact_int):
    """``(anchored, n_unanchored)`` — the contract..

    A judgment row J counts as a RESPONSE only if, for SOME validated `candidate_audit`
    row A read under the subject set, the shipped
    ``judgment_row_targets(J, A["seq"], A["filename"], exact_int=…, design_name=A["design_name"])``
    is True AND ``J["zmx_sha256"] == A["zmx_sha256"]``. `zmx_dir` is the one manifest
    being read, so the effective identity is the full five-tuple.

    **What this establishes**: the judgment's identity is one the harness wrote a
    candidate under. **What it does NOT establish**: who wrote the judgment row. A
    hand-edit that COPIES a real candidate's four fields anchors — the manifest carries
    no MAC, and that is the shipped `candidate_audit` trust ceiling, not a new one.

    Round 1 was spoofable here: it selected judgment rows by `event` and `design_name`,
    and `_judgment.validated_row` checks SHAPE only — no `seq`, no join, no digest — so
    a hand-edited row with a well-formed fabricated digest COVERED a real finding.

    An unanchored row is not an error and not a refusal of its own — it is simply not a
    response, so ignoring it is the closed direction — but it is COUNTED, because a
    HARNESS-written judgment that fails to anchor means the audit row it was written
    beside was never written (`workspace.py:1200-1203`, best-effort), and the operator
    should see that rather than an inexplicable `unanswered`.

    Validation is INHERITED, never re-performed: every member of ``audit_rows`` was
    already admitted by `_validated_audit_row`, and every member of ``judgment_rows`` by
    `_judgment.validated_row`. Re-validating here would be the second-predicate defect.

    ▶ **`exact_int` is a SIGNATURE DEVIATION from the contract**, whose listed
      signature omits it while the contract's own pseudocode passes ``exact_int=exact_int`` into
      `judgment_row_targets`. It cannot come from anywhere else; it is injected, with no
      default.
    """
    rows = list(judgment_rows or ())
    audits = [a for a in (audit_rows or ()) if isinstance(a, dict)]
    anchored = []
    for judgment in rows:
        if not isinstance(judgment, dict):
            continue
        for audit in audits:
            targets = judgment_row_targets(
                judgment, audit.get("seq"), audit.get("filename"),
                exact_int=exact_int, design_name=audit.get("design_name"))
            if targets and _eq(judgment.get("zmx_sha256"), audit.get("zmx_sha256")):
                anchored.append(judgment)
                break
    return anchored, len(rows) - len(anchored)


# ---------------------------------------------------------------------------
# the contract — COVERAGE, union semantics.
# ---------------------------------------------------------------------------

def _docketed_ids(finding_rows):
    """The distinct `finding_id`s of the in-scope finding rows, in a stable order."""
    ids = []
    for row in finding_rows or ():
        if not isinstance(row, dict):
            continue
        fid = row.get("finding_id")
        if isinstance(fid, str) and not _contains(ids, fid):
            ids.append(fid)
    return ids


def _named_ids(judgment):
    """The id-set a judgment row names, as a list. Total; never raises."""
    ids = judgment.get("finding_ids") if isinstance(judgment, dict) else None
    if not isinstance(ids, (list, tuple)):
        return []
    return [x for x in ids if isinstance(x, str)]


def open_finding_ids(finding_rows, anchored_rows):
    """The docketed ids NAMED BY NO anchored judgment row. UNION semantics.

    ``answered`` is the UNION over EVERY anchored row's id-set, not a per-id lookup:
    the shipped subject is an id-SET (`_judgment.py:279`) compared by EXACT TUPLE
    EQUALITY (`:328`), so `resolve_records(subject=("f1",))` answers `absent` for a row
    that answered `["f1","f2"]` together. A per-finding question cannot be asked through
    it, which is why this is a new scan rather than a call to it.

    The anchored rows are bound to ANY seq of the design, so a seq-9 judgment answers a
    seq-3 finding — that IS the Q3 design-scoped ruling, not a loophole in it.

    ▶ Takes the ANCHORED list only. :func:`evaluate_gate` is the one caller that anchors,
      so a later reader cannot hand this raw rows by accident without also skipping Rule 3b.
    """
    docketed = _docketed_ids(finding_rows)
    answered = []
    for judgment in anchored_rows or ():
        for fid in _named_ids(judgment):
            answered.append(fid)
    return tuple(fid for fid in docketed if not _contains(answered, fid))


# ---------------------------------------------------------------------------
# the contract — PER-ID conflict, asked at the id's LATEST anchored identity.
# ---------------------------------------------------------------------------

def _identity_of(judgment, exact_int):
    """``(design_name, seq, filename, zmx_sha256)`` or ``None`` when unusable."""
    seq = judgment.get("seq")
    if not exact_int(seq):
        return None
    return (judgment.get("design_name"), seq, judgment.get("filename"),
            judgment.get("zmx_sha256"))


def _group_by_identity(rows, exact_int):
    """``[[identity, [row, ...]], ...]`` — grouped by ``==``, never hashed."""
    groups = []
    for judgment in rows:
        identity = _identity_of(judgment, exact_int)
        if identity is None:
            continue
        for group in groups:
            if _eq(group[0], identity):
                group[1].append(judgment)
                break
        else:
            groups.append([identity, [judgment]])
    return groups


def _projection_count(rows, judgment_conflict_key):
    """How many distinct `(reason, disposition)` tails these rows carry.

    ``judgment_conflict_key(row)[1:]`` — the TAIL of the contract's three-field key, i.e. what
    the row AUTHORISES minus WHAT it is about. Projecting the id-SET instead would make
    two rows naming different id-sets look like a disagreement when they agree
    (P6c), and would make two rows that genuinely disagree look identical when their
    id-sets happen to match (P6b).

    The key callable is invoked OUTSIDE any `try` on purpose: an injected predicate that
    raises is the injector's defect and must reach :func:`evaluate_gate`'s net as a
    `refuse_finding_gate_internal`, not be silently absorbed into a conflict.
    """
    keys = []
    for judgment in rows:
        key = judgment_conflict_key(judgment)
        keys.append(tuple(key[1:]) if isinstance(key, (tuple, list)) else (key,))
    return _distinct(keys)


def per_id_conflicts(finding_rows, anchored_rows, *, judgment_conflict_key, exact_int):
    """``(conflicting, superseded_conflicts)`` — the contract

    The shipped rule conflicts per id-SET, so `["f1","f2"]: "fine"` and `["f1"]:
    "wrong"` are different subjects, no conflict is reported, and `f1` quietly carries
    two contradictory reasons. This adds a PER-ID rule, asked at the id's LATEST
    ANCHORED IDENTITY.

    **Why the latest identity, and why that is NOT "append order."**
    `_judgment.py:305-306` forbids resolving by append order because the `[-1]` hazard
    picks one of two contradictory rows AT ONE IDENTITY and calls it the answer. This
    never does that: at one identity, two projections is a conflict, full stop, whatever
    their file order (P7c). What it does is honour Q3's own model — rows ACCUMULATE
    across subjects (`_judgment.py:264-272`), and a later seq is a new subject in time —
    so the design's CURRENT answer to `f` is the one given at its newest bytes, and an
    older contradiction is a FACT ABOUT THE RECORD that stays in it and is COUNTED, not
    a question still open. `seq` is read from the anchored row's OWN validated field
    under `exact_int`, never from file position (P7d).

    Round 1 asked the question at EVERY identity, which made a within-identity conflict
    PERMANENT under an append-only manifest: its disclosed remedy — re-save under a new
    identity — did not clear it, because the old pair stayed in scope. Under this rule
    the remedy works and is the same one call as for `unanswered`.

    Two identities sharing one maximal `seq` is a corrupted manifest and conflicts: it
    means the design's newest bytes have two different answers and nothing decides
    between them.
    """
    docketed = _docketed_ids(finding_rows)
    anchored = [j for j in (anchored_rows or ()) if isinstance(j, dict)]
    conflicting = []
    superseded = []
    for fid in docketed:
        rows = [j for j in anchored if _contains(_named_ids(j), fid)]
        groups = _group_by_identity(rows, exact_int)
        if not groups:
            continue
        max_seq = max(group[0][1] for group in groups)
        latest = [group for group in groups if group[0][1] == max_seq]
        if len(latest) > 1:
            conflicting.append(fid)
            continue
        if _projection_count(latest[0][1], judgment_conflict_key) > 1:
            conflicting.append(fid)
            continue
        older = [group for group in groups if group[0][1] != max_seq]
        for group in older:
            if _projection_count(group[1], judgment_conflict_key) > 1:
                superseded.append(fid)
                break
    return tuple(conflicting), tuple(superseded)


# ---------------------------------------------------------------------------
# the contract — the gate ladder Rule 0..Rule 4. FIRST FAILURE WINS, no default-allow.
# ---------------------------------------------------------------------------

def _subject_names(caller, proven):
    """`{caller} ∪ proven_design_names`, as a LIST compared by ``==``.

    A list rather than a set because a hand-edited row's `design_name` reaching an `in`
    against a set would hash it. `proven is None` means UNKNOWN and is NEVER the empty
    set (`promotion_gate.py:220-223`); the Rule 1 arm below is what handles the UNKNOWN case,
    and treating `None` as `set()` here is the fail-OPEN direction STOP-6 names.
    """
    names = [caller]
    for name in sorted(proven or ()):
        if not _contains(names, name):
            names.append(name)
    return names


def _read_state(finding_state, judgment_state, audit_state):
    """The composite read state disclosed in the `finding_gate` key.

    `[INTERPRETATION]` — the contract lists a single `read_state` key while the contract reads three
    manifests. This reports the WORST of the three, so a disclosure can never read `ok`
    beside a refusal caused by a read, and the three individual states ride beside it.
    """
    for state in (finding_state, judgment_state, audit_state):
        if state == READ_UNREADABLE:
            return READ_UNREADABLE
    if finding_state == READ_ABSENT:
        return READ_ABSENT
    return READ_OK


def _outcome(verdict, detail):
    """Build the one result shape, normalizing an off-vocabulary token.

    **THE ONE `permits` ASSIGNMENT LIVES HERE AND NOWHERE ELSE.** It is
    ``verdict in _PERMITTING`` — never a default-True, never a per-branch literal, never
    a deny-list. A token outside the frozen seven is kept for diagnosis and REPLACED by
    `refuse_finding_gate_internal`: an unrecognised verdict must not merely be
    non-permitting, it must be NAMED.

    `isinstance` FIRST, then membership — a bare `in` against a frozenset RAISES
    `TypeError: unhashable type` on a list verdict, and the ONE site that decides
    `permits` should not be the site that needs rescuing.
    """
    if not isinstance(verdict, str) or verdict not in _ALL_VERDICTS:
        detail = dict(detail)
        detail["verdict_raw"] = verdict
        verdict = REFUSE_FINDING_GATE_INTERNAL
    permits = verdict in _PERMITTING
    return FindingGateOutcome(permits, verdict, _FAMILY_BY_VERDICT.get(verdict), detail)


def _detail(subject, states, **fields):
    """The contract disclosure body. Unreached steps read ``None``, never a fabricated 0."""
    body = {
        "subject_names": list(subject),
        "n_in_scope": None,
        "n_answered": None,
        "n_unanchored": None,
        "open_finding_ids": (),
        "conflicting_finding_ids": (),
        "superseded_conflict_ids": (),
        "read_state": _read_state(*states),
        "finding_read_state": states[0],
        "judgment_read_state": states[1],
        "audit_read_state": states[2],
    }
    body.update(fields)
    return body


def _evaluate_gate(caller, proven, finding_rows, finding_state, audit_rows, audit_state,
                   judgment_rows, judgment_state, judgment_row_targets,
                   judgment_conflict_key, exact_int):
    """The contract ladder. Called ONLY through:func:`evaluate_gate`'s net."""
    subject = _subject_names(caller, proven)
    states = (finding_state, judgment_state, audit_state)

    # Rule 0 — the reader's own POSITIVE tokens, never `len(rows) == 0`.
    if finding_state == READ_UNREADABLE:
        return _outcome(REFUSE_FINDING_UNREADABLE, _detail(subject, states))
    if finding_state == READ_ABSENT:
        return _outcome(FINDING_NOT_APPLICABLE, _detail(subject, states))

    rows = [r for r in (finding_rows or ()) if isinstance(r, dict)]

    # Rule 1 — the alias arm. With the subject set UNESTABLISHED and finding rows under a
    # name the caller did not give, the bytes MAY belong to that design and the question
    # cannot be answered: REFUSE, never route to `not_applicable`
    # (`promotion_gate.py:497-508` is this exact defect in the contract gate's history).
    if proven is None:
        foreign = [r for r in rows if not _contains(subject, r.get("design_name"))]
        if foreign:
            return _outcome(REFUSE_FINDING_NAME_UNPROVEN,
                            _detail(subject, states, n_in_scope=len(rows) - len(foreign)))

    # Rule 2 — the design-scoped row filter. When `proven` is a set the UNION is the scope,
    # so an alias must answer the owner's findings.
    scoped = [r for r in rows if _contains(subject, r.get("design_name"))]
    if not scoped:
        return _outcome(FINDING_NOT_APPLICABLE, _detail(subject, states, n_in_scope=0))

    # Rule 3 / Rule 3a — both joins must be READ before either can be trusted.
    if judgment_state == READ_UNREADABLE or audit_state == READ_UNREADABLE:
        return _outcome(REFUSE_FINDING_UNREADABLE,
                        _detail(subject, states, n_in_scope=len(scoped)))

    # Rule 3b — SHAPE IS NOT BINDING. Only anchored rows are responses.
    anchored, n_unanchored = anchored_judgment_rows(
        judgment_rows, audit_rows, judgment_row_targets=judgment_row_targets,
        exact_int=exact_int)

    # Rule 4 — conflict first, then coverage.
    conflicts, superseded = per_id_conflicts(
        scoped, anchored, judgment_conflict_key=judgment_conflict_key,
        exact_int=exact_int)
    docketed = _docketed_ids(scoped)
    opened = open_finding_ids(scoped, anchored)
    body = _detail(subject, states, n_in_scope=len(scoped),
                   n_answered=len(docketed) - len(opened),
                   n_unanchored=n_unanchored,
                   open_finding_ids=opened,
                   conflicting_finding_ids=conflicts,
                   superseded_conflict_ids=superseded)
    if conflicts:
        return _outcome(REFUSE_FINDING_CONFLICTING, body)
    if opened:
        return _outcome(REFUSE_FINDING_UNANSWERED, body)
    return _outcome(FINDING_ANSWERED, body)


def evaluate_gate(*, caller, proven, finding_rows, finding_state,
                  audit_rows, audit_state, judgment_rows, judgment_state,
                  judgment_row_targets, judgment_conflict_key, exact_int):
    """THE question asked at point P. Returns a :class:`FindingGateOutcome`; NEVER raises.

    Note what is HANDED IN and never re-derived: every row list and every read state
    arrives WHOLE from the readers `workspace` already ran. This module resolves no
    file, opens nothing and imports no tool.

    **What it never reads**: the promote request's own `judgment` block
    (that row is written AFTER the copy, so counting it would discharge the docket on a
    request not yet on disk — 's `X == X`); any finding's `direction`, `note`,
    `config` or `key`; any judgment's `reason` CONTENT or `disposition` VALUE; any
    `check_clearance` output; a dispatch or round count; the shipped `promote` row; or
    ANY row's file position. The one `seq` comparison it makes is over a
    validated, anchored field.

    The net below can only ever produce `refuse_finding_gate_internal` /
    ``permits=False``, so nothing here can manufacture a permission out of a raise.
    """
    try:
        return _evaluate_gate(caller, proven, finding_rows, finding_state,
                              audit_rows, audit_state, judgment_rows, judgment_state,
                              judgment_row_targets, judgment_conflict_key, exact_int)
    except Exception as exc:                             # noqa: BLE001 — fail closed
        return _outcome(REFUSE_FINDING_GATE_INTERNAL,
                        {"subject_names": [], "n_in_scope": None, "n_answered": None,
                         "n_unanchored": None, "open_finding_ids": (),
                         "conflicting_finding_ids": (), "superseded_conflict_ids": (),
                         "read_state": READ_UNREADABLE,
                         "gate_raised": type(exc).__name__})
