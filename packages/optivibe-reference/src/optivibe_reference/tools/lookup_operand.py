"""tools/lookup_operand.py — intent->MeritOperandType grounding (session-free).

The typed, DB-backed, never-raise tool the merit-builder intent layer consumes.
Handler signature ``lookup_operand(db_conn, params)`` — arg-0 is the catalog DB
connection the ref ``Dispatcher`` threads positionally (the FORWARD analog of the
harness session, PIN 4). It answers from THAT connection, never a module-global.

Routing predicate (§5 / HOOK 3): a ``query`` that IS a known case-sensitive code
(``'EFFL'``) resolves on the keyed ``operand`` table with NO RAG fallthrough;
``'effl'`` lowercase does NOT resolve exact and falls to FTS5 RAG. A phrase ->
FTS5 RAG (top-N ranked by bm25).

Failure discipline (never raises past its boundary for expected failures):
- missing ``query`` -> raise ``ToolParamError`` (param-error, classified by
  dispatch, NOT internal — the required_params presence check normally catches
  it first; the direct-call guard is belt-and-suspenders);
- a candidate cap (``limit`` OR its alias ``top_n``) that is a ``bool`` -> raise
  ``ToolParamError`` BEFORE int coercion (``True == 1`` would otherwise slip
  through — isinstance(bool) check FIRST), as does a non-int or a value ``< 1``;
  giving BOTH ``limit`` and ``top_n`` is a ``ToolParamError``;
- exact miss / RAG no hits -> ``operand_unknown`` envelope;
- a NUL/control char or an oversized (>4096-char) query -> ``query_malformed``
  envelope, sourced from the shared ``assert_query_well_formed`` guard (NUL/control/
  oversize) run BEFORE any MATCH (a NUL would otherwise raise
  ``sqlite3.ProgrammingError``, a non-OperationalError sibling that escapes the
  backstop into ``internal``); a retained ``sqlite3.OperationalError`` backstop
  around the MATCH catches any residual FTS5 error and also maps to
  ``query_malformed``, never ``internal``;
- a no-single-operand intent -> ``{resolved:False, reason:'no_single_operand'}``
  honesty marker, never a fabricated best-fuzzy-match.

NOTE on FTS metacharacters: each query term is wrapped as an FTS5 LITERAL phrase
(double-quoted via ``_rag_match.and_expr``/``or_expr``, consistent with
``search_reference``), so a metacharacter is searched as a literal word and loses
its special FTS grammar meaning — it is NOT a syntax error and does NOT produce a
``query_malformed`` envelope on its own.

RAG is a TWO-PASS AND-then-OR (Class-A stopword-zeroing fix, §1):
- PASS 1 — implicit-AND of literal-phrase terms (the prior behavior). If it
  returns >=1 row, those rows win; ``match_mode='and'``. Identical ranking to
  before, so every intent that already resolved is structurally unchanged.
- PASS 2 — OR-fallback, ONLY when pass 1 returned ZERO rows. OR-joins the same
  literal-phrase terms; bm25 re-ranks natively; ``match_mode='or'`` is surfaced on
  the result. May still be empty -> the ``operand_unknown`` path. Strictly
  additive: it never mutates the token SET and never fires when AND already matched.
"""
import json
import sqlite3

from .._envelope import error_envelope
from ..errors import ToolParamError
from ..server import ToolSpec
from ._query_params import effective_n as _resolve_effective_n
from ._rag_match import and_expr, assert_query_well_formed, or_expr

# The ranked-candidate cap for the RAG path (§5: a defined/tested N). It is BOTH
# the default cap and the hard ceiling: a caller-supplied ``limit``/``top_n`` is
# applied as ``min(override, TOP_N)``. ``truncated`` is True only when MORE rows
# matched than were returned (over-fetch-by-one), NOT merely when the count == N.
TOP_N = 10

_TOOL = "lookup_operand"

# Single source of truth for the operand-row columns selected on an exact hit
# (L-1): both ``_columns()`` (the SELECT list) and ``_row_to_dict`` (the keys it
# zips against) derive from THIS tuple, so the SELECT order and the dict keys can
# never drift. Order is load-bearing — the SELECT and the zip MUST agree.
OPERAND_COLUMNS = (
    "code",
    "description",
    "description_source",
    "description_pending",
    "citation_handle",
    "units",
    "units_source",
    "sign_convention",
    "sign_convention_source",
    "cell_layout",
    "row_type_name",
    "optic_studio_version",
    "schema_version",
    # Tolerance-safety columns (trailing, additive). NULL on every merit
    # row; real closed-enum values only on the separate tolerance_operand catalog.
    "category",
    "precondition_class",
    "run_verdict",
)

