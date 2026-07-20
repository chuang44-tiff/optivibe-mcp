"""tools/analysis_mtf.py — get_mtf: FFT MTF typed-array extraction.

``get_mtf`` runs the FFT MTF analysis (``Analyses.New_FftMtf()`` ->
``ApplyAndWaitForCompletion`` -> ``GetResults()``) and marshals each data series'
X (spatial-frequency grid, ``System.Double[]`` rank 1, length 300 in the baseline)
and Y (modulation, ``System.Double[,]`` rank 2, shape [300, 2]) arrays to JSON
lists. Per the locked column ordering: col0 = Tangential, col1 = Sagittal.
Every cell passes through ``safe_float``.

Completion canary (§d): ``results.NumberOfDataSeries > 0``; else the
structured ``analysis_empty`` envelope.

This module also hosts ``_analysis_idm`` — the shared resolver for an
``AnalysisIDM`` member off the live ``ZOSAPI.Analysis`` namespace — reused by
``analysis_spot`` and ``analysis_graphic``.

Live ZOS-API integration: exercised by the live test (4 series, Y[3]~0.99888
within 2%); unit-tested against a fixture-seeded fake MTF result.
"""
import math

from .._io import safe_float
from ..errors import AnalysisResultError, ToolParamError
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _config_common as _cfg

# (S7 #16) Auto-extend the FFT-MTF grid to cover requested at_frequencies. The default
# grid caps at 30.0 cyc/mm (probe-frozen), so an at_frequencies element above it silently
# returns null+note. When max_frequency is ABSENT, auto-raise the ceiling to cover the
# request (round up with a small pad so the target sits strictly INSIDE the grid, not at
# the excluded edge). An explicit max_frequency is HONORED — never overridden.
_MTF_DEFAULT_GRID_MAX = 30.0     # probe: the engine's default FFT-MTF grid cap (cyc/mm)
_MTF_AUTO_EXTEND_PAD = 1.1       # round needed UP so the target sits strictly INSIDE the
                                 # grid (not at the excluded grid edge); ceil() after
_MTF_AUTO_EXTEND_MAX = 2000.0    # SOFT cap (cyc/mm): beyond it, fall through to the honest
                                 # null+note, never auto-author a garbage grid


def _resolve_single_config(system, params, tool_name):
    """Resolve a SINGLE-config selector (None|int) — ``config="all"`` is REFUSED (D1).

    MTF/spot are the heavy-analysis class (the render-obscured-slow stall on the single
    serialized seat), so they offer ``config=None|int`` but NOT the ``"all"`` sweep. Thin
    wrapper over the shared ``_config_common.resolve_single_config_selector`` (one
    contract) — extracts ``config`` from ``params`` and delegates. Returns the int config to
    read at (``None`` -> the active config, no switch), or RAISES ``ToolParamError``.
    """
    config = params.get("config") if isinstance(params, dict) else None
    return _cfg.resolve_single_config_selector(system, config, tool_name)


def _validate_at_frequencies(value):
    """Validate the optional ``at_frequencies`` param -> a list of finite floats >= 0.

    Mirrors how ``series`` is validated (a strict pre-flight raise -> the dispatch
    envelope catches it). Requires a NON-EMPTY list/tuple of real numbers (an int
    or float, NOT a bool) that are each FINITE and ``>= 0`` (a negative frequency /
    nan / inf / string / bool element is rejected). Returns the list of floats.
    """
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise ToolParamError(
            f"at_frequencies must be a non-empty list of numbers >= 0, got "
            f"{type(value).__name__} {value!r}"
        )
    if len(value) == 0:
        raise ToolParamError("at_frequencies must be a non-empty list of numbers >= 0")
    out = []
    for f in value:
        if isinstance(f, bool) or not isinstance(f, (int, float)):
            raise ToolParamError(
                f"at_frequencies elements must be numbers, got "
                f"{type(f).__name__} {f!r}"
            )
        ff = float(f)
        if not math.isfinite(ff):
            raise ToolParamError(
                f"at_frequencies elements must be finite numbers, got {f!r}"
            )
        if ff < 0:
            raise ToolParamError(
                f"at_frequencies elements must be >= 0, got {f!r}"
            )
        out.append(ff)
    return out


