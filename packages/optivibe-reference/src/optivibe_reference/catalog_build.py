"""catalog_build.py — build the MeritOperandType operand catalog (JSON + DB).

The catalog has THREE artifacts and this module owns the pipeline between them:

1. ``build_catalog_json`` — turn the two LIVE PROBE captures
   (``operand_inventory_438.json`` = the 438-code primary-key set, and
   ``probe_operands_capture.json`` = the per-operand cell-layout battery) into a
   normalized catalog dict (one row per code, schema §2).
2. ``write_catalog_json`` — persist that dict as a committed, normalized-LF
   (``newline="\n"``), ``indent=2`` JSON under ``data/operand_catalog.json``.
   Committed as TEXT so the forbidden-token scanner sees it (the binary ``.db``
   is non-reproducible and is NEVER committed — §2 / §7).
3. ``build_db`` / ``open_catalog`` — build the SQLite keyed ``operand`` table +
   external-content FTS5 ``operand_fts`` from the committed JSON, at TEST/RUNTIME.

Provenance discipline (§4): Step-1 live probe RESOLVED NEGATIVE on engine labels
(``op.TypeName`` / ``op.RowTypeName`` return only the CODE, no prose), so
``description_source='live_label'`` is NEVER available for a meaning. All 438 rows
start with ``description=None`` / ``description_source='authored'`` /
``description_pending=1`` pending the manual/authored ingest. The 4 hand-authored
probe-literal strings (DIST/REAY/REAX/EFFL) must NEVER be tagged ``live_label``.

The .NET enum INTEGER value is version-unstable and is NEVER persisted — only the
code NAME string keys (§2).
"""
import json
import os
import re
import sqlite3
import sys

from .manual_build import normalize as _normalize_text


class BuildContractError(AssertionError):
    """A production BUILD-PATH contract gate failed (F5 — survives ``python -O``).

    Subclasses ``AssertionError`` deliberately: the ``assert`` STATEMENT is stripped
    under ``python -O`` / ``PYTHONOPTIMIZE`` (re-opening every §3-matrix / provenance /
    coverage fail-loud hole at once — external F5), but a ``raise`` statement is
    NEVER stripped, so a build-path contract violation still HARD-ERRORS under ``-O``.
    Subclassing ``AssertionError`` keeps the many existing ``pytest.raises(AssertionError)``
    contract pins green while giving the gate an explicit, named build-error class.
    A pure internal-consistency invariant that can NEVER fire on user/committed input
    (e.g. the field-disjointness assert over module constants) stays a plain ``assert``.
    """


def _require(cond, msg):
    """Raise ``BuildContractError(msg)`` unless ``cond`` — the F5 assert→raise helper.

    Use for every BUILD-PATH contract gate over user/committed data so the check
    survives ``python -O``; keep a genuinely-internal (can't-fire-on-input) sanity
    check as a plain ``assert``.
    """
    if not cond:
        raise BuildContractError(msg)


# The provenance source tags allowed on a catalog row (§2). ``live_label`` is in
# the SET but Step-1 proved it is never *available* for a meaning — a guard
# asserts no row actually carries it. ``manual_verbatim`` is the honest
# tag for the gitignored, locally-built verbatim descriptions merged from the raw
# manual extract; a build assert forbids it in ANY COMMITTED JSON (R6/R7).
ALLOWED_DESCRIPTION_SOURCES = ("live_label", "chm", "authored", "manual_verbatim")
ALLOWED_UNITS_SOURCES = ("chm", "authored")  # NEVER 'live_label' (§2: no live unit attr)

# The operand-semantics §3/§4 closed vocabularies. Imported by the §9 tests so
# the enum sets have a single source of truth. ``None`` is permitted alongside
# every set (honest-unknown) but is NOT a member token.
ALLOWED_UNITS = (
    "lens_units",
    "inverse_lens_units",
    "waves",
    "degrees",
    "radians",
    "percent",
    "dimensionless",
)  # §3 closed units vocab (7 tokens + null)
ALLOWED_SIGN_CONVENTIONS = (
    "boundary_ge",
    "boundary_le",
    "equality",
    "minimize",
    "maximize",
    "measurement",
)  # §4 closed sign enum (6 values + null)
ALLOWED_SIGN_SOURCES = ("suffix", "family", "manual")  # §4 source set (+ null); NEVER live_label
# A directional sign (one of these) REQUIRES a non-null sign_convention_source
# (§7 guard 2). ``measurement`` / ``null`` may be sourceless.
_DIRECTIONAL_SIGNS = ("boundary_ge", "boundary_le", "equality", "minimize", "maximize")

# §4 source⇔sign COMPATIBILITY table: each provenance source may only carry the
# signs the rule that source represents can produce. ``suffix`` is the mechanical
# 2-letter rule (GT/LT/VA). ``family`` is an MN*/MX* + oracle keyword (ge/le only).
# ``manual`` is the cited minimize/maximize overlay. ``measurement`` and a null
# sign are sourceless (source MUST be None — enforced separately). A
# ``{boundary_ge, manual}`` or ``{measurement, suffix}`` row is a mistagged
# direction and MUST be rejected (defense in depth — the catalog build is the
# authoritative gate; the generator self-validates the same table pre-write).
SIGN_SOURCE_COMPATIBILITY = {
    "suffix": frozenset({"boundary_ge", "boundary_le", "equality"}),
    "family": frozenset({"boundary_ge", "boundary_le"}),
    "manual": frozenset({"minimize", "maximize"}),
}

# Suffix-collision carve-out (semantics §4). These codes END in
# GT/LT but are NOT boundary operands (LOGT=log transform, FCGT=tangential field
# curvature, NSLT=non-sequential LightningTrace). This frozenset is the SINGLE
# canonical definition — ``scripts/build_operand_semantics.py`` imports it (no
# duplicate literal, no drift hazard). The §7.4 contradiction guard SKIPS these so
# LOGT's incidental log-domain "less than ... zero" wording cannot false-positive
# the assert. The guard still BITES a genuine ``*GT`` boundary whose oracle says
# "less than the target" — only these 3 are excused.
SUFFIX_COLLISIONS = frozenset({"LOGT", "FCGT", "NSLT"})
# Back-compat alias for existing internal references / tests.
_SUFFIX_COLLISIONS = SUFFIX_COLLISIONS

