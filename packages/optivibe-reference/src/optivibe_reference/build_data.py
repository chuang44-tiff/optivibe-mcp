"""build_data.py — one-shot local rebuild of all user-side vendor data (``optivibe-build-data``).

OptiVibe ships code only: the glass catalog, the manual FTS5 corpus, and the merit +
tolerance operand catalogs are built LOCALLY from YOUR licensed Zemax OpticStudio install
and are never committed. Run this once after install (and again after upgrading
OpticStudio):

    optivibe-build-data              # installed (pip install optivibe-mcp)
    optivibe-build-data --probe      # first run: probe the operand surfaces, then build

It is the in-package equivalent of the published ``scripts/build_vendor_data.py`` and
needs no clone. Steps (each calls the package-level builder; nothing is duplicated here):

0. (``--probe`` only) the live operand + tolerance probes (``probe_operands.main``,
   ``probe_tolerances.main``): boot a headless OpticStudio, write the captures steps 2-4
   need, reap the engine. They need pythonnet (``optivibe-reference[probe]``; the
   optivibe-mcp install already has it). A missing pythonnet or a probe error FAILS
   that probe, and the build steps still run.
1. Glass catalog  — ``build_steps.build_glass_catalog``: the ``.agf`` files from your
   install's Glasscat dir (``ZEMAX_GLASSCAT`` env override, else
   ``~/Documents/Zemax/Glasscat``) -> ``data/glass_catalog.json``.
2. Manual corpus  — ``build_steps.build_manual_corpus``: your install's
   ``OpticStudio_UserManual_en.pdf`` (``OPTIVIBE_MANUAL_PDF`` env override, else the
   standard install dir) -> the local ``data/manual_corpus.db`` + text-free
   ``data/manual_manifest.json``, plus (``--emit-raw``) the verbatim raw
   operand/tolerance oracles steps 3-4 merge as local descriptions. Needs PyMuPDF
   (``optivibe-reference[manual]``).
3. Merit operand catalog     — ``catalog_build.main`` -> ``data/operand_catalog.json``.
4. Tolerance operand catalog — ``tolerance_build.main`` ->
   ``data/tolerance_operand_catalog.json``.

A missing input (no Glasscat dir, no manual PDF, no PyMuPDF, no live-probe captures) is
reported and SKIPPED: that tool family answers a typed ``*_unavailable`` envelope until
the build runs. An unexpected error FAILS the step. Exit code = number of FAILED (not
skipped) steps. ASCII status output only.
"""
import argparse
import importlib.util
import json
import os
import re
import sys

from . import _paths, build_steps, catalog_build, tolerance_build

# The live-probe captures each operand-catalog build REQUIRES (regenerated from the
# user's OpticStudio install); absent -> that step is a fresh-install SKIP.
OPERAND_CAPTURES = ("operand_inventory_438.json", "probe_operands_capture.json")
TOLERANCE_CAPTURES = ("tolerance_inventory_62.json", "probe_tolerances_capture.json")
# The capture the manual step's ``--emit-raw`` opens unconditionally (tolerance oracle).
TOLERANCE_INVENTORY_CAPTURE = "tolerance_inventory_62.json"
# ``--emit-raw`` opens BOTH inventories: the merit one for the glued-code gate, the
# tolerance one for the tolerance oracle. Either absent -> no ``--emit-raw``.
EMIT_RAW_CAPTURES = (OPERAND_CAPTURES[0], TOLERANCE_INVENTORY_CAPTURE)

_DESCRIPTION = (
    "Build the OptiVibe reference data (glass catalog, manual corpus, merit and "
    "tolerance operand catalogs) locally from your licensed OpticStudio install. "
    "A missing input is SKIPPED; the exit code is the number of FAILED steps."
)


def _ascii(text):
    """``text`` with every non-ASCII character replaced (status output is ASCII-only)."""
    return str(text).encode("ascii", "replace").decode("ascii")


def _say(text):
    print(_ascii(text))


_NOT_WRITABLE_HINT = (
    " (if the install location is not writable -- the build writes into the "
    "package's data and captures directories, so install optivibe-mcp into a virtual "
    "environment, or use `pip install --user`, then re-run.)"
)


def _error_text(exc):
    text = "%s: %s" % (type(exc).__name__, exc)
    if isinstance(exc, PermissionError):
        text += _NOT_WRITABLE_HINT
    return _ascii(text)


def _captures_present(names, captures=None):
    captures = _paths.captures_dir() if captures is None else captures
    return all(os.path.isfile(os.path.join(captures, n)) for n in names)


