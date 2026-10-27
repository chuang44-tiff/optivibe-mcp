#!/usr/bin/env python
"""Glass-search clock hook: a real wall-clock budget for the glass search.

It tells the agent the measured elapsed time after each glass trial and blocks a stop while another trial still
fits the budget. The budget is configurable, per-session state is kept outside the project, and the clock restarts
on every user prompt.

    python glass_clock_hook.py <enforce|log> <prompt|post|stop>      (Claude Code hook JSON on stdin)

The rule: another trial fits while elapsed + 2 x longest trial call <= budget; a partner re-try fits while
elapsed + longest <= budget; at most MAX_BLOCKS (2) Stop blocks per user prompt.

prompt: (UserPromptSubmit) writes t0 = now. Nothing on stdout.
post:   (PostToolUse) logs each trial_glass_swaps / load_design call; in enforce mode prints an additionalContext
        note with the measured clock after a trial or a keep. A session that never ran a trial writes nothing.
stop:   (Stop) allow or block; the decision is logged in both modes, printed (as a block) only in enforce mode.

Environment (read on each call):
    OPTIVIBE_GLASS_BUDGET_MIN   float minutes; unset, empty or unparsable -> 60; 0 or negative -> hook OFF
    OPTIVIBE_GLASS_CLOCK_MODE   enforce | log | off; overrides the command-line mode when set (off = budget 0)
    OPTIVIBE_GLASS_CLOCK_DIR    state root; default <tempdir>/optivibe-glass-clock. State lives in <root>/<session_id>:
                                glass_clock_log.jsonl, glass_clock_log.lock, t0. Nothing is ever written under cwd.

Registrations (settings.json hooks):
    python "$CLAUDE_PROJECT_DIR/.claude/hooks/glass_clock_hook.py" enforce prompt     (UserPromptSubmit)
    python "$CLAUDE_PROJECT_DIR/.claude/hooks/glass_clock_hook.py" enforce post       (PostToolUse, matcher
                                         mcp__optivibe__trial_glass_swaps|mcp__optivibe__load_design)
    python "$CLAUDE_PROJECT_DIR/.claude/hooks/glass_clock_hook.py" enforce stop       (Stop)

Stdlib only (Python 3.9). Fails open: any error -> exit 0, no output, an error event in the log if it can be written.
Trials, keeps, owed debt and coverage are recomputed from the session log on every call; only the Stop-block count
is per prompt (blocks with t >= t0).
"""
import ast
import contextlib
import json
import math
import os
import posixpath
import re
import sys
import tempfile
import time

DEFAULT_BUDGET_MIN = 60.0
DEFAULT_BUDGET_S = DEFAULT_BUDGET_MIN * 60.0
MAX_BLOCKS = 2
TRIAL_TOOL = "mcp__optivibe__trial_glass_swaps"
LOAD_TOOL = "mcp__optivibe__load_design"
LOG_NAME = "glass_clock_log.jsonl"
LOCK_NAME = "glass_clock_log.lock"
START_NAME = "t0"
LOCK_TIMEOUT_S = 10.0
LOCK_STALE_S = 60.0
ENV_BUDGET = "OPTIVIBE_GLASS_BUDGET_MIN"
ENV_MODE = "OPTIVIBE_GLASS_CLOCK_MODE"
ENV_DIR = "OPTIVIBE_GLASS_CLOCK_DIR"


def parse_tool_response(tool_response):
    """The dispatch envelope dict from an MCP tool_response (a list of text blocks holding a Python repr)."""
    if isinstance(tool_response, dict):
        return tool_response
    if isinstance(tool_response, str):
        return ast.literal_eval(tool_response)
    text = "".join(b.get("text", "") for b in tool_response if isinstance(b, dict) and b.get("type") == "text")
    return ast.literal_eval(text)


def surfaces(obj):
    """Every `surface` value in a swaps payload: dicts and (nested) lists of dicts both occur in real calls."""
    out = []
    if isinstance(obj, dict):
        if "surface" in obj:
            out.append(int(obj["surface"]))
    elif isinstance(obj, (list, tuple)):
        for o in obj:
            out.extend(surfaces(o))
    return out


def norm_path(path):
    """Normalised FULL path: forward slashes, `.` / `..` collapsed, lower-cased (candidate paths in a trial result and
    load_design paths are both absolute in real runs; a relative or differently-rooted spelling will not match)."""
    return posixpath.normpath(str(path).replace("\\", "/")).lower()


