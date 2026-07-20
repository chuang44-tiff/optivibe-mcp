"""manual_build.py — build the whole-manual FTS5 RAG corpus (LOCAL, gitignored).

The Layer-2 document store. Ingests ``OpticStudio_UserManual_en.pdf`` (the
licensed-install PDF) into a SEPARATE, gitignored SQLite ``.db`` exposing an
external-content FTS5 table over per-page (size-capped) chunks. Mirrors the
``catalog_build`` discipline (UTF-8/LF writes, package-relative anchors, build
the binary from source, never commit it) — see §3/§7.

PROVENANCE (§8): the ``.db`` and all chunk ``body`` text are
gitignored and NEVER committed. The only committed artifacts are this script and
a TEXT-FREE manifest (``data/manual_manifest.json`` — per-chunk structure +
checksums, zero prose). The build FAILS CLOSED if the PROVENANCE.md ledger row
that permits manual indexing is not on file.

The fitz (PyMuPDF) dependency is isolated to ``iter_pdf_pages`` /
``iter_operand_pages`` (imported lazily) so the pure chunking logic
(``build_chunks``) is FAST-testable from synthetic in-memory pages with no PDF.

Chunking (§0-A): one chunk per physical page (primary) PLUS a
size-cap secondary split at ~512 tokens, keyed ``(section_path, page, ordinal)``
with a CONTENT-ADDRESSED ``chunk_id = sha256(section_path|page|ordinal)`` so a
re-ingest can never silently re-point a citation (the ``checksum`` separately
pins the chunk TEXT).
"""
import hashlib
import os
import re
import sqlite3
from collections import namedtuple

from .errors import ProvenanceGateError

# --- constants -------------------------------------------------------------
# Bump when the chunker / normalizer changes (invalidates a content-addressed
# skip and is asserted by the drift test). v2: heading rule narrowed to R_dotcap
# (>=1 dot + capitalized title) — the section assignment changed, so every
# content-addressed chunk_id = sha256(section_path|page|ordinal) shifts.
BUILDER_VERSION = 2
SCHEMA_VERSION = 1
OPTIC_STUDIO_VERSION = "25.1.0"

# Page-layout constants — probe-cited defaults (the manual-rag probe findings
# finding 3): running header sits in the top margin, footer (release/copyright/
# page-number) in the bottom margin. Bands are stripped before chunking. The
# footer threshold is computed per-page as (page_height - FOOTER_MARGIN) so it is
# robust to a differing page size.
HEADER_Y = 50.0
FOOTER_MARGIN = 52.0
# Secondary size-cap: split a page body into <= this many whitespace tokens.
MAX_CHUNK_TOKENS = 512

# The PROVENANCE ledger row marker that must be on file before manual ingest.
_LEDGER_ROW_MARKER = "OpticStudio manual FTS5 index"

# A genuine numbered-heading line: a DOTTED section number (>=1 dot, e.g.
# "5.2.1.3" or "1.1") followed by a CAPITALIZED title word — rule R_dotcap. This
# is the SINGLE source of truth, shared with the enrich-side end-heading detector
# (manual_enrich imports it as _END_HEADING_RE). The probe (manual-rag-quality
# quality probe) found the prior ``(\d+(?:\.\d+)*)\.?\s+\S`` rule promoted 275 bare-integer
# BODY lines (e.g. a "200 radial points ..." continuation line) to section_paths —
# garbage citations — versus 948 genuine dotted headings, ALL period-bearing.
# Requiring >=1 dot kills the bare-integer false-promotions; requiring a leading
# capital after the number kills lowercase body-line false-promotions. The trade:
# a genuine SINGLE-LEVEL chapter heading ("2. Wavefront Notes", no dot) is no
# longer tracked — but on this manual the entire no-dot class was garbage, so the
# net is a large precision gain (probe Task C/D).
SECTION_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)\.?\s+[A-Z]")
# Back-compat alias (the section tracker reads this name).
_HEADING_RE = SECTION_HEADING_RE