def probed_count(capture_path):
    """The ``inventory_count`` a probe recorded in its capture, or None.

    None when the capture is absent, unreadable, or carries no integer count.
    """
    try:
        with open(capture_path, encoding="utf-8") as fh:
            count = json.load(fh).get("inventory_count")
    except (OSError, ValueError, AttributeError):
        return None
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def _capture_incomplete(capture_path):
    """True iff the capture reads and says ``ok: false``: a probe that failed after its first
    step writes its capture early (so the count matches the new inventory) but never marks it ok."""
    try:
        with open(capture_path, encoding="utf-8") as fh:
            return json.load(fh).get("ok") is False
    except (OSError, ValueError, AttributeError):
        return False


def catalog_step_blocker(captures, captures_dir=None):
    """Why this operand/tolerance catalog cannot be built for the probed install, or None.

    ``captures`` is ``(<kind>_inventory_<N>.json, probe_<kind>s_capture.json)``; N is the
    operand count the shipped curated data (sign conventions, run-verdict buckets) was
    built against. The probe names its inventory by the LIVE count, so an OpticStudio
    with a different operand list writes a different file and the curated data does not
    cover it. That is not a missing probe, so it must not be reported as one.
    """
    captures_dir = _paths.captures_dir() if captures_dir is None else captures_dir
    curated = int(re.search(r"_(\d+)\.json$", captures[0]).group(1))
    capture = os.path.join(captures_dir, captures[1])
    probed = probed_count(capture)
    if probed is None or probed == curated:
        if _capture_incomplete(capture):
            return ("the last live probe did not finish (%s is marked incomplete), so this "
                    "catalog is not built from it; re-run `optivibe-build-data --probe`." % captures[1])
        return None
    kind = "tolerance" if captures[0].startswith("tolerance") else "merit"
    return ("your OpticStudio exposes %d %s operands; the curated data shipped with "
            "this release covers the %d it was built against, so this catalog cannot "
            "be built for your OpticStudio version yet (re-running the probes will not "
            "change that)." % (probed, kind, curated))


def _run_probe(label, module_name):
    """Run one live probe; return 1 iff it FAILED (a missing pythonnet included).

    The probe module is imported INSIDE the guard, so a broken import is that probe's
    FAILED report and the other probe still runs.
    """
    # Lazy: importing the probe modules is cheap and clr-free, but keep them off the
    # import path of a plain build run.
    try:
        from ._probe_common import PROBE_EXTRA_HINT, PythonnetMissing
        probe_main = importlib.import_module("." + module_name, __package__).main
    except Exception as exc:  # noqa: BLE001 — report per-step, keep going
        _say("[%s] FAILED - cannot import the probe: %s" % (label, _error_text(exc)))
        return 1
    try:
        rc = probe_main()
    except PythonnetMissing:
        _say("[%s] FAILED - pythonnet is not installed, so the live probe cannot run. "
             "%s." % (label, PROBE_EXTRA_HINT))
        return 1
    except Exception as exc:  # noqa: BLE001 — report per-step, keep going
        _say("[%s] FAILED - %s" % (label, _error_text(exc)))
        return 1
    if rc:
        _say("[%s] FAILED - probe returned exit code %d (see messages above)." % (label, rc))
        return 1
    return 0


def run_probes():
    """Step 0: the two live probes, merit first. Returns the number that FAILED."""
    failures = 0
    _say("=== [probe 1/2] merit operand surface (boots a headless OpticStudio) ===")
    failures += _run_probe("probe-operands", "probe_operands")
    _say("=== [probe 2/2] tolerance operand surface (boots a headless OpticStudio) ===")
    failures += _run_probe("probe-tolerances", "probe_tolerances")
    return failures


def _run_catalog_step(label, captures, build_main):
    """Run a user-built operand/tolerance catalog build; return 1 iff it FAILED.

    Absent live-probe captures = a fresh install that has not probed yet -> SKIP
    (that domain's operand tools answer operand_catalog_unavailable until built).
    Present captures + a raised error OR a nonzero build exit code -> FAILED.
    """
    blocker = catalog_step_blocker(captures)
    if blocker is not None:
        _say("[%s] SKIPPED - %s" % (label, blocker))
        _say("[%s] this operand family answers operand_catalog_unavailable."
             % label)
        return 0
    if not _captures_present(captures):
        _say("[%s] SKIPPED - live-probe captures not present in %s. Run the probes "
             "against your OpticStudio first: optivibe-build-data --probe"
             % (label, _paths.captures_dir()))
        _say("[%s] this operand family answers operand_catalog_unavailable until "
             "the build runs." % label)
        return 0
    try:
        rc = build_main()
    except Exception as exc:  # noqa: BLE001 — report per-step, keep going
        _say("[%s] FAILED - %s" % (label, _error_text(exc)))
        return 1
    if rc:
        _say("[%s] FAILED - build returned exit code %d (see messages above)."
             % (label, rc))
        return 1
    return 0


