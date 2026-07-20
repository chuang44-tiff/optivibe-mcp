"""tools/_rag_match.py — shared FTS5 query-preprocessing helpers (session-free).

The single source of truth for turning a natural-language query into an FTS5
MATCH expression for the two RAG tools (``lookup_operand`` and
``search_reference``). Factored out so per-term literal-phrase quoting and the
query well-formedness guards live ONCE (PIN 4, the established shared-helper
pattern beside ``_query_params.py``).

Three MATCH builders + one well-formedness guard:
- ``quote_term(t)`` — wrap one term as an FTS5 LITERAL phrase (the bare ``-``/
  ``*``/``^``/``:``/AND/OR/NOT lose their special meaning). The single source of
  truth for per-term quoting, lifted VERBATIM from ``search_reference.sanitize_query``.
- ``and_expr(query)`` — implicit-AND of literal-phrase terms; BYTE-IDENTICAL to
  the prior ``sanitize_query`` output (the regression-guard contract).
- ``or_expr(query)`` — OR-join of the same literal-phrase terms (the relaxed pass
  for ``lookup_operand``: fires ONLY when the AND pass zeroes).
- ``assert_query_well_formed(query)`` — returns an error-reason string or None;
  lifts the NUL/control-char + ``_MAX_QUERY_LEN`` (4096) checks out of
  ``search_reference`` so both RAG tools own the ``query_malformed`` family
  deterministically BEFORE any MATCH (the §3 hardening-parity contract).
"""

# Max accepted query length (chars). A multi-megabyte query would raise
# ``MemoryError`` inside ``split()``/``join()`` in the MATCH builders — a
# non-OperationalError that escapes the OperationalError backstop and is then
# misclassified as 'internal'. Cap the input BEFORE building so the handler owns
# the failure family (``query_malformed``). 4096 chars is far above any genuine
# natural-language RAG query while staying cheap to build.
_MAX_QUERY_LEN = 4096

# Whitespace control chars that are legitimate term separators: ``str.split()``
# drops them, so they never reach MATCH. Every OTHER C0/C1 control char (NUL
# especially) survives ``split()`` inside a term and makes the parameterized
# ``execute`` raise ``sqlite3.ProgrammingError`` (a ``DatabaseError`` SIBLING of
# ``OperationalError`` NOT caught by the OperationalError backstop -> 'internal').
_ALLOWED_CONTROL_CHARS = frozenset({"\t", "\n", "\r"})


def quote_term(t):
    """Wrap one term as an FTS5 LITERAL phrase (embedded ``"`` doubled).

    FTS5 treats a double-quoted string as a literal phrase, so a hyphenated term
    like ``anti-reflection`` matches literally (the ``-`` no longer parses as a
    column/NOT operator) and bare ``*``/``^``/``:``/AND/OR/NOT lose their special
    meaning. The single source of truth for per-term quoting (lifted verbatim from
    the prior ``search_reference.sanitize_query`` body).
    """
    return '"' + t.replace('"', '""') + '"'


def and_expr(query):
    """Implicit-AND of literal-phrase terms (the current ``sanitize_query`` output).

    Splits ``query`` on whitespace, quotes each term via ``quote_term``, and
    space-joins them (an FTS5 implicit AND). BYTE-IDENTICAL to the prior
    ``search_reference.sanitize_query`` — this equality is the regression-guard
    contract (§1a). An all-whitespace query yields ``""`` (no terms); callers
    handle the blank case BEFORE calling this.
    """
    return " ".join(quote_term(term) for term in query.split())


def or_expr(query):
    """OR-join of the SAME literal-phrase terms (the relaxed-pass MATCH).

    Same per-term quoting as ``and_expr`` but joined with ``" OR "`` so any single
    term suffices to match; bm25 re-ranks natively. Used ONLY as the pass-2
    fallback in ``lookup_operand`` when the AND pass returns zero rows — strictly
    additive (never mutates the token SET, never fires when AND already matched).
    """
    return " OR ".join(quote_term(term) for term in query.split())


def assert_query_well_formed(query):
    """Return an error-reason string if ``query`` is ill-formed, else None.

    The ONE source of truth for query well-formedness across both RAG tools.
    Lifts the NUL/control-char + ``_MAX_QUERY_LEN`` checks: a disallowed control
    char (NUL or any C0/C1 NOT in {tab, \\n, \\r}) would survive ``split()`` and
    make the parameterized ``execute`` raise ``sqlite3.ProgrammingError`` (escapes
    the OperationalError backstop -> 'internal'); an oversized query would raise
    ``MemoryError`` in ``split()``/``join()``. Both pre-empt as ``query_malformed``.
    Called AFTER the empty-query check, BEFORE any MATCH.
    """
    for ch in query:
        # C0 controls (< 0x20) and DEL (0x7f); the NUL is < 0x20 so it is covered.
        code = ord(ch)
        if (code < 0x20 or code == 0x7F) and ch not in _ALLOWED_CONTROL_CHARS:
            return "query contains a NUL or other disallowed control character"
    if len(query) > _MAX_QUERY_LEN:
        return f"query exceeds the {_MAX_QUERY_LEN}-char limit (len={len(query)})"
    return None