# Path anchors (package-relative, NOT cwd-relative — agent cwd resets, §5).
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
MANUAL_DB_PATH = os.path.join(DATA_DIR, "manual_corpus.db")
MANUAL_MANIFEST_PATH = os.path.join(DATA_DIR, "manual_manifest.json")
# repo root = packages/optivibe-reference/.. /.. (DATA_DIR -> src/optivibe_reference/data)
_PKG_ROOT = os.path.dirname(os.path.dirname(_HERE))
_REPO_ROOT = os.path.dirname(os.path.dirname(_PKG_ROOT))
PROVENANCE_PATH = os.path.join(_REPO_ROOT, "PROVENANCE.md")
# Back-compat alias (callers historically imported the gate path by this name).

# A built chunk. ``body`` is LOCAL-ONLY (never committed); the manifest emits
# every field EXCEPT body.
Chunk = namedtuple(
    "Chunk", "chunk_id section_path page ordinal char_len checksum body"
)


# --- hashing helpers -------------------------------------------------------
def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --- provenance fail-closed gate ------------------------------------------
def assert_manual_grant_on_file(provenance_path=PROVENANCE_PATH):
    """Fail CLOSED unless the manual-indexing ledger row is on file (§7/§8).

    Reads ``PROVENANCE.md`` and requires the ledger-row marker. Raises
    ``ProvenanceGateError`` if the file is missing or the row is absent — guarding
    against a future deletion of the grant silently re-enabling ingest.
    """
    try:
        with open(provenance_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ProvenanceGateError(
            f"PROVENANCE ledger not readable at {provenance_path!r}: {exc}"
        )
    if _LEDGER_ROW_MARKER not in text:
        raise ProvenanceGateError(
            "manual-indexing grant row "
            f"({_LEDGER_ROW_MARKER!r}) not on file in PROVENANCE.md — "
            "manual ingest fails closed until the ledger row is recorded"
        )


# --- pure text/layout helpers (fitz-free; FAST-testable) -------------------
def normalize(text):
    """Collapse whitespace to single spaces and strip (the chunk-text norm).

    Used for BOTH the stored ``body`` and the ``checksum`` so the committed
    manifest hash matches a freshly-built ``.db`` deterministically.
    """
    return " ".join(text.split())


def strip_header_footer(words, page_height):
    """Drop running-header / footer words by y-band (probe finding 3).

    ``words`` is an iterable of ``(x0, y0, x1, y1, word)`` tuples. Returns the
    words whose vertical position is inside the body band
    ``HEADER_Y <= y0`` and ``y1 <= page_height - FOOTER_MARGIN``.
    """
    footer_cut = page_height - FOOTER_MARGIN
    return [
        w for w in words
        if w[1] >= HEADER_Y and w[3] <= footer_cut
    ]


def words_to_lines(words):
    """Reconstruct text lines from bbox words (group by y, sort by x0).

    ``words`` = iterable of ``(x0, y0, x1, y1, word)``. Returns a list of line
    strings, top-to-bottom. Two words share a line when their ``y0`` rounds to
    the same integer (the manual's rows are ~12pt apart; rounding to the unit is
    sufficient and matches the probe's row grouping).
    """
    rows = {}
    for w in words:
        x0, y0, _x1, _y1, word = w[0], w[1], w[2], w[3], w[4]
        key = round(y0)
        rows.setdefault(key, []).append((x0, word))
    lines = []
    for y in sorted(rows):
        ws = sorted(rows[y], key=lambda t: t[0])
        lines.append(" ".join(word for _x0, word in ws))
    return lines


class RunningSectionTracker:
    """Best-effort forward section-path tracker (§0-A).

    A heading line (numbered, e.g. ``5.2.1.3. ...``) updates the running section
    path; non-heading pages inherit the last-seen heading. When no heading has
    been seen yet, the path is ``''`` (→ page-only citation, never a guess).
    """

    def __init__(self):
        self._current = ""

    def update(self, lines):
        for line in lines:
            m = _HEADING_RE.match(line)
            if m:
                # Use the heading text (number + title) as the section path,
                # whitespace-normalized and length-capped (defensive).
                self._current = normalize(line)[:200]
        return self._current


def _split_tokens(text, max_tokens=MAX_CHUNK_TOKENS):
    """Split normalized ``text`` into <= ``max_tokens``-word slices (≥1 slice).

    Returns a list of ``(ordinal, slice_text)``. An empty/blank text yields ``[]``
    (a blank page produces no chunk).
    """
    tokens = text.split()
    if not tokens:
        return []
    out = []
    for ordinal, start in enumerate(range(0, len(tokens), max_tokens)):
        out.append((ordinal, " ".join(tokens[start:start + max_tokens])))
    return out


def build_chunks(pages, max_tokens=MAX_CHUNK_TOKENS):
    """Build the chunk list from page (index, text|lines) pairs — PURE.

    ``pages`` is an iterable of ``(page_index, page_lines)`` where ``page_lines``
    is either a list of line strings or a single text string. Header/footer
    stripping is assumed already applied by the caller (``iter_pdf_pages``); this
    function is the fitz-FREE chunker the FAST tests drive with synthetic pages.

    Page-primary + size-cap secondary split; content-addressed ``chunk_id``.
    """
    tracker = RunningSectionTracker()
    chunks = []
    for page_index, page_lines in pages:
        if isinstance(page_lines, str):
            lines = page_lines.splitlines()
        else:
            lines = list(page_lines)
        section = tracker.update(lines)
        page_text = normalize(" ".join(lines))
        for ordinal, slice_text in _split_tokens(page_text, max_tokens):
            norm = normalize(slice_text)
            chunk_id = _sha256_text(f"{section}|{page_index}|{ordinal}")
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    section_path=section,
                    page=page_index,
                    ordinal=ordinal,
                    char_len=len(norm),
                    checksum=_sha256_text(norm),
                    body=norm,
                )
            )
    return chunks