def candidate_maps(result):
    """({norm candidate_path: sorted kept slots}, [norm control candidate paths]) from an ok trial result dict.
    Agent labels name the files, so a keep is identified by this exact path, never by the file name."""
    cands, controls = {}, []
    if not isinstance(result, dict):
        return cands, controls
    for t in result.get("trials") or []:
        if isinstance(t, dict) and t.get("candidate_path"):
            cands[norm_path(t["candidate_path"])] = sorted(set(surfaces(t.get("swaps"))))
    c = result.get("control")
    if isinstance(c, dict) and c.get("candidate_path"):
        controls.append(norm_path(c["candidate_path"]))
    return cands, controls


def classify_load(events, path):
    """('keep', slots) if `path` is an earlier ok trial's candidate_path, ('control', None) if it is an earlier
    result's control candidate_path, else ('other', None)."""
    n = norm_path(path)
    kind, slots = "other", None
    for ev in events:
        if ev.get("kind") != "trial":
            continue
        if n in (ev.get("controls") or []):
            kind, slots = "control", None
        c = ev.get("candidates") or {}
        if n in c:
            kind, slots = "keep", c[n]
    return kind, slots


def file_mtime(path):
    try:
        return os.path.getmtime(path)
    except BaseException:
        return None


def groups(slots):
    """Cemented groups: runs of consecutive integers in the slot list."""
    out, cur = [], []
    for s in sorted(set(slots or [])):
        if cur and s == cur[-1] + 1:
            cur.append(s)
        else:
            if cur:
                out.append(tuple(cur))
            cur = [s]
    if cur:
        out.append(tuple(cur))
    return out


def owed_for_keep(kept, slots):
    """Partner slots owed after keeping `kept`: the group members not kept (the whole group if all were kept)."""
    owed = set()
    for g in groups(slots):
        hit = set(g) & set(kept)
        if not hit or len(g) < 2:
            continue
        owed |= (set(g) - set(kept)) or set(g)
    return sorted(owed)


def state(events, t0=None):
    """A trial-candidate load (keep) is PENDING until a later trial call confirms it. A later load of a path that is
    neither a candidate nor a control cancels the pending keep (owed set restored) only when the loaded file's mtime
    (recorded in the load event) predates the keep: a revert. A newer file keeps it; no mtime (stat failed) changes
    nothing. A new keep while one is pending replaces it; both candidates were built on the pre-keep lens, so a later
    revert restores the debt from before the FIRST pending keep.

    Only `stop` block events with t >= t0 count toward `blocks` (each user prompt gets its own blocks); with
    t0=None every block counts."""
    st = {"n_trials": 0, "longest": 0.0, "coverage": None, "owed": [], "owed_after": [], "blocks": 0,
          "last_cancelled": False}
    slots = []
    pending = None  # {"owed", "owed_after" (before the unconfirmed keep(s)), "t" (latest keep time)}
    for ev in events:
        k = ev.get("kind")
        st["last_cancelled"] = False
        if k == "trial":
            pending = None
            st["n_trials"] += 1
            if ev.get("ok"):
                st["longest"] = max(st["longest"], float(ev.get("dur_s") or 0.0))
                if ev.get("coverage"):
                    st["coverage"] = ev["coverage"]
                    slots = ev["coverage"].get("slots") or slots
                if st["owed"] and set(ev.get("slots_tried") or []) & set(st["owed"]):
                    st["owed"], st["owed_after"] = [], []
        elif k == "load":
            if pending is not None and ev.get("mtime") is not None:
                if float(ev["mtime"]) < pending["t"]:
                    st["owed"], st["owed_after"] = pending["owed"], pending["owed_after"]
                    st["last_cancelled"] = True
                pending = None  # older file: reverted; newer file: the keep stands
        elif k == "keep":
            if pending is None:
                pending = {"owed": st["owed"], "owed_after": st["owed_after"], "t": float(ev.get("t") or 0.0)}
            else:
                pending["t"] = float(ev.get("t") or 0.0)
            st["owed"] = owed_for_keep(ev.get("slots") or [], slots)
            st["owed_after"] = sorted(ev.get("slots") or []) if st["owed"] else []
        elif k == "stop" and ev.get("decision") == "block":
            if t0 is None or float(ev.get("t") or 0.0) >= t0:
                st["blocks"] += 1
    return st


def _s(slots):
    return ", ".join("S%d" % s for s in slots) if slots else "none"


