"""``tolerance(mode="gauge")``: the call path of the vector tolerancing gauge (spec amendment, 5.2).

An in-loop screen that reads like the native ``monte_carlo`` it stands in for: the same envelope keys plus ONE new
``per_field`` block (and ``paired`` when ``baseline`` is given). Native Monte Carlo stays the judge.

CALL ORDER (every refusal before the engine is touched is pure):
  1. ``validate``: budget, threshold, compensator (omitted = the judge's ``dls``), ``cb_group``, trials, ``full``,
     ``cap_s``, ``baseline``; ``criterion`` and every other mode's knob refuse.
  2. ``baseline`` is looked up in the gauge store (a Quick Yield id, an unknown or an expired id refuses).
  3. multi-configuration lens, the DLS back-gap preflight and (on a push) the existing ``_resolve_cb_group`` run
     (``cb_group`` takes ``decenter_mm`` and/or ``tilt_deg``; ``cb_tilt`` and ``dls_cb_tilt`` stay refused).
  4. the inner sensitivity run (always wavefront / paraxial / the caller's budget, built fresh): the row source.
  5. ``_wfd_acquire.acquire_pack`` (T = 37, batch route; row gate; teardown proof) -> ``_wfd_gauge.evaluate`` ->
     the result store -> ``paired`` against the baseline.
  6. the envelope: ``monte_carlo`` keys exactly, with ``state_mutated`` read from the teardown proof.

The gauge never raises: ``run`` returns an error envelope for every failure.
"""
import hashlib
import json
import math
import os
import time

from .._io import is_finite_number, safe_exc
from . import _analysis_common as _ac
from . import _tolerance_common as _tc

TOOL = "tolerance"
MODE = "gauge"
DEFAULT_TRIALS = 10000
MIN_TRIALS = 100
MAX_TRIALS = 200000
DEFAULT_CAP_S = 600.0             # acquisition budget (the spec's gate is 240 s per call; this is the hang guard)
DEFAULT_COMPENSATOR = "dls"       # the judge's, not the tool's default
ALLOWED = ("mode", "budget", "threshold", "compensator", "cb_group", "trials", "full", "cap_s", "baseline")
CRITERION_NAME = "worst_field_rms_wavefront"
BIAS_NOTE = ("own_drop was graded on the 10x start only (held out 2.05 % relative); the Zernike norm runs above the "
             "polychromatic RWCE on tilted states, so the gauge yield reads low")
COMPOSITION = {"variant_id": "own_drop", "route": "batch_opd", "terms": 37, "p1b_heldout_rel": 0.0205,
               "bias_note": BIAS_NOTE}
SENSITIVITY_ONLY = ("sensitivity", "worst_offenders", "rss_estimated_change", "rss_estimated_criterion",
                    "back_focus_change", "hotspots", "hotspots_withheld")
CARRIED = ("lens_units", "strehl_nominal", "tde_cleared", "authored_operands", "ran_operands", "refused_operands",
           "unaccounted_operands", "operand_errors", "known_gaps", "units_by_family", "units_ambiguous_families",
           "grin_perturbation", "reconciled_operand_keys", "reconciliation_mode")
RUN_SUMMARY_KEYS = ("criterion", "criterion_units", "compensator", "compensators", "cycle_cap", "sampling", "field_mode",
                    "budget_rule_id", "budget_digest", "n_budget_rows", "topology_digest", "nominal", "rss_change",
                    "rss_identity", "wall_s", "cores")
_CLOCK = time.monotonic


MUTATED_REMEDY = "reload the design with load_design: the inner sensitivity run left the lens changed"


def _refuse(message, family, reason=None):
    return _tc.ToleranceError(message, family=family, reason=reason)


def _tr():
    from . import tolerance_run
    return tolerance_run


def _g():
    from . import _wfd_gauge
    return _wfd_gauge


def _acq():
    from . import _wfd_acquire
    return _wfd_acquire


# --------------------------------------------------------------------------- #
# validate: pure, nothing is opened
# --------------------------------------------------------------------------- #
def _trials(params):
    if not dict.__contains__(params, "trials"):
        return DEFAULT_TRIALS
    v = dict.__getitem__(params, "trials")
    if isinstance(v, float) and math.isfinite(v) and v == int(v):
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or not MIN_TRIALS <= v <= MAX_TRIALS:
        raise _refuse("trials must be an integer in %d..%d, got %s; nothing was opened"
                      % (MIN_TRIALS, MAX_TRIALS, repr(v)[:40]), "gauge_param", "trials_invalid")
    return v


