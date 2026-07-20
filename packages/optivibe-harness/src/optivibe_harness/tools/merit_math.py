"""tools/merit_math.py — the intent->math composer ``add_math_constraint`` (math §4).

The ONE dispatchable tool of the merit-builder Phase-C math cycle (Shape 2 — the
single composer, user sign-off 2026-06-18). It authors a derived/relational/bounded
merit constraint from **already-resolved operand codes** + an agent-**stated
relationship** + the agent-**read** ``sign_convention`` + **stable labels** +
target/weight, via the EXISTING ``apply_merit_recipe`` scaffold.

THE decisive architectural fact (math-tool §0): a harness handler receives ONLY the
ZOS ``session`` as arg-0 — it physically CANNOT call ``lookup_operand`` in-process (the
reference tools take a SQLite conn threaded by a SEPARATE dispatcher; the composite
reconciles the two arg-0 contracts at the OUTER route-by-name boundary). So the
grounding + disambiguation happen in the AGENT, BETWEEN tool calls; this composer takes
a RESOLVED CODE, never a phrase/query. There is literally NO phrase->code path in
this handler — that is the no-silent-wrong enforcement mechanic (§6.1, structural).

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

Only TWO NEW error families (§7): ``merit_math_unresolved`` (unknown code / a refs
label that resolves to no prior operand / null-or-missing sign_convention / a
relationship not in the enum) and ``merit_math_crosscheck`` (a REFUSED cross-check
step; OR a ``warn`` step without ``confirm_crosscheck``). Everything downstream REUSES
the Phase-A recipe families verbatim.

Live ZOS-API integration; unit-tested against fixture-seeded fakes
(``FakeRecipeMFE``) whose DIFF.Value is COMPUTED from its live ``Op#`` pointers
(so a remap bug yields the WRONG value).
"""
from ..enums import _resolve_enum
from ..errors import ToolParamError
from ..server import ToolSpec
from . import _config_common as _ccfg
from . import _merit_cells as _mc
from . import _merit_math as _mm
from . import _optimize_common as _oc
from .optimize_merit_io import _phase1_validate, apply_merit_recipe


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
        "block in a per-config CONF k bracket. Takes a CODE, never a phrase (you do the "
        "grounding via lookup_operand). See add_operand, apply_merit_recipe, "
        "build_merit."
    ),
)

TOOL_SPECS = (ADD_MATH_CONSTRAINT_SPEC,)
