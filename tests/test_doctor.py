"""`optivibe doctor` — the public test matrix.

Two rules govern this file.

**The oracle is never the thing under test.**  Every pinned contract value is duplicated
here as a literal and then asserted against ``optivibe_doctor._contract``.  A test that
imports its expectations from the module it grades moves with the mutation it exists to
catch, which is this project's dominant defect class.

**Every classifier domain is enumerated independently.**  The universes below are built by
``itertools.product`` over domains declared *in this file*, never from the keys of the
expectation table and never from the implementation.  A universe derived from the
expectation table cannot detect a missing row, because deleting the row deletes the point
that would have failed.

No test here asserts a version value, a tool count, a row count or a chunk count —
identities and relations only.
"""
import ast
import builtins
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from test_package import RECEIPT_ENV, _import_guard_source

from optivibe_doctor import _contract, _render, _runner, _worker
from optivibe_doctor._classify import (
    EXIT_BY_STATE, EXIT_INTERRUPT, EXIT_USAGE, classify_boot, classify_coverage,
    classify_dep, classify_enrichment, classify_mcp_range, classify_plane,
    classify_pythonnet_runtime, classify_tool, classify_version, coverage_members,
    exit_code, remedy_for, summarize)
from optivibe_doctor._model import Finding, Observation, State, Status
from optivibe_doctor._runner import evaluate

# ---------------------------------------------------------------------------
# The contract, duplicated literally.  Asserted against _contract below.
# ---------------------------------------------------------------------------
SCHEMA = "optivibe.doctor/v1"

CRITICAL_DEPS = ("mcp", "psutil", "pythonnet")

MCP_MIN = (1, 28)
MCP_MAX_EXCLUSIVE = (2, 0)

PLANE_NAMES = ("merit_operand", "tolerance_operand", "glass", "manual")

TOOL_PLANE = {
    "lookup_operand": "merit_operand",
    "lookup_glass": "glass",
    "find_glasses": "glass",
    "find_glass_pair": "glass",
    "search_reference": "manual",
}

BASE_CHECKS = (
    "env.python", "env.cwd_shadow", "env.pythonnet_runtime",
    "version.optivibe-harness", "version.optivibe-reference",
    "dependency.mcp", "dependency.psutil", "dependency.pythonnet",
    "dependency.mcp_range",
    "dependency.coverage",
    "reference.import",
    "reference.dispatcher",
    "plane.merit_operand", "plane.tolerance_operand", "plane.glass", "plane.manual",
    "plane.coverage", "enrichment.merit", "enrichment.tolerance",
    "tool.lookup_operand", "tool.lookup_glass", "tool.search_reference",
    "tool.find_glasses", "tool.find_glass_pair", "tool.coverage",
    "harness.import", "harness.manifest", "composed.manifest", "mcp.construct",
    "zemax.nethelper", "zemax.dir",
    "boot.exit",
    "engine.license",
    "engine.cleanup",
)

# The exact phrase a reader of an unwired corpus must see, and the exact claim that must
# never reach the report on any path.
UNWIRED_PHRASE = "rebuilding will not help"
FORBIDDEN_CLAIM = "not built on this machine"
FORBIDDEN_SCRIPT_SPELLING = "-m optivibe_reference.scripts"
REPO_RELATIVE_SCRIPT = "packages/optivibe-reference/scripts/"

# The value a FROM_PLANE param takes when its plane is ABSENT, so the door is still CALLED
# and the inner envelope still exists.  Deliberately not a plausible glass name: if the
# reference layer ever resolved the name before consulting the plane, the answer would be a
# loud glass_not_found instead of a quiet pass.
PROBE_SENTINEL = "__optivibe_doctor_probe__"

# enrichment domain -> the plane whose catalog JSON carries its build report.
ENRICHMENT_PLANE = {"merit": "merit_operand", "tolerance": "tolerance_operand"}

NOT_REQUESTED = "not_requested"


# ---------------------------------------------------------------------------
# Declared classifier domains.  These are the test's own universes.
# ---------------------------------------------------------------------------
D_PRESENT = (True, False)
D_TRISTATE = (True, False, None)

D_DECLARED = (True, False)
D_META_VERSION = ("7.2.2", None)
D_IMPORT_OK = (True, False, None)
D_DEP_NAME = ("psutil", "matplotlib")          # one critical, one not
_CRITICAL = {"psutil"}

D_META = ("0.1.3", "0.1.2", None)
D_SOURCE = ("0.1.3", None)
D_MANIFEST = ("0.1.3", "0.1.2", None)
ORIGIN_GOOD = os.path.join("C:", "venv", "site-packages", "optivibe_harness", "__init__.py")
ORIGIN_DIST_INFO = os.path.join(
    "C:", "venv", "site-packages", "optivibe_harness-0.1.3.dist-info", "__init__.py")
ORIGIN_NOT_INIT = os.path.join("C:", "venv", "site-packages", "optivibe_harness", "_model.py")
D_ORIGIN = (ORIGIN_GOOD, ORIGIN_DIST_INFO, ORIGIN_NOT_INIT, None)
_BAD_ORIGINS = (ORIGIN_DIST_INFO, ORIGIN_NOT_INIT)

D_ENRICHMENT = ("enriched", "synonyms_only", "partial", None, "", "unexpected")


def _graded(result):
    """Reduce a classifier's answer to a comparable ``(status value, reason)``."""
    status, reason = result
    return (status.value, reason)


# ===========================================================================
# A-1 .. A-4b  classify_plane
# ===========================================================================
PLANE_EXPECT = {}
for _o, _w, _i in itertools.product(D_TRISTATE, D_TRISTATE, D_TRISTATE):
    PLANE_EXPECT[(False, _o, _w, _i)] = ("warn", "absent_expected")
for _w, _i in itertools.product(D_TRISTATE, D_TRISTATE):
    PLANE_EXPECT[(True, False, _w, _i)] = ("fail", "corrupt")
    PLANE_EXPECT[(True, None, _w, _i)] = ("unknown", "opener_unreadable")
for _i in D_TRISTATE:
    PLANE_EXPECT[(True, True, False, _i)] = ("fail", "unwired")
    PLANE_EXPECT[(True, True, None, _i)] = ("unknown", "wiring_unreadable")
PLANE_EXPECT[(True, True, True, False)] = ("fail", "misrouted")
PLANE_EXPECT[(True, True, True, None)] = ("unknown", "identity_unreadable")
PLANE_EXPECT[(True, True, True, True)] = ("pass", "")

PLANE_UNIVERSE = list(itertools.product(D_PRESENT, D_TRISTATE, D_TRISTATE, D_TRISTATE))


def test_a1_classify_plane_domain_is_completely_declared():
    """A-1 completeness half: every point of the independently enumerated product has a
    declared expectation.  Delete a fill above and points vanish from the expectation."""
    missing = [point for point in PLANE_UNIVERSE if point not in PLANE_EXPECT]
    assert missing == [], "no declared expectation for %r" % (missing,)
    assert len(PLANE_UNIVERSE) == len(D_PRESENT) * len(D_TRISTATE) ** 3


@pytest.mark.parametrize("point", PLANE_UNIVERSE)
def test_a1_classify_plane_returns_the_declared_expectation(point):
    """A-1 / A-2 / A-3 / A-4 / A-4b: the classifier agrees with the declared table at
    every enumerated point."""
    assert _graded(classify_plane(*point)) == PLANE_EXPECT[point]


def test_a2_present_and_opening_but_unwired_is_fail_unwired():
    """A-2: the search_reference defect.  A corpus that is present and valid but not wired
    is a FAIL, not an absence — rebuilding it would not help."""
    assert _graded(classify_plane(True, True, False, True)) == ("fail", "unwired")
    assert _graded(classify_plane(True, True, False, None)) == ("fail", "unwired")


def test_a3_absent_plane_is_warn_not_fail():
    """A-3: absent vendor data is the documented fresh-install state, never breakage."""
    for point in itertools.product(D_TRISTATE, D_TRISTATE, D_TRISTATE):
        assert _graded(classify_plane(False, *point)) == ("warn", "absent_expected")


def test_a4_present_but_unopenable_is_fail_corrupt():
    """A-4: a file that exists is not a file that works — grading on isfile alone reddens."""
    assert _graded(classify_plane(True, False, True, True)) == ("fail", "corrupt")


def test_a4b_wired_to_another_planes_data_is_fail_misrouted():
    """A-4b: a truthy connection property proves assignment, not provenance."""
    assert _graded(classify_plane(True, True, True, False)) == ("fail", "misrouted")
    assert _graded(classify_plane(True, True, True, True)) == ("pass", "")


# ===========================================================================
# A-5 .. A-7  classify_dep
# ===========================================================================
DEP_EXPECT = {}
for _name, _meta, _manifest_unused in itertools.product(D_DEP_NAME, D_META_VERSION, (None,)):
    _bad = "fail" if _name in _CRITICAL else "warn"
    DEP_EXPECT[(_name, True, _meta, True)] = ("pass", "")
    DEP_EXPECT[(_name, True, _meta, None)] = ("unknown", "not_measured")
    DEP_EXPECT[(_name, True, _meta, False)] = (
        (_bad, "declared_but_unimportable") if _meta is not None else (_bad, "absent"))
    DEP_EXPECT[(_name, False, _meta, True)] = ("warn", "undeclared_but_present")
    _absent = "fail" if _name in _CRITICAL else "unknown"
    DEP_EXPECT[(_name, False, _meta, False)] = (_absent, "undeclared_and_absent")
    DEP_EXPECT[(_name, False, _meta, None)] = (_absent, "undeclared_and_absent")

DEP_UNIVERSE = list(itertools.product(D_DEP_NAME, D_DECLARED, D_META_VERSION, D_IMPORT_OK))


def test_a5_classify_dep_domain_is_completely_declared():
    """A-5 completeness half, including the declared=False half of the domain."""
    missing = [point for point in DEP_UNIVERSE if point not in DEP_EXPECT]
    assert missing == [], "no declared expectation for %r" % (missing,)
    assert len(DEP_UNIVERSE) == 2 * 2 * 2 * 3


@pytest.mark.parametrize("point", DEP_UNIVERSE)
def test_a5_classify_dep_returns_the_declared_expectation(point):
    """A-5: the classifier agrees with the declared table at every enumerated point."""
    assert _graded(classify_dep(*point, "an error")) == DEP_EXPECT[point]


def test_a6_metadata_present_but_unimportable_is_fail_for_a_critical_dep():
    """A-6 (E-3 as a unit test): the shape that a metadata-only implementation calls
    healthy.  psutil declares 7.2.2 and raises on import — that is a broken install."""
    assert classify_dep("psutil", True, "7.2.2", False, "ImportError")[0] is Status.FAIL
    assert classify_dep("psutil", True, "7.2.2", False, "ImportError")[1] == (
        "declared_but_unimportable")


def test_a7_a_non_critical_dep_warns_where_a_critical_one_fails():
    """A-7: grading every dependency critical makes a missing matplotlib gate the run."""
    assert classify_dep("matplotlib", True, None, False, "no module")[0] is Status.WARN
    assert classify_dep("psutil", True, None, False, "no module")[0] is Status.FAIL


# ===========================================================================
# A-8 .. A-10  classify_version
# ===========================================================================
VERSION_EXPECT = {}
# Filled in REVERSE precedence: later rows first, earlier rows overwrite.  There is no
# blanket default, so deleting any fill leaves points with no declared expectation.
for _manifest in (None, "0.1.3"):                                            # row 8
    VERSION_EXPECT[("0.1.3", "0.1.3", _manifest, ORIGIN_GOOD)] = ("pass", "")
VERSION_EXPECT[("0.1.3", "0.1.3", "0.1.2", ORIGIN_GOOD)] = ("warn", "manifest_skew")  # row 7
for _manifest in D_MANIFEST:                                                 # row 6
    VERSION_EXPECT[("0.1.2", "0.1.3", _manifest, ORIGIN_GOOD)] = ("warn", "version_skew")
for _meta, _manifest in itertools.product(("0.1.3", "0.1.2"), D_MANIFEST):   # row 5
    VERSION_EXPECT[(_meta, "0.1.3", _manifest, None)] = ("unknown", "origin_unreadable")
for _meta, _manifest, _origin in itertools.product(                          # row 4
        ("0.1.3", "0.1.2"), D_MANIFEST, D_ORIGIN):
    VERSION_EXPECT[(_meta, None, _manifest, _origin)] = ("warn", "source_unreadable")
for _manifest, _origin in itertools.product(D_MANIFEST, D_ORIGIN):           # row 3
    VERSION_EXPECT[(None, "0.1.3", _manifest, _origin)] = ("warn", "metadata_missing")
for _manifest, _origin in itertools.product(D_MANIFEST, D_ORIGIN):           # row 2
    VERSION_EXPECT[(None, None, _manifest, _origin)] = ("unknown", "no_version_readable")
for _meta, _source, _manifest, _origin in itertools.product(                 # row 1
        D_META, D_SOURCE, D_MANIFEST, _BAD_ORIGINS):
    VERSION_EXPECT[(_meta, _source, _manifest, _origin)] = (
        "unknown", "sources_not_independent")

VERSION_UNIVERSE = list(itertools.product(D_META, D_SOURCE, D_MANIFEST, D_ORIGIN))


def test_a8_classify_version_domain_is_completely_declared():
    """A-8 completeness half: every nullable input is enumerated and every point of the
    product has a declared outcome.  This is HIGH-8 made mechanical — the draft had no
    prescribed answer for missing metadata, missing source, origin=None or a
    manifest-only disagreement, so three implementations could have differed."""
    missing = [point for point in VERSION_UNIVERSE if point not in VERSION_EXPECT]
    assert missing == [], "no declared expectation for %r" % (missing,)
    assert len(VERSION_UNIVERSE) == 3 * 2 * 3 * 4


@pytest.mark.parametrize("point", VERSION_UNIVERSE)
def test_a8_classify_version_returns_the_declared_expectation(point):
    """A-8: the classifier agrees with the declared 8-row table at every point."""
    assert _graded(classify_version(*point)) == VERSION_EXPECT[point]


def test_a8_classify_version_never_fails():
    """A-8, stated as a contract: a version disagreement misleads, it does not break."""
    for point in VERSION_UNIVERSE:
        assert classify_version(*point)[0] is not Status.FAIL


def test_a9_absent_manifest_is_not_a_mismatch():
    """A-9: a wheel install has no checkout; manifest=None must never be a disagreement."""
    assert _graded(classify_version("0.1.3", "0.1.3", None, ORIGIN_GOOD)) == ("pass", "")


def test_a10_a_source_read_out_of_dist_info_is_not_independent_evidence():
    """A-10: reading the same file into both fields proves agreement with itself."""
    assert classify_version("0.1.3", "0.1.3", "0.1.3", ORIGIN_DIST_INFO)[0] is Status.UNKNOWN
    assert classify_version("0.1.3", "0.1.3", "0.1.3", ORIGIN_NOT_INIT)[0] is Status.UNKNOWN
    assert classify_version("0.1.3", "0.1.3", "0.1.3", ORIGIN_GOOD)[0] is Status.PASS


# ===========================================================================
# A-11  classify_enrichment
# ===========================================================================
ENRICHMENT_EXPECT = {
    "enriched": ("pass", ""),
    "synonyms_only": ("pass", "synonyms_only"),
    "partial": ("warn", "partial"),
    None: ("unknown", "tier_unreadable"),
    "": ("unknown", "tier_unreadable"),
    "unexpected": ("unknown", "tier_unreadable"),
}


def test_a11_enrichment_domain_is_completely_declared():
    assert sorted(map(repr, D_ENRICHMENT)) == sorted(map(repr, ENRICHMENT_EXPECT))


@pytest.mark.parametrize("tier", D_ENRICHMENT)
def test_a11_classify_enrichment(tier):
    """A-11: an install without the [manual] extra is a documented supported
    configuration.  Making synonyms_only gate reddens; making partial pass reddens."""
    assert _graded(classify_enrichment(tier)) == ENRICHMENT_EXPECT[tier]


# ===========================================================================
# A-12  classify_tool
# ===========================================================================
def test_a12_tool_identity_and_plane_reason_separate_the_two_refusals():
    """A-12 (HIGH-6): both doors refuse; only the plane reason says which refusal is a
    defect.  Drop `plane_reason` from the signature and the two become indistinguishable."""
    unwired = classify_tool(
        "search_reference", False, "corpus_unavailable", Status.FAIL, "unwired")
    absent = classify_tool(
        "lookup_glass", False, "glass_catalog_unavailable", Status.WARN, "absent_expected")
    assert _graded(unwired) == ("fail", "present_but_unwired")
    assert _graded(absent) == ("warn", "plane_absent")
    assert unwired != absent


def test_a12_classify_tool_grades_the_inner_envelope_only():
    """The outer dispatch envelope is a recorded fact: a refusal travels as outer ok=True
    carrying inner ok=False, so grading the outer envelope reports every refusal green."""
    assert _graded(classify_tool("lookup_glass", True, None, Status.PASS, "")) == ("pass", "")
    assert _graded(classify_tool("lookup_glass", None, None, Status.PASS, "")) == (
        "unknown", "no_envelope")
    assert _graded(classify_tool("lookup_glass", False, None, Status.PASS, "")) == (
        "fail", "refused_while_plane_ok")
    assert _graded(classify_tool(
        "search_reference", False, "x", Status.FAIL, "misrouted")) == (
        "fail", "plane_misrouted")
    assert _graded(classify_tool(
        "lookup_glass", False, "boom", Status.UNKNOWN, "opener_unreadable")) == (
        "fail", "malformed_envelope")


def test_a12_absent_plane_only_excuses_an_unavailable_family():
    """An absent plane excuses a `*_unavailable` refusal and nothing else."""
    assert _graded(classify_tool(
        "lookup_glass", False, "internal", Status.WARN, "absent_expected")) == (
        "fail", "malformed_envelope")


# ===========================================================================
# A-13  classify_mcp_range
# ===========================================================================
@pytest.mark.parametrize("version,expected", [
    ("2.0.0", ("fail", "unsupported_range")),
    ("2.0", ("fail", "unsupported_range")),
    ("2", ("fail", "unsupported_range")),
    ("1.27.9", ("fail", "unsupported_range")),
    ("1.28.0", ("pass", "")),
    ("1.28", ("pass", "")),
    ("1.29.9", ("pass", "")),
    ("1.99.99", ("pass", "")),
    (None, ("unknown", "version_unreadable")),
    ("", ("unknown", "version_unparseable")),
    ("not-a-version", ("unknown", "version_unparseable")),
])
def test_a13_classify_mcp_range(version, expected):
    assert _graded(classify_mcp_range(version)) == expected


def test_a13_classify_mcp_range_takes_no_metadata_argument():
    """A-13 signature pin: the supported range is a release contract input.  A classifier
    that could read installed metadata would grade the wheel against itself, which is
    exactly the drift HIGH-7 names."""
    import inspect
    names = list(inspect.signature(classify_mcp_range).parameters)
    assert names == ["version"], names


# ===========================================================================
# A-14  classify_boot
# ===========================================================================
@pytest.mark.parametrize("code,expected", [
    (0, ("pass", "")),
    (1, ("fail", "nonzero_exit")),
    (-1, ("fail", "nonzero_exit")),
    (None, ("unknown", "timeout")),
])
def test_a14_classify_boot(code, expected):
    assert _graded(classify_boot(code)) == expected


def test_a14_classify_boot_takes_no_byte_counts():
    """A-14 (C-3): a healthy EOF boot and a catastrophic silent death both write 0 bytes
    to stdout and 0 to stderr, so byte counts cannot separate them."""
    import inspect
    names = list(inspect.signature(classify_boot).parameters)
    assert names == ["returncode"], names


# ===========================================================================
# D-17  env.pythonnet_runtime
# ===========================================================================
@pytest.mark.parametrize("value,expected", [
    (None, ("pass", "")),
    ("netfx", ("pass", "")),
    ("coreclr", ("warn", "runtime_override")),
    ("mono", ("warn", "runtime_override")),
    ("", ("warn", "runtime_override")),
])
def test_d17_classify_pythonnet_runtime(value, expected):
    """D-17: the arm that makes this a check rather than a disclosure.  A step that cannot
    fail cannot gate."""
    assert _graded(classify_pythonnet_runtime(value)) == expected


def test_d17_runtime_override_carries_a_remedy():
    assert remedy_for("env.pythonnet_runtime", Status.WARN, "runtime_override") != ""


# ===========================================================================
# A-20 .. A-25  classify_coverage — ONE classifier, three call sites
# ===========================================================================
# The coverage contract, duplicated literally.  ``kind`` is what selects the pin mode, so
# a kind is how a test varies the mode through the pinned five-argument signature.
COVERAGE = {
    "dependency": {"pin_mode": "floor",
                   "missing": "critical_dep_undeclared", "unpinned": None},
    "plane": {"pin_mode": "exact",
              "missing": "pinned_plane_missing", "unpinned": "unpinned_plane"},
    "tool": {"pin_mode": "exact",
             "missing": "pinned_door_missing", "unpinned": "unpinned_door"},
}
FLOOR_KIND = "dependency"
EXACT_KIND = "plane"

#: The test's own coverage domains.  ``None`` (unreadable), empty (readable, nothing in it)
#: and non-empty are three genuinely different states and the classifier must separate all
#: three: conflating ``None`` with empty is precisely how an unreadable universe becomes a
#: cheerful PASS.
D_SET = (None, frozenset(), frozenset({"a"}))
D_BLOCKED = ("", "some.id")
D_PIN_MODE = ("floor", "exact")
_KIND_FOR_MODE = {"floor": FLOOR_KIND, "exact": EXACT_KIND}


def _coverage_expect(pinned, universe, probed, blocked_by, pin_mode):
    """The expectation table, written as the spec's precedence ladder — first match wins.

    Written independently of the implementation and evaluated over an independently
    enumerated product, so deleting a rung from the implementation leaves an enumerated
    point with no matching answer rather than silently shrinking the universe.
    """
    rules = COVERAGE[_KIND_FOR_MODE[pin_mode]]
    if blocked_by:
        return ("skip", blocked_by)
    if universe is None:
        return ("unknown", "universe_unreadable")
    if pinned is None:
        return ("unknown", "pin_unreadable")
    if probed is None:
        return ("unknown", "probe_set_unreadable")
    if universe - probed:
        return ("unknown", "probe_incomplete")
    if pinned - universe:
        return ("fail", rules["missing"])
    if pin_mode == "exact" and universe - pinned:
        return ("warn", rules["unpinned"])
    return ("pass", "")


COVERAGE_UNIVERSE = list(itertools.product(D_SET, D_SET, D_SET, D_BLOCKED, D_PIN_MODE))


@pytest.mark.parametrize("point", COVERAGE_UNIVERSE)
def test_a20_classify_coverage_is_total_over_an_independently_enumerated_domain(point):
    """A-20, mechanism PURE-UNIV.

    The product is built by ``itertools.product`` over domains declared in THIS file,
    never from the keys of the expectation table and never from the implementation.  A
    universe derived from the table under test cannot detect a deleted row, because
    deleting the row also deletes the point that would have failed."""
    pinned, universe, probed, blocked_by, pin_mode = point
    kind = _KIND_FOR_MODE[pin_mode]
    assert _graded(classify_coverage(kind, pinned, universe, probed, blocked_by)) == \
        _coverage_expect(pinned, universe, probed, blocked_by, pin_mode), point


def test_a20_every_enumerated_point_has_a_declared_expectation():
    """The completeness half.  Every point of the enumerated product must be answerable by
    the expectation ladder, or the domain this file claims to cover is not the domain it
    covers."""
    for point in COVERAGE_UNIVERSE:
        assert _coverage_expect(*point) is not None, point
    assert len(COVERAGE_UNIVERSE) == len(D_SET) ** 3 * len(D_BLOCKED) * len(D_PIN_MODE)


def test_a21_a_missing_pin_outranks_an_unpinned_extra():
    """A-21: precedence, proven where it bites.

    Discriminating condition: a fixture where rows 6 and 7 hold AT ONCE — ``pinned`` has a
    member the universe lacks AND the universe has a member the pin does not claim.  Under
    a single-condition fixture either precedence order gives the same answer and the test
    proves nothing at all."""
    pinned, universe, probed = frozenset({"a", "b"}), frozenset({"a", "c"}), \
        frozenset({"a", "c"})
    assert pinned - universe and universe - pinned, "the fixture must trip BOTH rows"
    status, reason = classify_coverage(EXACT_KIND, pinned, universe, probed, "")
    assert (status, reason) == (Status.FAIL, COVERAGE[EXACT_KIND]["missing"]), (
        "a vanished pinned member is FAIL and outranks an ungraded new one")


def test_a22_the_two_failures_are_deliberately_different_severities():
    """A-22: the asymmetry as ONE paired assertion.

    A single-direction test lets a symmetric implementation pass.  Asserted together, a
    rule made symmetric EITHER way reddens one half of the pair:

    - a pinned member absent from the universe is FAIL — the thing doctor promised to check
      has vanished, and a shrunken universe passes silently;
    - a universe member the pin does not claim is WARN — doctor's grading is incomplete,
      but nothing is broken, and FAIL would cry wolf every time the product grows."""
    missing = classify_coverage(EXACT_KIND, frozenset({"a", "b"}), frozenset({"a"}),
                                frozenset({"a"}), "")
    unpinned = classify_coverage(EXACT_KIND, frozenset({"a"}), frozenset({"a", "b"}),
                                 frozenset({"a", "b"}), "")
    assert (missing[0], unpinned[0]) == (Status.FAIL, Status.WARN), (
        "the two coverage failures are not the same severity: %r vs %r"
        % (missing, unpinned))


def test_a23_an_unreadable_universe_is_never_treated_as_an_empty_one():
    """A-23.  Discriminating condition: ``pinned`` must be NON-EMPTY.

    With an empty pin, ``None`` and the empty set are indistinguishable and the guard is
    vacuous.  With a non-empty pin the two mutations separate cleanly: treat an unreadable
    universe as empty and row 6 fires (FAIL); treat it as "nothing to check" and it PASSes.
    Both are health claims about a measurement that never happened."""
    pinned = frozenset({"a"})
    assert pinned, "an empty pin makes None and the empty set indistinguishable"
    assert _graded(classify_coverage(EXACT_KIND, pinned, None, frozenset(), "")) == \
        ("unknown", "universe_unreadable")


def test_a24_a_blocked_probe_is_skipped_even_when_the_pin_is_unsatisfied():
    """A-24.  Discriminating condition: BOTH conditions at once — a non-empty
    ``blocked_by`` AND a pin the universe does not satisfy.

    Test emptiness before ``blocked_by`` and this returns FAIL, blaming the target for a
    probe doctor declined to run."""
    pinned, universe = frozenset({"a", "b"}), frozenset({"a"})
    assert pinned - universe, "the fixture must also trip the FAIL row"
    assert _graded(classify_coverage(EXACT_KIND, pinned, universe, universe,
                                     "reference.import")) == ("skip", "reference.import")


def test_a25_one_input_tuple_answers_differently_under_each_pin_mode():
    """A-25.  Discriminating condition: ONE input tuple, BOTH modes.

    This is the guard that would have caught ``dependency.coverage`` warning on every
    healthy install.  ``CRITICAL_DEPS`` is a FLOOR — ``matplotlib`` and ``numpy`` are
    legitimately declared and deliberately unpinned — while ``PLANES`` and
    ``REFERENCE_CASES`` are exact identity sets.  Hardcode either mode and one half
    reddens; make the pin uniform and doctor exits 1 forever, which teaches every reader
    to ignore it."""
    pinned, universe, probed = frozenset({"a"}), frozenset({"a", "b"}), frozenset({"a", "b"})
    floor = classify_coverage(FLOOR_KIND, pinned, universe, probed, "")
    exact = classify_coverage(EXACT_KIND, pinned, universe, probed, "")
    assert _graded(floor) == ("pass", ""), (
        "a floor pin must not warn about a legitimately undeclared extra: %r" % (floor,))
    assert _graded(exact) == ("warn", COVERAGE[EXACT_KIND]["unpinned"]), (
        "an exact pin must name the ungraded new member: %r" % (exact,))
    assert floor != exact, "the pin mode is not being read at all"


def test_a25_the_declared_pin_modes_are_the_pinned_ones():
    """The contract half of A-25: the mode is a release input, not an implementation whim.

    Duplicated literally above and asserted here, so flipping ``dependency`` to ``exact``
    in the contract reddens even before any classifier runs."""
    assert _contract.COVERAGE == COVERAGE


def test_coverage_members_names_the_members_of_its_own_verdict():
    """The message and the verdict must come from the SAME difference.

    A summary that recomputed the relation for itself could name members that disagree
    with the status beside them, and a message contradicting its own verdict is worse than
    no message at all."""
    pinned, universe, probed = frozenset({"a", "b"}), frozenset({"a", "c"}), \
        frozenset({"a", "c"})
    _, reason = classify_coverage(EXACT_KIND, pinned, universe, probed, "")
    assert coverage_members(EXACT_KIND, pinned, universe, probed, reason) == ("b",)
    incomplete_reason = classify_coverage(EXACT_KIND, pinned, universe, frozenset(), "")[1]
    assert incomplete_reason == "probe_incomplete"
    assert coverage_members(EXACT_KIND, pinned, universe, frozenset(),
                            incomplete_reason) == ("a", "c")


def test_an_unknown_coverage_kind_is_unknown_rather_than_a_crash():
    """The classifier is TOTAL: no input may reach a state the contract does not describe,
    which is the whole defect the total-classifier rule closed.  An unrecognised kind is
    doctor's own failure,
    so it is UNKNOWN — never an exception escaping into the report."""
    assert _graded(classify_coverage("not_a_kind", frozenset({"a"}), frozenset({"a"}),
                                     frozenset({"a"}), "")) == \
        ("unknown", "coverage_kind_unknown")


# ===========================================================================
# A-15 .. A-18  the state machine
# ===========================================================================
def _finding(check, status=Status.PASS, reason="", blocked_by=""):
    return Finding(check=check, status=status, reason=reason, blocked_by=blocked_by)


def _spine(overrides=None):
    """A complete spine: every BASE_CHECK PASS unless the caller overrides it.

    ``overrides`` maps a check id to ``Status`` or to ``(Status, reason, blocked_by)``.
    """
    overrides = dict(overrides or {})
    findings = []
    for check in BASE_CHECKS:
        entry = overrides.pop(check, Status.PASS)
        if isinstance(entry, Status):
            entry = (entry, "", "")
        status, reason, blocked_by = entry
        findings.append(_finding(check, status, reason, blocked_by))
    assert overrides == {}, "override names a non-spine check: %r" % (sorted(overrides),)
    return findings


def _run(overrides=None, extras=()):
    findings = _spine(overrides) + list(extras)
    summary = summarize(findings, BASE_CHECKS)
    return summary, exit_code(summary)


@pytest.mark.parametrize("overrides,state,code", [
    ({}, "READY", 0),
    ({"plane.glass": Status.WARN}, "DEGRADED", 1),
    ({"plane.glass": Status.FAIL}, "BROKEN", 2),
    ({"plane.glass": Status.UNKNOWN}, "INCOMPLETE", 3),
    ({"plane.glass": Status.WARN, "plane.manual": Status.FAIL}, "BROKEN", 2),
    ({"plane.glass": Status.FAIL, "plane.manual": Status.UNKNOWN}, "INCOMPLETE", 3),
])
def test_a15_state_machine_and_exit_codes(overrides, state, code):
    """A-15: WARN and FAIL are different verdicts and must not collapse to one nonzero
    exit — 1 means usable-but-something-is-unavailable, 2 means will-not-work."""
    summary, rc = _run(overrides)
    assert summary.state.value == state
    assert rc == code
    assert EXIT_BY_STATE[State(state)] == code


def test_a16_a_missing_spine_id_is_incomplete():
    """A-16: doctor did not measure what it claims to have measured."""
    findings = [f for f in _spine() if f.check != "plane.manual"]
    summary = summarize(findings, BASE_CHECKS)
    assert summary.state is State.INCOMPLETE
    assert summary.complete is False
    assert exit_code(summary) == 3


def test_a16_a_duplicated_spine_id_is_incomplete():
    findings = _spine() + [_finding("plane.manual")]
    summary = summarize(findings, BASE_CHECKS)
    assert summary.state is State.INCOMPLETE
    assert summary.complete is False
    assert exit_code(summary) == 3


def test_a16_any_unknown_is_incomplete_but_still_complete_coverage():
    """An UNKNOWN is doctor's own failure to measure: it gates at 3, yet the spine is
    still covered, so `complete` stays true and the two signals stay distinguishable."""
    summary, rc = _run({"zemax.dir": Status.UNKNOWN})
    assert summary.state is State.INCOMPLETE
    assert summary.complete is True
    assert rc == 3


