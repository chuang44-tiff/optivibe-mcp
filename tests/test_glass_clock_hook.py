"""Tests for the glass-search clock hook in .claude/hooks/ (run with pytest from the repository root)."""
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
HOOK_DIR = os.path.join(REPO, ".claude", "hooks")
HOOK = os.path.join(HOOK_DIR, "glass_clock_hook.py")
FIXTURE = os.path.join(HERE, "fixtures", "glass_clock_trial_ok_10x.json")
# Loaded by file path: the hook is a standalone Claude Code script under .claude/hooks/, not an
# installed module, so it is never put on sys.path.
_spec = importlib.util.spec_from_file_location("glass_clock_hook", HOOK)
h = importlib.util.module_from_spec(_spec)
sys.modules["glass_clock_hook"] = h
_spec.loader.exec_module(h)

B = h.DEFAULT_BUDGET_S
SID = "sess"
SLOTS = [1, 2, 5, 6, 8, 9, 11, 12, 14, 15, 16, 18, 20, 22]


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def cov(sweep_done=False, slots=SLOTS, untried=(), open_=()):
    return {"slots": list(slots), "at_rest": [], "open": list(open_), "untried": list(untried),
            "sweep_done": sweep_done, "never_tried": [], "search_wall_s": 600.0, "summary": ""}


def trial(dur=300.0, ok=True, tried=(12,), coverage="default", t=0.0):
    c = cov() if coverage == "default" else coverage
    return {"kind": "trial", "t": t, "dur_s": dur, "ok": ok, "slots_tried": list(tried),
            "coverage": c if ok else None}


def keep(slots, t=0.0):
    return {"kind": "keep", "t": t, "slots": list(slots)}


def stop(decision="block", t=0.0):
    return {"kind": "stop", "t": t, "decision": decision, "why": "x", "facts": {}}


# ---- plumbing --------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clock_env(tmp_path, monkeypatch):
    """Every test gets its own state root; the three hook variables are cleared first."""
    monkeypatch.delenv(h.ENV_BUDGET, raising=False)
    monkeypatch.delenv(h.ENV_MODE, raising=False)
    monkeypatch.setenv(h.ENV_DIR, str(tmp_path / "clock"))


@pytest.fixture
def sdir(tmp_path):
    """The state dir of session SID (created)."""
    d = tmp_path / "clock" / SID
    d.mkdir(parents=True)
    return d


@pytest.fixture
def cwd(tmp_path):
    """The hook's cwd: must stay empty (state never lands under the project)."""
    d = tmp_path / "cwd"
    d.mkdir()
    return d


def run_hook(args, stdin, extra_env=None):
    env = os.environ.copy()
    env.update(extra_env or {})
    return subprocess.run([sys.executable, HOOK] + args, input=stdin, capture_output=True, text=True, env=env,
                          timeout=60)


def _post_input(cwd_, tool, tool_input, resp, dur_ms=1000, sid=SID):
    return json.dumps({"session_id": sid, "cwd": str(cwd_), "hook_event_name": "PostToolUse", "tool_name": tool,
                       "tool_input": tool_input, "tool_response": resp, "duration_ms": dur_ms})


def _stop_input(cwd_, sid=SID):
    return json.dumps({"session_id": sid, "cwd": str(cwd_), "hook_event_name": "Stop", "stop_hook_active": False})


def _prompt_input(cwd_, sid=SID):
    return json.dumps({"session_id": sid, "cwd": str(cwd_), "hook_event_name": "UserPromptSubmit", "prompt": "go"})


def _write_t0(sdir_, value):
    (sdir_ / h.START_NAME).write_text(repr(value))


