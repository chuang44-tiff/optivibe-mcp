"""manual_enrich.py — bbox code->description pairing for operand enrichment.

Implements the probe's finding-2 rule (the manual-rag probe findings): the
alphabetical operand table is TWO-COLUMN and naive text flow DECOUPLES the code
from its description. PyMuPDF word-bbox extraction recovers the pairing: a line
whose leftmost word starts at x0 ≈ 85 OPENS a new operand entry (first token =
the 4-letter code, remainder = first description line); lines at x0 ≈ 137 are
CONTINUATION; the next x0 ≈ 85 line closes the prior entry.

The extracted ``raw_text`` is VERBATIM manual prose — it is the ORACLE the human
authors a local paraphrase from, NEVER a committed artifact (§6/§8). It lands only in a gitignored scratch capture. This module's committed
output is the build script; the descriptions a human authors live in
``data/operand_descriptions.json`` (paraphrased, cited).

The pure pairing (``pair_operands``) is fitz-FREE and FAST-testable from synthetic
``(page, [(x0, line_text), ...])`` fixtures. ``iter_operand_pages`` is the lazy
fitz extraction over the live PDF.
"""
import re
import sys

from .manual_build import SECTION_HEADING_RE, normalize, strip_header_footer

# Column x0 bands (probe finding 2): code column ≈ 84.7, description ≈ 137.3.
CODE_X_BAND = (80.0, 100.0)
CONT_X_BAND = (120.0, 155.0)

# An operand code: 2-5 uppercase letters/digits, leading letter (e.g. EFFL, REAY,
# RMSWavefront-style codes are <=4 here; guard rejects prose tokens like "The").
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,4}$")

# The alphabetical operand section is "5.2.1.3. Optimization Operands
# (Alphabetically)". This phrase appears TWICE in the PDF: once in the TOC /
# cross-references (indented, x0 ~172, with dot leaders) and once as the REAL
# body heading at the left margin (x0 ~82, near physical page 1145). Matching the
# bare phrase started at the TOC page; matching the SECTION-NUMBER-PREFIXED
# heading line at the LEFT MARGIN lands on the real body.
_ALPHA_SECTION_PREFIX = "5.2.1.3"
# START heading: the line must BEGIN with the exact section number "5.2.1.3"
# (NOT a deeper "5.2.1.3.x") followed by the section title. ``5\.2\.1\.3``
# anchored, with a negative lookahead for a further ".<digit>" so a sub-section
# like "5.2.1.30" (none exists, but defensive) can't masquerade as the heading.
_START_HEADING_RE = re.compile(
    r"^\s*5\.2\.1\.3(?!\.?\d)\.?\s+Optimization Operands", re.IGNORECASE
)
# END heading: the NEXT real section heading — a DOTTED section number (>=1 dot,
# so a bare continuation number like "200 radial points ..." is NOT a heading)
# followed by a CAPITALIZED title word. The matched number must NOT live under the
# 5.2.1.3 prefix (those are still inside the alphabetical section). This is the
# SAME rule the build-side section tracker uses, so it is the SHARED
# ``SECTION_HEADING_RE`` (one source of truth — centralized). Only the
# number-prefix logic and x0 cap below are enrich-specific.
_END_HEADING_RE = SECTION_HEADING_RE
# A heading sits at the left margin; the TOC / cross-ref copies are indented far
# to the right (x0 ~172). Cap the heading x0 so an indented TOC line — which can
# carry the same section number — can never be read as the body heading.
_HEADING_X_MAX = 110.0


