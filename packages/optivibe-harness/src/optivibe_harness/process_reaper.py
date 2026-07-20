"""process_reaper.py — stateless engine-process reaping helpers (L22 safety).

These are pure free functions; the per-session state (tracked PIDs + their
spawn-time ``create_time``, and the baseline PID set) lives on ``ZemaxSession``.

The safety rules are grounded in observed engine behavior:
- A cold ``CreateNewApplication()`` spawns EXACTLY ONE ``OpticStudio.exe`` (the
  headless Server-mode engine). ``ENGINE_PROCESS_NAME`` below is the snapshot /
  evidence key ONLY — it is NEVER a kill key. A user's interactive OpticStudio is
  the SAME exe name, so the reaper keys on the tracked spawn PID, never the name.
- Force-terminate is a LAST RESORT (only if ``CloseApplication()`` failed). It
  refuses unless the PID is NOT in the pre-spawn baseline AND still exists AND its
  ``create_time`` matches the value captured at spawn — the PID-reuse guard that
  prevents terminating an unrelated process that reused a recycled PID.

psutil is a HARD dependency (imported at module level); there is no
absent-degrade path. We NEVER name-sweep.

Live ZOS-API integration: exercised end-to-end by the live test (real spawn ->
real reap); unit-tested here with psutil mocked.
"""
from dataclasses import dataclass

import psutil

# The headless engine process name observed by the probe. Used ONLY to
# filter the process snapshot to engine candidates — never as a kill key. A
# name-sweep would risk a user's interactive OpticStudio; the reaper targets the
# tracked spawn PID exclusively.
ENGINE_PROCESS_NAME = "OpticStudio.exe"

# ``pre_create_time`` is a ``time.time()`` instant; psutil's
# ``create_time()`` for a real spawn can round/skew fractionally BELOW it (a
# coarser file-time clock source). A strict ``>=`` would then EXCLUDE a
# legitimately-spawned engine -> a success-path orphan. Subtract this small
# tolerance from the threshold so a real spawn at ~the same instant is still
# claimed as ours. This is CLOCK-RESOLUTION SLACK ONLY: Windows timer coarseness
# (``time.time()`` ~15.6 ms, psutil file-time create_time sub-100 ms) is absorbed
# by 0.5 s with margin, while a process started even half a second before our
# create call is NOT admitted. Tightened from 2.0 s — a 2 s window
# was wide enough to mis-claim an unrelated engine launched just before us.
CREATE_TIME_TOLERANCE_S = 0.5


@dataclass(frozen=True)
class ProcInfo:
    """An engine-process snapshot row: its name + creation time (PID-reuse key)."""

    name: str
    create_time: float


@dataclass(frozen=True)
class ReapResult:
    """Outcome of a single ``terminate_tracked`` attempt.

    ``action`` is one of: ``refused_baseline``, ``refused_pid_reuse``,
    ``already_gone``, ``terminated``, ``killed``, ``access_denied``,
    ``no_such_process``.
    """

    pid: int
    action: str
    ok: bool
    detail: str