# The 4 in-repo "descriptions" are HAND-AUTHORED probe-script literals, NOT
# engine-harvested (§4). No row may tag these ``live_label``. Kept here as
# the build-guard reference set; for this increment all descriptions are null so
# none is tagged anything but ``authored``.
PROBE_LITERAL_CODES = ("DIST", "REAY", "REAX", "EFFL")

# The current catalog schema version (§2: additive growth). Bumped 1->2 by the
# operand-semantics cycle (adds units/units_source + sign_convention/sign_convention_source).
# Bumped 2->3 by the tolerance cycle (adds the 3 tolerance-safety columns
# category/precondition_class/run_verdict — NULL for every merit row, real values
# only on the separate tolerance_operand catalog built by tolerance_build).
SCHEMA_VERSION = 3

# Cols 2..9 are the operand-specific PARAM cells; cols 1 ("Type") and 10..13
# (Target/Weight/Value/% Contrib, fixed across all operands) are NOT params.
_PARAM_COL_MIN = 2
_PARAM_COL_MAX = 9

# Path anchors (package-relative, NOT cwd-relative — agent cwd resets §5).
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")
CATALOG_JSON_PATH = os.path.join(DATA_DIR, "operand_catalog.json")
# Committed SYNONYMS + units source of truth (§1): one key per enriched
# merit code (the keyset IS the expected-enriched set — M1). Carries
# ``synonyms``/``units``/``units_source`` ONLY (description/citation/source are
# verbatim-local + gitignored). ``schema_version`` MUST be 1 (asserted).
SYNONYMS_JSON_PATH = os.path.join(DATA_DIR, "operand_synonyms.json")
# The GITIGNORED, user-built VERBATIM raw operand extract (the manual oracle),
# emitted by ``scripts/build_manual_corpus.py --emit-raw``. Reshaped into the
# ``description``/``citation_handle``/``description_source='manual_verbatim'``
# merge fields at build time. ABSENT on a fresh install -> synonyms-only build.
RAW_DESCRIPTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(_HERE)),
    "scripts", "captures", "operand_raw_descriptions.json",
)
# Committed, regenerable deterministic sign-convention artifact (semantics §1) emitted
# by ``scripts/build_operand_semantics.py``. Merged into the catalog at build time.
SEMANTICS_JSON_PATH = os.path.join(DATA_DIR, "operand_semantics.json")

# The synonyms-envelope schema version (§1). An unknown version is a HARD
# ERROR (loud) — the build refuses to merge a synonyms file it cannot understand.
SYNONYMS_SCHEMA_VERSION = 1

# The verbatim-derived fields that NEVER appear in a committed synonyms file (they
# are gitignored verbatim-local, §1). A committed-JSON assert enforces this.
_SHIP_FORBIDDEN_FIELDS = ("description", "citation_handle", "description_source")

# Field ownership for the disjoint by-field merge (§2.2): the synonyms file
# owns synonyms/units (+ tolerance safety fields); the local descriptions own the
# verbatim fields. Asserted disjoint so a conflicting ``units`` collision is
# structurally impossible.
_SYNONYM_FIELD_NAMES = frozenset(
    {"synonyms", "units", "units_source",
     "category", "precondition_class", "run_verdict"}
)
_LOCAL_DESC_FIELD_NAMES = frozenset(_SHIP_FORBIDDEN_FIELDS)


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _synonyms_to_text(synonyms):
    """Coerce a row's ``synonyms`` field to a clean space-joined FTS string.

    The committed ``operand_descriptions.json`` stores synonyms as a single
    space-joined STRING today, but the schema permits a list/tuple (e.g.
    ``["anti reflection", "AR coating"]``). A list rendered through ``str``/format
    would emit a Python repr (``['anti reflection', 'AR coating']``) and pollute
    the FTS index with brackets, quotes, and commas. This normalizes:

    - ``None`` -> ``""`` (no trailing junk);
    - ``str`` -> the SAME string byte-for-byte (current behavior is preserved);
    - ``list``/``tuple`` -> its items space-joined, each coerced to ``str``.
    """
    if synonyms is None:
        return ""
    if isinstance(synonyms, str):
        return synonyms
    if isinstance(synonyms, (list, tuple)):
        return " ".join(str(item) for item in synonyms)
    return str(synonyms)


def _oracle_boundary_dir(raw_text):
    """Classify the BOUNDARY direction documented in oracle prose, or None.

    Returns ``'ge'`` if the prose states a "greater than the target/specified"
    boundary, ``'le'`` for "less than the target/specified", else ``None``.

    Deliberately matches only the BOUNDARY-TARGET phrasing real boundary operands
    use (constrain a value vs the target/specified value), NOT an incidental
    "less than" (e.g. "...if the value is less than or equal to zero..."), so a
    non-boundary operand whose mechanical 2-letter suffix happens to be GT/LT is
    not misread as a boundary contradiction (§7.4 firewall against false
    positives). The oracle text is run-together for some entries, so the
    whitespace-stripped form is also probed.
    """
    text = re.sub(r"\s+", " ", (raw_text or "")).lower()
    squished = text.replace(" ", "")
    greater = ("greater than the target" in text or "greater than the spe" in text
               or "greaterthanthetarget" in squished or "greaterthanthespe" in squished)
    less = ("less than the target" in text or "less than the spe" in text
            or "lessthanthetarget" in squished or "lessthanthespe" in squished)
    if greater and not less:
        return "ge"
    if less and not greater:
        return "le"
    return None


def _param_cells_for(operand_entry):
    """Extract the cols 2..9 non-blank-header param cells from a probe entry.

    Returns a list of ``{"col", "header", "data_type"}`` dicts (in column order)
    for cells whose ``Header`` is non-blank. A blank header (``" "`` or ``""``)
    means that column is an unused trailing param slot for this operand and is
    dropped. Cols 1 and 10..13 are structural and never included.
    """
    cells = operand_entry.get("cells", {})
    out = []
    for col in range(_PARAM_COL_MIN, _PARAM_COL_MAX + 1):
        cell = cells.get(str(col))
        # A GetCellAt error surfaces as a string, not a dict — skip it defensively.
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


