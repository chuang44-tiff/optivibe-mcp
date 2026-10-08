"""tools/merit_math.py — the intent->math composer ``add_math_constraint`` (math §4).

The ONE dispatchable tool of the merit-builder Phase-C math cycle (Shape 2 — the
single composer, user sign-off 2026-06-18). It authors a derived/relational/bounded
merit constraint from **already-resolved operand codes** + an agent-**stated
relationship** + the agent-**read** ``sign_convention`` + **stable labels** +
target/weight, via the EXISTING ``apply_merit_recipe`` scaffold.

THE decisive architectural fact (CORRECTED by S-REF-3): a
harness handler receives ONLY the ZOS ``session`` as arg-0, so the reference tools' own
SQLite conn — threaded by a SEPARATE dispatcher, reconciled at the OUTER route-by-name
boundary — never reaches it. That is a CONTRACT, not a physical limit. Until S-REF-3
this docstring said the handler "physically CANNOT call ``lookup_operand`` in-process";
that was FALSE and unexamined for three months. The editable install puts
``optivibe_reference`` in the SAME interpreter (measured), and the handler now reads the
catalog directly through ``_grounding`` to CHECK the agent's relayed ``sign_convention``.

What is unchanged is the no-silent-wrong mechanic: disambiguation still happens in the
AGENT, between tool calls, and this composer still takes a RESOLVED CODE, never a
phrase/query. There is still NO phrase->code path in this handler (it is
structural). Grounding is ADDITIVE — where no catalog is built it degrades to exactly
the pre-S-REF-3 behaviour, so a cold clone loses nothing it has today.

The handler flow (math-tool §4):

1. validate the top-level shape (``operands`` a non-empty list; ``mode``/``atomic``/
   ``dry_run``/``confirm_crosscheck`` types) -> ``merit_recipe_schema`` on a bad shape
   (REUSED — the recipe families own everything mechanical, §7);
2. per operand: validate ``code`` vs the LIVE ``MeritOperandType`` enum (an unknown
   code -> ``merit_math_unresolved``); validate ``relationship`` is one of the five §4
   strings + ``sign_convention`` present (else ``merit_math_unresolved``);
3. run ``_cross_check`` per operand (§3): a ``refused`` -> ``merit_math_crosscheck``; a
   ``warn`` without ``confirm_crosscheck`` -> ``merit_math_crosscheck``;
4. resolve every ``refs`` label -> a 0-based recipe-index map via ``_resolve_handles``
   (§5): a dangling / before-defined label -> ``merit_math_unresolved`` (NO engine
   touch);
5. assemble the ``optivibe.merit-recipe`` dict via ``_constraint_to_recipe`` (§4);
6. ``dry_run=true`` -> return the PREVIEW (recipe + per-operand cross_check verdicts +
   refs_resolved), NO mutation (the echo-back surface, §4); else DELEGATE to
   ``apply_merit_recipe(session, {recipe, mode, atomic})`` and surface its result + a
   cross_check summary.

The handler NEVER raises (the L26 firewall): every step that could fault is guarded; an
engine fault below the cross-check inherits ``apply_merit_recipe``'s structured
families (``merit_recipe_*`` / ``surface_write``), never an opaque dispatch
``internal``.

THREE new error families (the third added by S-REF-3): ``merit_math_unresolved``
(unknown code / a refs label that resolves to no prior operand / null-or-missing
sign_convention / a relationship not in the enum), ``merit_math_crosscheck`` (a REFUSED
cross-check step; OR a ``warn`` step without ``confirm_crosscheck``), and
``merit_math_ungrounded`` (the relayed ``sign_convention`` contradicts what the
reference catalog states for that code — carries BOTH values in structured fields).
The families stay distinguishable on purpose: a MISSING convention is an unresolved
intent, a CONTRADICTED one is a grounding failure, and ``merit_math_unresolved`` still
fires first for the input that satisfies both. Everything downstream REUSES the Phase-A
recipe families verbatim.

Live ZOS-API integration; unit-tested against fixture-seeded fakes
(``FakeRecipeMFE``) whose DIFF.Value is COMPUTED from its live ``Op#`` pointers
(so a remap bug yields the WRONG value).
"""
from ..enums import _resolve_enum
from ..errors import ToolParamError
from ..server import ToolSpec
from . import _config_common as _ccfg
from . import _grounding
from . import _merit_cells as _mc
from . import _merit_math as _mm
from . import _optimize_common as _oc
from .optimize_merit_io import _phase1_validate, apply_merit_recipe


