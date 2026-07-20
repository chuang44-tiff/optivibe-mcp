"""tools/_merit_math.py — the pure intent->math composer helpers (§4).

NOT dispatchable (no ``TOOL_SPECS``). The analog of the
``_merit_cells`` / ``_merit_io`` split: the pure, total, engine-free logic the single
``add_math_constraint`` composer (``merit_math.py``) funnels through lives here:

- ``_cross_check(relationship, sign_convention) -> (status, reason)`` — the EXACT
  §3 relationship × ``sign_convention`` grid. A COARSE cross-FAMILY category gate
  (§3 limit): it catches a measurement/derived operand asked to be a hard bound, a
  bound asked to be a derived value, an OPGT-as-OPLT direction flip, a named-value vs
  bound confusion. It CANNOT separate WITHIN the ``measurement`` family (EQUA vs DIFF
  vs MAXX vs … are all ``measurement``) — the within-family pick rests on the LLM
  reading descriptions + the L5 gate, NOT this guard. Pure, total, every cell.
- ``_resolve_handles(operands) -> (refs_by_index, errors)`` — the stable-handle
  row-reference bookkeeping (§5). Walks the operands list IN AUTHOR ORDER, builds
  ``label -> 0-based-position``, and maps each spec's ``refs:[la, lb]`` ->
  ``{Op#1: pos(la), Op#2: pos(lb)}`` (unary ``[la]`` -> ``{Op#: pos(la)}``) — the
  positional ``Op#``/``Op#1``/``Op#2`` Header convention OWNED by the tool, never the
  agent (§5.1). A label referenced before defined / a dangling label -> an error (the
  ``merit_math_unresolved`` family). The ONLY supported ref form is an in-call LABEL
  string; the append-mode ``{existing_row: n}`` raw-row form was CUT this cycle (§5.2 —
  ``apply_merit_recipe`` reads every refs value as a 0-based recipe INDEX, with no
  raw-live-row encoding; a raw row would silently remap to the WRONG operand, MM-4).
- ``_constraint_to_recipe(operands, refs_by_index) -> recipe_dict`` — assemble the
  ``optivibe.merit-recipe`` dict (type=code, params=non-ref cells, refs=the resolved
  index map, target, weight per operand) that ``apply_merit_recipe`` consumes verbatim
  (§4 — "everything funnels into apply_merit_recipe").

The ``Op#``-by-position convention (§5.1): a unary ref list ``[la]`` writes the single
``Op#`` cell; a binary list ``[la, lb]`` writes ``Op#1``/``Op#2``; an N-ary list writes
``Op#1``..``Op#N``. ``apply_merit_recipe``'s ``_validate_refs`` then checks each Header
exists on the operand's LIVE signature and each index is in range, so a too-long /
wrong-Header ref list is REJECTED by the EXISTING Phase-A guard (§8.4 — the
tool inherits the recipe guards, it does not bypass them).

Provenance (§10): ZOS-API / ``MeritOperandType`` vocabulary only — the ``Op#`` cell
Headers + the ``sign_convention`` strings the reference already ships. NO phrase->code
map: every helper here takes a CODE + an agent-STATED relationship + the agent-READ
``sign_convention``, never an intent phrase (§0 — the harness physically cannot reach
the reference DB; grounding is the agent's job).
"""

# --------------------------------------------------------------------------- #
# §3 — the relationship × sign_convention cross-check grid.
# --------------------------------------------------------------------------- #
# The five relationships the agent STATES (§4 signature). A relationship
# NOT in this set is a ``merit_math_unresolved`` (validated by the handler, not here).
_RELATIONSHIPS = ("ge", "le", "eq", "derived", "minimize")

# The five ``sign_convention`` strings the reference ships on a candidate (§3 grid),
# PLUS ``None`` (the ``null``/unknown convention -> REFUSE every relationship). The
# string keys are operand-AGNOSTIC (a new operand inherits correct behavior; only a
# NEW convention string would need a grid row — caught by any × unknown -> REFUSED).
_SIGN_CONVENTIONS = ("boundary_ge", "boundary_le", "equality", "measurement",
                     "minimize", None)