# The 3 tolerance-safety fields every committed TOLERANCE synonyms row must carry
# (a hardening finding). Merit rows never carry them.
_TOLERANCE_SAFETY_FIELDS = ("category", "precondition_class", "run_verdict")

# Per-schema committed-synonyms field ALLOWLISTS (F3 — the provenance boundary is
# schema-CLOSED, not a 3-field denylist that fails open on an unknown field). A
# committed MERIT synonyms row may carry ONLY these fields; a TOLERANCE row adds the
# 3 safety fields. ANY field outside the allowlist — an oracle ``raw_text`` carrying
# verbatim prose, or the verbatim ``description``/``citation_handle``/
# ``description_source`` fields — is a HARD ERROR (unknown/verbatim content can never
# ride a committed artifact, R6/R7). This SUBSUMES the old ``_SHIP_FORBIDDEN_FIELDS``
# denylist (those 3 are simply not in the allowlist).
_SYNONYMS_ALLOWED_FIELDS = frozenset({"synonyms", "units", "units_source"})
_TOLERANCE_SYNONYMS_ALLOWED_FIELDS = _SYNONYMS_ALLOWED_FIELDS | frozenset(
    _TOLERANCE_SAFETY_FIELDS
)


def load_synonyms_rows(synonyms_path, *, tolerance=False):
    """Load + validate a committed synonyms file; return its ``rows`` dict.

    Asserts ``schema_version == 1`` (an unknown version is a HARD ERROR — the build
    refuses to merge a file it cannot understand, §1) and that no committed
    synonyms row carries a ship-forbidden verbatim field (R6/R7 committed-JSON
    assert).

    Per-row CONTENT validation (a hardening finding — a malformed synonym
    HARD-ERRORS at build instead of silently shipping a lost synonym):

    - each ``row`` is a ``dict``;
    - ``"synonyms"`` is PRESENT and a ``str`` (``""`` is permitted — the KEY is the
      coverage signal — but a ``null``/missing/non-``str`` synonyms is a HARD ERROR,
      never silently "present");
    - the ``units`` ⇔ ``units_source`` PAIRING invariant (both present or both
      absent — a half-specified units, either an unsourced unit OR an orphaned
      source tag, is a HARD ERROR);
    - when ``tolerance`` is True, every row carries all 3 tolerance-safety fields
      (``category`` / ``precondition_class`` / ``run_verdict``).

    Returns ``{code: {synonyms, units?, units_source?, <tol safety?>}}``.
    """
    doc = _load_json(synonyms_path)
    version = doc.get("schema_version")
    _require(version == SYNONYMS_SCHEMA_VERSION, (
        f"{synonyms_path}: unknown synonyms schema_version {version!r} "
        f"(expected {SYNONYMS_SCHEMA_VERSION})"
    ))
    rows = doc.get("rows", {})
    # F1: an EMPTY keyset on a PROVIDED committed synonyms file is a broken checkout
    # (the keyset IS the expected-enriched set; zero rows would silently publish a
    # synonym-less catalog at exit 0). A HARD ERROR — the shared locus for both the
    # merit and the tolerance build (both call this loader with a real path).
    _require(isinstance(rows, dict) and len(rows) > 0, (
        f"{synonyms_path}: committed synonyms file has an EMPTY 'rows' keyset "
        "(a zero-row committed synonyms file is a broken checkout — the keyset is "
        "the expected-enriched set)"
    ))
    allowed = (
        _TOLERANCE_SYNONYMS_ALLOWED_FIELDS if tolerance else _SYNONYMS_ALLOWED_FIELDS
    )
    for code, row in rows.items():
        _require(isinstance(row, dict), (
            f"{code}: committed synonyms row is not a dict ({type(row).__name__})"
        ))
        # F3: per-schema ALLOWLIST — the provenance boundary is schema-CLOSED. ANY
        # field outside the allowlist (an oracle ``raw_text`` / a verbatim field /
        # any unknown key) is a HARD ERROR, not silently carried.
        extra = set(row) - allowed
        _require(not extra, (
            f"{code}: committed synonyms row carries field(s) {sorted(extra)} "
            f"outside the allowlist {sorted(allowed)} (verbatim/unknown content "
            "must never ride a committed artifact)"
        ))
        # synonyms KEY present + a str (empty permitted; null/non-str is a HARD ERROR).
        _require("synonyms" in row, (
            f"{code}: committed synonyms row is MISSING the 'synonyms' key "
            f"(a lost synonym must not ship silently)"
        ))
        _require(isinstance(row["synonyms"], str), (
            f"{code}: 'synonyms' must be a str (empty permitted), got "
            f"{type(row['synonyms']).__name__} {row['synonyms']!r}"
        ))
        # units ⇔ units_source pairing — VALUE-aware (A3): both PRESENT with non-null
        # values OR both ABSENT. A half-specified units (an orphaned source tag OR a
        # sourceless unit) AND a present-but-null units/units_source both HARD-ERROR.
        has_units = "units" in row
        has_units_source = "units_source" in row
        _require(has_units == has_units_source, (
            f"{code}: units/units_source must be paired — got "
            f"units={'present' if has_units else 'absent'}, "
            f"units_source={'present' if has_units_source else 'absent'}"
        ))
        if has_units:
            _require(
                row["units"] is not None and row["units_source"] is not None, (
                    f"{code}: units/units_source are present but a value is null "
                    f"(units={row['units']!r}, units_source={row['units_source']!r}) "
                    "— a null-valued units pair is a half-specified unit"
                )
            )
        if tolerance:
            for field in _TOLERANCE_SAFETY_FIELDS:
                _require(field in row, (
                    f"{code}: committed TOLERANCE synonyms row is MISSING the "
                    f"required safety field {field!r}"
                ))
    return rows


