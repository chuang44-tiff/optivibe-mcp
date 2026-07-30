"""The pure classifiers, plus the run-level state machine.

**No I/O.**  Every function here is total over its declared domain, deterministic, and
free of side effects, which is what makes the classifier tables exhaustively enumerable
rather than merely sampled.

Rule 2 of the execution model lives here: a worker emits facts, and *this* module turns
facts into a status.  Nothing downstream of a worker may hand-write a verdict.

Stdlib only.
"""
import re

from ._contract import (
    BASE_CHECKS, COVERAGE, CRITICAL_DEPS, MCP_MAX_EXCLUSIVE, MCP_MIN, REMEDY)
from ._model import NOT_REQUESTED, State, Status, Summary

# ---------------------------------------------------------------------------
# Exit codes.  0/1/2/3 come out of the state machine; 64 and 130 are the two the
# state machine never produces and the CLI raises directly.
# ---------------------------------------------------------------------------
EXIT_BY_STATE = {
    State.READY: 0,
    State.DEGRADED: 1,
    State.BROKEN: 2,
    State.INCOMPLETE: 3,
}
EXIT_USAGE = 64
EXIT_INTERRUPT = 130

#: The declared domains, published so a caller can see what "total" means here.  A test
#: MUST enumerate its own copy of these: a universe read from the thing it grades moves
#: with the mutation it is supposed to catch.
DOMAIN_PRESENT = (True, False)
DOMAIN_TRISTATE = (True, False, None)
DOMAIN_DECLARED = (True, False)
DOMAIN_META_VERSION = ("1.2.3", None)
DOMAIN_ENRICHMENT_TIER = ("enriched", "synonyms_only", "partial", None)
DOMAIN_COVERAGE_KIND = tuple(COVERAGE)
DOMAIN_PIN_MODE = ("floor", "exact")


# ---------------------------------------------------------------------------
# plane
# ---------------------------------------------------------------------------
def classify_plane(present, opens, wired, identity_ok):
    """Grade one reference data plane over ``present x opens x wired x identity_ok``.

    ``identity_ok`` is the plane's own *provenance* fact — the pinned identity token read
    back through **that plane's** connection, or (for the file-backed corpus) the wired
    connection's own database path resolving to the plane's own file.  A non-null
    connection property proves only that *something* was assigned; provenance proves it
    was assigned the right thing, which is the whole of the ``misrouted`` verdict.

    Returns ``(Status, reason)``.  ``present`` is boolean by construction (it is an
    ``isfile`` result read in a worker); the tri-states carry ``None`` for "the worker
    could not measure this", which is doctor's own failure and therefore UNKNOWN.
    """
    if present is not True:
        return (Status.WARN, "absent_expected")
    if opens is False:
        return (Status.FAIL, "corrupt")
    if opens is not True:
        return (Status.UNKNOWN, "opener_unreadable")
    if wired is False:
        return (Status.FAIL, "unwired")
    if wired is not True:
        return (Status.UNKNOWN, "wiring_unreadable")
    if identity_ok is False:
        return (Status.FAIL, "misrouted")
    if identity_ok is not True:
        return (Status.UNKNOWN, "identity_unreadable")
    return (Status.PASS, "")


# ---------------------------------------------------------------------------
# dependency
# ---------------------------------------------------------------------------
def classify_dep(name, declared, meta_version, import_ok, import_error):
    """Grade one declared dependency.  ``import_error`` is recorded, never graded.

    The load-bearing row is ``declared and meta_version is not None and not import_ok``:
    metadata present and the module unimportable is a *broken install*, not a healthy one,
    and an implementation that grades on ``meta_version is not None`` reports it healthy.
    """
    del import_error  # carried into the finding's facts; it is evidence, not a verdict.
    critical = name in CRITICAL_DEPS
    bad = Status.FAIL if critical else Status.WARN

    if declared:
        if import_ok is True:
            return (Status.PASS, "")
        if import_ok is None:
            return (Status.UNKNOWN, "not_measured")
        # import_ok is False
        if meta_version is not None:
            return (bad, "declared_but_unimportable")
        return (bad, "absent")

    if import_ok is True:
        return (Status.WARN, "undeclared_but_present")
    return (Status.FAIL if critical else Status.UNKNOWN, "undeclared_and_absent")


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------
def _origin_is_independent(origin):
    """True when ``origin`` looks like a real package initialiser and not dist metadata.

    A source version read out of a ``.dist-info`` tree is the *same* evidence as the
    metadata version, so comparing them proves nothing — that is what
    ``sources_not_independent`` names.
    """
    parts = str(origin).replace("\\", "/").split("/")
    if any(part.endswith(".dist-info") for part in parts):
        return False
    return parts[-1] == "__init__.py"