# The EXACT §3 grid (§3 "the cross-check grid"). status is one
# of ``"ok"`` / ``"warn"`` / ``"refused"``. Keyed (relationship, sign_convention).
# A ``None`` sign_convention is the ``null`` column — REFUSED for every relationship.
#
# Read the grid against §3 directly:
#   ge      : ok ONLY on boundary_ge; everything else REFUSED (incl. the direction
#             flip ge×boundary_le and the silent-wrong ge×measurement DIFF-as-bound).
#   le      : ok ONLY on boundary_le; symmetric.
#   eq      : ok on equality (the *VA named-value) AND measurement (target-via-Target);
#             warn on boundary_ge/boundary_le (pinning a GT/LT with Target+Weight is
#             unusual-but-possible -> confirm_crosscheck); REFUSED on minimize/null.
#   derived : ok ONLY on measurement (a derived A=f(B,C) is a measurement); REFUSED on
#             every bound/named-value/minimize convention.
#   minimize: ok on minimize; warn on measurement (drive-to-0 on a measurement is
#             common, e.g. DIST->0); REFUSED otherwise.
_GRID = {
    ("ge", "boundary_ge"): ("ok", "relationship 'ge' matches the operand's "
                            "boundary_ge (>=) convention"),
    ("ge", "boundary_le"): ("refused", "relationship 'ge' (>=) on a boundary_le (<=) "
                            "operand is a direction flip; pick a >= bound operand "
                            "(e.g. an OPGT-family code)"),
    ("ge", "equality"): ("refused", "relationship 'ge' on an equality (named-value) "
                         "operand has no one-sided-bound semantics; add a separate "
                         ">= bound operand referencing it"),
    ("ge", "measurement"): ("refused", "relationship 'ge' on a measurement/derived "
                            "operand has no one-sided-bound semantics (the "
                            "DIFF-as-a-hard->= silent-wrong class); add a separate "
                            ">= bound operand referencing this measurement"),
    ("ge", "minimize"): ("refused", "relationship 'ge' on a minimize operand is a "
                         "category error; a minimize operand drives toward 0"),
    ("ge", None): ("refused", "the operand's sign_convention is null/unknown; refuse "
                   "rather than guess a >= bound — qualify the operand"),

    ("le", "boundary_ge"): ("refused", "relationship 'le' (<=) on a boundary_ge (>=) "
                            "operand is a direction flip; pick a <= bound operand "
                            "(e.g. an OPLT-family code)"),
    ("le", "boundary_le"): ("ok", "relationship 'le' matches the operand's "
                            "boundary_le (<=) convention"),
    ("le", "equality"): ("refused", "relationship 'le' on an equality (named-value) "
                         "operand has no one-sided-bound semantics; add a separate "
                         "<= bound operand referencing it"),
    ("le", "measurement"): ("refused", "relationship 'le' on a measurement/derived "
                            "operand has no one-sided-bound semantics; add a separate "
                            "<= bound operand referencing this measurement"),
    ("le", "minimize"): ("refused", "relationship 'le' on a minimize operand is a "
                         "category error; a minimize operand drives toward 0"),
    ("le", None): ("refused", "the operand's sign_convention is null/unknown; refuse "
                   "rather than guess a <= bound — qualify the operand"),

    ("eq", "boundary_ge"): ("warn", "relationship 'eq' on a boundary_ge operand pins "
                            "a >= bound with Target+Weight; unusual but possible — "
                            "confirm_crosscheck required"),
    ("eq", "boundary_le"): ("warn", "relationship 'eq' on a boundary_le operand pins "
                            "a <= bound with Target+Weight; unusual but possible — "
                            "confirm_crosscheck required"),
    ("eq", "equality"): ("ok", "relationship 'eq' matches the operand's equality "
                         "(named-value = value) convention"),
    ("eq", "measurement"): ("ok", "relationship 'eq' targets a measurement via its "
                            "Target (the deviation is minimized) — the legitimate "
                            "equality-via-Target path"),
    ("eq", "minimize"): ("refused", "relationship 'eq' on a minimize operand is a "
                         "category error; a minimize operand drives toward 0, it does "
                         "not target a value"),
    ("eq", None): ("refused", "the operand's sign_convention is null/unknown; refuse "
                   "rather than guess an equality — qualify the operand"),

    ("derived", "boundary_ge"): ("refused", "relationship 'derived' on a boundary_ge "
                                 "operand is a category error; a derived A=f(B,C) is "
                                 "never a one-sided bound"),
    ("derived", "boundary_le"): ("refused", "relationship 'derived' on a boundary_le "
                                 "operand is a category error; a derived A=f(B,C) is "
                                 "never a one-sided bound"),
    ("derived", "equality"): ("refused", "relationship 'derived' on an equality "
                              "(named-value) operand is a category error; a derived "
                              "A=f(B,C) is never a named-value operand"),
    ("derived", "measurement"): ("ok", "relationship 'derived' matches a measurement "
                                 "operand (DIFF/SUMM/PROD/DIVI/MAXX/MINN are all "
                                 "measurement arithmetic — the derived family)"),
    ("derived", "minimize"): ("refused", "relationship 'derived' on a minimize "
                              "operand is a category error"),
    ("derived", None): ("refused", "the operand's sign_convention is null/unknown; "
                        "refuse rather than guess a derived value — qualify the "
                        "operand"),

    ("minimize", "boundary_ge"): ("refused", "relationship 'minimize' on a "
                                  "boundary_ge operand is a category error"),
    ("minimize", "boundary_le"): ("refused", "relationship 'minimize' on a "
                                  "boundary_le operand is a category error"),
    ("minimize", "equality"): ("refused", "relationship 'minimize' on an equality "
                               "operand is a category error"),
    ("minimize", "measurement"): ("warn", "relationship 'minimize' on a measurement "
                                  "operand (drive-to-0, e.g. DIST->0) is common but "
                                  "not the operand's stated convention — "
                                  "confirm_crosscheck required"),
    ("minimize", "minimize"): ("ok", "relationship 'minimize' matches the operand's "
                               "minimize (drive toward 0) convention"),
    ("minimize", None): ("refused", "the operand's sign_convention is null/unknown; "
                         "refuse rather than guess a minimize — qualify the operand"),
}