def reshape_raw_descriptions(raw):
    """Reshape the gitignored raw manual extract into the merge-field shape.

    ``raw`` = ``{code: {code, page, raw_text}}`` (the ``--emit-raw`` oracle) ->
    ``{code: {description, citation_handle, description_source}}`` where
    ``description`` is the ``normalize``-cleaned verbatim ``raw_text`` (R1: collapse
    the FACT-4 glyph-spacing whitespace before it reaches the FTS body),
    ``citation_handle='manual:p{page}'``, ``description_source='manual_verbatim'``.

    A row with an empty ``raw_text`` OR a missing ``page`` is SKIPPED (a description
    can never land without a page cite — the provenance invariant).
    """
    out = {}
    for code, entry in raw.items():
        if isinstance(entry, dict):
            raw_text = entry.get("raw_text")
            page = entry.get("page")
        else:  # tolerant of a bare-string oracle value
            raw_text, page = entry, None
        if not raw_text or page is None:
            continue
        out[code] = {
            "description": _normalize_text(raw_text),
            "citation_handle": "manual:p{}".format(page),
            "description_source": "manual_verbatim",
        }
    return out


def merge_enrichment(synonyms, local_descriptions, expected_codes):
    """Merge synonyms + (optional) verbatim-local descriptions into per-code rows.

    Field-disjoint by ownership (§2.2): ``synonyms``/``units``/... come from
    ``synonyms``; ``description``/``citation_handle``/``description_source`` from
    ``local_descriptions``. ``expected_codes`` = the synonyms keyset (M1: the keyset
    IS the expected-enriched set).

    Returns ``(per_code_enrichment, report)``. Never emits fewer descriptions than
    expected without a loud report:

    - ``local_descriptions is None``     -> state ``synonyms_only`` (fresh install; NORMAL)
    - present but missing some expected  -> state ``partial``       (a REAL drop; LOUD)
    - present, covers every expected     -> state ``enriched``

    ``report = {descriptions_state, enriched:int, expected:int, missing:sorted[:20]}``.
    A code is "covered" iff ``local_descriptions`` supplies a non-null description.
    """
    per_code = {}
    covered = set()
    for code in expected_codes:
        # Copy the synonyms-owned fields (synonyms + units + tolerance safety).
        base = dict(synonyms.get(code, {}))
        if local_descriptions is not None:
            desc = local_descriptions.get(code)
            if desc is not None and desc.get("description"):
                base["description"] = desc["description"]
                base["citation_handle"] = desc["citation_handle"]
                base["description_source"] = desc.get(
                    "description_source", "manual_verbatim"
                )
                covered.add(code)
        per_code[code] = base

    expected = len(expected_codes)
    if local_descriptions is None:
        state = "synonyms_only"
        missing = []
    else:
        missing = sorted(set(expected_codes) - covered)
        state = "enriched" if not missing else "partial"

    report = {
        "descriptions_state": state,
        "enriched": len(covered),
        "expected": expected,
        "missing": missing[:20],
    }
    return per_code, report


