"""tools/_tolerance_quick_yield.py -- ``tolerance(mode="quick_yield")``: OpticStudio's Quick Yield analysis. NOT dispatchable.

WHAT IT IS. One call = the wizard budget on the loaded lens, then ``repeats`` rounds of Quick Yield analyses
(``IAS_QuickYield``) with ``CompensatorStrategy = OptimizeAllDampedLeastSquares`` (a back-focus refocus), all
wavelengths. By DEFAULT a round is one analysis PER FIELD (``Field.SetFieldNumber(k)``, k = 1..F); with
``criterion='composite'`` a round is one "Field: All" analysis. It is a fast, REFOCUS-ONLY in-loop screen (about 20-25 s
per analysis at pupil 2 on the 10x lens). Native ``monte_carlo`` / ``pick_push`` stay the judge.

GENERALLY DO NOT USE IT WITH THE PUSH. Quick Yield with a push compensator (CPAR rows on a coordinate-break pair)
read 83 % on a lens whose native push judge read 57.5 %; the gap opens only once the push compensates, and the push
is the part Quick Yield models worst (it cannot count at-bound pushes and has no worst-field statistic). So this mode
builds NO push variant: no coordinate-break pair, no CPAR rows, and a ``cb_group`` / ``cb_tilt`` request is refused.
The push group is identified by one native ``pick_push`` / ``monte_carlo`` call at the end of the loop, never here.

WHAT THE NUMBER IS (per field, the default). Each field's own Quick Yield cumulative curve of the RMS wavefront, read
at that field's threshold (``threshold`` is a number for every field, or a list with one value per field). Every field is
drawn SEPARATELY, so each field's yield is a MARGINAL pass rate. The headline (``worst_field`` and the top-level
``yield``) is the LOWEST of those marginals (tie: the higher P90): an UPPER BOUND on the pass rate of every field
passing in the SAME trial, which is what the native ``monte_carlo`` / ``pick_push`` judge scores. A censored field read is
a bound: when another field's bound can lie below the picked field's yield (or two exact fields tie on yield and P90) the
worst field is ``selection_uncertain`` (``competing_fields`` names them) and the headline is ``censored``, so a paired
read never calls it ``resolved``; a headline censored from above (``>=``) is only a lower bound of the upper bound. It is not comparable
with the yield of ``monte_carlo``; compare a Quick Yield result with another Quick Yield result of the same statistic
and field count only (``baseline=<id>``; anything else is refused ``baseline_incomparable``). With
``criterion='composite'`` the number is the "Field: All" COMPOSITE RMS wavefront curve read at one threshold: it
averages the fields, so a change that buys lower sensitivity by spending edge-field nominal still reads as a gain. It
has no seed (see ``_SEED_ATTRS``), so two identical calls differ; ``deterministic`` is False.

FAILURE PATHS. The tool never edits the lens: it proves the LDE unchanged (``state_mutated``) and clears the TDE
it authored. The ONE exception is a capped run whose stop could not be confirmed: then nothing below it touches the
analysis, the lens or the TDE (``cap_stop_failed``, ``state_mutated`` True, the release-the-engine recovery).

MEMORY. One Quick Yield analysis holds about 3.7 GB per 1000 runs inside OpticStudio until it is released. A call runs
F x ``repeats`` analyses (``repeats`` when composite). The call refuses before it opens anything when the host's
available memory, less the whole call's expected hold, would fall below a floor; ``Close()`` is called on every analysis; the host's available memory and (when the session's own engine
pid is known) the engine's private bytes are reported before and after, with the growth since this engine was first
seen. Measured live: ``Close()`` does NOT release the analysis memory, so a long loop in one engine accumulates it;
the remedy is ``release_engine`` (named in the refusal), and the tool tries nothing else to free it. A soft limit
(``SOFT_PRIVATE_GB``) warns before the floor refuses.

THE CAP. ``cap_s`` bounds the POLLED phase, including the first moments after ``Apply`` (``IsRunning`` must read false
twice in a row to count). ``WaitForCompletion`` runs only after that and is the one unbounded call; the cap does not
cover it.
"""
import math
import time
import uuid
from collections import OrderedDict

from .._io import is_finite_number, safe_exc
from . import _analysis_common as _ac
from . import _tolerance_common as _tc

TOOL = "tolerance"
MODE = "quick_yield"

QY_MIN_N = 1000                 # NumMonteCarlo below this raises ArgumentException in the engine (probe FINDINGS Q3)
DEFAULT_PUPIL = 2               # 2 is ~20-25 s, 4 is ~42 s on the 10x (the diagnosis runs)
DEFAULT_THRESHOLD = 0.0714      # waves: the desensitize skill's Marechal threshold
DEFAULT_REPEATS = 1
MAX_REPEATS = 10
DEFAULT_CAP_S = 600.0           # per analysis
CANCEL_WAIT_S = 10.0            # after a cap: Cancel (if the analysis has one), then watch IsRunning this long
POLL_INTERVAL_S = 0.5
STORE_MAX = 16                  # a bench parent must survive a desensitize round; results are small dicts
LEAK_GB_PER_1000 = 3.7          # engine private bytes per 1000-run Quick Yield analysis until it is released
MEM_FLOOR_GB = 20.0             # the prototype's floor
SOFT_PRIVATE_GB = 30.0          # warn (never refuse) once the engine's private bytes pass this: about eight 10x-lens
                                # 1000-run analyses (3.7 GB each) that Close() did not release (live gate)
SETTLE_READS = 2                # IsRunning must read false this many CONSECUTIVE polls (one interval apart) to count as done
ALL_SELECTED = 0                # the read-back of Field.UseAllFields() / Wavelength.UseAllWavelengths()
RELEASE_REMEDY = "call release_engine (Close() does not free the analysis memory; a fresh engine does), then reload the lens"
STRATEGY = "OptimizeAllDampedLeastSquares"
# Candidate Quick Yield settings names for a random seed. None is documented; the run probes for them and ECHOES a hit.
_SEED_ATTRS = ("Seed", "RandomSeed", "MonteCarloSeed")
_FORBIDDEN = ("tolerances", "include_mechanical", "strict", "output_file", "full")
_PUSH_KEYS = ("cb_group", "cb_tilt")
STATISTIC = ("Quick Yield, one analysis per field: each field's RMS wavefront (waves) cumulative curve read at that field's "
             "threshold. The headline is the LOWEST of the per-field MARGINAL pass rates (each field drawn separately), "
             "an UPPER BOUND on the pass rate of every field passing in one trial (the native monte_carlo / pick_push "
             "judge); NOT comparable with their yields; compare only with another per-field quick_yield result")
