"""glass_build.py — build the glass catalog (JSON + SQLite). Mirrors catalog_build.

Pipeline (§5/§6/§8), mirroring ``catalog_build.py``:

1. ``build_glass_catalog_json(glasscat_dir, provenance_md)`` — assert the
   ``.agf``-derivation grant row is on file FIRST (fail-closed
   ``ProvenanceGateError``), then parse every shipped ``.agf`` record, slice the
   leading ``FORMULA_CD_COUNT[formula]`` CD coeffs, recompute the cardinal indices
   (nd/ng/nF/nC), apply the §5 in-band validity guard, and store ``delta_pg_f``
   VERBATIM (ED token 4). ``provenance='agf_recompute'``.
2. ``write_glass_catalog_json`` — persist normalized-LF / ``ensure_ascii`` /
   ``indent=2`` (the same idiom as ``catalog_build.write_catalog_json``) so the
   forbidden-token scanner sees it and the drift hash is over THIS file.
3. ``build_glass_db`` / ``open_glass_catalog`` — build a keyed ``glass`` table
   (``PRIMARY KEY(catalog, name)``) from the committed JSON. EXACT-ONLY, NO FTS5.

THE CARDINAL DEFENSE (§5): a computed cardinal is stored ONLY when its line is
in-band; otherwise the field is ``null`` and the ``*_valid`` flag is ``False`` —
so an out-of-band IR glass (KRS5) NEVER returns a plausible-but-meaningless Pg,F.
``assert_glass_provenance_invariants`` enforces the value<->flag parity.

PROVENANCE (§8): no raw ``.agf`` is ever committed; only OUR computed/parsed
numbers + factual glass names land in ``glass_catalog.json``. The build fails
CLOSED unless the ``.agf``-derivation ledger row is on file in ``PROVENANCE.md``.
"""
import json
import os
import sqlite3

from .errors import ProvenanceGateError
from .glass_dispersion import (
    FORMULA_CD_COUNT,
    FORMULA_NAMES,
    LINE_C,
    LINE_F,
    LINE_d,
    LINE_g,
    abbe_vd,
    index_at,
    partial_pgf,
)
from .glass_parse import find_catalogs, iter_glass_records

SCHEMA_VERSION = 1
BUILDER_VERSION = 1

# A computed value whose |nF-nC| denominator is below this floor is a near-flat-
# dispersion artifact (e.g. a Conrady adhesive or a VACUUM row with nF~nC): BOTH
# the Abbe number Vd = (nd-1)/(nF-nC) AND the partial Pg,F = (ng-nF)/(nF-nC)
# divide by that same denominator, so a machine-noise denominator makes BOTH
# numerically meaningless. ONE source of truth — vd AND pg_f are nulled when
# |nF-nC| <= _DENOM_FLOOR even when the lines are in-band (§5 / gate 1).
_DENOM_FLOOR = 0.005
# Back-compat alias (the floor was historically named for vd only).
_VD_DENOM_FLOOR = _DENOM_FLOOR

# Path anchors (package-relative, NOT cwd-relative — agent cwd resets, §5).
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
GLASS_CATALOG_JSON = os.path.join(DATA_DIR, "glass_catalog.json")
# repo root = packages/optivibe-reference/.. /.. (DATA_DIR -> src/optivibe_reference/data)
_PKG_ROOT = os.path.dirname(os.path.dirname(_HERE))
_REPO_ROOT = os.path.dirname(os.path.dirname(_PKG_ROOT))
PROVENANCE_PATH = os.path.join(_REPO_ROOT, "PROVENANCE.md")
# Back-compat alias (callers historically imported the gate path by this name).

# The PROVENANCE ledger-row marker that must be on file before .agf derivation.
# ASCII substring of the committed row ("Glass catalog (Nd/Vd/coeffs/...)").
_LEDGER_ROW_MARKER = "Glass catalog (Nd/Vd/coeffs"