def build_catalog_json(
    inventory_path,
    probe_capture_path,
    *,
    synonyms_path=None,
    local_descriptions_path=None,
    semantics_path=None,
):
    """Build the normalized catalog dict via the field-disjoint two-source merge.

    One row per code (schema §2). The by-field ownership (§2.2):
    ``synonyms``/``units``/``units_source`` <- the committed synonyms file ONLY;
    ``description``/``citation_handle``/``description_source`` <- the gitignored
    verbatim-local raw extract ONLY; signs <- ``operand_semantics.json`` only. A
    synonyms-only build (``local_descriptions_path=None`` — the fresh-install
    state) carries synonyms yet every row stays ``description=None`` /
    ``description_pending=1`` (honest, NOT an error). ``build_report`` is stamped
    into the top-level so the merge state is readable downstream.

    ``cell_layout`` carries the cols 2..9 param-cell list ONLY for probe-covered
    operands; un-covered operands get ``None`` (NEVER an inferred layout — §3).

    Asserts the code set is unique AND its size equals the inventory file's
    ``total_members`` (NO hardcoded 438 — §3: assert against the live count) AND
    the field-disjoint merge is structurally conflict-free.
    """
    inventory = _load_json(inventory_path)
    probe = _load_json(probe_capture_path)

    synonyms = load_synonyms_rows(synonyms_path) if synonyms_path else {}
    # The verbatim-local descriptions (gitignored). ``None`` (path absent or not
    # supplied) => synonyms-only build; present => reshape the raw extract.
    if local_descriptions_path:
        local_descriptions = reshape_raw_descriptions(
            _load_json(local_descriptions_path)
        )
    else:
        local_descriptions = None
    # Semantics §1: the deterministic sign-convention artifact (suffix/family/overlay),
    # keyed by code. Absent PATH -> empty merge (every row's sign stays None). F1: the
    # gate is on whether the FILE was PROVIDED (``semantics_path is not None``), NEVER
    # on truthiness — an empty/short PARSED semantics from a provided path is a broken
    # projection, caught by the coverage check below, not silently skipped.
    semantics_provided = semantics_path is not None
    semantics = _load_json(semantics_path) if semantics_provided else {}

    # Structural disjointness (§2.2): the two enrichment sources own DISJOINT
    # fields, so a conflicting ``units`` collision is impossible. Asserted.
    assert not (_LOCAL_DESC_FIELD_NAMES & _SYNONYM_FIELD_NAMES) - {"code"}, (
        "local-description and synonym field ownership overlap"
    )

    members = inventory["members"]
    codes = [m["code"] for m in members]
    total_members = inventory["total_members"]
    osv = inventory.get("optic_studio_version")

    # Python-layer dedupe + completeness guard (necessary, not sufficient; the SQL
    # PRIMARY KEY is the storage-layer enforcement). Assert against the file's
    # total_members, NEVER a literal. F5: an explicit raise (survives ``-O``).
    _require(len(codes) == len(set(codes)) == total_members, (
        f"code-set integrity: {len(codes)} codes, {len(set(codes))} unique, "
        f"{total_members} declared total_members"
    ))

    code_set = set(codes)

    # Semantics §7 guard 5 (PRE-MERGE, non-vacuous): every semantics key is a known
    # inventory code (no stale/renamed key survives). AND (§2.2 / F1) when a
    # semantics FILE was PROVIDED it must COVER the full inventory code set — a short
    # OR EMPTY (``{}``) parsed semantics is a broken projection (HARD ERROR, closes the
    # stale-oracle silent sign-drop). Gated on ``semantics_provided`` (is-not-None),
    # NEVER truthiness — an empty ``{}`` from a provided path now HARD-ERRORS via the
    # ``missing_sem`` check rather than silently writing 438 null signs at exit 0.
    if semantics_provided:
        stale_keys = set(semantics) - code_set
        _require(not stale_keys, (
            f"operand_semantics.json carries {len(stale_keys)} key(s) not in the "
            f"inventory code set: {sorted(stale_keys)[:5]}"
        ))
        missing_sem = code_set - set(semantics)
        _require(not missing_sem, (
            f"operand_semantics.json is SHORT — missing {len(missing_sem)} "
            f"inventory code(s): {sorted(missing_sem)[:5]} (broken projection)"
        ))

    expected_codes = set(synonyms)
    per_code, report = merge_enrichment(synonyms, local_descriptions, expected_codes)

    # a hardening finding: stamp a machine-readable semantics-presence
    # signal into build_report so a downstream caller that writes THIS returned dict
    # directly (a fixture / library primitive) has a signal that the sign columns are
    # null. ``build_catalog_json`` is a library primitive; ``main()`` is the SOLE
    # publish gate that raises on synonyms/semantics-absent — no production path
    # writes this primitive's return value directly.
    report["semantics_state"] = "present" if semantics_provided else "absent"

    cell_layout_section = probe.get("cell_layout", {})
    covered = set(cell_layout_section.get("covered", []))
    operands_probe = cell_layout_section.get("operands", {})

    rows = []
    for code in codes:
        if code in covered:
            entry = operands_probe.get(code, {})
            cell_layout = _param_cells_for(entry)
            row_type_name = entry.get("row_type_name")
        else:
            cell_layout = None
            row_type_name = None

        enrichment = per_code.get(code)
        if enrichment is not None:
            # Field-disjoint merge (§2.2). ``synonyms``/``units`` always ride (the
            # code is in the synonyms keyset); ``description``/``citation`` ride ONLY
            # when the verbatim-local extract covered the code.
            synonyms_val = enrichment.get("synonyms")
            units = enrichment.get("units")
            units_source = enrichment.get("units_source")
            description = enrichment.get("description")
            citation_handle = enrichment.get("citation_handle")
            if description is not None:
                description_source = enrichment.get(
                    "description_source", "manual_verbatim"
                )
                description_pending = 0
            else:
                # Synonyms-only row: synonyms present, description pending.
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

        # Semantics §4: deterministic sign-convention merge from operand_semantics.json
        # (suffix/family/overlay). Absent code -> None (honest unknown).
        sem = semantics.get(code, {})
        sign_convention = sem.get("sign_convention")
        sign_convention_source = sem.get("sign_convention_source")

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
                "sign_convention_source": sign_convention_source,
                "synonyms": synonyms_val,
                "cell_layout": cell_layout,
                "row_type_name": row_type_name,
                "optic_studio_version": osv,
                "schema_version": SCHEMA_VERSION,
                # Tolerance cycle (schema 2->3): the 3 tolerance-safety columns. NULL for
                # every merit row (real values only on the separate tolerance_operand
                # catalog). Carried explicitly so the committed JSON matches its own
                # schema_version=3 row shape and a schema-3-aware re-dump is byte-stable.
                "category": None,
                "precondition_class": None,
                "run_verdict": None,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "optic_studio_version": osv,
        "total_members": total_members,
        "build_report": report,
        "rows": rows,
    }


def assert_provenance_invariants(catalog):
    """Assert the §4 provenance invariants over a catalog dict.

    - every row's ``description_source`` is set AND in the allowed set;
    - NO row is tagged ``description_source='live_label'`` (Step 1 proved no
      engine label exists — it can never be load-bearing for a meaning);
    - a row whose ``description_source != 'live_label'`` and that HAS a non-null
      description must carry a non-empty ``citation_handle`` (for this increment
      descriptions are null so this is vacuously satisfied — but the invariant is
      ASSERTED, not assumed);
    - ``units_source`` is never ``live_label``.

    Raises ``AssertionError`` on any violation. Used by the build and the tests.
    """
    for row in catalog["rows"]:
        code = row["code"]
        src = row["description_source"]
        _require(src in ALLOWED_DESCRIPTION_SOURCES, f"{code}: bad description_source {src!r}")
        _require(src != "live_label", f"{code}: no row may be tagged live_label (Step 1)")
        if src != "live_label" and row["description"] is not None:
            _require(row["citation_handle"], (
                f"{code}: non-live description requires a citation_handle"
            ))
        usrc = row["units_source"]
        _require(usrc in (None,) + ALLOWED_UNITS_SOURCES, f"{code}: bad units_source {usrc!r}")
        _require(usrc != "live_label", f"{code}: units_source may never be live_label")
        # Semantics §7: sign_convention_source ∈ {None, suffix, family, manual},
        # NEVER live_label (no live sign attribute exists — probe resolve-negative).
        ssrc = row.get("sign_convention_source")
        _require(ssrc in (None,) + ALLOWED_SIGN_SOURCES, (
            f"{code}: bad sign_convention_source {ssrc!r}"
        ))
        _require(ssrc != "live_label", f"{code}: sign_convention_source may never be live_label")