# --- fitz (PyMuPDF) extraction (lazy import; not needed for FAST tests) ----
def iter_pdf_pages(pdf_path):
    """Yield ``(page_index, lines)`` for every page, header/footer stripped.

    Lazy ``import fitz`` so importing this module needs no PyMuPDF. NEVER prints
    extracted text (the cp1252 ``conda run`` relay crash, finding 3).
    """
    import fitz  # noqa: E402 — lazy: only the build path needs PyMuPDF

    doc = fitz.open(pdf_path)
    try:
        for page_index in range(doc.page_count):
            page = doc[page_index]
            words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,wno)
            kept = strip_header_footer(words, page.rect.height)
            yield page_index, words_to_lines(kept)
    finally:
        doc.close()


def _pdf_page_count(pdf_path):
    import fitz  # noqa: E402

    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


# --- SQLite schema (§3) ----------------------------------------
_CREATE_META = """
CREATE TABLE meta (
    pdf_sha256      TEXT NOT NULL,
    pdf_page_count  INTEGER NOT NULL,
    chunk_count     INTEGER NOT NULL,
    optic_studio_version TEXT,
    builder_version INTEGER NOT NULL,
    build_complete  INTEGER NOT NULL DEFAULT 0
)
"""
_CREATE_CHUNK = """
CREATE TABLE manual_chunk (
    chunk_id      TEXT PRIMARY KEY,
    section_path  TEXT NOT NULL,
    page          INTEGER NOT NULL,
    ordinal       INTEGER NOT NULL,
    char_len      INTEGER NOT NULL,
    checksum      TEXT NOT NULL,
    body          TEXT NOT NULL
)
"""
_CREATE_CHUNK_INDEX = (
    "CREATE UNIQUE INDEX ux_chunk_key ON manual_chunk(section_path, page, ordinal)"
)
_CREATE_FTS = """
CREATE VIRTUAL TABLE manual_chunk_fts
USING fts5(body, content='manual_chunk', content_rowid='rowid')
"""


