"""Publication checks: package provenance, backend-free imports, version literals, and name resolution."""
import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

# The distributions this repository publishes: directory -> its top-level import packages,
# VERSION-BEARING FIRST.  Pinned rather than globbed: a glob would take its universe from
# the tree it grades.
PACKAGES = {
    "optivibe-harness": ("optivibe_harness", "optivibe_doctor"),
    "optivibe-reference": ("optivibe_reference",),
}

# One distribution now ships two top-level packages, so "a distribution" and "a package"
# are different sets and the walk needs both: the ROOT is a distribution's src/, the NAME
# is a package the import system can be asked to walk.

# Filing the first under the second is the defect this shape replaces -- every
# `optivibe_doctor` module was recorded as a module of `optivibe_harness`, which
# `pkgutil.walk_packages` correctly declined to find there.

# Version-bearing import name -> directory, DERIVED so there is no second hand-written
# copy to drift.  A version literal is a property of the DISTRIBUTION: its manifest and
# its primary package's initialiser.
PACKAGE_DIRS = {tops[0]: directory for directory, tops in PACKAGES.items()}

# EVERY top-level package -> its directory.  The import-side map, and its DIFFERENCE from
# PACKAGE_DIRS is the repair: same shape, different contents, and collapsing them back
# into one reintroduces the defect.
PACKAGE_ROOTS = {top: directory
                 for directory, tops in PACKAGES.items() for top in tops}

# The single acceptance set for every backend-import consumer in this suite.
BLOCKED_ROOTS = frozenset({
    "clr",
    "pythonnet",
    "clr_loader",
    "System",
    "ZOSAPI",
    "ZOSAPI_NetHelper",
    "ZOSAPI_Interfaces",
})

GUARD_SENTINEL = "OPTIVIBE_BACKEND_IMPORT_BLOCKED"
RECEIPT_ENV = "OPTIVIBE_IMPORT_GUARD_RECEIPT"

VERSION_PATTERN = r"^\d+\.\d+\.\d+$"

GRADIENT_TYPE_NAMES = ("Gradient1", "Gradient2", "Gradient3", "Gradient10", "Gradient12")
UNRESOLVABLE_TYPE_NAMES = ("Gradient", "Gradient2X", "", "Standard")

_IMPORT_CHILD = '''\
import importlib, json, pkgutil, sys
tops = json.loads(sys.argv[1])
blocked = json.loads(sys.argv[2])
report = {"walked": {}, "failures": [], "control": {}}
for top in tops:
    def note(name, _report=report):
        _report["failures"].append([name, "walk", repr(sys.exc_info()[1])[:300]])
    package = importlib.import_module(top)
    found = [top] + [item.name for item in
                     pkgutil.walk_packages(package.__path__, top + ".", onerror=note)]
    report["walked"][top] = sorted(set(found))
    for name in found:
        try:
            importlib.import_module(name)
        except BaseException as exc:
            report["failures"].append([name, type(exc).__name__, str(exc)[:300]])
for root in blocked:
    try:
        importlib.import_module(root)
        report["control"][root] = None
    except ImportError as exc:
        report["control"][root] = str(exc)
    except BaseException as exc:
        report["control"][root] = type(exc).__name__ + ": " + str(exc)
with open(sys.argv[3], "w") as handle:
    json.dump(report, handle)
'''


def _anchor_absences(root):
    """Which declared distributions have no packaging manifest under `root`."""
    return [name for name in sorted(PACKAGES)
            if not os.path.isfile(os.path.join(root, "packages", name, "pyproject.toml"))]