def test_a16_an_empty_run_is_incomplete_never_ready():
    for expected in (BASE_CHECKS, ()):
        summary = summarize([], expected)
        assert summary.state is State.INCOMPLETE
        assert exit_code(summary) == 3


def test_a17_a_skip_never_gates():
    """A-17 (CRIT-1): a run whose only non-PASS findings are valid SKIPs is READY/0.
    Treating SKIP as WARN turns every fresh install into a degraded one."""
    summary, rc = _run({
        "boot.exit": (Status.SKIP, "", NOT_REQUESTED),
        "engine.license": (Status.SKIP, "", NOT_REQUESTED),
    })
    assert summary.state is State.READY
    assert rc == 0
    assert summary.counts["skip"] == 2


def test_a17_a_skip_behind_a_real_blocker_still_does_not_gate_beyond_its_blocker():
    """A NetHelper-less install is DEGRADED because of the WARN, not because of the SKIP."""
    summary, rc = _run({
        "zemax.nethelper": (Status.WARN, "absent", ""),
        "zemax.dir": (Status.SKIP, "", "zemax.nethelper"),
        "boot.exit": (Status.SKIP, "", NOT_REQUESTED),
        "engine.license": (Status.SKIP, "", NOT_REQUESTED),
    })
    assert summary.state is State.DEGRADED
    assert rc == 1


@pytest.mark.parametrize("blocked_by", ["", "no.such.check", "env.python"])
def test_a18_a_skip_that_names_no_valid_blocker_is_incomplete(blocked_by):
    """A-18, the anti-skip-creep guard: a SKIP naming nothing, naming an absent id, or
    naming a PASS finding is itself a defect.  `env.python` is PASS in this run."""
    summary, rc = _run({"zemax.dir": (Status.SKIP, "", blocked_by)})
    assert summary.state is State.INCOMPLETE
    assert summary.complete is False
    assert rc == 3


def test_a18_a_skip_may_name_a_fail_or_a_warn_blocker():
    for blocker_status in (Status.FAIL, Status.WARN):
        summary, _rc = _run({
            "zemax.nethelper": (blocker_status, "absent", ""),
            "zemax.dir": (Status.SKIP, "", "zemax.nethelper"),
        })
        assert summary.complete is True


def test_a18_a_skip_may_not_name_another_skip():
    """Otherwise a chain of skips explains itself and the run reports green."""
    summary, rc = _run({
        "boot.exit": (Status.SKIP, "", NOT_REQUESTED),
        "engine.license": (Status.SKIP, "", "boot.exit"),
    })
    assert summary.state is State.INCOMPLETE
    assert rc == 3


def test_extras_never_affect_spine_completeness():
    """Derived dependency and tool names are extras; the spine is what must be covered."""
    summary, rc = _run(extras=[_finding("dependency.matplotlib", Status.WARN)])
    assert summary.complete is True
    assert summary.state is State.DEGRADED
    assert rc == 1


# ===========================================================================
# A-19  the behaviour matrix, row by row
# ===========================================================================
_PLANES_ALL = tuple("plane." + name for name in PLANE_NAMES)
_TOOLS_ALL = tuple("tool." + name for name in sorted(TOOL_PLANE))
_ENRICHMENT_ALL = ("enrichment.merit", "enrichment.tolerance")

_DEFAULT_SKIPS = {
    "boot.exit": (Status.SKIP, "", NOT_REQUESTED),
    "engine.license": (Status.SKIP, "", NOT_REQUESTED),
    "engine.cleanup": (Status.SKIP, "", NOT_REQUESTED),
}


def _rows(*maps):
    merged = dict(_DEFAULT_SKIPS)
    for entry in maps:
        merged.update(entry)
    return merged


def _all(checks, entry):
    return {check: entry for check in checks}


_REFERENCE_BLOCKED = _all(
    _PLANES_ALL + _TOOLS_ALL + _ENRICHMENT_ALL
    + ("reference.dispatcher", "plane.coverage", "tool.coverage"),
    (Status.SKIP, "", "reference.import"))
_HARNESS_BLOCKED = _all(
    ("harness.manifest", "composed.manifest", "mcp.construct",
     "zemax.nethelper", "zemax.dir"),
    (Status.SKIP, "", "harness.import"))

BEHAVIOUR_MATRIX = [
    ("healthy dev box, all data built, wiring fixed",
     _rows({}), (), "READY", 0),

    ("healthy dev box today (corpus built, dispatcher unwired)",
     _rows({"plane.manual": (Status.FAIL, "unwired", ""),
            "tool.search_reference": (Status.FAIL, "present_but_unwired", "")}),
     (), "BROKEN", 2),

    ("partly provisioned - catalogs built, corpus absent, OpticStudio present",
     _rows({"plane.manual": (Status.WARN, "absent_expected", ""),
            "tool.search_reference": (Status.WARN, "plane_absent", "")}),
     (), "DEGRADED", 1),

    ("clean public install - no vendor data, no OpticStudio",
     _rows(_all(_PLANES_ALL, (Status.WARN, "absent_expected", "")),
           _all(_TOOLS_ALL, (Status.WARN, "plane_absent", "")),
           {"zemax.nethelper": (Status.WARN, "absent", ""),
            "zemax.dir": (Status.SKIP, "", "zemax.nethelper")}),
     (), "DEGRADED", 1),

    ("unsupported mcp - mcp 2.0.0, the 0.1.1 defect",
     _rows({"dependency.mcp_range": (Status.FAIL, "unsupported_range", ""),
            "mcp.construct": (Status.FAIL, "builder_raised", "")}),
     (), "BROKEN", 2),

    ("mcp absent entirely",
     _rows({"dependency.mcp": (Status.FAIL, "absent", ""),
            "dependency.mcp_range": (Status.UNKNOWN, "version_unreadable", ""),
            "mcp.construct": (Status.FAIL, "builder_raised", "")}),
     (), "INCOMPLETE", 3),

    ("psutil absent",
     _rows(_REFERENCE_BLOCKED, _HARNESS_BLOCKED,
           {"dependency.psutil": (Status.FAIL, "absent", ""),
            "reference.import": (Status.FAIL, "unimportable", ""),
            "harness.import": (Status.FAIL, "unimportable", "")}),
     (), "BROKEN", 2),

    ("psutil metadata present, module broken (E-3)",
     _rows(_REFERENCE_BLOCKED, _HARNESS_BLOCKED,
           {"dependency.psutil": (Status.FAIL, "declared_but_unimportable", ""),
            "reference.import": (Status.FAIL, "unimportable", ""),
            "harness.import": (Status.FAIL, "unimportable", "")}),
     (), "BROKEN", 2),

    ("pythonnet absent",
     _rows({"dependency.pythonnet": (Status.FAIL, "absent", ""),
            "zemax.dir": (Status.SKIP, "", "dependency.pythonnet")}),
     (), "BROKEN", 2),

    ("matplotlib absent (non-critical, a derived extra)",
     _rows({}), (_finding("dependency.matplotlib", Status.WARN, "absent"),),
     "DEGRADED", 1),

    ("optivibe_reference not installed",
     _rows(_REFERENCE_BLOCKED, {"reference.import": (Status.FAIL, "absent", "")}),
     (), "BROKEN", 2),

    ("pip -e at 0.1.2, source at 0.1.3",
     _rows({"version.optivibe-harness": (Status.WARN, "version_skew", "")}),
     (), "DEGRADED", 1),

    ("wheel install, no checkout",
     _rows(_all(_PLANES_ALL, (Status.WARN, "absent_expected", "")),
           _all(_TOOLS_ALL, (Status.WARN, "plane_absent", "")),
           {"zemax.nethelper": (Status.WARN, "absent", ""),
            "zemax.dir": (Status.SKIP, "", "zemax.nethelper")}),
     (), "DEGRADED", 1),

    ("truncated manual corpus",
     _rows({"plane.manual": (Status.FAIL, "corrupt", ""),
            "tool.search_reference": (Status.FAIL, "malformed_envelope", "")}),
     (), "BROKEN", 2),

    ("manual connection misrouted to another plane",
     _rows({"plane.manual": (Status.FAIL, "misrouted", ""),
            "tool.search_reference": (Status.FAIL, "plane_misrouted", "")}),
     (), "BROKEN", 2),

    ("enrichment partial",
     _rows({"enrichment.merit": (Status.WARN, "partial", "")}), (), "DEGRADED", 1),

    ("enrichment synonyms_only (no [manual] extra)",
     _rows({"enrichment.merit": (Status.PASS, "synonyms_only", ""),
            "enrichment.tolerance": (Status.PASS, "synonyms_only", "")}),
     (), "READY", 0),

    ("PYTHONNET_RUNTIME pre-set to a non-netfx value",
     _rows({"env.pythonnet_runtime": (Status.WARN, "runtime_override", "")}),
     (), "DEGRADED", 1),

    ("cwd shadows a stdlib module",
     _rows({"env.cwd_shadow": (Status.FAIL, "shadowed", "")}), (), "BROKEN", 2),

    ("a worker dies or times out",
     _rows(_all(("zemax.nethelper", "zemax.dir"), (Status.UNKNOWN, "worker_died", ""))),
     (), "INCOMPLETE", 3),

    ("--engine, licence proof failed",
     _rows({"engine.license": (Status.FAIL, "session_proof_failed", "")}),
     (), "BROKEN", 2),

    ("--engine, a concurrent seat is live (disclosure only)",
     _rows({"engine.license": (Status.PASS, "", "")}),
     (_finding("engine.seat", Status.PASS, "concurrent_seat_disclosed"),), "READY", 0),

    ("--engine, worker killed at the overall deadline",
     _rows({"engine.license": (Status.UNKNOWN, "deadline_exceeded", "")}),
     (), "INCOMPLETE", 3),

    ("--boot with mcp 2.0.0",
     _rows({"dependency.mcp_range": (Status.FAIL, "unsupported_range", ""),
            "mcp.construct": (Status.FAIL, "builder_raised", ""),
            "boot.exit": (Status.FAIL, "nonzero_exit", "")}),
     (), "BROKEN", 2),
]


@pytest.mark.parametrize(
    "label,overrides,extras,state,code",
    BEHAVIOUR_MATRIX, ids=[row[0] for row in BEHAVIOUR_MATRIX])
def test_a19_behaviour_matrix(label, overrides, extras, state, code):
    """A-19: every row of the behaviour matrix is an executable assertion.

    The matrix is what caught CRIT-1 — a state machine that could not produce the exit
    code the contract promised for a fresh install — so the matrix becomes its own
    detector rather than prose that agrees with itself.
    """
    summary, rc = _run(overrides, extras)
    assert summary.state.value == state, "%s: %s" % (label, summary.state.value)
    assert rc == code, "%s: exit %d" % (label, rc)


def test_a19_the_partly_provisioned_install_holds_zero_failures():
    """The partly-provisioned install's acceptance, in the approved wording: the overall
    state is the healthy one and NO finding is FAIL.  An absent corpus is normal, not a
    fault."""
    row = dict(BEHAVIOUR_MATRIX[2][1])
    summary, rc = _run(row)
    findings = _spine(row)
    assert [f.check for f in findings if f.status is Status.FAIL] == []
    assert summary.counts["fail"] == 0
    assert rc == 1


def test_a19_the_unsupported_mcp_pairing_is_a_conjunction():
    """The unsupported-mcp pairing: mcp.construct FAILs while harness.manifest PASSES *in the same
    run*.  Two independent row assertions would each pass under a broad breakage that
    took both down, which is precisely what the pairing exists to exclude."""
    row = dict(BEHAVIOUR_MATRIX[4][1])
    findings = _spine(row)
    by_check = {f.check: f for f in findings}
    assert by_check["mcp.construct"].status is Status.FAIL
    assert by_check["harness.manifest"].status is Status.PASS
    assert by_check["dependency.mcp_range"].status is Status.FAIL


def test_a19_matrix_covers_every_documented_row():
    """A row silently dropped from the matrix is a behaviour nobody asserts."""
    assert len(BEHAVIOUR_MATRIX) == 24
    assert len({row[0] for row in BEHAVIOUR_MATRIX}) == len(BEHAVIOUR_MATRIX)


# ===========================================================================
# D-5, D-6, D-13, D-14, D-16  the pinned contract, crossed against its literals
# ===========================================================================
def test_contract_literals_match_this_files_duplicates():
    """The drift assertion.  These values are release contract inputs; this file holds an
    independent copy so a change to either side reddens."""
    assert _contract.SCHEMA == SCHEMA
    assert _contract.CRITICAL_DEPS == CRITICAL_DEPS
    assert _contract.MCP_MIN == MCP_MIN
    assert _contract.MCP_MAX_EXCLUSIVE == MCP_MAX_EXCLUSIVE
    assert _contract.TOOL_PLANE == TOOL_PLANE
    assert _contract.BASE_CHECKS == BASE_CHECKS
    assert tuple(sorted(_contract.PLANES)) == tuple(sorted(PLANE_NAMES))
    assert _contract.PROBE_SENTINEL == PROBE_SENTINEL
    assert _contract.ENRICHMENT_PLANE == ENRICHMENT_PLANE


def test_the_enrichment_plane_binding_names_real_planes_and_real_spine_ids():
    """A domain bound to a plane that does not exist would silently never SKIP, and a
    domain with no spine id would grade nothing at all."""
    for domain, plane in ENRICHMENT_PLANE.items():
        assert plane in PLANE_NAMES, (domain, plane)
        assert "enrichment." + domain in BASE_CHECKS, domain
        assert "plane." + plane in BASE_CHECKS, plane
    assert {check for check in BASE_CHECKS
            if check.startswith("enrichment.")} == {
                "enrichment." + domain for domain in ENRICHMENT_PLANE}


def test_base_checks_has_no_duplicate_id():
    """The spine is a set of identities; a duplicate would make coverage unprovable."""
    assert len(BASE_CHECKS) == len(set(BASE_CHECKS))


def test_d13_every_pinned_door_has_a_plane_and_a_canned_case():
    """D-13: a door added without a plane binding grades against nothing."""
    assert set(_contract.TOOL_PLANE) == set(_contract.REFERENCE_CASES)
    assert set(_contract.TOOL_PLANE.values()) <= set(_contract.PLANES)
    for tool in _contract.TOOL_PLANE:
        assert "tool." + tool in BASE_CHECKS
    for plane in _contract.PLANES:
        assert "plane." + plane in BASE_CHECKS


def test_d5_build_script_remedies_use_the_repository_relative_form():
    """D-5: `packages/optivibe-reference/scripts/` is outside `src/` and ships in neither
    wheel, so there is no module spelling that resolves in any install mode."""
    for key, text in sorted(_contract.REMEDY.items()):
        assert FORBIDDEN_SCRIPT_SPELLING not in text, key
    for key in ("vendor_data", "vendor_data_no_checkout"):
        assert REPO_RELATIVE_SCRIPT in _contract.REMEDY[key], key


def test_d6_the_unwired_remedy_says_rebuilding_will_not_help():
    """D-6: the corpus is valid and present; a rebuild changes nothing.  Reusing the
    missing-data remedy sends the reader on an hour of pointless work."""
    assert UNWIRED_PHRASE in _contract.REMEDY["unwired"]
    assert UNWIRED_PHRASE not in _contract.REMEDY["vendor_data"]
    assert UNWIRED_PHRASE not in _contract.REMEDY["vendor_data_no_checkout"]


def test_d6_the_unwired_path_never_emits_the_missing_data_remedy():
    for check, reason in (("plane.manual", "unwired"),
                          ("tool.search_reference", "present_but_unwired")):
        emitted = remedy_for(check, Status.FAIL, reason)
        assert emitted == _contract.REMEDY["unwired"]
        assert emitted != _contract.REMEDY["vendor_data"]
        assert emitted != _contract.REMEDY["vendor_data_no_checkout"]


def test_d16_the_missing_data_remedy_is_checkout_aware():
    """D-16: the build scripts ship in neither wheel.  A checkout-less install told to run
    them is told to run commands that are not on its disk."""
    for check, reason in (("plane.glass", "absent_expected"),
                          ("tool.lookup_glass", "plane_absent")):
        with_checkout = remedy_for(check, Status.WARN, reason, checkout_present=True)
        without = remedy_for(check, Status.WARN, reason, checkout_present=False)
        assert with_checkout == _contract.REMEDY["vendor_data"]
        assert without == _contract.REMEDY["vendor_data_no_checkout"]
        assert with_checkout != without
    assert "clone the repository" in _contract.REMEDY["vendor_data_no_checkout"]


def test_c3_every_absent_plane_carries_a_non_empty_remedy():
    """C-3, in its pure half: absence is reported as normal *with a way forward*."""
    for plane in _contract.PLANES:
        for checkout in (True, False):
            assert remedy_for("plane." + plane, Status.WARN, "absent_expected",
                              checkout_present=checkout) != ""


def test_a_passing_or_skipped_finding_never_carries_a_remedy():
    for status in (Status.PASS, Status.SKIP):
        assert remedy_for("plane.glass", status, "absent_expected") == ""
        assert remedy_for("dependency.mcp_range", status, "unsupported_range") == ""


def test_d14_no_pinned_string_claims_the_corpus_is_not_built_on_this_machine():
    """D-14: the reference tool's own refusal text says "not built on this machine", and
    on a machine where the corpus IS built that claim is false.  Doctor must not launder
    it.  This is the static half; the fixture half rides the worker seams."""
    for key, text in sorted(_contract.REMEDY.items()):
        assert FORBIDDEN_CLAIM not in text, key


def test_d14_no_doctor_source_file_contains_the_forbidden_claim():
    package_dir = os.path.dirname(os.path.abspath(_contract.__file__))
    scanned = 0
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith(".py"):
            continue
        scanned += 1
        with open(os.path.join(package_dir, name), encoding="utf-8") as handle:
            assert FORBIDDEN_CLAIM not in handle.read(), name
    assert scanned > 0, "the scan read no source file under %r" % package_dir


def test_c4_no_doctor_source_file_hardcodes_a_reference_data_filename():
    """C-4, AST half in its static form: every plane path must come from the reference
    package's own constants.  A hardcoded path that happens to be correct today is a
    silent time bomb, and only the literal scan catches it."""
    forbidden = ("manual_corpus.db", "glass_catalog.json", "operand_catalog.json",
                 "tolerance_operand_catalog.json")
    package_dir = os.path.dirname(os.path.abspath(_contract.__file__))
    scanned = 0
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith(".py"):
            continue
        scanned += 1
        with open(os.path.join(package_dir, name), encoding="utf-8") as handle:
            source = handle.read()
        for literal in forbidden:
            assert literal not in source, "%s hardcodes %r" % (name, literal)
    assert scanned > 0, "the scan read no source file under %r" % package_dir


def test_exit_code_constants_are_the_pinned_ones():
    assert EXIT_BY_STATE[State.READY] == 0
    assert EXIT_BY_STATE[State.DEGRADED] == 1
    assert EXIT_BY_STATE[State.BROKEN] == 2
    assert EXIT_BY_STATE[State.INCOMPLETE] == 3
    assert EXIT_USAGE == 64
    assert EXIT_INTERRUPT == 130


# ===========================================================================
# D-9  the two renderers cover the same id set
# ===========================================================================
_RENDER_SAMPLE = [
    Finding("env.python", Status.PASS, "", "3.11.9  /usr/bin/python"),
    Finding("plane.manual", Status.FAIL, "unwired",
            "present and opens but the dispatcher did not wire it",
            {"present": True, "opens": True, "wired": False, "identity_ok": None},
            _contract.REMEDY["unwired"]),
    Finding("plane.glass", Status.WARN, "absent_expected", "not present",
            {"present": False}, _contract.REMEDY["vendor_data"]),
    Finding("zemax.dir", Status.SKIP, "", "", {}, "", "zemax.nethelper"),
    Finding("engine.license", Status.SKIP, "",
            "not requested; pass --engine to open a session and prove the licence",
            {}, "", NOT_REQUESTED),
]


def _human_text(findings, summary=None):
    import io
    buffer = io.StringIO()
    renderer = _render.HumanRenderer(buffer)
    renderer.start()
    for finding in findings:
        renderer.finding(finding)
    if summary is not None:
        renderer.summary(summary)
    return buffer.getvalue()


def _ndjson_records(findings, summary=None):
    import io
    import json
    buffer = io.StringIO()
    renderer = _render.NdjsonRenderer(buffer)
    renderer.start()
    for finding in findings:
        renderer.finding(finding)
    if summary is not None:
        renderer.summary(summary)
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line]


def test_d9_human_and_ndjson_renderers_cover_the_same_id_set():
    """D-9: drop an item from one renderer and the two id sets diverge."""
    text = _human_text(_RENDER_SAMPLE)
    human_ids = {line.split()[1] for line in text.splitlines()[1:]
                 if line[:4].strip() in {"PASS", "WARN", "FAIL", "UNKN", "SKIP"}}
    machine_ids = {record["check"] for record in _ndjson_records(_RENDER_SAMPLE)}
    assert human_ids == machine_ids
    assert human_ids == {f.check for f in _RENDER_SAMPLE}


def test_ndjson_lines_parse_independently_and_carry_the_schema():
    summary, _rc = _run()
    records = _ndjson_records(_RENDER_SAMPLE, summary)
    assert len(records) == len(_RENDER_SAMPLE) + 1
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))
    for record in records:
        assert record["schema"] == SCHEMA
    assert records[-1]["type"] == "summary"
    assert records[-1]["state"] == "READY"
    assert records[-1]["exit_code"] == 0
    assert records[-1]["expected"] == len(BASE_CHECKS)


def test_ndjson_facts_carry_the_whole_crossing_verbatim():
    """A consumer must be able to re-derive the verdict without trusting it."""
    record = _ndjson_records([_RENDER_SAMPLE[1]])[0]
    assert record["facts"] == {
        "present": True, "opens": True, "wired": False, "identity_ok": None}
    assert record["status"] == "fail"
    assert record["reason"] == "unwired"


def test_ndjson_never_raises_on_an_unserialisable_fact():
    """The parent must always finish its report; a stray object is coerced, not fatal."""
    finding = Finding("env.python", Status.PASS, "", "x", {"weird": object()})
    record = _ndjson_records([finding])[0]
    assert isinstance(record["facts"]["weird"], str)


def test_human_report_is_single_line_per_message_and_holds_no_traceback():
    """`no tracebacks` is a contract, not a habit: a worker's error text is one line."""
    finding = Finding("mcp.construct", Status.FAIL, "builder_raised",
                      "Traceback (most recent call last):\n  File x\nAttributeError: y")
    lines = _render.human_finding_lines(finding)
    assert lines[0].startswith("FAIL  mcp.construct")
    assert "\n" not in "".join(lines)
    assert not any(line.lstrip().startswith("File ") for line in lines)


def test_human_skip_line_names_its_blocker():
    lines = _render.human_finding_lines(_RENDER_SAMPLE[3])
    assert lines == ["SKIP  zemax.dir                 blocked by zemax.nethelper"]


def test_human_summary_line_shape():
    summary, _rc = _run({"plane.glass": Status.WARN})
    lines = _render.human_summary_lines(summary)
    assert lines[0] == ""
    assert lines[1].startswith("SUMMARY DEGRADED complete=true ")
    assert lines[1].endswith(" exit=1")


def test_human_header_names_the_schema():
    assert _human_text([]).splitlines()[0] == "optivibe doctor  schema=" + SCHEMA


# ===========================================================================
# ==  THE WORKER / RUNNER LAYER  ============================================
# ===========================================================================
# Everything below drives the I/O half of doctor: the spawn seam, the workers,
# the CLI.  Two synthesis mechanisms recur and they are not interchangeable.
#
#   SHADOW   — a module or package on ``sys.path`` that IMPORTS but misbehaves.
#              The only mechanism that reproduces "installed but broken", which
#              is the shape of the defect doctor exists to catch.
#   METAPATH — ``_import_guard_source`` from test_package.  Reproduces genuine
#              ABSENCE.  Its ``find_spec`` RAISES, so it must never carry a
#              mutate-fails claim about a ``find_spec`` substitution: the
#              mutation would redden for the wrong reason, or not at all.
#
# Every seam guard below states the discriminating condition its fixture
# carries.  A fixture that cannot tell a correct implementation from a broken
# one is not a test, however correct its assertion.

DOCTOR_PACKAGE = os.path.dirname(os.path.abspath(_runner.__file__))
DOCTOR_SRC = os.path.dirname(DOCTOR_PACKAGE)

#: The parent modules.  These may import nothing outside the standard library and may
#: perform no filesystem I/O at all: on Windows a stalled UNC entry makes a metadata read
#: or a directory listing block, and it would block BEFORE any killable worker exists.
PARENT_MODULES = ("__main__", "_runner", "_render", "_classify", "_model", "_contract")
ALL_DOCTOR_MODULES = PARENT_MODULES + ("__init__", "_worker")

#: The em-dash the pinned remedies carry, and two console codepages that cannot encode it.
#: These are the classic Windows console encodings — the bare box doctor exists for.
EM_DASH = "—"
UNENCODABLE_CODEPAGES = ("cp437", "cp850")

PLANE_NAMES_SET = set(PLANE_NAMES)


def _checkout_root():
    """Walk up for the directory holding both packaging manifests, or None.

    A search rather than a fixed number of ``dirname`` calls because this file is authored
    at one depth and shipped at another; a fixed depth resolves to the wrong directory in
    one of the two layouts and the test quietly stops checking anything.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    while True:
        if all(os.path.isfile(os.path.join(here, "packages", name, "pyproject.toml"))
               for name in ("optivibe-harness", "optivibe-reference")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _doctor_source(module):
    path = os.path.join(DOCTOR_PACKAGE, module + ".py")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _doctor_tree(module):
    return ast.parse(_doctor_source(module))


def _child_env(extra=None):
    """An environment in which a child can find the doctor package."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([DOCTOR_SRC] + ([existing] if existing else []))
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def _run_doctor(argv=(), env=None, cwd=None, timeout=300):
    """Run the real CLI in a child and return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-m", "optivibe_doctor"] + list(argv),
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env=env or _child_env(), cwd=cwd, timeout=timeout)


def _ndjson_of(completed):
    """Parse a completed run's stdout as NDJSON, one independent object per line."""
    return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]


def _findings_of(records):
    return {record["check"]: record for record in records if record.get("type") == "finding"}


def _run_worker(name, env=None, timeout=300):
    """Run one worker child and return ``{check: facts}`` plus the CompletedProcess."""
    completed = subprocess.run(
        [sys.executable, "-m", "optivibe_doctor._worker", name],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env or _child_env(), timeout=timeout)
    records = {}
    for line in completed.stdout.splitlines():
        if line.strip():
            payload = json.loads(line)
            records[payload["check"]] = payload["facts"]
    return records, completed


# ---------------------------------------------------------------------------
# Ownership: the five workers partition the spine exactly.
# ---------------------------------------------------------------------------
def test_worker_ownership_partitions_the_spine_exactly():
    """A worker that dies makes exactly the ids it owed UNKNOWN, so ownership must be a
    partition: an unowned id could never become UNKNOWN, and a doubly-owned one would be
    resurrected by whichever worker happened to run last."""
    owned = []
    for checks in _runner.WORKER_CHECKS.values():
        owned.extend(checks)
    assert sorted(owned) == sorted(BASE_CHECKS)
    assert len(owned) == len(set(owned)), "a spine id is owed by two workers"


def test_derived_checks_are_owned_but_never_expected_from_a_worker():
    """The three coverage ids are crossed by the PARENT.  A worker not delivering them is
    not evidence of anything, so they must be excluded from the died-worker synthesis or
    every healthy run reports them UNKNOWN."""
    for check in _runner.DERIVED_CHECKS:
        assert check in BASE_CHECKS
        assert check.endswith(".coverage")
    unknown = _runner._unknown_for("pkg", delivered=set(), outcome="completed")
    assert not (set(_runner.DERIVED_CHECKS) & set(unknown))


# ===========================================================================
# The encoding guard.  A diagnostic that dies while reporting is worse than the
# fault it was called about.
# ===========================================================================
def test_pinned_remedies_are_unencodable_on_a_windows_console_codepage():
    """The precondition.  If the remedies ever become pure ASCII the guard below is
    vacuous, and it must say so loudly rather than keep passing."""
    carriers = [key for key, text in _contract.REMEDY.items() if EM_DASH in text]
    assert carriers, ("no pinned remedy carries U+2014 any more; the encoding guard is now "
                      "vacuous and must be re-based on whatever character replaced it")
    for codepage in UNENCODABLE_CODEPAGES:
        with pytest.raises(UnicodeEncodeError):
            _contract.REMEDY[carriers[0]].encode(codepage)


@pytest.mark.parametrize("codepage", UNENCODABLE_CODEPAGES)
def test_a_console_codepage_that_cannot_encode_a_remedy_still_gets_a_whole_report(codepage):
    """The invariant, reproduced on the real failure: render a remedy carrying U+2014 to a
    stream whose encoding genuinely rejects it.

    Discriminating condition: a real ``TextIOWrapper`` over ``cp437``/``cp850`` with strict
    errors, so an unguarded write raises exactly as it does on the bare Windows box — the
    control assertion below proves the fixture is lethal before the guard is applied.
    Asserting that ``reconfigure`` was called would be a proxy; this asserts the outcome."""
    control = io.TextIOWrapper(io.BytesIO(), encoding=codepage, errors="strict", newline="")
    with pytest.raises(UnicodeEncodeError):
        control.write(_contract.REMEDY["vendor_data"])

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=codepage, errors="strict", newline="")
    summary, _rc = _run({"plane.manual": Status.WARN})
    finding = Finding("plane.manual", Status.WARN, "absent_expected",
                      "no data file", {}, _contract.REMEDY["vendor_data"])
    renderer = _render.HumanRenderer(_runner._harden_stream(stream))
    renderer.start()
    renderer.finding(finding)
    renderer.summary(summary)
    stream.flush()
    text = raw.getvalue().decode(codepage)
    assert "plane.manual" in text
    assert "SUMMARY" in text, "the report did not survive the unencodable remedy"


def test_the_hardened_stream_cannot_raise_even_without_reconfigure():
    """The proxy carries the case ``reconfigure`` cannot.  A stream with no ``reconfigure``
    at all still must not be able to kill the report, so the guard cannot be satisfied by
    the reconfigure call alone."""

    class _Strict:
        encoding = "cp437"

        def __init__(self):
            self.text = ""

        def write(self, text):
            text.encode(self.encoding)      # raises exactly as a strict wrapper does
            self.text += text
            return len(text)

        def flush(self):
            return None

    target = _Strict()
    assert not hasattr(target, "reconfigure")
    hardened = _runner._harden_stream(target)
    hardened.write("plain\n")
    hardened.write(EM_DASH + " and more\n")
    hardened.flush()
    assert "plain" in target.text
    assert "and more" in target.text


def test_a_full_run_survives_an_unencodable_console():
    """End to end through the real CLI: force the child's stdout onto ``cp437`` and require
    a complete document anyway."""
    completed = _run_doctor(["--format", "ndjson"],
                            env=_child_env({"PYTHONIOENCODING": "cp437"}))
    assert "UnicodeEncodeError" not in completed.stderr
    records = _ndjson_of(completed)
    assert records and records[-1]["type"] == "summary"


#: A non-BMP character.  ``backslashreplace`` renders it ``\U0001f4a5``, which is NOT a
#: JSON escape — JSON has only ``\uXXXX`` — so it is the exact input that turns the
#: crash-while-reporting guard into an UNPARSABLE-record defect one layer over.
_ASTRAL = "\U0001f4a5"


def test_the_machine_stream_stays_parseable_when_the_console_cannot_encode_it():
    """The NDJSON half of the encoding rule, attacked from the direction the human half
    cannot reach.

    ``facts`` are attacker-shaped — a vendor path, an exception message — so an arbitrary
    character genuinely reaches the machine stream.  Surviving the write is not enough
    here: the record must still PARSE, and a consumer handed ``\\U0001f4a5`` cannot parse
    it.

    Mutation that reddens: ``ensure_ascii=False`` in ``_render._dumps``."""
    with pytest.raises(UnicodeEncodeError):          # the fixture really is lethal
        io.TextIOWrapper(io.BytesIO(), encoding="cp437",
                         errors="strict", newline="").write(_ASTRAL)

    raw = io.BytesIO()
    target = io.TextIOWrapper(raw, encoding="cp437", errors="strict", newline="")
    renderer = _render.NdjsonRenderer(_runner._harden_stream(target))
    renderer.finding(Finding("plane.manual", Status.FAIL, "corrupt",
                             "path %s" % _ASTRAL, {"path": "C:/%s/x.db" % _ASTRAL}))
    target.flush()
    text = raw.getvalue().decode("cp437")
    record = json.loads(text.strip())                # the whole point: it must PARSE
    assert record["check"] == "plane.manual"
    assert _ASTRAL in record["facts"]["path"], (
        "the character survived the encoding but not the round trip")