def _engine_version(session):
    """The live OpticStudio version as ``"<major>.<minor>.<sp>"``, or None.

    NEVER raises. The string shape is the one the probes capture
    (the live boot and session probes) and is byte-identical
    to the reference catalog's ``optic_studio_version`` (measured: both ``"25.1.0"``),
    which is what makes the S-REF-3 freshness comparison meaningful rather than a
    format guess.

    ``session.app`` raises ``SessionClosedError`` on a closed session, and dereferencing
    the .NET proxy can raise a raw remoting exception on a poisoned channel. Neither may
    reach the caller: a version we cannot read means "do not ground this call", never a
    failed dispatch.
    """
    try:
        app = session.app
        return ".".join(str(v) for v in (
            app.ZOSMajorVersion, app.ZOSMinorVersion, app.ZOSSPVersion))
    except Exception:  # noqa: BLE001 — see docstring; a missing version is not an error
        return None


def add_math_constraint(session, params):
    """Compose + author (or preview) a math/relational merit constraint (math §4).

    See the module docstring for the full flow. ``params`` per math-tool §4:

    - ``operands``: REQUIRED, 1..N operand-specs IN AUTHOR ORDER; each carries a
      resolved ``code`` + STATED ``relationship`` + READ ``sign_convention`` (+ optional
      ``label`` / ``params`` / ``refs`` / ``target`` / ``weight``);
    - ``mode``: ``"append"`` (default) / ``"replace"`` — passthrough to
      ``apply_merit_recipe`` (math intents usually ADD);
    - ``atomic``: bool (default True) — passthrough;
    - ``dry_run``: bool (default False) — preview WITHOUT mutation;
    - ``confirm_crosscheck``: bool (default False) — a ``warn`` cross-check step is
      REFUSED unless True.

    NEVER raises. Returns the envelope (see the per-branch returns below).
    """
    # ---- never-raise guards (defense-in-depth, the L26 firewall as a STANDALONE
    #      invariant — unit tests call this handler directly, no dispatch wrapper). A
    #      non-dict ``params`` or a degraded/None ``session`` -> a
    #      structured envelope, never an AttributeError escape. ----
    if not isinstance(params, dict):
        params = {}
    system = getattr(session, "system", None)
    if system is None:
        return _oc.error_envelope(
            "add_math_constraint", "merit_math_unresolved",
            "no live ZOS session/system is available to validate operand codes "
            "(a degraded engine); cannot author or preview a math constraint",
        )

    # ---- top-level shape (REUSE the recipe schema family — §7) ----
    operands = params.get("operands")
    if not isinstance(operands, list) or not operands:
        return _oc.error_envelope(
            "add_math_constraint",
            "merit_recipe_schema",
            f"'operands' must be a non-empty list of operand-specs, got "
            f"{type(operands).__name__ if not isinstance(operands, list) else 'an empty list'}",
        )

    mode, mode_err = _flag_str(params, "mode", "append", ("append", "replace"))
    if mode_err is not None:
        return _oc.error_envelope("add_math_constraint", "merit_recipe_schema", mode_err)
    atomic, atomic_err = _flag_bool(params, "atomic", True)
    if atomic_err is not None:
        return _oc.error_envelope("add_math_constraint", "merit_recipe_schema", atomic_err)
    dry_run, dry_err = _flag_bool(params, "dry_run", False)
    if dry_err is not None:
        return _oc.error_envelope("add_math_constraint", "merit_recipe_schema", dry_err)
    confirm, conf_err = _flag_bool(params, "confirm_crosscheck", False)
    if conf_err is not None:
        return _oc.error_envelope("add_math_constraint", "merit_recipe_schema", conf_err)

    # ---- per-operand: code vs the live enum, relationship + sign_convention,
    #      then the §3 cross-check. ALL three pre-mutation gates run BEFORE any
    #      engine touch (a dry_run or a refusal NEVER opens/authors). ----
    try:
        enum_type = _oc._merit_operand_enum(system)
    except ToolParamError as exc:
        # The live enum could not be resolved (a degraded engine / proxy gap). A
        # param-class problem -> a structured envelope, never an internal (§0/L26).
        return _oc.error_envelope(
            "add_math_constraint", "merit_math_unresolved",
            f"could not resolve the live MeritOperandType enum to validate codes: {exc}",
        )

    # S-REF-3: the LIVE engine version, read ONCE per call. It gates whether the
    # reference catalog is allowed to contradict the agent (see ``_grounding``) —
    # a catalog built against a different engine than the one running may not refuse
    # anything. None (unreadable, or offline) simply means no grounding this call.
    engine_version = _engine_version(session)

    cross_check = []          # the per-operand verdict list (preview + summary)
    for index, spec in enumerate(operands):
        if not isinstance(spec, dict):
            return _oc.error_envelope(
                "add_math_constraint", "merit_recipe_schema",
                f"operand[{index}] must be a dict, got {type(spec).__name__}",
            )

        code = spec.get("code")
        if not isinstance(code, str) or code == "":
            return _oc.error_envelope(
                "add_math_constraint", "merit_math_unresolved",
                f"operand[{index}] 'code' must be a non-empty resolved "
                f"MeritOperandType mnemonic, got {code!r}",
                index=index,
            )
        # Validate the code vs the LIVE enum (an unknown code -> merit_math_unresolved,
        # never passed downstream — §4). This is also the structural no-phrase guard:
        # a phrase is not a live enum member, so a query can never resolve here.
        try:
            _resolve_enum(enum_type, code)
        except ToolParamError as exc:
            return _oc.error_envelope(
                "add_math_constraint", "merit_math_unresolved",
                f"operand[{index}] code {code!r} is not a known MeritOperandType: "
                f"{exc}",
                index=index, code=code,
            )

        relationship = spec.get("relationship")
        if relationship not in _mm._RELATIONSHIPS:
            return _oc.error_envelope(
                "add_math_constraint", "merit_math_unresolved",
                f"operand[{index}] 'relationship' must be one of "
                f"{list(_mm._RELATIONSHIPS)}, got {relationship!r}",
                index=index, code=code,
            )

        # The sign_convention MUST be present (the agent READ it from lookup_operand —
        # the tool cannot re-fetch it, §0). A null/missing convention -> unresolved (the
        # §3 grid would REFUSE it anyway, but a MISSING convention is an unresolved
        # intent, not a cross-family mismatch — distinguish the families, §7).
        if "sign_convention" not in spec or _is_null_convention(spec.get("sign_convention")):
            return _oc.error_envelope(
                "add_math_constraint", "merit_math_unresolved",
                f"operand[{index}] ({code}) has no usable 'sign_convention'; the agent "
                "must supply the value it read from lookup_operand (a null/unknown "
                "convention is refused — qualify the operand)",
                index=index, code=code,
            )

        sign_convention = spec.get("sign_convention")

        # S-REF-3: the handler's OWN read of the reference, instead of believing the
        # relayed value. Fires ONLY when the catalog states a convention AND is
        # trustworthy; every other case is ``unavailable`` and leaves the pre-S-REF-3
        # behaviour untouched (owner ruling: tracked floor + enrich). Absence is NEVER
        # a refusal — the live enum above already owns whether a code exists.
        # The accessor owns a never-raise belt of its own, so this second net is
        # defense-in-depth rather than the primary guarantee. It is here because this
        # module's docstring promises the handler NEVER raises and names EVERY faulting
        # step as guarded — leaving the one new call bare would make that promise false
        # about this cycle's own edit, which is the defect class S-REF-3 exists to fix.
        # A breached belt degrades to "no grounding", never to a broken dispatch.
        try:
            g_status, reference_convention = _grounding.sign_convention_for(
                code, engine_version)
        except Exception:  # noqa: BLE001 — see above; a grounding fault is never fatal
            g_status, reference_convention = _grounding.GROUNDING_UNAVAILABLE, None
        if (g_status == _grounding.GROUNDING_OK
                and reference_convention != sign_convention):
            return _oc.error_envelope(
                "add_math_constraint", "merit_math_ungrounded",
                f"operand[{index}] ({code}) sign_convention mismatch: you supplied "
                f"{sign_convention!r}, the reference catalog states "
                f"{reference_convention!r}. Re-read the operand with lookup_operand and "
                f"pass the value it returns.",
                index=index, code=code,
                relayed_sign_convention=sign_convention,
                reference_sign_convention=reference_convention,
            )

        status, reason = _mm._cross_check(relationship, sign_convention)
        verdict = {
            "index": index,
            "label": spec.get("label"),
            "code": code,
            "relationship": relationship,
            "sign_convention": sign_convention,
            "status": status,
            "reason": reason,
        }
        cross_check.append(verdict)

        if status == "refused":
            return _oc.error_envelope(
                "add_math_constraint", "merit_math_crosscheck",
                f"operand[{index}] ({code}) cross-check REFUSED: {reason}",
                index=index, code=code, relationship=relationship,
                sign_convention=sign_convention, cross_check=cross_check,
            )
        if status == "warn" and not confirm:
            return _oc.error_envelope(
                "add_math_constraint", "merit_math_crosscheck",
                f"operand[{index}] ({code}) cross-check is a WARN: {reason}. Pass "
                "confirm_crosscheck=true to author it anyway.",
                index=index, code=code, relationship=relationship,
                sign_convention=sign_convention, cross_check=cross_check,
            )

    # ---- NEW: validate config=k, then build the RESOLUTION list with a synthetic
    #      CONF at index 0 so every user operand resolves at +1 (the structural index-shift
    #      fix, §2.2 — NO manual offset). The synthetic CONF carries NEITHER a label NOR
    #      refs, so it contributes nothing to label_to_index and gets no refs entry; it just
    #      OCCUPIES index 0, so _resolve_handles binds every user label to user_index + 1
    #      and _constraint_to_recipe enumerates the SAME shifted list (recipe index ==
    #      resolution index). ``config`` absent -> resolution_operands IS operands (byte-
    #      identical). ----
    config = params.get("config")
    resolution_operands = operands          # default: byte-identical (no CONF prepend)
    cfg = None
    if config is not None:
        cfg, cfg_err = _validate_math_config(system, config)
        if cfg_err is not None:
            return cfg_err
        # §2.6: a user operand that is itself a CONF + config=k is an ambiguous double-CONF
        # (zero mutation, pre-assembly) — the agent must pick ONE path.
        if any(
            isinstance(s, dict) and _mc.is_valueless_control(s.get("code")) for s in operands
        ):
            return _oc.error_envelope(
                "add_math_constraint", "merit_math_unresolved",
                "config=k authors the per-config CONF bracket for you; a user operand that "
                "is itself a CONF is ambiguous — pass config=k OR a hand CONF operand, not "
                "both",
            )
        resolution_operands = [{"code": "CONF", "params": {"Cfg#": cfg}}, *operands]

    # ---- §5: resolve every refs label -> a 0-based recipe-index map (NO engine
    #      touch — pure label bookkeeping; a dangling/before-defined label aborts
    #      BEFORE any mutation, §8.4). Resolved OVER resolution_operands (CONF at 0 ->
    #      user ops at +1). ----
    refs_by_index, ref_errors = _mm._resolve_handles(resolution_operands)
    if ref_errors:
        return _oc.error_envelope(
            "add_math_constraint", "merit_math_unresolved",
            "one or more operand row-reference labels did not resolve: "
            + "; ".join(f"operand[{e['index']}]: {e['error']}" for e in ref_errors),
            errors=ref_errors,
        )

    # ---- §4: assemble the optivibe.merit-recipe dict apply_merit_recipe consumes (OVER
    #      the SAME shifted resolution_operands so the CONF is recipe operand 0). ----
    recipe = _mm._constraint_to_recipe(resolution_operands, refs_by_index)

    # The refs echo iterates the SAME resolution list (§2.5) so the echoed recipe_index
    # matches the actual recipe refs value.
    refs_resolved = _refs_resolved_summary(resolution_operands, refs_by_index)

    # ---- §4 dry_run: the echo-back PREVIEW (recipe + verdicts + refs), NO NET mutation.
    #      The preview MUST be a FAITHFUL echo of apply-ability: run the SAME Phase-1
    #      validation apply runs (the zero-net-mutation scratch read — the only engine
    #      touch dry_run is allowed, §1/Risk 4), so a dry_run that would FAIL apply returns
    #      the SAME structured error apply would, never a false-ok preview. ----
    if dry_run:
        dry_err = _dry_run_validate(system, recipe)
        if dry_err is not None:
            dry_err.setdefault("cross_check", cross_check)
            dry_err.setdefault("refs_resolved", refs_resolved)
            if cfg is not None:
                dry_err.setdefault("config", cfg)
            return dry_err
        preview = {
            "ok": True,
            "dry_run": True,
            "recipe": recipe,
            "cross_check": cross_check,
            "refs_resolved": refs_resolved,
            "mode": mode,
            "atomic": atomic,
        }
        if cfg is not None:
            preview["config"] = cfg          # §2.3 additive echo
        return preview

    # ---- DELEGATE to apply_merit_recipe (the author engine — §4). It NEVER raises;
    #      its families (merit_recipe_*/surface_write) are REUSED verbatim (§7). We
    #      surface its result + the cross_check summary. ----
    applied = apply_merit_recipe(
        session, {"recipe": recipe, "mode": mode, "atomic": atomic}
    )
    if isinstance(applied, dict):
        # Attach the math-layer telemetry to BOTH success and the apply's own failure
        # envelopes (so a caller always sees the cross-check verdicts + the refs map),
        # without overwriting apply_merit_recipe's own ok/error_family.
        applied.setdefault("cross_check", cross_check)
        applied.setdefault("refs_resolved", refs_resolved)
        if cfg is not None:
            applied.setdefault("config", cfg)        # §2.3 additive echo
    return applied


