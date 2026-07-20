"""tools/optimize_merit_io.py — native MFE save/load + clear/remove + recipe (the SAVING layer).

The dispatchable NATIVE/IO tools of the merit-builder cycle-1 PLUS the two RECIPE
tools (``serialize_merit`` / ``apply_merit_recipe``).
Thin typed wrappers over the live ``MFE`` save/load/clear/remove surface, each
durability-gated or read-back-proven, each returning the envelope shape:

- ``save_merit``     — ``mfe.SaveMeritFunction(path)`` then GATE the written file
  through the ``_merit_io._is_merit_file`` magic-byte oracle (§6). A
  0-byte / text-named ``.MF`` (the headless-``ToFile``-writes-TEXT trap) ->
  ``merit_save_unwritten``. The path resolves §5.3 (relative -> workspace
  ``projects/`` root; absolute -> as-is; empty/non-str -> ``merit_io_path``). An
  engine THROW -> ``merit_io``.
- ``load_merit``     — magic-check the file BEFORE the engine call (don't feed
  garbage to ``LoadMeritFunction``), then ``mfe.LoadMeritFunction(path)`` and read
  back ``NumberOfOperands``. Does NOT assert the count changed — a faithful reload
  of an identical merit legitimately leaves it unchanged (§6).
- ``clear_merit``    — ``mfe.DeleteAllRows()`` (returns the deleted-row count); the
  MFE floors at count 1 (one ``BLNK`` placeholder), NEVER 0. Returns
  ``deleted`` + ``before`` + the floored ``number_of_operands``. A post-clear count
  != 1 -> ``merit_clear``.
- ``remove_operand`` — client-side index bound (``_require_int_index`` +
  ``1..NumberOfOperands`` range — a bad index -> ``merit_remove``, the engine call
  is NEVER reached out of range), then ``mfe.RemoveOperandAt(n)`` and assert the
  count dropped by one (else ``merit_remove``).

The 2 RECIPE tools (``serialize_merit`` / ``apply_merit_recipe``) author / read the
portable Header-keyed recipe (§4/§5): MFE -> recipe dict (pure read) and
recipe -> MFE (two-phase validate-before-mutate + an atomic temp-``.MF`` checkpoint
whose OWN failure path is fail-closed). They reuse the SHARED ``_merit_cells`` author
core + the four IO tools above.

Live ZOS-API integration: exercised by the merit-recipe live test; unit-tested
against the fixture-seeded fake MFE.
"""
import math
import os
import tempfile

from .._io import safe_float
from ..enums import _resolve_enum
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _config_common as _ccfg
from . import _lens_common as _lc
from . import _merit_cells as _mc
from . import _merit_io as _mio
from . import _optimize_common as _oc


def save_merit(session, params):
    """Save the MFE to a native ``.MF`` file, magic-gated for durability (§6).

    Resolves the path (§5.3), calls ``mfe.SaveMeritFunction(path)`` (guarded -> a
    THROW is ``merit_io``), then GATES the written file through
    ``_merit_io._is_merit_file`` (the magic-byte oracle: UTF-16LE BOM + ``"VERS"``).
    A 0-byte / wrong-magic file -> ``merit_save_unwritten``. On success returns the
    resolved ``path`` + the on-disk ``bytes``.
    """
    system = session.system
    mfe = system.MFE

    resolved, err = _mio.resolve_merit_path(session, params.get("path"))
    if err is not None:
        return _oc.error_envelope("save_merit", "merit_io_path", err)

    # (persistence-workspace): makedirs BEFORE the write — SaveMeritFunction SILENTLY
    # no-ops on a missing directory, so a merit path under a folder
    # the agent never created vanished with a size-0 magic-byte failure. Create the
    # parent FIRST. Guarded -> a real unwritable parent becomes a structured
    # workspace_unwritable envelope (never raise).
    #
    # fix: the catch is ``(OSError, ValueError)``, NOT ``OSError`` alone.
    # A pathological path that passes ``resolve_merit_path`` but is illegal at the OS
    # layer (an embedded NUL — ``"sub\x00bad/cdf.MF"``) makes ``os.makedirs`` raise
    # ``ValueError`` (the illegal-path class), which would ESCAPE the ``except OSError``
    # and the handler as a raw raise — dispatch nets it as an opaque ``internal``,
    # losing the structured envelope every sibling save-site upholds. ValueError covers
    # the embedded-NUL / illegal-path class; it does NOT swallow a genuine logic bug
    # (the envelope carries the exception type + text for diagnosis, and the happy path
    # is contract-tested), it only routes the same write-impossible class to the same
    # ``workspace_unwritable`` envelope the OSError branch already returns.
    try:
        os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
    except (OSError, ValueError) as exc:
        return _oc.error_envelope(
            "save_merit",
            "workspace_unwritable",
            f"could not create the parent directory for {resolved!r} "
            f"({type(exc).__name__}: {exc})",
            path=resolved,
        )

    try:
        mfe.SaveMeritFunction(resolved)
    except Exception as exc:  # noqa: BLE001 — an engine save THROW -> merit_io, never internal
        return _oc.error_envelope(
            "save_merit",
            "merit_io",
            f"SaveMeritFunction threw on {resolved!r}: {exc!r}",
            path=resolved,
        )

    if not _mio._is_merit_file(resolved):
        # The save did not produce a real merit file (0-byte / text-named .MF — the
        # headless-ToFile-writes-TEXT trap the magic oracle exists to catch).
        try:
            size = os.path.getsize(resolved) if os.path.isfile(resolved) else 0
        except OSError:
            size = 0
        return _oc.error_envelope(
            "save_merit",
            "merit_save_unwritten",
            f"SaveMeritFunction did not write a valid merit file at {resolved!r} "
            f"(size={size}, magic-byte gate failed)",
            path=resolved,
            bytes=size,
        )

    return {"ok": True, "path": resolved, "bytes": os.path.getsize(resolved)}


def load_merit(session, params):
    """Load a native ``.MF`` into the MFE, magic-checked BEFORE the engine call (§6).

    Resolves the path (§5.3), REQUIRES the file exist + PASS the magic-byte oracle
    BEFORE ``LoadMeritFunction`` (don't feed garbage to the engine — a missing /
    wrong-magic file -> ``merit_io_path``), then ``mfe.LoadMeritFunction(path)``
    (guarded -> a THROW is ``merit_io``) and reads back ``NumberOfOperands``. Does
    NOT assert the count changed (a faithful reload of an identical merit
    legitimately leaves it unchanged).
    """
    system = session.system
    mfe = system.MFE

    resolved, err = _mio.resolve_merit_path(session, params.get("path"))
    if err is not None:
        return _oc.error_envelope("load_merit", "merit_io_path", err)

    if not _mio._is_merit_file(resolved):
        return _oc.error_envelope(
            "load_merit",
            "merit_io_path",
            f"{resolved!r} is not a readable native merit file (missing or failed "
            "the magic-byte gate); refusing to feed it to LoadMeritFunction",
            path=resolved,
        )

    try:
        mfe.LoadMeritFunction(resolved)
        number_of_operands = int(mfe.NumberOfOperands)
    except Exception as exc:  # noqa: BLE001 — an engine load THROW -> merit_io, never internal
        return _oc.error_envelope(
            "load_merit",
            "merit_io",
            f"LoadMeritFunction threw on {resolved!r}: {exc!r}",
            path=resolved,
        )

    # preserve_custom (§2.3): a loaded .MF REPLACES the whole MFE with a merit
    # that has NOTHING to do with the stored wizard-block boundary — the prior {B,sig} now
    # describes a merit that no longer exists. Invalidate it (alongside clear_merit +
    # load_design) so a later preserve_custom sees "no boundary" (first-use / refuse), not
    # a stale one that could mis-slice. Defense-in-depth: the §2.4 sig-hash staleness check
    # is authoritative (a loaded merit's TypeName[1..B] almost never matches sig), but the
    # delattr converts a confusing spurious refuse into the correct path. Never raises.
    if hasattr(session, "_merit_wizard_boundary"):
        delattr(session, "_merit_wizard_boundary")

    return {
        "ok": True,
        "path": resolved,
        "number_of_operands": number_of_operands,
    }