def classify_version(meta, source, manifest, origin):
    """Grade the agreement of installed metadata, source literal and packaging manifest.

    First match wins.  There is deliberately **no FAIL state**: a version disagreement
    misleads a reader, it does not break an install.  ``manifest is None`` (a wheel
    install with no checkout) is never a mismatch.
    """
    if origin is not None and not _origin_is_independent(origin):
        return (Status.UNKNOWN, "sources_not_independent")
    if meta is None and source is None:
        return (Status.UNKNOWN, "no_version_readable")
    if meta is None:
        return (Status.WARN, "metadata_missing")
    if source is None:
        # The verdict for an unreadable source lives on the import check; here we only
        # report that the metadata value stands alone.
        return (Status.WARN, "source_unreadable")
    if origin is None:
        # A source version with no provenance is not evidence.
        return (Status.UNKNOWN, "origin_unreadable")
    if meta != source:
        return (Status.WARN, "version_skew")
    if manifest is not None and manifest != source:
        # The source is what runs; the manifest is hand-editable.
        return (Status.WARN, "manifest_skew")
    return (Status.PASS, "")


# ---------------------------------------------------------------------------
# mcp supported range
# ---------------------------------------------------------------------------
_VERSION_HEAD = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _parse_version(version):
    """Return a 3-tuple of ints for the leading release segment, or None."""
    if not isinstance(version, str):
        return None
    match = _VERSION_HEAD.match(version)
    if match is None:
        return None
    return tuple(int(part) if part else 0 for part in match.groups())


def classify_mcp_range(version):
    """Grade the installed ``mcp`` version against the range pinned in ``_contract``.

    The signature takes **no metadata argument** on purpose: the supported range is a
    release contract input, never a value read back from the wheel whose behaviour it is
    supposed to bound.  An out-of-range ``mcp`` that happens to construct a server is
    still reported, because this check gates independently of ``mcp.construct``.
    """
    if version is None:
        return (Status.UNKNOWN, "version_unreadable")
    parsed = _parse_version(version)
    if parsed is None:
        return (Status.UNKNOWN, "version_unparseable")
    if MCP_MIN <= parsed < MCP_MAX_EXCLUSIVE:
        return (Status.PASS, "")
    return (Status.FAIL, "unsupported_range")


# ---------------------------------------------------------------------------
# tool
# ---------------------------------------------------------------------------
def classify_tool(tool, inner_ok, error_family, plane_status, plane_reason):
    """Grade one canned reference-tool call.

    Grades the **INNER** ``result["ok"]`` only.  The outer dispatch envelope is a recorded
    fact and is never graded: a refusal travels as ``outer ok=True`` carrying an inner
    ``ok=False``, so grading the outer envelope reports every refusal as a success.

    ``tool`` and ``plane_reason`` are both in the signature because without them the two
    cases that matter — a door refusing because its data is *absent* (normal) and a door
    refusing because its data is present but *unwired* (a defect) — are indistinguishable.
    """
    del tool  # identity is carried into the finding; the verdict comes from the plane.
    if inner_ok is True:
        return (Status.PASS, "")
    if inner_ok is None:
        return (Status.UNKNOWN, "no_envelope")
    family = error_family or ""
    if (plane_status is Status.WARN and plane_reason == "absent_expected"
            and family.endswith("_unavailable")):
        return (Status.WARN, "plane_absent")
    if plane_reason == "unwired":
        return (Status.FAIL, "present_but_unwired")
    if plane_reason == "misrouted":
        return (Status.FAIL, "plane_misrouted")
    if plane_status is Status.PASS and inner_ok is False:
        return (Status.FAIL, "refused_while_plane_ok")
    return (Status.FAIL, "malformed_envelope")


# ---------------------------------------------------------------------------
# enrichment
# ---------------------------------------------------------------------------
def classify_enrichment(tier):
    """Grade a reference build tier.

    ``synonyms_only`` is a **documented supported configuration** (an install without the
    ``[manual]`` extra) and must not gate.  The tier is read from the build report; it is
    never derived from a pending-description tally, which counts work-in-progress rather
    than capability.
    """
    if tier == "enriched":
        return (Status.PASS, "")
    if tier == "synonyms_only":
        return (Status.PASS, "synonyms_only")
    if tier == "partial":
        return (Status.WARN, "partial")
    return (Status.UNKNOWN, "tier_unreadable")