def _dry_run_validate(system, recipe):
    """Run apply's Phase-1 validation in the dry_run path -> a reject envelope or None.

    A FAITHFUL dry_run must NOT preview ``ok:True`` for a plan the real apply would
    reject (the audit's echo-back-integrity gap). This reuses the EXISTING zero-net-
    mutation ``_phase1_validate`` (schema/version + per-operand type/params/refs against
    a P5 scratch-read signature cache — the scratch operand is ``RemoveOperandAt``-reaped,
    so NO net mutation) and rebuilds the SAME ``merit_recipe_*`` envelope
    ``apply_merit_recipe`` returns on a Phase-1 reject (``optimize_merit_io.py`` L938).

    Returns the reject envelope dict when Phase-1 fails, or ``None`` when the recipe is
    apply-clean. NEVER raises — a probe-time engine fault is mapped to a structured
    ``merit_recipe_apply`` envelope (the dry_run could not be validated), mirroring the
    apply path's never-raise firewall.
    """
    try:
        mfe = system.MFE
        family, message, errors = _phase1_validate(system, mfe, recipe)
    except Exception as exc:  # noqa: BLE001 — never-raise (L26): a probe fault -> envelope.
        return _oc.error_envelope(
            "add_math_constraint", "merit_recipe_apply",
            f"could not validate the recipe for dry_run preview ({exc!r}); the engine "
            "rejected the zero-net-mutation signature probe",
        )
    if family is None:
        return None
    detail = {}
    if errors is not None:
        detail["errors"] = errors
    return _oc.error_envelope(
        "add_math_constraint", family,
        message if message is not None else "recipe failed validation; see 'errors'",
        **detail,
    )


