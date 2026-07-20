"""build_manual_corpus.py — CLI orchestrator for the manual FTS5 RAG corpus.

Resolves the licensed install-dir manual PDF (env ``OPTIVIBE_MANUAL_PDF`` else the
standard install-dir glob), then calls ``manual_build.build_manual_db`` to write
the gitignored ``.db`` (at ``MANUAL_DB_PATH``) and the committed TEXT-FREE manifest
(at ``MANUAL_MANIFEST_PATH``). On genuine PDF absence (license-free CI) it prints an
ASCII status and exits 0 — the only allowed skip of the live build.

``--emit-raw`` ALSO runs the bbox operand-pairer (``manual_enrich``) over the
alphabetical operand section and writes the VERBATIM raw extract to
``scripts/captures/operand_raw_descriptions.json`` (GITIGNORED — it is the oracle a
human paraphrases from, NEVER committed). It is the input the verbatim-overlap
guard test (``test_catalog_enrichment``) checks the committed paraphrases against.

NEVER prints extracted manual text to stdout (the cp1252 ``conda run`` relay crash,
probe finding 3) — only ASCII status (page count, chunk count, paths).
"""
import glob
import json
import os
import sys

# Package-relative anchors (mirror catalog_build.main): make the src layout
# importable without an installed package, and resolve the captures dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)  # packages/optivibe-reference
_SRC = os.path.join(_PKG_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from optivibe_reference import manual_build, manual_enrich  # noqa: E402

_CAPTURES = os.path.join(_PKG_ROOT, "scripts", "captures")
_RAW_EXTRACT_PATH = os.path.join(_CAPTURES, "operand_raw_descriptions.json")
_TOL_RAW_EXTRACT_PATH = os.path.join(_CAPTURES, "tolerance_raw_descriptions.json")
# F7: the tolerance operand INVENTORY (the authoritative 62-code set) — passed to the
# tolerance pairer so a phantom/swallowed heading token is dropped at source.
_TOL_INVENTORY_PATH = os.path.join(_CAPTURES, "tolerance_inventory_62.json")


def _find_manual_pdf():
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


def _write_raw(path, entries, order):
    """Write ``{code: {code, page, raw_text}}`` to a GITIGNORED capture (oracle).

    NEVER prints the extracted prose — the caller prints only the entry count.

    ATOMIC (a hardening finding): the oracle is written to a same-dir
    ``<path>.tmp`` then ``os.replace``d onto the final path, so a concurrent reader
    never sees a half-written raw extract.
    """
    os.makedirs(_CAPTURES, exist_ok=True)
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


def _emit_raw(pdf_path):
    """Extract BOTH raw operand oracles to the GITIGNORED captures (verbatim source).

    Merit: the finding-2 bbox two-column pairer over the alphabetical §5.2.1.3
    section -> ``operand_raw_descriptions.json``. Tolerance: the NEW heading-delimited
    §7.2.1.1 pairer -> ``tolerance_raw_descriptions.json`` (a SEPARATE strategy,
    probe FACT 3). NEVER prints the extracted prose — only entry counts.
    """
    merit_pages = manual_enrich.iter_operand_pages(pdf_path)
    merit_entries, merit_order = manual_enrich.pair_operands(merit_pages)
    merit_payload = _write_raw(_RAW_EXTRACT_PATH, merit_entries, merit_order)
    print("raw-extract: wrote {} operand entries -> {}".format(
        len(merit_payload), _RAW_EXTRACT_PATH))

    # F7: pass the tolerance INVENTORY code set so a phantom heading token
    # (``IMPORTANT NOTE``) or a swallowed title word is dropped at source, never
    # admitted as a bogus code into the raw oracle.
    tol_inv = json.load(open(_TOL_INVENTORY_PATH, encoding="utf-8"))
    tol_valid_codes = {m["code"] for m in tol_inv["members"]}
    tol_pages = manual_enrich.iter_tolerance_operand_pages(pdf_path)
    tol_entries, tol_order = manual_enrich.pair_tolerance_operands(
        tol_pages, valid_codes=tol_valid_codes
    )
    tol_payload = _write_raw(_TOL_RAW_EXTRACT_PATH, tol_entries, tol_order)
    print("raw-extract: wrote {} tolerance entries -> {}".format(
        len(tol_payload), _TOL_RAW_EXTRACT_PATH))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    emit_raw = "--emit-raw" in argv

    pdf = _find_manual_pdf()
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

    if emit_raw:
        _emit_raw(pdf)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
