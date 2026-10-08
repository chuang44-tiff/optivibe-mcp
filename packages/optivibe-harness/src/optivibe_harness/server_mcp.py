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
# Case-insensitive: a
# sentence that OPENS with a door name ("Lookup_operand grounds it.") must be seen too --
# the lowercase OUTPUT is _word_tokens's .lower(). Measured 0 served-byte diffs across 95
# descriptions + both instruction compositions at the change.
_WORD_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9_]*\b", re.IGNORECASE)


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


# A sentence break is ``.``/``!``/``?`` + whitespace + the START of a sentence: an uppercase
# letter, optionally after one opening quote/backtick/paren/bracket. A period followed by a
# lowercase word is NEVER a break -- so an abbreviation followed by a lowercase word (e.g./i.e./
# vs./cf./etc./E.G.) never splits, without listing the members; one followed by a CAPITAL
# (``cf. Table``, ``vs. Zemax``) still does. The price, pinned by D13: a sentence that
# names a reference door must itself START with a capital, or it merges into its neighbour
# and a partial composition drops both. The terminal ``.``/``!``/``?`` may be followed by ONE
# closing paren/bracket/quote/backtick (``...door.) Next``), else a parenthesised sentence
# would swallow the one after it. Measured: 0 served-byte diffs and 0 moved split points
# across 7 compositions against the lookbehind-only splitter, except the list_catalogs
# rewording that shipped with it.
_SENTENCE_BREAK_RE = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?][)\]\"'`]))\s+(?=[\"'`(\[]?[A-Z])")


def _description_names_absent_tools(text, manifest_names):
    """Sorted KNOWN tools named by ``text`` (whole-word, ``_word_tokens``) that are absent
    from ``manifest_names``. Known = manifest | _ADDENDUM_REFERENCE_DOORS. Curated harness
    names are deliberately NOT unioned (a harness-partial manifest is not constructible in
    production; the prose word ``tolerance`` is a harness tool name). [] = holds."""
    names = set(manifest_names)
    known = names | set(_ADDENDUM_REFERENCE_DOORS)
    return sorted((_word_tokens(text) & known) - names)


def served_description(name, description, manifest_names):
    """The description a client receives for THIS manifest: every SENTENCE that names a
    known tool absent from ``manifest_names`` is dropped. Byte-identical to ``description``
    when nothing is absent (pinned by D3: a no-op over all 95 -- 90 harness + 5 reference --
    on the full composition). Splits on
    ``.``/``!``/``?`` (optionally + one closer) + whitespace + an uppercase start
    (optionally after a quote/backtick/paren). AUTHORING RULE (pinned by D6/D11): a sentence names at most ONE
    optional door and carries only that door's routing. Does not raise for a ``str`` (or
    ``None``) description and an iterable of names; logs ONE WARNING per call that drops."""
    parts = _SENTENCE_BREAK_RE.split(description or "")
    kept = [p for p in parts if not _description_names_absent_tools(p, manifest_names)]
    if len(kept) == len(parts):
        return description
    logging.getLogger("optivibe_harness.server_mcp").warning(
        "ABSENT-TOOL INVARIANT (descriptions): %s names tool(s) %s absent from the served "
        "manifest -- serving it without those sentence(s).",
        name, _description_names_absent_tools(description, manifest_names))
    return " ".join(kept)


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
    ``param_types`` fall back to the LEGACY all-string-over-required-params schema (the
    shim still un-stringifies them at the boundary); today those are ONLY zero-parameter
    harness tools, whose legacy schema is the empty object -- every reference spec has
    carried ``param_types`` since the typed-schema change.

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
            elif t == "array":
                properties[p] = {"type": "array", "items": {}}
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
    then LOUD-reject a perfectly valid value. A tool with NO ``param_types`` passes an
    empty ``string_params`` -> every string still reparses (the legacy path); today those
    are ONLY zero-parameter harness tools (nothing to reparse), and every reference spec
    carries its ``param_types`` since the typed-schema change.

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
    "EVERY TOOL'S OWN DESCRIPTION IS THE AUTHORITY ON ITS CONTRACT — its arguments,\n"
    "its refusals, and the keys it returns. Read it before first use of that tool and\n"
    "do not carry a rule about one tool in your head from here; this text is only what\n"
    "is true BEFORE any tool is in play.\n"
    "\n"
    "Session model:\n"
    "- Single seat (N=1): ONE OpticStudio engine, one design at a time. Calls are\n"
    "  serialized; do not assume concurrency.\n"
    "- Lazy engine-open: the seat is taken on the FIRST design-touching call, not at\n"
    "  startup. Reaping is automatic — you never manage process lifecycle.\n"
    "\n"
    "Trust model (proof, not absence-of-error):\n"
    "- Tools return a uniform envelope and NEVER raise — inspect result.ok and the\n"
    "  read-back, not an exception. A clean call is NOT proof of success.\n"
    "- A tool REFUSES what it cannot do safely and says why in error_family. A refusal\n"
    "  is information, not an obstacle: read it rather than retrying around it.\n"
    "- Probe-first: verify real backend behaviour via a read-back before believing\n"
    "  anything backend-dependent — INCLUDING what this text says.\n"
    "\n"
    "Codes, not phrases:\n"
    "- Many tools take a domain CODE (a merit operand, a glass, a solve type, a\n"
    "  tolerance). Resolve it from the design INTENT first, then pass the RESOLVED\n"
    "  code — never a phrase, and never the first ranked hit inside an ambiguous\n"
    "  family. The tool that takes the code names the door that resolves it, if one is served.\n"
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
_ADDENDUM_INSTRUCTION_TOOL_NAMES = frozenset()
#: EMPTY as of the Stage-2 addendum (4.1): the one-line addendum names only
#: the five reference doors, which are covered by ``_ADDENDUM_REFERENCE_DOORS`` and
#: unioned separately. The harness tools the OLD addendum named (add_operand,
#: add_math_constraint, load_catalog, list_catalogs) are named there no longer -- their
#: rules moved into those tools' own descriptions.
#:
#: KEPT for the same reason as ``_BASE_INSTRUCTION_TOOL_NAMES`` above.

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
_BASE_INSTRUCTION_TOOL_NAMES = frozenset()
#: EMPTY as of the Stage-2 head (4.1): the base prose names NO tool. It is
#: session model, trust model and code-not-phrase discipline -- every statement about a
#: specific tool now lives in that tool's own description, which is delivered in full on
#: load. The drift test asserts the base names no tool, so this emptiness is CHECKED, not
#: assumed.
#:
#: KEPT, not deleted. This is branch (c) of the known-tool universe in
#: ``_instructions_name_only_present_tools``. Removing it would make a future base-prose
#: edit that names a harness tool ABSENT from the manifest invisible to the absent-tool
#: invariant -- hole the branch was added to close. An empty set is the
#: correct value for "names nothing"; no set at all is a silent gap.


