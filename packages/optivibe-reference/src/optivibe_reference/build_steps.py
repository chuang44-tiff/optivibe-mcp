"""build_steps.py — the glass-catalog and manual-corpus build steps, as package code.

These were the bodies of ``scripts/build_glass_catalog.py`` and
``scripts/build_manual_corpus.py``. They live in the package so an INSTALLED
optivibe-reference (no ``scripts/`` dir) can run them through ``build_data``
(``optivibe-build-data``); the two scripts are now thin wrappers with the same CLI.

* ``build_glass_catalog()`` — resolve the install Glasscat dir (``ZEMAX_GLASSCAT``
  override, else ``~/Documents/Zemax/Glasscat``), assert the ``.agf``-derivation grant
  (fail-closed), re-parse + recompute every shipped ``.agf`` record, write
  ``data/glass_catalog.json``. No raw ``.agf`` is kept; only OUR computed/parsed numbers
  + factual glass names land in the JSON.
* ``build_manual_corpus(argv)`` — resolve the licensed install-dir manual PDF
  (``OPTIVIBE_MANUAL_PDF`` else the standard install-dir glob) and build the local FTS5
  ``.db`` + the TEXT-FREE manifest. ``--emit-raw`` ALSO writes the VERBATIM raw
  operand/tolerance extracts (the manual oracle, never committed) into the captures dir.

Both print ASCII status ONLY and NEVER relay extracted manual or catalog prose (the
cp1252 ``conda run`` relay crash, probe finding 3).
"""
import glob
import json
import os
import sys

from . import _paths, glass_build, manual_build
from .glass_parse import resolve_glasscat


# --- glass catalog ----------------------------------------------------------
def build_glass_catalog():
    """Build ``data/glass_catalog.json`` from the install Glasscat dir; return 0.

    Raises ``FileNotFoundError`` when no Glasscat dir resolves (the caller's SKIP) and
    ``ProvenanceGateError`` when the derivation grant is not on file (a FAIL).
    """
    glasscat = resolve_glasscat()
    print("[glass-build] Glasscat dir: %s" % glasscat)

    catalog = glass_build.build_glass_catalog_json(
        glasscat, glass_build.PROVENANCE_PATH
    )
    glass_build.write_glass_catalog_json(glass_build.GLASS_CATALOG_JSON, catalog)

    rows = catalog["rows"]
    nd_valid = sum(1 for r in rows if r["nd_valid"])
    pg_f_valid = sum(1 for r in rows if r["pg_f_valid"])
    nd_null = sum(1 for r in rows if r["nd"] is None)
    print("[glass-build] agf_glass_count: %d" % catalog["agf_glass_count"])
    print("[glass-build] rows written:    %d" % len(rows))
    print("[glass-build] nd_valid rows:   %d" % nd_valid)
    print("[glass-build] pg_f_valid rows: %d" % pg_f_valid)
    print("[glass-build] nd-null (out-of-band) rows: %d" % nd_null)
    print("[glass-build] wrote -> %s" % glass_build.GLASS_CATALOG_JSON)
    return 0


# --- manual corpus ----------------------------------------------------------
def raw_extract_path():
    """The GITIGNORED verbatim merit-operand raw extract (the oracle)."""
    return os.path.join(_paths.captures_dir(), "operand_raw_descriptions.json")


def tol_raw_extract_path():
    """The GITIGNORED verbatim tolerance-operand raw extract (the oracle)."""
    return os.path.join(_paths.captures_dir(), "tolerance_raw_descriptions.json")


def find_manual_pdf():
    """Resolve the licensed manual PDF, or return None (genuine absence)."""
    override = os.environ.get("OPTIVIBE_MANUAL_PDF")
    if override and os.path.isfile(override):
        return override
    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        hits = glob.glob(
            os.path.join(base, "*Zemax OpticStudio*", "OpticStudio_UserManual_en.pdf")
        )
        if hits:
            return sorted(hits, reverse=True)[0]
    return None


