"""_paths.py — the ONE resolver for where build inputs live (clone vs installed wheel).

Two locations used to be derived from ``__file__`` as ``<package root>/scripts/...`` and
``<repo root>/PROVENANCE.md``. Both exist in a source checkout and NEITHER exists in an
installed wheel (``site-packages/optivibe_reference`` has no ``scripts/`` sibling and no
repo root), so every reader and writer routes through this module instead:

* ``captures_dir()`` — the live-probe captures + verbatim raw oracles. A source checkout
  keeps them at ``packages/optivibe-reference/scripts/captures`` (unchanged, gitignored);
  an installed package keeps them at ``<package>/data/captures`` (created on first write).
* ``provenance_ledger_path()`` — the file the two fail-closed provenance gates
  (``glass_build.assert_agf_grant_on_file``, ``manual_build.assert_manual_grant_on_file``)
  read. A source checkout reads the repo-root ledger and ONLY that — deleting it, or the
  row in it, still refuses, exactly as before. An installed package has no repo ledger, so
  it reads the marker file shipped beside the code (``data/provenance_grants.txt``). The
  marker check itself is unchanged; this only chooses which file it reads.

"Source checkout" is decided by the CLONE LAYOUT (``pyproject.toml`` + ``scripts/`` at the
package root), never by whether ``scripts/captures`` exists: that dir is gitignored and
absent on a fresh clone until the probes run, and keying on it would send a fresh clone's
probe output into the package's ``data/`` tree instead.

Stdlib only; imports nothing from this package (every build module imports it).
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))  # .../optivibe_reference
# packages/optivibe-reference in a clone (src/optivibe_reference -> ../..); some unrelated
# dir (e.g. ``<venv>/Lib``) in an installed wheel, which is why it is CHECKED, not trusted.
_PKG_ROOT = os.path.dirname(os.path.dirname(_HERE))
_REPO_ROOT = os.path.dirname(os.path.dirname(_PKG_ROOT))

# The repo-root provenance ledger a source checkout must carry.
LEDGER_NAME = "PROVENANCE.md"
# The marker file an installed package ships (package-data) in place of the ledger. It
# holds the ledger-row marker strings the two gates look for, one per line.
PROVENANCE_MARKER_NAME = "provenance_grants.txt"


def is_source_checkout(pkg_root=_PKG_ROOT):
    """True iff ``pkg_root`` is a package root in a clone (pyproject + scripts/ beside src)."""
    return (os.path.isfile(os.path.join(pkg_root, "pyproject.toml"))
            and os.path.isdir(os.path.join(pkg_root, "scripts")))


def captures_dir(pkg_root=_PKG_ROOT, package_dir=_HERE):
    """Return the live-probe captures dir (NOT created here — writers create it).

    Clone: ``<pkg_root>/scripts/captures`` (the historic location, unchanged).
    Installed: ``<package_dir>/data/captures``.
    """
    if is_source_checkout(pkg_root):
        return os.path.join(pkg_root, "scripts", "captures")
    return os.path.join(package_dir, "data", "captures")


def provenance_ledger_path(pkg_root=_PKG_ROOT, repo_root=_REPO_ROOT, package_dir=_HERE):
    """Return the file the provenance gates read (fail-closed either way).

    Clone: the repo-root ledger, whether or not it exists — a missing ledger in a clone
    must refuse, never fall through to the shipped marker. Installed: the shipped marker
    file; if that is missing too the returned path does not exist and the gate refuses.
    """
    if is_source_checkout(pkg_root):
        return os.path.join(repo_root, LEDGER_NAME)
    return os.path.join(package_dir, "data", PROVENANCE_MARKER_NAME)