STATISTIC_COMPOSITE = ("Quick Yield 'Field: All' composite RMS wavefront (waves) read off its cumulative curve: NOT the worst-field "
                       "judge statistic and NOT comparable with monte_carlo / pick_push yields; compare only with another "
                       "quick_yield result")
PER_FIELD = "per_field"
COMPOSITE = "composite"
ENGINE_BUSY_RECOVERY = (
    "the engine may still be running a Quick Yield analysis: do not edit this lens in this engine; "
    "release the engine (CloseApplication) and reload the lens in a fresh one")
BASELINE_REMEDY = "rerun the baseline quick_yield in this session and pass its id"

# seams a test replaces (by NAME on this module)
_clock = time.perf_counter
_sleep = time.sleep


def _mem_available_gb():
    """Host available memory in GB (psutil; a test replaces this seam)."""
    import psutil
    return psutil.virtual_memory().available / 1e9


def _engine_private_gb(session):
    """-> ``(gb_or_None, note_or_None)``: the private bytes of the session's OWN engine, only when it is unambiguous.

    The session tracks the pids it spawned (never a name scan). Exactly one pid is required; anything else is
    reported as unknown with the reason, not guessed. When the platform offers no private-bytes counter the working set
    is read instead and the note SAYS so (the number is then not the engine's private bytes).
    """
    try:
        tracked = getattr(session, "_tracked", None)
        pids = sorted(tracked) if tracked else []
        if len(pids) != 1:
            return None, f"the session tracks {len(pids)} engine pid(s); need exactly one"
        import psutil
        info = psutil.Process(int(pids[0])).memory_info()
        private = getattr(info, "private", None)
        if private is not None:
            return round(private / 1e9, 3), None
        return round(info.rss / 1e9, 3), "working set (rss): private bytes are not offered on this platform"
    except Exception as exc:  # noqa: BLE001 -- a memory read never fails the run
        return None, f"unreadable ({type(exc).__name__})"


def _engine_pid(session):
    """The session's one tracked engine pid, or ``None`` (none, several, or unreadable)."""
    try:
        tracked = getattr(session, "_tracked", None)
        pids = sorted(tracked) if tracked else []
        return int(pids[0]) if len(pids) == 1 else None
    except Exception:  # noqa: BLE001
        return None


_ENGINE_FIRST = {}              # engine pid -> lowest private GB seen by this tool (a new engine has a new pid)


def memory_clear():
    _ENGINE_FIRST.clear()


def _note_engine(session, gb):
    """Record ``gb`` as the engine's reference when it is the first (or a lower) reading; -> the pid or ``None``."""
    pid = _engine_pid(session)
    if pid is None or gb is None:
        return None
    if pid not in _ENGINE_FIRST or gb < _ENGINE_FIRST[pid]:
        _ENGINE_FIRST[pid] = gb
    return pid


def engine_growth_gb(session, gb):
    """Private GB gained since this tool first saw the session's engine, or ``None`` when it cannot be known."""
    pid = _note_engine(session, gb)
    return None if pid is None else round(gb - _ENGINE_FIRST[pid], 3)


class EngineBusy(_tc.ToleranceError):
    """A capped analysis whose stop could not be confirmed: nothing may touch the analysis, the lens or the TDE."""

    def __init__(self, message):
        super().__init__(message, family="tolerancing_run", reason="cap_stop_failed")


def _refuse(message, reason, family="tolerancing_param"):
    return _tc.ToleranceError(message, family=family, reason=reason)


def _tr():
    """``tolerance_run`` (lazy: it imports this module lazily, so neither import order can cycle)."""
    from . import tolerance_run
    return tolerance_run


# --------------------------------------------------------------------------- #
# the result store (process memory, bounded, insertion order)
# --------------------------------------------------------------------------- #
_STORE = OrderedDict()


def store_put(result):
    """Keep ``result`` under its id; the oldest is evicted beyond ``STORE_MAX``. The id is unique per run (no seed)."""
    _STORE[result["id"]] = result
    while len(_STORE) > STORE_MAX:
        _STORE.popitem(last=False)


def store_get(result_id):
    return _STORE.get(result_id)


def store_clear():
    _STORE.clear()


def new_id():
    """A fresh id per run. NOT a hash of the inputs: with no seed, two identical calls are two results, and the id of
    the first must stay retrievable. The ``qy-`` prefix keeps the MCP argument re-parse from turning an all-digit
    hex id into a number."""
    return "qy-" + uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# validate: every refusal is pure -- nothing is opened
# --------------------------------------------------------------------------- #
def _int_param(params, key, default, low, high=None):
    if not dict.__contains__(params, key):
        return default
    value = dict.__getitem__(params, key)
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _refuse(f"{key} must be an integer, got {value!r}; nothing was opened", f"{key}_invalid")
    if value < low or (high is not None and value > high):
        bound = f">= {low}" if high is None else f"in {low}..{high}"
        raise _refuse(f"{key} must be {bound}, got {value}; nothing was opened", f"{key}_invalid")
    return value


