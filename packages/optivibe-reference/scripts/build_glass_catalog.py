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

Run:
    conda run -n optivibe-reference python scripts/build_glass_catalog.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from optivibe_reference import glass_build  # noqa: E402
from optivibe_reference.glass_parse import resolve_glasscat  # noqa: E402


def main():
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


if __name__ == "__main__":
    sys.exit(main())
