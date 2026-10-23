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

Thin wrapper: the step itself is ``optivibe_reference.build_steps`` (package code, so
an installed package runs it via ``optivibe-build-data``). The module-level names
below are kept because callers use them (the reference ``conftest`` calls
``_find_manual_pdf`` and ``main``; tests patch ``_find_manual_pdf``).

NEVER prints extracted manual text to stdout (the cp1252 ``conda run`` relay crash,
probe finding 3) — only ASCII status (page count, chunk count, paths).
"""
import os
import sys

# Package-relative anchor: make the src layout importable without an installed package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)  # packages/optivibe-reference
_SRC = os.path.join(_PKG_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from optivibe_reference import _paths, build_steps  # noqa: E402

_CAPTURES = _paths.captures_dir()
_RAW_EXTRACT_PATH = build_steps.raw_extract_path()
_TOL_RAW_EXTRACT_PATH = build_steps.tol_raw_extract_path()

_find_manual_pdf = build_steps.find_manual_pdf
_write_raw = build_steps.write_raw
_emit_raw = build_steps.emit_raw


def main(argv=None):
    # ``_find_manual_pdf`` is looked up at CALL time, so patching it here steers the build.
    return build_steps.build_manual_corpus(argv, find_pdf=lambda: _find_manual_pdf())


if __name__ == "__main__":
    raise SystemExit(main())
