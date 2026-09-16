"""``record_findings`` — the tool that makes a vision reviewer's declaration DURABLE.

Vision<->design contract, Phase 2 step 4 (the contract, and the row schema
the contract it writes through).

WHAT THIS TOOL IS. A caller-agnostic RECORDING door. It takes the reviewer's `findings`
ARRAY, binds it by digest to a saved candidate's `.zmx` AND to the paired PNG the
reviewer was shown, and appends one durable row per finding to that candidate's
manifest. Recording is IDEMPOTENT at finding granularity, so the driver may call it
whether or not the reviewer already did.

WHAT THIS TOOL IS NOT. It is not a claim that a review happened, not a claim that a
finding is honest, not a claim that anyone acted on one, and NOT a gate — it refuses
BAD BINDINGS, never bad opinions. Nothing here reads `direction`, `note` or `config` as
evidence about the design; those fields are transported and stored, never judged (R-1).
A candidate with zero recorded findings promotes freely (Q1).

▶ **PLACEMENT — A DELIBERATE, REPORTED DEVIATION FROM the contract** The
  spec's change-set table puts this handler and its `ToolSpec` INSIDE `workspace.py`.
  They land in a module of their own instead, for the reason the spec itself gives twice
  when it places `_judgment.py` and `_finding.py` BESIDE `workspace.py` rather than
  inside it (`_judgment.py:15-20`): `workspace.py` carries an OPEN size size escalation,
  and this handler would add ~200 statements to a 4,200-line file. The IO half it calls
  is UNCHANGED and is IMPORTED, not copied — there is exactly one writer, one receipt
  and one acceptance set, which is the property the contract is actually defending. The
  consequence for registration is stated at :data:`TOOL_SPECS`.

SEAT BEHAVIOUR. This handler reads
`session.workspace_root` / `session.projects_root` only and NEVER touches
`session.system`. It is still a dispatchable HARNESS tool, so `LazyHarnessDispatcher`
attempts `session.open()` ahead of it; the cost is SERIALISATION on the single seat, not
a second engine. the contract T11 is the row that pins the no-engine-access half.
"""
import os

from .. import _io
from ..server import ToolSpec
from . import _finding
from .workspace import (
    _AUDIT_EVENT,
    _design_dir,
    _exact_int,
    _finding_receipt,
    _is_hex64,
    _read_finding_records,
    _scan_manifest_records,
    _sha256_file,
    _validated_audit_row,
    _writable_name,
    _write_finding_record,
)

#: Reason tokens, FROZEN. Rule 1/Rule 2 come from `_finding` (the writer's own vocabulary); the
#: seven binding tokens below are this tool's, one per the contract refusal row.
AUDIT_RECORD_UNREADABLE = "audit_record_unreadable" # Rule 3
AUDIT_RECORD_ABSENT = "audit_record_absent" # Rule 4
AUDIT_RECORD_CONFLICTING = "audit_record_conflicting" # Rule 5
OWNER_MISMATCH = "owner_mismatch" # Rule 6
DIGEST_MISMATCH = "digest_mismatch" # Rule 7
ZMX_UNREADABLE = "zmx_unreadable" # Rule 7
PICTURE_UNBINDABLE = "picture_unbindable" # Rule 8
PICTURE_MISMATCH = "picture_mismatch" # Rule 9

#: Rule 10 — **NOT IN the contract, and REPORTED as amendment A16 rather than
#: invented silently.** the contract enumerates nine refusals and none of them covers an
#: UNREADABLE *finding* record, which is a genuinely reachable state the nine cannot
#: reach: an invalid parsed `finding` row under this design at ANY seq makes the
#: design-scoped read `unreadable` (`workspace.py:1638-1659`) while the seq-scoped AUDIT
#: read this tool performs first stays `ok`. The idempotency question — "is this finding
#: already recorded" — then has no answer, and the rule that `promotion_gate.py:32-36` forbids
#: reading UNKNOWN as "no, record it again". It fails CLOSED, inside the EXISTING
#: `finding_unbound` family (the three wire families are frozen at the contract), because a
#: record whose prior state cannot be established is exactly a record that cannot be
#: BOUND. No new family; one new reason token, disclosed.
FINDING_RECORD_UNREADABLE = "finding_record_unreadable" # Rule 10 [INTERPRETATION]

