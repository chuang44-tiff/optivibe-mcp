#!/usr/bin/env python
"""build_glass_catalog.py — generate the committed data/glass_catalog.json.

Resolves the install Glasscat dir (``ZEMAX_GLASSCAT`` override, else the default
``~/Documents/Zemax/Glasscat``), asserts the ``.agf``-derivation grant row is on
file in ``PROVENANCE.md`` (fail-closed), re-parses + recomputes every shipped
``.agf`` record, and writes the normalized-LF ``data/glass_catalog.json``.

PROVENANCE (§8): no raw ``.agf`` is committed — the script reads the
catalogs at build time and discards them; only OUR computed/parsed numbers +
factual glass names land in the committed JSON. Prints ASCII status ONLY (glass
count + path); NEVER relays raw catalog prose (the conda-run cp1252 relay also
crashes on non-ASCII).

Thin wrapper: the step itself is ``optivibe_reference.build_steps.build_glass_catalog``
(package code, so an installed package runs it via ``optivibe-build-data``).

Run:
    conda run -n optivibe-reference python scripts/build_glass_catalog.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from optivibe_reference.build_steps import build_glass_catalog  # noqa: E402


def main():
    return build_glass_catalog()


if __name__ == "__main__":
    sys.exit(main())