def assert_semantics_invariants(catalog, oracle_path=None):
    """Assert the semantics §7 build-time guards over a catalog dict.

    Guards (fail the BUILD, not just CI, so the committed catalog can never
    regenerate malformed):

    1. **Enum closure** — every non-null ``units`` ∈ §3 vocab; every non-null
       ``sign_convention`` ∈ §4 enum; every ``sign_convention_source`` ∈ §4 source
       set; none ``live_label``.
    2. **Sourceless-direction forbidden** — a directional sign
       (``ge``/``le``/``equality``/``minimize``/``maximize``) ⇒
       ``sign_convention_source`` non-null. ``measurement``/``null`` may be sourceless.
    2b. **Source⇔sign compatibility** — each source carries only the signs its
       rule produces (suffix→{ge,le,equality}; family→{ge,le};
       manual→{minimize,maximize}); a measurement/null sign must be sourceless.
       Rejects a mistagged direction (``{boundary_ge, manual}``).
    3. **Orphan units** — ``units != null`` ⇒ ``units_source ∈ {chm, authored}``
       (an authored unit can never be sourceless).
    4. **Suffix↔oracle no-contradiction** — no ``*GT`` whose oracle prose says
       "less than" (and not "greater than"); symmetric for ``*LT``. Catches a
       mis-captured oracle. SKIPPED gracefully when the oracle is absent (mirrors
       the verbatim-overlap test's optional-oracle idiom).
    5. **Semantics-key membership** — every code carrying a non-null
       ``sign_convention`` OR ``sign_convention_source`` is a known catalog code
       (no stale entry survives after a rename). The catalog is the code set; this
       holds by construction here, but the assert pins it.

    Raises ``AssertionError`` on any violation.
    """
    catalog_codes = {row["code"] for row in catalog["rows"]}

    # Guard 4 oracle (optional): load only if a path is supplied and the file
    # exists, like the verbatim test. Absent -> guard 4 is skipped, not failed.
    oracle = None
    if oracle_path and os.path.isfile(oracle_path):
        oracle = _load_json(oracle_path)

    for row in catalog["rows"]:
        code = row["code"]
        # --- Guard 1: enum closure -------------------------------------------
        units = row.get("units")
        _require(units is None or units in ALLOWED_UNITS, (
            f"{code}: units {units!r} not in the §3 closed vocab"
        ))
        sign = row.get("sign_convention")
        _require(sign is None or sign in ALLOWED_SIGN_CONVENTIONS, (
            f"{code}: sign_convention {sign!r} not in the §4 enum"
        ))
        ssrc = row.get("sign_convention_source")
        _require(ssrc in (None,) + ALLOWED_SIGN_SOURCES, (
            f"{code}: sign_convention_source {ssrc!r} not in the §4 source set"
        ))
        _require(ssrc != "live_label", f"{code}: sign_convention_source live_label forbidden")

        # --- Guard 2: sourceless direction forbidden -------------------------
        if sign in _DIRECTIONAL_SIGNS:
            _require(ssrc is not None, (
                f"{code}: directional sign {sign!r} requires a non-null "
                f"sign_convention_source"
            ))

        # --- Guard 2b: source⇔sign COMPATIBILITY table -----------------------
        # A non-null source may only carry the signs its rule can produce
        # (suffix→{ge,le,equality}; family→{ge,le}; manual→{minimize,maximize}).
        # A {boundary_ge, manual} or {measurement, suffix} row is a mistagged
        # direction → raise. Conversely ``measurement``/null sign MUST be
        # sourceless (no provenance for "no inherent direction").
        if ssrc is not None:
            allowed = SIGN_SOURCE_COMPATIBILITY[ssrc]
            _require(sign in allowed, (
                f"{code}: sign {sign!r} incompatible with source {ssrc!r} "
                f"(allowed: {sorted(allowed)})"
            ))
        else:
            _require(sign in (None, "measurement"), (
                f"{code}: sign {sign!r} requires a non-null "
                f"sign_convention_source (only measurement/null may be sourceless)"
            ))

        # --- Guard 3: orphan units -------------------------------------------
        if units is not None:
            _require(row.get("units_source") in ALLOWED_UNITS_SOURCES, (
                f"{code}: units {units!r} present but units_source "
                f"{row.get('units_source')!r} not in {ALLOWED_UNITS_SOURCES}"
            ))

        # --- Guard 4: suffix↔oracle no-contradiction (oracle-gated) ----------
        # §7.4 catches a MIS-CAPTURED oracle: a *GT whose prose documents the
        # OPPOSITE boundary direction. We test the BOUNDARY-TARGET phrasing
        # ("less than the target/specified...") rather than any incidental
        # "less than", so a non-boundary operand whose suffix is mechanically
        # GT/LT (e.g. LOGT = log base 10, prose "...less than or equal to zero")
        # is NOT a false positive: incidental "less than" is not a boundary claim.
        # Real boundary operands phrase the direction against the target/spec, so
        # a genuine mis-capture (a *GT documented as "less than the target") still
        # trips this. ``_oracle_boundary_dir`` returns 'ge'/'le'/None.
        if oracle is not None and code not in _SUFFIX_COLLISIONS:
            entry = oracle.get(code)
            if entry is not None:
                direction = _oracle_boundary_dir(entry.get("raw_text"))
                if code[-2:] == "GT":
                    _require(direction != "le", (
                        f"{code}: *GT but oracle documents a 'less than the "
                        f"target' boundary (mis-captured oracle)"
                    ))
                if code[-2:] == "LT":
                    _require(direction != "ge", (
                        f"{code}: *LT but oracle documents a 'greater than the "
                        f"target' boundary (mis-captured oracle)"
                    ))

        # --- Guard 5: semantics-key membership -------------------------------
        if sign is not None or ssrc is not None:
            _require(code in catalog_codes, (
                f"{code}: semantics for a non-catalog code (stale entry)"
            ))


def write_catalog_json(out_path, catalog):
    """Write ``catalog`` as normalized-LF, indent=2 JSON (committed TEXT).

    ``newline="\n"`` forces LF on every platform so the committed artifact is
    byte-stable and the reproducibility hash is over THIS file (§2). Creates the
    parent dir if absent.

    ATOMIC (a hardening finding): the JSON is written to a same-dir
    ``<out_path>.tmp`` then ``os.replace``d onto the final path (atomic on Windows +
    POSIX), so a concurrent reader (e.g. an xdist worker opening the catalog) never
    sees a half-written file — it reads either the OLD complete file or the NEW
    complete file, never a partial one. A failed write leaves no ``.tmp`` behind on
    the success path; on an exception mid-write the ``.tmp`` is cleaned up.
    """
    text = json.dumps(catalog, indent=2, ensure_ascii=True)
    _atomic_write_text(out_path, text)