def write_raw(path, entries, order):
    """Write ``{code: {code, page, raw_text}}`` to a GITIGNORED capture (oracle).

    NEVER prints the extracted prose — the caller prints only the entry count.

    ATOMIC (a hardening finding): the oracle is written to a same-dir
    ``<path>.tmp`` then ``os.replace``d onto the final path, so a concurrent reader
    never sees a half-written raw extract.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {code: entries[code] for code in order}
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=True)
            fh.write("\n")
        os.replace(tmp_path, path)  # atomic same-dir rename
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return payload


def emit_raw(pdf_path):
    """Extract BOTH raw operand oracles to the GITIGNORED captures (verbatim source).

    Merit: the finding-2 bbox two-column pairer over the alphabetical §5.2.1.3
    section -> ``operand_raw_descriptions.json``. Tolerance: the NEW heading-delimited
    §7.2.1.1 pairer -> ``tolerance_raw_descriptions.json`` (a SEPARATE
    strategy). NEVER prints the extracted prose — only entry counts.
    """
    # Lazy: manual_enrich imports manual_build, and only this path needs it.
    from . import manual_enrich

    # The merit operand INVENTORY gates the glued ``CODE(`` split (an entry
    # opens only when the prefix is a real operand, never on prose like ``RMS(``).
    # ONE shared loader — no second path/list here.
    merit_valid_codes = manual_enrich.load_merit_operand_inventory()
    merit_pages = manual_enrich.iter_operand_pages(pdf_path)
    merit_entries, merit_order = manual_enrich.pair_operands(
        merit_pages, valid_codes=merit_valid_codes
    )
    merit_path = raw_extract_path()
    merit_payload = write_raw(merit_path, merit_entries, merit_order)
    print("raw-extract: wrote {} operand entries -> {}".format(
        len(merit_payload), merit_path))

    # Pass the tolerance INVENTORY code set so a phantom heading token
    # (``IMPORTANT NOTE``) or a swallowed title word is dropped at source, never
    # admitted as a bogus code into the raw oracle.
    tol_valid_codes = set(manual_enrich.load_tolerance_operand_inventory())
    tol_pages = manual_enrich.iter_tolerance_operand_pages(pdf_path)
    tol_entries, tol_order = manual_enrich.pair_tolerance_operands(
        tol_pages, valid_codes=tol_valid_codes
    )
    tol_path = tol_raw_extract_path()
    tol_payload = write_raw(tol_path, tol_entries, tol_order)
    print("raw-extract: wrote {} tolerance entries -> {}".format(
        len(tol_payload), tol_path))


def build_manual_corpus(argv=None, find_pdf=None):
    """Build the manual corpus (and, with ``--emit-raw``, the raw oracles); return 0.

    ``find_pdf`` defaults to ``find_manual_pdf``; the script wrapper passes its own
    module-level name so a caller patching the wrapper still steers this function. On
    genuine PDF absence (license-free CI) it prints an ASCII status and returns 0 — the
    only allowed skip of the live build.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    emit = "--emit-raw" in argv

    pdf = (find_pdf or find_manual_pdf)()
    if pdf is None:
        print(
            "manual PDF not present (set OPTIVIBE_MANUAL_PDF or install OpticStudio); "
            "skipping corpus build (license-free CI has no PDF)."
        )
        return 0

    print("building manual corpus from the install-dir PDF ...")
    result = manual_build.build_manual_db(pdf)
    if result.get("skipped"):
        print("corpus: up-to-date (content-addressed skip); nothing to rebuild.")
    else:
        print("corpus: pages={} chunks={}".format(
            result["pdf_page_count"], result["chunk_count"]))
        print("corpus db   -> {}".format(result["db_path"]))
        print("manifest    -> {}".format(result["manifest_path"]))

    if emit:
        emit_raw(pdf)

    return 0
