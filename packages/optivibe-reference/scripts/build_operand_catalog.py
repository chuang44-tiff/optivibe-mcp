"""build_operand_catalog.py — regenerate the (user-built) merit operand catalog JSON.

Thin entrypoint: delegates to ``catalog_build.main()`` (which builds from the
live-probe captures + the committed ``operand_synonyms.json`` + the gitignored
verbatim-local raw oracle, runs the build invariants, and writes the user-built
``data/operand_catalog.json``). the verbatim-local build: this catalog embeds verbatim-local
descriptions, so it is gitignored + user-built — never committed.

Run from the package root:
    conda activate optivibe-reference
    PYTHONPATH=src python scripts/build_operand_catalog.py
"""
from optivibe_reference import catalog_build

if __name__ == "__main__":
    raise SystemExit(catalog_build.main())