# Intents that have NO single typed-operand answer — a phrase the agent must NOT
# be handed a fabricated best-fuzzy-match for. Kept as a small, explicit,
# TESTED set for this increment (§5: keep it simple but tested). When the top
# bm25 match for one of these is not meaningfully better than the runner-up AND
# there is more than one candidate, the honesty marker fires.
_KNOWN_NO_SINGLE_OPERAND_INTENTS = frozenset(
    {
        "image quality",
        "good performance",
        "make it better",
    }
)

# How much better (more negative bm25 == better) the top hit must be than the
# runner-up to count as a "meaningfully better" single answer. bm25 returns
# negative scores; a smaller (more negative) score is a better match.
_DECISIVE_BM25_MARGIN = 0.5


# The cap-override helpers (``validate_cap``/``effective_n``/``CAP_PARAM_ALIASES``)
# are now lifted into ``tools/_query_params.py`` and SHARED with
# ``search_reference`` (PIN 4) — one source of truth for the bool-before-int trap.
# ``_resolve_effective_n(params, TOP_N)`` is the imported ``effective_n`` (aliased
# to avoid shadowing the ``effective_n`` int local in ``lookup_operand`` below).


def _columns():
    """The operand-row column SELECT list for an exact hit (stable order).

    Derived from ``OPERAND_COLUMNS`` (single source of truth, L-1) — no f-stringed
    literal that could drift from the dict keys in ``_row_to_dict``.
    """
    return ", ".join(OPERAND_COLUMNS)


def _row_to_dict(row):
    """Map a selected ``operand`` row tuple to a name-keyed dict.

    Keyed by ``OPERAND_COLUMNS`` (the SAME tuple the SELECT list derives from),
    so the column order and the dict keys can never drift apart (L-1).
    """
    return dict(zip(OPERAND_COLUMNS, row))


def _param_cells(cell_layout_json):
    """Parse the stored ``cell_layout`` JSON to a list (``[]`` when NULL)."""
    if not cell_layout_json:
        return []
    try:
        parsed = json.loads(cell_layout_json)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _is_exact_code(db_conn, query):
    """True iff ``query`` is a case-sensitive code present in the keyed table.

    Uses a case-sensitive equality (the column compares case-sensitively for the
    ASCII codes) so ``'EFFL'`` matches and ``'effl'`` does NOT. Returns the
    fetched row tuple (or None).
    """
    row = db_conn.execute(
        f"SELECT {_columns()} FROM operand WHERE code = ?", (query,)
    ).fetchone()
    if row is None:
        return None
    # Defensive case-sensitivity: SQLite ``=`` on a TEXT column is binary
    # (case-sensitive) by default, but assert the exact byte match so a future
    # COLLATE NOCASE on the column can never silently route 'effl' to exact.
    if row[0] != query:
        return None
    return row


def intent_has_no_single_operand(query, hits):
    """Decide whether ``query`` has no single-operand answer (honesty marker).

    Simple, TESTED rule (§5): fires when ``query`` (case-folded, stripped) is in
    the known ambiguous-intent set AND there is more than one candidate AND the
    top bm25 score is not meaningfully better than the runner-up's. ``hits`` is
    the ``[(code, bm25_score), ...]`` list ordered best-first.

    Returns ``True`` when the marker should fire. Mutating the rule (e.g.
    dropping the margin check, or the membership check) flips a paired test red.
    """
    normalized = (query or "").strip().lower()
    if normalized not in _KNOWN_NO_SINGLE_OPERAND_INTENTS:
        return False
    if len(hits) <= 1:
        return False
    top_score = hits[0][1]
    runner_up_score = hits[1][1]
    # bm25: more negative == better. The top is "meaningfully better" only if it
    # beats the runner-up by more than the margin; otherwise the answer is
    # genuinely ambiguous and the honesty marker fires.
    if (runner_up_score - top_score) > _DECISIVE_BM25_MARGIN:
        return False
    return True


