"""tolerance_build.py — build the ToleranceOperandType catalog (JSON + DB).

The tolerance analog of ``catalog_build.py``. It mirrors the 3-artifact
pipeline (live-probe captures -> committed normalized JSON -> runtime SQLite +
FTS5), but the tolerance catalog is a PHYSICALLY SEPARATE table/connection from the
merit ``operand`` catalog because three codes (``TTHI``/``TRAD``/``TCUR``) collide
on the ``code`` PRIMARY KEY with DIFFERENT meanings across the two enums. The
``domain``-selects-the-connection model on ``lookup_operand`` is the structural
consequence (§0/§2).

KEY DIFFERENCES from the merit build (§3):

- It does NOT call ``assert_semantics_invariants`` — the merit ``*GT``/``*LT``/``*VA``
  suffix-sign rules + SUFFIX_COLLISIONS are merit-only and meaningless here. A
  tolerance bound is a symmetric +/- perturbation window, never a directional
  boundary, so ``sign_convention`` is ``measurement`` (for the operands that bite a
  quantity) or ``None`` (control/inert), and ``sign_convention_source`` is ALWAYS
  None (no suffix/family/overlay provenance exists for tolerance).
- It carries 3 additive tolerance-safety columns (``category`` /
  ``precondition_class`` / ``run_verdict``) authored for EVERY row, including the
  ``description_pending`` control/ISO rows — so the agent is warned about crash-class
  TNPA/TNMA and nsc TNPS even without a paraphrase.
- ``units`` adds the tolerance-only token ``fringes`` (1 fringe = lambda/2) on top of
  the FROZEN merit closed set.
- The cell-layout param block is cols 2..4 (``_TOL_PARAM_COL_MIN``/``MAX``),
  distinct from the merit cols 2..9 — a tolerance row's trailing block is the FIXED
  Nominal/Min/Max/Comment quartet, documented in the description, not stored as a
  param cell.

The provenance discipline + the byte-stable committed-JSON idiom + the shared
``catalog_build.build_db`` (which now binds the 3 columns from the JSON rows) are
REUSED — DRY across the two catalogs.
"""
import json
import os
import sys

from . import catalog_build

# Path anchors (package-relative, NOT cwd-relative — agent cwd resets, §5).
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
TOLERANCE_CATALOG_JSON_PATH = os.path.join(DATA_DIR, "tolerance_operand_catalog.json")
# Committed SYNONYMS + units + the 3 safety fields (§1): one key per
# enriched tolerance code (the 39). ``schema_version`` MUST be 1 (asserted).
TOLERANCE_SYNONYMS_JSON_PATH = os.path.join(
    DATA_DIR, "tolerance_operand_synonyms.json"
)

# Capture anchors (the live probe truth — assert against the FILE, never a literal).
_CAPTURES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(_HERE)), "scripts", "captures"
)
TOLERANCE_INVENTORY_PATH = os.path.join(_CAPTURES_DIR, "tolerance_inventory_62.json")
TOLERANCE_CAPTURE_PATH = os.path.join(_CAPTURES_DIR, "probe_tolerances_capture.json")
# The GITIGNORED, user-built VERBATIM tolerance raw extract (the manual oracle),
# emitted by ``scripts/build_manual_corpus.py --emit-raw`` from the NEW
# heading-delimited §7.2.1 pairer. Reshaped into the verbatim merge fields at
# build time; ABSENT on a fresh install -> synonyms-only tolerance build.
TOLERANCE_RAW_DESCRIPTIONS_PATH = os.path.join(
    _CAPTURES_DIR, "tolerance_raw_descriptions.json"
)

# The catalog schema version is the SHARED merit schema (catalog_build.SCHEMA_VERSION
# == 3 after the tolerance bump that added the 3 tolerance-safety columns). The
# tolerance catalog rides the same build_db, so it carries the same schema_version.
SCHEMA_VERSION = catalog_build.SCHEMA_VERSION