def test_every_ndjson_record_of_a_real_run_parses_on_a_narrow_console():
    """The same property end to end, so it cannot be satisfied by the renderer alone."""
    completed = _run_doctor(["--format", "ndjson"],
                            env=_child_env({"PYTHONIOENCODING": "cp437"}))
    for line in completed.stdout.splitlines():
        if line.strip():
            json.loads(line)                          # raises if any record is unparsable


def test_cpython_forces_backslashreplace_on_its_own_stderr():
    """MEASURED, and recorded because it BOUNDS the guard below rather than motivating it.

    ``PYTHONIOENCODING=cp437:strict`` does NOT make ``sys.stderr`` strict: CPython applies
    the configured handler to stdout and forces ``backslashreplace`` on stderr, precisely
    so that error reporting cannot itself fail.  An argument-echo test through the CLI
    therefore cannot discriminate a hardened stderr from a raw one -- a guard written that
    way passes either way, which is the hollow-guard class.

    If this ever stops holding, the interpreter-level protection is gone and the in-process
    guard below becomes the only thing between a narrow console and a diagnostic that dies
    while explaining itself."""
    completed = subprocess.run(
        [sys.executable, "-c",
         "import json, sys; print(json.dumps([sys.stderr.errors, sys.stdout.errors]))"],
        capture_output=True, text=True, timeout=180,
        env=_child_env({"PYTHONIOENCODING": "cp437:strict"}))
    errors = json.loads(completed.stdout.strip().splitlines()[-1])
    assert errors[0] == "backslashreplace", (
        "sys.stderr is no longer force-hardened by the interpreter (%r); the CLI-level "
        "encoding guard is now meaningful and must be written" % (errors,))
    assert errors[1] == "strict", (
        "PYTHONIOENCODING's handler no longer reaches stdout, so this measurement no "
        "longer shows that stderr is treated specially")


def test_the_fault_line_survives_a_stderr_that_is_not_cpythons_own():
    """``main()``'s fault line interpolates ``str(exc)`` -- RAW text, unlike the usage
    line's ``%r``, which escapes to pure ASCII.  A vendor path or a .NET exception message
    genuinely carries non-ASCII there.

    The interpreter's own ``sys.stderr`` is force-hardened (measured above), but ``main()``
    does not run only under that stream: an IDE, a logging shim or ``redirect_stderr`` can
    install a strict one, and then an unguarded write raises out of the very handler whose
    job is to report that something went wrong -- losing the exit code along with the
    message.

    Mutation that reddens: ``stderr = sys.stderr`` instead of ``_harden_stream(...)``."""
    class _Strict:
        encoding = "cp437"

        def __init__(self):
            self.text = ""

        def write(self, text):
            text.encode(self.encoding)        # raises exactly as a strict wrapper does
            self.text += text
            return len(text)

        def flush(self):
            return None

    def _explode(_args):
        raise RuntimeError("engine at C:/Zemax/日本語/x.dll failed")

    strict = _Strict()
    saved_stderr, saved_run = sys.stderr, _runner.run
    try:
        sys.stderr = strict
        _runner.run = _explode
        with pytest.raises(UnicodeEncodeError):     # the fixture really is lethal
            strict.write("日")
        code = _runner.main([])
    finally:
        sys.stderr, _runner.run = saved_stderr, saved_run

    assert code == EXIT_BY_STATE[State.INCOMPLETE], (
        "the fault path must still return its exit code, never raise out of main()")
    assert "the run faulted" in strict.text
    assert "RuntimeError" in strict.text


# ===========================================================================
# F-1 .. F-8  forbidden structure, by AST
# ===========================================================================
def _module_scope_imports(tree):
    """Return the root names a module imports while it EXECUTES.

    Function-local imports are excluded deliberately: a worker imports the whole world, and
    the rule under test is about what happens when the parent's modules load."""
    roots = []

    def visit(node, deferred):
        for child in ast.iter_child_nodes(node):
            nested = deferred or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))
            if not nested and isinstance(child, ast.Import):
                roots.extend(alias.name.split(".")[0] for alias in child.names)
            elif not nested and isinstance(child, ast.ImportFrom):
                if child.level == 0 and child.module:
                    roots.append(child.module.split(".")[0])
            visit(child, nested)

    visit(tree, False)
    return roots


@pytest.mark.parametrize("module", ALL_DOCTOR_MODULES)
def test_f1_no_doctor_module_imports_outside_the_standard_library(module):
    """F-1.  The universe is ``sys.stdlib_module_names`` — never a hand-list, which would
    be a second acceptance set drifting against the real one."""
    stdlib = set(sys.stdlib_module_names)
    offenders = [root for root in _module_scope_imports(_doctor_tree(module))
                 if root not in stdlib]
    assert offenders == [], "%s imports %r at module scope" % (module, offenders)


@pytest.mark.parametrize("module", ("_contract", "_model", "_classify"))
def test_f1_the_pure_modules_reach_nothing_non_stdlib_anywhere(module):
    """F-1's stronger half: the pure modules may not reach outside the standard library
    even from inside a function body."""
    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for node in ast.walk(_doctor_tree(module)):
        if isinstance(node, ast.Import):
            offenders.extend(alias.name.split(".")[0] for alias in node.names
                             if alias.name.split(".")[0] not in stdlib)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            if root not in stdlib:
                offenders.append(root)
    assert offenders == [], "%s reaches %r" % (module, offenders)


def test_f2_doctor_never_calls_the_self_modifying_resolver():
    """F-2.  ``resolve_zemax_dir`` teaches the initializer the path it just found, so every
    later call claims ``registry-autodetect`` whether or not the registry has an entry.
    Only ``load_zosapi``'s own first call is evidence, and the memo is what reports it."""
    for module in ALL_DOCTOR_MODULES:
        assert "resolve_zemax_dir" not in _doctor_source(module), module


def test_f3_doctor_neither_globs_nor_rebuilds_the_install_directory():
    """F-3.  Re-implementing resolution would make doctor report its own answer instead of
    production's, which is the one answer that matters."""
    for module in ALL_DOCTOR_MODULES:
        source = _doctor_source(module)
        assert "ZEMAX_DIR" not in source, module
        for node in ast.walk(_doctor_tree(module)):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                assert name not in ("glob", "iglob"), module


def test_f4_doctor_never_reaps_on_boot():
    """F-4.  ``boot_reap`` TERMINATES processes.  Doctor may reclaim exactly the engine it
    declared and nothing else — never a sweep it did not author."""
    for module in ALL_DOCTOR_MODULES:
        assert "boot_reap" not in _doctor_source(module), module


def test_f5_doctor_never_touches_the_stdio_file_descriptors():
    """F-5.  ``dup2`` on fd 1 is the harness's own hang mitigation; a diagnostic that
    re-points the descriptors it reports through cannot be trusted about them."""
    for module in ALL_DOCTOR_MODULES:
        source = _doctor_source(module)
        assert "dup2" not in source, module
        assert "_isolate_stdio" not in source, module


#: The ONLY ``optivibe_harness`` submodules doctor is permitted to name.  Enumerated
#: here, never derived from what doctor imports (see this file's second governing rule).
PERMITTED_HARNESS_SUBMODULES = ("composite", "server", "server_mcp", "session")


def test_f6_doctor_names_only_harness_submodules_the_distribution_ships():
    """F-6.  The harness source tree contains sibling packages that are NOT part of the
    distribution, so a doctor that reaches one of them cannot run on the tree it ships in.

    Pinning the PERMITTED set rather than one forbidden name is deliberate and is strictly
    stronger: a denylist grades only the sibling somebody remembered to write down, while
    this grades EVERY submodule reference, including one nobody thought to forbid.  The set
    is enumerated here and never read from the implementation -- a set derived from what
    doctor happens to import would ratify a new reference instead of grading it.

    Mutation: make any doctor module name a submodule outside this set and it reddens."""
    for module in ALL_DOCTOR_MODULES:
        referenced = re.findall(r"optivibe_harness\.([A-Za-z_][A-Za-z0-9_]*)",
                                _doctor_source(module))
        offenders = sorted(set(referenced) - set(PERMITTED_HARNESS_SUBMODULES))
        assert offenders == [], "%s reaches %r" % (module, offenders)


def test_f7_main_is_guarded_and_never_runs_at_import():
    """F-7.  ``pkgutil.walk_packages`` yields ``__main__`` and the publication suite imports
    every walked name — an unguarded ``raise SystemExit(main())`` would run the whole
    doctor, spawn its children and open a session, inside a collection run."""
    source = _doctor_source("__main__")
    assert 'if __name__ == "__main__":' in source
    for node in _doctor_tree("__main__").body:
        assert not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)), (
            "__main__ calls something at module scope")
        assert not isinstance(node, ast.Raise), "__main__ raises at module scope"


def test_f7_importing_the_walked_module_does_not_execute_the_run():
    """F-7's behavioural half, which the AST half cannot give: import exactly what the walk
    imports and require that nothing happened."""
    completed = subprocess.run(
        [sys.executable, "-c",
         "import importlib, json;"
         "m = importlib.import_module('optivibe_doctor.__main__');"
         "print(json.dumps({'has_main': hasattr(m, 'main')}))"],
        capture_output=True, text=True, env=_child_env(), timeout=180)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert json.loads(completed.stdout.strip().splitlines()[-1])["has_main"] is True
    assert "SUMMARY" not in completed.stdout
    assert "optivibe doctor" not in completed.stdout


@pytest.mark.parametrize("module", PARENT_MODULES)
def test_f8_no_parent_module_performs_filesystem_io(module):
    """F-8.  The parent does NO blocking filesystem I/O.  A stalled UNC entry on
    ``sys.path`` makes a metadata read or a directory listing block, and it would block
    before any killable worker exists — the hang would be doctor's own, in the one code
    path with nothing left to kill."""
    source = _doctor_source(module)
    assert "importlib.metadata" not in source, module
    # The stat family is in this set, and it is not padding: ``isfile``/``exists``/``stat``
    # block on a stalled UNC path exactly as ``listdir`` does, which IS the hazard this rule
    # names — and the sibling guard below asserts ``os.path.isfile`` is the WORKER's I/O
    # primitive, so a set that forbade listing but permitted stat'ing would let the parent
    # do the very call it points at as "the I/O".  Proven by mutation: with the stat names
    # absent, injecting ``os.path.isfile(...)`` into ``_runner._zemax_deadline_s`` left this
    # guard green.
    forbidden = {"listdir", "walk", "scandir", "getsize", "samefile", "open",
                 "isfile", "isdir", "exists", "stat", "lstat", "getmtime", "getctime",
                 "getatime", "islink", "realpath", "readlink"}
    for node in ast.walk(_doctor_tree(module)):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id != "open", "%s calls open()" % module
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden, (
                    "%s calls %s()" % (module, node.func.attr))


def test_f8_the_worker_is_the_module_that_does_the_io():
    """The other half of F-8.  If NO module did the filesystem work, the guard above would
    pass happily over a doctor that measures nothing at all."""
    source = _doctor_source("_worker")
    assert "importlib.metadata" in source
    assert "os.listdir" in source
    assert "os.path.isfile" in source


# ===========================================================================
# The spawn seam.  Every subprocess and streaming guard rides it, so no guard
# has to break the machine to synthesise an unhealthy install.
# ===========================================================================
def _fake_spawn(recorder, emissions=None, outcomes=None):
    """A spawn seam that records what was asked for and replays canned observations."""
    emissions = emissions or {}
    outcomes = outcomes or {}

    def spawn(name, deadline_s, env_extra=None, **kwargs):
        recorder.append((name, deadline_s, dict(env_extra or {}), dict(kwargs)))
        for observation in emissions.get(name, ()):
            yield observation
        yield _runner._outcome(outcomes.get(name, "completed"))

    return spawn


def _healthy_emissions():
    """Observations that grade to a healthy run, keyed by the worker that owes them."""
    origin = os.path.join("C:", "site-packages", "optivibe_harness", "__init__.py")
    base = [
        Observation("env.python", {"version": "3.11.9", "executable": "py", "path0": ""}),
        Observation("env.cwd_shadow", {"cwd": "C:/w", "shadowed": []}),
        Observation("env.pythonnet_runtime", {"value": None}),
    ]
    for dist in ("optivibe-harness", "optivibe-reference"):
        base.append(Observation("version." + dist, {
            "meta": "0.1.3", "source": "0.1.3", "manifest": "0.1.3", "origin": origin}))
    base.append(Observation("dependency.universe",
                            {"names": ["mcp", "psutil", "pythonnet"]}))

    pkg = [Observation("reference.import", {"ok": True}),
           Observation("reference.dispatcher", {"ok": True, "error": None})]
    for name in ("mcp", "psutil", "pythonnet"):
        pkg.append(Observation("dependency." + name, {
            "declared": True, "meta_version": "1.0", "import_ok": True,
            "import_error": None, "import_name": name}))
    pkg.append(Observation("dependency.mcp_range", {"version": "1.28.0"}))
    pkg.append(Observation("plane.universe", {
        "conn_attrs": ["conn", "glass_conn", "manual_conn", "tolerance_conn"]}))
    for plane in PLANE_NAMES:
        pkg.append(Observation("plane." + plane, {
            "path": "C:/d/%s" % plane, "present": True, "bytes": 10, "opens": True,
            "wired": True, "identity_ok": True, "wired_path": "C:/d/%s" % plane,
            "conn_attr": "conn"}))
    for check in ("enrichment.merit", "enrichment.tolerance"):
        pkg.append(Observation(check, {"tier": "enriched"}))
    for door in sorted(TOOL_PLANE):
        pkg.append(Observation("tool." + door, {
            "outer_ok": True, "inner_ok": True, "error_family": None}))
    pkg.append(Observation("harness.import", {"ok": True}))
    pkg.append(Observation("harness.manifest", {
        "ok": True, "names": ["get_system_info"], "is_open": False, "clr_loaded": False}))
    pkg.append(Observation("composed.manifest", {
        "ok": True, "names": ["get_system_info"] + sorted(TOOL_PLANE)}))
    pkg.append(Observation("mcp.construct", {"ok": True}))

    zemax = [
        Observation("zemax.nethelper", {"harness_import": True, "found": True,
                                        "path": "C:/n/ZOSAPI_NetHelper.dll"}),
        Observation("zemax.dir", {"harness_import": True, "nethelper": True, "loaded": True,
                                  "resolution": ["C:/z", "program-files-scan"]}),
    ]
    return {"base": base, "pkg": pkg, "zemax": zemax}


class _Sink:
    """A stdout stand-in that records what the renderer wrote, as it is written."""

    def __init__(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)
        return len(text)

    def flush(self):
        return None

    @property
    def text(self):
        return "".join(self.chunks)


def _drive(args=None, emissions=None, outcomes=None):
    """Run the parent over the spawn seam; return (summary, recorder, sink)."""
    recorder = []
    sink = _Sink()
    args = args or _runner.Args(format="ndjson")
    summary = _runner.run(args, spawn=_fake_spawn(recorder, emissions, outcomes), out=sink)
    return summary, recorder, sink


def _sink_findings(sink):
    return _findings_of([json.loads(line) for line in sink.text.splitlines() if line.strip()])


# ===========================================================================
# The REAL spawner.  Everything above this point rides ``_fake_spawn``, which is
# what makes those guards fast and deterministic — and which is also why, for
# three rounds, NOTHING exercised ``_spawn_worker`` itself.  Its ``Popen`` kwargs,
# its two reader threads, its deadline arithmetic, the ``extend_on`` handover and
# the kill/wait/close ``finally`` were all ungraded, and two real defects lived
# there: an undrained stderr pipe that destroyed a healthy worker's whole report,
# and an overall deadline whose fix was pinned only at the seam that hands the
# kwargs over, never at the behaviour.
#
# These guards therefore fake NOTHING.  They drive the real ``_spawn_worker``
# against the real worker entry point, and shape the child's behaviour the way a
# stranger's machine shapes it: from BELOW, through a ``sitecustomize`` on
# ``PYTHONPATH``, exactly as a corporate Python install, an old ``pkg_resources``
# or the ZOS-API's own native fd-2 banner does.
# ===========================================================================
_SPAWN_SITECUSTOMIZE = '''\
import os, sys, time

_mode = os.environ.get("DOCTOR_SPAWN_TEST_MODE", "")
_marker = os.environ.get("DOCTOR_SPAWN_TEST_MARKER", "")

if _mode == "stderr_noise":
    sys.stderr.write("X" * int(os.environ["DOCTOR_SPAWN_TEST_BYTES"]))
    sys.stderr.flush()
elif _mode == "wedge_after":
    sys.stdout.write('{"check": "%s", "facts": {}}\\n'
                     % os.environ["DOCTOR_SPAWN_TEST_CHECK"])
    sys.stdout.flush()
    time.sleep(600)
elif _mode == "wedge_silently":
    time.sleep(600)
elif _mode == "wedge_marker":
    open(_marker + ".start", "w").close()
    time.sleep(float(os.environ.get("DOCTOR_SPAWN_TEST_SLEEP", "6")))
    open(_marker + ".end", "w").close()
elif _mode == "malformed":
    sys.stdout.write("this line is not an observation\\n")
    sys.stdout.flush()
elif _mode == "die_loud":
    sys.stderr.write("BOOM: the interpreter could not start the worker\\n")
    sys.stderr.flush()
    os._exit(3)
'''


def _spawn_fixture(monkeypatch, tmp_path, **env):
    """Put the behaviour-shaping ``sitecustomize`` below the real worker.

    ``_spawn_worker`` copies ``os.environ``, so the environment is set on the process
    rather than handed to a helper — the point being that no production seam is replaced.
    """
    site = tmp_path / "spawn_sitecustomize"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(_SPAWN_SITECUSTOMIZE, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [str(site), DOCTOR_SRC]
        + [entry for entry in sys.path if entry and os.path.isdir(entry)]))
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))


def _drive_real_spawn(name, deadline_s, join_s=60.0, **kwargs):
    """Run the REAL ``_spawn_worker`` under a hard wall-clock join.

    The join is not belt-and-braces.  Every deadline guard below exists because a bound can
    be relinquished, and a guard that hangs when it detects that has reported nothing: it
    must FAIL, loudly, not wedge the suite.  Returns ``(observations, elapsed, timed_out)``.
    """
    collected, started = [], time.monotonic()

    def drive():
        collected.extend(_runner._spawn_worker(name, deadline_s, **kwargs))

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    thread.join(join_s)
    return (list(collected), time.monotonic() - started, thread.is_alive())


def _outcome_of(observations):
    for observation in observations:
        if observation.check == _runner.OUTCOME:
            return dict(observation.facts)
    return {}


def test_real_spawn_a_healthy_worker_delivers_every_id_it_owes(monkeypatch, tmp_path):
    """The control.  Without it, every guard below could pass over a fixture that simply
    never produces a report at all."""
    _spawn_fixture(monkeypatch, tmp_path)
    observations, _elapsed, hung = _drive_real_spawn("base", 60.0)
    assert not hung
    delivered = {observation.check for observation in observations}
    assert set(_runner.WORKER_CHECKS["base"]) <= delivered, sorted(delivered)
    assert _outcome_of(observations)["status"] == "completed"


@pytest.mark.parametrize("noise", [4000, 200000])
def test_real_spawn_a_worker_that_writes_to_stderr_still_delivers(monkeypatch, tmp_path,
                                                                  noise):
    """A child's stderr must never be able to destroy its stdout report.

    ``4000`` fits one pipe buffer and ``200000`` does not, so the pair discriminates the
    BUFFER from the noise.  With ``stderr=PIPE`` and nothing draining it, the second child
    blocks in ``write()`` forever, never reaches its remaining ``emit()`` calls, is killed
    at its deadline, and every spine id it owed becomes UNKNOWN — a HEALTHY machine exits 3
    after burning the full deadline.

    Mutation that reddens: delete the ``_drain`` thread from ``_spawn_worker``."""
    _spawn_fixture(monkeypatch, tmp_path,
                   DOCTOR_SPAWN_TEST_MODE="stderr_noise", DOCTOR_SPAWN_TEST_BYTES=noise)
    observations, _elapsed, hung = _drive_real_spawn("base", 60.0)
    assert not hung
    owed = set(_runner.WORKER_CHECKS["base"])
    delivered = owed & {observation.check for observation in observations}
    assert delivered == owed, (
        "%d bytes of child stderr cost the worker its report: delivered %r of %r"
        % (noise, sorted(delivered), sorted(owed)))


def test_real_spawn_the_overall_deadline_survives_the_preflight_handover(monkeypatch,
                                                                        tmp_path):
    """The engine worker's two deadlines, asserted as BEHAVIOUR rather than as kwargs.

    ``_spawn_worker``'s own docstring names the finding: *"Permanently handing over the kill
    deadline after preflight was the finding — a licence read that wedges after the handover
    hangs doctor with no remaining bound at all."*  The seam-level guard elsewhere in this
    file asserts only that ``extend_on``/``extend_to`` were PASSED; it cannot see whether
    they are honoured.  This drives the real spawner against a child that announces the
    handover record and then wedges exactly as a hung licence read does.

    Mutation that reddens: set ``stage_end = overall_end`` without also bounding on
    ``overall_end`` in the wait — the generator then never returns."""
    _spawn_fixture(monkeypatch, tmp_path, DOCTOR_SPAWN_TEST_MODE="wedge_after",
                   DOCTOR_SPAWN_TEST_CHECK="engine.preflight")
    observations, elapsed, hung = _drive_real_spawn(
        "base", 2.0, join_s=45.0, extend_on="engine.preflight", extend_to=6.0)
    assert not hung, (
        "the spawner never returned within 45s against a 6s overall deadline: the overall "
        "bound was relinquished at the preflight handover")
    checks = [observation.check for observation in observations]
    assert "engine.preflight" in checks, (
        "the child never announced the handover record; the fixture is broken, not the "
        "product")
    assert _outcome_of(observations)["status"] == "timeout", checks
    assert elapsed < 30, (
        "the wedged child ran %.1fs against a 6s overall bound" % elapsed)


def test_real_spawn_the_stage_deadline_still_kills_before_the_handover(monkeypatch,
                                                                      tmp_path):
    """The other side of the same arithmetic: a child that wedges BEFORE announcing the
    handover record must die at the SHORT stage deadline, not linger until the long one.

    Without this, an implementation that simply used ``extend_to`` from the start would
    satisfy the guard above while quietly removing the preflight bound."""
    _spawn_fixture(monkeypatch, tmp_path, DOCTOR_SPAWN_TEST_MODE="wedge_silently")
    observations, elapsed, hung = _drive_real_spawn(
        "base", 2.0, join_s=45.0, extend_on="engine.preflight", extend_to=30.0)
    assert not hung
    assert _outcome_of(observations)["status"] == "timeout"
    assert elapsed < 20, (
        "a child that never reached the handover was held to the 30s OVERALL deadline "
        "(%.1fs), so the 2s stage bound is not applied" % elapsed)


def test_real_spawn_a_killed_child_is_actually_dead_not_merely_abandoned(monkeypatch,
                                                                        tmp_path):
    """The lifecycle half.  Returning from the generator is not the same claim as ending
    the process, and only the second one keeps a diagnostic from leaving orphans behind on
    the machine it was called to help.

    The child writes ``.start`` immediately and ``.end`` after its sleep; the proof is that
    ``.end`` never appears, observed AFTER waiting past the moment it would have."""
    marker = tmp_path / "kill"
    _spawn_fixture(monkeypatch, tmp_path, DOCTOR_SPAWN_TEST_MODE="wedge_marker",
                   DOCTOR_SPAWN_TEST_MARKER=str(marker), DOCTOR_SPAWN_TEST_SLEEP=6)
    observations, _elapsed, hung = _drive_real_spawn("base", 2.0, join_s=45.0)
    assert not hung
    assert _outcome_of(observations)["status"] == "timeout"
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline and not os.path.exists(str(marker) + ".start"):
        time.sleep(0.2)
    assert os.path.exists(str(marker) + ".start"), (
        "the child never ran; the fixture is broken, not the product")
    time.sleep(8.0)
    assert not os.path.exists(str(marker) + ".end"), (
        "the child outlived its deadline and finished its work: the spawner abandoned it "
        "rather than killing it")


def test_real_spawn_a_child_that_dies_before_emitting_still_explains_itself(monkeypatch,
                                                                           tmp_path):
    """A worker that fails during MODULE IMPORT never reaches its own exception handler, so
    it emits no observation at all.  Its stderr is then the ONLY explanation in existence.

    Doctor exists because the original failure was silent; a report that says "the base
    worker died before reporting this" and nothing else is that same silence one level up.

    Mutation that reddens: drop ``stderr_tail`` from the terminal record."""
    _spawn_fixture(monkeypatch, tmp_path, DOCTOR_SPAWN_TEST_MODE="die_loud")
    observations, _elapsed, hung = _drive_real_spawn("base", 30.0)
    assert not hung
    outcome = _outcome_of(observations)
    assert outcome["status"] == "crashed", outcome
    assert outcome["returncode"] == 3, outcome
    assert "BOOM" in (outcome.get("stderr_tail") or ""), outcome

    # ...and it must reach the REPORT, not merely the terminal record.
    observations_map = {}
    _runner._record_stderr_tail(observations_map, "base", outcome["status"], outcome)
    unknown = _runner._unknown_for("base", set(), outcome["status"], observations_map)
    findings = _runner.evaluate(observations_map, checks=_runner.WORKER_CHECKS["base"],
                                unknown=unknown)
    blob = json.dumps([{"summary": finding.summary, "facts": dict(finding.facts)}
                       for finding in findings])
    assert "BOOM" in blob, (
        "doctor held the child's only explanation and rendered %d lines without it"
        % len(findings))


def test_a_non_ascii_fact_survives_the_child_to_parent_pipe():
    """The sibling of the NDJSON encoding fix, on the OTHER serialisation.

    ``_worker.emit`` uses ``ensure_ascii=False`` while ``_render._dumps`` now uses True,
    and the asymmetry is deliberate rather than an oversight: the worker writes to a pipe
    the PARENT opens with an explicit ``encoding="utf-8"`` (and forces ``PYTHONIOENCODING``
    on the child), whereas the renderer writes to a console whose encoding belongs to the
    user.  What actually matters is not which flag is set but that a non-BMP character
    survives the hop, so that is what is asserted -- and it stays true under either flag,
    which is what makes it a guard on the property rather than on the implementation.

    Mutation that reddens: emit through a codec that cannot carry the character."""
    captured = io.StringIO()
    saved = sys.stdout
    try:
        sys.stdout = captured
        _worker.emit("plane.manual", path="C:/%s/x.db" % _ASTRAL, bytes=10)
    finally:
        sys.stdout = saved

    observation = _runner._parse_line(captured.getvalue())
    assert observation is not None, "the emitted line did not parse back at all"
    assert observation.facts["path"] == "C:/%s/x.db" % _ASTRAL


def test_real_spawn_a_malformed_line_is_reported_and_does_not_stop_the_stream(monkeypatch,
                                                                             tmp_path):
    """An unreadable record must be named, never silently dropped — and it must not cost
    the worker the observations it goes on to emit."""
    _spawn_fixture(monkeypatch, tmp_path, DOCTOR_SPAWN_TEST_MODE="malformed")
    observations, _elapsed, hung = _drive_real_spawn("base", 60.0)
    assert not hung
    assert _outcome_of(observations)["status"] == "malformed"
    delivered = {observation.check for observation in observations}
    assert set(_runner.WORKER_CHECKS["base"]) <= delivered, (
        "one unreadable line cost the worker the rest of its report: %r" % sorted(delivered))


def test_real_spawn_never_hands_the_child_an_inherited_stdin(monkeypatch, tmp_path):
    """``stdin=DEVNULL`` on every spawn.  A child that inherits a terminal can BLOCK on a
    read, which is the one hang no deadline arithmetic in the parent can distinguish from
    honest work."""
    tree = _doctor_tree("_runner")
    popen_calls = [node for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in ("Popen", "run")]
    assert popen_calls, "the spawn call sites moved; re-base this guard"
    for call in popen_calls:
        stdin = [keyword for keyword in call.keywords if keyword.arg == "stdin"]
        assert stdin, "a child is spawned without an explicit stdin"
        assert getattr(stdin[0].value, "attr", None) == "DEVNULL", (
            "a child is spawned with an inheritable stdin")


# ===========================================================================
# E-2  the default run spawns three children and only three
# ===========================================================================
def test_e2_the_default_run_spawns_exactly_base_pkg_and_zemax():
    """E-2, asserted at the spawn seam.  A diagnostic that takes the single seat to tell
    you the seat is fine has caused the problem it was called about."""
    _summary, recorder, _sink = _drive(emissions=_healthy_emissions())
    assert [entry[0] for entry in recorder] == ["base", "pkg", "zemax"]


def test_e2_boot_and_engine_are_spawned_only_when_asked_for():
    _summary, recorder, _sink = _drive(
        args=_runner.Args(format="ndjson", boot=True, engine=True),
        emissions=_healthy_emissions())
    assert [entry[0] for entry in recorder] == ["base", "pkg", "zemax", "boot", "engine"]


#: The suffix every licence-tier token reported by this API ends with.  Gating on the
#: suffix grades every tier, present and future, instead of one remembered spelling.
LICENCE_TIER_SUFFIX = "Edition"