def validate(params):
    """-> the resolved config. Refuses a bad call BEFORE anything opens (``tolerancing_param``)."""
    tr = _tr()
    for key in _FORBIDDEN:
        if dict.__contains__(params, key):
            raise _refuse(f"mode='quick_yield' does not take {key!r}; nothing was opened", f"param_not_allowed:{key}")
    for key in _PUSH_KEYS:
        if dict.__contains__(params, key):
            raise _refuse(
                f"mode='quick_yield' is refocus-only: {key!r} (a push compensator) is not built, because Quick Yield "
                "is generally not for use with the push. Identify the push group with one native pick_push / "
                "monte_carlo call; nothing was opened", "push_not_built")
    if dict.__contains__(params, "sampling"):
        raise _refuse("mode='quick_yield' takes 'pupil' (Quick Yield's PupilSampling), not 'sampling'; "
                      "nothing was opened", "param_not_allowed:sampling")
    statistic = PER_FIELD
    if dict.__contains__(params, "criterion"):
        criterion = dict.__getitem__(params, "criterion")
        if criterion not in ("wavefront", COMPOSITE):
            raise _refuse("mode='quick_yield' reads the RMS wavefront: criterion must be absent or 'wavefront' (one "
                          "analysis per field, the default) or 'composite' (one 'Field: All' analysis); "
                          f"got {criterion!r}; nothing was opened", "criterion_not_wavefront")
        statistic = COMPOSITE if criterion == COMPOSITE else PER_FIELD
    if dict.__contains__(params, "compensator"):
        comp = dict.__getitem__(params, "compensator")
        if comp == "paraxial":
            raise _refuse("Quick Yield with ParaxialFocus returns no results (probe d1), so compensator='paraxial' is "
                          "refused; omit compensator or pass 'dls' (OptimizeAllDampedLeastSquares); nothing was opened",
                          "paraxial_focus_no_results")
        if comp != "dls":
            raise _refuse(f"mode='quick_yield' admits compensator 'dls' (the default) only, got {comp!r}; "
                          "nothing was opened", "compensator_invalid")
    if not isinstance(dict.get(params, "budget"), dict):
        raise _refuse("mode='quick_yield' needs budget={vendor, grade, element_decenter, element_tilt}; "
                      "nothing was opened", "budget_required")
    budget = tr._resolve_budget(params)
    trials = _int_param(params, "trials", QY_MIN_N, 1)
    if trials < QY_MIN_N:
        raise _refuse(f"trials must be >= {QY_MIN_N} (Quick Yield raises ArgumentException below that), got {trials}; "
                      "nothing was opened", "trials_below_minimum")
    pupil = _int_param(params, "pupil", DEFAULT_PUPIL, 1)
    repeats = _int_param(params, "repeats", DEFAULT_REPEATS, 1, MAX_REPEATS)
    threshold = DEFAULT_THRESHOLD
    if dict.__contains__(params, "threshold"):
        threshold = dict.__getitem__(params, "threshold")
        if isinstance(threshold, list):
            if statistic == COMPOSITE:
                raise _refuse("threshold as a list (one value per field) needs the per-field statistic: "
                              "criterion='composite' has one curve and one threshold; nothing was run",
                              "threshold_list_composite")
            bad = [v for v in threshold if isinstance(v, bool) or not is_finite_number(v) or v <= 0]
            if bad:
                raise _refuse(f"every threshold entry must be a finite number > 0 (waves), got {bad!r} in {threshold!r}; "
                              "nothing was run", "threshold_invalid")
            threshold = [float(v) for v in threshold]
        elif isinstance(threshold, bool) or not is_finite_number(threshold) or threshold <= 0:
            raise _refuse(f"threshold must be a finite number > 0 (waves) or a list of them, one per field, got "
                          f"{threshold!r}; nothing was opened", "threshold_invalid")
        else:
            threshold = float(threshold)
    cap_s = DEFAULT_CAP_S
    if dict.__contains__(params, "cap_s"):
        cap_s = dict.__getitem__(params, "cap_s")
        if isinstance(cap_s, bool) or not is_finite_number(cap_s) or cap_s <= 0:
            raise _refuse(f"cap_s must be a finite number > 0 (seconds per analysis), got {cap_s!r}; nothing was opened",
                          "cap_s_invalid", family="tolerancing_param")
        cap_s = float(cap_s)
    baseline = None
    if dict.__contains__(params, "baseline"):
        baseline = dict.__getitem__(params, "baseline")
        baseline = store_get(baseline) if isinstance(baseline, str) else None
        if baseline is None:
            raise _refuse(f"baseline {dict.__getitem__(params, 'baseline')!r} is not a quick_yield result held by this process (a server restart, "
                          f"or {STORE_MAX} newer results, empties it); {BASELINE_REMEDY}; nothing was opened",
                          "baseline_unknown")
    return {"budget": budget, "trials": trials, "pupil": pupil, "repeats": repeats, "threshold": threshold,
            "cap_s": cap_s, "baseline": baseline, "statistic": statistic}      # the baseline RESULT, held now: a new put may evict its id


def read_field_count(system):
    """The lens's field count F (>= 1), read before any analysis opens; an unreadable count is a refusal, never a guess."""
    try:
        n = int(system.SystemData.Fields.NumberOfFields)
    except Exception as exc:  # noqa: BLE001 -- a foreign engine error is rendered safely
        raise _refuse(f"the lens's field count could not be read ({safe_exc(exc)}); nothing was run",
                      "field_count_unreadable", family="tolerancing_run")
    if n < 1:
        raise _refuse(f"the lens reports {n} fields; nothing was run", "field_count_unreadable", family="tolerancing_run")
    return n


def field_thresholds(threshold, n_fields):
    """-> the threshold of each field 1..F: a number applies to every field, a list gives one per field (its length must be F)."""
    if isinstance(threshold, list):
        if len(threshold) != n_fields:
            raise _refuse(f"threshold lists {len(threshold)} value(s) but the lens has {n_fields} field(s): give one per "
                          "field in field order, or one number for all; nothing was run", "threshold_length")
        return list(threshold)
    return [threshold] * n_fields


def check_baseline(base, statistic, n_fields):
    """Refuse a baseline of another statistic (per field vs composite) or another field count; ``None`` passes."""
    if base is None:
        return
    key = base.get("key") or {}
    if key.get("statistic") != statistic or key.get("field_count") != n_fields:
        raise _refuse(f"baseline {base.get('id')!r} is not comparable: it used the {key.get('statistic')!r} statistic over "
                      f"{key.get('field_count')} field(s), this call uses {statistic!r} over {n_fields} field(s). Rerun "
                      "the baseline with the same criterion on this lens; nothing was run", "baseline_incomparable")


def check_memory(n_runs_left, trials, available_gb):
    """-> ``(ok, need_gb)``: the host can hold ``n_runs_left`` analyses of ``trials`` runs above the floor."""
    need = LEAK_GB_PER_1000 * trials / 1000.0 * n_runs_left
    return (available_gb - need) >= MEM_FLOOR_GB, need


# --------------------------------------------------------------------------- #
# the curve: pure
# --------------------------------------------------------------------------- #
def yield_at(xs, ys, thr):
    """Yield (fraction) at ``thr`` from the cumulative curve (xs = criterion at yield ys percent, ascending).

    Linear inside ``[xs[0], xs[-1]]``. Outside, the curve says only "yield <= ys[0]/100" (``bound`` 'upper') or
    ">= ys[-1]/100" ('lower'): a bound, NEVER an extrapolation.
    """
    if not xs or len(xs) != len(ys):
        return {"value": None, "bound": "invalid"}
    if thr < xs[0]:
        return {"value": ys[0] / 100.0, "bound": "upper"}
    if thr >= xs[-1]:
        return {"value": ys[-1] / 100.0, "bound": "lower"}
    for i in range(1, len(xs)):
        if xs[i - 1] <= thr < xs[i]:
            span = xs[i] - xs[i - 1]
            t = 0.0 if span <= 0 else (thr - xs[i - 1]) / span
            return {"value": (ys[i - 1] + t * (ys[i] - ys[i - 1])) / 100.0, "bound": "exact"}
    return {"value": None, "bound": "invalid"}


