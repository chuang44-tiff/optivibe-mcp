"""__main__.py — the single OptiVibe MCP stdio entrypoint (``python -m optivibe_harness``).

Boots BOTH tool sets behind ONE composed MCP server: the harness dispatcher
(threads the live ZOS session as arg-0 — lens / analysis / optimize / workspace
tools) and the reference dispatcher (threads a DB connection as arg-0 —
``lookup_glass`` / ``lookup_operand`` / ``search_reference``). The composite routes
by tool name; ``list_tools()`` returns the merged manifest (composite.py).

LAZY ENGINE-OPEN: the engine is NO LONGER opened
at boot. The harness is wrapped in a ``LazyHarnessDispatcher`` over a fresh, UN-opened
``ZemaxSession`` — its construction is engine-free (``Dispatcher(session)``
only reads ``session._lock``), so the merged manifest serves immediately and OpticStudio
is opened only on the FIRST harness-tool call. This stops a registered Claude Code MCP
from grabbing the single (N=1) OpticStudio seat the instant it launches — reference
grounding tools are served cold, the engine seat is taken on demand.

Asymmetric degradation (lazy engine-open): each dispatcher is built in its own
guarded helper that returns the dispatcher or ``None`` (warning to stderr on failure).
Because the harness build is now engine-free, it effectively ALWAYS builds — only a
manifest-import failure makes it ``None``. The reference layer is provably engine-free +
license-free. reference-None -> harness-lazy-only; harness-None ->
reference-only; BOTH-None -> stderr FATAL + return 2.

SESSION REAP (L22): ``main()`` is the SINGLE owner of the ZOS session. It reaps EXACTLY
ONCE in its ``finally`` via the lazy dispatcher's ``close()`` (which calls
``session.close()``) — or directly via ``session.close()`` if the lazy build itself
failed — on the MAIN thread, idempotent, never-raising, across EVERY exit path (normal
EOF, KeyboardInterrupt, serve crash, compose-after-build fails). Crucially the reap is
safe whether or not the engine ever opened: ``session.close()`` on a NEVER-opened session
is a no-op. The session is a standalone ``CreateNewApplication`` engine; we never
attach/reap an interactive session.

L26 self-adversary note (what this entrypoint now PERMITS / OWNS on every failure path):
- It PERMITS the server to boot with the engine COLD and to serve reference grounding +
  the harness MANIFEST while OpticStudio is never opened; the engine seat is taken only on
  the first harness-tool call (and an open-failure there degrades that one call, not boot).
- It OWNS the ZOS session lifecycle: ``main()``'s ``finally`` is the SINGLE reap owner and
  calls ``close()`` EXACTLY ONCE on EVERY path — serve returns, serve raises,
  KeyboardInterrupt, post-build compose raises. The reap targets the SAME session whether
  the engine opened lazily or never opened: it prefers the lazy dispatcher's ``close()``
  (which closes the session it wraps) and falls back to ``session.close()`` only when the
  lazy build returned ``None`` (no wrapper) — so the session is never closed twice and
  never leaked. ``session`` is constructed BEFORE the wrapper, so even a wrapper-build that
  raised leaves a session the finally can close once (close() is idempotent + never-raises,
  session.py:268; safe on a never-opened session). The ``if session is not None``
  guard only covers ``ZemaxSession()`` construction itself raising. No double-close, no
  leak, no half-open engine: an open-failure on the first harness call is reaped by
  ``open()``'s own internal half-open reap (session.py:386), and the never-opened path has
  nothing to reap.

STDIO ISOLATION (lazy-boot-logging): at boot,
BEFORE any import/construction that can trigger library output (ZemaxSession, matplotlib,
.NET FRU), ``_isolate_stdio()`` saves the real CC pipe fds via ``os.dup``, then redirects
fd 0/1/2 away from the pipe: stdout + stderr + stdin → ``/dev/null``. NO log FILE is
created at boot — devnull never blocks (the pipe-protection invariant holds) and an idle /
reference-only session leaves nothing on disk; the real log file (``optivibe_stdio.log``)
is opened LAZILY, when the engine is first opened, by ``lazy._activate_logging`` (which
swaps fd 1/2 from devnull onto the file). The saved
private fds are passed to ``_serve()`` which builds TextIOWrapper stacks (with
BufferedReader/BufferedWriter — matching the SDK's own wrapping) and passes them as
explicit ``stdin``/``stdout`` to ``stdio_server()``. This OS-level fd surgery prevents ANY
native output (matplotlib font cache, FRU warnings, .NET Console.Write, stray prints) from
corrupting the JSON-RPC transport on the CC pipe, and prevents >4KB stderr from blocking
the process against the pipe buffer. Fallback: if isolation fails, ``_serve()`` gets
``(None, None)`` and calls ``stdio_server()`` with no overrides (the pre-fix behavior).
Private fds are ``os.close()``d in ``main()``'s ``finally`` (explicit, not GC-dependent);
all Python wrappers use ``closefd=False`` (Axis 4 — correctness requirement).
"""
import math
import os
import sys