def snapshot_engine_pids():
    """Return ``{pid: ProcInfo}`` for every live engine-named process.

    Filters ``psutil.process_iter`` to ``ENGINE_PROCESS_NAME`` (case-insensitive)
    purely to build the before/after spawn diff. A process that vanishes mid-scan
    (NoSuchProcess) or denies access is simply skipped — this is a snapshot, not a
    kill.
    """
    out = {}
    for proc in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            info = proc.info
            name = info.get("name") or ""
            if name.lower() != ENGINE_PROCESS_NAME.lower():
                continue
            out[int(info["pid"])] = ProcInfo(
                name=name, create_time=float(info.get("create_time") or 0.0)
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def diff_spawned(before, after, *, min_create_time=None):
    """Return ``{pid: ProcInfo}`` present in ``after`` but not in ``before``.

    These are the engine PIDs OUR ``CreateNewApplication()`` spawned — the only
    PIDs the reaper is ever allowed to target.

    Provenance hardening: a PID that is new-to-``after`` is still only
    OURS if it was created AT OR AFTER our own pre-create moment. When
    ``min_create_time`` is supplied (the wall-clock instant captured just before
    ``CreateNewApplication()``), a candidate whose ``create_time`` is older than
    that instant is an unrelated engine that merely appeared in the snapshot window
    (e.g. the user launched their own ``OpticStudio.exe``) and is EXCLUDED. A
    candidate with an unknown ``create_time`` (``0.0`` — AccessDenied sentinel)
    cannot be proven ours and is likewise EXCLUDED when ``min_create_time`` is set.

    Clock-boundary tolerance (NEW-2): the gate is ``create_time >=
    min_create_time - CREATE_TIME_TOLERANCE_S`` (not a strict ``>=
    min_create_time``) so a legitimately-spawned engine whose reported
    ``create_time`` rounds/skews fractionally before the captured instant is NOT
    dropped (a success-path orphan). The tolerance is clock-resolution slack only
    (0.5 s), tiny relative to a human launching a separate engine, so it does not
    re-admit an unrelated process.

    The create_time gate is the ONLY membership test — there is NO
    sole-candidate fallback. A PID clearly older than our pre-create instant is
    NEVER ours, even if it is the only new engine-named PID: a user's own
    ``OpticStudio.exe`` launched just before us would otherwise be mis-claimed and
    force-killed (the failure this gate exists to prevent). If the gate
    admits NOTHING, we track nothing — a missed reap of our own engine in a
    pathological clock case is the SAFE failure (recoverable), whereas killing a
    user's engine is not. "Never kill on doubt."
    """
    new = {pid: info for pid, info in after.items() if pid not in before}
    if min_create_time is None:
        return new
    threshold = min_create_time - CREATE_TIME_TOLERANCE_S
    return {
        pid: info
        for pid, info in new.items()
        if info.create_time and info.create_time >= threshold
    }


def poll_pids_gone(pids, *, timeout_s=6.0, interval_s=0.25):
    """Poll until every PID in ``pids`` has exited, or ``timeout_s`` elapses.

    Returns ``(gone, secs)`` — ``gone`` True if all PIDs are gone, ``secs`` the
    wall-time spent polling. Grounds the observed behavior that
    ``CloseApplication()`` returns before the ``OpticStudio.exe`` PID actually
    exits (~1.5 s later), so close
    must poll rather than assume synchronous teardown.
    """
    import time

    pid_set = set(int(p) for p in pids)
    start = time.perf_counter()
    if not pid_set:
        return True, 0.0
    while True:
        alive = {p for p in pid_set if psutil.pid_exists(p)}
        if not alive:
            return True, time.perf_counter() - start
        elapsed = time.perf_counter() - start
        if elapsed >= timeout_s:
            return False, elapsed
        time.sleep(interval_s)


def terminate_tracked(pid, spawn_create_time, *, baseline_pids, grace_s=5.0):
    """Force-reap a single tracked engine PID — LAST RESORT (L22 safety net).

    Refuses (returns a ``ReapResult`` with ``ok=False``) unless ALL hold:
    - ``pid`` is NOT in ``baseline_pids`` (never touch a pre-existing engine, e.g.
      the user's interactive OpticStudio) -> ``refused_baseline``;
    - ``spawn_create_time`` is VERIFIABLE — i.e. NOT ``None`` and NOT ``0.0`` (the
      AccessDenied snapshot sentinel). Without a trustworthy spawn create_time the
      PID-reuse guard cannot prove identity, so we "never kill on doubt" ->
      ``refused_pid_reuse``;
    - the PID still exists -> else ``already_gone``;
    - the live process's ``create_time`` MATCHES ``spawn_create_time`` (PID-reuse
      guard: a recycled PID now belongs to an unrelated process) ->
      ``refused_pid_reuse``.

    When allowed: ``terminate()`` -> ``wait(grace_s)`` -> ``kill()`` if still
    alive. ``AccessDenied`` / ``NoSuchProcess`` are SURFACED as a ``ReapResult``,
    never raised.
    """
    pid = int(pid)

    if pid in set(int(p) for p in baseline_pids):
        return ReapResult(
            pid=pid, action="refused_baseline", ok=False,
            detail="pid is in the pre-spawn baseline; refusing to terminate",
        )

    # Identity-unverifiable guard (BUG-1): a None or 0.0 spawn_create_time cannot
    # be matched against the live process, so the PID-reuse guard would be a no-op
    # and we might kill an unrelated recycled PID. Never kill on unverifiable
    # identity — refuse BEFORE touching the OS process.
    if spawn_create_time is None or float(spawn_create_time) == 0.0:
        return ReapResult(
            pid=pid, action="refused_pid_reuse", ok=False,
            detail=(
                "spawn create_time is unverifiable "
                f"({spawn_create_time!r}); refusing to terminate on doubt"
            ),
        )

    if not psutil.pid_exists(pid):
        return ReapResult(
            pid=pid, action="already_gone", ok=True,
            detail="pid no longer exists; nothing to reap",
        )

    try:
        proc = psutil.Process(pid)
        live_create_time = float(proc.create_time())
    except psutil.NoSuchProcess:
        return ReapResult(
            pid=pid, action="no_such_process", ok=True,
            detail="process vanished before reap",
        )
    except psutil.AccessDenied:
        return ReapResult(
            pid=pid, action="access_denied", ok=False,
            detail="access denied reading process create_time",
        )

    # PID-reuse guard: only terminate if the live process is the SAME one we
    # spawned. A recycled PID with a different create_time is someone else's.
    # (spawn_create_time is guaranteed verifiable here — None/0.0 refused above.)
    if live_create_time != float(spawn_create_time):
        return ReapResult(
            pid=pid, action="refused_pid_reuse", ok=False,
            detail=(
                "create_time mismatch (pid reuse): "
                f"spawn={spawn_create_time} live={live_create_time}"
            ),
        )

    try:
        proc.terminate()
        try:
            proc.wait(timeout=grace_s)
            return ReapResult(
                pid=pid, action="terminated", ok=True,
                detail="terminate() succeeded within grace window",
            )
        except psutil.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=grace_s)
            except psutil.TimeoutExpired:
                pass
            return ReapResult(
                pid=pid, action="killed", ok=True,
                detail="terminate() timed out; kill() issued",
            )
    except psutil.NoSuchProcess:
        return ReapResult(
            pid=pid, action="no_such_process", ok=True,
            detail="process exited during reap",
        )
    except psutil.AccessDenied:
        return ReapResult(
            pid=pid, action="access_denied", ok=False,
            detail="access denied terminating process",
        )