def clear_merit(session, params):
    """Clear the MFE via ``DeleteAllRows`` (floor = count 1).

    Reads ``before``, calls ``mfe.DeleteAllRows()`` (returns the deleted-row count),
    and reads the post-clear ``NumberOfOperands``. The MFE NEVER reaches 0 rows — it
    floors at 1 (one ``BLNK`` placeholder). A post-clear count != 1 -> ``merit_clear``
    (the floor invariant was violated). Idempotent: clearing an already-cleared MFE
    returns ``deleted: 0, number_of_operands: 1, ok: true``.
    """
    system = session.system
    mfe = system.MFE

    # fix: ``before`` + ``DeleteAllRows()`` + the post-clear count are raw .NET
    # calls. A degraded-engine THROW on any of them must become a structured
    # ``merit_clear`` envelope, never escape the handler as an opaque dispatch
    # ``internal`` (the firewall class).
    try:
        before = int(mfe.NumberOfOperands)
        deleted = mfe.DeleteAllRows()
        number_of_operands = int(mfe.NumberOfOperands)
    except Exception as exc:  # noqa: BLE001 — an engine clear THROW -> merit_clear
        return _oc.error_envelope(
            "clear_merit",
            "merit_clear",
            f"clearing the MFE threw ({exc!r}); the merit function may be in an "
            "indeterminate state — refusing rather than reporting a guessed count",
        )

    if number_of_operands != 1:
        return _oc.error_envelope(
            "clear_merit",
            "merit_clear",
            f"DeleteAllRows left the MFE at {number_of_operands} operands, expected "
            "the floor of 1 (one BLNK placeholder)",
            deleted=int(deleted),
            before=before,
            number_of_operands=number_of_operands,
        )

    # preserve_custom (§2.3): the wizard-block boundary is now meaningless — the MFE
    # floors at the BLNK placeholder, so any later ``preserve_custom`` MUST see "no
    # boundary" (first-use), not a stale one. Defense-in-depth: the §2.4 staleness check
    # is authoritative (``N < B`` would refuse anyway), but invalidating here converts a
    # confusing spurious refuse into the correct first-use path. Trivial + never raises.
    if hasattr(session, "_merit_wizard_boundary"):
        delattr(session, "_merit_wizard_boundary")

    return {
        "ok": True,
        "deleted": int(deleted),
        "before": before,
        "number_of_operands": number_of_operands,
    }


def remove_operand(session, params):
    """Remove operand row ``operand_number`` via ``RemoveOperandAt``.

    The index is validated CLIENT-SIDE (``_require_int_index`` rejects a bool /
    non-integral; then a ``1..NumberOfOperands`` range check) BEFORE the engine call
    — an out-of-range index -> ``merit_remove`` and ``RemoveOperandAt`` is NEVER
    reached out of range. After the remove the count must have dropped by one (else
    ``merit_remove``).
    """
    system = session.system
    mfe = system.MFE

    try:
        operand_number = _lc._require_int_index(params, "operand_number")
    except ToolParamError as exc:
        return _oc.error_envelope("remove_operand", "merit_remove", str(exc))

    before = int(mfe.NumberOfOperands)
    if not (1 <= operand_number <= before):
        return _oc.error_envelope(
            "remove_operand",
            "merit_remove",
            f"operand_number {operand_number} out of range; valid 1..{before} "
            f"(NumberOfOperands={before})",
            operand_number=operand_number,
            number_of_operands=before,
        )

    mfe.RemoveOperandAt(operand_number)
    after = int(mfe.NumberOfOperands)
    if after != before - 1:
        return _oc.error_envelope(
            "remove_operand",
            "merit_remove",
            f"RemoveOperandAt({operand_number}) did not drop the operand count by "
            f"one (before={before}, after={after})",
            operand_number=operand_number,
            number_of_operands=after,
        )

    return {
        "ok": True,
        "removed": operand_number,
        "number_of_operands": after,
    }


# --------------------------------------------------------------------------- #
# The RECIPE layer (§4/§5).
# --------------------------------------------------------------------------- #
_RECIPE_SCHEMA = "optivibe.merit-recipe"
_RECIPE_VERSION = 1

# The KNOWN per-operand recipe-entry keys (§4). An operand-LEVEL key NOT in
# this set is a typo and is REJECTED (tolerant-outer / strict-inner, §4). The
# OPTIONAL ``value_at_capture`` sibling ``serialize_merit`` may emit is tolerated on
# apply (it is NEVER replayed); it is included so a round-tripped recipe re-applies.
_OPERAND_KEYS = frozenset(
    {"type", "params", "refs", "target", "weight", "value_at_capture"}
)

# A structural-noise floor / wizard-spacer row: its ``TypeName`` is the
# placeholder mnemonic. Skipped by ``serialize_merit`` (re-created implicitly).
_BLANK_OPERAND = "BLNK"

# The wizard's merit-function START marker row (live-probed: ``build_merit`` emits it
# as row 1 with ``Target == Weight == inf`` — an infinite SENTINEL, not a real target).
# It is a STRUCTURAL marker (no replayable params/target), in the SAME class as the
# ``BLNK`` spacer: ``serialize_merit`` SKIPS it so the portable recipe carries only
# REAL authored operands and ``apply_merit_recipe`` never has to replay an ``inf``
# target/weight (the live gate caught the wizard's ``DMFS`` row otherwise diverging
# the round-trip — its ``inf`` target/weight serialize to the string ``"inf"`` which the
# strict-number recipe validator rightly refuses).
#
# fix: the skip is GATED on the sentinel SHAPE (a non-finite Target/Weight), NOT on
# the bare ``TypeName`` — a member of this set with a FINITE Target/Weight is a REAL
# authored operand and is serialized faithfully (a bare-name skip would drop it).
_STRUCTURAL_MARKER_OPERANDS = frozenset({_BLANK_OPERAND, "DMFS"})


