"""server_mcp.py — thin MCP adapter over the dispatch core (LAZY mcp import).

``mcp`` is NOT in the env, so this module must import cleanly without
it. ``build_mcp_server`` imports ``mcp`` INSIDE the function body — there is zero
top-level ``import mcp`` — so the module loads anywhere; the dependency is needed
only when an MCP server is actually built.

The adapter is intentionally thin: it mirrors ``Dispatcher.list_tools()`` as the
MCP tool list and routes every ``call_tool`` straight to ``Dispatcher.dispatch``,
whose never-raise envelope becomes the tool result.

Live ZOS-API integration: N/A this tier (the adapter wraps the dispatcher; the
dispatcher is what touches the backend).

Param re-parse: the all-string ``inputSchema`` makes the MCP client
string-coerce every outgoing arg (``25 -> "25"``, ``[[0,0,1]] -> "[[0,0,1]]"``),
which the strict probe-grounded handlers reject — so NO numeric/geometry/dict
write is callable from a CC-driven session. ``_reparse_arguments`` undoes that
coercion at the ADAPTER boundary (not the dispatcher): in-process callers pass
already-typed args and must NOT be reparsed, and the handlers are the
re-validation net — a genuine string survives (real strings aren't valid JSON)
and a mis-coerce fails LOUDLY in the handler rather than silently
corrupting. Only the MCP path, where the coercion actually happens,
routes through the shim.
"""
import copy
import json
import logging
import re

from .server import Dispatcher


# A GENERIC tool-name-shaped token: a snake_case identifier with AT LEAST one
# underscore (a multi-word lowercase identifier). The underscore is the
# discriminator that separates a tool name (``find_coating``, ``lookup_operand``,
# ``search_reference``) from an ordinary English prose word (``tolerance``,
# ``operand``, ``firewall``) — a single-word lowercase token is NOT treated as a
# tool name. This is deliberately SHAPE-AGNOSTIC (it does NOT hard-code the
# ``lookup_*`` / ``find_glass*`` / ``search_reference`` prefixes), so a future
# reference door of ANY shape (``find_coating``, ``match_glass``, ``resolve_field``)
# is mined too. It only needs to catch tool-name-shaped tokens; false positives on
# ordinary prose are avoided because the runtime invariant + the drift test both
# INTERSECT this token set with the set of KNOWN tool names (manifest ∪ the
# reference-door constant), so an underscore-bearing non-tool phrase that is not a
# known tool is simply ignored.
# A tool-name-shaped token: a snake_case identifier with AT LEAST one underscore.
# Used ONLY to DISCOVER candidate (possibly future / unknown) reference doors of any
# shape (see the drift test); it is NOT the invariant's matcher — the invariant matches
# KNOWN tool names by whole-word presence (``_word_tokens``), so a single-word tool name
# (``optimize``) is NOT missed just because it lacks an underscore.
_TOOL_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# A GENERIC lowercase word token (letters/digits/underscores) — single OR multi word.
# This is the invariant's miner: every whole-word identifier in the text, so a known
# tool of ANY shape (``optimize``, ``get_mtf``, ``trace_rays``) is seen. False positives
# on prose are excluded by INTERSECTING with the known-tool universe, not by token shape.
_WORD_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9_]*\b")


def _tool_shaped_tokens(text):
    """Every tool-name-shaped (snake_case, underscore-bearing) token in ``text``.

    DISCOVERY helper for the drift test (mines underscore-bearing door candidates of
    any prefix). NOT the invariant matcher — see ``_word_tokens``. Returns a lowercase set.
    """
    return {m.group(0).lower() for m in _TOOL_TOKEN_RE.finditer(text or "")}


def _word_tokens(text):
    """Every whole-word lowercase identifier in ``text`` (single OR multi word).

    The invariant's miner: it must catch a single-word tool name (``optimize``) too, so
    matching is by whole-word presence against the known-tool universe — NOT by the
    underscore shape (which would miss every single-word tool). Returns a lowercase set.
    """
    return {m.group(0).lower() for m in _WORD_TOKEN_RE.finditer(text or "")}


def _instructions_name_only_present_tools(instructions, manifest_names):
    """The COMPOSE-TIME INVARIANT: the served instructions must never name a KNOWN
    tool that is absent from the served manifest.

    "Known tool" = a name OptiVibe recognises as a real tool/door — the union of (a)
    the served manifest names, (b) ``_ADDENDUM_REFERENCE_DOORS`` (the reference-door
    constant), and (c) the curated instruction tool names
    (``_BASE_INSTRUCTION_TOOL_NAMES`` + ``_ADDENDUM_INSTRUCTION_TOOL_NAMES`` — the
    harness tools the prose names, INCLUDING single-word ones like ``optimize``).
    Branch (c) is load-bearing: WITHOUT it a harness tool the base prose names but that
    is ABSENT from the served manifest would be silently dropped (it is in neither the
    manifest nor the door constant), so the self-check would be a no-op for exactly the
    tools it guards.

    Matching is by WHOLE-WORD presence (``_word_tokens``), NOT the underscore-bearing
    shape — so a single-word tool name (``optimize``) absent from the manifest is caught,
    not missed. An ordinary prose word (``tolerance``) or a non-tool underscore phrase
    (``sign_convention``) is ignored because it is not in the known-tool universe, so this
    does not false-positive on prose. Returns the SORTED list of offending
    (named-but-absent) known tools; an empty list means the invariant HOLDS.

    This is the belt-and-suspenders runtime check that makes the absent-tool invariant
    hold regardless of whether the constant / prose / drift test drifted: even a stale
    constant cannot ship an instruction string that names an absent-from-manifest tool,
    because this fires at compose time on the FINAL served string and forces a base-only
    fallback / line excision.
    """
    manifest = set(manifest_names)
    known_tools = (
        manifest
        | set(_ADDENDUM_REFERENCE_DOORS)
        | _BASE_INSTRUCTION_TOOL_NAMES
        | _ADDENDUM_INSTRUCTION_TOOL_NAMES
    )
    named = _word_tokens(instructions)
    offending = (named & known_tools) - manifest
    return sorted(offending)


def _strip_lines_naming(instructions, tokens):
    """Drop every LINE of ``instructions`` that names any token in ``tokens``.

    The fail-closed remediation for an absent-tool violation on a base/floor string
    where there is no safer fallback to swap to: rather than serve a string that names
    an absent tool, EXCISE the offending lines so the served string can no longer name
    one (it loses a guidance line, acceptably, instead of mis-steering toward a tool
    that is not there). Matching is by WHOLE-WORD presence (same as the invariant), so a
    single-word tool name on a line is excised too, and only lines that genuinely name an
    offending tool are removed.
    """
    bad = set(tokens)
    kept = []
    for line in instructions.split("\n"):
        if _word_tokens(line) & bad:
            continue
        kept.append(line)
    return "\n".join(kept)


# The opt-IN allow-list of tools whose handler ACCEPTS config="all" via
# resolve_config_selector (the per-config sweep, driven through evaluate_over_configs).
# The frozenset governs SCHEMA advertisement (the anyOf(number|'all')), NOT read-only-
# ness: most members are read-only graders, but set_vignetting is a MUTATING
# member (it authors per-config FV rows over the sweep) that still advertises 'all'
# because it routes the selector through resolve_config_selector. ONLY these advertise
# the string "all"; every other config-bearing tool stays bare {"type":"number"} (fail-
# safe: a new config tool never advertises 'all' unless added here). Cross-ref the
# probe ACCEPTS table.
_CONFIG_ALL_TOOLS = frozenset({
    "get_first_order", "analyze_strehl", "analyze_wavefront",
    "analyze_distortion", "analyze_relative_illumination", "analyze_lateral_color",
    "get_operand", "check_clearance", "verify_collimation",
    "set_vignetting",
    "analyze_grin_profile",            # GRIN — the 11th ACCEPTS member
})

