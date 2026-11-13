"""Engine-free checks for the arithmetic behind tolerance(mode="quick_yield"); no engine is opened."""
import math

import pytest

from optivibe_harness.tools import _tolerance_common as tc
from optivibe_harness.tools import _tolerance_quick_yield as qy

XS = [0.02, 0.04, 0.06, 0.08, 0.10]          # criterion (waves) at each yield point
YS = [10.0, 30.0, 50.0, 70.0, 90.0]          # yield, percent


# ------------------------------------------------------------------ reading the curve
def test_yield_inside_the_curve_is_interpolated_and_exact():
    r = qy.yield_at(XS, YS, 0.05)
    assert r["bound"] == "exact"
    assert r["value"] == pytest.approx(0.40)


def test_yield_outside_the_curve_is_a_bound_never_an_extrapolation():
    below = qy.yield_at(XS, YS, 0.01)
    above = qy.yield_at(XS, YS, 0.50)
    assert (below["bound"], below["value"]) == ("upper", pytest.approx(0.10))
    assert (above["bound"], above["value"]) == ("lower", pytest.approx(0.90))


def test_criterion_at_a_yield_reads_the_curve_and_is_none_outside_it():
    assert qy.x_at_yield(XS, YS, 50.0) == pytest.approx(0.06)
    assert qy.x_at_yield(XS, YS, 95.0) is None


@pytest.mark.parametrize("xs, ys", [
    ([0.02], [10.0]),                       # too short
    ([0.04, 0.02], [10.0, 30.0]),           # x descending
    ([0.02, 0.04], [10.0, 130.0]),          # yield above 100
    ([0.02, float("nan")], [10.0, 30.0]),   # non-finite
])
def test_an_unreadable_curve_is_refused(xs, ys):
    with pytest.raises(tc.ToleranceError) as exc:
        qy.check_curve(xs, ys)
    assert exc.value.reason == "curve_invalid"


# ------------------------------------------------------------------ pooling repeats
def _rec(y, p90=0.09, bound="exact"):
    return {"yield": y, "bound": bound, "p50": 0.06, "p90": p90, "mean": 0.06}


def test_repeats_pool_to_the_mean_with_a_repeat_sd_standard_error():
    out, stats = qy.summarise([_rec(0.50), _rec(0.60)], trials=1000)
    sd = math.sqrt(((0.50 - 0.55) ** 2 + (0.60 - 0.55) ** 2) / 1)
    assert out["value"] == pytest.approx(0.55)
    assert out["se"] == pytest.approx(sd / math.sqrt(2))
    assert out["se_kind"] == "repeat_sd/sqrt(repeats)"
    assert out["censored"] is False


def test_one_repeat_falls_back_to_a_binomial_floor():
    out, stats = qy.summarise([_rec(0.40)], trials=1000)
    assert out["se"] == pytest.approx(math.sqrt(0.40 * 0.60 / 1000))
    assert out["se_kind"].startswith("binomial")
    assert stats["p90"]["se"] is None


def test_a_censored_repeat_marks_the_pool_censored():
    out, _ = qy.summarise([_rec(0.10, bound="upper"), _rec(0.12)], trials=1000)
    assert out["censored"] is True
    assert out["bound"] == "upper"


# ------------------------------------------------------------------ thresholds
def test_one_threshold_applies_to_every_field_and_a_list_must_match_the_field_count():
    assert qy.field_thresholds(0.07, 3) == [0.07, 0.07, 0.07]
    assert qy.field_thresholds([0.05, 0.06], 2) == [0.05, 0.06]
    with pytest.raises(tc.ToleranceError) as exc:
        qy.field_thresholds([0.05, 0.06], 3)
    assert exc.value.reason == "threshold_length"


# ------------------------------------------------------------------ the worst field
def _field(n, y, p90=0.09, bound="exact"):
    return {"field": n, "yield": {"value": y, "bound": bound, "censored": bound != "exact"},
            "p90": {"value": p90}}


def test_the_worst_field_is_the_lowest_yield_and_a_tie_goes_to_the_higher_p90():
    fields = [_field(1, 0.6), _field(2, 0.4, p90=0.08), _field(3, 0.4, p90=0.11)]
    assert fields[qy._worst_index(fields)]["field"] == 3


def test_a_censored_field_that_can_lie_below_the_pick_is_named_as_competing():
    fields = [_field(1, 0.30), _field(2, 0.35, bound="upper"), _field(3, 0.80)]
    w = qy._worst_index(fields)
    assert fields[w]["field"] == 1
    assert qy._competing_fields(fields, w) == [2]


# ------------------------------------------------------------------ paired reads
def _result(rid, y, se, censored=False, **key):
    k = {"threshold": [0.0714], "trials": 1000, "pupil": 2, "compensator": "dls", "budget_digest": "b",
         "topology_digest": "t", "statistic": "per_field", "field_count": 1}
    k.update(key)
    stat = {"value": 0.09, "se": 0.001}
    return {"id": rid, "key": k, "yield": {"value": y, "se": se, "censored": censored},
            "p90": dict(stat), "mean": dict(stat)}


def test_a_paired_difference_uses_the_combined_se_and_resolves_past_two_se():
    p = qy.paired(_result("a", 0.50, 0.02), _result("b", 0.60, 0.02))
    se = math.sqrt(0.02 ** 2 + 0.02 ** 2)
    assert p["comparable"] is True
    assert p["d_yield"]["value"] == pytest.approx(0.10)
    assert p["d_yield"]["se"] == pytest.approx(se)
    assert p["d_yield"]["resolved"] is True
    small = qy.paired(_result("a", 0.50, 0.02), _result("b", 0.52, 0.02))
    assert small["d_yield"]["resolved"] is False


def test_a_paired_read_of_different_settings_is_not_comparable_and_never_resolves():
    p = qy.paired(_result("a", 0.50, 0.01), _result("b", 0.90, 0.01, trials=2000))
    assert p["comparable"] is False
    assert "trials" in p["reason"]
    assert p["d_yield"]["resolved"] is False


def test_an_unreadable_digest_is_never_comparable():
    p = qy.paired(_result("a", 0.50, 0.01, budget_digest=None), _result("b", 0.90, 0.01, budget_digest=None))
    assert p["comparable"] is False


def test_a_censored_yield_is_never_resolved():
    p = qy.paired(_result("a", 0.10, 0.01, censored=True), _result("b", 0.90, 0.01))
    assert p["d_yield"]["resolved"] is False


# ------------------------------------------------------------------ the result store
def test_the_store_keeps_the_newest_results_and_drops_the_oldest():
    qy.store_clear()
    try:
        ids = ["qy-%02d" % i for i in range(qy.STORE_MAX + 1)]
        for rid in ids:
            qy.store_put({"id": rid})
        assert qy.store_get(ids[0]) is None
        assert qy.store_get(ids[1]) is not None
        assert qy.store_get(ids[-1]) is not None
    finally:
        qy.store_clear()