def _atomic_write_text(out_path, text):
    """Write ``text`` + a trailing newline to ``out_path`` ATOMICALLY (D5 / F8).

    The SHARED atomic-write primitive: the text is written to a same-dir
    ``<out_path>.tmp`` then ``os.replace``d onto the final path (atomic on Windows +
    POSIX). A concurrent reader never sees a half-written file, and a failure DURING
    the write (mid-``fh.write``) leaves the ORIGINAL ``out_path`` byte-intact (the
    ``.tmp`` is discarded) — the guarantee a direct ``open(out_path, "w")`` write
    cannot make. Reused by ``write_catalog_json`` AND the release-semantics projection
    writer (``project_release_semantics.main``) so both share ONE proven atomic path.
    Creates the parent dir if absent; normalized-LF (``newline="\n"``).
    """
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.write("\n")
        os.replace(tmp_path, out_path)  # atomic same-dir rename
    except Exception:
        # Never leave a stray partial ``.tmp`` on a failed write.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


_CREATE_OPERAND = """
CREATE TABLE operand (
    code TEXT PRIMARY KEY,
    description TEXT,
    description_source TEXT NOT NULL,
    description_pending INTEGER,
    citation_handle TEXT,
    units TEXT,
    units_source TEXT,
    sign_convention TEXT,
    sign_convention_source TEXT,
    cell_layout TEXT,
    row_type_name TEXT,
    optic_studio_version TEXT,
    schema_version INTEGER,
    category TEXT,
    precondition_class TEXT,
    run_verdict TEXT,
    body TEXT
)
"""

# external-content FTS5: the content table (``operand``) must expose EVERY FTS
# column by name (``code``, ``body``) or a query/rebuild raises
# ``no such column: T.body``. ``body`` is therefore a real (derived) storage
# column on ``operand`` — it is NOT a catalog-row schema field (§2 rows are
# unchanged); it is the searchable text FTS5 indexes. ``body`` is rebuilt from
# the content table via the FTS5 ``'rebuild'`` command after the inserts.
_CREATE_FTS = """
CREATE VIRTUAL TABLE operand_fts
USING fts5(code, body, content='operand', content_rowid='rowid')
"""


def _validate_catalog_shape(catalog, catalog_json_path):
    """Validate a loaded catalog's SHAPE before ``build_db`` touches it (F2).

    Raises ``ValueError`` (a member of the server degrade set) on a present-but-
    WRONG-SHAPE or ZERO-ROW catalog so ``_safe_open_catalog`` degrades it to
    ``operand_catalog_unavailable`` instead of letting ``build_db`` crash the
    dispatcher's ``__init__`` with a raw ``KeyError``/``TypeError``/``AttributeError``
    (finding 10 was BYTE-shaped only; a valid-JSON wrong-SHAPE — ``{}`` / ``[]`` /
    ``{"schema_version":3}`` / ``{"rows":"str"}`` — slipped past it). A ZERO-ROW
    (``{"rows":[]}``) catalog is a corruption too (existence ≠ readiness): a zero-row
    connection would answer ``operand_unknown``, masking an unbuilt catalog — so it
    degrades to unavailable, NOT a silently-empty catalog.
    """
    if not isinstance(catalog, dict):
        raise ValueError(
            f"{catalog_json_path}: catalog root is not a JSON object "
            f"({type(catalog).__name__}) — degrading"
        )
    rows = catalog.get("rows")
    if not isinstance(rows, list):
        raise ValueError(
            f"{catalog_json_path}: catalog 'rows' is not a list "
            f"({type(rows).__name__}) — degrading"
        )
    if len(rows) == 0:
        raise ValueError(
            f"{catalog_json_path}: catalog has ZERO rows (existence != readiness) "
            "— degrading to operand_catalog_unavailable"
        )


def build_db(catalog_json_path, conn):
    """Build the keyed ``operand`` table + external-content FTS5 from the JSON.

    Creates the schema (§2), inserts every row (the SQL ``code TEXT PRIMARY KEY``
    raises ``IntegrityError`` on a duplicate — the storage-layer dedupe), then
    rebuilds ``operand_fts`` from the content table. The FTS ``body`` is
    ``code + ' ' + (synonyms or '')`` — the DESCRIPTION is DELIBERATELY EXCLUDED
    from the searchable body (the verbatim-local build amendment-1 A1). This makes the enriched and
    the fresh-install (synonyms-only) states share ONE ranking corpus
    (``code + synonyms``) by construction — a MATCH ranks identically whether or not
    the user built the verbatim oracle (the state-parity win), and a whitespace-free
    glyph-run in a verbatim description can never reach the tokenized index. The
    ``description`` STAYS stored in the ``operand.description`` column and is returned
    verbatim by ``lookup_operand`` (its DISPLAY value is untouched — only its ranking
    contribution is removed).

    ``cell_layout`` is stored as a JSON string (or NULL). Returns ``conn``.
    """
    catalog = _load_json(catalog_json_path)
    # F2: validate SHAPE + non-zero rows BEFORE touching the DB, so a present-but-
    # wrong-shape / zero-row catalog raises a degrade-classified ValueError (caught by
    # _safe_open_catalog) instead of a raw KeyError/TypeError crashing __init__.
    _validate_catalog_shape(catalog, catalog_json_path)
    conn.execute(_CREATE_OPERAND)
    conn.execute(_CREATE_FTS)

    for row in catalog["rows"]:
        cell_layout = row.get("cell_layout")
        cell_layout_json = (
            json.dumps(cell_layout) if cell_layout is not None else None
        )
        description = row.get("description")
        # Coerce synonyms to a clean string BEFORE the body format so a
        # list/tuple-valued entry is space-joined (not rendered as a Python repr
        # that would pollute the FTS index with brackets/quotes/commas). A string
        # synonyms passes through byte-identical to the prior `synonyms or ""`.
        synonyms_text = _synonyms_to_text(row.get("synonyms"))
        # the verbatim-local build amendment-1 A1: the searchable FTS body is CODE + SYNONYMS ONLY.
        # The description is DELIBERATELY NOT in the body (it stays in the stored
        # `description` column below + is returned by lookup_operand). Excluding it
        # makes the enriched and synonyms-only builds share ONE ranking corpus, so
        # the ranking is identical regardless of whether the verbatim oracle was
        # built, and a glyph-run description artifact never becomes a search token.
        body = "{} {}".format(row["code"], synonyms_text).strip()
        conn.execute(
            "INSERT INTO operand (code, description, description_source, "
            "description_pending, citation_handle, units, units_source, "
            "sign_convention, sign_convention_source, cell_layout, row_type_name, "
            "optic_studio_version, schema_version, category, precondition_class, "
            "run_verdict, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["code"],
                description,
                row["description_source"],
                row.get("description_pending"),
                row.get("citation_handle"),
                row.get("units"),
                row.get("units_source"),
                row.get("sign_convention"),
                row.get("sign_convention_source"),
                cell_layout_json,
                row.get("row_type_name"),
                row.get("optic_studio_version"),
                row.get("schema_version"),
                # Tolerance cycle: the 3 tolerance-safety columns. NULL for every merit row
                # (operand_descriptions.json / operand_semantics.json never supply
                # them) — real values only on the separate tolerance_operand catalog.
                row.get("category"),
                row.get("precondition_class"),
                row.get("run_verdict"),
                body,
            ),
        )
    # Rebuild the external-content FTS index from the content table (`operand`):
    # FTS5 reads `code` + `body` per rowid. This is the locked "rebuild
    # operand_fts from content" step — one rebuild, not per-row mirroring.
    conn.execute("INSERT INTO operand_fts(operand_fts) VALUES('rebuild')")
    conn.commit()
    return conn