# The 11 config-bearing tools that REFUSE config="all" (the fail-safe partition
# sibling). Single-figure heavy-analysis raise via resolve_single_config_selector;
# authoring/index tools take a plain 1-based int with no 'all' semantics. Kept here
# next to _CONFIG_ALL_TOOLS so the exhaustive-partition drift guard (a NEW config
# tool in NEITHER set turns the test RED) reads from one auditable place.
_CONFIG_REFUSES = frozenset({
    # heavy single-figure analysis (resolve_single_config_selector)
    "analyze_axial_color", "get_mtf", "get_spot", "render_layout",
    "describe_surfaces",
    # authoring / index (config = a 1-based int)
    "set_config_value", "set_config_variable", "set_current_configuration",
    "add_operand", "add_math_constraint", "remove_configuration",
})

# The anyOf that advertises "number OR the literal string 'all'". param_types["config"]
# STAYS "number" (so the reparse string_params set at server_mcp.py :614-617/:760-763 is
# untouched — config is never JSON-string-reparsed; "all" hits json.loads -> ValueError
# -> raw-string fallback -> the literal reaches the handler; validate_input=False so mcp
# 1.28 never hard-rejects the string against the number schema).
_CONFIG_ALL_SCHEMA = {
    "anyOf": [{"type": "number"}, {"type": "string", "enum": ["all"]}]
}


def _build_input_schema(entry):
    """Build an MCP ``inputSchema`` for one ``Dispatcher.list_tools()`` entry.

    If the entry carries per-param type metadata (``param_types``,
    non-empty), emit a TYPED, optional-aware schema — every param (required AND
    optional) becomes a property with its real JSON-Schema type, and ``required``
    is the dispatcher's ``required_params`` (a flat strict-subset; conditional
    requiredness is the handler's job, D3). Tools WITHOUT
    ``param_types`` (the reference dispatcher's specs) fall back to the LEGACY
    all-string-over-required-params schema — unchanged behavior (the shim still
    un-stringifies them at the boundary).

    A ``config`` param on a tool in ``_CONFIG_ALL_TOOLS`` is advertised
    as an ``anyOf`` (number OR the literal string ``"all"``) — the per-config sweep
    selector. Every other config-bearing tool keeps bare ``{"type":"number"}``
    (fail-safe opt-IN: an un-listed tool never advertises ``'all'``).
    """
    param_types = entry.get("param_types") or {}
    required = list(entry["required_params"])
    if param_types:
        allow_all = entry["name"] in _CONFIG_ALL_TOOLS
        properties = {}
        for p, t in param_types.items():
            if allow_all and p == "config" and t == "number":
                # Fresh per-emit deep copy — each served tool gets its OWN object
                # (incl. the nested type dicts), never the shared module-level
                # template, so a downstream mutation of one tool's config property
                # can't corrupt every other ACCEPTS tool (the composite.py
                # _copy_entry aliasing footgun, closed here too).
                properties[p] = copy.deepcopy(_CONFIG_ALL_SCHEMA)
            else:
                properties[p] = {"type": t}
        return {"type": "object", "properties": properties, "required": required}
    # Legacy fallback (no metadata): all-string over the required params only.
    return {
        "type": "object",
        "properties": {p: {"type": "string"} for p in required},
        "required": required,
    }


def _reject_nonfinite_constant(_token):
    # json.loads calls this for the bare tokens 'NaN'/'Infinity'/'-Infinity'.
    # Raising makes the whole parse fail -> raw-string fallback -> the strict
    # handler re-validates the raw string LOUDLY (the shim must never emit a
    # non-finite float from client coercion). The intended infinity path is the
    # string "inf" handled by the handlers' own _coerce_inf, which is untouched
    # here ("inf" is not valid JSON, so it was never reparsed).
    raise ValueError("non-finite JSON constant rejected at the MCP boundary")


def _reparse_arguments(arguments, string_params=frozenset()):
    """Undo MCP-client string coercion: json.loads each string, raw fallback.

    Type-awareness: a param whose declared type is ``"string"``
    (its name in ``string_params``) is passed through UNTOUCHED — a string
    param's value must NEVER be JSON-coerced. Otherwise a legit string that
    happens to be a bare JSON literal would be mangled (``design_name="123"``
    -> ``123``; ``label="true"`` -> ``True``), and the strict handler would
    then LOUD-reject a perfectly valid value. Tools with NO ``param_types``
    (the reference dispatcher's specs) pass an empty ``string_params`` -> every
    string still reparses (UNCHANGED legacy behavior — they need the shim to
    un-stringify their numeric params).

    A ``parse_constant`` hook REJECTS the JSON non-finite tokens
    (``NaN``/``Infinity``/``-Infinity``) at ANY nesting depth — they would
    otherwise coerce to a real ``float('nan')``/``float('inf')`` and pass the
    handlers' ``isinstance(x,(int,float))`` gate as a SILENT non-finite write.
    Rejecting forces the raw-string fallback so the strict handler re-validates
    LOUDLY. The legit infinity path (the lowercase ``"inf"``/``"-inf"`` string,
    not valid JSON, so never reparsed → handled by ``_coerce_inf``) is untouched.
    The except is widened to ``RecursionError`` (a pathologically deep nested
    string makes ``json.loads`` recurse) so the shim NEVER raises.
    """
    out = {}
    for k, v in (arguments or {}).items():
        if isinstance(v, str) and k not in string_params:
            try:
                out[k] = json.loads(v, parse_constant=_reject_nonfinite_constant)
            except (ValueError, TypeError, RecursionError):
                out[k] = v
        else:
            out[k] = v
    return out


