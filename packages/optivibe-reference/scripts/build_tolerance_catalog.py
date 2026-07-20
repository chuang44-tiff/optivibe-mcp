"""build_tolerance_catalog.py — regenerate the committed tolerance catalog JSON.

Thin entrypoint: delegates to ``tolerance_build.main()`` (which builds from the
live-probe captures + the committed bootstrap descriptions, runs the §3 build
invariants, and writes the byte-stable committed
``data/tolerance_operand_catalog.json``).

Run from the package root:
    conda activate optivibe-reference
    PYTHONPATH=src python scripts/build_tolerance_catalog.py
"""
from optivibe_reference import tolerance_build

if __name__ == "__main__":
    tolerance_build.main()
