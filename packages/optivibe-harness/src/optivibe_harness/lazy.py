"""lazy.py — LazyHarnessDispatcher: open the ZOS engine on FIRST harness call (NO mcp import).

Why (lazy engine-open): OptiVibe is being registered as a Claude
Code MCP. The as-built entrypoint opened OpticStudio EAGERLY at boot, so every CC
session would grab the single (N=1) seat the instant it launched. ``LazyHarnessDispatcher``
defers the engine-open: it serves the harness MANIFEST immediately (engine-free)
and opens the engine only when a harness TOOL is first dispatched.

Shape: dispatcher-shaped (``list_tools`` / ``dispatch``) so the ``CompositeDispatcher``
wraps it identically to the real ``Dispatcher``. It holds:
- an un-opened ``ZemaxSession`` (constructed engine-free), and
- the real ``Dispatcher(session)`` built EAGERLY — its construction only reads
  ``session._lock`` (created in ``__init__``), it does NOT open the engine,
  so the merged manifest is available with the engine never opened.

``list_tools()`` delegates straight to the inner Dispatcher (engine-free manifest).

``dispatch(name, params)`` (VALIDATE-THEN-OPEN):
The engine is opened ONLY for a call that will actually reach a handler. An UNKNOWN
tool name or a call MISSING a required param is delegated to the inner Dispatcher
WITHOUT opening — the inner envelopes ``unknown_tool`` / ``tool_param`` before any
handler runs, so an unrunnable call never grabs the single (N=1) seat (the original
open-before-route grabbed it for invalid calls). Logic:
- coerce non-dict params to ``{}`` (mirror server.py:160); a non-str / unhashable / None
  name is treated as UNKNOWN (it is not in the known-tools map) and does NOT open;
- if the name is NOT in the known-tools map -> delegate WITHOUT opening (inner -> unknown_tool);
- elif a required param for that name is MISSING -> delegate WITHOUT opening (inner -> tool_param);
- else (dispatchable): if ``not session.is_open`` attempt ``session.open()`` inside a guard:
  - on open SUCCESS -> delegate to the inner Dispatcher;
  - on open FAILURE -> return a never-raise DEGRADED envelope
    (``error_family="engine_unavailable"``) for THAT call and DO NOT latch — a later
    harness call retries (``open()``'s internal retry-cap bounds each attempt). The
    manifest stays engine-free regardless.

The known-tools map (``{name: required_params}``) is built ONCE at ``__init__`` from the
inner dispatcher's PUBLIC ``list_tools()`` (never reaching into ``inner._manifest``). The
inner manifest is immutable post-init (like the composite), so the snapshot stays accurate.

never-raise asymmetry (mirror the boot-helper, ``__main__._build_harness_dispatcher``):
the open-attempt guard catches ``Exception`` -> degraded envelope, but lets
``KeyboardInterrupt`` / ``SystemExit`` PROPAGATE (a deliberate abort is never swallowed
into degraded-mode). The inner Dispatcher already never-raises; the composite outer net
still backstops.

Thread-safety: the check-then-open is safe WITHOUT a wrapper lock because
``ZemaxSession.open()`` re-checks ``is_open`` UNDER ``session._lock`` (double-checked,
session.py:171-175) and is itself serialized — two concurrent first-dispatch calls can
never both ``CreateNewApplication()`` (single-seat). So we rely on the session's
own lock; the lazy wrapper adds no second lock.

``close()`` reaps the session (``session.close()`` is idempotent + never-raises + safe on
a NEVER-opened session), so reap-if-opened is free; ``main()``'s finally can
always close.

``mcp`` is NOT imported here (mirror server.py / composite.py — the MCP adapter imports
``mcp`` lazily in server_mcp.py).

LAZY-BOOT-LOGGING: the on-disk logs (``optivibe_stdio.log`` +
``optivibe_interactions.jsonl``) are no longer created at boot — a reference-only / idle
session must leave NOTHING on disk. ``dispatch()`` calls the one-time, never-raise
``_activate_logging()`` at the TOP of the dispatchable-but-cold branch, BEFORE
``session.open()``, so the FIRST engine-touching call (a "Zemax ping") activates logging:
it swaps fd 1/2 from devnull onto the real log file AND wires the durable
``InteractionLog`` (rebinding the inner Dispatcher's cached ``_logger`` — server.py:185
caches it at ``__init__``). A reference / unknown / non-dispatchable / missing-param call
never enters that branch, so it never activates. Activating BEFORE ``open()`` means a
wedged/slow first-open still leaves the connect breadcrumbs.

Live ZOS-API integration exercises the decisive lazy gate (a reference call does NOT grab
the seat; the engine opens only on the first harness call) and the lazy-boot-logging gate
(a reference call leaves NO log files; the first harness call creates them).
"""
import os
import threading