# Server-instructions preamble (the standing disciplines a CC-driven session needs;
# they live only in CLAUDE.md + the doc tree otherwise — the MCP client never sees
# them). Split base + reference addendum (D1): the base applies with the
# harness tools alone and MUST NOT name a reference tool (the absent-tool hazard); the
# addendum is appended ONLY when the reference tool set is composed in.
HARNESS_INSTRUCTIONS = (
    "OptiVibe — a vendor-neutral harness for optical (lens) design automation.\n"
    "You drive Zemax OpticStudio through typed tools; reason about the design, the\n"
    "tools take the verified action.\n"
    "\n"
    "Session model:\n"
    "- Single seat (N=1): ONE OpticStudio engine, one design at a time. Calls are\n"
    "  serialized; do not assume concurrency.\n"
    "- Lazy engine-open: the engine seat is taken on the FIRST design-touching tool\n"
    "  call, not at startup; thereafter every call serializes on that single seat.\n"
    "- Reaping is automatic — the engine is closed for you on shutdown; you never\n"
    "  manage process lifecycle.\n"
    "\n"
    "Trust model (proof, not absence-of-error):\n"
    "- A clean call is NOT proof of success. Trust the read-back value or the\n"
    "  optimizer verdict the tool returns, never merely that the call did not raise —\n"
    "  EXCEPT on a cell driven by a solve, where the read-back is the engine's own\n"
    "  recomputation (see Solve discipline below).\n"
    "- Tools return a uniform envelope and NEVER raise — inspect result.ok and the\n"
    "  read-back, not an exception.\n"
    "\n"
    "Solve discipline (a driven cell is not a stored number):\n"
    "- read_surface and describe_surfaces disclose non-default solves per cell under\n"
    "  `solves` (radius/thickness/conic/semi_diameter/material). An ABSENT `solves`\n"
    "  key means every cell carries its DEFAULT solve — not that solves went\n"
    "  unchecked. `solves_unreadable` lists cells whose solve state did not reach you;\n"
    "  it has two provenances (a cell-read fault, or a whole-block fault) and you\n"
    "  cannot tell which, so do not infer one.\n"
    "- THIS EXCEPTS THE TRUST RULE ABOVE: on a driven cell the read-back is the\n"
    "  engine's own recomputation, so it can agree with the SOLVE while disagreeing\n"
    "  with what you asked for. Read `solves` before concluding a write landed.\n"
    "- WHICH TYPES ARE DRIVING: Fixed, Variable, Automatic and \"None\" are NOT — a\n"
    "  cell carrying one of those is an ordinary writable number and set_surface\n"
    "  writes it normally. EVERY OTHER type is driving (SurfacePickup,\n"
    "  MarginalRayHeight, ChiefRayHeight, …). So the mere PRESENCE of a cell under\n"
    "  `solves` does not mean you must not write it: read the type. A Variable is an\n"
    "  optimizer degree of freedom, not a constraint on your write — write it, and\n"
    "  expect the optimizer to move it afterwards.\n"
    "- set_surface REFUSES a write to a driven cell before mutating anything\n"
    "  (error_family solve_driven), naming the cell and its solve type. Retrying will\n"
    "  not help, and set_surface has NO override: change the relationship or write a\n"
    "  different cell. It ALSO refuses when the cell is listed in `solves_unreadable`\n"
    "  — an unreadable solve is treated as possibly driving, never as absent.\n"
    "- set_variable and vary likewise REFUSE a cell already driven by a solve, because\n"
    "  making it a variable DELETES that relationship. Pass replace_solve=true to do\n"
    "  it deliberately — each replacement comes back in `replaced_solves`.\n"
    "- The block names the solve TYPE and its FIELD NAMES, never values: it tells you\n"
    "  a cell is constrained, not what it is constrained to.\n"
    "- \"None\" is a REAL solve type meaning 'no solve' — a positive finding, never a\n"
    "  failed read. It is suppressed like a default, so you will not normally see it\n"
    "  under `solves`; if you do, it is not driving and the cell is writable.\n"
    "- `par_cell_solves_not_audited` (read_surface / describe_surfaces; per surface, a\n"
    "  boolean) = this surface carries parameter cells outside those five and their\n"
    "  SOLVE STATE was NOT inspected (it derives from an independent type read, so it\n"
    "  can disagree with the row's own type on a degraded read). Do NOT confuse it with\n"
    "  remove_surface's `par_refs_not_audited`, which is a LIST and answers a different\n"
    "  question: was that table searched for REFERENCES to the row being removed. For a\n"
    "  coordinate break the two legitimately disagree — the read door says not-audited,\n"
    "  the remove door DID audit it.\n"
    "  `mce_overrides_not_audited` = multi-configuration system, only the CURRENT\n"
    "  configuration was read.\n"
    "- Material writes flow through substitute_glass and are NOT covered by the solve\n"
    "  refusal.\n"
    "\n"
    "Authoring a solve:\n"
    "- set_solve(surface, cell, solve_type, fields) authors a relationship solve on a\n"
    "  radius / thickness / conic / semi_diameter / material cell. It validates\n"
    "  solve_type against that cell's OWN live legal set and refuses a type the cell\n"
    "  does not offer, naming the set. The field names it takes are the ones\n"
    "  read_surface prints under `solves.<cell>.fields` — one vocabulary, both\n"
    "  directions.\n"
    "- A SurfacePickup requires Surface, Column, ScaleFactor and Offset stated\n"
    "  EXPLICITLY; the tool then verifies the arithmetic you stated against an\n"
    "  independent read of the source cell, and rolls back if it disagrees. Column\n"
    "  takes a cell token (radius/thickness/conic/semi_diameter/material) or the\n"
    "  SurfaceColumn name (Radius/Thickness/Conic/SemiDiameter/Material) — that is\n"
    "  the whole vocabulary, and a wrong one is refused with the set named.\n"
    "- A RADIUS PICKUP SCALES CURVATURE, so the factor is a DIVISOR of the radius:\n"
    "    R_target = R_source / ScaleFactor\n"
    "  ScaleFactor=2 gives HALF the source radius. For TWICE the radius pass 0.5.\n"
    "  This is the one place set_solve can author a design you did not mean and\n"
    "  still report relation:verified — it verifies the relationship you EXPRESSED,\n"
    "  not the one you intended, and no read-back can tell them apart. The result\n"
    "  echoes scaled_quantity ('curvature' on a radius target, 'value' otherwise);\n"
    "  read it before trusting a radius pickup. An Offset is honoured only on a\n"
    "  thickness target; a NEGATIVE ScaleFactor is refused on semi_diameter, where\n"
    "  the engine ignores the sign.\n"
    "- It does NOT author Variable (use set_variable), Fixed or \"None\" (use\n"
    "  clear_solve), or Automatic (clear_solve on semi_diameter) — each refusal names\n"
    "  the door and the cell's real legal set.\n"
    "- It REPLACES an existing solve and reports what it replaced (`replaced_solve`);\n"
    "  that relationship is reported once and is unrecoverable after.\n"
    "- clear_solve(surface, cell) removes the solve and FREEZES the cell at its\n"
    "  CURRENT value on radius/thickness/conic (`frozen_at` reports the number now\n"
    "  baked in) — it does not restore a default or an earlier number. On\n"
    "  semi_diameter it RE-FLOATS the aperture instead (the freeze_semidiameters\n"
    "  mode='auto' state). On material it clears the SOLVE and REPORTS whether the\n"
    "  GLASS survived — read `glass_unchanged` (true/false/null) and `material`; some\n"
    "  material solves own the glass string, so it is NOT always preserved, `frozen_at`\n"
    "  is null, and substitute_glass is the door for setting glass.\n"
    "- error_family solve_partial_state means a write was attempted, the rollback\n"
    "  could not be completed or PROVEN, and the cell's state is UNKNOWN: reload your\n"
    "  last saved design (load_design) rather than retrying.\n"
    "- Read `solves` on read_surface before authoring; the block tells you what is\n"
    "  already there.\n"
    "\n"
    "Optimization discipline:\n"
    "- Preflight before you run: set variables (set_variable) + a merit function,\n"
    "  then dry_run to confirm readiness WITHOUT opening the optimizer.\n"
    "- optimize/dry_run default-REFUSE a glass-vertex aperture stop\n"
    "  (require_free_stop=True): run normalize_stop first (or pass auto_normalize),\n"
    "  otherwise the run is refused with no optimizer opened.\n"
    "- A glass-vertex stop on the FRONT lens vertex is also auto-handled by\n"
    "  normalize_stop (it inserts a zero-thickness dummy AIR stop ahead of the front\n"
    "  glass; the OBJECT gap must be air) — do not hand-build a dummy stop.\n"
    "- When a wide-field/fast merit is uncomputable (a corner ray cannot trace at full\n"
    "  pupil), apply corner vignetting (set_vignetting) then REBUILD the merit\n"
    "  (build_merit) and re-seed from a gentler form before re-running.\n"
    "- optimize's verdict is QUALIFIED by the geometry audit. improved = the merit FELL\n"
    "  and no confirmed nonphysical measurement was found; stable = the merit did NOT\n"
    "  move outside tolerance (an exact no-op or a sub-tolerance drift) and likewise no\n"
    "  finding. A *_unphysical suffix means a CONFIRMED impossible measurement — reload a\n"
    "  checkpoint. Exactly three are checked: a negative interior air-gap CENTRE, a\n"
    "  negative glass edge/centre, and a negative back focal distance; it is NOT an\n"
    "  exhaustive physicality check, so their absence is no news, not a clean bill.\n"
    "  A *_unverified suffix means the audit did not establish anything —\n"
    "  either it provably did not run (folded system / refused / threw / returned no\n"
    "  gaps) or its result could not be read (reason 'qualifier_failed'); read\n"
    "  geometry_audit.reason to tell which. merit_verdict always carries the raw merit\n"
    "  answer; geometry_audit carries the evidence.\n"
    "- LIMITATION: geometry_audit.status 'no_findings' means the audit RAN and found no\n"
    "  confirmed nonphysical measurement — it does NOT mean the geometry is sound. Absence\n"
    "  of a finding is not proof of clean geometry; before you keep a design, still run\n"
    "  check_clearance and look at the layout.\n"
    "\n"
    "Checkpoint discipline (persist the design as you go):\n"
    "- Before you optimize, save_snapshot (or save_candidate) the START form — optimize\n"
    "  also auto-captures a per-pass .zmx trail.\n"
    "- After each accepted optimize, save_candidate the result; once you have a keeper,\n"
    "  promote_best it.\n"
    "- Reuse ONE design_name for a given design so its candidates share one trail.\n"
    "- Saves land in the working folder (candidates/, BEST_<design>.zmx). A save into a\n"
    "  missing/unwritable folder fails LOUD — inspect result.ok, do not assume it wrote.\n"
    "- Tolerance a design LOADED from disk via load_design, not a freshly-built\n"
    "  in-memory system: an unsaved/just-built system produces an empty tolerance\n"
    "  report (the engine blesses only a saved .zmx loaded fresh from its own path) —\n"
    "  save it, then load_design it, then tolerance.\n"
    "- The tolerance criterion already includes per-perturbation paraxial back-focus\n"
    "  refocus — do NOT tell the user as-built performance is better than the reported\n"
    "  numbers; compensator_participates:false refers only to inert user COMP/CPAR\n"
    "  operands (back-focus refocus IS applied; it is the only compensator today).\n"
    "\n"
    "Clearance / visual gate at save time:\n"
    "- save_candidate runs a clearance check and WARNS (never blocks) when the design\n"
    "  is manufacturably thin or no figure was rendered — when it does, render_layout\n"
    "  and describe the design, and confirm with the user before promoting.\n"
    "- promote_best REFUSES a manufacturably-thin design (or one whose clearance could\n"
    "  not be audited) unless you pass force=true; it audits the LIVE system geometry\n"
    "  at config=all, so promote right after saving (do not mutate the system in\n"
    "  between) — or run check_clearance yourself to see the full audit.\n"
    "- LIMITATION: clearance_ok:true means the per-gap audit produced an UNBROKEN run of gap\n"
    "  records, from the first optical gap through the back airgap, each with a finite edge\n"
    "  thickness, and found no violation. It is a claim about COVERAGE, not correctness — it\n"
    "  is not checked against an independent surface count, and it does NOT mean the geometry\n"
    "  is right. Absence of a finding is not proof; still look at the layout.\n"
    "- clearance_ok:null means the gate could not CERTIFY clearance. Causes include a FOLDED\n"
    "  system (the per-gap audit is unfolded-only), an unfaithful surface model, an\n"
    "  unreadable global frame and an incomplete config sweep — read\n"
    "  clearance_summary.coverage_reason for WHICH, instead of guessing. promote_best\n"
    "  refuses it; force=true is you asserting the geometry yourself.\n"
    "\n"
    "Communication model (the project workspace IS the channel):\n"
    "- render_layout draws a headless layout figure with OUR surface numbers stamped\n"
    "  on it — the user points at a stamped number to talk about a surface.\n"
    "- describe_surfaces gives the surface-number -> role ground-truth table; pair it\n"
    "  with render_layout so number talk is unambiguous.\n"
    "- save_candidate / promote_best persist designs into a per-design project\n"
    "  workspace for the user to review.\n"
    "\n"
    "Domain-code discipline (resolve from intent, never guess):\n"
    "- Many tools take a domain CODE, an enum member, or a catalog choice (an operand\n"
    "  code, a glass, a tolerance code, an aperture/field/wavelength type). Resolve it\n"
    "  from the design INTENT FIRST, then pass the RESOLVED code — never pass a phrase,\n"
    "  and never trust the first ranked hit blindly within an ambiguous family.\n"
    "- A system enum member (aperture/field/wavelength type) is validated against the\n"
    "  live engine enum: a wrong member is loud-rejected (inspect result.ok), not\n"
    "  silently mis-applied — so pick the member that matches the intent.\n"
    "\n"
    "Diffraction gratings:\n"
    "- The type (reflective vs transmissive) is a load-bearing user declaration —\n"
    "  set_diffraction_grating REQUIRES reflective and will not guess; if the design\n"
    "  spec doesn't state it, ASK the user before authoring.\n"
    "- A grating's diffraction angle is deterministic (the grating equation, given\n"
    "  type/order/wavelength/line-density) — compute it yourself; there is no angle tool.\n"
    "\n"
    "Obscured/annular pupils:\n"
    "- A central obscuration (e.g. a Cassegrain secondary shadow) is a\n"
    "  CircularObscuration on the obstructing surface — author it with\n"
    "  set_surface_aperture so PSF/MTF/Strehl/encircled-energy are computed on the TRUE\n"
    "  obscured pupil, not the full circle (a central CircularObscuration min_radius=0,\n"
    "  max_radius=R + an outer CircularAperture forms the annulus).\n"
    "\n"
    "Afocal / collimated output (image at infinity):\n"
    "- A forward collimator, beam-expander, or reverse-telescope output forms its\n"
    "  image AT INFINITY — there is no image plane for a focus metric to land on. On\n"
    "  such a system Strehl, RMS wavefront, MTF, and spot at the image plane are\n"
    "  MEANINGLESS (a focus scan optimizes nonsense and can report a plausible-but-\n"
    "  wrong number — e.g. a 973-wave RMS). Grade a collimated output with\n"
    "  verify_collimation (per-field RMS angular residual + chief pointing + edge\n"
    "  slope, in mrad), NOT analyze_strehl/analyze_wavefront. When the analyzers detect\n"
    "  a collimated output they quarantine the image-plane headline (null it) and flag\n"
    "  collimated_output — trust the flag, not a nulled headline. A collimator is a\n"
    "  reversed imager (a point at the focus -> parallel out); declaring the system\n"
    "  afocal (AFocalImageSpace) keeps the model honest but does NOT by itself make the\n"
    "  focus metrics meaningful — verify_collimation is the grading tool.\n"
    "\n"
    "Asphere variable discipline:\n"
    "- When asphere polynomial coefficients (A4, A6, ...) are variable on a surface,\n"
    "  do NOT also vary the conic K (set_variable cell=\"conic\") — K contributes ~r^4,\n"
    "  collinear with A4, and both variable is rank-deficient (degenerate). Free ONE:\n"
    "  typically conic for reflective/pure-conic, polynomial for refractive aspheres.\n"
    "  set_variable and set_asphere_variable warn if both are active, but do not block.\n"
    "\n"
    "Variable lifecycle (inherited optimizer variables):\n"
    "- A design LOADED from disk (load_design) or applied via apply_lens_spec carries in\n"
    "  its optimizer VARIABLES — the Variable solves survive a load. load_design /\n"
    "  apply_lens_spec disclose them (inherited_variables / n_inherited_variables); use\n"
    "  list_variables to inspect EVERY variable (LDE + asphere + per-config MCE) before you\n"
    "  optimize, so an inherited variable can't silently drive optimization.\n"
    "- Before deliberately re-varying from scratch, clear_all_variables to reset the DOF\n"
    "  set to none (or pass reset_variables=true to load_design / apply_lens_spec).\n"
    "- A clear/reset FREEZES each cell AT ITS CURRENT value — it does NOT restore an\n"
    "  original. If the design is mid-optimize at a bad value, the clear locks it in;\n"
    "  restore a snapshot (save_snapshot / load_design) for a bad value, then clear.\n"
    "\n"
    "Zoom / multi-configuration design strategy (EFL-span multi-config designs):\n"
    "- Architecture: give a zoom ENOUGH zoom-variable air spaces AND a FIXED positive\n"
    "  master/relay group — decouple imaging from the zoom kernel. Prefer >=3\n"
    "  zoom-variable air gaps + a fixed rear master group; a 2-moving-gap form often\n"
    "  has too few DOF for a WIDE zoom ratio — the compensator must hold EFL AND focus\n"
    "  at once and can run out of travel at the tele extreme. Too few per-config\n"
    "  thickness DOF for the requested zoom ratio is a common failure — add groups\n"
    "  before fighting an un-focusable extreme.\n"
    "- Per-config bounds: per-config air/glass thickness FLOORS are REQUIRED. A bare\n"
    "  span merit lets the optimizer drive a per-config air gap NEGATIVE (overlapping\n"
    "  elements) into a garbage low-merit basin. Author per-config floors as CONF\n"
    "  blocks, and pair CENTER and EDGE glass floors (build_merit min_glass authors\n"
    "  both) — a wide front group goes thin at the EDGE while the center floor holds.\n"
    "  Build the merit with build_merit(span_configs=true) so per-config thickness\n"
    "  floors bind the back-airgap in EVERY config; a single-config build leaves the\n"
    "  other configs' gaps unfloored (they can go negative under optimize) — the\n"
    "  merit_single_config warning names them.\n"
    "- EFL weighting: a per-config EFFL constraint must DOMINATE the spot merit, or\n"
    "  the optimizer collapses every config to ONE focal length (a non-zooming local\n"
    "  min). Weight per-config EFFL well above the spot operands (scale by the\n"
    "  spot-operand count) and SEED separated zoom positions before the first optimize.\n"
    "- Never trust the scalar multi-config merit: an RMS merit DILUTES one broken\n"
    "  config across all operands, so a low merit can hide a bad position. After\n"
    "  optimize, read PER-CONFIG performance with the config=\"all\" graders\n"
    "  (get_first_order, analyze_strehl, analyze_wavefront, check_clearance) — never\n"
    "  accept the scalar merit as proof a multi-config design is good.\n"
    "- Constant-image-height zooms: prefer a ParaxialImageHeight field type (constant\n"
    "  across configs) over per-config field angles.\n"
    "\n"
    "Zoom tool workflow (drive a multi-config / wide-field design with these tools):\n"
    "- Wide field / wide pupil: enable real ray aiming (set_ray_aiming mode=\"real\")\n"
    "  when get_first_order flags ray_aiming_recommended (a displaced entrance pupil),\n"
    "  and set vignetting factors (set_vignetting mode=\"from_rays\") when a fast/wide\n"
    "  corner ray fails to trace (the merit reads the could-not-compute sentinel) —\n"
    "  then REBUILD the merit (build_merit), since it bakes the pupil sampling at build\n"
    "  time.\n"
    "- Stop zooms by DEFAULT: set_zoom(mode=\"zoom\", hold_fnum=true) holds f/# across\n"
    "  the zoom (the aperture stop zooms while EFL changes); pass fnum for a specific\n"
    "  f/#, or hold_fnum=false to opt out for a deliberately variable-f/# zoom.\n"
    "- Re-target a per-config EFFL IN PLACE with edit_operand (number, target, weight)\n"
    "  — NOT remove+re-add, which appends and breaks the CONF bracket.\n"
    "- Before presenting a multi-config design: freeze_semidiameters (so each element\n"
    "  draws at ONE size across configs — a physically-correct layout) and verify_zoom\n"
    "  (flag a gap declared to zoom that is CONSTANT across configs).\n"
    "- To rebuild a multi-config (zoom) design as a single-config system, call\n"
    "  reset_to_single_config (it collapses to one config, bakes the active config into\n"
    "  the design, clears the per-config MCE rows, and unblocks read_lens_spec /\n"
    "  apply_lens_spec, which refuse a multi-config system) — do NOT load an unrelated\n"
    "  blank file. To drop ONE configuration, call remove_configuration(config=n) (delete\n"
    "  the highest index first when removing several; the active config index may shift —\n"
    "  inspect current_after).\n"
    "\n"
    "MTF-aware merit (a corner tangential-MTF reversal RMS-spot is blind to):\n"
    "- An RMS-spot merit is BLIND to a corner tangential-MTF collapse — a design can\n"
    "  read a fine spot while the corner tangential MTF has reversed (tangential\n"
    "  contrast near 0 while sagittal holds). To CONSTRAIN MTF, add MTFT (tangential)\n"
    "  and MTFS (sagittal) operands via add_operand with\n"
    "  params={'Field':<1-based index>,'Freq':<cyc/mm>}. The field is an integer Field\n"
    "  INDEX (NOT Hx/Hy — a pupil coordinate would silently read the on-axis field);\n"
    "  an omitted Field reads on-axis (a Field=0 operand is flagged, not refused).\n"
    "- Spot-check the operand reading against get_mtf at the same field+frequency (two\n"
    "  independent engine paths) before trusting it.\n"
    "\n"
    "Preserving hand-authored merit rows across a rebuild:\n"
    "- A build_merit rebuild (e.g. forced by a set_vignetting change, which needs a\n"
    "  fresh merit) REPLACES the WHOLE merit function — the wizard rows are re-created\n"
    "  and any operands YOU added (EFFL/DIMX/CTGT/RWCE/MTFT/...) are DELETED. To keep\n"
    "  them: before rebuilding, serialize_merit and slice out ONLY the entries you\n"
    "  authored (the operands AFTER the wizard's block) — NOT the wizard's own\n"
    "  ray/boundary operands. After the rebuild, apply_merit_recipe(mode='append') with\n"
    "  that custom slice re-appends them; do not re-add each row by hand.\n"
    "- The SLICE is yours to make (you authored those rows): serialize_merit captures\n"
    "  ALL non-structural operands INCLUDING the wizard's, so appending the UNSLICED\n"
    "  recipe onto a fresh wizard build DOUBLES the wizard rows. Keep only your tail.\n"
    "- Carry glass=true on the rebuild (build_merit(preserve_custom=true, glass=true)) so\n"
    "  the wizard's glass floors AND the per-glass-surface ETGT true-edge floors are\n"
    "  REGENERATED (they are glass=true-only, exactly like MNEG/MNCG — a glass=false\n"
    "  rebuild drops them all).\n"
    "\n"
    "GRIN (gradient-index) design workflow:\n"
    "- Cell convention (READ BEFORE AUTHORING): a Gradient2's Par polynomial is the index\n"
    "  SQUARED (n^2 = n0 + Nr2*r^2 + ...), exactly as Zemax's Gradient 2 defines it, so\n"
    "  set_grin(n0=2.25) builds a medium of PHYSICAL index 1.5 — pass n^2 for a Gradient2.\n"
    "  A Gradient3's polynomial is the index itself (n = n0 + ... + Nz1*z + ...). set_grin\n"
    "  takes the cell value verbatim; analyze_grin_profile and the build_merit index box\n"
    "  (grin_dn_max/grin_min_index) are in PHYSICAL index for both types.\n"
    "- Choose a topology: Gradient2 (radial) and Gradient3 (radial+axial) are the\n"
    "  authorable primitives (set_grin); pair an axial Nz coefficient DOF with a\n"
    "  genuinely powered element — a flat axial window supplies piston/OPL, not\n"
    "  focusing power. Gradium, Grid Gradient and the other GRIN family members are\n"
    "  refuse-tier (set_grin refuses them loud, naming the reason).\n"
    "- Author, then read back: verify the exact surface type + coefficient cells after\n"
    "  set_grin; do NOT route GRIN authoring through LensSpec (it is REFUSE-FIRST).\n"
    "- Interpret first-order (get_first_order): place the image surface near paraxial\n"
    "  focus before treating readout heuristics as defects — a bare flat axial window\n"
    "  may legitimately read afocal.\n"
    "- Discover optimization state: inspect list_variables (the returned variables key)\n"
    "  so the GRIN coefficient AND a real powered-element variable are BOTH active.\n"
    "  Author a manufacturable index envelope (build_merit grin_dn_max=..., grin_min_index)\n"
    "  BEFORE making a coefficient variable (set_grin_variable).\n"
    "- Verify a flat axial GRIN: trace_rays(opd_mode='Current') -> abs(opd)*lambda_um*1e-3\n"
    "  mm, differenced vs a homogeneous control; read the radial index field with\n"
    "  analyze_grin_profile.\n"
    "- Manufacturability: an AUTHORED Gradient2/Gradient3 element is a SOLID element —\n"
    "  its edge AND center clearance IS audited by check_clearance at the glass floors\n"
    "  (min_glass), and optimize/check_clearance carry that coverage via\n"
    "  grin_geometric_audit — a POSITIVE record of which GRIN surfaces were audited as a\n"
    "  solid element, NOT a per-surface violation flag (a real thin edge/center still\n"
    "  surfaces in the normal clearance violation channel). build_merit(glass=true)\n"
    "  authors an ETGT EDGE\n"
    "  restoring floor for it (a build-time GRIN CENTER floor / MNCG is a documented\n"
    "  gap in this release; run check_clearance after optimize for\n"
    "  center safety). Tune with min_glass. The internal index profile is monochromatic\n"
    "  (grin_wavelength_blind) and NOT drawn. A LOADED non-authorable GRIN family member\n"
    "  (Gradium, Grid Gradient, ...) is NOT audited — check_clearance/build_merit/optimize\n"
    "  disclose it under grin_not_audited. Tolerance the index-profile COEFFICIENT per\n"
    "  Par# via tolerance (TPAR) — the edge-index change is a derived, profile-dependent\n"
    "  consequence, not a direct edge-index tolerance.\n"
    "- Close-out boundaries: GRIN authoring is single-configuration (a config=\"all\"\n"
    "  readout does NOT imply multi-config authoring); Material/catalog is\n"
    "  expected-negative; CheckGRINApertures stays engine-default (documented numerical\n"
    "  caveat).\n"
    "- Tool tokens (all shipped): set_grin, set_grin_variable, analyze_grin_profile,\n"
    "  trace_rays, list_variables, build_merit, check_clearance, get_first_order,\n"
    "  optimize, tolerance, save_candidate.\n"
    "\n"
    "Standing order: probe-first — verify real backend behavior via a read-back\n"
    "before believing anything backend-dependent.\n"
)

