"""tools/_tolerance_pick.py -- ``tolerance(mode="pick_push")``: the compensator picker. NOT dispatchable on its own.

One call = one wavefront SENSITIVITY run (to rank the element / cemented-group decenter and tilt hotspots) -> a
shortlist of at most three groups the production CB layout accepts -> one capped ``dls_cb_group`` Monte Carlo per
candidate on the loaded merit function (``criterion="merit"``) -> a ranking by yield under ``threshold`` (else by P90)
with the Monte-Carlo noise REPORTED, never used to change the pick -> the ``judge_call`` (MC2000) for the caller.

Everything engine-facing (the cap, the no-edit path, the merit guard, the spread warning) lives in ``tolerance_run``;
this module only sequences the inner calls and judges their envelopes. The inner handler is the never-raise wrapped
``tolerance`` handed in by ``tolerance_run`` (the desensitize pattern), so a test can hand in a scripted one.

WHAT THIS FILE OWNS ON EVERY FAILURE PATH: it never edits the lens itself. After every inner call it proves the
lens unchanged with ``diff_allowed(found0, ...)`` (every snapshot passes the ``allow_no_variables`` flag: a fixed
prescription is a valid tolerance input) and STOPS at the first candidate that is unrestored, changed the lens, or
could not stop its engine run (``cap_stop_failed``). After a ``cap_stop_failed`` it makes NO further engine call (no
state re-read, no merit read): the engine may still be running an analysis.
"""
import copy
import math
import time

from . import _analysis_common as _ac
from . import _lens_state
from . import _tolerance_common as _tc
from . import _tolerance_layout as _tl
from .._io import is_finite_number

TOOL = "tolerance"
PUSH_TYPES = ("TEDX", "TEDY", "TETX", "TETY")
N_SHORTLIST = 3
DEFAULT_TRIALS = 200                    # OD2
S_PER_TRIAL_CAP = 10                    # OD6: the cap catches a hang, not a slow candidate
WALL_BUDGET_S = 1500                    # the whole call's wall budget
BUDGET_RESERVE_S = 60
DROP_FRAC = 0.10                        # OD4
JUDGE_TRIALS = 2000
Z = 2.0
NOMINAL_BAND = 1e-4
SPREAD_FLOOR = _tc.SPREAD_FLOOR         # one definition (PE3)
rel_spread = _tc.rel_spread             # one definition (PE3)
LENS_UNCHANGED_REL = 1e-9
RECOVERY = "reload your saved candidate with load_design"
_FORBIDDEN = ("tolerances", "include_mechanical", "strict", "cb_tilt", "output_file")

_now = time.perf_counter                # the clock seam: a test replaces THIS name, never ``time.perf_counter``


def _tr():
    """``tolerance_run`` (lazy: it imports this module lazily, so neither import order can cycle)."""
    from . import tolerance_run
    return tolerance_run


def _refuse(message, reason, family="tolerancing_param"):
    return _tc.ToleranceError(message, family=family, reason=reason)


def _finite(value):
    return is_finite_number(value)


