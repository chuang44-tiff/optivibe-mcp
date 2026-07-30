"""The parent: spawn, stream, classify, render, exit.

**Rule 1 — the parent is stdlib-only AND does no blocking filesystem I/O.**  Not one
metadata read, not one directory listing, not one ``os.path`` probe of a user path.  On
Windows a stalled UNC entry on ``sys.path`` or a network-mapped working directory makes any
of those block, and they would block *before any killable worker exists*.  Every
observation — including the environment — is made in a bounded child.  The parent's only
syscalls are process spawn, bounded pipe reads, kill, and writes to its own stdout.

**Rule 2 — workers emit facts; the parent alone assigns a status.**  This module is where
that happens: ``evaluate`` crosses the observation map against the pinned contract and
produces findings.  A worker cannot hand-write a verdict, because ``Observation`` has no
field to write it into.

**Rule 4 — children stream, the parent reconciles.**  A worker that dies mid-stream loses
nothing it already said; the parent synthesises ``UNKNOWN`` for every spine identity that
worker owed and did not deliver.  ``UNKNOWN`` is always a statement about doctor, and it is
always exit 3 — never a health claim.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

from ._classify import (
    EXIT_BY_STATE, EXIT_INTERRUPT, EXIT_USAGE, classify_boot, classify_coverage,
    classify_dep, classify_enrichment, classify_mcp_range, classify_plane,
    classify_pythonnet_runtime, classify_tool, classify_version, coverage_members,
    exit_code, remedy_for, summarize)
from ._contract import (
    BASE_CHECKS, CRITICAL_DEPS, DISTRIBUTIONS, PLANES, REFERENCE_CASES,
    normalise_dist_name)
from ._model import NOT_REQUESTED, Finding, State, Status
from ._render import make_renderer

# ---------------------------------------------------------------------------
# Deadlines.  Every one of them is a kill, not a timeout flag on a thread: a wedged .NET
# P/Invoke cannot be interrupted from Python, and only the operating system can end it.
# ---------------------------------------------------------------------------
BASE_DEADLINE_S = 10.0
PKG_DEADLINE_S = 60.0
BOOT_DEADLINE_S = 30.0
ENGINE_PREFLIGHT_DEADLINE_S = 30.0
ENGINE_OVERALL_DEADLINE_S = 90.0
#: The sixth worker mode.  Spawned ONLY after an --engine overall-deadline kill that saw an
#: engine.owned record — never part of a default run.
REAP_DEADLINE_S = 15.0
DEFAULT_ZEMAX_DEADLINE_S = 30.0

#: Doctor's own band for the ``zemax`` child's kill deadline.  The operator's
#: ``OPTIVIBE_CONNECT_TIMEOUT_S`` is *read* but never *obeyed unconditionally*, because both
#: ends of its range destroy the diagnostic:
#:
#: * below the floor the child is killed before the interpreter has loaded the .NET
#:   assemblies, so ``zemax.nethelper`` and ``zemax.dir`` go UNKNOWN and a **healthy machine
#:   exits 3** — the cry-wolf direction.  A sub-second value is a plausible setting for an
#:   operator debugging the very hang doctor exists to diagnose;
#: * above the ceiling there is no bound at all.  ``OPTIVIBE_CONNECT_TIMEOUT_S=31536000``
#:   makes doctor wait a year, which contradicts this module's own opening rule that every
#:   deadline is a kill rather than a suggestion.
#:
#: A configured value *inside* the band is honoured exactly, so the knob still works.
ZEMAX_DEADLINE_FLOOR_S = 10.0
ZEMAX_DEADLINE_CEILING_S = 300.0

#: The internal terminal record a spawn yields last.  Never a spine id, never rendered.
OUTCOME = "_outcome"

#: Which worker owes which spine identities.  A worker that dies makes exactly these
#: UNKNOWN — which is why ownership is single-valued and asserted against BASE_CHECKS.
WORKER_CHECKS = {
    "base": ("env.python", "env.cwd_shadow", "env.pythonnet_runtime",
             "version.optivibe-harness", "version.optivibe-reference"),
    "pkg": ("dependency.mcp", "dependency.psutil", "dependency.pythonnet",
            "dependency.mcp_range", "dependency.coverage", "reference.import",
            "reference.dispatcher",
            "plane.merit_operand", "plane.tolerance_operand", "plane.glass",
            "plane.manual", "plane.coverage", "enrichment.merit", "enrichment.tolerance",
            "tool.lookup_operand", "tool.lookup_glass", "tool.search_reference",
            "tool.find_glasses", "tool.find_glass_pair", "tool.coverage",
            "harness.import", "harness.manifest", "composed.manifest", "mcp.construct"),
    "zemax": ("zemax.nethelper", "zemax.dir"),
    "boot": ("boot.exit",),
    "engine": ("engine.license", "engine.cleanup"),
}

#: The human sentence a flag-gated check carries when its flag was not passed.  The
#: renderer prints a bare ``blocked by <id>`` for a prerequisite SKIP; a not-requested SKIP
#: has to say what to pass instead, or the reader learns nothing from it.
NOT_REQUESTED_SUMMARY = {
    "boot.exit": "not requested; pass --boot to start the real server in a throwaway "
                 "temp directory",
    "engine.license": "not requested; pass --engine to open a session and prove the licence",
    "engine.cleanup": "not requested; pass --engine to open a session and prove the engine "
                      "it opened was reclaimed",
}

#: The pinned summary a non-PASS ``zemax.dir`` always carries, and the key it must never
#: carry.  Doctor reports what it resolved *in this process* or it reports nothing: a
#: placeholder mechanism is a fabricated answer wearing a real answer's clothes.
UNRESOLVED_SUMMARY = "not resolved in this process"


# ---------------------------------------------------------------------------
# Output stream safety.
# ---------------------------------------------------------------------------
class _SafeStream:
    """A stdout proxy that cannot raise ``UnicodeEncodeError`` mid-report.

    The pinned remedies contain an em-dash.  ``cp437`` and ``cp850`` — the classic Windows
    console codepages, and the ones on the bare box doctor exists for — cannot encode it,
    so an unguarded write kills the parent *while it is reporting*.  A diagnostic that dies
    describing the problem is the worst available outcome, worse than the problem.

    Two layers, deliberately: ``reconfigure`` fixes the stream where the stream supports
    it, and this proxy carries the case where it does not.  The fallback re-encodes with
    ``backslashreplace``, which stays ASCII and stays legible; a text stream encodes the
    whole string before it writes any of it, so a refused write has written nothing and the
    retry cannot double up.
    """

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        try:
            return self._stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self._stream, "encoding", None) or "ascii"
            try:
                safe = text.encode(encoding, "backslashreplace").decode(encoding, "replace")
            except BaseException:                                  # noqa: BLE001
                safe = text.encode("ascii", "backslashreplace").decode("ascii")
            return self._stream.write(safe)

    def flush(self):
        try:
            return self._stream.flush()
        except BaseException:                                      # noqa: BLE001
            return None

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _harden_stream(stream):
    """Return a stream that will not raise while rendering the report."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="backslashreplace")
        except BaseException:                                      # noqa: BLE001
            pass
    return _SafeStream(stream)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
FORMATS = ("human", "ndjson")
USAGE = "usage: python -m optivibe_doctor [--format {human,ndjson}] [--engine] [--boot]"


class _Usage(Exception):
    """A command line doctor will not act on.  Exit 64, and say why on stderr."""


class Args:
    """The parsed command line.  A plain object so a test can build one by hand."""

    __slots__ = ("format", "engine", "boot")

    def __init__(self, format="human", engine=False, boot=False):   # noqa: A002
        self.format = format
        self.engine = engine
        self.boot = boot


def parse_args(argv):
    """Parse the command line, or raise ``_Usage``.

    Hand-rolled rather than ``argparse`` for one reason: ``argparse`` exits 2 on an unknown
    flag, and the pinned usage code is 64.  A parser that owns its own exit code cannot be
    made to honour a contract it does not know about.
    """
    args = Args()
    rest = list(argv)
    while rest:
        item = rest.pop(0)
        if item == "--format":
            if not rest:
                raise _Usage("--format needs a value\n" + USAGE)
            value = rest.pop(0)
            if value not in FORMATS:
                raise _Usage("unknown format %r\n%s" % (value, USAGE))
            args.format = value
        elif item.startswith("--format="):
            value = item.split("=", 1)[1]
            if value not in FORMATS:
                raise _Usage("unknown format %r\n%s" % (value, USAGE))
            args.format = value
        elif item == "--engine":
            args.engine = True
        elif item == "--boot":
            args.boot = True
        else:
            raise _Usage("unknown argument %r\n%s" % (item, USAGE))
    return args


