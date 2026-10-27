"""tools/engine_release.py — release_engine: hand the single OpticStudio seat back.

An idle server cannot know its client was abandoned, so the agent releases the engine
itself when it is done with engine work. The handler closes THIS session's engine through
the session's one close body (``_close_locked``: ``CloseApplication()`` + the reap of the
session's OWN tracked engine PIDs + the ledger ``unrecord``), then VERIFIES that every
(pid, create_time) this session started is gone (a recycled PID counts as exited).

Why ``_close_locked`` and not ``close()``: dispatch already holds ``session._lock`` on this
thread while the handler runs, and ``close()`` refuses a same-thread caller
(``SessionMisuseError``). ``_close_locked`` is the body ``close()`` runs after that guard,
so there is still ONE close path. Dispatch is serialized on that lock, so no other call can
be in flight while this handler runs.

``kill_others`` (default false; the agent asks the user first): after the own release (or
when none was open) it kills every OTHER OpticStudio SERVER engine on the machine, busy or
idle, and their unsaved lens is lost. A server engine is an ``OpticStudio.exe`` whose
command line carries ``-server`` (headless ZOS-API servers carry ``-server -pid="<owner>"``);
an OpticStudio WITHOUT ``-server`` is a person's interactive GUI and is NEVER touched (it is
listed as ``interactive_skipped``). Identity is ``(pid, create_time)`` captured at
enumeration; the kill goes through ``process_reaper.terminate_tracked`` whose create_time
guard re-checks it, and an engine-ledger row is removed only if its recorded create_time
matches. This session's own pids are never targets.

After the release ``_app`` is None, so the next harness call cold-opens a fresh engine
exactly as on a never-opened session (the lazy dispatcher's cold-open branch). A session
whose channel was observed dead never reaches this handler: the dispatcher's channel gate
refuses it first, and only a process restart clears that.
"""
import re
import time

import psutil

from .. import engine_ledger, process_reaper
from ..errors import ToolError, ToolParamError
from ..server import ToolSpec
from . import _analysis_common as _ac

# How long to wait for the engine process(es) to exit after the close, and the poll step.
_VERIFY_TIMEOUT_S = 10.0
_VERIFY_INTERVAL_S = 0.25

# Seams so tests drive the verification without real processes or real waiting.
_pid_exists = psutil.pid_exists
_monotonic = time.monotonic
_sleep = time.sleep
_PID_ARG = re.compile(r'-pid="?(\d+)', re.IGNORECASE)


def _create_time(pid):
    return float(psutil.Process(int(pid)).create_time())


def _enumerate_opticstudio():
    """``[{pid, create_time, cmdline}]`` for every live OpticStudio.exe. Raises on a
    wholesale failure; a process that vanishes or denies access mid-scan is skipped."""
    out = []
    for proc in psutil.process_iter(["pid", "name", "create_time", "cmdline"]):
        try:
            info = proc.info
            if (info.get("name") or "").lower() != process_reaper.ENGINE_PROCESS_NAME.lower():
                continue
            out.append({"pid": int(info["pid"]),
                        "create_time": info.get("create_time"),  # None when denied
                        # None = the command line was DENIED (unknown); [] = read, and empty
                        "cmdline": None if info.get("cmdline") is None else list(info["cmdline"])})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


_terminate = process_reaper.terminate_tracked


class EngineReleaseError(ToolError):
    """An engine process was still alive after the close / kill, or could not be listed."""

    error_family = "engine_release_failed"


def _still_running(idents):
    """PIDs from ``[(pid, create_time)]`` that still name the SAME process. A PID whose
    create_time differs was recycled by the OS: the engine exited, so it counts as gone."""
    alive = []
    for pid, ct in idents:
        if not _pid_exists(pid):
            continue
        if not ct:  # None / 0.0: identity unreadable, so exit is unprovable (never "recycled")
            alive.append(pid)
            continue
        try:
            if _create_time(pid) != ct:
                continue
        except psutil.NoSuchProcess:
            continue
        except Exception:  # noqa: BLE001 — cannot read it: cannot prove it exited
            pass
        alive.append(pid)
    return alive


def _wait_gone(idents):
    """Poll until every identity is gone or the verify window ends; the survivors."""
    deadline = _monotonic() + _VERIFY_TIMEOUT_S
    alive = _still_running(idents)
    while alive and _monotonic() < deadline:
        _sleep(_VERIFY_INTERVAL_S)
        alive = _still_running(idents)
    return alive