def _serialize_with_rowmap(session, params):
    """Serialize the MFE to a recipe AND expose the ``live_to_index`` row-map (§5.1).

    The internal core of ``serialize_merit``: returns ``(envelope, live_to_index)``
    where ``envelope`` is the byte-identical public serialize envelope (an ok payload
    OR an error family) and ``live_to_index`` maps each SERIALIZED live row -> its
    0-based recipe index (the SAME map the recipe's Op#-remap is built from). The
    ``preserve_custom`` flow derives the recipe-space wizard boundary ``W`` FRESH from
    this map (§2.1) — ``W`` is never a stored value, so it can never drift from the
    slice it indexes. On EVERY early return the current (possibly partial) map is
    returned alongside the envelope.

    Read the MFE into a portable Header-keyed recipe dict (§4). PURE READ.

    Walks ``1..NumberOfOperands``, and for each operand reads ``TypeName`` (skipping
    a ``BLNK`` structural-noise row), its type-aware ``read_param_map`` (Header-keyed
    ``{Header: value}``), and ``Target`` / ``Weight``. A per-operand
    ``value_at_capture`` (the operand ``Value``) is emitted for HUMAN diffing only —
    ``apply_merit_recipe`` NEVER replays it. Stamps ``schema`` /``version`` (§4).

    **Math scaffold (§3.2):** a derived/relational operand carries its
    operand-ROW REFERENCES in ``Op#``-prefixed int cells. A live row number is NOT a
    portable recipe index (``serialize`` SKIPS structural rows, so the recipe index !=
    the live row). Each operand's cells are partitioned into NON-ref ``params``
    vs ``Op#`` ``refs``, and a SECOND in-Python pass (no engine touch — the recipe is
    already in memory) translates each resolved raw live row -> its 0-based recipe
    index. A two-pass shape is REQUIRED so a FORWARD reference (a row referencing
    an operand authored AFTER it) resolves. An UNSET ``Op#`` = 0 (the as-built
    default) is kept as a LITERAL in ``params`` (NOT in ``refs``); a non-zero raw
    row into a skipped/structural row CANNOT be expressed as an index -> serialize
    fail-closes ``merit_recipe_invalid`` (a silently-zeroed ref would MANUFACTURE the
    failure into a stored artifact). ``refs`` is added to an entry ONLY when
    non-empty (a non-math operand stays byte-identical to cycle-1).

    NEVER mutates the MFE (no ``AddOperand`` / ``ChangeType`` / cell write). A
    parameter-cell read THROW surfaces as ``SurfaceWriteError`` (the ``_merit_cells``
    firewall) which dispatch converts to ``surface_write``; the read of
    ``TypeName`` / ``Target`` / ``Weight`` is guarded so a transient proxy gap is a
    structured ``merit_io`` envelope, never an opaque internal.
    """
    system = session.system
    mfe = system.MFE
    # ``live_to_index`` maps a serialized live row -> its 0-based recipe index; it is
    # initialised HERE (before the count read) so EVERY early-return — including the
    # count-throw before the loop — can return the (possibly empty) map alongside the
    # envelope. ``preserve_custom`` derives ``W`` from it (§2.1).
    live_to_index = {}
    # fix: the top-of-loop ``NumberOfOperands`` read is a raw .NET getter; a
    # degraded-engine THROW must become a structured ``merit_io`` envelope, not an
    # opaque dispatch ``internal`` (the same firewall the inner reads already honor).
    try:
        number_of_operands = int(mfe.NumberOfOperands)
    except Exception as exc:  # noqa: BLE001 — a count read THROW -> merit_io
        return _oc.error_envelope(
            "serialize_merit",
            "merit_io",
            f"could not read NumberOfOperands: {exc!r}",
        ), live_to_index

    # (live-probed): on a MULTI-config system the engine
    # AUTO-SEEDS a leading CONF bracket at row 1 (the current-config marker) the moment
    # any operand is added, and RE-CREATES it on apply (clear -> author). Serializing
    # that auto-seed produces a DUPLICATE config-1 block on round-trip (the live gate
    # caught a 3rd CONF entry from a 2-CONF authored merit; apply-replace would double
    # the config-1 block). Read the config count so the loop can SKIP the auto-seeded
    # row-1 CONF. THROW-guarded -> 1 on a fresh / non-MCE system, so a single-config
    # serialize is byte-identical to before (the skip never fires).
    n_configs = _ccfg.safe_number_of_configurations(system)

    operands = []
    # the recipe index of an operand IS its append position; a structural/skipped
    # row gets NO entry, so ``live_to_index.get(skipped_row) -> None`` (the "skipped"
    # sentinel a dangling ref maps to). Built AS the non-structural rows are appended
    # (initialised at the top of the function so the pre-loop early-returns carry it).
    for i in range(1, number_of_operands + 1):
        # fix: ``GetOperandAt(i)`` is also a raw .NET read — guard it the same way.
        # ``raw_target`` / ``raw_weight`` are the unstringified floats kept for the
        # finiteness gate (``safe_float`` stringifies a non-finite value, so the gate
        # must test the RAW float, not the serialized value).
        try:
            op = mfe.GetOperandAt(i)
            type_name = str(op.TypeName)
            raw_target = op.Target
            raw_weight = op.Weight
            target = safe_float(raw_target)
            weight = safe_float(raw_weight)
            value = safe_float(op.Value)
        except Exception as exc:  # noqa: BLE001 — a row read THROW -> merit_io
            return _oc.error_envelope(
                "serialize_merit",
                "merit_io",
                f"could not read operand row {i}: {exc!r}",
                operand_number=i,
            ), live_to_index
        # fix: skip a BLNK/DMFS marker row ONLY when it carries the STRUCTURAL
        # sentinel shape (a non-finite Target/Weight — BLNK reads nan, DMFS reads inf
        # per the probe), NOT on the bare TypeName. A REAL authored operand whose
        # TypeName happens to be ``DMFS`` always has FINITE Target/Weight and must
        # NEVER be dropped (the round-trip data-loss §4 names directly).
        if type_name in _STRUCTURAL_MARKER_OPERANDS and not (
            _is_finite_number(raw_target) and _is_finite_number(raw_weight)
        ):
            # A structural marker row (the ``BLNK`` spacer / floor with a nan
            # Target/Weight, or the wizard's ``DMFS`` merit-START marker whose
            # Target/Weight are an ``inf`` sentinel): re-created implicitly on apply
            # (§4 — round-trip compares REAL operands only). The non-finite gate keeps
            # an ``inf``/``nan`` target/weight out of the portable recipe (the
            # live-gate divergence) WITHOUT dropping a finite-target real operand.
            # Skipped rows get NO live_to_index entry (the dangling sentinel).
            continue

        # SKIP the engine's AUTO-SEEDED leading config
        # bracket — the CONF at live row 1 on a MULTI-config system. The engine maintains
        # exactly one leading CONF at the top of a multi-config MFE and RE-CREATES it on
        # apply, so serializing it would DOUBLE the config-1 block on round-trip (the
        # live gate caught the 3rd CONF). Scoped to i == 1 (the auto-seed is ALWAYS the
        # top row; a hand-authored CONF can never BE row 1 on a multi-config MFE — the
        # engine's auto-CONF precedes it) and n_configs > 1 (no auto-seed on a single-
        # config system). Like a structural skip, it gets NO live_to_index entry (the
        # engine re-creates it; CONF carries no Op# refs so nothing can dangle to it).
        if i == 1 and n_configs > 1 and _mc.is_valueless_control(type_name):
            continue

        # Type-aware param read (the fix lives in read_param_map). A cell read
        # THROW raises SurfaceWriteError -> surface_write at the dispatch boundary.
        param_map = _mc.read_param_map(op)
        # partition the cells into NON-ref ``params`` vs ``Op#`` row refs. The
        # ref cells are translated to recipe indices in the SECOND pass (below).
        recipe_params = {
            h: param_map[h]["value"]
            for h in param_map
            if not _mc.is_row_ref_header(h, param_map[h]["kind"])
        }
        raw_refs = _mc.read_ref_map(op)  # {Header: raw_live_row} (Op# cells only)

        index = len(operands)
        live_to_index[i] = index
        # a value-less control operand (CONF) has a NON-FINITE
        # Target/Weight (the inf sentinel) — do NOT serialize them (the round-trip-break
        # the strict-number recipe validator rightly refuses). Carry ONLY
        # type + params (the Cfg# proof cell) [+ refs]. NOT a structural skip (CONF must
        # be PRESERVED, unlike BLNK/DMFS) — it carries its semantic cell.
        entry = {
            "type": type_name,
            "params": recipe_params,
            # Transient build keys (popped before emit): the raw Op# rows + the
            # live source row, resolved index->recipe in the second pass.
            "_raw_refs": raw_refs,
            "_src_row": i,
        }
        if not _mc.is_valueless_control(type_name):
            entry["target"] = target
            entry["weight"] = weight
            entry["value_at_capture"] = value  # human diffing only; NEVER replayed.
        operands.append(entry)

    # ---- Second pass: translate raw live rows -> 0-based recipe indices (§3.2). ----
    # The recipe is already in memory — NO engine touch. A forward reference resolves
    # because every non-structural row already has a live_to_index entry by now.
    for entry in operands:
        raw_refs = entry.pop("_raw_refs")
        src_row = entry.pop("_src_row")
        resolved = {}
        for header, raw_live_row in raw_refs.items():
            if raw_live_row == 0:
                # UNSET (the as-built default): keep as a LITERAL in params (the
                # unset default lives in the params channel), do NOT translate.
                entry["params"][header] = 0
                continue
            mapped = live_to_index.get(raw_live_row)
            if mapped is None:
                # A non-zero raw row into a skipped/structural/out-of-range row — NOT
                # expressible as a recipe index. Fail-closed (§3.2): a silently-zeroed
                # ref would MANUFACTURE the failure into a stored artifact.
                return _oc.error_envelope(
                    "serialize_merit",
                    "merit_recipe_invalid",
                    f"operand row {src_row} ({entry['type']}) references row "
                    f"{raw_live_row} via {header!r}, but that row was skipped "
                    "(structural) or is out of range — the reference cannot be "
                    "expressed as a portable recipe index; refusing to emit a recipe "
                    "that would silently dangle the reference to 0.0",
                    operand_number=src_row,
                ), live_to_index
            resolved[header] = mapped
        # ``refs`` is added ONLY when non-empty (a non-math operand stays refs-free ->
        # byte-identical to cycle-1).
        if resolved:
            entry["refs"] = resolved

    recipe = {
        "schema": _RECIPE_SCHEMA,
        "version": _RECIPE_VERSION,
        "operands": operands,
    }
    return {
        "ok": True,
        "recipe": recipe,
        "number_of_operands": number_of_operands,
    }, live_to_index