def _create_corpus_schema(conn):
    conn.execute(_CREATE_META)
    conn.execute(_CREATE_CHUNK)
    conn.execute(_CREATE_CHUNK_INDEX)
    conn.execute(_CREATE_FTS)


def _insert_chunks(conn, chunks):
    conn.executemany(
        "INSERT INTO manual_chunk (chunk_id, section_path, page, ordinal, "
        "char_len, checksum, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (c.chunk_id, c.section_path, c.page, c.ordinal, c.char_len,
             c.checksum, c.body)
            for c in chunks
        ],
    )
    conn.execute("INSERT INTO manual_chunk_fts(manual_chunk_fts) VALUES('rebuild')")


def build_corpus_db(conn, chunks, pdf_sha256, pdf_page_count,
                    optic_studio_version=OPTIC_STUDIO_VERSION):
    """Create the corpus schema in ``conn`` and insert ``chunks`` (build_complete LAST).

    Used directly by the FAST tests (synthetic chunks into an in-memory conn) AND
    by ``build_manual_db`` (real chunks into a temp file). Writes the ``meta`` row
    with ``build_complete=1`` LAST so a crash mid-insert never looks complete.
    """
    _create_corpus_schema(conn)
    _insert_chunks(conn, chunks)
    conn.execute(
        "INSERT INTO meta (pdf_sha256, pdf_page_count, chunk_count, "
        "optic_studio_version, builder_version, build_complete) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (pdf_sha256, pdf_page_count, len(chunks), optic_studio_version,
         BUILDER_VERSION),
    )
    conn.commit()
    return conn


def _read_meta(conn):
    """Return the meta row dict, or None if the table/row is unreadable.

    Catches ANY ``sqlite3.Error`` (not just ``OperationalError``): a garbage /
    non-sqlite file passes the lazy ``sqlite3.connect`` but the FIRST query raises
    ``sqlite3.DatabaseError`` ('file is not a database'), the SUPERCLASS of
    ``OperationalError``. Broadening to ``sqlite3.Error`` turns every unusable .db
    into a None return so ``open_manual_corpus`` never crashes the caller
    (§7 / gate 1).
    """
    try:
        row = conn.execute(
            "SELECT pdf_sha256, pdf_page_count, chunk_count, "
            "optic_studio_version, builder_version, build_complete FROM meta"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "pdf_sha256": row[0],
        "pdf_page_count": row[1],
        "chunk_count": row[2],
        "optic_studio_version": row[3],
        "builder_version": row[4],
        "build_complete": row[5],
    }


def open_manual_corpus(db_path=MANUAL_DB_PATH):
    """Open the gitignored manual corpus, or return None when absent/partial.

    Returns a read-only ``sqlite3.Connection`` ONLY when the build is complete and
    consistent: ``build_complete == 1`` AND ``meta.chunk_count == COUNT(*)``. A
    missing file, a partial build, or a count mismatch returns ``None`` (→ the
    Dispatcher answers ``corpus_unavailable``, never serving partial data).
    """
    if not os.path.isfile(db_path):
        return None
    conn = sqlite3.connect(db_path, check_same_thread=False)
    meta = _read_meta(conn)
    if meta is None or meta["build_complete"] != 1:
        conn.close()
        return None
    # The consistency COUNT can itself raise if meta says build_complete=1 but the
    # manual_chunk table is absent ('no such table') — turn that into None too, and
    # CLOSE the conn first (no leaked connection on any failure path, gate 1).
    try:
        actual = conn.execute("SELECT COUNT(*) FROM manual_chunk").fetchone()[0]
    except sqlite3.Error:
        conn.close()
        return None
    if actual != meta["chunk_count"]:
        conn.close()
        return None
    return conn