# --------------------------------------------------------------------------- #
# Pure local helpers (validation + telemetry; no engine touch).
# --------------------------------------------------------------------------- #
def _flag_bool(params, key, default):
    """Pull an optional bool flag; reject a non-bool -> ``(default, message)``.

    Returns ``(value, None)`` on success or ``(default, message)`` on a non-bool (a
    client miswrite — the recipe schema family owns it, §7). NEVER raises.
    """
    if key not in params:
        return default, None
    value = params[key]
    if not isinstance(value, bool):
        return default, (f"{key!r} must be a bool, got {type(value).__name__} {value!r}")
    return value, None


def _flag_str(params, key, default, allowed):
    """Pull an optional str flag constrained to ``allowed`` -> ``(value, message)``.

    Returns ``(value, None)`` on success or ``(default, message)`` on a value outside
    ``allowed`` (the recipe schema family owns it, §7). NEVER raises.
    """
    if key not in params:
        return default, None
    value = params[key]
    if value not in allowed:
        return default, (f"{key!r} must be one of {list(allowed)}, got {value!r}")
    return value, None


def _validate_math_config(system, value):
    """Validate ``config=k`` 1-based -> ``(cfg, None)`` or ``(None, error_envelope)`` (§2.4).

    REUSES ``_mc.row_ref_int`` (the SAME rule the ``Cfg#`` writer uses, L30 no-divergence):
    an exact ``int`` or an integral ``float`` (``2.0``) passes; a ``bool``, a non-integral
    float, a string, ``nan``/``inf`` all reject. A BAD SHAPE -> ``merit_recipe_schema``
    (the tool's shape family); OUT OF RANGE (``<1`` or ``>n_configs``) ->
    ``merit_math_unresolved`` (the tool's range/resolution family). Both are EXISTING
    families — NO new family. The range uses the THROW-guarded
    ``safe_number_of_configurations`` (-> 1 on a fresh / non-MCE / wedged system).
    """
    cfg = _mc.row_ref_int(value)
    if cfg is None:
        return None, _oc.error_envelope(
            "add_math_constraint", "merit_recipe_schema",
            f"config must be an integer config number, got {value!r}",
        )
    n_configs = _ccfg.safe_number_of_configurations(system)
    if not (1 <= cfg <= n_configs):
        return None, _oc.error_envelope(
            "add_math_constraint", "merit_math_unresolved",
            f"config {cfg} is out of range; the system has {n_configs} "
            f"configuration(s) (valid 1..{n_configs})",
        )
    return cfg, None