def serialize_merit(session, params):
    """Read the MFE into a portable Header-keyed recipe dict (§4). PURE READ.

    The dispatchable PUBLIC tool: a THIN wrapper over ``_serialize_with_rowmap`` that
    returns ONLY the envelope. The ``live_to_index`` row-map is an INTERNAL detail
    (``preserve_custom``'s ``W`` derivation) and is NEVER leaked into the public return
    — the envelope is byte-identical to before the extraction (regression-guarded by
    ``test_serialize_public_envelope_unchanged``).
    """
    envelope, _live_to_index = _serialize_with_rowmap(session, params)
    return envelope


def _validate_recipe_schema(recipe):
    """Validate the recipe top-level schema/version (§4/§5 Phase-1 step 1).

    Returns ``(error_family, message)`` on a malformed recipe, or ``(None, None)``
    when the top-level shape is acceptable:

    - a non-dict recipe / a bad ``schema`` / a missing or ``< 1`` ``version`` /
      an ``operands`` that is not a list -> ``("merit_recipe_schema", msg)``;
    - a ``version > 1`` (newer than supported) -> ``("merit_recipe_version", msg)``;
    - UNKNOWN TOP-LEVEL keys are TOLERATED (forward-compat, §4 tolerant-outer).

    Per-operand strict-inner key validation happens in ``_validate_operand_entry``.
    """
    if not isinstance(recipe, dict):
        return "merit_recipe_schema", (
            f"recipe must be a dict, got {type(recipe).__name__}"
        )

    schema = recipe.get("schema")
    if schema != _RECIPE_SCHEMA:
        return "merit_recipe_schema", (
            f"recipe schema must be {_RECIPE_SCHEMA!r}, got {schema!r}"
        )

    version = recipe.get("version")
    # version MUST be a plain int (a bool is an int subclass — reject it) >= 1.
    if isinstance(version, bool) or not isinstance(version, int):
        return "merit_recipe_schema", (
            f"recipe version must be an integer >= 1, got {version!r}"
        )
    if version < 1:
        return "merit_recipe_schema", (
            f"recipe version must be >= 1, got {version!r}"
        )
    if version > _RECIPE_VERSION:
        return "merit_recipe_version", (
            f"recipe version {version} is newer than the supported version "
            f"{_RECIPE_VERSION}; upgrade the merit-builder to apply it"
        )

    operands = recipe.get("operands")
    if not isinstance(operands, list):
        return "merit_recipe_schema", (
            f"recipe 'operands' must be a list, got {type(operands).__name__}"
        )

    return None, None


def _validate_operand_entry(entry, index):
    """Validate ONE operand entry's shape (strict-inner key check, §4). Pure.

    Returns ``(error_message_or_None)``: an operand entry that is not a dict, OR
    carries an UNKNOWN operand-level key (a typo'd ``"targett"`` MUST NOT silently
    drop the target — §4 strict-inner), OR a non-str/empty ``type`` -> a message;
    a clean entry -> ``None``. Does NOT touch the engine (param-name + value
    validation is the per-type-signature step in the caller).
    """
    if not isinstance(entry, dict):
        return f"operand[{index}] must be a dict, got {type(entry).__name__}"
    unknown = [k for k in entry if k not in _OPERAND_KEYS]
    if unknown:
        return (
            f"operand[{index}] has unknown key {unknown[0]!r}; "
            f"valid keys: {sorted(_OPERAND_KEYS)}"
        )
    op_type = entry.get("type")
    if not isinstance(op_type, str) or op_type == "":
        return f"operand[{index}] 'type' must be a non-empty string, got {op_type!r}"
    return None


def _signature_cache_for_type(mfe, member, op_type):
    """Read one operand type's live param signature ZERO-NET-MUTATION (§5).

    Builds a throwaway operand of ``op_type``, ``read_param_map`` it (the live
    ``{Header: kind}`` signature), then ``RemoveOperandAt`` the throwaway so the MFE
    is left exactly as before (count ``88 -> 88``). The throwaway is removed in a
    ``finally`` so a read THROW still reaps the scratch row.

    Returns ``{Header: kind}`` (the kind, not the col — the apply path re-reads the
    live col off the real operand it authors). Raises ``SurfaceWriteError`` on a cell
    read firewall fault (the caller turns Phase-1 faults into an error LIST, but a
    raw .NET fault here is a genuine engine problem -> ``surface_write``).

    fix (§5.4): the scratch reap is keyed on ``count_before`` (the count
    captured BEFORE ``AddOperand``), NOT on ``op.OperandNumber`` — because the
    ``OperandNumber`` read can ITSELF throw AFTER the row was appended. ``AddOperand``
    APPENDS, so the scratch row is the highest-index row (``count_before + 1``); the
    ``finally`` reaps THAT row even if ``OperandNumber`` never returned, preserving
    zero-net-mutation. A raw engine THROW on the ``OperandNumber`` probe is normalized
    to ``SurfaceWriteError`` so it surfaces as a structured ``surface_write`` envelope
    (the firewall), never an opaque dispatch ``internal``.

    fix (§5.4, whole-class sweep): the SIBLING raw .NET reads on this
    signature-probe path — ``mfe.NumberOfOperands`` (the ``count_before`` read
    added above), ``mfe.AddOperand()``, and ``op.ChangeType(member)`` — get the SAME firewall
    treatment as ``OperandNumber``: a degraded-engine THROW on any of them is normalized
    to a structured ``SurfaceWriteError`` (which the Phase-1 caller already records as a
    per-entry ``merit_recipe_invalid``), never escaping as an opaque dispatch
    ``internal``. Ordering preserves zero-net-mutation: ``count_before`` and
    ``AddOperand`` are guarded BEFORE the ``finally``-reap is armed, so a throw from
    either reaps NOTHING (no row was added); the ``finally`` is entered ONLY after the
    append is known to have succeeded, so a later probe THROW (``ChangeType`` /
    ``OperandNumber`` / ``read_param_map``) still reaps the appended scratch row.
    """
    # Capture the count BEFORE the append so the reap is deterministic regardless of
    # whether the OperandNumber read below succeeds (AddOperand appends -> the
    # scratch row is count_before + 1). Both this count read and AddOperand are
    # guarded BEFORE the finally-reap is armed — a throw here reaps NOTHING (no row was
    # added yet) and surfaces as a structured SurfaceWriteError, never an internal.
    try:
        count_before = int(mfe.NumberOfOperands)
    except Exception as exc:  # noqa: BLE001 — a count read THROW -> structured surface_write
        raise SurfaceWriteError(
            f"could not read NumberOfOperands before probing the signature of "
            f"{op_type} ({exc!r}); refusing rather than guessing",
            field="operand_number",
            intended=op_type,
            actual=None,
            surface=None,
        ) from exc
    try:
        op = mfe.AddOperand()
    except Exception as exc:  # noqa: BLE001 — an AddOperand THROW -> structured surface_write
        # The append itself threw: there is NO scratch row to reap (zero-net-mutation
        # holds without arming the finally). Surface a structured SurfaceWriteError.
        raise SurfaceWriteError(
            f"could not add a scratch operand while probing the signature of "
            f"{op_type} ({exc!r}); the engine rejected the append — refusing rather "
            "than guessing",
            field="operand_number",
            intended=op_type,
            actual=None,
            surface=None,
        ) from exc
    # The scratch row IS now appended; from here every exit MUST reap it (the finally).
    try:
        try:
            scratch_number = int(op.OperandNumber)
        except Exception as exc:  # noqa: BLE001 — a probe THROW -> structured surface_write
            raise SurfaceWriteError(
                f"could not read the scratch OperandNumber while probing the "
                f"signature of {op_type} ({exc!r}); refusing rather than guessing",
                field="operand_number",
                intended=op_type,
                actual=None,
                surface=None,
            ) from exc
        if scratch_number != count_before + 1:
            # AddOperand must append (count_before + 1); a divergence means the engine
            # renumbered or inserted elsewhere — refuse rather than reap the wrong row.
            raise SurfaceWriteError(
                f"scratch operand for {op_type} landed at row {scratch_number}, "
                f"expected the appended row {count_before + 1}; refusing rather than "
                "reaping an unexpected row",
                field="operand_number",
                intended=count_before + 1,
                actual=scratch_number,
                surface=None,
            )
        # fix: ChangeType is a raw .NET call on the scratch row; a degraded-engine
        # THROW (distinct from the engine cleanly returning False) must surface as a
        # structured SurfaceWriteError (the firewall), never an opaque internal. The
        # scratch row is already appended, so the finally still reaps it. A clean False
        # return stays the ParamCoercionError bad-type path (unchanged behavior).
        try:
            changed = bool(op.ChangeType(member))
        except Exception as exc:  # noqa: BLE001 — a ChangeType THROW -> structured surface_write
            raise SurfaceWriteError(
                f"could not change the scratch operand to type {op_type} while probing "
                f"its signature ({exc!r}); refusing rather than guessing",
                field="operand_type",
                intended=op_type,
                actual=None,
                surface=None,
            ) from exc
        if not changed:
            # The engine rejected the type on the scratch row — treat as a bad type.
            raise _mc.ParamCoercionError(
                f"engine rejected operand type {op_type} on the scratch row"
            )
        live = _mc.read_param_map(op)
        return {header: live[header]["kind"] for header in live}
    finally:
        # remove the throwaway so the MFE is zero-net-mutated. Reap by the
        # captured append position (count_before + 1) so the scratch row is removed
        # EXACTLY even when the OperandNumber read threw. Guarded — a remove
        # THROW must not mask the original read outcome.
        try:
            mfe.RemoveOperandAt(count_before + 1)
        except Exception:  # noqa: BLE001 — best-effort scratch reap
            pass