def _exact_payload(row):
    """Build the success payload for an exact keyed-table hit."""
    d = _row_to_dict(row)
    param_cells = _param_cells(d["cell_layout"])
    if d["description"] is None:
        # KNOWN-BUT-UNENRICHED: distinct from the '' empty-string bug (§5). This
        # branch returns the SAME key set as the enriched branch below (H-1: no
        # shape drift) — description_source/units/sign_convention are carried with
        # their pending values ('authored'/None this increment), NOT omitted.
        return {
            "ok": True,
            "code": d["code"],
            "description": None,
            "description_pending": True,
            "description_source": d["description_source"],
            "param_cells": param_cells,
            "units": d["units"],
            "units_source": d["units_source"],
            "sign_convention": d["sign_convention"],
            "sign_convention_source": d["sign_convention_source"],
            # Tolerance-safety fields (NULL for merit rows, real for
            # the tolerance domain). Carried on BOTH branches so the key set never
            # drifts (H-1).
            "category": d["category"],
            "precondition_class": d["precondition_class"],
            "run_verdict": d["run_verdict"],
            "match_kind": "exact",
        }
    return {
        "ok": True,
        "code": d["code"],
        "description": d["description"],
        "description_pending": False,
        "description_source": d["description_source"],
        "param_cells": param_cells,
        "units": d["units"],
        "units_source": d["units_source"],
        "sign_convention": d["sign_convention"],
        "sign_convention_source": d["sign_convention_source"],
        "category": d["category"],
        "precondition_class": d["precondition_class"],
        "run_verdict": d["run_verdict"],
        "match_kind": "exact",
    }


def _rag_candidate(db_conn, code, score):
    """Build one RAG candidate dict (code + enrichment + bm25 score)."""
    row = db_conn.execute(
        f"SELECT {_columns()} FROM operand WHERE code = ?", (code,)
    ).fetchone()
    d = _row_to_dict(row) if row is not None else {"code": code}
    return {
        "code": d.get("code", code),
        "description": d.get("description"),
        "description_source": d.get("description_source"),
        "param_cells": _param_cells(d.get("cell_layout")),
        # Semantics §6: the merit-builder ranks RAG candidates by direction + units, so
        # surface them (+ their sources) alongside the description on every candidate.
        "units": d.get("units"),
        "units_source": d.get("units_source"),
        "sign_convention": d.get("sign_convention"),
        "sign_convention_source": d.get("sign_convention_source"),
        # Tolerance-safety fields on every candidate (NULL for merit).
        "category": d.get("category"),
        "precondition_class": d.get("precondition_class"),
        "run_verdict": d.get("run_verdict"),
        "score": score,
    }