def _facts(st, elapsed, budget_s=DEFAULT_BUDGET_S):
    cov = st["coverage"]
    longest = st["longest"]
    if cov:
        cov_txt = "sweep %s; untried %s; open %s" % ("done" if cov.get("sweep_done") else "not done",
                                                     _s(cov.get("untried")), _s(cov.get("open")))
    else:
        cov_txt = "none yet"
    txt = ("Bench clock (measured by the bench, not an estimate; report it as total elapsed time): %.1f of %.0f min "
           "elapsed. Longest trial call so far %.1f min. Coverage: %s." % (
               elapsed / 60.0, budget_s / 60.0, longest / 60.0, cov_txt))
    if st["owed"]:
        txt += " Partner re-try owed: %s (after keeping %s). A partner re-try fits while elapsed + longest call <= " \
               "%.0f min: %s." % (_s(st["owed"]), _s(st["owed_after"]), budget_s / 60.0,
                                  "yes" if elapsed + longest <= budget_s else "no")
    txt += " Another trial fits while elapsed + 2 x longest call <= %.0f min: %s." % (
        budget_s / 60.0, "yes" if elapsed + 2 * longest <= budget_s else "no")
    return txt


def note(st, elapsed, budget_s=DEFAULT_BUDGET_S):
    return _facts(st, elapsed, budget_s)


def stop_decision(st, elapsed, budget_s=DEFAULT_BUDGET_S):
    """-> (decision, why, reason_text). decision is 'block' or 'allow'."""
    longest = st["longest"]
    if st["n_trials"] == 0:
        return "allow", "no_trials", None
    if st["blocks"] >= MAX_BLOCKS:
        return "allow", "block_cap", None
    if elapsed >= budget_s:
        return "allow", "out_of_time", None
    cov = st["coverage"]
    owed_fits = bool(st["owed"]) and elapsed + longest <= budget_s
    sweep_fits = bool(cov) and not cov.get("sweep_done") and elapsed + 2 * longest <= budget_s
    if owed_fits:
        which, why = "(a) a partner re-try is owed and fits", "owed_fits"
    elif sweep_fits:
        which, why = "(b) the sweep is not done and another trial fits", "sweep_fits"
    else:
        return "allow", "nothing_fits_or_done", None
    reason = ("Not done yet: %s. %s If you run more trials, the final rest, save and report must be done again on "
              "the final lens." % (which, _facts(st, elapsed, budget_s)))
    return "block", why, reason


# ---- configuration --------------------------------------------------------------------------------------------

def budget_seconds():
    """Budget in seconds from OPTIVIBE_GLASS_BUDGET_MIN (read now). Unset / empty / unparsable / non-finite -> 60 min.
    A value <= 0 is returned as-is (<= 0 means the hook is off)."""
    raw = os.environ.get(ENV_BUDGET, "")
    try:
        v = float(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_BUDGET_S
    if not math.isfinite(v):
        return DEFAULT_BUDGET_S
    return v * 60.0


def effective_mode(arg_mode):
    """OPTIVIBE_GLASS_CLOCK_MODE (enforce|log|off) overrides the argument when set to one of those."""
    env = os.environ.get(ENV_MODE, "").strip().lower()
    return env if env in ("enforce", "log", "off") else arg_mode


def sanitise_session(sid):
    s = re.sub(r"[^A-Za-z0-9_-]", "", str(sid)) if sid is not None else ""
    return s or "nosession"


def state_dir(session_id):
    root = os.environ.get(ENV_DIR) or os.path.join(tempfile.gettempdir(), "optivibe-glass-clock")
    return os.path.join(root, sanitise_session(session_id))


# ---- io -------------------------------------------------------------------------------------------------------

def log_path(sdir):
    return os.path.join(sdir, LOG_NAME)


def read_log(sdir):
    out = []
    p = log_path(sdir)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return out


def append_log(sdir, ev):
    with open(log_path(sdir), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")


@contextlib.contextmanager
def locked(sdir):
    """Hold <sdir>/glass_clock_log.lock (O_CREAT|O_EXCL) around one read-decide-append transaction. Spins up to
    LOCK_TIMEOUT_S, breaks a lock older than LOCK_STALE_S, and on timeout proceeds WITHOUT the lock (fail open),
    logging an error event. Yields whether the lock is held; always releases in `finally`."""
    path = os.path.join(sdir, LOCK_NAME)
    held = False
    deadline = time.time() + LOCK_TIMEOUT_S
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > LOCK_STALE_S:
                    os.remove(path)
                    continue
            except OSError:
                pass  # a lock we cannot read or remove: wait and retry, bounded by the deadline
        if time.time() >= deadline:
            append_log(sdir, {"kind": "error", "t": time.time(), "why": "lock_timeout"})
            break
        time.sleep(0.05)
    try:
        yield held
    finally:
        if held:
            try:
                os.remove(path)
            except OSError:
                pass


def write_t0(sdir, now):
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, START_NAME), "w", encoding="utf-8") as fh:
        fh.write(repr(now))