# --- closed vocabularies (single source of truth here, §3) -----------------
# The merit closed units set is FROZEN; ``fringes`` is a TOLERANCE-ONLY addition
# (1 fringe = lambda/2; an irregularity tolerance is quoted in fringes, NOT waves).
ALLOWED_TOLERANCE_UNITS = catalog_build.ALLOWED_UNITS + ("fringes",)

# A tolerance bound is a symmetric +/- window, not a directional boundary, so the
# only sign tokens are ``measurement`` (the op bites a quantity) and None.
ALLOWED_TOLERANCE_SIGN_CONVENTIONS = ("measurement", None)

ALLOWED_PRECONDITION_CLASS = ("tde_native", "cb_required", "nsc", "crash", "control")
ALLOWED_RUN_VERDICT = ("bites", "inert", "needs_precondition", "crashes_run")
ALLOWED_TOLERANCE_CATEGORY = (
    "surface",
    "element",
    "index",
    "irregularity",
    "parameter",
    "mechanical_tilt",
    "mechanical_decenter",
    "tir_roll",
    "compensator",
    "control",
    "iso",
)

# The param block on a tolerance row is cols 2..4 (Param1/Param2/Param3); cols 0/1
# always raise and cols 5..8 are the FIXED Nominal/Min/Max/Comment quartet — distinct
# from the merit cols 2..9 (§3).
_TOL_PARAM_COL_MIN = 2
_TOL_PARAM_COL_MAX = 4

# --- the harness-families run-verdict SETS (the source of truth, §3/§4) -----
# These are CODE sets (locally authored) cross-referenced from the
# harness-sweep tables. They are the authoritative tag source for EVERY row — the
# build does NOT hand-tag run_verdict per row, it derives it from these sets so the
# cross-consistency invariant (assert_tolerance_invariants guard 5) holds by
# construction.

# Need a Coordinate-Break surface in the LDE to bite (element pivot-tilt / mech
# decenter family). precondition_class='cb_required', run_verdict='needs_precondition'.
_CB_REQUIRED = frozenset({"TUDX", "TUDY", "TUTX", "TUTY", "TUTZ"})
# Crash the headless RUN (NSC precondition; IPC RemotingException). 'crash' /
# 'crashes_run'.
_CRASH = frozenset({"TNPA", "TNMA"})
# Needs a Non-Sequential surface; errors cleanly (does not crash). 'nsc' /
# 'needs_precondition'.
_NSC = frozenset({"TNPS"})
# Perturb the criterion directly on a simple sequential lens. 'tde_native' / 'bites'.
_BITES = frozenset(
    {
        "TRAD", "TCUR", "TFRN", "TTHI", "TCON", "TIND", "TABB",
        "TSDX", "TSDY", "TSDR", "TSTX", "TSTY",
        "TIRX", "TIRY", "TIRR",
        "TEDX", "TEDY", "TEDR", "TETX", "TETY",
        "TEXI", "TEZI",
        "TRLX", "TRLY", "TRLR", "TARX", "TARY", "TARR",
        "ISOA", "ISOB", "ISOC", "ISOD",
    }
)
# Authorable but inert on a simple lens (needs the right DOF). 'tde_native' / 'inert'.
_INERT = frozenset(
    {"TSDI", "TPAR", "TPAI", "TCMU", "TCIO", "TCEO", "TETZ"}
)
# Control / compensator / housekeeping operands — no sensitivity row by design.
# 'control' / 'inert'.
_CONTROL = frozenset(
    {
        "COMP", "CPAR", "CEDV", "CMCO", "CNPA", "CNPS", "COMM", "MPVT",
        "SAVE", "SEED", "STAT", "TEDV", "TMCO", "TOFF", "TWAV",
    }
)


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _verdict_for(code):
    """Derive (precondition_class, run_verdict) for ``code`` from the harness SETS.

    The SETS (cross-referenced from the probe findings) are the authoritative source.
    Every one of the 62 codes lands in exactly one bucket — a code reaching the
    fallthrough is a build-time error (a new enum member that was never bucketed).
    """
    if code in _CRASH:
        return "crash", "crashes_run"
    if code in _CB_REQUIRED:
        return "cb_required", "needs_precondition"
    if code in _NSC:
        return "nsc", "needs_precondition"
    if code in _BITES:
        return "tde_native", "bites"
    if code in _INERT:
        return "tde_native", "inert"
    if code in _CONTROL:
        return "control", "inert"
    raise ValueError(
        f"tolerance code {code!r} is not in any harness run-verdict bucket — "
        "a new enum member must be assigned a bucket in tolerance_build"
    )