# --------------------------------------------------------------------------- #
# validate: spec 3.1 rows 1-12, in order, all pure (nothing is opened)
# --------------------------------------------------------------------------- #
def validate(params, system):
    """Refuse a bad call BEFORE anything opens; return the resolved config incl. ``merit_before`` (rows 10-12)."""
    tr = _tr()
    for key in _FORBIDDEN:                                                                      # row 1
        if dict.__contains__(params, key):
            raise _refuse(f"mode='pick_push' does not take {key!r}; nothing was opened", f"param_not_allowed:{key}")
    if dict.__contains__(params, "criterion") and dict.__getitem__(params, "criterion") != "merit":   # row 2
        raise _refuse("mode='pick_push' judges on the loaded merit function: criterion must be 'merit' or absent; "
                      "nothing was opened", "criterion_not_merit")
    if (dict.__contains__(params, "compensator")
            and dict.__getitem__(params, "compensator") != "dls_cb_group"):                      # row 3
        raise _refuse("mode='pick_push' picks the dls_cb_group compensator: compensator must be 'dls_cb_group' or "
                      "absent; nothing was opened", "compensator_not_dls_cb_group")
    raw_budget = dict.get(params, "budget")
    if not isinstance(raw_budget, dict):                                                        # row 4
        raise _refuse("mode='pick_push' needs budget={vendor, grade, element_decenter, element_tilt}; "
                      "nothing was opened", "budget_required")
    budget = tr._resolve_budget(params)
    cb = dict.get(params, "cb_group")                                                           # row 5
    if not isinstance(cb, dict) or set(cb) != {"decenter_mm"}:
        raise _refuse("mode='pick_push' needs cb_group={'decenter_mm': <mm>} and nothing else (the picker chooses "
                      "the group; tilt is outside the approved shape); nothing was opened", "cb_group_shape")
    decenter = tr._cb_group_range(cb, "decenter_mm", "mm")                                      # row 6
    threshold = None
    if dict.__contains__(params, "threshold"):                                                  # row 7
        threshold = dict.__getitem__(params, "threshold")
        if not _finite(threshold) or threshold <= 0:
            raise _refuse(f"threshold must be a finite number > 0 (merit units), got {threshold!r}; "
                          "nothing was opened", "threshold_invalid")
        threshold = float(threshold)
    trials = tr._resolve_trials(params) if dict.__contains__(params, "trials") else DEFAULT_TRIALS   # row 8
    sampling = tr._resolve_sampling(params) if dict.__contains__(params, "sampling") else None
    full = tr._bool_param(params, "full", False)
    tr._dls_preflight(system)                                                                   # row 9
    merit_before = tr.merit_preflight(system)                                                   # rows 10-12
    return {"budget": budget, "decenter_mm": decenter, "threshold": threshold, "trials": trials,
            "sampling": sampling, "full": full, "merit_before": merit_before}


# --------------------------------------------------------------------------- #
# shortlist (spec 3.6)
# --------------------------------------------------------------------------- #
def shortlist_scores(hotspots):
    """``{push key: sqrt(sum c^2 over the TEDX/TEDY/TETX/TETY rows)}``; an element inside a cemented group scores toward it."""
    sq = {}
    for h in hotspots or []:
        if not isinstance(h, dict) or h.get("kind") not in ("element", "cemented_group"):
            continue
        key = h.get("parent") or h["key"]
        s = sum(float(r.get("c") or 0.0) ** 2 for r in h.get("rows") or [] if r.get("type") in PUSH_TYPES)
        sq[key] = sq.get(key, 0.0) + s
    return {k: v ** 0.5 for k, v in sq.items()}


def _ranked(scores):
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def shortlist(system, scores, n=N_SHORTLIST):
    """Top ``n`` eligible keys by score -> ``(keep, skipped)``; an ineligible key is skipped with the layout's reason."""
    keep, skipped = [], []
    for key, score in _ranked(scores):
        if len(keep) >= n:
            break
        if score <= 0.0:
            skipped.append({"group": key, "score": score, "reason": "zero_score", "error": None})
            continue
        try:
            _tr()._cb_lde_rows(system, key, "cb_group")
        except Exception as exc:  # noqa: BLE001 -- the production layout refusal says why
            skipped.append({"group": key, "score": score, "reason": getattr(exc, "reason", None),
                            "error": str(exc)})
            continue
        keep.append({"group": key, "score": score})
    return keep, skipped


# --------------------------------------------------------------------------- #
# stats (spec 3.7), pure
# --------------------------------------------------------------------------- #
def p90_interval(values):
    """``[s[l-1], s[u-1]]`` on the sorted values: one-based ranks ``l = floor(0.9N - 2 sqrt(0.09N))``, ``u = ceil(...)``."""
    s = sorted(values)
    n = len(s)
    centre, half = n * 9 / 10, 2.0 * math.sqrt(n * 9 / 100)
    lo = min(max(math.floor(centre - half), 1), n)
    hi = min(max(math.ceil(centre + half), 1), n)
    return [s[lo - 1], s[hi - 1]]