def validate(params):
    """-> the resolved config, or raises ``ToleranceError`` (``gauge_param`` / ``gauge_scope``)."""
    tr = _tr()
    if dict.__contains__(params, "criterion"):
        raise _refuse("the gauge computes the worst-field RMS wavefront itself; omit criterion (the loaded merit is not "
                      "read); nothing was opened", "gauge_scope", "criterion_refused")
    if dict.__contains__(params, "cb_tilt"):
        raise _refuse("the gauge takes cb_group (a decenter and/or tilt push: cb_group.tilt_deg), not cb_tilt; nothing was "
                      "opened", "gauge_scope", "cb_tilt_refused")
    unknown = sorted(repr(k) for k in params if k not in ALLOWED)
    if unknown:
        raise _refuse("mode='gauge' takes %s; unexpected %s (the gauge is deterministic: there is no seed); nothing was "
                      "opened" % (list(ALLOWED), unknown), "gauge_param", "unexpected_param")
    budget = tr._resolve_budget(params)
    if budget is None:
        raise _refuse("mode='gauge' needs budget={vendor, grade, element_decenter, element_tilt}; nothing was opened",
                      "gauge_param", "budget_required")
    if not dict.__contains__(params, "threshold"):
        raise _refuse("mode='gauge' needs a numeric threshold (waves); nothing was opened", "gauge_param", "threshold_required")
    thr = dict.__getitem__(params, "threshold")
    if isinstance(thr, list):
        raise _refuse("threshold takes ONE number in mode='gauge', got a list; nothing was opened", "gauge_param", "threshold_invalid")
    if isinstance(thr, bool):
        raise _refuse("threshold must be a number of waves, a boolean is not a number, got %s; nothing was opened" % repr(thr),
                      "gauge_param", "threshold_bool")
    if not is_finite_number(thr) or thr <= 0:
        raise _refuse("threshold must be a finite number > 0 (waves), got %s; nothing was opened" % repr(thr)[:40],
                      "gauge_param", "threshold_invalid")
    comp = DEFAULT_COMPENSATOR
    if dict.__contains__(params, "compensator"):
        comp = dict.__getitem__(params, "compensator")
    if comp in ("paraxial", "dls_cb_tilt"):
        raise _refuse("compensator=%r is not available in the gauge (it solves the judge's compensators: 'dls' or "
                      "'dls_cb_group'%s); nothing was opened"
                      % (comp, "; a tilt push is compensator='dls_cb_group' with cb_group.tilt_deg" if comp == "dls_cb_tilt" else ""),
                      "gauge_scope", "compensator_refused")
    if comp not in ("dls", "dls_cb_group"):
        raise _refuse("compensator must be 'dls' or 'dls_cb_group', got %s; nothing was opened" % repr(comp)[:40],
                      "gauge_param", "compensator_invalid")
    has_group = dict.__contains__(params, "cb_group")
    raw = dict.__getitem__(params, "cb_group") if has_group else None
    if comp == "dls_cb_group" and not has_group:
        raise _refuse("compensator='dls_cb_group' needs cb_group={group, decenter_mm and/or tilt_deg}; nothing was opened", "gauge_param", "cb_group_required")
    if comp == "dls" and has_group:
        raise _refuse("cb_group needs compensator='dls_cb_group'; nothing was opened", "gauge_param", "cb_group_needs_compensator")
    cap_s, cap_source = DEFAULT_CAP_S, "default"
    if dict.__contains__(params, "cap_s"):
        cap_s = dict.__getitem__(params, "cap_s")
        if isinstance(cap_s, bool) or not is_finite_number(cap_s) or cap_s <= 0:
            raise _refuse("cap_s must be a finite number > 0 (seconds), got %s; nothing was opened" % repr(cap_s)[:40],
                          "gauge_param", "cap_s_invalid")
        cap_s, cap_source = float(cap_s), "param"
    baseline = None
    if dict.__contains__(params, "baseline"):
        baseline = dict.__getitem__(params, "baseline")
        if not isinstance(baseline, str):
            raise _refuse("baseline must be a gauge id string, got %s; nothing was opened" % type(baseline).__name__,
                          "gauge_param", "baseline_invalid")
    return {"budget": budget, "threshold": float(thr), "compensator": comp, "cb_group_raw": raw, "trials": _trials(params),
            "full": tr._bool_param(params, "full", False), "cap_s": cap_s, "cap_source": cap_source, "baseline": baseline}