def _validate_refs(entry, this_index, sig, op_type, n_operands, entry_errs):
    """Validate ONE entry's parallel ``refs`` map (math scaffold §5.1/§5.2). Pure.

    ZERO net mutation — pure recipe-index arithmetic over the in-hand ``operands``
    list (reads NOTHING new from the engine; reuses the per-type ``sig`` the params
    block already built). ALL errors are COLLECTED into ``entry_errs`` (no fail-fast).

    Cases (§5.2):

    - ``refs`` absent / ``None`` -> treat as ``{}`` (no error — an operand with no refs);
    - ``refs`` not a dict -> error;
    - a ``refs`` KEY not in the live signature -> error (no such parameter);
    - a ``refs`` KEY present but NOT an ``Op#``-prefixed int cell on this type
      (``is_row_ref_header`` False) -> error (refs are ONLY for row-ref cells; the
      the OSCD ``Wave`` int-but-not-a-ref guard);
    - a ``refs`` VALUE that is a ``bool`` or not an ``int`` -> error (bool is an int
      subclass — reject; an index is an EXACT int);
    - **out-of-range** (``index < 0`` OR ``index >= n_operands``) -> REJECT;
    - **forward** (``index > this_index``) / **self** (``index == this_index``) /
      backward-in-range -> ALLOW (the mechanical round-trip charter resolves any
      in-range index via the two-phase apply; stale-read semantics are Phase C).
    """
    refs = entry.get("refs")
    if refs is None:
        return
    if not isinstance(refs, dict):
        entry_errs.append(
            f"refs must be a dict {{Header: index}}, got {type(refs).__name__}"
        )
        return
    for header, index in refs.items():
        if header not in sig:
            entry_errs.append(
                f"operand {op_type} has no parameter {header!r} for a row reference; "
                f"valid: {sorted(sig)}"
            )
            continue
        if not _mc.is_row_ref_header(header, sig.get(header)):
            entry_errs.append(
                f"refs key {header!r} is not an Op#-prefixed integer row-reference "
                f"cell on operand {op_type}; refs are only for row-ref cells"
            )
            continue
        if isinstance(index, bool) or not isinstance(index, int):
            entry_errs.append(
                f"refs[{header!r}] must be an integer recipe index, got "
                f"{type(index).__name__} {index!r}"
            )
            continue
        if index < 0 or index >= n_operands:
            entry_errs.append(
                f"refs[{header!r}] index {index} is out of range; an authored recipe "
                f"must reference an operand in 0..{n_operands - 1}"
            )
            # forward/self/backward-in-range are ALLOWED un-validated this cycle.


def _phase1_validate(system, mfe, recipe):
    """Phase-1 DRY validation: zero engine MUTATION (§5 Phase 1).

    1. Top-level schema/version (``_validate_recipe_schema``) -> a hard
       ``(family, message)`` reject (MFE untouched).
    2. For EVERY operand entry, WITHOUT a net mutation: strict-inner key check;
       resolve ``type`` vs live ``MeritOperandType``; coerce ``target`` / ``weight``;
       validate each ``params`` NAME against the operand's live signature (a lazily
       built per-type cache, zero-net-mutation) + coerce each ``params`` VALUE.
       ALL errors are COLLECTED into a per-entry list.
    3. Returns ``(family, message, errors)``: a top-level reject is
       ``(family, message, None)``; a clean recipe is ``(None, None, [])``; a
       per-entry failure list is ``("merit_recipe_invalid", None, [ {index,type,
       error}, ... ])``.

    The per-type signature cache is bounded by the number of DISTINCT operand types
    in the recipe (NOT the recipe length). It is built via throwaway operands that
    are ``RemoveOperandAt``-reaped (proven zero-net-mutation).
    """
    family, message = _validate_recipe_schema(recipe)
    if family is not None:
        return family, message, None

    enum_type = _oc._merit_operand_enum(system)
    signature_cache = {}        # {op_type: {Header: kind}}
    member_cache = {}           # {op_type: live enum member}
    errors = []
    # The recipe length bounds a valid 0-based ``refs`` index (§5.2 out-of-range).
    n_operands = len(recipe["operands"])

    for index, entry in enumerate(recipe["operands"]):
        shape_err = _validate_operand_entry(entry, index)
        if shape_err is not None:
            errors.append({"index": index, "type": entry.get("type")
                           if isinstance(entry, dict) else None, "error": shape_err})
            continue

        op_type = entry["type"]

        # Resolve the operand type vs the live enum (cache the member for Phase 2).
        if op_type not in member_cache:
            try:
                member_cache[op_type] = _resolve_enum(enum_type, op_type)
            except ToolParamError as exc:
                errors.append({"index": index, "type": op_type, "error": str(exc)})
                # Cannot read the signature of an unknown type — skip the rest.
                continue

        # Coerce target/weight (a non-number -> a recorded error, no mutation).
        # a value-less control operand authors NO Target/Weight (the
        # inf sentinel) — do NOT validate them (tolerate + ignore a stray legacy
        # target/weight key; the authored proof is the semantic cell, range-checked
        # below).
        entry_errs = []
        if not _mc.is_valueless_control(op_type):
            for key, default in (("target", 0.0), ("weight", 1.0)):
                if key in entry:
                    val = entry[key]
                    if isinstance(val, bool) or not isinstance(val, (int, float)):
                        entry_errs.append(
                            f"{key} must be a number, got "
                            f"{type(val).__name__} {val!r}"
                        )

        # Build (lazily, zero-net-mutation) the per-type signature ONCE — both the
        # params and the refs blocks below reuse it (§5.4: no extra engine touch).
        sig = None
        if op_type not in signature_cache:
            try:
                signature_cache[op_type] = _signature_cache_for_type(
                    mfe, member_cache[op_type], op_type
                )
            except _mc.ParamCoercionError as exc:
                entry_errs.append(str(exc))
                signature_cache[op_type] = {}
            except SurfaceWriteError as exc:
                # fix (§5.4): a probe-time engine fault (the scratch
                # ``OperandNumber`` / ``read_param_map`` THROW, already normalized to a
                # structured ``SurfaceWriteError`` with the scratch row reaped in
                # ``_signature_cache_for_type``'s finally) is RECORDED as a per-entry
                # validation error rather than ESCAPING ``apply_merit_recipe`` — so the
                # handler still returns a structured ``merit_recipe_invalid`` envelope
                # (never an opaque dispatch ``internal``, never a raw escape) while
                # Phase-1 stays zero-net-mutation (the scratch was reaped).
                entry_errs.append(
                    f"could not probe the live signature of {op_type}: {exc}"
                )
                signature_cache[op_type] = {}
        sig = signature_cache[op_type]

        # Validate params NAME + VALUE against the live per-type signature.
        recipe_params = entry.get("params", {})
        if recipe_params is None:
            recipe_params = {}
        if not isinstance(recipe_params, dict):
            entry_errs.append(
                f"params must be a dict {{Header: value}}, got "
                f"{type(recipe_params).__name__}"
            )
        else:
            for header, value in recipe_params.items():
                if header not in sig:
                    entry_errs.append(
                        f"operand {op_type} has no parameter {header!r}; "
                        f"valid: {sorted(sig)}"
                    )
                    continue
                try:
                    _mc.coerce_param_value(header, sig[header], value)
                except _mc.ParamCoercionError as exc:
                    entry_errs.append(str(exc))
                # §5.3 LOUD raw-Op#-in-params guard: an Op#-prefixed cell on this
                # operand type carrying a NON-ZERO value in the ``params`` channel is an
                # un-remapped raw row reference (the cycle-1 silent-wrong case — cycle-1
                # serialize dumped Op# cells as raw ints in params). An unset Op#=0 is
                # ALLOWED (the literal default). References belong in ``refs``.
                #
                # fix (§5.3): key the non-zero check on the SAME integer-acceptance
                # rule the WRITER uses (``_mc.row_ref_int`` — reject bool, accept exact
                # int AND integral float). An integral float ``7.0`` is coerced to
                # ``int(7)`` by ``coerce_param_value`` and WRITTEN as raw row 7, so the
                # old ``isinstance(value, int)`` conjunct let it slip the guard (the
                # dangling-ref bypass). A non-integral float / non-number Op# value is
                # NOT a row pointer here — ``coerce_param_value`` already rejected it
                # above (its int-cell error is in ``entry_errs``); ``row_ref_int``
                # returns ``None`` so we do not double-handle it.
                if _mc.is_row_ref_header(header, sig.get(header)):
                    effective_row = _mc.row_ref_int(value)
                    if effective_row is not None and effective_row != 0:
                        entry_errs.append(
                            f"a non-zero Op# in the params channel ({header!r} = "
                            f"{value}) is an un-remapped raw row reference; references "
                            "belong in 'refs', not 'params'"
                        )

        # range-validate a value-less control operand's proof param
        # (CONF -> Cfg# in 1..NumberOfConfigurations) PRE-mutation, the negative-gate
        # firewall (an out-of-range config pin would be a silent dangling reference). The
        # Cfg# NAME + integer kind were already validated by the params loop above; this
        # adds the presence + range check via the SAME shared helper add_operand uses.
        if _mc.is_valueless_control(op_type):
            verr = _mc.validate_valueless_control(
                op_type, recipe_params,
                n_configs=_ccfg.safe_number_of_configurations(system),
            )
            if verr is not None:
                entry_errs.append(verr)

        # §5.1/§5.2 the parallel ``refs`` map: a 0-based recipe index per Op# cell.
        # ZERO net mutation (pure recipe-index arithmetic over the in-hand list) +
        # reuses the SAME per-type ``sig``. ALL errors collected (no fail-fast).
        _validate_refs(entry, index, sig, op_type, n_operands, entry_errs)

        if entry_errs:
            errors.append(
                {"index": index, "type": op_type, "error": "; ".join(entry_errs)}
            )

    if errors:
        return "merit_recipe_invalid", None, errors
    return None, None, []