def _default_category(code):
    """A closed-enum ``category`` for a row WITHOUT an authored description.

    Authored rows carry their own ``category`` in the descriptions file; the
    pending control/compensator/ISO rows fall back to one of these closed tokens so
    EVERY row carries a non-null category (§4). Membership is derived from the same
    harness SETS — no per-code hand-literal.
    """
    if code.startswith("ISO"):
        return "iso"
    if code in _CONTROL:
        # Compensator family (COMP/CPAR/CEDV/CMCO/CNPA/CNPS) vs pure controls.
        if code in ("COMP", "CPAR", "CEDV", "CMCO", "CNPA", "CNPS"):
            return "compensator"
        return "control"
    # Crash/nsc NSC operands and the remaining inert/bites codes that lack an
    # authored description: bucket by their mechanism where determinable, else
    # 'parameter' (a generic surface/parameter tolerance) as the honest default.
    return "parameter"


def _param_cells_for_tolerance(operand_entry):
    """Extract the cols 2..4 non-blank-header param cells from a probe entry.

    Mirrors ``catalog_build._param_cells_for`` but over the tolerance param range
    (cols 2..4 — Param1/Param2/Param3) and the tolerance capture's ``cells`` dict.
    A blank header means that column is an unused param slot for this operand and is
    dropped. Cols 0/1 always raise; cols 5..8 are the FIXED trailing block and are
    NEVER param cells (documented in the description, §3).
    """
    cells = operand_entry.get("cells", {})
    out = []
    for col in range(_TOL_PARAM_COL_MIN, _TOL_PARAM_COL_MAX + 1):
        cell = cells.get(str(col))
        # A GetCellAt error surfaces as a string, not a dict — skip defensively.
        if not isinstance(cell, dict):
            continue
        header = cell.get("Header", "")
        if header is None or header.strip() == "":
            continue
        out.append(
            {
                "col": cell.get("Col", col),
                "header": header.strip(),
                "data_type": cell.get("DataType"),
            }
        )
    return out


def _sign_convention_for(code, run_verdict):
    """A tolerance row's ``sign_convention``: measurement (bites a quantity) or None.

    The bites/inert-but-tolerable operands measure a perturbed criterion, so they
    carry ``measurement``; the pure control/housekeeping operands (run_verdict
    inert AND control class) and the crash/nsc rows that never measure a clean
    quantity carry None. ``sign_convention_source`` is ALWAYS None (§3).
    """
    if code in _CONTROL or code in _CRASH or code in _NSC:
        return None
    return "measurement"