def pair_operands(pages_lines, code_band=CODE_X_BAND, cont_band=CONT_X_BAND):
    """Pair operand codes to their raw description text — PURE (finding-2 rule).

    ``pages_lines`` = iterable of ``(page_index, lines)`` where ``lines`` is a
    list of ``(leftmost_x0, line_text)`` for that page (header/footer already
    stripped). Returns ``(entries, order)`` where ``entries`` maps
    ``code -> {"code", "page", "raw_text"}`` (FIRST occurrence wins — the table is
    alphabetical, each code once) and ``order`` is the discovery order.

    A line in the CODE band whose first token is a valid code OPENS an entry; CONT
    band lines append to the open entry; lines outside both bands are ignored
    (defensive — a header/footer fragment that slipped the y-band).
    """
    entries = {}
    order = []
    current = None  # {"code", "page", "parts": [str, ...]}

    def flush():
        if current is None:
            return
        raw = normalize(" ".join(current["parts"]))
        code = current["code"]
        if code not in entries:
            entries[code] = {"code": code, "page": current["page"], "raw_text": raw}
            order.append(code)

    for page_index, lines in pages_lines:
        for x0, text in lines:
            if code_band[0] <= x0 <= code_band[1]:
                tokens = text.split()
                if tokens and _CODE_RE.match(tokens[0]):
                    flush()
                    rest = " ".join(tokens[1:])
                    current = {
                        "code": tokens[0],
                        "page": page_index,
                        "parts": [rest] if rest else [],
                    }
                elif current is not None:
                    # a code-band line that is NOT a code (rare) -> treat as cont.
                    current["parts"].append(text)
            elif cont_band[0] <= x0 <= cont_band[1]:
                if current is not None:
                    current["parts"].append(text)
            # else: outside both bands -> ignore
    flush()
    return entries, order


def _lines_with_x(words):
    """Group bbox ``words`` into ``(leftmost_x0, line_text)`` rows (by y)."""
    rows = {}
    for w in words:
        x0, y0, word = w[0], w[1], w[4]
        rows.setdefault(round(y0), []).append((x0, word))
    out = []
    for y in sorted(rows):
        ws = sorted(rows[y], key=lambda t: t[0])
        leftmost = ws[0][0]
        text = " ".join(word for _x, word in ws)
        out.append((leftmost, text))
    return out


def _is_start_heading(x0, text):
    """True iff ``(x0, text)`` is the REAL 5.2.1.3 body heading (left margin).

    The phrase also appears in the TOC / cross-refs, but indented (x0 ~172); the
    left-margin cap excludes those so we start at the genuine section body.
    """
    return x0 <= _HEADING_X_MAX and _START_HEADING_RE.match(text) is not None


def _is_end_heading(x0, text):
    """True iff ``(x0, text)`` is the NEXT real section heading (ends the table).

    A heading is a LEFT-MARGIN line whose section number is DOTTED (>=1 dot) and
    is followed by a capitalized title word, AND whose number is not under the
    ``5.2.1.3`` prefix (deeper sub-sections, e.g. none here, stay inside). A bare
    continuation number like "200 radial points ..." has NO dot and so is never a
    heading; a right-indented continuation line is excluded by the x0 cap.
    """
    if x0 > _HEADING_X_MAX:
        return False
    m = _END_HEADING_RE.match(text)
    if not m:
        return False
    number = m.group(1)
    return number != _ALPHA_SECTION_PREFIX and not number.startswith(
        _ALPHA_SECTION_PREFIX + "."
    )


# =====================================================================
# Tolerance operand pairing (§7.2.1.1) — a SEPARATE heading-delimited strategy
# (probe FACT 3): the tolerance section is NOT the merit two-column code/description
# table. Each operand is a HEADING-DELIMITED prose sub-section
# ``7.2.1.1.N. <CODE>[, <CODE>...]: <Title>`` at the left margin (x0 ≈ 81.7); the
# description is the prose from that heading to the next ``7.2.1.x`` heading. A
# multi-code heading (``TSDX, TSDY, TSDR``) shares ONE raw_text across every code.
# This is NOT a parameterization of ``pair_operands`` (the merit bbox two-column
# rule) — the tolerance layout has no continuation column.
# =====================================================================

# The per-operand heading opener: section number ``7.2.1.1.N.`` + a comma-separated
# UPPERCASE code list + ``:`` + the title. Non-greedy up to the FIRST colon so the
# code list never swallows the title. The summary-table heading
# ("7.2.1.1.1. Tolerance Operands Summary Table") has no code-list + colon, so it
# never opens a section.
_TOL_OPEN_RE = re.compile(r"^\s*7\.2\.1\.1\.(\d+)\.\s+([A-Z][A-Z0-9, ]*?):\s*(.*)$")
# The 7.2.1.1 operand-descriptions subsection prefix; a heading NOT under it (e.g.
# 7.2.1.2 "Tolerance Control Operands", 7.2.2, 7.3) closes the section.
_TOL_SECTION_PREFIX = "7.2.1.1"