def _author_recipe_operand(mfe, system, entry):
    """Author ONE recipe operand onto the MFE via the SHARED cell-write core (§5).

    The ONE cell-write truth: ``AddOperand`` -> ``ChangeType`` -> ``apply_params``
    (the shared ``_merit_cells`` validate+coerce+read-back-proven write) ->
    Target/Weight DIRECT + read-back. Phase-1 already validated every name/value, so
    a failure here can only be an engine-side write rejection (a ``SurfaceWriteError``
    read-back mismatch) — which is RAISED for the atomic-rollback caller to catch.

    Returns the authored ``operand_number``. Raises ``SurfaceWriteError`` /
    ``CellLayoutError`` on an engine write firewall fault.
    """
    op_type = entry["type"]
    enum_type = _oc._merit_operand_enum(system)
    member = _resolve_enum(enum_type, op_type)

    op = mfe.AddOperand()
    if not bool(op.ChangeType(member)):
        raise SurfaceWriteError(
            f"engine rejected operand type {op_type} during recipe apply",
            field="operand_type",
            intended=op_type,
            actual=None,
            surface=None,
        )

    recipe_params = entry.get("params") or {}
    _mc.apply_params(op, recipe_params, operand_token=op_type)

    if _mc.is_valueless_control(op_type):
        # Value-less control operand (CONF): SKIP the Target/Weight write (the inf
        # sentinel). The proof is the semantic cell (Cfg#), authored + read-back-proven by
        # apply_params; Phase-1 already range-validated it. Return the live row.
        return int(op.OperandNumber)

    target = float(entry.get("target", 0.0))
    weight = float(entry.get("weight", 1.0))
    op.Target = target
    op.Weight = weight
    _lc._verify_or_raise(f"{op_type}.target", target, float(op.Target), surface=None)
    _lc._verify_or_raise(f"{op_type}.weight", weight, float(op.Weight), surface=None)
    return int(op.OperandNumber)


def _wire_refs(op, refs, index_to_live, *, operand_token):
    """Write each ``Op#`` ref cell on an already-authored operand (math scaffold §4.2).

    Translates each recipe index -> the operand's NEW live row and writes it
    via the EXISTING read-back-proven ``write_verified_cell``. Phase-1 validated the
    index ranges + the row-ref-Header membership rule, so a failure here is only an
    engine write rejection -> ``SurfaceWriteError`` (the atomic caller rolls back).

    ``refs`` carries ONLY resolved references (index in ``0..n-1``); the UNSET default
    never reaches here (it is a literal ``0`` in ``params``, authored in 2a). So this
    NEVER special-cases ``0`` — every ``refs`` value is a real index whose
    ``index_to_live[index]`` is a non-zero live row.
    """
    live = _mc.read_param_map(op)        # the freshly-authored row's live layout
    for header, index in refs.items():
        if header not in live:           # belt: Phase-1 already validated this header
            raise SurfaceWriteError(
                f"operand {operand_token} has no row-ref cell {header!r}",
                field="cell_layout",
                intended=header,
                actual=None,
                surface=None,
            )
        live_row = index_to_live[index]  # Phase-1 guaranteed index in 0..n-1 -> a key
        _mc.write_verified_cell(
            op, live[header]["col"], header, live_row, operand_token=operand_token
        )


