"""The child entry point — ``python -m optivibe_doctor._worker <name>``.

**Workers emit FACTS.  The parent alone assigns a status** (execution-model rule 2).
Nothing here writes a ``status`` key, and nothing here imports ``_classify``: a probe that
could hand-write a verdict would make every classifier untestable and every verdict
unfalsifiable.

Every blocking call in this project lives in one of these children, because a wedged .NET
P/Invoke cannot be killed from a Python thread but a child process can be killed from
outside.  The parent therefore does no filesystem I/O of its own — not one ``listdir``, not
one metadata read — and every worker body is wrapped so an exception becomes a recorded
fact rather than a traceback on stdout.

Unlike every other module in this package, a worker imports non-stdlib code freely: that is
the entire point of the split.  The imports are all function-local so that importing this
module (which the parent's spawn does not do, but a test may) stays stdlib-only.
"""
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# The observation stream.
# ---------------------------------------------------------------------------
#: The environment variable carrying the ``base``-derived dependency universe into ``pkg``.
DEP_UNIVERSE_ENV = "OPTIVIBE_DOCTOR_DEP_UNIVERSE"
#: The environment variables carrying an owned engine PID into the ``reap`` worker.
REAP_PID_ENV = "OPTIVIBE_DOCTOR_REAP_PID"
REAP_CREATE_TIME_ENV = "OPTIVIBE_DOCTOR_REAP_CREATE_TIME"
REAP_BASELINE_ENV = "OPTIVIBE_DOCTOR_REAP_BASELINE"

#: The two distributions doctor grades.  Duplicated from the contract on purpose — a worker
#: importing ``_contract`` would couple the child to the parent's module set for no gain.
DISTRIBUTIONS = ("optivibe-harness", "optivibe-reference")
TOP_LEVEL = {"optivibe-harness": "optivibe_harness",
             "optivibe-reference": "optivibe_reference"}

_ERROR_CHARS = 240


def _short(text):
    """Collapse any exception text to a single line of at most 240 characters.

    A traceback must never reach stdout: the report is a report, and a consumer parsing
    NDJSON cannot tell a stack frame from a finding.
    """
    flat = " ".join(str(text or "").split())
    return flat[:_ERROR_CHARS]