# --- provenance fail-closed gate (mirrors manual_build.assert_manual_grant) -
def assert_agf_grant_on_file(provenance_path=PROVENANCE_PATH):
    """Fail CLOSED unless the .agf-derivation ledger row is on file (§8).

    Reads ``PROVENANCE.md`` and requires the ledger-row marker. Raises
    ``ProvenanceGateError`` if the file is missing or the row is absent — guarding
    against a future deletion of the grant silently re-enabling .agf-derived
    commits. Mirrors ``manual_build.assert_manual_grant_on_file``.
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
            ".agf-derivation grant row "
            f"({_LEDGER_ROW_MARKER!r}) not on file in PROVENANCE.md — "
            "glass-catalog .agf derivation fails closed until the ledger row "
            "is recorded"
        )


def in_band(min_wave, max_wave, line):
    """True iff ``line`` (µm) lies within ``[min_wave, max_wave]`` (both present)."""
    return (min_wave is not None and max_wave is not None
            and min_wave <= line <= max_wave)


def _build_row(rec):
    """Recompute one parsed record into a committed-JSON row dict (§5/§6)."""
    formula = rec["formula"]
    need = FORMULA_CD_COUNT.get(formula, 0)
    # Slice the LEADING need coeffs; a short CD list is tolerated (recompute then
    # yields None -> null fields, never a raise).
    cd = (rec["cd"] or [])[:need]

    nd = index_at(formula, cd, LINE_d)
    ng = index_at(formula, cd, LINE_g)
    nf = index_at(formula, cd, LINE_F)
    nc = index_at(formula, cd, LINE_C)

    min_wave = rec["min_wave"]
    max_wave = rec["max_wave"]
    # Band membership of each cardinal line (the §5 in-band guard input).
    d_in_band = in_band(min_wave, max_wave, LINE_d)
    pg_f_in_band = (in_band(min_wave, max_wave, LINE_g)
                    and in_band(min_wave, max_wave, LINE_F)
                    and in_band(min_wave, max_wave, LINE_C))

    vd = abbe_vd(nd, nf, nc)
    pg_f = partial_pgf(ng, nf, nc)

    # The validity FLAG means "in-band AND the value was actually computed" — so a
    # band-OK glass whose recompute fell over (short CD -> None, or a degenerate
    # flat index nF==nC -> partial_pgf None) carries flag=False, not a True flag
    # over a null value. This keeps the §6 value<->flag biconditional EXACT (an
    # in-band-but-null pg_f, e.g. MISC.AGF N15 with a constant 1.5 index, would
    # otherwise break it) while still NEVER emitting an out-of-band number.
    nd_valid = d_in_band and nd is not None
    # pg_f = (ng-nF)/(nF-nC): meaningless when nF~nC even if the lines are in-band
    # (a VACUUM-style row has |nF-nC|~3e-6 -> a noise pg_f~0.556). The SAME
    # denominator floor vd carries (one source of truth, _DENOM_FLOOR) gates pg_f.
    pg_f_valid = (pg_f_in_band and pg_f is not None
                  and nf is not None and nc is not None
                  and abs(nf - nc) > _DENOM_FLOOR)
    # vd is meaningless when nF~nC even if d is in-band (near-flat dispersion).
    vd_ok = (d_in_band and vd is not None and nf is not None and nc is not None
             and abs(nf - nc) > _DENOM_FLOOR)

    return {
        "catalog": rec["catalog"],
        "name": rec["name"],
        "formula": formula,
        "formula_name": FORMULA_NAMES.get(formula),
        "cd": cd,
        "min_wave": min_wave,
        "max_wave": max_wave,
        "nd_stored": rec["nd_stored"],
        "vd_stored": rec["vd_stored"],
        "delta_pg_f": rec["dpgf"],  # VERBATIM ED[4]; no re-derivation (§5 R3)
        "nd": nd if nd_valid else None,
        "ng": ng if pg_f_valid else None,
        "nf": nf if pg_f_valid else None,
        "nc": nc if pg_f_valid else None,
        "vd": vd if vd_ok else None,
        "pg_f": pg_f if pg_f_valid else None,
        "nd_valid": nd_valid,
        "pg_f_valid": pg_f_valid,
        "provenance": "agf_recompute",
    }


def _completeness_score(row):
    """Count the structurally-load-bearing fields present on a parsed row.

    Used by the dedupe (H-2): on a within-catalog duplicate (catalog, name) key, we
    keep the MORE structurally-complete record rather than blindly last-wins, so a
    truncated bare ``NM DUP`` (formula/cd/min_wave/max_wave all None) can never
    overwrite the complete first record. The score counts the four fields that a
    truncation drops; genuine near-identical dups (e.g. two full records) tie and
    fall back to the documented OpticStudio last-wins behavior.
    """
    return sum(
        1 for k in ("formula", "cd", "min_wave", "max_wave")
        if row.get(k) is not None
    )


def assert_glass_provenance_invariants(rows):
    """Assert the §6 value<->flag parity over the catalog rows (never disagree).

    - non-null ``pg_f`` <=> ``pg_f_valid is True``;
    - non-null ``nd``   <=> ``nd_valid is True``;
    - every row's ``provenance`` is ``'agf_recompute'``.

    Raises ``AssertionError`` on any violation. Used by the build and the tests.
    """
    for row in rows:
        key = (row.get("catalog"), row.get("name"))
        assert (row["pg_f"] is not None) == bool(row["pg_f_valid"]), (
            f"{key}: pg_f value<->pg_f_valid flag mismatch"
        )
        assert (row["nd"] is not None) == bool(row["nd_valid"]), (
            f"{key}: nd value<->nd_valid flag mismatch"
        )
        assert row["provenance"] == "agf_recompute", (
            f"{key}: bad provenance {row['provenance']!r}"
        )


def build_glass_catalog_json(glasscat_dir, provenance_md=PROVENANCE_PATH):
    """Build the normalized glass-catalog dict from the shipped .agf set (§5/§8).

    START GATE: ``assert_agf_grant_on_file`` is the FIRST action (fail-closed). Then
    every record in every catalog is parsed + recomputed into a row (§6 schema).
    ``agf_glass_count`` counts EVERY parsed record (in-band or not).
    """
    assert_agf_grant_on_file(provenance_md)
    # ``agf_glass_count`` counts every RAW parsed record. A handful of shipped
    # catalogs carry a same-name DUPLICATE entry (e.g. HIKARI.AGF E-F2 appears
    # twice with near-identical coeffs) — the composite (catalog, name) PRIMARY KEY
    # cannot hold both, so we DEDUPE keeping the LAST occurrence (OpticStudio's own
    # loader is last-wins) and emit one row per key. The raw count is preserved in
    # ``agf_glass_count`` so the offline-repro drift gate still pins the true record
    # tally; the Nd checksum is over the (deterministic) deduped row set.
    by_key = {}
    count = 0
    quarantined = 0
    empty_catalogs = []
    for path in find_catalogs(glasscat_dir):
        # Per-catalog record count: a shipped .agf that parses to ZERO records is a
        # hard build failure, NEVER a silent drop. ~11 install catalogs ship
        # UTF-16-LE; a decode-encoding regression there yields zero records and
        # would silently shrink the catalog (the SILENT-DATA-LOSS defect this guard
        # closes). The load-bearing guard: assert every enumerated catalog >= 1.
        per_catalog = 0
        for rec in iter_glass_records(path):
            count += 1
            per_catalog += 1
            row = _build_row(rec)
            # M-1: a record with no NM name token yields name=None — that can never
            # be a real keyed glass (it would land a junk (catalog, None) PRIMARY
            # KEY). QUARANTINE it (skip + tally), but NOT a record that merely lacks
            # optical data (formula None with a valid name is a safe-null row that
            # stays). A blank/whitespace name is treated the same as None.
            name = row["name"]
            if name is None or (isinstance(name, str) and not name.strip()):
                quarantined += 1
                continue
            key = (row["catalog"], name)
            # H-2: on a duplicate key, keep the MORE structurally-complete row. A
            # within-catalog dup whose SECOND record is truncated (bare "NM DUP"
            # with formula/cd None) must NOT overwrite the complete first row with a
            # null-filled one. Only when the two are EQUALLY complete do we fall
            # back to last-wins (the documented OpticStudio behavior for genuine
            # near-identical dups like a doubled full record).
            prev = by_key.get(key)
            if prev is None or _completeness_score(row) >= _completeness_score(prev):
                by_key[key] = row
        if per_catalog == 0:
            empty_catalogs.append(os.path.basename(path))
    if empty_catalogs:
        raise ValueError(
            "glass-catalog build: %d enumerated .agf catalog(s) parsed to ZERO "
            "records (a silently-empty catalog is now a hard build failure, not a "
            "silent drop) — check the file encoding (UTF-16 catalogs need BOM "
            "decode): %s" % (len(empty_catalogs), ", ".join(sorted(empty_catalogs)))
        )
    rows = [by_key[k] for k in sorted(by_key)]
    assert_glass_provenance_invariants(rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "agf_glass_count": count,
        "quarantined_count": quarantined,
        "row_count": len(rows),
        "rows": rows,
    }


def write_glass_catalog_json(out_path, catalog):
    """Write ``catalog`` as normalized-LF, indent=2 JSON (reuse §2 idiom).

    ``newline="\n"`` forces LF on every platform so the committed artifact is
    byte-stable and the reproducibility hash is over THIS file (§8). Mirrors
    ``catalog_build.write_catalog_json``.
    """
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    text = json.dumps(catalog, indent=2, ensure_ascii=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.write("\n")


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --- SQLite schema (§6) — keyed, EXACT-ONLY, NO FTS5 -----------------------
_CREATE_GLASS = """
CREATE TABLE glass (
    catalog       TEXT NOT NULL,
    name          TEXT NOT NULL,
    formula       INTEGER,
    formula_name  TEXT,
    cd            TEXT,
    min_wave      REAL,
    max_wave      REAL,
    nd_stored     REAL,
    vd_stored     REAL,
    delta_pg_f    REAL,
    nd            REAL,
    ng            REAL,
    nf            REAL,
    nc            REAL,
    vd            REAL,
    pg_f          REAL,
    nd_valid      INTEGER NOT NULL,
    pg_f_valid    INTEGER NOT NULL,
    provenance    TEXT,
    PRIMARY KEY (catalog, name)
)
"""


def build_glass_db(catalog_json_path, conn):
    """Build the keyed ``glass`` table from the committed JSON (NO FTS5).

    ONE-SHOT: ``build_glass_db`` creates the ``glass`` table on a FRESH connection.
    It is NOT idempotent against a persistent path — a second call on a connection
    that already holds a ``glass`` table raises a clear ``RuntimeError`` (GLASS-3)
    rather than the cryptic ``sqlite3.OperationalError: table glass already
    exists``. The runtime dispatch path uses ``:memory:`` (a fresh table per open),
    so this never bites at runtime; the guard makes a developer's accidental reuse
    of a persistent path fail clearly. Pass a fresh ``:memory:`` db or a new path to
    rebuild. We deliberately do NOT add ``IF NOT EXISTS`` (that would silently REUSE
    a stale table) or any version-reconcile machinery (no persistent-reuse path is
    in use — YAGNI).

    The SQL ``PRIMARY KEY (catalog, name)`` raises ``IntegrityError`` on a
    duplicate (the storage-layer dedupe). ``cd`` is stored as a JSON string. The
    ``*_valid`` flags are stored as 0/1 INTEGERs. Returns ``conn``.
    """
    # One-shot precondition: a pre-existing ``glass`` table means this connection
    # was already built — reject reuse with a clear message (not the cryptic
    # "table glass already exists" OperationalError that CREATE TABLE would raise).
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='glass'"
    ).fetchone()
    if existing is not None:
        raise RuntimeError(
            "glass table already exists; open_glass_catalog/build_glass_db is "
            "one-shot — pass a fresh :memory: db or a new path"
        )
    catalog = _load_json(catalog_json_path)
    conn.execute(_CREATE_GLASS)
    for row in catalog["rows"]:
        conn.execute(
            "INSERT INTO glass (catalog, name, formula, formula_name, cd, "
            "min_wave, max_wave, nd_stored, vd_stored, delta_pg_f, nd, ng, nf, "
            "nc, vd, pg_f, nd_valid, pg_f_valid, provenance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["catalog"],
                row["name"],
                row.get("formula"),
                row.get("formula_name"),
                json.dumps(row.get("cd")),
                row.get("min_wave"),
                row.get("max_wave"),
                row.get("nd_stored"),
                row.get("vd_stored"),
                row.get("delta_pg_f"),
                row.get("nd"),
                row.get("ng"),
                row.get("nf"),
                row.get("nc"),
                row.get("vd"),
                row.get("pg_f"),
                1 if row.get("nd_valid") else 0,
                1 if row.get("pg_f_valid") else 0,
                row.get("provenance"),
            ),
        )
    conn.commit()
    return conn


def open_glass_catalog(db_path=":memory:", json_path=None, check_same_thread=False):
    """Open a glass-catalog connection, building the keyed table from committed JSON.

    The DB is ALWAYS built from the committed normalized-LF JSON (the binary
    ``.db`` is never committed — §8). The JSON path resolves via a package-relative
    anchor. ``check_same_thread=False`` mirrors ``open_catalog`` (the runtime path
    is READ-ONLY and may be used from an async MCP worker thread).

    ONE-SHOT: each call builds the ``glass`` table on a FRESH connection. The
    default ``:memory:`` path gives a fresh table every open; reusing the SAME
    persistent ``db_path`` raises a clear ``RuntimeError`` (GLASS-3) because the
    table already exists — open a fresh connection (a new path or ``:memory:``) per
    catalog. No persistent-reuse / version-reconcile path is supported (YAGNI).

    Returns a ``sqlite3.Connection``. The caller owns closing it.
    """
    if json_path is None:
        json_path = GLASS_CATALOG_JSON
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    try:
        build_glass_db(json_path, conn)
    except Exception:
        # build_glass_db RAISES (one-shot guard) on a pre-existing ``glass``
        # table BEFORE we can return ``conn`` — the just-opened connection would
        # leak (on Windows it can retain a file handle). The raise path must own
        # the conn it opened: close it, then re-raise unchanged.
        conn.close()
        raise
    return conn