def _seed_log(sdir_, events):
    with open(os.path.join(str(sdir_), h.LOG_NAME), "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _events(sdir_):
    return [json.loads(x) for x in (sdir_ / h.LOG_NAME).read_text().splitlines()]


# =================================================================================================================
# Part 1: the clock rule
# =================================================================================================================

# ---- parsing ---------------------------------------------------------------------------------------------------

def test_parse_real_tool_response():
    fx = load_fixture()
    env = h.parse_tool_response(fx["tool_response"])
    assert env["ok"] is True and env["tool"] == "trial_glass_swaps"
    res = env["result"]
    assert res["ok"] is True
    assert res["coverage"]["slots"] == SLOTS
    assert res["coverage"]["sweep_done"] is False
    assert res["coverage"]["untried"] == [6, 8, 11, 14, 20, 22]
    assert res["trials"][0]["swaps"][0]["surface"] == 12


def test_parse_refusal_repr():
    text = ("{'ok': True, 'tool': 'trial_glass_swaps', 'result': {'ok': False, 'error_family': 'x', "
            "'reason': 'catalog_would_not_bind', 'rebind': [{'set': 1, 'binds': None}]}}")
    env = h.parse_tool_response([{"type": "text", "text": text}])
    assert env["result"]["ok"] is False and "coverage" not in env["result"]


def test_surfaces_flat_nested_and_mixed():
    d = lambda n: {"surface": n, "glass": "g", "catalog": "c"}  # noqa: E731
    assert h.surfaces([d(1), d(2)]) == [1, 2]
    assert h.surfaces([[d(1), d(2)], d(5), [d(9)]]) == [1, 2, 5, 9]
    assert h.surfaces(None) == [] and h.surfaces([]) == []


def test_post_nested_swaps_logged(sdir, cwd):
    _write_t0(sdir, time.time())
    d = lambda n: {"surface": n, "glass": "g", "catalog": "c"}  # noqa: E731
    resp = [{"type": "text", "text": repr({"ok": True, "result": {"ok": True, "coverage": cov(), "trials": []}})}]
    r = run_hook(["log", "post"], _post_input(cwd, h.TRIAL_TOOL, {"swaps": [[d(1), d(2)], d(5)]}, resp))
    assert r.returncode == 0
    ev = json.loads((sdir / h.LOG_NAME).read_text().splitlines()[0])
    assert ev["kind"] == "trial" and ev["slots_tried"] == [1, 2, 5]


# ---- candidate paths (exact keep identification) / groups -------------------------------------------------------

CAND = "D:/R/candidates/zmx/glass10x_019_t3_s1-nbh59_s2-pk51.zmx"  # an agent-chosen label, not glass_trial_*
CTRL = "D:/R/candidates/zmx/glass10x_022_control.zmx"


def test_candidate_maps_from_real_result():
    fx = load_fixture()
    cands, controls = h.candidate_maps(h.parse_tool_response(fx["tool_response"])["result"])
    assert len(cands) == 4 and sorted(v[0] for v in cands.values()) == [12, 15, 16, 18]
    assert all("/" in k and k == k.lower() for k in cands)
    assert len(controls) == 1 and controls[0].endswith("_007_control.zmx")


def test_classify_load_exact_path_not_file_name():
    ev = [{"kind": "trial", "ok": True, "candidates": {h.norm_path(CAND): [1, 2]}, "controls": [h.norm_path(CTRL)]}]
    assert h.classify_load(ev, CAND.replace("/", chr(92)).upper()) == ("keep", [1, 2])
    assert h.classify_load(ev, CTRL) == ("control", None)
    assert h.classify_load(ev, "D:/R/candidates/zmx/glass_trial_10x_008_t1_S12_N-KZFS4.zmx") == ("other", None)
    assert h.classify_load([], CAND) == ("other", None)
    assert h.classify_load(ev, "") == ("other", None)


def test_groups():
    assert h.groups(SLOTS) == [(1, 2), (5, 6), (8, 9), (11, 12), (14, 15, 16), (18,), (20,), (22,)]
    assert h.groups([]) == []


def test_owed_for_keep():
    assert h.owed_for_keep([11], SLOTS) == [12]
    assert h.owed_for_keep([15], SLOTS) == [14, 16]
    assert h.owed_for_keep([11, 12], SLOTS) == [11, 12]  # kept fills the group: whole group
    assert h.owed_for_keep([18], SLOTS) == []  # singlet owes nothing
    assert h.owed_for_keep([1, 20], SLOTS) == [2]


# ---- state: owed set and its clearing --------------------------------------------------------------------------

def test_owed_set_after_keep():
    st = h.state([trial(), keep([11])])
    assert st["owed"] == [12] and st["owed_after"] == [11]


def test_owed_cleared_by_ok_trial_including_owed_slot():
    st = h.state([trial(), keep([11]), trial(tried=(12, 14))])
    assert st["owed"] == []


def test_owed_not_cleared_by_other_slot():
    st = h.state([trial(), keep([11]), trial(tried=(14, 20))])
    assert st["owed"] == [12]


def test_owed_not_cleared_by_refusal():
    st = h.state([trial(), keep([11]), trial(ok=False, tried=(12,), dur=3.0)])
    assert st["owed"] == [12]
    assert st["longest"] == 300.0  # a refusal's 3 s does not count as a trial call


def test_new_keep_replaces_debt():
    st = h.state([trial(), keep([11]), keep([15])])
    assert st["owed"] == [14, 16] and st["owed_after"] == [15]


def test_singlet_keep_owes_nothing():
    assert h.state([trial(), keep([18])])["owed"] == []


def test_state_counts():
    st = h.state([trial(dur=100), trial(dur=420), trial(ok=False, dur=3), stop("block"), stop("allow"), stop("block")])
    assert st["n_trials"] == 3 and st["longest"] == 420 and st["blocks"] == 2


# ---- stop_decision rows ----------------------------------------------------------------------------------------

def dec(events, elapsed):
    return h.stop_decision(h.state(events), elapsed)


def test_stop_no_trials_allows():
    assert dec([], 600)[:2] == ("allow", "no_trials")
    assert dec([keep([11])], 600)[:2] == ("allow", "no_trials")


def test_stop_block_cap_allows():
    ev = [trial(coverage=cov()), keep([11]), stop("block"), stop("block")]
    assert dec(ev, 600)[:2] == ("allow", "block_cap")


def test_stop_out_of_time_allows():
    ev = [trial(), keep([11])]
    assert dec(ev, B)[:2] == ("allow", "out_of_time")
    assert dec(ev, B + 5)[:2] == ("allow", "out_of_time")


def test_stop_owed_fits_blocks():
    ev = [trial(dur=300, coverage=cov(sweep_done=True)), keep([11])]
    d, why, reason = dec(ev, B - 300)  # elapsed + longest == budget: fits
    assert (d, why) == ("block", "owed_fits")
    assert "(a)" in reason and reason.endswith(
        "If you run more trials, the final rest, save and report must be done again on the final lens.")


def test_stop_owed_does_not_fit_allows():
    ev = [trial(dur=300, coverage=cov(sweep_done=True)), keep([11])]
    assert dec(ev, B - 299)[:2] == ("allow", "nothing_fits_or_done")


def test_stop_sweep_not_done_fits_blocks():
    ev = [trial(dur=300, coverage=cov(untried=[6]))]
    d, why, reason = dec(ev, B - 600)  # elapsed + 2 x longest == budget: fits
    assert (d, why) == ("block", "sweep_fits") and "(b)" in reason


def test_stop_sweep_not_done_does_not_fit_allows():
    ev = [trial(dur=300, coverage=cov(untried=[6]))]
    assert dec(ev, B - 599)[:2] == ("allow", "nothing_fits_or_done")


def test_stop_sweep_done_nothing_owed_allows():
    ev = [trial(dur=300, coverage=cov(sweep_done=True))]
    assert dec(ev, 600)[:2] == ("allow", "nothing_fits_or_done")


def test_stop_refusal_only_never_blocks():
    ev = [trial(ok=False, dur=3.0)]
    assert dec(ev, 600)[:2] == ("allow", "nothing_fits_or_done")


def test_owed_beats_nothing_when_sweep_done_but_fits_only_owed():
    # owed fits (elapsed + longest) while a further trial (2 x longest) does not: still (a)
    ev = [trial(dur=400, coverage=cov(sweep_done=True)), keep([1])]
    assert dec(ev, B - 500)[:2] == ("block", "owed_fits")


# ---- note ------------------------------------------------------------------------------------------------------

def test_note_text():
    st = h.state([trial(dur=468, coverage=cov(untried=[1, 5])), keep([11])])
    n = h.note(st, 41.3 * 60)
    assert "41.3 of 60 min elapsed" in n and "Longest trial call so far 7.8 min" in n
    assert "untried S1, S5; open none" in n
    assert "Partner re-try owed: S12 (after keeping S11)" in n
    assert "A partner re-try fits while elapsed + longest call <= 60 min: yes" in n
    assert "Another trial fits while elapsed + 2 x longest call <= 60 min: yes" in n  # 41.3 + 15.6 = 56.9
    n2 = h.note(st, 47 * 60)  # 47 + 7.8 = 54.8 fits; 47 + 15.6 = 62.6 does not
    assert "partner re-try fits while elapsed + longest call <= 60 min: yes" in n2.replace("A partner", "partner")
    assert "Another trial fits while elapsed + 2 x longest call <= 60 min: no" in n2


def test_note_no_partner_sentence_when_nothing_owed():
    n = h.note(h.state([trial()]), 600)
    assert "Partner" not in n and "Another trial fits" in n


# ---- script-level: modes, fail-open ----------------------------------------------------------------------------

def test_log_mode_prints_nothing_even_on_block(sdir, cwd):
    _write_t0(sdir, time.time() - 600)
    _seed_log(sdir, [trial(dur=300, coverage=cov(untried=[6]))])
    r = run_hook(["log", "stop"], _stop_input(cwd))
    assert r.returncode == 0 and r.stdout == ""
    last = _events(sdir)[-1]
    assert last["kind"] == "stop" and last["decision"] == "block"


def test_enforce_mode_prints_block(sdir, cwd):
    _write_t0(sdir, time.time() - 600)
    _seed_log(sdir, [trial(dur=300, coverage=cov(untried=[6]))])
    r = run_hook(["enforce", "stop"], _stop_input(cwd))
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["decision"] == "block" and "(b)" in out["reason"]


def test_enforce_post_note_and_log(sdir, cwd):
    _write_t0(sdir, time.time() - 600)
    fx = load_fixture()
    r = run_hook(["enforce", "post"], _post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"],
                                                   fx["duration_ms"]))
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse" and "sweep not done" in out["additionalContext"]
    ev = json.loads((sdir / h.LOG_NAME).read_text().splitlines()[0])
    assert ev["kind"] == "trial" and ev["ok"] is True and ev["slots_tried"] == [12, 15, 16, 18]
    assert abs(ev["dur_s"] - 243.06249) < 1e-3