# The COMPLETE set of reference doors the REFERENCE_ADDENDUM prose names as usable
# (the absent-tool hazard at the per-door grain). This is the SINGLE
# SOURCE OF TRUTH for the addendum gate: the served instructions must NEVER name a
# reference door absent from the served manifest, so the addendum is appended IFF
# EVERY door in this set is present in the merged manifest (the SUBSET invariant —
# the addendum's named doors must be a subset of the served tools).
#
# A drift test (test_adv_tool_enrichment) asserts this constant and the prose cannot
# diverge: every door here appears in REFERENCE_ADDENDUM, and the addendum names no
# OTHER reference door — so the checked door-set IS the prose's door-set.
#
# Supersedes the earlier single-token ``_REFERENCE_DOOR_WITNESS`` (kept as an alias
# below for back-compat): a single token proved only ONE of five doors present, which
# (Direction 2) let the addendum name an absent sibling door on a partial set.
_ADDENDUM_REFERENCE_DOORS = (
    "search_reference",
    "lookup_operand",
    "lookup_glass",
    "find_glasses",
    "find_glass_pair",
)

# The HARNESS (non-reference-door) tool names the ADDENDUM prose names. The addendum
# names the 5 reference doors (covered by ``_ADDENDUM_REFERENCE_DOORS``) PLUS these
# harness tools; like ``_BASE_INSTRUCTION_TOOL_NAMES`` they must be in the known-tool
# universe so an addendum-named harness tool absent from the served manifest is caught,
# while the prose's non-tool tokens (``sign_convention``) are EXCLUDED so they never
# false-fallback. (The 5 reference doors live in ``_ADDENDUM_REFERENCE_DOORS`` and are
# unioned separately.)
_ADDENDUM_INSTRUCTION_TOOL_NAMES = frozenset({
    "add_operand",
    "add_math_constraint",
    "load_catalog",
    "list_catalogs",
})