def x_at_yield(xs, ys, pct):
    """Criterion at ``pct`` percent yield (linear on the curve); ``None`` outside the curve."""
    if not xs or pct < ys[0] or pct > ys[-1]:
        return None
    for i in range(1, len(xs)):
        if ys[i - 1] <= pct <= ys[i]:
            span = ys[i] - ys[i - 1]
            t = 0.0 if span <= 0 else (pct - ys[i - 1]) / span
            return xs[i - 1] + t * (xs[i] - xs[i - 1])
    return None


def _mean(v):
    return sum(v) / len(v) if v else None


def _sd(v):
    """Sample SD (n - 1); ``None`` for fewer than two values."""
    if len(v) < 2:
        return None
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def check_curve(xs, ys):
    """Refuse a curve that cannot be read: non-finite, too short, y outside 0-100 or either axis descending."""
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in list(xs) + list(ys))
    ok = (numbers and len(xs) == len(ys) and len(xs) >= 2
          and all(math.isfinite(v) for v in xs) and all(math.isfinite(v) for v in ys)
          and all(0.0 <= v <= 100.0 for v in ys)
          and all(b >= a for a, b in zip(xs, xs[1:])) and all(b >= a for a, b in zip(ys, ys[1:])))
    if not ok:
        raise _refuse(f"the Quick Yield curve is unreadable ({len(xs)} x / {len(ys)} y points, or non-finite, or not "
                      "ascending, or yield outside 0-100); refusing to report a guessed number",
                      "curve_invalid", family="tolerancing_parse")


def run_record(xs, ys, threshold):
    """The derived numbers of ONE analysis' curve."""
    ya = yield_at(xs, ys, threshold)
    return {"yield": ya["value"], "bound": ya["bound"], "p50": x_at_yield(xs, ys, 50.0),
            "p90": x_at_yield(xs, ys, 90.0), "mean": _mean(xs)}


def summarise(records, trials):
    """Pool the repeats: means, the repeat SD (more than one repeat) or a binomial SE (one), and the censored flag."""
    n = len(records)
    yields = [r["yield"] for r in records]
    p90s = [r["p90"] for r in records if r["p90"] is not None]
    p50s = [r["p50"] for r in records if r["p50"] is not None]
    means = [r["mean"] for r in records]
    bounds = [r["bound"] for r in records if r["bound"] != "exact"]
    y = _mean(yields)
    out = {"value": y, "censored": bool(bounds),
           "bound": (bounds[0] if len(set(bounds)) == 1 else ("mixed" if bounds else "exact")),
           "yields": yields, "repeat_sd": _sd(yields)}
    if n > 1:
        out["se"], out["se_kind"] = out["repeat_sd"] / math.sqrt(n), "repeat_sd/sqrt(repeats)"
    elif y is not None:
        out["se"] = math.sqrt(max(y * (1.0 - y), 0.0) / trials)
        out["se_kind"] = ("binomial (one repeat: a floor, not a repeat SD; assumes the N reconstructed draws are "
                          "independent)")
    else:
        out["se"], out["se_kind"] = None, None
    stats = {}
    for name, vals in (("p50", p50s), ("p90", p90s), ("mean", means)):
        sd = _sd(vals)
        stats[name] = {"value": _mean(vals), "values": vals, "repeat_sd": sd,
                       "se": None if sd is None else sd / math.sqrt(len(vals)),
                       "se_kind": None if sd is None else "repeat_sd/sqrt(repeats)"}
    return out, stats


def _same(a, b):
    """Equal for the comparability key: floats within 1e-12 relative, everything else by ``==``."""
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


COMPARABILITY_KEYS = ("threshold", "trials", "pupil", "compensator", "budget_digest", "topology_digest",
                      "statistic", "field_count")      # threshold is the per-field VECTOR for a per-field result
_DIGEST_KEYS = ("budget_digest", "topology_digest")


def paired(base, cand):
    """candidate - baseline for yield, P90 and mean, with a combined SE and a ``resolved`` flag at > 2 SE.

    Quick Yield has no seed, so the two results share no draws: the comparison is ALWAYS independent and the combined
    SE is ``sqrt(se_a^2 + se_b^2)``. ``comparable`` is False (with the first mismatch as ``reason``) when the two ran a
    different threshold, N, pupil, compensator, budget rows or topology; then ``resolved`` is forced False. A censored
    yield is a bound, not a number: it is never ``resolved``. A statistic with no SE (one repeat: P90 and mean) has
    ``resolved`` null.

    A per-field pair (both results carry ``fields``) adds ``fields`` (the same three differences for each field) and
    ``worst_field`` (the worst field of each read, whether they differ, and the worst-vs-worst differences, which are
    also the top-level ``d_*``).
    """
    unreadable = next((f"{k} was unreadable on the {side} (None): two unread digests are not equal" for k in _DIGEST_KEYS
                       for side, res in (("baseline", base), ("this run", cand)) if res["key"][k] is None), None)
    reason = unreadable or next((f"{k} differs: baseline {base['key'][k]!r}, this run {cand['key'][k]!r}"
                                 for k in COMPARABILITY_KEYS if not _same(base["key"][k], cand["key"][k])), None)
    comparable = reason is None

    def diff(name, a, b, censored=False):
        va, vb = a["value"], b["value"]
        if va is None or vb is None:
            return {"value": None, "se": None, "resolved": None}
        d = vb - va
        if a.get("se") is None or b.get("se") is None:
            return {"value": d, "se": None, "resolved": None if comparable else False}
        se = math.sqrt(a["se"] ** 2 + b["se"] ** 2)
        resolved = bool(comparable and not censored and abs(d) > 2.0 * se)
        return {"value": d, "se": se, "resolved": resolved}

    cens = base["yield"]["censored"] or cand["yield"]["censored"]
    out = {"pairing": "independent", "pairing_note": "no seed: the two results share no draws",
           "baseline_id": base["id"], "comparable": comparable, "reason": reason,
           "d_yield": diff("yield", base["yield"], cand["yield"], censored=cens),
           "d_p90": diff("p90", base["p90"], cand["p90"]),
           "d_mean": diff("mean", base["mean"], cand["mean"]),
           "censored": cens, "se_rule": "sqrt(se_a^2 + se_b^2); resolved when |difference| > 2 SE"}
    bf, cf = base.get("fields"), cand.get("fields")
    if bf and cf and len(bf) == len(cf):
        out["fields"] = [{"field": b["field"],
                          "d_yield": diff("yield", b["yield"], c["yield"], censored=b["yield"]["censored"] or c["yield"]["censored"]),
                          "d_p90": diff("p90", b["p90"], c["p90"]), "d_mean": diff("mean", b["mean"], c["mean"])}
                         for b, c in zip(bf, cf)]
        bw, cw = base["worst_field"]["field"], cand["worst_field"]["field"]
        out["worst_field"] = {
            "baseline_field": bw, "candidate_field": cw, "differs": bw != cw,
            "note": (None if bw == cw else f"the worst field moved from field {bw} (baseline) to field {cw} (this run): "
                     "the headline difference compares the worst field of each read, not one field"),
            "d_yield": out["d_yield"], "d_p90": out["d_p90"], "d_mean": out["d_mean"]}
    return out


