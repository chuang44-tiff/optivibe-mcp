"""tools/search_reference.py — whole-manual FTS5 RAG (session-free, never-raise).

The Layer-2 grounding tool: a short keyword query over the entire OpticStudio
manual corpus (the gitignored ``manual_corpus.db`` built by ``manual_build``).
Returns BM25-ranked snippet+citation candidates. The complement to
``lookup_operand`` (the exact intent→operand surface): the LAYERING CONTRACT
(§9) keeps exact 4-letter codes on ``lookup_operand`` — there is NO
exact-code path here; ``search_reference`` is RAG-only by definition.

Handler signature ``search_reference(manual_conn, params)`` — arg-0 is the MANUAL
corpus connection the ``Dispatcher`` threads by ``spec.kind='manual_rag'`` (PIN 3).
When the corpus is absent the Dispatcher answers ``corpus_unavailable`` BEFORE the
handler is called, so the handler never sees a ``None`` connection.

Query path, in order:
1. param validation, the blank query, the shared well-formedness guard, and the
   ``MAX_QUERY_TERMS`` cap (``ToolParamError``) — all BEFORE any SQL;
2. UK→US spelling normalisation of whole-word terms (``_normalize_terms``), guarded
   by the corpus vocabulary (the US form must be present and the UK form absent);
3. the AND pass (implicit-AND of literal phrases), ranked
   ``bm25(manual_chunk_fts, 1.0, _BM25_SECTION_WEIGHT)``; rows → ``match_mode: "and"``;
4. on an AND miss, the ABSENT-TERM GATE: every term that carries a letter or digit
   is checked for corpus presence with the SAME ``quote_term`` quoting the MATCH
   uses. A term with no letter or digit (``?``, ``&``, ``--``) tokenizes to
   nothing and is skipped. If any checked term is absent → ``reference_unknown``
   with ``absent_terms`` (raw query terms, deduped, query order), and the OR pass
   never runs;
5. otherwise the OR pass; rows → ``match_mode: "or"``; 0 rows → ``reference_unknown``
   WITHOUT ``absent_terms``.

Failure discipline (mirrors ``lookup_operand``, PIN 5):
- missing/ill-typed ``query`` or more than ``MAX_QUERY_TERMS`` terms ->
  ``ToolParamError`` (dispatch classifies it);
- a cap (``limit``/``top_n``) bool/non-int/<1, or both aliases -> ``ToolParamError``
  via the SHARED ``_query_params`` helpers (the bool-before-int trap lives once);
- empty/blank query -> ``reference_unknown`` envelope (never handed to MATCH);
- a HYPHENATED / metachar-bearing natural query (e.g. ``anti-reflection``) is
  SANITIZED before MATCH (``sanitize_query``): each whitespace-split term is
  wrapped in double-quotes (embedded ``"`` doubled), so FTS5 treats every term as
  a LITERAL phrase and the bare ``-`` no longer parses as a column/NOT operator.
  A side effect is that FTS metacharacters (``*``/``^``/boolean keywords/``col:``)
  lose their special meaning here and become literal-word searches;
- the ``except sqlite3.OperationalError -> query_malformed`` backstop REMAINS
  (defense in depth) around every statement; the MATCH is parameterized, never
  f-string;
- a valid query matching nothing -> ``reference_unknown`` envelope.
"""
import re
import sqlite3

from .. import manual_build
from .._envelope import error_envelope
from ..errors import ToolParamError
from ..server import ToolSpec
from ._query_params import effective_n as _resolve_effective_n
from ._rag_match import _MAX_QUERY_LEN as _MAX_QUERY_LEN  # re-export (lifted guard)
from ._rag_match import and_expr, assert_query_well_formed, or_expr, quote_term

# The ranked-candidate cap (default + hard ceiling), matching ``lookup_operand``.
TOP_N = 10

_TOOL = "search_reference"