def _is_tol_start_heading(x0, text):
    """True iff ``(x0, text)`` is a per-operand heading at the left margin."""
    return x0 <= _HEADING_X_MAX and _TOL_OPEN_RE.match(text) is not None


def _is_tol_end_heading(x0, text):
    """True iff ``(x0, text)`` is the NEXT section heading OUTSIDE 7.2.1.1.

    A left-margin dotted heading whose number is neither ``7.2.1.1`` nor a deeper
    ``7.2.1.1.N`` (those stay inside) ends the operand-descriptions section. A
    per-operand heading ``7.2.1.1.6`` is under the prefix, so it never ends it.
    """
    if x0 > _HEADING_X_MAX:
        return False
    m = _END_HEADING_RE.match(text)
    if not m:
        return False
    number = m.group(1)
    return number != _TOL_SECTION_PREFIX and not number.startswith(
        _TOL_SECTION_PREFIX + "."
    )


def _valid_tol_code(token, valid_codes):
    """True iff ``token`` is a legitimate tolerance operand code (F7).

    The heading opener regex admits an arbitrary UPPERCASE space-containing label
    (``IMPORTANT NOTE``) and a trailing all-caps title word swallowed into the code
    list (``TSDR TILT``). Validate each comma-split token at SOURCE against:

    - operand-code SHAPE (``_CODE_RE``: a 2..5-char ``[A-Z][A-Z0-9]{1,4}`` token — a
      space-containing / over-long label like ``IMPORTANT NOTE`` fails this); AND
    - the tolerance INVENTORY code set when ``valid_codes`` is supplied (a shaped but
      non-inventory token — a phantom acronym — is rejected too).

    A rejected token is DROPPED (not admitted as a phantom code). The synonyms keyset
    remains the downstream net, but this closes the leak at source (F7 + A5).
    """
    if _CODE_RE.match(token) is None:
        return False
    if valid_codes is not None and token not in valid_codes:
        return False
    return True


def pair_tolerance_operands(pages_lines, valid_codes=None):
    """Pair tolerance operand codes to their raw prose — PURE (FACT 3 heading rule).

    ``pages_lines`` = iterable of ``(page_index, lines)`` where ``lines`` is a list
    of ``(leftmost_x0, line_text)`` (header/footer already stripped). Returns
    ``(entries, order)`` where ``entries`` maps ``code -> {"code","page","raw_text"}``
    (FIRST occurrence wins) and ``order`` is the discovery order.

    A left-margin line matching the per-operand heading opener OPENS a section (its
    comma-separated code list is split and the SAME accumulated prose is attributed
    to EVERY code — FACT 3 caveat 2); every following line appends to the open
    section's prose until the next opener (or the end of input) closes it. Lines
    before the first opener (e.g. the summary table) are ignored.

    F7: each comma-split token is validated against operand-code SHAPE (always) and
    the ``valid_codes`` inventory set (when supplied) — a phantom heading token
    (``IMPORTANT NOTE``) or a swallowed title word (``TSDR TILT``) is DROPPED at
    source rather than admitted as a bogus code. The real ``_emit_raw`` caller passes
    the tolerance inventory code set.
    """
    entries = {}
    order = []
    current = None  # {"codes": [...], "page": int, "parts": [str, ...]}

    def flush():
        if current is None:
            return
        raw = normalize(" ".join(current["parts"]))
        for code in current["codes"]:
            if code not in entries:
                entries[code] = {
                    "code": code, "page": current["page"], "raw_text": raw
                }
                order.append(code)

    for page_index, lines in pages_lines:
        for x0, text in lines:
            if _is_tol_start_heading(x0, text):
                flush()
                m = _TOL_OPEN_RE.match(text)
                # F7: SHAPE + inventory validation drops phantom/swallowed tokens.
                # The F7 ruling drops a rejected token WITH a loud debug NOTE (not
                # silently) so a phantom-admitting heading is diagnosable in the raw
                # oracle build log — behavior is unchanged (still dropped, confined to
                # the gitignored oracle), only the drop is now visible on stderr.
                section = m.group(1)
                code_list = []
                for raw_tok in m.group(2).split(","):
                    tok = raw_tok.strip()
                    if not tok:
                        continue
                    if _valid_tol_code(tok, valid_codes):
                        code_list.append(tok)
                    else:
                        print(
                            f"manual_enrich: dropped non-inventory heading token "
                            f"{tok!r} (7.2.1.1.{section})",
                            file=sys.stderr,
                        )
                title_rest = m.group(3).strip()
                current = {
                    "codes": code_list,
                    "page": page_index,
                    "parts": [title_rest] if title_rest else [],
                }
            elif current is not None:
                # Section prose (no continuation column here) — append verbatim.
                current["parts"].append(text)
    flush()
    return entries, order