def lookup_operand(db_conn, params):
    """Resolve design intent onto a typed operand (never raises).

    Resolves on whatever catalog connection is threaded as ``db_conn`` (arg-0) — the
    merit ``operand`` catalog by default, or the tolerance ``ToleranceOperandType``
    catalog when the ``Dispatcher`` selected it from ``domain="tolerance"``.

    arg-0 IS AUTHORITATIVE: this handler does NOT read ``params["domain"]`` and does
    NOT stamp a ``domain`` label on any payload (the Dispatcher already resolved the
    domain at the single validation point and is the SOLE author of the label - the
    registry key that selected arg-0, so conn and label agree by construction). A
    direct-bypass call therefore returns NO ``domain`` key: the handler is
    catalog-agnostic and must not claim a domain it cannot know.

    See module docstring for the routing predicate and failure discipline.
    """
    # --- param validation (raises -> dispatch classifies as tool_param) ---
    if "query" not in params:
        raise ToolParamError("missing required param 'query'")
    query = params["query"]
    if not isinstance(query, str):
        raise ToolParamError(f"'query' must be a string; got {type(query).__name__}")

    # --- candidate-cap override: 'limit' and 'top_n' are ALIASES ---------------
    # Both name the optional caller cap on the RAG candidate list. Supplying BOTH
    # is contradictory -> ToolParamError. Each is validated identically: reject a
    # bool BEFORE int coercion (True == 1 / False == 0 trap — isinstance(bool)
    # FIRST), reject a non-int, reject < 1 (so negative AND zero both raise). The
    # validated override is APPLIED below (capped at TOP_N), not silently ignored.
    effective_n = _resolve_effective_n(params, TOP_N)

    # --- exact path: case-sensitive known code -> keyed table, NO RAG ---
    exact_row = _is_exact_code(db_conn, query)
    if exact_row is not None:
        return _exact_payload(exact_row)

    # An empty/blank query can never be an exact code and is meaningless for RAG;
    # treat it as unknown rather than handing it to MATCH (which would also error).
    if not query.strip():
        return error_envelope(
            _TOOL, "operand_unknown", "empty query", query=query
        )

    # Never-raise input guard (BEFORE any MATCH so the handler owns the failure
    # family). A NUL/control char would survive the split()/join() inside the
    # MATCH builders and make execute() raise sqlite3.ProgrammingError (a
    # DatabaseError sibling NOT caught by the OperationalError backstop ->
    # misclassified 'internal'); an oversized query would raise MemoryError in
    # split()/join(). Both pre-empt as query_malformed via the shared guard.
    malformed_reason = assert_query_well_formed(query)
    if malformed_reason is not None:
        return error_envelope(
            _TOOL, "query_malformed", malformed_reason, query=query
        )

    # --- RAG path: TWO-PASS AND-then-OR, parameterized FTS5 MATCH (NEVER ---
    # f-string), bm25-ranked. Over-fetch effective_n + 1 per pass so ``truncated``
    # is correct AT THE BOUNDARY (N-3): exactly effective_n matches must report
    # truncated=False, not True. Both passes share the SAME OperationalError
    # backstop. PASS 2 (OR) runs ONLY when PASS 1 (AND) returns zero rows.
    sql = (
        "SELECT code, bm25(operand_fts) AS score FROM operand_fts "
        "WHERE operand_fts MATCH ? ORDER BY score LIMIT ?"
    )
    try:
        rows = db_conn.execute(sql, (and_expr(query), effective_n + 1)).fetchall()
        match_mode = "and"
        if not rows:
            # PASS 2 — OR-fallback: only when the implicit-AND pass zeroed.
            rows = db_conn.execute(
                sql, (or_expr(query), effective_n + 1)
            ).fetchall()
            match_mode = "or"
    except sqlite3.OperationalError as exc:
        # FTS5 metacharacter syntax error -> PRE-EMPT as query_malformed; never
        # let it fall through to 'internal'.
        return error_envelope(
            _TOOL, "query_malformed", str(exc), query=query
        )

    if not rows:
        return error_envelope(
            _TOOL, "operand_unknown", "no operand matched the query", query=query
        )

    # We over-fetched by one (on whichever pass returned rows): more than
    # effective_n rows means there were further matches we did NOT return ->
    # truncated. Then trim to the effective_n cap.
    truncated = len(rows) > effective_n
    hits = rows[:effective_n]

    if intent_has_no_single_operand(query, hits):
        candidates = [_rag_candidate(db_conn, code, score) for code, score in hits]
        return {
            "ok": True,
            "resolved": False,
            "reason": "no_single_operand",
            "candidates": candidates,
            "candidate_count": len(candidates),
            "truncated": truncated,
            "match_kind": "rag",
            "match_mode": match_mode,
        }

    candidates = [_rag_candidate(db_conn, code, score) for code, score in hits]
    return {
        "ok": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "truncated": truncated,
        "match_kind": "rag",
        "match_mode": match_mode,
    }


LOOKUP_OPERAND_SPEC = ToolSpec(
    name="lookup_operand",
    handler=lookup_operand,
    required_params=("query",),
    description=(
        "Resolve a typed operand from design intent - pass an exact 4-letter code "
        "(e.g. EFFL, OPGT) for its definition, or a natural phrase (\"effective "
        "focal length\", \"hold one operand above another\") to get ranked "
        "candidate operands. Each candidate carries its description, units, and "
        "sign_convention (the constraint direction). Defaults to the merit-function "
        "catalog; pass domain=\"tolerance\" to resolve tolerance codes instead "
        "(tilt/decenter resolve to a FAMILY that differs by mechanism - surface vs "
        "element vs coordinate-break - so read each candidate's category and "
        "precondition_class and pick the code; a cb_required code with no "
        "coordinate-break surface will not bite). Gotcha: do NOT trust the top "
        "candidate blindly - within the arithmetic/relational family (DIFF, DIVI, "
        "OPGT, OPLT, EQUA, ...) the rank-1 hit can be a plausible-but-wrong sibling, "
        "so read the candidate descriptions and sign_convention and pick the code "
        "yourself. See search_reference for open-ended manual questions."
    ),
    kind="operand",
    param_types={
        "query": "string",
        "domain": "string",
        "limit": "integer",
        "top_n": "integer",
    },
)

TOOL_SPECS = (LOOKUP_OPERAND_SPEC,)