def emit(check, **facts):
    """Write one JSON observation line to stdout and flush it immediately.

    The flush is load-bearing, not hygiene: a buffered document is indistinguishable from
    a hang, and a worker that dies mid-stream must lose nothing it already said.
    """
    line = json.dumps({"check": check, "facts": facts},
                      default=lambda value: repr(value)[:_ERROR_CHARS],
                      ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _guard(check, call):
    """Run ``call()``; on any exception emit ``check`` carrying the error and return None.

    Returns ``(True, value)`` or ``(False, None)`` so a caller can tell "measured None"
    from "could not measure".
    """
    try:
        return (True, call())
    except BaseException as exc:                                   # noqa: BLE001
        emit(check, error_type=type(exc).__name__, error=_short(exc))
        return (False, None)


def _tri_attr(obj, name):
    """Read a boolean-ish attribute FAITHFULLY as True / False / None (indeterminate).

    ``None`` means the reading could not be established — the attribute is missing, is itself
    ``None``, or is not a plain bool.  ``bool(getattr(obj, name, False))`` collapses all three
    of those into a fabricated ``False``: a session whose ``is_open`` cannot be read would emit
    a clean healthy ``False`` and the manifest would grade "cold" though coldness was never
    established.  Emitting ``None`` instead is what lets the grader's UNKNOWN branch fire
    (``_runner._tri`` coerces with the same non-widening rule).

    Determinate is decided by TYPE IDENTITY, not equality.  ``value in (True, False)`` is
    ``True`` for ``0``, ``0.0`` and ``1`` (``0 == False``), so a non-bool reading would be
    returned verbatim and slip past the grader's UNKNOWN branch as a fabricated cold reading.
    Only the genuine ``bool`` singletons are a determinate tri-state.
    """
    try:
        value = getattr(obj, name)
    except BaseException:                                          # noqa: BLE001
        return None
    return value if type(value) is bool else None


# ---------------------------------------------------------------------------
# base — environment and distribution metadata.  10 s.
# ---------------------------------------------------------------------------
def _read_source_version(dist):
    """Return ``(version, origin)`` read from the installed package initialiser.

    Deliberately does **not** import the package: an import would run the target's own code
    inside the metadata worker, and a broken target would take the environment report down
    with it.  The file is located through the distribution's own record and parsed, so the
    version literal and the metadata version remain two independent readings.
    """
    import ast
    import importlib.metadata as md
    import importlib.util

    top = TOP_LEVEL[dist]
    origin = None
    # ``find_spec`` resolves the finder without executing the package, and it is the only
    # reading that survives an editable install: ``locate_file`` answers relative to the
    # ``.dist-info`` directory, which for an editable install is a site-packages path that
    # holds no source at all.
    try:
        spec = importlib.util.find_spec(top)
        if spec is not None and spec.origin and os.path.isfile(spec.origin):
            origin = os.path.abspath(spec.origin)
    except BaseException:                                          # noqa: BLE001
        origin = None
    if origin is None:
        try:
            located = md.distribution(dist).locate_file(os.path.join(top, "__init__.py"))
            candidate = os.path.abspath(str(located))
            if os.path.isfile(candidate):
                origin = candidate
        except BaseException:                                      # noqa: BLE001
            origin = None
    if origin is None:
        return (None, None)
    try:
        with open(origin, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except BaseException:                                          # noqa: BLE001
        return (None, origin)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    return (node.value.value, origin)
    return (None, origin)


def _read_manifest_version(dist, origin):
    """Return the checkout's ``pyproject.toml`` version for ``dist``, or None.

    ``None`` means *no source checkout is locatable*, which is the ordinary state of a
    wheel install and is never a mismatch.  It also selects the checkout-less remedy: the
    build scripts live outside ``src/`` and ship in neither wheel, so a checkout-less
    reader cannot run the repository-relative commands.
    """
    import tomllib

    if not origin:
        return None
    # <checkout>/packages/<dist>/src/<top>/__init__.py  ->  <checkout>/packages/<dist>
    package_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(origin))))
    manifest = os.path.join(package_dir, "pyproject.toml")
    if not os.path.isfile(manifest):
        return None
    try:
        with open(manifest, "rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    except BaseException:                                          # noqa: BLE001
        return None


def _marker_is_active(entry):
    """Is this ``Requires-Dist`` entry a requirement of the install doctor is looking at?

    A ``Requires-Dist`` line may carry a PEP 508 environment marker, and two kinds of
    marker make the requirement **inactive** on a perfectly healthy machine:

    * ``pymupdf; extra == "manual"`` — an OPTIONAL extra.  Nobody is required to install
      it; ``classify_enrichment`` already documents an install without ``[manual]`` as a
      supported configuration;
    * ``foo; sys_platform == "linux"`` — a platform the reader is not on.

    Treating either as mandatory produces ``WARN  dependency.pymupdf  declared but is not
    installed`` on every clean end-user install: DEGRADED, exit 1, permanently.  That is
    the cry-wolf failure the pinned contract forbids one layer down for
    ``dependency.coverage`` — telling a user their install is degraded because *pytest* is
    absent trains them to ignore the tool, which is total loss of function.

    ``packaging`` evaluates the marker properly when it is importable.  It is not a
    declared dependency of either wheel, so when it is absent doctor falls back to the
    narrower rule that covers the extras half only, and DISCLOSES which of the two ran
    rather than letting a reader assume the full evaluation happened.
    """
    text = str(entry)
    if ";" not in text:
        return (True, "no_marker")
    # Split the two conditions the old single ``try`` conflated.  An IMPORT failure means
    # ``packaging`` is genuinely absent (it is not a declared dependency of either wheel)
    # -> the narrower extras-only fallback.  A PARSE/EVALUATE failure means ``packaging``
    # is PRESENT and THIS marker is malformed -> the fallback must NOT run, because a
    # malformed line that merely LOOKS like a bare extras selector would grade INACTIVE and
    # silently drop a dependency (a false green).  The safe direction for a diagnostic on an
    # uncertain input is ACTIVE, disclosed — never inactive.
    try:
        from packaging.requirements import Requirement
    except BaseException:                                          # noqa: BLE001
        return _fallback_marker_is_active(text)

    try:
        requirement = Requirement(text)
        if requirement.marker is None:
            return (True, "packaging")
        # ``extra`` bound to the empty string IS the "no extras selected" environment — the
        # clean end-user install this check exists to protect.
        return (bool(requirement.marker.evaluate({"extra": ""})), "packaging")
    except BaseException:                                          # noqa: BLE001
        return (True, "marker_unparseable")


def _fallback_marker_is_active(text):
    """The ``packaging``-ABSENT fallback: settle ONLY the forms it can decide SOUNDLY.

    A bare, uncomposed ``extra == "x"`` marker is inactive in the clean no-extras
    environment this check protects — the extras half that keeps ``pytest; extra == "dev"``
    from crying wolf.  A marker with ANY boolean composition (``and`` / ``or``), a second
    marker variable (``python_version``, ``sys_platform``, ...), or an extras literal that
    is not a plain, non-empty extras NAME is UNDECIDABLE here and kept ACTIVE, disclosed as
    a distinct mechanism.  Over-keeping is at worst the WARN that already exists; dropping a
    possibly-active dependency is the false green the include-all behaviour forbade.  The
    fallback's ``inactive`` verdict is thus a PROVABLE SUBSET of ``packaging``'s inactive
    verdict — never inactive where ``packaging`` would say active.
    """
    marker = text.split(";", 1)[1].strip()
    if _is_bare_extra_marker(marker):
        return (False, "fallback_extra")
    return (True, "fallback_undecidable")


# The literal is restricted to a plain, non-empty extras NAME (``[A-Za-z0-9._-]+`` — the
# same character class ``_requires_dist_names`` accepts for a distribution name), NOT the
# permissive ``[^"]+``.  Two reasons, both keeping the fallback's ``inactive`` verdict a
# PROVABLE SUBSET of ``packaging``'s:
#   * An EMPTY literal (``extra == ""``) is the "no extras selected" environment, which
#     ``packaging`` evaluates ACTIVE — matching it here would DROP a base-install
#     requirement (a false green).  ``+`` (non-empty) already refused it.
#   * A WHITESPACE / CONTROL / otherwise non-name literal is an UNCERTAIN input.  The old
#     ``[^"]+`` accepted it and called it a sound, inactive bare-extras selector; the safe
#     direction for a diagnostic on an uncertain marker is undecidable -> ACTIVE.  So such
#     a literal now falls through to ``fallback_undecidable``.
# (Both guarded directly by the agreement / fail-safe battery.)
_BARE_EXTRA_MARKER = re.compile(
    r'''\s*(?:extra\s*==\s*(?:"[A-Za-z0-9._-]+"|'[A-Za-z0-9._-]+')'''
    r'''|(?:"[A-Za-z0-9._-]+"|'[A-Za-z0-9._-]+')\s*==\s*extra)\s*\Z''')


def _is_bare_extra_marker(marker):
    """True only for a lone, uncomposed ``extra == "x"`` (or ``"x" == extra``) marker.

    This is the ONLY marker form the ``packaging``-absent fallback can settle soundly: a lone
    extras selector is inactive in the no-extras environment doctor grades.  It refuses — by
    ``\\Z``-anchored full match — anything with an ``and``/``or`` composition or a second
    marker variable (``python_version``, ``sys_platform``, ...), so those stay undecidable and
    are kept ACTIVE rather than silently dropped.  Quote- and whitespace-tolerant.
    """
    return _BARE_EXTRA_MARKER.match(marker) is not None


def _requires_dist_names():
    """Return the bare distribution names both wheels declare as ACTIVE requirements.

    The universe of dependencies doctor grades is *derived from what is installed*; only
    the critical floor is pinned.  A name is stripped of its version specifier and its
    extras bracket; its environment marker is **evaluated** (see ``_marker_is_active``),
    never merely discarded — discarding it makes every optional extra a mandatory probe.
    """
    import importlib.metadata as md
    import re

    names = []
    per_dist = {}
    inactive = []
    how = set()
    for dist in DISTRIBUTIONS:
        found = []
        try:
            requires = md.distribution(dist).requires or []
        except BaseException:                                      # noqa: BLE001
            per_dist[dist] = None
            continue
        for entry in requires:
            head = str(entry).split(";")[0]
            match = re.match(r"^\s*([A-Za-z0-9._-]+)", head)
            if not match:
                continue
            active, mechanism = _marker_is_active(entry)
            how.add(mechanism)
            if not active:
                inactive.append(match.group(1))
                continue
            found.append(match.group(1))
        per_dist[dist] = sorted(set(found))
        names.extend(found)
    return (sorted(set(names)), per_dist, sorted(set(inactive)), sorted(how))


def _cwd_shadowed_names():
    """Return the stdlib top-level names shadowed by a file in the current directory.

    A ``inspect.py`` beside the interpreter's working directory silently replaces the
    standard library module for every import in the process; the failures it produces are
    baffling and are attributed to whatever imported it last.
    """
    try:
        entries = os.listdir(os.getcwd())
    except BaseException:                                          # noqa: BLE001
        return None
    stdlib = set(getattr(sys, "stdlib_module_names", ()) or ())
    hits = []
    for entry in entries:
        stem = entry[:-3] if entry.endswith(".py") else entry
        if entry.endswith(".py") and stem in stdlib:
            hits.append(stem)
        elif stem in stdlib and os.path.isdir(os.path.join(os.getcwd(), entry)):
            if os.path.isfile(os.path.join(os.getcwd(), entry, "__init__.py")):
                hits.append(stem)
    return sorted(set(hits))


def worker_base():
    """Emit the environment snapshot and every distribution-metadata reading."""
    emit("env.python",
         version=sys.version.split()[0],
         executable=os.path.abspath(sys.executable) if sys.executable else None,
         path0=sys.path[0] if sys.path else None,
         prefix=os.path.abspath(sys.prefix))

    shadowed = _cwd_shadowed_names()
    emit("env.cwd_shadow", cwd=os.path.abspath(os.getcwd()), shadowed=shadowed)

    # Read BEFORE any optivibe import: ``_bootstrap`` setdefault()s ``netfx`` at import
    # time, so only a process that has not imported the package can see what the operator
    # actually had set.
    emit("env.pythonnet_runtime", value=os.environ.get("PYTHONNET_RUNTIME"))

    import importlib.metadata as md
    for dist in DISTRIBUTIONS:
        try:
            meta = md.version(dist)
        except BaseException:                                      # noqa: BLE001
            meta = None
        source, origin = _read_source_version(dist)
        manifest = _read_manifest_version(dist, origin)
        emit("version." + dist, meta=meta, source=source, manifest=manifest, origin=origin)

    ok, universe = _guard("dependency.universe", _requires_dist_names)
    if ok:
        names, per_dist, inactive, marker_eval = universe
        # ``inactive`` and ``marker_eval`` are disclosure, never a verdict: a reader can see
        # exactly which declared names were excluded as optional/off-platform, and by which
        # of the two mechanisms, instead of having to trust that the exclusion was right.
        emit("dependency.universe", names=names, per_dist=per_dist,
             inactive=inactive, marker_eval=marker_eval)


# ---------------------------------------------------------------------------
# pkg — imports, planes, enrichment, doors, manifests, the server object.  60 s.
# ---------------------------------------------------------------------------
def _import_name_for(dist_name):
    """Map a distribution name to the module name that proves it importable."""
    return {"pythonnet": "clr"}.get(dist_name, dist_name.replace("-", "_"))


def _probe_dependency(dist_name, declared=True, raw_name=None):
    """Return the three independent readings for one dependency.

    ``meta_version``, ``find_spec`` and a real ``import`` are deliberately all recorded:
    an implementation that grades on either of the first two reports a shadowed,
    metadata-present, unimportable module as healthy.

    ``declared`` is a PARAMETER, not a constant.  Hard-coding it True made
    ``classify_dep(declared=False, ...)`` — the row that reports a critical dependency
    missing from the metadata *and* missing from the machine — structurally unreachable
    from a real run: the executor only ever probed names it had already read out of
    ``Requires-Dist``, so a wheel whose metadata lost ``pythonnet`` produced UNKNOWN
    (exit 3, "doctor could not measure") instead of the contracted BROKEN (exit 2).

    ``raw_name`` carries the distribution's own spelling as evidence when it differs from
    the PEP 503 normalised name the check is keyed on.
    """
    import importlib
    import importlib.metadata as md
    import importlib.util

    facts = {"declared": bool(declared),
             "import_name": _import_name_for(dist_name)}
    if raw_name and raw_name != dist_name:
        facts["declared_as"] = raw_name
    facts["meta_version"] = None
    for candidate in ([raw_name, dist_name] if raw_name else [dist_name]):
        try:
            facts["meta_version"] = md.version(candidate)
            break
        except BaseException:                                      # noqa: BLE001
            continue
    try:
        facts["find_spec_found"] = importlib.util.find_spec(facts["import_name"]) is not None
    except BaseException:                                          # noqa: BLE001
        facts["find_spec_found"] = None
    try:
        importlib.import_module(facts["import_name"])
        facts["import_ok"] = True
        facts["import_error"] = None
    except BaseException as exc:                                   # noqa: BLE001
        facts["import_ok"] = False
        facts["import_error"] = type(exc).__name__ + ": " + _short(exc)
    return facts


def _plane_paths(planes):
    """Return ``{plane: path}`` for the four data planes.

    Every path comes from the reference package's own build-module constant, resolved by
    name from the contract.  Doctor contains no data filename literal: a hardcoded path is
    a second source of truth that drifts silently when the reference layer moves a file.
    """
    import importlib

    from ._contract import PLANES

    if planes is not None:
        return dict(planes)
    resolved = {}
    for plane, spec in PLANES.items():
        module_name, const_name = spec[0], spec[1]
        try:
            module = importlib.import_module("optivibe_reference." + module_name)
            resolved[plane] = getattr(module, const_name)
        except BaseException:                                      # noqa: BLE001
            resolved[plane] = None
    return resolved


def _open_plane(plane, path):
    """Open one plane's file with the reference layer's own opener, and prove it readable.

    The opener is the acceptance predicate, and a truncated file that a future opener
    learns to accept would change this answer — which is what the corruption guard pins.
    Proving the returned handle is actually usable is the *prober's* job, not this
    function's: see ``_readable``.
    """
    import importlib

    from ._contract import PLANES

    module = importlib.import_module("optivibe_reference." + PLANES[plane][0])
    if plane == "manual":
        conn = module.open_manual_corpus(path)
    else:
        opener = {"merit_operand": "open_catalog",
                  "tolerance_operand": "open_tolerance_catalog",
                  "glass": "open_glass_catalog"}[plane]
        conn = getattr(module, opener)(":memory:", path)
    if conn is None:
        # The corpus opener answers ``None`` for a partial or inconsistent build rather
        # than raising.  That is a refusal, and it must be reported as one — letting the
        # None fall through would surface as an ``AttributeError`` and describe a
        # half-built corpus as an internal doctor failure.
        raise ValueError("the reference opener refused this file (partial or inconsistent)")
    return conn


def _readable(conn):
    """Force the connection to actually touch the file, and say whether it could.

    Not belt-and-braces — this is what makes ``opens`` mean what the finding claims it
    means.  ``sqlite3.connect`` is LAZY: handed a file of arbitrary bytes it returns a
    connection quite happily and does not look at the header until something queries.  A
    prober that accepts the handle itself as proof would report a wrecked plane as healthy
    the moment any opener stopped reading eagerly, and the reader would never know.
    """
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return True


def _fingerprint(conn, table, key_column):
    """``(row_count, sha256 over the sorted key column)`` for one plane, through ``conn``.

    The content fingerprint is what replaced the pinned-token identity rule.  A token test
    asked "is this plane's token reachable, and are the other planes' tokens not" — and the
    merit catalog genuinely carries the tolerance plane's token, so that rule reported a
    healthy install as misrouted while staying blind to tolerance-wired-to-merit (the token
    is in both).  A fingerprint sees the whole key column: two catalogs of different sizes
    and different code sets cannot collide.

    Raises ``LookupError`` when the table is absent.  A connection that does not even carry
    this plane's table is a *misroute* — a fact about the target — not an unreadable probe,
    which would be a fact about doctor, and the caller separates the two.
    """
    import hashlib

    info = list(conn.execute("PRAGMA table_info(%s)" % table))
    if not info:
        raise LookupError("no table %r through this connection" % table)
    if key_column not in [row[1] for row in info]:
        raise LookupError("table %r has no column %r through this connection"
                          % (table, key_column))
    keys = sorted("" if row[0] is None else str(row[0])
                  for row in conn.execute("SELECT %s FROM %s" % (key_column, table)))
    digest = hashlib.sha256(chr(10).join(keys).encode("utf-8")).hexdigest()
    return (len(keys), digest)


def _identity_ok(plane, conn, path, reference_conn=None, detail=None):
    """Prove the wired connection carries *this* plane's data, not another plane's.

    A non-null connection property proves only that *something* was assigned; provenance
    proves it was assigned the right thing.  Two routes, one per plane kind:

    - ``manual`` is **file-backed**, so provenance is direct and decisive: the wired
      connection's own ``PRAGMA database_list`` path must resolve to the plane's own file.
      It is also the plane the live wiring defect lives on.
    - the three JSON-built planes are ``:memory:``, so ``database_list`` reports ``''`` and
      cannot discriminate at all.  Instead the wired connection's **content fingerprint**
      must equal that of a connection opened **independently from the plane's own file**.

    That crossing is real rather than a correlated second layer: the independent reference
    is the plane's own *file*, the artifact under test is the dispatcher's *routing*, and
    those are different things.  It also catches strictly more than the pinned-token rule
    it replaces — tolerance-wired-to-merit is invisible to a token test, because the merit
    catalog genuinely carries the tolerance token, and obvious to a fingerprint.

    ``None`` means the probe itself could not run — doctor's own failure, never the plane's.
    """
    from ._contract import PLANE_FINGERPRINT

    fingerprint_spec = PLANE_FINGERPRINT[plane]
    if fingerprint_spec is None:
        try:
            rows = list(conn.execute("PRAGMA database_list"))
        except BaseException:                                      # noqa: BLE001
            return None
        for row in rows:
            wired_path = row[2]
            if not wired_path:
                continue
            try:
                if os.path.samefile(wired_path, path):
                    return True
            except BaseException:                                  # noqa: BLE001
                if os.path.normcase(os.path.abspath(wired_path)) == \
                        os.path.normcase(os.path.abspath(path)):
                    return True
        return False

    table, key_column = fingerprint_spec
    try:
        actual = _fingerprint(conn, table, key_column)
    except LookupError:
        return False          # the wired connection does not even carry this plane's shape
    except BaseException:                                          # noqa: BLE001
        return None
    if detail is not None:
        detail["identity_wired"] = list(actual)
    if reference_conn is None:
        return None           # the independent oracle could not be read; do not guess
    try:
        expected = _fingerprint(reference_conn, table, key_column)
    except BaseException:                                          # noqa: BLE001
        return None           # the oracle itself is unreadable; that is doctor's failure
    if detail is not None:
        detail["identity_file"] = list(expected)
    return actual == expected


def _reference_dispatcher():
    """Build the reference Dispatcher exactly as production builds it.

    A seam, not a convenience: the misrouted-plane guard replaces this to hand back a
    dispatcher whose corpus property is bound to another plane's connection, which is the
    only fixture under which ``identity_ok`` can be shown to be load-bearing.
    """
    from optivibe_reference.server import Dispatcher

    return Dispatcher()


def _conn_attributes(dispatcher):
    """Return the dispatcher's connection-shaped public properties.

    A naming-convention proxy (``conn`` or ``*_conn``), and disclosed as one: a fifth data
    plane that follows the convention is caught and one that does not is invisible.  It is
    still better than a hand-list, because the pin and the universe come from different
    places and can therefore disagree — a hand-list from ``PLANES`` could never *discover* a
    new plane, which is the only thing this scan is for.

    Scanned on the **instance**, not on ``type(dispatcher)``: a class scan sees only
    properties and descriptors, so a plane wired as a plain ``self.new_conn = ...`` in
    ``__init__`` is invisible — and an under-counted universe makes the "something new
    appeared ungraded" WARN unreachable, i.e. a coverage check that stopped covering
    reports PASS.  ``dir(instance)`` is a superset of ``dir(type(instance))``, so every
    property this used to find is still found.  Over-inclusion is the safe direction: a
    stray public ``*_conn`` can only ADD an unpinned member (a WARN doctor is meant to
    raise), never hide one.

    Returns ``None`` — never ``[]`` — when the object cannot be scanned at all, because an
    empty universe reads as "every pinned plane vanished" (FAIL, a target defect) where the
    truth is that doctor could not measure (UNKNOWN).
    """
    try:
        names = dir(dispatcher)
    except BaseException:                                          # noqa: BLE001
        return None
    found = []
    for name in names:
        if name.startswith("_"):
            continue
        if name == "conn" or name.endswith("_conn"):
            found.append(name)
    return sorted(found)


def _enrichment_tier(plane, paths):
    """Return the build tier a catalog's own build report declares, or None."""
    try:
        with open(paths[plane], encoding="utf-8") as handle:
            return json.load(handle)["build_report"]["descriptions_state"]
    except BaseException:                                          # noqa: BLE001
        return None


def _probe_planes(paths, dispatcher, dispatcher_error=None):
    """Emit one observation per plane: presence, openability, wiring and provenance.

    ``dispatcher_error`` separates the two reasons there is no dispatcher.  A Dispatcher
    that RAISED on construction means the plane is definitively **not wired** — there is no
    object that could be holding a connection — which is a fact about the target.  A
    dispatcher that is merely absent for some other reason is a fact about doctor, and
    stays the tri-state ``None``.  Collapsing the two reported a definitive, nameable
    target defect as "doctor could not measure".
    """
    from ._contract import PLANES

    for plane in PLANES:
        path = paths.get(plane)
        facts = {"path": os.path.abspath(path) if path else None}
        present = bool(path) and os.path.isfile(path)
        facts["present"] = present
        facts["bytes"] = os.path.getsize(path) if present else None
        opens, wired, identity_ok, wired_path = None, None, None, None
        probe_conn = None
        if present:
            try:
                probe_conn = _open_plane(plane, path)
                opens = _readable(probe_conn)
            except BaseException as exc:                           # noqa: BLE001
                opens = False
                facts["open_error"] = type(exc).__name__ + ": " + _short(exc)
            if opens is True:
                attr = PLANES[plane][2]
                if dispatcher is None:
                    wired = False if dispatcher_error else None
                    if dispatcher_error:
                        facts["dispatcher_error"] = dispatcher_error
                else:
                    try:
                        live = getattr(dispatcher, attr)
                        wired = live is not None
                    except BaseException:                          # noqa: BLE001
                        live, wired = None, None
                    if wired:
                        identity_ok = _identity_ok(plane, live, path, probe_conn,
                                                   detail=facts)
                        try:
                            rows = list(live.execute("PRAGMA database_list"))
                            wired_path = rows[0][2] if rows else None
                        except BaseException:                      # noqa: BLE001
                            wired_path = None
            if probe_conn is not None:
                try:
                    probe_conn.close()
                except BaseException:                              # noqa: BLE001
                    pass
        facts.update(opens=opens, wired=wired, identity_ok=identity_ok,
                     wired_path=wired_path, conn_attr=PLANES[plane][2])
        emit("plane." + plane, **facts)


#: Cache marker for "this plane's file is not there at all", kept distinct from a query that
#: returned no row.  Collapsing the two would send an absent plane down the underivable
#: branch and turn every fresh install into exit 3.
_PLANE_ABSENT = object()


def _resolve_from_plane(door, params, paths):
    """Resolve a case's ``FROM_PLANE`` values from the plane's **own file**.

    The pinned-literal rule: pin the param NAMES, derive any value that is DATA.  A bare
    pinned glass name returns ``ambiguous_glass`` on a perfectly healthy machine — the
    catalog is built from the user's own ``.agf`` files and three of them carry that name —
    and pinning the *qualifier* instead only moves the bet onto the user's catalog set.
    Taking ``(name, catalog)`` from the catalog's own first row is one query with no
    fallback, and it cannot go stale.

    The oracle is the plane's own file, deliberately **not** the dispatcher's connection: a
    value read back out of a misrouted connection would let the door answer happily and
    hide the misroute at the tool layer.

    Three outcomes, and the domain is total:

    (a) the plane is there and the query returns a row -> the real ``(name, catalog)``;
    (b) the plane is **absent** -> the fail-loud ``PROBE_SENTINEL``, and the door is still
        CALLED.  The tool's plane gate fires before name resolution (measured against the
        real public tree), so the content cannot change the answer — but the param must be
        PRESENT, because omitting a required one is rejected at dispatch level and yields
        no inner envelope at all.  That inner envelope is the whole of the evidence a clean
        public install needs;
    (c) the plane is present and the derivation still yields nothing — a valid but EMPTY
        catalog, or an opener that throws -> ``probe_input_underivable``.  Doctor could not
        form the question on a plane that is supposedly fine, which is doctor's failure and
        never the door's.

    Returns ``(resolved_params, underivable_reason, source_plane)``.  A non-``None`` reason
    means doctor could not form the question at all.
    """
    from ._contract import FROM_PLANE, FROM_PLANE_SOURCE, PROBE_SENTINEL

    wanted = [name for name, value in params.items() if value is FROM_PLANE]
    if not wanted:
        return (dict(params), None, None)

    resolved = dict(params)
    caches = {}
    plane = None
    for name in wanted:
        plane, query, column = FROM_PLANE_SOURCE[(door, name)]
        # Keyed by (plane, query), not by query alone: two params of one door may
        # legitimately derive the same query text from different planes, and collapsing
        # them would silently answer one from the other's data.
        key = (plane, query)
        if key not in caches:
            path = (paths or {}).get(plane)
            if not path or not os.path.isfile(path):
                # Outcome (b).  No data at all — the ordinary state of a fresh install.
                # Send the sentinel and let the door refuse for the honest reason.
                caches[key] = _PLANE_ABSENT
            else:
                try:
                    conn = _open_plane(plane, path)
                except BaseException:                              # noqa: BLE001
                    # Outcome (c): the file is THERE and the opener refused it.  The plane
                    # check reports the corruption; here doctor simply could not ask.
                    return (None, "probe_input_underivable", plane)
                try:
                    caches[key] = conn.execute(query).fetchone()
                except BaseException:                              # noqa: BLE001
                    caches[key] = None
                finally:
                    try:
                        conn.close()
                    except BaseException:                          # noqa: BLE001
                        pass
        row = caches[key]
        if row is _PLANE_ABSENT:
            resolved[name] = PROBE_SENTINEL
            continue
        if row is None or column >= len(row) or row[column] is None:
            # Outcome (c).  A valid but EMPTY catalog.  UNKNOWN — never FAIL, which would
            # blame the target for doctor's own inability to ask.
            return (None, "probe_input_underivable", plane)
        resolved[name] = row[column]
    return (resolved, None, plane)


def _probe_doors(dispatcher, paths=None):
    """Call each pinned reference door once, read-only, and record BOTH envelopes.

    The outer dispatch envelope reports ``ok: true`` for a refusal — the refusal travels
    inside it.  Recording both and grading only the inner one is the difference between a
    report that notices a dead door and one that congratulates it.
    """
    from ._contract import PROBE_SENTINEL, REFERENCE_CASES

    for door, case in REFERENCE_CASES.items():
        params, underivable, source_plane = _resolve_from_plane(door, case, paths)
        if underivable is not None:
            emit("tool." + door, params=sorted(case), outer_ok=None, inner_ok=None,
                 error_family=None, probe_input_underivable=True,
                 probe_input_reason=underivable, probe_input_plane=source_plane)
            continue
        # The sentinel params are disclosed by name, so a reader can see exactly which
        # arguments were placeholders standing in for absent data rather than derived
        # values — a refusal produced by a sentinel is evidence, not an accident.
        sentinel_params = sorted(name for name, value in params.items()
                                 if value == PROBE_SENTINEL)
        facts = {"params": sorted(params),
                 "derived_params": sorted(name for name, value in case.items()
                                          if value is not params.get(name)),
                 "sentinel_params": sentinel_params,
                 "probe_input_plane": source_plane}
        try:
            envelope = dispatcher.dispatch(door, dict(params))
        except BaseException as exc:                               # noqa: BLE001
            emit("tool." + door, outer_ok=None, inner_ok=None, error_family=None,
                 error_type=type(exc).__name__, error=_short(exc))
            continue
        if not isinstance(envelope, dict):
            emit("tool." + door, outer_ok=None, inner_ok=None, error_family=None,
                 error="the dispatcher returned %s, not an envelope" % type(envelope).__name__)
            continue
        facts["outer_ok"] = envelope.get("ok")
        result = envelope.get("result")
        if isinstance(result, dict):
            facts["inner_ok"] = result.get("ok")
            facts["error_family"] = result.get("error_family") or envelope.get("error_family")
        else:
            facts["inner_ok"] = None
            facts["error_family"] = envelope.get("error_family")
        emit("tool." + door, **facts)


def _probe_harness(manifest):
    """Emit the harness import, the cold tool manifest, the composition and the server.

    ``mcp.construct`` calls the builder.  Neither ``find_spec`` nor an import can stand in
    for it: an ``mcp`` whose ``Server`` lost a decorator imports perfectly and only fails
    when the builder runs, which is the whole of the 0.1.1 defect.
    """
    # The invariant is "building the manifest does not reach the backend", which is not the
    # same statement as "clr is absent from this process": the pythonnet dependency probe
    # legitimately imports clr a few observations earlier.  Snapshot before, compare after,
    # and report BOTH so a reader can tell the two apart.
    clr_preloaded = "clr" in sys.modules
    try:
        from optivibe_harness.server import Dispatcher as HarnessDispatcher
        from optivibe_harness.session import ZemaxSession
        emit("harness.import", ok=True, error=None,
             clr_loaded=("clr" in sys.modules) and not clr_preloaded)
    except BaseException as exc:                                   # noqa: BLE001
        emit("harness.import", ok=False, error_type=type(exc).__name__, error=_short(exc))
        return

    session, harness = None, None
    try:
        session = ZemaxSession()
        harness = HarnessDispatcher(session)
        names = sorted(entry["name"] for entry in harness.list_tools())
        # FAITHFUL, not ``bool(...)``: a missing/None/unreadable ``is_open`` must reach the
        # grader as ``None`` (UNKNOWN — coldness unestablished), never as a fabricated healthy
        # ``False`` that lets the manifest read PASS "cold" when coldness was never proven.
        opened = _tri_attr(session, "is_open")
        emit("harness.manifest", ok=True, names=names,
             is_open=opened,
             clr_loaded=("clr" in sys.modules) and not clr_preloaded,
             clr_preloaded=clr_preloaded)
        if opened is not False:
            # Building the manifest must not take the seat.  If a regression makes it do so
            # anyway — OR the seat state cannot be read (``opened is None``) — the observation
            # above records what it could, and this hands the seat back on any not-definitely-
            # cold reading: a diagnostic that detects a leak and then contributes one of its
            # own has made the problem it was called about.  ``session.close`` is idempotent,
            # so closing a session that was in fact cold costs nothing.
            _guard("harness.manifest_close", session.close)
    except BaseException as exc:                                   # noqa: BLE001
        emit("harness.manifest", ok=False, error_type=type(exc).__name__, error=_short(exc),
             is_open=_tri_attr(session, "is_open") if session else None,
             clr_loaded=("clr" in sys.modules) and not clr_preloaded,
             clr_preloaded=clr_preloaded)

    composed = None
    if manifest is not None:
        composed = sorted(manifest)
        emit("composed.manifest", ok=True, names=composed, injected=True)
    else:
        try:
            from optivibe_harness.composite import CompositeDispatcher
            from optivibe_reference.server import Dispatcher as ReferenceDispatcher
            pair = CompositeDispatcher([("harness", harness),
                                        ("reference", ReferenceDispatcher())])
            composed = sorted(entry["name"] for entry in pair.list_tools())
            emit("composed.manifest", ok=True, names=composed, injected=False)
        except BaseException as exc:                               # noqa: BLE001
            emit("composed.manifest", ok=False, error_type=type(exc).__name__,
                 error=_short(exc))

    try:
        from optivibe_harness.server_mcp import build_composite_mcp_server
        from optivibe_harness.composite import CompositeDispatcher
        from optivibe_reference.server import Dispatcher as ReferenceDispatcher
        try:
            pair = CompositeDispatcher([("harness", harness),
                                        ("reference", ReferenceDispatcher())])
        except BaseException:                                      # noqa: BLE001
            pair = CompositeDispatcher([("harness", harness)])
        server = build_composite_mcp_server(pair)
        emit("mcp.construct", ok=server is not None, error=None)
    except BaseException as exc:                                   # noqa: BLE001
        emit("mcp.construct", ok=False, error_type=type(exc).__name__, error=_short(exc))


def worker_pkg(planes=None, manifest=None):
    """Emit every in-process observation: imports, planes, doors, manifests, server.

    ``planes`` and ``manifest`` are the declared injection seams.  Every guard that needs
    an unhealthy install synthesises it here rather than breaking the machine.
    """
    from ._contract import CRITICAL_DEPS, normalise_dist_name

    universe = []
    raw = os.environ.get(DEP_UNIVERSE_ENV)
    if raw:
        try:
            universe = list(json.loads(raw))
        except BaseException:                                      # noqa: BLE001
            universe = []
    if not universe:
        try:
            universe = _requires_dist_names()[0]
        except BaseException:                                      # noqa: BLE001
            universe = []
    emit("dependency.universe", names=sorted(set(universe)))

    # The check IDENTITY is the PEP 503 normalised name, and the distribution's own
    # spelling travels as evidence.  A wheel that declares ``PSUtil`` would otherwise emit
    # ``dependency.PSUtil`` while the spine expects ``dependency.psutil``: the spine id is
    # then missing, the run is INCOMPLETE/3, and coverage — which DOES normalise — passes
    # at the same time.  Two layers disagreeing about the same name is exactly the
    # acceptance-set divergence the contract normalises to prevent.
    declared = {}
    for name in universe:
        declared.setdefault(normalise_dist_name(name), name)
    # The probe set is the UNION of what is declared and what is pinned as critical.  Probing
    # only declared names makes ``classify_dep(declared=False, ...)`` unreachable, so a wheel
    # whose metadata has lost ``pythonnet`` reports UNKNOWN (exit 3) for the individual check
    # while coverage FAILs — and UNKNOWN wins, so a definitive target defect exits 3 instead
    # of the contracted BROKEN/2.
    probe_set = sorted(set(declared) | {normalise_dist_name(name)
                                        for name in CRITICAL_DEPS})
    for dist_name in probe_set:
        emit("dependency." + dist_name,
             **_probe_dependency(dist_name, declared=dist_name in declared,
                                 raw_name=declared.get(dist_name)))

    mcp_version = None
    try:
        import importlib.metadata as md
        mcp_version = md.version("mcp")
    except BaseException:                                          # noqa: BLE001
        mcp_version = None
    emit("dependency.mcp_range", version=mcp_version)

    dispatcher = None
    try:
        import optivibe_reference                                  # noqa: F401
        emit("reference.import", ok=True, error=None)
    except BaseException as exc:                                   # noqa: BLE001
        emit("reference.import", ok=False, error_type=type(exc).__name__, error=_short(exc))
        _probe_harness(manifest)
        return

    # Importing the package and BUILDING its Dispatcher are separate claims with separate
    # failure modes, so the construction gets its own observation on BOTH outcomes — a
    # silent success would leave the parent unable to distinguish "it built" from "the
    # worker died before trying".
    dispatcher, dispatcher_error = None, None
    try:
        dispatcher = _reference_dispatcher()
        emit("reference.dispatcher", ok=True, error=None)
    except BaseException as exc:                                   # noqa: BLE001
        dispatcher_error = type(exc).__name__ + ": " + _short(exc)
        emit("reference.dispatcher", ok=False, error_type=type(exc).__name__,
             error=_short(exc))
    if dispatcher is not None:
        emit("plane.universe", conn_attrs=_conn_attributes(dispatcher))

    paths = _plane_paths(planes)
    _probe_planes(paths, dispatcher, dispatcher_error)

    # The domain->plane binding is pinned once in the contract and read here, so the worker
    # that emits the tier and the grader that applies the plane boundary cannot drift.
    from ._contract import ENRICHMENT_PLANE
    for domain, tier_plane in sorted(ENRICHMENT_PLANE.items()):
        emit("enrichment." + domain, tier=_enrichment_tier(tier_plane, paths),
             plane=tier_plane,
             plane_present=bool(paths.get(tier_plane))
             and os.path.isfile(paths[tier_plane]))

    if dispatcher is not None:
        _probe_doors(dispatcher, paths)
        try:
            emit("tool.universe",
                 doors=sorted(entry["name"] for entry in dispatcher.list_tools()))
        except BaseException:                                      # noqa: BLE001
            pass

    _probe_harness(manifest)


# ---------------------------------------------------------------------------
# zemax — NetHelper, assemblies, and the write-once resolution memo.
# ---------------------------------------------------------------------------
def worker_zemax():
    """Emit the NetHelper reading and the first — and therefore only truthful — resolution.

    Doctor never calls the bootstrap's resolver directly and never re-implements
    resolution.  That function is self-modifying: its third arm teaches the initializer the
    path it just found, so every LATER call claims ``registry-autodetect`` whether or not
    the registry has an entry.  Only ``load_zosapi()``'s own first call is evidence, and
    the write-once memo is what reports it.
    """
    try:
        from optivibe_harness import _bootstrap
    except BaseException as exc:                                   # noqa: BLE001
        emit("zemax.nethelper", harness_import=False,
             error_type=type(exc).__name__, error=_short(exc))
        emit("zemax.dir", harness_import=False)
        return

    try:
        path = _bootstrap.find_nethelper()
        emit("zemax.nethelper", harness_import=True, found=True, path=os.path.abspath(path))
    except BaseException as exc:                                   # noqa: BLE001
        emit("zemax.nethelper", harness_import=True, found=False,
             error_type=type(exc).__name__, error=_short(exc))
        emit("zemax.dir", harness_import=True, nethelper=False)
        return

    try:
        _bootstrap.load_zosapi()
    except BaseException as exc:                                   # noqa: BLE001
        emit("zemax.dir", harness_import=True, nethelper=True, loaded=False,
             error_type=type(exc).__name__, error=_short(exc))
        return

    resolution = _bootstrap.resolved_zemax_dir()
    emit("zemax.dir", harness_import=True, nethelper=True, loaded=True,
         resolution=list(resolution) if resolution else None)


# ---------------------------------------------------------------------------
# boot — the real entry point, in a throwaway temp directory.
# ---------------------------------------------------------------------------
#: Left below the parent's own 30 s kill so the grandchild's timeout is reportable.
BOOT_GRANDCHILD_TIMEOUT_S = 25.0


def worker_boot():
    """Start ``python -m optivibe_harness`` with stdin at EOF and report its exit status.

    ``TEMP``, ``TMP`` and ``TMPDIR`` are pointed at a fresh empty directory for the
    grandchild.  The harness entry point sweeps orphaned engines at start-up, and that
    sweep **terminates processes**; it reads the ledger from ``tempfile.gettempdir()`` at
    call time, so a redirected grandchild reads an EMPTY ledger and reclaims nothing.  The
    redirect is what makes ``--boot`` safe to run beside a live design session rather than
    merely disclosed as risky.
    """
    import shutil
    import subprocess
    import tempfile

    scratch = tempfile.mkdtemp(prefix="optivibe-doctor-boot-")
    try:
        env = dict(os.environ)
        for key in ("TEMP", "TMP", "TMPDIR"):
            env[key] = scratch
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "optivibe_harness"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=scratch, env=env, timeout=BOOT_GRANDCHILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            emit("boot.exit", returncode=None, temp_dir=scratch, timed_out=True)
            return
        except BaseException as exc:                               # noqa: BLE001
            emit("boot.exit", returncode=None, temp_dir=scratch,
                 error_type=type(exc).__name__, error=_short(exc))
            return
        # Byte counts are recorded and never graded: a healthy EOF boot and a catastrophic
        # silent death both write nothing at all.
        emit("boot.exit", returncode=completed.returncode, temp_dir=scratch,
             stdout_bytes=len(completed.stdout or b""),
             stderr_bytes=len(completed.stderr or b""),
             stderr_head=_short((completed.stderr or b"").decode("utf-8", "replace")))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# engine — the licence proof.  Bounded end to end.
# ---------------------------------------------------------------------------
#: The create_time drift above which two same-pid snapshot rows are DIFFERENT
#: processes (the OS recycled the number).  This is the SAME gate ``_pid_is_gone``
#: reaps under — one documented tolerance, reused by both, never a second epsilon.
_PID_REUSE_TOLERANCE_S = 1.0


def _identity_unverifiable(create_time):
    """Is this recorded ``create_time`` too weak to settle ``(pid, create_time)`` identity?

    ``None`` **and** ``0.0``, by production's own rule: ``process_reaper`` documents ``0.0``
    as the AccessDenied "unknown create_time" **sentinel** and ``terminate_tracked`` refuses
    to kill on either value.  A real process's create_time is an epoch float, so ``0.0`` is
    a sentinel and never a reading — which is exactly why a *truthiness* test (``if
    create_time:``) is the wrong shape here: it silently folds "no create_time was
    recorded" into "identity checked and matched", the weaker-identity defect this whole
    sprint has been closing.  One predicate, so doctor's proof gate and production's kill
    gate cannot drift apart.

    NON-FINITE is unverifiable too, and that arm is not decoration: doctor's drift gate is
    ``abs(live - recorded) > tolerance``, and every comparison against ``nan`` is ``False``,
    so a ``nan`` recording would slide past the drift test and answer *not gone* — the same
    decisive-verdict-from-no-identity defect one value over.  (Production's gate is a bare
    ``!=``, which ``nan`` fails safe against; doctor's is a tolerance, which it does not.
    The predicate carries the arm so the two gates agree on the answer rather than on the
    expression.)
    """
    if create_time is None:
        return True
    try:
        value = float(create_time)
    except (TypeError, ValueError, OverflowError):
        return True                # an uncoercible recording proves nothing either
        # ``OverflowError`` is UNREACHABLE from either feeder — ``_pid_is_gone`` coerces the
        # env var through ``float()`` (a 400-digit literal becomes ``inf``, not a huge int)
        # and ``_same_process`` reads psutil's already-float ``ProcInfo.create_time``.  It is
        # caught anyway because an *unverifiable* recording must answer "unverifiable", and
        # a predicate whose whole job is to avoid a decisive verdict must not instead raise
        # one out of a diagnostic.
    return value == 0.0 or value != value or value in (float("inf"), float("-inf"))


def _same_process(before_info, after_info, *, on_unknown):
    """Do two engine-snapshot rows name the SAME OS process?

    Identity is ``(pid, create_time)`` — ``process_reaper``'s own PID-reuse key.  The
    caller pairs rows by pid (the dict key), so this settles the create_time half: the
    same process iff the ``create_time`` drift is within ``_PID_REUSE_TOLERANCE_S`` (the
    gate ``_pid_is_gone`` treats a live PID as recycled above).  A pid absent on one side
    (``None`` row) is trivially NOT the same.  When EITHER side's ``create_time`` fails
    ``_identity_unverifiable`` the identity is UNPROVABLE, and the caller states the
    fail-safe direction with ``on_unknown`` — ownership fails safe to OWNED, survival
    fails safe to STILL-RUNNING.

    The unverifiable test is the SHARED predicate, not a local ``is None`` — that
    narrower form was this function's third false-green: ``0.0`` is
    ``process_reaper``'s AccessDenied sentinel (``snapshot()`` synthesises it, and
    ``terminate_tracked`` REFUSES to kill on it), so a one-sided AccessDenied — realistic
    on Windows for a process that is exiting — reached the drift test as if it were an
    epoch reading.  ``abs(real - 0.0)`` is then hugely greater than the tolerance, so the
    answer was a CONFIDENT "different process" derived from NO IDENTITY AT ALL: at the
    survival site that dropped doctor's own possible survivor from ``still_running`` and
    passed a leaking clean-close; at the ownership site the symmetric ``0.0``-vs-``0.0``
    row answered "same" and excluded a recycled baseline pid from ``owned``, so the
    close-time check read empty by construction.  Both directions are now the caller's
    declared fail-safe.  One predicate, so doctor's proof gate and production's kill gate
    cannot drift apart.
    """
    if before_info is None or after_info is None:
        return False
    b = getattr(before_info, "create_time", None)
    a = getattr(after_info, "create_time", None)
    if _identity_unverifiable(b) or _identity_unverifiable(a):
        return on_unknown
    # Unreachable belt: ``_identity_unverifiable`` already routed every value whose
    # ``float()`` raises (and every non-finite) to ``on_unknown`` above, so both operands
    # are finite floats here.  Kept because a *decisive* verdict is the failure mode this
    # function keeps producing — if a value ever coerces non-deterministically, the answer
    # must still be the caller's fail-safe and never an exception out of a diagnostic.
    try:
        return abs(float(a) - float(b)) <= _PID_REUSE_TOLERANCE_S
    except (TypeError, ValueError, OverflowError):
        return on_unknown


def worker_engine():
    """Open one real session, prove the licence, and reap exactly what we spawned.

    The ordering is the contract.  ``engine.owned`` is emitted **before any property
    read**, so a parent that has to kill this worker still knows the PID it must reclaim.
    Every post-open vendor call runs under production's own watchdog — never a
    re-implementation — because a wedged .NET call cannot be interrupted from Python and an
    unbounded licence read would silently convert a diagnosis into a hang.
    """
    from optivibe_harness import _bootstrap, engine_ledger, process_reaper
    from optivibe_harness.session import ZemaxSession, _run_with_watchdog

    watchdog_s = 30.0
    before = {}
    try:
        before = process_reaper.snapshot_engine_pids()
    except BaseException as exc:                                   # noqa: BLE001
        emit("engine.snapshot", error_type=type(exc).__name__, error=_short(exc))
    try:
        ledger = engine_ledger.read_ledger()
        live = []
        for pid, record in (ledger or {}).items():
            try:
                identity = engine_ledger.engine_identity_ok(
                    int(pid), float(record.engine_create_time))
                dead = engine_ledger.is_parent_dead(
                    int(record.parent_pid), float(record.parent_create_time))
            except BaseException:                                  # noqa: BLE001
                continue
            if identity and not dead:
                live.append(int(pid))
        # Disclosure only.  A concurrent seat is the user's, and doctor never refuses to
        # run because someone else is working.
        emit("engine.seat", concurrent_pids=sorted(live),
             baseline_pids=sorted(int(pid) for pid in before))
    except BaseException as exc:                                   # noqa: BLE001
        emit("engine.seat", error_type=type(exc).__name__, error=_short(exc))

    try:
        _bootstrap.load_zosapi()
    except BaseException as exc:                                   # noqa: BLE001
        emit("engine.license", opened=False, phase="preflight",
             error_type=type(exc).__name__, error=_short(exc))
        # No session object was ever built, so there is nothing to reclaim — and saying so
        # explicitly is the point.  Emitting nothing would leave the parent to synthesise
        # UNKNOWN ("doctor could not measure") for a lifecycle that demonstrably never
        # started, turning a clean assembly failure into exit 3 instead of exit 2.
        emit("engine.cleanup", session_created=False)
        return
    emit("engine.preflight", assembly_ready=True)

    # An entitlement rejection is definitive.  The default three attempts cost ~21 s and
    # spawn three engines to reach the same verdict.
    session = ZemaxSession(max_connect_retries=1)
    owned = []
    # ``owned_open_info`` carries each owned pid's OPEN-TIME identity (its ``ProcInfo``,
    # i.e. its ``create_time``), so the close-time survival check can match on ``(pid,
    # create_time)`` — the reaper's own key — not on the pid alone.  Assigned at the top
    # so the ``finally`` can read it even on an early path.
    owned_open_info = {}
    # ``owned_known`` distinguishes "the post-open snapshot succeeded and doctor owns these
    # PIDs" (possibly the empty set) from "the post-open snapshot FAILED, so ownership is
    # INDETERMINATE".  Without it a failed snapshot leaves ``owned == []`` and the close-time
    # ``still_running = owned & after`` is empty BY CONSTRUCTION — engine.cleanup would then
    # read PASS while a leaked engine sits unmatched in the post-close table.  A clean pass
    # built on a forgotten owned-set is a false green, so the failed-snapshot case must reach
    # the grader as UNKNOWN.
    owned_known = True
    try:
        opened_ok = True
        try:
            session.open()
        except BaseException as exc:                               # noqa: BLE001
            opened_ok = False
            emit("engine.license", opened=False, phase="open",
                 error_type=type(exc).__name__, error=_short(exc))
            # DO NOT return here.  ``session.open()`` can spawn an engine (CreateApplication)
            # and THEN raise on a connect/entitlement failure — the whole engine-ledger/reap
            # machinery exists because opens leak.  The invariant is: engine.cleanup may read
            # PASS only if doctor observed the process table AFTER every action that could
            # spawn and nothing it spawned survives.  ``open()`` is such an action on BOTH its
            # success and failure paths, so the post-open snapshot below runs unconditionally;
            # an engine spawned-then-orphaned only enters ``owned = after_open - before`` if
            # that difference is actually taken.  A ``return`` here would leave ``owned == []``
            # and let the close-time ``still_running`` read empty by construction — the false
            # green.  If the snapshot itself cannot run, ``owned_known`` goes False -> UNKNOWN.
        after_open = {}
        try:
            after_open = process_reaper.snapshot_engine_pids()
        except BaseException:                                      # noqa: BLE001
            after_open = {}
            owned_known = False
        # Ownership by ``(pid, create_time)`` IDENTITY, not a pid-key set-difference.
        # If the OS recycled a baseline pid ``p`` for doctor's own engine, ``p`` sits in
        # BOTH ``before`` and ``after_open`` under the same key but a DIFFERENT
        # create_time; ``set(after_open) - set(before)`` would EXCLUDE ``p`` from
        # ``owned``, then a clean close reads PASS while doctor's engine leaks (the
        # recycled-PID false green).  The reaper keys identity on ``(pid, create_time)``
        # — the PID-reuse guard — and doctor must consume that same identity, never
        # re-derive a weaker one.  Unprovable identity fails SAFE to OWNED.
        owned = sorted(
            pid for pid, info in after_open.items()
            if not _same_process(before.get(pid), info, on_unknown=False))
        owned_open_info = {pid: after_open.get(pid) for pid in owned}
        owned_pid = owned[0] if len(owned) == 1 else None
        create_time = None
        if owned_pid is not None:
            create_time = getattr(owned_open_info.get(owned_pid), "create_time", None)
        # Emitted on both paths: a spawned-then-failed engine's PID must reach the parent's
        # reap arm just as a cleanly-opened one does.
        emit("engine.owned", pid=owned_pid, create_time=create_time,
             baseline=sorted(int(pid) for pid in before))

        if opened_ok:
            facts = {"opened": True, "owned_pid": owned_pid}
            for label, call in (
                    ("valid_license", lambda: bool(session.app.IsValidLicenseForAPI)),
                    ("license_status", lambda: str(session.app.LicenseStatus))):
                outcome, value = _run_with_watchdog(call, watchdog_s)
                facts[label] = value if outcome == "ok" else None
                facts[label + "_watchdog"] = outcome
            emit("engine.license", **facts)
    finally:
        # The lifecycle record.  ``after`` is a POST-CLOSE re-read of the OS process table,
        # and it — not ``close``'s own return value — is what the parent grades.  "The tool
        # reported a clean close" and "the process is gone" are different claims; a
        # diagnostic that leaks the single seat while reporting PASS has caused the problem
        # it was called about.  ``None`` where the snapshot itself failed, so an
        # unobservable cleanup reads UNKNOWN rather than silently as a clean one.
        outcome, _value = _run_with_watchdog(session.close, watchdog_s)
        after, after_ok = {}, True
        try:
            after = process_reaper.snapshot_engine_pids()
        except BaseException:                                      # noqa: BLE001
            after, after_ok = {}, False
        after_pids = sorted(int(pid) for pid in after) if after_ok else None
        owned_pids = sorted(int(pid) for pid in owned)
        # Survival matched on ``(pid, create_time)`` too, not the pid alone.  A pid the OS
        # recycled between close and this re-read (doctor's engine GONE, the number now a
        # foreign engine) must NOT be counted as doctor's leak (else a false RED on a clean
        # close); doctor's OWN surviving engine (same pid, same create_time) MUST be.
        # Unprovable identity fails SAFE to STILL-RUNNING (a leak flagged over a leak missed).
        still_running = (sorted(
            pid for pid in owned_pids
            if _same_process(owned_open_info.get(pid), after.get(pid), on_unknown=True))
            if after_ok else None)
        emit("engine.cleanup", session_created=True, close_watchdog=outcome,
             reap_failures=[repr(item)[:_ERROR_CHARS]
                            for item in getattr(session, "reap_failures", []) or []],
             owned_known=owned_known,
             owned_pids=owned_pids, after_pids=after_pids, still_running=still_running)


# ---------------------------------------------------------------------------
# reap — the parent's targeted recovery arm, in a child because the parent is stdlib-only.
# ---------------------------------------------------------------------------
def worker_reap():
    """Reap exactly one declared engine PID through the create-time-gated predicates.

    This exists as a *worker* for one reason: the parent may import nothing outside the
    standard library, and ``process_reaper`` needs ``psutil``.  Running the reclaim in a
    child keeps rule 1 intact and keeps the kill inside production's own guards — never a
    name sweep, never a PID that was not declared, never a PID that was already there.
    """
    from optivibe_harness import process_reaper

    try:
        pid = int(os.environ.get(REAP_PID_ENV) or 0)
    except BaseException:                                          # noqa: BLE001
        pid = 0
    if not pid:
        emit("engine.reaped", pid=None, ok=False, detail="no owned pid was declared")
        return
    # ABSENCE is spelled ``None``, never ``0.0``: an env channel carries only strings, so an
    # unset/empty/unparseable recording means *nothing was recorded* — a different fact from
    # a create_time that happens to be zero.  Overloading them is how a missing identity
    # slipped past the gate below as though it had been checked.  ``terminate_tracked``
    # refuses on ``None`` and on ``0.0`` alike, so both encodings stay safe by its own rule.
    try:
        raw_create_time = os.environ.get(REAP_CREATE_TIME_ENV)
        create_time = float(raw_create_time) if raw_create_time else None
    except BaseException:                                          # noqa: BLE001
        create_time = None
    try:
        baseline = [int(item) for item in json.loads(os.environ.get(REAP_BASELINE_ENV) or "[]")]
    except BaseException:                                          # noqa: BLE001
        baseline = []
    if pid in baseline:
        # A PID that was already running before doctor started is somebody else's engine.
        # Never a PID doctor did not itself open, and never a name sweep.
        emit("engine.reaped", pid=pid, ok=False, gone=None,
             detail="the declared pid was already running before doctor started")
        return

    reported, action, detail, error_type = False, None, "", None
    try:
        result = process_reaper.terminate_tracked(pid, create_time, baseline_pids=baseline)
        reported = bool(getattr(result, "ok", False))
        action = getattr(result, "action", None)
        detail = _short(getattr(result, "detail", ""))
    except BaseException as exc:                                   # noqa: BLE001
        error_type = type(exc).__name__
        detail = _short(exc)

    # *The tool reported a reap* and *the process is gone* are different claims, and only
    # the second is evidence.  The proof is a post-kill re-read of the OS, never the kill
    # call's own return value: a reaper that returns cheerfully while the process survives
    # is exactly the failure this disclosure exists to catch.
    gone = _pid_is_gone(pid, create_time)
    emit("engine.reaped", pid=pid, ok=(gone is True), reported_ok=reported, gone=gone,
         action=action, detail=detail, error_type=error_type)


def _pid_is_gone(pid, create_time):
    """Ask the OS whether ``pid`` is gone.  ``None`` when the OS could not be asked.

    A recycled PID is *not* the same process, so a live PID whose creation time no longer
    matches the one doctor recorded counts as gone — that is the same create-time gate
    production's reaper kills under, applied to the proof rather than to the kill.

    An ABSENT pid is gone whatever doctor recorded — nothing is left to identify.  But a
    LIVE pid whose recorded identity is unverifiable (``_identity_unverifiable``) is
    **indeterminate**, never ``False``: with no create_time to match, "this pid exists" and
    "doctor's engine survived" are different claims, and a live-but-recycled pid satisfies
    the first while contradicting the second.  Answering ``False`` there would assert
    survival on pid existence alone — the weaker identity this function exists to reject —
    so it returns ``None`` and the caller reports that it could not re-read the process.
    """
    import psutil

    try:
        process = psutil.Process(pid)
        # Order matters: NoSuchProcess above still wins, so an absent pid reads True even
        # with nothing recorded.  Only a pid that EXISTS needs identity to say more.
        if _identity_unverifiable(create_time):
            return None
        try:
            return abs(process.create_time() - create_time) > _PID_REUSE_TOLERANCE_S
        except psutil.NoSuchProcess:
            # It vanished between the lookup and the read: absence, so genuinely gone.
            return True
        except psutil.Error:
            # AccessDenied (or any other non-absence psutil error) means the identity is
            # UNREADABLE, not that the process is gone.  ``True`` here asserted a reap
            # doctor never observed: it flows straight to ``ok=(gone is True)`` and reads
            # as a clean reclaim over a process that may still be alive.  Only
            # NoSuchProcess establishes disappearance; everything else is indeterminate,
            # which is what this function's own contract already promises for an OS that
            # could not be asked.
            return None
    except psutil.NoSuchProcess:
        return True
    except BaseException:                                          # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
WORKERS = {
    "base": worker_base,
    "pkg": worker_pkg,
    "zemax": worker_zemax,
    "boot": worker_boot,
    "engine": worker_engine,
    "reap": worker_reap,
}


def main(argv=None):
    """``_worker <base|pkg|zemax|boot|engine|reap>``.

    Every body is wrapped: an escaping exception becomes one recorded observation, never a
    traceback on stdout.  A worker that cannot finish still leaves the parent everything it
    already said, and the parent synthesises UNKNOWN for the rest.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in WORKERS:
        sys.stderr.write("usage: python -m optivibe_doctor._worker {%s}\n"
                         % "|".join(WORKERS))
        return 64
    name = argv[0]
    try:
        WORKERS[name]()
    except BaseException as exc:                                   # noqa: BLE001
        emit("worker." + name, error_type=type(exc).__name__, error=_short(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