def iter_tolerance_operand_pages(pdf_path):
    """Yield ``(page_index, lines_with_x)`` over the §7.2.1.1 tolerance section.

    Starts at the FIRST per-operand heading ``7.2.1.1.N. <CODE>:`` at the left
    margin (skipping the summary table), then yields pages until the next section
    heading OUTSIDE 7.2.1.1 (e.g. 7.2.1.2 "Tolerance Control Operands"). Lazy fitz
    import; NEVER prints extracted text (FACT 3).
    """
    import fitz  # noqa: E402 — lazy: only the live extraction needs PyMuPDF

    doc = fitz.open(pdf_path)
    try:
        started = False
        for page_index in range(doc.page_count):
            page = doc[page_index]
            words = page.get_text("words")
            kept = strip_header_footer(words, page.rect.height)
            lines = _lines_with_x(kept)

            if not started:
                start_idx = None
                for i, (x0, text) in enumerate(lines):
                    if _is_tol_start_heading(x0, text):
                        start_idx = i
                        break
                if start_idx is None:
                    continue
                started = True
                lines = lines[start_idx:]

            end_idx = None
            for i, (x0, text) in enumerate(lines):
                if _is_tol_end_heading(x0, text):
                    end_idx = i
                    break
            if end_idx is not None:
                if end_idx > 0:
                    yield page_index, lines[:end_idx]
                break
            yield page_index, lines
    finally:
        doc.close()


def iter_operand_pages(pdf_path):
    """Yield ``(page_index, lines_with_x)`` over the alphabetical operand section.

    Starts ONLY at the genuine left-margin "5.2.1.3. Optimization Operands ..."
    body heading (skipping the TOC / cross-ref copies of the same phrase), then
    yields pages until the NEXT real section heading (a dotted section number not
    under 5.2.1.3, at the left margin) marks the next section. On the START page
    the previous-section heading ("5.2.1.2.34. ...") precedes the start heading —
    end-detection only runs on lines AFTER the start heading is seen, so that
    earlier heading can never trip a false end. Lazy fitz import. NEVER prints
    extracted text (finding 3).
    """
    import fitz  # noqa: E402 — lazy: only the live extraction needs PyMuPDF

    doc = fitz.open(pdf_path)
    try:
        started = False
        for page_index in range(doc.page_count):
            page = doc[page_index]
            words = page.get_text("words")
            kept = strip_header_footer(words, page.rect.height)
            lines = _lines_with_x(kept)

            if not started:
                # Find the start heading on this page; if present, emit ONLY the
                # lines at/after it (drop the preceding prior-section content).
                start_idx = None
                for i, (x0, text) in enumerate(lines):
                    if _is_start_heading(x0, text):
                        start_idx = i
                        break
                if start_idx is None:
                    continue
                started = True
                lines = lines[start_idx:]

            # Detect the end: the next real section heading. Scan only the lines
            # we are about to emit; if one ends the section, truncate to before it
            # and stop after yielding the partial page.
            end_idx = None
            for i, (x0, text) in enumerate(lines):
                if _is_end_heading(x0, text):
                    end_idx = i
                    break
            if end_idx is not None:
                if end_idx > 0:
                    yield page_index, lines[:end_idx]
                break
            yield page_index, lines
    finally:
        doc.close()