# --------------------------------------------------------------------------- #
# the engine side
# --------------------------------------------------------------------------- #
def _qy_enum(system, namespace, name):
    """Reach a Quick Yield enum TYPE (``AnalysisIDM`` on ``ZOSAPI.Analysis``; ``QYPrecisions`` /
    ``QYCompensatorStrategy`` on ``ZOSAPI.Analysis.Tolerancing``). A fake injects ``_qy_enum_types[name]``."""
    injected = getattr(system, "_qy_enum_types", None)
    if injected is not None and name in injected:
        return injected[name]
    try:  # pragma: no cover - live backend path
        if name == "AnalysisIDM":
            import ZOSAPI.Analysis as _a  # type: ignore
            return _a.AnalysisIDM
        import ZOSAPI.Analysis.Tolerancing as _t  # type: ignore
        if name == "QYPrecisions":
            return _t.QYPrecisions
        if name == "QYCompensatorStrategy":
            return _t.QYCompensatorStrategy
        raise KeyError(name)
    except Exception as exc:  # noqa: BLE001 -- TYPE reach failure -> unavailable
        raise _tc.ToleranceError(f"could not reach the {name!r} enum on ZOSAPI.{namespace} ({safe_exc(exc, repr_form=True)}); "
                                 "Quick Yield is unavailable", family="tolerancing_unavailable")