# Back-compat alias — the canonical primary route-map door. Retained so existing
# importers resolve; the GATE is now the full-subset check over
# ``_ADDENDUM_REFERENCE_DOORS``, not this single token.
_REFERENCE_DOOR_WITNESS = "search_reference"

# The HARNESS tool names the BASE prose (``HARNESS_INSTRUCTIONS``) deliberately
# names. This is the SECOND known-tool source (alongside ``_ADDENDUM_REFERENCE_DOORS``)
# for ``_instructions_name_only_present_tools``: without it, a base-named harness tool
# absent from the served manifest is in neither the manifest nor the reference-door
# constant, so the runtime self-check would silently MISS it (an earlier hole). These
# are CURATED tool names ONLY — the prose's NON-tool tokens (``auto_normalize`` /
# ``design_name`` / ``require_free_stop``, param/flag names, not tools; ``tolerance`` /
# ``operand`` / ``glass``, ENGLISH PROSE WORDS that happen to collide with a tool name)
# are deliberately EXCLUDED so they never force a false base-only fallback / line excise.
# Note: SINGLE-WORD tool names the prose genuinely references AS tools (``optimize`` in
# "optimize/dry_run default-REFUSE...") ARE included — the invariant matches by whole word
# (``_word_tokens``), so a single-word tool absent from the manifest must be catchable.
# A drift test pins that every name here is a real served harness tool and is named by the
# base prose, so this stays the prose's true harness-tool-name set.
_BASE_INSTRUCTION_TOOL_NAMES = frozenset({
    "set_variable",
    # Named by the Solve-discipline block (0 ast.stmt: three entries
    # in a frozenset literal). All three are shipped harness tools, so the base build's
    # absent-tool invariant holds on the reference-DEGRADED path too.
    "read_surface",
    "set_surface",
    "vary",
    # The "Authoring a solve:" block's two tools, curated in the
    # SAME change as the prose (a mandatory obligation). Without the curated
    # entries the runtime absent-tool sweep is "a no-op for exactly the tools it
    # guards"; without the prose the sweep would excise the naming lines. Both ship, or
    # neither does — the bidirectional curated-vs-prose pin decides it.
    "set_solve",
    "clear_solve",
    "set_asphere_variable",
    "dry_run",
    "optimize",
    "normalize_stop",
    "save_snapshot",
    "save_candidate",
    "promote_best",
    "load_design",
    "render_layout",
    "describe_surfaces",
    "set_diffraction_grating",
    "set_surface_aperture",
    "verify_collimation",
    # Zoom-strategy block — already-shipped tools the zoom prose names.
    "build_merit",
    "get_first_order",
    "analyze_strehl",
    "analyze_wavefront",
    "check_clearance",
    # Zoom TOOL workflow block — the now-shipped tools the block
    # names. Curated here so the runtime excision sweep RECOGNIZES them as tools and excises
    # the naming line if a degraded manifest drops one (an earlier hole).
    "set_ray_aiming",
    "set_vignetting",
    "edit_operand",
    "freeze_semidiameters",
    "verify_zoom",
    "set_zoom",
    # Multi-config reset block — the now-shipped collapse/delete tools the zoom prose names.
    "reset_to_single_config",
    "remove_configuration",
    # Variable-lifecycle — the inherited-variable inventory + bulk clear the base
    # "Variable lifecycle" block names.
    "list_variables",
    "clear_all_variables",
    # MTF-aware merit — the merit-authoring + MTF cross-check tools the base
    # "MTF-aware merit" block names (add_operand is also addendum-named).
    "add_operand",
    "get_mtf",
    # The preserve-hand-authored-rows-across-rebuild idiom names
    # the serialize/append round-trip tools (build_merit + set_vignetting already above).
    "serialize_merit",
    "apply_merit_recipe",
    # GRIN closeout — the "GRIN (gradient-index) design workflow" block names the
    # now-shipped GRIN authoring/analysis tools; curated here so the runtime excision
    # sweep RECOGNIZES them as tools and excises the naming line if a degraded manifest
    # drops one. (list_variables/build_merit/check_clearance/get_first_order/optimize/
    # save_candidate are already curated above; tolerance stays EXCLUDED — an English
    # prose word that is always a served manifest tool.)
    "set_grin",
    "set_grin_variable",
    "analyze_grin_profile",
    "trace_rays",
})