REFERENCE_ADDENDUM = (
    "Reference doors — resolve a CODE before you pass it; lookups take no engine seat:\n"
    "- merit operand -> lookup_operand. Tolerance code -> lookup_operand with\n"
    "  domain=\"tolerance\"; WITHOUT it the merit catalog answers and is confidently wrong.\n"
    "- glass by NAME -> lookup_glass. Glass by property/intent -> find_glasses or\n"
    "  find_glass_pair, a different door.\n"
    "- manual prose, chapter context -> search_reference.\n"
)


#: The length at which a client SLICES the served ``instructions`` string.
#:
#: MEASURED: Claude Code, live -- the string is cut at exactly this many
#: characters, mid-word, with no notice to either side. This is a property of the
#: CLIENT, not of this repo, and it cannot be read from any API: there is no field in
#: the MCP initialize handshake that reports it. See PROBE-FINDINGS-CONTEXT.md.
#:
#: THE CLIENT VERSION WAS NOT RECORDED AT MEASUREMENT TIME, and is not reconstructed
#: here -- inventing provenance for a measurement is worse than admitting the gap. So
#: this is a measurement with a DATE and no version: a later reader who finds the cap
#: has moved cannot tell whether the client changed or the original reading was wrong.
#: Whoever re-measures should record the version alongside the number.
#:
#: Consequences worth stating where the number lives:
#: * A composed string at or under this length is delivered whole to this client. It
#:   says NOTHING about any other client -- OpenCode delivers the whole string and all
#:   tool descriptions every turn, and an unmeasured client may do either.
#: * Nothing in CI reds when a client CHANGES this behaviour. Periodic re-measurement
#:   is the only honest mitigation and no mechanism here pretends otherwise.
CLIENT_INSTRUCTION_BUDGET_CHARS = 2048