def _sval(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return str(v)


class PollError(Exception):
    """``IsRunning`` could not be read in either form (the call failed with an engine error)."""


def make_is_running(an, probe):
    """A zero-argument ``running()`` for an analysis; the form is fixed on the FIRST call and recorded in ``probe``.

    ``IsRunning`` is a METHOD on an analysis (manual 10.6.6.3): ``bool(an.IsRunning)`` is a bound method and is ALWAYS
    true, which made every capped run in the first prototype read as hung. Poll 1 records the attribute's type, then
    CALLS it. Only a NON-callable attribute (a property after all) is read as a value. EVERY read, the first or a later
    one, that raises is an engine error (``PollError``): the analysis state is then unknown.
    """
    state = {}

    def running():
        try:
            if not state:
                attr = an.IsRunning
                probe["attr_type"] = str(type(attr))
                probe["bool_of_attr"] = bool(attr)
                if callable(attr):
                    try:
                        val = attr()
                    except Exception:
                        probe["form"] = "call_failed"
                        raise
                    state["form"] = "call"
                else:
                    val = attr
                    state["form"] = "property"
                probe["form"] = state["form"]
                probe["first_value"] = _sval(val)
                return bool(val)
            return bool(an.IsRunning()) if state["form"] == "call" else bool(an.IsRunning)
        except Exception as exc:  # noqa: BLE001
            probe["call_error"] = safe_exc(exc)
            raise PollError(probe["call_error"]) from exc

    return running


def poll_until_done(an, cap_s, probe):
    """Poll until ``IsRunning`` has read false ``SETTLE_READS`` times in a row or ``cap_s`` passes. Returns ``capped``.

    ``IsRunning`` can read false for an instant right after ``Apply``, so ONE false read is not "done": the settle
    window is inside the cap. ``WaitForCompletion()`` runs only after the settle, and on a cap it is never called
    (on a hung analysis it never returns).
    """
    running = make_is_running(an, probe)
    t0 = _clock()
    quiet = 0
    while True:
        quiet = 0 if running() else quiet + 1
        if quiet >= SETTLE_READS:
            break
        if _clock() - t0 > cap_s:
            return True
        _sleep(POLL_INTERVAL_S)
    an.WaitForCompletion()
    return False


def _stop_after_cap(an, probe):
    """Cancel (when the analysis has it) and watch ``IsRunning`` for ``CANCEL_WAIT_S``. -> True when it stopped."""
    cancel = getattr(an, "Cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:  # noqa: BLE001
            pass
    try:
        running = make_is_running(an, probe)            # every read of it raises PollError, never a foreign exception
        t0 = _clock()
        while running():
            if _clock() - t0 > CANCEL_WAIT_S:
                return False
            _sleep(POLL_INTERVAL_S)
        return True
    except PollError:
        return False


def _read_curve(an):
    """-> ``(xs, ys, x_label)`` from the analysis' first data series; refuses an absent or unreadable result.

    The arrays are marshalled by rank through the shared ``_analysis_common`` marshallers (a non-finite cell becomes a
    string sentinel and ``check_curve`` refuses it); there is no rank fallback. ``x_label`` is the engine's own label
    or ``None`` when the series has none: it is never defaulted to a made-up string.
    """
    res = an.GetResults()
    ds = None if res is None else res.GetDataSeries(0)
    if ds is None:
        raise _refuse("Quick Yield returned no results (ParaxialFocus does this; so can a run that did not complete)",
                      "no_results", family="tolerancing_run")
    try:
        xs = _ac._marshal_array(ds.XData.Data)
        y_rows = _ac._marshal_array(ds.YData.Data)
    except Exception as exc:  # noqa: BLE001 -- an empty / wrong-rank / unreadable array
        raise _refuse(f"the Quick Yield curve could not be marshalled ({safe_exc(exc)}); refusing to report a guessed "
                      "number", "curve_invalid", family="tolerancing_parse")
    ys = [row[0] if isinstance(row, list) else row for row in y_rows]
    try:
        label = ds.XLabel
    except Exception:  # noqa: BLE001 -- no label: None, not a default
        label = None
    return xs, ys, (label if isinstance(label, str) else None)


def _safe(fn):
    try:
        return _sval(fn())
    except Exception as exc:  # noqa: BLE001
        return f"<unreadable: {type(exc).__name__}>"


def qy_once(system, cfg):
    """ONE Quick Yield analysis: settings, read-back proof, capped poll, curve, ``Close()`` in ``finally``.

    ``cfg["field"]`` is the field this analysis asks for (``None`` = all fields); the read-back must show it.

    Returns ``{xs, ys, x_label, wall_s, readback, probe}``. Raises ``ToleranceError`` (the analysis is closed) or
    ``EngineBusy`` (a cap whose stop was not confirmed: the analysis is NOT closed and nothing else is touched).
    """
    idm = _qy_enum(system, "Analysis", "AnalysisIDM")
    precisions = _qy_enum(system, "Analysis.Tolerancing", "QYPrecisions")
    strategies = _qy_enum(system, "Analysis.Tolerancing", "QYCompensatorStrategy")
    an = system.Analyses.New_Analysis(idm.QuickYield)
    busy = False
    try:
        settings = an.GetSettings()
        typed = getattr(settings, "__implementation__", None) or settings
        typed.NumMonteCarlo = int(cfg["trials"])
        typed.Precision = precisions.Standard
        typed.CompensatorStrategy = strategies.OptimizeAllDampedLeastSquares
        typed.PupilSampling = int(cfg["pupil"])
        field_k = cfg.get("field")                      # None = "Field: All" (composite); k = one field (per field)
        msgs = ({"UseAllFields": _safe(typed.Field.UseAllFields)} if field_k is None else
                {"SetFieldNumber": _safe(lambda: typed.Field.SetFieldNumber(int(field_k)))})
        msgs["UseAllWavelengths"] = _safe(typed.Wavelength.UseAllWavelengths)
        readback = {"NumMonteCarlo": _safe(lambda: int(typed.NumMonteCarlo)), "Precision": _safe(lambda: str(typed.Precision)),
                    "CompensatorStrategy": _safe(lambda: str(typed.CompensatorStrategy)),
                    "PupilSampling": _safe(lambda: int(typed.PupilSampling)),
                    "Compensation": _safe(lambda: str(typed.Compensation)), "Statistic": _safe(lambda: str(typed.Statistic)),
                    "field_number": _safe(lambda: int(typed.Field.GetFieldNumber())),
                    "wavelength_number": _safe(lambda: int(typed.Wavelength.GetWavelengthNumber())),
                    "settings_messages": msgs}
        seed = {}
        if hasattr(typed, "Seed"):
            seed["Seed"] = _safe(lambda: typed.Seed)
        if hasattr(typed, "RandomSeed"):
            seed["RandomSeed"] = _safe(lambda: typed.RandomSeed)
        if hasattr(typed, "MonteCarloSeed"):
            seed["MonteCarloSeed"] = _safe(lambda: typed.MonteCarloSeed)
        readback["seed_attributes_found"] = seed
        bad = [k for k, want in (("NumMonteCarlo", cfg["trials"]), ("PupilSampling", cfg["pupil"]),
                                 ("CompensatorStrategy", STRATEGY),
                                 ("field_number", ALL_SELECTED if field_k is None else field_k),
                                 ("wavelength_number", ALL_SELECTED)) if readback[k] != want]
        if bad:
            raise _refuse(f"Quick Yield did not take the requested settings ({', '.join(bad)}): read back "
                          f"{ {k: readback[k] for k in bad} }; nothing was run", "readback_mismatch", family="tolerancing_run")
        probe = {}
        t0 = _clock()
        an.Apply()
        try:
            capped = poll_until_done(an, cfg["cap_s"], probe)
        except PollError as exc:
            busy = True
            raise EngineBusy(f"IsRunning could not be read ({exc}); the analysis state is unknown")
        wall_s = round(_clock() - t0, 3)
        if capped:
            if not _stop_after_cap(an, probe):
                busy = True
                raise EngineBusy(f"Quick Yield was capped at {cfg['cap_s']:g} s and could not be confirmed stopped")
            raise _refuse(f"Quick Yield was capped at {cfg['cap_s']:g} s: no result (unjudged)", "capped",
                          family="tolerancing_run")
        xs, ys, x_label = _read_curve(an)
        check_curve(xs, ys)
        return {"xs": xs, "ys": ys, "x_label": x_label, "wall_s": wall_s, "readback": readback, "probe": probe}
    finally:
        if not busy:
            try:
                an.Close()
            except Exception:  # noqa: BLE001 -- a teardown throw never masks the body
                pass


# --------------------------------------------------------------------------- #
# the call
# --------------------------------------------------------------------------- #
def _count_cpar(system):
    try:
        return sum(1 for r in _tc.read_tde_rows(system.TDE) if r.get("type") == "CPAR")
    except Exception:  # noqa: BLE001 -- unreadable: say nothing rather than guess
        return None


def run(session, params):
    """``tolerance(mode="quick_yield")``. Never raises."""
    try:
        return _run(session, params)
    except _tc.ToleranceError as exc:
        reason = getattr(exc, "reason", None)
        extra = {} if reason is None else {"reason": reason}
        extra.update(getattr(exc, "qy_proof", None) or {})
        if isinstance(exc, EngineBusy):
            extra.update(state_mutated=True, tde_cleared=False, recovery=ENGINE_BUSY_RECOVERY)
        return _ac.error_envelope(TOOL, getattr(exc, "family", "tolerancing_param"), str(exc), mode=MODE, **extra)
    except Exception as exc:  # noqa: BLE001 -- the dispatch net
        return _ac.error_envelope(TOOL, "tolerancing_run", f"quick_yield hit an unexpected fault ({safe_exc(exc)})",
                                  reason="internal_error", mode=MODE, **(getattr(exc, "qy_proof", None) or {}))


def _stat_blocks(yld, stats, thr):
    """The ``yield`` / ``p50`` / ``p90`` / ``mean`` blocks of one field's (or the composite's) pooled repeats."""
    return {"yield": dict(yld, threshold=thr),
            "p50": dict(stats["p50"], definition="mean over repeats of each repeat's own P50 read off its curve"),
            "p90": dict(stats["p90"], definition="mean over repeats of each repeat's own P90 read off its curve"),
            "mean": dict(stats["mean"], definition="mean of the curve's criterion over its 1-99 % points")}


def _curve_block(curves, x_label):
    """The reported curve of one series of repeats: the one curve, or the mean of quantiles over the repeats."""
    ys0 = curves[0][1]
    many = len(curves) > 1
    same_grid = all(c[1] == ys0 for c in curves)
    curve_x = [_mean([c[0][j] for c in curves]) for j in range(len(curves[0][0]))] if many and same_grid else curves[0][0]
    curve_def = ("the one repeat's curve" if not many else
                 "the MEAN over repeats of each repeat's criterion at the same yield points (a mean of quantiles, not "
                 "the pooled distribution: reading it at the threshold need not equal 'yield'); see repeat_curves"
                 if same_grid else "the FIRST repeat's curve only (the repeats' yield grids differ); see repeat_curves")
    return {"yield_percent": ys0, "criterion": curve_x, "criterion_definition": curve_def,
            "repeat_curves": [{"yield_percent": c[1], "criterion": c[0]} for c in curves] if many else None,
            "x_label": x_label}


def _worst_index(fields):
    """The worst field: the lowest pooled yield, tie -> the higher P90 (a missing P90 loses the tie), then the lower field."""
    def key(j):
        p90 = fields[j]["p90"]["value"]
        return (fields[j]["yield"]["value"], -(p90 if p90 is not None else -math.inf), j)
    return min(range(len(fields)), key=key)


def _yield_interval(yld):
    """The range the TRUE pooled yield can take: a value for an exact read; ``[0, v]`` for an upper bound; ``[v, 1]`` for a
    lower bound; ``[0, 1]`` when the repeats mix both (a censored read is a bound, never a number)."""
    v, bound = yld["value"], yld["bound"]
    if bound == "exact":
        return v, v
    if bound == "upper":
        return 0.0, v
    if bound == "lower":
        return v, 1.0
    return 0.0, 1.0


def _competing_fields(fields, w):
    """The fields (numbers) whose reads leave the choice of worst field ``w`` uncertain: another field whose true yield
    can lie below the picked field's true yield (a censored bound admits it), or one that ties it on yield AND P90."""
    lo_w, hi_w = _yield_interval(fields[w]["yield"])
    out = []
    for j, f in enumerate(fields):
        if j == w:
            continue
        lo, hi = _yield_interval(f["yield"])
        tie = (not f["yield"]["censored"] and not fields[w]["yield"]["censored"]
               and f["yield"]["value"] == fields[w]["yield"]["value"] and f["p90"]["value"] == fields[w]["p90"]["value"])
        if lo < hi_w or tie:
            out.append(f["field"])
    return out


_UPPER_BOUND_LABEL = ("the lowest of the per-field MARGINAL pass rates, each field drawn separately: an UPPER BOUND on "
                      "the pass rate of every field passing in one trial")
_LOWER_OF_BOUND_LABEL = (_UPPER_BOUND_LABEL + "; censored from above (>=) here, so only a lower bound of that bound")


def _run(session, params):
    tr = _tr()
    system = session.system
    cfg = validate(params)
    per_field = cfg["statistic"] == PER_FIELD
    n_fields = read_field_count(system)             # F: read before anything opens
    thresholds = field_thresholds(cfg["threshold"], n_fields) if per_field else [cfg["threshold"]]
    check_baseline(cfg["baseline"], cfg["statistic"], n_fields)
    n_series = n_fields if per_field else 1         # analyses per repeat
    analyses = cfg["repeats"] * n_series
    tr._dls_preflight(system)                       # the wizard's back-focus COMP needs an air gap to move
    free0 = _mem_available_gb()
    priv0, priv_reason = _engine_private_gb(session)
    growth0 = engine_growth_gb(session, priv0)      # records the engine's first reading; 0.0 on its first call here
    ok, need = check_memory(analyses, cfg["trials"], free0)
    if not ok:
        held = "" if not growth0 else f" This engine already holds {growth0:.1f} GB more than when this tool first saw it."
        how = f"{n_fields} fields x {cfg['repeats']} repeats" if per_field else f"{cfg['repeats']} repeats"
        raise _refuse(f"memory floor: {free0:.1f} GB available, this call holds about {need:.1f} GB in the engine "
                      f"({analyses} analyses ({how}) x {LEAK_GB_PER_1000} GB per 1000 runs) and the floor is "
                      f"{MEM_FLOOR_GB:.0f} GB free after it.{held} Remedy: {RELEASE_REMEDY}; or lower trials / repeats. "
                      "Nothing was opened", "memory_floor", family="tolerancing_run")
    warnings = []
    lde_before = _tc.snapshot_lde(system)
    system_file = _tc._safe_system_file(system)
    t_start = _clock()
    budget = cfg["budget"]
    cpar_found = busy = None
    records = [[] for _ in range(n_series)]         # per series (field k = index k - 1, or the one composite)
    curves = [[] for _ in range(n_series)]
    series_walls = [[] for _ in range(n_series)]
    series_readback = [None] * n_series
    walls, readback, probe, x_label = [], None, None, None
    priv_after = []
    tde_cleared = False
    topology = rule_id = budget_digest = None
    stopped = failure = None
    try:
        cpar_found = _count_cpar(system)
        if cpar_found:
            warnings.append(
                f"cpar_rows_found: the TDE carried {cpar_found} CPAR row(s). The wizard clears the TDE before Quick "
                "Yield runs, so none reached it: this run is refocus-only. Quick Yield is generally not for use with "
                "the push (it reads a push optimistically and cannot count at-bound pushes); judge the push with one "
                "native pick_push / monte_carlo call")
        wiz = _tc.apply_wizard_budget(system, budget)
        effective = _tc.check_wizard_rows(wiz["rows"], wiz["back_surface"], budget)
        eff_budget = dict(budget, **effective)
        rule_id = _tc.budget_rule_id(eff_budget, None, False)
        budget_digest = _tc.budget_digest(_tc.read_tde_rows(system.TDE))
        try:
            topology = _tc.topology_digest(system)
        except Exception:  # noqa: BLE001
            topology = None
            warnings.append("topology_digest could not be read; paired reads against this result cannot be comparable")
        for i in range(cfg["repeats"]):
            if i > 0:
                # re-checked at a repeat boundary only: every field then pools the same number of repeats
                ok, need = check_memory((cfg["repeats"] - i) * n_series, cfg["trials"], _mem_available_gb())
                if not ok:
                    stopped = f"memory floor reached before repeat {i + 1} of {cfg['repeats']}"
                    warnings.append(f"memory_floor: {stopped}; the result pools the {i} finished repeat(s). Remedy: {RELEASE_REMEDY}")
                    break
            for j in range(n_series):
                one = qy_once(system, dict(cfg, field=(j + 1) if per_field else None))
                records[j].append(run_record(one["xs"], one["ys"], thresholds[j]))
                curves[j].append((one["xs"], one["ys"]))
                series_walls[j].append(one["wall_s"])
                series_readback[j] = one["readback"]
                walls.append(one["wall_s"])
                readback, probe, x_label = one["readback"], one["probe"], one["x_label"]
                priv_after.append(_engine_private_gb(session)[0])
    except EngineBusy:
        busy = True
        raise
    except Exception as exc:
        if isinstance(exc, _tc.ToleranceError):
            failure = exc
            raise
        # a foreign (possibly pythonnet-wrapped .NET) exception: re-raise as our own type, which is known to take the
        # proof attribute and to render safely; never assign onto, or interpolate, the foreign object
        failure = _tc.ToleranceError(f"quick_yield hit an unexpected fault ({safe_exc(exc)})",
                                     family="tolerancing_run", reason="internal_error")
        raise failure from None
    finally:
        if not busy:
            try:
                tr._clear_tde(system.TDE)
                tde_cleared = True
            except Exception:  # noqa: BLE001 -- reported below, never masks the body
                tde_cleared = False
            if failure is not None:
                # a failure after the wizard ran still PROVES the TDE and the lens (the picker reads an envelope that
                # names no TDE state as unproven)
                try:
                    mutated = not _tc.lde_unchanged(lde_before, _tc.snapshot_lde(system))
                except Exception:  # noqa: BLE001 -- unreadable: not proven unchanged
                    mutated = True
                failure.qy_proof = {"tde_cleared": tde_cleared, "state_mutated": mutated}
    free1 = _mem_available_gb()
    priv1, _r = _engine_private_gb(session)
    growth1 = engine_growth_gb(session, priv1)
    if priv1 is not None and priv1 > SOFT_PRIVATE_GB:
        warnings.append(f"engine_memory_high: the engine holds {priv1:.1f} GB (soft limit {SOFT_PRIVATE_GB:.0f} GB); "
                        f"Close() does not release Quick Yield's memory. Remedy: {RELEASE_REMEDY}")
    lde_after = _tc.snapshot_lde(system)
    state_mutated = not _tc.lde_unchanged(lde_before, lde_after)
    if state_mutated:
        warnings.append("state_mutated: the LDE differs from before the call; reload your saved design")
    if not tde_cleared:
        warnings.append("tde_not_cleared: the Tolerance Data Editor could not be proven empty")

    pooled = [summarise(records[j], cfg["trials"]) for j in range(n_series)]
    blocks = [_stat_blocks(pooled[j][0], pooled[j][1], thresholds[j]) for j in range(n_series)]
    cblocks = [_curve_block(curves[j], x_label) for j in range(n_series)]
    fields = None
    if per_field:
        fields = [dict(blocks[j], field=j + 1, threshold=thresholds[j], curve=dict(cblocks[j], field=j + 1),
                       wall_s_per_run=series_walls[j], readback=series_readback[j]) for j in range(n_series)]
        w = _worst_index(fields)
        best = fields[w]
        competing = _competing_fields(fields, w)
        uncertain = bool(competing)
        head_censored = best["yield"]["censored"] or uncertain
        censored_fields = [f["field"] for f in fields if f["yield"]["censored"]]
        if censored_fields:
            warnings.append(
                f"censored: on field(s) {censored_fields} the field's threshold lies outside the curve's range for at "
                "least one repeat; that field's yield is a bound, not a value")
        if uncertain:
            warnings.append(
                f"selection_uncertain: field {best['field']} has the lowest read, but the censored read(s) or tie of "
                f"field(s) {competing} admit a lower true yield; the worst field and the headline are not a definite value")
        worst_field = {"field": best["field"], "yield": best["yield"]["value"], "p90": best["p90"]["value"],
                       "threshold": best["threshold"], "censored": head_censored,
                       "bound": best["yield"]["bound"], "selection_uncertain": uncertain, "competing_fields": competing}
        label = _LOWER_OF_BOUND_LABEL if best["yield"]["bound"] in ("lower", "mixed") else _UPPER_BOUND_LABEL
        top = {"yield": dict(best["yield"], field=best["field"], censored=head_censored, selection_uncertain=uncertain,
                             competing_fields=competing, statistic=label + ("; the worst field is uncertain" if uncertain else "")),
               "p50": best["p50"], "p90": best["p90"], "mean": best["mean"]}
        curve, threshold_out = best["curve"], thresholds
    else:
        top, curve, threshold_out, worst_field = blocks[0], cblocks[0], cfg["threshold"], None
        if pooled[0][0]["censored"]:
            warnings.append(
                f"censored: the threshold {cfg['threshold']:g} lies outside the curve's range for at least one repeat; the "
                f"yield is a {pooled[0][0]['bound']} bound, not a value")
    seed_found = (readback or {}).get("seed_attributes_found") or {}
    key = {"threshold": threshold_out, "trials": cfg["trials"], "pupil": cfg["pupil"], "compensator": STRATEGY,
           "budget_digest": budget_digest, "topology_digest": topology, "statistic": cfg["statistic"],
           "field_count": n_fields}
    result = {
        "ok": True, "tool": TOOL, "mode": MODE, "id": new_id(),
        "statistic": STATISTIC if per_field else STATISTIC_COMPOSITE,
        "comparable_with_native_mc": False,
        "criterion": ("RMSWavefront (Quick Yield, one analysis per field)" if per_field else
                      "RMSWavefront (Quick Yield, Field: All composite)"), "criterion_units": "waves",
        "compensation": "dls_back_focus", "compensator": STRATEGY, "refocus_only": True,
        "push": "not built: Quick Yield is generally not for use with the push",
        "threshold": threshold_out, "trials": cfg["trials"], "pupil": cfg["pupil"], "repeats": cfg["repeats"],
        "repeats_done": len(records[0]), "budget_rule_id": rule_id,
        "yield": top["yield"], "p50": top["p50"], "p90": top["p90"], "mean": top["mean"],
        "curve": curve,
        "analyses_run": len(walls), "wall_s_per_run": walls, "wall_s": round(_clock() - t_start, 3),
        "readback": readback, "poll_probe": probe,
        "deterministic": False, "seed_supported": bool(seed_found), "seed": seed_found or None,
        "seed_note": ("Quick Yield settings expose a seed attribute: echoed in 'seed'" if seed_found else
                      f"no seed attribute found on the Quick Yield settings (probed {list(_SEED_ATTRS)}); two identical "
                      "calls differ, and none is invented here"),
        "memory": {"available_gb_before": round(free0, 2), "available_gb_after": round(free1, 2),
                   "engine_private_gb_before": priv0, "engine_private_gb_after_each_close": priv_after,
                   "engine_private_gb_after": priv1, "engine_private_note": priv_reason,
                   "engine_private_growth_gb": growth1, "engine_private_growth_before_call_gb": growth0,
                   "soft_limit_gb": SOFT_PRIVATE_GB, "analyses_expected": analyses,
                   "expected_hold_gb": round(LEAK_GB_PER_1000 * cfg["trials"] / 1000.0 * analyses, 2),
                   "floor_gb": MEM_FLOOR_GB},
        "cpar_rows_found": cpar_found, "system_file": system_file,
        "tde_cleared": tde_cleared, "state_mutated": state_mutated, "warnings": warnings,
        "key": key,
    }
    if per_field:
        result["field_count"] = n_fields
        result["worst_field"] = worst_field
        result["fields"] = fields
    if stopped:
        result["stopped_early"] = stopped
    result["stored"] = not state_mutated and tde_cleared
    if result["stored"]:
        store_put(result)
    else:
        warnings.append("not_stored: the lens or the TDE was not proven clean after this run, so its id cannot be a "
                        "baseline")
    if cfg["baseline"] is not None:
        result["paired"] = paired(cfg["baseline"], result)
    return result
