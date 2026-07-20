"""tools/search_reference.py — whole-manual FTS5 RAG (session-free, never-raise).

The Layer-2 grounding tool: an OPEN-ENDED free-text query over the entire
OpticStudio manual corpus (the gitignored ``manual_corpus.db`` built by
``manual_build``). Returns BM25-ranked snippet+citation candidates. The complement
to ``lookup_operand`` (the exact intent→operand surface): the LAYERING CONTRACT
(§9) keeps exact 4-letter codes on ``lookup_operand`` — there is NO
exact-code path here; ``search_reference`` is RAG-only by definition.

Handler signature ``search_reference(manual_conn, params)`` — arg-0 is the MANUAL
corpus connection the ``Dispatcher`` threads by ``spec.kind='manual_rag'`` (PIN 3).
When the corpus is absent the Dispatcher answers ``corpus_unavailable`` BEFORE the
handler is called, so the handler never sees a ``None`` connection.

Failure discipline (mirrors ``lookup_operand`` verbatim, PIN 5):
- missing/ill-typed ``query`` -> ``ToolParamError`` (dispatch classifies it);
- a cap (``limit``/``top_n``) bool/non-int/<1, or both aliases -> ``ToolParamError``
  via the SHARED ``_query_params`` helpers (the bool-before-int trap lives once);
- empty/blank query -> ``reference_unknown`` envelope (never handed to MATCH);
- a HYPHENATED / metachar-bearing natural query (e.g. ``anti-reflection``) is
  SANITIZED before MATCH (``sanitize_query``): each whitespace-split term is
  wrapped in double-quotes (embedded ``"`` doubled), so FTS5 treats every term as
  a LITERAL phrase and the bare ``-`` no longer parses as a column/NOT operator.
  This is the right default for a RAG tool fed natural agent prose: the user is
  searching for words, not authoring FTS grammar. A side effect is that prior
  FTS metacharacters (``*``/``^``/boolean keywords) lose their special meaning
  here and become literal-word searches — ``lookup_operand`` keeps the raw-grammar
  passthrough; ``search_reference`` is deliberately literal.
- the ``except sqlite3.OperationalError -> query_malformed`` backstop REMAINS
  (defense in depth): a query the sanitizer cannot neutralize still pre-empts as
  ``query_malformed``, never ``internal``; the MATCH is parameterized, never
  f-string;
- a valid query matching nothing -> ``reference_unknown`` envelope.
"""
import sqlite3

from .._envelope import error_envelope
from ..errors import ToolParamError
from ..server import ToolSpec
from ._query_params import effective_n as _resolve_effective_n
from ._rag_match import _MAX_QUERY_LEN as _MAX_QUERY_LEN  # re-export (lifted guard)
from ._rag_match import and_expr, assert_query_well_formed

# The ranked-candidate cap (default + hard ceiling), matching ``lookup_operand``.
TOP_N = 10

_TOOL = "search_reference"

# The snippet window (FTS5 ``snippet()`` token budget around the match).
_SNIPPET_TOKENS = 16

# ``_MAX_QUERY_LEN`` is re-exported from ``_rag_match`` (the lifted source of
# truth) so existing call sites / tests that import it from here still resolve.


def sanitize_query(query):
    """Turn a natural query into an implicit-AND of FTS5 LITERAL phrase terms.

    Delegates to the shared ``_rag_match.and_expr`` (the single source of truth for
    per-term literal-phrase quoting). BYTE-IDENTICAL to the prior body: splits
    ``query`` on whitespace; for each term, escapes an embedded double-quote
    (``"`` -> ``""``) and wraps the term in double-quotes; joins with spaces. FTS5
    treats a double-quoted string as a literal phrase, so a hyphenated term like
    ``anti-reflection`` matches literally (the ``-`` no longer parses as a
    column/NOT operator) and the space-join is an implicit AND.

    Returns the sanitized MATCH string. An all-whitespace ``query`` yields ``""``
    (no terms) — the caller treats that as the empty-query ``reference_unknown``
    case BEFORE this is reached, so a non-blank query always yields >= 1 term here.
    """
    return and_expr(query)