def compose_instructions(manifest_names):
    """THE ONE place the served instruction string is built. Both builders call it.

    Applies, in order:

    1. the base (``HARNESS_INSTRUCTIONS``);
    2. the **addendum subset gate** -- ``REFERENCE_ADDENDUM`` is appended IFF EVERY
       door in ``_ADDENDUM_REFERENCE_DOORS`` is served. A PARTIAL set warns loudly and
       serves the base alone, because naming an absent door is the hazard the gate
       exists to prevent and hiding the route map is the only safe degradation;
    3. the **absent-tool invariant** -- if the composed string names any KNOWN tool
       absent from the manifest, fall back to base-only; if the base ITSELF violates
       (a degraded manifest), excise the offending lines and re-verify; if a violation
       survives excision, raise rather than serve a violating string;
    4. the **budget check** -- log ERROR when the result exceeds
       ``CLIENT_INSTRUCTION_BUDGET_CHARS``.

    **The budget check never refuses to serve.** A string over budget is not wrong, it
    is TRUNCATED BY SOMEONE ELSE, and only for clients that truncate. Refusing to serve
    would convert a degradation on one client into an outage on every client.

    ``manifest_names`` is the served manifest -- the harness-only builder passes the
    harness names (no reference door is present, so the gate appends nothing, which is
    what that builder did when the logic was inline) and the composite builder passes
    the merged names.

    Returns the string to serve. Callers hold no copy of the prose and no copy of the
    budget: the guard measures what THIS function returns, so a hand-synced constant
    cannot drift away from what is served.
    """
    log = logging.getLogger("optivibe_harness.server_mcp")
    names = set(manifest_names)

    doors = frozenset(_ADDENDUM_REFERENCE_DOORS)
    present = doors & names
    instructions = HARNESS_INSTRUCTIONS
    if present == doors:
        instructions = HARNESS_INSTRUCTIONS + REFERENCE_ADDENDUM
    elif present:
        log.warning(
            "PARTIAL reference manifest: serving base instructions only "
            "(addendum would name absent door(s) %s). Present: %s.",
            sorted(doors - present),
            sorted(present),
        )

    absent_named = _instructions_name_only_present_tools(instructions, names)
    if absent_named:
        log.error(
            "ABSENT-TOOL INVARIANT VIOLATED: served instructions name tool(s) %s "
            "absent from the manifest — falling back to base instructions only. "
            "(Likely a stale _ADDENDUM_REFERENCE_DOORS vs REFERENCE_ADDENDUM prose.)",
            absent_named,
        )
        instructions = HARNESS_INSTRUCTIONS
        absent_base = _instructions_name_only_present_tools(instructions, names)
        if absent_base:
            log.error(
                "ABSENT-TOOL INVARIANT: base fallback still names absent tool(s) %s "
                "(degraded manifest) — excising the offending line(s).",
                absent_base,
            )
            instructions = _strip_lines_naming(instructions, absent_base)
            still = _instructions_name_only_present_tools(instructions, names)
            if still:
                raise ValueError(
                    "composed instructions still name absent tool(s) after excision: "
                    f"{still}"
                )

    if len(instructions) > CLIENT_INSTRUCTION_BUDGET_CHARS:
        log.error(
            "SERVED INSTRUCTIONS OVER BUDGET: %d chars > %d — a truncating client "
            "(measured: Claude Code) will slice this mid-word and the tail reaches no "
            "agent. Serving it anyway; a client that does not truncate loses nothing.",
            len(instructions),
            CLIENT_INSTRUCTION_BUDGET_CHARS,
        )

    return instructions


def build_mcp_server(session):
    """Build an MCP ``Server`` exposing the dispatcher's tools (LAZY mcp import).

    Imports ``mcp`` HERE (never at module top). Mirrors
    ``Dispatcher.list_tools()`` into MCP tool descriptors and routes each
    ``call_tool`` to ``Dispatcher.dispatch``, returning its envelope.
    """
    import mcp.types as mcp_types  # noqa: E402 — lazy: mcp may be absent at import
    from mcp.server import Server  # noqa: E402

    dispatcher = Dispatcher(session)

    # The subset gate, the absent-tool invariant and the client budget check all live in
    # compose_instructions. This builder serves the HARNESS-ONLY
    # manifest, in which no reference door is present, so the gate appends no addendum --
    # the same string this block produced when the logic was inline here.
    _names = {e["name"] for e in dispatcher.list_tools()}
    _instructions = compose_instructions(_names)

    server = Server("optivibe-harness", instructions=_instructions)

    # Per-tool set of params declared "string" — those must NOT be
    # JSON-reparsed (a string value must stay a string). A tool with no
    # param_types -> empty set -> full reparse (legacy path; today only zero-parameter
    # harness tools, whose legacy schema is the empty object -- every reference spec
    # carries param_types since 868a8b8).
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
                    description=served_description(
                        entry["name"], entry["description"], _names),
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

    # The subset gate, the absent-tool invariant and the client budget check all live in
    # compose_instructions. Passing the MERGED manifest is what makes
    # the addendum eligible: the gate appends it iff every reference door it names is
    # served, and falls back to base-only on a partial or degraded set.
    _instructions = compose_instructions(_names)

    server = Server("optivibe", instructions=_instructions)

    # Per-tool set of params declared "string" — see build_mcp_server.
    # Same rule as build_mcp_server: an entry with no param_types -> empty set -> full
    # legacy reparse. Reference entries DO carry param_types (868a8b8).
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
                    description=served_description(
                        entry["name"], entry["description"], _names),
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