def ld(path, mtime):
    return {"kind": "load", "t": 0.0, "path": path, "mtime": mtime}


def kp(slots, t=100.0, path=CAND):
    return {"kind": "keep", "t": t, "slots": list(slots), "path": path}


BASE = "D:/R/candidates/trail/snapshots/0000_screen_base.zmx"


def test_keep_then_load_of_older_file_cancels_debt():
    # the 3469dfe1 shape: snapshot saved BEFORE the keep (mtime 50 < keep 100), loaded after the keep
    st = h.state([trial(), kp([11]), ld(BASE, 50.0)])
    assert st["owed"] == [] and st["last_cancelled"] is True


def test_keep_then_load_of_newer_file_keeps():
    # the 9790a0d0 shape: 0001_pass00_after.zmx was written after the keep
    st = h.state([trial(), kp([11]), ld(BASE, 150.0)])
    assert st["owed"] == [12] and st["last_cancelled"] is False
    assert h.state([trial(), kp([11]), ld(BASE, 150.0), ld(BASE, 50.0)])["owed"] == [12]  # keep stands for good


def test_keep_then_load_with_stat_failure_does_not_cancel():
    st = h.state([trial(), kp([11]), ld(BASE, None)])
    assert st["owed"] == [12] and st["last_cancelled"] is False