def build_tolerance_catalog_json(
    inventory_path,
    capture_path,
    *,
    synonyms_path=None,
    local_descriptions_path=None,
):
    """Build the normalized tolerance-catalog dict via the field-disjoint merge.

    Reuses ``catalog_build.merge_enrichment`` (§2.3): ``synonyms``/``units``
    + the 3 safety fields <- the committed tolerance synonyms file; ``description``/
    ``citation_handle``/``description_source`` <- the gitignored verbatim-local
    tolerance raw extract. One row per code (the merit row schema, so
    ``catalog_build.build_db`` consumes it unchanged), PLUS the 3 tolerance-safety
    fields on EVERY row. A code NOT in the synonyms keyset (the 23 legitimately
    pending) stays ``description=None`` / ``description_pending=1`` but STILL carries
    category + precondition_class + run_verdict via the derived fallbacks (§4).

    ``build_report`` is stamped into the top-level (the merge state, readable
    downstream). Asserts the code set is unique AND equals the inventory file's
    ``total_members`` AND its declared SET (NO hardcoded 62 — §3: assert vs the FILE).
    """
    inventory = _load_json(inventory_path)
    capture = _load_json(capture_path)

    synonyms = (
        catalog_build.load_synonyms_rows(synonyms_path, tolerance=True)
        if synonyms_path else {}
    )
    if local_descriptions_path:
        local_descriptions = catalog_build.reshape_raw_descriptions(
            _load_json(local_descriptions_path)
        )
    else:
        local_descriptions = None

    members = inventory["members"]
    codes = [m["code"] for m in members]
    total_members = inventory["total_members"]
    osv = inventory.get("optic_studio_version")

    # Member-count + set integrity vs the FILE (never a literal). F5: explicit raise
    # (survives ``-O``) via the shared catalog_build contract-gate helper.
    catalog_build._require(len(codes) == len(set(codes)) == total_members, (
        f"tolerance code-set integrity: {len(codes)} codes, "
        f"{len(set(codes))} unique, {total_members} declared total_members"
    ))

    expected_codes = set(synonyms)
    per_code, report = catalog_build.merge_enrichment(
        synonyms, local_descriptions, expected_codes
    )

    cell_layout_section = capture.get("cell_layout", {})
    covered = set(cell_layout_section.get("covered", []))
    operands_probe = cell_layout_section.get("operands", {})

    rows = []
    for code in codes:
        if code in covered:
            entry = operands_probe.get(code, {})
            cell_layout = _param_cells_for_tolerance(entry)
            row_type_name = entry.get("type_name")
        else:
            cell_layout = None
            row_type_name = None

        # Derived safety fallbacks (kept so a synonyms-only / pending row still
        # carries them — §4). An authored (synonyms-file) value overrides below.
        precondition_class, run_verdict = _verdict_for(code)
        category = _default_category(code)

        enrichment = per_code.get(code)
        if enrichment is not None:
            synonyms_val = enrichment.get("synonyms")
            units = enrichment.get("units")
            units_source = enrichment.get("units_source")
            # An authored entry MAY carry its own category / verdict tags (from the
            # synonyms file). Prefer the authored value when present (kept in sync
            # with the SETS — assert_tolerance_invariants guard 5 reddens a mismatch).
            category = enrichment.get("category", category)
            precondition_class = enrichment.get(
                "precondition_class", precondition_class
            )
            run_verdict = enrichment.get("run_verdict", run_verdict)
            # description/citation ride ONLY when the verbatim-local extract covered
            # the code (§2.3 field-disjoint merge).
            description = enrichment.get("description")
            citation_handle = enrichment.get("citation_handle")
            if description is not None:
                description_source = enrichment.get(
                    "description_source", "manual_verbatim"
                )
                description_pending = 0
            else:
                description_source = "authored"
                description_pending = 1
        else:
            synonyms_val = None
            units = None
            units_source = None
            description = None
            citation_handle = None
            description_source = "authored"
            description_pending = 1

        sign_convention = _sign_convention_for(code, run_verdict)

        rows.append(
            {
                "code": code,
                "description": description,
                "description_source": description_source,
                "description_pending": description_pending,
                "citation_handle": citation_handle,
                "units": units,
                "units_source": units_source,
                "sign_convention": sign_convention,
                # ALWAYS None for tolerance (§3): no suffix/family/overlay provenance.
                "sign_convention_source": None,
                "synonyms": synonyms_val,
                "cell_layout": cell_layout,
                "row_type_name": row_type_name,
                "optic_studio_version": osv,
                "schema_version": SCHEMA_VERSION,
                # The 3 tolerance-safety columns (authored for EVERY row, §4).
                "category": category,
                "precondition_class": precondition_class,
                "run_verdict": run_verdict,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "optic_studio_version": osv,
        "total_members": total_members,
        "build_report": report,
        "rows": rows,
    }


def assert_tolerance_invariants(catalog, inventory_path):
    """Assert the §3 tolerance build-time guards over a catalog dict.

    The 6 guards (fail the BUILD so the committed catalog can never regenerate
    malformed). This does NOT call ``assert_semantics_invariants`` (merit-only
    suffix-sign rules); it DOES reuse ``assert_provenance_invariants`` for the base
    provenance (no live_label; non-null description requires a citation).
    """
    inventory = _load_json(inventory_path)
    inv_codes = {m["code"] for m in inventory["members"]}
    total_members = inventory["total_members"]

    rows = catalog["rows"]
    codes = [r["code"] for r in rows]
    code_set = set(codes)

    # Guard 1: member count + set vs the FILE (never a literal). F5: explicit raises
    # (survive ``-O``) via the shared catalog_build contract-gate helper.
    _require = catalog_build._require
    _require(len(codes) == len(code_set) == total_members, (
        f"tolerance member count: {len(codes)} codes, {len(code_set)} unique, "
        f"{total_members} declared"
    ))
    _require(code_set == inv_codes, (
        "tolerance code SET does not equal the inventory file's code set: "
        f"missing={sorted(inv_codes - code_set)[:5]} "
        f"extra={sorted(code_set - inv_codes)[:5]}"
    ))

    for row in rows:
        code = row["code"]
        # Guard 2: units closed + sourced.
        units = row.get("units")
        _require(units is None or units in ALLOWED_TOLERANCE_UNITS, (
            f"{code}: units {units!r} not in the tolerance closed vocab"
        ))
        if units is not None:
            _require(row.get("units_source") in catalog_build.ALLOWED_UNITS_SOURCES, (
                f"{code}: units {units!r} present but units_source "
                f"{row.get('units_source')!r} not sourced"
            ))
        # Guard 3: sign in {measurement, None}; source ALWAYS None.
        sign = row.get("sign_convention")
        _require(sign in ALLOWED_TOLERANCE_SIGN_CONVENTIONS, (
            f"{code}: sign_convention {sign!r} not in {{measurement, None}}"
        ))
        _require(row.get("sign_convention_source") is None, (
            f"{code}: sign_convention_source must always be None for tolerance"
        ))
        # Guard 4: precondition_class / run_verdict / category closed.
        _require(row.get("precondition_class") in ALLOWED_PRECONDITION_CLASS, (
            f"{code}: precondition_class {row.get('precondition_class')!r} not closed"
        ))
        _require(row.get("run_verdict") in ALLOWED_RUN_VERDICT, (
            f"{code}: run_verdict {row.get('run_verdict')!r} not closed"
        ))
        _require(row.get("category") in ALLOWED_TOLERANCE_CATEGORY, (
            f"{code}: category {row.get('category')!r} not closed"
        ))

    # Guard 5: cross-consistency as SETS (mis-tag on either side reddens).
    cb_set = {r["code"] for r in rows if r.get("precondition_class") == "cb_required"}
    crash_set = {r["code"] for r in rows if r.get("precondition_class") == "crash"}
    nsc_set = {r["code"] for r in rows if r.get("precondition_class") == "nsc"}
    _require(cb_set == set(_CB_REQUIRED), (
        f"cb_required set mismatch: catalog={sorted(cb_set)} "
        f"expected={sorted(_CB_REQUIRED)}"
    ))
    _require(crash_set == set(_CRASH), (
        f"crash set mismatch: catalog={sorted(crash_set)} expected={sorted(_CRASH)}"
    ))
    _require(nsc_set == set(_NSC), (
        f"nsc set mismatch: catalog={sorted(nsc_set)} expected={sorted(_NSC)}"
    ))

    # Guard 6: base provenance (reused merit invariant).
    catalog_build.assert_provenance_invariants(catalog)


def write_tolerance_catalog_json(out_path, catalog):
    """Write ``catalog`` as normalized-LF, indent=2, ASCII JSON (committed TEXT).

    Reuses the byte-stable idiom (``catalog_build.write_catalog_json``); the binary
    ``.db`` is never committed (§3/§7).
    """
    catalog_build.write_catalog_json(out_path, catalog)


def open_tolerance_catalog(db_path_or_memory=":memory:", catalog_json_path=None):
    """Open a tolerance-catalog connection, building the table from committed JSON.

    The DB is ALWAYS built from the committed normalized-LF tolerance JSON (the
    binary ``.db`` is never committed — §3). Defaults to the committed
    ``TOLERANCE_CATALOG_JSON_PATH``. Uses the SHARED ``catalog_build.build_db``
    (which now binds the 3 tolerance-safety columns), so the keyed ``operand`` table
    + ``operand_fts`` are byte-identical in shape to the merit catalog and
    ``lookup_operand`` runs on it unchanged (catalog-agnostic handler, §0).

    ``check_same_thread=False`` mirrors ``open_catalog`` (the runtime path is
    READ-ONLY and may be used from an async MCP worker thread). Returns a
    ``sqlite3.Connection``; the caller owns closing it.
    """
    import sqlite3

    if catalog_json_path is None:
        catalog_json_path = TOLERANCE_CATALOG_JSON_PATH
    conn = sqlite3.connect(db_path_or_memory, check_same_thread=False)
    catalog_build.build_db(catalog_json_path, conn)
    return conn


def main():
    """Build the (gitignored, user-built) tolerance-catalog JSON — fail-loud (§3).

    Returns an exit code: 0 for ``enriched``/``synonyms_only`` (the ONE exit-0
    degraded state, LOUDLY marked), nonzero for ``partial``. RAISES on
    synonyms-absent (committed -> broken checkout). The M2 ``synonyms_only_permanent``
    marker is NOT wired: the heading-delimited tolerance pairer SHIPS this cycle, so
    an absent raw oracle is a fresh install (``synonyms_only``), not a deferral.
    """
    # S committed -> a missing synonyms file is a BROKEN checkout: HARD ERROR.
    if not os.path.isfile(TOLERANCE_SYNONYMS_JSON_PATH):
        raise FileNotFoundError(
            f"tolerance_operand_synonyms.json is committed but ABSENT "
            f"({TOLERANCE_SYNONYMS_JSON_PATH}) — broken checkout"
        )

    if os.environ.get("OPTIVIBE_NO_LOCAL_DESCRIPTIONS"):
        local_descriptions_path = None
    elif os.path.isfile(TOLERANCE_RAW_DESCRIPTIONS_PATH):
        local_descriptions_path = TOLERANCE_RAW_DESCRIPTIONS_PATH
    else:
        local_descriptions_path = None

    catalog = build_tolerance_catalog_json(
        TOLERANCE_INVENTORY_PATH,
        TOLERANCE_CAPTURE_PATH,
        synonyms_path=TOLERANCE_SYNONYMS_JSON_PATH,
        local_descriptions_path=local_descriptions_path,
    )
    # §3 guards fail the BUILD so the catalog can never regenerate malformed.
    # assert_tolerance_invariants reuses assert_provenance_invariants.
    assert_tolerance_invariants(catalog, TOLERANCE_INVENTORY_PATH)

    report = catalog["build_report"]
    state = report["descriptions_state"]

    if state == "partial":
        print(
            "WARN: tolerance catalog build is PARTIAL — {}/{} descriptions merged, "
            "missing {}: {}".format(
                report["enriched"], report["expected"],
                report["expected"] - report["enriched"], report["missing"],
            ),
            file=sys.stderr,
        )
        print(
            "REFUSED: not writing a lossy tolerance catalog; run "
            "scripts/build_manual_corpus.py --emit-raw (see PROVENANCE.md)",
            file=sys.stderr,
        )
        return 2

    write_tolerance_catalog_json(TOLERANCE_CATALOG_JSON_PATH, catalog)
    print("wrote {} rows -> {}".format(
        len(catalog["rows"]), TOLERANCE_CATALOG_JSON_PATH))
    print("build_report: {}".format(json.dumps(report)))

    if state == "synonyms_only":
        print(
            "WARN: tolerance catalog built SYNONYMS-ONLY "
            "(descriptions_state=synonyms_only) — every description_pending=1; run "
            "scripts/build_manual_corpus.py --emit-raw to enrich (see PROVENANCE.md)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