# ---------------------------------------------------------------------------
# boot
# ---------------------------------------------------------------------------
def classify_boot(returncode):
    """Grade the ``--boot`` grandchild's exit status.

    Takes **no byte counts**: a healthy server that reaches EOF on stdin and a
    catastrophic silent death both write 0 bytes to stdout and 0 to stderr, so byte counts
    cannot separate them and an implementation that grades on them grades on noise.
    """
    if returncode is None:
        return (Status.UNKNOWN, "timeout")
    if returncode == 0:
        return (Status.PASS, "")
    return (Status.FAIL, "nonzero_exit")


# ---------------------------------------------------------------------------
# env.pythonnet_runtime
# ---------------------------------------------------------------------------
def classify_pythonnet_runtime(value):
    """Grade the operator's pre-existing ``PYTHONNET_RUNTIME``.

    Unset or exactly ``netfx`` passes.  Anything else WARNs: the bootstrap only
    ``setdefault()``s ``netfx``, so a pre-set value wins, and the ZOS-API assemblies target
    the .NET Framework.  Without this arm the check would be a step that cannot fail — and
    a step that cannot fail cannot gate.
    """
    if value is None or value == "netfx":
        return (Status.PASS, "")
    return (Status.WARN, "runtime_override")


# ---------------------------------------------------------------------------
# coverage — ONE classifier, three call sites
# ---------------------------------------------------------------------------
def _difference(left, right):
    """The single set-difference implementation behind the whole coverage rule.

    Three inline copies of ``pinned - universe`` would be three rules that can drift, and
    a rule that can drift is not a rule.  Everything that needs this relation — the verdict
    and the message that names the members — comes through here.
    """
    return tuple(sorted(set(left) - set(right)))


def classify_coverage(kind, pinned, universe, probed, blocked_by):
    """Grade one ``*.coverage`` check.  First match wins — **the order IS the contract**.

    ``kind`` selects the reason tokens and the pin mode from ``COVERAGE``.  The mode is
    load-bearing rather than cosmetic: ``CRITICAL_DEPS`` is a **floor** (``matplotlib`` and
    ``numpy`` are legitimately declared and deliberately unpinned), while ``PLANES`` and
    ``REFERENCE_CASES`` are **exact identity sets**.  Under a uniform symmetric reading
    ``dependency.coverage`` would WARN on every healthy install, make exit 1 permanent, and
    train the reader to ignore the tool.

    The two failures are deliberately **not** the same severity:

    - a pinned member absent from the derived universe is ``FAIL`` — *the thing doctor
      promised to check has vanished*, and a shrunken universe passes silently, which is
      the "a check that stopped running reports green" shape;
    - a universe member the pin does not claim is ``WARN`` — *something new appeared
      ungraded*.  That is a **doctor** coverage gap, not a target defect, and grading it
      FAIL would make doctor cry wolf every time the product grows.

    Rows 3 and 4 cannot arise from a literal pin.  They exist because the domain must be
    **total**: no input tuple may reach a state the contract does not describe, which is
    the whole defect this closes.
    """
    if blocked_by:
        # Doctor measured correctly and declined to probe because a definitive
        # prerequisite already reported FAIL/WARN.  That is a statement about the target's
        # configuration, so it is SKIP — and a SKIP never gates.
        return (Status.SKIP, blocked_by)
    if universe is None:
        return (Status.UNKNOWN, "universe_unreadable")
    if pinned is None:
        return (Status.UNKNOWN, "pin_unreadable")
    if probed is None:
        return (Status.UNKNOWN, "probe_set_unreadable")
    if _difference(universe, probed):
        return (Status.UNKNOWN, "probe_incomplete")
    rules = COVERAGE.get(kind)
    if rules is None:
        return (Status.UNKNOWN, "coverage_kind_unknown")
    if _difference(pinned, universe):
        return (Status.FAIL, rules["missing"])
    if rules["pin_mode"] == "exact" and _difference(universe, pinned):
        return (Status.WARN, rules["unpinned"])
    return (Status.PASS, "")