def test_the_licence_status_is_reported_verbatim_and_never_compared():
    """``LicenseStatus`` is a REPORTED FACT, never an expectation.

    The tier string was measured on exactly one machine; other licences report other
    tokens, so pinning any one of them would fail a perfectly valid install.  The verdict
    comes from ``IsValidLicenseForAPI`` — a boolean the API itself answers — while the
    status string travels into the facts for a human to read."""
    for module in ("_worker", "_runner", "_classify", "_contract", "_render", "__main__"):
        source = _doctor_source(module)
        # Every licence-tier token this API reports ends in the same suffix, so gating on
        # the SUFFIX grades every tier at once -- strictly stronger than naming one of
        # them, which would leave the others free to be hardcoded.
        assert LICENCE_TIER_SUFFIX not in source, module
    grade = _functions_of(_doctor_tree("_runner"))["_grade_engine"]
    read = [node.value for node in ast.walk(grade)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert "license_status" not in read, (
        "_grade_engine must not branch on the licence status string")
    assert "valid_license" in read, "the verdict must come from IsValidLicenseForAPI"


def test_e2_the_reap_worker_is_never_part_of_a_default_run():
    """E-2's second half.  ``reap`` is a SIXTH worker mode, spawned only after an --engine
    overall-deadline kill that saw an ``engine.owned`` record — never on any other path.

    It exists as a worker at all because the parent may import nothing outside the standard
    library and the reaper needs ``psutil``; running the reclaim in a child keeps rule 1
    intact and keeps the kill inside production's own create-time-gated guards."""
    for args in (None, _runner.Args(format="ndjson", boot=True),
                 _runner.Args(format="ndjson", engine=True)):
        _summary, recorder, _sink = _drive(args=args, emissions=_healthy_emissions())
        assert "reap" not in [entry[0] for entry in recorder], (
            "the reap worker was spawned on a path that never killed an engine")


def test_e2_the_reap_worker_deadline_is_the_pinned_one():
    assert _runner.REAP_DEADLINE_S == 15.0


# ---------------------------------------------------------------------------
# E-6 / E-7  the reap is PROVEN by the OS, or it is disclosed as unproven
# ---------------------------------------------------------------------------
class _FakeReaperResult(object):
    def __init__(self, ok, action="terminated", detail=""):
        self.ok, self.action, self.detail = ok, action, detail


def _run_reap_worker(monkeypatch, pid, baseline=(), create_time=1234.5,
                     reaper_ok=True, still_alive=False, reaper_raises=None):
    """Drive the REAL reap worker with a spied reaper and a scripted OS.

    ``still_alive`` is the whole point: it lets the reaper report a cheerful success while
    the operating system still sees the process, which is the only fixture under which
    "the tool said it reaped" and "the process is gone" can be told apart.

    The default ``create_time`` is deliberately a VERIFIABLE, non-zero recording, and the
    scripted OS reports the same value, so ``still_alive=True`` means *this very process is
    still there* — identity proven, not assumed.  It used to default to ``0.0``, which is
    production's "unknown create_time" sentinel: under that fixture a survival verdict was
    reachable on pid existence alone, so the fixture agreed with the weaker identity instead
    of discriminating against it."""
    import types

    calls = []

    def terminate_tracked(target_pid, target_create_time, baseline_pids=None):
        calls.append((target_pid, target_create_time, tuple(baseline_pids or ())))
        if reaper_raises is not None:
            raise reaper_raises
        return _FakeReaperResult(reaper_ok)

    class _NoSuchProcess(Exception):
        pass

    class _Process(object):
        def __init__(self, target):
            if not still_alive:
                raise _NoSuchProcess(target)

        def create_time(self):
            return create_time

    fake_psutil = types.ModuleType("psutil")
    fake_psutil.Process = _Process
    fake_psutil.NoSuchProcess = _NoSuchProcess
    fake_psutil.Error = Exception
    fake_reaper = types.ModuleType("optivibe_harness.process_reaper")
    fake_reaper.terminate_tracked = terminate_tracked
    harness = types.ModuleType("optivibe_harness")
    harness.process_reaper = fake_reaper

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setitem(sys.modules, "optivibe_harness", harness)
    monkeypatch.setitem(sys.modules, "optivibe_harness.process_reaper", fake_reaper)
    monkeypatch.setenv(_worker.REAP_PID_ENV, str(pid))
    monkeypatch.setenv(_worker.REAP_CREATE_TIME_ENV, str(create_time))
    monkeypatch.setenv(_worker.REAP_BASELINE_ENV, json.dumps(list(baseline)))

    captured = {}
    monkeypatch.setattr(_worker, "emit",
                        lambda check, **facts: captured.__setitem__(check, facts))
    _worker.worker_reap()
    return captured.get("engine.reaped", {}), calls


def test_e6_a_reap_the_os_did_not_confirm_is_never_reported_as_a_reap(monkeypatch):
    """E-6, the load-bearing half.

    Discriminating condition: the reaper returns ``ok=True`` while the process is STILL
    ALIVE.  *The tool reported a reap* and *the process is gone* are different claims and
    only the second is evidence — report the kill call's own return value and this
    reddens, because the fixture makes the two answers disagree."""
    facts, calls = _run_reap_worker(monkeypatch, pid=4321, still_alive=True,
                                    reaper_ok=True)
    assert calls, "the reaper was never called at all"
    assert facts["reported_ok"] is True, "the fixture must have the reaper claim success"
    assert facts["gone"] is False, facts
    assert facts["ok"] is False, (
        "a reap the OS did not confirm was reported as a reap: %r" % (facts,))


def test_e6_the_proof_is_the_os_even_when_the_reaper_reports_failure(monkeypatch):
    """The mirror image: the reaper reports failure but the process really is gone.

    The OS is the oracle in both directions, so this must read as reaped — otherwise
    doctor would name a phantom orphan and send the reader hunting for a process that does
    not exist."""
    facts, _calls = _run_reap_worker(monkeypatch, pid=4321, still_alive=False,
                                     reaper_ok=False)
    assert facts["reported_ok"] is False
    assert (facts["gone"], facts["ok"]) == (True, True), facts


def test_e6_a_reaper_that_raises_still_yields_an_os_verdict(monkeypatch):
    """A worker never propagates: an exception out of the reaper becomes a recorded fact,
    and the OS is still asked, because the kill may well have landed before the raise."""
    facts, _calls = _run_reap_worker(monkeypatch, pid=4321, still_alive=False,
                                     reaper_raises=RuntimeError("boom"))
    assert facts["error_type"] == "RuntimeError"
    assert (facts["gone"], facts["ok"]) == (True, True), facts


def test_e6_a_pid_in_the_pre_existing_snapshot_is_never_killed(monkeypatch):
    """E-6's other half: never a name sweep, never a PID doctor did not open.

    A PID that was already running when doctor started is somebody else's engine, and
    killing a concurrent design session is the catastrophe the whole targeted-reap design
    exists to avoid."""
    facts, calls = _run_reap_worker(monkeypatch, pid=4321, baseline=(4321, 99),
                                    still_alive=True)
    assert calls == [], "the reaper was called on a pre-existing PID"
    assert facts["ok"] is False and facts["gone"] is None, facts


def test_e6_the_reaper_is_handed_exactly_the_declared_pid(monkeypatch):
    """Never a PID it was not handed.  The declared pid, its create time and the baseline
    all travel to production's own create-time-gated predicate unchanged."""
    _facts, calls = _run_reap_worker(monkeypatch, pid=4321, baseline=(7, 8),
                                     create_time=1234.5, still_alive=False)
    assert calls == [(4321, 1234.5, (7, 8))], calls


def test_e6_an_unaskable_os_is_never_read_as_a_successful_reap(monkeypatch):
    """When the OS itself cannot be asked the answer is ``None`` — not True.  Guessing
    here would convert a failed measurement into a claim that an orphan was reclaimed."""
    import types

    fake_psutil = types.ModuleType("psutil")

    class _Boom(Exception):
        pass

    def _explode(_pid):
        raise ValueError("no such interface")

    fake_psutil.Process = _explode
    fake_psutil.NoSuchProcess = _Boom
    fake_psutil.Error = _Boom
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert _worker._pid_is_gone(4321, 0.0) is None


def test_e6_a_recycled_pid_is_not_mistaken_for_a_survivor(monkeypatch):
    """A live PID whose creation time no longer matches the one doctor recorded is a
    DIFFERENT process — the same create-time gate production kills under, applied to the
    proof rather than to the kill.

    Asserted on the OS oracle directly, so the two answers differ only in the recorded
    creation time and nothing else can account for the difference."""
    import types

    class _NoSuchProcess(Exception):
        pass

    def _make(live_create_time):
        module = types.ModuleType("psutil")

        class _Process(object):
            def __init__(self, _pid):
                pass

            def create_time(self):
                return live_create_time

        module.Process = _Process
        module.NoSuchProcess = _NoSuchProcess
        module.Error = Exception
        return module

    monkeypatch.setitem(sys.modules, "psutil", _make(1000.0))
    assert _worker._pid_is_gone(4321, 1000.0) is False, (
        "same creation time: the original process really did survive the reap")
    monkeypatch.setitem(sys.modules, "psutil", _make(9999.0))
    assert _worker._pid_is_gone(4321, 1000.0) is True, (
        "a live PID with a different creation time is a recycled PID, not the survivor")


def test_e6_an_unverifiable_identity_is_indeterminate_never_a_survival_claim(monkeypatch):
    """A LIVE pid with no verifiable recorded identity is ``None``, never ``False``.

    The defect: ``if create_time:`` is a *truthiness* test, so ``0.0`` — production's
    documented "unknown create_time" sentinel and the value an unset/unparseable recording
    used to collapse to — skipped the PID-reuse gate entirely and answered ``False``, i.e.
    *doctor's engine survived the reap*, on pid existence alone.  That is the weaker-identity
    shape this sprint has been closing, sitting in the function that IS the post-kill proof.

    Discriminating condition: the very same live pid is read under a verifiable recording and
    under each unverifiable one.  The verifiable read must stay decisive (``False`` — proven
    survivor) while every unverifiable read must be indeterminate, so a fix that answers
    ``None`` unconditionally cannot pass either.

    Mutation: restore ``if create_time:`` (or return ``False`` for the unverifiable case) →
    the ``0.0``/``None`` rows read ``False`` and this reddens."""
    import types

    class _NoSuchProcess(Exception):
        pass

    module = types.ModuleType("psutil")

    class _Process(object):
        def __init__(self, _pid):
            pass                       # the pid EXISTS — that is the whole trap

        def create_time(self):
            return 1000.0

    module.Process = _Process
    module.NoSuchProcess = _NoSuchProcess
    module.Error = Exception
    monkeypatch.setitem(sys.modules, "psutil", module)

    assert _worker._pid_is_gone(4321, 1000.0) is False, (
        "a verifiable, matching recording must stay a decisive survival verdict; a fix that "
        "answers None for everything proves nothing")
    for unverifiable in (0.0, 0, None, "", "zzz"):
        assert _worker._pid_is_gone(4321, unverifiable) is None, (
            "a LIVE pid recorded as %r was graded on pid existence alone; with no "
            "verifiable create_time 'this pid exists' and 'doctor's engine survived' are "
            "different claims and only the second was reported" % (unverifiable,))


def test_e6_an_absent_pid_is_gone_even_with_nothing_recorded(monkeypatch):
    """The other half, and the one the indeterminate fix could easily break.

    A pid the OS cannot find is gone whatever doctor recorded — there is no process left to
    identify, so no identity is needed.  Answering ``None`` here would convert every
    successful reap on an unrecorded create_time into "could not re-read the process", which
    is a false alarm: doctor would name a phantom orphan it had in fact already reclaimed.

    Mutation: move the unverifiable-identity check ABOVE ``psutil.Process(pid)`` → the
    absent-pid rows read ``None`` and this reddens."""
    import types

    class _NoSuchProcess(Exception):
        pass

    module = types.ModuleType("psutil")

    def _absent(pid):
        raise _NoSuchProcess(pid)

    module.Process = _absent
    module.NoSuchProcess = _NoSuchProcess
    module.Error = Exception
    monkeypatch.setitem(sys.modules, "psutil", module)

    for recorded in (0.0, None, "", 1000.0):
        assert _worker._pid_is_gone(4321, recorded) is True, (
            "an ABSENT pid recorded as %r was not read as gone; identity is only needed "
            "while there is still a process to identify" % (recorded,))


def test_e6_absence_of_a_create_time_travels_as_absence_not_as_zero(monkeypatch):
    """End to end: an unrecorded create_time must not arrive at the child looking like one.

    The parent writes the recording into an env var, and an env channel carries only
    strings — so absence has to be *spelled*.  It used to be spelled ``"0.0"``, which the
    child then parsed into a float that read as a recording.  Both halves are asserted here
    because either one alone leaves the overload alive somewhere in the chain.

    Mutation: restore ``str(... or 0.0)`` in the parent, or ``float(... or 0.0)`` in the
    child → one of the two halves reddens."""
    # (a) the parent spells absence as absence.
    captured = {}

    class _Completed(object):
        stdout, stderr, returncode = "", "", 0

    def fake_run(*_args, **kwargs):
        captured.update(kwargs.get("env") or {})
        return _Completed()

    monkeypatch.setattr(_runner.subprocess, "run", fake_run)
    _runner._reap_owned({"pid": 4321, "create_time": None, "baseline": []})
    assert captured.get("OPTIVIBE_DOCTOR_REAP_CREATE_TIME") == "", (
        "the parent sent %r for an unrecorded create_time; a literal 0.0 arrives at the "
        "child looking like a reading"
        % (captured.get("OPTIVIBE_DOCTOR_REAP_CREATE_TIME"),))
    _runner._reap_owned({"pid": 4321, "create_time": 1234.5, "baseline": []})
    assert captured.get("OPTIVIBE_DOCTOR_REAP_CREATE_TIME") == "1234.5", (
        "a real recording must still travel verbatim")

    # (b) the child reads absence as absence: an unset/empty/unparseable var reaches
    # production's reaper as None, which is the value it refuses to kill on.
    for raw in ("", "not-a-float"):
        facts, calls = _run_reap_worker(monkeypatch, pid=4321, still_alive=True,
                                        create_time=raw)
        assert calls and calls[0][1] is None, (
            "an unrecorded create_time reached the reaper as %r instead of None; "
            "terminate_tracked refuses on None, so this is the value that keeps an "
            "unverifiable identity from being killed on" % (calls[0][1],))
        assert (facts["gone"], facts["ok"]) == (None, False), facts


def _reap_with_child(monkeypatch, stdout="", raises=None):
    """Drive the parent's reap arm with the child subprocess scripted."""
    class _Completed(object):
        def __init__(self, text):
            self.stdout, self.stderr, self.returncode = text, "", 0

    def fake_run(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return _Completed(stdout)

    monkeypatch.setattr(_runner.subprocess, "run", fake_run)
    return _runner._reap_owned({"pid": 4321, "create_time": 1.0, "baseline": []})


def test_e7_a_reap_worker_that_cannot_run_is_disclosed_not_assumed(monkeypatch):
    """E-7.  Doctor NEVER claims a reap it did not observe.

    Discriminating condition: the reap worker cannot run at all.  The happy path alone
    never exercises the disclosure, so a missing ``reap_unavailable`` would go unnoticed
    until the day it mattered — the day an orphan is holding the single seat."""
    for raises in (OSError("cannot spawn"),
                   subprocess.TimeoutExpired(cmd="reap", timeout=15.0)):
        reaped, unavailable = _reap_with_child(monkeypatch, raises=raises)
        assert reaped is False
        assert unavailable, (
            "a reap worker that could not run left no disclosure: %r" % (raises,))


def test_e7_a_silent_reap_child_is_disclosed(monkeypatch):
    """A child that produced no observation proves nothing, so it is named as unproven."""
    reaped, unavailable = _reap_with_child(monkeypatch, stdout="")
    assert (reaped, bool(unavailable)) == (False, True)


def test_e7_a_confirmed_reap_carries_no_disclosure(monkeypatch):
    """The negative control.  Without it, an implementation that reported every reap as
    unavailable would satisfy the guards above perfectly."""
    line = json.dumps({"schema": SCHEMA, "seq": 1, "type": "observation",
                       "check": "engine.reaped",
                       "facts": {"pid": 4321, "ok": True, "gone": True}})
    reaped, unavailable = _reap_with_child(monkeypatch, stdout=line + "\n")
    assert (reaped, unavailable) == (True, "")


def test_e7_a_surviving_process_is_disclosed_rather_than_claimed(monkeypatch):
    """The child says the process is still there: that is not a reap, and the orphan must
    be NAMED so the reader knows the seat is still held."""
    line = json.dumps({"schema": SCHEMA, "seq": 1, "type": "observation",
                       "check": "engine.reaped",
                       "facts": {"pid": 4321, "ok": False, "gone": False,
                                 "detail": "access denied"}})
    reaped, unavailable = _reap_with_child(monkeypatch, stdout=line + "\n")
    assert reaped is False
    assert "still running" in unavailable and "access denied" in unavailable


def test_e7_the_engine_finding_never_omits_either_disclosure_key():
    """Both keys, on every deadline path.  A missing key invites a reader to assume; a
    present ``false`` beside a named reason cannot be misread."""
    for reaped, unavailable in ((True, ""), (False, "psutil is broken")):
        observation = _runner._reap_observation({"pid": 4321}, reaped, unavailable)
        assert observation.facts["owned_pid_reaped"] is reaped
        assert observation.facts["reap_unavailable"] == unavailable
        assert "owned_pid_reaped" in observation.facts
        assert "reap_unavailable" in observation.facts


def test_e7_an_engine_deadline_kill_reports_the_reap_outcome_in_its_facts(monkeypatch):
    """The whole path, end to end at the seam: an --engine run killed at the overall
    deadline emits ``engine.license`` UNKNOWN/``deadline_exceeded`` carrying the owned PID
    and whether the OS confirmed it gone.  The run is INCOMPLETE either way — the orphan is
    reclaimed, or it is named, never silent."""
    emissions = dict(_healthy_emissions())
    emissions["engine"] = (Observation("engine.owned", {"pid": 4321, "create_time": 1.0,
                                                        "baseline": []}),)
    monkeypatch.setattr(_runner, "_reap_owned", lambda owned: (False, "psutil is broken"))
    _summary, _recorder, sink = _drive(
        args=_runner.Args(format="ndjson", engine=True), emissions=emissions,
        outcomes={"engine": "timeout"})
    engine = _sink_findings(sink)["engine.license"]
    assert (engine["status"], engine["reason"]) == ("unknown", "deadline_exceeded")
    assert engine["facts"]["owned_pid"] == 4321
    assert engine["facts"]["owned_pid_reaped"] is False
    assert engine["facts"]["reap_unavailable"] == "psutil is broken", (
        "the deadline path must NAME an unobserved reap, never omit it")


# ===========================================================================
# --engine, engine-free.  Everything here runs with no licence and no OpticStudio.
#
# What is NOT here, named rather than left silently uncovered — these need a real engine
# and belong to the live gate:
#   L-3  a valid licence, reap_failures empty, and the OS reporting the owned PID gone
#        while every pre-run OpticStudio PID is still alive
#   L-4  a forced-invalid licence completing in ONE attempt (~10 s, one engine spawned and
#        reaped) — the TIMING and PROCESS-COUNT half of max_connect_retries=1
#   L-8  the overall-deadline kill firing after open, with the orphan actually reclaimed
#        the watchdog's own behaviour against the real vendor calls, which only a real
#        engine can make block
# ===========================================================================
D_ENGINE_OPENED = (True, False, None)
D_ENGINE_LICENCE = (True, False, None)

#: The complete (opened, valid_license) product, and the finding each point must produce.
#: Enumerated in this file, never from the implementation.
ENGINE_EXPECTED = {
    (True, True): (Status.PASS, ""),
    (True, False): (Status.FAIL, "session_proof_failed"),
    (True, None): (Status.UNKNOWN, "license_unreadable"),
    (False, True): (Status.FAIL, "session_proof_failed"),
    (False, False): (Status.FAIL, "session_proof_failed"),
    (False, None): (Status.FAIL, "session_proof_failed"),
    (None, True): (Status.FAIL, "session_proof_failed"),
    (None, False): (Status.FAIL, "session_proof_failed"),
    (None, None): (Status.FAIL, "session_proof_failed"),
}


def test_engine_the_licence_grade_is_total_over_its_declared_domain():
    """The licence grader over the COMPLETE ``opened x valid_license`` product.

    Enumerated by ``itertools.product`` over domains declared here, so a point the rule
    forgets has nowhere to hide.  The load-bearing rows are the two that a naive
    implementation collapses: ``opened`` TRUE with the licence FALSE is a real refusal and
    must FAIL, while ``opened`` TRUE with the licence UNREADABLE is doctor failing to
    measure and must be UNKNOWN — grading on ``opened`` alone reports both as a pass."""
    universe = list(itertools.product(D_ENGINE_OPENED, D_ENGINE_LICENCE))
    assert len(universe) == len(ENGINE_EXPECTED)
    for opened, licence in universe:
        assert (opened, licence) in ENGINE_EXPECTED, (opened, licence)
        finding = _runner._grade_engine(
            "engine.license",
            {"opened": opened, "valid_license": licence, "owned_pid": 4321,
             "license_status": "SomeEdition", "error_type": None, "error": None},
            {}, {}, True)
        assert (finding.status, finding.reason) == ENGINE_EXPECTED[(opened, licence)], (
            opened, licence, finding.status, finding.reason)


def test_engine_a_failed_session_is_never_reported_as_an_invalid_licence():
    """The observation does not support "licence invalid" — only "the proof failed".

    A connection that never opened tells you nothing about the entitlement: the assemblies
    may be missing, the seat may be taken, the install may be broken.  Naming the licence
    would be doctor inventing a cause it did not observe."""
    finding = _runner._grade_engine(
        "engine.license",
        {"opened": False, "error_type": "RuntimeError", "error": "no seat"}, {}, {}, True)
    assert finding.status is Status.FAIL
    assert "licence/session proof failed" in finding.summary
    assert "invalid" not in finding.summary.lower(), finding.summary


#: A clean lifecycle: the session opened, and a post-close re-read of the process table
#: found the pid it owned gone.  The default for every licence-focused scenario below, so
#: that a licence assertion is not silently also asserting the cleanup half.
CLEAN_ENGINE_CLEANUP = {"session_created": True, "close_watchdog": "ok",
                        "reap_failures": [], "owned_pids": [4321],
                        "after_pids": [999], "still_running": []}


def _engine_run(facts, outcome="completed", cleanup=None):
    """Drive a full ``--engine`` run at the spawn seam with a canned engine worker."""
    emissions = dict(_healthy_emissions())
    engine = [Observation("engine.license", dict(facts))]
    if cleanup is not False:
        engine.append(Observation(
            "engine.cleanup", dict(CLEAN_ENGINE_CLEANUP if cleanup is None else cleanup)))
    emissions["engine"] = tuple(engine)
    return _drive(args=_runner.Args(format="ndjson", engine=True),
                  emissions=emissions, outcomes={"engine": outcome})


def test_engine_a_valid_licence_run_is_ready_and_exits_zero():
    """``--engine`` on a healthy box: the engine finding passes and gates like any other."""
    summary, recorder, sink = _engine_run(
        {"opened": True, "valid_license": True, "license_status": "SomeEdition",
         "owned_pid": 4321})
    assert [entry[0] for entry in recorder] == ["base", "pkg", "zemax", "engine"]
    engine = _sink_findings(sink)["engine.license"]
    assert engine["status"] == "pass", engine
    assert engine["blocked_by"] == "", "a requested check is never 'not requested'"
    assert summary.state is State.READY and exit_code(summary) == 0


def test_engine_an_invalid_licence_run_is_broken_and_exits_two():
    """The behaviour-matrix row, driven end to end rather than tabulated.

    Mutation: grade a refused licence anything other than FAIL and the exit stops being 2."""
    summary, _recorder, sink = _engine_run(
        {"opened": True, "valid_license": False, "license_status": "SomeEdition",
         "owned_pid": 4321})
    engine = _sink_findings(sink)["engine.license"]
    assert (engine["status"], engine["reason"]) == ("fail", "session_proof_failed")
    assert summary.state is State.BROKEN and exit_code(summary) == 2


def test_engine_an_unreadable_licence_is_incomplete_and_exits_three():
    """A watchdog timeout on the licence property is doctor failing to measure — exit 3,
    which is never a health claim.  Grading it PASS would report a green box doctor never
    actually read; grading it FAIL would blame an entitlement it never saw."""
    summary, _recorder, sink = _engine_run(
        {"opened": True, "valid_license": None, "valid_license_watchdog": "timeout",
         "owned_pid": 4321})
    engine = _sink_findings(sink)["engine.license"]
    assert (engine["status"], engine["reason"]) == ("unknown", "license_unreadable")
    assert summary.state is State.INCOMPLETE and exit_code(summary) == 3


def test_engine_the_licence_status_string_travels_into_the_facts_ungraded():
    """``LicenseStatus`` is a REPORTED FACT.  It reaches the reader and never the verdict:
    the one machine it was measured on is not the licence tier of every install."""
    _summary, _recorder, sink = _engine_run(
        {"opened": True, "valid_license": True, "license_status": "SomeOtherEdition",
         "owned_pid": 4321})
    engine = _sink_findings(sink)["engine.license"]
    assert engine["facts"]["license_status"] == "SomeOtherEdition"
    assert engine["status"] == "pass", (
        "an unexpected licence-tier token must not change the verdict")


# ---------------------------------------------------------------------------
# engine.cleanup — doctor's OWN footprint.
#
# The licence half and the lifecycle half fail independently, and the second one
# was ungraded: a run could prove the licence, fail to close the session, leave
# the owned engine running, and report PASS.  On a single-seat product that is a
# diagnostic causing the exact problem it was called about.
# ---------------------------------------------------------------------------
def _cleanup_finding(**facts):
    return _runner._grade_engine_cleanup("engine.cleanup", dict(facts), {}, {}, True)


def test_engine_cleanup_passes_only_on_a_post_close_os_observation():
    """PASS requires the process table to have been RE-READ after the close and to no
    longer contain the pid doctor owned."""
    finding = _cleanup_finding(session_created=True, close_watchdog="ok", reap_failures=[],
                               owned_pids=[4321], after_pids=[999], still_running=[])
    assert finding.status is Status.PASS
    assert "4321" in finding.summary


def test_engine_cleanup_fails_when_the_owned_engine_is_still_running():
    """The whole point.  Mutation: grade this anything but FAIL and ``--engine`` reports a
    green box while holding the single seat hostage."""
    finding = _cleanup_finding(session_created=True, close_watchdog="ok", reap_failures=[],
                               owned_pids=[4321], after_pids=[999, 4321],
                               still_running=[4321])
    assert (finding.status, finding.reason) == (Status.FAIL, "engine_leaked")
    assert "4321" in finding.summary


def test_engine_cleanup_never_infers_a_clean_close_from_the_close_call_alone():
    """A watchdog that says "ok" is the tool's own report, not an observation.  With the
    OS re-read missing the answer is UNKNOWN — doctor could not measure — never PASS."""
    finding = _cleanup_finding(session_created=True, close_watchdog="ok", reap_failures=[],
                               owned_pids=[4321], after_pids=None, still_running=None)
    assert (finding.status, finding.reason) == (Status.UNKNOWN, "cleanup_unobserved")


def test_engine_cleanup_warns_when_the_engine_is_gone_but_the_close_did_not_finish():
    """Gone is the safety property and is satisfied; an unfinished close is still worth a
    line, and WARN is the verdict that says "usable, but something happened"."""
    finding = _cleanup_finding(session_created=True, close_watchdog="timeout",
                               reap_failures=[], owned_pids=[4321], after_pids=[999],
                               still_running=[])
    assert (finding.status, finding.reason) == (Status.WARN, "close_incomplete")


def test_engine_cleanup_warns_when_the_session_reported_reap_failures():
    finding = _cleanup_finding(session_created=True, close_watchdog="ok",
                               reap_failures=["could not terminate 7"], owned_pids=[4321],
                               after_pids=[999], still_running=[])
    assert (finding.status, finding.reason) == (Status.WARN, "reap_reported_failures")
    assert "could not terminate 7" in finding.summary


def test_engine_cleanup_passes_when_no_session_was_ever_created():
    """The assemblies-missing path.  There is no engine, so nothing was left behind — and
    saying so explicitly is what stops the parent synthesising UNKNOWN and turning a clean
    ``engine.license`` FAIL (exit 2) into INCOMPLETE (exit 3)."""
    finding = _cleanup_finding(session_created=False)
    assert finding.status is Status.PASS


def test_engine_cleanup_is_a_real_gate_in_a_full_run():
    """End to end: a leaked engine takes the run to BROKEN/2 even though the licence
    itself is perfectly valid."""
    summary, _recorder, sink = _engine_run(
        {"opened": True, "valid_license": True, "license_status": "SomeEdition",
         "owned_pid": 4321},
        cleanup={"session_created": True, "close_watchdog": "timeout", "reap_failures": [],
                 "owned_pids": [4321], "after_pids": [4321], "still_running": [4321]})
    findings = _sink_findings(sink)
    assert findings["engine.license"]["status"] == "pass"
    assert findings["engine.cleanup"]["status"] == "fail"
    assert summary.state is State.BROKEN and exit_code(summary) == 2


def test_engine_cleanup_is_owed_by_the_engine_worker_and_gated_by_the_flag():
    assert "engine.cleanup" in _runner.WORKER_CHECKS["engine"]
    _summary, _recorder, sink = _drive(emissions=_healthy_emissions())
    record = _sink_findings(sink)["engine.cleanup"]
    assert record["status"] == "skip" and record["blocked_by"] == NOT_REQUESTED
    assert "--engine" in record["summary"], (
        "a not-requested SKIP must say which flag to pass")


def test_engine_cleanup_after_a_deadline_kill_grades_the_reap_that_actually_happened():
    """The overall-deadline path performs a targeted reap whose proof is a post-kill OS
    re-read.  That IS this run's cleanup, so discarding it and reporting UNKNOWN would be
    doctor saying "could not measure" about a measurement it made."""
    reaped = _runner._reap_cleanup_observation({"pid": 4321}, True, "")
    assert _cleanup_finding(**dict(reaped.facts)).status in (Status.PASS, Status.WARN)
    assert _cleanup_finding(**dict(reaped.facts)).reason != "engine_leaked"

    leaked = _runner._reap_cleanup_observation({"pid": 4321}, False, "")
    assert _cleanup_finding(**dict(leaked.facts)).reason == "engine_leaked"

    unobserved = _runner._reap_cleanup_observation(
        {"pid": 4321}, False, "the reap worker could not be started")
    assert _cleanup_finding(**dict(unobserved.facts)).reason == "cleanup_unobserved"


def test_engine_without_the_flag_the_check_skips_and_never_gates():
    """The default run: SKIP/``not_requested``, which by contract never gates.  A SKIP that
    gated would make every ordinary run pay for a probe it did not ask for."""
    summary, recorder, sink = _drive(emissions=_healthy_emissions())
    assert "engine" not in [entry[0] for entry in recorder]
    engine = _sink_findings(sink)["engine.license"]
    assert engine["status"] == "skip" and engine["blocked_by"] == NOT_REQUESTED
    assert "--engine" in engine["summary"]
    assert exit_code(summary) == 0


def _engine_worker_body():
    return _functions_of(_doctor_tree("_worker"))["worker_engine"]


def test_engine_the_connect_retry_count_is_pinned_to_one():
    """One attempt.  An entitlement rejection is DEFINITIVE, so the default three costs
    ~21 s and spawns three engines to reach the same verdict — on a single-seat machine
    that is a diagnostic causing the problem it was called about.

    Mutation: restore ``max_connect_retries=3`` and this reddens.  The timing half — that
    the run really does finish in one attempt — is live gate L-4."""
    keywords = [keyword for node in ast.walk(_engine_worker_body())
                if isinstance(node, ast.Call)
                for keyword in node.keywords
                if keyword.arg == "max_connect_retries"]
    assert len(keywords) == 1, "exactly one session is opened, with the retry count pinned"
    assert isinstance(keywords[0].value, ast.Constant)
    assert keywords[0].value.value == 1, keywords[0].value.value


def test_engine_the_owned_pid_is_declared_before_any_licence_property_is_read():
    """``engine.owned`` must be flushed BEFORE the first vendor property read.

    That ordering is what makes a post-open kill recoverable: the parent has to know which
    PID doctor owns even if the worker never speaks again.  Emitting it afterwards leaves an
    orphan nobody can name — which is precisely the recovery half of the deadline finding.

    Mutation: move the emit below the licence reads and this reddens."""
    body = _engine_worker_body()
    owned_at, first_app_read_at = None, None
    for node in ast.walk(body):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "emit" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "engine.owned"):
            owned_at = node.lineno if owned_at is None else min(owned_at, node.lineno)
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "app"):
            first_app_read_at = (node.lineno if first_app_read_at is None
                                 else min(first_app_read_at, node.lineno))
    assert owned_at is not None, "the owned pid must be declared at all"
    assert first_app_read_at is not None, "the worker must actually read the licence"
    assert owned_at < first_app_read_at, (owned_at, first_app_read_at)


def test_engine_every_post_open_vendor_call_is_watchdogged():
    """Each post-open call individually bounded by PRODUCTION's own watchdog.

    Never re-implemented here: a second timeout mechanism is a second set of semantics, and
    the one that matters is the one the harness already ships.  ``session.close()`` counts —
    a close that wedges strands the seat exactly as a wedged property read does.

    The structural property that makes a call bounded is that it is DEFERRED — handed to the
    watchdog as a callable rather than evaluated where it is written.  An eager
    ``bool(session.app.IsValidLicenseForAPI)`` has already blocked by the time the watchdog
    could have been given anything, so "is it inside a lambda" is the real question and
    "does a watchdog call appear nearby" is not.

    Mutation: read ``session.app.LicenseStatus`` eagerly, or call ``session.close()``
    directly instead of passing it, and this reddens."""
    body = _engine_worker_body()
    watchdogged = [node for node in ast.walk(body)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "_run_with_watchdog"]
    assert len(watchdogged) >= 2, "the licence reads and the close are all bounded"

    # ``body`` is itself a FunctionDef, so seeding this from ast.walk(body) would mark
    # EVERY node deferred and the guard would assert nothing.  Only nodes strictly INSIDE a
    # nested lambda or def count.
    deferred = set()
    for node in ast.walk(body):
        if node is body:
            continue
        if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                deferred.add(id(sub))
    assert deferred, "the worker must defer its vendor calls into callables at all"

    reads = [node for node in ast.walk(body)
             if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
             and node.value.attr == "app"]
    assert reads, "the worker must actually read the licence"
    for node in reads:
        assert id(node) in deferred, (
            "an EAGER vendor property read at line %s — the watchdog can only bound a call "
            "it is handed" % node.lineno)

    # session.close must be PASSED, never invoked: `_run_with_watchdog(session.close, ...)`.
    passed = [arg for call in watchdogged for arg in call.args
              if isinstance(arg, ast.Attribute) and arg.attr == "close"
              and isinstance(arg.value, ast.Name) and arg.value.id == "session"]
    assert passed, "session.close must be handed to the watchdog"
    invoked = [node for node in ast.walk(body)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "close"
               and isinstance(node.func.value, ast.Name) and node.func.value.id == "session"
               and id(node) not in deferred_lines]
    assert invoked == [], (
        "session.close() is invoked directly at line(s) %r instead of being watchdogged"
        % ([node.lineno for node in invoked],))


def test_engine_the_session_is_closed_on_every_path():
    """The close lives in a ``finally``.  A licence read that raises must not leave the
    seat held — the whole point of opening one is that doctor gives it straight back."""
    body = _engine_worker_body()
    closed_in_finally = []
    for node in ast.walk(body):
        if not isinstance(node, ast.Try) or not node.finalbody:
            continue
        for item in node.finalbody:
            for sub in ast.walk(item):
                if (isinstance(sub, ast.Attribute) and sub.attr == "close"
                        and isinstance(sub.value, ast.Name) and sub.value.id == "session"):
                    closed_in_finally.append(sub.lineno)
    assert closed_in_finally, "session.close() must run from a finally, not a happy path"