from ._stdio_log import _LOG_FILENAME, _open_log_fd
from .composite import CompositeDispatcher
from .lazy import LazyHarnessDispatcher
from .server import _safe_error_text
from .server_mcp import build_composite_mcp_server
from .session import ZemaxSession

# Re-exported for existing tests that import them from ``__main__`` (the shared
# resolver now lives in ``_stdio_log`` so ``lazy`` can reuse it without an import
# cycle — lazy-boot-logging, L30).
__all__ = ["main", "_LOG_FILENAME", "_open_log_fd"]


def _warn(message):
    """Best-effort stderr warning that NEVER raises.

    The degrade/FATAL notices are advisory; a broken stderr (closed/redirected
    handle whose ``write`` raises) must not turn a clean degrade — or the
    ``return 2`` both-failed path — into a crash that escapes ``main()``'s try.
    ``except Exception`` only: a ``KeyboardInterrupt``/``SystemExit`` raised during
    the write still propagates (we never swallow an abort), consistent with the
    narrow boot-helper except.
    """
    try:
        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001 — advisory warning; a broken stderr must not crash boot
        pass


def _isolate_stdio() -> tuple:
    """Redirect fd 0/1/2 away from the CC pipe; return private (stdin_fd, stdout_fd).

    After this call:
    - fd 0 -> devnull (stdin isolated)
    - fd 1 -> devnull (stdout isolated)
    - fd 2 -> devnull (stderr isolated)
    - sys.stdin / sys.stdout / sys.stderr rewrapped over the new fds
    - The returned (stdin_fd, stdout_fd) are dup'd copies of the ORIGINAL
      pipe fds, suitable for the MCP transport.

    lazy-boot-logging: fd 1/2 are redirected to **devnull**, NOT
    a log file — devnull never blocks (the pipe-protection invariant
    holds) and leaves NOTHING on disk for an idle / reference-only session. The real
    log FILE is opened lazily by ``lazy._activate_logging`` on the first engine-open,
    which swaps fd 1/2 from devnull onto the file. The MCP transport uses the SAVED
    private pipe fds (step B), decoupled from fd 1/2 — probe-proven unaffected by the
    later devnull->file swap.

    Raises on failure (caller catches + warns + proceeds without isolation).
    On a partial dup2 failure, undoes completed redirects before raising.
    """
    import io

    # A. FLUSH born-with Python wrappers (Axis 5).
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass

    # B. Save the real pipe fds BEFORE any redirect.
    real_stdin_fd = os.dup(0)
    real_stdout_fd = os.dup(1)

    # C. Open devnull as the fd-1/2 redirect target (lazy-boot-logging): NO file at
    #    boot — devnull never blocks (pipe protection holds), leaves nothing on disk.
    #    The real log file is opened lazily on first engine-open (lazy._activate_logging).
    redirect_fd = os.open(os.devnull, os.O_WRONLY)

    # D. Redirect fd 1 -> devnull.
    try:
        os.dup2(redirect_fd, 1)
    except OSError:
        # dup2(fd 1) failed.  Clean up saved fds + redirect_fd, raise.
        os.close(real_stdin_fd)
        os.close(real_stdout_fd)
        os.close(redirect_fd)
        raise

    # E. Redirect fd 2 -> devnull.
    try:
        os.dup2(redirect_fd, 2)
    except OSError:
        # dup2(fd 2) failed, but fd 1 was already redirected.
        # UNDO: restore fd 1 from the saved pipe fd.
        os.dup2(real_stdout_fd, 1)
        os.close(real_stdin_fd)
        os.close(real_stdout_fd)
        os.close(redirect_fd)
        raise

    # F. Close the original redirect_fd (dup2 copies; fd 1 + fd 2 keep it open).
    os.close(redirect_fd)

    # G. Redirect fd 0 -> devnull (Axis 2 — stdin isolation).
    devnull_fd = -1
    try:
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull_fd, 0)
        os.close(devnull_fd)
    except OSError:
        if devnull_fd >= 0:
            try:
                os.close(devnull_fd)
            except OSError:
                pass

    # H. Rewrap sys.stdout, sys.stderr, sys.stdin over the redirected fds.
    #    closefd=False: fd 0/1/2 are process-global, must outlive the wrappers.
    sys.stdout = io.TextIOWrapper(
        io.FileIO(1, "w", closefd=False),
        encoding="utf-8", line_buffering=True,
    )
    sys.stderr = io.TextIOWrapper(
        io.FileIO(2, "w", closefd=False),
        encoding="utf-8", line_buffering=True,
    )
    sys.stdin = io.TextIOWrapper(
        io.FileIO(0, "r", closefd=False),
        encoding="utf-8",
    )

    # I. Return the private pipe fds (caller owns cleanup).
    return real_stdin_fd, real_stdout_fd