def test_second_keep_replaces_pending_and_cancel_restores_pre_first_keep():
    st = h.state([trial(), kp([11], t=100.0), kp([15], t=110.0)])
    assert st["owed"] == [14, 16] and st["owed_after"] == [15]  # the second keep's own debt
    st = h.state([trial(), kp([11], t=100.0), kp([15], t=110.0), ld(BASE, 105.0)])
    assert st["owed"] == [] and st["last_cancelled"] is True  # 105 < latest keep time 110; debt from before keep 11
    st = h.state([trial(), kp([11]), trial(ok=False, tried=(5,)), kp([15], t=110.0), ld(BASE, 50.0)])
    assert st["owed"] == [12]  # keep 11 was confirmed by a trial call; only keep 15 is cancelled


def test_trial_call_confirms_keep_so_later_load_does_not_cancel():
    st = h.state([trial(), kp([11]), trial(ok=False, tried=(5,)), ld(BASE, 50.0)])
    assert st["owed"] == [12]


def test_load_without_pending_keep_is_inert():
    st = h.state([trial(), ld(BASE, 50.0)])
    assert st["owed"] == [] and st["last_cancelled"] is False


def _trial_log_event(cands, controls):
    ev = trial(coverage=cov(untried=[6]))
    ev["candidates"], ev["controls"] = cands, controls
    return ev


def test_post_agent_label_candidate_is_a_keep_and_control_is_neutral(sdir, cwd):
    _write_t0(sdir, time.time())
    _seed_log(sdir, [_trial_log_event({h.norm_path(CAND): [1]}, [h.norm_path(CTRL)])])
    r = run_hook(["enforce", "post"], _post_input(cwd, h.LOAD_TOOL, {"path": CTRL}, []))
    assert r.returncode == 0 and r.stdout == "" and len((sdir / h.LOG_NAME).read_text().splitlines()) == 1
    r = run_hook(["enforce", "post"], _post_input(cwd, h.LOAD_TOOL, {"path": CAND}, []))
    assert "Partner re-try owed: S2 (after keeping S1)" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    ev = _events(sdir)[-1]
    assert ev["kind"] == "keep" and ev["slots"] == [1]