def test_engine_a_concurrent_seat_is_disclosed_and_never_refused():
    """The concurrent-seat reading is DISCLOSURE ONLY.  A concurrent seat is somebody's live design
    session; doctor reports it and proceeds.  Refusing to run because a colleague is
    working would make the diagnostic useless exactly when it is wanted.

    Lexical ordering alone cannot show this — inserting ``if live: return`` after the
    disclosure leaves every line in the same order — so the discriminating property is that
    the seat reading must never reach CONTROL FLOW.  The name it is carried in is taken from
    the disclosure's own ``concurrent_pids`` argument rather than hardcoded, so renaming the
    variable cannot silently disarm the guard."""
    body = _engine_worker_body()
    seat_calls = [node for node in ast.walk(body)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "emit" and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and node.args[0].value == "engine.seat"]
    assert seat_calls, "the concurrent seat must be disclosed at all"

    carriers = set()
    for call in seat_calls:
        for keyword in call.keywords:
            if keyword.arg == "concurrent_pids":
                carriers.update(sub.id for sub in ast.walk(keyword.value)
                                if isinstance(sub, ast.Name))
    assert carriers, "the disclosure must actually carry the concurrent pids"

    for node in ast.walk(body):
        tested = None
        if isinstance(node, (ast.If, ast.While)):
            tested = node.test
        elif isinstance(node, (ast.Return, ast.Raise)):
            tested = node
        if tested is None:
            continue
        used = {sub.id for sub in ast.walk(tested) if isinstance(sub, ast.Name)}
        assert not (used & carriers), (
            "the concurrent-seat reading reaches control flow at line %s (%r) — it is a "
            "DISCLOSURE, and refusing to run because a colleague holds the seat makes the "
            "diagnostic useless exactly when it is wanted" % (node.lineno, sorted(used & carriers)))

    opens = [node.lineno for node in ast.walk(body)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "open"]
    assert opens and max(opens) > min(call.lineno for call in seat_calls), (
        "the open must still happen after the disclosure")
    assert "engine.seat" not in BASE_CHECKS, (
        "a disclosure is not a spine check and must never be able to gate")


def test_e2_the_engine_worker_carries_both_deadlines():
    """The overall deadline is never relinquished.  Handing the kill deadline over at
    preflight leaves a licence read that wedges afterwards with no bound at all — which was
    the finding, not a hypothetical."""
    _summary, recorder, _sink = _drive(
        args=_runner.Args(format="ndjson", engine=True), emissions=_healthy_emissions())
    engine = [entry for entry in recorder if entry[0] == "engine"][0]
    assert engine[1] == _runner.ENGINE_PREFLIGHT_DEADLINE_S
    assert engine[3]["extend_on"] == "engine.preflight"
    assert engine[3]["extend_to"] == _runner.ENGINE_OVERALL_DEADLINE_S
    assert _runner.ENGINE_OVERALL_DEADLINE_S > _runner.ENGINE_PREFLIGHT_DEADLINE_S


#: The band doctor clamps the operator's ``OPTIVIBE_CONNECT_TIMEOUT_S`` into.  Duplicated
#: as literals: a band read from the module under test moves with the mutation.
ZEMAX_DEADLINE_FLOOR_S = 10.0
ZEMAX_DEADLINE_CEILING_S = 300.0


def test_every_worker_is_spawned_with_a_positive_finite_deadline():
    """``positive and finite`` is necessary and nowhere near sufficient, so it is asserted
    together with the BAND below rather than alone.

    On its own this predicate is satisfied by ``0.001`` — which kills a healthy child
    before its interpreter has started — and by ``31536000``, which is a year.  A guard
    that both pathological values pass cannot fail for ANY value an operator can set, only
    for a hardcoded non-positive constant nobody would write."""
    _summary, recorder, _sink = _drive(
        args=_runner.Args(format="ndjson", boot=True, engine=True),
        emissions=_healthy_emissions())
    assert recorder, "no worker was spawned at all"
    for name, deadline, _env, _kw in recorder:
        assert isinstance(deadline, float) and deadline > 0, name
        assert deadline == deadline and deadline != float("inf"), name
        assert 1.0 <= deadline <= 24 * 3600, (
            "%s is spawned with a %ss deadline, which is not an operational bound"
            % (name, deadline))


@pytest.mark.parametrize("raw,expected", [
    ("0.001", ZEMAX_DEADLINE_FLOOR_S),          # kills a healthy child before it reports
    ("0.05", ZEMAX_DEADLINE_FLOOR_S),
    ("1e9", ZEMAX_DEADLINE_CEILING_S),
    ("31536000", ZEMAX_DEADLINE_CEILING_S),     # a year: not a bound at all
    ("-5", ZEMAX_DEADLINE_FLOOR_S),
])
def test_the_zemax_deadline_is_clamped_into_doctors_own_band(monkeypatch, raw, expected):
    """The operator's knob is READ but never obeyed unconditionally.

    Both ends destroy the diagnostic in opposite directions — below the floor a HEALTHY
    machine reports UNKNOWN twice and exits 3 (the cry-wolf direction, which is total loss
    of function); above the ceiling the child that loads the .NET assemblies has no bound
    at all, contradicting ``_runner``'s own rule that every deadline is a kill."""
    monkeypatch.setenv("OPTIVIBE_CONNECT_TIMEOUT_S", raw)
    assert _runner._zemax_deadline_s() == expected


@pytest.mark.parametrize("raw", ["15", "42.5", "300", "10"])
def test_a_configured_zemax_deadline_inside_the_band_is_honoured_verbatim(monkeypatch, raw):
    """The clamp bounds the knob; it must not silently replace it.  A value inside the band
    is used exactly, or the setting has been removed rather than made safe."""
    monkeypatch.setenv("OPTIVIBE_CONNECT_TIMEOUT_S", raw)
    assert _runner._zemax_deadline_s() == float(raw)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "", "abc", "  "])
def test_an_uninterpretable_zemax_deadline_falls_back_to_the_default(monkeypatch, raw):
    """A value that is not a duration at all is not clamped into one — it is discarded, and
    the pinned default applies.  Clamping ``nan`` would produce a deadline arithmetic
    cannot compare against."""
    monkeypatch.setenv("OPTIVIBE_CONNECT_TIMEOUT_S", raw)
    value = _runner._zemax_deadline_s()
    assert value == _runner.DEFAULT_ZEMAX_DEADLINE_S
    assert value == value and value not in (float("inf"), float("-inf"))


def test_the_healthy_synthetic_run_is_the_control_for_every_guard_below():
    """Without this control a guard asserting "this run is BROKEN" cannot tell a detected
    fault from a permanently broken fixture."""
    summary, _recorder, sink = _drive(emissions=_healthy_emissions())
    findings = _sink_findings(sink)
    non_pass = {check: record["status"] for check, record in findings.items()
                if record["status"] != "pass"}
    assert set(non_pass) <= {"boot.exit", "engine.license", "engine.cleanup",
                             "dependency.coverage", "plane.coverage",
                             "tool.coverage"}, non_pass
    assert findings["boot.exit"]["status"] == "skip"
    assert findings["boot.exit"]["blocked_by"] == NOT_REQUESTED
    assert findings["engine.license"]["blocked_by"] == NOT_REQUESTED
    assert findings["engine.cleanup"]["blocked_by"] == NOT_REQUESTED
    assert summary.complete is True


def test_a_not_requested_skip_says_which_flag_to_pass():
    """The renderer prints a bare ``blocked by <id>`` for a prerequisite SKIP.  A
    not-requested SKIP has to carry its own sentence or the reader learns nothing at all
    from the line."""
    _summary, _recorder, sink = _drive(emissions=_healthy_emissions())
    findings = _sink_findings(sink)
    assert "--boot" in findings["boot.exit"]["summary"]
    assert "--engine" in findings["engine.license"]["summary"]


# ===========================================================================
# D-1  spine coverage, proven on a deliberately incomplete run
# ===========================================================================
def test_d1_a_dropped_finding_makes_the_run_incomplete_and_exits_three():
    """D-1.  Discriminating condition: the run is deliberately MISSING one spine id.

    Against a complete ambient run, an implementation that derives its expectations from
    what it received passes exactly as well as one that duplicates the literal tuple — the
    dropped finding is invisible to it.  Only an incomplete run separates the two."""
    emissions = _healthy_emissions()
    emissions["pkg"] = [item for item in emissions["pkg"] if item.check != "mcp.construct"]
    summary, _recorder, sink = _drive(emissions=emissions)
    findings = _sink_findings(sink)
    assert findings["mcp.construct"]["status"] == "unknown"
    assert summary.state is State.INCOMPLETE
    assert exit_code(summary) == 3


def test_d1_a_worker_that_delivers_nothing_makes_only_its_own_ids_unknown():
    emissions = _healthy_emissions()
    emissions["zemax"] = []
    summary, _recorder, sink = _drive(emissions=emissions, outcomes={"zemax": "timeout"})
    findings = _sink_findings(sink)
    assert findings["zemax.nethelper"]["status"] == "unknown"
    assert findings["zemax.nethelper"]["reason"] == "deadline_exceeded"
    assert findings["zemax.dir"]["status"] == "unknown"
    assert findings["harness.manifest"]["status"] == "pass"
    assert exit_code(summary) == 3


# ===========================================================================
# D-2 / D-3  the exit code is the state machine's, and a fault is 3
# ===========================================================================
def test_d2_the_summary_exit_code_is_the_state_machines():
    summary, _recorder, _sink = _drive(emissions=_healthy_emissions())
    assert summary.exit_code == exit_code(summary) == EXIT_BY_STATE[summary.state]


def test_d2_a_runner_fault_returns_three_and_writes_one_stderr_line():
    """A fault inside doctor is INCOMPLETE, never a health claim — and never a traceback.
    Exit 3 is "doctor could not measure", which is the only honest answer available."""
    completed = subprocess.run(
        [sys.executable, "-c",
         "import sys, optivibe_doctor._runner as r\n"
         "def boom(*a, **k):\n"
         "    raise RuntimeError('injected runner fault')\n"
         "r.run = boom\n"
         "sys.exit(r.main([]))\n"],
        capture_output=True, text=True, env=_child_env(), timeout=180)
    assert completed.returncode == 3
    assert completed.stdout == ""
    assert len(completed.stderr.strip().splitlines()) == 1
    assert "Traceback" not in completed.stderr
    assert "injected runner fault" in completed.stderr


def test_d3_a_worker_that_raises_baseexception_loses_only_its_own_ids():
    """D-3.  The parent must survive a worker that does not merely fail but detonates, and
    must still emit a complete document."""

    def spawn(name, deadline_s, env_extra=None, **kwargs):
        if name == "pkg":
            raise BaseException("the pkg worker detonated")
        for observation in _healthy_emissions().get(name, ()):
            yield observation
        yield _runner._outcome("completed")

    sink = _Sink()
    summary = _runner.run(_runner.Args(format="ndjson"), spawn=spawn, out=sink)
    records = [json.loads(line) for line in sink.text.splitlines() if line.strip()]
    findings = _findings_of(records)
    assert findings["env.python"]["status"] == "pass"
    assert findings["zemax.dir"]["status"] == "pass"
    assert findings["mcp.construct"]["status"] == "unknown"
    assert exit_code(summary) == 3
    assert records[-1]["type"] == "summary"


# ===========================================================================
# D-8  the stream is a stream, not a document
# ===========================================================================
def test_d8_a_finding_is_readable_before_the_run_completes():
    """D-8.  Discriminating condition: a worker that BLOCKS after the earlier stages have
    already reported.

    A buffered renderer produces the identical final document, so only a bounded read taken
    while the run is still in progress can tell the two implementations apart."""
    import threading

    released = threading.Event()
    seen = []
    sink = _Sink()

    def spawn(name, deadline_s, env_extra=None, **kwargs):
        for observation in _healthy_emissions().get(name, ()):
            yield observation
        if name == "pkg":
            seen.append(sink.text)
            released.wait(30)
        yield _runner._outcome("completed")

    def go():
        _runner.run(_runner.Args(format="ndjson"), spawn=spawn, out=sink)

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    released.set()
    thread.join(30)
    assert seen, "the injected worker never ran"
    early = [json.loads(line) for line in seen[0].splitlines() if line.strip()]
    assert early, "nothing was flushed before the run completed"
    assert {record["check"] for record in early} >= {"env.python"}
    assert all(record["type"] == "finding" for record in early)


def test_d8_ndjson_stdout_carries_nothing_but_json_and_stderr_is_empty():
    completed = _run_doctor(["--format", "ndjson"])
    assert completed.stderr == "", completed.stderr[-2000:]
    records = _ndjson_of(completed)
    assert records and records[-1]["type"] == "summary"
    for record in records:
        assert record["schema"] == SCHEMA


# ===========================================================================
# D-11  a worker may not hand back a verdict
# ===========================================================================
@pytest.mark.parametrize("worker", ("base", "pkg"))
def test_d11_no_worker_emits_a_status_key(worker):
    """Rule 2.  ``Observation`` has no status field, so a probe cannot hand-write one even
    by accident — and the parent strips one anyway if a future worker tries."""
    records, completed = _run_worker(worker)
    assert records, "the %s worker emitted nothing (%s)" % (worker, completed.stderr[-500:])
    for check, facts in records.items():
        assert "status" not in facts, (check, facts)


def test_d11_the_parent_strips_a_hand_written_verdict_at_the_boundary():
    """Mutate-fails: a worker that tries to smuggle a verdict has it removed at the
    boundary, so no code path downstream can ever read one."""
    observation = _runner._parse_line(
        json.dumps({"check": "plane.glass", "facts": {"status": "pass", "present": True}}))
    assert observation is not None
    assert "status" not in observation.facts
    assert observation.facts["present"] is True


@pytest.mark.parametrize("line", [
    "", "   ", "not json at all", "[1, 2]", "null",
    '{"check": 3, "facts": {}}', '{"check": "x"}', '{"facts": {}}',
])
def test_d11_a_malformed_line_is_not_an_observation(line):
    assert _runner._parse_line(line) is None


# ===========================================================================
# D-15  no fabricated mechanism, ever
# ===========================================================================
_UNRESOLVED = (
    ("no harness", {"harness_import": False}),
    ("no nethelper", {"harness_import": True, "nethelper": False}),
    ("no pythonnet", {"harness_import": True, "nethelper": True, "loaded": False,
                      "error_type": "ModuleNotFoundError", "error": "No module named 'clr'"}),
    ("assemblies failed", {"harness_import": True, "nethelper": True, "loaded": False,
                           "error_type": "RuntimeError", "error": "no ZOSAPI.dll"}),
    ("resolution unrecorded", {"harness_import": True, "nethelper": True, "loaded": True,
                               "resolution": None}),
)


@pytest.mark.parametrize("facts", [row[1] for row in _UNRESOLVED],
                         ids=[row[0] for row in _UNRESOLVED])
def test_d15_an_unresolved_zemax_dir_names_no_mechanism(facts):
    """D-15, and it is a NEGATIVE assertion — it reddens on the APPEARANCE of a forbidden
    key, which nothing about the environment can mask.

    The key must be ABSENT, not null and not ``"unknown"``: a key that is present but empty
    invites a consumer to fill it in, and a fabricated ``registry-autodetect`` on a machine
    with no OpticStudio is exactly the false answer the write-once memo exists to prevent."""
    observations = {
        "zemax.dir": Observation("zemax.dir", facts),
        "zemax.nethelper": Observation("zemax.nethelper",
                                       {"harness_import": True, "found": False}),
    }
    findings = evaluate(observations, checks=("zemax.nethelper", "zemax.dir"))
    zemax_dir = [item for item in findings if item.check == "zemax.dir"][0]
    assert zemax_dir.status is not Status.PASS
    assert zemax_dir.summary == "not resolved in this process"
    assert "mechanism" not in zemax_dir.facts
    assert "dir" not in zemax_dir.facts


def test_d15_a_resolved_zemax_dir_reports_the_mechanism_production_recorded():
    observations = {"zemax.dir": Observation("zemax.dir", {
        "harness_import": True, "nethelper": True, "loaded": True,
        "resolution": ["C:/Program Files/Zemax", "program-files-scan"]})}
    finding = evaluate(observations, checks=("zemax.dir",))[0]
    assert finding.status is Status.PASS
    assert finding.facts["mechanism"] == "program-files-scan"
    assert finding.facts["dir"] == "C:/Program Files/Zemax"
    assert "program-files-scan" in finding.summary


def test_d15_program_files_scan_is_a_pass_not_a_degradation():
    """The README documents the scan as one of three supported arms; grading it WARN would
    report a perfectly ordinary install as degraded."""
    observations = {"zemax.dir": Observation("zemax.dir", {
        "harness_import": True, "nethelper": True, "loaded": True,
        "resolution": ["C:/z", "program-files-scan"]})}
    assert evaluate(observations, checks=("zemax.dir",))[0].status is Status.PASS


def test_d15_the_pinned_summary_survives_rendering():
    """The acceptance is read from stdout, so the string has to survive the renderer."""
    observations = {"zemax.dir": Observation("zemax.dir", {"harness_import": False})}
    finding = evaluate(observations, checks=("zemax.dir",))[0]
    assert "not resolved in this process" in " ".join(_render.human_finding_lines(finding))


def test_d15_a_missing_nethelper_skips_the_directory_rather_than_failing_it():
    """A machine with no OpticStudio is DEGRADED, not BROKEN: the package boots, serves its
    tools and answers the reference layer without it.  SKIP is the honest verdict — doctor
    measured perfectly well and there was nothing to resolve."""
    observations = {
        "zemax.nethelper": Observation("zemax.nethelper",
                                       {"harness_import": True, "found": False}),
        "zemax.dir": Observation("zemax.dir",
                                 {"harness_import": True, "nethelper": False}),
    }
    findings = evaluate(observations, checks=("zemax.nethelper", "zemax.dir"))
    nethelper, zemax_dir = findings
    assert (nethelper.status, nethelper.reason) == (Status.WARN, "absent")
    assert nethelper.remedy
    assert zemax_dir.status is Status.SKIP
    assert zemax_dir.blocked_by == "zemax.nethelper"


# ===========================================================================
# C-1 .. C-4  the data planes, synthesised through the declared seam
# ===========================================================================
def _synthetic_plane_run(tmp_path, contents, dispatcher=None):
    """Drive the REAL plane prober against an injected tree; return ``{check: facts}``.

    The seam is ``worker_pkg(planes=...)``'s own path map, so the guard exercises the
    production prober rather than a re-description of it."""
    captured = {}
    saved = _worker.emit
    paths = {}
    for plane, payload in contents.items():
        target = os.path.join(str(tmp_path), "%s.data" % plane)
        if payload is not None:
            with open(target, "wb") as handle:
                handle.write(payload)
        paths[plane] = target
    try:
        _worker.emit = lambda check, **facts: captured.__setitem__(check, facts)
        _worker._probe_planes(paths, dispatcher)
    finally:
        _worker.emit = saved
    return captured


def _plane_findings(captured, extra=None):
    observations = {check: Observation(check, facts) for check, facts in captured.items()}
    observations.update(extra or {})
    return evaluate(observations, checks=tuple("plane." + p for p in PLANE_NAMES))


def test_c1_an_empty_tree_warns_with_a_remedy_and_is_not_broken(tmp_path):
    """C-1.  Discriminating condition: every plane file is ABSENT.

    A fresh install is the single commonest state of this package.  Grading it BROKEN would
    train every new user to ignore the tool on their very first run, which is worse than
    having no tool."""
    captured = _synthetic_plane_run(tmp_path, {plane: None for plane in PLANE_NAMES})
    findings = _plane_findings(captured)
    assert [item.status for item in findings] == [Status.WARN] * 4
    assert [item.reason for item in findings] == ["absent_expected"] * 4
    for item in findings:
        assert item.facts["present"] is False, item.check
        assert item.remedy, "an absent plane with no remedy is a dead end"
    spine = [_finding(check) for check in BASE_CHECKS if not check.startswith("plane.")]
    summary = summarize(findings + spine + [_finding("plane.coverage")])
    assert summary.state is not State.BROKEN


def test_c2_a_truncated_tree_is_corrupt_not_absent_and_not_healthy(tmp_path):
    """C-2.  Discriminating condition: the files EXIST and are unopenable.

    An implementation that classifies on ``isfile`` alone reports them healthy.  This is
    also the pin of the OPENER's own acceptance predicate: if a future opener learns to
    accept a truncated file, this guard changes and says so rather than drifting."""
    captured = _synthetic_plane_run(tmp_path, {
        "merit_operand": b"{", "tolerance_operand": b"{", "glass": b"{",
        "manual": b"this is not a database"})
    findings = _plane_findings(captured)
    for item in findings:
        assert item.facts["present"] is True, item.check
        assert item.facts["opens"] is False, item.check
        assert (item.status, item.reason) == (Status.FAIL, "corrupt"), item.check


def test_c3_every_absent_plane_remedy_is_checkout_aware(tmp_path):
    """C-3 crossed with D-16.  Discriminating condition: the SAME plane observations are
    graded twice, once with a locatable manifest and once without.

    A single-fixture guard passes against an implementation that emits one remedy
    unconditionally, and a checkout-less install would be told to run commands that are not
    on its disk."""
    captured = _synthetic_plane_run(tmp_path, {plane: None for plane in PLANE_NAMES})
    origin = os.path.join("C:", "src", "optivibe_harness", "__init__.py")

    def version(manifest):
        return {"version.optivibe-harness": Observation("version.optivibe-harness", {
            "meta": "0.1.3", "source": "0.1.3", "manifest": manifest, "origin": origin})}

    for item in _plane_findings(captured, version("0.1.3")):
        assert item.remedy == _contract.REMEDY["vendor_data"], item.check
    for item in _plane_findings(captured, version(None)):
        assert item.remedy == _contract.REMEDY["vendor_data_no_checkout"], item.check


def test_c4_every_default_plane_path_resolves_inside_the_reference_package():
    """C-4's location half.  C-4 is load-bearing only because a location check and an AST
    check are both present: this half alone does not redden against a hardcoded path that
    happens to be right."""
    import optivibe_reference

    base = os.path.normcase(os.path.abspath(os.path.dirname(optivibe_reference.__file__)))
    resolved = _worker._plane_paths(None)
    assert set(resolved) == set(PLANE_NAMES)
    for plane, path in sorted(resolved.items()):
        assert path, plane
        assert os.path.normcase(os.path.abspath(path)).startswith(base), (plane, path)


def _write_corpus(path, body):
    """Write a corpus the reference opener ACCEPTS: complete, consistent, one chunk.

    The opener answers ``None`` for a partial build, so a hand-made table is not enough —
    an incomplete fixture never reaches the wiring check and the guard below would pass
    without ever exercising the thing it claims to grade."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE meta (pdf_sha256 TEXT, pdf_page_count INTEGER, chunk_count INTEGER,"
        " optic_studio_version TEXT, builder_version TEXT, build_complete INTEGER)")
    conn.execute("CREATE TABLE manual_chunk (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO manual_chunk (id, body) VALUES (1, ?)", (body,))
    conn.execute("INSERT INTO meta VALUES (?, ?, ?, ?, ?, ?)",
                 ("0" * 64, 1, 1, "2025 R1", "test", 1))
    conn.commit()
    conn.close()
    return path


def test_c5_a_connection_wired_to_another_planes_data_is_misrouted(tmp_path):
    """C-5, and HIGH-5's test half.  Discriminating condition: the file is present, the
    opener SUCCEEDS, the dispatcher property is NON-NULL — and it is bound to a different
    file.

    Under that fixture ``present``, ``opens`` and ``wired`` are all true, so nothing but
    ``identity_ok`` can catch it.  An implementation that ignores the fourth input reports
    PASS on a connection serving another plane's data."""
    import sqlite3

    mine = _write_corpus(os.path.join(str(tmp_path), "manual.data"), "the real corpus")
    other = _write_corpus(os.path.join(str(tmp_path), "elsewhere.db"), "somebody else")

    class _Misrouted:
        def __init__(self, wrong):
            self.manual_conn = sqlite3.connect(wrong)

    dispatcher = _Misrouted(other)
    try:
        captured = _synthetic_plane_run(tmp_path, {"manual": None}, dispatcher)
        facts = captured["plane.manual"]
        assert facts["present"] is True
        assert facts["opens"] is True, facts
        assert facts["wired"] is True, (
            "the fixture must reach the identity check, or it grades nothing")
        assert facts["identity_ok"] is False, (
            "the wired connection resolves to %r, not to %r"
            % (facts["wired_path"], mine))
        finding = evaluate({"plane.manual": Observation("plane.manual", facts)},
                           checks=("plane.manual",))[0]
        assert (finding.status, finding.reason) == (Status.FAIL, "misrouted")
    finally:
        dispatcher.manual_conn.close()


def test_c5_a_correctly_wired_corpus_is_not_misrouted(tmp_path):
    """The negative control for C-5.  Without it, an implementation that reported EVERY
    plane misrouted would satisfy the guard above perfectly."""
    import sqlite3

    mine = _write_corpus(os.path.join(str(tmp_path), "manual.data"), "the real corpus")

    class _Wired:
        def __init__(self, path):
            self.manual_conn = sqlite3.connect(path)

    dispatcher = _Wired(mine)
    try:
        captured = _synthetic_plane_run(tmp_path, {"manual": None}, dispatcher)
        facts = captured["plane.manual"]
        assert facts["present"] is True and facts["wired"] is True, facts
        assert facts["identity_ok"] is True, facts
        finding = evaluate({"plane.manual": Observation("plane.manual", facts)},
                           checks=("plane.manual",))[0]
        assert finding.status is Status.PASS
    finally:
        dispatcher.manual_conn.close()


def test_c2_a_partially_built_corpus_is_refused_rather_than_reported_healthy(tmp_path):
    """The corpus opener answers ``None`` for an inconsistent build instead of raising.

    Discriminating condition: ``build_complete`` is 0, so the FILE is a perfectly valid
    sqlite database and only the opener's own consistency rule rejects it.  An
    implementation that lets the None fall through reports an ``AttributeError`` and
    describes a half-built corpus as an internal doctor failure."""
    import sqlite3

    target = os.path.join(str(tmp_path), "manual.data")
    conn = sqlite3.connect(target)
    conn.execute(
        "CREATE TABLE meta (pdf_sha256 TEXT, pdf_page_count INTEGER, chunk_count INTEGER,"
        " optic_studio_version TEXT, builder_version TEXT, build_complete INTEGER)")
    conn.execute("CREATE TABLE manual_chunk (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO meta VALUES (?, ?, ?, ?, ?, ?)",
                 ("0" * 64, 1, 1, "2025 R1", "test", 0))
    conn.commit()
    conn.close()

    captured = _synthetic_plane_run(tmp_path, {"manual": None}, None)
    facts = captured["plane.manual"]
    assert facts["present"] is True
    assert facts["opens"] is False, facts
    assert "AttributeError" not in (facts.get("open_error") or ""), facts
    finding = evaluate({"plane.manual": Observation("plane.manual", facts)},
                       checks=("plane.manual",))[0]
    assert (finding.status, finding.reason) == (Status.FAIL, "corrupt")


def test_c2_a_lazily_opened_connection_is_not_evidence_that_a_file_opens(tmp_path,
                                                                          monkeypatch):
    """The pin of the OPENER's acceptance predicate, made load-bearing.

    Discriminating condition: an opener that hands back a LAZILY connected handle.
    ``sqlite3.connect`` never looks at the file until something queries, so a plane of pure
    garbage yields a connection quite happily.  Every reference opener today reads eagerly,
    so no natural fixture reaches this — the seam is monkeypatched precisely so the claim
    "opens means the file is a usable database" is proven rather than asserted.

    Delete the forcing read from ``_open_plane`` and this reddens; keep it and a lazy
    opener is caught."""
    import sqlite3

    target = os.path.join(str(tmp_path), "glass.data")
    with open(target, "wb") as handle:
        handle.write(b"this is emphatically not a database")

    def lazy_opener(plane, path):
        return sqlite3.connect(path)      # accepts anything; reads nothing

    # The control: the lazy connection really is handed back without complaint.
    assert lazy_opener("glass", target) is not None

    monkeypatch.setattr(_worker, "_open_plane", lazy_opener)
    captured = {}
    saved = _worker.emit
    try:
        _worker.emit = lambda check, **facts: captured.__setitem__(check, facts)
        _worker._probe_planes({"glass": target}, None)
    finally:
        _worker.emit = saved
    facts = captured["plane.glass"]
    assert facts["present"] is True
    assert facts["opens"] is False, (
        "a lazily opened handle was accepted as proof that the file opens: %r" % (facts,))


def _catalog_conn(rows):
    """Build a JSON-plane-shaped connection carrying exactly the given operand codes."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE operand (code TEXT PRIMARY KEY, description TEXT)")
    conn.executemany("INSERT INTO operand VALUES (?, ?)", [(code, "") for code in rows])
    conn.commit()
    return conn


def _token_in(conn, token):
    """The TEST's own token reader, used only to measure a fixture's precondition.

    Deliberately not doctor's: the token rule was deleted from production for being
    unsound, and a precondition asserted with the implementation's own helper would move
    with the implementation instead of constraining it.
    """
    return conn.execute(
        "SELECT 1 FROM operand WHERE code = ? LIMIT 1", (token,)).fetchone() is not None


def test_c5_the_identity_oracle_is_the_planes_own_file_not_its_wiring(tmp_path):
    """The token-plane half of C-5, and the reason the oracle is what it is.

    Discriminating condition: the TOLERANCE plane wired to the MERIT connection.  The merit
    catalog genuinely contains the tolerance plane's pinned token, so a fixed
    "sibling tokens must be absent" rule cannot separate the two — and it would also report
    every healthy merit plane as misrouted, because merit legitimately carries both.

    What separates them is comparing the wired connection against a connection opened
    INDEPENDENTLY from the plane's own file: tolerance's own file answers EFFL false, the
    merit connection answers it true, and the disagreement is the misroute.  Point the
    oracle at the wired connection instead and it agrees with itself, which is exactly the
    mutation this reddens on."""
    merit = _catalog_conn(["EFFL", "TRAD"])          # merit really does carry both
    tolerance = _catalog_conn(["TRAD"])
    try:
        # The fixture's own precondition, measured rather than assumed — and measured with
        # the TEST's own token reader, never the production one, so the precondition cannot
        # move with the implementation it is here to constrain.
        assert _token_in(merit, "TRAD") is True, (
            "the fixture must reproduce the overlap, or it proves nothing")
        assert _token_in(tolerance, "EFFL") is False

        misrouted = _worker._identity_ok(
            "tolerance_operand", merit, "unused", reference_conn=tolerance)
        assert misrouted is False, (
            "a tolerance plane wired to the merit connection was accepted")

        correct = _worker._identity_ok(
            "tolerance_operand", tolerance, "unused", reference_conn=tolerance)
        assert correct is True, "the negative control must not be misrouted"

        healthy_merit = _worker._identity_ok(
            "merit_operand", merit, "unused", reference_conn=merit)
        assert healthy_merit is True, (
            "a healthy merit plane carrying the tolerance token must not be misrouted")
    finally:
        merit.close()
        tolerance.close()


def _plane_finding_from_identity(plane, identity_ok):
    """Grade one plane whose presence, openability and wiring all hold, from identity alone.

    Present, opens and wired are all true by construction, so ``identity_ok`` is the ONLY
    input that can change the verdict — which is what makes these two guards about the
    identity rule rather than about the surrounding prober."""
    facts = {"present": True, "opens": True, "wired": True, "identity_ok": identity_ok,
             "path": "unused", "conn_attr": "unused", "wired_path": ""}
    return evaluate({"plane." + plane: Observation("plane." + plane, facts)},
                    checks=("plane." + plane,))[0]


def test_c6_a_merit_catalog_carrying_the_tolerance_token_is_healthy(tmp_path):
    """C-6: **the deleted rule must stay deleted.**

    Discriminating condition: the fixture's merit catalog CARRIES the cross-plane token
    ``TRAD``, exactly as the real one does (measured: one row).  A naive fixture whose
    catalogs share no token passes under both the deleted rule and the fingerprint, and so
    discriminates nothing.

    The deleted rule required this plane's token be present *and the other planes' tokens
    absent*.  Reinstate it and this healthy merit plane reports ``misrouted`` — cry-wolf on
    every provisioned install, which is why the rule was replaced rather than repaired."""
    wired = _catalog_conn(["EFFL", "TRAD"])
    from_file = _catalog_conn(["EFFL", "TRAD"])       # the same content, opened separately
    try:
        assert _token_in(wired, "TRAD") is True, (
            "the fixture must carry the cross-plane token, or it proves nothing")
        identity = _worker._identity_ok("merit_operand", wired, "unused",
                                        reference_conn=from_file)
        assert identity is True, (
            "a healthy merit plane that legitimately carries the tolerance token was "
            "reported misrouted — the deleted pinned-token rule is back")
        finding = _plane_finding_from_identity("merit_operand", identity)
        assert (finding.status, finding.reason) == (Status.PASS, "")
    finally:
        wired.close()
        from_file.close()


def test_c7_tolerance_wired_to_merit_is_the_case_a_token_test_cannot_see():
    """C-7: the misroute the deleted rule was structurally blind to.

    Discriminating condition: ``tolerance_conn`` bound to the MERIT connection, where the
    tolerance plane's own token is reachable through the wrong connection because ``TRAD``
    is in both catalogs.  Grade on token presence — "is this plane's own token reachable" —
    and the swap is invisible and the plane reports PASS.

    The fingerprint sees it immediately: different row counts over different code sets."""
    merit = _catalog_conn(["EFFL", "TRAD"])
    tolerance_file = _catalog_conn(["TRAD"])
    try:
        assert _token_in(merit, "TRAD") is True, (
            "the fixture's whole point is that the token IS reachable through the wrong "
            "connection; without that a token test would catch this and C-7 proves nothing")

        identity = _worker._identity_ok("tolerance_operand", merit, "unused",
                                        reference_conn=tolerance_file)
        assert identity is False, (
            "a tolerance plane wired to the merit connection was accepted")
        finding = _plane_finding_from_identity("tolerance_operand", identity)
        assert (finding.status, finding.reason) == (Status.FAIL, "misrouted")

        control = _worker._identity_ok("tolerance_operand", tolerance_file, "unused",
                                       reference_conn=_catalog_conn(["TRAD"]))
        assert control is True, (
            "the negative control must pass, or the guard would hold for an "
            "implementation that reported every plane misrouted")
    finally:
        merit.close()
        tolerance_file.close()


def _unindexed_catalog_conn(rows):
    """An operand table with NO index on the key column, so a plain SELECT returns rows in
    INSERTION order.

    Discriminating condition for the guard below.  With ``code TEXT PRIMARY KEY`` the query
    is covered by the unique index and SQLite hands back sorted rows whatever the insertion
    order, so an unsorted digest would agree by accident and the guard would prove nothing.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE operand (code TEXT, description TEXT)")
    conn.executemany("INSERT INTO operand VALUES (?, ?)", [(code, "") for code in rows])
    conn.commit()
    return conn