def read_t0(sdir, now):
    """This prompt's t0; a missing or unreadable t0 (hook enabled mid-prompt) is written as `now` and used."""
    try:
        with open(os.path.join(sdir, START_NAME), encoding="utf-8") as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        write_t0(sdir, now)
        return now


def handle_prompt(data, mode, now, budget_s, sdir):
    write_t0(sdir, now)
    return None


def handle_post(data, mode, now, budget_s, sdir):
    tool = data.get("tool_name")
    if tool == LOAD_TOOL and not os.path.isfile(log_path(sdir)):
        return None  # a session that never searched glass writes nothing
    if tool not in (TRIAL_TOOL, LOAD_TOOL):
        return None
    os.makedirs(sdir, exist_ok=True)
    t0 = read_t0(sdir, now)
    ev = None
    if tool == TRIAL_TOOL:
        env = parse_tool_response(data["tool_response"])
        res = env.get("result") if isinstance(env.get("result"), dict) else {}
        ok = bool(env.get("ok")) and bool(res.get("ok"))
        slots = surfaces((data.get("tool_input") or {}).get("swaps"))
        if ok:
            slots += surfaces([t.get("swaps") for t in res.get("trials") or [] if isinstance(t, dict)])
        slots = sorted(set(slots))
        cands, controls = candidate_maps(res) if ok else ({}, [])
        ev = {"kind": "trial", "t": now, "dur_s": float(data.get("duration_ms") or 0.0) / 1000.0, "ok": ok,
              "slots_tried": slots, "coverage": res.get("coverage") if ok else None,
              "candidates": cands, "controls": controls}
    elif tool == LOAD_TOOL:
        path = (data.get("tool_input") or {}).get("path", "")
        kind, slots = classify_load(read_log(sdir), path)
        if kind == "keep":
            ev = {"kind": "keep", "t": now, "slots": slots, "path": path}
        elif kind == "other":  # a control load is neutral: no event
            ev = {"kind": "load", "t": now, "path": path, "mtime": file_mtime(path)}
    if ev is None:
        return None
    append_log(sdir, ev)
    if mode != "enforce" or ev["kind"] == "load" and not state(read_log(sdir), t0)["last_cancelled"]:
        return None
    msg = note(state(read_log(sdir), t0), now - t0, budget_s)
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}})


def handle_stop(data, mode, now, budget_s, sdir):
    if not os.path.isfile(log_path(sdir)):
        return None  # a session that never searched glass: allow, write nothing
    t0 = read_t0(sdir, now)
    st = state(read_log(sdir), t0)
    elapsed = now - t0
    decision, why, reason = stop_decision(st, elapsed, budget_s)
    append_log(sdir, {"kind": "stop", "t": now, "decision": decision, "why": why,
                      "facts": {"elapsed_s": round(elapsed, 1), "longest_s": st["longest"],
                                "n_trials": st["n_trials"], "owed": st["owed"], "blocks": st["blocks"],
                                "sweep_done": (st["coverage"] or {}).get("sweep_done")}})
    if mode == "enforce" and decision == "block":
        return json.dumps({"decision": "block", "reason": reason})
    return None


HANDLERS = {"prompt": handle_prompt, "post": handle_post, "stop": handle_stop}


def main(argv):
    sdir = None
    try:
        mode, which = effective_mode(argv[1]), argv[2]
        budget_s = budget_seconds()
        if mode == "off" or budget_s <= 0:
            return 0
        data = json.loads(sys.stdin.read())
        sdir = state_dir(data.get("session_id"))
        fn = HANDLERS[which]
        if which == "prompt":
            os.makedirs(sdir, exist_ok=True)
        elif which == "stop" or data.get("tool_name") == LOAD_TOOL:
            if not os.path.isfile(log_path(sdir)):
                return 0  # no log: nothing to decide, nothing to write (not even the lock)
        elif data.get("tool_name") != TRIAL_TOOL:
            return 0
        else:
            os.makedirs(sdir, exist_ok=True)
        with locked(sdir):
            out = fn(data, mode, time.time(), budget_s, sdir)
        if out:
            sys.stdout.write(out + "\n")
    except BaseException as exc:  # fail open, always
        try:
            if sdir and os.path.isdir(sdir):
                append_log(sdir, {"kind": "error", "t": time.time(), "error": repr(exc)[:500]})
        except BaseException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
