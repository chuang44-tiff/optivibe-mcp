#!/usr/bin/env python
"""build_vendor_data.py — one-shot local rebuild of all user-side vendor data.

OptiVibe ships code only: the glass catalog, the manual FTS5 corpus, and the
merit + tolerance operand catalogs are built LOCALLY from YOUR licensed Zemax
OpticStudio install and are gitignored (see PROVENANCE.md). Run this once after
install (and again after upgrading OpticStudio):

    python packages/optivibe-reference/scripts/build_vendor_data.py

Steps (each delegates to the existing builder; nothing is duplicated here):

1. Glass catalog  — ``build_glass_catalog.py``: reads the ``.agf`` files from
   your install's Glasscat dir (``ZEMAX_GLASSCAT`` env override, else
   ``~/Documents/Zemax/Glasscat``) and writes ``data/glass_catalog.json``.
2. Manual corpus  — ``build_manual_corpus.py --emit-raw``: indexes your install's
   ``OpticStudio_UserManual_en.pdf`` (``OPTIVIBE_MANUAL_PDF`` env override, else
   the standard install dir) into the local ``data/manual_corpus.db`` +
   text-free ``data/manual_manifest.json``, AND emits the gitignored verbatim raw
   operand/tolerance oracles the next two steps merge as local descriptions.
3. Merit operand catalog     — ``catalog_build.main``: merges the committed
   ``operand_synonyms.json`` + the live-probe captures + (when present) the
   verbatim-local raw oracle into the user-built ``data/operand_catalog.json``.
4. Tolerance operand catalog — ``tolerance_build.main``: the same, for
   ``data/tolerance_operand_catalog.json``.

Steps 3-4 need the live-probe captures (``scripts/captures/``), regenerated from
your OpticStudio install by ``probe_operands.py`` + ``probe_tolerances.py`` (run
those ONCE first — they boot a headless engine, probe the operand/tolerance enum
surface, and reap it). Absent the captures, the step is SKIPPED and that operand
family answers a typed ``operand_catalog_unavailable`` envelope. A missing
glass/manual input is likewise reported and SKIPPED (its tool family degrades); an
unexpected error fails the step. Exit code = number of FAILED (not skipped) steps.
ASCII status output only.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)  # packages/optivibe-reference
_SRC = os.path.join(_PKG_ROOT, "src")
for _p in (_HERE, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_glass_catalog  # noqa: E402
import build_manual_corpus  # noqa: E402
from optivibe_reference import catalog_build, tolerance_build  # noqa: E402

_CAPTURES = os.path.join(_HERE, "captures")
# The live-probe captures each operand-catalog build REQUIRES (regenerated from
# the user's OpticStudio install); absent -> that step is a fresh-install SKIP.
_OPERAND_CAPTURES = ("operand_inventory_438.json", "probe_operands_capture.json")
_TOLERANCE_CAPTURES = ("tolerance_inventory_62.json", "probe_tolerances_capture.json")


def _captures_present(names):
    return all(os.path.isfile(os.path.join(_CAPTURES, n)) for n in names)


def _run_catalog_step(label, captures, build_main):
    """Run a user-built operand/tolerance catalog build; return 1 iff it FAILED.

    Absent live-probe captures = a fresh install that has not probed yet -> SKIP
    (that domain's operand tools answer operand_catalog_unavailable until built).
    Present captures + a raised error OR a nonzero build exit code -> FAILED.
    """
    if not _captures_present(captures):
        print("[%s] SKIPPED - live-probe captures not present in scripts/captures. "
              "Run the probes against your OpticStudio first: "
              "python scripts/probe_operands.py && python scripts/probe_tolerances.py"
              % label)
        print("[%s] this operand family answers operand_catalog_unavailable until "
              "the build runs." % label)
        return 0
    try:
        rc = build_main()
    except Exception as exc:  # noqa: BLE001 — report per-step, keep going
        print("[%s] FAILED - %s: %s" % (label, type(exc).__name__, exc))
        return 1
    if rc:
        print("[%s] FAILED - build returned exit code %d (see messages above)."
              % (label, rc))
        return 1
    return 0


def main():
    failures = 0

    print("=== [1/4] glass catalog (from your install's .agf Glasscat) ===")
    try:
        build_glass_catalog.main()
    except FileNotFoundError as exc:
        print("[glass-build] SKIPPED - %s" % exc)
        print("[glass-build] glass tools answer glass_catalog_unavailable until "
              "this build runs.")
    except Exception as exc:  # noqa: BLE001 — report per-step, keep going
        failures += 1
        print("[glass-build] FAILED - %s: %s" % (type(exc).__name__, exc))

    print("=== [2/4] manual corpus + verbatim raw oracles (from your install's PDF) ===")
    try:
        # --emit-raw ALSO writes the gitignored operand/tolerance raw oracles that
        # steps 3-4 merge as verbatim-local descriptions. The TOLERANCE oracle opens
        # the tolerance INVENTORY capture (tolerance_inventory_62.json) unconditionally
        # — absent, --emit-raw raises FileNotFoundError mid-run and the whole manual
        # step (corpus + merit oracle already written) is mis-reported FAILED. Preflight
        # that capture: emit the oracles only when it is present, else build the corpus
        # WITHOUT --emit-raw — the SAME fresh-install degrade steps 3-4 take (operand/
        # tolerance stay synonyms-only / unavailable), never a FAIL. build_manual_corpus
        # itself still skips cleanly (exit 0 + message) when no licensed PDF is present.
        if os.path.isfile(os.path.join(_CAPTURES, "tolerance_inventory_62.json")):
            emit_args = ["--emit-raw"]
        else:
            emit_args = []
            print("[manual-build] NOTE: tolerance inventory capture absent -> building "
                  "corpus WITHOUT --emit-raw; operand/tolerance catalogs stay "
                  "synonyms-only until the live-probe captures are regenerated.")
        build_manual_corpus.main(emit_args)
    except Exception as exc:  # noqa: BLE001 — report per-step, keep going
        failures += 1
        print("[manual-build] FAILED - %s: %s" % (type(exc).__name__, exc))

    print("=== [3/4] merit operand catalog (user-built; synonyms + local oracle) ===")
    failures += _run_catalog_step(
        "operand-build", _OPERAND_CAPTURES, catalog_build.main
    )

    print("=== [4/4] tolerance operand catalog (user-built; synonyms + local oracle) ===")
    failures += _run_catalog_step(
        "tolerance-build", _TOLERANCE_CAPTURES, tolerance_build.main
    )

    if failures:
        print("done with %d FAILED step(s) - see messages above." % failures)
    else:
        print("done. Built data is local-only and gitignored - never commit it.")
    return failures


if __name__ == "__main__":
    sys.exit(main())