REFERENCE_ADDENDUM = (
    "\n"
    "Operand disambiguation (reference grounding present):\n"
    "- Resolve operand CODES via lookup_operand BETWEEN calls: read the descriptions\n"
    "  AND the sign_convention; never trust the rank-1 hit blindly (it can be\n"
    "  confidently wrong within an operator family).\n"
    "- add_operand / add_math_constraint take a CODE, not a phrase — do the\n"
    "  intent -> code disambiguation yourself, then pass the resolved code.\n"
    "- add_math_constraint cross-checks relationship x sign_convention (a cross-FAMILY\n"
    "  firewall); it does NOT pick the operator for you — that is your call.\n"
    "- Reference lookups do not take the engine seat; consult them freely.\n"
    "\n"
    "Domain -> reference door (use the RIGHT door per code family):\n"
    "- Merit-function operands -> lookup_operand (read descriptions + sign_convention).\n"
    "- MTF-constraint operands (a corner tangential-MTF reversal RMS-spot misses) ->\n"
    "  lookup_operand to pick the code (MTFT tangential / MTFS sagittal / MTFA...), then\n"
    "  add_operand with params={'Field':<1-based index>,'Freq':<cyc/mm>}.\n"
    "- Glass by name -> lookup_glass; glass by property/intent ('a crown for this\n"
    "  achromat') -> find_glasses / find_glass_pair (a different door than lookup_glass).\n"
    "- Tolerance operands (TDE codes) -> lookup_operand with domain=\"tolerance\" (the\n"
    "  structured 62-row tolerance catalog: ranked codes + category/precondition_class +\n"
    "  units, the same code-not-phrase grounding as merit). lookup_operand WITHOUT\n"
    "  domain=\"tolerance\" mis-routes a tolerance ask: it defaults to the merit catalog\n"
    "  and returns confidently WRONG merit operands (TOLR/VOLU/EQUA) — always pass\n"
    "  domain=\"tolerance\". search_reference is the manual-prose fallback (chapter context).\n"
    "- System aperture/field/wavelength TYPE -> the typed engine enum is the source of\n"
    "  truth (the tool validates the member and loud-rejects a wrong token); use\n"
    "  search_reference if you need help mapping intent to a member.\n"
    "- Glass from a NON-DEFAULT catalog (plastics in MISC: PMMA/POLYSTYR/POLYCARB;\n"
    "  OHARA/HOYA/CDGM) resolves ONLY when its catalog is IN USE — a fresh system has\n"
    "  only SCHOTT. substitute_glass / apply_lens_spec have auto_load defaulting ON, so\n"
    "  a glass in a not-in-use catalog is loaded + DISCLOSED automatically in one call\n"
    "  (the auto_loaded_catalog(s) key names what was loaded). Use list_catalogs to see\n"
    "  in-use vs available; pass auto_load=false for strict explicit control (then it\n"
    "  refuses naming the owner — run load_catalog(name=<that catalog>) and retry).\n"
    "  When auto_load_ambiguous is true the glass is in MULTIPLE catalogs and the FIRST\n"
    "  (GetAvailableCatalogs() order) was picked — if you meant a specific vendor (e.g.\n"
    "  OHARA's S-BSL7 vs LACROIX's), run load_catalog(name=<that catalog>) first to pin\n"
    "  it, then retry.\n"
    "- This is the same code-not-phrase discipline as merit operands, generalized to\n"
    "  every domain: resolve the code from intent at the right door, then call.\n"
)