# ---------------------------------------------------------------------------
# Spawning.
# ---------------------------------------------------------------------------
def _zemax_deadline_s():
    """The ``zemax`` child's kill deadline, from production's own connect-timeout knob.

    **Clamped to doctor's own band**, never returned raw.  Rejecting only the obviously
    wrong values (nan, inf, non-numeric) and passing everything else through leaves the two
    settings that actually break the diagnostic — a sub-second deadline that kills a healthy
    child before it reports, and a year-long one that is not a bound at all.  See
    ``ZEMAX_DEADLINE_FLOOR_S`` / ``ZEMAX_DEADLINE_CEILING_S``.
    """
    raw = os.environ.get("OPTIVIBE_CONNECT_TIMEOUT_S")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ZEMAX_DEADLINE_S
    if value != value or value in (float("inf"), float("-inf")):
        return DEFAULT_ZEMAX_DEADLINE_S
    return min(max(value, ZEMAX_DEADLINE_FLOOR_S), ZEMAX_DEADLINE_CEILING_S)


#: How much of a child's stderr the parent keeps.  Bounded on purpose: a child that spews
#: is exactly the child this drain exists for, and an unbounded buffer would trade a hang
#: for a memory exhaustion.
STDERR_TAIL_CHARS = 2000
_STDERR_CHUNK = 4096


def _reader(stream, sink):
    """Drain a child's stdout into a queue, one line at a time, then signal EOF."""
    try:
        for line in stream:
            sink.put(("line", line))
    except BaseException as exc:                                   # noqa: BLE001
        sink.put(("error", repr(exc)[:240]))
    finally:
        sink.put(("eof", None))


def _drain(stream, box):
    """Consume a child's stderr to EOF, keeping only its tail.

    **The tail is the bonus; not blocking is the point.**  A pipe nobody reads fills — 8192
    bytes on Windows — and the child's next ``write()`` to fd 2 blocks *forever*.  It then
    never reaches its remaining ``emit()`` calls, is killed at its deadline, and every spine
    id it owed becomes UNKNOWN: a healthy machine exits 3 after burning the full deadline.
    This project has already diagnosed exactly this mechanism in its own MCP server
   , and the ``zemax`` and ``engine`` children are precisely the code that
    drives the .NET/FRU layer documented to write below Python straight to fd 2.

    ``DEVNULL`` would also have unblocked the pipe.  Draining instead is strictly better,
    because the bytes a dying child writes to stderr are frequently the ONLY explanation
    that exists: a worker that fails during *module import* never reaches its own
    exception handler and emits no observation at all, so without this the reader is told
    "the base worker died before reporting this" and nothing whatsoever about why.
    """
    try:
        while True:
            chunk = stream.read(_STDERR_CHUNK)
            if not chunk:
                return
            box.append(chunk)
            if len(box) > 1:
                joined = "".join(box)
                box[:] = [joined[-STDERR_TAIL_CHARS:]]
    except BaseException:                                          # noqa: BLE001
        # The drain must never be able to raise into the interpreter's thread hook: this
        # thread exists to keep the parent alive, so it cannot become a way to kill it.
        return


def _tail_of(box):
    """Return the collapsed, bounded stderr tail a drained child left behind, or ``""``."""
    text = " ".join("".join(box).split())
    return text[-STDERR_TAIL_CHARS:]