def apply_merit_recipe(session, params):
    """Apply a portable recipe to the MFE: two-phase + atomic rollback (§5).

    **Phase 1 (ZERO engine mutation):** schema/version validation then validate
    EVERY operand (type vs live enum, target/weight coercion, each ``params`` name +
    value against a zero-net-mutation per-type signature cache). Any failure ->
    reject the WHOLE recipe (``merit_recipe_schema`` / ``merit_recipe_version`` /
    ``merit_recipe_invalid`` — the last carrying the per-entry error list). NO
    mutation occurred.

    **Phase 2 (only if Phase 1 clean):** ``mode="replace"`` (default) clears the MFE
    first; ``mode="append"`` authors onto the existing MFE. TWO-PHASE author-then-wire
    (math scaffold §4): sub-phase 2a authors EVERY operand (literal ``params`` +
    Target/Weight) recording each row's live number; sub-phase 2b WIRES every ``Op#``
    row reference, translating each recipe index -> the referenced operand's NEW live
    row (the remap) via the read-back-proven ``write_verified_cell``. Both
    sub-phases run inside the SAME checkpoint envelope, so a wire failure rolls back
    exactly like an author failure.

    **Atomicity (§5):** ``atomic=True`` (default) ``SaveMeritFunction(tmp)`` BEFORE
    Phase 2 mutates; on the FIRST Phase-2 write failure ``LoadMeritFunction(tmp)``
    rolls back, returns ``merit_recipe_apply`` ``rolled_back:true``. FAIL-CLOSED: a
    checkpoint SAVE throw -> never enter the apply (``checkpoint:false``, MFE
    unchanged); a rollback LOAD throw -> ``partial_state:true, rolled_back:false`` +
    an explicit "reload your .zmx" message. The temp ``.MF`` is reaped in ``finally``.
    ``atomic=False`` = best-effort (author the rest, ``ok:true`` + per-row report).
    The handler NEVER raises.
    """
    system = session.system
    mfe = system.MFE

    recipe = params.get("recipe")
    mode = params.get("mode", "replace")
    if mode not in ("replace", "append"):
        return _oc.error_envelope(
            "apply_merit_recipe",
            "merit_recipe_schema",
            f"mode must be 'replace' or 'append', got {mode!r}",
        )
    atomic = params.get("atomic", True)
    if not isinstance(atomic, bool):
        return _oc.error_envelope(
            "apply_merit_recipe",
            "merit_recipe_schema",
            f"atomic must be a bool, got {type(atomic).__name__} {atomic!r}",
        )

    # ---- Phase 1: dry validation (zero engine mutation). ----
    family, message, errors = _phase1_validate(system, mfe, recipe)
    if family is not None:
        detail = {}
        if errors is not None:
            detail["errors"] = errors
        return _oc.error_envelope(
            "apply_merit_recipe", family,
            message if message is not None else
            "recipe failed validation; see 'errors'",
            **detail,
        )

    operands = recipe["operands"]

    # ---- Atomic checkpoint (fail-closed): SaveMeritFunction(tmp) BEFORE mutating. ----
    checkpoint_path = None
    if atomic:
        try:
            fd, checkpoint_path = tempfile.mkstemp(suffix=".MF", prefix="optivibe_ckpt_")
            os.close(fd)
            mfe.SaveMeritFunction(checkpoint_path)
        except Exception as exc:  # noqa: BLE001 — a checkpoint save throw -> fail-closed
            # We never mutated, so the MFE is unchanged. Reap the temp file.
            _unlink_quiet(checkpoint_path)
            return _oc.error_envelope(
                "apply_merit_recipe",
                "merit_recipe_apply",
                f"could not checkpoint the MFE before applying "
                f"({exc!r}); MFE unchanged — nothing was applied",
                checkpoint=False,
                rolled_back=False,
                applied=0,
                mode=mode,
                atomic=atomic,
            )

    try:
        # ---- Phase 2: apply (mode replace clears first). ----
        if mode == "replace":
            clear = clear_merit(session, {})
            if not clear.get("ok", False):
                # A clear failure before any author: roll back / fail-closed.
                return _rollback_or_failclosed(
                    session, mfe, checkpoint_path, atomic, mode,
                    results=[], failed_index=None,
                    reason=f"clear_merit failed: {clear.get('error')}",
                )

        # ---- Sub-phase 2a: AUTHOR every operand (literal params + Target/Weight) ----
        # ``refs`` are NOT touched here (they are not in ``params``); we only record
        # each authored row's live number into ``index_to_live`` for the wire pass
        # (math scaffold §4.1, the two-phase author-then-wire contract).
        index_to_live = {}
        results = []
        for index, entry in enumerate(operands):
            op_type = entry["type"]
            try:
                operand_number = _author_recipe_operand(mfe, system, entry)
            except SurfaceWriteError as exc:
                results.append(
                    {"index": index, "type": op_type, "ok": False,
                     "error": str(exc)}
                )
                if atomic:
                    return _rollback_or_failclosed(
                        session, mfe, checkpoint_path, atomic, mode,
                        results=results, failed_index=index,
                        reason=str(exc),
                    )
                # atomic=False: best-effort, continue past the failure.
                continue
            except Exception as exc:  # noqa: BLE001
                # fix (states A/B): a NON-SurfaceWriteError engine THROW from
                # ``_author_recipe_operand`` (a raw .NET / pythonnet-proxy fault on
                # ``AddOperand`` / ``ChangeType`` / ``Target`` / ``Weight`` /
                # ``OperandNumber``) must STILL route through the SAME atomic rollback —
                # otherwise a partial merit (the rows authored before the throw) silently
                # PERSISTS and the outer dispatch envelopes it as ``internal`` (the
                # ownership gap). ``Exception`` (NOT ``BaseException``) so a
                # ``KeyboardInterrupt`` / ``SystemExit`` is NEVER swallowed. The reason
                # NAMES it an engine throw (distinct from a clean write-rejection).
                results.append(
                    {"index": index, "type": op_type, "ok": False,
                     "error": repr(exc)}
                )
                if atomic:
                    return _rollback_or_failclosed(
                        session, mfe, checkpoint_path, atomic, mode,
                        results=results, failed_index=index,
                        reason=f"engine threw while authoring operand[{index}] "
                               f"({op_type}): {exc!r}",
                    )
                # atomic=False: best-effort, continue past the failure.
                continue
            index_to_live[index] = operand_number
            results.append(
                {"index": index, "type": op_type, "ok": True,
                 "operand_number": operand_number}
            )

        # ---- Sub-phase 2b: WIRE every Op# ref (translate recipe index -> live row) ----
        # NEW (math scaffold §4.2): each ref's recipe index is resolved to the NEW live
        # row of the referenced operand (the remap) and written via the EXISTING
        # read-back-proven ``write_verified_cell``. Inside the SAME checkpoint envelope
        # as 2a, so a wire failure rolls back exactly like an author failure (§4.3).
        # ``(SurfaceWriteError, KeyError)`` are caught: a KeyError is an internal remap
        # desync (Phase-1 range-validated every index + 2a authored every entry, so it
        # cannot happen — but if it did it routes to rollback with a clear reason,
        # NEVER escaping as an opaque dispatch ``internal``, state C).
        for index, entry in enumerate(operands):
            refs = entry.get("refs") or {}
            if not refs:
                continue
            if not results[index]["ok"]:
                # The author pass (atomic=False best-effort) already failed this row;
                # do not attempt to wire a row that was not authored.
                continue
            try:
                op = mfe.GetOperandAt(index_to_live[index])
                _wire_refs(op, refs, index_to_live, operand_token=entry["type"])
            except (SurfaceWriteError, KeyError) as exc:
                results[index]["ok"] = False
                results[index]["error"] = str(exc)
                if atomic:
                    return _rollback_or_failclosed(
                        session, mfe, checkpoint_path, atomic, mode,
                        results=results, failed_index=index,
                        reason=(f"internal remap desync (missing live row for index "
                                f"{exc})" if isinstance(exc, KeyError) else str(exc)),
                    )
                # atomic=False: best-effort, continue past the failure.
                continue
            except Exception as exc:  # noqa: BLE001
                # fix (states A/B): a NON-(SurfaceWriteError/KeyError) engine
                # THROW from ``mfe.GetOperandAt`` / ``_mc.read_param_map`` inside
                # ``_wire_refs`` (a raw .NET / pythonnet-proxy fault) must STILL route
                # through the SAME atomic rollback — otherwise a fully-authored but
                # partially-wired merit (this state) silently PERSISTS and dispatch
                # envelopes it as ``internal``. ``Exception`` (NOT ``BaseException``) so a
                # ``KeyboardInterrupt`` / ``SystemExit`` is NEVER swallowed. The reason
                # NAMES it an engine throw (distinct from a clean write-rejection).
                results[index]["ok"] = False
                results[index]["error"] = repr(exc)
                if atomic:
                    return _rollback_or_failclosed(
                        session, mfe, checkpoint_path, atomic, mode,
                        results=results, failed_index=index,
                        reason=f"engine threw while wiring refs for operand[{index}] "
                               f"({entry['type']}): {exc!r}",
                    )
                # atomic=False: best-effort, continue past the failure.
                continue

        applied = sum(1 for r in results if r["ok"])
        failed = sum(1 for r in results if not r["ok"])
        # fix: the success-tail readback (``NumberOfOperands`` +
        # ``CalculateMeritFunction``) is a raw .NET read AFTER the MFE was already
        # mutated in Phase 2. A degraded-engine THROW here must NOT escape as an opaque
        # dispatch ``internal`` (and we must NOT roll back — the apply already
        # SUCCEEDED). Report the post-mutation state with best-effort telemetry instead.
        try:
            number_of_operands = int(mfe.NumberOfOperands)
        except Exception:  # noqa: BLE001 — best-effort post-apply telemetry
            number_of_operands = None
        try:
            merit = safe_float(mfe.CalculateMeritFunction())
        except Exception:  # noqa: BLE001 — best-effort post-apply telemetry
            merit = None
        if number_of_operands is None or merit is None:
            # The apply itself SUCCEEDED (every row authored); only the post-mutation
            # readback threw. Report the partial telemetry as a structured envelope —
            # do NOT raise, do NOT roll back a successful apply.
            return _oc.error_envelope(
                "apply_merit_recipe",
                "merit_recipe_apply",
                "the recipe was applied but reading back the post-apply MFE state "
                "(NumberOfOperands / CalculateMeritFunction) threw; the operands were "
                "authored — the readback telemetry is unavailable",
                applied=applied,
                failed=failed,
                results=results,
                rolled_back=False,
                checkpoint=bool(checkpoint_path),
                number_of_operands=number_of_operands,
                merit=merit,
                mode=mode,
                atomic=atomic,
            )
        return {
            "ok": True,
            "mode": mode,
            "atomic": atomic,
            "applied": applied,
            "failed": failed,
            "results": results,
            "number_of_operands": number_of_operands,
            "merit": merit,
        }
    finally:
        _unlink_quiet(checkpoint_path)