# The snippet window (FTS5 ``snippet()`` token budget around the match).
_SNIPPET_TOKENS = 16

# bm25 column weights: body (column 0) = 1.0, section_path (column 1) = W. W is the
# SMALLEST of {1, 2, 3, 5} passing all three retrieval-evaluation gates on the
# rebuilt corpus (a selection rule fixed before any weight was measured).
_BM25_SECTION_WEIGHT = 1.0

# Term cap, enforced BEFORE any SQL. On an AND miss every term costs one presence
# MATCH (plus up to two for a UK-shaped word), so the cap bounds that fan-out; the
# per-term cost behind it was measured on the rebuilt corpus.
MAX_QUERY_TERMS = 32

# ``_MAX_QUERY_LEN`` is re-exported from ``_rag_match`` (the lifted source of
# truth) so existing call sites / tests that import it from here still resolve.

# UK→US spelling rules: (whole-word UK suffix pattern, US replacement, minimum stem).
# GENERAL suffix pairs, not a word list; applied only to an all-ASCII-letter term, only
# when the stem BEFORE the matched suffix has at least that rule's minimum letters, and
# only under the vocabulary guard (``_normalize_term``). The vocabulary guard proves
# presence, not equivalence, so each rule carries a stem floor reasoned from English
# morphology, not from which queries failed:
# - ``-ise`` family, stem >= 5: English has many short DISTINCT ``-ise`` words
#   (rise, wise, prise, poise, noise, raise, arise) that are not spelling variants;
# - ``-yse``, stem >= 3: analyse / catalyse / paralyse pass; the bare ``lyse`` does not;
# - ``-our``, stem >= 3: excludes four / hour / tour / your / pour (stem <= 2) and
#   keeps colour / honour / vapour / flavour;
# - ``-tre``, stem >= 2: metre / litre are true variants, and no common distinct short
#   ``-tre`` word exists.
_UK_US_RULES = (
    (re.compile(r"isation(s?)$"), r"ization\1", 5),
    (re.compile(r"is(e|es|ed|ing|er|ers)$"), r"iz\1", 5),
    (re.compile(r"ys(e|es|ed|ing)$"), r"yz\1", 3),
    (re.compile(r"our(s?)$"), r"or\1", 3),
    (re.compile(r"tre(s?)$"), r"ter\1", 2),
)
_ASCII_WORD_RE = re.compile(r"[A-Za-z]+")

_SELECT_SQL = (
    "SELECT m.chunk_id, m.section_path, m.page, "
    "bm25(manual_chunk_fts, 1.0, {w}) AS score, "
    "snippet(manual_chunk_fts, 0, '[', ']', '...', ?) AS snip "
    "FROM manual_chunk_fts JOIN manual_chunk m "
    "ON m.rowid = manual_chunk_fts.rowid "
    "WHERE manual_chunk_fts MATCH ? ORDER BY score LIMIT ?"
)
_PRESENCE_SQL = (
    "SELECT 1 FROM manual_chunk_fts WHERE manual_chunk_fts MATCH ? LIMIT 1"
)


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


def _term_present(manual_conn, term):
    """True iff ``term`` (as a literal phrase, ``quote_term``) matches any chunk."""
    return (
        manual_conn.execute(_PRESENCE_SQL, (quote_term(term),)).fetchone()
        is not None
    )


def _term_is_checkable(term):
    """True iff the term holds a letter or digit (else it tokenizes to nothing)."""
    return any(ch.isalnum() for ch in term)