def _is_server(cmdline):
    """EXACT switch: an argument equal to ``-server`` (case-insensitive). ``-serverless``,
    ``-server=false`` or ``-server-x.zmx`` are NOT the switch: that process is interactive."""
    return any(isinstance(a, str) and a.lower() == "-server" for a in cmdline)


def _owner_pid(cmdline):
    for a in cmdline:
        m = _PID_ARG.match(str(a))
        if m:
            return int(m.group(1))
    return None


def _forget_ledger_rows(idents):
    """Drop the engine-ledger row of each killed ``(pid, create_time)`` ONLY if its recorded
    create_time matches (a recycled PID's new row survives). Best-effort; NEVER raises."""
    try:
        with engine_ledger.ledger_lock() as held:
            if not held:
                return
            current, raw, fault = engine_ledger._read_ledger_status()
            if fault:
                return
            changed = False
            for pid, ct in idents:
                rec = current.get(pid)
                if rec is not None and float(rec.engine_create_time) == float(ct):
                    del current[pid]
                    changed = True
            if changed:
                engine_ledger._atomic_write(current, raw)
    except Exception:  # noqa: BLE001
        pass


def _call_terminate(pid, ct, errors):
    """One terminate call; a raise is recorded in ``errors`` and never escapes. Returns the
    reaper's result, or None after a raise."""
    try:
        return _terminate(pid, ct, baseline_pids=())
    except Exception as exc:  # noqa: BLE001 — one bad target must not stop the loop
        errors[pid] = f"{type(exc).__name__}: {exc}"
        return None


def _kill_others(own_idents):
    """Kill every other OpticStudio SERVER engine; returns the report dict (survivors in
    ``still_running``). Own engines are excluded by ``(pid, create_time)``, so a foreign
    engine that reuses a stale own pid is a target. A server engine whose create_time is
    unreadable is never killed (identity unprovable) and counts as still running; only PIDs
    actually signalled are waited on, the rest get one immediate liveness read. Raises
    ``EngineReleaseError`` only when the listing itself fails, in which case nothing was
    killed."""
    own = {(int(p), float(ct)) for p, ct in own_idents}
    try:
        procs = _enumerate_opticstudio()
    except Exception as exc:  # noqa: BLE001 — cannot list: kill nothing
        raise EngineReleaseError(
            f"could not list OpticStudio processes ({type(exc).__name__}); "
            "nothing was killed") from None
    targets, interactive, unreadable = [], [], []
    for p in procs:
        ct = p["create_time"]
        if ct and (p["pid"], float(ct)) in own:
            continue
        if p["cmdline"] is None:  # denied: neither a proven server nor a proven GUI
            unreadable.append(p)
        elif _is_server(p["cmdline"]):
            targets.append(p)
        else:
            interactive.append(p["pid"])
    killed, idents, signalled, unverifiable, errors = [], [], [], [], {}
    for p in targets:
        ct = p["create_time"]
        if not ct:
            unverifiable.append(p["pid"])  # never kill what cannot be identified
            idents.append((p["pid"], 0.0))
            continue
        res = _call_terminate(p["pid"], float(ct), errors)
        idents.append((p["pid"], float(ct)))
        if res is not None and getattr(res, "ok", False):
            # ok also covers already_gone / no_such_process: waiting on those returns at once
            signalled.append((p["pid"], float(ct)))
            killed.append({"pid": p["pid"], "owner_pid": _owner_pid(p["cmdline"])})
    for p in unreadable:  # never killed, but it may still hold the seat: verify it like the rest
        idents.append((p["pid"], float(p["create_time"] or 0.0)))
    waited = _wait_gone(signalled) if signalled else []
    once = _still_running([i for i in idents if i not in signalled])  # one read, no waiting
    alive = sorted(set(waited) | set(once))
    _forget_ledger_rows([i for i in idents if i[0] not in alive and i[1]])
    report = {"killed": [k for k in killed if k["pid"] not in alive],
              "still_running": sorted(alive),
              "interactive_skipped": sorted(interactive)}
    if unverifiable:
        report["unverifiable_identity"] = sorted(unverifiable)
    if unreadable:
        report["cmdline_unreadable"] = sorted(p["pid"] for p in unreadable)
    if errors:
        report["terminate_errors"] = {str(k): v for k, v in sorted(errors.items())}
    return report