def _rollback_or_failclosed(session, mfe, checkpoint_path, atomic, mode, *,
                            results, failed_index, reason):
    """Roll back via ``LoadMeritFunction(tmp)``; fail-closed if the LOAD throws (§5).

    Builds the ``merit_recipe_apply`` envelope for an atomic Phase-2 failure:

    - the rollback ``LoadMeritFunction(checkpoint)`` restores the pre-apply state ->
      ``rolled_back:true, checkpoint:true, applied:0``;
    - a rollback LOAD THROW (checkpoint existed, restore failed) -> FAIL-CLOSED:
      ``partial_state:true, rolled_back:false, checkpoint:true`` + an EXPLICIT "reload
      your design .zmx to recover" message. The handler NEVER raises.

    When ``checkpoint_path`` is falsy (``None`` / empty — the ``atomic=False`` case,
    which takes NO checkpoint), there is NOTHING to restore: NEVER feed a ``None`` /
    empty path to ``LoadMeritFunction`` (it THROWS on the live engine). Return a
    best-effort failure envelope with ``checkpoint:false, rolled_back:false`` and a
    clear message — the MFE may be partially mutated (the documented ``atomic=False``
    best-effort posture). This is the ONLY path that reaches this helper with no
    checkpoint (the ``atomic=False`` clear-failure branch of the replace mode).

    The temp ``.MF`` is reaped by the caller's ``finally`` (this helper only
    LoadMeritFunctions it).

    fix: the recovery-path telemetry reads (``NumberOfOperands``) are BEST-EFFORT
    (``_safe_count`` -> ``None`` on a THROW, never raises). The fail-closed report must
    not corrupt itself — if the MFE proxy is degraded enough that ``NumberOfOperands``
    now throws, the report still honors ``partial_state:true, rolled_back:false`` rather
    than escaping as an opaque dispatch ``internal``.
    """
    if not checkpoint_path:
        # No checkpoint was ever taken (atomic=False): there is nothing to roll back
        # to and we must NOT hand a None/empty path to LoadMeritFunction (it THROWS
        # live). Report the best-effort failure honestly.
        return _oc.error_envelope(
            "apply_merit_recipe",
            "merit_recipe_apply",
            f"recipe apply failed ({reason}); atomic=False took no checkpoint so the "
            "MFE was NOT rolled back and may be partially mutated (best-effort "
            "posture). Reload your design .zmx to recover.",
            checkpoint=False,
            rolled_back=False,
            partial_state=True,
            applied=0,
            failed_index=failed_index,
            results=results,
            mode=mode,
            atomic=atomic,
            number_of_operands=_safe_count(mfe),
        )
    try:
        mfe.LoadMeritFunction(checkpoint_path)
    except Exception as exc:  # noqa: BLE001 — a rollback load throw -> partial state
        return _oc.error_envelope(
            "apply_merit_recipe",
            "merit_recipe_apply",
            f"ROLLBACK FAILED while applying the recipe ({reason}); the restore "
            f"itself threw ({exc!r}) — the MFE is in a PARTIAL state. Reload your "
            "design .zmx to recover.",
            checkpoint=True,
            rolled_back=False,
            partial_state=True,
            applied=0,
            failed_index=failed_index,
            results=results,
            mode=mode,
            atomic=atomic,
            number_of_operands=_safe_count(mfe),
        )
    return _oc.error_envelope(
        "apply_merit_recipe",
        "merit_recipe_apply",
        f"recipe apply failed at operand[{failed_index}] ({reason}); the MFE was "
        "rolled back to its pre-apply state via the temp checkpoint.",
        checkpoint=True,
        rolled_back=True,
        applied=0,
        failed_index=failed_index,
        results=results,
        mode=mode,
        atomic=atomic,
        number_of_operands=_safe_count(mfe),
    )


def _is_finite_number(value):
    """True iff ``value`` is a finite real number (structural-marker gate).

    The raw ``op.Target`` / ``op.Weight`` may be a .NET ``Double`` proxy; coerce via
    ``float`` and test ``math.isfinite``. A non-numeric / un-coercible value is treated
    as NOT a finite number (so a structural marker with such a value is skipped).
    """
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _safe_count(mfe):
    """Best-effort ``NumberOfOperands`` read for a fail-closed report. NEVER raises.

    The recovery-path telemetry must not corrupt its OWN report: if the MFE proxy is
    degraded enough that ``NumberOfOperands`` now throws, return ``None`` rather than
    letting the read escape as an opaque dispatch ``internal``.
    """
    try:
        return int(mfe.NumberOfOperands)
    except Exception:  # noqa: BLE001 — best-effort fail-closed telemetry, never raise
        return None


def _unlink_quiet(path):
    """Best-effort unlink of the temp checkpoint ``.MF`` (the §5 ``finally`` reap).

    NEVER raises: a ``None`` path, a missing file, or any ``OSError`` is swallowed —
    a temp-file reap failure must not mask the apply outcome.
    """
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


SAVE_MERIT_SPEC = ToolSpec(
    name="save_merit",
    handler=save_merit,
    required_params=("path",),
    param_types={"path": "string"},
    description=(
        "Save the merit function to a native .MF file (durability-gated by the "
        "magic-byte oracle). A relative path resolves under the project workspace."
    ),
)

LOAD_MERIT_SPEC = ToolSpec(
    name="load_merit",
    handler=load_merit,
    required_params=("path",),
    param_types={"path": "string"},
    description=(
        "Load a native .MF merit file into the merit editor (magic-checked first); "
        "read back the operand count."
    ),
)

CLEAR_MERIT_SPEC = ToolSpec(
    name="clear_merit",
    handler=clear_merit,
    required_params=(),
    description=(
        "Clear all merit operands; the editor floors at one placeholder row. Returns "
        "the deleted count + before/after operand counts."
    ),
)

REMOVE_OPERAND_SPEC = ToolSpec(
    name="remove_operand",
    handler=remove_operand,
    required_params=("operand_number",),
    param_types={"operand_number": "number"},
    description=(
        "Remove one merit operand row by its 1-based number (client-side index "
        "bound, count-drop verified)."
    ),
)

SERIALIZE_MERIT_SPEC = ToolSpec(
    name="serialize_merit",
    handler=serialize_merit,
    required_params=(),
    description=(
        "Read the merit function into a portable, Header-keyed recipe dict "
        "(versioned; BLNK rows skipped). Pure read; never mutates the MFE."
    ),
)

APPLY_MERIT_RECIPE_SPEC = ToolSpec(
    name="apply_merit_recipe",
    handler=apply_merit_recipe,
    required_params=("recipe",),
    param_types={
        "recipe": "object",
        "mode": "string",
        "atomic": "boolean",
    },
    description=(
        "Apply a portable merit recipe (validated before any mutation). "
        "mode='replace' (default) clears the merit editor first; mode='append' adds "
        "onto it. Gotcha: fail-closed — on any error it rolls back and reports "
        "checkpoint:false / partial_state:true rather than leaving a half-applied "
        "merit. See add_operand, add_math_constraint, build_merit."
    ),
)

# The four NATIVE/IO tools + the two RECIPE tools in
# the SAME aggregation — the server registers ``optimize_merit_io.TOOL_SPECS`` once.
TOOL_SPECS = (
    SAVE_MERIT_SPEC,
    LOAD_MERIT_SPEC,
    CLEAR_MERIT_SPEC,
    REMOVE_OPERAND_SPEC,
    SERIALIZE_MERIT_SPEC,
    APPLY_MERIT_RECIPE_SPEC,
)