def coverage_members(kind, pinned, universe, probed, reason):
    """The members a coverage ``reason`` names, through the same one difference.

    A second place recomputing ``pinned - universe`` for the message could disagree with
    the verdict, and a message that disagrees with its own verdict is worse than no
    message.  Returns ``()`` for a reason that names nothing.
    """
    rules = COVERAGE.get(kind) or {}
    if reason == "probe_incomplete" and universe is not None and probed is not None:
        return _difference(universe, probed)
    if universe is None or pinned is None:
        return ()
    if reason == rules.get("missing"):
        return _difference(pinned, universe)
    if reason and reason == rules.get("unpinned"):
        return _difference(universe, pinned)
    return ()


# ---------------------------------------------------------------------------
# remedies
# ---------------------------------------------------------------------------
_VENDOR_DATA = object()   # resolved against checkout_present at lookup time

_REMEDY_EXACT = {
    ("dependency.mcp_range", "unsupported_range"): "mcp_range",
    ("env.pythonnet_runtime", "runtime_override"): "runtime_override",
    ("zemax.nethelper", "absent"): "nethelper",
}
_REMEDY_FAMILY = {
    ("version", "metadata_missing"): "reinstall",
    ("version", "version_skew"): "reinstall",
    ("plane", "absent_expected"): _VENDOR_DATA,
    ("plane", "unwired"): "unwired",
    ("tool", "plane_absent"): _VENDOR_DATA,
    ("tool", "present_but_unwired"): "unwired",
}


def remedy_for(check, status, reason, checkout_present=True):
    """Return the pinned remedy string for a finding, or ``""``.

    ``checkout_present`` is the *only* thing that switches the missing-vendor-data remedy:
    the build scripts live outside ``src/`` and ship in neither wheel, so on a
    checkout-less install the repository-relative commands do not exist on disk.  A remedy
    the reader cannot run is a diagnostic failure, not a formatting detail.

    A PASS or a SKIP never carries a remedy.
    """
    if status in (Status.PASS, Status.SKIP):
        return ""
    if check == "reference.import":
        return REMEDY["reference_missing"]
    key = _REMEDY_EXACT.get((check, reason))
    if key is None:
        family = str(check).split(".")[0]
        key = _REMEDY_FAMILY.get((family, reason))
    if key is None:
        return ""
    if key is _VENDOR_DATA:
        return REMEDY["vendor_data"] if checkout_present else REMEDY["vendor_data_no_checkout"]
    return REMEDY[key]


# ---------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------
def _skip_is_explained(finding, by_check):
    """True when a SKIP names a blocker doctor is willing to accept.

    The anti-skip-creep guard: a SKIP naming nothing, naming an id that is not in this
    run, or naming a finding that PASSED, is itself a defect.  Otherwise "skip" quietly
    becomes a place to put anything inconvenient, and the run still reports green.
    """
    blocked_by = finding.blocked_by or ""
    if not blocked_by:
        return False
    if blocked_by == NOT_REQUESTED:
        return True
    blocker = by_check.get(blocked_by)
    return blocker is not None and blocker.status in (Status.FAIL, Status.WARN)


def summarize(findings, expected=BASE_CHECKS):
    """Reduce a run's findings to a ``Summary``.

    ``expected`` is the spine.  Every spine id must appear **exactly once**: a missing id
    and a duplicated id are both "doctor did not measure what it claims to have measured",
    which is INCOMPLETE and never a health claim.  Extras (derived dependency and tool
    names) never affect spine completeness.
    """
    findings = list(findings)
    expected = tuple(expected)

    seen = {}
    for finding in findings:
        seen[finding.check] = seen.get(finding.check, 0) + 1
    by_check = {}
    for finding in findings:
        by_check.setdefault(finding.check, finding)

    spine_ok = all(seen.get(check, 0) == 1 for check in expected)
    skips_ok = all(_skip_is_explained(finding, by_check)
                   for finding in findings if finding.status is Status.SKIP)
    empty = not findings

    counts = {status.value: 0 for status in Status}
    for finding in findings:
        counts[finding.status.value] += 1

    if empty or not spine_ok or not skips_ok or counts[Status.UNKNOWN.value]:
        state = State.INCOMPLETE
    elif counts[Status.FAIL.value]:
        state = State.BROKEN
    elif counts[Status.WARN.value]:
        state = State.DEGRADED
    else:
        state = State.READY

    return Summary(
        state=state,
        complete=(not empty) and spine_ok and skips_ok,
        expected=expected,
        received=tuple(finding.check for finding in findings),
        counts=counts,
        exit_code=EXIT_BY_STATE[state],
    )


def exit_code(summary):
    """Return the process exit code for a summary.  Never a health claim on its own."""
    return EXIT_BY_STATE[summary.state]