def _existing_db_matches(db_path, pdf_sha256):
    """True iff a complete ``.db`` already built from THIS pdf + builder exists."""
    conn = open_manual_corpus(db_path)
    if conn is None:
        return False
    try:
        meta = _read_meta(conn)
        return (
            meta is not None
            and meta["pdf_sha256"] == pdf_sha256
            and meta["builder_version"] == BUILDER_VERSION
        )
    finally:
        conn.close()


# --- manifest (committed, TEXT-FREE) ---------------------------------------
def build_manifest(chunks, pdf_sha256, pdf_page_count,
                   optic_studio_version=OPTIC_STUDIO_VERSION):
    """Build the TEXT-FREE manifest dict (§4) — zero prose.

    Each per-chunk entry is EXACTLY ``{chunk_id, page, ordinal, char_len,
    checksum}`` — all hashes/ints, NO ``body`` AND NO ``section_path`` prose. The
    ``section_path`` is deliberately dropped: manual headings can contain
    forbidden prior-vendor tokens (e.g. a competitor-conversion section name like
    "Convert <competitor> to OpticStudio"), so committing the raw heading would
    leak a vendor token. Nothing is lost for drift detection: ``chunk_id =
    sha256(section_path|page|ordinal)`` already content-addresses section
    identity, so chunk_id set-equality + per-chunk ``checksum`` fully cover drift.
    The ``section_path`` survives only in the gitignored local ``.db``
    (``manual_chunk.section_path``) where ``search_reference`` reads it for
    runtime citations.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "pdf_sha256": pdf_sha256,
        "pdf_page_count": pdf_page_count,
        "optic_studio_version": optic_studio_version,
        "chunk_count": len(chunks),
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "page": c.page,
                "ordinal": c.ordinal,
                "char_len": c.char_len,
                "checksum": c.checksum,
            }
            for c in chunks
        ],
    }


def write_manual_manifest(out_path, manifest):
    """Write the manifest as normalized-LF, indent=2 JSON (reuse §2 idiom)."""
    import json

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    text = json.dumps(manifest, indent=2, ensure_ascii=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.write("\n")


# --- top-level build (fail-closed, atomic, content-addressed skip) ---------
def build_manual_db(pdf_path, out_db=MANUAL_DB_PATH, out_manifest=MANUAL_MANIFEST_PATH,
                    provenance_path=PROVENANCE_PATH, force=False):
    """Build the manual corpus ``.db`` + write the text-free manifest.

    Order (§7): (1) FAIL CLOSED unless the ledger row is on file; (2)
    content-addressed skip if an up-to-date ``.db`` already matches this PDF; (3)
    extract+chunk into a TEMP ``.db``, write ``meta`` last, ``os.replace`` to the
    final path (ATOMIC — a partial build never becomes live); (4) write manifest.

    Returns a dict summary. NEVER prints extracted text.
    """
    assert_manual_grant_on_file(provenance_path)
    pdf_sha = sha256_file(pdf_path)
    if not force and _existing_db_matches(out_db, pdf_sha):
        return {"skipped": True, "reason": "up-to-date", "pdf_sha256": pdf_sha}

    page_count = _pdf_page_count(pdf_path)
    chunks = build_chunks(iter_pdf_pages(pdf_path))

    tmp = out_db + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    os.makedirs(os.path.dirname(out_db), exist_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        build_corpus_db(conn, chunks, pdf_sha, page_count)
    finally:
        conn.close()
    os.replace(tmp, out_db)  # atomic

    manifest = build_manifest(chunks, pdf_sha, page_count)
    write_manual_manifest(out_manifest, manifest)
    return {
        "skipped": False,
        "pdf_sha256": pdf_sha,
        "pdf_page_count": page_count,
        "chunk_count": len(chunks),
        "db_path": out_db,
        "manifest_path": out_manifest,
    }