def _normalize_term(manual_conn, term):
    """Return the US spelling of a UK-spelled word, or ``term`` unchanged.

    Rewrites only an all-ASCII-letter term that one ``_UK_US_RULES`` suffix rule
    matches with a stem of at least that rule's minimum letters before the suffix,
    and only when the US form is present in the corpus AND the UK form is not (the
    vocabulary guard: a UK spelling the manual itself uses stays as-is).
    """
    if not _ASCII_WORD_RE.fullmatch(term):
        return term
    lowered = term.lower()
    for pattern, replacement, min_stem in _UK_US_RULES:
        match = pattern.search(lowered)
        if match is None:
            continue
        if match.start() < min_stem:
            return term
        us = lowered[:match.start()] + match.expand(replacement)
        if _term_present(manual_conn, us) and not _term_present(manual_conn, term):
            return us
        return term
    return term


def _normalize_terms(manual_conn, terms):
    return [_normalize_term(manual_conn, t) for t in terms]


def _run_pass(manual_conn, expr, limit):
    sql = _SELECT_SQL.format(w=float(_BM25_SECTION_WEIGHT))
    return manual_conn.execute(sql, (_SNIPPET_TOKENS, expr, limit)).fetchall()


def search_reference(manual_conn, params):
    """Keyword RAG over the manual corpus (never raises past its boundary).

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

    raw_terms = query.split()
    # INVARIANT: unreachable for a non-blank query — ``str.split()`` on any string
    # with a non-whitespace char yields >= 1 term; the blank case already returned
    # above. Kept as defense-in-depth.
    if not raw_terms:
        return error_envelope(_TOOL, "reference_unknown", "empty query", query=query)
    if len(raw_terms) > MAX_QUERY_TERMS:
        raise ToolParamError(
            f"'query' has {len(raw_terms)} terms; the limit is {MAX_QUERY_TERMS} - "
            "send a short keyword query"
        )

    # --- RAG path: parameterized FTS5 MATCH (NEVER f-string), bm25-ranked ---
    # Over-fetch effective_n + 1 so ``truncated`` is correct AT THE BOUNDARY.
    try:
        terms = _normalize_terms(manual_conn, raw_terms)
        normalized = " ".join(terms)
        rows = _run_pass(manual_conn, sanitize_query(normalized), effective_n + 1)
        match_mode = "and"
        if not rows:
            # ABSENT-TERM GATE: a term the corpus never uses zeroes the AND pass;
            # answering it from an OR pass would be confident junk.
            absent = []
            for raw, term in zip(raw_terms, terms):
                if raw in absent or not _term_is_checkable(term):
                    continue
                if not _term_present(manual_conn, term):
                    absent.append(raw)
            if absent:
                return error_envelope(
                    _TOOL, "reference_unknown",
                    "a query term never occurs in the manual as that literal token "
                    "or phrase; drop or rephrase it and retry",
                    query=query, absent_terms=absent,
                )
            rows = _run_pass(manual_conn, or_expr(normalized), effective_n + 1)
            match_mode = "or"
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
        "match_mode": match_mode,
    }


def _citation(section_path, page):
    """Build a human-traceable citation handle for a chunk hit.

    ``page`` is the stored 0-based PDF page index; the handle names the 1-based
    physical page via ``manual_build.citation_page`` (``manual:p{page+1}``).
    The served ``page`` field stays 0-based.
    """
    if section_path:
        return f"manual:p{manual_build.citation_page(page)}/§{section_path}"
    return f"manual:p{manual_build.citation_page(page)}"


SEARCH_REFERENCE_SPEC = ToolSpec(
    name="search_reference",
    handler=search_reference,
    required_params=("query",),
    description=(
        "Search the OpticStudio manual for concepts, procedures and terminology "
        "and get back ranked text snippets, each with a page + section citation. "
        "Send a short keyword query: 1-4 distinctive terms or a heading-like "
        "phrase (e.g. 'ray aiming', 'thickness solves'), not a full question. "
        "Results report match_mode: 'and' when every term matched, 'or' for a "
        "looser any-term match worth discounting. If a whitespace-separated query "
        "term never occurs in the manual as that literal token or phrase, the "
        "search answers no match and lists the term in absent_terms - drop or "
        "rephrase it and retry. Gotcha: this is prose search, not the operand "
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