def test_c7_the_fingerprint_compares_content_not_row_order():
    """Two connections carrying the same rows in a different order must agree.

    The fingerprint sorts before hashing, so provenance does not depend on row order — an
    unsorted digest reports a healthy plane MISROUTED whenever the build happened to emit
    its rows in another sequence, which is a fabricated FAIL on a correct install.

    Discriminating condition: an UNINDEXED key column, so the two connections genuinely
    return their rows in different orders."""
    forward = _unindexed_catalog_conn(["AAAA", "BBBB", "CCCC"])
    backward = _unindexed_catalog_conn(["CCCC", "AAAA", "BBBB"])
    try:
        assert [row[0] for row in forward.execute("SELECT code FROM operand")] != \
            [row[0] for row in backward.execute("SELECT code FROM operand")], (
                "the fixture must return its rows in different orders, or an unsorted "
                "digest agrees by accident and this guard proves nothing")
        assert _worker._identity_ok("merit_operand", forward, "unused",
                                    reference_conn=backward) is True
    finally:
        forward.close()
        backward.close()


def test_c5_a_connection_missing_the_planes_table_is_misrouted_not_unreadable():
    """A wired connection that does not even carry this plane's table is a MISROUTE — a
    fact about the target — not an unreadable probe, which would be a fact about doctor and
    would make the run INCOMPLETE instead of BROKEN."""
    import sqlite3

    stranger = sqlite3.connect(":memory:")
    stranger.execute("CREATE TABLE glass (catalog TEXT, name TEXT)")
    reference = _catalog_conn(["EFFL"])
    try:
        assert _worker._identity_ok(
            "merit_operand", stranger, "unused", reference_conn=reference) is False
    finally:
        stranger.close()
        reference.close()


def test_c5_an_unreadable_oracle_is_never_guessed_at():
    """When the independent oracle cannot be read, the answer is ``None`` — doctor could
    not measure — never a cheerful True.  Guessing here would convert a probe failure into
    a health claim, which is the one thing UNKNOWN exists to prevent."""
    merit = _catalog_conn(["EFFL"])
    try:
        assert _worker._identity_ok(
            "merit_operand", merit, "unused", reference_conn=None) is None
    finally:
        merit.close()


# ===========================================================================
# D-10  the composed manifest is graded on IDENTITY, never on a count
# ===========================================================================
def test_d10_a_door_replaced_by_an_unrelated_tool_fails_with_the_count_preserved():
    """D-10.  Discriminating condition: the injected manifest has the SAME NUMBER of tools
    and one pinned door swapped out for something unrelated.

    Asserting the identity set inside the test does not redden a count-grading
    implementation; the FIXTURE has to carry the substitution."""
    healthy = ["get_system_info"] + sorted(TOOL_PLANE)
    swapped = (["get_system_info"] + sorted(set(TOOL_PLANE) - {"search_reference"})
               + ["a_tool_that_is_not_a_door"])
    assert len(healthy) == len(swapped), "the fixture must preserve the count"

    def graded(names):
        return evaluate({"composed.manifest": Observation(
            "composed.manifest", {"ok": True, "names": names})},
            checks=("composed.manifest",))[0]

    assert graded(healthy).status is Status.PASS
    broken = graded(swapped)
    assert (broken.status, broken.reason) == (Status.FAIL, "pinned_door_absent")
    assert "search_reference" in broken.summary


def test_d10_the_worker_honours_the_declared_manifest_seam():
    """The seam D-10 rides has to exist in the WORKER, not only in the parent, or the guard
    is testing a path production never takes."""
    captured = {}
    saved = _worker.emit
    try:
        _worker.emit = lambda check, **facts: captured.setdefault(check, facts)
        _worker._probe_harness(["only_this_tool"])
    finally:
        _worker.emit = saved
    assert captured["composed.manifest"]["names"] == ["only_this_tool"]
    assert captured["composed.manifest"]["injected"] is True


# ===========================================================================
# harness.manifest — the word "cold" is part of the verdict, not decoration.
#
# The finding's own sentence says the manifest "builds cold".  Grading only
# ``ok`` meant a run could print ``engine open=True`` on the very line it
# reported PASS: a false green that states its own counter-evidence.
# ===========================================================================
def _manifest_finding(**facts):
    return _runner._grade_harness_manifest("harness.manifest", dict(facts), {}, {}, True)


def test_the_cold_manifest_passes_only_when_it_really_was_cold():
    finding = _manifest_finding(ok=True, names=["get_system_info"], is_open=False,
                                clr_loaded=False)
    assert finding.status is Status.PASS


@pytest.mark.parametrize("is_open,clr_loaded", [(True, False), (False, True), (True, True)])
def test_a_manifest_that_reached_the_backend_is_not_a_pass(is_open, clr_loaded):
    """Building the tool manifest must not take the single seat.  Mutation: grade on ``ok``
    alone and a regression that opens an engine during construction reports PASS while
    printing the evidence against itself."""
    finding = _manifest_finding(ok=True, names=["get_system_info"], is_open=is_open,
                                clr_loaded=clr_loaded)
    assert (finding.status, finding.reason) == (Status.FAIL, "manifest_not_cold")


@pytest.mark.parametrize("facts", [
    {"ok": True, "names": []},                                   # neither fact recorded
    {"ok": True, "names": [], "is_open": False},                 # half of it recorded
    {"ok": True, "names": [], "is_open": None, "clr_loaded": False},
])
def test_a_manifest_whose_coldness_was_not_recorded_is_unknown(facts):
    """Missing evidence is never favourable evidence: an absent side-effect fact means
    doctor could not establish the claim it is about to print."""
    finding = _manifest_finding(**facts)
    assert (finding.status, finding.reason) == (Status.UNKNOWN, "cold_proof_unreadable")


def test_an_unbuildable_dispatcher_makes_a_plane_definitively_unwired_not_unreadable():
    """The SKIP/UNKNOWN split, applied to the one object every plane's wiring depends on.

    ``wired=None`` means "doctor could not read the property" — its own failure, UNKNOWN,
    exit 3.  But when the Dispatcher RAISED on construction there is no object that could
    be holding a connection, so ``not wired`` is a definitive fact about the TARGET.
    Collapsing the two reported a nameable defect as "doctor could not measure".

    Mutation that reddens: emit ``wired=None`` regardless of ``dispatcher_error``."""
    captured = {}
    saved = {"emit": _worker.emit, "isfile": os.path.isfile}
    try:
        _worker.emit = lambda check, **facts: captured.setdefault(check, facts)
        paths = {plane: "C:/nope/%s" % plane for plane in PLANE_NAMES}

        # Only the wiring half is under test, so presence/openability are supplied rather
        # than staged on disk; the plane opener has its own guards elsewhere.
        _worker._probe_planes(paths, None, None)
        for plane in PLANE_NAMES:
            assert captured["plane." + plane]["wired"] is None, (
                "with no dispatcher AND no reason, wiring is genuinely unreadable")

        captured.clear()
        _worker._probe_planes(paths, None, "RuntimeError: no such table")
        for plane in PLANE_NAMES:
            facts = captured["plane." + plane]
            # The file is absent in this fixture, so wiring is never reached; what must
            # hold is that the two cases are DISTINGUISHED at all, which the branch below
            # proves on a present plane.
            assert "path" in facts
    finally:
        _worker.emit = saved["emit"]

    # The present-and-opening case, where the branch actually fires.
    captured = {}
    saved_emit, saved_open, saved_readable = (
        _worker.emit, _worker._open_plane, _worker._readable)
    saved_isfile, saved_getsize = os.path.isfile, os.path.getsize
    try:
        _worker.emit = lambda check, **facts: captured.setdefault(check, facts)
        _worker._open_plane = lambda plane, path: object()
        _worker._readable = lambda conn: True
        os.path.isfile = lambda path: True
        os.path.getsize = lambda path: 10
        _worker._probe_planes({plane: "C:/d/%s" % plane for plane in PLANE_NAMES},
                              None, "RuntimeError: no such table: operand")
    finally:
        _worker.emit, _worker._open_plane, _worker._readable = (
            saved_emit, saved_open, saved_readable)
        os.path.isfile, os.path.getsize = saved_isfile, saved_getsize

    for plane in PLANE_NAMES:
        facts = captured["plane." + plane]
        assert facts["wired"] is False, (
            "a Dispatcher that could not be constructed is not 'unreadable wiring'")
        assert facts["dispatcher_error"] == "RuntimeError: no such table: operand", (
            "the cause must travel with the plane, not only with its own check")


def test_the_worker_hands_back_a_seat_the_manifest_should_never_have_taken():
    """Detecting a leak and then contributing one is not a diagnostic.

    Behavioural, not a token scan: an AST test asserting that ``_probe_harness`` merely
    MENTIONS ``close`` stays green when the branch that reaches it is disabled, which is
    the hollow-guard class.  This drives the real function against a session that reports
    itself open and requires the close to actually happen.

    Mutation that reddens: disable the ``if opened:`` branch."""
    closed = []

    class _Session:
        is_open = True

        def close(self):
            closed.append(True)

    class _Dispatcher:
        def __init__(self, session):
            self.session = session

        def list_tools(self):
            return [{"name": "get_system_info"}]

    import optivibe_harness.server as harness_server
    import optivibe_harness.session as harness_session

    saved = (harness_session.ZemaxSession, harness_server.Dispatcher, _worker.emit)
    captured = {}
    try:
        harness_session.ZemaxSession = _Session
        harness_server.Dispatcher = _Dispatcher
        _worker.emit = lambda check, **facts: captured.setdefault(check, facts)
        _worker._probe_harness(["only_this_tool"])
    finally:
        (harness_session.ZemaxSession, harness_server.Dispatcher,
         _worker.emit) = saved

    assert captured["harness.manifest"]["is_open"] is True, (
        "the fixture did not reach the branch under test")
    assert closed, (
        "the manifest build opened a session and the worker did not hand the seat back")


# ===========================================================================
# The dependency probe: which names are probed, what they are called, and
# whether "not declared at all" is a state a real run can ever reach.
# ===========================================================================
def _capture_pkg_observations(universe):
    """Drive the REAL ``worker_pkg`` dependency loop over a declared universe.

    The declared universe is injected through the worker's own published environment seam
    (``DEP_UNIVERSE_ENV``), so the code under test is production's, unmodified.  Everything
    downstream of the dependency loop is stubbed out — those are OTHER probes with their
    own guards, and running them here would only add seconds and unrelated failure modes.
    """
    captured = {}
    saved = {name: getattr(_worker, name) for name in
             ("emit", "_probe_planes", "_probe_doors", "_probe_harness",
              "_reference_dispatcher", "_plane_paths", "_enrichment_tier")}
    previous = os.environ.get(_worker.DEP_UNIVERSE_ENV)
    try:
        _worker.emit = lambda check, **facts: captured.setdefault(check, facts)
        _worker._probe_planes = lambda *args, **kwargs: None
        _worker._probe_doors = lambda *args, **kwargs: None
        _worker._probe_harness = lambda *args, **kwargs: None
        _worker._reference_dispatcher = lambda: object()
        _worker._plane_paths = lambda planes: {}
        _worker._enrichment_tier = lambda plane, paths: None
        os.environ[_worker.DEP_UNIVERSE_ENV] = json.dumps(list(universe))
        _worker.worker_pkg()
    finally:
        for name, value in saved.items():
            setattr(_worker, name, value)
        if previous is None:
            os.environ.pop(_worker.DEP_UNIVERSE_ENV, None)
        else:
            os.environ[_worker.DEP_UNIVERSE_ENV] = previous
    return captured


def test_a_critical_dependency_is_probed_even_when_the_metadata_does_not_declare_it():
    """``classify_dep(declared=False, ...)`` is a contracted row — a critical dependency
    absent from BOTH the metadata and the machine is BROKEN/2.  Probing only declared names
    made it unreachable from a real run: the individual check went UNKNOWN, coverage went
    FAIL, and UNKNOWN wins, so the run exited 3 ("doctor could not measure") for a defect
    doctor had measured exactly.

    Mutation: probe ``declared`` alone and ``dependency.pythonnet`` disappears from the
    emitted set entirely."""
    emitted = _capture_pkg_observations(universe=["mcp", "psutil"])
    for name in CRITICAL_DEPS:
        assert "dependency." + name in emitted, (
            "%s is pinned critical but was never probed" % name)
    assert emitted["dependency.pythonnet"]["declared"] is False, (
        "a name absent from Requires-Dist must be reported as undeclared, not asserted "
        "declared by the prober")
    assert emitted["dependency.mcp"]["declared"] is True


def test_an_optional_extra_is_not_a_mandatory_dependency(monkeypatch):
    """A PEP 508 marker is EVALUATED, never discarded.

    ``pymupdf; extra == "manual"`` and ``pytest; extra == "dev"`` are optional by
    construction, and ``classify_enrichment`` already documents an install without
    ``[manual]`` as a supported configuration.  Grading them as runtime requirements
    reports ``WARN declared but is not installed`` twice on every clean end-user install:
    DEGRADED, exit 1, permanently.  Telling a user their install is degraded because
    *pytest* is absent trains them to ignore the tool, which is total loss of function —
    the same cry-wolf failure the contract forbids one layer down for coverage.

    Asserted through the real reader against synthetic metadata, so it does not depend on
    what happens to be installed on the machine running the suite."""
    entries = ['mcp>=1.28,<2', 'psutil', 'pymupdf; extra == "manual"',
               'pytest; extra == "dev"', 'nowhere; sys_platform == "no-such-platform"']

    class _Dist:
        requires = entries

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: _Dist())
    names, _per_dist, inactive, _how = _worker._requires_dist_names()
    assert "mcp" in names and "psutil" in names
    assert "pymupdf" not in names, "an extras-only requirement entered the graded universe"
    assert "pytest" not in names
    assert set(inactive) >= {"pymupdf", "pytest"}, (
        "the excluded names must be DISCLOSED, not silently dropped")


def test_an_unconditional_requirement_is_never_excluded_by_the_marker_rule(monkeypatch):
    """The other direction, and the dangerous one: over-eager exclusion silently stops
    grading a real dependency, and a check that stopped running reports green.

    Two of the entries carry markers that EVALUATE TRUE.  Without them nothing here would
    reach the evaluation branch at all -- every unmarked entry short-circuits before it --
    so a rule that answered "inactive" to every marker it looked at would still pass."""
    class _Dist:
        requires = ['mcp>=1.28,<2', 'psutil', 'pythonnet',
                    'matplotlib; python_version >= "3.0"',
                    'numpy; os_name == "%s"' % os.name]

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: _Dist())
    names, _per_dist, inactive, how = _worker._requires_dist_names()
    assert set(names) == {"mcp", "psutil", "pythonnet", "matplotlib", "numpy"}
    assert inactive == []
    assert "packaging" in how or "fallback" in how, (
        "no entry reached the marker evaluation at all; this guard is vacuous")


def test_the_marker_rule_still_excludes_extras_without_packaging(monkeypatch):
    """``packaging`` is not declared by either wheel, so it may simply be absent on a
    stranger's machine.  The fallback is narrower — extras only — and it must still close
    the defect rather than silently reverting to grading them mandatory."""
    real_import = builtins.__import__

    def refuse_packaging(name, *args, **kwargs):
        if name.startswith("packaging"):
            raise ImportError("no packaging here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_packaging)
    active, mechanism = _worker._marker_is_active('pymupdf; extra == "manual"')
    assert (active, mechanism) == (False, "fallback_extra")
    active, mechanism = _worker._marker_is_active('mcp>=1.28,<2')
    assert active is True


def test_the_check_id_is_the_normalised_name_and_the_raw_spelling_is_evidence():
    """PEP 503 is applied to BOTH sides of every dependency comparison, and the check
    IDENTITY is one of those sides.  A wheel declaring ``PSUtil`` would otherwise emit
    ``dependency.PSUtil`` while the spine expects ``dependency.psutil``: the spine id is
    missing, the run is INCOMPLETE/3, and coverage — which DOES normalise — passes at the
    same moment.  Two layers disagreeing about one name."""
    emitted = _capture_pkg_observations(universe=["PSUtil", "MCP", "pythonnet"])
    assert "dependency.psutil" in emitted, sorted(emitted)
    assert "dependency.PSUtil" not in emitted
    assert emitted["dependency.psutil"]["declared_as"] == "PSUtil", (
        "the distribution's own spelling must survive as evidence")
    assert emitted["dependency.psutil"]["declared"] is True


# ===========================================================================
# D-7 / D-21  the repository pin, and its separation from the runtime check
# ===========================================================================
def test_d7_every_critical_dependency_is_declared_by_the_harness_manifest():
    """D-7 reads the REPOSITORY's manifest.  ``dependency.coverage`` asserts the same
    relation against THIS MACHINE's installed metadata, and the two may not share an
    implementation: the case where they disagree — an install that differs from the repo —
    is precisely the case doctor exists for."""
    import re
    import tomllib

    root = _checkout_root()
    if root is None:
        pytest.skip("no checkout is locatable from here")
    manifest = os.path.join(root, "packages", "optivibe-harness", "pyproject.toml")
    with open(manifest, "rb") as handle:
        declared = tomllib.load(handle)["project"].get("dependencies", [])
    names = set()
    for entry in declared:
        match = re.match(r"^\s*([A-Za-z0-9._-]+)", str(entry).split(";")[0])
        if match:
            names.add(match.group(1).lower().replace("_", "-").replace(".", "-"))
    assert names, "the harness manifest declares no dependencies at all"
    missing = [name for name in CRITICAL_DEPS if name not in names]
    assert missing == [], (
        "%r is pinned critical but the harness manifest does not declare it" % (missing,))


def test_d21_the_repository_assertion_never_routes_through_the_runtime_classifier():
    """D-21.  Two independent facts must stay independent: implementing the repository
    assertion by calling the runtime classifier collapses them into one that cannot
    disagree, and the disagreement is the entire diagnostic value."""
    classify_source = _doctor_source("_classify")
    assert "pyproject" not in classify_source
    assert "tomllib" not in classify_source
    with open(os.path.abspath(__file__), encoding="utf-8") as handle:
        tests = handle.read()
    marker = "def test_d7_every_critical_dependency_is_declared_by_the_harness_manifest"
    body = tests.split(marker, 1)[1].split("\ndef ", 1)[0]
    assert "classify_coverage" not in body
    assert "_dependency_coverage" not in body


# ===========================================================================
# D-19 / D-20  ONE coverage rule, and one name spelling
# ===========================================================================
_COVERAGE_CALL_SITES = ("_dependency_coverage", "_plane_coverage", "_tool_coverage")
_PIN_NAMES = ("CRITICAL_DEPS", "REFERENCE_CASES", "PLANES")


def _functions_of(tree):
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _set_differences(node):
    """Every set-difference expression in ``node``, as the set of Names it mentions.

    Both spellings count: the ``-`` operator and an explicit ``.difference(...)`` call.
    Yields one name-set per difference so a caller can ask what each one is computed
    against."""
    found = []
    for item in ast.walk(node):
        operands = None
        if isinstance(item, ast.BinOp) and isinstance(item.op, ast.Sub):
            operands = [item.left, item.right]
        elif (isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
              and item.func.attr in ("difference", "symmetric_difference")):
            operands = [item.func.value] + list(item.args)
        if operands is None:
            continue
        names = set()
        for operand in operands:
            names.update(sub.id for sub in ast.walk(operand) if isinstance(sub, ast.Name))
        found.append(names)
    return found


def test_d19_no_coverage_call_site_open_codes_the_pin_versus_universe_rule():
    """D-19, mechanism AST.  Inline a second copy of the rule for one kind and this
    reddens.

    The three call sites derive their own ``universe`` — ``set(composed) - set(harness)``
    is mandated, and is a different relation — but none of them may compute the pin
    against it.  That relation is the RULE, and three copies of one rule are three rules
    that can drift."""
    functions = _functions_of(_doctor_tree("_runner"))
    for name in _COVERAGE_CALL_SITES:
        assert name in functions, name
        offenders = [names for names in _set_differences(functions[name])
                     if "pinned" in names]
        assert offenders == [], (
            "%s computes the pin against the universe itself instead of delegating to "
            "classify_coverage: %r" % (name, offenders))


#: The ONE function outside the coverage rule allowed to difference a pinned identity set,
#: and why.  ``composed.manifest`` asserts ``REFERENCE_CASES`` against the COMPOSED MCP
#: manifest; ``tool.coverage`` asserts it against the derived reference-door universe
#: (``set(composed) - set(harness)``).  Same relation, different artifacts — exactly the
#: D-7 / dependency.coverage split, where the spec requires two implementations precisely
#: so the two facts can disagree.  A THIRD copy is what this allow-list catches.
_PIN_DIFFERENCE_ALLOWED = {"_runner._grade_composed_manifest"}


def test_d19_the_pinned_identity_sets_are_never_differenced_outside_the_classifier():
    """No new copy of the pin-versus-universe rule may appear anywhere in the package.

    The pin reaches the coverage rule as the ``pinned`` ARGUMENT and nowhere else, so a
    second implementation of THAT rule cannot quietly grow beside the first.  The single
    documented exception is D-10's independent check on a different artifact; anything
    else differencing a pinned identity set directly is a drifting duplicate."""
    offenders = []
    for module in ("_runner", "_worker", "_classify", "_render", "_contract", "__main__"):
        for function_name, node in _functions_of(_doctor_tree(module)).items():
            for names in _set_differences(node):
                if names & set(_PIN_NAMES):
                    offenders.append("%s.%s" % (module, function_name))
    assert sorted(set(offenders)) == sorted(_PIN_DIFFERENCE_ALLOWED), (
        "a pinned identity set is differenced outside the coverage rule and outside the "
        "one documented independent check: %r" % (sorted(set(offenders)),))


def test_d19_no_coverage_function_anywhere_differences_a_pin_directly():
    """The coverage rule specifically: every ``*coverage*`` function must receive the pin
    as an argument, never reach for the contract constant itself."""
    for module in ("_runner", "_classify"):
        for function_name, node in _functions_of(_doctor_tree(module)).items():
            if "coverage" not in function_name:
                continue
            for names in _set_differences(node):
                clash = names & set(_PIN_NAMES)
                assert not clash, (
                    "%s.%s reaches for %r instead of taking the pin as an argument"
                    % (module, function_name, sorted(clash)))


def test_d19_no_coverage_function_computes_a_set_difference_at_all():
    """D-19 part (ii), in its own words: **no function whose name contains ``coverage``
    computes a set difference itself — it must delegate.**

    Stricter than the pin-only rule above, and deliberately so.  The pin-versus-universe
    relation is the RULE and lives in exactly one place; the composed-minus-harness
    universe DERIVATION is a *different* relation and belongs in its own named helper.
    Open-coding either one inside a coverage site is how a second copy grows beside the
    first and starts drifting: a caller must consume the one writer's own predicate, never a
    second copy of it — the defect this whole rule exists to close.

    Mutation: inline ``sorted(set(composed["names"]) - set(harness["names"]))`` back into
    ``_tool_coverage`` and this reddens."""
    offenders = []
    for module in ("_runner", "_classify", "_worker", "_render", "_contract", "__main__"):
        for function_name, node in _functions_of(_doctor_tree(module)).items():
            if "coverage" not in function_name:
                continue
            if _set_differences(node):
                offenders.append("%s.%s" % (module, function_name))
    assert offenders == [], (
        "a *coverage* function computes its own set difference instead of delegating: %r"
        % (sorted(offenders),))


def test_d19_the_coverage_sites_still_reach_a_derived_universe():
    """The other half of part (ii).  A rule that only FORBIDS is satisfied by deleting the
    work: if no coverage site derived a universe at all, the guard above would pass over a
    doctor that grades nothing.  The derivation must still exist — somewhere else."""
    functions = _functions_of(_doctor_tree("_runner"))
    assert "_reference_door_universe" in functions, (
        "the composed-minus-harness derivation must live in its own named helper")
    assert _set_differences(functions["_reference_door_universe"]), (
        "that helper is where the derivation actually happens")
    called = [name for name, node in functions.items()
              if any(isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
                     and item.func.id == "_reference_door_universe"
                     for item in ast.walk(node))]
    assert called == ["_tool_coverage"], (
        "the derived door universe must reach exactly the tool coverage site, not %r"
        % (called,))


def test_d19_classify_coverage_is_reached_through_exactly_one_call_site():
    """The single-implementation claim, asserted structurally rather than by inspection."""
    tree = _doctor_tree("_runner")
    callers = [name for name, node in _functions_of(tree).items()
               if any(isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
                      and item.func.id == "classify_coverage" for item in ast.walk(node))]
    assert callers == ["_coverage_finding"], (
        "classify_coverage must be reached from exactly one place, not %r" % (callers,))
    delegating = [name for name in _COVERAGE_CALL_SITES
                  if any(isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
                         and item.func.id == "_coverage_finding"
                         for item in ast.walk(_functions_of(tree)[name]))]
    assert sorted(delegating) == sorted(_COVERAGE_CALL_SITES), (
        "every coverage check must go through the shared classifier: %r" % (delegating,))


def test_the_plane_universe_discovers_a_plane_wired_as_a_plain_instance_attribute():
    """The plane universe is scanned on the INSTANCE, so a plain attribute is discovered.

    The defect: ``dir(type(dispatcher))`` lists class-level names only, so a plane wired as
    ``self.new_conn = sqlite3.connect(...)`` in ``__init__`` — no ``@property``, no
    ``__slots__`` — was invisible.  An under-counted universe makes ``universe - pinned``
    empty, so the "something new appeared ungraded" WARN becomes unreachable and doctor
    reports PASS: the coverage check has stopped covering, which is the exact shape the whole
    coverage layer exists to catch, one level up.

    Discriminating condition: the double wires one plane as a property and one as a plain
    instance attribute.  An all-property double (which is what the real Dispatcher happens to
    be today) passes under both implementations and proves nothing.

    Mutation: revert to ``dir(type(dispatcher))`` → ``plain_conn`` vanishes and this reddens.
    """
    class _Mixed(object):
        def __init__(self):
            self.plain_conn = object()           # a plane wired as a plain attribute
            self._private_conn = object()         # never public API, must stay out
            self.unrelated = object()

        @property
        def conn(self):
            return None

    universe = _worker._conn_attributes(_Mixed())
    assert universe == ["conn", "plain_conn"], (
        "the derived plane universe missed a plane wired as a plain instance attribute: %r"
        % (universe,))

    # The under-count's consequence, asserted through the real grader rather than argued:
    # a plane present but unpinned MUST reach the reader as an ungraded-member WARN.
    pinned = sorted({spec[2] for spec in _contract.PLANES.values()})
    status, _reason = classify_coverage(EXACT_KIND, pinned,
                                       sorted(pinned + ["plain_conn"]),
                                       sorted(pinned + ["plain_conn"]), None)
    assert status is Status.WARN, (
        "an undiscovered plane is not merely unreported — discovering it is what raises the "
        "ungraded-member WARN, and a short universe silently returns %s" % (status,))

    # And the real Dispatcher's universe is UNCHANGED by the instance scan: every property
    # the class scan found is still found, and no private attribute leaked in.
    class _AsShipped(object):
        def __init__(self):
            self._conn = self._glass_conn = None

        @property
        def conn(self):
            return self._conn

        @property
        def glass_conn(self):
            return self._glass_conn

    assert _worker._conn_attributes(_AsShipped()) == ["conn", "glass_conn"]


def test_an_unscannable_dispatcher_leaves_the_universe_unknown_not_empty():
    """A universe doctor could not read is ``None``, never ``[]``.

    ``[]`` is not the neutral answer it looks like: ``pinned - []`` is every pinned plane, so
    an empty universe reads as *all four data planes have vanished from the dispatcher* — a
    definitive FAIL against the target — where the truth is that doctor's own scan failed
    (UNKNOWN).  This is the fabricated-defect direction of the same asymmetry, and the
    instance scan is what makes it reachable at all: ``dir()`` on an instance runs
    ``__dir__``, which is target code and can raise.

    Mutation: return ``[]`` (or let the exception escape) on an unscannable object → the
    ``None`` assertion, or the UNKNOWN grading below, reddens."""
    class _Hostile(object):
        def __dir__(self):
            raise RuntimeError("__dir__ raised")

    assert _worker._conn_attributes(_Hostile()) is None, (
        "an unscannable dispatcher produced a universe instead of admitting it could not "
        "be read")

    pinned = sorted({spec[2] for spec in _contract.PLANES.values()})
    assert classify_coverage(EXACT_KIND, pinned, None, pinned, None) == (
        Status.UNKNOWN, "universe_unreadable")
    assert classify_coverage(EXACT_KIND, pinned, [], [], None)[0] is Status.FAIL, (
        "the fixture must confirm the two answers differ, or returning [] would be "
        "indistinguishable from returning None")


def test_d20_a_non_canonically_spelled_dependency_still_satisfies_the_pin():
    """D-20, mechanism SEAM.

    Discriminating condition: the fixture spells one dependency NON-canonically
    (``PSUtil``).  Against an all-canonical fixture a raw-string comparison passes and the
    guard proves nothing.

    ``PSUtil`` in a distribution's ``Requires-Dist`` and ``psutil`` in the pin are the same
    dependency; reporting the pin as missing would be a fabricated FAIL on a healthy
    install."""
    raw = ["PSUtil", "MCP", "PythonNet"]
    observations = {"dependency.universe": Observation("dependency.universe",
                                                       {"names": raw})}
    for name in raw:
        observations["dependency." + name] = Observation("dependency." + name, {})
    assert any(name != name.lower() for name in raw), (
        "the fixture must spell a name non-canonically, or it discriminates nothing")

    finding = _runner._dependency_coverage(observations, {}, True)
    assert (finding.status, finding.reason) == (Status.PASS, ""), (
        "a differently-cased spelling of a pinned dependency read as missing: %s"
        % finding.summary)
    assert _contract.normalise_dist_name("PSUtil") == "psutil"
    assert _contract.normalise_dist_name("zope.interface") == "zope-interface"
    assert _contract.normalise_dist_name("a__b") == "a-b"


def test_dependency_coverage_is_not_incomplete_on_a_healthy_derived_universe():
    """The derived dependency names are emitted as EXTRAS, which ``evaluate`` appends after
    the spine loop.  Grade ``dependency.coverage`` from the findings produced so far and
    every one of them is still missing, so a perfectly healthy install reports
    ``probe_incomplete``/UNKNOWN and doctor exits 3 forever.

    A permanent exit 3 is not a conservative failure: it is a report that has stopped
    saying anything, and it teaches the reader to ignore the tool."""
    raw = ["mcp", "psutil", "pythonnet", "matplotlib", "numpy"]
    observations = {"dependency.universe": Observation("dependency.universe",
                                                       {"names": raw})}
    for name in raw:
        observations["dependency." + name] = Observation("dependency." + name, {})
    spine_only = {"dependency.mcp": None, "dependency.psutil": None,
                  "dependency.pythonnet": None}
    finding = _runner._dependency_coverage(observations, spine_only, True)
    assert finding.status is Status.PASS, (
        "the derived extras are probed but not yet graded: %s" % finding.summary)


def test_dependency_coverage_still_reports_a_worker_that_died_mid_loop():
    """The negative control for the guard above: a dependency the worker never reached is
    genuinely unprobed, and that must still read UNKNOWN rather than being absorbed."""
    observations = {"dependency.universe": Observation(
        "dependency.universe", {"names": ["mcp", "psutil", "pythonnet", "numpy"]})}
    for name in ("mcp", "psutil", "pythonnet"):
        observations["dependency." + name] = Observation("dependency." + name, {})
    finding = _runner._dependency_coverage(observations, {}, True)
    assert (finding.status, finding.reason) == (Status.UNKNOWN, "probe_incomplete")
    assert "numpy" in finding.summary


# ---------------------------------------------------------------------------
# The SAME ordering trap, one check over: ``tool.coverage``.
#
# ``UNKNOWN is always a statement about doctor, never about the target``, so a definitive
# target defect may never surface as one.  The derived reference-door universe is read from
# ``harness.manifest`` and ``composed.manifest``; both are graded AFTER ``tool.coverage`` in
# the spine, and both are unavailable once ``harness.import`` fails.  A broken ``psutil``
# therefore left the universe unreadable, and the check answered UNKNOWN
# ``universe_unreadable`` — doctor's own failure — for an install doctor had itself already
# failed twice by name, turning a contracted BROKEN/2 into INCOMPLETE/3.
# ---------------------------------------------------------------------------
def _broken_harness_emissions(**replace):
    """Healthy emissions with the harness half made definitively unhealthy.

    ``replace`` maps a check id to its facts.  Any id it names is dropped and re-emitted, and
    any harness id it does NOT name is dropped entirely — which is what the worker really
    does: once the harness import fails it returns, so the manifests are never emitted at all
    and become SKIPs, and a SKIP is not a blocker.
    """
    emissions = _healthy_emissions()
    owned = ("harness.import", "harness.manifest", "composed.manifest", "mcp.construct")
    kept = [item for item in emissions["pkg"] if item.check not in owned]
    for check, facts in sorted(replace.items()):
        kept.append(Observation(check, facts))
    emissions["pkg"] = kept
    # The zemax worker reports the SAME import, and its two ids SKIP naming it.  A SKIP that
    # names a PASSing check is itself a defect (the anti-skip-creep invariant), so the
    # fixture must follow the harness import rather than hard-code it broken — otherwise
    # these rows would read INCOMPLETE for a reason that has nothing to do with coverage.
    imported = bool(replace.get("harness.import", {}).get("ok"))
    if imported:
        emissions["zemax"] = _healthy_emissions()["zemax"]
    else:
        emissions["zemax"] = [Observation("zemax.nethelper", {"harness_import": False}),
                              Observation("zemax.dir", {"harness_import": False})]
    return emissions


#: ``(label, emissions)`` — every way the derived door universe becomes unreadable because
#: of a defect in the TARGET.  Each row is definitive and already reported by another
#: finding, so each must reach BROKEN/2 with ``tool.coverage`` SKIPped, never UNKNOWN.
_DOOR_UNIVERSE_DEFECTS = (
    # The dogfooded row.  A broken psutil fails the harness import; both manifests are then
    # never emitted.  The blocker must name the ROOT, not the downstream SKIP.
    ("harness.import failed", _broken_harness_emissions(**{
        "harness.import": {"ok": False, "error_type": "ModuleNotFoundError",
                           "error": "No module named 'psutil'"}}), "harness.import"),
    # The import succeeded and building the Dispatcher (or listing its tools) raised.  The
    # manifest is a DIRECT input and fails on its own, with everything upstream green.
    ("harness.manifest failed", _broken_harness_emissions(**{
        "harness.import": {"ok": True},
        "harness.manifest": {"ok": False, "error_type": "RuntimeError", "error": "boom",
                             "is_open": None},
        "composed.manifest": {"ok": True, "names": ["get_system_info"]},
        "mcp.construct": {"ok": True}}), "harness.manifest"),
    # The composition itself raised: the second direct input, also on its own.
    ("composed.manifest failed", _broken_harness_emissions(**{
        "harness.import": {"ok": True},
        "harness.manifest": {"ok": True, "names": ["get_system_info"], "is_open": False,
                             "clr_loaded": False},
        "composed.manifest": {"ok": False, "error_type": "RuntimeError", "error": "boom"},
        "mcp.construct": {"ok": True}}), "composed.manifest"),
)


@pytest.mark.parametrize("label,emissions,blocker",
                         _DOOR_UNIVERSE_DEFECTS,
                         ids=[row[0] for row in _DOOR_UNIVERSE_DEFECTS])
def test_tool_coverage_skips_on_a_definitive_defect_instead_of_exiting_3(
        label, emissions, blocker):
    """A target whose door universe cannot be derived is BROKEN, not INCOMPLETE.

    The exit code is the whole product for a caller in CI: 2 says *this install is broken,
    act on the findings*, 3 says *doctor could not tell you* — advice to re-run, on a machine
    where re-running will report the same two FAILs forever.

    Mutation: shorten ``_DOOR_UNIVERSE_PREREQUISITES`` back to the two reference-side ids, or
    resolve the blocker with ``_first_blocker`` (which cannot see an id the spine has not
    graded yet), and every row reddens on UNKNOWN/INCOMPLETE/3."""
    summary, _recorder, sink = _drive(emissions=emissions)
    findings = _sink_findings(sink)
    coverage = findings["tool.coverage"]

    assert coverage["status"] == Status.SKIP.value, (
        "%s: tool.coverage answered %s/%s — a statement about doctor for a defect doctor "
        "itself reported" % (label, coverage["status"], coverage["reason"]))
    assert coverage["blocked_by"] == blocker, (
        "%s: the SKIP names %r instead of the root cause %r"
        % (label, coverage["blocked_by"], blocker))
    assert findings[blocker]["status"] == Status.FAIL.value, (
        "%s: the fixture must make %s definitively FAIL, or the SKIP would be charged to a "
        "check that is fine" % (label, blocker))
    assert (summary.state, summary.exit_code) == (State.BROKEN, 2), (
        "%s: a definitively broken install reported %s/%s"
        % (label, summary.state, summary.exit_code))


def test_tool_coverage_still_says_unknown_when_doctor_itself_could_not_measure():
    """The negative control, and the half that keeps the fix honest.

    A guard that turns "universe unreadable" into a SKIP unconditionally would bury doctor's
    own failures: a worker that died before emitting the manifests delivered no observation
    at all, nothing is definitively FAILed, and the only truthful answer is still UNKNOWN and
    exit 3.  The blocker is resolved from FAIL/WARN findings only, so absence stays absent.

    Mutation: treat a missing observation, or a non-FAIL/WARN prerequisite, as a blocker and
    this reddens on the UNKNOWN assertion."""
    emissions = _broken_harness_emissions()          # no harness observations whatsoever
    summary, _recorder, sink = _drive(emissions=emissions)
    coverage = _sink_findings(sink)["tool.coverage"]

    assert (coverage["status"], coverage["reason"]) == (
        Status.UNKNOWN.value, "universe_unreadable"), (
        "a universe doctor never measured was charged to the target: %s/%s"
        % (coverage["status"], coverage["reason"]))
    assert coverage["blocked_by"] == ""
    assert (summary.state, summary.exit_code) == (State.INCOMPLETE, 3)


def test_the_healthy_run_is_untouched_by_the_door_universe_prerequisites():
    """The control that stops the guard from being satisfied by blocking everything: on a
    healthy install ``tool.coverage`` must still be graded, and still PASS."""
    summary, _recorder, sink = _drive(emissions=_healthy_emissions())
    coverage = _sink_findings(sink)["tool.coverage"]
    assert (coverage["status"], coverage["blocked_by"]) == (Status.PASS.value, "")
    assert (summary.state, summary.exit_code) == (State.READY, 0)


def test_every_door_universe_prerequisite_is_really_one_of_its_inputs():
    """The list may not grow into a convenient place to put anything inconvenient.

    Every id in it must be an input the derivation actually depends on — the two manifests it
    reads, or an id whose failure makes one of them unavailable — and the order must be
    most-upstream-first, because ``_first_blocker`` returns the FIRST match and naming a
    downstream symptom sends the reader to the wrong place.  ``harness.import`` in particular
    must come before ``harness.manifest``: when the import fails the manifest is a SKIP, and
    charging the SKIP to the manifest would name a check that never ran."""
    prerequisites = _runner._DOOR_UNIVERSE_PREREQUISITES
    assert prerequisites == ("harness.import", "harness.manifest",
                            "reference.import", "reference.dispatcher",
                            "composed.manifest")
    for check in prerequisites:
        assert check in BASE_CHECKS, check
    reads = _doctor_source("_runner").split("def _reference_door_universe", 1)[1]
    reads = reads.split("\ndef ", 1)[0]
    for direct in ("harness.manifest", "composed.manifest"):
        assert direct in reads, (
            "%s is claimed as a direct input but the derivation does not read it" % direct)
        assert direct in prerequisites
    assert prerequisites.index("harness.import") < prerequisites.index("harness.manifest")
    assert prerequisites.index("reference.import") < prerequisites.index(
        "reference.dispatcher")


# ===========================================================================
# D-22 / D-23  the pinned cases, and a probe input that cannot be derived
# ===========================================================================
_AMBIGUOUS_NAME = "TESTGLASS"


def _glass_fixture(tmp_path, catalogs=("SCHOTT.AGF", "OHARA.AGF", "HOYA.AGF")):
    """A glass catalog carrying ONE name in several catalogs — D-22's discriminating
    condition, and exactly the shape of the real one (measured: 3 rows for the name the
    draft pinned)."""
    path = os.path.join(str(tmp_path), "glass_catalog.json")
    rows = [{"catalog": catalog, "name": _AMBIGUOUS_NAME} for catalog in catalogs]
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"rows": rows}))
    return path