def _interp_at(freq_grid, y_values, target):
    """Linearly interpolate ``y_values`` at ``target`` over the monotonic ``freq_grid``.

    ``freq_grid`` is the spatial-frequency X grid (monotonic increasing, the FFT
    MTF grid); ``y_values`` is the matching modulation column (same length). Rules:

    - an EXACT grid hit returns the exact grid value;
    - a ``target`` strictly between two grid points is LINEAR-interpolated between
      the bracketing pair;
    - ``target`` below the grid min (usually 0) interpolates from the first point
      (the grid starts at 0 in practice, so this is normally an exact hit);
    - ``target`` ABOVE the grid max returns ``(None, "beyond MTF grid max …")`` (MTF
      past the cutoff is ~0 but we do NOT fabricate it).

    Returns ``(value, note)`` — ``note`` is ``None`` unless the point is beyond the
    grid max. A non-finite marshalled grid/Y cell (a string sentinel from
    ``safe_float``) is skipped for interpolation math by treating it as unusable ->
    the bracketing pair is used as-is (the sentinel surfaces in the full-grid mode;
    in summary mode an interpolation that would need a sentinel cell returns None).
    """
    n = len(freq_grid)
    if n == 0:
        return None, "empty MTF grid"
    fmax = freq_grid[-1]
    fmin = freq_grid[0]
    # Beyond the grid max -> null + note (MTF past cutoff ~ 0; do not fabricate).
    if isinstance(fmax, (int, float)) and target > fmax:
        return None, f"beyond MTF grid max {fmax}"
    # At/below the grid min -> the first grid value (the grid starts at 0).
    if isinstance(fmin, (int, float)) and target <= fmin:
        return _as_finite(y_values[0]), None
    # Find the bracketing pair [i-1, i] with freq_grid[i-1] <= target <= freq_grid[i].
    for i in range(1, n):
        lo = freq_grid[i - 1]
        hi = freq_grid[i]
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            continue
        if lo <= target <= hi:
            ylo = _as_finite(y_values[i - 1])
            yhi = _as_finite(y_values[i])
            if ylo is None or yhi is None:
                return None, "non-finite modulation at the interpolation point"
            if hi == lo:  # a duplicated grid point -> exact value
                return ylo, None
            frac = (target - lo) / (hi - lo)
            return ylo + frac * (yhi - ylo), None
    # Should not reach here for an in-range monotonic grid; be defensive.
    return None, f"frequency {target} not bracketed by the MTF grid"