def open_catalog(db_path_or_memory=":memory:", catalog_json_path=None):
    """Open a catalog connection, building the schema from the committed JSON.

    The DB is ALWAYS built from the committed normalized-LF JSON (the binary
    ``.db`` is never committed — §2). The JSON path resolves via a
    package-relative anchor (NOT cwd-relative — the agent cwd resets between
    calls, §5). ``db_path_or_memory`` defaults to an in-memory DB.

    Returns a ``sqlite3.Connection``. The caller owns closing it.
    """
    if catalog_json_path is None:
        catalog_json_path = CATALOG_JSON_PATH
    # check_same_thread=False: the catalog connection is READ-ONLY at runtime and
    # may be used from an async MCP worker thread (a different thread than the one
    # that opened it). sqlite3 otherwise raises ProgrammingError on cross-thread
    # use. Safe here because the runtime path issues no writes (H-2).
    conn = sqlite3.connect(db_path_or_memory, check_same_thread=False)
    build_db(catalog_json_path, conn)
    return conn


def main():
    """Build the (gitignored, user-built) catalog JSON — fail-loud (§3).

    Returns an exit code: 0 for ``enriched``/``synonyms_only`` (the ONE exit-0
    degraded state, LOUDLY marked), nonzero for ``partial`` (a REAL drop). RAISES
    on synonyms-absent (committed -> broken checkout) or semantics-absent (committed
    -> broken/projection error) — see the §3 matrix.
    """
    captures = os.path.join(
        os.path.dirname(os.path.dirname(_HERE)), "scripts", "captures"
    )
    inventory_path = os.path.join(captures, "operand_inventory_438.json")
    probe_path = os.path.join(captures, "probe_operands_capture.json")

    # S committed -> a missing synonyms file is a BROKEN checkout: HARD ERROR.
    if not os.path.isfile(SYNONYMS_JSON_PATH):
        raise FileNotFoundError(
            f"operand_synonyms.json is committed but ABSENT ({SYNONYMS_JSON_PATH}) "
            "— broken checkout"
        )
    # M committed -> a missing semantics file is a BROKEN checkout / projection
    # error: HARD ERROR.
    if not os.path.isfile(SEMANTICS_JSON_PATH):
        raise FileNotFoundError(
            f"operand_semantics.json is committed but ABSENT ({SEMANTICS_JSON_PATH}) "
            "— broken checkout / projection error"
        )

    # The gitignored verbatim-local descriptions. Forced OFF by
    # OPTIVIBE_NO_LOCAL_DESCRIPTIONS=1 (the fresh-install reproduction, §2.8/§6);
    # otherwise merged when the raw oracle is present, synonyms-only when absent.
    if os.environ.get("OPTIVIBE_NO_LOCAL_DESCRIPTIONS"):
        local_descriptions_path = None
    elif os.path.isfile(RAW_DESCRIPTIONS_PATH):
        local_descriptions_path = RAW_DESCRIPTIONS_PATH
    else:
        local_descriptions_path = None

    catalog = build_catalog_json(
        inventory_path,
        probe_path,
        synonyms_path=SYNONYMS_JSON_PATH,
        local_descriptions_path=local_descriptions_path,
        semantics_path=SEMANTICS_JSON_PATH,
    )
    # §7 guards fail the BUILD so the catalog can never regenerate malformed. The
    # semantics guard accepts the oracle path for guard 4 and skips gracefully if
    # the oracle (gitignored) is absent.
    assert_provenance_invariants(catalog)
    assert_semantics_invariants(catalog, oracle_path=RAW_DESCRIPTIONS_PATH)

    report = catalog["build_report"]
    state = report["descriptions_state"]

    # PARTIAL is a REAL drop: REFUSE-and-report (do NOT write a lossy catalog);
    # name the missing codes; exit nonzero.
    if state == "partial":
        print(
            "WARN: operand catalog build is PARTIAL — {}/{} descriptions merged, "
            "missing {}: {}".format(
                report["enriched"], report["expected"],
                report["expected"] - report["enriched"], report["missing"],
            ),
            file=sys.stderr,
        )
        print(
            "REFUSED: not writing a lossy catalog; run "
            "scripts/build_manual_corpus.py --emit-raw (see PROVENANCE.md)",
            file=sys.stderr,
        )
        return 2

    write_catalog_json(CATALOG_JSON_PATH, catalog)
    print("wrote {} rows -> {}".format(len(catalog["rows"]), CATALOG_JSON_PATH))
    print("build_report: {}".format(json.dumps(report)))

    # SYNONYMS_ONLY is the ONE exit-0 degraded state — LOUDLY marked.
    if state == "synonyms_only":
        print(
            "WARN: operand catalog built SYNONYMS-ONLY "
            "(descriptions_state=synonyms_only) — every description_pending=1; run "
            "scripts/build_manual_corpus.py --emit-raw to enrich (see PROVENANCE.md)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