def _operand_fixture(tmp_path, tier):
    """A merit catalog in one of the two supported enrichment tiers.

    The FTS body is ``code + synonyms`` — descriptions are deliberately excluded from it —
    so the pinned query must match through ``synonyms`` in BOTH tiers."""
    row = {"code": "REAY", "description_source": "authored",
           "description": None, "description_pending": 1, "citation_handle": None,
           "synonyms": "real chief ray height y image"}
    if tier == "enriched":
        row.update(description="Real ray Y coordinate: the chief ray height.",
                   description_source="manual_verbatim", description_pending=0,
                   citation_handle="manual:p1")
    path = os.path.join(str(tmp_path), "operand_catalog_%s.json" % tier)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"rows": [row],
                                 "build_report": {"descriptions_state": tier}}))
    return path


def test_d22_the_derived_glass_case_answers_where_a_pinned_name_is_ambiguous(tmp_path):
    """D-22, mechanism SEAM.

    Discriminating condition: the fixture's glass table carries the SAME NAME in more than
    one catalog, as the real one does.  Under a single-catalog fixture the broken pin
    passes and this proves nothing.

    Measured live on a fully-provisioned machine: a bare pinned glass name returns
    ``ambiguous_glass``, graded ``refused_while_plane_ok`` -> FAIL, which made the
    partly-provisioned install's
    "no finding is FAIL" unreachable.  Deriving ``(name, catalog)`` from the catalog's own
    first row answers, and cannot go stale."""
    from optivibe_reference import glass_build
    from optivibe_reference.tools.lookup_glass import lookup_glass

    path = _glass_fixture(tmp_path)
    conn = glass_build.open_glass_catalog(json_path=path)
    try:
        # The fixture's own precondition: the bare name really is ambiguous here.
        bare = lookup_glass(conn, {"name": _AMBIGUOUS_NAME})
        assert bare["ok"] is False and bare["error_family"] == "ambiguous_glass", (
            "the fixture must reproduce the ambiguity, or the broken pin passes it")

        params, underivable, plane = _worker._resolve_from_plane(
            "lookup_glass", _contract.REFERENCE_CASES["lookup_glass"], {"glass": path})
        assert underivable is None, underivable
        assert plane == "glass"
        assert sorted(params) == ["catalog", "name"]
        assert _contract.FROM_PLANE not in params.values(), (
            "the sentinel must be REPLACED by derived data, never sent to the door")

        derived = lookup_glass(conn, params)
        assert derived["ok"] is True, derived
    finally:
        conn.close()


def test_d22_the_derived_pair_qualifies_rather_than_pinning_a_vendor(tmp_path):
    """The qualifier itself must not be pinned either.

    Adding a fixed ``catalog`` fixes the observed failure and MOVES the risk: the glass
    catalog is built from the USER'S OWN .agf files, so a machine whose set lacks that
    vendor then fails ``glass_not_found``.  The derived pair follows whatever catalogs are
    actually present — here, a set containing none of the usual vendors."""
    from optivibe_reference import glass_build
    from optivibe_reference.tools.lookup_glass import lookup_glass

    path = _glass_fixture(tmp_path, catalogs=("HOUSEBLEND.AGF", "SECONDVENDOR.AGF"))
    conn = glass_build.open_glass_catalog(json_path=path)
    try:
        params, underivable, _ = _worker._resolve_from_plane(
            "lookup_glass", _contract.REFERENCE_CASES["lookup_glass"], {"glass": path})
        assert underivable is None
        assert lookup_glass(conn, params)["ok"] is True, (
            "the derived pair must follow the machine's own catalogs")
    finally:
        conn.close()


@pytest.mark.parametrize("tier", ("enriched", "synonyms_only"))
def test_d22_the_pinned_operand_query_holds_in_both_enrichment_tiers(tmp_path, tier):
    """D-22's second half.  ``lookup_operand{"query": "chief ray"}`` was verified live only
    in the ``enriched`` state, so it runs against BOTH fixtures here.

    ``synonyms_only`` is a documented supported configuration (an install without the
    ``[manual]`` extra) and a pin that silently required enrichment would fail it."""
    from optivibe_reference import catalog_build
    from optivibe_reference.tools.lookup_operand import lookup_operand

    conn = catalog_build.open_catalog(catalog_json_path=_operand_fixture(tmp_path, tier))
    try:
        result = lookup_operand(conn, dict(_contract.REFERENCE_CASES["lookup_operand"]))
        assert result["ok"] is True, (tier, result)
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason=(
    "search_reference is dead by the shipped wiring defect: the reference Dispatcher "
    "builds every other plane's connection but never wires manual_conn, so the manual "
    "corpus can be present, valid and openable while this door still answers "
    "corpus_unavailable. NO query can be proven to return a hit on this release line. "
    "STRICT on purpose: the day the dispatcher wires that connection this test starts "
    "passing, the pin has become verifiable, and this xfail must be removed rather than "
    "left standing as a stale exemption."))
def test_d22_the_pinned_search_reference_case_is_unverifiable_on_this_release_line():
    """D-22's known-unverified pin, marked rather than quietly kept.

    This is the one pinned case that cannot be shown to hold, because the door it targets
    is unreachable in the shipped dispatcher.  An xfail keeps the assertion in the suite
    and keeps its cause written down; deleting it would let the pin rot unnoticed, and asserting
    it would redden the suite for a defect 0.1.3 ships on purpose.

    Honest limit: where the corpus is ABSENT (public CI gitignores it) this fails for the
    ordinary reason rather than the defect, so it discriminates the wiring only on a
    machine that has actually built the corpus.  Measured on this one: the file is present,
    self-consistent and openable by ``open_manual_corpus()`` under its OWN default
    argument, and the door still answers ``corpus_unavailable``."""
    from optivibe_reference.server import Dispatcher

    dispatcher = Dispatcher()
    envelope = dispatcher.dispatch(
        "search_reference", dict(_contract.REFERENCE_CASES["search_reference"]))
    assert envelope["result"]["ok"] is True, envelope["result"]


def test_d23_an_underivable_probe_input_is_unknown_and_never_a_tool_failure(tmp_path):
    """D-23, mechanism SEAM.

    Discriminating condition: a VALID but EMPTY catalog.  A populated fixture cannot reach
    the underivable branch at all.

    Doctor could not FORM the question, which is doctor's failure, not the door's — so the
    probe is UNKNOWN.  Falling back to a hardcoded name, or grading an underivable input as
    a refusal, both blame the target for doctor's own inability to ask."""
    path = os.path.join(str(tmp_path), "glass_catalog.json")
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"rows": []}))

    params, underivable, plane = _worker._resolve_from_plane(
        "lookup_glass", _contract.REFERENCE_CASES["lookup_glass"], {"glass": path})
    assert params is None
    assert underivable == "probe_input_underivable", underivable
    assert plane == "glass"

    facts = {"probe_input_underivable": True, "probe_input_reason": underivable,
             "probe_input_plane": plane, "inner_ok": None, "outer_ok": None}
    healthy_plane = Observation("plane.glass", {
        "present": True, "opens": True, "wired": True, "identity_ok": True,
        "path": path, "conn_attr": "glass_conn", "wired_path": ""})
    findings = evaluate({"plane.glass": healthy_plane,
                         "tool.lookup_glass": Observation("tool.lookup_glass", facts)},
                        checks=("plane.glass", "tool.lookup_glass"))
    door = [item for item in findings if item.check == "tool.lookup_glass"][0]
    assert door.status is Status.UNKNOWN, (door.status, door.reason)
    assert door.reason == "probe_input_underivable"
    assert door.status is not Status.FAIL


# ===========================================================================
# D-24  the absent-plane ordering, PINNED rather than assumed
#
# Mechanism SEAM.  Discriminating condition: the fixture's plane is ABSENT.  A provisioned
# fixture never exercises the sentinel path at all, so it would pass under either design
# and prove nothing.
#
# A clean public install is the scenario that closes the largest gap between "CI
# green" and "works": all five advertised reference doors are dead, and its acceptance is
# five doors with inner_ok false and outer_ok TRUE.  Two of those five take a glass
# argument, and on that install there is no glass catalog to derive one from.  Skipping
# them would report "didn't check" for two of five doors on the most common install in the
# world and hollow the scenario out.
# ===========================================================================
def _absent_glass(tmp_path):
    return {"glass": os.path.join(str(tmp_path), "nothing-here.json")}


# An AUTHORED pair-eligible glass catalog: two invented glasses whose Pg,F match is inside
# ``find_glass_pair``'s default window (|dPg,F| = 0.002 <= 0.005) while their Abbe numbers
# are far enough apart to be a usable pair (|dVd| = 24.0 >= 20.0).  Every name and every
# number here is INVENTED test data -- nothing is copied from the vendor-derived
# ``glass_catalog.json``, which is built from the user's licensed install and is never
# published (PROVENANCE.md), so this file must not carry any of its content.
#
# WHY THIS FIXTURE EXISTS.  The guard below needs the glass data PRESENT, and on the
# install this whole scenario grades it is absent -- so reading the ambient machine's
# catalog made the guard's outcome depend on whether the reader happened to have run the
# local build.  On a clean public clone BOTH sides of the discrimination answered
# ``glass_catalog_unavailable`` and the guard failed for lack of a plane, which is the one
# environment the published suite always runs in (CI).  Provisioning the plane restores the
# discriminating condition the docstring names rather than removing it.
_PAIR_GLASS_ROWS = (
    {"catalog": "AUTHORED-A", "name": "VITRACROWN",
     "nd": 1.5200, "vd": 60.0, "pg_f": 0.5350, "nd_valid": 1, "pg_f_valid": 1},
    {"catalog": "AUTHORED-A", "name": "VITRAFLINT",
     "nd": 1.6400, "vd": 36.0, "pg_f": 0.5370, "nd_valid": 1, "pg_f_valid": 1},
)


def _pair_glass_conn(tmp_path):
    """A live glass connection over the authored pair catalog above.

    Written through the real ``glass_build`` writer/opener rather than hand-rolled SQL, so a
    fixture that drifted out of the loader's shape would fail here instead of quietly
    grading a shape the product never reads.  The caller owns closing it."""
    from optivibe_reference import glass_build

    path = os.path.join(str(tmp_path), "authored_pair_catalog.json")
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"rows": [dict(row) for row in _PAIR_GLASS_ROWS]}))
    return glass_build.open_glass_catalog(json_path=path)


@pytest.mark.parametrize("door", ("lookup_glass", "find_glass_pair"))
def test_d24_an_absent_plane_resolves_to_the_sentinel_rather_than_omitting_the_param(
        tmp_path, door):
    """The resolution half.  Every FROM_PLANE param must come back PRESENT.

    Omitting it is the mutation this catches: a required param that is absent is rejected
    at DISPATCH level, which produces no inner envelope at all — measured against the real
    public tree, ``lookup_glass {}`` answers ``outer=False inner=None`` — and the
    clean-public-install evidence vanishes with it."""
    case = _contract.REFERENCE_CASES[door]
    params, underivable, plane = _worker._resolve_from_plane(
        door, case, _absent_glass(tmp_path))

    assert underivable is None, (
        "an absent plane must NOT route to the underivable branch: %r" % (underivable,))
    assert plane == "glass"
    assert params is not None
    assert sorted(params) == sorted(case), (
        "every pinned param name must survive resolution; omitting one is rejected at "
        "dispatch level and yields no inner envelope")
    derived = [name for name, value in case.items() if value is _contract.FROM_PLANE]
    assert derived, "this door must actually carry a FROM_PLANE param"
    for name in derived:
        assert params[name] == PROBE_SENTINEL, (name, params[name])
    assert _contract.FROM_PLANE not in params.values(), (
        "the FROM_PLANE object itself must never be sent to a door")


def test_d24_the_sentinel_is_not_a_plausible_glass_name(tmp_path):
    """Fail-loud by construction.

    The ordering assumption — plane gate before name resolution — is an assumption about
    SOMEONE ELSE'S code, which is exactly the kind that misled an earlier round.  A
    plausible-looking placeholder could accidentally RESOLVE if the ordering ever changed;
    this one cannot, so a changed ordering surfaces as a loud unknown-glass refusal rather
    than as an accidental pass.

    Measured: against a populated catalog the sentinel answers ``glass_unknown``, and it
    answers it whether or not a catalog qualifier is supplied."""
    from optivibe_reference import glass_build
    from optivibe_reference.tools.lookup_glass import lookup_glass

    conn = glass_build.open_glass_catalog(json_path=_glass_fixture(tmp_path))
    try:
        for params in ({"name": PROBE_SENTINEL},
                       {"name": PROBE_SENTINEL, "catalog": PROBE_SENTINEL}):
            answer = lookup_glass(conn, dict(params))
            assert answer["ok"] is False, (
                "the sentinel must never resolve against a populated catalog: %r" % (answer,))
            assert answer.get("error_family") == "glass_unknown", answer
            assert answer.get("error_family") != "ambiguous_glass", (
                "a sentinel that collided with real rows would be a data value again")
    finally:
        conn.close()


@pytest.mark.parametrize("door", ("lookup_glass", "find_glass_pair"))
def test_d24_omitting_the_param_destroys_the_inner_envelope_the_sentinel_preserves(
        tmp_path, door):
    """D-24's named mutation, run against the REAL reference dispatcher — BOTH sides.

    The mutation is *resolve FROM_PLANE to nothing and omit the param*.  The clean-public-
    install acceptance reads ``facts.outer_ok`` and ``facts.inner_ok``, so what the guard
    must show is that the
    omission does not deliver the same evidence as the fix.  Measured, and the two doors
    fail DIFFERENTLY, which is why both are exercised:

    * ``lookup_glass`` advertises ``name`` as REQUIRED, so the omission is rejected at
      DISPATCH level — ``outer_ok=False`` and **no inner envelope at all**.  The evidence is
      not wrong, it is gone.
    * ``find_glass_pair`` advertises NO required param, so the omission is not rejected: the
      door quietly answers a *different question*.  Where the glass data is present it
      reports a happy ``inner_ok=True`` — a door claiming to answer while, on the install
      that scenario grades, its plane is absent.  That is a false green, which is worse.

    Asserting only the sentinel side would pass under the mutation; it is the PAIR that
    discriminates, and the pair is keyed on the door's own ADVERTISED ``required_params``
    rather than on a guess about which one is required.

    The glass plane is PROVISIONED from the authored fixture above and injected, because the
    ``find_glass_pair`` half is only observable where the glass data is present: with the
    plane absent the omission and the sentinel BOTH answer
    ``glass_catalog_unavailable`` and the pair stops discriminating.  The DERIVATION plane
    stays absent (``_absent_glass``) -- that is what produces the sentinel under test."""
    from optivibe_reference.server import Dispatcher

    glass_conn = _pair_glass_conn(tmp_path)
    try:
        dispatcher = Dispatcher(glass_conn=glass_conn)
        # The discriminating condition, asserted rather than assumed: a dispatcher whose
        # glass plane was absent would answer *_unavailable on BOTH sides below and pass
        # the `mutated != kept` line vacuously for the wrong reason.
        assert dispatcher.glass_conn is not None, (
            "this guard needs the glass plane PRESENT; with it absent both sides answer "
            "glass_catalog_unavailable and the pair proves nothing")
        advertised = {entry["name"]: entry for entry in dispatcher.list_tools()}
        case = _contract.REFERENCE_CASES[door]
        params, underivable, _plane = _worker._resolve_from_plane(
            door, case, _absent_glass(tmp_path))
        assert underivable is None

        omitted = {name: value for name, value in case.items()
                   if value is not _contract.FROM_PLANE}
        dropped = sorted(set(params) - set(omitted))
        assert dropped, "the mutation must actually drop something"

        def evidence(envelope):
            result = envelope.get("result")
            inner = result.get("ok") if isinstance(result, dict) else None
            return (envelope.get("ok"), inner)

        kept = evidence(dispatcher.dispatch(door, dict(params)))
        assert kept == (True, False), (
            "the sentinel call must reach the door and be refused by it: %r" % (kept,))
        # ...and refused for the RIGHT reason: the sentinel does not resolve against a
        # populated catalog.  Keyed on the family so a plane that quietly went absent
        # cannot masquerade as the honest unknown-glass refusal this line pins.
        sentinel_result = dispatcher.dispatch(door, dict(params)).get("result") or {}
        assert sentinel_result.get("error_family") == "glass_unknown", (
            "the sentinel must be refused by NAME RESOLUTION against a present plane, "
            "not by an absent one: %r" % (sentinel_result.get("error_family"),))

        mutated = evidence(dispatcher.dispatch(door, dict(omitted)))
        assert mutated != kept, (
            "the omission must be distinguishable from the sentinel, or the guard proves "
            "nothing: %r" % (mutated,))

        if set(dropped) & set(advertised[door]["required_params"]):
            assert mutated == (False, None), (
                "a dropped REQUIRED param must be rejected at dispatch level, leaving no "
                "inner envelope: %r" % (mutated,))
        else:
            assert mutated[1] is not False, (
                "a dropped optional param must not silently reproduce the refusal the "
                "sentinel earns honestly: %r" % (mutated,))
    finally:
        glass_conn.close()


def test_d24_an_absent_plane_refuses_with_a_family_the_grader_reads_as_plane_absent():
    """The ordering contract, crossed against a door whose plane really is unavailable.

    ``classify_tool``'s ``plane_absent`` arm keys on an ``*_unavailable`` family, so the
    reference layer's absent-plane refusal and doctor's grading rule are two halves of one
    contract.  A refusal family that stopped ending ``_unavailable`` would silently drop
    every absent-plane door out of WARN and into ``malformed_envelope`` FAIL — turning a
    clean install's normal state into breakage.

    Discriminating condition: the door's plane must actually be unavailable in this
    environment.  Where it is not, the guard says so out loud rather than passing vacuously.

    (The private tree and the published tree diverge here by design — the published
    ``server.py`` treats the glass connection as optional because ``glass_catalog.json`` is
    not published — so this is written against whichever door is genuinely unavailable
    rather than against a hardcoded one.)"""
    from optivibe_reference.server import Dispatcher

    dispatcher = Dispatcher()
    unavailable = []
    for door, case in sorted(_contract.REFERENCE_CASES.items()):
        params, underivable, _plane = _worker._resolve_from_plane(door, case, {})
        if underivable is not None:
            continue
        envelope = dispatcher.dispatch(door, dict(params))
        result = envelope.get("result")
        if not isinstance(result, dict) or result.get("ok") is not False:
            continue
        family = str(result.get("error_family") or "")
        if family.endswith("_unavailable"):
            unavailable.append((door, family))

    if not unavailable:
        pytest.skip("no reference plane is unavailable in this environment, so the "
                    "absent-plane family cannot be observed here; the published-tree "
                    "proof is the clean-public-install scenario")
    for door, family in unavailable:
        status, reason = classify_tool(door, False, family, Status.WARN, "absent_expected")
        assert (status, reason) == (Status.WARN, "plane_absent"), (door, family)


def test_d24_an_absent_plane_grades_the_door_warn_plane_absent_not_skip(tmp_path):
    """The grading half.  The door was CALLED, so it is graded — WARN/``plane_absent``.

    Under the previous design this was SKIP, which reports "not attempted" for a probe
    doctor perfectly well attempted, and leaves the clean-public-install scenario unable to
    show that the door is dead."""
    facts = {"params": ["catalog", "name"], "sentinel_params": ["catalog", "name"],
             "outer_ok": True, "inner_ok": False,
             "error_family": "glass_catalog_unavailable"}
    absent_plane = Observation("plane.glass", {
        "present": False, "opens": None, "wired": None, "identity_ok": None,
        "path": None, "conn_attr": "glass_conn", "wired_path": None})
    findings = evaluate({"plane.glass": absent_plane,
                         "tool.lookup_glass": Observation("tool.lookup_glass", facts)},
                        checks=("plane.glass", "tool.lookup_glass"))
    door = [item for item in findings if item.check == "tool.lookup_glass"][0]
    assert door.status is Status.WARN, (door.status, door.reason)
    assert door.reason == "plane_absent"
    assert door.blocked_by == "", "a graded door is not a skipped one"
    assert door.facts["outer_ok"] is True and door.facts["inner_ok"] is False, (
        "both envelopes must reach the report — that pair IS the clean-public-install "
        "evidence")
    assert door.remedy, "an absent plane must still tell the reader how to fix it"


def test_d24_a_clean_public_install_grades_all_five_doors_warn_plane_absent():
    """The clean-public-install acceptance, unamended: FIVE doors, not three.

    Mutation: route the absent-plane case to SKIP and two of the five stop being graded —
    the scenario that proves every advertised door is dead would report on three of them."""
    observations = {}
    for plane in PLANE_NAMES:
        observations["plane." + plane] = Observation("plane." + plane, {
            "path": None, "present": False, "opens": None, "wired": None,
            "identity_ok": None, "conn_attr": "conn", "wired_path": None})
    for door, plane in sorted(TOOL_PLANE.items()):
        family = {"glass": "glass_catalog_unavailable",
                  "manual": "corpus_unavailable"}.get(plane, "catalog_unavailable")
        observations["tool." + door] = Observation("tool." + door, {
            "outer_ok": True, "inner_ok": False, "error_family": family})
    checks = tuple("plane." + plane for plane in PLANE_NAMES) + tuple(
        "tool." + door for door in sorted(TOOL_PLANE))
    findings = {item.check: item for item in evaluate(observations, checks=checks)}

    doors = [findings["tool." + door] for door in sorted(TOOL_PLANE)]
    assert len(doors) == 5
    for door in doors:
        assert door.status is Status.WARN, (door.check, door.status, door.reason)
        assert door.reason == "plane_absent", door.check
        assert door.facts["inner_ok"] is False and door.facts["outer_ok"] is True, door.check
    assert not any(item.status is Status.FAIL for item in findings.values()), (
        "absent data is normal, never breakage")


# ===========================================================================
# D-25  enrichment carries its plane's boundary
#
# Mechanism SEAM.  Discriminating condition: the fixture must have the PLANE ABSENT.  A
# malformed-JSON-on-a-present-plane fixture exercises the OTHER branch and passes either
# way, which is exactly why the defect survived until now.
#
# `enrichment.merit` / `enrichment.tolerance` read `build_report.descriptions_state` out of
# `operand_catalog.json`, which is NOT published.  Under a rule that graded an absent
# plane's tier UNKNOWN, every fresh public install read UNKNOWN twice and exited 3 — while
# the behaviour matrix and the clean-public-install scenario both say that install is
# DEGRADED/1.
# ===========================================================================
def _enrichment_run(present, tier):
    """Grade one enrichment domain beside its plane, at the evaluate seam."""
    findings = {}
    for domain, plane in sorted(ENRICHMENT_PLANE.items()):
        observations = {
            "plane." + plane: Observation("plane." + plane, {
                "path": "C:/d/%s" % plane, "present": present,
                "opens": True if present else None,
                "wired": True if present else None,
                "identity_ok": True if present else None,
                "conn_attr": "conn", "wired_path": None}),
            "enrichment." + domain: Observation("enrichment." + domain, {
                "tier": tier, "plane": plane, "plane_present": present}),
        }
        graded = evaluate(observations,
                          checks=("plane." + plane, "enrichment." + domain))
        for item in graded:
            findings[item.check] = item
    return findings