def _as_finite(value):
    """Return ``value`` as a float if it is a finite number, else ``None``.

    A marshalled MTF cell is either a finite float or a ``safe_float`` STRING
    sentinel ("inf"/"-inf"/"nan"); a sentinel is not interpolatable, so it maps to
    ``None`` (the caller emits a null point rather than fabricating a number).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _analysis_idm(system, member_name):
    """Resolve an ``AnalysisIDM`` enum member off the live ``ZOSAPI.Analysis``.

    A fake system injects an ``_analysis_idm`` mapping (``{name: member}``) so unit
    tests resolve without the backend; otherwise the live ``ZOSAPI.Analysis``
    namespace's ``AnalysisIDM`` enum is imported and the member resolved by name.
    """
    injected = getattr(system, "_analysis_idm", None)
    if injected is not None and member_name in injected:
        return injected[member_name]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Analysis as _an  # type: ignore

        return getattr(_an.AnalysisIDM, member_name)
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve AnalysisIDM.{member_name} from ZOSAPI.Analysis: {exc}"
        )


def get_mtf(session, params):
    """Run FFT MTF and return per-series frequency/tangential/sagittal lists.

    ``series`` (int | None, default None=all) restricts the output to one series
    (field); ``max_frequency`` (number, optional) sets the FFT spatial-frequency
    grid ceiling via ``IAS_FftMtf.MaximumFrequency`` (default: engine default,
    typically ~150 cyc/mm). A zero ``NumberOfDataSeries`` returns the
    ``analysis_empty`` envelope (§d).

    SUMMARY MODE (``at_frequencies``, a list of finite numbers >= 0): instead of
    the full ~300-point grid (the overflow), each series returns ONLY
    the interpolated tangential + sagittal at each requested frequency
    (``{index, label, at:[{frequency, tangential, sagittal, [note]}]}``). An exact
    grid hit is exact; a frequency beyond the grid max is ``null`` + a note. With NO
    ``at_frequencies`` the full-grid behaviour is unchanged.

    ``config`` (None|int) selects the configuration — SINGLE-config only,
    ``config="all"`` is REFUSED (MTF is the heavy-analysis class). Reads inside a
    ``with_configuration`` wrap that ALWAYS restores the active config.
    """
    cfg_idx = _resolve_single_config(session.system, params, "get_mtf")
    if cfg_idx is None:
        result = _get_mtf_at(session, params)
        if isinstance(result, dict):
            result.setdefault(
                "config_evaluated", _cfg.safe_current_configuration(session.system)
            )
        return result
    with _cfg.with_configuration(session.system, cfg_idx) as ctx:
        result = _get_mtf_at(session, params)
    if isinstance(result, dict):
        result.setdefault("config_evaluated", cfg_idx)
        if not ctx["restore_verified"]:
            result["mutation_warning"] = ctx["mutation_warning"]
        if not ctx["switched"] and ctx["mutation_warning"]:
            result.setdefault("config_switch_warning", ctx["mutation_warning"])
    return result


def _get_mtf_at(session, params):
    """The pure per-config FFT-MTF body (read at the ACTIVE config). See ``get_mtf``."""
    system = session.system

    series_sel = params.get("series")
    if series_sel is not None:
        if isinstance(series_sel, bool) or not isinstance(series_sel, int):
            raise ToolParamError(
                f"series must be an integer index, got {series_sel!r}"
            )
        series_sel = int(series_sel)

    at_frequencies = None
    if params.get("at_frequencies") is not None:
        at_frequencies = _validate_at_frequencies(params["at_frequencies"])

    max_frequency = params.get("max_frequency")
    if max_frequency is not None:
        if isinstance(max_frequency, bool) or not isinstance(
            max_frequency, (int, float)
        ):
            raise ToolParamError(
                f"max_frequency must be a finite positive number, got "
                f"{type(max_frequency).__name__} {max_frequency!r}"
            )
        max_frequency = float(max_frequency)
        if not math.isfinite(max_frequency) or max_frequency <= 0:
            raise ToolParamError(
                f"max_frequency must be a finite positive number (> 0), got "
                f"{max_frequency!r}"
            )

    # (S7 #16) Auto-extend the grid for a requested at_frequency above the default cap.
    # Fires ONLY when max_frequency is ABSENT (an explicit ceiling is the caller's stated
    # intent — never overridden; a beyond-explicit element stays the existing null+note).
    # A request beyond the SOFT cap falls through to the honest "beyond MTF grid max"
    # null+note (NOT a refusal, NOT a garbage grid). _validate_at_frequencies already
    # rejected a non-finite element pre-mutation.
    max_frequency_auto_extended = False
    if at_frequencies and max_frequency is None:
        needed = max(at_frequencies)
        if _MTF_DEFAULT_GRID_MAX < needed <= _MTF_AUTO_EXTEND_MAX:
            max_frequency = float(max(
                _MTF_DEFAULT_GRID_MAX,
                math.ceil(needed * _MTF_AUTO_EXTEND_PAD),
            ))
            max_frequency_auto_extended = True

    analysis = system.Analyses.New_FftMtf()
    max_frequency_applied = None
    if max_frequency is not None:
        max_frequency_applied = False
        try:
            settings = analysis.GetSettings()
            # GetSettings() returns the bare IAS_ base, which has NO MaximumFrequency
            # (analyses.md §6 — the documented "bare IAS_ base" hazard). A plain
            # `settings.MaximumFrequency = X` sets a phantom Python attr on the COM
            # wrapper and the grid never moves (live-falsified). The concrete
            # AS_FftMtf view (which HAS the property) is reached via pythonnet's
            # __implementation__; the getattr fallback keeps unit fakes working.
            typed = getattr(settings, "__implementation__", settings)
            typed.MaximumFrequency = max_frequency
            # Read-back-as-proof: the typed view actually accepted the ceiling.
            if math.isclose(
                float(typed.MaximumFrequency), max_frequency,
                rel_tol=1e-9, abs_tol=1e-9,
            ):
                max_frequency_applied = True
        except Exception:  # noqa: BLE001 — degrade to engine default
            pass
    with _ac._run_analysis(analysis) as results:
        n = int(results.NumberOfDataSeries)
        # Completion canary (§d): zero series is a void result.
        if n <= 0:
            return _ac.error_envelope(
                "get_mtf", "analysis_empty",
                f"FFT MTF produced {n} data series",
            )

        if series_sel is not None:
            if not (0 <= series_sel < n):
                raise ToolParamError(
                    f"series {series_sel} out of range; valid 0..{n - 1}"
                )
            indices = [series_sel]
        else:
            indices = list(range(n))

        series_out = []
        for i in indices:
            ds = results.GetDataSeries(i)
            label = str(ds.SeriesLabels[0])
            # Per-series completion canary (§d): NumberOfDataSeries>0 is NOT
            # proof of data — a stale/void completion can carry an empty per-series
            # payload. Route BOTH X and Y through the marshalling helper that owns
            # the None/empty `analysis_empty` canary (never marshal *.Data directly
            # and bypass the guard), then assert the frequency<->modulation length
            # pairing (X.Length == Y rows) so a misaligned result surfaces as a
            # structured error, never `ok:true` with mismatched/empty arrays.
            try:
                freq = _ac._marshal_array(ds.XData.Data)
                y_marshalled = _ac._marshal_array(ds.YData.Data)
                # _split_mtf_columns must sit INSIDE this try (NEW-1): a non-2-col
                # Y raises AnalysisResultError(family="analysis_malformed"); if the
                # call were outside, that exception would escape past the tool
                # boundary instead of returning the locked {ok:false} envelope.
                tangential, sagittal = _ac._split_mtf_columns(y_marshalled)
            except AnalysisResultError as exc:
                return _ac.error_envelope(
                    "get_mtf", exc.family, str(exc), series_index=i,
                )
            if len(freq) != len(tangential):
                # Frequency count != modulation row count: frequency[i] no longer
                # pairs with tangential[i]. A wrong-number-as-success the canary
                # must intercept (BUG2/BUG3).
                return _ac.error_envelope(
                    "get_mtf", "analysis_malformed",
                    f"series {i}: frequency length {len(freq)} != modulation row "
                    f"count {len(tangential)} (X/Y arrays misaligned)",
                    series_index=i,
                )
            if at_frequencies is not None:
                # SUMMARY MODE: interpolate T/S at each requested frequency and OMIT
                # the full freq/tangential/sagittal arrays (the ~300-point overflow).
                at = []
                for target in at_frequencies:
                    t_val, t_note = _interp_at(freq, tangential, target)
                    s_val, s_note = _interp_at(freq, sagittal, target)
                    point = {
                        "frequency": target,
                        "tangential": safe_float(t_val) if t_val is not None else None,
                        "sagittal": safe_float(s_val) if s_val is not None else None,
                    }
                    note = t_note or s_note
                    if note is not None:
                        point["note"] = note
                    at.append(point)
                series_out.append({"index": i, "label": label, "at": at})
            else:
                series_out.append(
                    {
                        "index": i,
                        "label": label,
                        "frequency": freq,
                        "tangential": tangential,
                        "sagittal": sagittal,
                    }
                )

    # Spatial-frequency unit is per-lens-unit (cycles/mm for the baseline mm lens),
    # derived from the same SystemData.Units source (§6).
    lens_unit = _ac._lens_units_string(system)
    grid_max = None
    if series_out and "frequency" in series_out[0]:
        freq_data = series_out[0].get("frequency", [])
        if freq_data:
            last = freq_data[-1]
            if isinstance(last, (int, float)) and math.isfinite(last):
                grid_max = last
    result = {
        "ok": True,
        "number_of_data_series": n,
        "x_label": "Spatial Frequency",
        "x_units": f"cycles/{lens_unit}",
        "y_units": "modulation (dimensionless 0..1)",
        "series": series_out,
    }
    if grid_max is not None:
        result["grid_max"] = grid_max
    # Active disclosure: when a max_frequency was requested, say
    # whether the engine actually accepted it — a silent degrade is now explicit,
    # not inferred from grid_max alone.
    if max_frequency_applied is not None:
        result["max_frequency_applied"] = max_frequency_applied
    # (S7 #16) Disclose the auto-extension ONLY when it fired (default-grid result key
    # set stays byte-identical). max_frequency_auto_extended:true + max_frequency_applied:
    # false is the honest "tried to extend, engine didn't accept it" disclosure.
    if max_frequency_auto_extended:
        result["max_frequency_auto_extended"] = True
    return result


GET_MTF_SPEC = ToolSpec(
    name="get_mtf",
    handler=get_mtf,
    required_params=(),
    param_types={
        "series": "integer",
        "max_frequency": "number",
        "at_frequencies": "array",
        # config is SINGLE-config only (None|int); 'all' is refused. Advertised "number".
        "config": "number",
    },
    description=(
        "Measure image contrast vs spatial frequency (FFT MTF). Pass "
        "max_frequency to set the spatial-frequency grid ceiling (default: "
        "engine default); grid_max in the result reports the actual extent. By "
        "DEFAULT returns the full per-series grid plus tangential and sagittal "
        "modulation (col0=tangential, col1=sagittal) — can be ~300 points per "
        "series (large). Pass at_frequencies=[f1,f2,...] for SUMMARY mode: each "
        "series returns only the interpolated tangential+sagittal at those "
        "frequencies (compact; null+note past the grid max). The series map: idx0 = "
        "the diffraction-limit series, idx1..N = the fields in order; on-axis T == S."
    ),
)

TOOL_SPECS = (GET_MTF_SPEC,)