def _is_null_convention(sign_convention):
    """True iff ``sign_convention`` is the ``null``/unknown convention (§3 null column).

    A Python ``None`` / JSON ``null`` / the string ``"null"`` / an empty string all
    mark an unknown convention (REFUSED). Mirrors ``_merit_math._normalize_sign_
    convention`` returning ``None`` — kept local so the handler's "missing vs present"
    distinction (unresolved vs cross-check) is explicit.
    """
    return _mm._normalize_sign_convention(sign_convention) is None


def _refs_resolved_summary(operands, refs_by_index):
    """Build the ``refs_resolved`` echo-back list (§4 dry_run preview shape).

    One entry per RESOLVED reference: ``{label, recipe_index}``. The ``recipe_index`` is
    the 0-based recipe position the label resolved to (the value written into the ``Op#``
    cell at apply time, remapped to a live row by the Phase-A two-phase pass). The ONLY
    supported ref form is an in-call label (the append-mode ``{existing_row: n}`` raw-row
    form was CUT this cycle, §5.2 — a non-label entry never resolves, so it never reaches
    here). Pure; derived from the already-resolved ``refs_by_index`` + the operand specs.
    """
    summary = []
    for index, spec in enumerate(operands):
        spec_refs = spec.get("refs")
        if not isinstance(spec_refs, list) or not spec_refs:
            continue
        resolved = refs_by_index.get(index, {})
        headers = _mm._op_ref_headers(len(spec_refs))
        for header, entry in zip(headers, spec_refs):
            if header not in resolved:
                continue
            if isinstance(entry, str):
                summary.append({
                    "referencing_index": index,
                    "header": header,
                    "label": entry,
                    "recipe_index": resolved[header],
                })
    return summary


ADD_MATH_CONSTRAINT_SPEC = ToolSpec(
    name="add_math_constraint",
    handler=add_math_constraint,
    required_params=("operands",),
    param_types={
        "operands": "array",
        "mode": "string",
        "atomic": "boolean",
        "dry_run": "boolean",
        "confirm_crosscheck": "boolean",
        "config": "number",
    },
    description=(
        "Author a derived/relational/bounded merit constraint from already-resolved "
        "operand CODES + a stated relationship (ge/le/eq/derived/minimize) + the "
        "agent-read sign_convention + stable labels; runs the relationship x "
        "sign_convention cross-check, owns the label->row bookkeeping, and authors via "
        "the merit recipe. dry_run=true previews the recipe + cross-check verdicts + "
        "resolved refs without mutating. Pass config=k to wrap the whole constraint "
        "block in a per-config CONF k bracket. Takes a CODE, never a phrase. Ground the code "
        "with lookup_operand first. See add_operand, apply_merit_recipe, build_merit."
    ),
)

TOOL_SPECS = (ADD_MATH_CONSTRAINT_SPEC,)
