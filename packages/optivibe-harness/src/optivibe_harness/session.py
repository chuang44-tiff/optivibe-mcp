"""session.py — ZemaxSession: single-handle ZOS-API engine lifecycle.

Probe-grounded: the ZOS-API is SINGLE-SEAT (N=1). A 2nd
``CreateNewApplication()`` binds the SAME engine and closing any one handle
poisons the shared remoting channel — so ``ZemaxSession`` owns EXACTLY ONE
application handle, opened once and closed once.

- ``open()`` is idempotent: a 2nd call returns ``self`` WITHOUT a 2nd
  ``CreateNewApplication()``.
- ``close()`` is idempotent (2nd call is a no-op), runs UNDER ``_lock`` (atexit /
  SIGTERM paths use a non-blocking acquire + timeout so they never deadlock on an
  in-flight dispatch), swallows the ``RemotingException`` a double-close raises,
  flips ``_closed``, PID-polls for async teardown (the PID exits ~1.5 s
  after ``CloseApplication()`` returns), then force-reaps any straggler. ``close``
  NEVER raises.
- ``app`` / ``system`` guard ``_closed`` and raise ``SessionClosedError`` BEFORE
  touching the .NET proxy, so callers get a clean domain error, not a raw
  IPC traceback.
- An ``atexit`` handler closes unconditionally; a SIGTERM handler is installed
  ONLY when ``install_sigterm=True`` (default off).

Live ZOS-API integration: exercised end-to-end by the live test; unit-tested
here against a fake app double (no backend).
"""
import atexit
import math
import os
import queue
import sys
import threading
import time

from . import _bootstrap, process_reaper
from .errors import (
    SessionClosedError,
    SessionConnectError,
    SessionConnectTimeoutError,
    SessionMisuseError,
)


def _run_with_watchdog(fn, timeout_s):
    """Run ``fn()`` on a DAEMON thread; return ``("ok", value) | ("error", exc) |
    ("timeout", None)`` (the hang-watchdog §1.1).

    On timeout the worker LEAKS (daemon => process exit is never blocked). The
    worker is NEVER joined; ``box.get(timeout)`` is the only bounded wait. The
    worker takes NO lock (it is a bare closure over a local connection/app), so it
    can never deadlock the calling thread (which holds ``session._lock`` for at most
    ``timeout_s`` via the TIMED queue wait — not a lock acquire).

    L26 self-adversary — what this PERMITS / OWNS:
    - PERMITS a wedged .NET call to leak ONE daemon worker; the daemon flag GUARANTEES
      interpreter shutdown is never blocked by it, and the worker is never joined.
    - OWNS every failure path: ``_worker`` catches ``BaseException`` and routes it to
      the queue, so a late .NET raise lands in a queue the parent no longer reads
      (harmless, never re-raised into the parent). The function itself never raises.

    The timeout can arrive via
    the ``ZemaxSession(connect_timeout_s=...)`` kwarg, NOT only the clamped env var —
    so clamp it HERE too. A non-positive / non-finite ``timeout_s`` is REPLACED with
    a sane 30.0 s. This makes ``box.get(timeout=neg)`` raising ``ValueError`` (A1) and
    ``box.get(timeout=nan)`` degrading to a blocking wait (A2) UNREACHABLE at the
    primitive — the bounded wait is ALWAYS real, so the helper's "never raises / only
    a bounded wait" contract (§1.1) now holds for ANY caller. L26: this never rejects
    a valid value (any positive finite seconds passes through); only pathological
    inputs are floored to the default.
    """
    if not (
        isinstance(timeout_s, (int, float))
        and math.isfinite(timeout_s)
        and timeout_s > 0
    ):
        timeout_s = 30.0
    box = queue.Queue(maxsize=1)

    def _worker():
        try:
            box.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 — carry the .NET/license exc back
            box.put(("error", exc))

    t = threading.Thread(target=_worker, name="zos-connect", daemon=True)
    t.start()
    try:
        return box.get(timeout=timeout_s)
    except queue.Empty:
        return ("timeout", None)