def build_mcp_server(session):
    """Build an MCP ``Server`` exposing the dispatcher's tools (LAZY mcp import).

    Imports ``mcp`` HERE (never at module top). Mirrors
    ``Dispatcher.list_tools()`` into MCP tool descriptors and routes each
    ``call_tool`` to ``Dispatcher.dispatch``, returning its envelope.
    """
    import mcp.types as mcp_types  # noqa: E402 — lazy: mcp may be absent at import
    from mcp.server import Server  # noqa: E402

    dispatcher = Dispatcher(session)

    # SWEEP-THE-CLASS runtime self-enforcement, symmetric with the
    # composite builder: the harness-only base must never name a tool absent from
    # the served manifest. This trivially passes today (HARNESS_INSTRUCTIONS names
    # no reference door, and the base names only harness tools that ARE served), but
    # the check future-proofs a base-prose edit that names a tool not in the harness
    # manifest. ENFORCE, do not merely log: the base IS the floor
    # here (no safer fallback string to swap to), so on violation we EXCISE the
    # offending lines from the served string — the final served instructions then never
    # name an absent tool, the invariant holds UNCONDITIONALLY on this path too. (This
    # cannot fire on the shipping prose, whose named harness tools are all served; it
    # fail-closes a future base-prose edit that names a non-served tool.)
    _harness_names = {e["name"] for e in dispatcher.list_tools()}
    _instructions = HARNESS_INSTRUCTIONS
    _absent_named = _instructions_name_only_present_tools(_instructions, _harness_names)
    if _absent_named:
        logging.getLogger("optivibe_harness.server_mcp").error(
            "ABSENT-TOOL INVARIANT VIOLATED (harness-only base): instructions name "
            "tool(s) %s absent from the manifest — excising the offending line(s) so "
            "the served instructions name no absent tool.",
            _absent_named,
        )
        _instructions = _strip_lines_naming(_instructions, _absent_named)
        # Re-verify the excision fully cleared the violation (a tool named on a line we
        # could not safely drop would otherwise slip through); if anything remains, the
        # invariant is enforced by refusing to serve a violating string.
        _still = _instructions_name_only_present_tools(_instructions, _harness_names)
        if _still:
            raise ValueError(
                "harness-only instructions still name absent tool(s) after excision: "
                f"{_still}"
            )

    server = Server("optivibe-harness", instructions=_instructions)

    # Per-tool set of params declared "string" — those must NOT be
    # JSON-reparsed (a string value must stay a string). A tool with no
    # param_types (reference specs) -> empty set -> full reparse (legacy).
    _string_params = {
        e["name"]: {p for p, t in (e.get("param_types") or {}).items() if t == "string"}
        for e in dispatcher.list_tools()
    }

    @server.list_tools()
    async def _list_tools():
        tools = []
        for entry in dispatcher.list_tools():
            tools.append(
                mcp_types.Tool(
                    name=entry["name"],
                    description=entry["description"],
                    inputSchema=_build_input_schema(entry),
                )
            )
        return tools

    # validate_input=False: the typed schema would otherwise make mcp 1.28
    # HARD-REJECT a stringified arg before the handler, regressing
    # the string straggler path. With validation off, native args pass
    # straight through and a stringified straggler is un-coerced by the shim;
    # the strict handlers are the re-validation net either way.
    @server.call_tool(validate_input=False)
    async def _call_tool(name, arguments):
        reparsed = _reparse_arguments(arguments, _string_params.get(name, frozenset()))
        envelope = dispatcher.dispatch(name, reparsed)
        return [mcp_types.TextContent(type="text", text=repr(envelope))]

    return server