#: A refusal that does not name a REMEDY is a defect report addressed to nobody. One
#: entry per reason token, so an arm cannot be added without one.
_REMEDY = {
    AUDIT_RECORD_UNREADABLE: (
        "the candidate manifest could not be read; repair or quarantine the bad line "
        "in candidates/zmx/manifest.jsonl out of band"),
    AUDIT_RECORD_ABSENT: (
        "no candidate_audit row exists for this seq; call save_candidate first and "
        "record against the seq it returns"),
    AUDIT_RECORD_CONFLICTING: (
        "two candidate_audit rows for this seq disagree about the bytes; repair the "
        "manifest out of band"),
    OWNER_MISMATCH: (
        "this seq belongs to a different design; record under the design_name the "
        "audit row names"),
    DIGEST_MISMATCH: (
        "the .zmx on disk is no longer the file the audit row measured; save_candidate "
        "again and record against the new seq"),
    ZMX_UNREADABLE: (
        "the candidate .zmx could not be read, so the findings cannot be bound to it"),
    PICTURE_UNBINDABLE: (
        "no picture is bound to this candidate (png_sha256 is null on its audit row), "
        "so a finding about a figure has nothing to bind to; save_candidate again"),
    PICTURE_MISMATCH: (
        "png_sha256 does not match the picture recorded for this candidate; you are "
        "reviewing a different figure — pass the png_sha256 save_candidate returned"),
    FINDING_RECORD_UNREADABLE: (
        "the finding records already on this design's manifest could not be read, so "
        "whether these findings are already recorded cannot be established; repair or "
        "quarantine the bad line in candidates/zmx/manifest.jsonl out of band"),
}


def _read_audit_records_seq(zmx_dir, seq):
    """Validated `candidate_audit` rows for THIS ``seq``, whatever design. NEVER raises.

    ONE call to the shipped `_scan_manifest_records` ladder — not a second ladder.
    That ladder's ABSENT/UNREADABLE discrimination took four audit rounds to settle and
    is inherited here, never re-argued.

    ▶ **SEQ-SCOPED, NOT DESIGN-SCOPED, AND THAT IS LOAD-BEARING.** the contract's
      `_read_audit_records_design` selects on `design_name`, so a row filed under
      ANOTHER design would not be selected at all and Rule 6 (`owner_mismatch`) would be
      UNREACHABLE — every foreign seq would answer Rule 4 `audit_record_absent`, telling the
      caller "no such candidate" when the truth is "that candidate is not yours". the contract
      Rule 6 exists precisely to tell those two apart (`_judgment.py:182-189` — the caller's
      parameter is not evidence about the bytes), so the selector must ADMIT the foreign
      row and let Rule 6 refuse it.

    `seq` is compared through the injected `_exact_int` on BOTH sides, exactly as
    `_audit_row_targets` (`workspace.py:1280-1295`) does: a row carrying ``seq: True``
    does not match seq 1, and a caller passing ``True`` matches nothing.
    """
    return _scan_manifest_records(
        os.path.join(zmx_dir, "manifest.jsonl"),
        targets=lambda row: (isinstance(row, dict)
                             and row.get("event") == _AUDIT_EVENT
                             and _exact_int(row.get("seq"))
                             and _exact_int(seq)
                             and row.get("seq") == seq),
        validated=_validated_audit_row,
    )


def _refusal(family, reason, *, design_name, seq, read_state, record=None):
    """The contract failure envelope. ONE construction site, so no arm can drop a key."""
    remedy = _REMEDY.get(reason)
    return {
        "ok": False,
        "error_family": family,
        "error": reason if remedy is None else ("%s: %s" % (reason, remedy)),
        "design_name": design_name,
        "seq": seq,
        # NEVER a computed id on a refusal: the caller is never told ids landed that did
        # not (T8, `proposal-B-robust.md:249`).
        "finding_ids": None,
        "n_recorded": 0,
        "n_already_recorded": 0,
        "record": record,
        "read_state": read_state,
    }