def _env_float(name, default):
    """Read an env var as a float, falling back to ``default`` on absent/unparseable.

    the hang-watchdog §1.6/§4.3/§5: ``OPTIVIBE_CONNECT_TIMEOUT_S`` /
    ``OPTIVIBE_CALL_WARN_S`` are the operator surface. GUARDED so a typo never
    crashes boot — an unparseable value silently uses the default.

    POSITIVE-FINITE CLAMP: the value MUST
    be a positive, finite float. A non-positive value (0 / negative) makes EVERY
    open instantly/falsely time out (``box.get(timeout<=0)`` -> Empty) or fires the
    duration Timer immediately on every call; a non-finite value (nan / inf)
    DEFEATS the watchdog (``nan>0`` is False -> blocking wait; ``inf`` -> unbounded
    wait) — re-introducing the very hang this cycle exists to kill. Any of those is
    rejected back to ``default`` (which is itself a sane positive finite constant),
    so the operator surface is "guarded" (spec §1.6 / §4.3).

    L26 self-adversary: does the clamp ever reject a VALID value? No — every value
    a real operator would set (a positive number of seconds) is finite and > 0, so
    it passes through verbatim; only pathological 0/negative/nan/inf are floored.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _warn(
            f"optivibe: {name}={raw!r} is not a number — using default {default}"
        )
        return default
    if not math.isfinite(value) or value <= 0:
        # reject nan / inf / -inf / 0 / negative -> the operator surface is guarded
        # (spec §1.6/§4.3). A non-positive timeout would falsely fail-fast every open;
        # a non-finite one would DEFEAT the watchdog (the unbounded-hang regression).
        _warn(
            f"optivibe: {name}={raw!r} is not a positive finite number — "
            f"using default {default}"
        )
        return default
    return value


def _build_harness_dispatcher(session, call_warn_threshold_s=60.0):
    """Build the LAZY harness dispatcher over an UN-opened session, or return ``None``.

    LAZY-OPEN (lazy engine-open): the engine is NOT opened here. ``LazyHarnessDispatcher``
    holds the un-opened ``session`` and builds the real ``Dispatcher(session)`` EAGERLY —
    that construction is engine-free (it only reads ``session._lock``), so
    the merged manifest is available cold. OpticStudio is opened only on the FIRST
    harness-tool ``dispatch()`` (inside the lazy wrapper). The wrapper effectively always
    builds; the only failure mode is a manifest IMPORT error (a tool module failing to
    import while ``Dispatcher`` builds its default manifest), which degrades to
    reference-only instead of refusing to boot.

    BOOT-helper except is NARROW (``except Exception``, NOT ``BaseException``): a
    ``KeyboardInterrupt``/``SystemExit`` during the build must ABORT the boot (propagate to
    ``main()``'s handler + ``finally``, which reaps the session), not be swallowed into
    degraded-mode — the deliberate asymmetry with the composite dispatch-path
    outer net, which stays ``except BaseException``.

    This helper does NOT open and does NOT reap: lazy construction touches no engine, and
    ``main()``'s ``finally`` is the single reap owner.
    """
    try:
        return LazyHarnessDispatcher(
            session, call_warn_threshold_s=call_warn_threshold_s
        )
    except Exception as exc:  # noqa: BLE001 — manifest-import failure -> degrade; KI/SystemExit propagate
        _warn(
            f"optivibe: harness manifest UNAVAILABLE — harness tools DISABLED "
            f"({_safe_error_text(exc)})"
        )
        return None


def _build_reference_dispatcher():
    """Build the reference dispatcher, or return ``None`` (warns to stderr).

    Imports ``optivibe_reference`` INSIDE the helper so a missing editable install
    (``pip install -e ../optivibe-reference``) fails loudly here at startup as a
    degraded-mode warning, not at module import. The reference ``Dispatcher()``
    default-constructs from the committed catalog JSON (engine-free, license-free),
    so it stands up with no live engine.

    BOOT-helper except is NARROW (``except Exception``, NOT ``BaseException``) so a
    ``KeyboardInterrupt``/``SystemExit`` raised during the import aborts the boot
    (propagates to ``main()``'s ``finally``) instead of degrading to harness-only.
    The warning text is routed through ``_safe_error_text`` so a broken
    ``__str__`` on the failure cannot turn a clean degrade into a crash.
    """
    try:
        from optivibe_reference.server import Dispatcher as ReferenceDispatcher

        return ReferenceDispatcher()
    except Exception as exc:  # noqa: BLE001 — missing install/data -> degrade; KI/SystemExit propagate
        _warn(
            f"optivibe: reference layer UNAVAILABLE — grounding tools DISABLED "
            f"({_safe_error_text(exc)})"
        )
        return None


def _serve(composite, pipe_in_fd=None, pipe_out_fd=None):
    """Run the MCP serve loop.

    When pipe_in_fd/pipe_out_fd are not None, builds TextIOWrapper stacks
    from the private fds and passes them as explicit stdin/stdout to
    ``stdio_server()``.  When None, calls ``stdio_server()`` with no overrides
    (the pre-fix fallback behavior).

    Pinned against the installed mcp 1.28 SDK: ``stdio_server()``
    is an async context manager yielding ``(read, write)``;
    ``server.run(read, write, server.create_initialization_options())`` —
    ``create_initialization_options`` has defaults, so no hand-built
    ``InitializationOptions``. Runner is ``anyio.run`` (mcp is built on anyio).
    """
    import anyio
    from io import BufferedReader, BufferedWriter, FileIO, TextIOWrapper
    from mcp.server.stdio import stdio_server

    server = build_composite_mcp_server(composite)

    async def _run():
        if pipe_out_fd is not None:
            # Build the TextIOWrapper stacks from the private fds.
            # closefd=False: main()'s finally owns the os.close().
            pipe_in = TextIOWrapper(
                BufferedReader(FileIO(pipe_in_fd, "r", closefd=False)),
                encoding="utf-8", errors="replace",
            )
            pipe_out = TextIOWrapper(
                BufferedWriter(FileIO(pipe_out_fd, "w", closefd=False)),
                encoding="utf-8",
            )
            a_stdin = anyio.wrap_file(pipe_in)
            a_stdout = anyio.wrap_file(pipe_out)
            async with stdio_server(stdin=a_stdin, stdout=a_stdout) as (read, write):
                await server.run(
                    read, write, server.create_initialization_options()
                )
        else:
            # Fallback: no isolation.  Pre-fix behavior.
            async with stdio_server() as (read, write):
                await server.run(
                    read, write, server.create_initialization_options()
                )

    anyio.run(_run)


def main():
    """Boot the single OptiVibe MCP: build each dispatcher, compose, serve, reap.

    Returns a process exit code: ``0`` on a clean serve, ``2`` when BOTH subsystems
    failed to stand up (nothing to serve). The engine is NOT opened at boot (lazy-open);
    OpticStudio opens on the first harness-tool call. The ZOS session is reaped EXACTLY
    ONCE in the ``finally`` regardless of which exit path is taken, and whether or not the
    engine ever opened (L22).
    """
    session = None
    harness = None
    pipe_in_fd, pipe_out_fd = None, None  # init for finally
    try:
        # OS-level stdio isolation — BEFORE any import/construction
        # that can trigger library output (ZemaxSession, matplotlib, .NET FRU).
        try:
            pipe_in_fd, pipe_out_fd = _isolate_stdio()
        except Exception:  # noqa: BLE001 — isolation failure must never crash boot; KI/SystemExit propagate
            _warn("optivibe: stdio isolation FAILED -- proceeding without "
                  "isolation (the hang risk is present)")
            pipe_in_fd, pipe_out_fd = None, None

        # the hang-watchdog §1.6/§4.3/§5: the operator surface — two guarded floats.
        connect_timeout_s = _env_float("OPTIVIBE_CONNECT_TIMEOUT_S", 30.0)
        call_warn_threshold_s = _env_float("OPTIVIBE_CALL_WARN_S", 60.0)
        session = ZemaxSession(
            connect_timeout_s=connect_timeout_s,
            slow_call_threshold_s=call_warn_threshold_s,
        )
        # Pin the design WORKSPACE ROOT once at boot
        # — the folder CC launched the MCP in IS the workspace the design session
        # operates in (no invented ``projects/<name>/`` parallel tree). An explicit
        # ``OPTIVIBE_WORKSPACE_ROOT`` env var is the one escape hatch for when CC's cwd
        # is not the design folder. Capturing cwd at BOOT into the attr (not per-call
        # ``os.getcwd()``) makes it immune to any later ``chdir`` drift. GUARDED: a
        # deleted cwd makes ``os.getcwd()`` raise OSError — leave the attr UNSET so the
        # resolver's tier-4 ``cwd/projects`` fallback (which has its own deleted-cwd
        # guard) takes over rather than crashing boot.
        try:
            session.workspace_root = os.environ.get("OPTIVIBE_WORKSPACE_ROOT") or os.getcwd()
        except OSError:
            pass  # deleted cwd + no env override -> leave unset (tier-4 fallback)

        # Reclaim a stranded seat from a prior hard-killed MCP, BEFORE the
        # first lazy-open so the seat is free when lazy-open later runs. Dead-parent-
        # gated; never touches a live concurrent engine. Best-effort: a bad sweep must
        # never block boot (the symmetric BOOT bookend to the finally TEARDOWN reap).
        try:
            from . import engine_ledger
            sweep = engine_ledger.boot_reap()
            if sweep.reaped or sweep.refused or sweep.pruned or sweep.lock_skipped:
                _warn(f"optivibe: engine-ledger boot sweep — reaped={sweep.reaped} "
                      f"spared(live)={sweep.spared_live_parent} pruned={sweep.pruned} "
                      f"refused={sweep.refused} lock_skipped={sweep.lock_skipped}")
        except Exception:  # noqa: BLE001 — boot reap is best-effort; a bad sweep must never block boot
            pass

        # lazy-boot-logging: the durable InteractionLog is NO LONGER
        # wired at boot. ``session._logger`` stays None until the engine is first opened
        # — ``lazy.LazyHarnessDispatcher._activate_logging`` wires it (and REBINDS the
        # inner Dispatcher's cached ``_logger``) on the first dispatchable harness call,
        # so a reference-only / idle session leaves NO ``optivibe_interactions.jsonl`` on
        # disk. Accepted consequence: a slow REFERENCE-tool call before any engine-open is
        # not breadcrumb-logged (the breadcrumb log exists for the ENGINE hang, which is
        # post-activation). ``workspace_root`` is pinned above so the activation can place
        # the file in the workspace.

        harness = _build_harness_dispatcher(
            session, call_warn_threshold_s=call_warn_threshold_s
        )
        reference = _build_reference_dispatcher()

        if harness is None and reference is None:
            _warn(
                "optivibe: FATAL — neither the harness manifest nor the reference "
                "layer could be stood up; nothing to serve."
            )
            return 2

        pairs = []
        if harness is not None:
            pairs.append(("harness", harness))
        if reference is not None:
            pairs.append(("reference", reference))

        composite = CompositeDispatcher(pairs)
        _serve(composite, pipe_in_fd, pipe_out_fd)
        return 0
    finally:
        # SINGLE-OWNER reap (L22): close the session EXACTLY ONCE on EVERY exit path,
        # whether or not the engine ever opened (close() on a never-opened
        # session is a no-op). Prefer the lazy wrapper's close() (it closes the session
        # it wraps); fall back to session.close() only when the wrapper build returned
        # None (no wrapper, but a ZemaxSession() was constructed). close() is idempotent +
        # never-raises (session.py:268), so this is single-owner and leak-free.
        if harness is not None:
            harness.close()
        elif session is not None:
            session.close()
        # Close the private pipe fds (explicit, not GC-dependent).
        for fd in (pipe_in_fd, pipe_out_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    sys.exit(main())