def stable_doc(pack):
    """The pack as the numeric core hashes it: without the volatile timings and canaries, so ``gauge.id`` is a function
    of the lens, the budget and the readings only."""
    return {k: v for k, v in pack.items() if k not in ("canaries", "timings")}


def doc_sha(doc):
    """The numeric core's own pack hash (``Pack.sha``), for a pack the core refused to load."""
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def capture_pack(session, doc, sha):
    """Write the pack JSON to the workspace (``candidates/scratch/wfd_pack_<sha>.json``) so a failed or odd call can be
    re-scored offline with no seat. Best effort: ``None`` when there is no workspace or the write fails; never raises."""
    try:
        from . import _workspace_paths as wp
        scratch, _fault = wp.scratch_dir_from_root_state(wp.root_state(session))
        if not scratch:
            return None
        os.makedirs(scratch, exist_ok=True)
        path = os.path.join(scratch, "wfd_pack_%s.json" % sha)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, default=str)
        return path.replace("\\", "/")
    except Exception:  # noqa: BLE001 -- the capture must never sink the call it documents
        return None


def lookup_baseline(baseline):
    """The stored baseline result (pure), or a ``gauge_param`` refusal (a Quick Yield id included)."""
    if baseline is None:
        return None
    got = _g().store_get(baseline)
    if _g().is_refusal(got):
        raise _refuse(got["message"], "gauge_param", "baseline_unknown")
    return got


# --------------------------------------------------------------------------- #
# the pure pieces of the envelope
# --------------------------------------------------------------------------- #
def cb_group_summary(spec, res, pack, restored, full):
    """``run_summary.cb_group`` in MC's own key set, from the gauge's clip counts (the gauge authors no CPAR rows)."""
    g = _g()
    pars = spec["pars"]
    eff = {par: float(pack["limits"][g.COMP_LIMIT[g.PAR_LABEL[par]]]) for par in pars}
    by = {a["label"]: a for a in res["axes"]}
    axes = []
    for par in sorted(pars):
        axis, units = _tc._CPAR_AXES[par]
        r = eff[par]
        a = by.get(g.PAR_LABEL[par]) or {"n_at_min": 0, "n_at_max": 0}
        axes.append({"par": par, "axis": axis, "units": units, "range": r, "minimum": None, "maximum": None,
                     "saturated": bool(a["n_at_min"] > 0 and a["n_at_max"] > 0),
                     "n_at_min": a["n_at_min"], "n_at_max": a["n_at_max"]})
    ab = list(res["at_bound"])
    out = {"group": spec["group"], "decenter_mm": spec["decenter_mm"], "tilt_deg": spec["tilt_deg"],
           "effective_decenter_mm": eff.get(2), "effective_tilt_deg": eff.get(4), "axes": axes,
           "saturated": any(a["saturated"] for a in axes), "n_trials_at_bound": int(sum(1 for f in ab if f)),
           "layout": None, "zero_tilt_max_abs": None, "layout_tde_rows": None, "restored": bool(restored)}
    if full:
        out["per_trial_at_bound"] = ab
    return out


def run_summary(sens_rs, cfg, res, wall_s, spec, pack, restored):
    """The monte_carlo ``run_summary`` key set, the gauge's own values where the native ones do not exist."""
    tr = _tr()
    rule = sens_rs.get("budget_rule_id")
    if spec is not None and rule is not None:
        g = _g()
        rule = tr._cb_group_rule_id(rule, spec, {par: float(pack["limits"][g.COMP_LIMIT[g.PAR_LABEL[par]]])
                                                 for par in (2, 4) if par in spec["pars"]})
    out = {"criterion": CRITERION_NAME, "criterion_units": "waves", "compensator": cfg["compensator"], "compensators": [],
           "cycle_cap": None, "sampling": sens_rs.get("sampling"), "field_mode": sens_rs.get("field_mode"),
           "budget_rule_id": rule, "budget_digest": sens_rs.get("budget_digest"), "n_budget_rows": sens_rs.get("n_budget_rows"),
           "topology_digest": sens_rs.get("topology_digest"), "nominal": res["nominal_criterion"], "rss_change": None,
           "rss_identity": None, "wall_s": round(wall_s, 3), "cores": None}
    out["cap_s"], out["cap_source"] = cfg["cap_s"], cfg["cap_source"]
    if spec is not None:
        out["cb_group"] = cb_group_summary(spec, res, pack, restored, cfg["full"])
    return out