def test_post_trial_event_stores_candidate_map(sdir, cwd):
    _write_t0(sdir, time.time())
    fx = load_fixture()
    run_hook(["log", "post"], _post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"], 1000))
    ev = json.loads((sdir / h.LOG_NAME).read_text().splitlines()[0])
    assert len(ev["candidates"]) == 4 and len(ev["controls"]) == 1


def test_post_load_records_real_mtime_and_cancel_prints_note(sdir, cwd, tmp_path):
    t0 = time.time()
    _write_t0(sdir, t0)
    snap = tmp_path / "0000_screen_base.zmx"
    snap.write_text("x")
    os.utime(str(snap), (t0 - 500, t0 - 500))
    _seed_log(sdir, [_trial_log_event({h.norm_path(CAND): [1, 2]}, []), {"kind": "keep", "t": t0 - 100,
                                                                        "slots": [1, 2], "path": CAND}])
    r = run_hook(["enforce", "post"], _post_input(cwd, h.LOAD_TOOL, {"path": str(snap)}, []))
    msg = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Partner" not in msg and "untried S6" in msg
    ev = _events(sdir)[-1]
    assert ev["kind"] == "load" and abs(ev["mtime"] - (t0 - 500)) < 2


def test_post_load_of_missing_file_logs_none_and_prints_nothing(sdir, cwd):
    # beyond plumbing: a load_design with NO log now writes nothing, so a glass session (a trial already logged) is seeded
    _write_t0(sdir, time.time())
    _seed_log(sdir, [trial()])
    r = run_hook(["enforce", "post"], _post_input(cwd, h.LOAD_TOOL, {"path": "D:/nope/seed.zmx"}, []))
    assert r.returncode == 0 and r.stdout == ""
    ev = _events(sdir)[-1]
    assert ev["kind"] == "load" and ev["mtime"] is None


def test_post_save_tools_are_not_hooked():
    assert "save_" not in open(HOOK, encoding="utf-8").read().replace("save and report", "")


def test_fail_open_garbage_stdin():
    for mode in ("enforce", "log"):
        for which in ("post", "stop"):
            r = run_hook([mode, which], "this is not json {{{")
            assert r.returncode == 0 and r.stdout == ""


def test_fail_open_missing_t0(sdir, cwd):
    # beyond plumbing: a missing t0 is no longer an error (it is created); the error event now comes from a real
    # failure (an unparseable trial response) after t0 was created
    r = run_hook(["enforce", "post"], _post_input(cwd, h.TRIAL_TOOL, {}, []))
    assert r.returncode == 0 and r.stdout == ""
    errs = _events(sdir)
    assert errs and all(e["kind"] == "error" for e in errs)


def test_fail_open_unparseable_response(sdir, cwd):
    _write_t0(sdir, time.time())
    r = run_hook(["enforce", "post"], _post_input(cwd, h.TRIAL_TOOL, {}, [{"type": "text", "text": "not a repr ("}]))
    assert r.returncode == 0 and r.stdout == ""


def test_fail_open_no_args():
    r = subprocess.run([sys.executable, HOOK], input="{}", capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == ""


# ---- registration contract of the hook -----------------------------------------------------

def test_write_hooks():
    # the hook's contract is its docstring (registrations, environment) and the subcommands it dispatches
    doc = h.__doc__
    assert doc.startswith("Glass-search clock hook")
    for ev in ("enforce prompt", "enforce post", "enforce stop"):
        assert 'python "$CLAUDE_PROJECT_DIR/.claude/hooks/glass_clock_hook.py" ' + ev in doc
    assert "mcp__optivibe__trial_glass_swaps|mcp__optivibe__load_design" in doc
    for var in (h.ENV_BUDGET, h.ENV_MODE, h.ENV_DIR):
        assert var in doc
    assert set(h.HANDLERS) == {"prompt", "post", "stop"}
    assert h.TRIAL_TOOL + "|" + h.LOAD_TOOL == "mcp__optivibe__trial_glass_swaps|mcp__optivibe__load_design"


# ---- path normalisation and the log lock -----------------------------------------------------------------------

def test_candidate_path_spellings_with_dot_and_dotdot_match():
    ev = [{"kind": "trial", "ok": True, "candidates": {h.norm_path("D:/R/candidates/a.zmx"): [3]}, "controls": []}]
    bs = chr(92)
    for spelling in ("D:/R/candidates/./a.zmx", "D:" + bs + "R" + bs + "candidates" + bs + "x" + bs + ".." + bs + "a.zmx",
                     "d:/r/CANDIDATES//a.zmx"):
        assert h.classify_load(ev, spelling) == ("keep", [3]), spelling


def test_lock_released_after_normal_call_and_after_exception(tmp_path):
    lock = tmp_path / h.LOCK_NAME
    with h.locked(str(tmp_path)) as held:
        assert held is True and lock.exists()
    assert not lock.exists()
    with pytest.raises(RuntimeError):
        with h.locked(str(tmp_path)):
            raise RuntimeError("boom")
    assert not lock.exists()


def test_hook_subprocess_leaves_no_lock(sdir, cwd):
    # beyond plumbing: a stop with no log returns before the lock, so a glass session (a log) is seeded
    _write_t0(sdir, time.time())
    _seed_log(sdir, [trial()])
    run_hook(["enforce", "stop"], _stop_input(cwd))
    assert not (sdir / h.LOCK_NAME).exists()


def test_stale_lock_is_broken(tmp_path):
    lock = tmp_path / h.LOCK_NAME
    lock.write_text("")
    old = time.time() - h.LOCK_STALE_S - 5
    os.utime(str(lock), (old, old))
    with h.locked(str(tmp_path)) as held:
        assert held is True
    assert not lock.exists()


def test_fresh_held_lock_times_out_and_proceeds_without_it(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "LOCK_TIMEOUT_S", 0.3)
    lock = tmp_path / h.LOCK_NAME
    lock.write_text("")
    with h.locked(str(tmp_path)) as held:
        assert held is False
    assert lock.exists()  # not ours: left alone
    ev = json.loads((tmp_path / h.LOG_NAME).read_text().splitlines()[0])
    assert ev["kind"] == "error" and ev["why"] == "lock_timeout"


# =================================================================================================================
# Part 2: what the promotion adds
# =================================================================================================================

# ---- budget ----------------------------------------------------------------------------------------------------

def test_budget_seconds_parsing(monkeypatch):
    assert h.budget_seconds() == 3600.0  # unset
    for raw, want in (("30", 1800.0), ("0.5", 30.0), (" 45 ", 2700.0), ("", 3600.0), ("abc", 3600.0),
                      ("nan", 3600.0), ("inf", 3600.0), ("0", 0.0), ("-5", -300.0)):
        monkeypatch.setenv(h.ENV_BUDGET, raw)
        assert h.budget_seconds() == want, raw


def _enforce_post_note(sdir_, cwd_, env=None, age=600):
    _write_t0(sdir_, time.time() - age)
    fx = load_fixture()
    r = run_hook(["enforce", "post"], _post_input(cwd_, fx["tool_name"], fx["tool_input"], fx["tool_response"],
                                                   fx["duration_ms"]), env)
    return r, json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_budget_unset_note_says_60(sdir, cwd):
    r, msg = _enforce_post_note(sdir, cwd)
    assert "of 60 min elapsed" in msg and "<= 60 min" in msg


def test_budget_30_changes_note_text(sdir, cwd):
    r, msg = _enforce_post_note(sdir, cwd, {h.ENV_BUDGET: "30"})
    assert "of 30 min elapsed" in msg and "<= 30 min" in msg and "60 min" not in msg


def test_budget_30_moves_the_fit_boundary(sdir, cwd):
    # trial 300 s, sweep not done: a block needs elapsed + 600 s <= budget
    _seed_log(sdir, [trial(dur=300, coverage=cov(untried=[6]))])
    _write_t0(sdir, time.time() - 1190)  # 1190 + 600 = 1790 <= 1800
    r = run_hook(["enforce", "stop"], _stop_input(cwd), {h.ENV_BUDGET: "30"})
    assert json.loads(r.stdout)["decision"] == "block" and "of 30 min" in json.loads(r.stdout)["reason"]
    _seed_log(sdir, [trial(dur=300, coverage=cov(untried=[6]))])
    _write_t0(sdir, time.time() - 1260)  # 1260 + 600 = 1860 > 1800: allow at 30 ...
    r = run_hook(["enforce", "stop"], _stop_input(cwd), {h.ENV_BUDGET: "30"})
    assert r.stdout == "" and _events(sdir)[-1]["decision"] == "allow"
    _seed_log(sdir, [trial(dur=300, coverage=cov(untried=[6]))])
    r = run_hook(["enforce", "stop"], _stop_input(cwd))  # ... but the same elapsed fits the default 60
    assert json.loads(r.stdout)["decision"] == "block"


def test_budget_garbage_means_60(sdir, cwd):
    for raw in ("garbage", "", "nan"):
        r, msg = _enforce_post_note(sdir, cwd, {h.ENV_BUDGET: raw})
        assert "of 60 min elapsed" in msg, raw


@pytest.mark.parametrize("raw", ["0", "-1", "-0.5"])
def test_budget_zero_or_negative_is_off(sdir, cwd, tmp_path, raw):
    clock = tmp_path / "clock"
    for p in sdir.iterdir():
        p.unlink()
    sdir.rmdir()
    clock.rmdir()
    env = {h.ENV_BUDGET: raw}
    fx = load_fixture()
    for args, stdin in ((["enforce", "prompt"], _prompt_input(cwd)),
                        (["enforce", "post"], _post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"],
                                                          fx["duration_ms"])),
                        (["enforce", "stop"], _stop_input(cwd))):
        r = run_hook(args, stdin, env)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert not clock.exists()  # nothing written, not even the state root
    assert list(cwd.iterdir()) == []


def test_budget_zero_off_leaves_existing_log_untouched(sdir, cwd):
    _write_t0(sdir, time.time() - 600)
    _seed_log(sdir, [trial(dur=300, coverage=cov(untried=[6]))])
    before = (sdir / h.LOG_NAME).read_text()
    r = run_hook(["enforce", "stop"], _stop_input(cwd), {h.ENV_BUDGET: "0"})
    assert r.stdout == "" and (sdir / h.LOG_NAME).read_text() == before


# ---- prompt / t0 / blocks per prompt ---------------------------------------------------------------------------

def test_prompt_writes_t0_and_prints_nothing(sdir, cwd):
    r = run_hook(["enforce", "prompt"], _prompt_input(cwd))
    assert r.returncode == 0 and r.stdout == ""
    t0 = float((sdir / h.START_NAME).read_text())
    assert abs(t0 - time.time()) < 30
    assert not (sdir / h.LOG_NAME).exists()  # a prompt alone makes no log


def test_prompt_creates_state_dir(tmp_path, cwd):
    r = run_hook(["enforce", "prompt"], _prompt_input(cwd, sid="fresh"))
    assert r.returncode == 0 and (tmp_path / "clock" / "fresh" / h.START_NAME).is_file()


def test_second_prompt_resets_t0(sdir, cwd):
    _write_t0(sdir, 1.0)
    run_hook(["enforce", "prompt"], _prompt_input(cwd))
    assert float((sdir / h.START_NAME).read_text()) > time.time() - 60


def test_state_counts_blocks_only_since_t0():
    ev = [trial(), stop("block", t=10.0), stop("block", t=20.0), stop("block", t=100.0), stop("allow", t=101.0)]
    assert h.state(ev)["blocks"] == 3
    assert h.state(ev, 0.0)["blocks"] == 3
    assert h.state(ev, 15.0)["blocks"] == 2
    assert h.state(ev, 100.0)["blocks"] == 1  # t >= t0 is inclusive
    assert h.state(ev, 200.0)["blocks"] == 0


def test_each_prompt_gets_its_own_two_blocks(sdir, cwd):
    _seed_log(sdir, [trial(dur=300, coverage=cov(untried=[6]))])
    run_hook(["enforce", "prompt"], _prompt_input(cwd))
    out = [run_hook(["enforce", "stop"], _stop_input(cwd)).stdout for _ in range(3)]
    assert json.loads(out[0])["decision"] == "block" and json.loads(out[1])["decision"] == "block"
    assert out[2] == ""  # third stop of the same prompt: block_cap -> allow
    assert _events(sdir)[-1]["why"] == "block_cap"
    run_hook(["enforce", "prompt"], _prompt_input(cwd))  # a new prompt: its own two blocks
    r = run_hook(["enforce", "stop"], _stop_input(cwd))
    assert json.loads(r.stdout)["decision"] == "block"
    assert _events(sdir)[-1]["facts"]["blocks"] == 0


def test_trials_and_owed_survive_a_new_prompt(sdir, cwd):
    _seed_log(sdir, [trial(dur=300, coverage=cov(sweep_done=True)), keep([11])])
    run_hook(["enforce", "prompt"], _prompt_input(cwd))
    r = run_hook(["enforce", "stop"], _stop_input(cwd))
    reason = json.loads(r.stdout)["reason"]
    assert "(a)" in reason and "Partner re-try owed: S12 (after keeping S11)" in reason
    run_hook(["enforce", "prompt"], _prompt_input(cwd))
    r = run_hook(["enforce", "stop"], _stop_input(cwd))
    assert "Partner re-try owed: S12" in json.loads(r.stdout)["reason"]


# ---- missing t0 ------------------------------------------------------------------------------------------------

def test_missing_t0_at_stop_is_created_without_error(sdir, cwd):
    _seed_log(sdir, [trial(dur=300, coverage=cov(untried=[6]))])
    r = run_hook(["enforce", "stop"], _stop_input(cwd))
    assert r.returncode == 0
    assert (sdir / h.START_NAME).is_file() and float((sdir / h.START_NAME).read_text()) > time.time() - 60
    assert all(e["kind"] != "error" for e in _events(sdir))
    assert json.loads(r.stdout)["decision"] == "block"  # elapsed ~0: a trial fits


def test_missing_t0_at_post_is_created_without_error(sdir, cwd):
    fx = load_fixture()
    r = run_hook(["enforce", "post"], _post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"],
                                                   fx["duration_ms"]))
    assert r.returncode == 0 and "Bench clock" in r.stdout
    assert (sdir / h.START_NAME).is_file()
    evs = _events(sdir)
    assert [e["kind"] for e in evs] == ["trial"]