def stats(values, at_bound, threshold):
    """Yield / P90* / mean / spread of one candidate's trials. A failure is an at-bound or unreadable trial."""
    n = len(values)
    fail = [bool(flag) or not _finite(v) for v, flag in zip(values, at_bound)]
    s = sorted(math.inf if f else float(v) for v, f in zip(values, fail))
    finite = [float(v) for v in values if _finite(v)]
    n_pass = (None if threshold is None
              else sum(1 for v, f in zip(values, fail) if not f and float(v) <= threshold))
    return {"n_trials": n, "n_at_bound": sum(1 for flag in at_bound if flag), "n_pass": n_pass,
            "yield": None if n_pass is None else n_pass / n,
            "p90": s[(9 * n + 9) // 10 - 1],                     # nearest rank, ceil(0.9 n) in integers
            "p90_interval": p90_interval(s),
            "mean": math.fsum(finite) / len(finite) if finite else None,
            "rel_spread": rel_spread(values)}


def _gap_and_two_se(p_a, p_b, n_a, n_b):
    pooled = (round(p_a * n_a) + round(p_b * n_b)) / (n_a + n_b)
    return abs(p_a - p_b), Z * math.sqrt(pooled * (1.0 - pooled) * (1.0 / n_a + 1.0 / n_b))


def yield_tie(p_a, p_b, n_a, n_b):
    """Two yields are within Monte-Carlo noise: ``|p_a - p_b| <= 2 sqrt(pbar (1 - pbar) (1/n_a + 1/n_b))``, pooled ``pbar``."""
    gap, two_se = _gap_and_two_se(p_a, p_b, n_a, n_b)
    return gap <= two_se


# --------------------------------------------------------------------------- #
# restore + classify (spec 3.9)
# --------------------------------------------------------------------------- #
def restore_state(env):
    """R-RESTORE -> ``(clean, source)``: was the lens left as found by this inner envelope? Fails closed."""
    if env.get("state_mutated") is True:
        return False, "state_mutated"
    if env.get("ok") is True:
        cb = (env.get("run_summary") or {}).get("cb_group")
        if not isinstance(cb, dict) or cb.get("restored") is not True:
            return False, "run_summary.cb_group.restored"
        return True, "run_summary.cb_group.restored"
    if "restored" in env:
        return env["restored"] is True, "restored"
    if "run_summary" in env:
        return False, "run_summary_without_restored"
    if env.get("reason") in (None, "handler_raised"):
        return False, "no_restore_evidence"   # PF2: a failure that says nothing is not a named pre-layout refusal
    return True, "no_layout_began"          # a NAMED refusal before any layout: unmutated iff state_mutated is not True


def _opened(env):
    """Did the inner call open the tolerancing tool? Closed rule: a guard refusal and a pre-layout refusal did not."""
    if env.get("ok") is True:
        return True
    if env.get("reason") in ("merit_layout_mismatch", "merit_dmfs_with_element_tolerances"):      #: refused before the open
        return False
    return not ("restored" not in env and "state_mutated" not in env and "run_summary" not in env)


def classify(env, merit_before, *, threshold=None, diff_ok=True):
    """One inner MC envelope -> ``{status, reason, stop, <stats>}``. First match wins, top to bottom (spec 3.9)."""
    out = {"status": "unjudged", "reason": None, "stop": None}
    if env.get("reason") == "cap_stop_failed":                           # PE5: BEFORE R-RESTORE (flat, restored False)
        return dict(out, reason="cap_stop_failed", stop="cap_stop_failed")
    clean, _src = restore_state(env)
    if not clean:
        return dict(out, reason="candidate_unrestored", stop="candidate_unrestored")
    if not diff_ok:
        return dict(out, reason="lens_changed", stop="lens_changed")
    if env.get("ok") is not True:
        reason = env.get("reason")
        if reason in ("capped", "merit_layout_mismatch"):
            return dict(out, reason=reason)
        return dict(out, reason=f"{env.get('error_family')}:{reason or ''}")
    cb = (env.get("run_summary") or {}).get("cb_group") or {}
    values, flags = env.get("per_trial"), cb.get("per_trial_at_bound")
    if (not isinstance(values, list) or not isinstance(flags, list) or len(values) != len(flags) or not values
            or not all(isinstance(f, bool) for f in flags)):
        return dict(out, reason="at_bound_unknown")
    out.update(stats(values, flags, threshold))
    nominal = env.get("nominal_criterion")
    if not _finite(nominal):
        return dict(out, reason="nominal_unknown")
    if nominal > merit_before * (1.0 + NOMINAL_BAND):
        return dict(out, reason="nominal_above_lens_merit")
    if out["rel_spread"] is not None and out["rel_spread"] < SPREAD_FLOOR:
        return dict(out, reason="trials_near_identical")
    if out["n_at_bound"] > DROP_FRAC * out["n_trials"]:
        return dict(out, status="dropped", reason="at_bound_often")
    return dict(out, status="judged")


# --------------------------------------------------------------------------- #
# rank (spec 3.8), pure
# --------------------------------------------------------------------------- #
def rank(candidates, threshold):
    """Order the JUDGED candidates and report the noise: ``{rank_by, basis, order, chosen, tied_with, near_tie, margin}``."""
    judged = [c for c in candidates if c.get("status") == "judged"]
    by_yield = threshold is not None

    def key(c):
        mean = math.inf if c.get("mean") is None else c["mean"]
        return ((-c["yield"], c["p90"], mean, c["group"]) if by_yield else (c["p90"], mean, c["group"]))
    ordered = sorted(judged, key=key)
    out = {"rank_by": "yield" if by_yield else "p90", "basis": None, "order": [c["group"] for c in ordered],
           "chosen": None, "tied_with": [], "near_tie": False, "margin": None}
    if not ordered:
        return out
    top, rest = ordered[0], ordered[1:]
    out["chosen"] = top["group"]
    out["basis"] = "only_candidate" if not rest else out["rank_by"]
    if not rest:
        return out
    if by_yield:
        tied = [c for c in rest if yield_tie(top["yield"], c["yield"], top["n_trials"], c["n_trials"])]
        near = min(rest, key=lambda c: abs(top["yield"] - c["yield"]))
        gap, two_se = _gap_and_two_se(top["yield"], near["yield"], top["n_trials"], near["n_trials"])
        out["margin"] = {"vs": near["group"], "yield_diff": gap, "two_se": two_se}
    else:
        lo, hi = top["p90_interval"]
        tied = [c for c in rest if c["p90_interval"][0] <= hi and lo <= c["p90_interval"][1]]
        out["margin"] = {"vs": rest[0]["group"], "p90_intervals": {top["group"]: list(top["p90_interval"]),
                                                                  rest[0]["group"]: list(rest[0]["p90_interval"])}}
    out["tied_with"] = [c["group"] for c in tied]
    out["near_tie"] = bool(tied)
    return out


def judge_call(cfg, chosen):
    """The params dict for the MC2000 confirmation of ``chosen``: dispatchable on the unchanged lens (spec 3.10)."""
    call = {"mode": "monte_carlo", "budget": copy.deepcopy(cfg["budget"]), "compensator": "dls_cb_group",
            "cb_group": {"group": chosen, "decenter_mm": cfg["decenter_mm"]}, "criterion": "merit",
            "trials": JUDGE_TRIALS, "full": True}
    if cfg["sampling"] is not None:
        call["sampling"] = cfg["sampling"]
    return call


# --------------------------------------------------------------------------- #
# the call
# --------------------------------------------------------------------------- #
def _clean(obj):
    """JSON-safe at the envelope boundary only: a non-finite float (P90* with a failed trial is +inf) becomes None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _call(handler, session, params, **kw):
    try:
        env = handler(session, params, **kw)
    except Exception as exc:  # noqa: BLE001 -- the handler never raises; a double might
        return {"ok": False, "tool": TOOL, "error_family": "tolerancing_run", "reason": "handler_raised",
                "error": f"the inner call raised ({exc!r})"}
    return env if isinstance(env, dict) else {
        "ok": False, "tool": TOOL, "error_family": "tolerancing_run", "reason": "handler_raised",
        "error": f"the inner call returned {env!r}"}


def _blank(group, score, score_rank):
    return {"group": group, "score": score, "score_rank": score_rank, "rank": None, "status": "not_run",
            "reason": None, "yield": None, "n_pass": None, "p90": None, "p90_interval": None, "mean": None,
            "rel_spread": None, "n_trials": None, "n_at_bound": None, "effective_decenter_mm": None,
            "nominal_report": None, "merit_neutralised_rows": None, "merit_layout_rel": None, "cap_s": None,
            "cap_source": None, "wall_s": None, "s_per_trial": None, "budget_rule_id": None, "restored": None,
            "detail": None}


_STAT_KEYS = ("yield", "n_pass", "p90", "p90_interval", "mean", "rel_spread", "n_trials", "n_at_bound")


class _Call:
    """The mutable state of one call; ``build`` turns it into any envelope."""

    def __init__(self, cfg, found0, t_start):
        self.cfg, self.found0, self.t_start = cfg, found0, t_start
        self.opens = 0
        self.warnings = []
        self.envs = []
        self.shortlist = None
        self.candidates = []

    def elapsed(self):
        return round(_now() - self.t_start, 3)

    def tde_cleared(self):
        return all(env.get("tde_cleared", True) is not False and not self._evidence_free(env) for env in self.envs)

    @staticmethod
    def _evidence_free(env):
        """PF2: a raised handler, or a reason-less engine-error envelope that names no TDE state, did not prove it."""
        if env.get("ok") is True:
            return False
        return env.get("reason") == "handler_raised" or (env.get("reason") is None and "tde_cleared" not in env)

    def body(self):
        cfg = self.cfg
        out = {"mode": "pick_push", "criterion": "MeritFunction", "criterion_units": "merit",
               "merit_before": cfg["merit_before"], "decenter_mm": cfg["decenter_mm"], "trials": cfg["trials"],
               "threshold": cfg["threshold"], "rank_by": "yield" if cfg["threshold"] is not None else "p90",
               "s_per_trial_cap": float(S_PER_TRIAL_CAP), "wall_budget_s": float(WALL_BUDGET_S),
               "at_bound_drop_fraction": DROP_FRAC, "spread_floor": SPREAD_FLOOR}
        if self.shortlist is not None:
            out["shortlist"] = self.shortlist
        out.update(candidates=self.candidates, tolerancing_opens=self.opens, tde_cleared=self.tde_cleared(),
                   wall_s=self.elapsed(), warnings=self.warnings)
        return out

    def fail(self, family, reason, message, **extra):
        env = self.body()
        env.update(extra)
        return _clean(_ac.error_envelope(TOOL, family, message, reason=reason, **env))


def run(session, params, tolerance_handler):
    """``tolerance(mode="pick_push")``: the whole recipe (spec 3.2). Never raises."""
    try:
        return _run(session, params, tolerance_handler)
    except _tc.ToleranceError as exc:
        reason = getattr(exc, "reason", None)
        return _ac.error_envelope(TOOL, getattr(exc, "family", "tolerancing_param"), str(exc),
                                  **({} if reason is None else {"reason": reason}), mode="pick_push")
    except Exception as exc:  # noqa: BLE001 -- the dispatch net
        return _ac.error_envelope(
            TOOL, "tolerancing_run", f"the picker hit an unexpected fault ({exc!r}); if a candidate was running, "
            f"{RECOVERY}", reason="internal_error", mode="pick_push")


def _lens_check(system, found0):
    """-> ``(unchanged, first_differences)``: the only proof the lens is as found (spec 3.2 step 4)."""
    try:
        d = _tl.diff_allowed(found0, _tl.state_with_mce(system, allow_no_variables=True), allow_dofs=False)
    except Exception as exc:  # noqa: BLE001 -- a proof that cannot run is a failed proof (PF5)
        return False, [f"the lens state could not be re-read ({exc})"]
    return bool(d["pass"]), list(d["first_differences"][:3])


def _fill(cand, env, verdict):
    cb = (env.get("run_summary") or {}).get("cb_group") or {}
    cand.update(status=verdict["status"], reason=verdict["reason"])
    for key in _STAT_KEYS:
        cand[key] = verdict.get(key)
    cand["nominal_report"] = env.get("nominal_criterion")
    cand["effective_decenter_mm"] = cb.get("effective_decenter_mm")
    cand["merit_neutralised_rows"] = cb.get("merit_neutralised_rows", env.get("merit_neutralised_rows"))
    cand["merit_layout_rel"] = cb.get("merit_layout_rel", env.get("merit_layout_rel"))
    cand["budget_rule_id"] = (env.get("run_summary") or {}).get("budget_rule_id")
    cand["restored"] = cb.get("restored") if env.get("ok") is True else env.get("restored")
    if env.get("ok") is False:
        cand["detail"] = env.get("error")          # the inner refusal's text, as is (the measured rel + remedy)
    wall = (env.get("run_summary") or {}).get("wall_s")
    if env.get("ok") is True and _finite(wall):
        cand["wall_s"] = wall
        cand["s_per_trial"] = round(wall / cand["n_trials"], 4) if cand["n_trials"] else None


def _stop_envelope(call, family, reason, message, env, **extra):
    recovery = env.get("recovery") or RECOVERY
    return call.fail(family, reason, message, state_mutated=True, recovery=recovery, **extra)


def _run(session, params, handler):
    system = session.system
    cfg = validate(params, system)
    t_start = _now()
    try:
        found0 = _tl.state_with_mce(system, allow_no_variables=True)
    except _lens_state.StateUnreadable as exc:
        raise _refuse(f"the lens state could not be read ({exc}); nothing was opened or changed", "state_unreadable",
                      family="tolerancing_run")
    call = _Call(cfg, found0, t_start)
    budget, trials = cfg["budget"], cfg["trials"]

    # step 2: the wavefront sensitivity run (uncapped; charged to the budget)
    sens_params = {"mode": "sensitivity", "budget": copy.deepcopy(budget), "criterion": "wavefront",
                   "compensator": "paraxial"}
    if cfg["sampling"] is not None:
        sens_params["sampling"] = cfg["sampling"]
    sens = _call(handler, session, sens_params)
    call.envs.append(sens)
    call.opens += 1 if _opened(sens) else 0
    if sens.get("ok") is not True:
        out = dict(sens)
        out.update(mode="pick_push", stage="sensitivity", tolerancing_opens=call.opens)
        return _clean(out)
    call.warnings.extend(f"sensitivity: {w}" for w in sens.get("warnings") or [])
    if sens.get("hotspots_withheld") is not None:
        return call.fail("tolerancing_run", "hotspots_withheld",
                         f"the sensitivity run withheld its hotspot ranking ({sens.get('hotspots_withheld')!r}); "
                         "there is nothing to shortlist")
    unchanged, first = _lens_check(system, found0)
    if not unchanged or sens.get("state_mutated"):
        return call.fail("tolerancing_run", "lens_changed",
                         f"the sensitivity run changed the lens ({first}); {RECOVERY}", state_mutated=True,
                         recovery=RECOVERY)

    # step 3: the shortlist
    scores = shortlist_scores(sens.get("hotspots"))
    keep, skipped = shortlist(system, scores, N_SHORTLIST)
    rank_of = {key: i for i, (key, _s) in enumerate(_ranked(scores), start=1)}
    call.shortlist = {"criterion": "RMSWavefront", "n_eligible": len(keep), "shortlist_short": len(keep) < N_SHORTLIST,
                      "scores": [{"group": k, "score": s, "score_rank": rank_of[k]} for k, s in _ranked(scores)],
                      "skipped": skipped, "sensitivity_run_summary": sens.get("run_summary")}
    if not keep:
        return call.fail("tolerancing_param", "no_eligible_group",
                         "no hotspot group is a legal cb_group (see shortlist.skipped); nothing was run",
                         state_mutated=False)
    call.candidates = [_blank(k["group"], k["score"], rank_of[k["group"]]) for k in keep]

    # step 4: one capped Monte Carlo per candidate
    stop = None
    for i, cand in enumerate(call.candidates):
        remaining = WALL_BUDGET_S - (_now() - t_start)
        if remaining <= BUDGET_RESERVE_S:                                    # PE7: <=, so cap_s is always > 0
            for later in call.candidates[i:]:
                later.update(status="not_run", reason="wall_budget_exhausted")
            break
        per_trial, budget_cap = trials * S_PER_TRIAL_CAP, remaining - BUDGET_RESERVE_S
        cap_s, source = (float(per_trial), "per_trial") if per_trial <= budget_cap else (budget_cap, "budget")
        cand.update(cap_s=cap_s, cap_source=source)
        mc = {"mode": "monte_carlo", "budget": copy.deepcopy(budget), "compensator": "dls_cb_group",
              "cb_group": {"group": cand["group"], "decenter_mm": cfg["decenter_mm"]}, "criterion": "merit",
              "trials": trials, "full": True}
        if cfg["sampling"] is not None:
            mc["sampling"] = cfg["sampling"]
        t0 = _now()
        env = _call(handler, session, mc, cap_s=cap_s)
        measured = round(_now() - t0, 3)
        call.envs.append(env)
        call.opens += 1 if _opened(env) else 0
        busy = env.get("reason") == "cap_stop_failed"
        diff_ok = True if busy else _lens_check(system, found0)[0]            # a busy engine is not touched
        verdict = classify(env, cfg["merit_before"], threshold=cfg["threshold"], diff_ok=diff_ok)
        _fill(cand, env, verdict)
        if cand["wall_s"] is None:
            cand["wall_s"] = measured                                         # a capped / failed run: our own clock
        call.warnings.extend(f"{cand['group']}: {w}" for w in env.get("warnings") or [])
        if cfg["full"] and isinstance(env.get("per_trial"), list):
            cand["per_trial"] = list(env["per_trial"])
            flags = ((env.get("run_summary") or {}).get("cb_group") or {}).get("per_trial_at_bound")
            if isinstance(flags, list):
                cand["per_trial_at_bound"] = list(flags)
        if verdict["stop"] is not None:
            stop = (verdict["stop"], env)
            break

    if stop is not None:
        reason, env = stop
        msg = {"cap_stop_failed": "a capped run could not be confirmed stopped; the engine may still be running an "
                                  "analysis, so the call stopped without touching the lens",
               "candidate_unrestored": "a candidate left the layout in the lens (see unrestored_result)",
               "lens_changed": "a candidate changed the lens (see unrestored_result)"}[reason]
        return _stop_envelope(call, "tolerancing_run", reason, msg, env, unrestored_result=env)

    # step 5-6: the final proof, the ranking, the envelope
    unchanged, first = _lens_check(system, found0)
    try:
        merit_after = float(system.MFE.CalculateMeritFunction())
    except Exception:  # noqa: BLE001 -- an unreadable merit proves nothing
        merit_after = None
    mb = cfg["merit_before"]
    lens_unchanged = bool(unchanged and merit_after is not None and math.isfinite(merit_after)
                          and abs(merit_after - mb) <= LENS_UNCHANGED_REL * abs(mb))
    ranking = rank(call.candidates, cfg["threshold"])               # spec 3.9: the pick stays listed on lens_changed
    for position, group in enumerate(ranking["order"], start=1):
        next(c for c in call.candidates if c["group"] == group)["rank"] = position
    if not lens_unchanged:
        return call.fail("tolerancing_run", "lens_changed",
                         f"the lens is not as found after the candidates ({first}; merit {mb!r} -> {merit_after!r}); "
                         f"{RECOVERY}", state_mutated=True, recovery=RECOVERY, merit_after=merit_after,
                         lens_unchanged=False)
    if ranking["chosen"] is None:
        return call.fail("tolerancing_run", "no_candidate_judged",
                         "no candidate could be judged (see candidates[].reason)", state_mutated=False,
                         merit_after=merit_after, lens_unchanged=True)
    top = call.candidates[0]                      # PF4: the first candidate in score order
    if top["status"] != "judged":
        call.warnings.append(f"pick_among_judged_only: {top['group']} (score rank {top['score_rank']}) was "
                             f"{top['reason'] or top['status']}")
    if ranking["near_tie"]:
        call.warnings.append(f"near_tie: the pick is within Monte-Carlo noise of {', '.join(ranking['tied_with'])} "
                             f"at n={trials}; judge both with judge_call or accept the pick")
    out = call.body()
    out.update(ok=True, tool=TOOL, merit_after=merit_after, lens_unchanged=True, chosen=ranking["chosen"],
               basis=ranking["basis"], near_tie=ranking["near_tie"], tied_with=ranking["tied_with"],
               margin=ranking["margin"], judge_call=judge_call(cfg, ranking["chosen"]), state_mutated=False)
    return _clean(out)