def build_composite_mcp_server(dispatcher):
    """Build the single OptiVibe MCP ``Server`` over an already-built dispatcher.

    Mirrors the reference ``build_mcp_server(dispatcher)`` contract (takes a
    dispatcher-like object — here the ``CompositeDispatcher`` — NOT a raw session,
    so a third MCP adapter module is avoided; the composite is fronted directly).
    Server name is ``"optivibe"`` (one MCP, one name). LAZY
    ``mcp`` import inside the body (never at module top). Wires
    ``@server.list_tools()`` / ``@server.call_tool()`` straight to the dispatcher's
    ``list_tools()`` / ``dispatch()`` never-raise envelope.

    Addendum selection is MANIFEST-DRIVEN (the absent-tool hazard,
    on the path that SHIPS). ``__main__.main`` ALWAYS calls this
    builder, even when the reference layer degrades to ``None`` (it then composes a
    ``CompositeDispatcher`` with only the harness pair). So the served instructions
    must NOT unconditionally name reference doors.

    THE ENFORCED INVARIANT: the served instructions must NEVER name a
    reference door absent from the served manifest, on ANY composite manifest (full
    / partial / none). The addendum's named doors must be a SUBSET of the served
    tools. We therefore append ``REFERENCE_ADDENDUM`` (which names every door in
    ``_ADDENDUM_REFERENCE_DOORS``) IFF EVERY one of those doors is in the merged
    manifest. Consequences:
      * full reference set served -> addendum (the route map shows);
      * partial set served (some named doors present, some absent) -> base ONLY +
        a LOUD compose-time warning (never name an absent door — the safe direction;
        the partial route map is hidden, acceptably, rather than mis-steering);
      * no reference door served (degraded) -> base only;
      * harness-only ``build_mcp_server`` -> base only (unchanged).

    Supersedes the earlier single-token witness, which only proved ONE of the five
    doors present and so (Direction 2) let the addendum name an absent sibling door
    on a partial set.
    """
    import mcp.types as mcp_types  # noqa: E402 — lazy: mcp may be absent at import
    from mcp.server import Server  # noqa: E402

    # Snapshot the merged manifest ONCE (the composite's list_tools() is a copy per
    # call) and drive BOTH the addendum predicate and the string-param map from it.
    _entries = list(dispatcher.list_tools())
    _names = {e["name"] for e in _entries}

    # SUBSET gate: append the reference addendum IFF EVERY door it
    # names is served. The served instructions can then NEVER name an absent door
    # (Direction 2 closed). A PARTIAL set (some named doors present, some absent) ->
    # base only + a LOUD warning, so a future partial build is surfaced not silently
    # degraded (Direction 1 in the only safe way: hide the route map rather than name
    # an absent door).
    _addendum_doors = frozenset(_ADDENDUM_REFERENCE_DOORS)
    _present = _addendum_doors & _names
    _instructions = HARNESS_INSTRUCTIONS
    if _present == _addendum_doors:
        _instructions = HARNESS_INSTRUCTIONS + REFERENCE_ADDENDUM
    elif _present:
        # SOME but not ALL named doors are served — never name the absent ones.
        logging.getLogger("optivibe_harness.server_mcp").warning(
            "PARTIAL reference manifest: serving base instructions only "
            "(addendum would name absent door(s) %s). Present: %s.",
            sorted(_addendum_doors - _present),
            sorted(_present),
        )

    # SWEEP-THE-CLASS runtime self-enforcement: the subset gate above
    # trusts ``_ADDENDUM_REFERENCE_DOORS`` to be the TRUE set of doors the prose
    # names. A stale constant (a door added to REFERENCE_ADDENDUM but not the
    # constant) would let the gate serve the addendum naming a door absent from the
    # manifest — an earlier hazard, recurred. This GENERIC final-string check makes
    # the absent-tool invariant hold at RUNTIME regardless of constant/prose/test
    # drift: if the FINAL served string names ANY known tool absent from the served
    # manifest, fall back to base-only + a loud error (never serve the addendum that
    # names an absent door).
    #
    # DESIGN NOTE: the runtime check's known-tool universe is
    # ``manifest ∪ constant`` BY DESIGN. It catches a STALE-CONSTANT door — a door
    # named in the prose AND the constant but absent from the manifest. A BRAND-NEW
    # door named only in the prose (in NEITHER manifest nor constant) is invisible to
    # this check and is the generic DRIFT TEST's responsibility
    # (``test_addendum_doors_constant_matches_prose_no_drift`` asserts constant ≡
    # prose-named doors). The two nets jointly cover the door-naming hazard space:
    # the drift test forces prose→constant parity at build time; this runtime check
    # then forces constant→manifest parity on every served string.
    _absent_named = _instructions_name_only_present_tools(_instructions, _names)
    if _absent_named:
        logging.getLogger("optivibe_harness.server_mcp").error(
            "ABSENT-TOOL INVARIANT VIOLATED: served instructions name tool(s) %s "
            "absent from the manifest — falling back to base instructions only. "
            "(Likely a stale _ADDENDUM_REFERENCE_DOORS vs REFERENCE_ADDENDUM prose.)",
            _absent_named,
        )
        _instructions = HARNESS_INSTRUCTIONS
        # The base fallback could ITSELF name an absent tool (a DEGRADED harness manifest
        # missing a base-named harness tool) — base is then the floor with no safer
        # string to swap to, so EXCISE the offending lines and re-verify, exactly as the
        # harness-only builder does. This makes the served string name no absent tool on
        # EVERY composite manifest (full / partial / degraded), unconditionally.
        _absent_base = _instructions_name_only_present_tools(_instructions, _names)
        if _absent_base:
            logging.getLogger("optivibe_harness.server_mcp").error(
                "ABSENT-TOOL INVARIANT: base fallback still names absent tool(s) %s "
                "(degraded harness manifest) — excising the offending line(s).",
                _absent_base,
            )
            _instructions = _strip_lines_naming(_instructions, _absent_base)
            _still = _instructions_name_only_present_tools(_instructions, _names)
            if _still:
                raise ValueError(
                    "composite instructions still name absent tool(s) after excision: "
                    f"{_still}"
                )

    server = Server("optivibe", instructions=_instructions)

    # Per-tool set of params declared "string" — see build_mcp_server.
    # Reference tools carry no param_types -> empty set -> full legacy reparse.
    _string_params = {
        e["name"]: {p for p, t in (e.get("param_types") or {}).items() if t == "string"}
        for e in _entries
    }

    @server.list_tools()
    async def _list_tools():
        # Serve from the SAME ``_entries`` snapshot the addendum predicate read —
        # NOT a fresh ``dispatcher.list_tools()`` — so the served manifest cannot
        # desync from the predicate decision (TOCTOU close): the absent-tool
        # invariant then holds for ANY dispatcher, not only the immutable-snapshot
        # CompositeDispatcher. The real composite snapshots at __init__, so this is
        # behaviour-identical for the shipping path; it future-proofs a flaky one.
        tools = []
        for entry in _entries:
            tools.append(
                mcp_types.Tool(
                    name=entry["name"],
                    description=entry["description"],
                    inputSchema=_build_input_schema(entry),
                )
            )
        return tools

    # validate_input=False — see build_mcp_server: keep the typed
    # schema non-regressive against a stringified straggler; the shim + the strict
    # handlers are the net.
    @server.call_tool(validate_input=False)
    async def _call_tool(name, arguments):
        reparsed = _reparse_arguments(arguments, _string_params.get(name, frozenset()))
        envelope = dispatcher.dispatch(name, reparsed)
        return [mcp_types.TextContent(type="text", text=repr(envelope))]

    return server