class ZemaxSession:
    """A single-handle ZOS-API engine session (open/dispatch/close lifecycle).

    Optional persistence attributes (NOT set in ``__init__`` — a launch-path concern
    set externally; persistence-workspace D1/D2). The resolvers read them via
    ``getattr(session, <attr>, None)`` so an un-set session degrades gracefully:

    - ``workspace_root`` (str) — the FLAT design workspace root, pinned ONCE at MCP
      boot (``__main__.main()``: ``OPTIVIBE_WORKSPACE_ROOT`` env or ``os.getcwd()``).
      When set, ``save_candidate`` / ``promote_best`` / ``save_snapshot`` / the
      optimizer trail / merit files resolve FLAT under it (no ``projects/<name>/``).
    - ``projects_root`` (str) — the LEGACY ``projects/<design>/`` layout alias (back-
      compat; only consulted when ``workspace_root`` is unset).
    - ``artifact_sink`` — an explicitly-wired ``ArtifactSink`` (back-compat; when
      absent, ``save_snapshot`` / ``optimize`` fall back to the session-default
      workspace sink built lazily under ``workspace_root``).
    """

    # NEW-5: cap the surviving-orphan ledger. A genuine reap failure is an
    # abnormal event, but a re-openable session under repeated failed reaps could
    # grow ``reap_failures`` without bound. Keep only the most-recent N entries
    # (a bounded ring) — the latest failures are the actionable ones.
    _REAP_FAILURES_MAX = 100

    def __init__(
        self,
        *,
        connect_timeout_s: float = 30.0,
        max_connect_retries: int = 3,
        close_poll_timeout_s: float = 6.0,
        logger=None,
        install_sigterm: bool = False,
        slow_call_threshold_s: float = 60.0,
    ):
        self._connect_timeout_s = connect_timeout_s
        self._max_connect_retries = max_connect_retries
        self._close_poll_timeout_s = close_poll_timeout_s
        self._logger = logger
        # the hang-watchdog §4.3: ONE knob the Dispatcher AND open() share. Read by
        # open()'s slow-open breadcrumb (§4.2) and by Dispatcher via getattr.
        self.slow_call_threshold_s = slow_call_threshold_s

        self._app = None
        self._closed = False
        self._tracked = {}  # {pid: create_time} — only PIDs WE spawned
        self._baseline = set()  # engine PIDs present before our spawn (never touch)
        # the hang-watchdog §1.2: the pre-create instant persisted at the TOP of
        # _open_locked so close() can re-diff against the SAME create-time gate even
        # when open() raised. None => no open ever attempted (close-sweep no-ops).
        self._connect_pre_create_time = None
        # A single REENTRANT lock (NEW-1). INVARIANT: cross-thread mutual
        # exclusion is preserved exactly as a plain Lock — a second THREAD still
        # blocks until the holder releases, so dispatch/close serialization
        # (single-seat engine) is unchanged. What RLock adds is SAME-thread
        # re-entry: the fix put open()'s critical section under this lock,
        # so a dispatched handler (which runs while dispatch holds the lock) that
        # re-enters ``session.open()`` — or ``close()`` — on the SAME thread no
        # longer self-deadlocks (NEW-1). A plain Lock dead-locked that edge; the
        # "handlers must not re-enter" policy was documented but unenforced
        # and could not cover the new open()-under-lock path. RLock closes it
        # structurally. (A handler must still not re-enter ``dispatch`` itself,
        # which would re-run the whole call under the same held lock — that is a
        # logic loop, not a lock issue.)
        self._lock = threading.RLock()
        # Surviving-orphan ledger: any reap that did NOT succeed
        # (access_denied / refused_pid_reuse / still-alive) is appended here AND
        # logged, so a leaked engine is never silent. Queryable by callers/tests.
        self.reap_failures = []

        # atexit close is UNCONDITIONAL (standing order L22: reap every spawn).
        atexit.register(self._atexit_close)

        # SIGTERM handler is opt-in (default off) so importing/using a session in
        # a non-main thread or a host that owns signals is not disturbed.
        if install_sigterm:
            self._install_sigterm_handler()

    # ------------------------------------------------------------------ #
    # State inspection.
    # ------------------------------------------------------------------ #
    @property
    def is_open(self) -> bool:
        """True if a live engine handle is held and the session is not closed."""
        return self._app is not None and not self._closed

    @property
    def app(self):
        """The live ``IZOSAPI_Application`` handle.

        Guards ``_closed`` / ``_app is None`` and raises ``SessionClosedError``
        BEFORE any .NET access, so a use-after-close yields a clean domain error
        rather than a raw remoting IPC traceback.

        CONTRACT: this property only guards the *closed* state — it returns
        a LIVE proxy. A subsequent member access on that proxy can still raise a
        raw ``System.Runtime.Remoting.RemotingException`` if the channel was
        poisoned while ``_closed`` is still ``False``. Callers MUST therefore route
        every engine call through ``Dispatcher.dispatch`` (or otherwise wrap it in
        ``map_dotnet_exception``), which classifies that raw .NET exception into a
        typed ``SessionClosedError`` envelope. Touching ``session.app`` /
        ``session.system`` directly and dereferencing the proxy outside dispatch
        forgoes that classification.
        """
        if self._closed or self._app is None:
            raise SessionClosedError("session is closed; no live engine handle")
        return self._app

    @property
    def system(self):
        """The engine's ``PrimarySystem`` (``IOpticalSystem``); same closed guard.

        Same contract as ``app``: the closed guard is BEFORE the .NET call,
        but ``self._app.PrimarySystem`` itself is a live remoting access that can
        raise a raw ``RemotingException`` on a poisoned-but-not-yet-flagged channel.
        Route through ``Dispatcher.dispatch`` so that raw exception is classified.
        """
        if self._closed or self._app is None:
            raise SessionClosedError("session is closed; no live engine handle")
        return self._app.PrimarySystem

    # ------------------------------------------------------------------ #
    # Lifecycle.
    # ------------------------------------------------------------------ #
    def open(self):
        """Open the engine session (idempotent). Returns ``self``.

        The open/connect critical section runs UNDER ``_lock`` so two
        concurrent opens can never both call ``CreateNewApplication()`` and spawn
        N>1 engines (single-seat: a 2nd create binds the same engine and poisons
        the shared channel when the first handle closes). ``is_open`` is
        re-checked INSIDE the lock (double-checked) so the 2nd caller returns
        ``self`` without a 2nd create.

        Per attempt: capture a pre-create wall-clock instant + the baseline PID
        set -> create the app -> diff to capture EXACTLY our newly-spawned PID
        (only a PID whose ``create_time`` is at/after the pre-create
        instant is "ours"; an unrelated concurrent ``OpticStudio.exe`` is
        excluded) -> assert ``IsValidLicenseForAPI`` (else reap + retry).
        Retries up to ``max_connect_retries`` with backoff ``0.5 * 2**attempt``.
        The spawned PID is captured BEFORE the license check (the only post-create
        step that can raise) so a half-open engine is always reapable.

        Reentry guard: if THIS thread already holds ``_lock`` (we are nested
        inside a dispatch handler that re-entered ``open()``) AND the session is
        already open, return ``self`` (a harmless no-op) — we must NOT spawn a 2nd
        engine under the in-flight call (single-seat: a 2nd create would bind+poison
        the engine). If we own the lock but the session is NOT open, there is no
        live engine proxy in flight to corrupt, so this is the NEW-1 reentrant-open
        edge: fall through to ``_open_locked`` (the RLock permits the re-entry) and
        open the engine normally.
        """
        # Same-thread reentry from within a dispatch handler with the engine
        # already open — never spawn a 2nd engine. Idempotent no-op (return self).
        if self._lock_owned_by_current_thread() and self.is_open:
            return self

        # Fast path without taking the lock; the authoritative re-check is inside.
        if self.is_open:
            return self

        with self._lock:
            # Double-checked: another thread may have opened while we waited.
            if self.is_open:
                return self
            return self._open_locked()

    def _open_locked(self):
        """The open body, assumed to run while ``_lock`` is held.

        The baseline PID set + the pre-create instant are captured EXACTLY
        ONCE, before the FIRST connect attempt — NOT re-snapshotted per retry. A
        retry re-snapshot would fold a survivor that attempt N spawned-but-failed-
        to-reap into attempt N+1's baseline (immunizing it from future reaping and
        dropping it from ``_tracked`` — a permanent orphan). Instead, across
        retries we ACCUMULATE (union) every engine PID we spawned into
        ``tracked_acc``; a PID spawned by a failed attempt that did NOT get reaped
        stays tracked (reapable) and never migrates into ``_baseline``.
        """
        zosapi = _bootstrap.load_zosapi()

        # Captured ONCE: the pre-spawn engine baseline and the pre-create
        # instant. Everything created at/after this instant that was NOT in this
        # baseline is OURS — for the WHOLE retry loop, not per attempt. The
        # baseline snapshot iterates all processes and can take time, so capture
        # ``pre_create_time`` AFTER it (closer to the actual create call) to keep
        # the "ours" provenance window as tight as possible.
        baseline = process_reaper.snapshot_engine_pids()
        baseline_pids = set(baseline.keys())
        pre_create_time = time.time()
        # the hang-watchdog §1.2: persist baseline + pre_create_time on the session
        # NOW (at the loop top), BEFORE any attempt — not only on a successful
        # commit. So _close_locked's _abandoned_connect_sweep can always re-diff
        # against the SAME baseline + create-time gate even when open() raised /
        # timed out. The success-commit path re-sets self._baseline (redundant but
        # harmless). (The baseline is the ONE pre-spawn set.)
        self._baseline = set(baseline_pids)
        self._connect_pre_create_time = pre_create_time
        # the hang-watchdog §4.2: a slow-but-not-timed-out open still leaves a
        # durable breadcrumb naming engine_open + elapsed.
        open_start = time.perf_counter()
        # Accumulator (union across attempts): every engine PID WE spawned, mapped
        # to its create_time. A failed attempt's un-reaped spawn stays here so it
        # remains reapable and is never confused with the baseline.
        tracked_acc = {}

        last_exc = None
        for attempt in range(self._max_connect_retries):
            app = None
            try:
                connection = zosapi.ZOSAPI_Connection()
                # the hang-watchdog §1.3: WATCHDOG the un-interruptible .NET create.
                # A timeout returns control after connect_timeout_s (the worker
                # leaks as a daemon) instead of hanging the whole MCP forever.
                status, value = _run_with_watchdog(
                    lambda: connection.CreateNewApplication(),
                    self._connect_timeout_s,
                )
                if status == "timeout":
                    raise SessionConnectTimeoutError(
                        "CreateNewApplication() exceeded "
                        f"connect_timeout_s={self._connect_timeout_s}s"
                    )
                if status == "error":
                    raise value  # re-raise the worker's .NET exception on this thread
                app = value
                if app is None:
                    raise SessionConnectError(
                        "CreateNewApplication() returned None — no engine acquired."
                    )

                # Capture OUR spawned PID BEFORE the license check (the next step
                # that can raise). Diff against the ONE captured baseline; only a
                # PID created at/after pre_create_time is ours, with a
                # small tolerance (NEW-2) so a real spawn whose reported create_time
                # rounds/skews fractionally before pre_create_time is not dropped.
                # PID capture stays on the CALLING thread (unchanged ordering).
                after = process_reaper.snapshot_engine_pids()
                spawned = process_reaper.diff_spawned(
                    baseline, after, min_create_time=pre_create_time
                )
                for pid, info in spawned.items():
                    tracked_acc.setdefault(pid, info.create_time)

                # the hang-watchdog §1.3: WATCHDOG the license read too — the probe
                # notes the license remoting read can itself wedge.
                lic_status, lic_val = _run_with_watchdog(
                    lambda: bool(app.IsValidLicenseForAPI),
                    self._connect_timeout_s,
                )
                if lic_status == "timeout":
                    raise SessionConnectTimeoutError(
                        "IsValidLicenseForAPI read exceeded "
                        f"connect_timeout_s={self._connect_timeout_s}s"
                    )
                if lic_status == "error":
                    raise lic_val
                if not lic_val:
                    raise SessionConnectError(
                        "License is NOT valid for the ZOS-API (entitlement gap)."
                    )

                # Commit state only after the license check passed. The baseline is
                # the ONE pre-spawn set; tracked is the accumulated union of every
                # PID we spawned this open (this attempt + any prior un-reaped one).
                self._app = app
                self._closed = False
                self._baseline = set(baseline_pids)
                self._tracked = dict(tracked_acc)
                # Persist for cross-restart orphan reclaim. Best-effort:
                # a failed write means at worst a future MISSED reap, never a failed
                # open. ``_tracked`` is exactly the create-time-provenance-gated set we
                # legitimately spawned; the parent identity is captured HERE (the actual
                # MCP parent). The hard-kill path never reaches the clean-close
                # unrecord, so this surviving record is what the next boot reclaims.
                try:
                    import psutil as _psutil

                    from . import engine_ledger
                    parent_pid = os.getpid()
                    parent_ct = _psutil.Process(parent_pid).create_time()
                    now = time.time()
                    for pid, ct in self._tracked.items():
                        engine_ledger.record(engine_ledger.EngineRecord(
                            engine_pid=int(pid), engine_create_time=float(ct),
                            parent_pid=parent_pid, parent_create_time=float(parent_ct),
                            recorded_at=now))
                except Exception:  # noqa: BLE001 — ledger is best-effort; open() must never fail on it
                    pass
                # the hang-watchdog §4.2: durable breadcrumb on a slow-but-healthy
                # open (e.g. 18 s under a 30 s timeout). NEVER raises (_log is guarded).
                self._log_slow_open(open_start)
                return self
            except Exception as exc:  # noqa: BLE001 — reap half-open + retry/backoff
                last_exc = exc
                # Reap whatever THIS attempt spawned. Re-diff (same provenance
                # gate) in case the PID only became enumerable after the failing
                # step; union into the accumulator so a PID can never slip the net.
                # The create_time gate keeps an unrelated concurrent
                # engine out of the reap set.
                spawned_after = process_reaper.diff_spawned(
                    baseline,
                    process_reaper.snapshot_engine_pids(),
                    min_create_time=pre_create_time,
                )
                for pid, info in spawned_after.items():
                    tracked_acc.setdefault(pid, info.create_time)
                # Reap the FULL accumulated set against the ONE baseline. A PID a
                # prior attempt failed to reap is retried here and, if it survives
                # again, remains in tracked_acc (reapable) — it never folds into
                # baseline.
                self._reap_app(app, baseline_pids, dict(tracked_acc))
                # the hang-watchdog §1.4: FAIL-FAST on a connect TIMEOUT. A timeout
                # is a SYSTEMIC wedge (healthy open ~5 s) — retrying re-issues the
                # same wedging call (up to N*timeout dead time + N leaked workers).
                # So we re-raise the SAME SessionConnectTimeoutError (preserving its
                # family) WITHOUT falling into the retry/backoff. A prompt failure
                # (None / invalid-license / transient raise) STILL retries below.
                if isinstance(exc, SessionConnectTimeoutError):
                    # Catch a late orphan visible by now, then breadcrumb + re-raise.
                    self._connect_terminal_sweep(
                        baseline, pre_create_time, tracked_acc
                    )
                    self._log_slow_open(open_start, timed_out=True)
                    raise
                if attempt < self._max_connect_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))

        # the hang-watchdog §4.2: breadcrumb on BOTH terminal paths.
        # This is the EXHAUSTED-prompt-retries terminal raise (NOT a timeout — a timeout
        # fails fast earlier with timed_out=True). It is NOT a timeout, so timed_out=False:
        # _log_slow_open fires ONLY if elapsed > slow_call_threshold_s, the correct §4.2
        # semantics for a slow-but-not-timed-out exhausted open. NEVER raises (guarded).
        # L26: a fast exhausted open (e.g. prompt None each attempt, sub-threshold total)
        # emits NOTHING — the breadcrumb is gated on elapsed > threshold, not on the raise.
        self._log_slow_open(open_start, timed_out=False)
        raise SessionConnectError(
            f"ZOS-API connect failed after {self._max_connect_retries} attempts: "
            f"{last_exc}"
        )

    def _log_slow_open(self, open_start, *, timed_out=False):
        """Durable breadcrumb (§4.2): log ``engine_open`` + elapsed when the open
        took longer than ``slow_call_threshold_s`` (or ALWAYS on a timeout). Reuses
        the existing never-raise ``self._log`` seam. NEVER raises.

        L26: this only READS perf_counter + a session attr and writes via _log
        (guarded). It never touches _lock, the engine, or _app.
        """
        try:
            elapsed = time.perf_counter() - open_start
            threshold = getattr(self, "slow_call_threshold_s", 60.0) or 60.0
            if timed_out or elapsed > threshold:
                self._log(
                    f"ZemaxSession: engine_open {'TIMED OUT' if timed_out else 'SLOW'} "
                    f"after {elapsed:.1f}s (threshold {threshold:.1f}s)."
                )
        except Exception:  # noqa: BLE001 — breadcrumb must never break open()
            pass

    def _connect_terminal_sweep(self, baseline, pre_create_time, tracked_acc):
        """One final provenance-gated re-diff + force-reap on the connect-timeout
        exit (the hang-watchdog §1.5, Layer 1). Catches a late orphan that became
        enumerable between the last per-attempt re-diff and the raise. NEVER raises.

        L26 — what it OWNS: every kill routes through ``_force_reap`` ->
        ``terminate_tracked`` (baseline + create_time gated), so a foreign/baseline
        or pre-create engine is NEVER touched (L22). The whole body is guarded so a
        sweep failure can never break the fail-fast raise.
        """
        try:
            spawned = process_reaper.diff_spawned(
                baseline,
                process_reaper.snapshot_engine_pids(),
                min_create_time=pre_create_time,
            )
            for pid, info in spawned.items():
                tracked_acc.setdefault(pid, info.create_time)
            self._force_reap(dict(tracked_acc), set(baseline.keys()))
        except Exception:  # noqa: BLE001 — terminal sweep must never break the raise
            pass

    def _abandoned_connect_sweep(self):
        """Backstop (the hang-watchdog §1.5, Layer 2): re-diff live engine PIDs
        against the SESSION's persisted baseline + pre_create_time and force-reap
        any own-spawn a late worker leaked AFTER open() returned. The catch-all for
        a late orphan enumerable only at close time. NEVER raises.

        L26 — what it OWNS: ``self._connect_pre_create_time is None`` => no open was
        ever attempted => no-op (never sweeps a never-opened session). ALREADY-RESOLVED
        build-time point 1: ``diff_spawned`` keys on the ``before`` mapping's KEYS
        only and never dereferences its values (process_reaper.py:123), so we pass
        the persisted ``self._baseline`` SET directly as ``before``. Every kill
        routes through ``_force_reap`` -> ``terminate_tracked`` (baseline +
        create_time gated): a foreign/baseline or pre-create engine is NEVER touched
        (L22). PIDs already in ``self._tracked`` are excluded (the normal-close reap
        already handled them).
        """
        try:
            if self._connect_pre_create_time is None:
                return  # never attempted an open -> nothing to sweep
            # diff_spawned keys on `before` membership only (process_reaper.py:123) —
            # pass the persisted baseline SET directly (no {pid: None} dict needed).
            spawned = process_reaper.diff_spawned(
                self._baseline,
                process_reaper.snapshot_engine_pids(),
                min_create_time=self._connect_pre_create_time,
            )
            late = {
                pid: info.create_time
                for pid, info in spawned.items()
                if pid not in self._tracked
            }
            if late:
                self._force_reap(late, set(self._baseline))
            # RECORD-ONLY: disclose any engine PID that
            # IS in our create-time provenance window (the diff_spawned result: post-
            # pre_create_time, not in self._baseline) but SURVIVED this sweep — i.e. it
            # was NOT among the PIDs this sweep reaped (NOT in ``late``, e.g. because a
            # later non-latching reopen overwrote the per-session baseline so a prior
            # open's late orphan can no longer be reaped here). We do NOT KILL it:
            # L22 — an un-attributable engine might be the user's concurrent OpticStudio.
            # We RECORD a durable breadcrumb naming the PID + create_time so the leak is
            # never silent (the deferred in-process reap is a known follow-up). L26: this
            # branch owns NO kill path — it only reads the diff + writes one _log line
            # per surviving un-attributable PID; the only mutation is the durable log.
            for pid, info in spawned.items():
                if pid in late:
                    continue  # already targeted by this sweep's reap above
                self._log(
                    f"ZemaxSession: UNATTRIBUTED LATE ENGINE pid={pid} "
                    f"create_time={getattr(info, 'create_time', '?')} — NOT reaped "
                    f"(un-attributable; possibly a concurrent user engine)."
                )
        except Exception:  # noqa: BLE001 — close-time sweep must never break teardown
            pass

    def close(self):
        """Close the engine session (idempotent; NEVER raises in normal use).

        Acquires ``_lock`` (so a dispatch in flight finishes first), then:
        ``CloseApplication()`` in try/except (a double-close raises
        ``RemotingException`` — swallowed), flips ``_closed``,
        PID-polls for the async teardown, and force-reaps any straggler PID via
        the L22 guard.

        Reentry guard: if THIS thread already holds ``_lock`` (we are nested
        inside a dispatch handler that called ``session.close()`` mid-call), do NOT
        run the destructive ``_close_locked()`` — the RLock would otherwise let it
        flip ``_closed`` / call ``CloseApplication()`` / null ``_app`` WHILE the
        in-flight engine call is still using that proxy (the BUG-2 race on the
        synchronous path). A handler closing the engine it is being dispatched on,
        mid-call, is a programming error: raise ``SessionMisuseError`` so it
        surfaces loudly. Dispatch's never-raise envelope catches it (the server
        still returns a clean error envelope, it does NOT crash). The atexit /
        SIGTERM paths do NOT go through here — they use ``_close_nonblocking``,
        which already PID-only-reaps on same-thread ownership.
        """
        if self._lock_owned_by_current_thread():
            raise SessionMisuseError(
                "close() must not be called from within a tool handler "
                "(it would tear the engine down mid-dispatch); the session is "
                "closed by its owner, not by a dispatched call."
            )
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        """The close body, assumed to run while ``_lock`` is held."""
        if self._closed:
            return  # idempotent: 2nd close is a no-op
        app = self._app
        # Flip closed FIRST so any concurrent app/system access guards out cleanly
        # even though CloseApplication is async.
        self._closed = True
        if app is not None:
            try:
                app.CloseApplication()
            except Exception:  # noqa: BLE001 — double-close / transport-loss is fine
                pass
        self._reap_tracked()
        # the hang-watchdog §1.5 Layer 2: AFTER the normal tracked-reap, sweep for a
        # LATE orphan a leaked connect-worker may have spawned after open() returned
        # (the close-time backstop). NEVER raises; no-op if no open was attempted.
        self._abandoned_connect_sweep()
        # Drop our ledger records on clean close — a clean restart must
        # find nothing to reclaim. Best-effort; NEVER raises. (The boot gate would
        # already refuse a dead engine via engine_identity_ok, so this is hygiene, not
        # a correctness dependency.) The hard-kill path deliberately never reaches here
        # — that is the whole point; the record survives so the next boot
        # reclaims.
        try:
            from . import engine_ledger
            engine_ledger.unrecord([int(p) for p in self._tracked.keys()])
        except Exception:  # noqa: BLE001 — unrecord is best-effort hygiene; never raise
            pass
        self._app = None

    def _record_reap_failure(self, result):
        """Record + log a non-successful reap so a survivor is never silent.

        ``result`` is a ``ReapResult`` whose ``ok`` is ``False`` (access_denied /
        refused_pid_reuse / refused_baseline / still-alive). It is appended to the
        ``reap_failures`` ledger AND emitted to the logger/stderr. NEVER raises.
        """
        try:
            self.reap_failures.append(result)
            # NEW-5: bound the ledger — drop the oldest beyond the cap so a
            # long-lived, repeatedly-failing session cannot grow it without limit.
            overflow = len(self.reap_failures) - self._REAP_FAILURES_MAX
            if overflow > 0:
                del self.reap_failures[:overflow]
        except Exception:  # noqa: BLE001 — ledger append must never break teardown
            pass
        msg = (
            f"ZemaxSession: reap of pid={getattr(result, 'pid', '?')} did NOT "
            f"succeed (action={getattr(result, 'action', '?')}): "
            f"{getattr(result, 'detail', '')} — possible surviving engine."
        )
        self._log(msg)

    def _log(self, msg):
        """Best-effort log to the session logger if present, else stderr. NEVER raises.

        Uses the ``InteractionLog.log`` audit seam (keyword-only) when a logger
        is wired so a surviving-orphan event lands in the durable corpus; falls
        back to stderr otherwise. The logger itself never raises (it drops + notes
        stderr), but the call is still guarded so teardown can never break.
        """
        try:
            logger = self._logger
            if logger is not None and hasattr(logger, "log"):
                logger.log(intent="reap_failure", call="terminate_tracked", args=msg)
                return
        except Exception:  # noqa: BLE001 — fall through to stderr
            pass
        try:
            print(msg, file=sys.stderr)
        except Exception:  # noqa: BLE001 — logging must never break teardown
            pass

    def _force_reap(self, tracked, baseline):
        """Poll then force-reap a tracked PID set; record any failed reap.

        Returns nothing; surviving/refused PIDs are routed through
        ``_record_reap_failure`` (logged + appended to ``reap_failures``). NEVER
        raises. A PID that simply exited (already_gone/no_such_process) is success.
        """
        if not tracked:
            return
        gone, _secs = process_reaper.poll_pids_gone(
            list(tracked.keys()), timeout_s=self._close_poll_timeout_s
        )
        if gone:
            return
        # Async close did not finish in time -> force-reap each tracked PID,
        # guarded against baseline + PID-reuse. HONOR the ReapResult: a reap that
        # did NOT succeed means a PID may still be alive — never silently claim
        # success.
        for pid, create_time in tracked.items():
            result = process_reaper.terminate_tracked(
                pid, create_time, baseline_pids=baseline
            )
            if result is not None and not getattr(result, "ok", False):
                self._record_reap_failure(result)

    def _reap_tracked(self):
        """PID-poll the tracked spawn(s); force-reap any straggler (L22)."""
        self._force_reap(dict(self._tracked), self._baseline)

    def _reap_app(self, app, baseline, tracked):
        """Reap a half-open engine during a failed ``open()`` attempt."""
        if app is not None:
            try:
                app.CloseApplication()
            except Exception:  # noqa: BLE001 — best-effort reap of a half-open engine
                pass
        self._force_reap(dict(tracked), baseline)

    # ------------------------------------------------------------------ #
    # Non-blocking close paths (atexit / SIGTERM): never deadlock on dispatch.
    # ------------------------------------------------------------------ #
    def _close_nonblocking(self, *, acquire_timeout_s: float = 2.0):
        """Close via a NON-blocking lock acquire (atexit / SIGTERM safe).

        If the lock IS acquired, run the full orderly ``_close_locked()`` (mutates
        ``_app`` / ``_closed`` + reaps) under the lock.

        If the lock CANNOT be acquired (a dispatch is in flight holding it), DO NOT
        run the mutating teardown unsynchronized (BUG-2): mutating ``_app`` /
        ``_closed`` + driving ``CloseApplication()`` would race the in-flight
        remoting call on the same engine. Instead do a best-effort reap-BY-PID ONLY
        — ``poll_pids_gone`` + ``terminate_tracked`` touch the OS process, NOT the
        shared .NET proxy / ``_app`` / ``_closed`` state — and log that the orderly
        close was skipped. NEVER raises.

        NEW-1 interaction: ``_lock`` is now an ``RLock``. A plain timeout-acquire
        would re-enter SUCCESSFULLY if THIS thread already holds the lock — i.e. a
        SIGTERM/atexit firing on the SAME thread that is mid-dispatch — and then
        run the destructive orderly close UNDER the in-flight call (the exact
        BUG-2 race). So we FIRST check ``_lock._is_owned()``: if this thread
        already holds it, treat it like the "held by a dispatch" case (PID-only
        reap), NOT a clean orderly close. Only when this thread does NOT hold the
        lock do we attempt the timeout-acquire; failing it (another thread holds
        it) also falls back to the PID-only reap.
        """
        already_owned = self._lock_owned_by_current_thread()
        acquired = False
        if not already_owned:
            acquired = self._lock.acquire(timeout=acquire_timeout_s)
        try:
            if acquired:
                self._close_locked()
            else:
                # Lock held by an in-flight dispatch (another thread), or re-entered
                # on this thread mid-dispatch (already_owned): NEVER mutate
                # _app/_closed or call CloseApplication() unsynchronized. Reap by
                # PID only.
                self._log(
                    "ZemaxSession: orderly close skipped (a dispatch holds the "
                    "lock); doing a best-effort PID-only reap without touching the "
                    "engine proxy or close state."
                )
                self._reap_tracked_pids_only()
        except Exception:  # noqa: BLE001 — shutdown path must never raise
            pass
        finally:
            if acquired:
                self._lock.release()

    def _lock_owned_by_current_thread(self) -> bool:
        """True if the CURRENT thread already holds ``_lock`` (RLock re-entry probe).

        ``threading.RLock`` exposes ``_is_owned()`` (used internally by
        ``Condition``); it is stable across CPython. Guarded with a ``getattr``
        fallback so a future lock type without it degrades to ``False`` (the
        timeout-acquire path then governs, which is the pre-RLock behaviour).
        """
        is_owned = getattr(self._lock, "_is_owned", None)
        if is_owned is None:
            return False
        try:
            return bool(is_owned())
        except Exception:  # noqa: BLE001 — probe must never break teardown
            return False

    def _reap_tracked_pids_only(self):
        """Best-effort reap of tracked PIDs WITHOUT touching shared proxy/close state.

        Used only on the unsynchronized shutdown path (BUG-2) when the lock could
        not be acquired. Reads a snapshot of ``_tracked`` / ``_baseline`` and
        force-reaps stragglers by PID; it does NOT read/write ``_app`` or
        ``_closed`` and does NOT call ``CloseApplication()``. NEVER raises.
        """
        try:
            self._force_reap(dict(self._tracked), set(self._baseline))
        except Exception:  # noqa: BLE001 — shutdown reap must never raise
            pass

    def _atexit_close(self):
        """Unconditional atexit reaper (standing order L22). NEVER raises."""
        try:
            self._close_nonblocking()
        except Exception:  # noqa: BLE001
            pass

    def _install_sigterm_handler(self):
        """Install a SIGTERM handler that closes the session (opt-in only)."""
        import signal

        prev = signal.getsignal(signal.SIGTERM)

        def _handler(signum, frame):
            self._close_nonblocking()
            # Chain to a previously installed callable handler, if any.
            if callable(prev) and prev not in (signal.SIG_DFL, signal.SIG_IGN):
                prev(signum, frame)

        signal.signal(signal.SIGTERM, _handler)

    # ------------------------------------------------------------------ #
    # Context manager.
    # ------------------------------------------------------------------ #
    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