def search_reference(manual_conn, params):
    """Free-text RAG over the manual corpus (never raises past its boundary).

    See module docstring for the routing and failure discipline.
    """
    # --- param validation (raises -> dispatch classifies as tool_param) ---
    if "query" not in params:
        raise ToolParamError("missing required param 'query'")
    query = params["query"]
    if not isinstance(query, str):
        raise ToolParamError(
            f"'query' must be a string; got {type(query).__name__}"
        )
    effective_n = _resolve_effective_n(params, TOP_N)

    # An empty/blank query is meaningless for MATCH (and FTS5 would error) ->
    # treat it as a structured no-match, never hand it to MATCH.
    if not query.strip():
        return error_envelope(_TOOL, "reference_unknown", "empty query", query=query)

    # Never-raise input guards (BEFORE sanitize/MATCH so the handler owns the
    # failure family). A NUL/control char would survive sanitize and make execute()
    # raise sqlite3.ProgrammingError (a DatabaseError sibling NOT caught by the
    # OperationalError backstop -> misclassified 'internal'); an oversized query
    # would raise MemoryError in split()/join(). Both pre-empt as query_malformed
    # via the SHARED guard (one source of truth across both RAG tools).
    # NOTE: we deliberately do NOT widen the backstop to ``sqlite3.Error`` — a
    # genuine corrupt-DB DatabaseError must stay 'internal', not be relabelled.
    malformed_reason = assert_query_well_formed(query)
    if malformed_reason is not None:
        return error_envelope(_TOOL, "query_malformed", malformed_reason, query=query)

    # Sanitize: wrap each term as an FTS5 LITERAL phrase so a hyphenated /
    # metachar-bearing natural query (e.g. 'anti-reflection') matches literally
    # and never trips the FTS grammar (defence: the OperationalError backstop
    # below still pre-empts anything the sanitizer cannot neutralize). A non-blank
    # query always yields >= 1 term (split drops only whitespace); a sanitized-to-
    # empty string would mean a blank query, already handled above. Guard anyway.
    match_expr = sanitize_query(query)
    # INVARIANT: unreachable for a non-blank query — ``str.split()`` on any string
    # with a non-whitespace char yields >= 1 term, so ``sanitize_query`` returns a
    # non-empty quoted expression; the blank case already returned above. Kept as
    # defense-in-depth so a future sanitize change that can emit "" stays safe.
    if not match_expr:
        return error_envelope(_TOOL, "reference_unknown", "empty query", query=query)

    # --- RAG path: parameterized FTS5 MATCH (NEVER f-string), bm25-ranked ---
    # Over-fetch effective_n + 1 so ``truncated`` is correct AT THE BOUNDARY.
    # rowid-join back to the content table for the citation metadata.
    try:
        rows = manual_conn.execute(
            "SELECT m.chunk_id, m.section_path, m.page, "
            "bm25(manual_chunk_fts) AS score, "
            "snippet(manual_chunk_fts, 0, '[', ']', '...', ?) AS snip "
            "FROM manual_chunk_fts JOIN manual_chunk m "
            "ON m.rowid = manual_chunk_fts.rowid "
            "WHERE manual_chunk_fts MATCH ? ORDER BY score LIMIT ?",
            (_SNIPPET_TOKENS, match_expr, effective_n + 1),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # FTS5 metacharacter syntax error -> PRE-EMPT as query_malformed; never
        # let it fall through to 'internal'.
        return error_envelope(_TOOL, "query_malformed", str(exc), query=query)

    if not rows:
        return error_envelope(
            _TOOL, "reference_unknown", "no chunk matched the query", query=query
        )

    truncated = len(rows) > effective_n
    hits = rows[:effective_n]
    candidates = [
        {
            "chunk_id": chunk_id,
            "section_path": section_path,
            "page": page,
            "score": score,
            "snippet": snip,
            "citation": _citation(section_path, page),
        }
        for (chunk_id, section_path, page, score, snip) in hits
    ]
    return {
        "ok": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "truncated": truncated,
        "match_kind": "rag",
    }


def _citation(section_path, page):
    """Build a human-traceable citation handle for a chunk hit."""
    if section_path:
        return f"manual:p{page}/§{section_path}"
    return f"manual:p{page}"


SEARCH_REFERENCE_SPEC = ToolSpec(
    name="search_reference",
    handler=search_reference,
    required_params=("query",),
    description=(
        "Search the OpticStudio manual for open-ended questions - concepts, "
        "procedures, terminology - and get back ranked text snippets, each with a "
        "page + section citation. Gotcha: this is prose search, not the operand "
        "index - for an exact 4-letter operand code or an intent->operand mapping "
        "use lookup_operand instead. See lookup_operand for operand grounding."
    ),
    kind="manual_rag",
    param_types={
        "query": "string",
        "limit": "integer",
        "top_n": "integer",
    },
)

TOOL_SPECS = (SEARCH_REFERENCE_SPEC,)