def _normalize_sign_convention(sign_convention):
    """Normalize an agent-supplied ``sign_convention`` to a grid key (§3).

    The ``null`` column of the §3 grid is keyed on Python ``None``. An agent may pass
    the literal Python ``None``, the JSON ``null`` (also ``None`` over the wire), the
    string ``"null"``, or an empty string — all collapse to the ``None`` grid key (an
    unknown convention -> REFUSED). Any other string is returned verbatim (an
    UNRECOGNIZED non-null string then misses the grid -> ``_cross_check`` REFUSES it
    as an unknown convention, §3 ``any × null -> REFUSED`` posture extended to any
    convention the grid does not know — drift-safe, Risk 5).
    """
    if sign_convention is None:
        return None
    text = str(sign_convention).strip()
    if text == "" or text.lower() == "null":
        return None
    return text


def _cross_check(relationship, sign_convention):
    """The §3 relationship × ``sign_convention`` cross-check. Pure, total.

    Returns ``(status, reason)`` with ``status`` in ``{"ok", "warn", "refused"}``:

    - ``relationship`` not one of the five §4 strings -> ``("refused", <reason>)`` (a
      bad relationship is structurally a refusal here; the HANDLER additionally maps a
      bad relationship to ``merit_math_unresolved`` BEFORE reaching the grid, §7);
    - a ``None``/``"null"``/unknown ``sign_convention`` -> ``("refused", <reason>)``
      (the §3 ``any × null -> REFUSED`` column, extended to any convention the grid
      does not know — Risk 5 drift-safety);
    - otherwise the EXACT §3 grid cell.

    This is the §3 COARSE cross-FAMILY gate (§3 honest-limit): it catches a
    measurement/derived operand asked to be a hard bound, a bound asked to be a
    derived value, a direction flip, a named-value vs bound confusion. It CANNOT
    separate WITHIN the ``measurement`` family — that pick rests on the LLM reading
    descriptions + the §8.5 L5 gate, NOT this guard (Risk 1).
    """
    if relationship not in _RELATIONSHIPS:
        return ("refused",
                f"relationship {relationship!r} is not one of {list(_RELATIONSHIPS)}")
    key = (relationship, _normalize_sign_convention(sign_convention))
    cell = _GRID.get(key)
    if cell is None:
        # A non-null sign_convention string the grid does not know (a future/unknown
        # convention) — refuse rather than silently pass (Risk 5 drift-safety).
        return ("refused",
                f"sign_convention {sign_convention!r} is not a recognized convention "
                f"for relationship {relationship!r}; refuse rather than guess")
    return cell