def test_unreadable_t0_is_treated_as_missing(sdir, cwd):
    (sdir / h.START_NAME).write_text("not a number")
    _seed_log(sdir, [trial()])
    r = run_hook(["enforce", "stop"], _stop_input(cwd))
    assert r.returncode == 0 and all(e["kind"] != "error" for e in _events(sdir))
    assert float((sdir / h.START_NAME).read_text()) > 1.0


# ---- sessions that never search glass --------------------------------------------------------------------------

def test_load_design_without_log_writes_nothing(tmp_path, cwd):
    r = run_hook(["enforce", "post"], _post_input(cwd, h.LOAD_TOOL, {"path": "D:/x/seed.zmx"}, []))
    assert r.returncode == 0 and r.stdout == ""
    assert not (tmp_path / "clock").exists()


def test_stop_without_log_allows_and_writes_nothing(tmp_path, cwd):
    r = run_hook(["enforce", "stop"], _stop_input(cwd))
    assert r.returncode == 0 and r.stdout == ""
    assert not (tmp_path / "clock").exists()


def test_stop_without_log_but_with_t0_writes_no_log(sdir, cwd):
    run_hook(["enforce", "prompt"], _prompt_input(cwd))
    r = run_hook(["enforce", "stop"], _stop_input(cwd))
    assert r.stdout == "" and sorted(p.name for p in sdir.iterdir()) == [h.START_NAME]