def record_findings(session, params):
    """Record a vision reviewer's `findings` array against a saved candidate.

    NEVER raises — inspect ``result.ok``. Returns the contract envelope.

    THE ORDER OF THE NINE REFUSALS IS THE CONTRACT (first failure wins). Shape before
    binding, binding before picture, picture before any write: nothing is written until
    every identity fact is established, so a refused call leaves the manifest
    BYTE-IDENTICAL (T1) and the caller re-calls with nothing lost.

    IDENTITY IS TAKEN FROM THE ROW, NEVER FROM THE REQUEST. `filename`, `zmx_sha256` and
    `png_sha256` on the written row come from the VALIDATED `candidate_audit` row;
    `design_name` and `seq` are the LOOKUP KEY and are compared, not echoed;
    `finding_id` is COMPUTED. A top-level `filename` / `zmx_sha256` / `png_filename` /
    `finding_id` in ``params`` is NEVER READ — not refused, never read (T7, M5:
    the dispatcher checks MISSING required params only, `server.py:608-612`, and no tool
    in this harness carries a top-level unknown-key firewall; this one adds none).
    """
    # ---- Rule 1: the request's own shape. No read, no write, no engine. ----------
    if not isinstance(params, dict):
        return _refusal(_finding.FINDING_PARAM, _finding.BAD_PARAM,
                        design_name=None, seq=None, read_state=None)
    design_name = params.get("design_name")
    seq = params.get("seq")
    png_sha256 = params.get("png_sha256")
    findings = params.get("findings")

    # ``design_name`` is checked with `_writable_name` — the manifest ENCODABILITY rule
    # this repo already owns (`workspace.py:1087`) — not with a local `isinstance`: a
    # lone surrogate is a perfectly legal `str` that the manifest write dies on, and a
    # second opinion about what a writable name is is exactly the drift forbids.
    if not _writable_name(design_name):
        return _refusal(_finding.FINDING_PARAM, _finding.BAD_PARAM,
                        design_name=None, seq=None, read_state=None)
    if not _exact_int(seq):
        return _refusal(_finding.FINDING_PARAM, _finding.BAD_PARAM,
                        design_name=design_name, seq=None, read_state=None)
    if not _is_hex64(png_sha256):
        return _refusal(_finding.FINDING_PARAM, _finding.BAD_PARAM,
                        design_name=design_name, seq=seq, read_state=None)

    # ---- Rule 2: the findings themselves, through the WRITER'S OWN predicate. ----
    # `normalize_findings` is the ONE acceptance set: whatever it refuses here the
    # reader refuses on the way back in, because `_finding.validated_row` carries the
    # same clauses (amendment A1). This handler writes no second opinion about what a
    # finding is. A non-list `findings` answers Rule 1's `bad_param` from inside it —
    # passing the reviewer's whole REPLY OBJECT is that case.
    cores, finding_err = _finding.normalize_findings(
        findings, writable=_writable_name, exact_int=_exact_int)
    if finding_err is not None:
        return _refusal(_finding.FINDING_PARAM, finding_err,
                        design_name=design_name, seq=seq, read_state=None)

    # ---- The manifest the rows live in. --
    # `<design_dir>/candidates/zmx/manifest.jsonl` — what `_get_sink` builds and what
    # `promote_best` computes. Resolved in a guard: an unresolvable root must not raise
    # out of a tool documented to never raise, and it answers UNKNOWN (Rule 3), never "no
    # audit row" — again.
    try:
        zmx_dir = os.path.join(_design_dir(session, design_name), "candidates", "zmx")
    except Exception:  # noqa: BLE001 — an unresolvable root is UNKNOWN, not absent
        return _refusal(_finding.FINDING_UNBOUND, AUDIT_RECORD_UNREADABLE,
                        design_name=design_name, seq=seq,
                        read_state=_finding.READ_UNREADABLE)

    # ---- Rule 3 / Rule 4 / Rule 5: the binding row. --------------------------------------
    audit_rows, audit_state = _read_audit_records_seq(zmx_dir, seq)
    if audit_state == _finding.READ_UNREADABLE:
        return _refusal(_finding.FINDING_UNBOUND, AUDIT_RECORD_UNREADABLE,
                        design_name=design_name, seq=seq, read_state=audit_state)
    if not audit_rows:
        # ABSENT is a POSITIVELY ESTABLISHED absence and is a different fact from
        # unreadable: "save_candidate first" is a remedy, "repair the manifest"
        # is a different one, and collapsing them sends the caller to the wrong place.
        return _refusal(_finding.FINDING_UNBOUND, AUDIT_RECORD_ABSENT,
                        design_name=design_name, seq=seq,
                        read_state=_finding.READ_ABSENT)
    identities = {(r.get("design_name"), r.get("filename"), r.get("zmx_sha256"))
                  for r in audit_rows}
    if len(identities) > 1:
        # Two validated rows for one seq that DISAGREE about the bytes. Reported as
        # CONFLICTING and never as unreadable: "I read two rows and they disagree" is
        # not "I could not read it" (`_judgment.py:41-49`, 's sibling).
        return _refusal(_finding.FINDING_UNBOUND, AUDIT_RECORD_CONFLICTING,
                        design_name=design_name, seq=seq, read_state=audit_state)
    audit = audit_rows[0]

    # ---- Rule 6: the owner. ------------------------------------------------------
    if audit.get("design_name") != design_name:
        return _refusal(_finding.FINDING_UNBOUND, OWNER_MISMATCH,
                        design_name=design_name, seq=seq, read_state=audit_state)

    # ---- Rule 7: the replay guard — RE-DIGEST, never trust the row alone. --------
    filename = audit.get("filename")
    row_zmx = audit.get("zmx_sha256")
    if not (isinstance(filename, str) and filename.strip()):
        # `_validated_audit_row` does not constrain `filename`, so a row can validate
        # carrying a non-str. There is then no path to re-digest, which is
        # `zmx_unreadable` and NEVER `absent` (T4's second clause).
        return _refusal(_finding.FINDING_UNBOUND, ZMX_UNREADABLE,
                        design_name=design_name, seq=seq, read_state=audit_state)
    on_disk = _sha256_file(os.path.join(zmx_dir, filename))
    if on_disk is None:
        # UNKNOWN resolves toward the alarm: `_sha256_file` returns UNKNOWN, never "no
        # match" and never a match.
        return _refusal(_finding.FINDING_UNBOUND, ZMX_UNREADABLE,
                        design_name=design_name, seq=seq, read_state=audit_state)
    if on_disk != row_zmx:
        return _refusal(_finding.FINDING_UNBOUND, DIGEST_MISMATCH,
                        design_name=design_name, seq=seq, read_state=audit_state)

    # ---- Rule 8 / Rule 9: the PICTURE. ----------------------------------------------
    row_png = audit.get("png_sha256")
    if not _is_hex64(row_png):
        # `png_sha256: null` is the config-restore guard having DECLINED to bind the
        # figure (`workspace.py:2670-2682`). A finding about an unbindable picture is
        # not recordable at all — the contract makes it unrepresentable on disk, and this is
        # where that becomes a refusal WITH A REMEDY instead of a validator failure.
        return _refusal(_finding.FINDING_FIGURE_UNBOUND, PICTURE_UNBINDABLE,
                        design_name=design_name, seq=seq, read_state=audit_state)
    if png_sha256 != row_png:
        # *"if the design agent reviews some other PNG, T4 refuses"*
        # (`proposal-A-minimalist.md:165-167`).
        return _refusal(_finding.FINDING_FIGURE_UNBOUND, PICTURE_MISMATCH,
                        design_name=design_name, seq=seq, read_state=audit_state)

    # ---- The ids. Byte-identical findings within ONE reply are ONE finding. ---
    # Deduped BY ID, in REPLY ORDER, and the id is what decides sameness: two elements
    # differing only in key order, or in `[4,3]` vs `[3,4]`, are the same finding stated
    # twice (`proposal-A-minimalist.md:144`) and `finding_id` canonicalises both.
    ordered_ids, cores_by_id = [], {}
    for core in cores:
        fid = _finding.finding_id(core, row_png)
        if fid is None:
            # An uncomputable id is NOT A RECORD (amendment A3). Unreachable through Rule 2
            # — every core it builds is canonicalisable — but the never-raise contract
            # does not rest on that reasoning.
            return _refusal(_finding.FINDING_PARAM, _finding.BAD_PARAM,
                            design_name=design_name, seq=seq, read_state=audit_state)
        if fid not in cores_by_id:
            cores_by_id[fid] = core
            ordered_ids.append(fid)

    if not ordered_ids:
        # `findings: []` passes Rule 1-Rule 9 and writes NO row — SILENCE IS NOT A ROW (the contract row
        # 22). The binding was still established, which is the half T10 exists to pin:
        # `[]` with a bad picture is REFUSED, `[]` with a good one is a clean no-op.
        #
        # ▶ `read_state` here reports the AUDIT read that ESTABLISHED THE BINDING, not a
        # read-back, because there is nothing to read back. [INTERPRETATION] — the contract
        #   describes the receipt's state and is silent on the empty case; building a
        #   receipt anyway would report `absent` on a first call and `ok` on a later one
        #   for the SAME no-op, a state describing the manifest's history rather than
        #   this call.
        return {
            "ok": True,
            "design_name": design_name,
            "seq": seq,
            "filename": filename,
            "zmx_sha256": row_zmx,
            "png_sha256": row_png,
            "finding_ids": [],
            "n_recorded": 0,
            "n_already_recorded": 0,
            "record": "nothing_to_record",
            "read_state": audit_state,
        }

    # ---- Rule 10: what is ALREADY recorded (idempotency at (fid, zmx_sha256)). ---
    known_rows, known_state = _read_finding_records(zmx_dir,
                                                    design_names={design_name})
    if known_state == _finding.READ_UNREADABLE:
        return _refusal(_finding.FINDING_UNBOUND, FINDING_RECORD_UNREADABLE,
                        design_name=design_name, seq=seq, read_state=known_state)
    already = {(r.get("finding_id"), r.get("zmx_sha256")) for r in known_rows}

    ts = _io.utc_now_iso()
    new_rows, n_already = [], 0
    for fid in ordered_ids:
        if (fid, row_zmx) in already:
            # IDEMPOTENT: the same reply recorded twice leaves ONE row per finding. The
            # pair is `(fid, zmx_sha256)` and not `fid` alone, because the SAME finding
            # about DIFFERENT bytes is a new record under the Q3 design scope.
            n_already += 1
            continue
        new_rows.append(_finding.build_row(
            seq=seq, design_name=design_name, filename=filename,
            zmx_sha256=row_zmx, png_sha256=row_png,
            core=cores_by_id[fid], ts=ts))

    if new_rows:
        record = _write_finding_record(zmx_dir, rows=new_rows)
    else:
        record = "already_recorded"

    # ---- The receipt: from a RE-READ, never from the request. ----------------
    receipt = _finding_receipt(zmx_dir, design_name=design_name, seq=seq,
                               filename=filename, expected_ids=ordered_ids)
    read_state = receipt.get("read_state")
    recorded_ids = receipt.get("recorded_finding_ids")
    ok = read_state == "ok" and recorded_ids is not None

    envelope = {
        "ok": ok,
        "design_name": design_name,
        "seq": seq,
        "filename": filename,
        "zmx_sha256": row_zmx,
        "png_sha256": row_png,
        "finding_ids": recorded_ids if ok else None,
        "n_recorded": len(new_rows),
        "n_already_recorded": n_already,
        "record": record,
        "read_state": read_state,
    }
    if not ok:
        # A write that LANDS but does not READ BACK is `ok: false` with `record` still
        # reporting what the writer said. The three families are the binding
        # ones; this is a read fault about a record, so it travels as `finding_unbound`.
        envelope["error_family"] = _finding.FINDING_UNBOUND
        envelope["error"] = (
            "finding_record_unconfirmed: the write reported %r but the records could "
            "not be read back (read_state=%r); nothing is claimed to have landed"
            % (record, read_state))
    return envelope