# --------------------------------------------------------------------------- #
# §5 — the stable-handle row-reference bookkeeping.
# --------------------------------------------------------------------------- #
def _op_ref_headers(n_refs):
    """The positional ``Op#`` Header sequence for ``n_refs`` references (§5.1).

    The convention OWNED by the tool (not the agent): a UNARY ref list (``n_refs == 1``)
    writes the single ``Op#`` cell; a BINARY/N-ary list writes ``Op#1``/``Op#2``/...
    /``Op#N`` BY POSITION. ``apply_merit_recipe``'s ``_validate_refs`` then checks each
    Header against the operand's LIVE signature, so a too-long / wrong-shape ref list
    is REJECTED downstream by the EXISTING Phase-A guard (§8.4). Returns the Header
    list in ref order.
    """
    if n_refs == 1:
        return ["Op#"]
    return [f"Op#{i}" for i in range(1, n_refs + 1)]


def _resolve_handles(operands):
    """Resolve every operand-spec's ``refs`` labels -> a 0-based recipe-index map (§5).

    Walks ``operands`` IN AUTHOR ORDER (the position IS the recipe index — the same
    append-position discipline ``serialize_merit`` uses), building
    ``label -> 0-based-position`` as it goes. For each spec, maps its ordered
    ``refs:[la, lb, ...]`` -> ``{Op#1: pos(la), Op#2: pos(lb), ...}`` (unary ``[la]``
    -> ``{Op#: pos(la)}``) via ``_op_ref_headers`` (§5.1). The ONLY supported ref form
    is an in-call LABEL string; the append-mode ``{existing_row: n}`` raw-row form was
    CUT this cycle (§5.2) — a non-label entry is rejected ``merit_math_unresolved``.

    Returns ``(refs_by_index, errors)``:

    - ``refs_by_index`` — ``{operand_index: {Op#Header: 0-based recipe index}}`` for every
      operand that carries refs (an operand with no refs gets NO entry — it stays
      refs-free, byte-identical to a non-math recipe entry);
    - ``errors`` — a list of ``{index, error}`` for every resolution failure (a label
      referenced before defined / a dangling label / a malformed refs shape). ALL
      errors are COLLECTED (no fail-fast) so the handler reports every problem in one
      ``merit_math_unresolved`` envelope. A non-empty ``errors`` -> the caller MUST NOT
      mutate (the §8.4 "no engine touch" on a label error).

    LABEL discipline (§5.1): a label is defined when ITS operand is walked, so a ref
    can only resolve to an EARLIER operand in this call (a forward/self label is
    "referenced before defined" -> an error). A ``label`` collision (two operands
    sharing a label) keeps the FIRST binding and records an error on the duplicate (a
    later ref would be ambiguous).
    """
    label_to_index = {}
    refs_by_index = {}
    errors = []

    for index, spec in enumerate(operands):
        # The label of THIS operand becomes referenceable by LATER operands only AFTER
        # this operand's own refs are resolved (so a self-reference is "before defined").
        spec_refs = spec.get("refs")

        if spec_refs is not None:
            resolved_refs, ref_errs = _resolve_one_refs(
                spec_refs, index, label_to_index
            )
            for msg in ref_errs:
                errors.append({"index": index, "error": msg})
            if resolved_refs:
                refs_by_index[index] = resolved_refs

        # Register this operand's label LAST (after its refs resolved) so a label can
        # only point BACKWARD (§5.1 — a forward/self ref is "before defined").
        label = spec.get("label")
        if label is not None:
            if not isinstance(label, str) or label == "":
                errors.append({"index": index,
                               "error": f"label must be a non-empty string, got "
                                        f"{label!r}"})
            elif label in label_to_index:
                errors.append({"index": index,
                               "error": f"duplicate label {label!r} (already bound to "
                                        f"operand index {label_to_index[label]}); a "
                                        "label must be unique within the call"})
            else:
                label_to_index[label] = index

    return refs_by_index, errors