def gauge_block(res, pack, acq, sens_wall_s, timings):
    """``run_summary.gauge``: A's identity / SE block, completed with the acquisition's disclosures."""
    block = dict(res["gauge"])
    block.update({"role": "screen", "read": "batch_opd", "terms": 37, "composition": dict(COMPOSITION),
                  "delta0": acq.get("delta0"), "folded_share": acq.get("folded_share", block.get("folded_share")),
                  "demoted": list(acq.get("demoted") or []), "canary": (pack.get("canaries") or {}).get("row_gate"),
                  "push_column_check": acq.get("push_column_check") or {}, "twav_um": acq.get("twav_um"),
                  "pack_path": acq.get("pack_path"),
                  "timings": dict(timings, sens_s=round(sens_wall_s, 3))})
    return block


def compensation_note(objective):
    return ("The reported criterion (worst-field RMS wavefront, waves) is a LINEAR MODEL of the judge's run: per-field "
            "Zernike vectors read once at +-each tolerance row, the back-focus gap and the group push (decenter and/or tilt) solved per draw by %s "
            "minimisation, a push at its limit failing. It screens candidates in the loop; native Monte Carlo is the "
            "judge, and the gauge reads low where the Zernike norm runs above the polychromatic RWCE (see "
            "run_summary.gauge.composition)." % ("worst-field" if objective == "worst_field" else "composite"))


def build_envelope(sens_env, cfg, res, pack, acq, spec, wall_s, timings, paired_block, sens_wall_s):
    """The monte_carlo envelope plus ``per_field`` (and ``paired``). ``res`` is the evaluate result without ``_arrays``."""
    tr = _tr()
    objective = res["gauge"]["refocus_objective"]
    restored = bool(((pack.get("canaries") or {}).get("teardown") or {}).get("restored"))
    warnings = list(sens_env.get("warnings") or []) + list(acq.get("warnings") or []) + list(res["gauge"].get("warnings") or [])
    out = {"ok": True, "tool": TOOL, "mode": MODE, "criterion": CRITERION_NAME, "criterion_units": "waves",
           "compensation": "model_worst_field" if objective == "worst_field" else "model_composite",
           "compensator_participates": True, "criterion_includes_back_focus_refocus": True,
           "compensation_note": compensation_note(objective),
           "nominal_criterion": res["nominal_criterion"], "state_mutated": (not restored) or sens_env.get("state_mutated") is True, "warnings": warnings,
           "report_path": None}
    for key in CARRIED:
        if key in sens_env:
            out[key] = sens_env[key]
    out["run_summary"] = run_summary(sens_env.get("run_summary") or {}, cfg, res, wall_s, spec, pack, restored)
    out["run_summary"]["gauge"] = gauge_block(res, pack, acq, sens_wall_s, timings)
    out["compensator_statistics"] = res["compensator_statistics"]
    if sens_env.get("grin_tolerance_coverage_warning") is not None:
        out["grin_tolerance_coverage_warning"] = sens_env["grin_tolerance_coverage_warning"]
    out["trials"] = cfg["trials"]
    out["deterministic"] = True
    out["seed_supported"] = False
    out["seed"] = None  # the gauge takes no seed; key present for parity with tolerance (r2)
    out["monte_carlo"] = res["monte_carlo"]
    out["yield"] = tr._mc_yield_block(list(res["values"]), cfg["compensator"], list(res["at_bound"]), cfg["threshold"], CRITERION_NAME)
    if cfg["full"]:
        out["per_trial"] = list(res["per_trial"])
    out["per_field"] = res["per_field"]
    if paired_block is not None:
        out["paired"] = paired_block
    return out


# --------------------------------------------------------------------------- #
# the call
# --------------------------------------------------------------------------- #
def run(session, params):
    """``tolerance(mode="gauge")``. Never raises."""
    try:
        return _run(session, params)
    except _tc.ToleranceError as exc:
        reason = getattr(exc, "reason", None)
        return _ac.error_envelope(TOOL, getattr(exc, "family", "tolerancing_param"), str(exc), mode=MODE,
                                  **({} if reason is None else {"reason": reason}))
    except Exception as exc:  # noqa: BLE001 -- the dispatch net
        return _ac.error_envelope(TOOL, "tolerancing_run", "gauge hit an unexpected fault (%s)" % safe_exc(exc),
                                  mode=MODE, reason="internal_error")