def test_first_trial_creates_the_log(sdir, cwd):
    fx = load_fixture()
    assert not (sdir / h.LOG_NAME).exists()
    run_hook(["log", "post"], _post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"], 1000))
    assert (sdir / h.LOG_NAME).is_file()


def test_handlers_directly_without_log_write_nothing(sdir):
    assert h.handle_post({"tool_name": h.LOAD_TOOL, "tool_input": {"path": "x"}}, "enforce", time.time(), B,
                         str(sdir)) is None
    assert h.handle_stop({}, "enforce", time.time(), B, str(sdir)) is None
    assert list(sdir.iterdir()) == []


# ---- sessions are separate -------------------------------------------------------------------------------------

def test_two_sessions_keep_separate_state(tmp_path, cwd):
    fx = load_fixture()
    run_hook(["log", "post"], _post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"], 1000, sid="a"))
    a = tmp_path / "clock" / "a"
    assert (a / h.LOG_NAME).is_file() and not (tmp_path / "clock" / "b").exists()
    r = run_hook(["enforce", "stop"], _stop_input(cwd, sid="b"))  # b never searched glass
    assert r.stdout == "" and not (tmp_path / "clock" / "b").exists()
    run_hook(["log", "post"], _post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"], 1000, sid="b"))
    assert len((a / h.LOG_NAME).read_text().splitlines()) == 1
    assert len((tmp_path / "clock" / "b" / h.LOG_NAME).read_text().splitlines()) == 1
    run_hook(["enforce", "prompt"], _prompt_input(cwd, sid="a"))
    assert float((a / h.START_NAME).read_text()) > float((tmp_path / "clock" / "b" / h.START_NAME).read_text()) - 60


def test_session_id_is_sanitised(tmp_path, monkeypatch):
    assert h.sanitise_session("abc-DEF_123") == "abc-DEF_123"
    assert h.sanitise_session("../../evil") == "evil"
    assert h.sanitise_session("a b/c\\d:e") == "abcde"
    assert h.sanitise_session("") == "nosession" and h.sanitise_session(None) == "nosession"
    assert h.sanitise_session("///") == "nosession"
    root = str(tmp_path / "clock")
    assert h.state_dir("../../evil") == os.path.join(root, "evil")
    assert h.state_dir(None) == os.path.join(root, "nosession")


def test_state_dir_default_is_under_tempdir(monkeypatch):
    import tempfile
    monkeypatch.delenv(h.ENV_DIR)
    assert h.state_dir("s1") == os.path.join(tempfile.gettempdir(), "optivibe-glass-clock", "s1")


def test_missing_session_id_uses_nosession(tmp_path, cwd):
    fx = load_fixture()
    inp = json.loads(_post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"], 1000))
    del inp["session_id"]
    run_hook(["log", "post"], json.dumps(inp))
    assert (tmp_path / "clock" / "nosession" / h.LOG_NAME).is_file()


# ---- nothing under cwd -----------------------------------------------------------------------------------------

def test_state_never_lands_under_cwd(sdir, cwd, tmp_path):
    fx = load_fixture()
    run_hook(["enforce", "prompt"], _prompt_input(cwd))
    run_hook(["enforce", "post"], _post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"], 1000))
    run_hook(["enforce", "post"], _post_input(cwd, h.LOAD_TOOL, {"path": "D:/x/s.zmx"}, []))
    run_hook(["enforce", "stop"], _stop_input(cwd))
    run_hook(["enforce", "post"], _post_input(cwd, h.TRIAL_TOOL, {}, [{"type": "text", "text": "bad ("}]))  # error path
    assert list(cwd.iterdir()) == []
    assert (sdir / h.LOG_NAME).is_file()
    assert not (cwd / ".claude").exists()


# ---- mode override ---------------------------------------------------------------------------------------------

def _block_ready(sdir_):
    _write_t0(sdir_, time.time() - 600)
    _seed_log(sdir_, [trial(dur=300, coverage=cov(untried=[6]))])


def test_mode_env_off_overrides_argument(sdir, cwd):
    _block_ready(sdir)
    before = (sdir / h.LOG_NAME).read_text()
    t0_before = (sdir / h.START_NAME).read_text()
    for which, stdin in (("stop", _stop_input(cwd)), ("prompt", _prompt_input(cwd))):
        r = run_hook(["enforce", which], stdin, {h.ENV_MODE: "off"})
        assert r.returncode == 0 and r.stdout == ""
    assert (sdir / h.LOG_NAME).read_text() == before and (sdir / h.START_NAME).read_text() == t0_before


def test_mode_env_log_overrides_enforce_argument(sdir, cwd):
    _block_ready(sdir)
    r = run_hook(["enforce", "stop"], _stop_input(cwd), {h.ENV_MODE: "log"})
    assert r.returncode == 0 and r.stdout == ""
    assert _events(sdir)[-1]["decision"] == "block"  # decided and logged, not printed


def test_mode_env_enforce_overrides_log_argument(sdir, cwd):
    _block_ready(sdir)
    r = run_hook(["log", "stop"], _stop_input(cwd), {h.ENV_MODE: "enforce"})
    assert json.loads(r.stdout)["decision"] == "block"


def test_mode_env_unrecognised_value_is_ignored(sdir, cwd):
    _block_ready(sdir)
    r = run_hook(["enforce", "stop"], _stop_input(cwd), {h.ENV_MODE: "bogus"})
    assert json.loads(r.stdout)["decision"] == "block"
    assert h.effective_mode("log") == "log"


# ---- subprocess end to end -------------------------------------------------------------------------------------

def test_subprocess_prompt_post_stop_enforce(sdir, cwd):
    fx = load_fixture()
    r = run_hook(["enforce", "prompt"], _prompt_input(cwd))
    assert r.returncode == 0 and r.stdout == ""
    r = run_hook(["enforce", "post"], _post_input(cwd, fx["tool_name"], fx["tool_input"], fx["tool_response"],
                                                   fx["duration_ms"]))
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert r.returncode == 0 and out["hookEventName"] == "PostToolUse" and "of 60 min elapsed" in out["additionalContext"]
    r = run_hook(["enforce", "stop"], _stop_input(cwd))
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["decision"] == "block" and "(b)" in out["reason"]
    kinds = [e["kind"] for e in _events(sdir)]
    assert kinds == ["trial", "stop"]
    assert list(cwd.iterdir()) == []


def test_subprocess_garbage_stdin_all_subcommands():
    for mode in ("enforce", "log"):
        for which in ("prompt", "post", "stop"):
            r = run_hook([mode, which], "this is not json {{{")
            assert r.returncode == 0 and r.stdout == ""
    r = run_hook(["enforce", "bogus"], _prompt_input("."))  # unknown subcommand: fail open
    assert r.returncode == 0 and r.stdout == ""