def _resolve_one_refs(spec_refs, index, label_to_index):
    """Resolve ONE operand-spec's ``refs`` list -> ``{Op#Header: index_or_row}`` (§5).

    ``spec_refs`` is the ordered ``refs:[...]`` of the operand at ``index``. Each entry
    MUST be a ``label`` string (resolved to its 0-based recipe index via
    ``label_to_index`` — a label not yet defined / unknown is a "referenced before
    defined" error, §5.1). The append-mode ``{existing_row: n}`` raw-row form was CUT
    this cycle (§5.2) — any non-label entry (a dict, an int, ...) is rejected with a
    ``merit_math_unresolved`` message steering to in-call labels or mode=replace. The
    entries are mapped to ``Op#``/``Op#1``/``Op#2``/... BY POSITION (``_op_ref_headers``).

    Returns ``(resolved_refs, errors)`` — ``resolved_refs`` is the
    ``{Op#Header: 0-based recipe index}`` map (empty when the refs list is empty or every
    entry errored); ``errors`` is the list of error message strings (collected, no
    fail-fast).
    """
    errors = []
    if not isinstance(spec_refs, list):
        return {}, [f"refs must be a list of in-call label strings, "
                    f"got {type(spec_refs).__name__}"]
    if not spec_refs:
        return {}, []  # an empty refs list -> no refs (handled like absent refs).

    headers = _op_ref_headers(len(spec_refs))
    resolved = {}
    for header, entry in zip(headers, spec_refs):
        if isinstance(entry, str):
            target = label_to_index.get(entry)
            if target is None:
                errors.append(
                    f"refs entry {entry!r} (-> {header}) does not resolve to an "
                    "operand defined EARLIER in this call (a label must reference a "
                    "prior operand; forward/self/unknown labels are rejected)"
                )
                continue
            resolved[header] = target
        else:
            # The ONLY supported ref form is an IN-CALL LABEL string (resolved to a
            # 0-based recipe index above). The append-mode ``{existing_row: n}`` raw-row
            # form was CUT (§5.2, this cycle): ``apply_merit_recipe`` treats
            # EVERY refs value as a 0-based recipe INDEX (0..n_operands-1), with no
            # raw-live-row encoding — so a raw row was either falsely rejected (small
            # recipe) or SILENTLY remapped to the WRONG operand (>=4-operand recipe), the
            # MM-4 hazard. No raw live row may enter the recipe refs map. Author the
            # referenced operand in THIS call and reference it by label, or use
            # mode="replace" for a fresh all-label math block (a future ticket can add a
            # properly-encoded raw-row append if a real need arises).
            errors.append(
                f"refs entry {entry!r} (-> {header}) must be an in-call label string. "
                "An existing_row / pre-existing-live-operand reference is not supported; "
                "author the referenced operand in THIS call and reference it by label, "
                "or use mode=replace for a fresh math block"
            )
    return resolved, errors


# --------------------------------------------------------------------------- #
# §4 — assemble the optivibe.merit-recipe dict apply_merit_recipe consumes.
# --------------------------------------------------------------------------- #
_RECIPE_SCHEMA = "optivibe.merit-recipe"
_RECIPE_VERSION = 1


def _constraint_to_recipe(operands, refs_by_index):
    """Assemble the ``optivibe.merit-recipe`` dict from the resolved constraint (§4).

    Builds one recipe entry per operand-spec, IN AUTHOR ORDER (so the recipe index ==
    the operand position the labels were resolved against, §5.1):

    - ``type``   = the spec's ``code`` (an ALREADY-RESOLVED MeritOperandType mnemonic,
      validated vs the live enum by the handler BEFORE this call — §4/§0);
    - ``params`` = the spec's NON-ref param cells (``Wave``/``Surf``/``Surf1``/... —
      passed straight to ``apply_params`` by the recipe author, §4);
    - ``refs``   = the resolved ``{Op#Header: 0-based-recipe-index}`` from
      ``refs_by_index`` (added ONLY when this operand carries refs — a refs-free
      operand stays byte-identical to a non-math recipe entry);
    - ``target`` / ``weight`` = the spec's values (defaults 0.0 / 1.0, §4).

    The assembled recipe is consumed VERBATIM by ``apply_merit_recipe`` (its Phase-1
    ``_validate_refs`` re-checks every ``Op#`` Header vs the live signature + every
    index range, and its two-phase author-then-wire remap translates each recipe index
    -> the referenced operand's NEW live row, MM-4). NO new author/atomic/rollback
    machinery (§4 — "everything funnels into apply_merit_recipe"). Pure;
    zero engine touch.
    """
    recipe_operands = []
    for index, spec in enumerate(operands):
        entry = {"type": spec["code"]}

        params = spec.get("params")
        if params is not None:
            entry["params"] = params

        if index in refs_by_index:
            entry["refs"] = refs_by_index[index]

        entry["target"] = spec.get("target", 0.0)
        entry["weight"] = spec.get("weight", 1.0)
        recipe_operands.append(entry)

    return {
        "schema": _RECIPE_SCHEMA,
        "version": _RECIPE_VERSION,
        "operands": recipe_operands,
    }


__all__ = [
    "_cross_check",
    "_resolve_handles",
    "_constraint_to_recipe",
    "_op_ref_headers",
    "_RELATIONSHIPS",
    "_SIGN_CONVENTIONS",
]