def _repo_root():
    """Return the nearest enclosing checkout of this project, verified by both manifests."""
    # The proof is unchanged and is still the whole guarantee: a directory qualifies only
    # when BOTH declared distributions carry a manifest under it.  What widened is the
    # number of candidates it will accept that proof from: one, to the ancestor chain.

    # One candidate assumed this file's parent IS the repository root -- true in a published
    # checkout and nowhere else, so anywhere else every check here raised before asserting
    # anything, and the suite was gradeable only in the layout already known to be correct.

    # Still fails closed in the direction that matters: if NO ancestor carries both
    # manifests this raises, naming the nearest candidate's absences and the chain searched.
    # Nearest-wins, so a checkout nested inside another anchors on itself.
    start = os.path.dirname(os.path.abspath(__file__))
    searched, current = [], start
    while True:
        searched.append(current)
        if not _anchor_absences(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise RuntimeError(
        "no ancestor of this file is a checkout of this project: %s has no packaging "
        "manifest under %r, and none of %r carries both"
        % (", ".join(_anchor_absences(searched[0])), searched[0], searched))


def _iter_source_files(base):
    """Yield every source path under a tree, ignoring compiled-bytecode directories."""
    for current, subdirs, names in os.walk(base):
        subdirs[:] = [name for name in subdirs if name != "__pycache__"]
        for name in sorted(names):
            if name.endswith(".py"):
                yield os.path.join(current, name)


def _module_scope_dotnet_imports(source):
    """Return (line, name) for every blocked-root import a module reaches when it executes."""
    hits = []

    def visit(node, deferred):
        for child in ast.iter_child_nodes(node):
            nested = deferred or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if not nested and isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name.split(".")[0] in BLOCKED_ROOTS:
                        hits.append((child.lineno, alias.name))
            elif not nested and isinstance(child, ast.ImportFrom):
                named = child.module or ""
                if named.split(".")[0] in BLOCKED_ROOTS:
                    hits.append((child.lineno, named))
            visit(child, nested)

    visit(ast.parse(source), False)
    return hits


def _src_modules():
    """Map each top-level import name to its filesystem module set and source-file count."""
    # Keyed on the top-level package and rooted at src/<top>/, so a distribution shipping
    # two packages yields two entries whose module sets are exactly what the import system
    # can walk.  Dotted names stay relative to src/, so they read `optivibe_doctor._model`.

    # AND src/ IS STILL CLOSED OVER THE DECLARATION.  Narrowing each walk to src/<top>/
    # would move anything under src/ outside a declared package out of every comparison
    # here: previously mis-filed LOUDLY, afterwards invisible, which is strictly worse.

    # So src/ is still walked, and every file it finds must be claimed by exactly one
    # declared package.  An unclaimed one raises below instead of vanishing.
    root = _repo_root()
    found, claimed = {}, {}
    for top, directory in sorted(PACKAGE_ROOTS.items()):
        base = os.path.join(root, "packages", directory, "src")
        top_dir = os.path.join(base, top)
        if not os.path.isdir(top_dir):
            raise RuntimeError("no source tree at %r" % top_dir)
        modules, files = set(), 0
        for path in _iter_source_files(top_dir):
            files += 1
            claimed.setdefault(directory, set()).add(os.path.normcase(os.path.abspath(path)))
            parts = os.path.relpath(path, base).replace(os.sep, "/")[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            modules.add(".".join(parts))
        if not modules:
            raise RuntimeError("no module found under %r" % top_dir)
        found[top] = (modules, files)
    for directory in sorted(PACKAGES):
        base = os.path.join(root, "packages", directory, "src")
        if not os.path.isdir(base):
            raise RuntimeError("no source tree at %r" % base)
        every = {os.path.normcase(os.path.abspath(p)) for p in _iter_source_files(base)}
        stray = sorted(os.path.relpath(p, base) for p in every - claimed.get(directory, set()))
        if stray:
            raise RuntimeError(
                "%s has source file(s) under src/ that no declared top-level package "
                "claims: %r.  PACKAGES names %r for this distribution; a file outside "
                "those trees is in no module set and therefore in no comparison in this "
                "file.  Declare its package or move it."
                % (directory, stray, list(PACKAGES[directory])))
    return found


def _import_guard_source(block_extra=()):
    """Return start-up source installing a meta-path finder that refuses the blocked roots."""
    return (
        "import json, os, sys\n"
        "_BLOCKED = %r\n"
        "_SENTINEL = %r\n"
        "_RECEIPT = os.environ[%r]\n"
        "_ATTEMPTED = []\n"
        "def _record():\n"
        "    with open(_RECEIPT, 'w') as handle:\n"
        "        json.dump({'armed': True, 'attempted': _ATTEMPTED}, handle)\n"
        "class _Guard(object):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        root = fullname.split('.')[0]\n"
        "        if root in _BLOCKED:\n"
        "            if root not in _ATTEMPTED:\n"
        "                _ATTEMPTED.append(root)\n"
        "                _record()\n"
        "            raise ImportError(_SENTINEL + ':' + fullname)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Guard())\n"
        "_record()\n"
    ) % (sorted(BLOCKED_ROOTS | set(block_extra)), GUARD_SENTINEL, RECEIPT_ENV)


def _walk_and_import_in_guarded_child(tops):
    """Import every module of the named packages in a child that cannot load the backend."""
    guard_dir = tempfile.mkdtemp()
    work_dir = tempfile.mkdtemp()
    try:
        with open(os.path.join(guard_dir, "sitecustomize.py"), "w", newline="\n") as handle:
            handle.write(_import_guard_source())
        env = dict(os.environ)
        env["PYTHONPATH"] = guard_dir
        env[RECEIPT_ENV] = os.path.join(guard_dir, "guard-receipt.json")
        out = os.path.join(guard_dir, "walk-report.json")
        completed = subprocess.run(
            [sys.executable, "-c", _IMPORT_CHILD,
             json.dumps(list(tops)), json.dumps(sorted(BLOCKED_ROOTS)), out],
            cwd=work_dir, env=env, capture_output=True, text=True, timeout=600)
        report = None
        if os.path.isfile(out):
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
        return completed, report
    finally:
        shutil.rmtree(guard_dir, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)


def _declared_version(path):
    """Return the version literal declared by a packaging manifest or a package initialiser."""
    if path.endswith(".toml"):
        with open(path, "rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    literals = [node.value.value for node in tree.body
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                for target in node.targets
                if isinstance(target, ast.Name) and target.id == "__version__"]
    if len(literals) != 1:
        raise RuntimeError("expected one version literal in %r, found %d" % (path, len(literals)))
    return literals[0]


def test_installed_packages_resolve_under_this_repository():
    root = _repo_root()
    # EVERY top-level package, not one per distribution: a secondary package resolving to
    # some other copy on sys.path would make every assertion about it in this suite a
    # statement about a tree nobody is releasing.
    for top, directory in sorted(PACKAGE_ROOTS.items()):
        tree = os.path.normcase(os.path.abspath(
            os.path.join(root, "packages", directory, "src")))
        resolved = importlib.import_module(top).__file__
        here = os.path.normcase(os.path.abspath(resolved))
        assert here.startswith(tree), (
            "%s resolved to %r, which is not under %r (anchored at %r)"
            % (top, resolved, tree, root))


def test_no_module_execution_scope_dotnet_import():
    root = _repo_root()
    # Keyed on the DISTRIBUTION and deliberately still a walk of the whole src/ tree: this
    # check reads source text rather than importing, so the widest scope is the right one
    # and a newly added top-level package is covered with no edit here.
    for directory in sorted(PACKAGES):
        base = os.path.join(root, "packages", directory, "src")
        scanned = 0
        offenders = []
        for path in _iter_source_files(base):
            scanned += 1
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for line, imported in _module_scope_dotnet_imports(source):
                offenders.append("%s line %d imports %s" % (path, line, imported))
        assert scanned > 0, "the scan read no source file under %r" % base
        assert offenders == [], (
            "%s reaches a backend import while its modules execute: %s"
            % (directory, offenders))


def test_every_module_imports_without_dotnet():
    on_disk = _src_modules()
    completed, report = _walk_and_import_in_guarded_child(sorted(on_disk))
    assert completed.returncode == 0, (
        "the guarded child exited %d\n%s" % (completed.returncode, completed.stderr[-4000:]))
    assert report is not None, "the guarded child wrote no report"
    assert report["failures"] == [], "modules failed to import: %r" % (report["failures"],)
    for top, (modules, _files) in sorted(on_disk.items()):
        walked = set(report["walked"].get(top) or [])
        assert len(walked) > 0, "no module of %s was walked" % top
        assert walked == modules, (
            "%s: the import system and the source tree disagree; only on disk %r, "
            "only walked %r" % (top, sorted(modules - walked), sorted(walked - modules)))
    assert set(report["control"]) == set(BLOCKED_ROOTS), (
        "the liveness control exercised %r, not every blocked root"
        % (sorted(report["control"]),))
    for root, message in sorted(report["control"].items()):
        assert message is not None and message.startswith(GUARD_SENTINEL), (
            "importing %r was not refused by the guard; it reported %r" % (root, message))


def test_version_literals_agree():
    # This walks PACKAGES (distributions) while the import checks walk PACKAGE_ROOTS
    # (packages), because a version literal belongs to a distribution and those two counts
    # are no longer equal.

    # So the `2 *` below stays anchored on the number of DISTRIBUTIONS, where it keeps
    # meaning "one manifest plus one initialiser each, all of them read".
    root = _repo_root()
    declared = {}
    for directory, tops in sorted(PACKAGES.items()):
        package = os.path.join(root, "packages", directory)
        declared[directory + " manifest"] = _declared_version(
            os.path.join(package, "pyproject.toml"))
        declared[directory + " " + tops[0]] = _declared_version(
            os.path.join(package, "src", tops[0], "__init__.py"))
    assert len(declared) == 2 * len(PACKAGES), "read %d literals" % len(declared)
    distinct = sorted(set(declared.values()))
    assert len(distinct) == 1, "the declared versions disagree: %r" % (declared,)
    assert re.match(VERSION_PATTERN, distinct[0]), (
        "the declared version %r is not three dotted numbers" % (distinct[0],))
    # A version-bearing package MUST report the release version.  A secondary one need not
    # carry a literal at all -- it is versioned by the distribution shipping it -- but if it
    # ever grows one it must agree.

    # Otherwise a second, disagreeing literal in the same wheel would be exactly the
    # partial-bump defect this test exists for, held one package over where nothing looks.
    for top, directory in sorted(PACKAGE_ROOTS.items()):
        required = top == PACKAGES[directory][0]
        imported = getattr(importlib.import_module(top), "__version__", None)
        if imported is None and not required:
            continue
        assert imported == distinct[0], (
            "%s reports version %r but the source tree declares %r"
            % (top, imported, distinct[0]))


def test_gradient_type_names_match_exactly():
    from optivibe_harness.tools._grin_cells import (
        GRIN_FAMILY_TYPE_TOKENS, grin_family_type_of_name)
    for name in GRADIENT_TYPE_NAMES:
        assert name in GRIN_FAMILY_TYPE_TOKENS, (
            "%r is not among the recognised type names %r"
            % (name, sorted(GRIN_FAMILY_TYPE_TOKENS)))
    for name in GRADIENT_TYPE_NAMES:
        assert grin_family_type_of_name(name) == name, (
            "%r resolved to %r" % (name, grin_family_type_of_name(name)))
    for name in sorted(GRIN_FAMILY_TYPE_TOKENS):
        assert grin_family_type_of_name(name) == name, (
            "%r resolved to %r" % (name, grin_family_type_of_name(name)))
    for name in UNRESOLVABLE_TYPE_NAMES:
        assert grin_family_type_of_name(name) is None, (
            "%r resolved to %r but names no recognised type"
            % (name, grin_family_type_of_name(name)))