from ._stdio_log import _open_log_fd
from .server import Dispatcher, _safe_error_text

# Open-failure families forwarded VERBATIM to the wire; every other open failure
# still degrades to "engine_unavailable" (the hang-watchdog contract). This
# list is EXTENDED, never relaxed.
#
# "engine_channel_dead" is needed because the close-after-an-observed-fault
# path reaches ``session.open()`` -> ``_open_locked`` -> GATE B2, and without the
# entry it would be mislabelled ``engine_unavailable`` — which PROMISES a later
# retry for a session that is terminal and will never re-open.
#
# Disclosed seam: these strings are DECLARED on the exception classes in errors.py
# and REPEATED here — a hand-sync dependency, correct for these two entries
# and pinned by ``test_close_after_fault_does_not_report_engine_unavailable``: drop
# ``engine_channel_dead`` from this tuple and it reddens. That regression lives in
# the development suite and is not shipped with this package, so a reader of the
# published file cannot run it — the guarantee is disclosed here, not demonstrated.
_FORWARDED_OPEN_FAMILIES = ("engine_connect_timeout", "engine_channel_dead")


class LazyHarnessDispatcher:
    """Dispatcher-shaped wrapper that opens the ZOS engine on the FIRST harness call.

    Construct with an un-opened ``ZemaxSession``. The inner ``Dispatcher(session)`` is
    built EAGERLY (engine-free — it only reads ``session._lock``), so ``list_tools()``
    serves the manifest with the engine never opened. ``dispatch()`` lazy-opens on the
    first call; an open-failure degrades that one call (no latch) so a later call retries.
    """

    def __init__(self, session, call_warn_threshold_s=60.0):
        self._session = session
        # Terminal closed-state latch: set True by close(); once True,
        # dispatch() returns a never-raise session_closed envelope and NEVER
        # re-attempts session.open() (no seat re-grab on a reaped session).
        self._closed = False
        # lazy-boot-logging: one-time logging activation (fd-1/2 devnull->file swap +
        # InteractionLog wiring) on the FIRST engine-open. The dedicated lock is small
        # and INDEPENDENT of session._lock (no lock-ordering entanglement with open());
        # the flag makes activation fire at most once per process (no re-swap on a
        # re-open-after-reap, no retry-storm on a failed activation).
        self._logging_activated = False
        self._activation_lock = threading.Lock()
        # Eager, engine-free build: Dispatcher.__init__ only reads
        # session._lock (created in ZemaxSession.__init__); it does NOT open the engine.
        # the hang-watchdog §4.3: pass the slow-call WARN threshold through to the
        # inner Dispatcher so a slow dispatched call leaves a durable breadcrumb.
        self._inner = Dispatcher(session, call_warn_threshold_s=call_warn_threshold_s)
        # VALIDATE-THEN-OPEN gate: snapshot {name: required_params}
        # from the inner's PUBLIC list_tools() ONCE (the inner manifest is immutable
        # post-init, like the composite). Built engine-free (list_tools needs no engine).
        # Used to decide whether a call is DISPATCHABLE before grabbing the seat;
        # never reaches into inner._manifest. Tuple values -> required-param membership.
        self._required_by_tool = {
            entry["name"]: tuple(entry.get("required_params") or ())
            for entry in self._inner.list_tools()
        }

    # ------------------------------------------------------------------ #
    # L26 permit/own — what the validate-then-open gate now OWNS:
    #   - The known-tools map is a SNAPSHOT of list_tools() at __init__; the inner
    #     manifest is immutable post-init (confirmed: Dispatcher builds it in __init__
    #     and never mutates), so the snapshot stays authoritative for this wrapper's life.
    #   - A non-str / unhashable / None name is treated as UNKNOWN (the ``name in map``
    #     test is guarded so an unhashable name cannot crash the gate — it routes to the
    #     inner's own never-raise net, no engine opened).
    #   - A dispatchable call whose params is a weird/mapping object still works: params
    #     are coerced to {} only for the MISSING-required-param presence test; the original
    #     params object is passed UNCHANGED to the inner (which re-coerces, server.py:160).
    #
    # L26 self-adversary — what each path now PERMITS/OWNS:
    #   1. Broadened ``except Exception`` gate: the ENTIRE dispatchable
    #      determination (known-map lookup AND required-param presence test) is wrapped in
    #      one ``try/except Exception``. A pathological-but-functionally-valid call (a
    #      tool_name whose ``__hash__``/``__eq__`` raises, or a hostile dict-subclass whose
    #      ``__contains__`` raises) degrades GRACEFULLY -> ``dispatchable=False`` -> engine
    #      NOT opened -> delegated to the inner's ``except BaseException`` net (enveloped,
    #      no crash). NO ordinary valid call is misclassified: a real name with all required
    #      params present still hashes/compares/contains cleanly -> ``dispatchable=True`` ->
    #      the happy path still opens. KI/SystemExit still PROPAGATE (``except Exception``,
    #      not ``BaseException``) — a deliberate abort is never swallowed.
    #   2. ``_closed`` latch: set True in ``close()``; a dispatch AFTER close
    #      returns a ``session_closed`` envelope WITHOUT re-opening AND without delegating
    #      (the session is reaped). Dispatch-after-close never re-opens AND never raises.
    #      ``close()`` stays idempotent (sets the flag then routes to session.close(), which
    #      is itself idempotent). main()'s single-owner reap is unaffected (close runs in the
    #      finally AFTER _serve). A normal dispatch BEFORE close is unaffected (the flag is
    #      False, so the full validate-then-open path runs as before).
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # State inspection (for tests).
    # ------------------------------------------------------------------ #
    @property
    def engine_opened(self) -> bool:
        """True once the engine has been opened (the session HOLDS a handle).

        Reads the session's own ``is_open`` (``_app is not None and not _closed``), so a
        post-reap ``close()`` flips this back to False — it reflects handle state,
        not a one-way latch.

        WHAT IT DOES NOT ESTABLISH: it inherits ``is_open`` verbatim, one hop
        out, so like ``is_open`` it says nothing about whether the engine's remoting
        CHANNEL is usable — after an observed channel fault this still reads True
        while every engine call is refused. The channel verdict is
        ``session.channel_dead``.
        """
        return bool(self._session.is_open)

    # ------------------------------------------------------------------ #
    # Dispatcher-shaped surface.
    # ------------------------------------------------------------------ #
    def list_tools(self):
        """Delegate to the inner Dispatcher's manifest (engine-free)."""
        return self._inner.list_tools()

    def _activate_logging(self):
        """One-time, never-raise logging activation on the FIRST engine-open.

        lazy-boot-logging: boot redirected fd 1/2 to devnull and left
        ``session._logger`` None, so an idle / reference-only session leaves NOTHING on
        disk. The first dispatchable harness call is a "Zemax ping" — activate logging:

        (a) swap fd 1/2 from devnull onto the real log file (step-F ownership: open ->
            dup2(1) -> dup2(2) -> close the original fd; dup2 copies, fd 1+2 keep it open);
        (b) wire the durable ``InteractionLog`` (only if absent) in the workspace; and
        (c) REBIND the inner Dispatcher's ``_logger`` — server.py:185 caches
            ``getattr(session, "_logger", None)`` at ``__init__`` (None at boot now), so
            the inner's slow-call watchdog would otherwise log through a stale None.

        Idempotent: double-checked under the dedicated ``_activation_lock`` and gated by
        the one-time ``_logging_activated`` flag (set in ``finally`` even on failure, so a
        broken activation never retry-storms). NEVER raises and NEVER takes
        ``session._lock`` (no lock-ordering entanglement with ``open()``). A re-open after
        reap does NOT re-fire (the flag persists); the fd 1/2 from the first activation
        (append-mode file) stay valid.
        """
        if self._logging_activated:
            return
        with self._activation_lock:
            if self._logging_activated:
                return
            try:
                # (a) swap fd 1/2 devnull -> the real log file.
                fd = _open_log_fd()
                try:
                    os.dup2(fd, 1)
                    os.dup2(fd, 2)
                finally:
                    os.close(fd)
                # (b) wire the InteractionLog (only if absent) + (c) REBIND the inner.
                from .interaction_log import InteractionLog

                root = getattr(self._session, "workspace_root", None) or os.getcwd()
                if getattr(self._session, "_logger", None) is None:
                    self._session._logger = InteractionLog(
                        os.path.join(root, "optivibe_interactions.jsonl")
                    )
                # server.py:185 caches _logger at __init__ (None at boot) -> rebind.
                self._inner._logger = self._session._logger
            except Exception:  # noqa: BLE001 — never block a dispatch on logging
                pass
            finally:
                # Set even on failure -> never retry-storm a broken activation.
                self._logging_activated = True

    def dispatch(self, tool_name, params):
        """VALIDATE-THEN-OPEN, then delegate; ALWAYS returns an envelope.

        TERMINAL CLOSED-STATE: once ``close()`` has reaped the session, every
        further ``dispatch()`` returns a never-raise ``session_closed`` envelope WITHOUT
        re-opening and WITHOUT delegating — a reaped session never re-grabs the seat.

        The engine is opened ONLY for a call that will actually reach a handler. An
        UNKNOWN tool name, or a real tool MISSING a required param, is delegated to the
        inner Dispatcher WITHOUT opening (it envelopes ``unknown_tool`` / ``tool_param``
        before any handler runs) — so an unrunnable call never grabs the single (N=1)
        seat. Only a genuinely dispatchable call attempts ``session.open()``.

        For a dispatchable call: if the session is already open, delegate straight to the
        inner Dispatcher (which never-raises). Otherwise attempt ``session.open()`` inside
        a guard: on success delegate; on an ORDINARY-exception failure return a never-raise
        degraded envelope (``error_family="engine_unavailable"``) for THIS call WITHOUT
        latching — a later harness call retries. ``KeyboardInterrupt`` / ``SystemExit``
        during the open PROPAGATE (mirror the boot-helper asymmetry): a deliberate abort
        is never degraded.

        Thread-safety: the ``is_open`` fast-check followed by ``open()`` is safe with no
        wrapper lock because ``open()`` re-checks ``is_open`` under ``session._lock``
        (double-checked) and serializes the connect — concurrent first-dispatch can never
        spawn N>1 engines (single-seat).
        """
        # terminal closed-state: once close() has reaped the session, NEVER
        # re-attempt session.open() and NEVER delegate to the inner — return a
        # never-raise session_closed envelope. The seat is gone; a post-reap dispatch
        # must not re-grab it (the wrapper's object contract is "closed is terminal").
        if self._closed:
            return {
                "ok": False,
                "tool": tool_name,
                "result": None,
                "error": "dispatcher is closed; the engine session was reaped",
                "error_family": "session_closed",
            }

        # Mirror server.py:160 — coerce non-dict params before the presence test.
        probe_params = params if isinstance(params, dict) else {}

        # VALIDATE: is this call dispatchable? The ENTIRE
        # determination — the known-map lookup AND the required-param presence test —
        # runs inside ONE never-raise guard. A pathological tool_name (``__hash__`` /
        # ``__eq__`` raising) or a hostile params object (``__contains__`` raising)
        # therefore degrades to NOT dispatchable -> the engine is NOT opened and the
        # call is delegated to the inner's own ``except BaseException`` net (enveloped
        # safely). ``except Exception`` (not ``BaseException``) so KI/SystemExit
        # propagate (deliberate abort is never swallowed into a misroute).
        try:
            required = self._required_by_tool.get(tool_name)
            dispatchable = required is not None and all(
                p in probe_params for p in required
            )
        except Exception:  # noqa: BLE001 — hostile name/params -> NOT dispatchable; KI/SystemExit propagate
            dispatchable = False

        if dispatchable and not self._session.is_open:
            # lazy-boot-logging: a dispatchable harness call on a COLD session is the
            # first "Zemax ping" — activate logging BEFORE open() so a wedged/slow first
            # open still captures the connect breadcrumbs (one-time, never-raise). A
            # reference / unknown / non-dispatchable / missing-param call never reaches
            # here, so it never activates -> leaves nothing on disk.
            self._activate_logging()
            # OPEN only now — the call WILL reach a handler.
            try:
                self._session.open()
            except Exception as exc:  # noqa: BLE001 — engine cold/seat taken -> degrade; KI/SystemExit propagate
                # Degraded envelope for THIS call only — no latch. A later harness call
                # retries (open()'s internal retry-cap bounds each attempt). list_tools
                # stays engine-free regardless.
                #
                # the hang-watchdog §3 — EXPLICIT ALLOW-LIST: forward ONLY
                # the one new family we intentionally added (a WEDGED open ->
                # "engine_connect_timeout"); EVERY other open failure (license-invalid
                # SessionConnectError, the generic "after N attempts" SessionConnectError,
                # a bare RuntimeError) still degrades to "engine_unavailable" — UNCHANGED,
                # non-regressive. A blanket ``family or "engine_unavailable"`` would leak
                # a generic ``session_connect`` family the agent's contract does not expect.
                family = getattr(exc, "error_family", None)
                wire_family = (
                    family
                    if family in _FORWARDED_OPEN_FAMILIES
                    else "engine_unavailable"
                )
                return {
                    "ok": False,
                    "tool": tool_name,
                    "result": None,
                    "error": _safe_error_text(exc),
                    "error_family": wire_family,
                }
        # Either: dispatchable + engine now open, OR unknown/under-specified (engine NOT
        # opened — the inner envelopes the validation error before any handler runs).
        # The inner Dispatcher never-raises and re-coerces params itself (server.py:160).
        return self._inner.dispatch(tool_name, params)

    # ------------------------------------------------------------------ #
    # Lifecycle.
    # ------------------------------------------------------------------ #
    def close(self):
        """Reap the underlying session (idempotent + never-raises; safe if never opened).

        REAP-THEN-LATCH: reap the session FIRST, then set the terminal
        ``_closed`` latch in a ``finally`` so the latch is set on EVERY path — but never
        BEFORE the reap. The prior latch-first ordering had a latent gap: if
        ``session.close()`` ever raised, the wrapper would mark itself closed (blocking a
        later ``dispatch()`` from re-opening) while the session went un-reaped. The
        ``finally`` keeps the guarantee (a later ``dispatch()`` short-circuits to a
        ``session_closed`` envelope and never re-opens) AND guarantees the reap is attempted
        first. ``ZemaxSession.close()`` is a no-op on a never-opened session and is
        itself idempotent, so the single-owner reap in ``main()``'s finally is safe whether
        or not the engine ever opened, and a double close() never raises. NEVER raises.
        """
        try:
            self._session.close()
        finally:
            self._closed = True