RECORD_FINDINGS_SPEC = ToolSpec(
    name="record_findings",
    handler=record_findings,
    required_params=("design_name", "seq", "png_sha256", "findings"),
    param_types={
        "design_name": "string",
        # STRICT integer, on the `promote_best` precedent: `seq` is a workspace INDEX
        # compared with `_exact_int` on BOTH sides, so advertising `number` would
        # advertise a float as acceptable and then refuse it.
        "seq": "integer",
        "png_sha256": "string",
        # The reviewer's `findings` ARRAY, not the reply object. An `array`, so the
        # adapter's type-aware shim json.loads a string-coercing client's payload.
        "findings": "array",
    },
    description=(
        "Record a vision reviewer's findings against a saved candidate so the response "
        "to them is durable; NEVER raises - inspect result.ok. Pass the reviewer "
        "reply's findings ARRAY element-for-element unmodified (NOT the whole reply "
        "object - stamps_read/unreadable_stamps are loop control and are recorded "
        "nowhere), together with the design_name, seq and png_sha256 that "
        "save_candidate returned for the figure the reviewer was shown. Each finding is "
        "{config: int|null (REQUIRED, may be null), where: 'figure'|'system', key: int "
        "or [int,int] (REQUIRED at figure scope, FORBIDDEN at system scope), direction, "
        "note: non-empty}; direction is one of looks_tight | looks_generous | too_thin "
        "| too_thick | asymmetric | steep_bend | wrong_sign_suspected | "
        "not_the_expected_form | wrong_kind_element, and at 'system' scope ONLY "
        "not_the_expected_form and wrong_kind_element are accepted. One malformed "
        "finding refuses the WHOLE call (finding_param, bad_finding:<index>:<clause>) "
        "and writes nothing, so you are never left believing you recorded findings you "
        "did not. findings=[] is legal and records nothing - silence is not a row. "
        "The call binds to the bytes AND the picture: it refuses when no candidate_audit "
        "row exists for the seq or it could not be read (finding_unbound), when the seq "
        "belongs to another design (owner_mismatch), when the .zmx no longer digests to "
        "what was audited (digest_mismatch), and when no picture is bound to the "
        "candidate or your png_sha256 is not the one recorded (finding_figure_unbound) "
        "- review the figure the candidate actually published, and NOT a render_layout "
        "PNG: that picture is scratch, drawn for your own eyes, and its digest is never "
        "on a row. Recording is IDEMPOTENT: "
        "the same finding about the same bytes is stored once, so calling twice is safe "
        "and n_already_recorded says how many were already there. finding_ids are "
        "COMPUTED from the finding's own content plus the picture digest - they are "
        "never caller-supplied - and are returned from a RE-READ of the manifest, or "
        "null when the read-back did not establish them. Name those ids back in "
        "save_candidate / promote_best's judgment={'finding_ids': [...], 'disposition': "
        "..., 'reason': ...} to record your response. LIMITATION: this tool records a "
        "DECLARATION; it does not check the design, does not read your note, and "
        "decides nothing about whether the finding is right. See save_candidate, "
        "promote_best."
    ),
)

#: ▶ **REGISTRATION IS NOT AUTO-DISCOVERY** — the contract's last row says it
#: is, and that is WRONG: `server.load_manifest` imports a HAND-MAINTAINED module list
#: (`server.py:358-437`, `_SINGLE_SPEC_MODULES` / `_MULTI_SPEC_MODULES`) and collects
#: `TOOL_SPEC` / `TOOL_SPECS` from it. A module nobody adds to that list registers
#: nothing, SILENTLY — no import error, no missing tool, just a manifest one short.
#: Reported as amendment A17; this module is added to `_MULTI_SPEC_MODULES` in the same
#: change, and `test_served_schema_pin` is what would have caught the omission.
TOOL_SPECS = (RECORD_FINDINGS_SPEC,)