def _spawn_worker(name, deadline_s, env_extra=None, *, extend_on=None, extend_to=None):
    """Yield every ``Observation`` a worker child emits, killing it at its deadline.

    ``extend_on``/``extend_to`` implement the engine worker's two deadlines: the preflight
    bound applies until the named record arrives, and the overall bound applies from the
    start and is **never relinquished**.  Permanently handing over the kill deadline after
    preflight was the finding — a licence read that wedges after the handover hangs doctor
    with no remaining bound at all.

    The last thing yielded is always the internal ``_outcome`` record, so the caller can
    tell a completed worker from a killed one without inspecting the process.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    started = time.monotonic()
    overall_end = started + float(extend_to if extend_to else deadline_s)
    stage_end = started + float(deadline_s)
    try:
        child = subprocess.Popen(
            [sys.executable, "-m", "optivibe_doctor._worker", name],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", errors="replace", bufsize=1)
    except BaseException as exc:                                   # noqa: BLE001
        yield _outcome("crashed", detail="could not start the worker: %s" % (exc,))
        return

    sink = queue.Queue()
    pump = threading.Thread(target=_reader, args=(child.stdout, sink), daemon=True)
    pump.start()
    # The second thread is not optional hygiene — see ``_drain``.  Without it the child
    # blocks on a full stderr pipe and its whole report is lost.
    stderr_box = []
    drain = threading.Thread(target=_drain, args=(child.stderr, stderr_box), daemon=True)
    drain.start()
    status, detail = "completed", ""
    try:
        while True:
            remaining = min(stage_end, overall_end) - time.monotonic()
            if remaining <= 0:
                status = "timeout"
                detail = "the %s worker exceeded its deadline" % name
                break
            try:
                kind, payload = sink.get(timeout=remaining)
            except queue.Empty:
                status = "timeout"
                detail = "the %s worker exceeded its deadline" % name
                break
            if kind == "eof":
                break
            if kind == "error":
                status = "crashed"
                detail = payload
                break
            observation = _parse_line(payload)
            if observation is None:
                status = "malformed"
                detail = "the %s worker emitted a line that is not an observation" % name
                continue
            if extend_on and observation.check == extend_on:
                stage_end = overall_end
            yield observation
    finally:
        if child.poll() is None:
            try:
                child.kill()
            except BaseException:                                  # noqa: BLE001
                pass
        try:
            child.wait(timeout=5)
        except BaseException:                                      # noqa: BLE001
            pass
        # Join BEFORE closing: closing the handle out from under a reading thread makes it
        # raise, and the tail it was collecting is the evidence this whole path exists for.
        try:
            drain.join(2.0)
        except BaseException:                                      # noqa: BLE001
            pass
        for handle in (child.stdout, child.stderr):
            try:
                if handle is not None:
                    handle.close()
            except BaseException:                                  # noqa: BLE001
                pass
    returncode = child.returncode
    if status == "completed" and returncode not in (0, None):
        status = "crashed"
        detail = "the %s worker exited %s" % (name, returncode)
    yield _outcome(status, returncode=returncode, detail=detail,
                   stderr_tail=_tail_of(stderr_box))


def _outcome(status, returncode=None, detail="", stderr_tail=""):
    from ._model import Observation
    return Observation(OUTCOME, {"status": status, "returncode": returncode,
                                 "detail": detail, "stderr_tail": stderr_tail})


def _parse_line(line):
    """Turn one child stdout line into an ``Observation``, or ``None`` if it is not one."""
    from ._model import Observation

    text = (line or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except BaseException:                                          # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    check = payload.get("check")
    facts = payload.get("facts")
    if not isinstance(check, str) or not isinstance(facts, dict):
        return None
    # Rule 2, enforced at the boundary: a worker may not hand back a verdict.
    facts.pop("status", None)
    return Observation(check, facts)


# ---------------------------------------------------------------------------
# Classification helpers.  Every one of them reads facts and returns a verdict; none of
# them touches the filesystem, the network or the clock.
# ---------------------------------------------------------------------------
def _facts(observations, check):
    observation = observations.get(check)
    return dict(observation.facts) if observation is not None else None


def _tri(value):
    """Coerce a JSON-carried tri-state to True / False / None without widening it.

    Determinate is TYPE IDENTITY, not equality: ``value in (True, False)`` also admits
    ``0``, ``0.0`` and ``1`` (``0 == False``), which would return a non-bool reading
    verbatim and bypass the grader's UNKNOWN branch.  Only the genuine ``bool`` singletons
    survive; JSON carries ``true``/``false`` as real bools, so nothing legitimate is lost.
    """
    return value if type(value) is bool else None


def _env_python(facts):
    return (Status.PASS, "", "%s  %s  sys.path[0]=%r" % (
        facts.get("version"), facts.get("executable"), facts.get("path0") or ""))


def _env_cwd_shadow(facts):
    shadowed = facts.get("shadowed")
    if shadowed is None:
        return (Status.UNKNOWN, "cwd_unreadable", "the working directory could not be read")
    if shadowed:
        return (Status.FAIL, "stdlib_shadowed",
                "%s shadows the standard library from %s"
                % (", ".join(shadowed), facts.get("cwd")))
    return (Status.PASS, "", "no standard-library name is shadowed by %s" % (facts.get("cwd"),))


def _summary_version(facts):
    parts = []
    for label, key in (("installed", "meta"), ("source", "source"), ("manifest", "manifest")):
        parts.append("%s %s" % (label, facts.get(key)))
    return "%s (%s)" % (facts.get("meta") or facts.get("source"), " | ".join(parts))


def _import_finding(facts, subject):
    if facts.get("ok") is True:
        return (Status.PASS, "", "%s imports" % subject)
    if facts.get("ok") is False:
        return (Status.FAIL, "import_failed", "%s did not import: %s"
                % (subject, facts.get("error")))
    return (Status.UNKNOWN, "not_measured", "%s was not measured" % subject)


def _plane_summary(facts, status, reason):
    if status is Status.PASS:
        return "present, opens, wired and identity-checked  %s" % (facts.get("path"),)
    if reason == "absent_expected":
        return "no data file at %s" % (facts.get("path"),)
    if reason == "corrupt":
        return "present (%s bytes) but the opener refused it" % (facts.get("bytes"),)
    if reason == "unwired":
        return ("present and opens but the dispatcher did not wire it (%s is None)"
                % (facts.get("conn_attr"),))
    if reason == "misrouted":
        return ("wired to another plane's data: %s resolves to %s"
                % (facts.get("conn_attr"), facts.get("wired_path")))
    return "%s could not be established for %s" % (reason, facts.get("path"))


def _coverage_finding(check, kind, pinned, universe, probed, blocked_by, facts):
    """Grade one ``*.coverage`` check through the one shared classifier.

    The single call site.  Three inline copies of the coverage rule would be three rules
    that can drift, so ``classify_coverage`` is reached from here and nowhere else, and no
    caller re-derives the pin-versus-universe relation for itself.
    """
    status, reason = classify_coverage(kind, pinned, universe, probed, blocked_by)
    summary = _coverage_summary(kind, status, reason, pinned, universe, probed)
    return Finding(check, status, reason, summary, facts,
                   remedy_for(check, status, reason),
                   blocked_by if status is Status.SKIP else "")


def _coverage_summary(kind, status, reason, pinned, universe, probed):
    """Name the members the verdict is about, through the verdict's own difference.

    The members come from ``coverage_members``, which shares ``classify_coverage``'s one
    set-difference.  A message that recomputed the relation for itself could disagree with
    the verdict it accompanies, and a message that contradicts its own verdict is worse
    than no message at all.
    """
    if status is Status.PASS:
        # "accounted for in", not "present in".  This is an IDENTITY claim — every pinned
        # case is a case the derived universe knows about — and says nothing about whether
        # that case's data exists.  Read as presence it contradicts the line above it: on a
        # fresh install ``plane.manual`` WARNs "no data file at ..." and this PASSes
        # immediately after, and a reader who takes "present" literally reads the PASS as
        # denying the WARN.  A message that appears to contradict its neighbour is worse
        # than no message: it teaches the reader that the tool disagrees with itself.
        return "every pinned %s is accounted for in the derived universe" % kind
    if status is Status.SKIP:
        return ""
    if universe is None or pinned is None or probed is None:
        return "the %s universe could not be established" % kind
    members = ", ".join(coverage_members(kind, pinned, universe, probed, reason))
    if reason == "probe_incomplete":
        return "not every derived %s was probed: %s" % (kind, members)
    if reason == "coverage_kind_unknown":
        return "no coverage rule is declared for %r" % (kind,)
    if status is Status.FAIL:
        return "pinned %s absent from the derived universe: %s" % (kind, members)
    return "derived %s with no pinned case: %s" % (kind, members)


# ---------------------------------------------------------------------------
# evaluate — the whole crossing.
# ---------------------------------------------------------------------------
def evaluate(observations, *, checks=BASE_CHECKS, unknown=None, requested=()):
    """Turn an observation map into findings for exactly ``checks``, in spine order.

    ``unknown`` maps a spine id to the reason doctor could not measure it — a dead worker,
    a blown deadline, a malformed line.  Nothing else may produce ``UNKNOWN``: it is
    always a statement about doctor, never about the target.
    """
    observations = dict(observations or {})
    unknown = dict(unknown or {})
    requested = set(requested)
    findings = []
    by_check = {}

    def add(finding):
        findings.append(finding)
        by_check.setdefault(finding.check, finding)
        return finding

    checkout_present = _checkout_present(observations)

    for check in checks:
        # A definitive prerequisite outranks "the worker did not deliver it".  When the
        # reference package does not import, the pkg worker RIGHTLY stops probing planes —
        # so their absence from the stream is a statement about the target's configuration
        # (SKIP), not about doctor's ability to measure (UNKNOWN).  Consulting the unknown
        # map first would convert every honest declined probe into a false exit 3.
        blocked = _prerequisite_blocker(check, by_check)
        if blocked:
            add(_skip(check, blocked))
            continue
        if check in unknown:
            # The cause is attached HERE, not only where ``unknown`` was built, so that a
            # caller who synthesised the map from ownership alone still gets it.  An
            # UNKNOWN line that does not say why is the silent failure doctor exists for,
            # reproduced inside doctor.
            reason, detail = unknown[check][0], unknown[check][1]
            facts = _facts(observations, check) or {}
            cause = _worker_cause(OWNER_OF_CHECK.get(check), observations)
            if cause:
                facts = dict(facts, worker_fault=cause)
                if cause not in detail:
                    detail = "%s: %s" % (detail, cause)
            add(Finding(check, Status.UNKNOWN, reason, detail, facts))
            continue
        add(_grade(check, observations, by_check, checkout_present, requested, unknown))

    findings.extend(_extras(checks, observations, by_check, checkout_present))
    return findings


def _checkout_present(observations):
    """True when any distribution's packaging manifest was locatable.

    This is the *only* thing that switches the missing-vendor-data remedy.  The build
    scripts live outside ``src/`` and ship in neither wheel, so on a checkout-less install
    the repository-relative commands do not exist on disk, and a remedy the reader cannot
    run is a diagnostic failure rather than a formatting detail.
    """
    for dist in DISTRIBUTIONS:
        facts = _facts(observations, "version." + dist)
        if facts and facts.get("manifest") is not None:
            return True
    return False


def _blocker(by_check, check):
    """Return ``check`` when it is a definitive blocker in this run, else ``''``.

    A SKIP may only name a finding that itself failed or warned.  Naming a PASS, or naming
    nothing, is how "skip" quietly becomes a place to put anything inconvenient while the
    run still reports green.
    """
    finding = by_check.get(check)
    if finding is not None and finding.status in (Status.FAIL, Status.WARN):
        return check
    return ""


def _first_blocker(by_check, *checks):
    """The first of ``checks`` that is a definitive blocker in this run, else ``''``.

    Order is significance, not convenience: the earliest listed cause is the one a reader
    should act on, and naming the later one would send them to a symptom.
    """
    for check in checks:
        found = _blocker(by_check, check)
        if found:
            return found
    return ""


def _blocker_anywhere(observations, by_check, check, checkout_present):
    """``_blocker`` freed from grading ORDER.

    ``_blocker`` can only see ids the spine has already graded.  A check whose inputs sit
    LATER in ``BASE_CHECKS`` therefore reads no blocker at all, however definitive the
    later id's defect is — the same order trap ``_dependency_coverage`` documents, and the
    reason ``tool.coverage`` reported UNKNOWN "universe_unreadable" for an install whose
    harness import doctor had itself already failed.

    The verdict is never re-derived here.  An already-graded finding is used as it stands;
    an ungraded one is settled by the check's OWN handler on its OWN observation, so there
    is exactly one rule and a second copy cannot drift from it.  The handlers reachable
    this way are pure functions of ``facts``, so grading one twice costs nothing.

    No observation, or no handler, returns ``''`` on purpose: that is doctor failing to
    measure, which must stay UNKNOWN rather than become a SKIP charged to the target.
    """
    found = _blocker(by_check, check)
    if found or check in by_check:
        return found
    facts = _facts(observations, check)
    handler = _HANDLERS.get(check)
    if facts is None or handler is None:
        return ""
    finding = handler(check, facts, observations, by_check, checkout_present)
    return check if finding.status in (Status.FAIL, Status.WARN) else ""


def _first_blocker_anywhere(observations, by_check, checkout_present, checks):
    """``_first_blocker`` over ``_blocker_anywhere``; ``checks`` stays most-upstream-first."""
    for check in checks:
        found = _blocker_anywhere(observations, by_check, check, checkout_present)
        if found:
            return found
    return ""


def _skip(check, blocked_by, summary=""):
    return Finding(check, Status.SKIP, "blocked", summary, {}, "", blocked_by)


#: check-prefix -> the finding whose failure makes it unattemptable.  A SKIP produced here
#: is a statement about the TARGET's configuration; it never gates.
_PREREQUISITE = (
    (("reference.dispatcher", "plane.", "tool.", "enrichment."), "reference.import"),
    # A door cannot be called through a Dispatcher that does not exist.  The PLANES are
    # deliberately absent from this row: presence, size and openability are still real,
    # measurable facts about the target's data even with no dispatcher, and reporting them
    # SKIP would hide a corrupt catalog behind an unrelated failure.
    (("tool.",), "reference.dispatcher"),
    (("harness.manifest", "composed.manifest", "mcp.construct"), "harness.import"),
)

#: The ids that are themselves blockers, and so can never be blocked by the table above.
_NEVER_BLOCKED = ("reference.import", "harness.import")


def _prerequisite_blocker(check, by_check):
    """Return the definitive blocker that makes ``check`` unattemptable, or ``''``.

    Everything downstream of an import or a Dispatcher construction is a probe that could
    not have been run, not a probe that failed.  The coverage checks carry their blocker
    into the classifier instead, so they are excluded here.

    **Every** matching row is consulted, not merely the first.  Returning the first row's
    answer would mean that once ``reference.import`` passed, a tool could never be reported
    as blocked by anything else — the later rows would be structurally dead.
    """
    if check in DERIVED_CHECKS or check in _NEVER_BLOCKED:
        return ""
    for prefixes, blocker in _PREREQUISITE:
        if check.startswith(prefixes):
            found = _blocker(by_check, blocker)
            if found:
                return found
    return ""


def _grade(check, observations, by_check, checkout_present, requested, unknown):
    """Produce the finding for one spine id."""
    facts = _facts(observations, check)

    if check in ("boot.exit", "engine.license", "engine.cleanup"):
        flag = "boot" if check == "boot.exit" else "engine"
        if flag not in requested:
            return Finding(check, Status.SKIP, NOT_REQUESTED,
                           NOT_REQUESTED_SUMMARY[check], {}, "", NOT_REQUESTED)

    blocked = _prerequisite_blocker(check, by_check)
    if blocked:
        return _skip(check, blocked)

    if check == "dependency.coverage":
        return _dependency_coverage(observations, by_check, checkout_present)
    if check == "plane.coverage":
        return _plane_coverage(observations, by_check)
    if check == "tool.coverage":
        return _tool_coverage(observations, by_check, checkout_present)

    if facts is None:
        return Finding(check, Status.UNKNOWN, "not_emitted",
                       "no worker reported this check", {})

    handler = _HANDLERS.get(check)
    if handler is None:
        for prefix, prefixed in _PREFIXED:
            if check.startswith(prefix):
                handler = prefixed
                break
    if handler is None:
        return Finding(check, Status.UNKNOWN, "ungraded",
                       "no rule grades this check", facts)
    return handler(check, facts, observations, by_check, checkout_present)


def _finish(check, status, reason, summary, facts, checkout_present=True, blocked_by=""):
    return Finding(check, status, reason, summary, facts,
                   remedy_for(check, status, reason, checkout_present), blocked_by)


# -- individual graders ------------------------------------------------------
def _grade_env_python(check, facts, *_rest):
    status, reason, summary = _env_python(facts)
    return _finish(check, status, reason, summary, facts)


def _grade_cwd_shadow(check, facts, *_rest):
    status, reason, summary = _env_cwd_shadow(facts)
    return _finish(check, status, reason, summary, facts)


def _grade_runtime(check, facts, *_rest):
    value = facts.get("value")
    status, reason = classify_pythonnet_runtime(value)
    summary = ("PYTHONNET_RUNTIME is unset" if value is None
               else "PYTHONNET_RUNTIME=%s" % (value,))
    return _finish(check, status, reason, summary, facts)


def _grade_version(check, facts, *_rest):
    status, reason = classify_version(
        facts.get("meta"), facts.get("source"), facts.get("manifest"), facts.get("origin"))
    return _finish(check, status, reason, _summary_version(facts), facts)


def _grade_dependency(check, facts, *_rest):
    name = check.split(".", 1)[1]
    status, reason = classify_dep(
        name, facts.get("declared"), facts.get("meta_version"),
        _tri(facts.get("import_ok")), facts.get("import_error"))
    if status is Status.PASS:
        summary = "%s %s imports" % (name, facts.get("meta_version"))
    elif reason == "declared_but_unimportable":
        summary = ("%s is declared and its metadata reports %s, but importing %r raised %s"
                   % (name, facts.get("meta_version"), facts.get("import_name"),
                      facts.get("import_error")))
    elif reason == "absent":
        summary = "%s is declared but is not installed" % name
    else:
        summary = "%s: %s" % (name, reason)
    return _finish(check, status, reason, summary, facts)


def _grade_mcp_range(check, facts, *_rest):
    version = facts.get("version")
    status, reason = classify_mcp_range(version)
    if status is Status.PASS:
        summary = "mcp %s is inside the supported range >=1.28,<2" % version
    elif reason == "unsupported_range":
        summary = "mcp %s is outside the supported range >=1.28,<2" % version
    else:
        summary = "the installed mcp version could not be established"
    return _finish(check, status, reason, summary, facts)


def _grade_reference_import(check, facts, *_rest):
    status, reason, summary = _import_finding(facts, "optivibe_reference")
    return _finish(check, status, reason, summary, facts)


def _grade_reference_dispatcher(check, facts, *_rest):
    """Grade the CONSTRUCTION of the reference Dispatcher — a claim its import does not make.

    Separate from ``reference.import`` because the two fail independently: a package can
    import perfectly and still raise when its Dispatcher is built (a missing table, a
    broken ``sqlite3``, an ``__init__`` that opens a file).  Without this id that exception
    is caught, recorded, and then never rendered, while every plane and every door reads
    UNKNOWN — the run says "doctor could not measure" about a defect doctor measured
    exactly.
    """
    if facts.get("ok") is True:
        return _finish(check, Status.PASS, "", "the reference Dispatcher constructs", facts)
    if facts.get("ok") is False:
        return _finish(check, Status.FAIL, "dispatcher_unbuildable",
                       "the reference Dispatcher could not be constructed: %s: %s"
                       % (facts.get("error_type"), facts.get("error")), facts)
    return _finish(check, Status.UNKNOWN, "not_measured",
                   "the reference Dispatcher was not measured", facts)


def _grade_plane(check, facts, *_rest_args):
    checkout_present = _rest_args[2] if len(_rest_args) > 2 else True
    status, reason = classify_plane(
        _tri(facts.get("present")), _tri(facts.get("opens")),
        _tri(facts.get("wired")), _tri(facts.get("identity_ok")))
    dispatcher_error = facts.get("dispatcher_error")
    if dispatcher_error and reason == "unwired":
        # Same verdict, different cause, different fix.  The pinned ``unwired`` remedy says
        # "the corpus is present but this release does not wire it — upgrade", which is
        # actively wrong when the Dispatcher cannot be built at all, and a remedy that
        # misdirects is worse than no remedy.
        return Finding(check, status, "dispatcher_unbuildable",
                       "the reference Dispatcher could not be constructed, so nothing is "
                       "wired: %s" % (dispatcher_error,), facts, "", "")
    return _finish(check, status, reason, _plane_summary(facts, status, reason), facts,
                   checkout_present)


def _grade_enrichment(check, facts, observations, by_check, checkout_present):
    """Grade one build tier, with the plane boundary its only source carries.

    The tier's ONLY source is that domain's own catalog JSON, so an absent catalog means
    there is no report to read — a fact about the TARGET's configuration, already reported
    on the plane check, and therefore SKIP naming it.  Grading it UNKNOWN instead would make
    every fresh public install exit 3: the baked operand catalogs are user-built and are not
    published, so both enrichment checks would read "doctor could not measure" on the most
    common install in the world while the contract says that install is DEGRADED/1.

    Present-but-unreadable is the other half and stays UNKNOWN: the file is there, doctor
    tried to read the tier out of it and could not.  The two are deliberately disjoint —
    keyed on the plane being ABSENT, never merely on the tier being unreadable.
    """
    del observations, checkout_present
    plane = _enrichment_plane_of(check)
    plane_finding = by_check.get("plane." + plane) if plane else None
    if (plane_finding is not None and plane_finding.status is Status.WARN
            and plane_finding.reason == "absent_expected"):
        return _skip(check, "plane." + plane,
                     "no build report to read: the %s catalog is absent" % plane)
    tier = facts.get("tier")
    status, reason = classify_enrichment(tier)
    summary = ("build tier %s" % tier if tier
               else "the build report did not name a tier")
    return _finish(check, status, reason, summary, facts)


def _enrichment_plane_of(check):
    from ._contract import ENRICHMENT_PLANE
    return ENRICHMENT_PLANE.get(check.split(".", 1)[1])


def _grade_tool(check, facts, observations, by_check, checkout_present):
    door = check.split(".", 1)[1]
    plane = _plane_of(door)
    plane_finding = by_check.get("plane." + plane) if plane else None
    plane_status = plane_finding.status if plane_finding else None
    plane_reason = plane_finding.reason if plane_finding else ""
    if facts.get("probe_input_underivable") is True:
        # Doctor could not FORM the question.  That is never the door's failure, so it is
        # never FAIL.  Which of the two honest answers it is follows the SKIP/UNKNOWN split
        # exactly: the plane's data is absent (a fact about the target's configuration, and
        # already reported on the plane check) -> SKIP naming that blocker; the plane is
        # fine and the derivation still yielded nothing, e.g. a valid but EMPTY catalog
        # -> UNKNOWN, because doctor failed to measure.
        blocked = _blocker(by_check, "plane." + plane) if plane else ""
        if blocked:
            return Finding(check, Status.SKIP, "blocked",
                           "%s was not called: its probe input could not be derived "
                           "from %s" % (door, blocked), facts, "", blocked)
        return Finding(check, Status.UNKNOWN, "probe_input_underivable",
                       "%s was not called: no probe input could be derived from the %s "
                       "plane's own data" % (door, facts.get("probe_input_plane")), facts)
    status, reason = classify_tool(
        door, _tri(facts.get("inner_ok")), facts.get("error_family"),
        plane_status, plane_reason)
    if status is Status.PASS:
        summary = "%s answered" % door
    elif reason == "plane_absent":
        summary = "%s refused %s while its plane is absent" % (door, facts.get("error_family"))
    elif reason == "present_but_unwired":
        summary = ("%s refused %s while its plane is present"
                   % (door, facts.get("error_family")))
    elif reason == "plane_misrouted":
        summary = "%s answered from a connection wired to another plane" % door
    else:
        summary = "%s: %s (%s)" % (door, reason, facts.get("error_family"))
    return _finish(check, status, reason, summary, facts, checkout_present)


def _plane_of(door):
    from ._contract import TOOL_PLANE
    return TOOL_PLANE.get(door)


def _grade_harness_import(check, facts, *_rest):
    status, reason, summary = _import_finding(facts, "optivibe_harness")
    return _finish(check, status, reason, summary, facts)


def _grade_harness_manifest(check, facts, *_rest):
    """Grade the tool manifest, and grade the word **cold** rather than printing it.

    Enumeration succeeding is only half the claim.  The finding's own sentence says the
    manifest "builds cold", so the two side-effect facts that make that sentence true —
    the session did not open and this construction did not load ``clr`` — are part of the
    verdict, not decoration.  Reporting PASS while printing ``engine open=True`` is a
    false green that states its own counter-evidence on the same line.
    """
    if facts.get("ok") is True:
        is_open = _tri(facts.get("is_open"))
        clr_loaded = _tri(facts.get("clr_loaded"))
        if is_open is None or clr_loaded is None:
            return _finish(check, Status.UNKNOWN, "cold_proof_unreadable",
                           "the tool manifest built, but whether it did so without opening "
                           "an engine could not be established (engine open=%s, clr "
                           "loaded=%s)" % (facts.get("is_open"), facts.get("clr_loaded")),
                           facts)
        if is_open or clr_loaded:
            return _finish(check, Status.FAIL, "manifest_not_cold",
                           "the tool manifest reached the backend while building (engine "
                           "open=%s, clr loaded=%s); building the manifest must not take "
                           "the seat" % (is_open, clr_loaded), facts)
        return _finish(check, Status.PASS, "",
                       "the tool manifest builds cold (engine open=%s, clr loaded=%s)"
                       % (is_open, clr_loaded), facts)
    if facts.get("ok") is False:
        return _finish(check, Status.FAIL, "manifest_failed",
                       "building the tool manifest raised %s: %s"
                       % (facts.get("error_type"), facts.get("error")), facts)
    return _finish(check, Status.UNKNOWN, "not_measured",
                   "the tool manifest was not measured", facts)


def _grade_composed_manifest(check, facts, *_rest):
    if facts.get("ok") is not True:
        return _finish(check, Status.FAIL, "composition_failed",
                       "composing the manifest raised %s: %s"
                       % (facts.get("error_type"), facts.get("error")), facts)
    names = set(facts.get("names") or ())
    missing = sorted(set(REFERENCE_CASES) - names)
    if missing:
        return _finish(check, Status.FAIL, "pinned_door_absent",
                       "the composed manifest does not advertise %s" % ", ".join(missing),
                       dict(facts, missing_doors=missing))
    return _finish(check, Status.PASS, "",
                   "the composed manifest advertises every pinned door", facts)


def _grade_mcp_construct(check, facts, *_rest):
    if facts.get("ok") is True:
        return _finish(check, Status.PASS, "", "the MCP server object constructs", facts)
    return _finish(check, Status.FAIL, "builder_raised",
                   "build_composite_mcp_server() raised %s: %s"
                   % (facts.get("error_type"), facts.get("error")),
                   facts)


def _grade_nethelper(check, facts, observations, by_check, checkout_present):
    if facts.get("harness_import") is False:
        blocked = _blocker(by_check, "harness.import")
        return _skip(check, blocked or "harness.import")
    if facts.get("found") is True:
        return _finish(check, Status.PASS, "",
                       "ZOSAPI_NetHelper.dll at %s" % facts.get("path"), facts)
    return _finish(check, Status.WARN, "absent", "ZOSAPI_NetHelper.dll not found", facts)


def _grade_zemax_dir(check, facts, observations, by_check, checkout_present):
    """Grade the install-directory resolution, and never fabricate a mechanism.

    Whenever this is not a PASS the summary is the pinned ``"not resolved in this
    process"`` and the facts carry **no** ``mechanism`` key — absent, not null, not
    ``"unknown"``.  A key that is present but empty invites a consumer to fill it in; an
    absent key cannot be misread.
    """
    carried = {key: value for key, value in facts.items() if key != "resolution"}

    if facts.get("harness_import") is False:
        blocked = _blocker(by_check, "harness.import") or "harness.import"
        return Finding(check, Status.SKIP, "blocked", UNRESOLVED_SUMMARY, carried,
                       "", blocked)
    if facts.get("nethelper") is False:
        blocked = _blocker(by_check, "zemax.nethelper") or "zemax.nethelper"
        return Finding(check, Status.SKIP, "blocked", UNRESOLVED_SUMMARY, carried,
                       "", blocked)
    if facts.get("loaded") is False:
        # A missing pythonnet surfaces as a raw ImportError with no error family at all —
        # never as one of the harness's own taxonomy tokens.  Keying on the taxonomy here
        # would misreport the commonest install on earth as a broken one.
        if str(facts.get("error_type")) in ("ImportError", "ModuleNotFoundError"):
            blocked = _blocker(by_check, "dependency.pythonnet") or "dependency.pythonnet"
            return Finding(check, Status.SKIP, "blocked", UNRESOLVED_SUMMARY, carried,
                           "", blocked)
        return Finding(check, Status.FAIL, "assemblies_failed", UNRESOLVED_SUMMARY,
                       carried, "", "")
    resolution = facts.get("resolution")
    if not resolution or len(resolution) != 2 or not resolution[1]:
        return Finding(check, Status.UNKNOWN, "resolution_unrecorded", UNRESOLVED_SUMMARY,
                       carried, "", "")
    directory, mechanism = resolution[0], resolution[1]
    return Finding(check, Status.PASS, "", "%s  (%s)" % (directory, mechanism),
                   dict(carried, dir=directory, mechanism=mechanism), "", "")


def _grade_boot(check, facts, *_rest):
    status, reason = classify_boot(facts.get("returncode"))
    if status is Status.PASS:
        summary = "python -m optivibe_harness started and exited 0"
    elif reason == "nonzero_exit":
        summary = ("python -m optivibe_harness exited %s writing %s bytes to stdout and "
                   "%s to stderr" % (facts.get("returncode"), facts.get("stdout_bytes"),
                                     facts.get("stderr_bytes")))
    else:
        summary = "python -m optivibe_harness did not exit before its deadline"
    return _finish(check, status, reason, summary, facts)


def _grade_engine(check, facts, *_rest):
    if facts.get("opened") is not True:
        return _finish(check, Status.FAIL, "session_proof_failed",
                       "licence/session proof failed: %s %s"
                       % (facts.get("error_type"), facts.get("error")), facts)
    if facts.get("valid_license") is True:
        return _finish(check, Status.PASS, "",
                       "a session opened and the licence is valid (owned pid %s)"
                       % facts.get("owned_pid"), facts)
    if facts.get("valid_license") is None:
        return _finish(check, Status.UNKNOWN, "license_unreadable",
                       "a session opened but the licence property could not be read", facts)
    return _finish(check, Status.FAIL, "session_proof_failed",
                   "licence/session proof failed: the API reported the licence invalid",
                   facts)


def _grade_engine_cleanup(check, facts, *_rest):
    """Grade doctor's OWN footprint: did the engine this run opened actually go away?

    The proof is a **post-close OS re-read** (``snapshot_engine_pids`` after
    ``session.close``), never the close call's own return.  "The tool reported a clean
    close" and "the process is gone" are different claims and only the second is evidence —
    the same rule the targeted reap path already follows.

    Failing here is not cosmetic.  OptiVibe is single-seat: a diagnostic that proves the
    licence and then leaves the engine running has taken the seat away from the user it was
    called to help, and the previous report said PASS while doing it.
    """
    if facts.get("session_created") is False:
        return _finish(check, Status.PASS, "",
                       "no engine was opened, so there was nothing to reclaim", facts)
    if facts.get("owned_known") is False:
        # The post-open process snapshot failed, so doctor never learned which PID it opened.
        # The close-time ``still_running`` set is then empty BY CONSTRUCTION, not because the
        # owned engine is gone — a PASS here would prove only that doctor forgot its own
        # engine, while a leak could sit unmatched in ``after_pids``.  Indeterminate ownership
        # cannot read PASS.  (Absent on the targeted-reap observation path, so ``is False``
        # keeps that path on the logic below.)
        return _finish(check, Status.UNKNOWN, "ownership_indeterminate",
                       "doctor opened an engine but could not read the process table "
                       "immediately afterward, so it never established which PID was its own; "
                       "a clean close cannot then prove that engine is gone", facts)
    still = facts.get("still_running")
    if still is None or facts.get("after_pids") is None:
        return _finish(check, Status.UNKNOWN, "cleanup_unobserved",
                       "doctor could not re-read the process table after closing the "
                       "session, so it cannot say whether the engine it opened is gone",
                       facts)
    if still:
        return _finish(check, Status.FAIL, "engine_leaked",
                       "the engine doctor opened is STILL RUNNING after close: pid(s) %s. "
                       "It holds the single seat; end it before running a design session."
                       % ", ".join(str(pid) for pid in still), facts)
    if facts.get("close_watchdog") not in (None, "ok"):
        return _finish(check, Status.WARN, "close_incomplete",
                       "the owned engine is gone, but session.close did not finish "
                       "cleanly (watchdog=%s)" % (facts.get("close_watchdog"),), facts)
    if facts.get("reap_failures"):
        return _finish(check, Status.WARN, "reap_reported_failures",
                       "the owned engine is gone, but the session reported reap failures: "
                       "%s" % ("; ".join(str(item)
                                         for item in facts.get("reap_failures") or ())),
                       facts)
    return _finish(check, Status.PASS, "",
                   "the engine doctor opened was reclaimed and is gone from the process "
                   "table (owned pid(s) %s)"
                   % (", ".join(str(pid) for pid in facts.get("owned_pids") or ()) or "none"),
                   facts)


_HANDLERS = {
    "env.python": _grade_env_python,
    "env.cwd_shadow": _grade_cwd_shadow,
    "env.pythonnet_runtime": _grade_runtime,
    "dependency.mcp_range": _grade_mcp_range,
    "reference.import": _grade_reference_import,
    "reference.dispatcher": _grade_reference_dispatcher,
    "harness.import": _grade_harness_import,
    "harness.manifest": _grade_harness_manifest,
    "composed.manifest": _grade_composed_manifest,
    "mcp.construct": _grade_mcp_construct,
    "zemax.nethelper": _grade_nethelper,
    "zemax.dir": _grade_zemax_dir,
    "boot.exit": _grade_boot,
    "engine.license": _grade_engine,
    "engine.cleanup": _grade_engine_cleanup,
}

_PREFIXED = (
    ("version.", _grade_version),
    ("dependency.", _grade_dependency),
    ("plane.", _grade_plane),
    ("enrichment.", _grade_enrichment),
    ("tool.", _grade_tool),
)


# -- coverage ----------------------------------------------------------------
def _dependency_coverage(observations, by_check, checkout_present):
    """The pin is a FLOOR here.  ``matplotlib`` and ``numpy`` are legitimately declared
    and deliberately unpinned, so a universe member the pin does not claim is normal.

    The blocker is ``""`` and must stay so.  This universe comes from ``dependency.universe``,
    which the pkg worker emits before it probes anything, so no check can be a prerequisite
    of it: there is nothing here whose failure could make the universe unreadable, and
    copying either sibling's blocker list in would let this check SKIP — and a SKIP never
    gates — on a failure that has no bearing on whether the universe could be read.
    """
    facts = _facts(observations, "dependency.universe")
    universe = None
    if facts is not None and facts.get("names") is not None:
        universe = sorted({normalise_dist_name(name) for name in facts["names"]})
    # The probed set comes from the OBSERVATIONS, not from the findings graded so far.
    # The derived dependency names are emitted as *extras*, which ``evaluate`` appends
    # after the spine loop — so at the moment this check is graded they are not yet in
    # ``by_check``, and reading it would report every healthy install as probe_incomplete
    # and pin the exit code at 3 forever.  A dependency that produced an observation was
    # probed; one the worker died before reaching did not, and that is the real signal.
    probed = sorted({normalise_dist_name(check.split(".", 1)[1]) for check in observations
                     if check.startswith("dependency.")
                     and check not in ("dependency.coverage", "dependency.mcp_range",
                                       "dependency.universe")})
    pinned = sorted({normalise_dist_name(name) for name in CRITICAL_DEPS})
    return _coverage_finding("dependency.coverage", "dependency", pinned, universe, probed,
                             "", {"pinned": pinned, "universe": universe, "probed": probed})


def _plane_coverage(observations, by_check):
    """The universe is the dispatcher's connection-shaped properties — a naming-convention
    proxy, and reported as one: a fifth plane that does not follow it is invisible."""
    # The universe is the DISPATCHER's connection-shaped properties, so a Dispatcher that
    # could not be built leaves no universe to read.  Without this the check reports
    # UNKNOWN "universe_unreadable" — doctor's own failure — for a definitive, already
    # reported target defect, and drags the whole run to exit 3.
    #
    # These two are THIS universe's own inputs — ``plane.universe`` is emitted only once a
    # reference Dispatcher exists — and both are graded EARLIER in the spine, so plain
    # ``_first_blocker`` can see them.  Neither property holds for the derived door
    # universe next door, which is why ``_tool_coverage`` needs a different list AND
    # ``_first_blocker_anywhere``: copying this line there is what shipped the defect.
    blocked = _first_blocker(by_check, "reference.import", "reference.dispatcher")
    facts = _facts(observations, "plane.universe")
    universe = sorted(facts["conn_attrs"]) if facts and facts.get("conn_attrs") is not None \
        else None
    pinned = sorted({spec[2] for spec in PLANES.values()})
    probed = sorted({PLANES[check.split(".", 1)[1]][2] for check in by_check
                     if check.startswith("plane.")
                     and check.split(".", 1)[1] in PLANES})
    return _coverage_finding("plane.coverage", "plane", pinned, universe, probed, blocked,
                             {"pinned": pinned, "universe": universe, "probed": probed})


def _reference_door_universe(observations):
    """The reference doors, derived: the composed manifest minus the harness manifest.

    A separate function on purpose.  This is the UNIVERSE DERIVATION, a different relation
    from the pin-versus-universe RULE, and D-19 requires that no ``*coverage*`` function
    compute a set difference of its own — it must delegate, so that the one rule stays in
    one place and a second copy cannot grow beside it.  Never hand-listed: a sixth
    reference door must appear in the universe without anybody editing doctor.
    """
    harness = _facts(observations, "harness.manifest")
    composed = _facts(observations, "composed.manifest")
    if not harness or not composed:
        return None
    if harness.get("names") is None or composed.get("names") is None:
        return None
    return sorted(set(composed["names"]) - set(harness["names"]))


#: The prerequisites of the DERIVED reference-door universe, most-upstream-first.
#:
#: The list must match the inputs ``_reference_door_universe`` actually reads —
#: ``harness.manifest`` and ``composed.manifest`` — and not the inputs of a neighbouring
#: check.  Carrying only the two reference-side rows left the HARNESS side unguarded: a
#: broken ``psutil`` fails ``harness.import``, both manifests are then SKIP-blocked, the
#: universe is unreadable, and the check reported UNKNOWN "universe_unreadable" — doctor's
#: own failure — for a target defect doctor had itself already reported FAIL, dragging a
#: BROKEN/2 install to INCOMPLETE/3.
#:
#: Every row is a real input, and the order is causal so ``_first_blocker`` names a root
#: rather than a symptom:
#:   ``harness.import``     — upstream of BOTH manifests; on failure the harness probe
#:                            returns early and neither is emitted (they become SKIP, and a
#:                            SKIP is not a blocker, so the import itself must be listed).
#:   ``harness.manifest``   — a direct input, and it can fail on its own (building the
#:                            Dispatcher or listing its tools raised) with the import green.
#:   ``reference.import``   — upstream of the composed manifest: the composition imports
#:                            ``optivibe_reference.server``.
#:   ``reference.dispatcher`` — the composition constructs a reference Dispatcher, so a
#:                            build that cannot succeed there cannot succeed here either.
#:   ``composed.manifest``  — the second direct input, which can also fail on its own.
_DOOR_UNIVERSE_PREREQUISITES = ("harness.import", "harness.manifest",
                                "reference.import", "reference.dispatcher",
                                "composed.manifest")


def _tool_coverage(observations, by_check, checkout_present):
    """The universe is derived (see ``_reference_door_universe``); the rule is delegated.

    Every prerequisite here is graded AFTER ``tool.coverage`` in the spine, so the blocker
    must be resolved order-independently — ``_first_blocker`` alone would read ``''`` for
    all five and hand back the UNKNOWN this closes.
    """
    blocked = _first_blocker_anywhere(observations, by_check, checkout_present,
                                      _DOOR_UNIVERSE_PREREQUISITES)
    universe = _reference_door_universe(observations)
    pinned = sorted(REFERENCE_CASES)
    probed = sorted({check.split(".", 1)[1] for check in by_check
                     if check.startswith("tool.") and check != "tool.coverage"})
    return _coverage_finding("tool.coverage", "tool", pinned, universe, probed, blocked,
                             {"pinned": pinned, "universe": universe, "probed": probed})


# -- derived extras ----------------------------------------------------------
def _extras(checks, observations, by_check, checkout_present):
    """Grade the derived names that are not part of the spine.

    ``dependency.*`` beyond the three critical names is derived from ``Requires-Dist``.
    Extras are reported by name and never affect spine completeness: a run must not become
    INCOMPLETE because the product grew a dependency.
    """
    produced = []
    wanted = set(checks)
    # An extra belongs to the stage that owns its family.  Without this the derived
    # dependency names would be re-emitted by every later stage, and a spine that is
    # complete would still print the same finding four times.
    if not any(check.startswith("dependency.") for check in wanted):
        return produced
    for check, observation in sorted(observations.items()):
        if check in wanted or check in BASE_CHECKS or check == OUTCOME:
            continue
        if not check.startswith("dependency."):
            continue
        if check in ("dependency.universe", "dependency.coverage", "dependency.mcp_range"):
            continue
        produced.append(_grade_dependency(check, dict(observation.facts), observations,
                                          by_check, checkout_present))
    return produced


# ---------------------------------------------------------------------------
# run — the staged execution.
# ---------------------------------------------------------------------------
def _stage_plan(args):
    """Return the ordered stages for this invocation.

    The default run spawns exactly ``base``, ``pkg`` and ``zemax``.  It never starts the
    real server and never opens an engine: a diagnostic that takes the single seat to tell
    you the seat is fine has made the problem it was called about.
    """
    plan = [("base", BASE_DEADLINE_S, {}), ("pkg", PKG_DEADLINE_S, {}),
            ("zemax", _zemax_deadline_s(), {})]
    if args.boot:
        plan.append(("boot", BOOT_DEADLINE_S, {}))
    if args.engine:
        plan.append(("engine", ENGINE_PREFLIGHT_DEADLINE_S, {}))
    return plan


#: The spine ids the PARENT derives by crossing other observations.  A worker never emits
#: them, so "the worker did not deliver it" is not evidence of anything about them.
DERIVED_CHECKS = frozenset({"dependency.coverage", "plane.coverage", "tool.coverage"})


#: The observation a worker emits about ITSELF when its body raised — ``_worker.main``'s
#: last act.  It is not a spine id, so nothing in the spine loop would ever render it.
WORKER_FAULT = "worker."


#: Which worker owes a given spine id — the inverse of ``WORKER_CHECKS``, so that a synthesised
#: UNKNOWN can find the fault record belonging to the worker that owed it.
OWNER_OF_CHECK = {check: worker
                  for worker, checks in WORKER_CHECKS.items() for check in checks}


def _worker_cause(worker, observations):
    """The one-line explanation doctor actually holds for a dead worker, or ``""``.

    Two independent sources, in order of directness:

    1. the ``error``/``error_type`` a child caught in its own ``main()`` and deliberately
       reported before dying.  This is doctor's best evidence, and it was being collected
       into the observation map and then dropped on the floor;
    2. the child's **stderr tail** — the only source that survives a failure during *module
       import*, which happens before the child's own handler exists, so no observation is
       emitted at all.

    Doctor exists because the original failure was silent.  Capturing the one non-silent
    explanation available and then withholding it from the report is that same defect one
    level up, so this is not a nicety: it is the product's whole reason for existing,
    applied to doctor itself.
    """
    facts = _facts(observations or {}, WORKER_FAULT + str(worker)) or {}
    error = str(facts.get("error") or "").strip()
    if error:
        error_type = str(facts.get("error_type") or "").strip()
        return ("%s: %s" % (error_type, error)) if error_type else error
    tail = str(facts.get("stderr_tail") or "").strip()
    if tail:
        return "the child wrote to stderr: %s" % tail
    return ""


def _unknown_for(worker, delivered, outcome, observations=None):
    """Map every spine id a worker owed but did not deliver to an UNKNOWN reason.

    ``observations`` is optional so a caller reasoning purely about ownership can omit it;
    when supplied, the captured CAUSE of the worker's death is appended to every detail,
    because a line reading "the pkg worker died before reporting this" with no reason at
    all is exactly the silence doctor exists to break.
    """
    reason = {"timeout": "deadline_exceeded", "crashed": "worker_died",
              "malformed": "malformed_observation"}.get(outcome, "not_emitted")
    detail = {"deadline_exceeded": "the %s worker exceeded its deadline" % worker,
              "worker_died": "the %s worker died before reporting this" % worker,
              "malformed_observation": "the %s worker emitted an unreadable record" % worker,
              "not_emitted": "the %s worker did not report this" % worker}[reason]
    cause = _worker_cause(worker, observations)
    if cause:
        detail = "%s: %s" % (detail, cause)
    return {check: (reason, detail) for check in WORKER_CHECKS[worker]
            if check not in delivered and check not in DERIVED_CHECKS}


def run(args, spawn=_spawn_worker, out=sys.stdout):
    """Execute the staged run and return its ``Summary``.

    ``spawn`` and ``out`` are the injectable seams: every subprocess guard and every
    streaming guard in the suite goes through them, so no guard has to break the machine
    to synthesise an unhealthy install.
    """
    stream = _harden_stream(out)
    renderer = make_renderer(args.format, stream)
    renderer.start()

    requested = {name for name, flag in (("boot", args.boot), ("engine", args.engine))
                 if flag}
    observations = {}
    findings = []
    engine_owned = None

    for worker, deadline_s, env_extra in _stage_plan(args):
        if worker == "pkg":
            universe = _facts(observations, "dependency.universe")
            if universe and universe.get("names") is not None:
                env_extra = dict(env_extra)
                env_extra["OPTIVIBE_DOCTOR_DEP_UNIVERSE"] = json.dumps(universe["names"])
        delivered, outcome, outcome_facts = _collect(
            spawn, worker, deadline_s, env_extra, observations)
        _record_stderr_tail(observations, worker, outcome, outcome_facts)
        if worker == "engine":
            engine_owned = _facts(observations, "engine.owned")
            if outcome == "timeout":
                observations.pop("engine.license", None)
        unknown = _unknown_for(worker, delivered, outcome, observations)
        if worker == "engine" and outcome == "timeout":
            unknown["engine.license"] = (
                "deadline_exceeded",
                "the engine worker exceeded the overall deadline")
            reaped, unavailable = _reap_owned(engine_owned)
            observations["engine.license"] = _reap_observation(
                engine_owned, reaped, unavailable)
            # The targeted reap IS this run's cleanup, so it is what the lifecycle check
            # grades.  Leaving it UNKNOWN would discard a post-kill OS re-read doctor
            # actually performed and report "could not measure" about a measurement.
            observations["engine.cleanup"] = _reap_cleanup_observation(
                engine_owned, reaped, unavailable)
            unknown.pop("engine.cleanup", None)
            unknown["engine.license"] = (
                "deadline_exceeded",
                "the engine worker exceeded the overall deadline; owned pid %s reaped=%s%s"
                % ((engine_owned or {}).get("pid"), reaped,
                   ("; " + unavailable) if unavailable else ""))
        stage = evaluate(observations, checks=WORKER_CHECKS[worker], unknown=unknown,
                         requested=requested)
        for finding in stage:
            findings.append(finding)
            renderer.finding(finding)

    for worker in ("boot", "engine"):
        if worker in requested:
            continue
        for finding in evaluate(observations, checks=WORKER_CHECKS[worker],
                                requested=requested):
            findings.append(finding)
            renderer.finding(finding)

    summary = summarize(findings)
    renderer.summary(summary)
    return summary


def _record_stderr_tail(observations, worker, outcome, outcome_facts):
    """Fold a dying child's stderr tail into its ``worker.<name>`` fault record.

    Only when the worker did NOT complete: a healthy child that chatters on stderr has
    explained nothing, and attaching its noise to a clean run would be noise in the report.

    Merged into the existing record rather than replacing it, because the two sources are
    complementary — the child's own caught exception is the better evidence, and the stderr
    tail is the only evidence that exists when the child died before it had a handler.
    """
    from ._model import Observation

    tail = str((outcome_facts or {}).get("stderr_tail") or "").strip()
    if not tail or outcome == "completed":
        return
    check = WORKER_FAULT + worker
    existing = observations.get(check)
    facts = dict(existing.facts) if existing is not None else {}
    facts.setdefault("stderr_tail", tail)
    observations[check] = Observation(check, facts)


def _collect(spawn, worker, deadline_s, env_extra, observations):
    """Drain one worker's stream into the observation map; return what it delivered.

    Returns ``(delivered, outcome, outcome_facts)``.  The third element carries the
    terminal record's own facts — notably the child's ``stderr_tail`` — so the caller can
    NAME why a worker died instead of reporting an anonymous UNKNOWN.
    """
    delivered = set()
    outcome = "completed"
    outcome_facts = {}
    kwargs = {}
    if worker == "engine":
        kwargs = {"extend_on": "engine.preflight", "extend_to": ENGINE_OVERALL_DEADLINE_S}
    try:
        if kwargs:
            stream = spawn(worker, deadline_s, env_extra or None, **kwargs)
        else:
            stream = spawn(worker, deadline_s, env_extra or None)
        for observation in stream:
            if observation.check == OUTCOME:
                outcome_facts = dict(observation.facts or {})
                outcome = outcome_facts.get("status", "completed")
                continue
            observations[observation.check] = observation
            delivered.add(observation.check)
    except BaseException:                                          # noqa: BLE001
        outcome = "crashed"
    return (delivered, outcome, outcome_facts)


def _reap_owned(owned):
    """Reclaim exactly the PID the engine worker declared, in a child.

    A *targeted* reap through production's create-time-gated predicates — never a name
    sweep, never a PID doctor did not see declared, never a PID that was already there.
    It runs in a child because the reaper needs ``psutil`` and the parent may import
    nothing outside the standard library.

    Returns ``(reaped, unavailable_reason)``.  ``reaped`` is ``True`` only when the child
    reported that a **post-kill re-read of the OS** found the process gone.  When the child
    could not run at all — spawn failure, broken ``psutil``, blown deadline — the reason is
    non-empty and ``reaped`` is ``False``: doctor never claims a reap it did not observe,
    so the orphan is either reclaimed or **named**, never silent.
    """
    if not owned or not owned.get("pid"):
        return (False, "the engine worker never declared an owned pid")
    env = dict(os.environ)
    env["OPTIVIBE_DOCTOR_REAP_PID"] = str(owned.get("pid"))
    # An absent create_time travels as the EMPTY string, not as ``"0.0"``.  The child reads
    # empty as "nothing was recorded" and refuses to settle identity on the pid alone; a
    # literal ``0.0`` would arrive looking like a reading, which is how an unverifiable
    # identity came to be reported as a checked one.
    env["OPTIVIBE_DOCTOR_REAP_CREATE_TIME"] = str(owned.get("create_time") or "")
    env["OPTIVIBE_DOCTOR_REAP_BASELINE"] = json.dumps(list(owned.get("baseline") or []))
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "optivibe_doctor._worker", "reap"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", errors="replace",
            timeout=REAP_DEADLINE_S)
    except subprocess.TimeoutExpired:
        return (False, "the reap worker exceeded its %ss deadline" % REAP_DEADLINE_S)
    except BaseException as exc:                                   # noqa: BLE001
        return (False, "the reap worker could not be started: %s" % type(exc).__name__)
    for line in (completed.stdout or "").splitlines():
        record = _parse_line(line)
        if record is not None and record.check == "engine.reaped":
            if record.facts.get("gone") is True:
                return (True, "")
            if record.facts.get("gone") is None:
                return (False, "the reap worker could not re-read the process: %s"
                        % (record.facts.get("detail") or "no detail"))
            return (False, "the process was still running after the reap: %s"
                    % (record.facts.get("detail") or "no detail"))
    return (False, "the reap worker produced no observation")


def _reap_observation(owned, reaped, unavailable):
    from ._model import Observation
    return Observation("engine.license", {
        "owned_pid": (owned or {}).get("pid"),
        "owned_pid_reaped": bool(reaped),
        "reap_unavailable": unavailable,
        "opened": None,
    })


def _reap_cleanup_observation(owned, reaped, unavailable):
    """The lifecycle facts for a run whose engine worker was killed at its deadline.

    ``reaped`` is already the strong claim — the reap child reports it True only when a
    **post-kill re-read of the OS** found the process gone — so it maps straight onto
    ``still_running``.  When the reap child could not run at all, the process table was
    never re-read and the honest answer is "unobserved" (``None``), never "clean".
    """
    from ._model import Observation

    pid = (owned or {}).get("pid")
    owned_pids = [pid] if pid else []
    observed = not unavailable
    return Observation("engine.cleanup", {
        "session_created": True,
        # Not "ok": the worker was killed mid-run, so close never ran to completion. The
        # engine may be gone via the reap, but the shutdown was not clean and saying so is
        # the difference between a report and a reassurance.
        "close_watchdog": "killed_at_deadline",
        "reap_failures": [],
        "owned_pids": owned_pids,
        "after_pids": [] if observed else None,
        "still_running": ([] if reaped else owned_pids) if observed else None,
        "reap_unavailable": unavailable,
    })


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    """``python -m optivibe_doctor`` — parse, run, and return the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # Both stderr writes below interpolate arbitrary text — ``%r`` of the operator's own
    # argument, ``str(exc)`` of any exception — so an unencodable character genuinely reaches
    # them.  For the *interpreter's own* ``sys.stderr`` this is in fact safe: CPython FORCES
    # the ``backslashreplace`` error handler on ``sys.stderr`` regardless of
    # ``PYTHONIOENCODING`` (an earlier claim that ``cp437:strict`` removes it was wrong).  The
    # hardening is kept as defence in depth for the case ``sys.stderr`` is NOT the interpreter
    # default — a replaced / wrapped / non-console stream that carries no such guarantee —
    # where a crash-while-reporting is otherwise the em-dash finding with a different stream.
    stderr = _harden_stream(sys.stderr)
    try:
        args = parse_args(argv)
    except _Usage as exc:
        stderr.write(str(exc) + "\n")
        stderr.flush()
        return EXIT_USAGE
    try:
        summary = run(args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPT
    except BaseException as exc:                                   # noqa: BLE001
        # One line, never a traceback, and never a health claim: doctor could not measure.
        stderr.write("optivibe doctor: the run faulted: %s: %s\n"
                     % (type(exc).__name__, " ".join(str(exc).split())[:240]))
        stderr.flush()
        return EXIT_BY_STATE[State.INCOMPLETE]
    return exit_code(summary)