def _tracked_idents(session):
    """``{pid: create_time}`` this session tracks, whether or not it is open."""
    try:
        return {int(p): float(ct) for p, ct in dict(session._tracked).items()}
    except Exception:  # noqa: BLE001
        return {}


def _failure(message, result):
    """The structured partial outcome beside ``ok: false`` / ``engine_release_failed``."""
    return _ac.error_envelope("release_engine", EngineReleaseError.error_family, message,
                              **result)


def release_engine(session, params):
    """Close this session's engine, verify its own process(es) exited, optionally kill others."""
    if "kill_others" in params and type(params["kill_others"]) is not bool:
        raise ToolParamError(
            f"kill_others must be a boolean (true or false), got {params['kill_others']!r}; "
            "nothing was closed")
    kill = params.get("kill_others") is True

    # Own identities = everything the session tracked BEFORE the close plus everything it
    # promoted DURING it (close's _resolve_unresolved adds some), open or not: a closed
    # session may still retain a live own engine. All are verified and none is foreign.
    before = _tracked_idents(session)
    was_open = bool(session.is_open)
    # Unconditional: on an already-closed session the close body re-attributes engines whose
    # owner could not be identified at the first close and reaps what resolves. On a
    # never-opened session it opens nothing (``_app`` is None; the connect sweep is a no-op
    # without a prior open attempt); its only effect is ``_closed = True``, which ``open()``
    # resets.
    session._close_locked()
    idents = sorted(set(before.items()) | set(_tracked_idents(session).items()))
    pids = sorted({p for p, _ in idents})

    alive = _wait_gone(idents) if idents else []
    result_errors = {}
    if alive:
        # RETRY the cleanup for the retained own identities (a closed session, or a close
        # whose reap failed, keeps them in ``_tracked``): the reaper keeps its create_time
        # guard, so a recycled pid is refused, never killed. Then verify again.
        ct_of = dict(idents)
        errors = {}
        for pid in alive:
            _call_terminate(pid, ct_of[pid], errors)
        alive = _wait_gone([(p, ct_of[p]) for p in alive])
        if errors:
            result_errors = {str(k): v for k, v in sorted(errors.items())}
    result = {"engine_was_open": was_open, "released": was_open and not alive}
    if alive and result_errors:
        result["terminate_errors"] = result_errors
    if was_open or idents:
        result["engine_pids"] = pids
        result["still_running"] = alive
    if alive:
        return _failure(
            f"the engine was closed but its process(es) {alive} are still running "
            f"after {_VERIFY_TIMEOUT_S:g} s; the seat may not be free yet", result)
    if kill:
        try:
            report = _kill_others(idents)
        except EngineReleaseError as exc:
            return _failure(str(exc), result)
        result["kill_others"] = report
        if report["still_running"]:
            return _failure(
                f"kill_others: OpticStudio server engine process(es) "
                f"{report['still_running']} are still running (a signalled engine is checked "
                f"after up to {_VERIFY_TIMEOUT_S:g} s, one never signalled on a single read); "
                f"already killed: "
                f"{[k['pid'] for k in report['killed']]}", result)
    return result


TOOL_SPEC = ToolSpec(
    name="release_engine",
    handler=release_engine,
    required_params=(),
    param_types={"kill_others": "boolean"},
    description=(
        "Close this server's OpticStudio engine and confirm its process has exited, so "
        "the single seat is free for another session or bench. Call it when you are done "
        "with engine work and something else needs the seat. The lens held in memory is "
        "lost, so save first (save_candidate or save_snapshot). The next harness call "
        "reopens an engine automatically. If no engine is open it does nothing and opens "
        "none. Returns engine_was_open, released, engine_pids and still_running; if a "
        "process is still alive after the wait the call fails with "
        "error_family engine_release_failed. "
        "kill_others: also kills every other session's OpticStudio server engine on this "
        "machine, busy or not, and their unsaved lens is lost; ask the user before using "
        "it. A person's interactive OpticStudio is never touched. An OpticStudio whose "
        "command line cannot be read is never killed either; it is listed in "
        "cmdline_unreadable and counts as still running."
    ),
)