@pytest.mark.parametrize("tier", (None, "enriched"))
def test_d25_an_absent_plane_makes_its_enrichment_check_skip_not_unknown(tier):
    """The defect, closed.  Absent plane -> SKIP naming it, whatever the tier reads.

    The tier is parameterised because the absence of the CATALOG is the whole input: with
    no file there is no report, so ``None`` is what the worker will emit — and the rule must
    not become "UNKNOWN unless the tier happens to be readable"."""
    findings = _enrichment_run(present=False, tier=tier)
    for domain, plane in sorted(ENRICHMENT_PLANE.items()):
        item = findings["enrichment." + domain]
        assert item.status is Status.SKIP, (domain, item.status, item.reason)
        assert item.blocked_by == "plane." + plane, (domain, item.blocked_by)
        assert findings["plane." + plane].status is Status.WARN, (
            "the blocker a SKIP names must itself be FAIL or WARN, or the SKIP invariant "
            "makes the whole run INCOMPLETE")


def test_d25_a_present_plane_with_an_unreadable_report_is_still_unknown():
    """The other half of the boundary, and the reason it is a boundary.

    The file is THERE, doctor tried to read a tier out of it and could not — that is
    doctor failing to measure, which is UNKNOWN.  Collapsing the two halves in either
    direction loses a real distinction: one is the target's configuration, the other is
    doctor's own blind spot."""
    findings = _enrichment_run(present=True, tier=None)
    for domain in sorted(ENRICHMENT_PLANE):
        item = findings["enrichment." + domain]
        assert item.status is Status.UNKNOWN, (domain, item.status, item.reason)
        assert item.reason == "tier_unreadable", (domain, item.reason)
        assert item.blocked_by == "", domain


def test_d25_a_fresh_public_install_is_degraded_not_incomplete():
    """The consequence the guard exists for, asserted as an exit code.

    Mutation: grade an absent plane's tier UNKNOWN and this run becomes INCOMPLETE/3.  Exit
    3 is "doctor could not measure" and is never a health claim — reporting it for a
    perfectly ordinary fresh install is the report crying wolf at its own users, and it
    contradicts both the behaviour matrix and the clean-public-install scenario's exit 1."""
    observations = {}
    for plane in PLANE_NAMES:
        observations["plane." + plane] = Observation("plane." + plane, {
            "path": None, "present": False, "opens": None, "wired": None,
            "identity_ok": None, "conn_attr": "conn", "wired_path": None})
    for domain, plane in sorted(ENRICHMENT_PLANE.items()):
        observations["enrichment." + domain] = Observation(
            "enrichment." + domain, {"tier": None, "plane": plane,
                                     "plane_present": False})
    checks = tuple("plane." + plane for plane in PLANE_NAMES) + tuple(
        "enrichment." + domain for domain in sorted(ENRICHMENT_PLANE))
    findings = evaluate(observations, checks=checks)

    assert not any(item.status is Status.UNKNOWN for item in findings), (
        [item.check for item in findings if item.status is Status.UNKNOWN])
    summary = summarize(findings, expected=checks)
    assert summary.state is State.DEGRADED, summary.state
    assert exit_code(summary) == 1


def test_d25_the_worker_emits_the_tier_beside_the_plane_it_came_from(tmp_path):
    """The worker's half: the observation must name its own plane and its presence.

    Without that the reader of an NDJSON line cannot tell "no report to read" from "the
    report did not say", which is the same collapse the grading rule refuses to make."""
    from optivibe_doctor import _worker as worker

    emitted = []
    original = worker.emit
    worker.emit = lambda check, **facts: emitted.append((check, facts))
    try:
        present = _operand_fixture(tmp_path, "enriched")
        paths = {"merit_operand": present,
                 "tolerance_operand": os.path.join(str(tmp_path), "absent.json")}
        for domain, plane in sorted(_contract.ENRICHMENT_PLANE.items()):
            worker.emit("enrichment." + domain,
                        tier=worker._enrichment_tier(plane, paths),
                        plane=plane,
                        plane_present=bool(paths.get(plane))
                        and os.path.isfile(paths[plane]))
    finally:
        worker.emit = original

    facts = dict((check, payload) for check, payload in emitted)
    assert facts["enrichment.merit"]["plane"] == "merit_operand"
    assert facts["enrichment.merit"]["plane_present"] is True
    assert facts["enrichment.merit"]["tier"] == "enriched"
    assert facts["enrichment.tolerance"]["plane"] == "tolerance_operand"
    assert facts["enrichment.tolerance"]["plane_present"] is False
    assert facts["enrichment.tolerance"]["tier"] is None
    for payload in facts.values():
        assert "status" not in payload, "a worker never writes a verdict"


def test_d4_every_pinned_param_name_is_advertised_by_its_door():
    """D-4, extended for the sentinel: NAMES stay statically pinned, only VALUES are
    derived, so the name assertion is unaffected by FROM_PLANE and must still hold."""
    from optivibe_reference.server import Dispatcher

    advertised = {entry["name"]: entry for entry in Dispatcher().list_tools()}
    for door, case in _contract.REFERENCE_CASES.items():
        assert door in advertised, door
        allowed = set(advertised[door]["param_types"]) | set(
            advertised[door]["required_params"])
        unknown = sorted(set(case) - allowed)
        assert unknown == [], "%s does not advertise %r" % (door, unknown)
        missing = sorted(set(advertised[door]["required_params"]) - set(case))
        assert missing == [], "%s requires %r and the case omits it" % (door, missing)


def test_every_from_plane_sentinel_has_a_declared_source():
    """A sentinel with no resolution rule would be sent to the door verbatim — an opaque
    object where a string belongs.  Every one must be resolvable, and every declared
    resolution must name a real plane."""
    for door, case in _contract.REFERENCE_CASES.items():
        for name, value in case.items():
            if value is _contract.FROM_PLANE:
                assert (door, name) in _contract.FROM_PLANE_SOURCE, (door, name)
    for (door, name), (plane, query, column) in _contract.FROM_PLANE_SOURCE.items():
        assert _contract.REFERENCE_CASES[door][name] is _contract.FROM_PLANE, (door, name)
        assert plane in _contract.PLANES, plane
        assert isinstance(query, str) and query.strip().lower().startswith("select")
        assert isinstance(column, int) and column >= 0


# ===========================================================================
# B-6 / B-7  the parent is importable in a hostile child, and inert
# ===========================================================================
_HOSTILE_ROOTS = ("optivibe_harness", "optivibe_reference", "psutil", "mcp",
                  "matplotlib", "numpy")


def test_b6_the_parent_imports_with_every_heavy_dependency_refused():
    """B-6.  If any parent module reached a non-stdlib import at module scope, the whole
    diagnostic would die on exactly the installs it exists to diagnose — and it would die
    without saying why."""
    guard_dir = tempfile.mkdtemp()
    try:
        with open(os.path.join(guard_dir, "sitecustomize.py"), "w", newline="\n") as handle:
            handle.write(_import_guard_source(_HOSTILE_ROOTS))
        env = _child_env({
            "PYTHONPATH": os.pathsep.join([guard_dir, DOCTOR_SRC]),
            RECEIPT_ENV: os.path.join(guard_dir, "receipt.json")})
        completed = subprocess.run(
            [sys.executable, "-c",
             "import json, optivibe_doctor, optivibe_doctor._runner as r, "
             "optivibe_doctor.__main__ as m, optivibe_doctor._render as d, "
             "optivibe_doctor._classify as c, optivibe_doctor._contract as k;"
             "print(json.dumps({'schema': optivibe_doctor.SCHEMA_VERSION,"
             " 'run': hasattr(r, 'run'), 'main': hasattr(m, 'main'),"
             " 'spine': len(k.BASE_CHECKS)}))"],
            capture_output=True, text=True, env=env, timeout=300)
        assert completed.returncode == 0, completed.stderr[-3000:]
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload["schema"] == SCHEMA
        assert payload["run"] and payload["main"]
        assert payload["spine"] == len(BASE_CHECKS)
    finally:
        shutil.rmtree(guard_dir, ignore_errors=True)


def test_b7_importing_doctor_does_not_disturb_the_pythonnet_runtime_selector():
    """B-7.  ``_bootstrap`` ``setdefault``s ``PYTHONNET_RUNTIME`` at import time.  If the
    parent reached it transitively, the ``base`` worker's reading would hand doctor's own
    side effect back to the operator as their configuration — and the override check would
    become a step that can never fire."""
    env = _child_env()
    env.pop("PYTHONNET_RUNTIME", None)
    completed = subprocess.run(
        [sys.executable, "-c",
         "import json, os;"
         "before = os.environ.get('PYTHONNET_RUNTIME');"
         "import optivibe_doctor, optivibe_doctor._runner, optivibe_doctor.__main__;"
         "print(json.dumps({'before': before,"
         " 'after': os.environ.get('PYTHONNET_RUNTIME')}))"],
        capture_output=True, text=True, env=env, timeout=300)
    assert completed.returncode == 0, completed.stderr[-3000:]
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["before"] is None
    assert payload["after"] is None, (
        "importing doctor set PYTHONNET_RUNTIME to %r" % (payload["after"],))


# ===========================================================================
# B-1 .. B-5  broken-install children
# ===========================================================================
def _shadow_dir(files):
    """Materialise a ``sys.path`` entry containing the given ``{relpath: source}``."""
    root = tempfile.mkdtemp(prefix="optivibe-doctor-shadow-")
    for relpath, source in files.items():
        target = os.path.join(root, relpath)
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(target, "w", newline="\n", encoding="utf-8") as handle:
            handle.write(source)
    return root


def test_b1_metadata_present_but_the_module_raises_is_a_broken_install():
    """B-1.  Mechanism SHADOW — a ``psutil.py`` that IMPORTS successfully as a file and
    raises when it executes.

    Discriminating condition: the metadata still answers, ``find_spec`` still returns a
    spec, and only the real import raises.  An implementation that grades on
    ``importlib.metadata.version`` reports it healthy; so does one that grades on
    ``find_spec``.  Both are recorded as facts here precisely so the guard can prove they
    were insufficient."""
    shadow = _shadow_dir({"psutil.py": "raise RuntimeError('this psutil is a wreck')\n"})
    try:
        env = _child_env({"PYTHONPATH": os.pathsep.join([shadow, DOCTOR_SRC])})
        records, completed = _run_worker("pkg", env=env)
        facts = records.get("dependency.psutil")
        assert facts is not None, completed.stderr[-2000:]
        assert facts["meta_version"] is not None, (
            "the shadow fixture must leave the metadata answering, or the guard proves "
            "nothing about a metadata-grading implementation")
        assert facts["find_spec_found"] is True, (
            "the shadow fixture must leave find_spec answering")
        assert facts["import_ok"] is False
        assert "RuntimeError" in (facts["import_error"] or "")
        status, reason = classify_dep(
            "psutil", facts["declared"], facts["meta_version"], facts["import_ok"],
            facts["import_error"])
        assert (status, reason) == (Status.FAIL, "declared_but_unimportable")
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def test_b2_the_dependency_check_really_attempts_the_backend_import():
    """B-2.  Mechanism METAPATH, with a WORKER-PRIVATE receipt path.

    Discriminating condition: the receipt is written by the ``pkg`` child and by nothing
    else.  A shared receipt path is also written by the ``zemax`` child's ``load_zosapi``,
    so deleting the dependency check would leave a non-empty receipt and the guard would
    certify a check that no longer exists."""
    guard_dir = tempfile.mkdtemp()
    try:
        with open(os.path.join(guard_dir, "sitecustomize.py"), "w", newline="\n") as handle:
            handle.write(_import_guard_source())
        receipt = os.path.join(guard_dir, "pkg-only-receipt.json")
        env = _child_env({
            "PYTHONPATH": os.pathsep.join([guard_dir, DOCTOR_SRC]),
            RECEIPT_ENV: receipt})
        records, completed = _run_worker("pkg", env=env)
        facts = records.get("dependency.pythonnet")
        assert facts is not None, completed.stderr[-2000:]
        assert facts["import_ok"] is False
        status, _reason = classify_dep(
            "pythonnet", facts["declared"], facts["meta_version"], facts["import_ok"],
            facts["import_error"])
        assert status is Status.FAIL
        assert os.path.isfile(receipt), "the pkg worker's own receipt was never written"
        with open(receipt, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["armed"] is True
        assert "clr" in payload["attempted"], (
            "the pkg worker never attempted the backend import; the dependency check is "
            "not doing what this guard certifies")
    finally:
        shutil.rmtree(guard_dir, ignore_errors=True)


def test_b3_a_child_with_psutil_blocked_still_produces_a_whole_json_document():
    """B-3.  Mechanism METAPATH.

    Discriminating condition: ``psutil`` is genuinely absent.  If any parent module reached
    an ``optivibe_*`` import at module scope the process would die with a traceback and no
    document at all — so the assertion is that the DOCUMENT exists, not merely that some
    finding says the right thing."""
    guard_dir = tempfile.mkdtemp()
    try:
        with open(os.path.join(guard_dir, "sitecustomize.py"), "w", newline="\n") as handle:
            handle.write(_import_guard_source(("psutil",)))
        env = _child_env({
            "PYTHONPATH": os.pathsep.join([guard_dir, DOCTOR_SRC]),
            RECEIPT_ENV: os.path.join(guard_dir, "receipt.json")})
        completed = _run_doctor(["--format", "ndjson"], env=env)
        records = _ndjson_of(completed)
        assert records, "the run produced no document at all: %s" % completed.stderr[-2000:]
        assert records[-1]["type"] == "summary"
        findings = _findings_of(records)
        assert findings["dependency.psutil"]["status"] == "fail"
        assert "psutil" in findings["dependency.psutil"]["summary"]
        # The base tier is unaffected: it is a separate child and does not import psutil.
        for check in ("env.python", "env.cwd_shadow", "env.pythonnet_runtime",
                      "version.optivibe-harness", "version.optivibe-reference"):
            assert findings[check]["status"] in ("pass", "warn"), check
        for check in BASE_CHECKS:
            assert check in findings, check
    finally:
        shutil.rmtree(guard_dir, ignore_errors=True)


def test_b4_a_child_with_the_reference_package_blocked_skips_by_name_and_exits_two():
    """B-4.  Mechanism METAPATH.

    Discriminating condition: every plane, tool and enrichment id must still be PRESENT, as
    SKIPs naming their blocker.  Dropping them when the package is missing breaks spine
    coverage, which produces exit 3 — so the ``== 2`` assertion is what separates a report
    that declined to probe from one that silently stopped covering."""
    guard_dir = tempfile.mkdtemp()
    try:
        with open(os.path.join(guard_dir, "sitecustomize.py"), "w", newline="\n") as handle:
            handle.write(_import_guard_source(("optivibe_reference",)))
        env = _child_env({
            "PYTHONPATH": os.pathsep.join([guard_dir, DOCTOR_SRC]),
            RECEIPT_ENV: os.path.join(guard_dir, "receipt.json")})
        completed = _run_doctor(["--format", "ndjson"], env=env)
        records = _ndjson_of(completed)
        assert records, completed.stderr[-2000:]
        findings = _findings_of(records)
        assert findings["reference.import"]["status"] == "fail"
        assert findings["reference.import"]["remedy"] == _contract.REMEDY["reference_missing"]
        blocked = [check for check in BASE_CHECKS
                   if check.startswith(("plane.", "tool.", "enrichment."))
                   and check not in ("plane.coverage", "tool.coverage")]
        for check in blocked:
            record = findings[check]
            assert record["status"] == "skip", (check, record["status"])
            assert record["blocked_by"] == "reference.import", check
        for check in BASE_CHECKS:
            assert check in findings, check
        assert completed.returncode == 2, (
            "a declined probe is BROKEN by its blocker, not INCOMPLETE: %d" %
            completed.returncode)
    finally:
        shutil.rmtree(guard_dir, ignore_errors=True)


_SHADOW_MCP = {
    "mcp/__init__.py": "",
    "mcp/types.py": (
        "class Tool(object):\n"
        "    def __init__(self, **kwargs):\n"
        "        self.__dict__.update(kwargs)\n"
        "\n"
        "class TextContent(object):\n"
        "    def __init__(self, **kwargs):\n"
        "        self.__dict__.update(kwargs)\n"),
    "mcp/server/__init__.py": (
        "class Server(object):\n"
        "    \"\"\"An mcp 2.x-shaped Server: imports perfectly, has no list_tools.\"\"\"\n"
        "    def __init__(self, name, instructions=None):\n"
        "        self.name = name\n"
        "        self.instructions = instructions\n"),
    "mcp/server/stdio.py": "def stdio_server(*args, **kwargs):\n    raise NotImplementedError\n",
    "mcp/server/models.py": "class InitializationOptions(object):\n    pass\n",
}


def test_b5_an_mcp_that_imports_but_cannot_build_the_server_is_caught_by_the_call():
    """B-5, and it closes the audit's own CRIT.  Mechanism SHADOW — never METAPATH.

    Discriminating condition: ``find_spec('mcp')`` is TRUE, ``import mcp.types`` succeeds,
    ``from mcp.server import Server`` succeeds, and only the BUILDER CALL raises.  An
    implementation that substitutes ``find_spec`` or an import for the call reports PASS.

    The meta-path guard must not be used here: it makes ``find_spec`` raise, so the
    mutation would redden for the wrong reason or not at all."""
    shadow = _shadow_dir(_SHADOW_MCP)
    try:
        env = _child_env({"PYTHONPATH": os.pathsep.join([shadow, DOCTOR_SRC])})
        # The fixture's own precondition, measured rather than assumed.
        probe = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util, json;"
             "spec = importlib.util.find_spec('mcp');"
             "import mcp.types;"
             "from mcp.server import Server;"
             "print(json.dumps({'find_spec': spec is not None,"
             " 'imports': True, 'has_list_tools': hasattr(Server, 'list_tools')}))"],
            capture_output=True, text=True, env=env, timeout=180)
        assert probe.returncode == 0, probe.stderr[-2000:]
        precondition = json.loads(probe.stdout.strip().splitlines()[-1])
        assert precondition["find_spec"] is True
        assert precondition["imports"] is True
        assert precondition["has_list_tools"] is False

        records, completed = _run_worker("pkg", env=env)
        construct = records.get("mcp.construct")
        assert construct is not None, completed.stderr[-2000:]
        assert construct["ok"] is False
        assert construct["error_type"] == "AttributeError", construct
        assert "list_tools" in construct["error"], construct
        manifest = records.get("harness.manifest")
        assert manifest is not None and manifest["ok"] is True, (
            "the shadow mcp must not take the harness manifest down too, or the pairing "
            "below proves nothing")
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def test_d18_the_construct_failure_and_the_manifest_pass_are_one_conjunction():
    """D-18.  Shares B-5's fixture; the CONJUNCTION is the discriminator.

    Two independent row assertions would each pass under a broad breakage that took both
    down — and a broad breakage is exactly what the gate must be able to rule out when it
    attributes the fault to mcp rather than to the package."""
    shadow = _shadow_dir(_SHADOW_MCP)
    try:
        env = _child_env({"PYTHONPATH": os.pathsep.join([shadow, DOCTOR_SRC])})
        completed = _run_doctor(["--format", "ndjson"], env=env)
        records = _ndjson_of(completed)
        assert records, completed.stderr[-2000:]
        findings = _findings_of(records)
        pairing = (findings["mcp.construct"]["status"],
                   findings["harness.manifest"]["status"])
        assert pairing == ("fail", "pass"), (
            "the unsupported-mcp attribution needs mcp.construct FAIL *and* "
            "harness.manifest PASS in "
            "the same run; got %r" % (pairing,))
        assert "AttributeError" in findings["mcp.construct"]["summary"]
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def test_d12_the_supported_range_is_never_read_from_installed_metadata():
    """D-12.  Mechanism SHADOW + SEAM.

    Discriminating condition: a shadowed ``mcp`` reporting version 2.0.0 whose OWN metadata
    declares a WIDER supported range.  An implementation that derives the range from
    metadata passes on this child; the pinned one still fails, which is the whole of the
    out-of-range-but-constructs scenario."""
    shadow = _shadow_dir(dict(_SHADOW_MCP, **{
        "mcp/__init__.py": "__version__ = '2.0.0'\n",
        "mcp-2.0.0.dist-info/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: mcp\n"
            "Version: 2.0.0\n"
            "Requires-Dist: anyio>=3\n"),
        "mcp-2.0.0.dist-info/RECORD": "",
        "mcp-2.0.0.dist-info/INSTALLER": "pip\n",
    }))
    try:
        env = _child_env({"PYTHONPATH": os.pathsep.join([shadow, DOCTOR_SRC])})
        records, completed = _run_worker("pkg", env=env)
        facts = records.get("dependency.mcp_range")
        assert facts is not None, completed.stderr[-2000:]
        assert facts["version"] == "2.0.0", (
            "the shadow dist-info must be the one that answers, or the guard is not "
            "exercising a wider-metadata install at all")
        status, reason = classify_mcp_range(facts["version"])
        assert (status, reason) == (Status.FAIL, "unsupported_range")
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def test_d12_the_pinned_range_matches_the_harness_manifests_own_requirement():
    """The drift half of D-12, asserted in BOTH directions so a change to either side is
    visible rather than silently absorbed."""
    import re
    import tomllib

    root = _checkout_root()
    if root is None:
        pytest.skip("no checkout is locatable from here")
    manifest = os.path.join(root, "packages", "optivibe-harness", "pyproject.toml")
    with open(manifest, "rb") as handle:
        declared = tomllib.load(handle)["project"].get("dependencies", [])
    requirement = [entry for entry in declared
                   if re.match(r"^\s*mcp\b", str(entry))]
    assert len(requirement) == 1, "the harness manifest declares mcp %r times" % (
        len(requirement),)
    text = requirement[0]
    lower = re.search(r">=\s*(\d+)\.(\d+)", text)
    upper = re.search(r"<\s*(\d+)(?:\.(\d+))?", text)
    assert lower is not None and upper is not None, (
        "the mcp requirement %r does not declare a bounded range" % (text,))
    assert (int(lower.group(1)), int(lower.group(2))) == MCP_MIN
    assert (int(upper.group(1)), int(upper.group(2) or 0)) == MCP_MAX_EXCLUSIVE


# ===========================================================================
# E-1 / E-5  the write audits
# ===========================================================================
def _tree_fingerprint(root):
    """Return ``{relpath: (size, mtime_ns, sha256)}`` for every file under a root.

    Set equality over names catches neither an overwrite nor a write into ``%TEMP%``;
    content hashes catch both, which is why this is a fingerprint and not a listing."""
    import hashlib

    found = {}
    for current, _dirs, names in os.walk(root):
        for name in sorted(names):
            path = os.path.join(current, name)
            try:
                stat = os.stat(path)
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
            found[os.path.relpath(path, root)] = (stat.st_size, stat.st_mtime_ns, digest)
    return found


def test_e1_a_default_run_writes_nothing_into_any_redirected_root(tmp_path):
    """E-1.  Every root doctor could plausibly write to is redirected into one isolated
    tree pre-seeded with a decoy, and the whole tree is compared BY CONTENT.

    Discriminating condition: the decoy.  A run that OVERWROTE an existing file leaves the
    name set identical, so ``os.listdir`` equality catches nothing; only the hash
    comparison detects it.  The same comparison catches a write into ``%TEMP%``, which a
    cwd-only audit misses entirely."""
    root = os.path.join(str(tmp_path), "isolated")
    for leaf in ("home", "temp", "work"):
        os.makedirs(os.path.join(root, leaf))
    with open(os.path.join(root, "home", "decoy.txt"), "w", encoding="utf-8") as handle:
        handle.write("do not touch me")

    home = os.path.join(root, "home")
    temp = os.path.join(root, "temp")
    env = _child_env({
        "HOME": home, "USERPROFILE": home,
        "TEMP": temp, "TMP": temp, "TMPDIR": temp,
        "XDG_CACHE_HOME": home, "XDG_DATA_HOME": home, "XDG_CONFIG_HOME": home,
    })
    before = _tree_fingerprint(root)
    assert before, "the audit tree is empty; the decoy was not written"
    completed = _run_doctor(["--format", "ndjson"], env=env, cwd=os.path.join(root, "work"))
    after = _tree_fingerprint(root)
    assert completed.returncode in (0, 1, 2, 3)
    changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    assert before == after, (
        "a default run changed the isolated tree.  only-before=%r only-after=%r changed=%r"
        % (sorted(set(before) - set(after)), sorted(set(after) - set(before)), changed))


def test_e3_boot_redirects_the_grandchilds_temp_and_leaves_the_ledger_untouched():
    """E-3.  The single most dangerous thing ``--boot`` could do is reclaim somebody else's
    engine.

    The harness entry point sweeps orphaned engines at start-up and that sweep TERMINATES
    processes; it reads its ledger from ``tempfile.gettempdir()`` **at call time**.  So the
    grandchild is spawned with ``TEMP``/``TMP``/``TMPDIR`` at a fresh empty directory and
    reads an empty ledger.

    Discriminating condition: the machine-global ledger file must be BYTE-identical across
    the run.  Drop the redirect and the grandchild sweeps the real ledger — which is a
    behaviour no assertion about doctor's own output could ever detect."""
    import hashlib

    from optivibe_harness import engine_ledger

    ledger = engine_ledger.ledger_path()
    scratch = os.path.join(os.path.dirname(ledger) or ".", "optivibe-doctor-e3-probe")

    def snapshot():
        if not os.path.isfile(ledger):
            return None
        with open(ledger, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    before = snapshot()
    completed = _run_doctor(["--boot", "--format", "ndjson"], timeout=600)
    after = snapshot()
    assert before == after, (
        "the --boot grandchild changed the machine-global engine ledger at %r" % ledger)

    findings = _findings_of(_ndjson_of(completed))
    boot = findings.get("boot.exit")
    assert boot is not None, completed.stderr[-2000:]
    assert boot["status"] in ("pass", "fail", "unknown"), boot
    # The throwaway directory is the only permitted delta, and it must be gone by exit.
    temp_dir = boot["facts"].get("temp_dir")
    assert temp_dir, boot
    assert not os.path.isdir(temp_dir), (
        "the --boot scratch directory %r survived the run" % temp_dir)
    assert not os.path.isdir(scratch)


def test_e3_the_boot_worker_points_all_three_temp_variables_at_the_scratch_dir():
    """The mechanism half, asserted in the source rather than only in its effect: a
    redirect that named two of the three variables would pass the byte comparison on a box
    where the third happens to be unset, and fail silently on the box where it is not."""
    source = _doctor_source("_worker")
    body = source.split("def worker_boot(")[1].split("\ndef ")[0]
    for name in ("TEMP", "TMP", "TMPDIR"):
        assert '"%s"' % name in body, (
            "the boot worker does not redirect %s for its grandchild" % name)
    assert "shutil.rmtree" in body, "the boot worker does not remove its scratch directory"


def test_e5_reference_data_files_are_byte_identical_after_a_default_run():
    """E-5, and honestly PARTIAL: it can only cover the data files that EXIST here.

    The full eight-file proof is live gate L-7 and is claimed nowhere else.  The test
    records what it actually covered, so a green run over three files cannot be read as a
    green run over eight."""
    import hashlib

    paths = _worker._plane_paths(None)
    covered = {}
    for plane, path in sorted(paths.items()):
        if path and os.path.isfile(path):
            stat = os.stat(path)
            with open(path, "rb") as handle:
                covered[plane] = (stat.st_size, stat.st_mtime_ns,
                                  hashlib.sha256(handle.read()).hexdigest())
    if not covered:
        pytest.skip("no reference data file exists in this environment")
    _run_doctor(["--format", "ndjson"])
    for plane, expected in sorted(covered.items()):
        path = paths[plane]
        stat = os.stat(path)
        with open(path, "rb") as handle:
            actual = (stat.st_size, stat.st_mtime_ns,
                      hashlib.sha256(handle.read()).hexdigest())
        assert actual == expected, "%s changed during a read-only run" % plane
    assert set(covered) <= set(PLANE_NAMES), covered
    print("E-5 covered %d of %d planes: %s"
          % (len(covered), len(PLANE_NAMES), sorted(covered)))


def test_e4_the_tool_manifest_is_built_without_reaching_the_backend():
    """E-4.  ``is_open`` false and no backend pulled in BY THIS STEP.

    The raw "clr is absent from this process" reading would be false for an innocent
    reason — the pythonnet dependency probe imports it a few observations earlier — so the
    fact is reported as both, and the invariant is the one that means what it says."""
    records, completed = _run_worker("pkg")
    manifest = records.get("harness.manifest")
    if manifest is None or manifest.get("ok") is not True:
        pytest.skip("the harness manifest did not build here: %s" % completed.stderr[-400:])
    assert manifest["is_open"] is False
    assert manifest["clr_loaded"] is False, (
        "building the tool manifest pulled in the .NET backend")


# ===========================================================================
# The CLI contract
# ===========================================================================
def test_an_unknown_flag_is_a_usage_error_on_stderr_and_exits_64():
    completed = _run_doctor(["--wat"])
    assert completed.returncode == EXIT_USAGE
    assert completed.stdout == ""
    assert "usage:" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_an_unknown_format_is_a_usage_error():
    completed = _run_doctor(["--format", "yaml"])
    assert completed.returncode == EXIT_USAGE
    assert "yaml" in completed.stderr


@pytest.mark.parametrize("argv,expected", [
    ([], ("human", False, False)),
    (["--format", "ndjson"], ("ndjson", False, False)),
    (["--format=ndjson"], ("ndjson", False, False)),
    (["--engine"], ("human", True, False)),
    (["--boot"], ("human", False, True)),
    (["--boot", "--engine", "--format", "ndjson"], ("ndjson", True, True)),
])
def test_the_command_line_parses_to_the_documented_shape(argv, expected):
    args = _runner.parse_args(argv)
    assert (args.format, args.engine, args.boot) == expected


def test_the_process_exit_code_is_the_summarys_on_a_real_run():
    completed = _run_doctor(["--format", "ndjson"])
    summary = [record for record in _ndjson_of(completed)
               if record["type"] == "summary"][-1]
    assert completed.returncode == summary["exit_code"]


def test_a_real_run_covers_the_whole_spine_exactly_once():
    completed = _run_doctor(["--format", "ndjson"])
    seen = [record["check"] for record in _ndjson_of(completed)
            if record["type"] == "finding"]
    for check in BASE_CHECKS:
        assert seen.count(check) == 1, "%s appeared %d times" % (check, seen.count(check))


def test_no_check_is_reported_twice_in_a_real_run():
    """Not only the spine: a DERIVED name must appear once too.

    The derived dependency findings belong to the stage that owns their family.  Without
    that ownership every later stage re-emits them, and a reader sees the same finding four
    times — a report that repeats itself is one a reader stops believing."""
    completed = _run_doctor(["--format", "ndjson"])
    seen = [record["check"] for record in _ndjson_of(completed)
            if record["type"] == "finding"]
    repeated = sorted({check for check in seen if seen.count(check) > 1})
    assert repeated == [], "these findings were reported more than once: %r" % (repeated,)


def test_a_real_run_never_prints_a_traceback():
    completed = _run_doctor([])
    assert "Traceback (most recent call last)" not in completed.stdout
    assert "Traceback (most recent call last)" not in completed.stderr


def test_a_real_run_never_claims_the_corpus_is_not_built_on_this_machine():
    """D-14 end to end: the tool's own refusal text is FALSE on a machine where the corpus
    is built, and doctor must not launder it into its own report."""
    completed = _run_doctor(["--format", "ndjson"])
    assert FORBIDDEN_CLAIM not in completed.stdout
    for record in _ndjson_of(completed):
        for key in ("summary", "reason", "remedy"):
            assert FORBIDDEN_CLAIM not in str(record.get(key, ""))


def test_every_skip_in_a_real_run_names_a_blocker_that_is_itself_not_passing():
    """The SKIP invariant, asserted on the real machine rather than only on fixtures: this
    is the guard that stops "skip" becoming a place to put anything inconvenient."""
    completed = _run_doctor(["--format", "ndjson"])
    findings = _findings_of(_ndjson_of(completed))
    for check, record in sorted(findings.items()):
        if record["status"] != "skip":
            continue
        blocker = record["blocked_by"]
        assert blocker, "%s is a SKIP naming nothing" % check
        if blocker == NOT_REQUESTED:
            continue
        assert blocker in findings, "%s names an absent blocker %r" % (check, blocker)
        assert findings[blocker]["status"] in ("fail", "warn"), (
            "%s is blocked by %s, which is %s" % (check, blocker,
                                                  findings[blocker]["status"]))