def _from_refusal(r, extra=None):
    """A refusal dict of the numeric core / the acquisition -> the served error envelope."""
    fam = r.get("error_family") or "tolerancing_run"
    extra = dict(extra or {})
    for key in ("reason", "restore", "recovery", "folded_share"):
        if r.get(key) is not None:
            extra[key] = r[key]
    if r.get("error_family") == "state_mutated":
        extra["state_mutated"] = True
    return _ac.error_envelope(TOOL, fam, r.get("message") or r.get("error") or "the gauge refused", mode=MODE, **extra)


def _run(session, params):
    t0 = _CLOCK()
    if not isinstance(params, dict):
        raise _refuse("mode='gauge' takes an object of parameters", "gauge_param")
    tr, g = _tr(), _g()
    cfg = validate(params)
    base = lookup_baseline(cfg["baseline"])
    system = session.system
    try:
        tr._multi_config_refusal(system, "gauge")
    except _tc.ToleranceError as exc:
        raise _refuse(str(exc), "gauge_scope", getattr(exc, "reason", None))
    tr._dls_preflight(system)
    spec = push = None
    if cfg["compensator"] == "dls_cb_group":
        spec = tr._resolve_cb_group({"cb_group": cfg["cb_group_raw"]}, "dls_cb_group", "monte_carlo", cfg["budget"], system, True)
        push = {"group": spec["group"], "a": spec["a"], "b": spec["b"], "decenter_mm": spec["decenter_mm"],
                "pars": dict(spec["pars"])}
    # the row source: always wavefront / paraxial, built fresh (never the caller's dict)
    t1 = _CLOCK()
    sens = tr.tolerance(session, {"mode": "sensitivity", "budget": cfg["budget"], "criterion": "wavefront",
                                  "compensator": "paraxial"})
    sens_wall = _CLOCK() - t1
    if not (isinstance(sens, dict) and sens.get("ok") is True):
        if isinstance(sens, dict):
            # code audit C1 HIGH 2: serve the inner failure envelope as it came (state_mutated, recovery, restored, tde_cleared,
            # warnings, reason) with the gauge mode
            return dict(sens, ok=False, tool=TOOL, mode=MODE, error_family=sens.get("error_family") or "tolerancing_run")
        raise _refuse("the inner sensitivity run returned no envelope", "tolerancing_run")
    if sens.get("state_mutated") is True:
        # code audit C1 HIGH 2: the inner run says it left the lens changed; acquiring would build on that state
        return _ac.error_envelope(TOOL, "state_mutated", "the inner sensitivity run left the lens changed; the gauge stopped "
                                  "before acquiring", mode=MODE, state_mutated=True, warnings=sens.get("warnings") or [],
                                  recovery=sens.get("recovery") or MUTATED_REMEDY)
    t2 = _CLOCK()
    pack = _acq().acquire_pack(session, sens, push=push, cap_s=cfg["cap_s"], budget=cfg["budget"])
    if g.is_refusal(pack):
        return _from_refusal(pack, {"inner_sensitivity_s": round(sens_wall, 3)})
    acq = (pack.get("canaries") or {}).get("acquire") or {}
    acquire_s = _CLOCK() - t2
    pack["criterion"]["threshold"] = cfg["threshold"]
    stable = stable_doc(pack)
    loaded = g.load_pack(stable)
    pack_path = capture_pack(session, stable, doc_sha(stable) if g.is_refusal(loaded) else loaded.sha)
    acq = dict(acq, pack_path=pack_path)
    if g.is_refusal(loaded):
        return _from_refusal(loaded, {"pack_path": pack_path})
    t3 = _CLOCK()
    res = g.evaluate(loaded, threshold=cfg["threshold"], n=cfg["trials"], full=cfg["full"])
    if g.is_refusal(res):
        return _from_refusal(res, {"pack_path": pack_path})
    evaluate_s = _CLOCK() - t3
    put = g.store_put(res)
    if g.is_refusal(put):
        return _from_refusal(put)
    paired_block = None
    if base is not None:
        paired_block = g.paired(base, res)
        if g.is_refusal(paired_block):
            return _from_refusal(paired_block)
    res.pop("_arrays", None)
    timings = {"acquire_s": round(acquire_s, 3), "evaluate_s": round(evaluate_s, 3),
               "per_state_ms": (pack.get("timings") or {}).get("per_state_ms"),
               "n_states": (pack.get("timings") or {}).get("n_states")}
    return build_envelope(sens, cfg, res, pack, acq, spec, _CLOCK() - t0, timings, paired_block, sens_wall)