def _run_glass_step():
    """Step 1; return 1 iff it FAILED. No Glasscat dir -> SKIPPED."""
    try:
        build_steps.build_glass_catalog()
    except FileNotFoundError as exc:
        _say("[glass-build] SKIPPED - %s" % _ascii(exc))
        _say("[glass-build] glass tools answer glass_catalog_unavailable until "
             "this build runs.")
        return 0
    except Exception as exc:  # noqa: BLE001 — report per-step, keep going
        _say("[glass-build] FAILED - %s" % _error_text(exc))
        return 1
    return 0


def _run_manual_step():
    """Step 2; return 1 iff it FAILED. No PDF or no PyMuPDF -> SKIPPED."""
    pdf = build_steps.find_manual_pdf()
    if pdf is None:
        _say("[manual-build] SKIPPED - manual PDF not present (set OPTIVIBE_MANUAL_PDF "
             "or install OpticStudio).")
        _say("[manual-build] search_reference answers corpus_unavailable until this "
             "build runs.")
        return 0
    if importlib.util.find_spec("fitz") is None:
        _say("[manual-build] SKIPPED - PyMuPDF is not installed, so the manual PDF "
             "cannot be read. install optivibe-reference[manual] "
             '(pip install "optivibe-reference[manual]").')
        return 0
    try:
        # --emit-raw ALSO writes the gitignored operand/tolerance raw oracles that
        # steps 3-4 merge as verbatim-local descriptions. It opens BOTH inventory
        # captures unconditionally (merit for the glued-code gate, tolerance for the
        # tolerance oracle) — either absent, --emit-raw raises FileNotFoundError mid-run
        # and the whole manual step is mis-reported FAILED. Preflight both: emit the
        # oracles only when both are present, else build the corpus WITHOUT --emit-raw —
        # the SAME fresh-install degrade steps 3-4 take (operand/tolerance stay
        # synonyms-only / unavailable), never a FAIL.
        if _captures_present(EMIT_RAW_CAPTURES):
            emit_args = ["--emit-raw"]
        else:
            emit_args = []
            _say("[manual-build] NOTE: an operand inventory capture is absent -> building "
                 "corpus WITHOUT --emit-raw; operand/tolerance catalogs stay "
                 "synonyms-only until the live-probe captures are regenerated.")
        build_steps.build_manual_corpus(emit_args, find_pdf=lambda: pdf)
    except Exception as exc:  # noqa: BLE001 — report per-step, keep going
        _say("[manual-build] FAILED - %s" % _error_text(exc))
        return 1
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="optivibe-build-data", description=_DESCRIPTION)
    parser.add_argument(
        "--probe", action="store_true",
        help="first run the live operand + tolerance probes (boots a headless "
             "OpticStudio; needs pythonnet: optivibe-reference[probe])")
    return parser.parse_args(argv)


def main(argv=None):
    """Run every step; return the number of FAILED steps (``--help`` exits 0)."""
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    failures = 0

    if args.probe:
        failures += run_probes()

    _say("=== [1/4] glass catalog (from your install's .agf Glasscat) ===")
    failures += _run_glass_step()

    _say("=== [2/4] manual corpus + verbatim raw oracles (from your install's PDF) ===")
    failures += _run_manual_step()

    _say("=== [3/4] merit operand catalog (user-built; synonyms + local oracle) ===")
    failures += _run_catalog_step(
        "operand-build", OPERAND_CAPTURES, lambda: catalog_build.main()
    )

    _say("=== [4/4] tolerance operand catalog (user-built; synonyms + local oracle) ===")
    failures += _run_catalog_step(
        "tolerance-build", TOLERANCE_CAPTURES, lambda: tolerance_build.main()
    )

    if failures:
        _say("done with %d FAILED step(s) - see messages above." % failures)
    else:
        _say("done. Built data is local-only and gitignored - never commit it.")
    return failures


if __name__ == "__main__":
    sys.exit(main())
